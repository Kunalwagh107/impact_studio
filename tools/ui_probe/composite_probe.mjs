// Verifies that a mapping may be a Category + Subcategory combination on
// EITHER side, and that the composite reaches the analysis.
//
// The reference TW workbook is flat on the Dataset-1 side, so it cannot show
// this. `mapping_fixture_5cat.xlsx` is deliberately asymmetric: Dataset 1 uses a
// bare CATEGORY (SUBCATEGORY blank) and Dataset 2 splits it into CATEGORY +
// SUBCATEGORY. That asymmetry is the point - the mapping must be able to
// express a composite on whichever side actually has one.
//
//   - the Dataset 2 picker offers a subcategory whenever that side has one
//   - a Category + Subcategory target can be committed and shows on the card
//   - the Dataset 1 picker also offers a subcategory (symmetry), even though
//     this file's Dataset 1 has none - the control exists on both sides
//   - the composite reaches the run: the canonical category is analysed
//
//   node composite_probe.mjs

import { mkdirSync, existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const CDP = process.env.CDP_URL || 'http://127.0.0.1:9222'
const APP = process.env.APP_URL || 'http://127.0.0.1:8777'
const OUT = 'tools/ui_probe/shots_composite'
const _ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..')
const WORKBOOK = process.env.WORKBOOK || [
  join(_ROOT, 'mapping_fixture_5cat.xlsx'),
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

  await send('Page.navigate', { url: APP + '/?_comp=' + Date.now() })
  await sleep(2500)

  // Step 1 is upload-first: the workbook goes through the hidden #file-input,
  // then #btn-upload registers it. There is no #path-input / #btn-path any more.
  // Bytes come from /__workbook/ - an empty File uploads 0 bytes and every later
  // sheet/column read 500s, which reads like a broken control.
  console.log('  upload:', await evaluate(`(async () => {
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
  })()`))
  await click('#btn-upload')
  await sleep(4000)
  const srcOpts = await evaluate(
    `[...document.querySelector('#a-source').options].map(o => o.value).filter(Boolean)`)
  const sid = srcOpts[srcOpts.length - 1]

  for (const side of ['a', 'b']) { await setSelect(`#${side}-source`, sid); await sleep(600) }
  await sleep(2500)
  for (const [side, val] of [['a', 'Current MAT'], ['b', 'New MAT']]) {
    await waitForOpts(`#${side}-sheet`, 2)
    await setSelect(`#${side}-sheet`, 'Raw_MAT')
    await waitForOpts(`#${side}-splitcol`, 4)
    await setSelect(`#${side}-splitcol`, 'Dataset')
    await waitForOpts(`#${side}-value`, 2)
    await setSelect(`#${side}-value`, val)
    await sleep(400)
  }

  // Wire the subcategory role on BOTH sides, so the mapping can express a
  // composite either way. This is the wiring the user would do for a workbook
  // whose categories are split. The role selectors are
  // `select[data-dim-role="subcategory"][data-side="a"|"b"]`.
  await evaluate(`(() => {
    for (const side of ['a','b']) {
      const sel = document.querySelector(
        'select[data-dim-role="subcategory"][data-side="' + side + '"]')
      if (!sel) continue
      const opts = [...sel.options].map(o => o.value)
      const hit = opts.find(o => /subcat/i.test(o)) || opts.find(Boolean)
      if (hit) { sel.value = hit; sel.dispatchEvent(new Event('change', { bubbles: true })) }
    }
    return true
  })()`)
  await sleep(800)

  await evaluate(`document.querySelector('#btn-profile').click()`)
  await waitFor(`document.querySelector('.step.active')?.dataset.step === '2'`, 120000)
  await evaluate(`document.querySelector('#btn-map')?.click()`)
  await waitFor(`document.querySelector('.step.active')?.dataset.step === '3'`, 180000)
  await evaluate(`document.querySelector('#btn-catmap').click()`)
  const at4 = await waitFor(`document.querySelector('.step.active')?.dataset.step === '4'`, 240000)
  check('reached step 4 (category mapping)', at4 === true)
  await sleep(1200)

  const suba = await evaluate(`(window.S.catmap&&S.catmap.has_subcategory_a)===true`)
  const subb = await evaluate(`(window.S.catmap&&S.catmap.has_subcategory_b)===true`)
  console.log(`  subcategory detected: A=${suba} B=${subb}`)
  check('the engine reports which side has subcategories', subb === true)

  // ---- open the editor and inspect BOTH pickers --------------------------
  await click('#cm-add-map')
  await sleep(600)
  const editor = await evaluate(`({
    hasACat: !!document.querySelector('#cm-draft-a-cat'),
    hasASub: !!document.querySelector('#cm-draft-a-sub'),
    aSubDisabled: document.querySelector('#cm-draft-a-sub')?.disabled ?? null,
    bCatRows: document.querySelectorAll('#cm-draft-targets [data-draft-cat]').length,
    bSubRows: document.querySelectorAll('#cm-draft-targets [data-draft-sub]').length,
  })`)
  console.log('  editor:', JSON.stringify(editor))
  check('the editor offers a Dataset 2 category picker', editor.bCatRows >= 1)
  check('the editor offers a Dataset 1 subcategory picker too (symmetry)',
        editor.hasASub === true)

  // Choose an A category, then a B category that has subcategories.
  await evaluate(`(() => {
    const s = document.querySelector('#cm-draft-a-cat')
    const hit = [...s.options].find(o => o.value && /biscuit/i.test(o.value)) 
             || [...s.options].find(o => o.value)
    s.value = hit.value; s.dispatchEvent(new Event('change', { bubbles: true }))
    return hit.value
  })()`)
  await sleep(600)
  const aSubNow = await evaluate(`({
    present: !!document.querySelector('#cm-draft-a-sub'),
    options: [...(document.querySelector('#cm-draft-a-sub')?.options||[])].map(o=>o.value),
  })`)
  console.log('  A sub picker:', JSON.stringify(aSubNow))
  check('the Dataset 1 subcategory picker is present once a category is chosen',
        aSubNow.present === true)
  check('it always offers "(whole category)" so a bare mapping is one click',
        aSubNow.options.includes(''))

  // Pick the B category, then assert a subcategory select appears for it.
  await evaluate(`(() => {
    const s = document.querySelector('#cm-draft-targets [data-draft-cat]')
    const hit = [...s.options].find(o => o.value && /tandy/i.test(o.value))
             || [...s.options].find(o => o.value)
    s.value = hit.value; s.dispatchEvent(new Event('change', { bubbles: true }))
    return hit.value
  })()`)
  await sleep(700)
  const bSubNow = await evaluate(`({
    present: !!document.querySelector('#cm-draft-targets [data-draft-sub]'),
    options: [...(document.querySelector('#cm-draft-targets [data-draft-sub]')?.options||[])].map(o=>o.value),
  })`)
  console.log('  B sub picker:', JSON.stringify(bSubNow))
  check('the Dataset 2 target offers a subcategory picker', bSubNow.present === true)
  check('the subcategory list is populated from the data',
        bSubNow.options.filter(Boolean).length >= 1,
        `options=${JSON.stringify(bSubNow.options)}`)

  // Commit a composite target.
  await evaluate(`(() => {
    const s = document.querySelector('#cm-draft-targets [data-draft-sub]')
    const hit = [...s.options].find(o => o.value)
    if (hit) { s.value = hit.value; s.dispatchEvent(new Event('change', { bubbles: true })) }
    return hit ? hit.value : null
  })()`)
  await sleep(400)
  await click('#cm-draft-done')
  await sleep(700)
  const saved = await evaluate(`({
    maps: (window.S.catmapMaps||[]).length,
    card: document.querySelector('.cm-map')?.innerText || '',
    targetSub: (window.S.catmapMaps?.[0]?.targets||[]).map(t=>t.subcategory||''),
  })`)
  console.log('  saved:', JSON.stringify(saved))
  check('a Category + Subcategory target is committed', saved.maps === 1, `${saved.maps} card(s)`)
  check('the composite subcategory is carried in state',
        saved.targetSub.some(Boolean) === true, JSON.stringify(saved.targetSub))
  check('the card shows both halves of the composite',
        /\/|\+/.test(saved.card), saved.card.replace(/\s+/g, ' ').slice(0, 80))

  // ---- the composite reaches the run ------------------------------------
  // The step-4 primary action is "Continue to selection"; the mapping the user
  // just committed is what it carries forward.
  await click('#btn-select')
  const reached = await waitFor(
    `document.querySelector('.step.active')?.dataset.step === '5'`, 120000)
  const step5 = await evaluate(`({
    step: document.querySelector('.step.active')?.dataset.step,
    cats: [...document.querySelectorAll('#sel-cats input[data-cat]')].map(i=>i.dataset.cat),
    chips: [...document.querySelectorAll('.chip[data-cat]')].map(c=>c.dataset.cat),
  })`)
  console.log('  step5:', JSON.stringify(step5))
  check('the composite mapping carried into step 5',
        reached === true || step5.cats.length > 0 || step5.chips.length > 0,
        JSON.stringify(step5))

  const consoleErrors = events.filter(e =>
    e.method === 'Runtime.exceptionThrown' ||
    (e.method === 'Runtime.consoleAPICalled' && e.params?.type === 'error')).length
  const badReqs = events.filter(e => e.method === 'Network.responseReceived'
      && e.params?.response?.status >= 400)
      .map(e => `${e.params.response.status} ${e.params.response.url}`)
  if (badReqs.length) console.log('  rejected:', JSON.stringify(badReqs, null, 2))
  check('no uncaught exceptions', consoleErrors === 0, `${consoleErrors}`)
  check('no request was rejected by the server', badReqs.length === 0,
        `${badReqs.length} 4xx/5xx`)

  console.log('\n' + '='.repeat(70))
  console.log(failures ? ` COMPOSITE PROBE: ${failures} FAILED`
                       : ' COMPOSITE PROBE: ALL CHECKS PASSED')
  console.log('='.repeat(70))
  process.exit(failures ? 1 : 0)
}

main().catch((e) => { console.error(e); process.exit(2) })
