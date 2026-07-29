# Game Mode Implementation Placement Proposal

状态：Repository Architecture Mapping / P0.2 Frozen

冻结依据：[Game Mode Architecture Freeze P0.2](game-mode-architecture-freeze.md)
范围：工程放置与开发顺序，不包含功能实现

## 1. 当前仓库架构映射

| 能力 | 当前真实位置 | 当前边界 |
|---|---|---|
| Runtime 启动 | `bot.py`、`pyproject.toml` | NoneBot 从插件列表加载入口 |
| Normal Agent | `plugins/ai_chat.py` | `on_message(priority=20, block=False)`；构建上下文并运行 Agent Tool Loop |
| Message Ingress | 各 NoneBot matcher | 当前没有统一 Mode Router |
| 普通群上下文 | `plugins/group_context_service.py` | 读取采集消息或进程内短期消息 |
| 原始消息采集 | `plugins/message_collector.py`、`plugins/message_archive.py` | Collector 可保存群消息原文和事件数据 |
| 长期 Memory | `plugins/companion_memory.py` | 群友画像、长期记忆、群上下文；不能被 Game Mode 复用 |
| 全局/群功能权限 | `plugins/access_control.py` | 系统管理员、群功能开关和配额 |
| Agent Tool 权限 | `plugins/agent_tool_access.py` | 普通 Tool、群 scope 和目标群管理员权限 |
| Tool Registry/Metadata | `plugins/agent_tools/registry.py` | Agent Tool 注册与 Metadata inventory |
| Tool Gateway | `plugins/agent_tools/gateway.py` | 注册 Tool 的授权、确认、幂等、审计和执行编排 |
| Audit | `plugins/agent_tools/audit.py` | HMAC fingerprint、脱敏和 Tool Audit 持久化 |
| Confirmation | `plugins/agent_tools/confirmation.py` | Tool 确认状态和绑定 |
| Idempotency | `plugins/agent_tools/idempotency.py` | Tool 执行 claim、缓存和 UNKNOWN |
| Tool Policy | `plugins/agent_tools/policy.py` | Metadata/legacy 兼容解析；授权仍有 legacy 边界 |
| 其他消息消费者 | `plugins/group_reactions.py`、`plugins/media_insights.py`、`plugins/remote_approval.py` | 独立 matcher，尚不了解 Mode ownership |

## 2. 主要现状风险

### 2.1 缺少统一 Mode Ownership

当前 `ai_chat.py` 直接消费消息，没有先判断 Runtime Mode。仅增加一个 Game Handler 不足以避免 Normal Agent、消息采集、群反应和媒体流水线同时处理同一游戏消息。

### 2.2 Normal Context 不适合 Game Mode

`build_local_context()` 会组合普通知识、群最近消息、群画像、Companion Memory 和语义图。该路径无法满足 `game_id`、Character Visibility 和 Hidden Truth 边界，Game Mode 必须使用独立 Context Builder。

### 2.3 现有 Archive 会保存原始消息

`message_collector.py` 调用 `message_archive.py` 保存完整群消息相关字段。P0 要求 Game Event 结构化存储且不把消息直接保存为游戏聊天记录，因此活动 Session 消息必须经过明确采集策略，不能默认落入普通长期采集链。

### 2.4 Tool Gateway 不是 Mode Gateway

现有 Gateway 以注册 `AgentTool` 为中心。将 Game Mode 或全部 Game Action 注册成 Tool 会污染 Tool Inventory，并把 Session Actor 生命周期压缩成单次 Tool 调用。Game Action 需要独立 Gateway facade，但应复用现有安全原语和语义。

## 3. 冻结目录边界

核心 Runtime 冻结放在顶层包 `game_runtime/`，而不是 `plugins/agent_tools/`、memory 子模块或单个 Plugin 文件中。Shared Audit Engine 和 Episode Summary 长期 Memory 接口属于共享基础设施：

```text
runtime_core/
└── audit/
    ├── engine.py
    ├── models.py
    ├── fingerprint.py
    └── sanitizer.py

runtime_memory/
└── game_episode.py

game_runtime/
├── __init__.py
├── contracts/
│   ├── events.py
│   ├── actions.py
│   └── context.py
├── session/
│   ├── model.py
│   ├── manager.py
│   ├── actor.py
│   ├── phase.py
│   ├── setup.py
│   └── recovery.py
├── participants/
│   └── registry.py
├── knowledge/
│   ├── store.py
│   ├── visibility.py
│   └── context_builder.py
├── reasoning/
│   ├── state.py
│   ├── timeline.py
│   └── decision.py
├── actions/
│   ├── planner.py
│   ├── queue.py
│   ├── gateway.py
│   └── executor.py
├── security/
│   ├── authorization.py
│   ├── audit.py
│   └── disclosure.py
├── persistence/
│   ├── sessions.py
│   ├── events.py
│   ├── knowledge.py
│   └── actions.py
└── failures.py

plugins/
├── runtime_mode_router.py
└── game_mode_ingress.py
```

