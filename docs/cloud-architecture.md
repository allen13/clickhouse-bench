# 12. ClickHouse Cloud architecture

> **Tier 4 — Context.** Not a knob you turn. The realities of running on ClickHouse Cloud — `SharedMergeTree`, compute/storage separation, idle scaling — change which OSS practices apply and which need adjustment.

## `SharedMergeTree` is the real engine

In ClickHouse Cloud, `ENGINE = MergeTree` (and every variant: `AggregatingMergeTree`, `ReplacingMergeTree`, etc.) **automatically maps to `SharedMergeTree` (SMT)**. You don't write `SharedMergeTree` in DDL — it's transparent.

What's different from the OSS `ReplicatedMergeTree`:

| Feature | `ReplicatedMergeTree` (OSS) | `SharedMergeTree` (Cloud) |
|---|---|---|
| Metadata location | Each replica | ClickHouse-Keeper (centralised) |
| Replication model | Synchronous, leader-based | Asynchronous, leaderless |
| Insert quorum | Needs `insert_quorum` setting | All inserts are inherently quorum |
| Adding replicas | Resharding required | Instant — Keeper hands new node the metadata |
| Replica count | Typically 2–3 | Hundreds per table possible |
| `system.replication_queue` | Present | **Replaced by `system.virtual_parts`** |

> "All communication happens through shared storage and ClickHouse-Keeper" — no peer-to-peer replica sync.

### Practical consequences

- **Compute and storage are independent.** All data lives in object storage (S3/GCS/Azure). Local SSD on each compute node is a transparent read cache. Adding compute does not require moving data.
- **System tables you might know are missing.** `system.replication_queue` doesn't exist; scripts that query it will error. Use `system.virtual_parts` for the equivalent view.
- **Quorum behaviour is automatic.** Don't set `insert_quorum`; SMT handles consistency through Keeper.
- **Replicas are stateless compute.** Killing one is a no-op for data; the SSD cache rebuilds on next query.

## Compute / storage separation

Reads execute on the compute node and pull from object storage when the SSD cache misses. This means:

- **First query of the day is slower** — SSD cache cold-start. Don't include the first run in your benchmark numbers.
- **Object-storage round-trip dominates for cold scans** — typical S3 first-byte latency is 30–50 ms. Subsequent reads of the same parts are SSD-fast.
- **Parts can be larger than local disk** — only the hot working set lives on SSD; the rest stays in S3.

For benchmarking: always run a **warm-up pass** before measuring. The project's benchmark runner already does this via the `--warm-runs` flag.

## Idle scaling <a name="idle-scaling"></a>

Per the service definition for this account: `idleTimeoutMinutes = 15`, `idleScaling = true`. Behaviour:

- After 15 minutes with no queries, the service auto-suspends to zero compute.
- Cost: storage (object storage + Keeper metadata) only.
- First query after suspension wakes the service. Cold-start is in the **~10 second** range.
- The first query also pays for SSD cache warm-up on whatever parts it touches.

### Practical guidance

- **For benchmarking sessions:** issue a no-op query (e.g., `SELECT 1`) at the start to wake the service before timed runs.
- **For dashboards:** keep something hitting the service every 10 minutes if first-query latency matters to users.
- **For dev:** let it idle. The cost saving is real.

The trial on this account has 29 days remaining (per the service summary).

## Service profile / sizing

Current service:

- **Provider:** AWS `us-east-1`
- **Replicas:** 3
- **Memory per replica:** 16 – 120 GB (auto-scales)
- **Total memory:** 48 – 360 GB
- **ClickHouse version:** 25.12
- **MCP enabled:** yes (per the [Cloud per-service `mcpEnabled` toggle](memory:project_clickhouse_mcp_setup.md))
- **IP allow list:** `0.0.0.0/0` (open trial — change before storing real data)

Memory auto-scaling is the headline operational difference from OSS — the service grows under load, shrinks when idle. Don't pin it; trust the scaler.

## What this means for the docs in this folder

- **All `MergeTree` advice (`primary-key.md`, `data-types.md`, codecs, etc.) applies unchanged.** The on-disk layout is the same; only the metadata path changed.
- **`OPTIMIZE TABLE … FINAL` is even worse on Cloud** — it pulls all parts from S3 to merge. Avoid it harder.
- **Parallel replicas are practical** — three replicas already exist. See [parallel-replicas.md](parallel-replicas.md).
- **Replica-affinity is something you usually don't need to worry about.** Cloud routes queries; SMT handles consistency.

## Sources

- ClickHouse, *SharedMergeTree*. <https://clickhouse.com/docs/en/cloud/reference/shared-merge-tree>
- Project memory: `project_clickhouse_mcp_setup.md` — `mcpEnabled` is a per-service Cloud toggle, console-only.
