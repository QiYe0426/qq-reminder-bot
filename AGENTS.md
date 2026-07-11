# AGENTS.md — 猎bot (qq-reminder-bot)

## Agent Runtime Output Governance

Metadata v2 Phase 2.5 的 output budget 使用 canonical `ToolResult` 的 UTF-8 JSON
bytes 作为统一单位。当前具备 measurement、默认关闭的 enforcement framework、
`TextReducer` 原型、capability 声明模型和静态 inventory；production reducer 与
capability registry 仍为空。

Reducer 开发必须遵守：

- 只能修改显式审计通过的 `data` namespace 路径，禁止递归字符串扫描；
- 不得修改 `ok`、`error`、`retryable`、execution、idempotency、confirmation、
  resource/file/task/message ID、URL 或状态字段；
- reducer 必须 deterministic、无网络、无数据库、无 NoneBot context、无副作用；
- reducer 结果必须 canonical rebuild，并在 idempotency cache 前确定；
- inventory 的 candidate path 不等于 capability，更不等于 production enable；
- 未经逐工具 Shadow Reduction 审核，不得向 `OUTPUT_BUDGET_REDUCERS` 注册实例，
  不得设置 `enabled=True` capability，也不得默认开启 enforcement flag。

基于 NoneBot2 + OneBot v11 + NapCat 的 QQ bot。AI agent 驱动：用户消息进入 `ai_chat.py`，通过注册在 `agent_tools/` 下的受控工具执行操作。

本文适用于整个仓库。项目当前版本以 `pyproject.toml` 中的 `2.4.0` 为准；行为说明以 `README.md` 和实际代码为准，历史变化查阅 `VERSION.md`。

## 技术栈

- Python 3.10+、NoneBot2、OneBot v11、NapCat
- APScheduler、aiosqlite、python-dotenv
- OpenAI 兼容 API；默认配置使用 DeepSeek，对图片文字识别使用 Qwen VL
- Pillow、ReportLab、pypdf 生成日报图片和 PDF
- NetworkX、Netgraph、Matplotlib 生成语义图
- NoneBot 的 FastAPI driver 承载内置管理控制台；前端为原生 HTML/CSS/JavaScript
- pytest 和 GitHub Actions；服务器使用 Ubuntu 22.04、systemd、Nginx、Certbot

## 快速开始

```powershell
# 本地开发
.\start.ps1                        # 快捷方式 —— 激活虚拟环境后运行 bot.py
.\.venv\Scripts\python bot.py      # 手动运行

# 提交前 —— 安装 dev 依赖再跑测试
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest -q
```

`bot.py` 是唯一入口。它从 `pyproject.toml` 的 `[tool.nonebot] plugins = [...]` 加载所有插件。**新增插件时，除了创建 .py 文件，还必须把路径加到这个列表里。**

## 服务器部署（腾讯云 Ubuntu 22.04）

```bash
# 更新代码 —— git pull、pip install、编译检查、重启、看日志
cd ~/qq-reminder-bot
git pull --ff-only
.venv/bin/pip install -e .
.venv/bin/python -m py_compile bot.py plugins/*.py scripts/codex_remote_approval_hook.py
sudo systemctl restart qq-reminder-bot
journalctl -u qq-reminder-bot -n 80 --no-pager
```

**绝对不要在服务器上直接改源码。** 本地修改、提交、推送，再到服务器 git pull。

### 服务管理

```bash
systemctl is-active qq-reminder-bot       # NoneBot 进程状态
sudo docker ps --filter name=napcat       # QQ 客户端容器状态
sudo certbot certificates                 # Let's Encrypt IP 证书状态
```

### 回滚

```bash
git log --oneline -5
git reset --hard <上一个好用的提交>
.venv/bin/pip install -e .
sudo systemctl restart qq-reminder-bot
```

运行数据（`data/`、`.env.local`、`.venv/`）只存在服务器本地。重建服务器目录前必须先备份。完整备份和恢复流程见 `DEPLOY.md`。

## 架构

```
NapCat (QQ) ←→ OneBot v11 WebSocket → NoneBot → plugins/*
                                                   └─ ai_chat.py → agent_tools/registry
```

- **入口与插件加载**：`bot.py` → `nonebot.load_from_toml("pyproject.toml")`。所有插件路径必须写在 `pyproject.toml` 里。
- **AI agent 流程**：`ai_chat.py` 把用户消息路由给 tool-calling agent。工具在 `agent_tools/registry.py` 注册，每个工具带分类、功能依赖、是否需要管理员权限等元信息。
- **工具注册**：见 `plugins/agent_tools/__init__.py`，它负责导入各工具族并调用 `register_tools()` 注册。每个工具有名称、OpenAI 格式的 definition、handler、`requires_feature`（需要某个群功能开启）和 `requires_admin` 标记。
- **消息流水线**：`message_archive.py` 写入原始消息 → `message_collector.py` / `media_insights.py` 从归档消费 → `daily_report.py` 和 `semantic_graph.py` 读取采集数据。
- **猎宝控制台**：内建 Web UI，地址 `/hunterbot/admin-console`。需要配置 `COMPANION_ADMIN_TOKEN`。由 `plugins/admin_console/` 插件提供，随 bot 进程启动（不需要单独部署前端）。服务器上通过 Nginx 反代提供 HTTPS。

