# 2. Data types

> **Tier 1 — Critical.** Type choice drives 2–10× storage differences and decides whether ClickHouse can apply type-specific codecs and SIMD scans. The columnar format makes type fit a first-order optimisation, not a polish step.

## Use native types, not `String` for everything

Per `schema-types-native-types`, mapping every field to `String` is the most common avoidable mistake. The cost is concrete:

| Field | As `String` | Native type | Saving |
|---|---|---|---|
| UUID | 36 bytes | `UUID` (16 bytes) | 2.25× |
| Timestamp | 19 bytes (`'2024-01-15 10:30:00'`) | `DateTime` (4 bytes) | 4.75× |
| Boolean | 4 bytes (`'true'`) | `Bool` (1 byte) | 4× |
| Counter | 1–10 bytes | `UInt32` (4 bytes) | varies, plus you can do math |

Storage is only half the win. `String` columns can't use `Delta`, `T64`, or `Gorilla` codecs (see [codecs](codecs-compression.md)), can't use `minmax` skip indexes, and force string comparison instead of integer comparison everywhere.

| Data | Use | Avoid |
|---|---|---|
| Sequential IDs | `UInt32` / `UInt64` | `String` |
| UUIDs | `UUID` | `String` |
| Status / category | `Enum8` or `LowCardinality(String)` | `String` |
| Timestamps | `DateTime` | `DateTime64`, `String` |
| Dates only | `Date` (or `Date32` for >2149) | `DateTime`, `String` |
| Counts | `UInt8/16/32` (smallest that fits) | `Int64`, `String` |
| Money | `Decimal(P, S)` or `Int64` cents | `Float64`, `String` |
| Booleans | `Bool` or `UInt8` | `String` |

## Minimise bit-width

Per `schema-types-minimize-bitwidth`. ClickHouse can store an HTTP status code as `Int64` (8 bytes) or `UInt16` (2 bytes). For 1B rows the difference is 6 GB. Smaller types also fit more values per cache line, which speeds aggregations.

| Type | Range | Bytes |
|---|---|---|
| `UInt8` | 0 – 255 | 1 |
| `UInt16` | 0 – 65,535 | 2 |
| `UInt32` | 0 – 4.3 B | 4 |
| `UInt64` | 0 – 18 Q | 8 |
| `Int8` | −128 – 127 | 1 |
| `Int16` | −32 K – 32 K | 2 |
| `Int32` | ±2.1 B | 4 |
| `Int64` | ±9 Q | 8 |

Default to `UInt*` over `Int*` when negatives can't occur. Default to the smallest unsigned type that fits your *expected maximum × headroom*.

## `LowCardinality(String)` <a name="lowcardinality"></a>

Per `schema-types-lowcardinality`. Wrap a `String` column with `LowCardinality(...)` and ClickHouse dictionary-encodes it: each block stores a tiny per-block dictionary plus a column of dictionary indexes (typically `UInt8` or `UInt16`).

- Halves storage on columns where the same values repeat (status, country, event_type).
- ≈2× faster `GROUP BY` and equality filters on those columns.
- Free at ingest — the dictionary is built per-block.

```sql
-- Repeated values stored once per block
country LowCardinality(String),      -- ~200 unique values
browser LowCardinality(String),      -- ~50 unique values
event_type LowCardinality(String)    -- ~100 unique values
```

**The 10K rule.** `LowCardinality` wins for **<10K distinct values**. Above that, the dictionary itself becomes large and offers diminishing returns; above ~100K it can be a net loss. Check before deciding:

```sql
SELECT uniq(column_name) FROM table_name;
```

| Unique values | Use |
|---|---|
| <10K | `LowCardinality(String)` |
| 10K – 100K | Benchmark; usually still `LowCardinality` |
| >100K | Plain `String` |

**`LowCardinality` vs `FixedString(N)`.** `FixedString` only fits truly fixed-length data (ISO country codes, fixed hashes). For variable-length text, `LowCardinality(String)` outperforms `FixedString` because it doesn't pad.

## Avoid `Nullable` unless semantically required

Per `schema-types-avoid-nullable`. `Nullable(T)` adds a parallel `UInt8` null map column. Storage overhead, slower scans, and most aggregations have to special-case nulls.

