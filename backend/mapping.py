"""Dimension mapping between the previous (A) and updated (B) datasets.

The brief is explicit that the two datasets may not share a structure - a
category called ``Tandy`` in A may appear as ``Category + Subcategory`` in B -
so the app must *probe and propose* rather than assume.

Matching is layered cheapest-first and every proposal carries the evidence
that produced it:

  1. exact        - identical strings
  2. normalised   - case / whitespace / punctuation / bracketed-tag insensitive
  3. hierarchical - one name is a path component of the other
                    ("CVS" vs "CVS/TW Total TW Offline (G)/MT w/o Costco")
  4. token_set    - token order and subset insensitive
                    ("TEA RTD" vs "READY TO DRINK TEA")
  5. core key     - geography prefix and filler words stripped
  6. squash key   - spacing/initials insensitive ("Nestle S.A." vs "NESTLE SA")
  7. fuzzy        - edit-distance similarity, blocked and chunked for scale
  8. value        - corroboration: does the metric total for the pair agree?

Nothing is applied silently: every row lands as ``accepted``, ``suggested``,
``unmatched`` or ``excluded`` and the user can change it.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

try:  # pragma: no cover
    from rapidfuzz import fuzz, process as rf_process

    _HAVE_RAPIDFUZZ = True
except Exception:  # pragma: no cover
    _HAVE_RAPIDFUZZ = False

try:  # pragma: no cover
    from rapidfuzz.utils import default_process as _DEFAULT_PROCESS
except Exception:  # pragma: no cover
    _DEFAULT_PROCESS = None

# ----------------------------------------------------------------------------
# Normalisation
# ----------------------------------------------------------------------------

_BRACKETS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")

# Country / geography prefixes commonly prepended to market names
_GEO_PREFIX = re.compile(
    r"^(TW|AE|HK|MO|SG|MY|TH|ID|PH|VN|CN|JP|KR|IN|SA|KW|QA|BH|OM|EG|ZA|"
    r"UK|US|AU|NZ)\b[\s\-_]*",
    re.I,
)

# Filler words that carry little discriminating power in this domain
_STOPWORDS = {
    "THE", "AND", "OF", "TOTAL", "ALL", "OTHER", "OTHERS", "MISC",
    "G", "O", "FMCG", "W", "WO", "MT", "INC", "LTD", "CO", "CORP",
}


def normalize_name(value: Any) -> str:
    """Aggressive but content-preserving key: upper, no punctuation/spaces."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return ""
    s = unicodedata.normalize("NFKD", str(value))
    s = s.replace("&", " AND ")
    s = _BRACKETS.sub(" ", s)
    s = _NON_ALNUM.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip().upper()


def core_key(value: Any) -> str:
    """Normalised key with geography prefix and filler words removed.

    Used as a secondary blocking key so 'TW CVS' can meet
    'CVS/TW Total TW Offline (G)/MT w/o Costco'.
    """
    n = normalize_name(value)
    n = _GEO_PREFIX.sub("", n)
    toks = [t for t in n.split() if t not in _STOPWORDS]
    return " ".join(toks) if toks else n


def squash_key(value: Any) -> str:
    """All spaces removed. Catches initial-style variants of the same name:
    'Nestle S.A.' -> NESTLESA meets 'NESTLE SA' -> NESTLESA."""
    return normalize_name(value).replace(" ", "")


def _path_parts(value: Any) -> list[str]:
    """Split a hierarchical market path on '/' and normalise each part."""
    raw = str(value or "")
    if "/" not in raw:
        return []
    return [p for p in (normalize_name(x) for x in raw.split("/")) if p]


# ----------------------------------------------------------------------------
# Result types
# ----------------------------------------------------------------------------


@dataclass
class MappingRow:
    source: str
    target: str | None
    method: str
    confidence: float
    status: str
    evidence: dict = field(default_factory=dict)
    alternatives: list[dict] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MappingResult:
    dimension: str
    rows: list[MappingRow] = field(default_factory=list)
    summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "dimension": self.dimension,
            "rows": [r.to_dict() for r in self.rows],
            "summary": self.summary,
        }


# ----------------------------------------------------------------------------
# Value corroboration index
# ----------------------------------------------------------------------------


def build_value_index(
    df: pd.DataFrame, dim_col: str, metric_cols: Sequence[str]
) -> dict[str, dict[str, float]]:
    """dimension value -> {metric: total} used to corroborate a match."""
    cols = [c for c in metric_cols if c in df.columns]
    if not cols or dim_col not in df.columns:
        return {}
    g = df.groupby(df[dim_col].astype(str).str.strip(), dropna=True)[cols].sum(numeric_only=True)
    out: dict[str, dict[str, float]] = {}
    for idx, row in g.iterrows():
        out[str(idx)] = {c: float(row[c]) for c in cols if pd.notna(row[c])}
    return out


