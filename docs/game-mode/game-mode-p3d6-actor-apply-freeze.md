# Game Mode P3-D-6 Actor Apply Freeze Supplement

- 文档类型：ADR / Design Supplement
- 状态：Draft Freeze Proposal
- 目标阶段：P3-D-6 Actor Apply
- 当前分支：`feature/game-mode-runtime-v01`
- 代码基线：`057c610160babbbc4cd3750410484e6e710af798`
- 上位约束：[Architecture Freeze P0.2](game-mode-architecture-freeze.md)、[P2 Detailed Design](game-mode-p2-detailed-design.md)、[Implementation Constraint Freeze P2.1](game-mode-implementation-freeze.md)
- 直接前置：[P3-C Persistence Design](game-mode-p3c-persistence-design.md)、[P3-D Session Control Plane Design](game-mode-p3d-session-control-design.md)
- 范围：Control Result Event、Event Envelope、Actor Apply Boundary、Atomic Apply Port、Control Operation、CREATE bootstrap
- 非目标：代码、生产接管、Normal Mode、LLM、Memory、Knowledge、Reasoning、Hunter Agent、QQ发送和 Action Executor

## 0. 文档权威性与准入状态

本文是 P3-D-6 的 Freeze Supplement，用于解决实现前发现的契约缺口。本文不替代 P0.2、P2、P2.1、P3-C 或 P3-D Design Freeze，也不放宽其中任何安全不变量。

发生冲突时，优先级为：

1. P0.2 Architecture Freeze；
2. P2.1 Implementation Constraint Freeze；
3. P3-D Session Control Plane Design Freeze；
4. 本 Supplement；
5. P3-C Persistence Design 与 P2 Detailed Design 的细节映射。

本文在人工确认前保持 `Draft Freeze Proposal`。确认前不得进入 P3-D-6.1 或后续实现；确认后，本文成为 P3-D-6 Implementation 的唯一补充基线。任何实现若需要改变本文的 Event、Actor、Atomic Apply、Operation 或 CREATE 边界，必须新增 ADR，不能在代码中隐式调整。

## 1. 背景与 Freeze Gate

P3-D-1 至 P3-D-5 已建立 Command Contract、Session Resolver、Authorization、Confirmation 和 `DM_COMMAND` Event Integration。进入 Actor Apply 前发现当前 P3-A/P3-C 实现只覆盖了冻结契约的一部分，无法在不调整持久和执行接口的情况下安全完成：

1. `GameEventType` 尚未包含完整 Session Control Result/Reject Event；
2. 当前 `GameSessionActor` 使用同步 mailbox drain 和同步 state updater，不能直接等待异步 Atomic Apply；
3. 当前 `apply_event` 只能提交 Snapshot、Participant/ownership projection 与输入 Event processing，不能在同一事务中提交 Result Event 和 Control Operation terminal state；
4. `CREATE_SESSION` 前不存在 Session row、Participant 或 Actor，现有 Event 外键和 `create_session` 接口不能表达冻结的 bootstrap transaction；
5. 当前 persistence schema 尚未包含独立 Control Operation、creation guard、Participant binding version 和完整 Setup/retention projection；
6. 当前持久 Event Envelope 尚未把 visibility、observed version、causation 和 schema version 作为正式字段保存。

这些缺口不是 P3-C 架构错误。P2 与 P3-C 已冻结“两阶段 Event 持久化”“Actor 单写者”“Apply Transaction 原子提交 State、Event processing、派生 Event/Outbox 和版本”。P3-D 引入 Session Control Operation 后，需要以兼容扩展补齐相应领域和持久接口。

本 Supplement 冻结以下结论：

- 不创建第二套 Event、Actor、State Store 或 ownership 系统；
- Event、Actor 和 Persistence 只做版本化、可迁移的增量扩展；
- Actor 仍是唯一 State Writer；Coordinator 不是 Writer；
- Control Plane、Authorization、Confirmation 和 Adapter 不能获得 State 写 Port；
- `CREATE_SESSION` 只能通过 provisional Actor 和 `create_session_with_event` 原子事务；
- 所有变化完成前，P3-D-6 保持不可实现、不可生产启用。

## 2. Event Schema Extension Design

### 2.1 决策

继续使用唯一的 `GameEvent` 和现有 Event Store。新增受控 Event Type 与独立 payload schema，不创建 `ControlEvent` 第二套 Envelope，不使用自由字符串模拟结果类型。

