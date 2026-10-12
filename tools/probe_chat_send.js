(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const input = document.querySelector('textarea.input-area');
  if (!input) return JSON.stringify({ error: 'no-input' });
  const out = {};

  // 只输入、不发送：看"发送"是怎么被触发的（先看有没有发送按钮出现）
  const setValue = (el, value) => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
    setter.call(el, value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
  };
  setValue(input, '测试文本（不会发送）');
  await sleep(800);

  const panel = document.querySelector('.dialog-wrap.startchat-dialog') || input.closest('div');
  out.afterTypingButtons = panel ? [...panel.querySelectorAll('button, [class*="btn"], [class*="send"], [ka]')]
    .filter((n) => n.getBoundingClientRect().width > 0)
    .map((n) => ({ tag: n.tagName, cls: (n.className || '').toString().slice(0, 40), ka: n.getAttribute('ka') || '', text: (n.innerText || '').trim().slice(0, 10) }))
    .slice(0, 10) : [];
  out.valueNow = input.value;

  // 清空草稿，别留东西在输入框里
  setValue(input, '');
  await sleep(300);
  out.cleared = input.value === '';
  return JSON.stringify(out, null, 1);
})()