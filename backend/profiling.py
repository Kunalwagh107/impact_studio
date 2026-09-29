"""Dataset probing, profiling and column role classification.

The brief asks the app to *probe* both datasets and work out what it is
looking at rather than assume the two share a structure. This module produces
that profile and, crucially, the column-role guess that seeds the mapping step.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any

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

# Metric naming: a base metric plus an optional period qualifier
QUALIFIER_TOKENS = {
    "2ya": "2YA", "2yr": "2YA", "2y": "2YA", "twoya": "2YA", "2yaago": "2YA",
    "ya": "YA", "lya": "YA", "ly": "YA", "py": "YA", "previousyear": "YA",
    "lastyear": "YA", "prioryear": "YA",
    "ty": "TY", "cy": "TY", "currentyear": "TY", "thisyear": "TY",
    "yago": "YA",
}

RATE_HINTS = re.compile(
    r"\b(dist|distribution|share|sot\b|%|percent|percentage|price|rate|ratio|"
    r"avg|average|mean|index|penetration|coverage|frequency|per[\s_]?capita|"
    r"per[\s_]?unit|density|nd\b|wtd|numeric)\b",
    re.I,
)

# Roles that identify an entity to analyse
ENTITY_ROLES = ("category", "subcategory", "manufacturer", "brand", "product", "market")


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


def split_metric_name(name: str) -> tuple[str, str | None]:
    """'Sales Value 2YA' -> ('Sales Value', '2YA')."""
    raw = re.sub(r"[_]+", " ", str(name)).strip()
    tokens = raw.split()
    variant = None
    base_tokens = []
    for tok in tokens:
        key = re.sub(r"[^a-z0-9]", "", tok.lower())
        if key in QUALIFIER_TOKENS:
            variant = QUALIFIER_TOKENS[key]
        else:
            base_tokens.append(tok)
    base = " ".join(base_tokens).strip() or raw
    return base, variant


def _variant_order(v: str | None) -> int:
    return {"2YA": 0, "YA": 1, "TY": 2}.get(v or "", 99)


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
