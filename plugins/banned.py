import logging
from pyrogram import Client, filters, StopPropagation
from pyrogram.types import Message
from database import db
from config import Config

logger = logging.getLogger(__name__)

# ── Owner check (env + DB co-owners) ─────────────────────────────────────────
async def _is_any_owner(user_id: int) -> bool:
    if Config.OWNER_IDS and user_id in Config.OWNER_IDS:
        return True
    try:
        return await db.is_co_owner(user_id)
    except Exception:
        return False

# ── Ban interceptor (runs on ALL bots) ───────────────────────────────────────
async def ban_interceptor(client, update):
    user = update.from_user
    if not user:
        return
    # Never block owners or co-owners
    if await _is_any_owner(user.id):
        return
    try:
        ban_status = await db.get_ban_status(user.id)
    except Exception:
        return  # DB error — don't block user
    if ban_status.get('is_banned'):
        if hasattr(update, 'answer'):
            try:
                await update.answer("⛔ You are banned from using this bot.", show_alert=True)
            except Exception:
                pass
        raise StopPropagation

# Register on main bot via @Client.on_* decorators
@Client.on_message(filters.all, group=-999)
async def main_bot_ban_message_interceptor(client, message):
    await ban_interceptor(client, message)

@Client.on_callback_query(filters.all, group=-999)
async def main_bot_ban_callback_interceptor(client, query):
    await ban_interceptor(client, query)

# ── Helper: resolve target user from command / reply ─────────────────────────
async def _resolve_target(client, message: Message):
    """
    Returns (user_id, display_name) or (None, error_msg).
    Supports:
      - /ban @username [reason]
      - /ban 123456789 [reason]
      - Reply to a message + /ban [reason]
    """
    reply = message.reply_to_message
    if reply and reply.from_user:
        uid = reply.from_user.id
        name = reply.from_user.first_name or str(uid)
        reason_parts = message.command[1:]
        return uid, name, " ".join(reason_parts) if reason_parts else "Banned by owner"

    if len(message.command) < 2:
        return None, None, "Usage: /ban @username|user_id [reason]\nOr reply to a message and use /ban [reason]"

    target = message.command[1]
    reason = " ".join(message.command[2:]) if len(message.command) > 2 else "Banned by owner"

    try:
        if target.startswith("@") or not target.lstrip("-").isdigit():
            user = await client.get_users(target)
            return user.id, user.first_name or target, reason
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

# ── /ban command ──────────────────────────────────────────────────────────────
@Client.on_message(filters.command("ban"))
async def ban_user_cmd(client: Client, message: Message):
    if not await _is_any_owner(message.from_user.id if message.from_user else 0):
        return

    uid, name, reason = await _resolve_target(client, message)
    if uid is None:
        await message.reply_text(f"❌ {reason}")
        return

    # Safety: never ban an owner
    if await _is_any_owner(uid):
        await message.reply_text("⛔ Cannot ban an owner or co-owner.")
        return

    await db.ban_user(uid, ban_reason=reason)

    # Notify across all share bots too
    _notify_share_bots_ban(uid)

    await message.reply_text(
        f"✅ **Banned:** `{uid}` ({name})\n"
        f"**Reason:** {reason}\n\n"
        f"_This user is now blocked from the main bot AND all delivery bots._"
    )

# ── /unban command ────────────────────────────────────────────────────────────
@Client.on_message(filters.command("unban"))
async def unban_user_cmd(client: Client, message: Message):
    if not await _is_any_owner(message.from_user.id if message.from_user else 0):
        return

    uid, name, _ = await _resolve_target(client, message)
    if uid is None:
        await message.reply_text(f"❌ {_}")
        return

    await db.remove_ban(uid)
    await message.reply_text(f"✅ **Unbanned:** `{uid}` ({name})")

# ── /banlist command ──────────────────────────────────────────────────────────
@Client.on_message(filters.command("banlist"))
async def ban_list_cmd(client: Client, message: Message):
    if not await _is_any_owner(message.from_user.id if message.from_user else 0):
        return
    try:
        cursor = db.col.find({"ban_status.is_banned": True}, {"id": 1, "ban_status": 1, "name": 1})
        users = await cursor.to_list(length=50)
    except Exception as e:
        await message.reply_text(f"Error: {e}")
        return
    if not users:
        await message.reply_text("✅ No banned users.")
        return
    lines = [f"🚫 **Banned Users ({len(users)}):**"]
    for u in users:
        reason = u.get("ban_status", {}).get("ban_reason", "No reason")
        uname  = u.get("name", "")
        lines.append(f"• `{u.get('id')}` {uname} — {reason}")
    await message.reply_text("\n".join(lines))

# ── Helper to propagate ban to all running share bots ─────────────────────────
def _notify_share_bots_ban(user_id: int):
    """
    The ban is stored in MongoDB — all bots read the same DB on every request.
    No extra action needed. This function is a no-op placeholder for future
    in-memory cache invalidation if needed.
    """
    pass  # DB-based ban is universal across all bots automatically
