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
  catmap: null,                     // enumerated category units, no targets
  catmapEdits: {},                  // idx -> {canonical, targets, status}
  catmapTouched: false,
  // View state for the category-mapping table. This has to outlive a re-render:
  // editing a target used to rebuild the whole panel, which reset the filter and
  // the "Show all" checkbox, so the row list appeared to collapse mid-edit.
  catmapView: { showAll: false, search: '' },
  // Focused category editor: which row is open. Also has to outlive a render,
  // for the same reason as catmapView above. `null` means every accordion row is
  // collapsed; on first arrival the step opens the first row that still needs a
  // decision (see drawCatmapAccordion), and `catmapOpened` stops it re-opening
  // one the user has deliberately closed.
  catmapPick: null,
  catmapOpened: false,
  catmapOnlyOne: true,
  // Accordion view state for the one-at-a-time editor: the filter box and the
  // open row. Kept in state so an edit does not reset what the user typed.
  catmapAcc: { search: '' },
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
// function declaration), so the headless probes could reach `drawCatmapAccordion`
// but not the state it reads. Expose it explicitly.
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

async function refreshColumns(side) {
  const sid = $(`#${side}-source`).value;
  if (!sid) return;
  const sheet = $(`#${side}-sheet`).value || null;
  const hr = Number($(`#${side}-header`).value) || 1;
  overlay(true, 'Reading columns…');
  try {
    const q = new URLSearchParams();
    if (sheet) q.set('sheet', sheet);
    q.set('header_row', hr);
    const d = await api(`/api/source/${sid}/columns?${q}`);
    const splitSel = $(`#${side}-splitcol`);
    const cur = splitSel.value;
    splitSel.innerHTML = '<option value="">— none —</option>' +
      d.columns.map(c => `<option>${esc(c)}</option>`).join('');
    if (cur && d.columns.includes(cur)) splitSel.value = cur;
    $(`#${side}-summary`).innerHTML =
      `<b>Rows:</b> ${fmtNum(d.rows)}\n<b>Columns:</b> ${d.columns.length}\n` +
      `<b>First cols:</b> ${d.columns.slice(0, 6).map(esc).join(', ')}`;
  } catch (e) {
    $(`#${side}-summary`).textContent = 'Error: ' + e.message;
  } finally { overlay(false); }
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
  const d = await api(`/api/source/${sid}/values?${q}`);
  $(`#${side}-value`).innerHTML = d.values.map(v => `<option>${esc(v)}</option>`).join('');
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

async function doMarketMapping() {
  const a = spec('a'), b = spec('b');
  if (!a || !b) return;
  const mcol = S.dimCols.a.market || '';
  if (!mcol) {
    S.marketAdvisory = null;
    renderMarketPairing();
    return;
  }
  overlay(true, 'Reading the market values on both sides…');
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
    renderMarketPairing();
  } catch (e) {
    const box = $('#map-out');
    if (box) box.innerHTML = `<div class="card"><div class="notice warn">
      The market values could not be read: ${esc(e.message)}</div></div>`;
  } finally { overlay(false); }
}

