import logging
import time
import sys
import os
import httpx
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

def _get_env_or_config(key: str, default="") -> str:
    """Safely get config value from environment or dotenv files."""
    from os import environ
    val = environ.get(key)
    if val:
        return val.strip()
    
    # Try manual .env parser fallback
    for dotenv_path in (".env", "AryaPremium/.env", "config.env", "AryaPremium/config.env"):
        try:
            if os.path.exists(dotenv_path):
                with open(dotenv_path, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            if k.strip() == key:
                                return v.strip().strip("'").strip('"')
        except Exception:
            pass
    return default

def _get_target_channel() -> str:
    """Resolves the logs channel ID strictly for premium bans, avoiding fallback to other logs channels."""
    ch = _get_env_or_config("PREMIUM_BAN_LOGS_CHANNEL")
    if ch:
        return ch
    return ""

async def _send_plain_text_log(text: str) -> None:
    """Sends log text to the resolved channel. Safely falls back to direct HTTP requests."""
    ch_id = _get_target_channel()
    if not ch_id:
        logger.warning("[BanLogger] No target channel resolved. Skipping log.")
        return

    # Normalize chat ID
    target_chat_id = str(ch_id).strip()
    if target_chat_id.startswith('-') or target_chat_id.startswith('+'):
        if target_chat_id[1:].isdigit():
            target_chat_id = int(target_chat_id)
    elif target_chat_id.isdigit():
        target_chat_id = int(target_chat_id)

    # 1. Try Pyrogram Client (Main Bot)
    bot_client = None
    if 'bot' in sys.modules:
        bot_client = getattr(sys.modules['bot'], 'BOT_INSTANCE', None)
    if not bot_client:
        try:
            import bot as _bot
            bot_client = getattr(_bot, 'BOT_INSTANCE', None)
        except Exception:
            pass

    if bot_client and getattr(bot_client, "is_connected", False):
        try:
            await bot_client.send_message(
                chat_id=target_chat_id,
                text=text,
                parse_mode='html',
                disable_web_page_preview=True
            )
            return
        except Exception as e:
            logger.warning(f"[BanLogger] Pyrogram main bot delivery failed: {e}. Trying fallback...")

    # 2. Try Premium Management Bot Client
    mgmt_client = None
    if 'AryaPremium.database' in sys.modules:
        mgmt_client = getattr(sys.modules['AryaPremium.database'].db, 'mgmt_client', None)
    if mgmt_client and getattr(mgmt_client, "is_connected", False):
        try:
            await mgmt_client.send_message(
                chat_id=target_chat_id,
                text=text,
                parse_mode='html',
                disable_web_page_preview=True
            )
            return
        except Exception as e:
            logger.warning(f"[BanLogger] Premium mgmt bot delivery failed: {e}. Trying fallback...")

    # 3. Direct Asynchronous HTTP API Fallback (Fail-safe for serverless/Vercel)
    bot_token = _get_env_or_config("BOT_TOKEN") or _get_env_or_config("MGMT_BOT_TOKEN")
    if bot_token:
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = {
                "chat_id": str(ch_id),
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            }
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=payload, timeout=5.0)
                if resp.status_code == 200:
                    return
                else:
                    logger.warning(f"[BanLogger] HTTP fallback rejected: {resp.status_code} - {resp.text}")
        except Exception as http_err:
            logger.error(f"[BanLogger] HTTP fallback failed: {http_err}")

    logger.error(f"[BanLogger] All notification methods failed for message: {text[:100]}...")


# ── Unified Ban/Unban Logger (STRICTLY NO EMOJIS) ──
async def log_premium_ban_event(
    user_id: int,
    name: str,
    action: str,  # "BANNED" | "UNBANNED"
    reason: str,
    ips: list = None
) -> None:
    """Logs a ban/unban control event."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    ip_str = ", ".join(ips) if ips else "None"
    
    text = (
        f"PREMIUM BAN SYSTEM EVENT\n"
        f"------------------------\n"
        f"Action: {action}\n"
        f"User ID: {user_id}\n"
        f"Name: {name}\n"
        f"Reason: {reason}\n"
        f"IP Addresses: {ip_str}\n"
        f"Timestamp: {ts}\n"
        f"------------------------\n"
        f"Status: Executed successfully"
    )
    
    # Wrap in HTML code block to ensure clean plain-text styling in Telegram
    html_text = f"<pre>{text}</pre>"
    await _send_plain_text_log(html_text)


# ── Banned User Access Attempt Logger (STRICTLY NO EMOJIS) ──
async def log_premium_ban_activity(
    user_id: int,
    name: str,
    ip: str,
    action: str,  # e.g., "App Open (/api/stories)"
    reason: str
) -> None:
    """Logs a blocked access attempt by a banned user or blocked IP address."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    
    text = (
        f"BLOCKED ACCESS ATTEMPT\n"
        f"------------------------\n"
        f"User ID: {user_id or 'Unknown'}\n"
        f"Name: {name or 'Unknown'}\n"
        f"IP Address: {ip}\n"
        f"Attempted: {action}\n"
        f"Block Reason: {reason}\n"
        f"Timestamp: {ts}\n"
        f"------------------------\n"
        f"Status: Blocked strictly"
    )
    
    html_text = f"<pre>{text}</pre>"
    await _send_plain_text_log(html_text)
