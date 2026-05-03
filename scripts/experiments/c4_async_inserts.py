"""C4 — Async inserts vs synchronous inserts at small batch size.

Setup
-----
Two identical exp_async_<sync|async> tables. Push 10,000 rows in 100-row
batches. Synchronous mode creates 100 parts (one per insert). Async mode
buffers server-side and creates ~1–2 parts.

Captures: total wall time, final part count, total bytes.

Reference: docs/ingestion.md#async-inserts, insert-async-small-batches rule.
"""
from __future__ import annotations

import time
import uuid

from scripts.experiments._lib import (
    get_client, run_command, server_summary, temp_tables, write_result,
)


SYNC_TABLE = "exp_async_sync"
ASYNC_TABLE = "exp_async_async"
TOTAL_ROWS = 10_000
BATCH = 100
NUM_BATCHES = TOTAL_ROWS // BATCH

DDL_TMPL = """
    CREATE TABLE {name} (
        id UInt64,
        ts DateTime DEFAULT now(),
        payload String
    ) ENGINE = MergeTree() ORDER BY id
"""


def _populate(client, table: str, *, async_insert: int) -> dict:
    """Insert NUM_BATCHES * BATCH rows of 100-byte payloads, returning timing."""
    settings = {}
    if async_insert:
        settings.update({
            "async_insert": 1,
            "wait_for_async_insert": 1,
            "async_insert_busy_timeout_ms": 200,  # let it flush quickly
        })
    t0 = time.perf_counter()
    payload = "x" * 100  # 100-byte filler
    for b in range(NUM_BATCHES):
        # build a VALUES batch of BATCH rows
        rows = ",".join(
            f"({b * BATCH + i},now(),'{payload}')" for i in range(BATCH)
        )
        client.command(
            f"INSERT INTO {table} (id, ts, payload) VALUES {rows}",
            settings=settings,
        )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return {"wall_ms": round(elapsed_ms, 1)}


def _table_state(client, table: str) -> dict:
    rows = client.query(f"""
        SELECT count(), sum(rows), formatReadableSize(sum(bytes_on_disk))
        FROM system.parts
        WHERE active AND database = currentDatabase() AND table = '{table}'
    """).result_rows[0]
    parts, total_rows, size = rows
    return {"active_parts": parts, "rows": total_rows, "on_disk": size}


def main() -> None:
    client = get_client()
    server = server_summary(client)

    payload: dict = {
        "title": "Async inserts vs synchronous inserts (100-row batches × 100)",
        "server": server,
        "settings": {
            "total_rows": TOTAL_ROWS,
            "batch": BATCH,
            "num_batches": NUM_BATCHES,
        },
    }

    with temp_tables(client, SYNC_TABLE, ASYNC_TABLE):
        client.command(DDL_TMPL.format(name=SYNC_TABLE))
        client.command(DDL_TMPL.format(name=ASYNC_TABLE))

        # Synchronous run
        sync_perf = _populate(client, SYNC_TABLE, async_insert=0)
        time.sleep(2)  # let any background activity settle
        sync_state = _table_state(client, SYNC_TABLE)
        payload["synchronous"] = {**sync_perf, **sync_state}

        # Async run
        async_perf = _populate(client, ASYNC_TABLE, async_insert=1)
        # Allow the buffer to flush before measuring final part count
        time.sleep(3)
        async_state = _table_state(client, ASYNC_TABLE)
        payload["async"] = {**async_perf, **async_state}

        # Cross-comparison
        payload["wall_speedup_x"] = round(
            payload["synchronous"]["wall_ms"]
            / max(payload["async"]["wall_ms"], 1.0), 2)
        payload["parts_ratio_sync_to_async"] = (
            payload["synchronous"]["active_parts"]
            / max(payload["async"]["active_parts"], 1)
        )

    write_result("c4_async_inserts", payload)


if __name__ == "__main__":
    main()
