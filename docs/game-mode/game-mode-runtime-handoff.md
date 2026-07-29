# Game Mode Runtime Development Handoff

- 文档类型：Development Handoff / Continuation Guide
- 状态：Current Working Handoff
- 更新时间：2026-07-16
- 项目：`qq-reminder-bot`
- 当前分支：`feature/game-mode-runtime-v01`
- 当前 HEAD：`057c610160babbbc4cd3750410484e6e710af798`
- 当前阶段：P3-D-6.5 Actor Apply Adapter Freeze Gate
- 实现状态：`BLOCKED`，等待人工确认 P3-D-6.5 Freeze Supplement
- 生产集成：未启用

## 1. 新对话快速接续

新对话开始后，先执行只读核验，不要立即修改文件：

```text
1. 确认仓库：D:\猎bot\qq-reminder-bot
2. 确认分支：feature/game-mode-runtime-v01
3. 确认 HEAD：057c610160babbbc4cd3750410484e6e710af798
4. 查看 git status 和 staged 状态
5. 阅读本 handoff 与全部直接 Freeze 文档
6. 等待人工明确接受 P3-D-6.5 Draft Freeze Proposal
7. 接受后只进入 P3-D-6.5.1，不提前实现后续阶段
```

工作区包含尚未提交的 P3-D 累计成果。不得 reset、checkout、clean、覆盖、移动或重写这些文件，也不得把未跟踪文档误判为临时文件。

## 2. 项目定位

Game Mode 是 Agent Runtime 的独立、有状态 Runtime Mode：

```text
Agent Runtime
├── Normal Mode
└── Game Runtime
```

Game Runtime 不是 Plugin、Tool、单次函数调用、`agent/tools` 子模块或普通 Memory 子模块。它拥有独立 Session lifecycle、Event loop、Actor、State、Operation 和恢复边界。

目标角色为 Hunter Game Agent，即 Level 2 AI Player。Hunter 后续负责环境理解、推理、决策和行动；系统权限、Session 管理、信息隔离、Atomic Apply、Audit 和 Recovery 始终由 Runtime 控制。

当前 P3-D 只建设 Session Control Plane，不包含 Hunter、LLM、Reasoning、Knowledge 或 QQ Action。

## 3. 架构冻结原则

### 3.1 文档优先级

发生冲突时依次服从：

1. [Game Mode Architecture Freeze P0.2](game-mode-architecture-freeze.md)；
2. [Implementation Constraint Freeze P2.1](game-mode-implementation-freeze.md)；
3. [P3-D Session Control Plane Design Freeze](game-mode-p3d-session-control-design.md)；
4. [P3-D-6 Actor Apply Freeze Supplement](game-mode-p3d6-actor-apply-freeze.md)；
5. [P3-D-6.5 Actor Apply Adapter Freeze Supplement](game-mode-p3d65-actor-apply-freeze.md)；
6. [P3-C Persistence Design](game-mode-p3c-persistence-design.md) 与 [P2 Detailed Design](game-mode-p2-detailed-design.md) 的细节映射。

Draft Freeze Proposal 必须经人工确认后才能进入对应实现。实现便利不能隐式改变 Freeze；需要改变时先新增 ADR。

### 3.2 永久不变量

1. Game Mode 与 Normal Mode 并列，不成为 Plugin 或 Tool。
2. `RUNNING/PAUSED` Session 对群消息独占；失败时不回落 Normal Mode。
3. DM 是当前 Game Session Controller，不是 System Admin。
4. DM 权限不能修改系统配置、操作 Normal Mode、查看其他 Session 或获得 Global Tool 权限。
5. 同一 Session 只有一个权威 Actor 和一个 State Writer。
6. `GameEvent` 是唯一 Runtime Event 协议，Event append-only。
7. 同局 Event 按持久 sequence 串行处理，`state_version` 单调治理。
8. State、input processing、Result Event、Operation terminal、版本和相关 projection 必须原子提交。
9. Actor 内存 Snapshot 只能在有效 commit receipt 后更新。
10. Game Memory 按 `game_id` 隔离，不进入 Companion/User/Semantic/Graph Memory，不跨局学习。
11. Hidden Truth 不进入 Hunter Context；不保存完整 Chain of Thought。
12. `UNKNOWN` 不自动重试，不推定为成功或失败。
13. P3-D 不接入生产 QQ、LLM、Knowledge、Reasoning、Hunter 或 Memory。

