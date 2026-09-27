# Data Sources, Provenance and Handling Policy

This document records where every dataset in this repository came from, who owns
it, and why some of it is deliberately **not** published.

---

## 1. Confidentiality policy (read this first)

The insurance portfolio analysed by this project is **proprietary, confidential
commercial data** belonging to **GAM Assurance**, a Algerian insurance company.
It was supplied to the author for the GAM Assurance hackathon under the
organisers' terms.

| Category | Published? | Rationale |
|---|---|---|
| Row-level policy data (one row per insurance policy) | **No** | Contains live policy numbers, insured capitals, premiums and coverage dates. Publishing would breach the confidentiality terms agreed with the company. |
| Commune-level aggregates (policy counts, total/mean insured capital, dominant risk class) | **Yes** | Non-identifying, and these are the actual analytical results. |
| RPA99 seismic zoning table | **Yes** | Derived from a published government annex, contains no GAM data. |

### What "row-level" means here

A row-level file has roughly one record per insurance policy and carries
`NUMERO_POLICE`. Any such file is excluded by `.gitignore` and must never be
committed. The excluded files are:

```
CATNAT_2023_2025.xlsx              # organiser-supplied raw portfolio
CATNAT_2023_2025_with_zones.xlsx   # same portfolio, pre-joined to seismic zones
CATNAT_2023_2025(1).xlsx           # cleaned variant actually consumed by the model
Algeria_Seismic_Risk_Classified.xlsx  # model output, still one row per policy
```

### Why aggregation is considered safe to publish

