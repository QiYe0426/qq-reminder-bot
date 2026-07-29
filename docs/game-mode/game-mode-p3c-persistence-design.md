# Game Mode P3-C Persistence Design Freeze

- 文档类型：RFC / Architecture Design
- 状态：P3-C Design Freeze，待人工确认
- 架构基线：[P0.2 Architecture Freeze](/D:/猎bot/qq-reminder-bot/docs/game-mode/game-mode-architecture-freeze.md)、[P2 Detailed Design](/D:/猎bot/qq-reminder-bot/docs/game-mode/game-mode-p2-detailed-design.md)、[P2.1 Implementation Freeze](/D:/猎bot/qq-reminder-bot/docs/game-mode/game-mode-implementation-freeze.md)
- 前置阶段：[P3-B Router Integration Design](/D:/猎bot/qq-reminder-bot/docs/game-mode/game-mode-p3b-router-integration-design.md)
- 代码基线：`feature/game-mode-runtime-v01` / `170964467d4aa9d69a276deb37e0dc03f4e5b403`
- 目标版本：Game Mode V0.1
- 范围：Game State、Event、Action、ownership、恢复与数据保留的持久化架构
- 非目标：代码、数据库文件、迁移脚本、Runtime/Router/Normal Mode 修改、生产接管、LLM、Action 执行、Agent Memory 接入

## 0. 文档权威性与冻结结论

本文在 P0.2、P2、P2.1 和 P3-B 冻结边界内细化 P3-C。发生冲突时，优先级依次为 P0.2 Architecture Freeze、P2.1 Implementation Freeze、本文、P2 Detailed Design、P3-B Design。需要改变上位冻结决策时，必须新增 ADR 并获得人工确认，不能在实现中隐式调整。

P3-C 冻结以下结论：

1. V0.1 使用独立 SQLite 数据库承载逻辑分离的 Game State Store、Event Store、Action Store、Ownership Index 和 Retention Metadata。
2. Game State Snapshot 是恢复权威状态；Event Store 是结构化、Session 内有序的事实日志。V0.1 不采用完整 Event Sourcing。
3. Event Envelope 在单局生命周期内 append-only；Event processing projection 可以更新。T+5 retention 是唯一允许的批量删除边界。
4. Ingest 与 Apply 使用两个事务阶段。Apply 必须原子提交 Event 处理结果、Snapshot/Projection、Action Outbox 和 `state_version`。
5. `RUNNING` 与 `PAUSED` ownership 必须持久化。进程启动时先恢复 ownership，再允许任何消息被判断为 Normal Mode。
6. 正常关闭且完整校验通过的原 `RUNNING` Session 可以恢复为 RUNNING；异常退出后的原 `RUNNING` Session一律以 PAUSED 恢复，等待有权限主体显式恢复。
7. 同一 `game_id` 同时最多一个进程内 Actor；同一 `group_id` 同时最多一个 RUNNING/PAUSED ownership。V0.1 不设计分布式 lease。
8. 所有外部或高影响状态副作用 Action 必须持久化后才能执行。恢复时不能证明结果的 `EXECUTING` Action 转为 `UNKNOWN`，且永不自动重发。
9. Game Persistence 与 Agent Memory 完全隔离。Episode Summary 是唯一受控长期输出，但其 Agent Memory 写入不属于 P3-C 实现。
10. 任一 DB、Replay、Snapshot、ownership 或版本一致性故障均 fail closed；保留或建立 Game ownership，不回落 Normal Mode，不启动 LLM，不执行 Action。

## 1. Persistence Architecture

### 1.1 逻辑架构

```text
Game Runtime Domain
        |
Persistence Ports
        |
SQLite Persistence Adapter
        |
+---------------------+---------------------+
|                     |                     |
Game State Store      Event Store           Action Store
|                     |                     |
Session Snapshot      Event Envelope        Action Record
Participants          Processing Projection Idempotency/Claim
Ownership Index       Sequence Cursor       Result Evidence
Retention Metadata    Recovery Events       Reconciliation
+---------------------+---------------------+
        |
Recovery Manager
        |
Ownership Registry -> Session Actor -> Hunter Instance
```

“Game State DB + Event Store”是逻辑隔离，不要求 V0.1 使用两个物理数据库。V0.1 将它们置于同一个专用 SQLite 数据库中，以获得跨 State、Event processing 和 Action Outbox 的本地原子事务；表、Repository 和查询端口仍保持独立，避免把 Event Store 变成通用状态表。

### 1.2 分层职责

