"""CLI entrypoint for the ClickHouse Cloud benchmarking toolkit.

Usage::

    # Show help
    clickhouse-bench --help

    # Run individual steps
    clickhouse-bench setup [--drop]
    clickhouse-bench seed [--scale 10000]
    clickhouse-bench benchmark [--warm-runs 5] [--category aggregation]
    clickhouse-bench evaluate
    clickhouse-bench compare

    # Compare ClickHouse features (engines, codecs, partitioning, etc.)
    clickhouse-bench compare-features [--comparison engines] [--warm-runs 3]
    clickhouse-bench cleanup-variants

    # Run everything end-to-end
    clickhouse-bench full [--scale 10000]
"""

from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table as RichTable

from .config import CONFIG, ClickHouseConfig
from .queries import Category

console = Console()


def _print_banner() -> None:
    console.print(
        "\n[bold cyan]╔══════════════════════════════════════════╗[/bold cyan]"
    )
    console.print(
        "[bold cyan]║   ClickHouse Cloud Benchmark Toolkit     ║[/bold cyan]"
    )
    console.print(
        "[bold cyan]╚══════════════════════════════════════════╝[/bold cyan]\n"
    )


def _print_config(cfg: ClickHouseConfig) -> None:
    rt = RichTable(title="Connection Config")
    rt.add_column("Parameter", style="cyan")
    rt.add_column("Value", style="green")
    for k, v in cfg.summary().items():
        rt.add_row(k, v)
    console.print(rt)


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group()
@click.option("--host", envvar="CLICKHOUSE_HOST", default=None, help="ClickHouse host")
@click.option("--port", envvar="CLICKHOUSE_PORT", default=None, type=int, help="ClickHouse port")
@click.option("--user", envvar="CLICKHOUSE_USER", default=None, help="ClickHouse user")
@click.option("--password", envvar="CLICKHOUSE_PASSWORD", default=None, help="ClickHouse password")
@click.option("--database", envvar="CLICKHOUSE_DATABASE", default=None, help="Database name")
@click.pass_context
def cli(ctx: click.Context, host, port, user, password, database) -> None:
    """ClickHouse Cloud benchmarking toolkit.

    Set connection details via environment variables (CLICKHOUSE_HOST, etc.),
    a .env file, or command-line options.
    """
    _print_banner()

    # Build config, overriding env-based defaults with any CLI flags
    overrides: dict[str, object] = {}
    if host is not None:
        overrides["host"] = host
    if port is not None:
        overrides["port"] = port
    if user is not None:
        overrides["user"] = user
    if password is not None:
        overrides["password"] = password
    if database is not None:
        overrides["database"] = database

    from dataclasses import fields as dc_fields

    base = CONFIG
    if overrides:
        vals = {f.name: getattr(base, f.name) for f in dc_fields(base)}
        vals.update(overrides)
        base = ClickHouseConfig(**vals)

    ctx.ensure_object(dict)
    ctx.obj["config"] = base
    _print_config(base)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--drop", is_flag=True, help="Drop existing tables before creating")
@click.pass_context
def setup(ctx: click.Context, drop: bool) -> None:
    """Create benchmark tables in ClickHouse."""
    from .setup_db import setup_database

    cfg = ctx.obj["config"]
    setup_database(cfg, drop_existing=drop)
    console.print("\n[bold green]Setup complete.[/bold green]")


@cli.command()
@click.option("--scale", default=1_000, type=int, show_default=True, help="Base row count (users table)")
@click.pass_context
def seed(ctx: click.Context, scale: int) -> None:
    """Generate and insert test data."""
    from .seed_data import seed_data

    cfg = ctx.obj["config"]
    seed_data(cfg, scale=scale)
    console.print("\n[bold green]Seeding complete.[/bold green]")


@cli.command()
@click.option("--warm-runs", default=3, type=int, show_default=True, help="Number of warm runs per query")
@click.option(
    "--category", "categories", multiple=True,
    type=click.Choice([c.value for c in Category], case_sensitive=False),
    help="Only run queries in this category (repeatable)",
)
@click.option("--output-dir", default="results", show_default=True, help="Directory for JSON results")
@click.pass_context
def benchmark(ctx: click.Context, warm_runs: int, categories: tuple[str, ...], output_dir: str) -> None:
    """Run benchmark queries and measure performance."""
    from .benchmark import run_benchmarks

    cfg = ctx.obj["config"]
    cat_enums = [Category(c) for c in categories] if categories else None
    run_benchmarks(cfg, warm_runs=warm_runs, categories=cat_enums, output_dir=output_dir)
    console.print("\n[bold green]Benchmark complete.[/bold green]")


