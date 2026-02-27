"""
AMP Transfer Metrics Pipeline
==============================
Parses a transfer_metrics_metrics_<date>.csv file and writes/updates
three sheets in a Google Spreadsheet:

  1. "4.1 Error Catalog"   — one row per error class (written fresh each run,
                             but Jira/review columns are READ FIRST and preserved)
  2. "Run Snapshots"       — one row per error class per run, appended forever
  3. "Run Summary"         — one row per model per run (input/cleaned/transferred counts)

Usage
-----
    python amp_pipeline.py --file transfer_metrics_logs/transfer_metrics_metrics_2026-01-27T01_15_56.csv

    # Or let it auto-detect the latest file in the folder:
    python amp_pipeline.py

Setup
-----
    pip install google-auth google-auth-httplib2 google-auth-oauthlib google-api-python-client

    Create a Service Account in Google Cloud Console, enable the Sheets API,
    download service_account.json, and share your spreadsheet with the
    service account email (Editor role).
"""

import argparse
import csv
import io
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── CONFIGURATION ──────────────────────────────────────────────────────────────
SPREADSHEET_ID      = "1iQzeKqRWHIKbnNptH_wRQEpJ_pt1rI00ax9d5BhDAhU"
CREDENTIALS_FILE    = "transfermetrics_service_account.json"
METRICS_FOLDER      = "transfer_metrics_logs"
METRICS_FILENAME_RE = re.compile(r"transfer_metrics_metrics_(.+)\.csv")

SHEET_CATALOG   = "4.1 Error Catalog"
SHEET_SNAPSHOTS = "Run Snapshots"
SHEET_SUMMARY   = "Run Summary"
SHEET_TRACKING  = "PointID Tracking"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Catalog columns that humans fill in — preserved across runs
PRESERVED_CATALOG_COLS = [
    "Jira Ticket",
    "Jira Status",
    "AMP Reviewed By",
    "AMP Reviewed Date",
    "Dev Fix Deployed Date",
    "Notes",
]

# ── FILE PARSING ───────────────────────────────────────────────────────────────

# Known model name → display label
MODEL_LABELS = {
    "Well": "Well",
    "WellScreen": "WellScreen",
    "Contact": "Contact",
    "ThingGeologicFormationAssociation": "Formation Association",
    "AssociatedData": "Associated Data",
    "Manual Water Levels": "Manual Water Levels",
    "WellData Link IDs": "WellData Link IDs",
    "LocationData Link IDs": "LocationData Link IDs",
    "Group": "Group",
    "SurfaceWaterPhotos": "Surface Water Photos",
    "Soil_Rock_Results": "Soil/Rock Results",
    "NMA_SurfaceWaterData": "Surface Water Data",
    "HydraulicsData": "Hydraulics Data",
    "Chemistry_SampleInfo": "Chemistry Sample Info",
    "WaterLevelsContinuous_Pressure_Daily": "WL Continuous Pressure Daily",
    "WeatherData": "Weather Data",
    "WeatherPhotos": "Weather Photos",
    "MajorChemistry": "Major Chemistry",
    "Radionuclides": "Radionuclides",
    "MinorTraceChemistry": "Minor/Trace Chemistry",
    "FieldParameters": "Field Parameters",
    "Sensor": "Sensor",
    "Pressure Transducer": "Pressure Transducer",
    "Acoustic Sounder": "Acoustic Sounder",
}


def parse_metrics_file(filepath: str) -> tuple[str, list[dict], list[dict]]:
    """
    Parse a transfer_metrics_metrics_<date>.csv file.

    Returns:
        run_date:    ISO date string extracted from filename
        model_stats: list of dicts with model-level counts
        error_rows:  list of dicts with individual error rows
    """
    path = Path(filepath)
    m = METRICS_FILENAME_RE.search(path.name)
    if not m:
        # Fall back to file mtime
        ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        run_date = ts.strftime("%Y-%m-%d")
    else:
        # Parse the timestamp from the filename, e.g. 2026-01-27T01_15_56
        raw_ts = m.group(1).replace("_", ":")  # 2026-01-27T01:15:56
        try:
            run_date = datetime.fromisoformat(raw_ts).strftime("%Y-%m-%d")
        except ValueError:
            run_date = m.group(1)[:10]  # just take the date part

    model_stats = []
    error_rows = []
    current_model = None

    with open(filepath, "r", encoding="utf-8-sig") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n\r")
            if not line.strip():
                continue

            parts = line.split("|")

            # Model summary line: ModelName|input_count|cleaned_count|transferred|issue_pct
            if (
                len(parts) == 5
                and parts[0] not in ("model", "PointID")
                and _is_number(parts[1])
            ):
                current_model = parts[0].strip()
                model_stats.append({
                    "model": current_model,
                    "label": MODEL_LABELS.get(current_model, current_model),
                    "input_count": int(float(parts[1])),
                    "cleaned_count": int(float(parts[2])),
                    "transferred": int(float(parts[3])),
                    "issue_pct": float(parts[4]),
                })
                continue

            # Column header line
            if parts[0] == "PointID" and len(parts) >= 4:
                continue

            # Global header line
            if parts[0] == "model":
                continue

            # Error data line: PointID|Table|Field|Error
            if len(parts) >= 4 and current_model is not None:
                point_id = parts[0].strip()
                table = parts[1].strip()
                field = parts[2].strip()
                error = "|".join(parts[3:]).strip()  # error may contain pipes
                if point_id:
                    error_rows.append({
                        "model": current_model,
                        "point_id": point_id,
                        "table": table,
                        "field": field,
                        "table_field": f"{table}.{field}" if field else table,
                        "error": error,
                    })

    return run_date, model_stats, error_rows


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


# ── ERROR CLASSIFICATION ───────────────────────────────────────────────────────

