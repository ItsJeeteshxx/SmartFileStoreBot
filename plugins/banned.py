import logging
from pyrogram import Client, filters, StopPropagation
from pyrogram.types import Message
from database import db
from config import Config

logger = logging.getLogger(__name__)

async def _is_any_owner(user_id: int) -> bool:
    """Check if user is owner: env-configured OR DB co-owner."""
    if Config.OWNER_IDS and user_id in Config.OWNER_IDS:
        return True
    try:
        return await db.is_co_owner(user_id)
    except Exception:
        return False

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
        # Answer callback to clear loading spinner before stopping
        if hasattr(update, 'answer'):
            try:
                await update.answer("⛔ You are banned from using this bot.", show_alert=True)
            except Exception:
                pass
        raise StopPropagation

# These automatically register on the main bot (Arya Forward Bot) because it uses plugins mechanism
@Client.on_message(filters.all, group=-999)
async def main_bot_ban_message_interceptor(client, message):
    await ban_interceptor(client, message)

@Client.on_callback_query(filters.all, group=-999)
async def main_bot_ban_callback_interceptor(client, query):
    await ban_interceptor(client, query)

async def _check_ban_cmd_permission(client, message):
    """Returns True if caller is allowed to use /ban or /unban."""
    uid = message.from_user.id if message.from_user else None
    if not uid:
        return False
    return await _is_any_owner(uid)

@Client.on_message(filters.command("ban") & filters.private)
async def ban_user_cmd(client: Client, message: Message):
    if not await _check_ban_cmd_permission(client, message):
        return  # Silently ignore — not an owner
    if len(message.command) < 2:
        await message.reply_text("Usage: /ban <user_id or @username> [reason]")
        return
    target = message.command[1]
    reason = " ".join(message.command[2:]) if len(message.command) > 2 else "Banned by admin"
    try:
        if target.startswith("@"):
            user = await client.get_users(target)
            user_id = user.id
        else:
            user_id = int(target)
    except Exception as e:
        await message.reply_text(f"Could not find user: {e}")
        return
    # Safety: never ban an owner
    if await _is_any_owner(user_id):
        await message.reply_text("⛔ Cannot ban an owner or co-owner.")
        return
    await db.ban_user(user_id, ban_reason=reason)
    await message.reply_text(f"✅ User `{user_id}` has been **banned**.\nReason: {reason}")

@Client.on_message(filters.command("unban") & filters.private)
async def unban_user_cmd(client: Client, message: Message):
    if not await _check_ban_cmd_permission(client, message):
        return
    if len(message.command) < 2:
        await message.reply_text("Usage: /unban <user_id or @username>")
        return
    target = message.command[1]
    try:
        if target.startswith("@"):
            user = await client.get_users(target)
            user_id = user.id
        else:
            user_id = int(target)
    except Exception as e:
        await message.reply_text(f"Could not find user: {e}")
        return
    await db.remove_ban(user_id)
    await message.reply_text(f"✅ User `{user_id}` has been **unbanned**.")

@Client.on_message(filters.command("banlist") & filters.private)
async def ban_list_cmd(client: Client, message: Message):
    if not await _check_ban_cmd_permission(client, message):
        return
    try:
        banned = await db.get_banned()
        users = await banned.to_list(length=50)
    except Exception as e:
        await message.reply_text(f"Error: {e}")
        return
    if not users:
        await message.reply_text("✅ No banned users.")
        return
    lines = [f"🚫 Banned Users ({len(users)}):"]
    for u in users:
        reason = u.get('ban_status', {}).get('ban_reason', 'No reason')
        lines.append(f"• `{u.get('id')}` — {reason}")
    await message.reply_text("\n".join(lines))

