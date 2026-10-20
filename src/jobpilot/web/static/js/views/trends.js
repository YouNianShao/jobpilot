/* 概览「趋势」卡片（2026-10-20 用户选定改版：一张全宽「堆叠柱状图」+「采集 | 投递」切换）。
   每个时间桶一根柱（「今天」按小时，其余按天），柱子按平台分段堆叠（平台色，顺序固定 boss → 51job → liepin → zhaopin → other）。
   顶上一行 = 指标切换 + 汇总 + 共用图例（图例只显示当前指标的平台合计）。
   图例是按钮：点一个平台只看它（柱子变成单色），再点一次或点「全部平台」回到全部。
   数据是聚合查询，不跟 2 秒心跳同频：概览可见时最多 60 秒拉一次；切换天数、切换账号立刻重拉。
   不用第三方图表库：按容器的真实像素宽度画 SVG（文字不会被拉伸），容器变宽变窄时重画。 */

import { get } from '../api.js';
import { platformName } from '../common.js';
import { clear, el } from '../ui.js';
import { $ } from '../store.js';

const RANGES = [1, 7, 14, 30];   // 1 = 「今天」：后端按小时分桶（unit='hour'）
const STORE_KEY = 'jp.trendDays';
const PLATFORM_KEY = 'jp.trendPlatform';
const METRIC_KEY = 'jp.trendMetric';
const REFRESH_MS = 60000;
const CHART_H = 160;
const BAR_MAX = 48;              // 柱子最宽 48px；否则按「每格宽 × 0.62」
/* 左边放 y 刻度、底下放 x 日期；右边留一点给「平均 N」标签的尾巴 */
const M = { top: 10, right: 12, bottom: 20, left: 34 };
const METRICS = [
  { key: 'collected', label: '采集', day: '每天采集', hour: '每小时采集' },
  { key: 'applied', label: '投递', day: '每天投递', hour: '每小时投递' },
];
const ORDER = ['boss', '51job', 'liepin', 'zhaopin', 'other'];
/* 「今天」视图：每行是一小时（r.hour），不是一天 */
const isHour = () => !!(local.data && local.data.unit === 'hour');
const hh = (r) => String(r.hour).padStart(2, '0');
/* 提示框 / 读屏里那一格叫什么 */
function slotLabel(rows, i) {
  const r = rows[i];
  if (isHour()) return hh(r) + ':00–' + hh(r) + ':59' + (i === rows.length - 1 ? '（现在）' : '');
  return longDate(r.date) + (i === rows.length - 1 ? '（今天）' : '');
}

const local = { data: null, at: 0, account: null, loading: false, again: false, bound: false, plot: null, ro: null, width: '' };

function loadDays() {
  let saved = 14;
  try { saved = Number(localStorage.getItem(STORE_KEY)) || 14; } catch (e) { /* 隐私模式读不到就用默认 */ }
  return RANGES.includes(saved) ? saved : 14;   // 1 = 今天
}
let days = loadDays();

function loadMetric() {
  let saved = null;
  try { saved = localStorage.getItem(METRIC_KEY); } catch (e) { /* 读不到就默认采集 */ }
  return METRICS.some((m) => m.key === saved) ? saved : 'collected';
}
let metric = loadMetric();
const metricOf = () => METRICS.find((m) => m.key === metric) || METRICS[0];

/* 只看哪个平台：null = 全部。存的值要到拿到数据后才能对照 platforms 校验 */
function loadPlatform() {
  try { return localStorage.getItem(PLATFORM_KEY) || null; } catch (e) { return null; }
}
let platform = loadPlatform();
function savePlatform() {
  try {
    if (platform) localStorage.setItem(PLATFORM_KEY, platform);
    else localStorage.removeItem(PLATFORM_KEY);
  } catch (e) { /* 隐私模式存不了就不记 */ }
}

