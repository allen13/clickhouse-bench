# 4. Partitioning and TTL

> **Tier 3 — High in its niche, neutral or harmful elsewhere.** Per the official docs, **partitioning is a data-lifecycle tool, not a query optimisation.** Pick the partition key for `DROP PARTITION` and tiered storage; let pruning be a side effect.

## What partitioning actually does

`PARTITION BY` splits each MergeTree table into independent storage units (folders on disk / object-storage prefixes). Each partition has its own parts, its own merges, and can be dropped/attached as a single metadata operation. ClickHouse automatically maintains a **MinMax index on the partition columns** for query pruning.

The four things partitioning enables:

1. **Instant bulk delete.** `ALTER TABLE … DROP PARTITION '202301'` is a metadata operation. `DELETE WHERE` would scan and rewrite parts.
2. **Tiered storage moves.** `TTL ts + INTERVAL 90 DAY TO VOLUME 's3'` migrates whole partitions to cheap storage.
3. **Archive workflows.** `ATTACH PARTITION` / `DETACH PARTITION` move a whole month between tables in seconds.
4. **Partition pruning at query time.** A `WHERE` that aligns with the partition expression skips entire partitions.

## The four rules that matter

### `schema-partition-lifecycle` (High)

> Partitioning is **primarily a data-management technique, not a query-optimisation tool.**

Pick the partition key to match your *retention* and *archive* policy, not your `WHERE` clauses. The right key is whatever lets you drop or move whole partitions atomically.

```sql
-- Partition by month → retention works at month granularity
PARTITION BY toYYYYMM(event_time)
TTL event_time + INTERVAL 1 YEAR DELETE
```

### `schema-partition-low-cardinality` (High)

Keep partition cardinality between **100 and 1,000 distinct values**. Above that you start hitting `Too many parts` errors and `max_parts_in_total` / `parts_to_throw_insert` thresholds.

| Partition key | Annual cardinality | Verdict |
|---|---|---|
| `toYYYYMM(ts)` | 12 | ✅ ideal |
| `toDate(ts)` | 365 | ✅ acceptable for one year, gets uncomfortable past two |
| `toStartOfHour(ts)` | 8,760 | ❌ too many partitions |
| `user_id` | millions | ❌ "millions of partitions" — cluster failure |

### `schema-partition-query-tradeoffs` (Medium)

Partitioning cuts both ways:

- **Helps:** queries whose `WHERE` includes the partition expression prune to one or a few partitions.
- **Hurts:** queries that span many partitions pay coordination overhead and merge-state checks for each one.

```sql
-- Helped: prunes to one partition
WHERE timestamp >= '2024-01-01' AND timestamp < '2024-02-01'

-- Not helped: scans every partition's parts
WHERE event_type = 'click'
```

### `schema-partition-start-without` (Medium)

Default to **no partitioning** when you don't have a clear lifecycle requirement. Adding partitioning later is doable (recreate + insert-as-select); removing it is the same work.

| Need | Partition? |
|---|---|
| Time-based retention | Yes |
| Archive to cold storage | Yes |
| Query latency on time ranges | Maybe — benchmark first |
| No specific lifecycle | No |

## Common partition keys

```sql
-- Monthly: most common, bounded cardinality
PARTITION BY toYYYYMM(event_time)

-- Daily: surgical pruning for time-window queries; watch part count
PARTITION BY toDate(event_time)

-- Bucketed by user (for per-user dropping policies)
PARTITION BY (user_id % 16)

-- No partitioning: simplest, often best
-- (omit the PARTITION BY clause)
```

## TTL — the real reason partitioning earns its keep

`TTL` rules let ClickHouse take action when data ages out of a window. Multiple rules can stack on the same table.

