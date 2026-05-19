"""
URL Bypass Agentic System - Arya Forward Bot
"""

from __future__ import annotations

import asyncio
import re
import time
import logging
from typing import Optional
from pyrogram import Client, filters, enums, ContinuePropagation
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from database import db

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

BYPASS_BOT = "Nick_Bypass_Bot"
BASE_IDLE_TIMEOUT = 15
INTER_LINK_DELAY = 5

SHORTENER_PATTERNS = [
    r'urlshortx\.io', r'shrinkme\.io', r'ouo\.io',
    r'short2url\.com', r'adf\.ly', r'linkvertise\.com',
    r'rekonise\.com', r'bc\.vc', r'za\.gl', r'exe\.io',
    r'gplinks\.in', r'tnshort\.com', r'linkshrink\.net',
    r'droplink\.co', r'techymedies\.com', r'modijiurl\.com',
]
SHORTENER_RE = re.compile('|'.join(SHORTENER_PATTERNS), re.IGNORECASE)

_bypass_sessions: dict = {}
_bypass_input_waiting: dict = {}
PM = enums.ParseMode.HTML  # shortcut


# ─── Input Router ─────────────────────────────────────────────────────────────

@Client.on_message(filters.private, group=-20)
async def _bypass_input_router(bot, message):
    uid = message.from_user.id if message.from_user else None
    if uid and uid in _bypass_input_waiting:
        fut = _bypass_input_waiting.pop(uid)
        if not fut.done():
            fut.set_result(message)
    raise ContinuePropagation


async def _ask_user(bot, user_id: int, timeout: int = 120):
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    old = _bypass_input_waiting.pop(user_id, None)
    if old and not old.done():
        old.cancel()
    _bypass_input_waiting[user_id] = fut
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        _bypass_input_waiting.pop(user_id, None)
        raise


# ─── Safe Send Helper ─────────────────────────────────────────────────────────

async def _safe_reply(bot, chat_id: int, text: str, reply_markup=None, msg_to_edit=None):
    """Try edit first; if it's a photo or fails, delete + send fresh."""
    if msg_to_edit is not None:
        # Don't try to edit photo messages
        if not getattr(msg_to_edit, 'photo', None):
            try:
                await msg_to_edit.edit_text(
                    text,
                    parse_mode=PM,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True
                )
                return
            except Exception:
                pass
        # Delete before sending fresh
        try:
            await msg_to_edit.delete()
        except Exception:
            pass

    try:
        await bot.send_message(
            chat_id,
            text,
            parse_mode=PM,
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"[URLBypass] send_message failed: {e}")


async def _update_progress(status_msg, text: str):
    try:
        await status_msg.edit_text(text, parse_mode=PM, disable_web_page_preview=True)
    except Exception:
        pass


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _load_userbot(user_id: int, bot_id: str):
    from plugins.test import start_clone_bot, CLIENT
    bots = await db.get_bots(user_id)
    userbots = [b for b in bots if not b.get('is_bot', True)]
    target = next((b for b in userbots if str(b.get('id', '')) == str(bot_id)), None)
    if not target:
        return None
    try:
        cl_factory = CLIENT()
        ub_client = cl_factory.client(target)
        ub_client = await start_clone_bot(ub_client)
        return ub_client
    except Exception as e:
        logger.error(f"[URLBypass] Userbot load failed: {e}")
        return None


def _extract_shortener_links(message) -> list:
    results = []
    if not message.reply_markup:
        return results
    rows = getattr(message.reply_markup, 'inline_keyboard', [])
    for row in rows:
        for btn in row:
            url = getattr(btn, 'url', None) or ''
            if url and SHORTENER_RE.search(url):
                label = (btn.text or '').strip() or 'Link'
                results.append((label, url))
    return results