ERROR_CLASSES = [
    {
        "id": "INTEGRITY.transducer_block.duplicate_key",
        "match": lambda tf, err: "uq_transducer_block_status_parameter_time" in err,
        "table_fields": ["transducer_observation_block"],
        "constraint_type": "Database (PostgreSQL unique constraint C:23505)",
        "signature": r"pg8000.dbapi.IntegrityError.*uq_transducer_block_status_parameter_time",
        "plain_english": "A transducer observation block for this parameter/time window already exists with 'approved' status. Inserting a second block with identical (review_status, parameter_id, start_datetime, end_datetime) violates the unique constraint.",
        "owner": "Dev / Data Services",
        "owner_rationale": "Duplicate blocks arise when the ETL re-runs without dedup logic. Data Services must confirm which block is canonical; Dev must add idempotent upsert logic.",
        "diagnose": "1. Query transducer_observation_block WHERE review_status='approved' AND parameter_id=<id> AND start/end datetimes match.\n2. Compare row counts and values between the two blocks.\n3. Check ETL run logs for duplicate import runs.",
        "resolution_paths": "Path 1 (Data Services): Identify canonical block, archive the duplicate.\nPath 2 (Dev): Refactor import to use INSERT ... ON CONFLICT DO UPDATE.\nPath 3 (Dev): Add pre-import dedup check.\nNOT ALLOWED: Deleting both blocks without confirming which holds correct data.",
        "amp_needs": "N/A — AMP should flag if the observation data is identical (true duplicate) or different (merge conflict).",
        "dev_needs": "Upsert on (review_status, parameter_id, start_datetime, end_datetime).\nUnit tests: insert identical block twice → second is no-op; overlapping block with different data → clear business error.\nMigration: audit existing duplicates before deploying.",
    },
    {
        "id": "VALIDATION.well_data.measuring_point_height.required",
        "match": lambda tf, err: "measuring_point_height" in err and "float_type" in err,
        "table_fields": ["WellData.measuring_point_height"],
        "constraint_type": "Programmatic (Pydantic float_type)",
        "signature": r"Validation Error.*float_type.*measuring_point_height.*Input should be a valid number",
        "plain_english": "measuring_point_height is missing or non-numeric. The schema expects a float (elevation in feet above datum). This is the most frequent error (~522 occurrences).",
        "owner": "AMP",
        "owner_rationale": "AMP field staff measure MP height. Dev cannot fabricate this value.",
        "diagnose": "1. Search AMP UI for the PointID.\n2. Check field measurement sheets for 'MP height', 'stick-up', or 'casing height'.\n3. For legacy records, check NMBGMR archives or OSE files.",
        "resolution_paths": "Path 1 (AMP – preferred): Enter measured MP height in AMP UI.\nPath 2 (Dev): For legacy records where MP height was never measured, make field optional with NULL + mp_height_unknown flag.\nPath 3 (Dev + AMP): If a standard default is scientifically acceptable for a specific well class, document as policy.\nNOT ALLOWED: Defaulting to 0 — produces incorrect water-level calculations.",
        "amp_needs": "Format: decimal number (e.g., 1.37). Units: feet above top of casing. Range: 0.1–10.0 ft typical; flag outliers.\nDomain: MP height used to convert raw transducer depth to water-level elevation.",
        "dev_needs": "Reject non-numeric values with clear field name in error. Do not accept empty string as 0.\nUnit tests: valid float → accepted; empty string → error; NULL → error unless mp_height_unknown flag set.\nMigration: 522+ records affected — allow grace period for AMP to populate.",
    },
    {
        "id": "VALIDATION.well_data.casing_depth.exceeds_hole_depth",
        "match": lambda tf, err: "casing depth must be less than or equal to hole depth" in err,
        "table_fields": ["WellData.CasingDepth", "WellData.HoleDepth"],
        "constraint_type": "Programmatic (Pydantic value_error cross-field)",
        "signature": r"well casing depth must be less than or equal to hole depth",
        "plain_english": "Casing depth > hole depth — physically impossible. Likely a unit mismatch (ft vs m) or data entry transposition.",
        "owner": "AMP",
        "owner_rationale": "AMP entered the well construction data.",
        "diagnose": "1. Compare CasingDepth and HoleDepth in AMP UI.\n2. Check for ft vs m unit mismatch.\n3. Consult driller's log or completion report.",
        "resolution_paths": "Path 1 (AMP): Correct erroneous value after verifying against driller's log.\nPath 2 (AMP): If units differ, standardize both to feet.\nNOT ALLOWED: Swapping casing and hole depth without confirming which is wrong.",
        "amp_needs": "Verified CasingDepth and HoleDepth in consistent units (feet preferred). Source: driller's log, OSE completion report.",
        "dev_needs": "Validate CasingDepth ≤ HoleDepth on save; show both values in error message.\nUnit tests: casing > hole → error; casing = hole → accepted; casing < hole → accepted.\nMigration: 18 records; require human review, do not auto-correct.",
    },
    {
        "id": "VALIDATION.well_data.well_depth.exceeds_hole_depth",
        "match": lambda tf, err: "well depth must be less than" in err and "hole depth" in err,
        "table_fields": ["WellData.WellDepth", "WellData.HoleDepth"],
        "constraint_type": "Programmatic (Pydantic value_error cross-field)",
        "signature": r"well depth must be less than than or equal to hole depth",
        "plain_english": "Well (total) depth > borehole depth — physically impossible. Same root cause as casing depth error.",
        "owner": "AMP",
        "owner_rationale": "AMP entered the well construction data.",
        "diagnose": "Same as casing depth error; check driller's log.",
        "resolution_paths": "Same as VALIDATION.well_data.casing_depth.exceeds_hole_depth.",
        "amp_needs": "Verified WellDepth and HoleDepth in consistent units.",
        "dev_needs": "Validate WellDepth ≤ HoleDepth. Unit tests: same pattern as casing depth. Migration: 3 records.",
    },
    {
        "id": "VALIDATION.well_data.depth_logic.value_error",
        "match": lambda tf, err: "WellData" in tf and "value_error" in err and "depth" in err.lower(),
        "table_fields": ["WellData.WellDepth", "WellData.CasingDepth", "WellData.HoleDepth"],
        "constraint_type": "Programmatic (Pydantic value_error cross-field)",
        "signature": r"Validation Error.*value_error.*well.*depth",
        "plain_english": "Pydantic model-level depth logic violation — covers both 'well depth > hole depth' and 'casing depth > hole depth' caught at the validator level.",
        "owner": "AMP",
        "owner_rationale": "Same as individual depth errors above.",
        "diagnose": "Check which field pair is invalid from the 'loc' field in the error dict.",
        "resolution_paths": "Same as VALIDATION.well_data.casing_depth.exceeds_hole_depth.",
        "amp_needs": "Verified depth values from driller's log.",
        "dev_needs": "Ensure error message specifies which pair of fields triggered validation.",
    },
    {
        "id": "VALIDATION.well_data.formation_zone.unknown",
        "match": lambda tf, err: "FormationZone" in tf and err.startswith("Unknown formation:"),
        "table_fields": ["WellData.FormationZone"],
        "constraint_type": "Business Logic (lexicon lookup)",
        "signature": r"Unknown formation: [A-Z0-9/a-z_]+",
        "plain_english": "FormationZone code not present in the formation lookup table. Typically compound codes (e.g. '121TSUQs/112ANCH') or suffix variants (e.g. 'ppm', 'sr') not yet in the lexicon.",
        "owner": "AMP",
        "owner_rationale": "AMP hydrogeologists assign formation codes. Dev needs AMP to confirm which codes are valid before adding to lexicon.",
        "diagnose": "1. Extract code from error.\n2. Check NMBGMR formation lexicon / Ocotillo system for base code.\n3. Determine if compound (A/B format) or suffix variant.\n4. Contact AMP hydrogeologist.",
        "resolution_paths": "Path 1 (AMP): Code is a valid variant not in lexicon → provide definition for Dev to add.\nPath 2 (AMP): Code is a data entry error → correct to nearest valid code.\nPath 3 (Dev): Compound codes (A/B) → implement compound-code parsing if the system should support primary/secondary formation pairs.\nNOT ALLOWED: Mapping unknown codes to 'Unknown' without documenting intent.",
        "amp_needs": "For each unique unknown code: full name, description, USGS GEOLEX or NMBGMR reference, and whether it is a new entry or alias.",
        "dev_needs": "Reject unknown formation codes; show code name in error with link to valid lexicon.\nUnit tests: known code → accepted; unknown → error with code shown; compound code → each component validated.\nMigration: run frequency report, batch-add most common codes first.",
    },
    {
        "id": "VALIDATION.well_data.aquifer_type.unknown_lexicon",
        "match": lambda tf, err: "LU_AquiferType" in err,
        "table_fields": ["WellData.AquiferType"],
        "constraint_type": "Business Logic (lexicon lookup)",
        "signature": r"Unknown lexicon value: LU_AquiferType:[A-Za-z0-9]+",
        "plain_english": "AquiferType value not in LU_AquiferType lookup. Common offenders: '8' (numeric) and 'S' (likely 'semi-confined').",
        "owner": "AMP",
        "owner_rationale": "AMP entered the aquifer classification; allowed values are controlled by NMBGMR lexicon.",
        "diagnose": "1. Note the invalid value.\n2. Check current LU_AquiferType table for valid entries.\n3. Ask AMP which standard type the value was intended to represent.",
        "resolution_paths": "Path 1 (AMP): Determine correct lexicon value and update in AMP UI.\nPath 2 (Dev + AMP): If 'S' and '8' represent valid concepts not yet in the lexicon, add them after AMP provides definitions.\nNOT ALLOWED: Silently converting to a default type.",
        "amp_needs": "Mapping from invalid codes to their intended standard AquiferType values.",
        "dev_needs": "Show invalid value in error with list of valid options. Unit tests: invalid → error with valid list; valid → accepted.",
    },
    {
        "id": "VALIDATION.well_data.current_use.unknown_lexicon",
        "match": lambda tf, err: "LU_CurrentUse" in err,
        "table_fields": ["WellData.CurrentUse"],
        "constraint_type": "Business Logic (lexicon lookup)",
        "signature": r"Unknown lexicon value: LU_CurrentUse:[A-Za-z]*",
        "plain_english": "CurrentUse value (e.g. 'W' or blank) not in LU_CurrentUse lookup table.",
        "owner": "AMP",
        "owner_rationale": "AMP assigns well use classifications; the lexicon controls valid options.",
        "diagnose": "1. Identify the invalid value.\n2. Compare against LU_CurrentUse valid entries.\n3. Determine intended use from source documents.",
        "resolution_paths": "Path 1 (AMP): Correct the value to a valid CurrentUse code.\nPath 2 (Dev + AMP): If blank is valid, add an explicit 'UNKNOWN' or 'UNSPECIFIED' lexicon entry.\nNOT ALLOWED: Storing empty string in a controlled vocabulary field.",
        "amp_needs": "Intended CurrentUse classification for each flagged PointID.",
        "dev_needs": "Reject empty string; accept NULL only if field is nullable.",
    },
    {
        "id": "VALIDATION.well_data.construction_method.unknown",
        "match": lambda tf, err: "LU_ConstructionMethod" in err or ("well_construction_method" in err and "enum" in err),
        "table_fields": ["WellData.ConstructionMethod"],
        "constraint_type": "Programmatic (Pydantic enum) / Business Logic (lexicon lookup)",
        "signature": r"(Unknown lexicon value: LU_ConstructionMethod:|Validation Error.*enum.*well_construction_method)",
        "plain_english": "Well construction method code (e.g. 'AH', 'H') not in the LU_ConstructionMethod lookup or Pydantic enum. Likely 'Auger Hollow-stem' and 'Hydraulic' rotary.",
        "owner": "AMP / Dev",
        "owner_rationale": "AMP must confirm the intended method; Dev adds to lexicon/enum.",
        "diagnose": "1. Identify the invalid code.\n2. Check driller's log for construction method description.\n3. Compare against existing LU_ConstructionMethod entries.",
        "resolution_paths": "Path 1 (AMP): Map code to existing lexicon entry and update record.\nPath 2 (Dev + AMP): If new valid methods, add to lexicon with full descriptions.\nNOT ALLOWED: Using abbreviations not defined in the lexicon.",
        "amp_needs": "Full name and description for each unrecognized construction method code.",
        "dev_needs": "Show invalid code + valid options in error. Unit tests: 'AH' before update → error; after update → accepted.",
    },
    {
        "id": "VALIDATION.well_data.well_completion_date.datetime_precision",
        "match": lambda tf, err: "date_from_datetime_inexact" in err and "well_complet" in err,
        "table_fields": ["WellData.WellCompletionDate"],
        "constraint_type": "Programmatic (Pydantic date_from_datetime_inexact)",
        "signature": r"Validation Error.*date_from_datetime_inexact.*well_complet",
        "plain_english": "WellCompletionDate was provided as a datetime with time component (e.g. '1985-06-15 00:00:00') but schema expects date-only ('1985-06-15'). Source data exports dates as datetimes.",
        "owner": "Dev",
        "owner_rationale": "This is a schema/parser issue — Dev should strip the time component during ETL rather than requiring AMP to reformat.",
        "diagnose": "1. Check raw source value for WellCompletionDate.\n2. Confirm it is always midnight (00:00:00).\n3. Verify schema column type (DATE vs TIMESTAMP).",
        "resolution_paths": "Path 1 (Dev – preferred): Add ETL coerce step to strip time component from date fields before validation.\nPath 2 (Dev): Change schema column to TIMESTAMP if time-of-day matters.\nNOT ALLOWED: Requiring AMP to manually reformat dates in source spreadsheet.",
        "amp_needs": "N/A — purely a technical ETL fix.",
        "dev_needs": "Accept '1985-06-15T00:00:00' and coerce to '1985-06-15' for DATE columns.\nUnit tests: datetime with midnight → coerced to date, no error; non-midnight → warning.\nMigration: 6 records; low risk.",
    },
    {
        "id": "VALIDATION.well_data.status.unknown_lexicon",
        "match": lambda tf, err: "LU_Status" in err,
        "table_fields": ["WellData.Status"],
        "constraint_type": "Business Logic (lexicon lookup)",
        "signature": r"Unknown lexicon value: LU_Status:[A-Za-z0-9]+",
        "plain_english": "Well status code (e.g. 'O') not in LU_Status lookup. Likely represents 'Other' or 'Observation' but needs formal definition.",
        "owner": "Data Services / Dev",
        "owner_rationale": "Data Services must confirm the intended status; Dev adds to lexicon.",
        "diagnose": "1. Check source record for context on the status code.\n2. Review existing LU_Status entries for close matches.\n3. Consult NMBGMR or OSE status classification documentation.",
        "resolution_paths": "Path 1 (Dev + Data Services): Define the code and add to LU_Status lexicon.\nPath 2 (Data Services): Map to an existing status code if equivalent.\nNOT ALLOWED: Importing records with undefined status codes.",
        "amp_needs": "N/A.",
        "dev_needs": "Add code to lexicon after Data Services confirms definition.",
    },
    {
        "id": "VALIDATION.well_data.missing_unit_identifier",
        "match": lambda tf, err: "Missing UnitIdentifier" in err,
        "table_fields": ["WellData (UnitIdentifier)"],
        "constraint_type": "Business Logic (import script check)",
        "signature": r"Missing UnitIdentifier",
        "plain_english": "A well record is missing a unit identifier required to associate it with a hydrologic unit or formation zone.",
        "owner": "AMP / Jake Ross",
        "owner_rationale": "Jake Ross owns unit identifier mapping per the source sheet.",
        "diagnose": "1. Check if the same PointID has a FormationZone error — missing formation often causes missing unit identifier.\n2. Contact Jake Ross for correct formation/unit mapping.",
        "resolution_paths": "Path 1 (AMP): Provide correct formation code; unit identifier should derive from it.\nPath 2 (Dev): If UnitIdentifier derivable from FormationZone, auto-populate it.\nNOT ALLOWED: Leaving UnitIdentifier blank in a finalized well record.",
        "amp_needs": "Formation code from which unit identifier can be derived; contact Jake Ross.",
        "dev_needs": "If UnitIdentifier can be derived from FormationZone, auto-populate it.\nUnit tests: valid FormationZone → UnitIdentifier auto-populated; null FormationZone → error on UnitIdentifier.",
    },
    {
        "id": "VALIDATION.well_data.duplicate_point_id",
        "match": lambda tf, err: "WellData" in tf and "duplicate" in err.lower() and "PointID" in tf,
        "table_fields": ["WellData.PointID"],
        "constraint_type": "Business Logic (import script check)",
        "signature": r"duplicate records",
        "plain_english": "Multiple well records share the same PointID, violating the uniqueness constraint.",
        "owner": "Dev / Data Services (Ethan Mamer)",
        "owner_rationale": "Ethan Mamer owns deduplication per the source sheet.",
        "diagnose": "1. Query WellData WHERE PointID = <id>.\n2. Compare field values to determine canonical record.\n3. Check submission history.",
        "resolution_paths": "Path 1 (Data Services): Identify canonical record and archive duplicates.\nPath 2 (Dev): Add pre-import uniqueness check.\nNOT ALLOWED: Silently overwriting existing record without review.",
        "amp_needs": "N/A.",
        "dev_needs": "Reject import if PointID already exists; offer explicit 'update' mode.\nUnit tests: duplicate PointID → error; unique → accepted.",
    },
    {
        "id": "VALIDATION.well_screens.depth.invalid_numeric",
        "match": lambda tf, err: "WellScreen" in tf and ("screen_depth_bottom: Input should be a valid number" in err or "screen_depth_top: Input should be a valid number" in err),
        "table_fields": ["WellScreens.screen_depth_top", "WellScreens.screen_depth_bottom"],
        "constraint_type": "Programmatic (Pydantic float_type)",
        "signature": r"screen_depth_(top|bottom): Input should be a valid number",
        "plain_english": "A well screen depth field contains a non-numeric value (e.g. text, blank, or non-standard notation). Screen depths must be numeric (feet below surface).",
        "owner": "AMP",
        "owner_rationale": "AMP entered the screen intervals; source data has non-numeric characters.",
        "diagnose": "1. Inspect screen interval data in well record.\n2. Check for text like 'unknown', 'N/A', or units embedded in value.\n3. Reference driller's log for actual screen placement depths.",
        "resolution_paths": "Path 1 (AMP): Enter verified numeric screen depths in AMP UI.\nPath 2 (Dev): If screen depth genuinely unknown, allow NULL with 'screen_depth_unknown' flag.\nNOT ALLOWED: Entering 0 for unknown screen depths.",
        "amp_needs": "Numeric screen top and bottom depths in feet below land surface.",
        "dev_needs": "Reject non-numeric values; accept NULL if field is nullable.\nUnit tests: string → error; 0 → error if greater_than constraint; valid float → accepted.",
    },
    {
        "id": "VALIDATION.well_screens.depth.top_must_be_positive",
        "match": lambda tf, err: "WellScreen" in tf and "screen_depth_top" in err and "greater than 0" in err,
        "table_fields": ["WellScreens.screen_depth_top"],
        "constraint_type": "Programmatic (Pydantic greater_than)",
        "signature": r"screen_depth_top: Input should be greater than 0",
        "plain_english": "Top of screen recorded as 0 or negative — physically impossible. Almost certainly a data entry error or 0 used as placeholder for unknown.",
        "owner": "AMP",
        "owner_rationale": "AMP entered the screen depth; zero is almost certainly an error.",
        "diagnose": "1. Check driller's log for top-of-screen depth.\n2. Determine if 0 was a placeholder for 'unknown'.\n3. For artesian wells, confirm screens are below land surface.",
        "resolution_paths": "Path 1 (AMP): Enter correct positive depth from driller's log.\nPath 2 (Dev + AMP): If unknown, set to NULL and add flag — do not use 0.\nNOT ALLOWED: Using 0 as placeholder for unknown screen depth.",
        "amp_needs": "Confirmed screen top depth in feet below land surface (must be > 0).",
        "dev_needs": "Enforce screen_depth_top > 0.\nUnit tests: 0 → error; negative → error; positive float → accepted.",
    },
    {
        "id": "VALIDATION.well_screens.depth.bottom_less_than_top",
        "match": lambda tf, err: "WellScreen" in tf and "screen_depth_bottom must be greater than screen_depth_top" in err,
        "table_fields": ["WellScreens.screen_depth_top", "WellScreens.screen_depth_bottom"],
        "constraint_type": "Programmatic (Pydantic value_error cross-field)",
        "signature": r"screen_depth_bottom must be greater than screen_depth_top",
        "plain_english": "Bottom of screen is shallower than top — physically impossible. Values are likely transposed.",
        "owner": "AMP",
        "owner_rationale": "AMP entered the screen interval; transposition is the likely cause.",
        "diagnose": "1. Compare screen_depth_top and screen_depth_bottom.\n2. If top > bottom, values are swapped.\n3. Verify correct values against driller's log.",
        "resolution_paths": "Path 1 (AMP): Swap values if transposed.\nPath 2 (AMP): Enter correct values from driller's log if both wrong.\nNOT ALLOWED: Setting bottom = top (zero-length screen).",
        "amp_needs": "Confirmed top and bottom screen depths; bottom must exceed top.",
        "dev_needs": "Validate bottom > top; show both values in error.\nUnit tests: bottom ≤ top → error; bottom > top → accepted.",
    },
    {
        "id": "VALIDATION.owners_data.name_or_org_required",
        "match": lambda tf, err: "OwnersData" in tf and ("Either name or organization must be provided" in err or "Eithe" in err),
        "table_fields": ["OwnersData.Name", "OwnersData.Organization"],
        "constraint_type": "Programmatic (Pydantic value_error model-level)",
        "signature": r"(Either name or organization must be provided|Value error, Eithe)",
        "plain_english": "An owner record has neither a person name nor an organization name. At least one must be provided.",
        "owner": "Data Services",
        "owner_rationale": "Data Services maintains owner records.",
        "diagnose": "1. Locate owner record linked to the PointID.\n2. Check OSE water rights records or NMBGMR files for owner name.\n3. Determine if owner is individual or organization.",
        "resolution_paths": "Path 1 (Data Services): Enter owner's name or organization from official records.\nPath 2 (Dev): If owner truly unknown, add explicit 'UNKNOWN OWNER' organization entry.\nNOT ALLOWED: Leaving both name and organization blank.",
        "amp_needs": "N/A.",
        "dev_needs": "Require at least one of name/organization at model level.\nUnit tests: both null → error; name only → accepted; org only → accepted; both → accepted.",
    },
    {
        "id": "VALIDATION.owners_data.organization.invalid",
        "match": lambda tf, err: "OwnersData" in tf and err.startswith("Invalid organization:"),
        "table_fields": ["OwnersData.Organization"],
        "constraint_type": "Business Logic (import script lookup)",
        "signature": r"Invalid organization: .+",
        "plain_english": "Organization name not in the approved organization lookup table. Affects many water utilities, ranches, and government agencies not yet registered.",
        "owner": "Data Services",
        "owner_rationale": "Data Services manages the organization registry.",
        "diagnose": "1. Check OwnersData.Organization against organization lookup table.\n2. Search for close matches (abbreviations, spelling variants).\n3. For government agencies, check if parent-agency entry exists.",
        "resolution_paths": "Path 1 (Data Services): Register organization with full name, type, and aliases.\nPath 2 (Data Services): Map to existing entry if known alias or sub-unit.\nPath 3 (Dev): Build bulk organization-registration tool to process the full list at once.\nNOT ALLOWED: Using free-text organization names that bypass the lookup table.",
        "amp_needs": "N/A.",
        "dev_needs": "Show invalid org name in error; provide fuzzy-match suggestion if close entry exists.\nUnit tests: unregistered org → error; registered → accepted.\nMigration: batch-register all known organizations before re-running imports.",
    },
    {
        "id": "VALIDATION.owners_data.organization.fk_missing",
        "match": lambda tf, err: "OwnersData" in tf and "is not present in table" in err,
        "table_fields": ["OwnersData.Organization"],
        "constraint_type": "Database (PostgreSQL FK constraint C:23503)",
        "signature": r"Key \(organization\)=\(.+\) is not present in table",
        "plain_english": "Foreign key violation — the organization value exists in source data but has no matching row in the organizations reference table.",
        "owner": "Data Services",
        "owner_rationale": "Same as Invalid organization — the organization must be registered first.",
        "diagnose": "Same as VALIDATION.owners_data.organization.invalid.",
        "resolution_paths": "Same as VALIDATION.owners_data.organization.invalid.",
        "amp_needs": "N/A.",
        "dev_needs": "FK error should surface the organization name (not just the key value) for easier debugging.",
    },
    {
        "id": "VALIDATION.equipment.date_installed.required",
        "match": lambda tf, err: "DateInstalled" in tf and "Installation Date cannot be None" in err,
        "table_fields": ["Equipment.DateInstalled"],
        "constraint_type": "Business Logic (import script check)",
        "signature": r"row\.SerialNo=\S+\. Installation Date cannot be None",
        "plain_english": "Equipment has no installation date. The schema requires DateInstalled before sensor deployment records can be linked.",
        "owner": "AMP",
        "owner_rationale": "AMP field staff installed the equipment and are the primary source of truth for the install date.",
        "diagnose": "1. Search AMP UI for the SerialNo.\n2. Check field notebooks, deployment logs, or site visit records.\n3. Fallback: use date of first continuous measurement as approximation (flag as estimated).",
        "resolution_paths": "Path 1 (AMP – preferred): Enter confirmed installation date in AMP UI.\nPath 2 (AMP – fallback): If exact date unknown, use first continuous measurement date and mark as 'estimated'.\nPath 3 (AMP): If instrument never had continuous data, mark for deletion and notify Dev.\nNOT ALLOWED: Setting DateInstalled to a system default without field verification.",
        "amp_needs": "Format: YYYY-MM-DD. Must be ≤ start of first deployment record for that serial number.",
        "dev_needs": "Reject import if DateInstalled is null; surface clear error naming SerialNo.\nUnit tests: null DateInstalled → error with SerialNo; DateInstalled after first measurement → warning.\nMigration: quarantine existing null-date records, do not delete.",
    },
    {
        "id": "VALIDATION.equipment.date_installed.estimated",
        "match": lambda tf, err: "DateInstalled" in tf and err.startswith("Estimated installation date="),
        "table_fields": ["Equipment.DateInstalled"],
        "constraint_type": "Business Logic (import script inference warning)",
        "signature": r"Estimated installation date=\d{4}-\d{2}-\d{2}\. Is this correct\?",
        "plain_english": "System inferred an installation date from available data rather than a confirmed field record. AMP must verify whether the estimate is acceptable.",
        "owner": "AMP",
        "owner_rationale": "Only AMP staff can confirm whether the estimated date matches the actual field installation.",
        "diagnose": "1. Note the estimated date shown in the error.\n2. Cross-reference with deployment logs, field visit records, or purchase orders.\n3. If estimate is within ±7 days of known install, likely acceptable.",
        "resolution_paths": "Path 1 (AMP): Confirm date is correct → mark Reviewed = Yes; no other action needed.\nPath 2 (AMP): Date is incorrect → enter correct date in AMP UI and mark Fixed = Yes.\nNOT ALLOWED: Leaving the row unreviewed — the estimate will be imported as fact on next run.",
        "amp_needs": "Confirmed or corrected installation date in YYYY-MM-DD format. Flag whether 'confirmed from field records' or 'best estimate'.",
        "dev_needs": "Store a boolean flag (is_estimated) alongside DateInstalled so downstream queries can filter on confidence level.",
    },
    {
        "id": "VALIDATION.equipment.recording_interval.estimated",
        "match": lambda tf, err: "RecordingInterval" in tf and err.startswith("Estimated recording interval="),
        "table_fields": ["Equipment.RecordingInterval"],
        "constraint_type": "Business Logic (import script inference warning)",
        "signature": r"Estimated recording interval=\d+ (hour|minute)\. Is this correct\?",
        "plain_english": "System estimated the sensor's recording interval from measurement timestamps rather than instrument configuration. AMP must confirm whether this matches the actual programmed interval.",
        "owner": "AMP",
        "owner_rationale": "AMP programmed the logger. Incorrect interval causes time-series alignment errors across all downstream analyses.",
        "diagnose": "1. Check sensor configuration file or field programming records.\n2. Compare estimated interval against raw data file header.\n3. Watch for irregular intervals (e.g. 11h, 13h) indicating clock drift or partial download.",
        "resolution_paths": "Path 1 (AMP): Interval is correct → confirm in AMP UI; mark Reviewed = Yes.\nPath 2 (AMP): Interval is incorrect → update RecordingInterval in AMP UI.\nPath 3 (Dev): If genuinely irregular (variable-rate logger), update schema to allow NULL or 'variable'.\nNOT ALLOWED: Accepting obvious wrong intervals (e.g. 11h for standard hourly logger) without investigation.",
        "amp_needs": "Integer value and unit (minutes or hours). Acceptable: 15 min, 30 min, 1h, 2h, 4h, 6h, 12h, 24h. Document any non-standard interval.",
        "dev_needs": "Store interval as integer + unit enum; reject free-text.\nUnit tests: fractional-hour estimates (11h, 13h) → 'suspicious interval' warning, not hard error.",
    },
    {
        "id": "VALIDATION.equipment.recording_interval.no_measurements",
        "match": lambda tf, err: "RecordingInterval" in tf and "No measurements found for PointID" in err,
        "table_fields": ["Equipment.RecordingInterval"],
        "constraint_type": "Business Logic (import script check)",
        "signature": r"name=\d+, row\.SerialNo=\S+\. error=No measurements found for PointID: \S+",
        "plain_english": "System could not estimate a recording interval because no measurement records were found for this instrument at the given PointID. Equipment record exists but has no associated data.",
        "owner": "AMP",
        "owner_rationale": "AMP must determine whether data was collected but not uploaded, was lost, or whether the equipment record was created in error.",
        "diagnose": "1. Search AMP UI for SerialNo and PointID combination.\n2. Check raw data file directories for files matching this logger serial.\n3. Determine whether a field deployment actually occurred.",
        "resolution_paths": "Path 1 (AMP): Data exists but not uploaded → upload raw data file and re-run import.\nPath 2 (AMP): Logger deployed but data lost → mark 'no data' and enter RecordingInterval from config records if known.\nPath 3 (AMP): Record created in error → flag for deletion by Dev.\nNOT ALLOWED: Fabricating a RecordingInterval when no data and no config record exists.",
        "amp_needs": "For each SerialNo/PointID: confirm one of — (a) data file location for upload, (b) confirmed interval from logger config, or (c) record-deletion request.",
        "dev_needs": "When no measurements exist, emit WARNING (not ERROR) and allow equipment record to be saved with RecordingInterval = NULL.",
    },
    {
        "id": "VALIDATION.equipment.equipment_type.invalid",
        "match": lambda tf, err: "EquipmentType" in tf and err.startswith("Invalid sensor_type:"),
        "table_fields": ["Equipment.EquipmentType"],
        "constraint_type": "Business Logic (lexicon lookup)",
        "signature": r"Invalid sensor_type: (Diver Cable|DiverLink)",
        "plain_english": "Sensor type ('Diver Cable' or 'DiverLink') not present in the equipment-type lookup table.",
        "owner": "Dev / AMP",
        "owner_rationale": "AMP must clarify whether these are new distinct categories or aliases for existing types. Dev adds to lexicon once approved.",
        "diagnose": "1. Check current equipment_type lookup table for existing entries.\n2. Ask AMP: Is 'Diver Cable' a cable accessory or a standalone sensor type?\n3. Search field documentation for how these instruments are catalogued.",
        "resolution_paths": "Path 1 (Dev + AMP): AMP confirms new distinct types → Dev adds to equipment_type lexicon.\nPath 2 (AMP): These map to existing 'pressure transducer' → AMP corrects source data.\nPath 3 (Dev): Add as aliases pointing to canonical entry.\nNOT ALLOWED: Silently mapping unknown types to 'Other' without documented rationale.",
        "amp_needs": "Written description of 'Diver Cable' and 'DiverLink': what they measure, how they differ from existing types, and whether they should appear separately in the UI.",
        "dev_needs": "After lexicon update, re-run import; new types should be accepted.\nUnit tests: known type → accepted; unknown → clear error with type name; new type after lexicon update → accepted.\nMigration: backfill existing records to canonical lexicon ID.",
    },
    {
        "id": "VALIDATION.deployment.no_deployment_gap",
        "match": lambda tf, err: re.search(r"no deployment between", err) is not None,
        "table_fields": ["transducer_observation_block", "WaterLevelsContinuous_Pressure.DateMeasured", "WaterLevelsContinuous_Acoustic.DateMeasured"],
        "constraint_type": "Business Logic (import script check)",
        "signature": r"no deployment between \d{4}-\d{2}-\d{2}.*and \d{4}-\d{2}-\d{2}",
        "plain_english": "Observation data exists for a PointID during a time window when no sensor deployment record is registered. The system cannot associate the readings with an instrument.",
        "owner": "Dev / Data Services",
        "owner_rationale": "Data Services (Kelsey & Ethan) must determine which sensor was physically at the location during the gap. Dev may need to adjust deployment date boundaries.",
        "diagnose": "1. In AMP UI navigate to PointID > Equipment > Deployments; inspect dates around the flagged window.\n2. Check field visit records for sensor retrieval/reinstallation events near gap dates.\n3. For very short gaps (same day, few hours), check UTC offset errors in deployment timestamps.",
        "resolution_paths": "Path 1 (Data Services): Identify missing deployment → create deployment record with correct sensor and dates.\nPath 2 (Dev): Gap due to ±12h timezone offset error → correct deployment timestamps.\nPath 3 (Data Services): Data during gap is invalid → mark observations as 'rejected'.\nNOT ALLOWED: Creating dummy deployment without knowing which instrument was deployed.",
        "amp_needs": "For each PointID + date range: identify serial number of sensor deployed during that period and exact install/removal dates.",
        "dev_needs": "When no deployment covers a time window, log specific PointID and date range; do not silently skip.\nUnit tests: observation within deployment → accepted; observation outside all deployments → error with date range shown.",
    },
    {
        "id": "VALIDATION.deployment.no_deployments_at_all",
        "match": lambda tf, err: err.strip() == "no deployments",
        "table_fields": ["transducer_observation_block"],
        "constraint_type": "Business Logic (import script check)",
        "signature": r"^no deployments$",
        "plain_english": "A PointID has observation data but zero deployment records. Usually a downstream consequence of a missing Equipment.DateInstalled — fix that first.",
        "owner": "AMP",
        "owner_rationale": "This error is a downstream consequence of VALIDATION.equipment.date_installed.required. Fix that first.",
        "diagnose": "1. Check if same PointID appears in Equipment.DateInstalled error list.\n2. If DateInstalled is present, check whether a deployment record was created in AMP UI.\n3. Verify PointID is not retired/duplicate.",
        "resolution_paths": "Path 1 (AMP – primary): Resolve Equipment.DateInstalled error first; deployments should then auto-populate.\nPath 2 (AMP): If DateInstalled known but no deployment created, manually add deployment in AMP UI.\nNOT ALLOWED: Importing observations for a PointID with no deployment history.",
        "amp_needs": "Installation date and sensor serial number so a deployment record can be created.",
        "dev_needs": "Surface 'no deployments' as a dependency error linked to DateInstalled validation.\nUnit tests: PointID with DateInstalled but no deployment → distinct warning; PointID with neither → chained error message.",
    },
    {
        "id": "VALIDATION.location.plss.invalid",
        "match": lambda tf, err: "Township" in tf and "is not a valid PLSS" in err,
        "table_fields": ["Location.Township", "Location.TownshipDirection", "Location.Range", "Location.Section"],
        "constraint_type": "Business Logic (import script PLSS validation)",
        "signature": r"T\d+[NS]\.R\d+[EW]\.S\d+\.\d+ is not a valid PLSS",
        "plain_english": "The PLSS location code is malformed or references a section/township/range combination that does not exist in New Mexico.",
        "owner": "AMP",
        "owner_rationale": "AMP or the original data submitter entered the PLSS coordinates; only AMP can verify the correct location.",
        "diagnose": "1. Parse the PLSS string: Township, Direction, Range, Direction, Section, QQ.\n2. Cross-check against BLM General Land Office survey plats for NM.\n3. Use a PLSS web service (e.g. geocommunicator.gov) to validate.",
        "resolution_paths": "Path 1 (AMP): Correct the PLSS string using GPS coordinates and a PLSS lookup tool.\nPath 2 (Dev): Integrate a PLSS validation API to give real-time feedback in the UI.\nNOT ALLOWED: Importing an invalid PLSS code — corrupts spatial queries.",
        "amp_needs": "Verified Township, Direction, Range, Direction, Section for each flagged location.",
        "dev_needs": "Validate PLSS code against reference dataset on import.\nUnit tests: valid PLSS → accepted; invalid section number → error; non-existent township → error.",
    },
]