const SHORT = { '': '全部', boss: 'BOSS', '51job': '前程', liepin: '猎聘', zhaopin: '智联', other: '已清理' };
const sum = (obj) => Object.values(obj || {}).reduce((a, b) => a + b, 0);
/* 'other' = 岗位已被清理、只剩投递记录，查不到平台 */
const nameOf = (p) => (p === 'other' ? '已清理岗位' : platformName(p));
const colorOf = (p) => (p === 'other' ? 'var(--muted)' : 'var(--tp-' + p + ', var(--muted))');
const shortDate = (iso) => iso.slice(5);
const longDate = (iso) => Number(iso.slice(5, 7)) + ' 月 ' + Number(iso.slice(8, 10)) + ' 日';
/* 一段时间里每个平台加起来 */
function totals(rows, key) {
  const out = {};
  for (const r of rows) for (const [p, n] of Object.entries(r[key] || {})) out[p] = (out[p] || 0) + n;
  return out;
}
/* 固定顺序：boss, 51job, liepin, zhaopin, other；注册表里还有别的平台就排在 other 前面 */
function ordered(list) {
  const rank = (p) => { const i = ORDER.indexOf(p); return i < 0 ? (p === 'other' ? 99 : 50) : (p === 'other' ? 99 : i); };
  return list.slice().sort((a, b) => rank(a) - rank(b) || String(a).localeCompare(String(b)));
}
/* 当前指标有哪些平台：注册平台常驻；'other' 只在这段时间有数时出现（实际只有投递会有） */
function seriesOf(platforms, tot) {
  const base = platforms.filter((p) => p !== 'other');
  if (platforms.includes('other') && tot.other > 0) base.push('other');
  return ordered(base);
}
/* 现在实际画哪几段（受「只看某平台」影响） */
const visibleOf = (series) => (platform ? [platform] : series);

/* y 轴刻度：整数、好读；最大值很小（如 3）时就 0,1,2,3 */
function niceTicks(max) {
  if (max <= 0) return [0, 1];
  let step = 1;
  if (max > 5) {
    const raw = max / 4;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    step = Math.max(1, Math.round((norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag));
  }
  const top = Math.ceil(max / step) * step;
  const out = [];
  for (let v = 0; v <= top; v += step) out.push(v);
  return out;
}

const SVG = 'http://www.w3.org/2000/svg';
function svgEl(tag, attrs, text) {
  const n = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, String(v));
  if (text !== undefined) n.textContent = text;
  return n;
}
/* 图高跟着卡片走：一屏概览里卡片高度按屏幕高度分（overview.css 给卡片 --fit:1），图吃掉卡片里剩下的高度；
   卡片不定高（窄屏 / 别的布局）时就是 CHART_H */
function chartHeight() {
  const body = $('trendBody');
  const card = body && body.closest('.card');
  if (!card || getComputedStyle(card).getPropertyValue('--fit').trim() !== '1') return CHART_H;
  const head = card.querySelector('.card-head');
  const top = body.querySelector('.tr-top');
  const cs = getComputedStyle(body);
  const pad = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
  const free = card.clientHeight - (head ? head.offsetHeight : 0) - (top ? top.offsetHeight + 10 : 36) - pad - 4;
  return Math.round(Math.min(420, Math.max(110, free)));
}