### 2.2 Control Result Event Registry

|Event Type|Producer|主要 Consumer|Visibility|最小 Payload|Persistence requirement|
|---|---|---|---|---|---|
|`SESSION_CREATED`|Provisional Session Actor|Session Control、Recovery、Audit correlation|`DM_CONTROL`|common result fields、`status=CREATED`、`phase=LOBBY`|与 Session、初始 DM Participant、输入 Event、Operation 原子提交|
|`SESSION_STARTED`|Session Actor|Ownership、Recovery、未来 Hunter Runtime|`SYSTEM_ONLY`|common、previous/current lifecycle、ownership generation|与 RUNNING、ownership、Operation 原子提交|
|`SESSION_PAUSED`|Session Actor|Recovery、未来 Hunter Runtime|`SYSTEM_ONLY`|common、previous/current lifecycle、safe reason code|与 PAUSED、ownership 保留、Operation 原子提交|
|`SESSION_RESUMED`|Session Actor|Recovery、未来 Hunter Runtime|`SYSTEM_ONLY`|common、previous/current lifecycle、ownership generation|与 RUNNING、Operation 原子提交|
|`SESSION_ENDED`|Session Actor|Ownership、Retention、Recovery|`SYSTEM_ONLY`|common、previous/current lifecycle、retention reference|与 ENDED、ownership release、retention metadata、Operation 原子提交|
|`SCRIPT_SET`|Session Actor|Setup Projection、Recovery|`DM_CONTROL`|common、script ID、公开名称、manifest reference|与 Setup Projection、Operation 原子提交|
|`CHARACTER_ASSIGNED`|Session Actor|Participant Projection、Authorization、Recovery|`DM_CONTROL`|common、Participant ID、Character ID、binding version|与 Character binding、Operation 原子提交|
|`PLAYER_REPLACED`|Session Actor|Participant、Authorization、Recovery|`DM_CONTROL`|common、old/new Participant reference、新 binding version|与旧 binding 失效、新 binding 生效、Operation 原子提交|
|`SESSION_CONTROL_REJECTED`|Session Actor|Control Plane、Recovery、Audit correlation|`DM_CONTROL`|common、fixed reason code、current state version|与输入 Event REJECTED、cursor、Operation FAILED/CANCELLED 原子提交|
|`PHASE_CHANGED`|Session Actor|Session、Knowledge、Decision、Recovery|`PUBLIC`|common、previous/current phase|与 Phase、Operation 原子提交；沿用现有 Event Type|

`START_GAME` 成功必须在同一 Apply Transaction 中产生 `SESSION_STARTED` 和 `PHASE_CHANGED`。Operation 记录一个 primary result event ID，并可关联同一事务产生的其他 result event IDs；全部 Result Event 使用输入 `DM_COMMAND.event_id` 作为 `causation_event_id`。

Visibility 只控制 Event 数据访问，不代表自动向 QQ 群发送内容。P3-D 不实现任何 Result Event 通信 Action。

### 2.3 Common Result Payload

所有成功和确定性拒绝 Result Event 至少包含：

```text
command_id
operation_id
input_event_id
result_code
result_state_version
```

具体 Event 只能增加固定 schema 允许的最小字段。禁止保存：

- QQ 消息或命令原文；
- 剧本正文、Hidden Truth、Private Knowledge；
- Confirmation token 或 Confirmation 内容；
- Prompt、Context、Reasoning 或 Chain of Thought；
- Player 画像、评价或跨局数据。

### 2.4 Reject 与 Storage Failure

`SESSION_CONTROL_REJECTED` 只表达可确定的领域拒绝，例如 stale version、role/binding 失效、非法 lifecycle/phase 或 scope mismatch。它不用于伪装 Storage Failure。

若 Apply Transaction 未提交，不能单独补写“失败 Result Event”。输入 Event 保持 `RECEIVED/DEFERRED`，Operation 依据持久证据进入 recovery；事务结果无法证明时按 `UNKNOWN` 处理。

## 3. Event Envelope Extension

### 3.1 决策

扩展现有 `GameEvent`，不建立新 Envelope。以下字段成为统一 Event Contract：

