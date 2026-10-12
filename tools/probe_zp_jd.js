(() => {
  const out = { url: location.href, title: document.title };
  const cands = Array.from(document.querySelectorAll('div,section,article'))
    .filter((el) => {
      const text = el.innerText || '';
      return text.includes('工作职责') || text.includes('职位描述') || text.includes('任职要求');
    })
    .map((el) => ({ el, len: (el.innerText || '').length }))
    .sort((a, b) => a.len - b.len);
  out.smallest = cands.slice(0, 5).map((c) => ({
    tag: c.el.tagName,
    cls: String(c.el.className).slice(0, 100),
    attrs: Array.from(c.el.attributes).map((a) => a.name).join(','),
    len: c.len,
    head: (c.el.innerText || '').replace(/\s+/g, ' ').slice(0, 120)
  }));
  // 有没有 data-* / 语义属性可以当锚点
  const holder = cands[0] ? cands[0].el : null;
  out.parentChain = [];
  let node = holder;
  for (let i = 0; i < 6 && node; i++) {
    out.parentChain.push(node.tagName + '.' + String(node.className).slice(0, 70));
    node = node.parentElement;
  }
  return JSON.stringify(out, null, 1);
})()