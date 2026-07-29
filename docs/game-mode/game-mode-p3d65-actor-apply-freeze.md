# Game Mode P3-D-6.5 Actor Apply Adapter Freeze Supplement

- 文档类型：ADR / Design Supplement
- 状态：Draft Freeze Proposal
- 目标阶段：P3-D-6.5 Actor Apply Adapter
- 当前分支：`feature/game-mode-runtime-v01`
- 代码基线：`057c610160babbbc4cd3750410484e6e710af798`
- 上位约束：[Architecture Freeze P0.2](game-mode-architecture-freeze.md)、[P2 Detailed Design](game-mode-p2-detailed-design.md)、[Implementation Constraint Freeze P2.1](game-mode-implementation-freeze.md)
- 直接前置：[P3-C Persistence Design](game-mode-p3c-persistence-design.md)、[P3-D Session Control Plane Design](game-mode-p3d-session-control-design.md)、[P3-D-6 Actor Apply Freeze Supplement](game-mode-p3d6-actor-apply-freeze.md)
- 范围：Control Event Delivery、Actor async processing、Operation claim transfer、ApplyPlan Builder、Atomic Apply 调用和 Result notification
- 非目标：代码、生产接管、业务 Reducer、Normal Mode、LLM、Memory、Knowledge、Reasoning、Hunter Agent、QQ发送和 Action Executor

## 0. 文档权威性与准入状态

本文补充 P3-D-6.5 Actor Apply Adapter 实现所需的执行契约，不替代或放宽任何上位 Freeze。发生冲突时，优先级为：

1. P0.2 Architecture Freeze；
2. P2.1 Implementation Constraint Freeze；
3. P3-D Session Control Plane Design Freeze；
4. P3-D-6 Actor Apply Freeze Supplement；
5. 本 Supplement；
6. P3-C Persistence Design 与 P2 Detailed Design 的细节映射。

本文在人工确认前保持 `Draft Freeze Proposal`。确认前不得进入 P3-D-6.5.1 或后续实现。确认后，本文成为 P3-D-6.5 Implementation 的唯一补充基线；任何实现若需要改变本文的 delivery、mailbox turn、claim、Builder、Receipt 或 Result Event 边界，必须先新增 ADR。

本文只授权后续阶段按冻结契约进行小范围扩展，不授权修改 Normal Mode、生产 Message Ingress、Mode Router 默认行为、P3-C 的 Snapshot-authoritative 语义或 GameEvent 唯一事件协议。

## 1. 背景与 Freeze Gate

P3-D-6.1 至 P3-D-6.4 已补充 Control Result Event、Control Operation、Atomic Apply Port 和 CREATE bootstrap contract。进入 P3-D-6.5 时，现有实现仍缺少从已持久化 `DM_COMMAND` 到 Actor-owned Atomic Apply 的安全连接边界：

1. 当前 `GameSessionActor` 使用同步 mailbox、同步 drain 和 `threading.RLock`，不能在不改变 turn contract 的情况下等待异步 `SessionControlApplyPort`；
2. Actor 当前只接收裸 `GameEvent`，但 Control Apply 还需要 Event sequence、Operation identity、State version、requester binding 和持久化引用；
3. `ControlApplyPlan` 的 Candidate State Change 依赖 Lifecycle、Phase、Setup、Participant 和 Character reducer，而这些 reducer 分别属于 P3-D-6.6 和 P3-D-6.7；
4. Result Event 必须在 Atomic Apply 前产生，并与 State、输入 Event processing、Operation terminal、`state_version` 和 cursor 同事务提交；Actor 不得在 commit 后自行生成或补写结果事件。

这些缺口不是 P3-A、P3-C 或 P3-D-6 的架构错误，而是同步 Actor skeleton 与异步、持久、原子 Control Apply 之间尚未冻结的 adapter contract。P3-D-6.5 必须补齐该边界，同时保持 Actor 单写者、mailbox 顺序和 Session State 唯一来源。

冻结原则：

