# PowerPoint template

Put your standard presentation here, named exactly:

```
templates/impact_template.pptx
```

That is the file the export uses as its source. Everything about how a deck
*looks* comes from it — the slide master, the theme colours, the fonts, the
background graphics, and the slide size. Everything about what a deck *says*
comes from the run.

## How it is used

1. The template is opened as the base presentation.
2. **Its own slides are removed.** They are the template author's sample content,
   and leaving them in would put a stale cover or an example table in front of
   the client. The master, the layouts and the theme are kept — that is the part
   that carries the formatting.
3. Each slide is added on a layout with **no content placeholders**, and every
   placeholder the layout brings with it is then stripped from the slide. That is
   what keeps an empty "Click to add text" box — a vertical one, on the stock
   `Vertical Title and Text` layout — off a generated slide.

If the file is not found under the exact name but exactly one `.pptx` is sitting
in this folder, that one is used. Two or more, and the exact name is required.

## What the export writes

Per category, in this order, **per selected metric**:

| slide | content |
| --- | --- |
| Headline impact | BEFORE / AFTER MAT TY, level shift, absolute change |
| Market / channel | the channel rows — one slide |
| Market / region | the region rows — one slide, when the study has regions |
| Top N Manufacturers | the largest in the previous database, followed into the updated one |
| Top N Brands | the same shape |
| What drove the change | the largest gainers and losers |
| Client entities tracked | once, on the headline metric |

A run measuring Sales Value, Volume and ND therefore produces that set three
times. There is **no cover slide, no chart and no QC slide** — all three were
removed on request. Add charts by hand where you want them.

Every table has black borders, a compact layout, and **negative figures in red** —
the same convention the workbook uses, so a reader comparing the two does not
have to re-learn the colours.

## If the file is absent

The export still runs: it falls back to a plain 16:9 presentation with no theme.
A missing template is never a failed run, but the output will not carry your
branding. The run log records which template was used.

## Using a template somewhere else

Set `IMPACT_PPT_TEMPLATE` to a full path, or pass `template_path` in the export
request. The repository location above is the default.
