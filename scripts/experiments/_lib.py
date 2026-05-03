"""Helpers shared across Phase C/D/E experiment scripts.

Each script:
1. Connects via ``src.config.get_client``.
2. Runs DDL/DML to set up an isolated workspace (``exp_*`` tables).
3. Issues queries with explicit ``query_id``s so results can be correlated
   with ``system.query_log``.
4. Writes a single JSON result to ``results/experiment_<key>_<ts>.json``.
5. Drops its own tables at the end.

The functions here keep that boilerplate in one place.
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Make ``src.config`` importable regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.config import get_client  # noqa: E402


def new_query_id(prefix: str = "exp") -> str:
    """Return a tagged query_id so we can find this query in system.query_log."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _settings_with_qid(settings: dict | None, query_id: str) -> dict:
    """clickhouse-connect surfaces query_id via the settings dict."""
    s = dict(settings or {})
    s["query_id"] = query_id
    return s


def run_query(client, sql: str, *, settings: dict | None = None,
              query_id: str | None = None) -> dict:
    """Run a SELECT and return ``{rows, wall_ms, query_id}`` (no log lookup).

    Use ``log_metrics`` afterwards to enrich with server-side numbers.
    """
    qid = query_id or new_query_id()
    t0 = time.perf_counter()
    res = client.query(sql, settings=_settings_with_qid(settings, qid))
    wall_ms = (time.perf_counter() - t0) * 1000.0
    return {
        "query_id": qid,
        "wall_ms": round(wall_ms, 2),
        "rows": len(res.result_rows),
    }


def run_command(client, sql: str, *, settings: dict | None = None,
                query_id: str | None = None) -> dict:
    """Run a DDL/DML command (no rows expected). Same shape as run_query."""
    qid = query_id or new_query_id("cmd")
    t0 = time.perf_counter()
    client.command(sql, settings=_settings_with_qid(settings, qid))
    wall_ms = (time.perf_counter() - t0) * 1000.0
    return {"query_id": qid, "wall_ms": round(wall_ms, 2)}


def flush_logs(client) -> None:
    """Force the query log to disk so subsequent reads see today's queries."""
    client.command("SYSTEM FLUSH LOGS")


def log_metrics(client, query_ids: Iterable[str]) -> dict[str, dict]:
    """Look up server-side metrics in system.query_log for the given query_ids.

    Returns ``{query_id: {duration_ms, read_rows, read_bytes, memory_usage,
    peak_threads_usage, query_cache_usage, result_rows, exception}}``.
    Missing entries are returned with ``None`` values.
    """
    flush_logs(client)
    qid_list = ",".join(f"'{q}'" for q in query_ids)
    res = client.query(f"""
        SELECT
            query_id,
            query_duration_ms,
            read_rows,
            read_bytes,
            memory_usage,
            peak_threads_usage,
            query_cache_usage,
            result_rows,
            exception
        FROM system.query_log
        WHERE query_id IN ({qid_list}) AND type IN ('QueryFinish', 'ExceptionWhileProcessing')
        ORDER BY event_time DESC
        LIMIT 1 BY query_id
    """)
    cols = ["query_id", "query_duration_ms", "read_rows", "read_bytes",
            "memory_usage", "peak_threads_usage", "query_cache_usage",
            "result_rows", "exception"]
    out: dict[str, dict] = {}
    for row in res.result_rows:
        d = dict(zip(cols, row))
        out[d["query_id"]] = d
    # Ensure every requested id has an entry, even if missing
    for q in query_ids:
        out.setdefault(q, {k: None for k in cols} | {"query_id": q})
    return out


def warm_run(client, sql: str, *, n: int = 5,
             settings: dict | None = None,
             prefix: str = "warm") -> list[dict]:
    """Run a query N times, return the per-run measurements + log metrics.

    First run is treated as cold; the rest as warm. The caller decides which
    to discard.
    """
    runs: list[dict] = []
    qids: list[str] = []
    for i in range(n):
        qid = new_query_id(f"{prefix}-{i}")
        qids.append(qid)
        runs.append({**run_query(client, sql, settings=settings, query_id=qid),
                     "iteration": i})
    metrics = log_metrics(client, qids)
    for r in runs:
        r["server"] = metrics.get(r["query_id"], {})
    return runs


def write_result(key: str, payload: dict[str, Any]) -> Path:
    """Persist the experiment payload to results/experiment_<key>_<ts>.json."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = _REPO_ROOT / "results" / f"experiment_{key}_{ts}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "experiment_key": key,
        "timestamp": ts,
        **payload,
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"Wrote {path.relative_to(_REPO_ROOT)}")
    return path


@contextmanager
def temp_tables(client, *names: str):
    """Drop the given tables on entry AND exit, so the experiment is isolated."""
    for n in names:
        client.command(f"DROP TABLE IF EXISTS {n} SYNC")
    try:
        yield
    finally:
        for n in names:
            try:
                client.command(f"DROP TABLE IF EXISTS {n} SYNC")
            except Exception as e:
                print(f"warn: failed to drop {n}: {e}", file=sys.stderr)


def server_summary(client) -> dict:
    """One-shot server fingerprint to record in every result file."""
    rows = client.query(
        "SELECT version(), currentUser(), hostname(), uptime()"
    ).result_rows[0]
    return {
        "clickhouse_version": rows[0],
        "user": rows[1],
        "hostname": rows[2],
        "uptime_s": rows[3],
    }
