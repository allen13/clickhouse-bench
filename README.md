# ClickHouse Cloud Benchmark Toolkit

A UV-managed Python project for setting up, evaluating, and benchmarking ClickHouse Cloud. Generates realistic test data, runs a comprehensive suite of benchmark queries across multiple categories, and produces detailed performance reports with visualisations.

## Quick Start

### 1. Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) package manager
- A ClickHouse Cloud instance (or local ClickHouse server)

### 2. Get a ClickHouse Cloud Trial

1. Visit [clickhouse.cloud](https://clickhouse.cloud) and sign up for a free trial
2. Create a new service — pick your preferred cloud provider and region
3. Note the **host**, **port** (usually `8443`), **user** (`default`), and **password**

### 3. Install

```bash
cd ~/projects/clickhouse-bench
cp .env.example .env
# Edit .env with your ClickHouse Cloud credentials
uv sync
```

### 4. Run

```bash
# Run the full pipeline (setup → seed → benchmark → evaluate)
uv run clickhouse-bench full --scale 10000

# Or run individual steps
uv run clickhouse-bench setup            # Create tables
uv run clickhouse-bench seed --scale 1000 # Generate test data
uv run clickhouse-bench benchmark         # Run benchmarks
uv run clickhouse-bench evaluate          # Analyse results & generate charts
uv run clickhouse-bench compare           # Compare last two runs

# Feature comparisons
uv run clickhouse-bench list-comparisons              # See available comparisons
uv run clickhouse-bench compare-features              # Run all 8 comparisons
uv run clickhouse-bench compare-features --comparison partitioning --comparison ordering
uv run clickhouse-bench cleanup-variants              # Drop all cmp_* tables
```

## CLI Reference

```
clickhouse-bench [OPTIONS] COMMAND [ARGS]

Global options:
  --host        ClickHouse host (or CLICKHOUSE_HOST env var)
  --port        ClickHouse port (or CLICKHOUSE_PORT env var)
  --user        Username       (or CLICKHOUSE_USER env var)
  --password    Password       (or CLICKHOUSE_PASSWORD env var)
  --database    Database name  (or CLICKHOUSE_DATABASE env var)

Commands:
  setup              Create benchmark tables (--drop to recreate)
  seed               Generate and insert test data (--scale N)
  benchmark          Run query benchmarks (--warm-runs N, --category <cat>)
  evaluate           Analyse results and generate charts
  compare            Compare the two most recent benchmark runs
  compare-features   Build schema variants and benchmark feature impact
  list-comparisons   Show available feature comparisons
  cleanup-variants   Drop all cmp_* tables created by compare-features
  full               Run the entire pipeline end-to-end
```

## Feature comparisons

`compare-features` is a schema-evaluation harness. For each comparison it
creates several physical variants of the same data, copies the seeded source
into each, then measures storage, compression, and query latency side-by-side
in rich tables. Results are saved to `results/compare_features_*.json`.

Available comparisons (26 variants total, 22 query templates):

| Key | Variants | What it shows |
|---|---|---|
| `engines` | MergeTree, Replacing, Summing, Aggregating | Dedup, auto-summation, pre-aggregation trade-offs |
| `codecs` | LZ4, ZSTD(3), ZSTD(9), DoubleDelta+Delta+LZ4, Gorilla | Storage size and decompression CPU on time-series |
| `ordering` | user-first, time-first, type-first ORDER BY | How the primary key dictates which queries scan minimal data |
| `lowcardinality` | String vs LowCardinality(String) | Storage and GROUP BY speed for low-cardinality columns |
| `projections` | with vs without aggregating projection | 10–100x acceleration for queries matching the projection shape |
| `materialized_views` | raw vs MV-aggregated daily counts | Pre-aggregation via insert-time triggers |
| `skip_indexes` | none, bloom_filter, set, minmax | Granule-skipping for non-PK column lookups |
| `partitioning` | none, monthly, daily, user_id % 16 | Partition pruning, parts count, drop-old-data ergonomics |

The `partitioning` and index-key comparisons (`ordering`, `skip_indexes`) get
extra emphasis — they're the schema decisions with the biggest practical
impact on read performance.

Workflow:

```bash
# 1. Seed the base tables (these are the data source for the comparisons)
uv run clickhouse-bench seed --scale 100000

# 2. Run all comparisons
uv run clickhouse-bench compare-features

# 3. Or pick a few
uv run clickhouse-bench compare-features --comparison partitioning --comparison ordering

# 4. Clean up the cmp_* tables when done
uv run clickhouse-bench cleanup-variants
```

Each comparison prints two tables — a storage / load summary and a
query-latency grid where the fastest cell per row is highlighted — followed
by a one-paragraph insight. Errors on individual variants don't abort the
run; they're surfaced in the status column and the rest continue.


## Benchmark Categories

| Category | Description |
|---|---|
| **Point queries** | Single-row lookups by primary key |
| **Range scans** | Date and numeric range filters |
| **Aggregations** | GROUP BY with COUNT, SUM, AVG, uniqExact |
| **Joins** | Two- and three-table joins |
| **Window functions** | Running totals, rankings, moving averages |
| **Time-series** | Downsampling, gap detection, rate-of-change |
| **Insert performance** | Batch insert throughput at varying sizes |
| **Compression** | Table and column-level compression ratios |

## Data Model

The toolkit creates four tables:

- **users** — user profiles with demographics (country, age, signup date)
- **orders** — e-commerce orders linked to users (product, category, price)
- **events** — clickstream events (page views, clicks, form submissions)
- **metrics** — time-series sensor data (CPU, memory, latency, etc.)

Scale is controlled by the `--scale` flag (base user count). Other tables scale proportionally: orders = 3×, events = 10×, metrics = 5×.

## Output

Results are saved to the `results/` directory:

- `benchmark_YYYYMMDD_HHMMSS.json` — raw benchmark data
- `latency_cold_vs_warm.png` — cold vs warm query latency chart
- `category_latency.png` — latency distribution by category
- `insert_throughput.png` — insert performance chart
- `memory_usage.png` — client-side memory usage
- `evaluation_summary.txt` — text summary with recommendations

## Project Structure

```
clickhouse-bench/
├── pyproject.toml
├── .env.example
├── README.md
├── results/              # Generated output
└── src/
    ├── __init__.py
    ├── config.py         # Connection configuration
    ├── setup_db.py       # Schema creation
    ├── seed_data.py      # Test data generation
    ├── queries.py        # Benchmark query library
    ├── benchmark.py      # Benchmark runner
    ├── evaluate.py       # Analysis and charting
    └── main.py           # CLI entrypoint
```
