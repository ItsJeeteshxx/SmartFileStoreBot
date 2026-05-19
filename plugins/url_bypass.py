"""
URL Bypass Agentic System — Integrated into Arya Forward Bot
============================================================
Flow:
  1. User clicks "🔗 URL Bypass" from main menu or sends /bypass
  2. Bot shows userbots list → user picks one manually
  3. Bot asks for source channel link/ID
  4. Selected userbot joins that channel
  5. Userbot scans ALL messages, extracts shortener links from inline buttons
  6. One by one: sends each shortener link to bypass bot → waits for reply
  7. Extracts bypassed link → sends /start <param> to the target bot
  8. Smart-waits for files (auto-detects batch completion via idle timeout)
  9. Shows live progress. Moves to next link automatically.
"""

import asyncio
import re
import time
import logging
from pyrogram import Client, filters, ContinuePropagation
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from database import db
from config import Config

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

# Username of the URL shortener bypass bot
BYPASS_BOT = "Nick_Bypass_Bot"

# How many seconds of no new files = "batch done"
BASE_IDLE_TIMEOUT = 15

# Delay between sending each link to bypass bot (anti-spam)
INTER_LINK_DELAY = 5

# Shortener URL patterns to look for in buttons
SHORTENER_PATTERNS = [
    r'urlshortx\.io',
    r'shrinkme\.io',
    r'ouo\.io',
    r'short2url\.com',
    r'adf\.ly',
    r'linkvertise\.com',
    r'rekonise\.com',
    r'bc\.vc',
    r'za\.gl',
    r'exe\.io',
    r'gplinks\.in',
    r'tnshort\.com',
    r'linkshrink\.net',
    r'droplink\.co',
]
SHORTENER_RE = re.compile('|'.join(SHORTENER_PATTERNS), re.IGNORECASE)

# Active bypass sessions keyed by user_id
_bypass_sessions: dict = {}

# Input listeners for user replies (user_id → asyncio.Future)
_bypass_input_waiting: dict = {}


# ── Input Router — captures replies for bypass setup flow ────────────────────

@Client.on_message(filters.private, group=-20)
async def _bypass_input_router(bot, message):
    uid = message.from_user.id if message.from_user else None
    if uid and uid in _bypass_input_waiting:
        fut = _bypass_input_waiting.pop(uid)
        if not fut.done():
            fut.set_result(message)
    raise ContinuePropagation


async def _ask_user(bot, user_id: int, timeout: int = 120):
    """Wait for the next message from user_id."""
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


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _load_userbot(user_id: int, bot_id: str):
    """Load and return a connected Pyrogram userbot Client using the shared client cache."""
    from plugins.test import start_clone_bot, CLIENT
    bots = await db.get_bots(user_id)
    userbots = [b for b in bots if not b.get('is_bot', True)]
    target = next((b for b in userbots if str(b.get('id', '')) == str(bot_id)), None)
    if not target:
        return None
    try:
        cl_factory = CLIENT()
        ub = cl_factory.client(target)   # creates proper Pyrogram Client
        ub = await start_clone_bot(ub)   # uses shared cache — no AUTH_KEY_DUPLICATED
        return ub
    except Exception as e:
        logger.error(f"[URLBypass] Failed to start userbot {bot_id}: {e}")
        return None


def _extract_shortener_links(message) -> list:
    """
    Returns list of (button_label, url) tuples where url matches a shortener.
    """
    results = []
    if not message.reply_markup:
        return results
    rows = getattr(message.reply_markup, 'inline_keyboard', [])
    for row in rows:
        for btn in row:
            url = getattr(btn, 'url', None) or ''
            if url and SHORTENER_RE.search(url):
                label = (btn.text or '').strip() or 'No Label'
                results.append((label, url))
    return results


