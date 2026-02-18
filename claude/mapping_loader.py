"""
mapping_loader.py

Pulls the field mapping sheet from Google Sheets via the Sheets API
and parses it into a structured FieldMap used by the verifier.

Sheet expected columns (by name, order-independent):
    NMAquifer_TableField    → source  "table.field"
    Final Schema Target     → final   "table.field"
    Migration Path          → "direct-to-final" | "stage then refactor"
    Temp Schema Target      → temp    "table.field"  (may be blank for direct)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

MIGRATION_PATH_DIRECT = "direct-to-final"
MIGRATION_PATH_STAGE  = "stage then refactor"
MIGRATION_PATH_NA     = "n/a"          # field not being migrated — skip entirely

@dataclass
class FieldMapping:
    """One row from the mapping sheet."""
    source_table:  str
    source_field:  str
    final_table:   str
    final_field:   str
    migration_path: str                  # MIGRATION_PATH_DIRECT or _STAGE
    temp_table:    Optional[str] = None
    temp_field:    Optional[str] = None

    # Set by config: fields whose values should NOT be compared, just existence-checked
    is_transformed: bool = False

    @property
    def target_table(self) -> str:
        """The table we should actually verify against given migration_path."""
        if self.migration_path == MIGRATION_PATH_STAGE:
            return self.temp_table or self.final_table
        return self.final_table

    @property
    def target_field(self) -> str:
        """The field we should actually verify against given migration_path."""
        if self.migration_path == MIGRATION_PATH_STAGE:
            return self.temp_field or self.final_field
        return self.final_field

    @property
    def target_label(self) -> str:
        return f"{self.target_table}.{self.target_field}"

    @property
    def source_label(self) -> str:
        return f"{self.source_table}.{self.source_field}"


@dataclass
class MappingIndex:
    """
    All mappings, indexed for fast lookup.

    Attributes
    ----------
    mappings        : flat list of all FieldMapping rows
    by_source_table : source_table → [FieldMapping, ...]
    by_target_table : target_table → [FieldMapping, ...]
    source_tables   : set of all source table names
    target_tables   : set of all target table names (resolved for path)
    unmapped_source_warning : source labels with no valid target
    """
    mappings:         list[FieldMapping]   = field(default_factory=list)
    by_source_table:  dict[str, list[FieldMapping]] = field(default_factory=dict)
    by_target_table:  dict[str, list[FieldMapping]] = field(default_factory=dict)
    source_tables:    set[str]             = field(default_factory=set)
    target_tables:    set[str]             = field(default_factory=set)
    parse_warnings:   list[str]            = field(default_factory=list)


# ---------------------------------------------------------------------------
# Sheet loader
# ---------------------------------------------------------------------------

REQUIRED_COLS = {
    "NMAquifer_TableField",
    "Final Schema Target",
    "Migration Path",
}
OPTIONAL_COLS = {"Temp Schema Target"}

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def _parse_table_field(raw: str, context: str) -> tuple[str, str] | None:
    """
    Parse "table.field" → (table, field).
    Returns None and logs a warning if the format is wrong.
    """
    raw = raw.strip()
    if not raw:
        return None
    parts = raw.split(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        logging.warning(f"  Could not parse '{raw}' as table.field in [{context}]")
        return None
    return parts[0].strip(), parts[1].strip()


def _normalise_path(raw: str) -> str:
    """Normalise migration path value to one of the known constants."""
    cleaned = raw.strip().lower()
    if not cleaned or cleaned in ("n/a", "na", "not migrated", "skip", "—", "-"):
        return MIGRATION_PATH_NA
    if "stage" in cleaned or "refactor" in cleaned:
        return MIGRATION_PATH_STAGE
    return MIGRATION_PATH_DIRECT


def load_mapping_from_sheet(
    service_account_file: str,
    spreadsheet_id: str,
    sheet_range: str,
    transformed_fields: list[str],   # list of "source_table.source_field" to flag
) -> MappingIndex:
    """
    Pull the mapping sheet from Google Sheets and return a MappingIndex.

    Parameters
    ----------
    service_account_file : path to the GCP service account JSON key
    spreadsheet_id       : the ID from the sheet URL
                           (.../spreadsheets/d/<ID>/edit)
    sheet_range          : e.g. "Sheet1!A:D" or "Mapping!A:E"
    transformed_fields   : list of "old_table.old_field" labels whose values
                           should not be compared (flagged manual review)
    """
    logging.info(f"Loading mapping sheet from Google Sheets (id={spreadsheet_id})…")

    creds = Credentials.from_service_account_file(
        service_account_file, scopes=SCOPES
    )
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    sheet_api = service.spreadsheets()

    result = sheet_api.values().get(
        spreadsheetId=spreadsheet_id,
        range=sheet_range,
    ).execute()

    rows = result.get("values", [])
    if not rows:
        raise ValueError("Google Sheet returned no data.")

    # -- Parse header row (column names, strip whitespace)
    header = [h.strip() for h in rows[0]]
    missing = REQUIRED_COLS - set(header)
    if missing:
        raise ValueError(
            f"Sheet is missing required column(s): {missing}\n"
            f"Found columns: {header}"
        )

    col = {name: header.index(name) for name in header}
    transformed_set = {s.strip() for s in transformed_fields}
    warnings: list[str] = []
    mappings: list[FieldMapping] = []

    for i, row in enumerate(rows[1:], start=2):
        def cell(name: str) -> str:
            idx = col.get(name)
            if idx is None or idx >= len(row):
                return ""
            return row[idx].strip()

        src_raw   = cell("NMAquifer_TableField")
        final_raw = cell("Final Schema Target")
        path_raw  = cell("Migration Path")
        temp_raw  = cell("Temp Schema Target") if "Temp Schema Target" in col else ""

        if not src_raw and not final_raw:
            continue  # blank row

        src = _parse_table_field(src_raw, f"row {i} source")
        if src is None:
            warnings.append(f"Row {i}: unparseable source '{src_raw}' — skipped")
            continue

        final = _parse_table_field(final_raw, f"row {i} final")
        if final is None:
            warnings.append(
                f"Row {i}: source '{src_raw}' has no parseable final target — "
                f"flagged as unmapped"
            )
            # Still record it so we can report it as unmapped
            final = ("__UNMAPPED__", "__UNMAPPED__")

        path = _normalise_path(path_raw)

        if path == MIGRATION_PATH_NA:
            continue  # field not being migrated — skip entirely

        temp: tuple[str, str] | None = None
        if temp_raw:
            temp = _parse_table_field(temp_raw, f"row {i} temp")
            if temp is None:
                warnings.append(
                    f"Row {i}: unparseable temp target '{temp_raw}' for "
                    f"source '{src_raw}' — will fall back to final target"
                )

        if path == MIGRATION_PATH_STAGE and temp is None:
            warnings.append(
                f"Row {i}: migration_path is 'stage then refactor' but no "
                f"Temp Schema Target for '{src_raw}' — using final target instead"
            )

        fm = FieldMapping(
            source_table=src[0],
            source_field=src[1],
            final_table=final[0],
            final_field=final[1],
            migration_path=path,
            temp_table=temp[0] if temp else None,
            temp_field=temp[1] if temp else None,
            is_transformed=(src_raw in transformed_set),
        )
        mappings.append(fm)

    if warnings:
        for w in warnings:
            logging.warning(f"  [mapping] {w}")

    # -- Build index
    by_src: dict[str, list[FieldMapping]] = {}
    by_tgt: dict[str, list[FieldMapping]] = {}

    for fm in mappings:
        by_src.setdefault(fm.source_table, []).append(fm)
        by_tgt.setdefault(fm.target_table, []).append(fm)

    index = MappingIndex(
        mappings=mappings,
        by_source_table=by_src,
        by_target_table=by_tgt,
        source_tables=set(by_src.keys()),
        target_tables=set(by_tgt.keys()),
        parse_warnings=warnings,
    )

    logging.info(
        f"Mapping loaded: {len(mappings)} field mappings across "
        f"{len(by_src)} source tables → {len(by_tgt)} target tables"
    )
    direct_count = sum(1 for m in mappings if m.migration_path == MIGRATION_PATH_DIRECT)
    stage_count  = sum(1 for m in mappings if m.migration_path == MIGRATION_PATH_STAGE)
    xform_count  = sum(1 for m in mappings if m.is_transformed)
    logging.info(
        f"  direct-to-final: {direct_count}  |  "
        f"stage-then-refactor: {stage_count}  |  "
        f"flagged-transformed: {xform_count}"
    )

    return index


def load_mapping_from_csv(
    csv_path: str,
    transformed_fields: list[str],
) -> MappingIndex:
    """
    Fallback: load mapping from a CSV export of the same sheet.
    Same column names expected.
    """
    import csv
    logging.info(f"Loading mapping from CSV: {csv_path}")

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows_raw = list(reader)

    # Reuse the same parsing logic by converting to list-of-rows format
    if not rows_raw:
        raise ValueError(f"CSV file {csv_path} is empty.")

    missing = REQUIRED_COLS - set(rows_raw[0].keys())
    if missing:
        raise ValueError(
            f"CSV missing required column(s): {missing}\n"
            f"Found: {list(rows_raw[0].keys())}"
        )

    transformed_set = {s.strip() for s in transformed_fields}
    warnings: list[str] = []
    mappings: list[FieldMapping] = []

    for i, row in enumerate(rows_raw, start=2):
        src_raw   = row.get("NMAquifer_TableField", "").strip()
        final_raw = row.get("Final Schema Target", "").strip()
        path_raw  = row.get("Migration Path", "").strip()
        temp_raw  = row.get("Temp Schema Target", "").strip()

        if not src_raw and not final_raw:
            continue

        src = _parse_table_field(src_raw, f"row {i} source")
        if src is None:
            warnings.append(f"Row {i}: unparseable source '{src_raw}' — skipped")
            continue

        final = _parse_table_field(final_raw, f"row {i} final")
        if final is None:
            warnings.append(f"Row {i}: no parseable final target for '{src_raw}'")
            final = ("__UNMAPPED__", "__UNMAPPED__")

        path = _normalise_path(path_raw)

        if path == MIGRATION_PATH_NA:
            continue  # field not being migrated — skip entirely

        temp = _parse_table_field(temp_raw, f"row {i} temp") if temp_raw else None

        if path == MIGRATION_PATH_STAGE and temp is None:
            warnings.append(
                f"Row {i}: stage-then-refactor but no temp target for '{src_raw}'"
            )

        fm = FieldMapping(
            source_table=src[0],
            source_field=src[1],
            final_table=final[0],
            final_field=final[1],
            migration_path=path,
            temp_table=temp[0] if temp else None,
            temp_field=temp[1] if temp else None,
            is_transformed=(src_raw in transformed_set),
        )
        mappings.append(fm)

    for w in warnings:
        logging.warning(f"  [mapping] {w}")

    by_src: dict[str, list[FieldMapping]] = {}
    by_tgt: dict[str, list[FieldMapping]] = {}
    for fm in mappings:
        by_src.setdefault(fm.source_table, []).append(fm)
        by_tgt.setdefault(fm.target_table, []).append(fm)

    return MappingIndex(
        mappings=mappings,
        by_source_table=by_src,
        by_target_table=by_tgt,
        source_tables=set(by_src.keys()),
        target_tables=set(by_tgt.keys()),
        parse_warnings=warnings,
    )
