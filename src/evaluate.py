"""Evaluation and reporting for benchmark results.

Loads benchmark JSON files, generates matplotlib charts, and produces a
summary report with performance insights and recommendations.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # non-interactive backend
import matplotlib.pyplot as plt
import pandas as pd
from rich.console import Console
from rich.panel import Panel
from rich.table import Table as RichTable

console = Console()

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_results(results_dir: str | Path = "results") -> list[dict[str, Any]]:
    """Load all benchmark JSON files from *results_dir*, newest first."""
    rdir = Path(results_dir)
    files = sorted(rdir.glob("benchmark_*.json"), reverse=True)
    if not files:
        console.print("[red]No benchmark results found.[/red]")
        return []
    data: list[dict[str, Any]] = []
    for f in files:
        data.append(json.loads(f.read_text()))
    console.print(f"[green]Loaded {len(data)} result file(s) from {rdir}[/green]")
    return data


def _results_to_df(results: dict[str, Any]) -> pd.DataFrame:
    """Convert a single result dict to a DataFrame of query results."""
    rows = results.get("query_results", [])
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    return df


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------


def _chart_query_latency(df: pd.DataFrame, output_dir: Path) -> Path:
    """Bar chart of cold vs average warm latency per query."""
    plot_df = df[df["cold_time_ms"] > 0].copy()
    if plot_df.empty:
        return output_dir / "latency.png"

    fig, ax = plt.subplots(figsize=(14, 7))
    x = range(len(plot_df))
    width = 0.35
    ax.barh(
        [i + width / 2 for i in x],
        plot_df["cold_time_ms"],
        width,
        label="Cold",
        color="#e74c3c",
        alpha=0.85,
    )
    ax.barh(
        [i - width / 2 for i in x],
        plot_df["avg_warm_ms"],
        width,
        label="Warm (avg)",
        color="#2ecc71",
        alpha=0.85,
    )
    ax.set_yticks(list(x))
    ax.set_yticklabels(plot_df["query_name"], fontsize=8)
    ax.set_xlabel("Latency (ms)")
    ax.set_title("Query Latency: Cold vs Warm")
    ax.legend()
    ax.invert_yaxis()
    plt.tight_layout()
    path = output_dir / "latency_cold_vs_warm.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def _chart_by_category(df: pd.DataFrame, output_dir: Path) -> Path:
    """Box plot of warm latency grouped by category."""
    plot_df = df[df["avg_warm_ms"] > 0].copy()
    if plot_df.empty:
        return output_dir / "category_latency.png"

    categories = plot_df["category"].unique()
    data_groups = [plot_df[plot_df["category"] == c]["avg_warm_ms"].values for c in categories]

    fig, ax = plt.subplots(figsize=(10, 6))
    bp = ax.boxplot(data_groups, labels=categories, patch_artist=True, vert=True)
    colors = plt.cm.Set3([i / len(categories) for i in range(len(categories))])
    for patch, color in zip(bp["boxes"], colors):
        patch.set_facecolor(color)
    ax.set_ylabel("Avg Warm Latency (ms)")
    ax.set_title("Latency Distribution by Category")
    plt.xticks(rotation=45, ha="right", fontsize=9)
    plt.tight_layout()
    path = output_dir / "category_latency.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def _chart_insert_throughput(results: dict[str, Any], output_dir: Path) -> Path | None:
    """Bar chart of insert throughput at different batch sizes."""
    inserts = results.get("insert_results", [])
    if not inserts:
        return None

    fig, ax1 = plt.subplots(figsize=(8, 5))
    sizes = [r["batch_size"] for r in inserts]
    rps = [r["rows_per_second"] for r in inserts]
    times = [r["elapsed_ms"] for r in inserts]

    x = range(len(sizes))
    ax1.bar(x, rps, color="#3498db", alpha=0.8, label="Rows/sec")
    ax1.set_xticks(list(x))
    ax1.set_xticklabels([f"{s:,}" for s in sizes])
    ax1.set_xlabel("Batch Size")
    ax1.set_ylabel("Rows/second", color="#3498db")
    ax1.tick_params(axis="y", labelcolor="#3498db")

    ax2 = ax1.twinx()
    ax2.plot(list(x), times, "r-o", label="Time (ms)")
    ax2.set_ylabel("Time (ms)", color="red")
    ax2.tick_params(axis="y", labelcolor="red")

    ax1.set_title("Insert Throughput by Batch Size")
    fig.legend(loc="upper left", bbox_to_anchor=(0.12, 0.88))
    plt.tight_layout()
    path = output_dir / "insert_throughput.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def _chart_memory(df: pd.DataFrame, output_dir: Path) -> Path:
    """Bar chart of peak memory usage per query."""
    plot_df = df[df["memory_peak_kb"] > 0].copy()
    if plot_df.empty:
        return output_dir / "memory.png"

    fig, ax = plt.subplots(figsize=(14, 7))
    ax.barh(plot_df["query_name"], plot_df["memory_peak_kb"], color="#9b59b6", alpha=0.8)
    ax.set_xlabel("Peak Memory (KB)")
    ax.set_title("Client-Side Peak Memory by Query")
    ax.invert_yaxis()
    plt.tight_layout()
    path = output_dir / "memory_usage.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


# ---------------------------------------------------------------------------
# Recommendations engine
# ---------------------------------------------------------------------------


def _generate_recommendations(df: pd.DataFrame, results: dict[str, Any]) -> list[str]:
    """Produce human-readable recommendations from the benchmark data."""
    recs: list[str] = []

    if df.empty:
        return ["No query data to analyse."]

    timed = df[df["cold_time_ms"] > 0]
    if timed.empty:
        return recs

    # Cold / warm ratio
    with_warm = timed[timed["avg_warm_ms"] > 0]
    if not with_warm.empty:
        avg_ratio = (with_warm["cold_time_ms"] / with_warm["avg_warm_ms"]).mean()
        if avg_ratio > 3:
            recs.append(
                f"Cold queries are on average {avg_ratio:.1f}x slower than warm. "
                "Consider connection pooling or keep-alive queries to avoid cold starts."
            )

    # Slow queries
    p90 = timed["avg_warm_ms"].quantile(0.9)
    slow = timed[timed["avg_warm_ms"] > p90]
    if not slow.empty:
        names = ", ".join(slow["query_name"].tolist())
        recs.append(f"Top-10% slowest queries (>{p90:.0f} ms warm): {names}. Review their query plans.")

    # Joins
    joins = timed[timed["category"] == "join"]
    if not joins.empty and joins["avg_warm_ms"].mean() > timed["avg_warm_ms"].median() * 2:
        recs.append(
            "Join queries are significantly slower than the median. "
            "Consider denormalising hot paths or using materialised views."
        )

    # Insert throughput
    inserts = results.get("insert_results", [])
    if inserts:
        best = max(inserts, key=lambda r: r["rows_per_second"])
        recs.append(
            f"Best insert throughput: {best['rows_per_second']:,.0f} rows/s at batch size {best['batch_size']:,}. "
            "Use this batch size for bulk loads."
        )

    if not recs:
        recs.append("All benchmarks look healthy. No specific optimisations recommended.")

    return recs


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def evaluate(results_dir: str | Path = "results", output_dir: str | Path = "results") -> None:
    """Load the latest benchmark results, generate charts and a summary report.

    Parameters
    ----------
    results_dir:
        Where to find ``benchmark_*.json`` files.
    output_dir:
        Where to save chart PNGs and the summary report.
    """
    all_results = load_results(results_dir)
    if not all_results:
        return

    latest = all_results[0]
    df = _results_to_df(latest)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    console.print("\n[bold cyan]Generating evaluation charts...[/bold cyan]")

    charts: list[Path] = []
    charts.append(_chart_query_latency(df, out))
    charts.append(_chart_by_category(df, out))
    charts.append(_chart_memory(df, out))
    insert_chart = _chart_insert_throughput(latest, out)
    if insert_chart:
        charts.append(insert_chart)

    for c in charts:
        if c and c.exists():
            console.print(f"  [green]✓[/green] {c}")

    # Recommendations
    recs = _generate_recommendations(df, latest)

    # Summary table
    console.print()
    rt = RichTable(title="Evaluation Summary", show_lines=True)
    rt.add_column("Metric", style="cyan")
    rt.add_column("Value", style="green")

    timed = df[df["cold_time_ms"] > 0]
    if not timed.empty:
        rt.add_row("Total queries benchmarked", str(len(timed)))
        rt.add_row("Avg cold latency (ms)", f"{timed['cold_time_ms'].mean():.1f}")
        rt.add_row("Avg warm latency (ms)", f"{timed['avg_warm_ms'].mean():.1f}")
        rt.add_row("P50 warm latency (ms)", f"{timed['avg_warm_ms'].median():.1f}")
        rt.add_row("P95 warm latency (ms)", f"{timed['avg_warm_ms'].quantile(0.95):.1f}")
        rt.add_row("Fastest query", timed.loc[timed["avg_warm_ms"].idxmin(), "query_name"])
        rt.add_row("Slowest query", timed.loc[timed["avg_warm_ms"].idxmax(), "query_name"])
    rt.add_row("ClickHouse version", latest.get("clickhouse_version", "unknown"))
    console.print(rt)

    # Recommendations panel
    rec_text = "\n".join(f"  {i+1}. {r}" for i, r in enumerate(recs))
    console.print(Panel(rec_text, title="Recommendations", border_style="yellow"))

    # Write summary to file
    summary_path = out / "evaluation_summary.txt"
    with open(summary_path, "w") as f:
        f.write(f"ClickHouse Benchmark Evaluation\n")
        f.write(f"{'=' * 40}\n")
        f.write(f"Timestamp: {latest.get('timestamp', 'N/A')}\n")
        f.write(f"ClickHouse Version: {latest.get('clickhouse_version', 'N/A')}\n\n")
        if not timed.empty:
            f.write(f"Queries Benchmarked: {len(timed)}\n")
            f.write(f"Avg Cold Latency:    {timed['cold_time_ms'].mean():.1f} ms\n")
            f.write(f"Avg Warm Latency:    {timed['avg_warm_ms'].mean():.1f} ms\n")
            f.write(f"P50 Warm Latency:    {timed['avg_warm_ms'].median():.1f} ms\n")
            f.write(f"P95 Warm Latency:    {timed['avg_warm_ms'].quantile(0.95):.1f} ms\n\n")
        f.write("Recommendations:\n")
        for i, r in enumerate(recs):
            f.write(f"  {i+1}. {r}\n")
    console.print(f"\n[green]Summary saved to {summary_path}[/green]")


def compare(results_dir: str | Path = "results") -> None:
    """Compare the two most recent benchmark runs side-by-side."""
    all_results = load_results(results_dir)
    if len(all_results) < 2:
        console.print("[yellow]Need at least 2 benchmark runs to compare.[/yellow]")
        return

    current, previous = all_results[0], all_results[1]
    df_cur = _results_to_df(current)
    df_prev = _results_to_df(previous)

    merged = pd.merge(
        df_cur[["query_name", "category", "avg_warm_ms"]],
        df_prev[["query_name", "avg_warm_ms"]],
        on="query_name",
        suffixes=("_current", "_previous"),
        how="outer",
    )
    merged["delta_ms"] = merged["avg_warm_ms_current"] - merged["avg_warm_ms_previous"]
    merged["delta_pct"] = (
        (merged["delta_ms"] / merged["avg_warm_ms_previous"]) * 100
    ).round(1)

    rt = RichTable(title="Run Comparison (current vs previous)", show_lines=True)
    rt.add_column("Query", style="cyan")
    rt.add_column("Category", style="blue")
    rt.add_column("Current (ms)", justify="right")
    rt.add_column("Previous (ms)", justify="right")
    rt.add_column("Delta (ms)", justify="right")
    rt.add_column("Change %", justify="right")

    for _, row in merged.iterrows():
        delta = row["delta_ms"]
        pct = row["delta_pct"]
        style = "[green]" if delta < 0 else "[red]" if delta > 0 else ""
        end_style = "[/green]" if delta < 0 else "[/red]" if delta > 0 else ""
        rt.add_row(
            str(row["query_name"]),
            str(row.get("category", "")),
            f"{row['avg_warm_ms_current']:.1f}" if pd.notna(row["avg_warm_ms_current"]) else "—",
            f"{row['avg_warm_ms_previous']:.1f}" if pd.notna(row["avg_warm_ms_previous"]) else "—",
            f"{style}{delta:+.1f}{end_style}" if pd.notna(delta) else "—",
            f"{style}{pct:+.1f}%{end_style}" if pd.notna(pct) else "—",
        )

    console.print(rt)
