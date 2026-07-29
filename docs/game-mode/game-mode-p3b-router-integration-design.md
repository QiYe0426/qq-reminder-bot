# Game Mode P3-B Router Integration Design

- 文档类型：RFC / Architecture Design
- 状态：P3-B Design Freeze，待人工确认
- 架构基线：[P0.2 Architecture Freeze](/D:/猎bot/qq-reminder-bot/docs/game-mode/game-mode-architecture-freeze.md)、[P2 Detailed Design](/D:/猎bot/qq-reminder-bot/docs/game-mode/game-mode-p2-detailed-design.md)、[P2.1 Implementation Freeze](/D:/猎bot/qq-reminder-bot/docs/game-mode/game-mode-implementation-freeze.md)
- 仓库映射：[P1 Repository Mapping](/D:/猎bot/qq-reminder-bot/docs/game-mode/game-mode-p1-repository-mapping.md)
- 代码基线：`feature/game-mode-runtime-v01` / `cbf8200852cf7fe37592bc353db5742bc98306d4`
- 范围：Mode ownership、Session Registry、Message Envelope、GameEvent 转换、故障与测试设计
- 非目标：代码、配置、Feature Flag、数据库、真实消息接管、LLM、推理、Action 和 QQ 回复

## 0. 决策摘要

1. Router 对每条消息只返回一个终态决策：`GLOBAL_CONTROL`、`GAME_RUNTIME`、`NORMAL_MODE` 或 `REJECTED`。不存在广播给两个 Mode 的结果。
2. 路由优先级固定为 Global Control Plane → Active Game Session → Normal Mode。只有权威 Registry 明确返回 `NO_SESSION` 或已解除 ownership 的 `ENDED`，才能选择 Normal Mode。
3. `RUNNING` 与 `PAUSED` 都由 Game Runtime 独占。PAUSED 仍产生 `MESSAGE_RECEIVED` GameEvent，但禁止 LLM、主动行动、普通 Action 和 Normal Mode 回退。
4. Session Registry 只提供 `group_id -> ownership snapshot`，不读取完整 Game State，不负责 Participant 权限、DM 鉴权、推理或状态转换。
5. Registry timeout、异常、状态不一致或 Game Runtime 接收失败均 fail closed：阻断 Normal Mode，返回安全拒绝/不可用结果并记录安全 telemetry；不把消息转交普通 Agent。
6. Router 先处理平台级 Message Envelope；只有选择 Game Runtime 后，才补充 `game_id/session_id` 并创建 GameEvent，避免未路由消息携带伪造 Session scope。
7. 未登记用户仍属于活动游戏群的 Game Runtime ownership，产生 actor=`UNKNOWN` 的 GameEvent，只允许观察/分类，不能获得 DM、Player 或 Action 权限。
8. P3-B 实现阶段只建立平台无关 Router、Registry/Ingress contract、Envelope 转换和测试接收端。P3-C 完成持久 ownership/recovery 前不得注册生产入口或启用真实 Session 接管。

## 1. Routing Architecture

### 1.1 目标架构

```text
Incoming Platform Message
        |
Message Ingress
        |
Ingress Message Envelope
        |
Global Control Plane Check          Priority 0
        |
Mode Router
        |
Session Registry Lookup(group_id)   Priority 1
        |
Routing Decision
   +----+--------------------+
   |                         |
Game Runtime             Normal Mode           Priority 2
   |
Routed Game Envelope
   |
GameEvent
   |
Game Runtime Ingress Port
   |
GameSessionActor
```

Global Control、Game Runtime 和 Normal Mode 是互斥终点。Router 不返回多个 destination，不允许下游自行再次判断 Mode。

### 1.2 单一所有权不变量

每条消息由一个 `routing_decision_id` 标识一次路由判定。判定成功后：

- `GLOBAL_CONTROL`：Global Control Plane 消费，Game/Normal 均不可见；
- `GAME_RUNTIME`：Game Runtime 接收，所有 Normal matcher/Tool/Memory consumer 必须被阻断；
- `NORMAL_MODE`：Router 不创建 GameEvent，现有 Normal 链路保持不变；
- `REJECTED`：安全丢弃或固定不可用处理，Game/Normal 均不执行业务。

