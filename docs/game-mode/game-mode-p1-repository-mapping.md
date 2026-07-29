# Game Mode P1 Repository Architecture Mapping

- 文档类型：RFC / Repository Architecture Proposal
- 状态：P1 待人工确认
- 架构基线：Game Mode P0、P0.1 与 Architecture Freeze
- 仓库基线：`feature/agent-runtime-v3` / `b18f9553e743eb847854713085101ea02633447c`
- 范围：代码结构勘察、接入位置、模块边界、依赖方向和开发顺序
- 非目标：功能实现、配置变更、分支创建、提交、P2 开发

## 0. 结论摘要

1. 当前仓库没有独立的 `src/`、`app/`、`agent/`、`runtime/`、`memory/`、`tools/`、`gateway/` 或 `audit/` 顶层包；运行逻辑主要集中在 `plugins/`，Agent Tool 基础设施集中在 `plugins/agent_tools/`。
2. QQ 入站链路为 NapCat / OneBot V11 → NoneBot Adapter → matcher 分发。当前不存在中央 Mode Router，也不存在统一 Message Normalizer。
3. Game Mode 的首个接入点应位于 OneBot Adapter 完成事件解析之后、所有现有群消息 matcher 之前。该层只进行活动 Session 所有权判定、`GROUP_TEXT` 规范化和转交，不承载 Game Runtime 业务。
4. 活动游戏群的消息必须在入站早期独占：命中 `RUNNING` 或 `PAUSED` Session 后只进入 Game Runtime，并阻断 Normal Mode、普通命令、群反应、媒体分析、消息归档及其下游 Memory/Graph/Report 链路。
5. `game_runtime/` 必须作为新的顶层运行时包；可以依赖共享的 LLM、Audit、Authorization、Confirmation、Idempotency 和敏感存储基础设施，不得依赖 `plugins/ai_chat.py`、Companion Memory、普通聊天上下文或 Agent Tool Registry。
6. 现有 Audit、Confirmation、Idempotency 均带有明显的 Tool 数据模型。P1 推荐先抽取共享执行原语并保留 Tool 兼容适配器，再增加 `domain=GAME` 适配；不得把 Game Action 注册成 Tool，也不得复制基础设施。
7. V0.1 Game Memory 使用独立 Game State DB + Event Store。结束后的白名单 Episode Summary 通过专用 Agent Memory 端口写入，不能写入玩家画像，也不能被后续 Game Context 检索。

## 1. 约束与判定优先级

本映射以 [game-mode-architecture-freeze.md](/D:/猎bot/qq-reminder-bot/docs/game-mode/game-mode-architecture-freeze.md) 为最高约束。仓库现状只决定“如何接入”，不改变以下冻结决策：

- Game Runtime 是 Agent Runtime 的独立顶层 Mode，不是 Plugin、Tool 或 Memory 子模块。
- 活动游戏群消息只进入一个 Mode。
- DM 仅为 `game_id` 范围内的 Session 权限，不是系统管理员。
- Game Memory 与 Companion/User/Semantic Memory 隔离。
- Hidden Truth 不进入 Hunter Context；不保存完整 Chain of Thought。
- V0.1 使用进程内 Lightweight Hunter Instance，只支持 `GROUP_TEXT`。
- 所有 Game Action 必须经过 Authorization、Audit、Confirmation（按策略）和 Idempotency。

## 2. Repository Reconnaissance

### 2.1 真实目录地图

