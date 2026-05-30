"""
arya_logger.py — Centralized Log Sender for Arya Forward Bot Ecosystem
=======================================================================
Sends structured log messages to independently configured Telegram channels.
Each log category has its OWN separate channel — no supergroups, no topic IDs.

Channel keys (configured in Settings → Share Bot → Logs):
  ch_bans      → Bans & Warnings
  ch_new_users → New Users (per delivery bot)
  ch_batch     → Batch Link Creation
  ch_live      → Live Job Events
  ch_cleaner   → Cleaner Job Events
  ch_errors    → Error Alerts

All public functions are safe to call with `asyncio.create_task()` —
they never raise exceptions, so log failures never affect core bot logic.

Usage:
  import plugins.arya_logger as arya_log
  asyncio.create_task(arya_log.log_ban(...))
  asyncio.create_task(arya_log.log_new_user(...))
  asyncio.create_task(arya_log.log_error(...))
"""

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ── Internal: cache logs config in memory to avoid DB hit per log call ─────
_cfg_cache: dict = {}
_cfg_ts: float = 0.0
_CFG_TTL: float = 60.0   # re-read DB config at most once per minute


async def _get_cfg() -> dict:
    """Return logs config (ch_bans, ch_new_users, ...), cached for 60 seconds."""
    global _cfg_cache, _cfg_ts
    now = time.time()
    if now - _cfg_ts < _CFG_TTL and _cfg_cache:
        return _cfg_cache
    try:
        from database import db
        _cfg_cache = await db.get_logs_config()
        _cfg_ts = now
    except Exception as e:
        logger.warning(f"[AryaLog] Could not load logs config: {e}")
        if not _cfg_cache:
            _cfg_cache = {
                'ch_bans':      0,
                'ch_new_users': 0,
                'ch_batch':     0,
                'ch_live':      0,
                'ch_cleaner':   0,
                'ch_errors':    0,
            }
    return _cfg_cache


def _invalidate_cfg_cache():
    """Call this after updating logs config to force an immediate re-read."""
    global _cfg_ts
    _cfg_ts = 0.0