/** Add an empty pairing; the user fills in both sides. */
function addMarketPair() {
  if (!S.marketPairs) S.marketPairs = [];
  S.marketPairs.push({ market_a: '', market_b: '', level: S.marketLevel || 'total' });
  renderMarketPairing();
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
          const ev = p.evidence || {};
          const note = ev.contradiction ? `<span class="neg">${esc(ev.contradiction)}</span>`
            : ev.note ? esc(ev.note)
            : ev.child_of ? `sits under ${esc(Object.values(ev.child_of).flat().slice(0, 2).join(', '))}`
            : '';
          return `<tr>
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
            <td class="hint" style="text-align:left;font-size:11px">${note}</td>
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
      doMarketMapping();
    };
  });
  $$('#map-out select[data-mkt-b]').forEach(sel => {
    sel.onchange = () => {
      const i = Number(sel.dataset.mktB);
      S.marketPairs[i] = { ...S.marketPairs[i], market_b: sel.value, evidence: null };
      doMarketMapping();
    };
  });
  $$('#map-out select[data-mkt-level]').forEach(sel => {
    sel.onchange = () => {
      const i = Number(sel.dataset.mktLevel);
      S.marketPairs[i] = { ...S.marketPairs[i], level: sel.value, evidence: null };
      doMarketMapping();
    };
  });
  $$('#map-out [data-mkt-del]').forEach(btn => {
    btn.onclick = () => {
      S.marketPairs.splice(Number(btn.dataset.mktDel), 1);
      doMarketMapping();
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

/** Pick a sensible metric column on one side, for the values shown as evidence. */
function pickMetricCol(side, preferFamily) {
  const fams = S.profile?.[side]?.metric_families || {};
  let fam = preferFamily && fams[preferFamily] ? preferFamily
    : Object.keys(fams).find(f => /sales value/i.test(f))
    || Object.keys(fams)[0];
  if (!fam) return '';
  const variants = fams[fam] || {};
  return variants.VALUE || variants.TY || Object.values(variants)[0] || '';
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
    // A re-probe produces a fresh set of rows, so the focused row from the old
    // set no longer means anything: start collapsed and let the editor re-open
    // its entry row, rather than leaving a stale index pointing elsewhere.
    S.catmapPick = null;
    S.catmapOpened = false;
    if (!S.catmapTouched) S.catmapEdits = {};
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
 * First press reads the structure and opens the editor. If the user has already
 * authored a mapping, it goes straight to the analysis - they are not asked to
 * map the same thing twice. Any category they left unresolved is named before
 * the run, so a category disappearing from the numbers is never a surprise.
 */
async function mapAllCategories() {
  if (!S.catmap) { await doCategoryMapping(); return; }
  const unresolved = unresolvedCategories();
  const mapped = S.catmap.rows.filter((_, i) => catmapRow(i).status === 'mapped').length;
  if (!mapped) {
    alert('No category is mapped yet.\n\n'
        + 'Attach at least one target to a Dataset-1 category before running the '
        + 'analysis, so there is something to compare.');
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

/** Categories the user has neither mapped nor excluded. */
function unresolvedCategories() {
  const rows = S.catmap?.rows || [];
  return rows.map((_, i) => catmapRow(i))
    .filter(r => r.status !== 'mapped' && r.status !== 'excluded')
    .map(r => r.source + (r.source_sub ? ' / ' + r.source_sub : ''));
}

function catmapRow(idx) {
  const base = S.catmap.rows[idx];
  const ed = S.catmapEdits[idx] || {};
  // Every row starts with no target: the enumeration assigns none, and nothing
  // here fills one in. `canonical` defaults to the category's own name so the
  // analysis has something to report it under, but the row stays `unmapped`
  // until the user attaches a target, and an unmapped row is not analysed.
  return {
    source: base.source,
    source_sub: base.source_sub || '',
    canonical: ed.canonical !== undefined
      ? ed.canonical
      : (base.canonical || base.source),
    targets: ed.targets !== undefined
      ? ed.targets
      : (base.targets || []).map(t => ({ ...t })),
    status: ed.status !== undefined ? ed.status : base.status,
    method: base.method,
    // Evidence, not a score. `hint` carries the closest name and the closest
    // value so the user has something to look at; neither is applied.
    hint: base.hint || {},
    note: base.note || '',
    a_total: base.a_total,
    b_total: base.b_total,
  };
}

function cmSet(idx, patch) {
  S.catmapEdits[idx] = { ...(S.catmapEdits[idx] || {}), ...patch };
  S.catmapTouched = true;
}

/**
 * Whether the user has dealt with a row — mapped it or excluded it.
 *
 * This used to be `status === 'accepted'`, which meant "the app matched this
 * confidently". Nothing is matched confidently any more; a row is resolved
 * because the user made a call, and a row they have not dealt with is one the
 * analysis will leave out. The distinction matters: "needs review" now means
 * "you have not decided", not "the app is unsure".
 */
function isResolved(idx) {
  const st = catmapRow(idx).status;
  return st === 'mapped' || st === 'excluded';
}

function renderCatmap() {
  const cm = S.catmap;
  const s = cm.summary || {};
  const [scale, unit] = scaleOf(...cm.rows.map(r => r.a_total).filter(v => v),
                              ...cm.rows.map(r => r.b_total).filter(v => v));

  const bCats = Array.from(new Set((cm.b_units || []).map(u => u.category))).sort();
  const subsFor = (cat) => Array.from(new Set(
    (cm.b_units || []).filter(u => u.category === cat && u.subcategory)
      .map(u => u.subcategory))).sort();
  const allSubs = Array.from(new Set(
    (cm.b_units || []).map(u => u.subcategory).filter(Boolean))).sort();
  S._cmBCats = bCats;
  S._cmSubsFor = subsFor;
  S._cmAllSubs = allSubs;

  const rows = cm.rows;
  const attention = rows.map((r, i) => i)
    .filter(i => !isResolved(i));

  // If the panel shell already exists, do NOT rebuild it. Rebuilding replaced
  // the search box and the "Show all" checkbox, throwing away whatever the user
  // had typed or ticked - so editing one target reset the view and the list
  // looked like it had collapsed. Redraw only the body, and only if asked.
  if ($('#catmap-out') && $('#cm-tbl')) {
    drawCatmapRows();
    drawCatmapAccordion();
    return;
  }

  $('#catmap-out').innerHTML = `
    <div class="card">
      <div class="card-head"><h3>How the two category structures line up</h3></div>
      <p class="hint" style="margin:0 0 8px">
        Nothing here is matched for you. Each Dataset-1 category starts with no
        counterpart; you decide what it corresponds to. A category may map to one
        category in the other dataset, to several, to a category plus a
        subcategory, or to nothing at all — and several of yours may merge into one.
        A category you leave unmapped is <em>not</em> analysed: it has no counterpart
        to compare against, so it cannot produce an impact figure.
      </p>
      <div class="stat-grid">
        <div class="stat"><div class="s-label">Categories in A</div>
          <div class="s-value">${fmtNum(s.n_a)}</div></div>
        <div class="stat"><div class="s-label">Category units in B</div>
          <div class="s-value">${fmtNum(s.n_b)}</div></div>
        <div class="stat"><div class="s-label">Mapped by you</div>
          <div class="s-value">${fmtNum(s.mapped)}</div></div>
        <div class="stat"><div class="s-label">Not mapped yet</div>
          <div class="s-value ${attention.length ? 'neg' : ''}">${fmtNum(s.unmapped)}</div></div>
        <div class="stat"><div class="s-label">Excluded</div>
          <div class="s-value">${fmtNum(s.excluded)}</div></div>
        <div class="stat"><div class="s-label">New in B</div>
          <div class="s-value">${fmtNum(s.new_in_b)}</div></div>
        <div class="stat"><div class="s-label">A value covered</div>
          <div class="s-value">${fmtNum(s.a_coverage_pct, 1)}%</div></div>
        <div class="stat"><div class="s-label">B value covered</div>
          <div class="s-value">${fmtNum(s.b_coverage_pct, 1)}%</div></div>
      </div>
      ${s.composite || s.one_to_many || s.n_to_one ? `<div class="notice info">
        ${s.composite ? `${s.composite} composite mapping(s) (a category here = category + subcategory there). ` : ''}
        ${s.one_to_many ? `${s.one_to_many} one-to-many mapping(s) (one category here = several there). ` : ''}
        ${s.n_to_one ? `${s.n_to_one} Dataset-2 target(s) shared by several Dataset-1 categories — those merge.` : ''}
      </div>` : ''}
      <div class="row" style="margin-top:12px">
        <div class="field" style="margin:0;min-width:220px">
          <label>Metric whose values are shown beside each category</label>
          <select id="cm-metric">
            ${Array.from(new Set([
              ...Object.keys(S.profile.a.metric_families || {}),
              ...Object.keys(S.profile.b.metric_families || {}),
            ])).map(f => `<option ${S.catmapMetric === f ? 'selected' : ''}>${esc(f)}</option>`).join('')}
          </select>
        </div>
        <button class="btn small" id="cm-reprobe">Re-read the categories</button>
      </div>
      <p class="hint">
        The values are shown so you can judge a pairing for yourself. Alongside
        each category you will see the closest name in the other dataset and the
        closest value — both are <em>hints only</em>. Neither selects a target, and
        a loose value match is not evidence of equivalence: the whole point of the
        study is that the values moved.
      </p>
    </div>

    <div class="card">
      <div class="card-head"><h3>Map one category at a time</h3>
        <span class="sub">expand a category to map it; only one is open at a time</span></div>
      <div class="row">
        <label class="check"><input type="checkbox" id="cm-onlyone"
          ${S.catmapOnlyOne ? 'checked' : ''}> Work one category at a time</label>
        <button class="btn small" id="cm-prev">← Previous</button>
        <button class="btn small" id="cm-next">Next needing review →</button>
        ${attention.length
          ? `<button class="btn small" id="cm-open-next">Open next needing review (${attention.length})</button>`
          : ''}
        <span class="spacer"></span>
        <label class="check"><input type="checkbox" id="cm-hideok"
          ${S.catmapAcc.hideOk ? 'checked' : ''}> Hide confident matches</label>
        <input type="search" id="cm-acc-search" placeholder="filter categories…"
               value="${esc(S.catmapAcc.search || '')}" style="max-width:220px">
      </div>
      <p class="hint" style="margin:6px 0 0">
        Each category is a row. Click the arrow (or the name) to <em>expand</em> it and
        map it on its own — the list shows all ${rows.length} categories and only the
        expanded one reveals its targets, so you never lose your place.
      </p>
      <div id="cm-acc" class="accordion"></div>
    </div>

    <div class="card" id="cm-table-card">
      <div class="card-head"><h3>All categories at once</h3></div>
      <div class="row">
        <label class="check"><input type="checkbox" id="cm-showall">
          Show all ${rows.length} rows (including confident matches)</label>
        <input type="search" id="cm-search" placeholder="filter…" style="max-width:240px">
      </div>
      <div class="tbl-wrap" style="max-height:600px;overflow:auto">
        <table id="cm-tbl">
          <thead><tr>
            <th style="width:19%">Dataset 1 (previous)</th>
            <th style="width:36%">Dataset 2 (updated) — one or more targets</th>
            <th style="width:13%">Analysis name</th>
            <th>Status</th><th>Method</th><th>Evidence</th>
          </tr></thead>
          <tbody></tbody>
        </table>
      </div>
      <p class="hint" style="margin-top:8px">
        Add a target to make one Dataset-1 category cover several Dataset-2 entries.
        Give two rows the <em>same analysis name</em> to merge them into one reported
        category. Set a row's status to <em>excluded</em> to leave it out entirely.
        A row left <em>unmapped</em> is not analysed at all.
      </p>
    </div>

    ${(cm.new_in_b || []).length ? `
    <div class="card">
      <div class="card-head"><h3>Present only in Dataset 2 (${cm.new_in_b.length})</h3></div>
      <p class="hint">These have no counterpart in the previous dataset, so by
         default they are listed here and left out of the analysis — a category with
         no before-value cannot produce an impact figure. Attach one to a Dataset-1
         category above to bring it in as part of that category.</p>
      <div class="chips">
        ${cm.new_in_b.slice(0, 200).map(u =>
          `<span class="chip" data-newb="${esc(ukeyLabel(u))}">${esc(ukeyLabel(u))}</span>`).join('')}
      </div>
      ${cm.new_in_b.length > 200 ? `<p class="hint">…and ${cm.new_in_b.length - 200} more.</p>` : ''}
      <label class="check" style="margin-top:10px">
        <input type="checkbox" id="cm-inc-newb" ${S.catmapIncludeNewInB ? 'checked' : ''}>
        Report them as new categories in their own right (they will show no
        previous value)
      </label>
    </div>` : ''}`;

  const tbody = $('#cm-tbl tbody');
  const showAll = $('#cm-showall');
  const search = $('#cm-search');

  // restore whatever the user had set before this render
  showAll.checked = !!S.catmapView.showAll;
  search.value = S.catmapView.search || '';

  showAll.onchange = () => { S.catmapView.showAll = showAll.checked; drawCatmapRows(); };
  search.oninput = () => { S.catmapView.search = search.value; drawCatmapRows(); };

  // Must not be a re-declared closure: drawCatmapRows() is called again after an
  // edit, when this function body is not running.
  drawCatmapRows();

  // Accordion, one category at a time. The full table stays available behind the
  // toggle, but 150-odd rows at once is not how anyone confirms a mapping.
  const onlyOne = $('#cm-onlyone');
  if (onlyOne) onlyOne.onchange = () => {
    S.catmapOnlyOne = onlyOne.checked;
    applyCatmapLayout();
  };
  const prev = $('#cm-prev'), next = $('#cm-next');
  if (prev) prev.onclick = () => stepCatmapPick(-1);
  if (next) next.onclick = () => stepCatmapPick(+1);
  const openNext = $('#cm-open-next');
  if (openNext) openNext.onclick = () => stepCatmapPick(+1);

  const hideOk = $('#cm-hideok');
  if (hideOk) hideOk.onchange = () => {
    S.catmapAcc.hideOk = hideOk.checked;
    drawCatmapAccordion();
  };
  const accSearch = $('#cm-acc-search');
  if (accSearch) accSearch.oninput = () => {
    S.catmapAcc.search = accSearch.value;
    drawCatmapRowsAcc();
  };

  applyCatmapLayout();
  drawCatmapAccordion();

  const incNewB = $('#cm-inc-newb');
  if (incNewB) incNewB.onchange = () => {
    S.catmapIncludeNewInB = incNewB.checked;
  };

  $('#cm-reprobe')?.addEventListener('click', () => doCategoryMapping($('#cm-metric').value));
}

/** Show the accordion or the full table, according to the toggle. */
function applyCatmapLayout() {
  const card = $('#cm-table-card');
  if (card) card.style.display = S.catmapOnlyOne ? 'none' : '';
  const tgl = $('#cm-onlyone');
  if (tgl) tgl.checked = !!S.catmapOnlyOne;
}

/**
 * Move the open accordion row to the next/previous row that still needs a call.
 *
 * When every category is already accepted there is nowhere to step to, so the
 * buttons stay put rather than silently jumping. Use the filter or "Hide
 * confident matches" to work through the ones that do need review.
 */
function stepCatmapPick(dir) {
  const rows = S.catmap?.rows || [];
  if (!rows.length) return;
  const openable = rows.map((_, i) => i)
    .filter(i => !S.catmapAcc.hideOk || !isResolved(i));
  const pool = openable.length ? openable : rows.map((_, i) => i);
  const cur = S.catmapPick;
  if (dir > 0) {
    // Prefer the next row that still needs review; fall back to the next row.
    const pending = pool.filter(i => !isResolved(i));
    let nxt = null;
    if (pending.length) {
      const afterCur = pending.filter(i => cur === null || i > cur);
      nxt = afterCur.length ? afterCur[0] : pending[0];
    } else {
      const afterCur = pool.filter(i => cur === null || i > cur);
      nxt = afterCur.length ? afterCur[0] : pool[0];
    }
    S.catmapPick = nxt;
  } else {
    const before = pool.filter(i => cur !== null && i < cur);
    S.catmapPick = before.length ? before[before.length - 1] : pool[pool.length - 1];
  }
  drawCatmapAccordion();
  const el = $(`#cm-acc-item-${S.catmapPick}`);
  if (el) el.scrollIntoView({ block: 'nearest' });
}

