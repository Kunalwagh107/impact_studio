"""Per-category Excel export.

Layout follows the reference impact-study layout supplied with the brief:
a BEFORE block (green), an AFTER block (blue), a Level Shift block and a
Contribution block, followed by the value-share view and the ranked entity
tables.
"""

from __future__ import annotations

import os
from typing import Any, Sequence

import xlsxwriter

# --- palette (matches the reference workbook) --------------------------------
GREEN_HDR = "#E2EFDA"
GREEN_SUB = "#F2F9EE"
BLUE_HDR = "#DDEBF7"
BLUE_SUB = "#EFF6FC"
GREY_HDR = "#F2F2F2"
YELLOW = "#FFF2CC"
RED_FONT = "#C00000"
BORDER = "#BFBFBF"

FMT_VAL = "#,##0"
FMT_VAL2 = "#,##0.00"
FMT_PCT = '0.0"%"'
FMT_PP = '+0.0"%";-0.0"%";0.0"%"'


def _scale_of(*values: Any) -> tuple[float, str]:
    """Choose a display scale (millions/thousands) from the magnitude."""
    mx = 0.0
    for v in values:
        try:
            if v is not None:
                mx = max(mx, abs(float(v)))
        except Exception:
            continue
    if mx >= 1e9:
        return 1e9, "Bn"
    if mx >= 1e6:
        return 1e6, "M"
    if mx >= 1e3:
        return 1e3, "K"
    return 1.0, ""


def _pct(v: Any) -> float | None:
    """Analysis returns percent units; Excel percent format wants fractions."""
    try:
        return None if v is None else float(v) / 100.0
    except Exception:
        return None


def _num(v: Any) -> Any:
    return "" if v is None else v


# ----------------------------------------------------------------------------


def build_category_workbook(
    report: dict,
    qc: dict,
    out_path: str,
    meta: dict | None = None,
) -> str:
    """Write one category's impact workbook.

    When the run carried several metrics, each metric gets its own Summary and
    Channel Block sheet, named with the metric as a suffix (e.g. "Summary (ND)").
    A metric with no growth (ND) simply omits the growth and level-shift rows and
    says so, rather than printing a percentage change of a distribution level.
    The entity-level sheets (Top-N, brands, contributors, clients) stay with the
    first metric, which keeps the workbook recognisable.
    """
    meta = meta or {}
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    wb = xlsxwriter.Workbook(out_path, {"nan_inf_to_errors": True})
    f = _formats(wb)

    cat = report.get("category", "")
    markets = report.get("markets") or []
    mkt_label = ", ".join(markets) if markets else "All markets"

    metrics = report.get("metrics") or {}
    if not metrics:
        metrics = {report.get("metric_key") or "": {
            "label": report.get("metric", ""),
            "growth_applicable": report.get("growth_applicable", True),
            "is_rate_metric": report.get("is_rate_metric", False),
            "total": report.get("total") or {},
            "insights": report.get("insights") or [],
            "channel_block": report.get("channel_block") or [],
            "manufacturer_top_n": report.get("manufacturer_top_n") or [],
            "brand_top_n": report.get("brand_top_n") or [],
            "client_brands": report.get("client_brands") or [],
            "client_manufacturers": report.get("client_manufacturers") or [],
            "contributors": report.get("contributors") or [],
        }}

    multi = len(metrics) > 1
    for key, blk in metrics.items():
        metric = blk.get("label") or key or report.get("metric", "")
        # Build a per-metric view of the report so the existing sheet writers
        # keep their shape and only ever see one metric's numbers.
        view = dict(report)
        view["metric"] = metric
        view["total"] = blk.get("total") or {}
        view["insights"] = blk.get("insights") or []
        view["channel_block"] = blk.get("channel_block") or []
        view["growth_applicable"] = blk.get("growth_applicable", True)
        view["is_rate_metric"] = blk.get("is_rate_metric", False)
        # Each metric carries its own baseline. Without this the sheet would
        # inherit the head metric's, which decides whether the trailing sum row
        # is suppressed - so the wrong name would silently reinstate a
        # double-count on a secondary metric's sheet.
        if blk.get("baseline"):
            view["baseline"] = blk["baseline"]
        suffix = f" ({metric})" if multi else ""
        _sheet_summary(wb, f, view, qc, meta, cat, metric, mkt_label,
                       sheet_name=("Summary" + suffix)[:31])
        if view["channel_block"]:
            _sheet_channel(wb, f, view, cat, metric, mkt_label,
                           sheet_name=("Channel Block" + suffix)[:31])

    # Entity-level sheets follow the first metric.
    primary = next(iter(metrics.values()))
    report = dict(report)
    report["manufacturer_top_n"] = primary.get("manufacturer_top_n") or []
    report["brand_top_n"] = primary.get("brand_top_n") or []
    report["client_brands"] = primary.get("client_brands") or []
    report["client_manufacturers"] = primary.get("client_manufacturers") or []
    report["contributors"] = primary.get("contributors") or []
    metric = primary.get("label") or report.get("metric", "")

    if report.get("manufacturer_top_n") is not None:
        _sheet_ranked(wb, f, report.get("manufacturer_top_n") or [],
                      "Manufacturer Top-N", cat, metric, mkt_label)
    if report.get("brand_top_n") is not None:
        _sheet_ranked(wb, f, report.get("brand_top_n") or [],
                      "Brand Top-N", cat, metric, mkt_label)
    if report.get("client_brands") or report.get("client_manufacturers"):
        _sheet_clients(wb, f, report, cat, metric, mkt_label)
    if report.get("contributors"):
        _sheet_contributors(wb, f, report, cat, metric, mkt_label)
    _sheet_qc(wb, f, qc, cat, meta)

    wb.close()
    return out_path


