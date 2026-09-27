"""
GAM Assurance - Seismic Risk & Portfolio Optimisation
=====================================================
Deliverable B of the GAM Assurance hackathon: AI-based seismic risk
classification of the insured portfolio, at policy level.

WHAT THIS SCRIPT DOES
---------------------
1. Loads the GAM CatNat insurance portfolio (one Excel workbook, one sheet
   per year: 2023, 2024, 2025) and concatenates it into a single dataframe.
2. Joins it to the RPA99 seismic zoning table (1,541 communes / 48 wilayas)
   on normalised (COMMUNE_KEY, WILAYA_KEY) pairs, keeping the raw GAM
   columns and reporting any row that stays unresolved.
3. Derives a heuristic risk label per policy with `label_risque()`, an
   expert score combining seismic zone, insured capital and building type.
4. Trains a CatBoost multiclass classifier that learns to reproduce those
   labels, and writes the model prediction back as `Final_Classification`.
5. Aggregates the result to commune level (per-year and averaged) into Excel
   tables that are consumed by the QGIS project in QGis_files/.
6. Writes the join table the QGIS project actually reads, keyed on the
   boundary layer's own adm2_name spelling.

DATA CLASSIFICATION: the two input files are confidential. The raw policy
portfolio was supplied by GAM Assurance for the hackathon and is NOT
distributed with this repository. Only commune-level aggregates are
published. See docs/DATA_SOURCES.md.

USAGE
-----
    pip install -r requirements.txt
    python RisquePredictionCatBoost.py

    # if the portfolio is stored outside the repository:
    python RisquePredictionCatBoost.py --portfolio /path/to/CATNAT.xlsx

    # to write the output tables somewhere else:
    python RisquePredictionCatBoost.py --outdir /path/to/outputs

The RPA99 zoning table (zones_rpa99_seismic.xlsx) ships with the repository
because it is derived from a published government annex, not from GAM data.
"""

import argparse
import os
import re
import unicodedata
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import shapefile
from catboost import CatBoostClassifier

warnings.filterwarnings("ignore")

# Resolve paths relative to this file so the script can be run from anywhere.
SCRIPT_DIR = Path(__file__).resolve().parent

# Confidential: GAM's live policy portfolio. Expected sheet-per-year layout.
# The file is not committed (see .gitignore), so the exact filename depends on
# what the user drops in. Prefer an explicitly named file, otherwise take the
# first match of the CATNAT_2023_2025*.xlsx glob that is not a pre-joined
# intermediate.
CANONICAL_PORTFOLIO = SCRIPT_DIR / "CATNAT_2023_2025_clean.xlsx"
PORTFOLIO_GLOB = "CATNAT_2023_2025*.xlsx"


def resolve_default_portfolio() -> Path:
    """Pick the portfolio workbook to use when --portfolio is not given."""
    if CANONICAL_PORTFOLIO.is_file():
        return CANONICAL_PORTFOLIO
    candidates = sorted(
        path
        for path in SCRIPT_DIR.glob(PORTFOLIO_GLOB)
        if path.is_file() and "with_zones" not in path.stem
    )
    return candidates[0] if candidates else CANONICAL_PORTFOLIO

# Public: commune -> RPA99 seismic zone, transcribed from RPA99 Annexe 1.
DEFAULT_ZONES = SCRIPT_DIR / "zones_rpa99_seismic.xlsx"

# Default destination for the generated tables.
DEFAULT_OUTDIR = SCRIPT_DIR / "outputs"

# GADM admin-2 boundaries that the QGIS choropleth draws. Only the attribute
# table is needed: the QGIS project joins on `adm2_name`, so the export has to
# reproduce that field's exact spelling rather than invent its own.
DEFAULT_BOUNDARIES = (
    SCRIPT_DIR.parents[1] / "QGis_files" / "dza_admin_boundaries.shp" / "dza_admin2.shp"
)

# The QGIS project joins this file on COMMUNE = adm2_name and renders a
# categorised symbol on DOMINANT_RISK, so the column set is fixed by the
# project file and must not drift.
QGIS_CSV_COLUMNS = ["COMMUNE", "TOTAL_CAPITAL", "DOMINANT_RISK", "POLICY_COUNT"]

# `CAPITAL_ASSURE` is stored as text in the source export: French/Algerian
# number formatting uses a comma as the decimal separator ("2500,000") and
# thousands separators are also spaces. Normalise both before casting.
def parse_capital(series: pd.Series) -> pd.Series:
    """Normalise the CAPITAL_ASSURE column to a float64 DZD amount."""
    cleaned = (
        series.astype(str)
        .str.strip()
        .str.replace(" ", "", regex=False)   # thousands separator
        .str.replace(",", ".", regex=False)   # decimal separator
    )
    return pd.to_numeric(cleaned, errors="coerce").fillna(0)


