#!/usr/bin/env python
# -*- coding: utf-8 -*-

import csv
import os
import pytds

OUT_CSV = "nma_aquifer_nonnull_counts.csv"

SERVER = "127.0.0.1"
PORT = 1433
USER = os.getenv("NMA_SQL_USER", "sqlserver")
PASSWORD = os.getenv("NMA_SQL_PASS", "ilikewaterdata!!")
DATABASE = os.getenv("NMA_SQL_DB", "NM_Aquifer_Dev_DB")  # set this!

# Optional: limit to a schema (or list of schemas)
SCHEMAS = os.getenv("NMA_SQL_SCHEMAS", "dbo").split(",")  # e.g. "dbo" or "dbo,staging"


def qident(name: str) -> str:
    # SQL Server identifier quoting with brackets
    # (handles names with spaces/reserved words)
    if name is None:
        raise ValueError("Identifier is None")
    return "[" + str(name).replace("]", "]]") + "]"


def main():
    if not DATABASE:
        raise SystemExit("Set NMA_SQL_DB env var (database name).")

    conn = pytds.connect(
        server=SERVER,
        port=PORT,
        user=USER,
        password=PASSWORD,
        database=DATABASE,
        autocommit=True,
    )
    cur = conn.cursor()

    # pull columns
    schema_list = tuple(s.strip() for s in SCHEMAS if s.strip())
    placeholders = ",".join(["%s"] * len(schema_list))

    cur.execute(f"""
        SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA IN ({placeholders})
        ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
    """, schema_list)

    cols_by_table = {}
    for schema, table, col in cur.fetchall():
        cols_by_table.setdefault((schema, table), []).append(col)

    rows_out = []
    for (schema, table), cols in cols_by_table.items():
        # One query per table: COUNT(col) per column
        select_exprs = ", ".join([f"COUNT({qident(c)}) AS {qident(c)}" for c in cols])
        sql = f"SELECT {select_exprs} FROM {qident(schema)}.{qident(table)}"

        try:
            cur.execute(sql)
            counts = cur.fetchone()
        except Exception as e:
            print(f"[WARN] failed counting {schema}.{table}: {e}")
            # write 0s if table fails (or skip—your choice)
            counts = [0] * len(cols)

        for c, cnt in zip(cols, counts):
            table_field = f"{table}.{c}"
            rows_out.append([table_field, int(cnt or 0), schema, table, c])

    conn.close()

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["table_field", "nonnull_count", "schema", "table", "field"])
        w.writerows(rows_out)

    print(f"✓ Wrote {len(rows_out)} rows to {OUT_CSV}")


if __name__ == "__main__":
    main()
