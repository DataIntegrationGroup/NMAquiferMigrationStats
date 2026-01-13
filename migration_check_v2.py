#!/usr/bin/env python
# -*- coding: utf-8 -*-

import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple
import csv

import pandas as pd
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

import pg8000



# =============== CONFIG =================
NMA_COUNTS_CSV = "nma_aquifer_nonnull_counts.csv"   # from your other script
# expected columns: table_field, nonnull_count, schema, table, field

# =============== CONFIG (Proxy / Localhost) =================

DB_HOST = "127.0.0.1"
DB_PORT = 5432

DB_USER = "marissa.fichera@nmt.edu"   # IAM DB user (your email)
DB_NAME = "ocotillo-staging"

# If PyCharm worked only after using a token as password, do the same here:
# In PowerShell:  gcloud sql generate-login-token
DB_PASS = ""  # or set from env var (recommended)


SERVICE_ACCOUNT_FILE = "service_account.json"
SPREADSHEET_ID = "1NtkaSWh8COQpMXd9AZ-fXMsRok9l-wwC1sz0lgVCTeo"
SHEET_NAME = "MIGRATION_STATUS"
WHY_SHEET_NAME = "Why_MIGRATION_STATUS"


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
    "SurfaceWaterData",
    "WaterLevelsContinuous_Pressure_Daily"
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
COL_DEST_TYPE = "Destination Type"
COL_REFACTOR_STAGE = "Refactor Stage"
COL_WHY = "Why"
COL_NMA_FIELD_COUNT = "NMAquifer NonNull Count"
COL_OCO_FIELD_COUNT = "Ocotillo NonNull Count"
COL_FIELD_DIFF = "NonNull Diff"


# Values (exact strings written)
VAL_STAGE_THEN_REFACTOR = "stage then refactor"
VAL_DIRECT_TO_FINAL = "direct-to-final"
VAL_UNKNOWN = "unknown"

VAL_DEFINED_EXISTS = "defined + target exists"
VAL_DEFINED_MISSING = "defined + target missing"
VAL_UNDEFINED = "undefined"
VAL_NOT_STARTED = "undefined"

VAL_COMPLETE = "complete"
VAL_INCOMPLETE = "incomplete"
VAL_BLOCKED = "incomplete"

VAL_NA_UPPER = "N/A"   # for the special 1:1 NA branch you specified
VAL_NA_LOWER = "N/A"   # mapping status for non-1:1 N/A per your text
VAL_PATH_NA = "N/A"
VAL_NOT_BEING_MIGRATED = "not being migrated"


VAL_NEEDS_REVIEW = "NEEDS REVIEW"

DEBUG_PRINT_SAMPLES = False  # set False when you're satisfied

# 1:1 override destinations (old table.field -> list of actual destination table.field(s))
OVERRIDES_RAW = {
    "WaterLevelsContinuous_Acoustic.DateMeasured": [
        "transducer_observation.observation_datetime",
    ],
    "WaterLevelsContinuous_Acoustic.DepthToWaterBGS": [
        "transducer_observation.value",
    ],
    "WaterLevelsContinuous_Acoustic.PublicRelease": [
        "transducer_observation.release_status",
    ],
    "WaterLevelsContinuous_Pressure.DateMeasured": [
        "transducer_observation.observation_datetime",
    ],
    "WaterLevelsContinuous_Pressure.DepthToWaterBGS": [
        "transducer_observation.value",
    ],
    "WaterLevelsContinuous_Pressure.QCed": [
        "transducer_observation.release_status",
        "transducer_observation_block.review_status",
    ],
}