# ---------------------------------------------------------------------------
# Place-name normalisation for the RPA99 seismic-zone join
# ---------------------------------------------------------------------------
# The GAM export and the RPA99 annex spell the same places differently:
#   GAM   "Chéraga"  "CHERAGA ALGER"  "AIN BENIAN (ALGER)"  "AIT R_ZINE"
#   RPA99 "Cheraga"  "CHERAGA"        "CHERAGA"              "AIT R'ZINE"
# so the raw strings never join. The GAM COMMUNE field is worse than a
# case/accent difference: it is a *composite* "COMMUNE WILAYA" string, and
# the wilaya component is also the value of the separate WILAYA column, which
# makes COMMUNE+WILAYA redundant and unmatchable as written.
#
# These helpers build explicit join keys. They never modify the raw columns,
# so every normalisation decision stays auditable from the output.

_PARENTHETICAL = re.compile(r"\([^)]*\)")
_WHITESPACE = re.compile(r"\s+")
# Apostrophes and underscores are *deleted*, not replaced with a space: the two
# sources disagree on which separator they use for the same place
# (RPA99 "AIT R'ZINE" vs GAM "AIT R_ZINE"), so both must collapse to
# "AITRZINE" for the keys to meet.
_JOIN_NOISE = ("'", "\u2019", "\u02bc", "_")


def normalize_place(value: object) -> str:
    """
    Reduce a commune or wilaya name to a stable comparison key.

    Applied in this order: null coercion, strip, drop a bracketed qualifier,
    NFKD accent folding, removal of apostrophe/underscore noise, dot-to-space,
    whitespace collapsing, upper-casing.

    >>> normalize_place(" Chéraga ")
    'CHERAGA'
    >>> normalize_place("AIN BENIAN (ALGER)")
    'AIN BENIAN'
    >>> normalize_place("AIT R'ZINE") == normalize_place("AIT R_ZINE")
    True
    >>> normalize_place(None)
    ''
    """
    text = "" if pd.isna(value) else str(value)
    text = _PARENTHETICAL.sub(" ", text.strip())
    # NFKD splits "é" into "e" + a combining accent; drop the combining marks
    # so the base letter survives. Non-Latin scripts are left intact.
    text = "".join(
        ch for ch in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(ch)
    )
    for noise in _JOIN_NOISE:
        text = text.replace(noise, "")
    text = text.replace(".", " ")
    return _WHITESPACE.sub(" ", text).strip().upper()


def _strip_wilaya_suffix(commune_key: str, wilaya_key: str) -> str:
    """
    Drop a trailing wilaya name from a composite commune key.

    "CHERAGA ALGER" + wilaya "ALGER" -> "CHERAGA". Only applied when the
    commune key is strictly longer, so a commune that is genuinely named
    after its wilaya is left alone.
    """
    if (
        wilaya_key
        and len(commune_key) > len(wilaya_key)
        and commune_key.endswith(wilaya_key)
    ):
        return commune_key[: -len(wilaya_key)].strip()
    return commune_key


def build_join_keys(
    commune: pd.Series, wilaya: pd.Series
) -> tuple[pd.Series, pd.Series]:
    """
    Build the (COMMUNE_KEY, WILAYA_KEY) pair used to join the portfolio to the
    RPA99 zoning table. Both series are indexed like the inputs.
    """
    commune_key = commune.map(normalize_place)
    wilaya_key = wilaya.map(normalize_place)
    commune_key = pd.Series(
        [
            _strip_wilaya_suffix(c, w)
            for c, w in zip(commune_key, wilaya_key)
        ],
        index=commune.index,
        dtype="object",
    )
    return commune_key, wilaya_key


# ---------------------------------------------------------------------------
# QGIS export: map GAM communes onto the boundary layer's own names
# ---------------------------------------------------------------------------
# The QGIS project does not join on anything the pipeline produces. It joins
# the admin-2 boundary layer's `adm2_name` against the CSV's `COMMUNE`, and
# `adm2_name` is Title-Case, sometimes accented, and for the 49 communes whose
# name is not unique nationally it carries a qualifier ("Ain Turk (Oran)").
# Matching GAM's UPPERCASE composite "CHERAGA ALGER" against that is
# hopeless, so the export resolves each portfolio commune to the boundary
# polygon it belongs to and then writes that polygon's own `adm2_name`
# verbatim. The join then matches by construction.
#
# `adm2_ref_n` carries the bare commune stem, which is what lets both
# "Ain Turk" and "Ain Turk (Oran)" be looked up from the single key
# "AIN TURK"; `adm1_name` is the wilaya used to tell them apart.


