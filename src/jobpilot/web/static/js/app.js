import { get, post } from './api.js';
import {
  alertDialog, clear, confirmDialog, el, empty, fmtTime, icon, loadPageSize, meter, progress, remainTone,
  renderPager, savePageSize, skeleton, toast, withLoading,
} from './ui.js';
import { STATUS_LABEL, isApplied, modeName, openJobUrl, platformName } from './common.js';
import { askPermissionOnce, watchAttention, watchTasks } from './notify.js';
import { $, state } from './store.js';
import { binAction, initJobs, isBin, loadJobs, refreshJobsQuietly, setStatusFilter, toggleBin } from './views/jobs.js';
import { refreshTrends } from './views/trends.js';

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


const platformInfo = (platform) => ((state.status && state.status.platforms) || {})[platform] || {};
const natureLabel = (key) => ((state.status && state.status.nature_labels) || {})[key] || key;

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
  if (name === 'monitor') { state.monitorAt = Date.now(); loadFollowUps(); loadInteractions(); loadMonitorSchedule(); }
  if (name === 'activity') loadActivity();
  if (name === 'config') loadConfig();
}

/* ---------------- 概览：流程四步 ----------------
   以前概览上面有两排：一排「下一步」引导条，一排 4 张数字卡（今日投递 / 待确认 / 已确认 / 岗位池），
   同一个「待确认 4」在引导条、数字卡、下面的卡片标题里出现了三遍；「岗位池」还只加了 4 个状态（库里 31 个只写 4）。
   现在合成一排：按流程顺序排的 4 步，每一步 = 数量 + 一句说明 + 这一步该点的按钮。
   点数字本身 = 去岗位池看这些岗位。 */
function renderPipeline(status) {
  const box = $('kpis');
  clear(box);
  const counts = status.counts || {};
  // 不同平台的任务能同时跑：按钮只看「这一步要用的平台 / AI 忙不忙」，不再一跑任务就全禁用
  const tasks = taskList(status);
  const busy = busyPlatforms(status, tasks);
  const legacy = !!status.running && !Array.isArray(status.busy_platforms);
  const busyApply = applyPlatforms().map(([key]) => key).filter((key) => busy.has(key));
  // 一键评分：所有有待评分岗位的平台都在评分时才不让点（只有部分在评，后端跳过那几个）
  const scoring = new Set(tasks.filter(taskRunning).flatMap((t) => (t.claims || []).filter((c) => c.startsWith('ai:')).map((c) => c.slice(3))));
  const freshBy = status.new_by_platform || null;
  const freshKeys = freshBy ? Object.keys(freshBy).filter((k) => freshBy[k]) : Object.keys(status.platforms || {});
  // 某个平台此刻在跑什么（「智联招聘正在采集」），给被挡住的按钮写原因
  const doing = (key) => {
    const t = tasks.find((x) => taskRunning(x) && (x.claims || []).includes(key));
    return t ? platformName(key) + '正在' + (modeName(t.mode) || '跑任务').replace(/^运行/, '') : platformName(key) + '正在跑任务';
  };
  const blockers = {
    score: (legacy || (freshKeys.length && freshKeys.every((k) => scoring.has(k)))) ? '待评分的平台都正在评分，等它们结束' : '',
    // 所有平台轮流投：后端会跳过正被占着的平台（2026-10-21），所以只有「能投的平台全都在忙」才不让点
    apply: (legacy || (busyApply.length && busyApply.length === applyPlatforms().length))
      ? (busyApply.length === 1 ? doing(busyApply[0]) + '，等它结束再投'
        : busyApply.length ? '能投递的 ' + busyApply.length + ' 个平台都在跑任务，等任一个结束再投' : '有任务在跑，等它结束再投') : '',
  };
  // 这一步自己的任务正在跑：按钮变成「评分中…」，说明那行换成进度
  const runningOf = (mode) => tasks.find((t) => taskRunning(t) && t.mode === mode && !t.platform);
  const progressText = (t) => {
    const p = (t && t.progress) || {};
    return p.total ? '进行中 ' + (p.current || 0) + ' / ' + p.total : '进行中…';
  };
  const applied = status.applied_today ?? 0;
  const limit = status.daily_limit ?? 30;
  const fresh = counts.new || 0;
  const pending = counts.pending_confirm || 0;
  const approved = counts.approved || 0;
  const usedPct = limit ? Math.min(100, (applied / limit) * 100) : 0;
  /* 2026-10-20 去重：「采集 601」和趋势的「近 N 天采集」看着像一回事，挪到趋势里；
     评分 / 采集按钮顶栏已有（顶栏评分本来就是所有平台），这里不再放第二份。
     「投递」留着：它是「所有平台轮流投」，顶栏的投递只投选中的那个平台，不重复 */
  const steps = [
    {
      n: 1, label: '待评分', value: fresh, unit: '个', filter: 'new',
      sub: fresh ? '评分过阈值的进「待确认」，没过的不会被投递' : '没有待评分的岗位',
      // 一键评分（2026-10-21）：所有平台的待评分一起评，不用在顶栏一个个切平台；正在评分的平台后端会跳过
      action: fresh ? { text: '开始评分', kind: 'run', onclick: () => runTask('score', { all: true }),
        title: '所有平台的待评分一起评（正在评分的平台会跳过）', running: runningOf('score'), busyText: '评分中' } : null,
      now: !!fresh, block: blockers.score,
    },
    {
      n: 2, label: '待确认', value: pending, unit: '个', filter: 'pending_confirm',
      sub: pending ? '你确认之后才会投递，这是最后一道人工闸门' : '没有在等你确认的岗位',
      action: pending ? { text: '去确认', kind: 'go', onclick: () => goConfirm(), title: '切到投递队列的「待确认」，一个个看过再确认' } : null,
      now: !!pending && !fresh, local: true,
    },
    {
      n: 3, label: '已确认待投', value: approved, unit: '个', filter: 'approved',
      sub: approved ? '在能自动投递的平台里轮流投' : '没有已确认、等着投的岗位',
      action: approved ? { text: '开始投递', kind: 'run', onclick: () => runTask('apply', { all: true }),
        title: '在能自动投递的平台里轮流投（点了会先让你确认，发出后撤不回）', running: runningOf('apply'), busyText: '投递中' } : null,
      now: !!approved && !pending && !fresh,
      block: blockers.apply,
    },
    {
      n: 4, label: '今日已投', value: applied, unit: '/ ' + limit, filter: 'applied', pct: usedPct,
      sub: applied >= limit ? '已到今天的上限' : '今天还能投 ' + (limit - applied) + ' 个',
    },
  ];
  for (const step of steps) {
    const card = el('div', { class: 'pipe' + (step.now ? ' is-now' : '') + (step.value ? '' : ' is-zero') });
    card.appendChild(el('div', { class: 'pipe-top' }, [el('span', { class: 'pipe-n', text: String(step.n) }), step.label]));
    card.appendChild(el('button', {
      class: 'pipe-v', type: 'button', title: '去岗位池看这些岗位',
      onclick: () => { switchView('jobs'); setStatusFilter(step.filter); },
    }, [String(step.value), el('span', { class: 'unit', text: step.unit })]));
    if (step.pct !== undefined) card.appendChild(meter(step.pct, step.pct >= 85 ? 'bad' : (step.pct >= 60 ? 'warn' : 'ok')));
    const act = step.action;
    const running = act && act.running;
    const blocked = act && !running && step.block;
    // 说明行：在跑写进度，被挡住写原因（以前原因只藏在按钮的悬停提示里）
    card.appendChild(el('div', { class: 'pipe-sub' + (blocked ? ' is-blocked' : '') + (running ? ' is-running' : ''),
      text: running ? progressText(running) : (blocked || step.sub) }));
    if (act) {
      const btn = el('button', {
        class: 'btn small pipe-btn' + (step.now && !blocked ? ' primary' : '') + (running ? ' is-busy' : ''), type: 'button',
        disabled: !!(blocked || running), title: blocked || act.title || null, onclick: act.onclick,
        'aria-label': running ? act.busyText + '…' : act.text,
      }, running ? [el('span', { class: 'spin', 'aria-hidden': 'true' }), act.busyText + '…']
        : act.kind === 'run' ? [icon('play'), act.text] : [act.text, icon('chev', 'pipe-go')]);
      card.appendChild(btn);
    }
    box.appendChild(card);
  }
}
/* ---------------- 概览：风控仪表 ----------------
   布局 C（2026-10-16）：风控卡片占满一行，4 个平台并排成 4 张小卡 —— 横向一眼比较哪个平台快到上限。
   每张小卡只放结论：能不能开页 + 三格关键数字 + 降速 / 冷却提示。
   明细（四项额度 + 两个速率窗口）只有一条，展开在小卡下面，用小三角指着是哪个平台的。 */
/* 风控锁的类型说人话（后端存的是英文代号） */
const LOCK_KIND = { blocked: '被平台拦截', captcha: '出现验证', rate_limit: '访问太频繁', login: '掉登录' };
function riskTight(remain, limits) {
  // 最紧的一项：所有额度里剩余比例最小的那个 —— 它就是最先卡住你的闸门
  let tight = null;
  for (const stage of STAGES) {
    const value = remain[stage];
    const limit = limits[stage];
    if (value === undefined || value === null || value < 0 || !limit) continue;
    const pct = (value / limit) * 100;
    if (!tight || pct < tight.pct) tight = { stage, value, limit, pct };
  }
  return tight;
}

function riskMeterRow(label, value, limit, usedText) {
  const unlimited = value === undefined || value === null || value < 0 || !limit;
  const pct = unlimited ? 100 : (value / limit) * 100;
  return el('div', { class: 'mrow' }, [
    el('span', { text: label }),
    unlimited ? el('div', { class: 'meter' }) : meter(pct, remainTone(pct)),
    el('b', { text: unlimited ? '不限' : usedText }),
  ]);
}

