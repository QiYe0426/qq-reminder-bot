# Game Mode P3-D Session Control Plane Design Freeze

- 文档类型：RFC / Architecture Design Freeze
- 状态：Accepted / Frozen，待人工确认后进入 Implementation
- 当前分支：`feature/game-mode-runtime-v01`
- 代码基线：`057c610160babbbc4cd3750410484e6e710af798`
- 上位约束：[Architecture Freeze P0.2](game-mode-architecture-freeze.md)、[P2 Detailed Design](game-mode-p2-detailed-design.md)、[Implementation Constraint Freeze P2.1](game-mode-implementation-freeze.md)
- 前置阶段：[P3-C Persistence Design](game-mode-p3c-persistence-design.md)
- 路线导航：[Game Mode Development Roadmap](game-mode-development-roadmap.md)
- 范围：Session Command、DM Authorization、Confirmation、Control Operation、GameEvent、Actor Apply
- 非目标：代码、生产入口、LLM、Hunter Agent、Knowledge、Reasoning、Timeline、QQ发送、私聊和自动行动

## 1. P3-D Design Freeze 状态说明

本文是后续 P3-D Session Control Plane Implementation 的唯一阶段设计基线。实现必须同时遵守 P0.2 和 P2.1 的永久约束；本文只细化 Session Control Plane，不替代或放宽 Game Runtime 的 Mode、Memory、Knowledge、Action 和通信边界。

本文冻结：

1. Session Control Plane 是 `game_runtime/` 内的领域控制面，不是 Plugin、Agent Tool 或数据库管理接口。
2. DM 是单一 `game_id` 的 Session Controller，不是 System Admin。
3. 所有被接受的 Session Command 必须先建立持久 Control Operation，再转换为 GameEvent，由 Session Actor 原子 Apply。
4. Parser、Resolver、Authorization、Confirmation 和 Adapter 均不得直接修改 Session 或数据库。
5. `CREATE_SESSION` 使用受限 bootstrap controller、provisional Actor 和原子 create transaction，不绕过 Event/Actor。
6. 控制操作复用 P3-C 的 Event Store、Snapshot、`state_version`、Action/Operation 状态语义、ownership 和 recovery。
7. P3-D 不启用生产 QQ Message Ingress，不发送任何消息，不实现 Hunter 或游戏认知能力。

人工确认本文前不得开始 P3-D Implementation。任何需要改变本文 Command、权限、Confirmation 或状态边界的实现，必须先新增 ADR。

## 2. 与 P2.1 Decision 23 的阶段编号关系

P2.1 Decision 23 原始阶段命名为：

```text
P3-A Runtime Skeleton
  -> P3-B Mode Router Integration
  -> P3-C Persistence
  -> P3-D Hunter Agent Loop
  -> P3-E Action Execution
```

当前 Development Roadmap 在 P3-C 后插入 Session Control Plane，并将后续程序路线命名为 P4 Knowledge System、P5 Hunter Agent 和 P6 Advanced Capability。

本文对阶段编号作以下显式冻结：

```text
P3-C Persistence / Recovery
  -> P3-D Session Control Plane
  -> P4 Knowledge System
  -> P5-A Hunter Agent Loop
  -> P5-B Action Execution
```

该调整只解决阶段标签与新增前置能力，不改变 P2.1 的技术顺序和安全约束：

- Hunter Agent Loop 仍必须在 Persistence、Session Control 和 Knowledge/Visibility 之后；
- Action Execution 仍必须在 Hunter Loop 契约冻结后单独实现；
- P5-A 与 P5-B 不得合并交付或倒序启用；
- P3-D 不得包含原 Decision 23 的 Hunter Loop 或 Action Execution；
- P2.1 Decisions 15–22、24 及全部安全不变量继续有效。

对于 P3-D 的阶段名称和范围，本文作为 Decision 23 的后续阶段规划修订；对于其余架构决策，P2.1 保持上位权威。

## 3. Session Control Plane 总体目标

Session Control Plane 为真人 DM 提供确定性、可授权、可确认、可审计和可恢复的单局控制能力：

```text
Structured DM / Setup Input
        |
Session Command
        |
Authorization + Confirmation
        |
Persistent Control Operation
        |
GameEvent
        |
GameSession Actor
        |
Atomic State / Ownership Change
```

负责：

