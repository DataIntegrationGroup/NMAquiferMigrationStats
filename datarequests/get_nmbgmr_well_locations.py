import pytds
import pandas as pd

wells_df = pd.read_csv('/Users/marissafichera/PycharmProjects/NMAquiferMigrationStats/transferable_wells.csv', header=None, names=['PointID'])
point_ids = wells_df['PointID'].tolist()

conn = pytds.connect(
    server='127.0.0.1',
    port=1433,
    database='NM_Aquifer_Dev_DB',
    user='sqlserver',
    password='ilikewaterdata!!',
)

chunks = [point_ids[i:i+1000] for i in range(0, len(point_ids), 1000)]
results = []

for chunk in chunks:
    placeholders = ','.join([f"'{pid}'" for pid in chunk])
    query = f"""
        SELECT PointID, Easting, Northing
        FROM dbo.Location
        WHERE PointID IN ({placeholders})
        AND SiteType = 'GW'
    """
    results.append(pd.read_sql(query, conn))

result_df = pd.concat(results)
result_df.to_csv('output_locations.csv', index=False)
print(result_df)

conn.close()