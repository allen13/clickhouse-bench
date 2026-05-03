# 0. MergeTree — the foundation

> **Tier 0 — Foundational.** Every other lesson in this folder builds on MergeTree. If you understand parts, granules, and merge-time transformations, the rest of the priority list is just "how do we steer the merge".

Cross-reference: this lesson is summarised from §3 ("Storage Layer") of *ClickHouse — Lightning Fast Analytics for Everyone* (Schulze et al., VLDB 2024) — see [docs/references/clickhouse-vldb-2024.pdf](references/clickhouse-vldb-2024.pdf). When this doc says "the paper", that is what it means. ClickHouse Cloud's `SharedMergeTree` extends the same model — see [cloud-architecture.md](cloud-architecture.md).

## What MergeTree is

The MergeTree family is **the primary persistence format in ClickHouse** (paper §3). Every analytics workload ultimately runs on a MergeTree variant. It is an LSM-tree-derived design optimised for high-throughput inserts and fast columnar scans:

- Tables are split into **immutable parts** (sorted by the primary key columns).
- Each `INSERT` creates a new part on disk. Background merges combine smaller parts into larger ones (up to a 150 GB cap by default).
- Source parts are kept until their reference count drops to zero, then deleted.
- Variants of MergeTree differ only in **what the merge does** when it sees rows with the same key.

This is why MergeTree advice often sounds counterintuitive: it's an *append-mostly* engine where the read path benefits from background work. The merge is the optimisation, and `OPTIMIZE FINAL` short-circuiting the merge is what ruins it (see [ingestion.md](ingestion.md)).

## Parts, granules, blocks, and marks

Per the paper §3.1:

- **Part** — a directory on disk with one file per column (or a single file in `Compact` parts if the part is <10 MB). Self-contained: includes all metadata to interpret its content without a central catalog.
- **Granule** — a logical group of **8,192 rows** (the default `index_granularity`). The smallest unit the scan and index lookup operators address.
- **Block** — physical I/O unit. ClickHouse forms blocks by combining neighbouring granules within a column up to a configurable byte size (1 MB default). Blocks are compressed on disk.
- **Mark file (`*.mrk`)** — translates granule numbers to compressed-block offsets so reads can seek directly to the right bytes.

The sparse primary index has **one entry per granule**, not per row — covered in detail in [primary-key.md](primary-key.md).

## The MergeTree family — pick the merge to match the workload

Per the paper §3.3, the variants differ in their merge-time transformation:

| Engine | What the merge does | Use when |
|---|---|---|
| `MergeTree` | k-way merge sort by ORDER BY tuple; no row collapsing | Default. Append-only or write-once-read-many analytics. |
| `ReplacingMergeTree(version)` | Keeps only the **newest** row per primary key (by version column or insertion order) | Frequent updates. Insert new versions; collapse on merge. Read with `FINAL` or `argMax(...)`. |
| `SummingMergeTree(cols)` | Sums the listed numeric columns across rows with the same key | Pre-aggregated counters / running totals where you only need sums. |
| `AggregatingMergeTree` | Combines partial aggregation states (`*State` functions) on rows with the same key | Backbone of incremental materialized views — see [materialized-views.md](materialized-views.md). |
| `CollapsingMergeTree(sign)` | Collapses pairs of `+1`/`-1` rows | Soft-delete pattern. Insert with `sign = -1` instead of mutating. |
| `VersionedCollapsingMergeTree(sign, version)` | Like Collapsing, but version-aware so out-of-order arrivals collapse correctly | Same as Collapsing under unreliable insert ordering. |
| `GraphiteMergeTree` | Roll-up rules applied to time-series rows on merge | Graphite-style metric retention; rarely needed outside that ecosystem. |

**Key consequence:** the merge-time transformation **does not guarantee** that the table never contains pre-merge data. Reads must either tolerate it (acceptable for monotone aggregations at scale) or use the `FINAL` modifier or `argMax(...)` patterns. Per the paper §3.3: *"all merge-time transformations can be applied at query time by specifying the keyword FINAL in SELECT statements."*

## Insert paths

Per the paper §3.1, two insert modes:

