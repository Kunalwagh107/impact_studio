"""Impact analysis engine.

Produces the before/after comparison the brief describes, at category level,
with the blocks visible in the reference layout:

  * Channel / Market block   BEFORE(MAT YA, MAT TY, Growth) x
                             AFTER(MAT YA, MAT TY, Growth) +
                             Level Shift(delta pp, share before, share after) +
                             Contribution(share before/after, share of change)
  * Brand value-share block  BEFORE(MAT YA, MAT TY, share chg) x AFTER(...)
  * Manufacturer Top-N       rank before/after, movement class, entered/exited
  * Client brands            tracked independently of Top-N

The whole dataset is aggregated once per dataset and every category report is a
slice of that aggregation, so a 150-category bulk export does not re-scan the
raw rows 150 times.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Sequence

import numpy as np
import pandas as pd

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
        w = d[weight_col] if weight_col and weight_col in d.columns else None
        parts = []
        for out_name, col in value_cols.items():
            if col not in d.columns:
                continue
            x = pd.to_numeric(d[col], errors="coerce")
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
    notes: list[str] = []

    a_dims = _canonicalise(df_a, cfg.mapping_a, dim_cols_a)
    b_dims = _canonicalise(df_b, cfg.mapping_b, dim_cols_b)
    a_dims["category"] = _canonical_category(
        a_dims, cfg.category_map_a, cfg.category_excluded_a)
    b_dims["category"] = _canonical_category(
        b_dims, cfg.category_map_b, cfg.category_excluded_b)

    # --- dataset A -----------------------------------------------------------
    a = a_dims.copy()
    a["prior"] = pd.to_numeric(df_a[cfg.a_prior], errors="coerce") if cfg.a_prior in df_a else np.nan
    a["current"] = pd.to_numeric(df_a[cfg.a_current], errors="coerce") if cfg.a_current in df_a else np.nan
    # A rate metric is averaged. With no weight column we fall back to an
    # unweighted mean - never to the rate weighting itself, which is meaningless.
    if cfg.is_rate and cfg.weight_metric and cfg.weight_metric in df_a.columns:
        a["weight"] = pd.to_numeric(df_a[cfg.weight_metric], errors="coerce")
    a["__ds"] = "A"

    # --- dataset B -----------------------------------------------------------
    b = b_dims.copy()
    b["prior"] = pd.to_numeric(df_b[cfg.b_prior], errors="coerce") if cfg.b_prior in df_b else np.nan
    b["current"] = pd.to_numeric(df_b[cfg.b_current], errors="coerce") if cfg.b_current in df_b else np.nan
    wb = cfg.weight_metric_b or cfg.weight_metric
    if cfg.is_rate and wb and wb in df_b.columns:
        b["weight"] = pd.to_numeric(df_b[wb], errors="coerce")
    b["__ds"] = "B"

    dropped_a = int(a["category"].isna().sum())
    dropped_b = int(b["category"].isna().sum())
    if dropped_a or dropped_b:
        notes.append(
            f"{dropped_a + dropped_b} row(s) dropped because their category was "
            "excluded in the category mapping."
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
    wcol_a = "weight" if "weight" in a.columns else None
    wcol_b = "weight" if "weight" in b.columns else None
    if baseline_name:
        def _base(frame: pd.DataFrame, wcol: str | None,
                  name: str) -> dict[str, tuple]:
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
    ga = _aggregate(a, [dim], vcols, is_rate, weight_col)
    gb = _aggregate(b, [dim], vcols, is_rate, weight_col)
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
    delta = m["b_current"] - m["a_current"]
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
    """Rank entities in both datasets and classify their movement."""
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

    top = d.sort_values("b_current", ascending=False, na_position="last").head(top_n)
    top_records = [_record(r, dim, in_top="after") for _, r in top.iterrows()]

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
            rec["in_top_n"] = bool(row["rank_after"] <= top_n)
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

    # Category totals (before/after) ------------------------------------------
    tot = {
        "before_prior": _f(a["prior"].sum(min_count=1)),
        "before_current": _f(a["current"].sum(min_count=1)),
        "after_prior": _f(b["prior"].sum(min_count=1)),
        "after_current": _f(b["current"].sum(min_count=1)),
        "rows_before": int(len(a)),
        "rows_after": int(len(b)),
    }
    if cfg.is_rate:
        wa = a["weight"] if "weight" in a else None
        wb = b["weight"] if "weight" in b else None
        tot["before_prior"] = _f((a["prior"] * wa).sum() / wa.sum()) if wa is not None and wa.sum() else None
        tot["before_current"] = _f((a["current"] * wa).sum() / wa.sum()) if wa is not None and wa.sum() else None
        tot["after_prior"] = _f((b["prior"] * wb).sum() / wb.sum()) if wb is not None and wb.sum() else None
        tot["after_current"] = _f((b["current"] * wb).sum() / wb.sum()) if wb is not None and wb.sum() else None

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

    # Market / channel block --------------------------------------------------
    if cfg.market_col:
        mkt = _entity_block(a, b, "market", cfg.is_rate, wcol, cfg.metric_label,
                            base_a, base_b, cfg.growth_applicable)
        mkt = mkt.sort_values("b_current", ascending=False, na_position="last")
        report["blocks"]["market"] = [ _record(r, "market", "market") for _, r in mkt.iterrows() ]
        # level shift + contribution columns for the reference layout
        report["channel_block"] = [
            {
                "name": r["market"],
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
            for _, r in mkt.iterrows()
        ]

    # Subcategory block (when the dimension exists) ---------------------------
    if cfg.subcategory_col:
        sub = _entity_block(a, b, "subcategory", cfg.is_rate, wcol, cfg.metric_label,
                            growth_applicable=cfg.growth_applicable)
        sub = sub[sub["subcategory"].astype(str).str.len() > 0]
        sub = sub.sort_values("b_current", ascending=False, na_position="last")
        report["blocks"]["subcategory"] = [_record(r, "subcategory", "subcategory") for _, r in sub.iterrows()]

    # Brand block -------------------------------------------------------------
    if cfg.brand_col:
        br = _entity_block(a, b, "brand", cfg.is_rate, wcol, cfg.metric_label,
                           growth_applicable=cfg.growth_applicable)
        br = br[br["brand"].astype(str).str.len() > 0]
        br = br.sort_values("b_current", ascending=False, na_position="last")
        report["brand_block"] = [
            {
                "name": r["brand"],
                "before": {
                    "mat_ya": _f(r["a_prior"]), "mat_ty": _f(r["a_current"]),
                    "growth_pct": _f(r["before_growth_pct"]),
                    "share_pct": _f(r["before_share_pct"]),
                    "share_chg_pp": _f(r["share_change_pp"]),
                },
                "after": {
                    "mat_ya": _f(r["b_prior"]), "mat_ty": _f(r["b_current"]),
                    "growth_pct": _f(r["after_growth_pct"]),
                    "share_pct": _f(r["after_share_pct"]),
                },
                "abs_change": _f(r["abs_change"]),
                "contribution_to_change_pct": _f(r["contribution_to_change_pct"]),
            }
            for _, r in br.iterrows()
        ]

    # Manufacturer / brand Top-N ---------------------------------------------
    if cfg.manufacturer_col:
        mblock = _entity_block(a, b, "manufacturer", cfg.is_rate, wcol, cfg.metric_label,
                               growth_applicable=cfg.growth_applicable)
        top, client = _rank_block(mblock, "manufacturer", cfg.top_n, cfg.client_brands)
        report["manufacturer_top_n"] = top
        report["client_manufacturers"] = client
        mblock2 = mblock[mblock["manufacturer"].astype(str).str.len() > 0]
        report["n_manufacturers"] = int(len(mblock2))

    if cfg.brand_col:
        bblock = _entity_block(a, b, "brand", cfg.is_rate, wcol, cfg.metric_label,
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
        blk = _entity_block(a, b, dim_col, cfg.is_rate, wcol, cfg.metric_label,
                            growth_applicable=cfg.growth_applicable)
        blk = blk[blk[dim_col].astype(str).str.len() > 0].copy()
        blk = blk.sort_values("abs_change")
        losers = blk.head(5)
        gainers = blk.tail(5).iloc[::-1]
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
