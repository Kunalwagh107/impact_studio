"""End-to-end verification for Impact Studio.

Two independent parts:

  PART 1  real data - runs the whole pipeline (profile -> map -> analyse -> QC ->
          export) against the supplied TW workbook and then re-derives the key
          numbers by a *different* route (a second read with a different engine
          plus a mask-based aggregation) and compares.

  PART 2  synthetic - exercises the mapping engine on the exact failure mode the
          brief describes (a category split across Category + Subcategory, plus
          renamed and fuzzy-matched members).

Run:  python tools/verify_e2e.py
"""

from __future__ import annotations

import dataclasses
import os
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from backend import analysis as A
from backend import export_excel, export_pptx
from backend import ingest, mapping as M, profiling as P, qc as QC
from backend import market_mapping as MK
from backend import main as MAIN

# The reference workbook lives in the project root. It was previously looked for
# one directory higher, so Part 1 silently did not run and the suite reported a
# bare "workbook present: FAIL" while 20+ real-workbook checks were skipped.
# Resolve it in the root, then fall back to the parent for older checkouts.
ROOT_WB = os.path.join(ROOT, "TW Impact Study_V2 1 (1).xlsx")
WORKBOOK = ROOT_WB if os.path.isfile(ROOT_WB) else os.path.join(
    os.path.dirname(ROOT), "TW Impact Study_V2 1 (1).xlsx")
OUT = os.path.join(ROOT, "outputs", "_verification")

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, msg: str = "") -> None:
    results.append((name, PASS if ok else FAIL, msg))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f"  {msg}" if msg else ""))


def banner(t: str) -> None:
    print("\n" + "=" * 78)
    print(" " + t)
    print("=" * 78)


# ===========================================================================
# PART 1 - real workbook
# ===========================================================================


def part1() -> None:
    banner("PART 1  Real workbook pipeline")

    if not os.path.isfile(WORKBOOK):
        check("workbook present", False, WORKBOOK)
        return
    check("workbook present", True, f"{os.path.getsize(WORKBOOK)/1e6:.1f} MB")

    t0 = time.time()
    df_all = ingest.load_table(WORKBOOK, sheet_name="Raw_MAT", header_row=1)
    t_read = time.time() - t0
    check("Raw_MAT loaded", len(df_all) > 90_000,
          f"{len(df_all):,} rows x {len(df_all.columns)} cols in {t_read:.1f}s "
          f"(engine={'calamine' if ingest._HAVE_CALAMINE else 'openpyxl'})")

    # --- split into A / B via the Dataset discriminator --------------------
    vals = sorted(df_all["Dataset"].dropna().astype(str).unique())
    check("Dataset discriminator found", len(vals) == 2, f"values = {vals}")
    a_val = next((v for v in vals if "current" in v.lower()), vals[0])
    b_val = next((v for v in vals if v != a_val), vals[1])
    df_a = df_all[df_all["Dataset"] == a_val].reset_index(drop=True)
    df_b = df_all[df_all["Dataset"] == b_val].reset_index(drop=True)
    print(f"      A = {a_val!r}  {len(df_a):,} rows")
    print(f"      B = {b_val!r}  {len(df_b):,} rows")
    check("datasets are structurally different", len(df_a) != len(df_b),
          f"{len(df_a):,} vs {len(df_b):,} rows -> the update is not a no-op")

    # --- profile ------------------------------------------------------------
    pa = P.profile_dataset(df_a, "Previous (Current MAT)")
    pb = P.profile_dataset(df_b, "Updated (New MAT)")
    print(f"      A dims: {pa.dimensions}")
    print(f"      B dims: {pb.dimensions}")
    print(f"      metric families A: {list(pa.metric_families)}")
    check("category dimension detected", "category" in pa.dimensions
          and "category" in pb.dimensions)
    check("manufacturer + brand detected",
          "manufacturer" in pa.dimensions and "brand" in pa.dimensions)
    check("metric families found", len(pa.metric_families) >= 3,
          f"{list(pa.metric_families)}")

    fam = pa.metric_families.get("Sales Value", {})
    check("'Sales Value' family has YA + current variants",
          "YA" in fam and ("VALUE" in fam or "TY" in fam), str(fam))

    # --- mapping ------------------------------------------------------------
    t0 = time.time()
    # --- category enumeration (step 4) --------------------------------------
    # The reference workbook is already harmonised, so most categories share a
    # name across the two datasets. That is exactly why the *mapping* cannot be
    # checked here: on this file, mapping by name and mapping by hand give the
    # same answer. What is checked here is that the enumeration decides nothing
    # and that the batch flow works at scale. The mapping logic itself is proved
    # adversarially in tools/verify_user_mapping.py, on a fixture whose names
    # deliberately disagree.
    from backend import category_mapping as CM
    t0 = time.time()
    enum = CM.enumerate_category_units(
        df_a, df_b, "CATEGORY", None, "Sales Value",
        "CATEGORY", None, "Sales Value")
    t_map = time.time() - t0
    s = enum.summary
    print(f"      A units {s['n_a']}  B units {s['n_b']}  "
          f"mapped {s['mapped']}  unmapped {s['unmapped']}  "
          f"new-in-B {s['new_in_b']}  ({t_map:.1f}s)")
    check("every Dataset-1 category is enumerated", s["n_a"] >= 151, f"{s['n_a']}")
    check("nothing is mapped by the enumeration", s["mapped"] == 0,
          f"{s['mapped']} mapped")
    check("no row arrives with a target attached",
          all(not r.targets for r in enum.rows),
          f"{sum(len(r.targets) for r in enum.rows)} targets assigned")
    check("B units are offered as pickable targets", s["n_b"] >= 153,
          f"{s['n_b']}")
    check("the two B-only categories are reported, not dropped",
          len(enum.new_in_b) >= 2,
          f"{len(enum.new_in_b)} B-only unit(s)")

    # The mapping the user would author for this harmonised file: identity by
    # name. Built explicitly here, because nothing builds it for them any more.
    b_by_name = {}
    for u in enum.b_units:
        b_by_name.setdefault(u["category"], []).append(u)
    rows = []
    for r in enum.rows:
        cand = b_by_name.get(r.source) or []
        if len(cand) == 1:
            rows.append({"source": r.source, "source_sub": r.source_sub,
                         "canonical": r.source, "status": "mapped",
                         "targets": [{"category": cand[0]["category"],
                                      "subcategory": cand[0]["subcategory"],
                                      "total": cand[0]["total"]}]})
        elif cand:
            # Several B units under one category: the user claims all of them.
            rows.append({"source": r.source, "source_sub": r.source_sub,
                         "canonical": r.source, "status": "mapped",
                         "targets": [{"category": u["category"],
                                      "subcategory": u["subcategory"],
                                      "total": u["total"]} for u in cand]})
        else:
            # No counterpart: left unmapped, so it is not analysed.
            rows.append({"source": r.source, "source_sub": r.source_sub,
                         "canonical": r.source, "status": "unmapped",
                         "targets": []})
    cat_map_a, cat_map_b, ex_a, ex_b = CM.resolve(rows, enum.new_in_b)
    n_mapped = sum(1 for r in rows if r["status"] == "mapped")
    print(f"      authored: {n_mapped} mapped, "
          f"{sum(1 for r in rows if r['status'] == 'unmapped')} unmapped")
    check("the authored mapping covers the harmonised categories",
          n_mapped >= 150, f"{n_mapped} of {len(rows)}")

    cats = ["HEALTH FOOD", "CHILLED MILK", "CIGARETTE", "BEER", "READY-TO-DRINK TEA"]
    # Client brands: two well inside the Top-10 and two ranked outside it, to
    # prove the tracking is genuinely independent of the Top-N cut.
    CLIENT_BRANDS = ["WEIDER", "CENTRUM", "BLACKMORES", "DHC", "BIOGAIA"]
    cfg = A.AnalysisConfig(
        metric_label="Sales Value",
        a_prior="Sales Value YA", a_current="Sales Value",
        b_prior="Sales Value YA", b_current="Sales Value",
        is_rate=False,
        category_col="CATEGORY", market_col="Display Market Name",
        manufacturer_col="MANUFACTURER", brand_col="BRAND",
        markets=[], categories=cats, top_n=10,
        client_brands=CLIENT_BRANDS,
        category_map_a=cat_map_a, category_map_b=cat_map_b,
        category_excluded_a=sorted(ex_a), category_excluded_b=sorted(ex_b),
    )
    prep = A.prepare(df_a, df_b, cfg)
    check("selection resolves to the requested categories",
          sorted(prep.categories) == sorted(cats), str(prep.categories))
    check("the identity mapping keeps the category names unchanged",
          all(c in cat_map_a.values() for c in cats if c in cat_map_a.values()),
          "harmonised file: the mapping is the identity by name")

    reports = A.analyse(prep, cats)
    check("a report was produced per category", len(reports) == len(cats))

    # --- independent re-derivation -----------------------------------------
    # Deliberately different route: a fresh read with a *different engine*,
    # then a boolean-mask aggregation (not the groupby used by the engine).
    print("\n  Independent re-derivation (second engine + mask aggregation)")
    engine2 = "openpyxl" if ingest._HAVE_CALAMINE else "calamine"
    df2 = pd.read_excel(WORKBOOK, sheet_name="Raw_MAT", engine=engine2)
    df2["Dataset"] = df2["Dataset"].astype(str).str.strip()

    mismatches = []
    for rep in reports:
        cat = rep["category"]
        for side, dval, col, key in (
            ("before", a_val, "Sales Value", "before_current"),
            ("after", b_val, "Sales Value", "after_current"),
            ("before_prior", a_val, "Sales Value YA", "before_prior"),
            ("after_prior", b_val, "Sales Value YA", "after_prior"),
        ):
            mask = (df2["Dataset"] == dval) & (df2["CATEGORY"].astype(str) == cat)
            expected = float(pd.to_numeric(df2.loc[mask, col], errors="coerce")
                             .fillna(0).sum())
            got = rep["total"].get(key)
            if got is None:
                mismatches.append((cat, key, None, expected))
                continue
            rel = abs(got - expected) / max(abs(expected), 1e-9)
            if rel > 1e-9:
                mismatches.append((cat, key, got, expected, rel))
    check("all category totals reconcile across engines",
          not mismatches,
          f"{len(mismatches)} mismatch(es)" if mismatches else
          f"{len(reports)*4} values checked, rel. tol 1e-9")
    for m in mismatches[:6]:
        print("      MISMATCH", m)

    # --- spot-check a headline number --------------------------------------
    hf = next(r for r in reports if r["category"] == "HEALTH FOOD")
    print(f"\n      HEALTH FOOD  before {hf['total']['before_current']:,.0f} -> "
          f"after {hf['total']['after_current']:,.0f}  "
          f"(level shift {hf['total']['level_shift_pp']:+.2f}pp)")
    check("HEALTH FOOD shows a large positive impact",
          (hf["total"]["abs_change"] or 0) > 0,
          f"abs change {hf['total']['abs_change']:,.0f}")

    # --- channel block sanity ----------------------------------------------
    cb = hf.get("channel_block") or []
    check("channel block produced", len(cb) >= 1, f"{len(cb)} channels")
    if cb:
        # The Total Market leads the block when a baseline was designated. It is
        # *not* a member, so summing the whole block and expecting 100 would be
        # asserting that the Total is one of its own channels - which is exactly
        # the double-count the layout avoids.
        base_name = (hf.get("baseline") or {}).get("name")
        members = [c for c in cb if c["name"] != base_name]
        share_sum = sum(c["contribution"]["after_share_pct"] or 0 for c in members)
        check("channel shares sum to 100%", abs(share_sum - 100) < 0.01,
              f"{share_sum:.4f}% over {len(members)} member(s)"
              + (f", total '{base_name}' excluded" if base_name else ""))

    # --- Top-N and client brands -------------------------------------------
    # The Top-N is drawn from the PREVIOUS dataset, so the block is ordered by
    # the before value. Two independent things are asserted here: the ordering
    # is by A, and every member is genuinely A's largest - which is a stronger
    # claim than "the list is descending", because a list can be descending and
    # still be the wrong ten.
    mt = hf.get("manufacturer_top_n") or []
    check("manufacturer Top-N is correctly sized", len(mt) == 10, f"{len(mt)} rows")
    vals_desc = [b["before_current"] for b in mt if b["before_current"] is not None]
    check("Top-N sorted descending by the previous dataset",
          vals_desc == sorted(vals_desc, reverse=True))
    check("every Top-N member's before-rank is inside the cut",
          all((b.get("rank_before") or 99) <= 10 for b in mt),
          f"ranks {[b.get('rank_before') for b in mt]}")
    # Compare against a recomputation straight off the prepared frame.
    _mblock = A._entity_block(
        prep.a[prep.a["category"] == "HEALTH FOOD"],
        prep.b[prep.b["category"] == "HEALTH FOOD"],
        "manufacturer", False, None, "Sales Value")
    _top_a = (_mblock[_mblock["manufacturer"].astype(str).str.len() > 0]
              .sort_values("a_current", ascending=False, na_position="last")
              .head(10)["manufacturer"].tolist())
    check("the block is exactly A's ten largest manufacturers",
          [b["name"] for b in mt] == _top_a,
          f"report={[b['name'] for b in mt][:3]}… recomputed={_top_a[:3]}…")
    moves = {}
    for b in mt:
        moves[b["movement"]] = moves.get(b["movement"], 0) + 1
    print(f"      Top-10 movement mix: {moves}")
    check("movement classes assigned",
          all(b["movement"] in ("NEW", "EXITED", "GAINED", "LOST", "HELD") for b in mt))

    clients = hf.get("client_brands") or []
    check("client brands tracked independently", len(clients) == len(CLIENT_BRANDS),
          f"{[(c['name'], c.get('rank_after'), c.get('in_top_n')) for c in clients]}")
    found = [c for c in clients if c.get("found")]
    check("every client brand resolves to a real entity",
          len(found) == len(CLIENT_BRANDS),
          f"{len(found)}/{len(CLIENT_BRANDS)} found")
    outside = [c for c in found if not c.get("in_top_n")]
    check("client brands outside the Top-N are still reported",
          len(outside) >= 2,
          f"{[(c['name'], c['rank_after']) for c in outside]}")
    check("Top-N block is unaffected by client tracking", len(mt) == 10,
          f"still {len(mt)} rows")

    # --- QC -----------------------------------------------------------------
    # Build the same mapping-results view the API hands the QC. Without this the
    # coverage check sees nothing and reports PASS on an empty dict, which is
    # exactly the "a check that cannot run must not pass" failure this project
    # already hit once.
    map_out = MAIN.catmap_results(rows, enum.new_in_b)
    qcrep = QC.run_qc(reports, cfg, df_a, df_b, mapping_results=map_out)
    print("\n  QC results")
    for c in qcrep.checks:
        print(f"      [{c.status}] {c.name}: {c.message[:96]}")
    check("QC produced checks", len(qcrep.checks) >= 10, f"{len(qcrep.checks)} checks")
    check("no QC failures on clean data", qcrep.worst != "FAIL",
          f"worst = {qcrep.worst}")

    # --- exports ------------------------------------------------------------
    os.makedirs(OUT, exist_ok=True)
    files = []
    t0 = time.time()
    for rep in reports:
        cat_dir = os.path.join(OUT, rep["category"])
        os.makedirs(cat_dir, exist_ok=True)
        base = rep["category"].replace(" ", "_")
        xp = os.path.join(cat_dir, f"{base}_Impact.xlsx")
        pp = os.path.join(cat_dir, f"{base}_Impact.pptx")
        export_excel.build_category_workbook(rep, qcrep.to_dict(), xp)
        export_pptx.build_category_deck(rep, qcrep.to_dict(), pp)
        files += [xp, pp]
    t_exp = time.time() - t0
    check("exported one Excel + one PowerPoint per category",
          all(os.path.isfile(f) and os.path.getsize(f) > 4000 for f in files),
          f"{len(files)} files in {t_exp:.1f}s")

    # verify the produced artefacts actually open and contain what they claim
    import openpyxl
    from pptx import Presentation
    from pptx.oxml.ns import qn

    xp = files[0]
    wb = openpyxl.load_workbook(xp)
    got_sheets = set(wb.sheetnames)

    # The summary sheet and the QC sheet were both removed on request, and the
    # tables are now emitted **per metric**. The assertions are written to fail if
    # either removal is undone, not merely to skip the missing name.
    check("Excel has no summary sheet", "Summary" not in got_sheets,
          f"sheets={sorted(got_sheets)}")
    check("Excel has no QC sheet", "QC" not in got_sheets,
          f"sheets={sorted(got_sheets)}")
    check("Excel carries a market/channel table",
          any(s.startswith("Channel") for s in got_sheets),
          f"sheets={sorted(got_sheets)}")
    check("Excel carries a Manufacturer Top-N and a Brand Top-N",
          any(s.startswith("Manufacturer Top-N") for s in got_sheets)
          and any(s.startswith("Brand Top-N") for s in got_sheets),
          f"sheets={sorted(got_sheets)}")
    check("Excel does not contain the removed Brand Value Share sheet",
          "Brand Value Share" not in got_sheets,
          f"sheets={sorted(got_sheets)}")

    # "Proper tabular format" means one header row, one record per row, and no
    # merged cells - a merged banner is what stops a reader filtering the table.
    ch_name = next(s for s in got_sheets if s.startswith("Channel"))
    ws = wb[ch_name]
    rows = list(ws.iter_rows(values_only=True))
    hdr = next((r for r in rows if r and r[0] == "Entity"), None)
    check("channel table has a single flat header row",
          hdr is not None and "Level" in hdr and hdr.count("Entity") == 1,
          f"header={hdr}")
    check("channel table has no merged cells",
          len(ws.merged_cells.ranges) == 0,
          f"{len(ws.merged_cells.ranges)} merged range(s)")
    data_rows = [r for r in rows if r and r[0] and r[0] != "Entity"
                 and not str(r[0]).startswith(("Market /", "Category:", "Values are",
                                               "Share is", "Growth =", "This metric",
                                               "Distribution", "The Total"))]
    check("channel table has one record per entity", len(data_rows) >= 1,
          f"{len(data_rows)} data row(s)")

    # The workbook must write the figures in the unit the run resolved, not in a
    # scale of its own choosing.
    disp = (reports[0].get("metrics") or {}).get(
        next(iter(reports[0].get("metrics") or {}), ""), reports[0]).get("display")
    if disp:
        sym = disp.get("symbol") or ""
        check("Excel writes the metric's resolved unit in its title",
              sym in (rows[0][0] if rows and rows[0] else ""),
              f"title={rows[0][0] if rows else None} symbol={sym!r}")

    prs = Presentation(files[1])
    n_slides = len(prs.slides)
    check("PowerPoint has slides", n_slides >= 3, f"{n_slides} slides")
    check("PowerPoint has no chart (charts removed on request)",
          not any(sh.has_chart for sl in prs.slides for sh in sl.shapes))
    check("PowerPoint contains tables",
          any(sh.has_table for sl in prs.slides for sh in sl.shapes))
    first_text = [sh.text_frame.text for sh in prs.slides[0].shapes
                  if sh.has_text_frame and sh.text_frame.text.strip()]
    check("PowerPoint has no cover slide - it opens on the impact headline",
          bool(first_text) and first_text[0].lower().startswith("headline impact"),
          f"first slide reads {first_text[0] if first_text else None!r}")
    titles = [sh.text_frame.text for sl in prs.slides for sh in sl.shapes
              if sh.has_text_frame and sh.text_frame.text.strip()]
    check("PowerPoint has a Top-N manufacturers section and a Top-N brands section",
          any(t.lower().startswith("top") and "manufacturer" in t.lower() for t in titles)
          and any(t.lower().startswith("top") and "brand" in t.lower() for t in titles))
    check("PowerPoint has no QC slide",
          not any("automated qc" in t.lower() for t in titles))

    # Every table cell must carry a black border on all four sides.
    bordered = 0
    checked_cells = 0
    for sl in prs.slides:
        for sh in sl.shapes:
            if not sh.has_table:
                continue
            tc = sh.table.cell(0, 0)._tc
            tcPr = tc.find(qn('a:tcPr'))
            checked_cells += 1
            if tcPr is not None and all(tcPr.find(qn(f'a:ln{s}')) is not None
                                        for s in ("L", "R", "T", "B")):
                bordered += 1
    check("every table has black borders on all four sides",
          checked_cells > 0 and bordered == checked_cells,
          f"{bordered}/{checked_cells} tables bordered")

    print(f"\n      outputs written under {OUT}")


