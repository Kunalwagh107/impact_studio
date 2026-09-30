"""Build a **long-format** (periods as rows) copy of the reference workbook.

    python tools/make_long_fixture.py [--out PATH]

The reference workbook is the **wide** convention: one row per entity, with the
periods as separate *columns* (`Sales Value 2YA` / `Sales Value YA` /
`Sales Value`). This writes the same data in the **long** convention - one metric
column per measure and a `Periods` column carrying `MAT YA` / `MAT TY` as rows -
which is the shape the client's real extract uses and the shape the engine now
reads.

Two sheets, same structure as the source:

    Current_MAT   <- the previous dataset (A)
    New_MAT       <- the updated dataset (B)

Columns: Dataset · Display Market Name · Markets · Periods · CATEGORY ·
MANUFACTURER · BRAND · Sales Value · Sales Volume · ND Dist

**`2YA` is dropped on purpose.** It is a third moving-annual window the study
does not use; carrying it into the long file would only invite it back as "the
year ago". The script reports how much value that drops so the loss is visible
rather than silent.

The two encodings must produce identical analysis numbers. The script re-derives
every sum by a second route and asserts it before writing, so a faithful
conversion is proved rather than assumed.
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SOURCE = os.path.join(ROOT, "TW Impact Study_V2 1 (1).xlsx")
DEFAULT_OUT = os.path.join(ROOT, "TW_Impact_Study_V3_LONG_MAT_YA_MAT_TY.xlsx")

DIMS = ["Dataset", "Display Market Name", "Markets", "CATEGORY",
        "MANUFACTURER", "BRAND"]
METRICS = ["Sales Value", "Sales Volume", "ND Dist"]

# measure -> the wide column each period reads
WIDE = {
    "MAT YA": {"Sales Value": "Sales Value YA",
               "Sales Volume": "Sales Volume YA",
               "ND Dist": "ND Dist YA"},
    "MAT TY": {"Sales Value": "Sales Value",
               "Sales Volume": "Sales Volume",
               "ND Dist": "ND Dist"},
}
DROPPED = {"Sales Value": "Sales Value 2YA", "Sales Volume": "Sales Volume 2YA"}


def long_sheet(wide: pd.DataFrame, label: str) -> pd.DataFrame:
    """One wide sheet -> the long encoding, MAT YA block then MAT TY block."""
    blocks = []
    for period in ("MAT YA", "MAT TY"):
        block = wide[DIMS].copy()
        block["Periods"] = period
        for metric in METRICS:
            src = WIDE[period][metric]
            if src not in wide.columns:
                raise SystemExit(f"{label}: source column '{src}' is missing")
            block[metric] = pd.to_numeric(wide[src], errors="coerce")
        # A period an entity genuinely has no figures for is not a row. The sums
        # are unaffected either way (NaN contributes nothing), but keeping empty
        # rows would inflate the row count for no information.
        has_data = block[METRICS].notna().any(axis=1)
        blocks.append(block[has_data])
    out = pd.concat(blocks, ignore_index=True)
    return out[DIMS + ["Periods"] + METRICS]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--source", default=SOURCE)
    args = ap.parse_args()

    if not os.path.isfile(args.source):
        print(f"source workbook not found: {args.source}")
        return 1

    print("=" * 78)
    print(" Building a long-format copy of", os.path.basename(args.source))
    print("=" * 78)

    xl = pd.ExcelFile(args.source)
    sheets: dict[str, pd.DataFrame] = {}
    problems = 0
    for sheet in ("Current_MAT", "New_MAT"):
        if sheet not in xl.sheet_names:
            print(f"  MISSING SHEET {sheet}")
            return 1
        wide = xl.parse(sheet)
        long = long_sheet(wide, sheet)
        sheets[sheet] = long

        # --- prove the conversion before writing it -------------------------
        # The equivalence that matters: for every metric, the sum over the
        # MAT YA rows must equal the sum of the wide `... YA` column, and the
        # sum over MAT TY rows the sum of the unqualified column.
        print(f"\n  {sheet}: {len(wide):,} wide rows -> {len(long):,} long rows "
              f"({(long['Periods'] == 'MAT YA').sum():,} MAT YA / "
              f"{(long['Periods'] == 'MAT TY').sum():,} MAT TY)")
        for period in ("MAT YA", "MAT TY"):
            sub = long[long["Periods"] == period]
            for metric in METRICS:
                want = pd.to_numeric(wide[WIDE[period][metric]], errors="coerce").sum()
                got = sub[metric].sum()
                same = (abs(want - got) <= max(1e-6, abs(want) * 1e-12)
                        or (np.isnan(want) and np.isnan(got)))
                print(f"      {period}  {metric:14s} wide={want:18.6f} "
                      f"long={got:18.6f}  {'OK' if same else 'MISMATCH'}")
                if not same:
                    problems += 1
        # And the entity grain must be untouched: no duplicate (dims, period).
        dup = int(long.duplicated(subset=DIMS + ["Periods"]).sum())
        print(f"      duplicate (entity, period) rows: {dup}")
        if dup:
            problems += 1
        # What the 2YA drop costs, stated rather than hidden.
        for metric, col in DROPPED.items():
            if col in wide.columns:
                v = pd.to_numeric(wide[col], errors="coerce").sum()
                print(f"      dropped (not in the study): {col:18s} sum={v:18.6f}")

    if problems:
        print(f"\n  {problems} RECONCILIATION PROBLEM(S) - not writing the file.")
        return 1

    out = args.out
    if not os.path.isabs(out):
        out = os.path.join(ROOT, out)
    print(f"\n  writing {out} ...")
    with pd.ExcelWriter(out, engine="xlsxwriter") as xw:
        for sheet, df in sheets.items():
            df.to_excel(xw, sheet_name=sheet, index=False)
    print(f"  written: {os.path.getsize(out)/1024/1024:.1f} MB, "
          f"sheets {list(sheets)}")
    print("\n  Both encodings reconciled on every metric and period.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
