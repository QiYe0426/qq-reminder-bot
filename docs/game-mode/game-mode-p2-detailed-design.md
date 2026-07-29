# Game Mode P2 Detailed Design

- 文档类型：RFC / Architecture Design
- 状态：Accepted / P2 Detailed Design Baseline
- 架构基线：[Architecture Freeze](/D:/猎bot/qq-reminder-bot/docs/game-mode/game-mode-architecture-freeze.md)、[P1 Repository Mapping](/D:/猎bot/qq-reminder-bot/docs/game-mode/game-mode-p1-repository-mapping.md)
- 实现约束：[P2.1 Implementation Constraint Freeze](/D:/猎bot/qq-reminder-bot/docs/game-mode/game-mode-implementation-freeze.md)
- 仓库基线：`feature/agent-runtime-v3` / `b18f9553e743eb847854713085101ea02633447c`
- 目标版本：Game Mode V0.1 / Level 2 AI Player
- 范围：模块契约、数据流、生命周期、权限与 Memory 边界、失败恢复、扩展边界
- 非目标：代码、目录创建、配置修改、数据库实现、分支与提交

## 0. 设计结论

1. `game_runtime/` 是顶层有状态 Runtime；`plugins/game_mode_ingress.py` 仅是未来的 NoneBot 薄适配器。
2. 每个 `game_id` 对应一个逻辑 Session Actor 和一个 Lightweight Hunter Instance。同局只有 Actor 能提交状态，不同游戏可在同一进程异步并行。
3. GameEvent 是唯一运行时输入协议。外部事件先以 `RECEIVED` 状态持久化，再由 Actor 顺序处理；事件处理结果、Game State、Action Outbox 和 `state_version` 在同一提交边界内落盘。
4. Event Store 提供可追踪、可恢复的事件日志，但 V0.1 不采用完整 Event Sourcing。Game State DB 仍是恢复的权威快照，Event Store 用于去重、追踪、补偿和未来 Replay 基础。
5. Knowledge Store 不直接暴露给 LLM。Hunter LLM 唯一入口为 Visibility Policy 过滤后的 Hunter Context Package；Hidden Truth 的查询类型不对 Context Builder开放。
6. Reasoning Space 只保存结构化 Hypothesis、Evidence Reference、Confidence、Conflict 和 Unknown，不保存完整 Chain of Thought、旧 Prompt 或模型自由文本思考。
7. Decision 不能直接产生外部副作用。所有 Action Intent 经持久 Action Queue、Session Authorization、Shared Audit、必要 Confirmation、Idempotency 和 Disclosure Check 后执行。
8. QQ 发送结果无法确认时必须进入 `UNKNOWN`，禁止自动重发、禁止把 UNKNOWN 当作 FAILED 或 SUCCESS。
9. Storage、Visibility、Recovery 一致性错误 fail closed 并局部暂停 Session。活动群 ownership 在 PAUSED/恢复失败期间仍归 Game Runtime，不能回落 Normal Mode。
10. Game Episode Summary 是唯一长期输出，使用独立 Memory Namespace；Game Context Builder 永不读取该 Namespace。

## 1. 总体模块设计

### 1.1 逻辑目录与边界

以下是实现阶段的目标模块边界，不代表本阶段创建目录：

```text
game_runtime/
├── api                       # 对 ingress/control plane 的稳定门面
├── routing                   # group ownership 查询
├── session                   # Session 聚合根、生命周期、Phase、恢复协调
├── event                     # GameEvent contract、ingest、去重、Event Store
├── actor                     # 每 game_id mailbox、单写者和任务协调
├── identity                  # 长期 Hunter Template 与本局 Character Binding
├── participant               # QQ 与 DM/PLAYER/SPECTATOR/UNKNOWN 映射
├── knowledge                 # 分区知识、Visibility Policy、Clue/Evidence
├── context                   # Hunter Context Package 唯一构建出口
├── reasoning                 # 结构化推理状态
├── timeline                  # 时间、地点、行为、冲突视图
├── decision                  # Trigger、Reasoning Job、Level 2 决策
├── action                    # Intent、Queue、Gateway、Executor contract
├── persistence               # State/Event/Action/Retention 存储端口
└── adapter                   # OneBot、LLM、Audit、Auth、Episode Memory 适配

plugins/
└── game_mode_ingress         # 未来的 NoneBot 薄入口；不拥有 Game 状态
```

`api` 和 `routing` 是外部调用边界；其余模块不能被普通 Plugin 直接调用内部写接口。`persistence` 是端口集合，不允许业务模块绕过聚合根直接更新别的模块的数据。

### 1.2 模块契约矩阵

|模块|核心职责|主要输入|主要输出|允许依赖|生命周期|
|---|---|---|---|---|---|
|`session`|聚合根、Lifecycle/Phase、ownership、恢复与结束|Control Operation、已验证 GameEvent|State Transition、Session Snapshot、后续 Event|event、participant、identity、persistence、policy adapter|从 CREATED 到 ENDED，记录 T+5 清理|
|`event`|统一 Envelope、Schema、去重、顺序、Event Store|Platform Event、内部 Event、Action Result|带 `sequence_no` 的持久 Event|persistence、privacy adapter|随 Session 建立，T+5 删除内容|
|`actor`|Mailbox、单写者、顺序处理、异步 Job 协调|已持久 GameEvent|原子提交、Job/Action 请求|session、event、decision、action|RUNNING/PAUSED ownership 期间存在|
|`identity`|Template 与 Character Instance 分离|Template/Policy 版本、Character Binding|Instance Identity、只读人格片段|session、knowledge|Template 长期；Binding 单局|
|`participant`|QQ→Participant、Session Role、替换/失效|Setup 数据、群事件 actor ref|Participant Principal、Session Permission|session、audit adapter|单局，T+5 清理身份映射|
|`knowledge`|知识分区、可见性、生效阶段、来源|Statement、Clue、Setup 输入|Visible Knowledge View、Knowledge Change Event|participant、timeline、persistence|单局，ENDED 后离线，T+5 清理|
|`context`|最小必要 Context、预算、Manifest、注入隔离|Observation、Character、Phase、Visible View、Reasoning Summary、Goal|Hunter Context Package|identity、knowledge、reasoning、timeline|每个 Job 派生，不是恢复事实|
|`reasoning`|结构化假设、证据引用、冲突和未知项|Reasoning Result、Evidence/Timeline 变化|Reasoning Snapshot/Delta|knowledge、timeline、persistence|绑定 Instance，T+5 清理|
|`timeline`|业务时间、地点、行动、关系和冲突视图|Statement、Evidence、Clue、Phase Event|Timeline Slice、Conflict Signal|knowledge、persistence|单局，T+5 清理|
|`decision`|Trigger 分级、异步推理、Level 2 行动选择|Observation、Trigger、Context Package|Decision Record、Action Intent 或 WAIT|context、reasoning、action contract、LLM adapter|仅 ACTIVE Instance 运行|
|`action`|副作用前治理、队列、执行和结果收束|Action Intent、Session/Phase Policy|Action Result Event|participant、adapter、persistence、shared governance|单局；终态保留至 T+5|
|`adapter`|平台与共享基础设施的防腐层|领域 contract|平台/共享服务结果|OneBot、LLM、Audit、Auth、Memory ports|进程级；不拥有领域状态|
|`persistence`|原子事务、版本、查询和清理端口|领域写集合|提交结果、Snapshot、Recovery View|存储驱动|进程级端口，数据按 Session 生命周期|

