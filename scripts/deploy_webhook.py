"""
GitHub Webhook 自动部署接收器

监听 GitHub push 事件，自动执行：
  git pull --ff-only && sudo systemctl restart qq-reminder-bot

用法：
  WEBHOOK_SECRET="xxx" python scripts/deploy_webhook.py

支持通过环境变量配置：
  WEBHOOK_SECRET  — GitHub Webhook secret（必填）
  WEBHOOK_PORT    — 监听端口（默认 8081）
  WEBHOOK_HOST    — 监听地址（默认 127.0.0.1）
  REPO_DIR        — 仓库目录（默认 ~/qq-reminder-bot）
"""

import http.server
import hmac
import hashlib
import json
import logging
import os
import subprocess
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("deploy-webhook")

SECRET = os.environ.get("WEBHOOK_SECRET", "")
PORT = int(os.environ.get("WEBHOOK_PORT", "8081"))
HOST = os.environ.get("WEBHOOK_HOST", "127.0.0.1")
REPO_DIR = os.environ.get(
    "REPO_DIR",
    os.path.expanduser("~/qq-reminder-bot"),
)


def verify_signature(payload_body, signature_header):
    """验证 GitHub HMAC-SHA256 签名"""
    if not signature_header:
        logger.warning("Missing signature header")
        return False
    expected = "sha256=" + hmac.new(
        SECRET.encode(),
        payload_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def deploy():
    """执行部署：git pull + restart service"""
    logger.info("Starting deployment...")

    # git pull（带重试，服务器到 GitHub 网络不稳定）
    
    def git_pull_with_retry(max_retries=3):
        last_error = None
        for attempt in range(max_retries):
            try:
                return subprocess.run(
                    ["git", "pull", "--ff-only"],
                    cwd=REPO_DIR,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            except subprocess.TimeoutExpired as e:
                last_error = e
                logger.warning(f"git pull attempt {attempt + 1}/{max_retries} timed out, retrying...")
        raise last_error  # 所有重试都失败
    
    result = git_pull_with_retry()
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    if result.returncode != 0:
        logger.error(f"git pull failed (exit {result.returncode})")
        if stdout:
            logger.error(f"stdout: {stdout}")
        if stderr:
            logger.error(f"stderr: {stderr}")
        return False, f"git pull failed: {stderr or stdout}"

    if stdout:
        for line in stdout.split("\n"):
            logger.info(f"  git: {line}")
    if stderr:
        for line in stderr.split("\n"):
            logger.warning(f"  git(stderr): {line}")

    # 检查是否有实际更新
    if "Already up to date" in stdout:
        logger.info("No changes to deploy.")
        return True, "Already up to date"

    # 安装依赖 + 编译检查
    logger.info("Installing dependencies...")
    pip_result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", "."],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if pip_result.returncode != 0:
        logger.error(f"pip install failed: {pip_result.stderr}")
        return False, f"pip install failed: {pip_result.stderr}"

    logger.info("Running compile check...")
    py_result = subprocess.run(
        [sys.executable, "-m", "py_compile", "bot.py"]
        + [f"plugins/{f}" for f in os.listdir(os.path.join(REPO_DIR, "plugins"))
           if f.endswith(".py")]
        + ["scripts/codex_remote_approval_hook.py"],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if py_result.returncode != 0:
        logger.error(f"Compile check failed: {py_result.stderr}")
        return False, f"Compile check failed: {py_result.stderr}"

    # 重启服务
    logger.info("Restarting qq-reminder-bot service...")
    svc_result = subprocess.run(
        ["sudo", "systemctl", "restart", "qq-reminder-bot"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if svc_result.returncode != 0:
        logger.error(f"Service restart failed: {svc_result.stderr}")
        return False, f"Service restart failed: {svc_result.stderr}"

    # 检查服务状态
    status_result = subprocess.run(
        ["systemctl", "is-active", "qq-reminder-bot"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    status = status_result.stdout.strip()
    logger.info(f"Service status: {status}")
    return status == "active", f"Deploy OK, service {status}"


class WebhookHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        sig = self.headers.get("X-Hub-Signature-256", "")
        event = self.headers.get("X-GitHub-Event", "")

        if not verify_signature(body, sig):
            logger.warning(f"Invalid signature (event={event})")
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"Invalid signature")
            return

        if event == "ping":
            logger.info("Received ping from GitHub")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"msg": "pong"}).encode())
            return

        if event != "push":
            logger.info(f"Ignoring event: {event}")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(f"Ignored event: {event}".encode())
            return

        # 解析 payload 获取分支信息
        try:
            payload = json.loads(body)
            ref = payload.get("ref", "")
            branch = ref.replace("refs/heads/", "")
            logger.info(f"Push event: branch={branch}, repo={payload.get('repository', {}).get('full_name', 'unknown')}")
        except json.JSONDecodeError:
            logger.warning("Failed to parse push payload")
            branch = "unknown"

        success, message = deploy()

        self.send_response(200 if success else 500)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        result = f"{'✅' if success else '❌'} {message}"
        self.wfile.write(result.encode("utf-8"))

    def log_message(self, format, *args):
        logger.info(f"http: {args[0]} {args[1]} {args[2]}")


def main():
    if not SECRET:
        logger.error("WEBHOOK_SECRET 环境变量未设置！")
        sys.exit(1)

    if not os.path.isdir(REPO_DIR):
        logger.error(f"仓库目录不存在: {REPO_DIR}")
        sys.exit(1)

    logger.info(f"Starting webhook receiver on {HOST}:{PORT}")
    logger.info(f"Repository: {REPO_DIR}")

    server = http.server.HTTPServer((HOST, PORT), WebhookHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
