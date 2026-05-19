"""URL Bypass Agentic System - Arya Forward Bot"""
from __future__ import annotations
import asyncio, re, time, logging
from typing import Optional
from pyrogram import Client, filters, enums, ContinuePropagation
from pyrogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from database import db

logger = logging.getLogger(__name__)
PM = enums.ParseMode.HTML

BYPASS_BOT      = "Nick_Bypass_Bot"
BASE_IDLE_SEC   = 15
INTER_DELAY     = 5

SHORTENER_RE = re.compile(
    r'urlshortx\.io|shrinkme\.io|ouo\.io|short2url\.com|adf\.ly|'
    r'linkvertise\.com|rekonise\.com|bc\.vc|za\.gl|exe\.io|'
    r'gplinks\.in|tnshort\.com|linkshrink\.net|droplink\.co|'
    r'techymedies\.com|modijiurl\.com|instantlinks\.in|earnl\.in',
    re.IGNORECASE
)

_sessions: dict  = {}
_waiting:  dict  = {}

CANCEL_BTN = KeyboardButton("⛔ Cᴀɴᴄᴇʟ")
UNDO_BTN   = KeyboardButton("↩️ Uɴᴅᴏ")

def _is_cancel(t): return "⛔" in t or "cancel" in t.lower()
def _is_undo(t):   return "↩️" in t or "undo" in t.lower()

# ── Input router (same pattern as taskjob) ────────────────────────────────────
@Client.on_message(filters.private, group=-15)
async def _ub_bypass_router(bot, message):
    uid = message.from_user.id if message.from_user else None
    if uid and uid in _waiting:
        fut = _waiting.pop(uid)
        if not fut.done():
            fut.set_result(message)
    raise ContinuePropagation

async def _ask(bot, user_id: int, text: str, reply_markup=None, timeout: int = 300):
    loop = asyncio.get_event_loop()
    fut  = loop.create_future()
    old  = _waiting.pop(user_id, None)
    if old and not old.done():
        old.cancel()
    _waiting[user_id] = fut
    await bot.send_message(user_id, text, parse_mode=PM, reply_markup=reply_markup,
                           disable_web_page_preview=True)
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        _waiting.pop(user_id, None)
        raise

# ── Helpers ───────────────────────────────────────────────────────────────────
async def _load_ub(user_id: int, bot_id: str):
    from plugins.test import start_clone_bot, CLIENT
    bots     = await db.get_bots(user_id)
    userbots = [b for b in bots if not b.get('is_bot', True)]
    target   = next((b for b in userbots if str(b.get('id','')) == str(bot_id)), None)
    if not target:
        return None
    try:
        ub = CLIENT().client(target)
        return await start_clone_bot(ub)
    except Exception as e:
        logger.error(f"[Bypass] ub load fail: {e}")
        return None

def _get_links(message) -> list:
    """Return ALL (label, url) shortener tuples from a message's inline buttons IN ORDER."""
    out = []
    if not message.reply_markup:
        return out
    for row in getattr(message.reply_markup, 'inline_keyboard', []):
        for btn in row:
            url = getattr(btn, 'url', None) or ''
            if url and SHORTENER_RE.search(url):
                out.append(((btn.text or '').strip() or 'Link', url))
    return out

def _parse_bypassed(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r'Bypassed Link[^:]*:[\s\u2705]*(\S+)', text, re.I)
    if m: return m.group(1).strip()
    m = re.search(r'(https?://(?:t\.me|telegram\.me)/\S+\?start=\S+)', text, re.I)
    if m: return m.group(1).strip()
    return None

def _parse_start(url: str) -> tuple:
    m = re.search(r'(?:t\.me|telegram\.me)/([^/?]+)\?start=(.+)', url, re.I)
    return (m.group(1).strip(), m.group(2).strip()) if m else (None, None)

async def _upd(msg, text: str):
    try:
        await msg.edit_text(text, parse_mode=PM, disable_web_page_preview=True)
    except Exception:
        pass

def _resolve_channel(txt: str, fwd_chat=None):
    if fwd_chat:
        return fwd_chat.id, fwd_chat.title or str(fwd_chat.id)
    txt = txt.strip()
    if txt.lstrip('-').isdigit():
        return int(txt), txt
    m = re.search(r't\.me/c/(\d+)', txt)
    if m: return int('-100' + m.group(1)), txt
    m = re.search(r't\.me/([^/\s?]+)', txt)
    if m: return '@' + m.group(1), m.group(1)
    return None, None