def _extract_bypass_result(text: str) -> str | None:
    """
    Parse the bypass bot's reply to extract the final bypassed URL.
    """
    if not text:
        return None
    m = re.search(r'Bypassed Link[^:]*:[\s✅]*(\S+)', text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r'(https?://(?:t\.me|telegram\.me)/\S+\?start=\S+)', text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def _parse_start_link(url: str) -> tuple:
    """
    Parse t.me/?start= links → (bot_username, start_param)
    """
    m = re.search(r'(?:t\.me|telegram\.me)/([^/?]+)\?start=(.+)', url, re.IGNORECASE)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, None


async def _update_progress(status_msg, text: str):
    """Safely edit the live status message."""
    try:
        await status_msg.edit_text(text, parse_mode='html')
    except Exception:
        pass


# ── Core Bypass Engine ───────────────────────────────────────────────────────

async def _run_bypass_job(bot, user_id: int, ub: Client, status_msg, link_queue: list):
    """Main agentic loop — processes the link queue sequentially."""
    total = len(link_queue)
    done = 0
    failed = []

    for idx, (label, shortener_url) in enumerate(link_queue):

        if user_id not in _bypass_sessions:
            break

        await _update_progress(status_msg,
            f"<b>🔗 URL Bypass — Running</b>\n\n"
            f"<b>Progress:</b> {done}/{total} done\n"
            f"<b>Current:</b> <code>{label}</code> ({idx+1}/{total})\n"
            f"<b>Step:</b> Sending to bypass bot...\n\n"
            f"<i>Send /bypass_stop to cancel</i>"
        )

        # ── Step 1: Send to bypass bot ────────────────────────────────────────
        try:
            await ub.send_message(BYPASS_BOT, shortener_url)
        except Exception as e:
            logger.error(f"[URLBypass] Send to bypass bot failed: {e}")
            failed.append((label, f"Send failed: {e}"))
            await asyncio.sleep(INTER_LINK_DELAY)
            continue

        # ── Step 2: Wait for bypass bot reply (up to 60 sec) ─────────────────
        await _update_progress(status_msg,
            f"<b>🔗 URL Bypass — Running</b>\n\n"
            f"<b>Progress:</b> {done}/{total} done\n"
            f"<b>Current:</b> <code>{label}</code> ({idx+1}/{total})\n"
            f"<b>Step:</b> ⏳ Waiting for bypass bot reply...\n\n"
            f"<i>Send /bypass_stop to cancel</i>"
        )

        bypassed_url = None
        for _ in range(60):
            await asyncio.sleep(1)
            if user_id not in _bypass_sessions:
                break
            try:
                async for msg in ub.get_chat_history(BYPASS_BOT, limit=5):
                    msg_ts = msg.date.timestamp() if msg.date else 0
                    if (time.time() - msg_ts) > 120:
                        break  # Too old
                    candidate = _extract_bypass_result(msg.text or msg.caption or '')
                    if candidate and candidate != shortener_url:
                        bypassed_url = candidate
                        break
                if bypassed_url:
                    break
            except Exception:
                pass

        if not bypassed_url:
            failed.append((label, "Bypass bot did not reply in time"))
            await asyncio.sleep(INTER_LINK_DELAY)
            continue

        # ── Step 3: Parse the bypassed link ───────────────────────────────────
        bot_username, start_param = _parse_start_link(bypassed_url)

        if not bot_username or not start_param:
            # Not a /start link — log and skip
            failed.append((label, f"Not a ?start= link: {bypassed_url[:60]}"))
            done += 1
            await asyncio.sleep(INTER_LINK_DELAY)
            continue

        await _update_progress(status_msg,
            f"<b>🔗 URL Bypass — Running</b>\n\n"
            f"<b>Progress:</b> {done}/{total} done\n"
            f"<b>Current:</b> <code>{label}</code> ({idx+1}/{total})\n"
            f"<b>Step:</b> 🤖 Sending /start to @{bot_username}...\n\n"
            f"<i>Send /bypass_stop to cancel</i>"
        )

        # ── Step 4: Send /start to the target bot ─────────────────────────────
        try:
            await ub.send_message(bot_username, f"/start {start_param}")
        except Exception as e:
            logger.error(f"[URLBypass] /start failed: {e}")
            failed.append((label, f"/start failed: {e}"))
            await asyncio.sleep(INTER_LINK_DELAY)
            continue

        # ── Step 5: Smart wait for file delivery ──────────────────────────────
        wait_since = time.time()
        files_received = 0
        last_new_file_time = time.time()
        first_file = False
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
                        if msg_ts > last_new_file_time:
                            last_new_file_time = msg_ts
                            first_file = True

                if new_count > files_received:
                    delta = new_count - files_received
                    files_received = new_count
                    # Adapt timeout based on volume
                    if delta >= 3:
                        adaptive_timeout = max(adaptive_timeout, 20)
                    if new_count >= 10:
                        adaptive_timeout = max(adaptive_timeout, 30)

            except Exception:
                pass

            idle_secs = time.time() - last_new_file_time
            total_waited = time.time() - wait_since

            # Batch done: got at least 1 file and idle for adaptive_timeout
            if first_file and idle_secs >= adaptive_timeout:
                break
            # Hard timeout: no file in 90 seconds
            if not first_file and total_waited > 90:
                break

            await _update_progress(status_msg,
                f"<b>🔗 URL Bypass — Running</b>\n\n"
                f"<b>Progress:</b> {done}/{total} done\n"
                f"<b>Current:</b> <code>{label}</code> ({idx+1}/{total})\n"
                f"<b>Step:</b> 📥 Receiving files from @{bot_username}\n"
                f"<b>Files so far:</b> {files_received}\n"
                f"<b>Idle:</b> {int(idle_secs)}s / {adaptive_timeout}s\n\n"
                f"<i>Send /bypass_stop to cancel</i>"
            )

        done += 1
        await asyncio.sleep(INTER_LINK_DELAY)

    # ── Job Complete ──────────────────────────────────────────────────────────
    _bypass_sessions.pop(user_id, None)

    fail_text = ''
    if failed:
        lines = '\n'.join(f"• <code>{lb[:30]}</code>: {rs[:50]}" for lb, rs in failed[:10])
        fail_text = f"\n\n<b>⚠️ Failed ({len(failed)}):</b>\n{lines}"

    await _update_progress(status_msg,
        f"<b>✅ URL Bypass — Complete!</b>\n\n"
        f"<b>Total:</b> {total}  |  <b>Done:</b> {done}  |  <b>Failed:</b> {len(failed)}"
        f"{fail_text}"
    )


# ── Channel Scanner ──────────────────────────────────────────────────────────

async def _scan_channel(ub: Client, channel_id, status_msg) -> list:
    """Scans all channel messages for shortener links in inline buttons."""
    all_links = []
    scanned = 0

    await _update_progress(status_msg, "<b>🔍 Scanning channel messages...</b>")

    try:
        async for msg in ub.get_chat_history(channel_id):
            scanned += 1
            links = _extract_shortener_links(msg)
            all_links.extend(links)
            if scanned % 100 == 0:
                await _update_progress(status_msg,
                    f"<b>🔍 Scanning channel...</b>\n\n"
                    f"<b>Scanned:</b> {scanned} messages\n"
                    f"<b>Links found:</b> {len(all_links)}"
                )
    except Exception as e:
        logger.error(f"[URLBypass] Scan error: {e}")

    return all_links


# ── Entry Points ─────────────────────────────────────────────────────────────

@Client.on_message(filters.private & filters.command('bypass'))
async def bypass_cmd(bot, message):
    user_id = message.from_user.id
    await message.delete()
    await _show_bypass_menu(bot, user_id, message.chat.id)


@Client.on_callback_query(filters.regex(r'^ub#bypass$'))
async def bypass_cb(bot, query):
    user_id = query.from_user.id
    await query.answer()
    await query.message.delete()
    await _show_bypass_menu(bot, user_id, query.message.chat.id)


async def _show_bypass_menu(bot, user_id: int, chat_id: int):
    """Show userbot selection menu."""
    bots = await db.get_bots(user_id)
    userbots = [b for b in bots if not b.get('is_bot', True)]

    if not userbots:
        return await bot.send_message(
            chat_id,
            "<b>❌ No Userbots Found!</b>\n\n"
            "Add a userbot first via <b>Settings → Accounts → Add Userbot</b>.",
            parse_mode='html',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⚙️ Go to Settings", callback_data="settings#accounts"),
                InlineKeyboardButton("❮ Back", callback_data="back"),
            ]])
        )

    btns = []
    for ub in userbots:
        active_mark = "✔️ " if ub.get('active') else ""
        name = ub.get('name', f"Userbot {ub.get('id', '?')}")
        btns.append([InlineKeyboardButton(
            f"{active_mark}👤 {name}",
            callback_data=f"ub#bypass_sel_{ub.get('id', '')}"
        )])
    btns.append([InlineKeyboardButton("❮ Back", callback_data="back")])

    await bot.send_message(
        chat_id,
        "<b>🔗 URL Bypass System</b>\n\n"
        "Automatically bypasses shortener links from a channel and fetches files.\n\n"
        "<b>Select which Userbot should do this job:</b>",
        parse_mode='html',
        reply_markup=InlineKeyboardMarkup(btns)
    )


