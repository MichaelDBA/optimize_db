#!/usr/bin/env python3
#!/usr/bin/env python2
##################################################################################################
# pg_vacuum.py
#
# author: Michael Vitale, michaeldba@sqlexec.com
#
# Description: This program does dynamic FREEZEs, VACUUMs, and ANALYZEs based on PG statistics.
#              For large tables, it does these asynchronously, synchronous for all others.
#              The program constrains the number of processes running in asynchronous mode.
#              The program waits occasionally for 5 minutes if max processes is still surpassed.
#              The program avoids extremely large tables over 400GB, expecting them to be done manually.
#
# Date Created : June 19, 2016    Original Coding (v 1.0)
#                                 Tested with Ubuntu 14.04, PostgreSQL v9.6, Python 2.7
# Date Modified:
# March 15, 2019.  Updated for Python 3. Added asynchronous/monitoring logic.
#                                 Tested with Redhat 7.5, PostgreSQL v10, Python 3.6.3
# March 26, 2019.  Added Freeze logic.
# March 29, 2019.  Added better exception handling.
# April 25, 2019.  Added threshold for dead tups to parameters instead of hard coded default of 10,000.
#                  Fixed small table analyze query that didn't bring back correct result set.
# April 27, 2019.  Fixed my fix for small table analyze query. ha.
# May   24, 2019.  V2.3: Fixed vacuum/analyze check due to date comparison problem. Also reduced dead tuple threshold from 10,000 to 1,000
#                        Also added additional query to do vacuums/analyzes on tables not touched for over 2 weeks
# July  18, 2019.  V2.4: Fixed vacuum/analyze check query (Step 2).  Wasn't catching rows where no vacuums/analyzes ever done.
# Aug.  04, 2019.  V2.5: Add new parameter to do VACUUM FREEZE: --freeze. Default is not, but dryrun will show what could have been done.
# Dec.  15, 2019.  V2.6: Add schema filter support. Fixed bugs: not skipping duplicate tables, invalid syntax for vacuum with 2+ parms where need to be in parens.
# Dec.  16, 2019.  V2.6: Replace optparse with argparse which fixes a bug with optparse. Added freeze threshold logic. Fixed nohup async calls which left out psql connection parms.
# Mar.  18, 2020.  V2.7: Add option to run inquiry queries to validate work to be done. Fixed bug where case-sensitive table names caused errors. Added signal interrupts.
# Sept. 13, 2020.  V2.8: Fixed bug in dryrun mode where inquiry section was not indented correctly causing an exception.
# Nov.  12, 2020.  V2.9: Fixed schema sql bug in small table analyze section.  Did not escape double-quotes.
#                        Adjusted format to allow for longer table names.  
#                        Added parameter, --ignoreparts and sql logic supporting it.
#                        Ignore pg_catalog and information_schema tables not in ('pg_catalog', 'pg_toast', 'information_schema')
# Nov.  17, 2020.  V2.9: Deal with missing relispartition column from queries since it does not exist prior to version 10
# Dec.  04, 2020.  V2.9: Undo logic for #6 Catchall query for analyze that have not happened for over 30 days, not 14. Also, fixed dup tables again. and for very old vacuums
# Dec.  10, 2020.  V3.0: Rewrite for compatibiliity with python 2 and python3 and all versions of psycopg2.
#                        Changed "<>" to <!=>   
#                        Changed print "whatever" to print ("whatever") 
#                        Removed Psycopg2 exception handling and replaced with general one.
# Jan.  03, 2021   V3.1: Show aysync jobs summary. Added new parm, async, to trigger async jobs even if thresholds are not met (coded, but not implemented yet). 
#                        Added application name to async jobs.  Lowered async threshold for max rows (threshold_async_rows) and max sync size (threshold_max_sync)
# Jan.  12, 2021   V3.2: Fix nohup missing double quotes!
# Jan.  26, 2021   V3.3: Add another parameter to do vacuums longer than x days, just like we do now for analyzes.
#                        Also prevent multiple instances of this program to run against the same PG database.
#
# Notes:
#   1. Do not run this program multiple times since it may try to vacuum or analyze the same table again
#      since the logic is based on timestamps that are not updated until AFTER the action is done.
#   2. Change top shebang line to account for python, python or python2  
#   3. By default, system schemas are not included when schemaname is ommitted from command line.
#
# call example with all parameters:
# pg_vacuum.py -H localhost -d testing -p 5432 -u postgres --maxsize 400000000000 --maxdays 1 --mindeadtups 1000 --schema public --inquiry --dryrun
# pg_vacuum.py -H localhost -d testing -p 5432 -u postgres -s 400000000000 -y 1 -t 1000 -m public --freeze
#
# crontab example that runs every morning at 3am local time and will vacuum if more than 5000 dead tuples and/or its been over 5 days since the last vacuum/analyze.
# SHELL=/bin/sh
# PATH=<your binary paths as postgres user>
# 00 03 * * * /home/postgres/mjv/pg_vacuumb.py -H localhost -d <dbname> -u postgres -p 5432 -y 5 -t 5000 --dryrun >/home/postgres/mjv/optimize_db_`/bin/date +'\%Y-\%m-\%d-\%H.\%M.\%S'`.log 2>&1
#
##################################################################################################
import sys, os, threading, argparse, time, datetime, signal
from optparse import OptionParser
import psycopg2
import subprocess

version = '3.3  Jan. 26, 2021'
OK = 0
BAD = -1

fmtrows  = '%11d'
fmtbytes = '%13d'

1,000,000,000,000
#threshold for freezing tables: 25 million rows from wraparound
threshold_freeze = 25000000

# minimum dead tuples
threshold_dead_tups = 1000

# 100 million row threshold
threshold_async_rows = 100000000

# 400 GB threshold, above this table actions are deferred
threshold_max_size = 400000000000

# 50 MB minimum threshold
threshold_min_size = 50000000

# 100 GB threshold, above this table actions are done asynchronously
threshold_max_sync = 100000000000

# max async processes
threshold_max_processes = 12

# load threshold, wait for a time if very high
load_threshold = 250

def signal_handler(signal, frame):
     printit('User-interrupted!')
     sys.exit(1)
     
def printit(text):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    txt = now + ' ' + text
    print (txt)
    # don't use v3 way to flush output since we want this to work for v2 and 3
    #print (txt, flush=True)
    sys.stdout.flush()
    return

def execute_cmd(text):
    rc = os.system(text)
    return rc

def get_process_cnt():
    cmd = "ps -ef | grep 'VACUUM VERBOSE\|ANALYZE VERBOSE' | grep -v grep | wc -l"
    result = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE).stdout.read()
    result = int(result.decode('ascii'))
    return result

def highload():
    cmd = "uptime | sed 's/.*load average: //' | awk -F\, '{print $1}'"
    result = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE).stdout.read()
    min1  = str(result.decode())
    loadn = int(float(min1))
    cmd = "cat /proc/cpuinfo | grep processor | wc -l"
    result = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE).stdout.read()
    cpus  = str(result.decode())
    cpusn = int(float(cpus))
    load = (loadn / cpusn) * 100
    if load < load_threshold:
        return False
    else:
        printit ("High Load: loadn=%d cpusn=%d load=%d" % (loadn, cpusn, load))
        return True

'''
func requires psutil package
def getload():
    import psutil, os, sys
    #cpu_percent = float(psutil.cpu_percent())
    total_cpu=float(psutil.cpu_count())
    load_average=os.getloadavg()
    load_now=float(load_average[0])
    print ("load average=%s" % (load_average) 
    # print ("total cpu=%d" % total_cpu)
    # print ("load now=%d" % load_now)

    if (load_now > total_cpu):
        cpu_usage = ("CPU is :" + str(cpu_percent))
        Num_CPU = ("Number of CPU's : " + str(total_cpu))
        Load_Average = ("Load Average is : " + str(load_average))
        load_out = open("/tmp/load_average.out", "w")
        load_out.write(Num_CPU + "\n" + Load_Average);
        load_out.close();
        os.system("mail -s \"Load Average is Higher than Number of CPU's abc@abc.com 123@123.com < /tmp/load_average.out")
'''

def get_query_cnt(conn, cur):
    sql = "select count(*) from pg_stat_activity where state = 'active' and application_name = 'pg_vacuum' and query like 'VACUUM FREEZE VERBOSE%' OR query like 'VACUUM ANALYZE VERBOSE%' OR query like 'VACUUM VERBOSE%' OR query like 'ANALYZE VERBOSE%'"
    cur.execute(sql)
    rows = cur.fetchone()
    return int(rows[0])

def get_vacuums_in_progress(conn, cur):
    sql = "SELECT 'tables', array_agg(relid::regclass) from pg_stat_progress_vacuum group by 1"
    cur.execute(sql)
    rows = cur.fetchone()
    return rows[1]

def skip_table (atable, tablist):
    #print ("table=%s  tablist=%s" % (atable, tablist))
    if atable in tablist:
        #print ("table is in the list")
        return True
    else:
        #print ("table is not in the list")
        return False

def wait_for_processes(conn,cur):
    cnt = 0
    while True:
        rc = get_query_cnt(conn, cur)
        cnt = cnt + 1
        if cnt > 20:
            printit ("NOTE: Program ending, but vacuums/analyzes(%d) still in progress." % (rc))
            break
        if rc > 0:
            tables = get_vacuums_in_progress(conn, cur)
            printit ("NOTE: vacuums still running: %d (%s) Waiting another 5 minutes before exiting..." % (rc, tables))
            time.sleep(300)
        else:
            break
    return


####################
# MAIN ENTRY POINT #
####################

# Register the signal handler for CNTRL-C logic
signal.signal(signal.SIGINT, signal_handler)
signal.siginterrupt(signal.SIGINT, False)        
# test interrupt
#while True:
#    print('Waiting...')
#    time.sleep(5)

# Delay if high load encountered, give up after 30 minutes.
cnt = 0
while True:
    if highload():
        printit("Deferring program start for another 5 minutes while high load encountered.")
        cnt = cnt + 1
        time.sleep(300)
        if cnt > 5:
            printit("Aborting program due to high load.")
            sys.exit(1)
    else:
        break

total_freezes = 0
total_vacuums_analyzes = 0
total_vacuums  = 0
total_analyzes = 0
tables_skipped = 0
partitioned_tables_skipped = 0
asyncjobs = 0
tablist = []

# Setup up the argument parser
# parser = OptionParser("PostgreSQL Vacumming Tool", add_help_option=False)
'''
# parser = OptionParser("PostgreSQL Vacumming Tool", add_help_option=True)
parser.add_option("-r", "--dryrun", dest="dryrun",   help="dry run", default=False, action="store_true", metavar="DRYRUN")
parser.add_option("-f", "--freeze", dest="freeze",   help="vacuum freeze directive", default=False, action="store_true", metavar="FREEZE")
parser.add_option("-H", "--host",   dest="hostname", help="host name",      type=str, default="localhost",metavar="HOSTNAME")
parser.add_option("-d", "--dbname", dest="dbname",   help="database name",  type=str, default="",metavar="DBNAME")
parser.add_option("-U", "--dbuser", dest="dbuser",   help="database user",  type=str, default="postgres",metavar="DBUSER")
parser.add_option("-m", "--schema",dest="schema",    help="schema",         type=str, default="",metavar="SCHEMA")
parser.add_option("-p", "--dbport", dest="dbport",   help="database port",  type=int, default="5432",metavar="DBPORT")
parser.add_option("-s", "--maxsize",dest="maxsize",  help="max table size", type=int, default=-1,metavar="MAXSIZE")
parser.add_option("-y", "--maxdays",dest="maxdays",  help="max days",       type=int, default=5,metavar="MAXDAYS")
iparser.add_option("-t", "--mindeadtups",dest="mindeadtups",  help="min dead tups", type=int, default=10000,metavar="MINDEADTUPS")
parser.add_option("-q", "--inquiry", dest="inquiry", help="inquiry queries requested", type=str default="", metavar="INQUIRY")
(options,args) = parser.parse_args()
'''
parser = argparse.ArgumentParser("PostgreSQL Vacumming Tool", add_help=True)
parser.add_argument("-r", "--dryrun", dest="dryrun",           help="dry run",        default=False, action="store_true")
parser.add_argument("-f", "--freeze", dest="freeze",           help="vacuum freeze directive", default=False, action="store_true")
parser.add_argument("-H", "--host",   dest="hostname",         help="host name",      type=str, default="localhost",metavar="HOSTNAME")
parser.add_argument("-d", "--dbname", dest="dbname",           help="database name",  type=str, default="",metavar="DBNAME")
parser.add_argument("-U", "--dbuser", dest="dbuser",           help="database user",  type=str, default="postgres",metavar="DBUSER")
parser.add_argument("-m", "--schema",dest="schema",            help="schema",         type=str, default="",metavar="SCHEMA")
parser.add_argument("-p", "--dbport", dest="dbport",           help="database port",  type=int, default="5432",metavar="DBPORT")
parser.add_argument("-s", "--maxsize",dest="maxsize",          help="max table size", type=int, default=-1,metavar="MAXSIZE")
parser.add_argument("-y", "--analyzemaxdays" ,dest="maxdaysA", help="Analyze max days", type=int, default=60,metavar="ANALYZEMAXDAYS")
parser.add_argument("-x", "--vacuummaxdays"  ,dest="maxdaysV", help="Vacuum  max days", type=int, default=30,metavar="VACUUMMAXDAYS")
parser.add_argument("-z", "--pctfreeze",dest="pctfreeze",      help="max pct until wraparoun", type=int, default=90, metavar="PCTFREEZE")
parser.add_argument("-t", "--mindeadtups",dest="mindeadtups",  help="min dead tups",  type=int, default=10000,metavar="MINDEADTUPS")
parser.add_argument("-q", "--inquiry", dest="inquiry",         help="inquiry requested", choices=['all', 'found', ''], type=str, default="", metavar="INQUIRY")
parser.add_argument("-i", "--ignoreparts", dest="ignoreparts", help="ignore partition tables", default=False, action="store_true")
parser.add_argument("-a", "--async", dest="async",             help="run async jobs", default=False, action="store_true")

args = parser.parse_args()