边界说明：

- `game_runtime/` 是框架无关的业务 Runtime，不注册 NoneBot matcher。
- `runtime_core/audit/` 是 Tool/Game 共用的 Shared Audit Engine；Tool 与 Game 只提供 domain adapter。
- `runtime_memory/game_episode.py` 只保存冻结字段白名单化的 Episode Summary，不保存 Game State 或推理。
- `plugins/runtime_mode_router.py` 是薄的 Mode ownership 适配器。
- `plugins/game_mode_ingress.py` 是 OneBot/NoneBot 到 GameEvent 的通信适配器和生命周期钩子。
- Game Mode 即使通过薄 Plugin 接入 NoneBot，也不等同于“Game Mode 是 Plugin”；Plugin 只是传输入口。
- `game_runtime/session/setup.py` 是受信 Session Setup Control Plane 的业务边界，用于 Session 启动前预置角色私密信息；它不通过 `GROUP_TEXT` 传输秘密，具体管理界面不属于 V0.1 Communication Adapter。

## 4. 实现影响文件

### 4.1 必须修改

| 文件 | 目的 | 修改边界 |
|---|---|---|
| `pyproject.toml` | 打包 `runtime_core`、`runtime_memory`、`game_runtime`，加载 Router/Ingress | 不改变现有插件顺序之外的业务配置 |
| `plugins/ai_chat.py` | 在进入 Normal Agent 前查询 Mode ownership | 仅增加早期退出，不嵌入 Game 逻辑 |
| `plugins/message_collector.py` | 活动 Game Session 下执行采集隔离策略 | 不修改普通群采集行为 |
| `plugins/group_reactions.py` | Game ownership 下停止普通自动反应 | 不修改 Normal Mode 规则 |
| `plugins/media_insights.py` | V0.1 Game Session 下不启动普通媒体流水线 | 普通群保持现状 |
| `plugins/access_control.py` | 提供全局/群级 Game Mode enable gate | 不保存 DM Role |

### 4.2 可能修改，需实现前验证

| 文件 | 条件 |
|---|---|
| `plugins/remote_approval.py` | 只有其 matcher 会误消费 Game Session 消息时才加 ownership guard；远程审批权限本身不与 Game Mode 合并 |
| `plugins/logging_privacy.py` | 仅当新 Event/Action 日志需要新增结构化脱敏规则 |
| `bot.py` | 通常无需修改；只有启动顺序无法由 `pyproject.toml` 保证时才评估 |
| `plugins/agent_tools/audit.py` | 迁移为 Shared Audit Engine 的 `domain=TOOL` 兼容 adapter；保持现有 Tool Audit API、字段和故障语义 |
| `plugins/agent_tools/idempotency.py` | 优先通过 Game Action adapter 使用同等语义；只有确认可通用化且回归充分时再抽取公共组件 |
| `plugins/agent_tools/confirmation.py` | 仅复用确认协议；不得把 Session Role 塞进 Tool confirmation binding |

## 5. 明确不修改模块

V0.1 默认不修改：

- `plugins/agent_tools/registry.py`：Game Mode 和 Game Action 不进入 Agent Tool inventory。
- `plugins/agent_tools/gateway.py`：不把 Session Runtime 塞入现有 Tool Gateway。
- `plugins/agent_tool_access.py`：DM Role 不进入普通 Tool Capability。
- `plugins/companion_memory.py`：不增加 Game Memory 表、检索或总结；Episode Summary 进入独立 Agent Episode Memory，不进入玩家画像。
- `plugins/message_archive.py`：保留普通消息归档服务；隔离决策在入口/collector 层完成。
- `plugins/reminder.py`、`plugins/reminder_service.py`：不改变提醒业务。
- `plugins/daily_report.py`：不让 Game Mode 改变日报语义。
- `plugins/semantic_graph.py`：Game Event 不进入普通语义图。
- `plugins/admin_console/`：V0.1 不增加管理 UI。

“不修改”不代表无需回归测试；必须证明 Game Mode 激活和关闭时这些模块都不发生越界行为。

## 6. Mode Router 挂载方案

推荐调用关系：

```text
OneBot Event
    |
runtime_mode_router.resolve_ownership(event)
    +-- NORMAL --> existing matchers
    +-- GAME ----> game_mode_ingress -> GameEvent -> Session Actor
    +-- CONTROL --> authenticated control path
```

要求：

- Ownership 解析是只读、快速、确定性的。
- Game Session `RUNNING/PAUSED` 时，Game Mode 对该群的普通消息拥有独占权。
- 不能只依赖 Prompt 告诉 Normal Agent 不响应。
- Router 必须在通用 `on_message` 消费者之前运行。
- 现有低 priority number 的显式管理命令需要逐项审计，防止游戏语句命中普通命令。
- Normal Mode 消息不需要创建 GameEvent，也不承担额外 LLM 或存储成本。

