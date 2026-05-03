"""Feature-comparison runner.

Given a :class:`schema_variants.FeatureComparison`, this module:

1. Drops + recreates each variant table (and any helper objects).
2. Loads the same source data into each variant via its ``insert_sql``.
3. Optionally runs ``OPTIMIZE TABLE ... FINAL`` for engines that need it.
4. Measures storage (compressed bytes, uncompressed bytes, compression ratio).
5. Runs each test query against each variant — cold + warm timings.
6. Renders side-by-side rich tables and saves a JSON report.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table as RichTable

from .config import ClickHouseConfig, get_client
from .schema_variants import (
    ALL_COMPARISONS,
    FeatureComparison,
    SchemaVariant,
    comparison_keys,
    get_comparison,
)

console = Console()


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class VariantResult:
    """Measurement results for one variant within a comparison."""

    variant_name: str
    feature_label: str
    rows: int
    bytes_compressed: int
    bytes_uncompressed: int
    compression_ratio: float
    parts_count: int
    insert_seconds: float
    query_timings_ms: dict[str, dict[str, float]] = field(default_factory=dict)
    error: str | None = None


@dataclass
class ComparisonResult:
    """Result of running one full :class:`FeatureComparison`."""

    key: str
    title: str
    timestamp: str
    variants: list[VariantResult] = field(default_factory=list)
    insight: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _drop_variant(client, variant: SchemaVariant) -> None:
    """Drop a variant's table (and any associated MV)."""
    # Drop the variant table itself
    client.command(f"DROP TABLE IF EXISTS {variant.name}")
    # If there's a MV pointing at this variant by convention (cmp_events_mv_view), drop it.
    if variant.name.endswith("_mv_source"):
        client.command("DROP TABLE IF EXISTS cmp_events_mv_view")


def _create_variant(client, variant: SchemaVariant) -> None:
    """Create a variant — the table itself, then any post_setup statements."""
    client.command(variant.ddl)
    for stmt in variant.post_setup:
        client.command(stmt)


def _load_variant(client, variant: SchemaVariant) -> float:
    """Load data into a variant via its ``insert_sql``.

    Returns the elapsed seconds.  Returns ``0.0`` if ``insert_sql`` is ``None``
    (e.g. the variant is the *target* of a materialized view and is filled by
    a trigger).
    """
    if variant.insert_sql is None:
        return 0.0

    start = time.perf_counter()
    client.command(variant.insert_sql)
    if variant.requires_optimize:
        client.command(f"OPTIMIZE TABLE {variant.name} FINAL")
    return time.perf_counter() - start


def _measure_storage(client, table_name: str) -> tuple[int, int, int, int]:
    """Return ``(rows, bytes_compressed, bytes_uncompressed, parts)``.

    Reads from ``system.parts`` and ``system.tables``.
    """
    rows_q = client.query(f"""
        SELECT
            sum(rows)                       AS rows,
            sum(bytes_on_disk)              AS bytes_compressed,
            sum(data_uncompressed_bytes)    AS bytes_uncompressed,
            countIf(active)                 AS parts
        FROM system.parts
        WHERE table = '{table_name}' AND database = currentDatabase()
    """)
    row = rows_q.result_rows[0] if rows_q.result_rows else (0, 0, 0, 0)
    return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0), int(row[3] or 0)


def _run_query(client, sql: str) -> float:
    """Execute *sql* and return elapsed milliseconds."""
    start = time.perf_counter()
    client.query(sql)
    return (time.perf_counter() - start) * 1000


def _time_query(client, sql: str, *, warm_runs: int) -> dict[str, float]:
    """Run a query once cold, then ``warm_runs`` warm runs.  Return a stats dict."""
    cold_ms = _run_query(client, sql)
    warm = [_run_query(client, sql) for _ in range(warm_runs)]
    return {
        "cold_ms": round(cold_ms, 2),
        "avg_warm_ms": round(sum(warm) / len(warm), 2) if warm else 0.0,
        "min_warm_ms": round(min(warm), 2) if warm else 0.0,
        "max_warm_ms": round(max(warm), 2) if warm else 0.0,
    }


# ---------------------------------------------------------------------------
# Per-comparison runner
# ---------------------------------------------------------------------------


