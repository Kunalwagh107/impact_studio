// Reproduce the reported screenshot: load Current_MAT (a sheet with ~100k rows
// of which only 45,421 hold data) and check the profile reports numeric metrics.
import { writeFileSync } from 'node:fs'

const CDP = process.env.CDP_URL || 'http://127.0.0.1:9222'
const APP = process.env.APP_URL || 'http://127.0.0.1:8777'
const OUT = process.argv[2] || '.'
const WB = 'C:\\Users\\kunal\\OneDrive\\Desktop\\impact\\TW Impact Study_V2 1 (1).xlsx'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
let fails = 0
const check = (n, ok, m = '') => {
  if (!ok) fails++
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${n}${m ? '  ' + m : ''}`)
}

const t = await (await fetch(`${CDP}/json/list`)).json()
const pg = t.find((x) => x.type === 'page')
const ws = new WebSocket(pg.webSocketDebuggerUrl)
await new Promise((r, j) => { ws.onopen = r; ws.onerror = j })
let id = 0
const pend = new Map()
ws.onmessage = (e) => {
  const m = JSON.parse(e.data)
  if (m.id && pend.has(m.id)) {
    const { res, rej } = pend.get(m.id); pend.delete(m.id)
    m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result)
  }
}
const send = (method, params = {}) => new Promise((res, rej) => {
  const i = ++id; pend.set(i, { res, rej })
  ws.send(JSON.stringify({ id: i, method, params }))
})
const ev = async (x) => {
  const r = await send('Runtime.evaluate', { expression: x, returnByValue: true, awaitPromise: true })
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails))
  return r.result.value
}
await send('Page.enable'); await send('Runtime.enable')

const setSel = (s, v) => ev(`(()=>{const e=document.querySelector(${JSON.stringify(s)});if(!e)return 'NOT_FOUND';if(![...e.options].map(o=>o.value).includes(${JSON.stringify(v)}))return 'NO_OPT';e.value=${JSON.stringify(v)};e.dispatchEvent(new Event('change',{bubbles:true}));return 'ok'})()`)
const setInp = (s, v) => ev(`(()=>{const e=document.querySelector(${JSON.stringify(s)});if(!e)return 'NOT_FOUND';Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,${JSON.stringify(v)});e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));return 'ok'})()`)
const click = (s) => ev(`(()=>{const e=document.querySelector(${JSON.stringify(s)});if(!e)return 'NOT_FOUND';e.scrollIntoView({block:'center'});e.click();return 'clicked'})()`)
const waitOpts = async (s, min, ms = 60000) => {
  const t0 = Date.now()
  while (Date.now() - t0 < ms) {
    const n = await ev(`document.querySelector(${JSON.stringify(s)})?.options.length??0`)
    if (n >= min) return n
    await sleep(500)
  }
  return -1
}
const step = () => ev(`document.querySelector('.step.active')?.dataset.step??null`)

console.log('='.repeat(74))
console.log(' PROFILE PROBE - Current_MAT (the reported screenshot)')
console.log('='.repeat(74))
await send('Page.navigate', { url: APP }); await sleep(3500)

await setInp('#path-input', WB)
await click('#btn-path')
await sleep(4500)
const sid = await ev(`[...document.querySelector('#a-source').options].map(o=>o.value).filter(Boolean).pop()`)

// A = Current_MAT, B = New_MAT (these sheets are already one dataset each)
await setSel('#a-source', sid); await waitOpts('#a-sheet', 2)
await setSel('#a-sheet', 'Current_MAT'); await sleep(2500)
await setSel('#b-source', sid); await waitOpts('#b-sheet', 2)
await setSel('#b-sheet', 'New_MAT'); await sleep(2500)

await click('#btn-profile')
for (let i = 0; i < 150; i++) { await sleep(1000); if (await step() === '2') break }
check('reached the profile step', await step() === '2')
await sleep(1200)

// The column table lives inside a collapsed <details>; innerText returns ''
// for content the browser is not rendering, so open the panels first.
await ev(`document.querySelectorAll('#profile-out details').forEach(d => { d.open = true })`)
await sleep(500)

const p = await ev(`(() => {
  const cards = [...document.querySelectorAll('#profile-out .card')]
  const out = []
  for (const c of cards) {
    const stats = [...c.querySelectorAll('.stat')].map(s => ({
      l: s.querySelector('.s-label')?.innerText, v: s.querySelector('.s-value')?.innerText }))
    // The card holds two tables: the 6-column column listing and the 2-column
    // metric-family listing. Keep only the column listing.
    const rows = [...c.querySelectorAll('table tbody tr')].map(tr =>
      [...tr.querySelectorAll('td')].map(td => td.innerText.trim())).filter(r => r.length >= 6)
    out.push({ stats, metricRows: rows.filter(r => /Sales Value|Sales Volume|ND Dist/.test(r[0] || '')) })
  }
  const famText = (document.querySelector('#profile-out')?.innerText || '').match(/Metric families \\(\\d+\\)/g)
  return { cards: out, familiesText: famText }
})()`)

const a = p.cards[0]
console.log('\n  A stats:', JSON.stringify(a.stats))
console.log('  family count labels:', JSON.stringify(p.familiesText))
console.log('  metric columns as profiled:')
for (const r of a.metricRows.slice(0, 8)) console.log('    ', JSON.stringify(r.slice(0, 5)))

const fams = (p.familiesText || []).map(s => Number((s.match(/\((\d+)\)/) || [])[1]))
check('A profile shows a non-zero metric family count',
      fams.length > 0 && fams[0] > 0, JSON.stringify(p.familiesText))
check('metric columns are typed numeric, not str',
      a.metricRows.length > 0 && a.metricRows.every(r => /float|int/.test(r[2] || '')),
      a.metricRows.map(r => `${r[0]}=${r[2]}`).join(', ').slice(0, 120))
check('metric columns are roled as metrics, not attributes',
      a.metricRows.length > 0 && a.metricRows.every(r => !/attribute/i.test(r[1] || '')),
      a.metricRows.map(r => r[1]).join(', ').slice(0, 80))
check('A row count is 45,421 (not the 99,999 padded rows)',
      /45,421/.test(JSON.stringify(a.stats)), JSON.stringify(a.stats))

const b = p.cards[1]
check('B (New_MAT) also loads as numeric',
      b.metricRows.length > 0 && b.metricRows.every(r => /float|int/.test(r[2] || '')),
      JSON.stringify(b.stats))

const shot = await send('Page.captureScreenshot', { format: 'png' })
writeFileSync(`${OUT}/profile-fixed.png`, Buffer.from(shot.data, 'base64'))

console.log('\n' + (fails ? ` ${fails} CHECK(S) FAILED` : ' ALL PROFILE CHECKS PASSED'))
process.exit(fails ? 1 : 0)
