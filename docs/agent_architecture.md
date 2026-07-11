# Agent Runtime Architecture

更新时间：2026-07-11
代码基线：`510c0f66943b21b95ec8b8f6a5493fc70ea79bdd`，另包含尚未提交的 P2-3 Gateway Audit 接入。

本文描述当前真实实现。除“迁移建议”和“测试缺口”外，所有状态均以当前代码为准。

## 1. Agent Runtime 总览

猎bot 是一个由 LLM Agent 驱动的 QQ Bot。NoneBot2 接收 OneBot v11 消息，`plugins/ai_chat.py` 构建上下文并运行模型工具循环；注册工具由 `plugins/agent_tools/registry.py` 管理，统一通过 `plugins/agent_tools/gateway.py:104` 执行。

```text
QQ / NapCat
  -> NoneBot2 MessageEvent
  -> plugins/ai_chat.py
  -> LLM tool_call
  -> Tool Gateway
  -> Plugin Handler
  -> ToolResult
  -> LLM / direct reply
```

Agent Runtime 基础设施由以下模块组成：

| 模块 | 职责 |
|---|---|
| `contracts.py` | JSON 参数解析、JSON Schema 验证、统一 ToolResult |
| `registry.py` | Tool 定义、元数据、注册与兼容入口 |
| `gateway.py` | 参数、权限、确认、幂等、审计和 Handler 的统一编排 |
| `agent_tool_access.py` | 会话权限、目标群权限、功能与工具开关 |
| `confirmation.py` | 一次性确认记录、短确认码和内部 token |
| `idempotency.py` | 幂等键、执行 claim、结果缓存和 unknown 状态 |
| `audit.py` | 追加式审计事件、HMAC 参数指纹和安全详情脱敏 |

## 2. Tool 生命周期

当前注册工具执行链如下：

```text
LLM
  -> Tool Call
  -> Gateway: create invocation_id
  -> Audit: tool_requested
  -> Schema parse + validation
  -> Authorization + effective_group_id
  -> Confirmation validation / consumption
  -> Idempotency claim / replay decision
  -> Audit: execution_started
  -> Handler
  -> ToolResult normalization
  -> Idempotency completion
  -> Audit: execution_completed | execution_failed
```

Gateway 在各阶段追加以下事件：

| 阶段 | 事件 |
|---|---|
| 接收调用 | `tool_requested` |
| 参数失败 | `validation_failed` |
| 权限失败 | `authorization_denied` |
| 等待用户确认 | `confirmation_required` |
| 确认已消费 | `confirmation_accepted` |
| 幂等首次领取 | `idempotency_claimed` |
| 返回缓存 | `idempotency_replayed` |
| 相同调用执行中 | `idempotency_running` |
| 副作用状态不确定 | `idempotency_unknown` |
| Handler 前 | `execution_started` |
| 正常结束 | `execution_completed` |
| 业务失败或异常 | `execution_failed` |

高风险工具在 `execution_started` 无法写入时禁止执行 Handler。低风险和中风险工具的审计写入失败只写系统异常日志，不改变原有工具行为。Handler 已执行后的审计失败不能回滚外部副作用。

当前 `ai_chat.py:2164-2168` 会先解析和验证工具参数，注册工具进入 Gateway 后会再次验证。这是重复校验，不是安全边界冲突；迁移内建工具前不能直接删除循环层校验。

## 3. 权限模型

权限输入分为三个层次：

- Session scope：`_target_type`、`_target_id` 和 `_user_id` 描述当前消息会话与调用者。
- Effective group：`authorize_agent_tool()` 根据会话和参数计算 `_effective_group_id`，实现位于 `plugins/agent_tool_access.py:332-404`。
- Target authorization：需要目标群管理员权限的工具还会查询目标群成员角色，并检查目标群工具开关和功能开关。

群聊工具默认绑定当前群。`group_scope="private_explicit"` 的工具在私聊中必须显式提供 `group_id`；群聊中传入不同群号会被拒绝。`requires_target_group_admin` 不等同于全局 Bot 管理员身份，目标群角色会再次验证。

权限检查发生在 Schema 验证之后、Confirmation 之前。确认 token 不能替代权限；确认后重新执行时 Gateway 会重新授权，因此权限撤销会阻止执行。

Reminder 的他人目标授权不由通用 Tool 权限代替。最终创建流程要求目标经过服务端解析和授权，形成 `AuthorizedReminderTarget` 后才能创建。

## 4. Confirmation

Confirmation 状态为：

```text
pending -> confirmed -> consumed
    |          |
    +--------> expired
```

