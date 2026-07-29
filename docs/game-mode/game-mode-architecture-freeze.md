# Game Mode Architecture Freeze

类型：RFC Architecture Decision Record

状态：Accepted / Frozen

版本：P0.2

基线：`feature/agent-runtime-v3` / `b18f9553e743eb847854713085101ea02633447c`

## 1. 文档权威性

本文记录 Game Mode V0.1 已经人工确认、不再在实现阶段重新讨论的架构决策。本文优先级高于 P0、P0.1 和 Implementation Placement Proposal；其他文档与本文冲突时，以本文为准并同步修订。

冻结决策只能通过新的 ADR 显式替代。实现便利、局部代码结构或模型能力变化不能隐式改变这些边界。

## 2. Decision 1：Game Runtime 顶层模块

### 决策

Game Runtime 是 Agent Runtime 的独立顶层模块：

```text
Agent Runtime
├── Normal Mode
└── Game Runtime
```

未来代码目标位置为顶层 `game_runtime/`。NoneBot Plugin 只能作为薄 Message Ingress Adapter，不能承载 Game Runtime 业务状态和 Decision Loop。

### 禁止

- 将 Game Runtime 实现为普通 Plugin；
- 将 Game Runtime 注册为 Tool；
- 放入 `agent/tools` 子模块；
- 放入现有 memory 子模块；
- 使用 `play_script_game()` 式单次调用承载整局游戏。

### 理由

Game Runtime 自身拥有 Lifecycle、Event Loop、Session State、Actor Model 和 Action Queue。它是运行环境，不是可被 Agent 临时调用的一项能力。

### 结果

- Game Runtime 与 Normal Mode 只共享受控基础设施。
- Game Runtime 不进入 Agent Tool Registry 或 Metadata inventory。
- `game_runtime/` 不依赖 `ai_chat.py` 的 Normal Mode 上下文拼接或 Tool Loop。

## 3. Decision 2：活动游戏群消息独占

### 决策

存在 `RUNNING` Game Session 时，该群消息由 Game Runtime 独占：

```text
QQ Message
    |
Mode Router
    |
Game Runtime ONLY
```

同一消息不得同时进入 Normal Mode 和 Game Runtime。

### 禁止

游戏消息不得进入：

- Normal Mode Agent Loop；
- 普通聊天历史；
- Companion Memory；
- User/Semantic Memory；
- 普通语义图；
- 普通消息采集和日报素材链。

### 理由

消息独占用于防止双重回复、Game Character 污染日常 Agent、私密游戏信息进入长期数据链，以及普通 Tool 被游戏语言意外触发。

### 状态边界

- `RUNNING`：Game Runtime 独占并正常处理。
- `PAUSED`：Game Runtime 继续持有 ownership，只接受恢复、结束和受控修复事件，不回退 Normal Mode。
- `ENDED`：解除 ownership，后续消息恢复 Normal Mode 路由。

## 4. Decision 3：Shared Audit Engine + GAME Namespace

### 决策

不复制第二套 Audit 系统。Game Runtime 使用 Shared Audit Engine，并通过 `domain=GAME` 与普通 Agent Tool Audit 隔离。

概念事件：

```text
AuditEvent
    domain = GAME
    game_id
    session_id
    actor
    action
    policy_result
```

V0.1 中 `game_id` 表示游戏局，`session_id` 表示运行时 Session；二者一一对应但保持独立语义字段，避免未来恢复实例或多 Agent 扩展污染标识含义。

### 复用

- 持久 Audit HMAC；
- key epoch；
- stable fingerprint；
- append-only 事件机制；
- sanitizer 和隐私安全日志边界；
- correlation/invocation 关联语义。

### 隔离要求

- GAME 事件使用独立 domain 和事件类型；
- 不把 Game Action 注册为 Agent Tool 来获得审计；
- 不记录 QQ/剧本/私聊原文、Hidden Truth、Private Knowledge 或 Reasoning 正文；
- Shared Engine 故障策略保持高风险动作 fail closed。

## 5. Decision 4：独立 Game Memory

### 决策

Game Memory 独立于现有 Companion Memory 和 User/Semantic Memory：

```text
Memory Infrastructure
├── Companion Memory
├── User/Semantic Memory
└── Game Memory
```

V0.1 采用：

```text
Game State DB
+ Event Store
```

Game State DB 保存 Session、Phase、Participant、Knowledge、Timeline、Reasoning Summary 和 Action State；Event Store 保存可恢复的结构化 GameEvent。

### 禁止

- 跨 `game_id` 检索 Game Memory；
- 使用 Global Vector Memory 保存游戏知识；
- 将线索、角色秘密、Timeline、Reasoning 或 Hidden Truth 写入普通 Memory；
- 让 Game Context Builder读取 Companion/User/Semantic Memory。

