import { get, post } from './api.js';
import {
  alertDialog, clear, confirmDialog, el, empty, fmtTime, icon, meter, progress, remainTone, renderPager,
  skeleton, toast, withLoading,
} from './ui.js';
import { STATUS_LABEL, isApplied, modeName, platformName } from './common.js';
import { $, state } from './store.js';
import { binAction, initJobs, isBin, loadJobs, refreshJobsQuietly, setStatusFilter, toggleBin } from './views/jobs.js';

const VIEW_TITLES = { overview: '概览', jobs: '岗位池', monitor: '监测', activity: '记录', config: '配置' };
/* 岗位时间线里的事件名：数据库里是英文代号，界面上一律中文 */
const EVENT_NAME = {
  collected: '采集到', scored: '评分', prefilter_rejected: '预筛淘汰', greeting: '生成招呼语',
  confirmed: '你确认了投递', confirm_rejected: '你拒绝了投递', apply: '投递', applied: '已投递',
  probe_apply: '投递预演', resume: '定制简历', follow_up: '跟进话术', interview_prep: '面试准备',
  progress: '进展', cross_platform_dup: '其它平台有同一岗位', risk: '风控信号', page_error: '页面异常',
};
const STAGE_LABEL = {
  collect_search: '搜索页', collect_detail: '详情页', apply: '投递', monitor: '监测',
};
const CAP_LABEL = { collect: '采集', apply: '投递', monitor: '监测' };
const STAGES = ['collect_search', 'collect_detail', 'apply', 'monitor'];


const capsOf = (platform) => (((state.status && state.status.platforms) || {})[platform] || {}).capabilities || [];
const natureLabel = (key) => ((state.status && state.status.nature_labels) || {})[key] || key;
const poolTotal = (counts) => (counts.new || 0) + (counts.pending_confirm || 0)
  + (counts.approved || 0) + (counts.applied || 0);

/* ---------------- 顶部/侧栏 ---------------- */
function setTheme(theme) {
  state.theme = theme;
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('jp-theme', theme);
  $('btnTheme').innerHTML = '';
  $('btnTheme').appendChild(icon(theme === 'dark' ? 'sun' : 'moon'));
}

function switchView(name) {
  state.view = name;
  document.querySelectorAll('.nav-item').forEach((b) => b.classList.toggle('is-active', b.dataset.view === name));
  document.querySelectorAll('.view').forEach((v) => v.classList.toggle('is-active', v.dataset.view === name));
  $('crumb').textContent = VIEW_TITLES[name] || name;
  if (name === 'jobs') loadJobs();
  if (name === 'monitor') { loadFollowUps(); loadInteractions(); }
  if (name === 'activity') loadActivity();
  if (name === 'config') loadConfig();
}

/* ---------------- 概览：引导 ---------------- */
function renderSteps(status) {
  const box = $('steps');
  clear(box);
  const counts = status.counts || {};
  const total = poolTotal(counts);
  const pending = counts.pending_confirm || 0;

  if (!total) {
    const steps = [
      ['采集岗位', '从已启用的平台抓一轮岗位', '采集', () => runTask('collect'), false],
      ['AI 评分', '按简历和阈值挑出值得投的', '评分', () => runTask('score'), true],
      ['确认投递', '你点确认之后才真的打招呼', '运行全流程', () => runTask('full'), true],
    ];
    steps.forEach(([title, sub, btn, fn, disabled], index) => {
      box.appendChild(el('div', { class: 'step' + (index === 0 ? ' is-now' : '') }, [
        el('div', { class: 'step-i', text: String(index + 1) }),
        el('div', { class: 'step-body' }, [el('b', { text: title }), el('span', { text: sub })]),
        el('button', { class: 'btn small' + (index === 0 ? ' primary' : ''), text: btn, disabled, onclick: fn }),
      ]));
    });
    return;
  }

  // 岗位池里有东西之后：告诉用户「下一步该点什么」，而不是让人自己去猜流程
  const fresh = counts.new || 0;
  const approved = counts.approved || 0;
  const running = !!status.running;
  const next = (iconName, title, sub, label, fn) => box.appendChild(el('div', { class: 'step is-now' }, [
    el('div', { class: 'step-i' }, [icon(iconName)]),
    el('div', { class: 'step-body' }, [el('b', { text: title }), el('span', { text: sub })]),
    el('button', { class: 'btn small primary', text: label, disabled: running, onclick: fn }),
  ]));
  if (fresh) {
    next('spark', fresh + ' 个新采到的岗位还没评分', '评分过阈值的会进「待确认」；没过的沉到池底，不会被投递',
      '评分', () => runTask('score'));
  }
  if (pending) {
    next('inbox', '有 ' + pending + ' 个岗位在等你确认', '确认之后才会真的投递，这是最后一道人工闸门', '去确认',
      () => $('pending').scrollIntoView({ behavior: 'smooth', block: 'center' }));
  }
  if (approved) {
    next('send', approved + ' 个已确认的岗位在等投递', '投递会在平台上发起沟通并发出招呼语', '投递', () => runTask('apply'));
  }
}

/* ---------------- 概览：KPI ---------------- */
function renderKpis(status) {
  const counts = status.counts || {};
  const applied = status.applied_today ?? 0;
  const limit = status.daily_limit ?? 30;
  const usedPct = limit ? Math.min(100, (applied / limit) * 100) : 0;

  const cards = [
    {
      icon: 'send', label: '今日投递', value: applied, unit: '/ ' + limit,
      tone: applied >= limit ? 'is-warn' : 'is-accent',
      pct: usedPct, meterTone: usedPct >= 85 ? 'bad' : (usedPct >= 60 ? 'warn' : 'ok'),
      sub: applied >= limit ? '已达风控硬上限，今天不会再投' : ('还能投 ' + (limit - applied) + ' 个'),
    },
    {
      icon: 'inbox', label: '待确认', value: counts.pending_confirm ?? 0, unit: '',
      tone: (counts.pending_confirm ? 'is-warn' : ''),
      sub: counts.pending_confirm ? '需要你点一下才会投递' : '没有堆积',
    },
    {
      icon: 'check', label: '已确认', value: counts.approved ?? 0, unit: '',
      tone: counts.approved ? 'is-ok' : '',
      sub: '排队等待投递',
    },
    {
      icon: 'stack', label: '岗位池', value: poolTotal(counts), unit: '',
      sub: (counts.applied ?? 0) + ' 个已投递',
    },
  ];

  const box = $('kpis');
  clear(box);
  for (const card of cards) {
    box.appendChild(el('div', { class: 'kpi ' + (card.tone || '') }, [
      el('div', { class: 'kpi-top' }, [icon(card.icon), card.label]),
      el('div', { class: 'v' }, [String(card.value), card.unit ? el('span', { class: 'unit', text: card.unit }) : null]),
      card.pct === undefined ? null : meter(card.pct, card.meterTone),
      el('div', { class: 's', text: card.sub }),
    ]));
  }
}
/* ---------------- 概览：风控仪表 ---------------- */
/* ---------------- 概览：风控仪表 ----------------
   为什么重做：以前每个平台摊开 6 条进度条 + 页脚 + 锁提示，4 个平台就是一面绿条墙，
   最该一眼看清的「现在能不能干活、还差多少到上限」反而埋在最里面。
   现在每个平台先给一行结论 + 三格关键数字，明细折叠在后面（点平台名展开）。 */