|字段|约束|
|---|---|
|`visibility`|受控枚举：PUBLIC/CHARACTER_PRIVATE/DM_CONTROL/SYSTEM_ONLY|
|`observed_state_version`|输入或派生时观察到的版本；无版本语义的 root/system Event 可空|
|`causation_event_id`|派生 Event 必填；root Event 可空|
|`schema_version`|正整数；决定 Event Type 对应 payload schema|

Storage metadata 继续分离：

- `sequence_no` 由 Ingest/Apply Transaction 分配；
- `received_at` 由 Event Store 生成；
- `processing_status` 保存在 processing projection；
- 它们不由外部 Event producer 自行指定。

### 3.2 Persistence Migration

持久 `game_events` 需要版本化 migration，补充 visibility、observed state version、causation event ID、schema version 和 received timestamp。已有 Event 必须使用明确 backfill 规则迁移为旧 schema version，不能根据 payload 自由猜测权限或因果。

迁移必须保持：

1. Event Envelope append-only；
2. 同一 game/session 的 sequence 单调语义；
3. Event processing projection 与 Envelope 分离；
4. 重复 event ID 返回既有一致结果；
5. Result Event 不能覆盖或原地修改输入 Event；
6. T+5 retention 仍是批量删除 Event 内容的唯一边界。

### 3.3 Result Event Processing

Existing Session Apply 中：

- 输入 `DM_COMMAND` 已在 Ingest Transaction 中处于 `RECEIVED`；
- Apply Transaction 将输入 Event 收束为 `APPLIED` 或 `REJECTED`；
- Result Event 在同一事务插入，初始 processing status 为 `RECEIVED`；
- Result Event 获得 Event Store 当前可用的下一个 sequence；
- Actor commit 后通知 mailbox，通知丢失由 Recovery 从 Event Store 重建。

Result Event 仍按 sequence 处理，不能越过已先行 ingest 的 Event。`causation_event_id` 表达直接因果，不替代 sequence 顺序。

## 4. Actor Apply Boundary Design

### 4.1 候选方案

|方案|优点|风险|结论|
|---|---|---|---|
|Actor 直接 async persistence|调用链短，Actor 明确发起提交|领域 Actor 直接耦合 I/O/Repository；改变同步接口；容易跨 await 持有可变状态或锁|不采用|
|Actor 生成 Apply Request|领域计算与持久化隔离；容易测试|若 Actor 不等待结果即处理下一 Event，会破坏 mailbox 顺序；单独使用不完整|作为推荐方案的一部分|
|Actor-owned Apply Coordinator|可封装 async Port、错误归类和 commit receipt；保持领域与 adapter 分离|若 Coordinator 拥有队列、状态或独立重试权，会成为第二 Writer|采用，附加严格限制|

### 4.2 冻结执行模型

```text
Per-Session Async Mailbox Turn
  -> Actor loads next persisted Event
  -> Actor revalidates scope/auth/version/confirmation
  -> Pure Control Reducer derives immutable ControlApplyPlan
  -> Actor awaits Actor-owned Apply Coordinator
  -> Coordinator calls SessionControlApplyPort
  -> Persistence returns ControlApplyReceipt
  -> Actor swaps committed Snapshot/cursor
  -> Actor publishes committed Result Event notification
  -> Actor processes next mailbox Event
```

### 4.3 单写者约束

- Actor 在当前 Event 得到 commit/reject/recovery 结论前不得处理下一 Event；
- Coordinator 不拥有 mailbox、不缓存权威 Session、不生成业务 Delta、不直接重试；
- Coordinator 只能提交 Actor 已产生的不可变 ApplyPlan；
- Persistence Adapter 只能验证并原子写计划，不能新增领域状态转换；
- Actor 的内存 Snapshot 只在 commit receipt 返回后替换；
- Apply 失败必须丢弃 candidate state，不能让内存状态领先数据库；
- 不在 `threading.RLock` 中跨 `await`，使用单 Session logical turn/async processing gate；
- 同一 game 不能同时启用旧同步 state updater 与新 async Apply path；
- 不引入进程、Container、分布式 Actor、lease 或全局 Coordinator。

当前同步 Actor skeleton 可以保留为 P3-A contract-test surface，但不能成为 P3-D Control Event 的生产写路径。Actor Registry 对每个 game 只能发布一个权威 Runtime handler。

## 5. Atomic Apply Extension Design

### 5.1 ControlApplyPlan

`ControlApplyPlan` 是 Actor 产生的不可变领域计划，至少包含：