def load_boundary_index(shapefile_path: Path) -> pd.DataFrame:
    """
    Read the commune names and wilayas of the admin-2 boundary layer.

    Returns a frame with the exact `adm2_name` string QGIS joins on, plus the
    normalised stem and wilaya used to resolve a GAM commune to one polygon.
    """
    reader = shapefile.Reader(str(shapefile_path))
    fields = [field[0] for field in reader.fields[1:]]
    frame = pd.DataFrame([rec for rec in reader.records()], columns=fields)

    stem = frame["adm2_ref_n"]
    if stem.isna().any():
        stem = stem.fillna(frame["adm2_name"])

    return pd.DataFrame({
        "adm2_name": frame["adm2_name"].astype(str),
        "COMMUNE_KEY": stem.map(normalize_place),
        "WILAYA_KEY": frame["adm1_name"].map(normalize_place),
    })


def resolve_boundary_communes(
    commune_key: pd.Series, wilaya_key: pd.Series, boundary: pd.DataFrame
) -> tuple[pd.Series, pd.Series]:
    """
    Map each (COMMUNE_KEY, WILAYA_KEY) to a single boundary `adm2_name`.

    Resolution is deliberately two-stage and never guesses:
      1. the commune stem identifies exactly one polygon -> take it;
      2. the stem is shared by several polygons (40 stems, e.g. Cheraga
         exists in Alger and in Mila) -> keep only the polygon whose wilaya
         matches, and take it only if that leaves exactly one;
      3. otherwise return no name, so the row is reported instead of being
         assigned to an arbitrary polygon.

    Returns the resolved names (None where unresolved) and a status series.
    """
    by_stem = {
        key: group for key, group in boundary.groupby("COMMUNE_KEY", sort=False)
    }

    names, statuses = [], []
    for commune, wilaya in zip(commune_key, wilaya_key):
        candidates = by_stem.get(commune)
        if candidates is None:
            names.append(None)
            statuses.append("unmatched")
        elif len(candidates) == 1:
            names.append(candidates["adm2_name"].iloc[0])
            statuses.append("matched_commune")
        else:
            same_wilaya = candidates[candidates["WILAYA_KEY"] == wilaya]
            if len(same_wilaya) == 1:
                names.append(same_wilaya["adm2_name"].iloc[0])
                statuses.append("matched_commune_wilaya")
            elif same_wilaya.empty:
                names.append(None)
                statuses.append("unresolved_wilaya")
            else:
                names.append(None)
                statuses.append("ambiguous_boundary")
    index = commune_key.index
    return (
        pd.Series(names, index=index, dtype="object"),
        pd.Series(statuses, index=index, dtype="object"),
    )


def write_qgis_csv(frame: pd.DataFrame, path: Path) -> None:
    """
    Write the QGIS join table in the exact dialect the project expects.

    The datasource is declared as `decimalPoint=,` with a comma delimiter, so
    a decimal separator in the data would be ambiguous. Amounts are therefore
    written as whole DZD, which is also what the previous file did. UTF-8 with
    BOM and CRLF match the file the project already reads.
    """
    out = pd.DataFrame({
        "COMMUNE": frame["adm2_name"].astype(str),
        "TOTAL_CAPITAL": frame["TOTAL_CAPITAL"].round().astype("int64"),
        "DOMINANT_RISK": frame["DOMINANT_RISK"].astype(str),
        "POLICY_COUNT": frame["POLICY_COUNT"].astype("int64"),
    })[QGIS_CSV_COLUMNS].sort_values("COMMUNE", kind="stable")
    out.to_csv(path, index=False, encoding="utf-8-sig", lineterminator="\r\n")


