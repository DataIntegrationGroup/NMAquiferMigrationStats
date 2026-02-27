"""
Well Inventory Template — Validation Error Triage
==================================================
Reads the Template and Validation tabs from the well inventory Google Sheet,
classifies errors into named error classes, and writes two output sheets:

  1. "Error Catalog"   — one row per error class with diagnosis and resolution
  2. "Row Tracking"    — one row per (error class, PointID/row) for AMP to work through

Usage
-----
    python well_inventory_triage.py

    # Or point at local TSV exports instead of hitting the API:
    python well_inventory_triage.py --local

Setup
-----
    pip install google-auth google-auth-httplib2 google-auth-oauthlib google-api-python-client

    Share the spreadsheet with your service account email (Editor role).
"""

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── CONFIGURATION ──────────────────────────────────────────────────────────────
SPREADSHEET_ID   = "1m0kWV5jrYu023KFiwlQb4tc9QK-caH_vACRLTdA6wf8"
CREDENTIALS_FILE = "well_inventory_service_account.json"
SCOPES           = ["https://www.googleapis.com/auth/spreadsheets"]

SHEET_TEMPLATE   = "Template"
SHEET_VALIDATION = "validation"
SHEET_CATALOG    = "Error Catalog"
SHEET_TRACKING   = "Row Tracking"

# Local TSV paths for --local mode
LOCAL_TEMPLATE   = "mfichera_well_inventory_template_-_Template.tsv"
LOCAL_VALIDATION = "mfichera_well_inventory_template_-_validation.tsv"


# ── ERROR CLASSES ──────────────────────────────────────────────────────────────

