"""Per-category Excel export.

Every sheet is **one flat table**: a single header row, one record per row, no
merged cells. The reference layout grouped columns under BEFORE / AFTER banners,
which reads well on paper but cannot be filtered, sorted or pivoted - and a
workbook is exactly where a reader does those things.

The export carries **one table per selected metric**. A run may measure Sales
Value, Volume and Numeric Distribution together, and the market block and the
Top-N are a different question for each basis; a single table silently answered
only the first one.

There is deliberately **no summary sheet and no QC sheet**. Both were removed on
request: the figures the summary carried are in the tables beneath it, and the
validation belongs to the app that produced the workbook, not to the workbook the
client receives.
"""

from __future__ import annotations

import os
import re
from typing import Any

import xlsxwriter

# --- palette (matches the reference workbook) --------------------------------
GREEN_HDR = "#E2EFDA"
BLUE_HDR = "#DDEBF7"
GREY_HDR = "#F2F2F2"
YELLOW = "#FFF2CC"
RED_FONT = "#C00000"
BORDER = "#BFBFBF"

FMT_PCT = '0.0"%"'
FMT_PP = '+0.0"%";-0.0"%";0.0"%"'

# Used when a report carries no display block at all (an older report object, or
# a caller driving the exporter directly in a test). It reproduces the previous
# automatic behaviour so nothing silently changes unit.
DEFAULT_DISPLAY = {"unit": "auto", "scale": 1.0, "symbol": "", "decimals": 0}

# A short, stable tag per metric for the sheet name. Excel caps a sheet name at
# 31 characters and rejects []:*?/\, so a full label such as
# "Numeric Distribution (ND)" cannot be used verbatim - and neither can
# "Sales Value" on the longer sheet bases.
_METRIC_TAGS = {"sales_value": "Value", "volume": "Volume", "nd": "ND"}
_TAG_PATTERNS = (
    (r"\bvolume\b|\bunits?\b|\bqty\b|quantity", "Volume"),
    (r"sales?\s*value|\bvalue\b|revenue|turnover", "Value"),
    (r"\bnd\b|numeric\s*distribution|distribution", "ND"),
)


def _metric_tag(key: str, label: str) -> str:
    tag = _METRIC_TAGS.get(str(key or "").strip().lower())
    if tag:
        return tag
    # No stable key (an older caller, or a hand-rolled request): fall back to the
    # label, but prefer a short tag when the label names one of the three
    # measures. "Sales Value" on "Manufacturer Top-N" overflows 31 characters and
    # truncates to "Sales Valu", which reads like a typo.
    text = str(label or key or "")
    for pattern, short in _TAG_PATTERNS:
        if re.search(pattern, text, re.I):
            return short
    tag = re.sub(r"[\[\]:*?/\\]+", " ", text).strip()
    return re.sub(r"\s+", " ", tag) or "Metric"


def _sheet_name(base: str, tag: str) -> str:
    """`base` with the metric tag, inside Excel's 31-character sheet-name cap."""
    if not tag:
        return base[:31]
    room = 31 - len(base) - 3          # " (tag)"
    if room < 3:
        return f"{base} {tag}"[:31]
    return f"{base} ({tag[:room]})"


def _numfmt(decimals: int) -> str:
    dp = max(0, min(int(decimals or 0), 6))
    return "#,##0" + ("." + "0" * dp if dp else "")


def _total_of(blocks, baseline_name: str) -> dict | None:
    """The row that *is* the total, found from the block itself.

    Relying only on the declared baseline meant a run whose baseline was not
    carried through printed a "Total (sum of rows above)" row directly beneath a
    row already levelled `total` - a duplicate of the Total Market the user had
    selected, which is what they asked to be rid of.
    """
    for b in blocks or []:
        if baseline_name and b.get("name") == baseline_name:
            return b
    for b in blocks or []:
        if str(b.get("level") or "").strip().lower() == "total":
            return b
    return None


