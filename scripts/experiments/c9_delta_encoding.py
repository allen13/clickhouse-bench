"""C9 — Delta encoding behaviour across three data shapes.

What it tests
-------------
A single 10M-row table with NINE UInt64 columns. Three columns hold
identical data, but with different codecs applied:

- ``zero_*``  — every row holds 0 (constant column)
- ``mono_*``  — every row holds the row index (monotonic n+1)
- ``rand_*``  — every row holds cityHash64(n) (high-entropy random)

Per shape, three codec variants:

- ``*_lz4``         CODEC(LZ4)             general-purpose, no transform
- ``*_delta``       CODEC(Delta, LZ4)      first-order difference + LZ4
- ``*_dbldelta``    CODEC(DoubleDelta, LZ4) second-order difference + LZ4

The resulting per-column compressed sizes show *exactly* when Delta
encoding pays back and when it doesn't:

- Constant data collapses to nothing under any codec (LZ4 already
  finds the run of zeros).
- Monotonic data collapses under Delta (deltas are all 1) and
  DoubleDelta (second derivative is 0); plain LZ4 helps less.
- Random data is incompressible regardless of codec.

Reference: docs/codecs-compression.md.
"""
from __future__ import annotations

from scripts.experiments._lib import (
    get_client, server_summary, temp_tables, write_result,
)


TABLE = "exp_delta_demo"
ROWS = 10_000_000


# Columns are grouped by data shape; each shape has three codec variants.
# The CODEC string is used in DDL; the (shape, codec) tuple is used to
# group the result table.
COLUMNS = [
    # name                shape      codec_label        codec_clause
    ("zero_lz4",         "constant", "LZ4",             "CODEC(LZ4)"),
    ("zero_delta",       "constant", "Delta, LZ4",      "CODEC(Delta, LZ4)"),
    ("zero_dbldelta",    "constant", "DoubleDelta, LZ4","CODEC(DoubleDelta, LZ4)"),
    ("mono_lz4",         "monotonic","LZ4",             "CODEC(LZ4)"),
    ("mono_delta",       "monotonic","Delta, LZ4",      "CODEC(Delta, LZ4)"),
    ("mono_dbldelta",    "monotonic","DoubleDelta, LZ4","CODEC(DoubleDelta, LZ4)"),
    ("rand_lz4",         "random",   "LZ4",             "CODEC(LZ4)"),
    ("rand_delta",       "random",   "Delta, LZ4",      "CODEC(Delta, LZ4)"),
    ("rand_dbldelta",    "random",   "DoubleDelta, LZ4","CODEC(DoubleDelta, LZ4)"),
]

# Column-name -> SQL expression that produces its values from `numbers(ROWS)`.
SHAPE_EXPR = {
    "constant":  "toUInt64(0)",
    "monotonic": "number",
    "random":    "cityHash64(number)",
}


def main() -> None:
    client = get_client()
    server = server_summary(client)
    payload: dict = {
        "title": "Delta encoding behaviour: constant, monotonic, random",
        "server": server,
        "rows": ROWS,
    }

    column_defs = ",\n    ".join(
        f"{name} UInt64 {codec}" for name, _, _, codec in COLUMNS
    )
    select_exprs = ",\n        ".join(
        f"{SHAPE_EXPR[shape]} AS {name}"
        for name, shape, _, _ in COLUMNS
    )

    with temp_tables(client, TABLE):
        # Force WIDE part format so per-column sizes are reported by
        # system.parts_columns. Small parts default to COMPACT, which packs
        # every column into a single file and reports column_bytes_on_disk=0.
        client.command(f"""
            CREATE TABLE {TABLE} (
                id UInt64,
                {column_defs}
            ) ENGINE = MergeTree()
            ORDER BY id
            SETTINGS min_bytes_for_wide_part = 0, min_rows_for_wide_part = 0
        """)
        client.command(f"""
            INSERT INTO {TABLE}
            SELECT
                number AS id,
                {select_exprs}
            FROM numbers({ROWS})
        """)

        # Aggregate per-column sizes across all active parts.
        target_cols = [c[0] for c in COLUMNS]
        in_clause = ",".join(f"'{c}'" for c in target_cols)
        res = client.query(f"""
            SELECT
                column,
                sum(column_data_compressed_bytes)   AS comp_bytes,
                sum(column_data_uncompressed_bytes) AS uncomp_bytes
            FROM system.parts_columns
            WHERE active
              AND database = currentDatabase()
              AND table = '{TABLE}'
              AND column IN ({in_clause})
            GROUP BY column
        """).result_rows
        size_by_col = {r[0]: (int(r[1]), int(r[2])) for r in res}

        rows: list[dict] = []
        for name, shape, codec_label, _ in COLUMNS:
            comp_b, uncomp_b = size_by_col.get(name, (0, 0))
            rows.append({
                "column": name,
                "shape": shape,
                "codec": codec_label,
                "compressed_bytes": comp_b,
                "uncompressed_bytes": uncomp_b,
                "ratio": round(uncomp_b / max(comp_b, 1), 1),
            })
        payload["columns"] = rows

        # Convenience cross-tab: shape × codec → ratio
        cross: dict[str, dict[str, float]] = {}
        for r in rows:
            cross.setdefault(r["shape"], {})[r["codec"]] = r["ratio"]
        payload["ratios"] = cross

    write_result("c9_delta_encoding", payload)


if __name__ == "__main__":
    main()