def _extract_bypass_result(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r'Bypassed Link[^:]*:[\s\u2705]*(\S+)', text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r'(https?://(?:t\.me|telegram\.me)/\S+\?start=\S+)', text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def _parse_start_link(url: str) -> tuple:
    m = re.search(r'(?:t\.me|telegram\.me)/([^/?]+)\?start=(.+)', url, re.IGNORECASE)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, None


# ─── Channel Scanner ──────────────────────────────────────────────────────────

async def _scan_channel(ub: Client, channel_id, status_msg) -> list:
    all_links = []
    scanned = 0
    await _update_progress(status_msg, "<b>Scanning channel messages...</b>")
    try:
        async for msg in ub.get_chat_history(channel_id):
            scanned += 1
            links = _extract_shortener_links(msg)
            all_links.extend(links)
            if scanned % 100 == 0:
                await _update_progress(status_msg,
                    f"<b>Scanning...</b>\n\nScanned: {scanned}\nLinks found: {len(all_links)}")
    except Exception as e:
        logger.error(f"[URLBypass] Scan error: {e}")
    return all_links


# ─── Bypass Engine ────────────────────────────────────────────────────────────

async def _run_bypass_job(bot, user_id: int, ub: Client, status_msg, link_queue: list):
    total = len(link_queue)
    done = 0
    failed = []

    for idx, (label, shortener_url) in enumerate(link_queue):
        if user_id not in _bypass_sessions:
            break

        await _update_progress(status_msg,
            f"<b>Bypass Running</b>\n\n"
            f"<b>Progress:</b> {done}/{total}\n"
            f"<b>Current:</b> <code>{label[:40]}</code>\n"
            f"<b>Step:</b> Sending to bypass bot...\n\n"
            f"<i>/bypass_stop to cancel</i>"
        )

        try:
            await ub.send_message(BYPASS_BOT, shortener_url)
        except Exception as e:
            failed.append((label, f"Send failed: {e}"))
            await asyncio.sleep(INTER_LINK_DELAY)
            continue

        await _update_progress(status_msg,
            f"<b>Bypass Running</b>\n\n"
            f"<b>Progress:</b> {done}/{total}\n"
            f"<b>Current:</b> <code>{label[:40]}</code>\n"
            f"<b>Step:</b> Waiting for bypass reply...\n\n"
            f"<i>/bypass_stop to cancel</i>"
        )

        bypassed_url = None
        send_time = time.time()
        for _ in range(60):
            await asyncio.sleep(1)
            if user_id not in _bypass_sessions:
                break
            try:
                async for msg in ub.get_chat_history(BYPASS_BOT, limit=5):
                    msg_ts = msg.date.timestamp() if msg.date else 0
                    if msg_ts < send_time - 5:
                        break
                    candidate = _extract_bypass_result(msg.text or msg.caption or '')
                    if candidate and candidate != shortener_url:
                        bypassed_url = candidate
                        break
                if bypassed_url:
                    break
            except Exception:
                pass

        if not bypassed_url:
            failed.append((label, "No bypass reply received"))
            await asyncio.sleep(INTER_LINK_DELAY)
            continue

        bot_username, start_param = _parse_start_link(bypassed_url)
        if not bot_username or not start_param:
            failed.append((label, "Not a ?start= link"))
            done += 1
            await asyncio.sleep(INTER_LINK_DELAY)
            continue

        await _update_progress(status_msg,
            f"<b>Bypass Running</b>\n\n"
            f"<b>Progress:</b> {done}/{total}\n"
            f"<b>Current:</b> <code>{label[:40]}</code>\n"
            f"<b>Step:</b> Sending /start to @{bot_username}...\n\n"
            f"<i>/bypass_stop to cancel</i>"
        )

        try:
            await ub.send_message(bot_username, f"/start {start_param}")
        except Exception as e:
            failed.append((label, f"/start failed: {e}"))
            await asyncio.sleep(INTER_LINK_DELAY)
            continue

        # Smart wait for file delivery
        wait_since = time.time()
        files_received = 0
        last_file_time = time.time()
        got_first_file = False
        adaptive_timeout = BASE_IDLE_TIMEOUT

        while True:
            await asyncio.sleep(2)
            if user_id not in _bypass_sessions:
                break
            try:
                new_count = 0
                async for msg in ub.get_chat_history(bot_username, limit=20):
                    msg_ts = msg.date.timestamp() if msg.date else 0
                    if msg_ts < wait_since:
                        break
                    if msg.media or msg.document or msg.video or msg.audio or msg.voice:
                        new_count += 1
                        if msg_ts > last_file_time:
                            last_file_time = msg_ts
                            got_first_file = True
                if new_count > files_received:
                    delta = new_count - files_received
                    files_received = new_count
                    if delta >= 3:
                        adaptive_timeout = max(adaptive_timeout, 20)
                    if new_count >= 10:
                        adaptive_timeout = max(adaptive_timeout, 30)
            except Exception:
                pass

            idle = time.time() - last_file_time
            waited = time.time() - wait_since

            if got_first_file and idle >= adaptive_timeout:
                break
            if not got_first_file and waited > 90:
                break

            await _update_progress(status_msg,
                f"<b>Bypass Running</b>\n\n"
                f"<b>Progress:</b> {done}/{total}\n"
                f"<b>Current:</b> <code>{label[:40]}</code>\n"
                f"<b>Files received:</b> {files_received}\n"
                f"<b>Idle:</b> {int(idle)}s / {adaptive_timeout}s\n\n"
                f"<i>/bypass_stop to cancel</i>"
            )

        done += 1
        await asyncio.sleep(INTER_LINK_DELAY)

    _bypass_sessions.pop(user_id, None)
    fail_text = ''
    if failed:
        lines = '\n'.join(f"- {lb[:25]}: {rs[:40]}" for lb, rs in failed[:8])
        fail_text = f"\n\n<b>Failed ({len(failed)}):</b>\n{lines}"

    await _update_progress(status_msg,
        f"<b>Bypass Complete!</b>\n\n"
        f"Total: {total} | Done: {done} | Failed: {len(failed)}"
        f"{fail_text}"
    )


# ─── Entry Points ─────────────────────────────────────────────────────────────

@Client.on_message(filters.private & filters.command('bypass'))
async def bypass_cmd(bot, message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    try:
        await message.delete()
    except Exception:
        pass
    try:
        await _show_bypass_menu(bot, user_id, chat_id, None)
    except Exception as e:
        logger.error(f"[URLBypass] /bypass error: {e}", exc_info=True)
        try:
            await bot.send_message(chat_id,
                f"<b>Error:</b> <code>{str(e)[:200]}</code>", parse_mode=PM)
        except Exception:
            pass


@Client.on_callback_query(filters.regex(r'^ub#bypass$'))
async def bypass_cb(bot, query):
    user_id = query.from_user.id
    chat_id = query.message.chat.id
    original_msg = query.message
    try:
        await query.answer()
    except Exception:
        pass
    try:
        await _show_bypass_menu(bot, user_id, chat_id, original_msg)
    except Exception as e:
        logger.error(f"[URLBypass] callback error: {e}", exc_info=True)
        try:
            await bot.send_message(chat_id,
                f"<b>Error:</b> <code>{str(e)[:200]}</code>", parse_mode=PM)
        except Exception:
            pass


async def _show_bypass_menu(bot, user_id: int, chat_id: int, msg_to_edit):
    try:
        bots = await db.get_bots(user_id)
    except Exception as e:
        logger.error(f"[URLBypass] get_bots failed: {e}")
        await _safe_reply(bot, chat_id,
            f"<b>Database error:</b> <code>{str(e)[:150]}</code>",
            msg_to_edit=msg_to_edit)
        return

    userbots = [b for b in bots if not b.get('is_bot', True)]

    if not userbots:
        await _safe_reply(
            bot, chat_id,
            "<b>No Userbots Found!</b>\n\n"
            "Add a userbot:\nSettings - Accounts - Add Userbot",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Settings", callback_data="settings#accounts"),
                InlineKeyboardButton("Back", callback_data="back"),
            ]]),
            msg_to_edit=msg_to_edit
        )
        return

    btns = []
    for ub in userbots:
        mark = "[Active] " if ub.get('active') else ""
        name = ub.get('name', f"Userbot {ub.get('id', '?')}")
        btns.append([InlineKeyboardButton(
            f"{mark}{name}",
            callback_data=f"ub#bypass_sel_{ub.get('id', '')}"
        )])
    btns.append([InlineKeyboardButton("Back", callback_data="back")])

    await _safe_reply(
        bot, chat_id,
        "<b>URL Bypass System</b>\n\n"
        "Scans a channel for shortener links,\n"
        "bypasses them and opens the files.\n\n"
        "<b>Select Userbot to use:</b>",
        reply_markup=InlineKeyboardMarkup(btns),
        msg_to_edit=msg_to_edit
    )