def _level_tables(blk: dict) -> list[tuple[str, str, list[dict]]]:
    """The market rows grouped by level, as the analysis presents them.

    The analysis draws a separate block per level - channels, regions, and
    anything paired without a level - and the export only ever carried the flat
    list, so a study with both a channel and a region block lost the split on the
    way out.
    """
    out: list[tuple[str, str, list[dict]]] = []
    for key, title, noun in (("channel", "Channel", "Channels"),
                             ("region", "Region", "Regions"),
                             ("other", "Market", "Markets")):
        b = blk.get("market_other_block") if key == "other" \
            else blk.get(f"{key}_level_block")
        members = (b or {}).get("members") or []
        if not members:
            continue
        rows = ([b["total"]] if b.get("total") else []) + list(members)
        out.append((title, noun, rows))
    return out


def _pct(v: Any) -> float | None:
    """Analysis returns percent units; Excel percent format wants fractions."""
    try:
        return None if v is None else float(v) / 100.0
    except Exception:
        return None


# ----------------------------------------------------------------------------


def build_category_workbook(
    report: dict,
    qc: dict,
    out_path: str,
    meta: dict | None = None,
) -> str:
    """Write one category's impact workbook: one flat table per metric."""
    meta = meta or {}
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    wb = xlsxwriter.Workbook(out_path, {"nan_inf_to_errors": True})

    cat = report.get("category", "")
    markets = report.get("markets") or []
    mkt_label = ", ".join(markets) if markets else "All markets"

    metrics = _metric_blocks(report)

    used: set[str] = set()

    def unique(name: str) -> str:
        """A sheet name Excel will accept, and that no other sheet has taken.

        Excel refuses a duplicate sheet name outright, so two metrics whose
        labels shorten to the same tag would otherwise abort the whole workbook
        at the very end of a long run.
        """
        base = re.sub(r"[\[\]:*?/\\]+", " ", name).strip()[:31] or "Sheet"
        candidate, n = base, 2
        while candidate.lower() in used:
            suffix = f" ({n})"
            candidate = base[:31 - len(suffix)] + suffix
            n += 1
        used.add(candidate.lower())
        return candidate

    for key, blk in metrics.items():
        disp = blk.get("display") or report.get("display") or DEFAULT_DISPLAY
        f = _formats(wb, disp.get("decimals", 0))
        label = blk.get("label") or key or report.get("metric", "")
        tag = _metric_tag(blk.get("key") or key, label)
        if blk.get("channel_block"):
            _sheet_channel(wb, f, blk, cat, label, mkt_label, disp,
                           sheet_name=unique(_sheet_name("Channel", tag)))
        if blk.get("manufacturer_top_n"):
            _sheet_ranked(wb, f, blk["manufacturer_top_n"], "Manufacturer Top-N",
                          cat, label, mkt_label, disp,
                          sheet_name=unique(_sheet_name("Manufacturer Top-N", tag)))
        if blk.get("brand_top_n"):
            _sheet_ranked(wb, f, blk["brand_top_n"], "Brand Top-N",
                          cat, label, mkt_label, disp,
                          sheet_name=unique(_sheet_name("Brand Top-N", tag)))
        # One contributors table per metric. The report carries a contributors
        # block for every metric measured, so writing only the headline one
        # answered "what drove the change" for Sales Value and silently dropped
        # Volume and ND.
        if blk.get("contributors"):
            _sheet_contributors(wb, f, blk, cat, label, mkt_label, disp,
                                sheet_name=unique(_sheet_name("Contributors", tag)))

    # The client tracker stays single, on the headline metric: it answers "how are
    # our named entities doing", which is a question about the study rather than
    # about one basis.
    primary = next(iter(metrics.values()), {})
    pdisp = primary.get("display") or report.get("display") or DEFAULT_DISPLAY
    pf = _formats(wb, pdisp.get("decimals", 0))
    plabel = primary.get("label") or report.get("metric", "")
    if primary.get("client_brands") or primary.get("client_manufacturers"):
        _sheet_clients(wb, pf, primary, cat, plabel, mkt_label, pdisp,
                       sheet_name=unique("Client Brands"))

    wb.close()
    return out_path


