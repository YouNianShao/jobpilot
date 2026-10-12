const r = await fetch("http://127.0.0.1:9222/json/version");
const info = await r.json();
const ws = new WebSocket(info.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
const out = await new Promise((resolve) => {
  ws.addEventListener("message", (ev) => { const m = JSON.parse(ev.data); if (m.id === 1) resolve(m.result); });
  ws.send(JSON.stringify({ id: 1, method: "Target.getTargets" }));
});
console.log(JSON.stringify(out.targetInfos.map((t) => ({ id: t.targetId, type: t.type, url: String(t.url || String()).slice(0, 80), attached: t.attached })), null, 1));
ws.close();
