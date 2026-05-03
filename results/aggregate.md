# clickhouse-bench — aggregated results

## Phase A — Baseline benchmark

- File: `benchmark_20260503_191546.json`
- ClickHouse 25.12.1.1497
- Queries (timed): **18** of 20
- Avg cold latency: **94.0 ms**
- Avg warm latency: **82.6 ms**
- p50 warm: **62.2 ms** | p95 warm: **113.6 ms**
- Fastest query: `point_user_by_id` (44.9 ms warm)
- Slowest query: `join_three_tables` (404.5 ms warm)

### Insert throughput
| Batch | ms | rows/sec |
|---|---|---|
| 100 | 314.9 | 318 |
| 1,000 | 175.4 | 5,700 |
| 10,000 | 156.8 | 63,772 |
| 50,000 | 256.4 | 194,966 |

## Phase B / D — Feature comparisons

### bloom_filter_fpr
- File: `compare_features_20260503_192950.json`  | Title: bloom_filter false-positive rate tuning

| Variant | Compressed | Ratio | Insert (s) | Notes |
|---|---|---|---|---|
| bloom_filter(0.001) | 30.36 MiB | 2.83× | 0.98 | OK |
| bloom_filter(0.01) | 29.81 MiB | 2.88× | 0.94 | OK |
| bloom_filter(0.05) | 29.44 MiB | 2.92× | 0.99 | OK |

Warm-avg latency (ms):
| Test query | bloom_filter(0.001) | bloom_filter(0.01) | bloom_filter(0.05) |
|---|---|---|---|
| session_known | 45.6 | 47.5 | 49.6 |
| session_unknown | 42.1 | 40.9 | 41.4 |

> _Lower FPR cuts wasted granule reads on negative lookups but adds index bytes. The 'unknown' query shows the maximum win for tighter FPR — the filter rejects most granules outright. The 'known' query still requires reading the matching granule regardless of FPR._

### codecs
- File: `compare_features_20260503_191717.json`  | Title: Codec comparison: storage and query trade-offs

| Variant | Compressed | Ratio | Insert (s) | Notes |
|---|---|---|---|---|
| LZ4 (default) | 4.80 MiB | 3.34× | 0.44 | OK |
| ZSTD(3) on all columns | 4.04 MiB | 3.97× | 0.27 | OK |
| ZSTD(9) on all columns | 3.95 MiB | 4.06× | 0.51 | OK |
| DoubleDelta(timestamp) + Delta(value) + LZ4 | 5.05 MiB | 3.17× | 0.35 | OK |
| Gorilla(value) + DoubleDelta(timestamp) | 5.82 MiB | 2.75× | 0.46 | OK |

Warm-avg latency (ms):
| Test query | LZ4 (default) | ZSTD(3) on all columns | ZSTD(9) on all columns | DoubleDelta(timestamp) + Delta(value) + LZ4 | Gorilla(value) + DoubleDelta(timestamp) |
|---|---|---|---|---|---|
| range_scan | 47.8 | 42.1 | 42.0 | 41.7 | 42.1 |
| downsample | 52.6 | 49.4 | 49.4 | 50.0 | 48.8 |

> _DoubleDelta + Delta typically beat ZSTD-9 for storage on smooth time-series while keeping query CPU low.  ZSTD-9 wins for entropy-heavy strings.  Gorilla is excellent for floats but has been deprecated in newer versions._

### engines
- File: `compare_features_20260503_191746.json`  | Title: Engine comparison: MergeTree variants

| Variant | Compressed | Ratio | Insert (s) | Notes |
|---|---|---|---|---|
| MergeTree | 4.08 MiB | 2.88× | 0.38 | OK |
| ReplacingMergeTree | 4.11 MiB | 3.14× | 0.51 | OK |
| SummingMergeTree(total_price, quantity) | 1.87 MiB | 3.52× | 0.36 | OK |
| AggregatingMergeTree | 1.90 MiB | 3.06× | 0.47 | OK |

