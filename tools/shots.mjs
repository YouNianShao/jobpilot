// 一键截图（开发用）：把看板每个视图各截一张，作为 UI 改动的真机证据。
// 用法: node tools/shots.mjs [outDir] [width] [height]
import { writeFileSync, mkdirSync } from 'node:fs';

const PORT = process.env.CDP_PORT || '9333';
const BASE = `http://127.0.0.1:${PORT}`;
const START_URL = process.env.SHOT_URL || 'http://127.0.0.1:8700/';
const outDir = process.argv[2] || 'data/shots';
const W = Number(process.argv[3]) || 1600;
const H = Number(process.argv[4]) || 1000;

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

async function withWs(u, fn) {
  const ws = new WebSocket(u);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  try { return await fn(ws); } finally { ws.close(); }
}

async function targetWs(targetId) {
  const list = await fetch(`${BASE}/json/list`).then((r) => r.json());
  const t = list.find((x) => x.id === targetId);
  if (!t) throw new Error('target not found: ' + targetId);
  return t.webSocketDebuggerUrl;
}

async function open(u) {
  const r = await fetch(`${BASE}/json/new?${encodeURIComponent(u)}`, { method: 'PUT' });
  if (!r.ok) throw new Error('cannot open tab: ' + r.status);
  return (await r.json()).id;
}

async function evalOn(targetId, expression) {
  return withWs(await targetWs(targetId), (ws) =>
    rpc(ws, 'Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }));
}

async function shot(targetId, filePath) {
  return withWs(await targetWs(targetId), async (ws) => {
    await rpc(ws, 'Emulation.setDeviceMetricsOverride',
      { width: W, height: H, deviceScaleFactor: 1, mobile: false });
    await new Promise((r) => setTimeout(r, 800));
    const res = await rpc(ws, 'Page.captureScreenshot', { format: 'png' });
    writeFileSync(filePath, Buffer.from(res.data, 'base64'));
  });
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const VIEWS = ['overview', 'jobs', 'monitor', 'activity', 'config'];

mkdirSync(outDir, { recursive: true });
const target = await open(START_URL);
await wait(3500);
if (process.env.SHOT_THEME) {
  await evalOn(target, `document.documentElement.setAttribute('data-theme', '${process.env.SHOT_THEME}')`);
  await wait(400);
}

for (const view of VIEWS) {
  await evalOn(target, `document.querySelector('.nav-item[data-view="${view}"]').click()`);
  await wait(1500);
  await shot(target, `${outDir}/${view}.png`);
  console.log(`${outDir}/${view}.png`);
}

// 抽屉：点开岗位池第一行
await evalOn(target, `document.querySelector('.nav-item[data-view="jobs"]').click()`);
await wait(1500);
const opened = await evalOn(target, "const r=document.querySelector('#jobs tr'); if(r){r.click();true}else{false}");
if (opened && opened.result && opened.result.value) {
  await wait(1500);
  await shot(target, `${outDir}/drawer.png`);
  console.log(`${outDir}/drawer.png`);
}
await fetch(`${BASE}/json/close/${target}`);
console.log('done');