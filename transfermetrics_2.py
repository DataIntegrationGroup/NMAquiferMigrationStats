#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Update 'Issues' counts in an existing Google Sheet based on a pipe-delimited
transfer metrics CSV with header: PointID|Table|Field|Error

- Counts occurrences per Table.Field (Field may be blank → label is just Table)
- In spreadsheet ID 151t3h5CWmPU0k2xJcXbwFPBzYvP60bbZOk_J84N2uVs,
  sheet 'FieldPairs_Checked', finds column 'NMAquifer_TableField'
- Writes counts to column 'Issues' (creates if missing), aligning by row
"""

from pathlib import Path
import sys

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ================== CONFIG — EDIT THESE ==================
SERVICE_ACCOUNT_FILE = "service_account.json"
SPREADSHEET_ID = "1NtkaSWh8COQpMXd9AZ-fXMsRok9l-wwC1sz0lgVCTeo"
SHEET_NAME = "FieldPairs_Checked"
CSV_PATH = r"transfer_metrics_logs/transfer_metrics_metrics_2025-11-13T13_07_31.csv"  # pipe-delimited file
# =========================================================


def get_sheets_service(sa_path: str):
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(sa_path, scopes=scopes)
    return build("sheets", "v4", credentials=creds)


def robust_read_counts(csv_path: Path) -> dict:
    """
    Read the file line-by-line and split each line into exactly 4 parts:
    PointID | Table | Field | Error(with possible extra pipes)
    Return dict: normalized_label -> count
    """
    if not csv_path.exists():
        sys.exit(f"File not found: {csv_path}")

    with open(csv_path, "r", encoding="utf-8-sig", errors="replace") as f:
        lines = [ln.rstrip("\n") for ln in f.readlines()]

    if not lines:
        return {}

    # Header = first non-empty line
    header = None
    start_idx = 0
    for i, ln in enumerate(lines):
        if ln.strip():
            header = [h.strip() for h in ln.split("|", 3)]
            start_idx = i + 1
            break
    if header is None:
        return {}

    # Indices for columns (case-insensitive)
    hmap = {h.lower(): idx for idx, h in enumerate(header)}
    for need in ("pointid", "table", "field", "error"):
        if need not in hmap:
            sys.exit(f"Transfer metrics missing required header '{need}'. Found: {header}")

    ti, fi = hmap["table"], hmap["field"]

    def norm(s: str) -> str:
        return (s or "").strip().lower()

    counts = {}
    for ln in lines[start_idx:]:
        if not ln.strip():
            continue
        parts = ln.split("|", 3)
        if len(parts) < 4:
            parts += [""] * (4 - len(parts))
        parts = [p.strip() for p in parts]
        table = parts[ti]
        field = parts[fi]
        label = f"{table}.{field}" if field else table
        key = norm(label)
        counts[key] = counts.get(key, 0) + 1

    return counts


def col_index_to_letter(idx: int) -> str:
    """0-based index to A1 letter(s)."""
    idx += 1
    letters = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def main():
    # 1) Build counts from CSV
    counts = robust_read_counts(Path(CSV_PATH))

    # 2) Connect to Sheets and read the target sheet
    service = get_sheets_service(SERVICE_ACCOUNT_FILE)

    resp = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_NAME}'!A1:ZZ"
    ).execute()
    values = resp.get("values", [])
    if not values:
        sys.exit(f"Sheet '{SHEET_NAME}' is empty or not found in spreadsheet {SPREADSHEET_ID}.")

    headers = values[0]
    data_rows = values[1:]

    # Find 'NMAquifer_TableField'
    if "NMAquifer_TableField" not in headers:
        sys.exit(f"'NMAquifer_TableField' column not found. Headers present: {headers}")

    nm_idx = headers.index("NMAquifer_TableField")

    # Find or create 'Issues' column
    if "Issues" in headers:
        issues_idx = headers.index("Issues")
    else:
        issues_idx = len(headers)  # append at end
        headers.append("Issues")
        if len(values[0]) < len(headers):
            values[0] = values[0] + [""] * (len(headers) - len(values[0]))

    # 3) Build the 'Issues' column values, aligned per row
    def norm(s: str) -> str:
        return (s or "").strip().lower()

    issues_vals = []
    for row in data_rows:
        nm_val = row[nm_idx] if len(row) > nm_idx else ""
        key = norm(nm_val)
        cnt = counts.get(key, 0)
        issues_vals.append([int(cnt)])  # single-cell row (as list of list)

    # 4) Write ONLY the Issues column (header + data)
    col_letter = col_index_to_letter(issues_idx)
    total_rows = len(values)  # includes header
    col_payload = [["Issues"]] + issues_vals  # header + N rows

    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_NAME}'!{col_letter}1:{col_letter}{total_rows}",
        valueInputOption="RAW",
        body={"values": col_payload}
    ).execute()

    print(f"✓ Wrote Issues for {len(issues_vals)} rows to '{SHEET_NAME}' ({col_letter} column).")


if __name__ == "__main__":
    main()
