(() => {
  const visible = (n) => !!n && n.getBoundingClientRect().width > 0 && n.getBoundingClientRect().height > 0;
  const out = { url: location.href, title: document.title };

  // 聊天面板容器：找包含输入框的最近祖先
  const input = document.querySelector('textarea.input-area') || document.querySelector('textarea[class*="input"]');
  out.input = input ? { sel: 'textarea.input-area', placeholder: input.placeholder || '', visible: visible(input) } : null;

  let panel = input;
  for (let i = 0; i < 6 && panel && panel.parentElement; i++) panel = panel.parentElement;
  out.panelCls = panel ? (panel.className || '').toString().slice(0, 80) : null;

  // 面板里所有可见按钮（找发送键）
  const buttons = panel ? [...panel.querySelectorAll('button, [class*="btn"], [ka]')]
    .filter(visible)
    .map((n) => ({ tag: n.tagName, cls: (n.className || '').toString().slice(0, 40), ka: n.getAttribute('ka') || '', text: (n.innerText || '').trim().slice(0, 12) }))
    .slice(0, 14) : [];
  out.buttons = buttons;

  // 有没有已经发出去的消息（气泡）
  const bubbles = document.querySelectorAll('[class*="message"], [class*="bubble"], .chat-message, .msg-item');
  out.bubbleCount = bubbles.length;
  out.bubbleSamples = [...bubbles].slice(-3).map((n) => (n.innerText || '').replace(/\s+/g, ' ').slice(0, 60));

  // 面板可见文本（判断是否已经自动发了招呼语）
  out.panelText = panel ? (panel.innerText || '').replace(/\s+/g, ' ').slice(0, 260) : '';
  return JSON.stringify(out, null, 1);
})()