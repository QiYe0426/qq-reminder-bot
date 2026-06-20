# 猎bot

猎bot 是一个用于学习和自用的 QQ bot，基于 NoneBot2 + OneBot v11。当前版本主要支持定时提醒、常数报时和 AI 对话。

## 功能

- `ping`：检测 bot 是否在线。
- `help`：查看菜单。
- `提醒 09:00 喝水`：创建今天或明天最近一次的提醒。
- `提醒 10分钟后喝水`：创建相对时间提醒。
- `提醒 2026-06-20 09:00 喝水`：创建指定日期提醒。
- `查看提醒`：查看未完成提醒。
- `取消提醒 1`：取消指定编号的提醒。
- `启用🎒常数报时`：在当前群聊或私聊开启每小时 58 分报时。
- `关闭🎒常数报时`：关闭当前群聊或私聊的常数报时。
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
```

AI 密钥不要写进 `.env`，也不要提交到 GitHub。服务器上单独创建 `.env.local`：

```text
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
AI_MODEL=deepseek-v4-flash
AI_TIMEOUT_SECONDS=30
AI_MAX_QUESTION_LENGTH=800
AI_MAX_REPLY_LENGTH=1200
```

`.env.local` 已经被 `.gitignore` 忽略，不会进入 Git。

## 服务器部署

服务器当前使用两部分：

- NapCat：负责登录 QQ，并通过 OneBot v11 连接 NoneBot。
- NoneBot：运行猎bot的功能代码。

更新代码后，在服务器项目目录执行：

```bash
cd ~/qq-reminder-bot
unzip -o ~/qq-reminder-bot-source.zip
.venv/bin/pip install -e .
.venv/bin/python -m py_compile bot.py plugins/reminder.py plugins/ai_chat.py
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
plugins/reminder.py    提醒、查看、取消、常数报时代码
plugins/ai_chat.py     AI 对话代码
data/reminders.db      SQLite 数据库，运行后自动生成，不提交
.env.example           配置模板，不包含真实密钥
pyproject.toml         项目依赖和插件配置
VERSION.md             版本说明
```

## GitHub 注意事项

不要提交这些内容：

- `.env`
- `.env.local`
- `data/reminders.db`
- `.venv/`
- `qq_reminder_bot.egg-info/`
- 打包生成的 `.zip`

提交前可以用 VS Code 的源代码管理面板确认文件列表。看到密钥、数据库或虚拟环境文件时，不要发布。
