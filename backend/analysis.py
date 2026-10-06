"""Impact analysis engine.

Produces the before/after comparison the brief describes, at category level,
with the blocks visible in the reference layout:

  * Market / Channel block   BEFORE(MAT YA, MAT TY, Growth) x
                             AFTER(MAT YA, MAT TY, Growth) +
                             Level Shift(delta pp, share before, share after) +
                             Contribution(share before/after, share of change)
                             -- one block per level, the Total shown once at
                             the top and never repeated among its own members
  * Market / Region block     the same shape, when the user paired regions
  * Manufacturer Top-N       rank before/after, movement class, entered/exited
                             (selected on the previous dataset, followed into
                             the updated one)
  * Client brands            tracked independently of Top-N

There is deliberately no brand *value share* block: it was removed on request.
Brand Top-N and the client-brand tracker remain - they answer different
questions and neither is a share-of-category presentation.

The whole dataset is aggregated once per dataset and every category report is a
slice of that aggregation, so a 150-category bulk export does not re-scan the
raw rows 150 times.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Sequence

import numpy as np
import pandas as pd

from .profiling import (
    MAT_TY,
    MAT_YA,
    classify_period_value,
    default_period_columns,
    detect_period_column,
    period_slots_present,
    split_metric_name,
)

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------

# How many entities each side of the "what drove the change" block lists. One
# number for both sides, so Gain and Loss are always the same size and read as
# the two halves of one comparison rather than as two unrelated lists.
CONTRIBUTOR_N = 5

# Display units a report may be presented in. `auto` picks from the magnitude;
# the rest are the user's explicit choice in step 5, so Sales Value can be read
# in billions on every sheet of a run instead of per-sheet.
DISPLAY_UNITS: dict[str, tuple[float, str]] = {
    "ones": (1.0, ""),
    "thousands": (1e3, "K"),
    "millions": (1e6, "M"),
    "billions": (1e9, "Bn"),
}


def auto_unit(magnitude: float) -> tuple[float, str]:
    """The display unit implied by the largest figure in a run."""
    try:
        mx = abs(float(magnitude))
    except Exception:
        mx = 0.0
    if mx >= 1e9:
        return 1e9, "Bn"
    if mx >= 1e6:
        return 1e6, "M"
    if mx >= 1e3:
        return 1e3, "K"
    return 1.0, ""


def resolve_display(unit: str, decimals: int, is_rate: bool,
                    magnitude: float = 0.0) -> dict:
    """The one description of how a metric's numbers are written.

    Returned to the browser and to both exporters, so the screen, the workbook
    and the deck cannot disagree about whether a figure is in millions or
    billions. A rate metric is a percentage, not a quantity, so it is never
    scaled: its unit is the percent it already carries.
    """
    if is_rate:
        return {"unit": "percent", "scale": 1.0, "symbol": "", "decimals": 0}
    name = (unit or "auto").strip().lower()
    if name in DISPLAY_UNITS:
        factor, symbol = DISPLAY_UNITS[name]
    else:
        name = "auto"
        factor, symbol = auto_unit(magnitude)
    try:
        dp = int(decimals)
    except Exception:
        dp = 2
    return {"unit": name, "scale": factor, "symbol": symbol,
            "decimals": max(0, min(dp, 6))}


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------


@dataclass
class AnalysisConfig:
    # metric wiring - the same logical metric, per dataset and per period
    metric_label: str = "Sales Value"
    metric_key: str = ""             # stable id: sales_value | volume | nd
    a_prior: str = ""
    a_current: str = ""
    b_prior: str = ""
    b_current: str = ""
    is_rate: bool = False
    weight_metric: str = ""          # for rate metrics: weight column name (same role)
    weight_metric_b: str = ""        # weight column on the B side, if named differently
    # Whether a percentage growth is the right impact measure for this metric.
    # Numeric Distribution is a distribution level, not an accumulating quantity, so
    # "growth" is meaningless: the report shows Top / TY / YA / absolute change
    # (TY - YA) and omits growth, level shift and growth-based contribution. This is
    # separate from ``is_rate`` (which only decides how the metric aggregates) so a
    # future rate metric that *does* want growth can still have it.
    growth_applicable: bool = True

    # dimension wiring. The bare names describe dataset A; the *_b names describe
    # dataset B and fall back to the A name when unset, because the two datasets
    # need not use the same column names for the same concept.
    category_col: str = ""
    market_col: str = ""
    manufacturer_col: str = ""
    brand_col: str = ""
    subcategory_col: str = ""
    period_col: str = ""
    category_col_b: str = ""
    market_col_b: str = ""
    manufacturer_col_b: str = ""
    brand_col_b: str = ""
    subcategory_col_b: str = ""
    period_col_b: str = ""

    # selections
    markets: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    top_n: int = 10
    client_brands: list[str] = field(default_factory=list)

    # Market hierarchy. ``market_levels`` maps a market value to its level
    # (total | region | channel) and ``baseline_market`` names the market value
    # that represents the Total Market. Share and contribution are measured
    # against the baseline rather than against the sum of whatever entities
    # happen to be in a block - adding the Total to a block of channels
    # double-counts it, and dividing by that sum understates every share.
    market_levels: dict[str, str] = field(default_factory=dict)
    baseline_market: str = ""
    # The B side may name the same markets differently, so the scope carries a
    # value list per side. Empty means "use the A list on both sides", which is
    # the case when the two datasets share a market vocabulary.
    markets_b: list[str] = field(default_factory=list)
    # B's name for the Total Market, when the two sides differ.
    baseline_market_b: str = ""

    # confirmed dimension-member mappings: dimension -> {source_value: canonical}
    mapping_a: dict[str, dict[str, str]] = field(default_factory=dict)
    mapping_b: dict[str, dict[str, str]] = field(default_factory=dict)

    # confirmed category mapping, keyed by ukey(category, subcategory) -> canonical
    category_map_a: dict[str, str] = field(default_factory=dict)
    category_map_b: dict[str, str] = field(default_factory=dict)
    category_excluded_a: list[str] = field(default_factory=list)
    category_excluded_b: list[str] = field(default_factory=list)

    # trend support
    trend_enabled: bool = False
    trend_a_metric: str = ""
    trend_b_metric: str = ""

    def col_a(self, attr: str) -> str:
        return getattr(self, attr, "") or ""

    def col_b(self, attr: str) -> str:
        """The B-side column, falling back to the A-side name."""
        return getattr(self, f"{attr}_b", "") or getattr(self, attr, "") or ""

    def to_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------------------
# Aggregation helpers
# ----------------------------------------------------------------------------


def _aggregate(
    df: pd.DataFrame,
    group_cols: list[str],
    value_cols: dict[str, str],
    is_rate: bool,
    weight_col: str | None = None,
) -> pd.DataFrame:
    """Aggregate a frame. Sums additive metrics; weights rate metrics.

    ``value_cols`` maps output name -> source column, e.g.
    {'prior': 'Sales Value YA', 'current': 'Sales Value'}.

    ``weight_col`` is either one column name serving both slots, or a
    ``{slot: column}`` map for a row-based fact table where each period's rows
    carry their own weight. Getting this wrong on such a frame is silent: the
    denominator sums both periods and every rate comes back halved.
    """
    # An empty input still has to carry the group columns. Callers merge two
    # aggregated frames on those columns (``_entity_block`` does
    # ``ga.merge(gb, on=dim)``), so returning only prior/current raises
    # KeyError on the merge key the moment one side is empty - which is the
    # normal case when a category or market exists on only one dataset.
    if df.empty or not group_cols:
        return pd.DataFrame(columns=list(group_cols) + list(value_cols))

    use = [c for c in group_cols if c in df.columns]
    if not use:
        return pd.DataFrame(columns=list(group_cols) + list(value_cols))

    d = df
    if is_rate:
        # weighted average: sum(w * x) / sum(w)
        #
        # Group on the real columns rather than joining them into a "__g" string
        # and splitting it back apart. The string round-trip was silently
        # corrupting values: `.str.split("||", expand=True)` treats its argument
        # as a REGEX, so the two-character "||" matched either bar and a value of
        # "M1" came back as ["M", "1", None, None] - four columns where one was
        # expected, which then failed the `keys.columns = use` assignment.
        parts = []
        for out_name, col in value_cols.items():
            if col not in d.columns:
                continue
            x = pd.to_numeric(d[col], errors="coerce")
            # A per-slot weight map: the row-based convention masks each slot's
            # weight by its own period, so the denominator cannot include the
            # other period's rows.
            wname = weight_col.get(out_name) if isinstance(weight_col, dict) else weight_col
            w = d[wname] if wname and wname in d.columns else None
            tmp = d[use].copy()
            if w is not None:
                ww = pd.to_numeric(w, errors="coerce")
                tmp["__num"] = x * ww
                tmp["__den"] = ww
                g = tmp.groupby(use, dropna=False)[["__num", "__den"]].sum()
                parts.append((out_name, g["__num"] / g["__den"].replace(0, np.nan)))
            else:
                tmp["__num"] = x
                parts.append((out_name, tmp.groupby(use, dropna=False)["__num"].mean()))
        if not parts:
            return pd.DataFrame(columns=list(group_cols) + list(value_cols))
        out = pd.concat([p[1] for p in parts], axis=1)
        out.columns = [p[0] for p in parts]
        return out.reset_index()

    cols = [c for c in value_cols.values() if c in d.columns]
    if not cols:
        return pd.DataFrame(columns=list(group_cols) + list(value_cols))
    g = d.groupby(use, dropna=False)[cols].sum(numeric_only=True).reset_index()
    rename = {v: k for k, v in value_cols.items()}
    return g.rename(columns=rename)


def _safe_growth(current: float, prior: float) -> float | None:
    """Percentage growth, None when the base is unusable."""
    try:
        if prior is None or current is None:
            return None
        if not np.isfinite(prior) or not np.isfinite(current):
            return None
        if abs(prior) < 1e-9:
            return None
        return (current / prior - 1.0) * 100.0
    except Exception:
        return None


def _f(v: Any) -> float | None:
    try:
        if v is None:
            return None
        fv = float(v)
        return fv if np.isfinite(fv) else None
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Prepared state
# ----------------------------------------------------------------------------


@dataclass
class Prepared:
    cfg: AnalysisConfig
    a: pd.DataFrame
    b: pd.DataFrame
    categories: list[str] = field(default_factory=list)
    markets: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # category -> (baseline_before, baseline_after), measured on the Total Market
    # rows *before* the market-scope filter, so the denominator still exists when
    # the scope excludes the Total (e.g. analysing channels only). Used for the
    # blocks *within* a category.
    baseline: dict[str, tuple[float | None, float | None]] = field(default_factory=dict)
    # The Total Market across every category in scope - the denominator for a
    # category's own contribution. Per category it would be trivially 100%, so
    # the category-level figure has to be measured against the whole market.
    baseline_total: tuple[float | None, float | None] = (None, None)
    baseline_name: str = ""
    baseline_verified: bool = False
    # How many categories the baseline covers. Shares are of the Total Market
    # *in scope*, so analysing a subset of categories means the shares sum to
    # 100% of that subset - which has to be visible, not implied.
    baseline_n_categories: int = 0


def _canonicalise(
    df: pd.DataFrame,
    dim_map: dict[str, dict[str, str]],
    dim_cols: dict[str, str],
) -> pd.DataFrame:
    """Return a frame with canonical dimension values + prior/current metrics."""
    out = pd.DataFrame(index=df.index)
    for dim, col in dim_cols.items():
        if not col or col not in df.columns:
            out[dim] = ""
            continue
        s = df[col].astype(str).str.strip()
        mp = dim_map.get(dim, {})
        if mp:
            s = s.map(lambda v: mp.get(v, v))
        out[dim] = s
    return out


def _canonical_category(
    dims: pd.DataFrame,
    cat_map: dict[str, str] | None,
    excluded: Sequence[str] | None,
) -> pd.Series:
    """Map each row's (category, subcategory) onto a canonical category.

    This is what makes a category split across several Dataset-2 tuples - or
    several Dataset-1 categories collapsing into one - compare correctly. With
    no mapping supplied the raw category name is returned unchanged, so the
    feature is inert until the user confirms one.
    """
    from .category_mapping import ukey

    cat = dims["category"].astype(str).str.strip()
    if "subcategory" in dims.columns:
        sub = dims["subcategory"].astype(str).str.strip()
    else:
        sub = pd.Series("", index=dims.index)
    if not cat_map and not excluded:
        return cat
    ex = set(excluded or [])
    out = []
    for c, s in zip(cat, sub):
        k = ukey(c, "" if s in ("nan", "None") else s)
        if k in ex:
            out.append(None)
        else:
            out.append((cat_map or {}).get(k, c))
    return pd.Series(out, index=dims.index)


def _period_column(df: pd.DataFrame, wired: str = "") -> str:
    """The column whose values name the study's two periods, on one frame.

    The wired period dimension when it carries MAT YA / MAT TY, otherwise the
    column the frame itself offers. The periods are resolved from the data, so a
    client that sent no period column must not make the row-based convention
    unreachable.
    """
    return detect_period_column(df, wired or "")


def period_discriminates(df: pd.DataFrame, period_col: str,
                         prior_col: str, current_col: str) -> bool:
    """True when the *rows*, not the column name, tell the two periods apart.

    Both slots landing on one column is normally a wiring fault - the same
    measure read twice, so every growth rate reads 0% and looks like a flat
    market. It is legitimate in exactly one case: a fact table whose ``Periods``
    column carries both MAT YA and MAT TY as separate rows. This is the single
    definition of that case, used both to apply the period filter in
    :func:`prepare` and to suppress the degenerate-wiring warning.
    """
    if not prior_col or prior_col != current_col:
        return False
    slots = period_slots_present(df, _period_column(df, period_col))
    return MAT_YA in slots and MAT_TY in slots


def _period_mask(df: pd.DataFrame, period_col: str,
                 period_value: str) -> pd.Series:
    """Boolean row mask for one period. All-True when no period is named.

    An empty ``period_value`` means the column-name convention, where the row
    set needs no restriction - the column *is* the period. Returning an all-True
    mask rather than ``None`` lets every caller apply it unconditionally.
    """
    if not period_value or not period_col or period_col not in df.columns:
        return pd.Series(True, index=df.index)
    return df[period_col].map(classify_period_value) == period_value


def _slot_series(df: pd.DataFrame, col: str, period_value: str,
                 period_col: str) -> pd.Series | float:
    """One period's values: the metric column, restricted to that period's rows.

    ``period_value`` is empty for the column-name convention, where the row set
    needs no restriction - the column *is* the period.
    """
    if not col or col not in df.columns:
        return np.nan
    x = pd.to_numeric(df[col], errors="coerce")
    if not period_value or not period_col or period_col not in df.columns:
        return x
    return x.where(_period_mask(df, period_col, period_value))


def _weight_map(frame: pd.DataFrame) -> str | dict[str, str] | None:
    """The weight column(s) an aggregation should use for each slot.

    A plain column name when one weight serves both slots (the column-name
    convention), or ``{slot: column}`` when each period's rows carry their own
    weight (the row-based convention, where an unmasked denominator would
    average the other period in and halve the rate).
    """
    if "weight_prior" in frame.columns and "weight_current" in frame.columns:
        return {"prior": "weight_prior", "current": "weight_current"}
    return "weight" if "weight" in frame.columns else None


def resolve_mat_slots(
    a_prior: str, a_current: str, b_prior: str, b_current: str,
    df_a: pd.DataFrame, df_b: pd.DataFrame, metric_label: str = "",
    period_col_a: str = "", period_col_b: str = "",
) -> tuple[str, str, str, str]:
    """Resolve the two period slots the study reads, from the Period columns.

    The single definition of the rule. Both ``prepare`` (through
    ``_resolve_period_columns``) and the API's pre-flight validation call this,
    so the columns the run is *checked* against are the columns it *reads*, and
    the two can never drift apart.

    Each side is resolved against **its own** wired columns and its **own**
    frame: the two datasets may name the same metric differently (``Sales
    Value`` on A, ``Value (NT$)`` on B), and passing one side's names for the
    other silently resolves B against a family it does not have.

    **Where the periods come from.** Two workbook conventions exist and both
    must work:

    * **wide** - the periods are columns. ``Sales Value YA`` is MAT YA and the
      unqualified ``Sales Value`` is MAT TY, so each slot names its own column.
    * **long** - the periods are rows. There is one metric column and the fact
      table's ``Periods`` column says which period each row is. A slot the
      family cannot name is then supplied by the *same* metric column, read
      twice and restricted to that period's rows by :func:`prepare`.

    A slot neither convention can supply comes back empty rather than being
    guessed, so the caller reports it instead of reading a column twice and
    presenting a plausible 0%.

    Returns ``(a_prior, a_current, b_prior, b_current)`` - MAT YA and MAT TY on
    each side.
    """
    def families(df: pd.DataFrame) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        for col in df.columns:
            if not pd.api.types.is_numeric_dtype(df[col]):
                continue
            base, variant = split_metric_name(col)
            out.setdefault(base, {})[variant or "VALUE"] = col
        return out

    def resolve(fams: dict[str, dict[str, str]], wired: str,
                other: str, df: pd.DataFrame, period_col: str) -> tuple[str, str]:
        # The base name of whichever wired column this frame actually has. The
        # wired column may itself be the odd one out (a 2YA), whose base name is
        # still the family we want. A family dict maps variant -> *column*, so
        # the test is whether the wired column is one of its values - checking
        # its keys would compare a column name against variant labels like 'YA'.
        base = next((base for base, f in fams.items()
                     if wired in f.values() or other in f.values()), "")
        fam = fams.get(base, {})
        if not fam:
            # The named metric, if the frame has it. This is the fallback that
            # matters when the client sent no wiring at all - the periods come
            # from the data, so a blank request is still answerable.
            fam = fams.get(split_metric_name(metric_label or "")[0], {})
        if not fam:
            # Only when *nothing* was asked for by name do we pick a family:
            # a client that sent neither a metric nor a column. Choosing one
            # when a specific metric was named would let an unusable metric
            # silently read a *different* metric's columns - and then the
            # pre-flight guard could never fire, because everything resolves.
            if wired or other:
                return "", ""
            two = sorted((f for f in fams.values() if len(f) >= 2),
                         key=lambda f: sorted(f.values())[0])
            if two:
                fam = two[0]
        if not fam:
            return "", ""
        ya, ty = default_period_columns(fam)
        # The measure a row-based slot is read from: the current-period column,
        # never the 2YA window the study does not use.
        metric_col = ty or next((v for k, v in fam.items() if k != "2YA"), "")
        slots = period_slots_present(df, _period_column(df, period_col))
        if not ya and MAT_YA in slots and metric_col:
            ya = metric_col
        if not ty and MAT_TY in slots and metric_col:
            ty = metric_col
        return ya, ty

    fams_a, fams_b = families(df_a), families(df_b)
    a_ya, a_ty = resolve(fams_a, a_prior, a_current, df_a, period_col_a)
    b_ya, b_ty = resolve(fams_b, b_prior, b_current, df_b, period_col_b)
    # A slot that resolves on one side and not the other keeps the other side's
    # column: the two datasets are the same measure, so falling back beats
    # dropping the period entirely.
    return (a_ya or "", a_ty or "", b_ya or a_ya or "", b_ty or a_ty or "")


def _resolve_period_columns(cfg: AnalysisConfig, df_a: pd.DataFrame,
                            df_b: pd.DataFrame) -> list[str]:
    """Point the two period roles at the Period columns, and report what changed.

    The study reads two periods: **MAT YA** and **MAT TY**. Both are already in
    the data, in one of two shapes:

    * the metric family carries them as a period qualifier on the column name
      (``Sales Value YA`` is MAT YA, the unqualified ``Sales Value`` is MAT TY);
    * the fact table carries them as **rows**, in its ``Periods`` column, over a
      single metric column - so the two slots are the same column read twice,
      each restricted to its own period's rows.

    Either way the client does not have to declare them, and the run does not
    depend on it having done so. A workbook with no year-ago column at all is
    answered from the period rows; one with neither is reported, not refused on
    the strength of a column name it was never going to have.

    The resolution itself lives in :func:`resolve_mat_slots` - one definition,
    called by this function, by ``main``'s pre-flight validation and by the
    analysis, so the columns a run is *checked* against are the columns it
    *reads*. This function's job is to apply the result to ``cfg`` (in place, so
    every downstream reader sees it) and to explain any correction.

    Two behaviours matter:

    * **``2YA`` is never a role.** It is a third moving-annual window the study
      does not use. Treating it as "the prior period" is how an earlier build
      ended up reading a column two years back and reporting the move as growth.
    * **A slot that cannot be resolved is cleared and reported, not guessed.**
      Leaving the incoming value in place would read one column twice and report
      a plausible 0% everywhere.

    Returns the notes; the caller puts them in ``Prepared.notes``.
    """
    msgs: list[str] = []

    # Pin the period dimension to the column the frame actually offers, so the
    # row-based slots resolve and the QC's duplicate check keys on the real
    # grain. The wired name is honoured when it carries the periods; otherwise
    # the data supplies one.
    for side, df, attr in (("a", df_a, "period_col"), ("b", df_b, "period_col_b")):
        col = _period_column(df, getattr(cfg, attr, ""))
        if col:
            setattr(cfg, attr, col)

    a_ya, a_ty, b_ya, b_ty = resolve_mat_slots(
        cfg.a_prior, cfg.a_current, cfg.b_prior, cfg.b_current,
        df_a, df_b, cfg.metric_label, cfg.period_col, cfg.period_col_b)

    for side, prior, current, want_ya, want_ty, df, pcol in (
        ("A", cfg.a_prior, cfg.a_current, a_ya, a_ty, df_a, cfg.period_col),
        ("B", cfg.b_prior, cfg.b_current, b_ya, b_ty, df_b, cfg.period_col_b),
    ):
        # The resolved value always wins - **including when it is empty**. A slot
        # the family cannot supply must be *cleared*, not left on whatever
        # arrived, because the arrival is exactly what might be wrong: a client
        # sending 2YA as "the year ago" would otherwise keep reading it and the
        # note would describe a correction that never happened.
        assign_ya = (lambda v: setattr(cfg, "a_prior", v)) if side == "A" \
            else (lambda v: setattr(cfg, "b_prior", v))
        assign_ty = (lambda v: setattr(cfg, "a_current", v)) if side == "A" \
            else (lambda v: setattr(cfg, "b_current", v))

        # Both slots on one column is only a fault when nothing else separates
        # them. On a row-based fact table the Periods column does, and saying so
        # is the whole point of reading the periods from the rows.
        row_based = period_discriminates(df, pcol, want_ya, want_ty)

        if want_ya != prior:
            if want_ya:
                msgs.append(
                    f"Dataset {side}: MAT YA reads '{want_ya}' rather than the "
                    f"'{prior}' that was wired"
                    + (f" - the '{pcol}' column carries MAT YA as its own rows, "
                       f"so the two periods come from the Period column rather "
                       f"than from a second mapping." if row_based else
                       " - the period qualifier on the metric columns names it, "
                       "so both periods come from the Period columns rather "
                       "than from a second mapping."))
            assign_ya(want_ya)
        if want_ty != current:
            if want_ty:
                msgs.append(
                    f"Dataset {side}: MAT TY reads '{want_ty}' rather than the "
                    f"'{current}' that was wired.")
            assign_ty(want_ty)

        # A slot the data could not supply at all. Reported plainly: the report
        # will have an empty period and the reader has to know why. This is not
        # a refusal - the other period still runs, and on a row-based workbook
        # the absent one is genuinely absent rather than mis-named.
        if not want_ya:
            msgs.append(
                f"Dataset {side}: no MAT YA could be resolved - the metric has "
                f"no year-ago column (a name ending YA) and the '{pcol or 'period'}' "
                f"column carries no MAT YA rows, so before/after growth is not "
                f"available for this metric.")
        if not want_ty:
            msgs.append(
                f"Dataset {side}: no MAT TY could be resolved - the metric has "
                f"no current-period column (the unqualified name or one ending "
                f"TY) and the '{pcol or 'period'}' column carries no MAT TY rows, "
                f"so the current period is not available.")

    # A degenerate wiring - both roles landing on one column - is still possible
    # after the above (a single-column family with nothing to separate the two
    # periods). Left checked here because it is the one mistake that produces a
    # plausible-looking number: every growth rate would read 0%, which looks like
    # a flat market rather than a broken one.
    for side, prior, current, df, pcol in (("A", cfg.a_prior, cfg.a_current, df_a, cfg.period_col),
                                           ("B", cfg.b_prior, cfg.b_current, df_b, cfg.period_col_b)):
        if prior and current and prior == current \
                and not period_discriminates(df, pcol, prior, current):
            msgs.append(
                f"{side} MAT YA and MAT TY both resolve to '{current}', so growth "
                f"for {side} is 0% only because the same column was used twice, "
                f"not because the data is flat.")
    return msgs


def prepare(df_a: pd.DataFrame, df_b: pd.DataFrame, cfg: AnalysisConfig) -> Prepared:
    """Canonicalise both datasets and aggregate to the analysis grain."""
    dim_cols_a = {
        "category": cfg.category_col,
        "subcategory": cfg.subcategory_col,
        "market": cfg.market_col,
        "manufacturer": cfg.manufacturer_col,
        "brand": cfg.brand_col,
    }
    dim_cols_b = {
        "category": cfg.col_b("category_col"),
        "subcategory": cfg.col_b("subcategory_col"),
        "market": cfg.col_b("market_col"),
        "manufacturer": cfg.col_b("manufacturer_col"),
        "brand": cfg.col_b("brand_col"),
    }
    # Resolve the two period slots the study reads - MAT YA and MAT TY - from
    # the Metric Period columns, rewriting cfg in place so everything
    # downstream (this function, the QC, the exports) reads the same pair. The
    # notes say which column each slot landed on and flag the degenerate cases,
    # so a period wiring that cannot work is visible rather than showing up as a
    # plausible-looking 0% growth.
    notes: list[str] = list(_resolve_period_columns(cfg, df_a, df_b))

    a_dims = _canonicalise(df_a, cfg.mapping_a, dim_cols_a)
    b_dims = _canonicalise(df_b, cfg.mapping_b, dim_cols_b)
    a_dims["category"] = _canonical_category(
        a_dims, cfg.category_map_a, cfg.category_excluded_a)
    b_dims["category"] = _canonical_category(
        b_dims, cfg.category_map_b, cfg.category_excluded_b)

    # --- dataset A -----------------------------------------------------------
    #
    # Each slot is the metric column restricted to its own period's rows. On a
    # workbook whose periods are columns the restriction is empty and the column
    # *is* the period; on one whose periods are rows both slots read the same
    # metric column and the Periods column separates them. Applying the filter
    # only when the period actually discriminates keeps the column-name
    # convention byte-for-byte unchanged.
    a_row_based = period_discriminates(df_a, cfg.period_col, cfg.a_prior, cfg.a_current)
    a_prior_p = MAT_YA if a_row_based else ""
    a_current_p = MAT_TY if a_row_based else ""

    a = a_dims.copy()
    a["prior"] = _slot_series(df_a, cfg.a_prior, a_prior_p, cfg.period_col)
    a["current"] = _slot_series(df_a, cfg.a_current, a_current_p, cfg.period_col)
    # A rate metric is averaged. With no weight column we fall back to an
    # unweighted mean - never to the rate weighting itself, which is meaningless.
    # The weight is masked exactly like its slot: on a row-based frame a row's
    # weight belongs to that row's period, so an unmasked denominator would
    # average in the other period and halve the rate.
    if cfg.is_rate and cfg.weight_metric and cfg.weight_metric in df_a.columns:
        w = pd.to_numeric(df_a[cfg.weight_metric], errors="coerce")
        a["weight"] = w
        a["weight_prior"] = w.where(_period_mask(df_a, cfg.period_col, a_prior_p))
        a["weight_current"] = w.where(_period_mask(df_a, cfg.period_col, a_current_p))
    a["__ds"] = "A"

    # --- dataset B -----------------------------------------------------------
    b_row_based = period_discriminates(df_b, cfg.period_col_b, cfg.b_prior, cfg.b_current)
    b_prior_p = MAT_YA if b_row_based else ""
    b_current_p = MAT_TY if b_row_based else ""

    b = b_dims.copy()
    b["prior"] = _slot_series(df_b, cfg.b_prior, b_prior_p, cfg.period_col_b)
    b["current"] = _slot_series(df_b, cfg.b_current, b_current_p, cfg.period_col_b)
    wb = cfg.weight_metric_b or cfg.weight_metric
    if cfg.is_rate and wb and wb in df_b.columns:
        w = pd.to_numeric(df_b[wb], errors="coerce")
        b["weight"] = w
        b["weight_prior"] = w.where(_period_mask(df_b, cfg.period_col_b, b_prior_p))
        b["weight_current"] = w.where(_period_mask(df_b, cfg.period_col_b, b_current_p))
    b["__ds"] = "B"

    dropped_a = int(a["category"].isna().sum())
    dropped_b = int(b["category"].isna().sum())
    if dropped_a or dropped_b:
        # A *scope* statement, not a fault. A row whose category was left unmapped
        # (or explicitly excluded) has no counterpart to compare against, so it
        # cannot contribute an impact figure - but "80437 rows dropped" reads as
        # data loss. Give the denominator and say what the figures cover, so the
        # reader can judge the coverage instead of distrusting the run.
        dropped = dropped_a + dropped_b
        total = len(a) + len(b)
        share = dropped / total * 100
        # A share that rounds to 0.0% reads as a rounding error; say "<0.1%".
        pct = "<0.1%" if share < 0.05 else f"{share:.1f}%"
        notes.append(
            f"{total - dropped:,} of {total:,} rows are in categories you mapped. "
            f"The other {dropped:,} ({pct}) are in categories left unmapped or "
            f"excluded, so they are outside this analysis - add a mapping for them "
            f"in step 4 to bring them in."
        )
    a = a[a["category"].notna()]
    b = b[b["category"].notna()]

    # --- the Total Market baseline ------------------------------------------
    # Computed *before* the market-scope filter, deliberately. When the scope is
    # "channels only" the Total rows are filtered out of the working frames, but
    # the denominator every share is measured against still has to exist.
    baseline: dict[str, tuple[float | None, float | None]] = {}
    baseline_name = (cfg.baseline_market or "").strip()
    # The B side may call the Total Market something else; the pairing supplies
    # the counterpart name. Without it, a baseline named only on the A side would
    # be silently unverified on B.
    baseline_name_b = (cfg.baseline_market_b or baseline_name).strip()
    wcol_a = _weight_map(a)
    wcol_b = _weight_map(b)
    if baseline_name:
        def _base(frame: pd.DataFrame, wcol, name: str) -> dict[str, tuple]:
            sub = frame[frame["market"].astype(str) == name]
            if sub.empty:
                return {}
            g = _aggregate(sub, ["category"],
                           {"prior": "prior", "current": "current"},
                           cfg.is_rate, wcol)
            if g.empty or "category" not in g.columns:
                return {}
            return {str(r["category"]): (_f(r.get("prior")), _f(r.get("current")))
                    for _, r in g.iterrows()}

        ba, bb = _base(a, wcol_a, baseline_name), _base(b, wcol_b, baseline_name_b)
        for cat in set(ba) | set(bb):
            pa, ca = ba.get(cat, (None, None))
            pb, cb = bb.get(cat, (None, None))
            baseline[cat] = (ca if ca is not None else pa,
                             cb if cb is not None else pb)
        if not baseline:
            notes.append(
                f"The market '{baseline_name}' has no rows in either dataset, so "
                "shares could not be measured against a Total Market."
            )
    elif cfg.market_levels:
        notes.append(
            "No Total Market was designated, so shares are measured against the "
            "sum of the markets in scope rather than against a total."
        )

    # The Total Market across the categories in scope - the denominator for each
    # category's own contribution to the market.
    baseline_total: tuple[float | None, float | None] = (None, None)
    n_base_cats = 0
    if baseline:
        sel = set(cfg.categories) if cfg.categories else None
        use = [v for c, v in baseline.items() if sel is None or c in sel]
        n_base_cats = len(use)
        if use:
            has_a = any(v[0] is not None for v in use)
            has_b = any(v[1] is not None for v in use)
            baseline_total = (
                sum(v[0] for v in use if v[0] is not None) if has_a else None,
                sum(v[1] for v in use if v[1] is not None) if has_b else None,
            )

    if cfg.markets:
        a = a[a["market"].isin(cfg.markets)]
        b = b[b["market"].isin(cfg.markets_b or cfg.markets)]
    if cfg.categories:
        a = a[a["category"].isin(cfg.categories)]
        b = b[b["category"].isin(cfg.categories)]

    if a.empty:
        notes.append("No rows from the previous dataset match the current selection.")
    if b.empty:
        notes.append("No rows from the updated dataset match the current selection.")

    cats = sorted(set(a["category"].dropna()) | set(b["category"].dropna()))
    mkts = sorted(set(a["market"].dropna()) | set(b["market"].dropna()))
    return Prepared(cfg=cfg, a=a, b=b, categories=cats, markets=mkts, notes=notes,
                    baseline=baseline, baseline_total=baseline_total,
                    baseline_name=baseline_name,
                    baseline_verified=bool(baseline),
                    baseline_n_categories=n_base_cats)



# ----------------------------------------------------------------------------
# Block builders
# ----------------------------------------------------------------------------


def _entity_block(
    a: pd.DataFrame,
    b: pd.DataFrame,
    dim: str,
    is_rate: bool,
    weight_col: str | None,
    metric_label: str,
    baseline_a: float | None = None,
    baseline_b: float | None = None,
    growth_applicable: bool = True,
) -> pd.DataFrame:
    """Build the before/after + level-shift + contribution block for a dim.

    ``baseline_a`` / ``baseline_b`` are the Total Market values for this
    category. When given, they are the denominator for share and contribution
    instead of the sum of the block. That distinction is the difference between
    a correct share and one that either double-counts the Total or reports a
    channel against the other channels rather than against the market.

    ``growth_applicable=False`` (Numeric Distribution) omits growth, level shift
    and growth-derived contribution: a distribution level is not an accumulating
    quantity, so a percentage change of it is not a meaningful impact measure.
    The before/after levels and the absolute change remain, which is what the
    brief asks for.
    """
    vcols = {"prior": "prior", "current": "current"}
    # Resolved per frame: a row-based fact table carries a weight per period, so
    # the same slot on A and B may need a different weight column. `weight_col`
    # stays as the caller's fallback for a frame with no weight of its own.
    ga = _aggregate(a, [dim], vcols, is_rate, _weight_map(a) or weight_col)
    gb = _aggregate(b, [dim], vcols, is_rate, _weight_map(b) or weight_col)
    ga = ga.rename(columns={"prior": "a_prior", "current": "a_current"})
    gb = gb.rename(columns={"prior": "b_prior", "current": "b_current"})
    m = ga.merge(gb, on=dim, how="outer").fillna({dim: ""})

    for c in ["a_prior", "a_current", "b_prior", "b_current"]:
        if c not in m:
            m[c] = np.nan

    block_a = float(np.nansum(m["a_current"])) or np.nan
    block_b = float(np.nansum(m["b_current"])) or np.nan
    tot_a = baseline_a if (baseline_a is not None and np.isfinite(baseline_a)) else block_a
    tot_b = baseline_b if (baseline_b is not None and np.isfinite(baseline_b)) else block_b

    if growth_applicable:
        m["before_growth_pct"] = [_safe_growth(c, p) for c, p in zip(m["a_current"], m["a_prior"])]
        m["after_growth_pct"] = [_safe_growth(c, p) for c, p in zip(m["b_current"], m["b_prior"])]
        m["level_shift_pp"] = [
            (af - bf) if (af is not None and bf is not None) else None
            for af, bf in zip(m["after_growth_pct"], m["before_growth_pct"])
        ]
    else:
        m["before_growth_pct"] = [None] * len(m)
        m["after_growth_pct"] = [None] * len(m)
        m["level_shift_pp"] = [None] * len(m)
    m["before_share_pct"] = m["a_current"] / tot_a * 100 if tot_a and np.isfinite(tot_a) else np.nan
    m["after_share_pct"] = m["b_current"] / tot_b * 100 if tot_b and np.isfinite(tot_b) else np.nan
    m["share_change_pp"] = m["after_share_pct"] - m["before_share_pct"]

    d_tot = (tot_b - tot_a) if (tot_a and tot_b and np.isfinite(tot_a) and np.isfinite(tot_b)) else np.nan
    # The change in MAT TY between the two databases.
    #
    # A side with no row is **zero, not unknown** for an additive metric: that is
    # already what the category total assumes (`sum(min_count=1)` over the rows
    # that exist), so an entity that is new in the updated dataset is a real,
    # positive movement and one that exited is a real, negative one. Leaving them
    # as NaN was not harmless - `_contributors` sorts on this column and takes the
    # tail, and pandas puts NaN last, so the "Gainers" list selected exactly the
    # entities whose change could not be computed and printed five names with no
    # figures while "Losers" was fine.
    #
    # A rate metric has no zero level, so there a missing side stays unknown.
    #
    # Coerced explicitly: on a category that exists on only one side the merged
    # columns can arrive as object dtype, and an object column then propagates all
    # the way to a comparison that cannot be made.
    a_cur = pd.to_numeric(m["a_current"], errors="coerce")
    b_cur = pd.to_numeric(m["b_current"], errors="coerce")
    if is_rate:
        delta = b_cur - a_cur
    else:
        delta = b_cur.fillna(0) - a_cur.fillna(0)
    m["abs_change"] = delta
    m["contribution_to_change_pct"] = (
        (delta / d_tot * 100) if d_tot and np.isfinite(d_tot) and abs(d_tot) > 1e-9 else np.nan
    )
    m["metric"] = metric_label
    return m


def _rank_block(
    block: pd.DataFrame,
    dim: str,
    top_n: int,
    client_entities: Sequence[str] = (),
) -> tuple[list[dict], list[dict]]:
    """Rank entities in both datasets and classify their movement.

    **The Top-N is taken from Dataset A** - the previous database - and the
    members are then followed into Dataset B. This is the study's question: it
    asks how the entities that were largest *before* the refresh have moved,
    which is only answerable if the selection is made on the before value. An
    earlier version selected on ``b_current`` and reported the after-ranking,
    which answers a different and less useful question - "who is largest now" -
    and silently drops an entity that led in A but shrank in B, which is exactly
    the movement the report is meant to surface.

    Ranking, movement classification and the ``rank_before`` / ``rank_after``
    columns are computed over **every** entity in the block, then the top-N is
    cut on A, so ``rank_change`` still means "rank in A minus rank in B" over the
    full population.
    """
    d = block.copy()
    d = d[d[dim].astype(str).str.len() > 0]
    d["rank_before"] = d["a_current"].rank(ascending=False, method="min", na_option="bottom")
    d["rank_after"] = d["b_current"].rank(ascending=False, method="min", na_option="bottom")

    present_a = d["a_current"].notna() & (d["a_current"].abs() > 0)
    present_b = d["b_current"].notna() & (d["b_current"].abs() > 0)

    def movement(r) -> str:
        if r["rank_before"] <= 0 or not np.isfinite(r["rank_before"]):
            return "NEW"
        if r["rank_after"] <= 0 or not np.isfinite(r["rank_after"]):
            return "EXITED"
        rb, ra = float(r["rank_before"]), float(r["rank_after"])
        if rb > 1e9:
            return "NEW"
        if ra > 1e9:
            return "EXITED"
        if ra < rb:
            return "GAINED"
        if ra > rb:
            return "LOST"
        return "HELD"

    d["movement"] = d.apply(movement, axis=1)
    d.loc[~present_a & present_b, "movement"] = "NEW"
    d.loc[present_a & ~present_b, "movement"] = "EXITED"

    # Select on the previous dataset (A), then report each member's after (B)
    # value. The cut is on a_current, deliberately - see the docstring.
    top = d.sort_values("a_current", ascending=False, na_position="last").head(top_n)
    top_records = [_record(r, dim, in_top="before") for _, r in top.iterrows()]

    # client entities are tracked independently of the Top-N cut
    client_records: list[dict] = []
    want = [c for c in client_entities if c]
    if want:
        idx = {str(v).strip().upper(): v for v in d[dim].astype(str)}
        for name in want:
            key = str(name).strip().upper()
            match = idx.get(key)
            if match is None:
                client_records.append({
                    "name": name, "found": False,
                    "note": "not present in either dataset",
                })
                continue
            row = d[d[dim] == match].iloc[0]
            rec = _record(row, dim, in_top="client")
            rec["found"] = True
            rec["in_top_n"] = bool(row["rank_before"] <= top_n)
            client_records.append(rec)
    return top_records, client_records


def _record(r: pd.Series, dim: str, in_top: str) -> dict:
    def g(k):
        v = r.get(k)
        return _f(v)

    return {
        "name": str(r[dim]),
        "metric": r.get("metric"),
        "before_prior": g("a_prior"),
        "before_current": g("a_current"),
        "after_prior": g("b_prior"),
        "after_current": g("b_current"),
        "before_growth_pct": g("before_growth_pct"),
        "after_growth_pct": g("after_growth_pct"),
        "level_shift_pp": g("level_shift_pp"),
        "before_share_pct": g("before_share_pct"),
        "after_share_pct": g("after_share_pct"),
        "share_change_pp": g("share_change_pp"),
        "abs_change": g("abs_change"),
        "contribution_to_change_pct": g("contribution_to_change_pct"),
        "rank_before": int(r["rank_before"]) if np.isfinite(r.get("rank_before", np.nan)) and r["rank_before"] < 1e9 else None,
        "rank_after": int(r["rank_after"]) if np.isfinite(r.get("rank_after", np.nan)) and r["rank_after"] < 1e9 else None,
        "rank_change": (
            int(r["rank_before"] - r["rank_after"])
            if np.isfinite(r.get("rank_before", np.nan)) and np.isfinite(r.get("rank_after", np.nan))
            and r["rank_before"] < 1e9 and r["rank_after"] < 1e9 else None
        ),
        "movement": r.get("movement"),
        "block": in_top,
    }


# ----------------------------------------------------------------------------
# Category report
# ----------------------------------------------------------------------------


def _insights(
    tot: dict,
    market_total: tuple[float | None, float | None],
    baseline_name: str,
    metric_label: str,
    baseline_ok: bool,
    n_baseline_cats: int = 0,
) -> list[dict]:
    """The three headline insights, in the order the brief asks for them.

    Growth and level shift are the category's own before/after movement.
    Contribution is measured against the **Total Market across every category**,
    because a category measured against itself is trivially 100%.

    For a metric with no growth (ND), the growth and level-shift insights are
    replaced by a single **absolute-change** insight: TY minus YA. Returning a
    growth card that reads "n/a" would invite the reader to treat a percentage
    change of a distribution level as meaningful, which it is not.
    """
    ma, mb = market_total
    before_share = after_share = of_change = None
    if ma is not None and mb is not None and np.isfinite(ma) and np.isfinite(mb):
        if tot.get("before_current") is not None and ma:
            before_share = tot["before_current"] / ma * 100
        if tot.get("after_current") is not None and mb:
            after_share = tot["after_current"] / mb * 100
        d_market = mb - ma
        d_cat = tot.get("abs_change")
        if d_cat is not None and abs(d_market) > 1e-9:
            of_change = d_cat / d_market * 100

    growth_ok = bool(tot.get("growth_applicable", True))
    if not growth_ok:
        return [
            {
                "key": "abs_change",
                "label": "MAT AD Absolute Change",
                "unit": "abs",
                "before": tot.get("before_current"),
                "after": tot.get("after_current"),
                "value": tot.get("abs_change"),
                "note": (f"{metric_label} is a distribution level, so the impact is "
                         f"reported as TY minus YA, not as growth"),
            },
            {
                "key": "contribution",
                "label": "Contribution",
                "unit": "%",
                "baseline": baseline_name or None,
                "baseline_verified": baseline_ok,
                "before_share_pct": before_share,
                "after_share_pct": after_share,
                "share_change_pp": (after_share - before_share)
                if (after_share is not None and before_share is not None) else None,
                "of_change_pct": of_change,
                "baseline_categories": n_baseline_cats or None,
                "note": (
                    (f"share of {baseline_name}"
                     + (f" across the {n_baseline_cats} categories in scope"
                        if n_baseline_cats else ""))
                    if baseline_name else "no Total Market designated"
                ),
            },
        ]

    bg, ag = tot.get("before_growth_pct"), tot.get("after_growth_pct")
    return [
        {
            "key": "mat_growth",
            "label": "MAT AD Growth",
            "unit": "%",
            "before": bg,
            "after": ag,
            "change": (ag - bg) if (ag is not None and bg is not None) else None,
            "note": f"{metric_label}, current period vs prior period",
        },
        {
            "key": "mat_level_shift",
            "label": "MAT AD Level Shift",
            "unit": "pp",
            "value": tot.get("level_shift_pp"),
            "note": "after growth minus before growth",
        },
        {
            "key": "contribution",
            "label": "Contribution",
            "unit": "%",
            "baseline": baseline_name or None,
            "baseline_verified": baseline_ok,
            "before_share_pct": before_share,
            "after_share_pct": after_share,
            "share_change_pp": (after_share - before_share)
            if (after_share is not None and before_share is not None) else None,
            "of_change_pct": of_change,
            "baseline_categories": n_baseline_cats or None,
            "note": (
                (f"share of {baseline_name}"
                 + (f" across the {n_baseline_cats} categories in scope"
                    if n_baseline_cats else ""))
                if baseline_name else "no Total Market designated"
            ),
        },
    ]


def _total_market_names(cfg) -> set[str]:
    """Every market value that represents the Total Market, on either side."""
    names = {(cfg.baseline_market or "").strip(),
             (cfg.baseline_market_b or "").strip()}
    names |= {str(m) for m, lvl in (cfg.market_levels or {}).items()
              if str(lvl or "").lower() == "total"}
    return {n for n in names if n}


def _measure_frames(a: pd.DataFrame, b: pd.DataFrame, cfg):
    """The rows the category's *own* figures are measured on.

    A stacked source file carries the Total Market row **and** the channel rows it
    covers, so summing the frame adds the channels to a total that already
    includes them. On the reference workbook that is a factor of 1.48 - the
    headline read 38.47Bn where the Total Market row it was compared against read
    25.96Bn - and it is the same double count the block layout avoids by never
    repeating the Total among its own members.

    When a Total Market is designated, the measurement is its rows. The headline,
    the Top-N and the contributors then agree with the row the block leads with,
    and a manufacturer or brand breakdown sums back to the figure above it.

    Both sides must have such a row. Measuring A on its total and B on everything
    would compare a total against a set of channels - precisely the mismatch the
    market scope work removed - so if either side lacks one, both fall back to the
    full frame and the caller reports that no Total Market was used.
    """
    names = _total_market_names(cfg)
    if not names or a.empty or b.empty:
        return a, b, False
    if "market" not in a.columns or "market" not in b.columns:
        return a, b, False
    ta = a[a["market"].astype(str).isin(names)]
    tb = b[b["market"].astype(str).isin(names)]
    if ta.empty or tb.empty:
        return a, b, False
    return ta, tb, True


def category_report(prep: Prepared, category: str) -> dict:
    """The complete impact story for one category."""
    cfg = prep.cfg
    a = prep.a[prep.a["category"] == category]
    b = prep.b[prep.b["category"] == category]

    wcol = "weight" if cfg.is_rate and "weight" in prep.a.columns else None

    # The Total Market baseline for this category. Absent when no Total was
    # designated; the blocks then fall back to their own sum, and the QC says so
    # rather than quietly presenting a share of the wrong denominator.
    base_a, base_b = prep.baseline.get(category, (None, None))

    # The rows the category's own figures are measured on: the Total Market's,
    # not every row in scope. See `_measure_frames` - summing a stacked file adds
    # the channels to the total that already contains them.
    a_measure, b_measure, on_total = _measure_frames(a, b, cfg)

    # Category totals (before/after) ------------------------------------------
    tot = {
        "before_prior": _f(a_measure["prior"].sum(min_count=1)),
        "before_current": _f(a_measure["current"].sum(min_count=1)),
        "after_prior": _f(b_measure["prior"].sum(min_count=1)),
        "after_current": _f(b_measure["current"].sum(min_count=1)),
        "rows_before": int(len(a_measure)),
        "rows_after": int(len(b_measure)),
    }
    if cfg.is_rate:
        def _rate_total(frame: pd.DataFrame, slot: str) -> float | None:
            # The weight that belongs to *this* slot. On a row-based fact table
            # each period's rows carry their own weight, so using the shared
            # column here would divide by both periods' weights and halve the
            # level - a wrong number that looks like a real one.
            wkey = f"weight_{slot}"
            w = frame[wkey] if wkey in frame.columns else (
                frame["weight"] if "weight" in frame.columns else None)
            if w is None:
                return None
            den = w.sum()
            return _f((frame[slot] * w).sum() / den) if den else None

        tot["before_prior"] = _rate_total(a_measure, "prior")
        tot["before_current"] = _rate_total(a_measure, "current")
        tot["after_prior"] = _rate_total(b_measure, "prior")
        tot["after_current"] = _rate_total(b_measure, "current")

    if cfg.growth_applicable:
        tot["before_growth_pct"] = _safe_growth(tot["before_current"], tot["before_prior"])
        tot["after_growth_pct"] = _safe_growth(tot["after_current"], tot["after_prior"])
    else:
        # ND is a distribution level: a percentage change of it is not a
        # meaningful impact measure, so growth is deliberately not computed.
        tot["before_growth_pct"] = None
        tot["after_growth_pct"] = None
    tot["abs_change"] = (
        (tot["after_current"] - tot["before_current"])
        if tot["after_current"] is not None and tot["before_current"] is not None else None
    )
    tot["level_shift_pp"] = (
        (tot["after_growth_pct"] - tot["before_growth_pct"])
        if (cfg.growth_applicable
            and tot["after_growth_pct"] is not None
            and tot["before_growth_pct"] is not None) else None
    )
    tot["before_share_pct"] = 100.0 if tot["before_current"] else None
    tot["after_share_pct"] = 100.0 if tot["after_current"] else None
    tot["contribution_to_change_pct"] = 100.0 if tot["abs_change"] else None
    tot["metric"] = cfg.metric_label
    tot["growth_applicable"] = bool(cfg.growth_applicable)

    report: dict[str, Any] = {
        "category": category,
        "metric": cfg.metric_label,
        "is_rate_metric": cfg.is_rate,
        "growth_applicable": bool(cfg.growth_applicable),
        "markets": cfg.markets,
        # Which rows the category's own figures came from. A reader of the
        # headline needs to know whether it is the Total Market or the sum of the
        # markets in scope - the two differ by the channels the total already
        # contains.
        "measured_on": "total_market" if on_total else "all_markets_in_scope",
        "total": tot,
        "blocks": {},
        "baseline": {
            "name": prep.baseline_name or None,
            "before": base_a,
            "after": base_b,
            "verified": bool(prep.baseline_verified
                             and (base_a is not None or base_b is not None)),
            "categories_in_scope": prep.baseline_n_categories,
            "levels": cfg.market_levels or {},
        },
    }
    report["insights"] = _insights(
        tot, prep.baseline_total, prep.baseline_name,
        cfg.metric_label,
        bool(prep.baseline_verified and prep.baseline_total[0] is not None),
        prep.baseline_n_categories,
    )

    # Market block ------------------------------------------------------------
    #
    # The market dimension is a hierarchy, and the levels answer different
    # questions, so they are reported as **separate blocks**: a Market/Channel
    # block (the Total, then the channels beneath it) and a Market/Region block
    # (the Total, then the regions beneath it). Laying them in one flat block
    # would put the Total next to its own parts, which double-counts it and makes
    # every share in the block wrong. The Total is emitted once at the top of
    # each block and **removed from the member rows**, because the members are
    # already shown beneath it - repeating the total as a member row would count
    # it a second time in any sum a reader performs.
    if cfg.market_col:
        mkt_all = _entity_block(a, b, "market", cfg.is_rate, wcol, cfg.metric_label,
                                base_a, base_b, cfg.growth_applicable)
        mkt_all = mkt_all.sort_values("b_current", ascending=False, na_position="last")
        # `market_levels` is the user's authored pairing level, so the split
        # follows their decision rather than a classification the app made. A
        # market with no recorded level is still reported, in the unscoped block,
        # so nothing authored silently disappears from the analysis.
        levels = {str(k): str(v or "").lower() for k, v in (cfg.market_levels or {}).items()}
        baseline_name_a = (cfg.baseline_market or "").strip()
        baseline_name_b = (cfg.baseline_market_b or baseline_name_a).strip()
        total_rows = [
            r for _, r in mkt_all.iterrows()
            if str(r["market"]) in (baseline_name_a, baseline_name_b)
            or levels.get(str(r["market"])) == "total"
        ]
        # One Total row, preferring the A-side name, carried into the block head.
        total_row = total_rows[0] if total_rows else None
        member_rows = [
            r for _, r in mkt_all.iterrows()
            if r is not total_row and levels.get(str(r["market"])) != "total"
        ]

        def _channel_entry(r: pd.Series) -> dict:
            return {
                "name": r["market"],
                "level": levels.get(str(r["market"])) or "unscoped",
                "before": {
                    "mat_ya": _f(r["a_prior"]), "mat_ty": _f(r["a_current"]),
                    "growth_pct": _f(r["before_growth_pct"]),
                },
                "after": {
                    "mat_ya": _f(r["b_prior"]), "mat_ty": _f(r["b_current"]),
                    "growth_pct": _f(r["after_growth_pct"]),
                },
                "level_shift": {
                    "mat_ty_pp": _f(r["level_shift_pp"]),
                    "before_share_pct": _f(r["before_share_pct"]),
                    "after_share_pct": _f(r["after_share_pct"]),
                },
                "contribution": {
                    "before_share_pct": _f(r["before_share_pct"]),
                    "after_share_pct": _f(r["after_share_pct"]),
                    "of_change_pct": _f(r["contribution_to_change_pct"]),
                },
                "abs_change": _f(r["abs_change"]),
            }

        # The Total, once, for the head of every block that has members.
        total_entry = _channel_entry(total_row) if total_row is not None else None

        def _block_for(want: str) -> dict | None:
            rows = [r for r in member_rows if levels.get(str(r["market"])) == want]
            if not rows:
                return None
            return {
                "level": want,
                "total": total_entry,
                "members": [_channel_entry(r) for r in rows],
            }

        channel_block = _block_for("channel")
        region_block = _block_for("region")
        # Anything the user paired but did not level, so it is not silently lost.
        other_block = _block_for("") or None
        if not other_block:
            loose = [r for r in member_rows if not levels.get(str(r["market"]))]
            if loose:
                other_block = {"level": "unscoped", "total": total_entry,
                               "members": [_channel_entry(r) for r in loose]}

        report["blocks"]["channel"] = channel_block
        report["blocks"]["region"] = region_block
        report["blocks"]["market_other"] = other_block

        # `channel_block` stays the flat, most-complete list the exports and the
        # QC read, and it keeps the Total first so a share in it is never
        # ambiguous. The per-level blocks above are for the UI's side-by-side
        # presentation.
        flat = ([total_row] if total_row is not None else []) + member_rows
        report["channel_block"] = [_channel_entry(r) for r in flat]

    # Subcategory block (when the dimension exists) ---------------------------
    #
    # Measured on the same rows as the category total (`a_measure` / `b_measure`),
    # so the breakdown sums back to the figure above it rather than to the wider
    # frame that double counts the Total Market's own members.
    if cfg.subcategory_col:
        sub = _entity_block(a_measure, b_measure, "subcategory", cfg.is_rate, wcol,
                            cfg.metric_label,
                            growth_applicable=cfg.growth_applicable)
        sub = sub[sub["subcategory"].astype(str).str.len() > 0]
        sub = sub.sort_values("b_current", ascending=False, na_position="last")
        report["blocks"]["subcategory"] = [_record(r, "subcategory", "subcategory") for _, r in sub.iterrows()]

    # Brand value share was removed on request, so no `brand_block` is emitted.
    # Brand Top-N (below) and the client-brand tracker are separate and remain.

    # Manufacturer / brand Top-N ---------------------------------------------
    if cfg.manufacturer_col:
        mblock = _entity_block(a_measure, b_measure, "manufacturer", cfg.is_rate,
                               wcol, cfg.metric_label,
                               growth_applicable=cfg.growth_applicable)
        top, client = _rank_block(mblock, "manufacturer", cfg.top_n, cfg.client_brands)
        report["manufacturer_top_n"] = top
        report["client_manufacturers"] = client
        mblock2 = mblock[mblock["manufacturer"].astype(str).str.len() > 0]
        report["n_manufacturers"] = int(len(mblock2))

    if cfg.brand_col:
        bblock = _entity_block(a_measure, b_measure, "brand", cfg.is_rate, wcol,
                               cfg.metric_label,
                               growth_applicable=cfg.growth_applicable)
        btop, bclient = _rank_block(bblock, "brand", cfg.top_n, cfg.client_brands)
        report["brand_top_n"] = btop
        report["client_brands"] = bclient
        report["n_brands"] = int(len(bblock[bblock["brand"].astype(str).str.len() > 0]))

    # Major contributors to the change ---------------------------------------
    contribs: list[dict] = []
    for dim_key, dim_col in (("manufacturer", "manufacturer"), ("brand", "brand")):
        if not getattr(cfg, f"{dim_key}_col", ""):
            continue
        blk = _entity_block(a_measure, b_measure, dim_col, cfg.is_rate, wcol,
                            cfg.metric_label,
                            growth_applicable=cfg.growth_applicable)
        blk = blk[blk[dim_col].astype(str).str.len() > 0].copy()
        # Gain and Loss are the two sides of the *same* column, selected the same
        # way: the largest rises and the largest falls, each bounded by zero.
        #
        # Two defects lived here. The old code sorted ascending and took
        # `head(5)` / `tail(5)`, which (a) selected the NaN rows for "Gainers"
        # because pandas sorts NaN last, and (b) would have listed five *declines*
        # under "Gain" on a category where nothing rose, since tail() has no
        # sign test. Both sides are now drawn from rows that carry a change, and
        # each is bounded by zero, so "Gain" is never a fall with the sign
        # dropped and "Loss" is never a rise.
        val = blk.copy()
        val["abs_change"] = pd.to_numeric(val["abs_change"], errors="coerce")
        val = val[val["abs_change"].notna()]
        gainers = (val[val["abs_change"] > 0]
                   .sort_values("abs_change", ascending=False).head(CONTRIBUTOR_N))
        losers = (val[val["abs_change"] < 0]
                  .sort_values("abs_change", ascending=True).head(CONTRIBUTOR_N))
        contribs.append({
            "level": dim_key,
            "gainers": [
                {"name": r[dim_col], "abs_change": _f(r["abs_change"]),
                 "contribution_to_change_pct": _f(r["contribution_to_change_pct"])}
                for _, r in gainers.iterrows()
            ],
            "losers": [
                {"name": r[dim_col], "abs_change": _f(r["abs_change"]),
                 "contribution_to_change_pct": _f(r["contribution_to_change_pct"])}
                for _, r in losers.iterrows()
            ],
        })
    report["contributors"] = contribs

    # Trend series (period-level), if the period column is wired -------------
    if cfg.trend_enabled and cfg.period_col:
        report["trend"] = _trend_series(prep, category)

    return report


def _trend_series(prep: Prepared, category: str) -> dict:
    """Period-level series for the category, from the trend-grain datasets."""
    cfg = prep.cfg
    pcol = cfg.period_col
    out: dict[str, Any] = {"period_col": pcol, "series": []}
    ta, tb = prep.a, prep.b
    if pcol not in ta.columns and pcol not in tb.columns:
        return out
    # trend frames are attached by the caller via prep.notes payload
    return out


# ----------------------------------------------------------------------------
# Bulk
# ----------------------------------------------------------------------------


def analyse(prep: Prepared, categories: Sequence[str] | None = None) -> list[dict]:
    cats = list(categories) if categories else prep.categories
    return [category_report(prep, c) for c in cats]