### 1.3 模块依赖规则

允许：

```text
ingress -> game_runtime.api
game_runtime domain -> game_runtime ports
game_runtime adapter -> shared infrastructure / OneBot / LLM
```

禁止：

```text
game_runtime -> plugins.ai_chat
game_runtime -> Companion/User/Semantic/Graph Memory
game_runtime -> Agent Tool Registry / execute_tool
shared infrastructure -> game_runtime domain
adapter -> 直接修改 Session State
LLM/Executor callback -> 直接修改 Session State
```

## 2. 核心数据契约

### 2.1 GameSession 聚合根

|字段|语义|约束|
|---|---|---|
|`game_id`|游戏局标识|全局唯一，不复用|
|`session_id`|运行时 Session 标识|V0.1 与 game 一一对应，语义独立|
|`group_id`|绑定 QQ 群|同一群最多一个 RUNNING/PAUSED Session|
|`lifecycle`|CREATED/RUNNING/PAUSED/ENDED|决定 Runtime 是否处理与执行|
|`phase`|LOBBY/INTRODUCTION/EXPLORATION/DISCUSSION/VOTING/ENDING|决定游戏规则与可见性|
|`dm_participant_id`|当前 DM|Session scoped，不是系统管理员|
|`hunter_instance_id`|Lightweight Instance 引用|RUNNING 前必须存在且 READY|
|`hunter_character_id`|本局角色|不得写入长期 Agent Identity|
|`state_version`|聚合根版本|每个成功状态提交单调递增|
|`policy_version`|Game Policy 版本|Instance 生命周期内锁定|
|`template_version`|Hunter Template 版本|Instance 生命周期内锁定|
|`ownership_state`|群路由 ownership 派生状态|RUNNING/PAUSED 必须为 GAME|
|`recovery_status`|NONE/VALIDATING/READY/FAILED|不是新增 Lifecycle|
|`created_at/started_at/ended_at`|生命周期时间|由持久层统一生成|
|`retention_due_at`|T+5 清理时间|ENDED 时写入，Summary 失败不影响|

聚合根不保存完整聊天历史、LLM prompt、LLM 原始输出或完整推理文本。

### 2.2 GameEvent Envelope

|字段|语义|约束|
|---|---|---|
|`event_id`|全局唯一事件 ID|重试保持稳定|
|`game_id/session_id`|所属作用域|禁止跨 Session 转发|
|`event_type`|受控事件枚举|每类具有独立 payload schema|
|`source`|PLATFORM/CONTROL/DERIVED/ACTION/RECOVERY/SYSTEM|不包含敏感正文|
|`actor`|Participant/System principal 引用|未知身份显式 UNKNOWN|
|`timestamp`|业务发生时间|不决定处理顺序|
|`received_at`|Runtime 接收时间|用于观测和延迟分析|
|`payload_ref`|最小类型化数据或短期 Observation 引用|不复制完整 Knowledge Store|
|`visibility`|PUBLIC/CHARACTER_PRIVATE/DM_CONTROL/SYSTEM_ONLY|必须通过 schema 校验|
|`observed_state_version`|生产时看到的版本|控制 stale command|
|`correlation_id`|一次输入/推理/Action 链|贯穿 Event 与 Audit|
|`causation_event_id`|直接父 Event|派生事件必填|
|`sequence_no`|Session 内持久顺序|Event Store 原子分配、单调递增|
|`processing_status`|RECEIVED/APPLIED/REJECTED/DEFERRED|用于恢复，不替代业务结果|

### 2.3 核心事件类型

|Event Type|生产者|主要消费者|状态影响|
|---|---|---|---|
|`MESSAGE_RECEIVED`|OneBot ingress adapter|Observation/Participant pipeline|先进入短期 Observation，不直接成为事实|
|`DM_COMMAND`|已认证命令解析器|Session/Phase Controller|经授权后可能转换状态|
|`PLAYER_STATEMENT`|Understanding pipeline|Knowledge/Timeline/Trigger|写声明及来源，不自动升级为事实|
|`CLUE_REVEALED`|合法 DM/Phase Rule|Knowledge/Timeline|创建新的可见 Clue；不暴露 Hidden Truth 对象|
|`PHASE_CHANGED`|Session Controller|Knowledge/Decision/Action|原子 Phase 转换结果|
|`ACTION_REQUESTED`|Decision/Control Operation|Action Queue|持久 Intent|
|`ACTION_COMPLETED`|Executor adapter|Actor/Decision|收束 SUCCESS/FAILED/UNKNOWN|
|`SESSION_RECOVERY`|Recovery Coordinator|Actor/Session|记录恢复开始与结果|
|`SYSTEM_ERROR`|任一 Runtime 模块|Failure Policy|按严重度降级或暂停|

P2 允许增加内部事件，例如 `OBSERVATION_CLASSIFIED`、`REASONING_REQUESTED`、`REASONING_COMPLETED` 和 `KNOWLEDGE_CHANGED`。新增类型必须声明 schema、visibility、生产者、消费者、持久化和失败策略，不能使用自由字符串绕过治理。

### 2.4 Participant

|字段|语义|
|---|---|
|`participant_id`|Session 内稳定引用|
|`game_id`|强制 Session namespace|
|`qq_id_fingerprint`|QQ 映射的隐私安全引用；明文只在必要受控映射中|
|`type`|DM/PLAYER/SPECTATOR/UNKNOWN|
|`character_id`|可空；玩家角色绑定|
|`status`|ACTIVE/REPLACED/LEFT/REVOKED|
|`public_information`|全群公开身份信息|
|`permission_set`|Session action 白名单引用|
|`binding_version`|替换玩家或角色后递增|

Participant Registry 只回答“当前 Session 中此 QQ 是谁、能做什么”。它不授予 Global Permission，不查询或修改 `BOT_ADMIN_USER_IDS`，也不向其他 Session 暴露映射。

### 2.5 KnowledgeRecord

