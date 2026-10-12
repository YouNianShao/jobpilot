/* 岗位池视图（从 app.js 拆出来的第一个视图）。

   以前一次只取 400 条、在浏览器里筛选 / 搜索 / 排序 / 计数：岗位一过 400，
   最新采到、还没评分的岗位就直接看不见，状态分档的数字也只是这 400 条里的。
   现在这些都交给服务端（GET /api/jobs?page=…），这里只负责画一页、管分页和勾选。

   和 app.js 的边界：app.js 调 initJobs(ctx) 把共用能力传进来（打开详情、刷新状态…），
   这个文件不 import app.js，避免循环依赖。 */

import { get, post } from '../api.js';
import { FILTERS, PLATFORM_NAME, STATUS_LABEL, isApplied, platformName } from '../common.js';
import { clear, confirmDialog, el, empty, renderPager as sharedPager, skeletonRows, toast } from '../ui.js';

const PAGE_SIZE = 50;
const COLS = 9;
const $ = (id) => document.getElementById(id);

const pool = {
  status: '',            // 状态筛选（'' = 全部）
  platform: '',          // 平台筛选（'' = 全部平台）
  query: '',
  sort: 'created',       // 默认按采集时间倒序：刚采到的在最上面
  dir: 'desc',
  hideNature: false,     // 只看正常岗（排除外包 / 培训 / 营销话术）
  bin: false,            // 回收站
  page: 1,
  data: null,            // 最近一次接口返回 {items, total, page, pages, limit, counts}
  selected: new Set(),   // 勾选的岗位 id：翻页 / 筛选都保留
  seq: 0,                // 请求序号：用户连续操作时，只认最后一次的结果
  timer: 0,
};

let ctx = null;

export function initJobs(context) {
  ctx = context;
  $('jobQuery').addEventListener('input', (event) => {
    pool.query = event.target.value;
    clearTimeout(pool.timer);
    pool.timer = setTimeout(() => { pool.page = 1; loadJobs(); }, 250);   // 停手 0.25 秒再搜
  });
  $('btnBin').addEventListener('click', () => toggleBin());
  $('btnBadNature').addEventListener('click', () => {
    pool.hideNature = !pool.hideNature;
    $('btnBadNature').classList.toggle('is-on', pool.hideNature);
    $('btnBadNature').setAttribute('aria-pressed', String(pool.hideNature));
    pool.page = 1;
    loadJobs();
  });
  $('jobPickAll').addEventListener('change', (event) => {
    for (const id of visibleIds()) {
      if (event.target.checked) pool.selected.add(id); else pool.selected.delete(id);
    }
    renderRows();
  });
  $('bulkConfirm').addEventListener('click', () => bulkAction('confirm'));
  $('bulkReject').addEventListener('click', () => bulkAction('reject'));
  $('bulkDelete').addEventListener('click', () => bulkAction('delete'));
  $('bulkClear').addEventListener('click', () => { pool.selected.clear(); renderRows(); });
  document.querySelectorAll('.view[data-view="jobs"] th.sortable').forEach((th) => {
    th.addEventListener('click', () => {
      const key = th.dataset.sort;
      if (pool.sort === key) pool.dir = pool.dir === 'asc' ? 'desc' : 'asc';
      else { pool.sort = key; pool.dir = key === 'score' ? 'desc' : 'asc'; }
      pool.page = 1;
      loadJobs();
    });
  });
}

export const isBin = () => pool.bin;

const visibleIds = () => ((pool.data && pool.data.items) || []).map((job) => job.id);

/* 一页数据的指纹：轮询时数据没变就不重画（重画会打断 hover、滚动位置和正在看的那一行） */
function fingerprint(data) {
  return JSON.stringify([data.total, data.page, data.pages, data.counts,
    (data.items || []).map((job) => [job.id, job.status, job.score, job.updated_at])]);
}

