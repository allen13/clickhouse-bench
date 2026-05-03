"""C5 — Mutation cost vs ReplacingMergeTree insert.

Build two equivalent 100K-row tables. On one, run ALTER TABLE ... UPDATE
to flip a status column for ~1% of rows; on the other, insert new versions
into a ReplacingMergeTree. Measure mutation duration and parts written.

Reference: docs/ingestion.md#mutations.
"""
from __future__ import annotations

import time

from scripts.experiments._lib import (
    get_client, run_command, server_summary, temp_tables, write_result,
)


MUT_TABLE = "exp_mut_users"
RMT_TABLE = "exp_rmt_users"
N_ROWS = 100_000
N_UPDATES = 1_000  # 1% of rows


def _seed_mut(client) -> None:
    client.command(f"""
        CREATE TABLE {MUT_TABLE} (
            id UInt64,
            name String,
            status LowCardinality(String) DEFAULT 'active'
        ) ENGINE = MergeTree() ORDER BY id
    """)
    client.command(f"""
        INSERT INTO {MUT_TABLE} (id, name)
        SELECT number, 'user_' || toString(number)
        FROM numbers({N_ROWS})
    """)


def _seed_rmt(client) -> None:
    client.command(f"""
        CREATE TABLE {RMT_TABLE} (
            id UInt64,
            name String,
            status LowCardinality(String) DEFAULT 'active',
            updated_at DateTime DEFAULT now()
        ) ENGINE = ReplacingMergeTree(updated_at) ORDER BY id
    """)
    client.command(f"""
        INSERT INTO {RMT_TABLE} (id, name)
        SELECT number, 'user_' || toString(number)
        FROM numbers({N_ROWS})
    """)


def main() -> None:
    client = get_client()
    server = server_summary(client)

    payload: dict = {
        "title": "Mutation cost (ALTER UPDATE) vs ReplacingMergeTree insert",
        "server": server,
        "settings": {"rows": N_ROWS, "updates": N_UPDATES},
    }

    with temp_tables(client, MUT_TABLE, RMT_TABLE):
        # ── MUT: ALTER TABLE ... UPDATE
        _seed_mut(client)
        parts_before_mut = client.query(
            f"SELECT count() FROM system.parts WHERE active AND table = '{MUT_TABLE}'"
        ).result_rows[0][0]

        t0 = time.perf_counter()
        client.command(
            f"ALTER TABLE {MUT_TABLE} UPDATE status = 'inactive' "
            f"WHERE id < {N_UPDATES}",
            settings={"mutations_sync": 2},  # wait for completion
        )
        mut_wall_ms = (time.perf_counter() - t0) * 1000.0

        parts_after_mut = client.query(
            f"SELECT count() FROM system.parts WHERE active AND table = '{MUT_TABLE}'"
        ).result_rows[0][0]
        mut_size = client.query(f"""
            SELECT formatReadableSize(sum(bytes_on_disk))
            FROM system.parts WHERE active AND table = '{MUT_TABLE}'
        """).result_rows[0][0]

        # The mutation creates new parts for every part that contains affected rows
        payload["mutation"] = {
            "wall_ms": round(mut_wall_ms, 1),
            "parts_before": parts_before_mut,
            "parts_after": parts_after_mut,
            "size": mut_size,
        }

        # ── RMT: insert new versions
        _seed_rmt(client)
        parts_before_rmt = client.query(
            f"SELECT count() FROM system.parts WHERE active AND table = '{RMT_TABLE}'"
        ).result_rows[0][0]

        t0 = time.perf_counter()
        client.command(f"""
            INSERT INTO {RMT_TABLE} (id, name, status, updated_at)
            SELECT id, name, 'inactive' AS status, now() AS updated_at
            FROM {RMT_TABLE}
            WHERE id < {N_UPDATES}
        """)
        rmt_wall_ms = (time.perf_counter() - t0) * 1000.0

        parts_after_rmt = client.query(
            f"SELECT count() FROM system.parts WHERE active AND table = '{RMT_TABLE}'"
        ).result_rows[0][0]
        rmt_size = client.query(f"""
            SELECT formatReadableSize(sum(bytes_on_disk))
            FROM system.parts WHERE active AND table = '{RMT_TABLE}'
        """).result_rows[0][0]

        # Read latency comparison: simple FINAL read on RMT vs straight read on mutated table
        t0 = time.perf_counter()
        rmt_read_with_final = client.query(
            f"SELECT id, status FROM {RMT_TABLE} FINAL WHERE id < {N_UPDATES}"
        ).result_rows
        rmt_read_final_ms = (time.perf_counter() - t0) * 1000.0

        t0 = time.perf_counter()
        rmt_read_argmax = client.query(f"""
            SELECT id, argMax(status, updated_at)
            FROM {RMT_TABLE}
            WHERE id < {N_UPDATES}
            GROUP BY id
        """).result_rows
        rmt_read_argmax_ms = (time.perf_counter() - t0) * 1000.0

        payload["replacing_merge_tree"] = {
            "wall_ms": round(rmt_wall_ms, 1),
            "parts_before": parts_before_rmt,
            "parts_after": parts_after_rmt,
            "size": rmt_size,
            "read_final_ms": round(rmt_read_final_ms, 1),
            "read_argmax_ms": round(rmt_read_argmax_ms, 1),
        }

        payload["mutation_to_rmt_ratio"] = round(mut_wall_ms / rmt_wall_ms, 2)

    write_result("c5_mutation_cost", payload)


if __name__ == "__main__":
    main()
