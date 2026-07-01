# 猎bot

猎bot v2.0.0「猎bot-初具人形」是一个用于学习和自用的 QQ bot，基于 NoneBot2 + OneBot v11。当前版本已经从“命令型提醒 bot”整理成“消息交给 AI agent，再由 agent 调用工具”的雏形，主要支持 AI 对话、联网搜索、提醒工具、常数报时工具、分群消息采集、日报、智能陪伴画像、STS2 知识库和猎宝控制台。

猎bot加入新群后默认保持业务功能静默。常规 AI 对话、`ping`、`help` 可直接使用；提醒、消息采集和陪伴画像需要管理员按群开启。AI 对话也可以按群单独关闭。`群功能状态`、采集、日报、存储和媒体识别等管理命令仅管理员可用。

如果你是接手这份仓库的 AI，请先完整读本文件。仓库后续只保留这一份说明入口。

## 接手说明

- 先看本 README 的“功能 / 配置 / 服务器部署 / 项目结构”四部分。
- 再看 `VERSION.md`，了解各版本做过什么。
- 每次对话结束前，如果本轮改变了功能、部署方式、配置、命令或工作流，都要同步更新本 README。
- 主要代码入口：
  - `plugins/access_control.py`
  - `plugins/reminder_service.py`
  - `plugins/chime_service.py`
  - `plugins/message_archive.py`
  - `plugins/message_collector.py`
  - `plugins/media_insights.py`
  - `plugins/daily_report.py`
  - `plugins/ai_chat.py`
  - `plugins/reminder.py`
  - `plugins/group_reactions.py`
  - `plugins/storage_status.py`
  - `plugins/admin_console/`
- 当前数据流：
  - NoneBot 从 `pyproject.toml` 加载插件，OneBot v11/NapCat 负责 QQ 收发。
  - 普通用户消息先经过 `ai_chat` 判断是否触发猎宝；触发后构造本地上下文，再交给 AI agent。
  - AI agent 可调用受控工具：`web_search`、`fetch_url`、`create_reminder`、`list_reminders`、`cancel_reminder`、`get_chime`、`set_chime` 和 `respond`。
  - 命令入口仍由 `reminder.py`、`daily_report.py`、`message_collector.py` 等插件保留；提醒和常数报时的核心逻辑已下沉到 `reminder_service.py` / `chime_service.py`，方便 AI 调用。
  - 群消息先进入归档库 `message_archive`，再由 `message_collector` / `media_insights` 做采集和素材识别。
  - `daily_report` 读取数据库生成日报，`companion_memory` 读取采集消息生成画像，`admin_console` 写入控制台配置。
  - 日志只用于排障，不是最终数据源
- 关键约定：
  - `@` 会保留 QQ 号
  - 群聊日报命令会被静默忽略
  - 新群默认静默，只保留基础 AI / `ping` / `help`
  - 文件、语音、链接、聊天记录、图片、表情包、回复等都属于日报素材

## 功能

### 基础命令

- `ping`：检测 bot 是否在线。
- `help` / `帮助` / `菜单` / `功能`：查看普通用户菜单。

### 分群功能开关

新群默认只开放基础 AI 对话、`ping` 和 `help`。以下命令仅管理员可用，且需要在目标群里发送：

- `群功能状态`：查看本群提醒、消息采集、陪伴画像和常数报时状态。
- `开启群功能 AI对话` / `关闭群功能 AI对话`：控制本群是否允许 AI 对话。
- `开启群功能 提醒` / `关闭群功能 提醒`：控制本群提醒命令。
- `开启群功能 日报` / `关闭群功能 日报`：控制本群消息归档和日报数据来源；`消息采集` 仍作为兼容别名。
- `开启群功能 陪伴画像` / `关闭群功能 陪伴画像`：控制本群是否启用智能陪伴；具体记录对象由控制台按群选择。
- `开启群功能 调戏其他bot` / `关闭群功能 调戏其他bot`：控制已标记 bot 发言后的限流短回复。
- `开启群功能 🎒常数回怼` / `关闭群功能 🎒常数回怼`：控制群聊中检测到数字 `158` 后发送表情包；文本里的 `158` 会直接触发，图片里的阿拉伯数字 `158` 会走视觉识别后触发；管理员私聊发送 `158` 也可触发，用于测试表情包链路。