async def _send(text: str, ch_key: str) -> None:
    """
    Internal: send `text` to the channel configured for `ch_key`.
    `ch_key` is one of: 'ch_bans', 'ch_new_users', 'ch_batch',
                        'ch_live', 'ch_cleaner', 'ch_errors'.
    Silently skips if channel is not configured (0 or missing).
    """
    try:
        cfg = await _get_cfg()
        ch_id = cfg.get(ch_key, 0)
        if not ch_id:
            return   # This log type's channel not configured — skip silently

        # Get the main bot client
        try:
            import bot as _bot
            bot = getattr(_bot, 'BOT_INSTANCE', None)
            if not bot:
                return   # Not yet initialized
        except Exception:
            return

        await bot.send_message(
            chat_id=int(ch_id),
            text=text,
            parse_mode='html',
            disable_web_page_preview=True,
        )

    except Exception as e:
        logger.debug(f"[AryaLog] Log send failed (silently suppressed): {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Public log functions
# ─────────────────────────────────────────────────────────────────────────────

async def log_ban(
    user_id: int,
    user_name: str,
    strike_count: int,
    bot_name: str,
    bot_id: str,
    reason: str = "Rapid bulk file requests"
) -> None:
    """Send a ban log entry to the Bans topic."""
    import time as _t
    ts = _t.strftime("%Y-%m-%d %H:%M:%S UTC", _t.gmtime())
    text = (
        f"<b>🚫 SILENT BAN — Abuse Detected</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>User:</b> <a href='tg://user?id={user_id}'>{_esc(user_name)}</a>  "
        f"[<code>{user_id}</code>]\n"
        f"<b>Strike:</b> {strike_count} (auto-ban on strike 3)\n"
        f"<b>Reason:</b> {_esc(reason)}\n"
        f"<b>Detected by:</b> {_esc(bot_name)} (<code>{bot_id}</code>)\n"
        f"<b>Time:</b> <code>{ts}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>User is now permanently banned across all delivery bots.</i>"
    )
    await _send(text, 'ch_bans')


async def log_warn(
    user_id: int,
    user_name: str,
    strike_count: int,
    max_strikes: int,
    bot_name: str,
    bot_id: str,
) -> None:
    """Send a warning log entry (strike 1 or 2) to the Bans topic."""
    import time as _t
    ts = _t.strftime("%Y-%m-%d %H:%M:%S UTC", _t.gmtime())
    text = (
        f"<b>⚠️ ABUSE WARNING — Strike {strike_count}/{max_strikes}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>User:</b> <a href='tg://user?id={user_id}'>{_esc(user_name)}</a>  "
        f"[<code>{user_id}</code>]\n"
        f"<b>Strike:</b> {strike_count} of {max_strikes} before ban\n"
        f"<b>Detected by:</b> {_esc(bot_name)} (<code>{bot_id}</code>)\n"
        f"<b>Time:</b> <code>{ts}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>User re-requested files within 1-minute cooldown.</i>"
    )
    await _send(text, 'ch_bans')


async def log_new_user(
    user_id: int,
    user_name: str,
    bot_name: str,
    bot_id: str,
) -> None:
    """Send a new-user log when a user starts any delivery bot for the first time."""
    import time as _t
    ts = _t.strftime("%Y-%m-%d %H:%M:%S UTC", _t.gmtime())
    text = (
        f"<b>👤 New User</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>User:</b> <a href='tg://user?id={user_id}'>{_esc(user_name)}</a>  "
        f"[<code>{user_id}</code>]\n"
        f"<b>Bot:</b> {_esc(bot_name)} (<code>{bot_id}</code>)\n"
        f"<b>Time:</b> <code>{ts}</code>"
    )
    await _send(text, 'ch_new_users')


async def log_batch_link(
    uuid: str,
    source_chat,
    msg_ids: list,
    story: str = "",
    ep_range: str = "",
    bot_name: str = "",
) -> None:
    """Log when a new share/batch link is created and saved to DB."""
    import time as _t
    ts = _t.strftime("%Y-%m-%d %H:%M:%S UTC", _t.gmtime())
    story_line = f"<b>Story:</b> {_esc(story)}\n" if story else ""
    range_line  = f"<b>Range:</b> {_esc(ep_range)}\n" if ep_range else ""
    text = (
        f"<b>\U0001f517 Batch Link Created</b>\n"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        f"{story_line}"
        f"{range_line}"
        f"<b>UUID:</b> <code>{uuid}</code>\n"
        f"<b>Source Chat:</b> <code>{source_chat}</code>\n"
        f"<b>File Count:</b> {len(msg_ids)}\n"
        f"<b>Time:</b> <code>{ts}</code>"
    )
    await _send(text, 'ch_batch')


async def log_live_job(
    job_id: str,
    source: str,
    dest: str,
    user_id: int = 0,
) -> None:
    """Log when a Live Forward job enters its live-polling phase."""
    import time as _t
    ts = _t.strftime("%Y-%m-%d %H:%M:%S UTC", _t.gmtime())
    uid_line = f"<b>Owner:</b> <a href='tg://user?id={user_id}'>{user_id}</a>\n" if user_id else ""
    text = (
        f"<b>\u26a1 Live Job Active</b>\n"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        f"<b>Job ID:</b> <code>{_esc(job_id)}</code>\n"
        f"<b>Source:</b> <code>{_esc(source)}</code>\n"
        f"<b>Dest:</b> <code>{_esc(dest)}</code>\n"
        f"{uid_line}"
        f"<b>Time:</b> <code>{ts}</code>\n"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        f"<i>Job has completed batch phase and is now monitoring for new messages.</i>"
    )
    await _send(text, 'ch_live')


async def log_cleaner_job(
    job_id: str,
    base_name: str,
    files_done: int,
    total_files: int,
    status: str,          # "started" | "completed" | "failed" | "paused"
    user_id: int = 0,
    error: str = "",
) -> None:
    """Log cleaner job lifecycle events (started, completed, failed)."""
    import time as _t
    ts = _t.strftime("%Y-%m-%d %H:%M:%S UTC", _t.gmtime())
    STATUS_ICON = {
        "started":   "\U0001f504",
        "completed": "\u2705",
        "failed":    "\u274c",
        "paused":    "\u23f8",
    }
    icon = STATUS_ICON.get(status, "\u2753")
    uid_line = f"<b>Owner:</b> <a href='tg://user?id={user_id}'>{user_id}</a>\n" if user_id else ""
    err_line = f"<b>Error:</b> <code>{_esc(str(error)[:200])}</code>\n" if error else ""
    text = (
        f"<b>{icon} Cleaner Job {status.title()}</b>\n"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        f"<b>Job ID:</b> <code>{_esc(job_id)}</code>\n"
        f"<b>Name:</b> {_esc(base_name)}\n"
        f"<b>Progress:</b> {files_done}/{total_files} files\n"
        f"{uid_line}"
        f"{err_line}"
        f"<b>Time:</b> <code>{ts}</code>"
    )
    await _send(text, 'ch_cleaner')


async def log_error(
    location: str,
    error: str,
    extra: str = "",
) -> None:
    """Log an unexpected error from anywhere in the ecosystem."""
    import time as _t
    ts = _t.strftime("%Y-%m-%d %H:%M:%S UTC", _t.gmtime())
    text = (
        f"<b>❌ Error</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Location:</b> <code>{_esc(location)}</code>\n"
        f"<b>Error:</b> <code>{_esc(str(error)[:300])}</code>\n"
        f"{('<b>Extra:</b> ' + _esc(str(extra)[:200]) + chr(10)) if extra else ''}"
        f"<b>Time:</b> <code>{ts}</code>"
    )
    await _send(text, 'ch_errors')


# ─────────────────────────────────────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────────────────────────────────────

def _esc(text: str) -> str:
    """Escape HTML special characters for safe Telegram HTML formatting."""
    if not text:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
