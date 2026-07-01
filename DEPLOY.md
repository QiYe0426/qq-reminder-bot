# 猎bot服务器部署说明

本文用于在腾讯云服务器上维护 `~/qq-reminder-bot`。2.0 起服务器目录应保持为干净 Git clone，代码从 GitHub 更新；`.env.local`、`data/`、`.venv/` 只留在服务器本地，不提交 GitHub。

## 1. 连接服务器

本机已经配置 SSH 别名时：

```powershell
ssh tencent-bot
```

也可以用公网 IP：

```powershell
ssh ubuntu@62.234.188.16
```

## 2. 首次整理为干净 Git clone

整理服务器目录前必须先停止服务并备份运行数据：

```bash
sudo systemctl stop qq-reminder-bot
backup_dir=~/hunterbot-backups/$(date +%Y%m%d-%H%M%S)
mkdir -p "$backup_dir"
cp -a ~/qq-reminder-bot/.env.local "$backup_dir/.env.local"
cp -a ~/qq-reminder-bot/data "$backup_dir/data"
cp -a ~/qq-reminder-bot/.venv "$backup_dir/.venv"
```

再把旧目录挪走，重新 clone：

```bash
mv ~/qq-reminder-bot "$backup_dir/qq-reminder-bot-old"
git clone https://github.com/QiYe0426/qq-reminder-bot.git ~/qq-reminder-bot
cp -a "$backup_dir/.env.local" ~/qq-reminder-bot/.env.local
cp -a "$backup_dir/data" ~/qq-reminder-bot/data
cp -a "$backup_dir/.venv" ~/qq-reminder-bot/.venv
```

如果 `.venv` 不可复用，就在新目录里重新创建：

```bash
cd ~/qq-reminder-bot
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -e .
```

## 3. 日常更新

以后更新代码只需要：

```bash
cd ~/qq-reminder-bot
git pull --ff-only
.venv/bin/pip install -e .
```

不要在服务器运行目录里手工改源码。要改代码，先在本地改、测试、提交并推送 GitHub，再到服务器 `git pull`。

## 4. 配置 `.env.local`

真实密钥只放服务器 `.env.local`，不要提交到 GitHub。至少确认这些配置：

```text
BOT_ADMIN_USER_IDS=你的QQ号

DEEPSEEK_API_KEY=你的DeepSeek密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
AI_MODEL=deepseek-v4-flash
AI_AGENT_ENABLED=1
AI_AGENT_MODEL=deepseek-v4-pro
AI_AGENT_TIMEOUT_SECONDS=90
AI_AGENT_MAX_TOOL_CALLS=8

COMPANION_ADMIN_TOKEN=一串很长的随机管理令牌
COMPANION_MEMORY_AUTO_ENABLED=1
COMPANION_SUMMARY_MODEL=deepseek-v4-pro

SUMMARY_MODEL=deepseek-v4-pro
SUMMARY_API_KEY=你的日报总结密钥，留空则复用 DEEPSEEK_API_KEY
SUMMARY_BASE_URL=https://api.deepseek.com
DAILY_REPORT_ENABLED=0
DAILY_REPORT_GROUP_IDS=
DAILY_REPORT_SEND_TIME=04:00
DAILY_REPORT_TIMEZONE=Asia/Shanghai
DAILY_REPORT_STARTUP_GRACE_MINUTES=120
```

`DAILY_REPORT_GROUP_IDS` 留空时，自动日报跟随控制台每个群的「自动发送日报」开关；如果这里写了群号，它会变成强制白名单，只给这些群自动生成。

`BOT_PERSONA_PATH` 默认是 `data/bot_persona_prompt.txt`。控制台保存的人设、知识库、群画像、陪伴画像和运行数据库都在 `data/` 下。

## 5. 编译检查

```bash
cd ~/qq-reminder-bot
.venv/bin/python -m py_compile \
  bot.py \
  plugins/access_control.py \
  plugins/companion_registry.py \
  plugins/companion_memory.py \
  plugins/admin_console/__init__.py \
  plugins/message_archive.py \
  plugins/reminder.py \
  plugins/reminder_service.py \
  plugins/chime_service.py \
  plugins/ai_chat.py \
  plugins/group_reactions.py \
  plugins/message_collector.py \
  plugins/storage_status.py \
  plugins/media_insights.py \
  plugins/daily_report.py
```

## 6. 重启服务

```bash
sudo systemctl restart qq-reminder-bot
systemctl is-active qq-reminder-bot
journalctl -u qq-reminder-bot -n 100 --no-pager
```

确认 NapCat：

```bash
sudo docker ps --filter name=napcat
```

systemd 服务当前指向：

```text
WorkingDirectory=/home/ubuntu/qq-reminder-bot
ExecStart=/home/ubuntu/qq-reminder-bot/.venv/bin/python /home/ubuntu/qq-reminder-bot/bot.py
```

## 7. 群内启用流程

管理员在群里发送：

```text
开启群功能 日报
开启群功能 陪伴画像
```

然后打开猎宝控制台，在 `群管理 -> 智能陪伴 -> 群友画像管理` 里选择允许记录画像的群友。只有控制台已开启记录的群友消息会进入陪伴画像总结。

## 8. 打开控制台

固定网址：

```text
http://62.234.188.16/hunterbot/admin-console?token=你的管理令牌
```

服务器通过 Nginx 把这个地址反代到本机 `127.0.0.1:8080`。配置参考在 `deploy/nginx/hunterbot-admin-console.conf`。

控制台包含 `Bot 人设`、`知识库`、`群管理`、`日报`、`智能陪伴`、`群画像`、`群友画像管理` 和 `消息采集记录`。旧 `/hunterbot/companion-admin` 页面和知识库文字提取入口已移除。

## 9. 常用验证命令

群内：

```text
群功能状态
画像状态
我的画像
画像记录名单
```

服务器：

```bash
git status --short --branch
journalctl -u qq-reminder-bot -n 100 --no-pager
ls -lh data/
```

浏览器/API：

```text
http://62.234.188.16/hunterbot/admin-console?token=你的管理令牌
http://62.234.188.16/hunterbot/admin-console/api/state?token=你的管理令牌
```

## 10. 回滚

如果新版本启动失败，优先回滚 Git：

```bash
cd ~/qq-reminder-bot
git log --oneline -5
git reset --hard 上一个可用提交
.venv/bin/pip install -e .
sudo systemctl restart qq-reminder-bot
```

如果目录损坏，用 `~/hunterbot-backups/时间戳/` 里的 `.env.local`、`data/` 和 `.venv/` 恢复。运行数据在 `data/*.db`、`data/reports/`、`data/assets/` 等位置，不能从 GitHub 找回。
