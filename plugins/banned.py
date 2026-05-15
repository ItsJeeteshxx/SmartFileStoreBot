import logging
from pyrogram import Client, filters, StopPropagation
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.types import Message
from database import db
from config import Config

logger = logging.getLogger(__name__)

async def ban_interceptor(client, update):
    user = update.from_user
    if user:
        ban_status = await db.get_ban_status(user.id)
        if ban_status.get('is_banned'):
            raise StopPropagation

# These automatically register on the main bot (Arya Forward Bot) because it uses plugins mechanism
@Client.on_message(filters.all, group=-1)
async def main_bot_ban_message_interceptor(client, message):
    await ban_interceptor(client, message)

@Client.on_callback_query(filters.all, group=-1)
async def main_bot_ban_callback_interceptor(client, query):
    await ban_interceptor(client, query)

@Client.on_message(filters.command("ban") & filters.user(Config.OWNER_IDS))
async def ban_user_cmd(client: Client, message: Message):
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
        
    await db.ban_user(user_id, ban_reason=reason)
    await message.reply_text(f"✅ User `{user_id}` has been **banned**.\nReason: {reason}")

@Client.on_message(filters.command("unban") & filters.user(Config.OWNER_IDS))
async def unban_user_cmd(client: Client, message: Message):
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