|层|职责|明确不负责|
|---|---|---|
|Domain/Aggregate|校验 Lifecycle、Phase、版本和业务不变量，产生 State Delta/Outbox|SQL、连接管理、重试、文件路径|
|Persistence Ports|定义 Session、Event、Action、Ownership、Recovery、Retention 的最小领域契约|泄露通用数据库 handle|
|SQLite Adapter|事务、约束、序列分配、乐观版本、序列化、错误归类|修改领域规则、调用 LLM/QQ|
|Game State Store|保存恢复权威 Snapshot 和领域 Projection|保存完整聊天、Prompt、CoT|
|Event Store|保存结构化 GameEvent、顺序、去重和处理状态|替代 Audit、保存消息归档|
|Action Store|副作用前记录、原子 claim、结果和 UNKNOWN|执行通信副作用|
|Ownership Index|提供 `group_id -> active session` 的权威路由视图|Participant/DM 授权|
|Recovery Manager|启动扫描、验证、Replay、Action reconciliation、Actor 重建|猜测缺失状态、自动重发 Action|
|Retention Coordinator|执行 T+0/T+5 生命周期工作并安全审计|因 Summary 失败跳过清理|

### 1.3 事务边界

定义三类事务：

1. **Ingest Transaction**：按来源去重键追加 `RECEIVED` Event，并为该 `game_id` 原子分配 `sequence_no`。
2. **Apply Transaction**：Actor 读取 expected `state_version`，原子写入 State/Projection Delta、Event processing 结果、派生 Event/Action Outbox、cursor 和新版本。
3. **Action Transaction**：创建 Action、原子 claim、结果收束或 reconciliation；任何真实副作用必须发生在 CREATED 已持久、EXECUTING claim 已提交之后。

Session 启动、暂停、恢复、结束和 ownership 变更属于 Apply Transaction。不得出现“Lifecycle 已 RUNNING 但 ownership 未建立”或“ENDED 已提交但 ownership 仍活动”的可提交状态。

## 2. Storage Decision

### 2.1 候选比较

|方案|优点|缺点|适用场景|V0.1 结论|
|---|---|---|---|---|
|SQLite|单文件、进程内事务、部署简单、易备份；与当前单 Bot/单进程形态一致；可约束唯一性和外键|单写并发有限；跨主机 ownership 不适用；需要处理 busy、文件权限和备份一致性|单机 Bot、低到中等并发、同局串行|采用|
|PostgreSQL|并发、事务、运维工具、未来多进程/多主机扩展能力强|引入独立服务、凭据、部署与备份复杂度；V0.1 过度配置|Multi-Agent、分布式 Actor、高可用|未来 ADR 评估|
|Redis|低延迟、原子命令、队列/lease 能力强|默认不是长期事实库；持久语义、审计恢复和备份更复杂；不能单独承担权威 Game State|缓存、分布式 mailbox/lease|V0.1 不采用为权威存储|
|文件存储|直观、零数据库依赖|并发、事务、约束、部分写、索引、迁移和恢复难以证明|人工导入导出、只读资源|不得作为运行时权威存储|

### 2.2 V0.1 决策

V0.1 选择专用 SQLite 数据库，未来实现目标为 `data/` 下独立 Game Runtime 数据文件；本文不创建该文件。Game Runtime 不复用 Reminder、Audit、Companion、Message Archive 或 Semantic Graph 的业务表。

选择理由：

- 当前是单 Bot、单进程、每局 Actor 串行写；
- 同一个 SQLite 事务可覆盖 Snapshot、Event processing 和 Action Outbox；
- 单文件易于一致性备份和恢复演练；
- 可通过 Repository Port 保持 PostgreSQL 迁移可能性；
- 不需要为了未来 Multi-Agent 提前引入分布式数据库和 lease。

未来迁移必须以 Repository contract、schema version、导出校验、双读禁止和一次性 cutover 为基础。迁移期间不能让 SQLite 与 PostgreSQL 同时成为 ownership 权威源。

### 2.3 SQLite 运行约束

实现阶段应使用异步 I/O adapter、外键约束、明确事务、有限 busy timeout 和 WAL 能力；不得在 Actor 路径执行无限重试。数据库目录与文件遵循仓库既有敏感数据权限边界，运行数据禁止提交。备份必须使用 SQLite 一致性备份机制，不复制正在写入的裸文件组合。

Database unavailable、busy 超时、disk full、I/O error、integrity failure 均映射为结构化 Storage Failure。业务层不能根据错误字符串自行决定 fail open。

## 3. Game State Model

### 3.1 GameSession Snapshot

Snapshot 至少包含：

