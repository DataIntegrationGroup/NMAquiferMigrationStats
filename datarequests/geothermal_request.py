"""
Extract NMBGMR water data for SW New Mexico counties from SQL Server database.

Counties: Luna, Hidalgo, Doña Ana, Sierra, Grant, Socorro, Catron
Shapefile: datarequests/tl_2018_nm_county_geojson/tl_2018_nm_county.geojson

Outputs:
  - water_data_sw_nm.csv         : Main merged dataset (Location + WellData + Chemistry + Field Params)
  - chemistry_major_wide.csv     : MajorChemistry pivoted to wide format (one row per SamplePtID)
  - chemistry_minor_wide.csv     : MinorAndTraceChemistry pivoted to wide format
  - lookup_tables.csv            : All lookup tables concatenated with a 'LookupTable' column
"""

import os
import json
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point
import pytds

# ---------------------------------------------------------------------------
# CONFIG — update to match your Cloud SQL Server instance
# ---------------------------------------------------------------------------
DB_HOST     = "127.0.0.1"
DB_PORT     = 1433
DB_DATABASE = "NM_Aquifer_Dev_DB"
DB_USER     = "sqlserver"
DB_PASSWORD = "ilikewaterdata!!"

GEOJSON_PATH = "tl_2018_nm_county_geojson/tl_2018_nm_county.geojson"

TARGET_COUNTIES = [
    "Luna", "Hidalgo", "Doña Ana", "Sierra", "Grant", "Socorro", "Catron"
]

OUTPUT_DIR = "datarequests/outputs"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# STEP 1: Build county geometry for spatial filtering
# ---------------------------------------------------------------------------
print("Loading county shapefile...")
counties_gdf = gpd.read_file(GEOJSON_PATH)

# Filter to target counties (handle encoding variants for Doña Ana)
mask = counties_gdf["NAME"].isin(TARGET_COUNTIES) | \
       counties_gdf["NAME"].str.normalize("NFC").isin(
           [c.encode("utf-8").decode("utf-8") for c in TARGET_COUNTIES]
       )
sw_counties = counties_gdf[mask].copy()

if sw_counties.empty:
    raise ValueError(
        f"No matching counties found. Available: {counties_gdf['NAME'].unique().tolist()}"
    )

# Dissolve into a single polygon for intersection test
sw_union = sw_counties.geometry.union_all()  # shapely >= 2.0; use .unary_union if older
print(f"Loaded {len(sw_counties)} county polygons, dissolved into study area.")

# ---------------------------------------------------------------------------
# STEP 2: Connect and pull Location table
# ---------------------------------------------------------------------------
print("Connecting to database...")
conn = pytds.connect(
    server=DB_HOST,
    port=DB_PORT,
    database=DB_DATABASE,
    user=DB_USER,
    password=DB_PASSWORD,
)

print("Querying Location table...")
location_query = """
SELECT
    LocationId,
    PointID,
    SiteType,
    Easting,
    Northing,
    Altitude,
    CoordinateNotes,
    UTMZone
FROM Location
WHERE SiteType IN ('GW', 'SP')
  AND Easting IS NOT NULL
  AND Northing IS NOT NULL
"""
cursor = conn.cursor()
cursor.execute(location_query)
_cols = [desc[0] for desc in cursor.description]
location_df = pd.DataFrame(cursor.fetchall(), columns=_cols)
print(f"  {len(location_df)} rows from Location")

# ---------------------------------------------------------------------------
# STEP 3: Spatial filter — handle mixed UTM zones (12N and 13N)
# ---------------------------------------------------------------------------
print("Applying spatial filter...")

# UTM zone 12N = EPSG:26912, zone 13N = EPSG:26913 (both NAD83)
# We reproject everything to WGS84 (EPSG:4326) as a common CRS,
# then test intersection against the county boundary.
UTM_ZONE_EPSG = {12: "EPSG:26912", 13: "EPSG:26913"}
TARGET_CRS = "EPSG:4326"

# Reproject the county union to WGS84 once
sw_union_wgs84 = (
    gpd.GeoSeries([sw_union], crs=counties_gdf.crs)
    .to_crs(TARGET_CRS)
    .iloc[0]
)

filtered_parts = []
unknown_zones = location_df[~location_df["UTMZone"].isin([12, 13, "12", "13"])]
if not unknown_zones.empty:
    print(f"  WARNING: {len(unknown_zones)} rows have unrecognized UTMZone values "
          f"({unknown_zones['UTMZone'].unique()}) — skipping those rows.")