function renderRisk(status) {
  const box = $('risk');
  clear(box);
  const platforms = status.platforms || {};
  const locks = status.locks || {};
  const keys = Object.keys(platforms);
  const safety = state.config && state.config.safety ? state.config.safety : null;
  const windowOpen = status.active_window_open !== false;
  $('riskHint').textContent = windowOpen
    ? '点平台名展开明细'
    : '不在活跃时段 ' + ((status.active_windows || []).join('、') || '') + '：不会开页面';
  if (!keys.length) { box.appendChild(empty('没有可用平台', '检查 config.yaml 的 platforms 配置')); return; }

  if (!state.riskOpen) state.riskOpen = {};
  for (const key of keys) {
    const info = platforms[key];
    const remain = info.remaining || {};
    const limits = info.limits || (safety ? safety.daily_pages : null) || {};
    const nextIn = info.next_open_in || 0;
    state.nextOpen[key] = { base: nextIn, at: Date.now() };
    const expanded = state.riskOpen[key] === true;
    const lock = locks[key];

    // 最紧的一项：所有额度里剩余比例最小的那个 —— 它就是最先卡住你的闸门
    let tight = null;
    for (const stage of STAGES) {
      const value = remain[stage];
      const limit = limits[stage];
      if (value === undefined || value === null || value < 0 || !limit) continue;
      const pct = (value / limit) * 100;
      if (!tight || pct < tight.pct) tight = { stage, value, limit, pct };
    }
    const tone = lock ? 'bad' : (info.penalty ? 'warn' : (tight && tight.pct <= 15 ? 'warn' : 'ok'));
    const hour = (info.rate && info.rate.per_hour) || null;

    const item = el('div', { class: 'psafe' + (expanded ? ' is-open' : '') });
    item.appendChild(el('button', {
      class: 'psafe-head', type: 'button',
      onclick: () => {
        state.riskOpen[key] = !state.riskOpen[key];
        item.classList.toggle('is-open', state.riskOpen[key]);
      },
    }, [
      el('span', { class: 'status-dot t-' + tone }),
      el('b', { text: info.name || key }),
      el('span', { class: 'pill', text: (info.capabilities || []).map((c) => CAP_LABEL[c] || c).join(' · ') || '—' }),
      el('span', {
        class: 'psafe-next' + (nextIn > 0 ? '' : ' is-live'),
        'data-next-open': key,
        text: nextIn > 0 ? ('最快 ' + Math.ceil(nextIn) + ' 秒后开页') : '现在就能开页',
      }),
      icon('chev', 'chev'),
    ]));

    item.appendChild(el('div', { class: 'psafe-sum' }, [
      el('div', { class: 'kv' }, [el('span', { text: '今日可投' }), el('b', { text: String(remain.daily_limit ?? '—') })]),
      el('div', { class: 'kv' }, [
        el('span', { text: tight ? STAGE_LABEL[tight.stage] : '额度' }),
        el('b', {
          class: tight && tight.pct <= 15 ? 'bad' : '',
          text: tight ? (tight.value + ' / ' + tight.limit) : '—',
        }),
      ]),
      el('div', { class: 'kv' }, [
        el('span', { text: '近 1 小时' }),
        el('b', { text: hour ? ((hour.used || 0) + ' / ' + (hour.limit || '—')) : '不限' }),
      ]),
    ]));

    const detail = el('div', { class: 'psafe-body' });
    const stageRows = el('div', { class: 'mrows' });
    for (const stage of STAGES) {
      const value = remain[stage];
      const limit = limits[stage];
      const unlimited = value === undefined || value < 0;
      const pct = (!unlimited && limit) ? (value / limit) * 100 : 100;
      stageRows.appendChild(el('div', { class: 'mrow' }, [
        el('span', { text: STAGE_LABEL[stage] }),
        unlimited ? el('div', { class: 'meter' }) : meter(pct, remainTone(pct)),
        el('b', { text: unlimited ? '不限' : (value + (limit ? ' / ' + limit : '')) }),
      ]));
    }
    detail.appendChild(stageRows);

    const rateRows = el('div', { class: 'mrows rate' });
    for (const [rk, label] of [['per_10min', '近10分钟'], ['per_hour', '近1小时']]) {
      const row = (info.rate || {})[rk];
      if (!row) continue;
      const limit = Number(row.limit) || 0;
      const unlimited = limit <= 0;
      const pct = unlimited ? 100 : (row.remaining / limit) * 100;
      rateRows.appendChild(el('div', { class: 'mrow' }, [
        el('span', { text: label }),
        unlimited ? el('div', { class: 'meter' }) : meter(pct, remainTone(pct)),
        el('b', { text: unlimited ? '不限' : (row.used + ' / ' + limit) }),
      ]));
    }
    if (rateRows.childElementCount) detail.appendChild(rateRows);

    const fuse = safety ? safety.max_consecutive_page_failures : null;
    if (fuse !== null && fuse !== undefined) {   // 「今日可投」上面摘要里已经有了，这里不再重复
      detail.appendChild(el('div', { class: 'psafe-foot' }, [el('span', { text: '页面连续失败 ' + fuse + ' 次会暂停该平台' })]));
    }
    item.appendChild(detail);

    if (info.penalty) {
      item.appendChild(el('div', { class: 'lock warn' }, [
        icon('alert'),
        '命中过风控，已降速 ×' + info.penalty.factor + '（还有 ' + info.penalty.remaining_minutes + ' 分钟）',
      ]));
    }
    if (lock) {
      item.appendChild(el('div', { class: 'lock' }, [
        icon('alert'), '冷却中：' + (lock.kind || '风控') + '，约 ' + (lock.remaining_minutes ?? '-') + ' 分钟',
      ]));
    }
    box.appendChild(item);
  }
}



/* ---------------- 概览：待确认 ---------------- */
function renderPending(status) {
  const list = status.pending_confirm || [];
  const total = (status.counts && status.counts.pending_confirm) || list.length;
  $('pendingCount').textContent = total;
  $('navJobsBadge').textContent = total ? String(total) : '';
  $('btnConfirmAll').disabled = !total;
  $('btnRejectAll').disabled = !total;
  const box = $('pending');
  clear(box);
  if (!list.length) {
    $('pendingPager').hidden = true;
    box.appendChild(empty('暂无待确认岗位', '先跑「采集 → 评分」，通过阈值的岗位会出现在这里等你确认',
      [el('button', { class: 'btn small', text: '运行全流程', onclick: () => runTask('full') })]));
    return;
  }
  // 一页 6 条：待确认一多，整张卡片不至于拉到两千多像素
  const size = 6;
  const pages = Math.max(1, Math.ceil(list.length / size));
  state.pendingPage = Math.min(Math.max(1, state.pendingPage || 1), pages);
  const shown = list.slice((state.pendingPage - 1) * size, state.pendingPage * size);
  renderPager($('pendingPager'), { page: state.pendingPage, pages, total: list.length, limit: size }, (n) => {
    state.pendingPage = n;
    renderPending(state.status);
  });
  if (total > list.length) {
    box.appendChild(el('div', { class: 'hint' }, [
      '这里列出最新的 ' + list.length + ' 个，共 ' + total + ' 个待确认 · ',
      el('button', { class: 'link', type: 'button', text: '去岗位池看全部', onclick: () => { switchView('jobs'); setStatusFilter('pending_confirm'); } }),
    ]));
  }
  for (const job of shown) box.appendChild(pendingItem(job));
}

/* 待确认的一条，三行：
   1. 岗位名（粗）+ 右边分数
   2. 公司 · 薪资 · 平台（灰，一行写完）
   3. 招呼语（两行截断，没写就提示去生成）
   操作放在右侧一列：确认 / 拒绝是主操作，其余收在一行小按钮里 */