|字段|语义与约束|
|---|---|
|`game_id`|全局唯一、不可复用；所有 Game 数据的 namespace|
|`session_id`|运行时 Session ID；V0.1 与 game 一一对应但语义独立|
|`group_id`|绑定群；同群最多一个活动 ownership|
|`status`|CREATED/RUNNING/PAUSED/ENDED|
|`phase`|LOBBY/INTRODUCTION/EXPLORATION/DISCUSSION/VOTING/ENDING|
|`state_version`|成功业务提交后单调递增；乐观并发 expected version|
|`last_applied_sequence_no`|Snapshot 已完整包含的 Event cursor|
|`dm_participant_id`|当前 Session DM 引用，不是系统管理员|
|`hunter_instance_id`|本局 Lightweight Instance 引用|
|`hunter_character_id`|本局角色引用，不进入长期 Identity|
|`policy_version/template_version`|本局锁定版本|
|`recovery_status`|NONE/VALIDATING/READY/FAILED；不新增 Lifecycle 状态|
|`pause_reason_code`|结构化安全原因，不包含敏感正文|
|`created_at/started_at/updated_at/ended_at`|时区明确的 Runtime 时间|
|`retention_due_at`|ENDED 时固定为 T+5|
|`schema_version`|Snapshot 序列化/迁移版本|

合法组合由领域状态机验证。数据库约束负责拒绝空 ID、负版本、重复 ID 和明显非法 ownership；Lifecycle/Phase 组合仍由 Aggregate 规则判定，不能只依赖数据库 CHECK。

### 3.2 Participant Projection

Participant 至少包含：

|字段|语义与约束|
|---|---|
|`participant_id`|Session 内稳定 ID|
|`game_id/session_id`|强制 namespace；禁止跨局引用|
|`qq_identity_ref`|受控映射引用；普通查询、日志和 Audit 不返回 QQ 明文|
|`role`|DM/PLAYER/SPECTATOR/UNKNOWN|
|`character_id`|可空；本局角色|
|`membership_state`|ACTIVE/REPLACED/LEFT/REVOKED|
|`binding_version`|身份替换或权限变化时递增|
|`public_information_ref`|公开身份信息引用|
|`permission_set_ref`|Session-scoped 权限集合引用|
|`created_at/updated_at`|持久时间|

Session-scoped DM 权限不能写入全局 Authorization role，也不能反向修改 Normal Mode 或 Tool permission。

### 3.3 Ownership Projection

Ownership 是 Router 的最小权威读模型，不是完整 Session 副本：

|字段|语义与约束|
|---|---|
|`group_id`|唯一 active key|
|`game_id/session_id`|活动 Session scope|
|`session_status`|只允许 RUNNING/PAUSED|
|`ownership_generation`|每次同群绑定递增，阻止旧 Actor/旧消息污染|
|`state_version`|生成该视图时的 Session version|
|`recovery_status`|启动时可见 VALIDATING/FAILED；两者都 fail closed|
|`updated_at`|观测与诊断时间|

CREATED 不产生 ownership。ENDED 必须在同一事务解除 ownership；历史 Session 仍留在 State Store，但 Registry 应返回 NO_SESSION，而不是让 Router读取历史记录推断结果。

### 3.4 事实状态与派生状态

需要持久化的事实状态：

- Session Lifecycle/Phase、Participant binding、Character/Policy/Template 绑定；
- `state_version`、Event cursor、ownership generation；
- 结构化 Knowledge/Timeline/Reasoning Snapshot（由后续阶段写入时）；
- Action/Operation 状态、幂等键、claim 和可验证结果引用；
- Retention deadline、清理进度、Summary generation 状态；
- Recovery attempt 和结构化故障类别。

可以重建且不作为事实保存：

- Actor 对象、Mailbox 内存队列、异步 task；
- Hunter Context Package、Prompt、Context Cache；
- LLM provider session/response handle；
- Router 的进程内 cache；
- Timeline 的纯展示视图和统计指标。

禁止持久化：

- 完整 QQ 聊天记录或未分类消息全文；
- 普通聊天历史、Normal Context、Companion/User/Semantic/Graph Memory 内容；
- 完整 Prompt、LLM 原始输出、完整 Chain of Thought；
- Hidden Truth/Private Knowledge/Reasoning 正文在 Event、Audit 或 Action 中的副本；
- Global admin 凭据、Session 外玩家画像和跨局学习特征。

## 4. Event Store Model

### 4.1 Event Envelope

Event Store 保存结构化 GameEvent，至少包含：

