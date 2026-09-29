/* Impact Studio — front-end controller.
   Vanilla JS, no build step. Talks to the FastAPI backend under /api. */

'use strict';

// ---------------------------------------------------------------------------
// state
// ---------------------------------------------------------------------------
const S = {
  sources: [],
  step: 1,
  profile: null,
  dimCols: { a: {}, b: {} },        // role -> column, user-editable
  // The market pairing the user authors in step 3: one entry per pairing, each
  // with the A value, the B value and the level that pairing belongs to. Nothing
  // is filled in for them - the step shows the two value lists and they build
  // each row. `marketAdvisory` holds the hierarchy we read, which is used only
  // to flag a pairing that looks inconsistent, never to set the level.
  marketAdvisory: null,
  marketPairs: [],
  marketPathsUsed: false,
  marketPathSource: '',
  marketLevel: 'total',
  catmap: null,                     // enumerated units + one-sided report
  // The mappings the user has authored, in order. This list IS the step-4
  // content: a mapping exists because the user clicked "+ Mapping", filled it in
  // and pressed Done. Nothing else contributes a row.
  catmapMaps: [],
  // The mapping currently being edited, or null. Drafted as a copy so Cancel
  // discards rather than reverting an aliased object.
  catmapDraft: null,
  catmapTouched: false,
  catmapIncludeNewInB: false,
  clientName: '',
  impactName: '',
  metricFamilies: {},
  // Which impact metrics to measure, and their per-side wiring. Any of the three
  // can be selected; each is resolved independently on the two datasets.
  metricSel: { sales_value: true, volume: false, nd: false },
  metricWiring: {},
  selection: { markets: [], categories: [], top_n: 10, clients: [] },
  result: null,
  exportResult: null,
};

// A top-level `const` does not become a property of `window` (unlike `var` or a
// function declaration), so the headless probes could reach a function but not
// the state it reads. Expose it explicitly.
window.S = S;

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body && !(opts.body instanceof FormData)
      ? { 'Content-Type': 'application/json' } : {},
    ...opts,
  });
  const txt = await res.text();
  let data;
  try { data = JSON.parse(txt); } catch { data = { detail: txt }; }
  if (!res.ok) {
    const d = data.detail;
    if (d && typeof d === 'object') {
      // Structured errors from the pre-flight checks carry a list of problems.
      throw new Error((d.message || d.error || 'Request failed')
        + (Array.isArray(d.problems) && d.problems.length
          ? '\n\n· ' + d.problems.join('\n· ') : ''));
    }
    throw new Error(d || res.statusText);
  }
  return data;
}