- `confirmation_id` 是数据库内部 ID。
- `confirmation_code` 是用户输入的短码。
- `token` 是确认后生成的内部执行凭证，不展示给用户。
- token 只存摘要，并绑定用户、工具、规范化参数、会话 scope 和 `effective_group_id`。
- Gateway 先 `validate_confirmation()`，再原子 `consume_confirmation()`；已消费 token 重放会返回 `confirmation_consumed`。
- `ai_chat.py:2037-2063` 根据短码查找保存的 Tool Call，确认后直接执行保存的工具和参数，不重新让模型决策。

Confirmation 只回答“用户是否授权”，不负责权限判断或重复执行控制。

## 5. Idempotency

幂等键由以下字段生成：

```text
tool_name + canonical arguments + user_id
+ session target_type/target_id + effective_group_id
```

确认 ID、确认码和 token 不进入幂等键。首批写工具通过 `AgentTool.idempotency_enabled` 启用；读取工具不缓存。

状态机为：

```text
claim -> running -> succeeded
                 -> failed
                 -> unknown
succeeded/failed -> expired -> 可重新 claim
stale running    -> unknown
```

- `running`：另一个相同调用持有有效 lease，返回 `already_running`。
- `succeeded` / `failed`：TTL 内返回已有 ToolResult。
- `unknown`：外部副作用是否发生无法确定，禁止自动重试。
- `expired`：自然过期记录，可被下一次 claim 重置；它与 unknown 不等价。
- Handler 在 SQLite 事务外执行，执行流程是 claim、commit、Handler、保存结果。

临时失败仅短期缓存；永久失败按正常 TTL 缓存；Handler 异常按 unknown 处理。

## 6. Audit

审计数据库为 `data/agent_tool_audit.db`，表为 `agent_tool_audit_events`。事件以 `(invocation_id, sequence)` 排序，每次 Gateway 调用生成新的 `invocation_id`。

主要字段包括 Tool Call、调用者与会话 scope、目标群、参数指纹、Confirmation/Idempotency 状态、风险元数据、执行阶段、结果、错误码、失败分类和耗时。

安全原则：

- 不保存 Prompt、用户原文或完整工具参数。
- 不保存确认 token、确认码或幂等 owner token。
- 不保存提醒正文、用户昵称或 URL query。
- 参数指纹使用 HMAC-SHA256；密钥来自 `AGENT_TOOL_AUDIT_HMAC_KEY`。
- 未配置密钥时使用运行期随机密钥，并明确告警该指纹不能跨重启关联。
- `safe_details_json` 写入前始终再次经过 `sanitize_details()`。

当前限制：`ai_chat.py:2199` 调用 `run_agent_tool()` 时没有把模型 `tool_call.id` 注入 context，因此真实 Agent 调用的 `tool_call_id` 字段暂为空。修复该问题属于后续 `ai_chat.py` 入口整理，不应在 Gateway 内猜测 ID。

## 7. 故障恢复策略

| 故障 | 当前策略 |
|---|---|
| 参数或权限失败 | Handler 未执行，记录阶段事件并返回明确错误码 |
| Confirmation DB 失败 | fail-closed，不执行高风险操作 |
| Confirmation 过期或重放 | 拒绝执行，要求重新发起调用 |
| Idempotency claim 失败 | fail-closed，返回可重试错误 |
| 幂等记录为 running | 不重复执行，调用方稍后查询或重试 |
| 幂等记录为 unknown | 不自动重试，需人工核对外部状态 |
| 高风险 execution_started 审计失败 | fail-closed；已 claim 的记录收束为短期失败 |
| Handler 异常 | 转换为 `tool_execution_failed`；幂等状态记为 unknown |
| 终态审计写入失败 | 记录系统异常；不回滚已经发生的副作用 |

审计表当前没有自动归档、保留期和密钥轮换机制。部署恢复时应同时备份 Confirmation、Idempotency、Audit 数据库；恢复 unknown 记录前必须核对真实外部状态。

## 8. ai_chat Tool 入口审计

`run_agent_tool()` 位于 `plugins/ai_chat.py:1915-1974`。注册工具在函数开头通过 `has_agent_tool()` 进入 Gateway；其余工具继续使用本地分支。