/* 画堆叠柱状图（按真实像素宽度）。plot = { wrap, tip, live, series, hover, geo, band } */
function drawChart(plot, rows) {
  const W = Math.max(240, Math.floor(plot.wrap.clientWidth || 600));
  const H = chartHeight();
  const n = rows.length;
  const iw = W - M.left - M.right;
  const ih = H - M.top - M.bottom;
  const key = metric;
  const vis = visibleOf(plot.series);
  // 每格每个平台的数（按固定堆叠顺序），以及每格合计
  const cells = rows.map((r) => vis.map((p) => ({ p, v: (r[key] || {})[p] || 0 })));
  const stacks = cells.map((c) => c.reduce((a, s) => a + s.v, 0));
  const total = stacks.reduce((a, b) => a + b, 0);
  const max = Math.max(0, ...stacks);
  const ticks = niceTicks(max);
  const top = ticks[ticks.length - 1];
  const cw = iw / Math.max(1, n);                       // 每格宽
  const bw = Math.max(2, Math.min(BAR_MAX, cw * 0.62)); // 柱宽
  const cx = (i) => M.left + cw * (i + 0.5);
  const y = (v) => M.top + ih * (1 - v / top);
  plot.geo = { n, cw, W, H, cells, stacks };
  const mt = metricOf();
  const title = isHour() ? mt.hour : mt.day;

  const svg = svgEl('svg', {
    width: W, height: H, style: 'width:' + W + 'px;height:' + H + 'px', viewBox: '0 0 ' + W + ' ' + H, class: 'tr-svg', role: 'img',
    'aria-label': title + '堆叠柱状图，' + (platform ? '只看' + nameOf(platform) + '，' : '按平台分段，')
      + (isHour() ? '今天每小时（0 点到现在）共 ' : '近 ' + n + ' 天共 ') + total + ' 个',
  });
  // 悬停时的整列淡色背景带：放最底下，不挡柱子
  const band = svgEl('rect', { class: 'tr-band', y: M.top, height: ih, width: cw, x: M.left, rx: 3, visibility: 'hidden', 'aria-hidden': 'true' });
  svg.appendChild(band);
  plot.band = band;
  // 横向网格线 + y 刻度
  const grid = svgEl('g', { class: 'tr-grid', 'aria-hidden': 'true' });
  for (const t of ticks) {
    grid.appendChild(svgEl('line', { x1: M.left, x2: W - M.right, y1: y(t), y2: y(t), class: t === 0 ? 'tr-base' : '' }));
    grid.appendChild(svgEl('text', { x: M.left - 6, y: y(t), class: 'tr-ytick', 'text-anchor': 'end', 'dominant-baseline': 'middle' }, String(t)));
  }
  svg.appendChild(grid);
  // x 轴：以最后一格（今天 / 现在）为锚往前每隔 step 个标一个，30 天、24 小时都不挤
  const step = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(iw / 52))));
  const xs = svgEl('g', { class: 'tr-xaxis', 'aria-hidden': 'true' });
  rows.forEach((r, i) => {
    if ((n - 1 - i) % step !== 0) return;
    const last = i === n - 1;
    xs.appendChild(svgEl('text', { x: cx(i), y: H - 5, 'text-anchor': 'middle', class: last ? 'tr-xtick is-today' : 'tr-xtick' },
      isHour() ? (last ? '现在' : hh(r) + ' 时') : (last ? '今天' : shortDate(r.date))));
  });
  svg.appendChild(xs);

  if (total === 0) {
    svg.appendChild(svgEl('text', { x: M.left + iw / 2, y: M.top + ih / 2, 'text-anchor': 'middle', 'dominant-baseline': 'middle', class: 'tr-empty' },
      (isHour() ? '今天还没有' : '这段时间还没有') + mt.label + '数据'));
  } else {
    // 柱子：每格一个 <g>，按固定顺序自下往上堆；整根柱子用一个圆角 clipPath 裁出圆顶（底部伸到基线下，保持方角）
    const defs = svgEl('defs');
    const g = svgEl('g', { class: 'tr-bars', 'aria-hidden': 'true' });
    const base = y(0);
    cells.forEach((c, i) => {
      const x0 = cx(i) - bw / 2;
      if (stacks[i] === 0) {   // 0 的那一格只留一截 2px 的淡色短桩，表示「这格有，但是 0」
        g.appendChild(svgEl('rect', { class: 'tr-stub', x: x0, y: base - 2, width: bw, height: 2 }));
        return;
      }
      const id = 'trc-' + i;
      const r = Math.min(3, bw / 2);
      defs.appendChild(svgEl('clipPath', { id }, undefined)).appendChild(
        svgEl('rect', { x: x0, y: y(stacks[i]), width: bw, height: base - y(stacks[i]) + r, rx: r }));
      const col = svgEl('g', { 'clip-path': 'url(#' + id + ')', class: 'tr-col' });
      let acc = 0;
      for (const s of c) {
        if (!s.v) continue;
        const y1 = y(acc + s.v);
        const y0 = y(acc);
        acc += s.v;
        col.appendChild(svgEl('rect', {
          class: 'tr-seg' + (s.p === 'other' ? ' is-other' : ''), 'data-p': s.p,
          x: x0, y: y1, width: bw, height: Math.max(0.5, y0 - y1), style: 'fill:' + colorOf(s.p),
        }));
      }
      g.appendChild(col);
    });
    svg.appendChild(defs);
    svg.appendChild(g);
    // 平均线：这段时间每格平均多少（只有一格时没意义，不画）
    if (n > 1) {
      const avg = total / n;
      const ya = y(avg);
      const txt = avg >= 10 ? String(Math.round(avg)) : String(Math.round(avg * 10) / 10);
      const ag = svgEl('g', { class: 'tr-avg', 'aria-hidden': 'true' });
      ag.appendChild(svgEl('line', { x1: M.left, x2: W - M.right, y1: ya, y2: ya }));
      ag.appendChild(svgEl('text', { x: W - M.right, y: ya - 4, 'text-anchor': 'end' }, '平均 ' + txt));
      svg.appendChild(ag);
    }
  }

  const old = plot.wrap.querySelector('svg');
  if (old) old.replaceWith(svg); else plot.wrap.prepend(svg);
  plot.wrap.classList.toggle('is-empty', total === 0);
  showHover(plot, rows, total === 0 ? null : plot.hover);
}
/* 那一格的文字：每个可见平台一行（自上往下 = 柱子从上往下的顺序），多于一个平台时加「合计」 */
function cellLines(plot, i) {
  const c = plot.geo.cells[i] || [];
  const out = c.map((s) => ({ p: s.p, name: nameOf(s.p), v: s.v })).reverse();
  return { items: out, total: c.length > 1 ? plot.geo.stacks[i] : null };
}

