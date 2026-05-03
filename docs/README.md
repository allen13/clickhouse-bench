# ClickHouse performance — lessons learned

This folder is the technical record for the benchmark project. The goal is to prove (with data) that we know which ClickHouse features matter for performance, why they matter, and when they backfire.

Two starting points:

- **[priority-list.md](priority-list.md)** — the headline deliverable. Ranked features by performance impact, with rationale, expected gains, and when each one stops paying back.
- **[Per-feature lessons](#per-feature-lessons)** — one file per feature with the mechanics, DDL, validation queries, and gotchas the docs gloss over.

Each lesson cites the official docs at <https://clickhouse.com/docs> and the project's `clickhouse-best-practices` skill rules (28 rules across schema, query, and insert categories).

## Foundations

Read this first — the rest of the lessons assume the vocabulary it establishes.

| # | File | Topic |
|---|---|---|
| 0 | [merge-tree.md](merge-tree.md) | The MergeTree family, parts, granules, blocks, merge-time transformations. Cross-referenced with the VLDB 2024 paper. |

## Per-feature lessons

| # | File | Tier | Topic |
|---|---|---|---|
| 1 | [primary-key.md](primary-key.md) | Critical | `ORDER BY` and the sparse index — how granules work, why low-cardinality columns go first |
| 2 | [data-types.md](data-types.md) | Critical | Native types, `LowCardinality`, bit-width, `Nullable` vs `DEFAULT`, `Enum` |
| 3 | [codecs-compression.md](codecs-compression.md) | High | Per-column codec pipelines: `Delta`, `DoubleDelta`, `T64`, `ZSTD`, `Gorilla`, `ALP` |
| 4 | [partitioning-and-ttl.md](partitioning-and-ttl.md) | High | `PARTITION BY` for lifecycle (not queries), tiered storage, TTL move/recompress/delete |
| 5 | [skip-indexes.md](skip-indexes.md) | High | `bloom_filter`, `set`, `minmax`, `ngrambf_v1`, `tokenbf_v1` for non-PK filters |
| 6 | [projections.md](projections.md) | High | Alternate sort/aggregation inside the same table; `MATERIALIZE PROJECTION` |
| 7 | [materialized-views.md](materialized-views.md) | High | Incremental MVs at insert time, refreshable MVs on a schedule |
| 8 | [joins-and-dictionaries.md](joins-and-dictionaries.md) | Critical | Join algorithms, filter-before-join, `dictGet` as the join replacement |
| 9 | [ingestion.md](ingestion.md) | Critical | Batch sizing, async inserts, native format, why `ALTER … UPDATE/DELETE` is poison |
| 10 | [query-cache.md](query-cache.md) | Medium | When the result cache helps, eviction, verifying hits |
| 11 | [parallel-replicas.md](parallel-replicas.md) | Medium | Multi-replica read parallelism on ClickHouse Cloud |
| 12 | [cloud-architecture.md](cloud-architecture.md) | Context | `SharedMergeTree`, compute/storage separation, idle scaling |
| 13 | [observability.md](observability.md) | Toolbelt | `EXPLAIN PIPELINE/INDEXES/ESTIMATE`, `system.query_log`, `system.parts` |
| 14 | [sharding-and-distributed.md](sharding-and-distributed.md) | Cloud context | `SharedMergeTree` vs OSS sharding; parallel-replicas compatibility matrix from Phase E measurements |

## How these docs were built

- **Authoritative rules** — invoked the project's `clickhouse-best-practices` skill (28 rules, prioritised by impact). Each lesson cites the rules it implements with the format `Per the **schema-pk-cardinality-order** rule…`.
- **Docs gaps** — `_research_notes.md` captures topics the rule set does not cover (parallel replicas, query cache, sparse-index mechanics, dictionaries, codec details, cloud-specific engines, TTL, EXPLAIN). Sources are canonical pages on `clickhouse.com/docs`.
- **Academic grounding** — the foundational MergeTree lesson cites the canonical VLDB 2024 paper kept locally at [references/clickhouse-vldb-2024.pdf](references/clickhouse-vldb-2024.pdf).
- **Project alignment** — the project's `src/schema_variants.py` already defines 8 `compare-features` benchmarks (engines, codecs, ordering, lowcardinality, projections, materialized views, skip indexes, partitioning). The lessons here explain the *why* behind each variant so the numbers we publish are interpretable.

## References

The [`references/`](references/) directory holds canonical PDFs we cite from. When citing, prefer the local copy + section number so links stay stable.

| File | Citation |
|---|---|
| [references/clickhouse-vldb-2024.pdf](references/clickhouse-vldb-2024.pdf) | Schulze, R., Schreiber, T., Yatsishin, I., Dahimene, R., & Milovidov, A. (2024). *ClickHouse — Lightning Fast Analytics for Everyone*. **Proc. VLDB Endow.**, 17(12), 3731–3744. <https://www.vldb.org/pvldb/vol17/p3731-schulze.pdf> |

## Conventions

- File names are stable URLs — link from PRs, slides, and Slack.
- Each lesson opens with a one-sentence claim and an impact rating (`Critical / High / Medium / Context`).
- Numbers cite their source — never write "10× faster" without a link.
- Code blocks are runnable against ClickHouse Cloud (the project's target). OSS-only behaviour is called out.

## Status

| Phase | State |
|---|---|
| Cloud account + service | ✅ verified — smoke test in chat 2026-05-03 |
| Toolkit (`uv run clickhouse-bench`) connected | ⏳ pending — need `CLICKHOUSE_HOST/PASSWORD` in `.env` |
| Docs structure | ✅ this folder |
| Benchmark results published | ⏳ next phase |