dryrun      = False
freeze      = False
ignoreparts = False
async       = False
if args.dryrun:
    dryrun = True
if args.freeze:
    freeze = True;
if args.ignoreparts:
    ignoreparts = True;    
if args.async:
    async = True;        
if args.dbname == "":
    printit("DB Name must be provided.")
    sys.exit(1)

# if args.maxsize != -1:
if args.maxsize != -1:
    # use user-provided max instead of program default (300 GB)
        threshold_max_size = args.maxsize

dbname   = args.dbname
hostname = args.hostname
dbport   = args.dbport
dbuser   = args.dbuser
schema   = args.schema

threshold_max_days_analyze = args.maxdaysA
threshold_max_days_vacuum  = args.maxdaysV

min_dead_tups = args.mindeadtups
pctfreeze = args.pctfreeze

if pctfreeze > 99 or pctfreeze < 10:
    printit("pctfreeze must range between 10 and 99.")
    sys.exit(1)

if min_dead_tups > 100:
    threshold_dead_tups = min_dead_tups

inquiry = args.inquiry
if inquiry == 'all' or inquiry == 'found' or inquiry == '':
    pass
else:
    printit("Inquiry parameter invalid.  Must be 'all' or 'found'")
    sys.exit(1)

printit ("version: *** %s ***  Parms: dryrun(%r) inquiry(%s) freeze(%r) ignoreparts(%r) host:%s dbname=%s schema=%s dbuser=%s dbport=%d  Analyze max days:%d  Vacuumm max days:%d  min dead tups: %d  max table size: %d  pct freeze: %d" \
        % (version, dryrun, inquiry, freeze, ignoreparts, hostname, dbname, schema, dbuser, dbport, threshold_max_days_analyze, threshold_max_days_vacuum, threshold_dead_tups, threshold_max_size, pctfreeze))

# printit ("Exiting program prematurely for debug purposes.")
# sys.exit(0)

# Connect
# conn = psycopg2.connect("dbname=testing user=postgres host=locahost password=postgrespass")
# connstr = "dbname=%s port=%d user=%s host=%s password=postgrespass" % (dbname, dbport, dbuser, hostname )
connstr = "dbname=%s port=%d user=%s host=%s application_name=%s" % (dbname, dbport, dbuser, hostname, 'pg_vacuum' )
try:
    conn = psycopg2.connect(connstr)
except Exception as error:
    printit("Database Connection Error: %s *** %s" % (type(error), error))
    sys.exit (1)
        
printit("connected to database successfully.")

# to run vacuum through the psycopg2 driver, the isolation level must be changed.
old_isolation_level = conn.isolation_level
conn.set_isolation_level(0)

# Open a cursor to perform database operation
cur = conn.cursor()

# Abort if a pg_vacuum instance is already running against this database.
sql = "select count(*) from pg_stat_activity where application_name = 'pg_vacuum'"
try:
    cur.execute(sql)
except Exception as error:
    printit ("Unable to check for multiple pg_vacuum instances: %s" % (e))
    conn.close()
    sys.exit (1)

rows = cur.fetchone()
instances = int(rows[0])
if instances > 1:
    printit ("pg_vacuum instance(s) already running (%d). This instance will close now." % (instances))
    conn.close()
    sys.exit (1)

# get version since 9.6 and earlier do not have a relispartition column in pg_class table
# it will look something like this or this: 90618 or 100013, so anything greater than 100000 would be 10+ versions.
# also it looks like we are not currently using the relispartition column anyway, so just remove it for now
# substitute this case statement for c.relispartition wherever it is used.
# CASE WHEN (SELECT c.relname AS child FROM pg_inherits i JOIN pg_class p ON (i.inhparent=p.oid) where i.inhrelid=c.oid) IS NULL THEN 'False' ELSE 'True' END as partitioned 

sql = "show server_version_num"
try:
    cur.execute(sql)
except Exception as error:
    printit ("Unable to get server version number: %s" % (e))
    conn.close()
    sys.exit (1)

rows = cur.fetchone()
version = int(rows[0])

active_processes = 0

#################################
# 1. Freeze Tables              #
#################################
'''
-- all
SELECT n.nspname || '.' || c.relname as table, c.reltuples::bigint as rows, age(c.relfrozenxid) as xid_age, CAST(current_setting('autovacuum_freeze_max_age') AS bigint) as freeze_max_age,
CAST(current_setting('autovacuum_freeze_max_age') AS bigint) - age(c.relfrozenxid)::bigint as howclose,
pg_size_pretty(pg_total_relation_size(c.oid)) as table_size_pretty, pg_total_relation_size(c.oid) as table_size, c.relispartition FROM pg_class c, pg_namespace n WHERE n.nspname not in  ('pg_catalog', 'pg_toast',  'information_schema') and n.oid = c.relnamespace and c.relkind not in ('i','v','S','c') AND CAST(current_setting('autovacuum_freeze_max_age') AS bigint) - age(c.relfrozenxid)::bigint > 1::bigint and  CAST(current_setting('autovacuum_freeze_max_age') AS bigint) - age(c.relfrozenxid)::bigint < 25000000 ORDER BY age(c.relfrozenxid) DESC LIMIT 60;

-- public schema
SELECT n.nspname || '.' || c.relname as table, c.reltuples::bigint as rows, age(c.relfrozenxid) as xid_age, CAST(current_setting('autovacuum_freeze_max_age') AS bigint) as freeze_max_age,
CAST(current_setting('autovacuum_freeze_max_age') AS bigint) - age(c.relfrozenxid)::bigint as howclose,
pg_size_pretty(pg_total_relation_size(c.oid)) as table_size_pretty, pg_total_relation_size(c.oid) as table_size, c.relispartition FROM pg_class c, pg_namespace n WHERE n.nspname = 'public' and n.oid = c.relnamespace and c.relkind not in ('i','v','S','c') AND CAST(current_setting('autovacuum_freeze_max_age') AS bigint) - age(c.relfrozenxid)::bigint > 1::bigint and  CAST(current_setting('autovacuum_freeze_max_age') AS bigint) - age(c.relfrozenxid)::bigint < 25000000 ORDER BY age(c.relfrozenxid) DESC LIMIT 60;
'''
if version > 100000:
    if schema == "":
       sql = "SELECT n.nspname || '.\"' || c.relname || '\"' as table, c.reltuples::bigint as rows, age(c.relfrozenxid) as xid_age, CAST(current_setting('autovacuum_freeze_max_age') AS bigint) as freeze_max_age, " \
      "CAST(current_setting('autovacuum_freeze_max_age') AS bigint) - age(c.relfrozenxid)::bigint as howclose, pg_size_pretty(pg_total_relation_size(c.oid)) as table_size_pretty,  " \
      "pg_total_relation_size(c.oid) as table_size, c.relispartition FROM pg_class c, pg_namespace n WHERE n.nspname not in ('pg_catalog', 'pg_toast', 'information_schema') and n.oid = c.relnamespace and c.relkind not in ('i','v','S','c') AND CAST(current_setting('autovacuum_freeze_max_age') AS bigint) - " \
      "age(c.relfrozenxid)::bigint > 1::bigint and  CAST(current_setting('autovacuum_freeze_max_age') AS bigint) - age(c.relfrozenxid)::bigint < %d ORDER BY age(c.relfrozenxid) DESC LIMIT 60" % (threshold_freeze)
    else:
       sql = "SELECT n.nspname || '.\"' || c.relname || '\"' as table, c.reltuples::bigint as rows, age(c.relfrozenxid) as xid_age, CAST(current_setting('autovacuum_freeze_max_age') AS bigint) as freeze_max_age, " \
      "CAST(current_setting('autovacuum_freeze_max_age') AS bigint) - age(c.relfrozenxid)::bigint as howclose, pg_size_pretty(pg_total_relation_size(c.oid)) as table_size_pretty,  " \
      "pg_total_relation_size(c.oid) as table_size, c.relispartition FROM pg_class c, pg_namespace n WHERE n.nspname = '%s' and n.oid = c.relnamespace and c.relkind not in ('i','v','S','c') AND CAST(current_setting('autovacuum_freeze_max_age') " \
      "AS bigint) - age(c.relfrozenxid)::bigint > 1::bigint and  CAST(current_setting('autovacuum_freeze_max_age') AS bigint) - age(c.relfrozenxid)::bigint < %d ORDER BY age(c.relfrozenxid) DESC LIMIT 60" % (schema, threshold_freeze)
else:
# put version 9.x compatible query here
# CASE WHEN (SELECT c.relname AS child FROM pg_inherits i JOIN pg_class p ON (i.inhparent=p.oid) where i.inhrelid=c.oid) IS NULL THEN 'False' ELSE 'True' END as partitioned 
    if schema == "":
       sql = "SELECT n.nspname || '.\"' || c.relname || '\"' as table, c.reltuples::bigint as rows, age(c.relfrozenxid) as xid_age, CAST(current_setting('autovacuum_freeze_max_age') AS bigint) as freeze_max_age, " \
      "CAST(current_setting('autovacuum_freeze_max_age') AS bigint) - age(c.relfrozenxid)::bigint as howclose, pg_size_pretty(pg_total_relation_size(c.oid)) as table_size_pretty,  " \
      "pg_total_relation_size(c.oid) as table_size, CASE WHEN (SELECT c.relname AS child FROM pg_inherits i JOIN pg_class p ON (i.inhparent=p.oid) where i.inhrelid=c.oid) IS NULL THEN 'False'::boolean ELSE 'True'::boolean END as partitioned FROM pg_class c, pg_namespace n WHERE n.nspname not in ('pg_catalog', 'pg_toast', 'information_schema') and n.oid = c.relnamespace and c.relkind not in ('i','v','S','c') AND CAST(current_setting('autovacuum_freeze_max_age') AS bigint) - " \
      "age(c.relfrozenxid)::bigint > 1::bigint and  CAST(current_setting('autovacuum_freeze_max_age') AS bigint) - age(c.relfrozenxid)::bigint < %d ORDER BY age(c.relfrozenxid) DESC LIMIT 60" % (threshold_freeze)
    else:
       sql = "SELECT n.nspname || '.\"' || c.relname || '\"' as table, c.reltuples::bigint as rows, age(c.relfrozenxid) as xid_age, CAST(current_setting('autovacuum_freeze_max_age') AS bigint) as freeze_max_age, " \
      "CAST(current_setting('autovacuum_freeze_max_age') AS bigint) - age(c.relfrozenxid)::bigint as howclose, pg_size_pretty(pg_total_relation_size(c.oid)) as table_size_pretty,  " \
      "pg_total_relation_size(c.oid) as table_size, CASE WHEN (SELECT c.relname AS child FROM pg_inherits i JOIN pg_class p ON (i.inhparent=p.oid) where i.inhrelid=c.oid) IS NULL THEN 'False'::boolean ELSE 'True'::boolean END as partitioned FROM pg_class c, pg_namespace n WHERE n.nspname = '%s' and n.oid = c.relnamespace and c.relkind not in ('i','v','S','c') AND CAST(current_setting('autovacuum_freeze_max_age') " \
      "AS bigint) - age(c.relfrozenxid)::bigint > 1::bigint and  CAST(current_setting('autovacuum_freeze_max_age') AS bigint) - age(c.relfrozenxid)::bigint < %d ORDER BY age(c.relfrozenxid) DESC LIMIT 60" % (schema, threshold_freeze)
      
try:
     cur.execute(sql)
except Exception as error:
    printit("Freeze Tables Exception: %s *** %s" % (type(error), error))
    conn.close()
    sys.exit (1)     

rows = cur.fetchall()
if len(rows) == 0:
    printit ("No FREEZEs need to be done.")
else:
    printit ("VACUUM FREEZEs to be evaluated=%d.  Includes deferred ones too." % len(rows) )

cnt = 0
partcnt = 0
action_name = 'VACUUM FREEZE'
if not dryrun and len(rows) > 0 and not freeze:
    printit ('Bypassing VACUUM FREEZE action for %d tables. Otherwise specify "--freeze" to do them.' % len(rows))

