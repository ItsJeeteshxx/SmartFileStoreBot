"""URL Bypass Agentic System - Arya Forward Bot"""
from __future__ import annotations
import asyncio, re, time, logging, random, uuid
from typing import Optional
from pyrogram import Client, filters, enums, ContinuePropagation
from pyrogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from pyrogram.errors import FloodWait
from database import db
from plugins.owner_utils import require_feature
from bot import BOT_INSTANCE

logger = logging.getLogger(__name__)
PM = enums.ParseMode.HTML

BYPASS_BOT    = "Nick_Bypass_Bot"
BASE_IDLE_SEC = 15
MIN_DELAY     = 8    # anti-spam: min seconds between links
MAX_DELAY     = 18   # anti-spam: max seconds between links

UB_COLL = "url_bypass_jobs"

_ub_tasks: dict[str, asyncio.Task] = {}
_ub_paused: dict[str, asyncio.Event] = {}
_last_edits: dict[int, float] = {}

async def _save_bypass_job(job: dict):
    await db.db[UB_COLL].replace_one({"job_id": job["job_id"]}, job, upsert=True)

async def _get_bypass_job(jid: str):
    return await db.db[UB_COLL].find_one({"job_id": jid})

async def _get_all_bypass_jobs(uid: int):
    return [j async for j in db.db[UB_COLL].find({"user_id": uid})]

async def _delete_bypass_job(jid: str):
    await db.db[UB_COLL].delete_one({"job_id": jid})

async def _update_bypass_job(jid: str, kw: dict):
    await db.db[UB_COLL].update_one({"job_id": jid}, {"$set": kw})

# Skip these domains — they are NOT shorteners
SKIP_URL_RE = re.compile(
    r't\.me|telegram\.me|telegra\.ph|youtube\.com|youtu\.be|'  
    r'instagram\.com|facebook\.com|twitter\.com|x\.com|'         
    r'google\.com|drive\.google|docs\.google',
    re.IGNORECASE
)

_sessions: dict = {}
_waiting:  dict = {}

CANCEL_BTN = KeyboardButton("⛔ Cᴀɴᴄᴇʟ")
UNDO_BTN   = KeyboardButton("↩️ Uɴᴅᴏ")

def _is_cancel(t): return "⛔" in t or "cancel" in t.lower()
def _is_undo(t):   return "↩️" in t or "undo" in t.lower()


# ── Input router ──────────────────────────────────────────────────────────────
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


# Tracks current status message per user: user_id → (chat_id, msg_id)
_status_msgs: dict = {}

def _progress_bar(done: int, total: int, width: int = 10) -> str:
    pct = done / total if total else 0
    filled = round(width * pct)
    bar = '█' * filled + '░' * (width - filled)
    return f"[{bar}] {done}/{total} ({int(pct*100)}%)"

async def _upd(bot, user_id: int, chat_id: int, text: str):
    """Edit status message; fallback to send-new if edit fails. Rate-limited."""
    old = _status_msgs.get(user_id)
    if old:
        c_id, msg_id = old
        last_t = _last_edits.get(msg_id, 0)
        now_t = time.time()
        
        # Rate limit to avoid edit spam / FloodWait
        if now_t - last_t < 8:
            return 
            
        try:
            await bot.edit_message_text(
                c_id, msg_id, text,
                parse_mode=PM, disable_web_page_preview=True
            )
            _last_edits[msg_id] = now_t
            return  # edited successfully
        except FloodWait as fw:
            _last_edits[msg_id] = now_t + fw.value
            return
        except Exception:
            pass  # fall through → send new
    try:
        sent = await bot.send_message(
            chat_id, text, parse_mode=PM, disable_web_page_preview=True
        )
        _status_msgs[user_id] = (chat_id, sent.id)
        _last_edits[sent.id] = time.time()
    except Exception as e:
        logger.warning(f"[Bypass] _upd failed: {e}")


