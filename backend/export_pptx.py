"""Per-category PowerPoint export.

Builds the impact-study deck: headline before/after, channel movement, level
shift and contribution, ranked entities with their movement, brand share, the
tracked client entities, and a QC appendix.
"""

from __future__ import annotations

import os
from typing import Any, Sequence

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION, XL_LABEL_POSITION
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.util import Emu, Inches, Pt

# --- palette -----------------------------------------------------------------
INK = RGBColor(0x1F, 0x28, 0x37)
MUTED = RGBColor(0x6B, 0x72, 0x80)
GREEN = RGBColor(0x54, 0x8C, 0x3C)
BLUE = RGBColor(0x2E, 0x6D, 0xA4)
RED = RGBColor(0xC0, 0x00, 0x00)
LIGHT_GREEN = RGBColor(0xE2, 0xEF, 0xDA)
LIGHT_BLUE = RGBColor(0xDD, 0xEB, 0xF7)
GREY = RGBColor(0xF2, 0xF2, 0xF2)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)

SW, SH = Inches(13.333), Inches(7.5)


def _scale_of(*values) -> tuple[float, str]:
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


def _fmt(v, scale=1.0, unit="") -> str:
    if v is None:
        return "-"
    try:
        x = float(v) / scale
    except Exception:
        return "-"
    if unit:
        return f"{x:,.2f}{unit}"
    return f"{x:,.0f}"


def _pct(v) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):+.1f}%"
    except Exception:
        return "-"


def _pp(v) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):+.1f}pp"
    except Exception:
        return "-"


def _textbox(slide, l, t, w, h, text, size=14, bold=False, color=INK,
             align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, wrap=True):
    tb = slide.shapes.add_textbox(l, t, w, h)
    tf = tb.text_frame
    tf.word_wrap = wrap
    tf.vertical_anchor = anchor
    p = tf.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    run.text = text
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = color
    return tb


def _rect(slide, l, t, w, h, fill, line=None):
    from pptx.enum.shapes import MSO_SHAPE

    sh = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, l, t, w, h)
    sh.fill.solid()
    sh.fill.fore_color.rgb = fill
    if line is None:
        sh.line.fill.background()
    else:
        sh.line.color.rgb = line
    sh.shadow.inherit = False
    try:
        sh.adjustments[0] = 0.06
    except Exception:
        pass
    return sh


def _kpi_card(slide, l, t, w, h, label, value, sub="", value_color=INK):
    _rect(slide, l, t, w, h, GREY)
    _textbox(slide, l + Inches(0.15), t + Inches(0.10), w - Inches(0.3),
             Inches(0.28), label, size=10.5, color=MUTED)
    _textbox(slide, l + Inches(0.15), t + Inches(0.36), w - Inches(0.3),
             Inches(0.45), value, size=22, bold=True, color=value_color)
    if sub:
        _textbox(slide, l + Inches(0.15), t + Inches(0.85), w - Inches(0.3),
                 Inches(0.30), sub, size=10, color=MUTED)


def _title(slide, text, sub=None):
    _textbox(slide, Inches(0.6), Inches(0.32), SW - Inches(1.2), Inches(0.6),
             text, size=26, bold=True)
    if sub:
        _textbox(slide, Inches(0.6), Inches(0.95), SW - Inches(1.2), Inches(0.35),
                 sub, size=12, color=MUTED)
    line = slide.shapes.add_shape(1, Inches(0.6), Inches(1.34), SW - Inches(1.2),
                                  Emu(9525))
    line.fill.solid(); line.fill.fore_color.rgb = RGBColor(0xD9, 0xD9, 0xD9)
    line.line.fill.background(); line.shadow.inherit = False