# ─── Userbot Selection ────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r'^ub#bypass_sel_'))
async def bypass_select_ub(bot, query):
    user_id = query.from_user.id
    chat_id = query.message.chat.id
    try:
        await query.answer()
    except Exception:
        pass

    if user_id in _bypass_sessions:
        await query.answer("Already running! Send /bypass_stop first.", show_alert=True)
        return

    bot_id = query.data.replace('ub#bypass_sel_', '').strip()

    try:
        bots = await db.get_bots(user_id)
    except Exception as e:
        await bot.send_message(chat_id,
            f"<b>DB Error:</b> <code>{str(e)}</code>", parse_mode=PM)
        return

    ub_info = next((b for b in bots if str(b.get('id', '')) == str(bot_id)), {})
    ub_name = ub_info.get('name', f'Userbot {bot_id}')

    try:
        await query.message.edit_text(
            f"<b>Userbot: {ub_name}</b>\n\n"
            "Send the source channel:\n\n"
            "- Link: <code>https://t.me/channelname</code>\n"
            "- ID: <code>-1001234567890</code>\n"
            "- Or forward a message from the channel\n\n"
            "<i>Send /cancel to abort</i>",
            parse_mode=PM
        )
    except Exception:
        await bot.send_message(
            chat_id,
            f"<b>Userbot: {ub_name}</b>\n\nSend the source channel link or ID.\n"
            "<i>Send /cancel to abort</i>",
            parse_mode=PM
        )

    try:
        resp = await _ask_user(bot, user_id, timeout=120)
    except asyncio.TimeoutError:
        await bot.send_message(chat_id, "Timed out. Send /bypass to try again.")
        return

    txt = (resp.text or '').strip()

    if txt.lower() in ['/cancel', 'cancel']:
        try:
            await resp.delete()
        except Exception:
            pass
        await bot.send_message(chat_id, "Cancelled.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Back", callback_data="back")
            ]]))
        return

    # Resolve channel
    channel_id = None
    channel_title = "Unknown"

    try:
        if getattr(resp, 'forward_from_chat', None):
            channel_id = resp.forward_from_chat.id
            channel_title = resp.forward_from_chat.title or str(channel_id)
        elif txt.lstrip('-').isdigit():
            channel_id = int(txt)
        elif 't.me/' in txt:
            m = re.search(r't\.me/c/(\d+)', txt)
            if m:
                channel_id = int('-100' + m.group(1))
            else:
                m = re.search(r't\.me/([^/\s?]+)', txt)
                if m:
                    channel_id = '@' + m.group(1)
    except Exception:
        pass

    try:
        await resp.delete()
    except Exception:
        pass

    if not channel_id:
        await bot.send_message(chat_id,
            "Could not resolve channel. Send /bypass to try again.")
        return

    try:
        ch_info = await bot.get_chat(channel_id)
        channel_title = ch_info.title or channel_title
        channel_id = ch_info.id
    except Exception:
        pass

    status_msg = await bot.send_message(
        chat_id,
        f"<b>URL Bypass - Starting</b>\n\n"
        f"Userbot: {ub_name}\nChannel: {channel_title}\n\n"
        f"Connecting userbot...",
        parse_mode=PM,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Stop", callback_data=f"ub#bypass_stop_{user_id}")
        ]])
    )

    task = asyncio.create_task(
        _bypass_job_runner(bot, user_id, bot_id, ub_name, channel_id, channel_title, status_msg)
    )
    _bypass_sessions[user_id] = task