def classify_row(table_field: str, error: str) -> dict | None:
    for ec in ERROR_CLASSES:
        try:
            if ec["match"](table_field, error):
                return ec
        except Exception:
            continue
    return None


def classify_errors(error_rows: list[dict]) -> dict[str, dict]:
    """
    Returns a dict keyed by error_class_id with:
        ec:          the error class definition
        point_ids:   list of affected PointIDs
        models:      set of model names affected
    """
    classes: dict[str, dict] = {}

    for row in error_rows:
        table_field = row["table_field"]
        error = row["error"]
        ec = classify_row(table_field, error)

        if ec is None:
            key = f"UNCLASSIFIED.{row['table']}.{row['field']}"
            if key not in classes:
                classes[key] = {
                    "ec": {
                        "id": key,
                        "table_fields": [table_field],
                        "constraint_type": "Unclassified — mapping needed",
                        "signature": error[:120],
                        "plain_english": f"Unclassified error in {table_field}: {error[:200]}",
                        "owner": "Mapping Needed",
                        "owner_rationale": "This error class has not yet been mapped to a resolution path.",
                        "diagnose": "Review raw error text and determine root cause.",
                        "resolution_paths": "Escalate to Dev + AMP for triage.",
                        "amp_needs": "To be determined.",
                        "dev_needs": "To be determined.",
                    },
                    "point_ids": [],
                    "models": set(),
                }
        else:
            key = ec["id"]
            if key not in classes:
                classes[key] = {"ec": ec, "point_ids": [], "models": set()}

        classes[key]["point_ids"].append(row["point_id"])
        classes[key]["models"].add(row["model"])

    return classes