export async function loadJobs(options) {
  const quiet = !!(options && options.quiet);   // 轮询刷新：不画骨架、出错不打扰
  const seq = ++pool.seq;
  if (!quiet && !pool.data) {
    clear($('jobs'));
    for (const row of skeletonRows(6, COLS)) $('jobs').appendChild(row);
  }
  const params = new URLSearchParams({ page: String(pool.page), limit: String(PAGE_SIZE), sort: pool.sort, dir: pool.dir });
  if (pool.query.trim()) params.set('q', pool.query.trim());
  if (pool.bin) params.set('deleted', '1');
  else {
    if (pool.status) params.set('status', pool.status);
    if (pool.platform) params.set('platform', pool.platform);
    if (pool.hideNature) params.set('hide_nature', '1');
  }
  // 导出跟着当前筛选走：看到什么就导出什么（状态 / 搜索词 / 只看正常岗）
  const exportLink = $('btnExport');
  if (exportLink) {
    const ex = new URLSearchParams();
    if (pool.status) ex.set('status', pool.status);
    if (pool.platform) ex.set('platform', pool.platform);
    if (pool.query.trim()) ex.set('q', pool.query.trim());
    if (pool.hideNature) ex.set('hide_nature', '1');
    exportLink.href = '/api/export' + (ex.toString() ? '?' + ex.toString() : '');
    exportLink.hidden = pool.bin;            // 回收站里的东西不导出
  }
  const res = await get('/api/jobs?' + params.toString());
  if (seq !== pool.seq) return;                 // 期间又发了新请求：这个结果已经过时
  pool.loadedAt = Date.now();
  if (!res || !Array.isArray(res.items)) {
    if (quiet) return;
    if (!pool.data) {
      clear($('jobs'));
      $('jobs').appendChild(el('tr', {}, [el('td', { colspan: String(COLS) }, [
        empty('岗位池加载失败', (res && res.error) || '稍后会自动重试', null, 'alert'),
      ])]));
    } else {
      toast((res && res.error) || '岗位池加载失败', 'err');
    }
    return;
  }
  const print = fingerprint(res);
  if (quiet && print === pool.print) return;
  pool.print = print;
  pool.data = res;
  pool.page = res.page;
  renderStats();
  renderRows();
  renderPager();
}

/* 视图在岗位池上时，跟着全局轮询每 6 秒看一眼（采集 / 评分任务正在往池子里写） */
export function refreshJobsQuietly() {
  if (Date.now() - (pool.loadedAt || 0) < 6000) return;
  loadJobs({ quiet: true });
}

export function setStatusFilter(value) {
  if (pool.bin) toggleBin(false, true);         // 命令面板要求「筛选待确认」时先退出回收站
  pool.status = value || '';
  pool.page = 1;
  loadJobs();
}

export function toggleBin(force, silent) {
  pool.bin = force === undefined ? !pool.bin : !!force;
  pool.selected.clear();                        // 回收站和岗位池是两套东西，别带着旧选择过去
  pool.status = '';
  pool.platform = '';
  pool.page = 1;
  pool.data = null;
  pool.print = '';
  $('btnBadNature').disabled = pool.bin;
  $('btnBin').textContent = pool.bin ? '返回岗位池' : '回收站';
  $('btnBin').classList.toggle('is-on', pool.bin);
  if (!silent) loadJobs();
}