```text
game_id / session_id / group_id
command_id / command_type
operation_id / operation_claim_id
input_event_id / input_sequence_no
expected_state_version
expected_last_applied_sequence_no
expected_requester_binding_version
candidate_session_snapshot
participant/setup projection mutations
ownership intent
result events
terminal operation result
```

Plan 不能包含 SQL、connection、Repository、QQ正文、剧本正文、Hidden Truth、Private Knowledge 或 Reasoning。Candidate State 以 copy-on-write 方式产生；Reducer 不能预先修改 Actor 当前权威 Snapshot。

### 5.2 ControlApplyReceipt

Persistence 成功后返回只读 `ControlApplyReceipt`：

```text
game_id / session_id
committed_state_version
committed_last_applied_sequence_no
result_event_ids
operation_status
ownership_generation (nullable)
commit_evidence_reference
```

Receipt 是 Actor 更新内存视图的唯一依据，不替代持久 Snapshot，也不授予调用方额外权限。

### 5.3 Existing Session Apply Transaction

Apply Transaction 一次提交：

1. 验证 Operation claim 为当前 command/event/scope；
2. 验证输入 Event 为该 Snapshot cursor 后的最小未终结 sequence；
3. CAS 校验 `state_version`、cursor、binding version 和 ownership generation；
4. 写 Session、Participant、Setup 与 ownership delta；
5. 将输入 Event processing 更新为 `APPLIED`；
6. 插入所有 Result Event 和 processing projection；
7. 更新 Snapshot cursor 和新 `state_version`；
8. 将 Operation 收束为 `SUCCESS` 并写 result evidence；
9. Commit。

START、PAUSE、RESUME、END、CHANGE_PHASE、SET_SCRIPT、ASSIGN_CHARACTER 和 REPLACE_PLAYER 都必须使用该边界。不得先更新 Snapshot，再分别调用 Event Store 或 Operation Repository 补写结果。

### 5.4 Deterministic Reject Transaction

确定性拒绝原子提交：

- Session 业务字段与 `state_version` 不变；
- 输入 Event processing 进入 `REJECTED`；
- `last_applied_sequence_no` 前进到输入 Event sequence；
- 插入 `SESSION_CONTROL_REJECTED`；
- Operation 进入 `FAILED` 或 `CANCELLED`；
- Commit。

Storage/CAS/ownership 不确定不是确定性拒绝。此时整个事务回滚，Actor 停止该局继续 Apply并进入 fail-closed recovery。

### 5.5 Lifecycle Clarification

本 Supplement 不改变 P3-D 主文状态矩阵：`END_GAME` 允许从 CREATED、RUNNING 或 PAUSED 进入 ENDED。P3-D-6 实现不得因局部任务示例省略 CREATED 而删除 `CREATED -> ENDED`。

## 6. Persistence Port Extension

### 6.1 SessionControlApplyPort

新增中立 Port：

```text
SessionControlApplyPort
  claim_operation(...)
  commit_control_apply(plan, claim)
  commit_control_rejection(reject_plan, claim)
  create_session_with_event(bootstrap_plan)
```

职责：

- 提供 Control Operation CAS claim；
- 验证 expected version、cursor、scope 和 claim；
- 原子写 State/Projection、Event processing、Result Event、Operation terminal state；
- 返回 commit receipt 或结构化 conflict/failure；
- 对重复 command/result ID 返回既有一致证据；
- 不执行领域状态机、不调用 Audit/LLM/QQ、不自动重试 UNKNOWN。

### 6.2 调用权限

允许调用：

- 当前 game 的权威 Session Actor 所拥有的 Apply Coordinator；
- CREATE 的 provisional Actor 通过受限 bootstrap coordinator；
- Recovery Coordinator 仅按持久 claim/evidence 执行 reconciliation，不重新生成业务决策。

禁止调用：

- Command Parser、Session Resolver；
- Authorization、Confirmation Policy；
- NoneBot/QQ Adapter、Mode Router；
- Normal Mode、Agent Tool、LLM、Memory；
- 任意 Repository 外部调用方。

Control Plane 只获得 Command、治理结果和只读状态 Port，不能持有 `SessionControlApplyPort`。Port 实现不能泄露 SQLite connection 或通用 SQL handle。

### 6.3 兼容性

现有 P3-C `apply_event` 保留原有 contract，可作为非 Control Event 的兼容基础。P3-D 新 Port 是复合事务扩展，不允许通过串联多个现有 Repository 调用模拟原子提交。SQLite Adapter 可以复用内部序列化、Participant 和 ownership helper，但不能改变其领域含义。