任何 downstream error 都不能把已经选择的 destination 改成另一个 Mode。尤其是 GameEvent 创建或 Game Runtime 接收失败时，不允许 fallback 到 Normal Mode。

### 1.3 P3-B 与生产接管边界

P2.1 已冻结：P3-B 在 P3-C 前不得启用无法持久恢复的生产 ownership。因此 P3-B 后续代码只能形成：

```text
platform-neutral envelope
  -> pure routing contract
  -> in-memory/fake Session Registry
  -> test Game Runtime ingress sink
```

P3-B 不修改 `pyproject.toml` 插件加载，不注册真实 NoneBot matcher，不创建 Feature Flag，不接管生产群消息。P3-C 完成 State/Registry persistence、恢复和 PAUSED ownership 证明后，才允许通过单独人工准入设计真实 ingress activation。

## 2. Message Routing Flow

### 2.1 分层职责

|步骤|输入|输出|负责|不负责|
|---|---|---|---|---|
|Message Ingress|平台 Event|Ingress Message Envelope|平台字段提取、GROUP_TEXT 类型校验、correlation 初始化|Session 查询、权限、GameEvent、回复|
|Global Control Check|Ingress Envelope + authenticated platform principal|NOT_CONTROL / AUTHORIZED / DENIED / ERROR|识别极小全局控制白名单并验证系统权限|DM 权限、普通命令、Game 状态|
|Mode Router|Envelope + Control result|唯一 Routing Decision|执行固定优先级和 fail-closed 规则|推理、Participant 授权、状态修改|
|Session Registry Lookup|`group_id`|SessionLookupResult|返回原子 ownership snapshot|读取 Knowledge/Reasoning、判断 DM|
|Mode Selection|LookupResult|GAME/NORMAL/REJECTED|将状态映射为 destination|创建回复、调用 Tool|
|Game Envelope Enrichment|Envelope + active Session snapshot|RoutedGameMessageEnvelope|绑定可信 game/session scope|Participant 提权、消息分类|
|GameEvent Factory|Routed Game Envelope|`MESSAGE_RECEIVED` GameEvent|Schema 校验、Event/actor/source 映射|持久化、推理、Action|
|Game Runtime Ingress Port|GameEvent|ACCEPTED/REJECTED/UNAVAILABLE|定位 Actor 并提交事件 contract|Normal fallback、QQ 回复|

### 2.2 决策流程

```text
1. normalize platform message
2. evaluate Global Control Plane
   - AUTHORIZED -> GLOBAL_CONTROL, terminal
   - DENIED/ERROR -> REJECTED, terminal
   - NOT_CONTROL -> continue
3. lookup Session Registry by group_id
   - RUNNING -> GAME_RUNTIME
   - PAUSED -> GAME_RUNTIME
   - NO_SESSION -> NORMAL_MODE
   - ENDED with released ownership -> NORMAL_MODE
   - timeout/error/inconsistent -> REJECTED
4. for GAME_RUNTIME only:
   enrich trusted scope -> create GameEvent -> submit Game Runtime port
5. terminalize decision; never evaluate another Mode
```

只有 `LookupResult=OK` 且状态明确无活动 ownership 时可以进入 Normal Mode。不存在“查询不到就当作没有 Session”的隐式默认。

## 3. Router Priority

### 3.1 Priority 0：Global Control Plane

Global Control Plane 处理进程/Runtime 级维护操作，例如：

- 经强认证的系统管理员关闭服务；
- 经白名单定义的 Runtime maintenance/emergency operation；
- 独立运维策略允许的全局健康或安全操作。

它优先于 Game ownership，因为其作用域是 Runtime/进程，而非某个 `game_id`。它不属于 Normal Agent，也不是供 DM 调用的特殊 Game 命令。

Control Check 必须返回四态结果：