|逻辑领域|真实位置|当前职责|P1 判定|
|---|---|---|---|
|进程入口|[`bot.py`](/D:/猎bot/qq-reminder-bot/bot.py:6)|初始化 NoneBot、注册 OneBot V11 Adapter、加载插件|保留；不放 Game Runtime 逻辑|
|插件加载|[`pyproject.toml`](/D:/猎bot/qq-reminder-bot/pyproject.toml:40)|显式声明当前插件列表|未来仅登记薄入站适配器，并登记新顶层包|
|Normal Agent|[`plugins/ai_chat.py`](/D:/猎bot/qq-reminder-bot/plugins/ai_chat.py:604)|普通聊天、Context、LLM 循环、Tool 调用|属于 Normal Mode；不得成为 Game Runtime 宿主|
|群消息归档|[`plugins/message_collector.py`](/D:/猎bot/qq-reminder-bot/plugins/message_collector.py:28)、[`plugins/message_archive.py`](/D:/猎bot/qq-reminder-bot/plugins/message_archive.py:257)|收集群消息并写入 archive DB|活动游戏群必须绕过|
|短期群上下文|[`plugins/group_context_service.py`](/D:/猎bot/qq-reminder-bot/plugins/group_context_service.py:17)|按群维护进程内最近消息|活动游戏群不得写入或读取|
|Companion Memory|[`plugins/companion_memory.py`](/D:/猎bot/qq-reminder-bot/plugins/companion_memory.py:356)、[`plugins/companion_registry.py`](/D:/猎bot/qq-reminder-bot/plugins/companion_registry.py:18)|角色画像、长期记忆、群画像和 Companion 配置|与 Game Memory 隔离|
|Semantic Graph|[`plugins/semantic_graph.py`](/D:/猎bot/qq-reminder-bot/plugins/semantic_graph.py:265)|从普通消息归档构建图数据|游戏消息不得进入|
|全局/群功能控制|[`plugins/access_control.py`](/D:/猎bot/qq-reminder-bot/plugins/access_control.py:145)|系统管理员、群功能开关|只承载 Game Mode 启用门；不承载 DM 权限|
|Agent Tool Authorization|[`plugins/agent_tool_access.py`](/D:/猎bot/qq-reminder-bot/plugins/agent_tool_access.py:15)|计算 Tool capability 和调用授权|不得直接表达 Game Session 权限|
|Tool Gateway|[`plugins/agent_tools/gateway.py`](/D:/猎bot/qq-reminder-bot/plugins/agent_tools/gateway.py:243)|Tool 参数、授权、确认、幂等、执行和审计编排|仅复用抽取后的共享原语|
|Tool Metadata|[`plugins/agent_tools/registry.py`](/D:/猎bot/qq-reminder-bot/plugins/agent_tools/registry.py:28)|Tool 注册、Metadata 和 inventory|Game Action 使用独立 metadata domain|
|Audit|[`plugins/agent_tools/audit.py`](/D:/猎bot/qq-reminder-bot/plugins/agent_tools/audit.py:89)|Tool Audit 事件、HMAC、epoch、fingerprint、持久化|共享引擎能力需上移，Tool 模型保留兼容|
|Confirmation / Idempotency|`plugins/agent_tools/confirmation.py`、`plugins/agent_tools/idempotency.py`|Tool 形态的确认票据与执行绑定|抽取 domain-neutral 原语；不直接复用 Tool schema|
|隐私日志边界|`plugins/logging_privacy.py`|OneBot 日志脱敏与 safe 模式|作为共享边界复用|

结论：仓库目前是“插件聚合式 Bot Runtime”，并没有显式的 Agent Runtime Core 包。P1 不能把 Game Runtime继续堆入 `plugins/`；`plugins/` 中只允许存在框架适配层，长期运行状态和决策循环必须位于顶层 `game_runtime/`。

### 2.2 当前 matcher 拓扑

当前群消息会由多个 matcher 按优先级独立消费：

|优先级|代表入口|影响|
|---:|---|---|
|2|`remote_approval`|仅私聊规则，不与 V0.1 群游戏冲突|
|4|Reminder 群功能管理|会早于普通 AI Chat 处理群命令|
|5|Reminder、Companion、Collector、Storage 等命令|活动游戏群若不提前阻断，仍可能触发 Normal 功能|
|20|[`ai_chat`](/D:/猎bot/qq-reminder-bot/plugins/ai_chat.py:604)|普通 Agent 主入口|
|29|`group_reactions`|普通群反应|
|30|[`message_collector`](/D:/猎bot/qq-reminder-bot/plugins/message_collector.py:28)|消息归档|
|31|`media_insights`|媒体处理|

因此，仅在 `ai_chat` 内增加 Game Session 判断不能满足独占约束：优先级 4/5 的普通命令已经可能执行，优先级 29/30/31 的通用消费者也需要被阻断。

## 3. Message Ingress Layer 映射

### 3.1 当前路径

```text
NapCat
  -> OneBot V11 Event
  -> bot.py: NoneBot OneBotV11Adapter
  -> NoneBot matcher dispatch
  -> 各 plugins 自行解析和处理 event
```

