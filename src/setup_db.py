"""Database schema setup for ClickHouse Cloud benchmarks.

Creates the tables used for benchmarking: users, orders, events, and metrics
(time-series). All tables use the MergeTree family with appropriate ordering
keys optimised for the benchmark query patterns.
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table as RichTable

from .config import ClickHouseConfig, get_client

console = Console()

# ---------------------------------------------------------------------------
# Schema definitions
# ---------------------------------------------------------------------------

SCHEMAS: dict[str, str] = {
    "users": """
        CREATE TABLE IF NOT EXISTS users (
            id          UInt64,
            username    String,
            email       String,
            full_name   String,
            country     LowCardinality(String),
            city        String,
            age         UInt8,
            signup_date Date,
            is_active   UInt8,
            created_at  DateTime DEFAULT now()
        )
        ENGINE = MergeTree()
        ORDER BY (id)
        SETTINGS index_granularity = 8192
    """,
    "orders": """
        CREATE TABLE IF NOT EXISTS orders (
            id          UInt64,
            user_id     UInt64,
            product     LowCardinality(String),
            category    LowCardinality(String),
            quantity    UInt32,
            unit_price  Float64,
            total_price Float64,
            status      LowCardinality(String),
            order_date  Date,
            created_at  DateTime DEFAULT now()
        )
        ENGINE = MergeTree()
        ORDER BY (user_id, order_date)
        SETTINGS index_granularity = 8192
    """,
    "events": """
        CREATE TABLE IF NOT EXISTS events (
            id          UInt64,
            user_id     UInt64,
            event_type  LowCardinality(String),
            page        String,
            session_id  String,
            properties  String,
            event_time  DateTime,
            created_at  DateTime DEFAULT now()
        )
        ENGINE = MergeTree()
        ORDER BY (user_id, event_time)
        SETTINGS index_granularity = 8192
    """,
    "metrics": """
        CREATE TABLE IF NOT EXISTS metrics (
            sensor_id   UInt32,
            metric_name LowCardinality(String),
            value       Float64,
            tags        String,
            timestamp   DateTime
        )
        ENGINE = MergeTree()
        ORDER BY (sensor_id, timestamp)
        SETTINGS index_granularity = 8192
    """,
}


def setup_database(cfg: ClickHouseConfig | None = None, *, drop_existing: bool = False) -> None:
    """Create all benchmark tables.

    Parameters
    ----------
    cfg:
        Optional connection config override.
    drop_existing:
        If ``True``, drop tables before re-creating them.
    """
    client = get_client(cfg)

    if drop_existing:
        console.print("[bold yellow]Dropping existing tables...[/bold yellow]")
        for name in SCHEMAS:
            client.command(f"DROP TABLE IF EXISTS {name}")

    console.print("[bold cyan]Creating benchmark tables...[/bold cyan]")
    for name, ddl in SCHEMAS.items():
        client.command(ddl)
        console.print(f"  [green]✓[/green] {name}")

    # Print summary
    _show_table_summary(client)


def _show_table_summary(client) -> None:
    """Display a summary of the tables that exist in the database."""
    rt = RichTable(title="Database Tables", show_lines=True)
    rt.add_column("Table", style="cyan")
    rt.add_column("Engine", style="green")
    rt.add_column("Rows", justify="right")
    rt.add_column("Size (bytes)", justify="right")

    result = client.query(
        """
        SELECT
            name,
            engine,
            total_rows,
            total_bytes
        FROM system.tables
        WHERE database = currentDatabase()
          AND name IN ('users', 'orders', 'events', 'metrics')
        ORDER BY name
        """
    )
    for row in result.result_rows:
        rt.add_row(row[0], row[1], f"{row[2]:,}", f"{row[3]:,}")

    console.print(rt)