- `GameEvent` 仍是唯一 Runtime Event 协议；
- 新增 delivery envelope 只携带投递证据，不成为第二套 Event；
- Actor 仍是唯一 State Writer；
- Coordinator 不拥有 State、mailbox 或独立重试权；
- Builder 是纯函数边界，不进行 I/O 或 State mutation；
- Result Event 只能作为 ApplyPlan 的组成部分进入 Atomic Apply；
- Storage/CAS/commit outcome 不确定不得伪装成确定性业务拒绝。

## 2. Control Event Delivery Envelope

### 2.1 决策

新增不可变传输契约 `ControlEventDeliveryEnvelope`，用于将已经持久化并绑定 Control Operation 的 `DM_COMMAND` 安全投递给目标 Session Actor。

`ControlEventDeliveryEnvelope`：

- 不是 Event；
- 不替代、复制或扩展 `GameEvent` 的领域语义；
- 不进入第二套 Event Store；
- 不携带可变 Session State；
- 不授予调用方新的权限；
- 可由 Event Store、Control Operation 和治理证据重建。

### 2.2 冻结字段

|字段|语义与约束|
|---|---|
|`event`|已经持久化的 `GameEvent`；必须为 `DM_COMMAND`|
|`event_sequence_no`|Event Store 为当前 Session 分配的持久 sequence；必须大于零|
|`operation_id`|与 Command/Event 唯一绑定的 Control Operation|
|`command_id`|稳定幂等身份；必须与 Event payload 和 Operation 一致|
|`observed_state_version`|Command 产生时观察到的 Session version|
|`requester_principal_ref`|Session-scoped requester 引用，不保存 QQ 原文或额外画像|
|`requester_binding_version`|Actor Apply 前必须重验的身份代次|
|`authorization_reference`|已完成 Authorization 的安全证据引用，不等同于执行授权缓存|
|`confirmation_reference`|可空；需要 Confirmation 时必须存在并可重验|
|`correlation_id`|Command、Operation、Event、Audit 和 Recovery 的关联键|
|`stored_event_reference`|Event Store 中输入 Event 的稳定引用或记录身份|

Envelope 不包含 `operation_claim_id`。Operation claim 只能在 Actor 决定开始当前 mailbox turn 后，由 Actor-owned Coordinator 通过 CAS 获得，不能由 Ingress 预先 claim。

### 2.3 一致性校验

进入 Actor mailbox 前必须校验：

- `event.event_type == DM_COMMAND`；
- Event、Operation、Command 的 `game_id/session_id/command_id` 一致；
- Envelope 的 `correlation_id` 与 Event/Operation 一致；
- `event_sequence_no` 与 `stored_event_reference` 指向同一持久记录；
- requester principal 与 binding evidence 属于目标 Session；
- `observed_state_version` 与 Event Envelope/payload 一致；
- Event 已成功进入 Event Store，未持久化 Event 不得投递。

校验失败时 fail closed：不进入 Actor、不修改 State、不回落 Normal Mode，并记录 privacy-safe GAME Audit metadata。

### 2.4 Producer、Consumer 与生命周期

Producer 是 `Control Event Ingress Adapter`。它只在 `DM_COMMAND` 已持久化、Operation 已创建并绑定 input Event 后构造 Envelope。

Consumer 是目标 `game_id` 的唯一权威 Session Actor。普通 Adapter、Reducer、Repository、Coordinator 或 Normal Mode 不得消费 Envelope 并自行 Apply。

冻结生命周期：

```text
Command/Event Ingress
  -> Persist DM_COMMAND
  -> Bind Control Operation
  -> Build ControlEventDeliveryEnvelope
  -> Deliver to authoritative Session Actor
  -> Begin one ordered control turn
```

Envelope 是进程内、短生命周期、可重建的 delivery object，不作为新的业务事实独立持久化。通知丢失时，Recovery 从 Event Store、Operation、Snapshot 和治理证据重建；不得生成新的 Command ID 或等价替代 Event。

