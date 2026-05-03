# 11. Parallel replicas

> **Tier 3 — Medium, situational.** Splits a single query's work across multiple replicas. ClickHouse Cloud's multi-replica services are pre-wired for this. Linear speed-up on large scans; **incompatible with `FINAL` and projections**, and can be slower than serial for small or high-cardinality queries.

## What it is

Parallel replicas distribute one query across multiple stateless compute nodes. Work is split **granule-by-granule**, not by shard — so this provides horizontal read parallelism without sharding the data. In ClickHouse Cloud, all replicas share the same object-storage data, which makes the topology natural: each replica reads its assigned granules from S3 and the results are merged on the initiating node.

This is **not** the same as the parallelism within a single replica (multiple threads scanning local granules). That's always on. Parallel replicas adds *cross-replica* parallelism on top.

## When to enable

Per the [docs](https://clickhouse.com/docs/deployment-guides/parallel-replicas):

- Large scans processing tens of millions+ rows.
- Simple aggregations (`count`, `sum`, `avg`) without complex CTEs or correlated subqueries.
- Multi-replica services where replicas already exist and are otherwise idle.

This account's service is a 3-replica `us-east-1` AWS deployment, so the topology is in place.

## When to leave off

- **Queries using `FINAL`.** Incompatible.
- **Queries using projections.** Incompatible — see [projections.md](projections.md).
- **Small queries.** Coordination overhead exceeds the parallelism win. Use `parallel_replicas_min_number_of_rows_per_replica` to set the floor.
- **High-cardinality `GROUP BY` aggregations.** Coordination overhead can make them *slower* than serial.
- **Queries with subqueries / multi-table JOINs.** "Can have a negative impact on query performance" per the docs.

## Settings

```sql
-- Session or per-query
SET enable_parallel_replicas = 1;                          -- 1 = enable, 2 = force-or-throw
SET cluster_for_parallel_replicas = 'default';             -- 'default' works in Cloud
SET max_parallel_replicas = 3;                             -- how many to use
SET enable_analyzer = 1;                                   -- required
SET parallel_replicas_min_number_of_rows_per_replica = 500000;
                                                           -- avoid spinning up for small queries
```

For automatic activation based on query stats:

```sql
SET automatic_parallel_replicas_mode = 1;  -- requires enable_analyzer = 1
```

## Validation

```sql
-- Did this query actually go parallel across replicas?
EXPLAIN PIPELINE
SELECT count() FROM events WHERE event_time >= now() - INTERVAL 30 DAY
SETTINGS enable_parallel_replicas = 1;
-- Look for "ReadFromRemoteParallelReplicas" or similar in the plan
```

```sql
-- Per-query: which replica did what
SELECT
    event_time, query, query_duration_ms,
    peak_threads_usage,                       -- threads on the initiator
    ProfileEvents['DistributedConnectionTries'] AS replicas_tried,
    initial_user, hostname()
FROM system.query_log
WHERE event_time >= now() - INTERVAL 1 HOUR
  AND query ILIKE 'SELECT%'
ORDER BY query_duration_ms DESC LIMIT 20;
```

## Pitfalls

- **Forgetting `enable_analyzer = 1`.** Parallel replicas requires the new analyzer.
- **Using `FINAL` or projections.** The query silently runs serially without complaining loudly. Check `EXPLAIN PIPELINE` to be sure.
- **Treating it as a free lunch.** Coordination costs are real. For a 100ms query, the spin-up wins back nothing.
- **Mixing parallel replicas with a query cache hit.** The hit returns immediately from the initiator; parallel replicas would have done less work but it doesn't matter.

## How this project tests it

There's no dedicated comparison yet. To validate, run the project's heaviest scan with and without parallel replicas:

```bash
# 1. Seed enough data
uv run clickhouse-bench seed --scale 100000

# 2. Compare an aggregation with and without parallel replicas
uv run clickhouse-bench benchmark --category aggregations
# Then re-run with `SET enable_parallel_replicas = 1` in the query
```

Compare `query_duration_ms` and `read_rows` from `system.query_log` between the two runs.

## Sources

- ClickHouse, *Parallel Replicas*. <https://clickhouse.com/docs/deployment-guides/parallel-replicas>
- ClickHouse, *How ClickHouse Executes a Query in Parallel*. <https://clickhouse.com/docs/optimize/query-parallelism>