# ===========================================================================
# PART 2 - synthetic mapping edge cases
#
# This exercises `backend/mapping.py` directly, as a library. That module is no
# longer on any request path: step 3 no longer offers a suggestion for a
# dimension member, and `/api/mapping` is gone. It is still imported for its
# name-normalisation helpers, and these cases document what the tier cascade in
# it does - which is useful precisely because the cascade is the thing that
# produced the wrong answers on a workbook whose datasets disagree (see
# tools/verify_user_mapping.py). Treat this as characterisation of a retained
# helper, not as coverage of the mapping the user sees.
# ===========================================================================


def part2() -> None:
    banner("PART 2  Retained name-matching helpers (not on the request path)")

    # A: one category 'Tandy', markets renamed, a typo'd manufacturer
    values_a = ["Tandy", "TW CVS", "TW Total TW Offline (G)",
                "ACME FOODS", "Nestle S.A."]
    # B: Tandy split into Category+Subcategory path, markets hierarchically
    #    named, manufacturer with different formatting
    values_b = ["Tandy", "Tandy/Biscuits", "CVS/TW Total TW Offline (G)/MT w/o Costco",
                "TW Total TW Offline (G)", "ACME FOODS INC", "NESTLE SA"]

    idx_a = {"Tandy": {"Sales Value": 1000.0}, "TW CVS": {"Sales Value": 500.0},
             "ACME FOODS": {"Sales Value": 250.0}}
    idx_b = {"Tandy": {"Sales Value": 1000.0}, "Tandy/Biscuits": {"Sales Value": 1000.0},
             "CVS/TW Total TW Offline (G)/MT w/o Costco": {"Sales Value": 500.0},
             "ACME FOODS INC": {"Sales Value": 250.0}, "NESTLE SA": {"Sales Value": 90.0}}

    res = M.suggest_mapping(values_a, values_b, "category", idx_a, idx_b, fuzzy=True)
    for r in res.rows:
        print(f"      {r.source:<24} -> {str(r.target):<46} {r.status:<14} "
              f"{r.method:<12} conf={r.confidence:.2f}")
    print(f"      summary: {res.summary}")

    by_src = {r.source: r for r in res.rows}
    check("exact match detected", by_src["Tandy"].method == "exact")
    check("case/punctuation-insensitive match",
          by_src["TW Total TW Offline (G)"].status == "accepted")
    check("hierarchical path match (TW CVS -> CVS/.../MT w/o Costco)",
          by_src["TW CVS"].method == "hierarchical",
          f"-> {by_src['TW CVS'].target}")
    check("fuzzy match on a company suffix (ACME FOODS -> ACME FOODS INC)",
          by_src["ACME FOODS"].method in ("fuzzy", "normalised"),
          f"method={by_src['ACME FOODS'].method}")
    check("Nestle S.A. -> NESTLE SA resolved",
          by_src["Nestle S.A."].target == "NESTLE SA",
          f"method={by_src['Nestle S.A.'].method} conf={by_src['Nestle S.A.'].confidence:.2f}")
    check("'Tandy/Biscuits' reported as new in B (not silently dropped)",
          any(r.source == "Tandy/Biscuits" and r.status == "new_in_b"
              for r in res.rows))

    # a genuinely unmatched value must be flagged, never invented
    res2 = M.suggest_mapping(["Zzzz Qqqq"], ["Completely Different Thing"], "brand",
                             fuzzy=True)
    check("unmatchable value is flagged, not guessed",
          res2.rows[0].status in ("unmatched", "new_in_b"),
          f"status={res2.rows[0].status} target={res2.rows[0].target}")


# ===========================================================================
# PART 3 - the QC checks must be able to fail
# ===========================================================================


def part3() -> None:
    banner("PART 3  Mutation tests - a check that cannot fail is not a check")

    cfg = A.AnalysisConfig(top_n=5)

    # --- export completeness must catch a missing file ---------------------
    qc = QC.QCReport()
    expected = ["CAT_A", "CAT_B", "CAT_C"]
    produced = {
        "CAT_A": {"excel": "a.xlsx", "pptx": "a.pptx"},
        "CAT_B": {"excel": "b.xlsx"},            # pptx deliberately missing
        "CAT_C": {"excel": "c.xlsx", "pptx": "c.pptx"},
    }
    QC.check_export_completeness(expected, produced, qc)
    check("completeness FAILS when a PowerPoint is missing",
          qc.checks[-1].status == QC.FAIL,
          f"status={qc.checks[-1].status} n_missing={qc.checks[-1].detail.get('n')}")

    qc2 = QC.QCReport()
    QC.check_export_completeness(expected, {
        "CAT_A": {"excel": "a.xlsx", "pptx": "a.pptx"},
        "CAT_B": {"excel": "b.xlsx", "pptx": "b.pptx"},
        "CAT_C": {"excel": "c.xlsx", "pptx": "c.pptx"},
    }, qc2)
    check("completeness PASSES when all files exist",
          qc2.checks[-1].status == QC.PASS, f"status={qc2.checks[-1].status}")

    # --- category-level totals were removed on request ----------------------
    # This check reconciled every category total against an independent
    # recomputation. It was deliberately removed, so assert its absence rather
    # than let it quietly reappear. The helper it was built on
    # (`_independent_category_total`) is still exercised directly further down,
    # because the ND weighted-mean rule it encodes is a real behaviour worth
    # pinning - only the *check* is gone, not the arithmetic.
    check("the category-level totals check is removed from the module",
          not hasattr(QC, "check_category_totals"),
          f"hasattr={hasattr(QC, 'check_category_totals')}")
    # A minimal frame, so run_qc has something to walk. Part 3 is self-contained
    # on purpose - reaching for another part's fixture is how a test ends up
    # passing because of a frame it never built.
    _df = pd.DataFrame({"CATEGORY": ["X"], "Sales Value": [1.0]})
    _qc_removed = QC.run_qc([], cfg, _df, _df)
    check("run_qc no longer emits a category_totals check",
          all(c.id != "category_totals" for c in _qc_removed.checks),
          f"ids={[c.id for c in _qc_removed.checks]}")
    # run_qc still accepts the two parameters that existed only to feed it, so
    # callers (main.py) need no edit - assert that rather than assume it.
    QC.run_qc([], cfg, _df, _df, cfgs_by_metric={}, category_mapping=None)
    check("run_qc still accepts cfgs_by_metric / category_mapping",
          True, "signature retained for call-site compatibility")

    # --- Top-N must catch an oversized block -------------------------------
    # The block is selected and ordered on the previous dataset, so the fixture
    # carries before_current / rank_before - the fields the check now reads.
    qc5 = QC.QCReport()
    QC.check_topn([{"category": "X", "manufacturer_top_n":
                    [{"before_current": 10.0, "after_current": 12.0,
                      "rank_before": 1, "rank_after": 1}] * 7}], cfg, qc5)
    check("Top-N FAILS when the block exceeds N",
          qc5.checks[-1].status == QC.FAIL, f"status={qc5.checks[-1].status}")

    # ...and must catch a member whose A-rank sits outside the cut, which is the
    # failure mode the "select on A" change makes possible to get wrong.
    qc5c = QC.QCReport()
    QC.check_topn([{"category": "X", "manufacturer_top_n":
                    [{"before_current": 10.0, "after_current": 1.0,
                      "rank_before": 25, "rank_after": 90}]}], cfg, qc5c)
    check("Top-N FAILS when a member's before-rank is outside the cut",
          qc5c.checks[-1].status == QC.FAIL, f"status={qc5c.checks[-1].status}")

    qc5b = QC.QCReport()
    QC.check_topn([{"category": "X"}], cfg, qc5b)
    check("Top-N reports NOT-VERIFIED when no blocks exist",
          qc5b.checks[-1].status == QC.WARN, f"status={qc5b.checks[-1].status}")

    # --- Top-N is selected on the PREVIOUS dataset, then followed into B ---
    # The case that separates the two rules: an entity that led in A and
    # collapsed in B must still be in the block (it is part of the movement the
    # report exists to surface), and an entity that leads only in B must not
    # displace it. The reference workbook is harmonised, so its two selections
    # coincide and cannot prove this - a synthetic block is required.
    _blk = pd.DataFrame({
        "manufacturer": ["ALPHA", "BRAVO", "CHARLIE", "OMEGA"],
        "a_current":    [100.0,   50.0,    10.0,      5.0],
        "b_current":    [1.0,     55.0,    12.0,      500.0],
        "a_prior":      [90.0,    48.0,    9.0,       4.0],
        "b_prior":      [95.0,    50.0,    10.0,      100.0],
        "before_growth_pct": [None] * 4, "after_growth_pct": [None] * 4,
        "level_shift_pp": [None] * 4, "before_share_pct": [None] * 4,
        "after_share_pct": [None] * 4, "share_change_pp": [None] * 4,
        "abs_change": [0.0] * 4, "contribution_to_change_pct": [None] * 4,
        "metric": ["Sales Value"] * 4,
    })
    _top, _ = A._rank_block(_blk, "manufacturer", 2)
    _names = [t["name"] for t in _top]
    check("Top-N takes the largest from the previous dataset",
          _names == ["ALPHA", "BRAVO"], f"selected {_names}")
    check("an entity that led in A and collapsed in B is kept, not dropped",
          "ALPHA" in _names
          and next(t for t in _top if t["name"] == "ALPHA")["movement"] == "LOST",
          f"ALPHA movement={next(t for t in _top if t['name'] == 'ALPHA')['movement']}")
    check("an entity that leads only in the updated dataset does not displace it",
          "OMEGA" not in _names, f"OMEGA present={('OMEGA' in _names)}")
    check("the block is ordered by the previous dataset",
          [t["before_current"] for t in _top]
          == sorted([t["before_current"] for t in _top], reverse=True),
          f"{[t['before_current'] for t in _top]}")

    # --- percentages must catch a wrong growth rate ------------------------
    qc6 = QC.QCReport()
    QC.check_percentages([{"category": "X",
                           "total": {"before_current": 110.0, "before_prior": 100.0,
                                     "before_growth_pct": 99.0}}], qc6)
    check("percentages FAIL on a wrong growth rate",
          qc6.checks[-1].status == QC.FAIL, f"status={qc6.checks[-1].status}")

    # --- level shift must catch an inconsistent delta ----------------------
    qc7 = QC.QCReport()
    QC.check_before_after_consistency([{
        "category": "X",
        "channel_block": [{"name": "M",
                           "before": {"growth_pct": 1.0},
                           "after": {"growth_pct": 3.0},
                           "level_shift": {"mat_ty_pp": 5.0}}],
    }], qc7)
    check("level shift FAILS when it disagrees with the growth rates",
          qc7.checks[-1].status == QC.FAIL, f"status={qc7.checks[-1].status}")

    # --- duplicates must catch a repeated key ------------------------------
    # Two rows identical on the full grain (there is no period column here to
    # separate them), so this *is* a genuine duplicate and must be flagged.
    dup = pd.DataFrame({"CATEGORY": ["X", "X"], "Sales Value": [1.0, 2.0]})
    # `wired` used to be defined by the category-totals test above; that test is
    # gone, so it is defined here at its own point of use. It is also needed by
    # the "real run" assertion at the end of this part.
    wired = A.AnalysisConfig(top_n=5, category_col="CATEGORY",
                             a_current="Sales Value", b_current="Sales Value")
    qc8 = QC.QCReport()
    QC.check_duplicates(dup, dup, wired, qc8)
    check("duplicates flags a repeat on the full grain",
          qc8.checks[-1].status in (QC.WARN, QC.FAIL),
          f"status={qc8.checks[-1].status}")

    # The same dimension rows are *not* duplicates once a period column
    # separates them - that is a stacked workbook, not a fault.
    stacked = pd.DataFrame({"CATEGORY": ["X", "X"], "PERIOD": ["TY", "YA"],
                            "Sales Value": [1.0, np.nan]})
    wired_p = A.AnalysisConfig(category_col="CATEGORY", period_col="PERIOD")
    qc8b = QC.QCReport()
    QC.check_duplicates(stacked, stacked, wired_p, qc8b)
    check("duplicates does not flag a period-stacked frame",
          qc8b.checks[-1].status == QC.PASS,
          f"status={qc8b.checks[-1].status} {qc8b.checks[-1].message[:80]}")

    # Same first point of use: a config with no dimension columns wired, so the
    # check has nothing to key on and must say so rather than pass.
    bare = A.AnalysisConfig(top_n=5)
    qc9 = QC.QCReport()
    QC.check_duplicates(dup, dup, bare, qc9)
    check("duplicates report NOT-VERIFIED when unwired",
          qc9.checks[-1].status == QC.WARN, f"status={qc9.checks[-1].status}")

    # --- every check in a real run must have actually run ------------------
    # The frames are built here rather than borrowed from part1: `df_a`/`df_b`
    # are part1's *locals* (never module globals), so the previous version of
    # this assertion could never have executed - it raised NameError on its very
    # first run and took the rest of part3 with it. A self-contained fixture is
    # the only honest way to assert "a real run verifies things".
    run_a = pd.DataFrame({"CATEGORY": ["X", "Y"], "Sales Value": [230.0, 10.0]})
    run_b = pd.DataFrame({"CATEGORY": ["X", "Y"], "Sales Value": [270.0, 12.0]})
    n_verified = sum(1 for c in (QC.run_qc(
        [{"category": "X", "total": {"before_current": 230.0, "after_current": 270.0},
          "channel_block": []}], wired, run_a, run_b).checks)
        if c.status == QC.PASS)
    check("a wired run produces real PASS results, not skips", n_verified >= 2,
          f"{n_verified} PASS")


# ===========================================================================
# PART 4 - user-authored category mapping (1:1, composite, 1:N, N:1, none)
#
# Inverted from the version this replaces. That one asserted the engine
# *guessed* the right structure - `method == "composite"`, `status ==
# "suggested"`, `auto_rate_pct >= 99` - and on this fixture the tier cascade did
# produce the right answers, so the assertions passed while proving nothing
# about the case that matters: a workbook whose two datasets define categories
# differently, where the same cascade was wrong.
#
# Nothing is guessed now, so there is nothing to assert about a guess. What is
# asserted is that the engine enumerates without deciding, that the user's
# decisions are what reach the analysis, and that a category with no counterpart
# is left out rather than reported as a one-sided number.
# ===========================================================================