/** Toggle a row open/closed in the accordion. Only one is ever open. */
function toggleCatmapPick(idx) {
  S.catmapPick = (S.catmapPick === idx) ? null : idx;
  drawCatmapAccordion();
}

/**
 * The one-category-at-a-time editor, as an accordion.
 *
 * Every Dataset-1 category is a row. Clicking the arrow (or the row header)
 * expands exactly that row and collapses the others, so the user maps one
 * category at a time without the list disappearing underneath them. The body of
 * an expanded row is built once and only rebuilt when the row it belongs to
 * actually changes, so a half-typed analysis name or a target dropdown survives.
 */
function drawCatmapAccordion() {
  const box = $('#cm-acc');
  if (!box) return;
  const cm = S.catmap;
  const rows = cm?.rows || [];
  if (!rows.length) {
    box.innerHTML = '<p class="hint">No categories to map.</p>';
    return;
  }
  if (S.catmapPick !== null && (S.catmapPick < 0 || S.catmapPick >= rows.length)) {
    S.catmapPick = null;
  }
  // Arriving at the step leaves every row collapsed, which shows headers but no
  // controls and leaves Previous / Next with nothing to move from. Open the
  // first row that still needs a decision so the section demonstrates itself.
  // Done once (S.catmapOpened), so a deliberate collapse stays collapsed when
  // the user comes back to this panel.
  if (S.catmapPick === null && !S.catmapOpened && rows.length) {
    const first = rows.findIndex((_, i) => !isResolved(i));
    S.catmapPick = first >= 0 ? first : 0;
    S.catmapOpened = true;
  }
  const pending = rows.filter((_, i) => !isResolved(i)).length;

  // Build the shell once; only the header list is repainted from here.
  if (!$('#cm-acc-list')) {
    box.innerHTML = `
      <div class="acc-sum">
        <span><b>${rows.length}</b> categories</span>
        <span class="${pending ? 'neg' : ''}"><b>${pending}</b> still to review</span>
        <span><b>${rows.length - pending}</b> confident</span>
      </div>
      <div id="cm-acc-list"></div>`;
  }
  drawCatmapRowsAcc();
}

