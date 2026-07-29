# Game Context Builder

状态：P0.1 Architecture Baseline

冻结依据：[Game Mode Architecture Freeze P0.2](game-mode-architecture-freeze.md)

## 1. 位置与职责

```text
Knowledge Store
      |
Visibility Policy
      |
Game Context Builder
      |
Hunter Context Package
      |
Hunter LLM
```

Context Builder 是 Knowledge Store 与 LLM 之间唯一允许的读取出口。它负责身份绑定、可见性过滤、相关性裁剪、预算控制、来源标注和输出清单生成，不负责生成新事实。

## 2. 为什么不能直接传递 Knowledge Store

- Store 包含不同角色和可见性分区。
- Hidden Truth 可能存在于受控存储，但 Player LLM 无权读取。
- 全量历史会增加泄露、Prompt Injection 和上下文污染风险。
- 过期阶段知识可能被错误使用。
- LLM 无法作为授权执行者；不能让模型自行决定“忽略”不可见数据。
- 恢复后的 Store 可能包含待校验对象，不能自动提升为 Context。

## 3. 输入

Context Builder 输入固定为：

```text
Current Observation
+ Game Character
+ Current Phase
+ Visible Knowledge
+ Reasoning Summary
+ Action Goal
```

附加安全输入：`game_id`、Agent Instance ID、Participant Identity、Session State Version、Policy Version、Context Budget 和目标通道。

## 4. Hunter Context Package

输出是分区明确的包，而不是无结构长 Prompt：

| Section | 内容 |
|---|---|
| Runtime Contract | Hunter 是 AI Player、允许输出类型和禁止事项 |
| Session Scope | `game_id`、Lifecycle、Phase、state version |
| Character | 本局角色、公开身份、私有目标的最小相关部分 |
| Observation | 当前事件的规范化内容 |
| Public Facts | 与当前目标相关的公开事实 |
| Private Facts | Hunter 合法可见的角色私密事实 |
| Timeline Slice | 相关 Event 与冲突摘要 |
| Reasoning Summary | 结构化怀疑、假设和不确定项 |
| Action Goal | 本次任务：理解、推理、回答或规划 |
| Output Contract | 允许的结构、引用和披露限制 |
| Disclosure Manifest | 本次 Context 中允许使用的 Knowledge IDs |

## 5. 构建管线

```text
bind instance -> authorize partitions -> phase filter -> relevance select
-> provenance check -> injection filtering -> budget allocation
-> package assembly -> manifest seal
```

### 5.1 Instance Binding

请求必须绑定 `game_id + agent_instance_id + character_id`。任一不匹配立即拒绝，禁止回退到其他 Session 或长期 Memory。

### 5.2 Visibility Filter

规则先于相关性：

- Public：按 Phase 和 Action Goal 选择。
- Hunter Character Private：仅当前角色可见且已生效的条目。
- Other Character Private：拒绝。
- Hidden Truth：无条件拒绝。
- Reasoning：只允许当前 Instance 的结构化摘要。
- Normal Mode Memory：除稳定 Hunter Persona 外拒绝。

### 5.3 Relevance 与预算

预算按安全优先级分配：当前 Observation、当前 Phase 规则、必要角色信息、直接相关 Evidence、Timeline 冲突、Reasoning Summary、补充背景。预算不足时删除低相关背景，不得通过移除 Visibility 标记扩大内容。

### 5.4 Provenance

进入 Context 的事实必须具有来源、可见性、状态和生效阶段。来源不明、被撤销、相互冲突但未标注的知识不得以确定事实形式进入。

## 6. Hidden Truth 防泄露

防线包括：

1. 数据分区：Hidden Truth 与可见知识分开存储。
2. API 边界：Builder 查询接口不提供 Hidden Truth 返回类型。
3. Policy：任何 Hidden Truth 命中均 fail closed。
4. Manifest：输出只允许引用 Context Manifest 中的 Knowledge ID。
5. Disclosure Check：Action 发送前再次核对引用和内容。
6. Audit：记录拒绝类型，不记录被拒绝内容。

如果 DM 合法揭示某条真相，Knowledge System 必须创建新的可见条目；不能修改查询条件让 Hunter 读取原 Hidden Truth 对象。

## 7. Prompt Injection 边界

玩家陈述、DM 提供的剧本文本和线索都属于不可信内容。Context Package 必须把它们标为 data，而非 Runtime Instruction。只有经认证的 DM Command 和版本化 Runtime Policy 可以改变阶段或权限。

## 8. Reasoning Summary

只包含结构化状态：对象、置信度、Evidence 引用、冲突类型、未决问题和是否已公开。不包含模型逐步思考文本。Summary 必须按当前 Action Goal 裁剪，不能每轮注入完整推理历史。

## 9. 缓存与恢复

- Context Package 是派生数据，不是权威状态。
- 缓存键必须包含 `game_id + agent_instance_id + state_version + action_goal + policy_version`。
- State Version 或可见性变化后缓存立即失效。
- 重启后从结构化状态重建，禁止恢复旧 Prompt 文本。

## 10. 与 Normal Mode 的隔离

Game Context Builder 不调用 Normal Mode 的通用 `build_local_context()`，不读取群画像、Companion Memory、User/Semantic Memory、普通群历史、普通语义图或历史 Episode Summary。长期 Hunter Persona 可以通过只读模板注入，但模板不得包含任何 Game Session 内容。

V0.1 Context 的目标通道固定为 `GROUP_TEXT`。DM 私聊、玩家私聊、语音和图片 Capability 均为 unavailable，Builder 不为这些通道生成 Context Package。

## 11. 不变量

1. Hunter LLM 只能通过 Context Builder 读取 Game Knowledge。
2. Hidden Truth 永不出现在 Context Package。
3. Context Package 只属于一个 `game_id` 和 Agent Instance。
4. Context 不作为恢复事实来源。
5. LLM 输出不能扩大 Context Manifest。
6. Episode Summary 不能作为新 Session 的事实、关系、怀疑或角色上下文。