# ── GOOGLE SHEETS ──────────────────────────────────────────────────────────────

def get_service():
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_FILE, scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds)


def get_or_create_sheet(service, title: str) -> int:
    """Return sheetId, creating the tab if it doesn't exist."""
    ss = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    for sheet in ss.get("sheets", []):
        if sheet["properties"]["title"] == title:
            return sheet["properties"]["sheetId"]
    body = {"requests": [{"addSheet": {"properties": {"title": title}}}]}
    resp = service.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID, body=body
    ).execute()
    return resp["replies"][0]["addSheet"]["properties"]["sheetId"]


def read_sheet(service, sheet_name: str) -> list[list]:
    try:
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=SPREADSHEET_ID, range=f"'{sheet_name}'!A1:Z10000")
            .execute()
        )
        return result.get("values", [])
    except Exception:
        return []


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


def append_rows(service, sheet_name: str, rows: list[list]):
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{sheet_name}'!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


def format_sheet(service, sheet_id: int, num_cols: int, freeze_rows: int = 1,
                 header_color: tuple = (0.2, 0.4, 0.7)):
    r, g, b = header_color
    requests = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": freeze_rows},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": freeze_rows,
                    "startColumnIndex": 0,
                    "endColumnIndex": num_cols,
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
                    "startRowIndex": freeze_rows,
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
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": num_cols,
                },
                "properties": {"pixelSize": 280},
                "fields": "pixelSize",
            }
        },
    ]
    service.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID, body={"requests": requests}
    ).execute()