ERROR_CLASSES = [
    {
        "id": "BUG.validator.date_comparison_on_null",
        "match": lambda field, error, ctx_key, value: (
            "TypeError: '>' not supported between instances of 'NoneType' and 'datetime.date'" in error
        ),
        "fields": ["date_drilled", "measurement_date_time", "date_time"],
        "plain_english": (
            "The validator crashes with a Python TypeError when it tries to compare a blank date "
            "field against a date value. This is a validator bug — it should handle None gracefully "
            "instead of throwing an exception. This is NOT a data error on the user's part."
        ),
        "owner": "Dev",
        "owner_rationale": "The validator should handle None/blank dates without crashing. AMP cannot fix this.",
        "diagnose": (
            "1. Check whether the affected row's date field is blank (expected for optional fields).\n"
            "2. Confirm this appears on nearly every row regardless of other data quality.\n"
            "3. The validator is likely doing `if submitted_date > some_reference_date` without a None check."
        ),
        "resolution": (
            "Dev: Add a None guard before any date comparison in the validator.\n"
            "Example fix: `if date_drilled is not None and date_drilled > reference_date`\n"
            "NOT ALLOWED: Requiring users to enter a date in every row to avoid the crash."
        ),
        "amp_needs": "Nothing — this is a Dev fix. No data changes required.",
        "dev_needs": (
            "Fix the None comparison in the date validator. Add unit test: blank date field → "
            "no error; invalid date → clear error message."
        ),
        "disposition": "BLOCKER (Dev)",
        "priority": "HIGH — affects nearly every row, masks real errors",
    },
    {
        "id": "DATA.well_id.placeholder_not_filled",
        "match": lambda field, error, ctx_key, value: (
            field == "well_name_point_id" and
            error == "Duplicate value for well_name_point_id" and
            re.search(r"xxxx", value or "", re.IGNORECASE)
        ),
        "fields": ["well_name_point_id"],
        "plain_english": (
            "Rows still contain placeholder PointIDs like 'WL-xxxx' or 'SAC-xxxx' from the template. "
            "These aren't real duplicates — they're unfilled rows where the user hasn't entered the "
            "actual PointID yet."
        ),
        "owner": "AMP (data entry)",
        "owner_rationale": "The user who filled in the template needs to replace placeholders with real PointIDs.",
        "diagnose": (
            "1. Filter validation tab for field=well_name_point_id, value contains 'xxxx'.\n"
            "2. Open Template tab and find those rows.\n"
            "3. Determine if row has real data that needs a PointID, or if it's a truly blank/example row to delete."
        ),
        "resolution": (
            "Path 1 (AMP): Row has real data → enter the correct PointID for that well.\n"
            "Path 2 (AMP): Row is a blank example row → delete it from the template.\n"
            "NOT ALLOWED: Leaving 'xxxx' placeholders — they will be rejected on import."
        ),
        "amp_needs": "Correct PointID for each well, or confirmation that the row should be deleted.",
        "dev_needs": "N/A.",
        "disposition": "BLOCKER (AMP)",
        "priority": "HIGH — blocks import for all placeholder rows",
    },
    {
        "id": "DATA.well_id.missing_required",
        "match": lambda field, error, ctx_key, value: (
            field == "well_name_point_id" and error == "Field required"
        ),
        "fields": ["well_name_point_id"],
        "plain_english": (
            "Row has no PointID at all — the well_name_point_id field is completely blank. "
            "Every row must have a PointID to be imported."
        ),
        "owner": "AMP (data entry)",
        "owner_rationale": "PointID is the primary identifier — AMP must supply it or remove the row.",
        "diagnose": (
            "1. Find the row number in the Template tab.\n"
            "2. Check whether the row has any other data filled in.\n"
            "3. If the row is substantively blank, it can be deleted."
        ),
        "resolution": (
            "Path 1 (AMP): Row has real data → assign a PointID.\n"
            "Path 2 (AMP): Row is empty → delete it.\n"
            "NOT ALLOWED: Leaving blank PointID rows in the template."
        ),
        "amp_needs": "PointID for each affected row, or delete the row.",
        "dev_needs": "N/A.",
        "disposition": "BLOCKER (AMP)",
        "priority": "HIGH",
    },
    {
        "id": "DATA.well_id.blank_treated_as_duplicate",
        "match": lambda field, error, ctx_key, value: (
            field == "well_name_point_id" and
            error == "Duplicate value for well_name_point_id" and
            not re.search(r"xxxx", value or "", re.IGNORECASE)
        ),
        "fields": ["well_name_point_id"],
        "plain_english": (
            "The validator is flagging these rows as 'duplicates' of each other because they all "
            "share the same value: blank. This is not a true duplicate — it is the same missing "
            "PointID problem as DATA.well_id.missing_required, caught by a different validation rule. "
            "These rows have real site_name data (e.g. 'T2E (left floodplain)', 'Ellens Well') "
            "but no NMA PointID has been assigned yet. They need IDs before they can be imported."
        ),
        "owner": "AMP / Data Services",
        "owner_rationale": (
            "These appear to be a separate research project's wells not yet in the NMA system. "
            "Someone needs to assign PointIDs — either pull existing ones from NMA or register new wells."
        ),
        "diagnose": (
            "1. Check the site_name column for each affected row — these are real named wells.\n"
            "2. Search NMA / Ocotillo for each site_name to see if a PointID already exists.\n"
            "3. If the well exists in NMA → enter its PointID.\n"
            "4. If the well is new → it must be registered in Ocotillo first to get a PointID."
        ),
        "resolution": (
            "Path 1 (AMP): Well already exists in NMA → find its PointID and enter it.\n"
            "Path 2 (AMP + Data Services): Well is new to NMA → register it in Ocotillo to get a "
            "PointID, then come back and fill it in here.\n"
            "NOT ALLOWED: Leaving PointID blank or importing without a valid ID."
        ),
        "amp_needs": (
            "For each site_name: either the existing NMA PointID, or confirmation that this is a "
            "new well that needs to be registered."
        ),
        "dev_needs": (
            "Consider improving the validator error message to distinguish 'blank PointID' from "
            "'actual duplicate PointID' — the current message is misleading."
        ),
        "disposition": "BLOCKER (AMP)",
        "priority": "HIGH — 23 real wells with no PointID",
    },
    {
        "id": "DATA.elevation_method.invalid_enum",
        "match": lambda field, error, ctx_key, value: (
            field == "elevation_method" and "Input should be" in error
        ),
        "fields": ["elevation_method"],
        "plain_english": (
            "The elevation_method value doesn't match any of the allowed options. "
            "Common causes: free-text description entered instead of choosing from the dropdown, "
            "or a close-but-not-exact match (e.g. 'GPS' instead of 'Global positioning system (GPS)')."
        ),
        "owner": "AMP (data entry)",
        "owner_rationale": "User entered a non-standard value. Must be corrected to an allowed option.",
        "diagnose": (
            "1. Look up the entered value in the Template tab for that row.\n"
            "2. Compare against the allowed list below.\n"
            "3. Find the closest valid match."
        ),
        "resolution": (
            "AMP: Replace the entered value with one of the allowed options:\n"
            "  • Altimeter\n"
            "  • Differentially corrected GPS\n"
            "  • Survey-grade GPS\n"
            "  • Global positioning system (GPS)\n"
            "  • LiDAR DEM\n"
            "  • Level or other survey method\n"
            "  • Interpolated from topographic map\n"
            "  • Interpolated from digital elevation model (DEM)\n"
            "  • Reported\n"
            "  • Survey-grade Global Navigation Satellite Sys, Lvl1\n"
            "  • USGS National Elevation Dataset (NED)\n"
            "  • Unknown\n"
            "NOT ALLOWED: Leaving a free-text description."
        ),
        "amp_needs": "Select the correct value from the allowed list above.",
        "dev_needs": "Consider adding a dropdown in the template to prevent free-text entry.",
        "disposition": "BLOCKER (AMP)",
        "priority": "MEDIUM",
    },
    {
        "id": "DATA.numeric_field.non_numeric_value",
        "match": lambda field, error, ctx_key, value: (
            field in ("elevation_ft", "mp_height", "depth_to_water_ft",
                      "historic_depth_to_water_ft", "measuring_point_height_ft",
                      "total_well_depth_ft", "utm_easting", "utm_northing") and
            "Input should be a valid number" in error
        ),
        "fields": [
            "elevation_ft", "mp_height", "depth_to_water_ft",
            "historic_depth_to_water_ft", "measuring_point_height_ft",
            "total_well_depth_ft", "utm_easting", "utm_northing"
        ],
        "plain_english": (
            "A numeric field contains a non-numeric value. Common causes: blank field sent as empty "
            "string (should be null), text note appended to a number (e.g. '275 at time of drilling'), "
            "or lat/long entered in utm_easting/utm_northing instead of UTM coordinates."
        ),
        "owner": "AMP (data entry) / Dev",
        "owner_rationale": (
            "If blank → Dev should accept empty string as null. "
            "If text note → AMP must separate the note into the notes field. "
            "If lat/long in UTM field → AMP must convert or correct."
        ),
        "diagnose": (
            "1. Check the actual value in the Template tab for the flagged row and field.\n"
            "2. If blank → Dev fix (accept null).\n"
            "3. If text with a number embedded → AMP extracts the number, moves text to notes.\n"
            "4. If utm_easting/utm_northing contain 'Lat:'/'Long:' → AMP entered lat/long; needs UTM conversion."
        ),
        "resolution": (
            "Path 1 (Dev): Accept empty string as null for all optional numeric fields.\n"
            "Path 2 (AMP): Extract numeric value and move any text to the appropriate notes field.\n"
            "Path 3 (AMP — UTM fields): Convert lat/long coordinates to UTM Zone 13N.\n"
            "NOT ALLOWED: Entering text notes in numeric fields."
        ),
        "amp_needs": (
            "For text-embedded values: the numeric value only, with any notes moved to the well_notes field.\n"
            "For UTM fields: UTM easting and northing in meters (Zone 13N)."
        ),
        "dev_needs": "Treat empty string as null for all optional numeric fields.",
        "disposition": "BLOCKER (mixed — see diagnosis)",
        "priority": "MEDIUM",
    },
    {
        "id": "DATA.date_drilled.format_invalid",
        "match": lambda field, error, ctx_key, value: (
            field == "date_drilled" and (
                "input is too short" in error or
                "invalid character in year" in error
            )
        ),
        "fields": ["date_drilled"],
        "plain_english": (
            "date_drilled is in a format the validator can't parse. Common cases: year-only ('2007'), "
            "month/year ('08/2007'), partial dates ('Pre 1979'), text descriptions "
            "('Pit well dug ~30 years'), or ambiguous formats ('12/4/1970')."
        ),
        "owner": "AMP (data entry)",
        "owner_rationale": "AMP entered the date; it must be reformatted to YYYY-MM-DD or left blank with a note.",
        "diagnose": (
            "1. Check the actual value in the Template tab.\n"
            "2. Categorize: year-only, partial date, text description, or ambiguous format.\n"
            "3. For text descriptions, check if there's a known drill date in NMBGMR records."
        ),
        "resolution": (
            "Path 1 (AMP — known full date): Reformat to YYYY-MM-DD (e.g. '12/4/1970' → '1970-12-04').\n"
            "Path 2 (AMP — year only): Use Jan 1 of that year as a proxy: '2007' → '2007-01-01', "
            "and add note in well_notes: 'Drill date approximate: year only known'.\n"
            "Path 3 (AMP — partial month/year): Use first of month: '08/2007' → '2007-08-01', add note.\n"
            "Path 4 (AMP — text / completely unknown): Leave blank and add note in well_notes.\n"
            "NOT ALLOWED: Leaving unparseable text in the date_drilled field."
        ),
        "amp_needs": "YYYY-MM-DD formatted date, or blank field with explanation in well_notes.",
        "dev_needs": "Consider accepting year-only dates (YYYY) as valid with auto-conversion to YYYY-01-01.",
        "disposition": "BLOCKER (AMP)",
        "priority": "MEDIUM",
    },
    {
        "id": "DATA.date_drilled.year_only_integer",
        "match": lambda field, error, ctx_key, value: (
            field == "date_drilled" and
            "Datetimes provided to dates should have zero time" in error
        ),
        "fields": ["date_drilled"],
        "plain_english": (
            "A year-only value (e.g. '1999', '2004') was entered in date_drilled. "
            "The validator is interpreting it as a datetime with a time component rather than "
            "rejecting it outright — this is a near-miss that can likely be fixed by Dev "
            "auto-converting year integers to YYYY-01-01."
        ),
        "owner": "Dev (preferred) / AMP (fallback)",
        "owner_rationale": (
            "Dev can auto-convert bare year integers to Jan 1 of that year. "
            "If Dev cannot, AMP must reformat."
        ),
        "diagnose": (
            "1. Confirm the value is a 4-digit year integer with no other content.\n"
            "2. Check if this is a common pattern across many rows (it is — 8 occurrences)."
        ),
        "resolution": (
            "Path 1 (Dev — preferred): Auto-convert 4-digit integers to YYYY-01-01 and log a warning.\n"
            "Path 2 (AMP — fallback): Reformat to '1999-01-01' and add note in well_notes: "
            "'Drill date approximate: year only known'.\n"
            "NOT ALLOWED: Leaving bare year integers in the field."
        ),
        "amp_needs": "If Dev cannot fix: reformat each to YYYY-01-01.",
        "dev_needs": "Accept 4-digit year integers as YYYY-01-01 with a logged warning.",
        "disposition": "BLOCKER (Dev preferred)",
        "priority": "LOW — Dev fix covers all 8 occurrences at once",
    },
    {
        "id": "DATA.monitoring_frequency.invalid_enum",
        "match": lambda field, error, ctx_key, value: (
            field == "monitoring_frequency" and "Input should be" in error
        ),
        "fields": ["monitoring_frequency"],
        "plain_english": (
            "monitoring_frequency contains a free-text description instead of a controlled vocab value. "
            "Observed values: 'Monitoring complete', 'Annual water level'. These are descriptive notes "
            "that don't map to the allowed options."
        ),
        "owner": "AMP (data entry)",
        "owner_rationale": "User entered free text; must select from the allowed list.",
        "diagnose": (
            "1. Check the value in the Template tab.\n"
            "2. Map to the closest valid option, or blank + add note."
        ),
        "resolution": (
            "AMP: Replace with the closest allowed value:\n"
            "  • Monthly, Bimonthly, Bimonthly reported, Quarterly, Biannual, Annual, Decadal, Event-based\n"
            "'Annual water level' → 'Annual'\n"
            "'Monitoring complete' → leave blank or use most recent frequency + note in well_notes.\n"
            "NOT ALLOWED: Free-text descriptions in this field."
        ),
        "amp_needs": "Correct monitoring_frequency value from the allowed list, or blank + note.",
        "dev_needs": "Consider adding a dropdown to prevent free-text entry.",
        "disposition": "BLOCKER (AMP)",
        "priority": "MEDIUM",
    },
    {
        "id": "DATA.utm.lat_long_in_utm_field",
        "match": lambda field, error, ctx_key, value: (
            field in ("utm_easting", "utm_northing") and
            "Input should be a valid number" in error and
            re.search(r"(Lat:|Long:)", value or "")
        ),
        "fields": ["utm_easting", "utm_northing"],
        "plain_english": (
            "Latitude/longitude coordinates were entered in the UTM easting/northing fields. "
            "The template expects UTM coordinates in meters, not decimal degrees."
        ),
        "owner": "AMP (data entry)",
        "owner_rationale": "AMP entered the wrong coordinate format.",
        "diagnose": (
            "1. Note the Lat/Long values in the error's value field.\n"
            "2. Convert to UTM Zone 13N (standard for New Mexico).\n"
            "3. Verify the converted coordinates place the well in the expected location."
        ),
        "resolution": (
            "AMP: Convert lat/long to UTM Zone 13N using a GIS tool or online converter "
            "(e.g. https://www.ngs.noaa.gov/NCAT/).\n"
            "Enter: utm_easting in meters (e.g. 329456), utm_northing in meters (e.g. 3765432), "
            "utm_zone = 13.\n"
            "NOT ALLOWED: Leaving lat/long values in UTM fields."
        ),
        "amp_needs": "UTM easting, northing (meters), and zone for each affected well.",
        "dev_needs": "Consider detecting lat/long format and auto-converting, or give a clearer error message.",
        "disposition": "BLOCKER (AMP)",
        "priority": "MEDIUM",
    },
    {
        "id": "DATA.contact_role.invalid_enum",
        "match": lambda field, error, ctx_key, value: (
            field == "contact_1_role" and "Input should be" in error
        ),
        "fields": ["contact_1_role"],
        "plain_english": (
            "contact_1_role contains a non-standard value ('Owners'). "
            "The correct value is 'Owner' (no trailing s)."
        ),
        "owner": "AMP (data entry)",
        "owner_rationale": "Typo — 'Owners' vs 'Owner'.",
        "diagnose": "1. Check the value. 2. Replace 'Owners' with 'Owner'.",
        "resolution": (
            "AMP: Change 'Owners' to 'Owner'.\n"
            "Full allowed list: Unknown, Principal Investigator, Owner, Manager, Operator, "
            "Driller, Geologist, Hydrologist, Hydrogeologist, Engineer, Organization, Specialist, "
            "Technician, Research Assistant, Research Scientist, Graduate Student, Biologist, "
            "Lab Manager, Publications Manager, Software Developer."
        ),
        "amp_needs": "Correct contact_1_role value from the allowed list.",
        "dev_needs": "N/A.",
        "disposition": "BLOCKER (AMP)",
        "priority": "LOW — 1 row",
    },
    {
        "id": "DATA.phone.value_too_long",
        "match": lambda field, error, ctx_key, value: (
            "NumberParseException" in error and "too long to be a phone number" in error
        ),
        "fields": ["contact_1_phone_1", "contact_1_phone_2"],
        "plain_english": (
            "A phone number field contains extra text beyond the phone number itself "
            "(e.g. '505-927-5091 (Gloria)'). The validator expects a phone number only."
        ),
        "owner": "AMP (data entry)",
        "owner_rationale": "AMP appended a name to the phone number; the field only accepts the number.",
        "diagnose": (
            "1. Check the value — it likely has a name or note in parentheses after the number.\n"
            "2. The phone number itself is valid; just the suffix needs to be removed."
        ),
        "resolution": (
            "AMP: Remove any text after the phone number (e.g. '505-927-5091 (Gloria)' → '505-927-5091').\n"
            "If the contact name note is important, add it to contact_special_requests_notes.\n"
            "NOT ALLOWED: Any non-numeric text in phone fields."
        ),
        "amp_needs": "Phone number only, no appended names or notes.",
        "dev_needs": "N/A.",
        "disposition": "BLOCKER (AMP)",
        "priority": "LOW — 2 rows",
    },
    {
        "id": "DATA.date_time.timezone_aware",
        "match": lambda field, error, ctx_key, value: (
            field == "date_time" and "date_time must be a timezone-naive datetime" in error
        ),
        "fields": ["date_time"],
        "plain_english": (
            "The date_time field contains a timezone offset (e.g. '2025-02-15T10:30:00-08:00'). "
            "The system expects timezone-naive datetimes — strip the offset."
        ),
        "owner": "AMP (data entry)",
        "owner_rationale": "AMP included a timezone offset; the field expects local time only.",
        "diagnose": (
            "1. Check the value — it ends with a timezone offset like '-08:00' or 'Z'.\n"
            "2. Remove the offset; keep the datetime as-is."
        ),
        "resolution": (
            "AMP: Remove the timezone offset from the datetime value.\n"
            "Example: '2025-02-15T10:30:00-08:00' → '2025-02-15T10:30:00'\n"
            "NOT ALLOWED: Any timezone suffix in date_time."
        ),
        "amp_needs": "Timezone-naive datetime in ISO format: YYYY-MM-DDTHH:MM:SS.",
        "dev_needs": "N/A.",
        "disposition": "BLOCKER (AMP)",
        "priority": "LOW — 1 row",
    },
]