def _metric_blocks(report: dict) -> dict[str, dict]:
    """The per-metric blocks, synthesising one for a legacy single-metric report."""
    metrics = report.get("metrics") or {}
    if metrics:
        return metrics
    return {report.get("metric_key") or "": {
        "key": report.get("metric_key") or "",
        "label": report.get("metric", ""),
        "is_rate_metric": report.get("is_rate_metric", False),
        "growth_applicable": report.get("growth_applicable", True),
        "display": report.get("display"),
        # Whether the figures are the Total Market's own rows or the sum of every
        # market in scope - the sheet states it, so it has to reach the sheet.
        "measured_on": report.get("measured_on"),
        # Each metric carries its own baseline, which decides whether the table
        # leads with a Total Market or appends a labelled sum at the foot. Losing
        # it here would silently reinstate the double count this layout exists to
        # avoid.
        "baseline": report.get("baseline") or {},
        "total": report.get("total") or {},
        "channel_block": report.get("channel_block") or [],
        # The per-level blocks the analysis draws (channels, regions, anything
        # paired without a level). Without these the sheet can only show the flat
        # list, and the split the user sees on screen disappears on the way out.
        "channel_level_block": report.get("channel_level_block"),
        "region_level_block": report.get("region_level_block"),
        "market_other_block": report.get("market_other_block"),
        "manufacturer_top_n": report.get("manufacturer_top_n") or [],
        "brand_top_n": report.get("brand_top_n") or [],
        "client_brands": report.get("client_brands") or [],
        "client_manufacturers": report.get("client_manufacturers") or [],
        "contributors": report.get("contributors") or [],
    }}


def _formats(wb, decimals: int = 0) -> dict:
    num = _numfmt(decimals)
    base = {"font_name": "Calibri", "font_size": 10, "border": 1,
            "border_color": BORDER}
    return {
        "title": wb.add_format({"bold": True, "font_size": 14, "font_name": "Calibri"}),
        "subtitle": wb.add_format({"font_size": 10, "font_color": "#595959"}),
        "hdr": wb.add_format({**base, "bold": True, "bg_color": GREY_HDR,
                              "align": "center", "valign": "vcenter",
                              "text_wrap": True}),
        "hdr_green": wb.add_format({**base, "bold": True, "bg_color": GREEN_HDR,
                                    "align": "center", "valign": "vcenter"}),
        "hdr_blue": wb.add_format({**base, "bold": True, "bg_color": BLUE_HDR,
                                   "align": "center", "valign": "vcenter"}),
        "label": wb.add_format({**base, "align": "left"}),
        "label_b": wb.add_format({**base, "bold": True, "align": "left"}),
        "val": wb.add_format({**base, "num_format": num}),
        "pct": wb.add_format({**base, "num_format": FMT_PCT}),
        "pct_red": wb.add_format({**base, "num_format": FMT_PCT, "font_color": RED_FONT}),
        "pp": wb.add_format({**base, "num_format": FMT_PP}),
        "pp_red": wb.add_format({**base, "num_format": FMT_PP, "font_color": RED_FONT}),
        "note": wb.add_format({"font_size": 9, "font_color": "#808080",
                               "text_wrap": True, "valign": "top"}),
        "tot_row": wb.add_format({**base, "bold": True, "bg_color": YELLOW}),
        "tot_val": wb.add_format({**base, "bold": True, "bg_color": YELLOW,
                                  "num_format": num}),
        "tot_pct": wb.add_format({**base, "bold": True, "bg_color": YELLOW,
                                  "num_format": FMT_PCT}),
        "tot_pp": wb.add_format({**base, "bold": True, "bg_color": YELLOW,
                                 "num_format": FMT_PP}),
        "wrap": wb.add_format({**base, "text_wrap": True, "valign": "top"}),
    }