|字段|语义与约束|
|---|---|
|`event_id`|全局唯一；同一重投保持稳定|
|`game_id/session_id`|强绑定；所有查询必须带 namespace|
|`sequence_no`|单 Session 单调递增，由 Ingest Transaction 分配|
|`event_type`|受控枚举，不接受自由字符串扩权|
|`source`|PLATFORM/CONTROL/DERIVED/ACTION/RECOVERY/SYSTEM|
|`actor_ref`|Participant/System/UNKNOWN 安全引用|
|`timestamp`|业务发生时间，不决定处理顺序|
|`received_at`|Runtime 接收时间|
|`payload`/`payload_ref`|按事件类型校验的最小结构化数据|
|`visibility`|PUBLIC/CHARACTER_PRIVATE/DM_CONTROL/SYSTEM_ONLY|
|`observed_state_version`|生产者观察到的版本|
|`correlation_id`|一次输入、恢复或 Action 链|
|`causation_event_id`|直接父事件；派生事件必填|
|`schema_version`|Event payload contract 版本|

P3-A 的内存 `GameEvent` 是领域骨架；P3-C 的持久 Envelope 需要补齐顺序、可见性、版本、接收时间和 schema version，但不得改变已冻结的事件语义。

### 4.2 Append-only 与处理 Projection

为同时满足 append-only 与可恢复 processing status，逻辑上分为：

- **Event Envelope**：插入后不修改、不覆盖、不按业务请求删除；T+5 retention 统一清理是冻结例外。
- **Event Processing Projection**：按 `event_id` 保存 RECEIVED/APPLIED/REJECTED/DEFERRED、attempt、错误类别、applied state version 和更新时间。
- **Session Sequence Cursor**：保存 next sequence 与 last applied sequence，用于连续性校验。

禁止用原地修改 Event payload 表示“新事实”。业务更正必须追加新 Event，并通过 causation/reference 标明 supersede 或 rejection。

### 4.3 为什么不能只保存最终状态

只保存 Snapshot 无法证明：

- 同一平台消息是否被重复接收或重复应用；
- DM 命令、Phase 变化和 Action 的因果链；
- `state_version` 为什么变化；
- 重启前哪些 Event 已接收但尚未 Apply；
- 外部 Action 是否由已提交 Intent 产生；
- 恢复时 cursor、sequence 和 Snapshot 是否一致。

Event Store 用于 Session 恢复、去重、顺序验证、状态变化追踪和 Shared Audit correlation。它不替代 Shared Audit 的 HMAC/epoch/fingerprint 证据，也不成为聊天归档或长期 Game Memory。

### 4.4 顺序、去重与原子性

- 顺序只在同一 `game_id/session_id` 内定义；不同游戏不提供全局顺序。
- 平台来源去重键为稳定 `platform_event_id + group_id`；内部事件以稳定 `event_id` 去重。
- 唯一约束保证一个来源只对应一个 Event；重复 ingest 返回既有结果，不分配新 sequence。
- Actor 只 Apply 最小未终结 sequence；不得按 Event type 越过已持久事件。
- Apply 使用 expected `state_version` 和 expected cursor；不匹配时拒绝提交并进入恢复/重载路径。
- 系统只承诺 at-least-once notification + idempotent apply，不宣称端到端 exactly-once。

## 5. Recovery Design

### 5.1 Snapshot + Event Replay

```text
Load authoritative Snapshot
        |
Validate schema/state/cursor
        |
Read Events after last_applied_sequence_no
        |
Verify contiguous sequence and event schemas
        |
Replay deterministic unapplied Events
        |
Reconcile Action states
        |
Rebuild derived state/cache
        |
Acquire Actor ownership
        |
Create restricted or active Actor
```

Snapshot 提供当前 Lifecycle/Phase、Participant/Identity binding、Knowledge/Timeline/Reasoning Projection、`state_version` 和 cursor。Event 提供 Snapshot 之后已接收但未 Apply 的输入、派生因果链和 Action/Recovery 结果。

V0.1 Replay 不是从 Event 0 全量重建所有 Game State。它执行三项工作：验证 Snapshot 与历史 cursor 的一致性、对 Snapshot 后的结构化未决 Event 进行确定性 Apply、恢复 mailbox 通知。任何需要重新调用 LLM、重新解析原聊天、重新发送 QQ 或猜测旧自由文本的步骤都不属于 Replay。

### 5.2 启动恢复流程

```text
Process Start
  -> Open dedicated Game DB and validate schema
  -> Enter routing recovery barrier
  -> Load RUNNING/PAUSED ownership rows
  -> Publish fail-closed ownership snapshots
  -> Validate each Session independently
  -> Load Snapshot + Replay Events + reconcile Actions
  -> Acquire Actor ownership
  -> Rebuild Actor/Hunter Instance from facts
  -> Append SESSION_RECOVERY result
  -> Resume RUNNING or retain PAUSED
  -> Mark recovery barrier ready
```

在 recovery barrier 完成前：

