# 猎bot v1.0.0

发布日期：2026-06-20

## 版本定位

这是猎bot的第一个可用版本。目标不是做成大型平台，而是稳定跑通 QQ bot 的完整链路：QQ 登录、消息接收、功能处理、消息回复、服务器常驻运行。

## 已完成功能

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

## 运行环境

- Python 3.10+
- NoneBot2
- OneBot v11
- NapCat
- SQLite
- DeepSeek/OpenAI-compatible Chat Completions API

## 安全说明

真实 API Key 只放在服务器的 `.env.local`，不要提交到 GitHub。

以下文件不进入 Git：

- `.env`
- `.env.local`
- `.venv/`
- `data/reminders.db`
- `qq_reminder_bot.egg-info/`
- 打包生成的 `.zip`

## 后续方向

- 给 AI 对话增加上下文记忆。
- 增加管理员权限控制。
- 增加更多自然语言时间解析。
- 用 GitHub Release 管理正式版本包。