/* 显示 / 隐藏第 i 格的背景带和提示框；i = null 隐藏 */
function showHover(plot, rows, i) {
  plot.hover = i;
  const tip = plot.tip;
  if (i === null || i === undefined || !plot.geo || !plot.band || plot.wrap.classList.contains('is-empty')) {
    plot.hover = null;
    if (plot.band) plot.band.setAttribute('visibility', 'hidden');
    tip.hidden = true;
    return;
  }
  const { cw, W } = plot.geo;
  const x0 = M.left + cw * i;
  plot.band.setAttribute('x', x0.toFixed(1));
  plot.band.setAttribute('width', cw.toFixed(1));
  plot.band.setAttribute('visibility', 'visible');

  const { items, total } = cellLines(plot, i);
  clear(tip);
  tip.appendChild(el('div', { class: 'tr-tip-date', text: slotLabel(rows, i) + ' · ' + metricOf().label }));
  for (const it of items) {
    tip.appendChild(el('div', { class: 'tr-tip-row' }, [
      el('i', { class: 'tr-sw' + (it.p === 'other' ? ' is-other' : ''), style: 'background:' + colorOf(it.p) }),
      el('span', { text: it.name }), el('b', { text: String(it.v) }),
    ]));
  }
  if (total !== null) tip.appendChild(el('div', { class: 'tr-tip-row is-total' }, [el('span', { text: '合计' }), el('b', { text: String(total) })]));
  tip.hidden = false;
  // 默认在这一列右边；靠右边时翻到左边，始终留在图里
  const tw = tip.offsetWidth;
  let left = x0 + cw + 6;
  if (left + tw > W - 4) left = x0 - 6 - tw;
  tip.style.left = Math.max(4, left) + 'px';
  tip.style.top = M.top + 'px';
}

/* 读屏用：键盘移动时念出那一格的数 */
function announce(plot, rows, i) {
  const { items, total } = cellLines(plot, i);
  plot.live.textContent = metricOf().label + '，' + slotLabel(rows, i) + '：' + items.map((o) => o.name + ' ' + o.v).join('，')
    + (total !== null ? '，合计 ' + total : '');
}

function bindPointer(plot) {
  const rowsNow = () => (local.data && local.data.days) || [];
  const idxAt = (clientX) => {
    const { n, cw } = plot.geo;
    const px = clientX - plot.wrap.getBoundingClientRect().left;
    return Math.min(n - 1, Math.max(0, Math.floor((px - M.left) / cw)));
  };
  plot.wrap.addEventListener('pointermove', (ev) => {
    if (!plot.geo) return;
    const i = idxAt(ev.clientX);
    if (i !== plot.hover) showHover(plot, rowsNow(), i);
  });
  plot.wrap.addEventListener('pointerleave', () => showHover(plot, rowsNow(), null));
  plot.wrap.addEventListener('blur', () => showHover(plot, rowsNow(), null));
  plot.wrap.addEventListener('keydown', (ev) => {
    const rows = rowsNow();
    if (!plot.geo || !rows.length) return;
    if (ev.key === 'Escape') { showHover(plot, rows, null); return; }
    if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight') return;
    ev.preventDefault();
    const n = rows.length;
    let i = plot.hover;
    if (i === null || i === undefined) i = n - 1;
    else i = Math.min(n - 1, Math.max(0, i + (ev.key === 'ArrowLeft' ? -1 : 1)));
    showHover(plot, rows, i);
    if (plot.hover !== null) announce(plot, rows, i);
  });
}
/* 顶行汇总：跟着「只看某平台」走；两种指标的数都给（当前指标在前） */
function summaryText(rows, tc, ta) {
  const pick = (tot) => (platform ? tot[platform] || 0 : sum(tot));
  const last = rows[rows.length - 1];
  const lastOf = (k) => (platform ? (last[k] || {})[platform] || 0 : sum(last[k]));
  const prefix = platform ? '只看' + nameOf(platform) + ' · ' : '';
  const parts = metric === 'applied'
    ? ['投递 ' + pick(ta), '采集 ' + pick(tc)]
    : ['采集 ' + pick(tc), '投递 ' + pick(ta)];
  if (isHour()) {
    const t = prefix + '今天' + parts.join(' · ');
    return { text: t, full: t };
  }
  const head = prefix + '近 ' + rows.length + ' 天' + parts.join(' · ');
  return { text: head, full: head + ' · 今天' + metricOf().label + ' ' + lastOf(metric) };
}

