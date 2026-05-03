# 5. Skip (data-skipping) indexes

> **Tier 2 — High when the workload calls for them.** Skip indexes let ClickHouse rule out granules without reading them — but only for filters on columns *not* in `ORDER BY`. They are not a substitute for a good primary key.

## What they are

A skip index stores per-granule metadata (a Bloom filter, a value set, a min/max pair). When a query filters on the indexed column, ClickHouse consults the metadata first and **skips granules that definitely can't match**. False positives cost you a granule read; false negatives are impossible.

Per `query-index-skipping-indices`, the docs report up to **60× faster queries** when the index lets ClickHouse skip most granules.

## When skip indexes help

Per the rule: skip indexes should be considered **after** optimising data types, primary key selection, and materialized views — not as a first move.

**Use when:**

- Column has high overall cardinality but low cardinality *within blocks* (the values cluster).
- Rare values are the search target (error codes, specific session IDs).
- The column correlates with the primary key (so when you skip a granule by PK, you'd skip the same granule by this column).

**Don't use when:**

- It's your first optimisation step — fix the primary key first.
- Matching values are scattered evenly across all granules (no granule can be skipped → just adds overhead).
- You haven't tested on real data — synthetic uniform distributions hide the correlation that makes skip indexes work.

## Index types

| Type | Best for | Example filter |
|---|---|---|
| `bloom_filter(p)` | Equality on **high-cardinality** columns (UUIDs, session IDs) | `WHERE session_id = 'abc'` |
| `set(N)` | Low-cardinality; matches when N distinct values fit in the set | `WHERE status IN ('a','b')` |
| `minmax` | Range or equality on monotonic-ish columns | `WHERE amount > 1000`, `WHERE event_time BETWEEN ...` |
| `ngrambf_v1(n, …)` | Substring search (`LIKE '%term%'`) on text | `WHERE text LIKE '%error%'` |
| `tokenbf_v1(...)` | Token search (`hasToken`) | `WHERE hasToken(text, 'word')` |

The `p` for `bloom_filter` is the false-positive rate (typical 0.01). Lower → larger index → fewer false positives. The `N` for `set` is the max set size before the index gives up on a granule.

## Adding a skip index

```sql
-- At table creation
CREATE TABLE events (
    user_id UInt64,
    event_type LowCardinality(String),
    session_id String,
    event_time DateTime,
    INDEX idx_session session_id TYPE bloom_filter(0.01) GRANULARITY 4
)
ENGINE = MergeTree() ORDER BY (user_id, event_time);

-- On an existing table
ALTER TABLE events
    ADD INDEX idx_session session_id TYPE bloom_filter(0.01) GRANULARITY 4;

-- Materialise for existing parts (new parts include it automatically)
ALTER TABLE events MATERIALIZE INDEX idx_session;
```

The `GRANULARITY` parameter controls how many primary-index granules each skip-index entry covers. Higher granularity → smaller index, coarser skipping. `4` is a reasonable starting default.

## Validation

```sql
-- Confirm the index is being consulted and how many granules it skips
EXPLAIN indexes = 1
SELECT count() FROM events WHERE session_id = 'abc';
-- Look for the skip index name and "Granules: <selected>/<total>"

-- Marks/parts the query will read
EXPLAIN ESTIMATE
SELECT count() FROM events WHERE session_id = 'abc';
-- Compare with and without the index to prove the win
```

```sql
-- Inspect skip-index metadata size
SELECT name, type, expr, granularity,
       formatReadableSize(data_compressed_bytes) AS size
FROM system.data_skipping_indices
WHERE database = currentDatabase() AND table = 'events';
```

## Pitfalls

- **Bloom filter on a low-cardinality column.** The filter is full of bits set; everything looks possible. Use `set(N)` instead for low-cardinality columns.
- **`set(N)` with `N` smaller than actual cardinality per block.** When the set fills up, the index gives up on that granule (returns "could match"). Pick `N` larger than the per-granule distinct count.
- **`minmax` on a uniformly distributed numeric column.** Each granule's min/max spans most of the range → no skipping. Useful only when values cluster.
- **Adding indexes for columns rarely used in `WHERE`.** Index maintenance happens on every merge. Cost without benefit.
- **Forgetting `MATERIALIZE INDEX`.** New parts get the index, old parts don't, until you materialise.

## How this project tests it

`compare-features --comparison skip_indexes` runs four `events` variants — none, `bloom_filter` on `session_id`, `set(100)` on `event_type`, `minmax` on `event_time` — against the same three filter shapes. The result table makes it obvious which index helps which query.

```bash
uv run clickhouse-bench compare-features --comparison skip_indexes
```

## Sources

- ClickHouse, *Use Data Skipping Indices Where Appropriate*. <https://clickhouse.com/docs/best-practices/use-data-skipping-indices-where-appropriate>
- Project skill rule: `query-index-skipping-indices`
