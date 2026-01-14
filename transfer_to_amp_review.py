#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Append NEW [NMAquifer_Table.Field, PointID, Error] rows to AMP_review!A:C
without overwriting existing content. Skips duplicates already present.

- Parses transfer metrics blocks under "PointID|Table|Field|Error"
- Extra error columns are appended to Error, then cleaned/normalized
- Only writes to columns A..C using Sheets 'append' API
"""

from pathlib import Path
import sys
import re
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ======= CONFIG — EDIT THESE =======
SERVICE_ACCOUNT_FILE = "transfermetrics_service_account.json"
SPREADSHEET_ID = "1iQzeKqRWHIKbnNptH_wRQEpJ_pt1rI00ax9d5BhDAhU"
SHEET_NAME = "AMP_review"
TRANSFER_METRICS_PATH = r"metrics_2025-12-05T14_47_30.csv"
# ===================================

HEADER_CANON = "pointid|table|field|error"

def get_sheets_service(sa_path: str):
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(sa_path, scopes=scopes)
    return build("sheets", "v4", credentials=creds)

def canon(s: str) -> str:
    return (s or "").strip().lower()

# ---------- Error text normalization ----------
_row_id_re = re.compile(r"\brow\.id\s*=\s*\d+,\s*", re.IGNORECASE)
_sensor_type_re = re.compile(
    r"key\s+error\s+adding\s+sensor_type\s*:\s*(?P<stype>[^,|]+?)\s*error\s*:\s*'?(?P=stype)'?",
    re.IGNORECASE,
)
_org_missing_re = re.compile(
    r'key\s*\(organization\)\s*=\s*\((?P<org>[^)]+)\)\s*is\s*not\s*present\s*in\s*table\s*"?"?lexicon_term"?"?\.',
    re.IGNORECASE,
)
_value_error_prefix_re = re.compile(r"^\s*value\s*error\s*[,:\-]\s*", re.IGNORECASE)

def clean_error(msg: str) -> str:
    """Normalize error messages per requested rules."""
    if not msg:
        return msg
    out = msg
    out = _row_id_re.sub("", out)
    m = _sensor_type_re.search(out)
    if m:
        stype = m.group("stype").strip()
        out = _sensor_type_re.sub(f"Invalid sensor_type: {stype}", out)
    m = _org_missing_re.search(out)
    if m:
        org = m.group("org").strip()
        out = _org_missing_re.sub(f"Invalid organization: {org}", out)
    out = _value_error_prefix_re.sub("", out)
    # remove straight & smart double quotes
    out = out.replace('"', '').replace('“', '').replace('”', '')
    # tidy whitespace / trailing punctuation
    out = re.sub(r"\s{2,}", " ", out).strip()
    out = re.sub(r"[\s\|,]+$", "", out)
    return out

def parse_amp_rows(path: Path):
    """
    Return list of rows: [NMAquifer_Table.Field, PointID, Error]
    Scans all header-delimited blocks; supports extra error columns.
    """
    if not path.exists():
        sys.exit(f"File not found: {path}")
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        lines = [ln.rstrip("\n") for ln in f]

    rows_out = []
    n, i = len(lines), 0
    while i < n:
        line = lines[i].strip()
        if line and canon(line) == HEADER_CANON:
            i += 1
            while i < n:
                raw = lines[i]
                s = raw.strip()
                if not s:
                    break
                if canon(s) == HEADER_CANON:
                    break
                parts = [p.strip() for p in raw.split("|")]
                if len(parts) >= 4:
                    point_id = parts[0]
                    table    = parts[1]
                    field    = parts[2]
                    error    = parts[3]
                    extra    = " | ".join(parts[4:]).strip() if len(parts) > 4 else ""
                    combined_error = error if not extra else f"{error} | {extra}"
                    combined_error = clean_error(combined_error)
                    nm_tf = f"{table}.{field}" if field else table
                    rows_out.append([nm_tf, point_id, combined_error])
                i += 1
            continue
        i += 1
    return rows_out

def ensure_tab(service, spreadsheet_id: str, tab_name: str):
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    if tab_name not in [s["properties"]["title"] for s in meta.get("sheets", [])]:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
        ).execute()

def load_existing_set(service, spreadsheet_id: str, tab_name: str):
    """Load existing A:C rows into a set of tuples for duplicate detection."""
    resp = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab_name}'!A1:C"
    ).execute()
    values = resp.get("values", [])
    if not values:
        return set(), 0
    # skip header if present
    start = 1 if values and [v.strip().lower() for v in values[0]] == \
        ["nmaquifer_table.field", "pointid", "error"] else 0
    existing = set()
    for row in values[start:]:
        a = row[0].strip() if len(row) > 0 else ""
        b = row[1].strip() if len(row) > 1 else ""
        c = row[2].strip() if len(row) > 2 else ""
        if a or b or c:
            existing.add((a, b, c))
    return existing, len(values)

def main():
    service = get_sheets_service(SERVICE_ACCOUNT_FILE)
    ensure_tab(service, SPREADSHEET_ID, SHEET_NAME)

    # 1) Parse new rows from transfer metrics
    new_rows = parse_amp_rows(Path(TRANSFER_METRICS_PATH))
    # Deduplicate within incoming batch
    seen_batch = set()
    unique_new = []
    for r in new_rows:
        key = (r[0].strip(), r[1].strip(), r[2].strip())
        if key not in seen_batch:
            seen_batch.add(key)
            unique_new.append(r)

    # 2) Load existing rows from A:C to avoid duplicates
    existing_set, current_height = load_existing_set(service, SPREADSHEET_ID, SHEET_NAME)

    # 3) Filter to only truly new rows
    to_append = [r for r in unique_new if (r[0].strip(), r[1].strip(), r[2].strip()) not in existing_set]

    if not to_append:
        print("[info] No new rows to append. Nothing changed.")
        return

    # 4) If sheet is empty, include header first; otherwise just append rows
    # Check if there's any data at all (height == 0 or 1 with no header)
    need_header = (current_height == 0) or (current_height == 1 and len(existing_set) == 0)
    values = []
    if need_header:
        values.append(["NMAquifer_Table.Field", "PointID", "Error"])
    values.extend(to_append)

    # 5) Append to A:C only (no overwrite of other columns)
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_NAME}'!A:C",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": values}
    ).execute()

    print(f"[done] Appended {len(to_append)} new row(s) to {SHEET_NAME}!A:C (kept existing content).")

if __name__ == "__main__":
    main()