def _formats(wb) -> dict:
    base = {"font_name": "Calibri", "font_size": 10, "border": 1,
            "border_color": BORDER}
    return {
        "title": wb.add_format({"bold": True, "font_size": 14, "font_name": "Calibri"}),
        "subtitle": wb.add_format({"font_size": 10, "font_color": "#595959"}),
        "hdr_green": wb.add_format({**base, "bold": True, "bg_color": GREEN_HDR,
                                    "align": "center", "valign": "vcenter"}),
        "hdr_blue": wb.add_format({**base, "bold": True, "bg_color": BLUE_HDR,
                                   "align": "center", "valign": "vcenter"}),
        "hdr_grey": wb.add_format({**base, "bold": True, "bg_color": GREY_HDR,
                                   "align": "center", "valign": "vcenter"}),
        "label": wb.add_format({**base, "align": "left"}),
        "label_b": wb.add_format({**base, "bold": True, "align": "left"}),
        "val": wb.add_format({**base, "num_format": FMT_VAL}),
        "val2": wb.add_format({**base, "num_format": FMT_VAL2}),
        "pct": wb.add_format({**base, "num_format": FMT_PCT}),
        "pct_red": wb.add_format({**base, "num_format": FMT_PCT, "font_color": RED_FONT}),
        "pp": wb.add_format({**base, "num_format": FMT_PP}),
        "pp_red": wb.add_format({**base, "num_format": FMT_PP, "font_color": RED_FONT}),
        "kpi_label": wb.add_format({**base, "bold": True, "bg_color": GREY_HDR}),
        "kpi_val": wb.add_format({**base, "num_format": FMT_VAL, "bold": True}),
        "kpi_val2": wb.add_format({**base, "num_format": FMT_VAL2, "bold": True}),
        "kpi_pct": wb.add_format({**base, "num_format": FMT_PCT, "bold": True}),
        "kpi_pct_red": wb.add_format({**base, "num_format": FMT_PCT, "bold": True,
                                      "font_color": RED_FONT}),
        "kpi_pp": wb.add_format({**base, "num_format": FMT_PP, "bold": True}),
        "kpi_pp_red": wb.add_format({**base, "num_format": FMT_PP, "bold": True,
                                     "font_color": RED_FONT}),
        "note": wb.add_format({"font_size": 9, "font_color": "#808080",
                               "text_wrap": True, "valign": "top"}),
        "tot_row": wb.add_format({**base, "bold": True, "bg_color": YELLOW}),
        "tot_val": wb.add_format({**base, "bold": True, "bg_color": YELLOW,
                                  "num_format": FMT_VAL}),
        "tot_val2": wb.add_format({**base, "bold": True, "bg_color": YELLOW,
                                   "num_format": FMT_VAL2}),
        "tot_pct": wb.add_format({**base, "bold": True, "bg_color": YELLOW,
                                  "num_format": FMT_PCT}),
        "tot_pp": wb.add_format({**base, "bold": True, "bg_color": YELLOW,
                                 "num_format": FMT_PP}),
        "wrap": wb.add_format({**base, "text_wrap": True, "valign": "top"}),
    }


