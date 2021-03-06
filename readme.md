# pg_vacuum

This python program determines whether a vacuum/analyze/freeze should be done and if so, which one.

(c) 2018-2021 SQLEXEC LLC
<br/>
GNU V3 and MIT licenses are conveyed accordingly.
<br/>
Bugs can be reported @ michaeldba@sqlexec.com


## History
The first version of this program was created in 2018.  
Program renamed from optimize_db.py to pg_vacuum.py (December 2020)

## Overview
This program is useful to identify and vacuum tables.  Most inputs are optional, and either an optional parameter is not used or a default value is used if not provided.  That means you can override internal parameters by specifying them on the command line.  Here are the parameters:
<br/>
`-H --host`              host name
<br/>
`-d --dbname`            database name
<br/>
`-p --dbport`            database port
<br/>
`-U --dbuser`            database user
<br/>
`-s --maxsize`           max table size that will be considered
<br/>
`-y --analyzemaxdays`    Analyzes older than this will be considered
<br/>
`-x --vacuummaxdays`     Vacuums older than this will be considered
<br/>
`-t --mindeadtups`       minimum dead tups before considering a vacuum
<br/>
`-m --schema`            if provided, perform actions only on this schema
<br/>
`-z --pctfreeze`         specifies how close to wraparound before FREEZE is done.
<br/>
`-f --freeze`            perform freeze if necessary
<br/>
`-r --dryrun`            do a dry run for analysis before actually running it.
<br/>
`-q --inquiry`           show stats to validate run.  Best used with dryrun. Values: "all" | "found" | not specified
<br/>
`-i --ignoreparts`       ignore partitioned tables
<br/>
`-a --async`             run async jobs ignoring thresholds
<br/>
<br/>

## Requirements
1. python 2.7 or above
2. python packages: psycopg2
<br/>

## Assumptions
1. Only when a table is within 25 million of reaching the wraparound threshold is it considered a FREEZE candidate. 
2. By default, catalog tables are ignored unless specified explicitly with the --schema option.
<br/>

## Examples
pg_vacuum.py -H localhost -d testing -p 5432 -u postgres --maxsize 40000000 --maxdays 1 --mindeadtups 1000 --schema public --dryrun
<br/><br/>
pg_vacuum.py -H localhost -d testing -p 5432 -u postgres -s 400000000000 -y 1 -t 1000 -m public --pctfreeze 90 --freeze
<br/>
