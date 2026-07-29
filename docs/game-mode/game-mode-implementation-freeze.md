# Game Mode Implementation Constraint Freeze

- 类型：RFC / Architecture Decision Record
- 状态：Accepted / Frozen
- 版本：P2.1
- 架构基线：[Game Mode Architecture Freeze P0.2](/D:/猎bot/qq-reminder-bot/docs/game-mode/game-mode-architecture-freeze.md)
- 详细设计：[Game Mode P2 Detailed Design](/D:/猎bot/qq-reminder-bot/docs/game-mode/game-mode-p2-detailed-design.md)
- 仓库基线：`feature/agent-runtime-v3` / `b18f9553e743eb847854713085101ea02633447c`

## 1. 文档权威性

本文在 P0.2 Architecture Freeze 与 P2 Detailed Design 之上冻结 P3 Implementation 的实现级约束。P3 必须同时遵守 P0.2 与本文；本文不替代、不放宽已有 Architecture Freeze。

发生冲突时：

1. P0.2 Architecture Freeze 的架构与安全边界优先；
2. 本文的 Decisions 15–24 约束实现方式和阶段范围；
3. P2 Detailed Design 提供模块和数据流细节；
4. 任何需要改变冻结决策的方案必须先新增 ADR 并获得人工确认。

实现便利、局部代码结构、模型能力或第三方 API 差异均不能隐式改变这些约束。

## 2. Decision 15：LLM Interface Boundary

### 决策

Game Runtime 不直接管理模型 Provider 调用：

```text
Game Runtime
    |
Agent Runtime LLM Interface
    |
LLM Provider
```

Game Runtime 负责：

- Game Context Builder；
- Game Prompt Composition；
- Knowledge Visibility 与 Disclosure Manifest；
- Reasoning Memory 的结构化读写；
- Game 专用输出契约与结果校验。

Agent Runtime LLM Interface 负责：

- Provider 调用基础设施；
- 模型选择和受控配置；
- Token 使用统计；
- timeout、限流、Provider 错误等通用错误归类；
- 通用 request/correlation telemetry；
- Provider response 到中立响应协议的转换。

### 强制边界

- Game Runtime 只能依赖 LLM Interface/Port，不得直接导入或实例化具体 Provider SDK。
- LLM Interface 不读取 Game Knowledge、不构造 Game Prompt、不解释 DM 权限，也不修改 Session State。
- Token 统计不得记录 Prompt、剧本、Private Knowledge、Hidden Truth 或 Reasoning 正文。
- LLM 结果只能作为结构化 Result Event 回到 Session Actor，不能在回调中直接写 Game State。

### 禁止

- 复制 Normal Mode Prompt；
- 调用 Normal Mode Context Builder 或 `build_local_context()`；
- 注入 Companion/User/Semantic Memory；
- 复用 Normal Agent Tool Loop 执行 Game Action；
- 让 Provider 层持有 `game_id` 之外的业务授权逻辑。

## 3. Decision 16：Event Store Data Boundary

### 决策

Event Store 是结构化 Game Runtime 事件存储，不是聊天归档系统。

允许保存：

- GameEvent Envelope；
- Session Lifecycle 与 Game Phase 状态转换事件；
- Participant/Knowledge/Timeline 的结构化变化引用；
- Action 请求、claim 和状态变化；
- `event_id`、`sequence_no`、`state_version`、`correlation_id`、`causation_event_id`；
- 恢复、拒绝、错误类别等结构化处理结果。

禁止保存：

- 完整 QQ 群聊记录；
- 完整私聊原文；
- 图片、语音或其他媒体原文件；
- 未分类消息全文；
- 完整 Prompt、LLM 原始输出或 Chain of Thought；
- Hidden Truth、Character Private Knowledge 或 Reasoning 正文的内容副本。

### Observation 边界

平台消息正文只能进入按 `game_id` 隔离、具有短 TTL 的 Observation Buffer，用于分类和当前事件处理。Event Store 只保存最小来源引用和分类后的结构化事件；Observation 不进入普通消息归档、Companion Memory、Semantic Graph、日报或 Audit。

### 目标与非目标

Event Store 支持：

- Session 恢复；
- 事件去重和顺序验证；
- 状态变化追踪；
- Shared Audit correlation；
- Future Replay 的基础数据。

V0.1 不采用完整 Event Sourcing，不从 Event Store 取代 Game State Snapshot，也不提供聊天历史检索。

## 4. Decision 17：Knowledge Store Structure