### 未来扩展

未来可以增加严格以 `game_id` 为 namespace 的 Vector Index。该 Index 仍属于 Game Memory，受 Session 可见性和结束清理策略约束，不能升级为全局索引。

## 6. Decision 5：游戏结束数据策略

### 决策

Game Session 结束后生成一条 `Game Episode Summary Memory`，作为 Game Memory 与长期 Agent Memory 之间唯一允许的受控输出。

Summary 目标长度约 100 个中文字符。允许内容：

- 日期；
- 剧本名称；
- 参与玩家；
- Hunter 角色；
- 胜负结果；
- MVP。

禁止内容：

- 凶手身份；
- 私密线索；
- 推理过程或 Reasoning Summary；
- 玩家行为评价、可信度或心理画像；
- 隐藏剧情；
- 其他角色私密信息。

参与玩家字段只记录本局公开身份表示，不记录 QQ 号、权限、私聊内容或行为标签。

### 生命周期

```text
T+0
  - Session 进入 ENDED
  - 生成并校验 Episode Summary
  - 写入长期 Agent Memory 的 GAME_EPISODE 类型
  - Game Memory 立即停止在线检索

T+5 days
  - 删除 Game Session 内容
  - 删除 Timeline
  - 删除 Clue/Evidence
  - 删除 Reasoning Memory
  - 删除 Character Private Knowledge
  - 删除 Hidden Truth
  - 删除 Game Event Store 内容

长期保留
  - Episode Summary
  - Audit Metadata
```

Episode Summary 是受限纪念性记录，不是 Game Knowledge、玩家画像或跨局推理素材。Game Context Builder 必须拒绝读取它，新 Session 不得用它建立事实、怀疑或角色关系。

如果 Summary 生成失败，Session 仍然可以结束；记录失败 Audit，并且不得延迟 T+5 清理。Summary 禁止从 Hidden Truth 或 Reasoning 正文生成，输入只能来自允许的公开 Session Metadata 和最终公开结果。

## 7. Decision 6：Lightweight Hunter Agent Instance

### 决策

V0.1 使用进程内 Lightweight Agent Instance：

```text
Agent Runtime
    |
Game Session
    |
Hunter Instance
```

Instance 绑定 `game_id`、角色、Policy Version、State Version、Context Builder 和 Reasoning Summary。它是逻辑隔离单元，不是独立部署单元。

### V0.1 禁止

- 独立 Container Runtime；
- 分布式 Agent；
- 多实例调度；
- 多 Hunter Agent；
- 跨进程 Actor 协调。

Concurrency Model 保持单进程、每个 `game_id` 一个逻辑 Actor、同局串行、不同局异步并行。Multi-Agent 阶段再设计独立调度和分布式 ownership。

## 8. Decision 7：V0.1 通信范围

### 决策

Game Mode V0.1 只启用：

```text
GROUP_TEXT
```

明确关闭：

- 主动私聊玩家；
- DM 私聊；
- 临时会话；
- 语音输入或输出；
- 图片线索理解或发送。

### 保留边界

保留 Communication Adapter 抽象和能力枚举，但关闭的 Capability 必须返回 unavailable。Action Planner 不得生成 `SEND_PRIVATE`，不能把私聊失败降级为群内公开发送。V0.1 的 DM 群内控制只能携带可向全群公开的控制信息，不能通过群消息录入 Hidden Truth 或 Character Private Knowledge；这些私密内容必须在 Session 启动前通过受信 Session Setup Control Plane 预置。若没有该控制面，V0.1 不支持在运行中新增私密信息。

## 9. 冻结安全不变量

1. Normal Mode 行为在无活动 Session 的群保持不变。
2. Game Mode 不成为 Plugin、Tool、agent/tools 或 memory 子模块。
3. `RUNNING/PAUSED` 游戏群的消息 ownership 不进入 Normal Mode。
4. DM 权限只属于当前 Session，不成为 System Admin。
5. Hidden Truth 不进入 Hunter Context。
6. 不保存完整 Chain of Thought。
7. Game Memory 不跨 Session 检索。
8. Episode Summary 是进入长期 Agent Memory 的唯一受控例外。
9. 所有 Game Action 经过 Authorization、Shared Audit Engine 和 Idempotency。
10. `UNKNOWN` Action 不自动重试。
11. T+5 清理不因 Summary 或复盘失败而取消。
12. V0.1 只启用 `GROUP_TEXT`。

## 10. 实现准入

后续实现必须逐项映射本 ADR 的 Decision 和不变量。任何实现方案如果需要改变本文决策，必须先提交新的 Architecture Decision Record 并获得人工确认，不能在代码评审中临时放宽。
