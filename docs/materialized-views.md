# 7. Materialized views

> **Tier 2 — High.** The single biggest win for repeated dashboard queries. An incremental MV runs the aggregation at insert time and writes results to a target table — dashboard queries then read thousands of pre-aggregated rows instead of billions.

## What they are

A materialized view in ClickHouse is **a trigger that runs on inserts to a source table** and writes the result of a `SELECT` to a target table. Two flavours:

1. **Incremental MV** — fires on each insert. Tiny per-insert overhead. Stale only by the most-recent block. Best for real-time dashboards.
2. **Refreshable MV** — re-runs the full query on a schedule and overwrites the target. Best for complex multi-table joins or sub-millisecond dashboards where minor staleness is acceptable.

## Incremental MVs (real-time aggregation)

Per `query-mv-incremental`. The pattern:

```sql
-- 1. Target table for aggregated state
CREATE TABLE events_hourly (
    event_type   LowCardinality(String),
    hour         DateTime,
    events       AggregateFunction(count),
    unique_users AggregateFunction(uniq, UInt64)
)
ENGINE = AggregatingMergeTree()
ORDER BY (event_type, hour);

-- 2. MV that populates the target on every insert into events
CREATE MATERIALIZED VIEW events_hourly_mv TO events_hourly AS
SELECT
    event_type,
    toStartOfHour(timestamp) AS hour,
    countState()             AS events,
    uniqState(user_id)       AS unique_users
FROM events
GROUP BY event_type, hour;

-- 3. Read from the target with -Merge functions
SELECT
    event_type, hour,
    countMerge(events)       AS events,
    uniqMerge(unique_users)  AS unique_users
FROM events_hourly
WHERE hour >= now() - INTERVAL 7 DAY
GROUP BY event_type, hour;
```

### Key rules

- Use **`-State` aggregate functions in the MV** (`countState`, `uniqState`, `sumState`, `avgState`).
- Use **`-Merge` functions in the read query** (`countMerge`, `uniqMerge`, `sumMerge`, `avgMerge`).
- The target engine should be `AggregatingMergeTree` so partial states merge correctly during background merges.

### MV is incremental, not retroactive

A new MV does **not** see existing data — only inserts that arrive after creation. To backfill:

```sql
-- 1. Drop and recreate with POPULATE for small tables
CREATE MATERIALIZED VIEW … POPULATE AS SELECT … ;
-- (Caveat: while POPULATE runs, new inserts to source aren't seen)

-- 2. Or backfill manually for production safety:
INSERT INTO events_hourly
SELECT
    event_type, toStartOfHour(timestamp), countState(), uniqState(user_id)
FROM events
WHERE timestamp < (SELECT min(timestamp) FROM events_hourly_mv_already_seeing)
GROUP BY 1, 2;
```

The manual backfill is safer in production — `POPULATE` blocks new MV updates until backfill finishes.

## Refreshable MVs (scheduled batch)

Per `query-mv-refreshable`. The MV re-runs the full `SELECT` on a schedule and replaces (or appends to) the target.

```sql
CREATE MATERIALIZED VIEW orders_denormalized
REFRESH EVERY 5 MINUTE
ENGINE = MergeTree()
ORDER BY (created_at, order_id)
AS SELECT
    o.order_id, o.created_at, o.total,
    c.name AS customer_name, c.segment,
    p.name AS product_name
FROM orders o
JOIN customers c ON o.customer_id = c.id
JOIN products p ON o.product_id = p.id
WHERE o.created_at >= now() - INTERVAL 1 DAY;

-- Reads are sub-millisecond
SELECT * FROM orders_denormalized WHERE segment = 'enterprise';
```

### `REPLACE` vs `APPEND`

| Mode | Behaviour | Use case |
|---|---|---|
| `REPLACE` (default) | Overwrites on each refresh | Current-state lookup tables |
| `APPEND` | Adds new rows | Historical snapshots, slowly-changing dimensions |

### Critical timing rule

The query must run faster than the refresh interval. `REFRESH EVERY 10 SECOND` for a 30-second query queues runs and falls behind. Choose intervals that leave headroom (5× rule of thumb).

## When to use which

| Need | Use |
|---|---|
| Real-time dashboard with live data | Incremental MV |
| Sub-ms reads, minor staleness ok | Refreshable MV |
| Aggregation over single source table | Incremental MV |
| Multi-table join feeding the result | Refreshable MV |
| Pre-compute "top N" lists | Refreshable MV |
| Streaming counters | Incremental MV with `AggregatingMergeTree` |

## Failure modes

- **Schema drift on the target.** If you `ALTER` the source's columns, the MV may break. Plan migrations: drop MV, alter source, recreate MV.
- **Heavy MV `SELECT` slows inserts.** Each insert pays the MV's `GROUP BY` cost. Keep MV queries simple.
- **Forgetting `-State` / `-Merge`.** Storing raw `count()` instead of `countState()` in an `AggregatingMergeTree` returns wrong results after merges collapse rows.
- **Cascading MVs.** An MV's target can be the source for another MV. Possible but harder to reason about — keep cascade depth ≤2.

## Validation

```sql
-- Confirm the MV is firing
SELECT
    name,
    formatReadableSize(data_compressed_bytes) AS target_size,
    rows
FROM system.parts
WHERE active AND database = currentDatabase() AND table = 'events_hourly';

-- Inspect MV definition
SELECT name, as_select
FROM system.tables
WHERE engine = 'MaterializedView'
  AND database = currentDatabase();
```

```sql
-- Compare raw vs MV latency
SELECT count() FROM events
WHERE event_time >= now() - INTERVAL 7 DAY;
-- vs
SELECT countMerge(events) FROM events_hourly
WHERE hour >= now() - INTERVAL 7 DAY;
```

## How this project tests it

`compare-features --comparison materialized_views` builds a raw `events` source table plus an MV-backed `cmp_events_mv_target` populated by a `daily event count` MV. Expected: the MV-backed query is near-instant regardless of source size, while the raw query scales with rows scanned.

```bash
uv run clickhouse-bench compare-features --comparison materialized_views
```

## Sources

- ClickHouse, *Use Materialized Views*. <https://clickhouse.com/docs/best-practices/use-materialized-views>
- Project skill rules: `query-mv-incremental`, `query-mv-refreshable`
- Compare with [projections.md](projections.md) for in-table pre-aggregation.
