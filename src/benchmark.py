"""Benchmark runner for ClickHouse Cloud.

Executes every query from the catalogue, measuring wall-clock time and
tracking cold-vs-warm performance (first run vs subsequent runs).
Results are stored in a structured format for later evaluation.
"""

from __future__ import annotations

import json
import random
import time
import tracemalloc
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table as RichTable

from .config import ClickHouseConfig, get_client
from .queries import QUERIES, BenchmarkQuery, Category

console = Console()

# ---------------------------------------------------------------------------
# Result data structures
# ---------------------------------------------------------------------------


@dataclass
class QueryResult:
    """Timing result for a single query execution."""

    query_name: str
    category: str
    cold_time_ms: float
    warm_times_ms: list[float]
    avg_warm_ms: float
    min_warm_ms: float
    max_warm_ms: float
    rows_returned: int
    memory_peak_kb: float
    error: str | None = None


@dataclass
class InsertResult:
    """Timing result for insert-performance benchmarks."""

    batch_size: int
    elapsed_ms: float
    rows_per_second: float


@dataclass
class BenchmarkReport:
    """Full benchmark report."""

    timestamp: str
    clickhouse_version: str
    config_summary: dict[str, str]
    query_results: list[QueryResult] = field(default_factory=list)
    insert_results: list[InsertResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_query(client, sql: str) -> tuple[float, int]:
    """Execute *sql*, return ``(elapsed_ms, row_count)``."""
    start = time.perf_counter()
    result = client.query(sql)
    elapsed = (time.perf_counter() - start) * 1000
    return elapsed, len(result.result_rows)


def _measure_memory(client, sql: str) -> float:
    """Run *sql* while tracking peak memory, return peak KB."""
    tracemalloc.start()
    client.query(sql)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak / 1024


# ---------------------------------------------------------------------------
# Insert benchmark
# ---------------------------------------------------------------------------


def _benchmark_inserts(client, batch_sizes: list[int] | None = None) -> list[InsertResult]:
    """Benchmark insert throughput at various batch sizes."""
    if batch_sizes is None:
        batch_sizes = [100, 1_000, 10_000, 50_000]

    results: list[InsertResult] = []
    console.print("\n[bold cyan]Insert Performance Benchmark[/bold cyan]")

    for size in batch_sizes:
        rows = [
            [
                random.randint(1, 100),
                "benchmark_metric",
                round(random.uniform(0, 100), 4),
                "{}",
                datetime.now(),
            ]
            for _ in range(size)
        ]
        start = time.perf_counter()
        client.insert(
            "metrics",
            rows,
            column_names=["sensor_id", "metric_name", "value", "tags", "timestamp"],
        )
        elapsed_ms = (time.perf_counter() - start) * 1000
        rps = size / (elapsed_ms / 1000) if elapsed_ms > 0 else 0
        results.append(InsertResult(batch_size=size, elapsed_ms=round(elapsed_ms, 2), rows_per_second=round(rps, 0)))
        console.print(f"  batch={size:>6,}  {elapsed_ms:>8.1f} ms  ({rps:,.0f} rows/s)")

    return results


# ---------------------------------------------------------------------------
# Main benchmark runner
# ---------------------------------------------------------------------------

DEFAULT_WARM_RUNS = 3


def run_benchmarks(
    cfg: ClickHouseConfig | None = None,
    *,
    warm_runs: int = DEFAULT_WARM_RUNS,
    categories: list[Category] | None = None,
    output_dir: str | Path = "results",
) -> BenchmarkReport:
    """Execute all benchmark queries and return a report.

    Parameters
    ----------
    cfg:
        Connection config.
    warm_runs:
        How many additional (warm-cache) executions per query.
    categories:
        If provided, only run queries in these categories.
    output_dir:
        Directory to save the JSON results file.
    """
    client = get_client(cfg)

    # Gather server info
    version_row = client.query("SELECT version()")
    ch_version = version_row.result_rows[0][0] if version_row.result_rows else "unknown"

    cfg = cfg or ClickHouseConfig()
    report = BenchmarkReport(
        timestamp=datetime.utcnow().isoformat(),
        clickhouse_version=ch_version,
        config_summary=cfg.summary(),
    )

    # Filter queries
    queries = QUERIES
    if categories:
        cat_set = set(categories)
        queries = [q for q in queries if q.category in cat_set]

    # Skip insert/compression queries from timed runs (handled separately)
    timed_queries = [q for q in queries if q.category not in (Category.INSERT, Category.COMPRESSION)]
    meta_queries = [q for q in queries if q.category == Category.COMPRESSION]

    console.print(f"\n[bold cyan]Running {len(timed_queries)} benchmark queries "
                  f"({warm_runs} warm runs each)...[/bold cyan]")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        tid = progress.add_task("queries", total=len(timed_queries))

        for bq in timed_queries:
            progress.update(tid, description=f"[cyan]{bq.name}[/cyan]")
            try:
                # Cold run
                cold_ms, row_count = _run_query(client, bq.sql)

                # Warm runs
                warm_times: list[float] = []
                for _ in range(warm_runs):
                    wt, _ = _run_query(client, bq.sql)
                    warm_times.append(round(wt, 3))

                # Memory
                mem_kb = _measure_memory(client, bq.sql)

                report.query_results.append(QueryResult(
                    query_name=bq.name,
                    category=bq.category.value,
                    cold_time_ms=round(cold_ms, 3),
                    warm_times_ms=warm_times,
                    avg_warm_ms=round(sum(warm_times) / len(warm_times), 3) if warm_times else 0,
                    min_warm_ms=round(min(warm_times), 3) if warm_times else 0,
                    max_warm_ms=round(max(warm_times), 3) if warm_times else 0,
                    rows_returned=row_count,
                    memory_peak_kb=round(mem_kb, 2),
                ))
            except Exception as exc:
                report.query_results.append(QueryResult(
                    query_name=bq.name,
                    category=bq.category.value,
                    cold_time_ms=-1,
                    warm_times_ms=[],
                    avg_warm_ms=-1,
                    min_warm_ms=-1,
                    max_warm_ms=-1,
                    rows_returned=0,
                    memory_peak_kb=0,
                    error=str(exc),
                ))

            progress.advance(tid)

    # Run meta/compression queries (single execution, no timing)
    for bq in meta_queries:
        try:
            _, row_count = _run_query(client, bq.sql)
            report.query_results.append(QueryResult(
                query_name=bq.name,
                category=bq.category.value,
                cold_time_ms=0,
                warm_times_ms=[],
                avg_warm_ms=0,
                min_warm_ms=0,
                max_warm_ms=0,
                rows_returned=row_count,
                memory_peak_kb=0,
            ))
        except Exception as exc:
            report.query_results.append(QueryResult(
                query_name=bq.name,
                category=bq.category.value,
                cold_time_ms=-1,
                warm_times_ms=[],
                avg_warm_ms=-1,
                min_warm_ms=-1,
                max_warm_ms=-1,
                rows_returned=0,
                memory_peak_kb=0,
                error=str(exc),
            ))

    # Insert benchmarks
    if categories is None or Category.INSERT in categories:
        report.insert_results = _benchmark_inserts(client)

    # Print summary table
    _print_summary(report)

    # Save to JSON
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    ts_slug = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    json_path = out_path / f"benchmark_{ts_slug}.json"
    json_path.write_text(json.dumps(_report_to_dict(report), indent=2))
    console.print(f"\n[green]Results saved to {json_path}[/green]")

    return report


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def _print_summary(report: BenchmarkReport) -> None:
    """Print a rich table summarising query results."""
    rt = RichTable(title="Benchmark Results", show_lines=True)
    rt.add_column("Query", style="cyan", max_width=30)
    rt.add_column("Category", style="blue")
    rt.add_column("Cold (ms)", justify="right", style="yellow")
    rt.add_column("Avg Warm (ms)", justify="right", style="green")
    rt.add_column("Min Warm", justify="right")
    rt.add_column("Max Warm", justify="right")
    rt.add_column("Rows", justify="right")
    rt.add_column("Mem (KB)", justify="right", style="magenta")
    rt.add_column("Status", justify="center")

    for qr in report.query_results:
        status = "[green]OK[/green]" if qr.error is None else f"[red]ERR[/red]"
        rt.add_row(
            qr.query_name,
            qr.category,
            f"{qr.cold_time_ms:.1f}" if qr.cold_time_ms >= 0 else "—",
            f"{qr.avg_warm_ms:.1f}" if qr.avg_warm_ms >= 0 else "—",
            f"{qr.min_warm_ms:.1f}" if qr.min_warm_ms >= 0 else "—",
            f"{qr.max_warm_ms:.1f}" if qr.max_warm_ms >= 0 else "—",
            f"{qr.rows_returned:,}",
            f"{qr.memory_peak_kb:.1f}" if qr.memory_peak_kb > 0 else "—",
            status,
        )

    console.print(rt)

    if report.insert_results:
        it = RichTable(title="Insert Performance")
        it.add_column("Batch Size", justify="right", style="cyan")
        it.add_column("Time (ms)", justify="right", style="yellow")
        it.add_column("Rows/sec", justify="right", style="green")
        for ir in report.insert_results:
            it.add_row(f"{ir.batch_size:,}", f"{ir.elapsed_ms:.1f}", f"{ir.rows_per_second:,.0f}")
        console.print(it)


def _report_to_dict(report: BenchmarkReport) -> dict[str, Any]:
    """Convert report to a JSON-serializable dict."""
    return {
        "timestamp": report.timestamp,
        "clickhouse_version": report.clickhouse_version,
        "config": report.config_summary,
        "query_results": [asdict(qr) for qr in report.query_results],
        "insert_results": [asdict(ir) for ir in report.insert_results],
    }
