/* 配置页的城市选择器（独立小部件：城市目录和初始值都由调用方传进来，不读全局状态）。 */

import { clear, el, icon } from '../../ui.js';

/* 城市选择器：chip 输入 + 原生 datalist 联想。
   受限平台（BOSS/51job 用平台自己的城市码）填错城市会被跳过，
   所以表外的城市要当场标红并说清后果，而不是等采集跑完才发现。 */
export function cityPicker(initial, catalog, platformName) {
  const hidden = el('input', { type: 'hidden', id: 'cfg::platform.cities' });
  const chipBox = el('span', { class: 'chip-box' });
  const listId = 'city-options';
  const entry = el('input', { class: 'chip-input', placeholder: '输入城市名，回车添加…' });
  const hint = el('div', { class: 'hint' });
  const box = el('div', { class: 'chips' }, [chipBox, entry]);
  const datalist = el('datalist', { id: listId });
  const restricted = !!(catalog && catalog.restricted);
  const known = new Set((catalog && catalog.cities) || []);
  let items = initial.filter(Boolean).slice();

  for (const name of (catalog && catalog.cities) || []) {
    datalist.appendChild(el('option', { value: name }));
  }
  entry.setAttribute('list', listId);

  const add = (value) => {
    const name = String(value || '').trim();
    if (!name || items.includes(name)) return;
    items.push(name);
    render();
  };

  let rendered = false;
  function render() {
    hidden.value = items.join(',');
    if (rendered) box.dispatchEvent(new Event('input', { bubbles: true }));
    rendered = true;
    clear(chipBox);
    for (const name of items) {
      const bad = restricted && !known.has(name);
      chipBox.appendChild(el('span', { class: 'chip' + (bad ? ' bad' : '') }, [
        name,
        el('button', {
          class: 'chip-x', type: 'button', title: '移除', 'aria-label': '移除 ' + name,
          onclick: () => { items = items.filter((x) => x !== name); render(); },
        }, [icon('x')]),
      ]));
    }
    const unknown = restricted ? items.filter((name) => !known.has(name)) : [];
    if (restricted && unknown.length) {
      hint.className = 'hint warn';
      hint.textContent = '⚠ ' + (platformName || '该平台') + ' 只认内置城市，这些会被跳过：'
        + unknown.join('、') + '（共支持 ' + (catalog.count || 0) + ' 个）';
    } else if (restricted) {
      hint.className = 'hint';
      hint.textContent = '已选 ' + items.length + ' 个 · ' + (platformName || '该平台')
        + ' 支持 ' + (catalog.count || 0) + ' 个城市，输入时有联想';
    } else {
      hint.className = 'hint';
      hint.textContent = '该平台按城市名直接搜索，填任意城市名都可以';
    }
  }

  entry.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); add(entry.value); entry.value = ''; }
    else if (event.key === 'Backspace' && !entry.value && items.length) { items.pop(); render(); }
  });
  entry.addEventListener('change', () => { add(entry.value); entry.value = ''; });   // datalist 选中

  render();
  // hidden 必须挂进页面：保存配置时按 id（cfg::platform.cities）取它。以前它没被放进 DOM，
  // 保存永远读到空串 —— 点一次「保存配置」，当前平台的城市就被清空了。
  const wrap = el('div', { class: 'city-field' }, [hidden, box, datalist]);
  if (restricted && (catalog.hot || []).length) {
    const hotRow = el('div', { class: 'chips hot' });
    hotRow.appendChild(el('span', { class: 'muted small', text: '常用：' }));
    for (const name of catalog.hot) {
      if (items.includes(name)) continue;
      hotRow.appendChild(el('button', {
        class: 'chip-btn', type: 'button', text: name, onclick: () => add(name),
      }));
    }
    wrap.appendChild(hotRow);
  }
  wrap.appendChild(hint);
  return wrap;
}


/* 关键词：和城市一样的「标签」输入 —— 输入一个词回车（或打逗号），就变成一个带 × 的标签，
   有确认感，也一眼看得出现在到底有几个关键词（以前是一行「agent,AI,数据分析」）。
   值仍写在 hidden input（id 不变）里，保存配置照旧按 id 读；外面要加词就 dispatch 'chip-add'。 */
export function keywordChips(id, initial) {
  const hidden = el('input', { type: 'hidden', id });
  const chipBox = el('span', { class: 'chip-box' });
  const entry = el('input', { class: 'chip-input', placeholder: '输入关键词，回车添加…', 'aria-label': '添加关键词' });
  const box = el('div', { class: 'chips' }, [chipBox, entry]);
  const hint = el('div', { class: 'hint' });
  let items = (initial || []).filter(Boolean).slice();
  function render() {
    hidden.value = items.join(',');
    clear(chipBox);
    for (const word of items) {
      chipBox.appendChild(el('span', { class: 'chip' }, [word,
        el('button', { class: 'chip-x', type: 'button', title: '移除', 'aria-label': '移除 ' + word,
          onclick: () => { items = items.filter((x) => x !== word); render(); } }, [icon('x')])]));
    }
    hint.textContent = items.length ? '共 ' + items.length + ' 个关键词 · 采集时每个关键词 × 每个城市各搜一遍'
      : '还没有关键词：采集会没有东西可搜';
    hint.className = 'hint' + (items.length ? '' : ' warn');
  }
  const add = (raw) => {
    let changed = false;
    for (const part of String(raw || '').split(/[,，、;；\n]+/)) {
      const word = part.trim();
      if (word && !items.includes(word)) { items.push(word); changed = true; }
    }
    if (changed) render();
    return changed;
  };
  entry.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ',' || event.key === '，') { event.preventDefault(); add(entry.value); entry.value = ''; }
    else if (event.key === 'Backspace' && !entry.value && items.length) { items.pop(); render(); }
  });
  entry.addEventListener('input', () => { if (/[,，]/.test(entry.value)) { add(entry.value); entry.value = ''; } });   // 输入法下逗号不一定触发 keydown
  entry.addEventListener('blur', () => { if (entry.value.trim()) { add(entry.value); entry.value = ''; } });
  hidden.addEventListener('chip-add', (event) => add(event.detail));
  render();
  return el('div', { class: 'city-field' }, [hidden, box, hint]);
}
