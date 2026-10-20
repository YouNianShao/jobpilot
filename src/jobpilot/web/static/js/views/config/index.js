/* 配置页主流程：加载、渲染、保存、平台切换、未保存提示、数据与备份。
   由 app.js 在第一次打开配置页时动态 import（见 app.js 的「配置」一节）。
   这个目录不 import app.js：要用的几个共用动作由 initConfig(ctx) 传进来。 */

import { get, post } from '../../api.js';
import { $, state } from '../../store.js';
import { alertDialog, clear, confirmDialog, el, icon, toast } from '../../ui.js';
import { GROUPS, configField, focusField, pick, put, readNatures, showFieldError, splitList, validateConfigForm } from './fields.js';
import { cityPicker, keywordChips } from './city-picker.js';
import { resumeGroup } from './resume.js';

let ctx = null;

/* app.js 第一次打开配置页时调用：接上共用动作，绑定配置页自己的按钮（以前在 app.js 的 init 里） */
export function initConfig(context) {
  ctx = context;
  $('btnSaveConfig').addEventListener('click', saveConfig);
  // 改了没存要看得见：保存条吸底以后，这句提示就挂在按钮旁边
  $('configForm').addEventListener('input', (event) => {
    if (event.target && event.target.closest && event.target.closest('#cfg-resume')) return;
    $('saveMsg').textContent = '有未保存的修改';
    $('saveMsg').classList.add('is-dirty');
    $('saveMsg').classList.remove('is-bad');
  });
  $('btnReloadConfig').addEventListener('click', loadConfig);
}

// app.js 里的共用动作（通过 initConfig 传进来）
const refresh = () => ctx.refresh();
const renderRisk = (status) => ctx.renderRisk(status);

export async function loadConfig() {
  // 第一次打开配置页时跟顶栏选的平台走；之后在这里切平台不会改到顶栏（顶栏管的是采集 / 投递用哪个平台）
  if (!state.cfgPlatform) state.cfgPlatform = state.platform;
  const config = await get('/api/config');
  const cities = await get('/api/cities?platform=' + encodeURIComponent(state.cfgPlatform || ''));
  const resume = await get('/api/resume');
  state.config = config;
  state.cities = cities && !cities.error ? cities : null;   // 城市目录用于校验输入
  state.resume = resume && !resume.error ? resume : null;   // 简历状态：评分/招呼语都依赖它
  renderConfig();
  renderRisk(state.status || { platforms: {} });   // 额度上限跟着配置刷新
}

/* 数据与备份：整个项目的价值都在 data/ 里，得能一把拿走 */
function backupGroup() {
  const group = el('div', { class: 'fgroup' });
  group.appendChild(el('div', { class: 'fgroup-head' }, [
    icon('stack'), el('h3', { text: '数据与备份' }), el('div', { class: 'grow' }),
  ]));
  const body = el('div', { class: 'fgroup-body' });
  body.appendChild(el('div', {
    class: 'fgroup-note',
    text: '备份内容：岗位库（在线一致性快照，不是直接拷文件）、config.yaml、已生成的定制简历，外加一份 manifest。'
      + '恢复方式：停掉看板 → 解压覆盖项目里的同名文件 → 重新启动。',
  }));
  body.appendChild(el('div', { class: 'actions' }, [
    el('a', { class: 'btn small', href: '/api/backup', download: '', text: '下载备份（zip）' }),
  ]));
  group.appendChild(body);
  return group;
}

/* 配置页里的平台切换：直接点平台名切换（以前只能去右上角的下拉框切） */
function platformTabs() {
  const tabs = el('div', { class: 'plat-tabs', role: 'tablist', 'aria-label': '选择要配置的平台' });
  const platforms = (state.status && state.status.platforms) || {};
  const cfgPlatforms = (state.config && state.config.platforms) || {};
  for (const key of Object.keys(platforms)) {
    const on = !!(cfgPlatforms[key] || {}).enabled;
    tabs.appendChild(el('button', {
      class: 'plat-tab' + (key === state.cfgPlatform ? ' is-active' : ''), type: 'button', role: 'tab',
      'aria-selected': key === state.cfgPlatform ? 'true' : 'false', title: on ? '已启用' : '未启用',
      onclick: () => switchConfigPlatform(key),
    }, [el('span', { class: 'status-dot ' + (on ? 'on' : 'off') }), platforms[key].name || key]));
  }
  return tabs;
}

