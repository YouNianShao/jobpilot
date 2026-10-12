(() => {
  const box = document.querySelector('.job-intro-container');
  const out = { url: location.href };
  out.boxLen = box ? (box.innerText || '').length : 0;
  out.boxText = box ? (box.innerText || '').replace(/\s+/g, ' ').slice(0, 400) : null;
  out.children = box
    ? Array.from(box.querySelectorAll('*')).slice(0, 25).map((el) => ({
        tag: el.tagName,
        cls: String(el.className).slice(0, 60),
        len: (el.innerText || '').length
      }))
    : [];
  // 找 box 里文字最多的子块
  let best = null;
  let bestLen = 0;
  if (box) {
    box.querySelectorAll('div,section,p').forEach((el) => {
      const len = (el.innerText || '').length;
      if (len > bestLen) { best = el; bestLen = len; }
    });
  }
  out.bestChild = best ? { cls: String(best.className).slice(0, 70), len: bestLen } : null;
  return JSON.stringify(out, null, 1);
})()