- 已知活动群必须返回 GAME/REJECT，而不是 Normal；
- 无法证明 `NO_SESSION` 的查询必须 REJECT；
- 不调用 LLM、不调度 Action、不发送恢复公告；
- 单局失败不能阻止其他已验证 Session 恢复，但失败局持续 fail closed。

### 5.3 正常关闭与异常退出

正常关闭必须先停止新 ingest/Reasoning/Action claim，排空已提交 mailbox 到安全检查点，记录 clean shutdown marker，并保存各 Session cursor。下次启动时，原 RUNNING Session 只有在 Snapshot、Event、Action、ownership 和 clean marker 全部校验通过后，才恢复 RUNNING/ACTIVE；原 PAUSED 始终保持 PAUSED/SUSPENDED。

异常退出或缺失 clean marker 时：

- 原 RUNNING Session 的 ownership 立即恢复，但 Lifecycle 以 PAUSED 持久收束；
- `recovery_status` 为 VALIDATING，完成后为 READY 或 FAILED；
- 不自动恢复 Hunter ACTIVE；
- 不自动发送消息、不执行 CREATED Action；
- 由当前 Session 的合法 DM 或受信 Repair Control Plane 显式恢复。

该保守策略避免在外部副作用、Event apply 或 shutdown 边界不明确时自动继续游戏。

### 5.4 Replay 一致性规则

以下任一情况停止自动恢复：

- Snapshot schema 不支持或反序列化失败；
- Lifecycle/Phase、Participant/Identity/Policy 组合非法；
- sequence 缺口、重复冲突或 cursor 超出 Event 上限；
- Event schema、game/session scope 或 causation 无效；
- applied state version 与 Snapshot version 不一致；
- ownership group/game/session/generation 不一致；
- Action 状态、幂等键或 claim 不能安全解释；
- Hidden/Private/Reasoning 引用跨 `game_id`。

失败局保持或建立 PAUSED ownership，写安全 Recovery/Audit metadata，不创建可行动 Hunter Instance。若 Snapshot 无法读取到足以定位 Session，可使用最小 quarantine ownership 记录阻断相关群，等待人工修复或安全结束。

## 6. Actor Ownership

### 6.1 Ownership 层次

需要同时满足两个唯一性：

1. **Group ownership**：同一 `group_id` 最多一个 RUNNING/PAUSED Session，用于 Mode Router。
2. **Actor ownership**：同一 `game_id` 最多一个 Active/Restricted Actor，用于单写者保证。

启动顺序固定为：

```text
Load/validate Session
  -> acquire persistent group ownership or verify existing binding
  -> reserve game_id in process Actor Registry
  -> increment/verify ownership_generation
  -> create Actor with generation + state_version cursor
  -> publish ready
```

Actor 处理每个 Event 和 Result 时重新校验 `game_id/session_id`、ownership generation 和 expected `state_version`。旧 generation 的 mailbox notification、LLM Result 或 Action Result 一律拒绝，不能影响新 Actor。

### 6.2 防止重复 Actor 与重复恢复

V0.1 采用：

- 数据库对 active `group_id` 的唯一约束；
- `game_id`/`session_id` 唯一约束；
- 单进程 Actor Registry 的原子 reserve/compare-and-set；
- Recovery attempt ID 与 ownership generation；
- Actor 启动前的完整验证和启动后的 generation fencing；
- 重复 recovery 请求幂等返回已有 Actor/恢复结果。

若进程内发现已有 Actor，第二次创建失败并记录 ownership conflict；不得销毁原 Actor后“重试抢占”。若数据库表明另一个进程仍拥有运行权，V0.1 视为不支持的部署冲突并 fail closed，而不是实现分布式 lease 或自动抢占。

### 6.3 Actor 与持久状态关系

Actor 是持久事实的执行者，不是事实本身。它只持有 Snapshot/cache 和 cursor 的内存副本；每次提交由数据库 expected version 决定是否成功。Actor 崩溃后可以丢弃并从 Persistence 重建，不序列化线程、task、Mailbox、Prompt 或 provider handle。

## 7. Action Persistence

### 7.1 Action Record

Action/Operation 至少持久化：

|字段|语义与约束|
|---|---|
|`action_id`|全局唯一|
|`game_id/session_id/agent_instance_id`|强绑定|
|`action_type`|受控类型；Game Action 不是 Agent Tool|
|`status`|CREATED/EXECUTING/SUCCESS/FAILED/UNKNOWN/CANCELLED|
|`target_scope`|V0.1 仅绑定群或 internal|
|`payload_ref`|受控短期引用，不复制 Hidden/Private 数据|
|`source_event_id/correlation_id`|因果链|
|`created_state_version/phase`|执行前重验|
|`authorization/confirmation/disclosure result refs`|治理结果引用，不存敏感正文|
|`idempotency_key`|绑定 game/action/type/target/payload fingerprint|
|`claim_id/claimed_at`|原子 claim 证据|
|`result_class/result_evidence_ref`|可验证成功、失败或不确定依据|
|`created_at/updated_at/expires_at`|调度与 retention|

