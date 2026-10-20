/* 通用 UI 原语：只负责"画"，不碰业务逻辑 */

export function el(tag, attrs, children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? '' : value);
  }
  for (const child of [].concat(children || [])) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  return node;
}

export function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

export function icon(name, cls) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', cls ? 'ico ' + cls : 'ico');
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', '#i-' + name);
  svg.appendChild(use);
  return svg;
}

const TOAST_ICON = { ok: 'check', err: 'alert', warn: 'alert' };

/* 轻提示：成功 2.6 秒自动消失；错误留 6 秒、可以手动关（错误信息常常需要多看两眼） */
/* 翻页条（岗位池 / 任务历史 / 风控事件 / 监测页共用）：
   box = 容器；info = {page, pages, total, limit}；go(n) = 翻到第 n 页。只有一页时整条隐藏。
   sizes = {options: [20, 50, 100], onChange(n)}（可选）：带「每页 N 条」下拉。
   带了下拉时，只要总数超过最小档就显示整条 —— 否则一页装得下时，用户就没处把每页改小 / 改回去。 */
export function renderPager(box, info, go, sizes) {
  clear(box);
  const options = (sizes && sizes.options) || [];
  const showSizes = !!(info && options.length && info.total > Math.min(...options));
  if (!info || (info.pages <= 1 && !showSizes)) { box.hidden = true; return; }
  box.hidden = false;
  const page = info.page;
  const to = (n) => { const target = Math.min(Math.max(1, n), info.pages); if (target !== page) go(target); };
  const from = info.total ? (page - 1) * info.limit + 1 : 0;
  const end = Math.min(info.total, page * info.limit);
  box.appendChild(el('span', { class: 'pager-info', text: '第 ' + from + '–' + end + ' 条，共 ' + info.total + ' 条' }));
  if (showSizes) box.appendChild(pageSizeSelect(options, info.limit, sizes.onChange));
  box.appendChild(el('div', { class: 'grow' }));
  if (info.pages <= 1) return;   // 只有一页：留着「每页 N 条」，不画页码
  const nav = el('nav', { class: 'pager-nav', 'aria-label': '翻页' });
  nav.appendChild(el('button', { class: 'btn small ghost', type: 'button', text: '上一页', disabled: page <= 1, onclick: () => to(page - 1) }));
  // 页码：首页、末页、当前页前后各两页，其余用省略号
  const wanted = new Set([1, info.pages]);
  for (let n = page - 2; n <= page + 2; n += 1) if (n >= 1 && n <= info.pages) wanted.add(n);
  let last = 0;
  for (const n of [...wanted].sort((a, b) => a - b)) {
    if (n - last > 1) nav.appendChild(el('span', { class: 'pager-gap', text: '…' }));
    nav.appendChild(el('button', {
      class: 'pager-num' + (n === page ? ' is-active' : ''), type: 'button', text: String(n),
      'aria-current': n === page ? 'page' : null, onclick: () => to(n),
    }));
    last = n;
  }
  nav.appendChild(el('button', { class: 'btn small ghost', type: 'button', text: '下一页', disabled: page >= info.pages, onclick: () => to(page + 1) }));
  box.appendChild(nav);
  if (info.pages > 7) {
    const jump = el('input', {
      type: 'number', class: 'pager-jump', min: '1', max: String(info.pages), 'aria-label': '跳到第几页', placeholder: '页码',
      onkeydown: (event) => { if (event.key === 'Enter') to(Number(event.target.value) || 1); },
    });
    box.appendChild(el('label', { class: 'pager-jump-wrap' }, ['跳至', jump, '页']));
  }
}

/* 「每页 N 条」下拉；当前值不在选项里（比如旧的 50 条接口）也照样显示出来，不会悄悄跳档 */
export function pageSizeSelect(options, current, onChange) {
  const values = options.includes(current) ? options : [...options, current].sort((a, b) => a - b);
  const select = el('select', {
    class: 'select pager-size', 'aria-label': '每页显示条数',
    onchange: (event) => onChange(Number(event.target.value)),
  }, values.map((n) => el('option', { value: String(n), text: String(n), selected: n === current })));
  return el('label', { class: 'pager-size-wrap' }, ['每页', select, '条']);
}

