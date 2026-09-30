"""Dataset probing, profiling and column role classification.

The brief asks the app to *probe* both datasets and work out what it is
looking at rather than assume the two share a structure. This module produces
that profile and, crucially, the column-role guess that seeds the mapping step.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any, Mapping

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------
# Column role vocabulary
# ----------------------------------------------------------------------------

ROLE_PATTERNS: list[tuple[str, str]] = [
    # (role, regex) - order matters, first hit wins
    ("dataset", r"^(dataset|source|version|scenario|type_of_dataset|data_?set)\b"),
    ("subcategory", r"\b(sub[\s_-]?categor\w*|sub[\s_-]?segment|sub[\s_-]?brand|segment)\b"),
    ("category", r"\b(categor\w*|cat\b|department|dept|sector|product[\s_-]?group)\b"),
    ("manufacturer", r"\b(manufactur\w*|mfr\b|supplier|company|corporat\w*|parent|vendor|marketer)\b"),
    ("brand", r"\b(brand\w*|label|trademark)\b"),
    ("product", r"\b(product\w*|sku\b|item\b|ean\b|upc\b|gtin\b|article|variant|pack)\b"),
    ("market", r"\b(market\w*|channel|retailer|outlet|banner|region|country|geograph\w*|store|shop|area|territor\w*)\b"),
    ("period", r"\b(period\w*|month|week|year|quarter|date|mat\b|ytd\b|fy\b|time)\b"),
]

# Metric naming: a base metric plus an optional period qualifier.
#
# The qualifier vocabulary is deliberately **recognition only, never
# construction**. The app does not decide that "Sales Value YA" is the prior
# column of a "Sales Value" family and then go looking for it; it reads the
# columns the workbook actually has, strips whatever qualifier it recognises to
# find the *base* measure, and treats the concrete column every period select
# resolves to as the only column the analysis ever reads. That distinction is
# what lets a workbook whose period names are translated (or absent entirely -
# a family of exactly two columns, one prior and one current) work unchanged.
#
# English and the common Latin/Germanic/CJK abbreviations are recognised. The
# list does not need to be exhaustive: an unrecognised qualifier simply leaves
# the column as its own family, which the period picker then offers verbatim.
QUALIFIER_TOKENS = {
    # --- two years ago ------------------------------------------------------
    "2ya": "2YA", "2yr": "2YA", "2y": "2YA", "twoya": "2YA", "2yaago": "2YA",
    "2yearago": "2YA", "2yearsago": "2YA", "vy2": "2YA", "l2y": "2YA",
    "vor2jahren": "2YA",
    # --- one year ago / prior period ---------------------------------------
    "ya": "YA", "lya": "YA", "ly": "YA", "py": "YA", "previousyear": "YA",
    "lastyear": "YA", "prioryear": "YA", "yago": "YA", "yearago": "YA",
    "prior": "YA", "previous": "YA", "last": "YA", "prev": "YA",
    "vy": "YA", "vj": "YA", "vorjahr": "YA", "lfl": "YA",
    "上年": "YA", "去年": "YA", "去年同期": "YA", "上期": "YA",
    # --- current period ------------------------------------------------------
    "ty": "TY", "cy": "TY", "currentyear": "TY", "thisyear": "TY",
    "current": "TY", "cur": "TY", "ac": "TY", "ytd": "TY",
    "jahr": "TY", "diesesjahr": "TY", "geschaftsjahr": "TY",
    "今年": "TY", "本期": "TY", "当期": "TY", "当年": "TY",
}

# The canonical ordering of a metric's period variants, earliest first. Used
# only to sort the period selects and to choose a default - never to decide
# which columns exist.
VARIANT_ORDER = ("2YA", "YA", "TY", "VALUE")

# Which recognised qualifiers mean "the earlier period" and which mean "the
# later one", expressed as classes so a caller can ask for a sensible default
# without naming a specific token. ``VALUE`` is the unqualified column and is
# treated as the current period, because in every workbook seen here the bare
# measure is the latest one.
PRIOR_VARIANTS = ("2YA", "YA")
CURRENT_VARIANTS = ("VALUE", "TY")

# Roles that identify an entity to analyse
ENTITY_ROLES = ("category", "subcategory", "manufacturer", "brand", "product", "market")

# A metric whose name suggests a level rather than a flow. Advisory: it sets the
# default treatment in the UI, and the user can override it per metric.
RATE_HINTS = re.compile(
    r"\b(dist|distribution|share|sot\b|%|percent|percentage|price|rate|ratio|"
    r"avg|average|mean|index|penetration|coverage|frequency|per[\s_]?capita|"
    r"per[\s_]?unit|density|nd\b|wtd|numeric)\b",
    re.I,
)


# ----------------------------------------------------------------------------
# Profiling
# ----------------------------------------------------------------------------


@dataclass
class ColumnProfile:
    name: str
    dtype: str
    role: str
    is_metric: bool
    is_rate: bool
    metric_family: str | None
    metric_variant: str | None
    non_null: int
    null_pct: float
    n_unique: int
    samples: list[str]
    min: float | None = None
    max: float | None = None
    sum: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DatasetProfile:
    label: str
    rows: int
    columns: int
    column_profiles: list[ColumnProfile] = field(default_factory=list)
    dimensions: dict[str, str] = field(default_factory=dict)   # role -> column
    metrics: list[str] = field(default_factory=list)
    metric_families: dict[str, dict[str, str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def classify_role(name: str, is_numeric: bool, n_unique: int, rows: int) -> str:
    """Guess a column's role from its name, falling back to cardinality."""
    low = re.sub(r"[_\-]+", " ", str(name)).strip().lower()
    for role, pat in ROLE_PATTERNS:
        if re.search(pat, low, re.I):
            return role
    if is_numeric:
        return "metric"
    # Low-cardinality text is usually a dimension; high-cardinality is an id
    if rows and n_unique <= max(60, rows * 0.02):
        return "dimension"
    return "attribute"