/**
 * Repaint the accordion rows. Only the header of each row is regenerated; the
 * body of the open row is built (or left) separately, which is what preserves
 * focus and typed input while a target is being edited.
 */
function drawCatmapRowsAcc() {
  const box = $('#cm-acc-list');
  if (!box) return;
  const cm = S.catmap;
  const rows = cm?.rows || [];
  const q = (S.catmapAcc.search || '').toLowerCase();
  const hideOk = !!S.catmapAcc.hideOk;
  const [scale, unit] = scaleOf(...rows.map(r => r.a_total).filter(v => v),
                               ...rows.map(r => r.b_total).filter(v => v));

  const openIdx = S.catmapPick;
  const openEl = openIdx !== null ? $(`#cm-acc-item-${openIdx}`) : null;
  // If a row is open and its shell is still on screen, patch it in place and
  // leave its body alone — rebuilding it would destroy the control in use.
  if (openEl && openEl.parentElement === box && !openEl.dataset.needsRebuild) {
    const head = openEl.querySelector('.acc-head');
    const r = catmapRow(openIdx);
    if (head) head.outerHTML = accHeadHtml(openIdx, r, scale, unit, true);
    bindAccHeads(box);
    // A status change from elsewhere can flip "needs review", so refresh the
    // count tile without touching the bodies.
    const sum = $('#cm-acc > .acc-sum');
    if (sum) {
      const p = rows.filter((_, i) => !isResolved(i)).length;
      sum.innerHTML = `<span><b>${rows.length}</b> categories</span>
        <span class="${p ? 'neg' : ''}"><b>${p}</b> still to review</span>
        <span><b>${rows.length - p}</b> confident</span>`;
    }
    return;
  }

  const visible = [];
  rows.forEach((_, i) => {
    const st = catmapRow(i);
    if (hideOk && st.status === 'mapped') return;
    if (q && !(`${st.source} ${st.source_sub || ''} ${st.canonical} `
      + `${st.targets.map(t => t.category + ' ' + (t.subcategory || '')).join(' ')}`)
      .toLowerCase().includes(q)) return;
    visible.push(i);
  });

  if (!visible.length) {
    box.innerHTML = `<p class="hint" style="padding:10px">${
      q ? 'No category matches the filter.' : 'Nothing to show — every category is a confident match.'}</p>`;
    return;
  }

  box.innerHTML = visible.map(i => {
    const st = catmapRow(i);
    const open = i === openIdx;
    return `<div class="acc-item ${open ? 'open' : ''}" id="cm-acc-item-${i}">
      ${accHeadHtml(i, st, scale, unit, open)}
      ${open ? `<div class="acc-body" id="cm-acc-body-${i}">${accBodyHtml(i, st, scale, unit)}</div>` : ''}
    </div>`;
  }).join('');

  bindAccHeads(box);

  // Bind the body of the open row, if one is rendered.
  if (openIdx !== null && visible.includes(openIdx)) {
    bindAccBody(openIdx, catmapRow(openIdx));
  }
}

/** The clickable header of an accordion row: arrow, name, values, status flag. */
function accHeadHtml(idx, st, scale, unit, open) {
  // A row's flag now says what the *user* has done with it, not what the app
  // thinks of it. "not mapped" is "you have not decided"; "excluded" is a
  // deliberate call.
  const flag = st.status === 'mapped' ? { c: 'ok', t: 'mapped' }
    : st.status === 'excluded' ? { c: 'muted', t: 'excluded' }
      : { c: 'warn', t: 'not mapped' };
  const nt = st.targets.length;
  return `<button type="button" class="acc-head" data-acc="${idx}"
      aria-expanded="${open ? 'true' : 'false'}">
    <span class="acc-arrow">${open ? '▾' : '▸'}</span>
    <span class="acc-name">${esc(st.source)}${st.source_sub
      ? ` <span class="hint">/ ${esc(st.source_sub)}</span>` : ''}</span>
    <span class="acc-equiv">${nt
      ? `→ ${nt} entr${nt === 1 ? 'y' : 'ies'}${st.canonical && st.canonical !== st.source
          ? ` · reported as <em>${esc(st.canonical)}</em>` : ''}`
      : '<em>nothing attached</em>'}</span>
    <span class="acc-vals">${st.a_total != null ? `A ${fmtVal(st.a_total, scale, unit)}` : ''}${st.b_total != null
      ? ` · B ${fmtVal(st.b_total, scale, unit)}` : ''}</span>
    <span class="tag ${flag.c}">${flag.t}</span>
  </button>`;
}

/**
 * The evidence panel: the closest name and the closest value, side by side.
 *
 * Both are observations, not decisions. Each carries a button that applies it,
 * because making the user retype a name the app already found would be silly —
 * but the button is the only path from a hint into the mapping, so nothing is
 * ever applied on the user's behalf.
 */
function hintHtml(r) {
  const h = r.hint || {};
  const bits = [];
  if (h.closest_name) {
    const n = h.closest_name;
    bits.push(`<div class="hint-line">
      <span class="hint">Closest name in the updated dataset:</span>
      <strong>${esc(n.label || n.category)}</strong>
      <span class="hint">(${fmtNum(n.similarity_pct, 0)}% alike)</span>
      <button class="btn small ghost" data-cm-hint="name"
              data-cm-hint-cat="${esc(n.category)}"
              data-cm-hint-sub="${esc(n.subcategory || '')}">use this</button>
    </div>`);
  }
  if (h.closest_value) {
    const v = h.closest_value;
    const d = v.delta_pct;
    bits.push(`<div class="hint-line">
      <span class="hint">Closest value:</span>
      <strong>${esc(v.label || v.category)}</strong>
      <span class="hint ${d == null ? '' : (d < 0 ? 'neg' : '')}">(${d == null ? 'n/a' : (d > 0 ? '+' : '') + fmtNum(d, 1) + '%'})</span>
      <button class="btn small ghost" data-cm-hint="value"
              data-cm-hint-cat="${esc(v.category)}"
              data-cm-hint-sub="${esc(v.subcategory || '')}">use this</button>
    </div>`);
  }
  if (!bits.length) {
    return `<div class="hint" style="margin:0 0 6px">
      No close name or value in the other dataset — this category may have no
      counterpart there.</div>`;
  }
  return `<div class="cm-hints" style="margin:0 0 6px">
    ${bits.join('')}
    <div class="hint" style="font-size:11px;margin-top:2px">
      These are hints only. A close value is not proof of equivalence — the two
      datasets differ precisely because values moved.
    </div>
  </div>`;
}

function bindHintButtons(idx, r) {
  $$('#cm-acc-body-' + idx + ' [data-cm-hint]').forEach(btn => {
    btn.onclick = () => {
      const cat = btn.dataset.cmHintCat, sub = btn.dataset.cmHintSub || '';
      const targets = r.targets.map(t => ({ ...t }));
      if (targets.some(t => t.category === cat && (t.subcategory || '') === sub)) return;
      targets.push({ category: cat, subcategory: sub, total: null });
      cmSet(idx, { targets, status: 'mapped' });
      redrawOpenAccBody(idx);
      drawCatmapRows();
    };
  });
}

