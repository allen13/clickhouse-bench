"""Aggregate every results/*.json into a single paper-ready Markdown summary.

Reads:
- results/benchmark_*.json — Phase A baseline benchmark
- results/compare_features_*.json — Phase B feature comparisons + Phase D
                                     index-tuning + Phase F scale runs
- results/experiment_*.json — Phase C / D / E custom experiments

Writes ``results/aggregate.md`` plus a ``results/cost.md`` with the dollar
translation. Both are intermediate inputs for clickhouse-shape-matching-brief.tex; do not commit them
as the paper itself.

Cost model (very rough, marked approximate in the paper):
- Per-replica compute: $0.6885/hr on AWS Production tier (capture date in
  the paper's appendix). $0.2178/hr on Development.
- Storage: $0.026/GB/month compressed (Production), same on Development.

This script does NOT call ClickHouse. It works against the local results/
folder so it can be re-run any time.
"""
from __future__ import annotations

import glob
import json
import statistics
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS = REPO_ROOT / "results"

# Pricing snapshot (Cloud pricing page, captured date noted in clickhouse-shape-matching-brief.tex).
# Numbers are USD; treat all dollar figures as ±20% range estimates.
COMPUTE_PER_REPLICA_HR = {
    "development": 0.2178,
    "production": 0.6885,
}
STORAGE_PER_GB_MONTH = 0.0260


def _load(path: Path) -> dict | list:
    return json.loads(path.read_text())


def _per_query_cost_usd(server_metrics: dict, tier: str = "production") -> float:
    """Estimate a query's cost in USD given query_log fields.

    Cost = (duration_ms × peak_threads_used) / 3,600,000 × $per_replica_hr
    Crude — assumes the peak_threads count is on a single replica, which it
    is for non-parallel-replicas queries.
    """
    dur_ms = server_metrics.get("query_duration_ms") or 0
    threads = server_metrics.get("peak_threads_usage") or 1
    return (dur_ms * threads / 3_600_000.0) * COMPUTE_PER_REPLICA_HR[tier]


def _storage_cost_per_month(bytes_: int) -> float:
    gb = bytes_ / 1024 / 1024 / 1024
    return gb * STORAGE_PER_GB_MONTH


def _baseline_summary(out: list[str]) -> None:
    files = sorted(RESULTS.glob("benchmark_*.json"))
    if not files:
        return
    data = _load(files[-1])
    qres = data["query_results"]
    timed = [q for q in qres if q.get("avg_warm_ms") and not q.get("error")]
    cold = [q["cold_time_ms"] for q in timed]
    warm = [q["avg_warm_ms"] for q in timed]
    out.append("## Phase A — Baseline benchmark\n")
    out.append(f"- File: `{files[-1].name}`")
    out.append(f"- ClickHouse {data.get('clickhouse_version','?')}")
    out.append(f"- Queries (timed): **{len(timed)}** of {len(qres)}")
    out.append(f"- Avg cold latency: **{statistics.mean(cold):.1f} ms**")
    out.append(f"- Avg warm latency: **{statistics.mean(warm):.1f} ms**")
    out.append(f"- p50 warm: **{statistics.median(warm):.1f} ms** | "
               f"p95 warm: **{sorted(warm)[int(0.95*len(warm))-1]:.1f} ms**")
    fastest = min(timed, key=lambda q: q["avg_warm_ms"])
    slowest = max(timed, key=lambda q: q["avg_warm_ms"])
    out.append(f"- Fastest query: `{fastest['query_name']}` "
               f"({fastest['avg_warm_ms']:.1f} ms warm)")
    out.append(f"- Slowest query: `{slowest['query_name']}` "
               f"({slowest['avg_warm_ms']:.1f} ms warm)")
    if data.get("insert_results"):
        out.append("\n### Insert throughput")
        out.append("| Batch | ms | rows/sec |")
        out.append("|---|---|---|")
        for ir in data["insert_results"]:
            out.append(f"| {ir['batch_size']:,} | {ir['elapsed_ms']:.1f} "
                       f"| {ir['rows_per_second']:,.0f} |")
    out.append("")