|字段|语义|
|---|---|
|`knowledge_id`|单局唯一 ID|
|`game_id`|Session namespace|
|`category`|BACKGROUND/STATEMENT/EVIDENCE/CLUE/EVENT/RELATION|
|`visibility_class`|PUBLIC/CHARACTER_PRIVATE/HIDDEN_TRUTH/REASONING_ONLY|
|`principal_scope`|允许角色/Instance；PUBLIC 时为空|
|`content_ref`|受控内容记录，不进入 Audit|
|`source_refs`|来源 Event/Evidence ID|
|`valid_from_phase/valid_to_phase`|阶段有效性|
|`status`|ACTIVE/RETRACTED/CONFLICTED/SUPERSEDED|
|`confidence_kind`|ASSERTED/REPORTED/INFERRED/UNKNOWN|
|`created_state_version`|创建版本|

Hidden Truth 必须使用独立查询端口或物理分区。Context Builder 的依赖接口不返回 `HIDDEN_TRUTH` 类型。

### 2.6 ReasoningState

|结构|允许内容|禁止内容|
|---|---|---|
|Hypothesis|对象、命题、状态、置信度档位|模型逐步推导文本|
|Evidence Reference|Knowledge/Event ID、支持/反驳关系|复制线索全文|
|Conflict|冲突类型、相关引用、未决状态|玩家人格评价|
|Unknown|缺失信息、可验证问题、优先级|无来源猜测作为事实|
|Disclosure State|是否已公开、公开引用|私密内容正文|

每次更新保存结构化 Delta 和归一化 Snapshot。LLM 原始输出通过 schema 校验后只提取允许字段，随后按短 TTL 丢弃；失败输出不得部分写入。

### 2.7 ActionIntent

|字段|语义|
|---|---|
|`action_id`|全局唯一；重试保持稳定|
|`game_id/session_id/agent_instance_id`|强绑定|
|`action_type`|SPEAK_PUBLIC/ASK_DM_GROUP/WAIT/UPDATE_MEMORY 等|
|`target_scope`|V0.1 只能是绑定群或 internal|
|`payload_ref`|待发送内容或结构化更新引用|
|`source_event_id/correlation_id`|因果链|
|`created_state_version/phase`|执行前重验|
|`disclosure_manifest`|允许引用的 Knowledge IDs|
|`authorization_policy`|所需 Session permission|
|`confirmation_policy`|高影响操作确认策略|
|`idempotency_key`|Session 内执行绑定|
|`expires_at/priority`|调度和失效|

## 3. Session Manager Detailed Design

### 3.1 职责

Session Manager 是 GameSession 聚合根的唯一管理入口，负责：

- 创建、启动、暂停、恢复、结束 Session；
- 维护 `group_id -> game_id` ownership；
- 校验 Lifecycle 与 Phase 组合；
- 绑定 Participant、Hunter Character、Template/Policy Version；
- 创建、暂停、恢复和销毁 Hunter Instance；
- 协调 Event Store、Action Queue、Episode Summary 和 T+5 Retention；
- 对外提供只读 Session Snapshot 和 ownership 查询。

它不解析自然语言、不调用 LLM、不发送 QQ 消息、不读取 Companion Memory。

### 3.2 创建与 Setup

Session 创建流程：

```text
Trusted Setup Request
  -> Global/Group Gate
  -> create game_id + session_id
  -> bind group and initial DM
  -> lifecycle=CREATED, phase=LOBBY
  -> persist participants/character/knowledge partitions
  -> validate setup manifest
  -> provision Hunter Instance to READY
```

Setup Control Plane 是受信初始化端口，不是 V0.1 UI，也不是普通群消息命令。它可以在启动前写入 Hunter Character Private Knowledge 与 Hidden Truth；写入必须带来源、visibility、principal scope、policy version 和审计摘要。

DM 在游戏运行中可以通过群内公开命令提供公开剧本信息；任何私密信息、Hidden Truth 或角色秘密不能通过群消息录入。需要新增私密内容时，V0.1 只能暂停 Session 后使用受信 Setup/Repair Control Plane。

### 3.3 启动事务

`CREATED/LOBBY -> RUNNING/INTRODUCTION` 必须原子完成：

1. 校验 Game Mode 群 gate、DM、Participant 和角色绑定。
2. 校验最小 Public/Character Private Knowledge 与 Hidden Truth 分区完整性。
3. 校验 Hunter Instance 为 READY，Policy/Template 版本已锁定。
4. 校验该 `group_id` 没有其他 RUNNING/PAUSED Session。
5. 写入 Lifecycle/Phase、ownership row、`PHASE_CHANGED`/启动事件和新 `state_version`。
6. 提交后激活 Actor 与 Hunter Instance。

只有第 5 步提交成功，Mode Router 才能把群消息路由到 Game Runtime。启动失败保持 CREATED，不产生部分 ownership。

### 3.4 Lifecycle 与 Phase

Session Lifecycle：

```text
CREATED -> RUNNING <-> PAUSED -> ENDED
CREATED ---------------------> ENDED
RUNNING ---------------------> ENDED
```

Game Phase：

```text
LOBBY -> INTRODUCTION -> EXPLORATION <-> DISCUSSION <-> VOTING -> ENDING
```

规则：

- CREATED 只允许 LOBBY；RUNNING 允许 INTRODUCTION 至 ENDING。
- PAUSED 保留原 Phase，禁止普通 Observation 推理、LLM 和外部 Action。
- PAUSED 仍拥有群消息 ownership，只处理恢复、结束、受控修复和 Global Control Plane。
- ENDED 是终态且 Phase 为 ENDING；同一 Session 不可重新开始。
- V0.1 关键 Phase 只能由合法 DM 命令或授权 System end 操作推进，定时器不能自动推进。
- DM 不能跳过状态机，也不能通过命令修改 Global Permission。

### 3.5 Pause 与 End

Pause 原子写入 Lifecycle、暂停原因、Instance=SUSPENDED、Action 调度状态和 Audit；已 `EXECUTING` 外部 Action 不能假设已取消，其最终结果仍回到 mailbox 收束。

End 顺序：

1. 授权、必要 Confirmation 和 expected version 校验。
2. Phase 进入 ENDING，停止新 Reasoning Job 和 Action 创建。
3. 取消未执行 Action；执行中 Action收束为终态或 UNKNOWN。
4. Lifecycle 进入 ENDED，写 `ended_at` 与 `retention_due_at`。
5. 关闭 Game Memory 在线读取并销毁 Instance 能力。
6. 从 active ownership index 解除群绑定；后续群消息恢复 Normal Mode。
7. 使用公开 Session Metadata 和公开最终结果生成白名单 Episode Summary。

Episode Summary 失败只写 Audit，不回滚 ENDED，不推迟 T+5 清理。

### 3.6 Persistence 与 Recovery

State Store 保存当前权威 Snapshot；Event Store 保存可追踪事件；Action Store 保存副作用状态。三者在同一存储事务能力内维持版本约束，V0.1 不从全量事件重建所有状态。

重启恢复：

