# 猎bot版本日志

## v2.0.0 - 猎bot-初具人形

发布日期：2026-07-02

### 版本定位

这一版把猎bot从“功能插件集合”整理成“用户消息 -> AI agent -> 受控工具 -> 回复/执行”的雏形。猎宝开始具备人设、长期记忆、群画像、知识库、提醒和常数报时工具、日报生成、控制台运维和分群权限管理，因此命名为“猎bot-初具人形”。

### 新增和整理

- AI agent：
  - `plugins/ai_chat.py` 默认启用工具调用 agent。
  - agent 可调用 `web_search`、`fetch_url`、`create_reminder`、`list_reminders`、`cancel_reminder`、`get_chime`、`set_chime` 和 `respond`。
  - 提醒和常数报时核心逻辑分别下沉到 `plugins/reminder_service.py` 和 `plugins/chime_service.py`，命令入口与 AI 工具复用同一套业务代码。
- 猎宝控制台：
  - 新控制台固定为 `/hunterbot/admin-console`，旧 `/hunterbot/companion-admin` 已移除。
  - 控制台包含 Bot 人设、STS2 知识库、群管理、智能陪伴、群画像、群友画像管理和消息采集记录。
  - `日报` 标签下新增 `自动发送日报` 开关；开启日报后才可操作。
  - `智能陪伴附加功能` 下的 `调戏其他bot` 和 `🎒常数回怼` 可配置每分钟、每小时、每天触发上限，并显示本分钟、每小时、每天已使用次数。
- 智能陪伴和群管理：
  - 陪伴画像记录对象完全改为控制台选择，旧群内自助注册/退出/更新/重置/删除流程只保留提示，不再写入记录对象。
  - 支持群画像，AI 回复可读取当前群的群性质和回复参考。
  - 可把群友标记为其他 bot，并配置关键词，配合 `调戏其他bot` 做限流短回复。
- 常数相关功能：
  - 常数报时支持每小时 58 分和每天两次两种模式。
  - `🎒常数回怼` 支持文本和图片中的阿拉伯数字 `158` 检测，图片检测复用视觉模型配置。
  - 回怼图片以 base64 消息发送，避免服务器本地路径被 NapCat 拒读。
- 日报：
  - 自动日报可跟随控制台每群 `自动发送日报` 开关；`DAILY_REPORT_GROUP_IDS` 只在显式配置时作为强制白名单。
  - 自动生成日报时会私聊提醒管理员开始、跳过、失败和完成状态。
  - 启动补跑逻辑通过 `DAILY_REPORT_STARTUP_GRACE_MINUTES` 控制，并记录已发送日期避免重复发送。
- STS2 知识库：
  - 控制台只保留 STS2 入口，旧 STS1 种子和入口已清理。
  - `sts_knowledge_seed.py` 可重建事实库和自建攻略库。
  - 自建攻略源纳入 `knowledge_sources/sts2_guides/main_1.tex`。
- 部署：
  - 服务器后续改为干净 Git clone + `git pull --ff-only` 更新。
  - `.env.local`、`data/`、`.venv/` 仍只保存在服务器，不提交 GitHub。
  - 新增 `deploy/nginx/hunterbot-admin-console.conf` 作为控制台反向代理配置参考。

### 破坏性变更

- 删除 `plugins/companion_admin.py` 旧控制台插件。
- 删除旧控制台的文字提取入口；知识条目现在通过控制台编辑或 `sts_knowledge_seed.py` 重建。
- 服务器如果曾用手工上传 zip 更新，需要整理成干净 Git clone，并先备份 `.env.local`、`data/` 和运行数据库。

## v1.2.0-dev（历史开发阶段）

开发分支：`feature/knowledge-base`

### 版本定位

这一阶段合并群日报素材识别、日报生成、陪伴画像、管理页和本地知识库能力，让猎bot同时具备“群信息整理”和“长期陪伴记忆”两条工作流。

### 新增功能

- 陪伴画像注册：
  - 管理员可通过 `开启群功能 陪伴画像` / `关闭群功能 陪伴画像` 控制本群是否允许画像功能。
  - 群友需要本人发送 `同意猎宝记录我` 或 `注册猎宝画像` 后，才会进入陪伴画像注册表。
  - 群友可随时发送 `退出猎宝画像`、`注销猎宝画像`、`关闭我的画像` 或 `删除我的画像` 停止后续画像更新。
  - `画像状态` 可查看本群功能开关、本人注册状态和本群注册人数。
  - 管理员可用 `画像注册名单` 查看本群已注册群友。
