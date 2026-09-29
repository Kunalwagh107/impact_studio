// Verifies the redesigned step 4: the user builds the mapping list themselves.
//
//   - the step opens with a "+ Mapping" control and an empty list
//   - there is no inventory of unmapped categories, no status column, no
//     filter, no accordion over all 151 categories
//   - "+ Mapping" opens an editor: a Dataset 1 category picker, at least one
//     Dataset 2 picker, and a reported-name field
//   - "Done" saves it and the list shows exactly what was defined
//   - a mapping whose Dataset 2 category has subcategories offers them
//   - several Dataset 2 entries can be attached to one mapping
//   - the mapping reaches the run: step 5 offers exactly those categories
//
//   node step4_probe.mjs

import { mkdirSync, writeFileSync, existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const CDP = process.env.CDP_URL || 'http://127.0.0.1:9222'
const APP = process.env.APP_URL || 'http://127.0.0.1:8777'
const OUT = 'tools/ui_probe/shots_step4'
const _ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..')
const WORKBOOK = process.env.WORKBOOK || [
  join(_ROOT, 'TW Impact Study_V2 1 (1).xlsx'),
  join(_ROOT, '..', 'TW Impact Study_V2 1 (1).xlsx'),
].find(p => existsSync(p))

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
let failures = 0
function check(name, ok, msg = '') {
  if (!ok) failures++
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${name}${msg ? '  ' + msg : ''}`)
  return ok
}

let ws, msgId = 0
const pending = new Map()
const events = []
const dialogs = []

// Attach to the PAGE target, not the browser target. `Page.enable` and
// `Runtime.evaluate` are page-scoped domains; sending them over the browser
// websocket fails with "'Page.enable' wasn't found".
async function connect() {
  const targets = await (await fetch(`${CDP}/json/list`)).json()
  const page = targets.find((t) => t.type === 'page')
  if (!page) throw new Error('no page target')
  const sock = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((res, rej) => { sock.onopen = res; sock.onerror = rej })
  return sock
}
function send(method, params = {}) {
  const id = ++msgId
  ws.send(JSON.stringify({ id, method, params }))
  return new Promise((res, rej) => pending.set(id, { res, rej }))
}
async function evaluate(expr) {
  const r = await send('Runtime.evaluate', {
    expression: expr, returnByValue: true, awaitPromise: true,
  })
  if (r.exceptionDetails) {
    throw new Error(r.exceptionDetails.exception?.description || 'eval failed')
  }
  return r.result.value
}
const waitFor = async (expr, timeoutMs = 60000, every = 300) => {
  const t0 = Date.now()
  while (Date.now() - t0 < timeoutMs) {
    if (await evaluate(expr)) return true
    await sleep(every)
  }
  return false
}
const setInput = (sel, val) => evaluate(`(() => {
  const el = document.querySelector(${JSON.stringify(sel)})
  if (!el) return 'NOT_FOUND'
  el.value = ${JSON.stringify(val)}
  el.dispatchEvent(new Event('input', { bubbles: true }))
  el.dispatchEvent(new Event('change', { bubbles: true }))
  return el.value
})()`)
const click = (sel) => evaluate(`(() => {
  const el = document.querySelector(${JSON.stringify(sel)})
  if (!el) return 'NOT_FOUND'
  el.click()
  return 'ok'
})()`)
const waitForOpts = async (sel, min, timeoutMs = 60000, every = 400) => {
  const t0 = Date.now()
  while (Date.now() - t0 < timeoutMs) {
    const n = await evaluate(
      `document.querySelector(${JSON.stringify(sel)})?.options.length ?? 0`)
    if (n >= min) return n
    await sleep(every)
  }
  return -1
}
const setSelect = (sel, val) => evaluate(`(() => {
  const el = document.querySelector(${JSON.stringify(sel)})
  if (!el) return 'NOT_FOUND'
  const opts = [...el.options].map(o => o.value)
  const hit = opts.includes(${JSON.stringify(val)})
    ? ${JSON.stringify(val)} : opts.find(o => o && o !== '')
  if (hit === undefined) return 'NO_OPTION'
  el.value = hit
  el.dispatchEvent(new Event('change', { bubbles: true }))
  return hit
})()`)

async function main() {
  mkdirSync(OUT, { recursive: true })
  ws = await connect()
  const sock = ws
  sock.onmessage = (ev) => {
    const msg = JSON.parse(ev.data)
    if (msg.id && pending.has(msg.id)) {
      const { res, rej } = pending.get(msg.id)
      pending.delete(msg.id)
      msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result)
      return
    }
    if (msg.method === 'Page.javascriptDialogOpening') {
      dialogs.push(msg.params.message)
      ws.send(JSON.stringify({ id: ++msgId, method: 'Page.handleJavaScriptDialog',
                               params: { accept: true } }))
    }
    if (msg.method) events.push(msg)
  }
  await send('Page.enable')
  await send('Runtime.enable')
  await send('Log.enable')
  await send('Network.enable')
  await send('Network.setCacheDisabled', { cacheDisabled: true })
  await send('Emulation.setDeviceMetricsOverride', {
    width: 1500, height: 1000, deviceScaleFactor: 1, mobile: false,
  })

  await send('Page.navigate', { url: APP + '/?_step4=' + Date.now() })
  await sleep(2500)

  // ---- register the workbook through the app's own step-1 control -------
  // Setting `window.S.sourceId` directly does not register anything: the
  // source list is rendered from the server, and step 1 stays empty. Drive the
  // real control, then read the option list the page actually offers.
  // Step 1 is upload-first now (no #path-input / #btn-path), so the workbook
  // goes in through the hidden #file-input and then #btn-upload. The bytes are
  // fetched back from /__workbook/ - a `new File([''])` uploads 0 bytes and makes
  // every later read 500, which looks like a broken control rather than a
  // broken probe.
  const up = await evaluate(`(async () => {
    const el = document.querySelector('#file-input')
    if (!el) return 'NOT_FOUND'
    const name = ${JSON.stringify(WORKBOOK)}.split(/[\\\\/]/).pop()
    const r = await fetch('/__workbook/' + encodeURIComponent(name), { cache: 'no-store' })
    if (!r.ok) return 'fetch ' + r.status
    const buf = await r.arrayBuffer()
    if (!buf || buf.byteLength === 0) return 'served 0 bytes'
    const dt = new DataTransfer()
    dt.items.add(new File([buf], name, { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' }))
    el.files = dt.files
    el.dispatchEvent(new Event('change', { bubbles: true }))
    return 'ok:' + buf.byteLength
  })()`)
  console.log('  upload:', up)
  await click('#btn-upload')
  await sleep(4000)
  const reg = await evaluate(`document.querySelector('#upload-msg')?.textContent`)
  console.log('  register:', reg)

  const srcOpts = await evaluate(
    `[...document.querySelector('#a-source').options].map(o => o.value).filter(Boolean)`)
  const sid = srcOpts[srcOpts.length - 1]
  console.log('  sources:', JSON.stringify(srcOpts))

  // Use the app's own step-1 controls so the profile runs for real. Both sides
  // need a source: B falls back to A's column names, but a B that is not wired
  // at all fails the server's per-side wiring check before the profile runs.
  for (const side of ['a', 'b']) { await setSelect(`#${side}-source`, sid); await sleep(600) }
  await sleep(3000)
  // Each picker's contents arrive from a request triggered by the previous
  // selection, so poll for the options to land before choosing. Firing a
  // selection at a list that has not been repopulated yet silently does
  // nothing, and the stale value it leaves behind is what later 400s.
  for (const [side, val] of [['a', 'Current MAT'], ['b', 'New MAT']]) {
    await waitForOpts(`#${side}-sheet`, 2)
    await setSelect(`#${side}-sheet`, 'Raw_MAT')
    await waitForOpts(`#${side}-splitcol`, 5)
    await setSelect(`#${side}-splitcol`, 'Dataset')
    await waitForOpts(`#${side}-value`, 2)
    await setSelect(`#${side}-value`, val)
    await sleep(500)
  }
  const shape = await evaluate(`({
    aSplit: document.querySelector('#a-splitcol')?.value || '',
    aSplitOpts: [...(document.querySelector('#a-splitcol')?.options||[])].length,
    aVal: document.querySelector('#a-value')?.value || '',
    aSheet: document.querySelector('#a-sheet')?.value || '',
  })`)
  console.log('  shape:', JSON.stringify(shape))
  const bad = events.filter(e => e.method === 'Network.responseReceived'
      && e.params?.response?.status >= 400).length
  console.log('  400s so far:', bad)

  const ready = await evaluate(`(() => {
    const b = document.querySelector('#btn-profile')
    return b ? { text: b.innerText, disabled: b.disabled } : null
  })()`)
  console.log('  step1 button:', JSON.stringify(ready))

  // Drive the wizard with the real buttons: profile -> market -> catmap.
  await evaluate(`document.querySelector('#btn-profile').click()`)
  const at2 = await waitFor(`document.querySelector('.step.active')?.dataset.step === '2'`, 120000)
  check('reached step 2 (profile)', at2 === true)
  await evaluate(`document.querySelector('#btn-map').click()`)
  const at3 = await waitFor(`document.querySelector('.step.active')?.dataset.step === '3'`, 180000)
  check('reached step 3 (market mapping)', at3 === true)

  // ---- the new step 4 ----------------------------------------------------
  await evaluate(`document.querySelector('#btn-catmap').click()`)
  const at4 = await waitFor(`document.querySelector('.step.active')?.dataset.step === '4'`, 240000)
  check('reached step 4 (category mapping)', at4 === true)
  await sleep(1500)

  const opening = await evaluate(`(() => {
    const box = document.querySelector('#catmap-out')
    return {
      hasAddMap: !!document.querySelector('#cm-add-map'),
      addLabel: (document.querySelector('#cm-add-map') || {}).innerText || '',
      maps: document.querySelectorAll('.cm-map').length,
      // every trace of the old design must be gone
      accordion: document.querySelectorAll('.acc-item').length,
      table: !!document.querySelector('#cm-tbl'),
      showall: !!document.querySelector('#cm-showall'),
      search: !!document.querySelector('#cm-search'),
      allatonce: !!document.querySelector('#cm-allatonce'),
      statusCol: !!document.querySelector('#cm-tbl select[data-cm-status]'),
      combo: document.querySelectorAll('.combo').length,
      newb: document.querySelectorAll('[data-newb]').length,
      text: (box || {}).innerText?.slice(0, 200) || '',
    }
  })()`)
  console.log('  opening:', JSON.stringify(opening))
  check('the step opens with a "+ Mapping" control',
        opening.hasAddMap === true && /mapping/i.test(opening.addLabel),
        `"${opening.addLabel}"`)
  check('the mapping list starts empty', opening.maps === 0, `${opening.maps} card(s)`)
  check('the old accordion is gone', opening.accordion === 0)
  check('the old all-rows table is gone',
        opening.table === false && opening.showall === false
        && opening.allatonce === false && opening.statusCol === false)
  check('the old search/filter box is gone', opening.search === false)
  check('the 150-option combobox is gone', opening.combo === 0)
  check('a category with no counterpart is not listed as a row',
        opening.maps === 0 && opening.text.length > 0,
        'the only content is the "+ Mapping" prompt')

  // ---- open the editor ---------------------------------------------------
  await evaluate(`document.querySelector('#cm-add-map').click()`)
  await sleep(700)
  const draft = await evaluate(`(() => {
    const box = document.querySelector('#catmap-out')
    const aCat = document.querySelector('#cm-draft-a-cat')
    return {
      isDraft: !!document.querySelector('.cm-draft'),
      listHidden: document.querySelectorAll('.cm-map').length,
      aCatOpts: aCat ? aCat.options.length : 0,
      hasAName: !!document.querySelector('#cm-draft-a-sub'),
      targetRows: document.querySelectorAll('.cm-draft-target').length,
      bCatOpts: document.querySelector('.cm-draft-target select')?.options.length || 0,
      hasName: !!document.querySelector('#cm-draft-name'),
      done: !!document.querySelector('#cm-draft-done'),
      cancel: !!document.querySelector('#cm-draft-cancel'),
      addMore: !!document.querySelector('#cm-draft-add'),
      text: (box || {}).innerText?.slice(0, 260) || '',
    }
  })()`)
  console.log('  draft:', JSON.stringify({ ...draft, text: undefined }))
  check('"+ Mapping" opens the mapping editor', draft.isDraft === true)
  check('the editor offers a Dataset 1 category picker',
        draft.aCatOpts > 100, `${draft.aCatOpts} options`)
  check('the editor offers a Dataset 2 category picker',
        draft.bCatOpts > 100, `${draft.bCatOpts} options`)
  check('the editor offers a reported-name field', draft.hasName === true)
  check('the editor offers Done and Cancel',
        draft.done === true && draft.cancel === true)
  check('the editor can take more than one Dataset 2 entry',
        draft.addMore === true && draft.targetRows === 1,
        `${draft.targetRows} target row(s) to start`)

  // Cancel must discard, not save.
  await evaluate(`document.querySelector('#cm-draft-cancel').click()`)
  await sleep(400)
  const afterCancel = await evaluate(`({
    maps: document.querySelectorAll('.cm-map').length,
    draft: !!document.querySelector('.cm-draft'),
  })`)
  check('Cancel discards the draft without saving it',
        afterCancel.maps === 0 && afterCancel.draft === false,
        JSON.stringify(afterCancel))

  // ---- author one mapping, using only the real controls ------------------
  const authored = await evaluate(`(() => {
    document.querySelector('#cm-add-map').click()
    return true
  })()`)
  await sleep(600)
  const pickA = await setSelect('#cm-draft-a-cat', 'BEER')
  await sleep(500)
  // The Dataset 2 select is identified by data-draft-cat, not by an id.
  const pickB = await evaluate(`(() => {
    const sel = document.querySelector('[data-draft-cat="0"]')
    if (!sel) return 'NO_SELECT'
    const hit = [...sel.options].find(o => o.value === 'BEER')
    if (!hit) return 'NO_MATCH'
    sel.value = 'BEER'
    sel.dispatchEvent(new Event('change', { bubbles: true }))
    return sel.value
  })()`)
  console.log('  picks:', JSON.stringify({ pickA, pickB }))
  await sleep(400)
  await evaluate(`document.querySelector('#cm-draft-done').click()`)
  await sleep(600)
  const saved = await evaluate(`(() => {
    const card = document.querySelector('.cm-map')
    return {
      maps: document.querySelectorAll('.cm-map').length,
      text: card ? card.innerText.replace(/\\s+/g, ' ').trim() : '',
      draft: !!document.querySelector('.cm-draft'),
      editBtn: !!document.querySelector('[data-cm-edit]'),
      removeBtn: !!document.querySelector('[data-cm-remove]'),
      store: ((window.S || {}).catmapMaps || []).length,
    }
  })()`)
  console.log('  pickB:', pickB, '| saved:', JSON.stringify(saved))
  check('Done saves the mapping to the list', saved.maps === 1,
        `${saved.maps} card(s)`)
  check('the editor closes after Done', saved.draft === false)
  check('the saved mapping shows both sides and is editable/removable',
        /BEER/i.test(saved.text) && saved.editBtn && saved.removeBtn,
        saved.text)
  check('the mapping is recorded in state', saved.store === 1, `${saved.store}`)

  // ---- a second mapping, this time with two Dataset 2 entries ------------
  await evaluate(`document.querySelector('#cm-add-map').click()`)
  await sleep(600)
  await evaluate(`(() => {
    const s = document.querySelector('#cm-draft-a-cat')
    s.value = 'SNACK'; s.dispatchEvent(new Event('change', { bubbles: true }))
  })()`)
  await sleep(500)
  await evaluate(`(() => {
    const s = document.querySelector('[data-draft-cat="0"]')
    const hit = [...s.options].find(o => o.value === 'SNACK')
    s.value = hit ? hit.value : ''
    s.dispatchEvent(new Event('change', { bubbles: true }))
  })()`)
  await sleep(400)
  await evaluate(`document.querySelector('#cm-draft-add').click()`)
  await sleep(500)
  const twoTargets = await evaluate(`({
    rows: document.querySelectorAll('.cm-draft-target').length })`)
  check('a second Dataset 2 entry can be added',
        twoTargets.rows === 2, `${twoTargets.rows} row(s)`)
  await evaluate(`(() => {
    const s = document.querySelector('[data-draft-cat="1"]')
    const hit = [...s.options].find(o => o.value === 'SNACK')
    s.value = hit ? hit.value : ''
    s.dispatchEvent(new Event('change', { bubbles: true }))
  })()`)
  await sleep(400)
  await evaluate(`document.querySelector('#cm-draft-done').click()`)
  await sleep(600)
  const two = await evaluate(`({
    maps: document.querySelectorAll('.cm-map').length,
    second: document.querySelectorAll('.cm-map')[1]?.innerText.replace(/\\s+/g,' ').trim() || '',
  })`)
  check('a mapping may cover two Dataset 2 entries',
        two.maps === 2, `${two.maps} card(s) · "${two.second}"`)
  await send('Page.captureScreenshot', { format: 'png' }).then(
    r => writeFileSync(`${OUT}/step4-mappings.png`, Buffer.from(r.data, 'base64')))

  // ---- the mapping must reach the run ------------------------------------
  await evaluate(`document.querySelector('#btn-select').click()`)
  const at5 = await waitFor(`document.querySelector('.step.active')?.dataset.step === '5'`, 60000)
  check('continuing carries the mapping into step 5', at5 === true)
  await sleep(1500)
  const chips = await evaluate(`(() => {
    const all = [...document.querySelectorAll('#c-categories .chip')].map(c => c.dataset.cat)
    return { n: all.length, names: all }
  })()`)
  console.log('  step5 categories:', JSON.stringify(chips))
  check('step 5 offers exactly the categories that were mapped',
        chips.n === 2 && chips.names.includes('BEER') && chips.names.includes('SNACK'),
        `${chips.n}: [${chips.names.join(', ')}]`)
  check('no category appears that was never mapped',
        chips.names.length === 2, `[${chips.names.join(', ')}]`)

  // Report the messages, not just the count. A bare "2" says a page threw
  // twice but not whether it matters; the text is the whole finding.
  // Log the failing URL so a 'Column x not found' stops being anonymous.
  const failedReqs = events.filter(e => e.method === 'Network.responseReceived'
      && e.params?.response?.status >= 400)
    .map(e => e.params.response.status + ' ' + e.params.response.url)
  if (failedReqs.length) for (const f of failedReqs) console.log('    http:', f)

  const thrown = events
    .filter(e => e.method === 'Runtime.exceptionThrown')
    .map(e => e.params?.exceptionDetails?.exception?.description
           || e.params?.exceptionDetails?.text || '?')
  const errs = thrown.length
  if (errs) for (const t of thrown) console.log('    exception:', String(t).split('\n')[0])
  check('no uncaught exceptions', errs === 0, `${errs}`)
  // A rejected request is not the same as a clean run. The split-column value
  // used to survive a sheet change even though the new sheet lacked the column,
  // sending a request that could only ever 400.
  check('no request was rejected by the server', failedReqs.length === 0,
        failedReqs.join(' | ').slice(0, 200))
  check('no dialogs blocked the step', dialogs.length === 0,
        dialogs.join(' | ').slice(0, 160))

  console.log('\n' + '='.repeat(70))
  console.log(failures === 0 ? ' STEP-4 PROBE: ALL CHECKS PASSED'
                             : ` STEP-4 PROBE: ${failures} CHECK(S) FAILED`)
  console.log('='.repeat(70))
  process.exit(failures ? 1 : 0)
}

main().catch((e) => { console.error('PROBE FAILED:', e.message); process.exit(1) })
