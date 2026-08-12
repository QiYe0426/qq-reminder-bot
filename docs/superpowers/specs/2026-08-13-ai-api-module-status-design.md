# AI API 模块与 Qwen 状态查询设计

## 目标

扩展现有管理员私聊命令 `查询 AI 余额` / `查询余额` / `AI余额`：在 DeepSeek 余额之外标明各 API 当前负责的 bot 模块，并加入 Qwen（阿里云百炼）的可用性、欠费和鉴权状态查询。

## 查询输出

响应标题改为 `AI API 状态`，依次展示：

1. **DeepSeek**
   - 负责模块：AI 对话、Agent 工具调度、联网决策、陪伴画像；当日报未单独配置 `SUMMARY_API_KEY` 时也负责日报总结。
   - 展示账户可用状态，以及 CNY/USD 的总余额、赠金余额和充值余额。
2. **Qwen / 阿里云百炼**
   - 负责模块：媒体图片识别；关键词回怼图片文字检测使用相同 Key 时合并标注，使用独立 Key 时单独展示一次探测结果。
   - 展示配置的模型名称，以及 `可用`、`欠费`、`Key 无效`、`限流`、`服务异常`、`未配置` 中的一种状态。
   - 不声称可读取 Qwen 人民币余额。DashScope API Key 没有账户余额查询接口；查询真实阿里云余额需要额外的 Billing OpenAPI AccessKey，本功能不引入该高权限凭证。

示例：

```text
AI API 状态

DeepSeek
负责模块：AI 对话、Agent 工具调度、联网决策、陪伴画像、日报总结（回退配置）
状态：可用
CNY：总余额 ¥12.34（赠金 ¥2.34，充值 ¥10.00）

Qwen / 阿里云百炼
负责模块：媒体图片识别、关键词回怼图片文字检测
模型：qwen3-vl-flash
状态：欠费
说明：账户欠费，请前往阿里云充值
```

## Qwen 配置发现与去重

插件读取与现有运行链路完全一致的配置优先级：

- 媒体图片识别：`IMAGE_VISION_API_KEY` / `IMAGE_VISION_BASE_URL` / `IMAGE_VISION_MODEL`，Key 和 URL 分别回退到 `OPENAI_API_KEY` / `OPENAI_BASE_URL`。
- 关键词回怼图片检测：`KEYWORD_RETORT_IMAGE_SCAN_API_KEY` / `KEYWORD_RETORT_IMAGE_SCAN_BASE_URL` / `KEYWORD_RETORT_IMAGE_SCAN_MODEL`，依次回退到图片识别配置和 OpenAI 兼容配置。

两个模块解析出的 Key、服务地址和模型都相同时，只发送一次探测请求并合并模块标签；任一项不同则分别展示，避免用一个账户状态误代表另一个配置。Key 仅在内存中用于请求和等值比较，不记录、持久化或输出。

## Qwen 主动探测

使用现有 `aiohttp` 向配置的 OpenAI 兼容 `chat/completions` 地址发送最小非流式请求：一个短文本输入、`max_tokens=1`、10 秒总超时。探测使用配置中的实际 Qwen 模型，因此可同时验证 Key、账户状态、服务地址和模型调用链。

- HTTP 200：`可用`。
- 阿里云错误码 `Arrearage` 或对应欠费语义：`欠费`。
- HTTP 401/403 或鉴权错误码：`Key 无效`。
- HTTP 429 或限流错误码：`限流`。
- 超时、网络错误、5xx、非法或未知响应：`服务异常`，附稳定中文建议。
- 缺少 Key、URL 或模型：`未配置`，不发送请求。

一次成功探测最多生成 1 token，会产生极低调用费用；命令响应明确提示这一点。探测不自动重试。

## 安全与权限

- 继续仅允许 `BOT_ADMIN_USER_IDS` 中的管理员私聊使用；群聊静默忽略。
- 回复和日志均不得出现 API Key、Authorization 请求头、原始响应正文、请求 ID或未经筛选的服务端错误文本。
- 日志只记录供应商、模块标签、HTTP 状态和归一化错误分类。
- DeepSeek 查询失败不阻止 Qwen 查询，反之亦然；最终响应始终分别展示每个已发现服务的状态。

## 代码结构

继续使用 `plugins/ai_balance.py`，但把供应商结果表示和总报告组合拆成纯函数：

- DeepSeek：保留现有余额请求与格式化逻辑，增加模块标签。
- Qwen：新增配置解析、探测 URL 生成、主动探测与安全状态格式化。
- 总报告：并发查询 DeepSeek 和所有去重后的 Qwen 配置，按固定顺序组合；单个供应商错误转换为该供应商状态段。
- NoneBot matcher、管理员校验和命令别名保持不变。

## 测试与文档

扩展 `tests/test_ai_balance.py`，覆盖模块标签、Qwen 配置回退/去重、请求体的 `max_tokens=1`、可用、欠费、无效 Key、限流、超时、网络错误、服务异常、供应商部分失败，以及密钥和原始错误不泄露。更新 README，说明命令现会查询 DeepSeek 余额和 Qwen 在线状态，Qwen 探测有极低 token 消耗但不返回人民币余额。

## 非目标

- 不配置阿里云 Billing OpenAPI AccessKey，不查询阿里云人民币账户余额。
- 不增加自动定时检查、低余额告警或历史记录。
- 不查询语音转写或用户自定义的其他 OpenAI 兼容服务。
