(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const out = { steps: [], before: location.href, tabsBefore: 0 };
  try { out.tabsBefore = (await (await fetch('http://127.0.0.1:9333/json/list')).json()).filter((t) => t.type === 'page').length; } catch (e) {}

  const btn = document.querySelector('.btn-startchat') || document.querySelector('[class*="startchat"]');
  if (!btn) return JSON.stringify({ error: 'no-startchat-button', html: (document.body.innerText || '').slice(0, 200) }, null, 1);
  out.steps.push('found:' + (btn.className || '') + ' text=' + (btn.innerText || '').trim());
  btn.click();
  out.steps.push('clicked');
  await sleep(3500);

  // 点完之后：当前页可能被替换成聊天页，也可能开了新标签
  const probe = () => {
    const pick = (sels) => {
      for (const s of sels) {
        const n = document.querySelector(s);
        if (n && n.getBoundingClientRect().width > 0) return { sel: s, tag: n.tagName, cls: (n.className || '').toString().slice(0, 40) };
      }
      return null;
    };
    return {
      url: location.href,
      title: document.title,
      input: pick(['.chat-input', '#chat-input', '.input-area textarea', 'textarea[class*="input"]', '[contenteditable="true"]', '.chat-editor [contenteditable]']),
      send: pick(['.btn-send', '[class*="send"] button', 'button[class*="send"]', '[ka="chat_send"]']),
      head: (document.body.innerText || '').replace(/\s+/g, ' ').slice(0, 220),
    };
  };
  out.after = probe();
  return JSON.stringify(out, null, 1);
})()