|结果|路由行为|
|---|---|
|`NOT_CONTROL`|继续 Session Registry 查询|
|`AUTHORIZED_CONTROL`|只交 Global Control Plane，终止其他路由|
|`CONTROL_DENIED`|最小拒绝并阻断 Game/Normal|
|`CONTROL_CHECK_ERROR`|fail closed，阻断 Game/Normal并告警|

“看起来像控制命令但认证失败”的消息不能作为普通文本进入 Game 或 Normal Agent，避免通过自然语言触发第二条解释路径。

### 3.2 Priority 1：Active Game Session

活动 Session 对群消息具有业务 ownership：

- `RUNNING`：所有 V0.1 `GROUP_TEXT` 进入 Game Runtime；
- `PAUSED`：ownership 保留，消息仍进入 Game Runtime 的受限事件路径；
- 普通 Agent、Reminder、Search、Tool、Collector、Companion、Semantic Graph、日报等不得消费。

Game Session 优先于 Normal Mode，因为 Normal Mode 无权解释游戏角色、阶段、DM 命令和知识边界。让普通 Agent 先处理会产生双回复、Tool 误调用和长期 Memory 污染。

### 3.3 Priority 2：Normal Mode

Normal Mode 是显式无 Game ownership 时的结果，不是异常 fallback。以下条件之一且 Registry 查询成功时才允许：

- `NO_SESSION`；
- `ENDED` 且 ownership 已原子解除。

无活动 Session 的群保持当前 Normal Mode 行为不变。Router 不重写 Normal prompt、Context、Tool、Memory 或 matcher 顺序。

## 4. Mode Selection Rules

|Global Control Result|Registry Result|Destination|是否创建 GameEvent|是否允许 Normal|
|---|---|---|---:|---:|
|AUTHORIZED|不查询|GLOBAL_CONTROL|否|否|
|DENIED/ERROR|不查询|REJECTED|否|否|
|NOT_CONTROL|NO_SESSION|NORMAL_MODE|否|是|
|NOT_CONTROL|RUNNING|GAME_RUNTIME|是|否|
|NOT_CONTROL|PAUSED|GAME_RUNTIME|是|否|
|NOT_CONTROL|ENDED + released|NORMAL_MODE|否|是|
|NOT_CONTROL|lookup timeout/error|REJECTED|否|否|
|NOT_CONTROL|inconsistent/stale ownership|REJECTED|否|否|

### 4.1 RUNNING

RUNNING Session 的群消息：

```text
Envelope
  -> active Session scope
  -> MESSAGE_RECEIVED GameEvent
  -> Game Runtime Ingress Port
  -> Actor mailbox
```

不进入 Normal Mode，也不调用普通 Agent Tool。P3-B 只验证转发 contract，不执行 Hunter 决策或产生回复。

### 4.2 PAUSED

PAUSED 仍表示群被本 Session 占用。消息路径为：

```text
Envelope
  -> PAUSED Session scope
  -> MESSAGE_RECEIVED GameEvent
  -> Game Runtime restricted handling
  -> no proactive action
```

不能回退 Normal Mode，原因包括：

- 暂停不是结束，角色和知识 ownership 仍存在；
- 普通 Agent 无法判断消息是否是恢复、结束或修复命令；
- 回退会把游戏内容写入普通聊天历史和长期 Memory；
- PAUSED 期间双 Mode 语义会让恢复后的事件顺序不可证明。

P3-B 只创建并转交内存 GameEvent。目标架构中的 Event Store 记录在 P3-C 落地；P3-B 不伪造持久化保证，也不因此启用生产 PAUSED routing。

### 4.3 ENDED

Session 结束必须原子完成：

1. Session 进入 ENDED；
2. 停止新 GameEvent/Action；
3. 从 active `group_id` ownership index 解除绑定；
4. 新消息 Registry 查询返回 NO_SESSION，或在短暂可见窗口返回 `ENDED + released`；
5. Router 恢复 Normal Mode。

Router 不读取旧 Game Memory、Episode Summary、Reasoning 或 Event Store来决定路由。已在结束前路由到旧 Actor 的晚到 Event 由 Game Runtime 按版本/生命周期拒绝，不能转投 Normal Mode。

## 5. Session Registry Contract

