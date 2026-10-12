(async () => {
  const out = { url: location.href };
  for (let i = 0; i < 2; i++) {
    window.scrollTo(0, document.body.scrollHeight);
    await new Promise((r) => setTimeout(r, 2500));
  }
  const cards = Array.from(document.querySelectorAll('.job-card'));
  out.cards = cards.length;
  const last = cards[cards.length - 1];
  out.lastHTML = last ? last.outerHTML.slice(0, 1400) : null;
  out.dataAttrs = last
    ? Array.from(last.querySelectorAll('*'))
        .flatMap((el) => Array.from(el.attributes).filter((a) => a.name.startsWith('data-')))
        .map((a) => a.name + '=' + String(a.value).slice(0, 80))
        .slice(0, 20)
    : [];
  out.anchorHrefs = last ? Array.from(last.querySelectorAll('a[href]')).map((a) => a.getAttribute('href')).slice(0, 5) : [];
  // 内嵌 JSON 里到底有多少 positionNumber
  const html = document.documentElement.innerHTML;
  out.numbersInHtml = (html.match(/"positionNumber":"[A-Za-z0-9]{6,}"/g) || []).length;
  out.namesInHtml = (html.match(/"positionName":"[^"]{1,80}"/g) || []).length;
  const strict = html.match(/"positionName":"[^"]{1,80}"[^}]{0,200}?"positionNumber":"([A-Za-z0-9]{6,})"/g) || [];
  out.strictPairs = strict.length;
  return JSON.stringify(out, null, 1);
})()