## 核心插件与数据流

| 文件 | 职责 |
|---|---|
| `plugins/ai_chat.py` | AI agent 调度 —— 判断用户问题是否需要调用工具，调用 agent |
| `plugins/agent_tools/registry.py` | 注册/执行所有 AI 可调用的工具 |
| `plugins/agent_tool_access.py` | 按管理员身份、群功能和控制台配置过滤 Agent 工具 |
| `plugins/access_control.py` | 分群功能开关、管理员身份校验 |
| `plugins/reminder.py` + `reminder_service.py` | 提醒命令入口 + 核心服务（命令入口和 AI 工具复用同一套业务代码） |
| `plugins/reminder_target_service.py` | 群聊提醒对象识别、确认和指代处理 |
| `plugins/reminder_prompt.py` | 提醒确认和待补时间提示文本 |
| `plugins/chime_service.py` | 🎒常数报时开关/模式服务（命令入口和 AI 工具复用） |
| `plugins/message_archive.py` | 原始 OneBot 消息持久化 |
| `plugins/message_collector.py` | 在归档之上按群采集消息 |
| `plugins/media_insights.py` | 图片识别、链接抓取、语音转写、自动扫描流水线 |
| `plugins/daily_report.py` | 日报预览、AI 分块总结 + 最终合成、PNG 长图/PDF 生成、定时发送 |
| `plugins/daily_report_visual.py` + `daily_report_fallback.py` | 日报图片/PDF 渲染和 AI 超时兜底 |
| `plugins/semantic_graph.py` + `semantic_graph_visual.py` | 从采集消息中提取、保存并渲染话题关系图 |
| `plugins/companion_memory.py` | 按用户的长期画像和记忆，自动总结循环 |
| `plugins/knowledge_service.py` | STS2 知识条目检索 |
| `plugins/group_context_service.py` | 持久化或进程内群上下文读取 |
| `plugins/remote_approval.py` | Codex 权限请求的 QQ 私聊远程审批桥接 |
| `plugins/storage_cleanup.py` | 未跟踪的清理插件草稿；当前不在 `pyproject.toml` 插件列表中，不会自动加载 |
| `plugins/storage_status.py` | 存储用量查询（`存储状态` / `硬盘状态` / `空间状态`） |
| `plugins/admin_console/` | 内建 Web 管理页：人设、STS2 知识库、群管理、群友画像 |

## 目录职责

| 路径 | 用途 |
|---|---|
| `plugins/` | NoneBot 插件及可复用业务服务 |
| `plugins/agent_tools/` | Agent 工具定义、注册和执行 |
| `plugins/admin_console/` | 控制台后端、静态前端和局部设计规范 |
| `tests/` | pytest 单元及集成边界测试 |
| `config/` | 可提交的默认配置资源，如初始 Bot 人设 |
| `knowledge_sources/` | STS2 自建攻略源文件 |
| `deploy/` | Nginx 和 Certbot 部署配置 |
| `scripts/` | 部署、远程审批、隧道和一次性维护脚本 |
| `data/` | SQLite、日报、缓存和导出等运行数据；禁止提交 |

根目录中的图片合成脚本、生成 PNG、`package.json`、`node_modules/` 和 `tests/test_debug_graph.py` 当前均为未跟踪的本地辅助内容，不属于正式运行链路。不要在未确认用途前将其加入构建、部署或提交。

## 分群功能依赖关系

新群默认静默，只开放基础 AI 对话、`ping` 和 `help`。

```
消息采集（message_archive 写入 + collector 读取）
  ├── 日报（daily_report）—— 还需开启「日报」功能开关
  └── 智能陪伴（companion_memory）—— 还需在控制台按人选择记录
```

切换命令（仅管理员可用，需在目标群里发送）：

```
开启群功能 提醒           # 传统「提醒 ...」命令
开启群功能 消息采集       # 本地归档群消息
开启群功能 日报           # 允许生成日报（需要先开启消息采集）
开启群功能 陪伴画像       # 启用智能陪伴（需要先开启消息采集）
开启群功能 AI对话         # 按群控制 AI 对话
开启群功能 调戏其他bot     # 对已标记 bot 的限流短回复
开启群功能 关键词回怼     # 关键词触发回复
群功能状态                 # 查看本群所有开关状态
```

重要：AI 对话中的自然语言提醒跟随 **`AI对话` 开关**，不跟随「提醒」开关。

## 环境变量与密钥

- `.env` — NoneBot 基础配置（`HOST`、`PORT`、`COMMAND_START`）
- `.env.local`（被 .gitignore 忽略）— 全部真实密钥：API key、管理令牌、模型配置
- `.env.example` — 唯一可提交的环境变量模板，不得填入真实值
- `.env` 和 `.env.local` 都绝不提交