# =======================================
def load_nma_counts_csv(csv_path: str) -> dict[tuple[str, str], int]:
    """
    Reads: table_field,nonnull_count,schema,table,field
    Returns: (table_lower, field_lower) -> nonnull_count
    """
    out: dict[tuple[str, str], int] = {}

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise RuntimeError(f"{csv_path} has no header row")

        if "table_field" not in reader.fieldnames or "nonnull_count" not in reader.fieldnames:
            raise RuntimeError(
                f"{csv_path} must have columns: table_field, nonnull_count. Found: {reader.fieldnames}"
            )

        for row in reader:
            tf = (row.get("table_field") or "").strip()
            if not tf or "." not in tf:
                continue

            t, c = tf.split(".", 1)
            t = normalize_key(t)
            c = normalize_key(c)

            cnt_raw = (row.get("nonnull_count") or "").strip()
            try:
                cnt = int(cnt_raw) if cnt_raw != "" else 0
            except ValueError:
                cnt = 0

            out[(t, c)] = cnt

    return out


def build_identifier_maps_from_rows(table_field_rows: list[str]):
    table_actual = {}
    col_actual = {}
    for tf in table_field_rows:
        if "." not in tf:
            continue
        t, c = tf.split(".", 1)
        t = t.strip()
        c = c.strip()
        tl = t.lower()
        cl = c.lower()
        table_actual.setdefault(tl, t)
        col_actual[(tl, cl)] = c
    return table_actual, col_actual


def fetch_schema_table_field_rows(conn) -> list[str]:
    q = """
    SELECT table_name || '.' || column_name AS table_field
    FROM information_schema.columns
    WHERE table_schema='public'
    ORDER BY table_name, ordinal_position;
    """
    cur = conn.cursor()
    cur.execute(q)
    return [r[0] for r in cur.fetchall()]

def ensure_sheet_exists(service, spreadsheet_id: str, sheet_name: str) -> None:
    ss = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheets = ss.get("sheets", [])
    existing = {s["properties"]["title"] for s in sheets}

    if sheet_name in existing:
        return

    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {"addSheet": {"properties": {"title": sheet_name}}}
            ]
        }
    ).execute()

def destination_type(dest_val: str, old_tf: str) -> str:
    """
    Classify destination for readability.
    """
    if is_blank(dest_val):
        return ""
    if normalize_key(dest_val) == normalize_key(VAL_NOT_BEING_MIGRATED):
        return VAL_NOT_BEING_MIGRATED
    if dest_equals_old(dest_val, old_tf):
        return "old"
    if dest_is_staging(dest_val):
        return "staging"
    return "final"


def refactor_stage(path_val: str, dest_type: str) -> str:
    """
    A simple readable stage label.
    """
    if normalize_key(path_val) == normalize_key(VAL_NA_UPPER):
        return VAL_NOT_BEING_MIGRATED
    if dest_type in ("", None):
        return ""
    if dest_type == VAL_NOT_BEING_MIGRATED:
        return VAL_NOT_BEING_MIGRATED
    # If you’re still staging, you’re in the staging stage; otherwise final stage
    return "staging" if dest_type == "staging" else "final"


def dest_is_staging(dest_tf: str) -> bool:
    """
    True if Destination points to staging nma_* table OR has nma_* field.
    """
    table, field = split_table_field(dest_tf)
    return normalize_key(table).startswith("nma_") or normalize_key(field).startswith("nma_")

def dest_equals_old(dest_tf: str, old_tf: str) -> bool:
    return normalize_key(dest_tf) == normalize_key(old_tf)

def field_starts_with_nma(tf: str) -> bool:
    """
    True if tf looks like table.field and the field starts with 'nma_'
    """
    try:
        _, field = split_table_field(tf)  # preserves original case
    except Exception:
        return False
    return normalize_key(field).startswith("nma_")


def build_identifier_maps(df_oc: pd.DataFrame) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    """
    Returns:
      table_actual: {table_lower: actual_table_name}
      col_actual: {(table_lower, col_lower): actual_col_name}
    """
    table_actual: dict[str, str] = {}
    col_actual: dict[tuple[str, str], str] = {}

    for tf in df_oc[OCOTILLO_CSV_COL].dropna().astype(str):
        tf = tf.strip()
        if "." not in tf:
            continue
        t, c = tf.split(".", 1)
        t = t.strip()
        c = c.strip()

        tl = t.lower()
        cl = c.lower()

        table_actual.setdefault(tl, t)
        col_actual[(tl, cl)] = c

    return table_actual, col_actual


