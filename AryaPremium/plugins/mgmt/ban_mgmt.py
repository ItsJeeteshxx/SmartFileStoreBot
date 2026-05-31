import logging
import asyncio
from datetime import datetime, timezone
from pyrogram import Client, filters
from pyrogram.types import Message
from config import Config
from database import db

logger = logging.getLogger(__name__)

# Small caps translator utility
def _sc(text: str) -> str:
    return text.translate(str.maketrans(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"
    ))

def _is_owner(user_id: int) -> bool:
    """Checks if target user is configured as an owner or co-owner."""
    return int(user_id) in set(Config.OWNER_IDS or [])

async def _resolve_target(client, message: Message):
    """
    Returns (user_id, display_name, reason) or (None, None, error_msg).
    Supports replies, /ban @username [reason], and /ban 123456789 [reason].
    """
    reply = message.reply_to_message
    if reply and reply.from_user:
        uid = reply.from_user.id
        name = reply.from_user.first_name or str(uid)
        reason_parts = message.command[1:]
        return uid, name, " ".join(reason_parts) if reason_parts else "Banned by administrator"

    if len(message.command) < 2:
        return None, None, f"Usage:\n/ban @username|user_id [reason]\nOr reply to a message and use /ban [reason]"

    target = message.command[1]
    reason = " ".join(message.command[2:]) if len(message.command) > 2 else "Banned by administrator"

    try:
        if target.startswith("@") or not target.lstrip("-").isdigit():
            user = await client.get_users(target)
            name = user.first_name or target
            return user.id, name, reason
        else:
            uid = int(target)
            try:
                user = await client.get_users(uid)
                name = user.first_name or str(uid)
            except Exception:
                name = str(uid)
            return uid, name, reason
    except Exception as e:
        return None, None, f"Could not find user: {e}"


# ── /ban Command (Management Bot) ──
@Client.on_message(filters.command("ban") & filters.private)
async def ban_user_mgmt(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else 0
    if not _is_owner(user_id):
        await message.reply_text(f"❌ {_sc('Access Denied')} (Admin Only)")
        return

    uid, name, reason = await _resolve_target(client, message)
    if uid is None:
        await message.reply_text(f"❌ {reason}")
        return

    # Safety Check: Never ban admins
    if _is_owner(uid):
        await message.reply_text(f"⛔ <b>{_sc('Cannot Ban Admin')}</b>\n\nYou cannot ban an owner or co-owner.")
        return

    # Check if already banned in premium_bans collection
    existing = await db.db.premium_bans.find_one({"_id": uid})
    if existing:
        already_reason = existing.get("reason", "No reason")
        banned_at = existing.get("banned_at")
        date_str = banned_at.strftime("%Y-%m-%d %H:%M:%S UTC") if isinstance(banned_at, datetime) else "Unknown"
        await message.reply_text(
            f"⚠️ <b>{_sc('User Already Banned')}</b>\n\n"
            f"👤 <b>{_sc('User')}:</b> <code>{uid}</code> ({name})\n"
            f"📝 <b>{_sc('Reason')}:</b> {already_reason}\n"
            f"📅 <b>{_sc('Date Banned')}:</b> {date_str}\n"
            f"🌐 <b>{_sc('IPs Blocked')}:</b> {len(existing.get('ips', []))}\n\n"
            f"<i>This user is already banned in the premium database.</i>"
        )
        return

    # Extract historical visitor IPs of target user from mini_app_analytics
    historical_ips = await db.db.mini_app_analytics.distinct("ip", {"user_id": uid})
    ips = [ip for ip in historical_ips if ip and ip not in ("unknown", "127.0.0.1", "::1") and not ip.startswith(("192.168.", "10.", "172."))]

    # Save to premium_bans collection (Blocks Mini App + tracks VPNs)
    await db.db.premium_bans.update_one(
        {"_id": uid},
        {"$set": {
            "ips": list(set(ips)),
            "reason": reason,
            "status": "banned",
            "banned_at": datetime.now(timezone.utc),
            "name": name
        }},
        upsert=True
    )

    # Save/Propagate to Delivery Bot ban list in main database (Blocks Delivery Bot)
    await db.col.update_one(
        {"id": int(uid)},
        {"$set": {"ban_status": {"is_banned": True, "ban_reason": reason}}},
        upsert=True
    )

    # Clear in-memory abuse strikes
    try:
        from plugins.share_bot import _abuse_strikes, _abuse_last_delivery
        _abuse_strikes.pop(uid, None)
        _abuse_last_delivery.pop(uid, None)
    except Exception:
        pass

    # Send plain-text log to Telegram Logs Channel (STRICTLY NO EMOJIS)
    from utils_ban_logger import log_premium_ban_event
    asyncio.create_task(log_premium_ban_event(
        user_id=uid,
        name=name,
        action="BANNED",
        reason=reason,
        ips=ips
    ))

    # Bot confirmation reply (Polished markup)
    await message.reply_text(
        f"🚫 <b>{_sc('User Banned Successfully')}</b>\n\n"
        f"👤 <b>{_sc('User')}:</b> <code>{uid}</code> ({name})\n"
        f"📝 <b>{_sc('Reason')}:</b> {reason}\n"
        f"🌐 <b>{_sc('IPs Blocked')}:</b> {len(ips)}\n\n"
        f"<i>This user is now completely banned across all delivery bots and the web mini app.</i>"
    )


# ── /unban Command (Management Bot) ──
@Client.on_message(filters.command("unban") & filters.private)
async def unban_user_mgmt(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else 0
    if not _is_owner(user_id):
        await message.reply_text(f"❌ {_sc('Access Denied')} (Admin Only)")
        return

    uid, name, _ = await _resolve_target(client, message)
    if uid is None:
        await message.reply_text(f"❌ {name}")
        return

    # Check if target is actually banned in premium_bans collection
    existing = await db.db.premium_bans.find_one({"_id": uid})
    if not existing:
        await message.reply_text(
            f"ℹ️ <b>{_sc('User Not Banned')}</b>\n\n"
            f"👤 <b>{_sc('User')}:</b> <code>{uid}</code> ({name})\n\n"
            f"<i>This user is not present in the premium ban database.</i>"
        )
        return

    # Lift ban from premium_bans collection
    await db.db.premium_bans.delete_one({"_id": uid})

    # Lift ban from Delivery Bot list in main database
    await db.col.update_one(
        {"id": int(uid)},
        {"$set": {"ban_status": {"is_banned": False, "ban_reason": ""}}}
    )

    # Reset any anti-abuse strike records
    try:
        from plugins.share_bot import _abuse_strikes, _abuse_last_delivery
        _abuse_strikes.pop(uid, None)
        _abuse_last_delivery.pop(uid, None)
    except Exception:
        pass

    # Send plain-text unban log (STRICTLY NO EMOJIS)
    from utils_ban_logger import log_premium_ban_event
    asyncio.create_task(log_premium_ban_event(
        user_id=uid,
        name=name,
        action="UNBANNED",
        reason="Unbanned by administrator",
        ips=[]
    ))

    # Bot confirmation reply (Polished markup)
    await message.reply_text(
        f"🔓 <b>{_sc('User Unbanned Successfully')}</b>\n\n"
        f"👤 <b>{_sc('User')}:</b> <code>{uid}</code> ({name})\n\n"
        f"<i>This user is now unbanned and their access has been fully restored.</i>"
    )