async function switchConfigPlatform(key) {
  if (!key || key === state.cfgPlatform) return;
  if ($('saveMsg').classList.contains('is-dirty')) {
    const go = await confirmDialog('切换平台', '这一页有还没保存的修改，切换平台会丢掉它们。', { okText: '仍然切换', kind: 'warn' });
    if (!go) return;
  }
  state.cfgPlatform = key;
  await loadConfig();
}

function renderConfig() {
  const wrap = $('configForm');
  clear(wrap);
  const form = el('div', { class: 'form' });
  const nav = $('cfgNav');
  if (nav) clear(nav);
  let groupSeq = 0;
  const navItem = (label, id) => {
    if (!nav) return;
    nav.appendChild(el('a', {
      class: 'cfg-nav-item', href: '#' + id, text: label,
      onclick: (event) => {
        event.preventDefault();
        const node = document.getElementById(id);
        if (node) node.scrollIntoView({ behavior: 'smooth', block: 'start' });
      },
    }));
  };
  const cfg = state.config || {};

  const addGroup = (title, iconName, note, fields, switches, danger) => {
    const group = el('div', { class: 'fgroup' + (danger ? ' danger' : '') });
    const anchor = 'cfg-g' + (groupSeq++);
    group.id = anchor;
    navItem(title, anchor);
    group.appendChild(el('div', { class: 'fgroup-head' }, [
      icon(iconName), el('h3', { text: title }), el('div', { class: 'grow' }),
    ]));
    // 对齐：短字段进固定列宽的网格、长文本整行、开关单独一行（以前全塞一个 auto-fit 网格，
    // 开关比输入框矮一截，长短字段挤在一行，左右边都对不齐）
    const body = el('div', { class: 'fgroup-body' });
    const shortRow = el('div', { class: 'frow-fields' });
    const wideRow = el('div', { class: 'frow-wide' });
    for (const [path, label, type, wide] of fields) {
      const value = pick(cfg, path);
      const shown = (type === 'list' || type === 'windows') ? (value || []).join(',') : (value === undefined || value === null ? '' : value);
      // windows-str：后端存的是一个字符串（"08:00-12:00,14:00-22:00"），界面和 windows 一样用下拉
      (wide ? wideRow : shortRow).appendChild(configField(path, label, type, shown, wide));
    }
    if (shortRow.children.length) body.appendChild(shortRow);
    if (wideRow.children.length) body.appendChild(wideRow);
    if ((switches || []).length) {
      const switchRow = el('div', { class: 'frow-switches' });
      for (const [path, label] of switches) {
        const input = el('input', { type: 'checkbox', id: 'cfg::' + path, role: 'switch' });
        input.checked = !!pick(cfg, path);
        switchRow.appendChild(el('label', { class: 'field switch' }, [input, label]));
      }
      body.appendChild(switchRow);
    }
    if (note) body.appendChild(el('div', { class: 'fgroup-note', text: note }));
    if (fields.some(([path]) => path === 'ai.provider')) body.appendChild(aiTestRow());
    group.appendChild(body);
    form.appendChild(group);
  };

  const resumeBlock = resumeGroup({ loadConfig, refresh });
  resumeBlock.id = 'cfg-resume';
  navItem('简历正文', 'cfg-resume');
  form.appendChild(resumeBlock);

  for (const group of GROUPS) {
    addGroup(group.title, group.icon, group.note, group.fields, group.switches, group.danger);
  }

  const key = state.cfgPlatform;
  if (key) {
    const plat = (cfg.platforms || {})[key] || {};
    const group = el('div', { class: 'fgroup', id: 'cfg-platform' });
    // 平台直接在这里切（以前要去右上角的下拉框切，配置页只有一句"右上角切换平台可改其它平台"）
    group.appendChild(el('div', { class: 'fgroup-head' }, [
      icon('globe'),
      el('h3', { text: '平台' }),
      el('div', { class: 'grow' }),
      platformTabs(),
    ]));
    const body = el('div', { class: 'fgroup-body' });

    const switchRow = el('div', { class: 'frow-switches' });
    for (const [id, label, checked] of [
      ['enabled', '启用该平台', plat.enabled],
      ['apply', '允许对该平台投递', plat.apply],
      ['fetch_detail', '打开详情页抓 JD', plat.fetch_detail],
    ]) {
      const input = el('input', { type: 'checkbox', id: 'cfg::platform.' + id, role: 'switch' });
      input.checked = !!checked;
      switchRow.appendChild(el('label', { class: 'field switch' }, [input, label]));
    }
    body.appendChild(switchRow);

    const shortRow = el('div', { class: 'frow-fields' });
    shortRow.appendChild(configField('platforms.' + key + '.max_pages', '页数上限（智联是"滚动批数"）', 'number', plat.max_pages ?? 3));
    // 每个平台自己的每日投递上限：留空 = 只受「投递」分组里的全局上限（所有平台合计）约束
    const capField = configField('platforms.' + key + '.daily_limit', '这个平台每天最多投几个（留空 = 不单独限制）', 'number', plat.daily_limit ?? '');
    capField.querySelector('input').placeholder = '不单独限制';
    shortRow.appendChild(capField);
    body.appendChild(shortRow);

    const wideRow = el('div', { class: 'frow-wide' });
    wideRow.appendChild(el('div', { class: 'field wide' }, [
      el('span', { class: 'label', text: '城市' }),
      cityPicker(plat.cities || [], state.cities, plat.name || key),
    ]));
    wideRow.appendChild(el('div', { class: 'field wide' }, [
      el('span', { class: 'label', text: '关键词' }),
      keywordChips('cfg::platform.keywords', plat.keywords || []),
    ]));
    body.appendChild(wideRow);

    // 关键词质量直接决定你看到什么岗位 —— 给一个"看简历推荐"的入口，并附上实际效果佐证
    const kwBox = el('div', { class: 'kw-suggest' });
    const kwBtn = el('button', {
      class: 'btn ghost small', text: '让 AI 看简历推荐关键词',
      onclick: async () => {
        kwBtn.disabled = true;
        kwBtn.textContent = '正在看简历和已采数据…';
        const res = await post('/api/suggest-keywords', {});
        kwBtn.disabled = false;
        kwBtn.textContent = '让 AI 看简历推荐关键词';
        if (res.error) { toast(res.error, 'err'); return; }
        clear(kwBox);
        const add = (word) => {
          const input = $('cfg::platform.keywords');
          if (splitList(input.value).includes(word)) { toast('「' + word + '」已经在关键词里了', 'ok'); return; }
          input.dispatchEvent(new CustomEvent('chip-add', { detail: word }));
          toast('已加入关键词「' + word + '」（记得点保存配置）', 'ok');
        };
        if ((res.keywords || []).length) {
          kwBox.appendChild(el('div', { class: 'muted small', text: '建议加入（点「加入」就变成上面的一个标签）：' }));
          for (const row of res.keywords) {
            kwBox.appendChild(el('div', { class: 'kw-row' }, [
              el('span', { class: 'pill ok', text: row.text }),
              el('span', { class: 'kw-reason', text: row.reason || '' }),
              el('button', { class: 'btn ghost small', text: '加入', onclick: () => add(row.text) }),
            ]));
          }
        }
        if ((res.avoid || []).length) {
          kwBox.appendChild(el('div', { class: 'muted small', style: 'margin-top:8px', text: '建议避开（或在浪费时间）：' }));
          for (const row of res.avoid) {
            kwBox.appendChild(el('div', { class: 'kw-row' }, [
              el('span', { class: 'pill bad', text: row.text }),
              el('span', { class: 'kw-reason', text: row.reason || '' }),
            ]));
          }
        }
        kwBox.appendChild(el('div', {
          class: 'hint', style: 'margin-top:6px',
          text: '依据：你的简历 + 你已采岗位的评分 + 这些词的实际平均分（' + (res.model || '') + '）',
        }));
      },
    });
    body.appendChild(el('div', { class: 'field wide' }, [kwBtn, kwBox]));

    body.appendChild(el('div', {
      class: 'fgroup-note',
      text: '抓 JD 会让 AI 评分更准，但每个岗位要多开一次详情页（占当日详情页预算）。'
        + 'BOSS 的薪资和 JD 只在详情页里，所以默认开；关掉后 BOSS 岗位只剩标题 / 公司 / 城市。'
        + '自动投递目前只有 BOSS 实机核验过：其它平台打开「允许对该平台投递」也不会自动投，'
        + '概览「风控仪表」里每个平台名字旁边写着它现在能做什么。',
    }));
    group.appendChild(body);
    form.appendChild(group);
    navItem('平台 · ' + (((state.status && state.status.platforms) || {})[key] || {}).name || '平台', 'cfg-platform');
  }

  const backupBlock = backupGroup();
  backupBlock.id = 'cfg-backup';
  navItem('数据与备份', 'cfg-backup');
  form.appendChild(backupBlock);
  wrap.appendChild(form);
  // 滚动高亮：配置页很长，左侧导航得告诉用户现在在哪一节
  if (nav && typeof IntersectionObserver === 'function') {
    if (state.cfgObserver) state.cfgObserver.disconnect();
    state.cfgObserver = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const target = '#' + entry.target.id;
        for (const item of nav.querySelectorAll('.cfg-nav-item')) {
          item.classList.toggle('is-active', item.getAttribute('href') === target);
        }
      }
    }, { rootMargin: '-70px 0px -65% 0px' });
    for (const node of form.querySelectorAll('[id^=cfg-]')) state.cfgObserver.observe(node);
  }
  $('configHint').textContent = '';
  // 表单是按服务端的值重画的：之前的「有未保存的修改」已经不成立了（放弃修改 / 切平台都会走到这里）
  if ($('saveMsg').classList.contains('is-dirty') || $('saveMsg').classList.contains('is-bad')) {
    $('saveMsg').classList.remove('is-dirty', 'is-bad');
    $('saveMsg').textContent = '';
  }
}

