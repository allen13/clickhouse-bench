# 13. Observability — `EXPLAIN` and system tables

> **Tier 5 — Toolbelt.** You cannot prove a performance gain without these. Treat them as required reading before publishing benchmark numbers.

## `EXPLAIN` <a name="explain"></a>

`EXPLAIN` reveals what ClickHouse intends to do with a query *before* running it. Four flavours, each answering a different question:

| Form | What it shows | When to use |
|---|---|---|
| `EXPLAIN PLAN` | Logical step tree (the optimiser's output) | Sanity-check query rewrites |
| `EXPLAIN PLAN indexes = 1` | Which indexes are used + granules selected | Prove a primary index or skip index works |
| `EXPLAIN PIPELINE` | Physical processor pipeline (transforms, threads) | Verify parallel replicas, projection rewrite, join algorithm |
| `EXPLAIN PIPELINE graph = 1` | DOT graph for visualisation | Render with `graphviz` for tricky pipelines |
| `EXPLAIN ESTIMATE` | Rows / marks / parts the query *will* read (MergeTree only) | Compare before/after — the cleanest way to quantify an index win |

### `EXPLAIN ESTIMATE` — the workhorse

```sql
EXPLAIN ESTIMATE
SELECT count() FROM events WHERE user_id = 42;

-- ┌─database─┬─table──┬─parts─┬─rows──┬─marks─┐
-- │ default  │ events │     5 │  4096 │     1 │
-- └──────────┴────────┴───────┴───────┴───────┘
```

Run it before adding an index. Add the index. Run it again. The drop in `marks` is your proof.

### `EXPLAIN indexes = 1` — confirm the right key

```sql
EXPLAIN indexes = 1
SELECT * FROM events WHERE tenant_id = 123 AND event_type = 'click';
```

Look for:

- **`Keys`** — the columns ClickHouse used.
- **`Granules: <selected>/<total>`** — granule-skipping ratio. Closer to `<selected>` ≪ `<total>` is better.
- **`Selected … parts by partition key`** — partition pruning happened.
- **`PrimaryKey`** — primary index participated.

### `EXPLAIN PIPELINE` — verify the physical plan

```sql
EXPLAIN PIPELINE
SELECT toDate(event_time) AS day, count()
FROM events GROUP BY day
SETTINGS enable_parallel_replicas = 1;
```

Look for `ReadFromRemoteParallelReplicas` or the projection's name to confirm the optimiser actually used what you wanted.

## `system.query_log` <a name="query-log"></a>

Every query — success, failure, cancellation — is recorded here. The columns that matter for benchmarking:

| Column | Use |
|---|---|
| `query_duration_ms` | Wall-clock latency |
| `read_rows` / `read_bytes` | Volume actually read — proves index efficiency |
| `result_rows` / `result_bytes` | Output size |
| `memory_usage` | Peak RAM per query |
| `ProfileEvents` | Map of 200+ fine-grained metrics — cache hits, disk reads, merges, network |
| `query_cache_usage` | `'Read'`, `'Write'`, or `'None'` |
| `peak_threads_usage` | Parallelism actually achieved |
| `normalized_query_hash` | Groups parameterised variants for aggregation |
| `type` | Filter to `'QueryFinish'` for successful queries |
| `tables` / `columns` | What the query touched (good for cross-referencing) |

### Useful patterns

```sql
-- Slowest queries in the last hour
SELECT
    query_duration_ms, read_rows, memory_usage, query
FROM system.query_log
WHERE event_time >= now() - INTERVAL 1 HOUR
  AND type = 'QueryFinish'
ORDER BY query_duration_ms DESC
LIMIT 20;
```

```sql
-- Aggregate by query shape
SELECT
    normalized_query_hash,
    count()                                        AS runs,
    avg(query_duration_ms)                         AS avg_ms,
    quantile(0.95)(query_duration_ms)              AS p95_ms,
    avg(read_rows)                                 AS avg_rows
FROM system.query_log
WHERE event_time >= now() - INTERVAL 1 HOUR
  AND type = 'QueryFinish'
GROUP BY normalized_query_hash
ORDER BY p95_ms DESC LIMIT 20;
```

```sql
-- Profile-event breakdown for a specific query
SELECT
    arrayJoin(ProfileEvents) AS pe,
    pe.1 AS event,
    pe.2 AS value
FROM system.query_log
WHERE query_id = '<paste-query-id>'
ORDER BY value DESC;
```

The `ProfileEvents` map is gold for diagnosing *why* a query is slow — `OSReadChars`, `S3ReadRequestsCount`, `MergeTreeDataSelectExecutorMarksLoadMicroseconds`, etc.

## `system.parts` <a name="system-parts"></a>

Per-part inventory. Use to diagnose storage health, compression, and merge progress.

| Column | Use |
|---|---|
| `rows` / `marks` | Row + granule counts (`marks × index_granularity ≈ rows`) |
| `data_compressed_bytes` / `data_uncompressed_bytes` | Compression ratio |
| `level` | Merge depth — high = well-merged |
| `active` | Only `active = 1` parts participate in queries |
| `part_type` | `Wide` (column-per-file) vs `Compact` (small parts merged into one file) |
| `disk_name` | Confirm tiered-storage placement |
| `partition` | Which partition the part belongs to |

### Useful patterns

```sql
-- Per-table storage and part health
SELECT
    table,
    count()                                                AS parts,
    sum(rows)                                              AS rows,
    formatReadableSize(sum(data_compressed_bytes))         AS compressed,
    formatReadableSize(sum(data_uncompressed_bytes))       AS uncompressed,
    round(sum(data_uncompressed_bytes)
          / sum(data_compressed_bytes), 2)                 AS ratio
FROM system.parts
WHERE active AND database = currentDatabase()
GROUP BY table
ORDER BY rows DESC;
```

```sql
-- Per-partition health (warning signs: many partitions, many parts)
SELECT
    partition,
    count() AS parts, sum(rows) AS rows,
    formatReadableSize(sum(bytes_on_disk)) AS size
FROM system.parts
WHERE active AND table = 'events'
GROUP BY partition ORDER BY partition;
```

```sql
-- Per-column compression
SELECT
    name, type, compression_codec,
    formatReadableSize(data_compressed_bytes)   AS compressed,
    formatReadableSize(data_uncompressed_bytes) AS uncompressed,
    round(data_uncompressed_bytes / data_compressed_bytes, 2) AS ratio
FROM system.columns
WHERE database = currentDatabase() AND table = 'events'
ORDER BY data_compressed_bytes DESC;
```

## Other useful system tables

| Table | What it tells you |
|---|---|
| `system.events` | Server-wide counters (`QueryCacheHits`, `S3ReadRequestsCount`, etc.) |
| `system.metrics` | Live gauges (`MergesInProgress`, `BackgroundPoolTask`) |
| `system.merges` | Merges in flight — diagnose merge stalls |
| `system.mutations` | Outstanding mutations — see if `ALTER UPDATE/DELETE` is still running |
| `system.parts_columns` | Per-(part, column) compression — find a column inflating storage |
| `system.data_skipping_indices` | Skip-index inventory and size |
| `system.projection_parts` | Projection sub-parts — confirm projections are populated |
| `system.virtual_parts` | (Cloud / SMT) replacement for `system.replication_queue` |
| `system.asynchronous_insert_log` | Async insert flush events |
| `system.query_cache` | Currently-cached query results |

## A reproducible benchmarking workflow

1. **Define the question.** "Does adding a `bloom_filter` on `session_id` reduce reads for `WHERE session_id = ?` queries?"
2. **Capture baseline.** `EXPLAIN ESTIMATE` + run query 5×, record `query_duration_ms`, `read_rows` from `system.query_log`.
3. **Make the change.** Add the index, `MATERIALIZE INDEX`.
4. **Capture after.** Same `EXPLAIN ESTIMATE` + 5 runs.
5. **Report deltas.** `read_rows` dropped from N to M (X× reduction); p50/p95 latency change; index size from `system.data_skipping_indices`.

The project's `compare-features` runner is built on this pattern — see `src/compare_features.py` for the structure.

## Sources

- ClickHouse, *EXPLAIN Statement*. <https://clickhouse.com/docs/en/sql-reference/statements/explain>
- ClickHouse, *system.query_log*. <https://clickhouse.com/docs/operations/system-tables/query_log>
- ClickHouse, *system.parts*. <https://clickhouse.com/docs/en/operations/system-tables/parts>

## Rules cited in this folder <a name="rules-cited-in-this-folder"></a>

The lessons in `docs/` cite the project's `clickhouse-best-practices` skill rules. The full skill lives at `~/.claude/plugins/cache/clickhouse-wrapper/clickhouse/1.0.0/skills/clickhouse-best-practices/` (compiled doc: `AGENTS.md`). The 28 rules:

| Category | Rule |
|---|---|
| **Schema – PK** | `schema-pk-plan-before-creation`, `schema-pk-cardinality-order`, `schema-pk-prioritize-filters`, `schema-pk-filter-on-orderby` |
| **Schema – types** | `schema-types-native-types`, `schema-types-minimize-bitwidth`, `schema-types-lowcardinality`, `schema-types-avoid-nullable`, `schema-types-enum` |
| **Schema – partition** | `schema-partition-low-cardinality`, `schema-partition-lifecycle`, `schema-partition-query-tradeoffs`, `schema-partition-start-without` |
| **Schema – JSON** | `schema-json-when-to-use` |
| **Query – joins** | `query-join-choose-algorithm`, `query-join-consider-alternatives`, `query-join-filter-before`, `query-join-null-handling`, `query-join-use-any` |
| **Query – indexes** | `query-index-skipping-indices` |
| **Query – MV** | `query-mv-incremental`, `query-mv-refreshable` |
| **Insert – batch** | `insert-batch-size`, `insert-format-native` |
| **Insert – async** | `insert-async-small-batches` |
| **Insert – mutations** | `insert-mutation-avoid-update`, `insert-mutation-avoid-delete` |
| **Insert – optimize** | `insert-optimize-avoid-final` |