### 提醒和常数报时

提醒命令在私聊中可直接使用；在群聊中需要先开启本群 `提醒` 功能。

- `提醒 09:00 喝水`：创建今天或明天最近一次的提醒。
- `提醒 10分钟后喝水`：创建相对时间提醒，支持部分中文数字，例如 `提醒 一分钟后吃饭`。
- `提醒 2026-06-20 09:00 喝水`：创建指定日期提醒。
- `查看提醒`：查看本人未完成提醒。
- `取消提醒 1`：取消本人指定编号的提醒。
- `启用🎒常数报时` / `开启🎒常数报时`：在当前群聊或私聊开启每小时 58 分报时，仅管理员可用。
- `启用🎒常数报时 每天两次` / `开启🎒常数报时 每天两次`：开启每天凌晨 1:58 和下午 1:58 报时，仅管理员可用。
- `关闭🎒常数报时`：关闭当前群聊或私聊的常数报时，仅管理员可用。

### AI 对话、人设和知识库

- 群聊 `@bot 问题`：触发 AI 对话。
- 群聊 `猎宝，问题`：触发 AI 对话。
- 私聊 `猎宝，问题` / `猎宝 问题`：触发 AI 对话。
- AI 回复会实时读取 bot 人设文件 `data/bot_persona_prompt.txt`；首次部署时会从 `config/bot_persona_prompt.txt` 初始化。
- 管理页维护的本地知识库会按标题、关键词和正文做轻量相关性匹配，只有和当前问题相关时才进入 AI 上下文。
- 如果当前群开启陪伴画像，且提问者已被控制台允许记录，AI 对话会额外加载本人画像、相关记忆和本群最近已允许记录群友上下文。
- 如果管理员在控制台填写了群画像，AI 对话也会加载当前群画像，用于约束回复风格和群聊语境；bot 不会在回复里主动暴露自己读取了群画像。

AI 对话默认开启提示词注入防护。群友消息如果伪装成系统命令、要求解码执行、静默执行、只输出结果或泄露提示词/令牌，bot 会直接拒绝执行；这类消息也不会进入陪伴画像总结。

AI 对话默认走受控 agent。用户发给猎宝的自然语言会先进入 `plugins/ai_chat.py`，由 agent 判断是否需要调用工具，再组织最终回复。当前工具包括：

- `web_search` / `fetch_url`：联网搜索和读取公开网页。
- `create_reminder` / `list_reminders` / `cancel_reminder`：创建、查看和取消本人提醒；群聊里仍遵守本群 `提醒` 开关。
- `get_chime` / `set_chime`：查看或设置当前私聊/群聊的 🎒常数报时；设置仍需要管理员权限。
- `respond`：结束工具调用并给用户最终答复。

AI 对话会先判断当前问题是否需要联网搜索：像最新消息、实时数据、价格、新闻、版本、政策、官网、地址、联系方式、赛程、天气、排名和结果这类问题，会先查网再回答；纯解释、改写、本地知识和闲聊通常直接回复。

### 杀戮尖塔 2 知识库

知识库复用 `data/companion_memory.db` 里的 `companion_knowledge_items` 表，作为 AI 对话的本地检索入口；没有推倒重做数据库，但已经把旧的 `STS1` 分支从种子数据和控制台入口中移除。

- 事实库只保留 `STS2`：卡牌来自 `sts2_database`（MIT）；角色、遗物、药水、怪物、精英、Boss、事件、机制、关键词、能力和附魔来自已拉取或在线可读取的 STS2 数据文件。
- 攻略库只放自建攻略：当前内置源文件是 `knowledge_sources/sts2_guides/main_1.tex`，由 `D:\QQdownload\main (1).tex` 复制而来，导入时会按 LaTeX 章节切块为 `STS2/guide` 条目，并标注为“作者攻略/软知识”。
- 控制台 `重建 STS2 知识库` 会清空旧 `STS1` / `STS2` 条目后重建；非 STS 分类的手写知识不会被清理。
- 命令行可在项目目录执行 `python sts_knowledge_seed.py` 重建同一套知识库。可用 `STS2_GUIDE_TEX_PATHS` 指定额外 tex 攻略源，多个路径用分号或竖线分隔。
- 知识库回答时仍通过轻量相关性匹配进入模型上下文；事实条目和攻略条目会同时参与检索，但攻略内容会标明来源性质，避免把建议说成硬规则。

