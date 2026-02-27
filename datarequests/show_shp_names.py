# inspect_shapefile_pycharm.py

import geopandas as gpd
import pandas as pd

# --- Edit these ---
SHP_PATH = r"tl_2018_nm_county_geojson/tl_2018_nm_county.geojson"
N_ROWS = 50
# -----------------

def main():
    gdf = gpd.read_file(SHP_PATH)

    # Attribute headers (columns)
    cols = list(gdf.columns)
    print("\n=== Attribute headers (columns) ===")
    for c in cols:
        print(c)

    # Prefer "name-like" columns (anything containing 'name', case-insensitive)
    name_cols = [c for c in cols if "name" in c.lower()]

    print(f"\n=== First {N_ROWS} rows of name-like columns ===")
    if name_cols:
        # Print just the name-like columns
        print(gdf[name_cols].head(N_ROWS).to_string(index=False))
    else:
        # Fallback: print first rows of all non-geometry attributes
        print("(No columns containing 'name' found. Showing first rows of all attributes instead.)")
        attrs = gdf.drop(columns=["geometry"], errors="ignore")
        print(attrs.head(N_ROWS).to_string(index=False))

if __name__ == "__main__":
    # Makes pandas print wider tables nicely in PyCharm's console
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_colwidth", None)

    main()