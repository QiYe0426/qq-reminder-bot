"""Standalone script: start NoneBot, wait for NapCat to connect, send report to admin, exit."""

import asyncio
import os
import sys
from pathlib import Path

# Ensure we're in the right directory
os.chdir("/home/ubuntu/qq-reminder-bot")
sys.path.insert(0, ".")
os.environ.setdefault("DAILY_REPORT_VISUAL_FONT", "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc")

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter, Message, MessageSegment

nonebot.init()
driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

# ── config ──────────────────────────────────────────────
USER_ID = 1261957634
IMG_NAME = "2026年7月8日 USTCのAgent交流小队_1072225932.png"
PDF_NAME = "2026年7月8日 USTCのAgent交流小队_1072225932.pdf"
REPORTS_DIR = Path("/home/ubuntu/qq-reminder-bot/data/reports")

from plugins.message_collector import export_file_path, export_file_url

# Copy report files to export dir so they're reachable via HTTP
for fname in (IMG_NAME, PDF_NAME):
    src = REPORTS_DIR / fname
    dst = export_file_path(fname)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(src.read_bytes())

img_url = export_file_url(IMG_NAME)
pdf_url = export_file_url(PDF_NAME)

# ── lifecycle handlers ──────────────────────────────────

async def _send():
    """Wait briefly for adapter registration, then send the report."""
    await asyncio.sleep(3)
    try:
        bot = nonebot.get_bot()
    except Exception as exc:
        print(f"[FAIL] get_bot(): {exc}")
        driver.stop()
        return

    print(f"[SEND] Sending image to {USER_ID}...")
    try:
        await bot.call_api(
            "send_private_msg",
            user_id=USER_ID,
            message=Message(MessageSegment.image(file=img_url)),
        )
        print("[OK] Image sent")
    except Exception as exc:
        print(f"[FAIL] image send: {exc}")

    try:
        await bot.call_api(
            "send_private_msg",
            user_id=USER_ID,
            message=f"群 USTCのAgent交流小队 2026-07-08 长图如上。PDF 文件留档见文件传输。",
        )
        print("[OK] Text sent")
    except Exception as exc:
        print(f"[FAIL] text send: {exc}")

    try:
        await bot.call_api(
            "upload_private_file",
            user_id=USER_ID,
            file=pdf_url,
            name=PDF_NAME,
        )
        print("[OK] PDF uploaded")
    except Exception as exc:
        print(f"[FAIL] pdf upload: {exc}")

    print("[DONE] Sending complete, stopping driver.")
    driver.stop()


@driver.on_startup
async def _on_startup():
    print("[INIT] NoneBot started, waiting for NapCat connection...")
    asyncio.create_task(_send())

# ── run ──────────────────────────────────────────────────
if __name__ == "__main__":
    print("[RUN] Starting NoneBot (will exit automatically after send)...")
    nonebot.run()