### 7.2 状态与恢复策略

|恢复时状态|恢复处理|允许自动执行/重试|
|---|---|---:|
|CREATED|保留；重新验证 Session/Phase/Auth/Disclosure 后才可由未来 Scheduler claim|P3-C 不执行；P3-E 仅按显式 policy|
|EXECUTING|若同一事务内已有可验证结果则收束；否则转 UNKNOWN|否|
|SUCCESS|保持终态，不重复产生副作用|否|
|FAILED|保持终态；未来只有明确“副作用未发生”且新策略创建新 Action 时才可继续|原 Action 否|
|UNKNOWN|保持终态并冻结自动替代|否|
|CANCELLED|保持终态|否|

### 7.3 UNKNOWN 冻结规则

QQ 请求可能已到平台，但 Runtime 在回执或终态持久化前中断。此时标记 SUCCESS 缺少证据，标记 FAILED 会诱发重复发送，因此必须 UNKNOWN：

- 不自动重发原 Action；
- 不自动创建同 payload 的替代 Action；
- 不把内容标记为已公开；
- 不通过 Event Replay 再次执行；
- 只有平台回执、稳定消息 ID、可验证幂等查询或有证据的人工 reconciliation 可以追加收束记录；
- reconciliation 本身必须 Authorization、Audit、幂等并绑定原 action_id。

### 7.4 P3-C 执行边界

P3-C 只实现持久 Queue、claim/result/recovery contract 和测试，不实现 Action Executor、QQ/NapCat/OneBot 调用或自动 Scheduler。即使 Action Record 为 CREATED，也不能在 P3-C 产生真实副作用。

## 8. Memory Boundary

### 8.1 物理与逻辑隔离

```text
Agent Memory
├── Personality/User Memory
├── Companion/Semantic Memory
└── Game Episode Summary Memory       # 唯一受控长期出口

Game Persistence (game_id)
├── Session/Participant/Ownership
├── Event Store
├── Knowledge/Timeline
├── Reasoning State
├── Action State
└── Observation Buffer
```

Game Persistence 使用独立数据库、Repository 和查询端口。不得复用 Companion Memory、普通 group context、User/Semantic Memory、Message Archive 或 Global Vector Store；不得将它们作为恢复来源。所有查询强制带 `game_id`，Repository 返回值不得跨 namespace 聚合。

### 8.2 Game Memory 生命周期

Game Memory 包含本局状态、线索、Timeline、结构化 Reasoning、角色私密知识、Hidden Truth、Action 和结构化 Event。RUNNING/PAUSED 可在线读取；ENDED 后立即关闭 Game Runtime 在线查询，只允许 Summary Source View、Retention 和受信修复路径按白名单访问；T+5 删除内容。

Observation Buffer 只保存分类所需短 TTL 内容，不等同于聊天历史。Event Store、Audit 和普通日志不得复制 Observation 正文。

### 8.3 Episode Summary

Episode Summary 使用独立 `GAME_EPISODE` Memory Namespace，目标约 100 个中文字符。允许字段只有日期、剧本名称、参与者公开表示、Hunter 角色、胜负结果和 MVP。禁止凶手身份、私密线索、推理过程、玩家怀疑/评价、隐藏剧情、QQ ID 和权限信息。

Summary 只能读取专用 allowlisted Source View，并在写入前通过 Disclosure Filter。它不读取完整 Game Memory、Hidden Truth、Reasoning、Private Knowledge 或聊天内容。Game Context Builder 和新 Session 永远拒绝读取 Episode Summary。

P3-C 仅持久化 Summary Source View 所需公开字段、generation status 和 retention deadline，不接入现有 Agent Memory，不生成 LLM Summary。

## 9. Cleanup Policy

### 9.1 生命周期

```text
Game End (T+0)
  -> ENDED + release ownership
  -> close online Game Memory access
  -> build allowlisted Summary Source View
  -> request/record Episode Summary result
  -> retention_due_at = ended_at + 5 days

T+5
  -> claim idempotent cleanup job
  -> delete game-scoped content in controlled order
  -> verify no retained Game content
  -> retain Episode Summary + minimal Audit Metadata
```

Summary 生成失败不回滚 ENDED、不重新取得 ownership、不延迟或取消 T+5 清理。

### 9.2 删除与保留

T+5 删除：