# ── Scan channel ──────────────────────────────────────────────────────────────
async def _scan(ub, channel_id, status_msg, order: str, start_id: int, end_id: int) -> list:
    """
    Scan channel messages and return ALL shortener links in order.
    order: 'new_to_old' | 'old_to_new'
    start_id/end_id: 0 = no limit
    """
    all_links = []
    scanned   = 0
    await _upd(status_msg, "<b>»  Scanning channel messages...</b>")

    try:
        async for msg in ub.get_chat_history(channel_id):
            mid = msg.id
            # Range filter: get_chat_history returns newest first (descending IDs)
            if end_id and mid > end_id:
                continue
            if start_id and mid < start_id:
                break  # IDs only go lower from here, no point continuing

            scanned += 1
            links = _get_links(msg)
            if links:
                all_links.append((msg.id, links))

            if scanned % 100 == 0:
                await _upd(status_msg,
                    f"<b>»  Scanning...</b>\n\n"
                    f"»  Scanned: <code>{scanned}</code> messages\n"
                    f"»  Posts with links: <code>{len(all_links)}</code>")
    except Exception as e:
        logger.error(f"[Bypass] scan error: {e}")

    # get_chat_history gives new→old; reverse for old→new
    if order == 'old_to_new':
        all_links.reverse()

    # Flatten to (post_id, label, url) list preserving button order within each post
    flat = []
    for (post_id, links) in all_links:
        for lbl, url in links:
            flat.append((post_id, lbl, url))
    return flat

# ── Core bypass loop ──────────────────────────────────────────────────────────
async def _run_bypass(bot, user_id: int, ub, status_msg, queue: list):
    total  = len(queue)
    done   = 0
    failed = []

    for idx, (post_id, label, short_url) in enumerate(queue):
        if user_id not in _sessions:
            break

        await _upd(status_msg,
            f"<b>»  URL Bypass — Running</b>\n\n"
            f"»  Progress: <code>{done}/{total}</code>\n"
            f"»  Post ID: <code>{post_id}</code>  |  Link <code>{idx+1}/{total}</code>\n"
            f"»  Label: <code>{label[:35]}</code>\n"
            f"»  Step: Sending to bypass bot...\n\n"
            f"<i>Send /bypass_stop to cancel</i>"
        )

        try:
            await ub.send_message(BYPASS_BOT, short_url)
        except Exception as e:
            failed.append((label, f"Send failed: {e}"))
            await asyncio.sleep(INTER_DELAY)
            continue

        # Wait for reply
        bypassed = None
        t0 = time.time()
        for _ in range(60):
            await asyncio.sleep(1)
            if user_id not in _sessions: break
            try:
                async for m in ub.get_chat_history(BYPASS_BOT, limit=5):
                    ts = m.date.timestamp() if m.date else 0
                    if ts < t0 - 5: break
                    c = _parse_bypassed(m.text or m.caption or '')
                    if c and c != short_url:
                        bypassed = c; break
                if bypassed: break
            except Exception:
                pass

        if not bypassed:
            failed.append((label, "No bypass reply"))
            await asyncio.sleep(INTER_DELAY)
            continue

        bot_uname, param = _parse_start(bypassed)
        if not bot_uname:
            failed.append((label, "Not a ?start= link"))
            done += 1
            await asyncio.sleep(INTER_DELAY)
            continue

        await _upd(status_msg,
            f"<b>»  URL Bypass — Running</b>\n\n"
            f"»  Progress: <code>{done}/{total}</code>\n"
            f"»  Step: Sending /start to @{bot_uname}...\n\n"
            f"<i>Send /bypass_stop to cancel</i>"
        )

        try:
            await ub.send_message(bot_uname, f"/start {param}")
        except Exception as e:
            failed.append((label, f"/start failed: {e}"))
            await asyncio.sleep(INTER_DELAY)
            continue

        # Smart idle wait
        wait_since = time.time()
        files = 0; last_file = time.time(); got_file = False
        adaptive = BASE_IDLE_SEC

        while True:
            await asyncio.sleep(2)
            if user_id not in _sessions: break
            try:
                nc = 0
                async for m in ub.get_chat_history(bot_uname, limit=20):
                    ts = m.date.timestamp() if m.date else 0
                    if ts < wait_since: break
                    if m.media or m.document or m.video or m.audio or m.voice:
                        nc += 1
                        if ts > last_file: last_file = ts; got_file = True
                if nc > files:
                    d = nc - files; files = nc
                    if d >= 3:  adaptive = max(adaptive, 20)
                    if nc >= 10: adaptive = max(adaptive, 30)
            except Exception:
                pass
            idle = time.time() - last_file
            waited = time.time() - wait_since
            if got_file and idle >= adaptive: break
            if not got_file and waited > 90:  break
            await _upd(status_msg,
                f"<b>»  URL Bypass — Running</b>\n\n"
                f"»  Progress: <code>{done}/{total}</code>\n"
                f"»  Step: Waiting for files from @{bot_uname}\n"
                f"»  Files received: <code>{files}</code>\n"
                f"»  Idle: <code>{int(idle)}s / {adaptive}s</code>\n\n"
                f"<i>Send /bypass_stop to cancel</i>"
            )

        done += 1
        await asyncio.sleep(INTER_DELAY)

    _sessions.pop(user_id, None)
    fail_txt = ''
    if failed:
        lines = '\n'.join(f"  • {lb[:25]}: {rs[:40]}" for lb, rs in failed[:8])
        fail_txt = f"\n\n<b>»  Failed ({len(failed)}):</b>\n{lines}"
    await _upd(status_msg,
        f"<b>»  URL Bypass — Complete!</b>\n\n"
        f"»  Total: <code>{total}</code>  |  Done: <code>{done}</code>  |  Failed: <code>{len(failed)}</code>"
        f"{fail_txt}"
    )

