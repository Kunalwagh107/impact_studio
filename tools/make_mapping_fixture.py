"""Build the 5-category adversarial fixture.

Why this exists
---------------
The supplied TW workbook is *already harmonised*: its ``Raw_MAT`` sheet is a
VSTACK of the two datasets and its ``Display Market Name`` column is pre-mapped,
so a name-equality lookup returns ``exact`` for nearly every category. Testing
that file proves that an identity mapping reproduces an identity mapping — it
proves nothing about whether the mapping logic can handle two datasets that
define their categories differently, which is the actual requirement.

This fixture is deliberately *hostile* to a lookup. Five Dataset-1 categories
cover every relationship the brief calls out, and the names deliberately do NOT
line up, so a lookup-based engine cannot get them right by accident:

  +----------------+----------------------------------------+------------------+
  | A category     | B definition                           | relationship     |
  +================+========================================+==================+
  | BISCUITS       | TANDY / BISCUITS                       | composite        |
  |                |  A name matches a B *subcategory*      | (category+sub)   |
  +----------------+----------------------------------------+------------------+
  | SNACKS         | SAVOURY / CHIPS  +  SAVOURY / NUTS     | 1 : N            |
  |                |  one A category, two B units           |                  |
  +----------------+----------------------------------------+------------------+
  | CARB DRINKS    | BEVERAGES / CARBONATED                 | composite        |
  |                |  A "CARB DRINKS" is B's subcategory    |                  |
  +----------------+----------------------------------------+------------------+
  | BOTTLED WATER  | WATER                                  | 1 : 1            |
  |                |  the only genuinely simple row         |                  |
  +----------------+----------------------------------------+------------------+
  | DIET SUPPLEMENTS| (nothing)                             | no counterpart   |
  |                |  must be EXCLUDED from the impacts     |                  |
  +----------------+----------------------------------------+------------------+

Plus two B-only categories (PLANT BASED / TOFU, PLANT BASED / TEMPEH) which must
surface as "new in Dataset 2" rather than being silently dropped or force-matched
onto DIET SUPPLEMENTS.

The N:1 direction is exercised too: CHIPS and NUTS exist as separate Dataset-1
categories in a *second* sheet variant, so the same B target can be claimed by
two A rows and the engine must flag the double-count rather than hide it.

Values are chosen so a value-only matcher can be *tempted* into wrong pairings:
SNACKS' total equals SAVOURY/CHIPS alone at ~99%, so a naive "closest value"
match would bind SNACKS to CHIPS and leave NUTS unclaimed. Only the correct
1:N mapping reconciles all of SAVOURY.

Usage
-----
    python tools/make_mapping_fixture.py            # writes to the project root
    python tools/make_mapping_fixture.py --out X.xlsx
"""

from __future__ import annotations

import argparse
import os

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_OUT = os.path.join(ROOT, "mapping_fixture_5cat.xlsx")

# Market values: one Total plus two channels, so the market hierarchy logic is
# exercised as well. The Total is NOT the sum of the channels (channels are a
# subset), which is exactly the trap the market step must not fall into.
MARKET_TOTAL = "TW Total TW Offline (G)"
MARKET_CH_A = "TW CVS"
MARKET_CH_B = "TW PX MART"

A_ROWS = [
    # (category, subcategory, market, manufacturer, brand, sales_value, volume, nd)
    # --- BISCUITS: name matches a B *subcategory* (composite) ---------------
    ("BISCUITS", "", MARKET_TOTAL, "Tandy Co", "Tandy", 500.0, 100.0, 42.0),
    ("BISCUITS", "", MARKET_CH_A, "Tandy Co", "Tandy", 200.0, 40.0, 38.0),
    ("BISCUITS", "", MARKET_CH_B, "Tandy Co", "Tandy", 100.0, 20.0, 30.0),
    # --- SNACKS: one A category -> two B units (1:N) ------------------------
    # Total 900 = CHIPS 500 + NUTS 400 (the correct split)
    ("SNACKS", "", MARKET_TOTAL, "Crunch Ltd", "Crunchy", 900.0, 180.0, 55.0),
    ("SNACKS", "", MARKET_CH_A, "Crunch Ltd", "Crunchy", 350.0, 70.0, 50.0),
    # --- CARB DRINKS: A name matches B's subcategory (composite) ------------
    ("CARB DRINKS", "", MARKET_TOTAL, "Fizz Inc", "Fizzo", 750.0, 300.0, 61.0),
    ("CARB DRINKS", "", MARKET_CH_A, "Fizz Inc", "Fizzo", 300.0, 120.0, 58.0),
    # --- BOTTLED WATER: the one honest 1:1 --------------------------------
    ("BOTTLED WATER", "", MARKET_TOTAL, "Aqua Co", "PureDrop", 400.0, 500.0, 70.0),
    ("BOTTLED WATER", "", MARKET_CH_B, "Aqua Co", "PureDrop", 150.0, 190.0, 66.0),
    # --- DIET SUPPLEMENTS: NO counterpart in B -> must be excluded ---------
    ("DIET SUPPLEMENTS", "", MARKET_TOTAL, "Vita Corp", "VitaPlus", 620.0, 90.0, 33.0),
    ("DIET SUPPLEMENTS", "", MARKET_CH_A, "Vita Corp", "VitaPlus", 240.0, 30.0, 29.0),
]