# ── Userbot loader ────────────────────────────────────────────────────────────
async def _load_ub(user_id: int, bot_id: str):
    """Start a fresh independent userbot client for bypass."""
    from config import Config
    bots     = await db.get_bots(user_id)
    userbots = [b for b in bots if not b.get('is_bot', True)]
    target   = next((b for b in userbots if str(b.get('id','')) == str(bot_id)), None)
    if not target:
        logger.error(f"[Bypass] Userbot {bot_id} not found")
        return None
    session = target.get('session') or target.get('session_string')
    if not session:
        logger.error(f"[Bypass] No session for userbot {bot_id}")
        return None
    try:
        from plugins.test import CLIENT, start_clone_bot
        _c_mgr = CLIENT()
        _fresh_ub = _c_mgr.client(target)
        ub = await start_clone_bot(_fresh_ub, data=target)
        logger.info(f"[Bypass] Userbot {bot_id} connected")
        return ub
    except Exception as e:
        logger.error(f"[Bypass] Userbot {bot_id} start failed: {e}")
        return None


# ── Helpers ───────────────────────────────────────────────────────────────────
def _get_links(message, allowed_buttons=None) -> list:
    """ALL (label, url) non-Telegram button URLs in order — agentic: no domain whitelist."""
    out = []
    if not message.reply_markup:
        return out
    idx = 1
    for row in getattr(message.reply_markup, 'inline_keyboard', []):
        for btn in row:
            url = getattr(btn, 'url', None) or ''
            # Skip empty, non-http, and known non-shortener domains
            if not url or not url.startswith('http'):
                continue
            if allowed_buttons and idx not in allowed_buttons:
                idx += 1
                continue
            if SKIP_URL_RE.search(url):
                if not re.search(r'(?:t\.me|telegram\.me)/\w+\?start=', url, re.IGNORECASE):
                    idx += 1
                    continue
            out.append(((btn.text or '').strip() or 'Link', url))
            idx += 1
    return out

def _parse_bypassed(text: str) -> Optional[str]:
    if not text: return None
    m = re.search(r'Bypassed Link[^:]*:[\s\u2705]*(\S+)', text, re.I)
    if m: return m.group(1).strip()
    m = re.search(r'(https?://(?:t\.me|telegram\.me)/\S+\?start=\S+)', text, re.I)
    if m: return m.group(1).strip()
    return None

def _parse_start(url: str) -> tuple:
    m = re.search(r'(?:t\.me|telegram\.me)/([^/?]+)\?start=(.+)', url, re.I)
    return (m.group(1).strip(), m.group(2).strip()) if m else (None, None)

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


# ── Channel scanner ───────────────────────────────────────────────────────────
async def _scan(bot, user_id, chat_id, ub, channel_id, order, start_id, end_id, allowed_buttons) -> list:
    all_links = []
    scanned   = 0
    await _upd(bot, user_id, chat_id, "<b>»  Scanning channel messages...</b>")
    try:
        async for msg in ub.get_chat_history(channel_id):
            mid = msg.id
            if end_id   and mid > end_id:   continue
            if start_id and mid < start_id: break
            scanned += 1
            links = _get_links(msg, allowed_buttons)
            if links:
                all_links.append((msg.id, links))
            if scanned % 100 == 0:
                await _upd(bot, user_id, chat_id,
                    f"<b>»  Scanning...</b>\n\n"
                    f"»  Scanned: <code>{scanned}</code> messages\n"
                    f"»  Posts with links: <code>{len(all_links)}</code>")
    except Exception as e:
        logger.error(f"[Bypass] scan error: {e}")
    if order == 'old_to_new':
        all_links.reverse()
    flat = []
    for (post_id, links) in all_links:
        for lbl, url in links:
            flat.append((post_id, lbl, url))
    return flat


