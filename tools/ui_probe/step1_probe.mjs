// Focused probe: what actually happens when the driver sets B's source and sheet?
//
// The full driver reported `[b] column list populated  -1 options` while
// `[a]` populated fine and `[b] sheet list populated 16 options`. `-1` is
// waitForOptions' timeout sentinel, so the poll ran 60s and never saw 5+ options.
//
// This probe replays ONLY step 1 for the B side and records:
//   - every /api/source/*/columns request the page makes, and its status
//   - the value of #b-sheet before and after each setSelect
//   - whether the change event the driver dispatches is the one that fetches
//
// Run: node step1_probe.mjs

import { mkdirSync, existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const CDP = process.env.CDP_URL || 'http://127.0.0.1:9222'
const APP = process.env.APP_URL || 'http://127.0.0.1:8777'
const OUT = 'tools/ui_probe/_run/step1'
const _ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..')
const WORKBOOK = process.env.WORKBOOK || [
  join(_ROOT, 'TW Impact Study_V2 1 (1).xlsx'),
  join(_ROOT, '..', 'TW Impact Study_V2 1 (1).xlsx'),
].find(p => existsSync(p))

const sleep = (ms) => new Promise(r => setTimeout(r, ms))
let ws, msgId = 0
let raceFailures = 0
const pending = new Map()
const netRequests = []
const held = []
// Hold the stale (default-sheet) columns request this long, in ms, to force it to
// resolve after the newer one. 0 disables interception's delay.
let HOLD = Number(process.env.HOLD_MS || 0)

async function connect() {
  const targets = await (await fetch(`${CDP}/json/list`)).json()
  const page = targets.find(t => t.type === 'page')
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
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails))
  return r.result.value
}
const opts = (sel) => evaluate(
  `[...(document.querySelector(${JSON.stringify(sel)})?.options||[])].map(o=>o.value)`)
const valOf = (sel) => evaluate(
  `document.querySelector(${JSON.stringify(sel)})?.value ?? null`)
const setSelect = (sel, val) => evaluate(`(() => {
  const el = document.querySelector(${JSON.stringify(sel)})
  if (!el) return 'NOT_FOUND'
  const before = el.value
  const os = [...el.options].map(o => o.value)
  if (!os.includes(${JSON.stringify(val)})) return 'NO_OPTION:' + os.slice(0,5).join('|')
  el.value = ${JSON.stringify(val)}
  el.dispatchEvent(new Event('change', { bubbles: true }))
  return 'ok:' + before + '->' + el.value
})()`)

