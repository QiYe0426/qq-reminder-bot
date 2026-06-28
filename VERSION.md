# 猎bot版本日志

## v1.2.0（开发中）

### 新增功能

- 新增陪伴画像注册地基：
  - 管理员可通过 `开启群功能 陪伴画像` / `关闭群功能 陪伴画像` 控制本群是否允许画像功能。
  - 群友需要本人发送 `同意猎宝记录我` 或 `注册猎宝画像` 后，才会进入陪伴画像注册表。
  - 群友可随时发送 `退出猎宝画像`、`注销猎宝画像`、`关闭我的画像` 或 `删除我的画像` 停止后续画像更新。
  - `画像状态` 可查看本群功能开关、本人注册状态和本群注册人数。
  - 管理员可用 `画像注册名单` 查看本群已注册群友。
- 新增陪伴画像和记忆：
  - `我的画像` 查看本人当前画像。
  - `更新我的画像` 手动触发本人画像总结。
  - `重置我的画像` 清空本人画像和记忆，但保留注册状态。
  - `删除我的画像` 清空本人画像和记忆，并退出注册。
  - 管理员可用 `查看群友画像 @群友` 查看已注册群友画像。
  - AI 对话会在本群开启陪伴画像且用户本人已注册时，加载用户画像、相关记忆和最近注册群友上下文。
  - 未注册群友仍可普通 AI 对话，但不会加载画像，也不会被画像总结。
- 新增 bot 人设配置入口：
  - `.env.local` 可配置 `BOT_PERSONA_PROMPT`。
  - 默认留空，由使用者自行填写。
- 新增自动画像更新：
  - 通过 `COMPANION_MEMORY_AUTO_ENABLED` 控制是否自动更新。
  - 通过 `COMPANION_MEMORY_INTERVAL_SECONDS`、`COMPANION_MEMORY_BATCH_USERS`、`COMPANION_MEMORY_MIN_MESSAGES` 和 `COMPANION_MEMORY_COOLDOWN_MINUTES` 控制轮询频率、批次和冷却。
- 新增陪伴画像可视化管理页：
  - 通过 `COMPANION_ADMIN_TOKEN` 开启管理员访问令牌。
  - 访问 `/hunterbot/companion-admin?token=管理令牌` 可打开管理页。
  - 左侧显示 `bot 人设` 和已注册群友列表。
  - 右侧可编辑 bot 人设、近期在做、互动风格、陪伴偏好、常聊主题、画像摘要和置信度。
  - 保存后直接写入服务器 SQLite，后续 AI 回复实时读取。
- 新增本地知识库：
  - 知识库与 bot 人设共用 `data/companion_memory.db`。
  - 管理页左侧新增 `新建知识` 和知识条目列表。
  - 可编辑标题、分类、关键词、正文和启用状态。
  - AI 对话会先做轻量相关性判断，只有当前问题命中知识标题、关键词或正文时才读取知识库。
  - 通过 `COMPANION_KNOWLEDGE_LOOKUP_LIMIT` 控制最多读取条数。
  - 通过 `COMPANION_KNOWLEDGE_MIN_SCORE` 控制最低相关分数。

### 技术变更

- 新增插件 `plugins/companion_registry.py`。
- 新增插件 `plugins/companion_memory.py`。
- 新增插件 `plugins/companion_admin.py`。
- 新增 SQLite 数据库 `data/companion_memory.db`，目前保存陪伴画像注册记录，后续画像摘要也会放在这里。
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