1. 扫描持久化 RUNNING/PAUSED Session，并立即恢复 GAME ownership，禁止消息回落 Normal Mode。
2. 设置 `recovery_status=VALIDATING`；Lifecycle 不新增 RECOVERING 状态。
3. 校验 Session/Phase、Event sequence、state version、Participant/Character/Policy、Knowledge 引用和 Action 状态。
4. 重建 Actor、Mailbox、Hunter Instance 和 Context 派生缓存。
5. 原 RUNNING 且全部通过时恢复 ACTIVE；原 PAUSED 保持 SUSPENDED。
6. 任一关键校验失败则 Lifecycle=PAUSED、recovery_status=FAILED，不启动 LLM/Action，等待授权修复或结束。

不得从普通聊天记录、旧 Prompt 或 LLM 输出猜测恢复状态。

## 4. Game Event Pipeline Design

### 4.1 端到端数据流

```text
OneBot Platform Event
  -> Early Mode Ownership Adapter
  -> GROUP_TEXT Normalizer
  -> GameEvent Schema/Visibility Validation
  -> Event Store append(RECEIVED, sequence_no)
  -> Session Actor Mailbox notification
  -> Actor validate/authorize/classify/apply
  -> atomic commit(Event result + State + Outbox + state_version)
  -> Trigger/Reasoning Job or Action Queue
```

入口只有在 Event Store 成功接受事件后才确认 Game Runtime 已接管。持久化失败时仍阻断 Normal Mode，并按 Storage Failure 处理；不得为了“可用性”把原消息交回普通 matcher。

### 4.2 两阶段持久语义

为同时满足“先持久后消费”和“Event+State 原子提交”，Event Store 使用两个阶段：

1. Ingest Transaction：通过来源去重键插入 `RECEIVED` Event，并分配 `sequence_no`。
2. Apply Transaction：Actor 加载当前 Snapshot，校验 Event，原子写入业务状态、Event 的 APPLIED/REJECTED 状态、派生 Event/Action Outbox 和新 `state_version`。

若 Apply Transaction 失败，Event 保持 RECEIVED/DEFERRED，Session 暂停或由 Recovery 重试；不会出现“状态已改但 Event 未记”或“Action 已发但状态未提交”。

### 4.3 顺序与去重

- 顺序范围仅为单个 `game_id`；跨游戏不提供全局顺序。
- `sequence_no` 在 Ingest Transaction 中按 Session 单调分配。
- Actor 按最小未终结 `sequence_no` 处理，不能因消息类型重排已持久事件。
- DM/Recovery/System 通过预留 mailbox 容量和暂停低价值推理获得可用性，不通过越过已提交事件改变因果顺序。
- 平台事件使用 `platform_event_id + group_id` 来源唯一键；内部事件使用稳定 `event_id`。
- 重复事件返回已有 ingest/apply 结果，不再次修改 State 或创建 Action。
- 采用 at-least-once 投递 + idempotent apply，不宣称端到端 exactly-once。
- `timestamp` 只进入 Timeline 的业务时间解释，不决定 Actor 提交顺序。

### 4.4 Observation 生命周期

`MESSAGE_RECEIVED` 只保存最小 Envelope。消息正文进入短生命周期 Observation Buffer：

- 仅供 Participant 解析、Statement/Command 分类和当前 Context 使用；
- 带 TTL 和 `game_id` namespace；
- 分类完成且不再需要，或 TTL 到期后删除；
- 不进入普通 Message Archive、Companion Memory、Semantic Graph、日报或 Audit；
- 派生 Statement 只保存必要语义和来源引用，不等同于完整聊天记录。

### 4.5 Event 与 Audit 的分工

Event Store 用于 Session 恢复和业务追踪；Shared Audit 用于证明授权、策略、执行和状态边界。Audit 记录安全 fingerprint、事件/动作类型、policy result、state version before/after 和错误类别，不记录玩家原话、剧本正文、Hidden Truth、Private Knowledge、Context 或 Reasoning 正文。

## 5. Game Session Actor Design

### 5.1 Actor 职责

每个活动 `game_id` 只有一个逻辑 Actor：

- 持有 Session 单写者权；
- 驱动 Event apply 和 `state_version`；
- 启停 Hunter Instance；
- 创建异步 Reasoning Job 与 Action Intent；
- 处理 Job/Action Result Event；
- 执行 backpressure、故障隔离和安全暂停。

Actor 不等于线程、进程或 Container。V0.1 使用同一进程的异步任务；不同 Session 可并行。

### 5.2 Mailbox

Mailbox 保存持久 Event 的引用而非敏感 payload 副本。入队前 Event 必须已落 Event Store。Mailbox 具备：

- 有界容量；
- 单 Session sequence cursor；
- 控制事件预留容量；
- 重复通知合并；
- 低价值 Reasoning Trigger 合并；
- 硬上限时停止新推理/主动发言并告警。

权威 Event 不因背压丢弃。若无法安全接收，Session 暂停；Group ownership 不释放。

### 5.3 单事件处理

```text
load next Event
  -> verify game/session/sequence/schema
  -> resolve Participant principal
  -> authorize operation
  -> validate lifecycle/phase/observed version
  -> derive State Delta and Outbox
  -> atomic persist
  -> publish committed follow-ups
```

权限拒绝、stale command 或 schema 错误产生 REJECTED 处理结果和安全 Audit，但不递增业务状态版本；若审计策略要求事件处理游标推进，可更新 Event 处理状态而不修改 Game State。

### 5.4 异步 LLM 与外部 I/O

Actor 不在长 LLM/QQ I/O 中持有写权：

1. 先提交 `REASONING_REQUESTED` 或 `ACTION_REQUESTED` 及输入版本。
2. 异步 Worker 读取不可变 Context/Intent。
3. Worker 只产生 Result Event，不直接写 State。
4. Result 回到 mailbox 后，Actor重新检查 `game_id`、Instance、Phase、state version 和 Manifest。
5. 陈旧 Reasoning Result 被丢弃、降级或重新请求；陈旧 Action 在执行前取消。

同一 Hunter Instance 同时最多一个深度 Reasoning Job。新 Trigger 合并到 pending trigger set，不并行启动多个深度推理。

### 5.5 并发冲突处理

- 玩家同时发言：按 `sequence_no` 应用，Timeline 保留各自业务时间。
- DM 推进 Phase：使用 observed version；若排队期间版本变化则重验，依赖旧 Phase 时拒绝。
- Participant 替换：递增 binding version；旧身份产生的未执行 Action/Command 重新授权。
- Action Result：只通过 Event 提交；Executor 永远不是第二个 State Writer。
- Restart：Event ID、sequence cursor、state version 和 Action idempotency 共同去重。

## 6. Identity 与 Participant Detailed Design

### 6.1 三层身份

```text
Long-term Hunter Agent Template
  + Game Policy Version
  + Game Character Binding
  = Hunter Agent Instance Identity
```

- Hunter Template：长期人格、表达风格和安全基线；只读、版本化。
- Game Policy：Level 2 主动程度、Phase/Action/Disclosure 规则；单局锁定版本。
- Character Binding：本局角色、公开身份、私有目标；只存在于 Game Memory。