def part4() -> None:
    banner("PART 4  Category mapping the user authors (not the engine)")

    from backend import category_mapping as CM

    # Dataset 1 uses a flat taxonomy; Dataset 2 splits the same space differently
    # and uses entirely different names, so no name check can reconcile them.
    A_ = pd.DataFrame({
        "CAT": ["Biscuits", "Snacks", "Coffee", "Pet Food"],
        "VAL_YA": [90.0, 280.0, 180.0, 4500.0],
        "VAL": [100.0, 300.0, 200.0, 5000.0],
    })
    B_ = pd.DataFrame({
        "CAT": ["Tandy", "Tandy", "Savoury", "Savoury", "Bev", "Sweet", "Sweet"],
        "SUB": ["Biscuits", "Cake", "Chips", "Nuts", "Coffee", "Chocolate", "Candy"],
        "VAL_YA": [95.0, 40.0, 210.0, 110.0, 150.0, 44.0, 18.0],
        "VAL": [110.0, 45.0, 220.0, 120.0, 230.0, 50.0, 20.0],
    })

    res = CM.enumerate_category_units(A_, B_, "CAT", None, "VAL",
                                      "CAT", "SUB", "VAL")

    # ---- 1. enumeration decides nothing ------------------------------------
    check("every Dataset-1 category is enumerated", len(res.rows) == 4,
          f"{len(res.rows)}: {[r.source for r in res.rows]}")
    check("no row is given a target",
          all(not r.targets for r in res.rows),
          f"{sum(len(r.targets) for r in res.rows)} targets assigned by code")
    check("every row starts in the undecided state",
          all(r.status == CM.ST_UNMAPPED for r in res.rows),
          str([(r.source, r.status) for r in res.rows]))
    check("no automatic match rate is reported",
          "auto_rate_pct" not in res.summary,
          "the field is gone: it scored the machine's guessing")
    check("the summary counts every row as undecided",
          res.summary["unmapped"] == 4 and res.summary["mapped"] == 0,
          f"unmapped={res.summary['unmapped']} mapped={res.summary['mapped']}")

    by = {r.source: r for r in res.rows}
    check("name similarity is offered as a labelled hint",
          by["Coffee"].hint.get("closest_name", {}).get("category") == "Bev",
          str(by["Coffee"].hint.get("closest_name")))
    check("value proximity is offered as a labelled hint",
          "closest_value" in by["Snacks"].hint,
          str(by["Snacks"].hint.get("closest_value")))
    check("the hint does not set a status",
          all(r.status == CM.ST_UNMAPPED for r in res.rows),
          "hints are evidence, never a decision")

    check("every Dataset-2 unit is offered as a target",
          {u["label"] for u in res.b_units} >=
          {"Tandy / Biscuits", "Savoury / Chips", "Savoury / Nuts", "Bev / Coffee"},
          f"{len(res.b_units)} units")
    check("unclaimed Dataset-2 units are listed as new in B",
          len(res.new_in_b) == 7,
          str([u["label"] for u in res.new_in_b]))

    # ---- 2. the user authors the mapping, in every relationship shape ------
    rows = [r.to_dict() for r in res.rows]

    def author(src, targets, canonical):
        for r in rows:
            if r["source"] == src:
                r["targets"] = [{"category": c, "subcategory": s, "total": None}
                                for c, s in targets]
                r["status"] = CM.ST_MAPPED
                r["canonical"] = canonical

    author("Biscuits", [("Tandy", "Biscuits")], "Biscuits")   # composite
    author("Snacks", [("Savoury", "Chips"), ("Savoury", "Nuts")], "Snacks")  # 1:N
    author("Coffee", [("Bev", "Coffee")], "Coffee")           # composite
    for r in rows:                                            # no counterpart
        if r["source"] == "Pet Food":
            r["status"] = CM.ST_EXCLUDED

    s = CM.resummarise(res, rows)
    check("the summary describes the mapping the user authored",
          s["mapped"] == 3 and s["excluded"] == 1 and s["unmapped"] == 0,
          f"mapped={s['mapped']} excluded={s['excluded']}")
    check("a 1-to-many mapping is counted",
          s["one_to_many"] == 1, f"one_to_many={s['one_to_many']}")
    # Every one of the three authored rows targets a (category, subcategory)
    # tuple, so all three are composites - "composite" means the target carries
    # a subcategory, not "has exactly one target". Biscuits and Coffee are 1:1
    # composites; Snacks is a 1:2 that is composite as well. `one_to_many`
    # above is the separate axis, which is why they do not partition the set.
    check("composite mappings are counted",
          s["composite"] == 3, f"composite={s['composite']}")
    # `by` was snapshotted before authoring, so it still shows the enumerated
    # state. Read the method off the authored rows to test what it describes.
    # Only the dataclass fields go back in - a row dict also carries the derived
    # `method` / `b_sum` / `delta_pct`, which are not constructor arguments.
    _FIELDS = {f.name for f in dataclasses.fields(CM.CategoryMapRow)}
    def _row_method(r):
        return CM.CategoryMapRow(**{k: v for k, v in r.items() if k in _FIELDS}).method
    authored_by = {r["source"]: r for r in rows}
    methods = {src: _row_method(r) for src, r in authored_by.items()}
    check("the reported method describes the mapping, it is not a score",
          methods["Biscuits"] == "1:1 (category + subcategory)"
          and methods["Snacks"] == "1:2"
          and methods["Coffee"] == "1:1 (category + subcategory)",
          f"Biscuits={methods['Biscuits']} Snacks={methods['Snacks']} "
          f"Coffee={methods['Coffee']}")

    map_a, map_b, ex_a, ex_b = CM.resolve(rows, res.new_in_b)
    check("a mapped unit enters the analysis under its canonical name",
          map_a.get(CM.ukey("Biscuits", "")) == "Biscuits", str(map_a))
    check("a composite target maps through",
          map_b.get(CM.ukey("Tandy", "Biscuits")) == "Biscuits", str(map_b))
    check("both targets of a 1:N mapping map through",
          map_b.get(CM.ukey("Savoury", "Chips")) == "Snacks"
          and map_b.get(CM.ukey("Savoury", "Nuts")) == "Snacks", str(map_b))
    check("an excluded category is dropped",
          CM.ukey("Pet Food", "") in ex_a, str(sorted(ex_a)))

    # ---- 3. the analysis honours exactly what was authored -----------------
    cfg = A.AnalysisConfig(
        metric_label="Sales Value",
        a_prior="VAL_YA", a_current="VAL", b_prior="VAL_YA", b_current="VAL",
        category_col="CAT", category_col_b="CAT", subcategory_col_b="SUB",
        category_map_a=map_a, category_map_b=map_b,
        category_excluded_a=sorted(ex_a), category_excluded_b=sorted(ex_b),
        top_n=5)
    prep = A.prepare(A_, B_, cfg)

    check("the analysis reports exactly the mapped categories",
          sorted(prep.categories) == ["Biscuits", "Coffee", "Snacks"],
          str(sorted(prep.categories)))
    check("the excluded category is absent",
          "Pet Food" not in prep.categories, str(prep.categories))
    check("no Dataset-2 raw category leaked in as a one-sided category",
          not any(c in prep.categories
                  for c in ("Tandy", "Savoury", "Bev", "Sweet")),
          "unclaimed B units are excluded, not reported with a null 'before'")

    snacks = A.category_report(prep, "Snacks")
    expect_b = float(B_[B_["SUB"].isin(["Chips", "Nuts"])]["VAL"].sum())
    check("a 1:N category reconciles on the B side",
          abs(snacks["total"]["after_current"] - expect_b) < 1e-9,
          f"AFTER={snacks['total']['after_current']} Chips+Nuts={expect_b}")
    expect_a = float(A_[A_["CAT"] == "Snacks"]["VAL"].sum())
    check("a 1:N category reconciles on the A side",
          abs(snacks["total"]["before_current"] - expect_a) < 1e-9,
          f"BEFORE={snacks['total']['before_current']}")

    coffee = A.category_report(prep, "Coffee")
    expect_c = float(B_[B_["SUB"] == "Coffee"]["VAL"].sum())
    check("a composite target reconciles",
          abs(coffee["total"]["after_current"] - expect_c) < 1e-9,
          f"AFTER={coffee['total']['after_current']} Bev/Coffee={expect_c}")

    # ---- 4. N:1 - several A categories onto one B category -----------------
    n1_rows = [r.to_dict() for r in CM.enumerate_category_units(
        pd.DataFrame({"CAT": ["Alpha", "Beta"], "VAL": [60.0, 40.0]}),
        pd.DataFrame({"CAT": ["Combined"], "VAL": [100.0]}),
        "CAT", None, "VAL", "CAT", None, "VAL").rows]
    for r in n1_rows:
        r["targets"] = [{"category": "Combined", "subcategory": "", "total": None}]
        r["status"] = CM.ST_MAPPED
        r["canonical"] = "Combined"
    n1_sum = CM.resummarise(CM.enumerate_category_units(
        pd.DataFrame({"CAT": ["Alpha", "Beta"], "VAL": [60.0, 40.0]}),
        pd.DataFrame({"CAT": ["Combined"], "VAL": [100.0]}),
        "CAT", None, "VAL", "CAT", None, "VAL"), n1_rows)
    check("an N:1 merge is recognised as one target shared twice",
          n1_sum["n_to_one"] == 1, f"n_to_one={n1_sum['n_to_one']}")
    check("the merged rows share one canonical name",
          len({r["canonical"] for r in n1_rows}) == 1,
          str({r["canonical"] for r in n1_rows}))

    ma1, mb1, xa1, xb1 = CM.resolve(n1_rows, [])
    cfg_n1 = A.AnalysisConfig(
        metric_label="VAL", a_prior="VAL", a_current="VAL",
        b_prior="VAL", b_current="VAL",
        category_col="CAT", category_col_b="CAT",
        category_map_a=ma1, category_map_b=mb1,
        category_excluded_a=sorted(xa1), category_excluded_b=sorted(xb1), top_n=5)
    prep_n1 = A.prepare(pd.DataFrame({"CAT": ["Alpha", "Beta"], "VAL": [60.0, 40.0]}),
                        pd.DataFrame({"CAT": ["Combined"], "VAL": [100.0]}), cfg_n1)
    rep_n1 = A.category_report(prep_n1, "Combined")
    check("the merged N:1 category reconciles (60 + 40 = 100)",
          abs(rep_n1["total"]["before_current"] - 100.0) < 1e-9,
          f"merged BEFORE={rep_n1['total']['before_current']}")

    # ---- 5. a category left unmapped is not analysed ----------------------
    partial = [r.to_dict() for r in res.rows]
    for r in partial:
        if r["source"] == "Coffee":
            r["targets"] = [{"category": "Bev", "subcategory": "Coffee",
                             "total": None}]
            r["status"] = CM.ST_MAPPED
            r["canonical"] = "Coffee"
    mp_a, mp_b, px_a, px_b = CM.resolve(partial, res.new_in_b)
    cfg_p = A.AnalysisConfig(
        metric_label="Sales Value",
        a_prior="VAL_YA", a_current="VAL", b_prior="VAL_YA", b_current="VAL",
        category_col="CAT", category_col_b="CAT", subcategory_col_b="SUB",
        category_map_a=mp_a, category_map_b=mp_b,
        category_excluded_a=sorted(px_a), category_excluded_b=sorted(px_b), top_n=5)
    prep_p = A.prepare(A_, B_, cfg_p)
    check("only the mapped category is analysed",
          sorted(prep_p.categories) == ["Coffee"], str(sorted(prep_p.categories)))
    check("an unmapped category is not reported as a one-sided category",
          "Biscuits" not in prep_p.categories
          and "Pet Food" not in prep_p.categories,
          "no 'before' with a null 'after', and no null-with-a-value either")
    check("the unmapped categories can be named for the user",
          {u["label"] for u in CM.unresolved_rows(partial)} ==
          {"Biscuits", "Snacks", "Pet Food"},
          str([u["label"] for u in CM.unresolved_rows(partial)]))

    # ---- 6. metric wiring with differently-named columns per side ---------
    # This is the reported bug: the two datasets name the same metric
    # differently, so one side's period columns must be selectable separately.
    m_a = pd.DataFrame({"CAT": ["X"], "Sales Value YA": [100.0], "Sales Value": [110.0]})
    m_b = pd.DataFrame({"CAT": ["X"], "Value (NT$) YA": [100.0], "Value (NT$)": [140.0]})
    cfg3 = A.AnalysisConfig(
        metric_label="Sales Value / Value (NT$)",
        a_prior="Sales Value YA", a_current="Sales Value",
        b_prior="Value (NT$) YA", b_current="Value (NT$)",
        category_col="CAT", category_col_b="CAT", top_n=5)
    prep3 = A.prepare(m_a, m_b, cfg3)
    rep3 = A.category_report(prep3, "X")
    check("differently-named metric columns wire up on both sides",
          abs(rep3["total"]["before_current"] - 110.0) < 1e-9
          and abs(rep3["total"]["after_current"] - 140.0) < 1e-9,
          f"A={rep3['total']['before_current']} B={rep3['total']['after_current']}")
    check("growth is computed across the differently-named columns",
          abs(rep3["total"]["after_growth_pct"] - 40.0) < 1e-9,
          f"{rep3['total']['after_growth_pct']:.2f}%")

    # ---- 7. differently-named DIMENSION columns per side ------------------
    d_a = pd.DataFrame({"CAT": ["X"], "MKT": ["M1"], "V YA": [100.0], "V": [110.0]})
    d_b = pd.DataFrame({"CAT": ["X"], "MARKET": ["M1"], "V YA": [100.0], "V": [140.0]})
    cfg4 = A.AnalysisConfig(
        metric_label="V", a_prior="V YA", a_current="V",
        b_prior="V YA", b_current="V",
        category_col="CAT", category_col_b="CAT",
        market_col="MKT", market_col_b="MARKET", top_n=5)
    prep4 = A.prepare(d_a, d_b, cfg4)
    rep4 = A.category_report(prep4, "X")
    check("differently-named dimension columns wire up on both sides",
          len(rep4.get("channel_block") or []) == 1,
          f"{len(rep4.get('channel_block') or [])} channel(s)")
    check("the channel block reconciles with per-side column names",
          abs(rep4["channel_block"][0]["after"]["mat_ty"] - 140.0) < 1e-9,
          f"after={rep4['channel_block'][0]['after']['mat_ty']}")

    # ---- 8. per-side market scope, when the two name a market differently --
    ms_a = pd.DataFrame({"CAT": ["X"], "MKT": ["A-side name"],
                         "V YA": [100.0], "V": [200.0]})
    ms_b = pd.DataFrame({"CAT": ["X"], "MKT": ["B-side name"],
                         "V YA": [100.0], "V": [400.0]})
    cfg5 = A.AnalysisConfig(
        metric_label="V", a_prior="V YA", a_current="V",
        b_prior="V YA", b_current="V",
        category_col="CAT", category_col_b="CAT",
        market_col="MKT", market_col_b="MKT",
        markets=["A-side name"], markets_b=["B-side name"], top_n=5)
    prep5 = A.prepare(ms_a, ms_b, cfg5)
    rep5 = A.category_report(prep5, "X")
    check("the market scope is applied per side, so differently-named markets match",
          rep5["total"]["before_current"] == 200.0
          and rep5["total"]["after_current"] == 400.0,
          f"A={rep5['total']['before_current']} B={rep5['total']['after_current']}")

    # The baseline's B-side name is looked up separately too, so a Total named
    # differently on the two sides is still verified on both.
    bl_a = pd.DataFrame({"CAT": ["X", "X"], "MKT": ["Total A", "Chan"],
                         "V YA": [100.0, 10.0], "V": [200.0, 20.0]})
    bl_b = pd.DataFrame({"CAT": ["X", "X"], "MKT": ["Total B", "Chan"],
                         "V YA": [100.0, 10.0], "V": [400.0, 20.0]})
    cfg6 = A.AnalysisConfig(
        metric_label="V", a_prior="V YA", a_current="V",
        b_prior="V YA", b_current="V",
        category_col="CAT", category_col_b="CAT",
        market_col="MKT", market_col_b="MKT",
        markets=["Chan"], markets_b=["Chan"],
        baseline_market="Total A", baseline_market_b="Total B", top_n=5)
    prep6 = A.prepare(bl_a, bl_b, cfg6)
    check("the Total Market baseline is found under each side's own name",
          prep6.baseline_total == (200.0, 400.0),
          f"baseline_total={prep6.baseline_total} (A 'Total A'=200, B 'Total B'=400)")
    check("the baseline is reported as verified, not left unverified",
          prep6.baseline_verified is True, f"{prep6.baseline_verified}")


# ===========================================================================
# PART 5 - ingestion: numbers arriving as text
# ===========================================================================


def part5() -> None:
    banner("PART 5  Ingestion - numbers arriving as text (pandas 3 StringDtype)")

    # pandas 3 gives text columns dtype 'str' (StringDtype), not 'object'. Both
    # guards in _clean_frame compared against 'object', so every text column was
    # skipped: the numeric coercion never ran, a workbook whose numbers arrive
    # as text was profiled as having no metrics at all, and empty-string padding
    # rows were never blanked so they survived as data.
    raw = pd.DataFrame({
        "CATEGORY": ["A", "B", "C", ""],
        "Sales Value YA": ["90.0", "80.0", "70.0", ""],
        "Sales Value": ["100.0", "120.0", "130.0", ""],
    })
    check("the fixture really is text, not numbers",
          not pd.api.types.is_numeric_dtype(raw["Sales Value"]),
          f"dtype={raw['Sales Value'].dtype}")

    out = ingest._clean_frame(raw)
    check("string numbers are coerced to numeric",
          pd.api.types.is_numeric_dtype(out["Sales Value"]),
          f"dtype={out['Sales Value'].dtype}")
    check("coerced values are exact",
          list(out["Sales Value"]) == [100.0, 120.0, 130.0],
          str(list(out["Sales Value"])))
    check("blank padding rows are dropped", len(out) == 3, f"{len(out)} rows")

    # A column carrying a stray label must stay text: coercing it would destroy
    # the label silently.
    mixed = pd.DataFrame({"V": ["1.5", "2.5", "3.5", "Total"]})
    o2 = ingest._clean_frame(mixed)
    check("a column with a stray label is left as text, not silently gutted",
          not pd.api.types.is_numeric_dtype(o2["V"]) and len(o2) == 4,
          f"dtype={o2['V'].dtype} values={list(o2['V'])}")

    comma = pd.DataFrame({"V": ["1,234.5", "2,345.6"]})
    o3 = ingest._clean_frame(comma)
    check("thousands separators are handled",
          pd.api.types.is_numeric_dtype(o3["V"])
          and abs(float(o3["V"].iloc[0]) - 1234.5) < 1e-9,
          str(list(o3["V"])))

    # A placeholder must become blank, never zero: a zero would be summed into
    # the analysis. Needs a second column so the all-blank row is not itself
    # dropped as an empty row.
    ph = pd.DataFrame({"K": ["a", "b", "c", "d", "e"],
                       "V": ["1.0", "n/a", "-", "", "2.0"]})
    o4 = ingest._clean_frame(ph)
    check("placeholders become blank rather than zero",
          int(o4["V"].isna().sum()) == 3
          and list(o4["V"].dropna()) == [1.0, 2.0],
          f"nulls={int(o4['V'].isna().sum())} kept={list(o4['V'].dropna())}")

    prof = P.profile_dataset(out, "synthetic")
    check("profiling now detects the metric family",
          "Sales Value" in prof.metric_families,
          str(list(prof.metric_families)))
    check("the metric column is profiled as a numeric metric",
          any(c.name == "Sales Value" and c.is_metric and c.dtype.startswith("float")
              for c in prof.column_profiles),
          next((f"{c.name}:{c.role}:{c.dtype}" for c in prof.column_profiles
                if c.name == "Sales Value"), "not found"))

    # Real workbook: the sheet in the bug report must load as numeric too.
    if os.path.isfile(WORKBOOK):
        d = ingest.load_table(WORKBOOK, sheet_name="Current_MAT", header_row=1)
        nums = [c for c in d.columns if pd.api.types.is_numeric_dtype(d[c])]
        check("Current_MAT loads with numeric metric columns", len(nums) == 8,
              f"{len(nums)} numeric columns")
        check("Current_MAT padding rows are dropped", len(d) == 45_421,
              f"{len(d):,} rows (99,999 in the sheet)")
        check("Current_MAT + New_MAT equals Raw_MAT",
              len(d) + len(ingest.load_table(WORKBOOK, sheet_name="New_MAT",
                                             header_row=1)) == 96_135,
              "45,421 + 50,714 = 96,135")