### 消息采集、日报数据和导出

消息采集是日报的数据入口。命令侧仍保留 `开启群功能 消息采集` 兼容旧用法；控制台侧只保留 `日报` 开关，开启后会采集群消息并作为日报输入，关闭后不再采集新的日报数据。

- 采集内容包括发送人、时间、消息段类型、可读文本、原始 OneBot 消息结构和事件 JSON。
- 会保留 `@`、图片、表情包、语音、文件、视频、回复、合并转发聊天记录、位置、分享卡片、名片、戳一戳、音乐、匿名消息、JSON/XML 等消息段信息。
- 群内 AI 对话的用户提问由消息采集保存，bot 回复会由 AI 插件同步写入采集库。
- 管理员命令通常不进入采集库。
- `采集状态` / `消息采集状态`：查看当前群采集数量；管理员私聊使用时查看全部已采集群。
- `查看采集 20`：导出最近采集消息 txt，数字范围 1-50；群聊中只导出当前群，私聊中导出全部群最近消息。

### 媒体和素材识别

媒体识别由 `.env.local` 的 `MEDIA_INSIGHTS_ENABLED` 控制，通常配合消息采集使用。识别结果写入 `message_insights`，并会出现在 `查看采集` 导出的 txt 和日报输入中。

- `媒体识别状态` / `素材识别状态`：查看实验开关、自动识别、图片模型、密钥状态、语音转写和识别记录，仅管理员可用。
- `扫描媒体识别` / `扫描素材识别`：手动补扫已采集消息里的素材，仅管理员可用。
- 自动识别开启后，新采集到的图片、表情包、链接、语音、文件、回复、视频、位置、分享卡片、名片、戳一戳、音乐、匿名消息和聊天记录都会进入后台处理。
- 图片和图片类表情包：可调用 OpenAI 兼容视觉模型，默认示例为 Qwen `qwen-vl-plus`。
- 链接：从普通文本、JSON/XML 卡片和分享卡片中提取 URL，尽量抓取标题、摘要和正文摘录；会拒绝本机、内网和非公网地址。
- 文件：记录文件名、大小、类型和风险提示；文本、PDF、docx、xlsx、pptx 等常见文档会尽量提取正文摘录。
- 语音：预留转写流程，开启 `VOICE_TRANSCRIBE_ENABLED=1` 后可调用语音模型转写。
- 合并转发聊天记录：通过 OneBot/NapCat 的 `get_forward_msg` 尝试展开，日报中会用 `[forward_message#N]` 标记子消息来源。

### 日报

日报读取 `collected_messages` 和 `message_insights`。统计窗口不是自然日，而是目标日期 04:00 到次日 04:00。

- 私聊 `预览日报 今天 群号` / `预览日报 昨天 群号` / `预览日报 2026-06-27 群号`：生成该群日报预览 txt，不调用 AI，仅管理员可用。
- 私聊 `生成日报 今天 群号` / `生成日报 昨天 群号` / `生成日报 2026-06-27 群号`：调用 AI 分块总结并合成最终日报，保存 Markdown，并发送 PDF 文件，仅管理员可用。
- 私聊 `测试日报 昨天 群号`：按自动日报路径生成并私聊发送给管理员，仅管理员可用。
- 群聊内发送日报命令会被静默忽略，避免打扰群聊。
- 自动日报由 `DAILY_REPORT_ENABLED=1` 开启，按 `DAILY_REPORT_SEND_TIME` 定时运行，默认 04:00；如果配置了 `DAILY_REPORT_GROUP_IDS`，只给这些群生成日报；未配置时会跟随控制台里每个群的 `自动发送日报` 开关。自动任务开始、每个群开始生成、跳过、失败和本轮结束时都会私聊提醒管理员。
- `DAILY_REPORT_STARTUP_GRACE_MINUTES` 控制启动补发宽限期，默认 120 分钟。比如服务在 04:01 才启动，会自动补跑一次昨天的 04:00 日报；发送记录会写入服务器数据库，避免重启多次重复私聊。
- PDF 使用 ReportLab 生成，文件名格式类似 `2026年1月1日 群名_群号.pdf`；PDF 只保留正式总结和统计信息，不包含过长的“猎bot日报预览/结构化时间线”附录，完整附录仍保存在 Markdown 文件中。

