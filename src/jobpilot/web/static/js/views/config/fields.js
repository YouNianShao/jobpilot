/* 配置页的字段：字段清单（哪一组有哪些配置项）、单个输入框、实时校验。
   加 / 改一个配置项只需要动这个文件。 */

import { get } from '../../api.js';
import { $, state } from '../../store.js';
import { clear, el, icon } from '../../ui.js';

/* 只放"真的会被代码读到"的旋钮：一个假开关比没有开关更糟 */
export const GROUPS = [
  {
    title: '模型', icon: 'spark',
    note: 'AI 只做判断（评分 / 写招呼语 / 改简历），流程、限额、闸门永远由确定性代码控制。',
    fields: [
      ['ai.provider', '服务商', 'provider'],
      ['ai.model', '模型(可从下拉选，也可手填)', 'model'],
      ['ai.base_url', '接口地址'],
      ['ai.api_key', 'API Key', 'password'],
      ['ai.timeout_seconds', '超时(秒)', 'number'],
      ['ai.max_attempts', '失败重试次数', 'number'],
    ],
  },
  {
    title: '评分与筛选', icon: 'spark',
    note: '低于阈值的岗位直接沉到池底，不会进入待确认，也就永远不会被投递。',
    fields: [
      ['scoring.threshold', '评分阈值', 'number'],
      ['scoring.concurrency', '评分并发(1-3)', 'number'],
      ['profile.salary_min', '月薪范围', 'salary', true],
      ['profile.deal_breakers', '排除词(逗号分隔)', 'list'],
      ['profile.block_natures', '评分前直接剔除（不勾 = 只标记、不剔除）', 'natures', true],
    ],
    switches: [['profile.allow_internship', '接受实习岗']],
  },
  {
    title: '简历与话术', icon: 'user',
    note: '招呼语和定制简历都基于这份简历；定制简历有防编造闸门，只会重组真实经历。',
    fields: [
      ['profile.resume_path', '简历路径', 'text', true],
      ['profile.resume_output_dir', '定制简历输出目录'],
      ['profile.greeting_preference', '招呼语偏好(留空=模型自己写)', 'text', true],
      ['follow_up.after_hours', '投递后多久开始跟进(小时)', 'number'],
      ['follow_up.max_times', '同一岗位最多跟进次数', 'number'],
      ['follow_up.min_gap_hours', '两次跟进最小间隔(小时)', 'number'],
    ],
    switches: [['greeting.enabled', 'AI 生成招呼语'], ['follow_up.enabled', '自动跟进']],
  },
  {
    title: '投递', icon: 'send',
    note: '人工确认门关掉之后，评分达标的岗位会被直接投递——建议配合更保守的风控参数。',
    fields: [
      ['apply.daily_limit', '每日投递上限', 'number'],
      ['apply.interval_min', '两次投递最小间隔(秒)', 'number'],
      ['apply.interval_max', '两次投递最大间隔(秒)', 'number'],
    ],
    switches: [['confirm.required', '人工确认门：投递前必须我点确认']],
  },
  {
    title: '风控（第一优先级）', icon: 'shield', danger: true,
    note: '这一组每个数字都是一道闸门。开页面间隔是平台识别机器人最主要的特征，不建议低于 2.5 秒；'
        + '页面预算按"每开一次页面记一次"计，用完当天就自动停手。'
        + '滚动窗口（近 10 分钟 / 每小时）管的是另一种机器特征——把一天的额度在短时间内猛刷完：'
        + '超速时会先自动等窗口滑过去，需要等得比"最多自动等多久"更久就直接停手。'
        + '跨平台联动熔断管的是账号级信号：一天之内被两个平台分别拦下，基本不是平台抽风，'
        + '而是这个账号的行为模式被盯上了 —— 这时候整体停，而不是换个平台接着跑。',
    fields: [
      ['safety.page_interval_min_seconds', '开页面最短间隔(秒)', 'number'],
      ['safety.page_interval_max_seconds', '开页面最长间隔(秒)', 'number'],
      ['safety.delay_multiplier', '保守系数(≥1，越大越慢)', 'number'],
['safety.risk_lock_minutes', '命中风控后冷却(分)', 'number'],
      ['safety.risk_penalty_hours', '命中风控后降速持续(小时)', 'number'],
      ['safety.risk_penalty_factor', '降速倍数(1=关闭)', 'number'],
      ['safety.cross_platform_risk_platforms', '跨平台联动熔断(24h内几个平台中招就整体停，0=关)', 'number'],
      ['safety.max_consecutive_page_failures', '连续失败熔断阈值', 'number'],
      ['safety.recollect_after_hours', '同一页多少小时内不重复采(0=每次，按页记账)', 'number'],
      ['safety.max_pages_per_10min', '近10分钟页面上限(0=不限)', 'number'],
      ['safety.max_pages_per_hour', '每小时页面上限(0=不限)', 'number'],
      ['safety.rate_wait_max_seconds', '超速时最多自动等多久(秒)', 'number'],
      ['safety.day_off_probability', '随机休息日概率(0-1)', 'number'],
      ['safety.send_windows', '允许投递的时段', 'windows'],
      ['safety.active_windows', '允许开页面的时段', 'windows'],
      ['safety.daily_pages.collect_search', '每日搜索页预算', 'number'],
      ['safety.daily_pages.collect_detail', '每日详情页预算', 'number'],
      ['safety.daily_pages.apply', '每日投递页预算', 'number'],
      ['safety.daily_pages.monitor', '每日监测页预算', 'number'],
      ['safety.daily_pages.total', '每日总页面预算', 'number'],
    ],
  },
  {
    title: '浏览器与运行', icon: 'plug',
    note: '看板连的是你自己那个已经登录好的 Chrome；本项目不接管登录、不碰验证码。',
    fields: [
      ['browser.cdp_url', 'Chrome 调试地址'],
      ['server.idle_shutdown_minutes', '页面关闭后自动退出(分，0=常驻)', 'number'],
    ],
  },
];

