(() => {
  const out = { url: location.href, title: document.title };
  const candidates = [
    '.job-intro-container', '.job-title-box .ellipsis-1', '.job-salary', '.job-dq-box .ellipsis-1',
    '.job-detail-box', '[class*=job-intro]', '[class*=job-description]', '[class*=introduce]', '.paragraph'
  ];
  out.counts = {};
  for (const s of candidates) out.counts[s] = document.querySelectorAll(s).length;

  out.hasLogin = {
    qrcode: document.querySelectorAll('[class*=qrcode], [class*=qr-code], [class*=login]').length,
    text: /登录|扫码|手机号登录/.test((document.body.innerText || '').slice(0, 600))
  };

  let best = null;
  let bestLen = 0;
  document.querySelectorAll('div,section,article').forEach((el) => {
    const text = (el.innerText || '').trim();
    if (el.children.length > 40) return;
    if (text.length > bestLen && text.length < 8000) { best = el; bestLen = text.length; }
  });
  out.biggest = best
    ? { cls: String(best.className).slice(0, 90), len: bestLen,
        head: (best.innerText || '').replace(/\s+/g, ' ').slice(0, 240) }
    : null;
  return JSON.stringify(out, null, 1);
})()