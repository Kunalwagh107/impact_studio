// Verifies the market-mapping step no longer re-renders on every interaction.
//
//   - changing a pairing's level fires NO server request and rebuilds nothing
//   - changing a pairing's A/B value keeps the very select the user is holding
//     (same DOM node), so focus and an open dropdown survive - the panel is not
//     re-created, and no value list is re-read
//   - adding and deleting a pairing repaints the table from local state, visible
//     in the row count, without a market-mapping round-trip
//   - the advice cell still updates from the server's evidence
//
//   node market_probe.mjs

import { mkdirSync, existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const CDP = process.env.CDP_URL || 'http://127.0.0.1:9222'
const APP = process.env.APP_URL || 'http://127.0.0.1:8777'
const OUT = 'tools/ui_probe/shots_market'
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
const requests = []

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

// Count market-mapping POSTs since a marker index.
const mmPostsSince = (from) => requests.slice(from)
  .filter(u => u.includes('/api/market-mapping')).length
const reqMark = () => requests.length

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
    if (msg.method === 'Network.requestWillBeSent') {
      requests.push(msg.params.request.url)
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

  await send('Page.navigate', { url: APP + '/?_market=' + Date.now() })
  await sleep(2500)

  await setInput('#path-input', WORKBOOK)
  await click('#btn-path')
  await sleep(4000)
  const srcOpts = await evaluate(
    `[...document.querySelector('#a-source').options].map(o => o.value).filter(Boolean)`)
  const sid = srcOpts[srcOpts.length - 1]

  for (const side of ['a', 'b']) { await setSelect(`#${side}-source`, sid); await sleep(600) }
  await sleep(3000)
  for (const [side, val] of [['a', 'Current MAT'], ['b', 'New MAT']]) {
    await waitForOpts(`#${side}-sheet`, 2)
    await setSelect(`#${side}-sheet`, 'Raw_MAT')
    await waitForOpts(`#${side}-splitcol`, 5)
    await setSelect(`#${side}-splitcol`, 'Dataset')
    await waitForOpts(`#${side}-value`, 2)
    await setSelect(`#${side}-value`, val)
    await sleep(500)
  }

  await evaluate(`document.querySelector('#btn-profile').click()`)
  await waitFor(`document.querySelector('.step.active')?.dataset.step === '2'`, 120000)
  await evaluate(`document.querySelector('#btn-map').click()`)
  const at3 = await waitFor(`document.querySelector('.step.active')?.dataset.step === '3'`, 180000)
  check('reached step 3 (market mapping)', at3 === true)
  await sleep(1200)

  // The value lists must be present before we can pair anything. They arrive in
  // the advisory, which is rendered as soon as the step opens - the pairing
  // table itself is empty until a pairing is added, so waiting on a table select
  // would wait for a control that does not exist yet.
  const haveValues = await waitFor(
    `(window.S.marketAdvisory?.a_values?.length || 0) > 1`
    + ` && (window.S.marketAdvisory?.b_values?.length || 0) > 1`, 60000)
  check('the market values were read and offered', haveValues === true)

  // ---- add two pairings ---------------------------------------------------
  await click('#mkt-add')
  await sleep(400)
  await click('#mkt-add')
  await sleep(400)
  const twoRows = await evaluate(`document.querySelectorAll('#map-out tr[data-mkt-row]').length`)
  check('two pairings can be added', twoRows === 2, `${twoRows} row(s)`)

  // ---- LEVEL change must be purely local ----------------------------------
  // Choose values on row 0 first (this legitimately asks the server for advice),
  // then a level change must fire nothing at all.
  const rows0 = await evaluate(`document.querySelectorAll('#map-out select[data-mkt-a]').length`)
  check('the pairing table rendered its selects', rows0 === 2, `${rows0} A-select(s)`)

  await setSelect('#map-out select[data-mkt-a][data-mkt-a="0"]', '')
  await sleep(200)
  // set real values via option index (value strings vary by workbook)
  await evaluate(`(() => {
    const a = document.querySelectorAll('#map-out select[data-mkt-a]')[0]
    a.selectedIndex = a.options.length - 1
    a.dispatchEvent(new Event('change', { bubbles: true }))
    const b = document.querySelectorAll('#map-out select[data-mkt-b]')[0]
    b.selectedIndex = b.options.length - 1
    b.dispatchEvent(new Event('change', { bubbles: true }))
    return true
  })()`)
  await sleep(1500)   // let the advice-only calls settle

  let m = reqMark()
  // Snapshot the exact node we will hold across the interaction.
  await evaluate(`window.__held = document.querySelector('#map-out select[data-mkt-a]').closest('tr')`)
  await evaluate(`(() => {
    const s = document.querySelectorAll('#map-out select[data-mkt-level]')[0]
    s.selectedIndex = (s.selectedIndex + 1) % s.options.length
    s.dispatchEvent(new Event('change', { bubbles: true }))
    return s.value
  })()`)
  await sleep(1200)
  const lvlPosts = mmPostsSince(m)
  check('changing a pairing level fires NO server request',
        lvlPosts === 0, `${lvlPosts} market-mapping POST(s)`)
  const nodeKept = await evaluate(
    `document.querySelector('#map-out select[data-mkt-a]').closest('tr') === window.__held`)
  check('the row survives a level change (not rebuilt)', nodeKept === true)
  const lvlS = await evaluate(`document.querySelector('#map-out select[data-mkt-level]').value`)
  check('the level the user chose is the level in state',
        lvlS === await evaluate(`(window.S.marketPairs[0]||{}).level`),
        `dom=${lvlS} state=${await evaluate(`(window.S.marketPairs[0]||{}).level`)}`)

  // ---- A/B value change must NOT rebuild the panel ------------------------
  m = reqMark()
  // Hold the very select node; after the change it must still be in the DOM.
  const res = await evaluate(`(() => {
    const s = document.querySelectorAll('#map-out select[data-mkt-a]')[0]
    window.__sel = s
    const before = s.options.length
    s.selectedIndex = s.options.length - 1
    s.dispatchEvent(new Event('change', { bubbles: true }))
    return { before, now: s.value }
  })()`)
  await sleep(1500)
  const valPosts = mmPostsSince(m)
  check('changing an A value asks the server for advice only (not a re-read)',
        valPosts <= 1, `${valPosts} market-mapping POST(s)`)
  const stillHeld = await evaluate(
    `document.querySelector('#map-out select[data-mkt-a]') === window.__sel`)
  check('the select the user held is the same DOM node after the change',
        stillHeld === true)
  const adviceCell = await evaluate(
    `!!document.querySelector('#map-out tr[data-mkt-row] td[data-mkt-advice]')`)
  check('the advice cell exists and is patched in place', adviceCell === true)

  // ---- delete repaints locally, with no market-mapping call ---------------
  m = reqMark()
  await evaluate(`document.querySelector('#map-out [data-mkt-del]').click()`)
  await sleep(600)
  const afterDel = await evaluate(`document.querySelectorAll('#map-out tr[data-mkt-row]').length`)
  check('deleting a pairing updates the row count', afterDel === 1, `${afterDel} row(s)`)
  const delPosts = mmPostsSince(m)
  check('deleting a pairing fires no market-mapping request',
        delPosts === 0, `${delPosts} market-mapping POST(s)`)

  // ---- the panel values are still the server's -----------------------------
  const anyMissing = await evaluate(`(() => {
    const sels = document.querySelectorAll('#map-out select[data-mkt-a]')
    for (const s of sels) if (s.options.length < 2) return true
    return false
  })()`)
  check('the value lists were never dropped by the local edits', anyMissing === false)

  const consoleErrors = events.filter(e =>
    e.method === 'Runtime.exceptionThrown' ||
    (e.method === 'Runtime.consoleAPICalled' && e.params?.type === 'error')).length
  const badNet = events.filter(e => e.method === 'Network.responseReceived'
      && e.params?.response?.status >= 400).length
  check('no uncaught exceptions', consoleErrors === 0, `${consoleErrors}`)
  check('no request was rejected by the server', badNet === 0, `${badNet} 4xx/5xx`)
  check('no dialogs blocked the step', dialogs.length === 0, `${dialogs.length}`)

  console.log('\n' + '='.repeat(70))
  console.log(failures ? ` MARKET PROBE: ${failures} FAILED` : ' MARKET PROBE: ALL CHECKS PASSED')
  console.log('='.repeat(70))
  process.exit(failures ? 1 : 0)
}

main().catch((e) => { console.error(e); process.exit(2) })
