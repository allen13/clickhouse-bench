"""C8 — Nullable(T) overhead vs DEFAULT.

Two equivalent 100K-row tables:
- ``exp_users_nullable``: every column wrapped in Nullable(T)
- ``exp_users_default``: DEFAULT values, no Nullable

Measure compressed/uncompressed storage and a simple aggregation.

Reference: docs/data-types.md, schema-types-avoid-nullable rule.
"""
from __future__ import annotations

from scripts.experiments._lib import (
    get_client, run_query, server_summary, temp_tables, write_result,
)


NULLABLE_TABLE = "exp_users_nullable"
DEFAULT_TABLE = "exp_users_default"


def main() -> None:
    client = get_client()
    server = server_summary(client)

    payload: dict = {
        "title": "Nullable(T) overhead vs DEFAULT",
        "server": server,
    }

    with temp_tables(client, NULLABLE_TABLE, DEFAULT_TABLE):
        # Nullable everything
        client.command(f"""
            CREATE TABLE {NULLABLE_TABLE} (
                id Nullable(UInt64),
                username Nullable(String),
                email Nullable(String),
                full_name Nullable(String),
                country Nullable(String),
                city Nullable(String),
                age Nullable(UInt8),
                signup_date Nullable(Date),
                is_active Nullable(UInt8),
                created_at Nullable(DateTime)
            ) ENGINE = MergeTree() ORDER BY tuple()
        """)
        client.command(f"INSERT INTO {NULLABLE_TABLE} SELECT * FROM users")

        # DEFAULT-driven — match the source `users` schema exactly so the
        # only difference is Nullable() wrapping and ORDER BY. country stays
        # LowCardinality(String) to avoid confounding the storage comparison.
        client.command(f"""
            CREATE TABLE {DEFAULT_TABLE} (
                id UInt64,
                username String DEFAULT '',
                email String DEFAULT '',
                full_name String DEFAULT '',
                country LowCardinality(String) DEFAULT '',
                city String DEFAULT '',
                age UInt8 DEFAULT 0,
                signup_date Date DEFAULT toDate('1970-01-01'),
                is_active UInt8 DEFAULT 0,
                created_at DateTime DEFAULT now()
            ) ENGINE = MergeTree() ORDER BY tuple()
        """)
        client.command(f"INSERT INTO {DEFAULT_TABLE} SELECT * FROM users")

        def _storage(name: str) -> dict:
            row = client.query(f"""
                SELECT
                    sum(rows),
                    formatReadableSize(sum(data_compressed_bytes)),
                    formatReadableSize(sum(data_uncompressed_bytes)),
                    sum(data_compressed_bytes)
                FROM system.parts
                WHERE active AND table = '{name}'
            """).result_rows[0]
            return {
                "rows": row[0],
                "compressed": row[1],
                "uncompressed": row[2],
                "compressed_bytes": row[3],
            }

        nul = _storage(NULLABLE_TABLE)
        dft = _storage(DEFAULT_TABLE)
        payload["nullable"] = {"storage": nul}
        payload["default"] = {"storage": dft}
        payload["overhead_ratio"] = round(
            nul["compressed_bytes"]
            / max(dft["compressed_bytes"], 1), 2)

        # Per-column null-map overhead
        cols = client.query(f"""
            SELECT table, name, type,
                   formatReadableSize(data_compressed_bytes) AS comp,
                   data_compressed_bytes
            FROM system.columns
            WHERE table IN ('{NULLABLE_TABLE}', '{DEFAULT_TABLE}')
            ORDER BY table, name
        """).result_rows
        payload["per_column"] = [
            {"table": r[0], "name": r[1], "type": r[2],
             "compressed": r[3], "compressed_bytes": r[4]} for r in cols]

        # Aggregation latency: count distinct countries
        nul_runs = [run_query(client,
                              f"SELECT uniq(country) FROM {NULLABLE_TABLE}")
                    for _ in range(5)]
        dft_runs = [run_query(client,
                              f"SELECT uniq(country) FROM {DEFAULT_TABLE}")
                    for _ in range(5)]
        payload["nullable"]["uniq_country_warm_avg_ms"] = round(
            sum(r["wall_ms"] for r in nul_runs[1:]) / 4, 2)
        payload["default"]["uniq_country_warm_avg_ms"] = round(
            sum(r["wall_ms"] for r in dft_runs[1:]) / 4, 2)

    write_result("c8_nullable_overhead", payload)


if __name__ == "__main__":
    main()
