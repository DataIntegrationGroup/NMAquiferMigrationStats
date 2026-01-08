#!/usr/bin/env python
# -*- coding: utf-8 -*-

import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from google.cloud.sql.connector import Connector, IPTypes
import pg8000



# =============== CONFIG =================

INSTANCE_CONNECTION_NAME = "waterdatainitiative-271000:us-west4:dataservices"
DB_USER = "ocotillo"
DB_PASS = "Ilikewaterdata1!"
DB_NAME = "ocotillo-staging"
IP_TYPE = IPTypes.PUBLIC  # or IPTypes.PUBLIC if you use public IP

connector = Connector()
conn = connector.connect(
    INSTANCE_CONNECTION_NAME,
    "pg8000",
    user=DB_USER,
    password=DB_PASS,
    db=DB_NAME,
    ip_type=IP_TYPE,
)

SERVICE_ACCOUNT_FILE = "service_account.json"
SPREADSHEET_ID = "1NtkaSWh8COQpMXd9AZ-fXMsRok9l-wwC1sz0lgVCTeo"
SHEET_NAME = "MIGRATION_STATUS"

OCOTILLO_CSV_PATH = "ocotillo_current.csv"
OCOTILLO_CSV_COL = "table_field"

# Exact 1:1 table names (table portion only; fields can vary)
ONE_TO_ONE_TABLE_NAMES = [
    "WaterLevelsContinuous_Acoustic",
    "WaterLevelsContinuous_Pressure",
    "Radionuclides",
    "Chemistry SampleInfo",
    "FieldParameters",
    "MajorChemistry",
    "MinorandTraceChemistry",
    "SurfaceWaterData"
]

# Required input columns (must exist; script will fail if not found)
COL_OLD = "NMAquifer_TableField"
COL_NEW = "New Schema Target"

# Output columns (will be created if missing)
COL_MAP = "Mapping Status"
COL_PATH = "Migration Path"
COL_XFER = "Transfer Status"
COL_DEST = "Destination"
COL_REFACTOR = "Refactor Status"

# Values (exact strings written)
VAL_STAGE_THEN_REFACTOR = "stage then refactor"
VAL_DIRECT_TO_FINAL = "direct-to-final"
VAL_UNKNOWN = "unknown"

VAL_DEFINED_EXISTS = "defined + target exists"
VAL_DEFINED_MISSING = "defined + target missing"
VAL_UNDEFINED = "undefined"
VAL_NOT_STARTED = "not started"

VAL_COMPLETE = "complete"
VAL_INCOMPLETE = "incomplete"
VAL_BLOCKED = "blocked"

VAL_NA_UPPER = "N/A"   # for the special 1:1 NA branch you specified
VAL_NA_LOWER = "N/A"   # mapping status for non-1:1 N/A per your text
VAL_PATH_NA = "N/A"
VAL_NOT_BEING_MIGRATED = "not being migrated"


VAL_NEEDS_REVIEW = "NEEDS REVIEW"

DEBUG_PRINT_SAMPLES = True  # set False when you're satisfied

# =======================================

import re
from collections import defaultdict

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

def parse_table_field(tf: str) -> tuple[str, str]:
    parts = [p.strip() for p in (tf or "").split(".") if p.strip()]
    if len(parts) != 2:
        raise ValueError(f"Bad Destination format (expected Table.Field): {tf}")
    table = parts[0].lower()
    col = parts[1].lower()
    return table, col