def classify_row(field: str, error: str, ctx_key: str, value: str) -> dict | None:
    for ec in ERROR_CLASSES:
        try:
            if ec["match"](field, error, ctx_key, value):
                return ec
        except Exception:
            continue
    return None


# ── PARSING ────────────────────────────────────────────────────────────────────

def parse_from_tsv(template_path: str, validation_path: str) -> tuple[dict, list]:
    """Parse from local TSV files. Returns (row_lookup, enriched_errors)."""
    # Build row -> point_id lookup from Template
    with open(template_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter='\t')
        all_rows = list(reader)

    row_lookup = {}
    for i, row in enumerate(all_rows):
        csv_row_num = i + 2  # header = row 1; first data row = row 2
        pid = row.get('well_name_point_id', '').strip()
        row_lookup[str(csv_row_num)] = pid if pid and pid not in ('optional', 'text') else f"(row {csv_row_num})"

    # Load and enrich validation errors
    with open(validation_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter='\t')
        val_rows = list(reader)

    enriched = []
    for r in val_rows:
        enriched.append({
            "row":       r['row'],
            "field":     r['field'],
            "error":     r['error'],
            "ctx_key":   r.get('context_key', ''),
            "ctx_value": r.get('context_value', ''),
            "value":     r.get('value', ''),
            "point_id":  row_lookup.get(r['row'], f"(row {r['row']})"),
        })
    return row_lookup, enriched


