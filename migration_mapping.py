"""
Reads the Google Sheet tab "FieldPairs_Checked", pivots two columns, and writes the result to a new sheet.

Spec:
- Unique values in column "Ocotillo_TableField" -> become column headers in new sheet
- Under each header, list the corresponding "NMAquifer_TableField" values as rows
- New sheet name: "testing_mar"
- Spreadsheet ID: 1NtkaSWh8COQpMXd9AZ-fXMsRok9l-wwC1sz0lgVCTeo
"""

from collections import defaultdict
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SPREADSHEET_ID = "1NtkaSWh8COQpMXd9AZ-fXMsRok9l-wwC1sz0lgVCTeo"
SOURCE_SHEET_NAME = "FieldPairs_Checked"
NEW_SHEET_NAME = "testing_mar"

# Path to your service account key JSON
SERVICE_ACCOUNT_FILE = "service_account.json"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def find_col_indices(header_row, required_cols):
    header_map = {str(name).strip(): i for i, name in enumerate(header_row)}
    missing = [c for c in required_cols if c not in header_map]
    if missing:
        raise KeyError(
            f"Missing required columns: {missing}\n"
            f"Found headers: {list(header_map.keys())}"
        )
    return {c: header_map[c] for c in required_cols}


def ensure_sheet_exists(service, spreadsheet_id: str, sheet_name: str) -> int:
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for s in meta.get("sheets", []):
        props = s.get("properties", {})
        if props.get("title") == sheet_name:
            return props["sheetId"]

    resp = service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]},
    ).execute()

    return resp["replies"][0]["addSheet"]["properties"]["sheetId"]


def clear_sheet(service, spreadsheet_id: str, sheet_name: str):
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=f"'{sheet_name}'!A:ZZ",
        body={},
    ).execute()


def main():
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    service = build("sheets", "v4", credentials=creds)

    # Read data from the specified tab
    values_resp = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SOURCE_SHEET_NAME}'!A:ZZ"
    ).execute()

    values = values_resp.get("values", [])
    if not values:
        raise RuntimeError(f"No data found in source sheet '{SOURCE_SHEET_NAME}'.")

    header = values[0]
    col_idx = find_col_indices(header, ["Ocotillo_TableField", "NMAquifer_TableField"])
    o_idx = col_idx["Ocotillo_TableField"]
    n_idx = col_idx["NMAquifer_TableField"]

    # Build mapping: Ocotillo -> list of NMAquifer values (deduped per Ocotillo, preserves first-seen order)
    mapping = defaultdict(list)
    seen_per_key = defaultdict(set)

    for row in values[1:]:
        ocotillo = row[o_idx].strip() if o_idx < len(row) and str(row[o_idx]).strip() else ""
        nma = row[n_idx].strip() if n_idx < len(row) and str(row[n_idx]).strip() else ""
        if not ocotillo or not nma:
            continue

        if nma not in seen_per_key[ocotillo]:
            mapping[ocotillo].append(nma)
            seen_per_key[ocotillo].add(nma)

    # Preserve Ocotillo header order as it appears in the sheet
    headers = []
    seen_headers = set()
    for row in values[1:]:
        ocotillo = row[o_idx].strip() if o_idx < len(row) and str(row[o_idx]).strip() else ""
        if ocotillo and ocotillo in mapping and ocotillo not in seen_headers:
            headers.append(ocotillo)
            seen_headers.add(ocotillo)

    if not headers:
        raise RuntimeError(
            "No (Ocotillo_TableField, NMAquifer_TableField) pairs found. "
            "Check that both columns have values."
        )

    max_len = max(len(mapping[h]) for h in headers)

    output = [headers]
    for i in range(max_len):
        output.append([mapping[h][i] if i < len(mapping[h]) else "" for h in headers])

    # Create/clear target sheet, then write output
    ensure_sheet_exists(service, SPREADSHEET_ID, NEW_SHEET_NAME)
    clear_sheet(service, SPREADSHEET_ID, NEW_SHEET_NAME)

    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{NEW_SHEET_NAME}'!A1",
        valueInputOption="RAW",
        body={"values": output},
    ).execute()

    print(
        f"Done. Source='{SOURCE_SHEET_NAME}'. "
        f"Wrote {len(headers)} headers and {max_len} rows to '{NEW_SHEET_NAME}'."
    )


if __name__ == "__main__":
    main()