- 解析受控 Session Command；
- 解析目标 group/game/session 和 requester principal；
- 校验 prospective DM 或当前 Session DM；
- 执行 Global、Game Mode、Session Role、Lifecycle/Phase 四层授权；
- 建立 Command idempotency 与持久 Control Operation；
- 发起并验证必要 Confirmation；
- 生成 `DM_COMMAND` GameEvent；
- 将 Event 投递给唯一 Session Actor；
- 通过 Actor 原子更新 Session、Participant、Character binding 和 ownership；
- 记录 Shared Audit Engine `domain=GAME` 安全元数据；
- 在冲突、拒绝、存储或恢复故障时 fail closed。

不负责：

- 直接执行 SQL 或持有 SQLite connection；
- 直接修改 Session Model、Snapshot 或 ownership；
- 调用 LLM、构建 Prompt 或运行 Hunter Agent；
- 解析剧本、线索、Knowledge、Reasoning 或 Timeline；
- 读取或写入 Companion/User/Semantic/Graph Memory；
- 发送 QQ 群消息、私聊、语音或图片；
- 自动推进 Phase、自动结束游戏或代替真人 DM；
- 注册 Agent Tool、调用 Tool Gateway 或继承 Tool permission。

## 4. DM 权限边界

冻结定义：

```text
DM = Game Session Controller(game_id)
DM != System Admin
```

DM 可以在当前 Session 规则允许时：

- 创建并绑定自己控制的 Session；
- 设置本局公开剧本标识；
- 绑定本局角色；
- 开始、暂停、恢复和结束游戏；
- 推进合法 Phase；
- 替换 Participant；
- 在 CREATED 或 PAUSED 状态进入受信 Setup/Repair 流程。

DM 不可以：

- 修改系统配置、Runtime 配置或 Feature Gate；
- 操作 Normal Mode；
- 调用普通 Agent Tool 或获得 Tool Capability；
- 获得 Global Permission 或 System Admin 身份；
- 查看、修改或结束其他 Session；
- 绕过 Lifecycle/Phase 状态机；
- 通过群消息写入 Hidden Truth 或 Character Private Knowledge；
- 把 Game Character 写入长期 Agent Identity；
- 绕过 Authorization、Confirmation、Audit、Idempotency 或 Actor。

System Admin 也不会自动成为某局 DM。Global Control Plane 与 Session Control Plane 使用不同 principal、permission 和 Audit domain，不互相继承。

## 5. Session Command Model

### 5.1 Command Envelope

所有 Session Command 使用不可混淆的结构化 Envelope：

|字段|约束|
|---|---|
|`command_id`|全局稳定；重投保持不变；用于幂等|
|`command_type`|固定枚举，不接受自由字符串扩权|
|`requester_principal`|安全身份引用，不以昵称或自称授权|
|`group_id`|目标群；必填|
|`game_id/session_id`|除 CREATE_SESSION 外必填；由 Resolver 验证|
|`requester_binding_version`|除 bootstrap 外必填；拒绝替换前旧身份|
|`observed_state_version`|除 CREATE_SESSION 外必填；Actor Apply 前重验|
|`payload`|按 command type 独立 schema 校验的最小参数|
|`correlation_id`|贯穿 Command、Operation、Event 和 Audit|
|`requested_at`|timezone-aware Runtime 时间|
|`confirmation_token`|需要确认时携带，否则为空|

Command payload 禁止包含完整剧本、Hidden Truth、Character Private Knowledge、玩家聊天原文、Prompt、Reasoning 或 QQ 明文身份。Script 和 Character 只使用受控 ID、公开名称和 Setup manifest 引用。

### 5.2 Command 冻结矩阵

