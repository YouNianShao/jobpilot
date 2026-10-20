/* 「运行全流程」前的设置面板（右侧滑出）。

   为什么要它：以前点「运行全流程」只跑顶栏下拉框里那一个平台，按钮上看不出来，
   用户不知道启动的是一个平台还是四个。现在点了先出这个面板：
   - 每个平台一行：开关 + 一句话写清这一次搜什么；打开后能就地改关键词 / 城市 / 页数
   - 不能自动投递的平台照样能勾（只采集、评分），行里直接写明「这个平台不会自动投递」
   - 底部按真实执行顺序写出这一轮会做什么，最后一步（投递）单独一个开关
   这里改的关键词 / 城市 / 页数**只用这一次**，不改配置页里的长期设置。 */

import { get } from '../api.js';
import { platformName } from '../common.js';
import { clear, el, icon } from '../ui.js';

const ORDER = ['boss', '51job', 'liepin', 'zhaopin'];

function chipEditor(initial, { placeholder, catalog, label }) {
  let items = (initial || []).filter(Boolean).slice();
  const known = new Set((catalog && catalog.cities) || []);
  const restricted = !!(catalog && catalog.restricted);
  const box = el('span', { class: 'chip-box' });
  const entry = el('input', { class: 'chip-input', placeholder, 'aria-label': label });
  const note = el('div', { class: 'hint' });
  const wrap = el('div', { class: 'fs-chips' }, [el('div', { class: 'chips' }, [box, entry]), note]);
  if (restricted) {
    const listId = 'fs-city-' + Math.random().toString(36).slice(2, 8);
    const datalist = el('datalist', { id: listId }, catalog.cities.map((c) => el('option', { value: c })));
    entry.setAttribute('list', listId);
    wrap.appendChild(datalist);
  }
  const render = () => {
    clear(box);
    for (const name of items) {
      const bad = restricted && !known.has(name);
      box.appendChild(el('span', { class: 'chip' + (bad ? ' bad' : '') }, [name,
        el('button', { class: 'chip-x', type: 'button', 'aria-label': '移除 ' + name,
          onclick: () => { items = items.filter((x) => x !== name); render(); wrap.dispatchEvent(new Event('change')); } },
        [icon('x')])]));
    }
    const unknown = restricted ? items.filter((n) => !known.has(n)) : [];
    note.className = 'hint' + (unknown.length ? ' warn' : '');
    note.textContent = unknown.length ? '这个平台不认识：' + unknown.join('、') + '，采集时会跳过' : '';
    note.hidden = !unknown.length;
  };
  const add = (raw) => {
    for (const part of String(raw || '').split(/[,，、\s]+/)) {
      const v = part.trim();
      if (v && !items.includes(v)) items.push(v);
    }
    render();
    wrap.dispatchEvent(new Event('change'));
  };
  entry.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); add(entry.value); entry.value = ''; }
    else if (e.key === 'Backspace' && !entry.value && items.length) { items.pop(); render(); wrap.dispatchEvent(new Event('change')); }
  });
  entry.addEventListener('blur', () => { if (entry.value.trim()) { add(entry.value); entry.value = ''; } });
  render();
  wrap.read = () => items.slice();
  return wrap;
}
function toggle(on, label, onChange) {
  const btn = el('button', { class: 'fs-switch', type: 'button', role: 'switch', 'aria-label': label });
  const set = (value) => { btn.setAttribute('aria-checked', value ? 'true' : 'false'); };
  btn.addEventListener('click', () => { const next = btn.getAttribute('aria-checked') !== 'true'; set(next); onChange(next); });
  set(on);
  return btn;
}