### 5.1 定位

Session Registry 是 Router 的只读 ownership 视图：

```text
group_id -> SessionLookupResult
```

它不是 GameSession aggregate repository 的通用查询接口，也不是权限服务。

### 5.2 输入与输出

输入：

- `group_id`；
- lookup deadline；
- `routing_decision_id/correlation_id`，仅用于 telemetry。

成功输出 `SessionOwnershipSnapshot`：

|字段|语义|
|---|---|
|`lookup_status`|固定 `OK`|
|`session_status`|NO_SESSION/RUNNING/PAUSED/ENDED|
|`group_id`|查询群|
|`game_id`|RUNNING/PAUSED 必填；无 ownership 时为空|
|`session_id`|RUNNING/PAUSED 必填；无 ownership 时为空|
|`ownership_generation`|绑定代次，防止同群旧 Session 污染|
|`state_version`|读取时 Session 版本引用|
|`ownership_released`|ENDED 时必须为 true 才能进入 Normal|
|`observed_at`|Registry 读取时间|

失败输出与 Session 状态分离：

|`lookup_status`|含义|
|---|---|
|`TIMEOUT`|未在 deadline 内得到权威结果|
|`UNAVAILABLE`|Registry 不可用|
|`INCONSISTENT`|状态、ID、binding 或版本组合非法|

失败结果不得伪装成 `NO_SESSION`。

### 5.3 状态语义

|状态|ownership|Router 决策|
|---|---:|---|
|NO_SESSION|无|Normal Mode|
|RUNNING|有|Game Runtime|
|PAUSED|有|Game Runtime|
|ENDED|无；必须 released|Normal Mode|

对于 RUNNING/PAUSED，缺失 `game_id`、`session_id`、generation 或版本视为 INCONSISTENT。对于 ENDED，`ownership_released=false` 视为 INCONSISTENT并 fail closed。

### 5.4 Registry 职责边界

Registry 负责：

- 查询活动 Session ownership；
- 返回 Lifecycle 状态和可信 scope；
- 保证同一 `group_id` 最多一个 RUNNING/PAUSED binding；
- 提供原子 Snapshot，不混合两个版本。

Registry 不负责：

- 判断发送者是 DM、Player、Spectator 还是 Unknown；
- Authorization 或 Confirmation；
- 读取/写入 Knowledge、Reasoning、Timeline、Action；
- 推进 Phase 或修改 Session；
- 构建 Context、调用 LLM、回复 QQ；
- 查询旧 Game Memory 决定路由。

### 5.5 P3-B/P3-C 分界

P3-B 只能定义 Registry Port 和 in-memory/fake contract fixture。P3-C 才实现持久 ownership source、启动恢复、版本一致性和 PAUSED binding。没有 P3-C 的恢复证明，真实 ingress 不得依赖进程内 Registry 接管生产消息。

## 6. Message Envelope Design

### 6.1 两阶段 Envelope

路由前不知道可信 `game_id/session_id`，因此不能使用一个允许平台填写 Session scope 的对象。定义两个不可混淆的契约：

```text
Platform Event
  -> IngressMessageEnvelope
  -> Routing Decision + Registry Snapshot
  -> RoutedGameMessageEnvelope
  -> GameEvent
```

### 6.2 IngressMessageEnvelope

|字段|来源|约束|
|---|---|---|
|`platform_event_id`|平台 Adapter|平台范围内稳定；用于去重来源|
|`platform`|Adapter|V0.1 为 ONEBOT|
|`group_id`|平台 Event|必填；P3-B 只设计群消息|
|`sender_id`|平台 Event|不表示 Participant 权限|
|`timestamp`|平台 Event|业务发生时间，不决定提交顺序|
|`message_type`|Normalizer|V0.1 只接受 GROUP_TEXT|
|`content_ref`|Ingress/短期 Observation|最小短期引用，不是普通聊天归档|
|`correlation_id`|Ingress Runtime|每次入口生成，贯穿 Router/Event/Audit|

平台不得提供 `game_id`、`session_id`、Participant role、DM flag 或 Global admin 结论。