def label_risque(row) -> str:
    """
    Assign a heuristic seismic risk label (Red / Orange / Green) to one policy.

    The score is the product of a building-type factor and the sum of a
    seismic-zone weight and an insured-capital weight:

        score = b * (z + c)

        b - fragility factor of the construction type
            Installation Industrielle  -> 4.0
            Installation Commerciale   -> 2.5
            anything else (residential)-> 1.0

        z - seismic zone weight (RPA99)
            III -> 10 | IIb -> 7 | IIa -> 5 | I -> 2 | 0/Unknown -> 0

        c - insured capital weight, bucketed on log10(DZD) so that the
            exposure axis is not dominated by a handful of very large
            industrial risks
            >= 1e8.5 -> 10 | >= 1e7.5 -> 7 | >= 1e6.5 -> 5
            >= 1e5.5 -> 3  | otherwise -> 0

    Decision thresholds on `score`:
        >= 11 -> 'Red'    (high risk, priority for inspection)
        >=  6 -> 'Orange' (moderate risk)
        else  -> 'Green'  (low risk)

    Note: these weights and thresholds are an expert heuristic defined for
    this project, not a calibrated actuarial model. See docs/METHODOLOGY.md
    for the rationale and the known limitations.
    """
    capital = row["CAPITAL_ASSURE"]
    zone = row["ZONE SEISMIQUE"]
    build_type = str(row["TYPE"]).strip() if pd.notna(row["TYPE"]) else ""

    # z - seismic zone weight
    if zone == "III":
        z = 10
    elif zone == "IIb":
        z = 7
    elif zone == "IIa":
        z = 5
    elif zone == "I":
        z = 2
    else:  # zone 0 (Sahara) and rows that failed to match the zoning table
        z = 0

    # c - insured capital weight on a log10 scale. Guard against log10(0).
    cap_log = np.log10(capital) if capital > 0 else 0

    if cap_log >= 8.5:
        c = 10
    elif cap_log >= 7.5:
        c = 7
    elif cap_log >= 6.5:
        c = 5
    elif cap_log >= 5.5:
        c = 3
    else:
        c = 0

    # b - construction type fragility factor
    if build_type == "Installation Industrielle":
        b = 4
    elif build_type == "Installation Commerciale":
        b = 2.5
    else:
        b = 1

    score = b * (z + c)

    if score >= 11:
        return "Red"
    elif score >= 6:
        return "Orange"
    else:
        return "Green"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Classify the GAM Assurance insurance portfolio into seismic "
            "risk levels (Red / Orange / Green) using CatBoost."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--portfolio",
        type=Path,
        default=Path(os.environ.get("GAM_PORTFOLIO_FILE") or resolve_default_portfolio()),
        help="Path to the confidential CATNAT portfolio workbook "
             "(one sheet per year). Can also be set with $GAM_PORTFOLIO_FILE.",
    )
    parser.add_argument(
        "--zones",
        type=Path,
        default=Path(os.environ.get("GAM_ZONES_FILE", DEFAULT_ZONES)),
        help="Path to the RPA99 commune -> seismic zone table.",
    )
    parser.add_argument(
        "--outdir",
        type=Path,
        default=DEFAULT_OUTDIR,
        help="Directory that will receive the generated Excel tables.",
    )
    parser.add_argument(
        "--boundaries",
        type=Path,
        default=Path(os.environ.get("GAM_BOUNDARIES_FILE", DEFAULT_BOUNDARIES)),
        help="Admin-2 boundary shapefile whose adm2_name field the QGIS "
             "project joins on. Only its attribute table is read.",
    )
    parser.add_argument(
        "--skip-qgis-export",
        action="store_true",
        help="Do not write the QGIS join CSV (the boundary layer is then "
             "not needed).",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    if not args.portfolio.is_file():
        raise SystemExit(
            f"Portfolio workbook not found: {args.portfolio}\n"
            "The GAM Assurance policy portfolio is confidential and is not\n"
            "distributed with this repository. Place your copy at that path,\n"
            "or point the script elsewhere:\n"
            "    python RisquePredictionCatBoost.py --portfolio /path/to/file.xlsx\n"
            "See docs/DATA_SOURCES.md for the expected column schema."
        )
    if not args.zones.is_file():
        raise SystemExit(
            f"Seismic zone table not found: {args.zones}\n"
            "It ships with this repository; re-download it if it is missing."
        )
    if not args.skip_qgis_export and not args.boundaries.is_file():
        raise SystemExit(
            f"Boundary layer not found: {args.boundaries}\n"
            "It ships with this repository and supplies the adm2_name values\n"
            "the QGIS project joins on. Re-download it, point --boundaries at\n"
            "it, or pass --skip-qgis-export to build only the Excel tables."
        )

    args.outdir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 1. Load and concatenate the portfolio, one sheet per year
    # ------------------------------------------------------------------
    print("=== Loading Data ===")

    excel_file = pd.ExcelFile(args.portfolio)
    all_sheets = []

    for sheet_name in excel_file.sheet_names:
        df_sheet = pd.read_excel(excel_file, sheet_name=sheet_name)
        df_sheet.columns = df_sheet.columns.str.strip()
        # Keep the origin year as a feature: portfolio composition and risk
        # mix are not identical across the three exercise years.
        df_sheet["Source_Year"] = sheet_name
        all_sheets.append(df_sheet)

    df = pd.concat(all_sheets, ignore_index=True)
    print(f"Combined {len(excel_file.sheet_names)} years of data: {excel_file.sheet_names}")
    print(f"Total rows: {len(df)}")

    # ------------------------------------------------------------------
    # 2. Attach the RPA99 seismic zone
    # ------------------------------------------------------------------
    print("\n=== Loading Seismic Zones ===")
    df_zones = pd.read_excel(args.zones)
    print("Zone file columns:", df_zones.columns.tolist())
    df_zones.columns = df_zones.columns.str.strip()
    df.columns = df.columns.str.strip()

    # Place names differ between the GAM export and the RPA99 annex, and the
    # GAM COMMUNE field is a composite "COMMUNE WILAYA" string, so the raw
    # values cannot be joined directly. Keep the raw columns intact and build
    # explicit normalisation keys for the merge.
    df["COMMUNE_RAW"] = df["COMMUNE"]
    df["WILAYA_RAW"] = df["WILAYA"]

    df["COMMUNE_KEY"], df["WILAYA_KEY"] = build_join_keys(
        df["COMMUNE_RAW"], df["WILAYA_RAW"]
    )

    # The RPA99 commune names are already atomic, so only the GAM side needs
    # the trailing-wilaya strip. build_join_keys() is therefore applied to the
    # GAM frame only; the zones are normalised label-by-label.
    zones_lookup = pd.DataFrame({
        "COMMUNE_KEY": df_zones["COMMUNE"].map(normalize_place),
        "WILAYA_KEY": df_zones["wilaya_name"].map(normalize_place),
        "ZONE SEISMIQUE": df_zones["ZONE SEISMIQUE"],
    })

    print("\n=== Merging with Seismic Zones (using COMMUNE_KEY + WILAYA_KEY) ===")

    # ------------------------------------------------------------------
    # Stage 1: authoritative join on the (COMMUNE_KEY, WILAYA_KEY) pair.
    # ------------------------------------------------------------------
    # A pair carrying more than one distinct zone in the zoning table is
    # genuinely ambiguous, so it is excluded rather than resolved by table
    # order. (In the shipped annex no pair is ambiguous: every one of the
    # 1,541 communes carries a single zone.)
    pair_zone_counts = zones_lookup.groupby(
        ["COMMUNE_KEY", "WILAYA_KEY"]
    )["ZONE SEISMIQUE"].nunique()
    ambiguous_pairs = set(pair_zone_counts[pair_zone_counts > 1].index)

    zones_pair_index = pd.MultiIndex.from_frame(
        zones_lookup[["COMMUNE_KEY", "WILAYA_KEY"]]
    )
    zones_by_pair = zones_lookup.loc[~zones_pair_index.isin(ambiguous_pairs)]
    zones_by_pair = zones_by_pair.drop_duplicates(
        subset=["COMMUNE_KEY", "WILAYA_KEY"]
    )

    print(f"Zoning table rows: {len(zones_lookup)}")
    print(f"Distinct (COMMUNE_KEY, WILAYA_KEY) pairs: {len(pair_zone_counts)}")
    print(f"Ambiguous pairs withheld from stage 1: {len(ambiguous_pairs)}")
    for commune_key, wilaya_key in sorted(ambiguous_pairs):
        zones_seen = sorted(
            zones_lookup.loc[
                (zones_lookup["COMMUNE_KEY"] == commune_key)
                & (zones_lookup["WILAYA_KEY"] == wilaya_key),
                "ZONE SEISMIQUE",
            ].astype(str).unique()
        )
        print(f"  - {commune_key} / {wilaya_key} -> competing zones {zones_seen}")

    df_combined = pd.merge(
        df,
        zones_by_pair[["COMMUNE_KEY", "WILAYA_KEY", "ZONE SEISMIQUE"]],
        on=["COMMUNE_KEY", "WILAYA_KEY"],
        how="left",
    )
    matched_by_pair = df_combined["ZONE SEISMIQUE"].notna()
    print(f"Stage 1 - matched by COMMUNE_KEY + WILAYA_KEY: {int(matched_by_pair.sum())}")

    # ------------------------------------------------------------------
    # Stage 2: fill the residual gaps on the commune key alone.
    # ------------------------------------------------------------------
    # The GAM WILAYA column cannot carry the join on its own: it holds 56
    # spellings for 48 wilayas, and 17 of them do not resolve to any RPA99
    # wilaya name at all (typos such as "TAMENRASSET" for Tamanrasset or
    # "OURGLA" for Ouargla, the abbreviation "B.B ARRERIDJ" for Bordj Bou
    # Arreridj, and commune names such as "DJANET" or "IN SALAH" filed in
    # the wilaya column). Even where the spelling does resolve, a minority of
    # the communes filed under it belong to a different wilaya, so the pair
    # join alone leaves ~12.5% of the portfolio unclassified.
    #
    # The fallback therefore looks the commune name up in a table of
    # communes that the annex assigns a *single, verified* zone to. It is not
    # an order-dependent pick: a commune name that the annex splits across
    # several zones is excluded and reported, so no row is ever guessed. It
    # also cannot contradict stage 1, because it only fills rows stage 1 left
    # empty.
    commune_zone_counts = zones_lookup.groupby("COMMUNE_KEY")["ZONE SEISMIQUE"].nunique()
    ambiguous_communes = set(commune_zone_counts[commune_zone_counts > 1].index)
    zones_by_commune = (
        zones_lookup.loc[~zones_lookup["COMMUNE_KEY"].isin(ambiguous_communes)]
        .drop_duplicates("COMMUNE_KEY")
        .set_index("COMMUNE_KEY")["ZONE SEISMIQUE"]
    )

    print(f"Commune names the annex splits across several zones: {len(ambiguous_communes)}")
    print(f"  {sorted(ambiguous_communes)}")

    still_empty = ~matched_by_pair
    if still_empty.any():
        fallback_zone = zones_by_commune.reindex(
            df_combined.loc[still_empty, "COMMUNE_KEY"]
        )
        assignable = fallback_zone.notna()
        target = df_combined.index[still_empty][assignable]
        df_combined.loc[target, "ZONE SEISMIQUE"] = fallback_zone[assignable].values
        print(
            f"Stage 2 - additionally resolved on a single-zone COMMUNE_KEY: "
            f"{int(assignable.sum())}"
        )

    # Classify the residue so the report distinguishes "the place is not in
    # the annex", "the place exists but spans several zones" and "the pair
    # was ambiguous".
    portfolio_pairs = pd.MultiIndex.from_frame(
        df_combined[["COMMUNE_KEY", "WILAYA_KEY"]]
    )
    df_combined["ZONE_MATCH_STATUS"] = np.where(
        df_combined["ZONE SEISMIQUE"].notna(),
        "matched",
        np.where(
            df_combined["COMMUNE_KEY"].isin(ambiguous_communes),
            "ambiguous_commune",
            np.where(
                portfolio_pairs.isin(ambiguous_pairs), "ambiguous_pair", "unmatched"
            ),
        ),
    )

    # Restore the display columns to the upper-cased raw text used by the
    # downstream grouping and by the published commune-level tables.
    df_combined["COMMUNE"] = df_combined["COMMUNE_RAW"].str.strip().str.upper()
    df_combined["WILAYA"] = df_combined["WILAYA_RAW"].str.strip().str.upper()

    # ------------------------------------------------------------------
    # 2b. Report the rows that could not be resolved
    # ------------------------------------------------------------------
    print("\n=== Zone Matching Report ===")
    status_counts = df_combined["ZONE_MATCH_STATUS"].value_counts()
    matched = int(status_counts.get("matched", 0))
    ambiguous_commune_rows = int(status_counts.get("ambiguous_commune", 0))
    ambiguous_pair_rows = int(status_counts.get("ambiguous_pair", 0))
    unmatched_rows = int(status_counts.get("unmatched", 0))
    total_rows = len(df_combined)
    unresolved_rows = ambiguous_commune_rows + ambiguous_pair_rows + unmatched_rows

    print(f"Total portfolio rows            : {total_rows}")
    print(f"Matched to a seismic zone       : {matched} ({matched / total_rows * 100:.2f}%)")
    print(f"  of which via the pair join    : {int(matched_by_pair.sum())}")
    print(f"  of which via the single-zone commune fallback: {matched - int(matched_by_pair.sum())}")
    print(f"Ambiguous commune (zone withheld): {ambiguous_commune_rows} ({ambiguous_commune_rows / total_rows * 100:.2f}%)")
    print(f"Ambiguous pair (zone withheld)  : {ambiguous_pair_rows} ({ambiguous_pair_rows / total_rows * 100:.2f}%)")
    print(f"Unmatched (not in the annex)    : {unmatched_rows} ({unmatched_rows / total_rows * 100:.2f}%)")
    print(f"Rows left as 'Unknown'          : {unresolved_rows} ({unresolved_rows / total_rows * 100:.2f}%)")

    if unresolved_rows:
        residue = df_combined.loc[
            df_combined["ZONE_MATCH_STATUS"] != "matched"
        ]
        report = (
            residue.groupby(
                ["ZONE_MATCH_STATUS", "COMMUNE_KEY", "WILAYA_KEY", "COMMUNE", "WILAYA"],
                dropna=False,
            )
            .size()
            .reset_index(name="ROWS")
            .sort_values(["ZONE_MATCH_STATUS", "ROWS"], ascending=[True, False])
        )
        report_path = args.outdir / "unmatched_communes_report.csv"
        report.to_csv(report_path, index=False, encoding="utf-8-sig")
        print(
            f"\nDistinct unresolved commune names: "
            f"{report['COMMUNE_KEY'].nunique()}  "
            f"across {len(report)} (COMMUNE_KEY, WILAYA_KEY) combinations"
        )
        print(f"Detail written to: {report_path}")
        for status in ("ambiguous_commune", "ambiguous_pair", "unmatched"):
            block = report[report["ZONE_MATCH_STATUS"] == status]
            if block.empty:
                continue
            print(f"\n  {status} ({len(block)} distinct keys):")
            for _, row in block.iterrows():
                label = row["COMMUNE"] if row["COMMUNE"] else "<null>"
                wilaya_label = row["WILAYA"] if row["WILAYA"] else "<null>"
                print(f"    {label} / {wilaya_label:<22} rows={row['ROWS']}")

    df_combined["ZONE SEISMIQUE"] = df_combined["ZONE SEISMIQUE"].fillna("Unknown")

    # ------------------------------------------------------------------
    # 3. Clean the numeric exposure and coerce the categorical columns
    # ------------------------------------------------------------------
    print("\n=== Processing CAPITAL_ASSURE ===")
    df_combined["CAPITAL_ASSURE"] = parse_capital(df_combined["CAPITAL_ASSURE"])

    print(f"Average Capital: {df_combined['CAPITAL_ASSURE'].mean():,.2f} DZD")

    print("\n=== Creating Risk Labels ===")

    # CatBoost requires categorical columns to be non-null strings.
    df_combined["TYPE"] = df_combined["TYPE"].astype(str).str.strip()
    df_combined["COMMUNE"] = df_combined["COMMUNE"].fillna("Unknown").astype(str)
    df_combined["WILAYA"] = df_combined["WILAYA"].fillna("Unknown").astype(str)
    df_combined["ZONE SEISMIQUE"] = df_combined["ZONE SEISMIQUE"].fillna("Unknown").astype(str)
    df_combined["TYPE"] = df_combined["TYPE"].fillna("Unknown").astype(str)

    df_combined["Risque_Color"] = df_combined.apply(label_risque, axis=1)
    df_combined["Risque_Color"] = df_combined["Risque_Color"].fillna("Green")

    print("Unique Colors found:", df_combined["Risque_Color"].unique())
    print("Counts per color:\n", df_combined["Risque_Color"].value_counts())

    # ------------------------------------------------------------------
    # 4. Train the CatBoost classifier
    # ------------------------------------------------------------------
    print("\n=== Training CatBoost Model ===")

    features = ["COMMUNE", "WILAYA", "ZONE SEISMIQUE", "TYPE", "CAPITAL_ASSURE", "Source_Year"]
    target = "Risque_Color"
    categorical_features = ["COMMUNE", "WILAYA", "ZONE SEISMIQUE", "TYPE", "Source_Year"]

    # CatBoost consumes categorical features natively through ordered target
    # statistics - no one-hot encoding or label encoder required. That is the
    # reason the organisers specified CatBoost over Random Forest here: the
    # commune feature alone has ~1,500 distinct values.
    #
    # train_dir redirects the training logs and the catboost_info/ folder into
    # the output directory instead of the current working directory.
    model = CatBoostClassifier(
        iterations=500,
        loss_function="MultiClass",
        verbose=100,
        random_seed=42,
        train_dir=str(args.outdir / "catboost_info"),
        allow_writing_files=True,
    )

    model.fit(df_combined[features], df_combined[target], cat_features=categorical_features)

    df_combined["Final_Classification"] = model.predict(df_combined[features]).flatten()

    classified_path = args.outdir / "Algeria_Seismic_Risk_Classified.xlsx"
    df_combined.to_excel(classified_path, index=False)
    print(f"\nClassification complete! Saved to {classified_path}")

    # ------------------------------------------------------------------
    # 5. Aggregate to commune level for the QGIS join
    # ------------------------------------------------------------------
    print("\n=== Creating Yearly Summary ===")
    # Dominant risk per commune per year: the modal (most frequent) class.
    df_yearly = df_combined.groupby(["COMMUNE", "WILAYA", "Source_Year"]).agg({
        "CAPITAL_ASSURE": "sum",
        "Final_Classification": lambda x: x.mode()[0] if len(x) > 0 else "Green",
        "Risque_Color": "count",
    }).reset_index()
    df_yearly.columns = ["COMMUNE", "WILAYA", "YEAR", "TOTAL_CAPITAL", "DOMINANT_RISK", "POLICY_COUNT"]
    yearly_path = args.outdir / "Algeria_Seismic_Risk_By_Year.xlsx"
    df_yearly.to_excel(yearly_path, index=False)
    print(f"Yearly summary saved to {yearly_path}")

    print("\n=== Creating Aggregated Summary (Average across years) ===")

    def get_dominant_risk(risks):
        return risks.mode()[0] if len(risks) > 0 else "Green"

    def get_avg_capital(years_data):
        return years_data.mean()

    df_agg = df_combined.groupby(["COMMUNE", "WILAYA"]).agg({
        "CAPITAL_ASSURE": "mean",
        "Final_Classification": get_dominant_risk,
        "Source_Year": "nunique",
    }).reset_index()

    df_agg.columns = ["COMMUNE", "WILAYA", "AVG_CAPITAL", "DOMINANT_RISK", "YEARS_COUNT"]

    communes_with_multiple_years = df_agg[df_agg["YEARS_COUNT"] > 1]
    print(f"Communes with data in multiple years: {len(communes_with_multiple_years)}")

    # Re-derive DOMINANT_RISK over the whole three-year window (the grouped
    # aggregation above pools the years; this makes the "dominant" label
    # independent of the year weighting).
    df_agg["Final_Classification"] = df_combined.groupby(["COMMUNE", "WILAYA"])["Final_Classification"].apply(
        lambda x: x.mode()[0] if len(x) > 0 else "Green"
    ).values

    df_agg = df_agg[["COMMUNE", "WILAYA", "AVG_CAPITAL", "DOMINANT_RISK", "YEARS_COUNT"]]

    summary_path = args.outdir / "Algeria_Seismic_Summary_for_QGIS.xlsx"
    df_agg.to_excel(summary_path, index=False)
    print(f"QGIS summary (averaged) saved to {summary_path}")

    # ------------------------------------------------------------------
    # 6. Export the join table the QGIS project actually reads
    # ------------------------------------------------------------------
    qgis_csv_path = None
    if not args.skip_qgis_export:
        print("\n=== Exporting QGIS Join Table ===")
        boundary = load_boundary_index(args.boundaries)
        print(
            f"Boundary polygons: {len(boundary)} "
            f"({boundary['adm2_name'].nunique()} distinct adm2_name)"
        )

        # Resolve on the distinct key pairs only, then broadcast back to the
        # policy rows: the answer depends only on (COMMUNE_KEY, WILAYA_KEY).
        key_pairs = df_combined[["COMMUNE_KEY", "WILAYA_KEY"]].drop_duplicates()
        pair_names, pair_status = resolve_boundary_communes(
            key_pairs["COMMUNE_KEY"], key_pairs["WILAYA_KEY"], boundary
        )
        lookup = pd.DataFrame({
            "COMMUNE_KEY": key_pairs["COMMUNE_KEY"].values,
            "WILAYA_KEY": key_pairs["WILAYA_KEY"].values,
            "adm2_name": pair_names.values,
            "boundary_status": pair_status.values,
        })
        df_combined = df_combined.merge(lookup, on=["COMMUNE_KEY", "WILAYA_KEY"], how="left")

        pair_counts = lookup["boundary_status"].value_counts()
        resolved_pairs = int(lookup["adm2_name"].notna().sum())
        print(f"Distinct (COMMUNE_KEY, WILAYA_KEY) pairs: {len(lookup)}")
        for status, count in pair_counts.items():
            print(f"  {status:<24}: {count}")
        print(
            f"Pairs resolved to a boundary polygon: {resolved_pairs} "
            f"({resolved_pairs / len(lookup) * 100:.2f}%)"
        )

        # Aggregate onto the boundary polygon. adm2_name is unique in the
        # layer, and a polygon can collect several GAM groups because the
        # export often files a wilaya name in the COMMUNE column; pooling them
        # keeps the join 1:1 instead of letting QGIS pick the first match.
        mapped = df_combined[df_combined["adm2_name"].notna()]
        df_qgis = mapped.groupby("adm2_name").agg(
            TOTAL_CAPITAL=("CAPITAL_ASSURE", "sum"),
            DOMINANT_RISK=("Final_Classification", get_dominant_risk),
            POLICY_COUNT=("Final_Classification", "count"),
            YEARS_COUNT=("Source_Year", "nunique"),
        ).reset_index()

        print(
            f"Boundary polygons with at least one policy: {len(df_qgis)} "
            f"of {len(boundary)}"
        )
        print(f"Policies placed on a polygon: {len(mapped):,} of {len(df_combined):,}")
        unmapped_policies = len(df_combined) - len(mapped)
        if unmapped_policies:
            print(
                f"Warning: {unmapped_policies} policies "
                f"({unmapped_policies / len(df_combined) * 100:.2f}%) could not be "
                "placed on the boundary layer and are absent from the CSV"
            )
        print("Risk categories in the CSV:", df_qgis["DOMINANT_RISK"].value_counts().to_dict())

        qgis_csv_path = args.outdir / "Algeria_Seismic_Summary_for_QGIS_Mapped.csv"
        write_qgis_csv(df_qgis, qgis_csv_path)
        print(f"QGIS join CSV written to: {qgis_csv_path}")

        # Record the unresolved keys so the gap is auditable. Commune-level
        # only: no policy numbers, capitals or premiums.
        unmapped_keys = lookup[lookup["adm2_name"].isna()].copy()
        if not unmapped_keys.empty:
            report_path = args.outdir / "qgis_unmapped_communes.csv"
            unmapped_keys.to_csv(report_path, index=False, encoding="utf-8-sig")
            print(
                f"Distinct unresolved commune keys: "
                f"{unmapped_keys['COMMUNE_KEY'].nunique()} "
                f"(detail in {report_path.name})"
            )

    print("\n=== Training Complete ===")
    print(f"- Classified {len(df_combined)} records")
    outputs = [classified_path.name, yearly_path.name, summary_path.name]
    if qgis_csv_path is not None:
        outputs.append(qgis_csv_path.name)
    print(f"- Output files: {', '.join(outputs)}")


if __name__ == "__main__":
    main()
