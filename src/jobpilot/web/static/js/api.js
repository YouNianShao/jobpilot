const json = { 'Content-Type': 'application/json' };

/* 连接状态：服务断开时广播一次 jp:offline，恢复时广播 jp:online（界面据此显示离线条）。
   以前 fetch 一失败就直接抛出：轮询那条 setTimeout 链跟着断掉，服务回来了页面也不会再刷新。 */
let online = true;
function setOnline(value) {
  if (online === value) return;
  online = value;
  window.dispatchEvent(new Event(value ? 'jp:online' : 'jp:offline'));
}

async function request(path, options) {
  let res;
  try {
    res = await fetch(path, options);
  } catch (e) {
    setOnline(false);
    return { error: '连不上看板服务', offline: true };
  }
  setOnline(true);
  const text = await res.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch (e) { data = { error: text.slice(0, 200) }; }
  if (!res.ok && !data.error) data.error = 'HTTP ' + res.status;
  return data;
}

export const get = (path) => request(path);
export const post = (path, body) => request(path, { method: 'POST', headers: json, body: JSON.stringify(body || {}) });
/* 原始字节上传（简历导入）：不走 multipart，服务端直接读请求体 */
export const upload = (path, file) => request(path, {
  method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file,
});