### 陪伴画像和长期记忆

陪伴画像需要管理员先为本群开启 `智能陪伴`，再在控制台里按群选择允许记录的群友。画像数据按 `(群号, QQ号)` 分开保存，同一个 QQ 在不同群会有独立画像。未被选择记录的群友仍可普通 AI 对话，但不会加载画像，也不会被画像总结。

- `画像状态` / `我的画像状态` / `陪伴状态`：查看本群画像功能开关、本人是否已被控制台允许记录，以及本群已选择记录人数。
- `我的画像` / `查看我的画像`：查看本人在当前群的画像；如果控制台未开启记录，会提示未开启。
- `画像记录名单` / `画像注册名单` / `陪伴记录名单` / `陪伴注册名单`：查看本群控制台已选择记录的群友，仅管理员可用。
- `查看群友画像 @群友` / `群友画像 @群友`：查看已选择记录群友的画像，仅管理员可用。
- 旧的群内注册、退出、自助更新、重置和删除命令会被接住并提示去控制台处理，不再写入记录对象。
- 后台自动总结由 `COMPANION_MEMORY_AUTO_ENABLED` 控制，会按间隔处理控制台已选择记录对象的新消息，并过滤疑似提示词注入内容。
- 默认画像更新周期是每 300 秒扫描一批对象，每批最多 3 人；单个群友默认至少累计 10 条新消息才总结，且两次总结默认间隔 15 分钟。这些值分别由 `COMPANION_MEMORY_INTERVAL_SECONDS`、`COMPANION_MEMORY_BATCH_USERS`、`COMPANION_MEMORY_MIN_MESSAGES` 和 `COMPANION_MEMORY_COOLDOWN_MINUTES` 控制。
- 开启 `智能陪伴` 的群会自动开启 `日报` 对应的数据采集，以保证画像和日报都有消息来源。

### 猎宝控制台

管理员配置 `COMPANION_ADMIN_TOKEN` 后，推荐通过浏览器访问 `/hunterbot/admin-console?token=管理令牌`。新控制台作为 `plugins/admin_console/` 插件随 bot 启动，面向日常运维：

- `Bot 人设`：编辑 bot 人设，保存后写入 `data/bot_persona_prompt.txt`，后续 AI 回复实时读取。
- `知识库`：现在只维护 `STS2`（杀戮尖塔 2）知识。事实资料按 `card`、`character`、`relic`、`potion`、`enemy`、`elite`、`boss`、`event`、`mechanic`、`keyword`、`power`、`enchantment` 分类；`guide` 只放自建攻略库内容，避免把作者建议和官方/数据事实混在一起。
- `群管理`：只显示 `AI 对话`、`日报`、`智能陪伴` 和 `🎒常数报时` 四个群功能开关；`日报` 同时控制消息采集和日报数据来源。`日报` 卡片下方有 `自动发送日报` 开关，只有开启 `日报` 后才能操作，用于控制该群是否参加定时自动发送。
- `智能陪伴附加功能`：某群开启 `智能陪伴` 后，会出现 `调戏其他bot` 和 `🎒常数回怼` 两个开关，并可分别配置每分钟、每小时、每天的触发上限。限制输入框下方会显示当前已使用次数：本分钟、每小时、每天三列和上方输入框居中对齐。`🎒常数回怼` 会检测文本和图片中的阿拉伯数字 `158`，图片识别由 `CONSTANT_RETORT_IMAGE_SCAN_ENABLED` 控制，复用 `IMAGE_VISION_*` 视觉模型配置；回怼图片会以 base64 消息发送，避免 NapCat 无法读取服务器本地文件路径。
- `群画像`：某群开启 `智能陪伴` 后，可编辑群性质和回复参考，默认不超过 100 字，字数上限可在控制台调整。
- `群列表头像`：控制台会按群号读取 QQ 群头像，并临时缓存到服务器 `data/admin_console/group_avatars/`，默认 7 天刷新一次；头像拉取失败时只影响头像显示，不影响群管理。
- `群友画像管理`：当某群开启 `智能陪伴` 后，下方会显示实时群成员标签。标签包含头像、昵称/群名片、QQ 号和群头衔；已开启“允许记录画像”的群友置顶并显示浅绿色标签。左侧选择群友，右侧会突出显示头像、昵称、QQ 号和头衔，并可开启/关闭“允许记录画像”、编辑画像、标记为其他 bot 和维护 bot 关键词。
- `消息采集记录`：位于群管理页智能陪伴区域下方，可查看本群采集状态、累计条数和最近采集时间；展开后按页查看消息记录，并可按群友 QQ 筛选。消息列表会优先使用当前群成员实时昵称、头像和头衔展示。
- `日间 / 夜间`：控制台右上角可切换颜色模式，选择保存在当前浏览器本地，不会影响其他管理员。
- 点击保存会调用当前服务器上的控制台 API；如果页面通过服务器地址或 SSH 隧道打开，保存结果会直接写入云端 bot 的数据文件和 SQLite 数据库。
- 现在控制台的固定网址是 `http://62.234.188.16/hunterbot/admin-console?token=你的管理令牌`，服务器上已经通过反向代理把这个地址接到本机 `127.0.0.1:8080`。

