(() => {
  const out = { url: location.href, title: document.title };
  const dump = (el, n) => el ? el.outerHTML.slice(0, n || 1200) : null;

  const lp = document.querySelector('a[data-nick="job-detail-job-info"]');
  out.liepin = {
    anchor: dump(lp, 700),
    parents: lp ? [0,1,2,3,4].map(i => { let e = lp; for (let j=0;j<i;j++) e = e && e.parentElement; return e ? (e.tagName + '.' + String(e.className).slice(0,70)) : null; }) : [],
    card: lp ? dump(lp.parentElement && lp.parentElement.parentElement, 1500) : null
  };

  const anchors = Array.from(document.querySelectorAll('a[href]')).filter(a => /jobdetail|\/job\//.test(a.getAttribute('href') || ''));
  out.zhaopin = { anchorCount: anchors.length };
  out.zhaopin.anchorSample = anchors.slice(0, 3).map(a => ({ href: a.getAttribute('href'), cls: String(a.className).slice(0, 70), text: (a.textContent || '').trim().slice(0, 50) }));
  if (anchors.length) {
    let card = anchors[0];
    for (let i = 0; i < 4 && card.parentElement; i++) card = card.parentElement;
    out.zhaopin.card = dump(card, 1800);
  }
  out.zhaopin.probes = {};
  for (const p of ['[class*=joblist]', '[class*=job-card]', '[class*=jobcard]', '[class*=position]', '[class*=job-item]', 'li[class*=job]', '[class*=card]']) {
    out.zhaopin.probes[p] = document.querySelectorAll(p).length;
  }
  return JSON.stringify(out, null, 1);
})()