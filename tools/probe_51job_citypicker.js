(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const out = { steps: [] };

  // 1) 打开"工作地点"下拉
  const trigger = [...document.querySelectorAll('div,span,a,li')]
    .find((n) => n.children.length === 0 && (n.innerText || '').trim() === '工作地点');
  if (trigger) { trigger.click(); out.steps.push('clicked 工作地点'); } else { out.steps.push('no 工作地点'); }
  await sleep(1200);

  // 2) 点"其他城市"（如果出现）
  const other = [...document.querySelectorAll('a,li,span,div')]
    .find((n) => n.children.length === 0 && (n.innerText || '').trim() === '其他城市');
  if (other) { other.click(); out.steps.push('clicked 其他城市'); } else { out.steps.push('no 其他城市'); }
  await sleep(1500);

  // 3) 扫城市项：文本 + 6 位数字码（可能藏在属性/href/data-* 里）
  const found = [];
  document.querySelectorAll('a,li,span,div,button').forEach((node) => {
    const text = (node.innerText || '').trim();
    if (!text || text.length > 12 || /[\d]/.test(text)) return;
    const attrs = [node.getAttribute('href'), node.getAttribute('data-code'), node.getAttribute('data-area'),
      node.getAttribute('data-value'), node.getAttribute('value'), node.getAttribute('class')]
      .filter(Boolean).join(' ');
    const code = (attrs.match(/\b\d{6}\b/) || [])[0];
    if (code) found.push({ name: text, code, where: attrs.slice(0, 40) });
  });
  out.withCode = found.slice(0, 30);
  out.withCodeCount = found.length;
  out.visibleText = (document.body.innerText || '').replace(/\s+/g, ' ').slice(0, 500);
  return JSON.stringify(out, null, 1);
})()