def _feature_comparisons(out: list[str]) -> None:
    files = sorted(RESULTS.glob("compare_features_*.json"))
    if not files:
        return
    out.append("## Phase B / D — Feature comparisons\n")
    by_key: dict[str, list[Path]] = defaultdict(list)
    for f in files:
        d = _load(f)
        item = d[0] if isinstance(d, list) else d
        by_key[item.get("key", "?")].append(f)
    for key, paths in sorted(by_key.items()):
        # use the most recent run per key
        d = _load(paths[-1])
        item = d[0] if isinstance(d, list) else d
        out.append(f"### {key}")
        out.append(f"- File: `{paths[-1].name}`  | Title: {item.get('title','?')}")
        out.append("")
        out.append("| Variant | Compressed | Ratio | Insert (s) | "
                   "Notes |")
        out.append("|---|---|---|---|---|")
        for v in item.get("variants", []):
            comp = (v.get('bytes_compressed') or 0) / 1024 / 1024
            uncomp = (v.get('bytes_uncompressed') or 0) / 1024 / 1024
            ratio = (uncomp / comp) if comp else 0
            ins = v.get('insert_seconds') or 0
            err = v.get('error') or ''
            note = (err[:60] + '…') if err else 'OK'
            out.append(f"| {v.get('feature_label','?')} | "
                       f"{comp:.2f} MiB | "
                       f"{ratio:.2f}× | "
                       f"{ins:.2f} | {note} |")
        # Per-query latency grid
        timings = next(
            (v.get("query_timings_ms") for v in item.get("variants", [])
             if v.get("query_timings_ms")),
            None,
        )
        if timings:
            test_queries = list(timings.keys())
            out.append("")
            out.append(f"Warm-avg latency (ms):")
            head = ["Test query"] + [v["feature_label"]
                                     for v in item["variants"]]
            out.append("| " + " | ".join(head) + " |")
            out.append("|" + "---|" * len(head))
            for tq in test_queries:
                row = [tq]
                for v in item["variants"]:
                    t = (v.get("query_timings_ms") or {}).get(tq, {})
                    avg = t.get("avg_warm_ms")
                    row.append(f"{avg:.1f}" if avg else "—")
                out.append("| " + " | ".join(row) + " |")
        if item.get("insight"):
            out.append("")
            out.append(f"> _{item['insight']}_")
        out.append("")


def _experiments(out: list[str]) -> None:
    files = sorted(RESULTS.glob("experiment_*.json"))
    if not files:
        return
    out.append("## Phase C / D / E — Experiments\n")
    for f in files:
        d = _load(f)
        out.append(f"### `{f.name}`")
        out.append(f"- {d.get('title','?')}")
        # Surface the key headline number for each known experiment
        for h in ("dict_speedup_x", "speedup_wall", "speedup_server",
                  "speedup_2x", "speedup_3x",
                  "wall_speedup_x", "parts_ratio_sync_to_async",
                  "mutation_to_rmt_ratio", "us_per_row",
                  "storage_savings_x", "overhead_ratio",
                  "read_speed_mv_to_proj", "base_to_merge_ratio"):
            if h in d:
                out.append(f"  - `{h}` = **{d[h]}**")
        # Per-experiment summary lines
        if "events_rows" in d:
            out.append(f"  - events row count when run: {d['events_rows']:,}")
        if d.get("compat"):
            out.append(f"  - parallel-replicas compat: `{d['compat']}`")
        out.append("")


def _cost_table(out: list[str]) -> None:
    """Walk the experiment files and produce a $/month estimate per workload."""
    out.append("## Cost translation (approximate)\n")
    out.append(
        "Pricing snapshot — capture this from the ClickHouse Cloud pricing "
        "page on the day of paper publication and put the screenshot in the "
        "appendix. Numbers below are placeholders ±20%."
    )
    out.append("")
    out.append("| Workload | Compute (per query) | Storage (per month) |")
    out.append("|---|---|---|")
    # Baseline: average warm query
    bench_files = sorted(RESULTS.glob("benchmark_*.json"))
    if bench_files:
        d = _load(bench_files[-1])
        timed = [q for q in d["query_results"]
                 if q.get("avg_warm_ms") and not q.get("error")]
        if timed:
            avg_ms = statistics.mean(q["avg_warm_ms"] for q in timed)
            # assume single-replica execution (1 thread)
            cost = (avg_ms * 1 / 3_600_000.0) * COMPUTE_PER_REPLICA_HR["production"]
            out.append(f"| Avg warm query (n={len(timed)}) | "
                       f"~${cost:.6f} | n/a |")
    # Storage at scale=100K and scale=1M (if Phase F ran)
    # Pull from compare-features ordering files.
    cf_ordering = sorted(RESULTS.glob("compare_features_*.json"))
    for f in cf_ordering:
        d = _load(f)
        item = d[0] if isinstance(d, list) else d
        if item.get("key") != "ordering":
            continue
        for v in item.get("variants", []):
            cb = v.get("bytes_compressed") or 0
            mc = _storage_cost_per_month(cb)
            label = v.get("feature_label", "?")
            out.append(
                f"| events table — {label} ({f.stem.split('_')[-1]}) "
                f"| n/a | ~${mc:.4f}/mo |"
            )
            break  # one row per file
    out.append("")
    out.append(
        "Replace the placeholder numbers with the live pricing page on "
        "publication day; `_per_query_cost_usd` and `_storage_cost_per_month` "
        "in this script encode the formulas.")


def main() -> None:
    out: list[str] = ["# clickhouse-bench — aggregated results\n"]
    _baseline_summary(out)
    _feature_comparisons(out)
    _experiments(out)
    _cost_table(out)
    target = RESULTS / "aggregate.md"
    target.write_text("\n".join(out) + "\n")
    print(f"Wrote {target.relative_to(REPO_ROOT)} ({target.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