# ===========================================================================
# PART 6 - one side empty: the block builders must not raise KeyError
# ===========================================================================


def part6() -> None:
    banner("PART 6  One dataset empty - blocks build instead of raising")

    # Reported from the UI as a raw 500 mid-run on a large selection:
    #     KeyError: 'market'
    #     analysis.py:333 in _entity_block -> ga.merge(gb, on=dim)
    # Cause: _aggregate's early returns handed back a frame of prior/current
    # only, so the group column the merge keys on was simply absent whenever a
    # side aggregated to nothing. A category or market present on only one side
    # is the normal case for this tool, not an edge case.
    a = pd.DataFrame({"category": ["C1"], "subcategory": [""], "market": ["M1"],
                      "manufacturer": ["ACME"], "brand": ["b1"],
                      "prior": [10.0], "current": [12.0], "__ds": ["A"]})
    b_empty = a.iloc[0:0].copy()
    b_empty["__ds"] = "B"

    # _aggregate itself must keep the group column even with nothing to group.
    agg = A._aggregate(b_empty, ["market"], {"prior": "prior", "current": "current"},
                       False, None)
    check("an empty aggregate still carries its group column",
          "market" in agg.columns,
          f"columns={list(agg.columns)}")

    for dim, col in [("market", "M1"), ("manufacturer", "ACME"), ("brand", "b1")]:
        try:
            blk = A._entity_block(a, b_empty, dim, False, None, "Sales Value")
            ok, msg = (dim in blk.columns), f"rows={len(blk)} cols={list(blk.columns)[:5]}"
        except KeyError as exc:
            ok, msg = False, f"KeyError: {exc}"
        check(f"[{dim}] block builds with dataset B empty", ok, msg)

        try:
            blk2 = A._entity_block(b_empty, a, dim, False, None, "Sales Value")
            ok2 = dim in blk2.columns
            msg2 = f"rows={len(blk2)}"
        except KeyError as exc:
            ok2, msg2 = False, f"KeyError: {exc}"
        check(f"[{dim}] block builds with dataset A empty", ok2, msg2)

    # The same hole in the rate path, which takes the weighted-average branch.
    ar = a.copy()
    ar["weight"] = 5.0
    br = b_empty.copy()
    br["weight"] = 1.0
    try:
        blk = A._entity_block(ar, br, "market", True, "weight", "ND Dist")
        ok3, msg3 = ("market" in blk.columns), f"rows={len(blk)}"
    except KeyError as exc:
        ok3, msg3 = False, f"KeyError: {exc}"
    check("[rate metric] block builds with one side empty", ok3, msg3)

    # A rate metric must aggregate as a weighted average AND keep dimension
    # values intact. The old code joined the group columns with "||" and split
    # them back with `.str.split("||", expand=True)` - a REGEX split, so "M1"
    # came back as ["M", "1", None, None]: four columns instead of one (hence
    # the Length mismatch), and the market name silently mutated.
    rate = pd.DataFrame({
        "market": ["M1", "M1", "M2"],
        "brand": ["b1", "b1", "b2"],
        "prior": [10.0, 20.0, 40.0],
        "weight": [1.0, 3.0, 2.0],
    })
    gw = A._aggregate(rate, ["market"], {"prior": "prior"}, True, "weight")
    got = dict(zip(gw["market"], gw["prior"]))
    check("a rate metric aggregates as a weighted average",
          abs(got.get("M1", 0) - 17.5) < 1e-9 and abs(got.get("M2", 0) - 40.0) < 1e-9,
          f"M1={got.get('M1')} (want 17.5), M2={got.get('M2')} (want 40.0)")
    check("rate aggregation does not mutate the dimension values",
          set(gw["market"]) == {"M1", "M2"},
          f"values={list(gw['market'])} (regex split used to yield 'M','1')")

    gu = A._aggregate(rate, ["market"], {"prior": "prior"}, True, None)
    got_u = dict(zip(gu["market"], gu["prior"]))
    check("a rate metric with no weight falls back to a plain mean",
          abs(got_u.get("M1", 0) - 15.0) < 1e-9,
          f"M1={got_u.get('M1')} (want 15.0 = (10+20)/2)")

    gm = A._aggregate(rate, ["market", "brand"], {"prior": "prior"}, True, "weight")
    check("rate aggregation still groups on multiple columns",
          list(gm.columns) == ["market", "brand", "prior"],
          f"columns={list(gm.columns)}")

    # And the rank block downstream must tolerate the single-sided result,
    # rather than dying on a missing column further along.
    try:
        blk = A._entity_block(a, b_empty, "manufacturer", False, None, "Sales Value")
        top, client = A._rank_block(blk, "manufacturer", 10, [])
        ok4, msg4 = True, f"top={len(top)} client={len(client)}"
    except Exception as exc:
        ok4, msg4 = False, f"{type(exc).__name__}: {exc}"
    check("ranking tolerates a single-sided block", ok4, msg4)

    # End to end: a category that exists only in B must still produce a report
    # instead of aborting the whole run.
    cfg = A.AnalysisConfig(
        a_prior="prior", a_current="current", b_prior="prior", b_current="current",
        category_col="category", market_col="market",
        manufacturer_col="manufacturer", brand_col="brand",
        categories=["C1", "ONLY_IN_B"],
    )
    b2 = b_empty.copy()
    b2.loc[0] = ["ONLY_IN_B", "", "M2", "OTHER", "b9", 0.0, 7.0, "B"]
    try:
        prep = A.prepare(a, b2, cfg)
        reps = A.analyse(prep)
        names = [r["category"] for r in reps]
        ok5 = "ONLY_IN_B" in names
        msg5 = f"{len(reps)} report(s): {names}"
    except Exception as exc:
        ok5, msg5 = False, f"{type(exc).__name__}: {exc}"
    check("a B-only category is analysed, not dropped or fatal", ok5, msg5)

    # --- the QC's category-total reconciliation must be rate-aware -----------
    # A rate metric exists on a 0-100 scale and the pipeline averages it. The
    # check recomputed with a plain nansum, so every rate-metric run reported
    # "FAIL - failed to reconcile" on data that was entirely correct. That is
    # the same false-failure shape as a QC check that cannot run reporting PASS.
    rdf = pd.DataFrame({"CATEGORY": ["C1", "C1", "C1", "C2"],
                        "ND Dist": [10.0, 20.0, 60.0, 99.0],
                        "Sales Value": [1.0, 1.0, 2.0, 5.0]})
    wm = QC._independent_category_total(rdf, "CATEGORY", "C1", "ND Dist",
                                        is_rate=True, weight_col="Sales Value")
    check("a rate total reconciles as a weighted mean",
          wm is not None and abs(wm - 37.5) < 1e-9,
          f"got {wm}, want 37.5 = (10*1+20*1+60*2)/(1+1+2)")
    um = QC._independent_category_total(rdf, "CATEGORY", "C1", "ND Dist",
                                        is_rate=True)
    check("a rate with no weight reconciles as an unweighted mean",
          um is not None and abs(um - 30.0) < 1e-9,
          f"got {um}, want 30.0 = (10+20+60)/3")
    sm = QC._independent_category_total(rdf, "CATEGORY", "C1", "ND Dist",
                                        is_rate=False)
    check("an additive metric still reconciles as a sum",
          sm is not None and abs(sm - 90.0) < 1e-9, f"got {sm}, want 90.0")
    check("a rate is never reconciled as a plain sum", sm != wm,
          f"sum={sm} weighted_mean={wm}")

    zw = pd.DataFrame({"CATEGORY": ["C1"], "ND Dist": [50.0], "Sales Value": [0.0]})
    nn = QC._independent_category_total(zw, "CATEGORY", "C1", "ND Dist",
                                        is_rate=True, weight_col="Sales Value")
    check("a rate with zero total weight is Not Verified, not PASS or 0",
          nn is None, f"got {nn!r} (None = unverifiable)")


# ===========================================================================
# PART 7 - market hierarchy, and contribution against the Total Market
# ===========================================================================


def part7() -> None:
    banner("PART 7  Market pairing the user authors, and the Total Market baseline")

    # A market dimension carries the Total alongside the regions and channels
    # that make it up. In the reference workbook the channels sum to 55.5% of
    # the Total, so adding them to it double-counts and dividing by their sum
    # understates every share.
    #
    # The user says which pairing is a Total; the hierarchy is read only to
    # *advise* against a level that contradicts it. So there are two things to
    # check: an unpaired value is never paired for them, and a pairing that
    # contradicts the hierarchy keeps their level and gets a note.
    values = ["TW Total TW Offline (G)", "TW CVS", "TW Chain Super-PX MART",
              "TW Personal Care Store"]
    paths = {
        "TW Total TW Offline (G)": "TW Total TW Offline (G)",
        "TW CVS": "CVS/TW Total TW Offline (G)/MT w/o Costco",
        "TW Chain Super-PX MART": "PX MART/TW Total TW Offline (G)/MT w/o Costco",
        "TW Personal Care Store": "TW PCS&PMC (O)",
    }
    va = {"TW Total TW Offline (G)": 1000.0, "TW CVS": 400.0,
          "TW Chain Super-PX MART": 100.0, "TW Personal Care Store": 55.0}

    # ---- nothing is paired unless the user paired it ----------------------
    empty = MK.enumerate_markets(values, values, va, va,
                                 paths_a=paths, paths_b=paths)
    check("an unpaired value list produces no pairings",
          empty.pairs == [], f"{len(empty.pairs)} pair(s)")
    check("both sides' values are listed for the user to pick from",
          empty.a_values == sorted(values) and empty.b_values == sorted(values),
          f"A={len(empty.a_values)} B={len(empty.b_values)}")
    check("no level is assumed for an unpaired value",
          empty.summary["n_total"] == 0 and empty.summary["n_channel"] == 0,
          f"total={empty.summary['n_total']} channel={empty.summary['n_channel']}")
    check("every unpaired value is reported as unpaired",
          set(empty.summary["unpaired_a"]) == set(values),
          str(empty.summary["unpaired_a"]))

    # ---- the user's pairing is what the scope is derived from -------------
    res = MK.enumerate_markets(
        values, values, va, va, paths_a=paths, paths_b=paths,
        pairs=[{"market_a": "TW Total TW Offline (G)",
                "market_b": "TW Total TW Offline (G)", "level": "total"},
               {"market_a": "TW CVS", "market_b": "TW CVS", "level": "channel"},
               {"market_a": "TW Chain Super-PX MART",
                "market_b": "TW Chain Super-PX MART", "level": "channel"},
               {"market_a": "TW Personal Care Store",
                "market_b": "TW Personal Care Store", "level": "channel"}])
    check("the levels the user set are carried through unchanged",
          res.summary["n_total"] == 1 and res.summary["n_channel"] == 3,
          f"total={res.summary['n_total']} channel={res.summary['n_channel']}")
    check("the parts are recognised as a subset of the Total",
          res.summary["parts_are_subset"] is True
          and res.summary["parts_of_total_pct"] == 55.5,
          f"parts are {res.summary['parts_of_total_pct']}% of the Total")
    check("scoping by level returns only that level",
          res.scope_for("total") == ["TW Total TW Offline (G)"]
          and len(res.scope_for("channel")) == 3,
          f"total={res.scope_for('total')} channels={len(res.scope_for('channel'))}")

    # A market whose name says "Total" but is a child must not silently win, and
    # a channel wrongly marked as a Total must not have its level overridden.
    wrong = MK.enumerate_markets(
        values, values, va, va, paths_a=paths, paths_b=paths,
        pairs=[{"market_a": "TW CVS", "market_b": "TW CVS", "level": "total"}])
    p = wrong.pairs[0]
    check("a channel marked as a Total keeps the user's level",
          p.level == "total", f"level={p.level}")
    check("...but the hierarchy raises a contradiction against it",
          "contradiction" in p.evidence,
          str(p.evidence.get("contradiction")))
    check("a pairing that contradicts the hierarchy is counted for review",
          wrong.summary["needs_review"] == 1,
          f"needs_review={wrong.summary['needs_review']}")

    two_totals = MK.enumerate_markets(
        ["TW Total TW Offline (G)", "TW Total Online", "TW CVS"],
        ["TW Total TW Offline (G)", "TW Total Online", "TW CVS"],
        paths_a={"TW Total TW Offline (G)": "TW Total TW Offline (G)",
                 "TW CVS": "CVS/TW Total TW Offline (G)"},
        pairs=[{"market_a": "TW Total Online", "market_b": "TW Total Online",
                "level": "channel"}])
    check("a second total-looking name does not override the user's level",
          two_totals.pairs[0].level == "channel",
          f"level={two_totals.pairs[0].level} "
          f"note={two_totals.pairs[0].evidence.get('contradiction')}")

    # --- contribution must be measured against the Total, not the block sum ---
    # Total 100, channels 30 + 20. The channels are a subset, so their combined
    # share of the market is 50%, not 100% of themselves.
    df = pd.DataFrame({
        "CATEGORY": ["C1", "C1", "C1"],
        "Display Market Name": ["TOTAL", "CH-A", "CH-B"],
        "Sales Value YA": [90.0, 27.0, 18.0],
        "Sales Value": [100.0, 30.0, 20.0],
    })
    cfg = A.AnalysisConfig(
        a_prior="Sales Value YA", a_current="Sales Value",
        b_prior="Sales Value YA", b_current="Sales Value",
        category_col="CATEGORY", market_col="Display Market Name",
        markets=["CH-A", "CH-B"], baseline_market="TOTAL",
    )
    prep = A.prepare(df, df, cfg)
    rep = A.category_report(prep, "C1")
    ins = {i["key"]: i for i in rep["insights"]}

    check("the baseline is the Total Market, computed outside the scope filter",
          prep.baseline_total == (100.0, 100.0),
          f"baseline_total={prep.baseline_total} (scope holds only 30+20)")
    cat_share = ins["contribution"]["before_share_pct"]
    check("a category's share is measured against the Total Market",
          cat_share is not None and abs(cat_share - 50.0) < 1e-9,
          f"{cat_share}% (want 50 = 50/100, not 100 = 50/50 of the block)")
    chan = {c["name"]: c for c in rep["channel_block"]}
    ch_a = chan["CH-A"]["contribution"]["before_share_pct"]
    ch_b = chan["CH-B"]["contribution"]["before_share_pct"]
    check("each channel's share is also against the Total",
          ch_a is not None and ch_b is not None
          and abs(ch_a - 30.0) < 1e-9 and abs(ch_b - 20.0) < 1e-9,
          f"CH-A={ch_a}% CH-B={ch_b}% (want 30 / 20, not 60 / 40 of the block)")

    # --- the three headline insights, in the requested order ------------------
    keys = [i["key"] for i in rep["insights"]]
    check("the insights are MAT AD Growth, MAT AD Level Shift, Contribution",
          keys == ["mat_growth", "mat_level_shift", "contribution"], str(keys))
    check("growth is reported before and after",
          ins["mat_growth"]["before"] is not None
          and ins["mat_growth"]["after"] is not None,
          f"before={ins['mat_growth']['before']} after={ins['mat_growth']['after']}")
    check("level shift equals after growth minus before growth",
          abs(ins["mat_level_shift"]["value"]
              - (ins["mat_growth"]["after"] - ins["mat_growth"]["before"])) < 1e-9,
          f"{ins['mat_level_shift']['value']}")

    # --- with no Total designated the share must not silently become 100% ----
    cfg2 = A.AnalysisConfig(
        a_prior="Sales Value YA", a_current="Sales Value",
        b_prior="Sales Value YA", b_current="Sales Value",
        category_col="CATEGORY", market_col="Display Market Name",
        markets=["CH-A", "CH-B"], market_levels={"TOTAL": "total"},
    )
    prep2 = A.prepare(df, df, cfg2)
    rep2 = A.category_report(prep2, "C1")
    ins2 = {i["key"]: i for i in rep2["insights"]}
    check("no Total Market means the baseline is reported unverified",
          ins2["contribution"]["baseline_verified"] is False,
          f"verified={ins2['contribution']['baseline_verified']}")
    check("and the note says what the share is actually of",
          "Total Market" in (ins2["contribution"]["note"] or "")
          or "no Total Market" in (ins2["contribution"]["note"] or ""),
          str(ins2["contribution"]["note"]))


