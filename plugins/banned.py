import logging
from pyrogram import Client, filters, StopPropagation
from pyrogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
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
    # Safely clear strikes too
    try:
        from plugins.share_bot import _abuse_strikes, _abuse_last_delivery
        _abuse_strikes.pop(uid, None)
        _abuse_last_delivery.pop(uid, None)
    except Exception:
        pass
    await message.reply_text(f"✅ **Unbanned:** `{uid}` ({name})\n_This user has been unbanned and their anti-abuse strike counts have been cleared._")

# ── /banlist command & Interactive UI ──────────────────────────────────────────
async def _render_ban_list(client, user_id: int, message_or_query, page: int = 1):
    try:
        cursor = db.col.find({"ban_status.is_banned": True}, {"id": 1, "ban_status": 1, "name": 1})
        users = await cursor.to_list(length=1000)
    except Exception as e:
        err_msg = f"Error fetching banlist: {e}"
        if isinstance(message_or_query, CallbackQuery):
            await message_or_query.answer(err_msg, show_alert=True)
        else:
            await message_or_query.reply_text(err_msg)
        return

    is_cb = isinstance(message_or_query, CallbackQuery)

    if not users:
        text = "<b>🚫 Banned Users List</b>\n\n✅ <i>No users are currently banned.</i>"
        btns = InlineKeyboardMarkup([
            [InlineKeyboardButton("❮ Bᴀᴄᴋ Tᴏ Sᴇᴛᴛɪɴɢs", callback_data="settings#owners")],
            [InlineKeyboardButton("🗑 Cʟᴏsᴇ", callback_data="ban#close")]
        ])
        if is_cb:
            await message_or_query.message.edit_text(text, reply_markup=btns)
        else:
            await message_or_query.reply_text(text, reply_markup=btns)
        return

    PAGE_SIZE = 10
    total_users = len(users)
    total_pages = (total_users + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(1, min(page, total_pages))

    start_idx = (page - 1) * PAGE_SIZE
    end_idx = start_idx + PAGE_SIZE
    chunk = users[start_idx:end_idx]

    lines = [
        "<b>🚫 <u>Banned Users Control Panel</u></b>\n",
        f"Total Banned Users: <b>{total_users}</b>",
        f"Showing Page <b>{page}</b> of <b>{total_pages}</b>\n"
    ]

    btns_list = []

    for idx, u in enumerate(chunk, start=start_idx + 1):
        reason = u.get("ban_status", {}).get("ban_reason", "No reason")
        uname = u.get("name", "")
        uid = u.get("id")
        display_name = uname if uname else f"User {uid}"
        mention = f"<a href='tg://user?id={uid}'>{display_name}</a>"
        
        lines.append(
            f"<b>{idx}.</b> {mention} (<code>{uid}</code>)\n"
            f"   └ <b>Reason:</b> <i>{reason}</i>\n"
        )
        btns_list.append([
            InlineKeyboardButton(f"🔓 Unban {display_name[:15]}", callback_data=f"ban#unban#{uid}#{page}")
        ])

    # Navigation buttons
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"ban#list#{page-1}"))
    nav_row.append(InlineKeyboardButton(f"Page {page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"ban#list#{page+1}"))
    btns_list.append(nav_row)

    # Back / Close buttons
    btns_list.append([
        InlineKeyboardButton("❮ Bᴀᴄᴋ Tᴏ Sᴇᴛᴛɪɴɢs", callback_data="settings#owners"),
        InlineKeyboardButton("🗑 Cʟᴏsᴇ", callback_data="ban#close")
    ])

    text = "\n".join(lines)
    reply_markup = InlineKeyboardMarkup(btns_list)

    if is_cb:
        await message_or_query.message.edit_text(text, reply_markup=reply_markup)
    else:
        await message_or_query.reply_text(text, reply_markup=reply_markup)

@Client.on_message(filters.command("banlist"))
async def ban_list_cmd(client: Client, message: Message):
    if not await _is_any_owner(message.from_user.id if message.from_user else 0):
        return
    await _render_ban_list(client, message.from_user.id, message, page=1)

@Client.on_callback_query(filters.regex(r'^ban#list#(\d+)$'))
async def ban_list_cb(client, query):
    if not await _is_any_owner(query.from_user.id):
        return await query.answer("⛔ Owner only!", show_alert=True)
    page = int(query.matches[0].group(1))
    await query.answer()
    await _render_ban_list(client, query.from_user.id, query, page)

@Client.on_callback_query(filters.regex(r'^ban#unban#(-?\d+)#(\d+)$'))
async def ban_unban_cb(client, query):
    if not await _is_any_owner(query.from_user.id):
        return await query.answer("⛔ Owner only!", show_alert=True)
    target_uid = int(query.matches[0].group(1))
    page = int(query.matches[0].group(2))
    
    await db.remove_ban(target_uid)
    # Safely clear strikes in-memory too
    try:
        from plugins.share_bot import _abuse_strikes, _abuse_last_delivery
        _abuse_strikes.pop(target_uid, None)
        _abuse_last_delivery.pop(target_uid, None)
    except Exception:
        pass
    await query.answer(f"✅ User {target_uid} Unbanned Successfully!", show_alert=True)
    await _render_ban_list(client, query.from_user.id, query, page)

@Client.on_callback_query(filters.regex(r'^ban#close$'))
async def ban_close_cb(client, query):
    await query.message.delete()

# ── Whitelist System Commands ─────────────────────────────────────────────────
@Client.on_message(filters.command("whitelist"))
async def whitelist_cmd(client: Client, message: Message):
    if not await _is_any_owner(message.from_user.id if message.from_user else 0):
        return

    # If user ID is passed, add to whitelist
    if len(message.command) >= 2:
        target = message.command[1].strip()
        try:
            if target.startswith("@") or not target.lstrip("-").isdigit():
                user = await client.get_users(target)
                uid = user.id
                name = user.first_name or target
            else:
                uid = int(target)
                try:
                    user = await client.get_users(uid)
                    name = user.first_name or str(uid)
                except Exception:
                    name = str(uid)
            
            await db.whitelist_user(uid)
            # Clear any active strike records
            try:
                from plugins.share_bot import _abuse_strikes, _abuse_last_delivery
                _abuse_strikes.pop(uid, None)
                _abuse_last_delivery.pop(uid, None)
            except Exception:
                pass
            
            await message.reply_text(f"✅ **Whitelisted:** `{uid}` ({name})\n_This user is now completely exempt from all anti-abuse strikes and cooldowns._")
            return
        except Exception as e:
            await message.reply_text(f"❌ Could not whitelist user: {e}")
            return

    # Otherwise show whitelisted users list
    await _render_whitelist(client, message.from_user.id, message, page=1)

@Client.on_message(filters.command("unwhitelist"))
async def unwhitelist_cmd(client: Client, message: Message):
    if not await _is_any_owner(message.from_user.id if message.from_user else 0):
        return

    if len(message.command) < 2:
        await message.reply_text("Usage: /unwhitelist @username|user_id")
        return

    target = message.command[1].strip()
    try:
        if target.startswith("@") or not target.lstrip("-").isdigit():
            user = await client.get_users(target)
            uid = user.id
            name = user.first_name or target
        else:
            uid = int(target)
            name = str(uid)

        await db.unwhitelist_user(uid)
        await message.reply_text(f"✅ **Removed from Whitelist:** `{uid}` ({name})")
    except Exception as e:
        await message.reply_text(f"❌ Could not remove user from whitelist: {e}")

# ── Whitelist Interactive UI ───────────────────────────────────────────────────
async def _render_whitelist(client, user_id: int, message_or_query, page: int = 1):
    try:
        users = await db.get_whitelisted_users()
    except Exception as e:
        err_msg = f"Error fetching whitelist: {e}"
        if isinstance(message_or_query, CallbackQuery):
            await message_or_query.answer(err_msg, show_alert=True)
        else:
            await message_or_query.reply_text(err_msg)
        return

    is_cb = isinstance(message_or_query, CallbackQuery)

    if not users:
        text = "<b>⚪ Whitelisted Users List</b>\n\n✅ <i>No users are currently whitelisted.</i>"
        btns = InlineKeyboardMarkup([
            [InlineKeyboardButton("❮ Bᴀᴄᴋ Tᴏ Sᴇᴛᴛɪɴɢs", callback_data="settings#owners")],
            [InlineKeyboardButton("🗑 Cʟᴏsᴇ", callback_data="wl#close")]
        ])
        if is_cb:
            await message_or_query.message.edit_text(text, reply_markup=btns)
        else:
            await message_or_query.reply_text(text, reply_markup=btns)
        return

    PAGE_SIZE = 10
    total_users = len(users)
    total_pages = (total_users + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(1, min(page, total_pages))

    start_idx = (page - 1) * PAGE_SIZE
    end_idx = start_idx + PAGE_SIZE
    chunk = users[start_idx:end_idx]

    lines = [
        "<b>⚪ <u>Whitelisted Users Panel</u></b>\n",
        f"Total Whitelisted Users: <b>{total_users}</b>",
        f"Showing Page <b>{page}</b> of <b>{total_pages}</b>\n"
    ]

    btns_list = []

    for idx, u in enumerate(chunk, start=start_idx + 1):
        uname = u.get("name", "")
        uid = u.get("id")
        display_name = uname if uname else f"User {uid}"
        mention = f"<a href='tg://user?id={uid}'>{display_name}</a>"
        
        lines.append(
            f"<b>{idx}.</b> {mention} (<code>{uid}</code>)\n"
        )
        btns_list.append([
            InlineKeyboardButton(f"🗑 Remove {display_name[:15]}", callback_data=f"wl#remove#{uid}#{page}")
        ])

    # Navigation buttons
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"wl#list#{page-1}"))
    nav_row.append(InlineKeyboardButton(f"Page {page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"wl#list#{page+1}"))
    btns_list.append(nav_row)

    # Back / Close buttons
    btns_list.append([
        InlineKeyboardButton("❮ Bᴀᴄᴋ Tᴏ Sᴇᴛᴛɪɴɢs", callback_data="settings#owners"),
        InlineKeyboardButton("🗑 Cʟᴏsᴇ", callback_data="wl#close")
    ])

    text = "\n".join(lines)
    reply_markup = InlineKeyboardMarkup(btns_list)

    if is_cb:
        await message_or_query.message.edit_text(text, reply_markup=reply_markup)
    else:
        await message_or_query.reply_text(text, reply_markup=reply_markup)

@Client.on_callback_query(filters.regex(r'^wl#list#(\d+)$'))
async def wl_list_cb(client, query):
    if not await _is_any_owner(query.from_user.id):
        return await query.answer("⛔ Owner only!", show_alert=True)
    page = int(query.matches[0].group(1))
    await query.answer()
    await _render_whitelist(client, query.from_user.id, query, page)

@Client.on_callback_query(filters.regex(r'^wl#remove#(-?\d+)#(\d+)$'))
async def wl_remove_cb(client, query):
    if not await _is_any_owner(query.from_user.id):
        return await query.answer("⛔ Owner only!", show_alert=True)
    target_uid = int(query.matches[0].group(1))
    page = int(query.matches[0].group(2))
    
    await db.unwhitelist_user(target_uid)
    await query.answer(f"✅ User {target_uid} Removed from Whitelist Successfully!", show_alert=True)
    await _render_whitelist(client, query.from_user.id, query, page)

@Client.on_callback_query(filters.regex(r'^wl#close$'))
async def wl_close_cb(client, query):
    await query.message.delete()

# ── Helper to propagate ban to all running share bots ─────────────────────────
def _notify_share_bots_ban(user_id: int):
    """
    The ban is stored in MongoDB — all bots read the same DB on every request.
    No extra action needed. This function is a no-op placeholder for future
    in-memory cache invalidation if needed.
    """
    pass  # DB-based ban is universal across all bots automatically
