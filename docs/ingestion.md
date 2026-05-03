# 9. Ingestion — batching, async inserts, mutations, `OPTIMIZE`

> **Tier 1 — Critical.** ClickHouse's read performance assumes the write side behaves: **batch inserts, no mutations, no `OPTIMIZE FINAL`.** Get this wrong and the cluster destabilises long before you notice slow reads.

## Each `INSERT` creates a part

Every `INSERT` produces one new data part on disk. Background merges combine parts over time. Single-row inserts produce thousands of tiny parts, exceeding `parts_to_throw_insert` and triggering `Too many parts` errors. The cluster's merge throughput cannot keep up.

Batch in chunks of **10,000 to 100,000 rows** per `INSERT` — the sweet spot per `insert-batch-size`.

| Threshold | Value |
|---|---|
| Minimum | 1,000 rows per insert |
| Ideal range | 10,000 – 100,000 rows |
| Insert frequency | ≈1 insert per second per writer |

```python
# Wrong — one part per row
for event in events:
    client.insert("events", [event])

# Right — 10K rows per part
for batch in chunks(events, 10_000):
    client.insert("events", batch)
```

### Validate part count

```sql
-- More than ~3,000 active parts on a single table is a warning sign
SELECT
    table,
    count()    AS parts,
    sum(rows)  AS total_rows
FROM system.parts
WHERE active AND database = currentDatabase()
GROUP BY table
ORDER BY parts DESC;
```

## Use the native format

Per `insert-format-native`. Format choice affects parse cost:

| Format | Use |
|---|---|
| `Native` (recommended) | Column-oriented binary; minimal parsing |
| `RowBinary` | Efficient row-oriented alternative |
| `JSONEachRow` | Easy but expensive to parse — last resort |

```python
client.insert("events", batch, settings={"input_format": "Native"})
```

## Async inserts <a name="async-inserts"></a>

Per `insert-async-small-batches`. When client-side batching isn't practical (observability agents, IoT devices, hundreds of independent writers), async inserts shift batching to the server. ClickHouse buffers incoming rows in memory per (query shape + settings) and flushes when a threshold is crossed.

```sql
-- Per-user defaults (Cloud-friendly)
ALTER USER my_app_user SETTINGS
    async_insert = 1,
    wait_for_async_insert = 1,                  -- wait for flush; durable
    async_insert_max_data_size = 10000000,      -- 10 MB buffer cap
    async_insert_busy_timeout_ms = 1000;        -- 1s timeout
```

### Flush triggers (first one wins)

| Setting | Default (Cloud) |
|---|---|
| `async_insert_max_data_size` | 100 MiB |
| `async_insert_busy_timeout_ms` | 1,000 ms |
| `async_insert_max_query_number` | 450 |

Since 24.2, adaptive timeouts auto-tune between 50–200 ms based on incoming rate.

### Durability — pick one

| Setting | Behaviour | Use case |
|---|---|---|
| `wait_for_async_insert = 1` (default) | Client waits for flush | **Recommended** — survives crashes |
| `wait_for_async_insert = 0` | Fire-and-forget | Risky — silent data loss on crash before flush |

> "We strongly recommend using `async_insert = 1, wait_for_async_insert = 1`." — ClickHouse docs

### Dedup for retries

```sql
SET async_insert_deduplicate = 1;  -- enable for idempotent retries
```

### Monitor

```sql
SELECT * FROM system.asynchronous_insert_log ORDER BY event_time DESC LIMIT 20;
SELECT * FROM system.asynchronous_inserts;  -- live buffer state
```

## Avoid mutations <a name="mutations"></a>

Mutations (`ALTER TABLE … UPDATE/DELETE`) **rewrite whole parts**, even for one-row changes. They are asynchronous, can't be rolled back, and starve merges of I/O while running. Avoid in steady-state operations.

### `ALTER TABLE … UPDATE` → `ReplacingMergeTree`

Per `insert-mutation-avoid-update`. Insert the new version of the row instead of updating in place. ClickHouse keeps the latest version after merges.

```sql
CREATE TABLE users (
    user_id    UInt64,
    name       String,
    status     LowCardinality(String),
    updated_at DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY user_id;

-- "Update" by inserting a new row
INSERT INTO users VALUES (123, 'John', 'inactive', now());

-- Read latest with FINAL
SELECT * FROM users FINAL WHERE user_id = 123;
-- Or with argMax (avoids FINAL's read penalty)
SELECT user_id, argMax(status, updated_at) AS status
FROM users WHERE user_id = 123 GROUP BY user_id;
```

