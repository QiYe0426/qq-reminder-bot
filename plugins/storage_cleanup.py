from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from nonebot import get_driver, on_fullmatch
from nonebot.adapters.onebot.v11 import Event, Message
from nonebot.log import logger

from plugins.access_control import admin_denial


load_dotenv(".env.local")

# ── retention config (days) ──────────────────────────────────────────
RETENTION_MESSAGES = int(os.getenv("CLEANUP_MESSAGE_RETENTION_DAYS", "30"))
RETENTION_REMINDERS = int(os.getenv("CLEANUP_REMINDER_RETENTION_DAYS", "30"))
RETENTION_REMOTE_APPROVAL = int(os.getenv("CLEANUP_REMOTE_APPROVAL_RETENTION_DAYS", "7"))
RETENTION_MEDIA = int(os.getenv("CLEANUP_MEDIA_RETENTION_DAYS", "7"))
RETENTION_EXPORTS = int(os.getenv("CLEANUP_EXPORT_RETENTION_DAYS", "7"))
RETENTION_SEMANTIC_GRAPHS = int(os.getenv("CLEANUP_SEMANTIC_GRAPH_RETENTION_DAYS", "7"))
RETENTION_AVATARS = int(os.getenv("CLEANUP_AVATAR_RETENTION_DAYS", "7"))
RETENTION_KEYWORD_USAGE = int(os.getenv("CLEANUP_KEYWORD_USAGE_RETENTION_DAYS", "30"))
RETENTION_DAILY_REPORT_RUNS = int(os.getenv("CLEANUP_DAILY_REPORT_RUN_RETENTION_DAYS", "90"))
RETENTION_REPORTS = int(os.getenv("CLEANUP_REPORT_RETENTION_DAYS", "90"))
RETENTION_SEMANTIC_GRAPH_RECORDS = int(os.getenv("CLEANUP_SEMANTIC_GRAPH_RECORDS_DAYS", "90"))

# ── paths (mirrors storage_status.py) ─────────────────────────────────
DATA_DIR = Path("data")
MESSAGE_ARCHIVE_DB = DATA_DIR / "message_archive.db"
REMINDERS_DB = DATA_DIR / "reminders.db"
REMOTE_APPROVAL_DB = DATA_DIR / "remote_approvals.db"
BOT_SETTINGS_DB = DATA_DIR / "bot_settings.db"
KEYWORD_RETORT_DB = DATA_DIR / "keyword_retorts.db"
COMPANION_MEMORY_DB = DATA_DIR / "companion_memory.db"
SEMANTIC_GRAPH_DB = DATA_DIR / "semantic_graph.db"
MEDIA_DIR = DATA_DIR / "media"
EXPORTS_DIR = DATA_DIR / "exports"
REPORTS_DIR = DATA_DIR / "reports"
SEMANTIC_GRAPHS_DIR = DATA_DIR / "semantic_graphs"
AVATAR_DIR = DATA_DIR / "admin_console" / "group_avatars"

# ── scheduler ─────────────────────────────────────────────────────────
driver = get_driver()
scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

# ── admin command ─────────────────────────────────────────────────────
cleanup_cmd = on_fullmatch(("清理存储", "存储清理", "数据清理"), priority=5, block=True)


