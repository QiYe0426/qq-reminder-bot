# Game Mode 剧本杀 AI Player Runtime 架构设计方案 P0

状态：Architecture Baseline

基线：`feature/agent-runtime-v3` / `b18f9553e743eb847854713085101ea02633447c`

角色：Hunter Game Agent，Level 2 AI Player

冻结决策：[Game Mode Architecture Freeze P0.2](game-mode-architecture-freeze.md)。本文与冻结 ADR 冲突时，以冻结 ADR 为准。

## 核心决策

- Game Mode 是与 Normal Mode 并列的 Runtime Mode，不是 Plugin 或单次 Tool。
- 每条消息只能由一个 Mode 获得处理权；活动游戏群不得同时触发 Normal Mode。
- DM 是当前 `game_id` 内的游戏控制者，不是 System Admin。
- Long Term Agent Identity 与 Game Character Identity 分离。
- Game Memory 以 `game_id` 强隔离，禁止跨局检索和学习；游戏结束后只允许生成约 100 字、经过字段白名单校验的 Episode Summary。
- Hidden Truth 不进入 Hunter LLM Context；内部推理只保存结构化状态，不保存完整 Chain of Thought。

## 1. 总体架构

```text
QQ / OneBot Event
        |
Message Ingress
        |
Runtime Mode Router
        +-----------------------+
        |                       |
Normal Mode Runtime      Game Mode Runtime
        |                       |
Chat / Reminder /        Game Session Manager
Report / Agent Tools            |
                          Hunter Game Agent
```

Mode Router 依据活动 Session 绑定进行确定性路由：无活动 Session 的群进入 Normal Mode；`RUNNING` 或 `PAUSED` Session 所属群由 Game Mode 独占；`ENDED` 后解除绑定。`PAUSED` 不回退 Normal Mode，以免游戏话语被解释为普通指令。

## 2. 为什么是 Mode 而不是 Plugin

普通 Tool 是请求—执行—返回；Game Mode 是跨多轮消息、多个阶段和进程重启的长期运行环境。它拥有 Session 生命周期、参与者、可见性、世界状态、推理空间和自主 Decision Loop。Reminder/Search 等能力不拥有这些边界，也不能表达 DM 的 Session 权限。因此不得设计 `play_script_game()` 或让 LLM 通过 Tool 启动整个游戏。

## 3. Game Runtime 分层

| 模块 | 职责 |
|---|---|
| Session Manager | Session 生命周期、持久化、恢复和状态版本 |
| Participant Registry | QQ、DM、玩家、旁观者和角色映射 |
| Knowledge System | 知识分区、可见性和阶段生效规则 |
| Reasoning Space | 结构化假设、怀疑、冲突和不确定性 |
| Timeline Engine | 时间、地点、Actor、Action 和冲突检测 |
| Decision Engine | Trigger 分级、推理和行动选择 |
| Action Planner | 生成受约束 Action Intent |
| Communication Adapter | QQ 协议与统一消息模型转换 |

## 4. Game Session

`GameSession` 至少包含 `game_id`、`group_id`、DM、玩家列表、当前阶段、Hunter 当前角色、Game State、Timeline 和 Clue State。

Session 生命周期固定为：

```text
CREATED -> RUNNING -> PAUSED -> RUNNING
    |          |          |
    +----------+----------+-> ENDED
```

`ENDED` 不可恢复。重启时以结构化持久状态恢复；无法验证一致性的 Session 安全降级为 `PAUSED`。

## 5. Participant Registry

`Participant` 包含 `qq_id`、`type`、`character`、`public_information`、`statements` 和 `permission`。类型为 `DM`、`PLAYER`、`SPECTATOR`、`UNKNOWN`。昵称不作为安全身份；所有 Session Capability 必须绑定 `game_id + participant_id + action`。

## 6. Knowledge Policy

```text
Knowledge Store -> Visibility Policy -> Context Builder -> Hunter Context
```

| 分区 | 访问者 | 是否进入 Hunter Context | 生命周期 |
|---|---|---|---|
| Public Knowledge | Session 合法参与者 | 按相关性进入 | 单局 |
| Character Private Knowledge | Hunter 与受控 DM 分发面 | 最小必要进入 | 单局 |
| Hidden Truth | DM/受信控制面 | 永不直接进入 | 单局受控 |
| Reasoning Space | Hunter Runtime | 仅结构化摘要 | 单局 |

Hidden Truth 的合法揭示必须生成新的 Public 或 Character Private 条目，不能只靠 Prompt 要求模型“假装不知道”。

## 7. Reasoning Space

推理链为 `Observation -> Internal Reasoning -> Decision -> Disclosure Check -> Public Response`。Reasoning Memory 保存怀疑对象、线索关联、时间冲突、不确定信息、假设、证据引用和披露状态。只保存结论、置信度和依据引用，不保存自由文本式完整 Chain of Thought。

## 8. Agent Decision Loop

```text
Observe -> Understand -> Reason -> Decide -> Act
```

- Observe：接收并验证 Session Event。
- Understand：识别参与者、消息类型、时间、地点和实体。
- Reason：按 Trigger 更新结构化推理。
- Decide：判断是否行动，允许选择 `WAIT`。
- Act：经 Authorization、Audit、Idempotency 和 Disclosure Policy 执行。