function renderStats() {
  const box = $('jobStats');
  clear(box);
  const data = pool.data || {};
  $('jobHint').textContent = (pool.bin ? '回收站 · ' : '') + '共 ' + (data.total || 0) + ' 条';
  if (pool.bin) {
    box.appendChild(el('span', { class: 'muted small', text: '回收站里的岗位可以恢复，或彻底删除' }));
    return;
  }
  // 计数来自服务端、覆盖整个池子（只受搜索词影响，不受状态筛选影响）
  const counts = data.counts || {};
  const all = Object.values(counts).reduce((sum, n) => sum + n, 0);
  const chip = (value, label, n) => el('button', {
    class: 'seg-item' + (pool.status === value ? ' is-active' : ''), type: 'button',
    'aria-pressed': pool.status === value ? 'true' : 'false',
    onclick: () => setStatusFilter(value),
  }, [label, el('b', { text: String(n) })]);
  const statusRow = el('div', { class: 'seg-row', role: 'group', 'aria-label': '按状态筛选' }, [el('span', { class: 'seg-label', text: '状态' })]);
  statusRow.appendChild(chip('', '全部', all));
  for (const value of FILTERS.slice(1)) {
    if (!counts[value] && pool.status !== value) continue;
    statusRow.appendChild(chip(value, STATUS_LABEL[value] || value, counts[value] || 0));
  }
  box.appendChild(statusRow);

  // 平台分档：数字受状态 / 搜索影响，不受平台筛选影响（点之前就知道会看到多少）
  const byPlatform = data.platform_counts || {};
  const keys = Object.keys(PLATFORM_NAME).filter((key) => byPlatform[key] || pool.platform === key);
  if (keys.length > 1 || pool.platform) {
    const pick = (value) => { pool.platform = value; pool.page = 1; loadJobs(); };
    const pchip = (value, label, n) => el('button', {
      class: 'seg-item' + (pool.platform === value ? ' is-active' : ''), type: 'button',
      'aria-pressed': pool.platform === value ? 'true' : 'false', onclick: () => pick(value),
    }, [label, el('b', { text: String(n) })]);
    const row = el('div', { class: 'seg-row', role: 'group', 'aria-label': '按平台筛选' }, [el('span', { class: 'seg-label', text: '平台' })]);
    row.appendChild(pchip('', '全部', Object.values(byPlatform).reduce((sum, n) => sum + n, 0)));
    for (const key of keys) row.appendChild(pchip(key, PLATFORM_NAME[key], byPlatform[key] || 0));
    box.appendChild(row);
  }
}

function renderPager() {
  const data = pool.data;
  sharedPager($('jobPager'), data && { page: data.page, pages: data.pages, total: data.total, limit: data.limit }, (page) => {
    pool.page = page;
    loadJobs();
    const card = document.querySelector('.view[data-view="jobs"] .card');
    if (card && card.getBoundingClientRect().top < 0) card.scrollIntoView({ block: 'start', behavior: 'smooth' });
  });
}

function renderRows() {
  const body = $('jobs');
  clear(body);
  const rows = (pool.data && pool.data.items) || [];
  document.querySelectorAll('.view[data-view="jobs"] th.sortable').forEach((th) => {
    const on = th.dataset.sort === pool.sort;
    th.classList.toggle('is-sorted', on);
    th.classList.toggle('asc', on && pool.dir === 'asc');
    th.setAttribute('aria-sort', on ? (pool.dir === 'asc' ? 'ascending' : 'descending') : 'none');
  });
  if (!rows.length) {
    const filtered = !!(pool.query.trim() || pool.status || pool.hideNature);
    body.appendChild(el('tr', {}, [el('td', { colspan: String(COLS) }, [
      pool.bin
        ? empty('回收站是空的', '在岗位池里点「删除」会把岗位移到这里')
        : (filtered
          ? empty('没有符合条件的岗位', '换个搜索词或筛选条件试试', [
            el('button', { class: 'btn small', type: 'button', text: '清除筛选', onclick: clearFilters }),
          ], 'search')
          : empty('岗位池还是空的', '先跑一次采集，岗位会落进这个池子', [
            el('button', { class: 'btn small primary', type: 'button', text: '采集', onclick: () => ctx.openRunDialog('collect') }),
          ])),
    ])]));
    syncBulk();
    return;
  }
  for (const job of rows) body.appendChild(jobRow(job));
  syncBulk();
}