# ── CATALOG SHEET ──────────────────────────────────────────────────────────────

CATALOG_HEADERS = [
    "Error Class ID",
    "Constraint Type",
    "Affected Tables / Fields",
    "Signature / Match Pattern",
    "Plain-English Meaning",
    "Default Owner",
    "Owner Rationale",
    "How to Diagnose",
    "Approved Resolution Paths",
    "What AMP Needs to Provide",
    "What Dev Needs to Implement",
    # Preserved human columns
    "Jira Ticket",
    "Jira Status",
    "AMP Reviewed By",
    "AMP Reviewed Date",
    "Dev Fix Deployed Date",
    "Notes",
]


def update_catalog(service, classified: dict[str, dict], run_date: str):
    """
    Write the Error Catalog sheet. Reads existing Jira/review columns first
    and preserves them — only the error-definition columns are overwritten.
    """
    existing = read_sheet(service, SHEET_CATALOG)

    # Build a lookup of preserved values keyed by error class ID
    preserved: dict[str, list] = {}
    if existing and len(existing) > 0:
        try:
            header = existing[0]
            id_col = header.index("Error Class ID")
            preserved_start = header.index("Jira Ticket")
            for row in existing[1:]:
                if len(row) > id_col:
                    eid = row[id_col]
                    preserved_vals = row[preserved_start:] if len(row) > preserved_start else []
                    preserved[eid] = preserved_vals
        except (ValueError, IndexError):
            pass  # No preserved columns yet — first run

    # Build output rows
    output = [CATALOG_HEADERS]
    ordered_keys = [ec["id"] for ec in ERROR_CLASSES] + sorted(
        k for k in classified if k.startswith("UNCLASSIFIED")
    )

    for key in ordered_keys:
        if key not in classified:
            continue
        ec = classified[key]["ec"]
        prev = preserved.get(key, [])

        row = [
            ec.get("id", ""),
            ec.get("constraint_type", ""),
            ", ".join(ec.get("table_fields", [])),
            ec.get("signature", ""),
            ec.get("plain_english", ""),
            ec.get("owner", ""),
            ec.get("owner_rationale", ""),
            ec.get("diagnose", ""),
            ec.get("resolution_paths", ""),
            ec.get("amp_needs", ""),
            ec.get("dev_needs", ""),
        ]

        # Pad preserved columns
        for i in range(len(PRESERVED_CATALOG_COLS)):
            row.append(prev[i] if i < len(prev) else "")

        output.append(row)

    write_sheet(service, SHEET_CATALOG, output)
    sheet_id = get_or_create_sheet(service, SHEET_CATALOG)
    format_sheet(service, sheet_id, len(CATALOG_HEADERS))
    print(f"  ✅ '{SHEET_CATALOG}' updated — {len(output) - 1} error classes.")