- [`bot.py`](/D:/猎bot/qq-reminder-bot/bot.py:11) 注册 Adapter；[`bot.py`](/D:/猎bot/qq-reminder-bot/bot.py:13) 从 `pyproject.toml` 加载插件。
- Adapter 已完成 OneBot Event 类型解析，仓库没有自建 NapCat Adapter。
- 普通聊天直接使用 `event.get_plaintext()`，见 [`plugins/ai_chat.py`](/D:/猎bot/qq-reminder-bot/plugins/ai_chat.py:1158)。
- Archive 另有面向持久化的 segment/event 序列化，见 [`plugins/message_archive.py`](/D:/猎bot/qq-reminder-bot/plugins/message_archive.py:153) 和 [`plugins/message_archive.py`](/D:/猎bot/qq-reminder-bot/plugins/message_archive.py:245)。它会保留原始消息和完整事件数据，不适合作为 GameEvent Context 输入。

### 3.2 推荐接入点

在 Adapter 解析完成后新增一个优先级早于现有群 matcher 的薄适配器，例如 `plugins/game_mode_ingress.py`：

1. 仅接受 `GroupMessageEvent`。
2. 查询轻量、只读的 Game Session Ownership Index：`group_id -> active game_id/lifecycle`。
3. 未命中 `RUNNING`/`PAUSED` Session：不消费事件，现有 matcher 链行为不变。
4. 命中活动 Session：只接受 `GROUP_TEXT`，生成最小 Game Event Envelope，投递至对应 Game Session Actor mailbox。
5. 投递被 Game Runtime 接受后设置阻断，禁止事件继续流向其他 matcher。
6. 投递失败时采取 fail-closed：不回落到 Normal Mode；生成 `SYSTEM_ERROR`/Audit，并按 Failure Model 暂停或告警。

该文件是 NoneBot framework adapter，不是 Game Mode Plugin。它不得包含 Session、Knowledge、Reasoning、Action Queue 或持久化逻辑。

### 3.3 Game Message Normalizer 边界

V0.1 Normalizer 仅输出：

- OneBot message/event identity；
- `group_id`、`qq_id`、时间戳；
- 纯文本、reply/at 的最小引用信息；
- `correlation_id` 和入口接收时间；
- 输入 channel capability=`GROUP_TEXT`。

不应复用 Archive Serializer，因为其目标是完整归档而非最小披露，并可能携带 URL、媒体段或完整 event payload。语音、图片、临时会话和私聊在入口处标记为 V0.1 不支持，不进入 Hunter Context。

## 4. Mode Router 接入设计

### 4.1 当前状态

仓库没有 Mode Router、统一 Dispatcher 或 Intent Router。NoneBot matcher priority/block 机制是当前唯一的跨插件路由机制；`ai_chat` 内部的前缀、提及、提醒意图等判断属于 Normal Mode 内部路由，不能承担 Mode Ownership。

### 4.2 推荐职责分配

```text
plugins/game_mode_ingress.py
  -> game_runtime.routing.resolve_group_ownership(group_id)
  -> NORMAL: 不消费，交还现有 matcher 链
  -> GAME: normalize -> enqueue(game_id) -> block
```

Mode Router 的权威数据来自 Game Session State 的只读索引，不从聊天文本猜测 Mode。只有受信任的 Session Setup Control Plane 可以创建 Session、绑定群和配置 DM/角色；群消息不能自动开启 Game Mode，也不能携带隐藏剧本信息完成初始化。

### 4.3 不应修改的位置

- 不在 `bot.py` 中实现业务路由；入口只负责框架启动。
- 不在 `ai_chat.py` 中把 Game Mode 作为特殊 prompt 或 Tool。
- 不在 Reminder/Companion/Collector 的命令路由中分散实现 Mode 选择。
- 不使用普通 Chat Context 判断群是否处于游戏中。
- 不让路由失败时回落到 Normal Mode。

## 5. Normal Mode Runtime 分析

### 5.1 当前处理流

```text
Group/Private Message
  -> ai_chat matcher
  -> feature / mention / prefix / injection checks
  -> build_local_context
  -> build_agent_system_prompt
  -> ask_ai_with_agent (单请求 Agent loop)
  -> registered Tool Gateway 或 built-in Tool adapter
  -> reply
  -> 可选写入普通消息归档
```

- [`build_local_context`](/D:/猎bot/qq-reminder-bot/plugins/ai_chat.py:2801) 聚合普通知识、最近群消息、群画像、Companion Memory 和 Semantic Graph。
- [`build_agent_system_prompt`](/D:/猎bot/qq-reminder-bot/plugins/ai_chat.py:2158) 组合普通 Bot persona 和 Normal Agent 指令。
- [`ask_ai_with_agent`](/D:/猎bot/qq-reminder-bot/plugins/ai_chat.py:2286) 是按消息创建的 Tool-capable loop，不是长期 Agent Instance。
- 回复可在 [`plugins/ai_chat.py`](/D:/猎bot/qq-reminder-bot/plugins/ai_chat.py:2935) 再写入普通 archive。