function pendingItem(job) {
  const score = job.score ?? null;
  const tone = score === null ? 'lo' : (score >= 80 ? 'hi' : (score >= 60 ? 'mid' : 'lo'));
  const main = el('div', { class: 'pi-main' }, [
    el('div', { class: 'pi-head' }, [
      el('button', { class: 'pi-title', type: 'button', title: '查看详情', text: job.title || '（无标题）', onclick: () => openJob(job.id) }),
      el('span', { class: 'score ' + tone, text: score === null ? '—' : score + ' 分' }),
    ]),
    el('div', { class: 'pi-meta' }, [
      el('span', { class: 'pi-company', title: job.company || '', text: job.company || '未知公司' }),
      job.salary ? el('span', { text: job.salary }) : null,
      el('span', { text: platformName(job.platform) }),
    ]),
    job.greeting
      ? el('div', { class: 'pi-greet', title: job.greeting, text: job.greeting })
      : el('div', { class: 'pi-greet is-empty', text: '还没有招呼语 · 点顶栏「招呼语」生成' }),
  ]);
  const more = el('div', { class: 'pi-more' });
  if (job.greeting) {
    more.appendChild(el('button', {
      class: 'link', type: 'button', text: '复制招呼语',
      onclick: async () => { await navigator.clipboard.writeText(job.greeting); toast('招呼语已复制', 'ok'); },
    }));
  }
  more.appendChild(el('button', { class: 'link', type: 'button', text: '详情', onclick: () => openJob(job.id) }));
  if (job.url) more.appendChild(el('a', { class: 'link', href: job.url, target: '_blank', rel: 'noopener', text: '原网页 ↗' }));
  const side = el('div', { class: 'pi-side' }, [
    el('div', { class: 'pi-ops' }, [
      el('button', { class: 'btn primary small', type: 'button', text: '确认投递', onclick: () => decide('confirm', [job.id]) }),
      el('button', { class: 'btn small', type: 'button', text: '拒绝', onclick: () => decide('reject', [job.id]) }),
    ]),
    more,
  ]);
  return el('div', { class: 'pending-item' }, [main, side]);
}

/* ---------------- 概览：最近任务 ---------------- */
const fmtDur = (sec) => (sec < 60 ? sec + ' 秒' : Math.floor(sec / 60) + ' 分' + (sec % 60 ? ' ' + (sec % 60) + ' 秒' : ''));


/* ---------------- 概览：任务 ---------------- */
function renderTask(status) {
  const task = status.task;
  const running = !!status.running;
  $('btnStop').disabled = !running;
  $('btnPause').disabled = !running;
  $('btnPause').innerHTML = '';
  $('btnPause').appendChild(icon(status.paused ? 'play' : 'pause'));
  for (const id of ['btnFull', 'btnCollect', 'btnScore', 'btnGreet']) {
    $(id).disabled = running;
    $(id).classList.toggle('is-busy', running);
  }
  if (running) $('btnApply').disabled = true;

  const pill = $('taskPill');
  const meta = $('taskMeta');
  const prog = $('progress');
  const logs = $('logs');
  clear(prog);

  if (!task) {
    pill.className = 'pill';
    pill.textContent = '空闲';
    meta.textContent = '';
    prog.appendChild(el('div', { class: 'muted small', text: '当前没有运行中的任务。点右上角「运行全流程」开始一次采集 → 评分 → 待确认。' }));
    logs.style.display = 'none';
    logs.textContent = '';
    return;
  }

  // completed 但带 error = 跑完了却没做成（被风控锁 / 没登录 / 闸门拒绝），不能显示成绿色的「已完成」
  const ok = task.status === 'completed' && !task.error;
  const bad = task.status === 'failed';
  pill.className = 'pill ' + (running ? 'live' : (ok ? 'ok' : (bad ? 'bad' : 'warn')));
  pill.textContent = running ? (status.paused ? '已暂停' : '运行中')
    : ({ completed: ok ? '已完成' : '没有做成', failed: '出错', stopped: '已停止' }[task.status] || task.status);
  meta.textContent = [platformName(task.platform), modeName(task.mode)].filter(Boolean).join(' · ');

  const p = task.progress || {};
  if (running && p.total) prog.appendChild(progress(p.current || 0, p.total, p.label || ''));
  else if (running) prog.appendChild(el('div', { class: 'muted small', text: '运行中…' }));
  else if (task.error) prog.appendChild(el('div', { class: 'lock' }, [icon('alert'), task.error]));
  else prog.appendChild(el('div', { class: 'muted small', text: '任务已结束。' }));
  if (running) {
    prog.appendChild(el('div', {
      class: 'hint', style: 'margin-top:8px',
      text: '任务在后台运行：关掉这个页面不会停止它，服务会继续采集/投递；要停就点右上角「停止」。',
    }));
  }

  logs.textContent = (task.logs || []).join('\n');
  logs.style.display = logs.textContent ? '' : 'none';
  logs.scrollTop = logs.scrollHeight;
}
/* ---------------- 岗位池：见 views/jobs.js ---------------- */

/* ---------------- 待跟进 ---------------- */
async function loadFollowUps() {
  const rows = await get('/api/follow-ups');
  const box = $('followUps');
  clear(box);
  const list = rows || [];
  const due = list.filter((row) => row.due);
  $('followCount').textContent = due.length;
  $('followHint').textContent = list.length ? (list.length + ' 个已投递岗位') : '';
  if (!list.length) {
    box.appendChild(empty('还没有已投递的岗位', '投递成功后，超过设定时间没回复的岗位会出现在这里'));
    return;
  }
  for (const row of list) {
    const item = el('div', { class: 'pending-item' });
    item.appendChild(el('div', { class: 'pending-title' }, [
      el('b', { text: row.title || '' }),
      el('span', { class: 'muted', text: row.company || '' }),
      el('span', { class: 'pill ' + (row.due ? 'warn' : ''), text: row.due ? '待跟进' : '已生成' }),
      el('span', { class: 'pill', text: platformName(row.platform) }),
    ]));
    if (row.follow_up) {
      item.appendChild(el('div', { class: 'greeting', text: row.follow_up }));
      item.appendChild(el('div', { class: 'actions' }, [
        el('button', {
          class: 'btn ghost small', text: '复制跟进话术',
          onclick: async () => { await navigator.clipboard.writeText(row.follow_up); toast('已复制', 'ok'); },
        }),
      ]));
    }
    box.appendChild(item);
  }
}

/* ---------------- 监测 ---------------- */
async function loadInteractions() {
  state.interactions = await get('/api/interactions');
  renderMonitor();
}

function renderMonitor() {
  const body = $('hr');
  clear(body);
  const rows = state.interactions || [];
  $('hrHint').textContent = rows.length + ' 条';
  const platforms = (state.status && state.status.platforms) || {};
  const open = Object.keys(platforms).filter((k) => (platforms[k].capabilities || []).includes('monitor'));
  $('hrMode').className = 'pill ' + (open.length ? 'ok' : '');
  $('hrMode').textContent = open.length ? '已开放' : '未开放';
  // 概览条：每个状态各几个 —— 一眼看出整条线索走到哪了
  const labels = (state.status && state.status.progress_states) || {};
  const stats = $('hrStats');
  clear(stats);
  const counts = {};
  for (const row of rows) counts[row.state] = (counts[row.state] || 0) + 1;
  if (rows.length) {
    for (const [key, label] of Object.entries(labels)) {
      if (!counts[key]) continue;
      stats.appendChild(el('span', { class: 'seg-item is-active' }, [label, el('b', { text: String(counts[key]) })]));
    }
  }

  if (!rows.length) {
    body.appendChild(el('tr', {}, [el('td', { colspan: '6' }, [
      empty('还没有进展记录',
        '在岗位详情里点「记录进展」就能记（已查看 / 已回复 / 约面试 / offer…）；'
        + (open.length ? '以后监测驱动会自动写进来' : 'HR 监测驱动还没核验，所以现在先手动记')),
    ])]));
    return;
  }
  for (const row of rows) {
    const tr = el('tr', { onclick: () => row.job_id && openJob(row.job_id) });
    tr.appendChild(el('td', { text: row.company || '' }));
    tr.appendChild(el('td', { class: 'title', text: row.title || '' }));
    tr.appendChild(el('td', { text: row.hr_name || '' }));
    tr.appendChild(el('td', {}, [el('span', { class: 'pill', text: labels[row.state] || row.state || '' })]));
    tr.appendChild(el('td', { class: 'title', text: row.last_text || '' }));
    tr.appendChild(el('td', { class: 'muted', text: fmtTime(row.seen_at) }));
    body.appendChild(tr);
  }
}

/* ---------------- 手动投递回执 ---------------- */
/* 自动投递还没核验之前，真实投递是人在平台上点的。
   这里做两件事：一键"打开 + 复制招呼语"，以及投完之后记一笔 —— 否则今日额度、
   跟进队列、统计都会跟真实情况对不上。 */