Warm-avg latency (ms):
| Test query | MergeTree | ReplacingMergeTree | SummingMergeTree(total_price, quantity) | AggregatingMergeTree |
|---|---|---|---|---|
| count_all | 39.2 | 40.5 | 40.3 | 39.0 |
| user_lookup | 40.6 | 43.3 | 41.8 | 42.2 |
| category_revenue | 44.4 | 44.8 | 43.0 | — |

> _ReplacingMergeTree and SummingMergeTree shrink storage by collapsing rows on merge.  AggregatingMergeTree pre-computes aggregates so category_revenue runs on already-aggregated state.  Trade-off: less flexible queries on the AMT variant._

### index_granularity
- File: `compare_features_20260503_192945.json`  | Title: index_granularity tuning (sparse primary index)

| Variant | Compressed | Ratio | Insert (s) | Notes |
|---|---|---|---|---|
| index_granularity = 1024 | 29.67 MiB | 2.90× | 0.98 | OK |
| index_granularity = 4096 | 29.08 MiB | 2.96× | 0.95 | OK |
| index_granularity = 8192 | 28.65 MiB | 3.00× | 0.90 | OK |
| index_granularity = 16384 | 27.05 MiB | 3.18× | 0.89 | OK |
| index_granularity = 32768 | 27.03 MiB | 3.18× | 0.84 | OK |

Warm-avg latency (ms):
| Test query | index_granularity = 1024 | index_granularity = 4096 | index_granularity = 8192 | index_granularity = 16384 | index_granularity = 32768 |
|---|---|---|---|---|---|
| by_user_lookup | 42.0 | 41.2 | 44.0 | 43.9 | 44.1 |
| by_user_range | 42.3 | 43.2 | 43.4 | 43.0 | 43.3 |
| by_user_and_time | 41.8 | 43.7 | 43.6 | 44.0 | 43.9 |

> _Smaller granularity wins on point lookups (more skipping) at the cost of a larger in-memory primary index. Larger granularity is lighter but reads more rows per match. The default 8192 is a well-balanced compromise; only deviate when EXPLAIN ESTIMATE shows the granule scan dominating and the index size is comfortable._

### lowcardinality
- File: `compare_features_20260503_191711.json`  | Title: LowCardinality vs String

| Variant | Compressed | Ratio | Insert (s) | Notes |
|---|---|---|---|---|
| country: String | 3.03 MiB | 3.49× | 0.36 | OK |
| country: LowCardinality(String) | 2.94 MiB | 3.31× | 0.34 | OK |

Warm-avg latency (ms):
| Test query | country: String | country: LowCardinality(String) |
|---|---|---|
| group_by | 47.5 | 46.4 |
| filter_country | 48.0 | 44.8 |

> _LowCardinality typically halves storage and doubles GROUP BY speed for low-cardinality columns.  Don't use it for high-cardinality columns (>100K distinct values) — the dictionary overhead dominates._

### materialized_views
- File: `compare_features_20260503_194828.json`  | Title: Materialized views: streaming pre-aggregation

| Variant | Compressed | Ratio | Insert (s) | Notes |
|---|---|---|---|---|
| Raw events (source for MV) | 74.29 MiB | 2.70× | 1.71 | OK |
| MV target: daily event counts | 0.00 MiB | 0.00× | 0.00 | OK |

Warm-avg latency (ms):
| Test query | Raw events (source for MV) | MV target: daily event counts |
|---|---|---|
| raw_daily_count | 196.7 | 167.1 |
| mv_daily_count | 41.0 | 41.2 |

> _Querying the MV target should be near-instant regardless of base table size, while the raw query scales with the base table.  Cost: every insert into the source table runs the MV's GROUP BY._

### ordering
- File: `compare_features_20260503_194824.json`  | Title: ORDER BY key comparison (primary index)

| Variant | Compressed | Ratio | Insert (s) | Notes |
|---|---|---|---|---|
| ORDER BY (user_id, event_time) | 287.74 MiB | 2.99× | 4.89 | OK |
| ORDER BY (event_time, user_id) | 309.42 MiB | 2.78× | 5.93 | OK |
| ORDER BY (event_type, event_time, user_id) | 308.45 MiB | 2.79× | 7.23 | OK |

