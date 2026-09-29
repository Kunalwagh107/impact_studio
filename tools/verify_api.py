"""API integration test - drives the live server exactly as the browser does.

    python tools/verify_api.py [--base http://127.0.0.1:8777]

Covers: register source -> profile -> mapping -> run -> bulk export -> verify
artefacts on disk.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# The sandbox routes outbound traffic through a proxy that does not handle
# loopback, so bypass it explicitly for this local test.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(base, path, payload, timeout=900):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def get(base, path, timeout=900):
    with _OPENER.open(base + path, timeout=timeout) as r:
        return json.loads(r.read().decode())


def post_raw(base, path, payload, timeout=900):
    """POST returning (status, body) instead of raising.

    Needed to assert that a removed endpoint now answers 404 - a request that is
    expected to fail cannot go through post(), which raises on any error status.
    """
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:200]}


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# The reference workbook lives in the project root. This previously stepped one
# directory above the root, so every run failed at the first call with a 404 and
# the remaining 39 checks never executed.
WORKBOOK = os.path.join(ROOT, "TW Impact Study_V2 1 (1).xlsx")
if not os.path.isfile(WORKBOOK):
    WORKBOOK = os.path.join(os.path.dirname(ROOT), "TW Impact Study_V2 1 (1).xlsx")
OUTPUTS = os.path.join(ROOT, "outputs")

ok_all = True


def check(name, ok, msg=""):
    global ok_all
    if not ok:
        ok_all = False
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {msg}" if msg else ""))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8777")
    ap.add_argument("--categories", type=int, default=12)
    args = ap.parse_args()
    base = args.base

    print("=" * 78)
    print(" API integration test against", base)
    print("=" * 78)

    h = get(base, "/api/health")
    check("health endpoint", h.get("ok") is True, str(h))

    # --- 1. register the workbook ------------------------------------------
    src = post(base, "/api/source/from-path", {"path": WORKBOOK})
    check("source registered", bool(src.get("id")),
          f"{src.get('filename')} · {len(src.get('sheets', []))} sheets")
    check("Raw_MAT sheet present", "Raw_MAT" in src.get("sheets", []))

    A = {"source_id": src["id"], "sheet": "Raw_MAT", "header_row": 1,
         "split_column": "Dataset", "value": "Current MAT"}
    B = {"source_id": src["id"], "sheet": "Raw_MAT", "header_row": 1,
         "split_column": "Dataset", "value": "New MAT"}

    # --- 2. profile ---------------------------------------------------------
    t0 = time.time()
    prof = post(base, "/api/profile", {"a": A, "b": B})
    check("profile endpoint", prof["a"]["rows"] > 0 and prof["b"]["rows"] > 0,
          f"A={prof['a']['rows']:,} rows  B={prof['b']['rows']:,} rows  "
          f"({time.time()-t0:.1f}s)")
    check("dimensions detected in both",
          all(k in prof["a"]["dimensions"] for k in
              ("category", "market", "manufacturer", "brand")))
    fams = list(prof["a"]["metric_families"])
    check("metric families detected", len(fams) >= 3, str(fams))
    check("dimension value lists returned",
          len(prof["a_dim_values"].get("category", [])) > 100,
          f"{len(prof['a_dim_values'].get('category', []))} categories in A")

    dim_col_a = {"category": "CATEGORY", "market": "Display Market Name",
                 "manufacturer": "MANUFACTURER", "brand": "BRAND"}
    dim_col_b = dict(dim_col_a)

    # --- 3. the removed auto-mapper is gone ---------------------------------
    # It proposed a target for every dimension member and the UI pre-filled its
    # proposals. The brief is that the user defines the mapping, so the endpoint
    # was removed rather than left reachable.
    code, body = post_raw(base, "/api/mapping", {
        "a": A, "b": B, "dim_col_a": dim_col_a, "dim_col_b": dim_col_b,
        "dimensions": ["market"], "fuzzy": True,
    })
    check("the auto-mapping endpoint is gone", code == 404,
          f"HTTP {code}")

    # --- 3b. market pairing: enumerated, not classified ---------------------
    t0 = time.time()
    mk = post(base, "/api/market-mapping", {
        "a": A, "b": B,
        "market_col_a": "Display Market Name",
        "market_col_b": "Display Market Name",
        "metric_col_a": "Sales Value", "metric_col_b": "Sales Value",
    }, timeout=1800)
    ms = mk["summary"]
    check("market-mapping lists the values on both sides",
          ms["n_a_values"] > 0 and ms["n_b_values"] > 0,
          f"A={ms['n_a_values']} B={ms['n_b_values']} ({time.time()-t0:.1f}s)")
    check("no market is paired for the user", ms["n_pairs"] == 0,
          f"{ms['n_pairs']} pair(s) returned unasked")
    print(f"        A values {ms['n_a_values']}  B values {ms['n_b_values']}  "
          f"pairs {ms['n_pairs']}  hierarchy read: {mk.get('paths_used')}")

    # An authored pairing comes back carrying the user's level.
    mk2 = post(base, "/api/market-mapping", {
        "a": A, "b": B,
        "market_col_a": "Display Market Name",
        "market_col_b": "Display Market Name",
        "metric_col_a": "Sales Value", "metric_col_b": "Sales Value",
        "pairs": [{"market_a": "TW Total TW Offline (G)",
                   "market_b": "TW Total TW Offline (G)", "level": "total"}],
    }, timeout=1800)
    check("an authored pairing is returned with the level the user set",
          len(mk2["pairs"]) == 1
          and mk2["pairs"][0]["level"] == "total"
          and mk2["pairs"][0]["market_a"] == "TW Total TW Offline (G)",
          str([(p["market_a"], p["level"]) for p in mk2["pairs"]]))
    check("the pairing carries evidence but no invented pairings",
          len(mk2["pairs"]) == 1 and "evidence" in mk2["pairs"][0],
          f"{len(mk2['pairs'])} pair(s)")

    # --- 3c. category enumeration -------------------------------------------
    t0 = time.time()
    cm = post(base, "/api/category-mapping", {
        "a": A, "b": B,
        "cat_col_a": "CATEGORY", "sub_col_a": "", "metric_col_a": "Sales Value",
        "cat_col_b": "CATEGORY", "sub_col_b": "", "metric_col_b": "Sales Value",
    }, timeout=1800)
    cs = cm["summary"]
    check("category-mapping endpoint", cs["n_a"] > 100, f"({time.time()-t0:.1f}s)")
    print(f"        A units {cs['n_a']}  B units {cs['n_b']}  "
          f"mapped {cs['mapped']}  unmapped {cs['unmapped']}  "
          f"new-in-B {cs['new_in_b']}")
    check("nothing is mapped by the enumeration", cs["mapped"] == 0,
          f"{cs['mapped']} mapped")
    check("no row arrives with a target attached",
          all(not r["targets"] for r in cm["rows"]),
          f"{sum(len(r['targets']) for r in cm['rows'])} targets assigned")
    check("every row starts undecided",
          all(r["status"] == "unmapped" for r in cm["rows"]),
          str(sorted({r["status"] for r in cm["rows"]})))
    check("no automatic match rate is reported", "auto_rate_pct" not in cs,
          "the field is gone")
    check("new categories in B are reported, not dropped", cs["new_in_b"] == 2,
          f"{cs['new_in_b']}")
    check("B units are offered as pickable targets", cs["n_b"] > 100,
          f"{cs['n_b']}")
    check("category rows carry an editable target list and a hint",
          all("targets" in r and "hint" in r for r in cm["rows"]),
          f"{len(cm['rows'])} rows")

    # Build the mapping the user would author here: identity by name, since this
    # workbook is already harmonised. Nothing does this for them.
    b_by_cat = {}
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
    check("an authored mapping can be built for the run", n_mapped > 100,
          f"{n_mapped} of {len(authored)} categories")
    mapping_a = {"market": {}}  # market is paired separately now

    # --- 3d. pre-flight: an unmapped metric must be refused -----------------
    bad = {
        "a": A, "b": B,
        "dim_col_a": dim_col_a, "dim_col_b": dim_col_b,
        "metric_label": "Sales Value",
        "a_prior": "Sales Value YA", "a_current": "Sales Value",
        "b_prior": "", "b_current": "",            # deliberately unmapped
        "is_rate": False, "markets": [], "categories": ["BEER"], "top_n": 10,
        "client_brands": [], "mapping_a": {}, "mapping_b": {},
        "trend_enabled": False,
    }
    try:
        post(base, "/api/run", bad, timeout=300)
        check("unmapped metric is refused with a clear error", False,
              "run was accepted with blank metric columns")
    except urllib.error.HTTPError as e:
        detail = json.loads(e.read().decode()).get("detail", {})
        check("unmapped metric is refused with a clear error", e.code == 400,
              f"HTTP {e.code} · {detail.get('error')}")
        check("the error names the offending columns",
              len(detail.get("problems", [])) >= 2,
              " | ".join(detail.get("problems", []))[:120])

    # --- 4. choose categories ----------------------------------------------
    cat_vals = [r["source"] for r in cm["rows"]]
    df = None
    try:
        import pandas as pd
        df = pd.read_excel(WORKBOOK, sheet_name="Raw_MAT", engine="calamine")
        ranked = (df.groupby("CATEGORY")["Sales Value"].sum()
                  .sort_values(ascending=False).index.tolist())
        chosen = [c for c in ranked if c in cat_vals][: args.categories]
    except Exception:
        chosen = cat_vals[: args.categories]
    check("bulk category selection", len(chosen) == args.categories, str(chosen))

    run_req = {
        "a": A, "b": B,
        "dim_col_a": dim_col_a, "dim_col_b": dim_col_b,
        "metric_label": "Sales Value",
        "a_prior": "Sales Value YA", "a_current": "Sales Value",
        "b_prior": "Sales Value YA", "b_current": "Sales Value",
        "is_rate": False, "weight_metric_a": "", "weight_metric_b": "",
        "markets": [], "categories": chosen, "top_n": 10,
        "client_brands": ["WEIDER", "CENTRUM", "BLACKMORES", "DHC"],
        "mapping_a": mapping_a, "mapping_b": {},
        "category_mapping": {"rows": authored, "new_in_b": cm["new_in_b"]},
        "trend_enabled": False,
    }

    # --- 5. run -------------------------------------------------------------
    t0 = time.time()
    run = post(base, "/api/run", run_req, timeout=1800)
    check("run endpoint", run["n_categories"] == len(chosen),
          f"{run['n_categories']} categories in {time.time()-t0:.1f}s")
    check("QC executed", len(run["qc"]["checks"]) >= 10,
          f"{run['qc']['counts']} worst={run['qc']['worst']}")
    check("no QC failures", run["qc"]["worst"] != "FAIL")

    r0 = run["reports"][0]
    check("first report has all blocks",
          bool(r0.get("channel_block")) and bool(r0.get("brand_block"))
          and bool(r0.get("manufacturer_top_n")) and bool(r0.get("client_brands")))
    print(f"        {r0['category']}: before {r0['total']['before_current']:,.0f} "
          f"-> after {r0['total']['after_current']:,.0f}  "
          f"level shift {r0['total']['level_shift_pp']:+.2f}pp")

    # --- 6. bulk export -----------------------------------------------------
    t0 = time.time()
    exp = post(base, "/api/export", {**run_req, "run_name": "api_bulk_run",
                                     "include_excel": True, "include_pptx": True},
               timeout=3600)
    dt = time.time() - t0
    n_x = len([f for f in exp["files"] if f["kind"] == "excel"])
    n_p = len([f for f in exp["files"] if f["kind"] == "pptx"])
    check("bulk export produced one Excel per category", n_x == len(chosen),
          f"{n_x} xlsx")
    check("bulk export produced one PowerPoint per category", n_p == len(chosen),
          f"{n_p} pptx")
    check("export completeness QC passed",
          next((c["status"] for c in exp["qc"]["checks"]
                if c["id"] == "export_completeness"), "?") == "PASS")
    print(f"        {len(exp['files'])} files in {dt:.1f}s -> {exp['rel_dir']}")

    # --- 7. verify artefacts on disk ---------------------------------------
    missing = [f["rel"] for f in exp["files"]
               if not os.path.isfile(os.path.join(OUTPUTS, f["rel"]))]
    check("every reported file exists on disk", not missing,
          f"{len(missing)} missing" if missing else f"{len(exp['files'])} verified")

    # re-open a couple of them and confirm they are valid OOXML
    import openpyxl
    from pptx import Presentation
    xs = [f for f in exp["files"] if f["kind"] == "excel"][:2]
    ps = [f for f in exp["files"] if f["kind"] == "pptx"][:2]
    for f in xs:
        wb = openpyxl.load_workbook(os.path.join(OUTPUTS, f["rel"]))
        check(f"xlsx opens: {os.path.basename(f['rel'])}", "Summary" in wb.sheetnames,
              f"{len(wb.sheetnames)} sheets")
    for f in ps:
        prs = Presentation(os.path.join(OUTPUTS, f["rel"]))
        check(f"pptx opens: {os.path.basename(f['rel'])}", len(prs.slides) >= 7,
              f"{len(prs.slides)} slides")

    # --- 8. download endpoint ----------------------------------------------
    rel = exp["files"][0]["rel"]
    url = base + "/api/download?rel=" + urllib.parse.quote(rel)
    with _OPENER.open(url, timeout=60) as r:
        body = r.read()
    check("download endpoint serves the file", len(body) > 4000,
          f"{len(body)/1024:.0f} KB")

    # --- 9. session memory does not grow with repeated runs ----------------
    # The reported defect: opening the same workbook several times in one
    # session accumulated a fresh copy of the sheet each time until the
    # process was killed mid-request, surfacing as an opaque 500.
    mem0 = get(base, "/api/memory")
    for _ in range(6):
        again = post(base, "/api/source/from-path", {"path": WORKBOOK})
        post(base, "/api/profile", {"a": A, "b": B})
    mem1 = get(base, "/api/memory")

    check("re-opening the same file reuses one source",
          again.get("reused") is True, f"id={again.get('id')}")
    check("registered sources stay within the cap",
          mem1["n_sources"] <= mem1["max_sources"],
          f"{mem1['n_sources']} of max {mem1['max_sources']}")
    growth = mem1["cached_total_mb"] - mem0["cached_total_mb"]
    check("cached frame size does not grow across re-opens", growth <= 1.0,
          f"{mem0['cached_total_mb']:.1f} MB -> {mem1['cached_total_mb']:.1f} MB")

    # --- 10. a full-breadth export still completes -------------------------
    # 151 categories in one request is the "run multiple impacts" case.
    all_cats = sorted({r["source"] for r in cm["rows"]})
    t1 = time.time()
    big_req = dict(run_req)
    big_req.update({
        "categories": all_cats,
        "client_brands": [],
        "run_name": "verify_all_categories",
        "include_excel": True, "include_pptx": True,
    })
    big = post(base, "/api/export", big_req, timeout=2400)
    dt = time.time() - t1
    check(f"bulk export of all {len(all_cats)} categories succeeds",
          big.get("n_categories") == len(all_cats),
          f"{big.get('n_categories')} categories, {len(big.get('files', []))} files "
          f"in {dt:.0f}s")
    run_dir = os.path.join(OUTPUTS, big["rel_dir"])
    folders = [n for n in os.listdir(run_dir)
               if os.path.isdir(os.path.join(run_dir, n))]
    check("one folder per category, none merged or lost",
          len(folders) == len(all_cats),
          f"{len(folders)} folders for {len(all_cats)} categories")
    missing_big = [f["rel"] for f in big["files"]
                   if not os.path.isfile(os.path.join(OUTPUTS, f["rel"]))]
    check("every bulk file exists on disk", not missing_big,
          f"{len(missing_big)} missing" if missing_big else "all present")

    print("\n" + "=" * 78)
    print(" ALL API CHECKS PASSED" if ok_all else " SOME API CHECKS FAILED")
    print("=" * 78)
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
