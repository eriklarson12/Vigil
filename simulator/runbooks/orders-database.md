---
slug: orders-database
title: Orders Database Runbook
service_tags: [orders, database]
---

# Orders Database Runbook

## Slow queries

SlowQueries alerts fire when orders p99 query latency exceeds 4 seconds. First distinguish load
from locking:

- `SELECT * FROM pg_stat_activity WHERE state != 'idle' ORDER BY query_start;`
- Lock waits show `wait_event_type = 'Lock'` — go to the Migration lock contention section.
- If it is pure load, check for missing indexes with `EXPLAIN ANALYZE` on the slowest query from
  pg_stat_statements.

## Migration lock contention

A `CREATE INDEX` without `CONCURRENTLY` takes a lock that blocks reads and writes on the table for
the entire build. Symptoms: queries queueing, lock waits climbing, one long-running CREATE INDEX
in pg_stat_activity.

1. Find the blocking backend: `SELECT pid, query FROM pg_stat_activity WHERE query ILIKE 'create index%';`
2. Cancel it: `SELECT pg_cancel_backend(<pid>);` — latency recovers within seconds.
3. Re-run the migration as `CREATE INDEX CONCURRENTLY` (cannot run inside a transaction block).
4. Add the migration lint: non-concurrent index creation should fail CI review.

## Connection pool exhaustion

If latency climbs with `too many connections` errors, check pool saturation on the orders service
(db_pool metrics) before raising max_connections — leaked connections from a recent change are the
usual cause.
