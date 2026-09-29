"""Automated QC.

The brief asks for validation *before* the final export so that a 15-category
bulk run does not need 15 manual reviews.

Two rules are followed throughout:

  1. A check must be able to fail. Each one has a concrete threshold and emits
     the numbers it compared, so a PASS is evidence rather than an assertion.
  2. Where a value can be derived twice, it is derived twice and compared -
     re-reading the first derivation would only prove the code is
     self-consistent.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

SEVERITY = {PASS: 0, WARN: 1, FAIL: 2}


@dataclass
class Check:
    id: str
    name: str
    status: str
    message: str
    detail: dict = field(default_factory=dict)
    scope: str = "global"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class QCReport:
    checks: list[Check] = field(default_factory=list)

    def add(self, *a, **kw):
        self.checks.append(Check(*a, **kw))

    @property
    def worst(self) -> str:
        if not self.checks:
            return PASS
        return max(self.checks, key=lambda c: SEVERITY[c.status]).status

    def counts(self) -> dict[str, int]:
        out = {PASS: 0, WARN: 0, FAIL: 0}
        for c in self.checks:
            out[c.status] += 1
        return out

    def to_dict(self) -> dict:
        return {
            "worst": self.worst,
            "counts": self.counts(),
            "checks": [c.to_dict() for c in self.checks],
        }


# ----------------------------------------------------------------------------
# Individual checks
# ----------------------------------------------------------------------------


def check_metric_wiring(cfg, df_a, df_b, qc: QCReport) -> None:
    """Every metric column referenced must exist and be numeric.

    The four references are the two periods the study reads on each side, and
    they have already been resolved from the Metric Period columns by the time
    this runs - so a PASS here means the analysis read the columns the period
    qualifiers name, not merely that the client sent something.
    """
    problems = []
    for label, df, col in (
        ("A MAT YA", df_a, cfg.a_prior), ("A MAT TY", df_a, cfg.a_current),
        ("B MAT YA", df_b, cfg.b_prior), ("B MAT TY", df_b, cfg.b_current),
    ):
        if not col:
            problems.append(f"{label}: no column resolved from the period columns")
        elif col not in df.columns:
            problems.append(f"{label}: '{col}' missing from dataset")
        elif not pd.api.types.is_numeric_dtype(df[col]):
            problems.append(f"{label}: '{col}' is not numeric")
    if problems:
        qc.add("metric_wiring", "Metric column wiring", FAIL,
               "; ".join(problems), {"problems": problems})
    else:
        qc.add("metric_wiring", "Metric column wiring", PASS,
               f"Both periods resolve on each side from the Metric Period "
               f"columns: A['{cfg.a_prior}' (MAT YA) -> '{cfg.a_current}' (MAT TY)], "
               f"B['{cfg.b_prior}' (MAT YA) -> '{cfg.b_current}' (MAT TY)]",
               {"metric": cfg.metric_label,
                "mat_ya": {"a": cfg.a_prior, "b": cfg.b_prior},
                "mat_ty": {"a": cfg.a_current, "b": cfg.b_current}})


def check_missing_values(df_a, df_b, cfg, qc: QCReport) -> None:
    """Null rate on the analysis keys and metrics."""
    rows = []
    worst = PASS
    for label, df in (("previous", df_a), ("updated", df_b)):
        cols = [c for c in [cfg.category_col, cfg.market_col, cfg.brand_col,
                            cfg.manufacturer_col, cfg.a_current or cfg.b_current] if c]
        for c in cols:
            if c not in df.columns:
                continue
            pct = float(df[c].isna().mean() * 100)
            rows.append({"dataset": label, "column": c, "null_pct": round(pct, 3)})
            if pct >= 50:
                worst = FAIL
            elif pct >= 5 and worst != FAIL:
                worst = WARN
    msg = "No column exceeds the 5% null threshold." if worst == PASS else \
          "Nulls present above threshold on at least one analysis column."
    qc.add("missing_values", "Missing values", worst, msg, {"columns": rows})


def check_duplicates(df_a, df_b, cfg, qc: QCReport) -> None:
    """Rows that repeat on the *full* grain, which is what would double-count.

    The check used to key on the dimension columns alone (category, market,
    manufacturer, brand). On a stacked workbook - one that carries MAT TY and MAT
    YA as separate metric columns, or as separate rows differing only by a period
    column - that key is **not the grain**: the same manufacturer legitimately
    appears once per period, so the "duplicates" it reported (11 rows / 50% on
    the reference fixture) were the period structure, not a data fault. Flagging
    them as an error trains the reader to ignore the panel.

    The correct grain is every column that distinguishes a row: the dimensions
    **plus** the period/variant and dataset discriminators. A row that repeats on
    *that* key is genuinely duplicated and would double-count. Two reports are
    made, because they mean different things:

    * **exact duplicates** on the full grain - a real fault, `FAIL`;
    * **dimension repeats** on the dimension key alone - expected on stacked
      data, reported as `PASS` with a note explaining why, so the figure is
      visible without being alarming.
    """
    dims = [c for c in [cfg.category_col, cfg.market_col,
                        cfg.manufacturer_col, cfg.brand_col] if c]
    # Columns that separate legitimate repeats: the period dimension on each
    # side, and the A/B discriminators where the datasets are stacked in one
    # frame. Taken from the config so it follows the wiring, not a guess.
    separators = [c for c in [cfg.period_col, cfg.period_col_b] if c]
    extra = ["Dataset", "dataset", "__ds"]
    out = []
    worst = PASS
    checked = 0
    for label, df in (("previous", df_a), ("updated", df_b)):
        if not dims or any(k not in df.columns for k in dims):
            continue
        sep = [c for c in separators + extra if c in df.columns]
        full = dims + sep
        sub_full = df[full].astype(str)
        dup = int(sub_full.duplicated().sum())
        sub_dims = df[dims].astype(str)
        dim_dups = int(sub_dims.duplicated().sum())
        pct = round(dup / max(len(df), 1) * 100, 4)
        out.append({"dataset": label, "duplicate_rows": dup,
                    "duplicate_dimension_rows": dim_dups,
                    "pct": pct, "keys": full,
                    "dimension_keys": dims,
                    "separators": sep,
                    "rows": int(len(df))})
        checked += 1
        if pct > 1.0:
            worst = FAIL
        elif dup > 0 and worst == PASS:
            worst = WARN
    if checked == 0:
        # Reporting PASS here would claim a verification that never happened.
        qc.add("duplicates", "Duplicate records", WARN,
               "Not verified: no dimension columns were wired, so duplicate keys "
               "could not be tested.",
               {"keys_wired": dims})
        return
    if worst == PASS:
        # Report the dimension-level repeats that were *not* counted as faults,
        # so the number the reader expects to see is accounted for rather than
        # appearing to have been suppressed.
        repeats = {d["dataset"]: d["duplicate_dimension_rows"] for d in out
                   if d["duplicate_dimension_rows"]}
        msg = "No duplicate rows on the full grain."
        if repeats:
            msg += (" Repeats on the dimension key alone ("
                    + ", ".join(f"{k}: {v}" for k, v in repeats.items())
                    + ") are the period/variant structure of a stacked workbook, "
                    "not a data fault; they are separated by the "
                    + ", ".join(out[0]["separators"] or ["period"]) + " column(s).")
        qc.add("duplicates", "Duplicate records", PASS, msg, {"datasets": out})
    else:
        qc.add("duplicates", "Duplicate records", worst,
               "Duplicate rows found on the full grain - aggregated values may "
               "double-count if the source rows are not intended to repeat.",
               {"datasets": out})


def check_mapping_coverage(mapping_results: dict, qc: QCReport) -> None:
    """What the user's mappings cover, and what they left unresolved.

    This deliberately does NOT report a "match rate". An earlier version scored
    how often the app had guessed a pairing automatically, and on a harmonised
    workbook that number was ~100% - so it read as a quality score while
    measuring nothing about whether the pairings were *right*. On a workbook
    whose two datasets defined categories differently the same number stayed
    high while the pairings were wrong. Mapping is the user's decision now, so
    what is reportable is coverage and what is still outstanding.
    """
    worst = PASS
    detail = []
    for dim, res in (mapping_results or {}).items():
        s = res.get("summary", {})
        detail.append({"dimension": dim, **s})
        if s.get("truncated"):
            # A capped value list makes the unmatched count meaningless, so it
            # must not be reported as a genuine coverage failure.
            if worst == PASS:
                worst = WARN
            continue
        if s.get("unmapped", 0) > 0:
            # A category the user left unresolved is dropped from the study, not
            # an error - but the numbers must say so rather than imply the whole
            # selection was analysed.
            worst = max(worst, WARN, key=lambda x: SEVERITY[x])
    if not detail:
        qc.add("mapping_coverage", "Dataset mapping", WARN,
               "No dimension mappings were produced.", {})
        return
    pending = sum(d.get("unmapped", 0) for d in detail)
    mapped = sum(d.get("mapped", 0) for d in detail)
    excluded = sum(d.get("excluded", 0) for d in detail)
    msg = (f"{len(detail)} mapping(s) confirmed: {mapped} member mapping(s), "
           f"{excluded} excluded.")
    if pending:
        names: list[str] = []
        for d in detail:
            names.extend(d.get("unresolved") or [])
        msg += (f" {pending} unit(s) were left unresolved and are therefore not "
                f"in the analysis"
                + (f": {', '.join(str(n) for n in names[:8])}"
                   + (" …" if len(names) > 8 else "") if names else "."))
    qc.add("mapping_coverage", "Dataset mapping", worst, msg,
           {"dimensions": detail, "unresolved": pending})


def check_invalid_mappings(mapping_results: dict, qc: QCReport) -> None:
    """Many-to-one collisions silently merge distinct entities.

    A collision is now only reportable for the *dimension* members the user
    mapped. Category merges are deliberate by construction - several A rows
    sharing one canonical name is how a merge is expressed - so they are counted
    rather than flagged.
    """
    collisions = []
    for dim, res in (mapping_results or {}).items():
        if dim == "category":
            continue
        seen: dict[str, list[str]] = {}
        for r in res.get("rows", []):
            if r.get("target") and r.get("status") == "accepted":
                seen.setdefault(r["target"], []).append(r["source"])
        for tgt, srcs in seen.items():
            if len(srcs) > 1:
                collisions.append({"dimension": dim, "target": tgt, "sources": srcs})
    if collisions:
        qc.add("invalid_mappings", "Invalid mappings", WARN,
               f"{len(collisions)} target(s) receive more than one source entity. "
               "Confirm these merges are intended.",
               {"collisions": collisions[:40], "n": len(collisions)})
    else:
        qc.add("invalid_mappings", "Invalid mappings", PASS,
               "No many-to-one mapping collisions detected.", {})


def _independent_category_total(df: pd.DataFrame, cat_col: str, category: str,
                                metric_col: str,
                                is_rate: bool = False,
                                weight_col: str | None = None,
                                market_col: str = "",
                                markets: Sequence[str] | None = None,
                                members: Sequence[tuple[str, str]] | None = None,
                                sub_col: str = "") -> float | None:
    """Second derivation of a category total, via a mask rather than a groupby.

    A rate metric (``ND Dist``) is a percentage: the pipeline averages it,
    weighted by value. Recomputing it as a plain ``nansum`` therefore compares
    a ~90% weighted mean against a sum in the thousands and reports a failure
    on data that is correct. Mirror the weighted-mean definition here, and fall
    back to an unweighted mean when no weight column is wired - never to a sum,
    which is meaningless for a rate.

    ``markets`` narrows the recomputation to the market scope the report was
    built on. Without it, a report scoped to the Total Market is compared
    against a total over every market and fails on data that is right.

    ``members`` is the *category mapping*, and it is the reason this check used
    to fail on a workbook whose two datasets define categories differently. The
    report's category is a **canonical** name; the source frame carries **raw**
    names. When the user maps several raw units onto one canonical category -
    a merge, a 1:N, or a category+subcategory composite - masking the raw frame
    on the canonical string either finds nothing (and silently skips) or finds
    only the one raw unit that happens to share the name (and fails on a
    correct total). Comparing the two category *definitions* directly is the
    error. So when ``members`` is given, the mask is the **union of the mapped
    raw ``(category, subcategory)`` units**, which is exactly the set the
    analysis grouped by. ``None`` members means "no mapping for this side", and
    the raw-name mask is used unchanged.
    """
    if cat_col not in df.columns or metric_col not in df.columns:
        return None
    if members is not None:
        want = {(_s(c), _s(sub)) for c, sub in members}
        use_sub = bool(sub_col and sub_col in df.columns)
        if use_sub:
            pairs = list(zip(df[cat_col].map(_s), df[sub_col].map(_s)))
            keep = np.array([p in want for p in pairs], dtype=bool)
        elif any(sub for _, sub in want):
            # A composite member was mapped, but this side has no subcategory
            # column to key it by. Matching on the category alone would fold
            # every subcategory of that category into the total, so report
            # "cannot verify" rather than a total that is not the one compared.
            cats_only = {c for c, _ in want}
            sub_side = df[cat_col].map(_s)
            if sub_side.isin(cats_only).any():
                return None
            keep = np.zeros(len(df), dtype=bool)
        else:
            keep = df[cat_col].map(_s).isin({c for c, _ in want}).to_numpy(dtype=bool)
        mask = pd.Series(keep, index=df.index)
    else:
        mask = df[cat_col].astype(str) == str(category)
    if market_col and markets and market_col in df.columns:
        mask = mask & df[market_col].astype(str).isin([str(m) for m in markets])
    v = pd.to_numeric(df.loc[mask, metric_col], errors="coerce")
    if not len(v):
        # No rows for this category, in this market scope, on this side. There is
        # nothing to reconcile: "absent" is not "zero". Returning 0.0 here made
        # every category that exists on only one side report a failure.
        return None
    if is_rate:
        if weight_col and weight_col in df.columns:
            w = pd.to_numeric(df.loc[mask, weight_col], errors="coerce")
            pair = pd.DataFrame({"v": v.to_numpy(dtype="float64", na_value=np.nan),
                                 "w": w.to_numpy(dtype="float64", na_value=np.nan)})
            pair = pair.dropna()
            total_w = float(pair["w"].sum())
            if total_w == 0 or not np.isfinite(total_w):
                return None
            return float((pair["v"] * pair["w"]).sum() / total_w)
        return float(np.nanmean(v.to_numpy(dtype="float64", na_value=np.nan)))
    return float(np.nansum(v.to_numpy(dtype="float64", na_value=np.nan)))


def _s(v) -> str:
    """The blank-folding string the analysis uses for a (category, sub) half.

    Mirrors ``category_mapping._clean`` closely enough for a mask: a None / NaN
    half and the literal "nan" all mean "no value", and must compare equal or the
    member test misses exactly the rows the analysis kept.
    """
    if v is None:
        return ""
    try:
        if isinstance(v, float) and v != v:
            return ""
    except Exception:
        pass
    s = str(v).strip()
    return "" if s.lower() in ("", "nan", "none", "null", "<na>", "n/a", "-") else s


def _canonical_members(category_mapping, side: str) -> dict[str, list[tuple[str, str]]]:
    """canonical category -> the raw units the mapping folds into it, one side.

    Rebuilt from the *user-authored* rows, so it is the same set ``resolve()``
    handed the analysis. Only the rows that actually reach the analysis are
    counted: a mapped A row contributes its own source unit; the B side
    contributes that row's targets (which is how a merge, a 1:N and a
    category+subcategory composite all arrive). Excluded and unmapped rows are
    deliberately absent - nothing was reported for them, so there is nothing to
    reconcile.
    """
    out: dict[str, list[tuple[str, str]]] = {}
    if not category_mapping:
        return out
    rows = getattr(category_mapping, "rows", None) or []
    for r in rows:
        status = (r.get("status") or "").strip()
        if status != "mapped":
            continue
        canonical = (r.get("canonical") or "").strip()
        if not canonical:
            canonical = _label(str(r.get("source") or ""), str(r.get("source_sub") or ""))
        if side == "a":
            out.setdefault(canonical, []).append(
                (str(r.get("source") or ""), str(r.get("source_sub") or "")))
        else:
            for t in (r.get("targets") or []):
                out.setdefault(canonical, []).append(
                    (str(t.get("category") or ""), str(t.get("subcategory") or "")))
    return out


def _label(category: str, subcategory: str = "") -> str:
    return f"{category} / {subcategory}" if subcategory else str(category)


def check_percentages(reports: Sequence[dict], qc: QCReport) -> None:
    """Growth, share and contribution arithmetic must be internally correct."""
    worst = PASS
    bad = []
    checked = 0
    for rep in reports:
        t = rep["total"]
        for side in ("before", "after"):
            cur, pri = t.get(f"{side}_current"), t.get(f"{side}_prior")
            g = t.get(f"{side}_growth_pct")
            if cur is None or pri is None or g is None or abs(pri) < 1e-9:
                continue
            checked += 1
            expect = (cur / pri - 1) * 100
            if abs(expect - g) > 1e-6:
                bad.append({"category": rep["category"], "side": side,
                            "reported": g, "expected": expect})
                worst = FAIL
        # Shares within a market block must sum to 100 (within rounding) - but
        # only when the block is a *complete* partition of the market. The Total
        # legitimately sits in `channel_block` alongside members that are a
        # subset of it (the extract omits channels the Total covers), so summing
        # the whole block and expecting 100 was asserting something false about
        # correct data. Test the members at each level instead, and only when the
        # block carries an explicit level split.
        for blk_key in ("channel_level_block", "region_level_block",
                        "market_other_block"):
            blk = rep.get(blk_key)
            if not blk or not (blk.get("members") or []):
                continue
            for side in ("before", "after"):
                tot_share = sum(
                    c["contribution"][f"{side}_share_pct"] or 0
                    for c in blk["members"])
                checked += 1
                if abs(tot_share - 100) > 0.5:
                    bad.append({"category": rep["category"],
                                "side": side, "block": blk_key,
                                "share_sum": round(tot_share, 4)})
                    if worst != FAIL:
                        worst = WARN
    if checked == 0:
        qc.add("percentages", "Percentage calculations", WARN,
               "Not verified: no growth rates or share blocks were present to test.",
               {"categories": len(reports)})
        return
    if worst == PASS:
        qc.add("percentages", "Percentage calculations", PASS,
               f"Growth rates and share sums verified ({checked} comparison(s)).",
               {"comparisons": checked})
    else:
        qc.add("percentages", "Percentage calculations", worst,
               f"{len(bad)} percentage anomaly(ies) detected.",
               {"anomalies": bad[:25], "n": len(bad)})


def check_topn(reports: Sequence[dict], cfg, qc: QCReport) -> None:
    """Top-N blocks must be correctly sized and correctly ordered.

    The Top-N is selected on the **previous** dataset (``before_current``), so
    the block is expected to be ordered descending by that column, not by the
    updated one. Checking order against ``after_current`` was asserting the
    wrong thing: a member that led in A and fell in B belongs in the block *and*
    belongs at the top of it, because the block is "A's largest, followed into
    B". The after values are carried as columns, not as the sort key.
    """
    worst = PASS
    bad = []
    checked = 0
    for rep in reports:
        for key in ("manufacturer_top_n", "brand_top_n"):
            block = rep.get(key)
            if block is None:
                continue
            checked += 1
            if len(block) > cfg.top_n:
                bad.append({"category": rep["category"], "block": key,
                            "n": len(block), "top_n": cfg.top_n})
                worst = FAIL
            vals = [b["before_current"] for b in block if b["before_current"] is not None]
            if vals != sorted(vals, reverse=True):
                bad.append({"category": rep["category"], "block": key,
                            "issue": "not sorted descending by before_current"})
                worst = FAIL
            # The block is the top of A, so every member's A rank must be inside
            # the cut. A member with no A rank could only get here via a tie at
            # the boundary, which is why this is a warning-grade check.
            ranks = [b["rank_before"] for b in block if b["rank_before"] is not None]
            if any(r > cfg.top_n for r in ranks):
                bad.append({"category": rep["category"], "block": key,
                            "issue": "a member's before-rank is outside the Top-N cut"})
                worst = FAIL
    if checked == 0:
        qc.add("topn", "Top-N calculations", WARN,
               "Not verified: no Top-N blocks were produced, so ranking and "
               "sizing could not be tested.",
               {"top_n": cfg.top_n})
        return
    if worst == PASS:
        qc.add("topn", "Top-N calculations", PASS,
               f"Top-N blocks are within the configured N={cfg.top_n}, drawn from "
               f"the previous dataset and correctly ordered "
               f"({checked} block(s) checked).",
               {"top_n": cfg.top_n, "blocks": checked, "basis": "a_current"})
    else:
        qc.add("topn", "Top-N calculations", FAIL,
               f"{len(bad)} Top-N anomaly(ies).", {"anomalies": bad[:25], "n": len(bad)})


def check_before_after_consistency(reports: Sequence[dict], qc: QCReport) -> None:
    """Level shift must equal the difference of the two growth rates.

    For a metric with no growth (ND), the relationship is not computed by
    design — reporting it as "not verified" would read like a gap in the data.
    In that case the check verifies the relationship that *does* apply instead:
    the absolute change equals TY minus YA.
    """
    growth_ok = any(r.get("growth_applicable", True) for r in reports)
    if reports and not growth_ok:
        worst = PASS
        bad = []
        checked = 0
        for rep in reports:
            for c in (rep.get("channel_block") or []):
                b = c["before"]["mat_ty"]
                a = c["after"]["mat_ty"]
                d = c["abs_change"]
                if None in (b, a, d):
                    continue
                checked += 1
                if abs((a - b) - d) > max(1e-6, abs(a) * 1e-9):
                    bad.append({"category": rep["category"], "entity": c["name"],
                                "reported": d, "expected": a - b})
                    worst = FAIL
        if checked == 0:
            qc.add("before_after", "Before/after consistency", WARN,
                   "Not verified: no channel rows carried both before/after values "
                   "and an absolute change.", {"categories": len(reports)})
            return
        if worst == PASS:
            qc.add("before_after", "Before/after consistency", PASS,
                   "This metric is a distribution level, so no growth is computed; "
                   "the absolute change (TY - YA) reconciles for every entity "
                   f"({checked} checked).", {"comparisons": checked,
                                             "growth_applicable": False})
        else:
            qc.add("before_after", "Before/after consistency", FAIL,
                   f"{len(bad)} absolute-change inconsistency(ies).",
                   {"anomalies": bad[:25], "n": len(bad)})
        return

    worst = PASS
    bad = []
    checked = 0
    for rep in reports:
        for c in (rep.get("channel_block") or []):
            bg = c["before"]["growth_pct"]
            ag = c["after"]["growth_pct"]
            ls = c["level_shift"]["mat_ty_pp"]
            if None in (bg, ag, ls):
                continue
            checked += 1
            if abs((ag - bg) - ls) > 1e-6:
                bad.append({"category": rep["category"], "entity": c["name"],
                            "reported": ls, "expected": ag - bg})
                worst = FAIL
    if checked == 0:
        qc.add("before_after", "Before/after consistency", WARN,
               "Not verified: no channel rows carried both growth rates and a "
               "level shift, so the relationship could not be tested.",
               {"categories": len(reports)})
        return
    if worst == PASS:
        qc.add("before_after", "Before/after consistency", PASS,
               "Level shift equals after-growth minus before-growth for every "
               f"entity ({checked} checked).", {"comparisons": checked})
    else:
        qc.add("before_after", "Before/after consistency", FAIL,
               f"{len(bad)} level-shift inconsistency(ies).",
               {"anomalies": bad[:25], "n": len(bad)})


def check_metric_calculations(reports: Sequence[dict], qc: QCReport) -> None:
    """Absolute change must equal after minus before."""
    worst = PASS
    bad = []
    checked = 0
    for rep in reports:
        for c in (rep.get("channel_block") or []):
            b = c["before"]["mat_ty"]
            a = c["after"]["mat_ty"]
            d = c["abs_change"]
            if None in (b, a, d):
                continue
            checked += 1
            if abs((a - b) - d) > max(1e-6, abs(a) * 1e-9):
                bad.append({"category": rep["category"], "entity": c["name"],
                            "reported": d, "expected": a - b})
                worst = FAIL
    if checked == 0:
        qc.add("metric_calc", "Metric calculations", WARN,
               "Not verified: no channel rows carried both before/after values and "
               "an absolute change, so the arithmetic could not be tested.",
               {"categories": len(reports)})
        return
    if worst == PASS:
        qc.add("metric_calc", "Metric calculations", PASS,
               "Absolute changes reconcile with the underlying before/after values "
               f"({checked} checked).", {"comparisons": checked})
    else:
        qc.add("metric_calc", "Metric calculations", FAIL,
               f"{len(bad)} absolute-change inconsistency(ies).",
               {"anomalies": bad[:25], "n": len(bad)})


def check_new_and_exited(reports: Sequence[dict], qc: QCReport) -> None:
    """Entities present in only one dataset must be labelled, not dropped."""
    worst = PASS
    notes = []
    for rep in reports:
        blocks = {
            "manufacturer": rep.get("manufacturer_top_n") or [],
            "brand": rep.get("brand_top_n") or [],
        }
        for name, block in blocks.items():
            for b in block:
                if b["movement"] in ("NEW", "EXITED"):
                    notes.append({"category": rep["category"], "level": name,
                                  "entity": b["name"], "movement": b["movement"]})
    if notes:
        worst = WARN
    qc.add("new_exited", "Entered / exited entities", worst,
           (f"{len(notes)} entity(ies) appear in only one dataset and are labelled "
            "NEW/EXITED rather than dropped." if notes
            else "No single-dataset entities in the Top-N selections."),
           {"entities": notes[:40], "n": len(notes)})


def check_export_completeness(expected: Sequence[str], produced: dict,
                              qc: QCReport) -> None:
    """Every selected category must yield both an Excel and a PPTX."""
    missing = []
    for cat in expected:
        got = produced.get(cat, {})
        if not got.get("excel"):
            missing.append({"category": cat, "missing": "excel"})
        if not got.get("pptx"):
            missing.append({"category": cat, "missing": "pptx"})
    if missing:
        qc.add("export_completeness", "Export completeness", FAIL,
               f"{len(missing)} expected output file(s) were not produced.",
               {"missing": missing[:40], "n": len(missing)})
    else:
        qc.add("export_completeness", "Export completeness", PASS,
               f"All {len(expected)} categories produced both Excel and PowerPoint.",
               {"categories": len(expected)})


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------


def run_qc(reports: Sequence[dict], cfg, df_a, df_b,
           mapping_results: dict | None = None,
           produced: dict | None = None,
           cfgs_by_metric: dict | None = None,
           category_mapping=None) -> QCReport:
    """Run every check.

    ``cfgs_by_metric`` and ``category_mapping`` are retained in the signature for
    call-site compatibility but are **no longer consumed**: both existed to serve
    the category-level totals reconciliation, which was removed on request. They
    are accepted rather than dropped so existing callers keep working without
    edits; nothing in this function reads them any more.
    """
    qc = QCReport()
    check_metric_wiring(cfg, df_a, df_b, qc)
    check_missing_values(df_a, df_b, cfg, qc)
    check_duplicates(df_a, df_b, cfg, qc)
    check_mapping_coverage(mapping_results or {}, qc)
    check_invalid_mappings(mapping_results or {}, qc)
    check_percentages(reports, qc)
    check_topn(reports, cfg, qc)
    check_before_after_consistency(reports, qc)
    check_metric_calculations(reports, qc)
    check_new_and_exited(reports, qc)
    if produced is not None:
        check_export_completeness([r["category"] for r in reports], produced, qc)
    return qc