**`.env.local` 管理的内容**：AI 模型（对话、agent、总结、画像、视觉、联网搜索决策）、超时时间、功能开关（媒体识别、自动画像、自动日报）、STS2 数据路径、远程审批令牌。

## 测试

```powershell
.\.venv\Scripts\python -m compileall -q bot.py plugins scripts sts_knowledge_seed.py
.\.venv\Scripts\python -m pytest -q    # 所有测试，安静模式
```

测试文件在 `tests/` 里 —— 每个核心模块一个测试文件。CI 先 `compileall` 再 `pytest`，在 push/PR 时自动运行。测试没有网络依赖（只需要 pip install）。

### 写测试的规矩

- 用 pytest，不用额外框架。测试直接 import 插件的模块。
- 不需要 mock 服务器或 NapCat —— 只测业务逻辑。
- 新增插件或服务时，在 `tests/` 里加对应的测试文件。

## Git 纪律

**绝不提交**：`.env`、`.env.local`、`data/`、`.venv/`、`__pycache__/`、`*.db`、`*.zip`、`qq_reminder_bot.egg-info/`。

CI（`compileall + pytest`）必须通过才能推送。服务器用 `git pull --ff-only` 更新 —— 不要 force-push main 分支。

提交信息沿用仓库现有的简短祈使式说明；适用时使用 `feat(scope):`、`fix(scope):`、`refactor(scope):`、`perf(scope):`。一个提交只包含一个可说明的变更主题，不要顺带提交本地生成物或无关文件。

## 开发规范和代码风格

- **注册插件**：新建插件 `.py` 文件后，必须把路径加到 `pyproject.toml` 的 `[tool.nonebot] plugins = [...]`。
- **注册 AI 工具**：在 `plugins/agent_tools/` 下用 `AgentTool` dataclass 定义工具。然后在 `plugins/agent_tools/__init__.py` 里 import 并调用 `register_tools()`。
- **新命令**：加在已有的对应插件里。不要为了一个命令单独开一个插件文件。
- **业务复用**：命令 handler 只处理 OneBot/NoneBot 交互；解析、查询和写入逻辑下沉到 service，供命令和 Agent 工具共用。
- **异步 I/O**：数据库使用 `aiosqlite` 和 `async with`；不要在事件处理路径加入长时间同步阻塞操作。
- **数据结构**：沿用现有 dataclass、类型标注和 `Path`；SQL 必须参数化，不拼接用户输入。
- **错误处理**：用户可恢复的错误返回明确中文提示；后台异常使用 NoneBot logger，日志不得包含密钥、令牌或完整敏感命令。
- **前端**：修改控制台前先读 `plugins/admin_console/DESIGN.md`；沿用现有原生前端、设计 token、双主题和响应式规则。
- **文档同步**：功能、命令、配置、部署或数据流发生变化时，同步更新 `README.md`；版本发布时更新 `VERSION.md` 和 `pyproject.toml`。
- **服务器更新步骤**：`git pull --ff-only` → `pip install -e .` → 编译检查 → `systemctl restart` → 检查日志。
- **时区**：`Asia/Shanghai`。日报统计窗口是 04:00–04:00。

## 禁止事项和修改注意事项

- 不读取、输出、复制或提交真实 API key、管理员令牌、QQ 私钥及 `.env.local` 内容。
- 不提交 `data/`、数据库、缓存、报告、导出、虚拟环境、依赖目录或本地测试图片。
- 不绕过 `access_control.py` 和 `agent_tool_access.py` 的管理员、群功能及工具权限检查。
- 不改变“日报和陪伴依赖消息采集”的功能依赖，不把 AI 自然语言提醒错误地绑定到传统提醒开关。
- 不在服务器运行目录直接改源码，不把服务器运行数据当作 Git 可恢复内容。
- 不假定文件存在即代表功能已启用；以 `pyproject.toml` 的插件列表和 `agent_tools/__init__.py` 的注册列表为准。
- 修改共享数据库表或字段时必须考虑已有服务器数据和迁移路径，并补充测试。
- 工作区可能已有他人修改；先读 `git status` 和目标文件差异，不撤销、不覆盖无关改动。

## 容易踩的坑

- 消息里的 `@` 保留的是 QQ 号（不是昵称），AI agent 需要处理这一点。
- 群聊里发送 `生成日报`、`预览日报` 会被静默忽略。管理员必须在私聊里带群号使用。
- 控制台的 STS2 知识库只覆盖 STS2。非 STS2 的知识条目不会被 `sts_knowledge_seed.py` 或「重建 STS2」按钮影响。
- 关键词回怼的图片检测只通过 Qwen VL 提取图片中的可见文字 —— 不扫描 URL 或文件名。
- 远程审批：审批码一次有效。批准、拒绝或退出后立即失效。