/* 明细条：左边说明，中间四项额度，右边两个速率窗口 */
function riskDetail(key, info, limits, safety) {
  const remain = info.remaining || {};
  const stageRows = el('div', { class: 'mrows' });
  for (const stage of STAGES) {
    const value = remain[stage];
    const limit = limits[stage];
    stageRows.appendChild(riskMeterRow(STAGE_LABEL[stage], value, limit, value + (limit ? ' / ' + limit : '')));
  }
  const rateRows = el('div', { class: 'mrows rate' });
  for (const [rk, label] of [['per_10min', '近10分钟'], ['per_hour', '近1小时']]) {
    const row = (info.rate || {})[rk];
    if (!row) continue;
    const limit = Number(row.limit) || 0;
    rateRows.appendChild(riskMeterRow(label, limit > 0 ? row.remaining : -1, limit, (row.used || 0) + ' / ' + limit));
  }
  if (!rateRows.childElementCount) rateRows.appendChild(el('div', { class: 'muted small', text: '这个平台没有速率窗口' }));
  const fuse = safety ? safety.max_consecutive_page_failures : null;
  const intro = el('div', { class: 'rdet-intro' }, [
    el('b', { text: (info.name || key) + ' · 明细' }),
    el('div', { class: 'muted small', text: '条越短剩得越少；任何一项用完，这个平台今天就不再开对应的页面。' }),
    (fuse !== null && fuse !== undefined)
      ? el('div', { class: 'muted small', text: '页面连续失败 ' + fuse + ' 次会暂停该平台' }) : null,
  ]);
  return el('div', { class: 'rdet', id: 'riskDetail', role: 'region', 'aria-label': (info.name || key) + ' 风控明细' }, [
    intro,
    el('div', { class: 'rdet-col' }, [el('div', { class: 'rdet-h', text: '今日额度（剩余）' }), stageRows]),
    el('div', { class: 'rdet-col' }, [el('div', { class: 'rdet-h', text: '速率窗口（已用）' }), rateRows]),
  ]);
}

/* 遇到验证 / 掉登录时挂在小卡下面：你在调试浏览器里处理完，点「我已处理，继续」解除冷却（降速保留）。
   限流 / 封禁 / 整体冷却后端会拒绝（409），原因原样告诉你。解除后不自动重跑，由你决定。 */
function attentionBox(key, info, need) {
  const name = info.name || platformName(key);
  const what = need.kind === 'login' ? '登录已失效，请在调试浏览器里重新登录' : '出现了验证（滑块 / 验证码），请在调试浏览器里完成';
  const button = el('button', {
    class: 'btn small primary', type: 'button', text: '我已处理，继续',
    title: '解除这次验证造成的冷却（降速保留）。之后可以重新开始这个平台的任务',
    onclick: () => withLoading(button, async () => {
      const res = await post('/api/risk/resume', { platform: key });
      if (res.error) { await alertDialog('还不能继续', res.error, 'warn'); return; }
      toast(name + '：' + (res.message || '已解除，可以重新开始这个平台的任务'), 'ok', {
        action: '重新采集', onAction: () => { setPlatform(key); runTask('collect'); },
      });
      await refresh();
    }),
  });
  // 不加 role=alert：这块每 2 秒重画一次，读屏会反复念；出现那一下已经有 toast（role=status）播报过
  return el('div', { class: 'rtile-attn' }, [
    el('span', { class: 'lock' }, [icon('alert'), name + what + (need.url ? '（那一页留在调试浏览器里没关）' : '')]),
    button,
  ]);
}

function renderRisk(status) {
  const box = $('risk');
  // 每 2 秒轮询都会重画：重画前焦点在哪张小卡上，重画后还放回去（键盘用户不会被甩回页首）
  const focusKey = document.activeElement && box.contains(document.activeElement)
    ? document.activeElement.getAttribute('data-risk-key') : null;
  clear(box);
  const platforms = status.platforms || {};
  const locks = status.locks || {};
  const keys = Object.keys(platforms);
  const safety = state.config && state.config.safety ? state.config.safety : null;
  const windowOpen = status.active_window_open !== false;
  $('riskHint').textContent = windowOpen
    ? '点平台切换明细'
    : '不在活跃时段 ' + ((status.active_windows || []).join('、') || '') + '：不会开页面';
  if (!keys.length) { box.appendChild(empty('没有可用平台', '检查 config.yaml 的 platforms 配置')); return; }

  /* 手风琴（2026-10-20）：同一时间展开一个平台的明细，其余平台收成一行留在下面，点哪行就切到哪个。
     记住上次展开的是哪个；从没选过就展开额度最紧的那个。'-' = 你点过「收起」，四行都收着。
     出了要你处理的事（验证 / 掉登录 / 冷却 / 降速）那个平台自动展开，你点开别的就听你的 */
  if (state.riskOpenKey === undefined) {
    try { state.riskOpenKey = localStorage.getItem('jp.riskOpen') || ''; } catch (e) { state.riskOpenKey = ''; }
  }
  if (state.riskOpenKey && state.riskOpenKey !== '-' && !platforms[state.riskOpenKey]) state.riskOpenKey = '';
  const urgent = keys.find((k) => (status.attention || []).some((a) => a.platform === k)) || keys.find((k) => locks[k]);
  if (urgent && urgent !== state.riskUrgentSeen) { state.riskOpenKey = urgent; state.riskUrgentSeen = urgent; }
  if (!urgent) state.riskUrgentSeen = '';
  if (!state.riskOpenKey) {
    const tightest = keys.map((k) => [k, riskTight(platforms[k].remaining || {}, platforms[k].limits || (safety ? safety.daily_pages : null) || {})])
      .filter(([, t]) => t).sort((a, b) => a[1].pct - b[1].pct)[0];
    state.riskOpenKey = (keys.find((k) => platforms[k].penalty) || (tightest && tightest[0]) || keys[0]);
  }
  const openKey = state.riskOpenKey === '-' ? '' : state.riskOpenKey;
  const busy = new Set(status.busy_platforms || []);

  const grid = el('div', { class: 'rtiles' });
  for (const key of keys) {
    const info = platforms[key];
    const remain = info.remaining || {};
    const limits = info.limits || (safety ? safety.daily_pages : null) || {};
    const nextIn = info.next_open_in || 0;
    state.nextOpen[key] = { base: nextIn, at: Date.now() };
    const lock = locks[key];
    const tight = riskTight(remain, limits);
    const tone = lock ? 'bad' : (info.penalty ? 'warn' : (tight && tight.pct <= 15 ? 'warn' : 'ok'));
    const hour = (info.rate && info.rate.per_hour) || null;
    const open = key === openKey;

    const tile = el('button', {
      class: 'rtile' + (open ? ' is-open' : ''), type: 'button',
      'aria-expanded': open ? 'true' : 'false', 'aria-controls': 'riskDetail',
      title: open ? '收起明细' : '展开 ' + (info.name || key) + ' 的明细',
      onclick: () => setRiskOpen(open ? '-' : key, status),
      onkeydown: (ev) => {
        // 上下方向键在平台之间切换明细；Esc 收起
        if (ev.key !== 'ArrowDown' && ev.key !== 'ArrowUp') return;
        ev.preventDefault();
        const next = keys[(keys.indexOf(key) + (ev.key === 'ArrowDown' ? 1 : keys.length - 1)) % keys.length];
        setRiskOpen(next, status);
      },
    });
    tile.setAttribute('data-risk-key', key);
    tile.appendChild(el('span', { class: 'rtile-head' }, [
      el('span', { class: 'status-dot t-' + tone }),
      el('b', { text: info.name || key }),
      el('span', { class: 'pill', text: (info.capabilities || []).map((c) => CAP_LABEL[c] || c).join(' · ') || '—' }),
      busy.has(key) ? el('span', { class: 'pill live', text: '跑任务中' }) : null,
    ]));
    tile.appendChild(el('span', {
      class: 'psafe-next' + (nextIn > 0 ? '' : ' is-live'),
      'data-next-open': key,
      text: nextIn > 0 ? ('最快 ' + Math.ceil(nextIn) + ' 秒后开页') : '现在就能开页',
    }));
    tile.appendChild(el('span', { class: 'psafe-sum' }, [
      el('span', { class: 'kv' }, [el('span', { text: '今日可投' }), el('b', { text: String(remain.daily_limit ?? '—') })]),
      el('span', { class: 'kv' }, [
        el('span', { text: tight ? STAGE_LABEL[tight.stage] : '额度' }),
        el('b', { class: tight && tight.pct <= 15 ? 'bad' : '', text: tight ? (tight.value + ' / ' + tight.limit) : '—' }),
      ]),
      el('span', { class: 'kv' }, [
        el('span', { text: '近 1 小时' }),
        el('b', { text: hour ? ((hour.used || 0) + ' / ' + (hour.limit || '—')) : '不限' }),
      ]),
    ]));
    // 收起时也能一眼比较四个平台：四项今日额度各一根迷你条（剩多少），展开后看具体数字
    if (!open) {
      tile.appendChild(el('span', { class: 'rtile-mini', 'aria-hidden': 'true' }, STAGES.map((stage) => {
        const value = remain[stage];
        const limit = limits[stage];
        const pct = limit && value !== undefined && value !== null && value >= 0 ? Math.max(0, Math.min(100, (value / limit) * 100)) : null;
        return el('span', { class: 'rmini', title: STAGE_LABEL[stage] + (pct === null ? '：不限' : '：剩 ' + value + ' / ' + limit) }, [
          el('span', { text: STAGE_LABEL[stage].replace('页', '') }),
          pct === null ? el('i', { class: 'rmini-bar' }) : el('i', { class: 'rmini-bar t-' + remainTone(pct) }, [el('b', { style: 'width:' + pct + '%' })]),
        ]);
      })));
    }
    if (info.penalty) {
      tile.appendChild(el('span', { class: 'lock warn' }, [
        icon('alert'),
        '命中过风控，已降速 ×' + info.penalty.factor + '（还有 ' + info.penalty.remaining_minutes + ' 分钟）',
      ]));
    }
    if (lock) {
      tile.appendChild(el('span', { class: 'lock' }, [
        icon('alert'), '冷却中：' + (LOCK_KIND[lock.kind] || lock.kind || '风控') + '，约 ' + (lock.remaining_minutes ?? '-') + ' 分钟后自动恢复',
      ]));
    }
    const need = (status.attention || []).find((item) => item.platform === key);
    tile.appendChild(el('span', { class: 'rtile-chev', 'aria-hidden': 'true', text: open ? '▴' : '▾' }));
    // 小卡本身是个按钮（点了切换明细），按钮里不能再套按钮：有待处理时包一层，把「我已处理」放在小卡下面
    const item = el('div', { class: 'racc' + (open ? ' is-open' : '') + (need ? ' is-need' : '') }, [tile]);
    if (need) item.appendChild(attentionBox(key, info, need));
    if (open) item.appendChild(riskDetail(key, info, limits, safety));
    grid.appendChild(item);
  }
  box.appendChild(grid);
  box.classList.toggle('is-detail', !!openKey);
  if (focusKey) {
    const again = grid.querySelector('[data-risk-key="' + focusKey + '"]');
    if (again) again.focus();
  }
}

function setRiskOpen(key, status) {
  state.riskOpenKey = key;
  try { localStorage.setItem('jp.riskOpen', key); } catch (e) { /* 隐私模式存不了就不记 */ }
  renderRisk(state.status || status);
  const again = $('risk').querySelector('[data-risk-key="' + (key === '-' ? '' : key) + '"]')
    || document.activeElement;
  if (again && again.focus) again.focus();
}

