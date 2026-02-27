"""
AMP Error Catalog Generator
============================
Reads raw AMP review data from a Google Sheet, classifies each row into
error classes, and writes a new "4.1 Error Catalog" sheet with the 8
columns defined in the spec.

Setup
-----
1. pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client
2. Create a Google Cloud project, enable the Sheets API, and download
   credentials.json (OAuth 2.0 Desktop app) OR create a Service Account
   key (service_account.json) and share the spreadsheet with its email.
3. Set the constants below.

Usage
-----
    python amp_error_catalog.py
"""

import re
import json
from collections import defaultdict

# ── Google Sheets helpers ──────────────────────────────────────────────────────
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── CONFIGURATION ─────────────────────────────────────────────────────────────
SPREADSHEET_ID   = "1iQzeKqRWHIKbnNptH_wRQEpJ_pt1rI00ax9d5BhDAhU"          # from the URL
SOURCE_SHEET     = "AMP_review"                         # tab that holds raw data
CATALOG_SHEET    = "4.1 Error Catalog"                  # new tab to create/overwrite
CREDENTIALS_FILE = "transfermetrics_service_account.json"               # or change auth below
SCOPES           = ["https://www.googleapis.com/auth/spreadsheets"]

# Source column indices (0-based) in the raw sheet
COL_TABLE_FIELD  = 0   # NMAquifer_Table.Field
COL_POINT_ID     = 1   # PointID
COL_ERROR        = 2   # Error message
COL_OWNER        = 3   # Needs Review By
COL_NOTES        = 9   # Notes
COL_PREF_FIX     = 10  # Preferred fix