### 决策

V0.1 使用一个统一的 Game Knowledge Store 领域边界，内部具有四个强隔离分区：

```text
Knowledge Store
├── Public Knowledge
├── Character Private Knowledge
├── Hidden Truth
└── Reasoning Space
```

统一表示：四类数据共享 `game_id` namespace、Knowledge ID、provenance、Lifecycle/Phase、版本和 retention contract。强隔离表示各分区具有不同 principal scope、查询端口和 Visibility Policy。

### 唯一 LLM 读取入口

```text
Knowledge Store
    |
Visibility Policy
    |
Context Builder
    |
Hunter LLM
```

Visibility Policy 是代码和数据访问层的强制授权，不是 Prompt 指令。Context Builder 的依赖接口不得返回 Hidden Truth 或其他 Character Private Knowledge 类型。

### 允许的存储实现

实现可以使用独立表、独立列族或受限 repository 来降低泄漏风险，但它们必须属于同一 Knowledge Store contract、同一 `game_id` 生命周期和同一 Visibility Policy，不得演化为四套互不一致的知识系统。

### 禁止

- 建立四套独立领域模型、版本系统和生命周期；
- 依靠 Prompt 要求模型“不要看”不可见数据；
- 把 Knowledge DB、通用 query handle 或 Hidden Truth repository 直接提供给 LLM；
- 让 Context Builder 读取 Companion/User/Semantic Memory 或 Episode Summary；
- 通过相关性、token 预算或故障降级绕过 Visibility Policy。

## 5. Decision 18：Reasoning Trigger Policy

### 决策

Reasoning Memory 不对每条消息更新，普通消息不直接启动深度 Reasoning Job。

允许触发深度 Reasoning 的事件集合为：

- `NEW_CLUE`；
- `CONTRADICTION_FOUND`；
- `PLAYER_QUESTION`；
- `PHASE_CHANGE`；
- `DM_COMMAND`。

命中集合表示“允许进入深度推理策略判断”，不表示每次都必须调用 LLM。Trigger 仍需满足 Session=RUNNING、Instance=ACTIVE、Phase Policy、版本一致性、cooldown、预算和单 Instance 最多一个深度 Job 的约束。

### 普通消息路径

普通消息只进行：

```text
Observe
  -> Participant/Message 分类
  -> 必要的 Statement/Timeline/Knowledge 状态更新
  -> 产生 Trigger 或 WAIT
```

`NEW_STATEMENT` 本身不直接触发深度推理。若分类结果形成新线索、实质矛盾、直接玩家问题或合法 DM 命令，必须先派生对应受控 Trigger，再由 Trigger Policy 决定是否深度推理。

### DM_COMMAND 约束

只有通过 Session Authorization 的 DM Command 可以成为深度推理 Trigger；非法 DM 操作只拒绝并 Audit。合法命令通常在 Phase、Knowledge 或 Action Goal 发生变化时触发推理，纯查询或重复命令可以直接 WAIT。

### 禁止

- 每条群消息调用 LLM；
- 每条消息重写 Reasoning Snapshot；
- 通过消息长度或自由文本提示绕过 Trigger 枚举；
- 并行启动多个深度 Reasoning Job 消化积压。

## 6. Decision 19：Action Queue Persistence

### 决策

所有具有外部或高影响状态副作用的 Game Action 必须先持久化，再执行或应用。

包括：

- `SPEAK_PUBLIC`；
- `SEND_PRIVATE`（未来 Capability，V0.1 禁用）；
- Session Control Action；
- DM Action；
- 未来语音、图片和其他外部通信 Action。

Session Control/DM Action 即使由 Session Actor 内部应用，而不是由 Hunter Planner 或外部通信 Executor 产生，也必须建立持久 Action/Operation Record、幂等绑定、Authorization/Audit 结果和终态。

### 核心状态

```text
CREATED
EXECUTING
SUCCESS
FAILED
UNKNOWN
```

`CANCELLED` 可以作为尚未执行 Action 的补充收束状态，但不能替代五个核心状态。

### 执行不变量

1. Action/Operation Record 与幂等键必须在副作用前持久化。
2. 从 CREATED 到 EXECUTING 的 claim 必须原子且同一 Action 只能有一个执行者。
3. 执行前重新验证 Session、Phase、Participant binding、Authorization、Confirmation、Metadata 和 Disclosure。
4. 结果状态持久化后，才能产生 `ACTION_COMPLETED` 或等价控制结果 Event。
5. Action 状态写入失败时不得继续新的副作用；已可能发生的副作用按 UNKNOWN 处理。

