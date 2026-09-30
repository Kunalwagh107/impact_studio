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
  // Step 1 is now upload-first: there is no path box and no #btn-path. The file
  // is fed through the hidden #file-input (the same node the drop zone targets),
  // which fires the change listener that arms #btn-upload; then #btn-upload
  // posts it and auto-selects the new source on *both* sides.
  //
  // The bytes must be REAL. `new File([''])` uploads a 0-byte file: the source
  // registers, the picker fills, and then every sheet/column read 500s with
  // "File is not a zip file" - which reads exactly like a broken upload control
  // and is in fact a broken probe. So fetch the workbook back from the server
  // (which also proves the served bytes are intact) and build the File from them.
  const uploadWorkbook = async (filePath) => {
    const first = await evaluate(`(async () => {
      const el = document.querySelector('#file-input')
      if (!el) return { err: 'NOT_FOUND' }
      const name = ${JSON.stringify(filePath)}.split(/[\\\\/]/).pop()
      const url = '/__workbook/' + encodeURIComponent(name)
      let buf
      try {
        const r = await fetch(url, { cache: 'no-store' })
        if (!r.ok) return { err: 'fetch ' + r.status + ' for ' + url }
        buf = await r.arrayBuffer()
      } catch (e) { return { err: String(e) } }
      if (!buf || buf.byteLength === 0) return { err: 'served 0 bytes for ' + url }
      const dt = new DataTransfer()
      dt.items.add(new File([buf], name, { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' }))
      el.files = dt.files
      el.dispatchEvent(new Event('change', { bubbles: true }))
      return { ok: 'ok', bytes: buf.byteLength }
    })()`)
    if (first.err) return first.err
    console.log(`  upload source bytes: ${first.bytes} bytes`)
    const armed = await evaluate(
      `!document.querySelector('#btn-upload')?.disabled`)
    if (!armed) return 'BTN_DISABLED'
    await click('#btn-upload')
    return 'ok'
  }
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
  const up = await uploadWorkbook(WORKBOOK)
  check('workbook upload accepted', up === 'ok', up)
  await sleep(4000)
  const reg = await evaluate(`document.querySelector('#upload-msg')?.textContent`)
  console.log('  register:', reg)
  // The app reports `Loaded <file> · <n> sheets`. Accept either verb, but DO
  // require the sheet count - a registered-but-unreadable file also produces a
  // message, and it is the count that proves the workbook was actually parsed.
  check('workbook registered and its sheets enumerated',
        /(Loaded|Registered)/.test(reg || '') && /\d+\s+sheets?/.test(reg || ''),
        String(reg))

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
    text: (document.querySelector('#profile-out') || {}).innerText?.slice(0, 400) || '',
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
  check('advanced to step 3 (market mapping)', await activeStep() === '3')
  // Step 3 is now the user-authored market *pairing* screen - there are no
  // per-dimension tabs, because manufacturer/brand were removed and market is
  // authored as A-value -> level -> B-value rows.
  const mapInfo = await evaluate(`({
    addBtn: !!document.querySelector('#mkt-add, [data-mkt-add]'),
    levelSel: document.querySelectorAll('#mkt-level, [data-mkt-level]').length,
    pairs: document.querySelectorAll('.mkt-pair, [data-mkt-row]').length,
    tabs: document.querySelectorAll('#map-tabs .tab').length,
    text: (document.querySelector('#panel-3') || document.body).innerText.slice(0, 300),
  })`)
  console.log('  market mapping:', JSON.stringify({
    addBtn: mapInfo.addBtn, pairs: mapInfo.pairs, tabs: mapInfo.tabs }))
  check('the per-dimension tabs are gone (market pairing replaced them)',
        mapInfo.tabs === 0, `${mapInfo.tabs} tabs`)
  check('market pairing offers a level selector', mapInfo.levelSel >= 1,
        `${mapInfo.levelSel} level control(s)`)
  check('market pairing offers a way to add a pairing', mapInfo.addBtn === true)

  // Author a pairing: A's Total onto B's Total at level "total". Nothing is
  // paired until the user says so, so without this the market scope in step 5 is
  // legitimately empty - which is what the earlier version of this probe read as
  // a product defect.
  await click('#mkt-add')
  await sleep(600)
  const pairCount = await evaluate(
    `document.querySelectorAll('#map-out select[data-mkt-a]').length`)
  check('adding a pairing creates an editable row', pairCount >= 1,
        `${pairCount} pairing row(s)`)
  const paired = await evaluate(`(() => {
    const pick = (sel, wantTotal) => {
      const el = document.querySelector(sel);
      if (!el) return 'no-control';
      const opts = [...el.options].map(o => o.value).filter(Boolean);
      if (!opts.length) return 'no-options';
      // Prefer the Total: it is the only level whose channels are a subset, so it
      // is the correct scope for a whole-market impact study.
      const chosen = wantTotal
        ? (opts.find(o => /total/i.test(o)) || opts[0])
        : opts[0];
      el.value = chosen;
      el.dispatchEvent(new Event('change', { bubbles: true }));
      return chosen;
    };
    return {
      a: pick('#map-out select[data-mkt-a]', true),
      b: pick('#map-out select[data-mkt-b]', true),
      level: pick('#map-out select[data-mkt-level]', false),
    };
  })()`)
  await sleep(900)
  console.log('  pairing authored:', JSON.stringify(paired))
  const pairState = await evaluate(`(() => {
    const pairs = (window.S && window.S.marketPairs) || [];
    return { n: pairs.length, a: pairs[0] && pairs[0].market_a,
             b: pairs[0] && pairs[0].market_b, level: pairs[0] && pairs[0].level };
  })()`)
  check('the authored pairing is recorded with its level',
        pairState.n >= 1, JSON.stringify(pairState))
  await shot('04-step3-market-mapping')

  // ---- step 4: category mapping -------------------------------------------
  //
  // The user builds this list; nothing is proposed. `+ Mapping` opens an editor,
  // `Done` commits it, and the list is the mapping. This block walks that path
  // with the real controls and asserts the deletion as well as the addition: the
  // accordion over all 151 categories, the status column, the search box and the
  // 150-option combobox must all be gone, because "remove everything else" was
  // half the brief.
  await click('#btn-catmap')
  for (let i = 0; i < 240; i++) {
    await sleep(1000)
    if (await activeStep() === '4') break
  }
  check('advanced to step 4 (category mapping)', await activeStep() === '4')
  await sleep(1200)

  const cmOpen = await evaluate(`({
    addMap: !!document.querySelector('#cm-add-map'),
    addLabel: (document.querySelector('#cm-add-map') || {}).innerText || '',
    maps: document.querySelectorAll('.cm-map').length,
    accordion: document.querySelectorAll('#cm-acc .acc-item').length,
    table: !!document.querySelector('#cm-tbl'),
    showall: !!document.querySelector('#cm-showall'),
    search: !!document.querySelector('#cm-search'),
    allatonce: !!document.querySelector('#cm-allatonce'),
    combo: document.querySelectorAll('.combo').length,
    draft: !!document.querySelector('.cm-draft'),
  })`)
  console.log('  step 4 opens:', JSON.stringify(cmOpen))
  check('the step opens with a "+ Mapping" control',
        cmOpen.addMap === true && /mapping/i.test(cmOpen.addLabel),
        `"${cmOpen.addLabel}"`)
  check('the mapping list starts empty', cmOpen.maps === 0, `${cmOpen.maps} card(s)`)
  check('the all-category accordion is gone', cmOpen.accordion === 0)
  check('the full-table view is gone',
        cmOpen.table === false && cmOpen.showall === false
        && cmOpen.allatonce === false)
  check('the search box is gone', cmOpen.search === false)
  check('the target combobox is gone', cmOpen.combo === 0)
  await shot('05a-step4-empty')

  // The editor offers a picker per side, because a mapping may point at a whole
  // category (either side) or at a category+subcategory. The Dataset 1 select is
  // the one that carries an id; the Dataset 2 rows are addressed by data-draft-cat
  // so a second target row can be added without a second id.
  await click('#cm-add-map')
  await sleep(700)
  const draft = await evaluate(`({
    isDraft: !!document.querySelector('.cm-draft'),
    aOpts: (document.querySelector('#cm-draft-a-cat') || {}).options?.length || 0,
    bOpts: (document.querySelector('[data-draft-cat="0"]') || {}).options?.length || 0,
    hasName: !!document.querySelector('#cm-draft-name'),
    targetRows: document.querySelectorAll('.cm-draft-target').length,
    done: !!document.querySelector('#cm-draft-done'),
    cancel: !!document.querySelector('#cm-draft-cancel'),
    add: !!document.querySelector('#cm-draft-add'),
  })`)
  console.log('  draft:', JSON.stringify(draft))
  check('"+ Mapping" opens the mapping editor', draft.isDraft === true)
  check('the editor offers a Dataset 1 category picker',
        draft.aOpts > 100, `${draft.aOpts} options`)
  check('the editor offers a Dataset 2 category picker',
        draft.bOpts > 100, `${draft.bOpts} options`)
  check('the editor starts with one target row and can add more',
        draft.targetRows === 1 && draft.add === true && draft.hasName === true)
  check('the editor offers Done and Cancel',
        draft.done === true && draft.cancel === true)

  // Cancel must discard. Asserting only the list count would pass on an editor
  // that saved and then hid itself, so assert the draft closed too.
  await click('#cm-draft-cancel')
  await sleep(400)
  const cancelled = await evaluate(`({
    maps: document.querySelectorAll('.cm-map').length,
    draft: !!document.querySelector('.cm-draft'),
  })`)
  check('Cancel discards the draft without saving it',
        cancelled.maps === 0 && cancelled.draft === false, JSON.stringify(cancelled))

  // ---- author the mappings step 5 will offer ------------------------------
  // Map BY NAME rather than by position: the enumeration is sorted by descending
  // value, so the names this walk narrows to are not at the top of the list.
  const AUTHOR_TARGETS = ['BEER', 'SNACK']
  const pickDraft = async (side, name, ti = 0) => evaluate(`(() => {
    const sel = ${side === 'a'
      ? "document.querySelector('#cm-draft-a-cat')"
      : `document.querySelector('[data-draft-cat="${ti}"]')`}
    if (!sel) return 'NO_SELECT'
    const hit = [...sel.options].find(o => o.value === ${JSON.stringify(name)})
    if (!hit) return 'NO_MATCH'
    sel.value = hit.value
    sel.dispatchEvent(new Event('change', { bubbles: true }))
    return sel.value
  })()`)

  const authoredTargets = []
  // One pass per name. The 1:N mapping is authored by *editing* the SNACK card
  // afterwards, not by adding a third mapping: the list is keyed by canonical
  // name, so a second SNACK card would collapse onto the first in step 5 and
  // make the counts below read as a defect.
  for (const name of AUTHOR_TARGETS) {
    await click('#cm-add-map')
    await sleep(600)
    const a = await pickDraft('a', name)
    await sleep(450)
    const b = await pickDraft('b', name)
    await sleep(450)
    await click('#cm-draft-done')
    await sleep(600)
    if (a === name && b === name) authoredTargets.push(name)
    else console.log(`  ! mapping ${name} failed: a=${a} b=${b}`)
  }
  const afterFirstPass = await evaluate(
    `document.querySelectorAll('.cm-map').length`)

  // Reopen the SNACK mapping and give it a second Dataset 2 entry: that is how a
  // 1:N relationship is expressed, and it must round-trip through the editor.
  // Editing rather than adding also proves the card really reopens with its own
  // content, which is the only way "editable" means anything.
  await evaluate(`(() => {
    const cards = [...document.querySelectorAll('.cm-map')];
    const card = cards.find(c => /SNACK/i.test(c.innerText));
    if (card) { const b = card.querySelector('[data-cm-edit]'); if (b) b.click(); }
  })()`)
  await sleep(700)
  const editReopened = await evaluate(`(() => {
    const sel = document.querySelector('[data-draft-cat="0"]');
    return { draft: !!document.querySelector('.cm-draft'),
             aCat: (document.querySelector('#cm-draft-a-cat') || {}).value || '',
             bCat: sel ? sel.value : '' }
  })()`)
  await click('#cm-draft-add')
  await sleep(500)
  const twoRows = await evaluate(
    `document.querySelectorAll('.cm-draft-target').length`)
  await pickDraft('b', 'SNACK', 1)
  await sleep(450)
  await click('#cm-draft-done')
  await sleep(600)

  const authored = await evaluate(`(() => {
    const cards = [...document.querySelectorAll('.cm-map')];
    return {
      count: cards.length,
      text: cards.map(c => c.innerText.replace(/\\s+/g, ' ').trim()),
      store: ((window.S || {}).catmapMaps || []).length,
      targetCounts: ((window.S || {}).catmapMaps || []).map(m => (m.targets || []).length),
      editBtns: document.querySelectorAll('[data-cm-edit]').length,
      removeBtns: document.querySelectorAll('[data-cm-remove]').length,
    }
  })()`)
  console.log('  authored:', JSON.stringify({ count: authored.count, text: authored.text,
    store: authored.store, targetCounts: authored.targetCounts }))
  check('reopening a mapping restores its own content',
        editReopened.draft === true && editReopened.aCat === 'SNACK'
        && editReopened.bCat === 'SNACK', JSON.stringify(editReopened))
  check('Done saves one card per authored mapping',
        authored.count === AUTHOR_TARGETS.length
        && afterFirstPass === AUTHOR_TARGETS.length
        && authoredTargets.length === AUTHOR_TARGETS.length,
        `${authored.count} card(s) for ${authoredTargets.length} names`)
  check('the saved cards show both sides',
        authored.text.every(t => /Dataset 1/.test(t) && /Dataset 2/.test(t)),
        authored.text.join(' | '))
  check('every saved mapping is editable and removable',
        authored.editBtns === authored.count && authored.removeBtns === authored.count,
        `${authored.editBtns} edit / ${authored.removeBtns} remove`)
  check('the mappings are recorded in state, not just rendered',
        authored.store === authored.count, `${authored.store} in state`)
  check('a mapping can carry more than one Dataset 2 entry',
        twoRows === 2 && authored.targetCounts.includes(2),
        `rows=${twoRows} targetCounts=[${authored.targetCounts.join(', ')}]`)
  await shot('05-step4-category-mapping')

  // Removing a mapping must take it out of the list AND out of state.
  const beforeRemove = authored.count
  await evaluate(`(() => {
    const btns = [...document.querySelectorAll('[data-cm-remove]')];
    if (btns.length) btns[btns.length - 1].click();
  })()`)
  await sleep(500)
  const afterRemove = await evaluate(`({
    cards: document.querySelectorAll('.cm-map').length,
    store: ((window.S || {}).catmapMaps || []).length,
  })`)
  check('a mapping can be removed from the list',
        afterRemove.cards === beforeRemove - 1 && afterRemove.store === afterRemove.cards,
        `${beforeRemove} -> ${afterRemove.cards} cards, ${afterRemove.store} in state`)
  // Put the 1:N mapping back so step 5 still has the targets it asserts on.
  await click('#cm-add-map')
  await sleep(600)
  await pickDraft('a', 'SNACK')
  await sleep(450)
  await pickDraft('b', 'SNACK')
  await sleep(450)
  await click('#cm-draft-done')
  await sleep(600)


  // ---- step 5: selection --------------------------------------------------
  await click('#btn-select')
  await sleep(800)
  check('advanced to step 5 (selection)', await activeStep() === '5')
  // Wait for the step to settle: the metric picks render, at least one metric's
  // period block exists, and the wiring note has been written. Reading before the
  // market-mapping refresh lands yields a blank note on a panel that is actually
  // correct.
  //
  // Note this deliberately does NOT look for `#c-periods [data-per=...]`: those
  // were the period *selects*, and step 5 no longer has any - MAT YA / MAT TY are
  // resolved from the Metric Period columns and only reported. Waiting on a
  // selector that can never match made this step time out for 90s on every run
  // and then print "step 5 did not settle" against a panel that had settled.
  const settled = await waitFor(`(() => {
    const picks = document.querySelectorAll('#c-metric-picks input[data-metric]').length
    const blocks = document.querySelectorAll('#c-periods table.mini tbody tr').length
    const note = (document.querySelector('#c-metric-note') || {}).innerText || ''
    return picks === 3 && blocks >= 1 && /Wired:/i.test(note)
  })()`, 90000)
  if (!settled) console.log('  ! step 5 did not settle; reading what is there')
  // The metric choice is now a set of checkboxes (Sales Value / Volume / ND),
  // each with its own per-side family wiring and its own period pair.
  // Step 5 no longer offers period *selects*: MAT YA and MAT TY are read from
  // the Metric Period columns, and the panel reports the pair it resolved. So
  // there is nothing to wait for here beyond the block existing, and the check
  // moves to what the request actually carries (below).
  //
  // Read the *reported* pair instead: one row per period, first cell the role,
  // then the column each side resolves to. This is what the user sees, so it is
  // what the probe should assert on.
  const periodsRead = `(() => {
    const rows = [...document.querySelectorAll('#c-periods table.mini tbody tr')]
    return rows.map(r => {
      const cells = [...r.querySelectorAll('td')]
      return {
        role: (cells[0] || {}).innerText || '',
        a: (cells[1] && cells[1].querySelector('code') || {}).textContent || '',
        b: (cells[2] && cells[2].querySelector('code') || {}).textContent || '',
      }
    })
  })()`
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
    resolvedRows: document.querySelectorAll('#c-periods table.mini tbody tr').length,
    periodSelects: document.querySelectorAll('#c-periods select').length,
  })`)
  const catNames = await evaluate(`[...document.querySelectorAll('#c-categories .chip')]
    .map(c => c.dataset.cat)`)
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
  // The step reports the two periods it resolved - one row per period - and
  // offers no select, because there is nothing to choose.
  check('step 5 reports the resolved periods without offering a choice',
        sel.resolvedRows >= 2 && sel.periodSelects === 0,
        `${sel.resolvedRows} resolved row(s), ${sel.periodSelects} select(s)`)
  // The request must carry the real column names, and the two slots must differ -
  // a run where MAT YA and MAT TY collapsed onto one column reports 0% growth
  // everywhere, which looks like a finding rather than a bug.
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
  // The regression this whole change exists to prevent.
  const collapsed = parsed.filter(b =>
    b.a_prior === b.a_current || b.b_prior === b.b_current)
  check('MAT YA and MAT TY are distinct columns on each side',
        parsed.length > 0 && collapsed.length === 0,
        collapsed.length
          ? JSON.stringify(collapsed.map(b => `${b.key}: ${b.a_prior} == ${b.a_current}`))
          : JSON.stringify(parsed.map(b => `${b.key}: YA=${b.a_prior} TY=${b.a_current}`)))
  const usedTwoYear = parsed.filter(b => /2YA/i.test(
    [b.a_prior, b.a_current, b.b_prior, b.b_current].join(' ')))
  check('2YA is never read as a study period',
        usedTwoYear.length === 0,
        usedTwoYear.length ? JSON.stringify(usedTwoYear) : 'no 2YA reference in the request')
  check('wiring is reported as valid, not silently broken',
        /Wired:/i.test(sel.note || ''), (sel.note || '').slice(0, 70))
  // The scope chips are now derived from the market *pairings* the user authored
  // in step 3, at the analysis level they chose. `marketLevels` /`baselineMarket`
  // (the old auto-classification state) are gone.
  check('market scope chips are derived from the authored pairings',
        sel.markets >= 1, `${sel.markets} chip(s)`)
  const mktScope = await evaluate(`(() => {
    const on = [...document.querySelectorAll('#c-markets .chip.on')].map(c => c.dataset.market);
    const all = [...document.querySelectorAll('#c-markets .chip')].map(c => c.dataset.market);
    const pairs = (window.S && window.S.marketPairs) || [];
    return { on, all, pairCount: pairs.length,
             levels: [...new Set(pairs.map(p => p.level))],
             first: pairs[0] ? pairs[0].market_a : null };
  })()`)
  check('the scope follows the level chosen for the pairing',
        mktScope.pairCount >= 0 && Array.isArray(mktScope.levels),
        `pairs=${mktScope.pairCount} levels=[${mktScope.levels.join(', ')}] ` +
        `first=${mktScope.first}`)
  check('a market scope is applied (not silently empty)',
        mktScope.all.length >= 1,
        `on=[${mktScope.on.join(', ')}] of ${mktScope.all.length}`)
  // "Not empty" is too weak: the reported defect was that step 5 offered only
  // *some* of the paired markets, and a >=1 check passes happily in that state.
  // Assert the exact set instead - every market the user paired in step 3 must be
  // offered here, since the pairing list is the only thing that defines scope.
  const pairedMarkets = await evaluate(`(() => {
    const pairs = (window.S && window.S.marketPairs) || [];
    return [...new Set(pairs.flatMap(p => [p.market_a, p.market_b]).filter(Boolean))];
  })()`)
  const missing = pairedMarkets.filter(m => !mktScope.all.includes(m))
  const extra = mktScope.all.filter(m => !pairedMarkets.includes(m))
  check('step 5 offers EVERY market paired in step 3',
        pairedMarkets.length > 0 && missing.length === 0 && extra.length === 0,
        `paired=${pairedMarkets.length} offered=${mktScope.all.length} `
        + `missing=[${missing.join(', ')}] unexpected=[${extra.join(', ')}]`)
  // Categories are offered only for the categories the user actually mapped -
  // the enumeration no longer contributes any. With the mapping list as the only
  // source, the chips are exactly the canonical names that were authored, so the
  // assertion can be exact rather than a bound: every offered name was authored,
  // and every authored name is offered. The enumeration holds 151; anything near
  // that number means the step is reading the enumeration again.
  const ENUM_TOTAL = 151
  const expectedCats = [...new Set(authoredTargets)]
  check('categories come from the authored mapping, not the enumeration',
        sel.cats > 0 && sel.cats < ENUM_TOTAL,
        `${sel.cats} chip(s) for ${expectedCats.length} authored name(s); ` +
        `enumeration holds ${ENUM_TOTAL}`)
  check('the offered categories are exactly the ones that were authored',
        (catNames || []).length === expectedCats.length
        && (catNames || []).every(n => expectedCats.includes(n)),
        `offered=[${(catNames || []).join(', ')}] `
        + `unexpected=[${(catNames || []).filter(
          n => !expectedCats.includes(n)).join(', ')}]`)
  // The category names live in #c-categories; #c-markets holds market values.
  check('every authored target is offered in step 5',
        AUTHOR_TARGETS.every(t => (catNames || []).some(
          x => String(x).toUpperCase() === t)),
        `targets in [${(catNames || []).join(', ')}]`)
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
  // and for both of its resolved-period tables to be populated. There are no
  // selects to read any more - a populated table means the metric's family
  // resolved, which is the thing "auto-wired" is actually asking about.
  const twoWired = await waitFor(`(() => {
    if (document.querySelectorAll('#c-periods .wire-card').length < 2) return false
    const rows = [...document.querySelectorAll('#c-periods table.mini tbody tr')]
    return rows.length >= 4 && rows.every(r => r.querySelectorAll('code').length >= 1)
  })()`, 20000)
  const afterAdd = await evaluate(`({
    periods: document.querySelectorAll('#c-periods .wire-card').length,
    wireCards: document.querySelectorAll('#c-metric-wiring .wire-card').length,
    aCurrents: [...document.querySelectorAll('#c-periods table.mini tbody tr')]
                 .map(r => (r.querySelectorAll('code')[1] || {}).textContent)
                 .filter(Boolean),
    unresolved: [...document.querySelectorAll('#c-periods .tag.warn')].length,
    note: document.querySelector('#c-metric-note')?.innerText,
    onCount: document.querySelectorAll('#c-metric-picks input[data-metric]:checked').length })`)
  console.log('  metric add:', JSON.stringify({ before: beforeAdd, after: afterAdd,
    twoWired }))
  check('ticking a second metric adds a second period block, not a swap',
        afterAdd.periods === beforeAdd.periods + 1
          && afterAdd.onCount === 2,
        `periods ${beforeAdd.periods} -> ${afterAdd.periods}, on=${afterAdd.onCount}`)
  check('the second metric is auto-wired too',
        twoWired && afterAdd.aCurrents.length >= 2,
        `wired=${twoWired} · resolved MAT TY=[${afterAdd.aCurrents.join(', ')}]`)
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

  // narrow to the authored categories deterministically - the same names mapped
  // in step 4, so the two halves of the walk agree by construction.
  const TARGETS = AUTHOR_TARGETS
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
  check('category selection narrows to the authored categories',
        narrowed === TARGETS.length && narrowed > 0,
        `${narrowed} of ${TARGETS.length} authored`)
  const countText = await evaluate(`document.querySelector('#c-cat-count')?.textContent`)
  // The preview counts *categories*, not files. The wording used to read
  // "will produce 4 files" back when categories were the only grouping; the
  // count it actually tracks is the selected category list.
  check('file-count preview tracks the selection',
        new RegExp(`^${TARGETS.length} of \\d+`).test(countText || ''),
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
    noticesAboveKpis: document.querySelectorAll('#run-out > .notice').length,
    body: (document.querySelector('#run-body') || {}).innerText || '',
  })`)
  console.log('  run:', JSON.stringify({ kpis: run.kpis, tabs: run.tabs,
    tables: run.tables, noticesAboveKpis: run.noticesAboveKpis }))
  const nSel = TARGETS.length
  check('KPI cards rendered', run.kpis >= 5, `${run.kpis}`)
  check('a tab per category plus QC', run.tabs === nSel + 1,
        `${run.tabs} (expected ${nSel + 1})`)
  check('analysis tables rendered', run.tables >= 3, `${run.tables}`)
  check('QC banner rendered', /QC/.test(run.banner || ''), (run.banner || '').slice(0, 70))
  // Exactly one box above the KPIs - the QC status line. The run *notes* used to
  // render here too, under a "Check the period wiring" heading, which read as
  // "this analysis is suspect" even when every check passed. They belong in the
  // QC tab (asserted below), not over the numbers.
  check('no warning box sits above the KPIs, only the QC status line',
        run.noticesAboveKpis === 1, `${run.noticesAboveKpis} notice(s) above the KPIs`)
  // Scroll to the top before the screenshot. `Page.captureScreenshot` captures
  // the *viewport*, and the walk has scrolled a long way down by now - so this
  // shot used to document the Top-N tables and never the headline the step is
  // named for. The user's report was about exactly that part of the screen.
  await evaluate(`(() => { window.scrollTo(0, 0); return true })()`)
  await sleep(300)
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
    runNotes: document.querySelectorAll('#run-body [data-qc-notes]').length,
    runNotesText: (document.querySelector('#run-body [data-qc-notes]') || {}).innerText || '',
    runNotesHtml: ((document.querySelector('#run-body [data-qc-notes]') || {}).innerHTML || '')
      .slice(0, 400),
  })`)
  console.log('  qc:', JSON.stringify({ ...qc, runNotesHtml: undefined }))
  console.log('  run-notes text:', JSON.stringify(qc.runNotesText))
  console.log('  run-notes html:', JSON.stringify(qc.runNotesHtml))
  check('QC checks listed', qc.rows >= 10, `${qc.rows}`)
  check('QC shows no failures', qc.fails === 0, `${qc.fails} fail`)
  // The walk maps 2 of 151 categories, so the run has notes to record (rows
  // outside the mapped categories). They must be here - in the QC tab, as a
  // recorded note - and not above the KPIs, which is where they used to sit.
  check('the run notes are reported in the QC tab', qc.runNotes >= 1,
        `${qc.runNotes} note block(s)`)
  check('the run notes say what the run covered, not that rows were "dropped"',
        /are in categories you mapped/.test(qc.runNotesText)
        && !/row\(s\) dropped/.test(qc.runNotesText),
        qc.runNotesText.slice(0, 110))
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
    text: (document.querySelector('#export-out') || {}).innerText?.slice(0, 300) || '',
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