# ── ERROR CLASS DEFINITIONS ────────────────────────────────────────────────────
# Each entry describes ONE logical error class.
# Fields map directly to the 8 catalog columns.
ERROR_CLASSES = [
    {
        "id": "INTEGRITY.transducer_block.duplicate_key",
        "match": lambda tf, err: "uq_transducer_block_status_parameter_time" in err,
        "table_fields": ["transducer_observation_block"],
        "signature": r"pg8000.dbapi.IntegrityError.*uq_transducer_block_status_parameter_time.*Key \(review_status, parameter_id, start_datetime, end_datetime\)=\(.*\) already exists",
        "plain_english": (
            "A transducer observation block for this parameter and time window already "
            "exists with 'approved' status. Inserting a second block with identical "
            "(review_status, parameter_id, start_datetime, end_datetime) violates the "
            "unique constraint on transducer_observation_block."
        ),
        "owner": "Dev / Data Services",
        "owner_rationale": (
            "Duplicate blocks usually arise when the ETL pipeline re-runs without "
            "deduplication logic, or when an approved block's dates overlap a new import. "
            "Data Services must confirm which block is canonical; Dev must add idempotent "
            "upsert logic."
        ),
        "diagnose": (
            "1. Query transducer_observation_block WHERE review_status='approved' AND "
            "parameter_id=<id> AND start_datetime=<start> AND end_datetime=<end>.\n"
            "2. Compare row counts and data values between the two blocks.\n"
            "3. Check ETL run logs for duplicate import runs on the same date range.\n"
            "4. Look for the PointID in the AMP UI under Equipment > Sensor Deployments."
        ),
        "resolution_paths": (
            "Path 1 (Data Services): Identify the canonical block and soft-delete or archive "
            "the duplicate. Never delete both.\n"
            "Path 2 (Dev): Refactor the import pipeline to use INSERT ... ON CONFLICT DO UPDATE "
            "(upsert) so re-runs are idempotent.\n"
            "Path 3 (Dev): Add a pre-import deduplication step that checks for existing "
            "approved blocks before insertion.\n"
            "NOT ALLOWED: Deleting both blocks without confirming which holds the correct data."
        ),
        "amp_needs": (
            "N/A — AMP cannot resolve database-level duplicate keys. AMP should flag the "
            "PointID if it knows the observation data is identical (confirming true duplicate) "
            "or different (indicating a merge conflict)."
        ),
        "dev_needs": (
            "Expected behavior: upsert on (review_status, parameter_id, start_datetime, "
            "end_datetime); do not insert if an approved block already covers the range.\n"
            "Unit tests: (a) insert identical block twice → second is no-op; "
            "(b) insert overlapping block with different data → raise clear business error.\n"
            "Migration safety: audit existing duplicates before deploying upsert logic; "
            "resolve conflicts in a separate migration script."
        ),
    },
    {
        "id": "VALIDATION.equipment.date_installed.required",
        "match": lambda tf, err: tf == "Equipment.DateInstalled" and "Installation Date cannot be None" in err,
        "table_fields": ["Equipment.DateInstalled"],
        "signature": r"row\.SerialNo=\S+\. Installation Date cannot be None",
        "plain_english": (
            "A piece of equipment (identified by SerialNo) has no installation date. "
            "The schema requires DateInstalled to be non-null before sensor deployment "
            "records can be linked."
        ),
        "owner": "AMP",
        "owner_rationale": (
            "AMP field staff installed the equipment and are the primary source of truth "
            "for the install date. Dev cannot invent this date."
        ),
        "diagnose": (
            "1. Search AMP UI for the SerialNo listed in the error.\n"
            "2. Check field notebooks, deployment logs, or site visit records for the "
            "instrument commissioning date.\n"
            "3. As a fallback, use the date of the first continuous measurement in the "
            "data file as an approximation (flag it as estimated)."
        ),
        "resolution_paths": (
            "Path 1 (AMP – preferred): Enter the confirmed installation date in AMP UI for "
            "the given SerialNo.\n"
            "Path 2 (AMP – fallback): If exact date is unknown, use the first continuous "
            "measurement date and mark as 'estimated' in the Notes field.\n"
            "Path 3 (AMP): If the instrument never had continuous data (e.g., NMT student "
            "loggers with no downloaded files), mark the equipment record for deletion and "
            "notify Dev.\n"
            "NOT ALLOWED: Setting DateInstalled to a system default (e.g., today's date or "
            "1900-01-01) without field verification."
        ),
        "amp_needs": (
            "Required format: YYYY-MM-DD (ISO 8601).\n"
            "Acceptable values: Any valid calendar date on or before the first recorded "
            "measurement.\n"
            "Domain logic: DateInstalled must be ≤ the start of the first deployment record "
            "for that serial number."
        ),
        "dev_needs": (
            "Expected behavior: Reject import if DateInstalled is null; surface a clear "
            "error message naming the SerialNo.\n"
            "Unit tests: (a) null DateInstalled → validation error with SerialNo; "
            "(b) DateInstalled after first measurement → warning.\n"
            "Migration safety: Existing records with null dates should be quarantined, not "
            "deleted, until AMP supplies the correct value."
        ),
    },
    {
        "id": "VALIDATION.equipment.date_installed.estimated",
        "match": lambda tf, err: tf == "Equipment.DateInstalled" and err.startswith("Estimated installation date="),
        "table_fields": ["Equipment.DateInstalled"],
        "signature": r"Estimated installation date=\d{4}-\d{2}-\d{2}\. Is this correct\?",
        "plain_english": (
            "The system inferred an installation date from available data (e.g., first "
            "measurement timestamp) rather than a confirmed field record. AMP must verify "
            "whether the estimate is acceptable."
        ),
        "owner": "AMP",
        "owner_rationale": (
            "Only AMP staff can confirm whether the estimated date matches actual field "
            "installation. An incorrect date will corrupt deployment timeline integrity."
        ),
        "diagnose": (
            "1. Note the estimated date shown in the error.\n"
            "2. Cross-reference with deployment logs, field visit records, or purchase "
            "orders for the instrument.\n"
            "3. If the estimate is within ±7 days of the known install, it is likely "
            "acceptable; otherwise override."
        ),
        "resolution_paths": (
            "Path 1 (AMP): Confirm the date is correct → mark Reviewed = Yes in the "
            "source sheet; no other action needed.\n"
            "Path 2 (AMP): Date is incorrect → enter the correct date in the AMP UI and "
            "mark Fixed = Yes.\n"
            "NOT ALLOWED: Leaving the row unreviewed; the estimate will be imported as "
            "fact on the next pipeline run."
        ),
        "amp_needs": (
            "Provide: confirmed or corrected installation date in YYYY-MM-DD format.\n"
            "Flag: whether the corrected date is 'confirmed from field records' or "
            "'best estimate'."
        ),
        "dev_needs": (
            "Expected behavior: Store a boolean flag (is_estimated) alongside DateInstalled "
            "so downstream queries can filter on confidence level.\n"
            "Unit tests: confirmed vs. estimated flag round-trips correctly.\n"
            "No migration risk — additive field."
        ),
    },
    {
        "id": "VALIDATION.equipment.recording_interval.estimated",
        "match": lambda tf, err: tf == "Equipment.RecordingInterval" and err.startswith("Estimated recording interval="),
        "table_fields": ["Equipment.RecordingInterval"],
        "signature": r"Estimated recording interval=\d+ (hour|minute)\. Is this correct\?",
        "plain_english": (
            "The system estimated the sensor's recording interval (e.g., 1 hour, 15 minutes) "
            "from the measurement timestamps rather than from instrument configuration. "
            "AMP must confirm whether this matches the actual programmed interval."
        ),
        "owner": "AMP",
        "owner_rationale": (
            "AMP programmed the logger. An incorrect interval will cause time-series "
            "alignment errors across all downstream analyses."
        ),
        "diagnose": (
            "1. Check the sensor's configuration file or field programming records.\n"
            "2. Compare the estimated interval against the raw data file header.\n"
            "3. Watch for irregular intervals (e.g., 11 h, 13 h) which often indicate "
            "clock drift or a partially downloaded dataset — not the true interval."
        ),
        "resolution_paths": (
            "Path 1 (AMP): Interval is correct → confirm in AMP UI; mark Reviewed = Yes.\n"
            "Path 2 (AMP): Interval is incorrect → update RecordingInterval in AMP UI "
            "with the correct value and unit.\n"
            "Path 3 (Dev): If the interval is genuinely irregular (variable-rate logger), "
            "update the schema to allow NULL or 'variable' as a valid interval value.\n"
            "NOT ALLOWED: Accepting an obviously wrong interval (e.g., 11 hours for a "
            "standard hourly logger) without investigation."
        ),
        "amp_needs": (
            "Provide: integer value and unit (minutes or hours).\n"
            "Acceptable values: 15 min, 30 min, 1 h, 2 h, 4 h, 6 h, 12 h, 24 h are "
            "typical; document any non-standard interval with a justification note."
        ),
        "dev_needs": (
            "Expected behavior: Store interval as an integer + unit enum; reject free-text.\n"
            "Unit tests: fractional-hour estimates (11 h, 13 h) should trigger a "
            "'suspicious interval' warning, not a hard error.\n"
            "Migration safety: Existing rows with estimated intervals should retain an "
            "'is_estimated' flag until AMP confirms."
        ),
    },
    {
        "id": "VALIDATION.equipment.recording_interval.no_measurements",
        "match": lambda tf, err: tf == "Equipment.RecordingInterval" and "No measurements found for PointID" in err,
        "table_fields": ["Equipment.RecordingInterval"],
        "signature": r"name=\d+, row\.SerialNo=\S+\. error=No measurements found for PointID: \S+",
        "plain_english": (
            "The system could not estimate a recording interval because no measurement "
            "records were found for this instrument at the given PointID. The equipment "
            "record exists but has no associated data."
        ),
        "owner": "AMP",
        "owner_rationale": (
            "AMP must determine whether data was collected but not yet uploaded, was lost, "
            "or whether the equipment record was created in error."
        ),
        "diagnose": (
            "1. Search the AMP UI for the SerialNo and PointID combination.\n"
            "2. Check raw data file directories for files matching this logger serial.\n"
            "3. Determine whether a field deployment actually occurred for this PointID."
        ),
        "resolution_paths": (
            "Path 1 (AMP): Data exists but wasn't uploaded → upload raw data file and "
            "re-run the import.\n"
            "Path 2 (AMP): Logger was deployed but data was lost → mark equipment record "
            "with status 'no data' and enter a RecordingInterval from config records if "
            "known.\n"
            "Path 3 (AMP): Equipment record was created in error → flag for deletion by "
            "Dev.\n"
            "NOT ALLOWED: Entering a fabricated RecordingInterval when no data and no "
            "configuration record exists."
        ),
        "amp_needs": (
            "For each flagged SerialNo/PointID: confirm one of — (a) data file location "
            "for upload, (b) confirmed interval from logger config, or (c) record-deletion "
            "request."
        ),
        "dev_needs": (
            "Expected behavior: When no measurements exist, emit a WARNING (not ERROR) and "
            "allow the equipment record to be saved with RecordingInterval = NULL.\n"
            "Unit tests: equipment with zero measurements → warning emitted, record saved.\n"
            "Migration safety: None — this is a validation-level change only."
        ),
    },
    {
        "id": "VALIDATION.equipment.equipment_type.invalid",
        "match": lambda tf, err: tf == "Equipment.EquipmentType" and err.startswith("Invalid sensor_type:"),
        "table_fields": ["Equipment.EquipmentType"],
        "signature": r"Invalid sensor_type: (Diver Cable|DiverLink)",
        "plain_english": (
            "The sensor type value ('Diver Cable' or 'DiverLink') is not present in the "
            "equipment-type lookup table. The import rejects any sensor_type not "
            "pre-registered in the lexicon."
        ),
        "owner": "Dev / AMP",
        "owner_rationale": (
            "AMP must clarify whether these are new distinct equipment categories or aliases "
            "for existing types (e.g., 'pressure transducer'). Dev must add the new types "
            "to the lexicon once approved."
        ),
        "diagnose": (
            "1. Check the current equipment_type lookup table in the AMP database or Ocotillo "
            "lexicon for existing entries.\n"
            "2. Ask AMP: Is 'Diver Cable' a cable accessory for a Diver transducer, or a "
            "standalone sensor type? Same for 'DiverLink'.\n"
            "3. Search field documentation for how these instruments are catalogued."
        ),
        "resolution_paths": (
            "Path 1 (Dev + AMP): AMP confirms these are new distinct types → Dev adds them "
            "to the equipment_type lexicon with canonical names and descriptions.\n"
            "Path 2 (AMP): These map to existing type 'pressure transducer' → AMP corrects "
            "the source data to use the canonical name.\n"
            "Path 3 (Dev): Add 'Diver Cable' and 'DiverLink' as aliases pointing to a "
            "canonical entry.\n"
            "NOT ALLOWED: Silently mapping unknown types to 'Other' without documented "
            "rationale."
        ),
        "amp_needs": (
            "Provide: a written description of 'Diver Cable' and 'DiverLink' — what they "
            "measure, how they differ from existing types, and whether they should appear "
            "separately in the UI.\n"
            "Contact Marissa Fichera for the Ocotillo lexicon sensor type list."
        ),
        "dev_needs": (
            "Expected behavior: After lexicon is updated, re-run import; new types should "
            "be accepted.\n"
            "Unit tests: (a) known type → accepted; (b) unknown type → clear error with "
            "type name; (c) new type after lexicon update → accepted.\n"
            "Migration safety: Existing records using 'Diver Cable'/'DiverLink' strings "
            "should be backfilled to the canonical lexicon ID after the update."
        ),
    },
    {
        "id": "VALIDATION.deployment.no_deployment_gap",
        "match": lambda tf, err: re.search(r"no deployment between", err) is not None and "WaterLevels" not in tf,
        "table_fields": ["transducer_observation_block (no specific table.field)"],
        "signature": r"no deployment between \d{4}-\d{2}-\d{2}.*and \d{4}-\d{2}-\d{2}",
        "plain_english": (
            "Observation data exists for a PointID during a time window when no sensor "
            "deployment record is registered. The system cannot associate the readings with "
            "an instrument, so the import is rejected."
        ),
        "owner": "Dev / Data Services",
        "owner_rationale": (
            "Data Services (Kelsey & Ethan) must determine which sensor was physically at "
            "the location during the gap. Dev may need to adjust deployment date boundaries "
            "if they are known to be slightly off."
        ),
        "diagnose": (
            "1. In the AMP UI, navigate to the PointID > Equipment > Deployments and "
            "inspect deployment start/end dates around the flagged window.\n"
            "2. Check field visit records for sensor retrieval/reinstallation events near "
            "the gap dates.\n"
            "3. For very short gaps (same day, few hours), consider whether the gap is due "
            "to UTC offset errors in the deployment timestamps."
        ),
        "resolution_paths": (
            "Path 1 (Data Services): Identify the missing deployment → create the deployment "
            "record in AMP UI with correct sensor, start, and end dates.\n"
            "Path 2 (Dev): Gap is due to a ±12-hour timezone offset error → correct the "
            "deployment start/end timestamps (UTC conversion fix).\n"
            "Path 3 (Data Services): Data during the gap is invalid (no sensor was actually "
            "present) → mark those observations as 'rejected' rather than importing them.\n"
            "NOT ALLOWED: Creating a dummy deployment record without knowing which "
            "instrument was deployed."
        ),
        "amp_needs": (
            "For each flagged PointID + date range: identify the serial number of the "
            "sensor deployed during that period and its exact install/removal dates.\n"
            "Domain logic: observation data without a matching deployment is considered "
            "untrustworthy and must not be imported."
        ),
        "dev_needs": (
            "Expected behavior: When no deployment covers a time window, log the specific "
            "PointID and date range; do not silently skip.\n"
            "Unit tests: observation within deployment range → accepted; observation outside "
            "all deployment ranges → error with date range shown.\n"
            "Migration safety: Do not bulk-create deployments to cover gaps; require human "
            "approval for each."
        ),
    },
    {
        "id": "VALIDATION.deployment.no_deployments_at_all",
        "match": lambda tf, err: err.strip() == "no deployments",
        "table_fields": ["transducer_observation_block"],
        "signature": r"^no deployments$",
        "plain_english": (
            "A PointID has observation data but zero deployment records whatsoever. "
            "This usually means the Equipment.DateInstalled error was not yet fixed — "
            "once an installation date is provided, deployments can be created."
        ),
        "owner": "AMP",
        "owner_rationale": (
            "This error is a downstream consequence of the missing DateInstalled error "
            "(VALIDATION.equipment.date_installed.required). Fix that first."
        ),
        "diagnose": (
            "1. Check if the same PointID appears in the Equipment.DateInstalled error list.\n"
            "2. If DateInstalled is present, check whether a deployment record was ever "
            "created in the AMP UI for this PointID.\n"
            "3. Verify the PointID is not a retired/duplicate location."
        ),
        "resolution_paths": (
            "Path 1 (AMP – primary): Resolve the Equipment.DateInstalled error first; "
            "deployments should then auto-populate or can be created.\n"
            "Path 2 (AMP): If DateInstalled is known but no deployment record was created, "
            "manually add the deployment in the AMP UI.\n"
            "NOT ALLOWED: Importing observations for a PointID with no deployment history."
        ),
        "amp_needs": (
            "Provide: installation date and sensor serial number so a deployment record "
            "can be created. See VALIDATION.equipment.date_installed.required for format."
        ),
        "dev_needs": (
            "Expected behavior: Surface 'no deployments' as a dependency error linked to "
            "the DateInstalled validation, not as a standalone error.\n"
            "Unit tests: PointID with DateInstalled but no deployment → distinct warning; "
            "PointID with neither → chained error message."
        ),
    },
    {
        "id": "VALIDATION.waterlevel.continuous_pressure.no_deployment",
        "match": lambda tf, err: tf == "WaterLevelsContinuous_Pressure.DateMeasured" and "no deployment" in err,
        "table_fields": ["WaterLevelsContinuous_Pressure.DateMeasured"],
        "signature": r"no deployment(s| between \S+ and \S+)",
        "plain_english": (
            "A continuous pressure water-level reading has a DateMeasured outside any "
            "registered sensor deployment window. The data cannot be attributed to a "
            "specific instrument."
        ),
        "owner": "Data Services",
        "owner_rationale": "Same root cause as VALIDATION.deployment.no_deployment_gap but specific to continuous pressure water-level data.",
        "diagnose": (
            "1. Locate the PointID in AMP UI > Water Levels > Continuous (Pressure).\n"
            "2. Inspect deployment timeline for coverage gaps around DateMeasured.\n"
            "3. Check for timezone conversion issues (AEST vs UTC)."
        ),
        "resolution_paths": (
            "Path 1 (Data Services): Create missing deployment record.\n"
            "Path 2 (Dev): Fix UTC offset if gap is consistently ±10–12 hours.\n"
            "Path 3 (Data Services): If no instrument was present, reject/archive those "
            "readings.\n"
            "NOT ALLOWED: Importing unattributed pressure readings."
        ),
        "amp_needs": "N/A for this sub-type; Data Services owns resolution.",
        "dev_needs": (
            "Expected behavior: Same as VALIDATION.deployment.no_deployment_gap but scoped "
            "to WaterLevelsContinuous_Pressure.\n"
            "Unit tests: pressure reading within deployment → accepted; outside → error."
        ),
    },
    {
        "id": "VALIDATION.waterlevel.continuous_acoustic.no_deployment",
        "match": lambda tf, err: tf == "WaterLevelsContinuous_Acoustic.DateMeasured" and "no deployment" in err,
        "table_fields": ["WaterLevelsContinuous_Acoustic.DateMeasured"],
        "signature": r"no deployment(s| between \S+ and \S+)",
        "plain_english": (
            "A continuous acoustic water-level reading falls outside all registered "
            "deployment windows for the PointID."
        ),
        "owner": "Data Services",
        "owner_rationale": "Same as pressure variant but for acoustic sensors.",
        "diagnose": "Same steps as VALIDATION.waterlevel.continuous_pressure.no_deployment.",
        "resolution_paths": "Same paths as VALIDATION.waterlevel.continuous_pressure.no_deployment.",
        "amp_needs": "N/A.",
        "dev_needs": "Same as pressure variant; scope unit tests to acoustic table.",
    },
    {
        "id": "VALIDATION.well_data.measuring_point_height.required",
        "match": lambda tf, err: tf == "WellData.UnknownField" and "measuring_point_height" in err and "float_type" in err,
        "table_fields": ["WellData.measuring_point_height"],
        "signature": r"Validation Error.*float_type.*measuring_point_height.*Input should be a valid number",
        "plain_english": (
            "The measuring_point_height field is missing or non-numeric for this well record. "
            "The schema expects a float (elevation in feet or meters above a datum). "
            "This is the most frequent error in the dataset (522 occurrences)."
        ),
        "owner": "AMP",
        "owner_rationale": (
            "AMP field staff measure the height of the measuring point above ground surface "
            "or casing top. Dev cannot fabricate this value."
        ),
        "diagnose": (
            "1. Search the well record in AMP UI for the PointID.\n"
            "2. Check field measurement sheets for 'MP height', 'stick-up', or 'casing height'.\n"
            "3. If the well is a legacy record (pre-AMP), check NMBGMR archives or OSE files."
        ),
        "resolution_paths": (
            "Path 1 (AMP – preferred): Enter the measured MP height in AMP UI for the PointID.\n"
            "Path 2 (Dev): For legacy records where MP height was never measured, make the "
            "field optional with a NULL allowed AND a flag 'mp_height_unknown=True'.\n"
            "Path 3 (Dev + AMP): If a standard default is scientifically acceptable for a "
            "specific well class, document it as a policy decision — do NOT silently default "
            "to 0.\n"
            "NOT ALLOWED: Defaulting measuring_point_height to 0 — this produces incorrect "
            "water-level calculations."
        ),
        "amp_needs": (
            "Required format: decimal number (e.g., 1.37).\n"
            "Units: feet above top of casing (or specify if different).\n"
            "Acceptable range: 0.1 to ~10.0 ft for standard installations; flag outliers.\n"
            "Domain logic: MP height is used to convert raw transducer depth readings to "
            "water-level elevations; an error here propagates to all water-level data."
        ),
        "dev_needs": (
            "Expected behavior: Reject non-numeric values with a clear field name in the "
            "error message; do not accept empty string as 0.\n"
            "Unit tests: (a) valid float → accepted; (b) empty string → error; "
            "(c) '1.37 ft' string → error (no units in field); (d) NULL → error unless "
            "mp_height_unknown flag is set.\n"
            "Migration safety: 522 records affected — run a migration report before "
            "enforcing the constraint; allow a grace period for AMP to populate values."
        ),
    },
    {
        "id": "VALIDATION.well_data.casing_depth.exceeds_hole_depth",
        "match": lambda tf, err: tf == "WellData" and "casing depth must be less than or equal to hole depth" in err,
        "table_fields": ["WellData.CasingDepth", "WellData.HoleDepth"],
        "signature": r"well casing depth must be less than or equal to hole depth",
        "plain_english": (
            "The recorded casing depth is greater than the total hole (borehole) depth, "
            "which is physically impossible. One or both values is likely a data entry error."
        ),
        "owner": "AMP",
        "owner_rationale": "AMP entered the well construction data; the error is almost certainly a unit mismatch or transposition.",
        "diagnose": (
            "1. Open the well record in AMP UI and compare CasingDepth and HoleDepth values.\n"
            "2. Check if one value is in feet and the other in meters (common unit mismatch).\n"
            "3. Consult the driller's log or completion report for the PointID."
        ),
        "resolution_paths": (
            "Path 1 (AMP): Correct the erroneous value in AMP UI after verifying against "
            "the driller's log.\n"
            "Path 2 (AMP): If both values are correct but units differ, standardize both to "
            "feet (or meters) per the project convention.\n"
            "NOT ALLOWED: Swapping casing and hole depth without confirming which is wrong."
        ),
        "amp_needs": (
            "Provide: verified CasingDepth and HoleDepth in consistent units (feet preferred).\n"
            "Source: driller's log, OSE completion report, or NMBGMR records."
        ),
        "dev_needs": (
            "Expected behavior: Validate CasingDepth ≤ HoleDepth on save; show both values "
            "in the error message.\n"
            "Unit tests: casing > hole → error; casing = hole → accepted (cased full depth); "
            "casing < hole → accepted.\n"
            "Migration safety: 18 records; do not auto-correct — require human review."
        ),
    },
    {
        "id": "VALIDATION.well_data.well_depth.exceeds_hole_depth",
        "match": lambda tf, err: tf == "WellData" and "well depth must be less than" in err and "hole depth" in err,
        "table_fields": ["WellData.WellDepth", "WellData.HoleDepth"],
        "signature": r"well depth must be less than than or equal to hole depth",
        "plain_english": (
            "The recorded well (total) depth is greater than the borehole depth. "
            "Similar to casing depth error — likely a unit mismatch or data entry error."
        ),
        "owner": "AMP",
        "owner_rationale": "Same as VALIDATION.well_data.casing_depth.exceeds_hole_depth.",
        "diagnose": "Same steps as casing depth error; check driller's log.",
        "resolution_paths": "Same paths as VALIDATION.well_data.casing_depth.exceeds_hole_depth.",
        "amp_needs": "Verified WellDepth and HoleDepth in consistent units.",
        "dev_needs": (
            "Expected behavior: Validate WellDepth ≤ HoleDepth.\n"
            "Unit tests: same pattern as casing depth.\n"
            "Migration safety: 3 records."
        ),
    },
    {
        "id": "VALIDATION.well_data.formation_zone.unknown",
        "match": lambda tf, err: tf == "WellData.FormationZone" and err.startswith("Unknown formation:"),
        "table_fields": ["WellData.FormationZone"],
        "signature": r"Unknown formation: [A-Z0-9/a-z_]+",
        "plain_english": (
            "The FormationZone code entered for this well is not present in the formation "
            "lookup table. This typically involves compound codes (e.g., '121TSUQs/112ANCH') "
            "combining a primary and secondary aquifer unit, or variant suffixes "
            "(e.g., 'ppm', 'sr', 'f') not in the current lexicon."
        ),
        "owner": "AMP",
        "owner_rationale": (
            "AMP's hydrogeologists assign formation codes based on aquifer maps. Dev needs "
            "AMP to confirm which codes are valid and what new entries the lexicon needs."
        ),
        "diagnose": (
            "1. Extract the unknown formation code from the error.\n"
            "2. Check the NMBGMR formation lexicon / Ocotillo system for the base code.\n"
            "3. Determine if the code is a compound (two codes joined with '/') or a suffix "
            "variant (e.g., 'ppm' = pumice, 'sr' = sub-regional).\n"
            "4. Contact AMP hydrogeologist for authoritative definition."
        ),
        "resolution_paths": (
            "Path 1 (AMP): Code is a valid variant not yet in the lexicon → provide "
            "definition so Dev can add it.\n"
            "Path 2 (AMP): Code is a data entry error → correct to the nearest valid code.\n"
            "Path 3 (Dev): Compound codes (A/B format) → implement compound-code parsing "
            "if the system should support primary/secondary formation pairs.\n"
            "NOT ALLOWED: Mapping unknown codes to a catch-all 'Unknown' formation entry "
            "without documenting the intent."
        ),
        "amp_needs": (
            "For each unique unknown formation code: provide (a) full name, (b) description, "
            "(c) USGS GEOLEX or NMBGMR reference if applicable, (d) whether it is a new "
            "distinct entry or an alias.\n"
            "Acceptable format: NMBGMR lexicon code style (e.g., 3-digit prefix + "
            "4-letter abbreviation)."
        ),
        "dev_needs": (
            "Expected behavior: Reject unknown formation codes with the code name in the "
            "error; provide a link to the valid lexicon list.\n"
            "Unit tests: known code → accepted; unknown code → error with code shown; "
            "compound code (if supported) → each component validated separately.\n"
            "Migration safety: Many records affected; run a frequency report and batch-add "
            "most common codes first."
        ),
    },
    {
        "id": "VALIDATION.well_data.aquifer_type.unknown_lexicon",
        "match": lambda tf, err: tf == "WellData.Unknown" and "LU_AquiferType" in err,
        "table_fields": ["WellData.AquiferType"],
        "signature": r"Unknown lexicon value: LU_AquiferType:[A-Za-z0-9]+",
        "plain_english": (
            "The AquiferType value is not a recognized entry in the LU_AquiferType lookup. "
            "Common offenders: '8' (numeric) and 'S' (likely abbreviation for 'semi-confined')."
        ),
        "owner": "AMP",
        "owner_rationale": "AMP entered the aquifer classification; the allowed values are controlled by the NMBGMR lexicon.",
        "diagnose": (
            "1. Note the invalid value (e.g., 'S', '8').\n"
            "2. Check current LU_AquiferType table for valid entries.\n"
            "3. Ask AMP which standard aquifer type the value was intended to represent."
        ),
        "resolution_paths": (
            "Path 1 (AMP): Determine the correct lexicon value and update in AMP UI.\n"
            "Path 2 (Dev + AMP): If 'S' and '8' represent valid concepts not yet in the "
            "lexicon, add them after AMP provides definitions.\n"
            "NOT ALLOWED: Silently converting to a default type."
        ),
        "amp_needs": "Mapping from invalid codes to their intended standard AquiferType values.",
        "dev_needs": (
            "Expected behavior: Show invalid value in error message with list of valid options.\n"
            "Unit tests: invalid → error with valid list shown; valid → accepted."
        ),
    },
    {
        "id": "VALIDATION.well_data.current_use.unknown_lexicon",
        "match": lambda tf, err: tf == "WellData.Unknown" and "LU_CurrentUse" in err,
        "table_fields": ["WellData.CurrentUse"],
        "signature": r"Unknown lexicon value: LU_CurrentUse:[A-Za-z]*",
        "plain_english": (
            "The CurrentUse value (e.g., 'W' or blank) is not in the LU_CurrentUse lookup table."
        ),
        "owner": "AMP",
        "owner_rationale": "AMP assigns well use classifications; the lexicon controls valid options.",
        "diagnose": (
            "1. Identify the invalid value.\n"
            "2. Compare against LU_CurrentUse valid entries.\n"
            "3. Determine intended use from source documents."
        ),
        "resolution_paths": (
            "Path 1 (AMP): Correct the value to a valid CurrentUse code.\n"
            "Path 2 (Dev + AMP): If blank is valid ('unknown use'), explicitly add a "
            "'UNKNOWN' or 'UNSPECIFIED' lexicon entry rather than allowing empty string.\n"
            "NOT ALLOWED: Storing empty string as a valid value in a controlled vocabulary field."
        ),
        "amp_needs": "Intended CurrentUse classification for each flagged PointID.",
        "dev_needs": "Expected behavior: reject empty string; accept NULL only if field is nullable.",
    },
    {
        "id": "VALIDATION.well_data.construction_method.unknown_lexicon",
        "match": lambda tf, err: tf in ("WellData.Unknown", "WellData.UnknownField") and "LU_ConstructionMethod" in err,
        "table_fields": ["WellData.ConstructionMethod"],
        "signature": r"Unknown lexicon value: LU_ConstructionMethod:[A-Za-z0-9 ]+",
        "plain_english": (
            "The well construction method code (e.g., 'AH', 'H') is not in the "
            "LU_ConstructionMethod lookup. These likely represent 'Auger Hollow-stem' and "
            "'Hydraulic' rotary but need AMP confirmation."
        ),
        "owner": "AMP / Dev",
        "owner_rationale": "AMP must confirm the intended method; Dev adds to lexicon.",
        "diagnose": (
            "1. Identify the invalid code.\n"
            "2. Check original driller's log for the construction method description.\n"
            "3. Compare against existing LU_ConstructionMethod entries."
        ),
        "resolution_paths": (
            "Path 1 (AMP): Map the code to an existing lexicon entry and update the record.\n"
            "Path 2 (Dev + AMP): If these are new valid methods, add them to the lexicon "
            "with full descriptions.\n"
            "NOT ALLOWED: Using abbreviations not defined in the lexicon."
        ),
        "amp_needs": "Full name and description for each unrecognized construction method code.",
        "dev_needs": (
            "Expected behavior: Show invalid code + valid options in error.\n"
            "Unit tests: 'AH' before lexicon update → error; after update → accepted."
        ),
    },
    {
        "id": "VALIDATION.well_data.well_completion_date.datetime_precision",
        "match": lambda tf, err: tf == "WellData.UnknownField" and "date_from_datetime_inexact" in err and "well_complet" in err,
        "table_fields": ["WellData.WellCompletionDate"],
        "signature": r"Validation Error.*date_from_datetime_inexact.*well_complet",
        "plain_english": (
            "The well completion date was provided as a datetime with time component "
            "(e.g., '1985-06-15 00:00:00'), but the schema expects a date-only value "
            "(e.g., '1985-06-15'). The import rejects datetime values where a date was expected."
        ),
        "owner": "Dev",
        "owner_rationale": (
            "This is a schema/parser issue — the source data likely exports dates as datetimes. "
            "Dev should either accept datetime-with-midnight-time or strip the time component "
            "during ETL."
        ),
        "diagnose": (
            "1. Check the raw source value for WellCompletionDate in the extract.\n"
            "2. Confirm it is always midnight (00:00:00) — if so, time component is spurious.\n"
            "3. Verify the schema column type (DATE vs TIMESTAMP)."
        ),
        "resolution_paths": (
            "Path 1 (Dev – preferred): Add a coerce step in the ETL to strip the time component "
            "from date fields before validation.\n"
            "Path 2 (Dev): Change the schema column to TIMESTAMP if time-of-day matters.\n"
            "NOT ALLOWED: Requiring AMP to manually reformat dates in the source spreadsheet."
        ),
        "amp_needs": "N/A — this is a purely technical ETL fix.",
        "dev_needs": (
            "Expected behavior: Accept '1985-06-15T00:00:00' and coerce to '1985-06-15' for "
            "DATE columns.\n"
            "Unit tests: datetime with midnight → coerced to date, no error; datetime with "
            "non-midnight time → warning or error depending on policy.\n"
            "Migration safety: 6 records; low risk."
        ),
    },
    {
        "id": "VALIDATION.well_data.construction_method.enum_invalid",
        "match": lambda tf, err: tf == "WellData.UnknownField" and "well_construction_method" in err and "enum" in err,
        "table_fields": ["WellData.well_construction_method"],
        "signature": r"Validation Error.*enum.*well_construction_method",
        "plain_english": (
            "The well construction method value does not match any entry in the allowed "
            "enum. This is a Pydantic-level enum validation failure — the value exists but "
            "is not in the pre-defined set."
        ),
        "owner": "AMP / Dev",
        "owner_rationale": "AMP provides the correct method; Dev ensures the enum is complete.",
        "diagnose": "Same as VALIDATION.well_data.construction_method.unknown_lexicon.",
        "resolution_paths": "Same as VALIDATION.well_data.construction_method.unknown_lexicon.",
        "amp_needs": "Correct construction method code from driller's log.",
        "dev_needs": (
            "Expected behavior: Show the invalid value and list the allowed enum values.\n"
            "Unit tests: non-enum value → error with allowed values listed."
        ),
    },
    {
        "id": "VALIDATION.well_data.depth_logic.combined",
        "match": lambda tf, err: tf == "WellData.UnknownField" and "well" in err.lower() and "depth" in err.lower() and "value_error" in err,
        "table_fields": ["WellData.WellDepth", "WellData.CasingDepth", "WellData.HoleDepth"],
        "signature": r"Validation Error.*value_error.*well.*depth",
        "plain_english": (
            "A Pydantic value_error on well depth fields — covers both 'well depth > hole depth' "
            "and 'casing depth > hole depth' as caught by model-level validators."
        ),
        "owner": "AMP",
        "owner_rationale": "Same as individual casing/well depth errors above.",
        "diagnose": "Check which specific depth field pair is invalid; see error message loc field.",
        "resolution_paths": "Same as VALIDATION.well_data.casing_depth.exceeds_hole_depth.",
        "amp_needs": "Verified depth values from driller's log.",
        "dev_needs": "Ensure error message specifies which pair of fields triggered the validation.",
    },
    {
        "id": "VALIDATION.well_screens.depth.invalid_numeric",
        "match": lambda tf, err: tf in ("WellScreens", "WellScreens.UnknownField") and ("screen_depth_bottom: Input should be a valid number" in err or "screen_depth_top: Input should be a valid number" in err),
        "table_fields": ["WellScreens.screen_depth_top", "WellScreens.screen_depth_bottom"],
        "signature": r"screen_depth_(top|bottom): Input should be a valid number",
        "plain_english": (
            "A well screen depth field contains a non-numeric value (e.g., text, blank, or "
            "non-standard notation). Screen depths must be numeric (feet below surface)."
        ),
        "owner": "AMP",
        "owner_rationale": "AMP entered the screen intervals; the source data has non-numeric characters.",
        "diagnose": (
            "1. Open the well record and inspect screen interval data.\n"
            "2. Check for text like 'unknown', 'N/A', or units embedded in the value.\n"
            "3. Reference driller's log for actual screen placement depths."
        ),
        "resolution_paths": (
            "Path 1 (AMP): Enter verified numeric screen depths in AMP UI.\n"
            "Path 2 (Dev): If screen depth is genuinely unknown, allow NULL with a "
            "'screen_depth_unknown' flag.\n"
            "NOT ALLOWED: Entering 0 for unknown screen depths."
        ),
        "amp_needs": "Numeric screen top and bottom depths in feet below land surface.",
        "dev_needs": (
            "Expected behavior: Reject non-numeric values; accept NULL if field is nullable.\n"
            "Unit tests: string → error; 0 → error if greater_than constraint; "
            "valid float → accepted."
        ),
    },
    {
        "id": "VALIDATION.well_screens.depth.top_must_be_positive",
        "match": lambda tf, err: tf in ("WellScreens", "WellScreens.UnknownField") and "screen_depth_top" in err and "greater than 0" in err,
        "table_fields": ["WellScreens.screen_depth_top"],
        "signature": r"screen_depth_top: Input should be greater than 0",
        "plain_english": (
            "The top of the well screen is recorded as 0 or a negative number, which is "
            "physically impossible (screens are below ground surface)."
        ),
        "owner": "AMP",
        "owner_rationale": "AMP entered the screen depth; a zero value is almost certainly a data entry error.",
        "diagnose": (
            "1. Check the driller's log for the top-of-screen depth.\n"
            "2. Determine if 0 was a placeholder for 'unknown'.\n"
            "3. For artesian wells, confirm screens are below land surface."
        ),
        "resolution_paths": (
            "Path 1 (AMP): Enter the correct positive depth from the driller's log.\n"
            "Path 2 (Dev + AMP): If unknown, set to NULL and add a flag — do not use 0.\n"
            "NOT ALLOWED: Using 0 as a placeholder for unknown screen depth."
        ),
        "amp_needs": "Confirmed screen top depth in feet below land surface (must be > 0).",
        "dev_needs": (
            "Expected behavior: Enforce screen_depth_top > 0.\n"
            "Unit tests: 0 → error; negative → error; positive float → accepted."
        ),
    },
    {
        "id": "VALIDATION.well_screens.depth.bottom_less_than_top",
        "match": lambda tf, err: tf in ("WellScreens", "WellScreens.UnknownField") and "screen_depth_bottom must be greater than screen_depth_top" in err,
        "table_fields": ["WellScreens.screen_depth_top", "WellScreens.screen_depth_bottom"],
        "signature": r"screen_depth_bottom must be greater than screen_depth_top",
        "plain_english": (
            "The recorded bottom-of-screen depth is shallower than the top-of-screen depth, "
            "which is physically impossible. The values are likely transposed."
        ),
        "owner": "AMP",
        "owner_rationale": "AMP entered the screen interval; transposition is the likely cause.",
        "diagnose": (
            "1. Compare screen_depth_top and screen_depth_bottom in the record.\n"
            "2. If top > bottom, the values are swapped.\n"
            "3. Verify correct values against driller's log."
        ),
        "resolution_paths": (
            "Path 1 (AMP): Swap the values if they are transposed.\n"
            "Path 2 (AMP): Enter correct values from driller's log if both are wrong.\n"
            "NOT ALLOWED: Setting bottom = top (zero-length screen)."
        ),
        "amp_needs": "Confirmed top and bottom screen depths; bottom must exceed top.",
        "dev_needs": (
            "Expected behavior: Validate bottom > top; show both values in error.\n"
            "Unit tests: bottom ≤ top → error; bottom > top → accepted."
        ),
    },
    {
        "id": "VALIDATION.owners_data.name_or_org_required",
        "match": lambda tf, err: tf in ("OwnersData", "OwnersData.UnknownField") and ("Either name or organization must be provided" in err or "Eithe" in err),
        "table_fields": ["OwnersData.Name", "OwnersData.Organization"],
        "signature": r"(Either name or organization must be provided|Value error, Eithe)",
        "plain_english": (
            "An owner record has neither a person name nor an organization name. At least "
            "one must be provided to identify the well owner."
        ),
        "owner": "Data Services",
        "owner_rationale": "Data Services maintains owner records; AMP does not own this data.",
        "diagnose": (
            "1. Locate the owner record linked to the PointID.\n"
            "2. Check OSE water rights records or NMBGMR files for the owner name.\n"
            "3. Determine if the owner is an individual or organization."
        ),
        "resolution_paths": (
            "Path 1 (Data Services): Enter the owner's name or organization from official records.\n"
            "Path 2 (Dev): If owner is truly unknown, add an explicit 'UNKNOWN OWNER' "
            "organization entry rather than leaving both fields blank.\n"
            "NOT ALLOWED: Leaving both name and organization blank."
        ),
        "amp_needs": "N/A.",
        "dev_needs": (
            "Expected behavior: Require at least one of name/organization at the model level.\n"
            "Unit tests: both null → error; name only → accepted; org only → accepted; "
            "both → accepted."
        ),
    },
    {
        "id": "VALIDATION.owners_data.organization.invalid",
        "match": lambda tf, err: tf == "OwnersData" and err.startswith("Invalid organization:"),
        "table_fields": ["OwnersData.Organization"],
        "signature": r"Invalid organization: .+",
        "plain_english": (
            "The organization name is not in the approved organization lookup table. "
            "This affects a large number of organizations (water utilities, ranches, "
            "government agencies) that need to be registered before owner records can "
            "be imported."
        ),
        "owner": "Data Services",
        "owner_rationale": "Data Services manages the organization registry; these require manual registration.",
        "diagnose": (
            "1. Check the OwnersData.Organization value against the organization lookup table.\n"
            "2. Search for close matches (abbreviations, spelling variants).\n"
            "3. For government agencies (BLM, NPS, USFS), check if a parent-agency entry exists."
        ),
        "resolution_paths": (
            "Path 1 (Data Services): Register the organization in the system with its full "
            "name, type, and any aliases.\n"
            "Path 2 (Data Services): Map the organization to an existing entry if it is a "
            "known alias or sub-unit.\n"
            "Path 3 (Dev): Build a bulk organization-registration tool to process the full "
            "list at once rather than one-by-one.\n"
            "NOT ALLOWED: Using free-text organization names that bypass the lookup table."
        ),
        "amp_needs": "N/A.",
        "dev_needs": (
            "Expected behavior: Show the invalid organization name in the error; provide a "
            "fuzzy-match suggestion if a close entry exists.\n"
            "Unit tests: unregistered org → error with name; registered org → accepted.\n"
            "Migration safety: Batch-register all known organizations before re-running "
            "imports to avoid repeated failures."
        ),
    },
    {
        "id": "VALIDATION.owners_data.organization.fk_missing",
        "match": lambda tf, err: tf == "OwnersData" and "is not present in table" in err,
        "table_fields": ["OwnersData.Organization"],
        "signature": r"Key \(organization\)=\(.+\) is not present in table",
        "plain_english": (
            "A foreign-key constraint violation — the organization value exists in the source "
            "data but has no matching row in the organizations reference table."
        ),
        "owner": "Data Services",
        "owner_rationale": "Same as Invalid organization — the organization must be registered first.",
        "diagnose": "Same as VALIDATION.owners_data.organization.invalid.",
        "resolution_paths": "Same as VALIDATION.owners_data.organization.invalid.",
        "amp_needs": "N/A.",
        "dev_needs": (
            "Expected behavior: FK error should surface the organization name (not just the "
            "key value) for easier debugging.\n"
            "Unit tests: missing FK → clear error with org name shown."
        ),
    },
    {
        "id": "VALIDATION.well_data.missing_unit_identifier",
        "match": lambda tf, err: "Missing UnitIdentifier" in err,
        "table_fields": ["WellData (unit identifier / formation code)"],
        "signature": r"Missing UnitIdentifier",
        "plain_english": (
            "A well record is missing a unit identifier, which is required to associate it "
            "with a hydrologic unit or formation zone. This may be related to an incomplete "
            "formation code."
        ),
        "owner": "AMP / Jake Ross",
        "owner_rationale": "Jake Ross owns unit identifier mapping per the source sheet.",
        "diagnose": (
            "1. Check the well record for FormationZone — a missing formation code often "
            "causes a missing unit identifier.\n"
            "2. Contact Jake Ross for the correct formation/unit mapping."
        ),
        "resolution_paths": (
            "Path 1 (AMP): Provide the correct formation code; unit identifier should derive "
            "from it.\n"
            "Path 2 (Dev): If UnitIdentifier is a separate field, make it derivable from "
            "FormationZone automatically.\n"
            "NOT ALLOWED: Leaving UnitIdentifier blank in a finalized well record."
        ),
        "amp_needs": "Formation code from which unit identifier can be derived; contact Jake Ross.",
        "dev_needs": (
            "Expected behavior: If UnitIdentifier can be derived from FormationZone, auto-populate it.\n"
            "Unit tests: valid FormationZone → UnitIdentifier auto-populated; null FormationZone "
            "→ error on UnitIdentifier."
        ),
    },
    {
        "id": "VALIDATION.location.plss.invalid",
        "match": lambda tf, err: tf.startswith("Location.Township") and "is not a valid PLSS" in err,
        "table_fields": ["Location.Township", "Location.TownshipDirection", "Location.Range", "Location.RangeDirection", "Location.Section", "Location.SectionDirection"],
        "signature": r"T\d+[NS]\.R\d+[EW]\.S\d+\.\d+ is not a valid PLSS",
        "plain_english": (
            "The Public Land Survey System (PLSS) location code is malformed or references "
            "a section/township/range combination that does not exist in New Mexico."
        ),
        "owner": "AMP",
        "owner_rationale": "AMP or the original data submitter entered the PLSS coordinates; only AMP can verify the correct location.",
        "diagnose": (
            "1. Parse the PLSS string: Township, Direction, Range, Direction, Section, QQ.\n"
            "2. Cross-check against the BLM General Land Office survey plats for NM.\n"
            "3. Use a PLSS web service (e.g., geocommunicator.gov) to validate the code."
        ),
        "resolution_paths": (
            "Path 1 (AMP): Correct the PLSS string using GPS coordinates and a PLSS lookup tool.\n"
            "Path 2 (Dev): Integrate a PLSS validation API to give real-time feedback in the UI.\n"
            "NOT ALLOWED: Importing an invalid PLSS code — it will corrupt spatial queries."
        ),
        "amp_needs": "Verified Township, Direction, Range, Direction, Section for each flagged location.",
        "dev_needs": (
            "Expected behavior: Validate PLSS code against a reference dataset on import.\n"
            "Unit tests: valid PLSS → accepted; invalid section number → error; "
            "non-existent township → error."
        ),
    },
    {
        "id": "INTEGRITY.well_data.duplicate_point_id",
        "match": lambda tf, err: tf == "WellData.PointID" and "duplicate" in err.lower(),
        "table_fields": ["WellData.PointID"],
        "signature": r"duplicate records",
        "plain_english": (
            "Multiple well records share the same PointID, violating the uniqueness constraint. "
            "This is a data integrity issue requiring manual deduplication."
        ),
        "owner": "Dev / Data Services (Ethan Mamer)",
        "owner_rationale": "Ethan Mamer owns deduplication per the source sheet.",
        "diagnose": (
            "1. Query WellData WHERE PointID = <id> to find all duplicate records.\n"
            "2. Compare field values to determine which is canonical.\n"
            "3. Check submission history to understand when and how the duplicate was created."
        ),
        "resolution_paths": (
            "Path 1 (Data Services): Identify the canonical record and archive duplicates.\n"
            "Path 2 (Dev): Add a pre-import uniqueness check that fails fast with a "
            "clear message if PointID already exists.\n"
            "NOT ALLOWED: Silently overwriting the existing record without review."
        ),
        "amp_needs": "N/A.",
        "dev_needs": (
            "Expected behavior: Reject import if PointID already exists; offer an 'update' "
            "mode explicitly.\n"
            "Unit tests: duplicate PointID → error; unique PointID → accepted."
        ),
    },
    {
        "id": "VALIDATION.well_data.well_status.unknown_lexicon",
        "match": lambda tf, err: tf == "WellData.Unknown" and "LU_Status:O" in err,
        "table_fields": ["WellData.Status"],
        "signature": r"Unknown lexicon value: LU_Status:O",
        "plain_english": (
            "Well status code 'O' is not in the LU_Status lookup table. It likely represents "
            "'Other' or 'Observation' but needs to be formally defined."
        ),
        "owner": "Data Services / Dev",
        "owner_rationale": "Data Services must confirm the intended status; Dev adds to lexicon.",
        "diagnose": (
            "1. Check the source record for context on status 'O'.\n"
            "2. Review the existing LU_Status entries for close matches.\n"
            "3. Consult NMBGMR or OSE status classification documentation."
        ),
        "resolution_paths": (
            "Path 1 (Dev + Data Services): Define 'O' and add to LU_Status lexicon.\n"
            "Path 2 (Data Services): Map 'O' to an existing status code if equivalent.\n"
            "NOT ALLOWED: Importing records with undefined status codes."
        ),
        "amp_needs": "N/A.",
        "dev_needs": "Add LU_Status:O to lexicon after Data Services confirms definition.",
    },
]


