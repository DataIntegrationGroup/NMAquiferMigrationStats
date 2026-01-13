#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import re
import csv
from collections import defaultdict

import pyodbc
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

import pyodbc
print(pyodbc.drivers())

import pyodbc
import os






# =======================
# CONFIG — Google Sheet
# =======================
SERVICE_ACCOUNT_FILE = "service_account.json"
SPREADSHEET_ID = "1NtkaSWh8COQpMXd9AZ-fXMsRok9l-wwC1sz0lgVCTeo"
SHEET_NAME = "MIGRATION_STATUS"
COL_OLD = "NMAquifer_TableField"

OUT_CSV = "nma_aquifer_nonnull_counts.csv"

# =======================
# CONFIG — SQL Server
# =======================
# SQL_HOST = os.getenv("NMA_SQL_HOST", "127.0.0.1")
# SQL_PORT = int(os.getenv("NMA_SQL_PORT", "1433"))
#
# # SQL Server auth (recommended to use env vars)
# SQL_USER = os.getenv("NMA_SQL_USER", "sqlserver")
# SQL_PASS = os.getenv("NMA_SQL_PASS", "ilikewaterdata!!")
HOST = "127.0.0.1"
PORT = "1433"
USER = "sqlserver"
PASS = "ilikewaterdata!!"
DB = "NM_Aquifer_Dev_DB"

# If you want to force a schema preference (dbo first), keep this:
PREFERRED_SCHEMA = "dbo"


# -----------------------
# Helpers
# -----------------------
def normalize_key(x: str) -> str:
    if x is None:
        return ""
    return str(x).replace("\u00a0", " ").strip().lower()

def split_table_field(tf: str):
    s = (tf or "").replace("\u00a0", " ").strip()
    if "." not in s:
        return "", ""
    table, field = s.split(".", 1)
    return table.strip(), field.strip()

def bracket_ident(name: str) -> str:
    """Safe SQL Server identifier quoting with [ ] escaping."""
    if name is None:
        raise ValueError("Identifier is None")
    return "[" + str(name).replace("]", "]]") + "]"

def get_sheets_service():
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    return build("sheets", "v4", credentials=creds)

def fetch_sheet_table_fields() -> list[str]:
    service = get_sheets_service()
    resp = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_NAME}'!A1:ZZ"
    ).execute()

    values = resp.get("values", [])
    if not values:
        raise RuntimeError(f"Sheet '{SHEET_NAME}' is empty or not found.")

    headers = values[0]
    rows = values[1:]

    # find the column index for COL_OLD (flexible matching)
    def header_canon(h: str) -> str:
        s = normalize_key(h)
        return re.sub(r"[^a-z0-9]+", "", s)

    target = header_canon(COL_OLD)
    idx_old = None
    for i, h in enumerate(headers):
        if header_canon(h) == target:
            idx_old = i
            break
    if idx_old is None:
        raise RuntimeError(f"Could not find required column '{COL_OLD}' in headers: {headers}")

    out = []
    for r in rows:
        if idx_old < len(r):
            v = r[idx_old]
            if normalize_key(v):
                out.append(str(v).strip())
    return out

def connect_sqlserver() -> pyodbc.Connection:
    host = os.getenv("NMA_SQL_HOST", HOST)
    port = int(os.getenv("NMA_SQL_PORT", str(PORT)))
    db   = os.getenv("NMA_SQL_DB", DB)

    user = os.getenv("NMA_SQL_USER", USER)
    pwd  = os.getenv("NMA_SQL_PASS", PASS)

    if not user or not pwd:
        raise RuntimeError("Missing SQL credentials. Set NMA_SQL_USER and NMA_SQL_PASS.")

    conn_str = (
        "DRIVER={SQL Server};"
        f"SERVER={host},{port};"
        f"DATABASE={db};"
        f"UID={user};"
        f"PWD={pwd};"
    )
    return pyodbc.connect(conn_str, timeout=15)


def load_column_catalog(conn) -> dict[tuple[str, str], list[tuple[str, str, str]]]:
    """
    Returns mapping:
      (table_lower, col_lower) -> list of (schema_actual, table_actual, col_actual)
    If duplicates exist across schemas, we keep them all; later we prefer dbo.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT
            s.name AS schema_name,
            t.name AS table_name,
            c.name AS column_name
        FROM sys.tables t
        JOIN sys.schemas s ON t.schema_id = s.schema_id
        JOIN sys.columns c ON c.object_id = t.object_id
        ORDER BY s.name, t.name, c.column_id;
    """)
    mapping = defaultdict(list)
    for schema_name, table_name, column_name in cur.fetchall():
        mapping[(normalize_key(table_name), normalize_key(column_name))].append(
            (schema_name, table_name, column_name)
        )
    return mapping