# ── SNAPSHOT SHEET ─────────────────────────────────────────────────────────────

SNAPSHOT_HEADERS = [
    "Run Date",
    "Error Class ID",
    "Constraint Type",
    "Owner",
    "Occurrence Count",
    "Affected Models",
    "Sample PointIDs (up to 10)",
    "Jira Ticket",   # copied from catalog for easy filtering
]


def append_snapshot(service, classified: dict[str, dict], run_date: str):
    """
    Append one row per error class for this run.
    Creates the header row if the sheet is empty.
    """
    existing = read_sheet(service, SHEET_SNAPSHOTS)

    # Write header if sheet is empty
    if not existing:
        write_sheet(service, SHEET_SNAPSHOTS, [SNAPSHOT_HEADERS])
        sheet_id = get_or_create_sheet(service, SHEET_SNAPSHOTS)
        format_sheet(service, sheet_id, len(SNAPSHOT_HEADERS),
                     header_color=(0.1, 0.5, 0.3))

    # Read catalog to get Jira ticket numbers
    catalog_rows = read_sheet(service, SHEET_CATALOG)
    jira_lookup: dict[str, str] = {}
    if catalog_rows and len(catalog_rows) > 1:
        try:
            hdr = catalog_rows[0]
            id_col = hdr.index("Error Class ID")
            jira_col = hdr.index("Jira Ticket")
            for r in catalog_rows[1:]:
                if len(r) > jira_col:
                    jira_lookup[r[id_col]] = r[jira_col]
        except (ValueError, IndexError):
            pass

    new_rows = []
    ordered_keys = [ec["id"] for ec in ERROR_CLASSES] + sorted(
        k for k in classified if k.startswith("UNCLASSIFIED")
    )

    for key in ordered_keys:
        if key not in classified:
            continue
        info = classified[key]
        ec = info["ec"]
        point_ids = info["point_ids"]
        models = info["models"]
        unique_points = list(dict.fromkeys(point_ids))[:10]

        new_rows.append([
            run_date,
            ec.get("id", ""),
            ec.get("constraint_type", ""),
            ec.get("owner", ""),
            len(point_ids),
            ", ".join(sorted(models)),
            ", ".join(unique_points),
            jira_lookup.get(ec.get("id", ""), ""),
        ])

    append_rows(service, SHEET_SNAPSHOTS, new_rows)
    print(f"  ✅ '{SHEET_SNAPSHOTS}' — {len(new_rows)} rows appended for run {run_date}.")


