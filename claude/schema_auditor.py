"""
schema_auditor.py

Compares what the mapping sheet CLAIMS should exist against what
actually exists in both the source and target databases.

Reports:
  - Source fields that exist in the DB but have NO mapping row (leak risk)
  - Target fields that exist in the DB but have NO mapping row (surprise columns)
  - Mapped fields where the source column doesn't actually exist in source DB
  - Mapped fields where the target column doesn't actually exist in target DB
  - Type mismatches between mapped source/target columns (warning, not error)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Engine

from mapping_loader import MappingIndex, FieldMapping


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ColumnInfo:
    name: str
    type_str: str
    nullable: bool


@dataclass
class TypeMismatch:
    source_label: str
    target_label: str
    source_type:  str
    target_type:  str


@dataclass
class MissingColumn:
    label: str          # "table.field"
    side:  str          # "source" or "target"
    mapping: FieldMapping


@dataclass
class UnmappedColumn:
    table: str
    column: str
    type_str: str
    side: str           # "source" or "target"


@dataclass
class SchemaAuditResult:
    type_mismatches:       list[TypeMismatch]    = field(default_factory=list)
    missing_in_source_db:  list[MissingColumn]   = field(default_factory=list)
    missing_in_target_db:  list[MissingColumn]   = field(default_factory=list)
    unmapped_source_cols:  list[UnmappedColumn]  = field(default_factory=list)
    unmapped_target_cols:  list[UnmappedColumn]  = field(default_factory=list)
    audit_warnings:        list[str]             = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return bool(
            self.missing_in_source_db
            or self.missing_in_target_db
            or self.unmapped_source_cols
            or self.unmapped_target_cols
        )

    @property
    def has_warnings(self) -> bool:
        return bool(self.type_mismatches or self.audit_warnings)


# ---------------------------------------------------------------------------
# Column introspection helpers
# ---------------------------------------------------------------------------

def _get_columns(
    engine: Engine,
    table: str,
    schema: Optional[str],
) -> dict[str, ColumnInfo]:
    """Return {col_name: ColumnInfo} for a table. Returns {} if table not found."""
    insp = sa_inspect(engine)
    try:
        cols = insp.get_columns(table, schema=schema)
    except Exception:
        return {}
    return {
        c["name"]: ColumnInfo(
            name=c["name"],
            type_str=str(c["type"]),
            nullable=c.get("nullable", True),
        )
        for c in cols
    }


def _types_compatible(src_type: str, tgt_type: str) -> bool:
    """
    Loose type compatibility check.  We compare the base type family
    (INT, VARCHAR, DATETIME, etc.) rather than exact type strings because
    MSSQL and Postgres use different names for the same logical types.
    """
    def family(t: str) -> str:
        t = t.upper()
        if any(x in t for x in ("INT", "SERIAL", "NUMERIC", "DECIMAL", "NUMBER")):
            return "NUMERIC"
        if any(x in t for x in ("CHAR", "TEXT", "CLOB", "NVAR", "STRING")):
            return "TEXT"
        if any(x in t for x in ("DATE", "TIME", "TIMESTAMP")):
            return "DATETIME"
        if any(x in t for x in ("BOOL", "BIT")):
            return "BOOL"
        if any(x in t for x in ("BLOB", "BINARY", "BYTES", "BYTEA", "IMAGE")):
            return "BINARY"
        if any(x in t for x in ("FLOAT", "DOUBLE", "REAL")):
            return "FLOAT"
        return t.split("(")[0].strip()  # fall back to raw base type

    return family(src_type) == family(tgt_type)


# ---------------------------------------------------------------------------
# Auditor
# ---------------------------------------------------------------------------

class SchemaAuditor:

    def __init__(
        self,
        src_engine: Engine,
        tgt_engine: Engine,
        src_schema: Optional[str],
        tgt_schema: Optional[str],
        mapping_index: MappingIndex,
        check_unmapped_source: bool = True,
        check_unmapped_target: bool = True,
    ):
        self.src_engine = src_engine
        self.tgt_engine = tgt_engine
        self.src_schema = src_schema
        self.tgt_schema = tgt_schema
        self.idx = mapping_index
        self.check_unmapped_source = check_unmapped_source
        self.check_unmapped_target = check_unmapped_target

        # Cache column introspection per table to avoid repeated queries
        self._src_col_cache: dict[str, dict[str, ColumnInfo]] = {}
        self._tgt_col_cache: dict[str, dict[str, ColumnInfo]] = {}

    def _src_cols(self, table: str) -> dict[str, ColumnInfo]:
        if table not in self._src_col_cache:
            self._src_col_cache[table] = _get_columns(
                self.src_engine, table, self.src_schema
            )
        return self._src_col_cache[table]

    def _tgt_cols(self, table: str) -> dict[str, ColumnInfo]:
        if table not in self._tgt_col_cache:
            self._tgt_col_cache[table] = _get_columns(
                self.tgt_engine, table, self.tgt_schema
            )
        return self._tgt_col_cache[table]

    def run(self) -> SchemaAuditResult:
        result = SchemaAuditResult()

        logging.info("Running schema audit…")

        # --- 1. For every mapped field: does it actually exist in source + target?
        #         And if both exist, do their types match?
        for fm in self.idx.mappings:
            if fm.final_table == "__UNMAPPED__":
                continue  # already warned during mapping load

            # Source side
            src_cols = self._src_cols(fm.source_table)
            if not src_cols:
                result.audit_warnings.append(
                    f"Source table '{fm.source_table}' not found in DB — "
                    f"cannot verify its fields"
                )
            elif fm.source_field not in src_cols:
                result.missing_in_source_db.append(
                    MissingColumn(fm.source_label, "source", fm)
                )

            # Target side
            tgt_cols = self._tgt_cols(fm.target_table)
            if not tgt_cols:
                result.audit_warnings.append(
                    f"Target table '{fm.target_table}' not found in DB — "
                    f"cannot verify its fields"
                )
            elif fm.target_field not in tgt_cols:
                result.missing_in_target_db.append(
                    MissingColumn(fm.target_label, "target", fm)
                )

            # Type mismatch check (only if both cols exist and not transformed)
            if (
                not fm.is_transformed
                and src_cols
                and tgt_cols
                and fm.source_field in src_cols
                and fm.target_field in tgt_cols
            ):
                src_type = src_cols[fm.source_field].type_str
                tgt_type = tgt_cols[fm.target_field].type_str
                if not _types_compatible(src_type, tgt_type):
                    result.type_mismatches.append(TypeMismatch(
                        source_label=fm.source_label,
                        target_label=fm.target_label,
                        source_type=src_type,
                        target_type=tgt_type,
                    ))

        # --- 2. Unmapped source columns (exist in DB, not in mapping sheet)
        if self.check_unmapped_source:
            mapped_src: dict[str, set[str]] = {}
            for fm in self.idx.mappings:
                mapped_src.setdefault(fm.source_table, set()).add(fm.source_field)

            for table in self.idx.source_tables:
                db_cols = self._src_cols(table)
                mapped_fields = mapped_src.get(table, set())
                for col_name, col_info in db_cols.items():
                    if col_name not in mapped_fields:
                        result.unmapped_source_cols.append(UnmappedColumn(
                            table=table,
                            column=col_name,
                            type_str=col_info.type_str,
                            side="source",
                        ))

        # --- 3. Unmapped target columns (exist in DB, not in mapping sheet)
        if self.check_unmapped_target:
            mapped_tgt: dict[str, set[str]] = {}
            for fm in self.idx.mappings:
                mapped_tgt.setdefault(fm.target_table, set()).add(fm.target_field)

            for table in self.idx.target_tables:
                if table == "__UNMAPPED__":
                    continue
                db_cols = self._tgt_cols(table)
                mapped_fields = mapped_tgt.get(table, set())
                for col_name, col_info in db_cols.items():
                    if col_name not in mapped_fields:
                        result.unmapped_target_cols.append(UnmappedColumn(
                            table=table,
                            column=col_name,
                            type_str=col_info.type_str,
                            side="target",
                        ))

        # --- Summary log
        logging.info(
            f"Schema audit complete: "
            f"{len(result.type_mismatches)} type mismatches | "
            f"{len(result.missing_in_source_db)} missing source cols | "
            f"{len(result.missing_in_target_db)} missing target cols | "
            f"{len(result.unmapped_source_cols)} unmapped source cols | "
            f"{len(result.unmapped_target_cols)} unmapped target cols"
        )
        if result.missing_in_source_db:
            for m in result.missing_in_source_db:
                logging.error(f"  [MISSING IN SOURCE DB] {m.label}")
        if result.missing_in_target_db:
            for m in result.missing_in_target_db:
                logging.error(f"  [MISSING IN TARGET DB] {m.label}")
        if result.type_mismatches:
            for tm in result.type_mismatches:
                logging.warning(
                    f"  [TYPE MISMATCH] {tm.source_label} "
                    f"({tm.source_type}) → {tm.target_label} ({tm.target_type})"
                )

        return result
