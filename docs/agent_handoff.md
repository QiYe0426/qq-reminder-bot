# Agent Runtime 项目交接

## Current Phase 2 handoff update (2026-07-11)

本节记录当前工作区的实际后续进展，并取代本文后续“尚未进入 Phase 2”的旧基线说明；
相关修改仍未提交。

已完成：Compatibility Resolver、Audit policy source、Confirmation decision source、
Idempotency enable source、带默认关闭 feature flag 的 legacy AND metadata
Authorization、Handler execution timeout、Output Budget shadow measurement、默认关闭的
enforcement framework、隔离 `TextReducer`、capability model，以及 Phase 2.5-B1.1.6
Output Reducer Capability Inventory。

本阶段新增 `plugins/agent_tools/output_reducer_inventory.py` 和
`tests/test_agent_tool_output_reducer_inventory.py`。Inventory 显式覆盖当前 14 个注册
AgentTool，记录 canonical `data` 输出结构、候选文本路径、禁止路径、风险和 review
status。它不扫描 Handler 输出，不创建 reducer，也不写入 capability registry。

当前 `OUTPUT_BUDGET_REDUCERS` 和 `OUTPUT_REDUCER_CAPABILITIES` 均为空，
`AGENT_OUTPUT_BUDGET_ENFORCEMENT` 默认关闭。测试状态：本阶段 inventory、capability
与 TextReducer 定向测试已通过；未运行全量测试。

未完成事项：候选路径的真实样本 Shadow Reduction、画像嵌套 schema 人工复核、逐工具
capability 审批和 production reducer 注册。下一阶段入口是 Phase 2.5-B1.2 Shadow
Reduction；在完成结构差异与 replay 一致性审计前，不得启用 reducer。

本文记录 `qq-reminder-bot` 当前 Agent Runtime 的真实安全治理状态、执行架构和 Metadata v2 后续迁移边界。后续开发应先阅读仓库根目录的 `AGENTS.md`，并以当前 checkout 的代码为准。

## 1. 当前 Git baseline

- 分支：`feature/agent-runtime-v3`
- Git HEAD：`c8deccc12fed19c213b74e23819e06213884fe7b`
- Commit：`c8deccc add agent tool metadata v2 foundation`
- 基线状态：Metadata v2 Phase 1 已完成；本交接阶段不进入 Phase 2 编码。

## 2. 已完成的安全治理阶段

### P0-1 SSRF Hardening

- `fetch_url` 使用安全隔离的 `aiohttp` fetcher。
- 对 DNS 解析结果和实际连接目标执行安全检查，防止 DNS rebinding。
- 每次 redirect 都重新执行目标安全检查。
- 禁止继承环境代理。
- 限制响应体大小。

### P0-3 / P1-3 Reminder Authorization

- Agent Tool 只能创建当前用户提醒，或创建经过服务端授权的群成员提醒。
- 群成员目标需要通过当前群上下文验证。
- `@` 成员、候选确认和 recent target 均绑定授权上下文。
- 不信任模型直接提供的 `target_user_id`。

### P2-1 Confirmation Workflow

- 高风险 Agent Tool 在执行前进入确认门。
- SQLite 保存 confirmation 状态机。
- confirmation token 一次性使用。
- 确认记录绑定参数、用户、群和工具作用域。

### P2-2 Idempotency Layer

- Agent Tool 通过 execution claim 控制幂等执行。
- 支持结果 replay。
- 执行状态包括 `running`、`succeeded`、`failed` 和 `unknown`。
- 避免重试或重复 Tool Call 重复产生副作用。

### P2-3 Audit Runtime

- Agent Tool 使用结构化 Audit Event。
- `invocation_id` 贯穿调用关联。
- 记录 execution lifecycle。
- Audit details 对敏感字段脱敏。
- 内建工具通过 audit adapter 接入审计链路。

### Sensitive Logging Hardening

- 用户输入不直接写入普通运行日志。
- QQ 号、群号、关键词和 URL 敏感信息经过保护或脱敏。
- 日志不保存隐私原文。

### Metadata v2 Phase 1

- 新增不可变 `AgentToolMetadata`。
- 注册工具增加 metadata 声明。
- 内建工具增加 metadata catalog。
- 增加 metadata 枚举校验。
- 保留 legacy policy 字段兼容。
- 未改变 Runtime 行为。

## 3. 当前 Runtime 架构