/* ---------------- 概览：待确认 ---------------- */
/* 「去确认」：以前只是滚到待确认列表 —— 一屏概览里列表本来就在屏幕上，滚不动，看起来像没反应；
   而且投递队列可能停在别的栏。现在：切到「待确认」栏 → 滚到能看见 → 卡片闪一下 → 焦点给第一条 */
function goConfirm() {
  state.queueTab = 'pending'; state.queuePinned = true;
  if (state.status) renderPending(state.status);
  const card = $('pendingCard');
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  card.classList.remove('is-flash'); void card.offsetWidth; card.classList.add('is-flash');
  setTimeout(() => card.classList.remove('is-flash'), 1300);
  const first = $('pending').querySelector('.pi-title, .pending-item button');
  if (first) first.focus({ preventScroll: true });
}

/* 投递队列（2026-10-20）：待确认 / 已确认待投 / 今日已投 三栏。没有待确认时可以看另外两栏，
   卡片不再只剩一个空图标。默认打开第一个有内容的栏；你点过哪栏就停在哪栏（那栏空了再自动换） */
const QUEUE_TABS = ['pending', 'approved', 'applied'];
function queueCounts(status) {
  const counts = status.counts || {};
  return { pending: counts.pending_confirm || (status.pending_confirm || []).length, approved: counts.approved || 0,
    applied: status.applied_today ?? 0 };
}
function renderQueueTabs(status) {
  const n = queueCounts(status);
  $('qnPending').textContent = n.pending; $('qnApproved').textContent = n.approved; $('qnApplied').textContent = n.applied;
  if (!state.queueTab || (!n[state.queueTab] && !state.queuePinned)) state.queueTab = QUEUE_TABS.find((k) => n[k]) || 'pending';
  for (const btn of $('queueTabs').querySelectorAll('[data-q]')) {
    const on = btn.dataset.q === state.queueTab;
    btn.classList.toggle('is-active', on);
    btn.setAttribute('aria-selected', on ? 'true' : 'false');
  }
  const pendingTab = state.queueTab === 'pending';
  $('btnConfirmAll').hidden = !pendingTab; $('btnRejectAll').hidden = !pendingTab;
  return n;
}
/* 已确认待投 / 今日已投：从岗位池接口拉（服务端按状态筛），最多 30 秒拉一次，换栏立刻拉 */
async function loadQueueList(tab) {
  const status = tab === 'approved' ? 'approved' : 'applied';
  const url = '/api/jobs?page=1&limit=50&status=' + status + '&sort=' + (tab === 'applied' ? 'updated' : 'score') + '&dir=desc';
  const data = await get(url);
  if (!data || data.error) return;
  const today = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  const items = (data.items || []).filter((j) => tab !== 'applied' || String(j.updated_at || '').startsWith(today));
  state.queueLists = { ...(state.queueLists || {}), [tab]: { items, at: Date.now() } };
  if (state.queueTab === tab && state.status) renderPending(state.status);
}
function queueRow(job, tab) {
  return el('div', { class: 'q-row' }, [
    el('button', { class: 'q-title', type: 'button', text: job.title || '（无标题）', title: '查看详情', onclick: () => openJob(job.id) }),
    el('span', { class: 'q-meta muted small', text: [job.company, job.salary, platformName(job.platform)].filter(Boolean).join(' · ') }),
    el('span', { class: 'q-tail muted small mono', text: tab === 'applied' ? String(job.updated_at || '').slice(11, 16)
      : (job.score ?? null) !== null ? job.score + ' 分' : '' }),
  ]);
}

function renderPending(status) {
  const list = status.pending_confirm || [];
  const n = renderQueueTabs(status);
  const total = n.pending;
  $('navJobsBadge').textContent = total ? String(total) : '';
  $('btnConfirmAll').disabled = !total;
  $('btnRejectAll').disabled = !total;
  // 三栏都空才缩成「暂无」（窄屏时是一行横条）；classList.toggle 状态没变时不动布局，2 秒轮询不闪
  const none = !n.pending && !n.approved && !n.applied;
  $('pendingCard').classList.toggle('is-compact', none);
  $('ovWork').classList.toggle('no-pending', none);
  const box = $('pending');
  clear(box);
  if (state.queueTab !== 'pending') {
    $('pendingPager').hidden = true;
    const tab = state.queueTab;
    const cached = (state.queueLists || {})[tab];
    if (!cached || Date.now() - cached.at > 30000) loadQueueList(tab);
    const items = cached ? cached.items : [];
    if (!items.length) {
      box.appendChild(empty(tab === 'approved' ? '没有已确认、等着投的岗位' : '今天还没有投出去的岗位',
        tab === 'approved' ? '在「待确认」里确认之后会排到这里，点流水线上的「投递」就开始投' : '投递成功的岗位会按时间列在这里'));
      return;
    }
    if (tab === 'approved') {
      box.appendChild(el('div', { class: 'q-actions' }, [el('span', { class: 'muted small', text: n.approved + ' 个已确认，在能自动投递的平台里轮流投' }),
        el('button', { class: 'btn small primary', type: 'button', text: '开始投递', onclick: () => runTask('apply', { all: true }) })]));
    }
    for (const job of items) box.appendChild(queueRow(job, tab));
    return;
  }
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
  // 在调试浏览器里开（登录在那边），不再走普通浏览器的新标签页
  if (job.url) {
    more.appendChild(el('button', {
      class: 'link', type: 'button', text: '原网页 ↗', title: '在 JobPilot 的浏览器（已登录）里打开',
      onclick: () => openJobUrl(job.url),
    }));
  }
  const side = el('div', { class: 'pi-side' }, [
    el('div', { class: 'pi-ops' }, [
      el('button', { class: 'btn primary small', type: 'button', text: '确认投递', onclick: () => decide('confirm', [job.id]) }),
      el('button', { class: 'btn small', type: 'button', text: '拒绝', onclick: () => decide('reject', [job.id]) }),
    ]),
    more,
  ]);
  return el('div', { class: 'pending-item' }, [main, side]);
}

/* ---------------- 概览：任务 ----------------
   2026-10-16：不同平台的任务可以同时跑（比如 51job 在采集，BOSS 在投递）。
   status.tasks = 运行中的在前、最近结束的在后；旧后端只有 status.task，就当成只有一个。
   每个任务一条横道（任务监控台，见下面 lane）；日志是所有任务合在一起的一条时间线。
   没有任何任务可看时，整张卡片缩成一行横条，待确认那张占满整行。 */
const fmtDur = (sec) => (sec < 60 ? sec + ' 秒' : Math.floor(sec / 60) + ' 分' + (sec % 60 ? ' ' + (sec % 60) + ' 秒' : ''));

function taskList(status) {
  if (Array.isArray(status.tasks)) return status.tasks.filter(Boolean);
  return status.task ? [status.task] : [];
}
const taskRunning = (task) => task.status === 'running';
// 旧后端的任务快照没有 paused：那时只有一个任务，用全局的 status.paused
const taskPaused = (task, status) => taskRunning(task) && (task.paused ?? (!Array.isArray(status.tasks) && !!status.paused));

function taskPillInfo(task, status) {
  // completed 但带 error = 跑完了却没做成（被风控锁 / 没登录 / 闸门拒绝），不能显示成绿色的「已完成」
  const ok = task.status === 'completed' && !task.error;
  if (taskRunning(task)) return { cls: 'live', text: taskPaused(task, status) ? '已暂停' : '运行中' };
  if (ok) return { cls: 'ok', text: '已完成' };
  if (task.status === 'failed') return { cls: 'bad', text: '出错' };
  return { cls: 'warn', text: ({ completed: '没有做成', stopped: '已停止' }[task.status] || task.status || '—') };
}

/* 单个任务的暂停 / 继续 / 停止：带上 id，只动这一个 */
async function taskAction(task, action) {
  if (action === 'stop') {
    const name = [taskPlatform(task), modeName(task.mode)].filter(Boolean).join(' · ');
    if (!(await confirmDialog('停止任务', '确定要停止「' + name + '」吗？已完成的进度会保留，别的任务不受影响。'))) return;
  }
  const res = await post('/api/tasks/' + action, { id: task.id });
  if (res && res.error) { toast(res.error, 'err'); return; }
  if (action === 'stop') toast('已请求停止', 'ok');
  refresh();
}

/* 顶栏按钮按「平台忙不忙」来禁用：别的平台在跑不影响这个平台开新任务 */
function renderTaskButtons(status, tasks) {
  const running = !!status.running;
  $('btnStop').disabled = !running;
  $('btnPause').disabled = !running;
  const live = tasks.filter(taskRunning);
  // 全都暂停了才显示「继续」，否则按一下 = 全部暂停
  const allPaused = live.length ? live.every((t) => taskPaused(t, status)) : !!status.paused;
  $('btnPause').dataset.allPaused = allPaused ? '1' : '';
  $('btnPause').innerHTML = '';
  $('btnPause').appendChild(icon(allPaused ? 'play' : 'pause'));
  $('btnPause').title = allPaused ? '继续所有已暂停的任务' : '暂停所有运行中的任务';
  $('btnStop').title = live.length > 1 ? '停止所有运行中的任务（' + live.length + ' 个）：在安全点收手' : '停止当前任务：在安全点收手，不打断正在进行的动作（关闭页面不会停止它）';

  const busy = busyPlatforms(status, tasks);
  const platBusy = !!state.platform && busy.has(state.platform);
  // 评分位按平台占（ai:51job）：只有「这个平台的评分 / 招呼语正在跑」才禁用顶栏的评分 / 招呼语
  const aiBusy = live.some((t) => (t.claims || []).includes('ai:' + state.platform) || (t.claims || []).includes('ai'));
  // 没有 busy_platforms / claims 的旧后端：一跑任务就全禁用（和以前一样）
  const legacy = running && !Array.isArray(status.busy_platforms);
  const busyTip = platformName(state.platform) + ' 正在跑任务，等它结束或先停止';
  const aiTip = platformName(state.platform) + ' 的评分 / 写招呼语正在跑，等它结束；别的平台可以照常评';
  for (const [id, off, tip] of [
    ['btnCollect', platBusy || legacy, busyTip], ['btnApply', platBusy || legacy, busyTip],
    ['btnScore', aiBusy || legacy, aiTip], ['btnGreet', aiBusy || legacy, aiTip],
  ]) {
    const btn = $(id);
    if (btn.dataset.tip === undefined) btn.dataset.tip = btn.title;
    if (off) { btn.disabled = true; btn.title = tip; }
    else if (id !== 'btnApply') { btn.disabled = false; btn.title = btn.dataset.tip; }
    btn.classList.toggle('is-busy', off);
  }
  // 运行全流程不禁用：面板里会把正在跑的平台置灰
  $('btnFull').disabled = legacy;
  $('btnFull').classList.toggle('is-busy', legacy);
}

