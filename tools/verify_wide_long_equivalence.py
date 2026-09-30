"""Equivalence: the wide workbook and its long-format copy must analyse identically.

    python tools/verify_wide_long_equivalence.py [--base URL]

The two files hold **the same data in two encodings**:

* `TW Impact Study_V2 1 (1).xlsx` — wide: one row per entity, the periods are
  columns (`Sales Value YA` / `Sales Value`).
* `TW_Impact_Study_V3_LONG_MAT_YA_MAT_TY.xlsx` — long: one metric column and a
  `Periods` column carrying `MAT YA` / `MAT TY` rows.

Both are run through the real HTTP path for the same categories, market scope and
Top-N, and every headline figure plus every ranked row is compared. If the engine
read the periods differently between the two, these numbers would differ - which
is the whole point of the check. Two encodings agreeing is not a tautology here:
the wide path reads two *columns*, the long path reads one column twice with a
row filter, so they share no code below the config.

Known, expected difference: `total.rows_before` / `rows_after` count *rows*, and
the long file has one row per (entity, period) instead of one per entity. That
field is excluded from the comparison and reported separately.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

WIDE = os.path.join(ROOT, "TW Impact Study_V2 1 (1).xlsx")
LONG = os.path.join(ROOT, "TW_Impact_Study_V3_LONG_MAT_YA_MAT_TY.xlsx")
MARKET = "TW Total TW Offline (G)"
CATEGORIES = ["ADULT DIAPER", "BEER", "BISCUIT", "SNACK", "CIGARETTE"]
DIMS = {"category": "CATEGORY", "market": "Display Market Name",
        "manufacturer": "MANUFACTURER", "brand": "BRAND", "period": "Periods"}

ok_all = True


def check(name, ok, msg=""):
    global ok_all
    if not ok:
        ok_all = False
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {msg}" if msg else ""))
    return ok


def post(base, path, payload, timeout=1800):
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def run(base, spec_a, spec_b, label):
    common = {
        "a": spec_a, "b": spec_b,
        "dim_col_a": DIMS, "dim_col_b": dict(DIMS),
        "period_col_a": "Periods", "period_col_b": "Periods",
        "metric_label": "Sales Value",
        "a_prior": "", "a_current": "", "b_prior": "", "b_current": "",
        "is_rate": False,
        "market_pairs": [{"market_a": MARKET, "market_b": MARKET, "level": "total"}],
        "market_level": "total", "baseline_market": MARKET,
        "categories": CATEGORIES, "top_n": 10, "client_brands": [],
        "mapping_a": {}, "mapping_b": {}, "trend_enabled": False,
        "run_name": label,
    }
    d = post(base, "/api/run", common, timeout=1800)
    return {r["category"]: r for r in d["reports"]}, d


def num(v):
    return None if v is None else float(v)


def close(a, b, rel_tol=1e-9, abs_tol=1e-3):
    """Agreement to nine significant figures.

    The two encodings sum *different numbers of values in a different order* -
    the wide frame carries a NaN where a period has no figure, the long frame
    simply has no row - so the totals differ in the last bits of a float64. That
    is summation noise, not a period read differently: a wrong period would move
    these figures by billions, and nine significant figures is far tighter than
    any real discrepancy while still clearing the noise.
    """
    if a is None or b is None:
        return a is None and b is None
    a, b = float(a), float(b)
    if a == b:
        return True
    return abs(a - b) <= max(abs_tol, rel_tol * max(abs(a), abs(b)))


def rel_gap(a, b):
    if a is None or b is None:
        return float("inf")
    a, b = float(a), float(b)
    if a == b:
        return 0.0
    return abs(a - b) / max(abs(a), abs(b), 1e-30)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8796")
    args = ap.parse_args()
    base = args.base

    print("=" * 78)
    print(" Wide vs long encoding, live against", base)
    print("=" * 78)

    for path in (WIDE, LONG):
        if not os.path.isfile(path):
            print(f"  MISSING {path}")
            return 1

    # --- the wide workbook: Raw_MAT split on the Dataset discriminator ------
    sw = post(base, "/api/source/from-path", {"path": WIDE})
    wide = run(base,
               {"source_id": sw["id"], "sheet": "Raw_MAT", "header_row": 1,
                "split_column": "Dataset", "value": "Current MAT"},
               {"source_id": sw["id"], "sheet": "Raw_MAT", "header_row": 1,
                "split_column": "Dataset", "value": "New MAT"}, "wide_ref")

    # --- the long workbook: two sheets, no split needed --------------------
    sl = post(base, "/api/source/from-path", {"path": LONG})
    prof = post(base, "/api/profile", {
        "a": {"source_id": sl["id"], "sheet": "Current_MAT", "header_row": 1},
        "b": {"source_id": sl["id"], "sheet": "New_MAT", "header_row": 1}})
    fams = prof["a"]["metric_families"]
    check("the long file has one column per measure (no YA / 2YA variants)",
          all(len(v) == 1 for v in fams.values())
          and not any("YA" in c for f in fams.values() for c in f.values()),
          str({k: list(v.values()) for k, v in fams.items()}))
    check("its Periods column carries both MAT YA and MAT TY",
          set(prof["a"]["column_profiles"][
              [c["name"] for c in prof["a"]["column_profiles"]].index("Periods")
          ]["samples"]) >= {"MAT YA", "MAT TY"})

    lng = run(base,
              {"source_id": sl["id"], "sheet": "Current_MAT", "header_row": 1},
              {"source_id": sl["id"], "sheet": "New_MAT", "header_row": 1},
              "long_ref")

    wrep, wd = wide
    lrep, ld = lng
    check("both files produce the same categories",
          sorted(wrep) == sorted(lrep), f"{sorted(wrep)} vs {sorted(lrep)}")

    # --- headline figures ---------------------------------------------------
    print("\n--- headline figures, per category -------------------------------")
    fields = ["before_prior", "before_current", "after_prior", "after_current",
              "abs_change", "before_growth_pct", "after_growth_pct"]
    mismatches = []
    worst = 0.0
    for cat in sorted(set(wrep) & set(lrep)):
        wt, lt = wrep[cat]["total"], lrep[cat]["total"]
        gaps = {f: rel_gap(wt.get(f), lt.get(f)) for f in fields}
        worst = max(worst, max(gaps.values()))
        diffs = {f: (wt.get(f), lt.get(f)) for f in fields
                 if not close(wt.get(f), lt.get(f))}
        tag = "OK" if not diffs else "MISMATCH"
        print(f"  {cat:16s} MAT YA {wt['before_prior']:>16.4f} / "
              f"MAT TY {wt['before_current']:>16.4f}  "
              f"max rel diff {max(gaps.values()):.2e}  {tag}")
        if diffs:
            mismatches.append((cat, diffs))
    check("every headline figure agrees between the two encodings (9 sig figs)",
          not mismatches,
          f"worst relative difference {worst:.2e}" if not mismatches
          else json.dumps(mismatches[:2])[:300])

    # --- ranked rows --------------------------------------------------------
    print("\n--- Manufacturer Top-N, per category -----------------------------")
    rank_fields = ["name", "before_prior", "before_current", "after_prior",
                   "after_current", "share_change_pp", "rank_before",
                   "rank_after", "rank_change", "movement"]
    bad_rows = []
    for cat in sorted(set(wrep) & set(lrep)):
        wm = wrep[cat]["manufacturer_top_n"]
        lm = lrep[cat]["manufacturer_top_n"]
        if len(wm) != len(lm):
            bad_rows.append((cat, "length", len(wm), len(lm)))
            continue
        for a, b in zip(wm, lm):
            for f in rank_fields:
                if f in ("name", "movement", "rank_before", "rank_after",
                         "rank_change"):
                    same = a.get(f) == b.get(f)
                else:
                    same = close(a.get(f), b.get(f))
                if not same:
                    bad_rows.append((cat, a.get("name"), f, a.get(f), b.get(f)))
        print(f"  {cat:16s} top1={wm[0]['name'] if wm else '-':24s} "
              f"MAT YA {wm[0]['before_prior'] if wm else 0:>15.4f}  "
              f"{'OK' if not any(x[0] == cat for x in bad_rows) else 'MISMATCH'}")
    check("every ranked row agrees, field by field",
          not bad_rows, json.dumps(bad_rows[:3])[:400])

    # --- and the one difference that is real, stated not hidden -------------
    print("\n--- the row counts, which legitimately differ --------------------")
    for cat in sorted(set(wrep) & set(lrep))[:3]:
        print(f"  {cat:16s} wide rows {wrep[cat]['total']['rows_before']:>7} / "
              f"{wrep[cat]['total']['rows_after']:>7}   "
              f"long rows {lrep[cat]['total']['rows_before']:>7} / "
              f"{lrep[cat]['total']['rows_after']:>7}")
    check("the long file's row counts are larger (one row per entity per period)",
          all(lrep[c]["total"]["rows_before"] > wrep[c]["total"]["rows_before"]
              for c in sorted(set(wrep) & set(lrep))))

    # --- both are clean runs ------------------------------------------------
    check("the wide run has no QC failures", wd["qc"]["counts"]["FAIL"] == 0,
          str(wd["qc"]["counts"]))
    check("the long run has no QC failures", ld["qc"]["counts"]["FAIL"] == 0,
          str(ld["qc"]["counts"]))
    check("the long run resolves both periods without a degenerate warning",
          not any("used twice" in n for n in (ld.get("notes") or [])),
          str((ld.get("notes") or [])[:2]))

    print("\n" + "=" * 78)
    print(" ALL CHECKS PASSED" if ok_all else " SOME CHECKS FAILED")
    print("=" * 78)
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