def parse_from_sheets(service) -> tuple[dict, list]:
    """Parse directly from Google Sheets."""
    def get_tab(name):
        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{name}'!A1:Z2000"
        ).execute()
        values = result.get('values', [])
        if not values:
            return []
        header = values[0]
        return [dict(zip(header, row + [''] * (len(header) - len(row)))) for row in values[1:]]

    template_rows = get_tab(SHEET_TEMPLATE)
    row_lookup = {}
    for i, row in enumerate(template_rows):
        csv_row_num = i + 2
        pid = row.get('well_name_point_id', '').strip()
        row_lookup[str(csv_row_num)] = pid if pid and pid not in ('optional', 'text') else f"(row {csv_row_num})"

    val_rows = get_tab(SHEET_VALIDATION)
    enriched = []
    for r in val_rows:
        enriched.append({
            "row":       r.get('row', ''),
            "field":     r.get('field', ''),
            "error":     r.get('error', ''),
            "ctx_key":   r.get('context_key', ''),
            "ctx_value": r.get('context_value', ''),
            "value":     r.get('value', ''),
            "point_id":  row_lookup.get(r.get('row', ''), f"(row {r.get('row', '?')})"),
        })
    return row_lookup, enriched


# ── CLASSIFICATION ─────────────────────────────────────────────────────────────