def normalize_table_for_db(table: str) -> str:
    # Best-effort: match typical Postgres naming conventions
    # (lowercase, spaces -> underscores)
    return (table or "").strip().lower().replace(" ", "_")

def make_nma_stage_table_field(old_tf: str) -> str:
    """
    Old:  Table.Field
    Stage in Postgres: NMA_<Table>.<Field>
      - table: spaces -> underscores, preserve original case
      - field: preserve original case (we normalize for lookups later)
    """
    table, field = split_table_field(old_tf)
    table = (table or "").strip()
    field = (field or "").strip()
    if not table or not field:
        return ""
    table_stage = "NMA_" + re.sub(r"\s+", "_", table)
    return f"{table_stage}.{field}"


def export_ocotillo_current_csv(out_path: str) -> None:
    """
    Pulls all public table.field names from the DB and writes ocotillo_current.csv
    with header: table_field
    """
    query = """
            SELECT table_name || '.' || column_name AS table_field
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, ordinal_position; \
            """

    conn = None
    try:
        conn = pg8000.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASS,  # token or blank depending on your setup
            database=DB_NAME,
        )
        cur = conn.cursor()

        # Who am I / where am I?
        cur.execute("""
        SELECT
          current_database() AS db,
          current_user AS current_user,
          current_schema() AS current_schema,
          current_setting('search_path') AS search_path;
        """)
        print("SESSION:", cur.fetchone())

        # Count tables in public (exists)
        cur.execute("""
                    SELECT COUNT(*)
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_type = 'BASE TABLE';
                    """)
        print("public base tables (information_schema.tables):", cur.fetchone()[0])

        # Count tables with visible columns (privilege-filtered)
        cur.execute("""
                    SELECT COUNT(DISTINCT table_name)
                    FROM information_schema.columns
                    WHERE table_schema = 'public';
                    """)
        print("public tables with visible columns (information_schema.columns):", cur.fetchone()[0])

        # List the tables your export query will “see”
        cur.execute("""
                    SELECT DISTINCT table_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                    ORDER BY table_name;
                    """)
        tables = [r[0] for r in cur.fetchall()]
        print("tables seen by columns query:", len(tables))
        print("first 20:", tables[:20])

        cur = conn.cursor()
        cur.execute(query)
        rows = cur.fetchall()




        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["table_field"])
            w.writerows(rows)

        print(f"✓ Wrote {len(rows)} rows to {out_path}")
    finally:
        if conn is not None:
            conn.close()



import re
from collections import defaultdict

def parse_table_field(tf: str) -> tuple[str, str]:
    parts = [p.strip() for p in (tf or "").split(".") if p.strip()]
    if len(parts) != 2:
        raise ValueError(f"Bad Destination format (expected Table.Field): {tf}")
    table = parts[0].lower()
    col = parts[1].lower()
    return table, col

