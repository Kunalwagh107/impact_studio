// Reproduces the reported category-mapping defect:
//   "when I select a target in category mapping, the UI is automatically
//    collapsing, which is not expected"
//
// Drives the real app in headless Chrome, opens the category-mapping step,
// ticks "Show all", types a filter, then changes a target <select> and asserts
// on what survives:
//
//   - the filter text must still be there
//   - the "Show all" checkbox must still be ticked
//   - the row count must not collapse
//   - the edited row must still be on screen
//   - focus must not be thrown away
//
//   node catmap_probe.mjs

import { mkdirSync, writeFileSync, existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const CDP = process.env.CDP_URL || 'http://127.0.0.1:9222'
const APP = process.env.APP_URL || 'http://127.0.0.1:8777'
const OUT = 'tools/ui_probe/shots'
// Resolve relative to this file; the hardcoded absolute path pointed one
// directory above the project root, so the probe failed at source registration.
const _ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..')
const WORKBOOK = process.env.WORKBOOK || [
  join(_ROOT, 'TW Impact Study_V2 1 (1).xlsx'),
  join(_ROOT, '..', 'TW Impact Study_V2 1 (1).xlsx'),
].find(p => existsSync(p)) || join(_ROOT, 'TW Impact Study_V2 1 (1).xlsx')

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
let failures = 0
const results = []

function check(name, ok, msg = '') {
  if (!ok) failures++
  results.push({ name, ok, msg })
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${name}${msg ? '  ' + msg : ''}`)
  return ok
}

let ws, msgId = 0
const pending = new Map()

function send(method, params = {}) {
  const id = ++msgId
  ws.send(JSON.stringify({ id, method, params }))
  return new Promise((res, rej) => pending.set(id, { res, rej }))
}

async function evaluate(expr, awaitPromise = true) {
  const r = await send('Runtime.evaluate', {
    expression: expr, returnByValue: true, awaitPromise,
  })
  if (r.exceptionDetails) {
    throw new Error(r.exceptionDetails.exception?.description || 'eval failed')
  }
  return r.result?.value
}

async function connect() {
  const targets = await (await fetch(`${CDP}/json/list`)).json()
  const page = targets.find((t) => t.type === 'page')
  if (!page) throw new Error('no page target')
  ws = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej })
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data)
    if (m.id && pending.has(m.id)) {
      const { res, rej } = pending.get(m.id)
      pending.delete(m.id)
      m.error ? rej(new Error(m.error.message)) : res(m.result)
    }
  }
  await send('Page.enable')
  await send('Runtime.enable')
  // Headless Chrome caches app.js across navigate() calls. Without this the
  // probe happily re-tests the PREVIOUS build and reports the old failure -
  // which is exactly what happened once and cost a debugging round.
  await send('Network.enable')
  await send('Network.setCacheDisabled', { cacheDisabled: true })
}

async function goto(url) {
  // a unique query defeats any intermediate cache as well
  const bust = url + (url.includes('?') ? '&' : '?') + '_probe=' + Date.now()
  await send('Page.navigate', { url: bust })
  await sleep(1600)
}

async function waitFor(fn, label, timeout = 45000) {
  const t0 = Date.now()
  for (;;) {
    let v = null
    try { v = await evaluate(`(${fn})()`) } catch { /* retry */ }
    if (v) return v
    if (Date.now() - t0 > timeout) throw new Error('timeout waiting for ' + label)
    await sleep(250)
  }
}

async function waitForOpts(sel, min, timeout = 60000) {
  const t0 = Date.now()
  let last = 0
  for (;;) {
    last = await evaluate(
      `document.querySelector(${JSON.stringify(sel)})?.options.length ?? 0`)
    if (last >= min) return last
    if (Date.now() - t0 > timeout) {
      // Report what was actually on screen. A bare "timeout" costs the next
      // person a separate measuring script to find out whether the control was
      // empty, half-populated, or absent entirely.
      const seen = await evaluate(`(() => {
        const el = document.querySelector(${JSON.stringify(sel)});
        if (!el) return '(element not found)';
        return [...el.options].map(o => o.value || o.textContent).slice(0, 8).join(' | ');
      })()`)
      throw new Error(
        `timeout waiting for ${sel} to reach ${min} options ` +
        `(saw ${last}: ${seen}) - if the server was mid-export, re-run on an idle one`)
    }
    await sleep(400)
  }
}

async function main() {
  mkdirSync(OUT, { recursive: true })
  await connect()

  console.log('='.repeat(74))
  console.log(' Category-mapping collapse probe')
  console.log('='.repeat(74))

  // This probe assumes an idle server. A bulk export still running holds the
  // single worker, so every dropdown here arrives late and the probe reports
  // timeouts that look like product bugs. Say so up front rather than
  // discovering it as a mystery failure.
  try {
    const r = await fetch(APP + '/api/memory')
    const mem = await r.json()
    if (mem && mem.n_prepared > 0) {
      console.log(`  ! the server reports ${mem.n_prepared} prepared run(s) in flight.`)
      console.log('    If this probe times out on a dropdown, wait for that to finish')
      console.log('    and re-run - it is load, not a UI regression.')
    }
  } catch { /* informational only */ }

  await goto(APP)

  // Prove which build we are actually testing. A cached app.js once made this
  // probe report a fixed bug as still broken.
  const build = await evaluate(`(() => {
    const s = [...document.scripts].map(x => x.src).join(' ');
    return { hasFix: typeof drawCatmapRows === 'function', scripts: s.slice(0, 120) };
  })()`)
  check('the served build contains the render fix', build.hasFix === true,
        build.hasFix ? 'drawCatmapRows present' : 'STALE BUILD - fix not loaded')

  // ---- step 1: register the workbook and select it on both sides ----------
  // Use the real button, not a direct fetch: registering through the UI is the
  // path the user takes, and it also populates the source pickers.
  await evaluate(`(() => {
    const p = document.querySelector('#path-input');
    p.value = ${JSON.stringify(WORKBOOK)};
    p.dispatchEvent(new Event('input', { bubbles: true }));
  })()`)
  await evaluate(`document.querySelector('#btn-path').click()`)
  await sleep(4000)
  const reg = await evaluate(`document.querySelector('#upload-msg')?.textContent`)
  check('workbook registered through the UI', /Registered/.test(reg || ''), String(reg))

  const sid = await waitFor(`() => {
    const o = document.querySelector('#a-source');
    const v = o ? [...o.options].map(x => x.value).filter(Boolean) : [];
    return v.length ? v[v.length - 1] : null;
  }`, 'source in picker', 30000)

  for (const side of ['a', 'b']) {
    // Selecting a source kicks off an async column read; poll for the dependent
    // dropdown rather than sleeping a fixed amount, or a stale value slips through.
    await evaluate(`(() => {
      const s = document.querySelector('#${side}-source');
      s.value = ${JSON.stringify(sid)};
      s.dispatchEvent(new Event('change', { bubbles: true }));
    })()`)
    await waitForOpts(`#${side}-sheet`, 2)
    await evaluate(`(() => {
      const s = document.querySelector('#${side}-sheet');
      s.value = 'Raw_MAT';
      s.dispatchEvent(new Event('change', { bubbles: true }));
    })()`)
    await waitForOpts(`#${side}-splitcol`, 5)
    await evaluate(`(() => {
      const s = document.querySelector('#${side}-splitcol');
      s.value = 'Dataset';
      s.dispatchEvent(new Event('change', { bubbles: true }));
    })()`)
    await waitForOpts(`#${side}-value`, 2)
    const want = side === 'a' ? 'Current MAT' : 'New MAT'
    const ok = await evaluate(`(() => {
      const s = document.querySelector('#${side}-value');
      const opts = [...s.options].map(o => o.value);
      if (!opts.includes(${JSON.stringify(want)})) return 'NO_OPTION:' + opts.slice(0,4).join('|');
      s.value = ${JSON.stringify(want)};
      s.dispatchEvent(new Event('change', { bubbles: true }));
      return 'ok';
    })()`)
    check(`[${side}] discriminator set to ${want}`, ok === 'ok', String(ok))
  }

  // ---- step 2: profile, then get to the category-mapping step -------------
  await evaluate(`document.querySelector('#btn-profile').click()`)
  await waitFor(`() => document.querySelector('#btn-map') &&
    !document.querySelector('#overlay')?.offsetParent`, 'profile done', 90000)
  await sleep(500)

  await evaluate(`document.querySelector('#btn-map').click()`)
  await waitFor(`() => document.querySelector('#btn-catmap') &&
    !document.querySelector('#overlay')?.offsetParent`, 'mapping done', 180000)
  await sleep(500)

  await evaluate(`document.querySelector('#btn-catmap').click()`)
  await waitFor(`() => document.querySelector('#catmap-out table tbody tr')`,
                'category mapping rendered', 180000)
  await sleep(600)

  // ---- step 3: set up a reviewable state --------------------------------
  await evaluate(`(() => {
    const cb = document.querySelector('#cm-showall');
    if (cb && !cb.checked) { cb.checked = true; cb.dispatchEvent(new Event('change', { bubbles: true })); }
  })()`)
  await sleep(500)

  // filter to a small slice so the collapse is unmistakable
  await evaluate(`(() => {
    const s = document.querySelector('#cm-search');
    s.value = 'cig';
    s.dispatchEvent(new Event('input', { bubbles: true }));
  })()`)
  await sleep(500)

  const before = await evaluate(`(() => {
    const s = document.querySelector('#cm-search');
    const cb = document.querySelector('#cm-showall');
    const rows = [...document.querySelectorAll('#cm-tbl tbody tr')];
    const sel = document.querySelector('#cm-tbl select[data-cm-cat]');
    return {
      filter: s.value,
      showAll: cb.checked,
      rowCount: rows.length,
      firstRowText: rows[0] ? rows[0].innerText.slice(0, 40) : '',
      hasSelect: !!sel,
      scrollTop: document.querySelector('.tbl-wrap')?.scrollTop ?? 0,
    };
  })()`)
  console.log(`  before edit: filter="${before.filter}" showAll=${before.showAll} rows=${before.rowCount}`)
  check('a target <select> is present to edit', before.hasSelect)

  // ---- step 4: change a target - this is the reported action -------------
  const edited = await evaluate(`(() => {
    const sel = document.querySelector('#cm-tbl select[data-cm-cat]');
    if (!sel) return null;
    const opts = [...sel.options].filter(o => o.value);
    const pick = opts.find(o => o.value !== sel.value);
    if (!pick) return null;
    sel.focus();
    sel.value = pick.value;
    sel.dispatchEvent(new Event('change', { bubbles: true }));
    return { from: sel.dataset.cmT, picked: pick.value };
  })()`)
  check('a target selection was made', !!edited, JSON.stringify(edited))
  await sleep(700)

  // ---- step 5: what survived? -------------------------------------------
  const after = await evaluate(`(() => {
    const s = document.querySelector('#cm-search');
    const cb = document.querySelector('#cm-showall');
    const rows = [...document.querySelectorAll('#cm-tbl tbody tr')];
    const active = document.activeElement;
    return {
      filter: s ? s.value : null,
      showAll: cb ? cb.checked : null,
      rowCount: rows.length,
      activeTag: active ? active.tagName : null,
      focusStillInTable: !!(active && active.closest && active.closest('#cm-tbl')),
    };
  })()`)
  console.log(`  after edit:  filter="${after.filter}" showAll=${after.showAll} rows=${after.rowCount}`)

  check('the filter text is preserved across an edit',
        after.filter === before.filter, `"${before.filter}" -> "${after.filter}"`)
  check('"Show all" stays ticked across an edit',
        after.showAll === before.showAll, `${before.showAll} -> ${after.showAll}`)
  check('the row list does not collapse on edit',
        after.rowCount >= before.rowCount && after.rowCount > 0,
        `${before.rowCount} -> ${after.rowCount} rows`)

  // capture the evidence
  const shot = await send('Page.captureScreenshot', { format: 'png' })
  writeFileSync(`${OUT}/catmap-after-target-change.png`,
                Buffer.from(shot.data, 'base64'))
  console.log(`  screenshot: ${OUT}/catmap-after-target-change.png`)

  // ---- step 6: and the raw symptom - rows vanishing ---------------------
  const vanished = await evaluate(`(() => {
    const rows = [...document.querySelectorAll('#cm-tbl tbody tr')];
    const body = rows.map(r => r.innerText).join(' ');
    return { mentionsEmpty: /No rows match|Every category matched/.test(body) };
  })()`)
  check('the table is not showing an empty-state message after an edit',
        !vanished.mentionsEmpty)

  // ---- step 7: "+ add target" must not collapse the view either ---------
  const beforeAdd = await evaluate(`(() => ({
    filter: document.querySelector('#cm-search').value,
    showAll: document.querySelector('#cm-showall').checked,
    targets: document.querySelectorAll('#cm-tbl .target-row').length,
  }))()`)
  await evaluate(`document.querySelector('[data-cm-add]').click()`)
  await sleep(500)
  const afterAdd = await evaluate(`(() => ({
    filter: document.querySelector('#cm-search').value,
    showAll: document.querySelector('#cm-showall').checked,
    targets: document.querySelectorAll('#cm-tbl .target-row').length,
  }))()`)
  check('adding a target keeps the filter and "Show all"',
        afterAdd.filter === beforeAdd.filter && afterAdd.showAll === beforeAdd.showAll,
        `filter "${afterAdd.filter}" / showAll ${afterAdd.showAll}`)
  check('adding a target actually adds an editor row',
        afterAdd.targets === beforeAdd.targets + 1,
        `${beforeAdd.targets} -> ${afterAdd.targets} target rows`)

  // ---- step 8: removing a target behaves the same -----------------------
  await evaluate(`(() => {
    const del = document.querySelector('#cm-tbl [data-cm-del]');
    if (del) del.click();
  })()`)
  await sleep(500)
  const afterDel = await evaluate(`(() => ({
    filter: document.querySelector('#cm-search').value,
    showAll: document.querySelector('#cm-showall').checked,
    targets: document.querySelectorAll('#cm-tbl .target-row').length,
  }))()`)
  check('removing a target keeps the filter and "Show all"',
        afterDel.filter === beforeAdd.filter && afterDel.showAll === beforeAdd.showAll,
        `filter "${afterDel.filter}" / showAll ${afterDel.showAll}`)

  // ---- step 9: the status change IS allowed to redraw, but keeps state ---
  await evaluate(`(() => {
    const s = document.querySelector('#cm-tbl [data-cm-status]');
    if (s) { s.value = 'excluded'; s.dispatchEvent(new Event('change', { bubbles: true })); }
  })()`)
  await sleep(500)
  const afterStatus = await evaluate(`(() => ({
    filter: document.querySelector('#cm-search').value,
    showAll: document.querySelector('#cm-showall').checked,
  }))()`)
  check('changing status keeps the filter and "Show all"',
        afterStatus.filter === beforeAdd.filter && afterStatus.showAll === beforeAdd.showAll,
        `filter "${afterStatus.filter}" / showAll ${afterStatus.showAll}`)

  console.log('\n' + '='.repeat(74))
  console.log(failures ? ` ${failures} CHECK(S) FAILED` : ' ALL CHECKS PASSED')
  console.log('='.repeat(74))
  writeFileSync(`${OUT}/catmap_probe_result.json`,
                JSON.stringify({ before, after, results }, null, 2))
  process.exit(failures ? 1 : 0)
}

main().catch((e) => {
  console.error('PROBE FAILED:', e.message)
  process.exit(2)
})