def _title_block(ws, f, cat, metric, mkt_label, extra: str = "",
                 meta: dict | None = None) -> int:
    """Title rows above the table.

    The client and impact name go first, because they are how two analyses for
    the same client are told apart. Row 2 carries the baseline note, so a share
    in this workbook is never ambiguous about what it is a share *of*.
    """
    meta = meta or {}
    ws.write(0, 0, meta.get("impact_name") or f"{cat} - Impact Study", f["title"])
    line = f"{cat}   |   Metric: {metric}   |   Market: {mkt_label}"
    if meta.get("client_name"):
        line = f"Client: {meta['client_name']}   |   " + line
    if extra:
        line += f"   |   {extra}"
    ws.write(1, 0, line, f["subtitle"])
    if meta.get("baseline_market"):
        ws.write(2, 0,
                 f"Shares measured against {meta['baseline_market']}"
                 + ("" if meta.get("baseline_verified") else "  (not verified)"),
                 f["subtitle"])
    return 3


def _header(ws, f, row: int, headers: list[str], green: tuple[int, int] = (0, -1)) -> int:
    """One header row. No merged banner groups - a merged header is what stops a
    reader sorting or filtering the table."""
    lo, hi = green
    for i, h in enumerate(headers):
        fmt = f["hdr_green"] if lo <= i <= hi else f["hdr"]
        ws.write(row, i, h, fmt)
    return row + 1


def _unit_note(disp: dict) -> str:
    sym = disp.get("symbol") or ""
    if not sym:
        return "Values are absolute"
    return f"Values are in {sym}"


def _measured_note(blk: dict) -> str:
    """Which rows the figures were measured on - stated, not implied.

    A stacked workbook carries the Total Market row *and* the channels it covers,
    so "the category total" is ambiguous: the Total Market's own rows, or the sum
    of everything in scope. The two differ by the channels the total already
    contains (~1.48x on the reference file), and a reader cannot reconcile a
    Top-N against the headline without knowing which produced it.
    """
    if blk.get("measured_on") == "total_market":
        name = (blk.get("baseline") or {}).get("name")
        return ("Measured on the Total Market"
                + (f" · {name}" if name else "")
                + " - the channels beneath it are not added to it again.")
    return ("Measured as the sum of every market in scope, including any market "
            "that is itself a total.")


# ----------------------------------------------------------------------------