def _table(slide, headers, rows, l, t, w, h, col_w=None, font=10,
           highlight_rows: set[int] = frozenset()):
    n_rows, n_cols = len(rows) + 1, len(headers)
    shape = slide.shapes.add_table(n_rows, n_cols, l, t, w, h)
    tbl = shape.table
    if col_w:
        total = sum(col_w)
        for i, cw in enumerate(col_w):
            tbl.columns[i].width = Emu(int(w * cw / total))
    for j, htxt in enumerate(headers):
        c = tbl.cell(0, j)
        c.text = str(htxt)
        c.fill.solid(); c.fill.fore_color.rgb = GREY
        p = c.text_frame.paragraphs[0]
        p.font.size = Pt(font); p.font.bold = True; p.font.color.rgb = INK
        c.vertical_anchor = MSO_ANCHOR.MIDDLE
        c.margin_top = c.margin_bottom = Emu(0)
    for i, row in enumerate(rows, start=1):
        for j, val in enumerate(row):
            c = tbl.cell(i, j)
            c.text = "" if val is None else str(val)
            c.fill.solid()
            c.fill.fore_color.rgb = (RGBColor(0xFF, 0xF7, 0xE6)
                                     if (i - 1) in highlight_rows else WHITE)
            p = c.text_frame.paragraphs[0]
            p.font.size = Pt(font)
            p.font.color.rgb = INK
            if j > 0:
                p.alignment = PP_ALIGN.RIGHT
            c.vertical_anchor = MSO_ANCHOR.MIDDLE
            c.margin_top = c.margin_bottom = Emu(0)
    return tbl


def _chart(slide, kind, categories, series, l, t, w, h, title=None,
           colors=(GREEN, BLUE), number_format='#,##0', gap=60, legend=True,
           overlap=-10):
    cd = CategoryChartData()
    cd.categories = categories
    for name, values in series:
        cd.add_series(name, values)
    gf = slide.shapes.add_chart(kind, l, t, w, h, cd)
    ch = gf.chart
    ch.has_title = bool(title)
    if title:
        ch.chart_title.text_frame.text = title
        for r in ch.chart_title.text_frame.paragraphs[0].runs:
            r.font.size = Pt(12); r.font.bold = True; r.font.color.rgb = INK
    ch.has_legend = legend
    if legend:
        ch.legend.position = XL_LEGEND_POSITION.TOP
        ch.legend.include_in_layout = False
        ch.legend.font.size = Pt(10)
    for i, plot_series in enumerate(ch.plots[0].series):
        plot_series.format.fill.solid()
        plot_series.format.fill.fore_color.rgb = colors[i % len(colors)]
    ch.plots[0].gap_width = gap
    try:
        ch.plots[0].overlap = overlap
    except Exception:
        pass
    try:
        ch.value_axis.tick_labels.number_format = number_format
        ch.value_axis.tick_labels.number_format_is_linked = False
        ch.value_axis.tick_labels.font.size = Pt(9)
        ch.category_axis.tick_labels.font.size = Pt(9)
    except Exception:
        pass
    return ch


# ----------------------------------------------------------------------------


