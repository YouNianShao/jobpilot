/* 任务结束提醒：采集 / 投递 / 全流程跑完时，不用一直盯着看板。

   - Windows 右下角的系统通知：写明结果（「全流程结束：新增 12 个岗位，3 个等你确认」）或没做成的原因
   - 响一声（浏览器自己合成的短提示音，不需要音频文件）
   - 网页标签标题变成「✅ 完成」/「⚠ 没做成」，切回看板就恢复
   前提是看板页面开着（最小化、切到别的标签都行）。系统通知要你允许一次：
   第一次点「采集 / 投递 / 全流程」时顺手问（浏览器只允许在点击里弹这个询问）。 */

const BASE_TITLE = document.title;
let lastSeen = null;           // 上一次轮询看到的任务：{ id, status }
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

/* 每次轮询把 /api/status 的任务快照传进来：只在「刚才还在跑 → 现在结束了」那一下提醒一次。
   刷新页面时任务早就结束了，不会补发提醒。 */
export function watchTask(task, describe) {
  const now = task ? { id: task.id, status: task.status } : null;
  const was = lastSeen;
  lastSeen = now;
  if (!was || !now || was.id !== now.id || was.status !== 'running' || now.status === 'running') return;
  const ok = now.status === 'completed' && !task.error;
  const stopped = now.status === 'stopped';
  const name = describe(task);
  const title = stopped ? name + '已停止' : (ok ? name + '完成' : name + '没有做成');
  const logs = task.logs || [];
  const body = (task.error || (logs.length ? logs[logs.length - 1] : '') || '').slice(0, 180);
  flashTitle(ok ? '✅ 完成' : (stopped ? '⏹ 已停止' : '⚠ 没做成'));
  if (!stopped) beep(ok);
  if ('Notification' in window && Notification.permission === 'granted') {
    try {
      const note = new Notification('JobPilot · ' + title, { body, tag: 'jobpilot-task', requireInteraction: !ok });
      note.onclick = () => { window.focus(); note.close(); };
    } catch (e) { /* 通知失败不影响看板 */ }
  }
}