旧页面 `/hunterbot/companion-admin` 已移除。知识库文字提取入口也已移除；知识条目现在只通过控制台直接编辑、保存或重建 STS2 知识库维护。控制台和 API 未配置令牌时都会拒绝访问。

### 存储和运行状态

- `存储状态` / `硬盘状态` / `空间状态`：查看服务器磁盘、`data` 目录、消息数据库、提醒数据库、功能设置库、日报目录、媒体缓存和导出目录占用，仅管理员可用。
- 运行数据主要保存在 `data/reminders.db`、`data/bot_settings.db`、`data/message_archive.db`、`data/companion_memory.db` 和 `data/reports/`。

## 本地启动

在 PowerShell 里进入项目目录，然后运行：

```powershell
.\start.ps1
```

也可以手动运行：

```powershell
.\.venv\Scripts\python bot.py
```

启动后，NoneBot 默认监听：

```text
ws://127.0.0.1:8080/onebot/v11/ws
```

把这个地址填到 NapCat 的 OneBot v11 反向 WebSocket 配置里。

## 配置

项目根目录里的 `.env` 用于 NoneBot 基础配置，示例：

```text
HOST=127.0.0.1
PORT=8080
LOG_LEVEL=INFO
COMMAND_START=["/"]
BOT_ADMIN_USER_IDS=123456789
```

`BOT_ADMIN_USER_IDS` 填允许使用管理员命令的 QQ 号。多个 QQ 号用英文逗号分隔，例如：

```text
BOT_ADMIN_USER_IDS=123456789,987654321
```

AI 密钥不要写进 `.env`，也不要提交到 GitHub。服务器上单独创建 `.env.local`：