## 3. Actor Async Processing Boundary

### 3.1 方案比较

|方案|结论|原因|
|---|---|---|
|A：Actor 直接 async persistence|不采用|领域 Actor 直接耦合 Persistence；容易跨 `await` 持有锁或可变 State；模糊 Actor 与 adapter 边界|
|B：Actor 发送 Apply Request 后继续处理|不采用|当前 Event 未收束时处理下一 Event 会破坏 mailbox sequence、cursor 和单写者语义|
|C：Actor-owned Async Gateway / Apply Coordinator|采用|Actor 控制 turn 和 State；Coordinator 只封装 claim、Atomic Apply 调用、错误分类和 Receipt 返回|

### 3.2 冻结执行模型

```text
ControlEventDeliveryEnvelope
  -> Per-session async processing gate
  -> Actor revalidates scope/version/binding/governance evidence
  -> Actor-owned Coordinator claims Operation
  -> ControlApplyPlanBuilder derives immutable Plan or RejectPlan
  -> Coordinator awaits SessionControlApplyPort
  -> ControlApplyReceipt
  -> Actor validates committed evidence
  -> Actor performs commit-then-swap of in-memory Snapshot/cursor
  -> Actor publishes committed Result Event notification
  -> Release gate
  -> Process next mailbox item
```

### 3.3 Async Gate 与 mailbox 顺序

- 每个 Session 只有一个 logical async processing gate；
- 一个 control turn 在得到 commit、deterministic reject 或 recovery/fail-closed 结论前，不得开始下一 mailbox turn；
- gate 保护的是 Actor turn，不是第二个队列或全局调度器；
- 不允许在 `threading.RLock` 内跨 `await`；
- 不允许以 `asyncio.run`、fire-and-forget task 或后台 callback 绕过顺序；
- 不同 Session 可以异步并行，同一 Session 必须按 Event Store sequence 串行；
- 同一 Session 不得同时启用旧同步 state updater 和新 Control Apply path。

P3-A 的同步 Actor skeleton 可以保留为兼容 contract-test surface，但不能成为 P3-D Control Event 的生产 State 写路径。Actor Registry 对每个 `game_id` 只能发布一个权威 Runtime handler。

### 3.4 Coordinator 边界

Coordinator：

- 由当前 Session Actor 拥有并在 Actor turn 内调用；
- 不拥有 mailbox；
- 不缓存或替代权威 Session Snapshot；
- 不生成 Candidate State、mutation 或业务 Result Event；
- 不直接访问 SQLite 或具体 Repository；
- 只能调用 `SessionControlApplyPort`；
- 不独立调度、不自动重试、不并行处理同一 Operation；
- 只返回 typed claim、Receipt 或 typed failure 给 Actor。

因此 Coordinator 是 Actor 的 I/O gateway，不是第二个 State Writer、第二个 Actor 或新的 Runtime Loop。

## 4. Operation Claim Transfer

### 4.1 决策

Operation claim 由 Actor-owned Coordinator 在当前 Actor turn 内负责。Ingress 只创建 Operation、绑定 input Event 和投递 Envelope，不得把 Operation 提前置为 `EXECUTING`。

```text
ControlEventDeliveryEnvelope
  -> Actor begins ordered turn
  -> Coordinator calls claim_operation()
  -> CAS: CREATED -> EXECUTING
  -> ControlOperationClaim
  -> ControlApplyPlanBuilder
```

### 4.2 职责分配

|组件|职责|禁止|
|---|---|---|
|Ingress|创建/定位 Operation，绑定 input Event，构造 Envelope|claim、Apply、重试|
|Actor|决定当前 sequence 何时进入执行 turn，持有唯一 State 写权|直接写 Repository|
|Coordinator|通过 Port 执行 CAS claim，返回不可变 Claim|生成业务 Delta、独立重试|
|Builder/Reducer|消费 Claim 和不可变 Snapshot，产生 Plan|修改 Operation、执行 I/O|
|Atomic Apply|在事务内再次校验 Claim 并收束 Operation|信任未经重验的内存 Claim|
|Recovery|基于持久证据处理悬挂或不确定 Operation|无证据自动重放|