### 5.2 可复用与必须隔离

|能力|复用判定|边界|
|---|---|---|
|LLM Provider Client / timeout / transport|可复用|抽取为共享低层接口；不调用 Normal prompt/context 函数|
|Audit HMAC / epoch / fingerprint / append-only / sanitizer|必须复用|通过 Shared Audit Engine 的 `GAME` domain|
|Authorization 基础原语|可复用|Game Session Policy 独立计算，DM 不进入全局管理员模型|
|Confirmation / Idempotency 状态语义|可复用|使用 domain-neutral binding；不伪装成 Tool|
|敏感存储与隐私日志|可复用|保持 fail-closed 和最小披露|
|`build_local_context`|禁止|包含普通聊天、Companion 和 Semantic Graph|
|Normal System Prompt|禁止|角色和知识边界不同|
|普通 Tool loop|禁止|Game Action 不是 Tool，生命周期不同|
|普通聊天历史/归档|禁止|会造成双向污染与隐藏信息泄露|
|Companion/User/Semantic/Graph Memory|禁止|违反 Session 隔离和禁止跨局学习|

为避免反向依赖，Game Runtime 不应导入 `plugins.ai_chat`。未来若抽取共享 LLM transport，应先定义稳定的 shared interface，再由 Normal Mode 和 Game Runtime 分别适配；Normal Mode 的 prompt、上下文和可见行为保持不变。

## 6. Agent Runtime Core 映射

当前代码没有独立 Agent Core：Normal Agent 生命周期、Context、LLM 和 Tool orchestration 都位于 `plugins/ai_chat.py`，每条消息创建一次请求级 loop。

目标逻辑结构为：

```text
Agent Runtime
├── Normal Mode
│   └── existing ai_chat request runtime
└── Game Runtime
    └── Game Session Actor
        └── Lightweight Hunter Instance
```

Game Runtime 与现有 Agent Runtime 的关系是“同一进程内、共享基础设施、隔离业务状态的并列 Mode”。Hunter Instance 在角色和 Session 前置条件完成后创建，绑定单一 `game_id`，由 Actor 串行驱动；结束后销毁，禁止复用上一局实例。长期 Hunter Identity 只能通过只读共享身份端口提供稳定人格/风格，Game Character Identity 保存在 Game State 中。

## 7. Memory System 映射

### 7.1 当前 Memory 架构

```text
message_archive.db
  -> collected_messages
  -> group context / daily report / semantic graph / companion update

companion_memory.db
  -> companion_profiles
  -> companion_memories
  -> group_profiles
  -> companion_knowledge_items

semantic_graph.db
  -> graphs / nodes / edges
```

当前没有发现独立的全局 Vector Store。现有“语义”能力主要是 SQLite 数据、关键字检索和 Semantic Graph；这并不改变 Freeze 对跨局检索和普通 Memory 污染的禁止。

### 7.2 Game Memory 接入方式

`game_runtime/persistence/` 独立拥有：

- Game State DB：Session、Participant、Character、Phase、Knowledge visibility、Reasoning summary、Action state、清理状态；
- Event Store：有序 GameEvent Envelope、state version、correlation 和恢复位置；
- Retention Coordinator：END T+0 summary、T+5 清理、可恢复且幂等的删除任务。

Game DB 不复用 `companion_memory.db`、`message_archive.db` 或 `semantic_graph.db`，不向这些库写入消息、线索、推理或 Hidden Truth。

Episode Summary 是结束后的唯一白名单出口。推荐通过专用 `AgentEpisodeMemoryPort` 写入独立的 Agent episode store，而不是玩家画像/Companion memory；只允许 Freeze 列出的日期、剧本名称、参与玩家、Hunter 角色、胜负和 MVP。新的 Game Context Builder 永远不读取 Episode Summary。

## 8. Audit System 映射

### 8.1 当前状态

[`AuditEvent`](/D:/猎bot/qq-reminder-bot/plugins/agent_tools/audit.py:89) 及其表 [`agent_tool_audit_events`](/D:/猎bot/qq-reminder-bot/plugins/agent_tools/audit.py:266) 以 `tool_name`、Tool call、confirmation 和 idempotency 字段为中心。HMAC persistent key、epoch、fingerprint、sanitizer 和 append-only 写入能力具有共享价值，但当前物理 schema 不能在不伪装 Tool 的前提下直接表达 Game Action。