The published outputs are aggregated to the **commune** level (1,541 communes
across 48 wilayas, Algeria's smallest administrative division). A commune
typically holds thousands of policies held by unrelated third parties, so a
commune total of insured capital cannot be traced back to any individual
policy or client. No names, addresses, telephone numbers or email addresses
appear anywhere in the source data — exposure is commercial confidentiality and
policy-number re-identification, not personal data.

### If you want to run the pipeline

Obtain your own authorised copy of the portfolio and pass it in explicitly:

```bash
python RisquePredictionCatBoost/RisquePredictionCatBoost/RisquePredictionCatBoost.py \
    --portfolio /secure/path/CATNAT_2023_2025.xlsx
```

or set the environment variable:

```bash
export GAM_PORTFOLIO_FILE=/secure/path/CATNAT_2023_2025.xlsx
```

Never commit that file. `.gitignore` already blocks the standard filenames, but
a non-standard name would slip through — check `git status` before pushing.

---

## 2. Datasets used

### 2.1 GAM Assurance CatNat portfolio — **NOT PUBLISHED**

* **Owner:** GAM Assurance (confidential)
* **Format:** Excel workbook, one worksheet per year (`2023`, `2024`, `2025`)
* **Volume:** 123,434 policy records
* **Grain:** one row per insurance policy
* **Schema:** see [DATA_DICTIONARY.md](DATA_DICTIONARY.md)

Two variants exist and they are **not** interchangeable:

| File | `COMMUNE` / `WILAYA` format | `TYPE` format | Used by |
|---|---|---|---|
| `CATNAT_2023_2025.xlsx` (raw) | `"111 - BIRTOUTA"`, `"9 - BLIDA"` — code and name concatenated | `"1 - Installation Industrielle"` — numbered prefix | earlier exploration |
| `CATNAT_2023_2025(1).xlsx` (cleaned) | `"Birtouta"`, `"BLIDA"` — name only, separate `COMMUNE CODE` / `WILAYA CODE` | `"Installation Industrielle"` — no prefix | **the current model script** |

> **This matters.** `label_risque()` matches `TYPE` against the unprefixed
> strings `"Installation Industrielle"` and `"Installation Commerciale"`. Fed
> the raw variant, the industrial and commercial fragility factors would never
> fire and every policy would silently fall back to `b = 1`. Always use the
> cleaned workbook.

### 2.2 RPA99 seismic zoning table — **published**

* **File:** `RisquePredictionCatBoost/RisquePredictionCatBoost/zones_rpa99_seismic.xlsx`
* **Owner:** public domain (derived from a state-published annex)
* **Grain:** one row per commune — 1,541 rows, 48 wilayas
* **Build method:** transcribed by hand from **Annexe 1** of the RPA99
  classification document (see §2.4), cross-referenced with national geographic
  codes. The hackathon brief describes this step as *"tedious but critical"* and
  it was performed manually.
* **Columns:** `COMMUNE`, `commune_code`, `wilaya_name`, `wilaya_code`, `ZONE SEISMIQUE`

Zone distribution as built:

| Zone | Communes | Share | Meaning |
|---|---:|---:|---|
| `IIa` | 866 | 56.2% | moderate-to-high |
| `I` | 345 | 22.4% | low |
| `III` | 161 | 10.4% | **highest hazard** |
| `0` | 131 | 8.5% | negligible (deep Sahara) |
| `IIb` | 38 | 2.5% | moderate-to-high |
| **Total** | **1,541** | 100% | |

Derived CSV copies of this table are also kept at the repository root
(`zones_rpa99_final.csv`, `zones_rpa99_with_source.csv`) as provenance for
the QGIS join, and a copy sits in `QGis_files/` alongside the project that
loads it. The `*_with_source` variant carries an extra `source` column
recording where each zone assignment came from. Earlier iterations of the
join table were discarded because they disagreed with this one.

### 2.3 Administrative boundaries — **published, licence caveat**

* **Directory:** `QGis_files/dza_admin_boundaries.shp/`
* **Format:** ESRI Shapefile, WGS84 (`GCS_WGS_1984`, `.cpg` = UTF-8)
* **Provenance:** GADM-derived (GADM v2, 48 wilayas — the pre-2019
  administrative division that matches the RPA99 annex)

| Layer | Geometry | Records | Role in the QGIS project |
|---|---|---:|---|
| `dza_admin0` | polygon | 1 | country outline |
| `dza_admin1` | polygon | 48 | wilaya boundaries |
| `dza_admin2` | polygon | 1,541 | **commune polygons — the risk map canvas** |
| `dza_adminlines` | polyline | 4,762 | internal boundary network |
| `dza_adminpoints` | point | 1,589 | administrative label points |

The commune layer count (1,541) matches the RPA99 zoning table exactly, which
is what makes the attribute join clean.

> **Licence caveat.** GADM is free for academic and non-commercial use but is
> not published under an open licence. Redistribution inside a public repository
> is a grey area. The attribution is recorded here and in the QGIS project; if
> you reuse this repository, replace the shapefiles with an openly licensed
> equivalent (Natural Earth, or the official Algerian administrative limits) and
> re-point the layers.

### 2.4 Reference documents — `docs/`

| File | Source | Notes |
|---|---|---|
| `GAM_Hackathon_Brief_and_Student_Guide.pdf` | GAM Assurance organisers | The official brief. Defines deliverables A–D and the scoring criteria. English. |
| `GAM_Seismic_Risk_and_Portfolio_Report.pdf` | Author | Final report / presentation (Canva export). **The main write-up of this project.** |
| `RPA99_Seismic_Classification_by_Wilaya.pdf` | RPA99 annex extract | The source table transcribed in §2.2. |
| `RPA99_Confined_Masonry_Design.pdf` | Algerian Ministry of Equipment, 2008 (DTR) | The full RPA99 technical regulation. Not authored here. |

The two RPA99 documents are published by the **Algerian Ministry of Equipment**
and are therefore **not** covered by this repository's MIT licence.

---

## 3. Derived outputs

| File | Grain | Rows | Published |
|---|---|---:|---|
| `Algeria_Seismic_Summary_for_QGIS.xlsx` | commune × wilaya | 1,538 | yes |
| `Algeria_Seismic_Risk_By_Year.xlsx` | commune × wilaya × year | 3,119 | yes |
| `QGis_files/Algeria_Seismic_Summary_for_QGIS_Mapped.csv` | commune | 1,010 | yes |
| `QGis_files/commune_level_aggregated_corrected_dbf_fixed.csv` | commune | 1,422 | yes |
| `Algeria_Seismic_Risk_Classified.xlsx` | **policy** | 123,434 | **no — row level** |

### A note on stale artefacts

`QGis_files/Algeria_Seismic_Summary_for_QGIS_Mapped.csv` was produced by the
**first iteration** of the pipeline and still carries the old four-column schema
(`COMMUNE, TOTAL_CAPITAL, DOMINANT_RISK, POLICY_COUNT`). The current script emits
a five-column schema that adds `WILAYA` and renames the capital column to
`AVG_CAPITAL`. The QGIS project is joined to the older file, so the map and the
current model output are **not** perfectly in sync. Regenerating the mapped CSV
from the current summary and re-pointing the layer is a listed improvement — see
the README's *Future Improvements*.

Separately, the committed `Algeria_Seismic_Risk_Classified.xlsx` was produced by
an earlier run: it differs from a fresh run of the current script on 3
`Risque_Color` rows and 14 `Final_Classification` rows. The current script was
verified to reproduce its own output bit-for-bit.