async function openAndCopy(job) {
  // 顺序很重要：window.open 必须在用户点击的手势里同步调用，否则会被当成弹窗拦掉
  if (job.url) window.open(job.url, '_blank', 'noopener');
  if (!job.greeting) {
    toast(job.url ? '已打开岗位（这个岗位还没有招呼语）' : '这个岗位没有原链接', job.url ? 'ok' : 'err');
    return;
  }
  try {
    await navigator.clipboard.writeText(job.greeting);
    toast('已打开岗位，招呼语已复制', 'ok');
  } catch (e) {
    toast('已打开岗位，但复制失败（浏览器拒绝了剪贴板写入）', 'err');
  }
}

async function markApplied(job) {
  const left = state.status ? (state.status.daily_limit ?? 0) - (state.status.applied_today ?? 0) : null;
  const hint = left === null ? '' : `\n会占用今日投递额度（还剩 ${left} 次）。`;
  if (!(await confirmDialog('标记为已投递', `确认「${job.title}」已经在平台上投过了？${hint}`))) return;
  const res = await post('/api/jobs/' + encodeURIComponent(job.id) + '/applied', {});
  if (res.error) { toast(res.error, 'err'); return; }
  toast('已记为投递，今日还剩 ' + res.remaining_today + ' 次', 'ok');
  closeDrawer();
  await loadJobs();
  await refresh();
}

/* ---------------- 记录页 ---------------- */
const EVENT_LABEL = {
  risk_lock: ['暂停平台', 'bad'], rate_stop: ['速率停手', 'warn'], risk: ['风控信号', 'bad'],
  page_error: ['页面异常', 'warn'], collect: ['采集', 'ok'], monitor: ['监测', 'ok'],
};

function bars(items, { label = (item) => item.label, value = (item) => item.value, mark = () => false, small = false } = {}) {
  const max = Math.max(1, ...items.map(value));
  const box = el('div', { class: 'bars' + (small ? ' small' : '') });
  for (const item of items) {
    const count = value(item);
    const col = el('div', { class: 'bar-col' + (mark(item) ? ' is-now' : '') + (count ? '' : ' is-empty') }, [
      el('div', { class: 'bar-track', title: label(item) + '：' + count }, [
        el('i', { style: 'height:' + (count ? Math.max(4, Math.round((count / max) * 100)) : 2) + '%' }),
      ]),
      el('span', { text: label(item) }),
    ]);
    box.appendChild(col);
  }
  return box;
}

// 任务历史 / 风控事件各自翻页；后台 15 秒一次的刷新停在用户当前翻到的那一页
const actPage = { runs: 1, events: 1 };

async function loadActivity() {
  state.activityAt = Date.now();
  const data = await get('/api/activity?runs_page=' + actPage.runs + '&events_page=' + actPage.events);
  if (data.error) { toast(data.error, 'err'); return; }
  renderActivity(data);
}