def _sheet_channel(wb, f, blk, cat, metric, mkt_label, disp, sheet_name="Channel"):
    """The market rows as one flat table **per level**, stacked in one sheet.

    The analysis draws a separate block for the channels and for the regions, and
    the sheet carries the same split - a study with both no longer arrives as a
    single merged list. Falls back to the flat block when no level blocks were
    produced (an older report).

    For a metric with no growth (Numeric Distribution) the **contribution columns
    are omitted** on request: a distribution level is not an accumulating
    quantity, so a share of the category's change is not a meaningful reading of
    it. The levels, the absolute change and the plain share remain.
    """
    ws = wb.add_worksheet(sheet_name)
    growth_ok = blk.get("growth_applicable", True)
    scale = disp.get("scale") or 1.0
    suffix = f" ({disp.get('symbol')})" if disp.get("symbol") else ""
    baseline_name = (blk.get("baseline") or {}).get("name") or ""

    tables = _level_tables(blk)
    if not tables:
        flat = blk.get("channel_block") or []
        if not flat:
            return
        tables = [("Channel", "Channels", flat)]

    ws.write(0, 0, f"Market block - {metric}{suffix}", f["title"])
    ws.write(1, 0, f"Category: {cat}   |   Market: {mkt_label}", f["subtitle"])
    ws.write(2, 0, _unit_note(disp) + "   |   One row per market / channel, "
                                      "grouped by level.", f["subtitle"])
    ws.write(3, 0, _measured_note(blk), f["subtitle"])

    if growth_ok:
        headers = ["Entity", "Level", "BEFORE MAT YA", "BEFORE MAT TY",
                   "BEFORE growth", "AFTER MAT YA", "AFTER MAT TY", "AFTER growth",
                   "Level shift (pp)", "Absolute change",
                   "Share before", "Share after", "Contribution to change"]
        green = (2, 4)
    else:
        headers = ["Entity", "Level", "BEFORE MAT YA", "BEFORE MAT TY",
                   "AFTER MAT YA", "AFTER MAT TY", "Absolute change (TY - YA)",
                   "Share before", "Share after"]
        green = (2, 3)

    def write_row(row: int, b: dict, is_total: bool, label: str) -> int:
        """One record, walked in header order so the columns cannot drift."""
        lab_f = f["tot_row"] if is_total else f["label"]
        num_f = f["tot_val"] if is_total else f["val"]
        ws.write(row, 0, label, lab_f)
        ws.write(row, 1, b.get("level") or "", lab_f)
        c = 2
        for side in ("before", "after"):
            for key in ("mat_ya", "mat_ty"):
                v = (b.get(side) or {}).get(key)
                if v is not None:
                    ws.write_number(row, c, v / scale, num_f)
                c += 1
            if growth_ok:
                g = (b.get(side) or {}).get("growth_pct")
                if g is not None:
                    ws.write_number(row, c, g / 100,
                                    f["pct_red"] if g < 0 else f["pct"])
                c += 1
        if growth_ok:
            ls = (b.get("level_shift") or {}).get("mat_ty_pp")
            if ls is not None:
                ws.write_number(row, c, ls, f["pp_red"] if ls < 0 else f["pp"])
            c += 1
        ac = b.get("abs_change")
        if ac is not None:
            ws.write_number(row, c, ac / scale, num_f)
        c += 1
        for key in ("before_share_pct", "after_share_pct"):
            v = (b.get("level_shift") or {}).get(key)
            if v is not None:
                ws.write_number(row, c, v / 100, f["pct"])
            c += 1
        if growth_ok:
            v = (b.get("contribution") or {}).get("of_change_pct")
            if v is not None:
                ws.write_number(row, c, v / 100, f["pct_red"] if v < 0 else f["pct"])
            c += 1
        return row + 1

    multi = len(tables) > 1
    r = 5
    first_header = r
    for title, noun, rows in tables:
        if multi:
            # A band naming the level. Only when there is more than one table -
            # above a single table it just repeats the header row's own noun.
            ws.write(r, 0, title, f["label_b"])
            r += 1
        if r == first_header:
            first_header = r
        r = _header(ws, f, r, headers, green=green)

        total = _total_of(rows, baseline_name)
        for b in rows:
            # The Total Market leads its table and is styled as a total row,
            # because the members beneath it are a subset of it.
            is_total = total is not None and b is total
            r = write_row(r, b, is_total,
                          ("Total Market · " + b["name"]) if is_total else b["name"])

        # The Total Market already leads this table, and `blk["total"]` is the sum
        # over *every* row of the category - the Total Market plus its own members
        # - so printing it again at the foot would duplicate the head row and
        # overstate it. The row survives only when the table carries no total at
        # all, where the sum is the only aggregate there is, and it is labelled as
        # the sum of the rows above so it cannot be mistaken for a market total.
        if total is None:
            tot = dict(blk.get("total") or {})
            tot["level"] = ""
            tot["before"] = {"mat_ya": tot.get("before_prior"),
                             "mat_ty": tot.get("before_current"),
                             "growth_pct": tot.get("before_growth_pct")}
            tot["after"] = {"mat_ya": tot.get("after_prior"),
                            "mat_ty": tot.get("after_current"),
                            "growth_pct": tot.get("after_growth_pct")}
            tot["level_shift"] = {"mat_ty_pp": tot.get("level_shift_pp"),
                                  "before_share_pct": 100.0, "after_share_pct": 100.0}
            tot["contribution"] = {"of_change_pct": 100.0}
            r = write_row(r, tot, True, "Total (sum of rows above)")
        r += 1

    ws.write(r, 0, "Share is of the category total in each dataset; contribution to "
                   "change is this entity's share of the category's total movement.",
             f["note"])
    r += 1
    if growth_ok:
        ws.write(r, 0, "Growth = MAT TY / MAT YA - 1. Level shift = after growth - "
                       "before growth, in percentage points.", f["note"])
    else:
        ws.write(r, 0, "This metric is a distribution level (Numeric Distribution), so no "
                       "percentage growth is reported and no contribution is computed: "
                       "its impact is the absolute change TY - YA.", f["note"])
        r += 1
        ws.write(r, 0, "Distribution values are always percentages out of 100 and never "
                       "exceed 100; they are printed as they are.", f["note"])
    if multi:
        r += 1
        ws.write(r, 0, "Each table above is one market level; the Total Market leads "
                       "each of them.", f["note"])
    ws.freeze_panes(first_header + 1, 1)