function bindAccHeads(root) {
  $$('.acc-head', root).forEach(btn => {
    btn.onclick = () => toggleCatmapPick(Number(btn.dataset.acc));
  });
}

/** The body of an expanded row: value, analysis name, targets, status, notes. */
function accBodyHtml(idx, r, scale, unit) {
  const base = S.catmap.rows[idx];
  const num = (v) => (v === null || v === undefined)
    ? '—' : fmtNum(v / scale, 2) + (unit ? ' ' + unit : '');
  return `
    <div class="acc-body-grid">
      <div class="field" style="margin:0">
        <label>Reported as <span class="hint">(same name merges two categories)</span></label>
        <input id="cm-canon" value="${esc(r.canonical)}" style="width:100%"
               placeholder="the analysis name for this category">
      </div>
      <div class="field" style="margin:0;max-width:170px">
        <label>Status</label>
        <select id="cm-status">
          ${['unmapped', 'mapped', 'excluded'].map(v =>
            `<option value="${v}" ${r.status === v ? 'selected' : ''}>${v}</option>`).join('')}
        </select>
      </div>
      <div class="acc-vals-box">
        <div class="hint">Previous value</div>
        <div class="num">${num(r.a_total)}</div>
        <div class="hint" style="margin-top:6px">Updated value</div>
        <div class="num">${num(r.b_total)}</div>
      </div>
    </div>

    <div class="hint" style="margin:8px 0 4px">
      ${r.targets.length
        ? `You have attached ${r.targets.length} entr${r.targets.length === 1 ? 'y' : 'ies'}
           in the updated dataset.`
        : `Nothing in the updated dataset is attached yet.`}
      ${base.method ? ` <em>(${esc(base.method)})</em>` : ''}
    </div>
    ${hintHtml(r)}
    <div id="cm-targets"></div>
    <div class="row" style="margin-top:8px;align-items:flex-end">
      <div class="field" style="margin:0;min-width:190px;flex:1">
        <label>Add another target for this category</label>
        <select id="cm-add-cat"><option value="">— choose a category —</option>
          ${(S._cmBCats || []).map(c => `<option>${esc(c)}</option>`).join('')}</select>
      </div>
      <div class="field" style="margin:0;min-width:150px">
        <label>Subcategory</label>
        <select id="cm-add-sub"><option value="">(whole category)</option></select>
      </div>
      <button class="btn small" id="cm-add">+ Add target</button>
    </div>
    ${r.note ? `<p class="hint" style="margin-top:8px">${esc(r.note)}</p>` : ''}`;
}

/** Wire the controls inside one expanded row. */
function bindAccBody(idx, r) {
  drawCatmapTargets(idx, r);
  bindHintButtons(idx, r);

  const statusSel = $('#cm-status');
  if (statusSel) statusSel.onchange = (e) => {
    cmSet(idx, { status: e.target.value });
    // A status change alters the flag in the header and the pending count, but
    // not which rows are listed - so repaint headers only, keeping this body.
    drawCatmapRowsAcc();
    drawCatmapRows();
  };
  const canon = $('#cm-canon');
  if (canon) canon.oninput = (e) => cmSet(idx, { canonical: e.target.value });

  const addCat = $('#cm-add-cat'), addSub = $('#cm-add-sub');
  if (addCat) addCat.onchange = () => {
    const subs = (S._cmSubsFor || (() => []))(addCat.value);
    const list = subs.length ? subs : (S._cmAllSubs || []);
    addSub.innerHTML = '<option value="">(whole category)</option>'
      + list.map(x => `<option>${esc(x)}</option>`).join('');
  };
  const addBtn = $('#cm-add');
  if (addBtn) addBtn.onclick = () => {
    if (!addCat.value) return;
    const targets = r.targets.map(t => ({ ...t }));
    const sub = addSub.value || '';
    if (targets.some(t => t.category === addCat.value && (t.subcategory || '') === sub)) return;
    targets.push({ category: addCat.value, subcategory: sub, total: null });
    cmSet(idx, { targets, status: r.status === 'unmapped' ? 'mapped' : r.status });
    // The body genuinely changed (a new target was added), so rebuild just this
    // row's body - the headers and every other row are left alone.
    redrawOpenAccBody(idx);
    drawCatmapRows();
  };
}

/**
 * Rebuild the body of the currently-open accordion row and rebind it, without
 * repainting the header list. Used when the row's own content changes.
 */
function redrawOpenAccBody(idx) {
  const item = $(`#cm-acc-item-${idx}`);
  if (!item) { drawCatmapAccordion(); return; }
  const st = catmapRow(idx);
  const head = item.querySelector('.acc-head');
  const [scale, unit] = scaleOf(st.a_total, st.b_total);
  if (head) head.outerHTML = accHeadHtml(idx, st, scale, unit, true);
  let body = item.querySelector('.acc-body');
  const html = accBodyHtml(idx, st, scale, unit);
  if (body) body.innerHTML = html;
  else {
    const div = document.createElement('div');
    div.className = 'acc-body';
    div.id = `cm-acc-body-${idx}`;
    div.innerHTML = html;
    item.appendChild(div);
  }
  bindAccHeads(item);
  bindAccBody(idx, st);
}

/** The target list inside the expanded editor, each with a remove control. */
function drawCatmapTargets(idx, r) {
  const box = $('#cm-targets');
  if (!box) return;
  if (!r.targets.length) {
    box.innerHTML = `<div class="notice warn" style="margin:0">
      Nothing in the updated dataset is attached to this category yet. Add a target
      to map it, or set the status to <em>excluded</em> to leave it out.</div>`;
    return;
  }
  const [scale, unit] = scaleOf(...r.targets.map(t => t.total).filter(v => v));
  box.innerHTML = r.targets.map((t, i) => `
    <div class="row" style="align-items:center;gap:8px;padding:4px 0">
      <span class="chip">${esc(t.category)}${t.subcategory ? ' / ' + esc(t.subcategory) : ''}</span>
      <span class="hint">${t.total === null || t.total === undefined
        ? '' : fmtNum(t.total / scale, 2) + (unit ? ' ' + unit : '')}</span>
      <button class="btn small ghost" data-rm="${i}">×</button>
    </div>`).join('');
  box.querySelectorAll('[data-rm]').forEach(btn => {
    btn.onclick = () => {
      const targets = r.targets.map(t => ({ ...t }));
      targets.splice(Number(btn.dataset.rm), 1);
      cmSet(idx, { targets });
      // Removing a target changes only this row's body, so rebuild just that
      // row rather than the whole accordion (which would close it on the user).
      redrawOpenAccBody(idx);
      drawCatmapRows();
    };
  });
}