function renderActivity(data) {
  const today = data.today || {};
  const events = today.events || {};
  const week = data.week || {};
  const pages = today.pages || {};

  // KPI：今日做了什么 + 近 7 天风控
  const cards = [
    {
      icon: 'stack', label: '今日开页面', value: today.pages_total || 0, unit: '',
      tone: 'is-accent',
      sub: '搜索 ' + (pages.collect_search || 0) + ' · 详情 ' + (pages.collect_detail || 0)
        + ' · 投递 ' + (pages.apply || 0) + ' · 监测 ' + (pages.monitor || 0),
    },
    { icon: 'list', label: '今日采集', value: events.collected || 0, unit: '', sub: '新入库岗位' },
    {
      icon: 'spark', label: '今日评分', value: events.scored || 0, unit: '',
      sub: '预筛淘汰 ' + (events.prefilter_rejected || 0) + ' 个',
    },
    {
      icon: 'send', label: '今日投递', value: events.applied || 0, unit: '',
      tone: events.applied ? 'is-ok' : '',
      sub: '确认 ' + (events.confirmed || 0) + ' 个',
    },
    {
      icon: 'shield', label: '近 ' + (data.days || 7) + ' 天风控', value: week.risk_lock || 0, unit: '次',
      tone: week.risk_lock ? 'is-warn' : '',
      sub: '速率停手 ' + (week.rate_stop || 0) + ' 次 · 页面异常 ' + (week.page_error || 0) + ' 次',
    },
  ];
  const kpiBox = $('actKpis');
  clear(kpiBox);
  for (const card of cards) {
    kpiBox.appendChild(el('div', { class: 'kpi ' + (card.tone || '') }, [
      el('div', { class: 'kpi-top' }, [icon(card.icon), card.label]),
      el('div', { class: 'v' }, [String(card.value)]),
      el('div', { class: 's', text: card.sub }),
    ]));
  }

  // 今日 24 小时节奏：机器人最明显的特征就是"整段整段地连续开页面"
  const byHour = {};
  for (const row of (data.pages_by_hour || [])) byHour[Number(row.hour)] = Number(row.count);
  const nowHour = new Date().getHours();
  // 漏斗：从"采到"到"投出去"，一眼看出卡在哪一环
  const funnel = data.funnel || {};
  const steps = [
    ['采集', funnel.total || 0, '采到并入库'],
    ['评分', funnel.scored || 0, 'AI 打过分的'],
    ['过线', funnel.passed || 0, '达到阈值的'],
    ['确认', funnel.confirmed || 0, '你点过确认的'],
    ['投递', funnel.applied || 0, '真正投出去的'],
    ['面试', funnel.interview || 0, '约面 / 已面 / offer'],
    ['Offer', funnel.offer || 0, '拿到 offer 的'],
  ];
  const funnelBox = $('actFunnel');
  clear(funnelBox);
  const top = Math.max(1, ...steps.map(([, n]) => n));
  steps.forEach(([label, count, hint], index) => {
    const rate = index === 0 ? '' : (steps[index - 1][1] ? Math.round((count / steps[index - 1][1]) * 100) + '%' : '—');
    funnelBox.appendChild(el('div', { class: 'funnel-row' }, [
      el('span', { class: 'funnel-label', text: label, title: hint }),
      el('div', { class: 'bar' }, [el('i', { style: 'width:' + Math.round((count / top) * 100) + '%' })]),
      el('b', { class: 'funnel-count', text: String(count) }),
      el('span', { class: 'funnel-rate muted small', text: rate }),
    ]));
  });
  $('actFunnelHint').textContent = '阈值 ' + ((state.config && state.config.scoring && state.config.scoring.threshold) || 60) + ' 分';

  // 关键词效果：哪个词真的带来了机会
  const keywords = data.keywords || [];
  const kwBox = $('actKeywords');
  clear(kwBox);
  if (!keywords.length) {
    kwBox.appendChild(empty('还没有采集记录', '采过一次之后，这里会告诉你哪个关键词更值'));
  } else {
    const table = el('table', { class: 'table compact' });
    table.appendChild(el('thead', {}, [el('tr', {}, [
      el('th', { text: '关键词' }), el('th', { class: 'num', text: '岗位' }),
      el('th', { class: 'num', text: '均分' }), el('th', { class: 'num', text: '最高' }),
      el('th', { class: 'num', text: '过线' }),
    ])]));
    const body = el('tbody');
    for (const row of keywords) {
      const score = row.avg_score;
      const cls = score === null ? 'lo' : (score >= 60 ? 'hi' : (score >= 40 ? 'mid' : 'lo'));
      body.appendChild(el('tr', {}, [
        el('td', { class: 'title', text: row.keyword }),
        el('td', { class: 'num muted', text: String(row.jobs) }),
        el('td', { class: 'num' }, [el('span', { class: 'score ' + cls, text: score === null ? '—' : String(score) })]),
        el('td', { class: 'num muted', text: row.best === null ? '—' : String(row.best) }),
        el('td', { class: 'num', text: String(row.passed) }),
      ]));
    }
    table.appendChild(body);
    kwBox.appendChild(table);
    const best = keywords.find((row) => row.avg_score !== null);
    if (best && best.avg_score < 40) {
      kwBox.appendChild(el('div', {
        class: 'hint warn', style: 'margin-top:8px',
        text: '「' + best.keyword + '」带来的岗位平均只有 ' + best.avg_score + ' 分 —— 换个更贴近你经历的关键词（比如岗位全称里的词）通常比加城市更有效。',
      }));
    }
  }


  // 公司分布：同一家公司批量挂岗位，多半不是真在招人 —— 和岗位性质识别互补（那边看话术，这边看量）
  const companies = data.companies || [];
  const coBox = $('actCompanies');
  clear(coBox);
  if (!companies.length) {
    coBox.appendChild(empty('还没有公司数据', '采过岗位之后，这里会按公司聚合'));
  } else {
    // 只列前 6 家：后面的多半每家只有 1 个岗位，一长串满格的条只会把这一行撑得很高、什么也说明不了
    const most = Math.max(1, ...companies.map((row) => row.jobs));
    coBox.appendChild(el('div', { class: 'kw-row kw-head' }, [
      el('span', { class: 'co-name', text: '公司' }), el('span', { class: 'bar-label', text: '岗位数' }),
      el('b', { class: 'funnel-count', text: '个' }), el('span', { class: 'funnel-rate', text: '均分' }),
    ]));
    for (const row of companies.slice(0, 6)) {
      const pct = Math.round((row.jobs / most) * 100);
      coBox.appendChild(el('div', { class: 'kw-row' }, [
        el('span', { class: 'co-name', title: row.company, text: row.company }),
        el('div', { class: 'bar' }, [el('i', { style: 'width:' + pct + '%' })]),
        el('b', { class: 'funnel-count', text: String(row.jobs) }),
        el('span', { class: 'funnel-rate muted small', text: row.avg_score === null ? '—' : String(row.avg_score) }),
      ]));
    }
    if (companies.length > 6) {
      coBox.appendChild(el('div', { class: 'muted small', text: '另有 ' + (companies.length - 6) + ' 家公司，各只有零星岗位' }));
    }
    const suspect = companies.find((row) => row.jobs >= 3 && (row.avg_score === null || row.avg_score < 40));
    if (suspect) {
      const avg = suspect.avg_score === null ? '—' : suspect.avg_score;
      coBox.appendChild(el('div', {
        class: 'hint warn', style: 'margin-top:8px',
        text: '「' + suspect.company + '」挂了 ' + suspect.jobs + ' 个岗位，平均只有 ' + avg
          + ' 分 —— 这类批量招聘多半是外包 / 中介或培训机构，值得优先跳过。',
      }));
    }
  }

  const hours = [];
  for (let h = 0; h < 24; h += 1) {
    hours.push({ hour: h, label: (h % 3 === 0 ? String(h) : ''), value: byHour[h] || 0 });
  }
  const hourBox = $('actHours');
  clear(hourBox);
  if (!today.pages_total) {
    // 没有数据就明说，不要摆一个空坐标轴让人以为图表坏了
    $('actHourHint').textContent = '';
    hourBox.appendChild(empty('今天还没有开页面', '跑一次采集 / 投递，这里就会画出你今天的节奏'));
  } else {
    hourBox.appendChild(bars(hours, { label: (i) => i.label, value: (i) => i.value, mark: (i) => i.hour === nowHour }));
    $('actHourHint').textContent = '共 ' + today.pages_total + ' 页 · 绿柱是当前小时';
  }

  // 近 7 天
  const days = (data.pages_by_day || []).map((row) => ({
    day: row.day, label: String(row.day).slice(5), value: Number(row.count),
  }));
  const dayBox = $('actDays');
  clear(dayBox);
  if (!days.length || !(week.pages_total)) dayBox.appendChild(empty('近 7 天还没有开页面记录'));
  else {
    dayBox.appendChild(bars(days, { label: (i) => i.label, value: (i) => i.value, small: true,
      mark: (i) => i.day === new Date().toISOString().slice(0, 10) }));
    dayBox.appendChild(el('div', {
      class: 'hint', style: 'margin-top:10px',
      text: '近 7 天合计 ' + (week.pages_total || 0) + ' 页 · 采集 ' + (week.collected || 0)
        + ' · 投递 ' + (week.applied || 0),
    }));
  }

  // 任务历史
  const runs = data.runs || [];
  const runBody = $('actRuns');
  clear(runBody);
  const runsPager = data.runs_pager || { page: 1, pages: 1, total: runs.length, limit: runs.length };
  actPage.runs = runsPager.page;
  $('actRunsHint').textContent = runsPager.total ? '共 ' + runsPager.total + ' 条' : '';
  renderPager($('actRunsPager'), runsPager, (n) => { actPage.runs = n; loadActivity(); });
  if (!runs.length) {
    runBody.appendChild(el('tr', {}, [el('td', { colspan: '6' }, [empty('还没有任务记录', '点右上角「运行全流程」就会留下记录')])]));
  }
  for (const run of runs) {
    let dur = null;
    if (run.finished_at && run.created_at) {
      const span = (new Date(run.finished_at.replace(' ', 'T')) - new Date(run.created_at.replace(' ', 'T'))) / 1000;
      dur = Number.isFinite(span) ? Math.max(0, Math.round(span)) : null;   // 坏数据宁可显示 —，不要露 NaN
    }
    const notDone = run.not_done || (run.status === 'completed' && run.error);
    const tone = run.status === 'completed' ? (notDone ? 'warn' : 'ok') : (run.status === 'failed' ? 'bad' : 'warn');
    const tr = el('tr', {});
    tr.appendChild(el('td', { class: 'muted', text: fmtTime(run.created_at) }));
    tr.appendChild(el('td', { text: modeName(run.mode) }));
    tr.appendChild(el('td', {}, [el('span', { class: 'pill', text: platformName(run.platform) || '—' })]));
    tr.appendChild(el('td', {}, [el('span', { class: 'pill ' + tone, text: run.status === 'completed' ? (notDone ? '没做成' : '完成') : (run.status === 'failed' ? '失败' : '已停止') })]));
    tr.appendChild(el('td', { class: 'muted num', text: dur === null ? '—' : fmtDur(dur) }));
    // 结果 / 原因：后端已经翻成人话（run.summary）；一律左对齐，长了截断、悬停看全文
    const summary = run.summary || runSummary(run);
    tr.appendChild(el('td', { class: 'run-result', title: summary, text: summary }));
    runBody.appendChild(tr);
  }

  // 风控事件
  const list = data.events || [];
  const evBox = $('actEvents');
  clear(evBox);
  const eventsPager = data.events_pager || { page: 1, pages: 1, total: list.length, limit: list.length };
  actPage.events = eventsPager.page;
  $('actEventHint').textContent = eventsPager.total ? '共 ' + eventsPager.total + ' 条' : '';
  renderPager($('actEventsPager'), eventsPager, (n) => { actPage.events = n; loadActivity(); });
  if (!list.length) {
    evBox.appendChild(empty('还没有风控/系统事件', '命中验证码、限流、速率停手都会记在这里'));
  }
  for (const item of list) {
    const [label, tone] = EVENT_LABEL[item.kind] || [item.label || item.kind, ''];
    const text = item.text || item.detail || '';
    evBox.appendChild(el('div', { class: 'ev-row' }, [
      el('span', { class: 'pill ' + tone, text: item.label || label }),
      el('span', { class: 'ev-detail', title: text, text }),
      el('span', { class: 'ev-time', text: fmtTime(item.created_at).slice(5) }),
    ]));
  }
}

const SUMMARY_KEYS = {
  new: '新', seen: '见到', duplicate: '重复', scored: '评分', prefiltered: '预筛淘汰',
  greeted: '招呼语', confirmed: '确认', applied: '投递', failed: '失败', already: '已投过', skipped: '跳过',
  rejected: '拒绝', checked: '监测', events: '互动',
};

/* 任务结果有两种形状：采集/投递是扁平的，full 全流程是嵌套的（collected/scored/...）。
   两种都要能读成人话，不能把 [object Object] 直接摆给用户。 */