|Command|作用|关键输入|权限|Confirmation|输入/结果 GameEvent|状态影响|
|---|---|---|---|---|---|---|
|`CREATE_SESSION`|创建本局空白 Session|group、prospective DM、idempotency|Game Mode group gate + `BOOTSTRAP_CONTROLLER`|默认不需要|`DM_COMMAND(CREATE_SESSION)` → `SESSION_CREATED`|创建 `CREATED/LOBBY`；不建立 active ownership|
|`START_GAME`|启动完成 Setup 的 Session|game/session、expected version|当前 ACTIVE DM|必须|`DM_COMMAND(START_GAME)` → `SESSION_STARTED` + `PHASE_CHANGED`|`CREATED/LOBBY -> RUNNING/INTRODUCTION`；原子建立 ownership|
|`PAUSE_GAME`|安全暂停运行|game/session、reason code、expected version|当前 ACTIVE DM|默认不需要|`DM_COMMAND(PAUSE_GAME)` → `SESSION_PAUSED`|`RUNNING -> PAUSED`；保留 Phase 和 ownership|
|`RESUME_GAME`|恢复暂停 Session|game/session、expected version|当前 ACTIVE DM|必须|`DM_COMMAND(RESUME_GAME)` → `SESSION_RESUMED`|`PAUSED -> RUNNING`；Phase 与 ownership 保留|
|`END_GAME`|不可逆结束本局|game/session、公开结果引用、expected version|当前 ACTIVE DM；独立 emergency policy 除外|必须|`DM_COMMAND(END_GAME)` → `SESSION_ENDED`|进入 `ENDED/ENDING`；停止新 Operation、解除 ownership、建立 retention metadata|
|`CHANGE_PHASE`|执行合法 Phase 转换|target phase、expected version|当前 ACTIVE DM|进入 VOTING/ENDING 或触发可见性变化时必须|`DM_COMMAND(CHANGE_PHASE)` → `PHASE_CHANGED`|只允许 RUNNING；按冻结 Phase 图转换|
|`SET_SCRIPT`|绑定本局公开剧本标识|script ID、公开名称、manifest ref|当前 DM 或 bootstrap controller|首次设置可免；覆盖必须|`DM_COMMAND(SET_SCRIPT)` → `SCRIPT_SET`|只允许 CREATED/LOBBY；PAUSED 修改必须走 Repair|
|`ASSIGN_CHARACTER`|绑定 Participant/Hunter 本局角色|participant ID、character ID、binding version|当前 ACTIVE DM|首次绑定可免；覆盖或 PAUSED Repair 必须|`DM_COMMAND(ASSIGN_CHARACTER)` → `CHARACTER_ASSIGNED`|更新单局 Character binding；递增 binding/version|
|`REPLACE_PLAYER`|替换本局 Participant|old/new participant refs、expected binding|当前 ACTIVE DM|必须|`DM_COMMAND(REPLACE_PLAYER)` → `PLAYER_REPLACED`|只允许 CREATED 或 PAUSED；旧 binding 失效|

`DM_COMMAND` 是被治理后送入 Actor 的输入 Event。右侧结果类型是 P3-D 受控内部 Event 扩展；每个类型必须固定 schema、visibility、producer、consumer、persistence 和 failure policy，不得以任意字符串代替。

### 5.3 Command 状态约束

- `CREATE_SESSION`：目标群不存在未结束 Session 或未决 creation guard。
- `START_GAME`：只允许 CREATED/LOBBY；DM、Participant、Script、Character 和 Setup manifest 完整。
- `PAUSE_GAME`：只允许 RUNNING；重复 PAUSE 返回既有幂等结果。
- `RESUME_GAME`：只允许 PAUSED；Recovery/Repair 状态必须通过验证。
- `END_GAME`：允许 CREATED/RUNNING/PAUSED；ENDED 不可再次结束或重启。
- `CHANGE_PHASE`：只允许 RUNNING；不得跳过冻结状态图。
- `SET_SCRIPT`：群内控制只保存公开 ID/名称，不保存剧本内容。
- `ASSIGN_CHARACTER`：角色绑定不进入长期 Agent Identity，不携带私密角色正文。
- `REPLACE_PLAYER`：RUNNING 必须先 PAUSE；旧 Command、Confirmation 和未执行 Operation 必须重新授权或取消。

## 6. Command 生命周期

冻结主流程：

```text
DM Input
  -> Command Parser
  -> Session Resolve
  -> Authorization
  -> Persistent Control Operation(CREATED)
  -> Confirmation(if required)
  -> GameEvent(DM_COMMAND)
  -> Event Store Ingest
  -> Session Actor
  -> Re-authorization + Version Check
  -> Operation Claim(EXECUTING)
  -> Atomic State Change
  -> Result Event + Operation Terminal State
```

详细规则：

