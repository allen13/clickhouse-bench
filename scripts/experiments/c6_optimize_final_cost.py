"""C6 — OPTIMIZE TABLE ... FINAL cost demonstration.

Build a table, insert rows in many small batches to create lots of parts,
then measure how long ``OPTIMIZE TABLE ... FINAL`` takes to merge them
into one.

The point is to *quantify* the operation that the docs say to avoid; the
recommendation is "let background merges work."

Reference: docs/ingestion.md, insert-optimize-avoid-final rule.
"""
from __future__ import annotations

import time

from scripts.experiments._lib import (
    get_client, server_summary, temp_tables, write_result,
)


TABLE = "exp_optimize_final"
N_BATCHES = 50
ROWS_PER_BATCH = 1000


def main() -> None:
    client = get_client()
    server = server_summary(client)

    payload: dict = {
        "title": "OPTIMIZE TABLE … FINAL cost on a fragmented table",
        "server": server,
        "settings": {"batches": N_BATCHES, "rows_per_batch": ROWS_PER_BATCH},
    }

    with temp_tables(client, TABLE):
        client.command(f"""
            CREATE TABLE {TABLE} (
                id UInt64,
                payload String
            ) ENGINE = MergeTree() ORDER BY id
        """)

        # Force many parts: insert N_BATCHES batches, each into its own part.
        # Use min_insert_block_size to discourage server-side coalescing during
        # the writes.
        for b in range(N_BATCHES):
            offset = b * ROWS_PER_BATCH
            client.command(
                f"""
                INSERT INTO {TABLE}
                SELECT
                    {offset} + number AS id,
                    'payload_' || toString({offset} + number) AS payload
                FROM numbers({ROWS_PER_BATCH})
                """,
            )

        # Snapshot before OPTIMIZE
        before = client.query(f"""
            SELECT
                count(),
                sum(rows),
                formatReadableSize(sum(bytes_on_disk)),
                avg(level)
            FROM system.parts
            WHERE active AND table = '{TABLE}'
        """).result_rows[0]
        payload["before"] = {
            "active_parts": before[0],
            "rows": before[1],
            "on_disk": before[2],
            "avg_merge_level": float(before[3] or 0),
        }

        # OPTIMIZE FINAL
        t0 = time.perf_counter()
        client.command(
            f"OPTIMIZE TABLE {TABLE} FINAL",
            settings={"optimize_throw_if_noop": 1},
        )
        optimize_wall_ms = (time.perf_counter() - t0) * 1000.0
        time.sleep(1)  # let part bookkeeping settle

        # Snapshot after
        after = client.query(f"""
            SELECT
                count(),
                sum(rows),
                formatReadableSize(sum(bytes_on_disk)),
                avg(level)
            FROM system.parts
            WHERE active AND table = '{TABLE}'
        """).result_rows[0]
        payload["after"] = {
            "active_parts": after[0],
            "rows": after[1],
            "on_disk": after[2],
            "avg_merge_level": float(after[3] or 0),
        }
        payload["optimize_wall_ms"] = round(optimize_wall_ms, 1)

        # Per-row cost
        rows = payload["after"]["rows"] or 1
        payload["us_per_row"] = round(optimize_wall_ms * 1000.0 / rows, 2)

    write_result("c6_optimize_final_cost", payload)


if __name__ == "__main__":
    main()