/* 一个平台一行。返回 { node, plan() }：plan() = null 表示没勾选 */
function platformRow(key, info, cfg, catalog, { preselect, onChange }) {
  const canApply = (info.capabilities || []).includes('apply') && cfg.apply !== false;
  const locked = info.locked;
  // 这个平台已经有任务在跑（同一平台一次只能跑一个）：置灰、不能勾，等它结束再来
  const busy = !!info.busy;
  let on = preselect && cfg.enabled !== false && !busy;
  const kw = chipEditor(cfg.keywords, { placeholder: '输入关键词，回车添加', label: info.name + ' 关键词' });
  const city = chipEditor(cfg.cities, { placeholder: '输入城市，回车添加', label: info.name + ' 城市', catalog });
  const pages = el('input', { type: 'number', min: '1', max: '20', value: String(cfg.max_pages || 3), 'aria-label': info.name + ' 每组页数' });
  const summary = el('div', { class: 'fs-sum' });
  const editor = el('div', { class: 'fs-edit' }, [
    el('label', { class: 'fs-lab', text: '关键词' }), kw,
    el('label', { class: 'fs-lab', text: '城市' }), city,
    el('label', { class: 'fs-lab', text: '每组页数' }), el('div', { class: 'fs-pages' }, [pages,
      el('span', { class: 'muted small', text: '关键词 × 城市，每个组合翻这么多页' })]),
  ]);
  const badges = el('span', { class: 'fs-badges' }, [
    el('span', { class: 'pill ' + (canApply ? 'ok' : ''), text: canApply ? '可自动投递' : '只采集、评分' }),
    locked ? el('span', { class: 'pill warn', text: '风控暂停中' }) : null,
    busy ? el('span', { class: 'pill live', text: '正在跑任务' }) : null,
  ]);
  const paint = () => {
    const k = kw.read(); const c = city.read();
    summary.textContent = on
      ? (k.join('、') || '没填关键词') + ' · ' + (c.join('、') || '没填城市') + ' · 每组 ' + (pages.value || 3) + ' 页'
      : (busy ? '正在跑任务，等它结束才能再选' : (cfg.enabled === false ? '在配置里是停用的' : '这一轮不跑'));
    node.classList.toggle('is-on', on);
    editor.hidden = !on;
  };
  const sw = toggle(on, '这一轮跑' + info.name, (next) => { on = next; paint(); onChange(); });
  if (cfg.enabled === false || busy) sw.disabled = true;
  const node = el('div', { class: 'fs-row' + (busy ? ' is-busy' : '') }, [
    el('div', { class: 'fs-row-head' }, [sw, el('div', { class: 'fs-name' }, [el('b', { text: info.name }), summary]), badges]),
    editor,
  ]);
  for (const part of [kw, city, pages]) part.addEventListener(part === pages ? 'input' : 'change', () => { paint(); onChange(); });
  paint();
  return {
    node, key, canApply,
    plan() {
      if (!on) return null;
      return { platform: key, keywords: kw.read(), cities: city.read(), max_pages: Math.max(1, Math.min(20, parseInt(pages.value, 10) || 3)) };
    },
    problem() {
      if (!on) return '';
      if (!kw.read().length) return info.name + '：至少要一个关键词';
      if (!city.read().length) return info.name + '：至少要一个城市';
      return '';
    },
  };
}
/* 打开面板。start(body) 由调用方提供（发 /api/tasks），返回 { error } 或任务快照 */
export async function openFlowSheet({ status, config, preselect, start }) {
  const platforms = (status && status.platforms) || {};
  const keys = ORDER.filter((k) => platforms[k]).concat(Object.keys(platforms).filter((k) => !ORDER.includes(k)));
  const catalogs = await Promise.all(keys.map((k) => get('/api/cities?platform=' + encodeURIComponent(k))));
  const approved = (status && status.approved_by_platform) || {};
  const busy = new Set((status && status.busy_platforms) || []);

  const backdrop = el('div', { class: 'fs-backdrop' });
  const steps = el('ol', { class: 'fs-steps' });
  const err = el('div', { class: 'field-error', role: 'alert' });
  err.hidden = true;
  const applyNote = el('div', { class: 'muted small' });
  let applyAfter = true;
  const okBtn = el('button', { class: 'btn primary', type: 'button' }, [icon('play'), '开始']);
  const rows = [];
  const refreshPlan = () => {
    const chosen = rows.filter((r) => r.plan());
    const names = chosen.map((r) => platforms[r.key].name);
    const applicable = chosen.filter((r) => r.canApply);
    const waiting = applicable.reduce((n, r) => n + (approved[r.key] || 0), 0);
    clear(steps);
    const step = (title, text) => steps.appendChild(el('li', {}, [el('b', { text: title }), el('span', { text })]));
    step('采集', names.length ? names.join('、') + ' 同时开始，每个平台一个独立任务（各自的间隔 / 额度 / 风控照旧）' : '还没选平台');
    step('评分 · 写招呼语', '每个平台评自己的新岗位；某个平台出问题只停它自己，其它平台继续');
    step('等你确认', '评分过线的岗位停在「待确认」，你确认之后才会投递');
    applyNote.textContent = !applyAfter ? '这一轮不投递：停在评分之后。'
      : applicable.length
        ? '只有一个待确认的都没有时才会投：在 ' + applicable.map((r) => platforms[r.key].name).join('、')
          + ' 里轮流投已确认的岗位（现在已确认 ' + waiting + ' 个），发出后撤不回。'
        : '选的平台都不能自动投递，这一轮不会投。';
    okBtn.disabled = !names.length;
    okBtn.lastChild.textContent = names.length ? '开始（' + names.length + ' 个平台）' : '开始';
  };
  keys.forEach((key, i) => {
    const cfg = (config && config.platforms && config.platforms[key]) || {};
    const info = { ...platforms[key], locked: !!((status && status.locks) || {})[key], busy: busy.has(key) };
    const cat = catalogs[i] && !catalogs[i].error ? catalogs[i] : null;
    rows.push(platformRow(key, info, cfg, cat, { preselect: preselect.includes(key), onChange: refreshPlan }));
  });
  const applySwitch = toggle(true, '没有待确认时接着投递', (v) => { applyAfter = v; refreshPlan(); });

  const sheet = el('aside', { class: 'fs-sheet', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'fsTitle' }, [
    el('div', { class: 'fs-head' }, [
      el('div', {}, [el('h3', { id: 'fsTitle', text: '这一轮全流程跑哪些平台' }),
        el('p', { class: 'muted small', text: '这里改的关键词、城市、页数只用这一次，不改配置页里的设置。' })]),
      el('button', { class: 'btn ghost icon', type: 'button', 'aria-label': '关闭', onclick: () => close(false) }, [icon('x')]),
    ]),
    el('div', { class: 'fs-body' }, [
      el('div', { class: 'fs-list' }, rows.map((r) => r.node)),
      el('section', { class: 'fs-plan' }, [el('h4', { text: '这一轮会做什么' }), steps,
        el('div', { class: 'fs-apply' }, [applySwitch, el('div', {}, [el('b', { text: '最后：投递' }), applyNote])])]),
    ]),
    el('div', { class: 'fs-foot' }, [err, el('div', { class: 'grow' }),
      el('button', { class: 'btn ghost', type: 'button', text: '取消', onclick: () => close(false) }), okBtn]),
  ]);
  const before = document.activeElement;
  let resolver;
  const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); close(false); } };
  function close(value) {
    document.removeEventListener('keydown', onKey, true);
    backdrop.classList.remove('is-open'); sheet.classList.remove('is-open');
    setTimeout(() => { backdrop.remove(); sheet.remove(); }, 260);
    if (before && before.focus) before.focus();
    resolver(value);
  }
  backdrop.addEventListener('click', () => close(false));
  okBtn.addEventListener('click', async () => {
    const problem = rows.map((r) => r.problem()).find(Boolean);
    if (problem) { err.hidden = false; err.textContent = problem; return; }
    err.hidden = true;
    okBtn.disabled = true;
    // 每个平台一个独立的全流程任务，同时开始（2026-10-19 用户要求：已经能并行，就别串行等）。
    // 某个平台没能开始（例如刚好在跑别的任务），只报它，其它平台照常开
    const plans = rows.map((r) => r.plan()).filter(Boolean);
    const body = (plan) => ({ mode: 'full', plans: [plan], apply_after: applyAfter });
    const results = await Promise.all(plans.map((plan) => start(body(plan))));
    okBtn.disabled = false;
    const failed = results.map((res, i) => [plans[i], res]).filter(([, res]) => res && res.error);
    if (failed.length === plans.length) {          // 一个都没开成：面板留着，写清原因
      err.hidden = false;
      err.textContent = failed.map(([p, r]) => platformName(p.platform) + '：' + r.error).join('；');
      return;
    }
    // 部分平台没开成：其它平台已经在跑，关掉面板，把没开成的原因交给调用方提示
    close({ started: plans.length - failed.length, failed: failed.map(([p, r]) => platformName(p.platform) + '：' + r.error) });
  });
  refreshPlan();
  document.body.append(backdrop, sheet);
  document.addEventListener('keydown', onKey, true);
  requestAnimationFrame(() => { backdrop.classList.add('is-open'); sheet.classList.add('is-open'); okBtn.focus(); });
  return new Promise((resolve) => { resolver = resolve; });
}