# ── RUN SUMMARY SHEET ──────────────────────────────────────────────────────────

SUMMARY_HEADERS = [
    "Run Date",
    "Model",
    "Label",
    "Input Count",
    "Cleaned Count",
    "Transferred",
    "Issue %",
    "Errors Not Transferred",
]


def append_run_summary(service, model_stats: list[dict], run_date: str):
    existing = read_sheet(service, SHEET_SUMMARY)

    if not existing:
        write_sheet(service, SHEET_SUMMARY, [SUMMARY_HEADERS])
        sheet_id = get_or_create_sheet(service, SHEET_SUMMARY)
        format_sheet(service, sheet_id, len(SUMMARY_HEADERS),
                     header_color=(0.5, 0.2, 0.5))

    new_rows = []
    for s in model_stats:
        not_transferred = s["cleaned_count"] - s["transferred"]
        new_rows.append([
            run_date,
            s["model"],
            s["label"],
            s["input_count"],
            s["cleaned_count"],
            s["transferred"],
            round(s["issue_pct"], 4),
            max(not_transferred, 0),
        ])

    append_rows(service, SHEET_SUMMARY, new_rows)
    print(f"  ✅ '{SHEET_SUMMARY}' — {len(new_rows)} model rows appended for run {run_date}.")




# ── POINTID TRACKING SHEET ────────────────────────────────────────────────────

TRACKING_HEADERS = [
    "Error Class ID",
    "Constraint Type",
    "Owner",
    "PointID",
    "Model",
    "Table",
    "Field",
    "First Seen",
    "Last Seen",
    "Still Failing",
    # Human-filled — never overwritten by the script
    "AMP Reviewed By",
    "AMP Reviewed Date",
    "AMP Notes",
    "Jira Ticket",
]

# Index where human-filled columns begin (must stay in sync with TRACKING_HEADERS)
TRACKING_HUMAN_START = 10  # "AMP Reviewed By" onwards