```text
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
AI_MODEL=deepseek-v4-flash
AI_TIMEOUT_SECONDS=30
AI_MAX_QUESTION_LENGTH=800
AI_MAX_REPLY_LENGTH=1200
AI_PROMPT_INJECTION_GUARD_ENABLED=1
AI_WEB_SEARCH_ENABLED=1
AI_WEB_SEARCH_DECIDER_MODEL=deepseek-v4-flash
AI_WEB_SEARCH_DECIDER_TIMEOUT_SECONDS=10
AI_WEB_SEARCH_TIMEOUT_SECONDS=15
AI_WEB_SEARCH_MAX_RESULTS=3
BOT_PERSONA_PATH=data/bot_persona_prompt.txt
COMPANION_ADMIN_TOKEN=
COMPANION_MEMORY_AUTO_ENABLED=1
COMPANION_MEMORY_INTERVAL_SECONDS=300
COMPANION_MEMORY_BATCH_USERS=3
COMPANION_MEMORY_MIN_MESSAGES=10
COMPANION_MEMORY_COOLDOWN_MINUTES=15
COMPANION_RECENT_CONTEXT_LIMIT=8
COMPANION_MEMORY_LOOKUP_LIMIT=5
COMPANION_SUMMARY_MODEL=deepseek-v4-pro
COMPANION_KNOWLEDGE_LOOKUP_LIMIT=3
COMPANION_KNOWLEDGE_MIN_SCORE=2
STS2_DATABASE_CARDS_DIR=
SPIRE_CODEX_ZHS_DIR=
STS2_GUIDE_TEX_PATHS=
CONSTANT_RETORT_IMAGE_PATH=data/assets/constant_retort_158.jpg
CONSTANT_RETORT_IMAGE_SCAN_ENABLED=1
CONSTANT_RETORT_IMAGE_SCAN_MAX_IMAGES=2
CONSTANT_RETORT_IMAGE_SCAN_TIMEOUT_SECONDS=12
MEDIA_INSIGHTS_ENABLED=1
MEDIA_INSIGHTS_AUTO_ENABLED=1
IMAGE_VISION_ENABLED=1
IMAGE_VISION_MODEL=qwen-vl-plus
IMAGE_VISION_API_KEY=
IMAGE_VISION_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LINK_FETCH_TIMEOUT_SECONDS=15
LINK_FETCH_MAX_BYTES=1048576
VOICE_TRANSCRIBE_ENABLED=0
VOICE_TRANSCRIBE_MODEL=whisper-1
VOICE_TRANSCRIBE_API_KEY=
VOICE_TRANSCRIBE_BASE_URL=
VOICE_TRANSCRIBE_TIMEOUT_SECONDS=90
SUMMARY_MODEL=deepseek-v4-pro
SUMMARY_API_KEY=
SUMMARY_BASE_URL=
SUMMARY_TIMEOUT_SECONDS=90
SUMMARY_CHUNK_MESSAGES=80
SUMMARY_MAX_INPUT_CHARS=24000
DAILY_REPORT_ENABLED=0
DAILY_REPORT_GROUP_IDS=
DAILY_REPORT_SEND_TIME=04:00
DAILY_REPORT_TIMEZONE=Asia/Shanghai
DAILY_REPORT_STARTUP_GRACE_MINUTES=120
```

`.env.local` 已经被 `.gitignore` 忽略，不会进入 Git。

## 猎宝控制台

管理员可以在服务器上配置 `COMPANION_ADMIN_TOKEN` 后，通过浏览器管理 bot 人设、知识库、群功能开关和控制台已选择记录的群友画像。新控制台地址：

访问地址：

```text
http://62.234.188.16/hunterbot/admin-console?token=你的管理令牌
```

控制台是 bot 内置插件 `plugins/admin_console/` 提供的页面和 API，不需要单独部署前端。页面里点击保存时，会向当前访问的 bot 服务发起 API 请求；如果你通过服务器地址或 SSH 隧道访问，修改会直接写入服务器上的运行数据。

页面结构：

- `Bot 人设`：保存后写入 `data/bot_persona_prompt.txt`，后续 AI 回复也会从这个文件实时读取；首次部署时会用 `config/bot_persona_prompt.txt` 初始化。
- `知识库`：左侧固定为 `杀戮尖塔 2`，顶部按 `card`、`character`、`relic`、`potion`、`enemy`、`elite`、`boss`、`event`、`mechanic`、`keyword`、`power`、`enchantment`、`guide` 切换类型；右侧编辑标题、关键词、正文和启用状态，保存后写入 `data/companion_memory.db`。
- `群管理`：左侧选择群，右侧只管理 `AI 对话`、`日报`、`智能陪伴` 和 `🎒常数报时`。其中 `日报` 开关就是群消息采集开关，负责控制日报数据来源，控制台不再单独提供“消息采集”入口。
- 群列表会在群名前显示群头像；头像由服务器缓存到 `data/admin_console/group_avatars/`，缓存文件属于临时运行数据，不提交 Git。
- 开启 `智能陪伴` 后，群管理页下方会出现群友画像管理；左侧读取当前群成员并以标签展示头像、昵称/群名片、QQ 号和头衔，右侧可按当前群开启/关闭该 QQ 的画像记录，并编辑近期在做、互动风格、陪伴偏好、常聊主题、画像摘要和置信度。
- `消息采集记录` 固定显示在智能陪伴区域下方，用于快速查看本群已采集消息总量、最近采集时间和最近消息预览；展开后可分页查看记录，并按群友 QQ 筛选，昵称和头像每次打开控制台时会按实时群成员信息刷新。
- 右上角 `日间 / 夜间` 分段按钮用于切换控制台颜色模式，偏好保存在当前浏览器本地。