### 4.3 Claim 与幂等规则

- `command_id` 仍是控制操作的稳定幂等键；
- claim 使用 CAS，只允许 `CREATED -> EXECUTING`；
- Claim 必须绑定 game、session、command、operation、input Event、claim ID 和 claim time；
- 已为 `SUCCESS/FAILED/CANCELLED` 的重复 Command 返回已有持久 Result evidence，不重新执行；
- 已为 `EXECUTING` 时禁止第二执行者进入，由 Recovery 根据 Snapshot、input/result Event、version 和 Operation evidence reconciliation；
- `UNKNOWN` 不自动重试，也不创建等价替代 Operation；
- claim conflict、证据缺失或 scope mismatch 均 fail closed，不回落 Normal Mode；
- Atomic Apply 必须重验 `operation_claim_id`，内存 Claim 不能替代事务 CAS 证据。

## 5. ApplyPlan Builder Boundary

### 5.1 决策

新增纯领域边界 `ControlApplyPlanBuilder`。Actor 负责选择当前 Event 和调用 Builder，但不在 adapter 内直接编写 Lifecycle、Phase、Setup、Participant 或 Character mutation。

Builder 输入：

```text
ControlEventDeliveryEnvelope
+ ControlOperationClaim
+ Immutable Session Snapshot
+ Participant Projection
+ Setup Projection
+ Ownership evidence required by the command
```

Builder 输出必须是以下之一：

- `ControlApplyPlan`：确定性允许并可提交的 Candidate State 与 Result Events；
- `ControlRejectPlan`：确定性领域拒绝与 `SESSION_CONTROL_REJECTED`；
- typed non-commit outcome：证据不足、Storage/CAS 不确定或需要 Recovery，不伪造 Reject Event。

### 5.2 纯函数约束

Builder 必须：

- 无 I/O；
- 不访问 Repository、SQLite、Event Store 或网络；
- 不调用 LLM、Memory、Knowledge、Reasoning 或 Normal Mode；
- 不修改 Actor 当前 Session 或任何 projection；
- 使用 copy-on-write 构造 Candidate Snapshot；
- 在 Plan 中携带 expected version、cursor、binding、ownership、Claim 和 Result Event；
- 只使用固定 enum、typed mutation 和固定 reason code；
- 不保存 QQ 原文、剧本正文、Hidden Truth、Private Knowledge 或完整推理过程。

### 5.3 Adapter 阶段边界

P3-D-6.5 只冻结并接入 Builder contract，不实现具体业务 reducer。Contract test 可以使用确定性 fake Builder 验证 wiring，但生产注册必须保持关闭，直到 P3-D-6.6 和 P3-D-6.7 提供并验证全部 reducer。

Actor Apply Adapter 不得以空 mutation、默认成功、通用字符串 reducer 或直接修改 Session 的方式代替后续 reducer。

## 6. Result Event 生命周期

### 6.1 产生时机

Result Event 必须由对应纯 Reducer 在 Atomic Apply 前构造，并作为 `ControlApplyPlan` 或 `ControlRejectPlan` 的不可变组成部分。

```text
Reducer
  -> Result Event(s)
  -> ControlApplyPlan / ControlRejectPlan
  -> Atomic Apply Transaction
       State / Projection
       Input Event processing
       Result Event(s)
       Operation terminal state
       state_version / cursor
       ownership evidence when applicable
  -> ControlApplyReceipt
  -> Actor receipt validation
  -> committed Result Event notification
```

### 6.2 Receipt 语义

Receipt 是已提交结果的只读证据，至少用于验证：

- `game_id/session_id`；
- committed `state_version`；
- committed cursor；
- committed Result Event IDs；
- Operation terminal status；
- ownership generation；
- commit evidence reference。