```sql
CREATE TABLE events (
    ts DateTime,
    user_id UInt64,
    event_type LowCardinality(String)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (user_id, ts)
TTL
    ts + INTERVAL  7 DAY  TO VOLUME 'warm_hdd',
    ts + INTERVAL 90 DAY  TO VOLUME 'cold_s3',
    ts + INTERVAL 90 DAY  RECOMPRESS CODEC(ZSTD(9)),
    ts + INTERVAL 365 DAY DELETE;
```

### TTL actions <a name="ttl-actions"></a>

| Action | Effect |
|---|---|
| `DELETE` (default) | Removes expired rows |
| `TO DISK 'name'` | Move parts to a named disk |
| `TO VOLUME 'name'` | Move parts to a volume (a group of disks) |
| `RECOMPRESS CODEC(...)` | Recompress in place with the new codec |
| `GROUP BY ... SET col = aggr(col)` | Pre-roll-up: aggregate then delete |

### Column-level TTL

Expire individual columns (e.g., PII fields) without dropping the row:

```sql
CREATE TABLE users (
    id UInt64,
    pii_email String TTL created_at + INTERVAL 30 DAY,
    created_at DateTime
) ENGINE = MergeTree ORDER BY id;
```

After 30 days, `pii_email` is replaced with the type default (empty string). On-disk size doesn't shrink until the next merge.

### Failure modes

- **TTL is asynchronous.** Moves and deletes happen during background merges, not at the timestamp tick. There can be a multi-hour delay.
- **`OPTIMIZE TABLE … FINAL` forces TTL evaluation** but is expensive and unsafe in production. Avoid (rule: `insert-optimize-avoid-final`).
- **Recompress doesn't shrink immediately.** The new codec applies on the next merge of that part.

## Validation

```sql
-- Partition health: count, parts, size, row count
SELECT
    partition,
    count()                                 AS parts,
    sum(rows)                               AS rows,
    formatReadableSize(sum(bytes_on_disk))  AS size
FROM system.parts
WHERE active AND database = currentDatabase() AND table = 'events'
GROUP BY partition
ORDER BY partition;

-- Warning sign: hundreds of partitions on one table
```

```sql
-- Confirm partition pruning happened
EXPLAIN indexes = 1
SELECT count() FROM events
WHERE event_time >= '2024-01-01' AND event_time < '2024-02-01';
-- Look for "Selected N parts by partition key"
```

```sql
-- See active TTL rules for a table
SELECT name, engine_full
FROM system.tables
WHERE database = currentDatabase() AND name = 'events';
```

## Pitfalls

- **Partitioning by `user_id` or `event_id`.** Cardinality explodes; cluster fails on insert.
- **Partitioning when retention is "forever".** No lifecycle = no benefit; just storage overhead and merge fragmentation.
- **Partitioning to "speed up" a query that's slow because of a bad `ORDER BY`.** Fix the `ORDER BY` first. Partitioning is not a substitute for the primary index.
- **`DELETE FROM` before `DROP PARTITION` for time-based cleanup.** You're paying row-by-row scan when one metadata call would do.
- **Aligning `ORDER BY` and `PARTITION BY` strangely.** They're independent. `ORDER BY` controls the sort within a partition; `PARTITION BY` controls partition assignment.

## How this project tests it

`compare-features --comparison partitioning` builds four `events` variants — `no PARTITION`, `toYYYYMM`, `toDate`, `user_id % 16` — and runs `recent_window`, `specific_day`, `user_lookup`, and a `parts_count` query against each. The fourth query is critical: it surfaces part-count differences directly, which is the operational risk of bad partitioning.

```bash
uv run clickhouse-bench compare-features --comparison partitioning
```

## Sources

- ClickHouse, *Choosing a Partitioning Key*. <https://clickhouse.com/docs/best-practices/choosing-a-partitioning-key>
- ClickHouse, *MergeTree TTL*. <https://clickhouse.com/docs/en/engines/table-engines/mergetree-family/mergetree#table_engine-mergetree-ttl>
- Project skill rules: `schema-partition-lifecycle`, `schema-partition-low-cardinality`, `schema-partition-query-tradeoffs`, `schema-partition-start-without`