- Game Session 内容和非必要 cleanup tombstone；
- Participant/QQ mapping、Character binding；
- Event Envelope、processing projection 和 payload；
- Knowledge、Clue/Evidence、Timeline；
- Reasoning Snapshot/Delta；
- Private Knowledge、Hidden Truth；
- Action payload/result detail、Observation；
- Context/derived cache（通常更早删除）。

长期保留：

- 已通过 Disclosure Filter 的 Episode Summary；
- Shared Audit Engine 中的最小 GAME Audit Metadata；
- 不含业务内容的清理证明，例如 `game_id` 安全 fingerprint、完成时间、删除类别与计数。

### 9.3 幂等清理

Cleanup job 使用稳定 job ID 和状态 PENDING/RUNNING/SUCCESS/FAILED。删除按 `game_id` 执行、可重复；中断后重跑不能访问或恢复已删除内容。外键与删除顺序必须防止 orphan。最终验证发现任何禁止保留的数据时，job 为 FAILED 并继续告警/重试，不得因部分成功标记完成。

Retention 查询不能提供业务数据检索能力。Audit 只记录类别、计数、时间和 policy result，不记录被删除内容。

## 10. Security Boundary

### 10.1 Namespace 与访问控制

- 所有 State/Event/Action/Knowledge 查询必须显式接收 `game_id`；禁止“先按 ID 全局查再检查 game_id”。
- 复合唯一键、外键和 Repository contract 同时绑定 game/session scope。
- Context Builder 不获得 Hidden Truth repository、通用 SQL handle 或跨分区查询能力。
- Router 的 Registry Port 只返回最小 ownership snapshot，不返回 Participant、Knowledge 或 Action。
- DM 权限由 Participant binding + `game_id` + binding version 判定，不写入 Global Permission。
- 备份、日志、异常和 Audit 不输出 QQ 明文、消息正文、剧本秘密、Prompt 或 Reasoning 正文。

### 10.2 数据分区

统一 Knowledge Store contract 内部至少使用受限 Repository/表隔离 Public、Character Private、Hidden Truth 和 Reasoning。Hidden Truth 与 Private Knowledge 不进入 Event payload、Action payload、Audit 或 Context cache。Reasoning 只保存结构化状态，不保存完整 CoT。

### 10.3 Fail-closed 路由不变量

持久 Registry 不可用、启动恢复未完成、ownership 冲突或 Snapshot 不可验证时，Router 必须返回 REJECT/安全不可用。不得将“不知道是否有游戏”解释为 NO_SESSION，也不得把已归属 Game 的消息回投 Normal Mode。

## 11. Failure Model

|故障|检测|Session/ownership|Action|恢复策略|
|---|---|---|---|---|
|DB unavailable/busy timeout/disk full|连接、事务或 I/O 分类|已知活动群保持 fail-closed ownership；Session PAUSED|禁止新 claim/副作用；可能已发生者 UNKNOWN|有限基础设施重试后人工修复并完整 Recovery|
|Ingest write failure|Event append 未提交|阻断消息且不回落 Normal|不创建 Action|存储恢复后只接受平台稳定重投；不从聊天归档补写|
|Apply transaction failure|expected version/cursor 或事务失败|PAUSED，ownership 保留|Outbox 不可见；不执行|重载 Snapshot，验证 Event 后幂等 Apply|
|Event Replay failure|sequence/schema/scope/causation 不一致|PAUSED，recovery=FAILED|不调度、不重放|授权 Repair 或安全结束|
|Snapshot corrupt/unsupported|校验和、schema、反序列化或不变量失败|建立/保留 quarantine ownership；不创建可行动 Actor|全部冻结；EXECUTING 视风险为 UNKNOWN|从一致备份恢复或受信修复；禁止从 Event 猜测完整状态|
|Ownership conflict|DB 唯一约束、Actor Registry reserve 或 generation mismatch|冲突 Session PAUSED/REJECT；不抢占|无新执行|人工确认唯一 owner；V0.1 不自动 lease takeover|
|Action terminal write failure|Executor 结果无法提交|Session PAUSED|若副作用可能发生则 UNKNOWN|证据化 reconciliation|
|Cleanup failure|job/verification failure|ENDED 不回到活动状态|无执行|幂等重跑并告警；不保留超期数据作为降级|

### 11.1 Snapshot 损坏边界

V0.1 Snapshot 是权威状态，因此 Snapshot 损坏时不承诺只靠 Event Store 全量恢复。Event Store 可以用于验证、诊断和恢复 Snapshot 之后的未决事件，但不能触发 LLM、重新解析消息或补造缺失 Knowledge。只有已验证的一致备份或受信 Repair 可以恢复；否则维持 PAUSED/quarantine 并允许安全结束与清理。

### 11.2 Shared Audit