### 8.2 推荐映射

```text
Shared Audit Engine
├── TOOL adapter -> 现有 Tool Audit contract/table（兼容）
└── GAME adapter -> domain=GAME contract/store
```

P1 建议：

1. 抽取或封装 HMAC、epoch、fingerprint、sanitization、序列化和 append primitives，形成 domain-neutral Shared Audit Engine。
2. 保留现有 Tool Audit API 和表作为兼容适配，不改变 Normal Mode 行为。
3. 新增 Game Audit contract：`domain=GAME`、`game_id`、`session_id`、`actor`、`action/event`、`policy_result`、`correlation_id`、`state_version`。
4. Game Audit payload 只记录结构化决策与摘要，不记录 Hidden Truth、完整 prompt、完整 LLM 输出或 Chain of Thought。
5. V0.1 可使用独立的 GAME 物理表，但必须经过同一 Shared Audit Engine；“共享引擎”不要求立即把历史 Tool 表迁移成单表。

## 9. Authorization System 映射

### 9.1 当前模型

- [`access_control.py`](/D:/猎bot/qq-reminder-bot/plugins/access_control.py:15) 从 `BOT_ADMIN_USER_IDS` 建立全局管理员身份，并维护群功能开关。
- [`is_group_feature_enabled`](/D:/猎bot/qq-reminder-bot/plugins/access_control.py:230) 决定普通功能是否在群中启用。
- [`AgentToolCapability`](/D:/猎bot/qq-reminder-bot/plugins/agent_tool_access.py:15) 及其授权路径面向 Tool、目标群和全局管理员，不适合作为 Session Role Store。

### 9.2 两层权限映射

```text
Global / Group Gate
  AND
Game Session Policy(game_id, participant, lifecycle, phase, action, target)
```

- Global/System Admin：允许通过受信任 Control Plane 启用 Game Mode、创建 Session、指定初始 DM；不自动成为游戏内 DM。
- Group Feature Gate：只表达该群是否允许使用 Game Mode，不表达玩家角色。
- DM：保存在 Game State DB 的 Participant Registry 中，只能对绑定 `game_id` 执行白名单 Session 操作。
- Hunter/Player/Spectator/Unknown：由 Session Registry 判定，不写入全局角色或 Tool capability。
- 任何 Game Action 的有效权限是全局/群门、Session role、lifecycle、phase、action metadata 和 disclosure policy 的交集。

非 DM 发出结束、推进阶段、揭示线索等操作时，Session Policy 拒绝并写 GAME Audit；不得调用全局管理员逻辑，也不得改变 Normal Mode 或 Global Tool 权限。

## 10. Tool / Action Gateway 映射

### 10.1 当前 Tool Gateway

[`execute_tool`](/D:/猎bot/qq-reminder-bot/plugins/agent_tools/gateway.py:243) 编排参数校验、Tool Authorization、Confirmation、Idempotency、handler、timeout、result normalization 和 Audit。该流程的治理语义可复用，但接口、binding 和执行目标均以 Tool 为中心。

### 10.2 Game Action 边界

```text
Decision
  -> Game Action Intent
  -> Action Queue
  -> Game Action Gateway
     -> Session Authorization
     -> Shared Audit
     -> Confirmation Policy（如需要）
     -> Idempotency Claim
     -> GROUP_TEXT Executor
  -> Action Result Event
```

- 不调用 `execute_tool()` 执行 Game Action。
- 不在 Agent Tool Registry 注册 `SPEAK_PUBLIC`、`WAIT`、`UPDATE_MEMORY` 等 Action。
- Action Queue 是 Game Runtime 状态的一部分；同一 `game_id` 的创建、执行和结果提交由 Actor 串行协调。
- QQ 发送超时且无法确认投递结果时进入 `UNKNOWN`，禁止自动重复发送。
- `UPDATE_MEMORY` 是内部状态动作；仍需策略、审计和版本检查，但不调用通信 executor。
- `SEND_PRIVATE`、DM 私聊、语音和图片在 V0.1 metadata 中必须禁用，而不是仅依赖 prompt 约束。

## 11. Metadata System 映射

当前 [`AgentToolMetadata`](/D:/猎bot/qq-reminder-bot/plugins/agent_tools/registry.py:28) 描述 Tool risk、side effect、resource scope、confirmation、idempotency、timeout 等；[`list_agent_tools`](/D:/猎bot/qq-reminder-bot/plugins/agent_tools/registry.py:170) 还是 Tool inventory。现有 Metadata v2 仍有 explicit inventory 与 legacy executable compatibility 边界，Game Mode 不应扩大该迁移范围。

