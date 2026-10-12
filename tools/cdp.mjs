// 极简 CDP 探针（开发用）：list / open / eval / close
// 用法:
//   node tools/cdp.mjs list
//   node tools/cdp.mjs open <url>
//   node tools/cdp.mjs eval <targetId> <jsFile>
//   node tools/cdp.mjs close <targetId>
import { readFileSync } from 'node:fs';

const PORT = process.env.CDP_PORT || '9222';
const BASE = `http://127.0.0.1:${PORT}`;

async function browserWs() {
  const r = await fetch(`${BASE}/json/version`);
  return (await r.json()).webSocketDebuggerUrl;
}

function rpc(ws, method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = Math.floor(Math.random() * 1e9);
    const onMessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id !== id) return;
      ws.removeEventListener('message', onMessage);
      if (msg.error) reject(new Error(JSON.stringify(msg.error)));
      else resolve(msg.result);
    };
    ws.addEventListener('message', onMessage);
    ws.send(JSON.stringify({ id, method, params }));
  });
}

async function withWs(url, fn) {
  const ws = new WebSocket(url);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  try { return await fn(ws); } finally { ws.close(); }
}

async function list() {
  const r = await fetch(`${BASE}/json/list`);
  return (await r.json()).filter((t) => t.type === 'page');
}

async function open(url) {
  const r = await fetch(`${BASE}/json/new?${encodeURIComponent(url)}`, { method: 'PUT' });
  if (r.ok) return (await r.json()).id;
  return withWs(await browserWs(), async (ws) => {
    const res = await rpc(ws, 'Target.createTarget', { url });
    return res.targetId;
  });
}

async function close(targetId) {
  const r = await fetch(`${BASE}/json/close/${targetId}`);
  return r.ok;
}

async function watch(targetId, seconds) {
  const info = await fetch(`${BASE}/json/list`).then((r) => r.json());
  const target = info.find((t) => t.id === targetId);
  if (!target) throw new Error('target not found: ' + targetId);
  const messages = [];
  await withWs(target.webSocketDebuggerUrl, async (ws) => {
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.method === 'Runtime.exceptionThrown') {
        const details = msg.params.exceptionDetails;
        const desc = details.exception && details.exception.description
          ? details.exception.description : details.text;
        messages.push('EXCEPTION: ' + String(desc).split('\n').slice(0, 6).join(' | '));
      } else if (msg.method === 'Runtime.consoleAPICalled') {
        messages.push(msg.params.type + ': ' + msg.params.args
          .map((a) => (a.value !== undefined ? a.value : a.description)).join(' '));
      } else if (msg.method === 'Log.entryAdded') {
        messages.push('LOG/' + msg.params.entry.level + ': ' + msg.params.entry.text);
      }
    });
    await rpc(ws, 'Runtime.enable');
    await rpc(ws, 'Log.enable');
    await rpc(ws, 'Page.enable');
    await rpc(ws, 'Page.reload', { ignoreCache: true });
    await new Promise((r) => setTimeout(r, (Number(seconds) || 6) * 1000));
  });
  return messages.join('\n') || '(没有捕获到错误输出)';
}

async function shot(targetId, filePath, width, height) {
  const info = await fetch(`${BASE}/json/list`).then((r) => r.json());
  const target = info.find((t) => t.id === targetId);
  if (!target) throw new Error('target not found: ' + targetId);
  const { writeFileSync } = await import('node:fs');
  return withWs(target.webSocketDebuggerUrl, async (ws) => {
    await rpc(ws, 'Emulation.setDeviceMetricsOverride', {
      width: Number(width) || 1440, height: Number(height) || 900,
      deviceScaleFactor: 1, mobile: false,
    });
    await new Promise((r) => setTimeout(r, 700));
    const res = await rpc(ws, 'Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
    writeFileSync(filePath, Buffer.from(res.data, 'base64'));
    return filePath;
  });
}

async function evalOn(targetId, expression) {
  const info = await fetch(`${BASE}/json/list`).then((r) => r.json());
  const target = info.find((t) => t.id === targetId);
  if (!target) throw new Error(`target not found: ${targetId}`);
  return withWs(target.webSocketDebuggerUrl, async (ws) => {
    const res = await rpc(ws, 'Runtime.evaluate', {
      expression, returnByValue: true, awaitPromise: true,
    });
    if (res.exceptionDetails) throw new Error(JSON.stringify(res.exceptionDetails));
    return res.result?.value;
  });
}

async function clickSel(targetId, selector, index) {
  const expr = "(() => { const nodes = document.querySelectorAll(" + JSON.stringify(selector) + "); const node = nodes[" + (Number(index) || 0) + "]; if (!node) return { found: nodes.length, clicked: false }; node.click(); return { found: nodes.length, clicked: true }; })()";
  return evalOn(targetId, expr);
}

async function textSel(targetId, selector, index) {
  const expr = "(() => { const nodes = document.querySelectorAll(" + JSON.stringify(selector) + "); const node = nodes[" + (Number(index) || 0) + "]; return node ? node.textContent : null; })()";
  return evalOn(targetId, expr);
}

const [cmd, ...args] = process.argv.slice(2);
if (cmd === 'list') console.log(JSON.stringify(await list(), null, 1));
else if (cmd === 'open') console.log(await open(args[0]));
else if (cmd === 'click') console.log(JSON.stringify(await clickSel(args[0], args[1], args[2])));
else if (cmd === 'text') console.log(String(await textSel(args[0], args[1], args[2])));
else if (cmd === 'close') console.log(await close(args[0]) ? 'closed' : 'close-failed');
else if (cmd === 'eval') console.log(await evalOn(args[0], readFileSync(args[1], 'utf-8')));
else if (cmd === 'shot') console.log(await shot(args[0], args[1], args[2], args[3]));
else if (cmd === 'watch') console.log(await watch(args[0], args[1]));
else { console.error('usage: list | open <url> | eval <id> <file> | click <id> <sel> [i] | text <id> <sel> [i] | shot <id> <png> [w] [h] | close <id>'); process.exit(1); }