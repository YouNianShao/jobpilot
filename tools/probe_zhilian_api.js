(async () => {
  const all = Array.from(new Set(performance.getEntriesByType('resource').map(e => e.name)));
  const urls = all.filter(u => /zhaopin\.com|zhaopin\.cn/.test(u) && !/\.(png|jpg|jpeg|gif|css|js|svg|woff2?|webp|ico|mp4)/i.test(u));
  const out = { candidates: urls.slice(0, 30) };
  const api = urls.find(u => /(search|position|job|list|query)/i.test(u) && /\/(api|sou|position|search)/i.test(u));
  out.api = api || null;
  if (api) {
    try {
      const r = await fetch(api, { credentials: 'include' });
      out.status = r.status;
      out.body = (await r.text()).slice(0, 1200);
    } catch (e) { out.err = String(e); }
  }
  return JSON.stringify(out, null, 1);
})()