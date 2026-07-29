# Game Event Model

状态：P0.1 Architecture Baseline

冻结依据：[Game Mode Architecture Freeze P0.2](game-mode-architecture-freeze.md)

## 1. 目标

GameEvent 是 Game Runtime 的统一输入协议。QQ 消息、DM 控制、阶段变化、线索揭示、Action 反馈、恢复和系统异常必须先转换为 Event，再由 GameSession Actor 顺序消费。Event 是状态推进记录，不是原始聊天归档。

## 2. Game Event Envelope

| 字段 | 语义 | 约束 |
|---|---|---|
| `event_id` | 全局唯一事件 ID | 不可复用，用于去重 |
| `game_id` | 所属 Session | 必填，禁止跨 Session 转发 |
| `event_type` | 事件类型 | 使用受控枚举 |
| `source` | 生产者类别和来源引用 | 不保存敏感原文 |
| `actor` | 发起者 Participant/System 引用 | 未知身份显式标记 UNKNOWN |
| `timestamp` | 事件在来源处发生的时间 | 不等于提交顺序 |
| `payload` | 类型化最小负载 | 必须通过 Schema 和可见性校验 |
| `visibility` | 可见性标签 | PUBLIC/CHARACTER_PRIVATE/DM_CONTROL/SYSTEM_ONLY |
| `state_version` | 生产事件时观察到的 Session 版本 | 用于检测陈旧命令和并发冲突 |
| `correlation_id` | 关联一次输入、推理或 Action 链 | 贯穿 Audit，不承载内容 |

持久化层应另行分配单调递增的 `sequence_no`。`timestamp` 只表示业务时间，不能作为唯一处理顺序。

## 3. Event Type

| Event Type | 生产者 | 主要消费者 | 持久化 | Audit |
|---|---|---|---:|---|
| `MESSAGE_RECEIVED` | Communication Adapter | Actor、分类器 | 最小 Envelope | 来源类型、长度、fingerprint |
| `DM_COMMAND` | DM Command Parser | Session/Phase Controller | 是 | 授权结果、命令类型 |
| `PLAYER_STATEMENT` | Understanding Pipeline | Knowledge/Timeline | 是 | 分类结果、Actor 类型 |
| `CLUE_REVEALED` | 合法 DM 操作或 Phase Rule | Knowledge/Timeline/Decision | 是 | Clue ID、可见性、策略结果 |
| `PHASE_CHANGED` | Phase Controller | Actor、Context、Decision | 是 | from/to、授权结果 |
| `ACTION_REQUESTED` | Action Planner | Action Queue | 是 | Action 类型、目标 scope |
| `ACTION_COMPLETED` | Action Executor | Actor、Decision | 是 | SUCCESS/FAILED/UNKNOWN |
| `SESSION_RECOVERY` | Recovery Coordinator | Actor | 是 | checkpoint、恢复结果 |
| `SYSTEM_ERROR` | Runtime Component | Actor、Failure Policy | 是 | 组件、错误类别、严重度 |

允许后续增加细分类型，但不得通过任意字符串绕过 Schema。新增类型必须明确生产者、消费者、持久化、可见性和失败策略。

## 4. 生产规则

### 4.1 外部消息

Communication Adapter 只产生 `MESSAGE_RECEIVED`，负载包括平台 Event ID、通道、发送者引用、消息类型和短期内容引用。理解层验证 Participant 和语义后，派生 `PLAYER_STATEMENT` 或 `DM_COMMAND`。原始消息不得直接成为事实。

### 4.2 内部事件

Phase Controller、Knowledge System 和 Executor 只能通过 Event 请求状态变化。任何内部模块不得直接更新 Session 聚合根。

### 4.3 派生事件

派生 Event 必须：

- 继承原始 `correlation_id`；
- 使用新的 `event_id`；
- 引用父 Event；
- 重新计算 visibility；
- 携带产生时读取的 `state_version`。

## 5. 消费与提交

Actor 对一个 Event 的处理边界为：

```text
dequeue -> validate -> authorize -> apply -> persist state/event -> commit version -> emit follow-ups
```

只有状态与 Event 同时成功提交后，才能递增 `state_version` 并产生下游事件。校验失败、权限失败和存储失败不得产生部分状态。

## 6. 顺序保证

- 每个 `game_id` 使用独立 Mailbox。
- 入队时分配 Session 内单调递增 `sequence_no`。
- Actor 单次只应用一个 Event。
- 重复 `event_id` 返回已有处理结果，不再次应用。
- `state_version` 不匹配的控制事件按陈旧事件拒绝或重新验证，不能静默覆盖。
- 平台同时到达的玩家消息按 Mailbox 接收顺序处理；业务时间由 Timeline Engine 单独解释。
- 不承诺不同 `game_id` 之间的全局顺序。

## 7. 持久化策略

持久化 Event 应保存结构化语义和来源引用，不保存完整 QQ 消息、Hidden Truth 正文或 Reasoning 正文。`MESSAGE_RECEIVED` 的原始内容只允许存在于短生命周期 Observation Buffer，分类完成或 TTL 到期后删除。Event Store 在 Session 结束后停止在线检索，并在 T+5 天按冻结策略删除。

## 8. Audit 策略

Audit 与 Event Store 目的不同：Event Store 用于恢复 Session；Shared Audit Engine 用于证明权限和执行边界。Game 事件使用 `domain=GAME` namespace，不复制第二套 Audit 系统。Audit 记录：

- Event Type；
- `correlation_id`；
- Actor/Session 安全 fingerprint；
- visibility 分类；
- 授权和校验结果；
- state version before/after；
- 错误类别和耗时。

Audit 不记录 payload 正文、剧本内容、玩家原话或结构化推理内容。

GAME namespace 复用现有持久 HMAC、key epoch、fingerprint、append-only 和 sanitizer。`game_id` 与 `session_id` 作为独立语义字段进入 Audit Envelope；V0.1 二者一一对应。

## 9. 不变量

1. 不存在无 `game_id` 的 GameEvent。
2. Event ID 在重试和恢复时保持稳定。
3. Event 不以来源时间决定提交顺序。
4. `ACTION_COMPLETED=UNKNOWN` 不可解释为 FAILED。
5. 不合法 DM 操作仍形成安全审计，但不形成成功状态事件。