for row in rows:
    if not freeze and not dryrun:
        continue
    if active_processes > threshold_max_processes:
        # see how many are currently running and update the active processes again
        # rc = get_process_cnt()
        rc = get_query_cnt(conn, cur)
        if rc > threshold_max_processes:
            printit ("Current process cnt(%d) is still higher than threshold (%d). Sleeping for 5 minutes..." % (rc, threshold_max_processes))
            time.sleep(300)
        else:
            printit ("Current process cnt(%d) is less than threshold (%d).  Processing will continue..." % (rc, threshold_max_processes))
        active_processes = rc

    cnt = cnt + 1
    table    = row[0]
    tups     = row[1]
    xidage   = row[2]
    maxage   = row[3]
    howclose = row[4]
    sizep    = row[5]
    size     = row[6]
    part     = row[7]

    if part and ignoreparts:
        partcnt = partcnt + 1    
        #print ("ignoring partitioned table: %s" % table)
        continue

    # also bypass tables that are less than 15% of max age
    pctmax = float(xidage) / float(maxage)
    # print("maxage=%10f  xidage=%10f  pctmax=%4f  pctfreeze=%4f" % (maxage, xidage, pctmax, pctfreeze))
    if (100 * pctmax) < float(pctfreeze):
       printit ("Async %13s  %03d %-57s rows: %11d  size: %10s :%13d freeze_max: %10d  xid_age: %10d  how close: %10d  pct: %d: Defer" \
               % (action_name, cnt, table, tups, sizep, size, maxage, xidage, howclose, 100 * pctmax))
       tables_skipped = tables_skipped + 1
       continue

       if size > threshold_max_size:
          # defer action
          printit ("Async %13s  %03d %-57s rows: %11d size: %10s :%13d freeze_max: %10d  xid_age: %10d  how close: %10d NOTICE: Skipping large table.  Do manually." \
                  % (action_name, cnt, table, tups, sizep, size, maxage, xidage, howclose))
          tables_skipped = tables_skipped + 1
          continue
    elif tups > threshold_async_rows or size > threshold_max_sync:
    #elif (tups > threshold_async_rows or size > threshold_max_sync) and async:
        if dryrun:
            if active_processes > threshold_max_processes:
                printit ("%13s: Max processes reached. Skipping further Async activity for very large table, %s.  Size=%s.  Do manually." % (action_name, table, sizep))
                tables_skipped = tables_skipped + 1
                continue
            printit ("Async %13s: %03d %-57s rows: %11d size: %10s :%13d freeze_max: %10d  xid_age: %10d  how close: %10d  pct: %d" % (action_name, cnt, table, tups, sizep, size, maxage, xidage, howclose, (100 * pctmax)))
            total_freezes = total_freezes + 1
            tablist.append(table)
            active_processes = active_processes + 1
        else:
            if active_processes > threshold_max_processes:
                printit ("%13s: Max processes reached. Skipping further Async activity for very large table, %s.  Size=%s.  Do manually." % (action_name, table, sizep))
                tables_skipped = tables_skipped + 1
                continue
            # v3.1 change to include application name
            connparms = "dbname=%s port=%d user=%s host=%s application_name=%s" % (dbname, dbport, dbuser, hostname, 'pg_vacuum' )
            # cmd = 'nohup psql -h %s -d %s -p %s -U %s -c "VACUUM (FREEZE, VERBOSE) %s" 2>/dev/null &' % (hostname, dbname, dbport, dbuser, table)
            cmd = 'nohup psql -d "%s" -c "VACUUM (FREEZE, VERBOSE) %s" 2>/dev/null &' % (connparms, table)
            print(cmd)
            time.sleep(0.5)
            asyncjobs = asyncjobs + 1
            printit ("Async %13s: %03d %-57s rows: %11d size: %10s :%13d freeze_max: %10d  xid_age: %10d  how close: %10d  pct: %d" % (action_name, cnt, table, tups, sizep, size, maxage, xidage, howclose, (100 * pctmax)))
            rc = execute_cmd(cmd)
            total_freezes = total_freezes + 1
            tablist.append(table)
            active_processes = active_processes + 1

    else:
        if dryrun:
            printit ("Sync  %13s: %03d %-57s rows: %11d size: %10s :%13d freeze_max: %10d  xid_age: %10d  how close: %10d  pct: %d" % (action_name, cnt, table, tups, sizep, size, maxage, xidage, howclose, (100 * pctmax)))
            total_freezes = total_freezes + 1
        else:
            printit ("Sync  %13s: %03d %-57s rows: %11d size: %10s :%13d freeze_max: %10d  xid_age: %10d  how close: %10d  pct: %d" % (action_name, cnt, table, tups, sizep, size, maxage, xidage, howclose, (100 * pctmax)))
            sql = "VACUUM (FREEZE, VERBOSE) %s" % table
            time.sleep(0.5)
            try:
                cur.execute(sql)
            except Exception as error:
                printit("Exception: %s *** %s" % (type(error), error))
                continue
            total_freezes = total_freezes + 1
            tablist.append(table)            

if ignoreparts:
    printit ("Partitioned table vacuum freezes bypassed=%d" % partcnt)
    partitioned_tables_skipped = partitioned_tables_skipped + partcnt

# if action is freeze just exit at this point gracefully
if freeze:
   conn.close()
   printit ("End of Freeze action.  Closing the connection and exiting normally.")
   sys.exit(0)


#################################
# 2. Vacuum and Analyze query
#    older than threshold date OR (dead tups greater than threshold and table size greater than threshold min size)
#################################

# V2.3: Fixed query date problem
#V 2.4 Fix, needed to add logic to check for null timestamps!
'''
-- all
-- > v10 or higher
SELECT u.schemaname || '.' || u.relname as table, pg_size_pretty(pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname))::bigint) as size_pretty, pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname)) as size, c.reltuples::bigint AS n_tup, u.n_live_tup::bigint as n_live_tup, u.n_dead_tup::bigint AS dead_tup, c.relispartition, to_char(u.last_vacuum, 'YYYY-MM-DD HH24:MI') as last_vacuum, to_char(u.last_autovacuum, 'YYYY-MM-DD HH24:MI') as last_autovacuum, to_char(u.last_analyze,'YYYY-MM-DD HH24:MI') as last_analyze, to_char(u.last_autoanalyze,'YYYY-MM-DD HH24:MI') as last_autoanalyze
FROM pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and n.nspname not in ('pg_catalog', 'pg_toast', 'information_schema') and t.schemaname = n.nspname and t.tablename = c.relname and c.relname = u.relname and u.schemaname = n.nspname and n.nspname not in ('information_schema','pg_catalog', 'pg_toast') AND
(pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) > 50000000 AND u.n_dead_tup > 1000 AND (now()::date - GREATEST(last_analyze, last_autoanalyze)::date > 15 AND now()::date - GREATEST(last_vacuum, last_autovacuum)::date > 15) OR ((last_analyze IS NULL AND last_autoanalyze IS NULL) OR (last_vacuum IS NULL AND last_autovacuum IS NULL))) order by 4,1;

-- v9.6 or lower
SELECT u.schemaname || '.\"' || u.relname || '\"' as table, pg_size_pretty(pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname))::bigint) as size_pretty, 
pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname)) as size, c.reltuples::bigint AS n_tup, u.n_live_tup::bigint as n_live_tup,  
u.n_dead_tup::bigint AS dead_tup, CASE WHEN (SELECT c.relname AS child FROM pg_inherits i JOIN pg_class p ON (i.inhparent=p.oid) where i.inhrelid=c.oid) IS NULL THEN 'False'::boolean ELSE 'True'::boolean END as partitioned, to_char(u.last_vacuum, 'YYYY-MM-DD HH24:MI') as last_vacuum, to_char(u.last_autovacuum, 'YYYY-MM-DD HH24:MI') as last_autovacuum,  
to_char(u.last_analyze,'YYYY-MM-DD HH24:MI') as last_analyze, to_char(u.last_autoanalyze,'YYYY-MM-DD HH24:MI') as last_autoanalyze  
FROM pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and t.schemaname = n.nspname and n.nspname not in ('pg_catalog', 'pg_toast', 'information_schema') and t.tablename = c.relname and c.relname = u.relname and u.schemaname = n.nspname  
and n.nspname not in ('information_schema','pg_catalog', 'pg_toast') AND (u.n_dead_tup > 1000 AND   pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) > 50000000 AND 
(now()::date - GREATEST(last_analyze, last_autoanalyze)::date > 15 AND now()::date - GREATEST(last_vacuum, last_autovacuum)::date > 15) OR ((last_analyze IS NULL AND last_autoanalyze IS NULL) OR 
(last_vacuum IS NULL AND last_autovacuum IS NULL))) order by 4,1;

-- public schema only
SELECT u.schemaname || '.' || u.relname as table, pg_size_pretty(pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname))::bigint) as size_pretty, pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname)) as size, c.reltuples::bigint AS n_tup, u.n_live_tup::bigint as n_live_tup, u.n_dead_tup::bigint AS dead_tup, c.relispartition, to_char(u.last_vacuum, 'YYYY-MM-DD HH24:MI') as last_vacuum, to_char(u.last_autovacuum, 'YYYY-MM-DD HH24:MI') as last_autovacuum, to_char(u.last_analyze,'YYYY-MM-DD HH24:MI') as last_analyze, to_char(u.last_autoanalyze,'YYYY-MM-DD HH24:MI') as last_autoanalyze
FROM pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and and n.nspname = 'public' and t.schemaname = n.nspname and t.tablename = c.relname and c.relname = u.relname and u.schemaname = n.nspname AND
(pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) > 50000000 AND u.n_dead_tup > 1000 AND (now()::date - GREATEST(last_analyze, last_autoanalyze)::date > 5 AND now()::date - GREATEST(last_vacuum, last_autovacuum)::date > 5) OR ((last_analyze IS NULL AND last_autoanalyze IS NULL) OR (last_vacuum IS NULL AND last_autovacuum IS NULL))) order by 4,1;

'''

if version > 100000:
    if schema == "":
       sql = "SELECT u.schemaname || '.\"' || u.relname || '\"' as table, pg_size_pretty(pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname))::bigint) as size_pretty,  " \
      "pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname)) as size, c.reltuples::bigint AS n_tup, u.n_live_tup::bigint as n_live_tup,  " \
      "u.n_dead_tup::bigint AS dead_tup, c.relispartition, to_char(u.last_vacuum, 'YYYY-MM-DD HH24:MI') as last_vacuum, to_char(u.last_autovacuum, 'YYYY-MM-DD HH24:MI') as last_autovacuum,  " \
      "to_char(u.last_analyze,'YYYY-MM-DD HH24:MI') as last_analyze, to_char(u.last_autoanalyze,'YYYY-MM-DD HH24:MI') as last_autoanalyze  " \
      "FROM pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and t.schemaname = n.nspname and n.nspname not in ('pg_catalog', 'pg_toast', 'information_schema') " \
      "and t.tablename = c.relname and c.relname = u.relname and u.schemaname = n.nspname  " \
      "and n.nspname not in ('information_schema','pg_catalog', 'pg_toast') AND (u.n_dead_tup > %d AND   pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) > %d AND " \
      "(now()::date - GREATEST(last_analyze, last_autoanalyze)::date > %d AND now()::date - GREATEST(last_vacuum, last_autovacuum)::date > %d) OR ((last_analyze IS NULL AND last_autoanalyze IS NULL) OR " \
      "(last_vacuum IS NULL AND last_autovacuum IS NULL))) order by 4,1" % (threshold_dead_tups, threshold_min_size, threshold_max_days_analyze, threshold_max_days_analyze)
    else:
       sql = "SELECT u.schemaname || '.\"' || u.relname || '\"' as table, pg_size_pretty(pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname))::bigint) as size_pretty,  " \
      "pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname)) as size, c.reltuples::bigint AS n_tup, u.n_live_tup::bigint as n_live_tup,  " \
      "u.n_dead_tup::bigint AS dead_tup, c.relispartition, to_char(u.last_vacuum, 'YYYY-MM-DD HH24:MI') as last_vacuum, to_char(u.last_autovacuum, 'YYYY-MM-DD HH24:MI') as last_autovacuum,  " \
      "to_char(u.last_analyze,'YYYY-MM-DD HH24:MI') as last_analyze, to_char(u.last_autoanalyze,'YYYY-MM-DD HH24:MI') as last_autoanalyze  " \
      "FROM pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and n.nspname = '%s' and t.schemaname = n.nspname and t.tablename = c.relname and " \
      "c.relname = u.relname and u.schemaname = n.nspname  " \
      "and (u.n_dead_tup > %d AND   pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) > %d AND " \
      "(now()::date - GREATEST(last_analyze, last_autoanalyze)::date > %d AND now()::date - GREATEST(last_vacuum, last_autovacuum)::date > %d) OR ((last_analyze IS NULL AND last_autoanalyze IS NULL) OR " \
      "(last_vacuum IS NULL AND last_autovacuum IS NULL))) order by 4,1" % (schema, threshold_dead_tups, threshold_min_size, threshold_max_days_analyze, threshold_max_days_analyze)
else:
# put version 9.x compatible query here
#CASE WHEN (SELECT c.relname AS child FROM pg_inherits i JOIN pg_class p ON (i.inhparent=p.oid) where i.inhrelid=c.oid) IS NULL THEN 'False' ELSE 'True' END as partitioned
    if schema == "":
       sql = "SELECT u.schemaname || '.\"' || u.relname || '\"' as table, pg_size_pretty(pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname))::bigint) as size_pretty,  " \
      "pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname)) as size, c.reltuples::bigint AS n_tup, u.n_live_tup::bigint as n_live_tup,  " \
      "u.n_dead_tup::bigint AS dead_tup, CASE WHEN (SELECT c.relname AS child FROM pg_inherits i JOIN pg_class p ON (i.inhparent=p.oid) where i.inhrelid=c.oid) IS NULL THEN 'False'::boolean ELSE 'True'::boolean END as partitioned, to_char(u.last_vacuum, 'YYYY-MM-DD HH24:MI') as last_vacuum, to_char(u.last_autovacuum, 'YYYY-MM-DD HH24:MI') as last_autovacuum,  " \
      "to_char(u.last_analyze,'YYYY-MM-DD HH24:MI') as last_analyze, to_char(u.last_autoanalyze,'YYYY-MM-DD HH24:MI') as last_autoanalyze  " \
      "FROM pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and t.schemaname = n.nspname and n.nspname not in ('pg_catalog', 'pg_toast', 'information_schema') and t.tablename = c.relname and c.relname = u.relname and u.schemaname = n.nspname  " \
      "and n.nspname not in ('information_schema','pg_catalog', 'pg_toast') AND (u.n_dead_tup > %d AND   pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) > %d AND " \
      "(now()::date - GREATEST(last_analyze, last_autoanalyze)::date > %d AND now()::date - GREATEST(last_vacuum, last_autovacuum)::date > %d) OR ((last_analyze IS NULL AND last_autoanalyze IS NULL) OR " \
      "(last_vacuum IS NULL AND last_autovacuum IS NULL))) order by 4,1" % (threshold_dead_tups, threshold_min_size, threshold_max_days_analyze, threshold_max_days_analyze)
    else:
       sql = "SELECT u.schemaname || '.\"' || u.relname || '\"' as table, pg_size_pretty(pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname))::bigint) as size_pretty,  " \
      "pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname)) as size, c.reltuples::bigint AS n_tup, u.n_live_tup::bigint as n_live_tup,  " \
      "u.n_dead_tup::bigint AS dead_tup, CASE WHEN (SELECT c.relname AS child FROM pg_inherits i JOIN pg_class p ON (i.inhparent=p.oid) where i.inhrelid=c.oid) IS NULL THEN 'False'::boolean ELSE 'True'::boolean END as partitioned, to_char(u.last_vacuum, 'YYYY-MM-DD HH24:MI') as last_vacuum, to_char(u.last_autovacuum, 'YYYY-MM-DD HH24:MI') as last_autovacuum,  " \
      "to_char(u.last_analyze,'YYYY-MM-DD HH24:MI') as last_analyze, to_char(u.last_autoanalyze,'YYYY-MM-DD HH24:MI') as last_autoanalyze  " \
      "FROM pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and n.nspname = '%s' and t.schemaname = n.nspname and t.tablename = c.relname and c.relname = u.relname and u.schemaname = n.nspname  " \
      "and (u.n_dead_tup > %d AND   pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) > %d AND " \
      "(now()::date - GREATEST(last_analyze, last_autoanalyze)::date > %d AND now()::date - GREATEST(last_vacuum, last_autovacuum)::date > %d) OR ((last_analyze IS NULL AND last_autoanalyze IS NULL) OR " \
      "(last_vacuum IS NULL AND last_autovacuum IS NULL))) order by 4,1" % (schema, threshold_dead_tups, threshold_min_size, threshold_max_days_analyze, threshold_max_days_analyze)

      