# ── helpers ───────────────────────────────────────────────────────────
def _cutoff(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _file_mtime_days(path: Path) -> float:
    """Return how many days old a file is by mtime."""
    age = time.time() - path.stat().st_mtime
    return age / 86400.0


# ── cleanup routines ──────────────────────────────────────────────────

async def _clean_collected_messages() -> tuple[str, int]:
    """Delete collected_messages and orphaned insights older than retention."""
    if not MESSAGE_ARCHIVE_DB.exists():
        return ("消息归档库不存在", 0)
    cutoff = _cutoff(RETENTION_MESSAGES)
    async with aiosqlite.connect(MESSAGE_ARCHIVE_DB) as db:
        # delete old messages
        cursor = await db.execute(
            "DELETE FROM collected_messages WHERE created_at < ?",
            (cutoff,),
        )
        msg_deleted = cursor.rowcount

        # delete orphaned insights (no matching message)
        cursor = await db.execute(
            "DELETE FROM message_insights "
            "WHERE message_archive_id NOT IN "
            "(SELECT id FROM collected_messages)"
        )
        insight_deleted = cursor.rowcount

        # also delete old insights by created_at (backup)
        cursor = await db.execute(
            "DELETE FROM message_insights WHERE created_at < ?",
            (cutoff,),
        )
        insight_old = cursor.rowcount
        if insight_old > insight_deleted:
            insight_deleted = insight_old

        await db.commit()
        await db.execute("VACUUM")

    total = msg_deleted + insight_deleted
    return (f"消息归档：删除了 {msg_deleted} 条旧消息、{insight_deleted} 条旧识别记录", total)


async def _clean_reminders() -> tuple[str, int]:
    """Delete done reminders older than retention."""
    if not REMINDERS_DB.exists():
        return ("提醒库不存在", 0)
    cutoff = _cutoff(RETENTION_REMINDERS)
    async with aiosqlite.connect(REMINDERS_DB) as db:
        cursor = await db.execute(
            "DELETE FROM reminders WHERE done = 1 AND created_at < ?",
            (cutoff,),
        )
        deleted = cursor.rowcount
        await db.commit()
        if deleted:
            await db.execute("VACUUM")
    return (f"提醒库：清除了 {deleted} 条已完成的旧提醒", deleted)


async def _clean_remote_approvals() -> tuple[str, int]:
    """Delete non-pending remote approval requests older than retention."""
    if not REMOTE_APPROVAL_DB.exists():
        return ("远程审批库不存在", 0)
    cutoff = _cutoff(RETENTION_REMOTE_APPROVAL)
    async with aiosqlite.connect(REMOTE_APPROVAL_DB) as db:
        cursor = await db.execute(
            "DELETE FROM remote_approval_requests "
            "WHERE status != 'pending' AND created_at < ?",
            (cutoff,),
        )
        deleted = cursor.rowcount
        await db.commit()
        if deleted:
            await db.execute("VACUUM")
    return (f"远程审批：清除了 {deleted} 条旧记录", deleted)


async def _clean_keyword_usage() -> tuple[str, int]:
    """Delete old keyword retort usage records."""
    if not KEYWORD_RETORT_DB.exists():
        return ("关键词回怼库不存在", 0)
    cutoff = _cutoff(RETENTION_KEYWORD_USAGE)
    async with aiosqlite.connect(KEYWORD_RETORT_DB) as db:
        cursor = await db.execute(
            "DELETE FROM keyword_retort_usage WHERE created_at < ?",
            (cutoff,),
        )
        deleted = cursor.rowcount
        await db.commit()
        if deleted:
            await db.execute("VACUUM")
    return (f"关键词回怼：清除了 {deleted} 条旧使用记录", deleted)


async def _clean_daily_report_runs() -> tuple[str, int]:
    """Delete old daily report run records."""
    if not BOT_SETTINGS_DB.exists():
        return ("功能设置库不存在", 0)
    cutoff = _cutoff(RETENTION_DAILY_REPORT_RUNS)
    async with aiosqlite.connect(BOT_SETTINGS_DB) as db:
        cursor = await db.execute(
            "DELETE FROM daily_report_runs WHERE updated_at < ?",
            (cutoff,),
        )
        deleted = cursor.rowcount
        await db.commit()
        if deleted:
            await db.execute("VACUUM")
    return (f"日报运行记录：清除了 {deleted} 条旧记录", deleted)


async def _clean_semantic_graph_records() -> tuple[str, int]:
    """Delete old semantic graph edge records."""
    if not SEMANTIC_GRAPH_DB.exists():
        return ("语义图库不存在", 0)
    cutoff = _cutoff(RETENTION_SEMANTIC_GRAPH_RECORDS)
    async with aiosqlite.connect(SEMANTIC_GRAPH_DB) as db:
        # check if graph_edges table exists
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='graph_edges'"
        )
        if not await cursor.fetchone():
            return ("语义图库无 graph_edges 表", 0)
        cursor = await db.execute(
            "DELETE FROM graph_edges WHERE updated_at < ?",
            (cutoff,),
        )
        deleted = cursor.rowcount
        await db.commit()
        if deleted:
            await db.execute("VACUUM")
    return (f"语义图：清除了 {deleted} 条旧记录", deleted)


def _clean_directory(dir_path: Path, days: int, label: str) -> tuple[str, int]:
    """Delete files in a directory older than N days (recursive)."""
    if not dir_path.is_dir():
        return (f"{label}：目录不存在", 0)
    deleted = 0
    freed_bytes = 0
    for item in list(dir_path.rglob("*")):
        if item.is_file() and _file_mtime_days(item) > days:
            try:
                freed_bytes += item.stat().st_size
                item.unlink(missing_ok=True)
                deleted += 1
            except (OSError, PermissionError):
                pass
    if deleted == 0:
        return (f"{label}：无过期文件", 0)
    freed_mb = freed_bytes / (1024 * 1024)
    return (f"{label}：清理了 {deleted} 个文件（释放 {freed_mb:.1f} MB）", deleted)


# ── main cleanup entry ────────────────────────────────────────────────

async def run_cleanup() -> str:
    """Run all cleanup routines and return a summary."""
    logger.info("开始数据自动清理...")
    results: list[str] = []
    total = 0

    # databases
    for coro in (
        _clean_collected_messages,
        _clean_reminders,
        _clean_remote_approvals,
        _clean_keyword_usage,
        _clean_daily_report_runs,
        _clean_semantic_graph_records,
    ):
        try:
            msg, count = await coro()
            results.append(msg)
            total += count
        except Exception as exc:
            logger.exception("清理任务异常")
            results.append(f"[错误] {exc}")

    # directories
    dir_cleanups = [
        (MEDIA_DIR, RETENTION_MEDIA, "媒体缓存"),
        (EXPORTS_DIR, RETENTION_EXPORTS, "导出目录"),
        (REPORTS_DIR, RETENTION_REPORTS, "日报导出"),
        (SEMANTIC_GRAPHS_DIR, RETENTION_SEMANTIC_GRAPHS, "语义图渲染"),
        (AVATAR_DIR, RETENTION_AVATARS, "群头像缓存"),
    ]
    for dir_path, days, label in dir_cleanups:
        try:
            msg, count = _clean_directory(dir_path, days, label)
            results.append(msg)
            total += count
        except Exception as exc:
            results.append(f"[错误] {label}: {exc}")

    summary = f"数据清理完成。共处理 {total} 条/个。\n" + "\n".join(results)
    logger.info(summary)
    return summary


# ── scheduled job ─────────────────────────────────────────────────────

@driver.on_startup
async def startup() -> None:
    scheduler.add_job(
        run_cleanup,
        "cron",
        hour=3,
        minute=0,
        id="daily_storage_cleanup",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("存储清理定时任务已注册（每天 03:00）")


@driver.on_shutdown
async def shutdown() -> None:
    scheduler.shutdown(wait=False)


# ── admin command ─────────────────────────────────────────────────────

@cleanup_cmd.handle()
async def handle_cleanup(event: Event) -> None:
    if denial := admin_denial(event):
        await cleanup_cmd.finish(Message(denial))
    await cleanup_cmd.send(Message("开始清理，请稍候..."))
    summary = await run_cleanup()
    await cleanup_cmd.finish(Message(summary))
