"""Schema variants for ClickHouse feature comparisons.

Each :class:`FeatureComparison` defines a set of :class:`SchemaVariant` objects
that differ in exactly one dimension (the feature being studied).  The
``compare_features`` runner creates each variant, copies the same source data
into all of them, and measures storage + query latency side-by-side.

The ``{table}`` placeholder in ``test_queries`` is substituted with the variant
name at runtime, so a single query template can be run against every variant.

Special emphasis (per user request) is placed on:
  * **PARTITION BY** — covered with 4 partitioning strategies
  * **Index keys** — ORDER BY and skip indexes covered in depth
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class SchemaVariant:
    """One schema variant within a feature comparison.

    Attributes
    ----------
    name:
        Physical table name in ClickHouse (kept short, prefixed ``cmp_``).
    feature_label:
        Human-readable label for what this variant demonstrates
        (e.g. ``"MergeTree"``, ``"ZSTD(3)"``, ``"PARTITION BY toYYYYMM"``).
    ddl:
        ``CREATE TABLE`` statement for this variant.  Must be idempotent-friendly
        (``CREATE TABLE`` — the runner drops first).
    insert_sql:
        ``INSERT INTO <name> SELECT ... FROM <source>`` that copies the same
        data shape into this variant.  May be ``None`` when the runner needs
        to handle the load differently (e.g. materialized views, which fill
        from inserts on a source table).
    post_setup:
        Optional list of statements to run after the table is created — used
        e.g. for adding projections to a base table after creation.
    requires_optimize:
        If ``True``, the runner runs ``OPTIMIZE TABLE ... FINAL`` after loading
        so deduplication / aggregation merges complete before measurement
        (relevant for ReplacingMergeTree, SummingMergeTree, AggregatingMergeTree).
    """

    name: str
    feature_label: str
    ddl: str
    insert_sql: str | None = None
    post_setup: tuple[str, ...] = field(default_factory=tuple)
    requires_optimize: bool = False


@dataclass(frozen=True)
class FeatureComparison:
    """A group of :class:`SchemaVariant` objects compared along one dimension."""

    key: str
    title: str
    description: str
    base_table: str
    variants: tuple[SchemaVariant, ...]
    # ``test_queries`` is a tuple of (label, sql_template).  ``{table}`` in the
    # template is substituted with the variant name.
    test_queries: tuple[tuple[str, str], ...]
    insight: str = ""


# ---------------------------------------------------------------------------
# 1. Engines
# ---------------------------------------------------------------------------

ENGINES = FeatureComparison(
    key="engines",
    title="Engine comparison: MergeTree variants",
    description=(
        "Compares MergeTree, ReplacingMergeTree, SummingMergeTree and "
        "AggregatingMergeTree on the same orders dataset.  Highlights "
        "deduplication, automatic summation on merge, and storage trade-offs."
    ),
    base_table="orders",
    variants=(
        SchemaVariant(
            name="cmp_orders_mt",
            feature_label="MergeTree",
            ddl="""
                CREATE TABLE cmp_orders_mt (
                    id UInt64,
                    user_id UInt64,
                    product LowCardinality(String),
                    category LowCardinality(String),
                    quantity UInt32,
                    unit_price Float64,
                    total_price Float64,
                    status LowCardinality(String),
                    order_date Date
                ) ENGINE = MergeTree() ORDER BY (user_id, order_date)
            """,
            insert_sql="""
                INSERT INTO cmp_orders_mt
                SELECT id, user_id, product, category, quantity, unit_price,
                       total_price, status, order_date
                FROM orders
            """,
        ),
        SchemaVariant(
            name="cmp_orders_rmt",
            feature_label="ReplacingMergeTree",
            ddl="""
                CREATE TABLE cmp_orders_rmt (
                    id UInt64,
                    user_id UInt64,
                    product LowCardinality(String),
                    category LowCardinality(String),
                    quantity UInt32,
                    unit_price Float64,
                    total_price Float64,
                    status LowCardinality(String),
                    order_date Date,
                    version UInt32 DEFAULT 1
                ) ENGINE = ReplacingMergeTree(version) ORDER BY (id)
            """,
            insert_sql="""
                INSERT INTO cmp_orders_rmt
                (id, user_id, product, category, quantity, unit_price,
                 total_price, status, order_date)
                SELECT id, user_id, product, category, quantity, unit_price,
                       total_price, status, order_date
                FROM orders
            """,
            requires_optimize=True,
        ),
        SchemaVariant(
            name="cmp_orders_smt",
            feature_label="SummingMergeTree(total_price, quantity)",
            ddl="""
                CREATE TABLE cmp_orders_smt (
                    user_id UInt64,
                    category LowCardinality(String),
                    order_date Date,
                    quantity UInt32,
                    total_price Float64
                ) ENGINE = SummingMergeTree((total_price, quantity))
                ORDER BY (user_id, category, order_date)
            """,
            insert_sql="""
                INSERT INTO cmp_orders_smt
                SELECT user_id, category, order_date, quantity, total_price
                FROM orders
            """,
            requires_optimize=True,
        ),
        SchemaVariant(
            name="cmp_orders_amt",
            feature_label="AggregatingMergeTree",
            ddl="""
                CREATE TABLE cmp_orders_amt (
                    user_id UInt64,
                    category LowCardinality(String),
                    order_count AggregateFunction(count, UInt64),
                    revenue AggregateFunction(sum, Float64),
                    avg_order AggregateFunction(avg, Float64)
                ) ENGINE = AggregatingMergeTree() ORDER BY (user_id, category)
            """,
            insert_sql="""
                INSERT INTO cmp_orders_amt
                SELECT user_id,
                       category,
                       countState(toUInt64(id))     AS order_count,
                       sumState(total_price)        AS revenue,
                       avgState(total_price)        AS avg_order
                FROM orders
                GROUP BY user_id, category
            """,
            requires_optimize=True,
        ),
    ),
    test_queries=(
        ("count_all",        "SELECT count() FROM {table}"),
        ("user_lookup",      "SELECT count() FROM {table} WHERE user_id = 42"),
        ("category_revenue", "SELECT category, sum(total_price) FROM {table} GROUP BY category"),
    ),
    insight=(
        "ReplacingMergeTree and SummingMergeTree shrink storage by collapsing "
        "rows on merge.  AggregatingMergeTree pre-computes aggregates so "
        "category_revenue runs on already-aggregated state.  Trade-off: less "
        "flexible queries on the AMT variant."
    ),
)


# ---------------------------------------------------------------------------
# 2. Codecs (time-series oriented)
# ---------------------------------------------------------------------------

CODECS = FeatureComparison(
    key="codecs",
    title="Codec comparison: storage and query trade-offs",
    description=(
        "Same metrics data with different column codecs.  Time-series shapes "
        "(monotonic timestamps, smoothly-varying floats) compress dramatically "
        "better with delta + general-purpose codecs than with default LZ4 alone."
    ),
    base_table="metrics",
    variants=(
        SchemaVariant(
            name="cmp_metrics_lz4",
            feature_label="LZ4 (default)",
            ddl="""
                CREATE TABLE cmp_metrics_lz4 (
                    sensor_id   UInt32,
                    metric_name LowCardinality(String),
                    value       Float64,
                    tags        String,
                    timestamp   DateTime
                ) ENGINE = MergeTree() ORDER BY (sensor_id, timestamp)
            """,
            insert_sql="INSERT INTO cmp_metrics_lz4 SELECT * FROM metrics",
        ),
        SchemaVariant(
            name="cmp_metrics_zstd3",
            feature_label="ZSTD(3) on all columns",
            ddl="""
                CREATE TABLE cmp_metrics_zstd3 (
                    sensor_id   UInt32              CODEC(ZSTD(3)),
                    metric_name LowCardinality(String) CODEC(ZSTD(3)),
                    value       Float64             CODEC(ZSTD(3)),
                    tags        String              CODEC(ZSTD(3)),
                    timestamp   DateTime            CODEC(ZSTD(3))
                ) ENGINE = MergeTree() ORDER BY (sensor_id, timestamp)
            """,
            insert_sql="INSERT INTO cmp_metrics_zstd3 SELECT * FROM metrics",
        ),
        SchemaVariant(
            name="cmp_metrics_zstd9",
            feature_label="ZSTD(9) on all columns",
            ddl="""
                CREATE TABLE cmp_metrics_zstd9 (
                    sensor_id   UInt32              CODEC(ZSTD(9)),
                    metric_name LowCardinality(String) CODEC(ZSTD(9)),
                    value       Float64             CODEC(ZSTD(9)),
                    tags        String              CODEC(ZSTD(9)),
                    timestamp   DateTime            CODEC(ZSTD(9))
                ) ENGINE = MergeTree() ORDER BY (sensor_id, timestamp)
            """,
            insert_sql="INSERT INTO cmp_metrics_zstd9 SELECT * FROM metrics",
        ),
        SchemaVariant(
            name="cmp_metrics_dd_delta",
            feature_label="DoubleDelta(timestamp) + Delta(value) + LZ4",
            ddl="""
                CREATE TABLE cmp_metrics_dd_delta (
                    sensor_id   UInt32              CODEC(Delta(4), LZ4),
                    metric_name LowCardinality(String),
                    value       Float64             CODEC(Delta(8), LZ4),
                    tags        String,
                    timestamp   DateTime            CODEC(DoubleDelta, LZ4)
                ) ENGINE = MergeTree() ORDER BY (sensor_id, timestamp)
            """,
            insert_sql="INSERT INTO cmp_metrics_dd_delta SELECT * FROM metrics",
        ),
        SchemaVariant(
            name="cmp_metrics_gorilla",
            feature_label="Gorilla(value) + DoubleDelta(timestamp)",
            ddl="""
                CREATE TABLE cmp_metrics_gorilla (
                    sensor_id   UInt32,
                    metric_name LowCardinality(String),
                    value       Float64             CODEC(Gorilla, LZ4),
                    tags        String,
                    timestamp   DateTime            CODEC(DoubleDelta, LZ4)
                ) ENGINE = MergeTree() ORDER BY (sensor_id, timestamp)
            """,
            insert_sql="INSERT INTO cmp_metrics_gorilla SELECT * FROM metrics",
        ),
    ),
    test_queries=(
        ("range_scan",  "SELECT count(), avg(value) FROM {table} WHERE sensor_id = 1"),
        ("downsample",  "SELECT toStartOfMinute(timestamp), avg(value) FROM {table} GROUP BY 1 ORDER BY 1 LIMIT 100"),
    ),
    insight=(
        "DoubleDelta + Delta typically beat ZSTD-9 for storage on smooth time-series "
        "while keeping query CPU low.  ZSTD-9 wins for entropy-heavy strings.  "
        "Gorilla is excellent for floats but has been deprecated in newer versions."
    ),
)


# ---------------------------------------------------------------------------
# 3. ORDER BY (special emphasis: index keys)
# ---------------------------------------------------------------------------

ORDERING = FeatureComparison(
    key="ordering",
    title="ORDER BY key comparison (primary index)",
    description=(
        "The ORDER BY clause IS the primary key in MergeTree — it controls "
        "which queries scan minimal data.  Same events table, three orderings, "
        "three query shapes.  This comparison shows clearly that the right "
        "index key can mean 10-100x query speed differences."
    ),
    base_table="events",
    variants=(
        SchemaVariant(
            name="cmp_events_user_first",
            feature_label="ORDER BY (user_id, event_time)",
            ddl="""
                CREATE TABLE cmp_events_user_first (
                    id UInt64,
                    user_id UInt64,
                    event_type LowCardinality(String),
                    page String,
                    session_id String,
                    properties String,
                    event_time DateTime
                ) ENGINE = MergeTree() ORDER BY (user_id, event_time)
            """,
            insert_sql="INSERT INTO cmp_events_user_first SELECT id, user_id, event_type, page, session_id, properties, event_time FROM events",
        ),
        SchemaVariant(
            name="cmp_events_time_first",
            feature_label="ORDER BY (event_time, user_id)",
            ddl="""
                CREATE TABLE cmp_events_time_first (
                    id UInt64,
                    user_id UInt64,
                    event_type LowCardinality(String),
                    page String,
                    session_id String,
                    properties String,
                    event_time DateTime
                ) ENGINE = MergeTree() ORDER BY (event_time, user_id)
            """,
            insert_sql="INSERT INTO cmp_events_time_first SELECT id, user_id, event_type, page, session_id, properties, event_time FROM events",
        ),
        SchemaVariant(
            name="cmp_events_type_first",
            feature_label="ORDER BY (event_type, event_time, user_id)",
            ddl="""
                CREATE TABLE cmp_events_type_first (
                    id UInt64,
                    user_id UInt64,
                    event_type LowCardinality(String),
                    page String,
                    session_id String,
                    properties String,
                    event_time DateTime
                ) ENGINE = MergeTree() ORDER BY (event_type, event_time, user_id)
            """,
            insert_sql="INSERT INTO cmp_events_type_first SELECT id, user_id, event_type, page, session_id, properties, event_time FROM events",
        ),
    ),
    test_queries=(
        ("by_user",   "SELECT count() FROM {table} WHERE user_id = 42"),
        ("by_time",   "SELECT count() FROM {table} WHERE event_time >= now() - INTERVAL 7 DAY"),
        ("by_type",   "SELECT count() FROM {table} WHERE event_type = 'click'"),
        ("user_and_time", "SELECT count() FROM {table} WHERE user_id = 42 AND event_time >= now() - INTERVAL 30 DAY"),
    ),
    insight=(
        "user_first wins by_user; time_first wins by_time; type_first wins by_type. "
        "The first column of ORDER BY is the most selective — pick it based on your "
        "highest-frequency query shape.  Compound ORDER BY also affects column "
        "compression: data sorts more compressibly under one key than another."
    ),
)


# ---------------------------------------------------------------------------
# 4. LowCardinality vs String
# ---------------------------------------------------------------------------

LOWCARDINALITY = FeatureComparison(
    key="lowcardinality",
    title="LowCardinality vs String",
    description=(
        "LowCardinality wraps a column with a dictionary encoding.  For columns "
        "with under ~10K distinct values (countries, statuses, categories), it "
        "shrinks storage and accelerates GROUP BY/filter operations."
    ),
    base_table="users",
    variants=(
        SchemaVariant(
            name="cmp_users_string",
            feature_label="country: String",
            ddl="""
                CREATE TABLE cmp_users_string (
                    id UInt64,
                    username String,
                    email String,
                    full_name String,
                    country String,
                    city String,
                    age UInt8,
                    signup_date Date,
                    is_active UInt8
                ) ENGINE = MergeTree() ORDER BY (id)
            """,
            insert_sql="""
                INSERT INTO cmp_users_string
                SELECT id, username, email, full_name, toString(country),
                       city, age, signup_date, is_active
                FROM users
            """,
        ),
        SchemaVariant(
            name="cmp_users_lc",
            feature_label="country: LowCardinality(String)",
            ddl="""
                CREATE TABLE cmp_users_lc (
                    id UInt64,
                    username String,
                    email String,
                    full_name String,
                    country LowCardinality(String),
                    city String,
                    age UInt8,
                    signup_date Date,
                    is_active UInt8
                ) ENGINE = MergeTree() ORDER BY (id)
            """,
            insert_sql="""
                INSERT INTO cmp_users_lc
                SELECT id, username, email, full_name, country, city, age,
                       signup_date, is_active
                FROM users
            """,
        ),
    ),
    test_queries=(
        ("group_by",       "SELECT country, count() FROM {table} GROUP BY country ORDER BY 2 DESC"),
        ("filter_country", "SELECT count() FROM {table} WHERE country = 'US'"),
    ),
    insight=(
        "LowCardinality typically halves storage and doubles GROUP BY speed for "
        "low-cardinality columns.  Don't use it for high-cardinality columns "
        "(>100K distinct values) — the dictionary overhead dominates."
    ),
)


# ---------------------------------------------------------------------------
# 5. Projections
# ---------------------------------------------------------------------------

PROJECTIONS = FeatureComparison(
    key="projections",
    title="Projections (table-internal materialized aggregations)",
    description=(
        "A projection is an alternate sort/aggregation of a table that "
        "ClickHouse maintains automatically.  Queries that match the "
        "projection's shape are silently rewritten to use it."
    ),
    base_table="events",
    variants=(
        SchemaVariant(
            name="cmp_events_no_proj",
            feature_label="No projection",
            ddl="""
                CREATE TABLE cmp_events_no_proj (
                    id UInt64,
                    user_id UInt64,
                    event_type LowCardinality(String),
                    page String,
                    session_id String,
                    properties String,
                    event_time DateTime
                ) ENGINE = MergeTree() ORDER BY (user_id, event_time)
            """,
            insert_sql="INSERT INTO cmp_events_no_proj SELECT id, user_id, event_type, page, session_id, properties, event_time FROM events",
        ),
        SchemaVariant(
            name="cmp_events_with_proj",
            feature_label="With aggregating projection",
            ddl="""
                CREATE TABLE cmp_events_with_proj (
                    id UInt64,
                    user_id UInt64,
                    event_type LowCardinality(String),
                    page String,
                    session_id String,
                    properties String,
                    event_time DateTime,
                    PROJECTION daily_event_counts (
                        SELECT
                            toDate(event_time) AS day,
                            event_type,
                            count() AS cnt
                        GROUP BY day, event_type
                    )
                ) ENGINE = MergeTree() ORDER BY (user_id, event_time)
            """,
            insert_sql="INSERT INTO cmp_events_with_proj SELECT id, user_id, event_type, page, session_id, properties, event_time FROM events",
        ),
    ),
    test_queries=(
        ("daily_event_count", "SELECT toDate(event_time) AS day, event_type, count() FROM {table} GROUP BY day, event_type ORDER BY day"),
        ("type_distribution", "SELECT event_type, count() FROM {table} GROUP BY event_type ORDER BY 2 DESC"),
    ),
    insight=(
        "The projected variant should resolve the daily_event_count query "
        "directly from pre-aggregated parts — typically 10-100x faster than "
        "scanning the base table.  Storage cost: a small fraction of the "
        "base table.  Trade-off: writes are slower (every insert updates "
        "the projection)."
    ),
)


# ---------------------------------------------------------------------------
# 6. Materialized views
# ---------------------------------------------------------------------------

MATERIALIZED_VIEWS = FeatureComparison(
    key="materialized_views",
    title="Materialized views: streaming pre-aggregation",
    description=(
        "A materialized view is a trigger that runs on every insert into a "
        "source table and writes derived rows to a target table.  Unlike "
        "projections, MVs can join, filter, and reshape data freely."
    ),
    base_table="events",
    variants=(
        SchemaVariant(
            name="cmp_events_mv_source",
            feature_label="Raw events (source for MV)",
            ddl="""
                CREATE TABLE cmp_events_mv_source (
                    id UInt64,
                    user_id UInt64,
                    event_type LowCardinality(String),
                    event_time DateTime
                ) ENGINE = MergeTree() ORDER BY (user_id, event_time)
            """,
            insert_sql="INSERT INTO cmp_events_mv_source SELECT id, user_id, event_type, event_time FROM events",
        ),
        SchemaVariant(
            name="cmp_events_mv_target",
            feature_label="MV target: daily event counts",
            ddl="""
                CREATE TABLE cmp_events_mv_target (
                    day Date,
                    event_type LowCardinality(String),
                    cnt AggregateFunction(count, UInt64)
                ) ENGINE = AggregatingMergeTree() ORDER BY (day, event_type)
            """,
            insert_sql=None,  # filled by MV trigger
            post_setup=(
                """
                CREATE MATERIALIZED VIEW cmp_events_mv_view
                TO cmp_events_mv_target
                AS SELECT
                    toDate(event_time) AS day,
                    event_type,
                    countState(toUInt64(id)) AS cnt
                FROM cmp_events_mv_source
                GROUP BY day, event_type
                """,
            ),
            requires_optimize=True,
        ),
    ),
    test_queries=(
        # Query the source for raw count
        ("raw_daily_count", "SELECT toDate(event_time), event_type, count() FROM cmp_events_mv_source GROUP BY 1, 2 ORDER BY 1"),
        # Query the MV target for pre-aggregated count
        ("mv_daily_count",  "SELECT day, event_type, countMerge(cnt) FROM cmp_events_mv_target GROUP BY day, event_type ORDER BY day"),
    ),
    insight=(
        "Querying the MV target should be near-instant regardless of base "
        "table size, while the raw query scales with the base table.  Cost: "
        "every insert into the source table runs the MV's GROUP BY."
    ),
)


# ---------------------------------------------------------------------------
# 7. Skip indexes (special emphasis: index keys)
# ---------------------------------------------------------------------------

SKIP_INDEXES = FeatureComparison(
    key="skip_indexes",
    title="Skip (data-skipping) indexes on non-PK columns",
    description=(
        "Skip indexes let ClickHouse rule out granules without reading them.  "
        "Bloom filters excel at high-cardinality equality lookups (UUIDs); "
        "set indexes work for low-cardinality enum-like columns; minmax indexes "
        "help on monotonic numeric/date columns that aren't in the PK."
    ),
    base_table="events",
    variants=(
        SchemaVariant(
            name="cmp_events_no_idx",
            feature_label="No skip index",
            ddl="""
                CREATE TABLE cmp_events_no_idx (
                    id UInt64,
                    user_id UInt64,
                    event_type LowCardinality(String),
                    page String,
                    session_id String,
                    properties String,
                    event_time DateTime
                ) ENGINE = MergeTree() ORDER BY (user_id, event_time)
            """,
            insert_sql="INSERT INTO cmp_events_no_idx SELECT id, user_id, event_type, page, session_id, properties, event_time FROM events",
        ),
        SchemaVariant(
            name="cmp_events_bloom",
            feature_label="bloom_filter on session_id",
            ddl="""
                CREATE TABLE cmp_events_bloom (
                    id UInt64,
                    user_id UInt64,
                    event_type LowCardinality(String),
                    page String,
                    session_id String,
                    properties String,
                    event_time DateTime,
                    INDEX idx_session session_id TYPE bloom_filter(0.01) GRANULARITY 4
                ) ENGINE = MergeTree() ORDER BY (user_id, event_time)
            """,
            insert_sql="INSERT INTO cmp_events_bloom SELECT id, user_id, event_type, page, session_id, properties, event_time FROM events",
        ),
        SchemaVariant(
            name="cmp_events_set_idx",
            feature_label="set(100) on event_type",
            ddl="""
                CREATE TABLE cmp_events_set_idx (
                    id UInt64,
                    user_id UInt64,
                    event_type LowCardinality(String),
                    page String,
                    session_id String,
                    properties String,
                    event_time DateTime,
                    INDEX idx_etype event_type TYPE set(100) GRANULARITY 4
                ) ENGINE = MergeTree() ORDER BY (user_id, event_time)
            """,
            insert_sql="INSERT INTO cmp_events_set_idx SELECT id, user_id, event_type, page, session_id, properties, event_time FROM events",
        ),
        SchemaVariant(
            name="cmp_events_minmax_idx",
            feature_label="minmax on event_time",
            ddl="""
                CREATE TABLE cmp_events_minmax_idx (
                    id UInt64,
                    user_id UInt64,
                    event_type LowCardinality(String),
                    page String,
                    session_id String,
                    properties String,
                    event_time DateTime,
                    INDEX idx_time event_time TYPE minmax GRANULARITY 4
                ) ENGINE = MergeTree() ORDER BY (user_id, event_time)
            """,
            insert_sql="INSERT INTO cmp_events_minmax_idx SELECT id, user_id, event_type, page, session_id, properties, event_time FROM events",
        ),
    ),
    test_queries=(
        ("session_lookup", "SELECT count() FROM {table} WHERE session_id = (SELECT session_id FROM {table} LIMIT 1)"),
        ("type_filter",    "SELECT count() FROM {table} WHERE event_type = 'click'"),
        ("time_window",    "SELECT count() FROM {table} WHERE event_time BETWEEN now() - INTERVAL 30 DAY AND now() - INTERVAL 23 DAY"),
    ),
    insight=(
        "Bloom dramatically helps high-cardinality equality (session_id). "
        "Set helps when filter values fit in the set size. "
        "Minmax shines for time-window queries on tables not ordered by time."
    ),
)


# ---------------------------------------------------------------------------
# 8. PARTITION BY (special emphasis)
# ---------------------------------------------------------------------------

PARTITIONING = FeatureComparison(
    key="partitioning",
    title="PARTITION BY strategies",
    description=(
        "Partitioning splits a table into independent storage units that can "
        "be dropped, attached, and pruned at query time.  Choice of partition "
        "key dramatically affects parts count, drop-by-partition speed, "
        "and query pruning efficiency."
    ),
    base_table="events",
    variants=(
        SchemaVariant(
            name="cmp_events_no_part",
            feature_label="No PARTITION BY",
            ddl="""
                CREATE TABLE cmp_events_no_part (
                    id UInt64,
                    user_id UInt64,
                    event_type LowCardinality(String),
                    page String,
                    session_id String,
                    properties String,
                    event_time DateTime
                ) ENGINE = MergeTree() ORDER BY (user_id, event_time)
            """,
            insert_sql="INSERT INTO cmp_events_no_part SELECT id, user_id, event_type, page, session_id, properties, event_time FROM events",
        ),
        SchemaVariant(
            name="cmp_events_part_month",
            feature_label="PARTITION BY toYYYYMM(event_time)",
            ddl="""
                CREATE TABLE cmp_events_part_month (
                    id UInt64,
                    user_id UInt64,
                    event_type LowCardinality(String),
                    page String,
                    session_id String,
                    properties String,
                    event_time DateTime
                ) ENGINE = MergeTree()
                PARTITION BY toYYYYMM(event_time)
                ORDER BY (user_id, event_time)
            """,
            insert_sql="INSERT INTO cmp_events_part_month SELECT id, user_id, event_type, page, session_id, properties, event_time FROM events",
        ),
        SchemaVariant(
            name="cmp_events_part_day",
            feature_label="PARTITION BY toDate(event_time)",
            ddl="""
                CREATE TABLE cmp_events_part_day (
                    id UInt64,
                    user_id UInt64,
                    event_type LowCardinality(String),
                    page String,
                    session_id String,
                    properties String,
                    event_time DateTime
                ) ENGINE = MergeTree()
                PARTITION BY toDate(event_time)
                ORDER BY (user_id, event_time)
            """,
            insert_sql="INSERT INTO cmp_events_part_day SELECT id, user_id, event_type, page, session_id, properties, event_time FROM events",
        ),
        SchemaVariant(
            name="cmp_events_part_user_bucket",
            feature_label="PARTITION BY user_id % 16",
            ddl="""
                CREATE TABLE cmp_events_part_user_bucket (
                    id UInt64,
                    user_id UInt64,
                    event_type LowCardinality(String),
                    page String,
                    session_id String,
                    properties String,
                    event_time DateTime
                ) ENGINE = MergeTree()
                PARTITION BY (user_id % 16)
                ORDER BY (user_id, event_time)
            """,
            insert_sql="INSERT INTO cmp_events_part_user_bucket SELECT id, user_id, event_type, page, session_id, properties, event_time FROM events",
        ),
    ),
    test_queries=(
        ("recent_window",  "SELECT count() FROM {table} WHERE event_time >= now() - INTERVAL 30 DAY"),
        ("specific_day",   "SELECT count() FROM {table} WHERE event_time >= now() - INTERVAL 7 DAY AND event_time < now() - INTERVAL 6 DAY"),
        ("user_lookup",    "SELECT count() FROM {table} WHERE user_id = 42"),
        ("parts_count",    "SELECT count() FROM system.parts WHERE table = '{table}' AND active"),
    ),
    insight=(
        "Day-partitioning gives surgical pruning for time-window queries but "
        "produces many parts (slower writes if not batched).  Month-partitioning "
        "is the most common balance.  user_id % 16 favors per-user queries but "
        "kills time-range pruning.  No-partition keeps merges cheap but loses "
        "drop-old-data convenience."
    ),
)


# ---------------------------------------------------------------------------
# 9. index_granularity tuning (Phase D)
# ---------------------------------------------------------------------------

def _events_ddl_with_granularity(name: str, granularity: int) -> str:
    return f"""
        CREATE TABLE {name} (
            id UInt64,
            user_id UInt64,
            event_type LowCardinality(String),
            page String,
            session_id String,
            properties String,
            event_time DateTime
        ) ENGINE = MergeTree()
        ORDER BY (user_id, event_time)
        SETTINGS index_granularity = {granularity}
    """


INDEX_GRANULARITY = FeatureComparison(
    key="index_granularity",
    title="index_granularity tuning (sparse primary index)",
    description=(
        "The default index_granularity = 8192 sets one mark per 8192 rows.  "
        "Smaller granularity → more marks → finer skipping but a larger in-RAM "
        "primary index.  Larger granularity → coarser skipping, smaller index. "
        "This comparison sweeps 1024 through 32768 to find the inflection."
    ),
    base_table="events",
    variants=tuple(
        SchemaVariant(
            name=f"cmp_events_g{g}",
            feature_label=f"index_granularity = {g}",
            ddl=_events_ddl_with_granularity(f"cmp_events_g{g}", g),
            insert_sql=(
                f"INSERT INTO cmp_events_g{g} "
                "SELECT id, user_id, event_type, page, session_id, "
                "properties, event_time FROM events"
            ),
        )
        for g in (1024, 4096, 8192, 16384, 32768)
    ),
    test_queries=(
        ("by_user_lookup", "SELECT count() FROM {table} WHERE user_id = 42"),
        ("by_user_range",
         "SELECT count() FROM {table} WHERE user_id BETWEEN 1000 AND 1100"),
        ("by_user_and_time",
         "SELECT count() FROM {table} "
         "WHERE user_id = 42 AND event_time >= now() - INTERVAL 30 DAY"),
    ),
    insight=(
        "Smaller granularity wins on point lookups (more skipping) at the "
        "cost of a larger in-memory primary index. Larger granularity is "
        "lighter but reads more rows per match. The default 8192 is a "
        "well-balanced compromise; only deviate when EXPLAIN ESTIMATE shows "
        "the granule scan dominating and the index size is comfortable."
    ),
)


# ---------------------------------------------------------------------------
# 10. bloom filter false-positive rate tuning (Phase D)
# ---------------------------------------------------------------------------

def _events_ddl_with_bloom_fpr(name: str, fpr: float) -> str:
    return f"""
        CREATE TABLE {name} (
            id UInt64,
            user_id UInt64,
            event_type LowCardinality(String),
            page String,
            session_id String,
            properties String,
            event_time DateTime,
            INDEX idx_session session_id TYPE bloom_filter({fpr}) GRANULARITY 4
        ) ENGINE = MergeTree() ORDER BY (user_id, event_time)
    """


BLOOM_FILTER_FPR = FeatureComparison(
    key="bloom_filter_fpr",
    title="bloom_filter false-positive rate tuning",
    description=(
        "The bloom_filter(p) parameter is the desired false-positive rate. "
        "Lower p → larger filter → fewer wasted granule reads, but more "
        "index storage. This comparison sweeps 0.001 / 0.01 / 0.05 on "
        "session_id (high-cardinality) lookups."
    ),
    base_table="events",
    variants=tuple(
        SchemaVariant(
            name=f"cmp_events_bf{label}",
            feature_label=f"bloom_filter({fpr})",
            ddl=_events_ddl_with_bloom_fpr(f"cmp_events_bf{label}", fpr),
            insert_sql=(
                f"INSERT INTO cmp_events_bf{label} "
                "SELECT id, user_id, event_type, page, session_id, "
                "properties, event_time FROM events"
            ),
        )
        for label, fpr in (("001", 0.001), ("01", 0.01), ("05", 0.05))
    ),
    test_queries=(
        ("session_known",
         "SELECT count() FROM {table} "
         "WHERE session_id = (SELECT session_id FROM {table} LIMIT 1)"),
        ("session_unknown",
         "SELECT count() FROM {table} "
         "WHERE session_id = '00000000-0000-0000-0000-000000000000'"),
    ),
    insight=(
        "Lower FPR cuts wasted granule reads on negative lookups but adds "
        "index bytes. The 'unknown' query shows the maximum win for tighter "
        "FPR — the filter rejects most granules outright. The 'known' query "
        "still requires reading the matching granule regardless of FPR."
    ),
)


# ---------------------------------------------------------------------------
# 11. skip-index GRANULARITY tuning (Phase D)
# ---------------------------------------------------------------------------

def _events_ddl_with_skip_granularity(name: str, granularity: int) -> str:
    return f"""
        CREATE TABLE {name} (
            id UInt64,
            user_id UInt64,
            event_type LowCardinality(String),
            page String,
            session_id String,
            properties String,
            event_time DateTime,
            INDEX idx_session session_id TYPE bloom_filter(0.01) GRANULARITY {granularity}
        ) ENGINE = MergeTree() ORDER BY (user_id, event_time)
    """


SKIP_INDEX_GRANULARITY = FeatureComparison(
    key="skip_index_granularity",
    title="Skip-index GRANULARITY tuning",
    description=(
        "Skip indexes' GRANULARITY parameter sets how many primary-index "
        "granules each skip-index entry covers.  GRANULARITY 1 = one entry "
        "per primary granule (finest skipping, largest index); GRANULARITY 16 "
        "= one entry per 16 primary granules (coarsest)."
    ),
    base_table="events",
    variants=tuple(
        SchemaVariant(
            name=f"cmp_events_sg{g}",
            feature_label=f"GRANULARITY {g}",
            ddl=_events_ddl_with_skip_granularity(f"cmp_events_sg{g}", g),
            insert_sql=(
                f"INSERT INTO cmp_events_sg{g} "
                "SELECT id, user_id, event_type, page, session_id, "
                "properties, event_time FROM events"
            ),
        )
        for g in (1, 4, 16)
    ),
    test_queries=(
        ("session_known",
         "SELECT count() FROM {table} "
         "WHERE session_id = (SELECT session_id FROM {table} LIMIT 1)"),
        ("session_unknown",
         "SELECT count() FROM {table} "
         "WHERE session_id = '00000000-0000-0000-0000-000000000000'"),
    ),
    insight=(
        "Finer GRANULARITY skips more granules at the cost of a larger "
        "skip-index. The 'unknown' query is where finer wins; on 'known' "
        "lookups the matching granule still has to be read either way."
    ),
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

ALL_COMPARISONS: tuple[FeatureComparison, ...] = (
    ENGINES,
    CODECS,
    ORDERING,
    LOWCARDINALITY,
    PROJECTIONS,
    MATERIALIZED_VIEWS,
    SKIP_INDEXES,
    PARTITIONING,
    INDEX_GRANULARITY,
    BLOOM_FILTER_FPR,
    SKIP_INDEX_GRANULARITY,
)


def get_comparison(key: str) -> FeatureComparison:
    """Look up a :class:`FeatureComparison` by its short key."""
    for cmp in ALL_COMPARISONS:
        if cmp.key == key:
            return cmp
    keys = ", ".join(c.key for c in ALL_COMPARISONS)
    raise KeyError(f"Unknown comparison key: {key!r}.  Choose from: {keys}")


def comparison_keys() -> list[str]:
    """Return all known comparison keys."""
    return [c.key for c in ALL_COMPARISONS]


def all_variant_names() -> Iterable[str]:
    """Yield every cmp_* table name across every comparison.

    Useful for cleanup.
    """
    for cmp in ALL_COMPARISONS:
        for v in cmp.variants:
            yield v.name