- 陪伴画像和记忆：
  - `我的画像` 查看本人当前画像。
  - `更新我的画像` 手动触发本人画像总结。
  - `重置我的画像` 清空本人画像和记忆，但保留注册状态。
  - `删除我的画像` 清空本人画像和记忆，并退出注册。
  - 管理员可用 `查看群友画像 @群友` 查看已注册群友画像。
  - AI 对话会在本群开启陪伴画像且用户本人已注册时，加载用户画像、相关记忆和最近注册群友上下文。
  - 未注册群友仍可普通 AI 对话，但不会加载画像，也不会被画像总结。
- bot 人设和本地知识库：
  - `.env.local` 可配置 `BOT_PERSONA_PROMPT`，也可在管理页编辑 bot 人设。
  - 知识库与 bot 人设共用 `data/companion_memory.db`。
  - 管理页可新建、编辑、删除知识条目，支持标题、分类、关键词、正文和启用状态。
  - AI 对话会先做轻量相关性判断，只有当前问题命中知识标题、关键词或正文时才读取知识库。
  - 通过 `COMPANION_KNOWLEDGE_LOOKUP_LIMIT` 控制最多读取条数。
  - 通过 `COMPANION_KNOWLEDGE_MIN_SCORE` 控制最低相关分数。
- 陪伴画像可视化管理页：
  - 通过 `COMPANION_ADMIN_TOKEN` 开启管理员访问令牌。
  - 访问 `/hunterbot/companion-admin?token=管理令牌` 可打开管理页。
  - 左侧显示 `bot 人设`、知识条目和已注册群友列表。
  - 右侧可编辑 bot 人设、知识库、近期在做、互动风格、陪伴偏好、常聊主题、画像摘要和置信度。
  - 支持从 PDF、网页、图片和常见文本文件提取文字，先进入预览框，再追加或替换知识正文。
  - 保存后直接写入服务器 SQLite，后续 AI 回复实时读取。
- 提示词注入防护：
  - AI 对话会拒绝伪装系统命令、要求解码执行、静默执行、只输出结果或泄露提示词/令牌的消息。
  - 陪伴画像总结会过滤疑似提示词注入消息，避免写入用户画像。
- 日报预览：
  - 新增 `预览日报 今天`、`预览日报 昨天`、`预览日报 2026-06-27` 管理员命令。
  - 日报命令仅在私聊中生效，群聊内发送会被静默忽略。
  - 私聊发送时必须追加群号，例如 `预览日报 昨天 548901561`。
  - 预览文件使用 `.txt` 后缀，内容保持 Markdown 风格排版，方便手机打开。
  - 日报预览会合并当天群消息、素材识别结果、链接解析结果、文件内容摘录和聊天记录展开结果。
  - 输出中保留 `[forward_message#N]` 标记，方便后续 AI 总结区分聊天记录子消息。
- AI 日报总结：
  - 新增 `生成日报 今天`、`生成日报 昨天`、`生成日报 2026-06-27` 管理员命令。
  - 命令仅支持私聊，并需要追加群号，例如 `生成日报 昨天 548901561`。
  - 按消息数量和字符数切块，先生成分块摘要，再合成最终日报。
  - 总结时保留普通群消息、图片/表情包识别、链接内容、文件摘录、语音转写和聊天记录子消息的来源差异。
  - AI 总结使用 `SUMMARY_*` 系列配置，不影响常规 AI Chat。
- PDF 与定时发送：
  - 生成日报时保存 Markdown 到 `data/reports/`，并向管理员发送 PDF。
  - PDF 文件名使用 `2026年1月1日 群名的日报_群号.pdf` 格式。
  - PDF 输出会清理 `**加粗**` 等 Markdown 标记，并使用更清晰的标题层级。
  - PDF 内不再重复显示日期等文件名已包含的信息。
  - 新增 `DAILY_REPORT_ENABLED`、`DAILY_REPORT_SEND_TIME` 定时发送配置。
  - 自动日报会每天 04:00 为已开启“消息采集”的群生成前一天 04:00 到当天 04:00 的日报，并私聊发送给管理员。
  - 新增私聊 `测试日报 昨天 群号` 管理员命令，用于手动测试自动发送路径。
- 表情包识别：
  - 支持 `mface` 表情包消息段。
  - 支持普通 QQ `face` 表情记录。
  - 图片类表情包会走视觉模型识别。
  - 无图片 URL 的表情会保留表情 ID 或摘要。
- 聊天记录识别：
  - 支持识别合并转发 `forward` 消息段。
  - 通过 OneBot/NapCat 的 `get_forward_msg` 接口尝试展开聊天记录。
  - 展开后按发送人、时间和消息内容写入 `message_insights`。
  - 展开结果会在 `raw_result.messages[]` 中保留结构化子消息，并标记 `source=forward` 和 `is_forward_message=true`，方便后续日报区分聊天记录里的消息。
