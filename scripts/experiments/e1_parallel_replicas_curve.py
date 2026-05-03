"""E1 — Parallel replicas scaling curve.

Run the same heavy aggregation at max_parallel_replicas = 1, 2, 3, against
whatever row count is in the ``events`` table at run time.  Records the
current row count alongside the timings so a downstream aggregator can plot
"speedup vs N" at multiple scales (the script can be re-run after Phase F
seeds 10M events).

Reference: docs/parallel-replicas.md, docs/cloud-architecture.md.
"""
from __future__ import annotations

from scripts.experiments._lib import (
    get_client, run_query, server_summary, warm_run, write_result,
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
    return round(sum(r["wall_ms"] for r in runs[1:]) / max(len(runs) - 1, 1), 2)


def main() -> None:
    client = get_client()
    server = server_summary(client)

    # Capture current dataset size so the aggregator can label this run.
    rowcount = client.query("SELECT count() FROM events").result_rows[0][0]

    payload: dict = {
        "title": "Parallel replicas scaling curve",
        "server": server,
        "events_rows": rowcount,
        "settings": {"warm_runs_per_config": 5},
    }

    base_settings = {"enable_analyzer": 1}

    configs = [
        ("serial", {"enable_parallel_replicas": 0}),
        ("parallel_2", {"enable_parallel_replicas": 1,
                        "max_parallel_replicas": 2,
                        "cluster_for_parallel_replicas": "default",
                        "parallel_replicas_min_number_of_rows_per_replica": 100_000}),
        ("parallel_3", {"enable_parallel_replicas": 1,
                        "max_parallel_replicas": 3,
                        "cluster_for_parallel_replicas": "default",
                        "parallel_replicas_min_number_of_rows_per_replica": 100_000}),
    ]
    for label, cfg in configs:
        runs = warm_run(client, HEAVY_SQL, n=6, prefix=f"e1-{label}",
                        settings={**base_settings, **cfg})
        payload[label] = {
            "settings": cfg,
            "runs": runs,
            "warm_avg_ms": _warm_avg(runs),
        }

    serial_avg = payload["serial"]["warm_avg_ms"] or 0.01
    payload["speedup_2x"] = round(
        serial_avg / max(payload["parallel_2"]["warm_avg_ms"], 0.01), 2)
    payload["speedup_3x"] = round(
        serial_avg / max(payload["parallel_3"]["warm_avg_ms"], 0.01), 2)

    write_result(f"e1_parallel_replicas_curve_{rowcount}rows", payload)


if __name__ == "__main__":
    main()
