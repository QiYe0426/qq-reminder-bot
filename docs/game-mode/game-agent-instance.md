# Hunter Game Agent Instance Lifecycle

状态：P0.1 Architecture Baseline

冻结依据：[Game Mode Architecture Freeze P0.2](game-mode-architecture-freeze.md)

## 1. 身份层级

```text
Agent Runtime
    |
Hunter Agent Template
    |
Game Session + Character Binding
    |
Hunter Agent Instance
```

Hunter Agent Template 是长期、只读的身份和行为基线；Hunter Agent Instance 是绑定单个 Session 和角色的短期运行实体。

V0.1 冻结为进程内 Lightweight Agent Instance。它是逻辑隔离单元，不是独立 Container、分布式 Agent 或多实例调度单元。

## 2. Template 与 Instance

| 维度 | Hunter Agent Template | Hunter Agent Instance |
|---|---|---|
| 生命周期 | 长期 | 单局 |
| 内容 | 人格、风格、安全规则 | `game_id`、角色、阶段、知识、推理、Action |
| 可变性 | 版本化、受控更新 | 随 Event 推进 |
| Memory | Runtime Memory | Game Memory |
| 复用 | 可作为新实例模板 | 禁止跨局复用 |

有效人格为：

```text
Hunter Agent Identity + Game Policy + Game Character Identity
```

## 3. 创建时机

Session 创建时只建立 Session，不立即创建可运行 Agent Instance。实例创建必须等待：

1. `game_id` 和 `group_id` 已持久化。
2. DM 已验证。
3. Hunter Character 已确定。
4. 角色所需最小知识已提交并通过可见性校验。
5. Game Policy 和 Template Version 已锁定。

DM 发送更多剧本内容不会创建新实例；它产生 Knowledge Event，并由当前 Instance 在下一 State Version 使用。Session 启动前条件不完整时保持 `CREATED/LOBBY`。

## 4. Instance Identity

实例至少绑定：

- `agent_instance_id`；
- `game_id`；
- `character_id`；
- Hunter Template Version；
- Game Policy Version；
- 创建时 State Version；
- Instance Lifecycle State。

任何 Context、Reasoning 或 Action 都必须包含 `agent_instance_id`。

## 5. Instance 生命周期

```text
PROVISIONING -> READY -> ACTIVE <-> SUSPENDED -> TERMINATING -> DESTROYED
       |          |          |
       +----------+----------+-> FAILED
```

| 状态 | 语义 |
|---|---|
| PROVISIONING | 校验角色、知识和策略绑定 |
| READY | 可随 Session 启动激活，不处理普通 Event |
| ACTIVE | 消费 Event、构建 Context、决策和规划 Action |
| SUSPENDED | Session 暂停或运行时故障，不调用 LLM |
| TERMINATING | 停止接收新任务，收束 Action 和清理 Memory |
| DESTROYED | Game Context、Reasoning 和凭证不可再访问 |
| FAILED | 创建或运行不完整，需要人工检查或销毁 |

Session `RUNNING` 对应 Instance `ACTIVE`；Session `PAUSED` 对应 `SUSPENDED`；Session `ENDED` 必须最终对应 `DESTROYED`。

## 6. 恢复

Instance 本身不以进程对象恢复。重启时依据以下结构化记录重建：

- Session 和 Phase；
- Template/Policy Version；
- Character Binding；
- Knowledge Visibility；
- Reasoning Summary；
- 未决 Action 状态；
- 最后已提交 State Version。

旧 LLM 对话、Prompt 和自由文本思考不恢复。缺少任一强绑定时 Instance 进入 `FAILED`，Session 保持 `PAUSED`。

## 7. 销毁

销毁触发：

- Session 正常 `ENDED`；
- Session 被合法取消；
- 安全事件要求终止；
- 长期无法恢复并经授权清理。

销毁流程：停止新 Event 决策、取消 `CREATED` Action、等待或标记执行中 Action、撤销 Session Capability、删除 Context Cache并关闭 Game Memory 在线检索。Character Private、Reasoning、Timeline、Clue、Hidden Truth 和 Event Store 按 T+5 冻结策略清理；长期只保留白名单化 Episode Summary 和 Shared Audit Metadata。

## 8. 为什么不能复用上一局 Instance

- Instance 持有旧 `game_id` 和角色权限。
- Context Cache 可能包含旧局私密知识。
- Reasoning 中包含玩家怀疑和未公开假设。
- Action Queue 可能存在 UNKNOWN 外部副作用。
- Policy 和 Template Version 可能变化。
- 复用会破坏“结束后不可检索”和跨局学习禁令。

新游戏必须创建新 `agent_instance_id`，即使群、玩家、剧本和角色名称完全相同。

## 9. 不变量

1. 一个 Instance 只绑定一个 `game_id` 和一个角色。
2. 一个 V0.1 Session 最多一个 Hunter Instance。
3. Instance 不拥有 Global Runtime Permission。
4. DESTROYED Instance 不可恢复。
5. Instance 数据不写入 Companion Memory。
6. V0.1 不创建独立 Container Runtime，不进行分布式 Agent 或多实例调度。