## 4. 已完成阶段

### 4.1 Architecture 与 Mapping

|阶段|状态|主要结果|
|---|---|---|
|P0 Architecture|完成|确立 Game Runtime Mode、DM/Identity/Knowledge/Memory 隔离|
|P0.1 Hardening|完成|补充 Event、Phase、Context、Instance、Queue、Concurrency、Failure|
|P0.2 Architecture Freeze|完成|冻结顶层模块、消息独占、Shared Audit、Game Memory 和 V0.1 边界|
|P1 Repository Mapping|完成|完成真实仓库挂载、依赖方向和修改范围分析|
|P2 Detailed Design|完成|冻结 Session、Actor、Event、Knowledge、Decision、Action 与恢复设计|
|P2.1 Implementation Freeze|完成|冻结实现级依赖、安全和阶段纪律|

### 4.2 已提交基础设施

|阶段|状态|Commit|
|---|---|---|
|P3-A Runtime Skeleton|完成并已提交|`cbf820085`|
|P3-B Router Integration|完成并已提交|`170964467`|
|P3-C Persistence / Recovery|完成并已提交|`057c610160babbbc4cd3750410484e6e710af798`|

P3-C 后当前基础链路为：

```text
Message
  -> Mode Router
  -> Game Runtime
  -> Session Actor
  -> Persistence
  -> Recovery
```

### 4.3 P3-D 未提交累计成果

|阶段|状态|能力|
|---|---|---|
|P3-D Design Freeze|完成|冻结 9 个 Command、DM 权限、Confirmation、Operation 与 Atomic Apply 边界|
|P3-D-1 Command Contract|完成|类型安全 Command enum、payload 和 envelope|
|P3-D-2 Resolver + CREATE bootstrap|完成|只读 Session resolve 和 CREATE bootstrap context|
|P3-D-3 Authorization Policy|完成|纯 Decision 权限交集和基础状态约束|
|P3-D-4 Confirmation Framework|完成|高风险命令策略、绑定和有效性验证|
|P3-D-5 Event Integration|完成|受治理 Command 转换为 `DM_COMMAND` 并进入 Event pipeline|
|P3-D-6 Freeze Supplement|完成|冻结 Result Event、Operation、Atomic Apply、CREATE transaction|
|P3-D-6.1 Event Schema Extension|完成|Control Result/Reject Event 与 typed payload contract|
|P3-D-6.2 Control Operation Persistence|完成|Operation model、幂等、claim 与 persistence contract|
|P3-D-6.3 Atomic Apply Port|完成|ApplyPlan、RejectPlan、Receipt 和 Actor-only Port contract|
|P3-D-6.4 CREATE Bootstrap|完成|Creation guard、provisional actor 和 atomic create contract|
|P3-D-6.5 Freeze Supplement|文档完成|冻结 Actor Apply Adapter 缺失契约；尚待人工接受|

这些 P3-D 成果仍在工作区，尚未 commit、尚未 staged，不得当作已提交基线处理。

## 5. 已解决的问题与现有链路

当前已建立的控制链为：

```text
DM
  -> Command Contract
  -> Session Resolver / CREATE Bootstrap Context
  -> Authorization Decision
  -> Confirmation Validation
  -> DM_COMMAND GameEvent
  -> Control Operation
  -> Atomic Apply Contract
  -> CREATE Bootstrap Contract
  -> Actor Apply Adapter (待实现)
```

已解决：

