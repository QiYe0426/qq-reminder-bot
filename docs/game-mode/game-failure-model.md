# Game Runtime Failure Model

状态：P0.1 Architecture Baseline

冻结依据：[Game Mode Architecture Freeze P0.2](game-mode-architecture-freeze.md)

## 1. 失败原则

- 权限、可见性、状态一致性和持久化失败时 fail closed。
- 外部副作用不确定时使用 UNKNOWN，禁止自动重复。
- 单 Session 故障局部化，不影响其他 Game Session 和 Normal Mode。
- 不能从聊天历史、LLM 输出或猜测重建权威状态。
- 错误处理本身必须隐私安全并可审计。

## 2. 故障分类

| Failure Class | 例子 | Session 影响 | Action 影响 |
|---|---|---|---|
| LLM Failure | timeout、invalid output | 通常保持 RUNNING | 不创建 Action 或重试推理 |
| Communication Failure | QQ 拒绝、timeout | 视严重度保持或暂停 | FAILED/UNKNOWN |
| Storage Failure | Event/State 保存失败 | PAUSED | 禁止后续副作用 |
| Recovery Failure | 版本缺口、知识损坏 | PAUSED | 不恢复调度 |
| Authorization Failure | 非 DM 命令 | 无状态变化 | 拒绝 |
| Policy Failure | Visibility/Phase 冲突 | 可保持 RUNNING；重复则暂停 | 拒绝或取消 |
| Runtime Bug | invariant violation | PAUSED | 未决 Action 收束 |

## 3. LLM Failure

### Timeout

- Reasoning Job 标记临时失败。
- 不修改事实、Reasoning Memory 或 Action Queue。
- 可以按预算重试一次；重试仍使用新的 Job ID，但关联原 `correlation_id`。
- 直接玩家问题可以返回固定、安全、不含推理内容的降级消息；主动推理失败则选择 `WAIT`。

### Invalid Output

- Schema 验证失败，输出整体拒绝。
- 不尝试从部分自由文本提取 Action。
- 可进行一次受约束修复请求；仍失败则记录错误并 `WAIT`。
- 连续失败达到阈值时暂停该 Instance 的自动决策并通知 DM。

### Context Failure

Context Builder 拒绝、Manifest 不一致或 Hidden Truth 命中时，不调用 LLM。记录安全事件；无法构建最小安全 Context 时暂停 Decision Loop。

## 4. Tool / Communication Failure

### 明确失败

平台明确拒绝、目标不存在或会话不可达时，Action 为 `FAILED`。只有 Metadata/Policy 明确声明可重试且能证明副作用未发生时，才允许有限重试。

### 超时或连接中断

如果无法证明消息未发送，Action 为 `UNKNOWN`：

- 不自动重发；
- 不生成同内容替代 Action；
- 等待平台回执、消息查询或人工确认；
- Session 可以继续处理不依赖该 Action 的 Event，但不得假设发言成功。

### 连续平台故障

达到阈值后停止新的外部 Action，Session 转为 `PAUSED`。其他群和 Normal Mode 不受影响。

## 5. Storage Failure

以下写入失败必须暂停 Session：

- Event 与 State 原子提交失败；
- State Version 无法递增；
- Knowledge Visibility 写入失败；
- Action 状态无法持久化；
- 恢复检查点无法保存。

在持久化成功前不得发送依赖该状态的外部消息。若外部副作用已经发生但终态无法保存，Action 按 UNKNOWN 处理并暂停 Session。

## 6. Recovery Failure

恢复校验包括：

- Session/Phase 组合；
- Event 序列连续性；
- State Version；
- Agent Instance、角色和 Policy 绑定；
- Knowledge 分区引用；
- 未决 Action 状态；
- Actor 进程内 ownership 记录。

任一关键校验失败：

1. Session 保持或进入 `PAUSED`。
2. 不创建 ACTIVE Agent Instance。
3. 不重放外部 Action。
4. 生成 `SESSION_RECOVERY` 失败结果和安全 Audit。
5. 向有权限的维护主体报告，不向群公开敏感细节。

## 7. DM Invalid Operation

示例：普通玩家或旁观者发送“结束游戏”。处理顺序：

1. 解析为潜在 DM Command。
2. 根据 QQ ID 解析 Participant。
3. 校验 `game_id`、DM Role、Lifecycle、Phase 和 State Version。
4. 拒绝操作，不修改 Session。
5. Audit 记录 actor fingerprint、命令类型、拒绝原因和版本。
6. 对用户返回最小拒绝信息，不泄露 DM 配置或系统权限。

DM 发起不合法 Phase 跳转同样拒绝；DM 身份不能绕过状态机。

## 8. System Error

`SYSTEM_ERROR` 按严重度处理：

- recoverable：记录并跳过当前非关键推理。
- degraded：禁止主动行动，允许 DM 控制。
- fatal-session：暂停当前 Session。
- fatal-runtime：停止所有 Game Actor，但保持 Normal Mode 是否可用由独立健康检查决定。

错误 Event 不包含用户原文、剧本正文、Hidden Truth 或异常对象中的敏感参数。

## 9. 人工恢复边界

人工操作可以：检查状态版本、确认 UNKNOWN Action、修复存储后恢复、合法结束 Session。人工操作不能：伪造已发生 Event、把 UNKNOWN 直接改为 SUCCESS 而无证据、绕过 Visibility Policy、将 Game Memory 导入长期 Memory。唯一长期输出是按冻结字段生成的 Episode Summary，且不得作为新局 Game Context。

## 10. 不变量

1. Storage/Recovery 一致性失败必定暂停。
2. UNKNOWN 不等于 FAILED，也不自动重试。
3. 非 DM 操作不产生状态变化。
4. 单 Session 故障不传播到其他 Session。
5. 故障日志不记录游戏敏感内容。