/* 「采集 | 投递」切换：两个按钮（aria-pressed），记在 localStorage */
function metricToggle() {
  return el('div', { class: 'tr-metric', role: 'group', 'aria-label': '指标' }, METRICS.map((m) => {
    const on = m.key === metric;
    const btn = el('button', {
      type: 'button', class: 'tr-metric-btn' + (on ? ' is-on' : ''), 'aria-pressed': on ? 'true' : 'false',
      'data-metric': m.key, title: '看每' + (isHour() ? '小时' : '天') + m.label + '多少', text: m.label,
    });
    btn.addEventListener('click', () => {
      if (metric === m.key) return;
      metric = m.key;
      try { localStorage.setItem(METRIC_KEY, metric); } catch (e) { /* 隐私模式存不了就不记 */ }
      render({ metric: m.key });
    });
    return btn;
  }));
}

/* 共用图例：「全部平台」+ 每个平台（名字 + 这段时间当前指标的合计），都是按钮；选中一个时其他的变淡 */
function legend(series, tot) {
  const label = metricOf().label;
  const chip = (p, name, count, tip) => {
    const on = p === null ? !platform : platform === p;
    const btn = el('button', {
      type: 'button', class: 'tr-chip' + (on ? ' is-on' : '') + (platform && !on ? ' is-dim' : ''),
      'aria-pressed': on ? 'true' : 'false', 'data-p': p === null ? '' : p,
      title: p === null ? '显示全部平台' : tip + (on ? '（再点一次回到全部平台）' : '（点一下只看' + name + '）'),
      'aria-label': p === null ? '全部平台' : name + '，' + tip,
    }, [
      p === null ? null : el('i', { class: 'tr-sw' + (p === 'other' ? ' is-other' : ''), style: 'background:' + colorOf(p) }),
      // 卡片窄时（CSS @container）换成短名，免得一行放不下
      el('span', { class: 'tr-name', 'data-short': SHORT[p === null ? '' : p] || name, text: name }),
      count === null ? null : el('b', { text: count }),
    ]);
    btn.addEventListener('click', () => {
      platform = p === null || platform === p ? null : p;
      savePlatform();
      render({ p: p === null ? '' : p });
    });
    return btn;
  };
  return el('div', { class: 'tr-legend', role: 'group', 'aria-label': '按平台筛选' },
    [chip(null, '全部平台', null, '')].concat(series.map((p) => {
      const n = tot[p] || 0;
      if (p === 'other') return chip(p, '已清理', String(n), '已清理岗位：' + label + ' ' + n + '（岗位已删，查不到平台）');
      return chip(p, nameOf(p), String(n), label + ' ' + n);
    })));
}

function redraw() {
  const rows = local.data && local.data.days;
  if (!rows || !rows.length || !local.plot || !local.plot.wrap.isConnected) return;
  drawChart(local.plot, rows);
}