for zone, epsg in UTM_ZONE_EPSG.items():
    subset = location_df[location_df["UTMZone"].astype(str) == str(zone)].copy()
    if subset.empty:
        print(f"  No records found for UTM Zone {zone}, skipping.")
        continue

    gdf = gpd.GeoDataFrame(
        subset,
        geometry=gpd.points_from_xy(subset["Easting"], subset["Northing"]),
        crs=epsg,
    ).to_crs(TARGET_CRS)

    in_area = gdf.geometry.within(sw_union_wgs84)
    matched = gdf[in_area].drop(columns="geometry").copy()
    print(f"  UTM Zone {zone} ({epsg}): {len(subset)} total, {len(matched)} within study area")
    filtered_parts.append(matched)

location_df = pd.concat(filtered_parts, ignore_index=True) if filtered_parts else pd.DataFrame()
location_ids = location_df["LocationId"].tolist()

print(f"  {len(location_df)} total locations within study area")

if not location_ids:
    raise ValueError(
        "No locations found within the study area. "
        "Check that UTMZone values are 12 or 13 and that Easting/Northing are in meters."
    )

# Helper: chunk a list for SQL IN clauses (avoids parameter limit issues)
def chunked_query(base_query, id_list, conn, chunk_size=1000):
    frames = []
    for i in range(0, len(id_list), chunk_size):
        chunk = id_list[i : i + chunk_size]
        placeholders = ",".join(["%s" for _ in chunk])
        q = base_query.format(placeholders=placeholders)
        cursor = conn.cursor()
        cursor.execute(q, chunk)
        cols = [desc[0] for desc in cursor.description]
        frames.append(pd.DataFrame(cursor.fetchall(), columns=cols))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

# ---------------------------------------------------------------------------
# STEP 4: WellData
# ---------------------------------------------------------------------------
print("Querying WellData table...")
well_query = """
SELECT LocationId, WellDepth, FormationZone, CurrentUse
FROM WellData
WHERE LocationId IN ({placeholders})
"""
well_df = chunked_query(well_query, location_ids, conn)
print(f"  {len(well_df)} rows from WellData")

# ---------------------------------------------------------------------------
# STEP 5: Chemistry SampleInfo
# ---------------------------------------------------------------------------
print("Querying [Chemistry SampleInfo] table...")
sampleinfo_query = """
SELECT *
FROM [Chemistry SampleInfo]
WHERE LocationId IN ({placeholders})
"""
sampleinfo_df = chunked_query(sampleinfo_query, location_ids, conn)
print(f"  {len(sampleinfo_df)} rows from Chemistry SampleInfo")

sample_ids = sampleinfo_df["SamplePtID"].dropna().unique().tolist()

# ---------------------------------------------------------------------------
# STEP 6: MajorChemistry — pull then pivot wide
# ---------------------------------------------------------------------------
print("Querying MajorChemistry table...")
major_query = """
SELECT *
FROM MajorChemistry
WHERE SamplePtID IN ({placeholders})
"""
major_df = chunked_query(major_query, sample_ids, conn)
print(f"  {len(major_df)} rows from MajorChemistry (long format)")

print("  Pivoting MajorChemistry to wide format...")
# Identify non-data columns (everything except Analyte/SampleValue/Units)
major_id_cols = [c for c in major_df.columns if c not in ("Analyte", "SampleValue", "Units")]

# Create unique column names: Analyte__Units
def pivot_chemistry(df, id_cols):
    """Pivot a long chemistry table to wide, one row per SamplePtID."""
    df = df.copy()
    # Build column label: AnalyteName (Units)
    df["col_label"] = df["Analyte"].astype(str).str.strip()
    unit_present = "Units" in df.columns
    if unit_present:
        df["col_label"] = df["col_label"] + " (" + df["Units"].fillna("").astype(str).str.strip() + ")"

    # Keep metadata columns (first occurrence per SamplePtID)
    meta = df.drop_duplicates(subset=["SamplePtID"])[id_cols].set_index("SamplePtID")

    # Pivot values
    pivot = df.pivot_table(
        index="SamplePtID",
        columns="col_label",
        values="SampleValue",
        aggfunc="first",   # if truly one value per analyte per sample; adjust if dupes
    )
    pivot.columns.name = None
    return meta.join(pivot).reset_index()

major_wide = pivot_chemistry(major_df, major_id_cols)
print(f"  MajorChemistry wide: {major_wide.shape[0]} rows × {major_wide.shape[1]} columns")

# ---------------------------------------------------------------------------
# STEP 7: MinorAndTraceChemistry — pull then pivot wide
# ---------------------------------------------------------------------------
print("Querying MinorAndTraceChemistry table...")
minor_query = """
SELECT *
FROM MinorAndTraceChemistry
WHERE SamplePtID IN ({placeholders})
"""
minor_df = chunked_query(minor_query, sample_ids, conn)
print(f"  {len(minor_df)} rows from MinorAndTraceChemistry (long format)")