- Command 不再以自由字典传播，scope、payload 和 requester evidence 有类型约束；
- Resolver 不直接修改 Session、不直接写 SQLite；
- DM 权限只做 Session-scoped Decision，不成为系统权限；
- Confirmation 绑定 command、scope、requester、payload fingerprint 和 State version；
- `DM_COMMAND` 复用唯一 `GameEvent`，不创建第二套 Event；
- Control Operation 使用稳定 `command_id` 幂等、CAS claim 和 `UNKNOWN` 语义；
- Atomic Apply contract 能表达 State、Event processing、Result Event、Operation terminal 和版本的一次提交；
- CREATE 使用 reservation/provisional actor/atomic hook，不允许直接 INSERT 或绕过 Event/Actor；
- Result Event 必须在 ApplyPlan 中产生并在事务内提交，Actor 不能 commit 后伪造结果。

## 6. 当前阶段与阻塞原因

当前目标是 P3-D-6.5 Actor Apply Adapter Implementation，但状态为：

```text
P3-D-6.5 Implementation = BLOCKED
Production integration = DISABLED
P3-D-6.6 = NOT STARTED
```

直接 Freeze 文档为 [P3-D-6.5 Actor Apply Adapter Freeze Supplement](game-mode-p3d65-actor-apply-freeze.md)，状态为 `Draft Freeze Proposal`，等待人工确认。

此前 Freeze Gate 发现：

1. 当前 Actor 是同步 mailbox、同步 drain 和 `RLock`，无法安全直接等待异步 Atomic Apply Port；
2. Actor 当前收到裸 `GameEvent`，缺少 Event sequence、Operation、State version 和 requester binding evidence；
3. Candidate State Change 依赖后续 Lifecycle/Phase/Setup/Participant reducer；
4. Result Event 必须属于 Atomic Apply 事务，Receipt 返回后不得重新生成结果事件。

P3-D-6.5 Supplement 已给出解决契约：

- `ControlEventDeliveryEnvelope`；
- per-session async processing gate；
- Actor-owned Apply Coordinator；
- Operation Claim Bridge；
- pure `ControlApplyPlanBuilder` boundary；
- Receipt validation、commit-then-swap 和 committed Result notification。

新对话不能把“Freeze 文档已写入”误解为“已经批准实现”。必须先获得人工明确确认。

## 7. 下一阶段路线

人工接受 P3-D-6.5 Draft Freeze Proposal 后，严格按以下顺序推进：

```text
P3-D-6.5.1 Control Event Delivery Envelope
  -> P3-D-6.5.2 Per-session Async Gate
  -> P3-D-6.5.3 Operation Claim Bridge
  -> P3-D-6.5.4 ApplyPlan Builder Contract
  -> P3-D-6.5.5 Actor-owned Apply Coordinator
  -> P3-D-6.5.6 Receipt and Result Notification
  -> P3-D-6.5.7 Contract Tests
  -> P3-D-6.6 Lifecycle / Phase Apply
  -> P3-D-6.7 Setup / Participant Apply
  -> P3-D-6.8 Recovery / Idempotency
  -> P3-D-6.9 Contract and Isolation Tests
```

下一次获准实现时，只进入 P3-D-6.5.1：

- 建立不可变 `ControlEventDeliveryEnvelope` contract；
- 它只包装已持久化 `DM_COMMAND` 和 delivery evidence，不是第二套 Event；
- 不修改 Actor、mailbox、Atomic Apply、Persistence 或业务 State；
- 不提前实现 async gate、claim bridge、Builder、Coordinator 或 reducer；
- 增加最小 contract tests；
- 完成后运行阶段测试和 Game Runtime 回归测试；
- 不自动 commit。

## 8. 当前 Git 工作区

已核验：

- 分支：`feature/game-mode-runtime-v01`；
- HEAD：`057c610160babbbc4cd3750410484e6e710af798`；
- 暂存区为空；
- 分支跟踪 `origin/feature/game-mode-runtime-v01`；
- P3-D 累计代码与测试未提交；
- `docs/game-mode/` 包含未跟踪设计文档；
- 本 handoff 也是未跟踪 Markdown 文档；
- 本轮未运行代码测试。