@Client.on_callback_query(filters.regex(r'^ub#bypass_sel_'))
async def bypass_select_ub(bot, query):
    user_id = query.from_user.id
    await query.answer()

    if user_id in _bypass_sessions:
        return await query.answer(
            "⚠️ A bypass job is already running! Send /bypass_stop first.",
            show_alert=True
        )

    bot_id = query.data.replace('ub#bypass_sel_', '')

    bots = await db.get_bots(user_id)
    ub_info = next((b for b in bots if str(b.get('id', '')) == str(bot_id)), {})
    ub_name = ub_info.get('name', f'Userbot {bot_id}')

    await query.message.edit_text(
        f"<b>🔗 URL Bypass — Userbot: 👤 {ub_name}</b>\n\n"
        "<b>Send the Source Channel:</b>\n\n"
        "• Channel link: <code>https://t.me/channelname</code>\n"
        "• Channel ID: <code>-1001234567890</code>\n"
        "• Or forward any message from that channel\n\n"
        "<i>Send /cancel to abort.</i>",
        parse_mode='html'
    )

    # Wait for channel input
    try:
        resp = await _ask_user(bot, user_id, timeout=120)
    except asyncio.TimeoutError:
        return await bot.send_message(
            user_id, "⏰ Timed out. Send /bypass to try again.", parse_mode='html'
        )

    txt = (resp.text or '').strip()

    if txt.lower() in ['/cancel', 'cancel']:
        await resp.delete()
        return await bot.send_message(user_id, "❌ Cancelled.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❮ Back", callback_data="back")]]))

    # Resolve channel ID
    channel_id = None
    channel_title = "Unknown"

    try:
        await resp.delete()
    except Exception:
        pass

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

    if not channel_id:
        return await bot.send_message(user_id,
            "❌ Could not resolve channel. Send /bypass to try again.")

    # Try to get title via bot
    try:
        ch_info = await bot.get_chat(channel_id)
        channel_title = ch_info.title or channel_title
        channel_id = ch_info.id
    except Exception:
        pass

    # Send status message and kick off job
    status_msg = await bot.send_message(
        user_id,
        f"<b>🔗 URL Bypass — Starting</b>\n\n"
        f"<b>Userbot:</b> 👤 {ub_name}\n"
        f"<b>Channel:</b> {channel_title}\n\n"
        f"⏳ Connecting userbot...",
        parse_mode='html',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🛑 Stop", callback_data=f"ub#bypass_stop_{user_id}")
        ]])
    )

    task = asyncio.create_task(
        _bypass_job_runner(bot, user_id, bot_id, ub_name, channel_id, channel_title, status_msg)
    )
    _bypass_sessions[user_id] = task