def classify_all(enriched: list) -> dict[str, dict]:
    """
    Returns dict keyed by error_class_id:
        ec:       the error class definition
        rows:     list of enriched error rows matching this class
    """
    classified: dict[str, dict] = {}

    for row in enriched:
        field   = row['field']
        error   = row['error']
        ctx_key = row['ctx_key']
        value   = row['value']

        # For unknown-field errors, use context_key as the effective field
        effective_field = ctx_key if field == 'unknown' and ctx_key else field

        ec = classify_row(effective_field, error, ctx_key, value)

        if ec is None:
            key = f"UNCLASSIFIED.{effective_field}"
            if key not in classified:
                classified[key] = {
                    "ec": {
                        "id": key,
                        "fields": [effective_field],
                        "plain_english": f"Unclassified error on field '{effective_field}': {error[:200]}",
                        "owner": "Mapping Needed",
                        "owner_rationale": "Not yet mapped to a resolution path.",
                        "diagnose": "Review raw error text and determine root cause.",
                        "resolution": "Escalate to Dev + AMP for triage.",
                        "amp_needs": "TBD",
                        "dev_needs": "TBD",
                        "disposition": "NEEDS DECISION",
                        "priority": "UNKNOWN",
                    },
                    "rows": [],
                }
        else:
            key = ec["id"]
            if key not in classified:
                classified[key] = {"ec": ec, "rows": []}

        classified[key]["rows"].append(row)

    return classified