function busyPlatforms(status, tasks) {
  if (Array.isArray(status.busy_platforms)) return new Set(status.busy_platforms);
  // 旧后端：从运行中的任务自己推
  const set = new Set();
  for (const t of (tasks || taskList(status)).filter(taskRunning)) {
    for (const p of (t.platforms && t.platforms.length ? t.platforms : String(t.platform || '').split(','))) if (p) set.add(p);
  }
  return set;
}

/* 任务监控台（2026-10-20）：每个任务一条横道，同时看所有在跑的任务，不用再点来点去。
   横道 = 平台色 + 步骤条（全流程：采集 → 评分 → 招呼语 → 投递）+ 进度条 + 关键数字 + 最新一行日志。
   下面一条日志时间线：所有任务的日志按时间排在一起，可按平台筛。 */
const STAGE_STEPS = [['collect', '采集'], ['score', '评分'], ['greet', '招呼语'], ['apply', '投递']];
const PLATFORM_KEYS = ['boss', '51job', 'liepin', 'zhaopin'];
// 横道用哪个平台的颜色：单平台任务用它自己的；多平台 / 所有平台用主色
function laneTone(task) {
  const many = (task.platforms && task.platforms.length > 1) || String(task.platform || '').includes(',');
  return !many && PLATFORM_KEYS.includes(task.platform) ? task.platform : 'x';
}
function stepper(task) {
  const box = el('ol', { class: 'tl-steps', 'aria-label': '进行到哪一步' });
  const order = STAGE_STEPS.map(([k]) => k);
  // 不是全流程：只有一步，就是这个任务自己（采集 / 评分 / …）
  const steps = task.mode === 'full' ? STAGE_STEPS : [[task.mode, modeName(task.mode)]];
  const at = task.mode === 'full' ? order.indexOf(task.stage || 'collect') : 0;
  const done = task.status !== 'running';
  steps.forEach(([key, label], i) => {
    const state = done && task.status === 'completed' && !task.error ? (i <= at ? 'done' : 'skip')
      : (i < at ? 'done' : (i === at ? 'now' : 'todo'));
    box.appendChild(el('li', { class: 'tl-step is-' + state, 'aria-current': state === 'now' ? 'step' : null }, [
      el('i', { 'aria-hidden': 'true', text: state === 'done' ? '✓' : String(i + 1) }), label,
    ]));
  });
  return box;
}
function elapsed(task) {
  const start = parseLocal(task.started_at);
  const end = task.finished_at ? parseLocal(task.finished_at) : Date.now();
  if (start === null || end === null) return '';
  const sec = Math.max(0, Math.round((end - start) / 1000));
  const mm = Math.floor(sec / 60), ss = sec % 60;
  return (mm >= 60 ? Math.floor(mm / 60) + ':' + String(mm % 60).padStart(2, '0') : String(mm).padStart(2, '0'))
    + ':' + String(ss).padStart(2, '0');
}

/* 所有平台轮流投递：每个平台一条小进度条（task.lanes，见 services/applier.apply_all）。
   等间隔 / 冷却的倒计时按后端给的到点时刻（墙上时间）算，每次重画（2 秒）刷新一次 */
const LANE_STATE = {
  applying: ['投递中', 'is-live'], waiting: ['等间隔', 'is-wait'], cooling: ['采集后冷却', 'is-cool'],
  stopped: ['已停', 'is-stop'], skipped: ['没投', 'is-stop'], done: ['投完了', 'is-ok'],
};
function leftText(until) {
  const sec = Math.max(0, Math.round(until - Date.now() / 1000));
  if (!sec) return '';
  return sec < 90 ? '还剩 ' + sec + ' 秒' : '约 ' + Math.ceil(sec / 60) + ' 分钟';
}
function subLanes(lanes, paused) {
  const keys = Object.keys(lanes).sort((a, b) => {
    const r = (k) => (PLATFORM_KEYS.includes(k) ? PLATFORM_KEYS.indexOf(k) : 99);
    return r(a) - r(b);
  });
  const box = el('div', { class: 'tsubs' });
  for (const key of keys) {
    const ln = lanes[key] || {};
    const [word, cls] = LANE_STATE[ln.state] || ['等待', 'is-wait'];
    const total = ln.total || 0;
    const pct = total ? Math.min(100, Math.round(((ln.done || 0) / total) * 100)) : 0;
    let note = ln.state === 'applying' ? (ln.label || '')
      : (ln.state === 'waiting' || ln.state === 'cooling') ? leftText(ln.until || 0)
        : (ln.reason || '');
    if (paused && (ln.state === 'applying' || ln.state === 'waiting')) note = '已暂停';
    box.appendChild(el('div', { class: 'tsub tp-' + (PLATFORM_KEYS.includes(key) ? key : 'x') + ' ' + cls }, [
      el('b', { class: 'tsub-name', text: platformName(key) }),
      el('div', { class: 'tsub-bar', role: 'progressbar', 'aria-label': platformName(key) + ' 投递进度',
        'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(pct) }, [el('i', { style: 'width:' + pct + '%' })]),
      el('span', { class: 'mono tsub-num', text: (ln.applied || 0) + ' / ' + total }),
      el('span', { class: 'tsub-state', text: word }),
      el('span', { class: 'tsub-note', text: note, title: note || null }),
    ]));
  }
  return box;
}

/* 日志里的长链接（猎聘抓 JD 详情时整串 URL）只留岗位编号：https://www.liepin.com/a/80539809.shtml?… → 岗位 80539809 */
function shortUrls(text) {
  return String(text || '').replace(/https?:\/\/\S+/g, (url) => {
    const id = url.match(/\/(?:job|a|jobdetail|jobs)\/([A-Za-z0-9_-]{5,})/) || url.match(/([0-9]{6,})/);
    return id ? '岗位 ' + id[1] : (url.split('/')[2] || '链接');
  });
}
function stripPlatform(text) {
  return String(text || '').replace(/^(BOSS直聘|前程无忧|猎聘|智联招聘)[：:]\s*/, '');
}

function lane(task, status) {
  const run = taskRunning(task);
  const paused = taskPaused(task, status);
  const info = taskPillInfo(task, status);
  const p = task.progress || {};
  const tone = laneTone(task);
  const cls = 'tlane tp-' + tone + (run ? (paused ? ' is-paused' : ' is-run') : ' is-done') + (task.error ? ' is-bad' : '');
  const node = el('div', { class: cls });
  const head = el('div', { class: 'tlane-head' }, [
    el('span', { class: 'tlane-dot', 'aria-hidden': 'true' }),
    el('b', { class: 'tlane-name', text: taskPlatform(task) }),
    el('span', { class: 'muted small', text: modeName(task.mode) }),
    el('span', { class: 'pill ' + info.cls, text: info.text }),
  ]);
  const split = Object.keys(task.lanes || {}).length > 0;
  if (task.mode === 'full' && !split) head.appendChild(stepper(task));   // 只有一步的任务（单独采集 / 评分…）不画步骤条
  head.appendChild(el('div', { class: 'grow' }));
  head.appendChild(el('span', { class: 'tlane-clock mono', title: run ? '已用时' : '用时', text: elapsed(task) }));
  if (run) {
    head.appendChild(el('button', { class: 'btn ghost small', type: 'button', text: paused ? '继续' : '暂停',
      onclick: () => taskAction(task, paused ? 'resume' : 'pause') }));
    head.appendChild(el('button', { class: 'btn danger small', type: 'button', text: '停止',
      title: '任务在后台运行：关掉看板页面不会停止它，要停就点这里（或右上角停止全部）',
      onclick: () => taskAction(task, 'stop') }));
  }
  node.appendChild(head);
  if (!run) {
    // 跑完的收成一行：没做成写原因，做成了写最后一句
    const last = (task.logs || []).slice(-1)[0];
    // 跑完的收成一行（出错也是一行红字，悬停看全文），不再一个任务占一大块
    const why = shortUrls(task.error || last || '任务已结束。');
    node.appendChild(el('div', { class: 'small tlane-sum' + (task.error ? ' is-bad' : ' muted'), text: why, title: why }));
    node.appendChild(el('button', { class: 'btn ghost icon small tlane-x', type: 'button', 'aria-label': '从监控台移走这条已结束的任务',
      title: '移走（记录页里还在）', onclick: () => dismissTask(task.id) }, [icon('x')]));
    return node;
  }
  const pct = p.total ? Math.min(100, Math.round(((p.current || 0) / p.total) * 100)) : null;
  node.appendChild(el('div', { class: 'tlane-bar' + (pct === null ? ' is-indet' : ''), role: 'progressbar',
    'aria-label': taskPlatform(task) + ' 进度', 'aria-valuemin': '0', 'aria-valuemax': '100',
    'aria-valuenow': pct === null ? null : String(pct) }, [el('i', { style: pct === null ? '' : 'width:' + pct + '%' })]));
  const last = (task.logs || []).slice(-1)[0] || '';
  // 分平台投递：总进度写成「已投 x / y」（正在投哪个岗位写在各平台那一行里）
  const sums = Object.values(task.lanes || {}).reduce((a, l) => [a[0] + (l.applied || 0), a[1] + (l.total || 0)], [0, 0]);
  node.appendChild(el('div', { class: 'tlane-foot' }, [
    el('span', { class: 'tlane-label', text: split ? ('已投 ' + sums[0] + ' / ' + sums[1])
      : shortUrls(p.label || (paused ? '已暂停' : stripPlatform(last) || '运行中…')) }),
    p.total ? el('span', { class: 'mono tlane-num', text: (p.current || 0) + ' / ' + p.total + ' · ' + pct + '%' }) : null,
  ]));
  const lanes = task.lanes || {};
  if (Object.keys(lanes).length) node.appendChild(subLanes(lanes, paused));
  else if (last && p.label) node.appendChild(el('div', { class: 'tlane-log mono' }, [el('span', { text: shortUrls(last), title: last }), el('i', { class: 'tlane-caret', 'aria-hidden': 'true' })]));
  return node;
}

/* 日志时间线：所有任务的日志合成一条，按时间排（同一秒内保持各自原来的先后）；筛选只在本页记着 */
function timeline(tasks) {
  const rows = [];
  tasks.forEach((task, ti) => {
    const times = task.log_times || [];
    const day = String(task.started_at || '').slice(0, 10);
    (task.logs || []).forEach((line, i) => {
      // 老后端没有 log_times：用任务开始时间垫底，至少同一任务内顺序不乱
      const at = times[i] || String(task.started_at || '').slice(11, 19);
      rows.push({ key: (day + ' ' + at), at, line, tone: laneTone(task), name: taskPlatform(task), tid: task.id, ti, i });
    });
  });
  rows.sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : (a.ti - b.ti) || (a.i - b.i)));
  return rows;
}
function renderTimeline(tasks) {
  const filterBox = $('taskLogFilter');
  const list = $('logs');
  const all = timeline(tasks);
  // 筛选按钮按固定顺序排（BOSS / 前程无忧 / 猎聘 / 智联），不跟着日志先后跳
  const rank = (t) => (PLATFORM_KEYS.includes(t) ? PLATFORM_KEYS.indexOf(t) : 99);
  const tones = [...new Set(all.map((r) => r.tone))].sort((a, b) => rank(a) - rank(b));
  if (state.logTone && !tones.includes(state.logTone)) state.logTone = '';
  clear(filterBox);
  const chips = [['', '全部']].concat(tones.map((t) => [t, t === 'x' ? '多平台' : platformName(t)]));
  if (chips.length > 2) {
    for (const [tone, label] of chips) {
      const on = (state.logTone || '') === tone;
      filterBox.appendChild(el('button', { class: 'tl-chip' + (tone ? ' tp-' + tone : '') + (on ? ' is-on' : ''), type: 'button',
        'aria-pressed': on ? 'true' : 'false', text: label,
        onclick: () => { state.logTone = tone; renderTask(state.status); } }));
    }
  }
  const rows = (state.logTone ? all.filter((r) => r.tone === state.logTone) : all).slice(-200);
  $('taskTimeline').hidden = !rows.length;
  const running = tasks.some(taskRunning);
  const open = state.logOpen ?? running;
  $('taskTimeline').classList.toggle('is-collapsed', !open);
  $('taskLogToggle').setAttribute('aria-expanded', open ? 'true' : 'false');
  $('taskLogToggle').title = open ? '收起运行日志' : '展开运行日志';
  $('taskLogCount').textContent = open ? '' : (running ? '' : '最近一次 ') + rows.length + ' 条';
  filterBox.hidden = !open;
  // 内容没变就不重写，免得用户往上翻日志时被拽回底部
  const sig = (state.logTone || '') + '|' + rows.length + '|' + (rows.length ? rows[rows.length - 1].tid + rows[rows.length - 1].i : '');
  if (list.dataset.sig === sig) return;
  const atBottom = !list.dataset.sig || list.scrollHeight - list.scrollTop - list.clientHeight < 24;
  list.dataset.sig = sig;
  clear(list);
  for (const r of rows) {
    list.appendChild(el('li', { class: 'tl-row' }, [
      el('time', { class: 'mono muted', text: r.at || '' }),
      el('span', { class: 'tl-tag tp-' + r.tone, text: r.name }),
      el('span', { class: 'tl-text', text: shortUrls(r.line), title: r.line.length > 120 ? r.line : null }),
    ]));
  }
  if (atBottom) list.scrollTop = list.scrollHeight;
}

