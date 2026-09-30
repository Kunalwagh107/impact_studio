"""Live check: the two period conventions, end to end through the API.

    python tools/verify_period_rows.py [--base http://127.0.0.1:8796]

Two workbooks, one convention each:

* ``TW Impact Study_V2 1 (1) - Copy.xlsx`` has **no** ``Sales Value YA`` and no
  ``Sales Value 2YA`` column. The study must still run: MAT TY is read from the
  metric column, MAT YA is reported as unavailable rather than refused on the
  strength of a column the file was never going to have.
* a synthetic workbook stores its periods as **rows** over one metric column.
  Both periods must resolve from the ``Periods`` column, the values must be the
  sums of that period's rows, and growth must be a real MAT YA -> MAT TY move
  rather than the same column read twice.

The second is the case the reference workbook cannot prove - it is the wide
convention - so the synthetic file is what separates "reads the period column"
from "reads the same column twice and calls it 0%".
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
COPY = os.path.join(ROOT, "TW Impact Study_V2 1 (1) - Copy.xlsx")
RUN_DIR = os.path.join(HERE, "_run")
LONG_XLSX = os.path.join(RUN_DIR, "long_periods.xlsx")

ok_all = True


def check(name, ok, msg=""):
    global ok_all
    if not ok:
        ok_all = False
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {msg}" if msg else ""))
    return ok


def post(base, path, payload, timeout=900):
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def get(base, path, timeout=900):
    with _OPENER.open(base + path, timeout=timeout) as r:
        return json.loads(r.read().decode())


def build_long_workbook(path: str) -> None:
    """A fact table whose periods are rows, over a single metric column."""
    import pandas as pd

    rows = []
    # (dataset, category, manufacturer, MAT YA, MAT TY)
    #
    # CAT_A's totals are deliberately *different* between its two periods
    # (MAT YA 150, MAT TY 170): a fixture where they coincide cannot tell a real
    # MAT YA -> MAT TY growth apart from the same column read twice, which is
    # precisely the failure this file exists to catch.
    data = [
        ("Current MAT", "CAT_A", "M1", 100.0, 110.0),
        ("Current MAT", "CAT_A", "M2", 50.0, 60.0),
        ("Current MAT", "CAT_B", "M3", 200.0, 300.0),
        ("Current MAT", "CAT_B", "M4", 10.0, 5.0),
        ("New MAT", "CAT_A", "M1", 105.0, 120.0),
        ("New MAT", "CAT_A", "M2", 55.0, 65.0),
        ("New MAT", "CAT_B", "M3", 210.0, 330.0),
        ("New MAT", "CAT_B", "M4", 12.0, 6.0),
    ]
    for dataset, cat, man, ya, ty in data:
        for period, val in (("MAT YA", ya), ("MAT TY", ty)):
            rows.append({
                "Dataset": dataset,
                "Display Market Name": "TW TOTAL",
                "Markets": "TW TOTAL",
                "Periods": period,
                "CATEGORY": cat,
                "MANUFACTURER": man,
                "BRAND": f"{man}-B",
                "Sales Value": val,
                "Sales Volume": val * 2,
                "ND Dist": val / 10.0,
            })
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pd.DataFrame(rows).to_excel(path, sheet_name="Fact", index=False)


def section_copy(base: str) -> bool:
    """The Copy: no ``Sales Value YA`` / ``Sales Value 2YA`` column at all.

    Returns False only when the run was refused, so the caller can stop rather
    than pile up failures caused by a request that never happened.
    """
    src = post(base, "/api/source/from-path", {"path": COPY})
    spec_a = {"source_id": src["id"], "sheet": "Raw_MAT", "header_row": 1,
              "split_column": "Dataset", "value": "Current MAT"}
    spec_b = {"source_id": src["id"], "sheet": "Raw_MAT", "header_row": 1,
              "split_column": "Dataset", "value": "New MAT"}
    prof = post(base, "/api/profile", {"a": spec_a, "b": spec_b})
    fams = prof["a"]["metric_families"]
    check("the Copy has no YA / 2YA column",
          all(v not in ("Sales Value YA", "Sales Value 2YA")
              for f in fams.values() for v in f.values()),
          str(fams.get("Sales Value")))

    dim_a = {"category": "CATEGORY", "market": "Display Market Name",
             "manufacturer": "MANUFACTURER", "brand": "BRAND",
             "period": "Periods"}
    common = {
        "a": spec_a, "b": spec_b,
        "dim_col_a": dim_a, "dim_col_b": dict(dim_a),
        "period_col_a": "Periods", "period_col_b": "Periods",
        "metric_label": "Sales Value",
        # The *old, wrong* wiring: a YA column that does not exist. The server
        # must re-resolve it rather than refuse, which is the whole point.
        "a_prior": "Sales Value YA", "a_current": "Sales Value",
        "b_prior": "Sales Value YA", "b_current": "Sales Value",
        "is_rate": False,
        "market_pairs": [{"market_a": "TW Total TW Offline (G)",
                          "market_b": "TW Total TW Offline (G)", "level": "total"}],
        "market_level": "total",
        "baseline_market": "TW Total TW Offline (G)",
        "categories": ["ADULT DIAPER"], "top_n": 10, "client_brands": [],
        "mapping_a": {}, "mapping_b": {}, "trend_enabled": False,
    }
    try:
        d = post(base, "/api/run", common, timeout=600)
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:400]
        check("the Copy runs instead of being refused for a missing YA column",
              False, f"HTTP {e.code} · {body}")
        return False
    check("the Copy runs instead of being refused for a missing YA column", True)
    check("the Copy produces a report", bool(d.get("reports")),
          f"{d.get('n_categories')} category(ies)")
    tot = (d["reports"][0].get("total") or {}) if d.get("reports") else {}
    check("MAT TY is read from the metric column",
          tot.get("before_current") and tot.get("after_current"),
          f"before {tot.get('before_current')} after {tot.get('after_current')}")
    check("MAT YA stays empty rather than being guessed",
          tot.get("before_prior") is None and tot.get("after_prior") is None,
          f"{tot.get('before_prior')} / {tot.get('after_prior')}")
    check("the absent period is explained",
          any("no MAT YA" in n for n in (d.get("notes") or [])),
          str((d.get("notes") or [])[:2]))
    check("no 'used twice' growth warning on the Copy",
          not any("used twice" in n for n in (d.get("notes") or [])))
    qc_checks = {c["id"]: c for c in d["qc"]["checks"]}
    print("        QC:", {c["id"]: c["status"] for c in d["qc"]["checks"]})
    mw = qc_checks.get("metric_wiring") or {}
    check("the wiring check warns rather than fails on the absent period",
          mw.get("status") in ("WARN", "PASS"), f"{mw.get('status')} · {mw.get('message')}")
    check("the wiring check names the period column it read",
          (mw.get("detail") or {}).get("period_col") == "Periods",
          str((mw.get("detail") or {}).get("period_col")))
    check("no QC check fails on the Copy",
          d["qc"]["counts"]["FAIL"] == 0,
          str([c["id"] for c in d["qc"]["checks"] if c["status"] == "FAIL"]))
    top = (d["reports"][0].get("manufacturer_top_n") or []) if d.get("reports") else []
    check("the Top-N still ranks on the Copy", len(top) == 10, f"{len(top)} rows")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8796")
    args = ap.parse_args()
    base = args.base

    print("=" * 78)
    print(" Period conventions, live against", base)
    print("=" * 78)

    # ------------------------------------------------------------------ Copy --
    print("\n--- 1. Copy workbook: no Sales Value YA / Sales Value 2YA ----------")
    if os.path.isfile(COPY):
        if not section_copy(base):
            print("\n" + "=" * 78)
            print(" SOME CHECKS FAILED")
            print("=" * 78)
            return 1
    else:
        # The Copy is gitignored (a 28 MB Explorer copy of the reference file), so
        # a fresh checkout will not have it. Skipping is honest; failing would
        # report a missing fixture as a product defect.
        print(f"  [SKIP] {os.path.basename(COPY)} is not present - section 1 skipped")

    # ------------------------------------------------------------------ Long --
    print("\n--- 2. Synthetic workbook: periods as rows, one metric column -----")
    build_long_workbook(LONG_XLSX)
    src2 = post(base, "/api/source/from-path", {"path": LONG_XLSX})
    la = {"source_id": src2["id"], "sheet": "Fact", "header_row": 1,
          "split_column": "Dataset", "value": "Current MAT"}
    lb = {"source_id": src2["id"], "sheet": "Fact", "header_row": 1,
          "split_column": "Dataset", "value": "New MAT"}
    prof2 = post(base, "/api/profile", {"a": la, "b": lb})
    check("the long workbook has a single-column Sales Value family",
          list(prof2["a"]["metric_families"].get("Sales Value", {}).values())
          == ["Sales Value"],
          str(prof2["a"]["metric_families"].get("Sales Value")))
    check("its Periods column carries both MAT YA and MAT TY",
          set(prof2["a"]["column_profiles"][
              [c["name"] for c in prof2["a"]["column_profiles"]].index("Periods")
          ]["samples"]) >= {"MAT YA", "MAT TY"},
          "samples present")

    dim_l = {"category": "CATEGORY", "market": "Display Market Name",
             "manufacturer": "MANUFACTURER", "brand": "BRAND", "period": "Periods"}
    long_req = {
        "a": la, "b": lb,
        "dim_col_a": dim_l, "dim_col_b": dict(dim_l),
        "period_col_a": "Periods", "period_col_b": "Periods",
        "metric_label": "Sales Value",
        "a_prior": "", "a_current": "", "b_prior": "", "b_current": "",
        "is_rate": False,
        "markets": ["TW TOTAL"], "categories": ["CAT_A"], "top_n": 2,
        "client_brands": [], "mapping_a": {}, "mapping_b": {},
        "trend_enabled": False,
    }
    try:
        d2 = post(base, "/api/run", long_req, timeout=600)
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:600]
        check("the long workbook runs", False, f"HTTP {e.code} · {body}")
        print("\n" + "=" * 78)
        print(" SOME CHECKS FAILED")
        print("=" * 78)
        return 1
    rep = d2["reports"][0]
    t = rep["total"]
    check("both periods resolve from the Periods column",
          t["before_prior"] is not None and t["before_current"] is not None,
          f"MAT YA {t['before_prior']} MAT TY {t['before_current']}")
    check("MAT YA is the sum of the MAT YA rows (100 + 50)",
          abs((t["before_prior"] or 0) - 150.0) < 1e-6, str(t["before_prior"]))
    check("MAT TY is the sum of the MAT TY rows (110 + 60)",
          abs((t["before_current"] or 0) - 170.0) < 1e-6, str(t["before_current"]))
    check("AFTER MAT YA is the updated file's MAT YA rows (105 + 55)",
          abs((t["after_prior"] or 0) - 160.0) < 1e-6, str(t["after_prior"]))
    check("AFTER MAT TY is the updated file's MAT TY rows (120 + 65)",
          abs((t["after_current"] or 0) - 185.0) < 1e-6, str(t["after_current"]))
    # 150 -> 170 is +13.33%. A same-column read would report 0%, so this number
    # is what proves the two slots are two different row sets.
    check("growth is the real MAT YA -> MAT TY move (+13.33%), not a 0% collapse",
          t.get("before_growth_pct") is not None
          and abs(t["before_growth_pct"] - 13.3333333) < 0.01,
          str(t.get("before_growth_pct")))
    check("no 'used twice' warning on the long workbook",
          not any("used twice" in n for n in (d2.get("notes") or [])),
          str((d2.get("notes") or [])[:2]))
    ltop = rep.get("manufacturer_top_n") or []
    check("the Top-N is A's largest by MAT TY (M1 then M2)",
          [r["name"] for r in ltop] == ["M1", "M2"],
          str([r["name"] for r in ltop]))
    m1 = next((r for r in ltop if r["name"] == "M1"), {})
    check("a ranked row carries both BEFORE periods",
          abs((m1.get("before_prior") or 0) - 100.0) < 1e-6
          and abs((m1.get("before_current") or 0) - 110.0) < 1e-6,
          f"MAT YA {m1.get('before_prior')} MAT TY {m1.get('before_current')}")
    check("a ranked row carries both AFTER periods",
          abs((m1.get("after_prior") or 0) - 105.0) < 1e-6
          and abs((m1.get("after_current") or 0) - 120.0) < 1e-6,
          f"MAT YA {m1.get('after_prior')} MAT TY {m1.get('after_current')}")

    # -------------------------------------------------- the guard still holds --
    print("\n--- 3. A metric that is in neither dataset is still refused --------")
    bad = dict(long_req)
    bad.update({"metric_label": "Nonexistent Measure",
                "a_prior": "No Such YA", "a_current": "No Such",
                "b_prior": "No Such YA", "b_current": "No Such"})
    try:
        post(base, "/api/run", bad, timeout=300)
        check("a metric the data cannot supply is refused", False, "run was accepted")
    except urllib.error.HTTPError as e:
        detail = json.loads(e.read().decode()).get("detail", {})
        check("a metric the data cannot supply is refused", e.code == 400,
              f"HTTP {e.code} · {detail.get('error')}")
        check("the refusal names the columns it could not find",
              len(detail.get("problems", [])) >= 2,
              " | ".join(detail.get("problems", []))[:140])

    print("\n" + "=" * 78)
    print(" ALL CHECKS PASSED" if ok_all else " SOME CHECKS FAILED")
    print("=" * 78)
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