def _sheet_ranked(wb, f, block, title, cat, metric, mkt_label, disp,
                  sheet_name=None):
    """Manufacturer / Brand Top-N, one flat table, for one metric."""
    ws = wb.add_worksheet(sheet_name or title[:31])
    scale = disp.get("scale") or 1.0
    suffix = f" ({disp.get('symbol')})" if disp.get("symbol") else ""

    ws.write(0, 0, f"{title} - {metric}{suffix}", f["title"])
    ws.write(1, 0, f"Category: {cat}   |   Market: {mkt_label}   |   "
                   "Selected as the largest in the previous dataset, followed into "
                   "the updated one", f["subtitle"])
    ws.write(2, 0, _unit_note(disp), f["subtitle"])
    r = 4
    # Entity, then the MAT YA / MAT TY levels of BEFORE and AFTER, then the share
    # change, then the ranks - the Market Regions layout, so every ranked table in
    # the study reads the same way. Rank is never first: it is the consequence of
    # the movement, not the headline.
    headers = ["Entity", "BEFORE MAT YA", "BEFORE MAT TY", "AFTER MAT YA",
               "AFTER MAT TY", "Share change", "Rank BEFORE", "Rank AFTER",
               "Rank change", "Movement"]
    r = _header(ws, f, r, headers, green=(1, 4))

    for b in block:
        ws.write(r, 0, b["name"], f["label"])
        for col, v in ((1, b.get("before_prior")), (2, b.get("before_current")),
                       (3, b.get("after_prior")), (4, b.get("after_current"))):
            if v is not None:
                ws.write_number(r, col, v / scale, f["val"])
        sc = b.get("share_change_pp")
        if sc is not None:
            ws.write_number(r, 5, sc / 100, f["pp_red"] if sc < 0 else f["pp"])
        ws.write_number(r, 6, b["rank_before"] or 0, f["val"])
        ws.write_number(r, 7, b["rank_after"] or 0, f["val"])
        rc = b.get("rank_change")
        ws.write_number(r, 8, rc if rc is not None else 0,
                        f["pp_red"] if (rc is not None and rc < 0) else f["pp"])
        mv = b.get("movement") or ""
        ws.write(r, 9, mv, f["label_b"] if mv in ("NEW", "EXITED") else f["label"])
        r += 1

    r += 1
    ws.write(r, 0, "Selection: the largest entities in the PREVIOUS dataset (by MAT TY "
                   "there), then each followed into the updated one - so an entity that "
                   "led before and shrank after still appears, at the top.", f["note"])
    ws.write(r + 1, 0, "MAT YA / MAT TY are each side's own MAT - the previous dataset's "
                       "under BEFORE and the updated one's under AFTER. Share change is of "
                       "the category total. No growth column, matching the market block.",
             f["note"])
    ws.write(r + 2, 0, "Movement: NEW = only in the updated dataset, EXITED = only in the "
                       "previous dataset, GAINED/LOST = rank improved/declined, HELD = "
                       "unchanged.", f["note"])
    ws.freeze_panes(5, 1)


