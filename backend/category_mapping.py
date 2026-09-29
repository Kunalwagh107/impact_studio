"""Category mapping between the previous (A) and updated (B) datasets.

The brief is explicit that the two datasets need not define categories the same
way, and that a name-equality check is not sufficient. One category in A may
correspond to:

  * one category in B                       (1:1)
  * one category + one subcategory in B     (composite)
  * several categories/subcategories in B   (1:N)
  * and, in the other direction, several A categories may collapse into one B
    category                                (N:1)
  * or have no counterpart in B at all      (excluded)

The unit of analysis is therefore a **canonical category**. Each side maps its
own ``(category, subcategory)`` tuples onto a canonical name, and the impact
analysis groups by that. 1:1, composite, 1:N and N:1 all fall out of the same
model.

WHY THIS MODULE NO LONGER DECIDES ANYTHING
------------------------------------------
An earlier version *proposed* a mapping per A category using a cascade of
tiers: exact name, category-name match, subcategory match, then - when names
disagreed - whichever B category's **total happened to be closest**, followed by
fuzzy name similarity. On the supplied TW workbook that looked perfect, because
that workbook is already harmonised and the first tier matched everything.

On a workbook whose two datasets define categories differently, it was actively
harmful. Measured on ``mapping_fixture_5cat.xlsx`` (five A categories covering
composite / 1:N / 1:1 / no-counterpart, with names deliberately divergent):

    A category        correct mapping          what the tier cascade produced
    -----------------------------------------------------------------------
    BISCUITS          TANDY / BISCUITS         WATER            (33.9% away)
    SNACKS            SAVOURY / CHIPS + NUTS    SAVOURY/...      (right, by luck)
    CARB DRINKS       BEVERAGES / CARBONATED    BEVERAGES        (drops the sub)
    BOTTLED WATER     WATER                    (none)           (left unmatched)
    DIET SUPPLEMENTS  (none - must be excluded) TANDY/BISCUITS  (INVENTED)

It bound BISCUITS to WATER and DIET SUPPLEMENTS to TANDY, lost the one honest
1:1, and still reported 87.8% A-coverage and 97.8% B-coverage - a flattering
number over a substantively wrong answer. The root cause is that "the totals are
close" is not evidence of equivalence when the whole point of the study is that
the totals moved, and a loose tolerance needed to survive genuine change will
also survive two unrelated categories that happen to be similar in size.

So this module now does three things and no more:

  1. **Enumerates** the units on each side, with their totals, so the UI can
     offer every possible target and the user can see the size of each.
  2. **Provides evidence, clearly labelled as evidence** - name similarity and
     value delta - as *hints* on a row. Hints never select a target, never set a
     status, and never move a row forward on their own.
  3. **Resolves** the mapping the *user* authored into the per-side lookups the
     analysis consumes, and reports what the user left unresolved.

Every row therefore starts with no target. ``status`` begins at ``unmapped`` and
only becomes ``mapped`` / ``excluded`` because the user said so. There is no code
path in this module that assigns a target on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Sequence

import pandas as pd

try:  # pragma: no cover
    from rapidfuzz import fuzz
    from rapidfuzz.utils import default_process as _DP

    _HAVE_RAPIDFUZZ = True
except Exception:  # pragma: no cover
    _HAVE_RAPIDFUZZ = False
    _DP = None

from .mapping import normalize_name, core_key

SEP = "\x1f"          # unit separator for composite keys


# ---------------------------------------------------------------------------
# Keys and units
# ---------------------------------------------------------------------------


# Values that mean "no subcategory". `np.nan or ""` does NOT blank a NaN - bool
# of a float NaN is True, so it survives and stringifies - and a JSON body can
# carry the literal string "nan" as well. Both must fold to the empty string, or
# the key built here differs from the key `build_units` produced for the same
# row (`"CAT\x1f"` vs `"CAT\x1fnan"`), and every lookup silently misses.
_BLANK = {"", "nan", "none", "null", "<na>", "n/a", "-"}


def _clean(v: Any) -> str:
    if v is None:
        return ""
    try:
        if isinstance(v, float) and v != v:      # NaN
            return ""
    except Exception:
        pass
    s = str(v).strip()
    return "" if s.lower() in _BLANK else s


def ukey(category: Any, subcategory: Any = "") -> str:
    """Composite key for a (category, subcategory) tuple.

    Both halves are normalised the same way ``build_units`` normalises them, so a
    key produced during enumeration and a key produced from a JSON payload agree.
    """
    return f"{_clean(category)}{SEP}{_clean(subcategory)}"


def unkey(key: str) -> tuple[str, str]:
    if SEP in key:
        c, s = key.split(SEP, 1)
        return c, s
    return key, ""


def label(category: str, subcategory: str = "") -> str:
    return f"{category} / {subcategory}" if subcategory else str(category)


@dataclass
class CategoryUnit:
    category: str
    subcategory: str
    total: float
    rows: int

    @property
    def key(self) -> str:
        return ukey(self.category, self.subcategory)

    @property
    def label(self) -> str:
        return label(self.category, self.subcategory)

    def to_dict(self) -> dict:
        return {"category": self.category, "subcategory": self.subcategory,
                "total": self.total, "rows": self.rows, "label": self.label,
                "key": self.key}


def build_units(
    df: pd.DataFrame,
    cat_col: str,
    sub_col: str | None,
    metric_col: str,
) -> list[CategoryUnit]:
    """Aggregate a dataset to its (category, subcategory) units."""
    if not cat_col or cat_col not in df.columns:
        return []
    use_sub = bool(sub_col and sub_col in df.columns)
    if metric_col and metric_col in df.columns:
        m = pd.to_numeric(df[metric_col], errors="coerce")
    else:
        m = pd.Series(0.0, index=df.index)

    tmp = pd.DataFrame({
        "c": df[cat_col].map(_clean),
        "s": df[sub_col].map(_clean) if use_sub else "",
        "m": m,
    })
    tmp = tmp[tmp["c"].ne("")]
    g = tmp.groupby(["c", "s"], dropna=False)["m"].agg(["sum", "count"]).reset_index()
    out = []
    for _, r in g.iterrows():
        out.append(CategoryUnit(str(r["c"]), str(r["s"]), float(r["sum"] or 0.0),
                                int(r["count"])))
    out.sort(key=lambda u: -abs(u.total))
    return out


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

# Status vocabulary. Only the first two carry a decision; the rest are states the
# user has not yet resolved.
ST_UNMAPPED = "unmapped"     # no target chosen yet
ST_MAPPED = "mapped"         # user chose >=1 target
ST_EXCLUDED = "excluded"     # user excluded it from the study
ST_NEW_IN_B = "new_in_b"     # a B unit no A row claims


@dataclass
class CategoryMapRow:
    """One Database-1 unit and the Dataset-2 targets the *user* attached.

    ``hint`` carries name/value observations for the user to weigh; it is not a
    score and it is never consulted when deciding a status.
    """
    source: str
    source_sub: str
    canonical: str
    targets: list[dict] = field(default_factory=list)
    status: str = ST_UNMAPPED
    hint: dict = field(default_factory=dict)
    note: str = ""
    a_total: float | None = None
    b_total: float | None = None

    @property
    def method(self) -> str:
        """How the row was resolved - descriptive only, never machine-decided.

        Reported so the workbook/UI can say *why* a row is in its state, which is
        the audit trail the old confidence score was standing in for.
        """
        n = len(self.targets)
        if self.status == ST_EXCLUDED:
            return "excluded by user"
        if n == 0:
            return "not mapped"
        if n == 1:
            t = self.targets[0]
            if t.get("subcategory"):
                return "1:1 (category + subcategory)"
            return "1:1 (category)"
        return f"1:{n}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["method"] = self.method
        d["b_sum"] = self.b_sum()
        d["delta_pct"] = _delta_pct(self.a_total, self.b_sum())
        return d

    def b_sum(self) -> float | None:
        if not self.targets:
            return None
        return float(sum(t.get("total", 0.0) or 0.0 for t in self.targets))


@dataclass
class CategoryMappingResult:
    rows: list[CategoryMapRow] = field(default_factory=list)
    new_in_b: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    a_units: list[dict] = field(default_factory=list)
    b_units: list[dict] = field(default_factory=list)
    has_subcategory_a: bool = False
    has_subcategory_b: bool = False

    def to_dict(self) -> dict:
        return {
            "rows": [r.to_dict() for r in self.rows],
            "new_in_b": self.new_in_b,
            "summary": self.summary,
            "a_units": self.a_units,
            "b_units": self.b_units,
            "has_subcategory_a": self.has_subcategory_a,
            "has_subcategory_b": self.has_subcategory_b,
        }


# ---------------------------------------------------------------------------
# Evidence helpers - used to annotate a row, never to decide it
# ---------------------------------------------------------------------------


def name_similarity(a: str, b: str) -> float:
    """Name similarity in 0..1, punctuation/case insensitive."""
    if not a or not b:
        return 0.0
    if normalize_name(a) == normalize_name(b):
        return 1.0
    if core_key(a) == core_key(b):
        return 0.97
    if not _HAVE_RAPIDFUZZ:
        return 0.0
    return float(fuzz.token_set_ratio(a, b, processor=_DP)) / 100.0


def _delta_pct(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    if abs(a) < 1e-9:
        return None
    return (b - a) / abs(a) * 100.0


def _fmt_pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.1f}%"


def _best_hint(ua: CategoryUnit, units_b: Sequence[CategoryUnit]) -> dict:
    """The closest B unit by name, plus the closest by value, as *evidence*.

    Returned in the row's ``hint`` field purely so the user has something to look
    at. Neither is applied: the UI shows them side by side and offers a "use
    this suggestion" button that the user has to press.
    """
    best_name = None
    for u in units_b:
        # Compare across the tuple: A's category may line up with B's category
        # OR with B's subcategory (the composite case), so consider all pairings.
        sc = max(
            name_similarity(ua.category, u.category),
            name_similarity(ua.subcategory, u.subcategory) if ua.subcategory else 0.0,
            name_similarity(ua.category, u.subcategory) if u.subcategory else 0.0,
        )
        if sc > 0 and (best_name is None or sc > best_name[0]):
            best_name = (sc, u)
    best_val = None
    for u in units_b:
        d = _delta_pct(ua.total, u.total)
        if d is None:
            continue
        if best_val is None or abs(d) < abs(best_val[0]):
            best_val = (d, u)

    out: dict = {}
    if best_name and best_name[0] >= 0.55:
        out["closest_name"] = {
            "category": best_name[1].category,
            "subcategory": best_name[1].subcategory,
            "label": best_name[1].label,
            "similarity_pct": round(best_name[0] * 100, 1),
        }
    if best_val:
        out["closest_value"] = {
            "category": best_val[1].category,
            "subcategory": best_val[1].subcategory,
            "label": best_val[1].label,
            "delta_pct": round(best_val[0], 1),
        }
    return out


# ---------------------------------------------------------------------------
# Enumeration - the only "analysis" this module performs
# ---------------------------------------------------------------------------


def enumerate_category_units(
    df_a: pd.DataFrame,
    df_b: pd.DataFrame,
    cat_col_a: str,
    sub_col_a: str | None,
    metric_col_a: str,
    cat_col_b: str,
    sub_col_b: str | None,
    metric_col_b: str,
) -> CategoryMappingResult:
    """List every unit on both sides for the user to map. Decides nothing.

    Each A unit becomes a row with ``targets=[]`` and ``status='unmapped'``; the
    user attaches targets. Each B unit is offered as a candidate target and, if
    no one claims it, is reported in ``new_in_b``.
    """
    units_a = build_units(df_a, cat_col_a, sub_col_a, metric_col_a)
    units_b = build_units(df_b, cat_col_b, sub_col_b, metric_col_b)
    has_sub_a = bool(sub_col_a and sub_col_a in df_a.columns
                     and df_a[sub_col_a].astype(str).str.strip().ne("").any())
    has_sub_b = bool(sub_col_b and sub_col_b in df_b.columns
                     and df_b[sub_col_b].astype(str).str.strip().ne("").any())

    rows: list[CategoryMapRow] = []
    for ua in units_a:
        rows.append(CategoryMapRow(
            source=ua.category,
            source_sub=ua.subcategory,
            canonical=label(ua.category, ua.subcategory),
            targets=[],
            status=ST_UNMAPPED,
            hint=_best_hint(ua, units_b),
            note="",
            a_total=ua.total,
            b_total=None,
        ))

    summary = _summarise(rows, units_a, units_b, [], has_sub_a, has_sub_b)
    return CategoryMappingResult(
        rows=rows,
        new_in_b=[u.to_dict() for u in units_b],
        summary=summary,
        a_units=[u.to_dict() for u in units_a],
        b_units=[u.to_dict() for u in units_b],
        has_subcategory_a=has_sub_a,
        has_subcategory_b=has_sub_b,
    )


def _summarise(
    rows: Sequence[CategoryMapRow],
    units_a: Sequence[CategoryUnit],
    units_b: Sequence[CategoryUnit],
    new_in_b: Sequence[dict],
    has_sub_a: bool,
    has_sub_b: bool,
) -> dict:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1

    a_total = float(sum(u.total for u in units_a)) or None
    # Coverage is measured over the rows that will actually reach the analysis:
    # a mapped row's breadth is its B-side total, not A's.
    resolved_a = [r for r in rows if r.status == ST_MAPPED]
    unresolved_a = [r for r in rows if r.status == ST_UNMAPPED]
    matched_a = float(sum(r.a_total or 0 for r in resolved_a)) or 0.0

    claimed: set[str] = set()
    for r in rows:
        if r.status != ST_MAPPED:
            continue
        for t in r.targets:
            claimed.add(ukey(t.get("category"), t.get("subcategory") or ""))
    b_total = float(sum(u.total for u in units_b)) or None
    claimed_b = float(sum(u.total for u in units_b if u.key in claimed)) or 0.0

    # A B unit claimed by two or more A rows is a legitimate merge (N:1) but it
    # double-counts unless intended, so it is surfaced rather than hidden.
    used: dict[str, int] = {}
    for r in rows:
        if r.status != ST_MAPPED:
            continue
        for t in r.targets:
            k = ukey(t.get("category"), t.get("subcategory") or "")
            used[k] = used.get(k, 0) + 1
    merges = {k: v for k, v in used.items() if v > 1}

    return {
        "n_a": len(units_a),
        "n_b": len(units_b),
        "mapped": counts.get(ST_MAPPED, 0),
        "unmapped": counts.get(ST_UNMAPPED, 0),
        "excluded": counts.get(ST_EXCLUDED, 0),
        "new_in_b": len(new_in_b),
        "one_to_many": sum(1 for r in rows if r.status == ST_MAPPED
                           and len(r.targets) > 1),
        "composite": sum(1 for r in rows if r.status == ST_MAPPED
                         and any(t.get("subcategory") for t in r.targets)),
        "n_to_one": len(merges),
        "a_coverage_pct": round(matched_a / a_total * 100, 2) if a_total else 0.0,
        "b_coverage_pct": round(claimed_b / b_total * 100, 2) if b_total else 0.0,
        # Named for what it is. There is no "auto rate" any more: the previous
        # summary reported one, and it read as a quality score for a mapping the
        # machine had chosen. This is the share of rows the user has resolved.
        "user_resolved_pct": round(
            (counts.get(ST_MAPPED, 0) + counts.get(ST_EXCLUDED, 0))
            / len(units_a) * 100, 2) if units_a else 0.0,
        "unresolved_labels": [label(r.source, r.source_sub) for r in unresolved_a][:50],
        "has_subcategory_a": has_sub_a,
        "has_subcategory_b": has_sub_b,
    }


def resummarise(
    result: CategoryMappingResult,
    rows: Sequence[dict],
    new_in_b: Sequence[dict] | None = None,
) -> dict:
    """Recompute the summary from user-authored rows.

    The analysis path calls this after the user's decisions arrive, so the
    coverage figures describe the mapping that was actually confirmed rather
    than the one the app would have guessed.
    """
    units_a = [CategoryUnit(u["category"], u.get("subcategory", ""),
                            float(u.get("total") or 0.0),
                            int(u.get("rows") or 0)) for u in result.a_units]
    units_b = [CategoryUnit(u["category"], u.get("subcategory", ""),
                            float(u.get("total") or 0.0),
                            int(u.get("rows") or 0)) for u in result.b_units]
    built: list[CategoryMapRow] = []
    for r in rows:
        built.append(CategoryMapRow(
            source=r.get("source") or "",
            source_sub=r.get("source_sub") or "",
            canonical=r.get("canonical") or "",
            targets=list(r.get("targets") or []),
            status=r.get("status") or ST_UNMAPPED,
            hint=r.get("hint") or {},
            note=r.get("note") or "",
            a_total=r.get("a_total"),
            b_total=r.get("b_total"),
        ))
    nb = list(new_in_b if new_in_b is not None else result.new_in_b)
    return _summarise(built, units_a, units_b, nb,
                      result.has_subcategory_a, result.has_subcategory_b)


# ---------------------------------------------------------------------------
# Resolution used by the analysis
# ---------------------------------------------------------------------------


def resolve(
    rows: Sequence[dict],
    new_in_b: Sequence[dict] | None = None,
    include_new_in_b: bool = False,
) -> tuple[dict[str, str], dict[str, str], set[str], set[str]]:
    """Turn user-authored mapping rows into per-side lookups.

    Returns ``(map_a, map_b, excluded_a, excluded_b)`` where the maps are
    ``unit-key -> canonical category``.

    Rules, in the brief's own terms:

    * ``excluded``     - the A unit is dropped from the study *and* so are the B
                         units it pointed at.
    * ``unmapped``     - the A unit has no counterpart, so it is **dropped from
                         the impact calculation**. It is deliberately *not*
                         silently mapped to itself: a category that exists on
                         only one side has no before/after comparison to make,
                         and reporting it as its own category would present a
                         one-sided number as an impact. The old behaviour kept
                         it via a fallback; that fallback is what made an
                         unmatched category look like a matched one.
    * ``mapped``       - the A unit and every B target map to the canonical name.
    * **unclaimed B units** - a B unit no mapped A row points at has no
      counterpart on the A side, so it is one-sided in exactly the same way an
      unmapped A unit is. It is therefore excluded from the analysis too.
      Without this, mapping *one* row and leaving the rest untouched still let
      the other B categories through under their raw names, and they came back
      with ``before: None`` - a one-sided number presented as an impact, which
      is the specific thing this step exists to prevent. They are reported as
      new-in-B instead, so they are visible without being counted.

      ``include_new_in_b`` opts them into the analysis deliberately, for a caller
      that wants the new-only categories reported in their own right.
    """
    map_a: dict[str, str] = {}
    map_b: dict[str, str] = {}
    excluded_a: set[str] = set()
    excluded_b: set[str] = set()

    for r in rows or []:
        canonical = (r.get("canonical") or "").strip()
        status = (r.get("status") or "").strip()
        targets = r.get("targets") or []
        src_key = ukey(r.get("source"), r.get("source_sub") or "")
        if not canonical:
            canonical = label(str(r.get("source") or ""), str(r.get("source_sub") or ""))

        if status == ST_EXCLUDED:
            excluded_a.add(src_key)
            for t in targets:
                excluded_b.add(ukey(t.get("category"), t.get("subcategory") or ""))
            continue

        if status != ST_MAPPED or not targets:
            # Unmapped (or excluded-without-targets): no comparison is possible,
            # so it does not enter the analysis.
            if status == ST_UNMAPPED:
                excluded_a.add(src_key)
            continue

        map_a[src_key] = canonical
        for t in targets:
            map_b[ukey(t.get("category"), t.get("subcategory") or "")] = canonical

    # Unclaimed B units are one-sided; exclude them rather than let them report
    # an "after" with no "before".
    claimed_b = set(map_b)
    for u in new_in_b or []:
        k = ukey(u.get("category"), u.get("subcategory") or "")
        if k in claimed_b:
            continue
        if include_new_in_b:
            map_b.setdefault(k, label(str(u.get("category") or ""),
                                      str(u.get("subcategory") or "")))
        else:
            excluded_b.add(k)

    return map_a, map_b, excluded_a, excluded_b


def unresolved_rows(rows: Sequence[dict]) -> list[dict]:
    """The rows the user has neither mapped nor excluded.

    Returned so the UI can name them before a run rather than letting them
    vanish from the numbers with no explanation.
    """
    out = []
    for r in rows or []:
        if (r.get("status") or "") == ST_UNMAPPED:
            out.append({"source": r.get("source"),
                        "source_sub": r.get("source_sub") or "",
                        "label": label(str(r.get("source") or ""),
                                       str(r.get("source_sub") or ""))})
    return out