- 链接识别：
  - 从普通文本、JSON 分享卡片和 XML 分享卡片中提取 URL。
  - 尝试抓取网页标题、描述和正文摘录。
  - 对 B站、小红书、微信公众号等来源先走通用网页解析；页面不可访问时尽量保留 QQ 卡片里的标题/摘要。
  - 抓取前会校验 URL，跳过本机、内网、非公网地址。
- 自动处理增强：
  - 新采集消息触发自动识别时，会处理图片/表情包、链接、文件、语音、回复引用、视频、位置、分享卡片、名片、戳一戳、音乐、匿名消息和聊天记录。
  - `扫描媒体识别` 会显示图片/表情包、链接、文件、语音和聊天记录的处理结果。

### 配置变更

- `.env.example` 新增：
  - `LINK_FETCH_TIMEOUT_SECONDS`
  - `LINK_FETCH_MAX_BYTES`
  - `SUMMARY_API_KEY`
  - `SUMMARY_BASE_URL`
  - `SUMMARY_TIMEOUT_SECONDS`
  - `SUMMARY_CHUNK_MESSAGES`
  - `SUMMARY_MAX_INPUT_CHARS`
  - `DAILY_REPORT_ENABLED`
  - `DAILY_REPORT_SEND_TIME`
  - `DAILY_REPORT_TIMEZONE`
  - `VOICE_TRANSCRIBE_ENABLED`
  - `VOICE_TRANSCRIBE_MODEL`
  - `VOICE_TRANSCRIBE_API_KEY`
  - `VOICE_TRANSCRIBE_BASE_URL`
  - `VOICE_TRANSCRIBE_TIMEOUT_SECONDS`

### 技术变更

- 新增 `plugins/daily_report.py`：
  - 从 `collected_messages` 和 `message_insights` 按群、按日期取数。
  - 日报统计窗口从自然日改为每日 04:00 到次日 04:00。
  - 生成 txt 文件格式日报预览，正文保持 Markdown 风格排版。
  - 调用 OpenAI 兼容接口进行分块总结和最终总结。
  - 使用 ReportLab 生成 PDF；发送侧只发送 PDF，不再发送 Markdown 文件。
  - 使用 APScheduler 按已开启消息采集的群定时发送日报。
  - 复用现有 txt 文件导出和 NapCat 文件上传能力。
- 新增 `plugins/media_insights.py`：
  - 文件消息会按类型做风险提示，文本/文档类会尽量读取正文摘录。
  - 语音消息预留转写入口，开启后可把语音转成日报可用文本。
  - 回复、视频、位置、分享卡片、名片、戳一戳、音乐和匿名消息都会进入总结输入。
- 新增 `plugins/companion_registry.py`。
- 新增 `plugins/companion_memory.py`。
- 新增 `plugins/companion_admin.py`。
- 新增 SQLite 数据库 `data/companion_memory.db`，保存陪伴画像注册、画像摘要、记忆、人设和知识库。
- 分群功能开关新增 `陪伴画像`，默认关闭。

## v1.1.0

发布日期：2026-06-27

### 版本定位

这一版把猎bot从单群提醒 bot 扩展为可按群管理的 QQ 助手，新增消息采集、存储状态、媒体识别和自动图片识别，为后续群日报总结打基础。

### 新增功能

- 新增管理员权限体系：
  - 通过 `.env.local` 的 `BOT_ADMIN_USER_IDS` 配置管理员 QQ。
  - 兼容旧配置 `BOT_OWNER_USER_IDS` 和 `OWNER_USER_IDS`。
  - 管理员提示文案统一使用“管理员”。
- 新增分群功能开关：
  - 新群默认保留 AI 对话、`ping`、`help`、`群功能状态`。
  - 提醒和消息采集默认关闭，需要管理员按群开启。
  - 支持 `开启群功能 提醒`、`关闭群功能 提醒`。
  - 支持 `开启群功能 消息采集`、`关闭群功能 消息采集`。
- 常数报时增强：
  - `启用/开启🎒常数报时`、`关闭🎒常数报时` 改为管理员命令。
  - 支持每小时 58 分报时。
  - 支持每天两次报时：凌晨1:58和下午1:58。
  - 常数报时文案改为 12 小时制。
  - `群功能状态` 会显示 🎒常数报时状态。