try:
     cur.execute(sql)
except Exception as error:
    printit("Exception: %s *** %s" % (type(error), error))
    conn.close()
    sys.exit (1)

rows = cur.fetchall()
if len(rows) == 0:
    printit ("No vacuum/analyze pairs to be done.")
else:
    printit ("vacuums/analyzes to be evaluated=%d" % len(rows) )

cnt = 0
partcnt = 0
action_name = 'VAC/ANALYZE'
for row in rows:
    if active_processes > threshold_max_processes:
        # see how many are currently running and update the active processes again
        # rc = get_process_cnt()
        rc = get_query_cnt(conn, cur)
        if rc > threshold_max_processes:
            printit ("Current process cnt(%d) is still higher than threshold (%d). Sleeping for 5 minutes..." % (rc, threshold_max_processes))
            time.sleep(300)
        else:
            printit ("Current process cnt(%d) is less than threshold (%d).  Processing will continue..." % (rc, threshold_max_processes))
        active_processes = rc

    cnt = cnt + 1
    table  = row[0]
    sizep  = row[1]
    size   = row[2]
    tups   = row[3]
    live   = row[4]
    dead   = row[5]
    part = row[6]  
    
    if part and ignoreparts:
        partcnt = partcnt + 1    
        #print ("ignoring partitioned table: %s" % table)
        continue

    # check if we already processed this table
    if skip_table(table, tablist):
        continue

    if size > threshold_max_size:
        # defer action
        if dryrun:
            printit ("Async %13s: %03d %-57s rows: %11d size: %10s :%13d dead: %8d NOTICE: Skipping large table.  Do manually." % (action_name, cnt, table, tups, sizep, size, dead))
            tablist.append(table)
            tables_skipped = tables_skipped + 1
        continue
    elif tups > threshold_async_rows or size > threshold_max_sync:
    #elif (tups > threshold_async_rows or size > threshold_max_sync) and async:
        if dryrun:
            if active_processes > threshold_max_processes:
                printit ("%13s: Max processes reached. Skipping further Async activity for very large table, %s.  Size=%s.  Do manually." % (action_name, table, sizep))
                tables_skipped = tables_skipped + 1
                tablist.append(table)
                continue
            printit ("Async %13s: %03d %-57s rows: %11d size: %10s :%13d dead: %8d" % (action_name, cnt, table, tups, sizep, size, dead))
            total_vacuums_analyzes = total_vacuums_analyzes + 1
            tablist.append(table)
            active_processes = active_processes + 1
        else:
            if active_processes > threshold_max_processes:
                printit ("%13s: Max processes reached. Skipping further Async activity for very large table, %s.  Size=%s.  Do manually." % (action_name, table, sizep))
                tablist.append(table)
                tables_skipped = tables_skipped + 1
                continue
            printit ("Async %13s: %03d %-57s rows: %11d size: %10s :%13d dead: %8d" % (action_name, cnt, table, tups, sizep, size, dead))
            asyncjobs = asyncjobs + 1
            
            # v3.1 change to include application name
            connparms = "dbname=%s port=%d user=%s host=%s application_name=%s" % (dbname, dbport, dbuser, hostname, 'pg_vacuum' )
            # cmd = 'nohup psql -h %s -d %s -p %s -U %s -c "VACUUM (ANALYZE, VERBOSE) %s" 2>/dev/null &' % (hostname, dbname, dbport, dbuser, table)
            cmd = 'nohup psql -d "%s" -c "VACUUM (ANALYZE, VERBOSE) %s" 2>/dev/null &' % (connparms, table)            

            time.sleep(0.5)
            rc = execute_cmd(cmd)
            total_vacuums_analyzes = total_vacuums_analyzes + 1
            tablist.append(table)
            active_processes = active_processes + 1

    else:
        if dryrun:
            printit ("Sync  %13s: %03d %-57s rows: %11d size: %10s :%13d dead: %8d" % (action_name, cnt, table, tups, sizep, size, dead))
            total_vacuums_analyzes = total_vacuums_analyzes + 1
            tablist.append(table)
        else:
            printit ("Sync  %13s: %03d %-57s rows: %11d size: %10s :%13d dead: %8d" % (action_name, cnt, table, tups, sizep, size, dead))
            sql = "VACUUM (ANALYZE, VERBOSE) %s" % table
            time.sleep(0.5)
            try:
                cur.execute(sql)
            except Exception as error:
                printit("Exception: %s *** %s" % (type(error), error))
                continue            
            total_vacuums_analyzes = total_vacuums_analyzes + 1
            tablist.append(table)

if ignoreparts:
    printit ("Partitioned table vacuum/analyzes bypassed=%d" % partcnt)
    partitioned_tables_skipped = partitioned_tables_skipped + partcnt

#################################
# 3. Vacuum determination query #
#################################
'''
-- all
SELECT psut.schemaname || '.' || psut.relname as table, to_char(psut.last_vacuum, 'YYYY-MM-DD HH24:MI') as last_vacuum,  to_char(psut.last_autovacuum, 'YYYY-MM-DD HH24:MI') as last_autovacuum,
pg_class.reltuples::bigint AS n_tup,  psut.n_dead_tup::bigint AS dead_tup, pg_size_pretty(pg_total_relation_size(quote_ident(psut.schemaname) || '.' || quote_ident(psut.relname))::bigint),
pg_total_relation_size(quote_ident(psut.schemaname) || '.' || quote_ident(psut.relname)) as size, c.relispartition, to_char(CAST(current_setting('autovacuum_vacuum_threshold') AS bigint) +
(CAST(current_setting('autovacuum_vacuum_scale_factor') AS numeric) * pg_class.reltuples), '9G999G999G999') AS av_threshold, CASE WHEN CAST(current_setting('autovacuum_vacuum_threshold') AS bigint) +
(CAST(current_setting('autovacuum_vacuum_scale_factor') AS numeric) * pg_class.reltuples) < psut.n_dead_tup THEN '*' ELSE '' END AS expect_av
FROM pg_stat_user_tables psut JOIN pg_class on psut.relid = pg_class.oid  where psut.schemaname not in ('pg_catalog', 'pg_toast', 'information_schema') and psut.n_dead_tup > 10000  OR (last_vacuum is null and last_autovacuum is null) ORDER BY 5 desc, 1;

-- public schema
SELECT psut.schemaname || '.' || psut.relname as table, to_char(psut.last_vacuum, 'YYYY-MM-DD HH24:MI') as last_vacuum,  to_char(psut.last_autovacuum, 'YYYY-MM-DD HH24:MI') as last_autovacuum,
pg_class.reltuples::bigint AS n_tup,  psut.n_dead_tup::bigint AS dead_tup, pg_size_pretty(pg_total_relation_size(quote_ident(psut.schemaname) || '.' || quote_ident(psut.relname))::bigint),
pg_total_relation_size(quote_ident(psut.schemaname) || '.' || quote_ident(psut.relname)) as size, c.relispartition, to_char(CAST(current_setting('autovacuum_vacuum_threshold') AS bigint) +
(CAST(current_setting('autovacuum_vacuum_scale_factor') AS numeric) * pg_class.reltuples), '9G999G999G999') AS av_threshold, CASE WHEN CAST(current_setting('autovacuum_vacuum_threshold') AS bigint) +
(CAST(current_setting('autovacuum_vacuum_scale_factor') AS numeric) * pg_class.reltuples) < psut.n_dead_tup THEN '*' ELSE '' END AS expect_av
FROM pg_stat_user_tables psut JOIN pg_class on psut.relid = pg_class.oid  where psut.schemaname = 'public' and (psut.n_dead_tup > 10000  OR (last_vacuum is null and last_autovacuum is null)) ORDER BY 5 desc, 1;

'''
if version > 100000:
    if schema == "":
       sql = "SELECT psut.schemaname || '.\"' || psut.relname || '\"' as table, to_char(psut.last_vacuum, 'YYYY-MM-DD HH24:MI') as last_vacuum, to_char(psut.last_autovacuum, 'YYYY-MM-DD HH24:MI') as last_autovacuum, " \
      "pg_class.reltuples::bigint AS n_tup,  psut.n_dead_tup::bigint AS dead_tup, pg_size_pretty(pg_total_relation_size(quote_ident(psut.schemaname) || '.' || quote_ident(psut.relname))::bigint), " \
      "pg_total_relation_size(quote_ident(psut.schemaname) || '.' ||quote_ident(psut.relname)) as size, pg_class.relispartition, to_char(CAST(current_setting('autovacuum_vacuum_threshold') AS bigint) + " \
      "(CAST(current_setting('autovacuum_vacuum_scale_factor') AS numeric) * pg_class.reltuples), '9G999G999G999') AS av_threshold, CASE WHEN CAST(current_setting('autovacuum_vacuum_threshold') AS bigint) + " \
      "(CAST(current_setting('autovacuum_vacuum_scale_factor') AS numeric) * pg_class.reltuples) < psut.n_dead_tup THEN '*' ELSE '' END AS expect_av " \
      "FROM pg_stat_user_tables psut JOIN pg_class on psut.relid = pg_class.oid  where psut.schemaname not in ('pg_catalog', 'pg_toast', 'information_schema') and psut.n_dead_tup > %d OR (last_vacuum is null and last_autovacuum is null) ORDER BY 5 desc, 1;" % (threshold_dead_tups)
    else:
       sql = "SELECT psut.schemaname || '.\"' || psut.relname || '\"' as table, to_char(psut.last_vacuum, 'YYYY-MM-DD HH24:MI') as last_vacuum, to_char(psut.last_autovacuum, 'YYYY-MM-DD HH24:MI') as last_autovacuum, " \
      "pg_class.reltuples::bigint AS n_tup,  psut.n_dead_tup::bigint AS dead_tup, pg_size_pretty(pg_total_relation_size(quote_ident(psut.schemaname) || '.' || quote_ident(psut.relname))::bigint), " \
      "pg_total_relation_size(quote_ident(psut.schemaname) || '.' ||quote_ident(psut.relname)) as size, pg_class.relispartition, to_char(CAST(current_setting('autovacuum_vacuum_threshold') AS bigint) + " \
      "(CAST(current_setting('autovacuum_vacuum_scale_factor') AS numeric) * pg_class.reltuples), '9G999G999G999') AS av_threshold, CASE WHEN CAST(current_setting('autovacuum_vacuum_threshold') AS bigint) + " \
      "(CAST(current_setting('autovacuum_vacuum_scale_factor') AS numeric) * pg_class.reltuples) < psut.n_dead_tup THEN '*' ELSE '' END AS expect_av " \
      "FROM pg_stat_user_tables psut JOIN pg_class on psut.relid = pg_class.oid  where psut.schemaname = '%s' and (psut.n_dead_tup > %d OR (last_vacuum is null and last_autovacuum is null)) ORDER BY 5 desc, 1;" % (schema, threshold_dead_tups)
else:
# put version 9.x compatible query here
# CASE WHEN (SELECT c.relname AS child FROM pg_inherits i JOIN pg_class p ON (i.inhparent=p.oid) where i.inhrelid=c.oid) IS NULL THEN 'False' ELSE 'True' END as partitioned 
    if schema == "":
       sql = "SELECT psut.schemaname || '.\"' || psut.relname || '\"' as table, to_char(psut.last_vacuum, 'YYYY-MM-DD HH24:MI') as last_vacuum, to_char(psut.last_autovacuum, 'YYYY-MM-DD HH24:MI') as last_autovacuum, " \
      "pg_class.reltuples::bigint AS n_tup,  psut.n_dead_tup::bigint AS dead_tup, pg_size_pretty(pg_total_relation_size(quote_ident(psut.schemaname) || '.' || quote_ident(psut.relname))::bigint), " \
      "pg_total_relation_size(quote_ident(psut.schemaname) || '.' ||quote_ident(psut.relname)) as size, " \
      "CASE WHEN (SELECT pg_class.relname AS child FROM pg_inherits i JOIN pg_class p ON (i.inhparent=p.oid) where i.inhrelid=pg_class.oid) IS NULL THEN 'False'::boolean ELSE 'True'::boolean END as partitioned, " \
      "to_char(CAST(current_setting('autovacuum_vacuum_threshold') AS bigint) + " \
      "(CAST(current_setting('autovacuum_vacuum_scale_factor') AS numeric) * pg_class.reltuples), '9G999G999G999') AS av_threshold, CASE WHEN CAST(current_setting('autovacuum_vacuum_threshold') AS bigint) + " \
      "(CAST(current_setting('autovacuum_vacuum_scale_factor') AS numeric) * pg_class.reltuples) < psut.n_dead_tup THEN '*' ELSE '' END AS expect_av " \
      "FROM pg_stat_user_tables psut JOIN pg_class on psut.relid = pg_class.oid  where psut.schemaname not in ('pg_catalog', 'pg_toast', 'information_schema') and psut.n_dead_tup > %d OR (last_vacuum is null and last_autovacuum is null) ORDER BY 5 desc, 1;" % (threshold_dead_tups)
    else:
       sql = "SELECT psut.schemaname || '.\"' || psut.relname || '\"' as table, to_char(psut.last_vacuum, 'YYYY-MM-DD HH24:MI') as last_vacuum, to_char(psut.last_autovacuum, 'YYYY-MM-DD HH24:MI') as last_autovacuum, " \
      "pg_class.reltuples::bigint AS n_tup,  psut.n_dead_tup::bigint AS dead_tup, pg_size_pretty(pg_total_relation_size(quote_ident(psut.schemaname) || '.' || quote_ident(psut.relname))::bigint), " \
      "pg_total_relation_size(quote_ident(psut.schemaname) || '.' ||quote_ident(psut.relname)) as size, " \
      "CASE WHEN (SELECT pg_class.relname AS child FROM pg_inherits i JOIN pg_class p ON (i.inhparent=p.oid) where i.inhrelid=pg_class.oid) IS NULL THEN 'False'::boolean ELSE 'True'::boolean END as partitioned, " \
      "to_char(CAST(current_setting('autovacuum_vacuum_threshold') AS bigint) + " \
      "(CAST(current_setting('autovacuum_vacuum_scale_factor') AS numeric) * pg_class.reltuples), '9G999G999G999') AS av_threshold, CASE WHEN CAST(current_setting('autovacuum_vacuum_threshold') AS bigint) + " \
      "(CAST(current_setting('autovacuum_vacuum_scale_factor') AS numeric) * pg_class.reltuples) < psut.n_dead_tup THEN '*' ELSE '' END AS expect_av " \
      "FROM pg_stat_user_tables psut JOIN pg_class on psut.relid = pg_class.oid  where psut.schemaname = '%s' and (psut.n_dead_tup > %d OR (last_vacuum is null and last_autovacuum is null)) ORDER BY 5 desc, 1;" % (schema, threshold_dead_tups)
      