# ── Entry point callback ───────────────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r'^ub#bypass$'))
async def bypass_cb(bot, query):
    await query.answer()
    user_id = query.from_user.id
    chat_id = query.message.chat.id
    try:
        await query.message.delete()
    except Exception:
        pass
    await _bypass_flow(bot, user_id, chat_id)

@Client.on_message(filters.private & filters.command('bypass'))
async def bypass_cmd(bot, message):
    await _bypass_flow(bot, message.from_user.id, message.chat.id)

# ── Interactive setup flow ─────────────────────────────────────────────────────
async def _bypass_flow(bot, user_id: int, chat_id: int):
    if user_id in _sessions:
        await bot.send_message(chat_id,
            "<b>»  A bypass job is already running!</b>\nSend /bypass_stop to cancel it first.",
            parse_mode=PM)
        return

    # ── Step 1: Select Userbot ────────────────────────────────────
    bots     = await db.get_bots(user_id)
    userbots = [b for b in bots if not b.get('is_bot', True)]

    if not userbots:
        await bot.send_message(chat_id,
            "<b>»  No Userbots Found!</b>\n\nAdd a userbot first:\nSettings → Accounts → Add Userbot",
            parse_mode=PM)
        return

    ub_btns = [[KeyboardButton(f"👤 {b.get('name','?')}  [{b.get('id','')}]")] for b in userbots]
    ub_btns.append([CANCEL_BTN])

    try:
        r1 = await _ask(bot, user_id,
            "<b>»  URL Bypass — Step 1/5</b>\n\n"
            "Select the <b>Userbot</b> to run this job:\n\n"
            "<blockquote>The userbot must already be a member of the source channel.</blockquote>",
            reply_markup=ReplyKeyboardMarkup(ub_btns, resize_keyboard=True, one_time_keyboard=True))
    except asyncio.TimeoutError:
        return await bot.send_message(chat_id, "<i>Timed out.</i>", parse_mode=PM,
            reply_markup=ReplyKeyboardRemove())

    if _is_cancel(r1.text):
        return await bot.send_message(chat_id, "<i>Process Cancelled!</i>", parse_mode=PM,
            reply_markup=ReplyKeyboardRemove())

    # Parse selected userbot
    bot_id  = None
    ub_name = r1.text
    if '[' in r1.text and ']' in r1.text:
        try: bot_id = r1.text.split('[')[-1].split(']')[0].strip()
        except Exception: pass
    if not bot_id:
        bot_id = userbots[0].get('id', '')
    sel_ub = next((b for b in userbots if str(b.get('id','')) == str(bot_id)), userbots[0])
    ub_name = sel_ub.get('name', f'Userbot {bot_id}')

    # ── Step 2: Source Channel ────────────────────────────────────
    try:
        r2 = await _ask(bot, user_id,
            "<b>»  URL Bypass — Step 2/5</b>\n\n"
            "Send the <b>Source Channel</b>:\n\n"
            "<blockquote expandable>"
            "• <code>https://t.me/channelname</code>\n"
            "• <code>https://t.me/c/1234567890/1</code>\n"
            "• <code>-1001234567890</code>\n"
            "• Forward any message from the channel\n\n"
            "The userbot will join it automatically if not already a member."
            "</blockquote>",
            reply_markup=ReplyKeyboardMarkup([[UNDO_BTN, CANCEL_BTN]],
                resize_keyboard=True, one_time_keyboard=True))
    except asyncio.TimeoutError:
        return await bot.send_message(chat_id, "<i>Timed out.</i>", parse_mode=PM,
            reply_markup=ReplyKeyboardRemove())

    if _is_cancel(r2.text):
        return await bot.send_message(chat_id, "<i>Process Cancelled!</i>", parse_mode=PM,
            reply_markup=ReplyKeyboardRemove())

    # Resolve channel
    fwd_chat = getattr(r2, 'forward_from_chat', None)
    channel_id, channel_title = _resolve_channel(r2.text, fwd_chat)
    if not channel_id:
        await bot.send_message(chat_id, "<b>Could not resolve channel.</b>\nSend /bypass to retry.",
            parse_mode=PM, reply_markup=ReplyKeyboardRemove())
        return
    try:
        ci = await bot.get_chat(channel_id)
        channel_title = ci.title or channel_title
        channel_id    = ci.id
    except Exception:
        pass

    # ── Step 3: Scan Range ────────────────────────────────────────
    try:
        r3 = await _ask(bot, user_id,
            f"<b>»  URL Bypass — Step 3/5</b>\n\n"
            f"Channel: <b>{channel_title}</b>\n\n"
            "Set the <b>message scan range</b>:\n\n"
            "<blockquote expandable>"
            "• <b>ALL</b> — scan entire channel\n"
            "• <code>100:500</code> — only scan message IDs 100–500\n"
            "• <code>100</code> — start from message ID 100 to end\n\n"
            "Tip: Copy a message link to get its ID."
            "</blockquote>",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("ALL")], [UNDO_BTN, CANCEL_BTN]],
                resize_keyboard=True, one_time_keyboard=True))
    except asyncio.TimeoutError:
        return await bot.send_message(chat_id, "<i>Timed out.</i>", parse_mode=PM,
            reply_markup=ReplyKeyboardRemove())

    if _is_cancel(r3.text):
        return await bot.send_message(chat_id, "<i>Process Cancelled!</i>", parse_mode=PM,
            reply_markup=ReplyKeyboardRemove())

    scan_start = 0; scan_end = 0
    rt = r3.text.strip()
    if rt.lower() != 'all':
        if ':' in rt:
            try: scan_start = int(rt.split(':')[0].strip())
            except Exception: pass
            try: scan_end   = int(rt.split(':')[1].strip())
            except Exception: pass
        else:
            try: scan_start = int(rt)
            except Exception: pass

    # ── Step 4: Order ─────────────────────────────────────────────
    try:
        r4 = await _ask(bot, user_id,
            "<b>»  URL Bypass — Step 4/5</b>\n\n"
            "Choose <b>processing order</b>:\n\n"
            "<blockquote>"
            "• <b>New → Old</b> — latest posts first\n"
            "• <b>Old → New</b> — oldest posts first"
            "</blockquote>",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("»  New → Old"), KeyboardButton("»  Old → New")],
                 [UNDO_BTN, CANCEL_BTN]],
                resize_keyboard=True, one_time_keyboard=True))
    except asyncio.TimeoutError:
        return await bot.send_message(chat_id, "<i>Timed out.</i>", parse_mode=PM,
            reply_markup=ReplyKeyboardRemove())

    if _is_cancel(r4.text):
        return await bot.send_message(chat_id, "<i>Process Cancelled!</i>", parse_mode=PM,
            reply_markup=ReplyKeyboardRemove())

    order = 'old_to_new' if 'Old → New' in r4.text else 'new_to_old'
    order_label = '🕐 Old → New' if order == 'old_to_new' else '🕑 New → Old'

    # ── Step 5: Confirm ───────────────────────────────────────────
    range_label = f"<code>{scan_start}:{scan_end}</code>" if (scan_start or scan_end) else "ALL messages"
    try:
        r5 = await _ask(bot, user_id,
            f"<b>»  URL Bypass — Confirm</b>\n\n"
            f"»  Userbot: <b>{ub_name}</b>\n"
            f"»  Channel: <b>{channel_title}</b>\n"
            f"»  Range: {range_label}\n"
            f"»  Order: {order_label}\n\n"
            "<i>Confirm to start scanning and bypassing.</i>",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("✅ Confirm & Start")], [CANCEL_BTN]],
                resize_keyboard=True, one_time_keyboard=True))
    except asyncio.TimeoutError:
        return await bot.send_message(chat_id, "<i>Timed out.</i>", parse_mode=PM,
            reply_markup=ReplyKeyboardRemove())

    if _is_cancel(r5.text) or '✅' not in r5.text:
        return await bot.send_message(chat_id, "<i>Process Cancelled!</i>", parse_mode=PM,
            reply_markup=ReplyKeyboardRemove())

    # ── Start Job ─────────────────────────────────────────────────
    status_msg = await bot.send_message(
        chat_id,
        f"<b>»  URL Bypass — Starting</b>\n\n"
        f"»  Userbot: <b>{ub_name}</b>\n"
        f"»  Channel: <b>{channel_title}</b>\n"
        f"»  Order: {order_label}\n\n"
        f"⏳ Connecting userbot...",
        parse_mode=PM,
        reply_markup=ReplyKeyboardRemove()
    )

    task = asyncio.create_task(
        _job_runner(bot, user_id, bot_id, ub_name,
                    channel_id, channel_title,
                    order, scan_start, scan_end, status_msg)
    )
    _sessions[user_id] = task