/* 每页条数记在浏览器里（按表区分）：刷新 / 下次打开还是用户选的那一档 */
export function loadPageSize(key, options, fallback) {
  let saved = 0;
  try { saved = Number(localStorage.getItem('jp.pageSize.' + key)) || 0; } catch (e) { saved = 0; }
  return options.includes(saved) ? saved : fallback;
}

export function savePageSize(key, size) {
  try { localStorage.setItem('jp.pageSize.' + key, String(size)); } catch (e) { /* 隐私模式存不了就不记 */ }
}

/* options.action / options.onAction：提示里带一个按钮（例如「撤销」），点了执行并关掉提示 */
export function toast(message, kind, options) {
  const box = document.getElementById('toasts');
  const node = el('div', { class: 'toast ' + (kind || ''), role: kind === 'err' ? 'alert' : 'status' }, [
    icon(TOAST_ICON[kind] || 'spark'),
    el('div', { class: 'toast-text', text: message }),
  ]);
  let timer = 0;
  const leave = () => {
    clearTimeout(timer);
    node.classList.add('is-leaving');
    setTimeout(() => node.remove(), 300);
  };
  const action = options && options.action;
  if (action) {
    node.appendChild(el('button', {
      class: 'btn small ghost toast-action', type: 'button', text: action,
      onclick: () => { leave(); options.onAction(); },
    }));
  }
  node.appendChild(el('button', { class: 'toast-x', type: 'button', 'aria-label': '关闭提示', onclick: leave }, [icon('x')]));
  box.appendChild(node);
  while (box.children.length > 4) box.firstChild.remove();   // 别把屏幕堆满
  // 带「撤销」的提示多留一会儿，给人反应时间
  timer = setTimeout(leave, kind === 'err' ? 6000 : (action ? 6000 : 2600));
  node.addEventListener('mouseenter', () => clearTimeout(timer));
  node.addEventListener('mouseleave', () => { timer = setTimeout(leave, 2000); });
}

/* 共用一个模态框：确认（两个按钮）和错误弹窗（一个按钮）。
   - Esc / 点遮罩 = 取消；打开时焦点给主按钮（危险操作给「取消」，防止手快回车）
   - 关闭后焦点还给打开前的元素，键盘用户不会"迷路" */
function openModal({ title, text, okText, cancelText, kind, alert }) {
  return new Promise((resolve) => {
    const backdrop = document.getElementById('modalBackdrop');
    const box = document.getElementById('modalBox');
    const ok = document.getElementById('modalOk');
    const cancel = document.getElementById('modalCancel');
    const ico = document.getElementById('modalIco');
    const before = document.activeElement;
    document.getElementById('modalTitle').textContent = title;
    document.getElementById('modalText').textContent = text || '';
    box.className = 'modal' + (alert ? ' is-alert' : '') + (kind ? ' ' + kind : '');
    clear(ico);
    ico.appendChild(icon(kind === 'bad' ? 'alert' : (kind === 'warn' ? 'alert' : 'check')));
    ok.textContent = okText || (alert ? '知道了' : '确定');
    ok.className = 'btn ' + (kind === 'bad' && !alert ? 'danger' : 'primary');
    cancel.textContent = cancelText || '取消';
    cancel.hidden = !!alert;
    backdrop.classList.add('is-open');
    const done = (value) => {
      backdrop.classList.remove('is-open');
      ok.removeEventListener('click', onOk);
      cancel.removeEventListener('click', onCancel);
      backdrop.removeEventListener('click', onBackdrop);
      document.removeEventListener('keydown', onKey, true);
      if (before && typeof before.focus === 'function') before.focus();
      resolve(value);
    };
    const onOk = () => done(true);
    const onCancel = () => done(false);
    const onBackdrop = (event) => { if (event.target === backdrop) done(!!alert); };
    const onKey = (event) => {
      if (event.key === 'Escape') { event.stopPropagation(); done(!!alert); }
    };
    ok.addEventListener('click', onOk);
    cancel.addEventListener('click', onCancel);
    backdrop.addEventListener('click', onBackdrop);
    document.addEventListener('keydown', onKey, true);
    setTimeout(() => ((kind === 'bad' && !alert) ? cancel : ok).focus(), 30);
  });
}

