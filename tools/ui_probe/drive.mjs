// UI verification driver for Impact Studio.
// Walks the full seven-step workflow in headless Chrome and asserts on rendered
// content, not just the absence of errors.
//
//   node drive.mjs <outputDir>
//
// Run this against an IDLE server. The driver walks the app while another heavy
// request (a 150-category bulk export, say) is still occupying the single
// worker, the analysis step can observe a degraded state and the run reports
// failures that are load artefacts, not defects. Observed for real: a walk
// overlapping a full-breadth export reported 9 failures, then passed 100% when
// re-run against the same code on an idle server.

import { writeFileSync, mkdirSync, existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const CDP = process.env.CDP_URL || 'http://127.0.0.1:9222'
const APP = process.env.APP_URL || 'http://127.0.0.1:8777'
const OUT = process.argv[2] || '.'
const VIEWPORT = { width: 1600, height: 1200 }

// Resolve the reference workbook relative to this file rather than hardcoding an
// absolute path. The hardcoded path pointed one directory above the project root,
// so the driver could not register a source and stalled at step 1 - which then
// looked like a hung probe (only two screenshots in twelve minutes) when the
// real cause was a missing file.
const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = join(HERE, '..', '..')
const WORKBOOK = process.env.WORKBOOK || [
  join(ROOT, 'TW Impact Study_V2 1 (1).xlsx'),
  join(ROOT, '..', 'TW Impact Study_V2 1 (1).xlsx'),
].find(p => existsSync(p)) || join(ROOT, 'TW Impact Study_V2 1 (1).xlsx')
if (!existsSync(WORKBOOK)) {
  console.error(`  ! workbook not found: ${WORKBOOK}`)
  console.error('    set WORKBOOK=<path> to point the driver at it.')
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
let failures = 0

function check(name, ok, msg = '') {
  if (!ok) failures++
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${name}${msg ? '  ' + msg : ''}`)
  return ok
}

async function connect() {
  const targets = await (await fetch(`${CDP}/json/list`)).json()
  const page = targets.find((t) => t.type === 'page')
  if (!page) throw new Error('no page target')
  const ws = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej })

  let id = 0
  const pending = new Map()
  const events = []
  const dialogs = []
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data)
    if (msg.id && pending.has(msg.id)) {
      const { res, rej } = pending.get(msg.id)
      pending.delete(msg.id)
      msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result)
      return
    }
    if (msg.method) {
      events.push(msg)
      // An alert() blocks the renderer with no one to dismiss it in headless
      // Chrome, so the probe would sit silent forever. Accept the dialog and
      // keep its text, turning a hang into a reported failure.
      if (msg.method === 'Page.javascriptDialogOpening') {
        dialogs.push(msg.params.message)
        ws.send(JSON.stringify({
          id: ++id, method: 'Page.handleJavaScriptDialog',
          params: { accept: true },
        }))
      }
    }
  }
  const send = (method, params = {}) => new Promise((res, rej) => {
    const mid = ++id
    pending.set(mid, { res, rej })
    ws.send(JSON.stringify({ id: mid, method, params }))
  })
  return { send, events, dialogs, close: () => ws.close() }
}

async function main() {
  mkdirSync(OUT, { recursive: true })
  const { send, events, dialogs, close } = await connect()

  await send('Page.enable')
  await send('Runtime.enable')
  await send('Log.enable')
  // Disable the HTTP cache. Headless Chrome reuses app.js across navigations,
  // so without this a run can silently test the PREVIOUS build and report a
  // fixed defect as still broken.
  await send('Network.enable')
  await send('Network.setCacheDisabled', { cacheDisabled: true })
  await send('Emulation.setDeviceMetricsOverride', {
    ...VIEWPORT, deviceScaleFactor: 1, mobile: false,
  })
  await send('Emulation.setEmulatedMedia', {
    features: [{ name: 'prefers-color-scheme', value: 'dark' }],
  })

  const shot = async (name) => {
    const { data } = await send('Page.captureScreenshot', { format: 'png' })
    writeFileSync(`${OUT}/${name}.png`, Buffer.from(data, 'base64'))
  }
  const evaluate = async (expr) => {
    const r = await send('Runtime.evaluate', {
      expression: expr, returnByValue: true, awaitPromise: true,
    })
    if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails))
    return r.result.value
  }
  const setSelect = (sel, val) => evaluate(`(() => {
    const el = document.querySelector(${JSON.stringify(sel)})
    if (!el) return 'NOT_FOUND'
    const opts = [...el.options].map(o => o.value)
    if (!opts.includes(${JSON.stringify(val)})) return 'NO_OPTION:' + opts.slice(0,5).join('|')
    el.value = ${JSON.stringify(val)}
    el.dispatchEvent(new Event('change', { bubbles: true }))
    return 'ok'
  })()`)
  const setInput = (sel, val) => evaluate(`(() => {
    const el = document.querySelector(${JSON.stringify(sel)})
    if (!el) return 'NOT_FOUND'
    const d = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')
    d.set.call(el, ${JSON.stringify(val)})
    el.dispatchEvent(new Event('input', { bubbles: true }))
    el.dispatchEvent(new Event('change', { bubbles: true }))
    return 'ok'
  })()`)
  const click = (sel) => evaluate(`(() => {
    const el = document.querySelector(${JSON.stringify(sel)})
    if (!el) return 'NOT_FOUND'
    el.scrollIntoView({ block: 'center' })
    el.click(); return 'clicked'
  })()`)
  // Selecting a source/sheet triggers an async column read; polling for the
  // dependent dropdown to populate is far more reliable than a fixed sleep.
  const waitForOptions = async (sel, min, timeoutMs = 60000) => {
    const t0 = Date.now()
    while (Date.now() - t0 < timeoutMs) {
      const n = await evaluate(
        `document.querySelector(${JSON.stringify(sel)})?.options.length ?? 0`)
      if (n >= min) return n
      await sleep(500)
    }
    return -1
  }
  const valOf = (sel) => evaluate(
    `document.querySelector(${JSON.stringify(sel)})?.value ?? null`)
  // Poll an arbitrary predicate expression until it is truthy. Step 5 needs this:
  // buildSelectionUI kicks off doMarketMapping() and re-runs itself when the
  // classification arrives, so the metric/period panels are replaced AFTER the
  // step is first shown. A fixed sleep read the pre-refresh frame and reported
  // empty period selects and a blank wiring note on a UI that was in fact fine.
  const waitFor = async (expr, timeoutMs = 60000, every = 300) => {
    const t0 = Date.now()
    while (Date.now() - t0 < timeoutMs) {
      if (await evaluate(expr)) return true
      await sleep(every)
    }
    return false
  }
  const activeStep = () => evaluate(
    `document.querySelector('.step.active')?.dataset.step ?? null`)
  // A modal that never hides is invisible to el.click() (which bypasses
  // hit-testing) and to every API test. Measure it two ways.
  const overlayState = () => evaluate(`(() => {
    const o = document.querySelector('#overlay')
    if (!o) return { missing: true }
    const cs = getComputedStyle(o)
    const r = o.getBoundingClientRect()
    return {
      attrHidden: o.hidden,
      display: cs.display,
      visible: cs.display !== 'none' && cs.visibility !== 'hidden'
               && Number(cs.opacity) > 0.01 && r.width > 0 && r.height > 0,
    }
  })()`)
  const hitTest = (sel) => evaluate(`(() => {
    const el = document.querySelector(${JSON.stringify(sel)})
    if (!el) return { found: false }
    // An element below the fold hit-tests as null, which is a layout fact and
    // not a bug. Bring it into view before measuring.
    el.scrollIntoView({ block: 'center' })
    const r = el.getBoundingClientRect()
    const x = Math.round(r.left + r.width / 2), y = Math.round(r.top + r.height / 2)
    const top = document.elementFromPoint(x, y)
    return {
      found: true,
      onScreen: r.top < window.innerHeight && r.bottom > 0,
      reachable: !!top && (top === el || el.contains(top) || top.contains(el)),
      topEl: top ? (top.id ? '#' + top.id : (top.className || top.tagName)) : null,
    }
  })()`)
  const assertOverlayCleared = async (label) => {
    const o = await overlayState()
    check(`overlay cleared after ${label}`, o.visible === false, JSON.stringify(o))
  }

  const drainEvents = () => events.splice(0, events.length)
  const errorReport = () => {
    const out = { consoleErrors: [], exceptions: [], logErrors: [] }
    for (const e of events) {
      if (e.method === 'Runtime.consoleAPICalled' && e.params.type === 'error')
        out.consoleErrors.push(e.params.args.map(a => a.value ?? a.description).join(' '))
      if (e.method === 'Runtime.exceptionThrown')
        out.exceptions.push(e.params.exceptionDetails?.text ?? 'exception')
      if (e.method === 'Log.entryAdded' && e.params.entry.level === 'error')
        out.logErrors.push(e.params.entry.text)
    }
    return out
  }

  console.log('='.repeat(74))
  console.log(' Impact Studio UI verification')
  console.log('='.repeat(74))

  drainEvents()
  await send('Page.navigate', { url: `${APP}?_probe=${Date.now()}` })
  await sleep(3500)

  // ---- shell must actually render ----------------------------------------
  const shell = await evaluate(`({
    steps: document.querySelectorAll('.step').length,
    panels: document.querySelectorAll('.panel').length,
    title: document.querySelector('.brand h1')?.textContent,
    health: document.querySelector('#health')?.textContent,
  })`)
  console.log('\n  Shell:', JSON.stringify(shell))
  check('seven wizard steps rendered', shell.steps === 7, `${shell.steps}`)
  check('seven panels present', shell.panels === 7, `${shell.panels}`)
  check('brand title rendered', shell.title === 'Impact Studio', String(shell.title))
  check('backend health reported', /engine ok/.test(shell.health || ''),
        String(shell.health))
  check('step 1 is the active step on arrival', await activeStep() === '1')
  // A malformed SVG still returns 200, so decode it rather than trusting the
  // response code. (An XML comment containing "--" breaks parsing.)
  const fav = await evaluate(`new Promise(r => {
    const i = new Image()
    i.onload = () => r({ ok: i.naturalWidth > 0, w: i.naturalWidth })
    i.onerror = () => r({ ok: false })
    i.src = '/static/favicon.svg'
  })`)
  check('favicon decodes as an image', fav.ok === true, JSON.stringify(fav))
  await shot('01-step1-empty')

  // ---- register the workbook ---------------------------------------------
  await setInput('#path-input', WORKBOOK)
  await click('#btn-path')
  await sleep(4000)
  const reg = await evaluate(`document.querySelector('#upload-msg')?.textContent`)
  console.log('  register:', reg)
  check('workbook registered', /Registered/.test(reg || ''), String(reg))

  const srcOpts = await evaluate(
    `[...document.querySelector('#a-source').options].map(o => o.value).filter(Boolean)`)
  check('source appears in both pickers', srcOpts.length >= 1, `${srcOpts.length} source(s)`)
  const sid = srcOpts[srcOpts.length - 1]

  // ---- configure A and B --------------------------------------------------
  for (const side of ['a', 'b']) {
    await setSelect(`#${side}-source`, sid)
    const nSheets = await waitForOptions(`#${side}-sheet`, 2)
    check(`[${side}] sheet list populated`, nSheets > 0, `${nSheets} options`)
    const sheetOk = await setSelect(`#${side}-sheet`, 'Raw_MAT')
    check(`[${side}] Raw_MAT sheet selected`, sheetOk === 'ok', sheetOk)
    const nCols = await waitForOptions(`#${side}-splitcol`, 5)
    check(`[${side}] column list populated`, nCols > 0, `${nCols} options`)
    const colOk = await setSelect(`#${side}-splitcol`, 'Dataset')
    check(`[${side}] split column = Dataset`, colOk === 'ok', colOk)
    const nVals = await waitForOptions(`#${side}-value`, 2)
    check(`[${side}] discriminator values populated`, nVals > 0, `${nVals} options`)
    const val = side === 'a' ? 'Current MAT' : 'New MAT'
    const vOk = await setSelect(`#${side}-value`, val)
    check(`[${side}] discriminator value = ${val}`, vOk === 'ok', vOk)
  }

  // Assert the actual wiring the profile request will carry, so a driver race
  // shows up here rather than as a mysterious downstream count.
  const wiring = {
    a: { src: await valOf('#a-source'), sheet: await valOf('#a-sheet'),
         split: await valOf('#a-splitcol'), val: await valOf('#a-value') },
    b: { src: await valOf('#b-source'), sheet: await valOf('#b-sheet'),
         split: await valOf('#b-splitcol'), val: await valOf('#b-value') },
  }
  console.log('  wiring:', JSON.stringify(wiring))
  check('A is wired to the previous dataset',
        wiring.a.sheet === 'Raw_MAT' && wiring.a.split === 'Dataset'
        && wiring.a.val === 'Current MAT', JSON.stringify(wiring.a))
  check('B is wired to the updated dataset',
        wiring.b.sheet === 'Raw_MAT' && wiring.b.split === 'Dataset'
        && wiring.b.val === 'New MAT', JSON.stringify(wiring.b))
  await shot('02-step1-configured')

  // ---- step 2: profile ----------------------------------------------------
  await click('#btn-profile')
  for (let i = 0; i < 90; i++) {
    await sleep(1000)
    if (await activeStep() === '2') break
  }
  check('advanced to step 2 (profile)', await activeStep() === '2')
  await assertOverlayCleared('profile')
  const prof = await evaluate(`({
    cards: document.querySelectorAll('#profile-out .card').length,
    stats: document.querySelectorAll('#profile-out .stat').length,
    dimSelects: document.querySelectorAll('#profile-out [data-dim-role]').length,
    rows: [...document.querySelectorAll('#profile-out .stat')]
            .filter(s => /rows/i.test(s.querySelector('.s-label')?.innerText || ''))
            .map(s => s.querySelector('.s-value')?.innerText),
    text: document.querySelector('#profile-out').innerText.slice(0, 400),
  })`)
  console.log('  profile:', JSON.stringify({ cards: prof.cards, stats: prof.stats,
    dimSelects: prof.dimSelects, rows: prof.rows }))
  check('two profile cards rendered', prof.cards === 2, `${prof.cards}`)
  check('metric/dimension stat tiles rendered', prof.stats >= 8, `${prof.stats}`)
  check('editable dimension selects rendered', prof.dimSelects === 14,
        `${prof.dimSelects}`)
  // innerText reflects text-transform, so the stat labels read "ROWS" not "Rows"
  check('profile shows row counts', /rows/i.test(prof.text))
  // The split must actually have been applied: A=45,421 and B=50,714 rows.
  const rowNums = (prof.rows || []).map(s => Number(String(s).replace(/,/g, '')))
  check('dataset split applied (A and B have different row counts)',
        rowNums.length === 2 && rowNums[0] !== rowNums[1],
        `rows = ${JSON.stringify(prof.rows)}`)
  check('A row count matches the previous dataset',
        rowNums[0] === 45421, `A=${rowNums[0]}`)
  check('B row count matches the updated dataset',
        rowNums[1] === 50714, `B=${rowNums[1]}`)
  await shot('03-step2-profile')

  // ---- step 3: dimension mapping -----------------------------------------
  await click('#btn-map')
  for (let i = 0; i < 180; i++) {
    await sleep(1000)
    if (await activeStep() === '3') break
  }
  check('advanced to step 3 (dimension mapping)', await activeStep() === '3')
  const mapInfo = await evaluate(`({
    tabs: document.querySelectorAll('#map-tabs .tab').length,
    badges: [...document.querySelectorAll('#map-body .badge')].map(b => b.textContent.trim()),
    summary: document.querySelector('#map-body .map-sum')?.innerText,
  })`)
  console.log('  dimension mapping:', JSON.stringify({ tabs: mapInfo.tabs }))
  check('one mapping tab per non-category dimension', mapInfo.tabs === 3,
        `${mapInfo.tabs} (market/manufacturer/brand)`)
  const tabNames = await evaluate(
    `[...document.querySelectorAll('#map-tabs .tab')].map(t => t.textContent.trim())`)
  console.log('  dimension tabs:', JSON.stringify(tabNames))
  check('category is no longer handled in this step',
        !tabNames.some(t => /categor/i.test(t)),
        'category moved to its own step')
  await shot('04-step3-dimension-mapping')

  // ---- step 4: category mapping -------------------------------------------
  await click('#btn-catmap')
  for (let i = 0; i < 240; i++) {
    await sleep(1000)
    if (await activeStep() === '4') break
  }
  check('advanced to step 4 (category mapping)', await activeStep() === '4')
  const cmAttention = await evaluate(
    `document.querySelectorAll('#cm-tbl tbody tr').length`)
  const cmAttentionText = await evaluate(
    `document.querySelector('#cm-tbl tbody')?.innerText?.slice(0, 90)`)
  console.log('  category rows needing attention:', cmAttention,
    JSON.stringify(cmAttentionText))

  // The step opens in "one category at a time" mode, which is an accordion over
  // the same rows: the first row needing a decision is expanded, the full table
  // is deliberately hidden (#cm-table-card display:none). Assert the accordion
  // itself first - one open at a time, its attached targets, an add-target button
  // and a status control - then switch the mode off to assert the full table,
  // which is where targets are reassigned.
  const accCount = await evaluate(
    `document.querySelectorAll('#cm-acc .acc-item').length`)
  check('the accordion lists every category',
        accCount >= 100, `${accCount} rows`)
  const closedState = await evaluate(
    `document.querySelectorAll('#cm-acc .acc-item.open').length`)
  check('exactly one category is expanded at a time',
        closedState === 1, `${closedState} open`)
  // The step opens the first row that still needs a decision, so the controls
  // are visible on arrival without a click. Inspect that row, then open a
  // DIFFERENT one and confirm the first closed.
  const accFirst = await evaluate(`(() => {
    const open = document.querySelector('#cm-acc .acc-item.open');
    if (!open) return { open: 0 };
    return {
      open: document.querySelectorAll('#cm-acc .acc-item.open').length,
      name: (open.querySelector('.acc-head') || {}).innerText || '',
      // The controls the body actually renders: an analysis-name input, a status
      // select, a target list, and the add-target button. These are element ids,
      // not data-* attributes.
      hasCanonical: !!open.querySelector('#cm-canon'),
      hasStatus: !!open.querySelector('#cm-status'),
      // In the accordion the attached targets are listed as chips, each with a
      // remove button; the editable category dropdowns belong to the full-table
      // view (targetEditor in drawCatmapRows), not to this body.
      hasTargets: !!open.querySelector('#cm-targets'),
      hasTargetChips: !!open.querySelector('#cm-targets .chip'),
      hasRemove: !!open.querySelector('#cm-targets [data-rm]'),
      hasAdd: !!open.querySelector('#cm-add'),
      hasValues: !!open.querySelector('.acc-vals-box'),
    };
  })()`)
  const accSecond = await evaluate(`(() => {
    const items = [...document.querySelectorAll('#cm-acc .acc-item')];
    const openIdx = items.findIndex(it => it.classList.contains('open'));
    const other = items[openIdx + 1] || items[openIdx - 1];
    if (!other) return -1;
    const head = other.querySelector('.acc-head');
    if (head) head.click();
    return document.querySelectorAll('#cm-acc .acc-item.open').length;
  })()`)
  console.log('  accordion:', JSON.stringify(accFirst), '->', accSecond, 'open')
  check('expanded row lists its attached targets',
        accFirst.hasTargetChips === true && accFirst.hasRemove === true,
        `chips=${accFirst.hasTargetChips} removable=${accFirst.hasRemove}`)
  check('expanded row exposes an editable analysis name',
        accFirst.hasCanonical === true)
  check('the expanded row can take an extra target (1:N)',
        accFirst.hasAdd === true)
  check('the expanded row can be excluded', accFirst.hasStatus === true)
  check('the expanded row shows before/after values',
        accFirst.hasValues === true)
  check('opening a second category closes the first',
        accSecond === 1, `${accSecond} open`)
  await shot('05a-step4-accordion')

  // Switch off one-at-a-time so the full table is visible and assertable.
  await evaluate(`(() => {
    const t = document.querySelector('#cm-onlyone');
    if (t && t.checked) { t.checked = false; t.dispatchEvent(new Event('change', {bubbles:true})); }
  })()`)
  await sleep(600)
  const tableVisible = await evaluate(
    `getComputedStyle(document.querySelector('#cm-table-card')).display !== 'none'`)
  check('turning one-at-a-time off reveals the full table', tableVisible === true)
  // With every category matching automatically the default view is correctly
  // empty, so tick "show all" before inspecting the table.
  await evaluate(`document.querySelector('#cm-showall').click()`)
  await sleep(600)
  const cm = await evaluate(`({
    stats: document.querySelectorAll('#catmap-out .stat').length,
    rows: document.querySelectorAll('#cm-tbl tbody tr').length,
    hasTargetEditor: !!document.querySelector('#cm-tbl [data-cm-cat]'),
    hasCanonical: !!document.querySelector('#cm-tbl [data-cm-canonical]'),
    hasAddTarget: !!document.querySelector('#cm-tbl [data-cm-add]'),
    hasStatus: !!document.querySelector('#cm-tbl [data-cm-status]'),
    newInB: document.querySelectorAll('#catmap-out .chip[data-newb]').length,
  })`)
  console.log('  category mapping:', JSON.stringify({ stats: cm.stats, rows: cm.rows,
    newInB: cm.newInB }))
  check('category mapping summary rendered', cm.stats === 8, `${cm.stats} tiles`)
  check('every category is listed with a target editor',
        cm.hasTargetEditor === true && cm.rows >= 100, `${cm.rows} rows`)
  check('each row exposes an editable analysis name', cm.hasCanonical === true)
  check('rows can take an extra target (1:N)', cm.hasAddTarget === true)
  check('rows can be excluded', cm.hasStatus === true)
  check('new-in-B units are listed', cm.newInB >= 2, `${cm.newInB}`)
  await shot('05-step4-category-mapping')

  // Editing a target must not throw away the view. Rebuilding the panel used to
  // reset the filter and the "Show all" checkbox, so the list appeared to
  // collapse under the cursor. Guarded here so it cannot come back.
  await evaluate(`(() => {
    const cb = document.querySelector('#cm-showall');
    if (cb && !cb.checked) { cb.checked = true; cb.dispatchEvent(new Event('change', { bubbles: true })); }
    const s = document.querySelector('#cm-search');
    s.value = 'cig'; s.dispatchEvent(new Event('input', { bubbles: true }));
  })()`)
  await sleep(500)
  const beforeEdit = await evaluate(`(() => ({
    filter: document.querySelector('#cm-search').value,
    showAll: document.querySelector('#cm-showall').checked,
    rows: document.querySelectorAll('#cm-tbl tbody tr').length,
  }))()`)
  const didEdit = await evaluate(`(() => {
    const sel = document.querySelector('#cm-tbl select[data-cm-cat]');
    if (!sel) return false;
    const pick = [...sel.options].find(o => o.value && o.value !== sel.value);
    if (!pick) return false;
    sel.value = pick.value;
    sel.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  })()`)
  await sleep(600)
  const afterEdit = await evaluate(`(() => ({
    filter: document.querySelector('#cm-search').value,
    showAll: document.querySelector('#cm-showall').checked,
    rows: document.querySelectorAll('#cm-tbl tbody tr').length,
    empty: /No rows match|Every category matched/.test(
      [...document.querySelectorAll('#cm-tbl tbody tr')].map(r => r.innerText).join(' ')),
  }))()`)
  check('a target edit was exercised', didEdit === true)
  check('editing a target preserves the filter', afterEdit.filter === beforeEdit.filter,
        `"${beforeEdit.filter}" -> "${afterEdit.filter}"`)
  check('editing a target preserves "Show all"', afterEdit.showAll === beforeEdit.showAll,
        `${beforeEdit.showAll} -> ${afterEdit.showAll}`)
  check('editing a target does not collapse the row list',
        afterEdit.rows >= beforeEdit.rows && !afterEdit.empty,
        `${beforeEdit.rows} -> ${afterEdit.rows} rows`)
  // put the view back so the rest of the walk is unaffected
  await evaluate(`(() => {
    const cb = document.querySelector('#cm-showall');
    if (cb && cb.checked) { cb.checked = false; cb.dispatchEvent(new Event('change', { bubbles: true })); }
    const s = document.querySelector('#cm-search');
    s.value = ''; s.dispatchEvent(new Event('input', { bubbles: true }));
  })()`)
  await sleep(300)

  // ---- step 5: selection --------------------------------------------------
  await click('#btn-select')
  await sleep(800)
  check('advanced to step 5 (selection)', await activeStep() === '5')
  // Wait for the step to settle: the metric picks render, every metric's period
  // selects exist, and the wiring note has been written. Reading before the
  // market-mapping refresh lands yields empty selects and a blank note on a
  // panel that is actually correct.
  const settled = await waitFor(`(() => {
    const picks = document.querySelectorAll('#c-metric-picks input[data-metric]').length
    const periods = document.querySelectorAll('#c-periods [data-per="a_current"]').length
    const note = (document.querySelector('#c-metric-note') || {}).innerText || ''
    return picks === 3 && periods >= 1 && /Wired:/i.test(note)
  })()`, 90000)
  if (!settled) console.log('  ! step 5 did not settle; reading what is there')
  // The metric choice is now a set of checkboxes (Sales Value / Volume / ND),
  // each with its own per-side family wiring and its own period pair.
  // The period selects are re-created by every buildSelectionUI() repaint, so read
  // them only once all four exist AND carry a value. A single evaluate() here
  // captures whichever frame it lands in and reports empty strings on a panel
  // that is in fact correct - which is what this check used to do. Note the
  // selects are identified by `data-per`, not by an id.
  const PERIODS = ['a_prior', 'a_current', 'b_prior', 'b_current']
  const periodsRead = `(() => {
    const g = p => {
      const el = document.querySelector('#c-periods [data-per="' + p + '"]')
      return el ? el.value : null
    }
    return ${JSON.stringify(PERIODS)}.map(g)
  })()`
  const periodsWired = await waitFor(`(() => {
    const v = ${periodsRead}
    return v.every(x => x)
  })()`, 20000)
  const periodVals = await evaluate(periodsRead)
  const sel = await evaluate(`({
    picks: [...document.querySelectorAll('#c-metric-picks input[data-metric]')]
             .map(c => ({ key: c.dataset.metric, on: c.checked, disabled: c.disabled })),
    onCount: document.querySelectorAll('#c-metric-picks input[data-metric]:checked').length,
    wireCtrls: document.querySelectorAll('#c-metric-wiring [data-wire]').length,
    note: document.querySelector('#c-metric-note')?.innerText,
    rule: document.querySelector('#c-metric-rule')?.innerText,
    markets: document.querySelectorAll('#c-markets .chip').length,
    cats: document.querySelectorAll('#c-categories .chip').length,
    selected: document.querySelectorAll('#c-categories .chip.on').length,
    count: document.querySelector('#c-cat-count')?.textContent,
  })`)
  console.log('  selection:', JSON.stringify(sel), 'periods:', JSON.stringify(periodVals))
  check('the three metrics are offered as picks',
        sel.picks.length === 3
          && sel.picks.map(p => p.key).join(',') === 'sales_value,volume,nd',
        sel.picks.map(p => `${p.key}${p.on ? '+' : ''}`).join(' '))
  check('Sales Value is picked by default',
        sel.picks.find(p => p.key === 'sales_value')?.on === true,
        `on=${sel.onCount}`)
  check('every metric block is wired on both sides',
        sel.wireCtrls >= 2, `${sel.wireCtrls} wiring control(s) for 1 metric`)
  check('all four period columns auto-wired on both sides',
        periodsWired && periodVals.every(Boolean),
        `wired=${periodsWired} · ${periodVals.join(' / ')}`)
  // The selects hold *variant keys* (YA / VALUE); the request must carry the real
  // column names the family maps them to. Sending the bare key is what made the
  // server refuse the run with "column 'YA' does not exist" while this panel
  // looked perfectly wired.
  const blocks = await evaluate(
    `JSON.stringify((window.buildMetricBlocks || (() => []))())`)
  const parsed = JSON.parse(blocks || '[]')
  const known = await evaluate(`(() => {
    const p = window.S.profile;
    const cols = side => {
      const s = new Set();
      Object.values(p[side].metric_families || {}).forEach(f =>
        Object.values(f || {}).forEach(c => c && s.add(c)));
      return [...s];
    };
    return { a: cols('a'), b: cols('b') };
  })()`)
  const badCols = parsed.flatMap(b => [
    ['a_prior', b.a_prior, known.a], ['a_current', b.a_current, known.a],
    ['b_prior', b.b_prior, known.b], ['b_current', b.b_current, known.b],
  ]).filter(([, v, list]) => !v || !list.includes(v))
  check('the request carries real column names, not variant keys',
        parsed.length > 0 && badCols.length === 0,
        badCols.length ? JSON.stringify(badCols) : JSON.stringify(
          parsed.map(b => `${b.key}:${b.a_current}/${b.b_current}`)))
  check('wiring is reported as valid, not silently broken',
        /Wired:/i.test(sel.note || ''), (sel.note || '').slice(0, 70))
  check('markets rendered as chips', sel.markets >= 4, `${sel.markets}`)
  // The scope must default to the Total Market, not to every market value:
  // the channels are a subset of the Total, so selecting both double-counts.
  const mktScope = await evaluate(`(() => {
    const on = [...document.querySelectorAll('#c-markets .chip.on')].map(c => c.dataset.market);
    const all = [...document.querySelectorAll('#c-markets .chip')].map(c => c.dataset.market);
    return { on, all, level: window.S.marketLevel,
             levels: window.S.marketLevels || {},
             baseline: window.S.baselineMarket || '' };
  })()`)
  const expectTotal = Object.entries(mktScope.levels)
    .filter(([, l]) => l === 'total').map(([n]) => n)
  check('the market scope defaults to the Total Market only',
        expectTotal.length === 0
          || (mktScope.on.length === expectTotal.length
              && expectTotal.every(n => mktScope.on.includes(n))),
        `on=[${mktScope.on.join(', ')}] of ${mktScope.all.length} · ` +
        `total=[${expectTotal.join(', ')}] · level=${mktScope.level}`)
  check('a baseline market was chosen for the shares',
        !!mktScope.baseline || expectTotal.length === 0,
        `baseline=${mktScope.baseline || '(none)'}`)
  check('categories come from the confirmed mapping', sel.cats >= 100, `${sel.cats}`)
  check('all categories selected by default', sel.selected === sel.cats,
        `${sel.selected}/${sel.cats}`)
  await shot('06-step5-selection')

  // Ticking a second metric must add its wiring and period block, not replace
  // the first, and the run must stay valid throughout.
  const beforeAdd = await evaluate(`({
    periods: document.querySelectorAll('#c-periods .wire-card').length,
    wireCards: document.querySelectorAll('#c-metric-wiring .wire-card').length })`)
  await evaluate(`(() => {
    const cb = document.querySelector('#c-metric-picks input[data-metric="volume"]');
    if (cb.disabled) return 'disabled';
    cb.click(); return 'clicked';
  })()`)
  // Ticking a metric repaints #c-periods, so wait for the *second* period block
  // and for both of its a_current selects to carry a value before reading.
  const twoWired = await waitFor(`(() => {
    if (document.querySelectorAll('#c-periods .wire-card').length < 2) return false
    const cur = [...document.querySelectorAll('#c-periods [data-per="a_current"]')]
    return cur.length === 2 && cur.every(el => !!el.value)
  })()`, 20000)
  const afterAdd = await evaluate(`({
    periods: document.querySelectorAll('#c-periods .wire-card').length,
    wireCards: document.querySelectorAll('#c-metric-wiring .wire-card').length,
    aCurrents: [...document.querySelectorAll('#c-periods [data-per="a_current"]')]
                 .map(el => el.value).filter(Boolean),
    note: document.querySelector('#c-metric-note')?.innerText,
    onCount: document.querySelectorAll('#c-metric-picks input[data-metric]:checked').length })`)
  console.log('  metric add:', JSON.stringify({ before: beforeAdd, after: afterAdd,
    twoWired }))
  check('ticking a second metric adds a second period block, not a swap',
        afterAdd.periods === beforeAdd.periods + 1
          && afterAdd.onCount === 2,
        `periods ${beforeAdd.periods} -> ${afterAdd.periods}, on=${afterAdd.onCount}`)
  check('the second metric is auto-wired too',
        twoWired && afterAdd.aCurrents.length === 2,
        `wired=${twoWired} · current=[${afterAdd.aCurrents.join(', ')}]`)
  check('the run is still reported valid with two metrics',
        /Wired:/i.test(afterAdd.note || ''), (afterAdd.note || '').slice(0, 90))

  // ND is the special one: ticking it must say, in the UI, that no growth is
  // computed - the user asked for that distinction explicitly.
  await evaluate(`(() => {
    const cb = document.querySelector('#c-metric-picks input[data-metric="nd"]');
    if (cb.disabled) return 'disabled';
    cb.click(); return 'clicked';
  })()`)
  await sleep(500)
  const ndState = await evaluate(`({
    onCount: document.querySelectorAll('#c-metric-picks input[data-metric]:checked').length,
    rule: document.querySelector('#c-metric-rule')?.innerText,
    noGrowthTags: document.querySelectorAll('#c-periods .tag.warn').length,
    periods: document.querySelectorAll('#c-periods .wire-card').length })`)
  console.log('  nd tick:', JSON.stringify(ndState))
  check('ticking ND is allowed and yields three metrics',
        ndState.onCount === 3 && ndState.periods === 3,
        `on=${ndState.onCount} periods=${ndState.periods}`)
  check('the UI states ND carries no growth',
        /no percentage growth|no growth/i.test(ndState.rule || '')
          || ndState.noGrowthTags >= 1,
        (ndState.rule || '').slice(0, 110))
  await shot('06b-step5-three-metrics')

  // Narrow to Sales Value only again, so the rest of the walk matches the
  // single-metric path the export checks assume.
  await evaluate(`(() => {
    for (const k of ['volume', 'nd']) {
      const cb = document.querySelector('#c-metric-picks input[data-metric="' + k + '"]');
      if (cb && cb.checked) cb.click();
    }
  })()`)
  await sleep(500)
  const backToOne = await evaluate(`({
    onCount: document.querySelectorAll('#c-metric-picks input[data-metric]:checked').length,
    periods: document.querySelectorAll('#c-periods .wire-card').length })`)
  check('unticking returns to a single metric',
        backToOne.onCount === 1 && backToOne.periods === 1,
        `on=${backToOne.onCount} periods=${backToOne.periods}`)

  // narrow to 4 categories deterministically
  const TARGETS = ['BEER', 'SNACK', 'YOGURT', 'HEALTH FOOD']
  await click('#c-cat-none')
  await waitFor(
    `document.querySelectorAll('#c-categories .chip.on').length === 0`, 15000)
  for (const t of TARGETS) {
    const r = await evaluate(`(() => {
      const el = document.querySelector('#c-categories .chip[data-cat="' + ${JSON.stringify(t)} + '"]')
      if (!el) return 'NOT_FOUND'
      el.scrollIntoView({ block: 'center' }); el.click(); return 'ok'
    })()`)
    if (r !== 'ok') console.log('    note: could not click', t, r)
    await sleep(250)
  }
  // Wait for the selection to take rather than assuming the clicks landed.
  await waitFor(
    `document.querySelectorAll('#c-categories .chip.on').length === ${TARGETS.length}`,
    15000)
  const narrowed = await evaluate(
    `document.querySelectorAll('#c-categories .chip.on').length`)
  check('category selection narrows to 4', narrowed === 4, `${narrowed}`)
  const countText = await evaluate(`document.querySelector('#c-cat-count')?.textContent`)
  check('file-count preview tracks the selection', /4 of \d+/.test(countText || ''),
        String(countText))
  await setInput('#c-client-input', 'WEIDER')
  await click('#c-client-add')
  await sleep(400)
  const clientChips = await evaluate(
    `document.querySelectorAll('#c-clients .chip').length`)
  check('client brand chip added', clientChips === 1, `${clientChips}`)
  await shot('07-step5-narrowed')

  // ---- step 6: run + QC ---------------------------------------------------
  const dialogsBeforeRun = dialogs.length
  await click('#btn-run')
  for (let i = 0; i < 240; i++) {
    await sleep(1000)
    if (await activeStep() === '6') break
  }
  check('advanced to step 6 (analysis)', await activeStep() === '6')
  // If doRun() bailed via alert() the step never advances; say why rather than
  // reporting a bare timeout.
  check('the run was not blocked by a dialog',
        dialogs.length === dialogsBeforeRun,
        dialogs.slice(dialogsBeforeRun).join(' | ').slice(0, 200) || 'none')
  await sleep(1500)
  await assertOverlayCleared('analysis')
  // the primary action must be genuinely clickable, not covered by a scrim
  const hit = await hitTest('#btn-export')
  check('primary action is reachable by a real pointer', hit.reachable === true,
        JSON.stringify(hit))
  const run = await evaluate(`({
    kpis: document.querySelectorAll('#run-out .kpi').length,
    tabs: document.querySelectorAll('#run-tabs .tab').length,
    tables: document.querySelectorAll('#run-body table').length,
    banner: document.querySelector('#run-out .notice')?.innerText,
    body: document.querySelector('#run-body').innerText,
  })`)
  console.log('  run:', JSON.stringify({ kpis: run.kpis, tabs: run.tabs,
    tables: run.tables }))
  const nSel = TARGETS.length
  check('KPI cards rendered', run.kpis >= 5, `${run.kpis}`)
  check('a tab per category plus QC', run.tabs === nSel + 1,
        `${run.tabs} (expected ${nSel + 1})`)
  check('analysis tables rendered', run.tables >= 3, `${run.tables}`)
  check('QC banner rendered', /QC/.test(run.banner || ''), (run.banner || '').slice(0, 70))
  await shot('08-step6-analysis')

  // the channel block must actually carry BEFORE/AFTER headers
  const chanHead = await evaluate(`(() => {
    const t = document.querySelector('#run-body table')
    return t ? [...t.querySelectorAll('th')].map(h => h.innerText.trim()).join('|') : ''
  })()`)
  check('channel block has BEFORE and AFTER headers',
        /BEFORE/.test(chanHead) && /AFTER/.test(chanHead), chanHead.slice(0, 90))

  // QC tab
  await evaluate(`(() => {
    const t = [...document.querySelectorAll('#run-tabs .tab')]
      .find(x => /^QC/.test(x.textContent.trim()))
    if (t) t.click()
  })()`)
  await sleep(900)
  const qc = await evaluate(`({
    rows: document.querySelectorAll('#run-body .qc-row').length,
    passes: document.querySelectorAll('#run-body .qc-row.PASS').length,
    fails: document.querySelectorAll('#run-body .qc-row.FAIL').length,
  })`)
  console.log('  qc:', JSON.stringify(qc))
  check('QC checks listed', qc.rows >= 10, `${qc.rows}`)
  check('QC shows no failures', qc.fails === 0, `${qc.fails} fail`)
  await shot('09-step6-qc')

  // ---- text sanity sweep --------------------------------------------------
  const bad = await evaluate(`(() => {
    const t = document.body.innerText
    const hits = []
    for (const pat of ['undefined', 'NaN', '[object Object]', 'null%', '–%']) {
      if (t.includes(pat)) hits.push(pat)
    }
    return hits
  })()`)
  check('no undefined/NaN/[object Object] in rendered text', bad.length === 0,
        bad.join(', '))

  // ---- step 7: export -----------------------------------------------------
  await click('#btn-export')
  await sleep(1200)
  check('advanced to step 7 (export)', await activeStep() === '7')
  await setInput('#e-name', 'ui_verification_run')
  await click('#btn-export-run')
  for (let i = 0; i < 300; i++) {
    await sleep(1000)
    const done = await evaluate(
      `!!document.querySelector('#export-out .notice')`)
    if (done) break
  }
  await sleep(1200)
  const exp = await evaluate(`({
    notice: document.querySelector('#export-out .notice')?.innerText,
    stats: document.querySelectorAll('#export-out .stat').length,
    rows: document.querySelectorAll('#export-out .file-row').length,
    text: document.querySelector('#export-out').innerText.slice(0, 300),
  })`)
  console.log('  export:', JSON.stringify({ stats: exp.stats, rows: exp.rows }))
  check('export summary rendered', !!exp.notice, (exp.notice || '').slice(0, 80))
  check('export lists produced files', exp.rows === nSel * 2 + 1,
        `${exp.rows} file rows (expected ${nSel * 2 + 1})`)
  check('export reports both file types', /xlsx|Excel/.test(exp.text))
  check('export completeness QC passed',
        /0 fail/.test(exp.notice || ''), (exp.notice || '').slice(0, 110))
  await assertOverlayCleared('export')
  await shot('10-step7-export')

  // ---- console hygiene ----------------------------------------------------
  const errs = errorReport()
  console.log('\n  console errors:', errs.consoleErrors.length,
    '| exceptions:', errs.exceptions.length,
    '| log errors:', errs.logErrors.length)
  for (const e of errs.consoleErrors.slice(0, 5)) console.log('    console:', e.slice(0, 160))
  for (const e of errs.exceptions.slice(0, 5)) console.log('    except :', e.slice(0, 160))
  for (const e of errs.logErrors.slice(0, 5)) console.log('    log    :', e.slice(0, 160))
  check('no uncaught exceptions', errs.exceptions.length === 0)
  check('no failed network requests', errs.logErrors.length === 0)

  console.log('\n' + '='.repeat(74))
  console.log(failures === 0 ? ' ALL UI CHECKS PASSED' : ` ${failures} UI CHECK(S) FAILED`)
  console.log('='.repeat(74))
  close()
  process.exit(failures ? 1 : 0)
}

main().catch((e) => { console.error('DRIVER FAILED:', e.message); process.exit(1) })