def classify_row(table_field: str, error: str) -> dict | None:
    """Return the matching error class dict, or None if no match."""
    for ec in ERROR_CLASSES:
        try:
            if ec["match"](table_field, error):
                return ec
        except Exception:
            continue
    return None


def build_catalog_rows(raw_rows: list[list]) -> list[list]:
    """
    Collapse raw source rows into unique error-class catalog entries.
    Returns list of lists ready for Sheets API batchUpdate.
    """
    # Header
    headers = [
        "A. Error Class Name",
        "B. Signature / Match Pattern",
        "C. Plain-English Meaning",
        "D. Default Owner + Rationale",
        "E. How to Diagnose",
        "F. Approved Resolution Paths",
        "G. What AMP Needs to Provide",
        "H. What Dev Needs to Implement",
        "Affected Tables / Fields",
        "Affected PointIDs (sample)",
        "Occurrence Count",
    ]

    # Collect occurrences per class
    class_occurrences: dict[str, list[str]] = defaultdict(list)
    class_meta: dict[str, dict] = {}

    for row in raw_rows:
        table_field = row[COL_TABLE_FIELD].strip() if len(row) > COL_TABLE_FIELD else ""
        error = row[COL_ERROR].strip() if len(row) > COL_ERROR else ""
        point_id = row[COL_POINT_ID].strip() if len(row) > COL_POINT_ID else ""

        if not error:
            continue

        ec = classify_row(table_field, error)
        if ec is None:
            # Unclassified — put in a catch-all
            key = f"UNCLASSIFIED.{table_field or 'unknown'}"
            class_occurrences[key].append(point_id)
            if key not in class_meta:
                class_meta[key] = {
                    "id": key,
                    "table_fields": [table_field],
                    "signature": error[:120],
                    "plain_english": f"Unclassified error in {table_field or 'unknown table'}: {error[:200]}",
                    "owner": "Mapping Needed",
                    "owner_rationale": "This error class has not yet been mapped to a resolution path.",
                    "diagnose": "Review raw error text and determine root cause.",
                    "resolution_paths": "Mapping Needed — escalate to Dev + AMP for triage.",
                    "amp_needs": "To be determined.",
                    "dev_needs": "To be determined.",
                }
        else:
            key = ec["id"]
            class_occurrences[key].append(point_id)
            class_meta[key] = ec

    output_rows = [headers]
    # Classified rows first (in definition order), then unclassified
    ordered_keys = [ec["id"] for ec in ERROR_CLASSES] + sorted(
        k for k in class_meta if k.startswith("UNCLASSIFIED")
    )

    for key in ordered_keys:
        if key not in class_meta:
            continue
        ec = class_meta[key]
        occurrences = class_occurrences.get(key, [])
        unique_points = list(dict.fromkeys(p for p in occurrences if p))[:10]  # up to 10 sample IDs

        row = [
            ec.get("id", ""),
            ec.get("signature", ""),
            ec.get("plain_english", ""),
            f"Owner: {ec.get('owner', '')}\nWhy: {ec.get('owner_rationale', '')}",
            ec.get("diagnose", ""),
            ec.get("resolution_paths", ""),
            ec.get("amp_needs", ""),
            ec.get("dev_needs", ""),
            ", ".join(ec.get("table_fields", [])),
            ", ".join(unique_points),
            str(len(occurrences)),
        ]
        output_rows.append(row)

    return output_rows