def build_category_deck(report: dict, qc: dict, out_path: str,
                        meta: dict | None = None) -> str:
    meta = meta or {}
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    prs = Presentation()
    prs.slide_width, prs.slide_height = SW, SH
    blank = prs.slide_layouts[6]

    cat = report.get("category", "")
    markets = report.get("markets") or []
    mkt_label = ", ".join(markets) if markets else "All markets"

    # Metrics to present. A run may carry Sales Value, Volume and ND together;
    # each gets its own headline and level-shift slides, so the deck never shows
    # one metric's numbers under another metric's heading. A metric with no
    # growth (ND) replaces the growth line with the absolute change.
    metrics = report.get("metrics") or {}
    if not metrics:
        metrics = {report.get("metric_key") or "primary": {
            "label": report.get("metric", ""),
            "growth_applicable": report.get("growth_applicable", True),
            "total": report.get("total") or {},
            "channel_block": report.get("channel_block") or [],
        }}
    metric_word = "  ·  ".join(b.get("label") or k for k, b in metrics.items())
    primary = report.get("metric") or next(iter(metrics.values())).get("label", "")

    t = report["total"]

    # Scale for the entity slides below. They present the *primary* metric (the
    # one whose numbers sit in `report["total"]`), so the scale comes from that
    # metric's channel block. Using another metric's column here would mislabel
    # every figure on those slides.
    ch_blocks = report.get("channel_block") or []
    scale, unit = _scale_of(*[b["after"]["mat_ty"] for b in ch_blocks],
                            *[b["before"]["mat_ty"] for b in ch_blocks])

    # ---- 1. Title -----------------------------------------------------------
    s = prs.slides.add_slide(blank)
    _rect(s, Inches(0), Inches(0), SW, Inches(2.6), RGBColor(0x1F, 0x28, 0x37))
    _textbox(s, Inches(0.8), Inches(0.75), SW - Inches(1.6), Inches(0.9),
             f"{cat}", size=38, bold=True, color=WHITE)
    _textbox(s, Inches(0.8), Inches(1.7), SW - Inches(1.6), Inches(0.5),
             f"Data Update Impact Study   |   {metric_word}   |   {mkt_label}",
             size=15, color=RGBColor(0xBF, 0xC9, 0xD6))
    _textbox(s, Inches(0.8), Inches(3.1), SW - Inches(1.6), Inches(0.4),
             "Before vs After comparison of the previous and updated datasets",
             size=14, color=MUTED)
    if meta.get("period_label"):
        _textbox(s, Inches(0.8), Inches(3.6), SW - Inches(1.6), Inches(0.4),
                 meta["period_label"], size=12, color=MUTED)

    # Who the analysis is for - the thing that distinguishes two decks produced
    # for the same client.
    who = "   |   ".join(x for x in (meta.get("client_name"),
                                     meta.get("impact_name")) if x)
    if who:
        _textbox(s, Inches(0.8), Inches(2.45), SW - Inches(1.6), Inches(0.45),
                 who, size=14, color=RGBColor(0xBF, 0xC9, 0xD6))

    for key, blk in metrics.items():
        metric = blk.get("label") or key
        mt = blk.get("total") or {}
        growth_ok = blk.get("growth_applicable", True)
        m_ch = blk.get("channel_block") or []
        m_scale, m_unit = _scale_of(*[b["after"]["mat_ty"] for b in m_ch],
                                    *[b["before"]["mat_ty"] for b in m_ch])
        # ---- 2. Headline (per metric) --------------------------------------
        s = prs.slides.add_slide(blank)
        base_note = ""
        if meta.get("baseline_market"):
            base_note = (f"  |  shares vs {meta['baseline_market']}"
                         + ("" if meta.get("baseline_verified") else " (not verified)"))
        _title(s, f"Headline impact - {metric}", f"{cat} | {metric} | {mkt_label}{base_note}")
        cw, gap = Inches(2.85), Inches(0.22)
        x0 = Inches(0.6)
        y0 = Inches(1.75)
        hcard = Inches(1.35)

        _kpi_card(s, x0, y0, cw, hcard, "BEFORE - MAT TY",
                  _fmt(mt.get("before_current"), m_scale, m_unit),
                  f"MAT YA {_fmt(mt.get('before_prior'), m_scale, m_unit)}")
        _kpi_card(s, x0 + (cw + gap), y0, cw, hcard, "AFTER - MAT TY",
                  _fmt(mt.get("after_current"), m_scale, m_unit),
                  f"MAT YA {_fmt(mt.get('after_prior'), m_scale, m_unit)}")
        ls = mt.get("level_shift_pp") if growth_ok else None
        if growth_ok:
            _kpi_card(s, x0 + 2 * (cw + gap), y0, cw, hcard, "LEVEL SHIFT",
                      _pp(ls), "after growth - before growth",
                      value_color=RED if (ls is not None and ls < 0) else INK)
        else:
            _kpi_card(s, x0 + 2 * (cw + gap), y0, cw, hcard, "TOP (TY)",
                      _fmt(mt.get("after_current"), m_scale, m_unit),
                      "highest channel by TY")
        ac = mt.get("abs_change")
        _kpi_card(s, x0 + 3 * (cw + gap), y0, cw, hcard, "ABSOLUTE CHANGE",
                  _fmt(ac, m_scale, m_unit),
                  ("TY - YA (no growth for this metric)" if not growth_ok
                   else f"on {_fmt(mt.get('before_current'), m_scale, m_unit)} base"),
                  value_color=RED if (ac is not None and ac < 0) else INK)

        if growth_ok:
            # before/after growth line
            y1 = Inches(3.45)
            _rect(s, Inches(0.6), y1, SW - Inches(1.2), Inches(1.5), GREY)
            _textbox(s, Inches(0.85), y1 + Inches(0.12), Inches(6), Inches(0.3),
                     "Growth in MAT TY vs MAT YA", size=12, bold=True)
            _textbox(s, Inches(0.85), y1 + Inches(0.5), Inches(3), Inches(0.35),
                     "Previous dataset", size=11, color=MUTED)
            _textbox(s, Inches(0.85), y1 + Inches(0.85), Inches(3), Inches(0.45),
                     _pct(mt.get("before_growth_pct")), size=20, bold=True,
                     color=RED if (mt.get("before_growth_pct") or 0) < 0 else INK)
            _textbox(s, Inches(5.0), y1 + Inches(0.5), Inches(3), Inches(0.35),
                     "Updated dataset", size=11, color=MUTED)
            _textbox(s, Inches(5.0), y1 + Inches(0.85), Inches(3), Inches(0.45),
                     _pct(mt.get("after_growth_pct")), size=20, bold=True,
                     color=RED if (mt.get("after_growth_pct") or 0) < 0 else INK)
            _textbox(s, Inches(9.2), y1 + Inches(0.5), Inches(3.4), Inches(0.35),
                     "Impact on the read", size=11, color=MUTED)
            _textbox(s, Inches(9.2), y1 + Inches(0.85), Inches(3.4), Inches(0.45),
                     _pp(ls), size=20, bold=True,
                     color=RED if (ls is not None and ls < 0) else GREEN)
        else:
            # No growth for this metric: show the level movement instead.
            y1 = Inches(3.45)
            _rect(s, Inches(0.6), y1, SW - Inches(1.2), Inches(1.5), GREY)
            _textbox(s, Inches(0.85), y1 + Inches(0.12), Inches(9), Inches(0.3),
                     "Distribution level - impact reported as absolute change",
                     size=12, bold=True)
            _textbox(s, Inches(0.85), y1 + Inches(0.5), Inches(4), Inches(0.35),
                     "MAT YA (previous)", size=11, color=MUTED)
            _textbox(s, Inches(0.85), y1 + Inches(0.85), Inches(4), Inches(0.45),
                     _fmt(mt.get("before_prior"), m_scale, m_unit), size=20, bold=True)
            _textbox(s, Inches(5.2), y1 + Inches(0.5), Inches(4), Inches(0.35),
                     "MAT TY (updated)", size=11, color=MUTED)
            _textbox(s, Inches(5.2), y1 + Inches(0.85), Inches(4), Inches(0.45),
                     _fmt(mt.get("after_current"), m_scale, m_unit), size=20, bold=True)
            _textbox(s, Inches(9.4), y1 + Inches(0.5), Inches(3.2), Inches(0.35),
                     "Absolute change (TY - YA)", size=11, color=MUTED)
            _textbox(s, Inches(9.4), y1 + Inches(0.85), Inches(3.2), Inches(0.45),
                     _fmt(ac, m_scale, m_unit), size=20, bold=True,
                     color=RED if (ac is not None and ac < 0) else INK)

        # ---- 3. Channel before/after chart (per metric) --------------------
        if m_ch:
            s = prs.slides.add_slide(blank)
            _title(s, f"Market / channel movement - {metric}",
                   f"MAT TY before vs after, {metric} ({m_unit or 'absolute'})")
            top = m_ch[:10]
            cats = [b["name"][:26] for b in top]
            before = [(b["before"]["mat_ty"] or 0) / m_scale for b in top]
            after = [(b["after"]["mat_ty"] or 0) / m_scale for b in top]
            _chart(s, XL_CHART_TYPE.COLUMN_CLUSTERED, cats,
                   [("BEFORE", before), ("AFTER", after)],
                   Inches(0.6), Inches(1.6), SW - Inches(1.2), Inches(4.6),
                   colors=(GREEN, BLUE), number_format='#,##0')

            # ---- 3b. Level shift / absolute change + contribution ----------
            s = prs.slides.add_slide(blank)
            # The row to shade as the total: the leading Total Market when one is
            # designated, otherwise the trailing sum we append ourselves. Resolved
            # to a concrete index once `rows` is known. `report["baseline"]["name"]`
            # is the authoritative signal; each metric block carries its own
            # baseline, falling back to the report's, because the wrong name would
            # silently reinstate a double-count on a secondary metric's slide.
            has_baseline = bool(((blk.get("baseline") or report.get("baseline") or {})
                                 .get("name")))
            if growth_ok:
                _title(s, f"Level shift and contribution - {metric}",
                       "How each channel's growth and share of the category moved")
                rows = []
                for b in m_ch[:11]:
                    rows.append([
                        b["name"][:30],
                        _fmt(b["before"]["mat_ty"], m_scale, m_unit),
                        _fmt(b["after"]["mat_ty"], m_scale, m_unit),
                        _pct(b["before"]["growth_pct"]),
                        _pct(b["after"]["growth_pct"]),
                        _pp(b["level_shift"]["mat_ty_pp"]),
                        f"{(b['contribution']['before_share_pct'] or 0):.1f}%",
                        f"{(b['contribution']['after_share_pct'] or 0):.1f}%",
                    ])
                # A Total row is appended only when no Total Market baseline was
                # designated. With a baseline, `m_ch[0]` *is* the Total Market, and
                # `report["total"]` is the sum over every row of the category - the
                # Total Market plus its own members - so printing it here would
                # double-count the block it annotates.
                if not has_baseline:
                    rows.append([
                        "Total", _fmt(mt.get("before_current"), m_scale, m_unit),
                        _fmt(mt.get("after_current"), m_scale, m_unit),
                        _pct(mt.get("before_growth_pct")), _pct(mt.get("after_growth_pct")),
                        _pp(ls), "100.0%", "100.0%",
                    ])
                total_row_idx = 0 if has_baseline else len(rows) - 1
                _table(s, ["Channel", "BEFORE MAT TY", "AFTER MAT TY", "BEFORE growth",
                           "AFTER growth", "Level shift", "Contrib before", "Contrib after"],
                       rows, Inches(0.6), Inches(1.7), SW - Inches(1.2), Inches(4.9),
                       col_w=[30, 11, 11, 10, 10, 10, 10, 10], font=9.5,
                       highlight_rows={total_row_idx})
            else:
                _title(s, f"Top channels and absolute change - {metric}",
                       "Distribution level: TY minus YA, no percentage growth")
                rows = []
                for b in m_ch[:12]:
                    rows.append([
                        b["name"][:30],
                        _fmt(b["before"]["mat_ya"], m_scale, m_unit),
                        _fmt(b["before"]["mat_ty"], m_scale, m_unit),
                        _fmt(b["after"]["mat_ya"], m_scale, m_unit),
                        _fmt(b["after"]["mat_ty"], m_scale, m_unit),
                        _fmt(b.get("abs_change"), m_scale, m_unit),
                    ])
                if not has_baseline:
                    rows.append([
                        "Total", _fmt(mt.get("before_prior"), m_scale, m_unit),
                        _fmt(mt.get("before_current"), m_scale, m_unit),
                        _fmt(mt.get("after_prior"), m_scale, m_unit),
                        _fmt(mt.get("after_current"), m_scale, m_unit),
                        _fmt(ac, m_scale, m_unit),
                    ])
                total_row_idx = 0 if has_baseline else len(rows) - 1
                _table(s, ["Top channel (by TY)", "BEFORE MAT YA", "BEFORE MAT TY",
                           "AFTER MAT YA", "AFTER MAT TY", "Abs change (TY - YA)"],
                       rows, Inches(0.6), Inches(1.7), SW - Inches(1.2), Inches(4.9),
                       col_w=[34, 14, 14, 14, 14, 16], font=10,
                       highlight_rows={total_row_idx})

    # ---- 5. Manufacturer Top-N ---------------------------------------------
    mtop = report.get("manufacturer_top_n") or []
    if mtop:
        s = prs.slides.add_slide(blank)
        _title(s, f"Top {len(mtop)} manufacturers",
               f"The largest in the previous database, followed into the updated one "
               f"({primary})")
        rows = []
        for b in mtop:
            rows.append([
                b["name"][:30],
                _fmt(b.get("before_prior"), scale, unit),
                _fmt(b.get("before_current"), scale, unit),
                _fmt(b.get("before_current"), scale, unit),
                _fmt(b.get("after_current"), scale, unit),
                _pp(b.get("share_change_pp")),
                b["rank_before"] or "-",
                b["rank_after"] or "-",
                (f"{b['rank_change']:+d}" if b.get("rank_change") is not None else "-"),
                b.get("movement") or "",
            ])
        _table(s, ["Manufacturer", "MAT YA", "MAT TY", "Before", "After",
                   "Share chg", "Rank BEFORE", "Rank AFTER", "Rank chg", "Movement"],
               rows, Inches(0.6), Inches(1.7), SW - Inches(1.2), Inches(4.9),
               col_w=[24, 9, 9, 9, 9, 9, 9, 9, 8, 10], font=9,
               highlight_rows={i for i, b in enumerate(mtop)
                               if b.get("movement") in ("NEW", "EXITED")})

    # ---- 6. Client brands ---------------------------------------------------
    clients = (report.get("client_brands") or []) + \
              (report.get("client_manufacturers") or [])
    if clients:
        s = prs.slides.add_slide(blank)
        _title(s, "Client entities tracked",
               "Reported independently of the Top-N cut")
        rows = []
        for b in clients:
            if not b.get("found", True):
                rows.append([b.get("name", ""), "-", "-", "-", "-", "-",
                             "not found", b.get("note", "")])
                continue
            rows.append([
                b["name"][:28],
                _fmt(b["before_current"], scale, unit),
                _fmt(b["after_current"], scale, unit),
                b.get("rank_before") or "-", b.get("rank_after") or "-",
                _pp(b.get("share_change_pp")),
                "Yes" if b.get("in_top_n") else "No",
                b.get("movement") or "",
            ])
        _table(s, ["Client entity", "BEFORE MAT TY", "AFTER MAT TY",
                   "Rank BEFORE", "Rank AFTER", "Share chg", "In Top-N", "Movement"],
               rows, Inches(0.6), Inches(1.7), SW - Inches(1.2), Inches(4.5),
               col_w=[26, 12, 12, 10, 10, 10, 9, 11], font=9.5)

    # ---- 8. Contributors ----------------------------------------------------
    if report.get("contributors"):
        s = prs.slides.add_slide(blank)
        _title(s, "What drove the change",
               "Largest absolute gainers and losers in the category")
        y = Inches(1.7)
        for c in report["contributors"]:
            _textbox(s, Inches(0.6), y, Inches(6), Inches(0.3),
                     f"{c['level'].title()}s", size=13, bold=True)
            y += Inches(0.38)
            rows = []
            for it in c["gainers"][:5]:
                rows.append([it["name"][:30], "Gain", _fmt(it["abs_change"], scale, unit),
                             _pct(it["contribution_to_change_pct"])])
            for it in c["losers"][:5]:
                rows.append([it["name"][:30], "Loss", _fmt(it["abs_change"], scale, unit),
                             _pct(it["contribution_to_change_pct"])])
            _table(s, ["Entity", "Direction", "Absolute change", "Contribution to change"],
                   rows, Inches(0.6), y, SW - Inches(1.2), Inches(0.32 * len(rows) + 0.1),
                   col_w=[44, 14, 20, 22], font=9)
            y += Inches(0.32 * len(rows) + 0.35)
            if y > Inches(6.4):
                break

    # ---- 9. QC appendix -----------------------------------------------------
    s = prs.slides.add_slide(blank)
    _title(s, "Automated QC", f"Overall status: {(qc or {}).get('worst', 'n/a')}")
    rows = []
    for c in (qc or {}).get("checks", []):
        rows.append([c["name"][:34], c["status"], c["message"][:110]])
    _table(s, ["Check", "Status", "Message"], rows,
           Inches(0.6), Inches(1.7), SW - Inches(1.2), Inches(4.9),
           col_w=[24, 10, 66], font=9)
    counts = (qc or {}).get("counts", {})
    _textbox(s, Inches(0.6), Inches(6.75), SW - Inches(1.2), Inches(0.35),
             f"PASS {counts.get('PASS', 0)}   |   WARN {counts.get('WARN', 0)}   |   "
             f"FAIL {counts.get('FAIL', 0)}", size=11, bold=True)

    prs.save(out_path)
    return out_path