1. Input 是平台无关的结构化请求；P3-D 不注册 QQ matcher。
2. Parser 只进行 command enum、字段和 payload schema 校验，不调用 LLM。
3. Resolver 使用 `group_id/game_id/session_id` 查询权威 Session/ownership，不读取 Knowledge 或 Memory。
4. Authorization 执行全部权限交集；拒绝后不产生可 Apply 的 `DM_COMMAND`。
5. 通过预检后，以 `command_id` 和 canonical payload fingerprint 创建持久 Control Operation。
6. 需要 Confirmation 时，首次请求返回 challenge；Operation 保持 CREATED，不生成状态 Event。
7. Confirmation 通过后构造 `DM_COMMAND`，Event Store 分配 Session sequence；持久化失败不投递 Actor。
8. Actor 是唯一 State Writer。Actor 必须重新校验 principal、binding、Lifecycle、Phase、expected version 和 Confirmation。
9. Actor 原子 claim Operation 为 EXECUTING，再计算 State Delta。
10. 在同一事务边界提交 Snapshot/Participant/ownership、Event APPLIED、cursor、`state_version`、结果 Event 和 Operation terminal state。
11. 提交后才能发布 follow-up；P3-D 没有 QQ发送 follow-up。
12. 无法证明事务是否提交时进入 UNKNOWN 或基于 Snapshot/Event/Operation evidence reconciliation，禁止自动重放控制操作。

如果未来 Command 来源是游戏群消息，原始 `MESSAGE_RECEIVED` 可按既有 pipeline 存在；非法 DM 命令只产生最小拒绝 Audit，不产生状态变化或受权 `DM_COMMAND`。

## 7. Authorization Design Freeze

有效权限固定为：

```text
Effective Permission
    =
Global Runtime Permission
    ∩ Game Mode Permission
    ∩ Session Role / Bootstrap Role
    ∩ Action Constraint
```

### 7.1 权限层

|层|判定内容|明确不表示|
|---|---|---|
|Global Runtime Permission|服务未被全局关闭，Control Plane 基础能力可用|requester 是 System Admin|
|Game Mode Permission|目标群允许使用 Game Mode、Command capability 可用|允许调用 Normal Tool|
|Session Role|requester 是当前 game 中 ACTIVE DM，binding version 匹配|对其他 game 有权|
|Bootstrap Role|一次性、目标群限定的 prospective DM 创建能力|Global Admin 或现有 Session DM|
|Action Constraint|Lifecycle、Phase、target、expected version、payload 和 policy 均允许|DM 可绕过状态机|

### 7.2 DM 验证输入

除 CREATE_SESSION 外必须同时验证：

```text
game_id
+ session_id
+ group_id
+ participant_id
+ participant_type = DM
+ membership_state = ACTIVE
+ requester_binding_version
+ requested_command
+ observed_state_version
```

QQ 昵称、消息内容中的自称、System Admin 身份或另一局 DM 身份不能作为授权证据。

### 7.3 必须拒绝

- 非 DM 或已 REPLACED/LEFT/REVOKED Participant；
- game/session/group scope 不一致；
- 访问其他 Session；
- binding version 或 state version 过期；
- Lifecycle/Phase 不允许或非法跳转；
- START 前 Setup manifest 不完整；
- RUNNING 时直接替换玩家或覆盖角色；
- 群消息携带 Hidden Truth/Private Knowledge；
- 修改长期 Identity、系统配置、Normal Mode 或 Tool permission；
- ENDED Session 的恢复、修改或重新开始；
- Registry、Authorization、required Audit 或 Persistence 不可用。

拒绝使用 Shared Audit Engine `domain=GAME` 记录 command type、scope fingerprint、policy result、version 和错误类别；不得记录 QQ 明文、payload、剧本、角色秘密或 Confirmation 内容。

## 8. Confirmation Policy

### 8.1 必须确认

- `START_GAME`；
- `RESUME_GAME`；
- `END_GAME`；
- `REPLACE_PLAYER`；
- 覆盖已有 `SET_SCRIPT`；
- 覆盖已有角色或 PAUSED Repair 的 `ASSIGN_CHARACTER`；
- `CHANGE_PHASE` 进入 VOTING/ENDING；
- 任何触发信息可见性变化、角色权限撤销或不可逆状态变化的操作。

### 8.2 默认无需确认

- `CREATE_SESSION`；
- `PAUSE_GAME`；
- 首次 `SET_SCRIPT`；
- CREATED/LOBBY 中首次角色绑定；
- 不触发信息揭示的合法普通 Phase 相邻转换；
- 重复 command 的幂等结果查询。

### 8.3 Confirmation Binding

Confirmation 必须单次使用，并绑定：

