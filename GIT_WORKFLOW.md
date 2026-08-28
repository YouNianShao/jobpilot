# GitHub 使用与维护速查（JobPilot）

> 仓库地址：https://github.com/YouNianShao/jobpilot （私有仓库，只有你能看到）
> 本地目录：`E:\JobPilot`

---

## 1. 三个概念（懂这几个就够了）

| 名词 | 是什么 | 类比 |
|---|---|---|
| commit（提交） | 一次代码存档 | 游戏存档点 |
| push（推送） | 把本地存档同步到 GitHub | 存档上传云端 |
| branch（分支） | 一条独立的开发线 | 平行世界 |

流程永远是四步，顺序不能乱：

```
改文件  →  git add  →  git commit  →  git push
                       (存本地)      (传云端)
```

---

## 2. 日常四连（99% 的时间只用这几条）

在 `E:\JobPilot` 目录下打开终端：

```bash
git status             # 看看改了哪些文件（红=未暂存，绿=已暂存）
git add .              # 把所有改动放进待提交区
git commit -m "说明"    # 存档，说明写清楚做了什么
git push               # 同步到 GitHub
```

**建议节奏**：一个小功能 = 一次 commit；每天收工 = 一次 push。
别攒一个月才推一次，那样出问题没法回溯。

**commit 说明的写法**（业界惯例，一看就懂）：

```bash
git commit -m "feat: 新增猎聘自动打招呼"
git commit -m "fix: 修复评分失败岗位卡死"
git commit -m "docs: 更新 README"
git commit -m "refactor: 重构适配器基类"
```

前缀含义：`feat` 新功能 / `fix` 修 bug / `docs` 文档 / `refactor` 重构 / `chore` 杂项。

---

## 3. 怎么迭代（开发新功能）

### 简单做法（一个人开发，推荐现在用）

直接在 `main` 分支上改，改完 push。够用，别过度设计。

### 规范做法（改动较大、怕改坏主线的）

```bash
git checkout -b feat/新功能名     # 开一条独立分支
# ... 改代码，然后 add + commit ...
git checkout main                 # 切回主线
git merge feat/新功能名            # 把成果合并进来
git push
git branch -d feat/新功能名        # 删掉用完的分支
```

好处：分支改崩了直接删掉，主线毫发无损。

### 打版本标记（里程碑）

```bash
git tag -a v1.0 -m "第一个稳定版本"
git push --tags
```

之后 GitHub 网页右侧会出现 **Releases**，可以写版本说明、上传打包文件。

---

## 4. 安全红线（重要，务必看）

`.gitignore` 已经拦截了下面这些，**它们永远不能提交**：

| 文件/目录 | 里面有什么 | 泄露后果 |
|---|---|---|
| `config.yaml` | DeepSeek API Key | 被人盗刷额度 |
| `data/` | 简历、投递记录、HR 对话 | 隐私全曝光 |
| `chrome-profile/` | 招聘网站登录态 | 账号被盗 |
| `.venv/` | 虚拟环境 | 几个 G 的无用文件 |

**每次 push 前用 `git status` 扫一眼**，确认这些没出现在列表里。

万一手滑提交了：

```bash
git rm --cached config.yaml        # 从版本库移除（文件保留在本地）
git commit -m "chore: 移除误提交的敏感文件"
git push
```

⚠️ 但这只删掉了最新版本，**历史记录里还有**。所以：一旦提交过真实密钥，**立刻去改掉那个密钥**，别指望删除文件能救。

---

## 5. 出问题怎么回滚

```bash
git diff                     # 看还没 add 的改动
git checkout -- 文件名        # 丢弃某个文件的改动（不可恢复，慎用）
git log --oneline            # 看历史，每行开头的 7 位是提交号
git reset --soft HEAD~1      # 撤销最近一次 commit，改动还在工作区
git revert <提交号>           # 生成一个"反向提交"来撤销 —— 已推送的改动用这个，安全
```

**区别**：`reset` 是抹掉历史（已 push 的不要用），`revert` 是新增一次"取消操作"的存档（任何时候都安全）。

---

## 6. 换电脑 / 重装系统后恢复

```bash
git clone git@github.com:YouNianShao/jobpilot.git
cd jobpilot
python -m venv .venv
.venv\Scripts\activate
pip install -e .
# 最后：自己创建 config.yaml（它本来就不在仓库里，需按 config.example.yaml 填）
```

---

## 7. 网络问题（国内特有）

SSH 通道和授权都已配好，**正常直接 `git push` 就行**。

如果 push 卡住或超时（网络被干扰）：

1. 换网络环境 / 开加速器再重试
2. 提示 `Host key verification failed` 时，用这条：

```bash
GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new" git push
```

3. 实在推不上去也没关系：commit 是本地操作，不受网络影响。先把代码存好档，有网了再 push。

---

## 8. 网页上能干什么

打开 https://github.com/YouNianShao/jobpilot：

- **Code 页**：浏览代码、按 `t` 键搜索文件名
- **铅笔图标**：在线改单个文件，改完直接在网页上 commit（改个错别字很方便）
- **Issues**：记待办和 bug
- **Insights → Network**：看提交历史的可视化图
- **Settings → Collaborators**：邀请别人一起开发
- **Settings → Danger Zone**：删仓库（慎重，不可恢复）

---

## 9. 懒人方案

不想记命令？直接跟我说：

> "把这次改动提交并推送"

我会自动完成 status → add → commit → push 全流程。