知识库只在当前问题和知识标题、关键词或正文相关时被读取。无关对话不会把知识库内容塞进模型上下文。

旧页面 `/hunterbot/companion-admin` 已移除。如果没有配置 `COMPANION_ADMIN_TOKEN`，控制台页面和相关 API 会拒绝访问。

## 服务器部署

服务器当前使用两部分：

- NapCat：负责登录 QQ，并通过 OneBot v11 连接 NoneBot。
- NoneBot：运行猎bot的功能代码。

从本机连接服务器：

```powershell
ssh tencent-bot
```

如果本机还没有 SSH 别名，可以用服务器公网 IP 连接：

```powershell
ssh ubuntu@62.234.188.16
```

本机 SSH 别名通常写在 `C:\Users\12619\.ssh\config`，私钥保存在本机 `.ssh` 目录，不要提交到仓库。

2.0 起服务器项目目录应保持为干净 Git clone。更新代码后，在服务器项目目录执行：

```bash
cd ~/qq-reminder-bot
git pull --ff-only
.venv/bin/pip install -e .
.venv/bin/python -m py_compile bot.py plugins/access_control.py plugins/companion_registry.py plugins/companion_memory.py plugins/admin_console/__init__.py plugins/message_archive.py plugins/reminder.py plugins/reminder_service.py plugins/chime_service.py plugins/ai_chat.py plugins/group_reactions.py plugins/message_collector.py plugins/storage_status.py plugins/media_insights.py plugins/daily_report.py
sudo systemctl restart qq-reminder-bot
journalctl -u qq-reminder-bot -n 80 --no-pager
```

服务器运行数据不进 Git：`.env.local`、`data/`、`.venv/` 都保留在服务器本地。重建服务器目录前必须先备份 `.env.local`、`data/` 和 SQLite 数据库，恢复后再启动服务。

查看服务状态：

```bash
systemctl is-active qq-reminder-bot
sudo docker ps --filter name=napcat
```

更完整的服务器部署、配置、管理页访问和回滚步骤见 `DEPLOY.md`。

## 项目结构

```text
bot.py                 NoneBot 启动入口
plugins/access_control.py 分群开关和管理员权限代码
plugins/companion_registry.py 陪伴画像记录对象、状态和名单命令
plugins/companion_memory.py 陪伴画像、记忆、检索和自动总结代码
plugins/admin_console/ 猎宝控制台插件，提供人设、知识库、群管理和群友画像管理页面
plugins/reminder.py    提醒、查看、取消、常数报时命令入口和定时调度
plugins/reminder_service.py 提醒解析、创建、查询、取消和到期扫描服务，可供 AI agent 调用
plugins/chime_service.py 常数报时开关、模式和目标列表服务，可供 AI agent 调用
plugins/ai_chat.py     AI 对话、联网搜索和工具调用 agent
plugins/group_reactions.py 群聊附加反应：调戏其他bot、158常数回怼
plugins/message_archive.py 消息归档写入代码
plugins/message_collector.py 指定群消息采集代码
plugins/storage_status.py 存储占用查询代码
plugins/media_insights.py 媒体识别、链接解析、文件读取、语音转写和素材自动识别代码
plugins/daily_report.py 日报预览、AI总结、PDF和定时发送代码
data/                 SQLite 数据库、生成日报、头像缓存和表情包资源等运行时文件，不提交
.env.example           配置模板，不包含真实密钥
pyproject.toml         项目依赖和插件配置
VERSION.md             版本说明
```

## GitHub 注意事项

不要提交这些内容：

- `.env`
- `.env.local`
- `data/`
- `.venv/`
- `qq_reminder_bot.egg-info/`
- 打包生成的 `.zip`

提交前可以用 VS Code 的源代码管理面板确认文件列表。看到密钥、数据库或虚拟环境文件时，不要发布。