@cli.command()
@click.option("--results-dir", default="results", show_default=True)
@click.option("--output-dir", default="results", show_default=True)
@click.pass_context
def evaluate(ctx: click.Context, results_dir: str, output_dir: str) -> None:
    """Analyse benchmark results and generate charts."""
    from .evaluate import evaluate as do_evaluate

    do_evaluate(results_dir=results_dir, output_dir=output_dir)
    console.print("\n[bold green]Evaluation complete.[/bold green]")


@cli.command()
@click.option("--results-dir", default="results", show_default=True)
@click.pass_context
def compare(ctx: click.Context, results_dir: str) -> None:
    """Compare the two most recent benchmark runs."""
    from .evaluate import compare as do_compare

    do_compare(results_dir=results_dir)


@cli.command(name="compare-features")
@click.option(
    "--comparison", "comparisons", multiple=True,
    help="Comparison key to run (repeatable). Defaults to all.",
)
@click.option("--warm-runs", default=3, type=int, show_default=True,
              help="Warm runs per test query")
@click.option("--keep-existing", is_flag=True,
              help="Reuse existing variant tables instead of dropping them first")
@click.option("--output-dir", default="results", show_default=True)
@click.pass_context
def compare_features(
    ctx: click.Context,
    comparisons: tuple[str, ...],
    warm_runs: int,
    keep_existing: bool,
    output_dir: str,
) -> None:
    """Compare ClickHouse features side-by-side.

    Builds multiple schema variants of the same data (engines, codecs,
    PARTITION BY strategies, ORDER BY keys, skip indexes, projections,
    materialized views, LowCardinality vs String) and benchmarks each
    against representative queries.  Source data is read from the seeded
    base tables — run `clickhouse-bench seed` first.
    """
    from .compare_features import run_feature_comparisons
    from .schema_variants import comparison_keys

    cfg = ctx.obj["config"]
    keys = list(comparisons) if comparisons else None
    if keys:
        valid = set(comparison_keys())
        bad = [k for k in keys if k not in valid]
        if bad:
            console.print(f"[red]Unknown comparison key(s): {bad}.[/red] "
                          f"Valid: {sorted(valid)}")
            return
    run_feature_comparisons(
        cfg,
        keys=keys,
        warm_runs=warm_runs,
        drop_existing=not keep_existing,
        output_dir=output_dir,
    )
    console.print("\n[bold green]Feature comparison complete.[/bold green]")


@cli.command(name="cleanup-variants")
@click.pass_context
def cleanup_variants_cmd(ctx: click.Context) -> None:
    """Drop every comparison variant table (cmp_* tables and the MV view)."""
    from .compare_features import cleanup_variants

    cleanup_variants(ctx.obj["config"])


@cli.command(name="list-comparisons")
@click.pass_context
def list_comparisons_cmd(ctx: click.Context) -> None:
    """List the available feature comparisons."""
    from .schema_variants import ALL_COMPARISONS

    rt = RichTable(title="Available feature comparisons")
    rt.add_column("Key", style="cyan")
    rt.add_column("Title", style="green")
    rt.add_column("Variants", justify="right")
    rt.add_column("Test queries", justify="right")
    for c in ALL_COMPARISONS:
        rt.add_row(c.key, c.title, str(len(c.variants)), str(len(c.test_queries)))
    console.print(rt)


@cli.command()
@click.option("--scale", default=1_000, type=int, show_default=True, help="Base row count")
@click.option("--warm-runs", default=3, type=int, show_default=True, help="Warm runs per query")
@click.option("--drop", is_flag=True, help="Drop existing tables first")
@click.option("--output-dir", default="results", show_default=True)
@click.pass_context
def full(ctx: click.Context, scale: int, warm_runs: int, drop: bool, output_dir: str) -> None:
    """Run the full pipeline: setup -> seed -> benchmark -> evaluate."""
    from .benchmark import run_benchmarks
    from .evaluate import evaluate as do_evaluate
    from .seed_data import seed_data
    from .setup_db import setup_database

    cfg = ctx.obj["config"]

    console.rule("[bold]Step 1/4: Setup[/bold]")
    setup_database(cfg, drop_existing=drop)

    console.rule("[bold]Step 2/4: Seed Data[/bold]")
    seed_data(cfg, scale=scale)

    console.rule("[bold]Step 3/4: Benchmark[/bold]")
    run_benchmarks(cfg, warm_runs=warm_runs, output_dir=output_dir)

    console.rule("[bold]Step 4/4: Evaluate[/bold]")
    do_evaluate(results_dir=output_dir, output_dir=output_dir)

    console.print("\n[bold green]Full pipeline complete![/bold green]")


def main() -> None:
    """Entry point for ``python -m src.main``."""
    cli()


if __name__ == "__main__":
    main()