### 6.3 RoutedGameMessageEnvelope

由 Router 使用权威 Session snapshot 补充：

|字段|来源|
|---|---|
|全部 Ingress fields|不可变复制|
|`game_id`|Session Registry|
|`session_id`|Session Registry|
|`session_status`|Session Registry|
|`ownership_generation`|Session Registry|
|`observed_state_version`|Session Registry|
|`routing_decision_id`|Mode Router|
|`participant_reference`|初始为 UNKNOWN 或仅安全引用；权限由 Game Runtime 后续解析|

Enrichment 必须校验 Snapshot 的 group、status、IDs 和 generation，不能接受调用方传入的覆盖值。

### 6.4 GameEvent 映射

|GameEvent 字段|映射|
|---|---|
|`event_id`|Runtime 生成；平台重投时使用稳定来源去重映射|
|`game_id/session_id`|Registry Snapshot|
|`event_type`|固定 MESSAGE_RECEIVED|
|`actor`|已安全匹配 Participant 时为引用，否则 UNKNOWN|
|`source`|PLATFORM|
|`timestamp`|平台 timestamp|
|`payload`|message_type、content_ref、platform_event_id 等最小结构化字段|
|`correlation_id`|Ingress correlation_id|

GameEvent Factory 不保存完整 QQ 聊天，不调用普通 Message Archive，不解析 DM 权限，不创建 Statement/Clue/Action。

## 7. Unknown Participant Handling

Mode ownership 由群决定，不由发送者身份决定。因此活动游戏群中的未知用户：

```text
UNKNOWN sender
  -> Game Runtime ownership
  -> MESSAGE_RECEIVED(actor=UNKNOWN)
  -> observation/classification boundary
  -> WAIT
```

规则：

- 必须进入 Game Runtime，不能因为未登记而进入 Normal Mode；
- 必须产生最小 GameEvent，以维持事件顺序和安全追踪；
- Router 不自动创建 Player/DM 身份；
- UNKNOWN 无 DM、Player、Setup、Phase 或 Action 权限；
- 不能触发 Session Control、普通 Agent Tool 或主动行动；
- 后续合法 Participant 绑定必须通过 Session 管理流程和新版本生效，不能追溯提升旧 Event 权限。

P3-B 没有推理或 Action，因此实现测试只验证 UNKNOWN Event 被交给 Game Runtime test sink，Normal sink 未被调用。

## 8. Router Failure Model

### 8.1 原则

- Mode ownership 不确定时 fail closed；
- 只有权威“无活动 ownership”证明可以进入 Normal Mode；
- 故障局部化且不通过 Normal Agent补偿；
- 错误信息不包含消息正文、QQ ID、剧本或 Session 私密状态；
- P3-B 不建立持久重试队列，不宣称消息已可靠记录。

### 8.2 Session Registry 失败

Registry timeout/unavailable：

1. 允许在入口 deadline 内进行一次有界重试；
2. 重试仍失败则产生 `REJECTED/ROUTING_UNAVAILABLE`；
3. 阻断 Normal Mode 和 Game Runtime业务处理；
4. 写隐私安全 metric/Audit intent：group fingerprint、failure class、correlation、耗时；
5. 不保存完整消息，不无限重试。

该策略可能在 Registry 全局故障时降低 Normal Mode 可用性，但避免活动游戏消息泄漏到 Normal Memory/Tool。安全边界优先于无证据的 fail open。

### 8.3 Session 状态读取失败

以下视为 INCONSISTENT：

- RUNNING/PAUSED 缺少 game/session ID；
- 同群返回多个活动 binding；
- ENDED 但 ownership 未释放；
- generation/state version 不合法；
- Snapshot 在单次读取中发生撕裂。

处理为 fail closed。Router 不自行修复、不读取 Game State DB、不猜测最近状态；由 Session/Recovery 维护路径修复。

### 8.4 GameEvent 创建失败

Schema、时间戳、scope 或 message type 校验失败时：

- destination 仍不改为 Normal；
- 返回 REJECTED；
- 若已有可信 game/session scope，可记录安全 `SYSTEM_ERROR` intent；
- 不把原始平台对象传给 Actor 绕过 Factory。