/* 测试连接：用当前填的值发一次极小的请求（不保存）。Key 没改过时后端用已保存的那个 */
function aiTestRow() {
  const msg = el('span', { class: 'muted small', 'aria-live': 'polite' });
  const btn = el('button', { class: 'btn small', type: 'button', text: '测试连接' });
  btn.addEventListener('click', async () => {
    const body = {};
    for (const key of ['provider', 'base_url', 'model', 'api_key', 'timeout_seconds']) {
      const input = $('cfg::ai.' + key);
      if (input) body[key] = key === 'timeout_seconds' ? Number(input.value) : input.value;
    }
    btn.disabled = true;
    msg.className = 'muted small';
    msg.textContent = '正在连接…';
    const res = await post('/api/ai/test', body);
    btn.disabled = false;
    if (res.ok) {
      msg.className = 'small ok';
      msg.textContent = '已连通：' + (res.model || '') + ' · ' + ((res.latency_ms || 0) / 1000).toFixed(1) + ' 秒（还没保存的话记得保存）';
    } else {
      msg.className = 'small bad';
      msg.textContent = '没连通：' + (res.error || '未知错误') + (res.hint ? '。' + res.hint : '');
    }
  });
  return el('div', { class: 'frow-actions' }, [btn, msg]);
}

/* 保存失败时别把 safety.daily_pages.apply 这种路径丢给用户：换成配置页上的中文名 */
function fieldLabel(path) {
  for (const group of GROUPS) {
    for (const [p, label] of group.fields) if (p === path) return label;
    for (const [p, label] of (group.switches || [])) if (p === path) return label;
  }
  return '某项配置';
}