### UNKNOWN

UNKNOWN 表示副作用可能已经发生，但 Runtime 没有足够证据确认成功或失败。UNKNOWN：

- 禁止自动重试；
- 禁止自动生成相同副作用的替代 Action；
- 不能推定成功或失败；
- 只能通过平台回执、可验证 ID、幂等查询或有证据的人工 reconciliation 收束。

Game Action 不得为了复用 Queue/Gateway 而注册为 Agent Tool。

## 7. Decision 20：Episode Summary Generation

### 决策

游戏结束后的 Summary 流程固定为：

```text
Game End
    |
Allowlisted Summary Source View
    |
Summary Generator
    |
Disclosure Filter
    |
Game Episode Summary Memory
```

Summary Generator 只能读取专门构建的白名单 Source View。该 View 仅包含公开 Session Metadata 和公开最终结果：日期、剧本名称、参与者公开表示、Hunter 角色、胜负结果、MVP。

### 禁止输入

- 直接查询或总结完整 Game Memory；
- Hidden Truth；
- Reasoning Memory、Reasoning Summary 或 Hypothesis；
- Character Private Knowledge；
- Clue/Evidence 正文；
- 玩家原话、QQ ID、行为评价或画像。

Disclosure Filter 必须在写入长期 Memory 前执行字段 allowlist、长度、敏感实体和禁止内容校验。校验失败不得“尽量保存”部分自由文本；Summary 生成失败只写安全 Audit，不回滚 ENDED，也不延迟 T+5 清理。

## 8. Decision 21：Episode Summary Memory Namespace

### 决策

Episode Summary 使用独立长期 Memory Namespace：

```text
Memory
├── Personality Memory
├── User Preference Memory
└── Game Episode Summary Memory
```

它属于 Agent 可查询记忆，用户主动询问历史游戏经历时，Normal Agent 可以通过受控查询端口回答。

### 允许内容

- 日期；
- 剧本名称；
- 参与者公开表示；
- Hunter 角色；
- 胜负结果；
- MVP。

### 禁止内容

- 凶手身份；
- 私密线索；
- 推理过程；
- 玩家行为评价、可信度或心理画像；
- Hidden Truth 或隐藏剧情；
- QQ ID、Session 权限或私聊内容。

### 查询边界

- Normal Agent 只在用户主动询问相关游戏经历时查询该 Namespace。
- Game Context Builder、Knowledge Store、Reasoning Engine 和新 Session 禁止读取 Episode Summary。
- Episode Summary 不用于玩家画像、跨局推理、角色关系、事实初始化或模型训练素材。

## 9. Decision 22：Setup Control Plane

### 决策

V0.1 关闭 DM 私聊，因此角色私密信息和 Hidden Truth 必须通过受信 Setup Control Plane 进入系统：

```text
Trusted Initialization
    |
Setup Control Plane
    |
Game Character Identity
Character Private Knowledge
Hidden Truth
Initial Game State
```

### 边界

- Setup Control Plane 是受信初始化/修复端口，不是普通群消息入口。
- V0.1 不实现 UI。
- 每次写入必须绑定 `game_id`、Setup principal、policy version、visibility、provenance 和 Audit correlation。
- Setup 数据必须在 Session 启动前完成 schema、角色绑定和 Visibility 校验。
- 运行中需要新增私密信息时，必须先暂停 Session，再经受信 Repair 流程；不能改用群消息或 DM 私聊。

### 禁止

- 普通群消息导入 Character Private Knowledge 或 Hidden Truth；
- DM 私聊导入；
- 从普通聊天历史、文件附件或 LLM 输出自动推断初始化；
- 未授权主体读取或修改其他 Session 的 Setup 数据；
- 通过 Setup Control Plane 获取 Global Runtime Permission。

## 10. Decision 23：P3 Implementation Strategy

### 决策

P3 必须严格分阶段交付：

```text
P3-A  Runtime Skeleton
  |
P3-B  Mode Router Integration
  |
P3-C  Persistence
  |
P3-D  Hunter Agent Loop
  |
P3-E  Action Execution
```

禁止一次实现完整 Game Mode，禁止跨阶段提前启用后续能力。每个阶段必须有明确 allowlist、非目标、契约测试和人工准入。

### 阶段门槛

