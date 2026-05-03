"""C1 — Dictionary vs JOIN.

Question: does ``dictGet`` outperform a regular JOIN on our user lookup?

Setup
-----
- Existing ``users`` table is the dimension (~100K rows).
- Existing ``orders`` table is the fact (~300K rows).
- Build a HASHED dictionary on top of users.
- Run two equivalent queries — JOIN vs dictGet — and compare warm latency,
  read rows, peak memory, and peak threads.

Reference: docs/joins-and-dictionaries.md, query-join-consider-alternatives rule.
"""
from __future__ import annotations

from scripts.experiments._lib import (
    get_client, log_metrics, new_query_id, run_command, run_query,
    server_summary, temp_tables, warm_run, write_result,
)


DICT_NAME = "exp_users_dict"


JOIN_SQL = """
    SELECT u.country, count() AS orders, sum(o.total_price) AS revenue
    FROM orders o
    INNER JOIN users u ON u.id = o.user_id
    GROUP BY u.country
    ORDER BY revenue DESC
"""

DICTGET_SQL = """
    SELECT
        dictGet('{dict}', 'country', user_id) AS country,
        count() AS orders,
        sum(total_price) AS revenue
    FROM orders
    GROUP BY country
    ORDER BY revenue DESC
""".format(dict=DICT_NAME)


def main() -> None:
    client = get_client()
    server = server_summary(client)

    payload: dict = {
        "title": "Dictionary vs JOIN",
        "server": server,
        "settings": {"warm_runs": 5, "dict_layout": "HASHED",
                     "dict_lifetime_min_max": [300, 360]},
    }

    # Drop any prior dictionary so we start clean. Dictionaries are not 'tables'
    # per se — they live in their own DDL space.
    client.command(f"DROP DICTIONARY IF EXISTS {DICT_NAME}")
    try:
        client.command(f"""
            CREATE DICTIONARY {DICT_NAME} (
                id      UInt64,
                country String,
                city    String,
                age     UInt8,
                is_active UInt8
            )
            PRIMARY KEY id
            SOURCE(CLICKHOUSE(TABLE 'users'))
            LAYOUT(HASHED())
            LIFETIME(MIN 300 MAX 360)
        """)
        # Force the dict to load now so the first dictGet isn't measuring load time.
        client.command(f"SYSTEM RELOAD DICTIONARY {DICT_NAME}")

        join_runs = warm_run(client, JOIN_SQL, n=6, prefix="c1-join")
        dict_runs = warm_run(client, DICTGET_SQL, n=6, prefix="c1-dict")

        payload["join"] = {
            "sql": JOIN_SQL.strip(),
            "runs": join_runs,
            "warm_avg_ms": _warm_avg(join_runs),
        }
        payload["dictGet"] = {
            "sql": DICTGET_SQL.strip(),
            "runs": dict_runs,
            "warm_avg_ms": _warm_avg(dict_runs),
        }
        # Speedup
        payload["dict_speedup_x"] = round(
            payload["join"]["warm_avg_ms"] / payload["dictGet"]["warm_avg_ms"], 2
        )

    finally:
        client.command(f"DROP DICTIONARY IF EXISTS {DICT_NAME}")

    write_result("c1_dictionary_vs_join", payload)


def _warm_avg(runs: list[dict]) -> float:
    """Average wall ms of runs[1:] (skip cold)."""
    warm = runs[1:]
    return round(sum(r["wall_ms"] for r in warm) / len(warm), 2)


if __name__ == "__main__":
    main()