async def _bypass_job_runner(bot, user_id, bot_id, ub_name, channel_id, channel_title, status_msg):
    ub = None
    try:
        ub = await _load_userbot(user_id, bot_id)
        if not ub:
            _bypass_sessions.pop(user_id, None)
            await _update_progress(status_msg,
                "<b>Failed to connect userbot!</b>\n\n"
                "Session may be expired. Go to Settings - Accounts to re-add.")
            return

        await _update_progress(status_msg,
            f"<b>URL Bypass - Running</b>\n\n"
            f"Userbot: {ub_name}\nChannel: {channel_title}\n\n"
            f"Connected! Joining channel...")

        try:
            await ub.join_chat(channel_id)
        except Exception as e:
            s = str(e).lower()
            if 'already' not in s and 'participant' not in s:
                logger.warning(f"[URLBypass] Join warning: {e}")

        link_queue = await _scan_channel(ub, channel_id, status_msg)

        if not link_queue:
            _bypass_sessions.pop(user_id, None)
            await _update_progress(status_msg,
                "<b>No shortener links found!</b>\n\n"
                "No shortener links found in channel buttons.")
            return

        await _update_progress(status_msg,
            f"<b>URL Bypass - Queue Ready</b>\n\n"
            f"Channel: {channel_title}\nLinks: {len(link_queue)}\n\nStarting...")
        await asyncio.sleep(2)

        await _run_bypass_job(bot, user_id, ub, status_msg, link_queue)

    except asyncio.CancelledError:
        _bypass_sessions.pop(user_id, None)
        await _update_progress(status_msg, "<b>Bypass job cancelled.</b>")
    except Exception as e:
        _bypass_sessions.pop(user_id, None)
        logger.error(f"[URLBypass] job error: {e}", exc_info=True)
        await _update_progress(status_msg,
            f"<b>Error:</b> <code>{str(e)[:200]}</code>")


# ─── Stop ─────────────────────────────────────────────────────────────────────

@Client.on_message(filters.private & filters.command('bypass_stop'))
async def bypass_stop_cmd(bot, message):
    user_id = message.from_user.id
    task = _bypass_sessions.pop(user_id, None)
    if task:
        task.cancel()
        await message.reply_text("<b>Bypass job stopping...</b>", parse_mode=PM)
    else:
        await message.reply_text("No bypass job running.")


@Client.on_callback_query(filters.regex(r'^ub#bypass_stop_'))
async def bypass_stop_cb(bot, query):
    target_uid = int(query.data.replace('ub#bypass_stop_', ''))
    if query.from_user.id != target_uid:
        await query.answer("Not your job!", show_alert=True)
        return
    task = _bypass_sessions.pop(target_uid, None)
    if task:
        task.cancel()
        await query.answer("Stopping...", show_alert=True)
        try:
            await query.message.edit_text(
                "<b>Bypass job cancelled.</b>", parse_mode=PM)
        except Exception:
            pass
    else:
        await query.answer("No active bypass job.", show_alert=True)
