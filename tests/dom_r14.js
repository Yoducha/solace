// Tab r14 — DOM proof for items 3 and 8, driven over CDP by
// tests/test_tab_r14.py. Usage:
//   node dom_r14.js <base-url> <chrome-binary>
// Prints one JSON object: {anchors: [...], gender: {...}}.
const { spawn } = require('child_process');

const BASE = process.argv[2];
const CHROME = process.argv[3];
const PORT = 9339;

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function waitFor(fn, timeout = 15000, step = 200) {
  const t0 = Date.now();
  for (;;) {
    try { const v = await fn(); if (v) return v; } catch (e) {}
    if (Date.now() - t0 > timeout) throw new Error('waitFor timeout');
    await sleep(step);
  }
}

class CDP {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.pending = new Map();
    ws.onmessage = ev => {
      const m = JSON.parse(ev.data);
      if (m.id && this.pending.has(m.id)) {
        const { res, rej } = this.pending.get(m.id);
        this.pending.delete(m.id);
        if (m.error) rej(new Error(JSON.stringify(m.error)));
        else res(m.result);
      }
    };
  }
  send(method, params = {}, sessionId) {
    const id = ++this.id;
    const msg = { id, method, params };
    if (sessionId) msg.sessionId = sessionId;
    return new Promise((res, rej) => {
      this.pending.set(id, { res, rej });
      this.ws.send(JSON.stringify(msg));
    });
  }
}

const MEASURE_FN = `
(function (scope) {
  const out = [];
  document.querySelectorAll('a').forEach(a => {
    const r = a.getBoundingClientRect();
    const cs = getComputedStyle(a);
    const host = a.closest('[id]');
    out.push({
      scope: scope,
      host: host ? host.id : '',
      text: (a.textContent || '').trim().slice(0, 40),
      href: a.getAttribute('href') || '',
      w: Math.round(r.width * 10) / 10,
      h: Math.round(r.height * 10) / 10,
      visible: r.width > 0 && r.height > 0 &&
        cs.display !== 'none' && cs.visibility !== 'hidden'
    });
  });
  return out;
})`;

async function main() {
  const chrome = spawn(CHROME, [
    '--headless', '--no-sandbox', '--disable-gpu',
    '--remote-debugging-port=' + PORT, '--window-size=390,844',
    'about:blank',
  ], { stdio: 'ignore' });
  try {
    const ver = await waitFor(async () => {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/version`);
      return r.ok ? r.json() : null;
    });
    const ws = new WebSocket(ver.webSocketDebuggerUrl);
    await new Promise((res, rej) => {
      ws.onopen = res; ws.onerror = rej;
    });
    const cdp = new CDP(ws);
    const { targetId } = await cdp.send('Target.createTarget',
      { url: 'about:blank' });
    const { sessionId } = await cdp.send('Target.attachToTarget',
      { targetId, flatten: true });
    const S = sessionId;
    await cdp.send('Page.enable', {}, S);
    await cdp.send('Runtime.enable', {}, S);
    await cdp.send('Emulation.setDeviceMetricsOverride',
      { width: 390, height: 844, deviceScaleFactor: 2, mobile: true },
      S);

    async function ev(expr) {
      const r = await cdp.send('Runtime.evaluate',
        { expression: expr, awaitPromise: true, returnByValue: true },
        S);
      if (r.exceptionDetails) {
        throw new Error('eval failed: ' + expr.slice(0, 80) + ' :: ' +
          JSON.stringify(r.exceptionDetails).slice(0, 300));
      }
      return r.result.value;
    }
    async function nav(url) {
      await cdp.send('Page.navigate', { url }, S);
      await waitFor(() => ev(`document.readyState === 'complete'`));
      await sleep(400);
    }

    const anchors = [];
    const gender = {};

    // ---- main page, English boot (welcome + banner + chat) ----
    await nav(BASE + '/');
    await waitFor(() => ev(`!!document.getElementById('tiny-line')`));
    anchors.push(...await ev(`${MEASURE_FN}('welcome-en')`));
    await ev(`document.getElementById('crisis-banner').style.display = 'block'`);
    anchors.push(...await ev(`${MEASURE_FN}('banner')`));
    await ev(`document.getElementById('crisis-banner').style.display = 'none'`);
    await ev(`document.getElementById('welcome').style.display = 'none'`);
    await ev(`document.getElementById('chat').style.display = 'flex'`);
    await ev(`addMsg('If this feels urgent, call 988 or text 741741 right away.', 'ai')`);
    await sleep(200);
    anchors.push(...await ev(`${MEASURE_FN}('chat')`));

    // ---- item 3: New chat resets the writer's gender ----
    await ev(`writerGender = 'f'`);
    await ev(`window.confirm = () => true; window.alert = () => {};`);
    await ev(`document.getElementById('new-chat').click()`);
    await sleep(400);
    gender.afterNewChat = await ev(`writerGender`);

    // ---- item 3: Delete resets it too (real local user) ----
    await ev(`(async () => {
      const res = await api('/api/profile',
        {user_id: 'r14dom' + Date.now(), name: '', notes: ''});
      userId = res.user_id;
      writerGender = 'f';
    })()`);
    await sleep(300);
    await ev(`doDeleteData()`);
    await waitFor(async () => (await ev(`writerGender`)) === '');
    gender.afterDelete = await ev(`writerGender`);

    // ---- main page, Hebrew boot (HE tiny line: תנאים / 1201 / 101) ----
    await ev(`localStorage.setItem('solace_lang', 'he')`);
    await nav(BASE + '/');
    await waitFor(() => ev(`!!document.getElementById('tiny-line')`));
    // Leftover device state can boot the app into another view,
    // and its boot script can re-hide this one a beat later —
    // so the surface is forced and measured in ONE evaluate,
    // with no gap for the page to undo it.
    anchors.push(...await ev(`
      document.getElementById('welcome').style.display = 'flex';
      document.getElementById('chat').style.display = 'none';
      document.getElementById('intake-fields').style.display = 'block';
      (${MEASURE_FN})('welcome-he')`));

    // ---- terms + privacy, both languages ----
    for (const page of ['terms', 'privacy']) {
      await nav(BASE + '/' + page);
      anchors.push(...await ev(`${MEASURE_FN}('${page}-en')`));
      await ev(`document.getElementById('lang-toggle').click()`);
      await sleep(300);
      anchors.push(...await ev(`${MEASURE_FN}('${page}-he')`));
    }

    console.log(JSON.stringify({ anchors, gender }));
  } finally {
    chrome.kill('SIGKILL');
  }
}

main().catch(e => {
  console.log(JSON.stringify({ error: String(e && e.message || e) }));
  process.exit(1);
});
