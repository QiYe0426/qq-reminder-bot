# Game Session and Phase State Machine

状态：P0.1 Architecture Baseline

冻结依据：[Game Mode Architecture Freeze P0.2](game-mode-architecture-freeze.md)

## 1. 双状态模型

Session Lifecycle 与 Game Phase 是两个正交状态：

- Session Lifecycle 控制 Runtime 是否允许处理、决策和执行。
- Game Phase 控制游戏内部允许的知识、行为和阶段转换。

把二者合并会导致“暂停游戏”和“进入讨论阶段”无法区分，也会让恢复、权限和 Action Policy 产生歧义。

## 2. Session Lifecycle

```text
CREATED -> RUNNING <-> PAUSED
    |          |          |
    +----------+----------+-> ENDED
```

| 状态 | Event 消费 | LLM 决策 | 外部 Action |
|---|---|---|---|
| CREATED | 只处理配置和合法控制事件 | 禁止 | 禁止 |
| RUNNING | 正常 | 允许 | 允许 |
| PAUSED | 只处理恢复、结束和受控修复 | 禁止 | 仅系统/DM 控制反馈 |
| ENDED | 拒绝新游戏事件 | 禁止 | 禁止 |

`ENDED` 是终态，不允许原地重新开始。

## 3. Game Phase

V0.1 标准阶段：

```text
LOBBY -> INTRODUCTION -> EXPLORATION -> DISCUSSION -> VOTING -> ENDING
```

| Phase | 目的 | 典型允许行为 |
|---|---|---|
| LOBBY | 玩家、角色、DM 和规则准备 | 注册、校验、角色绑定 |
| INTRODUCTION | 公开背景与角色介绍 | 公开介绍、接收初始信息 |
| EXPLORATION | 搜集和揭示线索 | Clue Reveal、提问、基础推理 |
| DISCUSSION | 集中交换信息和推理 | 主动发言、质询、矛盾分析 |
| VOTING | 收束意见 | 受控表态或投票；禁止继续探索 |
| ENDING | 结果说明和清理前复盘 | 公开总结；禁止新推理事实写入 |

这些是 Runtime 标准语义，不要求所有剧本采用完全相同的 UI 名称。自定义显示名必须映射到标准阶段。

## 4. 两个状态的组合约束

| Lifecycle | Phase | 合法性 |
|---|---|---|
| CREATED | LOBBY | 合法默认组合 |
| RUNNING | INTRODUCTION 至 ENDING | 合法 |
| PAUSED | 保留原 Phase | 合法，但 Phase 不推进 |
| ENDED | ENDING | 终态 |
| CREATED | VOTING/ENDING | 非法 |
| RUNNING | LOBBY | 默认非法；启动转换应同时进入 INTRODUCTION |

暂停不会改变 Game Phase；恢复后继续原 Phase。

## 5. Phase 转换规则

| From | To | 发起者 | 前置条件 |
|---|---|---|---|
| LOBBY | INTRODUCTION | DM | Session 可启动、角色和参与者校验通过 |
| INTRODUCTION | EXPLORATION | DM | 初始公开信息完成 |
| EXPLORATION | DISCUSSION | DM | 阶段规则允许 |
| DISCUSSION | EXPLORATION | DM | 规则允许新一轮搜证 |
| DISCUSSION | VOTING | DM | 投票条件满足 |
| VOTING | DISCUSSION | DM | 规则显式允许撤回 |
| VOTING | ENDING | DM | 投票完成或 DM 终止 |
| ENDING | Session ENDED | DM/System | 待处理 Action 已收束或被取消 |

禁止由普通玩家、旁观者、LLM 文本或 Action Executor直接推进 Phase。

进入 Session `ENDED` 后，T+0 生成白名单化 Episode Summary 并停止 Game Memory 在线检索；T+5 天执行 Session、Event、Timeline、Clue、Reasoning、Private Knowledge 和 Hidden Truth 清理。Summary 失败不阻止 Session 结束或 T+5 清理。

## 6. 原子转换

一次 Phase 转换必须原子完成：

1. 校验 DM Session 权限。
2. 校验 Lifecycle、当前 Phase 和 expected `state_version`。
3. 校验目标 Phase 是否在允许转换表中。
4. 应用知识生效/失效规则。
5. 取消不再合法的待执行 Action。
6. 持久化 `PHASE_CHANGED` 和新状态版本。
7. 提交后再触发 Context 重建和推理。

任一步失败都保持旧 Phase。

## 7. Phase Policy

每个 Phase 必须声明：

- 允许的 Event Type；
- 允许的 Action Type；
- 可进入 Context 的知识分区；
- 是否允许深度推理；
- 是否允许主动发言；
- 可见性变化；
- 超时是否只提示 DM，还是允许自动转换。

V0.1 不允许 Runtime 自动推进关键 Phase；定时器只能产生提示或请求 DM 决策。

## 8. 恢复与异常

- 重启后恢复 Lifecycle 和 Phase 两个字段及其版本。
- Phase 缺失、未知或与 Lifecycle 不兼容时进入 `PAUSED`。
- 恢复不得根据最近聊天猜测 Phase。
- 已提交 `PHASE_CHANGED` 但后续 Context 构建失败时保留新 Phase，并暂停 Decision Loop；不得回滚已提交事实。

## 9. 不变量

1. Lifecycle 决定是否运行，Phase 不能越权启动 Runtime。
2. `PAUSED` 保留 Phase。
3. 每次 Phase 转换产生一个可审计 Event。
4. Phase 转换不能绕过 DM Session Authorization。
5. Phase 变化不能修改 Global Runtime Permission。