当前 tracked 修改：

```text
game_runtime/event/__init__.py
game_runtime/event/model.py
game_runtime/interfaces/__init__.py
game_runtime/interfaces/ports.py
game_runtime/persistence/repositories/session.py
```

当前关键 untracked 实现与测试：

```text
game_runtime/event/control_payloads.py
game_runtime/session_control/
tests/game_runtime/test_control_apply_port.py
tests/game_runtime/test_game_event_control_schema.py
tests/game_runtime/test_session_control_authorization.py
tests/game_runtime/test_session_control_bootstrap.py
tests/game_runtime/test_session_control_commands.py
tests/game_runtime/test_session_control_confirmation.py
tests/game_runtime/test_session_control_event_integration.py
tests/game_runtime/test_session_control_operation.py
tests/game_runtime/test_session_control_resolver.py
```

当前关键新增 Freeze 文档：

```text
docs/game-mode/game-mode-p3d6-actor-apply-freeze.md
docs/game-mode/game-mode-p3d65-actor-apply-freeze.md
docs/game-mode/game-mode-runtime-handoff.md
```

新对话必须重新执行 `git status --short --branch`、`git diff --cached --name-only` 和相关测试，不能依赖 handoff 中的状态替代现场核验。

## 9. 禁止事项

在人工确认 P3-D-6.5 Freeze 前，禁止：

- 写实现代码或继续 P3-D-6.5.1；
- 修改 Actor、mailbox、Event Pipeline、Persistence 或 Router；
- 修改、清理或提交现有 P3-D 累计变更；
- reset、rebase、merge、clean、checkout 覆盖或创建分支；
- 接入生产 Message Ingress 或启用生产消息接管；
- 接入 QQ发送、私聊、LLM、Hunter Agent、Reasoning、Knowledge、Timeline 或 Memory；
- 创建第二套 Event、State Store、Actor、Runtime Loop 或 State Writer；
- 绕过 Atomic Apply Port、直接 SQLite、commit 后补写 Result Event；
- 对 `UNKNOWN` 或不确定 commit 自动重试；
- 修改 Normal Mode；
- commit 或 push。

任何实现阶段仍须遵守：一个子阶段只承担一个职责，先核对 Freeze 和 allowlist，完成测试后报告，等待人工确认再继续。

## 10. 新对话启动 Prompt

以下 Prompt 可直接粘贴到新的 ChatGPT/Codex 对话：

