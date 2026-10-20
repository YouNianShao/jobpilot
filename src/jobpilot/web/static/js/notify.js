/* 任务结束提醒：采集 / 投递 / 全流程跑完时，不用一直盯着看板。

   - Windows 右下角的系统通知：写明结果（「全流程结束：新增 12 个岗位，3 个等你确认」）或没做成的原因
   - 响一声（浏览器自己合成的短提示音，不需要音频文件）
   - 网页标签标题变成「✅ 完成」/「⚠ 没做成」，切回看板就恢复
   前提是看板页面开着（最小化、切到别的标签都行）。系统通知要你允许一次：
   第一次点「采集 / 投递 / 全流程」时顺手问（浏览器只允许在点击里弹这个询问）。 */

import { toast } from './ui.js';

const BASE_TITLE = document.title;
let lastSeen = null;          // 上一次轮询看到的任务：Map(id → status)；null = 页面刚打开还没看过
let flashing = false;

export function askPermissionOnce() {
  if (!('Notification' in window) || Notification.permission !== 'default') return;
  try { Notification.requestPermission(); } catch (e) { /* 旧浏览器：不支持就算了 */ }
}

function beep(ok) {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const notes = ok ? [880, 1175] : [440, 330];        // 成功：上扬两声；没做成：下沉两声
    notes.forEach((freq, i) => {
      const osc = ctx.createOscillator(); const gain = ctx.createGain();
      osc.frequency.value = freq; osc.type = 'sine';
      const t = ctx.currentTime + i * 0.18;
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(0.25, t + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.16);
      osc.connect(gain).connect(ctx.destination); osc.start(t); osc.stop(t + 0.17);
    });
    setTimeout(() => ctx.close(), 800);
  } catch (e) { /* 没有声音设备就不响 */ }
}

function flashTitle(text) {
  document.title = text + ' · ' + BASE_TITLE;
  if (flashing) return;
  flashing = true;
  const restore = () => {
    if (document.hidden) return;
    document.title = BASE_TITLE; flashing = false;
    document.removeEventListener('visibilitychange', restore); window.removeEventListener('focus', restore);
  };
  document.addEventListener('visibilitychange', restore); window.addEventListener('focus', restore);
  if (!document.hidden && document.hasFocus()) setTimeout(restore, 6000);   // 正看着看板：几秒后自己恢复
}

/* 每次轮询把 /api/status 的任务列表传进来（可以同时跑好几个任务）：
   每个任务只在「刚才还在跑 → 现在结束了」那一下提醒一次，按任务 id 各记各的。
   刷新页面后第一次看到的任务只记下来、不提醒：它们早就结束了，不会补发。 */
export function watchTasks(tasks, describe) {
  const now = new Map();
  for (const task of tasks || []) if (task && task.id) now.set(task.id, task);
  const was = lastSeen;
  lastSeen = new Map([...now].map(([id, task]) => [id, task.status]));
  if (!was) return;
  for (const [id, task] of now) {
    if (was.get(id) === 'running' && task.status !== 'running') notifyDone(task, describe);
  }
}

/* 兼容旧调用：只有一个任务快照 */
export function watchTask(task, describe) { watchTasks(task ? [task] : [], describe); }

function notifyDone(task, describe) {
  const ok = task.status === 'completed' && !task.error;
  const stopped = task.status === 'stopped';
  const name = describe(task);
  const title = stopped ? name + '已停止' : (ok ? name + '完成' : name + '没有做成');
  const logs = task.logs || [];
  const body = (task.error || (logs.length ? logs[logs.length - 1] : '') || '').slice(0, 180);
  flashTitle(ok ? '✅ 完成' : (stopped ? '⏹ 已停止' : '⚠ 没做成'));
  if (!stopped) beep(ok);
  if ('Notification' in window && Notification.permission === 'granted') {
    try {
      // tag 带上任务 id：两个任务前后脚结束时，两条通知都留着，不会后一条顶掉前一条
      const note = new Notification('JobPilot · ' + title, { body, tag: 'jobpilot-task-' + task.id, requireInteraction: !ok });
      note.onclick = () => { window.focus(); note.close(); };
    } catch (e) { /* 通知失败不影响看板 */ }
  }
}

/* 遇到验证 / 掉登录：任务一停下就提醒，不等整个任务结束（status.attention，见 safety/attention.py）。
   只有你能处理的事才响：系统通知一直留着（requireInteraction），直到你点掉。
   按「平台 + 类型 + 时间」记：同一次验证每 2 秒轮询看到一次，只提醒一次；刷新页面后还挂着的也提醒一次，
   因为它还没处理 —— 和任务结束提醒不同，这个不怕补发。 */
const attentionSeen = new Set();
const ATTENTION_WHAT = { captcha: '需要你验证', login: '需要登录' };

export function watchAttention(items, nameOf) {
  for (const item of items || []) {
    const id = [item.platform, item.kind, item.at].join('|');
    if (attentionSeen.has(id)) continue;
    attentionSeen.add(id);
    const title = 'JobPilot · ' + nameOf(item.platform) + (ATTENTION_WHAT[item.kind] || '需要你处理');
    const body = '已在调试浏览器里打开那个页面，处理完回看板点「我已处理，继续」';
    flashTitle('⚠ ' + (ATTENTION_WHAT[item.kind] || '需要你处理'));
    beep(false);
    toast(title.replace('JobPilot · ', '') + '：' + body, 'warn');
    if ('Notification' in window && Notification.permission === 'granted') {
      try {
        // tag 按平台：同一个平台又出一次验证，新通知顶掉旧的那条，不堆一排
        const note = new Notification(title, { body, tag: 'jobpilot-attention-' + item.platform, requireInteraction: true });
        note.onclick = () => { window.focus(); note.close(); };
      } catch (e) { /* 通知失败不影响看板 */ }
    }
  }
}
