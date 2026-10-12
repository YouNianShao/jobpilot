# 08 · Agent 时代架构

> 本文回答一个问题：**模型只会越来越强，架构怎么才能不跟着重写？**
> 答案不是"接个 Agent 框架"，而是：**把副作用锁进一个安全内核，把判断权交给模型，把控制流留给确定性代码。**

---

## 1. 四条设计哲学

### 1.1 AI 只做判断，不做流程
模型输出**结构化判断**（这个岗位多少分 / 这条招呼语怎么写 / 这个 HR 消息什么意图）。
控制流、状态机、IO、限额**永远是确定性代码**。

> 好处：模型从 GPT-4 换到未来的更强模型，只是"判断更准"，**不可能绕过任何约束**。
> 这是本架构能扛住模型演进的根本原因。

### 1.2 副作用必须穿过唯一闸门（安全内核）
任何会改变外部世界的动作——开页面、点按钮、发消息——**只能**通过 `Policy.authorize(action)` 获得许可。
驱动（Driver）拿不到"直接开页面"的能力，它只能**申请**。

```
错误姿势：driver.open(url)                      ← 谁都能开页面，风控靠自觉
正确姿势：permit = policy.authorize(page_open)  ← 唯一闸门，风控是强制的
          if permit: page.goto(url)
```

这在安全工程里叫**安全内核（security kernel）**：所有访问都必须经过一个可验证的、不可绕过的检查点。

### 1.3 协议优于继承，可测试性优先
`BrowserDriver` / `PlatformDriver` / `LlmClient` 全是 `Protocol`。
真实实现之外永远有 `Fake` 实现，因此**90% 的逻辑不需要 Chrome、不需要 AI Key** 就能测。

### 1.4 一切可观测、可回放
每个副作用落 `events` 表（含 `trace_id`）；每次模型调用记 token 与耗时。
任何一个岗位的完整历史都能从库里还原成一条时间线。

---

## 2. 分层

```
┌──────────────────────────────────────────────────────────────────┐
│ 接口层    CLI  ·  Web API(FastAPI)  ·  MCP Server(给外部 Agent 用) │
├──────────────────────────────────────────────────────────────────┤
│ 编排层    TaskRunner：单任务互斥、可暂停、可停止、可续跑             │
├──────────────────────────────────────────────────────────────────┤
│ 推理层    LlmClient：供应商无关 / 结构化输出 / 提示词版本化 / 计费    │
├──────────────────────────────────────────────────────────────────┤
│ 能力层    PlatformDriver(collect/apply/monitor) + Registry        │
├──────────────────────────────────────────────────────────────────┤
│ ★ Policy ★  安全内核：预算 · 冷却锁 · 时间窗 · 退避 · 风控识别 · 确认 │
├──────────────────────────────────────────────────────────────────┤
│ 世界层    BrowserDriver(CDP) · HTTP · SQLite                      │
└──────────────────────────────────────────────────────────────────┘
```

**依赖方向永远向下**：推理层不认识浏览器，浏览器不认识模型。换任何一层都不动其它层。

---

## 3. ★ 安全内核（风控，重中之重）

风控不是一个"模块"，而是**所有副作用的唯一通道**。

### 3.1 闸门检查顺序

```
authorize(action)
 ├─ 0. 平台冷却锁      命中验证码/限流/封禁 → 该平台整体拒绝（按平台隔离，不连坐）
 ├─ 1. 时间窗          仅对 apply/send：不在 send_windows 内 → 拒绝
 ├─ 2. 随机休息日      仅对 apply/send：按概率整天拒绝
 ├─ 3. 每日投递上限    apply_one：今天成功数 ≥ daily_limit → 拒绝
 ├─ 4. 采集→投递冷却    apply_one：刚采完的一段时间内不发消息 → 拒绝
├─ 5. 活跃时段        page_open：不在 active_windows 内 → 拒绝（半夜刷招聘是机器特征）
 ├─ 6. 速率窗口        page_open：滚动 10 分钟 / 1 小时上限；能等就等，等太久 → 拒绝
 ├─ 7. 页面预算        page_open：按 平台+阶段+本地日期 计数，超限 → 拒绝
 ├─ 8. 开页面节流      page_open：两次开页面之间强制随机间隔（阻塞式等待）
 └─ 9. 放行            记账 → 返回 Permit（附剩余额度与等待时长，供界面展示）
```

### 3.2 三条铁律

1. **fail-closed**：任何不确定（页面结构变了、出现未知风控页）→ 停止，而不是"再试一次"。
2. **按平台隔离**：51job 被锁，不影响 BOSS/猎聘/智联。
3. **重复投递比漏投更危险**：投递前必须查状态与事件双重确认。