function overlay(on, msg = 'Working…') {
  $('#overlay').hidden = !on;
  if (msg) $('#overlay-msg').textContent = msg;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtNum(v, dp = 0) {
  if (v === null || v === undefined || !isFinite(v)) return '–';
  return Number(v).toLocaleString('en-US', {
    minimumFractionDigits: dp, maximumFractionDigits: dp,
  });
}

function scaleOf(...vals) {
  let mx = 0;
  vals.forEach(v => { if (v !== null && v !== undefined && isFinite(v)) mx = Math.max(mx, Math.abs(v)); });
  if (mx >= 1e9) return [1e9, 'Bn'];
  if (mx >= 1e6) return [1e6, 'M'];
  if (mx >= 1e3) return [1e3, 'K'];
  return [1, ''];
}

function fmtVal(v, scale = 1, unit = '') {
  if (v === null || v === undefined || !isFinite(v)) return '–';
  const x = v / scale;
  const dp = unit ? 2 : 0;
  return fmtNum(x, dp) + unit;
}

function cls(v) {
  if (v === null || v === undefined || !isFinite(v) || Math.abs(v) < 0.05) return 'zero';
  return v > 0 ? 'pos' : 'neg';
}

function pct(v, dp = 1) {
  if (v === null || v === undefined || !isFinite(v)) return '–';
  return (v >= 0 ? '+' : '') + Number(v).toFixed(dp) + '%';
}

function pp(v, dp = 1) {
  if (v === null || v === undefined || !isFinite(v)) return '–';
  return (v >= 0 ? '+' : '') + Number(v).toFixed(dp) + 'pp';
}

// ---------------------------------------------------------------------------
// navigation
// ---------------------------------------------------------------------------
function go(step) {
  S.step = step;
  $$('.panel').forEach(p => { p.hidden = Number(p.dataset.panel) !== step; });
  $$('.step').forEach(b => {
    const n = Number(b.dataset.step);
    b.classList.toggle('active', n === step);
    b.classList.toggle('done', n < step);
  });
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

document.addEventListener('click', e => {
  const g = e.target.closest('[data-goto]');
  if (g) go(Number(g.dataset.goto));
  const st = e.target.closest('.step');
  if (st) {
    const n = Number(st.dataset.step);
    if (n <= S.step || canGo(n)) go(n);
  }
});

function canGo(n) {
  if (n >= 2 && !S.profile) return false;
  if (n >= 3 && !S.profile) return false;
  // Step 3 is the market pairing; it is readable as soon as both datasets are
  // profiled, because the value lists come from the profile rather than from a
  // suggestion pass. Step 4 needs the categories to have been enumerated.
  if (n >= 4 && !S.marketAdvisory) return false;
  if (n >= 5 && !S.catmap) return false;
  if (n >= 6 && !S.result) return false;
  if (n >= 7 && !S.result) return false;
  return true;
}

// ---------------------------------------------------------------------------
// sources
// ---------------------------------------------------------------------------
async function loadSources() {
  S.sources = await api('/api/sources');
  for (const side of ['a', 'b']) {
    const sel = $(`#${side}-source`);
    const cur = sel.value;
    sel.innerHTML = '<option value="">— select —</option>' +
      S.sources.map(s => `<option value="${s.id}">${esc(s.filename)}</option>`).join('');
    if (cur && S.sources.some(s => s.id === cur)) sel.value = cur;
  }
}

async function onSourceChange(side) {
  const sid = $(`#${side}-source`).value;
  const sheetSel = $(`#${side}-sheet`);
  const splitSel = $(`#${side}-splitcol`);
  const valWrap = $(`#${side}-value-wrap`);
  if (!sid) {
    sheetSel.innerHTML = '<option value="">—</option>';
    splitSel.innerHTML = '<option value="">— none —</option>';
    valWrap.style.display = 'none';
    return;
  }
  const src = S.sources.find(s => s.id === sid);
  sheetSel.innerHTML = (src.sheets.length
    ? src.sheets.map(s => `<option>${esc(s)}</option>`).join('')
    : '<option value="">(single table)</option>');
  await refreshColumns(side);
}

// Per-side request token for the column read. Choosing a source fires a read for
// the panel's *default* sheet, and choosing a sheet fires a second one; the two
// can resolve out of order, and the older response then repaints `#side-splitcol`
// with the wrong sheet's columns (measured: `Formula`'s 4 options landing after
// `Raw_MAT`'s 16, leaving the split column unsettable). Bumping the token on
// every call and discarding any response whose token is no longer current makes
// the newest request the only one that can write.
if (!S.colToken) S.colToken = {};

async function refreshColumns(side) {
  const sid = $(`#${side}-source`).value;
  if (!sid) return;
  const sheet = $(`#${side}-sheet`).value || null;
  const hr = Number($(`#${side}-header`).value) || 1;
  const token = (S.colToken[side] || 0) + 1;
  S.colToken[side] = token;
  overlay(true, 'Reading columns…');
  try {
    const q = new URLSearchParams();
    if (sheet) q.set('sheet', sheet);
    q.set('header_row', hr);
    const d = await api(`/api/source/${sid}/columns?${q}`);
    // A later request has already been issued - this answer is stale, so drop it
    // rather than overwriting the panel with a sheet the user has moved off.
    if (S.colToken[side] !== token) return;
    const splitSel = $(`#${side}-splitcol`);
    const cur = splitSel.value;
    splitSel.innerHTML = '<option value="">— none —</option>' +
      d.columns.map(c => `<option>${esc(c)}</option>`).join('');
    // Clear the value when the new sheet does not have that column, rather than
    // leaving it selected. An <option> that is gone while the <select> still
    // *reports* it as its value is stale state: `spec()` would send a column
    // the sheet does not contain, and the next change event would fire a
    // request that can only 400. Reset and say why.
    if (cur && d.columns.includes(cur)) {
      splitSel.value = cur;
    } else if (cur) {
      splitSel.value = '';
      splitSel.dispatchEvent(new Event('change', { bubbles: true }));
    }
    // Written after the block above, which would otherwise overwrite them.
    const summary = [];
    if (cur && !d.columns.includes(cur)) {
      summary.push(`<i>Column '${esc(cur)}' is not in this sheet — the split `
                 + 'column was cleared.</i>');
    }
    summary.push(
      `<b>Rows:</b> ${fmtNum(d.rows)}\n<b>Columns:</b> ${d.columns.length}\n` +
      `<b>First cols:</b> ${d.columns.slice(0, 6).map(esc).join(', ')}`);
    $(`#${side}-summary`).innerHTML = summary.join('\n');
  } catch (e) {
    if (S.colToken[side] !== token) return;
    $(`#${side}-summary`).textContent = 'Error: ' + e.message;
  } finally {
    // A superseded call must not lift the overlay while the current read is
    // still in flight, or the panel looks finished while it is still loading.
    if (S.colToken[side] === token) overlay(false);
  }
}

async function onSplitChange(side) {
  const sid = $(`#${side}-source`).value;
  const col = $(`#${side}-splitcol`).value;
  const wrap = $(`#${side}-value-wrap`);
  if (!sid || !col) { wrap.style.display = 'none'; return; }
  wrap.style.display = '';
  const sheet = $(`#${side}-sheet`).value || null;
  const hr = Number($(`#${side}-header`).value) || 1;
  const q = new URLSearchParams({ column: col, header_row: hr });
  if (sheet) q.set('sheet', sheet);
  try {
    const d = await api(`/api/source/${sid}/values?${q}`);
    $(`#${side}-value`).innerHTML = d.values.map(v => `<option>${esc(v)}</option>`).join('');
  } catch (e) {
    // The split column can name a column the currently-selected sheet does not
    // have. `refreshColumns` keeps the previous value in place when the new
    // sheet lacks it, so changing sheets fires this handler with a column that
    // is not there, and the request 400s. Fall back to "no split" rather than
    // letting the rejection escape as an uncaught error: the picker stays
    // visible and honest about which column it needs.
    wrap.style.display = 'none';
    $(`#${side}-value`).innerHTML = '';
    $(`#${side}-summary`).textContent =
      `Split column '${col}' is not in sheet '${sheet || '(default)'}' — `
      + 'pick a column that exists here.';
  }
}

function spec(side) {
  const source_id = $(`#${side}-source`).value;
  if (!source_id) return null;
  const split_column = $(`#${side}-splitcol`).value || null;
  return {
    source_id,
    sheet: $(`#${side}-sheet`).value || null,
    header_row: Number($(`#${side}-header`).value) || 1,
    split_column,
    value: split_column ? ($(`#${side}-value`).value || null) : null,
  };
}

// ---------------------------------------------------------------------------
// step 2 — profile
// ---------------------------------------------------------------------------
async function doProfile() {
  const a = spec('a'), b = spec('b');
  if (!a || !b) { alert('Select a source for both dataset A and dataset B.'); return; }
  overlay(true, 'Probing both datasets — this reads the full file, please wait…');
  try {
    const d = await api('/api/profile', {
      method: 'POST',
      body: JSON.stringify({ a, b }),
    });
    S.profile = d;
    S.dimCols = {
      a: { ...d.a.dimensions },
      b: { ...d.b.dimensions },
    };
    renderProfile();
    go(2);
  } catch (e) {
    alert('Profile failed:\n' + e.message);
  } finally { overlay(false); }
}

const ROLE_LABEL = {
  category: 'Category', subcategory: 'Subcategory', market: 'Market / Channel',
  manufacturer: 'Manufacturer', brand: 'Brand', product: 'Product',
  period: 'Period', dataset: 'Dataset flag',
};
const EDITABLE_ROLES = ['category', 'subcategory', 'market', 'manufacturer', 'brand', 'product', 'period'];

function renderProfile() {
  const d = S.profile;
  $('#profile-out').innerHTML = ['a', 'b'].map(side => {
    const p = d[side];
    const cols = p.column_profiles;
    const dims = S.dimCols[side];
    return `
    <div class="card">
      <div class="card-head">
        <span class="tag ${side === 'a' ? 'green' : 'blue'}">${side.toUpperCase()}</span>
        <h3>${esc(p.label)}</h3>
      </div>
      <div class="stat-grid">
        <div class="stat"><div class="s-label">Rows</div><div class="s-value">${fmtNum(p.rows)}</div></div>
        <div class="stat"><div class="s-label">Columns</div><div class="s-value">${p.columns}</div></div>
        <div class="stat"><div class="s-label">Metrics</div><div class="s-value">${p.metrics.length}</div></div>
        <div class="stat"><div class="s-label">Families</div><div class="s-value">${Object.keys(p.metric_families).length}</div></div>
      </div>
      ${p.warnings.length ? `<div class="notice warn">${p.warnings.map(esc).join('<br>')}</div>` : ''}

      <h4 style="font-size:12px;margin:14px 0 8px;color:var(--ink2)">Detected dimensions — correct if wrong</h4>
      <div class="grid two tight">
        ${EDITABLE_ROLES.map(role => `
          <div class="field">
            <label>${ROLE_LABEL[role] || role}</label>
            <select data-dim-role="${role}" data-side="${side}">
              <option value="">— none —</option>
              ${cols.map(c => `<option value="${esc(c.name)}" ${dims[role] === c.name ? 'selected' : ''}>${esc(c.name)}</option>`).join('')}
            </select>
          </div>`).join('')}
      </div>

      <details style="margin-top:12px">
        <summary>All ${cols.length} columns</summary>
        <div class="tbl-wrap" style="margin-top:8px">
          <table>
            <thead><tr><th>Column</th><th>Role</th><th>Type</th><th>Unique</th><th>Null %</th><th>Sample</th></tr></thead>
            <tbody>
              ${cols.map(c => `<tr>
                <td>${esc(c.name)}</td>
                <td style="text-align:left;color:var(--muted)">${esc(c.role)}${c.is_rate ? ' · rate' : ''}</td>
                <td style="text-align:left;color:var(--muted)">${esc(c.dtype)}</td>
                <td class="num">${fmtNum(c.n_unique)}</td>
                <td class="num ${c.null_pct > 5 ? 'neg' : ''}">${c.null_pct}%</td>
                <td style="text-align:left;color:var(--muted);max-width:280px;overflow:hidden;text-overflow:ellipsis">${esc((c.samples || []).join(' · '))}</td>
              </tr>`).join('')}
            </tbody>
          </table>
        </div>
      </details>

      <details style="margin-top:8px">
        <summary>Metric families (${Object.keys(p.metric_families).length})</summary>
        <div class="tbl-wrap" style="margin-top:8px">
          <table>
            <thead><tr><th>Family</th><th>Variants</th></tr></thead>
            <tbody>
              ${Object.entries(p.metric_families).map(([fam, vs]) => `<tr>
                <td>${esc(fam)}</td>
                <td style="text-align:left;color:var(--muted)">${esc(Object.entries(vs).map(([k, v]) => `${k}=${v}`).join('  ·  '))}</td>
              </tr>`).join('')}
            </tbody>
          </table>
        </div>
      </details>
    </div>`;
  }).join('');
}

document.addEventListener('change', e => {
  const sel = e.target.closest('[data-dim-role]');
  if (sel) {
    const { dimRole, side } = sel.dataset;
    if (sel.value) S.dimCols[side][dimRole] = sel.value;
    else delete S.dimCols[side][dimRole];
  }
  // Which metrics to measure: ticking/unticking a metric adds or removes its
  // wiring block, so the wiring panel is repainted.
  const mpick = e.target.closest('[data-metric]');
  if (mpick) {
    S.metricSel[mpick.dataset.metric] = mpick.checked;
    renderMetricPicks();
    renderMetricPeriods();
    validateMetricWiring();
    return;
  }
  // Period selects live inside the metric wiring blocks; any change re-validates
  // the whole wiring (a period may be shared or per-side).
  if (e.target.closest('#c-metric-wiring')) {
    validateMetricWiring();
  }
  // "Is rate" is a property of the chosen metric family, not something the user
  // toggles: ND is a rate, Sales Value and Volume are additive. No handler.
  if (e.target.id === 'c-topn') {
    $('#c-topn-custom-wrap').style.display =
      $('#c-topn').value === 'custom' ? '' : 'none';
  }
});

// ---------------------------------------------------------------------------
// step 3 — market pairing
//
// The brief narrowed this step to four things: take a market value from
// Dataset A, give the pairing a level, choose the corresponding value from
// Dataset B, and use that level to scope the analysis. Region and Channel are
// the vocabulary of the level, not separate things to discover.
//
// Nothing is paired for the user. The step lists A's values and B's values and
// they add a row per pairing. The hierarchy we read is shown beside each row as
// advice - a value whose path sits under another is probably not a Total - but
// it never sets the level.
//
// Manufacturer and brand tabs are gone. Mapping those is not part of the brief,
// and the tabs carried a suggestion engine whose "confidence" was read as
// evidence that a pairing was correct. The manufacturer and brand analyses still
// run; they just use the datasets' own names on both sides.
// ---------------------------------------------------------------------------

const MKT_LEVELS = [
  ['total', 'Total Market'],
  ['channel', 'Channel'],
  ['region', 'Region'],
];

/**
 * Read (or re-read) the market values and annotate the authored pairings.
 *
 * `adviceOnly` is the difference between the two callers, and it is the whole
 * reason this step used to feel like it re-rendered on every click:
 *
 *   * A full call (`adviceOnly` false) is the one that *reads the values*. It
 *     runs when the step is entered, when the wiring changes, and from the
 *     explicit "Re-read the values" button. It rebuilds the panel.
 *   * An advice-only call (`adviceOnly` true) is what a pairing change needs:
 *     the value lists have not changed, so there is nothing to re-read - the
 *     server is asked only to annotate the new pairing, and the panel is *not*
 *     rebuilt. The row the user is editing keeps its focus and its open
 *     dropdown, and no other row is touched.
 *
 * Re-rendering the whole panel for a pairing change was rebuilding the very
 * `<select>` the user was interacting with on every keystroke-level choice,
 * which is why it read as "renders unnecessarily every time I map".
 */
async function doMarketMapping(adviceOnly = false) {
  const a = spec('a'), b = spec('b');
  if (!a || !b) return;
  const mcol = S.dimCols.a.market || '';
  if (!mcol) {
    S.marketAdvisory = null;
    renderMarketPairing();
    return;
  }
  if (adviceOnly && S.marketAdvisory) {
    // Paint the just-edited row immediately from local state - the value is
    // already in S.marketPairs - then let the server's advice catch up without
    // repainting anything. If the call fails, the row still shows what the user
    // chose; only the advice stays blank for that row.
    patchMarketRowAdvice();
  } else {
    overlay(true, 'Reading the market values on both sides…');
  }
  try {
    const d = await api('/api/market-mapping', {
      method: 'POST',
      body: JSON.stringify({
        a, b,
        market_col_a: mcol,
        market_col_b: S.dimCols.b.market || mcol,
        metric_col_a: pickMetricCol('a'),
        metric_col_b: pickMetricCol('b'),
        // Send back whatever the user has authored so the server can annotate
        // each pairing with evidence. It does not add any pairing of its own.
        pairs: S.marketPairs || [],
      }),
    });
    S.marketAdvisory = d;
    S.marketPathsUsed = !!d.paths_used;
    S.marketPathSource = d.path_source || '';
    if (adviceOnly) patchMarketRowAdvice();
    else renderMarketPairing();
  } catch (e) {
    if (adviceOnly) return;
    const box = $('#map-out');
    if (box) box.innerHTML = `<div class="card"><div class="notice warn">
      The market values could not be read: ${esc(e.message)}</div></div>`;
  } finally { if (!adviceOnly) overlay(false); }
}

/** Add an empty pairing; the user fills in both sides. */
function addMarketPair() {
  if (!S.marketPairs) S.marketPairs = [];
  S.marketPairs.push({ market_a: '', market_b: '', level: S.marketLevel || 'total' });
  renderMarketPairing();
}

/**
 * Refresh only the advice cell of each pairing row, in place.
 *
 * The pairing table is otherwise untouched: the selects keep their nodes (so
 * an open dropdown is not closed and focus is not lost) and no value list is
 * re-read. This is what makes a pairing change a local edit rather than a
 * full panel rebuild.
 */
function patchMarketRowAdvice() {
  const pairs = S.marketPairs || [];
  $$('#map-out tr[data-mkt-row]').forEach(tr => {
    const i = Number(tr.dataset.mktRow);
    const cell = tr.querySelector('td[data-mkt-advice]');
    if (!cell) return;
    cell.innerHTML = marketAdviceHtml(pairs[i]);
  });
}

/** The advice text for one pairing, from the server's evidence on it. */
function marketAdviceHtml(p) {
  const ev = (p && p.evidence) || {};
  if (ev.contradiction) return `<span class="neg">${esc(ev.contradiction)}</span>`;
  if (ev.note) return esc(ev.note);
  if (ev.child_of) {
    return `sits under ${esc(Object.values(ev.child_of).flat().slice(0, 2).join(', '))}`;
  }
  return '';
}

function renderMarketPairing() {
  const box = $('#map-out');
  if (!box) return;
  const mm = S.marketAdvisory;
  const pairs = S.marketPairs || [];

  if (!mm) {
    box.innerHTML = `<div class="card">
      <div class="card-head"><h3>Market pairing</h3></div>
      <p class="hint">A market column has to be wired on both datasets before its
        values can be listed. Set it on the Profile step (step 2).</p>
      <button class="btn small" id="mkt-load">Read the market values</button>
    </div>`;
    const b = $('#mkt-load');
    if (b) b.onclick = doMarketMapping;
    return;
  }

  const aVals = mm.a_values || [];
  const bVals = mm.b_values || [];
  const va = mm.values_a || {}, vb = mm.values_b || {};
  const s = mm.summary || {};
  const opts = (list, cur) => list.map(v =>
    `<option value="${esc(v)}" ${cur === v ? 'selected' : ''}>${esc(v)}</option>`).join('');

  // Which A values and which B values are already spoken for, so the counts are
  // honest about what is still unpaired rather than implying full coverage.
  const pairedA = new Set(pairs.map(p => p.market_a).filter(Boolean));
  const pairedB = new Set(pairs.map(p => p.market_b).filter(Boolean));
  const unpairedA = aVals.filter(v => !pairedA.has(v));
  const unpairedB = bVals.filter(v => !pairedB.has(v));

  box.innerHTML = `
    <div class="card">
      <div class="card-head"><h3>What the pairing does</h3></div>
      <p class="hint" style="margin:0">
        Each row pairs one market value from the previous dataset with the
        corresponding value in the updated one, and says which level that pair
        belongs to. The level you choose is what scopes the analysis: pick
        <em>Total Market</em> and the analysis covers the values you paired as
        totals. A market in one dataset need not be named the same in the other —
        that is what the two sides are for.
      </p>
      <div class="stat-grid" style="margin-top:10px">
        <div class="stat"><div class="s-label">A values</div>
          <div class="s-value">${fmtNum(aVals.length)}</div></div>
        <div class="stat"><div class="s-label">B values</div>
          <div class="s-value">${fmtNum(bVals.length)}</div></div>
        <div class="stat"><div class="s-label">Paired</div>
          <div class="s-value">${fmtNum(pairs.filter(p => p.market_a).length)}</div></div>
        <div class="stat"><div class="s-label">A not paired</div>
          <div class="s-value ${unpairedA.length ? 'neg' : ''}">${fmtNum(unpairedA.length)}</div></div>
        <div class="stat"><div class="s-label">B not paired</div>
          <div class="s-value ${unpairedB.length ? 'neg' : ''}">${fmtNum(unpairedB.length)}</div></div>
        <div class="stat"><div class="s-label">Need a look</div>
          <div class="s-value ${s.needs_review ? 'neg' : ''}">${fmtNum(s.needs_review || 0)}</div></div>
      </div>
      ${s.parts_are_subset ? `<div class="notice info">
        The values you paired as regions/channels sum to
        <strong>${fmtNum(s.parts_of_total_pct, 1)}%</strong> of the totals you paired —
        they are a subset, which is normal. Shares are measured against the Total,
        never against the sum of the parts.
      </div>` : ''}
      ${!pairs.some(p => p.level === 'total')
        ? `<div class="notice warn">
             No pairing is marked <em>Total Market</em> yet. Without one, shares
             are measured against whatever is in scope instead of against a total,
             and the QC will say so rather than implying a total was used.
           </div>` : ''}
      <div class="row" style="margin-top:10px">
        <button class="btn small primary" id="mkt-add">+ Add a pairing</button>
        <button class="btn small" id="mkt-reload">Re-read the values</button>
        <span class="spacer"></span>
        <div class="field" style="margin:0;min-width:230px">
          <label>Level the analysis covers</label>
          <select id="mkt-level">
            ${[['total', 'Total Market'], ['channel', 'Channels'],
               ['region', 'Regions'], ['all', 'Every paired value']]
              .map(([v, lab]) => `<option value="${v}" ${S.marketLevel === v ? 'selected' : ''}>${esc(lab)}</option>`).join('')}
          </select>
        </div>
      </div>
      <p class="hint" style="margin-top:6px">
        ${S.marketPathsUsed
          ? `Hierarchy read from ${esc(S.marketPathSource || 'the workbook')}, shown as advice only.`
          : 'No hierarchy column was found, so levels come from your choices alone.'}
      </p>
    </div>

    <div class="card">
      <div class="card-head"><h3>Pairings (${pairs.length})</h3></div>
      ${pairs.length ? `<div class="tbl-wrap" style="max-height:520px;overflow:auto">
        <table id="mkt-tbl"><thead><tr>
          <th style="width:30%">Market value in A (previous)</th>
          <th style="width:30%">Market value in B (updated)</th>
          <th style="width:16%">Level</th>
          <th>Advice</th><th></th>
        </tr></thead><tbody>
        ${pairs.map((p, i) => {
          return `<tr data-mkt-row="${i}">
            <td><select data-mkt-a="${i}">
              <option value="">— choose an A value —</option>
              ${opts(aVals, p.market_a)}
              ${p.market_a && !aVals.includes(p.market_a)
                ? `<option selected>${esc(p.market_a)}</option>` : ''}
            </select></td>
            <td><select data-mkt-b="${i}">
              <option value="">— choose a B value —</option>
              ${opts(bVals, p.market_b)}
              ${p.market_b && !bVals.includes(p.market_b)
                ? `<option selected>${esc(p.market_b)}</option>` : ''}
            </select></td>
            <td><select data-mkt-level="${i}">
              ${MKT_LEVELS.map(([v, lab]) =>
                `<option value="${v}" ${p.level === v ? 'selected' : ''}>${esc(lab)}</option>`).join('')}
            </select></td>
            <td class="hint" style="text-align:left;font-size:11px" data-mkt-advice>${marketAdviceHtml(p)}</td>
            <td><button class="btn small ghost" data-mkt-del="${i}">×</button></td>
          </tr>`;
        }).join('')}
        </tbody></table>
      </div>` : `<div class="notice info" style="margin:0">
        No pairings yet. Press <strong>Add a pairing</strong> to pair a market value
        from each dataset. Nothing is paired automatically — a market that happens
        to have the same name in both datasets is still your call.
      </div>`}
      ${(unpairedA.length && pairs.length) ? `<p class="hint" style="margin-top:8px">
        Still unpaired in A (${unpairedA.length}): ${esc(unpairedA.slice(0, 12).join(', '))}
        ${unpairedA.length > 12 ? '…' : ''}. Values you leave unpaired are not part of the analysis.
      </p>` : ''}
    </div>`;

  const add = $('#mkt-add');
  if (add) add.onclick = addMarketPair;
  const rl = $('#mkt-reload');
  if (rl) rl.onclick = doMarketMapping;
  const lvl = $('#mkt-level');
  if (lvl) lvl.onchange = () => { S.marketLevel = lvl.value; };

  $$('#map-out select[data-mkt-a]').forEach(sel => {
    sel.onchange = () => {
      const i = Number(sel.dataset.mktA);
      S.marketPairs[i] = { ...S.marketPairs[i], market_a: sel.value, evidence: null };
      // The value lists have not changed, so only this row's advice needs to
      // catch up - no panel rebuild, no re-read of the values.
      patchMarketRowAdvice();
      doMarketMapping(true);
    };
  });
  $$('#map-out select[data-mkt-b]').forEach(sel => {
    sel.onchange = () => {
      const i = Number(sel.dataset.mktB);
      S.marketPairs[i] = { ...S.marketPairs[i], market_b: sel.value, evidence: null };
      patchMarketRowAdvice();
      doMarketMapping(true);
    };
  });
  $$('#map-out select[data-mkt-level]').forEach(sel => {
    sel.onchange = () => {
      // The level is carried into the analysis; it does not change any pairing's
      // evidence, so there is nothing to ask the server and nothing to repaint.
      const i = Number(sel.dataset.mktLevel);
      S.marketPairs[i] = { ...S.marketPairs[i], level: sel.value };
    };
  });
  $$('#map-out [data-mkt-del]').forEach(btn => {
    btn.onclick = () => {
      S.marketPairs.splice(Number(btn.dataset.mktDel), 1);
      // A row left the table, so the numbering shifts and the panel is rebuilt -
      // but from local state, without a server call.
      renderMarketPairing();
    };
  });
}

// ---------------------------------------------------------------------------
// step 4 — category mapping, then the analysis
//
// One action. "Map all categories" reads both datasets' category structures and
// opens the editor; the user defines how A's categories correspond to B's; then
// the impact analysis runs on what they defined. A category they leave
// unmapped is not in the analysis, because it has no counterpart to compare
// against and reporting it would present a one-sided number as an impact.
// ---------------------------------------------------------------------------

/** Pick a sensible metric column on one side, for the values shown as evidence.
 *
 * "Sensible" means the *current-period column of the value metric*, resolved the
 * same generalised way the analysis resolves it - never a hardcoded "VALUE"/"TY"
 * key, which does not exist in a workbook whose periods are named differently.
 */
function pickMetricCol(side, preferFamily) {
  const fams = S.profile?.[side]?.metric_families || {};
  let fam = preferFamily && fams[preferFamily] ? preferFamily
    : Object.keys(fams).find(f => metricDefFor(f)?.key === 'sales_value')
    || Object.keys(fams)[0];
  if (!fam) return '';
  const variants = fams[fam] || {};
  return variants[defaultVariant(variants, 'current')]
    || Object.values(variants)[0] || '';
}

async function doCategoryMapping(metricOverride) {
  const a = spec('a'), b = spec('b');
  const catA = S.dimCols.a.category;
  const catB = S.dimCols.b.category;
  if (!catA || !catB) {
    alert('A category column is required in both datasets.\n'
        + 'Set it on the Profile step (step 2).');
    return;
  }
  const fam = metricOverride || $('#cm-metric')?.value || null;
  const metricA = pickMetricCol('a', fam);
  const metricB = pickMetricCol('b', fam);

  overlay(true, 'Reading the category structure of both datasets…');
  try {
    const d = await api('/api/category-mapping', {
      method: 'POST',
      body: JSON.stringify({
        a, b,
        cat_col_a: catA,
        sub_col_a: S.dimCols.a.subcategory || '',
        metric_col_a: metricA,
        cat_col_b: catB,
        sub_col_b: S.dimCols.b.subcategory || '',
        metric_col_b: metricB,
      }),
    });
    S.catmap = d;
    S.catmapMetric = fam || '';
    // A re-probe yields fresh unit lists, so any mapping still open must close:
    // its draft refers to categories from the set that was just replaced.
    S.catmapDraft = null;
    renderCatmap();
    go(4);
  } catch (e) {
    alert('The category structure could not be read:\n' + e.message);
  } finally { overlay(false); }
}

/**
 * The single action the brief asks for: map the categories, then run the
 * analysis on what the user defined.
 *
 * First press reads the structure and opens the mapping step. If the user has
 * already authored a mapping, it goes straight to the analysis - they are not
 * asked to map the same thing twice. Any Dataset 1 category left uncovered is
 * named before the run, so a category disappearing from the numbers is never a
 * surprise.
 */
async function mapAllCategories() {
  if (!S.catmap) { await doCategoryMapping(); return; }
  const unresolved = unresolvedCategories();
  const mapped = catmapList().length;
  if (!mapped) {
    alert('No category is mapped yet.\n\n'
        + 'Add at least one mapping before running the analysis, so there is '
        + 'something to compare.');
    go(4);
    return;
  }
  if (unresolved.length && !S.catmapWarned) {
    S.catmapWarned = true;
    const keep = confirm(
      `${unresolved.length} category(ies) are not mapped and will be left out of `
      + `the analysis:\n\n· ${unresolved.slice(0, 10).join('\n· ')}`
      + (unresolved.length > 10 ? `\n…and ${unresolved.length - 10} more.` : '')
      + `\n\nThey have no counterpart in the other dataset to compare against, so `
      + `they cannot produce an impact figure.\n\nRun the analysis anyway?`);
    if (!keep) return;
  }
  // The analysis runs on the mapping the user authored, with the metric and
  // period selection from step 5.
  confirmCategoryMapping({ thenRun: true });
}

// ---------------------------------------------------------------------------
// step 4 — category mapping
//
// One thing, and the brief is explicit about it: the user defines how a
// Dataset-1 category maps to a Dataset-2 category. Either side may be a bare
// category or a category-plus-subcategory, and one mapping may point at several
// Dataset-2 units. Nothing is mapped on the user's behalf, and the step lists
// only what the user built - not an inventory of both datasets.
//
// Deliberately absent, because it was asked for this way: no per-row status
// column, no "unmapped" inventory, no evidence hints, no confidence, no search
// or filter, no one-row-at-a-time accordion over every category. A mapping's
// existence IS the mapping.
//
//   [ + Mapping ]                         <- the only control
//   card:  A side  ->  B side(s)  ->  reported as
//   ...   click it: A picker, one-or-more B pickers, reported name
//   [ Done ]   -> saved to the list above
// ---------------------------------------------------------------------------

/** The mappings the user has authored. */
function catmapList() {
  return S.catmapMaps || [];
}

/** Unit key. Category and subcategory, folded so blanks never differ. */
function ukey(cat, sub) {
  return `${cat || ''}\x1f${sub || ''}`;
}

/** A unit's own key. */
function akey(u) {
  return ukey(u.category, u.subcategory || '');
}

/** The human label for a unit. */
function ukeyLabel(u) {
  return u.subcategory ? `${u.category} / ${u.subcategory}` : String(u.category);
}

/** The reported name for a unit, when the user leaves the field blank. */
function label(cat, sub) {
  return sub ? `${cat} / ${sub}` : String(cat || '');
}

/** Dataset-1 categories no mapping covers. They produce no impact figure. */
function unresolvedCategories() {
  const covered = new Set(catmapList().map(m => m.source_key));
  return (S.catmap?.a_units || [])
    .filter(u => !covered.has(akey(u)))
    .map(ukeyLabel)
    .sort();
}

function renderCatmap() {
  const box = $('#catmap-out');
  if (!box) return;
  const maps = catmapList();
  const aUnits = (S.catmap?.a_units || []).slice().sort(
    (x, y) => ukeyLabel(x).localeCompare(ukeyLabel(y)));
  const bUnits = (S.catmap?.b_units || []).slice().sort(
    (x, y) => ukeyLabel(x).localeCompare(ukeyLabel(y)));
  S._cmAUnits = aUnits;
  S._cmBUnits = bUnits;
  S._cmACats = Array.from(new Set(aUnits.map(u => u.category))).sort();
  S._cmBCats = Array.from(new Set(bUnits.map(u => u.category))).sort();
  S._cmASubsFor = (cat) => Array.from(new Set(
    aUnits.filter(u => u.category === cat && u.subcategory)
      .map(u => u.subcategory))).sort();
  S._cmSubsFor = (cat) => Array.from(new Set(
    bUnits.filter(u => u.category === cat && u.subcategory)
      .map(u => u.subcategory))).sort();

  // The editor is the point of the step, so it is the only thing on screen
  // until the user clicks Done or Cancel.
  if (S.catmapDraft) { box.innerHTML = draftHtml(); bindDraft(); return; }

  const uncovered = unresolvedCategories();
  box.innerHTML = `
    <div class="card">
      <div class="card-head">
        <h3>Category mappings (${maps.length})</h3>
        <span class="sub">each mapping pairs a Dataset 1 category with a Dataset 2 category</span>
      </div>
      <div class="row" style="margin-bottom:12px">
        <button class="btn primary" id="cm-add-map">+ Mapping</button>
        <span class="spacer"></span>
        ${maps.length
          ? `<span class="hint">${maps.length} mapping${maps.length === 1 ? '' : 's'} defined
             · ${uncovered.length} of ${aUnits.length} Dataset 1 categories not mapped</span>`
          : `<span class="hint">No mappings yet — click “+ Mapping” to define one.</span>`}
      </div>
      ${maps.length
        ? `<div class="cm-map-list">${maps.map(mapCardHtml).join('')}</div>
           ${uncovered.length ? `<p class="hint" style="margin:12px 0 0">
             Not mapped, so not analysed: ${uncovered.slice(0, 8).map(esc).join(', ')}${uncovered.length > 8 ? ` and ${uncovered.length - 8} more` : ''}.
             A category with no counterpart has no before-value to compare against.</p>` : ''}`
        : `<p class="hint" style="margin:0">
             A Dataset 1 category with no mapping has no counterpart to compare
             against, so it produces no impact figure and is left out.</p>`}
    </div>`;

  const add = $('#cm-add-map');
  if (add) add.onclick = () => openDraft(null);
}

/** One saved mapping: what it maps, and what it is reported as. */
function mapCardHtml(m, i) {
  const src = m.source_sub
    ? `${esc(m.source)} <span class="hint">/ ${esc(m.source_sub)}</span>`
    : esc(m.source);
  const dst = m.targets.map(t => t.subcategory
    ? `${esc(t.category)} <span class="hint">/ ${esc(t.subcategory)}</span>`
    : esc(t.category)).join(' <span class="hint">+</span> ');
  // Only show the reported name when it adds information - otherwise the card
  // repeats the Dataset 1 name three times.
  const derived = label(m.source, m.source_sub || '');
  return `
    <div class="cm-map">
      <div class="cm-map-side">
        <div class="hint">Dataset 1</div>
        <div class="cm-map-cat">${src}</div>
      </div>
      <div class="cm-map-arrow">→</div>
      <div class="cm-map-side">
        <div class="hint">Dataset 2</div>
        <div class="cm-map-cat">${dst}</div>
      </div>
      ${m.canonical !== derived ? `
      <div class="cm-map-side">
        <div class="hint">Reported as</div>
        <div class="cm-map-cat">${esc(m.canonical)}</div>
      </div>` : ''}
      <div class="cm-map-acts">
        <button class="btn small" data-cm-edit="${i}">Edit</button>
        <button class="btn small ghost" data-cm-remove="${i}" title="Remove">×</button>
      </div>
    </div>`;
}

// ---------------------------------------------------------------------------
// the editor
// ---------------------------------------------------------------------------

/**
 * Start a new mapping, or edit an existing one.
 *
 * `index === null` means new. The draft is a deep copy so Cancel genuinely
 * discards - an aliased object would let Cancel keep every edit.
 */
function openDraft(index = null) {
  const existing = index === null ? null : catmapList()[index];
  const draft = existing
    ? JSON.parse(JSON.stringify(existing))
    : { source: '', source_sub: '', canonical: '', targets: [] };
  draft._index = index;
  if (!draft.targets.length) draft.targets = [{ category: '', subcategory: '' }];
  S.catmapDraft = draft;
  renderCatmap();
}

/** Save the draft into the list. This is what "Done" does. */
function commitDraft() {
  const d = S.catmapDraft;
  if (!d) return;
  if (!d.source) { alert('Choose the Dataset 1 category first.'); return; }
  const targets = d.targets.filter(t => t.category);
  if (!targets.length) {
    alert('Choose at least one Dataset 2 category to map it to.');
    return;
  }
  const entry = {
    source: d.source,
    source_sub: d.source_sub || '',
    source_key: ukey(d.source, d.source_sub || ''),
    // Blank means "report it under the name it already has".
    canonical: (d.canonical || '').trim() || label(d.source, d.source_sub),
    targets: targets.map(t => ({
      category: t.category,
      subcategory: t.subcategory || '',
    })),
    status: 'mapped',
  };
  S.catmapMaps = S.catmapMaps || [];
  if (d._index === null) {
    // A duplicate would produce two rows fighting over the same Dataset 1 unit.
    const dup = S.catmapMaps.findIndex(m => m.source_key === entry.source_key);
    if (dup >= 0) S.catmapMaps[dup] = entry;
    else S.catmapMaps.push(entry);
  } else {
    S.catmapMaps[d._index] = entry;
  }
  S.catmapDraft = null;
  S.catmapTouched = true;
  renderCatmap();
}

function removeMap(index) {
  S.catmapMaps.splice(index, 1);
  S.catmapTouched = true;
  renderCatmap();
}

function draftHtml() {
  const d = S.catmapDraft;
  const aCats = S._cmACats || [];
  const aSubs = d.source ? (S._cmASubsFor || (() => []))(d.source) : [];
  // A mapping may be a bare category OR a category + subcategory, and the
  // combination is allowed on *either* side. So the picker is offered whenever
  // the wired structure can express a subcategory at all - a subcategory column
  // is wired on this side, or the picked category has subcategory units. With
  // neither, the only honest option is the whole category, and the select still
  // renders (with just that one option) so both sides look the same.
  const showSub = !!(S.dimCols.a.subcategory || S.catmap?.has_subcategory_a
                     || aSubs.length > 0);
  const derived = d.source ? label(d.source, d.source_sub) : '';
  return `
    <div class="card cm-draft">
      <div class="card-head">
        <h3>${d._index === null ? 'New mapping' : 'Edit mapping'}</h3>
        <span class="sub">pick a Dataset 1 category (optionally + subcategory), then the Dataset 2 entries it corresponds to</span>
      </div>

      <div class="cm-draft-grid">
        <div class="cm-draft-col">
          <h4>Dataset 1</h4>
          <div class="field">
            <label>Category</label>
            <select id="cm-draft-a-cat">
              <option value="">— pick a category —</option>
              ${aCats.map(c => `<option ${d.source === c ? 'selected' : ''}>${esc(c)}</option>`).join('')}
            </select>
          </div>
          ${showSub ? `
          <div class="field">
            <label>Subcategory <span class="hint">(optional)</span></label>
            <select id="cm-draft-a-sub" ${d.source ? '' : 'disabled'}>
              <option value="">(whole category)</option>
              ${aSubs.map(s => `<option ${d.source_sub === s ? 'selected' : ''}>${esc(s)}</option>`).join('')}
            </select>
          </div>` : ''}
        </div>

        <div class="cm-draft-col">
          <h4>Dataset 2 <span class="hint">— one or more</span></h4>
          <div id="cm-draft-targets">
            ${d.targets.map(targetRowHtml).join('')}
          </div>
          <button class="btn small" id="cm-draft-add">+ add another Dataset 2 entry</button>
        </div>
      </div>

      <div class="field" style="margin-top:14px;max-width:440px">
        <label>Reported as <span class="hint">(optional — blank uses the Dataset 1 name)</span></label>
        <input id="cm-draft-name" value="${esc(d.canonical || '')}"
               placeholder="${esc(derived || 'the analysis name')}">
      </div>

      <div class="row" style="margin-top:16px">
        <button class="btn primary" id="cm-draft-done">Done</button>
        <button class="btn ghost" id="cm-draft-cancel">Cancel</button>
      </div>
    </div>`;
}

function targetRowHtml(t, ti) {
  const bCats = S._cmBCats || [];
  const subs = t.category ? (S._cmSubsFor || (() => []))(t.category) : [];
  const many = (S.catmapDraft?.targets || []).length > 1;
  // Symmetric with the Dataset 1 picker: a Dataset 2 target may be a bare
  // category or a category + subcategory, so the sub select is shown whenever
  // that side can express a subcategory at all. `(whole category)` is always the
  // default, so a bare target is still one click away.
  const showSub = !!(S.dimCols.b.subcategory || S.catmap?.has_subcategory_b
                     || subs.length > 0);
  return `
    <div class="cm-draft-target">
      <select data-draft-cat="${ti}">
        <option value="">— pick a category —</option>
        ${bCats.map(c => `<option ${t.category === c ? 'selected' : ''}>${esc(c)}</option>`).join('')}
      </select>
      ${showSub ? `
        <select data-draft-sub="${ti}" ${t.category ? '' : 'disabled'}>
          <option value="">(whole category)</option>
          ${subs.map(s => `<option ${t.subcategory === s ? 'selected' : ''}>${esc(s)}</option>`).join('')}
        </select>` : ''}
      ${many ? `<button class="btn small ghost" data-draft-del="${ti}" title="Remove">×</button>` : ''}
    </div>`;
}

function bindDraft() {
  const d = S.catmapDraft;
  if (!d) return;
  const aCat = $('#cm-draft-a-cat');
  if (aCat) aCat.onchange = () => {
    d.source = aCat.value;
    d.source_sub = '';
    renderCatmap();   // repaint so the subcategory list matches the category
  };
  const aSub = $('#cm-draft-a-sub');
  if (aSub) aSub.onchange = () => { d.source_sub = aSub.value; };
  const name = $('#cm-draft-name');
  if (name) name.oninput = () => { d.canonical = name.value; };

  const add = $('#cm-draft-add');
  if (add) add.onclick = () => {
    d.targets.push({ category: '', subcategory: '' });
    renderCatmap();
  };
  // Rebound after every repaint, because a category change re-renders the row
  // (the subcategory list depends on it) and takes the old nodes with it.
  $$('#cm-draft-targets [data-draft-cat]').forEach(sel => {
    sel.onchange = () => {
      const ti = Number(sel.dataset.draftCat);
      d.targets[ti] = { category: sel.value, subcategory: '' };
      renderCatmap();
    };
  });
  $$('#cm-draft-targets [data-draft-sub]').forEach(sel => {
    sel.onchange = () => {
      d.targets[Number(sel.dataset.draftSub)].subcategory = sel.value;
    };
  });
  $$('#cm-draft-targets [data-draft-del]').forEach(btn => {
    btn.onclick = () => {
      d.targets.splice(Number(btn.dataset.draftDel), 1);
      if (!d.targets.length) d.targets = [{ category: '', subcategory: '' }];
      renderCatmap();
    };
  });
  const done = $('#cm-draft-done');
  if (done) done.onclick = commitDraft;
  const cancel = $('#cm-draft-cancel');
  if (cancel) cancel.onclick = () => { S.catmapDraft = null; renderCatmap(); };
}

// Edit / remove on a saved mapping. Delegated, because the list is rebuilt on
// every repaint.
document.addEventListener('click', e => {
  const edit = e.target.closest('[data-cm-edit]');
  if (edit) { openDraft(Number(edit.dataset.cmEdit)); return; }
  const rm = e.target.closest('[data-cm-remove]');
  if (rm) { removeMap(Number(rm.dataset.cmRemove)); return; }
});

/** The confirmed mapping, in the shape the run expects. */
function buildCategoryMapping() {
  const rows = catmapList().map(m => ({
    source: m.source,
    source_sub: m.source_sub || '',
    canonical: m.canonical,
    targets: m.targets.map(t => ({ ...t })),
    status: 'mapped',
  }));
  return {
    rows,
    new_in_b: S.catmap?.new_in_b || [],
    include_new_in_b: !!S.catmapIncludeNewInB,
  };
}

/** The categories the analysis will report, in a stable order. */
function canonicalCategoryList() {
  const out = new Set();
  catmapList().forEach(m => { if (m.canonical) out.add(m.canonical); });
  if (S.catmapIncludeNewInB) {
    const claimed = new Set();
    catmapList().forEach(m => m.targets.forEach(
      t => claimed.add(ukey(t.category, t.subcategory || ''))));
    (S.catmap?.new_in_b || []).forEach(u => {
      if (!claimed.has(akey(u))) out.add(ukeyLabel(u));
    });
  }
  return Array.from(out).sort();
}

// ---------------------------------------------------------------------------
// carrying the mapping forward into the run
// ---------------------------------------------------------------------------

/**
 * Carry the user's mapping forward, and optionally run the analysis with it.
 *
 * `thenRun` is the second half of the combined action: the mapping the user just
 * authored is the mapping the analysis uses, so there is no window in which the
 * two can disagree.
 */
function confirmCategoryMapping(opts = {}) {
  const { mapping_a, mapping_b } = buildMappings();
  S.mapping_a = mapping_a;
  S.mapping_b = mapping_b;
  S.categoryMapping = buildCategoryMapping();
  buildSelectionUI();
  go(5);
  if (opts.thenRun) doRun();
}

function buildMappings() {
  // The market pairing is the only dimension mapping now. It produces a per-side
  // map: A's value onto B's value. Manufacturer and brand are left alone and use
  // each dataset's own names, which is what the analysis did before the mapping
  // tabs existed.
  const mapping_a = {}, mapping_b = {};
  (S.marketPairs || []).forEach(p => {
    if (!p.market_a || !p.market_b) return;
    mapping_a.market = mapping_a.market || {};
    mapping_b.market = mapping_b.market || {};
    mapping_a.market[p.market_a] = p.market_b;
    // B already carries the target name, so its own map is the identity. It is
    // populated so a caller that reads mapping_b does not see a gap.
    mapping_b.market[p.market_b] = p.market_b;
  });
  return { mapping_a, mapping_b };
}

// ---------------------------------------------------------------------------
// Impact metrics
//
// The brief names three metrics — Sales Value, Volume and Numeric Distribution —
// and allows any combination of them. Each is resolved on both datasets
// independently, because the two need not name the same measure the same way.
//
// ND is special: it is a *distribution level*, not an accumulating quantity, so
// a percentage growth of it is not a meaningful impact measure. It is reported
// as Top / TY / YA / absolute change (TY - YA) and carries no growth at all.
//
// Period selects hold variant **keys** (`YA`, `VALUE`, and whatever token this
// workbook uses), and the request must carry the real **column names** the
// family maps them to. Every select resolves through the family, so a workbook
// whose period names are translated - or which has no recognisable qualifier at
// all - works unchanged: the options are whatever the profile actually found,
// and the analysis reads only the two concrete columns the selects resolve to.
// ---------------------------------------------------------------------------
const METRIC_DEFS = [
  {
    key: 'sales_value',
    label: 'Sales Value',
    match: /sales?\s*value|\bvalue\b|umsatz|revenue|turnover/i,
    is_rate: false,
    growth_applicable: true,
    hint: 'Value. Growth, level shift and contribution.',
  },
  {
    key: 'volume',
    label: 'Volume',
    match: /\bvolume\b|\bunits?\b|\bqty\b|\bquantity\b|menge|absatz/i,
    is_rate: false,
    growth_applicable: true,
    hint: 'Units / volume. Growth, level shift and contribution.',
  },
  {
    key: 'nd',
    label: 'Numeric Distribution (ND)',
    match: /\bnd\b|numeric\s*distribution|\bdist\b|distribution|distribution/i,
    is_rate: true,
    growth_applicable: false,
    hint: 'Distribution level. Top / TY / YA / absolute change — no growth.',
  },
];

/** A metric family name that matches one of the three defined metrics. */
function metricDefFor(familyName) {
  return METRIC_DEFS.find(d => d.match.test(String(familyName || ''))) || null;
}

/** Find the best metric-family name in a profile for a given definition. */
function familyFor(profile, def) {
  const fams = Object.keys(profile?.metric_families || {});
  const exact = fams.find(f => def.match.test(f));
  if (exact) return exact;
  if (def.key === 'sales_value') {
    // fall back to the biggest additive family
    return fams.find(f => !metricDefFor(f)) || '';
  }
  return '';
}

// ---------------------------------------------------------------------------
// step 5 — selection
// ---------------------------------------------------------------------------

function buildSelectionUI() {
  // Each side gets its own metric list. They need not share a name.
  const famsA = Object.keys(S.profile.a.metric_families || {}).sort();
  const famsB = Object.keys(S.profile.b.metric_families || {}).sort();

  // The checkboxes. Selection state lives in S.metricSel.
  if (!S.metricSel) S.metricSel = { sales_value: true, volume: false, nd: false };
  renderMetricPicks();
  renderMetricWiring(famsA, famsB);
  renderMetricPeriods();
  // Paint the wiring verdict now, not only on the next change. Without this the
  // user arrives on step 5 to a blank confirmation box, which reads as "the
  // metric is not wired" even though it is.
  validateMetricWiring();

  // markets — every value the user paired in step 3, plus what the level implies.
  //
  // This used to list only the pairings whose level equalled `S.marketLevel`, so
  // choosing "Channels" in step 3 dropped every region and every total from the
  // step-5 chip list - which read as "I am not getting all the markets". The
  // chip list is the *available* set; the level decides which of them the run
  // defaults to. Showing all of them and pre-selecting the level's members is
  // both more honest and more useful, since a run may legitimately want a total
  // alongside the channels beneath it.
  const pairs = S.marketPairs || [];
  const lvl = S.marketLevel || 'total';
  const allPaired = Array.from(new Set(pairs.map(p => p.market_a).filter(Boolean)));
  const atLevel = pairs
    .filter(p => lvl === 'all' || p.level === lvl)
    .map(p => p.market_a)
    .filter(Boolean);
  // Pre-select the level's members; when a level has no pairings, fall back to
  // showing none selected rather than silently selecting everything, so the
  // empty case is visible instead of looking like a full selection.
  const scope = lvl === 'all' ? allPaired : Array.from(new Set(atLevel));
  $('#c-markets').innerHTML = allPaired.map(v =>
    `<span class="chip ${scope.includes(v) ? 'on' : ''}" data-market="${esc(v)}">${esc(v)}</span>`).join('')
    || '<span class="hint">No market dimension detected.</span>';
  S.selection.markets = scope;
  if (!S.marketAdvisory && !S._mktAsked) {
    S._mktAsked = true;
    doMarketMapping().then(() => {
      if ($('#c-markets')) buildSelectionUI();
    }).catch(() => {});
  }

  // Client and impact name, carried into the exports.
  const ci = $('#c-client'), ii = $('#c-impact');
  if (ci) { ci.value = S.clientName || ''; ci.oninput = () => { S.clientName = ci.value; }; }
  if (ii) { ii.value = S.impactName || ''; ii.oninput = () => { S.impactName = ii.value; }; }

  // categories come from the confirmed category mapping, not from A's raw names
  S._allCategories = canonicalCategoryList();
  S.selection.categories = [...S._allCategories];
  renderCategories();

  // client entity suggestions
  const opts = new Set([
    ...(S.profile.a_dim_values?.brand || []),
    ...(S.profile.b_dim_values?.brand || []),
    ...(S.profile.a_dim_values?.manufacturer || []),
    ...(S.profile.b_dim_values?.manufacturer || []),
  ]);
  $('#client-options').innerHTML = Array.from(opts).sort()
    .map(v => `<option value="${esc(v)}">`).join('');

  renderClients();
  $('#c-topn').value = String(S.selection.top_n);
}

/** The three metric checkboxes, plus a custom family when one is unmapped. */
function renderMetricPicks() {
  const box = $('#c-metric-picks');
  if (!box) return;
  const famsA = Object.keys(S.profile.a.metric_families || {});
  const used = new Set();
  const html = METRIC_DEFS.map(def => {
    const on = !!S.metricSel[def.key];
    const fam = familyFor(S.profile.a, def);
    if (fam) used.add(fam);
    const avail = !!fam || !!familyFor(S.profile.b, def);
    return `<label class="metric-pick ${on ? 'on' : ''} ${avail ? '' : 'missing'}">
      <input type="checkbox" data-metric="${def.key}" ${on ? 'checked' : ''}
             ${avail ? '' : 'disabled'}>
      <span class="mp-name">${esc(def.label)}</span>
      <span class="mp-hint">${esc(def.hint)}</span>
      <span class="mp-fam">${avail
        ? `A: ${esc(familyFor(S.profile.a, def) || '—')} · B: ${esc(familyFor(S.profile.b, def) || '—')}`
        : 'not found in either dataset'}</span>
    </label>`;
  }).join('');
  box.innerHTML = html;

  // The checkbox change is handled by the single delegated document-level
  // 'change' listener (see the [data-metric] branch), so that re-rendering the
  // wiring panel cannot leave a stale handler behind on a detached node.

  const rule = $('#c-metric-rule');
  if (rule) {
    const anyRate = METRIC_DEFS.some(d => S.metricSel[d.key] && d.is_rate);
    const anyNoGrowth = METRIC_DEFS.some(d => S.metricSel[d.key] && !d.growth_applicable);
    const bits = [];
    if (anyRate) bits.push('a rate metric is averaged (weighted by a value column), never summed');
    if (anyNoGrowth) bits.push('ND reports Top / TY / YA / absolute change (TY − YA) — '
      + 'no percentage growth, because a distribution level has no meaningful growth rate');
    rule.innerHTML = bits.length ? 'Note: ' + bits.join('; ') + '.' : '';
  }
}

/**
 * The per-dataset column wiring, one row per selected metric. This is where the
 * two datasets are allowed to name the same measure differently, so every family
 * dropdown is per-side.
 */
function renderMetricWiring(famsA, famsB) {
  const box = $('#c-metric-wiring');
  if (!box) return;
  famsA = famsA || Object.keys(S.profile.a.metric_families || {}).sort();
  famsB = famsB || Object.keys(S.profile.b.metric_families || {}).sort();
  const sel = METRIC_DEFS.filter(d => S.metricSel[d.key]);
  if (!sel.length) {
    box.innerHTML = '<p class="hint" style="padding:6px 0">No metric selected.</p>';
    return;
  }
  box.innerHTML = sel.map(def => {
    const cur = S.metricWiring?.[def.key] || {};
    const fa = cur.family_a !== undefined ? cur.family_a : familyFor(S.profile.a, def);
    const fb = cur.family_b !== undefined ? cur.family_b : (familyFor(S.profile.b, def) || fa);
    const opt = (list, v) => list.map(f =>
      `<option ${f === v ? 'selected' : ''}>${esc(f)}</option>`).join('');
    const weightRow = def.is_rate ? `
      <div class="field" style="margin:0">
        <label>Weight (A · B)</label>
        <div class="grid two tight">
          <select data-wire="weight_a" data-key="${def.key}">${
            ['', ...famsA].map(f => `<option ${f === (cur.weight_a ?? '') ? 'selected' : ''}>${esc(f)}</option>`).join('')}</select>
          <select data-wire="weight_b" data-key="${def.key}">${
            ['', ...famsB].map(f => `<option ${f === (cur.weight_b ?? '') ? 'selected' : ''}>${esc(f)}</option>`).join('')}</select>
        </div>
      </div>` : '';
    return `<div class="wire-card">
      <div class="wire-head">${esc(def.label)}</div>
      <div class="grid two tight">
        <div class="field" style="margin:0">
          <label>Family — A (previous)</label>
          <select data-wire="family_a" data-key="${def.key}">
            ${famsA.length ? opt(famsA, fa) : '<option value="">— none —</option>'}
          </select>
        </div>
        <div class="field" style="margin:0">
          <label>Family — B (updated)</label>
          <select data-wire="family_b" data-key="${def.key}">
            ${famsB.length ? opt(famsB, fb) : '<option value="">— none —</option>'}
          </select>
        </div>
      </div>
      ${weightRow}
    </div>`;
  }).join('');

  $$('#c-metric-wiring [data-wire]').forEach(el => {
    el.onchange = () => {
      const key = el.dataset.key;
      S.metricWiring = S.metricWiring || {};
      S.metricWiring[key] = { ...(S.metricWiring[key] || {}), [el.dataset.wire]: el.value };
      renderMetricPeriods();
      validateMetricWiring();
    };
  });
}

/**
 * The two periods the study reads, shown per metric.
 *
 * This is a **report, not a question**. MAT YA and MAT TY are already in the
 * data: the metric family carries them as a period qualifier on the column name
 * (`Sales Value YA` is MAT YA; the unqualified `Sales Value` is MAT TY), and the
 * fact table's own `Periods` column says the same thing. Asking the user to name
 * them again invited a second answer that could disagree with the first, and the
 * default it offered (2YA -> YA) landed on a column one window too far back - so
 * A read B's year-ago column and the report called the difference growth.
 *
 * The server resolves both slots from the family and rewrites the wiring, so
 * there is nothing to choose here. What is shown is what the run will read.
 */
function renderMetricPeriods() {
  const box = $('#c-periods');
  if (!box) return;
  const sel = METRIC_DEFS.filter(d => S.metricSel[d.key]);
  if (!sel.length) {
    box.innerHTML = '<p class="hint">Select at least one metric.</p>';
    return;
  }
  // The two slots the study reads, and the family variant that names each. The
  // variant key is what the request carries; the column is what the user sees,
  // because the column is the thing they can check against their workbook.
  const SLOTS = [
    { role: 'MAT YA', want: 'prior', variants: ['YA'], label: 'Year ago' },
    { role: 'MAT TY', want: 'current', variants: ['VALUE', 'TY'], label: 'This year' },
  ];
  box.innerHTML = sel.map(def => {
    const wires = resolveWiring(def);
    const fa = S.profile.a.metric_families?.[wires.family_a] || {};
    const fb = S.profile.b.metric_families?.[wires.family_b] || {};
    const rows = SLOTS.map(slot => {
      const colA = slot.variants.map(v => fa[v]).find(Boolean) || '';
      const colB = slot.variants.map(v => fb[v]).find(Boolean) || colA;
      const cell = c => c
        ? `<code>${esc(c)}</code>`
        : '<span class="tag warn">not in this workbook</span>';
      return `<tr>
        <td><b>${esc(slot.role)}</b><div class="hint">${esc(slot.label)}</div></td>
        <td>${cell(colA)}</td><td>${cell(colB)}</td>
      </tr>`;
    }).join('');
    return `<div class="wire-card">
      <div class="wire-head">${esc(def.label)}
        ${def.is_rate ? '<span class="tag warn">no growth</span>' : ''}</div>
      <p class="hint" style="margin:2px 0 8px">Read from the Period columns — the
         qualifier in the metric header names each period, so there is nothing to
         map here. <b>2YA</b> is not part of the study and is never read.</p>
      <table class="mini" style="width:100%"><thead><tr>
        <th style="width:34%">Period</th>
        <th>${esc(S.profile.a.label || 'Previous dataset')}</th>
        <th>${esc(S.profile.b.label || 'Updated dataset')}</th>
      </tr></thead><tbody>${rows}</tbody></table>
    </div>`;
  }).join('');
}

/**
 * The recognised period classes, earliest first. Mirrors the backend's
 * ``profiling.QUALIFIER_TOKENS``, and is used for *ordering and defaulting only*
 * - the variants themselves are always the ones the profile actually found, so a
 * workbook with translated or unrecognised period names still works.
 */
const VARIANT_ORDER = ['2YA', 'YA', 'TY', 'VALUE'];
const PRIOR_VARIANTS = ['2YA', 'YA'];
const CURRENT_VARIANTS = ['VALUE', 'TY'];

/**
 * The variant key a family should default to for the prior / current period.
 *
 * Retained for the paths that need "a sensible column from this family" rather
 * than a named period slot (the category-mapping preview and the rate-weight
 * default). The study's own MAT YA / MAT TY slots do **not** come through here -
 * `buildMetricBlocks` resolves them itself so `2YA` can be excluded, which
 * ``PRIOR_VARIANTS`` includes.
 */
function defaultVariant(obj, want) {
  const keys = Object.keys(obj || {});
  if (!keys.length) return '';
  const list = want === 'prior' ? PRIOR_VARIANTS : CURRENT_VARIANTS;
  const hit = list.find(v => keys.includes(v));
  if (hit) return hit;
  // No recognised class (a translated or absent qualifier): fall back to
  // position, with the later variant as the current period.
  const ordered = keys.slice().sort(
    (x, y) => (VARIANT_ORDER.indexOf(x) + 1 || 99) - (VARIANT_ORDER.indexOf(y) + 1 || 99));
  return want === 'prior' ? (ordered[0] || '') : (ordered[ordered.length - 1] || '');
}

/** Resolve a metric definition to its two family names, honouring any override. */
function resolveWiring(def) {
  const cur = S.metricWiring?.[def.key] || {};
  const family_a = cur.family_a !== undefined && cur.family_a !== ''
    ? cur.family_a : familyFor(S.profile.a, def);
  const family_b = cur.family_b !== undefined && cur.family_b !== ''
    ? cur.family_b : (familyFor(S.profile.b, def) || family_a);
  return { ...cur, family_a, family_b };
}

/**
 * Turn the selected metrics into the request blocks, reading the periods from
 * the family and the per-side weight columns.
 *
 * The period **slots** are fixed by the study - MAT YA and MAT TY - and each is
 * named by a variant of the metric family the profile found. There is no longer
 * a select to read: the request derives the two columns itself, so the only
 * possible answer is the one the period qualifier gives.
 */
function buildMetricBlocks() {
  const out = [];
  METRIC_DEFS.forEach(def => {
    if (!S.metricSel?.[def.key]) return;
    const w = resolveWiring(def);
    const fa = S.profile.a.metric_families?.[w.family_a] || {};
    const fb = S.profile.b.metric_families?.[w.family_b] || {};
    // MAT YA is named by the `YA` variant; MAT TY by the unqualified measure
    // (`VALUE`), falling back to an explicit `TY`. A family that spells both
    // differently still resolves: `matSlot` walks the family's own variants
    // before giving up, so a translated or unqualified workbook keeps working.
    //
    // `2YA` is excluded by construction - it is a third moving-annual window
    // the study does not use, and including it here is what previously made the
    // "year ago" slot read a column two years back.
    const matSlot = (fam, want) => {
      const keys = Object.keys(fam || {});
      if (!keys.length) return { key: '', col: '' };
      const preferred = want === 'ya' ? ['YA'] : ['VALUE', 'TY'];
      let key = preferred.find(v => keys.includes(v));
      if (!key) {
        // No recognised qualifier: order the variants and take an end, with the
        // later one as MAT TY. 2YA never participates.
        const ordered = keys.filter(k => k !== '2YA').sort(
          (x, y) => (VARIANT_ORDER.indexOf(x) + 1 || 99) - (VARIANT_ORDER.indexOf(y) + 1 || 99));
        key = want === 'ya' ? (ordered[0] || '') : (ordered[ordered.length - 1] || '');
      }
      return { key, col: (key && fam[key]) || '' };
    };

    // A and B name their periods independently; B falls back to A's column only
    // when its own family cannot supply the slot at all.
    const yaA = matSlot(fa, 'ya'), tyA = matSlot(fa, 'ty');
    const yaB = matSlot(fb, 'ya'), tyB = matSlot(fb, 'ty');
    out.push({
      key: def.key,
      label: def.label,
      family_a: w.family_a || '',
      family_b: w.family_b || '',
      a_prior: yaA.col, a_current: tyA.col,
      b_prior: yaB.col || yaA.col, b_current: tyB.col || tyA.col,
      is_rate: def.is_rate,
      growth_applicable: def.growth_applicable,
      weight_metric_a: def.is_rate ? (w.weight_a || defaultWeight('a')) : '',
      weight_metric_b: def.is_rate ? (w.weight_b || defaultWeight('b')) : '',
    });
  });
  return out;
}

/**
 * The *column* to weight a rate metric by, per side.
 *
 * The server validates this against the real columns, so returning the family
 * name (e.g. "Sales Value") fails when the column is named "Sales Value 2YA" or
 * the family carries several periods. Prefer the family's current-period column,
 * resolved from the variants the profile actually found rather than from a
 * hardcoded "VALUE"/"TY" key.
 */
function defaultWeight(side) {
  const fams = S.profile[side]?.metric_families || {};
  const name = Object.keys(fams).find(f => metricDefFor(f)?.key === 'sales_value')
    || Object.keys(fams)[0] || '';
  const fam = fams[name] || {};
  return fam[defaultVariant(fam, 'current')] || Object.values(fam)[0] || '';
}

/**
 * The metric wiring must resolve on both sides. Previously an unmapped side
 * silently produced an empty analysis, which reads as "the selection does not
 * work" - so say so up front and block the run.
 */
function validateMetricWiring() {
  const problems = [];
  const blocks = buildMetricBlocks();
  if (!blocks.length) problems.push('Select at least one metric.');
  // Every column the profile knows about, per side. Checking the resolves against
  // these is what stops this validator from reporting "Wired" while the server
  // refuses the run - it previously only tested for a non-empty value, so a
  // variant key that was not a real column passed here and failed there.
  const knownCols = (side) => {
    const prof = S.profile[side] || {};
    const out = new Set();
    Object.values(prof.metric_families || {}).forEach(fam =>
      Object.values(fam || {}).forEach(col => col && out.add(col)));
    // `metrics` is an array (of names or {name} objects) and the authoritative
    // per-column detail is `column_profiles` - note `columns` is a *count*, and
    // treating it as a list throws.
    (Array.isArray(prof.metrics) ? prof.metrics : []).forEach(m =>
      out.add(typeof m === 'string' ? m : (m && (m.name || m.column))));
    (Array.isArray(prof.column_profiles) ? prof.column_profiles : []).forEach(c =>
      out.add(typeof c === 'string' ? c : (c && c.name)));
    out.delete(undefined);
    return out;
  };
  const colsA = knownCols('a'), colsB = knownCols('b');
  blocks.forEach(b => {
    [['MAT YA (year ago)', b.a_prior, colsA],
     ['MAT TY (this year)', b.a_current, colsA],
     ['MAT YA (year ago)', b.b_prior, colsB],
     ['MAT TY (this year)', b.b_current, colsB]]
      .forEach(([label, v, known]) => {
        if (!v) problems.push(`${b.label}: no ${label} column resolved`);
        // Only enforce membership when the profile actually listed columns; an
        // empty set means we do not know, and guessing would be worse.
        else if (known.size && !known.has(v)) {
          problems.push(`${b.label}: ${label} column "${v}" is not in this dataset`);
        }
      });
    if (b.is_rate && !b.weight_metric_a && !b.weight_metric_b) {
      problems.push(`${b.label}: a rate metric needs a weight column`);
    }
  });

  const el = $('#c-metric-note');
  if (el) {
    if (problems.length) {
      el.innerHTML = `<div class="notice err"><b>The analysis cannot run yet.</b><br>· `
        + problems.map(esc).join('<br>· ') + '</div>';
    } else {
      el.innerHTML = `<div class="notice ok">Wired: `
        + blocks.map(b => `<b>${esc(b.label)}</b>`
            + (b.family_a !== b.family_b
              ? ` (A: ${esc(b.family_a)}, B: ${esc(b.family_b)})` : '')).join(' · ')
        + `. Each category will carry one block per metric.</div>`;
    }
  }
  return problems;
}

function updateCatCount() {
  const all = S._allCategories || [];
  const n = S.selection.categories.length;
  $('#c-cat-count').textContent =
    `${n} of ${all.length} categories selected` +
    (n > 1 ? ` — will produce ${n * 2} files` : '');
}

// No text filter here on purpose: the list is already short enough to scan and
// the selection is made by clicking, so a search box in front of it was just
// another thing to clear before the chips were reachable.
function renderCategories() {
  const all = S._allCategories || [];
  const box = $('#c-categories');
  if (!box) return;
  // An empty list here is almost always "nothing has been mapped yet" rather
  // than "no categories exist". Saying which saves a hunt through step 4.
  box.innerHTML = all.map(c =>
    `<span class="chip ${S.selection.categories.includes(c) ? 'on' : ''}" data-cat="${esc(c)}">${esc(c)}</span>`
  ).join('') || `<span class="hint">No categories yet — map at least one category
      in step 4 and it will appear here.</span>`;
  updateCatCount();
}

$('#c-cat-all')?.addEventListener('click', () => {
  S.selection.categories = [...(S._allCategories || [])]; renderCategories();
});
$('#c-cat-none')?.addEventListener('click', () => {
  S.selection.categories = []; renderCategories();
});

document.addEventListener('click', e => {
  const c = e.target.closest('.chip[data-cat]');
  if (c) {
    // Toggle in place. Rebuilding the list on every click would detach the
    // node under the pointer and throw away the scroll position, which makes
    // selecting a run of categories by hand needlessly fiddly.
    const v = c.dataset.cat;
    const i = S.selection.categories.indexOf(v);
    const on = i < 0;
    if (on) S.selection.categories.push(v); else S.selection.categories.splice(i, 1);
    c.classList.toggle('on', on);
    updateCatCount();
  }
  const m = e.target.closest('.chip[data-market]');
  if (m) {
    m.classList.toggle('on');
    const v = m.dataset.market;
    const i = S.selection.markets.indexOf(v);
    if (i >= 0) S.selection.markets.splice(i, 1); else S.selection.markets.push(v);
  }
  const cl = e.target.closest('.chip[data-client] .x');
  if (cl) {
    const v = cl.parentElement.dataset.client;
    S.selection.clients = S.selection.clients.filter(x => x !== v);
    renderClients();
  }
});

function addClient(name) {
  const v = (name || '').trim();
  if (!v || S.selection.clients.includes(v)) return;
  S.selection.clients.push(v);
  renderClients();
}

function renderClients() {
  $('#c-clients').innerHTML = S.selection.clients.length
    ? S.selection.clients.map(c =>
      `<span class="chip" data-client="${esc(c)}">${esc(c)}<span class="x">×</span></span>`).join('')
    : '<span class="hint">None added — Top-N only.</span>';
}

$('#c-client-add')?.addEventListener('click', () => {
  addClient($('#c-client-input').value);
  $('#c-client-input').value = '';
});
$('#c-client-input')?.addEventListener('keydown', e => {
  if (e.key === 'Enter') { addClient(e.target.value); e.target.value = ''; }
});

// ---------------------------------------------------------------------------
// run
// ---------------------------------------------------------------------------
function topN() {
  const v = $('#c-topn').value;
  return v === 'custom' ? (Number($('#c-topn-custom').value) || 10) : Number(v);
}

function runRequest() {
  const a = spec('a'), b = spec('b');
  const metrics = buildMetricBlocks();
  const primary = metrics[0] || {};
  return {
    a, b,
    dim_col_a: S.dimCols.a,
    dim_col_b: S.dimCols.b,
    // Single-metric fields kept in sync for the legacy path; the multi-metric
    // list is what the server uses when it is non-empty.
    metric_label: metrics.map(m => m.label).join(' + '),
    a_prior: primary.a_prior || '',
    a_current: primary.a_current || '',
    b_prior: primary.b_prior || '',
    b_current: primary.b_current || '',
    is_rate: !!primary.is_rate,
    weight_metric_a: primary.weight_metric_a || '',
    weight_metric_b: primary.weight_metric_b || '',
    metrics,
    markets: S.selection.markets,
    categories: S.selection.categories,
    top_n: topN(),
    client_brands: S.selection.clients,
    mapping_a: S.mapping_a || {},
    mapping_b: S.mapping_b || {},
    category_mapping: S.categoryMapping || null,
    trend_enabled: false,
    // The market pairing the user authored. The server derives the per-side
    // scope and the baseline's B-side name from it, so the run uses exactly what
    // was configured in step 3 rather than re-deriving it.
    market_level: S.marketLevel || '',
    market_pairs: (S.marketPairs || []).filter(p => p.market_a && p.level),
    baseline_market: (S.marketPairs || []).find(p => p.level === 'total' && p.market_a)?.market_a || '',
    client_name: S.clientName || '',
    impact_name: S.impactName || '',
  };
}

async function doRun() {
  const problems = validateMetricWiring();
  if (problems.length) {
    go(5);
    alert('The metric and period selection is incomplete:\n\n· '
        + problems.join('\n· ')
        + '\n\nEvery period must be mapped on both sides before the analysis can run.');
    return;
  }
  if (!S.selection.categories.length) {
    alert('Select at least one category.'); return;
  }
  overlay(true, `Running impact analysis on ${S.selection.categories.length} category(ies)…`);
  try {
    const d = await api('/api/run', {
      method: 'POST', body: JSON.stringify(runRequest()),
    });
    S.result = d;
    renderRun();
    go(6);
  } catch (e) {
    alert('Analysis failed:\n' + e.message);
  } finally { overlay(false); }
}

function renderRun() {
  const d = S.result;
  const qc = d.qc;
  const cats = d.reports;

  const totB = cats.reduce((s, r) => s + (r.total.before_current || 0), 0);
  const totA = cats.reduce((s, r) => s + (r.total.after_current || 0), 0);
  const [scale, unit] = scaleOf(totB, totA);

  const qcBanner = {
    PASS: `<div class="notice ok">Automated QC passed — ${qc.counts.PASS} checks.</div>`,
    WARN: `<div class="notice warn">QC completed with ${qc.counts.WARN} warning(s) and ${qc.counts.FAIL} failure(s). Review before distributing.</div>`,
    FAIL: `<div class="notice err">QC found ${qc.counts.FAIL} failure(s). Fix before distributing.</div>`,
  }[qc.worst];

  // `notes` carries anything that would otherwise be a silently wrong number -
  // chiefly a degenerate period wiring, where prior and current resolve to the
  // same column and every growth rate reads 0%. The server has always sent it;
  // it was never rendered, so a warning nobody sees was doing no work.
  const notes = (d.notes || []).filter(Boolean);
  const notesBanner = notes.length
    ? `<div class="notice warn"><b>Check the period wiring</b><ul style="margin:6px 0 0 18px">${
        notes.map(n => `<li>${esc(n)}</li>`).join('')}</ul></div>`
    : '';

  $('#run-out').innerHTML = `
    ${qcBanner}
    ${notesBanner}
    <div class="kpis">
      <div class="kpi neutral"><div class="k-label">Categories analysed</div>
        <div class="k-value">${cats.length}</div>
        <div class="k-sub">${esc(d.categories.slice(0, 3).join(', '))}${cats.length > 3 ? '…' : ''}</div></div>
      <div class="kpi neutral"><div class="k-label">Total BEFORE (MAT TY)</div>
        <div class="k-value">${fmtVal(totB, scale, unit)}</div>
        <div class="k-sub">sum across selected categories</div></div>
      <div class="kpi neutral"><div class="k-label">Total AFTER (MAT TY)</div>
        <div class="k-value">${fmtVal(totA, scale, unit)}</div>
        <div class="k-sub">sum across selected categories</div></div>
      <div class="kpi ${totA >= totB ? 'good' : 'bad'}"><div class="k-label">Impact on total</div>
        <div class="k-value">${pct(totB ? (totA / totB - 1) * 100 : null)}</div>
        <div class="k-sub">updated vs previous</div></div>
      <div class="kpi ${qc.worst === 'PASS' ? 'good' : qc.worst === 'FAIL' ? 'bad' : 'neutral'}">
        <div class="k-label">QC status</div><div class="k-value">${qc.worst}</div>
        <div class="k-sub">${qc.counts.PASS} pass · ${qc.counts.WARN} warn · ${qc.counts.FAIL} fail</div></div>
    </div>

    <div class="tabs" id="run-tabs">
      ${cats.map((r, i) => `<button class="tab ${i === 0 ? 'active' : ''}" data-rt="${i}">${esc(r.category)}</button>`).join('')}
      <button class="tab" data-rt="qc">QC (${qc.worst})</button>
    </div>
    <div id="run-body"></div>`;

  const showCat = i => renderCategory(cats[i], scale, unit);
  const showQC = () => renderQC(qc);
  $$('#run-tabs .tab').forEach(t => t.onclick = () => {
    $$('#run-tabs .tab').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    if (t.dataset.rt === 'qc') showQC(); else showCat(Number(t.dataset.rt));
  });
  showCat(0);
}

function renderCategory(rep, scale, unit) {
  // A multi-metric run carries `rep.metrics` — one block per selected metric,
  // each with its own total, channel block and insights. Render one section per
  // metric so a category shows Sales Value, Volume and ND side by side rather
  // than only the primary. Legacy single-metric reports fall back to the
  // top-level fields (the server copies metric 1 there).
  const metrics = (rep.metrics && Object.keys(rep.metrics).length)
    ? Object.values(rep.metrics)
    : [{ key: rep.metric_key || 'metric', label: rep.metric || 'Metric',
         is_rate_metric: !!rep.is_rate_metric,
         growth_applicable: rep.growth_applicable !== false,
         total: rep.total, channel_block: rep.channel_block,
         channel_level_block: rep.channel_level_block,
         region_level_block: rep.region_level_block,
         market_other_block: rep.market_other_block,
         manufacturer_top_n: rep.manufacturer_top_n,
         brand_top_n: rep.brand_top_n, client_brands: rep.client_brands,
         client_manufacturers: rep.client_manufacturers,
         contributors: rep.contributors, insights: rep.insights,
         baseline: rep.baseline }];

  // The entity-level tables (rank movement, client entities, contributors) are
  // reported once per metric, since the ranking itself can differ by metric.
  $('#run-body').innerHTML = metrics.map((m, i) =>
    renderMetricSection(m, scale, unit, metrics.length > 1 ? i + 1 : 0,
      metrics.length)).join('');
}

/**
 * One metric's view of a category. When `growth_applicable` is false (ND), the
 * growth and level-shift columns are replaced by TY / YA / absolute change —
 * a distribution level has no meaningful percentage growth.
 */
function renderMetricSection(m, scale, unit, ordinal, totalMetrics) {
  const t = m.total || {};
  const g = m.growth_applicable !== false;

  const heading = totalMetrics > 1
    ? `<h3 class="metric-head">${ordinal}. ${esc(m.label)}</h3>` : '';

  return `${heading}
    <div class="kpis">
      <div class="kpi neutral"><div class="k-label">BEFORE · MAT TY</div>
        <div class="k-value">${fmtVal(t.before_current, scale, unit)}</div>
        <div class="k-sub">MAT YA ${fmtVal(t.before_prior, scale, unit)}</div></div>
      <div class="kpi neutral"><div class="k-label">AFTER · MAT TY</div>
        <div class="k-value">${fmtVal(t.after_current, scale, unit)}</div>
        <div class="k-sub">MAT YA ${fmtVal(t.after_prior, scale, unit)}</div></div>
      ${g ? `
      <div class="kpi ${cls(t.before_growth_pct) === 'neg' ? 'bad' : 'good'}">
        <div class="k-label">BEFORE growth</div>
        <div class="k-value">${pct(t.before_growth_pct)}</div>
        <div class="k-sub">MAT TY vs MAT YA</div></div>
      <div class="kpi ${cls(t.after_growth_pct) === 'neg' ? 'bad' : 'good'}">
        <div class="k-label">AFTER growth</div>
        <div class="k-value">${pct(t.after_growth_pct)}</div>
        <div class="k-sub">MAT TY vs MAT YA</div></div>
      <div class="kpi ${cls(t.level_shift_pp) === 'neg' ? 'bad' : 'good'}">
        <div class="k-label">Level shift</div>
        <div class="k-value">${pp(t.level_shift_pp)}</div>
        <div class="k-sub">after growth − before growth</div></div>` : `
      <div class="kpi neutral"><div class="k-label">Absolute change (TY − YA)</div>
        <div class="k-value">${fmtVal((t.after_current || 0) - (t.after_prior || 0), scale, unit)}</div>
        <div class="k-sub">AFTER · distribution level</div></div>
      <div class="kpi neutral"><div class="k-label">Growth</div>
        <div class="k-value">n/a</div>
        <div class="k-sub">not applicable to a distribution level</div></div>`}
      <div class="kpi ${cls(t.abs_change) === 'neg' ? 'bad' : 'good'}">
        <div class="k-label">Absolute change</div>
        <div class="k-value">${fmtVal(t.abs_change, scale, unit)}</div>
        <div class="k-sub">on MAT TY</div></div>
    </div>

    ${renderMarketBlocks(m, scale, unit, g)}
    ${renderEntities({ ...m, contributors: m.contributors }, { scale, unit, g })}
  `;
}

/**
 * The market blocks, one per level.
 *
 * The Total Market is shown once at the top of each block and is **not repeated
 * as a member row**, because the members are the levels beneath it: showing the
 * total inside its own parts double-counts it and makes every share in the block
 * wrong. A block is only drawn when the user paired at least one value at that
 * level, so a run with no regions simply has no region block.
 */
function renderMarketBlocks(m, scale, unit, g) {
  const levels = [
    ['channel', 'Market / Channel block', 'channels'],
    ['region', 'Market / Region block', 'regions'],
    ['other', 'Market block', 'markets'],
  ];
  return levels.map(([key, title, noun]) => {
    const blk = m[`${key}_level_block`]
      || (key === 'other' ? m.market_other_block : null);
    if (!blk || !(blk.members || []).length) return '';
    const tot = blk.total;
    const rows = [...(blk.members || [])];
    return `<div class="blk">
      <div class="blk-head"><h4>${esc(title)}</h4>
        <span class="sub">${esc(m.label)}${unit ? ' (' + unit + ')' : ''} · Total Market, then the ${esc(noun)} beneath it${g
          ? ' — level shift and contribution' : ' — absolute change and share'}</span></div>
      <div class="tbl-wrap"><table>
        <thead>${g ? `<tr>
            <th rowspan="2">${esc(key === 'region' ? 'Regions' : key === 'channel' ? 'Channels' : 'Markets')}</th>
            <th colspan="3" class="grp-before">BEFORE</th>
            <th colspan="3" class="grp-after">AFTER</th>
            <th colspan="3">Level Shift</th>
            <th colspan="2">Contribution · MAT TY</th>
          </tr>
          <tr>
            <th>MAT YA</th><th>MAT TY</th><th>Growth</th>
            <th>MAT YA</th><th>MAT TY</th><th>Growth</th>
            <th>MATTY</th><th>Before</th><th>After</th>
            <th>Before</th><th>After</th>
          </tr>` : `<tr>
            <th rowspan="2">${esc(key === 'region' ? 'Regions' : key === 'channel' ? 'Channels' : 'Markets')}</th>
            <th colspan="2" class="grp-before">BEFORE</th>
            <th colspan="2" class="grp-after">AFTER</th>
            <th colspan="2">Change</th>
            <th colspan="2">Contribution · MAT TY</th>
          </tr>
          <tr>
            <th>MAT YA</th><th>MAT TY</th>
            <th>MAT YA</th><th>MAT TY</th>
            <th>Abs (TY − YA)</th><th>Share</th>
            <th>Before</th><th>After</th>
          </tr>`}</thead>
        <tbody>
          ${tot ? renderMarketRow(tot, scale, unit, g, true) : ''}
          ${rows.map(c => renderMarketRow(c, scale, unit, g, false)).join('')}
        </tbody>
      </table></div>
    </div>`;
  }).join('');
}

/** One row of a market block: the Total (class `total`) or a member. */
function renderMarketRow(c, scale, unit, g, isTotal) {
  const label = isTotal ? `Total Market${c.name ? ' · ' + c.name : ''}` : c.name;
  const cls0 = isTotal ? ' class="total"' : '';
  if (g) {
    return `<tr${cls0}>
      <td>${esc(label)}</td>
      <td class="num">${fmtVal(c.before.mat_ya, scale, unit)}</td>
      <td class="num">${fmtVal(c.before.mat_ty, scale, unit)}</td>
      <td class="num ${cls(c.before.growth_pct)}">${pct(c.before.growth_pct)}</td>
      <td class="num">${fmtVal(c.after.mat_ya, scale, unit)}</td>
      <td class="num">${fmtVal(c.after.mat_ty, scale, unit)}</td>
      <td class="num ${cls(c.after.growth_pct)}">${pct(c.after.growth_pct)}</td>
      <td class="num ${cls(c.level_shift.mat_ty_pp)}">${pp(c.level_shift.mat_ty_pp)}</td>
      <td class="num">${fmtNum(c.level_shift.before_share_pct, 1)}%</td>
      <td class="num">${fmtNum(c.level_shift.after_share_pct, 1)}%</td>
      <td class="num">${fmtNum(c.contribution.before_share_pct, 1)}%</td>
      <td class="num">${fmtNum(c.contribution.after_share_pct, 1)}%</td>
    </tr>`;
  }
  return `<tr${cls0}>
    <td>${esc(label)}</td>
    <td class="num">${fmtVal(c.before.mat_ya, scale, unit)}</td>
    <td class="num">${fmtVal(c.before.mat_ty, scale, unit)}</td>
    <td class="num">${fmtVal(c.after.mat_ya, scale, unit)}</td>
    <td class="num">${fmtVal(c.after.mat_ty, scale, unit)}</td>
    <td class="num ${cls((c.after.mat_ty || 0) - (c.after.mat_ya || 0))}">${fmtVal((c.after.mat_ty || 0) - (c.after.mat_ya || 0), scale, unit)}</td>
    <td class="num">${fmtNum(c.contribution.after_share_pct, 1)}%</td>
    <td class="num">${fmtNum(c.contribution.before_share_pct, 1)}%</td>
    <td class="num">${fmtNum(c.contribution.after_share_pct, 1)}%</td>
  </tr>`;
}

/** Brand / manufacturer / client tables and the contributor grid, per metric. */
function renderEntities(m, { scale, unit, g }) {
  // The brand *value share* block was removed on request. Brand Top-N and the
  // client-brand tracker are separate tables and are unaffected - one answers
  // "who is biggest and how did they move", the other "how are our named brands
  // doing", and neither is a share-of-category presentation.
  const mt = m.manufacturer_top_n || [];
  const bt = m.brand_top_n || [];
  const rep = m;
  return `
    ${mt.length ? `
    <div class="blk">
      <div class="blk-head"><h4>Manufacturer Top-${mt.length}</h4>
        <span class="sub">the ${mt.length} largest manufacturers in the previous database, followed into the updated one</span></div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>Manufacturer</th><th>Rank BEFORE</th><th>Rank AFTER</th>
            <th>Δ Rank</th><th>BEFORE MAT TY</th><th>AFTER MAT TY</th>
            ${g ? '<th>AFTER growth</th>' : '<th>Abs change</th>'}<th>Share chg</th><th>Movement</th></tr></thead>
          <tbody>
            ${mt.map(b => `<tr class="${b.movement === 'NEW' || b.movement === 'EXITED' ? 'hi' : ''}">
              <td>${esc(b.name)}</td>
              <td class="num">${b.rank_before ?? '–'}</td>
              <td class="num">${b.rank_after ?? '–'}</td>
              <td class="num ${b.rank_change > 0 ? 'pos' : b.rank_change < 0 ? 'neg' : 'zero'}">${b.rank_change !== null && b.rank_change !== undefined ? (b.rank_change > 0 ? '+' : '') + b.rank_change : '–'}</td>
              <td class="num">${fmtVal(b.before_current, scale, unit)}</td>
              <td class="num">${fmtVal(b.after_current, scale, unit)}</td>
              ${g ? `<td class="num ${cls(b.after_growth_pct)}">${pct(b.after_growth_pct)}</td>` :
                `<td class="num ${cls((b.after_current || 0) - (b.before_current || 0))}">${fmtVal((b.after_current || 0) - (b.before_current || 0), scale, unit)}</td>`}
              <td class="num ${cls(b.share_change_pp)}">${pp(b.share_change_pp)}</td>
              <td style="text-align:left"><b>${esc(b.movement || '')}</b></td>
            </tr>`).join('')}
          </tbody>
        </table>
      </div>
    </div>` : ''}

    ${bt.length ? `
    <div class="blk">
      <div class="blk-head"><h4>Brand Top-${bt.length}</h4>
        <span class="sub">the ${bt.length} largest brands in the previous database, followed into the updated one</span></div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>Brand</th><th>Rank BEFORE</th><th>Rank AFTER</th>
            <th>Δ Rank</th><th>BEFORE MAT TY</th><th>AFTER MAT TY</th>
            ${g ? '<th>AFTER growth</th>' : '<th>Abs change</th>'}<th>Movement</th></tr></thead>
          <tbody>
            ${bt.map(b => `<tr class="${b.movement === 'NEW' || b.movement === 'EXITED' ? 'hi' : ''}">
              <td>${esc(b.name)}</td>
              <td class="num">${b.rank_before ?? '–'}</td>
              <td class="num">${b.rank_after ?? '–'}</td>
              <td class="num ${b.rank_change > 0 ? 'pos' : b.rank_change < 0 ? 'neg' : 'zero'}">${b.rank_change !== null && b.rank_change !== undefined ? (b.rank_change > 0 ? '+' : '') + b.rank_change : '–'}</td>
              <td class="num">${fmtVal(b.before_current, scale, unit)}</td>
              <td class="num">${fmtVal(b.after_current, scale, unit)}</td>
              ${g ? `<td class="num ${cls(b.after_growth_pct)}">${pct(b.after_growth_pct)}</td>` :
                `<td class="num ${cls((b.after_current || 0) - (b.before_current || 0))}">${fmtVal((b.after_current || 0) - (b.before_current || 0), scale, unit)}</td>`}
              <td style="text-align:left"><b>${esc(b.movement || '')}</b></td>
            </tr>`).join('')}
          </tbody>
        </table>
      </div>
    </div>` : ''}

    ${(rep.client_brands?.length || rep.client_manufacturers?.length) ? `
    <div class="blk">
      <div class="blk-head"><h4>Client entities tracked</h4>
        <span class="sub">reported independently of the Top-N cut</span></div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>Entity</th><th>Level</th><th>BEFORE MAT TY</th><th>AFTER MAT TY</th>
            <th>Rank BEFORE</th><th>Rank AFTER</th><th>Share chg</th><th>In Top-N</th><th>Movement</th></tr></thead>
          <tbody>
            ${[...(rep.client_brands || []).map(b => ['brand', b]),
               ...(rep.client_manufacturers || []).map(b => ['manufacturer', b])]
              .map(([lvl, b]) => b.found === false
                ? `<tr><td>${esc(b.name)}</td><td style="text-align:left">${lvl}</td>
                     <td colspan="7" style="text-align:left;color:var(--muted)">not present in either dataset</td></tr>`
                : `<tr><td>${esc(b.name)}</td><td style="text-align:left">${lvl}</td>
                     <td class="num">${fmtVal(b.before_current, scale, unit)}</td>
                     <td class="num">${fmtVal(b.after_current, scale, unit)}</td>
                     <td class="num">${b.rank_before ?? '–'}</td>
                     <td class="num">${b.rank_after ?? '–'}</td>
                     <td class="num ${cls(b.share_change_pp)}">${pp(b.share_change_pp)}</td>
                     <td style="text-align:left">${b.in_top_n ? 'Yes' : 'No'}</td>
                     <td style="text-align:left"><b>${esc(b.movement || '')}</b></td></tr>`).join('')}
          </tbody>
        </table>
      </div>
    </div>` : ''}

    ${(rep.contributors || []).length ? `
    <div class="blk">
      <div class="blk-head"><h4>What drove the change</h4>
        <span class="sub">largest absolute movers</span></div>
      <div class="grid two">
        ${rep.contributors.map(c => `
          <div>
            <h4 style="font-size:12px;margin-bottom:8px">${esc(c.level.charAt(0).toUpperCase() + c.level.slice(1))}s</h4>
            <div class="tbl-wrap"><table>
              <thead><tr><th>Entity</th><th>Dir</th><th>Abs change</th><th>Contrib.</th></tr></thead>
              <tbody>
                ${c.gainers.map(g2 => `<tr><td>${esc(g2.name)}</td>
                  <td style="text-align:left" class="pos">Gain</td>
                  <td class="num">${fmtVal(g2.abs_change, scale, unit)}</td>
                  <td class="num ${cls(g2.contribution_to_change_pct)}">${pct(g2.contribution_to_change_pct)}</td></tr>`).join('')}
                ${c.losers.map(g2 => `<tr><td>${esc(g2.name)}</td>
                  <td style="text-align:left" class="neg">Loss</td>
                  <td class="num">${fmtVal(g2.abs_change, scale, unit)}</td>
                  <td class="num ${cls(g2.contribution_to_change_pct)}">${pct(g2.contribution_to_change_pct)}</td></tr>`).join('')}
              </tbody>
            </table></div>
          </div>`).join('')}
      </div>
    </div>` : ''}
  `;
}

function renderQC(qc) {
  $('#run-body').innerHTML = `
    <div class="notice ${qc.worst === 'PASS' ? 'ok' : qc.worst === 'WARN' ? 'warn' : 'err'}">
      Overall QC status: <b>${qc.worst}</b> — ${qc.counts.PASS} passed,
      ${qc.counts.WARN} warning(s), ${qc.counts.FAIL} failure(s).
      Checks that could not be verified are reported as warnings rather than silently passed.
    </div>
    <div class="qc-list">
      ${qc.checks.map(c => `
        <div class="qc-row ${c.status}">
          <div class="qc-badge ${c.status}">${c.status}</div>
          <div>
            <div class="qc-name">${esc(c.name)}</div>
            <div class="qc-msg">${esc(c.message)}</div>
            ${Object.keys(c.detail || {}).length ? `
              <details><summary>detail</summary>
                <div class="qc-detail">${esc(JSON.stringify(c.detail, null, 1).slice(0, 2600))}</div>
              </details>` : ''}
          </div>
        </div>`).join('')}
    </div>`;
}

// ---------------------------------------------------------------------------
// export
// ---------------------------------------------------------------------------
async function doExport() {
  const req = {
    ...runRequest(),
    run_name: $('#e-name').value || 'impact_run',
    include_excel: $('#e-excel').checked,
    include_pptx: $('#e-pptx').checked,
  };
  overlay(true, `Generating ${S.selection.categories.length} × (Excel + PowerPoint)… this can take a while.`);
  try {
    const d = await api('/api/export', { method: 'POST', body: JSON.stringify(req) });
    S.exportResult = d;
    renderExport(d);
    go(7);
  } catch (e) {
    alert('Export failed:\n' + e.message);
  } finally { overlay(false); }
}

function renderExport(d) {
  const qc = d.qc;
  const byCat = {};
  d.files.forEach(f => {
    byCat[f.category] = byCat[f.category] || [];
    byCat[f.category].push(f);
  });
  const cats = Object.keys(byCat).filter(c => c !== '(run)');

  $('#export-out').innerHTML = `
    <div class="notice ${qc.worst === 'PASS' ? 'ok' : qc.worst === 'WARN' ? 'warn' : 'err'}">
      Generated in ${d.elapsed_sec}s · QC <b>${qc.worst}</b>
      (${qc.counts.PASS} pass / ${qc.counts.WARN} warn / ${qc.counts.FAIL} fail) ·
      ${d.files.length} file(s) across ${cats.length} category folder(s).
    </div>
    <div class="stat-grid">
      <div class="stat"><div class="s-label">Categories</div><div class="s-value">${cats.length}</div></div>
      <div class="stat"><div class="s-label">Excel files</div><div class="s-value">${d.files.filter(f => f.kind === 'excel').length}</div></div>
      <div class="stat"><div class="s-label">PowerPoint files</div><div class="s-value">${d.files.filter(f => f.kind === 'pptx').length}</div></div>
      <div class="stat"><div class="s-label">Output folder</div><div class="s-value" style="font-size:12px">${esc(d.rel_dir)}</div></div>
    </div>
    <div class="card">
      <div class="card-head"><h3>Run index &amp; QC summary</h3></div>
      ${(byCat['(run)'] || []).map(f => `
        <div class="file-row">
          <span class="fname">${esc(f.rel)}</span>
          <span class="fmeta">${(f.size / 1024).toFixed(0)} KB</span>
          <a class="btn small" href="/api/download?rel=${encodeURIComponent(f.rel)}">Download</a>
        </div>`).join('')}
    </div>
    <div class="card">
      <div class="card-head"><h3>Per-category outputs</h3></div>
      <div style="max-height:520px;overflow:auto">
      ${cats.map(c => `
        <div style="margin-bottom:12px">
          <div style="font-size:12.5px;font-weight:600;margin-bottom:5px">${esc(c)}</div>
          ${byCat[c].map(f => `
            <div class="file-row">
              <span class="fname">${esc(f.rel.split('/').slice(-1)[0])}</span>
              <span class="fmeta">${(f.size / 1024).toFixed(0)} KB</span>
              <a class="btn small" href="/api/download?rel=${encodeURIComponent(f.rel)}">Download</a>
            </div>`).join('')}
        </div>`).join('')}
      </div>
    </div>
    <div class="actions">
      <button class="btn ghost" id="btn-open-folder">Open output folder</button>
    </div>`;

  $('#btn-open-folder')?.addEventListener('click', () =>
    api(`/api/open-folder?rel=${encodeURIComponent(d.rel_dir)}`, { method: 'POST' })
      .catch(() => { }));
}

// ---------------------------------------------------------------------------
// wiring
// ---------------------------------------------------------------------------

// Step 1 is deliberately two actions in order: upload a file, then load the two
// datasets from it. The loader stays hidden until a source exists, so the user
// is not asked to choose a source that does not exist yet - and the profile
// button stays disabled until both sides are wired, so the wizard cannot be
// advanced into a state the next step cannot serve.
const uploadBtn = $('#btn-upload');
const fileInput = $('#file-input');
const fileChip = $('#file-chip');
const loader = $('#loader');
const dropZone = $('#drop');

function setPickedFile(file) {
  if (!file) {
    uploadBtn.disabled = true;
    fileChip.hidden = true;
    fileChip.textContent = '';
    return;
  }
  uploadBtn.disabled = false;
  fileChip.hidden = false;
  fileChip.textContent = `${file.name} · ${fmtBytes(file.size)}`;
}

function fmtBytes(n) {
  if (!Number.isFinite(n) || n <= 0) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1)} ${u[i]}`;
}

// Two ways to satisfy the same action, and both must be visible: clicking the
// zone (a <label>, so the browser opens the picker) and dropping onto it.
fileInput?.addEventListener('change', () => {
  setPickedFile(fileInput.files[0] || null);
});

if (dropZone) {
  for (const ev of ['dragenter', 'dragover']) {
    dropZone.addEventListener(ev, (e) => {
      e.preventDefault();
      dropZone.classList.add('over');
    });
  }
  for (const ev of ['dragleave', 'drop']) {
    dropZone.addEventListener(ev, (e) => {
      e.preventDefault();
      dropZone.classList.remove('over');
    });
  }
  dropZone.addEventListener('drop', (e) => {
    const f = e.dataTransfer?.files?.[0];
    if (!f) return;
    // Route the dropped file through the real input, so there is one upload
    // path and the visible selection always matches what Upload will send.
    const dt = new DataTransfer();
    dt.items.add(f);
    fileInput.files = dt.files;
    setPickedFile(f);
  });
}

uploadBtn?.addEventListener('click', async () => {
  if (!fileInput.files.length) return;
  const fd = new FormData();
  fd.append('file', fileInput.files[0]);
  overlay(true, 'Uploading…');
  try {
    const d = await api('/api/source', { method: 'POST', body: fd });
    await loadSources();
    // Point both sides at what was just uploaded: with one file in play, asking
    // the user to pick it twice is a choice with one answer.
    const sid = d?.id || (S.sources[S.sources.length - 1] || {}).id;
    if (sid) {
      for (const side of ['a', 'b']) {
        const sel = $(`#${side}-source`);
        if (sel && [...sel.options].some(o => o.value === sid)) {
          sel.value = sid;
          sel.dispatchEvent(new Event('change', { bubbles: true }));
        }
      }
    }
    dropZone?.classList.add('has-file');
    $(`#upload-msg`).textContent =
      `Loaded ${d?.filename || fileInput.files[0].name}` +
      `${d?.sheets?.length ? ` · ${d.sheets.length} sheets` : ''}`;
    fileInput.value = '';
    setPickedFile(null);
    if (loader) loader.hidden = false;
  } catch (e) { $(`#upload-msg`).textContent = 'Error: ' + e.message; }
  finally { overlay(false); syncStep1(); }
});

