"""
Well Inventory Template — Annotated Error Sheet
================================================
Creates a new tab "Template (Annotated)" in the well inventory Google Sheet that:

  1. Adds a NEW row 1 with field requirements (format, allowed values, etc.)
  2. Keeps the original metadata rows (required/optional, type) as rows 2 & 3
  3. Copies all data rows starting at row 4
  4. Highlights every problem cell RED (orange for validator bugs)
  5. Adds a hover note to each highlighted cell explaining the error

Usage
-----
    python annotate_template.py

    # Dry-run against local TSVs (no Sheets write):
    python annotate_template.py --local
"""

import argparse
import csv
import re
from collections import defaultdict

from google.oauth2 import service_account
from googleapiclient.discovery import build

SPREADSHEET_ID   = "1m0kWV5jrYu023KFiwlQb4tc9QK-caH_vACRLTdA6wf8"
CREDENTIALS_FILE = "well_inventory_service_account.json"
SCOPES           = ["https://www.googleapis.com/auth/spreadsheets"]

SHEET_TEMPLATE   = "Template"
SHEET_VALIDATION = "validation"
SHEET_OUTPUT     = "Template (Annotated)"

LOCAL_TEMPLATE   = "mfichera_well_inventory_template_-_Template.tsv"
LOCAL_VALIDATION = "mfichera_well_inventory_template_-_validation.tsv"

# ── FIELD REQUIREMENTS ROW ─────────────────────────────────────────────────────

FIELD_REQUIREMENTS = {
    "project":
        "Required. Text.",
    "well_name_point_id":
        "Optional but strongly recommended. Text. NMA PointID (e.g. RA-027). "
        "Must be unique. Placeholders like WL-xxxx are not valid.",
    "site_name":
        "Optional. Text. Informal site name.",
    "date_time":
        "Required. Datetime. Format: YYYY-MM-DDTHH:MM:SS — NO timezone offset. "
        "Example: 2025-06-11T14:15:00",
    "field_staff":
        "Required. Enum. Primary field staff name.",
    "field_staff_2":
        "Optional. Enum.",
    "field_staff_3":
        "Optional. Enum.",
    "contact_1_name":
        "Required if no contact_1_organization. Text. Full name.",
    "contact_1_organization":
        "Required if no contact_1_name. Text. Organization name.",
    "contact_1_role":
        "Required. Enum. Allowed: Unknown | Principal Investigator | Owner | Manager | "
        "Operator | Driller | Geologist | Hydrologist | Hydrogeologist | Engineer | "
        "Organization | Specialist | Technician | Research Assistant | Research Scientist | "
        "Graduate Student | Biologist | Lab Manager | Publications Manager | Software Developer",
    "contact_1_type":
        "Required. Enum.",
    "contact_1_phone_1":
        "Optional. Phone. Digits and hyphens only — NO names or notes in parentheses. "
        "Example: 505-555-1234",
    "contact_1_phone_1_type":
        "Required if phone_1 provided. Enum.",
    "contact_1_phone_2":
        "Optional. Phone. Digits and hyphens only.",
    "contact_1_phone_2_type":
        "Optional. Enum.",
    "utm_easting":
        "Required. Float. UTM easting in METERS (Zone 13N). "
        "Do NOT enter lat/long. Convert at: ngs.noaa.gov/NCAT",
    "utm_northing":
        "Required. Float. UTM northing in METERS (Zone 13N).",
    "utm_zone":
        "Required. Integer. For New Mexico: 13.",
    "elevation_ft":
        "Optional. Float. Elevation in feet above sea level. Numbers only.",
    "elevation_method":
        "Optional. Enum. Allowed: Altimeter | Differentially corrected GPS | Survey-grade GPS | "
        "Global positioning system (GPS) | LiDAR DEM | Level or other survey method | "
        "Interpolated from topographic map | Interpolated from digital elevation model (DEM) | "
        "Reported | Survey-grade Global Navigation Satellite Sys, Lvl1 | "
        "USGS National Elevation Dataset (NED) | Unknown",
    "date_drilled":
        "Optional. Date. Format: YYYY-MM-DD. "
        "Year only? Use YYYY-01-01 + note in well_notes. "
        "Month/year? Use YYYY-MM-01. No text descriptions.",
    "total_well_depth_ft":
        "Optional. Float. Numbers only.",
    "historic_depth_to_water_ft":
        "Optional. Float. Numbers only — no appended notes.",
    "measuring_point_height_ft":
        "Optional. Float. Numbers only.",
    "monitoring_frequency":
        "Optional. Enum. Allowed: Monthly | Bimonthly | Bimonthly reported | "
        "Quarterly | Biannual | Annual | Decadal | Event-based",
    "depth_to_water_ft":
        "Optional. Float. Numbers only.",
    "mp_height":
        "Optional. Float. Measuring point height in feet. Numbers only.",
    "well_notes":
        "Optional. Text. Use for date approximation notes, data quality context, etc.",
}

