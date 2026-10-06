# Impact Studio

An interactive application that automates **data-update impact studies** — the
analysis run whenever a dataset is refreshed, to see how the new data has moved
versus the previous release.

It replaces a manual, per-category workflow with one that probes both datasets,
proposes the dimension mappings between them, runs the comparison, validates the
numbers, and bulk-generates a category-level Excel + PowerPoint pack.

---

## Why it is built this way

Three constraints from the brief drive the design:

1. **The two datasets will not share a structure.** A category called `Tandy` in
   the old data may arrive as `Category + Subcategory` in the new data. The app
   therefore *probes and proposes* rather than assuming, and nothing is applied
   silently.
2. **Nothing may be silently dropped.** Entities present in only one dataset are
   labelled `NEW` / `EXITED`, unmatched values are preserved and flagged, and a
   QC panel reports what it checked.
3. **Impacts are delivered per category.** A client with 10–15 categories should
   select them all once and get 10–15 Excel + PowerPoint pairs.

---

## Quick start

```bash
cd impact_studio
python -m pip install -r requirements.txt
python run.py
```

Opens `http://127.0.0.1:8777/`. Outputs are written to `./outputs/`.

Options: `--port 9000`, `--host`, `--no-browser`, `--reload`.

**If the port is already taken** the launcher does not fail with a bare
`WinError 10048`. It says who holds the port, how to stop them, and starts on the
next free port instead:

```
! port 8777 is already in use by PID 2180 (python.exe).
  This is usually an Impact Studio server you left running.
  Stop it with:  taskkill /F /PID 2180
  Starting on port 8778 instead (override with --port).
```

`--port 0` lets the OS pick a free port. Read the URL the launcher prints — if you
left a server running, the app will be on a different port than you expect.

---

## The workflow

| Step | What happens |
|---|---|
| **1 · Datasets** | Register dataset A (previous) and B (updated). Either two files, or one workbook split on a discriminator column (e.g. `Dataset` = `Current MAT` / `New MAT`). |
| **2 · Profile** | Both datasets are read and profiled: rows, columns, null rates, cardinality, detected dimensions and metric families. Column roles are editable. |
| **3 · Dimension Mapping** | Markets, manufacturers and brands are matched A→B with layered evidence. Each row shows status, method, confidence and why. **The market hierarchy is identified here too** — see below. |
| **4 · Category Mapping** | Categories are mapped explicitly — 1:1, composite, 1:N or N:1. Worked **one category at a time** by default. See below. |
| **5 · Selection** | Metric (per dataset), periods, display unit and decimals, the market level, categories, Top-N, client entities to track, and the client / impact name. |
| **6 · Analysis & QC** | The headline insights (MAT AD Growth, MAT AD Level Shift, Contribution), before/after comparison, Top-N movement, and the automated validation results. |
| **7 · Bulk Export** | One `.xlsx` and one `.pptx` per category, in a folder per category, plus a run-level file index. |

The intended order is **market mapping → identify the Total Market → category
mapping → before/after → insights**, so the impact is always computed against a
known market definition rather than against every market value at once.

---

## Market hierarchy (step 3)

A market dimension is **not flat**. It normally carries the Total Market alongside
the regions and channels that make it up:

```
TW Total TW Offline (G)                     <- the Total
CVS / TW Total TW Offline (G) / MT w/o ...  <- a channel, child of the Total
PX MART / TW Total TW Offline (G) / ...
```

Treating those as equivalent is a correctness problem, not a cosmetic one. In the
reference workbook the channels sum to **55.5%** of the Total (385bn of 694bn),
because the Total covers channels the extract does not list. So adding the Total
to a block of channels double-counts it, and dividing by that block's sum
understates every share in it.

The classifier labels each market value `total`, `region` or `channel`, using:

1. **The hierarchy path**, when the workbook has one (the reference workbook keeps
   `UU Market Name (New)` in a separate lookup sheet; it is found automatically,
   or you can point at it). A market whose path *is* itself is a root; one whose
   path *contains* the root's name is its child.
2. **Name keywords** — `Total` / `Grand Total` / `Overall`, and region words.

Anything left over is labelled **channel (assumed)** with a *confirm* flag rather
than being treated as settled, and every row can be overridden. The panel reports
what the parts cover as a share of the Total, which is the evidence that they are
a subset.

