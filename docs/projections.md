# 6. Projections

> **Tier 2 — High.** A projection is an alternate sort or pre-aggregation of the same table, stored *inside* each part directory. The optimiser rewrites matching queries to use it. Easier than a materialized view (no separate target table), atomically consistent with the parent.

## What they are

A projection is a sub-MergeTree that lives inside each part of the parent table. Two flavours:

1. **Sort-order projection** — re-sorts the same columns by a different key. Lets a query that filters on a non-leading PK column hit a projection sorted by that column instead of full-scanning.
2. **Aggregating projection** — pre-computes `GROUP BY` aggregations. Lets a dashboard query read pre-aggregated rows instead of billions of raw rows.

When a query arrives, the optimiser:

1. Checks each projection: can it produce the same result?
2. Picks the projection with the fewest granules to read.
3. Routes the query to that projection. For parts created *before* the projection was added, ClickHouse can materialize the projection on-the-fly or fall back to the base table.

## Projection vs materialized view

Both pre-compute results. The differences matter:

| Dimension | Projection | Materialized view |
|---|---|---|
| Storage location | Inside each part directory | Separate independent table |
| Consistency | Atomic (same merge as parent) | Eventual (separate insert pipeline) |
| Query rewrite | Automatic by optimiser | Manual (query must target the MV) |
| DDL | Single table definition | Two tables + trigger |
| Backfill of existing data | `MATERIALIZE PROJECTION` rebuilds in place | `INSERT … SELECT` from source |
| `FINAL` support | ❌ no | ✅ yes |
| Parallel replicas | ❌ incompatible | ✅ compatible |

**Choose projection when:**

- The aggregation/sort is tightly coupled to one base table.
- You want the optimiser to pick automatically for matching queries.
- Atomic consistency matters (no eventual-consistency window).

**Choose materialized view when:**

- You need joins or filtering across multiple sources.
- Different consumers query different shapes that don't all match a single projection.
- You need `REFRESH EVERY ...` (refreshable MV) for batch workflows.

## Syntax

```sql
-- At table creation
CREATE TABLE events (
    id UInt64,
    user_id UInt64,
    event_type LowCardinality(String),
    event_time DateTime,
    PROJECTION daily_event_counts (
        SELECT
            toDate(event_time) AS day,
            event_type,
            count() AS cnt
        GROUP BY day, event_type
    )
) ENGINE = MergeTree() ORDER BY (user_id, event_time);

-- Add to existing table
ALTER TABLE events ADD PROJECTION agg_by_country (
    SELECT country, sum(revenue), count()
    GROUP BY country
);

-- Build the projection for parts that pre-date it
ALTER TABLE events MATERIALIZE PROJECTION agg_by_country;

-- Drop a projection
ALTER TABLE events DROP PROJECTION agg_by_country;
```

A query that matches the projection's shape is automatically rewritten:

```sql
-- Hits daily_event_counts projection — reads pre-aggregated rows
SELECT toDate(event_time) AS day, event_type, count()
FROM events
GROUP BY day, event_type;
```

## Forcing projection use (CI/regression)

`force_optimize_projection = 1` makes a query throw if no projection can be used. Useful in CI to assert a critical dashboard query keeps hitting its projection after refactors:

```sql
SET force_optimize_projection = 1;
SELECT toDate(event_time), event_type, count()
FROM events GROUP BY 1, 2;
-- Errors if no projection matched
```

## Cost: maintenance and merge I/O

Every part directory carries a sub-directory per projection. Inserts and merges write/rewrite the projection alongside the base data. Trade-offs:

- **Disk:** projection is typically a small fraction of base table size (often <10%), but it's not free.
- **Insert latency:** marginal — the projection's `GROUP BY` runs on the inserted block.
- **Merge cost:** projection parts merge in lockstep with parent parts.

Don't add projections you don't need. Start without; add one when a specific high-value query shape proves itself.

## Failure modes

- **Cannot use with `FINAL`.** Queries using `SELECT ... FINAL` (e.g., on `ReplacingMergeTree`) bypass projections.
- **Cannot use with parallel replicas.** Projections and `enable_parallel_replicas = 1` are mutually exclusive — see [parallel-replicas.md](parallel-replicas.md). On Cloud's multi-replica services this is a real choice.
- **Optimiser must prove equivalence.** Subtle query rewrites, non-deterministic functions, or filters that don't align with the projection's `GROUP BY` keys will skip the projection silently. Use `force_optimize_projection = 1` in tests to catch this.
- **Materializing on a huge existing table is heavy.** `MATERIALIZE PROJECTION` rebuilds the projection for every existing part; budget I/O.

## Validation

```sql
-- Did the optimiser use a projection for this query?
EXPLAIN PIPELINE
SELECT toDate(event_time), event_type, count()
FROM events GROUP BY 1, 2;
-- Look for "ReadFromMergeTree" with the projection's name in the plan

EXPLAIN indexes = 1 SELECT ... ;
-- "Selected N parts ... using projection: <name>"
```

```sql
-- Inventory projections per table
SELECT
    database, table, name AS projection_name,
    formatReadableSize(data_compressed_bytes) AS proj_size
FROM system.projection_parts
WHERE active AND database = currentDatabase()
ORDER BY data_compressed_bytes DESC;
```

## How this project tests it

`compare-features --comparison projections` builds two `events` variants — with and without an `aggregating projection daily_event_counts` — and runs the matching query against both. Expected: the projected variant resolves directly from pre-aggregated parts (10–100× faster).

```bash
uv run clickhouse-bench compare-features --comparison projections
```

## Sources

- ClickHouse, *MergeTree — Projections*. <https://clickhouse.com/docs/en/engines/table-engines/mergetree-family/mergetree#projections>
- See also: [materialized-views.md](materialized-views.md) for the alternative pre-aggregation pattern.