def update_point_id_tracking(service, error_rows: list, run_date: str):
    """
    Maintain a stable per-(error_class, PointID) tracking sheet.

    Rules
    -----
    - New (class, PointID) pairs → appended with First Seen = run_date.
    - Existing rows → Last Seen updated; Still Failing flips Yes/No automatically.
    - Human-filled columns (AMP Reviewed By, Date, Notes, Jira) → NEVER touched.
    """
    sheet_id = get_or_create_sheet(service, SHEET_TRACKING)
    existing_rows = read_sheet(service, SHEET_TRACKING)

    # ── Build lookup of existing rows ────────────────────────────────────────
    existing_lookup: dict = {}
    if existing_rows and "Error Class ID" in existing_rows[0]:
        header = existing_rows[0]
        try:
            id_col  = header.index("Error Class ID")
            pid_col = header.index("PointID")
        except ValueError:
            id_col, pid_col = 0, 3
        for i, row in enumerate(existing_rows[1:], start=1):
            if len(row) > max(id_col, pid_col):
                existing_lookup[(row[id_col], row[pid_col])] = i

    # ── Build set of (class_id, point_id) failing THIS run ───────────────────
    failing_this_run: dict = {}
    for row in error_rows:
        ec = classify_row(row["table_field"], row["error"])
        class_id = ec["id"] if ec else f"UNCLASSIFIED.{row['table']}.{row['field']}"
        key = (class_id, row["point_id"])
        if key not in failing_this_run:
            failing_this_run[key] = {
                "class_id":        class_id,
                "constraint_type": ec.get("constraint_type", "Unclassified") if ec else "Unclassified",
                "owner":           ec.get("owner", "Mapping Needed") if ec else "Mapping Needed",
                "point_id":        row["point_id"],
                "model":           row["model"],
                "table":           row["table"],
                "field":           row["field"],
            }

    # ── Column shortcuts ──────────────────────────────────────────────────────
    COL_LAST_SEEN     = TRACKING_HEADERS.index("Last Seen")
    COL_STILL_FAILING = TRACKING_HEADERS.index("Still Failing")

    # ── Build output — always start with the canonical header ─────────────────
    output = [TRACKING_HEADERS]

    seen_keys: set = set()
    for row in existing_rows[1:]:
        if len(row) < 4:
            output.append(row)
            continue
        try:
            class_id = row[TRACKING_HEADERS.index("Error Class ID")]
            point_id = row[TRACKING_HEADERS.index("PointID")]
        except (ValueError, IndexError):
            output.append(row)
            continue

        key = (class_id, point_id)
        seen_keys.add(key)

        # Pad to full width so all indices are safe
        padded = row + [""] * (len(TRACKING_HEADERS) - len(row))

        if key in failing_this_run:
            padded[COL_LAST_SEEN]     = run_date
            padded[COL_STILL_FAILING] = "Yes"
        else:
            padded[COL_STILL_FAILING] = "No"
            # Last Seen stays at the last run it appeared — intentional

        output.append(padded)

    # ── Append brand-new (class, PointID) pairs ───────────────────────────────
    # Preserve defined error-class order, then alphabetical for unclassified
    ordered_new: list = []
    for ec_def in ERROR_CLASSES:
        for key in failing_this_run:
            if key[0] == ec_def["id"] and key not in seen_keys and key not in ordered_new:
                ordered_new.append(key)
    for key in sorted(failing_this_run):
        if key not in seen_keys and key not in ordered_new:
            ordered_new.append(key)

    new_count = 0
    for key in ordered_new:
        info = failing_this_run[key]
        new_row = [""] * len(TRACKING_HEADERS)
        new_row[TRACKING_HEADERS.index("Error Class ID")]   = info["class_id"]
        new_row[TRACKING_HEADERS.index("Constraint Type")]  = info["constraint_type"]
        new_row[TRACKING_HEADERS.index("Owner")]            = info["owner"]
        new_row[TRACKING_HEADERS.index("PointID")]          = info["point_id"]
        new_row[TRACKING_HEADERS.index("Model")]            = info["model"]
        new_row[TRACKING_HEADERS.index("Table")]            = info["table"]
        new_row[TRACKING_HEADERS.index("Field")]            = info["field"]
        new_row[TRACKING_HEADERS.index("First Seen")]       = run_date
        new_row[TRACKING_HEADERS.index("Last Seen")]        = run_date
        new_row[TRACKING_HEADERS.index("Still Failing")]    = "Yes"
        # Human columns left blank on purpose
        output.append(new_row)
        new_count += 1

    write_sheet(service, SHEET_TRACKING, output)
    format_tracking_sheet(service, sheet_id)

    still_failing = sum(1 for r in output[1:] if len(r) > COL_STILL_FAILING and r[COL_STILL_FAILING] == "Yes")
    resolved      = sum(1 for r in output[1:] if len(r) > COL_STILL_FAILING and r[COL_STILL_FAILING] == "No")
    print(f"  ✅ '{SHEET_TRACKING}' — {new_count} new PointIDs added | "
          f"{still_failing} still failing | {resolved} resolved.")


def format_tracking_sheet(service, sheet_id: int):
    """Frozen header, per-column widths, yellow human columns, red/green conditional formatting."""
    num_cols = len(TRACKING_HEADERS)
    col_widths = {
        "Error Class ID":    320,
        "Constraint Type":   200,
        "Owner":             160,
        "PointID":           100,
        "Model":             130,
        "Table":             130,
        "Field":             130,
        "First Seen":        100,
        "Last Seen":         100,
        "Still Failing":     100,
        "AMP Reviewed By":   140,
        "AMP Reviewed Date": 120,
        "AMP Notes":         260,
        "Jira Ticket":       110,
    }

    requests = [
        # Freeze header row
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
        # Header: dark red background, white bold text
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0, "endRowIndex": 1,
                    "startColumnIndex": 0, "endColumnIndex": num_cols,
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {
                            "bold": True,
                            "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                        },
                        "backgroundColor": {"red": 0.7, "green": 0.2, "blue": 0.2},
                        "horizontalAlignment": "CENTER",
                    }
                },
                "fields": "userEnteredFormat(textFormat,backgroundColor,horizontalAlignment)",
            }
        },
        # Data cells: wrap + top-align
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
        # Light yellow tint for human-filled columns
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "startColumnIndex": TRACKING_HUMAN_START,
                    "endColumnIndex": num_cols,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 1.0, "green": 0.98, "blue": 0.80},
                    }
                },
                "fields": "userEnteredFormat(backgroundColor)",
            }
        },
    ]

    # Per-column widths
    for i, col_name in enumerate(TRACKING_HEADERS):
        requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": i,
                    "endIndex": i + 1,
                },
                "properties": {"pixelSize": col_widths.get(col_name, 150)},
                "fields": "pixelSize",
            }
        })

    sf_col = TRACKING_HEADERS.index("Still Failing")

    # Conditional: "Yes" → pink/red
    requests.append({
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [{
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "startColumnIndex": sf_col,
                    "endColumnIndex": sf_col + 1,
                }],
                "booleanRule": {
                    "condition": {
                        "type": "TEXT_EQ",
                        "values": [{"userEnteredValue": "Yes"}],
                    },
                    "format": {
                        "backgroundColor": {"red": 1.0, "green": 0.80, "blue": 0.80},
                        "textFormat": {"bold": True},
                    },
                },
            },
            "index": 0,
        }
    })

    # Conditional: "No" → light green
    requests.append({
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [{
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "startColumnIndex": sf_col,
                    "endColumnIndex": sf_col + 1,
                }],
                "booleanRule": {
                    "condition": {
                        "type": "TEXT_EQ",
                        "values": [{"userEnteredValue": "No"}],
                    },
                    "format": {
                        "backgroundColor": {"red": 0.80, "green": 0.95, "blue": 0.80},
                        "textFormat": {"bold": True},
                    },
                },
            },
            "index": 1,
        }
    })

    service.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID, body={"requests": requests}
    ).execute()

# ── MAIN ───────────────────────────────────────────────────────────────────────

def find_latest_metrics_file(folder: str) -> str:
    """Return the path of the most recently modified metrics CSV in the folder."""
    folder_path = Path(folder)
    files = sorted(
        [f for f in folder_path.glob("transfer_metrics_metrics_*.csv")],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if not files:
        raise FileNotFoundError(f"No transfer_metrics_metrics_*.csv files found in '{folder}'")
    return str(files[0])


def main():
    parser = argparse.ArgumentParser(description="Parse transfer metrics and update Google Sheets.")
    parser.add_argument(
        "--file", "-f",
        help="Path to a specific transfer_metrics_metrics_<date>.csv file. "
             "If omitted, the latest file in --folder is used.",
        default=None,
    )
    parser.add_argument(
        "--folder",
        help=f"Folder containing metrics files (default: {METRICS_FOLDER})",
        default=METRICS_FOLDER,
    )
    args = parser.parse_args()

    filepath = args.file or find_latest_metrics_file(args.folder)
    print(f"📂 Parsing: {filepath}")

    run_date, model_stats, error_rows = parse_metrics_file(filepath)
    print(f"   Run date:    {run_date}")
    print(f"   Model stats: {len(model_stats)} models")
    print(f"   Error rows:  {len(error_rows)} individual errors")

    classified = classify_errors(error_rows)
    print(f"   Error classes: {len(classified)} unique classes")

    print("\n🔗 Connecting to Google Sheets…")
    service = get_service()

    # Ensure all sheets exist before writing
    for sheet_name in [SHEET_CATALOG, SHEET_SNAPSHOTS, SHEET_SUMMARY, SHEET_TRACKING]:
        get_or_create_sheet(service, sheet_name)

    print("\n📋 Updating sheets…")
    update_catalog(service, classified, run_date)
    append_snapshot(service, classified, run_date)
    append_run_summary(service, model_stats, run_date)
    update_point_id_tracking(service, error_rows, run_date)

    print("\n✨ Done.")


if __name__ == "__main__":
    main()