| 工具 | 当前路径 | 风险 | 建议 |
|---|---|---|---|
| `web_search` | `ai_chat.py:1923-1927` 直接调用 `agent_web_search()` | 绕过统一 Audit、超时元数据和 ToolResult 异常边界 | 迁移为低风险、外部读取型注册工具 |
| `fetch_url` | `ai_chat.py:1929-1932` 直接调用 SSRF 安全 fetch | SSRF 边界已存在，但绕过 Audit 和统一异常处理 | 迁移适配层，必须复用 `safe_http_fetch`，不能重写网络边界 |
| `get_chime` | `ai_chat.py:1967-1972` 直接调用 service | 读取路径未审计，参数/上下文错误码不统一 | 迁移为低风险读取工具 |
| `respond` | `ai_chat.py:2193-2197` 直接终止 Agent 循环 | 既是 Tool 又是控制流，完全绕过 Gateway/Audit | 设计 terminal ToolResult 后迁移，仍由循环负责最终发送 |
| `set_chime` | 已注册于 `chime_tools.py:51-64` | 不再绕过 Gateway | 保持现状 |

`ai_chat.py:2164-2168` 与 Gateway 存在重复参数解析和 Schema 验证。建议在所有内建工具迁移后，让循环只提取 Tool Call envelope，并把原始 arguments、`tool_call_id` 和 invocation source 交给 Gateway。

## 9. 旧分发代码清理建议

`create_reminder`、`list_reminders` 和 `cancel_reminder` 已在 `plugins/agent_tools/__init__.py:24` 注册。`run_agent_tool()` 在 `ai_chat.py:1916-1917` 对所有注册工具提前返回，因此以下旧分支不可达：

| 位置 | 建议 | 风险与测试影响 |
|---|---|---|
| `ai_chat.py:1934-1942` | 删除旧 `create_reminder` Tool 分支 | 不要删除 `ai_chat.py:1482` 的服务调用；它属于已授权他人提醒的直接交互流程 |
| `ai_chat.py:1944-1953` | 删除旧 `list_reminders` 分支 | 删除后可移除仅供该分支使用的 `list_reminders_result` import |
| `ai_chat.py:1955-1965` | 删除旧 `cancel_reminder` 分支 | 删除后可移除仅供该分支使用的 `cancel_reminder` import |

清理前后应运行 `test_agent_tools.py`、`test_agent_tool_contract.py`、Reminder 全套测试以及 Agent 循环测试，并增加“注册提醒工具不会进入本地旧分支”的显式回归。

## 10. Tool Metadata v2

当前 `AgentTool` 字段位于 `registry.py:18-40`：`name`、`definition`、`handler`、`category`、功能/管理员/群 scope 字段、`side_effect`、`risk_level`、Confirmation 开关与超时、Idempotency 开关与 TTL/lease/失败分类集合。

缺失或表达不足的字段：

| v2 字段 | 目的 | 迁移建议 |
|---|---|---|
| `description` | UI 与说明生成的稳定文本 | 第一阶段从 `definition.function.description` 派生，避免双份维护 |
| `required_scope` | 统一表达 none/session/group/target-group | 由现有 `requires_group`、`group_scope` 和 target admin 字段映射，稳定后弃用旧字段 |
| `confirmation_policy` | none/always/conditional | 先映射 `requires_confirmation`，不要改变现有状态机 |
| `idempotency_policy` | none/result-cache/external-action | 先映射 `idempotency_enabled` 和 TTL 字段 |
| `timeout_seconds` | Gateway 统一执行预算 | 新增但先不强制，逐工具评估后启用 |
| `output_budget` | 限制 ToolResult 回灌大小 | 以字符或序列化字节定义，超限返回标准错误 |
| `external_side_effect` | 区分本地写入与外部发送 | 可由 `side_effect == external` 初始映射 |
| `mutates_state` | UI、确认和审计的明确写标识 | 可由 `side_effect != none` 初始映射 |

`category`、`side_effect`、`risk_level`、`requires_confirmation` 已存在，不应重复新增。推荐先增加只读派生属性和注册时一致性校验，再逐步替换布尔字段；同一阶段不要同时改 Tool Schema 和 Handler。

## 11. 错误码体系审计

当前主要错误码来自 `contracts.py`、`gateway.py`、`confirmation.py` 和各 Handler：

| 分类 | 当前错误码 |
|---|---|
| validation | `invalid_arguments`、`invalid_tool_definition`、`invalid_tool_result`、`invalid_id`、`invalid_date`、`empty_query` |
| authorization | `tool_not_allowed`、`group_permission_denied`、`feature_disabled`、`invalid_identity_scope`、`not_group_chat` |
| confirmation | `confirmation_required`、`confirmation_not_found`、`confirmation_expired`、`confirmation_not_pending`、`confirmation_scope_mismatch`、`confirmation_invalid`、`confirmation_consumed`、`confirmation_mismatch`、`confirmation_state_failed` |
| execution | `tool_execution_failed`、`audit_unavailable`、`idempotency_unavailable`、`idempotency_claim_failed`、`already_running`、`execution_state_unknown` |
| resource | `tool_not_found`、`missing_event`、`missing_user`、`missing_target`、`missing_group_id`、`not_found`、`graph_not_found`、`feature_unavailable`、`dependency_failed` |
| temporary | `generation_failed` 及 metadata 中声明为 temporary 的工具错误 |
| rate_limit | 当前没有统一错误码 |

