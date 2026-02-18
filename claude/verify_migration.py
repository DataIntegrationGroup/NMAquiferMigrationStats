"""
verify_migration.py  (v2 — mapping-aware)

Main entry point for the migration verifier.

What changed from v1:
  - Column names are NO LONGER assumed to match between source and target.
    Every field is looked up through the MappingIndex before comparison.
  - direct-to-final rows check Final Schema Target.
  - stage-then-refactor rows check Temp Schema Target.
  - Transformed fields are flagged "manual review required" — not diffed.
  - A schema audit runs before any data checks and is included in the report.
  - Unmapped source/target fields are reported as errors.
  - Type mismatches are reported as warnings.

Usage:
    python verify_migration.py --config config.yaml [--point-ids-file ids.txt] [--output-dir ./reports]

Exit codes:
    0 = all clean
    1 = data or schema discrepancies found
    2 = script/config error
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import yaml
from sqlalchemy import create_engine, inspect as sa_inspect, text
from sqlalchemy.engine import Engine

try:
    from google.cloud.sql.connector import Connector as CloudSQLConnector
except ImportError:
    CloudSQLConnector = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# Custom MSSQL dialect for pytds-via-Cloud-SQL-connector
# ---------------------------------------------------------------------------
# SQLAlchemy has no native pytds dialect. mssql+pyodbc works for SQL
# generation but its on_connect hook calls pyodbc-specific methods that
# don't exist on pytds connections.  We subclass it and strip that hook out.

from sqlalchemy.dialects.mssql.pyodbc import MSDialect_pyodbc  # noqa: E402

class _MSSQLPytdsDialect(MSDialect_pyodbc):
    """
    MSSQL dialect that uses pytds connections but inherits all of
    pyodbc's SQL generation / schema reflection logic.
    The pyodbc-specific on_connect hook is suppressed because pytds
    connection objects don't support add_output_converter().
    """
    driver = "pytds"

    @classmethod
    def import_dbapi(cls):          # SQLAlchemy 2.x hook
        import pytds
        return pytds

    @classmethod
    def dbapi(cls):                 # SQLAlchemy 1.x compat
        import pytds
        return pytds

    def _dbapi_version(self):       # pytds uses intversion, not version
        import pytds
        v = pytds.intversion
        # intversion is an int like 11002 → (1, 1, 2)
        return (v // 10000, (v % 10000) // 100, v % 100)

    def on_connect(self):           # suppress pyodbc-specific setup
        return None

    def create_connect_args(self, url):
        return [], {}               # connection is handled by creator=

# Register so SQLAlchemy resolves "mssql+pytds://" to our dialect
from sqlalchemy.dialects import registry as _sa_registry  # noqa: E402
_sa_registry.register("mssql.pytds", __name__, "_MSSQLPytdsDialect")

from mapping_loader import (
    MappingIndex, FieldMapping,
    load_mapping_from_sheet, load_mapping_from_csv,
    MIGRATION_PATH_DIRECT, MIGRATION_PATH_STAGE, MIGRATION_PATH_NA,
)
from schema_auditor import SchemaAuditor, SchemaAuditResult


# ---------------------------------------------------------------------------
# Cloud SQL engine factories
# ---------------------------------------------------------------------------

def _build_source_engine(config: dict) -> Engine:
    """
    Source = Cloud SQL SQL Server.
    Always uses the Cloud SQL Python Connector (handles SSL/auth automatically).
    SQL Server on Cloud SQL does NOT support IAM db auth, so a regular
    db username + password is still required — but no firewall rules or
    SSL cert management needed since the connector handles the secure tunnel.

    Config keys expected under 'cloud_sql_source':
        instance_connection_name : "project:region:instance"
        database                 : "your_db_name"
        user                     : "your_sql_server_user"
        password                 : "your_password"
        ip_type                  : "public" | "private"  (default: "public")
    """
    cs = config.get("cloud_sql_source")
    if cs:
        if CloudSQLConnector is None:
            raise ImportError(
                "cloud-sql-python-connector is not installed. "
                "Run: pip install 'cloud-sql-python-connector[pg8000,pytds]'"
            )
        connector = CloudSQLConnector(refresh_strategy="lazy")
        def getconn_src():
            return connector.connect(
                cs["instance_connection_name"],
                "pytds",
                user=cs["user"],
                password=cs["password"],
                db=cs["database"],
                ip_type=cs.get("ip_type", "public"),
            )
        logging.info(
            f"Source engine: Cloud SQL SQL Server via connector "
            f"({cs['instance_connection_name']})"
        )
        # Use our custom dialect that handles pytds connections without
        # attempting any pyodbc-specific setup
        return create_engine(
            "mssql+pytds://",
            creator=getconn_src,
            use_insertmanyvalues=False,
        )
    else:
        # Fallback: plain DSN (useful for local dev / non-Cloud-SQL source)
        logging.info("Source engine: plain DSN (no cloud_sql_source config found)")
        return create_engine(config["source_dsn"])


def _build_target_engine(config: dict) -> Engine:
    """
    Target = Cloud SQL PostgreSQL with IAM authentication.
    The connector picks up your gcloud ADC credentials automatically —
    no password needed as long as you've run:
        gcloud auth application-default login

    The IAM user must also exist as a database user inside the Cloud SQL
    instance (Cloud SQL Console → Users → Add IAM user).

    Config keys expected under 'cloud_sql_target':
        instance_connection_name : "project:region:instance"
        database                 : "your_db_name"
        iam_user                 : "your.email@domain.com"
                                   (or service account email)
        ip_type                  : "public" | "private"  (default: "public")
    """
    cs = config.get("cloud_sql_target")
    if cs:
        if CloudSQLConnector is None:
            raise ImportError(
                "cloud-sql-python-connector is not installed. "
                "Run: pip install 'cloud-sql-python-connector[pg8000,pytds]'"
            )
        connector = CloudSQLConnector(refresh_strategy="lazy")
        def getconn_tgt():
            return connector.connect(
                cs["instance_connection_name"],
                "pg8000",
                user=cs["iam_user"],
                db=cs["database"],
                ip_type=cs.get("ip_type", "public"),
                enable_iam_auth=True,
            )
        logging.info(
            f"Target engine: Cloud SQL Postgres via connector + IAM auth "
            f"({cs['instance_connection_name']}, user={cs['iam_user']})"
        )
        return create_engine("postgresql+pg8000://", creator=getconn_tgt)
    else:
        logging.info("Target engine: plain DSN (no cloud_sql_target config found)")
        return create_engine(config["target_dsn"])


# ---------------------------------------------------------------------------
# Result data structures
# ---------------------------------------------------------------------------

@dataclass
class FieldDiff:
    source_label:   str
    target_label:   str
    migration_path: str
    is_transformed: bool
    status: str              # "ok" | "manual_review" | "type_mismatch_warning"
                             # | "value_mismatch" | "error"
    source_row_count: int = 0
    target_row_count: int = 0
    missing_pks: list = field(default_factory=list)
    extra_pks:   list = field(default_factory=list)
    value_diffs: list = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class PointIDResult:
    point_id: Any
    fields_checked:       int = 0
    fields_ok:            int = 0
    fields_manual_review: int = 0
    fields_warned:        int = 0
    fields_failed:        int = 0
    field_diffs: list[FieldDiff] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.fields_failed == 0 and self.fields_checked > 0


@dataclass
class VerificationManifest:
    run_at:               str
    source_label:         str
    target_label:         str
    mapping_source:       str
    total_mapped_fields:  int
    point_ids_checked:    int
    point_ids_passed:     int
    point_ids_failed:     int
    schema_audit:         dict
    point_id_results: list[PointIDResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_pk_cols(engine: Engine, table: str, schema: Optional[str]) -> list[str]:
    insp = sa_inspect(engine)
    try:
        pk = insp.get_pk_constraint(table, schema=schema)
        cols = pk.get("constrained_columns", [])
        if cols:
            return cols
    except Exception:
        pass
    try:
        return [c["name"] for c in insp.get_columns(table, schema=schema)]
    except Exception:
        return []


def _build_join_to_anchor(
    engine: Engine,
    schema: Optional[str],
    start_table: str,
    anchor_table: str,
    max_depth: int = 8,
    manual_paths: Optional[dict] = None,
) -> Optional[str]:
    """BFS over FK graph to build a JOIN chain from start_table back to anchor_table.

    manual_paths: optional dict from config keyed by table name, each value is a
    list of {from_table, from_col, to_table, to_col} dicts describing the join hops
    in order from start_table toward anchor_table.
    """
    from collections import deque

    s = f'"{schema}".' if schema else ""

    # ── Manual path override ─────────────────────────────────────────────────
    if manual_paths and start_table in manual_paths:
        hops = manual_paths[start_table]
        clauses = []
        for hop in hops:
            clauses.append(
                f'JOIN {s}"{hop["to_table"]}" ON '
                f'{s}"{hop["from_table"]}"."{hop["from_col"]}" = '
                f'{s}"{hop["to_table"]}"."{hop["to_col"]}"'
            )
        logging.debug(f"FK path for '{start_table}' (manual): {clauses}")
        return " ".join(clauses)

    # ── BFS over introspected FK graph ───────────────────────────────────────
    insp = sa_inspect(engine)

    visited = {start_table}
    queue = deque([(start_table, [])])

    while queue:
        current, joins = queue.popleft()
        if len(joins) >= max_depth:
            continue
        try:
            fks = insp.get_foreign_keys(current, schema=schema)
            logging.debug(
                f"BFS: '{current}' → {len(fks)} FK(s): "
                + str([(f['constrained_columns'], f['referred_table'], f['referred_columns']) for f in fks])
            )
        except Exception as e:
            logging.debug(f"BFS: get_foreign_keys('{current}') raised: {e}")
            continue
        for fk in fks:
            ref_table = fk.get("referred_table")
            if not ref_table or ref_table in visited:
                continue
            lc = fk["constrained_columns"]
            rc = fk["referred_columns"]
            if not lc or not rc:
                continue
            join_clause = (
                f'JOIN {s}"{ref_table}" ON '
                f'{s}"{current}"."{lc[0]}" = {s}"{ref_table}"."{rc[0]}"'
            )
            new_joins = joins + [join_clause]
            if ref_table == anchor_table:
                logging.debug(f"BFS: found path for '{start_table}': {new_joins}")
                return " ".join(new_joins)
            visited.add(ref_table)
            queue.append((ref_table, new_joins))

    logging.debug(f"BFS: no path found from '{start_table}' to '{anchor_table}'")
    return None


def _fetch_col(
    engine: Engine,
    schema: Optional[str],
    table: str,
    col: str,
    pk_cols: list[str],
    anchor_table: str,
    anchor_pk: str,
    point_id: Any,
    manual_paths: Optional[dict] = None,
) -> pd.DataFrame:
    s = f'"{schema}".' if schema else ""
    select_cols = ", ".join(
        f'{s}"{table}"."{c}"' for c in dict.fromkeys(pk_cols + [col])
    )

    if table == anchor_table:
        sql = (
            f'SELECT {select_cols} FROM {s}"{table}" '
            f'WHERE "{anchor_pk}" = :pid'
        )
    else:
        join_sql = _build_join_to_anchor(engine, schema, table, anchor_table,
                                         manual_paths=manual_paths)
        if join_sql is None:
            raise ValueError(
                f"No FK path found from '{table}' to anchor '{anchor_table}'. "
                f"Verify FK constraints exist in the DB, or check exclude_tables config."
            )
        sql = (
            f'SELECT {select_cols} FROM {s}"{table}" '
            f'{join_sql} '
            f'WHERE {s}"{anchor_table}"."{anchor_pk}" = :pid'
        )

    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn, params={"pid": point_id})


# ---------------------------------------------------------------------------
# Value diff
# ---------------------------------------------------------------------------

def _diff_field(
    src_df: pd.DataFrame,
    tgt_df: pd.DataFrame,
    src_col: str,
    tgt_col: str,
    pk_cols: list[str],
    max_diffs: int,
) -> dict:
    def key(row, cols):
        return tuple(str(row[c]) for c in cols if c in row.index)

    src_map = {key(r, pk_cols): r.get(src_col) for _, r in src_df.iterrows()}
    tgt_map = {key(r, pk_cols): r.get(tgt_col) for _, r in tgt_df.iterrows()}

    missing_pks = [list(k) for k in src_map.keys() - tgt_map.keys()]
    extra_pks   = [list(k) for k in tgt_map.keys() - src_map.keys()]
    value_diffs = []

    for k in src_map.keys() & tgt_map.keys():
        sv, tv = src_map[k], tgt_map[k]
        sv_null = pd.isna(sv) if not isinstance(sv, (list, dict)) else False
        tv_null = pd.isna(tv) if not isinstance(tv, (list, dict)) else False
        if sv_null and tv_null:
            continue
        if str(sv) != str(tv):
            value_diffs.append({"pk": list(k), "src_val": str(sv), "tgt_val": str(tv)})

    return {
        "missing_pks": missing_pks[:max_diffs],
        "extra_pks":   extra_pks[:max_diffs],
        "value_diffs": value_diffs[:max_diffs],
    }


# ---------------------------------------------------------------------------
# Core verifier
# ---------------------------------------------------------------------------

class MigrationVerifier:

    def __init__(self, config: dict, mapping_index: MappingIndex):
        self.config     = config
        self.idx        = mapping_index
        self.src_engine = _build_source_engine(config)
        self.tgt_engine = _build_target_engine(config)
        self.src_schema = config.get("source_schema")
        self.tgt_schema = config.get("target_schema")
        # Source and target may have different anchor tables/PKs.
        # target_anchor_table/pk fall back to anchor_table/pk if not set.
        self.src_anchor_table = config["anchor_table"]
        self.src_anchor_pk    = config["anchor_pk"]
        self.tgt_anchor_table = config.get("target_anchor_table", self.src_anchor_table)
        self.tgt_anchor_pk    = config.get("target_anchor_pk",    self.src_anchor_pk)
        self.max_diffs    = config.get("max_diff_rows", 200)
        self.src_manual_paths = config.get("manual_fk_paths", {}).get("source", {})
        self.tgt_manual_paths = config.get("manual_fk_paths", {}).get("target", {})
        self.exclude_tables   = set(config.get("exclude_tables", []))
        self.exclude_src_prefixes = tuple(config.get("exclude_source_prefixes", ["LU_"]))
        self._src_pk_cache: dict[str, list[str]] = {}
        self._tgt_pk_cache: dict[str, list[str]] = {}

    def _src_pks(self, table: str) -> list[str]:
        if table not in self._src_pk_cache:
            self._src_pk_cache[table] = _get_pk_cols(
                self.src_engine, table, self.src_schema
            )
        return self._src_pk_cache[table]

    def _tgt_pks(self, table: str) -> list[str]:
        if table not in self._tgt_pk_cache:
            self._tgt_pk_cache[table] = _get_pk_cols(
                self.tgt_engine, table, self.tgt_schema
            )
        return self._tgt_pk_cache[table]

    def verify_point(self, point_id: Any) -> PointIDResult:
        result = PointIDResult(point_id=point_id)

        for fm in self.idx.mappings:
            # Skip lookup/reference tables and explicitly excluded tables
            if (fm.source_table in self.exclude_tables
                    or fm.source_table.startswith(self.exclude_src_prefixes)):
                continue

            fd = FieldDiff(
                source_label=fm.source_label,
                target_label=fm.target_label,
                migration_path=fm.migration_path,
                is_transformed=fm.is_transformed,
                status="ok",
            )

            if fm.final_table == "__UNMAPPED__":
                fd.status = "error"
                fd.error  = "No target mapping defined for this source field"
                result.field_diffs.append(fd)
                result.fields_checked += 1
                result.fields_failed  += 1
                continue

            if fm.is_transformed:
                fd.status = "manual_review"
                result.field_diffs.append(fd)
                result.fields_checked       += 1
                result.fields_manual_review += 1
                continue

            try:
                src_pk = self._src_pks(fm.source_table)
                tgt_pk = self._tgt_pks(fm.target_table)

                src_df = _fetch_col(
                    self.src_engine, self.src_schema,
                    fm.source_table, fm.source_field, src_pk,
                    self.src_anchor_table, self.src_anchor_pk, point_id,
                    manual_paths=self.src_manual_paths,
                )
                tgt_df = _fetch_col(
                    self.tgt_engine, self.tgt_schema,
                    fm.target_table, fm.target_field, tgt_pk,
                    self.tgt_anchor_table, self.tgt_anchor_pk, point_id,
                    manual_paths=self.tgt_manual_paths,
                )

                fd.source_row_count = len(src_df)
                fd.target_row_count = len(tgt_df)

                if not src_df.empty or not tgt_df.empty:
                    diff = _diff_field(
                        src_df, tgt_df,
                        fm.source_field, fm.target_field,
                        src_pk, self.max_diffs,
                    )
                    fd.missing_pks = diff["missing_pks"]
                    fd.extra_pks   = diff["extra_pks"]
                    fd.value_diffs = diff["value_diffs"]
                    fd.status = (
                        "value_mismatch"
                        if (fd.missing_pks or fd.extra_pks or fd.value_diffs)
                        else "ok"
                    )

            except Exception as exc:
                fd.status = "error"
                fd.error  = str(exc)
                logging.error(
                    f"  [pointID={point_id}] [{fm.source_label}→{fm.target_label}] "
                    f"ERROR: {exc}"
                )

            result.field_diffs.append(fd)
            result.fields_checked += 1
            if   fd.status == "ok":                 result.fields_ok      += 1
            elif fd.status == "manual_review":      result.fields_manual_review += 1
            elif fd.status == "type_mismatch_warning": result.fields_warned += 1
            else:                                   result.fields_failed  += 1

            if fd.status == "value_mismatch":
                logging.warning(
                    f"  [pointID={point_id}] MISMATCH "
                    f"{fm.source_label}→{fm.target_label} | "
                    f"missing={len(fd.missing_pks)} extra={len(fd.extra_pks)} "
                    f"diffs={len(fd.value_diffs)}"
                )

        return result

    def run(self, point_ids: list[Any], schema_audit: SchemaAuditResult) -> VerificationManifest:
        manifest = VerificationManifest(
            run_at=datetime.utcnow().isoformat() + "Z",
            source_label=self.config.get("source_label", "source"),
            target_label=self.config.get("target_label", "target"),
            mapping_source=self.config.get("_mapping_source", "unknown"),
            total_mapped_fields=len(self.idx.mappings),
            point_ids_checked=len(point_ids),
            point_ids_passed=0,
            point_ids_failed=0,
            schema_audit=_audit_to_dict(schema_audit),
        )
        for i, pid in enumerate(point_ids, 1):
            logging.info(f"[{i}/{len(point_ids)}] Verifying pointID={pid}…")
            pr = self.verify_point(pid)
            manifest.point_id_results.append(pr)
            if pr.passed:
                manifest.point_ids_passed += 1
            else:
                manifest.point_ids_failed += 1
        return manifest


def _audit_to_dict(a: SchemaAuditResult) -> dict:
    return {
        "type_mismatches": [
            {"source": t.source_label, "target": t.target_label,
             "source_type": t.source_type, "target_type": t.target_type}
            for t in a.type_mismatches
        ],
        "missing_in_source_db": [
            {"label": m.label, "mapping": m.mapping.source_label}
            for m in a.missing_in_source_db
        ],
        "missing_in_target_db": [
            {"label": m.label, "mapping": m.mapping.source_label}
            for m in a.missing_in_target_db
        ],
        "unmapped_source_cols": [
            {"table": u.table, "column": u.column, "type": u.type_str}
            for u in a.unmapped_source_cols
        ],
        "unmapped_target_cols": [
            {"table": u.table, "column": u.column, "type": u.type_str}
            for u in a.unmapped_target_cols
        ],
        "warnings": a.audit_warnings,
    }


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

STATUS_STYLE = {
    "ok":                    ("#22c55e", "✓ OK"),
    "manual_review":         ("#a78bfa", "⚠ Manual Review"),
    "type_mismatch_warning": ("#f59e0b", "⚠ Type Warning"),
    "value_mismatch":        ("#ef4444", "✗ Mismatch"),
    "error":                 ("#f97316", "✗ Error"),
}


def _schema_audit_html(audit: dict) -> str:
    sections = []

    def tbl(rows, headers, color):
        if not rows:
            return ""
        ths = "".join(f"<th>{h}</th>" for h in headers)
        trs = "".join(
            "<tr>" + "".join(f"<td>{v}</td>" for v in row) + "</tr>"
            for row in rows
        )
        return (
            f"<table class='audit-table' style='--ac:{color}'>"
            f"<thead><tr>{ths}</tr></thead><tbody>{trs}</tbody></table>"
        )

    items = [
        ("missing_in_source_db", "#ef4444",
         "🚨 Mapped fields missing from SOURCE database",
         lambda r: (r["label"], r["mapping"]),
         ["Target Column", "Source Mapping"]),
        ("missing_in_target_db", "#ef4444",
         "🚨 Mapped fields missing from TARGET database",
         lambda r: (r["label"], r["mapping"]),
         ["Target Column", "Source Mapping"]),
        ("type_mismatches", "#f59e0b",
         "⚠ Type Mismatches",
         lambda r: (r["source"], r["source_type"], r["target"], r["target_type"]),
         ["Source Column", "Source Type", "Target Column", "Target Type"]),
        ("unmapped_source_cols", "#ef4444",
         "🚨 Unmapped SOURCE columns — nothing in mapping sheet",
         lambda r: (r["table"], r["column"], r["type"]),
         ["Table", "Column", "Type"]),
        ("unmapped_target_cols", "#ef4444",
         "🚨 Unmapped TARGET columns — surprise columns",
         lambda r: (r["table"], r["column"], r["type"]),
         ["Table", "Column", "Type"]),
    ]

    for key, color, title, row_fn, headers in items:
        data = audit.get(key, [])
        if data:
            sections.append(
                f"<div class='audit-section'>"
                f"<h3 style='color:{color}'>{title} ({len(data)})</h3>"
                f"{tbl([row_fn(r) for r in data], headers, color)}"
                f"</div>"
            )

    if not sections:
        return "<div class='audit-ok'>✓ Schema audit passed — all fields mapped and present in both DBs</div>"
    return "\n".join(sections)


def write_html_report(manifest: VerificationManifest, path: Path):
    ts     = manifest.run_at
    total  = manifest.point_ids_checked
    passed = manifest.point_ids_passed
    failed = manifest.point_ids_failed
    pct    = f"{(passed/total*100):.1f}" if total else "0"
    oc     = "#22c55e" if failed == 0 else "#ef4444"
    ol     = "ALL CLEAR ✓" if failed == 0 else f"{failed} POINT ID(S) FAILED ✗"

    schema_html = _schema_audit_html(manifest.schema_audit)

    rows_html = ""
    for pr in manifest.point_id_results:
        pc    = "#22c55e" if pr.passed else "#ef4444"
        pi    = "✓" if pr.passed else "✗"
        disp  = "table-row" if not pr.passed else "none"
        ok_pct = f"{(pr.fields_ok/pr.fields_checked*100):.0f}%" if pr.fields_checked else "—"

        frows = ""
        for fd in pr.field_diffs:
            color, label = STATUS_STYLE.get(fd.status, ("#94a3b8", fd.status))
            detail = ""
            if fd.status == "manual_review":
                detail = "<span class='manual-note'>Transformed — manual verification required</span>"
            elif fd.error:
                detail = f"<span class='error-msg'>{fd.error}</span>"
            else:
                if fd.missing_pks:
                    detail += f"<span class='badge badge-red'>{len(fd.missing_pks)} missing in target</span> "
                if fd.extra_pks:
                    detail += f"<span class='badge badge-orange'>{len(fd.extra_pks)} extra in target</span> "
                if fd.value_diffs:
                    detail += "<ul class='diff-list'>"
                    for vd in fd.value_diffs[:8]:
                        pk_str = ", ".join(str(v) for v in vd["pk"])
                        detail += (
                            f"<li>PK [{pk_str}]: "
                            f"<span class='src-val'>{vd['src_val']}</span> → "
                            f"<span class='tgt-val'>{vd['tgt_val']}</span></li>"
                        )
                    if len(fd.value_diffs) > 8:
                        detail += f"<li><em>+{len(fd.value_diffs)-8} more — see JSON log</em></li>"
                    detail += "</ul>"

            pb = (
                "<span class='path-badge path-direct'>direct</span>"
                if fd.migration_path == MIGRATION_PATH_DIRECT
                else "<span class='path-badge path-stage'>stage→refactor</span>"
            )
            frows += (
                f"<tr><td style='color:{color}'>{label}</td>"
                f"<td><code>{fd.source_label}</code></td>"
                f"<td><code>{fd.target_label}</code></td>"
                f"<td>{pb}</td>"
                f"<td class='num'>{fd.source_row_count}</td>"
                f"<td class='num'>{fd.target_row_count}</td>"
                f"<td>{detail}</td></tr>"
            )

        rows_html += f"""
        <tr class='point-row' onclick='toggle("{pr.point_id}")'>
          <td style='color:{pc}' class='si'>{pi}</td>
          <td><b>{pr.point_id}</b></td>
          <td class='num'>{pr.fields_checked}</td>
          <td class='num' style='color:#22c55e'>{pr.fields_ok}</td>
          <td class='num' style='color:#a78bfa'>{pr.fields_manual_review}</td>
          <td class='num' style='color:#ef4444'>{pr.fields_failed}</td>
          <td class='num'>{ok_pct}</td>
        </tr>
        <tr id='d-{pr.point_id}' style='display:{disp}'>
          <td colspan='7'>
            <table class='inner-table'>
              <thead><tr>
                <th>Status</th><th>Source Field</th><th>Target Field</th>
                <th>Path</th><th>Src Rows</th><th>Tgt Rows</th><th>Detail</th>
              </tr></thead>
              <tbody>{frows}</tbody>
            </table>
          </td>
        </tr>"""

    html = f"""<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'>