### 8.5 Game Runtime 接收失败

Ingress Port 返回 REJECTED/UNAVAILABLE、Actor 不存在或 mailbox 拒绝时：

- 消息已被判定为 Game ownership，必须阻断 Normal；
- 不重试到另一个 Mode；
- P3-B 只记录安全错误并返回失败；
- P3-C 后由 Event Store 与 Recovery 定义可靠接收/重试语义。

### 8.6 Global Control Check 失败

Control-shaped 消息认证失败或 Control service 异常时，终态为 REJECTED。不得继续 Session Registry 或 Normal Mode，避免同一文本被低优先级路径重新解释。

## 9. Security Boundary

### 9.1 Global Control 与 DM

|维度|Global Control Plane|Game DM|
|---|---|---|
|作用域|Runtime/进程级|单一 game_id/session_id|
|认证来源|系统管理员/运维策略|Participant Registry + Session binding|
|路由优先级|Priority 0|进入 Game Runtime 后由 Actor授权|
|允许操作|极小维护/紧急白名单|开始、暂停、结束、合法 Phase 等|
|Normal Tool|不等于普通 Tool；仅专用 control contract|禁止|
|影响其他 Session|仅显式全局运维动作|禁止|
|修改 Global Permission|受独立系统策略|禁止|

DM 发送类似“关闭服务”的文本不能凭 DM 身份通过 Global Control Check。System Admin 也不会因为全局身份自动成为某局 DM。

### 9.2 Normal Mode 污染防线

目标生产集成必须同时具备：

1. 早期 ownership 判定先于现有 priority 4/5/20/29/30/31 matcher；
2. GLOBAL/GAME/REJECTED 结果终止下游 matcher；
3. Normal AI、Collector、Group Reaction、Media consumer 的防御性 active-game guard；
4. 游戏消息不写 ordinary archive、group context、Companion Memory、Semantic Graph、Daily Report；
5. 隔离测试断言所有 Normal sink 调用次数为零。

P3-B 设计并测试 Router contract；真实 matcher priority/block 与这些防御 guard 的生产启用必须等待 P3-C ownership persistence 和独立人工准入。

### 9.3 数据最小化

- Router 日志只记录 decision、状态类别、correlation、耗时和安全 fingerprint；
- 不记录消息正文、sender ID 明文、剧本或 Participant role；
- Registry 不返回 Knowledge/Reasoning/Character 数据；
- Routing Decision 不能作为 DM/Player Authorization 证据；
- Envelope/Router 不调用 LLM或 Prompt 分类 Mode。

## 10. P3-B Implementation Scope

### 10.1 允许

P3-B 后续代码阶段只允许：

- 平台无关 `IngressMessageEnvelope` 与 `RoutedGameMessageEnvelope` contract；
- `RoutingDecision`、destination 和 failure reason 枚举；
- `SessionRegistry` 只读 Port 与严格 Result validation；
- 固定 Priority 0/1/2 的纯 Router service；
- GameEvent Factory，创建 `MESSAGE_RECEIVED`；
- Game Runtime Ingress Port 和内存 test sink；
- UNKNOWN Participant 的安全默认映射；
- 不依赖 NoneBot/OneBot 的单元和契约测试。

### 10.2 禁止

- 修改真实 Message Ingress、NoneBot matcher、Mode Router 或 Normal Mode；
- 在 `pyproject.toml` 注册 ingress plugin；
- 创建数据库、Event Store persistence、migration、配置或 Feature Flag；
- 启用真实群 ownership 或转发生产 QQ 消息；
- LLM、Prompt、Context Builder、Knowledge、Hidden Truth、Reasoning；
- Hunter 决策、Action 执行、QQ 回复或主动发言；
- 调用 Agent Tool、Companion Memory、普通 archive 或 Semantic Graph；
- 用进程内 Registry 伪装可恢复生产 ownership。

### 10.3 预期代码放置（非本阶段实现）

