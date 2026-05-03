"""C2 — Query cache hit/miss latency.

Pick a heavy aggregation, run it 5x with use_query_cache=true. The first run
is a miss (cache write). Subsequent runs should be hits (cache read) and
return in microseconds.

Verify by reading ``query_cache_usage`` from system.query_log:
- 'Write' on the first run
- 'Read'  on the rest

Reference: docs/query-cache.md.
"""
from __future__ import annotations

from scripts.experiments._lib import (
    get_client, log_metrics, new_query_id, run_query,
    server_summary, write_result,
)


HEAVY_SQL = """
    SELECT
        toStartOfHour(timestamp) AS hour,
        metric_name,
        avg(value) AS v,
        quantile(0.95)(value) AS p95
    FROM metrics
    GROUP BY hour, metric_name
    ORDER BY hour, metric_name
    LIMIT 1000
"""


def main() -> None:
    client = get_client()
    server = server_summary(client)

    # Clear any stale cache entry for an apples-to-apples first-miss measurement.
    client.command("SYSTEM DROP QUERY CACHE")

    payload: dict = {
        "title": "Query cache hit / miss latency",
        "server": server,
        "settings": {
            "use_query_cache": True,
            "query_cache_ttl_s": 60,
            "iterations": 5,
        },
    }

    runs = []
    qids = []
    for i in range(5):
        qid = new_query_id(f"c2-cache-{i}")
        r = run_query(client, HEAVY_SQL,
                      settings={"use_query_cache": 1,
                                "query_cache_ttl": 60},
                      query_id=qid)
        runs.append({**r, "iteration": i})
        qids.append(qid)

    metrics = log_metrics(client, qids)
    for r in runs:
        r["server"] = metrics.get(r["query_id"], {})

    payload["runs"] = runs

    # Headline numbers.
    miss = runs[0]
    hits = [r for r in runs[1:]
            if r["server"].get("query_cache_usage") == "Read"]
    payload["miss_wall_ms"] = miss["wall_ms"]
    payload["miss_query_duration_ms"] = miss["server"].get("query_duration_ms")
    if hits:
        payload["hit_wall_ms_avg"] = round(
            sum(r["wall_ms"] for r in hits) / len(hits), 2)
        payload["hit_query_duration_ms_avg"] = round(
            sum(r["server"].get("query_duration_ms", 0) for r in hits) / len(hits), 2)
        payload["speedup_wall"] = round(
            payload["miss_wall_ms"] / payload["hit_wall_ms_avg"], 1)
        payload["speedup_server"] = round(
            (payload["miss_query_duration_ms"] or 0)
            / max(payload["hit_query_duration_ms_avg"] or 0.01, 0.01), 1)

    # Aggregate counters
    counts = client.query("""
        SELECT event, value
        FROM system.events
        WHERE event IN ('QueryCacheHits', 'QueryCacheMisses')
    """).result_rows
    payload["server_counters"] = dict(counts)

    write_result("c2_query_cache", payload)


if __name__ == "__main__":
    main()