## 7. Control Operation Design

### 7.1 为什么不复用 game_actions

`game_actions` 面向 Hunter/Communication Action，要求现有 Session 外键并承载外部副作用执行语义。Session Control Operation：

- 在 `CREATE_SESSION` 时先于 Session row 存在；
- 由真人 DM/Bootstrap Controller产生，不是 Hunter Planner Action；
- 结果是 Aggregate/ownership 原子状态变化；
- 需要绑定 command、confirmation、input/result Event 和 state version；
- 不得伪装为 Agent Tool 或普通 Game Action。

因此使用独立 `game_control_operations`，只复用 CREATED/EXECUTING/SUCCESS/FAILED/UNKNOWN/CANCELLED 状态语义和 idempotency/claim 原语。

### 7.2 Operation Record

至少包含：

|字段|语义|
|---|---|
|`operation_id / command_id`|稳定身份与幂等键；command ID 唯一|
|`command_type`|冻结枚举|
|`game_id / session_id / group_id`|单局 scope；CREATE 使用 prospective IDs|
|`requester_principal_ref / binding_version`|Session-scoped requester evidence|
|`status`|CREATED/EXECUTING/SUCCESS/FAILED/UNKNOWN/CANCELLED|
|`observed_state_version`|Actor Apply 前重验|
|`payload_fingerprint`|幂等和 Confirmation binding；不保存 payload 正文|
|`authorization_ref / confirmation_ref / audit_ref`|治理证据安全引用|
|`input_event_id / correlation_id`|输入和因果链|
|`claim_id / claimed_at`|单执行者 CAS claim|
|`result_event_id(s) / result_state_version`|终态证据|
|`result_code / failure_class`|固定安全分类|
|`created_at / updated_at / expires_at`|生命周期与 recovery|

### 7.3 状态与 Recovery

```text
CREATED -> EXECUTING -> SUCCESS
                    -> FAILED
                    -> UNKNOWN
CREATED ----------------------> CANCELLED
```

- 重复 command ID 必须返回既有 Operation/Result evidence，不创建新 Event 或 State delta；
- claim 使用 CAS，只有一个 Actor turn 可以进入 EXECUTING；
- SUCCESS/FAILED/CANCELLED 为终态，不重放；
- EXECUTING 在 Recovery 中以 Snapshot、input/result Event 和 version 证据收束；
- 无法证明是否提交时进入 UNKNOWN；
- UNKNOWN 不自动重试、不自动创建等价替代 Operation。

### 7.4 Schema Extension

Persistence schema 需要版本化增加：

- `game_control_operations`；
- `game_session_creation_guards`；
- 未结束 Session 的 group 唯一约束；
- Participant `binding_version`；
- Session Setup/Script Projection；
- Session started/ended/retention metadata；
- Event Envelope 补充字段。

以上属于 additive schema migration，不改变 Snapshot-authoritative、Event append-only、两阶段 Event processing 或 ownership 的 P3-C 语义。

## 8. CREATE_SESSION Bootstrap Freeze

### 8.1 冻结流程

```text
Authenticated Prospective DM
  -> Global + Game Mode group gate
  -> Reservation Transaction
       creation guard + immutable game/session IDs
       + Control Operation(CREATED)
  -> Build CREATED/LOBBY candidate aggregate
  -> Create provisional Actor
  -> Deliver DM_COMMAND(CREATE_SESSION)
  -> Actor derives BootstrapApplyPlan
  -> create_session_with_event
  -> Atomic Commit
  -> Publish restricted CREATED Actor
```

### 8.2 Reservation

Reservation Transaction：

- 对 group 建立唯一、持久 creation guard；
- 校验不存在未结束 Session 或其他有效 guard；
- 分配不可由 caller 覆盖的 game ID/session ID；
- 持久化 prospective scope 的 Control Operation(CREATED)；
- 不创建 Session、不建立 ownership、不进入 Router。

Creation guard 解决并发窗口；数据库还必须约束同一 group 最多一个 CREATED/RUNNING/PAUSED Session。仅依赖进程锁或查询后插入不满足要求。

### 8.3 Provisional Actor

Provisional Actor：