/** Redraw only the category-mapping table body, leaving the controls alone. */
function drawCatmapRows() {
  const cm = S.catmap;
  if (!cm || !$('#cm-tbl')) return;
  const tbody = $('#cm-tbl tbody');
  const showAll = $('#cm-showall');
  const search = $('#cm-search');
  const rows = cm.rows;
  const bCats = S._cmBCats || [];
  const subsFor = S._cmSubsFor || (() => []);
  const allSubs = S._cmAllSubs || [];
  const [scale, unit] = scaleOf(...rows.map(r => r.a_total).filter(v => v),
                               ...rows.map(r => r.b_total).filter(v => v));

  function targetEditor(idx, targets) {
    const rowsHtml = targets.map((t, ti) => {
      const subs = subsFor(t.category);
      const opts = subs.length ? subs : allSubs;
      return `<div class="target-row">
        <select data-cm-cat="${idx}" data-cm-t="${ti}">
          <option value="">— pick a category —</option>
          ${bCats.map(c => `<option ${t.category === c ? 'selected' : ''}>${esc(c)}</option>`).join('')}
        </select>
        <select data-cm-sub="${idx}" data-cm-t="${ti}">
          <option value="">(whole category)</option>
          ${opts.map(x => `<option ${t.subcategory === x ? 'selected' : ''}>${esc(x)}</option>`).join('')}
        </select>
        <button class="btn small" data-cm-del="${idx}" data-cm-t="${ti}" title="Remove">×</button>
      </div>`;
    }).join('');
    return `<div class="targets">${rowsHtml}
      <button class="btn small" data-cm-add="${idx}">+ add target</button></div>`;
  }

  function draw() {
    const q = (search.value || '').toLowerCase();
    const list = [];
    rows.forEach((r, i) => {
      const st = catmapRow(i);
      if (!showAll.checked && st.status === 'mapped') return;
      if (q && !(`${st.source} ${st.canonical} ${st.targets.map(t => t.category + ' ' + t.subcategory).join(' ')}`
        .toLowerCase().includes(q))) return;
      list.push([i, st]);
    });
    if (!list.length) {
      tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:22px">
        ${showAll.checked ? 'No rows match the filter.'
          : 'Every category has been dealt with. Tick “Show all” to review them.'}
      </td></tr>`;
      return;
    }
    tbody.innerHTML = list.map(([i, st]) => {
      // The evidence column shows what can be compared, labelled as a hint.
      const ev = [];
      const h = st.hint || {};
      if (h.closest_name) ev.push(`~${h.closest_name.label} (${fmtNum(h.closest_name.similarity_pct, 0)}%)`);
      if (h.closest_value) ev.push(`value Δ ${fmtNum(h.closest_value.delta_pct, 1)}%`);
      return `<tr>
        <td>${esc(st.source)}${st.source_sub ? ` <span class="hint">/ ${esc(st.source_sub)}</span>` : ''}
          ${st.a_total != null ? `<div class="hint">${fmtVal(st.a_total, scale, unit)}</div>` : ''}</td>
        <td style="text-align:left">${targetEditor(i, st.targets)}</td>
        <td style="text-align:left">
          <input type="text" data-cm-canonical="${i}" value="${esc(st.canonical)}" style="min-width:130px">
        </td>
        <td style="text-align:left">
          <select data-cm-status="${i}">
            ${['unmapped', 'mapped', 'excluded']
              .map(x => `<option ${st.status === x ? 'selected' : ''}>${x}</option>`).join('')}
          </select>
        </td>
        <td style="text-align:left;color:var(--muted)">${esc(st.method || '')}</td>
        <td style="text-align:left;color:var(--muted);font-size:11px;max-width:260px">
          ${esc(ev.join(' · ') || st.note || '')}
          ${st.b_total != null ? `<div>B total ${fmtVal(st.b_total, scale, unit)}</div>` : ''}
        </td>
      </tr>`;
    }).join('') + (list.length > 600
      ? `<tr><td colspan="6" style="color:var(--muted);padding:12px">Showing first 600 of ${list.length} — use the filter to narrow.</td></tr>`
      : '');
  }

  draw();
}

function ukeyLabel(u) {
  return u.subcategory ? `${u.category} / ${u.subcategory}` : String(u.category);
}

document.addEventListener('change', e => {
  const cat = e.target.closest('[data-cm-cat]');
  if (cat) {
    const i = Number(cat.dataset.cmCat), t = Number(cat.dataset.cmT);
    const st = catmapRow(i);
    const targets = st.targets.map(x => ({ ...x }));
    targets[t] = { ...targets[t], category: cat.value, subcategory: '' };
    cmSet(i, { targets });
    // Changing a target does not change WHICH rows are listed - a row's status
    // and canonical name are untouched. Redrawing the table here would close the
    // dropdown the user is still using, scroll to the top and drop focus, which
    // is exactly the "collapsing" that was reported. The only thing that needs
    // refreshing is the subcategory options for this one row.
    refreshTargetSubs(cat);
    return;
  }
  const sub = e.target.closest('[data-cm-sub]');
  if (sub) {
    const i = Number(sub.dataset.cmSub), t = Number(sub.dataset.cmT);
    const st = catmapRow(i);
    const targets = st.targets.map(x => ({ ...x }));
    targets[t] = { ...targets[t], subcategory: sub.value };
    cmSet(i, { targets });
    return;
  }
  const stat = e.target.closest('[data-cm-status]');
  if (stat) {
    // Status drives the "needs review" filter, so the listed rows genuinely can
    // change here. Redraw the body but keep the controls and scroll position.
    const wrap = $('.tbl-wrap');
    const keepTop = wrap ? wrap.scrollTop : 0;
    cmSet(Number(stat.dataset.cmStatus), { status: stat.value });
    drawCatmapRows();
    if (wrap) wrap.scrollTop = keepTop;
    return;
  }
  const can = e.target.closest('[data-cm-canonical]');
  if (can) { cmSet(Number(can.dataset.cmCanonical), { canonical: can.value }); return; }
});

/** Repoint one row's subcategory dropdown at the newly-chosen category. */
function refreshTargetSubs(catSelect) {
  const row = catSelect.closest('.target-row');
  const subSel = row && row.querySelector('[data-cm-sub]');
  if (!subSel) return;
  const subs = (S._cmSubsFor || (() => []))(catSelect.value);
  const opts = subs.length ? subs : (S._cmAllSubs || []);
  const current = subSel.value;
  subSel.innerHTML = '<option value="">(whole category)</option>'
    + opts.map(x => `<option ${x === current ? 'selected' : ''}>${esc(x)}</option>`).join('');
  subSel.value = opts.includes(current) ? current : '';
}

document.addEventListener('click', e => {
  const add = e.target.closest('[data-cm-add]');
  if (add) {
    const i = Number(add.dataset.cmAdd);
    const st = catmapRow(i);
    cmSet(i, { targets: [...st.targets, { category: '', subcategory: '' }] });
    renderCatmap();
    return;
  }
  const del = e.target.closest('[data-cm-del]');
  if (del) {
    const i = Number(del.dataset.cmDel), t = Number(del.dataset.cmT);
    const st = catmapRow(i);
    cmSet(i, { targets: st.targets.filter((_, j) => j !== t) });
    renderCatmap();
  }
});

function buildCategoryMapping() {
  const rows = S.catmap.rows.map((_, i) => {
    const st = catmapRow(i);
    // A row the user never touched stays `unmapped` and carries no targets, so
    // the analysis drops it. A row with a target is `mapped`; a row with a
    // status set but no target is respected as the user left it.
    const targets = st.targets.filter(t => t.category);
    return {
      source: st.source,
      source_sub: st.source_sub,
      canonical: st.canonical,
      targets,
      status: st.status,
    };
  });
  return {
    rows,
    new_in_b: S.catmap.new_in_b || [],
    // Off unless the user asks: a B-only category has no "before" figure, so
    // reporting it as an impact would be presenting a one-sided number.
    include_new_in_b: !!S.catmapIncludeNewInB,
  };
}

/** The canonical categories the analysis will report, in a stable order. */
function canonicalCategoryList() {
  const out = new Set();
  S.catmap.rows.forEach((_, i) => {
    const st = catmapRow(i);
    // Only mapped rows produce a category. An unmapped one has no counterpart,
    // so it is not a category the study can report an impact for.
    if (st.status === 'mapped') out.add(st.canonical);
    else if (st.status === 'excluded') return;
  });
  if (S.catmapIncludeNewInB) {
    (S.catmap.new_in_b || []).forEach(u => {
      const claimed = S.catmap.rows.some((_, i) => {
        const st = catmapRow(i);
        return st.targets.some(t => t.category === u.category
          && (t.subcategory || '') === (u.subcategory || ''));
      });
      if (!claimed) out.add(ukeyLabel(u));
    });
  }
  return Array.from(out).sort();
}

// ---------------------------------------------------------------------------
// step 5 — selection
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
// ---------------------------------------------------------------------------
const METRIC_DEFS = [
  {
    key: 'sales_value',
    label: 'Sales Value',
    match: /sales?\s*value/i,
    is_rate: false,
    growth_applicable: true,
    hint: 'Value. Growth, level shift and contribution.',
  },
  {
    key: 'volume',
    label: 'Volume',
    match: /\bvolume\b|\bunits?\b|\bqty\b|\bquantity\b/i,
    is_rate: false,
    growth_applicable: true,
    hint: 'Units / volume. Growth, level shift and contribution.',
  },
  {
    key: 'nd',
    label: 'Numeric Distribution (ND)',
    match: /\bnd\b|numeric\s*distribution|\bdist\b|distribution/i,
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

  // markets — the values the user paired in step 3, at the level they chose.
  // The scope is their decision, so it is read back from the pairings rather
  // than recomputed; the chips let them narrow it further for this run.
  const pairs = S.marketPairs || [];
  const lvl = S.marketLevel || 'total';
  const atLevel = pairs.filter(p => lvl === 'all' || p.level === lvl).map(p => p.market_a).filter(Boolean);
  const canon = atLevel.length ? atLevel
    : pairs.map(p => p.market_a).filter(Boolean);
  const scope = canon;
  $('#c-markets').innerHTML = canon.map(v =>
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
 * Period selects, one block per selected metric. Every metric gets its own
 * prior/current pair on each side, because the same metric family can expose a
 * different set of periods on the two datasets.
 */
function renderMetricPeriods() {
  const box = $('#c-periods');
  if (!box) return;
  const sel = METRIC_DEFS.filter(d => S.metricSel[d.key]);
  if (!sel.length) {
    box.innerHTML = '<p class="hint">Select at least one metric.</p>';
    return;
  }
  box.innerHTML = sel.map(def => {
    const wires = resolveWiring(def);
    const fa = S.profile.a.metric_families?.[wires.family_a] || {};
    const fb = S.profile.b.metric_families?.[wires.family_b] || {};
    const cur = S.metricWiring?.[def.key] || {};
    return `<div class="wire-card">
      <div class="wire-head">${esc(def.label)}
        ${def.is_rate ? '<span class="tag warn">no growth</span>' : ''}</div>
      <div class="grid two tight">
        <div class="field" style="margin:0"><label>A · prior period</label>
          <select data-per="a_prior" data-key="${def.key}">${periodOpts(fa, cur.a_prior, 'YA')}</select></div>
        <div class="field" style="margin:0"><label>A · current period</label>
          <select data-per="a_current" data-key="${def.key}">${periodOpts(fa, cur.a_current, 'VALUE')}</select></div>
        <div class="field" style="margin:0"><label>B · prior period</label>
          <select data-per="b_prior" data-key="${def.key}">${periodOpts(fb, cur.b_prior, 'YA')}</select></div>
        <div class="field" style="margin:0"><label>B · current period</label>
          <select data-per="b_current" data-key="${def.key}">${periodOpts(fb, cur.b_current, 'VALUE')}</select></div>
      </div>
    </div>`;
  }).join('');

  $$('#c-periods [data-per]').forEach(el => {
    el.onchange = () => {
      const key = el.dataset.key;
      S.metricWiring = S.metricWiring || {};
      S.metricWiring[key] = { ...(S.metricWiring[key] || {}), [el.dataset.per]: el.value };
      validateMetricWiring();
    };
  });
}

/** <option> list for one side's period variants, preferring the usual default. */
function periodOpts(obj, chosen, preferred) {
  const order = { '2YA': 0, 'YA': 1, 'VALUE': 2, 'TY': 3 };
  const variants = Object.keys(obj || {}).sort((x, y) => (order[x] ?? 9) - (order[y] ?? 9));
  if (!variants.length) return '<option value="">— not available —</option>';
  const def = obj[preferred] || obj[variants[0]];
  return variants.map(v => {
    const sel = chosen !== undefined && chosen !== null && chosen !== ''
      ? v === chosen
      : obj[v] === def;
    return `<option value="${esc(v)}" ${sel ? 'selected' : ''}>${esc(v)} · ${esc(obj[v])}</option>`;
  }).join('');
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
 * Turn the selected metrics into the request blocks, reading the period selects
 * and the per-side weight columns.
 */
function buildMetricBlocks() {
  const out = [];
  METRIC_DEFS.forEach(def => {
    if (!S.metricSel?.[def.key]) return;
    const w = resolveWiring(def);
    const fa = S.profile.a.metric_families?.[w.family_a] || {};
    const fb = S.profile.b.metric_families?.[w.family_b] || {};
    // A period select's value is a *variant key* within the family (`YA`,
    // `VALUE`, `2YA`), while the analysis needs the real column name the family
    // maps it to (`Sales Value YA`). Sending the bare key made the server reject
    // the run with "column 'YA' ... does not exist", so translate here. The
    // fallbacks are already column names, hence the || inside the lookup.
    const col = (fam, key) => (key && fam?.[key]) || key || '';
    const a_prior = col(fa, $(`[data-per="a_prior"][data-key="${def.key}"]`)?.value
      || w.a_prior || 'YA');
    const a_current = col(fa, $(`[data-per="a_current"][data-key="${def.key}"]`)?.value
      || w.a_current || 'VALUE');
    // B may name its periods differently, so resolve against B's family and only
    // fall back to A's real column when B has no such variant at all.
    const b_prior_key = $(`[data-per="b_prior"][data-key="${def.key}"]`)?.value
      || w.b_prior || 'YA';
    const b_current_key = $(`[data-per="b_current"][data-key="${def.key}"]`)?.value
      || w.b_current || 'VALUE';
    const b_prior = fb[b_prior_key] || col(fa, b_prior_key);
    const b_current = fb[b_current_key] || col(fa, b_current_key);
    out.push({
      key: def.key,
      label: def.label,
      family_a: w.family_a || '',
      family_b: w.family_b || '',
      a_prior, a_current, b_prior, b_current,
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
 * the family carries several periods. Prefer the family's current-period column.
 */
function defaultWeight(side) {
  const fams = S.profile[side]?.metric_families || {};
  const name = Object.keys(fams).find(f => /sales?\s*value/i.test(f))
    || Object.keys(fams)[0] || '';
  const fam = fams[name] || {};
  return fam.VALUE || fam.TY || Object.values(fam)[0] || '';
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
    [['A · prior period', b.a_prior, colsA], ['A · current period', b.a_current, colsA],
     ['B · prior period', b.b_prior, colsB], ['B · current period', b.b_current, colsB]]
      .forEach(([label, v, known]) => {
        if (!v) problems.push(`${b.label}: ${label} is not mapped`);
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

function renderCategories() {
  const q = ($('#c-cat-search').value || '').toLowerCase();
  const all = S._allCategories || [];
  const list = q ? all.filter(c => c.toLowerCase().includes(q)) : all;
  $('#c-categories').innerHTML = list.map(c =>
    `<span class="chip ${S.selection.categories.includes(c) ? 'on' : ''}" data-cat="${esc(c)}">${esc(c)}</span>`
  ).join('') || '<span class="hint">No match.</span>';
  updateCatCount();
}

$('#c-cat-search')?.addEventListener('input', renderCategories);
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

  $('#run-out').innerHTML = `
    ${qcBanner}
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
         brand_block: rep.brand_block, manufacturer_top_n: rep.manufacturer_top_n,
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
  const ch = m.channel_block || [];
  const br = m.brand_block || [];
  const mt = m.manufacturer_top_n || [];
  const bt = m.brand_top_n || [];

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

    ${ch.length ? `<div class="blk">
      <div class="blk-head"><h4>Market / channel block</h4>
        <span class="sub">${esc(m.label)}${unit ? ' (' + unit + ')' : ''} · BEFORE vs AFTER${g
          ? ' with level shift and contribution' : ' — top channels by TY, with absolute change'}</span></div>
      <div class="tbl-wrap"><table>
        <thead>${g ? `<tr>
            <th rowspan="2">Channels</th>
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
            <th rowspan="2">Channels</th>
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
          ${ch.map(c => g ? `<tr>
            <td>${esc(c.name)}</td>
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
          </tr>` : `<tr>
            <td>${esc(c.name)}</td>
            <td class="num">${fmtVal(c.before.mat_ya, scale, unit)}</td>
            <td class="num">${fmtVal(c.before.mat_ty, scale, unit)}</td>
            <td class="num">${fmtVal(c.after.mat_ya, scale, unit)}</td>
            <td class="num">${fmtVal(c.after.mat_ty, scale, unit)}</td>
            <td class="num ${cls((c.after.mat_ty || 0) - (c.after.mat_ya || 0))}">${fmtVal((c.after.mat_ty || 0) - (c.after.mat_ya || 0), scale, unit)}</td>
            <td class="num">${fmtNum(c.contribution.after_share_pct, 1)}%</td>
            <td class="num">${fmtNum(c.contribution.before_share_pct, 1)}%</td>
            <td class="num">${fmtNum(c.contribution.after_share_pct, 1)}%</td>
          </tr>`).join('')}
          <tr class="total">
            <td>Total</td>
            <td class="num">${fmtVal(t.before_prior, scale, unit)}</td>
            <td class="num">${fmtVal(t.before_current, scale, unit)}</td>
            ${g ? `<td class="num ${cls(t.before_growth_pct)}">${pct(t.before_growth_pct)}</td>` : '<td class="num">—</td>'}
            <td class="num">${fmtVal(t.after_prior, scale, unit)}</td>
            <td class="num">${fmtVal(t.after_current, scale, unit)}</td>
            ${g ? `<td class="num ${cls(t.after_growth_pct)}">${pct(t.after_growth_pct)}</td>
            <td class="num ${cls(t.level_shift_pp)}">${pp(t.level_shift_pp)}</td>` :
            `<td class="num ${cls((t.after_current || 0) - (t.after_prior || 0))}">${fmtVal((t.after_current || 0) - (t.after_prior || 0), scale, unit)}</td>
            <td class="num">100.0%</td>`}
            <td class="num">100.0%</td><td class="num">100.0%</td>
            <td class="num">100.0%</td><td class="num">100.0%</td>
          </tr>
        </tbody>
      </table></div>
    </div>` : ''}
    ${renderEntities({ ...m, contributors: m.contributors }, { scale, unit, g })}
  `;
}

/** Brand / manufacturer / client tables and the contributor grid, per metric. */
function renderEntities(m, { scale, unit, g }) {
  const br = m.brand_block || [];
  const mt = m.manufacturer_top_n || [];
  const bt = m.brand_top_n || [];
  const rep = m;
  return `
    ${br.length ? `
    <div class="blk">
      <div class="blk-head"><h4>Brand value share</h4>
        <span class="sub">share of the category, before vs after</span></div>
      <div class="tbl-wrap" style="max-height:420px;overflow:auto">
        <table>
          <thead>
            <tr><th rowspan="2">Brands</th>
              <th colspan="3" class="grp-before">BEFORE</th>
              <th colspan="3" class="grp-after">AFTER</th></tr>
            <tr><th>MAT YA</th><th>MAT TY</th><th>MAT share chg</th>
                <th>MAT YA</th><th>MAT TY</th><th>MAT share chg</th></tr>
          </thead>
          <tbody>
            ${br.slice(0, 30).map(b => {
              const chg = (b.before.share_pct !== null && b.after.share_pct !== null)
                ? b.after.share_pct - b.before.share_pct : null;
              return `<tr>
                <td>${esc(b.name)}</td>
                <td class="num">${fmtNum(b.before.share_pct, 1)}%</td>
                <td class="num">${fmtNum(b.before.share_pct, 1)}%</td>
                <td class="num ${cls(b.before.share_chg_pp)}">${pp(b.before.share_chg_pp)}</td>
                <td class="num">${fmtNum(b.after.share_pct, 1)}%</td>
                <td class="num">${fmtNum(b.after.share_pct, 1)}%</td>
                <td class="num ${cls(chg)}">${pp(chg)}</td>
              </tr>`;
            }).join('')}
          </tbody>
        </table>
      </div>
    </div>` : ''}

    ${mt.length ? `
    <div class="blk">
      <div class="blk-head"><h4>Manufacturer Top-${mt.length}</h4>
        <span class="sub">rank movement after the update</span></div>
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
        <span class="sub">rank movement after the update</span></div>
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
$('#btn-upload')?.addEventListener('click', async () => {
  const fi = $('#file-input');
  if (!fi.files.length) return;
  const fd = new FormData();
  fd.append('file', fi.files[0]);
  overlay(true, 'Uploading…');
  try {
    await api('/api/source', { method: 'POST', body: fd });
    await loadSources();
    $('#upload-msg').textContent = `Registered ${fi.files[0].name}`;
    fi.value = '';
  } catch (e) { $('#upload-msg').textContent = 'Error: ' + e.message; }
  finally { overlay(false); }
});

$('#btn-path')?.addEventListener('click', async () => {
  const p = $('#path-input').value.trim();
  if (!p) return;
  overlay(true, 'Opening…');
  try {
    const d = await api('/api/source/from-path', {
      method: 'POST', body: JSON.stringify({ path: p }),
    });
    await loadSources();
    $('#upload-msg').textContent = `Registered ${d.filename} (${d.sheets.length} sheets)`;
  } catch (e) { $('#upload-msg').textContent = 'Error: ' + e.message; }
  finally { overlay(false); }
});

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
