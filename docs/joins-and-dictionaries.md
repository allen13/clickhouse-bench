# 8. Joins and dictionaries

> **Tier 1–2 — Critical to get right.** ClickHouse's default `parallel_hash` join loads the right side into memory. Wrong table on the right OOMs. Pre-filter both sides, pick the algorithm to match table sizes, or replace the join entirely with a dictionary lookup.

## The two questions to answer first

Before tuning a join, ask:

1. **Can this be a dictionary lookup?** If the right side is a small dimension table that changes slowly, the answer is almost always yes — and the [docs benchmark shows 56% faster, 82% less RAM than `JOIN`](https://clickhouse.com/docs/dictionary).
2. **Can this be denormalized at insert time?** A materialized view (see [materialized-views.md](materialized-views.md)) that pre-joins on insert eliminates the read-time cost entirely.

If neither fits, *then* tune the join.

## Replace the join with a dictionary

Per `query-join-consider-alternatives`. A dictionary is an in-memory key→attributes mapping that ClickHouse keeps loaded per node. `dictGet()` calls hit the **direct join** algorithm — the fastest path.

```sql
CREATE DICTIONARY customer_dict (
    id UInt64,
    name String,
    email String,
    segment LowCardinality(String)
)
PRIMARY KEY id
SOURCE(CLICKHOUSE(TABLE 'customers'))
LAYOUT(HASHED())
LIFETIME(MIN 300 MAX 360);  -- refresh window in seconds

-- Replaces a JOIN to customers
SELECT
    order_id,
    dictGet('customer_dict', 'name', customer_id)  AS customer_name,
    dictGet('customer_dict', 'email', customer_id) AS customer_email
FROM orders
WHERE created_at > '2024-01-01';

-- Default value if key missing
SELECT dictGetOrDefault('customer_dict', 'name', customer_id, 'unknown') FROM orders;
```

### Dictionary layouts

| Layout | Key | Best for |
|---|---|---|
| `FLAT` | `UInt64` | Very small dicts (≤500K keys); fastest lookup |
| `HASHED` | `UInt64` | Tens of millions of keys; O(1) lookup |
| `SPARSE_HASHED` | `UInt64` | RAM-constrained; trades CPU for memory |
| `COMPLEX_KEY_HASHED` | composite (tuple) | Multi-column lookup keys |

### When a dictionary is wrong

- The "dimension" is actually a fact table (millions of rows changing constantly). Dicts live entirely in RAM per node.
- You need full-relational semantics (multi-row matches, complex predicates beyond key equality). Use a `JOIN`.
- Source has duplicate keys — **dictionaries silently dedupe**, keeping the last value. Only safe when source keys are unique.
- **Caveat:** `dictGet()` in a query body **disables the query cache** for that query.

## When you must use `JOIN`

### Pick the right algorithm

Per `query-join-choose-algorithm`. ClickHouse's join algorithms each pick a different point on the memory/CPU curve:

| Algorithm | Best for | Trade-off |
|---|---|---|
| `parallel_hash` (default since 24.11) | Small-to-medium right side that fits in RAM | Fast, multi-threaded build |
| `hash` | General purpose, all join types | Single-threaded build |
| `direct` | Right side is a `Dictionary` | No hash table to build; fastest |
| `full_sorting_merge` | Both sides already sorted on join key | Skips sort if pre-ordered; low memory |
| `partial_merge` | Large/large, memory-constrained | Lower memory; slower |
| `grace_hash` | Very large right side | Spills to disk |
| `auto` | Adaptive | Tries hash, falls back under pressure |

```sql
SET join_algorithm = 'auto';                  -- adaptive
SET join_algorithm = 'partial_merge';         -- large-to-large with memory cap
SET join_algorithm = 'full_sorting_merge';    -- both sides pre-sorted on key
```

**Right-side rule.** ClickHouse 24.12+ auto-positions the smaller table on the right. On older versions, **always put the smaller table on the right** — it's the side that gets hashed into memory.

### Filter before joining <a name="algorithms"></a>

Per `query-join-filter-before`. Joining full tables and then filtering wastes work. The optimiser sometimes pushes filters down, but don't depend on it — restructure as subqueries when in doubt:

```sql
-- Wrong: joins both whole tables, then filters
SELECT o.order_id, c.name, o.total
FROM orders o
JOIN customers c ON c.id = o.customer_id
WHERE o.created_at > '2024-01-01' AND c.country = 'US';

-- Right: filter both sides first
SELECT o.order_id, c.name, o.total
FROM (
    SELECT order_id, customer_id, total FROM orders
    WHERE created_at > '2024-01-01'
) o
JOIN (
    SELECT id, name FROM customers WHERE country = 'US'
) c ON c.id = o.customer_id;

-- Even better: aggregate first when the result is grouped
SELECT c.country, sum(o.total_revenue)
FROM (
    SELECT customer_id, sum(total) AS total_revenue
    FROM orders
    WHERE created_at > '2024-01-01'
    GROUP BY customer_id
) o
JOIN customers c ON c.id = o.customer_id
GROUP BY c.country;
```

### `LEFT ANY JOIN` when one match is enough

Per `query-join-use-any`. A regular `LEFT JOIN` returns one row per match — multiple matches multiply rows, multiplying memory. `LEFT ANY JOIN` returns at most one match per left row.

```sql
-- Returns first matching customer per order; less memory, faster
SELECT o.order_id, c.name
FROM orders o
LEFT ANY JOIN customers c ON c.id = o.customer_id;
```

| Variant | Behaviour |
|---|---|
| `LEFT ANY JOIN` | At most one match from right per left row |
| `INNER ANY JOIN` | Only matched rows; one per left row |
| `RIGHT ANY JOIN` | At most one match from left per right row |

### `join_use_nulls`

Per `query-join-null-handling`. `LEFT JOIN` non-matches default to `NULL` for right-side columns. With `join_use_nulls = 0` (default in many configs is 1; check yours), unmatched rows get the *type's default value* (empty string, 0) instead — no `Nullable` wrapper, lower memory.

```sql
SET join_use_nulls = 0;
SELECT o.order_id, c.name FROM orders o LEFT JOIN customers c ON c.id = o.customer_id;
-- Non-matching rows: name = '' (instead of NULL)
```

| Setting | Behaviour | Use when |
|---|---|---|
| `join_use_nulls = 0` | Defaults for non-matches | You can handle defaults |
| `join_use_nulls = 1` | NULL for non-matches | You need to distinguish "no match" from "matched-with-default" |

## Validation

```sql
-- See which algorithm was used
EXPLAIN PIPELINE SELECT … JOIN …;
-- Look for "JoiningTransform" with the algorithm name

-- Memory used by the join
SELECT
    query,
    formatReadableSize(memory_usage) AS mem,
    query_duration_ms
FROM system.query_log
WHERE has(tables, 'orders') AND has(tables, 'customers')
  AND type = 'QueryFinish'
ORDER BY event_time DESC LIMIT 5;
```

## Pitfalls

- **Right side too big to fit in RAM with `parallel_hash`.** OOM. Switch to `partial_merge` or `grace_hash`, or filter the right side, or replace with a dictionary.
- **Joining on `String` when both sides have `LowCardinality(String)`.** Forces materialisation. Match the types.
- **Cross joins by accident.** A missing `ON` or a non-equi-join collapses to cross. Always check the join key.
- **Multiple `JOIN`s on the same key.** Stage them in subqueries; intermediate result widths matter for memory.

## How this project tests it

The benchmark suite in `src/queries.py` includes the **Joins** category — two- and three-table join queries against the seeded `users` / `orders` / `events` data. There's no dedicated `compare-features` category for join algorithm choice yet; the place to validate is `EXPLAIN PIPELINE` plus `system.query_log` after running `uv run clickhouse-bench benchmark`.

A useful follow-up is to add a `query-join-vs-dict` comparison that materializes a `customer_dict` and re-runs the joins via `dictGet()`.

## Sources

- ClickHouse, *Minimize and Optimize JOINs*. <https://clickhouse.com/docs/best-practices/minimize-optimize-joins>
- ClickHouse, *Speeding Up Joins Using a Dictionary*. <https://clickhouse.com/docs/dictionary>
- ClickHouse, *Hashed Dictionary Layouts*. <https://clickhouse.com/docs/sql-reference/statements/create/dictionary/layouts/hashed>
- ClickHouse, *Functions for Working with Dictionaries*. <https://clickhouse.com/docs/sql-reference/functions/ext-dict-functions>
- Project skill rules: `query-join-choose-algorithm`, `query-join-consider-alternatives`, `query-join-filter-before`, `query-join-null-handling`, `query-join-use-any`