角色不能写入长期 Agent Identity，否则角色秘密、语言行为和关系会跨局残留，并破坏 Episode Summary 之外禁止跨局学习的边界。

### 6.2 DM Session Permission

DM 权限判定输入必须包含：

```text
game_id + participant_id + binding_version
+ lifecycle + phase + requested_operation + observed_state_version
```

允许操作：开始、暂停、恢复、结束、推进合法 Phase、公开揭示信息、修改角色/替换玩家及受信 Setup/Repair 操作。高影响操作按 policy 要求 Confirmation。

禁止：修改系统配置、调用 Normal Agent/Tool、访问其他 Session、修改 Global Permission、绕过 Phase/Visibility、通过群消息录入私密知识。

### 6.3 Global Control Plane 穿透

活动群只允许显式 Global Control Plane 在 Mode Router 之前处理极小白名单，例如系统管理员关闭服务。该通道：

- 使用独立、强认证的全局命令 contract；
- 不进入 Normal Mode Agent Loop；
- 不开放提醒、搜索、普通 Tool 或业务状态修改；
- 不继承 DM 权限，也不把 System Admin 自动映射为 DM；
- 所有操作使用 Global Audit domain，并与 GAME Event 通过 correlation 关联。

## 7. Knowledge System Detailed Design

### 7.1 四个分区

|分区|写入者|可读取主体|是否进入 Hunter Context|生命周期|
|---|---|---|---|---|
|Public Knowledge|Setup、合法 DM reveal、已验证派生事件|本局所有参与者/Hunter|按 Phase、Goal、预算进入|ENDED 后离线，T+5 删除|
|Character Private Knowledge|受信 Setup/Repair|绑定 Character 的 Hunter Instance|按最小必要原则进入|Instance 销毁后不可在线读，T+5 删除|
|Hidden Truth|受信 Setup/Repair|Game Engine 的受限规则端口；Hunter 无读取权|永不进入|ENDED 后离线，T+5 删除|
|Reasoning Space|Hunter Reasoning pipeline|当前 Hunter Instance|只以结构化摘要进入|绑定单局，T+5 删除|

### 7.2 写入与事实升级

- 玩家发言先保存为 `STATEMENT/REPORTED`，不能直接成为权威事实。
- Clue 具有来源、visibility、生效 Phase 和撤销状态。
- Evidence 是对来源的结构化引用，不复制原消息全文。
- DM 揭示真相时，从受控来源创建新的 PUBLIC 或 CHARACTER_PRIVATE KnowledgeRecord；不得放宽 Hidden Truth 查询权限。
- 相互冲突的条目同时保留并标记 CONFLICTED，由 Timeline/Reasoning 处理，不能无审计覆盖。
- Knowledge 状态变化通过 Event 提交并使相关 Context Cache 失效。

### 7.3 Visibility Policy

Policy 输入：`game_id`、Instance/Character principal、knowledge ID/class、Phase、Action Goal、target channel、state/policy version。

Policy 输出只有 `ALLOW`、`DENY`、`REDACT`，并返回理由码；LLM 不参与授权。任何以下情况 fail closed：

- game/Instance/Character 不匹配；
- Hidden Truth 请求；
- Other Character Private 请求；
- 知识未生效、已撤销或来源不可验证；
- 目标 channel 不是 V0.1 `GROUP_TEXT`/internal；
- Policy/State Version 不匹配。

## 8. Context Builder Detailed Design

### 8.1 唯一读取路径

```text
Knowledge Store
  -> Visibility Policy
  -> Context Builder
  -> Hunter Context Package
  -> Hunter LLM
```

Hunter LLM、Decision Engine 和 Prompt adapter 均不得绕过 Builder 读取 Game State 或 Knowledge Store。

### 8.2 输入

固定业务输入：

- Current Observation；
- Game Character；
- Current Phase；
- Visible Knowledge View；
- Reasoning Summary；
- Action Goal。

安全绑定输入：`game_id`、`session_id`、`agent_instance_id`、`character_id`、`state_version`、`policy_version`、Context budget、target channel、Trigger/correlation。

### 8.3 构建顺序

```text
bind instance
  -> authorize partitions
  -> phase/effective-state filter
  -> provenance and conflict labeling
  -> relevance selection
  -> injection/data boundary labeling
  -> budget allocation
  -> package assembly
  -> disclosure manifest seal
```

Visibility 过滤早于相关性和 token 预算。预算不足只能删除低相关内容，不能删除 visibility 标签或扩大分区。

### 8.4 Hunter Context Package

|Section|内容|
|---|---|
|Runtime Contract|AI Player 身份、Level 2 行为和禁止事项|
|Session Binding|game/session/instance/character/phase/version|
|Current Observation|规范化当前输入，明确标记为不可信 data|
|Character Scope|公开身份和最小相关私有目标|
|Public Facts|当前 Goal 相关且有效的公开知识|
|Private Facts|仅当前 Character 合法可见内容|
|Timeline Slice|相关事件、地点和冲突摘要|
|Reasoning Summary|结构化 Hypothesis/Confidence/Unknown|
|Action Goal|OBSERVE/ANSWER/DEEP_REASON/PLAN_ACTION|
|Output Contract|允许的决策/回复 schema 与 channel|
|Disclosure Manifest|允许引用和公开的 Knowledge IDs|

### 8.5 安全控制

- Hidden Truth 查询端口不注入 Builder 依赖图。
- 玩家陈述、线索文本和 DM 剧本内容一律标为 data，不可改变 Runtime Policy。
- Package 中每个事实携带 provenance、visibility、status 和 effective phase。
- LLM 输出只能引用 Manifest 中的 ID；Action Gateway 发送前再次执行 Disclosure Check。
- Context Cache key 包含 `game_id + instance_id + state_version + action_goal + policy_version`。
- Cache 是派生数据；重启后重建，禁止持久化旧 Prompt。
- Context 构建命中 Hidden Truth、跨局引用或 Manifest 不一致时，不调用 LLM并记录安全 Audit。

## 9. Hunter Agent Instance Detailed Design

### 9.1 创建前置条件

Session 创建不等于 Instance 可运行。只有以下条件全部满足才创建 Instance：

1. game/session/group/DM 已持久化；
2. Hunter Character 已确定；
3. Character Private Knowledge 通过可见性校验；
4. Hidden Truth 分区存在且与 Hunter 查询端口隔离；
5. Template/Policy Version 已锁定；
6. Setup manifest 完整。

### 9.2 生命周期

```text
PROVISIONING -> READY -> ACTIVE <-> SUSPENDED
       |          |         |
       +----------+---------+-> FAILED
ACTIVE/SUSPENDED -> TERMINATING -> DESTROYED
```