export function pick(obj, path) {
  return path.split('.').reduce((acc, key) => (acc === null || acc === undefined ? undefined : acc[key]), obj);
}

export function put(obj, path, value) {
  const keys = path.split('.');
  let node = obj;
  for (const key of keys.slice(0, -1)) {
    if (typeof node[key] !== 'object' || node[key] === null) node[key] = {};
    node = node[key];
  }
  node[keys[keys.length - 1]] = value;
}

export function splitList(value) {
  return String(value || '').split(/[,，]/).map((s) => s.trim()).filter(Boolean);
}

/* ---------------- 配置：实时校验 ----------------
   规则来自后端（/api/config 的 _rules，就是 pydantic 模型里的上下限），前端不再抄一份；
   再叠几条跨字段规则（最小 ≤ 最大）。离开输入框就校验，之后边改边校验；保存前全量校验一遍。 */
const CROSS_RULES = [
  ['apply.interval_min', 'apply.interval_max', '最大间隔不能小于最小间隔'],
  ['safety.page_interval_min_seconds', 'safety.page_interval_max_seconds', '最长间隔不能小于最短间隔'],
  ['profile.salary_min', 'profile.salary_max', '薪资上限不能低于下限（0 = 不限）', true],
];

const WINDOW_RE = /^([01]?\d|2[0-4]):[0-5]\d\s*-\s*([01]?\d|2[0-4]):[0-5]\d$/;

function ruleFor(path) {
  const rules = (state.config && state.config._rules) || {};
  if (rules[path]) return rules[path];
  const platform = path.replace(/^platforms\.[^.]+\./, 'platforms.*.');
  return rules[platform] || null;
}