def _token_key(tok: str) -> str:
    """Normalise a period token for the qualifier lookup.

    Keeps CJK characters, which ``[^a-z0-9]`` would strip to nothing - so a
    Chinese period word would otherwise never match its own entry.
    """
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(tok).lower())


def split_metric_name(name: str) -> tuple[str, str | None]:
    """'Sales Value 2YA' -> ('Sales Value', '2YA').

    The base is whatever remains after a recognised period qualifier is removed,
    so a workbook naming its columns in another language still groups correctly
    as long as the qualifier is one the vocabulary knows. An unrecognised
    trailing token is left in the base deliberately: inventing a split would
    merge two genuinely different measures into one family, and a family that is
    too wide is worse than one that is too narrow - the user picks the column.
    """
    raw = re.sub(r"[_]+", " ", str(name)).strip()
    tokens = raw.split()
    variant = None
    base_tokens = []
    for tok in tokens:
        key = _token_key(tok)
        if key in QUALIFIER_TOKENS:
            variant = QUALIFIER_TOKENS[key]
        else:
            base_tokens.append(tok)
    base = " ".join(base_tokens).strip() or raw
    return base, variant


def _variant_order(v: str | None) -> int:
    try:
        return VARIANT_ORDER.index(v or "VALUE")
    except ValueError:
        # An unrecognised qualifier sorts last: it is not one of the known
        # period classes, so it cannot be ordered against them meaningfully.
        return len(VARIANT_ORDER)


# The two period values the study is built on. MAT YA is the year-ago moving
# annual total, MAT TY the current one. Everything else the workbook may carry
# (2YA, and any month-level period) is deliberately outside the study.
MAT_YA = "MAT YA"
MAT_TY = "MAT TY"

# A period value in the fact table's ``Periods`` column that is *not* one of the
# two the study reads. Recorded so the engine can say what it skipped rather
# than silently dropping it.
PERIOD_YA_TOKENS = ("MAT YA", "MATYA", "YA")
PERIOD_TY_TOKENS = ("MAT TY", "MATTY", "TY")