def run_comparison(
    client,
    comparison: FeatureComparison,
    *,
    warm_runs: int = 3,
    drop_existing: bool = True,
) -> ComparisonResult:
    """Run a single :class:`FeatureComparison` end-to-end.

    Parameters
    ----------
    client:
        An open ``clickhouse_connect`` client.
    comparison:
        The comparison definition.
    warm_runs:
        How many warm-cache runs per test query.
    drop_existing:
        If ``True``, drop variant tables before re-creating them.
    """
    console.rule(f"[bold cyan]{comparison.title}[/bold cyan]")
    console.print(f"[dim]{comparison.description}[/dim]\n")

    # Verify base table exists and has rows
    base_rows = client.query(
        f"SELECT count() FROM {comparison.base_table}"
    ).result_rows[0][0]
    if base_rows == 0:
        console.print(
            f"[red]Base table `{comparison.base_table}` is empty.  "
            f"Run `clickhouse-bench seed` first.[/red]"
        )
        return ComparisonResult(
            key=comparison.key,
            title=comparison.title,
            timestamp=datetime.utcnow().isoformat(),
            insight=comparison.insight,
        )

    result = ComparisonResult(
        key=comparison.key,
        title=comparison.title,
        timestamp=datetime.utcnow().isoformat(),
        insight=comparison.insight,
    )

    # Phase 1: drop + create + load
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        tid = progress.add_task("[yellow]preparing variants", total=len(comparison.variants))

        for variant in comparison.variants:
            progress.update(tid, description=f"[yellow]preparing[/yellow] {variant.name}")
            try:
                if drop_existing:
                    _drop_variant(client, variant)
                _create_variant(client, variant)
                insert_seconds = _load_variant(client, variant)
                rows, comp, uncomp, parts = _measure_storage(client, variant.name)
                ratio = (uncomp / comp) if comp > 0 else 0.0

                vr = VariantResult(
                    variant_name=variant.name,
                    feature_label=variant.feature_label,
                    rows=rows,
                    bytes_compressed=comp,
                    bytes_uncompressed=uncomp,
                    compression_ratio=round(ratio, 2),
                    parts_count=parts,
                    insert_seconds=round(insert_seconds, 3),
                )
                result.variants.append(vr)
            except Exception as exc:
                result.variants.append(VariantResult(
                    variant_name=variant.name,
                    feature_label=variant.feature_label,
                    rows=0,
                    bytes_compressed=0,
                    bytes_uncompressed=0,
                    compression_ratio=0.0,
                    parts_count=0,
                    insert_seconds=0.0,
                    error=str(exc),
                ))
            progress.advance(tid)

    # Phase 2: query timings (only on healthy variants)
    healthy = [v for v in result.variants if v.error is None]
    if healthy:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            console=console,
        ) as progress:
            total_runs = len(healthy) * len(comparison.test_queries)
            tid = progress.add_task("[cyan]running queries", total=total_runs)

            for vr in healthy:
                for label, sql_template in comparison.test_queries:
                    sql = sql_template.format(table=vr.variant_name)
                    progress.update(tid, description=f"[cyan]{vr.variant_name}[/cyan] · {label}")
                    try:
                        vr.query_timings_ms[label] = _time_query(client, sql, warm_runs=warm_runs)
                    except Exception as exc:
                        vr.query_timings_ms[label] = {"error": str(exc)}
                    progress.advance(tid)

    _render_storage_table(result, comparison)
    _render_query_table(result, comparison)
    _render_insight(comparison)

    return result


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt_bytes(n: int) -> str:
    """Human-readable bytes."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _render_storage_table(result: ComparisonResult, comparison: FeatureComparison) -> None:
    """Print the storage / load summary table for a comparison."""
    rt = RichTable(title=f"Storage & load — {comparison.title}", show_lines=True)
    rt.add_column("Variant", style="cyan", no_wrap=False)
    rt.add_column("Feature", style="blue")
    rt.add_column("Rows", justify="right", style="green")
    rt.add_column("Compressed", justify="right", style="magenta")
    rt.add_column("Uncompressed", justify="right")
    rt.add_column("Ratio", justify="right", style="yellow")
    rt.add_column("Parts", justify="right")
    rt.add_column("Insert (s)", justify="right")
    rt.add_column("Status", justify="center")

    # Find baseline (smallest compressed) for highlighting
    healthy = [v for v in result.variants if v.error is None and v.bytes_compressed > 0]
    baseline_size = min((v.bytes_compressed for v in healthy), default=0)

    for vr in result.variants:
        status = "[green]OK[/green]" if vr.error is None else "[red]ERR[/red]"
        size_str = _fmt_bytes(vr.bytes_compressed)
        if vr.error is None and baseline_size and vr.bytes_compressed == baseline_size:
            size_str = f"[bold green]{size_str}[/bold green] *"
        rt.add_row(
            vr.variant_name,
            vr.feature_label,
            f"{vr.rows:,}" if vr.rows else "—",
            size_str,
            _fmt_bytes(vr.bytes_uncompressed) if vr.bytes_uncompressed else "—",
            f"{vr.compression_ratio:.2f}x" if vr.compression_ratio else "—",
            str(vr.parts_count),
            f"{vr.insert_seconds:.2f}",
            status,
        )

    if any(v.error is None and v.bytes_compressed == baseline_size for v in result.variants):
        rt.caption = "[dim]* smallest compressed size[/dim]"
    console.print(rt)


def _render_query_table(result: ComparisonResult, comparison: FeatureComparison) -> None:
    """Render warm-avg latencies as a side-by-side query × variant grid."""
    healthy = [v for v in result.variants if v.error is None and v.query_timings_ms]
    if not healthy:
        return

    rt = RichTable(title=f"Query latency (warm avg, ms) — {comparison.title}", show_lines=True)
    rt.add_column("Test query", style="cyan", no_wrap=False)
    for v in healthy:
        rt.add_column(v.feature_label, justify="right")

    # For each query, find the fastest variant for highlighting
    for label, _ in comparison.test_queries:
        timings = []
        for v in healthy:
            t = v.query_timings_ms.get(label, {})
            timings.append(t.get("avg_warm_ms"))
        valid = [t for t in timings if isinstance(t, (int, float))]
        fastest = min(valid) if valid else None

        row = [label]
        for v, t in zip(healthy, timings):
            if t is None:
                row.append("—")
            elif isinstance(t, (int, float)):
                cell = f"{t:.1f}"
                if fastest is not None and t == fastest and len(valid) > 1:
                    cell = f"[bold green]{cell}[/bold green]"
                row.append(cell)
            else:
                row.append("[red]err[/red]")
        rt.add_row(*row)

    rt.caption = "[dim]green = fastest in row[/dim]"
    console.print(rt)


def _render_insight(comparison: FeatureComparison) -> None:
    """Print the comparison's insight panel."""
    if comparison.insight:
        console.print(Panel(comparison.insight.strip(), title="Insight", border_style="yellow"))
    console.print()


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run_feature_comparisons(
    cfg: ClickHouseConfig | None = None,
    *,
    keys: list[str] | None = None,
    warm_runs: int = 3,
    drop_existing: bool = True,
    output_dir: str | Path = "results",
) -> list[ComparisonResult]:
    """Run a set of feature comparisons and save JSON results.

    Parameters
    ----------
    cfg:
        Optional connection config.
    keys:
        Optional list of comparison keys to run.  Defaults to all.
    warm_runs:
        Warm-cache runs per query.
    drop_existing:
        Drop variant tables before recreating.
    output_dir:
        Where to save the JSON report.
    """
    client = get_client(cfg)

    if keys:
        comparisons = [get_comparison(k) for k in keys]
    else:
        comparisons = list(ALL_COMPARISONS)

    results: list[ComparisonResult] = []

    console.print(
        f"\n[bold]Running {len(comparisons)} feature comparison(s):"
        f" {', '.join(c.key for c in comparisons)}[/bold]\n"
    )

    for cmp in comparisons:
        try:
            results.append(run_comparison(
                client, cmp, warm_runs=warm_runs, drop_existing=drop_existing,
            ))
        except Exception as exc:
            console.print(f"[red]Comparison {cmp.key!r} failed: {exc}[/red]")

    # Save report
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    json_path = out_path / f"compare_features_{ts}.json"
    json_path.write_text(json.dumps([_result_to_dict(r) for r in results], indent=2))
    console.print(f"[green]Comparison results saved to {json_path}[/green]\n")

    return results


def cleanup_variants(cfg: ClickHouseConfig | None = None) -> None:
    """Drop every cmp_* table (and the MV view) created by the comparisons."""
    from .schema_variants import all_variant_names

    client = get_client(cfg)
    console.print("[yellow]Dropping all comparison variants...[/yellow]")
    for name in all_variant_names():
        client.command(f"DROP TABLE IF EXISTS {name}")
    client.command("DROP TABLE IF EXISTS cmp_events_mv_view")
    console.print("[green]Cleanup complete.[/green]")


# ---------------------------------------------------------------------------
# JSON serialisation
# ---------------------------------------------------------------------------


def _result_to_dict(r: ComparisonResult) -> dict[str, Any]:
    return {
        "key": r.key,
        "title": r.title,
        "timestamp": r.timestamp,
        "insight": r.insight,
        "variants": [asdict(v) for v in r.variants],
    }