所有恢复开始/结果、ownership acquire/conflict、Action claim/UNKNOWN/reconciliation、清理结果和非法跨局访问使用 Shared Audit Engine 的 `domain=GAME`。复用 HMAC、epoch、fingerprint、append-only 和 sanitizer，不复制 Audit 数据库。Audit 故障时，高影响恢复、修复、Action reconciliation 和清理豁免请求 fail closed。

## 12. P3-C Implementation Scope

### 12.1 后续实现允许项

人工确认本文后，P3-C 实现阶段仅允许：

- `game_runtime/persistence/` 的 SQLite adapter、事务边界和 Repository 实现；
- Game State、Event、Event processing、Action、ownership、recovery、retention 的 schema 与 migration；
- P3-A Session/Event/Action contract 的持久映射与必要版本字段；
- P3-B Session Registry 的持久只读实现，但不启用生产 ingress；
- Ingest/Apply transaction、sequence、去重、expected version 和 outbox；
- Action 持久 Queue、claim/result/UNKNOWN recovery，不实现 Executor；
- Recovery Manager、clean/unclean restart policy、Actor 去重重建；
- T+5 cleanup contract、Summary Source View metadata 和幂等测试；
- 数据库故障、Replay、Snapshot、ownership、UNKNOWN、群隔离和跨局拒绝测试。

### 12.2 后续实现禁止项

- 修改或启用生产 NoneBot/OneBot/NapCat ingress；
- 修改 Normal Mode、普通 matcher、Router 默认生产行为或 `pyproject.toml` 插件注册；
- LLM、Prompt、Context Builder、Knowledge reasoning pipeline 或 Hunter Agent Loop；
- Action Executor、QQ 回复、主动发言、私聊、语音或图片；
- Agent Tool 注册或复用 Normal Tool Loop；
- 接入 Companion/User/Semantic/Graph Memory；
- 写入 Episode Summary 到长期 Agent Memory；
- 完整 Event Sourcing、PostgreSQL、Redis、分布式 Actor/lease 或 Container Runtime；
- 从普通聊天归档恢复 Event，或保存完整聊天/CoT/Prompt。

### 12.3 必须证明的退出条件

P3-C 完成后、进入 P3-D 设计前必须证明：

1. Snapshot/Event/Action/ownership 可跨进程重启恢复；
2. 同局 sequence 单调、来源去重、Apply 幂等、`state_version` 不回退；
3. RUNNING/PAUSED ownership 在恢复窗口不泄漏到 Normal Mode；
4. 正常关闭恢复策略与异常退出 PAUSED 策略可重复验证；
5. 同一 group/game 不会创建重复 ownership 或 Actor；
6. CREATED/EXECUTING/SUCCESS/FAILED/UNKNOWN/CANCELLED 的恢复行为符合本文；
7. EXECUTING 不确定结果进入 UNKNOWN 且不会自动重发；
8. DB/Replay/Snapshot/ownership 故障全部 fail closed；
9. Game 数据按 `game_id` 隔离，未进入普通 Memory/Archive/Graph；
10. 未启用生产消息接管、LLM、QQ Action 或 P3-D/P3-E 能力。

## 13. Traceability Matrix

|冻结要求|本文映射|
|---|---|
|Game State DB + Event Store|1、2、3、4|
|Session restart recovery|5|
|Event Replay|4、5|
|Action state recovery/UNKNOWN|7|
|single Actor / same-game ordering|4、6|
|RUNNING/PAUSED ownership|3.3、5、6|
|Game Memory isolation|8、10|
|T+5 cleanup / Summary exception|8、9|
|fail closed|5.4、10.3、11|
|P3-C only, no later capability|12|

## 14. Design Freeze Checklist

- [x] Game Runtime 仍是 Agent Runtime 顶层 Mode，不是 Plugin/Tool。
- [x] SQLite 只作为 V0.1 专用 Game Persistence，不复用普通 Memory。
- [x] Snapshot 权威、Event 结构化且非聊天归档。
- [x] RUNNING/PAUSED ownership 可恢复且故障时不回落 Normal Mode。
- [x] DM 权限保持 `game_id` scope。
- [x] Hidden Truth 不进入 Hunter Context、Event、Action 或 Audit 副本。
- [x] 不保存完整 Chain of Thought。
- [x] Action 先持久化；UNKNOWN 不自动重试。
- [x] Episode Summary 是唯一受控长期输出；P3-C 不接入 Agent Memory。
- [x] T+5 清理不因 Summary、恢复或复盘失败而取消。
- [x] 本阶段只新增架构文档，未开始 P3-C 实现。

本文完成 Game Mode P3-C Persistence Design Freeze。人工确认前不得进入 P3-C 实现。
