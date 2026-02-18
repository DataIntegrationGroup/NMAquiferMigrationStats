from __future__ import print_function
import os
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

# --- CONFIGURATION ---
SPREADSHEET_ID = "1iQzeKqRWHIKbnNptH_wRQEpJ_pt1rI00ax9d5BhDAhU"
SOURCE_SHEET = "Copy of AMP_review"  # where we copy FROM
TARGET_SHEET = "AMP_review"          # where we copy TO

# Columns used to match rows
MATCH_COLS = ["NMAquifer_Table.Field", "PointID", "Error"]

# Columns to copy from source to target
COPY_COLS = ["Reviewed (yes/no)", "Fixed (yes/no)", "AMP_Reviewer", "Notes"]

# Path to your service account JSON key (adjust as needed)
SERVICE_ACCOUNT_FILE = "transfermetrics_service_account.json"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def col_index_to_a1(col_index_0_based):
    """
    Convert a 0-based column index (0 -> A, 1 -> B, ...) to A1-style column letters.
    """
    col = col_index_0_based + 1  # make it 1-based
    result = ""
    while col > 0:
        col, remainder = divmod(col - 1, 26)
        result = chr(65 + remainder) + result
    return result


def get_service():
    """
    Build and return the Sheets API service using a service account.
    """
    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    service = build("sheets", "v4", credentials=creds)
    return service


def fetch_sheet_values(service, sheet_name):
    """
    Fetch all values from a sheet (A1:ZZZ range) and return as list of lists.
    """
    range_name = f"'{sheet_name}'!A1:ZZZ"
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SPREADSHEET_ID, range=range_name)
        .execute()
    )
    values = result.get("values", [])
    return values


def build_header_index_map(header_row):
    """
    Given a header row (list of column names), return a dict: name -> index.
    """
    return {name: idx for idx, name in enumerate(header_row)}


def main():
    service = get_service()

    # --- Fetch both sheets ---
    source_values = fetch_sheet_values(service, SOURCE_SHEET)
    target_values = fetch_sheet_values(service, TARGET_SHEET)

    if not source_values:
        print(f"No data found in {SOURCE_SHEET}.")
        return
    if not target_values:
        print(f"No data found in {TARGET_SHEET}.")
        return

    # First row assumed to be headers
    source_header = source_values[0]
    target_header = target_values[0]

    source_header_map = build_header_index_map(source_header)
    target_header_map = build_header_index_map(target_header)

    # Ensure all required columns exist
    for col in MATCH_COLS + COPY_COLS:
        if col not in source_header_map:
            raise ValueError(f"Column '{col}' not found in source sheet header.")
    for col in MATCH_COLS + COPY_COLS:
        if col not in target_header_map:
            raise ValueError(f"Column '{col}' not found in target sheet header.")

    # --- Build lookup from SOURCE SHEET keyed by (NMAquifer_Table.Field, PointID, Error) ---
    source_lookup = {}
    for row in source_values[1:]:  # skip header
        row_extended = row + [""] * (len(source_header) - len(row))
        key = tuple(row_extended[source_header_map[col]] for col in MATCH_COLS)

        copy_data = {col: row_extended[source_header_map[col]] for col in COPY_COLS}
        # If duplicate keys exist, the last one wins; you could log this if it matters
        source_lookup[key] = copy_data

    print(f"Built source lookup with {len(source_lookup)} unique keys.")

    # --- Prepare batch update for TARGET SHEET ---
    data_updates = []
    matched_count = 0
    updated_rows_count = 0
    updated_keys = []

    # Precompute min/max col indices for the span we’ll write per row
    copy_col_indices = [target_header_map[c] for c in COPY_COLS]
    min_copy_idx = min(copy_col_indices)
    max_copy_idx = max(copy_col_indices)

    # Row index in A1 is 1-based; target_values[0] is header, so row 2 is first data row
    for i, row in enumerate(target_values[1:], start=2):
        row_extended = row + [""] * (len(target_header) - len(row))
        key = tuple(row_extended[target_header_map[col]] for col in MATCH_COLS)

        if key in source_lookup:
            matched_count += 1
            src_vals = source_lookup[key]

            # Capture old & new values for the COPY_COLS
            old_vals = {}
            new_vals = {}
            changed = False

            for col_name in COPY_COLS:
                col_idx = target_header_map[col_name]
                current_val = row_extended[col_idx]
                new_val = src_vals[col_name]

                old_vals[col_name] = current_val
                new_vals[col_name] = new_val

                if current_val != new_val:
                    changed = True
                    row_extended[col_idx] = new_val  # update in memory

            if changed:
                updated_rows_count += 1
                updated_keys.append(key)

                # Build the range for the span of columns from min_copy_idx to max_copy_idx
                start_col_letter = col_index_to_a1(min_copy_idx)
                end_col_letter = col_index_to_a1(max_copy_idx)
                cell_range = f"'{TARGET_SHEET}'!{start_col_letter}{i}:{end_col_letter}{i}"

                # Values for that entire span (we preserve intermediate columns)
                span_values = row_extended[min_copy_idx : max_copy_idx + 1]

                data_updates.append(
                    {
                        "range": cell_range,
                        "values": [span_values],
                    }
                )

                # Log details for this row
                print(f"\nUpdating row {i} (key={key}):")
                print("  Old values:")
                for c in COPY_COLS:
                    print(f"    {c}: {old_vals[c]!r}")
                print("  New values:")
                for c in COPY_COLS:
                    print(f"    {c}: {new_vals[c]!r}")

    print(f"\nTotal matches found: {matched_count}")
    print(f"Rows with changes to apply: {updated_rows_count}")

    # --- Execute batch update ---
    if not data_updates:
        print("No matching rows with changes. Nothing to update.")
        return

    body = {
        "valueInputOption": "USER_ENTERED",
        "data": data_updates,
    }

    result = (
        service.spreadsheets()
        .values()
        .batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body=body,
        )
        .execute()
    )

    total_cells = sum(len(r["values"]) * len(r["values"][0]) for r in data_updates)
    print(f"\nUpdate complete. {total_cells} cells updated across {updated_rows_count} rows.")


if __name__ == "__main__":
    main()