# ── Bypass Jobs Command ───────────────────────────────────────────────────────
@Client.on_message(filters.private & filters.command(['bypass_jobs']))
@require_feature("url_bypass")
async def bypass_jobs_cmd(bot, message):
    uid = message.from_user.id
    jobs = await _get_all_bypass_jobs(uid)
    jobs = [j for j in jobs if j.get("status") not in ("completed", "stopped")]
    if not jobs:
        return await message.reply_text("ℹ️ No active bypass jobs.")
    for j in jobs:
        jid = j["job_id"]
        status = j.get("status", "running")
        ev = _ub_paused.get(jid)
        if status == "running" and ev and not ev.is_set():
            status = "paused"
            
        txt = (
            f"<b>\U0001f504 Bypass Job</b>\n"
            f"»  <b>Channel:</b> {j.get('channel_title', '?')}\n"
            f"»  <b>Progress:</b> {j.get('done', 0)} / {len(j.get('queue', []))}\n"
            f"»  <b>Status:</b> <code>{status.upper()}</code>\n"
        )
        kb = []
        if status == "running":
            kb.append([InlineKeyboardButton("⏸ Pause", callback_data=f"ub_pause:{jid}")])
        elif status == "paused":
            kb.append([InlineKeyboardButton("▶️ Resume", callback_data=f"ub_resume:{jid}")])
        kb.append([InlineKeyboardButton("🛑 Stop", callback_data=f"ub_stop:{jid}")])
        
        await message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=PM)

@Client.on_callback_query(filters.regex(r'^ub_(pause|resume|stop):(.+)$'))
async def bypass_action_cb(bot, query):
    action, jid = query.matches[0].groups()
    job = await _get_bypass_job(jid)
    if not job:
        return await query.answer("Job not found.", show_alert=True)
    if job["user_id"] != query.from_user.id:
        return await query.answer("Not your job.", show_alert=True)
        
    if action == "pause":
        if jid in _ub_paused: _ub_paused[jid].clear()
        await _update_bypass_job(jid, {"status": "paused"})
        await query.answer("Job paused.")
        try: await query.message.edit_text(query.message.text.replace("RUNNING", "PAUSED"), reply_markup=query.message.reply_markup)
        except: pass
        
    elif action == "resume":
        await _update_bypass_job(jid, {"status": "running"})
        if jid not in _ub_tasks:
            _ub_paused[jid] = asyncio.Event()
            _ub_paused[jid].set()
            _ub_tasks[jid] = asyncio.create_task(_ub_run_job(jid))
        else:
            if jid in _ub_paused: _ub_paused[jid].set()
        await query.answer("Job resumed.")
        try: await query.message.edit_text(query.message.text.replace("PAUSED", "RUNNING"), reply_markup=query.message.reply_markup)
        except: pass
        
    elif action == "stop":
        await _update_bypass_job(jid, {"status": "stopped"})
        if jid in _ub_tasks: _ub_tasks[jid].cancel()
        await query.answer("Job stopped.")
        try: await query.message.delete()
        except: pass


# ── Entry points ──────────────────────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r'^ub#bypass$'))
@require_feature("url_bypass")
async def bypass_cb(bot, query):
    await query.answer()
    user_id = query.from_user.id
    chat_id = query.message.chat.id
    try: await query.message.delete()
    except Exception: pass
    await _bypass_flow(bot, user_id, chat_id)

@Client.on_message(filters.private & filters.command('bypass'))
@require_feature("url_bypass")
async def bypass_cmd(bot, message):
    await _bypass_flow(bot, message.from_user.id, message.chat.id)


