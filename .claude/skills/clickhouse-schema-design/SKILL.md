---
name: clickhouse-schema-design
description: Design a ClickHouse `CREATE TABLE` statement from the data's shape and query pattern. Use when the user asks "how should I model this in ClickHouse", proposes a schema for review, describes a workload that needs a table, or wants to translate a Postgres/MySQL DDL into a ClickHouse-shaped one. Produces a DDL plus per-decision rationale citing best-practice rules, embedded official documentation, and the project's measured numbers in `clickhouse-shape-matching-brief.pdf`.
---

# ClickHouse schema design from data shape

The recurring lesson from the project's own `clickhouse-shape-matching-brief.pdf` is that **ClickHouse rewards matching the tool to the data's shape**. This skill turns that into a structured design pass.

> Output target: a single `CREATE TABLE` statement plus a numbered rationale, one bullet per decision, each citing the rule and the measurement that supports it. Do not produce vague advice — every choice must be defended.

## Position in the ClickHouse skill ecosystem

This skill complements rather than duplicates the official ClickHouse agent skills. Layer them like this:

| Skill | Question it answers | When to invoke |
|---|---|---|
| [`clickhouse:clickhouse-best-practices`](https://github.com/ClickHouse/agent-skills/tree/main/skills/clickhouse-best-practices) | *Does this DDL/query violate any of the 28 rules?* | Reviewing existing schemas or queries — the rule checker. |
| [`clickhouse-architecture-advisor`](https://github.com/ClickHouse/agent-skills/tree/main/skills/clickhouse-architecture-advisor) | *What architecture pattern fits this workload?* (ingestion strategy, JOIN-vs-dict-vs-denorm, real-time MVs, etc.) | Architecture-level when/why/how decisions; explicitly **not** a schema generator. |
| **This skill** (`clickhouse-schema-design`) | *Given a workload, write the actual `CREATE TABLE`.* | Design-time DDL synthesis with defended choices. |

If the user asks an architecture-level question, defer to `clickhouse-architecture-advisor`. If they want to review an existing schema or query, defer to `clickhouse-best-practices`. This skill is for the moment when the decisions are made and someone has to write the DDL.

## When to invoke

The user is doing one of:
- Asking to design a new ClickHouse table.
- Asking for a review of an existing `CREATE TABLE` — same skill, but you also flag what's wrong with the existing DDL.
- Translating a schema from another database (Postgres, MySQL, BigQuery) to ClickHouse.
- Asking "how should I store / model X in ClickHouse" where X is described in business terms.

Do **not** invoke for:
- Query tuning questions (use the `clickhouse:clickhouse-best-practices` skill).
- Operational questions (mutations, OPTIMIZE FINAL, partitioning maintenance — use the rules directly).

## How to apply

Work the design as a six-step decision tree. **Ask only the questions you can't already answer from what the user has told you.** If they've already described the workload in detail, skip ahead. If they haven't, gather the missing pieces with a single grouped follow-up — never a one-question-at-a-time interrogation.

### Step 1 — Name the shape

Before any DDL, write down (in your response) what shape of data this is:

- **Append-only event stream?** (logs, clicks, sensor readings, transactions). Default engine: `MergeTree`. Mutations rare or never.
- **Slowly-changing dimension?** (users, products, accounts). Records get updated. Default engine: `ReplacingMergeTree(updated_at)`.
- **Pre-aggregation target for an MV?** Default engine: `AggregatingMergeTree`.
- **Soft-delete pattern?** Records get cancelled, not deleted. Default engine: `CollapsingMergeTree(sign)` or `VersionedCollapsingMergeTree(sign, version)` if writes can arrive out of order.
- **Time-series with retention rules?** `MergeTree` with `PARTITION BY toYYYYMM(...)` and `TTL`.
- **Lookup table joined many times to facts?** Probably not a table at all — a `Dictionary` (see the joins-and-dictionaries lesson; measured `-37%` memory in `clickhouse-shape-matching-brief.pdf` §5).

If the user hasn't said which one this is, that's the first question to ask.

### Step 2 — Plan the `ORDER BY` (because it's immutable)

`ORDER BY` cannot be changed after table creation. Picking it wrong means a full data migration to fix. From the `clickhouse-shape-matching-brief.pdf` §2 measurement: at 10M rows each variant of the same table won the query that aligned with its leading column, with 7% storage variance.

Ask the user (if not already known):
1. The top 5–10 query shapes that will hit this table — `WHERE` columns specifically.
2. Of those, which run most often or scan the most data.

Then design `ORDER BY` as `(low-cardinality, …, high-cardinality)`:

- **Position 1**: lowest-cardinality column that appears in most queries' `WHERE` (e.g., `tenant_id`, `event_type`, `country`).
- **Position 2**: a date or coarse-time column (`toDate(event_time)`, not the raw `DateTime`, to shrink the index).
- **Position 3+**: medium-to-high cardinality (`user_id`, `session_id`).
- **Last**: high-cardinality (`event_id`, `uuid`) only if needed for uniqueness.

Limit to 4–5 columns. Per `schema-pk-cardinality-order` and `schema-pk-prioritize-filters`.

### Step 3 — Pick types and codecs from each column's value shape

For each column, write down two things: **the value type** and **the value distribution within the column**. The first decides the type; the second decides the codec.

| If the values are…                         | Type                                  | Codec hint                     |
|---|---|---|
| Sequential IDs (UInt32/64 sized)           | `UInt32`/`UInt64`                     | `Delta` if monotonic; else default LZ4 |
| UUIDs / hashes                             | `UUID`                                | LZ4 (default). Specialised codecs hurt — `clickhouse-shape-matching-brief.pdf` §4 measured `0.9×` (worse than nothing) under `DoubleDelta` |
| Timestamps                                 | `DateTime` (or `Date` for day-grain)  | `DoubleDelta, LZ4` for monotone time-series; `clickhouse-shape-matching-brief.pdf` §4 measured **850×** compression on monotonic UInt64 |
| Floats with slow drift (gauges)            | `Float64`                             | `Gorilla`/`ALP` if available; benchmark — see `docs/codecs-compression.md` |
| Status, country, event-type, etc. (<10K distinct) | `LowCardinality(String)`       | LZ4 (default) |
| Free-text                                  | `String`                              | `ZSTD(3)` if size matters     |
| Money                                      | `Decimal(P, S)` or `Int64` (cents)    | LZ4 |
| Booleans                                   | `Bool`                                | LZ4 |
| Counts in narrow range (HTTP codes, ages)  | `UInt8`/`UInt16`                      | `T64, LZ4` for narrow integers |

Per-rule citations: `schema-types-native-types`, `schema-types-minimize-bitwidth`, `schema-types-lowcardinality`. From `clickhouse-shape-matching-brief.pdf` §3: native types use **32% less raw I/O** than `String`-for-everything.

**Avoid `Nullable(...)`** unless the *absence* of a value is itself information (e.g., `deleted_at` where NULL = "not deleted"). Use `DEFAULT` values for "unknown" — empty string, 0, `now()`. Per `schema-types-avoid-nullable`.

### Step 4 — Decide on partitioning (carefully, or not at all)

**Default position**: don't partition. Per `schema-partition-start-without`. Add partitioning only if you have a real lifecycle requirement.

Use partitioning when:
- You'll `DROP PARTITION` whole time-windows of data (retention).
- You want `TO VOLUME 'cold'` to migrate aged data to cheaper storage.
- You'll archive partitions to a sibling table.

Cardinality cap: **100–1,000 distinct partitions total**. Day-partitioning over multi-year data triggers `TOO_MANY_PARTS` on the very first INSERT — see `clickhouse-shape-matching-brief.pdf`'s bonus section, where the server emits its own canonical error message. Per `schema-partition-low-cardinality`.

| If the data lifecycle is…                  | Partition by             |
|---|---|
| Multi-year, monthly retention windows      | `toYYYYMM(timestamp)`    |
| ~1 year with daily retention               | `toDate(timestamp)` (only if total partitions stays bounded) |
| Tenant-scoped with fast tenant-drop        | `tenant_id` (only if cardinality ≤ ~1,000) |
| No specific lifecycle                      | **Don't partition.** |

### Step 5 — Decide on projections / materialised views / skip indexes

Default: **none**. Add only when a measured query is the bottleneck.

- **Materialised view** when the same aggregation is asked repeatedly (dashboard refresh). Build a target `AggregatingMergeTree` + an MV trigger. From `clickhouse-shape-matching-brief.pdf` §5: **4.8× faster** at 10M rows for the dashboard query. Use `*State` aggregate functions in the MV definition and `*Merge` in the read query.
- **Projection** when you want a second sort order or pre-aggregation co-located with the base table, with atomic consistency. Cleaner DDL than an MV but doesn't compose across tables.
- **Skip index** (`bloom_filter`, `set`, `minmax`) when a query filters on a non-`ORDER BY` column AND the matching values cluster with the primary key. Add only after `EXPLAIN ESTIMATE` confirms the primary index alone can't help.

### Step 6 — Consider TTL for cost optimisation

If the user mentions cold-data costs, retention obligations, or PII expiration, propose:

- `TTL ts + INTERVAL N DAY DELETE` for whole-row expiry.
- `TTL ts + INTERVAL N DAY TO VOLUME 'cold_s3'` for tiered storage.
- `column TTL ts + INTERVAL N DAY` for column-level expiry (e.g., PII).

TTL moves are async — they happen during background merges, not at the timestamp tick. Don't promise instant.

## Output format

Always produce the DDL **and** the rationale, in this shape:

````
## Proposed schema

```sql
CREATE TABLE <name> (
    -- columns, with codec choices inline
)
ENGINE = <engine>
ORDER BY (...)
[PARTITION BY ...]
[TTL ...]
[SETTINGS ...];
```

## Rationale

1. **Engine: `<engine>`** — because <shape>. Per `<rule>`. Measured: <number from clickhouse-shape-matching-brief.pdf if applicable>.
2. **`ORDER BY (...)`** — because <query shape>. Per `schema-pk-cardinality-order`.
3. **`<col1>` `<type>` `CODEC(...)`** — because the values are <shape: constant / monotonic / random / low-cardinality / etc.>. Per `<rule>`.
4. ... (one bullet per non-obvious choice; skip the obvious ones)
5. **No partitioning** — because <reason>; or **`PARTITION BY ...`** — because <lifecycle reason>.
6. **(Optional) Materialised view** to accelerate `<query>` from raw scan to pre-aggregated read.

## Things I assumed (please correct any)

- Approximate cardinality of `<column>`: <range>.
- Update pattern: <append-only / slowly-changing / mutating>.
- Retention: <forever / N days / N months>.
- Top query shape: `<WHERE clause>`.
````

If the user gave you a **review** request (an existing DDL), structure your response as:

````
## What's good

- ...

## What I'd change

1. **Drop `Nullable` on `<col>`** — values look populated; null map costs storage and CPU. Per `schema-types-avoid-nullable`.
2. **Reorder `ORDER BY`** — `<col>` is high-cardinality and shouldn't lead. Per `schema-pk-cardinality-order`.
3. ...

## What I can't tell from the DDL alone

- Cardinality of `<col>`: tell me, and I'll either confirm `LowCardinality(String)` or switch to plain `String`.
- ...
````

## Worked example

A user says: *"I'm tracking customer-service call transcripts. Each call has an ID, a customer ID, an agent ID, a start time, a duration, a sentiment score, and a free-text transcript. We get ~50K calls/day, will keep 18 months of data. Most queries are 'all calls for customer X over the last 30 days' or 'sentiment distribution by agent for last week'."*

Expected output:

```sql
CREATE TABLE call_transcripts (
    call_id     UInt64,
    customer_id UInt64,
    agent_id    UInt32,
    sentiment   Int8,                                -- -100 .. +100, fits Int8
    duration_s  UInt32 CODEC(T64, LZ4),
    started_at  DateTime CODEC(DoubleDelta, LZ4),
    transcript  String CODEC(ZSTD(3))
)
ENGINE = MergeTree()
ORDER BY (customer_id, started_at, call_id)
PARTITION BY toYYYYMM(started_at)
TTL started_at + INTERVAL 18 MONTH DELETE;
```

**Rationale:**
1. **`MergeTree`** — append-only event log; calls don't get updated.
2. **`ORDER BY (customer_id, started_at, call_id)`** — top query is "calls for customer X over a date range", so `customer_id` leads. `started_at` second to enable date-range pruning within a customer. `call_id` last for uniqueness.
3. **`customer_id UInt64`** instead of `String` — 32% less raw I/O on every scan (`clickhouse-shape-matching-brief.pdf` §3).
4. **`sentiment Int8`** — score range -100..+100 fits easily; `Int8` is 8× smaller than `Int64`. Per `schema-types-minimize-bitwidth`.
5. **`started_at CODEC(DoubleDelta, LZ4)`** — monotonically-increasing timestamps; `clickhouse-shape-matching-brief.pdf` §4 measured 850× compression on monotonic UInt64. Same family wins for DateTime.
6. **`duration_s CODEC(T64, LZ4)`** — narrow integer; T64 strips zero high bytes.
7. **`transcript CODEC(ZSTD(3))`** — free text compresses well under ZSTD; level 3 is the sweet spot per docs.
8. **`PARTITION BY toYYYYMM(started_at)`** — 18-month retention × monthly = 18 partitions, well within the 100–1,000 cap. Lets you `DROP PARTITION '202401'` for the rolling-window cleanup.
9. **`TTL ... + INTERVAL 18 MONTH DELETE`** — automatic expiry; runs during background merges.
10. **No skip index, no MV yet** — add when `EXPLAIN ESTIMATE` shows a real bottleneck. The `ORDER BY` already covers the top query.

**Things I assumed (please correct):**
- `agent_id` cardinality fits in `UInt32` (4.3B agents). If <65K, drop to `UInt16`.
- Sentiment fits in `Int8`. If you need finer than 1-point granularity, use `Int16` or `Float32`.
- Transcripts are kept verbatim. If you only need full-text search, consider a `tokenbf_v1` skip index later.

## Embedded canonical guidance

This section embeds distilled official guidance from `clickhouse.com/docs/best-practices/*` so the skill works offline and with explicit citations. Treat these as authoritative; the rest of this skill operationalises them.

### Choosing a primary key (`ORDER BY`)

> Source: <https://clickhouse.com/docs/best-practices/choosing-a-primary-key>

ClickHouse primary keys are **fundamentally different** from OLTP databases. The key determines both the sparse-index structure and physical row order on disk, and it directly drives query performance and compression. Priority order for selection:

1. **Filter columns first.** Prioritize columns frequently used in `WHERE`, especially those that exclude large numbers of rows.
2. **Cardinality matters: low-to-high.** Place lower-cardinality columns first. The official Stack Overflow example orders on `PostTypeId` (cardinality 8) before date components.
3. **Correlation matters.** Columns correlated with each other improve compression and aggregation/sorting efficiency.
4. **Coarsen temporal granularity.** Prefer `toDate(timestamp)` over raw `DateTime` when day-grain filtering suffices — the index gets smaller without losing pruning power.

**Critical constraint.** *"Ordering keys must be defined on table creation and can't be added."* Adding a different ordering later requires a projection (which duplicates data) or a full table migration. Plan upfront — typically 4–5 keys suffice.

**Concrete impact (per the docs).** Stack Overflow demo: an unordered scan touched **59.82M rows in 0.055s**; the same query on a table with `ORDER BY (PostTypeId, toDate(CreationDate))` touched **196.53K rows in 0.013s** — a 4× speed-up via index-based granule pruning.

All non-key columns are still physically ordered by the key — so a thoughtful key orders the entire dataset cohesively.

### Selecting data types

> Source: <https://clickhouse.com/docs/best-practices/select-data-types>

ClickHouse achieves its compression and scan performance largely through type fit. Five operational rules:

1. **Use native types over `String`.** *"Numeric and date fields should use appropriate numeric and date types rather than general-purpose String types."* Native types enable type-specific codecs and correct comparison semantics.
2. **Minimize bit-width.** Pick the smallest type that fits your range. `UInt16` instead of `Int32` when 0–65,535 covers it.
3. **Optimize date/time precision.** Prefer `DateTime` over `DateTime64` unless sub-second precision is genuinely needed.
4. **`LowCardinality(String)` for <10K distinct values.** Dictionary-encoded; storage and `GROUP BY` both win.
5. **`Enum8`/`Enum16` for truly finite, schema-time-known value sets.** 1–2 byte storage, insert-time validation, natural ordering.
6. **Avoid `Nullable(...)`.** *"Nullable creates a separate column of UInt8 type"* for tracking — extra storage, slower scans. Use `DEFAULT` values or treat absence as empty string / 0 / `now()`. Reserve `Nullable` for cases where the absence is itself information (`deleted_at`, `parent_id`).

The recommended workflow: load a representative sample, run `uniq()` and `min()/max()` on each column, then commit to types based on the observed distribution.

### Choosing a partitioning key

> Source: <https://clickhouse.com/docs/best-practices/choosing-a-partitioning-key>

**Partitioning is primarily a data-management technique, not a query-optimization tool.** Pick the partition key to match retention/archive/tiered-storage needs, not to speed up `WHERE` clauses (the `ORDER BY` does that).

**Cardinality rule.** *"A low-cardinality partitioning key — with fewer than 100–1,000 distinct values — is usually optimal."* Higher cardinality proliferates parts, blocks effective merging, and eventually triggers `TOO_MANY_PARTS`. ClickHouse auto-builds a MinMax index on partition columns so query-time pruning is a side effect when the access pattern aligns.

**Default position.** *"If you're unsure whether partitioning is necessary, you may want to start without it and optimize later based on observed access patterns."* Querying across many partitions can be **slower** than querying an unpartitioned table because of fragmentation.

**Bottom line.** Partition only when:
- You'll `DROP PARTITION` whole time-windows (retention).
- You'll move aged partitions to cheaper storage (`TTL ... TO VOLUME`).
- You'll archive partitions to a sibling table.

Otherwise, don't.

## See also

- **Local pinned copies of the two official skills** — at `docs/references/clickhouse-skills/`:
  - `clickhouse-best-practices/` — 33 prioritised rules (rules/ subdirectory has one .md per rule; cite by name).
  - `clickhouse-architecture-advisor/` — 5 decision frameworks + worked examples for finserv / observability / SIEM workloads.
  - `PROVENANCE.md` — upstream commit, capture date, refresh instructions.
- `clickhouse-shape-matching-brief.pdf` — measured numbers cited above (`§3` types, `§4` codecs/shape, `§5` MV, `§6` dictionaries, `§7` cache, `§8` mutations).
- `docs/priority-list.md` — the ranked feature playbook.
- `docs/merge-tree.md`, `docs/primary-key.md`, `docs/data-types.md`, `docs/codecs-compression.md`, `docs/partitioning-and-ttl.md` — the per-feature deep dives.
- `clickhouse:clickhouse-best-practices` skill (installed) — the 28 rules cited inline.
- [ClickHouse, *Introducing Agent Skills*](https://clickhouse.com/blog/introducing-clickhouse-agent-skills) — the umbrella announcement of the ecosystem this skill plugs into.
