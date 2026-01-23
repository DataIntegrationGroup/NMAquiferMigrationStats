#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Stage-then-refactor non-null count checker (separate script to avoid breaking status logic).

Reads Google Sheet rows and, for Migration Path == "stage then refactor":
  - Old NonNull Count: from NMA export CSV (table_field, nonnull_count, ...)
  - Temp NonNull Count: from Ocotillo DB (COUNT(temp_column))
  - NonNull Diff: old - temp

Does NOT modify any other logic/status columns.
"""

import os
import re
import sys
import csv
from typing import Optional, Tuple, List
from collections import defaultdict

import pg8000
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from datetime import datetime, timezone
# Write last-run timestamp here (pick a cell outside your data table)
UPDATED_CELL = "O3"  # change to e.g. "H1" or "AA1" if A1 is your header row
UPDATED_PREFIX = "ROW COUNTS UPDATED:"

# =======================
# CONFIG
# =======================
SERVICE_ACCOUNT_FILE = "service_account.json"
SPREADSHEET_ID = "1NtkaSWh8COQpMXd9AZ-fXMsRok9l-wwC1sz0lgVCTeo"
SHEET_NAME = "MIGRATION_STATUS"

# Your NMAquifer non-null export CSV (produced by your other script)
NMA_COUNTS_CSV = "nma_aquifer_nonnull_counts.csv"
# expected columns include: table_field, nonnull_count, schema, table, field

DB_HOST = "127.0.0.1"
DB_PORT = 5432
DB_USER = "marissa.fichera@nmt.edu"
DB_NAME = "ocotillo-staging"
DB_PASS = os.getenv("DB_PASS", "")  # IAM token if needed

# Required sheet columns
COL_MIG_PATH = "Migration Path"
COL_OLD_TF = "NMAquifer_TableField"
COL_TEMP_TF = "Temp Schema Target"

# Output columns (created if missing)
COL_OLD_COUNT = "NMA NonNull Count"
COL_TEMP_COUNT = "Temp NonNull Count"
COL_DIFF = "NonNull Diff"

# Migration Path values
PATH_STAGE = "stage then refactor"

COL_XFER_STATUS = "Transfer Status"
XFER_STAGE_COMPLETE = "staging transfer complete"



# =======================
# Helpers
# =======================
def normalize_key(x: Optional[str]) -> str:
    if x is None:
        return ""
    return str(x).replace("\u00a0", " ").strip().lower()

def is_blank(x: Optional[str]) -> bool:
    return normalize_key(x) == ""

def split_table_field(tf: str) -> Tuple[str, str]:
    s = (tf or "").replace("\u00a0", " ").strip()
    if "." in s:
        t, f = s.split(".", 1)
        return t.strip(), f.strip()
    return s.strip(), ""

def parse_table_field(tf: str) -> Tuple[str, str]:
    parts = [p.strip() for p in (tf or "").split(".") if p.strip()]
    if len(parts) != 2:
        raise ValueError(f"Bad table.field: {tf}")
    return parts[0].lower(), parts[1].lower()

def qident(name: str) -> str:
    if name is None:
        raise ValueError("Identifier is None")
    s = str(name)
    return '"' + s.replace('"', '""') + '"'

def header_canon(h: str) -> str:
    s = normalize_key(h)
    return re.sub(r"[^a-z0-9]+", "", s)

def find_required_col(headers: List[str], desired_name: str) -> int:
    want = header_canon(desired_name)
    for i, h in enumerate(headers):
        if header_canon(h) == want:
            return i
    raise RuntimeError(f"Required column not found: '{desired_name}'. Headers: {headers}")

def ensure_output_col(headers: List[str], rows: List[List[str]], desired_name: str) -> int:
    want = header_canon(desired_name)
    for i, h in enumerate(headers):
        if header_canon(h) == want:
            return i
    headers.append(desired_name)
    new_idx = len(headers) - 1
    for r in rows:
        while len(r) < len(headers):
            r.append("")
    return new_idx

def col_index_to_letter(idx: int) -> str:
    idx += 1
    letters = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters

def get_sheets_service():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    return build("sheets", "v4", credentials=creds)


def load_nma_counts_csv(csv_path: str) -> dict[tuple[str, str], int]:
    """
    Reads NMA counts CSV:
      table_field, nonnull_count, schema, table, field
    Returns:
      (table_lower, field_lower) -> int nonnull_count
    """
    out: dict[tuple[str, str], int] = {}

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise RuntimeError(f"{csv_path} has no header row")

        if "table_field" not in reader.fieldnames or "nonnull_count" not in reader.fieldnames:
            raise RuntimeError(
                f"{csv_path} must have columns: table_field, nonnull_count. Found: {reader.fieldnames}"
            )

        for row in reader:
            tf = (row.get("table_field") or "").strip()
            if not tf or "." not in tf:
                continue

            t, c = tf.split(".", 1)
            t = normalize_key(t)
            c = normalize_key(c)

            cnt_raw = (row.get("nonnull_count") or "").strip()
            try:
                cnt = int(cnt_raw) if cnt_raw != "" else 0
            except ValueError:
                cnt = 0

            out[(t, c)] = cnt

    return out


def load_ocotillo_schema_maps(conn):
    """
    Returns:
      oc_set: set of "table_lower.col_lower" visible in public schema
      table_actual: map lower->actual table name (case preserved)
      col_actual: map (table_lower, col_lower)->actual col name
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema='public'
        ORDER BY table_name, ordinal_position;
    """)
    oc_set = set()
    table_actual = {}
    col_actual = {}
    for t, c in cur.fetchall():
        tl, cl = str(t).lower(), str(c).lower()
        table_actual.setdefault(tl, str(t))
        col_actual[(tl, cl)] = str(c)
        oc_set.add(f"{tl}.{cl}")
    return oc_set, table_actual, col_actual


def build_temp_nonnull_counts(conn,
                             table_to_cols: dict[str, set[str]],
                             table_actual: dict,
                             col_actual: dict) -> dict[tuple[str, str], Optional[int]]:
    """
    For each table, runs:
      SELECT COUNT("col1") AS "col1", COUNT("col2") AS "col2", ...
      FROM "Table";
    Returns:
      (table_lower, col_lower) -> int count (or None if query failed)
    """
    out: dict[tuple[str, str], Optional[int]] = {}
    cur = conn.cursor()

    for t_lower, cols_set in table_to_cols.items():
        cols = sorted(cols_set)
        if not cols:
            continue

        t_name = table_actual.get(t_lower, t_lower)
        t_sql = qident(t_name)

        exprs = []
        for c_lower in cols:
            c_name = col_actual.get((t_lower, c_lower), c_lower)
            c_sql = qident(c_name)
            # alias must be a valid identifier: use quoted alias too
            exprs.append(f"COUNT({c_sql}) AS {qident(c_name)}")

        q = "SELECT " + ", ".join(exprs) + f" FROM {t_sql};"

        try:
            cur.execute(q)
            row = cur.fetchone()
            for c_lower, cnt in zip(cols, row):
                out[(t_lower, c_lower)] = int(cnt)
        except Exception as e:
            print(f"[WARN] COUNT failed for {t_name}: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
            for c_lower in cols:
                out[(t_lower, c_lower)] = None

    return out


# =======================
# Main
# =======================
def main():
    # 1) Load sheet
    service = get_sheets_service()
    resp = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_NAME}'!A1:ZZ"
    ).execute()

    values = resp.get("values", [])
    if not values:
        sys.exit(f"Sheet '{SHEET_NAME}' is empty or not found.")

    headers = values[0]
    data_rows = values[1:]

    for r in data_rows:
        while len(r) < len(headers):
            r.append("")

    idx_path = find_required_col(headers, COL_MIG_PATH)
    idx_old = find_required_col(headers, COL_OLD_TF)
    idx_temp = find_required_col(headers, COL_TEMP_TF)

    idx_old_count = ensure_output_col(headers, data_rows, COL_OLD_COUNT)
    idx_temp_count = ensure_output_col(headers, data_rows, COL_TEMP_COUNT)
    idx_diff = ensure_output_col(headers, data_rows, COL_DIFF)

    # 2) Load NMA non-null counts
    nma_counts = load_nma_counts_csv(NMA_COUNTS_CSV)
    print(f"Loaded {len(nma_counts)} NMA old-field non-null counts from {NMA_COUNTS_CSV}")

    # 3) Connect to DB and prep COUNT queries only for relevant temp targets
    conn = pg8000.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASS,
        database=DB_NAME,
    )

    try:
        oc_set, table_actual, col_actual = load_ocotillo_schema_maps(conn)

        table_to_cols = defaultdict(set)

        # Gather which temp targets exist in schema and need COUNTs
        for r in data_rows:
            mig_path = normalize_key(r[idx_path] if idx_path < len(r) else "")
            if mig_path != PATH_STAGE:
                continue

            temp_tf = r[idx_temp] if idx_temp < len(r) else ""
            if is_blank(temp_tf):
                continue

            try:
                t, c = parse_table_field(temp_tf)
            except ValueError:
                continue

            # Only query fields that exist (prevents COUNT errors)
            if f"{t}.{c}" not in oc_set:
                continue

            table_to_cols[t].add(c)

        temp_counts = build_temp_nonnull_counts(conn, table_to_cols, table_actual, col_actual)

    finally:
        conn.close()

    # 4) Compute outputs
    out_old_count = []
    out_temp_count = []
    out_diff = []

    for r in data_rows:
        mig_path = normalize_key(r[idx_path] if idx_path < len(r) else "")
        old_tf = r[idx_old] if idx_old < len(r) else ""
        temp_tf = r[idx_temp] if idx_temp < len(r) else ""

        if mig_path != PATH_STAGE:
            out_old_count.append("")
            out_temp_count.append("")
            out_diff.append("")
            continue

        # old count from CSV
        old_t, old_c = split_table_field(old_tf)
        old_key = (normalize_key(old_t), normalize_key(old_c))
        old_cnt = nma_counts.get(old_key)
        old_cnt_str = "" if old_cnt is None else str(old_cnt)

        # temp count from DB (if temp target exists and was queried)
        temp_cnt = None
        temp_cnt_str = ""

        if not is_blank(temp_tf):
            try:
                t, c = parse_table_field(temp_tf)
                temp_cnt = temp_counts.get((t, c))
                if temp_cnt is not None:
                    temp_cnt_str = str(temp_cnt)
            except ValueError:
                pass

        # diff if both present
        diff_str = ""
        if old_cnt is not None and temp_cnt is not None:
            diff_str = str(int(old_cnt) - int(temp_cnt))

        out_old_count.append(old_cnt_str)
        out_temp_count.append(temp_cnt_str)
        out_diff.append(diff_str)

    # 5) Write updates back
    last_col_letter = col_index_to_letter(len(headers) - 1)
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_NAME}'!A1:{last_col_letter}1",
        valueInputOption="RAW",
        body={"values": [headers]}
    ).execute()

    # -------------------------------
    # NON-INTRUSIVE OVERRIDE: Transfer Status
    # Only change Transfer Status when NonNull Diff == 0 (for stage then refactor rows).
    # Otherwise preserve existing sheet values.
    # -------------------------------
    idx_xfer = find_required_col(headers, COL_XFER_STATUS)  # must already exist on sheet
    out_xfer = []

    for i, r in enumerate(data_rows):
        existing_xfer = r[idx_xfer] if idx_xfer < len(r) else ""
        mig_path = normalize_key(r[idx_path] if idx_path < len(r) else "")
        diff_str = out_diff[i] if i < len(out_diff) else ""

        if mig_path == PATH_STAGE and diff_str == "0":
            out_xfer.append(XFER_STAGE_COMPLETE)
        else:
            out_xfer.append(existing_xfer)

    num_rows = len(data_rows) + 1
    updates = [
        (idx_old_count, COL_OLD_COUNT, out_old_count),
        (idx_temp_count, COL_TEMP_COUNT, out_temp_count),
        (idx_diff, COL_DIFF, out_diff),
        (idx_xfer, COL_XFER_STATUS, out_xfer),
    ]

    for col_idx, header_name, col_data in updates:
        col_letter = col_index_to_letter(col_idx)
        col_values = [[header_name]] + [[v] for v in col_data]
        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{SHEET_NAME}'!{col_letter}1:{col_letter}{num_rows}",
            valueInputOption="RAW",
            body={"values": col_values}
        ).execute()

    # --- write last-run timestamp (single cell, non-intrusive) ---
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_NAME}'!{UPDATED_CELL}",
        valueInputOption="RAW",
        body={"values": [[f"{UPDATED_PREFIX} {ts}"]]}
    ).execute()

    print("✓ Wrote stage-then-refactor non-null counts to sheet columns:",
          COL_OLD_COUNT, COL_TEMP_COUNT, COL_DIFF)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.exit(f"An error occurred: {e}")