# Dataset B: the updated taxonomies. Note the deliberate name divergence.
B_ROWS = [
    # BISCUITS now lives as a subcategory of TANDY
    ("TANDY", "BISCUITS", MARKET_TOTAL, "Tandy Co", "Tandy", 545.0, 110.0, 45.0),
    ("TANDY", "BISCUITS", MARKET_CH_A, "Tandy Co", "Tandy", 215.0, 42.0, 40.0),
    ("TANDY", "BISCUITS", MARKET_CH_B, "Tandy Co", "Tandy", 108.0, 21.0, 32.0),
    # SNACKS split into a SAVOURY category with two subcategories
    # CHIPS 495 + NUTS 395 = 890, ~1.1% below A's 900 -> a truthful 1:N
    ("SAVOURY", "CHIPS", MARKET_TOTAL, "Crunch Ltd", "Crunchy", 495.0, 99.0, 52.0),
    ("SAVOURY", "CHIPS", MARKET_CH_A, "Crunch Ltd", "Crunchy", 190.0, 38.0, 47.0),
    ("SAVOURY", "NUTS", MARKET_TOTAL, "Crunch Ltd", "Crunchy", 395.0, 79.0, 48.0),
    ("SAVOURY", "NUTS", MARKET_CH_A, "Crunch Ltd", "Crunchy", 150.0, 30.0, 44.0),
    # CARB DRINKS is B's subcategory under BEVERAGES
    ("BEVERAGES", "CARBONATED", MARKET_TOTAL, "Fizz Inc", "Fizzo", 790.0, 315.0, 63.0),
    ("BEVERAGES", "CARBONATED", MARKET_CH_A, "Fizz Inc", "Fizzo", 315.0, 126.0, 60.0),
    # WATER: the clean 1:1
    ("WATER", "", MARKET_TOTAL, "Aqua Co", "PureDrop", 385.0, 480.0, 68.0),
    ("WATER", "", MARKET_CH_B, "Aqua Co", "PureDrop", 144.0, 182.0, 64.0),
    # Present ONLY in B -> reported as new, never matched to DIET SUPPLEMENTS
    ("PLANT BASED", "TOFU", MARKET_TOTAL, "Green Co", "GreenLeaf", 60.0, 25.0, 12.0),
    ("PLANT BASED", "TEMPEH", MARKET_TOTAL, "Green Co", "GreenLeaf", 25.0, 10.0, 8.0),
]

COLUMNS = ["Dataset", "Display Market Name", "CATEGORY", "SUBCATEGORY",
           "MANUFACTURER", "BRAND", "Periods",
           "Sales Value", "Sales Value YA", "Sales Volume", "ND Dist"]

# Period variants so the period wiring has something real to resolve.
PERIOD_CUR = "MAT TY"
PERIOD_YA = "MAT YA"


def _rows_for(rows, dataset_label):
    out = []
    for (cat, sub, market, manu, brand, sv, vol, nd) in rows:
        # current period
        out.append({
            "Dataset": dataset_label, "Display Market Name": market,
            "CATEGORY": cat, "SUBCATEGORY": sub, "MANUFACTURER": manu,
            "BRAND": brand, "Periods": PERIOD_CUR,
            "Sales Value": sv, "Sales Value YA": sv,
            "Sales Volume": vol, "ND Dist": nd,
        })
        # prior period, ~4% different so growth is non-zero and meaningful
        out.append({
            "Dataset": dataset_label, "Display Market Name": market,
            "CATEGORY": cat, "SUBCATEGORY": sub, "MANUFACTURER": manu,
            "BRAND": brand, "Periods": PERIOD_YA,
            "Sales Value": round(sv * 0.96, 2), "Sales Value YA": round(sv * 0.96, 2),
            "Sales Volume": round(vol * 0.97, 2),
            "ND Dist": round(max(nd - 1.5, 0), 2),
        })
    return out


def build() -> pd.DataFrame:
    raw = _rows_for(A_ROWS, "Current MAT") + _rows_for(B_ROWS, "New MAT")
    return pd.DataFrame(raw, columns=COLUMNS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    df = build()
    a = df[df["Dataset"] == "Current MAT"]
    b = df[df["Dataset"] == "New MAT"]

    with pd.ExcelWriter(args.out, engine="xlsxwriter") as xw:
        df.to_excel(xw, sheet_name="Raw_MAT", index=False)
        a.to_excel(xw, sheet_name="Current_MAT", index=False)
        b.to_excel(xw, sheet_name="New_MAT", index=False)

    print(f"wrote {args.out}")
    print(f"  Raw_MAT      {len(df):>4} rows (A {len(a)} + B {len(b)})")
    print(f"  A categories {sorted(a['CATEGORY'].unique())}")
    print(f"  B categories {sorted(b['CATEGORY'].unique())}")
    print()
    print("  The mapping the user must define (nothing is auto-filled):")
    print("    BISCUITS        -> TANDY / BISCUITS        (composite)")
    print("    SNACKS          -> SAVOURY / CHIPS")
    print("                       + SAVOURY / NUTS        (1:N)")
    print("    CARB DRINKS     -> BEVERAGES / CARBONATED  (composite)")
    print("    BOTTLED WATER   -> WATER                   (1:1)")
    print("    DIET SUPPLEMENTS-> (none)                  (excluded)")
    print("    (none)          -> PLANT BASED / TOFU      (new in B)")
    print("    (none)          -> PLANT BASED / TEMPEH    (new in B)")


if __name__ == "__main__":
    main()
