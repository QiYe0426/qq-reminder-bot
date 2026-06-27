from __future__ import annotations

import shutil
from pathlib import Path

import aiosqlite
from nonebot import on_fullmatch
from nonebot.adapters.onebot.v11 import Event, Message

from plugins.access_control import admin_denial


DATA_DIR = Path("data")
REMINDERS_DB = DATA_DIR / "reminders.db"
MESSAGE_ARCHIVE_DB = DATA_DIR / "message_archive.db"
BOT_SETTINGS_DB = DATA_DIR / "bot_settings.db"
REPORTS_DIR = DATA_DIR / "reports"
MEDIA_DIR = DATA_DIR / "media"
EXPORTS_DIR = DATA_DIR / "exports"

storage_status = on_fullmatch(("存储状态", "硬盘状态", "空间状态"), priority=5, block=True)


def format_size(size: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)}{unit}"
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{size}B"


def file_size(path: Path) -> int:
    if not path.exists() or not path.is_file():
        return 0
    return path.stat().st_size


def sqlite_size(path: Path) -> int:
    return sum(
        file_size(related_path)
        for related_path in (
            path,
            Path(f"{path}-wal"),
            Path(f"{path}-shm"),
            Path(f"{path}-journal"),
        )
    )


def directory_size(path: Path) -> int:
    if not path.exists() or not path.is_dir():
        return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


async def table_count(db_path: Path, table_name: str) -> int | None:
    if not db_path.exists():
        return None
    try:
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(f"SELECT COUNT(*) FROM {table_name}")
            row = await cursor.fetchone()
    except Exception:
        return None
    if row is None:
        return None
    return int(row[0])


async def storage_status_text() -> str:
    message_count = await table_count(MESSAGE_ARCHIVE_DB, "collected_messages")
    reminder_count = await table_count(REMINDERS_DB, "reminders")

    reminders_size = sqlite_size(REMINDERS_DB)
    archive_size = sqlite_size(MESSAGE_ARCHIVE_DB)
    settings_size = sqlite_size(BOT_SETTINGS_DB)
    reports_size = directory_size(REPORTS_DIR)
    media_size = directory_size(MEDIA_DIR)
    exports_size = directory_size(EXPORTS_DIR)
    data_size = directory_size(DATA_DIR)

    usage = shutil.disk_usage(Path.cwd())
    used_ratio = usage.used / usage.total * 100 if usage.total else 0

    lines = [
        "存储状态",
        f"服务器磁盘：已用 {format_size(usage.used)} / {format_size(usage.total)} ({used_ratio:.1f}%)",
        f"剩余空间：{format_size(usage.free)}",
        f"data目录：{format_size(data_size)}",
        f"消息数据库：{format_size(archive_size)}"
        + (f"（{message_count} 条）" if message_count is not None else ""),
        f"提醒数据库：{format_size(reminders_size)}"
        + (f"（{reminder_count} 条）" if reminder_count is not None else ""),
        f"功能设置库：{format_size(settings_size)}",
        f"日报目录：{format_size(reports_size)}",
        f"媒体缓存：{format_size(media_size)}",
        f"导出目录：{format_size(exports_size)}",
    ]
    return "\n".join(lines)


@storage_status.handle()
async def handle_storage_status(event: Event) -> None:
    if denial := admin_denial(event):
        await storage_status.finish(Message(denial))
    await storage_status.finish(Message(await storage_status_text()))
