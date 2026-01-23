#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Parse a pipe-delimited transfer metrics file with possible extra pipes in the Error field.
Columns: PointID|Table|Field|Error
- Robustly split each line with split('|', 3)
- Group by Table+Field
- Wide layout: each column = Table.Field; row2 = count; rows3+ = PointIDs
- Write to existing Google Sheet / tab
"""

from pathlib import Path
import sys
import pandas as pd
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ================== CONFIG — EDIT THESE ==================
SERVICE_ACCOUNT_FILE = "service_account.json"
SPREADSHEET_ID = "1NtkaSWh8COQpMXd9AZ-fXMsRok9l-wwC1sz0lgVCTeo"  # your sheet
TAB_NAME = "TableField_Issues"                                  # target tab
CSV_PATH = r"transfer_metrics_logs/transfer_metrics_metrics_2025-11-13T13_07_31.csv"  # your file
# =========================================================

REQUIRED_HEADERS = ["PointID", "Table", "Field", "Error"]

def get_sheets_service(sa_path: str):
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(sa_path, scopes=scopes)
    return build("sheets", "v4", credentials=creds)

def ensure_tab(service, spreadsheet_id: str, tab_name: str) -> int:
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    by_title = {s["properties"]["title"]: s["properties"] for s in meta.get("sheets", [])}
    if tab_name in by_title:
        return by_title[tab_name]["sheetId"]
    req = {"requests": [{"addSheet": {"properties": {"title": tab_name}}}]}
    resp = service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=req).execute()
    return resp["replies"][0]["addSheet"]["properties"]["sheetId"]

def clear_range(service, spreadsheet_id: str, tab_name: str, a1_range: str = "A:ZZ"):
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab_name}'!{a1_range}"
    ).execute()

def write_values(service, spreadsheet_id: str, tab_name: str, start_cell: str, values):
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab_name}'!{start_cell}",
        valueInputOption="RAW",
        body={"values": values}
    ).execute()

def robust_read_transfer_metrics(path: Path) -> pd.DataFrame:
    """Read the file line-by-line and split each line into exactly 4 fields:
       PointID | Table | Field | Error(with possible extra pipes)
    """
    rows = []
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        lines = f.read().splitlines()

    if not lines:
        sys.exit("CSV appears empty.")

    # Detect header (first non-empty line)
    header = None
    start_idx = 0
    for i, ln in enumerate(lines):
        if ln.strip():
            header = [h.strip() for h in ln.split("|", 3)]
            start_idx = i + 1
            break
    if header is None:
        sys.exit("No header found.")

    # Validate headers (allow minor case differences)
    header_map = {h.lower(): h for h in header}
    for need in REQUIRED_HEADERS:
        if need.lower() not in header_map:
            sys.exit(f"Missing required header '{need}'. Found headers: {header}")

    # Parse rows
    for ln in lines[start_idx:]:
        if not ln.strip():
            continue
        parts = ln.split("|", 3)
        # pad to 4 parts if short
        if len(parts) < 4:
            parts = parts + [""] * (4 - len(parts))
        # trim
        parts = [p.strip() for p in parts]
        row = dict(zip(header, parts))
        rows.append(row)

    df = pd.DataFrame(rows)
    # Normalize column names exactly to REQUIRED_HEADERS
    rename_map = {}
    lower_to_std = {h.lower(): h for h in REQUIRED_HEADERS}
    for col in df.columns:
        lc = col.lower()
        if lc in lower_to_std:
            rename_map[col] = lower_to_std[lc]
    df = df.rename(columns=rename_map)

    # Keep only expected columns
    df = df[REQUIRED_HEADERS]

    # Normalize strings
    for c in REQUIRED_HEADERS:
        df[c] = df[c].astype(str).fillna("").str.strip()

    return df

def build_wide_layout(df: pd.DataFrame) -> pd.DataFrame:
    # Label = Table.Field (or just Table if Field blank)
    labels = df.apply(lambda r: f"{r['Table']}.{r['Field']}" if r["Field"] else r["Table"], axis=1)

    # Group PointIDs by label
    grouped = {}
    for label, pid in zip(labels, df["PointID"]):
        grouped.setdefault(label, []).append(pid)

    # Sort labels
    items = sorted(grouped.items(), key=lambda kv: (kv[0] or ""))

    # Build columns: header (label), count, then PointIDs
    columns = {}
    max_len = 0
    for label, ids in items:
        ids_clean = [x for x in ids if x]
        col_vals = [label or "(unknown)", str(len(ids_clean))] + ids_clean
        columns[label or "(unknown)"] = col_vals
        max_len = max(max_len, len(col_vals))

    # Pad to equal lengths
    for k, v in columns.items():
        if len(v) < max_len:
            columns[k] = v + [""] * (max_len - len(v))

    return pd.DataFrame(columns)

def dataframe_to_2d_list(df: pd.DataFrame):
    return [df.columns.tolist()] + df.values.tolist()

def main():
    csv_path = Path(CSV_PATH)
    if not csv_path.exists():
        sys.exit(f"File not found: {csv_path}")

    df = robust_read_transfer_metrics(csv_path)
    wide = build_wide_layout(df)

    service = get_sheets_service(SERVICE_ACCOUNT_FILE)
    ensure_tab(service, SPREADSHEET_ID, TAB_NAME)
    clear_range(service, SPREADSHEET_ID, TAB_NAME, "A:ZZ")

    values = dataframe_to_2d_list(wide)
    write_values(service, SPREADSHEET_ID, TAB_NAME, "A1", values)

    print(f"Done. Wrote {wide.shape[1]} columns × {wide.shape[0]+1} rows to sheet {SPREADSHEET_ID}, tab '{TAB_NAME}'.")

if __name__ == "__main__":
    main()