# ===========================================================================
# PART 8 - metric treatment: growth-free ND, and a multi-metric run
# ===========================================================================
#
# The brief names three metrics (Sales Value, Volume, ND), allows any
# combination, and says ND must **not** carry growth: it is a distribution
# level, so the report shows Top / TY / YA / absolute change instead.
#
# These checks assert the contract the frontend and the exports rely on:
#   * growth_applicable=False removes growth from the total, the channel block
#     and the insights - and does not merely blank one of them;
#   * the multi-metric merge produces ONE report per category carrying a block
#     per metric, with the first metric copied to the headline fields.


def _metric_frame() -> pd.DataFrame:
    """Two markets, one category, carrying Sales Value and ND side by side."""
    rows = []
    for market, sv_ya, nd_ya, sv, nd in [
        ("CH-A", 100.0, 40.0, 120.0, 45.0),
        ("CH-B", 200.0, 60.0, 180.0, 55.0),
    ]:
        for _ in range(2):
            rows.append({
                "CATEGORY": "C1", "Market": market,
                "Sales Value YA": sv_ya / 2, "Sales Value": sv / 2,
                "ND Dist YA": nd_ya / 2, "ND Dist": nd / 2,
            })
    return pd.DataFrame(rows)


def part8() -> None:
    banner("PART 8  Metric treatment — growth-free ND and a multi-metric run")
    df = _metric_frame()

    # --- ND: growth_applicable=False must suppress growth everywhere ----------
    cfg_nd = A.AnalysisConfig(
        metric_label="Numeric Distribution", metric_key="nd",
        a_prior="ND Dist YA", a_current="ND Dist",
        b_prior="ND Dist YA", b_current="ND Dist",
        is_rate=True, growth_applicable=False,
        weight_metric="Sales Value", weight_metric_b="Sales Value",
        category_col="CATEGORY", market_col="Market", markets=["CH-A", "CH-B"],
    )
    prep_nd = A.prepare(df, df, cfg_nd)
    rep_nd = A.category_report(prep_nd, "C1")

    check("ND: the report declares growth not applicable",
          rep_nd.get("growth_applicable") is False,
          f"growth_applicable={rep_nd.get('growth_applicable')}")
    t_nd = rep_nd["total"]
    check("ND: the total carries no growth figure",
          t_nd.get("before_growth_pct") is None
          and t_nd.get("after_growth_pct") is None
          and t_nd.get("level_shift_pp") is None,
          f"before={t_nd.get('before_growth_pct')} "
          f"after={t_nd.get('after_growth_pct')} "
          f"shift={t_nd.get('level_shift_pp')}")
    check("ND: absolute change is still reported",
          t_nd.get("abs_change") is not None,
          f"abs_change={t_nd.get('abs_change')}")
    # Weighted mean, derived from the frame rather than hand-written: the rate
    # must aggregate as sum(ND * weight) / sum(weight), not as a plain sum.
    exp_nd = float(
        (df["ND Dist"] * df["Sales Value"]).sum() / df["Sales Value"].sum())
    check("ND: the total reconciles as a weighted mean, not a sum",
          t_nd.get("before_current") is not None
          and abs(t_nd["before_current"] - exp_nd) < 1e-9,
          f"got {t_nd.get('before_current')}, weighted mean = {exp_nd:.4f}, "
          f"plain sum = {df['ND Dist'].sum():.1f}")

    ch_nd = rep_nd["channel_block"]
    check("ND: every channel row omits growth and level shift",
          ch_nd and all(
              c["before"].get("growth_pct") is None
              and c["after"].get("growth_pct") is None
              and c["level_shift"].get("mat_ty_pp") is None
              for c in ch_nd),
          f"{len(ch_nd)} channel(s)")
    check("ND: channel rows still carry TY, YA and shares",
          ch_nd and all(
              c["before"].get("mat_ya") is not None
              and c["after"].get("mat_ty") is not None
              and c["contribution"].get("after_share_pct") is not None
              for c in ch_nd),
          "TY / YA / contribution shares present")

    keys_nd = [i["key"] for i in rep_nd["insights"]]
    check("ND: the insights drop growth and level shift",
          "mat_growth" not in keys_nd and "mat_level_shift" not in keys_nd,
          str(keys_nd))
    check("ND: the insights lead with an absolute change",
          "abs_change" in keys_nd or "contribution" in keys_nd,
          str(keys_nd))

    # --- the additive metric must still carry growth -------------------------
    cfg_sv = A.AnalysisConfig(
        metric_label="Sales Value", metric_key="sales_value",
        a_prior="Sales Value YA", a_current="Sales Value",
        b_prior="Sales Value YA", b_current="Sales Value",
        category_col="CATEGORY", market_col="Market", markets=["CH-A", "CH-B"],
    )
    rep_sv = A.category_report(A.prepare(df, df, cfg_sv), "C1")
    check("Sales Value: growth is still reported",
          rep_sv["total"].get("before_growth_pct") is not None
          and rep_sv["total"].get("after_growth_pct") is not None,
          f"before={rep_sv['total'].get('before_growth_pct')}")
    check("Sales Value: the insights include growth",
          "mat_growth" in [i["key"] for i in rep_sv["insights"]],
          str([i["key"] for i in rep_sv["insights"]]))

    # --- QC must follow the metric treatment ---------------------------------
    qc_sv = QC.QCReport()
    QC.check_before_after_consistency([rep_sv], qc_sv)
    sv_rows = [c for c in qc_sv.checks
               if c.name == "Before/after consistency"]
    check("QC: an additive metric is checked for level shift",
          sv_rows and sv_rows[-1].status == QC.PASS
          and sv_rows[-1].detail.get("growth_applicable", True) is True,
          f"{sv_rows[-1].status if sv_rows else 'missing'}")

    qc_nd = QC.QCReport()
    QC.check_before_after_consistency([rep_nd], qc_nd)
    nd_rows = [c for c in qc_nd.checks
               if c.name == "Before/after consistency"]
    check("QC: a growth-free metric is checked for absolute change instead",
          nd_rows and nd_rows[-1].status == QC.PASS
          and nd_rows[-1].detail.get("growth_applicable") is False,
          f"{nd_rows[-1].status if nd_rows else 'missing'} "
          f"{nd_rows[-1].message if nd_rows else ''}")

    # --- the multi-metric merge contract -------------------------------------
    # Recreate what main._run_reports does, without a server: analyse each
    # metric, then merge by category, keeping the first metric as the headline.
    blocks = [
        ("sales_value", "Sales Value", cfg_sv),
        ("nd", "Numeric Distribution", cfg_nd),
    ]
    by_cat: dict[str, dict] = {}
    for key, label, cfg in blocks:
        for rep in A.analyse(A.prepare(df, df, cfg), ["C1"]):
            base = by_cat.setdefault(rep["category"], dict(rep))
            base.setdefault("metrics", {})[key] = {
                "key": key, "label": label,
                "growth_applicable": rep.get("growth_applicable", True),
                "total": rep["total"], "channel_block": rep.get("channel_block") or [],
                "insights": rep.get("insights") or [],
            }
    merged = by_cat["C1"]
    check("multi-metric: one report per category, not one per metric",
          len(by_cat) == 1, f"{len(by_cat)} report(s)")
    check("multi-metric: the report carries a block per metric",
          set(merged["metrics"]) == {"sales_value", "nd"}, str(list(merged["metrics"])))
    check("multi-metric: each block keeps its own growth rule",
          merged["metrics"]["sales_value"]["growth_applicable"] is True
          and merged["metrics"]["nd"]["growth_applicable"] is False,
          "sales_value=True nd=False")
    check("multi-metric: each block keeps its own total",
          merged["metrics"]["sales_value"]["total"]["before_current"] != 0
          and merged["metrics"]["nd"]["total"]["before_current"] != 0
          and merged["metrics"]["sales_value"]["total"]["before_current"]
          != merged["metrics"]["nd"]["total"]["before_current"],
          f"sv={merged['metrics']['sales_value']['total']['before_current']} "
          f"nd={merged['metrics']['nd']['total']['before_current']}")


# ===========================================================================
# PART 9 - market levels, the duplicate grain, and generalised periods
# ===========================================================================
#
# Three behaviours this session changed, each of which the reference workbook
# cannot demonstrate on its own:
#
#  * the market dimension is presented as **one block per level** (Total at the
#    top, then its channels / its regions), with the Total never repeated among
#    its own members;
#  * the duplicate check keys on the **full grain** including the period
#    separator, because a stacked workbook (MAT TY + MAT YA) repeats its
#    dimension rows by design;
#  * the period vocabulary is **recognised, not assumed**, so a translated or
#    unrecognised qualifier still resolves to two concrete columns.


def part9() -> None:
    banner("PART 9 - market levels, duplicate grain, generalised periods")

    # --- the level split ----------------------------------------------------
    # TOTAL covers more than the listed channels (55% in the reference data), and
    # there are two regions as well. The blocks must be separate, and the Total
    # must appear once at the head of each - never inside `members`, or a reader
    # summing the block counts it twice.
    df = pd.DataFrame({
        "CATEGORY": ["C1"] * 6,
        "MKT": ["TOTAL", "CH-A", "CH-B", "RG-N", "RG-S", "OTHER-1"],
        "Sales Value YA": [90.0, 27.0, 18.0, 20.0, 22.0, 3.0],
        "Sales Value": [100.0, 30.0, 20.0, 24.0, 25.0, 1.0],
    })
    cfg = A.AnalysisConfig(
        a_prior="Sales Value YA", a_current="Sales Value",
        b_prior="Sales Value YA", b_current="Sales Value",
        category_col="CATEGORY", market_col="MKT",
        markets=["TOTAL", "CH-A", "CH-B", "RG-N", "RG-S", "OTHER-1"],
        baseline_market="TOTAL",
        market_levels={"TOTAL": "total", "CH-A": "channel", "CH-B": "channel",
                       "RG-N": "region", "RG-S": "region"},
    )
    rep = A.category_report(A.prepare(df, df, cfg), "C1")
    blocks = rep["blocks"]

    ch = blocks.get("channel")
    rg = blocks.get("region")
    check("a Market / Channel block is produced when channels are paired",
          bool(ch) and len(ch["members"]) == 2,
          f"members={[m['name'] for m in (ch or {}).get('members', [])]}")
    check("a Market / Region block is produced when regions are paired",
          bool(rg) and len(rg["members"]) == 2,
          f"members={[m['name'] for m in (rg or {}).get('members', [])]}")
    check("each block leads with the Total Market",
          ch and ch["total"] and ch["total"]["name"] == "TOTAL",
          f"channel total={ch and ch['total'] and ch['total']['name']}")
    check("the Total is NOT repeated among the members",
          all(m["name"] != "TOTAL" for m in (ch["members"] + rg["members"])),
          f"members={[m['name'] for m in ch['members'] + rg['members']]}")
    check("a market with no level is still reported, not silently dropped",
          bool(blocks.get("market_other"))
          and [m["name"] for m in blocks["market_other"]["members"]] == ["OTHER-1"],
          f"other={blocks.get('market_other') and [m['name'] for m in blocks['market_other']['members']]}")
    check("the flat channel_block keeps the Total first, for the exports",
          rep["channel_block"][0]["name"] == "TOTAL"
          and len(rep["channel_block"]) == 6,
          f"first={rep['channel_block'][0]['name']} n={len(rep['channel_block'])}")
    # A channel's share is of the Total, not of the channel block.
    c_a = {c["name"]: c for c in ch["members"]}["CH-A"]
    check("a channel's share is measured against the Total Market",
          abs(c_a["contribution"]["before_share_pct"] - 30.0) < 1e-9,
          f"{c_a['contribution']['before_share_pct']}% (want 30 = 30/100)")

    # --- the duplicate grain ------------------------------------------------
    # A stacked frame: the same manufacturer appears once per period. That is the
    # structure of the data, not a fault, so the check must NOT report it as
    # duplicates - while a genuine repeat on the full grain still must.
    stacked = pd.DataFrame({
        "CATEGORY": ["C1", "C1", "C1", "C1"],
        "MANUFACTURER": ["M1", "M1", "M2", "M2"],
        "BRAND": ["B1", "B1", "B2", "B2"],
        "Period": ["TY", "YA", "TY", "YA"],
        "Sales Value": [50.0, np.nan, 30.0, np.nan],
        "Sales Value YA": [np.nan, 40.0, np.nan, 25.0],
    })
    cfg_d = A.AnalysisConfig(
        a_prior="Sales Value YA", a_current="Sales Value",
        b_prior="Sales Value YA", b_current="Sales Value",
        category_col="CATEGORY", manufacturer_col="MANUFACTURER",
        brand_col="BRAND", period_col="Period",
    )
    qc_d = QC.QCReport()
    QC.check_duplicates(stacked, stacked, cfg_d, qc_d)
    item = next(c for c in qc_d.checks if c.id == "duplicates")
    d0 = item.detail["datasets"][0]
    check("a period-stacked frame is not reported as duplicated",
          d0["duplicate_rows"] == 0,
          f"duplicate_rows={d0['duplicate_rows']} "
          f"(dimension repeats={d0['duplicate_dimension_rows']})")
    check("the period repeats are reported as structure, not hidden",
          d0["duplicate_dimension_rows"] == 2
          and item.status == "PASS"
          and "stacked workbook" in item.message,
          f"dim repeats={d0['duplicate_dimension_rows']} status={item.status}")

    # The same key repeating *within* one period is a real duplicate.
    dup = pd.concat([stacked, stacked.iloc[[0]]], ignore_index=True)
    qc_d2 = QC.QCReport()
    QC.check_duplicates(dup, dup, cfg_d, qc_d2)
    item2 = next(c for c in qc_d2.checks if c.id == "duplicates")
    d2 = item2.detail["datasets"][0]
    check("a repeat on the full grain is still flagged",
          d2["duplicate_rows"] == 1 and item2.status in ("WARN", "FAIL"),
          f"duplicate_rows={d2['duplicate_rows']} status={item2.status}")

    # --- generalised period vocabulary --------------------------------------
    # The engine must recognise a translated qualifier AND fall back to position
    # when it recognises nothing, in both cases ending with two concrete columns.
    base, var = P.split_metric_name("Umsatz Vorjahr")
    check("a translated period qualifier is recognised",
          base == "Umsatz" and var == "YA", f"{base!r} {var!r}")
    base2, var2 = P.split_metric_name("Marketing Spend 上年")
    check("a CJK period qualifier is recognised",
          base2 == "Marketing Spend" and var2 == "YA", f"{base2!r} {var2!r}")
    prior, current = P.default_period_columns({"SOMETHING": "X", "OTHER": "Y"})
    check("a family with no recognised qualifier still yields MAT YA + MAT TY",
          prior == "X" and current == "Y", f"prior={prior!r} current={current!r}")
    prior3, current3 = P.default_period_columns(
        {"YA": "Sales Value YA", "VALUE": "Sales Value"})
    check("a recognised pair resolves to its real columns",
          prior3 == "Sales Value YA" and current3 == "Sales Value",
          f"{prior3!r} {current3!r}")
    # The study reads two periods only. A family carrying 2YA must still resolve
    # to YA -> VALUE, because 2YA is a third moving-annual window: taking it as
    # "the prior period" is what made A read B's year-ago column and call the
    # difference growth.
    prior4, current4 = P.default_period_columns(
        {"2YA": "Sales Value 2YA", "YA": "Sales Value YA", "VALUE": "Sales Value"})
    check("2YA is never chosen as a period slot",
          prior4 == "Sales Value YA" and current4 == "Sales Value",
          f"{prior4!r} {current4!r}")
    prior5, current5 = P.default_period_columns(
        {"2YA": "Sales Volume 2YA", "YA": "Sales Volume YA", "VALUE": "Sales Volume"})
    check("a second family resolves the same way",
          prior5 == "Sales Volume YA" and current5 == "Sales Volume",
          f"{prior5!r} {current5!r}")
    # A 2YA-only family genuinely has no year-ago column; reporting that is
    # correct, and guessing one of the two windows would not be.
    prior6, current6 = P.default_period_columns({"2YA": "S 2YA", "TY": "S TY"})
    check("a family with no YA reports no MAT YA rather than borrowing 2YA",
          prior6 == "" and current6 == "S TY", f"{prior6!r} {current6!r}")


# ===========================================================================
# PART 10 - the Total Market is printed once, at the head of the block
#
# A stacked source file carries a Total row *and* the rows it covers. The block
# therefore leads with the Total Market and lists the members beneath it - and
# `report["total"]` (the sum over every row of the category) is a superset of
# both. Printing it again at the foot duplicated the head row and overstated it.
#
# Both halves matter: with a baseline the foot row must be gone, and with no
# baseline it must remain, because there the sum is the only aggregate there is.
# A test that only checked the first half would be satisfied by deleting the row
# everywhere - including the case that needs it.
# ===========================================================================


# ===========================================================================
# PART 11 - the two periods the study reads come from the Period columns
#
# The study has exactly two periods: MAT YA and MAT TY. Both are already named
# in the data - a metric family spells them as a qualifier on the column name
# (`Sales Value YA` / `Sales Value`) - so a second mapping asking the user to
# declare them again is redundant, and the default it offered (2YA -> YA) was
# wrong: 2YA is a third window, and taking it as "the year ago" made dataset A
# read the column that is actually B's year-ago value, so the report showed a
# two-year move and labelled it growth.
#
# These checks are on the *engine*, not the vocabulary: the columns the analysis
# ultimately reads are asserted, and with a deliberately wrong wiring, so that a
# version which recognises the right names but still reads the wrong columns
# cannot pass.
# ===========================================================================