function fieldProblem(path, raw, type) {
  const value = String(raw ?? '').trim();
  const rule = ruleFor(path) || {};
  if (type === 'number' || rule.type === 'int' || rule.type === 'float') {
    if (value === '' && path.startsWith('platforms.') && path.endsWith('.daily_limit')) return '';   // 平台上限可以留空
    if (value === '' && (path === 'profile.salary_min' || path === 'profile.salary_max')) return '';   // 薪资留空 = 不限
    if (value === '') return '必填，需要一个数字';
    const num = Number(value);
    if (!Number.isFinite(num)) return '需要填数字';
    if (rule.type === 'int' && !Number.isInteger(num)) return '需要填整数';
    if (rule.min !== undefined && (rule.exclusive_min ? num <= rule.min : num < rule.min)) return '不能小于 ' + rule.min;
    if (rule.max !== undefined && (rule.exclusive_max ? num >= rule.max : num > rule.max)) return '不能大于 ' + rule.max;
  }
  if (path === 'safety.send_windows' || path === 'safety.active_windows') {
    const bad = splitList(value).find((item) => !WINDOW_RE.test(item));
    if (bad) return '「' + bad + '」不是 HH:MM-HH:MM 格式，例如 08:00-22:00';
  }
  if (path === 'ai.base_url' && value && !/^https?:\/\//i.test(value)) return '要以 http:// 或 https:// 开头';
  if (path === 'browser.cdp_url' && value && !/^https?:\/\/[^/\s]+/i.test(value)) return '形如 http://127.0.0.1:9333';
  return '';
}

function crossProblem(path) {
  for (const [low, high, message, zeroMeansOff] of CROSS_RULES) {
    if (path !== low && path !== high) continue;
    const a = $('cfg::' + low);
    const b = $('cfg::' + high);
    if (!a || !b || a.value === '' || b.value === '') continue;
    const lo = Number(a.value);
    const hi = Number(b.value);
    if (zeroMeansOff && hi === 0) continue;
    if (Number.isFinite(lo) && Number.isFinite(hi) && lo > hi) return { target: high, message };
  }
  return null;
}

export function showFieldError(path, message) {
  const input = $('cfg::' + path);
  if (!input) return;
  const field = input.closest('.field');
  if (!field) return;
  let box = field.querySelector('.field-error');
  field.classList.toggle('is-invalid', !!message);
  input.setAttribute('aria-invalid', message ? 'true' : 'false');
  if (!message) { if (box) box.remove(); return; }
  if (!box) {
    box = el('div', { class: 'field-error', id: 'err::' + path, role: 'alert' });
    field.appendChild(box);
    input.setAttribute('aria-describedby', box.id);
  }
  clear(box);
  box.append(icon('alert'), message);
}

/* 校验一个字段（连带它的配对字段）；返回是否通过 */
function validateField(path, type) {
  const input = $('cfg::' + path);
  if (!input) return true;
  const own = fieldProblem(path, input.value, type);
  showFieldError(path, own);
  for (const [low, high] of CROSS_RULES) {
    if (path !== low && path !== high) continue;
    const cross = crossProblem(path);
    const ownHigh = fieldProblem(high, ($('cfg::' + high) || {}).value, 'number');
    if (!ownHigh) showFieldError(high, cross ? cross.message : '');
  }
  return !own;
}

/* 服务商下拉：换服务商时把接口地址和推荐模型一起带出来（地址、模型仍然可以手改） */
function providerField(path, label, value) {
  const providers = (state.config && state.config._providers) || [];
  const select = el('select', { id: 'cfg::' + path, class: 'select' });
  for (const item of providers) select.appendChild(el('option', { value: item.key, text: item.name }));
  select.value = value || 'custom';
  const hint = el('span', { class: 'field-hint', id: 'cfg-provider-hint' });
  const paint = () => {
    const item = providers.find((p) => p.key === select.value);
    hint.textContent = item ? item.key_hint : '';
    const list = $('cfg-model-list');
    if (list) {
      clear(list);
      for (const model of (item && item.models) || []) list.appendChild(el('option', { value: model }));
    }
  };
  select.addEventListener('change', () => {
    const item = providers.find((p) => p.key === select.value);
    if (item && item.base_url) $('cfg::ai.base_url').value = item.base_url;
    if (item && item.models.length) $('cfg::ai.model').value = item.models[0];
    paint();
    select.dispatchEvent(new Event('input', { bubbles: true }));   // 让「有未保存的修改」提示亮起来
  });
  setTimeout(paint, 0);   // 模型的 datalist 在后面才挂上去
  return el('label', { class: 'field' }, [
    el('span', { class: 'field-meta' }, [el('span', { class: 'label', text: label })]), select, hint,
  ]);
}

/* 岗位性质：勾选框，不让用户手打 outsourcing / training 这种内部代码 */
const NATURES = [['outsourcing', '外包 / 派遣'], ['training', '培训招生'], ['marketing', '营销话术']];
function naturesField(path, label, value) {
  const picked = new Set(Array.isArray(value) ? value : String(value || '').split(',').filter(Boolean));
  const row = el('div', { class: 'nature-picks', id: 'cfg::' + path, role: 'group', 'aria-label': label });
  for (const [key, text] of NATURES) {
    const box = el('input', { type: 'checkbox', value: key });
    box.checked = picked.has(key);
    row.appendChild(el('label', { class: 'switch' }, [box, text]));
  }
  return el('div', { class: 'field wide' }, [
    el('span', { class: 'field-meta' }, [el('span', { class: 'label', text: label })]), row,
  ]);
}

export function readNatures(path) {
  const row = $('cfg::' + path);
  return row ? [...row.querySelectorAll('input:checked')].map((box) => box.value) : [];
}

export function configField(path, label, type, value, wide) {
  if (type === 'provider') return providerField(path, label, value);
  if (type === 'windows') return windowsField(path, label, value);
  if (type === 'salary') return salaryField(label);
  if (type === 'natures') return naturesField(path, label, value);
  // 'list' 是我们自己的字段类型，不是合法的 <input type>：写进去浏览器会退回原生样式，一律落成 text
  const inputType = type === 'number' || type === 'password' ? type : 'text';
  const input = el('input', { type: inputType, id: 'cfg::' + path, autocomplete: 'off', spellcheck: 'false' });
  input.value = value;
  if (type === 'model') {
    input.setAttribute('list', 'cfg-model-list');
    const list = el('datalist', { id: 'cfg-model-list' });
    setTimeout(() => input.after(list), 0);
  }
  const rule = ruleFor(path) || {};
  if (type === 'number') {
    input.step = rule.type === 'int' ? '1' : 'any';
    input.inputMode = rule.type === 'int' ? 'numeric' : 'decimal';
    if (rule.min !== undefined) input.min = String(rule.min);
    if (rule.max !== undefined) input.max = String(rule.max);
  }
  let touched = false;
  input.addEventListener('blur', () => { touched = true; validateField(path, type); });
  input.addEventListener('input', () => { if (touched) validateField(path, type); });
  const range = rule.min !== undefined && rule.max !== undefined ? (rule.min + ' ~ ' + rule.max) : '';
  return el('label', { class: 'field' + (wide ? ' wide' : '') }, [
    el('span', { class: 'field-meta' }, [el('span', { class: 'label', text: label }), range ? el('span', { text: range }) : null]),
    input,
  ]);
}

/* 保存前全量校验：有问题就停在第一个出错的字段上，而不是把一段英文报错弹给用户 */
export function validateConfigForm() {
  let first = null;
  const check = (path, type) => {
    if (!validateField(path, type) && !first) first = path;
  };
  for (const group of GROUPS) {
    for (const [path, , type] of group.fields) check(path, type);
  }
  if (state.cfgPlatform) {
    check('platforms.' + state.cfgPlatform + '.max_pages', 'number');
    check('platforms.' + state.cfgPlatform + '.daily_limit', 'number');
  }
  if (!first) {
    for (const [low, high] of CROSS_RULES) {
      const cross = crossProblem(low);
      if (cross) { showFieldError(high, cross.message); first = first || high; }
    }
  }
  return first;
}

export function focusField(path) {
  const input = $('cfg::' + path);
  if (!input) return;
  input.scrollIntoView({ behavior: 'smooth', block: 'center' });
  setTimeout(() => input.focus({ preventScroll: true }), 300);
}


/* 时段：每一段是「从 [下拉] 到 [下拉]」，可以加多段。
   以前是手敲「08:00-22:00」的文本框：难看，还容易写错（写成 08:00-00:00 被当成一分钟都不开）。
   值仍然存成同一个 hidden input（逗号分隔），保存 / 校验走原来 'list' 的那条路。 */
const HALF_HOURS = Array.from({ length: 49 }, (_, i) => String(Math.floor(i / 2)).padStart(2, '0') + ':' + (i % 2 ? '30' : '00'));

function timeSelect(value, { end, label }) {
  const select = el('select', { class: 'select', 'aria-label': label });
  const options = HALF_HOURS.filter((t) => (end ? t !== '00:00' : t !== '24:00'));
  if (value && !options.includes(value)) options.push(value);          // 配置里写过的非整点 / 半点也照样显示
  options.sort();
  for (const t of options) select.appendChild(el('option', { value: t, text: t }));
  select.value = value || (end ? '22:00' : '08:00');
  return select;
}

function windowsField(path, label, value) {
  const hidden = el('input', { type: 'hidden', id: 'cfg::' + path });
  const list = el('div', { class: 'win-list' });
  const empty = el('div', { class: 'win-empty muted small', text: path === 'safety.active_windows' ? '不限时段：全天都可以开页面' : '没有设时段：全天都可以投递' });
  const addBtn = el('button', { class: 'link small', type: 'button', text: '+ 加一段' });
  const sync = () => {
    const parts = Array.from(list.querySelectorAll('.win-row')).map((row) => {
      const [a, b] = row.querySelectorAll('select');
      return a.value + '-' + b.value;
    });
    hidden.value = parts.join(',');
    empty.hidden = parts.length > 0;
    for (const row of list.querySelectorAll('.win-row')) {
      const [a, b] = row.querySelectorAll('select');
      const tip = row.querySelector('.win-tip');
      const s = a.value; const e = b.value === '00:00' ? '24:00' : b.value;
      tip.textContent = e === s ? '一分钟都不开' : (e < s ? '跨夜' : '');
      tip.title = e === s ? '开始和结束一样：这一段一分钟都不开' : (e < s ? '结束比开始早：跨过半夜，到第二天' : '');
      tip.className = 'win-tip small ' + (e === s ? 'bad' : 'muted');
    }
    hidden.dispatchEvent(new Event('input', { bubbles: true }));
  };
  const addRow = (start, finish) => {
    const a = timeSelect(start, { label: label + ' 开始' });
    const b = timeSelect(finish === '00:00' ? '24:00' : finish, { end: true, label: label + ' 结束' });
    const row = el('div', { class: 'win-row' }, [
      el('span', { class: 'muted small', text: '从' }), a, el('span', { class: 'muted small', text: '到' }), b,
      el('span', { class: 'win-tip small' }),
      el('button', { class: 'chip-x', type: 'button', 'aria-label': '删掉这一段', onclick: () => { row.remove(); sync(); } }, [icon('x')]),
    ]);
    a.addEventListener('change', sync); b.addEventListener('change', sync);
    list.appendChild(row);
  };
  for (const part of String(value || '').split(/[,，;；\s]+/).filter(Boolean)) {
    const m = /^(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})$/.exec(part);
    if (m) addRow(m[1].padStart(5, '0'), m[2].padStart(5, '0'));
  }
  addBtn.addEventListener('click', () => { addRow(null, null); sync(); });
  sync();
  return el('div', { class: 'field' }, [
    // 「加一段」放在标题行右边：字段高度和旁边的输入框对齐，不多占一行
    el('span', { class: 'field-meta' }, [el('span', { class: 'label', text: label }), addBtn]),
    hidden, list, empty,
  ]);
}