/* 容器宽度变了就按新宽度重画（文字保持原大小，不拉伸） */
function watchSize(body) {
  if (local.ro || typeof ResizeObserver === 'undefined') return;
  let frame = 0;
  local.ro = new ResizeObserver(() => {
    const w = body.clientWidth + 'x' + chartHeight();
    if (w === local.width) return;
    local.width = w;
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(redraw);
  });
  local.ro.observe(body);
  // 卡片高度按屏幕高度分：只改窗口高度时宽度不变，ResizeObserver 不会报，单独听一下
  window.addEventListener('resize', () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(() => { local.width = ''; redraw(); }); });
}

function render(focus) {
  const body = $('trendBody');
  if (!body || !local.data) return;
  const { days: rows, platforms = [] } = local.data;
  const prevHover = local.plot ? local.plot.hover : null;   // 60 秒刷新 / 切指标时，悬停那一格保持住
  clear(body);
  local.plot = null;
  if (!rows || !rows.length) return;
  const tc = totals(rows, 'collected');
  const ta = totals(rows, 'applied');
  const tot = metric === 'applied' ? ta : tc;
  const series = seriesOf(platforms, tot);
  // 存的平台在当前指标里没有（换账号 / 切到采集时的 'other'）就回到全部
  if (platform && !series.includes(platform)) {
    platform = null;
    savePlatform();
  }
  const sm = summaryText(rows, tc, ta);
  body.appendChild(el('div', { class: 'tr-top' }, [
    metricToggle(),
    el('p', { class: 'tr-summary', text: sm.text, title: sm.full }),
    legend(series, tot),
  ]));
  const mt = metricOf();
  const title = isHour() ? mt.hour : mt.day;
  const plot = { series, hover: prevHover, geo: null, band: null };
  plot.tip = el('div', { class: 'tr-tip', 'aria-hidden': 'true' });
  plot.tip.hidden = true;
  plot.live = el('span', { class: 'tr-sr', 'aria-live': 'polite' });
  plot.wrap = el('div', { class: 'tr-plot', tabindex: '0', title: '左右方向键逐' + (isHour() ? '小时' : '天') + '查看，Esc 收起' }, [plot.tip, plot.live]);
  body.appendChild(el('section', { class: 'tr-chart', 'aria-label': title }, [plot.wrap]));
  local.plot = plot;
  bindPointer(plot);
  // 卡片换了数据后（切了天数）旧的悬停格号可能越界
  if (plot.hover !== null && plot.hover >= rows.length) plot.hover = null;
  drawChart(plot, rows);
  local.width = body.clientWidth + 'x' + chartHeight();
  watchSize(body);
  if (focus) {
    const sel = focus.metric ? '.tr-metric-btn[data-metric="' + focus.metric + '"]' : '.tr-chip[data-p="' + focus.p + '"]';
    const btn = body.querySelector(sel);
    if (btn) btn.focus();
  }
}
function renderRange() {
  const box = $('trendRange');
  if (!box) return;
  for (const btn of box.querySelectorAll('[data-days]')) {
    const on = Number(btn.dataset.days) === days;
    btn.classList.toggle('is-active', on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  }
}

async function load() {
  if (local.loading) { local.again = true; return; }
  local.loading = true;
  local.again = false;
  const asked = days;
  try {
    const data = await get('/api/trends?days=' + asked);
    if (asked === days && data && !data.error && Array.isArray(data.days)) {
      if (!local.data || local.data.unit !== data.unit || (local.data.days || []).length !== data.days.length) {
        if (local.plot) local.plot.hover = null;
      }
      local.data = data;
      render();
    }
  } finally {
    local.at = Date.now();
    local.loading = false;
  }
  // 请求途中切了时间范围：这次拿到的是旧范围，按新范围再拉一次
  if (local.again || asked !== days) load();
}

/* 概览每次心跳都会调这里，但真正发请求最多 60 秒一次；换了账号立刻重拉（数据按账号分开） */
export function refreshTrends(status) {
  if (!local.bound) {
    local.bound = true;
    const box = $('trendRange');
    if (box) {
      box.addEventListener('click', (ev) => {
        const btn = ev.target.closest('[data-days]');
        if (!btn) return;
        days = Number(btn.dataset.days);
        try { localStorage.setItem(STORE_KEY, String(days)); } catch (e) { /* 隐私模式存不了就不记 */ }
        renderRange();
        load();
      });
    }
    renderRange();
  }
  const account = status && status.account ? status.account.id : null;
  if (account !== local.account || Date.now() - local.at > REFRESH_MS) {
    local.account = account;
    load();
  }
}