def classify_period_value(value: Any) -> str:
    """Map a ``Periods`` column value onto MAT YA / MAT TY / ''.

    Used both for the fact table's own ``Periods`` column and for the *period
    qualifier* on a metric column name (``Sales Value YA`` -> MAT YA). The two
    spell the same concept differently - a row says ``MAT TY``, a column says
    ``YA`` - so a single classifier keeps them from drifting apart.
    """
    t = re.sub(r"[^0-9a-z]+", "", str(value or "").lower())
    if not t:
        return ""
    for tok in PERIOD_YA_TOKENS:
        if t == re.sub(r"[^0-9a-z]+", "", tok.lower()):
            return MAT_YA
    for tok in PERIOD_TY_TOKENS:
        if t == re.sub(r"[^0-9a-z]+", "", tok.lower()):
            return MAT_TY
    return ""


def period_slots_present(df: pd.DataFrame, period_col: str) -> set[str]:
    """Which of the study's two periods a fact table's own ``Periods`` column carries.

    The two workbooks conventions differ, and this is what tells them apart:

    * **wide** - the periods are separate *columns* (``Sales Value YA``,
      ``Sales Value``) and the ``Periods`` column labels the row's primary
      period, so it carries ``MAT TY`` only;
    * **long** - the periods are separate *rows*, one metric column, and the
      ``Periods`` column carries both ``MAT YA`` and ``MAT TY``.

    Anything the classifier does not recognise (a month name, a blank) is not a
    slot and is left out, so a monthly trend column returns the empty set rather
    than being mistaken for a period dimension.
    """
    if not period_col or period_col not in df.columns:
        return set()
    vals = {classify_period_value(v) for v in df[period_col].dropna().unique()}
    return {v for v in vals if v}


def detect_period_column(df: pd.DataFrame, prefer: str = "") -> str:
    """The column that names MAT YA / MAT TY in a frame, wired name first.

    Used as the fallback when the period dimension was not wired: the study's
    periods come from the data, so a client that sent no period column must not
    make the row-based convention unreachable. Only a column whose values
    actually classify to a study period is eligible - a ``Month`` column of
    month names returns nothing, which is the honest answer.
    """
    if prefer and prefer in df.columns and period_slots_present(df, prefer):
        return prefer
    best = ""
    for col in df.columns:
        s = df[col]
        if pd.api.types.is_numeric_dtype(s) or pd.api.types.is_bool_dtype(s):
            continue
        if not period_slots_present(df, col):
            continue
        low = re.sub(r"[_\-]+", " ", str(col)).strip().lower()
        # A column that calls itself a period wins over one that merely
        # contains the values, so a stray flag column cannot take the role.
        if re.search(r"\bperiod\w*", low):
            return str(col)
        if not best:
            best = str(col)
    if best:
        return best
    return prefer if prefer in df.columns else ""


def default_period_columns(variants: Mapping[str, str] | None) -> tuple[str, str]:
    """The (MAT YA, MAT TY) *columns* a metric family resolves to.

    The study needs exactly two columns from a metric family: the year-ago
    moving annual total and the current one. Both are named by the period
    qualifier the workbook already carries - ``Sales Value YA`` is MAT YA and
    the unqualified ``Sales Value`` is MAT TY - so this reads the family the
    profile found rather than asking the user to name the same thing twice.

    ``2YA`` is skipped: it is a third moving-annual window the study does not
    use, and treating it as "the prior period" is what made the previous
    default read a column that was really the *other dataset's* YA.

    Returns ``("", "")`` pieces when the family cannot supply a role, so the
    caller reports "not available" rather than guessing.
    """
    fam = {k: v for k, v in (variants or {}).items() if v}
    if not fam:
        return "", ""
    # The prior role comes from YA only. Deliberately not from PRIOR_VARIANTS,
    # which includes 2YA - see the docstring.
    prior = fam.get("YA", "")
    # The current role is the unqualified measure, else an explicit TY.
    current = next((fam[v] for v in ("VALUE", "TY") if v in fam), "")
    # A family that names neither role still has a positional answer for a
    # genuinely two-period workbook: the later column is the current one.
    if not prior or not current:
        usable = {k: v for k, v in fam.items() if k != "2YA"}
        ordered = sorted(usable.items(), key=lambda kv: _variant_order(kv[0]))
        cols = [c for _, c in ordered]
        if len(cols) >= 2:
            prior = prior or cols[-2]
            current = current or cols[-1]
        elif len(cols) == 1:
            current = current or cols[0]
    return prior, current