/* 空闲时任务卡里列今天跑过的任务（/api/activity 的任务历史，看板重启后也在），每个一行；
   最多 30 秒拉一次。今天一个都没跑过才显示「点运行全流程」 */
function renderTodayRuns(list) {
  const today = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  const runs = (state.todayRuns || []).filter((r) => String(r.created_at || '').startsWith(today));
  if (!state.todayRunsAt || Date.now() - state.todayRunsAt > 30000) {
    state.todayRunsAt = Date.now();
    get('/api/activity?days=1&page_size=30').then((data) => {
      if (!data || data.error || !Array.isArray(data.runs)) return;
      state.todayRuns = data.runs;
      if (state.status && !taskList(state.status).length) renderTask(state.status);
    });
  }
  if (!runs.length) {
    list.appendChild(empty('没有在跑的任务', '点「运行全流程」开始一次采集 → 评分 → 待确认',
      [el('button', { class: 'btn small', text: '运行全流程', onclick: () => runTask('full') })]));
    return;
  }
  list.appendChild(el('div', { class: 'today-head muted small', text: '今天跑过的任务' }));
  // 卡片有多高就列多少（最多 20 个，卡片里滚）；想看全部去记录页
  for (const run of runs.slice(0, 20)) {
    const start = parseLocal(run.created_at); const end = parseLocal(run.finished_at);
    const dur = start !== null && end !== null ? fmtDur(Math.max(0, Math.round((end - start) / 1000))) : '';
    const bad = run.status === 'failed' || run.not_done || !!run.error;
    const name = [platformName(run.platform) || (run.mode === 'apply' ? '所有平台' : ''), modeName(run.mode)].filter(Boolean).join(' · ');
    const tone = PLATFORM_KEYS.includes(run.platform) ? run.platform : 'x';
    list.appendChild(el('div', { class: 'tlane is-done tp-' + tone + (bad ? ' is-bad' : '') }, [
      el('div', { class: 'tlane-head' }, [el('span', { class: 'tlane-dot', 'aria-hidden': 'true' }), el('b', { class: 'tlane-name', text: name })]),
      el('div', { class: 'small tlane-sum' + (bad ? ' is-bad' : ' muted'), text: run.error || run.summary || '', title: run.error || run.summary || '' }),
      el('span', { class: 'tlane-clock mono', text: String(run.created_at || '').slice(11, 16) + (dur ? ' · ' + dur : '') }),
    ]));
  }
  list.appendChild(el('button', { class: 'link small tlane-more', type: 'button',
    text: runs.length > 20 ? '今天共 ' + runs.length + ' 个任务 · 去记录页看全部' : '去记录页看任务历史',
    onclick: () => switchView('activity') }));
}

/* 监控台里还该留着哪些已结束的任务（2026-10-21）：
   - 同一个平台后来又开了任务（在跑或更晚开始的）→ 旧的移走（「所有平台」的任务和每个平台都算同一个平台）
   - 正常完成的留 10 分钟；停止 / 出错的一直留着，直到被同平台的新任务替代或你点 × 关掉
   移走的不会丢：「记录」页的任务历史、空闲时「今天跑过的任务」里都有 */
const ENDED_OK_MS = 10 * 60 * 1000;
function taskKeys(task) {
  const many = (task.platforms && task.platforms.length) ? task.platforms
    : String(task.platform || '').split(',').filter(Boolean);
  return many.length ? many : ['*'];                    // 没写平台 = 所有平台
}
function overlaps(a, b) {
  const ka = taskKeys(a); const kb = taskKeys(b);
  return ka.includes('*') || kb.includes('*') || ka.some((k) => kb.includes(k));
}
function dismissedTasks() {
  if (!state.dismissed) {
    try { state.dismissed = new Set(JSON.parse(sessionStorage.getItem('jp.dismissedTasks') || '[]')); } catch (e) { state.dismissed = new Set(); }
  }
  return state.dismissed;
}
function dismissTask(id) {
  dismissedTasks().add(id);
  try { sessionStorage.setItem('jp.dismissedTasks', JSON.stringify([...state.dismissed].slice(-50))); } catch (e) { /* 隐私模式 */ }
  if (state.status) renderTask(state.status);
}
function keptEnded(tasks) {
  const gone = dismissedTasks();
  return tasks.filter((t) => {
    if (taskRunning(t) || gone.has(t.id)) return false;
    const started = parseLocal(t.started_at) || 0;
    if (tasks.some((o) => o !== t && overlaps(o, t) && (taskRunning(o) || (parseLocal(o.started_at) || 0) > started))) return false;
    const ok = t.status === 'completed' && !t.error;
    const end = parseLocal(t.finished_at);
    return !(ok && end !== null && Date.now() - end > ENDED_OK_MS);
  });
}

/* 采集被 1 小时 / 10 分钟开页上限打断后的自动续采（status.collect_resume），和「下次先补」（status.collect_carry）：
   放在任务监控台最上面一行一行写清楚，续采可以取消 */
function joinLabels(labels) {
  const list = labels || [];
  return list.slice(0, 3).join('、') + (list.length > 3 ? ' 等 ' + list.length + ' 个' : '');
}
function pendingWork(status) {
  const box = el('div', { class: 'tpend' });
  for (const [key, r] of Object.entries(status.collect_resume || {})) {
    const mins = Math.max(1, Math.ceil((r.in_seconds || 0) / 60));
    const when = r.in_seconds > 0 ? '约 ' + mins + ' 分钟后（' + String(r.at || '').slice(11, 16) + '）' : '马上';
    const btn = el('button', { class: 'btn ghost small', type: 'button', text: '取消续采',
      title: '不自动续采了（没采到的组合还会在下次采集时先补）',
      onclick: () => withLoading(btn, async () => {
        const res = await post('/api/collect/resume/cancel', { platform: key });
        if (res && res.error) { toast(res.error, 'err'); return; }
        toast('已取消' + platformName(key) + '的自动续采', 'ok'); refresh();
      }) });
    box.appendChild(el('div', { class: 'tpend-row tp-' + (PLATFORM_KEYS.includes(key) ? key : 'x') }, [
      icon('clock'), el('b', { text: platformName(key) }),
      el('span', { text: when + '自动接着' + (r.mode === 'full' ? '跑全流程' : '采') + '：' + joinLabels(r.labels) }),
      r.reason ? el('span', { class: 'muted small', text: '（' + r.reason + '）' }) : null,
      el('div', { class: 'grow' }), btn,
    ]));
  }
  for (const [key, c] of Object.entries(status.collect_carry || {})) {
    if ((status.collect_resume || {})[key]) continue;          // 已经在续采里写了
    box.appendChild(el('div', { class: 'tpend-row is-carry tp-' + (PLATFORM_KEYS.includes(key) ? key : 'x') }, [
      icon('inbox'), el('b', { text: platformName(key) }),
      el('span', { class: 'muted', text: '上次没采到，下次采集会先补：' + joinLabels(c.labels) }),
    ]));
  }
  return box.childElementCount ? box : null;
}

function renderTask(status) {
  const tasks = taskList(status);
  renderTaskButtons(status, tasks);

  const pill = $('taskPill');
  const meta = $('taskMeta');
  const list = $('taskList');
  const prog = $('progress');
  clear(list);
  clear(prog);
  const live = tasks.filter(taskRunning);
  const ended = keptEnded(tasks);
  const idle = !live.length && !ended.length;
  $('ovWork').classList.toggle('no-task', idle);
  $('taskCard').classList.toggle('is-compact', idle);
  $('taskCard').classList.toggle('is-live', live.length > 0);
  $('taskIdleText').textContent = idle ? '点「运行全流程」开始一次采集 → 评分 → 待确认' : '';
  if (idle) {
    pill.className = 'pill';
    pill.textContent = '空闲';
    meta.textContent = '';
    $('taskTimeline').hidden = true;
    const waitingIdle = pendingWork(status);
    if (waitingIdle) list.appendChild(waitingIdle);
    renderTodayRuns(list);
    return;
  }
  pill.className = 'pill ' + (live.length ? 'live' : '');
  pill.textContent = live.length ? (live.length + ' 个运行中') : '空闲';
  meta.textContent = live.length ? '' : '最近的任务';
  // 在跑的排前面（按开始时间），跑完的排后面、收成一行
  // 结束的只露最近 2 个，更早的收进「还有 N 个已结束」（点开再看）；被替代 / 收走的不算
  const shownEnded = state.showEnded ? ended : ended.slice(0, live.length ? 1 : 2);
  const waiting = pendingWork(status);
  if (waiting) list.appendChild(waiting);
  for (const task of [...live, ...shownEnded]) list.appendChild(lane(task, status));
  if (ended.length > shownEnded.length || (state.showEnded && ended.length > 2)) {
    list.appendChild(el('button', { class: 'link small tlane-more', type: 'button',
      text: state.showEnded ? '收起已结束的任务' : '还有 ' + (ended.length - shownEnded.length) + ' 个已结束的任务',
      onclick: () => { state.showEnded = !state.showEnded; renderTask(state.status || status); } }));
  }
  renderTimeline([...live, ...ended]);
}