async function main() {
  mkdirSync(OUT, { recursive: true })
  ws = await connect()
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data)
    if (m.id && pending.has(m.id)) {
      const p = pending.get(m.id); pending.delete(m.id)
      m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result)
    } else if (m.method === 'Network.requestWillBeSent') {
      const u = m.params.request.url
      if (/\/api\//.test(u)) netRequests.push({ t: Date.now(), url: u, id: m.params.requestId })
    } else if (m.method === 'Network.responseReceived') {
      const r = netRequests.find(x => x.id === m.params.requestId)
      if (r) { r.status = m.params.response.status; r.ms = Date.now() - r.t }
    } else if (m.method === 'Fetch.requestPaused') {
      // Interception is how the inversion is FORCED. Locally both reads return in
      // under 50ms and land in order, so a "click fast" fixture never enters the
      // branch the guard protects - the mutation survived exactly that way. Hold
      // the *stale* request (the default sheet, `Formula`) so it is guaranteed to
      // resolve AFTER the newer one (`Raw_MAT`); with the guard, the stale answer
      // is dropped; without it, the panel ends on the wrong sheet's columns.
      const u = m.params.request.url
      const delay = (HOLD && /sheet=Formula/.test(u)) ? HOLD : 0
      held.push({ url: u, delay })
      setTimeout(() => {
        send('Fetch.continueRequest', { requestId: m.params.requestId }).catch(() => {})
      }, delay)
    }
  }
  await send('Page.enable'); await send('Runtime.enable')
  await send('Network.enable'); await send('Network.setCacheDisabled', { cacheDisabled: true })
  await send('Fetch.enable', {
    patterns: [{ urlPattern: '*/api/source/*/columns*', requestStage: 'Request' }],
  })
  await send('Emulation.setDeviceMetricsOverride', {
    width: 1600, height: 1200, deviceScaleFactor: 1, mobile: false,
  })
  await send('Emulation.setEmulatedMedia', {
    features: [{ name: 'prefers-color-scheme', value: 'dark' }],
  })
  await send('Page.navigate', { url: `${APP}?_p=${Date.now()}` })
  await sleep(5000)

  // register — same route drive.mjs uses: upload the file, click Upload file.
  // The upload auto-selects the new source on both sides, so exercise that too.
  // Bytes come from /__workbook/: an empty File uploads 0 bytes and every later
  // sheet/column read 500s, which is a broken probe, not a broken control.
  const upBytes = await evaluate(`(async () => {
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
  console.log('upload:', upBytes)
  await evaluate(`document.querySelector('#btn-upload').click()`)
  await sleep(5000)
  const reg = await evaluate(`document.querySelector('#upload-msg')?.textContent`)
  console.log('register:', reg)

  const srcs = await opts('#a-source')
  console.log('sources:', srcs)
  const sid = srcs[srcs.length - 1]
  netRequests.length = 0

  // --- exactly what the driver does, in the driver's order -----------------
  for (const side of ['a', 'b']) {
    netRequests.length = 0
    console.log(`\n== side ${side}`)
    console.log('  source ->', await setSelect(`#${side}-source`, sid))
    await sleep(1500)
    console.log('  after source, sheets =', (await opts(`#${side}-sheet`)).length)
    console.log('  sheet value before =', await valOf(`#${side}-sheet`))
    console.log('  sheet ->', await setSelect(`#${side}-sheet`, 'Raw_MAT'))
    // What requests did the change fire?
    await sleep(2500)
    const cols = await opts(`#${side}-splitcol`)
    console.log(`  splitcol options = ${cols.length}`, cols.slice(0, 4))
    console.log(`  API calls since change (${netRequests.length}):`)
    for (const r of netRequests) console.log(`     ${r.status ?? '?'} ${r.ms ?? '?'}ms ${r.url}`)
  }

  // --- the race, deliberately -----------------------------------------------
  // The defect: choosing a source fires a read for the panel's default sheet,
  // and choosing a sheet fires a second; whichever *responds* last repaints the
  // panel. Left to natural timing both land in order, so the inversion is FORCED
  // by holding the stale request open (HOLD_MS).
  const holdMs = Number(process.env.HOLD_MS || 3500)
  console.log(`\n== race: source + sheet, stale read held ${holdMs}ms`)
  for (const side of ['a', 'b']) {
    netRequests.length = 0
    held.length = 0
    HOLD = holdMs
    await setSelect(`#${side}-source`, '')
    await setSelect(`#${side}-source`, sid)
    await setSelect(`#${side}-sheet`, 'Formula')
    await setSelect(`#${side}-sheet`, 'Raw_MAT')
    await sleep(6000)
    HOLD = 0
    const o = await opts(`#${side}-splitcol`)
    const sheetNow = await valOf(`#${side}-sheet`)
    const good = o.includes('Dataset') && o.includes('CATEGORY')
    console.log(`  [${side}] sheet=${sheetNow} options=${o.length} ` +
                `${good ? 'PASS' : 'FAIL'} ${JSON.stringify(o.slice(0, 5))} ` +
                `(held ${held.filter(h => h.delay > 0).length} stale read(s))`)
    if (!good) raceFailures++
  }

  console.log('\n--- verdict ---')
  const bCols = await opts('#b-splitcol')
  console.log(`B splitcol now: ${bCols.length} option(s) ` +
              `${bCols.includes('Dataset') ? '(has Dataset)' : '(MISSING Dataset)'}`)
  console.log(raceFailures === 0
    ? 'RACE: the panel kept the sheet the user chose'
    : `RACE: ${raceFailures} side(s) ended on the wrong sheet's columns`)
  ws.close()
  process.exit(raceFailures ? 1 : 0)
}
main().catch(e => { console.error('PROBE FAILED:', e.message); process.exit(1) })