**The analysis level defaults to the Total Market.** Choosing it scopes the
analysis to the total rows — not to every market value.

---

## Contribution is measured against the Total Market

Share and contribution are computed against the **Total Market**, never against
the sum of whatever entities happen to be in a block:

```
share          = entity value / Total Market value
of change      = entity change / Total Market change
```

There are two denominators, and they are deliberately different:

| Denominator | Used for | Why |
|---|---|---|
| Total Market **per category** | the market/channel block *within* a category | a channel's share of that category's total |
| Total Market **across the categories in scope** | the category's own contribution | per category it would be trivially 100% |

Both are computed from the Total rows **before** the market-scope filter, so the
denominator still exists when the scope is "channels only" and the Total rows are
not in the working frame. When no Total Market is designated, the baseline is
reported **unverified** and the note says what the share is actually of, rather
than quietly presenting a share of the wrong denominator.

If you analyse a subset of categories, the shares are of the Total Market **over
those categories** — which is why the scope size is reported alongside them.

---

## Headline insights

Each category report opens with three insights, in this order:

| Insight | Meaning |
|---|---|
| **MAT AD Growth** | before vs after, on the wired metric (`current / prior − 1`) |
| **MAT AD Level Shift** | after growth − before growth, in pp |
| **Contribution** | share of the Total Market, before and after, plus share of the change |

---

## Client and impact name

Each run can carry a **client name** and an **impact name**. Both are stamped into
every Excel workbook, every PowerPoint deck and the run index, so several
analyses for one client are distinguishable at a glance. The Excel title block
also records the baseline the shares were measured against.

---

## Category mapping (step 4)

A name-equality check is not enough. The two datasets may cut the same space
differently, and one category in the previous dataset may correspond to:

| Case | Dataset 1 | Dataset 2 |
|---|---|---|
| **1:1** | `Coffee` | `Coffee` |
| **composite** | `Biscuits` | `Tandy` / `Biscuits` |
| **1:N** | `Snacks` | `Savoury` / `Chips` + `Savoury` / `Nuts` |
| **N:1** | `Chips` + `Nuts` | `Savoury` |

The unit of analysis is a **canonical category**. Each side maps its own
`(category, subcategory)` tuples onto a canonical name, and the impact analysis
groups by that — so all four cases fall out of one model. The mapping is
confirmed *before* the analysis runs, and the analysis uses it.

**Worked one category at a time.** The engine proposes a match for every
category, but 150-odd rows at once is not how anyone confirms a mapping. The step
opens on a **focused editor**: a picker listing every category with its status,
*Next needing review* / *Previous* buttons, and the selected category's previous
and updated values, its targets, its analysis name and its status. The full table
is still there behind a toggle, and it is the same underlying model — only the
presentation differs.

Editing never resets the view. The picker selection, the analysis name and the
filter all survive a redraw; a name typed into the editor is still there after
the row repaints.

