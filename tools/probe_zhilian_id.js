(() => {
  const card = document.querySelector('.job-card');
  const out = { url: location.href };
  out.cardAttrs = card ? Array.from(card.attributes).map(a => a.name + '=' + String(a.value).slice(0, 120)) : [];
  out.fullHTML = card ? card.outerHTML.slice(0, 3200) : null;
  const withData = card ? Array.from(card.querySelectorAll('*')).map(e => ({
    tag: e.tagName,
    cls: String(e.className).slice(0, 45),
    attrs: Array.from(e.attributes).filter(a => a.name.startsWith('data-') || a.name === 'href' || a.name === 'aria-label')
      .map(a => a.name + '=' + String(a.value).slice(0, 90))
  })).filter(x => x.attrs.length) : [];
  out.nodesWithAttrs = withData.slice(0, 20);
  const anyLink = card ? Array.from(card.querySelectorAll('a[href]')).map(a => a.getAttribute('href')) : [];
  out.cardLinks = anyLink;
  return JSON.stringify(out, null, 1);
})()