function countParts(obj) {
  const parts = [];
  for (const [key, label] of Object.entries(SUMMARY_KEYS)) {
    const value = obj[key];
    if (typeof value === 'number' && value) parts.push(label + ' ' + value);
  }
  if (obj.note) parts.push(String(obj.note));
  if (obj.stopped) parts.push('停手：' + obj.stopped);
  return parts;
}

function runSummary(run) {
  if (run.error) return run.error;
  const result = run.result;
  if (!result || typeof result !== 'object') return result ? String(result) : '—';
  const parts = countParts(result);
  // full 全流程的真实形状是 {platform, steps:{collect,score,greet,apply}}；
  // 早期只处理了扁平的 {new, seen}，导致全流程在"结果"列显示成 ——
  if (result.steps && typeof result.steps === 'object') {
    for (const sub of Object.values(result.steps)) {
      if (sub && typeof sub === 'object') parts.push(...countParts(sub));
    }
  }
  for (const key of ['collected', 'scored', 'greeted', 'confirmed', 'applied', 'monitor', 'followup']) {
    const sub = result[key];
    if (sub && typeof sub === 'object') parts.push(...countParts(sub));
  }
  if (result.awaiting_confirmation) parts.push('等确认 ' + result.awaiting_confirmation + ' 个');
  return parts.join(' · ') || '—';
}

/* ---------------- 详情抽屉 ---------------- */
async function openJob(id) {
  const job = await get('/api/jobs/' + encodeURIComponent(id));
  if (job.error) { toast(job.error, 'err'); return; }
  $('drawerTitle').textContent = job.title || '岗位详情';
  const natureRow = $('drawerNature');
  natureRow.innerHTML = '';
  for (const key of (job.nature || [])) {
    natureRow.appendChild(el('span', {
      class: 'pill ' + (key === 'training' ? 'bad' : 'warn'), text: '疑似 ' + natureLabel(key),
    }));
  }
  const body = $('drawerBody');
  clear(body);

  const grid = el('div', { class: 'kv-grid' });
  const pairs = [
    ['公司', job.company], ['平台', platformName(job.platform)], ['城市', job.city], ['薪资', job.salary],
    ['评分', job.score === null ? '-' : String(job.score)], ['状态', STATUS_LABEL[job.status] || job.status],
  ];
  // 每一对要包成自己的 kv 单元格：直接把 k 和 v 平铺进 grid，会成为两个独立格子，
  // 结果就是「公司 / 平台」挤在第一行、值掉到第三行 —— 之前抽屉里那副错位就是这么来的。
  for (const [k, v] of pairs) {
    grid.appendChild(el('div', { class: 'kv' }, [
      el('span', { class: 'k', text: k }),
      el('b', { text: v || '-' }),
    ]));
  }
  body.appendChild(grid);

  if (job.score_reason) {
    body.appendChild(el('div', {}, [
      el('div', { class: 'muted small', text: '评分理由' }),
      el('div', { class: 'greeting', text: job.score_reason }),
    ]));
  }
  if (job.trace) {
    const parts = [job.trace.model, job.trace.prompt,
      (job.trace.tokens_in + '/' + job.trace.tokens_out + ' tokens'),
      (job.trace.latency_ms + ' ms')].filter(Boolean);
    body.appendChild(el('details', { class: 'tech' }, [el('summary', { text: '技术细节' }), el('div', { class: 'hint', text: parts.join(' · ') })]));
  }
  if (job.greeting) {
    body.appendChild(el('div', {}, [
      el('div', { class: 'muted small', text: '招呼语' }),
      el('div', { class: 'greeting', text: job.greeting }),
      el('div', { class: 'actions' }, [
        el('button', {
          class: 'btn ghost small', text: '复制',
          onclick: async () => { await navigator.clipboard.writeText(job.greeting); toast('已复制', 'ok'); },
        }),
      ]),
    ]));
  }
  if (job.jd) {
    body.appendChild(el('div', {}, [el('div', { class: 'muted small', text: 'JD' }), el('div', { class: 'greeting', text: job.jd })]));
  }
  const footOps = el('div', { class: 'actions' });
  if (job.url || job.greeting) {
    footOps.appendChild(el('button', {
      class: 'btn small primary', text: job.url ? '打开并复制招呼语' : '复制招呼语',
      onclick: () => openAndCopy(job),
    }));
  }
  if (!isApplied(job)) {
    footOps.appendChild(el('button', {
      class: 'btn small', text: '标记为已投递', onclick: () => markApplied(job),
    }));
  }
  const progressBox = el('div', { class: 'field wide' });
  progressBox.appendChild(el('div', { class: 'muted small', text: '记录进展（HR 回复 / 约面 / offer —— 手动记，监测驱动还没核验）' }));
  const progressRow = el('div', { class: 'actions' });
  const states = (state.status && state.status.progress_states) || {};
  for (const [key, label] of Object.entries(states)) {
    progressRow.appendChild(el('button', {
      class: 'btn ghost small', text: label,
      onclick: async () => {
        const res = await post('/api/jobs/' + encodeURIComponent(job.id) + '/progress', { state: key });
        if (res.error) { toast(res.error, 'err'); return; }
        toast('已记录：' + label, 'ok');
        await loadInteractions();
        await refresh();
      },
    }));
  }
  progressBox.appendChild(progressRow);
  body.appendChild(progressBox);

  // 面试准备：到这个阶段了，下一步就是"面试官会问什么"
  const prepBox = el('div', { class: 'field wide' });
  const prepResult = el('div', { class: 'kw-suggest' });
  const prepBtn = el('button', {
    class: 'btn small', text: '生成面试准备',
    onclick: async () => {
      prepBtn.disabled = true;
      prepBtn.textContent = '正在看简历和这份 JD…';
      const res = await post('/api/jobs/' + encodeURIComponent(job.id) + '/interview-prep', {});
      prepBtn.disabled = false;
      prepBtn.textContent = '生成面试准备';
      if (res.error) { toast(res.error, 'err'); prepResult.textContent = res.error; return; }
      clear(prepResult);
      (res.questions || []).forEach((row, index) => {
        prepResult.appendChild(el('div', { class: 'prep-q' }, [
          el('b', { text: (index + 1) + '. ' + row.q }),
          row.why ? el('div', { class: 'hint', text: '他想确认：' + row.why }) : null,
          row.angle ? el('div', { class: 'hint', text: '你可以讲：' + row.angle }) : null,
        ]));
      });
      if ((res.watchouts || []).length) {
        prepResult.appendChild(el('div', {
          class: 'hint warn', style: 'margin-top:8px',
          text: '可能被追问：' + res.watchouts.join('；'),
        }));
      }
      prepResult.appendChild(el('div', {
        class: 'hint', style: 'margin-top:6px', text: '依据你的简历 + 这份 JD（' + (res.model || '') + '）',
      }));
    },
  });
  prepBox.appendChild(el('div', { class: 'muted small', text: '面试准备（问题 + 该用哪段经历回答，只引用简历里有的）' }));
  prepBox.appendChild(el('div', { class: 'actions' }, [prepBtn]));
  prepBox.appendChild(prepResult);
  body.appendChild(prepBox);

  if (capsOf(job.platform).includes('collect')) {
    // 预演：只打开岗位页看按钮在不在，不点、不发 —— 自动投递能不能开，先拿证据再决定
    const probeBox = el('div', { class: 'hint', style: 'margin-top:8px' });
    const probeBtn = el('button', {
      class: 'btn ghost small', text: '预演投递（只看不发）',
      onclick: async () => {
        probeBtn.disabled = true;
        probeBox.textContent = '正在打开岗位页看一眼（不点任何东西）…';
        const res = await post('/api/jobs/' + encodeURIComponent(job.id) + '/probe-apply', {});
        probeBtn.disabled = false;
        if (res.error) { probeBox.textContent = '预演失败：' + res.error; probeBox.className = 'hint warn'; return; }
        probeBox.className = 'hint';
        probeBox.textContent = (res.ok ? '✅ ' : '⚠ ') + res.verdict
          + (res.button ? '　按钮：“' + (res.button.text || '') + '”' : '')
          + '　（没有发送任何消息）';
      },
    });
    footOps.appendChild(probeBtn);
    footOps.appendChild(probeBox);
  }
  if (job.deleted_at) {
    footOps.appendChild(el('button', {
      class: 'btn small', text: '恢复',
      onclick: async () => { await binAction('restore', job.id); closeDrawer(); },
    }));
  } else {
    footOps.appendChild(el('button', {
      class: 'btn ghost small danger', text: '移入回收站',
      onclick: async () => { await binAction('delete', job.id); closeDrawer(); },
    }));
  }
  body.appendChild(footOps);

  const resumeWrap = el('div', {});
  resumeWrap.appendChild(el('div', { class: 'muted small', text: '定制简历' }));
  const resumePreview = el('pre', { class: 'logs' });
  resumePreview.style.display = 'none';
  const resumePath = el('div', {
    class: 'muted small',
    text: job.resume_path ? ('已生成：' + job.resume_path) : '还没有为该岗位生成定制简历（只重组真实经历，不会编造）',
  });
  const resumeBtn = el('button', {
    class: 'btn small',
    text: job.resume_path ? '重新生成' : '生成定制简历',
    onclick: async () => {
      resumeBtn.disabled = true;
      resumeBtn.textContent = '生成中…（会让模型读一遍你的简历）';
      const res = await post('/api/jobs/' + encodeURIComponent(job.id) + '/resume', {});
      resumeBtn.disabled = false;
      resumeBtn.textContent = '重新生成';
      if (res.error) {
        toast(res.error, 'err');
        resumePath.textContent = res.error + (res.ungrounded_numbers ? '：' + res.ungrounded_numbers.join('、') : '');
        return;
      }
      toast('定制简历已生成', 'ok');
      resumePreview.textContent = res.markdown || '';
      resumePreview.style.display = '';
      resumePath.textContent = '已生成：' + res.path;
    },
  });
  resumeWrap.appendChild(el('div', { class: 'actions' }, [
    resumeBtn,
    el('button', {
      class: 'btn ghost small', text: '复制简历',
      onclick: async () => {
        if (!resumePreview.textContent) { toast('先点「生成定制简历」', 'err'); return; }
        await navigator.clipboard.writeText(resumePreview.textContent);
        toast('已复制', 'ok');
      },
    }),
  ]));
  resumeWrap.appendChild(resumePath);
  resumeWrap.appendChild(resumePreview);
  body.appendChild(resumeWrap);

  const events = job.events || [];
  if (events.length) {
    body.appendChild(el('div', { class: 'muted small', text: '时间线' }));
    const tl = el('div', { class: 'timeline' });
    for (const ev of events) {
      tl.appendChild(el('div', { class: 'tl-item' }, [
        el('div', { class: 'tl-dot' }),
        el('div', { class: 'tl-body' }, [
          el('div', { text: (EVENT_NAME[ev.kind] || ev.kind) + (ev.detail ? ' · ' + ev.detail : '') }),
          el('div', { class: 't', text: fmtTime(ev.created_at) }),
        ]),
      ]));
    }
    body.appendChild(tl);
  }
  $('drawerBackdrop').classList.add('is-open');
  $('drawer').classList.add('is-open');
}