```text
command_id
+ command_type
+ game_id/session_id
+ expected_state_version
+ requester participant_id
+ requester binding_version
+ canonical payload fingerprint
+ ownership_generation
+ expires_at
```

以下任一变化使 Confirmation 失效：Session version、requester binding、payload、target、Phase、command type 或 ownership generation 改变；token 过期、已使用；Session 被暂停、结束或恢复。

Confirmation 只证明 requester 确认了特定操作。Actor Apply 前仍必须重新执行 Authorization，不能把 Confirmation 当作权限或状态锁。

## 9. CREATE_SESSION Bootstrap Design

CREATE_SESSION 执行前尚无 Session、Session DM 或可持久化 GameEvent 外键目标。P3-D 冻结专用 bootstrap，不新增 Lifecycle 状态，也不要求 System Admin：

```text
Authenticated Prospective DM
  -> Global + Game Mode Group Gate
  -> Acquire persistent creation guard(command_id + group_id)
  -> Allocate game_id/session_id
  -> Persist Bootstrap Control Operation(CREATED)
  -> Build in-memory CREATED/LOBBY aggregate
  -> Create provisional Session Actor
  -> Deliver DM_COMMAND(CREATE_SESSION)
  -> Actor calls create_session_with_event transaction port
  -> Commit Session + Participant/DM + Event + Operation
  -> Publish CREATED result
```

冻结规则：

- `BOOTSTRAP_CONTROLLER` 是一次性、target group-scoped capability；创建完成即失效。
- prospective DM 通过受信 Control Plane认证，不通过昵称、自由文本或 System Admin 自动推导。
- `game_id/session_id` 在 Event 构造前分配且不可由 caller 指定覆盖。
- 同一群最多存在一个未结束 Session 或有效 creation guard。
- provisional Actor 在事务成功前不进入 active Actor Registry，不拥有群消息路由权。
- CREATED/LOBBY 不产生 active ownership；START_GAME 成功才原子建立 ownership。
- `create_session_with_event` 是 P3-D 新增 Persistence Port；Control Plane 不直接操作 Repository/SQL。
- 物理事务可以先插入 FK 所需 Session row，再插入 Event，但领域因果保持“Command Event 进入 Actor后才形成权威 State”；二者必须同一事务提交，不能暴露部分状态。
- bootstrap 失败回滚 Session、Event、Participant 和 Operation result；稳定 `command_id` 返回既有结果，不重复创建。

Bootstrap Control Operation 需要允许在 Session row 尚未存在时保存 prospective `game_id/session_id`。P3-D 可以在 Game Runtime 专用 SQLite schema 中增加受限 control operation/creation guard 表，但不得复用 Agent Tool 表、普通 Memory 或全局权限表。

## 10. Persistence Integration

### 10.1 复用边界

|P3-C 能力|P3-D 使用方式|
|---|---|
|Game State Snapshot|Control Command 的权威前置状态和 Apply 结果|
|Event Store|保存 `DM_COMMAND`、结果 Event、sequence 和 processing status|
|`state_version`|Command observed version、CAS 和 stale rejection|
|Event cursor|保证同局 Command 与消息 Event 的处理顺序|
|Action/Operation 状态语义|CREATED/EXECUTING/SUCCESS/FAILED/UNKNOWN/CANCELLED|
|Persistent Ownership|START 建立、PAUSE 保留、RESUME 维持、END 解除|
|Actor Ownership|防止同一 game 的并行 State Writer|
|Recovery Manager|恢复 Session、ownership、Actor 和未决 Operation|
|Schema version|新增 Operation/bootstrap 持久结构的最小 migration|

### 10.2 SessionControlOperation

所有 Command 使用独立 Game Runtime Operation Record，不注册为 Agent Tool。至少包含：

|字段|语义|
|---|---|
|`operation_id/command_id`|稳定身份与幂等键|
|`command_type`|受控枚举|
|`game_id/session_id/group_id`|单局 scope；bootstrap 使用 prospective IDs|
|`requester_principal_ref/binding_version`|执行主体与身份代次|
|`status`|CREATED/EXECUTING/SUCCESS/FAILED/UNKNOWN/CANCELLED|
|`observed_state_version`|Actor Apply 前重验|
|`payload_fingerprint`|幂等和 Confirmation binding；不保存敏感正文|
|`confirmation_ref`|可空；只保存安全引用|
|`source_event_id/correlation_id`|因果链|
|`result_event_id/result_state_version`|成功或可验证失败证据|
|`created_at/updated_at/expires_at`|生命周期与 recovery|

