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
from typing import Optional, Union, Any
from pyrogram import enums

logger = logging.getLogger(__name__)

# ── Direct bot reference — set via register_bot() called from bot.py start() ──
# This avoids the fragile sys.modules lookup that was causing all logs to silently fail.
_BOT_REF = None   # type: ignore

def register_bot(bot_instance) -> None:
    """
    Called once from bot.py's start() to register the main bot client.
    After this, all log channels are reachable.
    """
    global _BOT_REF
    _BOT_REF = bot_instance
    logger.info("[AryaLog] Bot registered successfully — log channels are now active.")


def _get_bot():
    """
    Returns the best available bot client for sending log messages.
    Priority: main bot → any running share/delivery bot client.
    """
    global _BOT_REF

    def _is_connected(cli) -> bool:
        """Safely check if a Pyrogram client is connected."""
        if not cli:
            return False
        try:
            # is_connected is a property in modern Pyrogram — call it directly
            return bool(cli.is_connected)
        except Exception:
            # If it raises (e.g. old version / not started), assume connected
            # so we at least try to send and get a real error back
            return True

    if _BOT_REF and _is_connected(_BOT_REF):
        return _BOT_REF

    # Fallback: use any running delivery bot (share_clients)
    try:
        from plugins.share_bot import share_clients
        for cli in share_clients.values():
            if cli and _is_connected(cli):
                return cli
    except Exception:
        pass

    # Last resort: return _BOT_REF even if not confirmed connected
    # (Pyrogram's send_message will raise if truly disconnected)
    return _BOT_REF


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
        # Only update timestamp on SUCCESS — so a DB failure doesn't poison
        # the cache for 60s and cause all startup logs to be silently dropped.
        _cfg_ts = now
    except Exception as e:
        logger.warning(f"[AryaLog] Could not load logs config: {e}. Will retry on next log call.")
        # Do NOT update _cfg_ts here — force an immediate retry next call
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
            # Channel not configured — skip silently
            logger.debug(f"[AryaLog] ch_key='{ch_key}' has no channel configured (value={ch_id!r}), skipping.")
            return

        bot = _get_bot()
        if not bot:
            logger.warning(f"[AryaLog] No bot client available yet — cannot send log to {ch_key} ({ch_id}). "
                           "Ensure register_bot() is called from bot.py start().")
            return

        # Parse channel ID (support integer IDs and @usernames)
        target_chat_id: any = str(ch_id).strip()
        stripped = target_chat_id.lstrip('+-')
        if stripped.isdigit():
            target_chat_id = int(str(ch_id).strip())

        try:
            await bot.send_message(
                chat_id=target_chat_id,
                text=text,
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True,
            )
            logger.debug(f"[AryaLog] Log sent to {ch_key} ({target_chat_id})")
        except Exception as send_err:
            err_str = str(send_err).upper()
            # PeerIdInvalid / ChannelInvalid → warm peer cache and retry once
            if any(k in err_str for k in ("PEER_ID_INVALID", "CHANNEL_INVALID", "CHAT_NOT_FOUND")):
                logger.warning(f"[AryaLog] Peer not in cache for {target_chat_id}, warming and retrying...")
                try:
                    from plugins.utils import safe_resolve_peer
                    await safe_resolve_peer(bot, target_chat_id)
                    await bot.send_message(
                        chat_id=target_chat_id,
                        text=text,
                        parse_mode=enums.ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                    logger.info(f"[AryaLog] Retry succeeded for {ch_key} ({target_chat_id})")
                except Exception as retry_err:
                    logger.error(f"[AryaLog] Retry also failed for {ch_key} ({target_chat_id}): {retry_err}")
            else:
                logger.error(f"[AryaLog] Send failed for {ch_key} ({target_chat_id}): {send_err}")

    except Exception as outer_err:
        logger.error(f"[AryaLog] Unexpected error in _send({ch_key}): {outer_err}", exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
# Public log functions
# ─────────────────────────────────────────────────────────────────────────────


def _ist_str() -> str:
    from datetime import datetime, timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%Y-%m-%d %I:%M:%S %p IST")

async def log_ban(
    user_id: int,
    user_name: str,
    strike_count: int,
    bot_name: str,
    bot_id: str,
    reason: str = "Rapid bulk file requests"
) -> None:
    """Send a ban log entry to the Bans & Warnings channel."""
    import time as _t
    ts = _ist_str()
    text = (
        f"<b>🚫 SILENT BAN — Abuse Detected</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>User:</b> <a href='tg://user?id={user_id}'>{_esc(user_name)}</a>  "
        f"[<code>{user_id}</code>]\n"
        f"<b>Strike:</b> {strike_count} (auto-ban on strike {strike_count})\n"
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
    """Send a warning log entry (strike 1 or 2) to the Bans & Warnings channel."""
    import time as _t
    ts = _ist_str()
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
    ts = _ist_str()
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
    ts = _ist_str()
    story_line = f"<b>Story:</b> {_esc(story)}\n" if story else ""
    range_line  = f"<b>Range:</b> {_esc(ep_range)}\n" if ep_range else ""
    text = (
        f"<b>🔗 Batch Link Created</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{story_line}"
        f"{range_line}"
        f"<b>UUID:</b> <code>{uuid}</code>\n"
        f"<b>Source Chat:</b> <code>{source_chat}</code>\n"
        f"<b>File Count:</b> {len(msg_ids)}\n"
        f"<b>Bot:</b> {_esc(bot_name)}\n"
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
    ts = _ist_str()
    uid_line = f"<b>Owner:</b> <a href='tg://user?id={user_id}'>{user_id}</a>\n" if user_id else ""
    text = (
        f"<b>⚡ Live Job Active</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Job ID:</b> <code>{_esc(job_id)}</code>\n"
        f"<b>Source:</b> <code>{_esc(source)}</code>\n"
        f"<b>Dest:</b> <code>{_esc(dest)}</code>\n"
        f"{uid_line}"
        f"<b>Time:</b> <code>{ts}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
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
    ts = _ist_str()
    STATUS_ICON = {
        "started":   "🔄",
        "completed": "✅",
        "failed":    "❌",
        "paused":    "⏸",
    }
    icon = STATUS_ICON.get(status, "❓")
    uid_line = f"<b>Owner:</b> <a href='tg://user?id={user_id}'>{user_id}</a>\n" if user_id else ""
    err_line = f"<b>Error:</b> <code>{_esc(str(error)[:200])}</code>\n" if error else ""
    text = (
        f"<b>{icon} Cleaner Job {status.title()}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
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
    ts = _ist_str()
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






async def log_live_batch_post(
    job_id: str,
    files_in_batch: int,
    total_forwarded: int,
    source: str,
) -> None:
    """Log when a Live Job successfully processes a batch of files."""
    ts = _ist_str()
    text = (
        f"<b>⚡ Live Job Progress</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Job ID:</b> <code>{_esc(job_id)}</code>\n"
        f"<b>Source:</b> <code>{_esc(source)}</code>\n"
        f"<b>Status:</b> Just processed a batch of <b>{files_in_batch}</b> files.\n"
        f"<b>Total Forwarded:</b> <code>{total_forwarded}</code>\n"
        f"<b>Time:</b> <code>{ts}</code>"
    )
    await _send(text, 'ch_live')

# ---------- ADMIN DM ALERTS ----------
_admin_dm_cache = {}

async def log_admin_dm(error_type: str, details: str) -> None:
    """
    Sends a direct message to the bot owners/admins for critical errors like FloodWait.
    Uses an in-memory cache to prevent spamming the same error type within 10 minutes.
    """
    global _admin_dm_cache
    now = time.time()
    
    # Rate limit: max 1 DM per error_type every 10 minutes
    if error_type in _admin_dm_cache and (now - _admin_dm_cache[error_type]) < 600:
        return
        
    _admin_dm_cache[error_type] = now

    bot = _get_bot()
    if not bot:
        return

    try:
        from AryaPremium.config import Config
        owners = getattr(Config, 'OWNER_IDS', [])
        if not owners:
            return
            
        ts = _ist_str()
        text = (
            f"⚠️ <b>CRITICAL ADMIN ALERT</b> ⚠️\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Type:</b> <code>{_esc(error_type)}</code>\n"
            f"<b>Details:</b> <code>{_esc(details[:800])}</code>\n"
            f"<b>Time:</b> <code>{ts}</code>"
        )
        
        for owner_id in owners:
            try:
                await bot.send_message(chat_id=owner_id, text=text)
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"[AryaLog] Failed to send admin DM: {e}")


async def log_pass_purchased(
    user_id: int,
    user_name: str,
    duration_str: str = "",
    amount: float = 0.0,
    order_id: str = "",
    expiry_ts: float = 0.0,
    log_channel: Optional[int] = None,
    gateway: str = "Cashfree PG",
    days: int = 1,
    tier: str = "basic",
    checkout_version: str = "v1"
) -> None:
    """Log when a user purchases an Unlimited Delivery Pass in Quoteblock format."""
    now_str = _ist_str()
    
    # Format expiry in IST
    import datetime
    try:
        import pytz
        ist_tz = pytz.timezone('Asia/Kolkata')
        exp_dt = datetime.datetime.fromtimestamp(expiry_ts, tz=ist_tz)
        exp_str = exp_dt.strftime('%d-%m-%Y %I:%M %p')
    except Exception:
        exp_str = datetime.datetime.fromtimestamp(expiry_ts).strftime('%d-%m-%Y %I:%M %p')

    plan_display = duration_str if duration_str else (f"{days} Day(s)" if str(days).isdigit() else str(days))
    t_clean = str(tier or 'basic').strip().lower()
    if t_clean == 'pro':
        tier_display = "👑 PRO PASS"
    elif t_clean == 'premium':
        tier_display = "💎 PREMIUM PASS"
    else:
        tier_display = "⚡ BASIC PASS"

    cv_display = str(checkout_version or "V1").upper()

    text = (
        f"<blockquote><b>UNLIMITED ACCESS PASS PURCHASED</b>\n"
        f"────────────────────\n"
        f"<b>Name:</b> {_esc(user_name)}\n"
        f"<b>User ID:</b> <code>{user_id}</code>\n"
        f"<b>Tier:</b> <b>{tier_display}</b>\n"
        f"<b>Checkout Version:</b> <code>{cv_display}</code>\n"
        f"<b>Plan:</b> {plan_display} Unlimited Access\n"
        f"<b>Amount:</b> ₹{amount:.2f}\n"
        f"<b>Gateway:</b> {_esc(gateway)}\n"
        f"<b>Order ID:</b> <code>{_esc(order_id)}</code>\n"
        f"<b>Valid Until:</b> <code>{exp_str}</code>\n"
        f"<b>Purchased At:</b> <code>{now_str}</code></blockquote>"
    )

    if log_channel:
        try:
            target_chat_id = int(str(log_channel).strip())
            candidate_bots = []
            primary_bot = _get_bot()
            if primary_bot:
                candidate_bots.append(primary_bot)
            try:
                from plugins.share_bot import share_clients
                if share_clients:
                    for s_cli in share_clients.values():
                        if s_cli and s_cli not in candidate_bots:
                            candidate_bots.append(s_cli)
            except Exception:
                pass

            for b in candidate_bots:
                try:
                    await b.send_message(
                        chat_id=target_chat_id,
                        text=text,
                        parse_mode=enums.ParseMode.HTML,
                        disable_web_page_preview=True
                    )
                    return
                except Exception as send_err:
                    err_str = str(send_err).upper()
                    if any(k in err_str for k in ("PEER_ID_INVALID", "CHANNEL_INVALID", "CHAT_NOT_FOUND")):
                        try:
                            from plugins.utils import safe_resolve_peer
                            await safe_resolve_peer(b, target_chat_id)
                            await b.send_message(
                                chat_id=target_chat_id,
                                text=text,
                                parse_mode=enums.ParseMode.HTML,
                                disable_web_page_preview=True
                            )
                            return
                        except Exception:
                            pass
        except Exception as e:
            logger.warning(f"[AryaLog] Failed to send pass purchase log to custom channel {log_channel}: {e}")

    # Fallback to general share bot log channel if no dedicated pass log channel
    await _send(text, 'ch_share')


async def log_rate_limit_reached(
    user_id: int,
    user_name: str,
    username: Optional[str] = None,
    hits_count: int = 5,
    max_limit: int = 5,
    window_str: str = "12 hours",
    cooldown_str: str = "11h 59m",
    bot_name: Optional[str] = None,
    bot_username: Optional[str] = None,
    log_channel: Optional[int] = None
) -> None:
    """Log when a user reaches their free delivery rate limit in Quoteblock format."""
    now_str = _ist_str()
    uname_str = f"@{username}" if username else "None"
    bot_display = f"@{bot_username}" if bot_username else (bot_name or "Delivery Bot")

    text = (
        f"<blockquote><b>RATE LIMIT REACHED</b>\n"
        f"────────────────────\n"
        f"<b>Name:</b> {_esc(user_name)}\n"
        f"<b>User ID:</b> <code>{user_id}</code>\n"
        f"<b>Username:</b> {uname_str}\n"
        f"<b>Deliveries Accessed:</b> <code>{hits_count} / {max_limit}</code>\n"
        f"<b>Window:</b> <code>{window_str}</code>\n"
        f"<b>Cooldown Left:</b> <code>{cooldown_str}</code>\n"
        f"<b>Triggered On:</b> {_esc(bot_display)}\n"
        f"<b>Triggered At:</b> <code>{now_str}</code></blockquote>"
    )

    if log_channel:
        try:
            target_chat_id = int(str(log_channel).strip())
            candidate_bots = []
            primary_bot = _get_bot()
            if primary_bot:
                candidate_bots.append(primary_bot)
            try:
                from plugins.share_bot import share_clients
                if share_clients:
                    for s_cli in share_clients.values():
                        if s_cli and s_cli not in candidate_bots:
                            candidate_bots.append(s_cli)
            except Exception:
                pass

            for b in candidate_bots:
                try:
                    await b.send_message(
                        chat_id=target_chat_id,
                        text=text,
                        parse_mode=enums.ParseMode.HTML,
                        disable_web_page_preview=True
                    )
                    return
                except Exception as send_err:
                    err_str = str(send_err).upper()
                    if any(k in err_str for k in ("PEER_ID_INVALID", "CHANNEL_INVALID", "CHAT_NOT_FOUND")):
                        try:
                            from plugins.utils import safe_resolve_peer
                            await safe_resolve_peer(b, target_chat_id)
                            await b.send_message(
                                chat_id=target_chat_id,
                                text=text,
                                parse_mode=enums.ParseMode.HTML,
                                disable_web_page_preview=True
                            )
                            return
                        except Exception:
                            pass
        except Exception as e:
            logger.warning(f"[AryaLog] Failed to send rate limit log to custom channel {log_channel}: {e}")

    # Fallback to general share bot log channel if configured
    await _send(text, 'ch_share')


