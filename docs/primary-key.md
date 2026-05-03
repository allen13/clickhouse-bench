# 1. `ORDER BY` and the sparse primary index

> **Tier 1 — Critical.** The sparse index is the only mechanism that lets ClickHouse skip data. Wrong key → full scan, every query, forever (the key is immutable after table creation).

## What it is

In ClickHouse, **`ORDER BY` is the primary key.** It controls:

1. **Physical row order on disk** — within each part, rows are sorted by the `ORDER BY` tuple.
2. **The sparse primary index** — a flat in-memory array with **one mark per granule** (8192 rows by default), not per row. This is why the entire index for an 8.87 M-row table fits in ~97 KiB ([sparse primary indexes guide](https://clickhouse.com/docs/en/guides/best-practices/sparse-primary-indexes)).
3. **Compression effectiveness** — sorted data compresses dramatically better. The same docs page shows one column going from 11.24 MiB to 877 KiB just by reversing key column order.

ClickHouse's index is **not a B-tree**. A B-tree per-row index at OLAP scale (billions of rows, millions of inserts/sec) would crush memory and rebalance cost. The sparse design trades per-row precision for a tiny index that can identify the candidate granule range with a binary search.

## How a lookup actually works

1. Binary-search the in-memory index → identifies a contiguous range of candidate granules.
2. For multi-column keys, **generic exclusion search** narrows the range using subsequent key columns. This step is efficient only when *preceding* key columns have low cardinality — otherwise every granule looks unique and the search degenerates.
3. Mark files (`*.mrk`) translate granule numbers to compressed-block offsets and intra-block positions, so ClickHouse seeks directly to the matching bytes.
4. Decompress those granules; scan rows; apply the rest of the `WHERE`.

The takeaway: **the index can skip whole blocks; it cannot point at individual rows.** Putting a high-cardinality column first defeats the skip mechanism completely.

## The four rules that matter

Per the project's `clickhouse-best-practices` skill:

### `schema-pk-plan-before-creation` (Critical)

`ORDER BY` cannot be modified after table creation. `ALTER TABLE … MODIFY ORDER BY` errors. Wrong choice → full data migration to a new table.

**Pre-creation checklist:**
- List your top 5–10 query patterns and their `WHERE` clauses.
- Identify columns that exclude the most rows (most selective).
- 4–5 key columns is typically enough.

### `schema-pk-cardinality-order` (Critical)

Order columns **low cardinality first**, high cardinality last.

```sql
-- Wrong: UUID first defeats the index
ORDER BY (event_id, event_type, timestamp)

-- Right: low-cardinality first enables granule skipping
ORDER BY (event_type, event_date, event_id)
```

| Position | Cardinality | Examples |
|---|---|---|
| 1st | Low | `event_type`, `status`, `country`, `tenant_id` |
| 2nd | Date (coarse) | `toDate(timestamp)` |
| 3rd+ | Medium-high | `user_id`, `session_id` |
| Last | High (if needed) | `event_id`, `uuid` |

**Tip from the skill:** use `toDate(timestamp)` instead of raw `DateTime` when day-level filtering suffices — shrinks the index entry from 32-bit to 16-bit.

### `schema-pk-prioritize-filters` (Critical)

The `ORDER BY` must include the columns your queries filter on. If 60% of queries are `WHERE tenant_id = ?`, `tenant_id` must be in `ORDER BY`. Otherwise it's a full scan, every time.

### `schema-pk-filter-on-orderby` (Critical)

The query side of the rule: filters must use the `ORDER BY` **prefix**, in order, or the index won't be used.

```sql
-- Given: ORDER BY (tenant_id, event_type, timestamp)
WHERE tenant_id = 123                                       -- ✅ uses index
WHERE tenant_id = 123 AND event_type = 'click'              -- ✅ uses index
WHERE tenant_id = 123 AND event_type = 'click' AND ts > X   -- ✅ uses index (range on suffix)
WHERE event_type = 'click'                                  -- ❌ skipped prefix → no index
WHERE timestamp > X                                         -- ❌ no prefix at all → full scan
```

## Validation

Always confirm with `EXPLAIN`:

```sql
EXPLAIN indexes = 1
SELECT * FROM events WHERE tenant_id = 123;
-- Look for: Keys: [tenant_id, ...]; Granules: <selected>/<total>

EXPLAIN ESTIMATE
SELECT * FROM events WHERE tenant_id = 123;
-- Returns rows / marks / parts the query will actually read
```

Compare `Granules: 12/8000` (good) to `Granules: 8000/8000` (no skipping happened).

For per-query inspection of cardinality assumptions:

```sql
SELECT
    name,
    type,
    uniqExact(...) AS cardinality
FROM ... -- inspect each candidate column on a sample
```

## Common pitfalls

- **High-cardinality first.** Putting `event_id`/`UUID` first nullifies the index. If you need fast lookups on a high-cardinality column, use a **skip index** (`bloom_filter`) on it — don't promote it to the PK.
- **More than 4–5 columns in `ORDER BY`.** Diminishing returns; later columns rarely contribute to skipping. Keep the key tight.
- **Including columns "just in case".** Each extra key column costs memory in the index and complicates the key prefix rule.
- **Conflating `ORDER BY` and `PRIMARY KEY`.** They can differ — `PRIMARY KEY` is a prefix of `ORDER BY`. Useful for `ReplacingMergeTree` where you need a sort key longer than the de-dup key.

## How this project tests it

The `compare-features` runner (`src/schema_variants.py`) builds three variants of the same `events` table:

- `ORDER BY (user_id, event_time)`
- `ORDER BY (event_time, user_id)`
- `ORDER BY (event_type, event_time, user_id)`

…then runs four query shapes (`by_user`, `by_time`, `by_type`, `user_and_time`) against all three. The result table makes it impossible to argue about which key is best — you can see the latency cells side-by-side.

Run it once the toolkit is connected:

```bash
uv run clickhouse-bench compare-features --comparison ordering
```

## Sources

- ClickHouse, *A Practical Introduction to Sparse Primary Indexes*. <https://clickhouse.com/docs/en/guides/best-practices/sparse-primary-indexes>
- ClickHouse, *Choosing a Primary Key*. <https://clickhouse.com/docs/best-practices/choosing-a-primary-key>
- Project skill rules: `schema-pk-plan-before-creation`, `schema-pk-cardinality-order`, `schema-pk-prioritize-filters`, `schema-pk-filter-on-orderby`