async def _bypass_job_runner(bot, user_id, bot_id, ub_name, channel_id, channel_title, status_msg):
    """Wrapper — handles setup, error catching, and cleanup."""
    ub = None
    try:
        ub = await _load_userbot(user_id, bot_id)
        if not ub:
            _bypass_sessions.pop(user_id, None)
            return await _update_progress(status_msg,
                "<b>❌ Failed to connect userbot!</b>\n\n"
                "Session may be expired. Go to Settings → Accounts to re-add.")

        await _update_progress(status_msg,
            f"<b>🔗 URL Bypass — Running</b>\n\n"
            f"<b>Userbot:</b> 👤 {ub_name}\n"
            f"<b>Channel:</b> {channel_title}\n\n"
            f"✅ Connected! Joining channel..."
        )

        # Join channel
        try:
            await ub.join_chat(channel_id)
        except Exception as e:
            err_s = str(e).lower()
            if 'already' in err_s or 'participant' in err_s:
                pass
            else:
                logger.warning(f"[URLBypass] Join warning: {e}")

        # Scan for links
        link_queue = await _scan_channel(ub, channel_id, status_msg)

        if not link_queue:
            _bypass_sessions.pop(user_id, None)
            return await _update_progress(status_msg,
                f"<b>⚠️ No Shortener Links Found!</b>\n\n"
                f"No matching shortener URLs found in the buttons of <b>{channel_title}</b>.\n\n"
                f"<i>Check that the channel has posts with inline buttons containing shortener links.</i>"
            )

        await _update_progress(status_msg,
            f"<b>🔗 URL Bypass — Queue Ready</b>\n\n"
            f"<b>Channel:</b> {channel_title}\n"
            f"<b>Links Found:</b> {len(link_queue)}\n\n"
            f"⚡ Starting bypass process..."
        )
        await asyncio.sleep(2)

        await _run_bypass_job(bot, user_id, ub, status_msg, link_queue)

    except asyncio.CancelledError:
        _bypass_sessions.pop(user_id, None)
        await _update_progress(status_msg, "🛑 <b>Bypass job cancelled.</b>")
    except Exception as e:
        _bypass_sessions.pop(user_id, None)
        logger.error(f"[URLBypass] Unexpected error: {e}", exc_info=True)
        await _update_progress(status_msg, f"<b>❌ Error:</b> <code>{str(e)[:200]}</code>")


# ── Stop Commands ─────────────────────────────────────────────────────────────

@Client.on_message(filters.private & filters.command('bypass_stop'))
async def bypass_stop_cmd(bot, message):
    user_id = message.from_user.id
    task = _bypass_sessions.pop(user_id, None)
    if task:
        task.cancel()
        await message.reply_text("🛑 <b>Bypass job stopping...</b>", parse_mode='html')
    else:
        await message.reply_text("ℹ️ No bypass job currently running.")


@Client.on_callback_query(filters.regex(r'^ub#bypass_stop_'))
async def bypass_stop_cb(bot, query):
    target_uid = int(query.data.replace('ub#bypass_stop_', ''))
    if query.from_user.id != target_uid:
        return await query.answer("This is not your job!", show_alert=True)
    task = _bypass_sessions.pop(target_uid, None)
    if task:
        task.cancel()
        await query.answer("🛑 Stopping...", show_alert=True)
        try:
            await query.message.edit_text("🛑 <b>Bypass job cancelled.</b>", parse_mode='html')
        except Exception:
            pass
    else:
        await query.answer("No active bypass job found.", show_alert=True)