```text
你现在作为 qq-reminder-bot 项目的 Principal Architect + Senior Engineer，继续 Game Mode Runtime 的严格分阶段开发。

项目目录：
D:\猎bot\qq-reminder-bot

当前分支：
feature/game-mode-runtime-v01

当前 HEAD：
057c610160babbbc4cd3750410484e6e710af798

首先只做现场核验，不要立即写代码：

1. 阅读：
   docs/game-mode/game-mode-runtime-handoff.md
   docs/game-mode/game-mode-architecture-freeze.md
   docs/game-mode/game-mode-implementation-freeze.md
   docs/game-mode/game-mode-p2-detailed-design.md
   docs/game-mode/game-mode-p3c-persistence-design.md
   docs/game-mode/game-mode-p3d-session-control-design.md
   docs/game-mode/game-mode-p3d6-actor-apply-freeze.md
   docs/game-mode/game-mode-p3d65-actor-apply-freeze.md

2. 执行只读 Git 检查：
   git status --short --branch
   git diff --cached --name-only
   git rev-parse HEAD
   git branch --show-current

3. 保留当前工作区：
   - P3-D 累计代码、测试和文档尚未 commit；
   - 暂存区应为空；
   - docs/game-mode/ 包含未跟踪设计文档；
   - 不得 reset、clean、checkout、覆盖、移动或提交现有变更。

项目定位：

Agent Runtime
├── Normal Mode
└── Game Runtime

Game Runtime 是有状态 Runtime Mode，不是 Plugin、Tool 或 Memory 子模块。DM 只是当前 Game Session Controller，不是 System Admin。同一 Session 只有一个 Actor/State Writer；GameEvent 是唯一 Runtime Event 协议；同局按持久 sequence 串行；State、input processing、Result Event、Operation terminal 和版本必须原子提交；UNKNOWN 不自动重试；Game Mode 失败不回落 Normal Mode。

已完成：

- P0 / P0.1 / P0.2 Architecture and Freeze
- P1 Repository Mapping
- P2 Detailed Design
- P2.1 Implementation Freeze
- P3-A Runtime Skeleton（commit cbf820085）
- P3-B Router Integration（commit 170964467）
- P3-C Persistence / Recovery（commit 057c610160babbbc4cd3750410484e6e710af798）
- P3-D Design Freeze
- P3-D-1 Command Contract
- P3-D-2 Session Resolver + CREATE bootstrap
- P3-D-3 Authorization Policy
- P3-D-4 Confirmation Framework
- P3-D-5 Event Integration
- P3-D-6 Freeze Supplement
- P3-D-6.1 Event Schema Extension
- P3-D-6.2 Control Operation Persistence
- P3-D-6.3 Atomic Apply Port
- P3-D-6.4 CREATE Bootstrap
- P3-D-6.5 Actor Apply Adapter Freeze Supplement 文档

当前控制链：

DM
-> Command Contract
-> Resolver
-> Authorization
-> Confirmation
-> DM_COMMAND GameEvent
-> Control Operation
-> Atomic Apply Contract
-> CREATE Bootstrap
-> Actor Apply Adapter（待实现）

当前状态：

P3-D-6.5 Actor Apply Adapter Implementation = BLOCKED
Production integration = DISABLED
P3-D-6.6 = NOT STARTED

原因：

docs/game-mode/game-mode-p3d65-actor-apply-freeze.md 已写入，但状态仍为 Draft Freeze Proposal，必须先由人工明确确认。该 ADR 冻结了 ControlEventDeliveryEnvelope、per-session async gate、Actor-owned Coordinator、Operation Claim Bridge、pure ControlApplyPlanBuilder、Receipt validation、commit-then-swap 和 committed Result notification。

本次任务目标：

- 先核验并报告仓库、Git 和 Freeze 状态；
- 不要把文档已写入视为人工已接受；
- 如果用户尚未明确确认 Freeze，只报告 BLOCKED 并等待；
- 如果用户明确接受 Freeze 并授权实现，只进入 P3-D-6.5.1 Control Event Delivery Envelope；
- P3-D-6.5.1 只建立不可变 delivery contract 和最小 contract tests，不修改 Actor、mailbox、Persistence、Atomic Apply 或业务 State，不提前实现后续子阶段。

开发纪律：

- Freeze 优先，不重新设计架构；
- 不修改 Normal Mode；
- 不接入 QQ、LLM、Hunter、Reasoning、Knowledge、Timeline 或 Memory；
- 不创建第二套 Event、Actor、State Store、Runtime Loop 或 State Writer；
- 不直接 SQLite，不绕过 Atomic Apply Port；
- 不自动重试 UNKNOWN；
- 不 commit、不 push；
- 一个阶段只完成一个职责；
- 如果实现需要越过 allowlist 或改变冻结边界，立即停止并报告 Freeze Gate。

输出格式：

1. 当前分支和 HEAD
2. Git 工作区与 staged 状态
3. 已读取的冻结文档
4. Freeze Gate 结论
5. 是否允许进入 P3-D-6.5.1

在没有新的人工实现授权前，不要修改任何文件。
```

## 11. 当前交接结论

Game Mode Runtime 已完成至 P3-D-6.5 Freeze Supplement 文档阶段。Actor Apply Adapter 尚未实现；下一动作是人工确认 Draft Freeze Proposal，而不是继续写代码。

```text
P3-D-6.5 Freeze Supplement = DOCUMENTED / AWAITING ACCEPTANCE
P3-D-6.5 Implementation = BLOCKED
Development continuation = NOT STARTED
```