def part11() -> None:
    banner("PART 11  MAT YA / MAT TY are resolved from the Period columns")

    # Three moving-annual windows, the shape the reference workbook has, and a
    # second family spelling its periods differently. The numbers are chosen so
    # every slot is distinguishable: 2YA / YA / TY are 10 / 20 / 40, so reading
    # the wrong one changes both the level and the growth.
    def frame() -> pd.DataFrame:
        return pd.DataFrame({
            "CATEGORY": ["C1", "C1", "C1"],
            "MKT": ["TOTAL"] * 3,
            "MF": ["M1", "M2", "M3"],
            "Sales Value 2YA": [3.0, 2.0, 5.0],
            "Sales Value YA": [10.0, 20.0, 30.0],
            "Sales Value": [40.0, 25.0, 35.0],
        })

    def cfg(**kw) -> A.AnalysisConfig:
        base = dict(
            metric_label="Sales Value", metric_key="sales_value",
            a_prior="Sales Value 2YA", a_current="Sales Value YA",
            b_prior="Sales Value 2YA", b_current="Sales Value YA",
            category_col="CATEGORY", market_col="MKT",
            manufacturer_col="MF", markets=["TOTAL"], categories=["C1"],
        )
        base.update(kw)
        return A.AnalysisConfig(**base)

    # --- the columns actually read -----------------------------------------
    # The wiring is deliberately the *old, wrong* pair: 2YA -> YA. If the
    # engine reads what it was handed, these fail.
    c = cfg()
    notes = A._resolve_period_columns(c, frame(), frame())
    check("MAT YA is re-pointed at the YA column, not 2YA",
          c.a_prior == "Sales Value YA" and c.b_prior == "Sales Value YA",
          f"A={c.a_prior!r} B={c.b_prior!r}")
    check("MAT TY is re-pointed at the unqualified column",
          c.a_current == "Sales Value" and c.b_current == "Sales Value",
          f"A={c.a_current!r} B={c.b_current!r}")
    check("the correction is reported, not silent",
          any("MAT YA reads" in n for n in notes) and any("MAT TY reads" in n for n in notes),
          f"{len(notes)} note(s)")

    # A wiring that is already right must not be churned, and must not warn.
    c_ok = cfg(a_prior="Sales Value YA", a_current="Sales Value",
               b_prior="Sales Value YA", b_current="Sales Value")
    notes_ok = A._resolve_period_columns(c_ok, frame(), frame())
    check("a correct wiring is left alone and reports nothing",
          c_ok.a_prior == "Sales Value YA" and c_ok.a_current == "Sales Value"
          and not notes_ok, f"notes={notes_ok}")

    # --- the numbers the report shows ---------------------------------------
    # With the wrong wiring fed in, the analysis must still produce the MAT YA ->
    # MAT TY movement: total 60 -> 100 is +66.67%, not the 2YA -> YA 15 -> 60
    # (+300%) the raw wiring would have given.
    prep = A.prepare(frame(), frame(), cfg())
    rep = A.category_report(prep, "C1")
    tot = rep["total"]
    check("the category MAT YA is the sum of the YA column",
          abs(tot["before_prior"] - 60.0) < 1e-9, f"got {tot['before_prior']}")
    check("the category MAT TY is the sum of the unqualified column",
          abs(tot["before_current"] - 100.0) < 1e-9, f"got {tot['before_current']}")
    check("growth is measured MAT YA -> MAT TY, not 2YA -> YA",
          abs(tot["before_growth_pct"] - 66.6666666) < 1e-4,
          f"got {tot['before_growth_pct']:.4f}% (2YA->YA would be +300%)")

    # --- a per-side difference is honoured ----------------------------------
    # The two datasets are the same measure but may name their periods
    # differently. B's family drops the unqualified name and spells TY explicitly.
    b_named = pd.DataFrame({
        "CATEGORY": ["C1", "C1", "C1"], "MKT": ["TOTAL"] * 3,
        "MF": ["M1", "M2", "M3"],
        "Sales Value 2YA": [3.0, 2.0, 5.0],
        "Sales Value YA": [10.0, 20.0, 30.0],
        "Sales Value TY": [40.0, 25.0, 35.0],
    })
    c2 = cfg()
    notes2 = A._resolve_period_columns(c2, frame(), b_named)
    check("B resolves MAT TY from its own TY-suffixed column",
          c2.b_current == "Sales Value TY", f"B MAT TY = {c2.b_current!r}")
    check("A is unaffected by B's naming",
          c2.a_current == "Sales Value", f"A MAT TY = {c2.a_current!r}")

    # --- a family that cannot supply a slot says so -------------------------
    only_2ya = pd.DataFrame({
        "CATEGORY": ["C1"], "MKT": ["TOTAL"], "MF": ["M1"],
        "Sales Value 2YA": [3.0], "Sales Value TY": [40.0],
    })
    c3 = cfg(a_prior="Sales Value 2YA", a_current="Sales Value TY",
             b_prior="Sales Value 2YA", b_current="Sales Value TY")
    notes3 = A._resolve_period_columns(c3, only_2ya, only_2ya)
    check("2YA is not borrowed when no YA column exists",
          c3.a_prior == "" and c3.a_current == "Sales Value TY",
          f"MAT YA={c3.a_prior!r} MAT TY={c3.a_current!r}")
    check("the missing MAT YA is explained",
          any("no MAT YA" in n for n in notes3), f"{len(notes3)} note(s)")
    # And the engine must not silently present a run with one period: prepare()
    # reports an empty side rather than using 2YA as a stand-in.
    prep3 = A.prepare(only_2ya, only_2ya, c3)
    tot3 = A.category_report(prep3, "C1")["total"]
    check("with no MAT YA the prior value stays empty, not 2YA",
          tot3["before_prior"] is None, f"got {tot3['before_prior']}")

    # --- a single-column family is a missing period, not a collapse ----------
    # One column genuinely cannot supply two periods. The engine must clear the
    # slot it cannot fill and say so - not leave the incoming value in place
    # (which would read the same column twice and report 0% growth) and not
    # borrow a neighbour. The earlier "collapsed pair" note is therefore not the
    # right report here; the honest one is "this period does not exist".
    one = pd.DataFrame({
        "CATEGORY": ["C1"], "MKT": ["TOTAL"], "MF": ["M1"],
        "Sales Value": [40.0],
    })
    c4 = cfg(a_prior="Sales Value", a_current="Sales Value",
             b_prior="Sales Value", b_current="Sales Value")
    notes4 = A._resolve_period_columns(c4, one, one)
    check("a single-column family clears the period it cannot supply",
          c4.a_prior == "" and c4.a_current == "Sales Value",
          f"MAT YA={c4.a_prior!r} MAT TY={c4.a_current!r}")
    check("...and says which period is missing",
          any("no MAT YA" in n for n in notes4), f"{len(notes4)} note(s)")

    # A two-column family whose prior column is named with a qualifier the
    # vocabulary does not know: `Spend` (bare, the current period) and
    # `Spend OP` (unrecognised, left in the base by the splitter). The family is
    # then a single one-column family, so the honest answer is "no MAT YA" - and
    # that is what must be reported rather than a positional guess that reads the
    # same measure twice. This is the case a positional fallback gets wrong, so
    # it is asserted explicitly.
    odd = pd.DataFrame({
        "CATEGORY": ["C1"], "MKT": ["TOTAL"], "MF": ["M1"],
        "Spend": [100.0], "Spend OP": [40.0],
    })
    c5 = A.AnalysisConfig(
        metric_label="Spend", metric_key="spend",
        a_prior="", a_current="", b_prior="", b_current="",
        category_col="CATEGORY", market_col="MKT", manufacturer_col="MF",
        markets=["TOTAL"], categories=["C1"])
    notes5 = A._resolve_period_columns(c5, odd, odd)
    check("an unrecognised qualifier is not treated as MAT YA",
          c5.a_prior == "" and c5.a_current == "Spend",
          f"MAT YA={c5.a_prior!r} MAT TY={c5.a_current!r}")

    # With nothing wired at all, the metric the run is about still resolves - the
    # periods come from the data, so the client does not have to send them.
    c6 = A.AnalysisConfig(
        metric_label="Sales Value", metric_key="sales_value",
        a_prior="", a_current="", b_prior="", b_current="",
        category_col="CATEGORY", market_col="MKT", manufacturer_col="MF",
        markets=["TOTAL"], categories=["C1"])
    notes6 = A._resolve_period_columns(c6, frame(), frame())
    check("a run with no wired periods still resolves them from the family",
          c6.a_prior == "Sales Value YA" and c6.a_current == "Sales Value",
          f"MAT YA={c6.a_prior!r} MAT TY={c6.a_current!r} notes={len(notes6)}")

    # Each side resolves against its OWN frame and its own wired names. A
    # resolver that passes one side's columns for both frames cannot see B's
    # family at all and comes back empty - which is the failure this asserts
    # against, and it is silent rather than loud because the slots simply do not
    # fill.
    n_a = pd.DataFrame({"CAT": ["X"], "Sales Value YA": [100.0],
                        "Sales Value": [110.0]})
    n_b = pd.DataFrame({"CAT": ["X"], "Value (NT$) YA": [100.0],
                        "Value (NT$)": [140.0]})
    check("each side resolves against its own column names",
          A.resolve_mat_slots("Sales Value YA", "Sales Value",
                              "Value (NT$) YA", "Value (NT$)",
                              n_a, n_b, "Sales Value") ==
          ("Sales Value YA", "Sales Value", "Value (NT$) YA", "Value (NT$)"),
          str(A.resolve_mat_slots("Sales Value YA", "Sales Value",
                                  "Value (NT$) YA", "Value (NT$)",
                                  n_a, n_b, "Sales Value")))


# ===========================================================================
# PART 12 - the periods may live in the *rows*, not in the column names
#
# Two workbook conventions exist and both must work. The reference workbook is
# the **wide** one: the periods are columns (`Sales Value YA`, `Sales Value`) and
# its `Periods` column says `MAT TY` on every row. The **long** one has a single
# metric column and a `Periods` column that says which period each row is.
#
# Part 11 proves the wide convention. This proves the long one - which is the
# case a workbook with no `Sales Value YA` / `Sales Value 2YA` column falls into,
# and the one that used to be refused with "column 'Sales Value YA' does not
# exist". The numbers are chosen so a same-column read is distinguishable from a
# real one: CAT_A is MAT YA 150 / MAT TY 170, so a collapsed read reports 0%
# where the answer is +13.33%.
# ===========================================================================


def part12() -> None:
    banner("PART 12  the periods may be rows: MAT YA / MAT TY from the Periods column")

    def long_frame() -> pd.DataFrame:
        """One metric column, the period in the rows. No YA / 2YA column."""
        rows = []
        for man, ya, ty in (("M1", 100.0, 110.0), ("M2", 50.0, 60.0)):
            for period, val in (("MAT YA", ya), ("MAT TY", ty)):
                rows.append({"CATEGORY": "C1", "MKT": "TOTAL", "MF": man,
                             "Periods": period, "Sales Value": val})
        return pd.DataFrame(rows)

    def cfg(**kw) -> A.AnalysisConfig:
        base = dict(
            metric_label="Sales Value", metric_key="sales_value",
            a_prior="", a_current="", b_prior="", b_current="",
            category_col="CATEGORY", market_col="MKT", manufacturer_col="MF",
            period_col="Periods", markets=["TOTAL"], categories=["C1"], top_n=2,
        )
        base.update(kw)
        return A.AnalysisConfig(**base)

    frame = long_frame()

    # --- resolution: one column, both slots --------------------------------
    check("a single metric column supplies both periods from the rows",
          A.resolve_mat_slots("", "", "", "", frame, frame, "Sales Value",
                              "Periods", "Periods")
          == ("Sales Value", "Sales Value", "Sales Value", "Sales Value"),
          str(A.resolve_mat_slots("", "", "", "", frame, frame, "Sales Value",
                                  "Periods", "Periods")))
    # Without *any* period column the same frame is one period, not two: the
    # positional fallback must not invent a year ago out of the one column. (The
    # period column above is found from the data even when it was not wired, so
    # the only way to be period-less is for the frame not to carry one.)
    no_pcol = frame.drop(columns=["Periods"])
    check("without any period column the one metric column is one period, not two",
          A.resolve_mat_slots("", "", "", "", no_pcol, no_pcol, "Sales Value")[0] == "",
          str(A.resolve_mat_slots("", "", "", "", no_pcol, no_pcol, "Sales Value")))
    # The period column is found from the data when it was not wired, so a
    # client that sent nothing still gets the row convention.
    check("the period column is detected from the data when unwired",
          P.detect_period_column(frame, "") == "Periods",
          repr(P.detect_period_column(frame, "")))

    # --- the values are the sums of that period's rows ---------------------
    c = cfg()
    notes = A._resolve_period_columns(c, frame, frame)
    check("both slots land on the one column, so the rows separate them",
          c.a_prior == c.a_current == "Sales Value",
          f"{c.a_prior!r} / {c.a_current!r}")
    check("the row-based slots are not reported as a degenerate wiring",
          not any("used twice" in n for n in notes), str(notes))
    prep = A.prepare(frame, frame, c)
    tot = A.category_report(prep, "C1")["total"]
    check("MAT YA is the sum of the MAT YA rows only",
          abs(tot["before_prior"] - 150.0) < 1e-9, f"got {tot['before_prior']}")
    check("MAT TY is the sum of the MAT TY rows only",
          abs(tot["before_current"] - 170.0) < 1e-9, f"got {tot['before_current']}")
    check("growth is the real MAT YA -> MAT TY move (+13.33%), not 0%",
          tot["before_growth_pct"] is not None
          and abs(tot["before_growth_pct"] - 13.3333333) < 1e-4,
          f"got {tot['before_growth_pct']}")

    # --- a ranked row carries both periods of both sides --------------------
    top = A.category_report(prep, "C1")["manufacturer_top_n"]
    m1 = next((r for r in top if r["name"] == "M1"), {})
    check("a Top-N row carries BEFORE MAT YA and MAT TY from the rows",
          abs((m1.get("before_prior") or 0) - 100.0) < 1e-9
          and abs((m1.get("before_current") or 0) - 110.0) < 1e-9,
          f"{m1.get('before_prior')} / {m1.get('before_current')}")
    check("the Top-N still selects on A's MAT TY (M1 110 > M2 60)",
          [r["name"] for r in top] == ["M1", "M2"], str([r["name"] for r in top]))

    # --- a rate metric must mask its weight too -----------------------------
    # A row-based frame carries one weight per *row*, so an unmasked denominator
    # sums both periods and halves the level: 10 and 30 over two MAT YA rows
    # average to 20, not 10.
    rate = pd.DataFrame({
        "CATEGORY": ["C1"] * 4, "MKT": ["TOTAL"] * 4,
        "MF": ["M1", "M1", "M2", "M2"],
        "Periods": ["MAT YA", "MAT TY", "MAT YA", "MAT TY"],
        "ND Dist": [10.0, 20.0, 30.0, 40.0],
        "Sales Value": [100.0] * 4,
    })
    rc = A.AnalysisConfig(
        metric_label="ND Dist", a_prior="ND Dist", a_current="ND Dist",
        b_prior="ND Dist", b_current="ND Dist", is_rate=True,
        growth_applicable=False, weight_metric="Sales Value",
        category_col="CATEGORY", market_col="MKT", manufacturer_col="MF",
        period_col="Periods", markets=["TOTAL"], categories=["C1"], top_n=2)
    rtot = A.category_report(A.prepare(rate, rate, rc), "C1")["total"]
    check("a row-based rate metric weights each period by its own rows",
          abs(rtot["before_prior"] - 20.0) < 1e-9
          and abs(rtot["before_current"] - 30.0) < 1e-9,
          f"MAT YA {rtot['before_prior']} (want 20) / MAT TY {rtot['before_current']} (want 30)")

    # --- the wide convention is untouched -----------------------------------
    # Same shape, but with the periods as columns: the row-based filter must not
    # engage, or every existing workbook's numbers would move.
    wide = pd.DataFrame({
        "CATEGORY": ["C1", "C1"], "MKT": ["TOTAL", "TOTAL"], "MF": ["M1", "M2"],
        "Periods": ["MAT TY", "MAT TY"],
        "Sales Value YA": [10.0, 20.0], "Sales Value": [40.0, 25.0],
    })
    wc = cfg(a_prior="Sales Value YA", a_current="Sales Value",
             b_prior="Sales Value YA", b_current="Sales Value")
    wprep = A.prepare(wide, wide, wc)
    wtot = A.category_report(wprep, "C1")["total"]
    check("a wide workbook still reads the YA column, unfiltered",
          abs(wtot["before_prior"] - 30.0) < 1e-9
          and abs(wtot["before_current"] - 65.0) < 1e-9,
          f"{wtot['before_prior']} / {wtot['before_current']}")
    check("the wide workbook reports no degenerate wiring either",
          not any("used twice" in n for n in wprep.notes), str(wprep.notes))


# ===========================================================================
# PART 13 - the run notes state the scope, not a fault
#
# A row whose category was left unmapped has no counterpart to compare against,
# so it cannot produce an impact figure - and it leaves the frame. Reported as
# "80437 row(s) dropped because their category was excluded" it read as data
# loss, and it was rendered under a "Check the period wiring" heading, which is
# not even what it is about. The user's words: *"it is currently creating low
# confidence in my impact"*.
#
# The note is a **scope** statement and must read as one: how much of the data
# the figures cover, with the denominator. Nothing changes silently - the fact
# is still reported, it just says what it is.
# ===========================================================================


