# 3. Compression codecs

> **Tier 2 — High.** Per-column codec choice can collapse storage 5–20× on time-series data and pays back at every read (less I/O, more values per cache line). Wrong codec on the wrong data wastes CPU with no benefit.

## How codecs compose

ClickHouse runs codecs as a pipeline. Each column passes through *codec stages* in declaration order, and the result is then handled by the general block compressor.

```sql
column_name Type CODEC(Encoder1, Encoder2, ...)
```

Typical pattern: a *transformer* codec (Delta, T64, Gorilla) followed by a *general* codec (LZ4, ZSTD).

```sql
-- Delta-encode adjacent values, then ZSTD-compress the deltas
event_time DateTime CODEC(DoubleDelta, ZSTD(1)),

-- Strip leading zeros from narrow integers, then LZ4
status_code UInt16 CODEC(T64, LZ4)
```

Without an explicit `CODEC`, ClickHouse uses LZ4 (the default).

## Codec reference

| Codec | Best for | Notes |
|---|---|---|
| `LZ4` | General default | Fast decode; modest ratio |
| `ZSTD(level)` | General default for storage-sensitive workloads | Levels 1–22; **levels >3 give diminishing returns**; default 1 |
| `Delta(N)` | Monotonic integer sequences (timestamps, counters) | Stores `value[i] - value[i-1]`; pair with `LZ4`/`ZSTD` |
| `DoubleDelta` | Time-series with constant stride (1-min intervals) | Second-order delta; "optimal for monotonic sequences with constant stride" |
| `Gorilla` | Slowly-changing float gauges | XOR between consecutive values; benchmark — `ZSTD` alone often wins |
| `T64` | Narrow integers (status codes, age) | Transposes 64-value blocks, strips zero high bits; great with sparse enums |
| `ALP` | Floating-point measurements (currency, sensor) | Newer codec; higher ratio + faster than Gorilla; preferred for new schemas |
| `FPC` | Slowly-changing float series | Alternative to Gorilla |
| `GCD` | Values that change in multiples of a common factor | Niche |

**On `Gorilla`'s status:** still supported and maintained as of 2025, but `ALP` is positioned as its preferred successor. New schemas: prefer `ALP`; legacy schemas: leave `Gorilla` in place unless you're already migrating.

## ZSTD level guidance

Default to `ZSTD(1)`. Per the docs: "compression levels above 3 rarely result in significant gains" — meaning the curve flattens. Use:

| Need | Level |
|---|---|
| Fastest writes / decode | `LZ4` (no ZSTD) |
| Default — good balance | `ZSTD(1)` |
| Storage-constrained, decode-tolerant | `ZSTD(3)` |
| Cold data (TTL recompress target) | `ZSTD(9)` or higher |

Each level above 3 trades *seconds of write CPU* for *kilobytes of saved storage*. For Cloud workloads where storage is cheap and compute is metered, the value tilts toward lower levels.

## Time-series rule of thumb

Time-series tables benefit dramatically from per-column codecs. A typical metrics schema:

```sql
CREATE TABLE metrics (
    sensor_id   UInt32  CODEC(T64, LZ4),
    metric_name LowCardinality(String) CODEC(ZSTD(1)),
    value       Float64 CODEC(Gorilla, LZ4),       -- or CODEC(ALP) on new schemas
    timestamp   DateTime CODEC(DoubleDelta, LZ4)
)
ENGINE = MergeTree() ORDER BY (sensor_id, timestamp);
```

Why each:

- **`timestamp` `DoubleDelta`** — sensor data arrives at near-constant intervals. The second derivative is mostly zero, which compresses to almost nothing.
- **`value` `Gorilla`/`ALP`** — float gauges change slowly. XOR between consecutive values yields lots of leading/trailing zeros.
- **`sensor_id` `T64`** — narrow integer; T64 strips the leading zero bytes that dominate the representation.
- **`metric_name` `ZSTD(1)`** — already a `LowCardinality(String)`; ZSTD on the dictionary is straightforward general compression.

## When NOT to specialise

- **High-entropy strings, hashes, encrypted blobs.** Already random; specialised codecs add CPU for no gain. Use `LZ4` or skip codec entirely.
- **Random integers.** No delta pattern → `Delta` is wasted CPU. Stick with `LZ4` or `T64` if the values are narrow.
- **Already-compressed data** (gzip blobs, protobuf bytes). Don't recompress.
- **Tiny tables.** The codec overhead per part can dominate.

> "Understanding your data distribution determines codec effectiveness — testing remains mandatory." — ClickHouse blog, *Optimizing ClickHouse with Schemas and Codecs*

## Validation

```sql
-- Per-column compression ratio
SELECT
    name,
    type,
    formatReadableSize(data_compressed_bytes)   AS compressed,
    formatReadableSize(data_uncompressed_bytes) AS uncompressed,
    round(data_uncompressed_bytes / data_compressed_bytes, 2) AS ratio,
    compression_codec
FROM system.columns
WHERE database = currentDatabase() AND table = 'metrics'
ORDER BY data_compressed_bytes DESC;
```

```sql
-- Whole-table footprint
SELECT
    formatReadableSize(sum(data_compressed_bytes))   AS compressed,
    formatReadableSize(sum(data_uncompressed_bytes)) AS uncompressed,
    round(sum(data_uncompressed_bytes) / sum(data_compressed_bytes), 2) AS ratio
FROM system.parts
WHERE active AND database = currentDatabase() AND table = 'metrics';
```

## TTL-driven recompression

Cold data can be recompressed in place to a higher level when it ages out of "hot" status:

```sql
ALTER TABLE events
MODIFY TTL ts + INTERVAL 30 DAY RECOMPRESS CODEC(ZSTD(9));
```

See [partitioning-and-ttl.md](partitioning-and-ttl.md) for the full TTL action set.

## How this project tests it

`compare-features --comparison codecs` runs five variants of the `metrics` table — `LZ4`, `ZSTD(3)`, `ZSTD(9)`, `DoubleDelta+Delta+LZ4`, and `Gorilla+DoubleDelta+LZ4` — and prints storage size and query latency for each. Run it once the toolkit is connected:

```bash
uv run clickhouse-bench compare-features --comparison codecs
```

## Sources

- ClickHouse, *Column Compression Codecs (CREATE TABLE)*. <https://clickhouse.com/docs/en/sql-reference/statements/create/table#column-compression-codecs>
- ClickHouse Blog, *Optimizing ClickHouse with Schemas and Codecs*. <https://clickhouse.com/blog/optimize-clickhouse-codecs-compression-schema>
