"""Test data generation and insertion using Faker.

Generates realistic data for all benchmark tables with configurable row
counts.  Data is inserted in batches to avoid memory issues at large scales.
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta
from typing import Any

from faker import Faker
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from .config import ClickHouseConfig, get_client

console = Console()
fake = Faker()
Faker.seed(42)
random.seed(42)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BATCH_SIZE = 10_000

PRODUCT_CATEGORIES: dict[str, list[str]] = {
    "Electronics": ["Laptop", "Phone", "Tablet", "Monitor", "Headphones", "Camera", "Speaker"],
    "Clothing": ["T-Shirt", "Jeans", "Jacket", "Sneakers", "Hat", "Dress", "Socks"],
    "Home": ["Desk", "Chair", "Lamp", "Rug", "Pillow", "Blender", "Toaster"],
    "Books": ["Novel", "Textbook", "Cookbook", "Biography", "Comic", "Manual", "Guide"],
    "Sports": ["Basketball", "Tennis Racket", "Yoga Mat", "Dumbbells", "Running Shoes"],
}

EVENT_TYPES = ["page_view", "click", "scroll", "form_submit", "purchase", "signup", "logout", "search", "error"]
PAGES = ["/home", "/products", "/cart", "/checkout", "/profile", "/settings", "/search", "/about", "/contact", "/blog"]
ORDER_STATUSES = ["pending", "confirmed", "shipped", "delivered", "cancelled", "returned"]
METRIC_NAMES = ["cpu_usage", "memory_usage", "disk_io", "network_in", "network_out", "request_latency", "error_rate"]
COUNTRIES = ["US", "UK", "DE", "FR", "JP", "AU", "CA", "BR", "IN", "KR", "MX", "ES", "IT", "NL", "SE"]


# ---------------------------------------------------------------------------
# Row generators
# ---------------------------------------------------------------------------

def _generate_users(count: int, start_id: int = 1) -> list[list[Any]]:
    """Generate a batch of user rows."""
    rows: list[list[Any]] = []
    for i in range(count):
        uid = start_id + i
        signup = fake.date_between(start_date="-3y", end_date="today")
        rows.append([
            uid,
            fake.user_name(),
            fake.email(),
            fake.name(),
            random.choice(COUNTRIES),
            fake.city(),
            random.randint(18, 80),
            signup,
            random.randint(0, 1),
        ])
    return rows


def _generate_orders(count: int, max_user_id: int, start_id: int = 1) -> list[list[Any]]:
    """Generate a batch of order rows."""
    rows: list[list[Any]] = []
    for i in range(count):
        category = random.choice(list(PRODUCT_CATEGORIES.keys()))
        product = random.choice(PRODUCT_CATEGORIES[category])
        qty = random.randint(1, 10)
        price = round(random.uniform(5.0, 500.0), 2)
        rows.append([
            start_id + i,
            random.randint(1, max_user_id),
            product,
            category,
            qty,
            price,
            round(qty * price, 2),
            random.choice(ORDER_STATUSES),
            fake.date_between(start_date="-2y", end_date="today"),
        ])
    return rows


def _generate_events(count: int, max_user_id: int, start_id: int = 1) -> list[list[Any]]:
    """Generate a batch of event rows."""
    rows: list[list[Any]] = []
    base_time = datetime.now() - timedelta(days=365)
    for i in range(count):
        event_time = base_time + timedelta(seconds=random.randint(0, 365 * 86400))
        rows.append([
            start_id + i,
            random.randint(1, max_user_id),
            random.choice(EVENT_TYPES),
            random.choice(PAGES),
            fake.uuid4(),
            "{}",
            event_time,
        ])
    return rows


def _generate_metrics(count: int) -> list[list[Any]]:
    """Generate a batch of time-series metric rows."""
    rows: list[list[Any]] = []
    num_sensors = 100
    base_time = datetime.now() - timedelta(days=30)
    for i in range(count):
        ts = base_time + timedelta(seconds=i * 10)  # 10-second intervals
        rows.append([
            random.randint(1, num_sensors),
            random.choice(METRIC_NAMES),
            round(random.uniform(0.0, 100.0), 4),
            "{}",
            ts,
        ])
    return rows


# ---------------------------------------------------------------------------
# Insert helpers
# ---------------------------------------------------------------------------

_COLUMN_NAMES: dict[str, list[str]] = {
    "users": ["id", "username", "email", "full_name", "country", "city", "age", "signup_date", "is_active"],
    "orders": ["id", "user_id", "product", "category", "quantity", "unit_price", "total_price", "status", "order_date"],
    "events": ["id", "user_id", "event_type", "page", "session_id", "properties", "event_time"],
    "metrics": ["sensor_id", "metric_name", "value", "tags", "timestamp"],
}


def _insert_batched(
    client,
    table: str,
    generator,
    total: int,
    progress: Progress,
    task_id,
    **gen_kwargs,
) -> float:
    """Insert *total* rows into *table* in batches, returning elapsed seconds."""
    start = time.perf_counter()
    inserted = 0
    while inserted < total:
        batch_size = min(BATCH_SIZE, total - inserted)
        rows = generator(batch_size, start_id=inserted + 1, **gen_kwargs)
        client.insert(table, rows, column_names=_COLUMN_NAMES[table])
        inserted += batch_size
        progress.update(task_id, completed=inserted)
    return time.perf_counter() - start


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def seed_data(
    cfg: ClickHouseConfig | None = None,
    *,
    scale: int = 1_000,
) -> dict[str, dict[str, float]]:
    """Seed all benchmark tables with test data.

    Parameters
    ----------
    cfg:
        Optional connection config override.
    scale:
        Base number of rows for the *users* table.  Other tables scale
        proportionally (orders = 3×, events = 10×, metrics = 5×).

    Returns
    -------
    dict
        Per-table stats: ``rows_inserted`` and ``elapsed_seconds``.
    """
    client = get_client(cfg)
    stats: dict[str, dict[str, float]] = {}

    table_counts = {
        "users": scale,
        "orders": scale * 3,
        "events": scale * 10,
        "metrics": scale * 5,
    }

    console.print(f"\n[bold cyan]Seeding data (scale={scale:,} base rows)...[/bold cyan]")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed:,}/{task.total:,}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        # Users
        tid = progress.add_task("users", total=table_counts["users"])
        elapsed = _insert_batched(client, "users", _generate_users, table_counts["users"], progress, tid)
        stats["users"] = {"rows_inserted": table_counts["users"], "elapsed_seconds": round(elapsed, 3)}

        max_user_id = table_counts["users"]

        # Orders
        tid = progress.add_task("orders", total=table_counts["orders"])
        elapsed = _insert_batched(
            client, "orders", _generate_orders, table_counts["orders"], progress, tid,
            max_user_id=max_user_id,
        )
        stats["orders"] = {"rows_inserted": table_counts["orders"], "elapsed_seconds": round(elapsed, 3)}

        # Events
        tid = progress.add_task("events", total=table_counts["events"])
        elapsed = _insert_batched(
            client, "events", _generate_events, table_counts["events"], progress, tid,
            max_user_id=max_user_id,
        )
        stats["events"] = {"rows_inserted": table_counts["events"], "elapsed_seconds": round(elapsed, 3)}

        # Metrics (generator doesn't take start_id/max_user_id)
        tid = progress.add_task("metrics", total=table_counts["metrics"])
        start = time.perf_counter()
        inserted = 0
        while inserted < table_counts["metrics"]:
            batch = min(BATCH_SIZE, table_counts["metrics"] - inserted)
            rows = _generate_metrics(batch)
            client.insert("metrics", rows, column_names=_COLUMN_NAMES["metrics"])
            inserted += batch
            progress.update(tid, completed=inserted)
        elapsed = time.perf_counter() - start
        stats["metrics"] = {"rows_inserted": table_counts["metrics"], "elapsed_seconds": round(elapsed, 3)}

    # Print summary
    from rich.table import Table as RichTable

    rt = RichTable(title="Seed Summary")
    rt.add_column("Table", style="cyan")
    rt.add_column("Rows", justify="right", style="green")
    rt.add_column("Time (s)", justify="right", style="yellow")
    rt.add_column("Rows/sec", justify="right", style="magenta")
    for tbl, s in stats.items():
        rps = s["rows_inserted"] / s["elapsed_seconds"] if s["elapsed_seconds"] > 0 else 0
        rt.add_row(tbl, f"{int(s['rows_inserted']):,}", f"{s['elapsed_seconds']:.3f}", f"{rps:,.0f}")
    console.print(rt)

    return stats
