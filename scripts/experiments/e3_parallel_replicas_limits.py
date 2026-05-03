"""E3 — Parallel replicas: when it doesn't apply.

The docs claim parallel replicas are incompatible with FINAL and with
projections. This script proves both with EXPLAIN PIPELINE evidence.

For each case we look for whether the plan contains a
``ReadFromRemoteParallelReplicas`` step (which means parallel replicas
*did* apply) or ``ReadFromMergeTree`` (which means it fell back to a
single-replica read).

Reference: docs/parallel-replicas.md, docs/projections.md.
"""
from __future__ import annotations

from scripts.experiments._lib import (
    get_client, server_summary, temp_tables, write_result,
)


PROJ_TABLE = "exp_pr_proj_events"
RMT_TABLE = "exp_pr_rmt_users"


def _explain_pipeline(client, sql: str, *, settings: dict) -> list[str]:
    res = client.query(f"EXPLAIN PIPELINE {sql}", settings=settings)
    return [r[0] for r in res.result_rows]


def main() -> None:
    client = get_client()
    server = server_summary(client)
    payload: dict = {
        "title": "Parallel replicas: incompatibilities with FINAL and projections",
        "server": server,
    }

    pr_settings = {
        "enable_analyzer": 1,
        "enable_parallel_replicas": 1,
        "max_parallel_replicas": 3,
        "cluster_for_parallel_replicas": "default",
        "parallel_replicas_min_number_of_rows_per_replica": 1,  # force consideration
    }

    serial_settings = {"enable_analyzer": 1, "enable_parallel_replicas": 0}

    with temp_tables(client, PROJ_TABLE, RMT_TABLE):
        # ── Case 1: plain MergeTree without FINAL or projection (control)
        client.command(f"""
            CREATE TABLE {PROJ_TABLE} (
                id UInt64,
                user_id UInt64,
                event_type LowCardinality(String),
                event_time DateTime,
                PROJECTION daily_event_counts (
                    SELECT toDate(event_time) AS day, event_type, count() AS cnt
                    GROUP BY day, event_type
                )
            ) ENGINE = MergeTree() ORDER BY (user_id, event_time)
        """)
        client.command(f"""
            INSERT INTO {PROJ_TABLE} (id, user_id, event_type, event_time)
            SELECT id, user_id, event_type, event_time FROM events
        """)

        # Plain query (no FINAL, no projection match)
        plain_sql = f"SELECT count() FROM {PROJ_TABLE}"
        payload["plain_serial_pipeline"] = _explain_pipeline(
            client, plain_sql, settings=serial_settings)
        payload["plain_pr_pipeline"] = _explain_pipeline(
            client, plain_sql, settings=pr_settings)

        # Query that matches the projection's GROUP BY shape — should hit projection
        proj_sql = f"""
            SELECT toDate(event_time) AS day, event_type, count()
            FROM {PROJ_TABLE}
            GROUP BY day, event_type
        """
        payload["projection_pr_pipeline"] = _explain_pipeline(
            client, proj_sql, settings=pr_settings)

        # ── Case 2: ReplacingMergeTree FINAL
        client.command(f"""
            CREATE TABLE {RMT_TABLE} (
                id UInt64,
                name String,
                updated_at DateTime DEFAULT now()
            ) ENGINE = ReplacingMergeTree(updated_at) ORDER BY id
        """)
        client.command(f"""
            INSERT INTO {RMT_TABLE} (id, name)
            SELECT number, 'user_' || toString(number) FROM numbers(1000)
        """)

        final_sql = f"SELECT count() FROM {RMT_TABLE} FINAL"
        payload["final_serial_pipeline"] = _explain_pipeline(
            client, final_sql, settings=serial_settings)
        payload["final_pr_pipeline"] = _explain_pipeline(
            client, final_sql, settings=pr_settings)

        # ── Compatibility verdict
        def _matches_pr(lines: list[str]) -> bool:
            joined = "\n".join(lines)
            return "ReadFromRemoteParallelReplicas" in joined

        payload["compat"] = {
            "plain": _matches_pr(payload["plain_pr_pipeline"]),
            "projection_match": _matches_pr(payload["projection_pr_pipeline"]),
            "final": _matches_pr(payload["final_pr_pipeline"]),
        }

    write_result("e3_parallel_replicas_limits", payload)


if __name__ == "__main__":
    main()