def safe_ident(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise ValueError(f"Unsafe identifier: {name}")
    return f'"{name}"'  # quote to preserve case if needed

def is_skip_destination(dest: str) -> bool:
    k = (dest or "").strip().lower()
    return k in ("", "n/a", "na", "not being migrated")

def apply_transfer_status_from_db(dest_val: str, xfer_val: str, nonnull_lookup: dict) -> str:
    """
    If Destination is a real table.field, overwrite Transfer Status based on non-null data:
      any non-null -> complete
      all null / empty -> incomplete
    Otherwise, return xfer_val unchanged.
    """
    if is_skip_destination(dest_val):
        return xfer_val
    try:
        t, c = parse_table_field(dest_val)  # IMPORTANT: should return lowercase table/col
    except ValueError:
        return xfer_val

    has_nonnull = nonnull_lookup.get((t, c), False)
    return VAL_COMPLETE if has_nonnull else VAL_INCOMPLETE



def get_sheets_service():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    return build("sheets", "v4", credentials=creds)


def normalize_key(x: Optional[str]) -> str:
    if x is None:
        return ""
    # normalize weird spaces too
    return str(x).replace("\u00a0", " ").strip().lower()


def is_blank(x: Optional[str]) -> bool:
    return normalize_key(x) == ""


def is_na_value(x: Optional[str]) -> bool:
    return normalize_key(x) in ("n/a", "na")


def split_table_field(tf: str) -> Tuple[str, str]:
    s = (tf or "").replace("\u00a0", " ").strip()
    if "." in s:
        table, field = s.split(".", 1)  # FIRST dot
        return table.strip(), field.strip()
    return s.strip(), ""


def canonical_table_name(name: str) -> str:
    # match Chemistry SampleInfo vs Chemistry_SampleInfo, collapse spaces
    s = normalize_key(name)
    s = s.replace("_", " ")
    s = re.sub(r"\s+", " ", s)
    return s


ONE_TO_ONE_TABLE_SET = {canonical_table_name(x) for x in ONE_TO_ONE_TABLE_NAMES}


def is_in_one_to_one_list(old_table_field: str) -> bool:
    table, _ = split_table_field(old_table_field)
    return canonical_table_name(table) in ONE_TO_ONE_TABLE_SET


def header_canon(h: str) -> str:
    """
    Canonicalize header names so small variations still match.
    Removes all non-alphanumerics.
    Example: "1:1 Transfer Status" -> "11transferstatus"
    """
    s = normalize_key(h)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def find_required_col(headers: List[str], desired_name: str, aliases: Optional[List[str]] = None) -> int:
    wanted = [desired_name] + (aliases or [])
    wanted_c = {header_canon(x) for x in wanted}

    for i, h in enumerate(headers):
        if header_canon(h) in wanted_c:
            return i

    # Fail fast: do NOT silently create a new empty "input" column
    raise RuntimeError(
        f"Required column not found: '{desired_name}'.\n"
        f"Headers present: {headers}\n"
        f"Tip: check for trailing spaces / slightly different names."
    )


def ensure_output_col(headers: List[str], rows: List[List[str]], desired_name: str, aliases: Optional[List[str]] = None) -> int:
    wanted = [desired_name] + (aliases or [])
    wanted_c = {header_canon(x) for x in wanted}

    for i, h in enumerate(headers):
        if header_canon(h) in wanted_c:
            return i

    # create new output column
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


def load_ocotillo_set(csv_path: Path) -> set:
    if not csv_path.exists():
        sys.exit(f"{csv_path} not found. Export it from Postgres first.")
    df = pd.read_csv(csv_path, dtype=str)
    if OCOTILLO_CSV_COL not in df.columns:
        sys.exit(f"{csv_path} must have a '{OCOTILLO_CSV_COL}' column.")
    return set(df[OCOTILLO_CSV_COL].dropna().map(normalize_key))


def main():
    oc_set = load_ocotillo_set(Path(OCOTILLO_CSV_PATH))
    print(f"Loaded {len(oc_set)} Ocotillo <table>.<field> entries from {OCOTILLO_CSV_PATH}.")

    service = get_sheets_service()
    resp = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_NAME}'!A1:Z"
    ).execute()

    values = resp.get("values", [])
    if not values:
        sys.exit(f"Sheet '{SHEET_NAME}' is empty or not found.")

    headers = values[0]
    data_rows = values[1:]



    # pad rows to header length
    for r in data_rows:
        while len(r) < len(headers):
            r.append("")

    # REQUIRED inputs: must exist
    idx_old = find_required_col(headers, COL_OLD, aliases=["Old Schema", "Old TableField"])
    idx_new = find_required_col(headers, COL_NEW, aliases=["New Target Schema", "New Schema"])

    # OUTPUT cols: create if missing
    idx_map = ensure_output_col(headers, data_rows, COL_MAP)
    idx_path = ensure_output_col(headers, data_rows, COL_PATH)
    idx_xfer = ensure_output_col(headers, data_rows, COL_XFER)
    idx_dest = ensure_output_col(headers, data_rows, COL_DEST)
    idx_ref = ensure_output_col(headers, data_rows, COL_REFACTOR)

    if DEBUG_PRINT_SAMPLES:
        print("Detected column indexes:")
        print("  old:", idx_old, "->", headers[idx_old])
        print("  new:", idx_new, "->", headers[idx_new])
        # print a few sample old values so we can see if it's reading right
        print("Sample old values (first 10 nonblank):")
        shown = 0
        for r in data_rows:
            v = r[idx_old]
            if normalize_key(v):
                print(" ", v)
                shown += 1
            if shown >= 10:
                break

    # Collect unique (table -> columns) from your sheet's Destination column
    table_to_cols = defaultdict(set)
    for r in data_rows:
        dest = r[idx_dest] if idx_dest < len(r) else ""
        if is_skip_destination(dest):
            continue
        try:
            t, c = parse_table_field(dest)
            table_to_cols[t].add(c)
        except ValueError:
            continue

    nonnull_lookup = {}  # (table, col) -> bool


    connector = Connector()
    conn = connector.connect(
        INSTANCE_CONNECTION_NAME,
        "pg8000",
        user=DB_USER,
        password=DB_PASS,
        db=DB_NAME,
        ip_type=IP_TYPE,
    )

    try:
        cur = conn.cursor()
        for t, cols_set in table_to_cols.items():
            cols = sorted(cols_set)

            t_sql = safe_ident(t)
            exprs = []
            for c in cols:
                c_sql = safe_ident(c)
                # EXISTS(SELECT 1 FROM "Table" WHERE "Field" IS NOT NULL LIMIT 1) AS "Field"
                exprs.append(
                    f"EXISTS(SELECT 1 FROM {t_sql} WHERE {c_sql} IS NOT NULL LIMIT 1) AS {c_sql}"
                )

            q = "SELECT " + ", ".join(exprs)
            try:
                cur.execute(q)
                row = cur.fetchone()  # tuple of booleans aligned with cols
                for c, has_val in zip(cols, row):
                    nonnull_lookup[(t.lower(), c.lower())] = bool(has_val)

            except Exception as e:
                print(f"DB check failed for table {t}: {e}")
                for c in cols:
                    nonnull_lookup[(t, c)] = False
    finally:
        conn.close()
        connector.close()

    updated_map, updated_path, updated_xfer, updated_dest, updated_ref, updated_new = [], [], [], [], [], []

    for r in data_rows:
        old_tf = r[idx_old] if idx_old < len(r) else ""
        new_tf = r[idx_new] if idx_new < len(r) else ""

        old_key = normalize_key(old_tf)
        new_key = normalize_key(new_tf)

        old_exists_in_oc = bool(old_key) and (old_key in oc_set)
        new_exists_in_oc = bool(new_key) and (new_key in oc_set)

        # start from existing values
        map_val = r[idx_map] if idx_map < len(r) else ""
        path_val = r[idx_path] if idx_path < len(r) else ""
        xfer_val = r[idx_xfer] if idx_xfer < len(r) else ""
        dest_val = r[idx_dest] if idx_dest < len(r) else ""
        ref_val = r[idx_ref] if idx_ref < len(r) else ""

        # --- 1:1 TABLES (EXACT MATCH ON TABLE NAME) ---
        if is_in_one_to_one_list(old_tf):
            # Force 1:1 path and DO NOT allow non-1:1 logic to overwrite it
            path_val = VAL_STAGE_THEN_REFACTOR

            if is_na_value(new_tf) and (not old_exists_in_oc):
                map_val = VAL_NA_UPPER
                xfer_val = VAL_NA_UPPER
                dest_val = VAL_NA_UPPER
                ref_val = VAL_NA_UPPER

            elif old_exists_in_oc and (is_blank(new_tf) or is_na_value(new_tf)):
                map_val = VAL_UNDEFINED
                xfer_val = VAL_COMPLETE
                dest_val = old_tf
                ref_val = VAL_INCOMPLETE

            elif old_exists_in_oc and (not is_blank(new_tf)) and (not is_na_value(new_tf)):
                map_val = VAL_DEFINED_MISSING
                xfer_val = VAL_COMPLETE
                dest_val = old_tf
                ref_val = VAL_INCOMPLETE

            elif (not old_exists_in_oc) and (not is_blank(new_tf)) and (not is_na_value(new_tf)):
                map_val = VAL_DEFINED_MISSING
                xfer_val = VAL_INCOMPLETE
                dest_val = ""
                ref_val = VAL_INCOMPLETE

            elif (not old_exists_in_oc) and is_blank(new_tf):
                map_val = VAL_UNDEFINED
                xfer_val = VAL_INCOMPLETE
                dest_val = ""
                ref_val = VAL_INCOMPLETE

            else:
                # conservative fallback
                if is_blank(ref_val):
                    ref_val = VAL_INCOMPLETE

            # --- If Destination differs from old schema and transfer is complete, mark refactor complete ---
            if normalize_key(dest_val) and normalize_key(dest_val) not in ("n/a", "na", "not being migrated"):
                if normalize_key(dest_val) != normalize_key(old_tf) and normalize_key(xfer_val) == "complete":
                    ref_val = "complete"

            xfer_val = apply_transfer_status_from_db(dest_val, xfer_val, nonnull_lookup)
            if normalize_key(map_val) == normalize_key(VAL_DEFINED_EXISTS):
                ref_val = VAL_COMPLETE

            if normalize_key(path_val) == normalize_key(VAL_NA_UPPER):
                new_tf = VAL_NOT_BEING_MIGRATED

            updated_map.append(map_val)
            updated_path.append(path_val)
            updated_xfer.append(xfer_val)
            updated_dest.append(dest_val)
            updated_ref.append(ref_val)
            updated_new.append(new_tf)

            continue  # <-- critical: guarantees non-1:1 logic never runs for 1:1 rows

            # --- NOT IN 1:1 LIST ---

        # --- NOT IN 1:1 LIST ---
        # --- NOT IN 1:1 LIST ---

        # 1) If Migration Path is N/A, force New Target Schema = not being migrated
        if normalize_key(path_val) == normalize_key(VAL_NA_UPPER):
            if normalize_key(new_tf) != normalize_key(VAL_NOT_BEING_MIGRATED):
                new_tf = VAL_NOT_BEING_MIGRATED
                # optional: why.append("path_na=>new_target_not_being_migrated")

        # 2) If New Target Schema is N/A (any variant), replace it
        if is_na_value(new_tf):
            new_tf = VAL_NOT_BEING_MIGRATED
            # optional: why.append("new_target_na=>not_being_migrated")

        # 3) If New Target Schema indicates "not being migrated", enforce statuses
        if normalize_key(new_tf) == normalize_key(VAL_NOT_BEING_MIGRATED):
            path_val = VAL_NA_UPPER
            map_val = VAL_NA_UPPER
            dest_val = VAL_NOT_BEING_MIGRATED
            xfer_val = VAL_NOT_BEING_MIGRATED
            ref_val = VAL_NOT_BEING_MIGRATED
            # optional: why.append("not_being_migrated=>na_statuses")

        # 4) ONLY AFTER N/A handling: blank -> NEEDS REVIEW + unknown/not started/blocked
        elif is_blank(new_tf):
            new_tf = VAL_NEEDS_REVIEW
            path_val = VAL_UNKNOWN
            map_val = VAL_NOT_STARTED
            dest_val = ""
            ref_val = VAL_INCOMPLETE
            xfer_val = VAL_BLOCKED
            # optional: why.append("blank_new_target=>needs_review_unknown_blocked")

        # 5) NEEDS REVIEW explicit (in case it's already set)
        elif normalize_key(new_tf) == normalize_key(VAL_NEEDS_REVIEW):
            path_val = VAL_UNKNOWN
            map_val = VAL_NOT_STARTED
            dest_val = ""
            ref_val = VAL_INCOMPLETE
            xfer_val = VAL_BLOCKED
            # optional: why.append("needs_review=>unknown_not_started_blocked")


        else:
            # new target provided (real value)
            if new_exists_in_oc:
                path_val = VAL_DIRECT_TO_FINAL
                map_val = VAL_DEFINED_EXISTS
                dest_val = new_tf
                if is_blank(xfer_val):
                    xfer_val = "check transfer script"
                if is_blank(ref_val):
                    ref_val = "check transfer script"
            else:
                map_val = VAL_DEFINED_MISSING
                dest_val = ""
                xfer_val = VAL_INCOMPLETE
                ref_val = VAL_INCOMPLETE

        # --- If Destination differs from old schema and transfer is complete, mark refactor complete ---
        if normalize_key(dest_val) and normalize_key(dest_val) not in ("n/a", "na", "not being migrated"):
            if normalize_key(dest_val) != normalize_key(old_tf) and normalize_key(xfer_val) == "complete":
                ref_val = "complete"

        xfer_val = apply_transfer_status_from_db(dest_val, xfer_val, nonnull_lookup)

        if normalize_key(map_val) == normalize_key(VAL_DEFINED_EXISTS):
            ref_val = VAL_COMPLETE

        if normalize_key(path_val) == normalize_key(VAL_NA_UPPER):
            new_tf = VAL_NOT_BEING_MIGRATED

        updated_new.append(new_tf)
        updated_map.append(map_val)
        updated_path.append(path_val)
        updated_xfer.append(xfer_val)
        updated_dest.append(dest_val)
        updated_ref.append(ref_val)

    # Write header row (in case output cols were added)
    last_col_letter = col_index_to_letter(len(headers) - 1)
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_NAME}'!A1:{last_col_letter}1",
        valueInputOption="RAW",
        body={"values": [headers]}
    ).execute()

    # Write updated columns back
    num_rows = len(data_rows) + 1
    updates = [
        (idx_map, COL_MAP, updated_map),
        (idx_path, COL_PATH, updated_path),
        (idx_xfer, COL_XFER, updated_xfer),
        (idx_dest, COL_DEST, updated_dest),
        (idx_ref, COL_REFACTOR, updated_ref),
        (idx_new, COL_NEW, updated_new),

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

    print(f"✓ Updated '{SHEET_NAME}'. (1:1 rows cannot be overwritten by non-1:1 logic.)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.exit(str(e))
    conn.close()
    connector.close()