|Instance State|Session 对应|行为|
|---|---|---|
|PROVISIONING|CREATED/LOBBY|校验绑定，不调用 LLM|
|READY|CREATED/LOBBY|等待 Session 启动|
|ACTIVE|RUNNING|观察、推理、决策、Action|
|SUSPENDED|PAUSED|停止 LLM 和普通 Action|
|FAILED|CREATED 或 PAUSED|等待修复/销毁|
|TERMINATING|ENDING/ENDED|停止新任务并收束 Action|
|DESTROYED|ENDED|不可恢复或访问 Game Context|

### 9.3 Recovery 与 Destroy

Instance 不是进程对象的序列化快照。Recovery 从 Session/Phase、Template/Policy、Character Binding、Knowledge visibility、Reasoning Snapshot、未决 Action 和 state version 重建。旧对话、Prompt、自由文本思考和 LLM session handle 均不恢复。

Destroy 撤销 Session capability、清空 Context Cache、停止 Job、取消 CREATED Action、收束 EXECUTING Action，并关闭 Game Memory 在线读取。T+5 再删除受冻结策略约束的持久内容。

Instance 不能跨游戏复用，因为其持有旧角色权限、Knowledge visibility、Reasoning、Context Cache 和可能的 UNKNOWN Action。即使群、玩家、剧本和角色名相同，新局也必须使用新 `agent_instance_id`。

## 10. Timeline 与 Reasoning Space Detailed Design

### 10.1 TimelineEvent

|字段|语义|
|---|---|
|`timeline_event_id`|单局唯一引用|
|`business_time_start/end`|剧本或陈述中的事件时间，允许未知/范围|
|`observed_at`|Runtime 观察时间|
|`location_id`|地点引用，可未知|
|`actor_refs`|涉及人物/Participant/Character|
|`action_code`|结构化行动|
|`source_refs`|Statement/Evidence/Clue/Event IDs|
|`certainty`|CONFIRMED/REPORTED/INFERRED/UNKNOWN|
|`visibility`|继承最严格来源可见性|
|`status`|ACTIVE/CONFLICTED/RETRACTED|

Timeline Engine 负责规范化时间、地点和关系，检测同一角色同一时间多地、行动时长不可能、陈述与 Evidence 冲突等信号。它只产生 `Conflict Signal`，不自动判定凶手或真相。

### 10.2 Reasoning 更新

Reasoning Result 必须是结构化 Delta：

- 新增/更新 Hypothesis；
- Evidence 支持或反驳引用；
- Confidence 档位变化及理由码；
- 新 Conflict/Unknown；
- 建议验证问题；
- 是否值得公开以及对应 Manifest 引用。

Reasoning Store 校验引用存在、可见性合法、Instance 一致和版本未陈旧后提交。完整模型响应、逐步思考和隐式人格评价不保存。

### 10.3 Confidence 约束

V0.1 使用离散档位 `LOW/MEDIUM/HIGH` 加证据计数与冲突标记，不把模型自报概率当作事实。Confidence 只能由可见 Evidence 引用驱动；Hidden Truth 不参与 Hunter Reasoning。

## 11. Decision Engine Detailed Design

### 11.1 Trigger 分级

|Trigger|默认处理|允许深度推理|可能 Action|
|---|---|---:|---|
|`NEW_STATEMENT`|快速分类、Timeline/Knowledge 更新；必要时派生受控 Trigger|否|通常 WAIT|
|`NEW_CLUE`|状态更新、相关性检查|是|关键线索可 SPEAK_PUBLIC|
|`CONTRADICTION_FOUND`|记录 Conflict|是|质询/公开提醒或 WAIT|
|`DM_COMMAND`|授权和状态机处理|是；仅合法命令且仍受 Trigger Policy 判断|控制反馈|
|`PHASE_CHANGE`|失效 Context/Action、刷新 Goal|按 Phase Policy|必要阶段发言或 WAIT|
|`PLAYER_QUESTION`|识别是否询问 Hunter|通常需要受限推理|回答或安全降级|
|`ACTION_RESULT`|更新 disclosure/已公开状态|通常否|WAIT 或后续计划|

不是每条消息都调用 LLM。快速路径优先使用确定性分类、Participant/Timeline/Knowledge 更新和 Trigger 聚合。深度推理只允许由 `NEW_CLUE`、`CONTRADICTION_FOUND`、`PLAYER_QUESTION`、`PHASE_CHANGE` 或已授权 `DM_COMMAND` 进入策略判断；`NEW_STATEMENT` 必须先派生其中一种受控 Trigger，不能直接启动深度推理。

### 11.2 Level 2 行为策略

V0.1 支持：观察全部游戏群消息、WAIT、被询问回复、关键线索主动公开发言、必要时在群内 ASK_DM。默认选择 WAIT，除非满足以下至少一类：

- 直接被合法 Participant 问询且可安全回答；
- 新线索显著改变当前公开 Hypothesis；
- 发现可引用、与当前讨论直接相关的高价值冲突；
- Phase Policy 要求 Hunter 公开表态。

主动发言受 cooldown、每 Phase quota、重复内容 fingerprint 和 pending Action 检查限制。不支持高频刷屏、控制讨论节奏或主持行为。

### 11.3 Decision Record

Decision 输出只包含：`decision_type`、`trigger_refs`、`state_version`、`rationale_codes`、`evidence_refs`、`confidence_band`、`action_intents` 和 `wait_reason`。不包含 Chain of Thought。

输出 schema 无效、引用越权或版本陈旧时整体拒绝，不从自由文本中抢救 Action。

## 12. Action Queue Detailed Design

### 12.1 状态机

```text
CREATED -> EXECUTING -> SUCCESS
    |          |------> FAILED
    |          |------> UNKNOWN
    +-----------------> CANCELLED
```

|状态|含义|自动重试|
|---|---|---:|
|CREATED|Intent 已持久化，尚未 claim|仅按显式 policy 调度|
|EXECUTING|执行 claim 已持久化|禁止并发执行|
|SUCCESS|有证据确认副作用成功|否|
|FAILED|有证据确认未发生或执行前失败|仅明确可重试类别|
|UNKNOWN|副作用可能已发生但无确认|禁止|
|CANCELLED|执行前因 Phase/Session/权限变化失效|否|

`CANCELLED` 是补充收束状态，不改变要求的五个核心状态。

### 12.2 执行协议

1. Actor 原子提交 `ACTION_REQUESTED`、Action Intent 与 State/Outbox。
2. Scheduler 只选择 CREATED 且未过期的 Action。
3. Gateway 重新校验 Session=RUNNING、Phase、Participant binding、metadata、Disclosure Manifest 和 target capability。
4. 执行 Session Authorization；按策略执行 Confirmation。
5. Shared Audit `domain=GAME` 写执行开始；高影响 Audit 不可用时 fail closed。
6. 使用幂等键原子 claim，状态进入 EXECUTING。
7. Executor 执行 internal update 或 `GROUP_TEXT` 发送。
8. 持久化 SUCCESS/FAILED/UNKNOWN，并产生 `ACTION_COMPLETED` 回到 Actor。