try:
     cur.execute(sql)
except Exception as error:
    printit("Exception: %s *** %s" % (type(error), error))
    conn.close()
    sys.exit (1)
    
rows = cur.fetchall()
if len(rows) == 0:
    printit ("No vacuums to be done.")
else:
    printit ("vacuums to be evaluated=%d" % len(rows) )

cnt = 0
partcnt = 0
action_name = 'VACUUM'
for row in rows:
    if active_processes > threshold_max_processes:
        # see how many are currently running and update the active processes again
        # rc = get_process_cnt()
        rc = get_query_cnt(conn, cur)
        if rc > threshold_max_processes:
            printit ("Current process cnt(%d) is still higher than threshold (%d). Sleeping for 5 minutes..." % (rc, threshold_max_processes))
            time.sleep(300)
        else:
            printit ("Current process cnt(%d) is less than threshold (%d).  Processing will continue..." % (rc, threshold_max_processes))
        active_processes = rc

    cnt = cnt + 1
    table= row[0]
    tups = row[3]
    dead = row[4]
    sizep= row[5]
    size = row[6]
    part = row[7]    

    if part and ignoreparts:
        partcnt = partcnt + 1    
        #print ("ignoring partitioned table: %s" % table)
        continue
        
    # check if we already processed this table
    if skip_table(table, tablist):
        continue
    #else:
        #printit("table = %s will NOT be skipped." % table)

    if size > threshold_max_size:
        # defer action
        printit ("Async %13s: %03d %-57s rows: %11d size: %10s :%13d dead: %8d NOTICE: Skipping large table.  Do manually." % (action_name, cnt, table, tups, sizep, size, dead))
        tables_skipped = tables_skipped + 1
        tablist.append(table)
        continue
    elif tups > threshold_async_rows or size > threshold_max_sync:
    #elif (tups > threshold_async_rows or size > threshold_max_sync) and async:
        if dryrun:
            if active_processes > threshold_max_processes:
                printit ("%13s: Max processes reached. Skipping further Async activity for very large table, %s.  Size=%s.  Do manually." % (action_name, table, sizep))
                tables_skipped = tables_skipped + 1
                continue
            printit ("Async %13s: %03d %-57s rows: %11d size: %10s :%13d dead: %8d" % (action_name, cnt, table, tups, sizep, size, dead))
            tablist.append(table)
            total_vacuums  = total_vacuums + 1
            active_processes = active_processes + 1
        else:
            if active_processes > threshold_max_processes:
                printit ("%13s: Max processes reached. Skipping further Async activity for very large table, %s.  Size=%s.  Do manually." % (action_name, table, sizep))
                tables_skipped = tables_skipped + 1
                continue
            printit ("Async %13s: %03d %-57s rows: %11d size: %10s :%13d dead: %8d" % (action_name, cnt, table, tups, sizep, size, dead))
            asyncjobs = asyncjobs + 1
            
            # v3.1 change to include application name
            connparms = "dbname=%s port=%d user=%s host=%s application_name=%s" % (dbname, dbport, dbuser, hostname, 'pg_vacuum' )
            # cmd = 'nohup psql -h %s -d %s -p %s -U %s -c "VACUUM VERBOSE %s" 2>/dev/null &' % (hostname, dbname, dbport, dbuser, table)
            cmd = 'nohup psql -d "%s" -c "VACUUM VERBOSE %s" 2>/dev/null &' % (connparms, table)                        

            time.sleep(0.5)
            rc = execute_cmd(cmd)
            total_vacuums  = total_vacuums + 1
            active_processes = active_processes + 1
            tablist.append(table)

    else:
        if dryrun:
            printit ("Sync  %13s: %03d %-57s rows: %11d size: %10s :%13d dead: %8d" % (action_name, cnt, table, tups, sizep, size, dead))
            total_vacuums  = total_vacuums + 1
            tablist.append(table)
        else:
            printit ("Sync  %13s: %03d %-57s rows: %11d size: %10s :%13d dead: %8d" %  (action_name, cnt, table, tups, sizep, size, dead))
            sql = "VACUUM VERBOSE %s" % table
            time.sleep(0.5)
            try:
                cur.execute(sql)
            except Exception as error:
                printit("Exception: %s *** %s" % (type(error), error))
                continue
            total_vacuums  = total_vacuums + 1
            tablist.append(table)

if ignoreparts:
    printit ("Partitioned table vacuums bypassed=%d" % partcnt)
    partitioned_tables_skipped = partitioned_tables_skipped + partcnt
    
#################################
# 4. Analyze on Small Tables    #
#################################
'''
-- all
select n.nspname || '.' || c.relname as table, c.reltuples::bigint, u.n_live_tup::bigint, u.n_dead_tup::bigint, pg_size_pretty(pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname))::bigint),  pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) as size, c.relispartition, u.last_analyze, u.last_autoanalyze, case when c.reltuples = 0 THEN -1 ELSE round((u.n_live_tup / c.reltuples) * 100) END as tupdiff, now()::date  - last_analyze::date as lastanalyzed2 from pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and n.nspname not in ('pg_catalog', 'pg_toast', 'information_schema') and t.schemaname = n.nspname and t.tablename = c.relname and c.relname = u.relname and
u.schemaname = n.nspname and n.nspname not in ('information_schema','pg_catalog') and now()::date - GREATEST(last_analyze, last_autoanalyze)::date > 5 and
pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) <= 50000000 order by 1,2;

-- public schema
select n.nspname || '.' || c.relname as table, c.reltuples::bigint, u.n_live_tup::bigint, u.n_dead_tup::bigint, pg_size_pretty(pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname))::bigint),  pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) as size, c.relispartition, u.last_analyze, u.last_autoanalyze, case when c.reltuples = 0 THEN -1 ELSE round((u.n_live_tup / c.reltuples) * 100) END as tupdiff, now()::date  - last_analyze::date as lastanalyzed2 from pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and t.schemaname = 'public' and t.schemaname = n.nspname and t.tablename = c.relname and c.relname = u.relname and u.schemaname = n.nspname and now()::date - GREATEST(last_analyze, last_autoanalyze)::date > 5 and
pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) <= 50000000 order by 1,2;
'''
if version > 100000:
    if schema == "":
       sql = "select n.nspname || '.\"' || c.relname || '\"' as table, c.reltuples::bigint, u.n_live_tup::bigint, u.n_dead_tup::bigint, pg_size_pretty(pg_total_relation_size(quote_ident(n.nspname) || '.' ||  " \
      "quote_ident(c.relname))::bigint), pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) as size, c.relispartition, u.last_analyze, u.last_autoanalyze, case when c.reltuples = 0 THEN -1 ELSE round((u.n_live_tup / c.reltuples) * 100) END as tupdiff, " \
      "now()::date  - last_analyze::date as lastanalyzed2 from pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and n.nspname not in ('pg_catalog', 'pg_toast', 'information_schema') and t.schemaname = n.nspname and t.tablename = c.relname and " \
      "c.relname = u.relname and u.schemaname = n.nspname and n.nspname not in ('information_schema','pg_catalog') and now()::date - GREATEST(last_analyze, last_autoanalyze)::date > %d " \
      "and pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) <= %d order by 1,2" % (threshold_max_days_analyze, threshold_min_size)
    else:
       sql = "select n.nspname || '.\"' || c.relname || '\"' as table, c.reltuples::bigint, u.n_live_tup::bigint, u.n_dead_tup::bigint, pg_size_pretty(pg_total_relation_size(quote_ident(n.nspname) || '.' || " \
      "quote_ident(c.relname))::bigint), pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) as size, c.relispartition, u.last_analyze, u.last_autoanalyze, case when c.reltuples = 0 THEN -1 ELSE round((u.n_live_tup / c.reltuples) * 100) END as tupdiff, " \
      "now()::date  - last_analyze::date as lastanalyzed2 from pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and t.schemaname = '%s' and t.schemaname = n.nspname and " \
      "t.tablename = c.relname and c.relname = u.relname and u.schemaname = n.nspname and now()::date - GREATEST(last_analyze, last_autoanalyze)::date > %d " \
      "and pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) <= %d order by 1,2" % (schema, threshold_max_days_analyze, threshold_min_size)
else:
# put version 9.x compatible query here
# CASE WHEN (SELECT c.relname AS child FROM pg_inherits i JOIN pg_class p ON (i.inhparent=p.oid) where i.inhrelid=c.oid) IS NULL THEN 'False' ELSE 'True' END as partitioned 
    if schema == "":
       sql = "select n.nspname || '.\"' || c.relname || '\"' as table, c.reltuples::bigint, u.n_live_tup::bigint, u.n_dead_tup::bigint, pg_size_pretty(pg_total_relation_size(quote_ident(n.nspname) || '.' ||  " \
      "quote_ident(c.relname))::bigint), pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) as size, CASE WHEN (SELECT c.relname AS child FROM pg_inherits i JOIN pg_class p ON (i.inhparent=p.oid) where i.inhrelid=c.oid) IS NULL THEN 'False'::boolean ELSE 'True'::boolean END as partitioned, u.last_analyze, u.last_autoanalyze, case when c.reltuples = 0 THEN -1 ELSE round((u.n_live_tup / c.reltuples) * 100) END as tupdiff, " \
      "now()::date  - last_analyze::date as lastanalyzed2 from pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and n.nspname not in ('pg_catalog', 'pg_toast', 'information_schema') and t.schemaname = n.nspname and t.tablename = c.relname and " \
      "c.relname = u.relname and u.schemaname = n.nspname and n.nspname not in ('information_schema','pg_catalog') and now()::date - GREATEST(last_analyze, last_autoanalyze)::date > %d " \
      "and pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) <= %d order by 1,2" % (threshold_max_days_analyze, threshold_min_size)
    else:
       sql = "select n.nspname || '.\"' || c.relname || '\"' as table, c.reltuples::bigint, u.n_live_tup::bigint, u.n_dead_tup::bigint, pg_size_pretty(pg_total_relation_size(quote_ident(n.nspname) || '.' || " \
      "quote_ident(c.relname))::bigint), pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) as size, CASE WHEN (SELECT c.relname AS child FROM pg_inherits i JOIN pg_class p ON (i.inhparent=p.oid) where i.inhrelid=c.oid) IS NULL THEN 'False' ELSE 'True' END as partitioned, u.last_analyze, u.last_autoanalyze, case when c.reltuples = 0 THEN -1 ELSE round((u.n_live_tup / c.reltuples) * 100) END as tupdiff, " \
      "now()::date  - last_analyze::date as lastanalyzed2 from pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and t.schemaname = '%s' and t.schemaname = n.nspname and " \
      "t.tablename = c.relname and c.relname = u.relname and u.schemaname = n.nspname and now()::date - GREATEST(last_analyze, last_autoanalyze)::date > %d " \
      "and pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) <= %d order by 1,2" % (schema, threshold_max_days_analyze, threshold_min_size)

try:
    cur.execute(sql)
except Exception as error:
    printit("Exception: %s *** %s" % (type(error), error))
    conn.close()
    sys.exit (1)    
      
rows = cur.fetchall()
if len(rows) == 0:
    printit ("No small tables require analyzes to be done.")
else:
    printit ("Small table analyzes to be evaluated=%d" % len(rows) )

cnt = 0
partcnt = 0
action_name = 'ANALYZE'
for row in rows:
    cnt = cnt + 1
    table= row[0]
    tups = row[1]
    dead = row[3]
    sizep= row[4]
    size = row[5]
    part = row[6]

    if part and ignoreparts:
        partcnt = partcnt + 1    
        #print *"ignoring partitioned table: %s" % table)
        continue

    # check if we already processed this table
    if skip_table(table, tablist):
        continue

    if dryrun:
        printit ("Sync  %13s: %03d %-57s rows: %11d size: %10s :%13d dead: %8d" % (action_name, cnt, table, tups, sizep, size, dead))
        total_analyzes  = total_analyzes + 1
        tablist.append(table)
    else:
        printit ("Sync  %13s: %03d %-57s rows: %11d size: %10s :%13d dead: %8d" % (action_name, cnt, table, tups, sizep, size, dead))
        sql = "ANALYZE VERBOSE %s" % table
        time.sleep(0.5)
        total_analyzes  = total_analyzes + 1
        tablist.append(table)
        try:
            cur.execute(sql)
        except Exception as error:
            printit("Exception: %s *** %s" % (type(error), error))

if ignoreparts:
    printit ("Small partitioned table analyzes bypassed=%d" % partcnt)
    partitioned_tables_skipped = partitioned_tables_skipped + partcnt