Game Runtime 使用独立 `GameActionMetadata`：

|字段|用途|
|---|---|
|`domain=GAME`|阻止进入 Tool inventory|
|`action_type`|稳定 Action 标识|
|`risk` / `side_effect`|治理和审计|
|`session_permission`|允许的 Session role|
|`allowed_lifecycle` / `allowed_phases`|状态机约束|
|`channel_capability`|V0.1 固定 `GROUP_TEXT` 或 internal|
|`confirmation_policy`|是否需要人类确认|
|`idempotency_policy`|执行绑定和重试限制|
|`timeout_policy`|Executor 超时处理|
|`disclosure_policy`|输出可见性和 Hidden Truth 防泄漏|

可以复用共享枚举与 policy resolver 约定，但 Game metadata 不导入 AgentTool、Tool Registry 或 Tool capability catalog。

## 12. 架构图

### 12.1 当前架构

```text
NapCat / QQ
    |
OneBot V11
    |
bot.py + NoneBot Adapter
    |
NoneBot matcher dispatch（无中央 Mode Router）
    |---------------------|---------------------|------------------|
priority 4/5 commands  priority 20 ai_chat  priority 29 reactions  priority 30/31 collector/media
                            |
                    Normal Context Builder
                     /      |       \
              Companion  Group Chat  Semantic Graph
                            |
                     per-request LLM loop
                            |
                       Tool Gateway
                            |
                Authorization / Audit / Confirmation / Idempotency
```

### 12.2 Game Mode 接入后

```text
NapCat / QQ
    |
OneBot V11 Adapter
    |
Early Mode Ownership Adapter
    |
    +-- no active game --------------------------> existing Normal Mode（行为不变）
    |
    +-- RUNNING / PAUSED game -> normalize GROUP_TEXT -> block downstream matchers
                                           |
                                      Game Runtime
                                           |
                                  Game Session Manager
                                           |
                                GameSession Actor + Mailbox
                                           |
                                 Sequential Event Processing
                                  /        |          \
                         State/Event DB  Hunter     Action Queue
                                         Instance       |
                                            |      Game Action Gateway
                                      Context Builder    |
                                            |      GROUP_TEXT Executor
                                         LLM Client      |
                                                    Action Result Event

Shared infrastructure:
  LLM transport | Audit Engine | Authorization primitives |
  Confirmation | Idempotency | privacy/sensitive storage
```

同一 `game_id` 的 Event、phase transition、state version 和 Action result 串行处理；不同 `game_id` 的 Actor 可以并行运行。外部 QQ I/O 可以异步等待，但结果必须重新投递到原 Actor mailbox 后才能提交状态。

## 13. 修改范围矩阵

|模块|是否修改|P1 推荐范围|必须保持不变|
|---|---|---|---|
|Message Ingress|是|新增薄 `game_mode_ingress`，完成 ownership、normalize、enqueue、block|`bot.py` 的 Adapter 启动职责|
|Mode Router|新增|基于 Session Index 进行确定性群所有权判定|不做文本意图猜测，不承载 Game 业务|
|Agent Core|是，抽取共享层|建立低层 LLM/identity interface|Normal prompt、Context、Tool loop 行为|
|Normal Mode / `ai_chat`|最小修改|增加防御性 ownership guard，并适配共享 LLM interface|普通消息行为、触发条件、回复路径|
|普通命令插件|原则上否|依赖早期 block；以隔离测试证明不会执行|Reminder/Companion/Storage 业务逻辑|
|Collector / Reactions / Media|最小修改|增加防御性 active-game ownership guard|非游戏群处理行为|
|Message Archive|否|不接收活动游戏群数据|schema 与既有数据|
|Companion/User/Semantic/Graph Memory|否|零接入；禁止 Game 数据读写|所有普通 Memory 模型|
|Game Memory|新增|独立 State DB、Event Store、retention|不得跨局检索或建全局向量索引|
|Audit|是，兼容抽取|上移共享密码学/写入原语，增加 GAME adapter/domain|现有 Tool Audit contract 和历史表可用性|
|Authorization|是|增加 Game Mode 群 gate；新增独立 Session Policy|全局管理员和 AgentTool authorization 语义|
|Tool Gateway|否（业务接口）|Game 不调用；仅复用抽取后的执行原语|`execute_tool()` 行为与 Tool registry|
|Confirmation / Idempotency|是，兼容抽取|建立 domain-neutral binding，保留 Tool adapter|现有 Tool token/binding 兼容性|
|Metadata|新增独立 domain|`GameActionMetadata`|AgentTool inventory/provenance 边界|
|Packaging / plugin config|是|声明 `game_runtime` 顶层包和薄适配器|现有插件加载顺序和配置默认值|
|Daily Report / Semantic Graph|否|通过上游不归档保证隔离|现有定时任务与查询逻辑|
|Tests|新增|按仓库现有平铺风格增加隔离和治理测试|现有测试语义|