Planner 时授权用于避免生成明显非法 Intent，Executor 前授权才是最终副作用门。

### 12.3 V0.1 Action Policy

|Action|Capability|外部副作用|V0.1|
|---|---|---:|---|
|SPEAK_PUBLIC|GROUP_TEXT|是|启用|
|ASK_DM_GROUP|GROUP_TEXT|是|启用，只问可公开控制问题|
|WAIT|internal|否|启用|
|UPDATE_MEMORY|internal Game Memory|内部写入|启用，经版本/审计|
|SEND_PRIVATE|PRIVATE_TEXT|是|禁用/unavailable|
|VOICE/IMAGE|VOICE/IMAGE|是|禁用/unavailable|

DM 的开始、暂停、结束、Phase 等属于 Session Control Operation，不由 Hunter Planner 创建；但仍必须先建立持久 Action/Operation Record，并使用相同的 CREATED/EXECUTING/SUCCESS/FAILED/UNKNOWN 状态语义、Authorization、Audit、必要 Confirmation、Idempotency 和版本治理原语。

### 12.4 Idempotency 与 UNKNOWN

幂等键至少绑定：

```text
game_id + action_id + action_type + target_scope
+ canonical_payload_fingerprint
```

QQ 请求可能已经到达平台，但 Runtime 在收到回执前超时。此时无法安全判断消息是否发送：标记 FAILED 会诱发重复，标记 SUCCESS 缺乏证据，回滚也无法撤销消息。因此必须进入 UNKNOWN：

- 禁止自动重发；
- 禁止 Planner 生成同内容替代 Action；
- 不把内容标记为“已公开”；
- 只允许通过平台回执/可验证消息 ID/幂等查询或有证据的人工 reconciliation 收束；
- Session 可处理不依赖该结果的 Event，不能假设成功或失败。

## 13. Adapter Design

|Adapter|职责|V0.1 边界|
|---|---|---|
|Ingress Adapter|OneBot Group Event→最小 Platform Event|早期 ownership、GROUP_TEXT、block；不做推理|
|Communication Adapter|发送 GROUP_TEXT并返回平台证据|私聊/语音/图片返回 unavailable，不降级公开|
|LLM Adapter|共享 transport、timeout、schema response|不注入 Normal prompt/memory，不持久化对话|
|Audit Adapter|映射 `domain=GAME` Audit contract|复用 HMAC/epoch/fingerprint/append-only/sanitizer|
|Authorization Adapter|连接 Global/Group Gate 与 Session Policy|DM 不进入 Global admin/tool capability|
|Confirmation Adapter|高影响控制操作确认|domain-neutral token，绑定 game/action/version|
|Idempotency Adapter|执行 claim 与结果绑定|不复用 Tool identity 伪装 Game Action|
|Setup Adapter|受信初始化/修复端口|无 UI；私密输入只经此端口|
|Episode Memory Adapter|写入/查询白名单 Summary namespace|Normal Agent 可按用户请求查询；Game Context hard deny|

Adapter 只做协议转换、错误归类和最小披露，不拥有 Session State，不绕过 Actor 提交状态。

## 14. Permission Boundary

### 14.1 权限层次

```text
Global Runtime Permission
  -> Group Feature Gate
     -> Game Session Permission(game_id)
        -> Lifecycle/Phase Policy
           -> Action/Disclosure Policy
```

所有层必须同时允许。任何上层管理员身份都不会自动成为下层 DM；DM 也不会反向获得上层权限。

### 14.2 操作矩阵

|操作|System Admin|当前 Session DM|Player|Hunter Instance|
|---|---:|---:|---:|---:|
|全局关闭服务|允许，经 Global Control Plane|禁止|禁止|禁止|
|创建/初始化 Session|允许，经 Setup Control Plane|仅被授权的本局 Setup 操作|禁止|禁止|
|开始/暂停/恢复/结束|不因 Admin 自动允许；需显式 Session principal 或 Global emergency policy|允许，受状态机/确认约束|禁止|禁止|
|推进 Phase|禁止自动获得；需 Session role|允许合法转换|禁止|禁止|
|公开发言|普通模式身份与 Game 无关|作为玩家消息|允许|经 Action Gateway|
|输入 Hidden Truth|仅受信 Setup principal|仅受信 Setup/Repair，不允许群消息|禁止|禁止|
|调用 Normal Tool|Global Control Plane 极小白名单不等于 Tool|禁止|禁止|禁止|
|访问其他 Session|仅独立运维策略|禁止|禁止|禁止|

无效 DM 操作：解析潜在命令→解析 Participant→校验 game/role/version/phase→拒绝→GAME Audit→返回最小错误。拒绝消息不得泄露谁是 DM、系统管理员配置或其他 Session 状态。

## 15. Memory Boundary 与 Retention

### 15.1 在线边界

```text
Memory Infrastructure
├── Personality/User Memory              # Game Context 禁止读取
├── Game Episode Summary Memory          # Normal Agent 可按用户请求查询
└── Game Memory(game_id)
    ├── State
    ├── Event Store
    ├── Timeline
    ├── Knowledge
    ├── Reasoning State
    └── Action State
```

Game Runtime 不写普通聊天历史、Companion Memory、User/Semantic Memory、Semantic Graph 或 Daily Report。Episode Summary 是唯一出口，不作为新 Game Session 的 Context。

### 15.2 T+0 / T+5

- T+0：ENDED 后从公开 Session Metadata 和公开最终结果生成约 100 字 Summary；字段白名单为日期、剧本名、参与玩家公开表示、Hunter 角色、胜负、MVP。
- 禁止：凶手身份、私密线索、推理、玩家评价/画像、Hidden Truth、QQ ID。
- T+5：删除 Session 内容、Event Store、Timeline、Clue/Evidence、Reasoning、Private Knowledge、Hidden Truth、Action payload 和 Observation。
- 长期保留：Episode Summary、隐私安全 Audit Metadata。
- Summary 失败、复盘失败或 Session 不可恢复都不能取消清理。

Retention Job 必须可幂等重跑，并以删除清单和计数写安全 Audit；Audit 不记录被删除内容。

## 16. Failure and Recovery Design

### 16.1 故障矩阵

