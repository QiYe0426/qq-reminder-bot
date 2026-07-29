# Game Action Queue

状态：P0.1 Architecture Baseline

冻结依据：[Game Mode Architecture Freeze P0.2](game-mode-architecture-freeze.md)

## 1. 目标

Decision Engine 不能直接产生外部副作用。Action Queue 在决策和执行之间提供授权、审计、幂等、顺序、恢复和未知结果处理。

```text
Decision -> Action Intent -> Action Queue -> Executor -> Action Result Event
```

## 2. Action Intent

Action Intent 至少包含：

- `action_id`；
- `game_id`；
- `agent_instance_id`；
- Action Type；
- 目标 Participant/Channel；
- 内容引用或 Memory 更新计划；
- 来源 Event 和 `correlation_id`；
- 创建时 `state_version` 与 Phase；
- Visibility/Disclosure Manifest；
- Authorization、Confirmation 和 Idempotency Policy；
- 过期时间和优先级。

Action Payload 不应复制完整 Knowledge Store 或 Reasoning 内容。

## 3. 状态机

```text
CREATED -> EXECUTING -> SUCCESS
    |          |
    |          +------> FAILED
    |          +------> UNKNOWN
    +-----------------> FAILED/CANCELLED
```

用户要求的核心状态为 `CREATED`、`EXECUTING`、`SUCCESS`、`FAILED`、`UNKNOWN`；架构允许 `CANCELLED` 作为未执行 Action 的收束状态。

| 状态 | 语义 | 是否自动重试 |
|---|---|---:|
| CREATED | 已持久化，尚未执行 | 仅按 Policy 调度 |
| EXECUTING | 已获得执行 claim | 禁止并发执行 |
| SUCCESS | 已确认副作用成功 | 否 |
| FAILED | 已确认未成功或执行前失败 | 仅显式可重试错误 |
| UNKNOWN | 副作用可能已经发生 | 禁止 |
| CANCELLED | 执行前因 Phase/Session 变化取消 | 否 |

## 4. 执行顺序

1. Actor 产生 `ACTION_REQUESTED`。
2. Queue 持久化 Action Intent 和幂等键。
3. Executor 重新验证 Session、Phase、Authorization 和 Disclosure。
4. 写入执行开始 Audit；高风险写入失败则 fail closed。
5. 原子 claim，状态变为 `EXECUTING`。
6. 执行平台或内部副作用。
7. 持久化 `SUCCESS`、`FAILED` 或 `UNKNOWN`。
8. 产生 `ACTION_COMPLETED`，回送原 Session Mailbox。

## 5. Idempotency

幂等键至少包含：

```text
game_id + action_id + action_type + target_scope + canonical_payload_fingerprint
```

同一 Action 的进程重试保留 `action_id`。新的决策即使文本相同，也必须使用新 Action ID，但 Planner 应先检查是否与已有未终结 Action 重复。

## 6. 为什么必须有 UNKNOWN

QQ 发送可能出现：请求已到达平台，但客户端在收到回执前超时。此时：

- 标记 FAILED 会允许自动重发，可能造成重复发言；
- 标记 SUCCESS 又没有证据；
- 回滚无法撤销已经发出的消息。

因此进入 `UNKNOWN`，禁止自动重试。只有平台回执、可验证消息 ID、幂等查询或人工处置可以收束 UNKNOWN。

## 7. Phase 与 Session 变化

- `CREATED` Action 在执行前必须重新验证 Phase。
- Session `PAUSED` 时停止普通 Action 调度。
- Session `ENDED` 时取消所有尚未执行 Action。
- 已 `EXECUTING` 的外部 Action 不能假设可取消；其结果仍须收束为 SUCCESS/FAILED/UNKNOWN。
- 旧 Phase 创建的发言 Action 不得在新 Phase 自动发送。

## 8. Action 类型策略

| Action | 外部副作用 | 失败策略 |
|---|---:|---|
| SPEAK_PUBLIC | 是 | 超时可能 UNKNOWN |
| SEND_PRIVATE | 是 | 不可达可 FAILED；发送超时可能 UNKNOWN |
| ASK_DM | 是 | 与消息发送相同 |
| WAIT | 否 | 可直接 SUCCESS，不进入外部 Executor |
| UPDATE_MEMORY | 内部写入 | 存储失败使 Session 暂停 |

V0.1 只启用 `GROUP_TEXT`：`SPEAK_PUBLIC` 和群内 `ASK_DM` 可执行；`SEND_PRIVATE` 保留在抽象模型中但 Capability 必须为 unavailable，Planner 不得创建可执行私聊 Action。

## 9. Audit

通过 Shared Audit Engine 的 `domain=GAME` namespace 记录 Action Type、目标 scope fingerprint、授权结果、幂等状态、执行阶段、结果、错误类别和耗时。禁止记录发送正文、Hidden Truth、Private Knowledge 或 Reasoning 正文。

## 10. 不变量

1. 外部 Action 不绕过 Queue。
2. Authorization 在规划和执行时都校验。
3. UNKNOWN 不自动重试。
4. Action Result 必须回送为 Event。
5. 不同 Session 的 Action 不共享幂等命名空间。