/* ---------------- 岗位池：见 views/jobs.js ---------------- */

/* ---------------- 待跟进 ---------------- */
// 后端存的是本地时间 "YYYY-MM-DD HH:MM:SS"，换成 T 才能被 Date 按本地时间解析
function parseLocal(value) {
  const t = value ? new Date(String(value).replace(' ', 'T')).getTime() : NaN;
  return Number.isNaN(t) ? null : t;
}

// "还要等多久"说人话：不足 1 小时按 1 小时算，超过一天就说天数
function waitText(dueAt) {
  const t = parseLocal(dueAt);
  if (t === null) return '等待中';
  const hours = Math.max(1, Math.ceil((t - Date.now()) / 3600000));
  return hours >= 24 ? (Math.ceil(hours / 24) + ' 天后到期') : (hours + ' 小时后到期');
}

const FOLLOW_STATE = {
  due: ['warn', '待跟进'],
  answered: ['ok', '已有回复'],
  done: ['', '已跟进'],
  unknown: ['', '缺投递时间'],
  disabled: ['', '跟进已关闭'],
};

/* 监测页两张表也分页：已投递岗位 / 进展记录多起来以后，第二张表被挤到很下面 */
const MON_SIZES = [20, 50, 100];
const monPage = {
  follow: 1, followSize: loadPageSize('follow', MON_SIZES, MON_SIZES[0]),
  hr: 1, hrSize: loadPageSize('hr', MON_SIZES, MON_SIZES[0]),
};

/* 监测页筛选（2026-10-20）：平台 + 搜索（公司 / 岗位 / HR），两张表一起筛；在服务端筛，翻页也对 */
const monFilter = { platform: '', q: '' };
try {
  const saved = JSON.parse(localStorage.getItem('jp.monFilter') || '{}');
  monFilter.platform = typeof saved.platform === 'string' ? saved.platform : '';
} catch (e) { /* 读不到就用默认 */ }
const monQuery = () => (monFilter.platform ? '&platform=' + encodeURIComponent(monFilter.platform) : '')
  + (monFilter.q ? '&q=' + encodeURIComponent(monFilter.q) : '');
function setMonFilter(patch) {
  Object.assign(monFilter, patch);
  try { localStorage.setItem('jp.monFilter', JSON.stringify({ platform: monFilter.platform })); } catch (e) { /* 隐私模式 */ }
  monPage.follow = 1; monPage.hr = 1;
  $('monClear').hidden = !monFilter.platform && !monFilter.q;
  loadFollowUps(); loadInteractions();
}
function initMonitorBar() {
  const sel = $('monPlatform');
  const fill = () => {
    const platforms = (state.status && state.status.platforms) || {};
    const keys = Object.keys(platforms);
    if (sel.options.length === keys.length + 1) return;
    clear(sel);
    sel.appendChild(el('option', { value: '', text: '全部平台' }));
    for (const k of keys) sel.appendChild(el('option', { value: k, text: platformName(k) }));
    sel.value = monFilter.platform;
    if (sel.value !== monFilter.platform) monFilter.platform = '';
  };
  fill();
  sel.addEventListener('focus', fill);
  sel.addEventListener('change', () => setMonFilter({ platform: sel.value }));
  const input = $('monQuery');
  let timer = 0;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => setMonFilter({ q: input.value.trim() }), 300);   // 打字停一下再查，不是每个字都请求
  });
  input.addEventListener('keydown', (e) => { if (e.key === 'Escape' && input.value) { input.value = ''; setMonFilter({ q: '' }); } });
  $('monClear').addEventListener('click', () => { input.value = ''; sel.value = ''; setMonFilter({ platform: '', q: '' }); input.focus(); });
  $('monClear').hidden = !monFilter.platform;
  initMonitorBar.fill = fill;
}

/* 自动监测下一次什么时候跑（/api/monitor/schedule）；老后端没有这个接口就不显示 */
async function loadMonitorSchedule() {
  const box = $('monAuto');
  const data = await get('/api/monitor/schedule');
  if (!data || data.error || data.auto === undefined) { box.textContent = ''; return; }
  if (!data.auto) { box.textContent = '自动监测已关（配置 → 监测里打开）'; return; }
  const when = data.next_run ? '下一次约 ' + String(data.next_run).slice(11, 16) : '';
  box.textContent = '自动监测：每 ' + data.interval_minutes + ' 分钟 · ' + data.working_hours
    + (when ? ' · ' + when : '') + (data.reason ? '（' + data.reason + '）' : '');
}

/* 换每页条数：停在原来这页第一条所在的页 */
function monResize(key, size, reload) {
  const first = (monPage[key] - 1) * monPage[key + 'Size'];
  monPage[key + 'Size'] = size;
  monPage[key] = Math.floor(first / size) + 1;
  savePageSize(key, size);
  reload();
}

async function loadFollowUps() {
  const data = await get('/api/follow-ups?page=' + monPage.follow + '&limit=' + monPage.followSize + monQuery());
  const body = $('followUps');
  clear(body);
  // 兼容旧接口（直接返回数组 / 不分页）：页面可能比后台先更新
  const list = Array.isArray(data) ? data : ((data && data.rows) || []);
  const paged = !!(data && !Array.isArray(data) && data.page);
  const total = paged ? data.total : list.length;
  const afterHours = (data && data.after_hours) || 48;
  // 待跟进个数 / 最早到期按全量算（分页后这一页里不一定有）
  const dueCount = paged ? data.due_count : list.filter((row) => row.due).length;
  $('followCount').textContent = dueCount;
  const waiting = list.filter((row) => row.state === 'waiting');
  const nextWait = paged ? data.next_wait_at : (waiting[0] && waiting[0].due_at);
  let hint = total ? (total + ' 个已投递岗位') : '';
  if (total && !dueCount && nextWait) {
    // 后端按到期时间排好了：第一个 waiting 就是最早到期的那个
    hint += '，最早一个' + waitText(nextWait);
  }
  $('followHint').textContent = hint;
  if (paged) monPage.follow = data.page;
  renderPager($('followPager'), paged ? { page: data.page, pages: data.pages, total, limit: data.limit } : null,
    (n) => { monPage.follow = n; loadFollowUps(); },
    { options: MON_SIZES, onChange: (n) => monResize('follow', n, loadFollowUps) });
  if (!list.length && (monFilter.platform || monFilter.q)) {
    body.appendChild(el('tr', {}, [el('td', { colspan: '6' }, [
      empty('没有符合筛选的已投递岗位', '换个平台或搜索词试试，或点上面的「清除筛选」'),
    ])]));
    return;
  }
  if (!list.length) {
    body.appendChild(el('tr', {}, [el('td', { colspan: '6' }, [
      empty('还没有已投递的岗位',
        '投递成功（或在岗位详情里「标记为已投递」）后会列在这里；投递满 ' + afterHours
        + ' 小时仍没有回复，就会变成「待跟进」，可以生成跟进话术'),
    ])]));
    return;
  }
  for (const row of list) {
    const tr = el('tr', { onclick: () => row.id && openJob(row.id) });
    tr.appendChild(el('td', {}, [el('span', { class: 'pill', text: platformName(row.platform) })]));
    tr.appendChild(el('td', { text: row.company || '' }));
    tr.appendChild(el('td', { class: 'title', text: row.title || '' }));
    tr.appendChild(el('td', { class: 'muted', text: fmtTime(row.applied_at) }));
    const [tone, label] = row.state === 'waiting' ? ['', waitText(row.due_at)]
      : (FOLLOW_STATE[row.state] || ['', row.due ? '待跟进' : '']);
    tr.appendChild(el('td', {}, [label ? el('span', { class: 'pill ' + tone, text: label }) : null]));
    const cell = el('td', { class: 'title' });
    if (row.follow_up) {
      cell.appendChild(el('button', {
        class: 'btn ghost small', text: '复制', title: row.follow_up,
        onclick: async (ev) => {
          // 别让点按钮也触发整行的"打开详情"
          ev.stopPropagation();
          try { await navigator.clipboard.writeText(row.follow_up); toast('已复制', 'ok'); } catch (e) { toast('复制失败（浏览器拒绝了剪贴板写入）', 'err'); }
        },
      }));
      cell.appendChild(el('span', { class: 'muted', text: ' ' + row.follow_up }));
    }
    tr.appendChild(cell);
    body.appendChild(tr);
  }
}

/* ---------------- 监测 ---------------- */
async function loadInteractions() {
  const data = await get('/api/interactions?page=' + monPage.hr + '&limit=' + monPage.hrSize + monQuery());
  // 兼容旧接口（直接返回数组）
  if (Array.isArray(data)) { state.interactions = data; state.hrPage = null; }
  else if (data && Array.isArray(data.items)) { state.interactions = data.items; state.hrPage = data; monPage.hr = data.page; }
  else { state.interactions = []; state.hrPage = null; }
  renderMonitor();
}