Receipt 不生成 Result Event、不替代持久 Snapshot，也不授权 Actor 或 Coordinator 补写任何持久记录。

### 6.3 Commit-then-swap 与通知

- Actor 只能在 Receipt 验证成功后，用已提交 Candidate Snapshot/cursor 替换内存视图；
- Result notification 只引用事务中已经提交的 Result Event；
- Actor 不得在 commit 后重新构造 `SESSION_STARTED`、`SESSION_CONTROL_REJECTED` 或其他结果事件；
- notification 失败不回滚已提交事务，由 Event Store 和 Recovery 重建通知；
- Result Event 仍按 Event Store sequence 处理，notification 不能越过更早的未终结 Event。

### 6.4 Failure 分类

|结果|处理|
|---|---|
|确定性业务拒绝|通过 `ControlRejectPlan` 原子提交 Reject Event、input processing、cursor 和 Operation terminal；业务 `state_version` 不递增|
|CAS/version/binding/ownership conflict|按冻结规则重验；若能确定为领域 stale 可形成 RejectPlan，否则 fail closed|
|Storage failure|事务回滚，不单独补写 Result Event，Session 进入 recovery/fail-closed|
|Commit outcome uncertain|依据 Operation、Snapshot 和 Result Event evidence reconciliation；不能证明时为 `UNKNOWN`|
|Receipt 与 Plan 不一致|不 swap 内存 State，停止该 Session 后续 Apply并进入 Recovery|

## 7. P3-D-6.5 与后续阶段边界

### 7.1 P3-D-6.5 负责

- `ControlEventDeliveryEnvelope` contract；
- Event/Operation/sequence/scope delivery validation；
- per-session async processing gate；
- Operation Claim Bridge；
- `ControlApplyPlanBuilder` interface；
- Actor-owned Apply Coordinator；
- `SessionControlApplyPort` 调用；
- Receipt validation；
- commit-then-swap；
- 已提交 Result Event notification；
- contract tests 和 fail-closed tests。

### 7.2 P3-D-6.5 不负责

- Lifecycle reducer；
- Phase reducer；
- Setup reducer；
- Participant reducer；
- Character binding/replacement reducer；
- Ownership业务规则或 generation 计算；
- CREATE bootstrap transaction implementation；
- Persistence schema/migration 或 Atomic Apply adapter implementation；
- Router、QQ、LLM、Memory、Knowledge、Reasoning、Timeline 或 Hunter Agent。

### 7.3 后续归属

P3-D-6.6 负责 Lifecycle / Phase Apply，包括 START、PAUSE、RESUME、END 和 CHANGE_PHASE 的 Candidate State、Result Event 与 ownership intent。

P3-D-6.7 负责 Setup / Participant Apply，包括 SET_SCRIPT、ASSIGN_CHARACTER 和 REPLACE_PLAYER 的 projection mutation、binding version 与 Result Event。

在 6.6/6.7 reducer 完成并通过人工 Gate 前，P3-D-6.5 只允许 contract-level wiring，不允许启用生产 Control Apply。

## 8. Implementation Roadmap

后续实现顺序冻结为：

```text
P3-D-6.5.0 Freeze Supplement
  -> P3-D-6.5.1 Control Event Delivery Envelope
  -> P3-D-6.5.2 Per-session Async Gate
  -> P3-D-6.5.3 Operation Claim Bridge
  -> P3-D-6.5.4 ApplyPlan Builder Contract
  -> P3-D-6.5.5 Actor-owned Apply Coordinator
  -> P3-D-6.5.6 Receipt and Result Notification
  -> P3-D-6.5.7 Contract Tests
  -> P3-D-6.6 Lifecycle / Phase Apply
  -> P3-D-6.7 Setup / Participant Apply
```

每个子阶段必须：

- 使用独立实现 allowlist；
- 运行 P3-D 与 Game Runtime 回归测试；
- 执行编译、依赖边界和 `git diff --check`；
- 不修改 Normal Mode 或生产 ingress；
- 在人工确认前不进入下一子阶段；
- 不自动 commit。