**How candidates are proposed.** Name matching first: identical tuples, then
same category (with A aggregating B's subcategories), then A's name matching a
B *subcategory* (the composite case), then fuzzy. Value comparison is used only
to disambiguate and to detect splits where the names share nothing.

**Value agreement is deliberately loose and never auto-accepts.** The whole
point of an impact study is that values moved, so a tight tolerance would reject
every genuinely-changed category. Value-based matches are always `suggested`,
always report the measured delta as evidence, and always need confirmation.

**Nothing is silently dropped.** Categories with no counterpart are flagged for
manual mapping; Dataset-2 units nobody claimed are reported as new categories in
their own right; an `excluded` row drops its Dataset-1 unit *and* the Dataset-2
units it pointed at. Giving two rows the same analysis name merges them, and the
UI warns when several Dataset-1 categories share one Dataset-2 target.

**Coverage is shown for both sides** — what share of A's value and of B's value
the confirmed mapping accounts for — so a gap is visible before the run.

**The table does not reset while you work in it.** Filter text, the "Show all"
checkbox and scroll position survive an edit, and changing one target only
repaints that row. This is deliberate: the panel used to be rebuilt wholesale on
every change, which threw the view back to "accepted rows only" and made the list
appear to collapse under the cursor mid-edit.

---

## Metric wiring

The metric is selected **independently per dataset**, because the two need not
name the same measure the same way. Pick the metric family on each side; the
updated-dataset metric is auto-matched to the previous one by name similarity,
and each side's prior/current period columns are chosen from that side's own
columns.

An earlier version used a single union-of-names dropdown, which meant that when
a family existed on only one side — or the datasets simply named the metric
differently — one side's period selects went empty, the run sent blank column
names, and the analysis came back empty. That read as "the metric selection
doesn't work". The app now states the wiring status explicitly and the server
**refuses to run** when a side resolves *no* period at all, naming what it could
not find:

```json
{
  "error": "metric_wiring_incomplete",
  "message": "The metric and period selection is incomplete, so the analysis would come back empty.",
  "problems": ["Dataset B (updated): neither MAT YA nor MAT TY could be resolved.", ...]
}
```

Rate metrics (distribution, share, price, index) are detected by name and
averaged with a weight column, selectable per side.

### The two periods are read from the data, not mapped again

The study needs exactly two periods — **MAT YA** (year ago) and **MAT TY** (this
year) — and both are already in the workbook, in one of two shapes:

| | the periods are | what the study reads |
|---|---|---|
| **wide** | columns: `Sales Value YA` is MAT YA, the unqualified `Sales Value` is MAT TY | those two columns |
| **long** | **rows**: one metric column, and a `Periods` column carrying `MAT YA` / `MAT TY` | the one column, restricted to each period's rows |

Nothing asks for them twice, and **`2YA` is never a study period** — it is a
third moving-annual window the study does not use, and treating it as "the year
ago" is how an earlier build read a column two years back and reported the move
as growth.

A workbook that carries only one of the two periods still runs: the period it has
is reported and the missing one is named as unavailable, rather than refusing the
run on the strength of a column the file was never going to have. A rate metric's
weight is restricted to the same rows as its period, so a row-based workbook does
not halve its own distribution levels.

---

## Reading the data

Numbers in a real workbook often arrive as **text** — a column formatted as Text,
a stray thousands separator, a `-` placeholder. The reader handles that:

- Placeholder tokens (`""`, `n/a`, `null`, `-`, `--`, `#n/a`, `#div/0!`, …)
  become **blank, never zero** — a zero would be summed into the analysis.
- Placeholder blanking runs **before** empty rows are dropped, so the padding
  rows at the end of a sheet (a sheet may report 100,000 rows and hold 45,000)
  are removed rather than profiled as data.
- A text column is converted to numeric only if **≥95% of its non-null values
  parse**, decided on the whole column rather than a sample. A column carrying a
  `Total` label therefore stays text instead of having the label silently
  blanked. When values are dropped, the count is reported as a profile warning.

Note that pandas 3 gives text columns dtype `str`, not `object`. Any guard
written as `dtype == object` silently misses every text column — which is
exactly how a workbook whose numbers are text came to be profiled as having no
metrics at all.

---

## What each category report contains

**Channel / market block** — the layout from the reference workbook:

| | BEFORE (MAT YA, MAT TY, Growth) | AFTER (MAT YA, MAT TY, Growth) | Level Shift (Δpp, share before, share after) | Contribution (share before/after, share of change) |

For **Numeric Distribution** the contribution columns are not shown. A
distribution level is not an accumulating quantity, so a share of the category's
change is not a meaningful reading of it; the levels, the absolute change and the
plain share remain, and the values are printed exactly as they arrive (a
distribution is a percentage out of 100 and never exceeds 100).

**Manufacturer / Brand Top-N** — the same shape as the market block, so the two
read alike:

| Entity | BEFORE MAT YA | BEFORE MAT TY | AFTER MAT YA | AFTER MAT TY | Share change | Rank BEFORE | Rank AFTER | Rank change | Movement |
|---|---|---|---|---|---|---|---|---|---|

The levels lead and the rank follows — a rank is the *consequence* of the
movement, not the headline — and there is no growth column, because for a ranked
entity the share change is the comparable movement. The selection is the largest
entities in the **previous** database, followed into the updated one. Movement:

- `NEW` — present only in the updated dataset
- `EXITED` — present only in the previous dataset
- `GAINED` / `LOST` — rank improved / declined
- `HELD` — rank unchanged

**Client entities** — tracked independently of the Top-N cut, so a client brand
ranked #27 is still reported alongside the Top-10.

**Contributors** — the largest absolute gainers and losers driving the change.
Gain and Loss are the two halves of one comparison: both are drawn from entities
whose change could be measured, both are bounded by zero, and both carry the same
two figures (absolute change and contribution to the change). An entity present in
only one dataset has no *change* — it is reported in the Top-N movement as
`NEW` / `EXITED` instead. A half with no entities says so in words rather than
printing an empty table.

A side with no row is treated as **zero**, not as unknown, for an additive metric:
that is the convention the category total already uses, so an entity new in the
updated dataset is a real rise and one that exited is a real fall, and the
contributions reconcile with the change the headline reports. A rate metric has no
zero level, so there a missing side stays unknown.

### Definitions

```
Growth        = current period / prior period - 1
Level shift   = after growth - before growth        (percentage points)
Share         = entity MAT TY / category MAT TY
Contribution  = share of the category total, plus share of the total change
```

---

## Display units (step 5)

Sales Value and Volume can be written in a unit of your choosing — **Auto** (from
the data), Ones, Thousands, Millions or Billions — with 0–4 decimal places. The
choice is resolved **once per metric for the whole run**, from the largest figure
the run carries, and is carried into the report, so the screen, every Excel
workbook and every PowerPoint deck in the run write the same figure the same way.
Scaling each view from its own slice is how one number ends up in millions on
screen and billions in a deck.

A distribution level is a percentage and is never scaled, so Numeric Distribution
has no unit setting.

---

## Dimension mapping engine (step 3)

Matching is layered cheapest-first; each tier records the evidence that produced
it, and the highest tier that fires wins:

| Tier | Method | Example |
|---|---|---|
| 1 | `exact` | `Tandy` → `Tandy` |
| 2 | `normalised` | case / whitespace / punctuation / bracketed tags ignored |
| 3 | `hierarchical` | `TW CVS` → `CVS/TW Total TW Offline (G)/MT w/o Costco` |
| 4 | `core key` | geography prefix and filler words stripped |
| 5 | `squash key` | `Nestle S.A.` → `NESTLE SA` (spacing / initials ignored) |
| 6 | `fuzzy` | blocked, chunked token-set similarity |
| 7 | `value` | no name match, but the metric totals align within 5% |

Fuzzy candidates are corroborated against the metric totals of each entity: close
agreement raises confidence, wild disagreement lowers it.

If the value list for a dimension is capped, the summary says so and the QC
reports the cap — otherwise the leftover entities would look like genuine gaps.

---

## Automated QC

Twelve checks run before the export. Three rules are enforced:

- **A check must be able to fail.** Each has a concrete threshold and emits the
  numbers it compared.
- **A check that cannot run reports `WARN` ("Not verified"), never `PASS`.** A
  green tick that means "did not run" is worse than no tick.
- **A check that cannot fail is not a check.** A run also records **notes** —
  what it did to the data (how much of it is inside the mapped categories, a
  period column corrected, a period the workbook cannot supply). They appear in
  the QC panel under **Run notes** and in the run index, and they are
  deliberately **not counted** among the checks: a run that verified everything
  should not report "2 warnings" merely because it also told you what it covered.
  They are also not shown above the headline figures — a titled warning box over
  the KPIs reads as "this analysis is suspect" even when every check passed.

QC is shown in the app's step 6 and is **not written into the deliverables**: the
QC sheet was removed from the Excel export and the QC slide from the deck on
request. The checks still run — they gate the run and are what the panel reports —
they are simply not shipped to the client. The run's scope notes survive in the
run index, because a report covering a subset of the rows has to say so itself.

| Check | What it proves |
|---|---|
| Metric column wiring | All four metric references resolve and are numeric |
| Missing values | Null rate on analysis keys and metrics |
| Duplicate records | Repeated dimension keys that would double-count |
| Dataset mapping | Automatic match rate per dimension, with cap disclosure |
| Invalid mappings | Many-to-one collisions that silently merge entities |
| Category-level totals | Reconciles against an **independent** recomputation from the source rows |
| Percentage calculations | Growth rates, and shares summing to 100 |
| Top-N calculations | Correctly sized and correctly ordered |
| Before/after consistency | Level shift = after growth − before growth |
| Metric calculations | Absolute change = after − before |
| Entered / exited entities | Single-dataset entities are labelled, not dropped |
| Export completeness | Every selected category produced both files |

The totals check is deliberately a second derivation (a boolean-mask aggregation
straight off the source frames, on a fresh read with a different engine), not a
re-run of the pipeline's own groupby — replaying the same parser cannot reveal
that the parser was wrong.

**The totals check is rate-aware.** A rate metric such as `ND Dist` is a
percentage, so the pipeline averages it (weighted by value). The check
recomputes it the same way — a weighted mean, or a plain mean when no weight
column is wired — and where it genuinely cannot verify (a zero total weight)
it reports *Not verified* rather than passing or failing. It previously
recomputed with a plain sum, which meant every run on `ND Dist` reported a
failure on data that was entirely correct.

**A single-dataset category is not an error.** With 151 categories in the
previous dataset and 153 in the updated one, the two extra categories have no
before-values by definition. They are analysed and the totals check flags the
missing side — which is the system telling you about a genuinely new category,
not a broken run.

---

## Architecture

```
impact_studio/
├── run.py                     launcher
├── requirements.txt
├── backend/
│   ├── ingest.py              xlsx/csv reading (calamine, openpyxl fallback)
│   ├── profiling.py           probing, column-role classification, metric families
│   ├── mapping.py             layered dimension matching (market/mfr/brand)
│   ├── category_mapping.py    category mapping: 1:1, composite, 1:N, N:1
│   ├── analysis.py            before/after, level shift, contribution, Top-N
│   ├── qc.py                  the eleven checks
│   ├── export_excel.py        per-category workbook (xlsxwriter)
│   ├── export_pptx.py         per-category deck (python-pptx)
│   └── main.py                FastAPI service
├── frontend/                  vanilla-JS single-page app (no build step)
├── templates/
│   └── impact_template.pptx   ← put your standard deck here (see templates/README.md)
├── tools/
│   ├── verify_e2e.py          pipeline + mapping + category mapping + QC mutation tests
│   ├── verify_api.py          live-server API integration test
│   └── ui_probe/              headless-Chrome UI driver
└── outputs/                   generated deliverables
```

---

## What the exports contain

### Excel — `<Category>_Impact.xlsx`

One flat table per sheet: a single header row, one record per row, no merged
cells, so the tables can be filtered and pivoted.

| sheet | content |
| --- | --- |
| `Channel (Sales Value)` | the market rows, **one table per level** — channels, then regions — in one sheet |
| `Manufacturer Top-N (Sales Value)` | the Top-N manufacturers |
| `Brand Top-N (Sales Value)` | the Top-N brands |
| `Contributors (Sales Value)` | the gainers and losers |
| … | one set per selected metric — Value, Volume, ND |
| `Client Brands` | the client entities tracked, on the headline metric |

Sheet names carry the metric because a run can measure several: a single Top-N
table would silently answer only the first one.

The market sheet carries the same level split the analysis shows. The Total Market
leads each table and is **not** repeated as a "sum of rows above" beneath itself;
that row appears only in a table that has no total at all, where the sum is the
only aggregate there is.

There is **no summary sheet and no QC sheet**. The figures a summary carried are
in the tables beneath it, and the validation belongs to the app, not to the
workbook the client receives.

### PowerPoint — `<Category>_Impact.pptx`

Built on **your own template** — drop it at `templates/impact_template.pptx`
(see `templates/README.md`). Its master, theme, fonts and background graphics are
used; its own slides are removed, and every layout placeholder is stripped from
each generated slide, so no stale cover and no empty "Click to add text" box
reaches the client. With no template present the export still runs, on a plain
16:9 deck.

Per category and **per metric**: an impact headline, a market table for the
channels and another for the regions, Top-N manufacturers, Top-N brands, and
"what drove the change". Then the client entities once. Every table has black
borders, a compact layout, and negative figures in red.

There is **no cover slide, no chart and no QC slide** — all three were removed on
request. Add charts by hand where you want them.

### Run index — `00_Index.xlsx`

A list of every file the run produced, plus the run's scope notes. No category
summary and no QC.

---

**Per-side wiring.** Every dimension and metric column is configured separately
for dataset A and dataset B, falling back to A's name when B is unset. The two
datasets need not use the same column names for the same concept — for either
dimensions or metrics.

**Metric families.** Numeric columns are grouped by base name and period
qualifier, so `Sales Value 2YA` / `Sales Value YA` / `Sales Value` are recognised
as one family with three variants. The same logical metric is then wired across
both datasets. Rate metrics (distribution, share, price, index) are detected by
name and aggregated as weighted averages rather than sums.

**Scale.** The whole dataset is aggregated once per dataset; every category report
is a slice of that aggregation, so a 150-category bulk run does not re-scan the
raw rows 150 times. On the reference workbook (96,135 rows) a 12-category run
with both exports takes ~15 s; all 151 categories take ~3 min.

**Session memory is bounded.** Opening a workbook registers a `Source`, and a
`Source` caches the sheets it has read. Re-opening the same file reuses the
existing registration (identified by absolute path, size and mtime), and the
registry is capped at six sources with least-recently-used eviction, each keeping
at most three cached frames. `GET /api/memory` reports what the session holds.

This matters because an unbounded registry grew the process ~24 MB per re-open —
a real workbook is ~19 MB of frames — until the OS killed it mid-request and the
browser showed only an opaque 500. The cap makes growth visible instead of fatal.

---

## Verification

```bash
python tools/verify_e2e.py                       # 86 checks, no server needed
python run.py --no-browser &                     # then:
python tools/verify_api.py --categories 12       # 40 checks against the live API
python tools/make_demo_outputs.py                # regenerate outputs/ via the API
```

`verify_e2e.py` covers five things:

1. **Real-data pipeline** against the supplied workbook — profiling, mapping,
   analysis, QC and export, with the key totals re-derived by a *different route*
   (second engine + mask aggregation) and compared to 1e-9.
2. **Dimension mapping edge cases** from the brief — a hierarchical market path,
   a renamed company, and an unmatchable value that must be flagged rather than
   guessed.
3. **Mutation tests** — each QC check is fed an input it *must* reject, plus the
   unwired case where it must report `NOT VERIFIED` instead of `PASS`.
4. **Category mapping** — 1:1, composite, 1:N and N:1, each followed through to
   the analysis to confirm the reported figure reconciles; plus differently-named
   metric *and* dimension columns wiring up on both sides.
5. **Ingestion** — numbers arriving as text: coercion, thousands separators,
   placeholders becoming blank rather than zero, padding rows dropped, and a
   stray label left as text rather than silently destroyed.

`verify_api.py` adds, beyond the functional endpoints:

6. **Session memory** — re-opening the same workbook reuses one registration,
   the registry stays within its cap, and cached bytes do not grow across
   re-opens.
7. **Full-breadth bulk export** — all 151 categories in one request: 303 files,
   one folder per category, every file present on disk.

The UI driver (`tools/ui_probe/drive.mjs`) walks all seven steps in headless
Chrome and asserts on rendered content, overlay visibility and pointer
reachability — not merely on the absence of console errors. Run it against an
**idle** server; it is not isolated from concurrent load.

---

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/source` | upload a workbook |
| POST | `/api/source/from-path` | register one already on disk (`reused: true` if the session already holds that file) |
| GET | `/api/sources` | list registered sources |
| GET | `/api/source/{id}/columns` | column names and a preview |
| GET | `/api/source/{id}/values` | distinct values for a column |
| POST | `/api/profile` | probe both datasets |
| POST | `/api/mapping` | dimension mapping (market / manufacturer / brand) |
| POST | `/api/market-mapping` | classify each market value as Total / Region / Channel, and find the hierarchy |
| POST | `/api/category-mapping` | category mapping |
| POST | `/api/run` | analysis |
| POST | `/api/export` | bulk Excel + PowerPoint export |
| GET | `/api/files` | everything under `outputs/` |
| GET | `/api/download` | download one artefact |
| GET | `/api/memory` | what the session is currently holding |
| GET | `/api/health` | liveness and optional dependencies |

---

## Dependencies

All permissively licensed.

| Package | Licence | Used for |
|---|---|---|
| fastapi, pydantic, python-multipart | MIT / Apache-2.0 | HTTP service |
| uvicorn | BSD-3-Clause | ASGI server |
| pandas, numpy | BSD-3-Clause | data handling |
| python-calamine | MIT | fast xlsx reading |
| openpyxl | MIT | xlsx fallback reader |
| xlsxwriter | BSD-2-Clause | Excel writing |
| python-pptx | MIT | PowerPoint writing |
| rapidfuzz | MIT | fuzzy dimension matching |

No proprietary or copyleft components.
