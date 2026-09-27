"""
SUPERSEDED - first iteration of the GAM Assurance risk classifier.
==============================================================
Kept for provenance: this is the earliest working version of the CatBoost
pipeline and shows how the approach evolved.

Use instead:
    RisquePredictionCatBoost/RisquePredictionCatBoost/RisquePredictionCatBoost.py

Differences from the current version
------------------------------------
* Joins the RPA99 zoning table on COMMUNE only. Commune names are not unique
  across wilayas, so the current version joins on (COMMUNE, WILAYA) and adds a
  COMMUNE-only fallback.
* `np.log10(capital)` is called unguarded, so a zero insured capital produces
  -inf. The current version guards it with `if capital > 0 else 0`.
* No WILAYA feature, no Source_Year feature, no per-year or averaged
  aggregation - it emits a single commune-level QGIS table.
* The model is created without `random_seed`, so runs are not reproducible.
* Imports `pickle`, which is never used.

The label heuristic (`label_risque`) is identical in both versions.
"""
import pandas as pd
from catboost import CatBoostClassifier
import numpy as np

excel_file = pd.ExcelFile('CATNAT_2023_2025(1).xlsx')
all_sheets = []

for sheet_name in excel_file.sheet_names:
    df_sheet = pd.read_excel(excel_file, sheet_name=sheet_name)
    df_sheet.columns = df_sheet.columns.str.strip()
    df_sheet['Source_Year'] = sheet_name
    
    all_sheets.append(df_sheet)


df = pd.concat(all_sheets, ignore_index=True)
print(f"Combined {len(excel_file.sheet_names)} years of data. Total rows: {len(df)}")

df_zones = pd.read_excel('zones_rpa99_seismic.xlsx')
print("Real column names in Zone file:", df_zones.columns.tolist())

df_zones.columns = df_zones.columns.str.strip()
df.columns = df.columns.str.strip()

df_combined = pd.merge(
    df, 
    df_zones[['COMMUNE', 'ZONE SEISMIQUE']], 
    on='COMMUNE', 
    how='left'
)

df_combined['ZONE SEISMIQUE'] = df_combined['ZONE SEISMIQUE'].fillna('Unknown')

df_combined['CAPITAL_ASSURE'] = df_combined['CAPITAL_ASSURE'].astype(str)

df_combined['CAPITAL_ASSURE'] = (
    df_combined['CAPITAL_ASSURE']
    .str.replace(' ', '', regex=False)
    .str.replace(',', '.', regex=False)
)

df_combined['CAPITAL_ASSURE'] = pd.to_numeric(df_combined['CAPITAL_ASSURE'], errors='coerce')

df_combined['CAPITAL_ASSURE'] = df_combined['CAPITAL_ASSURE'].fillna(0)

avg_capital = df_combined['CAPITAL_ASSURE'].mean()
print(f"Average Capital calculated: {avg_capital:,.2f} DZD")

print("Seismic zones successfully linked to the portfolio!")

df_combined.columns = df_combined.columns.str.strip() # Remove accidental spaces in headers
avg_capital = df_combined['CAPITAL_ASSURE'].mean()

def label_risque(row):
    capital = row['CAPITAL_ASSURE']
    zone = row['ZONE SEISMIQUE']
    build_type = row['TYPE']

    if zone == 'III' : z = 10
    elif zone == 'IIb' : z = 7
    elif zone == 'IIa' : z = 5
    elif zone == 'I' : z = 2
    else : z = 0

    cap_log = np.log10(capital)
    
    if cap_log >= 8.5: c = 10 
    elif cap_log >= 7.5: c = 7
    elif cap_log >= 6.5: c = 5
    elif cap_log >= 5.5: c = 3 
    else: c = 0
        
    if build_type == " Installation Industrielle": b = 4
    elif build_type == " Installation Commerciale" : b = 2.5
    else : b = 1

    score = b * (z + c)

    if  score >= 11 :
        return 'Red'
    elif score >= 6 :
        return 'Orange'
    else :
        return 'Green'

df_combined['Risque_Color'] = df_combined.apply(label_risque, axis=1)

features = ['COMMUNE', 'ZONE SEISMIQUE', 'TYPE',  'CAPITAL_ASSURE']
target = 'Risque_Color'
categorical_features = ['COMMUNE', 'ZONE SEISMIQUE', 'TYPE']

model = CatBoostClassifier(
    iterations=500,
    loss_function='MultiClass', # We use this for 3+ colors
    verbose=100
)

df_combined['TYPE'] = df_combined['TYPE'].astype(str).str.strip()
df_combined['COMMUNE'] = df_combined['COMMUNE'].fillna('Unknown').astype(str)
df_combined['ZONE SEISMIQUE'] = df_combined['ZONE SEISMIQUE'].fillna('Unknown').astype(str)
df_combined['TYPE'] = df_combined['TYPE'].fillna('Unknown').astype(str)

df_combined['CAPITAL_ASSURE'] = pd.to_numeric(df_combined['CAPITAL_ASSURE'], errors='coerce').fillna(0)

df_combined['Risque_Color'] = df_combined['Risque_Color'].fillna('Green')

print("All categorical features sanitized. Ready for training!")


print("--- DATA DIAGNOSTICS ---")
print("Unique Colors found:", df_combined['Risque_Color'].unique())
print("Counts per color:\n", df_combined['Risque_Color'].value_counts())
print("\nFirst 10 Building Types in data:", df_combined['TYPE'].unique()[:10])


model.fit(df_combined[features], df_combined[target], cat_features=categorical_features)

df_combined['Final_Classification'] = model.predict(df_combined[features]).flatten()
df_combined.to_excel('Algeria_Seismic_Risk_Classified.xlsx', index=False)

# Create a summary for QGIS mapping
# This counts policies and averages the risk scores for each commune
df_map = df_combined.groupby('COMMUNE').agg({
    'CAPITAL_ASSURE': 'sum',           # Total capital in the commune
    'Final_Classification': lambda x: x.mode()[0], # The most frequent risk color
    'Risque_Color': 'count'            # Number of policies in that commune
}).reset_index()

# Rename columns for clarity in QGIS
df_map.columns = ['COMMUNE', 'TOTAL_CAPITAL', 'DOMINANT_RISK', 'POLICY_COUNT']

# Export this specific file for QGIS
df_map.to_excel('Algeria_Seismic_Summary_for_QGIS.xlsx', index=False)
print("QGIS summary file created! Use this one to fill your map.")

print("Classification complete! Look for 'Final_Classification' in your new file.")