function clearFilters() {
  pool.query = '';
  pool.status = '';
  pool.platform = '';
  pool.hideNature = false;
  pool.page = 1;
  $('jobQuery').value = '';
  $('btnBadNature').classList.remove('is-on');
  $('btnBadNature').setAttribute('aria-pressed', 'false');
  loadJobs();
}

function jobRow(job) {
  const tr = el('tr', { onclick: () => ctx.openJob(job.id) });
  const box = el('input', {
    type: 'checkbox', class: 'pick-box', 'aria-label': '选择 ' + (job.title || ''),
    checked: pool.selected.has(job.id) ? true : null,
    onclick: (event) => {
      event.stopPropagation();          // 勾选不该顺手打开详情
      if (event.target.checked) pool.selected.add(job.id); else pool.selected.delete(job.id);
      syncBulk();
    },
  });
  tr.appendChild(el('td', { class: 'pick' }, [box]));
  const titleCell = el('td', { class: 'title' }, [el('span', { text: job.title || '' })]);
  for (const key of (job.nature || [])) {
    titleCell.appendChild(el('span', {
      class: 'pill ' + (key === 'training' ? 'bad' : 'warn'), style: 'margin-left:6px', text: ctx.natureLabel(key),
    }));
  }
  tr.appendChild(titleCell);
  tr.appendChild(el('td', { text: job.company || '' }));
  tr.appendChild(el('td', {}, [el('span', { class: 'pill', text: platformName(job.platform) })]));
  tr.appendChild(el('td', { class: 'muted', text: job.city || '' }));
  tr.appendChild(el('td', { class: 'muted', text: job.salary || '' }));
  const score = (job.score === null || job.score === undefined) ? null : Number(job.score);
  tr.appendChild(el('td', { class: 'num' }, [
    score === null ? el('span', { class: 'muted', text: '-' })
      : el('span', { class: 'score ' + (score >= 80 ? 'hi' : (score >= 60 ? 'mid' : 'lo')), text: String(score) }),
  ]));
  const cls = job.status === 'pending_confirm' ? 'warn' : (job.status === 'applied' ? 'ok' : '');
  tr.appendChild(el('td', {}, [el('span', { class: 'pill ' + cls, text: STATUS_LABEL[job.status] || job.status || '' })]));
  const action = el('td', {});
  const op = (label, kind, cls2) => el('button', {
    class: 'btn ghost small ' + (cls2 || ''), type: 'button', text: label,
    onclick: (event) => { event.stopPropagation(); binAction(kind, job.id); },
  });
  if (pool.bin) {
    action.append(op('恢复', 'restore'), op('彻底删除', 'purge', 'danger'));
  } else {
    if (job.url && job.greeting) {
      action.appendChild(el('button', {
        class: 'btn ghost small', type: 'button', text: '打开并复制',
        onclick: (event) => { event.stopPropagation(); ctx.openAndCopy(job); },
      }));
    } else if (job.url) {
      action.appendChild(el('a', {
        class: 'btn ghost small', href: job.url, target: '_blank', rel: 'noopener',
        onclick: (event) => event.stopPropagation(), text: '打开 ↗',
      }));
    }
    if (!isApplied(job)) {
      action.appendChild(el('button', {
        class: 'btn ghost small', type: 'button', text: '标记已投',
        onclick: (event) => { event.stopPropagation(); ctx.markApplied(job); },
      }));
    }
    action.appendChild(op('移入回收站', 'delete'));
  }
  tr.appendChild(action);
  return tr;
}

/* 批量选择：勾选跨页、跨筛选保留（Set 存 id），但「全选」只作用于当前这一页 —— 用户按的是眼前这一屏 */
function syncBulk() {
  const count = pool.selected.size;
  $('jobBulk').hidden = count === 0;
  $('bulkCount').textContent = '已选 ' + count + ' 个';
  const visible = visibleIds();
  const picked = visible.filter((id) => pool.selected.has(id)).length;
  $('jobPickAll').checked = visible.length > 0 && picked === visible.length;
  $('jobPickAll').indeterminate = picked > 0 && picked < visible.length;
}