RED    = {"red": 1.0,  "green": 0.75, "blue": 0.75}
ORANGE = {"red": 1.0,  "green": 0.88, "blue": 0.60}
BLUE_H = {"red": 0.18, "green": 0.37, "blue": 0.67}
WHITE  = {"red": 1.0,  "green": 1.0,  "blue": 1.0}
GRAY   = {"red": 0.88, "green": 0.88, "blue": 0.88}

BUG_ERROR = "TypeError: '>' not supported between instances of 'NoneType' and 'datetime.date'"
BUG_FIELDS = {"date_drilled", "measurement_date_time", "date_time"}


def load_local(template_path, validation_path):
    with open(template_path, encoding="utf-8-sig") as f:
        all_rows = list(csv.DictReader(f, delimiter='\t'))
    headers = list(all_rows[0].keys())

    with open(validation_path, encoding="utf-8-sig") as f:
        val_rows = list(csv.DictReader(f, delimiter='\t'))

    error_map = defaultdict(list)
    for r in val_rows:
        field = r['field']
        if field == 'unknown':
            field = r.get('context_key') or 'unknown'
        row_num = int(r['row'])
        val = r.get('context_value') or r.get('value', '')
        note = f"ERROR: {r['error']}" + (f"\nValue: {val}" if val else "")
        error_map[(row_num, field)].append(note)

    return headers, all_rows, error_map


def load_sheets(service):
    def fetch(name):
        res = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{name}'!A1:CZ2000"
        ).execute()
        return res.get('values', [])

    t_vals = fetch(SHEET_TEMPLATE)
    v_vals = fetch(SHEET_VALIDATION)

    headers = t_vals[0]
    all_rows = []
    for row in t_vals[1:]:
        padded = row + [''] * (len(headers) - len(row))
        all_rows.append(dict(zip(headers, padded)))

    v_headers = v_vals[0] if v_vals else []
    error_map = defaultdict(list)
    for row in v_vals[1:]:
        padded = row + [''] * (len(v_headers) - len(row))
        r = dict(zip(v_headers, padded))
        field = r.get('field', '')
        if field == 'unknown':
            field = r.get('context_key') or 'unknown'
        try:
            row_num = int(r.get('row', 0))
        except ValueError:
            continue
        val = r.get('context_value') or r.get('value', '')
        note = f"ERROR: {r.get('error','')}" + (f"\nValue: {val}" if val else "")
        error_map[(row_num, field)].append(note)

    return headers, all_rows, error_map


def build_output_rows(headers, all_rows):
    req_row  = [FIELD_REQUIREMENTS.get(h, '') for h in headers]
    hdr_row  = list(headers)                                      # original column headers
    meta_req = [all_rows[0].get(h, '') for h in headers]         # required/optional row
    meta_typ = [all_rows[1].get(h, '') for h in headers]         # type row
    data     = [[r.get(h, '') for h in headers] for r in all_rows[2:]]
    return [req_row, hdr_row, meta_req, meta_typ] + data


def get_or_create_sheet(service, title):
    ss = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    for sheet in ss.get("sheets", []):
        if sheet["properties"]["title"] == title:
            return sheet["properties"]["sheetId"]
    resp = service.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": title}}}]}
    ).execute()
    return resp["replies"][0]["addSheet"]["properties"]["sheetId"]


def write_values(service, rows):
    service.spreadsheets().values().clear(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_OUTPUT}'!A1:CZ5000"
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_OUTPUT}'!A1",
        valueInputOption="RAW",
        body={"values": rows},
    ).execute()