async function saveConfig() {
  const invalid = validateConfigForm();
  if (invalid) {
    const count = document.querySelectorAll('#configForm .field.is-invalid').length;
    $('saveMsg').textContent = '有 ' + count + ' 项需要修改';
    $('saveMsg').classList.add('is-bad', 'is-dirty');
    focusField(invalid);
    return;
  }
  const payload = {};
  for (const group of GROUPS) {
    for (const [path, , type] of group.fields) {
      const input = $('cfg::' + path);
      if (!input) continue;
      if (type === 'natures') {
        put(payload, path, readNatures(path));
      } else if (type === 'salary') {
        // 空 = 不限 = 0（后端的约定）；两个框一起存
        put(payload, 'profile.salary_min', Number($('cfg::profile.salary_min').value) || 0);
        put(payload, 'profile.salary_max', Number($('cfg::profile.salary_max').value) || 0);
      } else if (type === 'number' || type === 'interval') {
        put(payload, path, Number(input.value));
      } else if (type === 'windows-str') {
        put(payload, path, splitList(input.value).join(','));
      } else if (type === 'list' || type === 'windows') {
        put(payload, path, splitList(input.value));
      } else {
        if (path === 'ai.api_key' && String(input.value).includes('*')) continue;   // 掩码不回写
        put(payload, path, input.value);
      }
    }
    for (const [path] of (group.switches || [])) {
      const input = $('cfg::' + path);
      if (input) put(payload, path, input.checked);
    }
  }
  if (state.cfgPlatform && $('cfg::platform.enabled')) {
    // 这一份要带上平台的全部字段：后端按 section 一层覆盖，少带一个键就会被抹掉
    put(payload, 'platforms.' + state.cfgPlatform, {
      enabled: $('cfg::platform.enabled').checked,
      apply: $('cfg::platform.apply').checked,
      fetch_detail: $('cfg::platform.fetch_detail').checked,
      max_pages: Number($('cfg::platforms.' + state.cfgPlatform + '.max_pages').value),
      // 空 = 不单独限制（null）；后端按 section 整段覆盖，这个键不带上，保存一次就会被抹掉
      daily_limit: (() => {
        const raw = String(($('cfg::platforms.' + state.cfgPlatform + '.daily_limit') || {}).value || '').trim();
        return raw === '' ? null : Number(raw);
      })(),
      cities: splitList(($('cfg::platform.cities') || {}).value),
      keywords: splitList($('cfg::platform.keywords').value),
    });
  }
  const res = await post('/api/config', payload);
  if (res.error) {
    // 后端按字段给了原因：标到对应输入框下面，焦点停在第一个
    const fields = res.fields || {};
    const paths = Object.keys(fields);
    for (const path of paths) showFieldError(path, fields[path]);
    $('saveMsg').textContent = '保存失败';
    $('saveMsg').classList.add('is-bad', 'is-dirty');
    const shown = paths.filter((path) => $('cfg::' + path));
    if (shown.length) focusField(shown[0]);
    else await alertDialog('保存失败', res.error + (paths.length ? '\n\n' + paths.map((p) => fieldLabel(p) + '：' + fields[p]).join('\n') : ''));
    return;
  }
  toast('配置已保存', 'ok');
  $('saveMsg').classList.remove('is-dirty', 'is-bad');
  $('saveMsg').textContent = '已保存';
  setTimeout(() => { if (!$('saveMsg').classList.contains('is-dirty')) $('saveMsg').textContent = ''; }, 1800);
  await loadConfig();
  await refresh();
}