# ── Setup flow ────────────────────────────────────────────────────────────────
async def _bypass_flow(bot, user_id: int, chat_id: int):
    if user_id in _sessions:
        await bot.send_message(chat_id,
            "<b>»  A bypass job is already running!</b>\nSend /bypass_stop to cancel.",
            parse_mode=PM)
        return

    # Step 1: Userbot
    bots     = await db.get_bots(user_id)
    userbots = [b for b in bots if not b.get('is_bot', True)]
    if not userbots:
        await bot.send_message(chat_id,
            "<b>»  No Userbots Found!</b>\n\nAdd one:\nSettings → Accounts → Add Userbot",
            parse_mode=PM)
        return

    ub_btns = [[KeyboardButton(f"👤 {b.get('name','?')}  [{b.get('id','')}]")] for b in userbots]
    ub_btns.append([CANCEL_BTN])
    try:
        r1 = await _ask(bot, user_id,
            "<b>»  URL Bypass — Step 1/5</b>\n\n"
            "Select the <b>Userbot</b> to run this job:\n\n"
            "<blockquote>The userbot must be a member of the source channel.</blockquote>",
            reply_markup=ReplyKeyboardMarkup(ub_btns, resize_keyboard=True, one_time_keyboard=True))
    except asyncio.TimeoutError:
        return await bot.send_message(chat_id, "<i>Timed out.</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())
    if _is_cancel(r1.text):
        return await bot.send_message(chat_id, "<i>Cancelled!</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())

    bot_id = None
    if '[' in r1.text and ']' in r1.text:
        try: bot_id = r1.text.split('[')[-1].split(']')[0].strip()
        except Exception: pass
    if not bot_id: bot_id = str(userbots[0].get('id', ''))
    sel_ub  = next((b for b in userbots if str(b.get('id','')) == str(bot_id)), userbots[0])
    ub_name = sel_ub.get('name', f'Userbot {bot_id}')

    # Step 2: Channel
    try:
        r2 = await _ask(bot, user_id,
            "<b>»  URL Bypass — Step 2/5</b>\n\n"
            "Send the <b>Source Channel</b>:\n\n"
            "<blockquote expandable>"
            "• <code>https://t.me/channelname</code>\n"
            "• <code>https://t.me/c/1234567890/1</code>\n"
            "• <code>-1001234567890</code>\n"
            "• Forward any message from the channel\n\n"
            "Userbot joins automatically if not already a member."
            "</blockquote>",
            reply_markup=ReplyKeyboardMarkup([[UNDO_BTN, CANCEL_BTN]], resize_keyboard=True, one_time_keyboard=True))
    except asyncio.TimeoutError:
        return await bot.send_message(chat_id, "<i>Timed out.</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())
    if _is_cancel(r2.text):
        return await bot.send_message(chat_id, "<i>Cancelled!</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())

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

    # Step 3: Range
    try:
        r3 = await _ask(bot, user_id,
            f"<b>»  URL Bypass — Step 3/5</b>\n\n"
            f"Channel: <b>{channel_title}</b>\n\n"
            "Set <b>scan range</b>:\n\n"
            "<blockquote expandable>"
            "• <b>ALL</b> — scan entire channel\n"
            "• <code>100:500</code> — msg IDs 100 to 500 only\n"
            "• <code>100</code> — from msg ID 100 to end\n\n"
            "Open any message → Copy Post Link → get ID from URL."
            "</blockquote>",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("ALL")], [UNDO_BTN, CANCEL_BTN]],
                resize_keyboard=True, one_time_keyboard=True))
    except asyncio.TimeoutError:
        return await bot.send_message(chat_id, "<i>Timed out.</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())
    if _is_cancel(r3.text):
        return await bot.send_message(chat_id, "<i>Cancelled!</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())

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

    # Step 4: Buttons Selection
    try:
        r4 = await _ask(bot, user_id,
            "<b>»  URL Bypass — Step 4/6</b>\n\n"
            "Which <b>buttons</b> should be clicked in each post?\n\n"
            "<blockquote expandable>"
            "• <b>ALL</b> — Process all buttons\n"
            "• <code>1, 3, 5</code> — Process only specific buttons\n"
            "</blockquote>",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("ALL")], [UNDO_BTN, CANCEL_BTN]],
                resize_keyboard=True, one_time_keyboard=True))
    except asyncio.TimeoutError:
        return await bot.send_message(chat_id, "<i>Timed out.</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())
    if _is_cancel(r4.text):
        return await bot.send_message(chat_id, "<i>Cancelled!</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())

    allowed_buttons = None
    if r4.text.strip().lower() != 'all':
        allowed_buttons = []
        for x in r4.text.replace(',', ' ').split():
            try: allowed_buttons.append(int(x.strip()))
            except Exception: pass
        if not allowed_buttons: allowed_buttons = None

    # Step 5: Order
    try:
        r5 = await _ask(bot, user_id,
            "<b>»  URL Bypass — Step 5/6</b>\n\n"
            "Choose <b>processing order</b>:\n\n"
            "<blockquote>"
            "• <b>New → Old</b> — process latest posts first\n"
            "• <b>Old → New</b> — process oldest posts first"
            "</blockquote>",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("»  New → Old"), KeyboardButton("»  Old → New")],
                 [UNDO_BTN, CANCEL_BTN]],
                resize_keyboard=True, one_time_keyboard=True))
    except asyncio.TimeoutError:
        return await bot.send_message(chat_id, "<i>Timed out.</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())
    if _is_cancel(r5.text):
        return await bot.send_message(chat_id, "<i>Cancelled!</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())

    order       = 'old_to_new' if 'Old → New' in r5.text else 'new_to_old'
    order_label = '🕐 Old → New' if order == 'old_to_new' else '🕑 New → Old'

    # Step 6: Pacing Delay
    try:
        r6 = await _ask(bot, user_id,
            "<b>»  URL Bypass — Step 6/7</b>\n\n"
            "Set <b>Pacing Delay</b> for Live Job Synchronization:\n\n"
            "<blockquote>"
            "Target bots auto-delete files quickly. "
            "To give your Live Batch job time to forward the files, set a pacing delay "
            "between opening shortener links.\n\n"
            "• <code>0</code> — No extra delay\n"
            "• <code>2</code> — Wait 2 minutes\n"
            "• <code>4</code> — Wait 4 minutes"
            "</blockquote>",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("0"), KeyboardButton("2"), KeyboardButton("4"), KeyboardButton("5")],
                 [UNDO_BTN, CANCEL_BTN]],
                resize_keyboard=True, one_time_keyboard=True))
    except asyncio.TimeoutError:
        return await bot.send_message(chat_id, "<i>Timed out.</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())
    if _is_cancel(r6.text):
        return await bot.send_message(chat_id, "<i>Cancelled!</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())

    pacing_delay = 0
    try: pacing_delay = int(r6.text.strip())
    except Exception: pass

    # Step 7: Confirm
    range_label = f"<code>{scan_start}:{scan_end}</code>" if (scan_start or scan_end) else "ALL"
    btn_label = "ALL" if not allowed_buttons else ", ".join(map(str, allowed_buttons))
    try:
        r7 = await _ask(bot, user_id,
            f"<b>»  URL Bypass — Confirm</b>\n\n"
            f"»  Userbot: <b>{ub_name}</b>\n"
            f"»  Channel: <b>{channel_title}</b>\n"
            f"»  Range: {range_label}\n"
            f"»  Buttons: <b>{btn_label}</b>\n"
            f"»  Order: {order_label}\n"
            f"»  Pacing Delay: <b>{pacing_delay} min</b>\n\n"
            "<i>Tap Confirm to start.</i>",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("✅ Confirm & Start")], [CANCEL_BTN]],
                resize_keyboard=True, one_time_keyboard=True))
    except asyncio.TimeoutError:
        return await bot.send_message(chat_id, "<i>Timed out.</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())
    if _is_cancel(r7.text) or '✅' not in r7.text:
        return await bot.send_message(chat_id, "<i>Cancelled!</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())

    # Create job in DB
    job_id = str(uuid.uuid4())
    job = {
        "job_id": job_id, "user_id": user_id, "status": "running",
        "bot_id": bot_id, "ub_name": ub_name, "chat_id": chat_id,
        "channel_id": channel_id, "channel_title": channel_title,
        "order": order, "scan_start": scan_start, "scan_end": scan_end,
        "allowed_buttons": allowed_buttons, "pacing_delay": pacing_delay,
        "done": 0, "failed": [], "queue": []
    }
    await _save_bypass_job(job)
    _ub_paused[job_id] = asyncio.Event()
    _ub_paused[job_id].set()

    status_msg = await bot.send_message(
        chat_id,
        f"<b>»  URL Bypass — Starting</b>\n\n"
        f"»  Job ID: <code>{job_id[:8]}</code>\n"
        f"⏳ Connecting userbot...",
        parse_mode=PM, reply_markup=ReplyKeyboardRemove()
    )
    _status_msgs[user_id] = (chat_id, status_msg.id)
    
    task = asyncio.create_task(_ub_run_job(job_id))
    _ub_tasks[job_id] = task


# ── Job runner ────────────────────────────────────────────────────────────────
async def _ub_run_job(job_id: str):
    ub = None
    try:
        job = await _get_bypass_job(job_id)
        if not job or job.get("status") in ("stopped", "failed", "completed"):
            return
            
        user_id = job["user_id"]
        chat_id = job["chat_id"]
        
        logger.info(f"[Bypass] Loading userbot {job['bot_id']}...")
        ub = await _load_ub(user_id, job["bot_id"])
        if not ub:
            await _update_bypass_job(job_id, {"status": "failed", "error": "Userbot connect fail"})
            return await _upd(BOT_INSTANCE, user_id, chat_id, "<b>»  Failed to connect userbot!</b>")

        await _upd(BOT_INSTANCE, user_id, chat_id, "✅ Connected! Checking queue...")
        
        queue = job.get("queue", [])
        if not queue:
            # First run, need to scan
            channel_id = job["channel_id"]
            try:
                await asyncio.wait_for(ub.join_chat(channel_id), timeout=20)
            except: pass
            
            queue = await _scan(BOT_INSTANCE, user_id, chat_id, ub, channel_id, job["order"], job["scan_start"], job["scan_end"], job["allowed_buttons"])
            if not queue:
                await _update_bypass_job(job_id, {"status": "completed"})
                return await _upd(BOT_INSTANCE, user_id, chat_id, "<b>»  No Shortener Links Found!</b>")
            job["queue"] = queue
            await _update_bypass_job(job_id, {"queue": queue})
        
        total = len(queue)
        pacing = job.get("pacing_delay", 0)
        
        while True:
            # Check pause state
            ev = _ub_paused.get(job_id)
            if ev and not ev.is_set():
                await ev.wait()
                
            job = await _get_bypass_job(job_id)
            if not job or job.get("status") in ("stopped", "failed"):
                break
                
            done = job.get("done", 0)
            if done >= total:
                break
                
            failed = job.get("failed", [])
            post_id, label, short_url = queue[done]

            bar = _progress_bar(done, total)
            await _upd(BOT_INSTANCE, user_id, chat_id,
                f"\U0001f504 <b>URL Sʜᴏʀᴛᴇɴᴇʀ Bʏᴘᴀss</b>\n"
                f"<code>{bar}</code>\n\n"
                f"<b>\u00bb  Pᴏsᴛ ID  :</b> <code>{post_id}</code>\n"
                f"<b>\u00bb  Lɪɴᴋ    :</b> <code>{done+1} / {total}</code>\n"
                f"<b>\u00bb  Lᴀʙᴇʟ  :</b> <code>{label[:35]}</code>\n"
                f"<b>\u00bb  Sᴛᴇᴘ   :</b> Sending to bypass bot...\n\n"
                f"<i>\u26d4 /bypass_jobs to manage</i>"
            )

            bypassed = None
            bot_uname, param = _parse_start(short_url)
            if bot_uname and param:
                bypassed = short_url
            else:
                try:
                    await ub.send_message(BYPASS_BOT, short_url)
                except Exception as e:
                    failed.append((label, f"Send failed: {e}"))
                    done += 1
                    await _update_bypass_job(job_id, {"done": done, "failed": failed})
                    await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
                    continue
        
                t0 = time.time()
                for _ in range(60):
                    await asyncio.sleep(1)
                    if job_id not in _ub_tasks: break
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
                done += 1
                await _update_bypass_job(job_id, {"done": done, "failed": failed})
                await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
                continue

            bot_uname, param = _parse_start(bypassed)
            if not bot_uname:
                failed.append((label, "Not a ?start= link"))
                done += 1
                await _update_bypass_job(job_id, {"done": done, "failed": failed})
                await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
                continue

            await _upd(BOT_INSTANCE, user_id, chat_id,
                f"\U0001f504 <b>URL Sʜᴏʀᴛᴇɴᴇʀ Bʏᴘᴀss</b>\n"
                f"<code>{bar}</code>\n\n"
                f"<b>\u00bb  Lɪɴᴋ    :</b> <code>{done+1} / {total}</code>\n"
                f"<b>\u00bb  Sᴛᴇᴘ   :</b> Sending /start to @{bot_uname}...\n\n"
                f"<i>\u26d4 /bypass_jobs to manage</i>"
            )

            try:
                await ub.send_message(bot_uname, f"/start {param}")
            except Exception as e:
                failed.append((label, f"/start failed: {e}"))
                done += 1
                await _update_bypass_job(job_id, {"done": done, "failed": failed})
                await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
                continue

            wait_since = time.time()
            files = 0; last_file = time.time(); got_file = False
            adaptive = BASE_IDLE_SEC

            while True:
                await asyncio.sleep(2)
                if job_id not in _ub_tasks: break
                try:
                    nc = 0
                    async for m in ub.get_chat_history(bot_uname, limit=20):
                        ts = m.date.timestamp() if m.date else 0
                        if ts < wait_since: break
                        if m.media or m.document or m.video or m.audio or m.voice:
                            nc += 1
                            if ts > last_file: last_file = ts; got_file = True
                    
                    if nc > files:
                        files = nc
                        # Intelligent Timer Logic
                        adaptive = BASE_IDLE_SEC + int(files * 0.7) # Extra 0.7s per file
                        
                except Exception:
                    pass
                    
                idle   = time.time() - last_file
                waited = time.time() - wait_since
                if got_file and idle >= adaptive: break
                if not got_file and waited > 90:  break
                
                await _upd(BOT_INSTANCE, user_id, chat_id,
                    f"\U0001f504 <b>URL Sʜᴏʀᴛᴇɴᴇʀ Bʏᴘᴀss</b>\n"
                    f"<code>{_progress_bar(done, total)}</code>\n\n"
                    f"<b>\u00bb  Lɪɴᴋ      :</b> <code>{done+1} / {total}</code>\n"
                    f"<b>\u00bb  Fɪʟᴇs     :</b> <code>{files}</code> received\n"
                    f"<b>\u00bb  Iᴅʟᴇ Tɪᴍᴇ :</b> <code>{int(idle)}s / {adaptive}s</code>\n\n"
                    f"<i>\u26d4 /bypass_jobs to manage</i>"
                )

            done += 1
            await _update_bypass_job(job_id, {"done": done, "failed": failed})
            
            if pacing > 0 and done < total:
                wait_sec = pacing * 60
                await _upd(BOT_INSTANCE, user_id, chat_id,
                    f"⏳ <b>Pacing Delay:</b> Waiting {pacing} min for Live Job synchronization...\n"
                    f"<i>Job will resume automatically. /bypass_jobs to manage.</i>"
                )
                for _ in range(wait_sec):
                    if job_id not in _ub_tasks: break
                    ev = _ub_paused.get(job_id)
                    if ev and not ev.is_set(): await ev.wait()
                    await asyncio.sleep(1)
            else:
                delay = random.uniform(MIN_DELAY, MAX_DELAY)
                await asyncio.sleep(delay)

        # Loop finished
        job = await _get_bypass_job(job_id)
        if job and job.get("status") == "running":
            await _update_bypass_job(job_id, {"status": "completed"})
            fail_txt = ''
            if failed:
                lines = '\n'.join(f"  \u2022 {lb[:25]}: {rs[:40]}" for lb, rs in failed[:8])
                fail_txt = f"\n\n<b>\u00bb  Fᴀɪʟᴇᴅ ({len(failed)}):</b>\n{lines}"
            await _upd(BOT_INSTANCE, user_id, chat_id,
                f"\u2705 <b>URL Bʏᴘᴀss Cᴏᴍᴘʟᴇᴛᴇ!</b>\n"
                f"<code>{_progress_bar(total, total)}</code>\n\n"
                f"<b>\u00bb  Tᴏᴛᴀʟ   :</b> <code>{total}</code>\n"
                f"<b>\u00bb  Dᴏɴᴇ    :</b> <code>{done}</code>\n"
                f"<b>\u00bb  Fᴀɪʟᴇᴅ  :</b> <code>{len(failed)}</code>"
                f"{fail_txt}"
            )

    except asyncio.CancelledError:
        pass
    except Exception as e:
        await _update_bypass_job(job_id, {"status": "failed", "error": str(e)})
        logger.error(f"[Bypass] job error: {e}", exc_info=True)
    finally:
        _ub_tasks.pop(job_id, None)
        if ub:
            try:
                from plugins.test import release_client
                await release_client(ub.name)
            except: pass

# ── Stop ──────────────────────────────────────────────────────────────────────
@Client.on_message(filters.private & filters.command(['stopbypass', 'bypass_stop']))
async def bypass_stop_cmd(bot, message):
    await message.reply_text("Please use /bypass_jobs to manage and stop your jobs.", parse_mode=PM)
