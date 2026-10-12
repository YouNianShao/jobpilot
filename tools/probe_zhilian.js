(() => {
  const out = { url: location.href, title: document.title };
  const counts = {};
  document.querySelectorAll('[class]').forEach(el => {
    String(el.className || '').split(/\s+/).forEach(c => {
      if (c && /job|position|list|card|item|result/i.test(c)) counts[c] = (counts[c] || 0) + 1;
    });
  });
  out.classCounts = Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 45);

  const cands = Array.from(document.querySelectorAll('div,li,section,article')).filter(el => {
    const t = (el.innerText || '').replace(/\s+/g, ' ');
    return t.length > 30 && t.length < 400 && /(\d+(\.\d+)?-\d+(\.\d+)?万|\d{4,6}\s*元)/.test(t);
  });
  out.candCount = cands.length;
  out.samples = cands.slice(0, 3).map(el => ({
    tag: el.tagName,
    cls: String(el.className).slice(0, 90),
    text: (el.innerText || '').replace(/\s+/g, ' ').slice(0, 140),
    html: el.outerHTML.slice(0, 800)
  }));
  // 岗位链接：不限 href 关键词，找 a 的文本含"数据分析"
  const links = Array.from(document.querySelectorAll('a[href]')).filter(a => /数据分析/.test(a.textContent || ''));
  out.links = links.slice(0, 3).map(a => ({ href: a.getAttribute('href'), cls: String(a.className).slice(0, 60) }));
  return JSON.stringify(out, null, 1);
})()