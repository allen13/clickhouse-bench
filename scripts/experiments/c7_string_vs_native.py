"""C7 — String-for-everything vs native types.

Two equivalent 100K-row tables seeded from the existing ``users`` table:

- ``exp_users_string``: every column declared as String, including ids
  and dates.
- ``exp_users_native``: native types (UInt64, Date, UInt8, Bool).

Compare per-column compressed size and a simple range-scan latency.

Reference: docs/data-types.md, schema-types-native-types rule.
"""
from __future__ import annotations

from scripts.experiments._lib import (
    get_client, run_query, server_summary, temp_tables, write_result,
)


STRING_TABLE = "exp_users_string"
NATIVE_TABLE = "exp_users_native"


def main() -> None:
    client = get_client()
    server = server_summary(client)

    payload: dict = {
        "title": "String-for-everything vs native types",
        "server": server,
    }

    with temp_tables(client, STRING_TABLE, NATIVE_TABLE):
        # ── String version
        client.command(f"""
            CREATE TABLE {STRING_TABLE} (
                id String,
                username String,
                email String,
                full_name String,
                country String,
                city String,
                age String,
                signup_date String,
                is_active String
            ) ENGINE = MergeTree() ORDER BY id
        """)
        client.command(f"""
            INSERT INTO {STRING_TABLE}
            SELECT
                toString(id), username, email, full_name, country, city,
                toString(age),
                toString(signup_date),
                toString(is_active)
            FROM users
        """)

        # ── Native version
        client.command(f"""
            CREATE TABLE {NATIVE_TABLE} (
                id UInt64,
                username String,
                email String,
                full_name String,
                country LowCardinality(String),
                city String,
                age UInt8,
                signup_date Date,
                is_active UInt8
            ) ENGINE = MergeTree() ORDER BY id
        """)
        client.command(f"""
            INSERT INTO {NATIVE_TABLE}
            SELECT
                id, username, email, full_name, country, city,
                age, signup_date, is_active
            FROM users
        """)

        # Storage
        def _table_storage(name: str) -> dict:
            row = client.query(f"""
                SELECT
                    sum(rows),
                    formatReadableSize(sum(data_compressed_bytes)),
                    formatReadableSize(sum(data_uncompressed_bytes)),
                    sum(data_compressed_bytes),
                    round(sum(data_uncompressed_bytes)
                          / sum(data_compressed_bytes), 2)
                FROM system.parts
                WHERE active AND table = '{name}'
            """).result_rows[0]
            return {
                "rows": row[0],
                "compressed": row[1],
                "uncompressed": row[2],
                "compressed_bytes": row[3],
                "ratio": float(row[4] or 0),
            }

        s_storage = _table_storage(STRING_TABLE)
        n_storage = _table_storage(NATIVE_TABLE)
        payload["string"] = {"storage": s_storage}
        payload["native"] = {"storage": n_storage}
        payload["storage_savings_x"] = round(
            s_storage["compressed_bytes"]
            / max(n_storage["compressed_bytes"], 1), 2)

        # Per-column comparison (most interesting columns)
        per_col = client.query(f"""
            SELECT table, name, type,
                   formatReadableSize(data_compressed_bytes) AS comp,
                   formatReadableSize(data_uncompressed_bytes) AS uncomp,
                   data_compressed_bytes
            FROM system.columns
            WHERE table IN ('{STRING_TABLE}', '{NATIVE_TABLE}')
            ORDER BY table, name
        """).result_rows
        payload["per_column"] = [
            {"table": r[0], "name": r[1], "type": r[2],
             "compressed": r[3], "uncompressed": r[4],
             "compressed_bytes": r[5]} for r in per_col]

        # Range-scan latency
        s_runs = []
        n_runs = []
        for i in range(5):
            s_runs.append(run_query(
                client,
                f"SELECT count() FROM {STRING_TABLE} WHERE country = 'US'",
            ))
            n_runs.append(run_query(
                client,
                f"SELECT count() FROM {NATIVE_TABLE} WHERE country = 'US'",
            ))
        payload["string"]["filter_country_warm_avg_ms"] = round(
            sum(r["wall_ms"] for r in s_runs[1:]) / 4, 2)
        payload["native"]["filter_country_warm_avg_ms"] = round(
            sum(r["wall_ms"] for r in n_runs[1:]) / 4, 2)

    write_result("c7_string_vs_native", payload)


if __name__ == "__main__":
    main()