def _sheet_clients(wb, f, blk, cat, metric, mkt_label, disp,
                   sheet_name="Client Brands"):
    ws = wb.add_worksheet(sheet_name)
    scale = disp.get("scale") or 1.0
    ws.write(0, 0, f"Client entities tracked - {metric}", f["title"])
    ws.write(1, 0, f"Category: {cat}   |   Market: {mkt_label}   |   "
                   "Tracked independently of the Top-N cut", f["subtitle"])
    r = 3
    headers = ["Client entity", "Level", "Found", "In Top-N",
               "BEFORE MAT YA", "BEFORE MAT TY", "AFTER MAT YA", "AFTER MAT TY",
               "Rank BEFORE", "Rank AFTER", "Share change", "Movement"]
    r = _header(ws, f, r, headers, green=(4, 5))

    rows = []
    for b in blk.get("client_brands") or []:
        rows.append(("brand", b))
    for b in blk.get("client_manufacturers") or []:
        rows.append(("manufacturer", b))

    for level, b in rows:
        if not b.get("found", True):
            ws.write(r, 0, b.get("name", ""), f["label"])
            ws.write(r, 1, level, f["label"])
            ws.write(r, 2, "No", f["label_b"])
            ws.write(r, 3, "", f["label"])
            ws.write(r, 4, b.get("note", "not present in either dataset"), f["note"])
            r += 1
            continue
        ws.write(r, 0, b["name"], f["label"])
        ws.write(r, 1, level, f["label"])
        ws.write(r, 2, "Yes", f["label"])
        ws.write(r, 3, "Yes" if b.get("in_top_n") else "No", f["label"])
        for col, key in ((4, "before_prior"), (5, "before_current"),
                         (6, "after_prior"), (7, "after_current")):
            v = b.get(key)
            if v is not None:
                ws.write_number(r, col, v / scale, f["val"])
        ws.write_number(r, 8, b.get("rank_before") or 0, f["val"])
        ws.write_number(r, 9, b.get("rank_after") or 0, f["val"])
        sc = b.get("share_change_pp")
        if sc is not None:
            ws.write_number(r, 10, sc / 100, f["pp_red"] if sc < 0 else f["pp"])
        ws.write(r, 11, b.get("movement") or "", f["label"])
        r += 1

    if not rows:
        ws.write(r, 0, "No client entities were selected.", f["note"])


def _sheet_contributors(wb, f, blk, cat, metric, mkt_label, disp,
                        sheet_name="Contributors"):
    """The gainers and losers that drove the category's change.

    Gain and Loss are the two sides of one column, so both tables carry the same
    columns - Absolute change and Contribution - and both are drawn from entities
    whose change could be measured. A side with no entities is stated rather than
    printed as an empty table.
    """
    ws = wb.add_worksheet(sheet_name)
    scale = disp.get("scale") or 1.0
    ws.write(0, 0, f"Major contributors to the change - {metric}", f["title"])
    ws.write(1, 0, f"Category: {cat}   |   Market: {mkt_label}", f["subtitle"])
    ws.write(2, 0, "Absolute change is MAT TY in the updated dataset minus MAT TY in the "
                   "previous one. Contribution is this entity's share of the category's "
                   "total change.", f["subtitle"])
    r = 4
    headers = ["Level", "Direction", "Entity", "Absolute change",
               "Contribution to change"]
    r = _header(ws, f, r, headers)

    for c in blk.get("contributors") or []:
        level = c.get("level", "")
        for direction, rows in (("Gain", c.get("gainers") or []),
                                ("Loss", c.get("losers") or [])):
            if not rows:
                ws.write(r, 0, level, f["label"])
                ws.write(r, 1, direction, f["label_b"])
                ws.write(r, 2, f"no entity {direction.lower()}ed in this category",
                         f["note"])
                r += 1
                continue
            for it in rows:
                ws.write(r, 0, level, f["label"])
                ws.write(r, 1, direction, f["label_b"])
                ws.write(r, 2, it["name"], f["label"])
                if it.get("abs_change") is not None:
                    ws.write_number(r, 3, it["abs_change"] / scale, f["val"])
                v = it.get("contribution_to_change_pct")
                if v is not None:
                    ws.write_number(r, 4, v / 100, f["pct_red"] if v < 0 else f["pct"])
                r += 1
        r += 1