def _value_agreement(
    va: dict[str, float] | None, vb: dict[str, float] | None
) -> tuple[float | None, str]:
    """Return (delta_pct, human note) comparing the primary metric totals."""
    if not va or not vb:
        return None, ""
    shared = [k for k in va if k in vb]
    if not shared:
        return None, ""
    deltas = []
    for k in shared:
        a, b = va[k], vb[k]
        if abs(a) < 1e-9:
            continue
        deltas.append(abs(b - a) / abs(a) * 100.0)
    if not deltas:
        return None, ""
    d = float(np.mean(deltas))
    return d, f"metric totals agree to {d:.2f}%"


# ----------------------------------------------------------------------------
# Matching
# ----------------------------------------------------------------------------

FUZZY_ACCEPT = 96.0     # auto-accept
FUZZY_SUGGEST = 82.0    # surface as a suggestion
_MAX_FUZZY_POOL = 25000  # guard rail for absurd dimensions


def _fuzzy_topk(
    sources: list[str],
    targets: list[str],
    top_k: int = 3,
    chunk: int = 400,
) -> dict[str, list[tuple[str, float]]]:
    """Blocked, chunked fuzzy match returning top-k targets per source.

    ``default_process`` lower-cases and strips punctuation before scoring -
    without it the comparison is case-sensitive, which silently loses obvious
    matches such as 'Nestle S.A.' vs 'NESTLE SA'.
    """
    if not _HAVE_RAPIDFUZZ or not sources or not targets:
        return {}
    # Block by core key's first token so we compare like with like
    buckets: dict[str, list[str]] = {}
    for t in targets:
        k = core_key(t)
        buckets.setdefault(k[:2], []).append(t)
    all_targets = targets

    out: dict[str, list[tuple[str, float]]] = {}
    for i in range(0, len(sources), chunk):
        batch = sources[i : i + chunk]
        # candidate pool = union of buckets touched by the batch + global pool
        pool_set: set[str] = set()
        for s in batch:
            pool_set.update(buckets.get(core_key(s)[:2], []))
        pool = sorted(pool_set) or all_targets
        if len(pool) > _MAX_FUZZY_POOL:
            pool = pool[:_MAX_FUZZY_POOL]
        res = rf_process.cdist(
            batch,
            pool,
            scorer=fuzz.token_set_ratio,
            processor=_DEFAULT_PROCESS,
            score_cutoff=FUZZY_SUGGEST,
            workers=-1,
        )
        for bi, s in enumerate(batch):
            row = res[bi]
            order = np.argsort(-row)[:top_k]
            hits = [(pool[j], float(row[j])) for j in order if row[j] >= FUZZY_SUGGEST]
            if hits:
                out[s] = hits
    return out