# ── GOOGLE SHEETS OUTPUT ───────────────────────────────────────────────────────

def get_service():
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_FILE, scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds)


def get_or_create_sheet(service, title: str) -> int:
    ss = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    for sheet in ss.get("sheets", []):
        if sheet["properties"]["title"] == title:
            return sheet["properties"]["sheetId"]
    body = {"requests": [{"addSheet": {"properties": {"title": title}}}]}
    resp = service.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID, body=body
    ).execute()
    return resp["replies"][0]["addSheet"]["properties"]["sheetId"]


def write_sheet(service, sheet_name: str, rows: list[list]):
    service.spreadsheets().values().clear(
        spreadsheetId=SPREADSHEET_ID, range=f"'{sheet_name}'!A1:Z10000"
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{sheet_name}'!A1",
        valueInputOption="RAW",
        body={"values": rows},
    ).execute()


def format_sheet(service, sheet_id: int, num_cols: int,
                 header_color: tuple = (0.18, 0.37, 0.67)):
    r, g, b = header_color
    requests = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0, "endRowIndex": 1,
                    "startColumnIndex": 0, "endColumnIndex": num_cols,
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                        "backgroundColor": {"red": r, "green": g, "blue": b},
                        "horizontalAlignment": "CENTER",
                    }
                },
                "fields": "userEnteredFormat(textFormat,backgroundColor,horizontalAlignment)",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": num_cols,
                },
                "cell": {
                    "userEnteredFormat": {
                        "wrapStrategy": "WRAP",
                        "verticalAlignment": "TOP",
                    }
                },
                "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)",
            }
        },
    ]
    # Column widths
    widths = [320] + [220] * (num_cols - 1)
    for i, w in enumerate(widths[:num_cols]):
        requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": i, "endIndex": i + 1,
                },
                "properties": {"pixelSize": w},
                "fields": "pixelSize",
            }
        })
    service.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID, body={"requests": requests}
    ).execute()