def choose_best_match(matches: list[tuple[str, str, str]]) -> tuple[str, str, str]:
    """
    Prefer dbo schema if present; else first.
    """
    for m in matches:
        if normalize_key(m[0]) == normalize_key(PREFERRED_SCHEMA):
            return m
    return matches[0]

def build_table_to_cols(table_fields: list[str], catalog) -> tuple[dict, list[str]]:
    """
    Groups requested fields by (schema_actual, table_actual) -> set(col_actual)
    Returns also a list of warnings for missing fields.
    """
    table_to_cols = defaultdict(set)
    warnings = []

    for tf in table_fields:
        t_raw, c_raw = split_table_field(tf)
        if not t_raw or not c_raw:
            continue
        key = (normalize_key(t_raw), normalize_key(c_raw))
        matches = catalog.get(key)
        if not matches:
            warnings.append(f"Missing in SQL Server catalog: {tf}")
            continue

        schema, table, col = choose_best_match(matches)
        table_to_cols[(schema, table)].add(col)

    return table_to_cols, warnings

def run_counts(conn, table_to_cols) -> dict[str, dict]:
    """
    Returns:
      result[original_table_field_normalized-ish] = { schema, table, field, nonnull_count }
    We compute counts per table in one query: SELECT COUNT([c1])..., COUNT([cN]) FROM [schema].[table]
    """
    results = {}
    cur = conn.cursor()

    for (schema, table), cols in table_to_cols.items():
        cols = sorted(cols, key=lambda x: normalize_key(x))
        select_exprs = []
        for col in cols:
            # COUNT(col) counts non-null values in SQL Server
            select_exprs.append(f"COUNT({bracket_ident(col)}) AS {bracket_ident(col)}")

        q = (
            "SELECT " + ", ".join(select_exprs) +
            f" FROM {bracket_ident(schema)}.{bracket_ident(table)};"
        )

        try:
            cur.execute(q)
            row = cur.fetchone()
            # row aligns with cols
            for col, cnt in zip(cols, row):
                tf_key = f"{table}.{col}"  # keep it readable
                results[tf_key] = {
                    "schema": schema,
                    "table": table,
                    "field": col,
                    "nonnull_count": int(cnt) if cnt is not None else 0
                }
        except Exception as e:
            print(f"[ERROR] Count failed for {schema}.{table}: {e}")
            # If a table fails, set all requested cols to 0 so CSV still completes
            for col in cols:
                tf_key = f"{table}.{col}"
                results[tf_key] = {
                    "schema": schema,
                    "table": table,
                    "field": col,
                    "nonnull_count": 0
                }

    return results


def main():
    # 1) Pull requested fields from the sheet
    table_fields = fetch_sheet_table_fields()
    unique_table_fields = sorted({tf for tf in table_fields if normalize_key(tf)})
    print(f"Loaded {len(unique_table_fields)} unique NMAquifer_TableField values from Google Sheet.")

    # 2) Connect to SQL Server
    conn = connect_sqlserver()
    cur = conn.cursor()
    cur.execute("SELECT DB_NAME() AS db, SYSTEM_USER AS user_name;")
    print("Connected:", cur.fetchone())

    try:
        # 3) Build a column catalog so we can safely quote identifiers + avoid injection
        catalog = load_column_catalog(conn)

        # 4) Group fields by table for efficient counting
        table_to_cols, warnings = build_table_to_cols(unique_table_fields, catalog)
        if warnings:
            print(f"Warnings (showing first 20 of {len(warnings)}):")
            for w in warnings[:20]:
                print(" -", w)

        # 5) Run counts
        results = run_counts(conn, table_to_cols)

    finally:
        conn.close()

    # 6) Write CSV
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["table_field", "nonnull_count", "schema", "table", "field"])
        for tf, info in sorted(results.items(), key=lambda x: normalize_key(x[0])):
            w.writerow([tf, info["nonnull_count"], info["schema"], info["table"], info["field"]])

    print(f"✓ Wrote {len(results)} non-null counts to {OUT_CSV}")


if __name__ == "__main__":
    main()