#################################
# 5. Analyze on Big Tables      #
#################################
'''
query gets rows > 50MB
-- all
select n.nspname || '.' || c.relname as table, c.reltuples::bigint, u.n_live_tup::bigint, u.n_dead_tup::bigint, pg_size_pretty(pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname))::bigint),  pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) as size, c.relispartition, u.last_analyze, u.last_autoanalyze,
case when c.reltuples = 0 THEN -1 ELSE round((u.n_live_tup / c.reltuples) * 100) END as tupdiff, now()::date  - last_analyze::date as lastanalyzed2
 from pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and n.nspname not in ('pg_catalog', 'pg_toast', 'information_schema') and t.schemaname = n.nspname and t.tablename = c.relname and c.relname = u.relname and u.schemaname = n.nspname and n.nspname not in ('information_schema','pg_catalog') and ((last_analyze is null and last_autoanalyze is null) or (now()::date  - last_analyze::date > 5 AND now()::date - last_autoanalyze::date > 5)) and
 pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) > 50000000 order by 1,2;

-- public schema
select n.nspname || '.' || c.relname as table, c.reltuples::bigint, u.n_live_tup::bigint, u.n_dead_tup::bigint, pg_size_pretty(pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname))::bigint),  pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) as size, c.relispartition, u.last_analyze, u.last_autoanalyze,
case when c.reltuples = 0 THEN -1 ELSE round((u.n_live_tup / c.reltuples) * 100) END as tupdiff, now()::date  - last_analyze::date as lastanalyzed2
 from pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and t.schemaname = 'public' and t.schemaname = n.nspname and t.tablename = c.relname and c.relname = u.relname and u.schemaname = n.nspname and ((last_analyze is null and last_autoanalyze is null) or (now()::date  - last_analyze::date > 5 AND now()::date - last_autoanalyze::date > 5)) and
 pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) > 50000000 order by 1,2;

'''
if version > 100000:
    if schema == "":
       sql = "select n.nspname || '.\"' || c.relname || '\"' as table, c.reltuples::bigint, u.n_live_tup::bigint, u.n_dead_tup::bigint, pg_size_pretty(pg_total_relation_size(quote_ident(n.nspname) || '.' || " \
      "quote_ident(c.relname))::bigint), pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) as size, c.relispartition, u.last_analyze, u.last_autoanalyze, " \
      "case when c.reltuples = 0 THEN -1 ELSE round((u.n_live_tup / c.reltuples) * 100) END as tupdiff, now()::date  - last_analyze::date as lastanalyzed2 " \
      "from pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and n.nspname not in ('pg_catalog', 'pg_toast', 'information_schema') and t.schemaname = n.nspname and t.tablename = c.relname and c.relname = u.relname and  " \
      "u.schemaname = n.nspname and n.nspname not in ('information_schema','pg_catalog') and ((last_analyze is null and last_autoanalyze is null) or (now()::date  - last_analyze::date > %d AND  " \
      "now()::date - last_autoanalyze::date > %d)) and pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) > %d order by 1,2;" % (threshold_max_days_analyze,threshold_max_days_analyze, threshold_min_size)
    else:
       sql = "select n.nspname || '.\"' || c.relname || '\"' as table, c.reltuples::bigint, u.n_live_tup::bigint, u.n_dead_tup::bigint, pg_size_pretty(pg_total_relation_size(quote_ident(n.nspname) || '.' || " \
      "quote_ident(c.relname))::bigint), pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) as size, c.relispartition, u.last_analyze, u.last_autoanalyze, " \
      "case when c.reltuples = 0 THEN -1 ELSE round((u.n_live_tup / c.reltuples) * 100) END as tupdiff, now()::date  - last_analyze::date as lastanalyzed2 " \
      "from pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and t.schemaname = '%s' and t.schemaname = n.nspname and t.tablename = c.relname and c.relname = u.relname and  " \
      "u.schemaname = n.nspname and ((last_analyze is null and last_autoanalyze is null) or (now()::date  - last_analyze::date > %d AND  " \
      "now()::date - last_autoanalyze::date > %d)) and pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) > %d order by 1,2;" % (schema, threshold_max_days_analyze,threshold_max_days_analyze, threshold_min_size)
else:
# put version 9.x compatible query here
# CASE WHEN (SELECT c.relname AS child FROM pg_inherits i JOIN pg_class p ON (i.inhparent=p.oid) where i.inhrelid=c.oid) IS NULL THEN 'False' ELSE 'True' END as partitioned 
    if schema == "":
       sql = "select n.nspname || '.\"' || c.relname || '\"' as table, c.reltuples::bigint, u.n_live_tup::bigint, u.n_dead_tup::bigint, pg_size_pretty(pg_total_relation_size(quote_ident(n.nspname) || '.' || " \
      "quote_ident(c.relname))::bigint), pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) as size, CASE WHEN (SELECT c.relname AS child FROM pg_inherits i JOIN pg_class p ON (i.inhparent=p.oid) where i.inhrelid=c.oid) IS NULL THEN 'False'::boolean ELSE 'True'::boolean END as partitioned, u.last_analyze, u.last_autoanalyze, " \
      "case when c.reltuples = 0 THEN -1 ELSE round((u.n_live_tup / c.reltuples) * 100) END as tupdiff, now()::date  - last_analyze::date as lastanalyzed2 " \
      "from pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and n.nspname not in ('pg_catalog', 'pg_toast', 'information_schema') and t.schemaname = n.nspname and t.tablename = c.relname and c.relname = u.relname and  " \
      "u.schemaname = n.nspname and n.nspname not in ('information_schema','pg_catalog') and ((last_analyze is null and last_autoanalyze is null) or (now()::date  - last_analyze::date > %d AND  " \
      "now()::date - last_autoanalyze::date > %d)) and pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) > %d order by 1,2;" % (threshold_max_days_analyze,threshold_max_days_analyze, threshold_min_size)
    else:
       sql = "select n.nspname || '.\"' || c.relname || '\"' as table, c.reltuples::bigint, u.n_live_tup::bigint, u.n_dead_tup::bigint, pg_size_pretty(pg_total_relation_size(quote_ident(n.nspname) || '.' || " \
      "quote_ident(c.relname))::bigint), pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) as size, CASE WHEN (SELECT c.relname AS child FROM pg_inherits i JOIN pg_class p ON (i.inhparent=p.oid) where i.inhrelid=c.oid) IS NULL THEN 'False'::boolean ELSE 'True'::boolean END as partitioned, u.last_analyze, u.last_autoanalyze, " \
      "case when c.reltuples = 0 THEN -1 ELSE round((u.n_live_tup / c.reltuples) * 100) END as tupdiff, now()::date  - last_analyze::date as lastanalyzed2 " \
      "from pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and t.schemaname = '%s' and t.schemaname = n.nspname and t.tablename = c.relname and c.relname = u.relname and  " \
      "u.schemaname = n.nspname and ((last_analyze is null and last_autoanalyze is null) or (now()::date  - last_analyze::date > %d AND  " \
      "now()::date - last_autoanalyze::date > %d)) and pg_total_relation_size(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) > %d order by 1,2;" % (schema, threshold_max_days_analyze,threshold_max_days_analyze, threshold_min_size)

try:
    cur.execute(sql)
except Exception as error:
    printit("Exception: %s *** %s" % (type(error), error))
    conn.close()
    sys.exit (1)

rows = cur.fetchall()
if len(rows) == 0:
    printit ("No stale tables require analyzes to be done.")
else:
    printit ("Big table analyzes to be evaluated=%d" % len(rows) )

cnt = 0
partcnt = 0
action_name = 'ANALYZE'
for row in rows:
    cnt = cnt + 1
    table= row[0]
    tups = row[1]
    dead = row[3]
    sizep= row[4]
    size = row[5]
    part = row[6]

    if part and ignoreparts:
        partcnt = partcnt + 1    
        #print ("ignoring partitioned table: %s" % table)
        continue

    # check if we already processed this table
    if skip_table(table, tablist):
        continue

    # skip tables that are too large
    if active_processes > threshold_max_processes:
        # see how many are currently running and update the active processes again
        # rc = get_process_cnt()
        rc = get_query_cnt(conn, cur)
        if rc > threshold_max_processes:
            printit ("Current process cnt(%d) is still higher than threshold (%d). Sleeping for 5 minutes..." % (rc, threshold_max_processes))
            time.sleep(300)
        else:
            printit ("Current process cnt(%d) is less than threshold (%d).  Processing will continue..." % (rc, threshold_max_processes))
        active_processes = rc

    if size > threshold_max_size:
        if dryrun:
            printit ("Async %13s: %03d %-57s rows: %11d size: %10s :%13d dead: %8d NOTICE: Skipping large table.  Do manually." % (action_name, cnt, table, tups, sizep, size, dead))
            tables_skipped = tables_skipped + 1
        else:
            printit ("Async %13s: %03d %-57s rows: %11d size: %10s :%13d dead: %8d NOTICE: Skipping large table.  Do manually." % (action_name, cnt, table, tups, sizep, size, dead))
            tables_skipped = tables_skipped + 1
        continue
    elif size > threshold_max_sync:
        if dryrun:
            if active_processes > threshold_max_processes:
                printit ("%13s: Max processes reached. Skipping further Async activity for very large table, %-57s.  Size=%s.  Do manually." % (action_name, table, sizep))
                tables_skipped = tables_skipped + 1
                continue
            printit ("Async %13s: %03d %-57s rows: %11d size: %10s :%13d dead: %8d" % (action_name, cnt, table, tups, sizep, size, dead))
            active_processes = active_processes + 1
            total_analyzes  = total_analyzes + 1
            tablist.append(table)
        else:
            if active_processes > threshold_max_processes:
                printit ("%13s: Max processes reached. Skipping further Async activity for very large table, %-57s.  Size=%s.  Do manually." % (action_name, table, sizep))
                tables_skipped = tables_skipped + 1
                continue
            printit ("Async %13s: %03d %-57s rows: %11d size: %10s :%13d dead: %8d" % (action_name, cnt, table, tups, sizep, size, dead))
            tablist.append(table)
            asyncjobs = asyncjobs + 1
            
            # v3.1 change to include application name
            connparms = "dbname=%s port=%d user=%s host=%s application_name=%s" % (dbname, dbport, dbuser, hostname, 'pg_vacuum' )
            # cmd = 'nohup psql -h %s -d %s -p %s -U %s -c "ANALYZE VERBOSE %s" 2>/dev/null &' % (hostname, dbname, dbport, dbuser, table)
            cmd = 'nohup psql -d "%s" -c "ANALYZE VERBOSE %s" 2>/dev/null &' % (connparms, table)                                    
            
            time.sleep(0.5)
            rc = execute_cmd(cmd)
            active_processes = active_processes + 1
            total_analyzes  = total_analyzes + 1
    else:
        if dryrun:
            printit ("Sync  %13s: %03d %-57s rows: %11d size: %10s :%13d dead: %8d" % (action_name, cnt, table, tups, sizep, size, dead))
            total_analyzes  = total_analyzes + 1
            tablist.append(table)
        else:
            printit ("Sync  %13s: %03d %-57s rows: %11d size: %10s :%13d dead: %8d" % (action_name, cnt, table, tups, sizep, size, dead))
            tablist.append(table)
            sql = "ANALYZE VERBOSE %s" % table
            time.sleep(0.5)
            try:
                cur.execute(sql)
            except Exception as error:
                printit("Exception: %s *** %s" % (type(error), error))
                continue
            total_analyzes  = total_analyzes + 1
if ignoreparts:
    printit ("Big partitioned table analyzes bypassed=%d" % partcnt)
    partitioned_tables_skipped = partitioned_tables_skipped + partcnt    


#################################
# 6. Catchall query for analyze that have not happened for over 2 weeks.
#################################
# V2.3: Introduced
'''
-- all
SELECT u.schemaname || '.' || u.relname as table, pg_size_pretty(pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname))::bigint) as size_pretty, pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname)) as size, c.reltuples::bigint AS n_tup, u.n_live_tup::bigint as n_live_tup, u.n_dead_tup::bigint AS dead_tup, c.relispartition, to_char(u.last_vacuum, 'YYYY-MM-DD HH24:MI') as last_vacuum, to_char(u.last_autovacuum, 'YYYY-MM-DD HH24:MI') as last_autovacuum, to_char(u.last_analyze,'YYYY-MM-DD HH24:MI') as last_analyze, to_char(u.last_autoanalyze,'YYYY-MM-DD HH24:MI') as last_autoanalyze
FROM pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and n.nspname not in ('pg_catalog', 'pg_toast', 'information_schema') and t.schemaname = n.nspname and t.tablename = c.relname and c.relname = u.relname and u.schemaname = n.nspname and n.nspname not in ('information_schema','pg_catalog', 'pg_toast') AND now()::date - GREATEST(last_analyze, last_autoanalyze)::date > 30  order by 4,1;

-- public schema
SELECT u.schemaname || '.' || u.relname as table, pg_size_pretty(pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname))::bigint) as size_pretty, pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname)) as size, c.reltuples::bigint AS n_tup, u.n_live_tup::bigint as n_live_tup, u.n_dead_tup::bigint AS dead_tup, c.relispartition, to_char(u.last_vacuum, 'YYYY-MM-DD HH24:MI') as last_vacuum, to_char(u.last_autovacuum, 'YYYY-MM-DD HH24:MI') as last_autovacuum, to_char(u.last_analyze,'YYYY-MM-DD HH24:MI') as last_analyze, to_char(u.last_autoanalyze,'YYYY-MM-DD HH24:MI') as last_autoanalyze
FROM pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and t.schemaname = 'public' and t.schemaname = n.nspname and t.tablename = c.relname and c.relname = u.relname and u.schemaname = n.nspname AND now()::date - GREATEST(last_analyze, last_autoanalyze)::date > 30  order by 4,1;

'''
if version > 100000:
    if schema == "":
       sql = "SELECT u.schemaname || '.\"' || u.relname || '\"' as table, pg_size_pretty(pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname))::bigint) as size_pretty, " \
      "pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname)) as size, c.reltuples::bigint AS n_tup, u.n_live_tup::bigint as n_live_tup, u.n_dead_tup::bigint AS dead_tup, c.relispartition, " \
      "to_char(u.last_vacuum, 'YYYY-MM-DD HH24:MI') as last_vacuum, to_char(u.last_autovacuum, 'YYYY-MM-DD HH24:MI') as last_autovacuum, to_char(u.last_analyze,'YYYY-MM-DD HH24:MI') as last_analyze,  " \
      "to_char(u.last_autoanalyze,'YYYY-MM-DD HH24:MI') as last_autoanalyze FROM pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and n.nspname not in ('pg_catalog', 'pg_toast', 'information_schema') and t.schemaname = n.nspname  " \
      "and t.tablename = c.relname and c.relname = u.relname and u.schemaname = n.nspname and n.nspname not in ('information_schema','pg_catalog', 'pg_toast') AND  " \
      "now()::date - GREATEST(last_analyze, last_autoanalyze)::date > 30  order by 4,1"
    else:
       sql = "SELECT u.schemaname || '.\"' || u.relname || '\"' as table, pg_size_pretty(pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname))::bigint) as size_pretty, " \
      "pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname)) as size, c.reltuples::bigint AS n_tup, u.n_live_tup::bigint as n_live_tup, u.n_dead_tup::bigint AS dead_tup, c.relispartition, " \
      "to_char(u.last_vacuum, 'YYYY-MM-DD HH24:MI') as last_vacuum, to_char(u.last_autovacuum, 'YYYY-MM-DD HH24:MI') as last_autovacuum, to_char(u.last_analyze,'YYYY-MM-DD HH24:MI') as last_analyze,  " \
      "to_char(u.last_autoanalyze,'YYYY-MM-DD HH24:MI') as last_autoanalyze FROM pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and t.schemaname = '%s' and t.schemaname = n.nspname  " \
      "and t.tablename = c.relname and c.relname = u.relname and u.schemaname = n.nspname  AND  " \
      "now()::date - GREATEST(last_analyze, last_autoanalyze)::date > 30  order by 4,1" % (schema)