def _title_block(ws, f, cat, metric, mkt_label, extra: str = "",
                 meta: dict | None = None) -> int:
    """Title rows.

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


# ----------------------------------------------------------------------------


def _sheet_summary(wb, f, report, qc, meta, cat, metric, mkt_label,
                   sheet_name: str = "Summary"):
    ws = wb.add_worksheet(sheet_name)
    ws.set_column("A:A", 34)
    ws.set_column("B:F", 18)
    r = _title_block(ws, f, cat, metric, mkt_label, meta.get("period_label", ""), meta)

    t = report["total"]
    growth_ok = report.get("growth_applicable", True)
    scale, unit = _scale_of(t.get("before_current"), t.get("after_current"),
                            t.get("after_prior"), t.get("before_prior"))
    suffix = f" ({unit})" if unit else ""

    ws.write(r, 0, "Headline", f["hdr_grey"])
    for i, h in enumerate(["BEFORE (previous dataset)", "AFTER (updated dataset)"]):
        ws.write(r, 1 + i * 2, h, f["hdr_grey"] if i == 0 else f["hdr_blue"])
        ws.write(r, 2 + i * 2, "", f["hdr_grey"] if i == 0 else f["hdr_blue"])
    r += 1

    def pair(label, bkey, akey, kind):
        nonlocal r
        b, a = t.get(bkey), t.get(akey)
        ws.write(r, 0, label, f["kpi_label"])
        if kind == "num":
            ws.write_number(r, 1, (b / scale) if b is not None else 0, f["kpi_val"])
            ws.write_number(r, 2, (a / scale) if a is not None else 0, f["kpi_val"])
        elif kind == "pct":
            if b is not None:
                ws.write_number(r, 1, b / 100, f["kpi_pct_red"] if b < 0 else f["kpi_pct"])
            if a is not None:
                ws.write_number(r, 2, a / 100, f["kpi_pct_red"] if a < 0 else f["kpi_pct"])
        r += 1

    ws.write(r, 0, "Metric", f["kpi_label"])
    ws.write(r, 1, metric + suffix, f["label"])
    ws.write(r, 2, metric + suffix, f["label"])
    r += 1
    pair("Prior period (MAT YA)", "before_prior", "after_prior", "num")
    pair("Current period (MAT TY)", "before_current", "after_current", "num")
    if growth_ok:
        pair("Growth (MAT TY vs MAT YA)", "before_growth_pct", "after_growth_pct", "pct")
    else:
        # A distribution level has no meaningful percentage growth, so the
        # absolute change carries the impact. Printing growth here would invite
        # the reader to treat it as a rate of change, which it is not.
        ws.write(r, 0, "Growth (not applicable)", f["kpi_label"])
        ws.write(r, 1, "distribution level - see absolute change", f["label"])
        ws.write(r, 2, "distribution level - see absolute change", f["label"])
        r += 1
    r += 1

    ws.write(r, 0, "Impact of the update", f["hdr_grey"])
    ws.write(r, 1, "Value", f["hdr_grey"])
    r += 1
    pair("Absolute change in MAT TY", "abs_change", "abs_change", "num")
    if growth_ok:
        ws.write(r, 0, "Level shift (pp of growth)", f["kpi_label"])
        if t.get("level_shift_pp") is not None:
            v = t["level_shift_pp"]
            ws.write_number(r, 1, v, f["kpi_pp_red"] if v < 0 else f["kpi_pp"])
        r += 1
    ws.write(r, 0, "Before share of market", f["kpi_label"])
    if t.get("before_share_pct") is not None:
        ws.write_number(r, 1, t["before_share_pct"] / 100, f["kpi_pct"])
    r += 1
    ws.write(r, 0, "After share of market", f["kpi_label"])
    if t.get("after_share_pct") is not None:
        ws.write_number(r, 1, t["after_share_pct"] / 100, f["kpi_pct"])
    r += 2

    # Entity counts
    ws.write(r, 0, "Entities in scope", f["hdr_grey"])
    ws.write(r, 1, "Previous", f["hdr_grey"])
    ws.write(r, 2, "Updated", f["hdr_grey"])
    r += 1
    ws.write(r, 0, "Source rows", f["label"])
    ws.write_number(r, 1, t.get("rows_before", 0), f["val"])
    ws.write_number(r, 2, t.get("rows_after", 0), f["val"])
    r += 1
    if report.get("n_manufacturers") is not None:
        ws.write(r, 0, "Distinct manufacturers", f["label"])
        ws.write_number(r, 1, report["n_manufacturers"], f["val"])
        ws.write_number(r, 2, report["n_manufacturers"], f["val"])
        r += 1
    if report.get("n_brands") is not None:
        ws.write(r, 0, "Distinct brands", f["label"])
        ws.write_number(r, 1, report["n_brands"], f["val"])
        ws.write_number(r, 2, report["n_brands"], f["val"])
        r += 1

    r += 1
    ws.write(r, 0, "QC status", f["kpi_label"])
    worst = (qc or {}).get("worst", "PASS")
    ws.write(r, 1, worst, f["label"])
    r += 1
    ws.write(r, 0, "How to read this workbook", f["hdr_grey"])
    r += 1
    notes = [
        "Summary        - headline before/after and the impact of the update.",
        "Channel Block  - BEFORE vs AFTER by market/channel with level shift and contribution.",
        "Manufacturer Top-N / Brand Top-N - ranked entities and how they moved.",
        "Client Brands  - client entities tracked regardless of their rank.",
        "Contributors   - largest gainers and losers driving the category change.",
        "QC             - the automated validation results for this category.",
        "",
    ]
    if growth_ok:
        notes += [
            "Growth  = current period / prior period - 1.",
            "Level shift = after growth - before growth (percentage points).",
            "Contribution = share of the category total, plus share of the total change.",
        ]
    else:
        notes += [
            "This metric is a distribution level (Numeric Distribution), not an",
            "accumulating quantity, so no percentage growth is reported. The impact",
            "is the absolute change: TY - YA.",
            "Contribution = share of the category total, plus share of the total change.",
        ]
    for line in notes:
        ws.write(r, 0, line, f["note"])
        ws.merge_range(r, 0, r, 4, line, f["note"])
        r += 1


# ----------------------------------------------------------------------------


def _sheet_channel(wb, f, report, cat, metric, mkt_label,
                   sheet_name: str = "Channel Block"):
    ws = wb.add_worksheet(sheet_name)
    growth_ok = report.get("growth_applicable", True)
    ws.set_column("A:A", 34)
    ws.set_column("B:L", 13)
    ws.set_column("M:M", 12)

    blocks = report["channel_block"]
    scale, unit = _scale_of(*[b["after"]["mat_ty"] for b in blocks],
                            *[b["before"]["mat_ty"] for b in blocks])
    suffix = f" ({unit})" if unit else ""

    ws.write(0, 0, f"(Multiple Items) - {metric}{suffix}", f["title"])
    ws.write(1, 0, f"Category: {cat}   |   Market: {mkt_label}", f["subtitle"])

    r = 3
    if growth_ok:
        # Reference layout: BEFORE(YA/TY/growth) AFTER(YA/TY/growth) LevelShift Contr.
        ws.merge_range(r, 1, r, 3, "BEFORE", f["hdr_green"])
        ws.merge_range(r, 4, r, 6, "AFTER", f["hdr_blue"])
        ws.merge_range(r, 7, r, 9, "Level Shift", f["hdr_grey"])
        ws.merge_range(r, 10, r, 11, "Contribution - MAT TY", f["hdr_grey"])
        ws.write(r, 0, "Channels", f["hdr_grey"])
        r += 1
        for c, h in [(1, "MAT YA"), (2, "MAT TY"), (3, "Growth MAT"),
                     (4, "MAT YA"), (5, "MAT TY"), (6, "Growth MAT"),
                     (7, "MATTY"), (8, "Before"), (9, "After"),
                     (10, "Before"), (11, "After")]:
            ws.write(r, c, h, f["hdr_green"] if c <= 3 else
                     f["hdr_blue"] if c <= 6 else f["hdr_grey"])
        r += 1

        start = r
        for b in blocks:
            # The Total Market leads the block (it is `baseline_market`) and is
            # styled as a total row, because the members beneath it are a subset
            # of it - reading it as one more channel is the double-count this
            # layout exists to avoid.
            is_total = bool(report.get("baseline", {}).get("name")) and \
                b["name"] == report["baseline"]["name"]
            lab_f = f["tot_row"] if is_total else f["label"]
            num_f = f["tot_val2"] if is_total else f["val2"]
            ws.write(r, 0, ("Total Market · " + b["name"]) if is_total else b["name"], lab_f)
            for base, side in ((1, "before"), (4, "after")):
                ya = b[side]["mat_ya"]; ty = b[side]["mat_ty"]; gr = b[side]["growth_pct"]
                if ya is not None:
                    ws.write_number(r, base, ya / scale, num_f)
                if ty is not None:
                    ws.write_number(r, base + 1, ty / scale, num_f)
                if gr is not None:
                    ws.write_number(r, base + 2, gr / 100,
                                    f["pct_red"] if gr < 0 else f["pct"])
            ls = b["level_shift"]["mat_ty_pp"]
            if ls is not None:
                ws.write_number(r, 7, ls, f["pp_red"] if ls < 0 else f["pp"])
            for col, key in ((8, "before_share_pct"), (9, "after_share_pct")):
                v = b["level_shift"][key]
                if v is not None:
                    ws.write_number(r, col, v / 100, f["pct"])
            for col, key in ((10, "before_share_pct"), (11, "after_share_pct")):
                v = b["contribution"][key]
                if v is not None:
                    ws.write_number(r, col, v / 100, f["pct"])
            ws.write_number(r, 12, (b["abs_change"] or 0) / scale, f["val2"])
            r += 1
        end = r - 1
    else:
        # Distribution level: Top-N by TY, then YA, then the absolute change
        # (TY - YA). No growth column, because a percentage change of a
        # distribution level is not a meaningful impact measure.
        ws.merge_range(r, 1, r, 2, "BEFORE", f["hdr_green"])
        ws.merge_range(r, 3, r, 4, "AFTER", f["hdr_blue"])
        ws.merge_range(r, 5, r, 7, "Impact", f["hdr_grey"])
        ws.merge_range(r, 8, r, 9, "Contribution - MAT TY", f["hdr_grey"])
        ws.write(r, 0, "Top channels by TY", f["hdr_grey"])
        r += 1
        for c, h in [(1, "MAT YA"), (2, "MAT TY"),
                     (3, "MAT YA"), (4, "MAT TY"),
                     (5, "Abs change"), (6, "Before share"), (7, "After share"),
                     (8, "Before"), (9, "After")]:
            ws.write(r, c, h, f["hdr_green"] if c <= 2 else
                     f["hdr_blue"] if c <= 4 else f["hdr_grey"])
        r += 1

        start = r
        for b in blocks:
            ws.write(r, 0, b["name"], f["label"])
            for base, side in ((1, "before"), (3, "after")):
                ya = b[side].get("mat_ya"); ty = b[side].get("mat_ty")
                if ya is not None:
                    ws.write_number(r, base, ya / scale, f["val2"])
                if ty is not None:
                    ws.write_number(r, base + 1, ty / scale, f["val2"])
            ac = b.get("abs_change")
            if ac is not None:
                ws.write_number(r, 5, ac / scale, f["val2"])
            for col, key in ((6, "before_share_pct"), (7, "after_share_pct")):
                v = b["level_shift"].get(key)
                if v is not None:
                    ws.write_number(r, col, v / 100, f["pct"])
            for col, key in ((8, "before_share_pct"), (9, "after_share_pct")):
                v = b["contribution"].get(key)
                if v is not None:
                    ws.write_number(r, col, v / 100, f["pct"])
            r += 1
        end = r - 1

    # Total row -------------------------------------------------------------
    #
    # The Total Market already leads this block (it is the baseline, styled as a
    # total row). `report["total"]` is the sum over *every* row of the category,
    # which in a stacked source file means the Total Market row plus its own
    # members - a double count. So when a baseline Total Market is present the
    # trailing row is omitted entirely: printing it would both duplicate the head
    # row and overstate it. The row survives only when no Total was designated,
    # where it is the sole aggregate the block has, and is labelled as the sum of
    # the rows above so it cannot be mistaken for a market total.
    t = report["total"]
    baseline_name = (report.get("baseline") or {}).get("name")
    note_at = r
    if not baseline_name:
        ws.write(r, 0, "Total (sum of rows above)", f["tot_row"])
        note_at = r + 2
        if growth_ok:
            for base, side in ((1, "before"), (4, "after")):
                ya, ty, gr = t.get(f"{side}_prior"), t.get(f"{side}_current"), t.get(f"{side}_growth_pct")
                if ya is not None:
                    ws.write_number(r, base, ya / scale, f["tot_val2"])
                if ty is not None:
                    ws.write_number(r, base + 1, ty / scale, f["tot_val2"])
                if gr is not None:
                    ws.write_number(r, base + 2, gr / 100, f["tot_pct"])
            if t.get("level_shift_pp") is not None:
                ws.write_number(r, 7, t["level_shift_pp"], f["tot_pp"])
            for col in (8, 9, 10, 11):
                ws.write_number(r, col, 1.0, f["tot_pct"])
            ws.write_number(r, 12, (t.get("abs_change") or 0) / scale, f["tot_val2"])
        else:
            for base, side in ((1, "before"), (3, "after")):
                ya, ty = t.get(f"{side}_prior"), t.get(f"{side}_current")
                if ya is not None:
                    ws.write_number(r, base, ya / scale, f["tot_val2"])
                if ty is not None:
                    ws.write_number(r, base + 1, ty / scale, f["tot_val2"])
            if t.get("abs_change") is not None:
                ws.write_number(r, 5, t["abs_change"] / scale, f["tot_val2"])
            for col in (6, 7, 8, 9):
                ws.write_number(r, col, 1.0, f["tot_pct"])

    if growth_ok:
        ws.write(note_at, 0, "Column M = absolute change in MAT TY (AFTER - BEFORE).",
                 f["note"])
        ws.write(note_at + 1, 0, "Level Shift MATTY = AFTER growth - BEFORE growth, in percentage points.",
                 f["note"])
        ws.write(note_at + 2, 0, "Before/After under Level Shift and Contribution are shares of the "
                                 "category total in each dataset.", f["note"])
        if baseline_name:
            ws.write(note_at + 3, 0, "The Total Market leads this block; it is not repeated at "
                                     "the foot, because its members are a subset of it.",
                     f["note"])
    else:
        ws.write(note_at, 0, "This metric is a distribution level. Its impact is the absolute "
                             "change TY - YA; no percentage growth is computed.", f["note"])
        ws.write(note_at + 1, 0, "Before/After shares under Contribution are shares of the "
                                 "category total in each dataset.", f["note"])
    ws.freeze_panes(5, 1)


def _sheet_ranked(wb, f, block, title, cat, metric, mkt_label):
    ws = wb.add_worksheet(title[:31])
    ws.set_column("A:A", 36)
    ws.set_column("B:J", 14)

    scale, unit = _scale_of(*[b["after_current"] for b in block],
                            *[b["before_current"] for b in block])
    suffix = f" ({unit})" if unit else ""

    ws.write(0, 0, f"{title} - {metric}{suffix}", f["title"])
    ws.write(1, 0, f"Category: {cat}   |   Market: {mkt_label}   |   "
                   "Selected as the largest in the previous dataset, followed into "
                   "the updated one", f["subtitle"])
    r = 3
    # Column order: Entity, then the MAT YA / MAT TY levels of BEFORE and AFTER,
    # then the share change, then the ranks. This is the Market Regions layout, so
    # the two tables read the same way - the levels lead and the rank is the
    # consequence of the movement rather than the headline. Growth is
    # deliberately absent: for a ranked entity the share change is the comparable
    # movement, and a growth column here was the odd one out.
    headers = ["Entity", "BEFORE MAT YA", "BEFORE MAT TY", "AFTER MAT YA",
               "AFTER MAT TY", "Share change",
               "Rank BEFORE", "Rank AFTER", "Rank change", "Movement"]
    for i, h in enumerate(headers):
        ws.write(r, i, h, f["hdr_grey"])
    r += 1

    for b in block:
        ws.write(r, 0, b["name"], f["label"])
        # MAT YA / MAT TY on each side are that side's prior and current periods -
        # the same source the Market Regions block reads (`mat_ya` / `mat_ty`).
        values = [(1, b.get("before_prior")), (2, b.get("before_current")),
                  (3, b.get("after_prior")), (4, b.get("after_current"))]
        for col, v in values:
            if v is not None:
                ws.write_number(r, col, v / scale, f["val2"])
        sc = b.get("share_change_pp")
        if sc is not None:
            ws.write_number(r, 5, sc / 100, f["pp_red"] if sc < 0 else f["pp"])
        ws.write_number(r, 6, b["rank_before"] or 0, f["val"])
        ws.write_number(r, 7, b["rank_after"] or 0, f["val"])
        rc = b["rank_change"]
        ws.write_number(r, 8, rc if rc is not None else 0,
                        f["pp_red"] if (rc is not None and rc < 0) else f["pp"])
        mv = b.get("movement") or ""
        ws.write(r, 9, mv, f["label_b"] if mv in ("NEW", "EXITED") else f["label"])
        r += 1

    ws.write(r + 1, 0, "Selection: the largest entities in the PREVIOUS dataset (by MAT TY "
                       "there), then each followed into the updated one - so an entity that "
                       "led before and shrank after still appears, at the top.",
             f["note"])
    ws.write(r + 2, 0, "MAT YA / MAT TY are each side's own MAT - the previous dataset's "
                       "under BEFORE and the updated one's under AFTER. Share change is of "
                       "the category total. No growth column, matching the Market Regions "
                       "block.",
             f["note"])
    ws.write(r + 3, 0, "Movement: NEW = only in the updated dataset, EXITED = only in the "
                       "previous dataset, GAINED/LOST = rank improved/declined, HELD = unchanged.",
             f["note"])
    ws.freeze_panes(4, 1)


def _sheet_clients(wb, f, report, cat, metric, mkt_label):
    ws = wb.add_worksheet("Client Brands")
    ws.set_column("A:A", 36)
    ws.set_column("B:L", 14)
    ws.write(0, 0, f"Client entities tracked - {metric}", f["title"])
    ws.write(1, 0, f"Category: {cat}   |   Market: {mkt_label}   |   "
                   "Tracked independently of the Top-N cut", f["subtitle"])
    r = 3
    headers = ["Client entity", "Level", "Found", "In Top-N",
               "BEFORE MAT YA", "BEFORE MAT TY", "AFTER MAT YA", "AFTER MAT TY",
               "Rank BEFORE", "Rank AFTER", "Share chg (pp)", "Movement"]
    for i, h in enumerate(headers):
        ws.write(r, i, h, f["hdr_grey"])
    r += 1

    rows = []
    for b in report.get("client_brands") or []:
        rows.append(("brand", b))
    for b in report.get("client_manufacturers") or []:
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
                ws.write_number(r, col, v, f["val"])
        ws.write_number(r, 8, b.get("rank_before") or 0, f["val"])
        ws.write_number(r, 9, b.get("rank_after") or 0, f["val"])
        sc = b.get("share_change_pp")
        if sc is not None:
            ws.write_number(r, 10, sc / 100, f["pp_red"] if sc < 0 else f["pp"])
        ws.write(r, 11, b.get("movement") or "", f["label"])
        r += 1

    if not rows:
        ws.write(r, 0, "No client entities were selected.", f["note"])


def _sheet_contributors(wb, f, report, cat, metric, mkt_label):
    ws = wb.add_worksheet("Contributors")
    ws.set_column("A:A", 12)
    ws.set_column("B:B", 36)
    ws.set_column("C:C", 18)
    ws.set_column("D:D", 22)
    ws.write(0, 0, f"Major contributors to the change - {metric}", f["title"])
    ws.write(1, 0, f"Category: {cat}   |   Market: {mkt_label}", f["subtitle"])
    r = 3
    for c in report["contributors"]:
        ws.write(r, 0, c["level"].title(), f["hdr_grey"])
        ws.write(r, 1, "Entity", f["hdr_grey"])
        ws.write(r, 2, "Absolute change", f["hdr_grey"])
        ws.write(r, 3, "Contribution to change", f["hdr_grey"])
        r += 1
        for kind, rows in (("Gainers", c["gainers"]), ("Losers", c["losers"])):
            ws.write(r, 0, kind, f["label_b"])
            r += 1
            for it in rows:
                ws.write(r, 1, it["name"], f["label"])
                if it["abs_change"] is not None:
                    ws.write_number(r, 2, it["abs_change"], f["val2"])
                v = it["contribution_to_change_pct"]
                if v is not None:
                    ws.write_number(r, 3, v / 100, f["pct_red"] if v < 0 else f["pct"])
                r += 1
        r += 1


def _sheet_qc(wb, f, qc, cat, meta=None):
    ws = wb.add_worksheet("QC")
    ws.set_column("A:A", 28)
    ws.set_column("B:B", 10)
    ws.set_column("C:C", 70)
    ws.set_column("D:D", 60)
    ws.write(0, 0, "Automated QC", f["title"])
    ws.write(1, 0, f"Category: {cat}   |   Overall: {(qc or {}).get('worst', 'n/a')}",
             f["subtitle"])
    r = 3
    for i, h in enumerate(["Check", "Status", "Message", "Detail"]):
        ws.write(r, i, h, f["hdr_grey"])
    r += 1
    for c in (qc or {}).get("checks", []):
        ws.write(r, 0, c["name"], f["label_b"])
        ws.write(r, 1, c["status"], f["label"])
        ws.write(r, 2, c["message"], f["wrap"])
        detail = c.get("detail") or {}
        txt = ", ".join(f"{k}={v}" for k, v in list(detail.items())[:4]
                        if not isinstance(v, (list, dict)))
        ws.write(r, 3, txt, f["wrap"])
        r += 1
    counts = (qc or {}).get("counts", {})
    r += 1
    ws.write(r, 0, "Totals", f["label_b"])
    ws.write(r, 1, f"PASS {counts.get('PASS', 0)} / WARN {counts.get('WARN', 0)} / "
                   f"FAIL {counts.get('FAIL', 0)}", f["label"])

    # What the run did to the data - chiefly how much of it is in scope. Recorded
    # in the deliverable so a reader of the workbook knows the coverage without
    # having to be told. Deliberately below the checks and not counted as one:
    # these are statements of fact, not verifications, so folding them into the
    # totals would misstate how many checks passed.
    notes = [n for n in (meta or {}).get("run_notes") or [] if n]
    if notes:
        r += 2
        ws.write(r, 0, "Run notes", f["label_b"])
        r += 1
        for n in notes:
            ws.write(r, 0, "•", f["label"])
            ws.write(r, 1, n, f["wrap"])
            r += 1