<title>Migration Verification Report</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:#0f172a;color:#e2e8f0;margin:0;padding:24px}}
h1{{color:#f8fafc;margin-bottom:4px}}h2{{color:#cbd5e1;border-bottom:1px solid #1e293b;padding-bottom:8px}}
h3{{margin:8px 0 6px}}
.meta{{color:#94a3b8;font-size:.9em;margin-bottom:20px}}
.banner{{background:{oc}22;border:2px solid {oc};border-radius:10px;
  padding:14px 22px;margin-bottom:20px;font-size:1.3em;font-weight:700;color:{oc}}}
.stats{{display:flex;gap:14px;margin-bottom:24px;flex-wrap:wrap}}
.card{{background:#1e293b;border-radius:8px;padding:14px 20px;min-width:130px}}
.card .l{{font-size:.72em;color:#64748b;text-transform:uppercase;letter-spacing:.05em}}
.card .v{{font-size:1.9em;font-weight:700;color:#f1f5f9}}
.audit-section{{background:#1e293b;border-radius:8px;padding:16px;margin-bottom:14px}}
.audit-ok{{background:#052e16;border:1px solid #166534;color:#86efac;
  border-radius:8px;padding:12px 16px;margin-bottom:14px}}
.audit-table{{width:100%;border-collapse:collapse;font-size:.85em;margin-top:8px}}
.audit-table th{{background:#0f172a;padding:7px 10px;color:#94a3b8;
  text-align:left;border:1px solid #1e293b}}
.audit-table td{{padding:6px 10px;border:1px solid #1e293b}}
table.ot{{width:100%;border-collapse:collapse;margin-top:8px}}
.ot thead th{{background:#1e293b;padding:10px 12px;text-align:left;
  font-size:.82em;color:#94a3b8;text-transform:uppercase;letter-spacing:.04em}}
.ot tbody tr.point-row{{background:#1e293b;cursor:pointer;transition:background .15s}}
.ot tbody tr.point-row:hover{{background:#273449}}
.ot tbody td{{padding:10px 12px;border-bottom:1px solid #0f172a}}
.inner-table{{width:100%;border-collapse:collapse;font-size:.85em}}
.inner-table thead th{{background:#0f172a;padding:7px 10px;color:#64748b;text-align:left}}
.inner-table tbody td{{padding:7px 10px;border-bottom:1px solid #1e293b;vertical-align:top}}
.si{{font-size:1em;width:28px}}.num{{text-align:right;font-variant-numeric:tabular-nums}}
.badge{{display:inline-block;padding:2px 7px;border-radius:4px;
  font-size:.75em;font-weight:600;margin:2px 2px 0 0}}
.badge-red{{background:#450a0a;color:#fca5a5}}
.badge-orange{{background:#431407;color:#fdba74}}
.diff-list{{margin:4px 0;padding-left:16px;font-size:.83em;color:#cbd5e1}}
.diff-list li{{margin-bottom:2px}}
.src-val{{color:#fca5a5}}.tgt-val{{color:#86efac}}
.error-msg{{color:#f97316;font-size:.83em;font-family:monospace}}
.manual-note{{color:#a78bfa;font-size:.83em;font-style:italic}}
.path-badge{{display:inline-block;padding:2px 7px;border-radius:4px;font-size:.72em;font-weight:600}}
.path-direct{{background:#042f2e;color:#5eead4}}
.path-stage{{background:#1c1917;color:#d6d3d1}}
code{{background:#1e293b;padding:1px 5px;border-radius:3px;font-size:.88em;color:#7dd3fc}}
section{{margin-bottom:32px}}
</style></head><body>
<h1>🔍 Migration Verification Report</h1>
<div class='meta'>
  Run: {ts} &nbsp;|&nbsp; Source: <b>{manifest.source_label}</b>
  &nbsp;→&nbsp; Target: <b>{manifest.target_label}</b>
  &nbsp;|&nbsp; Mapping: <b>{manifest.mapping_source}</b>
  &nbsp;|&nbsp; Mapped fields: <b>{manifest.total_mapped_fields}</b>
</div>
<div class='banner'>{ol}</div>
<div class='stats'>
  <div class='card'><div class='l'>Point IDs</div><div class='v'>{total}</div></div>
  <div class='card'><div class='l'>Passed</div>
    <div class='v' style='color:#22c55e'>{passed}</div></div>
  <div class='card'><div class='l'>Failed</div>
    <div class='v' style='color:#ef4444'>{failed}</div></div>
  <div class='card'><div class='l'>Success Rate</div><div class='v'>{pct}%</div></div>
  <div class='card'><div class='l'>Mapped Fields</div>
    <div class='v'>{manifest.total_mapped_fields}</div></div>
</div>
<section><h2>Schema Audit</h2>{schema_html}</section>
<section><h2>Point ID Results</h2>
<table class='ot'>
  <thead><tr><th></th><th>Point ID</th><th>Fields</th>
    <th>OK</th><th>Manual Review</th><th>Failed</th><th>OK %</th></tr></thead>
  <tbody>{rows_html}</tbody>
</table></section>
<script>
function toggle(pid){{
  const r=document.getElementById('d-'+pid);
  if(r) r.style.display=r.style.display==='none'?'table-row':'none';
}}
</script>
</body></html>"""

    with open(path, "w") as f:
        f.write(html)
    logging.info(f"HTML report → {path}")


def write_json_log(manifest: VerificationManifest, path: Path):
    with open(path, "w") as f:
        json.dump(asdict(manifest), f, indent=2, default=str)
    logging.info(f"Detail JSON log → {path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Mapping-aware SQL→Postgres migration verifier"
    )
    parser.add_argument("--config",         required=True)
    parser.add_argument("--point-ids-file", help="Newline-separated pointID file")
    parser.add_argument("--output-dir",     default="./reports")
    parser.add_argument("--log-level",      default="INFO",
                        choices=["DEBUG","INFO","WARNING","ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    try:
        with open(args.config) as f:
            config = yaml.safe_load(f)
    except Exception as exc:
        logging.error(f"Config load failed: {exc}"); sys.exit(2)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    fh = logging.FileHandler(output_dir / f"run_{ts}.log")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(fh)

    # Load mapping
    sheets_cfg  = config.get("google_sheets", {})
    csv_path    = config.get("mapping_csv")
    transformed = config.get("transformed_fields", [])

    try:
        if sheets_cfg.get("spreadsheet_id"):
            mapping_index = load_mapping_from_sheet(
                service_account_file=sheets_cfg["service_account_file"],
                spreadsheet_id=sheets_cfg["spreadsheet_id"],
                sheet_range=sheets_cfg.get("range", "Sheet1!A:E"),
                transformed_fields=transformed,
            )
            config["_mapping_source"] = "google_sheets"
        elif csv_path:
            mapping_index = load_mapping_from_csv(csv_path, transformed)
            config["_mapping_source"] = f"csv:{csv_path}"
        else:
            raise ValueError("No mapping source. Add 'google_sheets' or 'mapping_csv' to config.")
    except Exception as exc:
        logging.error(f"Mapping load failed: {exc}"); sys.exit(2)

    # Load point IDs
    if args.point_ids_file:
        with open(args.point_ids_file) as f:
            point_ids = [l.strip() for l in f if l.strip()]
    elif "point_ids" in config:
        point_ids = config["point_ids"]
    else:
        logging.error("No point IDs. Use --point-ids-file or 'point_ids' in config.")
        sys.exit(2)

    logging.info(f"Loaded {len(point_ids)} pointID(s).")

    # Schema audit
    try:
        src_engine = _build_source_engine(config)
        tgt_engine = _build_target_engine(config)
        auditor = SchemaAuditor(
            src_engine, tgt_engine,
            config.get("source_schema"), config.get("target_schema"),
            mapping_index,
            check_unmapped_source=config.get("check_unmapped_source", True),
            check_unmapped_target=config.get("check_unmapped_target", True),
        )
        audit_result = auditor.run()
    except Exception as exc:
        logging.exception(f"Schema audit crashed: {exc}"); sys.exit(2)

    # Data verification
    try:
        verifier = MigrationVerifier(config, mapping_index)
        manifest = verifier.run(point_ids, audit_result)
    except Exception as exc:
        logging.exception(f"Verifier crashed: {exc}"); sys.exit(2)

    write_json_log(manifest, output_dir / f"detail_{ts}.json")
    write_html_report(manifest, output_dir / f"summary_{ts}.html")

    schema_errors = (
        audit_result.missing_in_source_db
        or audit_result.missing_in_target_db
        or audit_result.unmapped_source_cols
        or audit_result.unmapped_target_cols
    )

    if manifest.point_ids_failed or schema_errors:
        logging.warning(
            f"\n{'='*60}\nVERIFICATION FAILED\n"
            f"  Schema errors : {'YES' if schema_errors else 'none'}\n"
            f"  Data failures : {manifest.point_ids_failed}/{manifest.point_ids_checked} pointIDs\n"
            f"{'='*60}"
        )
        sys.exit(1)
    else:
        logging.info(
            f"\n{'='*60}\nVERIFICATION PASSED — {manifest.point_ids_checked} pointID(s) clean\n"
            f"{'='*60}"
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