/* 系统体检：复用抽屉当面板 —— 不改结构，键盘/焦点行为都现成 */
async function openChecks() {
  const report = await get('/api/checks');
  if (report.error) { toast(report.error, 'err'); return; }
  $('drawerTitle').textContent = report.ok ? '系统体检 · 都过了' : '系统体检 · 有必检项没过';
  $('drawerNature').innerHTML = '';
  const body = $('drawerBody');
  clear(body);
  for (const row of report.checks) {
    const tone = row.ok ? 'ok' : (row.level === 'error' ? 'bad' : 'warn');
    body.appendChild(el('div', { class: 'check-row' }, [
      el('span', { class: 'pill ' + tone, text: row.ok ? '通过' : (row.level === 'error' ? '必须修' : '注意') }),
      el('div', { class: 'check-body' }, [
        el('b', { text: row.label }),
        el('div', { class: 'hint', text: row.detail || '' }),
        row.ok || !row.fix ? null : el('div', { class: 'hint warn', text: '→ ' + row.fix }),
      ]),
    ]));
  }
  body.appendChild(el('div', {
    class: 'hint', style: 'margin-top:10px',
    text: '每一项都通过才能稳定地采集和投递；没通过的项按右边的提示修。',
  }));
  $('drawerBackdrop').classList.add('is-open');
  $('drawer').classList.add('is-open');
}

function closeDrawer() {
  $('drawerBackdrop').classList.remove('is-open');
  $('drawer').classList.remove('is-open');
}
/* ---------------- 配置：见 views/config/ ----------------
   配置页的代码第一次打开配置页时才加载：那里面哪怕有一处语法错误，
   也只有配置页打不开（页面上会说明原因），概览、岗位池这些照常能用。 */
let configModule = null;

async function loadConfig() {
  if (!configModule) {
    try {
      const mod = await import('./views/config/index.js');
      mod.initConfig({ refresh, renderRisk });
      configModule = mod;
    } catch (error) {
      showConfigLoadError(error);
      return;
    }
  }
  await configModule.loadConfig();
}

function showConfigLoadError(error) {
  const form = $('configForm');
  clear(form);
  form.appendChild(empty('配置页没能加载', String((error && error.message) || error), [
    // 浏览器会记住这次加载失败：修好文件之后要整页重新加载才会重新取
    el('button', { class: 'btn small primary', type: 'button', text: '重新加载页面', onclick: () => window.location.reload() }),
  ], 'alert'));
}

/* ---------------- 动作 ---------------- */
async function decide(kind, ids) {
  const res = await post(kind === 'confirm' ? '/api/jobs/confirm' : '/api/jobs/reject', {
    platform: '', ids: ids && ids.length ? ids : null,      // 不限平台：概览的待确认是全平台的
  });
  if (res.error) { toast(res.error, 'err'); return; }
  toast(kind === 'confirm' ? '已确认 ' + (res.confirmed || 0) + ' 个' : '已拒绝 ' + (res.rejected || 0) + ' 个', 'ok');
  await refresh();
}

/* ---------------- 跑任务 ----------------
   直接按配置页里保存的关键词 / 城市 / 页数跑，不再弹「这次用哪些词」的对话框
   （配置里已经填过一遍，每次点采集再填一遍是重复劳动）。
   - 采集 / 投递要开平台页面：用顶栏选的平台
   - 评分 / 招呼语不开页面：所有平台一起处理，不受顶栏平台影响
   - 只有「投递」会真的发出招呼、撤不回，所以只有它先确认一次 */
const TASK_TITLE = {
  collect: '采集', score: 'AI 评分', greet: '生成招呼语', apply: '投递', full: '运行全流程',
};
const NEEDS_PLATFORM = new Set(['collect', 'apply', 'full']);

function taskSummary(mode) {
  const name = platformName(state.platform);
  const cfg = (state.config && state.config.platforms && state.config.platforms[state.platform]) || {};
  const where = (cfg.keywords || []).join('、') + ' · ' + (cfg.cities || []).join('、') + ' · 每组 ' + (cfg.max_pages || 3) + ' 页';
  if (mode === 'collect') return name + '：' + where;
  if (mode === 'full') return name + '：' + where + '，然后评分、写招呼语，停在待确认';
  if (mode === 'apply') return name;
  return '所有平台';
}

async function runTask(mode) {
  if (NEEDS_PLATFORM.has(mode) && !state.platform) { toast('先在顶栏选一个平台', 'warn'); return; }
  if (mode === 'apply') {
    const approved = (state.status && state.status.counts && state.status.counts.approved) || 0;
    const left = (((state.status || {}).platforms || {})[state.platform] || {}).remaining || {};
    const ok = await confirmDialog('投递已确认的岗位',
      '将在' + platformName(state.platform) + '对已确认的岗位逐个发起沟通并发送招呼语，发出后撤不回。\n'
      + '已确认 ' + approved + ' 个 · 今日还能投 ' + (left.daily_limit ?? '-') + ' 个',
      { okText: '开始投递' });
    if (!ok) return;
  }
  const res = await post('/api/tasks', { mode, platform: NEEDS_PLATFORM.has(mode) ? state.platform : '' });
  if (res.error) { await alertDialog((TASK_TITLE[mode] || mode) + '没有开始', res.error, 'warn'); return; }
  toast('已开始' + (TASK_TITLE[mode] || mode) + '（' + taskSummary(mode) + '）', 'ok');
  await refresh();
}

