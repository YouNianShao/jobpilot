/* 多个视图共用的常量（岗位状态的中文名、筛选档位、投递状态）。 */

import { post } from './api.js';
import { toast } from './ui.js';

export const STATUS_LABEL = {
  new: '待评分', filtered: '未通过', pending_confirm: '待确认', approved: '已确认',
  applied: '已投递', rejected: '被拒', already: '已投过', failed: '失败',
  need_resume: '待补简历', unsupported: '不支持',
};

/* 岗位池的状态档位（'' = 全部），也是顶部下拉框的选项顺序 */
export const FILTERS = ['', 'pending_confirm', 'approved', 'applied', 'filtered', 'rejected', 'new'];

const APPLIED_STATUSES = new Set(['applied', 'already']);
export const isApplied = (job) => APPLIED_STATUSES.has(job.status);

/* 界面上不露内部代号：collect / boss 这种只给代码看 */
export const PLATFORM_NAME = { boss: 'BOSS直聘', '51job': '前程无忧', liepin: '猎聘', zhaopin: '智联招聘' };
export const platformName = (key) => PLATFORM_NAME[key] || key || '';
export const MODE_NAME = {
  collect: '采集', score: '评分', greet: '生成招呼语', confirm: '确认', apply: '投递',
  monitor: '监测', followup: '跟进', full: '全流程',
};
export const modeName = (key) => MODE_NAME[key] || key || '';

/* 岗位链接要在 JobPilot 的调试浏览器里开：用户的平台登录在那边，普通浏览器里是未登录状态。
   okMessage 让调用方换成自己的成功提示（例如顺带说招呼语已复制），避免弹两条。 */
export async function openJobUrl(url, okMessage) {
  if (!url) { toast('这个岗位没有原链接', 'err'); return false; }
  const res = await post('/api/browser/open', { url });
  if (res && res.ok) {
    toast(okMessage || '已在 JobPilot 的浏览器里打开（登录状态在那边）', 'ok');
    return true;
  }
  if (res && res.not_running) {
    toast(res.error || '调试浏览器没开：先点左下角「启动浏览器」', 'warn');
  } else {
    toast((res && res.error) || '打开失败', 'err');
  }
  return false;
}