# ── Google Sheets API ──────────────────────────────────────────────────────────

def get_sheets_service():
    """Authenticate and return a Sheets API service object."""
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_FILE, scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds)


def read_source_sheet(service) -> list[list]:
    """Read all values from the source sheet."""
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SPREADSHEET_ID, range=f"'{SOURCE_SHEET}'!A2:Z10000")
        .execute()
    )
    return result.get("values", [])


def ensure_catalog_sheet(service) -> int:
    """Create the catalog sheet if it doesn't exist; return its sheetId."""
    spreadsheet = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    for sheet in spreadsheet.get("sheets", []):
        if sheet["properties"]["title"] == CATALOG_SHEET:
            return sheet["properties"]["sheetId"]

    # Create it
    body = {"requests": [{"addSheet": {"properties": {"title": CATALOG_SHEET}}}]}
    response = service.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body=body).execute()
    return response["replies"][0]["addSheet"]["properties"]["sheetId"]


def write_catalog_sheet(service, catalog_rows: list[list], sheet_id: int):
    """Clear and write catalog rows, then apply formatting."""
    # Clear existing content
    service.spreadsheets().values().clear(
        spreadsheetId=SPREADSHEET_ID, range=f"'{CATALOG_SHEET}'!A2:Z10000"
    ).execute()

    # Write data
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{CATALOG_SHEET}'!A1",
        valueInputOption="RAW",
        body={"values": catalog_rows},
    ).execute()

    num_cols = max(len(r) for r in catalog_rows)

    # Formatting requests
    requests = [
        # Freeze header row
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
        # Bold + background for header
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                           "startColumnIndex": 0, "endColumnIndex": num_cols},
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True},
                        "backgroundColor": {"red": 0.2, "green": 0.4, "blue": 0.7},
                        "horizontalAlignment": "CENTER",
                    }
                },
                "fields": "userEnteredFormat(textFormat,backgroundColor,horizontalAlignment)",
            }
        },
        # Wrap text for all data cells
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1,
                           "startColumnIndex": 0, "endColumnIndex": num_cols},
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)",
            }
        },
        # Auto-resize columns A and B (narrower signature); set fixed widths for others
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                           "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 280},
                "fields": "pixelSize",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                           "startIndex": 1, "endIndex": num_cols},
                "properties": {"pixelSize": 300},
                "fields": "pixelSize",
            }
        },
        # Alternating row colors
        {
            "addBanding": {
                "bandedRange": {
                    "bandedRangeId": 1,
                    "range": {"sheetId": sheet_id, "startRowIndex": 1,
                               "startColumnIndex": 0, "endColumnIndex": num_cols},
                    "rowProperties": {
                        "headerColor": {"red": 0.2, "green": 0.4, "blue": 0.7},
                        "firstBandColor": {"red": 1, "green": 1, "blue": 1},
                        "secondBandColor": {"red": 0.93, "green": 0.95, "blue": 0.99},
                    },
                }
            }
        },
    ]

    service.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID, body={"requests": requests}
    ).execute()

    print(f"✅ '{CATALOG_SHEET}' written with {len(catalog_rows) - 1} error classes.")


def main():
    print("Connecting to Google Sheets…")
    service = get_sheets_service()

    print(f"Reading '{SOURCE_SHEET}'…")
    raw = read_source_sheet(service)

    # Skip title row (row 0) and header row (row 1); data starts at row 2
    data_rows = raw[2:] if len(raw) > 2 else []
    print(f"  {len(data_rows)} data rows found.")

    print("Classifying errors…")
    catalog_rows = build_catalog_rows(data_rows)
    print(f"  {len(catalog_rows) - 1} unique error classes generated.")

    print(f"Ensuring '{CATALOG_SHEET}' tab exists…")
    sheet_id = ensure_catalog_sheet(service)

    print("Writing catalog…")
    write_catalog_sheet(service, catalog_rows, sheet_id)


if __name__ == "__main__":
    main()
