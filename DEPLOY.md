# 猎bot服务器部署说明

本文用于把本地源码包部署到服务器上的 `~/qq-reminder-bot`。

## 1. 上传源码包

把 `qq-reminder-bot-source.zip` 上传到服务器用户目录，例如：

```bash
scp qq-reminder-bot-source.zip user@server:~/
```

## 2. 解压更新代码

```bash
cd ~/qq-reminder-bot
unzip -o ~/qq-reminder-bot-source.zip
```

源码包只包含代码、文档和配置模板，不包含 `.env.local`、SQLite 数据库、虚拟环境和本地日志。

## 3. 安装依赖

```bash
.venv/bin/pip install -e .
```

如果服务器环境里缺少 OpenAI SDK，可单独安装：

```bash
.venv/bin/pip install "openai>=1.0.0"
```

## 4. 配置 `.env.local`

真实密钥只放服务器 `.env.local`，不要提交到 Git。

至少确认这些配置：

```text
BOT_ADMIN_USER_IDS=你的QQ号

DEEPSEEK_API_KEY=你的DeepSeek密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
AI_MODEL=deepseek-v4-flash
AI_TIMEOUT_SECONDS=30
AI_MAX_QUESTION_LENGTH=800
AI_MAX_REPLY_LENGTH=1200
AI_PROMPT_INJECTION_GUARD_ENABLED=1

COMPANION_ADMIN_TOKEN=一串很长的随机管理令牌
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
```

`BOT_PERSONA_PROMPT` 可以留空。上线后建议在管理页里编辑 bot 人设，保存后会写入 `data/companion_memory.db` 并实时生效。知识库也放在同一个数据库里。

## 5. 编译检查

```bash
.venv/bin/python -m py_compile \
  bot.py \
  plugins/access_control.py \
  plugins/companion_registry.py \
  plugins/companion_memory.py \
  plugins/companion_admin.py \
  plugins/message_archive.py \
  plugins/reminder.py \
  plugins/ai_chat.py \
  plugins/message_collector.py \
  plugins/storage_status.py \
  plugins/media_insights.py
```

## 6. 重启服务

```bash
sudo systemctl restart qq-reminder-bot
journalctl -u qq-reminder-bot -n 100 --no-pager
```

确认状态：

```bash
systemctl is-active qq-reminder-bot
sudo docker ps --filter name=napcat
```

## 7. 群内启用流程

管理员在群里发送：

```text
开启群功能 消息采集
开启群功能 陪伴画像
```

群友本人发送：

```text
同意猎宝记录我
```

只有注册后的消息会进入陪伴画像总结。

## 8. 打开管理页

如果服务器端口可以访问：

```text
http://服务器地址:8080/hunterbot/companion-admin?token=你的管理令牌
```

如果不想把管理页暴露到公网，推荐 SSH 端口转发：

```bash
ssh -L 8090:127.0.0.1:8080 user@server
```

如果本机已经配置好 SSH 别名 `tencent-bot`，在 PowerShell 里运行：

```powershell
ssh -L 8090:127.0.0.1:8080 tencent-bot
```

然后本地浏览器打开：

```text
http://127.0.0.1:8090/hunterbot/companion-admin?token=你的管理令牌
```

管理页左侧包含 `bot 人设`、`新建知识`、知识条目和已注册群友。知识条目保存后，AI 对话会先做轻量相关性判断，只有相关时才读取知识库内容。

知识编辑区可以提取 PDF、网页、图片和常见文本文件里的文字。提取结果会先进入预览框，确认后可以追加或替换正文，最后点保存才会写入知识库。

PDF 提取需要 `pypdf`，执行 `.venv/bin/pip install -e .` 会一起安装。图片文字提取需要视觉模型配置：

```text
IMAGE_VISION_API_KEY=你的视觉模型密钥
IMAGE_VISION_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
IMAGE_OCR_MODEL=qwen-vl-plus
```

## 9. 常用验证命令

群内：

```text
群功能状态
画像状态
我的画像
画像注册名单
```

服务器：

```bash
journalctl -u qq-reminder-bot -n 100 --no-pager
ls -lh data/
```

## 10. 回滚

如果新版本启动失败，可以把上一版源码包重新解压回 `~/qq-reminder-bot`，再执行：

```bash
.venv/bin/pip install -e .
sudo systemctl restart qq-reminder-bot
```

数据文件在 `data/*.db`，源码包不会覆盖这些运行数据。