def apply_formatting(service, sheet_id, headers, all_rows, error_map):
    num_cols = len(headers)
    col_idx  = {h: i for i, h in enumerate(headers)}

    # output row index = csv_row_num - 1
    # (csv row 2 = original row 0 = output index 1, shifted +1 by new req row)

    requests = [
        # Freeze top 3 rows and first column
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 4, "frozenColumnCount": 1},
                },
                "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
            }
        },
        # Requirements row — blue bg, white bold text, wrap
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                          "startColumnIndex": 0, "endColumnIndex": num_cols},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": BLUE_H,
                    "textFormat": {"bold": True, "foregroundColor": WHITE, "fontSize": 8},
                    "wrapStrategy": "WRAP",
                    "verticalAlignment": "TOP",
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat,wrapStrategy,verticalAlignment)",
            }
        },
        # Requirements row height
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "ROWS",
                          "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 130},
                "fields": "pixelSize",
            }
        },
        # Metadata rows (header, required/optional, type) — gray, bold
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 4,
                          "startColumnIndex": 0, "endColumnIndex": num_cols},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": GRAY,
                    "textFormat": {"bold": True, "fontSize": 8},
                    "wrapStrategy": "CLIP",
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat,wrapStrategy)",
            }
        },
        # Data rows — wrap, small font
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 4,
                          "startColumnIndex": 0, "endColumnIndex": num_cols},
                "cell": {"userEnteredFormat": {
                    "wrapStrategy": "WRAP",
                    "verticalAlignment": "TOP",
                    "textFormat": {"fontSize": 9},
                }},
                "fields": "userEnteredFormat(wrapStrategy,verticalAlignment,textFormat)",
            }
        },
    ]

    # Column widths
    wide = {"well_name_point_id", "site_name", "elevation_method", "monitoring_frequency",
            "date_drilled", "well_notes", "well_measuring_notes", "water_notes",
            "directions_to_site", "specific_location_of_well", "measuring_point_description",
            "contact_special_requests_notes"}
    for i, h in enumerate(headers):
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": 200 if h in wide else 110},
                "fields": "pixelSize",
            }
        })

    # Error cell highlights
    note_requests = []
    highlighted = 0
    for (csv_row_num, field), notes in error_map.items():
        ci = col_idx.get(field)
        if ci is None:
            continue
        # Skip metadata rows (header, required/optional, type) — not real data
        if csv_row_num <= 3:
            continue
        out_row = csv_row_num + 2

        is_bug  = field in BUG_FIELDS and any(BUG_ERROR in n for n in notes)
        color   = ORANGE if is_bug else RED

        requests.append({
            "repeatCell": {
                "range": {"sheetId": sheet_id,
                          "startRowIndex": out_row, "endRowIndex": out_row + 1,
                          "startColumnIndex": ci, "endColumnIndex": ci + 1},
                "cell": {"userEnteredFormat": {"backgroundColor": color}},
                "fields": "userEnteredFormat(backgroundColor)",
            }
        })

        combined = "\n\n".join(notes)
        if is_bug:
            combined = "VALIDATOR BUG (not your data):\n" + combined
        note_requests.append({
            "updateCells": {
                "range": {"sheetId": sheet_id,
                          "startRowIndex": out_row, "endRowIndex": out_row + 1,
                          "startColumnIndex": ci, "endColumnIndex": ci + 1},
                "rows": [{"values": [{"note": combined}]}],
                "fields": "note",
            }
        })
        highlighted += 1

    service.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID, body={"requests": requests}
    ).execute()
    print(f"  ✅ Formatting applied to {highlighted} error cells.")

    # Notes in chunks of 100
    for i in range(0, len(note_requests), 100):
        service.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": note_requests[i:i+100]}
        ).execute()
    print(f"  ✅ {len(note_requests)} cell notes added.")


def dry_run(headers, all_rows, error_map):
    by_field = defaultdict(list)
    for (row_num, field), notes in sorted(error_map.items()):
        pid = all_rows[row_num - 2].get('well_name_point_id', f'row {row_num}') if row_num - 2 < len(all_rows) else f'row {row_num}'
        by_field[field].append((row_num, pid, notes))
    print("\n=== DRY RUN ===\n")
    for field, items in sorted(by_field.items(), key=lambda x: -len(x[1])):
        print(f"  {field} — {len(items)} cells")
        for row_num, pid, notes in items[:2]:
            print(f"    row {row_num} ({pid}): {notes[0][:90]}")
    print(f"\nTotal error cells: {len(error_map)}")


def get_service():
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_FILE, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true")
    args = parser.parse_args()

    if args.local:
        print("📂 Local mode…")
        headers, all_rows, error_map = load_local(LOCAL_TEMPLATE, LOCAL_VALIDATION)
        dry_run(headers, all_rows, error_map)
        return

    print("🔗 Connecting to Google Sheets…")
    service = get_service()

    print("📂 Loading data…")
    headers, all_rows, error_map = load_sheets(service)
    print(f"   {len(headers)} columns | {len(all_rows)} rows | {len(error_map)} error cells")

    print(f"\n📋 Writing '{SHEET_OUTPUT}'…")
    sheet_id = get_or_create_sheet(service, SHEET_OUTPUT)
    output_rows = build_output_rows(headers, all_rows)
    write_values(service, output_rows)
    print(f"  ✅ {len(output_rows)} rows written.")

    print("\n🎨 Formatting…")
    apply_formatting(service, sheet_id, headers, all_rows, error_map)

    print(f"\n✨ Done — open the '{SHEET_OUTPUT}' tab.")


if __name__ == "__main__":
    main()
