# Data Dictionary

Column definitions for every dataset in the pipeline. Where a field's business
meaning is not documented by the data provider, that is stated explicitly rather
than guessed.

---

## 1. Input — GAM CatNat portfolio (confidential, not published)

One worksheet per year: `2023`, `2024`, `2025`. 123,434 records total.

| # | Column | Dtype (as read) | Description |
|---|---|---|---|
| 1 | `NUMERO_POLICE` | string | Insurance policy number. Unique business key per policy. **This column makes a file row-level and therefore confidential.** |
| 2 | `CODE_SOUS_BRANCHE` | int | Internal GAM sub-branch code. Its meaning is not documented by the organisers. Observed values and their relationship to `TYPE`: |
| | | | `3301` → residential (`Bien immobilier`) only |
| | | | `3302` → commercial + industrial |
| | | | `93301` → residential only |
| | | | `93302` → commercial + industrial |
| | | | The `933xx` values appear to be a secondary/alternative classification applied to a minority of records. Not used as a model feature. |
| 3 | `NUM_AVNT_COURS` | int | Prior-claims counter on the policy. `0` for ~96% of 2023 records; observed range 0–6. Not used as a model feature. |
| 4 | `DATE_EFFET` | string | Policy start date. Two formats coexist in the source: `DD/MM/YYYY` (2023 sheet) and ISO `YYYY-MM-DD` (2024–2025 sheets). Not parsed by the pipeline. |
| 5 | `DATE_EXPIRATION` | string | Policy expiry date, same mixed formatting as `DATE_EFFET`. Not parsed by the pipeline. |
| 6 | `CAPITAL_ASSURE` | string | **Sum insured, in Algerian dinars (DZD).** Stored as text using French decimal comma (`"2500,000"`). The pipeline normalises it — see §4. |
| 7 | `PRIME_NETTE` | string | Net premium in DZD, same text formatting as `CAPITAL_ASSURE`. Not a model feature. |
| 8 | `COMMUNE CODE` | int | Official commune geographic code (e.g. `111` = Birtouta). |
| 9 | `COMMUNE CAPS` | string | Commune name, upper case, no code prefix. Differs from column 10 on 18 of 39,196 records in 2023. |
| 10 | `COMMUNE` | string | Commune name, title case (e.g. `Birtouta`). **Join key.** In the raw variant this column instead holds `"111 - BIRTOUTA"`. |
| 11 | `WILAYA CODE` | int | Official wilaya (province) code (e.g. `9` = Blida). |
| 12 | `WILAYA` | string | Wilaya name (e.g. `BLIDA`). **Join key.** |
| 13 | `TYPE` | string | Construction / usage type. One of `Bien immobilier`, `Installation Commerciale`, `Installation Industrielle`. **In the raw variant these carry a numeric prefix** (`"1 - Installation Industrielle"`). |
| 14 | `Source_Year` | string | *Added by the pipeline* from the worksheet name. Model feature. |

### Type breakdown (2023, 39,196 records)

| `TYPE` | Records | Fragility factor `b` |
|---|---:|---:|
| `Bien immobilier` | 29,100 | 1.0 |
| `Installation Commerciale` | 9,675 | 2.5 |
| `Installation Industrielle` | 421 | 4.0 |

### Capital distribution (2023)

| Statistic | DZD |
|---|---:|
| min | 93,000 |
| 25th pct | 1,540,700 |
| median | 2,484,030 |
| mean | 7,642,358 |
| 75th pct | 3,900,000 |
| max | 8,389,997,969 |

The mean sits far above the median and the maximum is four orders of magnitude
larger — the distribution is heavily right-skewed by a few very large industrial
risks. This is exactly why the risk heuristic buckets capital on a `log10`
scale rather than using the raw amount.

---

## 2. Reference — RPA99 seismic zoning (published)

`RisquePredictionCatBoost/RisquePredictionCatBoost/zones_rpa99_seismic.xlsx`,
worksheet `zones_rpa99_seismic`. 1,541 rows.

| Column | Dtype | Description |
|---|---|---|
| `COMMUNE` | string | Commune name, title case. Join key. |
| `commune_code` | int | Official commune geographic code. |
| `wilaya_name` | string | Wilaya name, title case. Join key. |
| `wilaya_code` | int | Official wilaya code. |
| `ZONE SEISMIQUE` | string | **Seismic hazard zone.** One of `0`, `I`, `IIa`, `IIb`, `III`. `III` is the most hazardous; `0` covers the deep Sahara. |

