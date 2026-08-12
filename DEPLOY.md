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
sudo apt-get install -y libgl1  # RapidOCR/OpenCV 本地图片文字识别运行库
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -e .
```

## 3. 日常更新

以后更新代码只需要：

```bash
cd ~/qq-reminder-bot
git pull --ff-only
sudo apt-get install -y libgl1  # 首次启用本地 OCR 时执行
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
AGENT_TOOL_AUDIT_HMAC_KEY=用安全生成器生成的64位十六进制字符串
LOG_PRIVACY_MODE=safe

COMPANION_ADMIN_TOKEN=一串很长的随机管理令牌
COMPANION_MEMORY_AUTO_ENABLED=1
COMPANION_SUMMARY_MODEL=deepseek-v4-pro
COMPANION_SUMMARY_TIMEOUT_SECONDS=90

SUMMARY_MODEL=deepseek-v4-pro
SUMMARY_API_KEY=你的日报总结密钥，留空则复用 DEEPSEEK_API_KEY
SUMMARY_BASE_URL=https://api.deepseek.com
DAILY_REPORT_ENABLED=0
DAILY_REPORT_GROUP_IDS=
DAILY_REPORT_SEND_TIME=04:00
DAILY_REPORT_TIMEZONE=Asia/Shanghai
DAILY_REPORT_STARTUP_GRACE_MINUTES=120
```

首次配置 Audit HMAC key 时，在服务器上生成 32 字节随机值的 64 位十六进制编码，并只写入 `.env.local`：

```bash
openssl rand -hex 32
# 或
python -c "import secrets; print(secrets.token_hex(32))"
chmod 600 .env.local
```

不要把生成值输出到工单、日志或 Git。配置缺失时服务仍可启动，但会使用仅当前进程有效的 ephemeral key 并输出 warning；配置存在但不是恰好 64 位十六进制字符串时会明确失败，不会静默换用随机 key。

启用持久 key 的时间点是新的 fingerprint epoch 边界：历史 Audit 事件保持原样，无法用新 key 重算，也不应修改。更换 key 会再次创建新 epoch；轮换前记录时间和旧 `key_epoch`。当前不支持多 key 或无缝轮换。

生产保持 `LOG_PRIVACY_MODE=safe`。只有受控开发排障才可临时显式使用 `debug`，因为该模式可能恢复含 QQ 标识、消息正文和媒体 URL 的 NoneBot 原始事件日志。

现有运行目录权限由管理员人工核对并收紧；应用只对新路径安全创建、对既有宽权限告警：

```bash
chmod 700 data
chmod 600 data/agent_tool_audit.db \
  data/agent_tool_confirmations.db \
  data/agent_tool_executions.db
```

若某个数据库尚未创建，应跳过对应路径。启动后可从日志确认 `key_source` 与短 `key_epoch`，但不要打印或搜索真实 key。

`DAILY_REPORT_GROUP_IDS` 留空时，自动日报跟随控制台每个群的「自动发送日报」开关；如果这里写了群号，它会变成白名单，但仍要求该群在控制台开启「消息采集」和「日报」。

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
  plugins/daily_report.py \
  plugins/remote_approval.py \
  scripts/codex_remote_approval_hook.py
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

## 8. 配置 HTTPS 并打开控制台

腾讯云安全组先放行入站 TCP 443。服务器使用 Let’s Encrypt 的公网 IP 短期证书，证书约 6 天有效，因此必须保留 Certbot 自动续期任务。

首次安装：

```bash
sudo snap install --classic certbot
sudo ln -sf /snap/bin/certbot /usr/local/bin/certbot
sudo install -d -m 0755 /var/www/letsencrypt/.well-known/acme-challenge
sudo certbot certonly \
  --preferred-profile shortlived \
  --webroot \
  --webroot-path /var/www/letsencrypt \
  --ip-address 62.234.188.16 \
  --cert-name hunterbot-ip \
  --non-interactive \
  --agree-tos \
  --register-unsafely-without-email
```

把 `deploy/nginx/hunterbot-admin-console.conf` 安装到 Nginx，并安装证书续期 hook：

```bash
sudo cp deploy/nginx/hunterbot-admin-console.conf /etc/nginx/sites-available/hunterbot-admin-console.conf
sudo install -m 0755 deploy/certbot/reload-nginx.sh /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
sudo nginx -t
sudo systemctl reload nginx
sudo certbot renew --cert-name hunterbot-ip --dry-run --no-random-sleep-on-renew
```

首次登录网址：

```text
https://62.234.188.16/hunterbot/admin-console?token=你的管理令牌
```

验证后会写入安全 Cookie 并跳转到干净网址。把这个干净网址加入收藏夹：

```text
https://62.234.188.16/hunterbot/admin-console
```

服务器通过 Nginx 把 HTTPS 请求反代到本机 `127.0.0.1:8080`，80 端口只保留证书验证并跳转到 HTTPS。

控制台包含 `Bot 人设`、`知识库`、`群管理`、`消息采集`、`日报`、`智能陪伴`、`群画像`、`群友画像管理` 和 `消息采集记录`。旧 `/hunterbot/companion-admin` 页面和知识库文字提取入口已移除。

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
https://62.234.188.16/hunterbot/admin-console
https://62.234.188.16/hunterbot/admin-console/api/state
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
