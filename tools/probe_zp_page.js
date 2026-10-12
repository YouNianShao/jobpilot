(() => {
  const titleEl = document.querySelector('.job-card__title-main [aria-label]');
  const out = {
    url: location.href,
    cards: document.querySelectorAll('.job-card').length,
    firstCard: titleEl ? titleEl.getAttribute('aria-label') : null,
    titles: Array.from(document.querySelectorAll('.job-card [aria-label]')).slice(0, 5).map((el) => el.getAttribute('aria-label'))
  };
  const pageEls = Array.from(document.querySelectorAll('[class*=page], [class*=pagination], nav'))
    .filter((el) => /下一页|上一页|\d+\s*\/\s*\d+|共.*页/.test(el.innerText || ''));
  out.pageEls = pageEls.slice(0, 3).map((el) => ({
    tag: el.tagName,
    cls: String(el.className).slice(0, 70),
    text: (el.innerText || '').replace(/\s+/g, ' ').slice(0, 140)
  }));
  out.pageLinks = Array.from(document.querySelectorAll('a[href]'))
    .map((a) => a.getAttribute('href'))
    .filter((h) => /page|p=\d|pn=|curPage/.test(h)).slice(0, 8);
  out.totalHint = ((document.body.innerText || '').match(/共\s*[\d,]+\s*个?职位/) || [null])[0];
  return JSON.stringify(out, null, 1);
})()