Warm-avg latency (ms):
| Test query | ORDER BY (user_id, event_time) | ORDER BY (event_time, user_id) | ORDER BY (event_type, event_time, user_id) |
|---|---|---|---|
| by_user | 41.4 | 43.1 | 43.4 |
| by_time | 64.5 | 40.9 | 46.4 |
| by_type | 60.5 | 61.4 | 42.2 |
| user_and_time | 41.5 | 45.8 | 49.1 |

> _user_first wins by_user; time_first wins by_time; type_first wins by_type. The first column of ORDER BY is the most selective — pick it based on your highest-frequency query shape.  Compound ORDER BY also affects column compression: data sorts more compressibly under one key than another._

### partitioning
- File: `compare_features_20260503_191740.json`  | Title: PARTITION BY strategies

| Variant | Compressed | Ratio | Insert (s) | Notes |
|---|---|---|---|---|
| No PARTITION BY | 28.65 MiB | 3.00× | 0.92 | OK |
| PARTITION BY toYYYYMM(event_time) | 28.99 MiB | 2.96× | 2.27 | OK |
| PARTITION BY toDate(event_time) | 0.00 MiB | 0.00× | 0.00 | Received ClickHouse exception, code: 252, server response: C… |
| PARTITION BY user_id % 16 | 28.66 MiB | 3.00× | 2.64 | OK |

Warm-avg latency (ms):
| Test query | No PARTITION BY | PARTITION BY toYYYYMM(event_time) | PARTITION BY toDate(event_time) | PARTITION BY user_id % 16 |
|---|---|---|---|---|
| recent_window | 45.1 | 42.9 | — | 45.3 |
| specific_day | 45.0 | 43.3 | — | 47.9 |
| user_lookup | 40.9 | 42.7 | — | 42.8 |
| parts_count | 40.5 | 42.1 | — | 41.8 |

> _Day-partitioning gives surgical pruning for time-window queries but produces many parts (slower writes if not batched).  Month-partitioning is the most common balance.  user_id % 16 favors per-user queries but kills time-range pruning.  No-partition keeps merges cheap but loses drop-old-data convenience._

### projections
- File: `compare_features_20260503_191721.json`  | Title: Projections (table-internal materialized aggregations)

| Variant | Compressed | Ratio | Insert (s) | Notes |
|---|---|---|---|---|
| No projection | 28.65 MiB | 3.00× | 0.89 | OK |
| With aggregating projection | 28.66 MiB | 3.00× | 1.02 | OK |

Warm-avg latency (ms):
| Test query | No projection | With aggregating projection |
|---|---|---|
| daily_event_count | 63.8 | 48.2 |
| type_distribution | 45.8 | 44.4 |

> _The projected variant should resolve the daily_event_count query directly from pre-aggregated parts — typically 10-100x faster than scanning the base table.  Storage cost: a small fraction of the base table.  Trade-off: writes are slower (every insert updates the projection)._

### skip_index_granularity
- File: `compare_features_20260503_192956.json`  | Title: Skip-index GRANULARITY tuning

| Variant | Compressed | Ratio | Insert (s) | Notes |
|---|---|---|---|---|
| GRANULARITY 1 | 29.81 MiB | 2.88× | 0.99 | OK |
| GRANULARITY 4 | 29.81 MiB | 2.88× | 0.96 | OK |
| GRANULARITY 16 | 29.81 MiB | 2.88× | 1.05 | OK |

Warm-avg latency (ms):
| Test query | GRANULARITY 1 | GRANULARITY 4 | GRANULARITY 16 |
|---|---|---|---|
| session_known | 46.1 | 47.4 | 51.9 |
| session_unknown | 43.6 | 42.7 | 42.5 |

> _Finer GRANULARITY skips more granules at the cost of a larger skip-index. The 'unknown' query is where finer wins; on 'known' lookups the matching granule still has to be read either way._

### skip_indexes
- File: `compare_features_20260503_191731.json`  | Title: Skip (data-skipping) indexes on non-PK columns

