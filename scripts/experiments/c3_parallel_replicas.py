"""C3 — Parallel replicas speed-up.

Question: how much does ``enable_parallel_replicas = 1`` help on a heavy
aggregation query at scale=100K? Also confirm the docs' incompatibility
claim with FINAL by running a query with FINAL and checking
EXPLAIN PIPELINE.

Reference: docs/parallel-replicas.md.
"""
from __future__ import annotations

from scripts.experiments._lib import (
    get_client, log_metrics, new_query_id, run_query, server_summary,
    warm_run, write_result,
)


HEAVY_SQL = """
    SELECT
        event_type,
        toDate(event_time) AS day,
        count() AS events,
        uniqExact(user_id) AS unique_users
    FROM events
    GROUP BY event_type, day
    ORDER BY day, events DESC
"""


def _warm_avg(runs: list[dict]) -> float:
    warm = runs[1:]
    return round(sum(r["wall_ms"] for r in warm) / len(warm), 2)


def main() -> None:
    client = get_client()
    server = server_summary(client)

    payload: dict = {
        "title": "Parallel replicas speed-up",
        "server": server,
        "settings": {"warm_runs_per_config": 5},
    }

    base_settings = {"enable_analyzer": 1}

    # 1) Serial baseline (no parallel replicas)
    serial_runs = warm_run(
        client, HEAVY_SQL, n=6, prefix="c3-serial",
        settings={**base_settings, "enable_parallel_replicas": 0},
    )

    # 2) Parallel replicas with max=2
    pr2_runs = warm_run(
        client, HEAVY_SQL, n=6, prefix="c3-pr2",
        settings={**base_settings,
                  "enable_parallel_replicas": 1,
                  "max_parallel_replicas": 2,
                  "cluster_for_parallel_replicas": "default",
                  "parallel_replicas_min_number_of_rows_per_replica": 100000},
    )

    # 3) Parallel replicas with max=3
    pr3_runs = warm_run(
        client, HEAVY_SQL, n=6, prefix="c3-pr3",
        settings={**base_settings,
                  "enable_parallel_replicas": 1,
                  "max_parallel_replicas": 3,
                  "cluster_for_parallel_replicas": "default",
                  "parallel_replicas_min_number_of_rows_per_replica": 100000},
    )

    payload["serial"] = {"runs": serial_runs, "warm_avg_ms": _warm_avg(serial_runs)}
    payload["parallel_2"] = {"runs": pr2_runs, "warm_avg_ms": _warm_avg(pr2_runs)}
    payload["parallel_3"] = {"runs": pr3_runs, "warm_avg_ms": _warm_avg(pr3_runs)}
    serial_avg = payload["serial"]["warm_avg_ms"] or 0.01
    payload["speedup_2x"] = round(serial_avg / max(payload["parallel_2"]["warm_avg_ms"], 0.01), 2)
    payload["speedup_3x"] = round(serial_avg / max(payload["parallel_3"]["warm_avg_ms"], 0.01), 2)

    # FINAL incompatibility — try a query that uses FINAL with parallel replicas.
    # We expect it to either error or silently fall back to serial; either way
    # we capture the EXPLAIN PIPELINE output so clickhouse-shape-matching-brief.tex can quote it.
    try:
        final_explain = client.query(
            "EXPLAIN PIPELINE SELECT count() FROM events FINAL WHERE event_type = 'click'",
            settings={**base_settings,
                      "enable_parallel_replicas": 1,
                      "max_parallel_replicas": 3,
                      "cluster_for_parallel_replicas": "default"},
        ).result_rows
        payload["final_explain_pipeline"] = [r[0] for r in final_explain]
        payload["final_compatible"] = "ReadFromRemoteParallelReplicas" in "\n".join(
            payload["final_explain_pipeline"])
    except Exception as e:
        payload["final_explain_error"] = str(e)
        payload["final_compatible"] = False

    write_result("c3_parallel_replicas", payload)


if __name__ == "__main__":
    main()