else:
# put version 9.x compatible query here  
# CASE WHEN (SELECT c.relname AS child FROM pg_inherits i JOIN pg_class p ON (i.inhparent=p.oid) where i.inhrelid=c.oid) IS NULL THEN 'False' ELSE 'True' END as partitioned 
    if schema == "":
       sql = "SELECT u.schemaname || '.\"' || u.relname || '\"' as table, pg_size_pretty(pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname))::bigint) as size_pretty, " \
      "pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname)) as size, c.reltuples::bigint AS n_tup, u.n_live_tup::bigint as n_live_tup, u.n_dead_tup::bigint AS dead_tup, CASE WHEN (SELECT c.relname AS child FROM pg_inherits i JOIN pg_class p ON (i.inhparent=p.oid) where i.inhrelid=c.oid) IS NULL THEN 'False'::boolean ELSE 'True'::boolean END as partitioned   , " \
      "to_char(u.last_vacuum, 'YYYY-MM-DD HH24:MI') as last_vacuum, to_char(u.last_autovacuum, 'YYYY-MM-DD HH24:MI') as last_autovacuum, to_char(u.last_analyze,'YYYY-MM-DD HH24:MI') as last_analyze,  " \
      "to_char(u.last_autoanalyze,'YYYY-MM-DD HH24:MI') as last_autoanalyze FROM pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and n.nspname not in ('pg_catalog', 'pg_toast', 'information_schema') and t.schemaname = n.nspname  " \
      "and t.tablename = c.relname and c.relname = u.relname and u.schemaname = n.nspname and n.nspname not in ('information_schema','pg_catalog', 'pg_toast') AND  " \
      "now()::date - GREATEST(last_analyze, last_autoanalyze)::date > 30  order by 4,1"
    else:
       sql = "SELECT u.schemaname || '.\"' || u.relname || '\"' as table, pg_size_pretty(pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname))::bigint) as size_pretty, " \
      "pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname)) as size, c.reltuples::bigint AS n_tup, u.n_live_tup::bigint as n_live_tup, u.n_dead_tup::bigint AS dead_tup, CASE WHEN (SELECT c.relname AS child FROM pg_inherits i JOIN pg_class p ON (i.inhparent=p.oid) where i.inhrelid=c.oid) IS NULL THEN 'False'::boolean ELSE 'True'::boolean END as partitioned   , " \
      "to_char(u.last_vacuum, 'YYYY-MM-DD HH24:MI') as last_vacuum, to_char(u.last_autovacuum, 'YYYY-MM-DD HH24:MI') as last_autovacuum, to_char(u.last_analyze,'YYYY-MM-DD HH24:MI') as last_analyze,  " \
      "to_char(u.last_autoanalyze,'YYYY-MM-DD HH24:MI') as last_autoanalyze FROM pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and t.schemaname = '%s' and t.schemaname = n.nspname  " \
      "and t.tablename = c.relname and c.relname = u.relname and u.schemaname = n.nspname  AND  " \
      "now()::date - GREATEST(last_analyze, last_autoanalyze)::date > 30  order by 4,1" % (schema)
try:
    cur.execute(sql)
except Exception as error:
    printit("Exception: %s *** %s" % (type(error), error))
    conn.close()
    sys.exit (1)

rows = cur.fetchall()
if len(rows) == 0:
    printit ("No very old analyzes to be done.")
else:
    printit ("very old analyzes to be evaluated=%d" % len(rows) )

cnt = 0
partcnt = 0
action_name = 'ANALYZE(2)'
for row in rows:
    if active_processes > threshold_max_processes:
        # see how many are currently running and update the active processes again
        # rc = get_process_cnt()
        rc = get_query_cnt(conn, cur)
        if rc > threshold_max_processes:
            printit ("Current process cnt(%d) is still higher than threshold (%d). Sleeping for 5 minutes..." % (rc, threshold_max_processes))
            time.sleep(300)
        else:
            printit ("Current process cnt(%d) is less than threshold (%d).  Processing will continue..." % (rc, threshold_max_processes))
        active_processes = rc

    cnt = cnt + 1
    table  = row[0]
    sizep  = row[1]
    size   = row[2]
    tups   = row[3]
    live   = row[4]
    dead   = row[5]
    part   = row[6]

    if part and ignoreparts:
        partcnt = partcnt + 1    
        #print ("ignoring partitioned table: %s" % table)
        continue

    # check if we already processed this table
    if skip_table(table, tablist):
        continue

    if size > threshold_max_size:
        # defer action
        if dryrun:
            printit ("Async %13s: %03d %-57s rows: %11d size: %10s :%13d dead: %8d NOTICE: Skipping large table.  Do manually." % (action_name, cnt, table, tups, sizep, size, dead))
            tables_skipped = tables_skipped + 1
        continue
    elif tups > threshold_async_rows or size > threshold_max_sync:
    #elif (tups > threshold_async_rows or size > threshold_max_sync) and async:
        if dryrun:
            if active_processes > threshold_max_processes:
                printit ("%13s: Max processes reached. Skipping further Async activity for very large table, %s.  Size=%s.  Do manually." % (action_name, table, sizep))
                tables_skipped = tables_skipped + 1
                continue
            printit ("Async %13s: %03d %-57s rows: %11d size: %10s :%13d dead: %8d" % (action_name, cnt, table, tups, sizep, size, dead))
            total_analyzes = total_analyzes + 1
            tablist.append(table)
            active_processes = active_processes + 1
        else:
            if active_processes > threshold_max_processes:
                printit ("%13s: Max processes reached. Skipping further Async activity for very large table, %s.  Size=%s.  Do manually." % (action_name, table, sizep))
                tables_skipped = tables_skipped + 1
                continue
            printit ("Async %13s: %03d %-57s rows: %11d size: %10s :%13d dead: %8d" % (action_name, cnt, table, tups, sizep, size, dead))
            asyncjobs = asyncjobs + 1
            
            # v3.1 change to include application name
            connparms = "dbname=%s port=%d user=%s host=%s application_name=%s" % (dbname, dbport, dbuser, hostname, 'pg_vacuum' )
            # cmd = 'nohup psql -h %s -d %s -p %s -U %s -c "ANALYZE VERBOSE %s" 2>/dev/null &' % (hostname, dbname, dbport, dbuser, table)
            cmd = 'nohup psql -d "%s" -c "ANALYZE VERBOSE %s" 2>/dev/null &' % (connparms, table)                                                
            
            time.sleep(0.5)
            rc = execute_cmd(cmd)
            total_analyzes = total_analyzes + 1
            tablist.append(table)
            active_processes = active_processes + 1

    else:
        if dryrun:
            printit ("Sync  %13s: %03d %-57s rows: %11d size: %10s :%13d dead: %8d" % (action_name, cnt, table, tups, sizep, size, dead))
            tablist.append(table)
        else:
            printit ("Sync  %13s: %03d %-57s rows: %11d size: %10s :%13d dead: %8d" % (action_name, cnt, table, tups, sizep, size, dead))
            sql = "ANALYZE VERBOSE %s" % table
            time.sleep(0.5)
            try:
                cur.execute(sql)
            except Exception as error:
                printit("Exception: %s *** %s" % (type(error), error))
                continue
            total_analyzes = total_analyzes + 1
            tablist.append(table)

if ignoreparts:
    printit ("Very old partitioned table analyzes bypassed=%d" % partcnt)
    partitioned_tables_skipped = partitioned_tables_skipped + partcnt    

#################################
# 7. Catchall query for vacuums that have not happened past vacuum max days threshold
#################################
# V2.3: Introduced
'''
-- all
-- v10 or higher
SELECT u.schemaname || '.' || u.relname as table, pg_size_pretty(pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname))::bigint) as size_pretty, pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname)) as size, c.reltuples::bigint AS n_tup, u.n_live_tup::bigint as n_live_tup, u.n_dead_tup::bigint AS dead_tup, c.relispartition, to_char(u.last_vacuum, 'YYYY-MM-DD HH24:MI') as last_vacuum, to_char(u.last_autovacuum, 'YYYY-MM-DD HH24:MI') as last_autovacuum, to_char(u.last_analyze,'YYYY-MM-DD HH24:MI') as last_analyze, to_char(u.last_autoanalyze,'YYYY-MM-DD HH24:MI') as last_autoanalyze
FROM pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and n.nspname not in ('pg_catalog', 'pg_toast', 'information_schema') and t.schemaname = n.nspname and t.tablename = c.relname and c.relname = u.relname and u.schemaname = n.nspname and n.nspname not in ('information_schema','pg_catalog', 'pg_toast') AND now()::date - GREATEST(last_vacuum, last_autovacuum)::date > 30  order by 4,1;

-- v9.6 or lower
SELECT u.schemaname || '.\"' || u.relname || '\"' as table, pg_size_pretty(pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname))::bigint) as size_pretty, 
pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname)) as size, c.reltuples::bigint AS n_tup, u.n_live_tup::bigint as n_live_tup, u.n_dead_tup::bigint AS dead_tup, 
CASE WHEN (SELECT c.relname AS child FROM pg_inherits i JOIN pg_class p ON (i.inhparent=p.oid) where i.inhrelid=c.oid) IS NULL THEN 'False'::boolean ELSE 'True'::boolean END as partitioned, 
to_char(u.last_vacuum, 'YYYY-MM-DD HH24:MI') as last_vacuum, to_char(u.last_autovacuum, 'YYYY-MM-DD HH24:MI') as last_autovacuum, to_char(u.last_analyze,'YYYY-MM-DD HH24:MI') as last_analyze, 
to_char(u.last_autoanalyze,'YYYY-MM-DD HH24:MI') as last_autoanalyze FROM pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and n.nspname not in ('pg_catalog', 'pg_toast', 'information_schema') and 
t.schemaname = n.nspname  and t.tablename = c.relname and c.relname = u.relname and u.schemaname = n.nspname and n.nspname not in ('information_schema','pg_catalog', 'pg_toast') AND 
now()::date - GREATEST(last_vacuum, last_autovacuum)::date > 60  order by 4,1;


-- public schema
SELECT u.schemaname || '.' || u.relname as table, pg_size_pretty(pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname))::bigint) as size_pretty, pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname)) as size, c.reltuples::bigint AS n_tup, u.n_live_tup::bigint as n_live_tup, u.n_dead_tup::bigint AS dead_tup, c.relispartition, to_char(u.last_vacuum, 'YYYY-MM-DD HH24:MI') as last_vacuum, to_char(u.last_autovacuum, 'YYYY-MM-DD HH24:MI') as last_autovacuum, to_char(u.last_analyze,'YYYY-MM-DD HH24:MI') as last_analyze, to_char(u.last_autoanalyze,'YYYY-MM-DD HH24:MI') as last_autoanalyze
FROM pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and t.schemaname = 'public' and t.schemaname = n.nspname and t.tablename = c.relname and c.relname = u.relname and u.schemaname = n.nspname and now()::date - GREATEST(last_vacuum, last_autovacuum)::date > 30  order by 4,1;
'''
if version > 100000:
    if schema == "":
       sql = "SELECT u.schemaname || '.\"' || u.relname || '\"' as table, pg_size_pretty(pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname))::bigint) as size_pretty, " \
      "pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname)) as size, c.reltuples::bigint AS n_tup, u.n_live_tup::bigint as n_live_tup, u.n_dead_tup::bigint AS dead_tup, c.relispartition, " \
      "to_char(u.last_vacuum, 'YYYY-MM-DD HH24:MI') as last_vacuum, to_char(u.last_autovacuum, 'YYYY-MM-DD HH24:MI') as last_autovacuum, to_char(u.last_analyze,'YYYY-MM-DD HH24:MI') as last_analyze,  " \
      "to_char(u.last_autoanalyze,'YYYY-MM-DD HH24:MI') as last_autoanalyze FROM pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and n.nspname not in ('pg_catalog', 'pg_toast', 'information_schema') and t.schemaname = n.nspname  " \
      "and t.tablename = c.relname and c.relname = u.relname and u.schemaname = n.nspname and n.nspname not in ('information_schema','pg_catalog', 'pg_toast') AND  " \
      "now()::date - GREATEST(last_vacuum, last_autovacuum)::date > %d  order by 4,1" % (threshold_max_days_vacuum)
    else:
       sql = "SELECT u.schemaname || '.\"' || u.relname || '\"' as table, pg_size_pretty(pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname))::bigint) as size_pretty, " \
      "pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname)) as size, c.reltuples::bigint AS n_tup, u.n_live_tup::bigint as n_live_tup, u.n_dead_tup::bigint AS dead_tup, c.relispartition, " \
      "to_char(u.last_vacuum, 'YYYY-MM-DD HH24:MI') as last_vacuum, to_char(u.last_autovacuum, 'YYYY-MM-DD HH24:MI') as last_autovacuum, to_char(u.last_analyze,'YYYY-MM-DD HH24:MI') as last_analyze,  " \
      "to_char(u.last_autoanalyze,'YYYY-MM-DD HH24:MI') as last_autoanalyze FROM pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and t.schemaname = '%s' and t.schemaname = n.nspname  " \
      "and t.tablename = c.relname and c.relname = u.relname and u.schemaname = n.nspname  AND  now()::date - GREATEST(last_vacuum, last_autovacuum)::date > %d  order by 4,1" % (schema, threshold_max_days_vacuum)
