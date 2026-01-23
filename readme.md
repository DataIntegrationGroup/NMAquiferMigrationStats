````md
# NMAquiferMigrationStats — `update*` Scripts

This README documents **only** the repository scripts whose filenames start with **`update`**:

- `update_migration_status.py`
- `update_nonnull_counts.py`
- `update_transfer_metrics_summary.py`
- `update_data_for_amp_review.py`
- `update_amp_review_data.py`

These scripts are “ops / reporting” utilities that sync **Google Sheets** (and, for some tasks, an **Ocotillo Postgres** database + local log/CSV files).

---

## Prerequisites

### Python
Python 3 recommended.

### Install dependencies
At minimum, these scripts rely on:

- `google-api-python-client`
- `google-auth`
- `pg8000` *(only for scripts that query Postgres)*

```bash
pip install google-api-python-client google-auth pg8000
````

### Google Sheets credentials (service account)

Several scripts expect a service account JSON file in the repo root:

* `service_account.json` *(migration status + non-null counts + transfer summary)*
* `transfermetrics_service_account.json` *(AMP review scripts)*

Make sure:

1. The **Google Sheets API** is enabled for the GCP project.
2. The target spreadsheets are **shared with the service account email** (Editor access).

### Database credentials (only for scripts that query Postgres)

Some scripts connect to Postgres using:

* host: `127.0.0.1`
* port: `5432`
* database: `ocotillo-staging`
* user: `marissa.fichera@nmt.edu`
* password: from env var `DB_PASS`

```bash
export DB_PASS="your_password_here"
```

---

## Scripts

## `update_migration_status.py`

**Purpose:** Recompute migration tracking columns in the `MIGRATION_STATUS` Google Sheet from scratch.

### Inputs (must exist as columns on the sheet)

* `Migration Path`
* `NMAquifer_TableField` *(old `Table.Field`, e.g. `Equipment.DateInstalled`)*
* `Final Schema Target` *(final `table.field`, e.g. `transducer_observation.value`)*

  * Accepts aliases like `New Schema Target`, `New Target Schema`, `Final Target Schema`

### Outputs (created if missing)

* `Temp Schema Target`
* `Final Mapping Status`
* `Final Target Status`
* `Temp Mapping Status`
* `Temp Target Status`
* `Transfer Status`

### What it does (high level)

* Derives **temp schema targets** using the staging naming convention (e.g. `nma_<table>.<field>`), with fallback/normalization logic.
* Checks whether targets exist in the Ocotillo DB schema.
* Computes status values using exact strings like:

  * `defined`, `undefined`, `exists`, `missing`
  * `staging transfer complete`, `final transfer complete`, `incomplete`
  * `not being migrated`

### Configuration to edit

Near the top of the script:

* `SERVICE_ACCOUNT_FILE`
* `SPREADSHEET_ID`
* `SHEET_NAME`
* DB connection settings (`DB_HOST`, `DB_PORT`, etc.)
* `UPDATED_CELL` *(writes a “last updated” timestamp into a single cell)*

### Run

```bash
python update_migration_status.py
```

---

## `update_nonnull_counts.py`

**Purpose:** For rows where `Migration Path == "stage then refactor"`, compute non-null counts for old vs temp fields and write the results back to the `MIGRATION_STATUS` sheet.

### Inputs

* Google Sheet columns:

  * `Migration Path`
  * `NMAquifer_TableField`
  * `Temp Schema Target` ✅ *(this is why you typically run `update_migration_status.py` first)*

* Local CSV file:

  * `nma_aquifer_nonnull_counts.csv` *(default name in the script)*

### Outputs (created if missing)

* `NMA NonNull Count`
* `Temp NonNull Count`
* `NonNull Diff` *(old − temp)*

### Special behavior

* It **does not** rewrite other status logic.
* It will **only** update `Transfer Status` to `staging transfer complete` when:

  * migration path is `stage then refactor`, **and**
  * `NonNull Diff == 0`
* Otherwise, it preserves the existing `Transfer Status` value.

### Run

```bash
export DB_PASS="..."
python update_nonnull_counts.py
```

---

## `update_transfer_metrics_summary.py`

**Purpose:** Parse “transfer metrics” log output (stored as a `.csv` text file with pipe-delimited blocks) and write **summary rows** to a Google Sheet tab.

### Input

* A log file path configured in:

  * `TRANSFER_METRICS_PATH` *(example points into `transfer_metrics_logs/...`)*

The parser expects blocks like:

* First block may contain a header line:

  * `model|input_count|cleaned_count|transferred|issue_percentage`
* Blocks then include rows under:

  * `PointID|Table|Field|Error`

### Output

Writes a summary table with headers:

* `model | Table | input_count | cleaned_count | transferred | issue_percentage`

…and writes it to the configured sheet/tab (defaults: spreadsheet `1Ntka...` and sheet name `transfer_metrics`).

### Run

```bash
python update_transfer_metrics_summary.py
```

---

## `update_data_for_amp_review.py`

**Purpose:** Append **new** `[NMAquifer_Table.Field, PointID, Error]` rows into the `AMP_review` sheet **without overwriting** existing content.

### Input

* Reads the transfer metrics log file (configured by `TRANSFER_METRICS_PATH`)
* Extracts rows from blocks under:

  * `PointID|Table|Field|Error`

### Output

* Appends to `AMP_review` columns **A:C** using the Sheets **append** API.
* Skips duplicates already present in the sheet.

### Error normalization

Cleans error text to reduce noise, including:

* removing `row.id=...`
* normalizing certain `sensor_type` and `organization` errors into shorter forms
* removing `value error` prefixes
* stripping quotes and trailing separators

### Run

```bash
python update_data_for_amp_review.py
```

---

## `update_amp_review_data.py`

**Purpose:** Copy review metadata from `Copy of AMP_review` → `AMP_review` in the AMP spreadsheet, matching rows by key columns.

### Match key (must match exactly)

* `NMAquifer_Table.Field`
* `PointID`
* `Error`

### Copied columns

* `Reviewed (yes/no)`
* `Fixed (yes/no)`
* `AMP_Reviewer`
* `Notes`

This is useful when you review/edit in a working copy sheet and then want to sync those annotations back to the main `AMP_review` tab.

### Run

```bash
python update_amp_review_data.py
```

---

## Suggested workflow

A common order that matches dependencies:

1. **Update migration tracking + temp targets**

   ```bash
   export DB_PASS="..."
   python update_migration_status.py
   ```

2. **(Stage/refactor only) Update non-null comparisons**

   ```bash
   python update_nonnull_counts.py
   ```

3. **Parse transfer metrics into summary tables**

   ```bash
   python update_transfer_metrics_summary.py
   ```

4. **Feed new transfer errors into AMP review**

   ```bash
   python update_data_for_amp_review.py
   ```

5. **After reviewing in the copy sheet, sync review columns back**

   ```bash
   python update_amp_review_data.py
   ```

---

## Troubleshooting

* **Sheets permission errors:** Share the spreadsheet with the service account email from the JSON credentials file.
* **DB connection/auth errors:** confirm `DB_PASS` is set, and that `ocotillo-staging` is reachable at `127.0.0.1:5432`.
* **Missing columns in sheets:** these scripts expect exact column headers. If your sheet differs, either rename columns or update the script’s header lookups.

```
::contentReference[oaicite:0]{index=0}
```
