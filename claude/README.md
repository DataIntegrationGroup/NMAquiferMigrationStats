# Migration Verifier v2

A rigorous, mapping-aware SQL→PostgreSQL migration verification tool.  
Handles non-1:1 table/field renames, two-phase migrations (direct vs. stage-then-refactor),
transformed fields, and full schema auditing against your Google Sheets mapping doc.

---

## Architecture

```
verify_migration.py     ← main entry point, orchestrator, HTML+JSON reports
mapping_loader.py       ← pulls your Google Sheet, parses the 4-column mapping
schema_auditor.py       ← checks what's mapped vs. what actually exists in both DBs
config.yaml             ← all configuration (DSNs, anchor table, Sheet ID, etc.)
```

---

## What it checks

### 1. Schema audit (runs first, before any data)
| Check | Severity |
|---|---|
| Mapped field doesn't exist in source DB | **Error** |
| Mapped field doesn't exist in target DB | **Error** |
| Source column exists in DB but has no mapping row | **Error** |
| Target column exists in DB but has no mapping row | **Error** |
| Mapped field exists in both DBs but types are incompatible | **Warning** |

### 2. Data verification (per pointID, per mapped field)
| Check | Severity |
|---|---|
| Source rows present in source but missing from target | **Fail** |
| Rows in target with no corresponding source row | **Fail** |
| Row exists in both but column value differs | **Fail** |
| Field flagged as `transformed_fields` in config | **Manual Review** (no value compare) |

### 3. Migration path awareness
- `direct-to-final` → verifies against **Final Schema Target** column  
- `stage then refactor` → verifies against **Temp Schema Target** column  
  *(final refactor not yet done, so temp is the source of truth)*

---

## Setup

```bash
pip install -r requirements.txt
```

### SQL Server ODBC driver (if source is MSSQL)
- **macOS**: `brew install msodbcsql17`
- **Ubuntu/Debian**: [Microsoft install guide](https://docs.microsoft.com/sql/connect/odbc/linux-mac/installing-the-microsoft-odbc-driver-for-sql-server)

### Google Sheets service account
1. Go to [Google Cloud Console](https://console.cloud.google.com) → IAM & Admin → Service Accounts
2. Create a service account → download the JSON key → save as `service_account.json`
3. **Share your mapping Google Sheet** with the service account email address (Viewer is enough)
4. Set `google_sheets.service_account_file` and `google_sheets.spreadsheet_id` in `config.yaml`

The Spreadsheet ID is in your sheet URL:  
`https://docs.google.com/spreadsheets/d/`**`<THIS PART>`**`/edit`

---

## Configuration

Key fields in `config.yaml`:

| Key | Description |
|---|---|
| `source_dsn` / `target_dsn` | SQLAlchemy connection strings |
| `anchor_table` / `anchor_pk` | Table + column that holds `pointID` |
| `google_sheets.spreadsheet_id` | Live mapping sheet (always latest) |
| `transformed_fields` | `old_table.old_field` labels to skip value comparison |
| `check_unmapped_source/target` | Whether unknown columns are errors |
| `exclude_tables` | Tables to skip entirely |
| `max_diff_rows` | Cap on diff rows captured per field per pointID |

---

## Usage

```bash
# Basic run
python verify_migration.py --config ~~config.yaml

# Pass point IDs from a file (one per line)
python verify_migration.py --config ~~config.yaml --point-ids-file ids.txt

# Custom output directory
python verify_migration.py --config ~~config.yaml --output-dir ./audit/2024-01-15

# Verbose logging
python verify_migration.py --config ~~config.yaml --log-level DEBUG
```

---

## Outputs

```
reports/
├── run_20240115T143022Z.log        # Full timestamped console log
├── detail_20240115T143022Z.json    # Machine-readable full diff (all fields, all pointIDs)
└── summary_20240115T143022Z.html   # Shareable HTML report
```

**The HTML report:**
- Overall pass/fail banner
- Schema audit section (errors and warnings at the top, before data results)
- Per-pointID table: fields checked, ok, manual review, failed
- Failed pointIDs **auto-expand** showing each field and exact value diffs
- Migration path badges (direct vs. stage→refactor) per field
- Manual review fields clearly flagged in purple

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | All pointIDs clean, schema audit passed |
| `1` | Discrepancies found (schema errors or data mismatches) |
| `2` | Script error (bad config, DB unreachable, etc.) |

CI/CD integration:
```bash
python verify_migration.py --config ~~config.yaml \
  || ./notify_team.sh "Migration verification failed — check reports/"
```

---

## Adding transform rules later

Currently, transformed fields are flagged as "Manual Review Required" with no value comparison.  
When you're ready to define explicit transform rules (e.g. `CONCAT(first, ' ', last) → full_name`),  
the `mapping_loader.py` `FieldMapping` dataclass and `verify_point()` in `verify_migration.py`  
are the two places to extend — each `FieldMapping` can carry a callable `transform_fn`.
