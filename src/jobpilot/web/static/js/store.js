/* 全局共享状态：app.js 和各视图（views/）都从这里读写同一个对象。
   只放「多个视图都要读」的东西；某个视图自己的状态留在它自己的文件里（比如岗位池的分页、勾选）。 */

export const state = {
  view: 'overview',
  platform: '',          // 顶栏：采集 / 投递用哪个平台
  cfgPlatform: '',       // 配置页正在编辑哪个平台（和顶栏互不影响）
  status: null,
  interactions: [],
  config: null,
  theme: localStorage.getItem('jp-theme') || 'dark',
  pollMs: 2000,
  nextOpen: {},
  riskOpen: {},
  trend: null,
  trendAt: 0,
};

export const $ = (id) => document.getElementById(id);