/* 月薪范围：「[最低] K 到 [最高] K」一行写完，空 = 不限；旁边实时写出按这个范围岗位池里会筛掉多少。
   以前是「薪资下限(千)」「薪资上限(千，0=不限)」两个分开的数字框，用户找不到、也不知道 0 是什么意思。
   两个 input 的 id 不变（cfg::profile.salary_min / salary_max），保存、交叉校验照旧。 */
function salaryField(label) {
  const prof = (state.config && state.config.profile) || {};
  const box = (path, value, placeholder) => {
    const input = el('input', { type: 'number', id: 'cfg::' + path, min: '0', step: '1', inputMode: 'numeric', placeholder });
    input.value = Number(value) > 0 ? String(value) : '';
    return input;
  };
  const low = box('profile.salary_min', prof.salary_min, '不限');
  const high = box('profile.salary_max', prof.salary_max, '不限');
  const preview = el('div', { class: 'hint' });
  let timer = null;
  const update = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const lo = Number(low.value) || 0; const hi = Number(high.value) || 0;
      if (hi && lo > hi) { preview.className = 'hint warn'; preview.textContent = '最高不能低于最低'; return; }
      if (!lo && !hi) { preview.className = 'hint'; preview.textContent = '不限薪资：所有岗位都进评分'; return; }
      const res = await get('/api/salary-preview?low=' + lo + '&high=' + hi);
      if (res.error) { preview.textContent = ''; return; }
      const parts = [];
      if (res.below) parts.push(res.below + ' 个低于 ' + lo + 'K');
      if (res.above) parts.push(res.above + ' 个高于 ' + hi + 'K');
      preview.className = 'hint';
      preview.textContent = '按现在岗位池的 ' + res.total + ' 个岗位算：' + res.inside + ' 个在范围内'
        + (parts.length ? '，' + parts.join('、') + '，这些在评分前直接筛掉' : '')
        + (res.unknown ? '；' + res.unknown + ' 个没写薪资 / 面议，照样进评分' : '');
    }, 250);
  };
  low.addEventListener('input', update); high.addEventListener('input', update);
  update();
  return el('div', { class: 'field wide' }, [
    el('span', { class: 'field-meta' }, [el('span', { class: 'label', text: label }),
      el('span', { text: '按月薪算，单位 K（千元）；年薪、日薪会自动折算' })]),
    el('div', { class: 'salary-row' }, [low, el('span', { class: 'muted', text: 'K 到' }), high, el('span', { class: 'muted', text: 'K' })]),
    preview,
  ]);
}
