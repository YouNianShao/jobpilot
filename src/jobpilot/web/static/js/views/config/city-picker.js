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
