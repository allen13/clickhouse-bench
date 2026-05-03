"""Benchmark query library for ClickHouse Cloud.

Each query is a ``BenchmarkQuery`` with a name, category, SQL text, and an
optional description.  Queries are grouped by category so the benchmark runner
can report per-category results.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Category(str, Enum):
    """Benchmark query categories."""

    POINT = "point_query"
    RANGE = "range_scan"
    AGGREGATION = "aggregation"
    JOIN = "join"
    WINDOW = "window_function"
    TIMESERIES = "time_series"
    INSERT = "insert_performance"
    COMPRESSION = "compression"


@dataclass(frozen=True)
class BenchmarkQuery:
    """A single benchmark query definition."""

    name: str
    category: Category
    sql: str
    description: str = ""


# ---------------------------------------------------------------------------
# Query catalogue
# ---------------------------------------------------------------------------

QUERIES: list[BenchmarkQuery] = [
    # ── Point queries ─────────────────────────────────────────────────
    BenchmarkQuery(
        name="point_user_by_id",
        category=Category.POINT,
        sql="SELECT * FROM users WHERE id = 42",
        description="Single-row lookup by primary key",
    ),
    BenchmarkQuery(
        name="point_order_by_id",
        category=Category.POINT,
        sql="SELECT * FROM orders WHERE id = 100",
        description="Single-row order lookup by primary key",
    ),

    # ── Range scans ───────────────────────────────────────────────────
    BenchmarkQuery(
        name="range_orders_date",
        category=Category.RANGE,
        sql="""
            SELECT *
            FROM orders
            WHERE order_date BETWEEN '2025-01-01' AND '2025-06-30'
            LIMIT 10000
        """,
        description="Date-range scan on orders",
    ),
    BenchmarkQuery(
        name="range_users_age",
        category=Category.RANGE,
        sql="SELECT * FROM users WHERE age BETWEEN 25 AND 35",
        description="Numeric-range scan on user age",
    ),
    BenchmarkQuery(
        name="range_events_time",
        category=Category.RANGE,
        sql="""
            SELECT *
            FROM events
            WHERE event_time >= now() - INTERVAL 7 DAY
            LIMIT 50000
        """,
        description="Recent-events range scan (last 7 days)",
    ),

    # ── Aggregations ──────────────────────────────────────────────────
    BenchmarkQuery(
        name="agg_count_by_country",
        category=Category.AGGREGATION,
        sql="""
            SELECT country, count() AS cnt
            FROM users
            GROUP BY country
            ORDER BY cnt DESC
        """,
        description="Low-cardinality GROUP BY (country)",
    ),
    BenchmarkQuery(
        name="agg_revenue_by_category",
        category=Category.AGGREGATION,
        sql="""
            SELECT category,
                   count()          AS order_count,
                   sum(total_price) AS revenue,
                   avg(total_price) AS avg_order
            FROM orders
            GROUP BY category
            ORDER BY revenue DESC
        """,
        description="Revenue aggregation by product category",
    ),
    BenchmarkQuery(
        name="agg_daily_events",
        category=Category.AGGREGATION,
        sql="""
            SELECT toDate(event_time) AS day,
                   event_type,
                   count() AS cnt
            FROM events
            GROUP BY day, event_type
            ORDER BY day, cnt DESC
        """,
        description="High-cardinality GROUP BY (day × event_type)",
    ),
    BenchmarkQuery(
        name="agg_heavy_uniq",
        category=Category.AGGREGATION,
        sql="""
            SELECT toDate(event_time) AS day,
                   uniqExact(user_id) AS unique_users,
                   count()            AS total_events
            FROM events
            GROUP BY day
            ORDER BY day
        """,
        description="Distinct-count aggregation per day",
    ),

    # ── Joins ─────────────────────────────────────────────────────────
    BenchmarkQuery(
        name="join_user_orders",
        category=Category.JOIN,
        sql="""
            SELECT u.username,
                   u.country,
                   count()            AS order_count,
                   sum(o.total_price) AS total_spent
            FROM orders AS o
            INNER JOIN users AS u ON o.user_id = u.id
            GROUP BY u.username, u.country
            ORDER BY total_spent DESC
            LIMIT 100
        """,
        description="Two-table join: users × orders",
    ),
    BenchmarkQuery(
        name="join_three_tables",
        category=Category.JOIN,
        sql="""
            SELECT u.country,
                   o.category,
                   count(DISTINCT e.session_id) AS sessions,
                   sum(o.total_price)            AS revenue
            FROM users AS u
            INNER JOIN orders  AS o ON o.user_id = u.id
            INNER JOIN events  AS e ON e.user_id = u.id
            GROUP BY u.country, o.category
            ORDER BY revenue DESC
            LIMIT 50
        """,
        description="Three-table join: users × orders × events",
    ),

    # ── Window functions ──────────────────────────────────────────────
    BenchmarkQuery(
        name="window_running_total",
        category=Category.WINDOW,
        sql="""
            SELECT user_id,
                   order_date,
                   total_price,
                   sum(total_price) OVER (
                       PARTITION BY user_id
                       ORDER BY order_date
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                   ) AS running_total
            FROM orders
            ORDER BY user_id, order_date
            LIMIT 1000
        """,
        description="Running total per user",
    ),
    BenchmarkQuery(
        name="window_rank_users",
        category=Category.WINDOW,
        sql="""
            SELECT user_id,
                   total_spent,
                   rank() OVER (ORDER BY total_spent DESC) AS spend_rank
            FROM (
                SELECT user_id, sum(total_price) AS total_spent
                FROM orders
                GROUP BY user_id
            )
            LIMIT 100
        """,
        description="Rank users by total spend",
    ),
    BenchmarkQuery(
        name="window_moving_avg",
        category=Category.WINDOW,
        sql="""
            SELECT sensor_id,
                   timestamp,
                   value,
                   avg(value) OVER (
                       PARTITION BY sensor_id
                       ORDER BY timestamp
                       ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
                   ) AS moving_avg_7
            FROM metrics
            WHERE sensor_id = 1
            ORDER BY timestamp
            LIMIT 500
        """,
        description="7-point moving average on metrics",
    ),

    # ── Time-series queries ───────────────────────────────────────────
    BenchmarkQuery(
        name="ts_downsample_1min",
        category=Category.TIMESERIES,
        sql="""
            SELECT sensor_id,
                   toStartOfMinute(timestamp) AS minute,
                   avg(value) AS avg_val,
                   min(value) AS min_val,
                   max(value) AS max_val
            FROM metrics
            GROUP BY sensor_id, minute
            ORDER BY sensor_id, minute
            LIMIT 5000
        """,
        description="Downsample metrics to 1-minute buckets",
    ),
    BenchmarkQuery(
        name="ts_downsample_1hr",
        category=Category.TIMESERIES,
        sql="""
            SELECT sensor_id,
                   toStartOfHour(timestamp) AS hour,
                   avg(value)   AS avg_val,
                   count()      AS samples
            FROM metrics
            GROUP BY sensor_id, hour
            ORDER BY sensor_id, hour
        """,
        description="Downsample metrics to 1-hour buckets",
    ),
    BenchmarkQuery(
        name="ts_rate_of_change",
        category=Category.TIMESERIES,
        sql="""
            SELECT sensor_id,
                   timestamp,
                   value,
                   value - lagInFrame(value, 1, 0) OVER (
                       PARTITION BY sensor_id ORDER BY timestamp
                   ) AS delta
            FROM metrics
            WHERE sensor_id <= 5
            ORDER BY sensor_id, timestamp
            LIMIT 2000
        """,
        description="Rate-of-change calculation across sensors",
    ),
    BenchmarkQuery(
        name="ts_gap_detection",
        category=Category.TIMESERIES,
        sql="""
            SELECT sensor_id,
                   timestamp,
                   dateDiff('second', lagInFrame(timestamp, 1, timestamp) OVER (
                       PARTITION BY sensor_id ORDER BY timestamp
                   ), timestamp) AS gap_seconds
            FROM metrics
            WHERE sensor_id = 1
            HAVING gap_seconds > 30
            ORDER BY timestamp
            LIMIT 500
        """,
        description="Detect gaps > 30 s in sensor data",
    ),

    # ── Compression ───────────────────────────────────────────────────
    BenchmarkQuery(
        name="compression_ratio",
        category=Category.COMPRESSION,
        sql="""
            SELECT
                name                                              AS table_name,
                formatReadableSize(total_bytes)                   AS compressed,
                formatReadableSize(total_bytes * decompress_ratio) AS uncompressed,
                round(decompress_ratio, 2)                        AS ratio
            FROM (
                SELECT
                    name,
                    total_bytes,
                    if(total_bytes > 0,
                       (SELECT sum(data_uncompressed_bytes) / sum(data_compressed_bytes)
                        FROM system.parts
                        WHERE table = t.name AND database = currentDatabase() AND active),
                       1) AS decompress_ratio
                FROM system.tables AS t
                WHERE database = currentDatabase()
                  AND name IN ('users', 'orders', 'events', 'metrics')
            )
            ORDER BY name
        """,
        description="Measure compression ratios for all benchmark tables",
    ),
    BenchmarkQuery(
        name="column_compression",
        category=Category.COMPRESSION,
        sql="""
            SELECT
                table,
                column,
                formatReadableSize(sum(data_compressed_bytes))   AS compressed,
                formatReadableSize(sum(data_uncompressed_bytes)) AS uncompressed,
                round(sum(data_uncompressed_bytes) / greatest(sum(data_compressed_bytes), 1), 2) AS ratio
            FROM system.parts_columns
            WHERE database = currentDatabase()
              AND table IN ('users', 'orders', 'events', 'metrics')
              AND active
            GROUP BY table, column
            ORDER BY table, ratio DESC
        """,
        description="Per-column compression breakdown",
    ),
]


def get_queries_by_category(category: Category | None = None) -> list[BenchmarkQuery]:
    """Return queries filtered by category (or all if *category* is ``None``)."""
    if category is None:
        return list(QUERIES)
    return [q for q in QUERIES if q.category == category]


def list_categories() -> list[Category]:
    """Return all distinct categories present in the query catalogue."""
    return sorted({q.category for q in QUERIES}, key=lambda c: c.value)