def build_nonnull_count_lookup(conn, table_to_cols, table_actual, col_actual):
    """
    Returns dict[(table_lower, col_lower)] = int nonnull_count
    Uses: SELECT COUNT("col1"), COUNT("col2"), ... FROM "table";
    """
    out = {}

    cur = conn.cursor()
    for t_lower, cols_set in table_to_cols.items():
        cols = sorted(cols_set)

        t_name = table_actual.get(t_lower, t_lower)  # exact table name
        t_sql = qident(t_name)

        exprs = []
        for c_lower in cols:
            c_name = col_actual.get((t_lower, c_lower), c_lower)
            c_sql = qident(c_name)
            exprs.append(f"COUNT({c_sql}) AS {qident(c_name)}")  # COUNT(col) == count non-null

        q = "SELECT " + ", ".join(exprs) + f" FROM {t_sql};"

        try:
            cur.execute(q)
            row = cur.fetchone()  # ints aligned with cols
            for c_lower, cnt in zip(cols, row):
                out[(t_lower, c_lower)] = int(cnt)
        except Exception as e:
            print(f"COUNT check failed for table {t_name}: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
            for c_lower in cols:
                out[(t_lower, c_lower)] = None

    return out


def qident(name: str) -> str:
    """
    Quote a Postgres identifier safely for dynamic SQL.
    Doubles any embedded double-quotes per SQL spec.
    """
    if name is None:
        raise ValueError("Identifier is None")
    s = str(name)
    return '"' + s.replace('"', '""') + '"'


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

    has_nonnull = nonnull_lookup.get((t, c))
    if has_nonnull is None:
        return xfer_val  # leave whatever it was (e.g., "check transfer script")
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


def load_ocotillo_schema(csv_path: Path) -> tuple[set, dict[str, str], dict[tuple[str, str], str]]:
    if not csv_path.exists():
        sys.exit(f"{csv_path} not found. Export it from Postgres first.")
    df = pd.read_csv(csv_path, dtype=str)
    if OCOTILLO_CSV_COL not in df.columns:
        sys.exit(f"{csv_path} must have a '{OCOTILLO_CSV_COL}' column.")

    oc_set = set(df[OCOTILLO_CSV_COL].dropna().map(normalize_key))
    table_actual, col_actual = build_identifier_maps(df)
    return oc_set, table_actual, col_actual



def main():

    # 0) Export current schema from DB to CSV (before doing anything else)
    export_ocotillo_current_csv(OCOTILLO_CSV_PATH)

    # 1) Now load the CSV we just created
    oc_set, table_actual, col_actual = load_ocotillo_schema(Path(OCOTILLO_CSV_PATH))

    print(f"Loaded {len(oc_set)} Ocotillo <table>.<field> entries from {OCOTILLO_CSV_PATH}.")

    service = get_sheets_service()
    resp = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_NAME}'!A1:ZZ"

    ).execute()

    ensure_sheet_exists(service, SPREADSHEET_ID, WHY_SHEET_NAME)

    values = resp.get("values", [])
    if not values:
        sys.exit(f"Sheet '{SHEET_NAME}' is empty or not found.")

    headers = values[0]
    data_rows = values[1:]

    why_rows = []  # list of [old_tf, why_text]

    nma_count_lookup = load_nma_counts_csv(NMA_COUNTS_CSV)
    print(f"Loaded {len(nma_count_lookup)} NMA non-null counts from {NMA_COUNTS_CSV}")

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
    idx_dest_type = ensure_output_col(headers, data_rows, COL_DEST_TYPE)
    idx_ref_stage = ensure_output_col(headers, data_rows, COL_REFACTOR_STAGE)
    idx_nma_fc = ensure_output_col(headers, data_rows, COL_NMA_FIELD_COUNT)
    idx_oco_fc = ensure_output_col(headers, data_rows, COL_OCO_FIELD_COUNT)
    idx_fc_diff = ensure_output_col(headers, data_rows, COL_FIELD_DIFF)

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



    # ---- C) Build table->cols for per-field counts (1:1 tables only) ----
    from collections import defaultdict

    # after normalize_key is defined (or inside main)
    override_map = defaultdict(set)
    for old_tf_raw, new_list_raw in OVERRIDES_RAW.items():
        for new_tf_raw in new_list_raw:
            override_map[normalize_key(old_tf_raw)].add(normalize_key(new_tf_raw))


    nma_table_to_cols = defaultdict(set)  # source tables
    oco_table_to_cols = defaultdict(set)  # staging tables in Postgres

    for r in data_rows:
        old_tf0 = r[idx_old] if idx_old < len(r) else ""
        if not is_in_one_to_one_list(old_tf0):
            continue

        stage_tf = make_nma_stage_table_field(old_tf0)

        # 🔥 critical: only count if it exists in the exported schema
        if normalize_key(stage_tf) not in oc_set:
            continue

        try:
            dst_t, dst_c = parse_table_field(stage_tf)  # lowercases
            oco_table_to_cols[dst_t].add(dst_c)
        except ValueError:
            pass

    # Collect unique (table -> columns) from your sheet's Destination column
    table_to_cols = defaultdict(set)

    for r in data_rows:
        old_tf0 = r[idx_old] if idx_old < len(r) else ""
        new_tf0 = r[idx_new] if idx_new < len(r) else ""
        dest0 = r[idx_dest] if idx_dest < len(r) else ""

        candidates = [dest0]

        if is_in_one_to_one_list(old_tf0):
            # staging destination always a possible check target
            candidates.append(make_nma_stage_table_field(old_tf0))

            # final destination might be used if it exists
            if not is_blank(new_tf0) and not is_na_value(new_tf0):
                candidates.append(new_tf0)
        else:
            # non-1:1 rows usually use new target as destination
            if not is_blank(new_tf0) and not is_na_value(new_tf0):
                candidates.append(new_tf0)

        for d in candidates:
            if is_skip_destination(d):
                continue

            # ✅ Only do DB non-null checks for fields that EXIST in schema export
            if normalize_key(d) not in oc_set:
                continue

            try:
                t, c = parse_table_field(d)  # lowercases
                table_to_cols[t].add(c)
            except ValueError:
                continue

    nonnull_lookup = {}  # (table, col) -> bool

    import os
    import pg8000

    DB_HOST = "127.0.0.1"
    DB_PORT = 5432

    # If you're using IAM auth, DB_USER is usually your email
    # DB_USER = "marissa.fichera@nmt.edu"

    # Put your token in an env var if needed:
    # PowerShell: $env:DB_PASS = (gcloud sql generate-login-token)
    DB_PASS = os.getenv("DB_PASS", "")  # can be "" if auto-iam-authn works without a password

    conn = pg8000.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASS,
        database=DB_NAME,
    )

    # ---- E) Precompute per-field non-null counts for 1:1 tables (source + staging) ----
    # Source counts (NMAquifer side): uses the <table>.<field> from the sheet directly
    # Staging counts (Ocotillo side): uses nma_<table>.<field> (stage_tf)

    oco_count_lookup = build_nonnull_count_lookup(
        conn,
        oco_table_to_cols,
        table_actual,
        col_actual,
    )

    oco_count_lookup = build_nonnull_count_lookup(conn, oco_table_to_cols, table_actual, col_actual)

    try:
        cur = conn.cursor()
        for t, cols_set in table_to_cols.items():
            cols = sorted(cols_set)

            t_lower = t.lower()
            t_name = table_actual.get(t_lower, t)  # exact table name from information_schema

            t_sql = qident(t_name)
            exprs = []

            for c in cols:
                c_lower = c.lower()
                c_name = col_actual.get((t_lower, c_lower), c)  # exact column name for that table
                c_sql = qident(c_name)

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
                try:
                    conn.rollback()
                except Exception:
                    pass
                for c in cols:
                    nonnull_lookup[(t.lower(), c.lower())] = False

    finally:
        conn.close()

    updated_nma_fc = []
    updated_oco_fc = []
    updated_fc_diff = []
    updated_dest_type = []
    updated_ref_stage = []
    updated_why, updated_map, updated_path, updated_xfer, updated_dest, updated_ref, updated_new = [], [], [], [], [], [], []

    for r in data_rows:
        old_tf = r[idx_old] if idx_old < len(r) else ""
        new_tf = r[idx_new] if idx_new < len(r) else ""
        new_target_exists = (
                (not is_blank(new_tf)) and
                (not is_na_value(new_tf)) and
                (normalize_key(new_tf) in oc_set)
        )

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

        why = []  # list of reason tags for this row

        # --- 1:1 TABLES (EXACT MATCH ON TABLE NAME) ---
        stage_tf = make_nma_stage_table_field(old_tf)
        stage_exists = (
                (not is_blank(stage_tf)) and
                (normalize_key(stage_tf) in oc_set)
        )
        stage_key = normalize_key(stage_tf)
        stage_exists_in_oc = bool(stage_key) and (stage_key in oc_set)

        nma_fc = ""
        oco_fc = ""
        fc_diff = ""

        if is_in_one_to_one_list(old_tf):
            old_table, old_field = split_table_field(old_tf)

            # NMA: lookup based on original table/field from sheet
            nma_t = normalize_key(old_table)
            nma_c = normalize_key(old_field)

            # Ocotillo staging: lookup based on generated stage table.field
            stage_tf = make_nma_stage_table_field(old_tf)
            try:
                oco_t, oco_c = parse_table_field(stage_tf)  # already lower
            except ValueError:
                oco_t, oco_c = "", ""

            nma_cnt = nma_count_lookup.get((nma_t, nma_c))
            oco_cnt = oco_count_lookup.get((oco_t, oco_c))
            oco_fc = "" if oco_cnt is None else str(oco_cnt)

            if nma_cnt is not None:
                nma_fc = str(nma_cnt)
            if oco_cnt is not None:
                oco_fc = str(oco_cnt)
            if (nma_cnt is not None) and (oco_cnt is not None):
                fc_diff = str(int(nma_cnt) - int(oco_cnt))

        updated_nma_fc.append(nma_fc)
        updated_oco_fc.append(oco_fc)
        updated_fc_diff.append(fc_diff)

        if is_in_one_to_one_list(old_tf):
            why.append("row=1to1")

            # 0) not being migrated (keep exactly as your current behavior)
            if is_na_value(new_tf) or normalize_key(new_tf) == normalize_key(VAL_NOT_BEING_MIGRATED):
                new_tf = VAL_NOT_BEING_MIGRATED
                map_val = VAL_NA_UPPER
                path_val = VAL_NA_UPPER
                dest_val = VAL_NOT_BEING_MIGRATED
                xfer_val = VAL_NOT_BEING_MIGRATED
                ref_val = VAL_NOT_BEING_MIGRATED
                why.append("1to1=not_being_migrated")

                d_type = destination_type(dest_val, old_tf)
                r_stage = refactor_stage(path_val, d_type)

                updated_dest_type.append(d_type)
                updated_ref_stage.append(r_stage)
                why_rows.append([old_tf, "; ".join(why)])

                updated_map.append(map_val)
                updated_path.append(path_val)
                updated_xfer.append(xfer_val)
                updated_dest.append(dest_val)
                updated_ref.append(ref_val)
                updated_new.append(new_tf)
                continue

            # 1) Decide if THIS ROW is one of the explicit overrides
            old_k = normalize_key(old_tf)
            new_k = normalize_key(new_tf)
            is_override_row = (old_k in override_map) and (new_k in override_map[old_k])

            if is_override_row:
                # OVERRIDE: use final destination (new_tf) and allow "complete"
                dest_val = new_tf
                path_val = VAL_DIRECT_TO_FINAL

                # mapping status for override: depends if the final target exists in ocotillo schema export
                if (not is_blank(dest_val)) and (normalize_key(dest_val) in oc_set):
                    map_val = VAL_DEFINED_EXISTS
                    why.append("1to1=override_final_target_exists")
                else:
                    map_val = VAL_DEFINED_MISSING
                    why.append("1to1=override_final_target_missing")

                # Transfer status from DB non-null check (if dest exists)
                if is_blank(dest_val) or normalize_key(dest_val) not in oc_set:
                    xfer_val = VAL_INCOMPLETE
                    why.append("xfer=override_dest_blank_or_missing")
                else:
                    before = xfer_val
                    xfer_val = apply_transfer_status_from_db(dest_val, xfer_val, nonnull_lookup)
                    if before != xfer_val:
                        why.append(f"db_nonnull={xfer_val}")

                # Refactor COMPLETE for override rows if dest is truly final and transfer complete
                if (normalize_key(dest_val) in oc_set) and (not dest_is_staging(dest_val)) and (
                        normalize_key(xfer_val) == normalize_key(VAL_COMPLETE)):
                    ref_val = VAL_COMPLETE
                    why.append("ref=complete_override_final")
                else:
                    ref_val = VAL_INCOMPLETE
                    why.append("ref=incomplete_override_not_done")

            else:
                # DEFAULT: stage-only (ignore New Schema Target entirely)
                stage_tf = make_nma_stage_table_field(old_tf)
                stage_exists = (not is_blank(stage_tf)) and (normalize_key(stage_tf) in oc_set)

                dest_val = stage_tf if stage_exists else ""
                path_val = VAL_STAGE_THEN_REFACTOR
                ref_val = VAL_INCOMPLETE
                why.append("1to1=default_stage_only")

                # mapping status: staging exists or missing
                map_val = VAL_DEFINED_EXISTS if stage_exists else VAL_DEFINED_MISSING

                # Transfer status from DB non-null check against staging destination
                if is_blank(dest_val):
                    xfer_val = VAL_INCOMPLETE
                    why.append("xfer=dest_blank")
                else:
                    before = xfer_val
                    xfer_val = apply_transfer_status_from_db(dest_val, xfer_val, nonnull_lookup)
                    if before != xfer_val:
                        why.append(f"db_nonnull={xfer_val}")

            d_type = destination_type(dest_val, old_tf)
            r_stage = refactor_stage(path_val, d_type)

            updated_dest_type.append(d_type)
            updated_ref_stage.append(r_stage)
            updated_why.append("; ".join(why))

            updated_map.append(map_val)
            updated_path.append(path_val)
            updated_xfer.append(xfer_val)
            updated_dest.append(dest_val)
            updated_ref.append(ref_val)
            updated_new.append(new_tf)
            continue

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
        # Only auto-complete refactor when we are direct-to-final
        if normalize_key(path_val) == normalize_key(VAL_DIRECT_TO_FINAL):
            if normalize_key(dest_val) and normalize_key(dest_val) not in ("n/a", "na", "not being migrated"):
                if normalize_key(dest_val) != normalize_key(old_tf) and normalize_key(xfer_val) == normalize_key(
                        VAL_COMPLETE):
                    ref_val = VAL_COMPLETE

        xfer_val = apply_transfer_status_from_db(dest_val, xfer_val, nonnull_lookup)

        if normalize_key(map_val) == normalize_key(VAL_DEFINED_EXISTS):
            ref_val = VAL_COMPLETE

        if normalize_key(path_val) == normalize_key(VAL_NA_UPPER):
            new_tf = VAL_NOT_BEING_MIGRATED

        if field_starts_with_nma(new_tf):
            ref_val = VAL_INCOMPLETE
            why.append("new_target_field_is_nma=>ref_incomplete")

        d_type = destination_type(dest_val, old_tf)
        r_stage = refactor_stage(path_val, d_type)

        updated_dest_type.append(d_type)
        updated_ref_stage.append(r_stage)
        why_rows.append([old_tf, "; ".join(why)])
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
        (idx_dest_type, COL_DEST_TYPE, updated_dest_type),
        (idx_ref_stage, COL_REFACTOR_STAGE, updated_ref_stage),
        (idx_nma_fc, COL_NMA_FIELD_COUNT, updated_nma_fc),
        (idx_oco_fc, COL_OCO_FIELD_COUNT, updated_oco_fc),
        (idx_fc_diff, COL_FIELD_DIFF, updated_fc_diff),

    ]

    # Clear previous contents so old rows don’t linger
    service.spreadsheets().values().clear(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{WHY_SHEET_NAME}'!A:Z"
    ).execute()

    # Write header + data
    why_values = [["NMAquifer_TableField", "Why"]] + why_rows

    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{WHY_SHEET_NAME}'!A1:B{len(why_values)}",
        valueInputOption="RAW",
        body={"values": why_values}
    ).execute()

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
    # import traceback
    # try:
    #     main()
    # except Exception:
    #     traceback.print_exc()
    #     raise
    try:
        main()
    except Exception as e:
        sys.exit(str(e))