## 9. 严格禁止事项

本 Freeze 文档阶段禁止：

- 写代码；
- 修改 Actor、mailbox、Event Pipeline 或 Persistence；
- 创建 migration、Runtime Loop、State Store 或第二个 Actor；
- 启用生产 Control Apply。

后续 P3-D-6.5 实现永久禁止：

- 创建第二套 Event、Event Store 或 Event Envelope 领域模型；
- 创建第二个 Session State Writer；
- 让 Coordinator、Repository、Ingress、Reducer 或 callback 直接修改 Session State；
- 让 Actor 绕过 Atomic Apply Port 或直接访问 SQLite/Repository；
- 在 commit 后生成、补写或伪造 Result Event；
- 在 Atomic Apply 得到结论前处理下一 mailbox Event；
- 对 `UNKNOWN`、EXECUTING 或不确定 commit 自动重试；
- 修改 P3-C 的 Snapshot-authoritative、append-only Event、两阶段 processing 或 ownership 语义；
- 修改 Normal Mode、Mode Router 默认行为或生产 Message Ingress；
- 引入 LLM、Hunter Agent、Reasoning、Knowledge System、Timeline、QQ发送、私聊或 Memory；
- 保存 QQ 原文、剧本秘密、Hidden Truth、Private Knowledge 或完整 Chain of Thought。

## 10. Architecture Invariants

1. `GameEvent` 是唯一 Runtime Event 协议，Delivery Envelope 只是可重建投递证据。
2. 同一 Session 只有一个权威 Actor 和一个 State Writer。
3. 同一 Session 的 Control Event 严格按持久 sequence 串行处理。
4. Actor 在当前 turn 得到 commit、reject 或 recovery 结论前不处理下一 Event。
5. Coordinator 只负责 claim 与 Port 调用，不拥有 State、mailbox、业务 Delta或重试权。
6. Operation 必须先持久化并绑定 input Event，再允许 Actor claim。
7. Atomic Apply 必须重验 claim、State version、cursor、binding 和 ownership evidence。
8. Builder 只产生不可变 Plan，不执行 I/O、不修改 Actor Session。
9. Result Event 必须在 ApplyPlan 内形成并与 State、processing、Operation terminal 和版本原子提交。
10. Actor 内存 Snapshot 只能在有效 Receipt 返回后 commit-then-swap。
11. 确定性 Reject 与 Storage/commit uncertainty 必须严格区分。
12. `UNKNOWN` 不自动重试。
13. Control Apply 失败不回落 Normal Mode。
14. DM 权限仍只属于当前 Session，不成为 System Admin。
15. P3-D-6.5 不包含任何业务 Reducer、LLM、Knowledge、Reasoning、Hunter、Memory 或 QQ Action。

## 11. Implementation Entry Gate

进入 P3-D-6.5.1 前必须人工确认：

- [ ] `ControlEventDeliveryEnvelope` 是 transport contract，不是第二套 Event；
- [ ] Envelope 字段、producer、consumer、校验和 recovery 语义；
- [ ] Actor-owned async gate 与同局严格顺序；
- [ ] Coordinator 不是 State Writer、队列或独立重试器；
- [ ] Actor turn 内 CAS claim 和 Claim transfer；
- [ ] `ControlApplyPlanBuilder` 纯函数边界；
- [ ] P3-D-6.5 不实现 6.6/6.7 reducer；
- [ ] Result Event 在 Atomic Apply 前产生并在同一事务提交；
- [ ] Receipt、commit-then-swap 和 Result notification 语义；
- [ ] failure/recovery/UNKNOWN 规则；
- [ ] Implementation Roadmap、Non-Goals 和全部 Architecture Invariants。

人工确认前：

```text
P3-D-6.5 Implementation = BLOCKED
Production integration = DISABLED
P3-D-6.6 = NOT STARTED
```

本文完成 P3-D-6.5 Actor Apply Adapter Freeze Supplement Proposal。Implementation 尚未开始。
