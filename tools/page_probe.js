(async () => {
  const out = {};
  try {
    const res = await fetch('/api/status');
    out.statusHttp = res.status;
    const data = await res.json();
    out.hasPlatforms = !!data.platforms;
    out.apiError = data.error || null;
  } catch (e) { out.statusThrew = String(e); }
  try {
    const res = await fetch('/api/health');
    out.healthHttp = res.status;
    out.healthBody = (await res.text()).slice(0, 100);
  } catch (e) { out.healthThrew = String(e); }
  out.platformOptions = document.getElementById('platform').options.length;
  out.kpiCards = document.getElementById('kpis').children.length;
  out.riskChildren = document.getElementById('risk').children.length;
  out.hasBtnBin = !!document.getElementById('btnBin');
  out.hasConfigForm = !!document.getElementById('configForm');
  out.browserText = document.getElementById('browserText').textContent;

  // 逐个点开视图：任何一个视图渲染成空白都要在这里暴露出来
  out.views = {};
  for (const name of ['overview', 'jobs', 'monitor', 'activity', 'config']) {
    const nav = document.querySelector(`.nav-item[data-view="${name}"]`);
    if (!nav) { out.views[name] = { missing: true }; continue; }
    nav.click();
    await new Promise((r) => setTimeout(r, 900));
    const section = document.querySelector(`.view[data-view="${name}"]`);
    out.views[name] = {
      active: !!section && section.classList.contains('is-active'),
      nodes: section ? section.querySelectorAll('*').length : 0,
      text: section ? (section.innerText || '').trim().length : 0,
    };
  }
  document.querySelector('.nav-item[data-view="overview"]').click();
  return JSON.stringify(out, null, 1);
})()