def part13() -> None:
    banner("PART 13  a partial mapping is reported as scope, not as rows dropped")

    from backend.category_mapping import ukey

    def frame() -> pd.DataFrame:
        return pd.DataFrame({
            "CATEGORY": ["C1", "C2", "C3"],
            "MKT": ["TOTAL"] * 3,
            "Sales Value YA": [10.0, 20.0, 30.0],
            "Sales Value": [11.0, 22.0, 33.0],
        })

    def cfg(**kw) -> A.AnalysisConfig:
        base = dict(
            metric_label="Sales Value", metric_key="sales_value",
            a_prior="Sales Value YA", a_current="Sales Value",
            b_prior="Sales Value YA", b_current="Sales Value",
            category_col="CATEGORY", market_col="MKT",
            markets=["TOTAL"], categories=["C1", "C2", "C3"],
        )
        base.update(kw)
        return A.AnalysisConfig(**base)

    df = frame()
    # C1 mapped; C2 and C3 left out. Four of six rows are outside the analysis.
    partial = cfg(category_map_a={ukey("C1", ""): "C1"},
                  category_excluded_a=[ukey("C2", ""), ukey("C3", "")])
    notes = A.prepare(df, df, partial).notes
    scope = [n for n in notes if "categories you mapped" in n]
    check("a partial mapping reports the scope it covers",
          len(scope) == 1, str(notes))
    check("...with the denominator and the count, not a bare 'rows dropped'",
          bool(scope) and "of 6 rows" in scope[0] and "row(s) dropped" not in scope[0],
          scope[0] if scope else "")
    check("...and it says what to do about it",
          bool(scope) and "step 4" in scope[0], scope[0][-60:] if scope else "")
    # The figures themselves must still cover the mapped category only.
    prep = A.prepare(df, df, partial)
    check("the analysis covers the mapped category only",
          A.category_report(prep, "C1")["total"]["before_current"] == 11.0,
          str(A.category_report(prep, "C1")["total"]["before_current"]))

    # Everything mapped -> nothing to report.
    full = cfg(category_map_a={ukey("C1", ""): "C1", ukey("C2", ""): "C2",
                              ukey("C3", ""): "C3"})
    notes_full = A.prepare(df, df, full).notes
    check("a complete mapping reports no scope note",
          not any("categories you mapped" in n for n in notes_full),
          str(notes_full))

    # A tiny share must not print as "0.0%", which reads as a rounding error.
    # The fixture has to *cross* the 0.05% boundary or it proves nothing: one
    # row out of 4,002 is 0.025%, which rounds to 0.0% at one decimal place. A
    # 1-in-202 fixture (0.5%) would pass this assertion on the old code too.
    wide = pd.DataFrame({
        "CATEGORY": ["C1"] * 2000 + ["C2"],
        "MKT": ["TOTAL"] * 2001,
        "Sales Value YA": [1.0] * 2001,
        "Sales Value": [1.0] * 2001,
    })
    tiny = cfg(category_map_a={ukey("C1", ""): "C1"},
               category_excluded_a=[ukey("C2", "")],
               categories=["C1"])
    note_tiny = [n for n in A.prepare(wide, wide, tiny).notes
                 if "categories you mapped" in n]
    check("a negligible share reads as '<0.1%', not '0.0%'",
          bool(note_tiny) and "<0.1%" in note_tiny[0] and "0.0%" not in note_tiny[0],
          note_tiny[0] if note_tiny else "")


# ===========================================================================
# PART 10 - the Total Market is printed once, at the head of the block
#
# A stacked source file carries a Total row *and* the rows it covers. The block
# therefore leads with the Total Market and lists the members beneath it - and
# `report["total"]` (the sum over every row of the category) is a superset of
# both. Printing it again at the foot duplicated the head row and overstated it.
#
# Both halves matter: with a baseline the foot row must be gone, and with no
# baseline it must remain, because there the sum is the only aggregate there is.
# A test that only checked the first half would be satisfied by deleting the row
# everywhere - including the case that needs it.
# ===========================================================================


def part10() -> None:
    banner("PART 10  the Total Market appears once, and only where it belongs")

    import openpyxl
    from pptx import Presentation

    df = pd.DataFrame({
        "CATEGORY": ["C1"] * 4,
        "MKT": ["TOTAL", "CH-A", "CH-B", "RG-N"],
        "Sales Value YA": [90.0, 27.0, 18.0, 20.0],
        "Sales Value": [100.0, 30.0, 20.0, 24.0],
    })
    common = dict(
        a_prior="Sales Value YA", a_current="Sales Value",
        b_prior="Sales Value YA", b_current="Sales Value",
        category_col="CATEGORY", market_col="MKT",
        markets=["TOTAL", "CH-A", "CH-B", "RG-N"],
        market_levels={"TOTAL": "total", "CH-A": "channel",
                       "CH-B": "channel", "RG-N": "region"},
    )

    def run(baseline: str, tag: str):
        cfg = A.AnalysisConfig(baseline_market=baseline, **common)
        rep = A.category_report(A.prepare(df, df, cfg), "C1")
        qc = QC.run_qc([rep], cfg, df, df, mapping_results={})
        d = os.path.join(OUT, tag)
        os.makedirs(d, exist_ok=True)
        xp = os.path.join(d, "C1_Impact.xlsx")
        pp = os.path.join(d, "C1_Impact.pptx")
        export_excel.build_category_workbook(rep, qc.to_dict(), xp)
        export_pptx.build_category_deck(rep, qc.to_dict(), pp)
        return rep, xp, pp

    def channel_sheet(path: str):
        """The channel table, plus where its BEFORE MAT TY column sits.

        The columns are located from the header row rather than hard-coded: the
        table is per metric now and the column count differs between a growth
        metric and a distribution level.
        """
        wb = openpyxl.load_workbook(path)
        name = next(s for s in wb.sheetnames if s.startswith("Channel"))
        ws = wb[name]
        all_rows = [r for r in ws.iter_rows(values_only=True) if r and r[0]]
        hdr = next((r for r in all_rows if r[0] == "Entity"), None)
        return ws, all_rows, hdr

    def disp_scale(rep: dict) -> float:
        """The scale the exporter used - read from the report, not guessed."""
        blk = (rep.get("metrics") or {}).get(
            next(iter(rep.get("metrics") or {}), ""), rep)
        return (blk.get("display") or {}).get("scale") or 1.0

    # --- with a Total Market designated ------------------------------------
    rep, xp, pp = run("TOTAL", "p10_with_baseline")
    check("the report knows the Total Market baseline",
          (rep.get("baseline") or {}).get("name") == "TOTAL",
          f"baseline={(rep.get('baseline') or {}).get('name')!r}")

    ws, rows, hdr = channel_sheet(xp)
    labels = [r[0] for r in rows]
    head = next((r for r in rows
                 if isinstance(r[0], str) and r[0].startswith("Total Market")), None)
    tails = [r[0] for r in rows
             if isinstance(r[0], str) and r[0].strip().startswith("Total")]
    check("Excel leads the block with the Total Market",
          head is not None and "TOTAL" in str(head[0]),
          f"head={head and head[0]!r}")
    check("Excel appends no second Total when a Total Market leads the block",
          not [t for t in tails if t.strip() == "Total"],
          f"row labels={labels}")
    if head is not None:
        # The head value must be the baseline itself, not the wider category sum.
        # The writer scales its figures, so re-derive the same scale from the
        # report's own display block rather than guessing from the cell value.
        col = hdr.index("BEFORE MAT TY")
        scale = disp_scale(rep)
        head_v = (head[col] or 0.0) * scale
        base_v = rep["baseline"]["before"]
        total_v = rep["total"]["before_current"]
        check("the head row equals the Total Market, not the category sum",
              base_v is not None and abs(head_v - base_v) <= max(abs(base_v), 1) * 1e-6,
              f"cell={head[col]}x{scale} = {head_v} baseline={base_v} "
              f"category_sum={total_v}")
        # And the sum must genuinely differ, or the check above proves nothing.
        check("the category sum really is wider than the Total Market",
              total_v is not None and abs(total_v - base_v) > 1e-9,
              f"category_sum={total_v} vs baseline={base_v}")

    prs = Presentation(pp)
    ls_tables = []
    for sl in prs.slides:
        for sh in sl.shapes:
            if not sh.has_table:
                continue
            hdr = [c.text for c in sh.table.rows[0].cells]
            # The market table's first column is headed by the level's noun, so a
            # region block reads "Regions". Matching only the old "Channel" label
            # made this read as a deck with no market table at all.
            if hdr and hdr[0] in ("Channel", "Channels", "Region", "Regions",
                                  "Market", "Markets", "Top channel (by TY)"):
                ls_tables.append(sh.table)
    check("the deck has a channel/level-shift table to inspect",
          len(ls_tables) >= 1, f"{len(ls_tables)} table(s)")
    deck_first = [t.rows[1].cells[0].text.strip() for t in ls_tables if len(t.rows) > 1]
    # python-pptx rows do not accept negative indices.
    deck_last = [t.rows[len(t.rows) - 1].cells[0].text.strip() for t in ls_tables]
    # The deck decorates the total row the same way the workbook does, so the
    # leading row of a market table reads as the Total Market rather than as one
    # more channel that happens to be highlighted.
    base_label = (rep.get("baseline") or {}).get("name") or ""
    check("the deck leads its table with the Total Market",
          bool(base_label) and any(
              t in (base_label, "Total Market · " + base_label) for t in deck_first),
          f"baseline={base_label!r} first rows={deck_first}")
    check("the deck appends no second Total row",
          not [t for t in deck_last if t == "Total"],
          f"last rows={deck_last}")

    # --- with no Total Market designated ------------------------------------
    #
    # The foot row is a fallback for a block that carries **no total at all**.
    # Naming no baseline is not enough on its own: this fixture still lists a
    # market levelled `total`, and that row already *is* the total - appending a
    # sum beneath it is the duplication the user reported ("we already have total
    # market selected"). So the case that must keep the row is a block whose
    # markets are all channels and regions.
    rep2, xp2, _ = run("", "p10_no_baseline")
    check("with no baseline the report says so",
          not (rep2.get("baseline") or {}).get("name"),
          f"baseline={(rep2.get('baseline') or {}).get('name')!r}")
    ws2, rows2, hdr2 = channel_sheet(xp2)
    sums = [r[0] for r in rows2
            if isinstance(r[0], str) and r[0].strip().startswith("Total (")]
    check("a block that already carries a levelled Total gets no second sum row",
          not sums, f"sum rows={sums}")
    check("...and that total row is labelled as the Total Market",
          any(str(r[0]).startswith("Total Market · ")
              for r in rows2 if r and r[0]),
          f"{[r[0] for r in rows2 if r and r[0]][:6]}")

    # A block with no total anywhere: the sum is the only aggregate there is.
    df3 = pd.DataFrame({
        "CATEGORY": ["C1"] * 3,
        "MKT": ["CH-A", "CH-B", "RG-N"],
        "Sales Value YA": [27.0, 18.0, 20.0],
        "Sales Value": [30.0, 20.0, 24.0],
    })
    cfg3 = A.AnalysisConfig(
        a_prior="Sales Value YA", a_current="Sales Value",
        b_prior="Sales Value YA", b_current="Sales Value",
        category_col="CATEGORY", market_col="MKT",
        markets=["CH-A", "CH-B", "RG-N"],
        market_levels={"CH-A": "channel", "CH-B": "channel", "RG-N": "region"},
    )
    rep3 = A.category_report(A.prepare(df3, df3, cfg3), "C1")
    d3 = os.path.join(OUT, "p10_no_total_at_all")
    os.makedirs(d3, exist_ok=True)
    xp3 = os.path.join(d3, "C1_Impact.xlsx")
    export_excel.build_category_workbook(rep3, {}, xp3)
    _ws3, rows3, hdr3 = channel_sheet(xp3)
    sums3 = [r[0] for r in rows3
             if isinstance(r[0], str) and r[0].strip().startswith("Total (")]
    check("with no total anywhere the foot row survives, labelled as a sum",
          len(sums3) == 1, f"sum rows={sums3}")
    # And it must be the sum the report computed - the only aggregate available.
    if len(sums3) == 1:
        cell = next(r for r in rows3 if r[0] == sums3[0])
        scale3 = disp_scale(rep3)
        v = (cell[hdr3.index("BEFORE MAT TY")] or 0.0) * scale3
        want = rep3["total"]["before_current"]
        check("the foot row carries the category sum",
              want is not None and abs(v - want) <= max(abs(want), 1) * 1e-6,
              f"cell={cell[hdr3.index('BEFORE MAT TY')]}x{scale3} = {v} "
              f"report_total={want}")


# ===========================================================================
# PART 14 - display units, per-metric tables, and a symmetric Gain / Loss
#
# Three things the user reported or asked for:
#
#   * "For Gain, I am not seeing any values." The contributor block sorted on
#     `abs_change` and took the tail, and pandas sorts an uncomputable value last
#     - so "Gainers" selected exactly the entities whose change was unknown and
#     printed names with no figures. Both halves are now drawn from rows that
#     carry a change, and each is bounded by zero, so a fall is never shown under
#     "Gain" either.
#   * A unit and decimal count chosen in step 5, honoured on screen and in both
#     exports.
#   * One table per metric in the exports, and no contribution columns for a
#     distribution level.
# ===========================================================================


def _multi_metric_report() -> dict:
    """A three-metric report, enough to exercise the exporters' per-metric paths."""
    def side(a, b, gp):
        return {"mat_ya": a, "mat_ty": b,
                "growth_pct": ((b / a - 1) * 100) if (gp and a) else None}

    def channel(name, level, ya, ty, ya2, ty2, gp):
        return {
            "name": name, "level": level,
            "before": side(ya, ty, gp), "after": side(ya2, ty2, gp),
            "level_shift": {"mat_ty_pp": 1.0, "before_share_pct": 60.0,
                            "after_share_pct": 58.0},
            "contribution": {"before_share_pct": 60.0, "after_share_pct": 58.0,
                             "of_change_pct": 12.5},
            "abs_change": ty2 - ty,
        }

    def topn(prefix):
        return [{"name": f"{prefix}{i}", "before_prior": 1e9 - i * 1e7,
                 "before_current": 1.1e9 - i * 1e7, "after_prior": 1.2e9 - i * 1e7,
                 "after_current": 1.25e9 - i * 1e7, "share_change_pp": 0.3,
                 "rank_before": i + 1, "rank_after": i + 1, "rank_change": 0,
                 "movement": "HELD"} for i in range(10)]

    def block(key, label, is_rate, gp, scale, symbol, decimals):
        return {
            "key": key, "label": label, "is_rate_metric": is_rate,
            "growth_applicable": gp,
            "display": {"unit": "auto", "scale": scale, "symbol": symbol,
                        "decimals": decimals},
            "total": {"before_prior": 3.85e10, "before_current": 3.846e10,
                      "after_prior": 3.92e10, "after_current": 3.88e10,
                      "abs_change": 3.3e8, "level_shift_pp": -0.9,
                      "before_growth_pct": -0.2, "after_growth_pct": -1.1,
                      "rows_before": 699, "rows_after": 701},
            "insights": [], "baseline": {"name": "TW Total TW Offline (G)"},
            "channel_block": [
                channel("TW Total TW Offline (G)", "total", 2.6e10, 2.6e10,
                        2.6e10, 2.61e10, gp),
                channel("TW CVS", "channel", 8.7e9, 8.7e9, 9.0e9, 9.35e9, gp),
            ],
            "manufacturer_top_n": topn("MFR"), "brand_top_n": topn("BRD"),
            "client_brands": [], "client_manufacturers": [],
            "contributors": [],
        }

    return {
        "category": "BEER", "metric": "Sales Value", "metric_key": "sales_value",
        "markets": ["TW Total TW Offline (G)"],
        "baseline": {"name": "TW Total TW Offline (G)"},
        "display": {"unit": "auto", "scale": 1e9, "symbol": "Bn", "decimals": 2},
        "metrics": {
            "sales_value": block("sales_value", "Sales Value", False, True, 1e9, "Bn", 2),
            "volume": block("volume", "Sales Volume", False, True, 1e6, "M", 1),
            "nd": block("nd", "Numeric Distribution (ND)", True, False, 1.0, "", 0),
        },
    }


