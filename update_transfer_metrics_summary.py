#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Parse transfer metrics blocks and write summaries to Google Sheets.

Patterns handled:
1) First block:
   model|input_count|cleaned_count|transferred|issue_percentage
   <values line>
   PointID|Table|Field|Error
   <data rows ...>

2) Subsequent blocks:
   <blank line>
   <values line>              (NO header)
   PointID|Table|Field|Error  (optional header line; skip if present)
   <data rows ...>

Writes rows to FieldPairs_Checked!G1 with headers:
  model | Table | input_count | cleaned_count | transferred | issue_percentage
"""

from pathlib import Path
import sys
import re

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from datetime import datetime, timezone
# Write last-run timestamp here (pick a cell outside your data table)
UPDATED_CELL = "A1"  # change to e.g. "H1" or "AA1" if A1 is your header row
UPDATED_PREFIX = "TRANSFER METRICS UPDATED:"

# ====== CONFIG: EDIT THESE ======
SERVICE_ACCOUNT_FILE = "service_account.json"
SHEET_NAME = "transfer_metrics"
SPREADSHEET_ID = "1NtkaSWh8COQpMXd9AZ-fXMsRok9l-wwC1sz0lgVCTeo"
TRANSFER_METRICS_PATH = r"transfer_metrics_logs/transfer_metrics_metrics_2026-01-23T07_38_02.csv"
# ===============================

SUMMARY_KEYS = ["model", "input_count", "cleaned_count", "transferred", "issue_percentage"]

def get_sheets_service(sa_path: str):
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(sa_path, scopes=scopes)
    return build("sheets", "v4", credentials=creds)

def is_summary_header(line: str) -> bool:
    parts = [p.strip().lower() for p in line.split("|")]
    return parts[:5] == SUMMARY_KEYS

def looks_like_values_line(line: str) -> bool:
    """Heuristic for a summary values line (no header)."""
    parts = [p.strip() for p in line.split("|", 4)]
    if len(parts) < 5:
        return False
    model, in_cnt, cl_cnt, trans, pct = parts[:5]
    def is_num(x): return bool(re.fullmatch(r"-?\d+(\.\d+)?", x or ""))
    numeric_ok = is_num(in_cnt) and is_num(cl_cnt) and is_num(trans)
    pct_ok = is_num(pct) or pct.endswith("%")
    return numeric_ok and pct_ok

def split5(line: str):
    parts = [p.strip() for p in line.split("|", 4)]
    if len(parts) < 5:
        parts += [""] * (5 - len(parts))
    return parts[:5]

def split_point_row(line: str):
    """Point row: PointID|Table|Field|Error(with extra pipes)."""
    parts = [p.strip() for p in line.split("|", 3)]
    if len(parts) < 4:
        parts += [""] * (4 - len(parts))
    return parts  # [PointID, Table, Field, Error...]

def parse_transfer_metrics_blocks(path: Path):
    if not path.exists():
        sys.exit(f"File not found: {path}")

    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        lines = [ln.rstrip("\n") for ln in f]

    out_rows = []
    n = len(lines)
    i = 0

    # --- FIRST BLOCK: allow explicit header ---
    while i < n and not lines[i].strip():
        i += 1

    if i < n and is_summary_header(lines[i]):
        # next non-empty line is the values line
        j = i + 1
        while j < n and not lines[j].strip():
            j += 1
        if j < n and looks_like_values_line(lines[j]):
            model, input_count, cleaned_count, transferred, issue_percentage = split5(lines[j])

            # infer Table from first data row after this values line
            table_val = ""
            k = j + 1
            while k < n:
                s = lines[k].strip()
                k += 1
                if not s:
                    continue
                if s.lower().startswith("pointid|table|field|error"):
                    continue
                pid, tbl, fld, _ = split_point_row(s)
                # works even if Field is blank; we only need Table (tbl)
                if pid.lower() == "pointid":
                    continue
                table_val = tbl
                break

            out_rows.append({
                "model": model,
                "Table": table_val,
                "input_count": input_count,
                "cleaned_count": cleaned_count,
                "transferred": transferred,
                "issue_percentage": issue_percentage,
            })
            i = j + 1
        else:
            i += 1  # header but missing values; move on

    # --- SUBSEQUENT BLOCKS: blank line then values line (no header) ---
    while i < n:
        # seek blank(s)
        while i < n and lines[i].strip():
            i += 1
        while i < n and not lines[i].strip():
            i += 1
        if i >= n:
            break

        if looks_like_values_line(lines[i]):
            model, input_count, cleaned_count, transferred, issue_percentage = split5(lines[i])

            # infer Table from first following data row
            table_val = ""
            k = i + 1
            while k < n:
                s = lines[k].strip()
                k += 1
                if not s:
                    continue
                if s.lower().startswith("pointid|table|field|error"):
                    continue
                if looks_like_values_line(s) or is_summary_header(s):
                    # next block encountered; no data rows here
                    k -= 1
                    break
                pid, tbl, fld, _ = split_point_row(s)
                if pid.lower() == "pointid":
                    continue
                table_val = tbl
                break

            out_rows.append({
                "model": model,
                "Table": table_val,
                "input_count": input_count,
                "cleaned_count": cleaned_count,
                "transferred": transferred,
                "issue_percentage": issue_percentage,
            })

            i = k
        else:
            i += 1

    return out_rows

def write_block_summary(service, spreadsheet_id: str, sheet_name: str, rows: list):
    """
    Write rows starting at G1 (columns G..L):
      model | Table | input_count | cleaned_count | transferred | issue_percentage
    """
    headers = ["model", "Table", "input_count", "cleaned_count", "transferred", "issue_percentage"]
    values = [headers] + [[r.get(h, "") for h in headers] for r in rows]
    end_row = len(values)  # header + data
    a1 = f"'{sheet_name}'!A2:F{end_row}"

    # Clear target area so old rows don't linger
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=f"'{sheet_name}'!A:F"
    ).execute()

    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_NAME}'!A2",  # <-- just the top-left cell
        valueInputOption="RAW",
        body={"values": values}  # 6 columns wide
    ).execute()

    # service.spreadsheets().values().update(
    #     spreadsheetId=spreadsheet_id,
    #     range=a1,
    #     valueInputOption="RAW",
    #     body={"values": values}
    # ).execute()



def main():
    rows = parse_transfer_metrics_blocks(Path(TRANSFER_METRICS_PATH))
    print(f"[info] Parsed {len(rows)} summary block(s).")
    if rows:
        print("[sample]", rows[0])

    service = get_sheets_service(SERVICE_ACCOUNT_FILE)
    write_block_summary(service, SPREADSHEET_ID, SHEET_NAME, rows)

    # --- write last-run timestamp (single cell, non-intrusive) ---
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_NAME}'!{UPDATED_CELL}",
        valueInputOption="RAW",
        body={"values": [[f"{UPDATED_PREFIX} {ts}"]]}
    ).execute()

    print("[done] Wrote summaries to FieldPairs_Checked!G1 (G..L).")

if __name__ == "__main__":
    main()
