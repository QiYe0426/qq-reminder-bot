# DeepSeek 余额 QQ 查询命令设计

## 目标

为猎 bot 增加一个无需调用大模型的管理员私聊命令，使管理员可通过 QQ 查询当前 `DEEPSEEK_API_KEY` 所属账户的余额。首版只支持 DeepSeek。

## 用户交互

- 支持完整匹配命令：`查询 AI 余额`、`查询余额`、`AI余额`。
- 仅处理 OneBot 私聊事件；群聊中的同名消息静默忽略，避免在群内暴露账户财务信息。
- 私聊发送者必须属于现有 `BOT_ADMIN_USER_IDS` 管理员集合；未配置管理员或发送者无权限时，复用现有 `admin_denial()` 中文提示。
- 成功响应包含：账户是否可用，以及 DeepSeek 返回的每种币种的总余额、赠金余额、充值余额。示例：

  ```text
  DeepSeek API 余额
  状态：可用
  CNY：总余额 ¥12.34（赠金 ¥2.34，充值 ¥10.00）
  ```

- 响应不得包含 API Key、Authorization 请求头或原始服务端响应。

## 架构

新增独立插件 `plugins/ai_balance.py`，并在 `pyproject.toml` 的 NoneBot 插件列表中注册。插件分为三个可独立测试的边界：

1. `fetch_deepseek_balance()`：读取 `DEEPSEEK_API_KEY` 和 `DEEPSEEK_BASE_URL`，使用现有 `aiohttp` 发起 `GET /user/balance` 请求，并把响应转换为内部结构。
2. `format_deepseek_balance()`：将已校验的余额结构渲染为简洁中文，不接触密钥或网络。
3. NoneBot handler：负责私聊过滤、管理员校验、调用查询函数并回复。

余额 URL 从 `DEEPSEEK_BASE_URL` 派生，默认值为 `https://api.deepseek.com`；若配置尾部为 OpenAI 兼容路径 `/v1`，查询余额时先移除 `/v1`，再追加 `/user/balance`。

## 数据与错误处理

- 请求使用 Bearer Token、`Accept: application/json` 和有限超时；不重试，避免用户一次命令产生不可控的重复请求。
- 未配置 `DEEPSEEK_API_KEY`：回复“DeepSeek API Key 未配置”。
- HTTP 401：回复“DeepSeek API Key 无效或已失效”。
- HTTP 402 或响应表明不可用：明确提示余额不足或账户当前不可用。
- HTTP 429、5xx、超时、网络错误、非 JSON 或响应结构异常：返回稳定的中文错误说明，并让管理员稍后重试。
- 日志只记录错误类别和 HTTP 状态码，不记录密钥、请求头、响应正文或余额明细。
- 金额保留服务端返回的十进制定点字符串；不通过浮点数重新计算。

## 测试

新增 `tests/test_ai_balance.py`，覆盖：

- URL 规范化，包括默认地址、尾部斜杠和 `/v1`。
- 正常的 CNY/USD 单币种及多币种格式化。
- 缺少 API Key、401、402、429、5xx、超时、非法 JSON、字段缺失。
- 私聊管理员可查询；私聊非管理员收到拒绝；群聊静默忽略。
- 错误文本和日志中不出现测试 API Key 或原始敏感响应。

同时更新 `README.md` 的管理员命令说明。实现不新增第三方依赖，不读取、输出或持久化真实密钥。

## 非目标

- 不支持其他 AI 服务商。
- 不提供自动低余额提醒、余额历史、消费统计或控制台页面。
- 不把余额查询注册成 AI Agent 工具，以确保余额耗尽时命令仍可工作。
