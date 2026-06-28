# 猎bot

猎bot 是一个用于学习和自用的 QQ bot，基于 NoneBot2 + OneBot v11。当前版本主要支持定时提醒、常数报时、AI 对话、分群消息采集和素材识别。

猎bot加入新群后默认保持业务功能静默。常规 AI 对话、`ping`、`help`、`群功能状态` 可直接使用，其他群功能需要管理员按群开启。

如果你是接手这份仓库的 AI，请先完整读本文件。仓库后续只保留这一份说明入口。

## 接手说明

- 先看本 README 的“功能 / 配置 / 服务器部署 / 项目结构”四部分。
- 再看 `VERSION.md`，了解各版本做过什么。
- 每次对话结束前，如果本轮改变了功能、部署方式、配置、命令或工作流，都要同步更新本 README。
- 主要代码入口：
  - `plugins/access_control.py`
  - `plugins/message_archive.py`
  - `plugins/message_collector.py`
  - `plugins/media_insights.py`
  - `plugins/daily_report.py`
  - `plugins/ai_chat.py`
  - `plugins/reminder.py`
  - `plugins/storage_status.py`
- 当前数据流：
  - 群消息先进入归档库 `message_archive`
  - 再由 `message_collector` / `media_insights` 做采集和素材识别
  - `daily_report` 读取数据库生成日报
  - 日志只用于排障，不是最终数据源
- 关键约定：
  - `@` 会保留 QQ 号
  - 群聊日报命令会被静默忽略
  - 新群默认静默，只保留基础 AI / `ping` / `help` / `群功能状态`
  - 文件、语音、链接、聊天记录、图片、表情包、回复等都属于日报素材

## 功能

- `ping`：检测 bot 是否在线。
- `help`：查看菜单。
- `提醒 09:00 喝水`：创建今天或明天最近一次的提醒。
- `提醒 10分钟后喝水`：创建相对时间提醒。
- `提醒 2026-06-20 09:00 喝水`：创建指定日期提醒。
- `查看提醒`：查看未完成提醒。
- `取消提醒 1`：取消指定编号的提醒。
- `启用🎒常数报时` / `开启🎒常数报时`：在当前群聊或私聊开启每小时 58 分报时，仅管理员可用。
- `启用🎒常数报时 每天两次` / `开启🎒常数报时 每天两次`：在当前群聊或私聊开启每天凌晨1:58和下午1:58报时，仅管理员可用。
- `关闭🎒常数报时`：关闭当前群聊或私聊的常数报时，仅管理员可用。
- `采集状态`：查看指定群消息采集状态，仅管理员可用。
- `查看采集 20`：导出当前群最近采集消息 txt，仅管理员可用，最大 50 条。
- `存储状态`：查看服务器硬盘、数据库、日报和媒体缓存占用，仅管理员可用。
- `媒体识别状态`：查看媒体识别开关、视觉模型、自动识别状态和识别记录，仅管理员可用。
- `扫描媒体识别`：手动补扫已采集消息里的图片、表情包、链接、语音、文件、回复、视频和聊天记录，仅管理员可用。
- 素材自动识别：开启媒体识别后，新采集到的图片、表情包、聊天记录、链接、语音、文件、回复、视频、位置、分享卡片、名片、戳一戳、音乐和匿名消息都会进入后台处理，结果写入采集导出的 txt。
- 链接内容提取：从文本和 QQ 分享卡片中提取链接，尽量抓取标题、摘要和正文摘录；会跳过内网/本机地址。
- 私聊 `预览日报 今天 群号` / `预览日报 昨天 群号`：生成该群日报预览 txt，不调用 AI，仅管理员可用。
- 私聊 `生成日报 今天 群号` / `生成日报 昨天 群号` / `生成日报 2026-06-27 群号`：调用 AI 生成日报，并发送 PDF 文件，仅管理员可用。
- 私聊 `测试日报 昨天 群号`：按自动日报路径生成并私聊发送给管理员，仅管理员可用。
- 群聊内发送日报命令会被静默忽略，避免打扰群聊。
- 自动日报每天 04:00 运行，自动为已开启“消息采集”的群生成前一天 04:00 到当天 04:00 的日报，并私聊发送给管理员。
- 群聊 `@bot 问题` 或 `猎宝，问题`：触发 AI 对话。
- 私聊 `猎宝，问题` 或 `猎宝 问题`：触发 AI 对话。

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
DAILY_REPORT_SEND_TIME=04:00
DAILY_REPORT_TIMEZONE=Asia/Shanghai
```

`.env.local` 已经被 `.gitignore` 忽略，不会进入 Git。

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

更新代码后，在服务器项目目录执行：

```bash
cd ~/qq-reminder-bot
unzip -o ~/qq-reminder-bot-source.zip
.venv/bin/pip install -e .
.venv/bin/python -m py_compile bot.py plugins/access_control.py plugins/message_archive.py plugins/reminder.py plugins/ai_chat.py plugins/message_collector.py plugins/storage_status.py plugins/media_insights.py plugins/daily_report.py
sudo systemctl restart qq-reminder-bot
journalctl -u qq-reminder-bot -n 80 --no-pager
```

查看服务状态：

```bash
systemctl is-active qq-reminder-bot
sudo docker ps --filter name=napcat
```

## 项目结构

```text
bot.py                 NoneBot 启动入口
plugins/access_control.py 分群开关和管理员权限代码
plugins/reminder.py    提醒、查看、取消、常数报时代码
plugins/ai_chat.py     AI 对话代码
plugins/message_archive.py 消息归档写入代码
plugins/message_collector.py 指定群消息采集代码
plugins/storage_status.py 存储占用查询代码
plugins/media_insights.py 媒体识别、链接解析、文件读取、语音转写和素材自动识别代码
plugins/daily_report.py 日报预览、AI总结、PDF和定时发送代码
data/                 SQLite 数据库、生成日报等运行时文件，运行后自动生成，不提交
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