# ── CATALOG SHEET ──────────────────────────────────────────────────────────────

CATALOG_HEADERS = [
    "Error Class ID",
    "Affected Fields",
    "Occurrence Count",
    "Disposition",
    "Priority",
    "Owner",
    "Plain-English Meaning",
    "How to Diagnose",
    "Approved Resolution Paths",
    "What AMP Needs to Provide",
    "What Dev Needs to Implement",
    # Human-filled — never overwritten
    "Jira / Ticket",
    "Status",
    "Notes",
]
CATALOG_PRESERVED_START = "Jira / Ticket"


def write_catalog(service, classified: dict):
    existing = read_sheet_raw(service, SHEET_CATALOG)
    preserved: dict[str, list] = {}
    if existing and len(existing) > 1:
        try:
            hdr = existing[0]
            id_col = hdr.index("Error Class ID")
            p_start = hdr.index(CATALOG_PRESERVED_START)
            for row in existing[1:]:
                if len(row) > id_col:
                    preserved[row[id_col]] = row[p_start:] if len(row) > p_start else []
        except (ValueError, IndexError):
            pass

    output = [CATALOG_HEADERS]
    # Defined classes first, then unclassified
    ordered = [ec["id"] for ec in ERROR_CLASSES] + sorted(
        k for k in classified if k.startswith("UNCLASSIFIED")
    )
    for key in ordered:
        if key not in classified:
            continue
        ec   = classified[key]["ec"]
        rows = classified[key]["rows"]
        prev = preserved.get(key, [])
        row  = [
            ec.get("id", ""),
            ", ".join(ec.get("fields", [])),
            len(rows),
            ec.get("disposition", ""),
            ec.get("priority", ""),
            ec.get("owner", ""),
            ec.get("plain_english", ""),
            ec.get("diagnose", ""),
            ec.get("resolution", ""),
            ec.get("amp_needs", ""),
            ec.get("dev_needs", ""),
        ]
        num_preserved = len(CATALOG_HEADERS) - CATALOG_HEADERS.index(CATALOG_PRESERVED_START)
        for i in range(num_preserved):
            row.append(prev[i] if i < len(prev) else "")
        output.append(row)

    write_sheet(service, SHEET_CATALOG, output)
    sheet_id = get_or_create_sheet(service, SHEET_CATALOG)
    format_sheet(service, sheet_id, len(CATALOG_HEADERS), header_color=(0.18, 0.37, 0.67))

    # Yellow tint for human-filled columns
    p_col = CATALOG_HEADERS.index(CATALOG_PRESERVED_START)
    service.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": [{
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "startColumnIndex": p_col,
                    "endColumnIndex": len(CATALOG_HEADERS),
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 1.0, "green": 0.98, "blue": 0.80},
                    }
                },
                "fields": "userEnteredFormat(backgroundColor)",
            }
        }]}
    ).execute()

    print(f"  ✅ '{SHEET_CATALOG}' — {len(output) - 1} error classes.")


# ── ROW TRACKING SHEET ─────────────────────────────────────────────────────────

TRACKING_HEADERS = [
    "Error Class ID",
    "Disposition",
    "Owner",
    "PointID / Row",
    "Template Row #",
    "Affected Field",
    "Entered Value",
    "Error Message",
    # Human-filled — never overwritten
    "Fixed By",
    "Fixed Date",
    "Fix Notes",
    "Status",
]
TRACKING_HUMAN_START = 8  # "Fixed By" onwards


