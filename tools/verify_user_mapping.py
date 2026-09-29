"""Verification: the mapping engine must not decide anything on its own.

This replaces the old ``auto_rate_pct >= 99`` assertions. Those measured how
often the machine *guessed*, and on the supplied TW workbook - which is already
harmonised - the guess was right, so the number looked like evidence of quality.
On a workbook whose two datasets define categories differently it was wrong and
still scored well (see the module docstring of backend/category_mapping.py).

So the checks here are inverted: the engine must NOT produce a mapping. Every
row starts unmapped, no code path assigns a target, and the only way a category
reaches the impact analysis is that the *user* attached a target to it.

Run:  python tools/verify_user_mapping.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from backend import category_mapping as CM
from backend import market_mapping as MK

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}" + (f"   [{detail}]" if detail else ""))
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL  {name}" + (f"   [{detail}]" if detail else ""))


def banner(t: str) -> None:
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "mapping_fixture_5cat.xlsx")


def load():
    """Read the fixture the way the app's own ingest would hand it over.

    The subcategory column is entirely empty on the A side (the fixture's
    Dataset-1 taxonomy is flat), so pandas gives NaN. `ukey` folds that to the
    empty string now - it did not always, and a key of "CAT\x1fnan" matched
    nothing the enumeration had produced.
    """
    a = pd.read_excel(FIXTURE, sheet_name="Current_MAT")
    b = pd.read_excel(FIXTURE, sheet_name="New_MAT")
    a.columns = [str(c).strip() for c in a.columns]
    b.columns = [str(c).strip() for c in b.columns]
    return a, b


# ===========================================================================
# 1. Enumeration decides nothing
# ===========================================================================

def part1() -> tuple:
    banner("PART 1  The engine enumerates; it does not decide")

    a, b = load()
    res = CM.enumerate_category_units(
        a, b, "CATEGORY", "SUBCATEGORY", "Sales Value",
        "CATEGORY", "SUBCATEGORY", "Sales Value")

    check("every Dataset-1 category becomes a row", len(res.rows) == 5,
          f"{len(res.rows)} rows: {[r.source for r in res.rows]}")

    check("no row arrives with a target attached",
          all(not r.targets for r in res.rows),
          f"{sum(len(r.targets) for r in res.rows)} targets assigned by code")

    check("no row arrives in a decided state",
          all(r.status == CM.ST_UNMAPPED for r in res.rows),
          str([(r.source, r.status) for r in res.rows]))

    check("the summary counts them all as unmapped",
          res.summary["unmapped"] == 5 and res.summary["mapped"] == 0,
          f"unmapped={res.summary['unmapped']} mapped={res.summary['mapped']}")

    check("nothing is reported as auto-resolved",
          "auto_rate_pct" not in res.summary,
          "auto_rate_pct was removed: it scored the machine's guessing")

    # The old tier cascade invented these; assert nothing replaces them.
    by = {r.source: r for r in res.rows}
    check("no pairing is invented for any row",
          all(len(r.targets) == 0 for r in res.rows),
          "including BISCUITS->WATER and DIET SUPPLEMENTS->TANDY, which the "
          "old cascade produced")

    # The evidence is still offered - as a hint, clearly labelled.
    bisc = by["BISCUITS"]
    check("a name hint is offered for BISCUITS",
          bisc.hint.get("closest_name", {}).get("category") == "TANDY",
          str(bisc.hint.get("closest_name")))
    check("the hint is labelled as a similarity, not a decision",
          "similarity_pct" in bisc.hint.get("closest_name", {}),
          f"{bisc.hint.get('closest_name', {}).get('similarity_pct')}%")

    water = by["BOTTLED WATER"]
    check("the one honest 1:1 gets a name hint of WATER",
          water.hint.get("closest_name", {}).get("category") == "WATER",
          str(water.hint.get("closest_name")))

    diet = by["DIET SUPPLEMENTS"]
    check("a category with no counterpart still gets a value hint, as evidence",
          "closest_value" in diet.hint,
          str(diet.hint.get("closest_value")))

    check("B units are enumerated as pickable targets",
          {u["label"] for u in res.b_units} >=
          {"TANDY / BISCUITS", "SAVOURY / CHIPS", "SAVOURY / NUTS",
           "BEVERAGES / CARBONATED", "WATER"},
          f"{len(res.b_units)} B units")

    check("unclaimed B units are listed as new in B",
          {u["label"] for u in res.new_in_b} >= {"PLANT BASED / TOFU",
                                                 "PLANT BASED / TEMPEH"},
          str([u["label"] for u in res.new_in_b]))

    check("subcategories are detected on both sides",
          res.has_subcategory_a and res.has_subcategory_b,
          f"A={res.has_subcategory_a} B={res.has_subcategory_b}")

    check("nothing has been resolved yet, so coverage is zero",
          res.summary["a_coverage_pct"] == 0
          and res.summary["user_resolved_pct"] == 0,
          f"A coverage {res.summary['a_coverage_pct']}% "
          f"resolved {res.summary['user_resolved_pct']}%")
    return res, a, b


# ===========================================================================
# 2. The user's decisions, and only those, move data forward
# ===========================================================================

def part2(res, a, b) -> None:
    banner("PART 2  Only the user's decisions reach the analysis")

    rows = [r.to_dict() for r in res.rows]

    def set_map(src, targets, canonical):
        for r in rows:
            if r["source"] == src:
                r["targets"] = [{"category": c, "subcategory": s, "total": None}
                                for c, s in targets]
                r["status"] = CM.ST_MAPPED
                r["canonical"] = canonical

    def find_label(cat, sub=""):
        for u in res.b_units:
            if u["category"] == cat and (u["subcategory"] or "") == sub:
                return u
        raise AssertionError(f"no B unit {cat} / {sub}")

    # The mapping the fixture requires, authored by "the user".
    set_map("BISCUITS", [("TANDY", "BISCUITS")], "Biscuits")
    set_map("SNACKS", [("SAVOURY", "CHIPS"), ("SAVOURY", "NUTS")], "Snacks")
    set_map("CARB DRINKS", [("BEVERAGES", "CARBONATED")], "Carbonated drinks")
    set_map("BOTTLED WATER", [("WATER", "")], "Bottled water")
    for r in rows:
        if r["source"] == "DIET SUPPLEMENTS":
            r["status"] = CM.ST_EXCLUDED

    s = CM.resummarise(res, rows)
    check("the summary describes the user's mapping, not a guess",
          s["mapped"] == 4 and s["excluded"] == 1 and s["unmapped"] == 0,
          f"mapped={s['mapped']} excluded={s['excluded']} unmapped={s['unmapped']}")
    check("the 1:N relationship is recognised",
          s["one_to_many"] == 1, f"one_to_many={s['one_to_many']}")
    check("the composite relationships are recognised",
          s["composite"] == 3, f"composite={s['composite']}")
    check("full coverage is reported once the user has resolved every row",
          s["user_resolved_pct"] == 100.0, f"{s['user_resolved_pct']}%")

    map_a, map_b, ex_a, ex_b = CM.resolve(rows, res.new_in_b)

    check("a mapped A unit enters the analysis",
          CM.ukey("BISCUITS", "") in map_a, str(list(map_a)))
    check("its composite B target enters under the same canonical name",
          map_b.get(CM.ukey("TANDY", "BISCUITS")) == "Biscuits",
          str(map_b.get(CM.ukey("TANDY", "BISCUITS"))))
    check("a 1:N row claims both B units",
          map_b.get(CM.ukey("SAVOURY", "CHIPS")) == "Snacks"
          and map_b.get(CM.ukey("SAVOURY", "NUTS")) == "Snacks",
          f"CHIPS={map_b.get(CM.ukey('SAVOURY','CHIPS'))} "
          f"NUTS={map_b.get(CM.ukey('SAVOURY','NUTS'))}")
    check("an excluded category is dropped from both sides",
          CM.ukey("DIET SUPPLEMENTS", "") in ex_a
          and not any(map_a.get(k) for k in map_a if "DIET" in k),
          f"excluded_a={sorted(x for x in ex_a if 'DIET' in x)}")

    # ---- the decisive check: the analysis must honour it -------------------
    from backend import analysis as A

    cfg = A.AnalysisConfig(
        metric_label="Sales Value",
        a_prior="Sales Value YA", a_current="Sales Value",
        b_prior="Sales Value YA", b_current="Sales Value",
        category_col="CATEGORY", category_col_b="CATEGORY",
        subcategory_col="SUBCATEGORY", subcategory_col_b="SUBCATEGORY",
        category_map_a=map_a, category_map_b=map_b,
        category_excluded_a=sorted(ex_a), category_excluded_b=sorted(ex_b),
        top_n=5)
    prep = A.prepare(a, b, cfg)

    check("the canonical set is exactly what the user mapped, plus nothing else",
          sorted(prep.categories) == ["Biscuits", "Bottled water",
                                      "Carbonated drinks", "Snacks"],
          str(sorted(prep.categories)) + "  (PLANT BASED is new-in-B: reported, "
          "not counted)")
    check("the excluded category is absent from the analysis",
          not any("DIET" in c.upper() for c in prep.categories),
          str(sorted(prep.categories)))

    # Biscuits: A BISCUITS vs B TANDY/BISCUITS, both sides from source
    b_t = float(b[(b["CATEGORY"] == "TANDY")
                  & (b["SUBCATEGORY"] == "BISCUITS")]["Sales Value"].sum())
    rep = A.category_report(prep, "Biscuits")
    check("a composite target reconciles against the source",
          abs(rep["total"]["after_current"] - b_t) < 1e-6,
          f"after={rep['total']['after_current']} source={b_t}")

    exp_snack = float(b[b["CATEGORY"] == "SAVOURY"]["Sales Value"].sum())
    rep_s = A.category_report(prep, "Snacks")
    check("a 1:N target sums both B units",
          abs(rep_s["total"]["after_current"] - exp_snack) < 1e-6,
          f"after={rep_s['total']['after_current']} "
          f"CHIPS+NUTS={exp_snack}")

    exp_water = float(b[b["CATEGORY"] == "WATER"]["Sales Value"].sum())
    rep_w = A.category_report(prep, "Bottled water")
    check("a straight 1:1 reconciles",
          abs(rep_w["total"]["after_current"] - exp_water) < 1e-6,
          f"after={rep_w['total']['after_current']} source={exp_water}")

    return rows


# ===========================================================================
# 3. An unmapped category is NOT silently self-mapped
# ===========================================================================

def part3(res, a, b) -> None:
    banner("PART 3  An unmapped category is excluded, not faked")

    rows = [r.to_dict() for r in res.rows]
    # Map only BOTTLED WATER; leave the rest untouched.
    for r in rows:
        if r["source"] == "BOTTLED WATER":
            r["targets"] = [{"category": "WATER", "subcategory": "", "total": None}]
            r["status"] = CM.ST_MAPPED
            r["canonical"] = "Bottled water"

    map_a, map_b, ex_a, ex_b = CM.resolve(rows, res.new_in_b)

    check("unmapped A units are recorded as excluded, not mapped to themselves",
          CM.ukey("BISCUITS", "") in ex_a
          and CM.ukey("BISCUITS", "") not in map_a,
          f"excluded {len(ex_a)} unit(s), mapped {len(map_a)}")
    check("only the one mapped row produced a lookup",
          len(map_a) == 1, str(list(map_a)))
    check("unclaimed B units are excluded, not left to report one-sided numbers",
          len(map_b) == 1 and len(ex_b) > 0,
          f"{len(map_b)} B unit(s) mapped, {len(ex_b)} excluded "
          f"(SAVOURY/TANDY/BEVERAGES/PLANT BASED)")

    unresolved = CM.unresolved_rows(rows)
    check("the unresolved rows can be named for the user",
          len(unresolved) == 4
          and {u["label"] for u in unresolved} >= {"BISCUITS", "SNACKS"},
          str([u["label"] for u in unresolved][:6]))

    from backend import analysis as A

    cfg = A.AnalysisConfig(
        metric_label="Sales Value",
        a_prior="Sales Value YA", a_current="Sales Value",
        b_prior="Sales Value YA", b_current="Sales Value",
        category_col="CATEGORY", category_col_b="CATEGORY",
        subcategory_col="SUBCATEGORY", subcategory_col_b="SUBCATEGORY",
        category_map_a=map_a, category_map_b=map_b,
        category_excluded_a=sorted(ex_a), category_excluded_b=sorted(ex_b),
        top_n=5)
    prep = A.prepare(a, b, cfg)

    check("only the mapped category reaches the analysis",
          sorted(prep.categories) == ["Bottled water"],
          str(sorted(prep.categories)))
    check("no unmapped B unit leaks in as a one-sided category",
          not any(c in prep.categories
                  for c in ("SAVOURY", "TANDY", "BEVERAGES", "PLANT BASED")),
          "mapping one row and leaving the rest still let the other B "
          "categories through under their raw names with before=None")
    check("BISCUITS did not survive as its own one-sided category",
          not any("BISCUIT" in c.upper() for c in prep.categories),
          "the old fallback kept it, so a one-sided number looked like an impact")

    # ...but a caller may opt into reporting them deliberately.
    _, mb_all, _, eb_all = CM.resolve(rows, res.new_in_b, include_new_in_b=True)
    check("new-in-B units can be opted into the analysis explicitly",
          CM.ukey("PLANT BASED", "TOFU") in mb_all
          and CM.ukey("PLANT BASED", "TOFU") not in eb_all,
          "include_new_in_b=True reports them in their own right")

    # What the user must be told before running.
    s = CM.resummarise(res, rows)
    check("the summary states how much is still unresolved",
          s["unmapped"] == 4 and s["user_resolved_pct"] == 20.0,
          f"unmapped={s['unmapped']} resolved={s['user_resolved_pct']}%")
    check("the unresolved categories are named in the summary",
          len(s["unresolved_labels"]) == 4, str(s["unresolved_labels"]))


# ===========================================================================
# 4. Market pairing is authored too, with the hierarchy advisory only
# ===========================================================================

def part4() -> None:
    banner("PART 4  Market pairing is authored; the hierarchy only advises")

    A_VALUES = ["TW Total TW Offline (G)", "TW CVS", "TW Chain Super-PX MART"]
    B_VALUES = ["TW Total TW Offline (G)", "TW CVS", "TW Chain Super-PX MART"]
    PATHS = {
        "TW Total TW Offline (G)": "TW Total TW Offline (G)",
        "TW CVS": "CVS/TW Total TW Offline (G)/MT w/o Costco",
        "TW Chain Super-PX MART": "PX MART/TW Total TW Offline (G)/MT w/o Costco",
    }
    VA = {"TW Total TW Offline (G)": 1000.0, "TW CVS": 400.0,
          "TW Chain Super-PX MART": 155.0}

    empty = MK.enumerate_markets(A_VALUES, B_VALUES, VA, VA, paths_a=PATHS,
                                 paths_b=PATHS)
    check("with no user pairings, no pairing is produced",
          empty.pairs == [], f"{len(empty.pairs)} pair(s)")
    check("the A values are still listed for the user to pick from",
          empty.a_values == sorted(A_VALUES), str(empty.a_values))
    check("the B values are listed independently",
          empty.b_values == sorted(B_VALUES), str(empty.b_values))
    check("no level is assumed", empty.summary["n_total"] == 0
          and empty.summary["n_channel"] == 0,
          f"total={empty.summary['n_total']} channel={empty.summary['n_channel']}")

    authored = MK.enumerate_markets(
        A_VALUES, B_VALUES, VA, VA, paths_a=PATHS, paths_b=PATHS,
        pairs=[{"market_a": "TW Total TW Offline (G)",
                "market_b": "TW Total TW Offline (G)", "level": "total"},
               {"market_a": "TW CVS", "market_b": "TW CVS", "level": "channel"},
               {"market_a": "TW Chain Super-PX MART",
                "market_b": "TW Chain Super-PX MART", "level": "channel"}])
    check("the user's levels are carried through unchanged",
          authored.summary["n_total"] == 1
          and authored.summary["n_channel"] == 2,
          f"total={authored.summary['n_total']} "
          f"channel={authored.summary['n_channel']}")
    check("the channels are reported as a subset of the Total",
          authored.summary["parts_are_subset"] is True,
          f"parts are {authored.summary['parts_of_total_pct']}% of the Total")

    la, lb = MK.resolve_scope(authored, "total")
    check("scoping by level returns the authored A value",
          la == ["TW Total TW Offline (G)"], str(la))
    check("and the authored B value alongside it",
          lb == ["TW Total TW Offline (G)"], str(lb))
    ca, cb = MK.resolve_scope(authored, "channel")
    check("scoping to channels returns the two authored channels",
          len(ca) == 2 and len(cb) == 2, f"A={ca} B={cb}")

    # A deliberate contradiction must be advised, never overridden.
    wrong = MK.enumerate_markets(
        A_VALUES, B_VALUES, VA, VA, paths_a=PATHS, paths_b=PATHS,
        pairs=[{"market_a": "TW CVS", "market_b": "TW CVS", "level": "total"}])
    p = wrong.pairs[0]
    check("a channel wrongly marked as a Total keeps the user's level",
          p.level == "total", f"level={p.level}")
    check("...but the hierarchy raises a contradiction",
          "contradiction" in p.evidence, str(p.evidence.get("contradiction")))
    check("the summary counts it for review",
          wrong.summary["needs_review"] == 1,
          f"needs_review={wrong.summary['needs_review']}")

    # A value whose name contains "total" but is not the root must not win.
    two = MK.enumerate_markets(
        ["TW Total TW Offline (G)", "TW Total Online", "TW CVS"],
        ["TW Total TW Offline (G)", "TW Total Online", "TW CVS"],
        pairs=[{"market_a": "TW Total Online", "market_b": "TW Total Online",
                "level": "channel"}])
    check("a misleading 'total' name does not override the user",
          two.pairs[0].level == "channel",
          f"level={two.pairs[0].level} "
          f"evidence={two.pairs[0].evidence.get('contradiction')}")

    # An unpaired A value must be surfaced, and there is no auto-pairing.
    partial = MK.enumerate_markets(
        A_VALUES, B_VALUES, VA, VA, paths_a=PATHS, paths_b=PATHS,
        pairs=[{"market_a": "TW Total TW Offline (G)",
                "market_b": "TW Total TW Offline (G)", "level": "total"}])
    check("unpaired A values are surfaced rather than silently paired",
          set(partial.summary["unpaired_a"]) == {"TW CVS",
                                                 "TW Chain Super-PX MART"},
          str(partial.summary["unpaired_a"]))
    check("no second pairing appeared on its own", len(partial.pairs) == 1,
          f"{len(partial.pairs)} pair(s)")

    # A market that exists on one side only is expressible.
    one_sided = MK.enumerate_markets(
        A_VALUES, B_VALUES, VA, VA, paths_a=PATHS, paths_b=PATHS,
        pairs=[{"market_a": "TW CVS", "market_b": "", "level": "channel"}])
    check("a market with no counterpart can be recorded without a false pairing",
          one_sided.pairs[0].market_b == ""
          and one_sided.pairs[0].level == "channel",
          "market_b is optional by design")


def main() -> int:
    res, a, b = part1()
    part2(res, a, b)
    part3(res, a, b)
    part4()
    print("\n" + "=" * 74)
    print(f"  {PASS} passed, {FAIL} failed")
    if FAILURES:
        print("  failures:")
        for f in FAILURES:
            print(f"    - {f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