print("  Pivoting MinorAndTraceChemistry to wide format...")
minor_id_cols = [c for c in minor_df.columns if c not in ("Analyte", "SampleValue", "Units")]
minor_wide = pivot_chemistry(minor_df, minor_id_cols)
print(f"  MinorAndTraceChemistry wide: {minor_wide.shape[0]} rows × {minor_wide.shape[1]} columns")

# ---------------------------------------------------------------------------
# STEP 8: FieldParameters
# ---------------------------------------------------------------------------
print("Querying FieldParameters table...")
field_query = """
SELECT *
FROM FieldParameters
WHERE SamplePtID IN ({placeholders})
"""
field_df = chunked_query(field_query, sample_ids, conn)
print(f"  {len(field_df)} rows from FieldParameters")

# ---------------------------------------------------------------------------
# Helper: drop junk columns wherever they appear
# ---------------------------------------------------------------------------
DROP_COLS = {"GlobalID", "SSMA_TimeStamp"}

def drop_junk(df):
    return df.drop(columns=[c for c in df.columns if c in DROP_COLS], errors="ignore")

# ---------------------------------------------------------------------------
# STEP 9: Merge everything into one output table
# ---------------------------------------------------------------------------
print("Merging tables...")

# Start with Location + WellData
main_df = location_df.merge(well_df, on="LocationId", how="left", suffixes=("", "_wd"))

# + Chemistry SampleInfo (one row per sample event per location)
main_df = main_df.merge(sampleinfo_df, on="LocationId", how="left", suffixes=("", "_si"))

# + MajorChemistry wide (on SamplePtID)
if "SamplePtID" in main_df.columns and not major_wide.empty:
    # Drop the duplicate SamplePtID metadata cols from the pivot (keep originals from sampleinfo)
    major_merge = major_wide.drop(
        columns=[c for c in major_wide.columns if c in main_df.columns and c != "SamplePtID"],
        errors="ignore"
    )
    main_df = main_df.merge(major_merge, on="SamplePtID", how="left", suffixes=("", "_maj"))

# + MinorAndTraceChemistry wide (on SamplePtID)
if "SamplePtID" in main_df.columns and not minor_wide.empty:
    minor_merge = minor_wide.drop(
        columns=[c for c in minor_wide.columns if c in main_df.columns and c != "SamplePtID"],
        errors="ignore"
    )
    main_df = main_df.merge(minor_merge, on="SamplePtID", how="left", suffixes=("", "_min"))

# + FieldParameters (on SamplePtID)
if "SamplePtID" in main_df.columns and not field_df.empty:
    field_merge = field_df.drop(
        columns=[c for c in field_df.columns if c in main_df.columns and c != "SamplePtID"],
        errors="ignore"
    )
    main_df = main_df.merge(field_merge, on="SamplePtID", how="left", suffixes=("", "_fp"))

# Drop GlobalID and SSMA_TimeStamp everywhere
main_df = drop_junk(main_df)

print(f"  Main dataset: {main_df.shape[0]} rows × {main_df.shape[1]} columns")

# ---------------------------------------------------------------------------
# STEP 10: Lookup tables
# ---------------------------------------------------------------------------
LOOKUP_TABLES = [
    "LU_FormationZone",
    "LU_CollectionMethod",
    "LU_SampleType",
    "LU_AllAnalytes",
    "LU_AllAnalytesUnits",
    "LU_MinorTraceAnalyte",
    "LU_MinorTraceUnits",
    "LU_FieldParameter",
]

print("Querying lookup tables...")
lookup_frames = {}
for tbl in LOOKUP_TABLES:
    try:
        df = pd.read_sql(f"SELECT * FROM [{tbl}]", conn)
        df = drop_junk(df)
        lookup_frames[tbl] = df
        print(f"  {tbl}: {len(df)} rows")
    except Exception as e:
        print(f"  WARNING: Could not load {tbl}: {e}")

conn.close()
print("Database connection closed.")

# ---------------------------------------------------------------------------
# STEP 11: Write outputs
# ---------------------------------------------------------------------------
print("Writing output files...")

# -- Main combined table
main_df = main_df.drop(columns=["LocationId"], errors="ignore")
main_path = os.path.join(OUTPUT_DIR, "water_data_sw_nm.csv")
main_df.to_csv(main_path, index=False)
print(f"  Main dataset -> {main_path}")

# -- Lookup tables: each block preceded by its table name as a header row
lookup_path = os.path.join(OUTPUT_DIR, "lookup_tables.csv")
with open(lookup_path, "w", newline="", encoding="utf-8") as f:
    for i, (tbl_name, df) in enumerate(lookup_frames.items()):
        if i > 0:
            f.write("\n")  # blank line between tables
        f.write(f"{tbl_name}\n")
        df.to_csv(f, index=False)
print(f"  Lookup tables -> {lookup_path}")

print("\nDone! All files written to:", OUTPUT_DIR)