def write_tracking(service, classified: dict):
    existing = read_sheet_raw(service, SHEET_TRACKING)
    # Build lookup: (class_id, row_num) -> preserved human columns
    preserved: dict[tuple, list] = {}
    if existing and len(existing) > 1:
        try:
            hdr = existing[0]
            id_col  = hdr.index("Error Class ID")
            row_col = hdr.index("Template Row #")
            h_start = TRACKING_HUMAN_START
            for row in existing[1:]:
                if len(row) > max(id_col, row_col):
                    key = (row[id_col], row[row_col])
                    preserved[key] = row[h_start:] if len(row) > h_start else []
        except (ValueError, IndexError):
            pass

    output = [TRACKING_HEADERS]
    ordered = [ec["id"] for ec in ERROR_CLASSES] + sorted(
        k for k in classified if k.startswith("UNCLASSIFIED")
    )
    for key in ordered:
        if key not in classified:
            continue
        ec   = classified[key]["ec"]
        rows = classified[key]["rows"]
        for r in rows:
            pkey = (ec.get("id", ""), r["row"])
            prev = preserved.get(pkey, [])
            # Use context_value as entered value for unknown-field errors
            entered_val = r["ctx_value"] if r["field"] == "unknown" and r["ctx_value"] else r["value"]
            new_row = [
                ec.get("id", ""),
                ec.get("disposition", ""),
                ec.get("owner", ""),
                r["point_id"],
                r["row"],
                r["ctx_key"] if r["field"] == "unknown" and r["ctx_key"] else r["field"],
                entered_val,
                r["error"][:120],
            ]
            num_human = len(TRACKING_HEADERS) - TRACKING_HUMAN_START
            for i in range(num_human):
                new_row.append(prev[i] if i < len(prev) else "")
            output.append(new_row)

    write_sheet(service, SHEET_TRACKING, output)
    sheet_id = get_or_create_sheet(service, SHEET_TRACKING)
    format_sheet(service, sheet_id, len(TRACKING_HEADERS), header_color=(0.55, 0.18, 0.18))

    # Yellow tint for human-filled columns
    service.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": [{
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "startColumnIndex": TRACKING_HUMAN_START,
                    "endColumnIndex": len(TRACKING_HEADERS),
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 1.0, "green": 0.98, "blue": 0.80},
                    }
                },
                "fields": "userEnteredFormat(backgroundColor)",
            }
        }]}
    ).execute()

    print(f"  ✅ '{SHEET_TRACKING}' — {len(output) - 1} rows.")


def read_sheet_raw(service, sheet_name: str) -> list[list]:
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{sheet_name}'!A1:Z10000"
        ).execute()
        return result.get("values", [])
    except Exception:
        return []


# ── CONSOLE SUMMARY ────────────────────────────────────────────────────────────

def print_summary(classified: dict):
    print("\n=== WELL INVENTORY TEMPLATE — ERROR SUMMARY ===\n")
    total = sum(len(v["rows"]) for v in classified.values())
    print(f"Total error rows: {total}")
    print(f"Error classes:    {len(classified)}\n")

    print(f"{'CLASS ID':<55} {'ROWS':>5}  {'DISPOSITION':<30}  {'PRIORITY'}")
    print("-" * 115)
    for key in [ec["id"] for ec in ERROR_CLASSES] + sorted(k for k in classified if k.startswith("UNCLASSIFIED")):
        if key not in classified:
            continue
        ec   = classified[key]["ec"]
        rows = classified[key]["rows"]
        print(f"  {key:<53} {len(rows):>5}  {ec.get('disposition',''):<30}  {ec.get('priority','')}")
    print()


# ── MAIN ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true",
                        help="Read from local TSV files instead of Google Sheets API")
    args = parser.parse_args()

    if args.local:
        print(f"📂 Reading from local TSV files…")
        _, enriched = parse_from_tsv(LOCAL_TEMPLATE, LOCAL_VALIDATION)
    else:
        print("🔗 Connecting to Google Sheets…")
        service = get_service()
        _, enriched = parse_from_sheets(service)

    print(f"   {len(enriched)} validation rows loaded.")

    classified = classify_all(enriched)
    print_summary(classified)

    if not args.local:
        print("📋 Writing output sheets…")
        for sheet_name in [SHEET_CATALOG, SHEET_TRACKING]:
            get_or_create_sheet(service, sheet_name)
        write_catalog(service, classified)
        write_tracking(service, classified)
    else:
        print("ℹ️  --local mode: skipping Google Sheets write. Remove --local to write output.")

    print("\n✨ Done.")


if __name__ == "__main__":
    main()
