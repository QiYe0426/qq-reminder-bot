# Agent Runtime 开发交接

本文记录 `qq-reminder-bot` 当前 Agent Runtime 的真实架构、安全边界和后续事项。后续开发应先阅读仓库根目录的 `AGENTS.md`，再结合当前代码与测试确认行为；不要从旧提交说明推断现状。

## 1. 当前完成状态

### P0-1：SSRF hardened `fetch_url`

- `safe_http_fetch` 模块统一承载 Agent URL 抓取。
- 使用受控解析与连接流程校验目标地址。
- 防止 DNS rebinding 导致已校验域名连接到私有或保留地址。
- 重定向目标继续经过安全校验，不绕过 SSRF 边界。

### P0-3：Agent Tool 目标群授权

- Gateway 授权阶段解析并验证 `effective_group_id`。
- 群聊不能通过模型参数切换到其他群。
- 私聊中的目标群访问必须满足对应授权条件。
- 工具权限以实际目标资源为准，不只依赖当前会话或全局管理员身份。
- 群资源在 Agent Tool 层完成隔离。

### P1-3：Reminder Target Authorization

- 使用 `AuthorizedReminderTarget` 表达服务端已授权的提醒目标。
- Agent Tool 不信任模型直接给出的 `target_user_id`。
- 模型不能通过构造参数为其他用户创建提醒。
- 提醒目标授权位于 Tool adapter / 目标解析边界；Reminder 核心服务不承担模型来源判断。

### P2-1：Confirmation Workflow

- 高风险 Tool 在执行前进入 Confirmation 流程。
- 确认状态绑定调用者、目标、工具和参数。
- 确认令牌一次性使用；不能作为长期授权或跨调用复用。
- 确认后的恢复执行仍经过 Runtime 执行链。

### P2-2：Idempotency Layer

- SQLite 保存 Tool execution state。
- 支持首次 claim、完成结果 replay、运行中状态和 unknown 状态。
- 已完成结果可复用缓存，防止相同写操作被模型重复执行。
- stale running 不会被静默当作成功或安全重试，而是进入 unknown 处理。

### P2-3：Audit Runtime

- Audit database 保存结构化 Runtime 事件。
- Gateway 记录请求、授权、确认、幂等和执行终态事件链。
- 模型 `tool_call.id` 进入内部 context，并与 Audit 事件关联。
- Confirmation 恢复调用使用服务端 invocation source，并通过 confirmation 记录关联原调用。
- Idempotency replay 保留各自 Tool Call 标识，同时通过相同 idempotency key 关联。
- `web_search`、`fetch_url`、`get_chime` 使用内建 Audit adapter；`respond` 记录 `response_emitted`。

### Sensitive operational logging

- 生产日志不再直接输出 QQ号、群号、用户输入、搜索关键词、画像关键词或文件绝对路径。
- 标识符使用安全指纹；自由文本使用长度、类型或指纹保留调试价值。
- URL 日志去除 userinfo、query 和 fragment。
- Audit 与普通运行日志均不应保存提醒正文、回复正文或其他敏感原文。

## 2. 当前 Runtime 架构

```text
LLM
 ↓
ai_chat
 ↓
Tool Gateway
 ↓
Schema
 ↓
Authorization
 ↓
Confirmation
 ↓
Idempotency
 ↓
Audit
 ↓
Handler
```

`plugins/ai_chat.py` 负责模型 Tool Call envelope、单次调用 context 和结果回灌。注册工具通过 `plugins/agent_tools/gateway.py` 的统一入口执行。Gateway 在调用业务 Handler 前完成服务端约束；模型输出和 Prompt 都不能替代这些检查。

部分内建工具仍由 `ai_chat.py` 执行，但已经接入专用 Audit adapter。它们不等同于完整迁入 Gateway，后续迁移需要保持现有 SSRF、安全日志和结果协议边界。

## 3. 职责边界

### Gateway

- 执行参数解析和 Schema 验证。
- 根据当前会话与目标资源完成权限检查。
- 对需要确认的高风险工具创建或验证 Confirmation。
- 对启用幂等的工具 claim execution state、处理 replay/running/unknown。
- 在关键阶段写入 Audit 事件。
- 只有通过上述边界后才调用 Handler。

Gateway 是注册工具的统一执行入口。不得新增直接从 Agent 循环调用注册 Handler 的旁路。

### Audit

- 记录 Tool invocation 的结构化事件和关联标识。
- 对允许记录的详情再次执行脱敏。
- 使用指纹、长度、数量、状态和错误分类代替敏感原文。
- 不保存 QQ号、群号、提醒正文、用户原文、搜索词、URL query、token 或其他秘密值。