- 只绑定 reservation 中的 game/session/group/command；
- 只处理该 `CREATE_SESSION` Event；
- 不进入 active Actor Registry；
- 不拥有群消息 routing ownership；
- 不接收普通消息、LLM Result 或 Action Result；
- 失败后丢弃，不暴露部分 Session。

### 8.4 create_session_with_event Transaction

最终事务一次完成：

1. 验证 creation guard、Operation、requester 和 prospective IDs；
2. 插入 CREATED/LOBBY Session Snapshot；
3. 插入初始 ACTIVE DM Participant 和 binding version；
4. 插入 `DM_COMMAND(CREATE_SESSION)` 并收束为 `APPLIED`；
5. 插入 `SESSION_CREATED` Result Event；
6. 将 Operation 更新为 SUCCESS并记录 result evidence；
7. 完成/消费 creation guard；
8. Commit。

物理 SQL 可以因外键先插入 Session row，再插入 Event，但两者必须位于同一事务且对外不可部分观察。该物理顺序不改变“Command 经 provisional Actor 形成权威 State”的领域因果。

Actor 对象不能进入数据库事务。Commit 后发布 Actor；若进程在 commit 后、publish 前崩溃，Recovery 从 Session/Event/Operation 重建。不得因 publish 失败回滚或删除已提交 Session。

### 8.5 Ownership

CREATED 不建立 `game_active_ownership`，Mode Router 不接管该群普通消息。只有 `START_GAME` 的 Atomic Apply 成功后，才同时建立 RUNNING ownership。PAUSE 保留 ownership；END 在同一事务解除 ownership。

### 8.6 禁止

- Command Handler 或 Resolver 直接 INSERT Session；
- 先创建 Session、之后补 Event/Operation；
- 以普通 `create_session` 替代 bootstrap transaction；
- 在事务成功前发布 Actor 或 routing ownership；
- 使用 System Admin 身份绕过 Bootstrap Controller；
- CREATE 失败后自动生成新的 command ID 重试。

## 9. Implementation Roadmap

后续顺序冻结为：

```text
P3-D-6.0 Freeze Supplement / ADR
  -> 本文人工确认

P3-D-6.1 Event Schema Extension
  -> Event Type、Envelope、payload schema、migration、compatibility tests

P3-D-6.2 Control Operation Persistence
  -> Operation、creation guard、claim、idempotency、recovery evidence

P3-D-6.3 Atomic Apply Port
  -> ApplyPlan/Receipt、apply/reject transaction、Result Event insertion

P3-D-6.4 CREATE Bootstrap
  -> reservation、provisional Actor contract、create_session_with_event

P3-D-6.5 Actor Apply Adapter
  -> async mailbox turn、pure reducer、Actor-owned Coordinator、commit-then-swap

P3-D-6.6 Lifecycle / Phase Apply
  -> START/PAUSE/RESUME/END/CHANGE_PHASE

P3-D-6.7 Setup / Participant Apply
  -> SET_SCRIPT/ASSIGN_CHARACTER/REPLACE_PLAYER

P3-D-6.8 Recovery / Idempotency
  -> duplicate command、EXECUTING/UNKNOWN、notification recovery、fail closed
```

每一子阶段必须有独立 allowlist、contract tests 和人工确认。不得在 Event Schema 阶段提前实现 State Apply，不得在 Actor Adapter 阶段接入生产 QQ ingress。

## 10. 风险、边界与 ADR 分类

### 10.1 本 ADR 授权的小范围扩展

- 增加受控 Control Result/Reject Event Type 和 payload validator；
- 增加统一 GameEvent Envelope 字段；
- 增加不可变 ApplyPlan、RejectPlan 和 CommitReceipt；
- 增加 `SessionControlApplyPort`；
- 增加 Actor-owned Apply Coordinator；
- 增加 Control Operation、creation guard 和必要 projection；
- 增加版本化 schema migration 与兼容测试；
- 增加 Result Event mailbox notification 和 recovery evidence；
- 增加 lifecycle/phase/setup/participant reducer 和测试。

这些扩展必须保持 P2/P3-C 的原有语义，不能被解释为授权重构整个 Runtime。

### 10.2 本 ADR 明确冻结的变化

以下变化只有在本文人工接受后才允许：

- Event Schema 修改；
- Persistence Schema 修改；
- Actor Control Event 接口修改；
- Atomic Apply 事务扩展；
- CREATE bootstrap transaction；
- Result Event processing/cursor 规则；
- Reject cursor 前进但不递增业务 `state_version`；
- 同群未结束 Session 的数据库唯一约束。

