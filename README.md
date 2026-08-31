# JobPilot

多平台自动岗位投递工具。基于真实 Chrome（CDP 桥接）驱动 51job / 猎聘 / 智联招聘，支持岗位采集 → AI 评分 → 自动/手动投递的完整流水线，带 Web 仪表盘。

[![License](https://img.shields.io/github/license/YouNianShao/jobpilot)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org)
[![Platforms](https://img.shields.io/badge/platforms-51job%20%7C%20liepin%20%7C%20zhaopin-green)](https://github.com/YouNianShao/jobpilot)

> ⚠️ **风险提示**：本工具仅限个人求职使用。主流招聘平台对自动化操作均有反爬风控（忽略点击 / 详情页 JS 封锁 / 会话级风控打标），自动投递随时可能失效——项目按"采集可用、投递尽量"设计，请以平台内手动投递为兜底，并自行评估账号风险。

## 功能

- **3 平台适配器**：前程无忧（51job）、猎聘（liepin）、智联招聘（zhaopin）
  - 51job / 猎聘：采集 + 自动投递（受反爬限制，可能失效）
  - 智联：仅采集（详情页 JS 会话级风控，自动投递不可靠），岗位池提供「去投递 ↗」直达链接手动投递
- **流水线**：`collect`（采集入库）→ `score`（DeepSeek AI 评分）→ `apply`（自动投递，按当日配额节流）→ `monitor`（HR 互动监测 + 建议回复）
- **Web 仪表盘**：工作台（统计卡片 / 运行控制 / 进度 / 日志）、岗位池（筛选 + 直达投递链接）、监测执行、配置，4-tab 单文件前端
- **安全护栏**：评分失败 / 投递被拦截红色横幅警示；协作式暂停；每日投递上限

## 快速开始

要求：Windows + Python 3.10+ + 已安装 Chrome

```bash
# 1. 克隆并安装依赖
git clone https://github.com/YouNianShao/jobpilot.git
cd jobpilot
python -m venv .venv
.venv\Scripts\activate
pip install -e .

# 2. 生成本地配置并填入 API Key
copy config.example.yaml config.yaml
#    编辑 config.yaml：ai.api_key 填 DeepSeek key；platforms.*.cities/keywords 按需修改

# 3. 启动（首次会自动拉起带远程调试端口的 Chrome，需登录各招聘平台）
launch.bat
```

浏览器访问 `http://127.0.0.1:8699` 打开仪表盘。

## 配置说明

见 `config.example.yaml`（已含全部字段注释）。核心项：

| 配置 | 说明 |
|---|---|
| `ai.api_key` | DeepSeek（或其他 OpenAI 兼容）API Key，评分使用 |
| `platforms.*.cities/keywords` | 各平台的城市 / 关键词（智联另有 `max_pages`） |
| `throttle.daily_limit` | 每日自动投递上限（默认 30） |
| `scoring.threshold` | 评分通过阈值（默认 60） |
| `profile.resume_path` | 简历 Markdown 路径，评分参考用 |

## 技术架构

```
src/jobpilot/
├── browser.py          # CDP 桥（浏览器自动化：new_tab/evaluate/click 等）
├── platforms/          # 平台适配器（BaseAdapter 抽象 collect/apply）
│   ├── job51.py        # 前程无忧
│   ├── liepin.py       # 猎聘
│   └── zhaopin.py      # 智联（collect_only）
├── ai/scorer.py        # DeepSeek 评分
├── pipeline.py         # 流水线编排 + 节流 + 协作式暂停
├── db.py               # SQLite 存储
├── monitor.py          # HR 互动监测
├── progress.py         # 运行状态持久化（run_status.json）
└── web/                # HTTP 服务 + 单文件仪表盘（dashboard.html）
```

## 开发与维护

Git 日常操作（提交 / 推送 / 迭代 / 回滚 / 安全红线）见 [GIT_WORKFLOW.md](./GIT_WORKFLOW.md)。

## 免责声明

- 自动投递依赖平台页面结构与反爬策略，**可能随时失效**；失效时请切换手动投递。
- 请遵守目标平台服务条款，控制频率，账号风险自负。
- 本项目仅用于个人求职场景，请勿用于批量营销或任何商业用途。

## 许可证

本项目采用 [MIT License](./LICENSE)。你可以自由使用、修改、分发（含商业用途），只需保留版权声明。

## 贡献

欢迎 Issue 与 PR！

- **提 Bug / 功能建议**：请使用仓库的 Issue 模板，提交前务必删除日志里的 API Key 等敏感信息。
- **提交代码**：请遵循 [PULL_REQUEST_TEMPLATE](./.github/PULL_REQUEST_TEMPLATE.md) 检查清单，确保不混入 `config.yaml` / `data/` / `chrome-profile/` / `*.log`。

日常 Git 操作见 [GIT_WORKFLOW.md](./GIT_WORKFLOW.md)。
