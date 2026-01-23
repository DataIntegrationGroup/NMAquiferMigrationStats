import pandas as pd
from pathlib import Path

# Input paths
ts_path = Path(r"C:\Users\mfichera-temp\PycharmProjects\NMAquiferMigrationStats\datarequests\timeseries_unified.csv")
mw_path = Path(r"C:\Users\mfichera-temp\PycharmProjects\NMAquiferMigrationStats\datarequests\MimbresWaterData.csv")

# Output path
out_path = ts_path.parent / "timeseries_unified__joined_MimbresWaterData.csv"

# Read CSVs
ts = pd.read_csv(ts_path, dtype={"id": "string"})
mw = pd.read_csv(mw_path, dtype={"id": "string"})

# Normalize join key (trim spaces, standardize missing)
ts["id"] = ts["id"].astype("string").str.strip()
mw["id"] = mw["id"].astype("string").str.strip()

# If there are duplicate non-key column names, suffix them so you keep both
merged = ts.merge(
    mw,
    on="id",
    how="inner",                 # keeps all rows from both files
    suffixes=("_timeseries", "_mimbres"),
    validate="m:m"               # allows duplicates on both sides
)

# Write result
merged.to_csv(out_path, index=False)

print(f"Wrote merged CSV to: {out_path}")
print(f"Rows: {len(merged):,} | Columns: {merged.shape[1]:,}")