async def _job_runner(bot, user_id, bot_id, ub_name,
                      channel_id, channel_title,
                      order, scan_start, scan_end, status_msg):
    ub = None
    try:
        ub = await _load_ub(user_id, bot_id)
        if not ub:
            _sessions.pop(user_id, None)
            return await _upd(status_msg,
                "<b>»  Failed to connect userbot!</b>\n\n"
                "Session expired? Go to Settings → Accounts to re-add.")

        await _upd(status_msg,
            f"<b>»  URL Bypass — Running</b>\n\n"
            f"»  Userbot: <b>{ub_name}</b>\n"
            f"»  Channel: <b>{channel_title}</b>\n\n"
            f"✅ Connected! Joining channel...")

        try:
            await ub.join_chat(channel_id)
        except Exception as e:
            s = str(e).lower()
            if 'already' not in s and 'participant' not in s:
                logger.warning(f"[Bypass] join warn: {e}")

        queue = await _scan(ub, channel_id, status_msg, order, scan_start, scan_end)

        if not queue:
            _sessions.pop(user_id, None)
            return await _upd(status_msg,
                f"<b>»  No Shortener Links Found!</b>\n\n"
                f"Channel <b>{channel_title}</b> has no posts with shortener buttons.")

        await _upd(status_msg,
            f"<b>»  URL Bypass — Queue Ready</b>\n\n"
            f"»  Channel: <b>{channel_title}</b>\n"
            f"»  Total links: <code>{len(queue)}</code>\n\n"
            f"⚡ Starting bypass process...")
        await asyncio.sleep(2)

        await _run_bypass(bot, user_id, ub, status_msg, queue)

    except asyncio.CancelledError:
        _sessions.pop(user_id, None)
        await _upd(status_msg, "<b>»  Bypass job cancelled.</b>")
    except Exception as e:
        _sessions.pop(user_id, None)
        logger.error(f"[Bypass] job error: {e}", exc_info=True)
        await _upd(status_msg, f"<b>»  Error:</b> <code>{str(e)[:200]}</code>")

# ── Stop ──────────────────────────────────────────────────────────────────────
@Client.on_message(filters.private & filters.command('bypass_stop'))
async def bypass_stop_cmd(bot, message):
    uid  = message.from_user.id
    task = _sessions.pop(uid, None)
    if task:
        task.cancel()
        await message.reply_text("<b>»  Bypass job stopping...</b>", parse_mode=PM)
    else:
        await message.reply_text("No bypass job running.")