### 3.3 为什么把预算放在闸门而不是驱动里
预算必须是**账号级**的：采集、投递、监测都会开页面，账号风控看的是**总量**。
只要有一个驱动绕过记账，整个预算就失效。所以它不是"约定"，是**强制**。

---

## 4. 工具面：一套工具，三种驱动者

把能力暴露成**类型化工具**（`search_jobs` / `score_jobs` / `draft_greeting` / `apply_job` / `check_replies`…）：

| 驱动者 | 今天 | 未来 |
|---|---|---|
| 人 | CLI / Web 按钮 | 同左 |
| 脚本 | 定时任务 | 同左 |
| **外部 Agent（MCP）** | —— | 同一个工具面，**同一个 Policy 闸门** |

> 关键：**工具面不随驱动者改变**。所以"以后接 MCP 让 Claude 直接驱动"不需要重写业务逻辑，
> 加一层 MCP 适配即可——而且它**照样过不了风控闸门**。

---

## 5. 自治等级：模型越强，你可以调得越高

配置 `autonomy.level`：

| 等级 | 行为 | 谁做决定 | 谁负责副作用 |
|---|---|---|---|
| **L0 手动** | 只采集，人工投递 | 人 | 人 |
| **L1 建议**（默认） | AI 打分 + 写招呼语，**人工确认**后投递 | AI 建议 | 人确认后系统执行 |
| **L2 半自动** | AI 决定投哪些（≥阈值），人工确认发送 | AI | 人确认后系统执行 |
| **L3 全自动** | AI 决定并发送 | AI | 系统执行（**仍受 Policy 约束**） |

**升到 L3 不等于关掉风控**：Policy 闸门永远在。等级只决定"是否需要人点一下确认"。

---

## 6. 推理层：可插拔、可版本化

```
LlmClient (Protocol)
 ├─ OpenAiCompatClient   DeepSeek / 豆包 / 通义千问 / 智谱 / Kimi / 硅基流动 / OpenAI / Gemini（400 点名 temperature 时自动去掉重发）
 ├─ AnthropicClient      Claude 原生 Messages API（system 顶层、max_tokens 必填、不发 temperature、只取 text 块）
 ├─ build_llm(cfg.ai)    按 ai/providers.py 的协议选客户端
 └─ FakeLlm              测试用

complete(system, user, schema) -> dict      # 结构化输出，schema 校验
```

- **结构化输出**：要 JSON 就给 JSON Schema，解析失败**不猜**，直接算失败并记录
- **提示词版本化**：`prompts/scoring.v3.md`，每次结果记 `prompt_version`，可回放对比
- **成本可观测**：每次调用记 `tokens_in/tokens_out/latency/model`
- **可插拔**：换模型只改配置；判断标准变化只改提示词文件

---

## 7. 目录

```
src/jobpilot/
  models.py            领域模型 + 错误类型（无依赖）
  config.py            pydantic 配置（配置即契约）
  db.py                SQLite + 迁移（唯一持久化出口）
  safety/
    policy.py          ★ 安全内核：唯一副作用闸门
    budget.py          页面预算（被 policy 调用）
    locks.py           按平台冷却锁
    throttle.py        时间窗 / 随机休息 / 退避 / 抖动
    risk.py            风控信号识别（URL + 正文）
  browser/
    driver.py          Page / BrowserDriver 协议
    patchright_driver.py  真实实现（连接已登录 Chrome）
    fake.py            测试实现
  platforms/
    base.py            PlatformDriver 协议 + DriverContext
    registry.py        平台注册表
    boss.py            已实机核验（collect）
  ai/
    llm.py             LlmClient 协议 + OpenAI 兼容实现 + FakeLlm
    prompts/           版本化提示词
  services/
    collector.py scorer.py greeter.py confirmer.py applier.py monitor.py
  web/app.py           FastAPI
  cli.py
  mcp/server.py        （未来）把工具面暴露给外部 Agent
```

---

## 8. 这套架构怎么扛住模型变强

| 模型变强带来的变化 | 架构的反应 |
|---|---|
| 判断更准 | 只影响 `ai/` 与提示词，不动流程 |
| 能做更长任务 | 编排层已有 Task + 事件流，可加"计划-执行"而不改协议 |
| 想让它自主操作 | 加 `mcp/server.py`，复用同一工具面与同一 Policy |
| 多模态（看图/看视频判断岗位） | 只扩 `LlmClient`，驱动层不变 |
| 想换供应商 | 改配置 |

**唯一不变的是：副作用必须过闸门。** 这就是这套架构的"抗时间性"。