def mat_period_columns(variants: Mapping[str, str] | None) -> dict[str, str]:
    """The study's two period slots for a family, named explicitly.

    ``{"MAT YA": col, "MAT TY": col}``, omitting any role the family cannot
    supply. Exists so a caller can report *which* slots resolved rather than
    only which columns did.
    """
    prior, current = default_period_columns(variants)
    out: dict[str, str] = {}
    if prior:
        out[MAT_YA] = prior
    if current:
        out[MAT_TY] = current
    return out


def profile_dataset(df: pd.DataFrame, label: str) -> DatasetProfile:
    rows = len(df)
    profiles: list[ColumnProfile] = []
    families: dict[str, dict[str, str]] = {}
    dimensions: dict[str, str] = {}
    metrics: list[str] = []
    warnings: list[str] = []

    if rows == 0:
        warnings.append(f"Dataset '{label}' contains no rows.")

    # Surface any value the numeric coercion had to drop, so a lost label is
    # visible rather than silently becoming a blank.
    for note in (getattr(df, "attrs", {}) or {}).get("coercion_notes", []) or []:
        warnings.append(
            f"Column '{note['column']}' arrived as text and was converted to "
            f"numeric; {note['values_not_numeric']} non-numeric value(s) were "
            "blanked and are excluded from the analysis."
        )

    for col in df.columns:
        s = df[col]
        is_numeric = pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s)
        non_null = int(s.notna().sum())
        n_unique = int(s.nunique(dropna=True))
        role = classify_role(col, is_numeric, n_unique, rows)
        is_metric = role == "metric" or (is_numeric and role not in ENTITY_ROLES)
        is_rate = bool(RATE_HINTS.search(str(col)))
        family, variant = (None, None)
        if is_metric:
            family, variant = split_metric_name(col)
            metrics.append(col)
            families.setdefault(family, {})[variant or "VALUE"] = col

        samples: list[str] = []
        if not is_numeric:
            vals = s.dropna().unique()
            samples = [str(v) for v in vals[:8]]
        else:
            samples = [f"{v:,.4g}" for v in s.dropna().head(4)]

        cp = ColumnProfile(
            name=str(col),
            dtype=str(s.dtype),
            role=role,
            is_metric=bool(is_metric),
            is_rate=bool(is_rate),
            metric_family=family,
            metric_variant=variant,
            non_null=non_null,
            null_pct=round((1 - non_null / rows) * 100, 2) if rows else 0.0,
            n_unique=n_unique,
            samples=samples,
        )
        if is_numeric and non_null:
            cp.min = float(np.nanmin(s.to_numpy(dtype="float64", na_value=np.nan)))
            cp.max = float(np.nanmax(s.to_numpy(dtype="float64", na_value=np.nan)))
            cp.sum = float(np.nansum(s.to_numpy(dtype="float64", na_value=np.nan)))
        profiles.append(cp)

        if role in ENTITY_ROLES and role not in dimensions:
            dimensions[role] = str(col)
        if role == "dataset" and "dataset" not in dimensions:
            dimensions["dataset"] = str(col)
        if role == "period" and "period" not in dimensions:
            dimensions["period"] = str(col)

    # Order metric variants so 2YA < YA < TY consistently
    for fam, variants in families.items():
        families[fam] = dict(
            sorted(variants.items(), key=lambda kv: _variant_order(kv[0]))
        )

    if not metrics:
        warnings.append(f"Dataset '{label}' has no numeric metric columns detected.")
    if "category" not in dimensions:
        warnings.append(f"Dataset '{label}' has no obvious category column.")

    return DatasetProfile(
        label=label,
        rows=rows,
        columns=len(df.columns),
        column_profiles=profiles,
        dimensions=dimensions,
        metrics=metrics,
        metric_families=families,
        warnings=warnings,
    )


def distinct_values(df: pd.DataFrame, column: str, limit: int = 5000) -> list[str]:
    if column not in df.columns:
        return []
    vals = df[column].dropna().astype(str).str.strip()
    uniq = sorted(v for v in vals.unique() if v)
    return uniq[:limit]
