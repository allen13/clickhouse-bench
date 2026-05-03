"""E2 — Distributed table across local 'shards'.

Cloud's SharedMergeTree exposes a single shard with N replicas. This
script demonstrates the OSS sharding mechanism by manually creating
N local tables and a Distributed engine that fans queries out to them,
contrasting that pattern with the unsharded base table.

The point isn't horizontal-scale-out (all four 'shards' live on the
same compute) — it's to show the wire-up the OSS world uses, and to
make the case for why SharedMergeTree's auto-parallelism is preferable
in Cloud.

Reference: docs/sharding-and-distributed.md (this experiment is the
source for that lesson's measured numbers).
"""
from __future__ import annotations

from scripts.experiments._lib import (
    get_client, run_query, server_summary, temp_tables, write_result,
)


N_SHARDS = 4
SHARD_TABLES = [f"exp_events_shard_{i}" for i in range(N_SHARDS)]
DIST_TABLE = "exp_events_dist"


def _warm_avg(runs: list[dict]) -> float:
    return round(sum(r["wall_ms"] for r in runs[1:]) / max(len(runs) - 1, 1), 2)


def main() -> None:
    client = get_client()
    server = server_summary(client)
    payload: dict = {
        "title": "Distributed table across local shards (educational)",
        "server": server,
        "shards": N_SHARDS,
    }

    with temp_tables(client, *SHARD_TABLES, DIST_TABLE):
        # 1. Create N local shard tables. Same schema as the base events table.
        for i, t in enumerate(SHARD_TABLES):
            client.command(f"""
                CREATE TABLE {t} (
                    id UInt64,
                    user_id UInt64,
                    event_type LowCardinality(String),
                    page String,
                    session_id String,
                    properties String,
                    event_time DateTime,
                    created_at DateTime DEFAULT now()
                ) ENGINE = MergeTree()
                ORDER BY (user_id, event_time)
            """)
            # Populate disjoint slice by user_id % N
            client.command(f"""
                INSERT INTO {t}
                SELECT * FROM events
                WHERE (user_id % {N_SHARDS}) = {i}
            """)

        # 2. Distributed engine table that fans out across the N shards.
        # Cluster 'default' has 3 replicas; the Distributed engine doesn't
        # need a multi-shard cluster — it can point at multiple local tables
        # via the cluster_local() pattern. Simpler: use a UNION view.
        # In OSS you'd use ENGINE = Distributed; here we approximate with
        # a Merge engine since Distributed-to-local isn't trivial in Cloud.
        client.command(f"""
            CREATE TABLE {DIST_TABLE} AS exp_events_shard_0
            ENGINE = Merge(currentDatabase(), '^exp_events_shard_[0-{N_SHARDS-1}]$')
        """)

        # 3. Same query against unsharded events vs the Merge fan-out.
        sql_base = """
            SELECT event_type, count() AS c
            FROM events
            GROUP BY event_type
            ORDER BY c DESC
        """
        sql_merge = sql_base.replace("FROM events", f"FROM {DIST_TABLE}")

        base_runs = [run_query(client, sql_base) for _ in range(6)]
        merge_runs = [run_query(client, sql_merge) for _ in range(6)]

        # Storage tally
        sizes = client.query(f"""
            SELECT table, sum(rows), formatReadableSize(sum(bytes_on_disk))
            FROM system.parts
            WHERE active AND (table = 'events' OR table LIKE 'exp_events_shard_%')
            GROUP BY table
            ORDER BY table
        """).result_rows

        payload["base_unsharded"] = {
            "warm_avg_ms": _warm_avg(base_runs),
            "runs": base_runs,
        }
        payload["merge_fanout"] = {
            "warm_avg_ms": _warm_avg(merge_runs),
            "runs": merge_runs,
        }
        payload["storage"] = [
            {"table": r[0], "rows": r[1], "size": r[2]} for r in sizes
        ]
        payload["base_to_merge_ratio"] = round(
            payload["base_unsharded"]["warm_avg_ms"]
            / max(payload["merge_fanout"]["warm_avg_ms"], 0.01), 2)

    write_result("e2_distributed_local", payload)


if __name__ == "__main__":
    main()
