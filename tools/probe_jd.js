(() => {
  const out = { url: location.href, title: document.title };
  const candidates = [
    '.job-sec-text', '.job-description__content', '.job-description', '.job-detail-section',
    '.tCompany_main', '.bmsg', '.job-detail', '.job-intro-container',
    '[class*=job-description]', '[class*=job-detail]', '[class*=job-intro]', '[class*=description]'
  ];
  out.counts = {};
  for (const s of candidates) out.counts[s] = document.querySelectorAll(s).length;

  let best = null;
  let bestLen = 0;
  document.querySelectorAll('div,section,article').forEach((el) => {
    const text = (el.innerText || '').trim();
    if (el.children.length > 40) return;
    if (text.length > bestLen && text.length < 8000) { best = el; bestLen = text.length; }
  });
  out.biggest = best
    ? { cls: String(best.className).slice(0, 90), tag: best.tagName, len: bestLen,
        head: (best.innerText || '').replace(/\s+/g, ' ').slice(0, 220) }
    : null;

  const body = document.querySelector('.job-sec-text, .job-description__content, .bmsg');
  out.sample = body ? (body.innerText || '').replace(/\s+/g, ' ').slice(0, 220) : null;
  return JSON.stringify(out, null, 1);
})()