“最小修改”的 guard 是纵深防御：主隔离边界仍是早期 Mode ownership + matcher block。所有 guard 在不存在活动 Session 时必须是无副作用的快速返回，确保 Normal Mode 行为不变。

## 14. 推荐目录结构

结合当前仓库以 Python 模块和少量子包为主的风格，V0.1 不机械拆成大量空目录。建议先建立以下聚合边界：

```text
game_runtime/
├── api.py                  # 对 ingress/control plane 暴露稳定入口
├── contracts.py            # GameEvent、ActionIntent、Result、可见性值对象
├── routing.py              # group ownership 只读判定
├── session/
│   ├── model.py            # lifecycle、phase、participant、character
│   ├── manager.py          # create/start/pause/end/recover 与实例管理
│   ├── actor.py            # mailbox 和单 game 串行处理
│   └── setup.py            # 受信任 Session Setup Control Plane 端口
├── persistence/
│   ├── state_store.py      # Game State DB
│   ├── event_store.py      # 有序 Event Store / state version
│   └── retention.py        # T+0 summary、T+5 清理
├── cognition/
│   ├── knowledge.py        # Knowledge Store 与 Visibility Policy
│   ├── context.py          # 最小 Hunter Context Package
│   ├── reasoning.py        # 结构化 Reasoning Memory，无完整 CoT
│   ├── timeline.py         # Event timeline、冲突检测
│   └── decision.py         # Observe-Understand-Reason-Decide
├── action/
│   ├── metadata.py         # 独立 GameActionMetadata
│   ├── queue.py            # CREATED/EXECUTING/SUCCESS/FAILED/UNKNOWN
│   ├── gateway.py          # Game Action 治理编排
│   └── executor.py         # V0.1 GROUP_TEXT 执行端口
└── adapters/
    ├── audit.py            # Shared Audit GAME adapter
    ├── authorization.py    # Session Policy 与 shared gate adapter
    ├── llm.py              # shared LLM transport adapter
    └── communication.py    # OneBot GROUP_TEXT adapter contract

plugins/
└── game_mode_ingress.py    # 唯一 NoneBot 薄入站适配器，不是 Game Runtime 宿主
```

共享基础设施建议在后续实现中逐步上移到中立命名空间，例如 `runtime_core/`：Audit primitives、execution primitives、LLM transport、privacy/sensitive storage。必须保留 `plugins/agent_tools/` 兼容 facade，避免一次性迁移 Normal Mode。

Episode Summary 建议通过中立端口和独立 store 持久化；它既不属于 Game Session DB 的长期保留数据，也不属于 Companion 玩家画像。具体顶层命名在 G1 以现有 packaging 约束确认，但其依赖边界已经冻结。

## 15. 依赖方向

### 15.1 允许

```text
plugins/game_mode_ingress
  -> game_runtime.api

game_runtime
  -> runtime_core interfaces
     -> audit / authorization / confirmation / idempotency / LLM / privacy

game_runtime.adapters.communication
  -> OneBot GROUP_TEXT send port
```

### 15.2 禁止

```text
game_runtime -> plugins.ai_chat
game_runtime -> plugins.companion_memory
game_runtime -> plugins.group_context_service
game_runtime -> plugins.message_archive
game_runtime -> plugins.semantic_graph
game_runtime -> plugins.agent_tools.registry / execute_tool

shared infrastructure -> game_runtime
Normal Mode -> Game State internals
```

依赖必须指向接口而不是具体 DB 或 plugin handler。Game Runtime 可以调用共享能力；共享能力不得识别 Character、Clue、DM 或 Game Phase。

## 16. Implementation Placement Proposal

### Phase G0：边界测试与接口冻结

- 固定 active-session ownership 查询接口、GameEvent/Action contracts 和 GAME Audit contract。
- 建立 Normal Mode 不变、活动游戏群独占、Hidden Truth 不出 Context 的测试基线。
- 明确 `pyproject.toml` 的顶层包发现方式。