```sql
-- Wrong: Nullable everywhere
CREATE TABLE users (
    id Nullable(UInt64),       -- IDs are never null
    name Nullable(String),     -- empty string is fine
    age Nullable(UInt8),       -- 0 is a valid default
    login_count Nullable(UInt32)
);

-- Right: DEFAULT for "unknown"; Nullable only when the absence is semantic
CREATE TABLE users (
    id UInt64,
    name String DEFAULT '',
    age UInt8 DEFAULT 0,
    login_count UInt32 DEFAULT 0,
    deleted_at Nullable(DateTime),     -- NULL = "not deleted" (semantic)
    parent_id Nullable(UInt64)         -- NULL = "no parent" (semantic)
);
```

When **is** `Nullable` correct? When the absence carries information you can't encode any other way:

| Column | Why `Nullable` is right |
|---|---|
| `deleted_at` | NULL ≠ deleted; timestamp = deleted at X |
| `parent_id` | NULL = root; value = has a parent |
| `discount_percent` | NULL = no discount; 0 = explicit 0% |

## `Enum8` / `Enum16` for finite value sets

Per `schema-types-enum`. Storage parity with `LowCardinality` (`Enum8` = 1 byte for ≤256 values, `Enum16` = 2 bytes for ≤65,536), plus:

- **Insert-time validation** — `INSERT … VALUES ('shiped')` errors instead of silently writing a typo.
- **Natural ordering** — `ORDER BY status` orders by the enum's integer value, not alphabetically.

```sql
status Enum8('pending' = 1, 'processing' = 2, 'shipped' = 3, 'delivered' = 4)
```

| Need | Use |
|---|---|
| Fixed value set, schema-time | `Enum8`/`Enum16` |
| Values may grow | `LowCardinality(String)` |
| Insert-time validation | `Enum` |
| Natural ordering in queries | `Enum` |

The cost: extending an `Enum` requires `ALTER TABLE … MODIFY COLUMN`, which is usually fine but slower than just inserting a new string into a `LowCardinality` column.

## `JSON` for truly dynamic schemas

Per `schema-json-when-to-use`. ClickHouse's `JSON` type splits objects into typed sub-columns at storage time, enabling field-level scans. Use it for *unpredictable* properties, not as a default.

| Scenario | Use |
|---|---|
| Field set varies unpredictably per row | `JSON` |
| Field types may change over time | `JSON` |
| Need field-level filter / aggregation | `JSON` |
| Fixed, known schema | Typed columns |
| Opaque blob (no field queries) | `String` |

Performance bias: typed columns will always beat `JSON` on known fields. Reach for `JSON` when the schema is genuinely open-ended (event payloads, integration webhooks).

## Validation

```sql
-- Storage by column (ratio ≈ how compressible)
SELECT
    name,
    formatReadableSize(data_compressed_bytes)   AS compressed,
    formatReadableSize(data_uncompressed_bytes) AS uncompressed,
    round(data_uncompressed_bytes / data_compressed_bytes, 2) AS ratio
FROM system.columns
WHERE table = 'events'
ORDER BY data_compressed_bytes DESC;
```

```sql
-- Cardinality check before deciding on LowCardinality
SELECT uniqExact(country), uniqExact(event_type), uniqExact(user_id)
FROM events;
```

## Pitfalls

- **`DateTime64(9)` for timestamps that don't need nanoseconds.** 8 bytes vs 4 for `DateTime`. Only use `DateTime64` when sub-second precision is required.
- **`Float64` for money.** Floating-point error in financial sums. Use `Decimal(P, S)` or store cents as `Int64`.
- **`String` for `UUID`.** 36 bytes per row instead of 16 — and you lose `UUIDStringToNum` / `UUIDNumToString` if you ever need the binary form.
- **`LowCardinality(UInt64)`.** Pointless for high-cardinality numeric IDs. The wrapper is for repeated *strings*.

## Sources

- ClickHouse, *Select Data Types*. <https://clickhouse.com/docs/best-practices/select-data-types>
- Project skill rules: `schema-types-native-types`, `schema-types-minimize-bitwidth`, `schema-types-lowcardinality`, `schema-types-avoid-nullable`, `schema-types-enum`, `schema-json-when-to-use`