## 9. Timeline Engine

`Event` 至少包含 `time`、`location`、`actor` 和 `action`，并保存来源、置信度、可见性和 Evidence 引用。支持精确时间、时间范围、相对顺序和未知时间；检测地点不可能、移动时间不足、陈述冲突和线索冲突。冲突是待推理信号，不等同于自动判定撒谎。

## 10. 信息分类系统

消息先分类为 `Statement`、`Evidence`、`Event`、`Clue`、`Hypothesis` 或 `Background`，再进入对应存储。原始消息只作为短期 Observation 输入，不作为 Game Memory 的永久聊天记录。Hypothesis 只进入 Reasoning Memory，不能提升为事实。

## 11. 推理触发机制

基础 Trigger 为 `NEW_CLUE`、`NEW_STATEMENT`、`CONTRADICTION_FOUND`、`DM_COMMAND`、`PHASE_CHANGE` 和 `PLAYER_QUESTION`。普通、重复或无关发言走快速分类路径；新线索、冲突、阶段变化、直接问题和 DM 要求触发深度推理。连续消息应先聚合，避免逐条深度调用。

## 12. Action Planner

Action 为 `SPEAK_PUBLIC`、`SEND_PRIVATE`、`ASK_DM`、`WAIT` 和 `UPDATE_MEMORY`。Planner 根据阶段、DM 规则、被提问状态、信息价值、披露范围、发言频率和 Adapter 能力选择行动。通道不可用或披露检查失败时只能 `ASK_DM` 或 `WAIT`，不能自动转为公开发送。

## 13. Communication Adapter

第一阶段只启用 QQ 群文本交互；抽象层预留群聊、私聊、语音和图片。Adapter 只负责平台协议转换、能力报告和发送结果，不判断 DM、不读取 Hidden Truth、不修改 Game State。

## 14. 私聊设计

私聊是后续能力，V0.1 明确关闭主动玩家私聊、DM 私聊和临时会话。V0.1 群消息不能承载 Hidden Truth 或 Character Private Knowledge，这些内容必须在 Session 启动前由受信 Setup Control Plane 预置。未来启用私聊时，入站私聊必须唯一解析 `game_id` 并验证 Session 身份；出站私聊必须验证目标 Participant、阶段规则、披露范围和平台能力。失败不得降级为群内公开发送。

## 15. Memory Architecture

Runtime Memory 长期保存 Hunter 人格和风格；Game Memory 单局保存角色、线索、状态、时间线和推理。有效游戏人格为 `Hunter Agent Identity + Session Rules + Game Character Identity`。角色不能写入长期 Identity，否则会造成跨局秘密泄露、人格污染和角色目标残留。

Session 结束 T+0 撤销 Capability、停止 Game Memory 在线检索，并从公开 Session Metadata 和最终公开结果生成约 100 字 `Game Episode Summary Memory`。它只允许包含日期、剧本名称、参与玩家公开身份、Hunter 角色、胜负结果和 MVP；禁止包含凶手身份、私密线索、推理过程、玩家行为评价和隐藏剧情。

T+5 天删除 Game Session、Event Store、Timeline、Clue/Evidence、Reasoning Memory、Character Private Knowledge 和 Hidden Truth。长期只保留 Episode Summary 与 Audit Metadata。Episode Summary 是唯一允许进入长期 Agent Memory 的受控例外，Game Context Builder 不得在新局读取它。

## 16. Agent Runtime 集成

Game Mode 使用 Shared Audit Engine 的 `domain=GAME` namespace，复用 Audit HMAC、epoch、fingerprint、append-only 和 sanitizer；不复制第二套 Audit 系统，也不把 Game Action 注册为 Tool。Authorization、Confirmation、Idempotency、Gateway 协议和 Metadata 概念通过独立 Session Context 适配。DM 权限不能进入 `access_control.py` 的系统管理员判定，Game Memory 不能进入 Companion Memory，Game Context 不能使用 Normal Mode 的通用上下文拼接路径。

## 17. V0.1 MVP

必须支持：Game Session、唯一 DM、玩家映射、Lightweight Hunter 单实例、`GROUP_TEXT` 群文本参与、知识隔离、结构化基础推理和重启恢复。

暂不支持：AI DM、多 AI 角色、独立 Container Runtime、分布式 Agent、多实例调度、主动玩家私聊、DM 私聊、QQ 电话、语音、图片理解、自动判案、跨群游戏和跨局学习。

## 18. 后续路线

演进顺序为：受控私聊和完整 Timeline、Level 3 AI Player、多媒体剧本、多 AI 角色、AI DM、Voice Agent。所有扩展继续保持 Mode、Session 权限、Identity、Knowledge 和 Memory 隔离边界。

## P0 不变量

1. Normal Mode 无行为变化。
2. Game Mode 不是 Tool。
3. DM 不是系统管理员。
4. Hidden Truth 不进入 Hunter Context。
5. Game Memory 不进入长期 Memory；白名单化 Episode Summary 是唯一例外。
6. 不保存完整 Chain of Thought。
7. 不跨 Session 检索或学习；Episode Summary 不作为新局 Game Context。
8. Game Action 必须经过 Authorization、Audit 和 Idempotency。