### Phase G1：共享基础设施兼容抽取

- 抽取 Shared Audit、domain-neutral confirmation/idempotency 和 LLM transport 接口。
- 保留所有 Agent Tool facade 和现有行为。
- 不创建 Game Session，不接管消息。

### Phase G2：Game Runtime Skeleton + Persistence

- 建立 `game_runtime/`、contracts、Game State DB、Event Store、schema/version 与 recovery boundary。
- 建立 Session lifecycle 和独立 Game phase state machine。

### Phase G3：Session Actor + Event Ordering

- 实现每 `game_id` mailbox、串行处理、不同 Session 并行和 state version CAS/commit。
- 覆盖同时发言、DM phase command、Action result 冲突和恢复事件。

### Phase G4：Mode Router + GROUP_TEXT Ingress

- 增加薄入站适配器和 Session Ownership Index。
- 验证 `RUNNING`/`PAUSED` 群事件不会触发任何 Normal matcher、archive 或 Memory。
- 路由异常 fail-closed，不回落 Normal Mode。

### Phase G5：Participant / Knowledge / Context

- Participant Registry、Session scoped DM policy、Knowledge Visibility、Character Instance。
- Context Builder 只接收最小可见知识和结构化 Reasoning Summary。
- 建立 Hidden Truth 泄漏的负向测试。

### Phase G6：Hunter Instance + Decision Loop

- 进程内 Lightweight Hunter 生命周期。
- Observe → Understand → Reason → Decide；不持久化完整 CoT。
- 不启用 AI DM、多 AI、跨局学习或玩家画像。

### Phase G7：Action Queue + Gateway

- Action metadata、Authorization、Audit、Confirmation、Idempotency、UNKNOWN 语义。
- 先支持 internal action，再接 `SPEAK_PUBLIC` 的 GROUP_TEXT executor。

### Phase G8：End / Retention / Recovery

- END T+0 白名单 Episode Summary。
- T+5 安全、幂等清理与 Audit Metadata 保留。
- 覆盖 LLM、通信、存储和恢复失败的安全降级。

每一阶段都必须保持 Feature Gate 默认关闭，并以 Normal Mode 回归和隔离测试为进入下一阶段的门槛。

## 17. 建议测试映射

遵循当前 `tests/` 平铺风格，后续实现阶段建议新增：

- `test_game_mode_router.py`
- `test_game_session.py`
- `test_game_event_actor.py`
- `test_game_context_visibility.py`
- `test_game_action_gateway.py`
- `test_game_audit_domain.py`
- `test_game_memory_retention.py`
- `test_game_normal_mode_isolation.py`

关键验收不是仅验证 Game 回复成功，而是证明：同一输入只被一个 Mode 消费、无普通归档/Memory 写入、无 Hidden Truth/CoT 泄漏、DM 权限不能越出 `game_id`、`UNKNOWN` 通信动作不自动重发。

## 18. P1 风险与实现前置检查

|风险|影响|P2 前置检查|
|---|---|---|
|matcher 优先级/block 语义理解错误|活动消息可能被双消费|用最小集成测试验证早期 matcher 对优先级 4/5/20/29/30/31 的阻断|
|Session Ownership Index 与持久状态不一致|错误路由或 Normal 回落|索引必须从权威 State Store 恢复，并带 lifecycle/version|
|共享 Audit 抽取破坏 Tool 兼容|影响生产 Agent Runtime|先加兼容契约测试，不迁移历史表|
|LLM interface 抽取携带 Normal Context|Game 信息隔离失效|shared 层只允许 transport/config，不允许 prompt/memory|
|只依赖入口 block|直接调用 handler 或未来新 matcher 可能旁路|对通用消费者增加轻量防御 guard，并建立“不写入”断言|
|Episode Summary 被新游戏检索|形成跨局学习|专用端口、专用 store、Game Context hard deny|
|发送超时被重试|群内重复发言|持久化 `UNKNOWN`，只允许人工/可证明的 reconciliation|

上述事项是实现验证点，不重新打开 Architecture Freeze 决策。

## 19. P1 完成边界

本文件完成了：

- 当前仓库模块与消息链路映射；
- Game Runtime 的真实挂载位置；
- Normal Mode、Memory、Audit、Authorization、Gateway 和 Metadata 的复用/隔离边界；
- 修改范围、新目录、依赖方向和分阶段开发顺序。

P1 到此停止。后续只有在人工确认本映射后才能进入 P2；本阶段不实现任何运行时代码。