```text
AgentTool
  ↓
Metadata v2 declaration
  ↓
Legacy compatibility layer
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

Metadata v2 Phase 1 只完成声明层建设。当前 Runtime 尚未消费以下 metadata 字段：

- `metadata.risk_level`
- `metadata.side_effect`
- `metadata.confirmation_policy`
- `metadata.idempotency_policy`
- `metadata.timeout_seconds`
- `metadata.output_budget`

实际执行策略仍来自 legacy policy 字段，包括：

- `risk_level`
- `side_effect`
- `requires_confirmation`
- `confirmation_timeout`
- `idempotency_enabled`
- `idempotency_ttl`
- `idempotency_lease_timeout`
- `requires_admin`
- `requires_feature`
- `group_scope`

因此，Phase 1 的 metadata 是声明信息，不是 Runtime 的策略事实来源。任何迁移都必须保持现有 fail-closed 行为，不能直接把 Runtime 切换到 metadata。

## 4. 当前风险与迁移约束

### Metadata / Legacy 双策略源

相同策略同时存在于 metadata 和 legacy 字段中，存在声明与实际执行行为漂移的风险。当前已知冲突包括：

| Tool | Metadata | Legacy Runtime |
|---|---|---|
| `generate_daily_report` | `medium / mixed` | `high / external` |
| `build_semantic_graph` | `medium / database_write` | `high / write` |

在 resolver 能够安全处理冲突之前，不得直接以 metadata 覆盖 legacy 值，否则可能降低既有高风险工具的保护等级。

### Confirmation policy 不完整

当前 Gateway 读取 `tool.requires_confirmation`，确认超时仍来自 legacy 字段。`confirmation_policy` 的 `optional` 尚无 Runtime 语义，`conditional` 也没有条件表达式。

Phase 2 只允许先迁移明确等价的语义：

- `required` → 必须确认
- `never` → 不需要确认

`optional` 和 `conditional` 暂不改变 Runtime 行为，并继续使用 legacy fallback。

### Idempotency policy 不完整

当前 Gateway 通过 `tool.idempotency_enabled` 进入 `claim_execution`，已有行为同时包含防并发、结果缓存和结果 replay。

Metadata 的 `none`、`single_flight`、`result_cache` 不能直接与现有布尔字段一一映射。特别是 `build_semantic_graph` 的现有行为不是单纯 `single_flight`，不得在缺少兼容解析的情况下直接切换。

### Authorization metadata 不完整

`resource_scope` 不能替代现有授权字段和语义，包括：

- `requires_admin`
- `requires_feature`
- `requires_group`
- `requires_target_group_admin`
- `group_scope`

后续 metadata 需要补充类似 `required_permissions` 的授权声明；在此之前，Authorization 必须继续读取现有 Runtime 策略。

### Timeout 与 output budget 未消费

`metadata.timeout_seconds` 和 `metadata.output_budget` 当前没有 Runtime 消费点。它们只能作为声明存在，不能被描述为已经具备执行限制能力。

## 5. 下一阶段计划

### Metadata v2 Phase 2

目标是在保留兼容性和 fail-closed 行为的前提下，让 Runtime 逐步读取解析后的统一策略：

1. 增加 compatibility resolver，统一解析 metadata 与 legacy policy。
2. Runtime 改为读取 resolved policy，而不是直接散落读取两套字段。
3. 迁移 Audit 的策略来源。
4. 迁移 Confirmation 中语义明确的 `required` / `never`。
5. 迁移 Idempotency 中语义明确的 `none` / `result_cache`。
6. 对未迁移、语义不完整或发生冲突的策略保留 legacy fallback。
7. 对风险等级冲突采用不降低保护等级的 fail-closed 解析规则。

Phase 2 不应顺带修改 Gateway、Authorization、Confirmation、Idempotency、Handler 或 Prompt 的业务行为；执行链改造必须作为后续明确授权的编码任务进行。

### Metadata v2 Phase 3

在所有工具完成迁移、兼容解析稳定且行为得到独立验证后：

1. 删除 legacy policy 字段。
2. 删除 legacy fallback 和双写路径。
3. 由 Metadata v2 成为 Runtime 唯一策略来源。

## 6. 后续开发禁止事项

- 不得把 Phase 1 描述为已经改变 Runtime 行为。
- 不得在 resolver 落地前直接从 metadata 覆盖 legacy 高风险策略。
- 不得为 `optional` / `conditional` confirmation 虚构尚未实现的 Runtime 语义。
- 不得把 `single_flight` 等同于当前全部幂等行为。
- 不得把 `resource_scope` 描述为完整 Authorization policy。
- 不得把 `timeout_seconds` 或 `output_budget` 描述为已被 Runtime 执行。
- 不记录未经当前 checkout 验证的测试数量。
- 不引用不存在的 commit。
- 不宣称尚未实现的功能。
- 未经单独任务授权，不修改 Gateway、Authorization、Confirmation、Idempotency、Handler 或 Prompt。

## 7. 本交接阶段的验证边界

本次只更新交接文档，不运行代码测试，不实施 Metadata v2 Phase 2。提交前仅执行：

```powershell
git diff --check
```
