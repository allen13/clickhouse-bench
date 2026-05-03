# 14. Sharding & parallel scan in ClickHouse Cloud

> **Tier 4 — Cloud context.** Cloud's `SharedMergeTree` replaces OSS sharding. The "sharding-on-read" mechanism in Cloud is **parallel replicas**, not `Distributed` engine fan-out. This lesson contrasts the two patterns with measured numbers from the project's experiments.

## The two worlds

| World | How data scales | How reads parallelise |
|---|---|---|
| **OSS ClickHouse** | Shard the table across N nodes; each shard is a `MergeTree` on its own node. | A `Distributed` engine table fans queries across shards. The orchestrator merges the partial results. |
| **ClickHouse Cloud (SharedMergeTree)** | All data lives in shared object storage; nothing to shard. | Parallel replicas split the work granule-by-granule across replicas. The initiating replica merges results. |

Cloud's `default` cluster on this account exposes **1 shard × 3 replicas** (verified via `system.clusters`). There is no sharding to do — the data is in S3 already.

## What this means in practice

- **Don't manually create `Distributed` tables in Cloud** to "scale out" reads. The setup adds coordination overhead for no parallelism benefit, since all your "shards" are virtual layers on the same compute.
- **Use parallel replicas instead** (`enable_parallel_replicas = 1`) when a single query is heavy enough to warrant cross-replica work. See [parallel-replicas.md](parallel-replicas.md).
- **Adding capacity** in Cloud is "add another replica" — Keeper hands it the metadata, the SSD cache primes on first query, and it's serving. No resharding.

## Measured: manual sharding has no Cloud upside

We ran a 4-way "shard split" of the 1M-event table into `exp_events_shard_0` … `exp_events_shard_3` (split by `user_id % 4`) and a `Merge`-engine fan-out table over them, then ran the same `SELECT event_type, count() FROM … GROUP BY event_type` against the unsharded base table and the fan-out:

| Source | Warm-avg latency | Storage |
|---|---|---|
| Unsharded `events` (1M rows, 1 table) | **45.6 ms** | 32.1 MiB |
| 4-way Merge fan-out (4 × 250K rows) | 47.6 ms | 7.4 MiB × 4 = 29.6 MiB |

The fan-out is **4% slower** than the unsharded read — coordination overhead with no parallelism payoff. Source: [results/experiment_e2_distributed_local_*.json](../results/) (Phase E2).

## Measured: parallel replicas at this scale

At 1M events, **parallel replicas slow the query down** because coordination overhead exceeds the parallelism win:

| Configuration | Warm-avg latency | Speedup |
|---|---|---|
| `enable_parallel_replicas = 0` (single replica, threaded) | **91.8 ms** | baseline |
| `max_parallel_replicas = 2` | 132.2 ms | 0.71× |
| `max_parallel_replicas = 3` | 123.1 ms | 0.75× |

Source: [results/experiment_e1_parallel_replicas_curve_1000000rows_*.json](../results/) (Phase E1). The curve crosses the break-even point as data grows — the docs put the threshold at "tens of millions of rows." Phase F will re-measure after seeding 10M events.

## Measured: parallel-replicas compatibility

Per E3 ([results/experiment_e3_parallel_replicas_limits_*.json](../results/)), running `EXPLAIN PIPELINE` on three query shapes with `enable_parallel_replicas = 1`:

| Query shape | Pipeline shows… | Parallel replicas applied? |
|---|---|---|
| Plain `count()` on a 1K-row `ReplacingMergeTree` | `SourceFromSingleChunk` (resolved at planner) | No — query too cheap to bother |
| `GROUP BY day, event_type` on 1M rows that matches a projection | `ReadFromRemoteParallelReplicas` + projection use in a Union | **Yes** — and the projection was used |
| `SELECT count() FROM … FINAL` on `ReplacingMergeTree` | `ReadFromMergeTree` only | No — fell back to single-replica |

**Notable update vs older docs:** projection use is *not* mutually exclusive with parallel replicas in ClickHouse 25.12 — the optimiser unions a `ReadFromRemoteParallelReplicas` branch with the projection-rewrite branch. This is a recent improvement; older 24.x docs that warn against the combination are stale for current Cloud services.

`FINAL` remains incompatible — confirmed.

## Quick reference

| Want… | Cloud (this service) | OSS analogue |
|---|---|---|
| Distribute one query's CPU across replicas | `SET enable_parallel_replicas = 1` | `Distributed` engine over sharded tables |
| Add more compute | Add a replica (idle scaling does it) | Add a node, reshard data to balance |
| Drop old data fast | `ALTER TABLE … DROP PARTITION` | Same |
| Storage capacity scales independently of compute | Yes (object storage) | No — bound to disks per node |
| Quorum on writes | Automatic via Keeper | `insert_quorum` per write |

## Validation

```sql
-- Check the cluster topology you actually have
SELECT cluster, count(DISTINCT shard_num) AS shards,
       count(DISTINCT replica_num) AS replicas
FROM system.clusters
GROUP BY cluster
ORDER BY cluster;
-- Cloud: shows 1 shard, N replicas. OSS clusters can show many shards.

-- Confirm a query went parallel
EXPLAIN PIPELINE
SELECT toDate(event_time) AS day, count() FROM events GROUP BY day
SETTINGS enable_parallel_replicas = 1, max_parallel_replicas = 3,
         cluster_for_parallel_replicas = 'default';
-- Look for "ReadFromRemoteParallelReplicas".

-- Confirm replicas-share-storage (Cloud only)
SELECT engine FROM system.tables WHERE name = 'events';
-- Returns 'SharedMergeTree' even though the DDL said MergeTree.
```

## Pitfalls

- **Trying to manually shard in Cloud.** Coordination overhead with no upside. See E2 above.
- **Setting `max_parallel_replicas` higher than the actual replica count.** Bounded by `system.clusters`; the extra setting is silently truncated.
- **Forgetting `cluster_for_parallel_replicas = 'default'`.** Without it, the parallel-replicas planner has no cluster to plan against and falls back to serial.
- **Expecting `Distributed` engine to magically scale on Cloud.** It works (you can create the engine) but it adds latency, not throughput, on a single-shard service.

## Sources

- ClickHouse, *SharedMergeTree*. <https://clickhouse.com/docs/en/cloud/reference/shared-merge-tree>
- ClickHouse, *Parallel Replicas*. <https://clickhouse.com/docs/deployment-guides/parallel-replicas>
- Project measurements: [results/experiment_e1_parallel_replicas_curve_*.json](../results/), [results/experiment_e2_distributed_local_*.json](../results/), [results/experiment_e3_parallel_replicas_limits_*.json](../results/).
- Cross-references: [cloud-architecture.md](cloud-architecture.md), [parallel-replicas.md](parallel-replicas.md), [observability.md#explain](observability.md#explain).