- 新增指定群消息采集：
  - 开启消息采集后，实时保存该群消息。
  - 保存发送人、时间、消息段类型、可读文本、原始消息结构和事件 JSON。
  - 保留 `@`、图片、语音、视频、回复、聊天记录、JSON/XML 等消息段信息。
  - 群内 AI Chat 的用户提问和 bot 回复都会写入采集库。
  - 管理员命令通常不进入采集库。
- 新增采集查询和导出：
  - `采集状态` 查看当前群或全局采集数量。
  - `查看采集 数字` 导出最近采集消息 txt，数字范围 1-50。
  - 群内导出只包含当前群，不会窜群。
  - 导出内容按时间顺序排列。
  - txt 文件通过 bot 的本地 HTTP 路由辅助上传给 NapCat。
- 新增存储状态命令：
  - `存储状态` / `硬盘状态` / `空间状态` 查看服务器磁盘和数据占用。
  - 显示消息数据库、提醒数据库、功能设置库、导出目录、日报目录和媒体缓存目录占用。
- 新增媒体识别实验模块：
  - 通过 `MEDIA_INSIGHTS_ENABLED=1` 开启。
  - 通过 `MEDIA_INSIGHTS_AUTO_ENABLED=1` 开启新图片自动识别。
  - 通过 `IMAGE_VISION_ENABLED=1` 控制是否真正调用视觉模型。
  - 支持 OpenAI 兼容视觉接口，默认示例使用 Qwen `qwen-vl-plus`。
  - 图片直接使用 QQ 图片 URL 交给视觉模型，不在服务器本地下载图片。
  - 识别结果写入 `message_insights`，并在 `查看采集` 导出的 txt 中展示。
  - `媒体识别状态` 查看识别开关、模型、密钥状态和识别记录。
  - `扫描媒体识别` 用于补扫历史消息或手动重试 pending 图片。

### 配置变更

- `.env.example` 新增：
  - `BOT_ADMIN_USER_IDS`
  - `MEDIA_INSIGHTS_ENABLED`
  - `MEDIA_INSIGHTS_AUTO_ENABLED`
  - `MEDIA_INSIGHTS_BATCH_SIZE`
  - `IMAGE_VISION_ENABLED`
  - `IMAGE_VISION_MODEL`
  - `IMAGE_VISION_API_KEY`
  - `IMAGE_VISION_BASE_URL`
  - `IMAGE_VISION_TIMEOUT_SECONDS`
  - `SUMMARY_MODEL`
- 新增 SQLite 数据库：
  - `data/bot_settings.db`：分群功能开关。
  - `data/message_archive.db`：群消息采集和媒体识别记录。

### 技术变更

- 新增插件：
  - `plugins/access_control.py`
  - `plugins/message_archive.py`
  - `plugins/message_collector.py`
  - `plugins/storage_status.py`
  - `plugins/media_insights.py`
- `plugins/ai_chat.py` 会在群消息采集开启时同步写入 AI 回复。
- `plugins/reminder.py` 接入管理员权限和分群功能开关。
- `pyproject.toml` 注册全部插件并将版本号更新到 `1.1.0`。

## v1.0.0

发布日期：2026-06-20

### 版本定位

第一个可用版本，跑通 QQ bot 的基础链路：QQ 登录、消息接收、功能处理、消息回复、服务器常驻运行。

### 已完成功能

- `ping` 在线检测。
- `help` 简洁菜单。
- 一次性提醒：
  - 指定日期时间：`提醒 2026-06-20 09:00 喝水`
  - 仅输入时间：`提醒 09:00 喝水`
  - 相对时间：`提醒 10分钟后喝水`
  - 支持部分中文数字：`提醒 一分钟后吃饭`
- `查看提醒` 查看未完成提醒。
- `取消提醒 1` 取消指定提醒。
- 每小时 58 分常数报时：
  - `启用🎒常数报时`
  - `关闭🎒常数报时`
- AI 对话：
  - 群聊 `@bot 问题`
  - 群聊 `猎宝，问题`
  - 私聊 `猎宝，问题`
  - 私聊 `猎宝 问题`
- 服务器部署：
  - NoneBot 使用 systemd 常驻运行。
  - NapCat 使用 Docker 运行。
  - SQLite 保存提醒和报时开关。

### 运行环境

- Python 3.10+
- NoneBot2
- OneBot v11
- NapCat
- SQLite
- DeepSeek/OpenAI-compatible Chat Completions API

### 安全说明

真实 API Key 只放在服务器的 `.env.local`，不要提交到 GitHub。

以下文件不进入 Git：

- `.env`
- `.env.local`
- `.venv/`
- `data/*.db`
- `qq_reminder_bot.egg-info/`
- 打包生成的 `.zip`