export function confirmDialog(title, text, options) {
  return openModal({ title, text, ...(options || {}) });
}

/* 错误反馈弹窗：用在"用户必须读完才能继续"的失败上（保存失败、任务起不来），
   普通的成功 / 小错误仍然用 toast，不打断操作 */
export function alertDialog(title, text, kind) {
  return openModal({ title, text, kind: kind || 'bad', alert: true });
}

export function fmtTime(value) {
  if (!value) return '';
  return String(value).replace('T', ' ').slice(0, 16);
}

export function shortTime(value) {
  const text = fmtTime(value);
  return text ? text.slice(5) : '';
}

/* 空数据占位：图标 + 一句结论 + 下一步该做什么（可带按钮） */
export function empty(title, hint, actions, iconName) {
  const node = el('div', { class: 'empty' }, [
    el('div', { class: 'empty-ico' }, [icon(iconName || 'inbox')]),
    el('b', { text: title }),
    hint ? el('span', { text: hint }) : null,
  ]);
  if (actions && actions.length) node.appendChild(el('div', { class: 'empty-actions' }, actions));
  return node;
}

/* 骨架屏：数据第一次回来之前先占住版面，避免"白一下再跳出来" */
export function skeleton(lines) {
  const widths = ['w-90', 'w-70', 'w-50', 'w-70', 'w-30'];
  return el('div', { class: 'skel-block', 'aria-hidden': 'true' },
    Array.from({ length: lines || 3 }, (_, i) => el('span', { class: 'skel ' + widths[i % widths.length] })));
}

export function skeletonRows(count, cols) {
  return Array.from({ length: count || 6 }, () => el('tr', { 'aria-hidden': 'true' }, [
    el('td', { colspan: String(cols || 9) }, [
      el('div', { class: 'skel-row' }, Array.from({ length: 7 }, () => el('span', { class: 'skel' }))),
    ]),
  ]));
}

/* 按钮 loading：期间禁用、转圈，结束后恢复（无论成功失败） */
export async function withLoading(button, task) {
  if (!button || button.classList.contains('is-loading')) return undefined;
  button.classList.add('is-loading');
  button.setAttribute('aria-busy', 'true');
  const wasDisabled = button.disabled;
  button.disabled = true;
  try {
    return await task();
  } finally {
    button.classList.remove('is-loading');
    button.removeAttribute('aria-busy');
    button.disabled = wasDisabled;
  }
}

export function progress(current, total, label) {
  const pct = total ? Math.min(100, Math.round((current / total) * 100)) : 0;
  return el('div', { class: 'progress-line' }, [
    el('span', { text: label || '进行中' }),
    el('div', { class: 'progress-track', role: 'progressbar', 'aria-valuenow': String(pct), 'aria-valuemin': '0', 'aria-valuemax': '100' },
      [el('i', { style: 'width:' + pct + '%' })]),
    el('span', { class: 'muted', text: total ? (current + ' / ' + total) : '' }),
  ]);
}

/* 额度条：pct 是"剩余比例"。剩得越少颜色越危险——风控面板全靠它一眼可读 */
export function meter(pct, tone) {
  return el('div', { class: 'meter ' + (tone ? 't-' + tone : '') }, [
    el('i', { style: 'width:' + Math.max(0, Math.min(100, pct)) + '%' }),
  ]);
}

export function remainTone(pct) {
  if (pct <= 15) return 'bad';
  if (pct <= 40) return 'warn';
  return 'ok';
}
