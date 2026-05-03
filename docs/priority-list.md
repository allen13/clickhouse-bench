# ClickHouse performance — priority list

A ranked list of ClickHouse features that move the needle on read and write performance, with rationale, expected gains, and known failure modes. Ordering is by **expected upside × frequency the feature applies** — a 100× win that only applies once is below a 5× win you can apply everywhere.

Each row links to the deeper lesson and to the `clickhouse-best-practices` skill rules it implements.

> **How to read the impact column.** Numbers are from canonical ClickHouse docs and benchmarks (cited in the per-feature lessons). They describe well-suited workloads — the same feature on the wrong workload can be neutral or negative. Always benchmark on your own data.

---

## Tier 1 — Foundations (get these wrong, nothing else helps)

These decide whether your queries are O(rows scanned) or O(rows that match). Every other optimisation runs on top of them.

| # | Feature | Why it ranks here | Expected upside | Lesson |
|---|---|---|---|---|
| 1 | **`ORDER BY` / primary key design** | The sparse primary index is the *only* mechanism that lets ClickHouse skip granules. Wrong key → full scan, every query. `ORDER BY` is **immutable** after table creation — you pay forever for the wrong choice. | 10–1000× on filtered queries that match the key prefix; near-zero on queries that don't | [primary-key.md](primary-key.md) — rules `schema-pk-plan-before-creation`, `-cardinality-order`, `-prioritize-filters`, `-filter-on-orderby` |
| 2 | **Native data types + minimal bit-width** | Columnar compression and SIMD scans both win when types match the data. `String` for everything wastes 2–10× storage and disables type-specific codecs. | 2–10× storage reduction, plus faster scans because cache lines hold more values | [data-types.md](data-types.md) — rules `schema-types-native-types`, `-minimize-bitwidth` |
| 3 | **`LowCardinality(String)`** | Dictionary-encodes string columns with <10K distinct values. Halves storage, doubles `GROUP BY` speed for these columns. Costs nothing for typical workloads (status, country, event_type). | ≈2× on storage and `GROUP BY` for low-cardinality string columns | [data-types.md#lowcardinality](data-types.md#lowcardinality) — rule `schema-types-lowcardinality` |
| 4 | **Insert batching (10K–100K rows)** | Each `INSERT` creates a part. Single-row inserts produce thousands of small parts, exhaust merge throughput, and trigger `Too many parts` errors. | Stable ingest at 100K+ rows/s vs cluster instability at high insert rate | [ingestion.md](ingestion.md) — rule `insert-batch-size` |
| 5 | **Avoid mutations (`ALTER … UPDATE/DELETE`)** | Mutations rewrite **whole parts**, even for one-row changes. Use `ReplacingMergeTree` (updates), lightweight `DELETE` (rare), or `DROP PARTITION` (bulk). | Avoids hours-long part rewrites and disk-I/O spikes that take the cluster offline | [ingestion.md#mutations](ingestion.md#mutations) — rules `insert-mutation-avoid-update`, `insert-mutation-avoid-delete` |

---

## Tier 2 — Query accelerators (apply on top of good schema)

These are the multipliers — they need a sensible schema underneath, but when applicable they turn 10-second queries into 100-millisecond queries.

| # | Feature | Why it ranks here | Expected upside | Lesson |
|---|---|---|---|---|
| 6 | **Materialized views (incremental)** | Run the aggregation at insert time, write results to a target table. Dashboard queries read pre-aggregated rows instead of scanning billions. The biggest single win for repeated dashboards. | 100–10,000× on dashboard latency. Cost: extra CPU per insert | [materialized-views.md](materialized-views.md) — rule `query-mv-incremental` |
| 7 | **Projections** | Alternate sort order or pre-aggregation **inside the same table**. The optimizer rewrites matching queries to use them. Easier than MVs (no separate target table) and atomically consistent. | 10–100× on queries that match the projection's shape | [projections.md](projections.md) — covers MergeTree projection support |
| 8 | **Skip indexes (`bloom_filter`, `set`, `minmax`)** | Granule-level metadata that lets ClickHouse skip blocks for filters on **non-`ORDER BY`** columns. Critical for needle-in-haystack lookups. | Up to 60× per the docs; depends heavily on data correlation with the PK | [skip-indexes.md](skip-indexes.md) — rule `query-index-skipping-indices` |
| 9 | **Compression codecs (per-column)** | `Delta`/`DoubleDelta` for monotonic series, `T64` for narrow integers, `ZSTD(1–3)` general-purpose, `ALP` for floats. Right codec → 5–20× storage reduction without query CPU penalty. | 5–20× on storage for time-series; smaller wins on entropy-heavy data | [codecs-compression.md](codecs-compression.md) |
| 10 | **Dictionaries (`dictGet` instead of JOIN)** | An in-memory key→value map. Replaces JOINs to small dimension tables. The docs' Stack Overflow benchmark: **56% faster, 82% less RAM** vs `JOIN`. | ≈2× on join-heavy workloads with small dimensions | [joins-and-dictionaries.md](joins-and-dictionaries.md) — rule `query-join-consider-alternatives` |
| 11 | **JOIN algorithm + filter-before-join** | The default `parallel_hash` loads the right side into memory — wrong table on the right OOMs. Filter both sides before joining; pick `full_sorting_merge` for already-sorted keys; `partial_merge` for memory-constrained large/large joins. | Difference between query completing and OOM | [joins-and-dictionaries.md#algorithms](joins-and-dictionaries.md#algorithms) — rules `query-join-choose-algorithm`, `-filter-before`, `-use-any` |
| 12 | **Async inserts** | Server-side buffering that turns many small inserts into one large part. Use when you can't batch on the client (observability agents, IoT devices). Keep `wait_for_async_insert=1` for durability. | Same throughput as good batching, without changing the client | [ingestion.md#async-inserts](ingestion.md#async-inserts) — rule `insert-async-small-batches` |

---

## Tier 3 — Situational (high impact in their niche, neutral elsewhere)

Worth knowing, but only deploy when the workload calls for them.

| # | Feature | Why situational | Expected upside | Lesson |
|---|---|---|---|---|
| 13 | **Partitioning** | Per the docs, partitioning is **a data lifecycle tool, not a query optimisation**. `DROP PARTITION` is instant; partition pruning at query time is a side benefit. Wrong partition key (high cardinality, e.g. `user_id`) → "too many parts" errors. | Instant bulk delete; sometimes 2–10× on partition-aligned queries; can hurt otherwise | [partitioning-and-ttl.md](partitioning-and-ttl.md) — rules `schema-partition-lifecycle`, `-low-cardinality`, `-query-tradeoffs` |
| 14 | **Query cache** | Stores full result sets keyed on query text. Default 60-second TTL. Useless for queries with `now()`/`rand()`/`dictGet()`/`SELECT FROM system.*`. Great for repeated dashboard queries with stable parameters. | Sub-ms repeats on otherwise-expensive queries — at the cost of 60s staleness | [query-cache.md](query-cache.md) |
| 15 | **Parallel replicas** | Splits a single query's work across multiple compute nodes. ClickHouse Cloud's 3-replica services already have the topology. **Incompatible with `FINAL` and projections.** Coordination overhead can make small or high-cardinality queries slower. | Linear speed-up on large scans (millions+ rows); negative on small/complex queries | [parallel-replicas.md](parallel-replicas.md) |
| 16 | **TTL with `TO VOLUME` / `RECOMPRESS`** | Move cold partitions to cheap storage; recompress aged data with higher-ratio codecs. Pure cost optimisation — query latency on cold data goes *up*. | Storage cost reduction (S3 vs NVMe); not a query speed-up | [partitioning-and-ttl.md#ttl-actions](partitioning-and-ttl.md#ttl-actions) |

---

## Tier 4 — Cloud context (default, but worth understanding)

Not a knob you turn — these are the realities of running on ClickHouse Cloud.

| # | Feature | Why it matters | Lesson |
|---|---|---|---|
| 17 | **`SharedMergeTree`** | What `ENGINE = MergeTree` actually maps to in Cloud. Compute and storage are separated; metadata lives in ClickHouse-Keeper, not on each replica. Adding replicas is instant — no resharding. `system.replication_queue` doesn't exist; use `system.virtual_parts`. | [cloud-architecture.md](cloud-architecture.md) |
| 18 | **Idle scaling / scale-to-zero** | The service auto-suspends after the configured idle timeout (15 min on this account). First query after suspension pays a cold-start cost (≈10s) and warms the SSD cache. Keep this in mind when reading the first benchmark run of the day. | [cloud-architecture.md#idle-scaling](cloud-architecture.md#idle-scaling) |

---

## Tier 5 — Toolbelt (not a feature; how you measure all the above)

You cannot prove a performance gain without these. Treat them as required reading before publishing benchmark numbers.

| # | Tool | Use | Lesson |
|---|---|---|---|
| 19 | **`EXPLAIN INDEXES=1`** | Confirms the primary index, skip indexes, and projections are actually used. Look for `Granules selected` shrinking. | [observability.md#explain](observability.md#explain) |
| 20 | **`EXPLAIN ESTIMATE`** | Returns marks/parts/rows the query *will* read — perfect for proving an index added value before/after. | [observability.md#explain](observability.md#explain) |
| 21 | **`EXPLAIN PIPELINE`** | Shows the physical pipeline (transforms, threads). Use to verify parallel replicas, projection rewrite, etc. | [observability.md#explain](observability.md#explain) |
| 22 | **`system.query_log`** | Per-query record: `query_duration_ms`, `read_rows/bytes`, `memory_usage`, `ProfileEvents`, `query_cache_usage`, `peak_threads_usage`. | [observability.md#query-log](observability.md#query-log) |
| 23 | **`system.parts`** | Per-part storage view: row count, compressed/uncompressed bytes, merge level. Use to spot part explosions and compression wins. | [observability.md#system-parts](observability.md#system-parts) |

---

## Anti-patterns (what *not* to do)

These come up often enough that they deserve a permanent place on the priority list — by negative weight.

| Anti-pattern | Why it's wrong | Fix |
|---|---|---|
| `OPTIMIZE TABLE … FINAL` after every insert | Forces a full merge ignoring the 150 GB part-size guard. Causes OOM. Background merges already do this work. | Remove. For `ReplacingMergeTree` reads, use `SELECT … FINAL` or `argMax(…)`. Per `insert-optimize-avoid-final` |
| `Nullable(...)` everywhere | Adds a parallel `UInt8` null map per column. Storage overhead + slower scans. | Use `DEFAULT` values for "unknown" (empty string, 0, `now()`). Reserve `Nullable` for real semantic nulls (`deleted_at`). Per `schema-types-avoid-nullable` |
| `ALTER TABLE … UPDATE/DELETE` for routine writes | Rewrites whole parts. Hours of I/O for one-row changes. | `ReplacingMergeTree` for updates, `DROP PARTITION` for bulk delete, lightweight `DELETE` only when rare. Per `insert-mutation-avoid-update`, `-delete` |
| Single-row `INSERT`s in a loop | Creates one part per row. Cluster will throw `Too many parts`. | Batch 10K–100K rows per insert, or enable `async_insert=1` server-side. Per `insert-batch-size` |
| `String` for ids, dates, booleans | 2–10× storage waste; no type-aware compression; can't do math. | `UInt32/64`, `UUID`, `Date`/`DateTime`, `Bool`. Per `schema-types-native-types` |
| `ORDER BY (uuid, ...)` | UUID is high-cardinality — every granule has a different value, so the sparse index can't skip anything. | Put low-cardinality columns first: `ORDER BY (event_type, event_date, uuid)`. Per `schema-pk-cardinality-order` |
| `PARTITION BY user_id` | High-cardinality partition → millions of parts → cluster failure. | Partition by month/day if at all (lifecycle), keep cardinality 100–1000. Per `schema-partition-low-cardinality` |
| Plain `SELECT` over a `ReplacingMergeTree` | Returns un-deduplicated rows until the next merge. | Use `FINAL` or `argMax(col, version) GROUP BY pk`. |

---

## How to use this list

1. **For new tables** — work top-down. Get tier 1 right before considering tier 2. Most projects ship with bad primary keys and try to fix it with skip indexes; the skip index will help, but not as much as fixing the key would have.
2. **For slow queries** — `EXPLAIN INDEXES=1` first. If the primary index isn't being used, the fix is in tier 1, not tier 2. If it is being used and is still slow, tier 2 is where to look (projection, MV, dictionary).
3. **For cost reduction** — tier 3 (partitioning, TTL) and tier 2 codecs. These reduce storage without changing query semantics.
4. **For management reporting** — point at this list and the per-feature numbers from `compare-features` runs.

## Sources

- ClickHouse Best Practices (canonical): <https://clickhouse.com/docs/best-practices>
- Project skill: `clickhouse-best-practices` (28 rules, see [observability.md#rules-cited-in-this-folder](observability.md#rules-cited-in-this-folder))
- Per-topic research: [_research_notes.md](_research_notes.md)
