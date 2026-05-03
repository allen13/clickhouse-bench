"""D4 — Projection vs Materialized View head-to-head.

Both pre-aggregate the same daily-event-counts shape:
- Projection: alters the source table to add an aggregating projection.
- MV:        creates a parallel target + a materialized view trigger.

Compare:
- Storage of the pre-aggregated form.
- Read latency for the aggregate query.
- Insert overhead (write a small batch and time it).

Reference: docs/projections.md, docs/materialized-views.md.
"""
from __future__ import annotations

import time

from scripts.experiments._lib import (
    get_client, run_query, server_summary, temp_tables, write_result,
)


SOURCE_PROJ = "exp_events_proj"
SOURCE_MV   = "exp_events_mv_src"
TARGET_MV   = "exp_events_mv_tgt"
MV_NAME     = "exp_events_mv"


READ_QUERY_PROJ = """
    SELECT toDate(event_time) AS day, event_type, count() AS cnt
    FROM exp_events_proj
    GROUP BY day, event_type
    ORDER BY day, event_type
"""

READ_QUERY_MV = """
    SELECT day, event_type, countMerge(cnt) AS cnt
    FROM exp_events_mv_tgt
    GROUP BY day, event_type
    ORDER BY day, event_type
"""


def _warm_avg(runs: list[dict]) -> float:
    return round(sum(r["wall_ms"] for r in runs[1:]) / max(len(runs) - 1, 1), 2)


def _bytes(client, name: str) -> int:
    rows = client.query(f"""
        SELECT sum(bytes_on_disk) FROM system.parts
        WHERE active AND table = '{name}'
    """).result_rows
    return int(rows[0][0] or 0) if rows else 0


def _proj_bytes(client, table: str, proj_name: str) -> int:
    """Sum bytes_on_disk for the projection's sub-parts."""
    rows = client.query(f"""
        SELECT sum(bytes_on_disk) FROM system.projection_parts
        WHERE active AND table = '{table}' AND name = '{proj_name}'
    """).result_rows
    return int(rows[0][0] or 0) if rows else 0


def main() -> None:
    client = get_client()
    server = server_summary(client)
    payload: dict = {
        "title": "Projection vs Materialized View — same pre-aggregation shape",
        "server": server,
    }

    with temp_tables(client, SOURCE_PROJ, SOURCE_MV, TARGET_MV):
        # ── Variant 1: Projection on source table
        client.command(f"""
            CREATE TABLE {SOURCE_PROJ} (
                id UInt64,
                user_id UInt64,
                event_type LowCardinality(String),
                page String,
                session_id String,
                properties String,
                event_time DateTime,
                PROJECTION daily_event_counts (
                    SELECT
                        toDate(event_time) AS day,
                        event_type,
                        count() AS cnt
                    GROUP BY day, event_type
                )
            ) ENGINE = MergeTree() ORDER BY (user_id, event_time)
        """)
        # Insert measurement
        t0 = time.perf_counter()
        client.command(f"""
            INSERT INTO {SOURCE_PROJ}
            SELECT id, user_id, event_type, page, session_id, properties, event_time
            FROM events
        """)
        proj_insert_ms = (time.perf_counter() - t0) * 1000.0

        # ── Variant 2: MV (source + aggregating target + trigger)
        client.command(f"""
            CREATE TABLE {SOURCE_MV} (
                id UInt64,
                user_id UInt64,
                event_type LowCardinality(String),
                page String,
                session_id String,
                properties String,
                event_time DateTime
            ) ENGINE = MergeTree() ORDER BY (user_id, event_time)
        """)
        client.command(f"""
            CREATE TABLE {TARGET_MV} (
                day Date,
                event_type LowCardinality(String),
                cnt AggregateFunction(count, UInt64)
            ) ENGINE = AggregatingMergeTree() ORDER BY (day, event_type)
        """)
        # Drop any prior MV with same name (defensive — temp_tables doesn't cover it)
        client.command(f"DROP VIEW IF EXISTS {MV_NAME}")
        client.command(f"""
            CREATE MATERIALIZED VIEW {MV_NAME}
            TO {TARGET_MV}
            AS SELECT
                toDate(event_time) AS day,
                event_type,
                countState(toUInt64(id)) AS cnt
            FROM {SOURCE_MV}
            GROUP BY day, event_type
        """)
        # Insert measurement (this triggers the MV)
        t0 = time.perf_counter()
        client.command(f"""
            INSERT INTO {SOURCE_MV}
            SELECT id, user_id, event_type, page, session_id, properties, event_time
            FROM events
        """)
        mv_insert_ms = (time.perf_counter() - t0) * 1000.0

        # Storage comparison
        proj_base_bytes = _bytes(client, SOURCE_PROJ)
        proj_sub_bytes = _proj_bytes(client, SOURCE_PROJ, "daily_event_counts")
        mv_source_bytes = _bytes(client, SOURCE_MV)
        mv_target_bytes = _bytes(client, TARGET_MV)

        # Read latency
        proj_runs = [run_query(client, READ_QUERY_PROJ) for _ in range(6)]
        mv_runs = [run_query(client, READ_QUERY_MV) for _ in range(6)]

        payload["projection"] = {
            "insert_wall_ms": round(proj_insert_ms, 1),
            "table_bytes": proj_base_bytes,
            "projection_bytes": proj_sub_bytes,
            "total_bytes": proj_base_bytes + proj_sub_bytes,
            "read_warm_avg_ms": _warm_avg(proj_runs),
            "read_runs": proj_runs,
        }
        payload["materialized_view"] = {
            "insert_wall_ms": round(mv_insert_ms, 1),
            "source_bytes": mv_source_bytes,
            "target_bytes": mv_target_bytes,
            "total_bytes": mv_source_bytes + mv_target_bytes,
            "read_warm_avg_ms": _warm_avg(mv_runs),
            "read_runs": mv_runs,
        }
        payload["read_speed_mv_to_proj"] = round(
            payload["projection"]["read_warm_avg_ms"]
            / max(payload["materialized_view"]["read_warm_avg_ms"], 0.01), 2)

        # Drop MV explicitly (temp_tables only handles the listed tables)
        client.command(f"DROP VIEW IF EXISTS {MV_NAME}")

    write_result("d4_projection_vs_mv", payload)


if __name__ == "__main__":
    main()
