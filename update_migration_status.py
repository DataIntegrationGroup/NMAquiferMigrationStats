#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Compute migration tracking columns from scratch.

Inputs (required on sheet):
- Migration Path
- NMAquifer_TableField   (old table.field, like Equipment.DateInstalled)
- Final Schema Target    (final table.field, like transducer_observation.value)

Outputs (created if missing):
- Temp Schema Target
- Final Mapping Status
- Final Target Status
- Temp Mapping Status
- Temp Target Status
- Transfer Status

Statuses use EXACT strings:
"defined" "undefined" "exists" "missing"
"staging transfer complete" "final transfer complete" "incomplete"
and for the stop-block: "not being migrated"
"""

import os
import re
import sys
from typing import List, Optional, Tuple
from collections import defaultdict

import pg8000
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from datetime import datetime, timezone
# Write last-run timestamp here (pick a cell outside your data table)
UPDATED_CELL = "O2"  # change to e.g. "H1" or "AA1" if A1 is your header row
UPDATED_PREFIX = "MIGRATION STATUS UPDATED:"


# =======================
# CONFIG
# =======================
SERVICE_ACCOUNT_FILE = "service_account.json"
SPREADSHEET_ID = "1NtkaSWh8COQpMXd9AZ-fXMsRok9l-wwC1sz0lgVCTeo"
SHEET_NAME = "MIGRATION_STATUS"

DB_HOST = "127.0.0.1"
DB_PORT = 5432
DB_USER = "marissa.fichera@nmt.edu"
DB_NAME = "ocotillo-staging"
DB_PASS = os.getenv("DB_PASS", "")  # IAM token if needed

# Required input columns
COL_MIG_PATH = "Migration Path"
COL_OLD = "NMAquifer_TableField"
COL_FINAL = "Final Schema Target"  # accepts aliases below

FINAL_ALIASES = ["New Schema Target", "New Target Schema", "Final Target Schema"]

# Output columns you asked for
COL_TEMP_SCHEMA = "Temp Schema Target"
COL_FINAL_MAP = "Final Mapping Status"
COL_FINAL_STATUS = "Final Target Status"
COL_TEMP_MAP = "Temp Mapping Status"
COL_TEMP_STATUS = "Temp Target Status"
COL_XFER_STATUS = "Transfer Status"

WRITE_NEEDS_REVIEW_TO_FINAL_CELL = True
NEEDS_REVIEW_TEXT = "NEEDS REVIEW"

# Migration Path values (normalized)
PATH_NA = "n/a"
PATH_DIRECT = "direct-to-final"
PATH_STAGE = "stage then refactor"

# Exact status strings
S_DEFINED = "defined"
S_UNDEFINED = "undefined"
S_EXISTS = "exists"
S_MISSING = "missing"

XFER_STAGE_COMPLETE = "staging transfer complete"
XFER_FINAL_COMPLETE = "final transfer complete"
XFER_INCOMPLETE = "incomplete"
XFER_NOT_MIGRATED = "not being migrated"

# PRESSURE_HAND_INPUT_TABLE = "waterlevelscontinuous_pressure"

# was:
# PRESSURE_HAND_INPUT_TABLE = "waterlevelscontinuous_pressure"

HAND_INPUT_TABLES = {
    "waterlevelscontinuous_pressure",
    "waterlevelscontinuous_acoustic",
    "minorandtracechemistry",
    "equipment",
    "location",
    "waterlevels"

}

# =======================
# Helpers
# =======================

def remove_underscores(s: str) -> str:
    return (s or "").replace("_", "")

def format_actual_tf(table_lower: str, col_lower: str, table_actual: dict, col_actual: dict) -> str:
    t_name = table_actual.get(table_lower, table_lower)
    c_name = col_actual.get((table_lower, col_lower), col_lower)
    return f"{t_name}.{c_name}"

def resolve_temp_schema_target(old_tf: str,
                               oc_set: set[str],
                               table_cols: dict[str, set[str]],
                               table_actual: dict,
                               col_actual: dict) -> tuple[str, bool]:
    """
    Returns (resolved_temp_schema_target, exists_bool)

    Base: nma_<oldtable>.<oldfield>
    If missing, try:
      1) nma_<oldtable>.nma_<oldfield>
      2) nma_<oldtable>.nma_<oldfield> where oldfield may have underscores (underscore-insensitive match)
      3) nma_<oldtable>.<oldfield> where oldfield may have underscores (underscore-insensitive match)
    """
    base = make_nma_stage_table_field(old_tf)  # lower-ish
    if is_blank(base):
        return "", False

    base_k = normalize_key(base)
    if base_k in oc_set:
        t, c = parse_table_field(base)
        return format_actual_tf(t, c, table_actual, col_actual), True

    # derive table + field from base
    try:
        t, f = parse_table_field(base)  # lower table, lower col
    except ValueError:
        return base, False

    cols = table_cols.get(t)
    if not cols:
        return base, False

    # explicit: nma_<table>.nma_<field>
    cand1_col = f"nma_{f}"
    cand1_tf = f"{t}.{cand1_col}"
    if normalize_key(cand1_tf) in oc_set:
        return format_actual_tf(t, cand1_col, table_actual, col_actual), True

    # underscore-insensitive search
    f_key = remove_underscores(f)
    nf_key = remove_underscores("nma_" + f)

    # Prefer nma_<field> matches first (per your spec), then plain <field> matches
    nma_matches = []
    plain_matches = []

    for col in cols:
        col_no_us = remove_underscores(col)
        if col_no_us == nf_key and col.startswith("nma_"):
            nma_matches.append(col)
        elif col_no_us == f_key and (not col.startswith("nma_")):
            plain_matches.append(col)
        # also allow nma_ prefix even if spec didn't mention it explicitly
        elif col_no_us == nf_key and (not col.startswith("nma_")):
            nma_matches.append(col)

    if nma_matches:
        chosen = sorted(nma_matches)[0]
        return format_actual_tf(t, chosen, table_actual, col_actual), True

    if plain_matches:
        chosen = sorted(plain_matches)[0]
        return format_actual_tf(t, chosen, table_actual, col_actual), True

    return base, False

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

def normalize_table_for_db(table: str) -> str:
    # stage table naming convention you were using
    return (table or "").strip().lower().replace(" ", "_")

def make_nma_stage_table_field(old_tf: str) -> str:
    """
    Old:  Table.Field
    Temp: nma_<table>.<field>  (table normalized; field lower)
    """
    t, f = split_table_field(old_tf)
    t_db = normalize_table_for_db(t)
    f_db = (f or "").strip().lower()
    if not t_db or not f_db:
        return ""
    return f"nma_{t_db}.{f_db}"

def qident(name: str) -> str:
    """Quote Postgres identifier safely."""
    if name is None:
        raise ValueError("Identifier is None")
    s = str(name)
    return '"' + s.replace('"', '""') + '"'

def parse_table_field(tf: str) -> Tuple[str, str]:
    parts = [p.strip() for p in (tf or "").split(".") if p.strip()]
    if len(parts) != 2:
        raise ValueError(f"Bad table.field: {tf}")
    return parts[0].lower(), parts[1].lower()

def header_canon(h: str) -> str:
    s = normalize_key(h)
    return re.sub(r"[^a-z0-9]+", "", s)

def find_required_col(headers: List[str], desired_name: str, aliases: Optional[List[str]] = None) -> int:
    wanted = [desired_name] + (aliases or [])
    wanted_c = {header_canon(x) for x in wanted}
    for i, h in enumerate(headers):
        if header_canon(h) in wanted_c:
            return i
    raise RuntimeError(f"Required column not found: '{desired_name}'. Headers: {headers}")

def ensure_output_col(headers: List[str], rows: List[List[str]], desired_name: str) -> int:
    desired_c = header_canon(desired_name)
    for i, h in enumerate(headers):
        if header_canon(h) == desired_c:
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

def load_ocotillo_schema_from_db(conn):
    """
    Returns:
      oc_set: set of normalized 'table.field'
      table_actual: {table_lower: actual_table_name}
      col_actual: {(table_lower, col_lower): actual_col_name}
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema='public'
        ORDER BY table_name, ordinal_position;
    """)
    table_actual = {}
    col_actual = {}
    oc_set = set()
    for t, c in cur.fetchall():
        tl, cl = str(t).lower(), str(c).lower()
        table_actual.setdefault(tl, str(t))
        col_actual[(tl, cl)] = str(c)
        oc_set.add(f"{tl}.{cl}")
    return oc_set, table_actual, col_actual

def build_nonnull_exists_lookup(conn, table_to_cols, table_actual, col_actual):
    """
    Returns dict[(table_lower, col_lower)] = bool
    Uses SELECT EXISTS(SELECT 1 FROM "Table" WHERE "Col" IS NOT NULL LIMIT 1) ...
    """
    out = {}
    cur = conn.cursor()

    for t_lower, cols_set in table_to_cols.items():
        cols = sorted(cols_set)
        t_name = table_actual.get(t_lower, t_lower)
        t_sql = qident(t_name)

        exprs = []
        for c_lower in cols:
            c_name = col_actual.get((t_lower, c_lower), c_lower)
            c_sql = qident(c_name)
            exprs.append(f"EXISTS(SELECT 1 FROM {t_sql} WHERE {c_sql} IS NOT NULL LIMIT 1) AS {qident(c_name)}")

        q = "SELECT " + ", ".join(exprs) + ";"
        try:
            cur.execute(q)
            row = cur.fetchone()
            for c_lower, has_val in zip(cols, row):
                out[(t_lower, c_lower)] = bool(has_val)
        except Exception as e:
            print(f"[WARN] nonnull exists check failed for {t_name}: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
            for c_lower in cols:
                out[(t_lower, c_lower)] = False

    return out

def normalize_mig_path(x: Optional[str]) -> str:
    """
    Make Migration Path matching tolerant to variations like:
    'Stage then refactor', 'stage then refactor (1:1)', 'direct to final', etc.
    """
    k = normalize_key(x)
    k = re.sub(r"\s+", " ", k)          # collapse whitespace
    k = re.sub(r"[^a-z0-9 ]+", " ", k)  # drop punctuation/parens/arrows etc
    k = re.sub(r"\s+", " ", k).strip()

    if k in ("n a", "na", "n a", "not being migrated"):
        return PATH_NA

    if "direct" in k and "final" in k:
        return PATH_DIRECT

    if "stage" in k and "refactor" in k:
        return PATH_STAGE

    # if it's something else, return normalized text so you can see it in debugging if needed
    return k


# =======================
# Main
# =======================
def main():
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

    # Required input cols
    idx_path_in = find_required_col(headers, COL_MIG_PATH)
    idx_old = find_required_col(headers, COL_OLD)
    idx_final_in = find_required_col(headers, COL_FINAL, aliases=FINAL_ALIASES)

    # Temp Schema Target might already exist and have hand-entered values
    idx_temp_schema_existing = None
    try:
        idx_temp_schema_existing = find_required_col(headers, COL_TEMP_SCHEMA)
    except Exception:
        idx_temp_schema_existing = None

    # Output cols
    idx_temp_schema = ensure_output_col(headers, data_rows, COL_TEMP_SCHEMA)
    idx_final_map = ensure_output_col(headers, data_rows, COL_FINAL_MAP)
    idx_final_status = ensure_output_col(headers, data_rows, COL_FINAL_STATUS)
    idx_temp_map = ensure_output_col(headers, data_rows, COL_TEMP_MAP)
    idx_temp_status = ensure_output_col(headers, data_rows, COL_TEMP_STATUS)
    idx_xfer = ensure_output_col(headers, data_rows, COL_XFER_STATUS)

    # Connect to Postgres (Ocotillo staging)
    conn = pg8000.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASS,
        database=DB_NAME,
    )
    try:
        oc_set, table_actual, col_actual = load_ocotillo_schema_from_db(conn)

        table_cols = defaultdict(set)
        for tf in oc_set:
            # tf is lower 'table.col'
            if "." not in tf:
                continue
            t, c = tf.split(".", 1)
            table_cols[t].add(c)

        # Precompute which fields need "has any nonnull?" checks
        table_to_cols = defaultdict(set)

        resolved_temp_for_row = ["" for _ in data_rows]
        resolved_temp_exists = [False for _ in data_rows]

        for i, r in enumerate(data_rows):
            mig_path = normalize_mig_path(r[idx_path_in] if idx_path_in < len(r) else "")
            old_tf = r[idx_old] if idx_old < len(r) else ""
            final_tf = r[idx_final_in] if idx_final_in < len(r) else ""

            if mig_path == PATH_DIRECT:
                if not is_blank(final_tf):
                    k = normalize_key(final_tf)
                    if k in oc_set:
                        t, c = parse_table_field(final_tf)
                        table_to_cols[t].add(c)


            elif mig_path == PATH_STAGE:

                old_table, _ = split_table_field(old_tf)

                old_table_k = normalize_key(old_table)

                # If this is the Pressure table, prefer the sheet's existing Temp Schema Target

                sheet_temp = ""

                if old_table_k in HAND_INPUT_TABLES and idx_temp_schema_existing is not None:
                    sheet_temp = (r[idx_temp_schema_existing] or "").strip()

                if sheet_temp:
                    # keep hand-input AND compute existence based on it
                    ktemp = normalize_key(sheet_temp)
                    if ktemp in oc_set:
                        t, c = parse_table_field(sheet_temp)
                        temp_tf = format_actual_tf(t, c, table_actual, col_actual)
                        exists = True
                    else:
                        temp_tf = sheet_temp
                        exists = False
                else:
                    # normal resolver for everyone else (and Pressure rows with blank temp)
                    temp_tf, exists = resolve_temp_schema_target(
                        old_tf=old_tf,
                        oc_set=oc_set,
                        table_cols=table_cols,
                        table_actual=table_actual,
                        col_actual=col_actual,
                    )
                resolved_temp_for_row[i] = temp_tf
                resolved_temp_exists[i] = exists
                if exists and temp_tf:
                    t, c = parse_table_field(temp_tf)
                    table_to_cols[t].add(c)

            # PATH_NA -> no DB checks

        nonnull_exists = build_nonnull_exists_lookup(conn, table_to_cols, table_actual, col_actual)

    finally:
        conn.close()

    # Compute outputs row-by-row
    # Compute outputs row-by-row
    out_temp_schema = []
    out_final_map = []
    out_final_status = []
    out_temp_map = []
    out_temp_status = []
    out_xfer = []
    out_final_cell = []  # possibly write NEEDS REVIEW into Final Schema Target

    for row_index, r in enumerate(data_rows):
        mig_path = normalize_mig_path(r[idx_path_in] if idx_path_in < len(r) else "")
        old_tf = r[idx_old] if idx_old < len(r) else ""
        final_tf = r[idx_final_in] if idx_final_in < len(r) else ""

        # defaults
        temp_schema = ""
        final_map = ""
        final_status = ""
        temp_map = ""
        temp_status = ""
        xfer = ""
        final_cell_val = final_tf  # default: unchanged

        # FIRST BLOCK: n/a => not being migrated
        if mig_path == PATH_NA:
            temp_schema = "n/a"
            temp_map = "n/a"
            temp_status = "n/a"
            final_map = "n/a"
            final_status = "n/a"
            xfer = XFER_NOT_MIGRATED
            final_cell_val = XFER_NOT_MIGRATED


        # SECOND BLOCK: direct-to-final
        elif mig_path == PATH_DIRECT:
            temp_schema = "n/a"
            temp_map = "n/a"
            temp_status = "n/a"

            if not is_blank(final_tf):
                final_map = S_DEFINED
                k = normalize_key(final_tf)

                if k in oc_set:
                    final_status = S_EXISTS
                    try:
                        t, c = parse_table_field(final_tf)
                        has_any = nonnull_exists.get((t, c), False)
                    except ValueError:
                        has_any = False
                    xfer = XFER_FINAL_COMPLETE if has_any else XFER_INCOMPLETE
                else:
                    final_status = S_MISSING
                    xfer = XFER_INCOMPLETE

            else:
                if WRITE_NEEDS_REVIEW_TO_FINAL_CELL:
                    final_cell_val = NEEDS_REVIEW_TEXT
                final_map = S_UNDEFINED
                final_status = S_MISSING
                xfer = XFER_INCOMPLETE

        # THIRD BLOCK: stage then refactor
        elif mig_path == PATH_STAGE:
            temp_schema = resolved_temp_for_row[row_index]
            temp_map = S_DEFINED

            final_map = S_DEFINED if not is_blank(final_tf) else S_UNDEFINED

            if resolved_temp_exists[row_index] and not is_blank(temp_schema):
                temp_status = S_EXISTS
                try:
                    t, c = parse_table_field(temp_schema)
                    has_any = nonnull_exists.get((t, c), False)
                except ValueError:
                    has_any = False
                xfer = XFER_STAGE_COMPLETE if has_any else XFER_INCOMPLETE
            else:
                temp_status = S_MISSING
                xfer = XFER_INCOMPLETE

            # Final target status is informational only
            kfinal = normalize_key(final_tf)
            final_status = S_EXISTS if (not is_blank(final_tf) and kfinal in oc_set) else S_MISSING

        # Unexpected Migration Path => safe fallback
        else:
            temp_schema = ""
            temp_map = S_UNDEFINED
            temp_status = S_MISSING
            final_map = S_UNDEFINED
            final_status = S_MISSING
            xfer = XFER_INCOMPLETE

        # ✅ THIS IS THE CRITICAL PART YOU WERE MISSING:
        out_temp_schema.append(temp_schema)
        out_final_map.append(final_map)
        out_final_status.append(final_status)
        out_temp_map.append(temp_map)
        out_temp_status.append(temp_status)
        out_xfer.append(xfer)
        out_final_cell.append(final_cell_val)

    # Write header row back (in case we added cols)
    last_col_letter = col_index_to_letter(len(headers) - 1)
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_NAME}'!A1:{last_col_letter}1",
        valueInputOption="RAW",
        body={"values": [headers]}
    ).execute()

    num_rows = len(data_rows) + 1

    # Write columns back (including possibly updated Final Schema Target column)
    updates = [
        (idx_temp_schema, COL_TEMP_SCHEMA, out_temp_schema),
        (idx_final_map, COL_FINAL_MAP, out_final_map),
        (idx_final_status, COL_FINAL_STATUS, out_final_status),
        (idx_temp_map, COL_TEMP_MAP, out_temp_map),
        (idx_temp_status, COL_TEMP_STATUS, out_temp_status),
        (idx_xfer, COL_XFER_STATUS, out_xfer),

        # optionally write back Final Schema Target if we inserted NEEDS REVIEW
        (idx_final_in, headers[idx_final_in], out_final_cell),
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

    print(f"✓ Updated '{SHEET_NAME}' with: {COL_TEMP_SCHEMA}, {COL_FINAL_MAP}, {COL_FINAL_STATUS}, "
          f"{COL_TEMP_MAP}, {COL_TEMP_STATUS}, {COL_XFER_STATUS}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.exit(str(e))