## 7. 安全基础设施依赖

```text
game_runtime.actions.gateway
    |
    +-- Game Session Authorization
    |       +-- access_control global/group allow
    |       +-- Session Role/Phase/Target policy
    |
    +-- GAME Audit Adapter
    |       +-- Shared Audit Engine domain=GAME
    |       +-- shared HMAC/epoch/fingerprint/append-only/sanitizer
    |
    +-- Game Confirmation Adapter
    |       +-- existing confirmation semantics where applicable
    |
    +-- Game Idempotency Adapter
            +-- existing claim/replay/UNKNOWN semantics
```

依赖方向必须是 Game Runtime 和现有 Tool Runtime 分别使用 Shared Audit Engine facade；二者互不依赖。现有 Normal Tool Gateway 不依赖 Game Runtime，Shared Engine 抽取必须通过兼容测试证明 Tool Audit 行为不变。

## 8. 数据依赖与隔离

| 数据域 | 所属 | 禁止依赖 |
|---|---|---|
| Runtime Persona | 现有长期配置/Memory | 不接受 Game Character 回写 |
| Game Session/Event | `game_runtime.persistence` | 不写普通消息 archive；ENDED 后 T+5 删除 |
| Game Knowledge | `game_id` 分区 | 不进入 Companion/Global Vector Memory |
| Reasoning | Agent Instance 分区 | 不进入 Audit 正文或聊天历史 |
| Game Action | Game Action Queue | 不注册为 Agent Tool |
| Episode Summary | `runtime_memory.game_episode` | 仅冻结字段；不供新局 Context 使用 |
| Tool Audit | Shared Audit Engine `domain=TOOL` | 不混入游戏内容 payload |
| Game Audit | Shared Audit Engine `domain=GAME` | 不记录 Game payload 正文 |

## 9. 测试放置建议

```text
tests/game_runtime/
├── test_event_model.py
├── test_session_lifecycle.py
├── test_phase_state_machine.py
├── test_context_visibility.py
├── test_agent_instance.py
├── test_action_queue.py
├── test_concurrency.py
├── test_recovery.py
└── test_failure_model.py

tests/
├── test_runtime_mode_router.py
└── test_game_mode_normal_mode_isolation.py
```

关键回归：

- 无活动 Session 时现有 Normal Mode 行为完全一致。
- 活动 Session 消息不进入 `ai_chat.build_local_context()`。
- 活动 Session 消息不进入 Companion Memory、普通消息采集和语义图。
- DM Role 无法调用系统管理员 Tool。
- Hidden Truth 无法通过 Context Builder、错误日志或 Action 泄露。
- UNKNOWN Action 在恢复后不重发。
- 两个 Session 可并行，同一 Session 保持顺序。

## 10. 推荐开发顺序

1. 只实现 contracts：Event、Lifecycle/Phase、Action 和错误分类。
2. 实现持久化边界与 State Version，不接 QQ、不调用 LLM。
3. 抽取 Shared Audit Engine，以 `domain=TOOL` 兼容测试证明现有 Tool Audit 不变，再增加 `domain=GAME` adapter。
4. 实现 Session Manager、单进程 Lightweight Actor Mailbox 和恢复。
5. 实现 Participant Registry 与 Session Authorization。
6. 实现 Knowledge/Visibility/Context Builder，并用不可泄露测试冻结。
7. 实现 Lightweight Hunter Instance 与结构化 Reasoning/Timeline。
8. 实现 Action Queue、Gateway facade、Shared Audit 和 Idempotency。
9. 实现 Mode Router 和 Game Ingress，先以无 LLM 的受控事件验证消息独占。
10. 接入 Hunter LLM Decision Loop。
11. 增加只支持 `GROUP_TEXT` 的 Communication Executor。
12. 实现 T+0 Episode Summary 和 T+5 清理调度。
13. 运行 Normal Mode 全量回归和故障恢复测试。
14. 生产功能开关默认关闭，另行进行发布设计和灰度验证。

## 11. P0.2 已冻结输入

1. 顶层 `game_runtime/` 加薄 NoneBot Adapter。
2. `RUNNING/PAUSED` 游戏群由 Game Mode 独占消息 ownership。
3. 游戏消息不进入普通采集、Companion Memory 或语义图。
4. Tool/Game 使用同一 Shared Audit Engine，以 domain namespace 隔离。
5. V0.1 使用单进程 Lightweight Instance/Actor，不实现分布式 lease。
6. V0.1 只启用 `GROUP_TEXT`。
7. T+0 生成白名单化 Episode Summary，T+5 删除单局内容，长期保留 Summary 和 Audit Metadata。

实现不得重新解释上述决策；变更必须先新增 ADR。
