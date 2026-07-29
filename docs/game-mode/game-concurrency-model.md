# Game Runtime Concurrency Model

状态：P0.1 Architecture Baseline

冻结依据：[Game Mode Architecture Freeze P0.2](game-mode-architecture-freeze.md)

## 1. 选择：Game Session Actor Model

```text
GameSession Actor
        |
      Mailbox
        |
Sequential Event Processing
        |
Single Session State Writer
```

每个活动 `game_id` 对应一个逻辑 Actor。Actor 是并发所有权模型，不要求固定线程或独立进程。实现可以使用异步任务，但必须保持单 Session 单 Writer 语义。

V0.1 冻结为单进程 Lightweight Actor：不实现独立 Container、分布式 Agent、跨进程 Actor 协调或多实例调度。不同 Session 的“并行”仅指同一进程内的异步并发。

## 2. 并行边界

- 同一游戏：事件顺序处理。
- 不同游戏：可并行处理。
- 同一游戏的 LLM/外部 I/O：可以异步等待，但其结果只能作为新 Event 回到 Mailbox，不能在回调中直接修改状态。
- 共享基础设施：Audit、存储和平台 Adapter 可并发，但必须使用 Session namespace 和幂等保护。

## 3. Mailbox

Mailbox 接收：

- QQ Observation；
- DM Command；
- 派生 Statement/Clue；
- Phase Change 请求；
- Action Result；
- Recovery Event；
- System Error。

入队时完成 Event ID 去重、Session 路由和序号分配。Actor 不接受无 `game_id` 或目标 Session 已结束的事件。

## 4. 玩家同时发言

平台近同时到达的消息按 Mailbox 接收顺序完成语义应用。Timeline 的业务时间仍由 Event `timestamp` 解释。可以在短窗口内将连续 Statement 聚合成一次深度推理，但聚合不能改变原始 Event 顺序或丢失来源。

## 5. DM 推进阶段冲突

DM Command 携带 observed `state_version`。当它排到 Mailbox 时：

- 版本仍匹配：按转换规则执行。
- 版本已变化但操作仍可安全重验：重新验证后执行。
- 操作依赖旧 Phase：拒绝为 stale command，并 Audit。

普通玩家在 Phase Change 前已入队的消息按序处理；在 Phase Change 后入队的消息适用新 Phase Policy。

## 6. Action 执行冲突

Action Intent 记录创建时 Phase 和 State Version。Executor 执行前重新验证：

- Session 是否仍 RUNNING；
- Phase 是否仍允许；
- 目标 Participant 是否仍有效；
- Disclosure Manifest 是否仍有效；
- Action 是否已被 claim 或终结。

Action Result 只能通过 Mailbox 更新 Session，避免 Executor 与 Actor 并发写状态。

## 7. LLM 调用期间的新事件

禁止 Actor 在长时间 LLM 调用期间阻塞整个 Mailbox。推荐流程：

1. Actor 创建带输入 State Version 的 Reasoning Job。
2. Job 异步运行，不持有 Session 写锁。
3. 结果包装为 Event 回到 Mailbox。
4. Actor 检查结果基于的 State Version。
5. 版本过旧且结论受新事件影响时丢弃或重新推理；不得直接覆盖新状态。

V0.1 同一 Instance 同时最多一个深度 Reasoning Job，后续事件可合并为待处理 Trigger。

## 8. Backpressure

Mailbox 必须有容量、优先级和降载策略：

- DM 控制、Recovery 和 System Error 优先于普通 Statement。
- 消息突发时合并低价值推理 Trigger，不丢弃权威 Event。
- 达到硬容量时暂停自动推理和发言，保留控制事件并告警。
- 不允许通过启动无限 LLM 任务消化积压。

## 9. Crash 与 Actor Ownership

- Actor 领取 Session 时建立进程内唯一 ownership 标识。
- V0.1 不实现分布式 lease 或跨进程接管。
- 重启后由 Recovery Coordinator 校验旧 ownership、Event 和 Action 状态，再创建新的 Lightweight Actor。
- Event 和 State 先持久化，再确认消费。
- 无法确定上一个 Actor 是否提交的 Event必须通过 Event ID 和 State Version 去重，不猜测重放。

## 10. 不变量

1. 每个 Session 同时只有一个逻辑状态 Writer。
2. 跨 Session 不要求全局顺序。
3. LLM 和 Executor 回调不直接写 Session。
4. State Version 单调递增。
5. 事件积压只能降级推理，不能绕过权限或丢失控制事件。