|阶段|核心目标|进入下一阶段前必须证明|
|---|---|---|
|P3-A|领域骨架与 contract|无 LLM/QQ/Knowledge/真实持久化副作用，Normal Mode 不变|
|P3-B|Mode ownership 与薄 ingress 接口|无活动 Session 透明通过；活动路由仍保持不可生产启用，直到持久 ownership 可恢复|
|P3-C|State/Event/Action 持久化与恢复|顺序、去重、版本、PAUSED ownership、UNKNOWN 可恢复|
|P3-D|Context/Reasoning/Hunter loop|Visibility、Trigger Policy、无 CoT、无 Hidden Truth 泄漏|
|P3-E|GROUP_TEXT Action execution|Authorization/Audit/Confirmation/Idempotency、UNKNOWN 不重试|

P3-B 在 P3-C 完成前只能集成并验证路由 contract，不得启用无法持久恢复的生产 Session ownership。任何阶段均不得改变现有 Normal Mode 的无活动 Session 行为。

## 11. Decision 24：P3-A Implementation Boundary

### 决策

P3-A 只允许实现：

- 顶层 `game_runtime` 骨架；
- Session Model；
- Event Model；
- Actor Skeleton；
- Action Queue Skeleton。

### P3-A 允许的最小内容

- 领域枚举、不可变数据 contract 和校验规则；
- 生命周期/Phase 的纯状态转换 contract；
- Mailbox/Actor 的接口和无外部副作用骨架；
- Action 状态机与 Queue port；
- shared infrastructure、persistence、LLM、communication 的抽象 port；
- 只验证 skeleton contract 的单元测试和文档。

### P3-A 禁止

- LLM Provider 调用、Prompt、Context Builder 或 Reasoning Job；
- QQ 接收、QQ 回复、主动发言或 Communication Executor；
- Knowledge Store 读取、Visibility 实现或 Hidden Truth 数据；
- 私聊、语音、图片；
- 完整 Persistence、数据库 schema、迁移或恢复；
- 生产 Mode Router 接管；
- Agent Tool 注册、Normal Mode 集成或 Companion Memory 接入；
- 创建可运行 Game Session 或产生任何真实副作用。

P3-A 的 Action Queue 仅冻结 contract、状态机和 port，不实现持久 store 或 Executor；Decision 19 的持久化要求在 P3-C 落地，在此之前不得执行 Action。

## 12. P3 实现准入清单

进入任一 P3 阶段前，变更必须回答并验证：

1. 变更属于哪个 P3 阶段和显式 allowlist？
2. 是否保持 Game Runtime 顶层 Mode，而非 Plugin/Tool/Memory？
3. 是否保持无活动 Session 的 Normal Mode 行为不变？
4. 是否存在游戏消息进入普通 archive/Memory/Graph 的路径？
5. 是否通过 Agent Runtime LLM Interface，而非直接 Provider 或 Normal Context？
6. Event Store 是否仅保存结构化事件？
7. Knowledge 访问是否先经过 Visibility Policy 和 Context Builder？
8. 是否仅由冻结 Trigger 启动深度 Reasoning？
9. 所有副作用 Action 是否先持久化并受治理？
10. Episode Summary 是否只读 allowlisted Source View 并通过 Disclosure Filter？
11. DM/Setup 权限是否严格绑定当前 `game_id`？
12. 是否引入当前阶段禁止的能力？

任一答案不满足时不得合并或部署。

## 13. 冻结不变量

1. P0.2 Architecture Freeze 的全部 Decisions 和安全不变量继续有效。
2. Game Runtime 不直接调用 LLM Provider，不复制 Normal Prompt/Context/Memory。
3. Event Store 不是聊天归档，不保存未分类消息全文。
4. Knowledge Store 统一建模并由 Visibility Policy 强制分区隔离。
5. 普通消息不直接触发深度 Reasoning 或 Reasoning Memory 更新。
6. 所有外部或高影响状态副作用 Action 在执行前持久化。
7. UNKNOWN 不自动重试。
8. Episode Summary 只从公开白名单 Source View 生成，并进入独立 Namespace。
9. Hidden Truth 和角色私密信息只通过受信 Setup Control Plane 初始化。
10. P3 必须按 P3-A → P3-B → P3-C → P3-D → P3-E 顺序推进。
11. P3-A 只实现 Skeleton，不接入 LLM、QQ、Knowledge、Hidden Truth 或完整 Persistence。

## 14. 状态

Decisions 15–24 共 10 项，自本文接受后冻结。P3 Implementation 只能在人工确认本文后启动；当前仍停留在 P2.1，不得进入 P3。