function renderMonitor() {
  const body = $('hr');
  clear(body);
  const rows = state.interactions || [];
  const paged = state.hrPage;
  $('hrHint').textContent = (paged ? paged.total : rows.length) + ' 条';
  renderPager($('hrPager'), paged && { page: paged.page, pages: paged.pages, total: paged.total, limit: paged.limit },
    (n) => { monPage.hr = n; loadInteractions(); },
    { options: MON_SIZES, onChange: (n) => monResize('hr', n, loadInteractions) });
  const platforms = (state.status && state.status.platforms) || {};
  const open = Object.keys(platforms).filter((k) => (platforms[k].capabilities || []).includes('monitor'));
  $('hrMode').className = 'pill ' + (open.length ? 'ok' : '');
  $('hrMode').textContent = open.length ? '已开放' : '未开放';
  // 概览条：每个状态各几个 —— 一眼看出整条线索走到哪了
  const labels = (state.status && state.status.progress_states) || {};
  const stats = $('hrStats');
  clear(stats);
  // 分页时用服务端的全量计数；旧接口才退回数当前这些行
  let counts = paged && paged.counts;
  if (!counts) {
    counts = {};
    for (const row of rows) counts[row.state] = (counts[row.state] || 0) + 1;
  }
  if (rows.length) {
    for (const [key, label] of Object.entries(labels)) {
      if (!counts[key]) continue;
      stats.appendChild(el('span', { class: 'seg-item is-active' }, [label, el('b', { text: String(counts[key]) })]));
    }
  }

  if (!rows.length && (monFilter.platform || monFilter.q)) {
    body.appendChild(el('tr', {}, [el('td', { colspan: '8' }, [
      empty('没有符合筛选的进展', '换个平台或搜索词试试，或点上面的「清除筛选」'),
    ])]));
    return;
  }
  if (!rows.length) {
    // 说清楚为什么空：监测还没跑过 / 还没有 HR 动静
    const names = open.map(platformName).join(' / ');
    body.appendChild(el('tr', {}, [el('td', { colspan: '8' }, [
      empty('还没有进展记录',
        (names ? names + ' 的 HR 查看 / 回复 / 邀面试由监测自动写进来（BOSS 读聊天列表，其他平台读「我的投递」）。' : '')
        + '自动监测按配置的间隔在时段内跑；想马上看就点右上角「检查一次」。'
        + '也可以在岗位详情里点「记录进展」手动记一笔。',
        [el('button', { class: 'btn small', type: 'button', text: '检查一次', onclick: () => runTask('monitor') })]),
    ])]));
    return;
  }
  for (const row of rows) {
    const tr = el('tr', { class: row.unread ? 'is-unread' : '', onclick: () => row.job_id && openJob(row.job_id) });
    tr.appendChild(el('td', {}, [el('span', { class: 'pill', text: platformName(row.platform) })]));
    tr.appendChild(el('td', {}, [row.unread ? el('span', { class: 'dot-new', title: '有未读消息', 'aria-label': '未读' }) : null, row.company || '']));
    tr.appendChild(el('td', { class: 'title', text: row.title || '' }));
    tr.appendChild(el('td', { text: row.hr_name || '' }));
    tr.appendChild(el('td', {}, [el('span', { class: 'pill', text: labels[row.state] || row.state || '' })]));
    tr.appendChild(el('td', { class: 'title', text: row.last_text || '', title: row.last_text || null }));
    tr.appendChild(el('td', { class: 'muted', text: row.msg_time || fmtTime(row.updated_at || row.seen_at) }));
    // 去回复：在调试浏览器里打开这条会话 / 我的投递（登录在那边）
    const link = row.deeplink || '';
    tr.appendChild(el('td', {}, [link ? el('button', {
      class: 'btn small' + (row.unread ? ' primary' : ' ghost'), type: 'button', text: '去回复',
      title: '在调试浏览器里打开' + platformName(row.platform) + '的这条会话',
      onclick: (ev) => { ev.stopPropagation(); openJobUrl(link, '已在调试浏览器里打开'); },
    }) : null]));
    body.appendChild(tr);
  }
}

/* ---------------- 手动投递回执 ---------------- */
/* 自动投递还没核验之前，真实投递是人在平台上点的。
   这里做两件事：一键"打开 + 复制招呼语"，以及投完之后记一笔 —— 否则今日额度、
   跟进队列、统计都会跟真实情况对不上。 */