### 10.3 原子状态规则

- START：Lifecycle、Phase、ownership、Event、Operation 和 `state_version` 同一 Apply transaction。
- PAUSE：Lifecycle、Instance suspension intent、Event 和 Operation 原子提交；ownership 不删除。
- RESUME：Lifecycle、recovery validation、Event 和 Operation 原子提交；不重新分配 ownership generation，除非 Repair policy 显式重建。
- END：ENDING/ENDED、停止新 Operation、ownership release、retention metadata、Event 和 Operation 原子提交。
- CHANGE_PHASE：Phase、可见性 change intent、Event 和 Operation 原子提交；P3-D 不执行 Knowledge reveal。
- Participant/Character 变更：binding version、旧权限失效、Event 和 Operation 原子提交。

Control Plane 不使用“先 update Session，再补 Event/Audit”的顺序。任何 write failure 均不得调用 Normal Mode 或继续下一副作用。

### 10.4 Recovery

- CREATED Operation：重新验证后等待 Confirmation/Actor，不自动执行。
- EXECUTING Operation：依据 Snapshot version、result Event 和 Operation evidence reconciliation；无法证明则 UNKNOWN。
- SUCCESS/FAILED/CANCELLED：保持终态，不重复 Apply。
- UNKNOWN：禁止自动重试或生成等价替代 Command；只允许证据化 reconciliation。
- unclean restart：原 RUNNING Session 按 P3-C 进入 PAUSED；未决 RESUME/START 不自动继续。
- recovery failure：保留或建立 fail-closed ownership，禁止新 Command Apply，允许受信 Repair 或安全 END。

## 11. 模块依赖边界

### 11.1 允许依赖

```text
session_control
  -> session        # aggregate、Lifecycle、Phase
  -> participant    # DM/Player binding
  -> identity       # Session-scoped identity
  -> actor          # mailbox、single writer
  -> event          # DM_COMMAND/result events
  -> persistence    # Snapshot/Event/Operation transaction ports
  -> routing ownership view
  -> recovery/actor ownership
  -> shared audit adapter(domain=GAME)
  -> shared authorization/confirmation/idempotency ports
```

依赖必须指向领域或中立基础设施 Port。Audit、Authorization、Confirmation 和 Idempotency 的适配器不得反向依赖 Game Session internals。

### 11.2 禁止依赖

```text
session_control -> plugins.ai_chat / Normal Mode
session_control -> Agent Tool Registry / execute_tool / Tool Gateway
session_control -> Companion/User/Semantic/Graph Memory
session_control -> Message Archive / Daily Report
session_control -> LLM Provider / Prompt / Context Builder
session_control -> Hunter Reasoning / Timeline / Decision
session_control -> OneBot/NapCat sender
```

不得为了复用治理能力，将 Session Command 注册为 Agent Tool。可以复用中立 protocol 和安全原语，不能继承 Tool identity、inventory、metadata provenance 或 authorization。

## 12. Implementation 拆分计划

```text
P3-D-1 Command Contract
  -> Command enum、Envelope、payload schema、strict parser

P3-D-2 Session Resolver
  -> scope resolve、bootstrap controller、creation guard、provisional Actor

P3-D-3 Authorization
  -> Global/Game/Role/Constraint intersection、DM binding、deny Audit

P3-D-4 Confirmation
  -> policy、binding、expiry、single-use、Operation persistence

P3-D-5 Event Integration
  -> DM_COMMAND/result schemas、Event ingest、Operation correlation

P3-D-6 Actor Apply
  -> Lifecycle/Phase/Participant/ownership atomic transactions、recovery

P3-D-7 Tests
  -> Command、permission、confirmation、concurrency、recovery、isolation
```

每个子阶段只能在前一子阶段 contract 测试通过后进入。P3-D Implementation 总体仍需一个 checkpoint，不得在 P3-D-1 时启用生产入口或部分控制能力。

### 12.1 P3-D-7 必测范围