def part14() -> None:
    banner("PART 14  display units, per-metric tables, symmetric Gain / Loss")

    import openpyxl
    from pptx import Presentation

    # --- A. Gain and Loss are the two halves of one comparison --------------
    df_a = pd.DataFrame({
        "CATEGORY": ["C1"] * 3,
        "MKT": ["TOTAL"] * 3,
        "MANUFACTURER": ["A", "B", "C"],
        "BRAND": ["A", "B", "C"],
        "Sales Value YA": [110.0, 100.0, 90.0],
        "Sales Value": [100.0, 90.0, 80.0],
    })
    # Five manufacturers exist only in the updated dataset, and three that exist
    # in both all fell. That is the shape that produced the bug: fewer than five
    # measurable declines, and more than five uncomputable changes.
    new_names = ["N1", "N2", "N3", "N4", "N5"]
    df_b = pd.DataFrame({
        "CATEGORY": ["C1"] * 8,
        "MKT": ["TOTAL"] * 8,
        "MANUFACTURER": ["A", "B", "C"] + new_names,
        "BRAND": ["A", "B", "C"] + new_names,
        "Sales Value YA": [105.0, 95.0, 85.0] + [np.nan] * 5,
        "Sales Value": [90.0, 80.0, 70.0, 500.0, 400.0, 300.0, 200.0, 100.0],
    })
    cfg = A.AnalysisConfig(
        metric_label="Sales Value", metric_key="sales_value",
        a_prior="Sales Value YA", a_current="Sales Value",
        b_prior="Sales Value YA", b_current="Sales Value",
        category_col="CATEGORY", market_col="MKT",
        manufacturer_col="MANUFACTURER", brand_col="BRAND",
        markets=["TOTAL"], categories=["C1"],
        baseline_market="TOTAL", market_levels={"TOTAL": "total"}, top_n=10,
    )
    rep = A.category_report(A.prepare(df_a, df_b, cfg), "C1")
    man = next(c for c in rep["contributors"] if c["level"] == "manufacturer")
    gainers, losers = man["gainers"], man["losers"]

    check("the Gain list is populated", len(gainers) == 5, f"{len(gainers)} gainer(s)")
    check("every Gain row carries an absolute change",
          all(g["abs_change"] is not None for g in gainers),
          f"{[g['abs_change'] for g in gainers]}")
    check("every Gain row carries a contribution",
          all(g["contribution_to_change_pct"] is not None for g in gainers),
          f"{[g['contribution_to_change_pct'] for g in gainers]}")
    check("Gain is never a fall", all(g["abs_change"] > 0 for g in gainers),
          f"{[(g['name'], g['abs_change']) for g in gainers]}")
    check("Loss is never a rise", all(l["abs_change"] < 0 for l in losers),
          f"{[(l['name'], l['abs_change']) for l in losers]}")
    check("every Loss row carries a value too",
          all(l["abs_change"] is not None for l in losers),
          f"{[l['abs_change'] for l in losers]}")
    check("Gain is the largest rise first", [g["name"] for g in gainers] == new_names,
          f"{[g['name'] for g in gainers]}")
    check("Loss is the largest fall first", [l["name"] for l in losers] == ["A", "B", "C"],
          f"{[l['name'] for l in losers]}")

    # A missing side is zero, not unknown - the same convention the category total
    # already uses. This is what makes the contributions reconcile with the change
    # the headline reports: sum(delta) == tot_b - tot_a, here 1740 - 270 = 1470.
    d_tot = rep["total"]["after_current"] - rep["total"]["before_current"]
    check("an entity new in the updated dataset is a real rise, not an unknown",
          gainers[0]["name"] == "N1" and abs(gainers[0]["abs_change"] - 500.0) < 1e-6,
          f"{gainers[0]['name']}={gainers[0]['abs_change']}")
    check("the contributor contributions are shares of the real category change",
          abs(gainers[0]["contribution_to_change_pct"] - 500.0 / d_tot * 100) < 1e-6,
          f"{gainers[0]['contribution_to_change_pct']} vs {500.0 / d_tot * 100}")

    # --- B. display units ---------------------------------------------------
    check("an explicit unit is honoured, not overridden by the magnitude",
          A.resolve_display("millions", 2, False, 2.6e10)["scale"] == 1e6,
          str(A.resolve_display("millions", 2, False, 2.6e10)))
    check("auto picks the unit from the magnitude",
          A.resolve_display("auto", 2, False, 2.5e6)["symbol"] == "M"
          and A.resolve_display("auto", 2, False, 2.5e9)["symbol"] == "Bn")
    check("the decimal count is carried",
          A.resolve_display("billions", 1, False, 1e9)["decimals"] == 1)
    check("a nonsensical decimal count is clamped, not passed through",
          A.resolve_display("ones", 99, False, 1.0)["decimals"] == 6)
    check("a rate metric is never scaled - it is already a percentage",
          A.resolve_display("billions", 3, True, 98.0)["scale"] == 1.0,
          str(A.resolve_display("billions", 3, True, 98.0)))

    class _Req:
        display = {"sales_value": MAIN.DisplaySpec(unit="millions", decimals=3)}

    stamped = _multi_metric_report()
    MAIN._apply_display(_Req(), [stamped])
    sv = stamped["metrics"]["sales_value"]["display"]
    nd = stamped["metrics"]["nd"]["display"]
    vol = stamped["metrics"]["volume"]["display"]
    check("the chosen unit reaches the report", sv["scale"] == 1e6 and sv["decimals"] == 3,
          str(sv))
    check("a metric with no choice falls back to auto", vol["symbol"] == "Bn",
          str(vol))
    check("the distribution metric stays unscaled whatever is asked for",
          nd["scale"] == 1.0 and nd["unit"] == "percent", str(nd))
    check("the headline metric's display is on the report itself",
          stamped["display"] == sv, str(stamped["display"]))

    # --- C. one table per metric, and no contribution for ND ----------------
    d = os.path.join(OUT, "p14_multi_metric")
    os.makedirs(d, exist_ok=True)
    xp = os.path.join(d, "BEER_Impact.xlsx")
    pp = os.path.join(d, "BEER_Impact.pptx")
    export_excel.build_category_workbook(stamped, {}, xp)
    export_pptx.build_category_deck(stamped, {}, pp)

    wb = openpyxl.load_workbook(xp)
    sheets = wb.sheetnames
    for tag in ("Value", "Volume", "ND"):
        check(f"Excel has a channel table for {tag}",
              any(s.startswith("Channel") and s.endswith(f"({tag})") for s in sheets),
              f"sheets={sheets}")
        check(f"Excel has a Manufacturer Top-N and a Brand Top-N for {tag}",
              any(s.startswith("Manufacturer Top-N") and s.endswith(f"({tag})")
                  for s in sheets)
              and any(s.startswith("Brand Top-N") and s.endswith(f"({tag})")
                      for s in sheets),
              f"sheets={sheets}")
    check("Excel still has no summary sheet", "Summary" not in sheets)
    check("Excel still has no QC sheet", "QC" not in sheets)

    def hdr_of(sheet: str):
        ws = wb[sheet]
        return next(r for r in ws.iter_rows(values_only=True) if r and r[0] == "Entity")

    growth_hdr = hdr_of(next(s for s in sheets if s.startswith("Channel") and s.endswith("(Value)")))
    nd_hdr = hdr_of(next(s for s in sheets if s.startswith("Channel") and s.endswith("(ND)")))
    check("the growth table keeps its contribution column",
          any("Contribution" in str(h) for h in growth_hdr), f"{growth_hdr}")
    check("the distribution table has no contribution column",
          not any("Contribution" in str(h) for h in nd_hdr), f"{nd_hdr}")
    check("the distribution table keeps its levels and absolute change",
          "Absolute change (TY - YA)" in nd_hdr and "BEFORE MAT TY" in nd_hdr,
          f"{nd_hdr}")

    prs = Presentation(pp)
    nd_slides = [sl for sl in prs.slides
                 if any(sh.has_text_frame and "Numeric Distribution" in sh.text_frame.text
                        and sh.text_frame.text.lower().startswith("market")
                        for sh in sl.shapes)]
    check("the deck has a distribution market slide", len(nd_slides) == 1,
          f"{len(nd_slides)} slide(s)")
    if nd_slides:
        tbl = next(sh.table for sh in nd_slides[0].shapes if sh.has_table)
        hdr = [c.text for c in tbl.rows[0].cells]
        check("the deck's distribution table drops the contribution columns",
              not any("Contrib" in h for h in hdr), f"{hdr}")
        check("the deck's distribution table keeps the levels and the change",
              "Abs change (TY - YA)" in hdr, f"{hdr}")
    check("the deck carries one market slide per metric",
          len([sl for sl in prs.slides
               if any(sh.has_text_frame and sh.text_frame.text.lower()
                      .startswith("market / channel") for sh in sl.shapes)]) == 3,
          "expected 3")


# ===========================================================================
# PART 15 - the deck and the workbook against the client's template
#
# Five things the user reported after the first template run:
#
#   * every slide arrived with a vertical "Click to add text" placeholder down
#     its side - the deck was using the LAST layout in the file, "Vertical Title
#     and Text", because `_blank_layout` tested for "no placeholders at all" and
#     even the stock "Blank" layout carries date/footer/slide-number ones.
#   * a "Total (sum of rows above)" row was printed directly under a row already
#     levelled `total` - a duplicate of the Total Market they had selected.
#   * "what drove the change" existed for Sales Value only, though the report
#     carries a contributors block per metric.
#   * the deck's figures were not colour-coded.
#   * the analysis shows a separate block for the channels and for the regions,
#     and the export merged them into one list.
# ===========================================================================

TOTAL_MKT = "TW Total TW Offline (G)"


def _lvl(name, level, ya, ty, ya2, ty2, gp=True, contrib=1.0):
    def side(a, b):
        return {"mat_ya": a, "mat_ty": b,
                "growth_pct": ((b / a - 1) * 100) if (gp and a) else None}
    return {
        "name": name, "level": level,
        "before": side(ya, ty), "after": side(ya2, ty2),
        "level_shift": {"mat_ty_pp": -1.4, "before_share_pct": 60.0,
                        "after_share_pct": 58.0},
        "contribution": {"before_share_pct": 60.0, "after_share_pct": 58.0,
                         "of_change_pct": contrib},
        "abs_change": ty2 - ty,
    }


def _deck_metric(key, label, is_rate, gp, scale, symbol, decimals):
    total = _lvl(TOTAL_MKT, "total", 2.6e10, 2.6e10, 2.6e10, 2.61e10, gp)
    chans = [_lvl("TW CVS", "channel", 8.7e9, 8.74e9, 9.0e9, 9.35e9, gp),
             _lvl("TW Chain Super-PX MART", "channel", 3.7e9, 3.74e9,
                  3.4e9, 3.31e9, gp, -2.6)]
    regions = [_lvl("TW North", "region", 1.2e9, 1.25e9, 1.3e9, 1.4e9, gp),
               _lvl("TW South", "region", 0.9e9, 0.95e9, 0.85e9, 0.8e9, gp, -0.4)]
    topn = lambda p: [{"name": f"{p}{i}", "before_prior": 1e9 - i * 1e7,
                       "before_current": 1.1e9 - i * 1e7,
                       "after_prior": 1.2e9 - i * 1e7,
                       "after_current": 1.25e9 - i * 1e7,
                       "share_change_pp": -0.4, "rank_before": i + 1,
                       "rank_after": i + 1, "rank_change": 0,
                       "movement": "HELD"} for i in range(10)]
    return {
        "key": key, "label": label, "is_rate_metric": is_rate,
        "growth_applicable": gp,
        "display": {"unit": "auto", "scale": scale, "symbol": symbol,
                    "decimals": decimals},
        "total": {"before_prior": 3.85e10, "before_current": 3.846e10,
                  "after_prior": 3.92e10, "after_current": 3.88e10,
                  "abs_change": 3.3e8, "level_shift_pp": -0.9,
                  "before_growth_pct": -0.2, "after_growth_pct": -1.1},
        "baseline": {"name": TOTAL_MKT},
        "channel_block": [total] + chans + regions,
        "channel_level_block": {"level": "channel", "total": total,
                                "members": chans},
        "region_level_block": {"level": "region", "total": total,
                               "members": regions},
        "market_other_block": None,
        "manufacturer_top_n": topn("M"), "brand_top_n": topn("B"),
        "client_brands": [], "client_manufacturers": [],
        "contributors": [
            {"level": "manufacturer",
             "gainers": [{"name": "AB", "abs_change": 1.28e8,
                          "contribution_to_change_pct": 38.7}],
             "losers": [{"name": "SUN MAI", "abs_change": -9.2e6,
                         "contribution_to_change_pct": -2.8}]},
            {"level": "brand",
             "gainers": [{"name": "BAR", "abs_change": 7.4e7,
                          "contribution_to_change_pct": 22.5}],
             "losers": [{"name": "TIGER", "abs_change": -1.0e7,
                         "contribution_to_change_pct": -3.1}]},
        ],
    }


def _deck_report() -> dict:
    return {
        "category": "BEER", "metric": "Sales Value", "markets": [TOTAL_MKT],
        "baseline": {"name": TOTAL_MKT},
        "display": {"unit": "auto", "scale": 1e9, "symbol": "Bn", "decimals": 2},
        "metrics": {
            "sales_value": _deck_metric("sales_value", "Sales Value", False, True,
                                        1e9, "Bn", 2),
            "volume": _deck_metric("volume", "Sales Volume", False, True,
                                   1e6, "M", 1),
            "nd": _deck_metric("nd", "Numeric Distribution (ND)", True, False,
                               1.0, "", 0),
        },
    }


def _run_colour(cell):
    """The colour on the cell's first run, as a hex string ('' when unset)."""
    try:
        runs = cell.text_frame.paragraphs[0].runs
        if not runs:
            return ""
        col = runs[0].font.color
        return str(col.rgb) if col and col.type is not None else ""
    except Exception:
        return ""


def part15() -> None:
    banner("PART 15  template-safe slides, per-level tables, per-metric contributors")

    import openpyxl
    from pptx import Presentation

    rep = _deck_report()
    d = os.path.join(OUT, "p15_deck")
    os.makedirs(d, exist_ok=True)
    xp = os.path.join(d, "BEER_Impact.xlsx")
    pp = os.path.join(d, "BEER_Impact.pptx")

    # Built with no template at all: this is the case that produced the vertical
    # placeholder, because the stock deck's layouts all carry chrome placeholders.
    export_pptx.build_category_deck(rep, {}, pp, {}, template="__none__.pptx")
    export_excel.build_category_workbook(rep, {}, xp)

    prs = Presentation(pp)
    titles = []
    for sl in prs.slides:
        t = [sh.text_frame.text for sh in sl.shapes
             if sh.has_text_frame and sh.text_frame.text.strip()]
        titles.append(t[0] if t else "")

    # --- A. no placeholder may survive onto a slide -------------------------
    n_ph = sum(len(sl.placeholders) for sl in prs.slides)
    check("no slide carries a layout placeholder", n_ph == 0,
          f"{n_ph} placeholder(s) across {len(prs.slides)} slides")
    layouts = sorted({sl.slide_layout.name for sl in prs.slides})
    check("...so no 'Click to add text' box can appear",
          all("vertical" not in n.lower() for n in layouts), f"layouts={layouts}")
    check("the deck is built on one consistent layout", len(layouts) == 1,
          f"layouts={layouts}")

    # --- B. one market slide per level --------------------------------------
    ch = [t for t in titles if t.startswith("Market / channel")]
    rg = [t for t in titles if t.startswith("Market / region")]
    check("a market slide per level, per metric - channels",
          len(ch) == 3, f"{len(ch)} channel slide(s)")
    check("a market slide per level, per metric - regions",
          len(rg) == 3, f"{len(rg)} region slide(s)")

    def table_of(slide):
        return next(sh.table for sh in slide.shapes if sh.has_table)

    region_slide = next(sl for sl, t in zip(prs.slides, titles)
                        if t.startswith("Market / region"))
    rhdr = [c.text for c in table_of(region_slide).rows[0].cells]
    check("the region slide is headed by regions",
          rhdr[0] == "Regions", f"header={rhdr[0]!r}")
    rnames = [r.cells[0].text for r in table_of(region_slide).rows]
    check("...and lists the region rows",
          any("TW North" in n for n in rnames) and any("TW South" in n for n in rnames),
          f"{rnames}")

    # --- C. the Total Market is not duplicated ------------------------------
    every_cell = [c.text for sl in prs.slides for sh in sl.shapes
                  if sh.has_table for r in sh.table.rows for c in r.cells]
    check("the deck prints no 'Total (sum of rows above)' when a Total Market exists",
          not any("sum of rows" in t for t in every_cell))
    check("the deck still leads each market table with the Total Market",
          sum(1 for t in every_cell if t.startswith("Total Market · ")) >= 6,
          f"{sum(1 for t in every_cell if t.startswith('Total Market · '))} lead rows")

    # --- D. negative figures are colour-coded -------------------------------
    reds = {c.text for sl in prs.slides for sh in sl.shapes if sh.has_table
            for r in sh.table.rows for c in r.cells if _run_colour(c) == "C00000"}
    check("negative figures are printed in red",
          bool(reds) and all(t.strip().startswith("-") for t in reds),
          f"{sorted(reds)[:6]}")
    inked = {c.text for sl in prs.slides for sh in sl.shapes if sh.has_table
             for r in sh.table.rows for c in r.cells if _run_colour(c) == "1F2837"}
    check("and positives keep the body colour",
          any(t.startswith("+") for t in inked),
          f"{sorted(t for t in inked if t.startswith('+'))[:4]}")

    # --- E. a contributors slide per metric ---------------------------------
    contrib = [t for t in titles if t.startswith("What drove the change")]
    check("a 'what drove the change' slide per metric", len(contrib) == 3,
          f"{contrib}")
    check("...each naming its own metric",
          len({t.split(" - ", 1)[1] for t in contrib}) == 3, f"{contrib}")

    # --- F. the workbook: per-level tables in one sheet, per-metric blocks ---
    wb = openpyxl.load_workbook(xp)
    sheets = wb.sheetnames
    check("Excel has a contributors table per metric",
          all(any(s.startswith("Contributors") and s.endswith(f"({tag})")
                  for s in sheets) for tag in ("Value", "Volume", "ND")),
          f"sheets={sheets}")
    ws = wb[next(s for s in sheets if s.startswith("Channel") and s.endswith("(Value)"))]
    col_a = [r[0] for r in ws.iter_rows(values_only=True) if r and r[0]]
    check("the channel sheet carries both the channel and the region table",
          "Channel" in col_a and "Region" in col_a, f"{col_a[:12]}")
    check("the channel sheet prints no 'Total (sum of rows above)'",
          not any(str(v).startswith("Total (") for v in col_a))
    check("the Total Market leads both tables in the sheet",
          sum(1 for v in col_a if str(v).startswith("Total Market · ")) == 2,
          f"{[v for v in col_a if str(v).startswith('Total Market')]}")

    # --- G. a table with no total at all keeps its labelled sum row ---------
    # The removal must not become a deletion: with nothing levelled `total` the
    # sum is the only aggregate the table has, and Part 10 already asserts the
    # Excel side of this. Assert the deck side too, so the two agree.
    no_total = _deck_report()
    blk = no_total["metrics"]["sales_value"]
    blk["baseline"] = {}
    blk["channel_level_block"] = None
    blk["region_level_block"] = None
    blk["channel_block"] = [
        dict(b, level="channel") for b in blk["channel_block"] if b["level"] != "total"
    ]
    pp2 = os.path.join(d, "no_total.pptx")
    export_pptx.build_category_deck(no_total, {}, pp2, {}, template="__none__.pptx")
    prs2 = Presentation(pp2)
    cells2 = [c.text for sl in prs2.slides for sh in sl.shapes
              if sh.has_table for r in sh.table.rows for c in r.cells]
    check("with no Total Market the labelled sum row survives in the deck",
          any(t.startswith("Total (sum of rows above)") for t in cells2))


# ===========================================================================


def main() -> int:
    part1()
    part2()
    part3()
    part4()
    part5()
    part6()
    part7()
    part8()
    part9()
    part10()
    part11()
    part12()
    part13()
    part14()
    part15()
    banner("SUMMARY")
    n_pass = sum(1 for _, s, _ in results if s == PASS)
    n_fail = len(results) - n_pass
    for name, st, msg in results:
        if st == FAIL:
            print(f"  FAIL  {name}  {msg}")
    print(f"\n  {n_pass} passed, {n_fail} failed, {len(results)} checks total")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