若实现需要超出本文描述继续修改这些边界，必须再提交新 ADR。

### 10.3 永久禁止

- 重构或替换 Agent Runtime / Game Runtime 顶层结构；
- 创建第二套 Event Store、Event Envelope 或 Actor Model；
- 创建第二个 Session State Writer；
- 让 Coordinator、Repository、LLM callback 或 Adapter直接修改 State；
- 修改 Normal Mode、Mode Router 默认行为或生产 Message Ingress；
- 把 Game Runtime 实现为 Plugin、Tool 或 Memory 子模块；
- 引入 LLM、Context Builder、Reasoning、Knowledge、Hunter Agent；
- 引入 QQ发送、私聊、语音、图片或 Action Executor；
- 接入 Companion/User/Semantic/Graph Memory；
- 引入分布式 Actor、lease、Container 或 Multi-Agent；
- 保存 QQ原文、剧本秘密、Hidden Truth、Private Knowledge 或完整 Chain of Thought。

### 10.4 Failure Boundary

- Event ingest failure：不投递 Actor、不修改 State；
- operation claim conflict：返回既有 evidence 或 fail closed；
- stale state/binding：确定性 reject transaction，不修改业务 version；
- Atomic Apply failure：candidate state 丢弃，输入 Event 保持未终结，Session 进入 recovery/fail closed；
- commit outcome uncertain：先按 operation/result event/snapshot evidence reconciliation；不能证明时 UNKNOWN；
- Result Event notification failure：不回滚 commit，由 Event Store/Recovery重新通知；
- Actor publish failure after CREATE commit：不删除 Session，交 Recovery 重建；
- ownership conflict：事务回滚，不抢占、不回落 Normal Mode。

## 11. Architecture Invariants

1. Game Runtime 仍是与 Normal Mode 并列的顶层 Mode。
2. GameEvent 是唯一 Runtime Event 协议。
3. Snapshot 是恢复权威，Event Store 不是完整 Event Sourcing。
4. 同一 Session 只有一个 Actor/State Writer。
5. Coordinator 只提交 Plan，不产生 State Delta。
6. Actor 在 commit 结论前不处理下一 mailbox Event。
7. Operation 必须先持久化，再允许 Control Apply。
8. accepted `DM_COMMAND` 必须先 ingest，再投递 Actor；CREATE 仅使用本文冻结的 bootstrap exception。
9. State、input processing、Result Event、Operation terminal 和相关 ownership/projection 必须原子提交。
10. 内存 Snapshot 只能在 commit 后更新。
11. CREATED 不拥有 active routing ownership。
12. START 建立 ownership，PAUSE 保留，END 解除，全部与 State 原子提交。
13. expected state version、cursor、binding version 和 ownership generation 必须在 Apply 内重验。
14. Reject 不递增业务 state version，但必须以受控 cursor/processing 语义收束输入 Event。
15. UNKNOWN 不自动重试。
16. DM 仍只控制当前 game，不成为 System Admin。
17. Game Control 不进入 Normal Mode、Agent Tool、普通 Memory 或生产 QQ路径。
18. P3-D-6 不包含 LLM、Knowledge、Reasoning、Hunter 或 Action Execution。

## 12. Implementation Entry Gate

进入 P3-D-6.1 前必须人工确认：

- [ ] Control Result/Reject Event registry；
- [ ] GameEvent Envelope extension 与 migration；
- [ ] Result Event processing/sequence/cursor 语义；
- [ ] Actor-owned Apply Coordinator；
- [ ] copy-on-write candidate state 与 commit-then-swap；
- [ ] ControlApplyPlan/Receipt；
- [ ] SessionControlApplyPort 调用边界；
- [ ] independent Control Operation 和状态机；
- [ ] CREATE reservation、provisional Actor 与 atomic hook；
- [ ] schema version migration 和同群唯一约束；
- [ ] failure/recovery/idempotency 规则；
- [ ] Implementation Roadmap 顺序；
- [ ] Non-Goals 与全部 Architecture Invariants。

人工确认前：

```text
P3-D-6 Implementation = BLOCKED
Production integration = DISABLED
P3-D-7 = NOT STARTED
```

本文完成 P3-D-6 Actor Apply Freeze Supplement Proposal。Implementation 尚未开始。