def suggest_mapping(
    values_a: Sequence[str],
    values_b: Sequence[str],
    dimension: str,
    index_a: dict[str, dict[str, float]] | None = None,
    index_b: dict[str, dict[str, float]] | None = None,
    fuzzy: bool = True,
    truncated: bool = False,
) -> MappingResult:
    """Propose a mapping from A-values to B-values for one dimension.

    ``truncated`` should be set by the caller when the value lists were capped,
    so the summary can say so rather than reporting the leftover entities as
    genuinely unmatched.
    """
    a_list = [str(v).strip() for v in values_a if str(v).strip()]
    b_list = [str(v).strip() for v in values_b if str(v).strip()]

    exact_b = {v: v for v in b_list}
    norm_b: dict[str, list[str]] = {}
    core_b: dict[str, list[str]] = {}
    squash_b: dict[str, list[str]] = {}
    parts_b: dict[str, list[str]] = {}
    for v in b_list:
        norm_b.setdefault(normalize_name(v), []).append(v)
        core_b.setdefault(core_key(v), []).append(v)
        squash_b.setdefault(squash_key(v), []).append(v)
        for p in _path_parts(v):
            parts_b.setdefault(p, []).append(v)

    used_b: set[str] = set()
    rows: list[MappingRow] = []
    pending: list[str] = []

    for s in a_list:
        ns, cs = normalize_name(s), core_key(s)
        ev: dict[str, Any] = {}

        # 1. exact
        if s in exact_b and s not in used_b:
            rows.append(MappingRow(s, s, "exact", 1.0, "accepted",
                                   {"similarity": 100.0}, note="identical name"))
            used_b.add(s)
            continue

        # 2. normalised
        cands = [c for c in norm_b.get(ns, []) if c not in used_b]
        if cands:
            t = cands[0]
            rows.append(MappingRow(s, t, "normalised", 0.98, "accepted",
                                   {"similarity": 100.0},
                                   note="same name ignoring case/formatting"))
            used_b.add(t)
            continue

        # 3. hierarchical path component (A is a component of B's path)
        cands = [c for c in parts_b.get(ns, []) if c not in used_b]
        if not cands:
            cands = [c for c in parts_b.get(cs, []) if c not in used_b]
        if cands:
            t = sorted(cands, key=len)[0]
            rows.append(MappingRow(s, t, "hierarchical", 0.95, "accepted",
                                   {"similarity": 100.0},
                                   note="A is a path component of B's name"))
            used_b.add(t)
            continue

        # 4. core key (geography/filler stripped)
        cands = [c for c in core_b.get(cs, []) if c not in used_b]
        if cands:
            t = cands[0]
            rows.append(MappingRow(s, t, "normalised", 0.93, "accepted",
                                   {"similarity": 100.0},
                                   note="matched after stripping geo/filler tokens"))
            used_b.add(t)
            continue

        # 5. squashed key - same letters, different spacing/initials
        sq = squash_key(s)
        cands = [c for c in squash_b.get(sq, []) if c not in used_b]
        if cands:
            t = cands[0]
            rows.append(MappingRow(s, t, "normalised", 0.92, "accepted",
                                   {"similarity": 100.0},
                                   note="same name ignoring spacing/initials"))
            used_b.add(t)
            continue

        pending.append(s)

    # 6. fuzzy on the remainder
    fuzzy_map: dict[str, list[tuple[str, float]]] = {}
    if fuzzy and pending and b_list:
        free = [b for b in b_list if b not in used_b]
        fuzzy_map = _fuzzy_topk(pending, free or b_list)

    for s in pending:
        hits = fuzzy_map.get(s, [])
        va = (index_a or {}).get(s)
        best: MappingRow | None = None
        alts: list[dict] = []
        for t, score in hits:
            delta, note = _value_agreement(va, (index_b or {}).get(t))
            conf = score / 100.0
            if delta is not None:
                # Tight agreement nudges confidence up, wild disagreement down
                if delta <= 2:
                    conf = min(1.0, conf + 0.05)
                elif delta > 25:
                    conf -= 0.15
            ev = {"similarity": round(score, 2)}
            if delta is not None:
                ev["value_delta_pct"] = round(delta, 3)
            cand = MappingRow(
                s, t,
                "fuzzy",
                round(max(0.0, conf), 3),
                "accepted" if score >= FUZZY_ACCEPT else "suggested",
                ev,
                note=note or f"name similarity {score:.1f}%",
            )
            if best is None:
                best = cand
            else:
                alts.append({"target": cand.target, "confidence": cand.confidence,
                             "method": cand.method, "note": cand.note})
        if best is None:
            # last resort: value-only match
            if va and index_b:
                scored = []
                for t, vb in index_b.items():
                    if t in used_b:
                        continue
                    delta, _ = _value_agreement(va, vb)
                    if delta is not None:
                        scored.append((delta, t))
                scored.sort()
                if scored and scored[0][0] <= 5:
                    delta, t = scored[0]
                    best = MappingRow(s, t, "value", 0.70, "suggested",
                                      {"value_delta_pct": round(delta, 3)},
                                      note="no name match; metric totals align")
            if best is None:
                best = MappingRow(s, None, "unmatched", 0.0, "unmatched",
                                  {}, note="no candidate found - map manually")
        best.alternatives = alts[:3]
        if best.target:
            used_b.add(best.target)
        rows.append(best)

    # B values that nothing mapped to = new entities entering in the update
    unmapped_b = [b for b in b_list if b not in used_b]
    for b in unmapped_b:
        rows.append(MappingRow(
            b, None, "reverse_unmatched", 0.0, "new_in_b",
            {}, note="present only in the updated dataset",
        ))

    counts: dict[str, int] = {}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1

    summary = {
        "dimension": dimension,
        "n_source": len(a_list),
        "n_target": len(b_list),
        "accepted": counts.get("accepted", 0),
        "suggested": counts.get("suggested", 0),
        "unmatched": counts.get("unmatched", 0),
        "new_in_b": counts.get("new_in_b", 0),
        "truncated": bool(truncated),
        "auto_rate_pct": round(
            (counts.get("accepted", 0) / len(a_list) * 100) if a_list else 0.0, 2
        ),
    }
    if truncated:
        summary["note"] = (
            "Value list was capped - raise the limit to map the full dimension. "
            "Unmatched counts here are an artefact of the cap, not real gaps."
        )
    return MappingResult(dimension=dimension, rows=rows, summary=summary)


def apply_mapping(
    values: pd.Series, mapping: dict[str, str | None]
) -> pd.Series:
    """Apply a confirmed mapping to a column; unmapped values pass through."""
    s = values.astype(str).str.strip()
    return s.map(lambda v: mapping.get(v, v) if mapping.get(v) is not None else v)
