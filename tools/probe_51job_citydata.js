(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const trigger = [...document.querySelectorAll('div,span,a,li')]
    .find((n) => n.children.length === 0 && (n.innerText || '').trim() === '工作地点');
  if (trigger) trigger.click();
  await sleep(1500);

  // 找到"选择城市"那个弹窗，从它的组件树往下翻
  const dialog = [...document.querySelectorAll('div')]
    .find((n) => /选择城市/.test((n.innerText || '').slice(0, 40)) && n.querySelectorAll('div').length > 8);
  const cities = [];
  const seen = new Set();
  const visit = (obj, depth) => {
    if (!obj || typeof obj !== 'object' || depth > 12 || seen.has(obj)) return;
    seen.add(obj);
    if (Array.isArray(obj)) {
      for (const item of obj) {
        if (item && typeof item === 'object') {
          const code = String(item.code ?? item.value ?? item.cityCode ?? item.areaCode ?? '');
          const name = String(item.name ?? item.label ?? item.cityName ?? item.shortName ?? '');
          if (/^\d{6}$/.test(code) && name && name.length <= 12) cities.push({ name, code });
          visit(item, depth + 1);
        }
      }
      return;
    }
    for (const key of Object.keys(obj)) {
      if (key.startsWith('__') && !key.startsWith('__vue')) continue;
      let v;
      try { v = obj[key]; } catch (e) { continue; }
      if (v && typeof v === 'object') visit(v, depth + 1);
    }
  };
  const roots = [];
  if (dialog) {
    if (dialog.__vue__) roots.push(dialog.__vue__);
    if (dialog.__vueParentComponent) roots.push(dialog.__vueParentComponent);
    dialog.querySelectorAll('*').forEach((el) => {
      if (el.__vue__) roots.push(el.__vue__);
      if (el.__vueParentComponent) roots.push(el.__vueParentComponent);
    });
  }
  roots.slice(0, 60).forEach((r) => visit(r, 0));

  const uniq = {};
  for (const row of cities) uniq[row.name] = row;
  const rows = Object.values(uniq);
  return JSON.stringify({
    dialogFound: !!dialog,
    roots: roots.length,
    cityCount: rows.length,
    cities: rows.slice(0, 25),
  }, null, 1);
})()