### Hazard weights used by the model

| Zone | Weight `z` | Interpretation |
|---|---:|---|
| `III` | 10 | highest hazard (e.g. Blida, Boumerdès — the 2003 epicentral region) |
| `IIb` | 7 | moderate-to-high |
| `IIa` | 5 | moderate-to-high |
| `I` | 2 | low |
| `0` | 0 | negligible |
| `Unknown` (unmatched) | 0 | treated as no credit |

---

## 3. Outputs

### 3.1 `Algeria_Seismic_Risk_Classified.xlsx` — **confidential, not published**

Policy-level, 123,434 rows. Input columns plus:

| Column | Description |
|---|---|
| `COMMUNE_CAPS` | duplicate of the input `COMMUNE CAPS`, upper-cased by the join |
| `ZONE SEISMIQUE` | joined hazard zone, or `Unknown` |
| `Risque_Color` | **ground-truth label** produced by the heuristic in `label_risque()` — `Red` / `Orange` / `Green` |
| `Final_Classification` | **model prediction** — what CatBoost learned to output |

### 3.2 `Algeria_Seismic_Risk_By_Year.xlsx` — published

Commune × wilaya × year, 3,119 rows.

| Column | Description |
|---|---|
| `COMMUNE`, `WILAYA` | location |
| `YEAR` | source year (`2023` / `2024` / `2025`) |
| `TOTAL_CAPITAL` | sum of `CAPITAL_ASSURE` in DZD |
| `DOMINANT_RISK` | modal `Final_Classification` for the group |
| `POLICY_COUNT` | number of policies in the group |

### 3.3 `Algeria_Seismic_Summary_for_QGIS.xlsx` — published

Commune × wilaya, 1,538 rows. This is the table the QGIS project joins against
the commune polygons.

| Column | Description |
|---|---|
| `COMMUNE`, `WILAYA` | location |
| `AVG_CAPITAL` | **mean** insured capital per policy in the commune, DZD |
| `DOMINANT_RISK` | modal `Final_Classification` over the full three-year window |
| `YEARS_COUNT` | number of distinct years present (1–3) — a data-completeness flag |

### 3.4 `QGis_files/commune_level_aggregated_corrected_dbf_fixed.csv` — published

1,422 rows, the most detailed commune-level aggregate produced during the
project.

| Column | Description |
|---|---|
| `WILAYA_CODE`, `COMMUNE_CODE` | official codes — **the robust join key for the shapefile** |
| `wilaya_name`, `commune_name` | names |
| `policy_count`, `total_capital`, `avg_capital` | volume and exposure |
| `total_prime`, `avg_prime` | premium volume and average |
| `Bien immobilier`, `Installation Commerciale`, `Installation Industrielle` | policy counts split by construction type |
| `pct_total_capital` | the commune's share of portfolio capital |
| `seismic_zone` | RPA99 zone |
| `baseline_risk` | `LOW` / `MODERATE` / `VERY_HIGH` |
| `zone_priority` | ordinal 1–5 ranking of the zone's hazard |

---

## 4. Normalisation rules applied by the pipeline

| Step | Rule | Reason |
|---|---|---|
| Column names | `.str.strip()` | the source export carries accidental leading/trailing spaces |
| `COMMUNE`, `WILAYA` | `.str.strip().str.upper()` | the GAM export and the RPA99 annex disagree on case, accents and abbreviation (`"B.B ARRERIDJ"` vs `Bordj Bou Arreridj`) |
| `TYPE` | `.astype(str).str.strip()` | the cleaned workbook has leading spaces (`" Installation Industrielle"`) |
| `CAPITAL_ASSURE` | strip spaces → `,` to `.` → `to_numeric` → `fillna(0)` | the value is text with a French decimal comma; unparseable values become `0` rather than `NaN` so `log10` stays defined |
| `ZONE SEISMIQUE` | `fillna('Unknown')` | CatBoost requires non-null categorical values |

> **Known gap.** The two place-name sets are reconciled only by upper-casing.
> Wilaya abbreviations (`B.B ARRERIDJ`), accented names and the
> `NUMERO_POLICE`-embedded codes are **not** normalised, which is the direct
> cause of the 9,554 unmatched records documented in
> [METHODOLOGY.md](METHODOLOGY.md) §5.