```text
game_runtime/
├── routing/             # envelope、decision、router service
├── interfaces/         # SessionRegistry、GameRuntimeIngress ports
└── event/               # MESSAGE_RECEIVED factory

tests/game_runtime/
├── test_router.py
├── test_router_fail_closed.py
└── test_message_envelope.py
```

不在 `plugins/`、`ai_chat.py` 或 `agent_tools/` 中实现领域 Router。

## 11. Test Plan

### 11.1 必须用例

|Case|Registry/Control|输入|期望 destination|关键断言|
|---|---|---|---|---|
|1 无活动 Session|NO_SESSION|普通 GROUP_TEXT|NORMAL_MODE|不创建 GameEvent；Normal sink 恰好一次|
|2 RUNNING|RUNNING|玩家消息|GAME_RUNTIME|创建正确 game/session Event；Normal sink 零次|
|3 PAUSED|PAUSED|玩家消息|GAME_RUNTIME|创建 Event；无 Action/回复；Normal sink 零次|
|4 群隔离|A=RUNNING，B=NO_SESSION|两群消息|A=GAME，B=NORMAL|scope、decision、调用计数互不污染|
|5 Registry 异常|TIMEOUT/UNAVAILABLE|普通消息|REJECTED|Normal/Game business sink 均零次；fail closed|

### 11.2 补充安全用例

|Case|期望|
|---|---|
|Global Control authorized|只调用 Global Control sink，不查询 Registry|
|Global Control denied/error|REJECTED，不进入 Game/Normal|
|ENDED + released|Normal Mode；不读取旧 Game Memory|
|ENDED + not released|INCONSISTENT/REJECTED|
|UNKNOWN Participant in RUNNING|GameEvent actor=UNKNOWN；无权限/Action|
|RUNNING missing game/session ID|Result validation fail closed|
|GameEvent Factory failure|不回落 Normal，不把平台原对象直接投 Actor|
|Game Runtime ingress rejected|不回落 Normal；返回安全失败|
|同一 Envelope 只终态一次|不能先 GAME 后 NORMAL，也不能重复 sink|
|不同 Session 同 platform_event_id|来源去重 scope 不互相污染|
|非 GROUP_TEXT|P3-B contract 拒绝 unavailable capability|

### 11.3 测试隔离要求

- 使用 fake Registry、fake Global Control Checker、capture Game/Normal sinks；
- 无 NoneBot、OneBot、NapCat、网络、数据库、LLM 或真实 Memory；
- 每个测试断言所有未选择 sink 调用次数为零；
- 使用固定时间、ID 和 correlation，保证 deterministic；
- 失败测试不得把错误信息与消息正文写入日志 fixture。

### 11.4 P3-B 退出门槛

进入 P3-C 设计/实现前必须证明：

1. 所有 Routing Decision 唯一且终态；
2. RUNNING/PAUSED 从不进入 Normal；
3. 只有权威 NO_SESSION/ENDED-released 进入 Normal；
4. Registry/Event/Ingress 故障均不 fail open；
5. 群、game、session、generation 严格隔离；
6. 未引入真实 ingress、配置、数据库、LLM、QQ 回复或 Normal Mode 修改。

## 12. 不变量与停止边界

1. Global Control > Active Game Session > Normal Mode 的优先级不可调整。
2. 一条消息只有一个 Mode owner；Router 不支持多播。
3. RUNNING/PAUSED ownership 均属于 Game Runtime。
4. Registry failure 不能解释为 NO_SESSION。
5. Game 路由失败不能 fallback Normal。
6. Router 不负责 DM/Participant Authorization，不读取 Game State/Knowledge/Reasoning。
7. 未登记用户进入 Game observation，但权限为 UNKNOWN 且默认 WAIT。
8. ENDED 解除 ownership 后才恢复 Normal；Router 不读取旧 Game Memory。
9. P3-B 不提供持久接收保证，不得在 P3-C 前启用生产 ownership。
10. P3-B 不实现 LLM、推理、Action、QQ 回复或真实消息处理。

本文件完成 P3-B Router Integration Design Freeze。当前只形成架构基线，不启动 P3-B 代码实现；等待人工确认。
