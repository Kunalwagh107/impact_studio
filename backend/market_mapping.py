"""Market mapping: pair the market values of Dataset A with those of Dataset B.

The brief narrowed this step to exactly four things:

  1. take a **market value from Dataset A**
  2. assign that pairing a **market level** (total / region / channel)
  3. choose the **corresponding market value from Dataset B**
  4. use the chosen **level** to scope the impact analysis

Region and Channel are not separate mapping *targets* to be discovered and
resolved; they are simply the vocabulary of step 2. The user says what each
pairing is. Nothing is classified behind their back.

WHY THE HIERARCHY IS STILL RESPECTED
------------------------------------
A market dimension in shipment data is not flat. It normally carries the Total
Market alongside the regions and channels that make it up:

    TW Total TW Offline (G)                     <- the Total
    CVS / TW Total TW Offline (G) / MT w/o ...  <- a channel, child of the Total
    PX MART / TW Total TW Offline (G) / ...

In the reference workbook the channels sum to **55.5%** of the Total (385bn of
694bn), because the Total covers channels the extract does not list. So putting
the Total in the same block as its channels double-counts it, and dividing by
that block's sum understates every share in it.

The classification is therefore still *computed*, because a wrong level gives a
wrong denominator and the user should be told when the numbers endorse or
contradict their choice. But the computed value is offered as a **default and an
advisory note**, never applied silently: every pairing carries the level the user
set. See ``evidence_for_level``.

The path is tested by **substring containment**, never by splitting on ``/`` -
the reference data contains a channel literally named ``MT w/o Costco``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any, Mapping, Sequence

LEVELS = ("total", "region", "channel")

TOTAL_WORDS = ("total", "grand total", "overall", "all market", "all outlet")
REGION_WORDS = ("region", "north", "south", "east", "west", "central",
                "zone", "area", "district", "province")


def norm(s: Any) -> str:
    """Lowercase, punctuation-collapsed form used for containment tests."""
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def _has_word(haystack: str, words: Sequence[str]) -> str | None:
    for w in words:
        if re.search(rf"(^|\s){re.escape(w)}(\s|$)", haystack):
            return w
    return None


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class MarketPair:
    """One user-authored pairing: an A market value, a B market value, a level.

    ``market_a`` is required - it is the value the analysis reads from the
    previous dataset. ``market_b`` is optional because a market may exist on only
    one side, and the user should be able to record that rather than be forced
    into a false pairing. ``level`` scopes the analysis.
    """
    market_a: str
    market_b: str = ""
    level: str = "total"
    note: str = ""
    value_a: float | None = None
    value_b: float | None = None
    rows_a: int = 0
    rows_b: int = 0
    # Evidence, never a decision.
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["in_scope"] = self.level in LEVELS
        return d


@dataclass
class MarketMappingResult:
    pairs: list[MarketPair] = field(default_factory=list)
    a_values: list[str] = field(default_factory=list)
    b_values: list[str] = field(default_factory=list)
    values_a: dict = field(default_factory=dict)
    values_b: dict = field(default_factory=dict)
    paths: dict = field(default_factory=dict)
    summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "pairs": [p.to_dict() for p in self.pairs],
            "a_values": self.a_values,
            "b_values": self.b_values,
            "values_a": self.values_a,
            "values_b": self.values_b,
            "paths": self.paths,
            "summary": self.summary,
        }

    def scope_for(self, level: str) -> list[str]:
        """The A market values to include for a given level.

        Returns the ``market_a`` of every pair whose level matches, so the
        analysis scope is derived from what the user set rather than from a
        classification the app performed. ``market_b`` equivalents are returned
        by the caller's own lookup, since B may name the same market differently.
        """
        if level in ("all", "", None):
            return [p.market_a for p in self.pairs if p.market_a]
        return [p.market_a for p in self.pairs if p.level == level]

    def b_scope_for(self, level: str) -> list[str]:
        if level in ("all", "", None):
            return [p.market_b for p in self.pairs if p.market_b]
        return [p.market_b for p in self.pairs
                if p.level == level and p.market_b]


# ---------------------------------------------------------------------------
# Enumeration: what the UI offers as pickable values
# ---------------------------------------------------------------------------


def enumerate_markets(
    values_a: Sequence[str],
    values_b: Sequence[str],
    value_a: Mapping[str, float] | None = None,
    value_b: Mapping[str, float] | None = None,
    rows_a: Mapping[str, int] | None = None,
    rows_b: Mapping[str, int] | None = None,
    paths_a: Mapping[str, str] | None = None,
    paths_b: Mapping[str, str] | None = None,
    pairs: Sequence[dict] | None = None,
) -> MarketMappingResult:
    """List the market values on each side and any pairings already authored.

    ``pairs`` is what the *user* has set so far. If it is empty this returns the
    two value lists with no pairings at all - the UI shows two pickers and the
    user builds each pairing, which is the whole point of the step.
    """
    va = [str(v) for v in (values_a or []) if str(v).strip() and str(v) != "nan"]
    vb = [str(v) for v in (values_b or []) if str(v).strip() and str(v) != "nan"]
    value_a = {str(k): v for k, v in (value_a or {}).items()}
    value_b = {str(k): v for k, v in (value_b or {}).items()}
    rows_a = {str(k): v for k, v in (rows_a or {}).items()}
    rows_b = {str(k): v for k, v in (rows_b or {}).items()}
    paths_a = {str(k): str(v) for k, v in (paths_a or {}).items()}
    paths_b = {str(k): str(v) for k, v in (paths_b or {}).items()}

    built: list[MarketPair] = []
    for raw in pairs or []:
        ma = str(raw.get("market_a") or "").strip()
        if not ma:
            continue
        mb = str(raw.get("market_b") or "").strip()
        level = str(raw.get("level") or "total").strip().lower()
        if level not in LEVELS:
            level = "total"
        built.append(MarketPair(
            market_a=ma, market_b=mb, level=level,
            note=str(raw.get("note") or ""),
            value_a=value_a.get(ma), value_b=value_b.get(mb) if mb else None,
            rows_a=int(rows_a.get(ma) or 0),
            rows_b=int(rows_b.get(mb) or 0) if mb else 0,
            evidence=evidence_for_level(ma, mb, level, paths_a, paths_b, value_a),
        ))

    summary = summarise(built, va, vb, value_a, value_b)
    return MarketMappingResult(
        pairs=built,
        a_values=sorted(va),
        b_values=sorted(vb),
        values_a=value_a, values_b=value_b,
        paths={**{f"A::{k}": v for k, v in paths_a.items()},
               **{f"B::{k}": v for k, v in paths_b.items()}},
        summary=summary,
    )


def evidence_for_level(
    market_a: str,
    market_b: str,
    level: str,
    paths_a: Mapping[str, str] | None = None,
    paths_b: Mapping[str, str] | None = None,
    value_a: Mapping[str, float] | None = None,
) -> dict:
    """Advisory observations about a pairing's level. Does not change the level.

    Two things are worth telling the user:

    * the **name** suggests something (``total`` in the name, or a region word);
    * the **path** says the value is a child of another value on the same side,
      which is the reliable signal that it is not itself the Total.

    A contradiction between the user's choice and either signal is surfaced as a
    note so it can be reconsidered - it is never an error and never overrides.
    """
    ev: dict = {}
    na = norm(market_a)
    nb = norm(market_b) if market_b else ""

    named_total = bool(_has_word(na, TOTAL_WORDS)) or (nb and bool(_has_word(nb, TOTAL_WORDS)))
    region_word = _has_word(na, REGION_WORDS) or (nb and _has_word(nb, REGION_WORDS))
    if named_total:
        ev["name_suggests"] = "total"
    elif region_word:
        ev["name_suggests"] = "region"

    # A value whose path names *another* value as its ancestor is a child of it,
    # so it is not the Total. Containment, not a split - 'MT w/o Costco' has no
    # slash to split on but is still a legitimate channel name.
    for side, name, paths in (("a", market_a, paths_a or {}),
                              ("b", market_b, paths_b or {})):
        if not name:
            continue
        path = norm(paths.get(name, ""))
        nname = norm(name)
        if not path or not nname:
            continue
        # Does this value's path mention a *different* value's name?
        others = []
        for other in list(paths.keys()):
            if other == name:
                continue
            no = norm(other)
            if no and no in path:
                others.append(other)
        if others:
            ev.setdefault("child_of", {})[side] = others[:5]

    child_of = ev.get("child_of")
    if level == "total" and child_of:
        ev["contradiction"] = (
            "marked as a Total, but its hierarchy path sits under "
            + ", ".join(sorted({o for lst in child_of.values() for o in lst})[:3])
        )
    elif level == "channel" and named_total:
        ev["contradiction"] = "marked as a channel, but the name contains 'total'"
    elif level == "region" and not region_word:
        ev["note"] = ("no region keyword in the name; confirm this is a region "
                      "rather than a channel")

    return ev


def summarise(
    pairs: Sequence[MarketPair],
    values_a: Sequence[str],
    values_b: Sequence[str],
    value_a: Mapping[str, float] | None,
    value_b: Mapping[str, float] | None,
) -> dict:
    value_a = value_a or {}
    value_b = value_b or {}
    counts = {lvl: 0 for lvl in LEVELS}
    for p in pairs:
        counts[p.level] = counts.get(p.level, 0) + 1

    paired_a = {p.market_a for p in pairs if p.market_a}

    # The Total and its parts must not share a block. Report the parts' share of
    # the Total when both are present and paired, because a figure well under
    # 100% is the evidence that the Total genuinely covers more than the listed
    # parts - and therefore that adding them together would understate nothing
    # but would double-count the Total.
    totals = [p for p in pairs if p.level == "total" and p.market_a]
    parts = [p for p in pairs if p.level in ("region", "channel") and p.market_a]
    total_sum = sum((value_a.get(p.market_a) or 0.0) for p in totals)
    parts_sum = sum((value_a.get(p.market_a) or 0.0) for p in parts)
    parts_of_total = round(parts_sum / total_sum * 100, 2) if total_sum else None

    return {
        "n_a_values": len(values_a),
        "n_b_values": len(values_b),
        "n_pairs": len(pairs),
        "n_total": counts["total"],
        "n_region": counts["region"],
        "n_channel": counts["channel"],
        "unpaired_a": [v for v in values_a if v not in paired_a],
        "needs_review": sum(1 for p in pairs if p.evidence.get("contradiction")),
        "total_value_a": total_sum or None,
        "parts_value_a": parts_sum or None,
        "parts_of_total_pct": parts_of_total,
        # True when the listed parts do NOT add up to the Total, which is the
        # normal case and the reason the Total is its own block.
        "parts_are_subset": bool(parts_of_total is not None and parts_of_total < 99.0),
    }


def resolve_scope(
    result: MarketMappingResult,
    level: str,
) -> tuple[list[str], list[str]]:
    """The A and B market values to filter the analysis to, for a level.

    Both lists are returned because the two datasets need not name a market the
    same way; the analysis applies ``markets_a`` to side A and ``markets_b`` to
    side B. An empty list means "no filter" to the analysis, so a level with no
    pairings returns the caller's signal to run unfiltered rather than silently
    selecting nothing.
    """
    return result.scope_for(level), result.b_scope_for(level)
