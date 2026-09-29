"""Regenerate the demo deliverables against a running server.

Runs the same four API calls the browser makes — profile, dimension mapping,
category mapping, bulk export — so the output on disk always reflects the
current code.

    python run.py --no-browser &
    python tools/make_demo_outputs.py [--categories 10] [--run-name TW_Impact_Study]

Writes to outputs/<run-name>/ : one folder per category containing
<Category>_Impact.xlsx and <Category>_Impact.pptx, plus 00_QC_and_Index.xlsx.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORKBOOK = os.path.join(os.path.dirname(ROOT), "TW Impact Study_V2 1 (1).xlsx")

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(base: str, path: str, payload: dict, timeout: int = 3600) -> dict:
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8777")
    ap.add_argument("--categories", type=int, default=10)
    ap.add_argument("--run-name", default="TW_Impact_Study")
    ap.add_argument("--workbook", default=WORKBOOK)
    args = ap.parse_args()

    if not os.path.isfile(args.workbook):
        print("workbook not found:", args.workbook)
        return 1

    src = post(args.base, "/api/source/from-path", {"path": args.workbook})
    sid = src["id"]
    print(f"source      : {src['filename']} ({len(src['sheets'])} sheets)")

    A = {"source_id": sid, "sheet": "Raw_MAT", "header_row": 1,
         "split_column": "Dataset", "value": "Current MAT"}
    B = {"source_id": sid, "sheet": "Raw_MAT", "header_row": 1,
         "split_column": "Dataset", "value": "New MAT"}
    dims = {"category": "CATEGORY", "market": "Display Market Name",
            "manufacturer": "MANUFACTURER", "brand": "BRAND"}

    # step 2 — profile
    prof = post(args.base, "/api/profile", {"a": A, "b": B})
    print(f"profile     : A {prof['a']['rows']:,} rows / B {prof['b']['rows']:,} rows"
          f" | metrics {len(prof['a']['metrics'])}"
          f" | families {list(prof['a']['metric_families'])}")

    # step 3 — market pairing. The user authors the pairings; the server only
    # enumerates the values on each side and reports hierarchy evidence.
    TOTAL_A = "TW Total TW Offline (G)"
    mk = post(args.base, "/api/market-mapping", {
        "a": A, "b": B,
        "market_col_a": "Display Market Name", "market_col_b": "Display Market Name",
        "metric_col_a": "Sales Value", "metric_col_b": "Sales Value",
        "pairs": [{"market_a": TOTAL_A, "market_b": TOTAL_A, "level": "total"}],
    }, timeout=1800)
    ms = mk["summary"]
    print(f"market      : A {ms['n_a_values']} values / B {ms['n_b_values']} values"
          f" | {ms['n_pairs']} pairing(s) authored"
          f" | hierarchy {mk.get('paths_used')}")
    market_pairs = [{"market_a": TOTAL_A, "market_b": TOTAL_A, "level": "total"}]
    mapping_a = {TOTAL_A: TOTAL_A}

    # step 4 — category enumeration. Nothing is decided for the user; this demo
    # authors identity rows because the reference workbook is already harmonised.
    cm = post(args.base, "/api/category-mapping", {
        "a": A, "b": B,
        "cat_col_a": "CATEGORY", "sub_col_a": "", "metric_col_a": "Sales Value",
        "cat_col_b": "CATEGORY", "sub_col_b": "", "metric_col_b": "Sales Value",
    }, timeout=1800)
    cs = cm["summary"]
    print(f"  category units : A {cs['n_a']} / B {cs['n_b']} "
          f"| mapped by enumeration {cs['mapped']} (must be 0)")
    b_by_cat: dict = {}
    for u in cm["b_units"]:
        b_by_cat.setdefault(u["category"], []).append(u)
    authored = []
    for r in cm["rows"]:
        cand = b_by_cat.get(r["source"]) or []
        authored.append({
            "source": r["source"], "source_sub": r["source_sub"],
            "canonical": r["source"],
            "status": "mapped" if cand else "unmapped",
            "targets": [{"category": u["category"], "subcategory": u["subcategory"]}
                        for u in cand],
        })
    n_mapped = sum(1 for r in authored if r["status"] == "mapped")
    print(f"  category mapping: {n_mapped} of {len(authored)} categories mapped by "
          f"the user, {cs['new_in_b']} new in B, "
          f"coverage A {cs['a_coverage_pct']}% / B {cs['b_coverage_pct']}%")
    category_mapping = {"rows": authored, "new_in_b": cm["new_in_b"]}

    # step 5 — pick the largest categories
    import pandas as pd
    df = pd.read_excel(args.workbook, sheet_name="Raw_MAT", engine="calamine")
    ranked = (df.groupby("CATEGORY")["Sales Value"].sum()
              .sort_values(ascending=False).index.tolist())
    known = {r["source"] for r in cm["rows"]}
    cats = [c for c in ranked if c in known][: args.categories]
    print(f"categories  : {len(cats)} selected")

    # steps 6+7 — export
    d = post(args.base, "/api/export", {
        "a": A, "b": B, "dim_col_a": dims, "dim_col_b": dims,
        "metric_label": "Sales Value",
        "a_prior": "Sales Value YA", "a_current": "Sales Value",
        "b_prior": "Sales Value YA", "b_current": "Sales Value",
        "is_rate": False, "weight_metric_a": "", "weight_metric_b": "",
        "markets": [TOTAL_A], "market_pairs": market_pairs,
        "market_level": "total", "baseline_market": TOTAL_A,
        "categories": cats, "top_n": 10,
        "client_brands": ["WEIDER", "CENTRUM", "BLACKMORES", "DHC"],
        "mapping_a": mapping_a, "mapping_b": {},
        "category_mapping": category_mapping, "trend_enabled": False,
        "run_name": args.run_name, "include_excel": True, "include_pptx": True,
    })
    print(f"\nrun         : {d['rel_dir']}")
    print(f"files       : {len(d['files'])} in {d['elapsed_sec']}s")
    print(f"QC          : {d['qc']['worst']} {d['qc']['counts']}")
    for c in d["qc"]["checks"]:
        if c["status"] != "PASS":
            print(f"   {c['status']} {c['name']}: {c['message'][:100]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