else:
# put version 9.x compatible query here
# CASE WHEN (SELECT c.relname AS child FROM pg_inherits i JOIN pg_class p ON (i.inhparent=p.oid) where i.inhrelid=c.oid) IS NULL THEN 'False' ELSE 'True' END as partitioned 
    if schema == "":
       sql = "SELECT u.schemaname || '.\"' || u.relname || '\"' as table, pg_size_pretty(pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname))::bigint) as size_pretty, " \
      "pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname)) as size, c.reltuples::bigint AS n_tup, u.n_live_tup::bigint as n_live_tup, u.n_dead_tup::bigint AS dead_tup, CASE WHEN (SELECT c.relname AS child FROM pg_inherits i JOIN pg_class p ON (i.inhparent=p.oid) where i.inhrelid=c.oid) IS NULL THEN 'False'::boolean ELSE 'True'::boolean END as partitioned, " \
      "to_char(u.last_vacuum, 'YYYY-MM-DD HH24:MI') as last_vacuum, to_char(u.last_autovacuum, 'YYYY-MM-DD HH24:MI') as last_autovacuum, to_char(u.last_analyze,'YYYY-MM-DD HH24:MI') as last_analyze,  " \
      "to_char(u.last_autoanalyze,'YYYY-MM-DD HH24:MI') as last_autoanalyze FROM pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and n.nspname not in ('pg_catalog', 'pg_toast', 'information_schema') and t.schemaname = n.nspname  " \
      "and t.tablename = c.relname and c.relname = u.relname and u.schemaname = n.nspname and n.nspname not in ('information_schema','pg_catalog', 'pg_toast') AND  " \
      "now()::date - GREATEST(last_vacuum, last_autovacuum)::date > %d order by 4,1" % threshold_max_days_vacuum
    else:
       sql = "SELECT u.schemaname || '.\"' || u.relname || '\"' as table, pg_size_pretty(pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname))::bigint) as size_pretty, " \
      "pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname)) as size, c.reltuples::bigint AS n_tup, u.n_live_tup::bigint as n_live_tup, u.n_dead_tup::bigint AS dead_tup, CASE WHEN (SELECT c.relname AS child FROM pg_inherits i JOIN pg_class p ON (i.inhparent=p.oid) where i.inhrelid=c.oid) IS NULL THEN 'False'::boolean ELSE 'True'::boolean END as partitioned, " \
      "to_char(u.last_vacuum, 'YYYY-MM-DD HH24:MI') as last_vacuum, to_char(u.last_autovacuum, 'YYYY-MM-DD HH24:MI') as last_autovacuum, to_char(u.last_analyze,'YYYY-MM-DD HH24:MI') as last_analyze,  " \
      "to_char(u.last_autoanalyze,'YYYY-MM-DD HH24:MI') as last_autoanalyze FROM pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and t.schemaname = '%s' and t.schemaname = n.nspname  " \
      "and t.tablename = c.relname and c.relname = u.relname and u.schemaname = n.nspname  AND  now()::date - GREATEST(last_vacuum, last_autovacuum)::date > %d  order by 4,1" % (schema, threshold_max_days_vacuum)
      
try:
     cur.execute(sql)
except Exception as error:
    printit("Exception: %s *** %s" % (type(error), error))
    conn.close()
    sys.exit (1)

rows = cur.fetchall()
if len(rows) == 0:
    printit ("No very old vacuums to be done.")
else:
    printit ("very old vacuums to be evaluated=%d" % len(rows) )

cnt = 0
partcnt = 0
action_name = 'VACUUM(2)'
for row in rows:
    if active_processes > threshold_max_processes:
        # see how many are currently running and update the active processes again
        # rc = get_process_cnt()
        rc = get_query_cnt(conn, cur)
        if rc > threshold_max_processes:
            printit ("Current process cnt(%d) is still higher than threshold (%d). Sleeping for 5 minutes..." % (rc, threshold_max_processes))
            time.sleep(300)
        else:
            printit ("Current process cnt(%d) is less than threshold (%d).  Processing will continue..." % (rc, threshold_max_processes))
        active_processes = rc

    cnt = cnt + 1
    table  = row[0]
    sizep  = row[1]
    size   = row[2]
    tups   = row[3]
    live   = row[4]
    dead   = row[5]
    part   = row[6]

    if part and ignoreparts:
        partcnt = partcnt + 1    
        #print ("ignoring partitioned table: %s" % table)
        continue

    # check if we already processed this table
    if skip_table(table, tablist):
        continue

    if size > threshold_max_size:
        # defer action
        if dryrun:
            printit ("Async %13s: %03d %-57s rows: %11d  dead: %8d  size: %10s :%13d NOTICE: Skipping large table.  Do manually." % (action_name, cnt, table, tups, dead, sizep, size))
            tables_skipped = tables_skipped + 1
        continue
    elif tups > threshold_async_rows or size > threshold_max_sync:
    #elif (tups > threshold_async_rows or size > threshold_max_sync) and async:
        if dryrun:
            if active_processes > threshold_max_processes:
                printit ("%13s: Max processes reached. Skipping further Async activity for very large table, %s.  Size=%s.  Do manually." % (action_name, table, sizep))
                tables_skipped = tables_skipped + 1
                continue
            printit ("Async %13s: %03d %-57s rows: %11d size: %10s :%13d dead: %8d" % (action_name, cnt, table, tups, sizep, size, dead))
            total_vacuums = total_vacuums + 1
            tablist.append(table)
            active_processes = active_processes + 1
        else:
            if active_processes > threshold_max_processes:
                printit ("%13s: Max processes reached. Skipping further Async activity for very large table, %s.  Size=%s.  Do manually." % (action_name, table, sizep))
                tables_skipped = tables_skipped + 1
                continue
            printit ("Async %13s: %03d %-57s rows: %11d size: %10s :%13d dead: %8d" % (action_name, cnt, table, tups, sizep, size, dead))
            asyncjobs = asyncjobs + 1
            
            # v3.1 change to include application name
            connparms = "dbname=%s port=%d user=%s host=%s application_name=%s" % (dbname, dbport, dbuser, hostname, 'pg_vacuum' )
            # cmd = 'nohup psql -h %s -d %s -p %s -U %s -c "VACUUM VERBOSE %s" 2>/dev/null &' % (hostname, dbname, dbport, dbuser, table)
            cmd = 'nohup psql -d "%s" -c "VACUUM VERBOSE %s" 2>/dev/null &' % (connparms, table)                                                            
            
            time.sleep(0.5)
            rc = execute_cmd(cmd)
            total_vacuums = total_vacuums + 1
            tablist.append(table)
            active_processes = active_processes + 1

    else:
        if dryrun:
            printit ("Sync  %13s: %03d %-57s rows: %11d size: %10s :%13d dead: %8d" % (action_name, cnt, table, tups, sizep, size, dead))
            tablist.append(table)
        else:
            printit ("Sync  %13s: %03d %-57s rows: %11d size: %10s :%13d dead: %8d" % (action_name, cnt, table, tups, sizep, size, dead))
            sql = "VACUUM VERBOSE %s" % table
            time.sleep(0.5)
            try:
                cur.execute(sql)
            except Exception as error:
                printit("Exception: %s *** %s" % (type(error), error))
                continue

            total_vacuums = total_vacuums + 1
            tablist.append(table)

if ignoreparts:
    printit ("Very old partitioned table vacuums bypassed=%d" % partcnt)
    partitioned_tables_skipped = partitioned_tables_skipped + partcnt    

# wait for up to 2 hours for ongoing vacuums/analyzes to finish.
if not dryrun:
    wait_for_processes(conn,cur)

printit ("Vacuum Freeze: %d  Vacuum Analyze: %d  Total Vacuums: %d  Total Analyzes: %d  Skipped Partitioned Tables: %d  Total Skipped Tables: %d  Total Async Jobs: %d " \
         % (total_freezes, total_vacuums_analyzes, total_vacuums, total_analyzes, partitioned_tables_skipped, tables_skipped + partitioned_tables_skipped, asyncjobs))
rc = get_query_cnt(conn, cur)
if rc > 0:
    printit ("NOTE: Current vacuums/analyzes still in progress: %d" % (rc))

# v3.1 feature: show async jobs running
#ps -ef | grep 'psql -h '| grep -v '\--color'
psjobs = "ps -ef | grep 'psql -h %s'| grep -v '\--color'" % hostname
#print ("psjobs = %s" % psjobs)


# v 2.7 feature: if inquiry, then show results of 2 queries
# print ("tables evaluated=%s" % tablist)
if inquiry != '':
   if schema == "":
      sql = "SELECT u.schemaname || '.\"' || u.relname || '\"' as table, pg_size_pretty(pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname))::bigint) as size_pretty, " \
         "pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname)) as size, age(c.relfrozenxid) as xid_age,c.reltuples::bigint AS n_tup, u.n_live_tup::bigint as n_live_tup, " \
         "u.n_dead_tup::bigint AS dead_tup, coalesce(to_char(u.last_vacuum, 'YYYY-MM-DD'),'') as last_vacuum, coalesce(to_char(u.last_autovacuum, 'YYYY-MM-DD'),'') as last_autovacuum, " \
         "coalesce(to_char(u.last_analyze,'YYYY-MM-DD'),'') as last_analyze, coalesce(to_char(u.last_autoanalyze,'YYYY-MM-DD'),'') as last_autoanalyze " \
         "FROM pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and t.schemaname = n.nspname and t.tablename = c.relname and c.relname = u.relname " \
         "and u.schemaname = n.nspname and n.nspname not in ('information_schema','pg_catalog') order by 1"
   else:
      sql = "SELECT u.schemaname || '.\"' || u.relname || '\"' as table, pg_size_pretty(pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname))::bigint) as size_pretty, " \
         "pg_total_relation_size(quote_ident(u.schemaname) || '.' || quote_ident(u.relname)) as size, age(c.relfrozenxid) as xid_age,c.reltuples::bigint AS n_tup, u.n_live_tup::bigint as n_live_tup, " \
         "u.n_dead_tup::bigint AS dead_tup, coalesce(to_char(u.last_vacuum, 'YYYY-MM-DD'),'') as last_vacuum, coalesce(to_char(u.last_autovacuum, 'YYYY-MM-DD'),'') as last_autovacuum, " \
         "coalesce(to_char(u.last_analyze,'YYYY-MM-DD'),'') as last_analyze, coalesce(to_char(u.last_autoanalyze,'YYYY-MM-DD'),'') as last_autoanalyze " \
         "FROM pg_namespace n, pg_class c, pg_tables t, pg_stat_user_tables u where c.relnamespace = n.oid and t.schemaname = '%s' and t.schemaname = n.nspname and t.tablename = c.relname and c.relname = u.relname " \
         "and u.schemaname = n.nspname order by 1" % schema
   try:
       cur.execute(sql)
   except Exception as error:
       printit("Exception: %s *** %s" % (type(error), error))
       conn.close()
       sys.exit (1)

   rows = cur.fetchall()
   if len(rows) == 0:
       printit ("Not able to retrieve inquiry results.")
   else:
       printit ("Inquiry Results Follow...")

   # v2.8 fix: indented following section else if was not part of the inquiry if section for cases that did not specify inquiry action.
   cnt = 0
   for row in rows:
      cnt = cnt + 1
      table            = row[0]
      sizep            = row[1]
      size             = row[2]
      xid_age          = row[3]
      n_tup            = row[4]
      n_live_tup       = row[5]
      dead_tup         = row[6]
      last_vacuum      = str(row[7])
      last_autovacuum  = str(row[8])
      last_analyze     = str(row[9])
      last_autoanalyze = str(row[10])

      if cnt == 1:
          printit("%55s %14s %14s %14s %12s %10s %10s %11s %12s %12s %16s" % ('table', 'sizep', 'size', 'xid_age', 'n_tup', 'n_live_tup', 'dead_tup', 'last_vacuum', 'last_autovac', 'last_analyze', 'last_autoanalyze'))
          printit("%55s %14s %14s %14s %12s %10s %10s %11s %12s %12s %16s" % ('-----', '-----', '----', '-------', '-----', '----------', '--------', '-----------', '------------', '------------', '----------------'))

      #print ("table = %s  len=%d" % (table, len(table)))

      #pretty_size_span = 14     
      #reduce = len(table) - 50
      #if reduce > 0 and reduce < 8:
      #    pretty_size_span = pretty_size_span - reduce

      if inquiry == 'all':
          printit("%55s %14s %14d %14d %12d %10d %10d %11s %12s %12s %16s" % (table, sizep, size, xid_age, n_tup, n_live_tup, dead_tup, last_vacuum, last_autovacuum, last_analyze, last_autoanalyze))      
          #printit("%55s %d%s %14d %14d %12d %10d %10d %11s %12s %12s %16s" % (table, pretty_size_span, sizep, size, xid_age, n_tup, n_live_tup, dead_tup, last_vacuum, last_autovacuum, last_analyze, last_autoanalyze))                
      else:    
          if skip_table(table, tablist):      
              printit("%55s %14s %14d %14d %12d %10d %10d %11s %12s %12s %16s" % (table, sizep, size, xid_age, n_tup, n_live_tup, dead_tup, last_vacuum, last_autovacuum, last_analyze, last_autoanalyze))
              #printit("%55s %d%s %14d %14d %12d %10d %10d %11s %12s %12s %16s" % (table, pretty_size_span, sizep, size, xid_age, n_tup, n_live_tup, dead_tup, last_vacuum, last_autovacuum, last_analyze, last_autoanalyze))              

# end of inquiry section

# Close communication with the database
conn.close()
printit ("Closed the connection and exiting normally.")
sys.exit(0)