|故障|检测|状态处理|Action 处理|恢复|
|---|---|---|---|---|
|LLM timeout|Job deadline|事实/Reasoning 不变；通常 RUNNING|不创建 Action；主动场景 WAIT|预算内一次新 Job 重试，保持 correlation|
|LLM invalid output|Schema/Manifest 校验|整体拒绝，不部分写入|不从自由文本提取 Action|一次受限修复；连续失败 SUSPEND decision|
|Context/Visibility failure|Builder/Policy deny|停止该 Job；严重时 PAUSED|无 Action|修复引用/Policy 后授权恢复|
|Gateway 明确失败|平台明确拒绝|通常 RUNNING；连续故障 PAUSED|FAILED；仅明确未发生时有限重试|平台恢复后新决策|
|Gateway timeout/断连|无可验证回执|不假设结果|UNKNOWN，禁止自动重发|证据化 reconciliation|
|Storage write failure|事务/版本失败|PAUSED，ownership 保留|禁止新副作用；已发生但未落盘为 UNKNOWN|修复存储并 Recovery 校验|
|Restart recovery failure|sequence/version/reference 不一致|PAUSED/recovery FAILED|不恢复调度、不重放外部 Action|授权修复或安全结束|
|Duplicate Event|唯一键命中|返回既有结果|不创建重复 Action|无需恢复|
|Invalid DM Action|Session Policy deny|无状态变化|无 Action|最小拒绝 + Audit|
|Invariant/runtime bug|断言/一致性检测|当前 Session PAUSED|收束 pending/UNKNOWN|人工检查；不传播其他 Session|

### 16.2 LLM 降级

- 玩家直接问题：可返回固定、安全且不含推理/私密内容的群内降级回复；该回复仍经 Action Queue。
- 主动推理失败：选择 WAIT，不发送错误细节。
- 连续 invalid output：暂停 Hunter 自动决策，但允许 DM 控制和受信恢复。
- Context 安全失败：不得为了响应而删除可见性检查或改用 Normal Agent。

### 16.3 Storage Failure 优先级

任何权威写失败都先于可用性：Event ingest 失败、State/Event 原子 apply 失败、Knowledge visibility 写失败、Action 终态写失败或恢复检查点失败均暂停 Session。若外部副作用可能已发生而终态无法保存，按 UNKNOWN 处理。

### 16.4 Recovery 安全序列

```text
restore ownership
  -> load authoritative Snapshot
  -> verify Event cursor/version
  -> verify participant/identity/policy
  -> verify knowledge references
  -> reconcile Action states
  -> rebuild Actor/Instance/cache
  -> emit SESSION_RECOVERY result
  -> resume or remain PAUSED
```

Recovery 不自动发送群消息、不自动重放 Action、不从聊天历史补事件。恢复失败只向有权限维护主体报告安全错误类别，不向群泄露剧本或存储细节。

## 17. Data Flow Scenarios

### 17.1 普通玩家发言

```text
GROUP_TEXT
 -> ownership=GAME
 -> MESSAGE_RECEIVED persisted
 -> Actor resolves Participant
 -> PLAYER_STATEMENT derived
 -> Knowledge/Timeline update
 -> Trigger classifier
 -> fast path WAIT or deep Reasoning Job
 -> optional SPEAK_PUBLIC through Action Queue
```

原消息不会进入 Normal Mode、普通 archive、Companion Memory、Semantic Graph 或 Daily Report。

### 17.2 DM 推进 Phase

```text
GROUP_TEXT potential command
 -> MESSAGE_RECEIVED
 -> authenticated DM_COMMAND
 -> Session role + expected version + transition policy
 -> atomic Phase/knowledge/action update
 -> PHASE_CHANGED
 -> Context cache invalidation
 -> optional phase Trigger
```

普通玩家发送相同文本只得到拒绝，不产生状态变化。

### 17.3 Hunter 公开发言

```text
Trigger
 -> Context Builder + Manifest
 -> Decision Record
 -> SPEAK_PUBLIC Intent
 -> Queue/Gateway/Auth/Audit/Idempotency/Disclosure
 -> OneBot GROUP_TEXT
 -> SUCCESS/FAILED/UNKNOWN
 -> ACTION_COMPLETED Event
```

### 17.4 Session 结束

```text
authorized END
 -> stop reasoning/new actions
 -> settle/cancel actions
 -> ENDED + release ownership
 -> close Game Memory online access
 -> generate whitelist Episode Summary
 -> schedule T+5 deletion
 -> retain Summary + Audit Metadata only
```

## 18. Future Extension Boundary

以下扩展只保留接口，不进入 V0.1：

- Multi-Agent：需要独立 Agent Registry、角色间可见性和调度 ADR；不能复用单 Hunter 假设直接扩展。
- AI DM：需要独立 authority、剧本全知 Context 和主持状态机；不能提升当前 Hunter 权限。
- Private/Voice/Image：通过 Communication Capability 增加；默认 unavailable，不能自动降级到 GROUP_TEXT。
- Vector Index：只能以 `game_id` namespace 存在，并遵守 Visibility/T+5；不得形成 Global Vector Memory。
- Full Replay：Event Store 已保留 sequence/causation/version，但 V0.1 Snapshot 仍为权威；启用 Event Sourcing 需新 ADR 和迁移策略。
- Distributed Actor：需要 lease、fencing token、跨进程 mailbox 和 ownership 协议；V0.1 不预实现。
- 主动控场/Level 3：需要新的主动性、频率、社会行为和风险策略；不能通过调高当前 prompt 隐式启用。

## 19. 架构不变量

1. `RUNNING/PAUSED` 群消息只进入 Game Runtime；失败时也不回落 Normal Mode。
2. Game Runtime 是顶层 Mode，不进入 Plugin 业务、Agent Tool Registry 或 Memory 子模块。
3. 同一 Session 只有一个 State Writer；LLM/Executor 只能回送 Event。
4. Event 处理采用单 Session 顺序、稳定去重和单调 `state_version`。
5. DM 权限必须绑定 `game_id`，不能获得 System Admin 或 Global Tool 权限。
6. Hunter LLM 只能通过 Context Builder读取 Game Knowledge。
7. Hidden Truth 永不进入 Hunter Context、Reasoning 或发言 Manifest。
8. 不保存完整 Chain of Thought、旧 Prompt、完整 LLM 输出或普通聊天历史。
9. Game Memory 不进入 Companion/User/Semantic/Graph Memory，不跨 Session 检索。
10. Episode Summary 是唯一长期输出，Game Context 不得读取。
11. 所有 Game Action经过 Authorization、Shared Audit、Idempotency，必要时 Confirmation。
12. UNKNOWN 不自动重试，也不被推定为成功或失败。
13. Storage/Recovery/Visibility 一致性失败 fail closed 并保留 Game ownership。
14. V0.1 只启用 GROUP_TEXT、单进程 Lightweight Hunter、Level 2 有限主动行为。
15. T+5 清理不因 Summary、复盘或恢复失败而取消。

## 20. P2 交付边界

本文固定了 Game Runtime 的模块职责、输入输出、依赖、生命周期、数据契约、事件提交模型、Actor 并发、Knowledge/Context/Reasoning 隔离、Action 治理和失败恢复策略。

P2 已完成并作为 Detailed Design Baseline。P3 必须同时遵守 [P2.1 Implementation Constraint Freeze](/D:/猎bot/qq-reminder-bot/docs/game-mode/game-mode-implementation-freeze.md)，并在该 Freeze 获得人工确认后按 P3-A 至 P3-E 分阶段进入；任何需要改变 Architecture Freeze 的事项必须先通过新的 ADR。
