(() => {
  const out = { url: location.href, title: document.title };

  const card = document.querySelector('.joblist-item');
  out.j51 = {
    html: card ? card.outerHTML.slice(0, 2600) : null,
    anchors: card ? Array.from(card.querySelectorAll('a[href]')).map(a => ({
      href: a.getAttribute('href'),
      cls: String(a.className).slice(0, 50),
      txt: (a.textContent || '').trim().slice(0, 28)
    })) : [],
    attrs: card ? Array.from(card.querySelectorAll('*')).slice(0, 25).map(e => ({
      tag: e.tagName,
      a: Array.from(e.attributes).filter(x => x.name.startsWith('data-') || x.name === 'href').map(x => x.name + '=' + String(x.value).slice(0, 120))
    })).filter(x => x.a.length) : []
  };

  const html = document.documentElement.innerHTML;
  out.zp = {
    numbers: Array.from(new Set(html.match(/\bC[CLZ]\d{6,}[A-Z0-9_~-]*/g) || [])).slice(0, 12),
    scriptHits: []
  };
  const scripts = Array.from(document.querySelectorAll('script')).map(s => s.textContent || '').filter(t => t.length > 80);
  for (const text of scripts) {
    const index = text.search(/jobNumber|positionNumber|jobId|"number"/);
    if (index >= 0) {
      out.zp.scriptHits.push(text.slice(Math.max(0, index - 100), index + 220));
      if (out.zp.scriptHits.length >= 3) break;
    }
  }
  return JSON.stringify(out, null, 1);
})()