重复和歧义：

- `not_found` 与 `graph_not_found` 表达同一资源缺失层级但粒度不同。
- `feature_disabled` 与 `feature_unavailable` 分别混合配置关闭和运行期不可用。
- `tool_not_allowed` 与 `group_permission_denied` 都表示授权拒绝，但调用方难以统一统计。
- `missing_target`、`missing_group_id`、`missing_event`、`missing_user` 混合了参数缺失和服务端上下文缺失。
- `generation_failed` 没有说明临时性，只能依赖每个 Tool metadata 二次解释。

建议保留 snake_case 协议并建立中央常量/分类映射：`invalid_arguments`、`authorization_denied`、`confirmation_*`、`resource_not_found`、`dependency_unavailable`、`execution_failed`、`temporary_unavailable`、`rate_limited`。迁移期保留旧码到新分类的兼容映射，不要一次性修改所有 Handler。

## 12. 日志安全扫描

未发现显式记录 Confirmation token、确认码或 Idempotency owner token 的日志。以下位置存在内容或身份泄露风险：

| 优先级 | 位置 | 风险 |
|---|---|---|
| P0 | `ai_chat.py:2191` | 非特殊工具会记录完整 args，可包含提醒正文、查询词、群号和用户 ID |
| P0 | `ai_chat.py:2184-2185` | `fetch_url` 的完整 URL 被放入日志对象，query 可能包含 token |
| P0 | `ai_chat.py:2676` | 同时记录 QQ 用户 ID 和用户原文前 60 字 |
| P0 | `ai_chat.py:2684` | 同时记录 QQ 用户 ID 和疑似 Prompt 注入原文前 120 字 |
| P1 | `ai_chat.py:2182-2183`、`2521-2524`、`2703` | 搜索 query 和派生查询可能包含用户原文或敏感实体 |
| P1 | `agent_tool_access.py:324` | 权限查询失败时同时记录目标群号和用户 QQ |
| P1 | `companion_memory.py:1489-1492` | 记录群号、用户 QQ 和画像更新 reason |
| P1 | `group_reactions.py:871` | 记录群号与触发关键词 |
| P2 | `daily_report.py:1174-1490` | 多处记录群号、管理员 QQ、日期和任务进度，主要是可关联标识符 |

推荐用 `invocation_id`、Tool 名、错误码、耗时和 HMAC scope 指纹替代原文及 QQ。URL 日志只保留 scheme/host/path，删除 userinfo、query 和 fragment。`logger.exception` 的固定消息本身较安全，但仍需约束底层异常对象不要包含请求正文或带 query 的 URL。

## 13. 测试覆盖缺口

现有测试覆盖 Contract、目标群权限、Confirmation 绑定与重放、Idempotency 状态、Audit 存储与并发、Reminder 授权、SSRF 和 Semantic Graph 边界。当前全量基线为 165 passed。

### P0

- 内建 `web_search`、`fetch_url`、`get_chime`、`respond` 绕过 Gateway/Audit 的端到端测试缺失。
- `ai_chat.py` 没有验证真实 `tool_call.id` 进入 Audit；当前字段为空。
- 日志测试没有断言提醒正文、用户原文、URL query、QQ 和 nickname 不被记录。
- Audit 数据库不可写时仅覆盖高风险 `execution_started`；没有覆盖磁盘满、数据库损坏和锁超时。

### P1

- Gateway Audit 尚无 `validation_failed` 专项测试。
- 尚无 `idempotency_running`、`idempotency_unknown` 事件链测试。
- 尚无低风险 Audit 写失败仍执行，以及终态 Audit 写失败不改变 ToolResult 的测试。
- Agent Loop 只覆盖 Confirmation 中止；缺少工具调用上限、多 Tool batch、内建工具异常和 ToolResult 过大场景。
- Reminder 已覆盖授权对象，但缺少 Agent Tool 与服务端授权目标组合的完整消息事件集成测试。

### P2

- Audit 没有保留期、归档、索引规模和查询性能测试。
- HMAC 密钥轮换、配置错误和跨重启关联策略没有测试。
- Metadata 缺少注册时一致性测试，例如高风险外部写操作必须配置 Confirmation 和 Idempotency。
- 错误码没有中央枚举、分类完整性或重复检测测试。
- 缺少 invocation duration、sequence 跨大量并发调用以及事件统计聚合测试。