// The profile button is enabled only when both sides have a source, so it never
// looks ready before there is anything to profile.
function syncStep1() {
  const ok = ['a', 'b'].every(side => ($(`#${side}-source`)?.value || '') !== '');
  const btn = $('#btn-profile');
  if (btn) btn.disabled = !ok;
}

['a', 'b'].forEach(side => {
  $(`#${side}-source`)?.addEventListener('change', () => onSourceChange(side));
  $(`#${side}-sheet`)?.addEventListener('change', () => refreshColumns(side));
  $(`#${side}-header`)?.addEventListener('change', () => refreshColumns(side));
  $(`#${side}-splitcol`)?.addEventListener('change', () => onSplitChange(side));
});

$('#btn-profile')?.addEventListener('click', doProfile);
// Step 2 goes straight to the market pairing: it is the only dimension mapping
// the brief asks for.
$('#btn-map')?.addEventListener('click', () => { go(3); doMarketMapping(); });
$('#btn-catmap')?.addEventListener('click', mapAllCategories);
$('#btn-select')?.addEventListener('click', () => confirmCategoryMapping());
$('#btn-run')?.addEventListener('click', doRun);
$('#btn-export')?.addEventListener('click', () => go(7));
$('#btn-export-run')?.addEventListener('click', doExport);

$('#btn-files')?.addEventListener('click', async () => {
  overlay(true, 'Loading outputs…');
  try {
    const files = await api('/api/files');
    if (!files.length) { alert('No outputs generated yet.'); return; }
    alert('Outputs:\n\n' + files.slice(0, 40)
      .map(f => `${f.rel}  (${(f.size / 1024).toFixed(0)} KB)`).join('\n'));
  } finally { overlay(false); }
});

// boot
(async function boot() {
  try {
    const h = await api('/api/health');
    $('#health').textContent = `engine ok · calamine ${h.calamine ? 'on' : 'off'} · fuzzy ${h.rapidfuzz ? 'on' : 'off'}`;
    $('#health').className = 'pill ok';
  } catch {
    $('#health').textContent = 'backend unreachable';
    $('#health').className = 'pill bad';
  }
  await loadSources();
  go(1);
})();