- **Synchronous** — each `INSERT` creates a new part immediately. Fast and durable; clients should batch (10K–100K rows) to keep merge load reasonable.
- **Asynchronous** (`async_insert = 1`) — the server buffers rows from many `INSERT`s into the same table and flushes when a buffer-size or timeout threshold is hit. Removes the need for client-side batching at the cost of a small flush latency. Critical for observability workloads with thousands of agents. See [ingestion.md#async-inserts](ingestion.md#async-inserts).

Idempotent retries (paper §3.5): the server keeps hashes of the last N inserted parts (typically 100) and rejects re-inserts of parts with a known hash. Clients can re-send a batch after a timeout without dedup logic.

## `SharedMergeTree` in ClickHouse Cloud

In Cloud, every MergeTree DDL implicitly maps to `SharedMergeTree` — compute and storage are separated, metadata centralised in ClickHouse-Keeper, and replicas are stateless compute nodes that read from object storage. Same on-disk format; different replication / coordination model. Full details in [cloud-architecture.md](cloud-architecture.md).

## DDL examples

```sql
-- Plain MergeTree — analytics default
CREATE TABLE events (
    user_id    UInt64,
    event_type LowCardinality(String),
    event_time DateTime
)
ENGINE = MergeTree()
ORDER BY (event_type, event_time, user_id);

-- ReplacingMergeTree for an updatable users dimension
CREATE TABLE users (
    user_id    UInt64,
    name       String,
    status     LowCardinality(String),
    updated_at DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY user_id;

-- AggregatingMergeTree as a target for a materialized view
CREATE TABLE events_hourly (
    event_type LowCardinality(String),
    hour       DateTime,
    events     AggregateFunction(count),
    users      AggregateFunction(uniq, UInt64)
)
ENGINE = AggregatingMergeTree()
ORDER BY (event_type, hour);

-- CollapsingMergeTree for soft delete
CREATE TABLE orders_active (
    order_id UInt64,
    total    Decimal(10, 2),
    sign     Int8       -- +1 active, -1 cancelled
)
ENGINE = CollapsingMergeTree(sign)
ORDER BY order_id;
```

## Validation

```sql
-- Per-table part inventory and merge depth
SELECT
    table,
    count()                                            AS parts,
    sum(rows)                                          AS rows,
    formatReadableSize(sum(data_compressed_bytes))     AS compressed,
    avg(level)                                         AS avg_merge_level,
    countIf(part_type = 'Wide') AS wide_parts,
    countIf(part_type = 'Compact') AS compact_parts
FROM system.parts
WHERE active AND database = currentDatabase()
GROUP BY table
ORDER BY parts DESC;
```

```sql
-- Active merges (so you can see the engine working)
SELECT database, table, elapsed, progress, num_parts,
       formatReadableSize(total_size_bytes_compressed) AS size
FROM system.merges
ORDER BY elapsed DESC;
```

```sql
-- For ReplacingMergeTree: count duplicates pre-merge vs post-FINAL
SELECT count() FROM users;            -- raw row count
SELECT count() FROM users FINAL;      -- post-collapse
-- The gap is your write-amplification on updates.
```

## Pitfalls

- **Running `OPTIMIZE TABLE … FINAL` "to clean up".** Forces a merge ignoring the 150 GB part-size guard; on big tables → OOM and hours of disk I/O. Background merges already do this work. Per `insert-optimize-avoid-final` and [ingestion.md](ingestion.md).
- **Reading `ReplacingMergeTree` without `FINAL` or `argMax`.** Returns multiple versions until the merge collapses them. Choose your read pattern at design time, not at "why is this row duplicated?" time.
- **`CollapsingMergeTree` without sign-aware reads.** The `+1`/`-1` rows coexist between merges. Always read with `sum(col * sign)` or `HAVING sum(sign) > 0`.
- **Picking `ReplacingMergeTree` for high-QPS dashboards using `FINAL`.** `FINAL` re-runs the merge logic per query — expensive at concurrency. Use `argMax(...)` patterns or push the dedup into a materialized view instead.
- **Treating MergeTree as a queue.** It's not — point updates and deletes (mutations) rewrite whole parts. Frequent UPDATE/DELETE patterns belong in `Replacing`/`Collapsing`/`Versioned` variants, not in `ALTER TABLE … UPDATE/DELETE`. Per `insert-mutation-avoid-update`, `insert-mutation-avoid-delete`.

## How this project tests it

The `compare-features --comparison engines` run benchmarks `MergeTree`, `ReplacingMergeTree`, `SummingMergeTree`, and `AggregatingMergeTree` on the same `orders` data set, surfacing storage and query latency for each. Run after the toolkit is connected:

```bash
uv run clickhouse-bench compare-features --comparison engines
```

`requires_optimize=True` on the `Replacing`/`Summing`/`Aggregating` variants in [src/schema_variants.py](../src/schema_variants.py) intentionally fires `OPTIMIZE TABLE … FINAL` once after the seed insert so the variants are measured at fully-merged state — this is a deliberate one-shot use of `OPTIMIZE FINAL` for benchmark isolation, *not* a recommendation for production.

## Sources

- Schulze, R., Schreiber, T., Yatsishin, I., Dahimene, R., & Milovidov, A. (2024). *ClickHouse — Lightning Fast Analytics for Everyone*. **Proceedings of the VLDB Endowment**, 17(12), 3731–3744. Local copy: [references/clickhouse-vldb-2024.pdf](references/clickhouse-vldb-2024.pdf). Sections cited: §3.1 on-disk format, §3.3 merge-time transformations, §3.4 updates/deletes, §3.5 idempotent inserts.
- ClickHouse, *MergeTree Table Engines*. <https://clickhouse.com/docs/engines/table-engines/mergetree-family/mergetree>
- Project skill rules: `insert-optimize-avoid-final`, `insert-mutation-avoid-update`, `insert-mutation-avoid-delete`.
- Related lessons: [primary-key.md](primary-key.md), [ingestion.md](ingestion.md), [materialized-views.md](materialized-views.md), [cloud-architecture.md](cloud-architecture.md).
