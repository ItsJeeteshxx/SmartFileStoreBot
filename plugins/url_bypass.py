"""
URL Bypass Agentic System — Integrated into Arya Forward Bot
============================================================
Flow:
  1. User clicks "🔗 URL Bypass" from main menu or sends /bypass
  2. Bot shows userbots list → user picks one manually
  3. Bot asks for source channel link/ID
  4. Selected userbot joins that channel (if not already a member)
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
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from database import db
from config import Config

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

# Username of the URL shortener bypass bot
BYPASS_BOT = "Nick_Bypass_Bot"

# How many seconds of no new files = "batch done"
# This is dynamic — starts at 15s and adjusts based on observed delivery speed
BASE_IDLE_TIMEOUT = 15

# Delay between sending each link to bypass bot (anti-spam)
INTER_LINK_DELAY = 5

# Regex patterns to detect shortener links in button URLs
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
]
SHORTENER_RE = re.compile('|'.join(SHORTENER_PATTERNS), re.IGNORECASE)

# Active bypass sessions keyed by user_id
_bypass_sessions: dict[int, asyncio.Task] = {}

# ── Helpers ──────────────────────────────────────────────────────────────────

async def _load_userbot(user_id: int, bot_id: str):
    """Load and return a connected Pyrogram userbot Client for the given bot record."""
    from plugins.test import get_configs, CLIENT
    bots = await db.get_bots(user_id)
    userbots = [b for b in bots if not b.get('is_bot', True)]
    target = next((b for b in userbots if str(b.get('id', '')) == str(bot_id)), None)
    if not target:
        return None
    session_str = target.get('session_string') or target.get('session')
    if not session_str:
        return None
    try:
        ub = Client(
            f"bypass_ub_{bot_id}",
            api_id=Config.APP_ID,
            api_hash=Config.API_HASH,
            session_string=session_str,
            in_memory=True,
            no_updates=False,
        )
        await ub.start()
        return ub
    except Exception as e:
        logger.error(f"[URLBypass] Failed to start userbot {bot_id}: {e}")
        return None


def _extract_shortener_links(message) -> list[tuple[str, str]]:
    """
    Returns list of (button_label, url) tuples from inline keyboard
    where the url matches a known shortener pattern.
    """
    results = []
    if not message.reply_markup:
        return results
    for row in message.reply_markup.inline_keyboard:
        for btn in row:
            url = getattr(btn, 'url', None) or ''
            if url and SHORTENER_RE.search(url):
                label = btn.text.strip() or 'No Label'
                results.append((label, url))
    return results


def _extract_bypass_result(text: str) -> str | None:
    """
    Parse the bypass bot's reply to extract the final bypassed URL.
    Expects lines like:  Bypassed Link:✅ https://t.me/...
    """
    if not text:
        return None
    # Try Bypassed Link pattern first
    m = re.search(r'Bypassed Link[^:]*:[\s✅]*(\S+)', text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Fallback: look for any t.me URL with ?start=
    m = re.search(r'(https?://t\.me/\S+\?start=\S+)', text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def _parse_start_link(url: str) -> tuple[str | None, str | None]:
    """
    Parse t.me links:
      https://t.me/BotUsername?start=PARAM  →  ('BotUsername', 'PARAM')
      https://telegram.me/BotUsername?start=PARAM  →  same
    """
    m = re.search(r't\.me/([^/?]+)\?start=(.+)', url, re.IGNORECASE)
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

async def _run_bypass_job(bot, user_id: int, ub: Client, channel_id, status_msg, link_queue: list):
    """
    Main agentic loop — processes the link queue one by one.
    link_queue: list of (label, url) tuples
    """
    total = len(link_queue)
    done = 0
    failed = []

    for idx, (label, shortener_url) in enumerate(link_queue):

        # Check if cancelled
        if user_id not in _bypass_sessions:
            break

        progress_txt = (
            f"<b>🔗 URL Bypass — Running</b>\n\n"
            f"<b>Progress:</b> {done}/{total} done\n"
            f"<b>Current:</b> <code>{label}</code> ({idx+1}/{total})\n"
            f"<b>Step:</b> Sending to bypass bot...\n\n"
            f"<i>⏱ Send /bypass_stop to cancel</i>"
        )
        await _update_progress(status_msg, progress_txt)

        # ── Step 1: Send shortener URL to bypass bot ──────────────────────────
        try:
            sent_msg = await ub.send_message(BYPASS_BOT, shortener_url)
        except Exception as e:
            logger.error(f"[URLBypass] Failed to send to bypass bot: {e}")
            failed.append((label, f"Send failed: {e}"))
            await asyncio.sleep(INTER_LINK_DELAY)
            continue

        # ── Step 2: Wait for bypass bot reply (up to 60 seconds) ─────────────
        bypassed_url = None
        reply_wait_start = time.time()

        await _update_progress(status_msg,
            f"<b>🔗 URL Bypass — Running</b>\n\n"
            f"<b>Progress:</b> {done}/{total} done\n"
            f"<b>Current:</b> <code>{label}</code>\n"
            f"<b>Step:</b> ⏳ Waiting for bypass bot reply...\n\n"
            f"<i>⏱ Send /bypass_stop to cancel</i>"
        )

        for _ in range(60):  # Wait up to 60 seconds
            await asyncio.sleep(1)
            if user_id not in _bypass_sessions:
                break
            # Check latest messages from bypass bot
            try:
                async for msg in ub.get_chat_history(BYPASS_BOT, limit=3):
                    if msg.date and (time.time() - msg.date.timestamp()) < 90:
                        candidate = _extract_bypass_result(msg.text or msg.caption or '')
                        if candidate and candidate != shortener_url:
                            bypassed_url = candidate
                            break
                if bypassed_url:
                    break
            except Exception:
                pass

        if not bypassed_url:
            logger.warning(f"[URLBypass] No bypass result for: {shortener_url}")
            failed.append((label, "Bypass bot did not reply in time"))
            await asyncio.sleep(INTER_LINK_DELAY)
            continue

        # ── Step 3: Parse the bypassed link ───────────────────────────────────
        bot_username, start_param = _parse_start_link(bypassed_url)

        if not bot_username or not start_param:
            # Not a /start link — maybe it's a direct file URL or something else
            logger.info(f"[URLBypass] Bypassed URL is not a ?start= link: {bypassed_url}")
            failed.append((label, f"Not a start link: {bypassed_url}"))
            done += 1
            await asyncio.sleep(INTER_LINK_DELAY)
            continue

        await _update_progress(status_msg,
            f"<b>🔗 URL Bypass — Running</b>\n\n"
            f"<b>Progress:</b> {done}/{total} done\n"
            f"<b>Current:</b> <code>{label}</code>\n"
            f"<b>Step:</b> 🤖 Sending /start to @{bot_username}...\n\n"
            f"<i>⏱ Send /bypass_stop to cancel</i>"
        )

        # ── Step 4: Send /start to the target bot ─────────────────────────────
        try:
            await ub.send_message(bot_username, f"/start {start_param}")
        except Exception as e:
            logger.error(f"[URLBypass] Failed to start target bot: {e}")
            failed.append((label, f"/start failed: {e}"))
            await asyncio.sleep(INTER_LINK_DELAY)
            continue

        # ── Step 5: Smart wait — wait for files to stop coming ────────────────
        await _update_progress(status_msg,
            f"<b>🔗 URL Bypass — Running</b>\n\n"
            f"<b>Progress:</b> {done}/{total} done\n"
            f"<b>Current:</b> <code>{label}</code>\n"
            f"<b>Step:</b> 📥 Waiting for files from @{bot_username}...\n\n"
            f"<i>⏱ Send /bypass_stop to cancel</i>"
        )

        # Smart idle wait: track last message time from target bot
        idle_start = time.time()
        files_received = 0
        last_msg_time = time.time()
        first_file_received = False
        adaptive_timeout = BASE_IDLE_TIMEOUT  # Will grow if more files are arriving

        # Record the timestamp BEFORE we start waiting so we only count new messages
        wait_since = time.time()

        while True:
            await asyncio.sleep(2)

            if user_id not in _bypass_sessions:
                break

            elapsed_since_last = time.time() - last_msg_time

            try:
                # Check for new messages from the target bot
                new_found = 0
                async for msg in ub.get_chat_history(bot_username, limit=10):
                    msg_ts = msg.date.timestamp() if msg.date else 0
                    if msg_ts < wait_since:
                        break
                    if msg.media or msg.document or msg.video or msg.audio:
                        new_found += 1
                        if msg_ts > last_msg_time:
                            last_msg_time = msg_ts
                            first_file_received = True

                if new_found > files_received:
                    # More files arrived, reset idle clock
                    delta = new_found - files_received
                    files_received = new_found
                    elapsed_since_last = 0

                    # Adapt timeout: if files are coming fast, give more time
                    if delta >= 3:
                        adaptive_timeout = max(adaptive_timeout, 20)
                    if new_found >= 10:
                        adaptive_timeout = max(adaptive_timeout, 30)

            except Exception:
                pass

            # If we've received at least one file and it's been idle for adaptive_timeout
            if first_file_received and elapsed_since_last >= adaptive_timeout:
                break

            # Hard timeout: if no file arrived in first 90 seconds, give up
            if not first_file_received and (time.time() - wait_since) > 90:
                break

            # Update progress with file count
            await _update_progress(status_msg,
                f"<b>🔗 URL Bypass — Running</b>\n\n"
                f"<b>Progress:</b> {done}/{total} done\n"
                f"<b>Current:</b> <code>{label}</code>\n"
                f"<b>Files received:</b> {files_received} 📥\n"
                f"<b>Idle:</b> {int(time.time() - last_msg_time)}s / {adaptive_timeout}s\n\n"
                f"<i>⏱ Send /bypass_stop to cancel</i>"
            )

        done += 1
        await asyncio.sleep(INTER_LINK_DELAY)

    # ── Job Complete ───────────────────────────────────────────────────────────
    _bypass_sessions.pop(user_id, None)

    fail_text = ''
    if failed:
        fail_lines = '\n'.join(f"• <code>{lbl}</code>: {reason}" for lbl, reason in failed[:10])
        fail_text = f"\n\n<b>⚠️ Failed ({len(failed)}):</b>\n{fail_lines}"

    final_txt = (
        f"<b>✅ URL Bypass — Complete!</b>\n\n"
        f"<b>Total Links:</b> {total}\n"
        f"<b>✅ Processed:</b> {done}\n"
        f"<b>❌ Failed:</b> {len(failed)}"
        f"{fail_text}"
    )
    await _update_progress(status_msg, final_txt)

    try:
        await ub.stop()
    except Exception:
        pass


# ── Scan Channel for Shortener Links ─────────────────────────────────────────

async def _scan_channel(ub: Client, channel_id, status_msg) -> list[tuple[str, str]]:
    """
    Scans all messages in the channel and returns a list of (label, url)
    tuples for all shortener links found in inline buttons.
    """
    all_links = []
    scanned = 0

    await _update_progress(status_msg,
        "<b>🔍 Scanning channel messages...</b>\n\n"
        "<i>This may take a moment for large channels.</i>"
    )

    async for msg in ub.get_chat_history(channel_id):
        scanned += 1
        links = _extract_shortener_links(msg)
        all_links.extend(links)

        if scanned % 50 == 0:
            await _update_progress(status_msg,
                f"<b>🔍 Scanning channel...</b>\n\n"
                f"<b>Scanned:</b> {scanned} messages\n"
                f"<b>Links found so far:</b> {len(all_links)}"
            )

    return all_links


# ── Entry Points ─────────────────────────────────────────────────────────────

@Client.on_message(filters.private & filters.command('bypass'))
async def bypass_cmd(bot, message):
    user_id = message.from_user.id
    await message.delete()
    await _show_bypass_menu(bot, user_id, message.chat.id)


@Client.on_callback_query(filters.regex(r'^ub#bypass'))
async def bypass_cb(bot, query):
    user_id = query.from_user.id
    await query.answer()
    await query.message.delete()
    await _show_bypass_menu(bot, user_id, query.message.chat.id)


async def _show_bypass_menu(bot, user_id: int, chat_id: int):
    """Show the URL Bypass main menu with userbot selection."""
    bots = await db.get_bots(user_id)
    userbots = [b for b in bots if not b.get('is_bot', True)]

    if not userbots:
        return await bot.send_message(
            chat_id,
            "<b>❌ No Userbots Found!</b>\n\n"
            "You need to add at least one userbot first.\n"
            "Go to <b>Settings → Accounts → Add Userbot</b>.",
            parse_mode='html',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⚙️ Settings", callback_data="settings#accounts")
            ]])
        )

    btns = []
    for ub in userbots:
        active_mark = "✔️ " if ub.get('active') else ""
        name = ub.get('name', f"Userbot {ub.get('id', '?')}")
        ub_id = ub.get('id', '')
        btns.append([InlineKeyboardButton(
            f"{active_mark}👤 {name}",
            callback_data=f"ub#bypass_select_{ub_id}"
        )])

    btns.append([InlineKeyboardButton("❮ Back", callback_data="back")])

    txt = (
        "<b>🔗 URL Bypass System</b>\n\n"
        "This tool automatically:\n"
        "• Scans a Telegram channel for shortener links\n"
        "• Bypasses them one-by-one via the bypass bot\n"
        "• Clicks the bypassed links and waits for files\n\n"
        "<b>Step 1: Select which userbot to use:</b>"
    )

    await bot.send_message(chat_id, txt, parse_mode='html',
                           reply_markup=InlineKeyboardMarkup(btns))


@Client.on_callback_query(filters.regex(r'^ub#bypass_select_'))
async def bypass_select_userbot(bot, query):
    user_id = query.from_user.id
    await query.answer()

    if user_id in _bypass_sessions:
        return await query.answer("⚠️ A bypass job is already running! Send /bypass_stop to cancel it.", show_alert=True)

    bot_id = query.data.split('ub#bypass_select_')[1]

    # Get userbot name for display
    bots = await db.get_bots(user_id)
    ub_info = next((b for b in bots if str(b.get('id', '')) == str(bot_id)), {})
    ub_name = ub_info.get('name', f'Userbot {bot_id}')

    await query.message.edit_text(
        f"<b>🔗 URL Bypass — Selected: 👤 {ub_name}</b>\n\n"
        "<b>Step 2: Send the Source Channel</b>\n\n"
        "Send the channel link or ID, e.g.:\n"
        "• <code>https://t.me/channelname</code>\n"
        "• <code>-1001234567890</code>\n"
        "• Forward a message from that channel\n\n"
        "Send /cancel to abort.",
        parse_mode='html'
    )

    # Listen for channel input
    try:
        resp = await bot.listen(chat_id=query.message.chat.id, timeout=120)
    except asyncio.TimeoutError:
        return await bot.send_message(user_id, "⏰ Timed out. Send /bypass to try again.")

    if not resp:
        return

    txt = (resp.text or '').strip()

    if txt.lower() in ['/cancel', 'cancel']:
        await resp.delete()
        return await bot.send_message(user_id, "❌ Cancelled.", parse_mode='html',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❮ Back", callback_data="back")]]))

    # Resolve channel
    channel_id = None
    channel_title = "Unknown"

    try:
        # Forward from channel
        if resp.forward_from_chat:
            channel_id = resp.forward_from_chat.id
            channel_title = resp.forward_from_chat.title or str(channel_id)
        elif txt.lstrip('-').isdigit():
            channel_id = int(txt)
        elif 't.me/' in txt:
            m = re.search(r't\.me/c/(\d+)', txt)
            if m:
                channel_id = int('-100' + m.group(1))
            else:
                m = re.search(r't\.me/([^/\s]+)', txt)
                if m:
                    channel_id = '@' + m.group(1).split('?')[0]
        await resp.delete()
    except Exception:
        pass

    if not channel_id:
        return await bot.send_message(user_id,
            "❌ Could not resolve channel. Please send a valid link or ID.\n\nSend /bypass to try again.")

    # Try to get channel info via bot (optional, may fail if bot is not a member)
    try:
        ch_info = await bot.get_chat(channel_id)
        channel_title = ch_info.title or channel_title
        if hasattr(ch_info, 'id'):
            channel_id = ch_info.id
    except Exception:
        pass

    # Show confirmation and start
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

    # Start the bypass job
    task = asyncio.create_task(
        _bypass_job_runner(bot, user_id, bot_id, ub_name, channel_id, channel_title, status_msg)
    )
    _bypass_sessions[user_id] = task


async def _bypass_job_runner(bot, user_id, bot_id, ub_name, channel_id, channel_title, status_msg):
    """Wrapper that handles setup and error catching for the bypass job."""
    ub = None
    try:
        # Connect userbot
        ub = await _load_userbot(user_id, bot_id)
        if not ub:
            return await _update_progress(status_msg,
                f"<b>❌ Failed to connect userbot!</b>\n\n"
                "Check that your userbot session is valid and not expired.\n"
                "Go to Settings → Accounts to re-add the userbot.")

        await _update_progress(status_msg,
            f"<b>🔗 URL Bypass — Running</b>\n\n"
            f"<b>Userbot:</b> 👤 {ub_name}\n"
            f"<b>Channel:</b> {channel_title}\n\n"
            f"✅ Connected! Joining channel..."
        )

        # Join the channel if not already a member
        try:
            await ub.join_chat(channel_id)
        except Exception as e:
            if 'USER_ALREADY_PARTICIPANT' in str(e) or 'already' in str(e).lower():
                pass  # Already a member, fine
            else:
                logger.warning(f"[URLBypass] Join error (may be ok): {e}")

        # Scan channel for shortener links
        link_queue = await _scan_channel(ub, channel_id, status_msg)

        if not link_queue:
            _bypass_sessions.pop(user_id, None)
            return await _update_progress(status_msg,
                f"<b>⚠️ No Shortener Links Found!</b>\n\n"
                f"Scanned <b>{channel_title}</b> but found no shortener links in any buttons.\n\n"
                f"Make sure the channel contains posts with inline buttons containing shortener URLs."
            )

        await _update_progress(status_msg,
            f"<b>🔗 URL Bypass — Starting Queue</b>\n\n"
            f"<b>Channel:</b> {channel_title}\n"
            f"<b>Links Found:</b> {len(link_queue)}\n\n"
            f"Starting bypass process... ⚡"
        )

        await asyncio.sleep(2)

        # Run the main bypass engine
        await _run_bypass_job(bot, user_id, ub, channel_id, status_msg, link_queue)

    except asyncio.CancelledError:
        _bypass_sessions.pop(user_id, None)
        await _update_progress(status_msg, "🛑 <b>Bypass job cancelled.</b>")
    except Exception as e:
        _bypass_sessions.pop(user_id, None)
        logger.error(f"[URLBypass] Unexpected error: {e}", exc_info=True)
        await _update_progress(status_msg, f"<b>❌ Error:</b> <code>{e}</code>")
    finally:
        if ub:
            try:
                await ub.stop()
            except Exception:
                pass


@Client.on_message(filters.private & filters.command('bypass_stop'))
async def bypass_stop_cmd(bot, message):
    user_id = message.from_user.id
    task = _bypass_sessions.pop(user_id, None)
    if task:
        task.cancel()
        await message.reply_text("🛑 <b>Bypass job stopping...</b>", parse_mode='html')
    else:
        await message.reply_text("ℹ️ No bypass job is currently running.")


@Client.on_callback_query(filters.regex(r'^ub#bypass_stop_'))
async def bypass_stop_cb(bot, query):
    target_uid = int(query.data.split('ub#bypass_stop_')[1])
    if query.from_user.id != target_uid:
        return await query.answer("This is not your job!", show_alert=True)
    task = _bypass_sessions.pop(target_uid, None)
    if task:
        task.cancel()
        await query.answer("🛑 Stopping bypass job...", show_alert=True)
        try:
            await query.message.edit_text("🛑 <b>Bypass job cancelled by user.</b>", parse_mode='html')
        except Exception:
            pass
    else:
        await query.answer("No active bypass job found.", show_alert=True)