- 9 个 Command 的合法和非法状态组合；
- non-DM、cross-session、revoked/replaced binding 拒绝；
- CREATE 并发、重复 command 和 bootstrap rollback；
- START/PAUSE/RESUME/END ownership 原子性；
- Phase 非法跳转和 stale `state_version`；
- Confirmation 篡改、重放、过期和版本失效；
- Control Operation 的 CREATED/EXECUTING/terminal/UNKNOWN recovery；
- Actor ownership 冲突和同局串行；
- Audit failure、Persistence failure 和 Recovery failure 均 fail closed；
- Script/Character payload 不接受 Hidden Truth/Private Knowledge；
- 无 LLM、QQ、Normal Mode、Tool Registry 或 Memory 依赖；
- 全量 Normal Mode 回归不变；
- 生产 Message Ingress 未启用。

## 13. Failure and Security Rules

|故障|处理|
|---|---|
|Parser/schema invalid|拒绝，不解析自由文本补救，不创建 Operation|
|Session resolve failure|fail closed，不视为 NO_SESSION 后交 Normal Mode|
|Authorization deny|无 State Event；最小 GAME Audit|
|Confirmation invalid|拒绝并保持 Operation CREATED 或安全取消|
|Event ingest failure|不投递 Actor，不修改 State|
|stale state/binding|Actor拒绝，Operation FAILED/CANCELLED，不自动重试|
|Apply transaction failure|Event 保持 RECEIVED/DEFERRED；Session PAUSED 或 recovery|
|ownership conflict|拒绝 START/CREATE，不抢占另一个 Session|
|result write uncertain|Operation UNKNOWN，禁止自动重放|
|Audit unavailable|高影响 Command fail closed|
|unclean restart|RUNNING 进入 PAUSED，START/RESUME 不自动继续|

安全日志和 Audit 只允许 command type、game/session 安全引用、policy result、version before/after、correlation、failure class 和 timing。禁止记录 QQ 明文、剧本、角色秘密、payload、Confirmation token、Prompt 或 Reasoning。

## 14. P3-D Implementation Non-Goals

P3-D 明确不实现：

- LLM Interface 调用、Prompt、Context Builder；
- Hunter Agent Instance 行为、Reasoning、Decision Loop；
- Knowledge Store、Visibility Policy、Clue/Evidence、Timeline；
- QQ/OneBot/NapCat 入站生产接管或消息发送；
- 私聊、语音、图片、临时会话；
- 主动行动、自动 Phase、自动结束或 AI DM；
- Episode Summary 写入 Agent Memory 或 T+5 cleanup executor；
- Agent Tool、Tool Registry、Tool Gateway 集成；
- Companion/User/Semantic/Graph Memory；
- 多 Agent、Container、分布式 ownership 或 lease。

## 15. Architecture Invariants

1. Game Runtime 仍是与 Normal Mode 并列的顶层 Mode。
2. DM 只控制当前 game，不成为 System Admin。
3. Session Command 不能直接修改数据库或 State。
4. 被接受的控制操作必须先持久化 Operation，再通过 GameEvent/Actor Apply。
5. CREATE 只能通过受限 bootstrap；CREATED 不拥有消息路由权。
6. 同一 Session 只有一个 Actor/State Writer。
7. expected `state_version` 和 binding version 必须在 Actor 内重验。
8. Confirmation 不替代 Authorization。
9. START 建立 ownership，PAUSE 保留，END 解除；均与 State 原子提交。
10. UNKNOWN 不自动重试。
11. Storage、Audit、Recovery 或 ownership 不确定时 fail closed。
12. Game Command/State 不进入 Normal Mode、Tool Registry 或普通 Memory。
13. P3-D 不引入 LLM、Knowledge、Reasoning、QQ Action 或自动行为。
14. V0.1 通信能力仍只冻结 `GROUP_TEXT`，但 P3-D 不启用其 Executor。
15. Hidden Truth 和 Private Knowledge 不通过 Session Command payload 传输。

## 16. Implementation Entry Gate

进入 P3-D Implementation 前必须人工确认：

- 本文的阶段编号修订；
- CREATE_SESSION bootstrap transaction；
- 9 个 Command 与状态矩阵；
- DM/Bootstrap Authorization；
- Confirmation 分类和 binding；
- Control Operation persistence；
- Event/Actor/ownership 原子边界；
- P3-D-1 至 P3-D-7 allowlist；
- Non-Goals 和全部 Architecture Invariants。

任一项需要调整时，先更新本文或新增 ADR，不得在实现中临时放宽。

本文完成 P3-D Session Control Plane Design Freeze。Implementation 尚未开始。