| Variant | Compressed | Ratio | Insert (s) | Notes |
|---|---|---|---|---|
| No skip index | 28.65 MiB | 3.00× | 0.91 | OK |
| bloom_filter on session_id | 29.81 MiB | 2.88× | 0.94 | OK |
| set(100) on event_type | 28.65 MiB | 3.00× | 1.38 | OK |
| minmax on event_time | 28.65 MiB | 3.00× | 0.93 | OK |

Warm-avg latency (ms):
| Test query | No skip index | bloom_filter on session_id | set(100) on event_type | minmax on event_time |
|---|---|---|---|---|
| session_lookup | 49.3 | 46.9 | 49.0 | 49.9 |
| type_filter | 45.4 | 44.2 | 45.0 | 46.0 |
| time_window | 44.8 | 46.0 | 46.2 | 46.7 |

> _Bloom dramatically helps high-cardinality equality (session_id). Set helps when filter values fit in the set size. Minmax shines for time-window queries on tables not ordered by time._

## Phase C / D / E — Experiments

### `experiment_c1_dictionary_vs_join_20260503_192050.json`
- Dictionary vs JOIN
  - `dict_speedup_x` = **1.14**

### `experiment_c2_query_cache_20260503_192130.json`
- Query cache hit / miss latency
  - `speedup_wall` = **1.6**
  - `speedup_server` = **28.0**

### `experiment_c3_parallel_replicas_20260503_192203.json`
- Parallel replicas speed-up
  - `speedup_2x` = **0.66**
  - `speedup_3x` = **0.64**

### `experiment_c4_async_inserts_20260503_192314.json`
- Async inserts vs synchronous inserts (100-row batches × 100)
  - `wall_speedup_x` = **0.63**
  - `parts_ratio_sync_to_async` = **1.0**

### `experiment_c5_mutation_cost_20260503_192410.json`
- Mutation cost (ALTER UPDATE) vs ReplacingMergeTree insert
  - `mutation_to_rmt_ratio` = **11.04**

### `experiment_c6_optimize_final_cost_20260503_192446.json`
- OPTIMIZE TABLE … FINAL cost on a fragmented table
  - `us_per_row` = **1.9**

### `experiment_c7_string_vs_native_20260503_192524.json`
- String-for-everything vs native types
  - `storage_savings_x` = **1.21**

### `experiment_c8_nullable_overhead_20260503_192714.json`
- Nullable(T) overhead vs DEFAULT
  - `overhead_ratio` = **0.92**

### `experiment_d4_projection_vs_mv_20260503_193059.json`
- Projection vs Materialized View — same pre-aggregation shape
  - `read_speed_mv_to_proj` = **1.02**

### `experiment_e1_parallel_replicas_curve_10000000rows_20260503_194839.json`
- Parallel replicas scaling curve
  - `speedup_2x` = **1.23**
  - `speedup_3x` = **1.2**
  - events row count when run: 10,000,000

### `experiment_e1_parallel_replicas_curve_1000000rows_20260503_193216.json`
- Parallel replicas scaling curve
  - `speedup_2x` = **0.71**
  - `speedup_3x` = **0.75**
  - events row count when run: 1,000,000

### `experiment_e2_distributed_local_20260503_193339.json`
- Distributed table across local shards (educational)
  - `base_to_merge_ratio` = **0.96**

### `experiment_e3_parallel_replicas_limits_20260503_193416.json`
- Parallel replicas: incompatibilities with FINAL and projections
  - parallel-replicas compat: `{'plain': False, 'projection_match': True, 'final': False}`

## Cost translation (approximate)

Pricing snapshot — capture this from the ClickHouse Cloud pricing page on the day of paper publication and put the screenshot in the appendix. Numbers below are placeholders ±20%.

| Workload | Compute (per query) | Storage (per month) |
|---|---|---|
| Avg warm query (n=18) | ~$0.000016 | n/a |
| events table — ORDER BY (user_id, event_time) (191654) | n/a | ~$0.0007/mo |
| events table — ORDER BY (user_id, event_time) (194824) | n/a | ~$0.0073/mo |

Replace the placeholder numbers with the live pricing page on publication day; `_per_query_cost_usd` and `_storage_cost_per_month` in this script encode the formulas.