Audit 负责可追溯性，不负责授权决策，也不能因为写入失败而猜测业务是否执行成功。

### Confirmation

- 为高风险调用提供一次性授权。
- 授权必须绑定具体调用者、工具、目标和规范化参数。
- 已使用、过期或绑定不匹配的确认不能执行。

Confirmation 不是角色权限系统，也不代替 Gateway Authorization。

### Idempotency

- 防止模型重试或重复 Tool Call 造成业务重复执行。
- 保存执行状态并缓存已完成结果。
- 对 replay、running 和 unknown 提供明确分支。

Idempotency 不能替代 Confirmation，也不能把未知外部状态自动视为可重试。

### Handler

- 只处理已授权、已验证参数对应的具体业务。
- 返回稳定的 ToolResult，由 Runtime 处理执行协议。
- 不自行实现另一套 Gateway、Confirmation、Idempotency 或 Audit 流程。
- Handler 内的检查只能作为业务约束或防御性补充，不能成为唯一安全边界。

## 4. 核心代码导航

| 路径 | 当前职责 |
|---|---|
| `plugins/ai_chat.py` | Agent 循环、Tool Call context、内建 Tool adapter、结果回灌 |
| `plugins/agent_tools/gateway.py` | 注册工具统一执行链 |
| `plugins/agent_tools/contracts.py` | 参数解析、JSON Schema 验证、ToolResult 规范化 |
| `plugins/agent_tool_access.py` | Tool 与目标群资源授权 |
| `plugins/agent_tools/confirmation.py` | 一次性确认状态 |
| `plugins/agent_tools/idempotency.py` | SQLite execution state 与结果 replay |
| `plugins/agent_tools/audit.py` | Audit database、事件、指纹和详情脱敏 |
| `plugins/agent_tools/reminder_tools.py` | Reminder Tool adapter 与目标授权接入 |
| `plugins/reminder_target_service.py` | 服务端提醒目标解析与授权数据结构 |
| `plugins/safe_http_fetch.py` | Agent URL 抓取的 SSRF 安全边界 |
| `plugins/sensitive_logging.py` | 普通运行日志的统一脱敏 helper |

具体文件名和接口在继续开发前仍应以当前 checkout 为准。

## 5. 当前未解决事项

以下事项仅记录，不在本文档任务中实施：

### Metadata v2 尚未实施

当前 Tool metadata 能支持现有 Runtime，但更完整的 Metadata v2 尚未设计和落地。后续如增加成本、超时、输出预算或更细粒度资源声明，应独立设计并保持兼容。

### 错误码体系仍处于兼容阶段

Runtime 已有机器可读错误码和 ToolResult 规范化，但注册工具、内建工具及部分旧业务返回仍存在兼容路径。统一错误分类、稳定语义和迁移策略需要单独治理。

### 部分内建工具未来可继续迁移 Gateway

`web_search`、`fetch_url`、`get_chime` 和 `respond` 目前仍有 `ai_chat.py` 内建执行或控制流职责。未来可以评估进一步迁移，但必须保留：

- `fetch_url` 的 `safe_http_fetch` 与 SSRF 防护；
- 现有 Audit 关联和敏感信息边界；
- `respond` 作为 Agent 终止控制流的语义；
- 内建工具当前的参数和结果兼容行为。

### 普通 AI context 的语义图自动构建问题仍待评估

普通 AI context 构建过程中可能触发语义图相关工作。其读取、自动构建、成本和副作用边界仍需评估；在结论明确前不要顺带改变 Context 或 Prompt 策略。

## 6. 后续开发约束

1. 不信任模型生成的参数、目标 ID 或调用顺序。
2. JSON 可解析、Schema 合法和资源已授权是三个不同阶段。
3. 注册工具必须通过 Gateway，不得直接调用 Handler。
4. 不在其他安全任务中顺带修改 Prompt、Reminder 核心服务或 SSRF 边界。
5. Confirmation、Idempotency 和 Audit 各自职责独立，不互相替代。
6. 敏感值不得进入普通日志、Audit details 或异常附加文本。
7. 修改执行链时应覆盖成功、拒绝、确认、重复调用、异常和恢复路径。
8. 文档不记录易过期的测试通过数量；测试状态以当前 checkout 的实际运行结果为准。

## 7. 推荐验证流程

```powershell
git status --short --branch
.\.venv\Scripts\python -m compileall -q bot.py plugins scripts sts_knowledge_seed.py
.\.venv\Scripts\python -m pytest -q
git diff --check
```

发布、推送或服务器更新必须由对应任务明确授权；完成本地代码或文档提交不等于允许 push 或部署。