async function bulkAction(kind) {
  const ids = [...pool.selected];
  if (!ids.length) return;
  const label = kind === 'confirm' ? '确认投递' : (kind === 'reject' ? '拒绝' : '移入回收站');
  const path = kind === 'confirm' ? '/api/jobs/confirm' : (kind === 'reject' ? '/api/jobs/reject' : '/api/jobs/delete');
  const res = await post(path, { ids });
  if (res.error) { toast(res.error, 'err'); return; }
  const done = res.confirmed ?? res.rejected ?? res.deleted ?? 0;
  pool.selected.clear();
  await loadJobs();
  await ctx.refresh();
  const skipped = res.skipped || {};
  if (kind === 'delete') {
    undoToast('已移入回收站 ' + done + ' 个', ids);
    return;
  }
  if (!Object.keys(skipped).length) {
    toast(label + ' ' + done + ' 个' + (kind === 'confirm' && done ? '，运行「投递」后才会真的发出' : ''), done ? 'ok' : 'warn');
    return;
  }
  // 只有「待确认」能确认 / 拒绝。以前跳过的只给一句「状态不符合」，看起来像按钮没反应
  const why = skipReasons(skipped);
  if (done) {
    toast(label + ' ' + done + ' 个；另有 ' + why.short, 'warn');
    return;
  }
  const ok = await confirmDialog(label + '：选中的岗位都不能操作', why.long, {
    okText: skipped.new ? '去给它们评分' : '知道了', kind: 'warn',
  });
  if (ok && skipped.new) ctx.runTask('score');
}

/* 状态 → 为什么不能确认。确认门只认「待确认」：新采到的要先评分，过了阈值才进待确认 */
function skipReasons(skipped) {
  const NOTE = {
    new: '还没评分（新采到的岗位要先「AI 评分」，分数过阈值才会进「待确认」）',
    filtered: '评分没过阈值',
    approved: '已经确认过了（在等「投递」任务发出）',
    applied: '已经投递过', already: '之前投过', rejected: '已被拒绝 / 预筛淘汰',
    missing: '已不存在',
  };
  const parts = Object.entries(skipped).map(([key, count]) => count + ' 个' + (NOTE[key] || (STATUS_LABEL[key] || key)));
  return {
    short: parts.join('，') + '，已跳过',
    long: '只有「待确认」的岗位能确认投递。你选中的：\n' + parts.join('\n')
      + '\n\n确认之后岗位进入「已确认」，还要运行一次「投递」才会真的发出招呼。',
  };
}

/* 移入回收站是可恢复的：直接做，提示里给「撤销」。只有「彻底删除」才需要二次确认 */
function undoToast(text, ids) {
  toast(text, 'ok', {
    action: '撤销',
    onAction: async () => {
      await post('/api/jobs/restore', { ids });
      toast('已撤销', 'ok');
      await loadJobs();
      await ctx.refresh();
    },
  });
}

export async function binAction(kind, id) {
  const path = kind === 'restore' ? '/api/jobs/restore' : (kind === 'purge' ? '/api/jobs/purge' : '/api/jobs/delete');
  if (kind === 'purge' && !(await confirmDialog(
    '彻底删除', '彻底删除后无法恢复（连事件记录一起清除）。', { okText: '彻底删除', kind: 'bad' }))) return;
  const res = await post(path, { ids: [id] });
  if (res.error) { toast(res.error, 'err'); return; }
  if (kind === 'delete') undoToast('已移入回收站', [id]);
  else toast(kind === 'restore' ? '已恢复' : '已彻底删除', 'ok');
  pool.selected.delete(id);
  await loadJobs();
  await ctx.refresh();
}
