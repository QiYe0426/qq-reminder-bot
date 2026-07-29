# Game Mode Architecture Hardening P0.1

状态：P0.1 Baseline / Frozen by P0.2

依赖：[Game Mode P0 Architecture](game-mode-p0-architecture.md)

最终冻结：[Game Mode Architecture Freeze P0.2](game-mode-architecture-freeze.md)。P0.2 已完成人工决策，优先于本文的 Proposed 项。

## 1. 目的

P0 已定义 Mode、Session、权限、知识、推理和 Memory 边界。P0.1 补齐进入工程实现前必须冻结的运行时协议：统一事件、游戏阶段、Context 构建、Agent Instance、Action Queue、并发和故障语义。

## 2. 新增架构模块

| 模块 | 冻结文档 | 解决的问题 |
|---|---|---|
| Game Event Model | [game-event-model.md](game-event-model.md) | 所有 Runtime 输入和内部反馈使用统一 Envelope |
| Game Phase State Machine | [game-state-machine.md](game-state-machine.md) | 分离 Session 可运行状态与游戏内部阶段 |
| Game Context Builder | [game-context-builder.md](game-context-builder.md) | 在 Knowledge 与 LLM 之间实施最小披露 |
| Game Agent Instance | [game-agent-instance.md](game-agent-instance.md) | 单局 Agent 身份、创建、恢复与销毁 |
| Game Action Queue | [game-action-queue.md](game-action-queue.md) | 决策到外部副作用之间的可靠状态机 |
| Concurrency Model | [game-concurrency-model.md](game-concurrency-model.md) | 同局有序、跨局并行、并发冲突收束 |
| Failure Model | [game-failure-model.md](game-failure-model.md) | 明确失败、未知结果和安全降级 |

## 3. 硬化后的 Runtime 数据流

```text
OneBot / Recovery / Internal Signal
                 |
          GameEvent Envelope
                 |
       GameSession Actor Mailbox
                 |
        Sequential Event Apply
                 |
    State + Phase + Knowledge Update
                 |
       Game Context Builder
                 |
      Hunter Agent Instance
                 |
           Action Intent
                 |
          Game Action Queue
                 |
   Authorization / Audit / Idempotency
                 |
             Executor
                 |
        Action Result Event
```

## 4. 冻结决策

### 4.1 Event 是唯一状态推进输入

外部消息、DM 命令、阶段变化、线索揭示、Action Result、恢复和系统异常都必须先规范化为 `GameEvent`。不得从 QQ Handler、LLM 回调或 Executor 直接修改 Session 状态。

### 4.2 单 Session 单 Writer

每个活动 `game_id` 对应一个逻辑 GameSession Actor。只有该 Actor 可以提交该 Session 的状态版本。不同 Session 可以并行，同一 Session 必须按 Mailbox 顺序串行推进。

### 4.3 LLM 不是事实存储

LLM 只接收 Context Builder 生成的 `Hunter Context Package`，并返回受约束的理解、推理更新或 Action Intent。LLM 输出必须验证后才能形成 Event、Knowledge 或 Action。

### 4.4 外部副作用必须排队

公开发言和其他外部副作用不能从 Decision Engine 直接执行。必须生成 Action Intent，进入 Action Queue，在授权、Shared Audit Engine 和幂等校验后执行，并将结果作为新 Event 回送 Actor。私聊仍保留为未来 Action Capability，但 V0.1 不得启用。

### 4.5 UNKNOWN 是终止自动化的安全状态

当外部系统超时且无法确认副作用是否发生时，Action 进入 `UNKNOWN`。Runtime 禁止自动重发，必须等待平台回执、幂等查询或人工处理。

### 4.6 恢复默认安全暂停

恢复只能依据持久化 Event、State Version、Action State 和检查点。任何版本缺口、未决写入或知识分区不一致均使 Session 进入 `PAUSED`，不能猜测性重放。

## 5. 跨文档统一术语

| 术语 | 含义 |
|---|---|
| Session Lifecycle | `CREATED/RUNNING/PAUSED/ENDED`，控制 Runtime 是否运行 |
| Game Phase | `LOBBY/INTRODUCTION/EXPLORATION/DISCUSSION/VOTING/ENDING`，控制游戏内部流程 |
| Observation | 尚未改变权威状态的输入 |
| GameEvent | 已规范化、可排序、可审计的 Runtime 事件 |
| State Version | Session 权威状态成功提交后的单调递增版本 |
| Action Intent | 尚未执行的行动计划 |
| Action Result | Executor 对 Action 的终态或未知状态反馈 |
| Hunter Context Package | Context Builder 生成的最小 LLM 输入包 |

## 6. V0.1 Explicit Non Goals

V0.1 明确禁止：

- 完整剧本自动解析；
- AI DM；
- 多 AI 角色；
- 游戏玩家画像；
- 跨局学习；
- Global Vector Memory；
- 自动判案或自动公布凶手；
- 复杂心理模型；
- 完整 Chain of Thought 存储；
- 将 Game Mode 注册为 Tool；
- 让 DM 获得系统权限；
- 让 Game Memory 进入 Companion Memory；
- 让 Episode Summary 进入新局 Game Context；
- 让 Hidden Truth 进入 Hunter Context。

## 7. Architecture Freeze Gate

进入实现前必须确认：

1. 所有状态变更均能映射到 GameEvent。
2. Session Lifecycle 与 Game Phase 没有混用。
3. Context Builder 对每个输入分区有明确 allow/deny 规则。
4. Agent Instance 不能跨 `game_id` 复用。
5. 所有外部副作用经过 Action Queue。
6. `UNKNOWN` 不会自动重试。
7. 同一 Session 只有一个逻辑 Writer。
8. 持久化失败和恢复失败都会暂停 Session。
9. Normal Mode 路径不读取任何 Game Role 或 Game Memory。
10. 当前文档经人工确认后才进入功能实现。

上述确认由 P0.2 Freeze ADR 完成；实现仍须逐项满足 ADR 的冻结决策。