### `ALTER TABLE … DELETE` → lightweight `DELETE`, `DROP PARTITION`, or `CollapsingMergeTree`

Per `insert-mutation-avoid-delete`. Choose by frequency:

| Need | Use |
|---|---|
| Bulk time-based cleanup | `ALTER TABLE … DROP PARTITION '202301'` (instant) |
| Occasional deletes | `DELETE FROM …` (lightweight, marks rows; physical delete on next merge) |
| Frequent soft-delete pattern | `CollapsingMergeTree(sign)` |
| Rare data corrections | `ALTER TABLE … DELETE WHERE …` (mutation; expensive) |

```sql
-- Lightweight DELETE — marks rows, doesn't rewrite immediately
DELETE FROM orders WHERE status = 'cancelled';

-- DROP PARTITION — instant metadata-only operation
ALTER TABLE events DROP PARTITION '202301';

-- CollapsingMergeTree — for frequent insert/delete cycles
CREATE TABLE orders (
    order_id UInt64, total Decimal(10, 2),
    sign Int8  -- +1 = active, -1 = deleted
)
ENGINE = CollapsingMergeTree(sign)
ORDER BY order_id;
```

## Don't run `OPTIMIZE TABLE … FINAL`

Per `insert-optimize-avoid-final`. `OPTIMIZE FINAL` forces an immediate merge of all parts in each partition, **bypassing the 150 GB part-size guard**. On large tables this causes OOMs, hours of disk I/O, and merge stalls.

| Mistake | Fix |
|---|---|
| `OPTIMIZE TABLE … FINAL` after every batch insert | Remove. Background merges already do this. |
| Cron job that periodically runs `OPTIMIZE … FINAL` | Remove. |
| `OPTIMIZE FINAL` to deduplicate `ReplacingMergeTree` | Use `SELECT … FINAL` or `argMax()` in the read path |

`OPTIMIZE FINAL` is occasionally acceptable as a one-off (freezing data before export, end-of-life table archive) — never as a recurring step.

> Note: `SELECT … FINAL` on `ReplacingMergeTree` is **different** from `OPTIMIZE … FINAL` and is fine to use. The problem is the `OPTIMIZE` form.

## Validation

```sql
-- Mutation queue (active and pending)
SELECT
    database, table, mutation_id, command, create_time,
    is_done, parts_to_do
FROM system.mutations
WHERE NOT is_done
ORDER BY create_time DESC;
```

```sql
-- Merge queue
SELECT
    database, table, elapsed, progress, num_parts, total_size_bytes_compressed
FROM system.merges
ORDER BY elapsed DESC;
```

```sql
-- Insert errors
SELECT
    event_time, query, exception
FROM system.query_log
WHERE type = 'ExceptionBeforeStart' OR type = 'ExceptionWhileProcessing'
  AND query ILIKE 'INSERT%'
ORDER BY event_time DESC LIMIT 20;
```

## Pitfalls

- **Counting on `parts_to_throw_insert` raise to flag part explosion.** By the time it fires, dashboards are already failing. Monitor `system.parts.count()` proactively.
- **Setting `wait_for_async_insert = 0` to "make it faster".** It's not faster — it's just lying about durability.
- **Running `ALTER TABLE … UPDATE` once and then "fixing it next time".** The mutation is queued; subsequent reads return mixed-state data until it finishes.
- **Using `clickhouse-client` to load a CSV row by row.** Use `--input_format=Native` and stream batches.

## How this project tests it

The `Insert performance` benchmark category in `src/queries.py` measures throughput at varying batch sizes. Run:

```bash
uv run clickhouse-bench benchmark --category insert
```

Pair it with `system.parts` inspection to see the part count as batch size changes.

## Sources

- ClickHouse, *Selecting an Insert Strategy*. <https://clickhouse.com/docs/best-practices/selecting-an-insert-strategy>
- ClickHouse, *Asynchronous Inserts*. <https://clickhouse.com/docs/optimize/asynchronous-inserts>
- ClickHouse, *Avoid Mutations*. <https://clickhouse.com/docs/best-practices/avoid-mutations>
- ClickHouse, *Avoid OPTIMIZE FINAL*. <https://clickhouse.com/docs/best-practices/avoid-optimize-final>
- Project skill rules: `insert-batch-size`, `insert-async-small-batches`, `insert-format-native`, `insert-mutation-avoid-update`, `insert-mutation-avoid-delete`, `insert-optimize-avoid-final`