async function openAndCopy(job) {
  // 先复制：剪贴板写入要在点击手势里尽早发起，等完打开请求再写可能被浏览器拒绝
  let copied = null;   // null = 没有招呼语，true/false = 复制成败
  if (job.greeting) {
    try { await navigator.clipboard.writeText(job.greeting); copied = true; } catch (e) { copied = false; }
  }
  if (!job.url) {
    // 没链接只能复制；复制结果就是唯一的提示
    if (copied === true) toast('招呼语已复制（这个岗位没有原链接）', 'ok');
    else if (copied === false) toast('复制失败（浏览器拒绝了剪贴板写入）', 'err');
    else toast('这个岗位没有原链接', 'err');
    return;
  }
  // 链接在 JobPilot 的调试浏览器里开（登录在那边）；成功提示合并复制结果，只弹一条
  const okMessage = copied === true ? '已打开岗位，招呼语已复制，粘贴即可'
    : copied === false ? '已打开岗位，但复制失败（浏览器拒绝了剪贴板写入）'
    : '已打开岗位（这个岗位还没有招呼语）';
  const opened = await openJobUrl(job.url, okMessage);
  // 打开失败时 openJobUrl 已经提示了原因；复制成功的话补一句，免得用户以为也没复制
  if (!opened && copied === true) toast('招呼语已复制', 'ok');
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
  risk_resume: ['你已处理', 'ok'],
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
  const week = data.week || {};

  // 以前这里还有一排 5 张数字卡（今日开页面 / 采集 / 评分 / 投递 / 近 7 天风控）：
  // 下面的节奏图、漏斗、风控事件说的就是同一组数，概览上又有一遍「今日投递」，所以删掉了。
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
    tr.appendChild(el('td', {}, [el('span', { class: 'pill', text: taskPlatform(run) || '—' })]));
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
  $('actEventHint').textContent = (eventsPager.total ? '共 ' + eventsPager.total + ' 条' : '')
    + ' · 近 ' + (data.days || 7) + ' 天暂停平台 ' + (week.risk_lock || 0) + ' 次、页面异常 ' + (week.page_error || 0) + ' 次';
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
        // 记了"已回复"之类的进展，这个岗位就退出跟进队列，两张表一起刷新
        await Promise.all([loadInteractions(), loadFollowUps()]);
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

  if (platformInfo(job.platform).can_probe) {
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

function taskSummary(mode, all) {
  if (mode === 'apply' && all) return '所有平台轮流';
  const name = platformName(state.platform);
  const cfg = (state.config && state.config.platforms && state.config.platforms[state.platform]) || {};
  const where = (cfg.keywords || []).join('、') + ' · ' + (cfg.cities || []).join('、') + ' · 每组 ' + (cfg.max_pages || 3) + ' 页';
  if (mode === 'collect') return name + '：' + where;
  if (mode === 'full') return name + '：' + where + '，然后评分、写招呼语，停在待确认';
  if (mode === 'apply') return name;
  if ((mode === 'score' || mode === 'greet') && state.platform) return name + ' 的岗位';
  return '所有平台';
}

/* 任务的平台一栏：没指定平台的投递 = 所有平台轮流投 */
function taskPlatform(task) {
  const many = (task.platforms && task.platforms.length) ? task.platforms
    : (String(task.platform || '').includes(',') ? task.platform.split(',') : null);
  if (many) return many.map(platformName).join('、');             // 多平台全流程：写出勾选的那几个
  if (!task.platform && task.mode === 'apply') return '所有平台';
  return platformName(task.platform);
}

/* 能自动投递的平台：驱动已实机核验 + 设置里允许投递 */
function applyPlatforms() {
  const status = state.status || {};
  return Object.entries(status.platforms || {}).filter(([key, info]) => {
    const cfg = (state.config && state.config.platforms && state.config.platforms[key]) || {};
    return (info.capabilities || []).includes('apply') && cfg.apply !== false;
  });
}

/* 「所有平台轮流投」确认框里逐个平台写清楚：几个已确认、今天还能投几个；不能投的也说出来 */
function applyAllText() {
  const byPlat = (state.status || {}).approved_by_platform || {};
  const usable = applyPlatforms();
  const lines = usable.filter(([key]) => byPlat[key]).map(([key, info]) =>
    '· ' + (info.name || key) + '：已确认 ' + byPlat[key] + ' 个，今天还能投 ' + ((info.remaining || {}).daily_limit ?? '-') + ' 个');
  if (!lines.length) return [];
  const off = Object.keys(byPlat).filter((key) => byPlat[key] && !usable.some(([k]) => k === key));
  if (off.length) lines.push('· ' + off.map(platformName).join('、') + '：没开放自动投递，这次不投');
  return lines;
}

/* 「运行全流程」先打开设置面板：选这一轮跑哪些平台、这一次搜什么（views/flow-sheet.js） */
async function openFullFlow() {
  if (state.browserOk === false) {
    const go = await confirmDialog('浏览器还没打开',
      '全流程要先采集，需要一个带调试端口、已经登录招聘网站的 Chrome，现在它没有开着。\n现在替你打开吗？打开后先登录，再回来点「运行全流程」。',
      { okText: '启动浏览器', kind: 'warn' });
    if (go) launchBrowser();
    return;
  }
  if (!state.config) state.config = await get('/api/config');
  let mod;
  try {
    mod = await import('./views/flow-sheet.js');
  } catch (exc) {
    await alertDialog('全流程面板打不开', '页面脚本加载失败：' + exc.message + '。刷新页面再试。', 'bad');
    return;
  }
  const started = await mod.openFlowSheet({
    status: state.status, config: state.config, preselect: state.platform ? [state.platform] : [],
    start: (body) => post('/api/tasks', body),
  });
  if (started) {
    // 每个平台一个任务、同时开始；部分平台没开成时说明是哪个、为什么
    const n = started.started || 0;
    if (started.failed && started.failed.length) toast('已开始 ' + n + ' 个平台的全流程；没开成：' + started.failed.join('；'), 'warn');
    else toast('已开始 ' + n + ' 个平台的全流程（同时进行）', 'ok');
    await refresh();
  }
}

async function runTask(mode, opts) {
  askPermissionOnce();          // 系统通知只能在点击里请求授权：第一次点任务按钮时顺手问一次
  if (mode === 'full') return openFullFlow();
  const all = mode === 'apply' && !!(opts && opts.all);
  if (NEEDS_PLATFORM.has(mode) && !all && !state.platform) { toast('先在顶栏选一个平台', 'warn'); return; }
  // 采集 / 投递要开平台页面：浏览器没连上就先说清楚，不要等任务跑起来再报一串英文
  if (NEEDS_PLATFORM.has(mode) && state.browserOk === false) {
    const go = await confirmDialog('浏览器还没打开',
      '采集和投递要用一个带调试端口、已经登录招聘网站的 Chrome，现在它没有开着。\n现在替你打开吗？打开后先登录，再回来点「' + (TASK_TITLE[mode] || mode) + '」。',
      { okText: '启动浏览器', kind: 'warn' });
    if (go) launchBrowser();
    return;
  }
  if (all) {
    const lines = applyAllText();
    if (!lines.length) { await alertDialog('没有可以投的岗位', '已确认的岗位所在的平台都还没开放自动投递。', 'warn'); return; }
    const ok = await confirmDialog('所有平台轮流投递',
      '一个平台在等投递间隔时，先去下一个平台投；每个平台自己的间隔、额度和风控一点不少。发出后撤不回。\n'
      + lines.join('\n') + '\n某个平台掉登录或被风控拦下，只停那一个，其它平台继续。',
      { okText: '开始投递' });
    if (!ok) return;
  } else if (mode === 'apply') {
    const approved = ((state.status || {}).approved_by_platform || {})[state.platform] || 0;
    const left = (((state.status || {}).platforms || {})[state.platform] || {}).remaining || {};
    const ok = await confirmDialog('投递已确认的岗位',
      '将在' + platformName(state.platform) + '对已确认的岗位逐个投递，发出后撤不回。\n'
      + platformName(state.platform) + '已确认 ' + approved + ' 个 · 今日还能投 ' + (left.daily_limit ?? '-') + ' 个\n'
      + '想几个平台一起投：用概览「今日投递」卡片上的「投递」。',
      { okText: '开始投递' });
    if (!ok) return;
  }
  // 顶栏的评分 / 招呼语只处理顶栏选的那个平台：前程无忧评分的同时，BOSS 可以照常采集、
  // 智联可以照常投递（2026-10-16 用户要求）。概览上的「评分」仍是所有平台（opts.all）
  const perPlatform = NEEDS_PLATFORM.has(mode) || ((mode === 'score' || mode === 'greet') && !(opts && opts.all));
  const res = await post('/api/tasks', { mode, platform: (perPlatform && !all) ? state.platform : '' });
  if (res.error) { await alertDialog((TASK_TITLE[mode] || mode) + '没有开始', res.error, 'warn'); return; }
  toast('已开始' + (TASK_TITLE[mode] || mode) + '（' + taskSummary(mode, all) + '）', 'ok');
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
  // 任务刚结束的那一下：系统通知 + 响一声 + 标签标题（见 notify.js）
  watchTasks(taskList(status), (task) => [taskPlatform(task), modeName(task.mode)].filter(Boolean).join(' · ') + ' ');
  // 遇到验证 / 掉登录：不等任务结束，马上提醒（处理完回概览点「我已处理，继续」）
  watchAttention(status.attention, (key) => ((status.platforms || {})[key] || {}).name || platformName(key));

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

  renderTask(status);
  // 每一页标题旁都写着当前是哪个账号：投递是不可撤回的，身份不能靠记忆
  if (status.account) $('crumbAccount').textContent = status.account.name;
  if (state.view === 'overview') {
    renderPipeline(status);
    renderRisk(status);
    renderPending(status);
    refreshTrends(status);   // 自己节流：最多 60 秒拉一次
  }
  if (state.view === 'jobs') refreshJobsQuietly();
  // 记录页是聚合查询，不必跟 2 秒的心跳同频；15 秒刷一次足够
  if (state.view === 'activity' && Date.now() - (state.activityAt || 0) > 15000) loadActivity();
  // 监测页：监测任务（手动「检查一次」或自动）刚结束就重拉两张表；平时 60 秒刷一次（自动监测随时可能写进来）
  if (state.view === 'monitor') {
    const last = taskList(status).find((t) => t.mode === 'monitor' && !taskRunning(t));
    const doneMonitor = !!last && last.id !== state.monSeenDone;
    if (last) state.monSeenDone = last.id;
    if (doneMonitor || Date.now() - (state.monitorAt || 0) > 60000) {
      state.monitorAt = Date.now();
      loadFollowUps(); loadInteractions(); loadMonitorSchedule();
    }
    if (initMonitorBar.fill) initMonitorBar.fill();
  }
}

function schedulePoll() {
  state.pollMs = document.hidden ? 15000 : 2000;   // 后台标签页降频，但仍打心跳
  setTimeout(async () => { await refresh(); schedulePoll(); }, state.pollMs);
}

/* 浏览器连通性单独一条慢轮询：Chrome 没开时 /api/health 要等满 2 秒超时，
   以前它卡在 refresh() 里，等于整个看板每次刷新都慢 2 秒。 */
async function checkBrowser() {
  const health = await get('/api/health');
  state.browserOk = !!health.ok;
  $('browserState').className = 'browser-state ' + (health.ok ? 'ok' : 'err');
  $('browserText').textContent = health.ok ? '浏览器已连接' : (health.occupied ? '端口被别的程序占用' : '浏览器未连接');
  $('browserState').title = health.occupied ? health.error : '';
  $('btnLaunchBrowser').hidden = !!health.ok;
}

/* 采集 / 投递都要用「带调试端口、已登录」的 Chrome。没开的话点一下就替你开
   （独立的登录资料目录，登录一次以后一直在；不会动你平时用的 Chrome） */
async function launchBrowser() {
  await withLoading($('btnLaunchBrowser'), async () => {
    const res = await post('/api/browser/launch');
    if (!res.ok) { await alertDialog('浏览器没能打开', res.error || '未知原因', 'warn'); return; }
    await checkBrowser();
    if (res.already) {
      // 以前这里什么都不说：用户只看到「点了没反应」
      toast(state.browserOk ? '浏览器已经开着，已连接' : '浏览器开着但还连不上，稍等几秒再试', state.browserOk ? 'ok' : 'err');
    } else {
      await alertDialog('浏览器已打开', '在刚打开的 Chrome 窗口里登录 BOSS直聘（和其它要用的招聘网站），'
        + '登录一次就会记住。登录好之后回来点「采集」。', 'ok');
    }
  });
}

function scheduleBrowserCheck() {
  setTimeout(async () => { await checkBrowser(); scheduleBrowserCheck(); }, document.hidden ? 60000 : 10000);
}

/* ---------------- 账号切换 ----------------
   两个求职账号分开投递，一次只用一个。每个账号有自己的简历、搜索条件、岗位池、投递记录、
   今日额度和调试 Chrome（登录资料分开、端口也分开）；AI 设置和风控规则两边共用。
   切换会整页刷新：页面上的每个数字都要换成另一个账号的，留一点旧数据都可能让人看错、投错。 */
async function loadAccounts() {
  const res = await get('/api/accounts');
  if (res.error || !Array.isArray(res.accounts)) return;
  const box = $('accountSwitch');
  clear(box);
  for (const acc of res.accounts) {
    box.appendChild(el('button', {
      class: 'acct-item' + (acc.active ? ' is-active' : ''), type: 'button', role: 'radio',
      'aria-checked': acc.active ? 'true' : 'false', text: acc.name,
      title: acc.active ? '当前账号' : '切换到' + acc.name,
      onclick: acc.active ? null : () => switchAccount(acc, res.running),
    }));
  }
}

async function switchAccount(acc, running) {
  if (running || (state.status && state.status.running)) {
    await alertDialog('现在不能切换', '有任务正在运行。先停止，或者等它跑完再切换账号。', 'warn');
    return;
  }
  const ok = await confirmDialog('切换到' + acc.name + '？',
    '切换后岗位池、投递记录、简历、搜索条件都换成' + acc.name + '的；AI 设置和风控规则两边共用。\n'
    + '每个账号有自己的调试 Chrome：切换后点左下角「启动浏览器」，打开的就是' + acc.name + '的浏览器'
    + (acc.ready ? '。' : '（第一次用要在里面登录各个招聘网站）。'),
    { okText: '切换' });
  if (!ok) return;
  const res = await post('/api/accounts/switch', { id: acc.id });
  if (res.error) { await alertDialog('没能切换', res.error, 'warn'); return; }
  location.reload();
}

/* ---------------- 初始化 ---------------- */
function paintSkeletons() {
  const kpis = $('kpis');
  clear(kpis);
  for (let i = 0; i < 4; i += 1) {
    kpis.appendChild(el('div', { class: 'pipe is-skel', 'aria-hidden': 'true' }, [
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
  initMonitorBar();
  $('queueTabs').addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-q]');
    if (!btn) return;
    state.queueTab = btn.dataset.q; state.queuePinned = true;
    (state.queueLists || {})[btn.dataset.q] && (state.queueLists[btn.dataset.q].at = 0);   // 换栏立刻重拉
    if (state.status) renderPending(state.status);
  });
  $('taskLogToggle').addEventListener('click', () => {
    const now = $('taskLogToggle').getAttribute('aria-expanded') === 'true';
    state.logOpen = !now;
    if (state.status) renderTask(state.status);
  });
  paintSkeletons();
  window.addEventListener('jp:offline', () => $('offlineBar').classList.add('is-open'));
  window.addEventListener('jp:online', () => { $('offlineBar').classList.remove('is-open'); refresh(); });
  document.querySelectorAll('.nav-item').forEach((btn) => btn.addEventListener('click', () => switchView(btn.dataset.view)));

  $('platform').addEventListener('change', (event) => setPlatform(event.target.value));
  $('btnRefresh').addEventListener('click', refresh);
  $('btnTheme').addEventListener('click', () => setTheme(state.theme === 'dark' ? 'light' : 'dark'));
  // 监测页「检查一次」：所有开放监测的平台逐个只读一遍（不指定平台）
  $('btnMonitorNow').addEventListener('click', () => runTask('monitor'));
  for (const [id, mode] of [['btnCollect', 'collect'], ['btnScore', 'score'], ['btnGreet', 'greet'],
    ['btnApply', 'apply'], ['btnFull', 'full']]) {
    $(id).addEventListener('click', () => runTask(mode));
  }
  $('btnStop').addEventListener('click', async () => {
    // 顶栏的停止 = 所有运行中的任务（不带 id）；只停一个用任务卡片上的「停止」
    const n = state.status ? taskList(state.status).filter(taskRunning).length : 0;
    const text = n > 1 ? '确定要停止全部 ' + n + ' 个运行中的任务吗？已完成的进度会保留。\n只想停一个：点任务卡片上那一个的「停止」。'
      : '确定要停止当前任务吗？已完成的进度会保留。';
    if (!(await confirmDialog('停止任务', text))) return;
    await post('/api/tasks/stop');
    toast('已请求停止', 'ok');
    refresh();
  });
  $('btnPause').addEventListener('click', async () => {
    // 和按钮图标同一个判断：运行中的全都暂停了 = 全部继续，否则 = 全部暂停
    const paused = $('btnPause').dataset.allPaused === '1';
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
  $('btnLaunchBrowser').addEventListener('click', launchBrowser);
  loadAccounts();
  $('btnDrawerClose').addEventListener('click', closeDrawer);
  $('drawerBackdrop').addEventListener('click', closeDrawer);

  // 只留两个人人都懂的快捷键：Esc 关闭、/ 搜索岗位
  document.addEventListener('keydown', (event) => {
    const tag = (event.target.tagName || '').toLowerCase();
    const typing = tag === 'input' || tag === 'textarea' || tag === 'select';
    if (event.key === 'Escape') {
      if (typing) event.target.blur();
      // 先关最上面那层：详情抽屉开着就只关它；否则概览上展开的风控明细收起（四个平台都收成一行）
      if ($('drawer').classList.contains('is-open')) { closeDrawer(); return; }
      if (state.view === 'overview' && state.riskOpenKey && state.riskOpenKey !== '-' && !typing) {
        const key = state.riskOpenKey;
        setRiskOpen('-');
        const tile = $('risk').querySelector('[data-risk-key="' + key + '"]');
        if (tile) tile.focus();
      }
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