/* ---------------- 共用动作 ---------------- */
function setPlatform(key) {
  if (!key) return;
  state.platform = key;
  $('platform').value = key;
  refresh();
}

async function startFollowUp() {
  const res = await post('/api/tasks', { mode: 'followup', platform: '' });
  if (res.error) { toast(res.error, 'err'); return; }
  toast('已开始生成跟进话术', 'ok');
  refresh();
}

/* ---------------- 轮询 ---------------- */
async function refresh() {
  const status = await get('/api/status');
  if (status.error) return;
  state.status = status;

  if (!state.config) state.config = await get('/api/config');   // 风控条需要"上限"做分母

  if (!state.platform) {
    const keys = Object.keys(status.platforms || {});
    const select = $('platform');
    clear(select);
    for (const key of keys) {
      select.appendChild(el('option', { value: key, text: (status.platforms[key].name || key) }));
    }
    state.platform = keys[0] || '';
  }

  $('idleHint').textContent = status.idle_shutdown_minutes > 0
    ? '页面全关 ' + status.idle_shutdown_minutes + ' 分钟后自动退出' : '常驻运行';

  // 平台能力决定按钮能不能点：不能投递的平台，"投递"按钮就该是禁用的并说清原因
  const info = (status.platforms || {})[state.platform] || {};
  const caps = info.capabilities || [];
  const platCfg = (state.config && state.config.platforms && state.config.platforms[state.platform]) || {};
  const canApply = caps.includes('apply') && platCfg.apply !== false;
  const applyBtn = $('btnApply');
  applyBtn.disabled = !canApply;
  applyBtn.title = canApply
    ? '只投递已确认的岗位'
    : '当前平台还没有开放自动投递：驱动能力未实机核验，或设置里关掉了「允许对该平台投递」';

  renderKpis(status);
  renderTask(status);
  if (state.view === 'overview') {
    renderSteps(status);
    renderRisk(status);
    renderPending(status);
  }
  if (state.view === 'jobs') refreshJobsQuietly();
  // 记录页是聚合查询，不必跟 2 秒的心跳同频；15 秒刷一次足够
  if (state.view === 'activity' && Date.now() - (state.activityAt || 0) > 15000) loadActivity();
}

function schedulePoll() {
  state.pollMs = document.hidden ? 15000 : 2000;   // 后台标签页降频，但仍打心跳
  setTimeout(async () => { await refresh(); schedulePoll(); }, state.pollMs);
}

/* 浏览器连通性单独一条慢轮询：Chrome 没开时 /api/health 要等满 2 秒超时，
   以前它卡在 refresh() 里，等于整个看板每次刷新都慢 2 秒。 */
async function checkBrowser() {
  const health = await get('/api/health');
  $('browserState').className = 'browser-state ' + (health.ok ? 'ok' : 'err');
  $('browserText').textContent = health.ok ? '浏览器已连接' : '浏览器未连接';
}

function scheduleBrowserCheck() {
  setTimeout(async () => { await checkBrowser(); scheduleBrowserCheck(); }, document.hidden ? 60000 : 10000);
}

/* ---------------- 初始化 ---------------- */
function paintSkeletons() {
  const kpis = $('kpis');
  clear(kpis);
  for (let i = 0; i < 4; i += 1) {
    kpis.appendChild(el('div', { class: 'kpi is-skel', 'aria-hidden': 'true' }, [
      el('span', { class: 'skel w-30' }), el('span', { class: 'skel h-l w-50', style: 'margin-top:14px' }),
      el('span', { class: 'skel w-70', style: 'margin-top:14px' }),
    ]));
  }
  for (const id of ['risk', 'pending', 'progress']) {
    if ($(id)) { clear($(id)); $(id).appendChild(skeleton(id === 'risk' ? 5 : 3)); }
  }
}

function init() {
  setTheme(state.theme);
  initJobs({ openJob, runTask, openAndCopy, markApplied, refresh, natureLabel });
  paintSkeletons();
  window.addEventListener('jp:offline', () => $('offlineBar').classList.add('is-open'));
  window.addEventListener('jp:online', () => { $('offlineBar').classList.remove('is-open'); refresh(); });
  document.querySelectorAll('.nav-item').forEach((btn) => btn.addEventListener('click', () => switchView(btn.dataset.view)));

  $('platform').addEventListener('change', (event) => setPlatform(event.target.value));
  $('btnRefresh').addEventListener('click', refresh);
  $('btnTheme').addEventListener('click', () => setTheme(state.theme === 'dark' ? 'light' : 'dark'));
  for (const [id, mode] of [['btnCollect', 'collect'], ['btnScore', 'score'], ['btnGreet', 'greet'],
    ['btnApply', 'apply'], ['btnFull', 'full']]) {
    $(id).addEventListener('click', () => runTask(mode));
  }
  $('btnStop').addEventListener('click', async () => {
    if (!(await confirmDialog('停止任务', '确定要停止当前任务吗？已完成的进度会保留。'))) return;
    await post('/api/tasks/stop');
    toast('已请求停止', 'ok');
    refresh();
  });
  $('btnPause').addEventListener('click', async () => {
    const paused = state.status && state.status.paused;
    await post(paused ? '/api/tasks/resume' : '/api/tasks/pause');
    refresh();
  });
  // 「全部」= 卡片里列出来的所有平台的待确认岗位（以前只处理顶栏选中的平台，其余的留着不动）
  $('btnConfirmAll').addEventListener('click', async () => {
    const n = (state.status && state.status.counts && state.status.counts.pending_confirm) || 0;
    if (!n) return;
    if (!(await confirmDialog('全部确认', '把 ' + n + ' 个待确认岗位都标记为「已确认」。之后运行「投递」才会真的发出。', { okText: '全部确认' }))) return;
    decide('confirm', null);
  });
  $('btnRejectAll').addEventListener('click', async () => {
    const n = (state.status && state.status.counts && state.status.counts.pending_confirm) || 0;
    if (!n) return;
    if (!(await confirmDialog('全部拒绝', '把 ' + n + ' 个待确认岗位都标记为「被拒」，它们不会再被投递。', { okText: '全部拒绝', kind: 'bad' }))) return;
    decide('reject', null);
  });
  $('btnFollowUp').addEventListener('click', () => startFollowUp());
  $('btnChecks').addEventListener('click', openChecks);
  $('btnDrawerClose').addEventListener('click', closeDrawer);
  $('drawerBackdrop').addEventListener('click', closeDrawer);

  // 只留两个人人都懂的快捷键：Esc 关闭、/ 搜索岗位
  document.addEventListener('keydown', (event) => {
    const tag = (event.target.tagName || '').toLowerCase();
    const typing = tag === 'input' || tag === 'textarea' || tag === 'select';
    if (event.key === 'Escape') {
      if (typing) event.target.blur();
      closeDrawer();
      return;
    }
    if (typing) return;
    if (event.key === '/') { event.preventDefault(); switchView('jobs'); $('jobQuery').focus(); }
  });

  // 风控倒计时：每 0.5 秒本地跳动一次，不必等下一次轮询
  setInterval(() => {
    if (state.view !== 'overview') return;
    for (const [key, info] of Object.entries(state.nextOpen)) {
      const node = document.querySelector('[data-next-open="' + key + '"]');
      if (!node) continue;
      const remaining = Math.max(0, info.base - (Date.now() - info.at) / 1000);
      node.textContent = remaining > 0 ? ('最快 ' + Math.ceil(remaining) + ' 秒后开页') : '现在就能开页';
      node.classList.toggle('is-live', remaining <= 0);
    }
  }, 500);

  refresh();
  schedulePoll();
  checkBrowser();
  scheduleBrowserCheck();
}

init();
