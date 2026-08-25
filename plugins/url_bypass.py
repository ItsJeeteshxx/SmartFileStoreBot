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

DEFAULT_BYPASS_BOT = "Nick_Bypass_Bot"
BASE_IDLE_SEC      = 4     # Fast exit after files finish arriving (optimized from 15s)
MIN_DELAY          = 1     # Fast pacing between links (optimized from 8-18s)
MAX_DELAY          = 3

UB_COLL = "url_bypass_jobs"

_ub_tasks: dict[str, asyncio.Task] = {}
_ub_paused: dict[str, asyncio.Event] = {}
_last_edits: dict[int, float] = {}
_pending_edits: dict[str, asyncio.Task] = {}

_active_ubs: dict = {}
_ub_refcounts: dict = {}

async def _save_bypass_job(job: dict):
    await db.db[UB_COLL].replace_one({"job_id": job["job_id"]}, job, upsert=True)

async def _get_bypass_job(jid: str):
    return await db.db[UB_COLL].find_one({"job_id": jid})

async def _get_all_bypass_jobs(uid: int):
    return [j async for j in db.db[UB_COLL].find({"user_id": uid}).sort("created_at", -1)]

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
@Client.on_message(filters.private, group=-50)
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


# Tracks current status message per job: job_id → (chat_id, msg_id)
_status_msgs: dict = {}

def _progress_bar(done: int, total: int, width: int = 10) -> str:
    pct = done / total if total else 0
    filled = round(width * pct)
    bar = '█' * filled + '░' * (width - filled)
    return f"[{bar}] {done}/{total} ({int(pct*100)}%)"

async def _upd(bot, job_id: str, chat_id: int, text: str, force: bool = False):
    """Edit status message strictly; edit the exact same message repeatedly to prevent chat spam."""
    global _status_msgs
    old = _status_msgs.get(job_id)
    
    if not old and job_id:
        try:
            job = await _get_bypass_job(job_id)
            if job and job.get("status_msg_id"):
                old = (chat_id, job["status_msg_id"])
                _status_msgs[job_id] = old
        except Exception: pass

    if old:
        c_id, msg_id = old
        last_t = _last_edits.get(msg_id, 0)
        now_t = time.time()
        
        # Throttled rate limit (1.2s to prevent Telegram FloodWait on edit_message_text)
        if not force and (now_t - last_t < 1.2):
            if job_id not in _pending_edits or _pending_edits[job_id].done():
                async def _delayed_edit():
                    await asyncio.sleep(1.2)
                    await _upd(bot, job_id, chat_id, text, force=True)
                _pending_edits[job_id] = asyncio.create_task(_delayed_edit())
            return
            
        try:
            await bot.edit_message_text(
                c_id, msg_id, text,
                parse_mode=PM, disable_web_page_preview=True
            )
            _last_edits[msg_id] = time.time()
            return  # edited successfully
        except FloodWait as fw:
            _last_edits[msg_id] = time.time() + fw.value
            return
        except Exception:
            pass  # fall through → send new status msg if previous was deleted
            
    try:
        sent = await bot.send_message(
            chat_id, text, parse_mode=PM, disable_web_page_preview=True
        )
        _status_msgs[job_id] = (chat_id, sent.id)
        _last_edits[sent.id] = time.time()
        await _update_bypass_job(job_id, {"status_msg_id": sent.id})
    except Exception as e:
        logger.warning(f"[Bypass] _upd failed: {e}")


# ── Userbot loader ────────────────────────────────────────────────────────────
async def _load_ub(user_id: int, bot_id: str):
    """Start a fresh independent userbot client for bypass, or reuse active."""
    bot_id = str(bot_id)
    if bot_id in _active_ubs and _active_ubs[bot_id].is_connected:
        _ub_refcounts[bot_id] = _ub_refcounts.get(bot_id, 0) + 1
        logger.info(f"[Bypass] Reusing Userbot {bot_id} (Refs: {_ub_refcounts[bot_id]})")
        return _active_ubs[bot_id]
        
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
        
        _active_ubs[bot_id] = ub
        _ub_refcounts[bot_id] = 1
        logger.info(f"[Bypass] Userbot {bot_id} connected (Refs: 1)")
        return ub
    except Exception as e:
        logger.error(f"[Bypass] Userbot {bot_id} start failed: {e}")
        return None


# ── Helpers ───────────────────────────────────────────────────────────────────
def _get_links(message, allowed_buttons=None) -> list:
    """ALL unique (label, url) non-Telegram button and text/caption URLs in order."""
    out = []
    seen_urls = set()
    
    # 1. Inline buttons links
    if message.reply_markup:
        idx = 1
        for row in getattr(message.reply_markup, 'inline_keyboard', []):
            for btn in row:
                url = getattr(btn, 'url', None) or ''
                if not url or not url.startswith('http'):
                    continue
                if allowed_buttons and idx not in allowed_buttons:
                    idx += 1
                    continue
                if SKIP_URL_RE.search(url):
                    if not re.search(r'(?:t\.me|telegram\.me)/\w+\?start=', url, re.IGNORECASE):
                        idx += 1
                        continue
                if url not in seen_urls:
                    seen_urls.add(url)
                    out.append(((btn.text or '').strip() or 'Link', url))
                idx += 1

    # 2. Text / Caption links (Entities & Regex fallback)
    text = message.text or message.caption or ""
    if text:
        # Extract from entities first (robust and Telegram-native)
        entities = getattr(message, 'entities', None) or getattr(message, 'caption_entities', None)
        if entities:
            for ent in entities:
                url = None
                ent_type = getattr(ent, 'type', None)
                if ent_type in (enums.MessageEntityType.TEXT_LINK, "text_link"):
                    url = getattr(ent, 'url', None)
                elif ent_type in (enums.MessageEntityType.URL, "url"):
                    offset = getattr(ent, 'offset', 0)
                    length = getattr(ent, 'length', 0)
                    url = text[offset : offset + length]
                
                if url:
                    url = url.strip()
                    if not url.startswith('http'):
                        continue
                    if SKIP_URL_RE.search(url):
                        if not re.search(r'(?:t\.me|telegram\.me)/\w+\?start=', url, re.IGNORECASE):
                            continue
                    if url not in seen_urls:
                        seen_urls.add(url)
                        out.append(('Link', url))

        # Regex fallback to match raw text URLs that weren't parsed as entities
        urls = re.findall(r'https?://\S+', text)
        for url in urls:
            url = url.strip().rstrip('.,;:!?)"\'}]')
            if not url or not url.startswith('http'):
                continue
            if SKIP_URL_RE.search(url):
                if not re.search(r'(?:t\.me|telegram\.me)/\w+\?start=', url, re.IGNORECASE):
                    continue
            if url not in seen_urls:
                seen_urls.add(url)
                out.append(('Link', url))
                
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
async def _scan(bot, job_id, chat_id, ub, channel_id, order, start_id, end_id, allowed_buttons) -> list:
    all_links = []
    scanned   = 0
    await _upd(bot, job_id, chat_id, "<b>»  Scanning channel messages...</b>")
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
                await _upd(bot, job_id, chat_id,
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
def _ub_emoji(status: str) -> str:
    return {
        "running": "🟢",
        "paused": "⏸",
        "stopped": "🔴",
        "completed": "✅",
        "failed": "❌"
    }.get(status, "⭘")

async def _send_bypass_menu(bot, uid, chat_id, mid=None):
    jobs = await _get_all_bypass_jobs(uid)
    active_bot = await db.get_bypass_bot(uid)
    
    if not jobs:
        txt = (
            "ℹ️ <b>No URL Bypass jobs found.</b>\n\n"
            f"🤖 <b>Active Bypass Bot:</b> <code>@{active_bot}</code>\n\n"
            "Use the buttons below to start a job or configure your bypass bot."
        )
        kb = [
            [InlineKeyboardButton("⚙️ Settings", callback_data="ub_settings")],
            [InlineKeyboardButton("➕ Start New Bypass Job", callback_data="ub#new")],
            [InlineKeyboardButton("❮ Bᴀᴄᴋ", callback_data="back")]
        ]
        if mid:
            try: return await bot.edit_message_text(chat_id, mid, txt, reply_markup=InlineKeyboardMarkup(kb))
            except: pass
        return await bot.send_message(chat_id, txt, reply_markup=InlineKeyboardMarkup(kb))
        
    txt = (
        f"<b>🔗 URL Bypass Jobs Menu</b>\n"
        f"🤖 <b>Active Bypass Bot:</b> <code>@{active_bot}</code>\n"
        f"<i>You have {len(jobs)} total jobs.</i>\n\n"
    )
    kb = []
    
    for i, j in enumerate(jobs, 1):
        jid = j["job_id"]
        status = j.get("status", "running")
        ev = _ub_paused.get(jid)
        if status == "running" and ev and not ev.is_set():
            status = "paused"
            
        done = j.get('done', 0)
        total = len(j.get('queue', []))
        pct = int(done / total * 100) if total else 0
        
        status_emoji = _ub_emoji(status)
        txt += f"<b>{i}. {j.get('channel_title', '?')}</b>\n"
        txt += f"   Status: {status_emoji} <code>{status.upper()}</code>\n"
        txt += f"   Progress: <code>{_progress_bar(done, total, width=8)}</code>\n\n"
        
        row = []
        if status == "running":
            row.append(InlineKeyboardButton(f"⏸ Pause [{i}]", callback_data=f"ub_pause:{jid}"))
            row.append(InlineKeyboardButton(f"🛑 Stop [{i}]", callback_data=f"ub_stop:{jid}"))
        elif status == "paused":
            row.append(InlineKeyboardButton(f"▶️ Resume [{i}]", callback_data=f"ub_resume:{jid}"))
            row.append(InlineKeyboardButton(f"🛑 Stop [{i}]", callback_data=f"ub_stop:{jid}"))
        else:
            row.append(InlineKeyboardButton(f"▶️ Start [{i}]", callback_data=f"ub_resume:{jid}"))
            row.append(InlineKeyboardButton(f"🔁 Reset [{i}]", callback_data=f"ub_reset:{jid}"))
            
        row.append(InlineKeyboardButton(f"🗑 Delete [{i}]", callback_data=f"ub_del:{jid}"))
        kb.append(row)
        
    kb.append([
        InlineKeyboardButton("⚙️ Settings", callback_data="ub_settings"),
        InlineKeyboardButton("🔄 Refresh", callback_data="ub_menu_refresh")
    ])
    kb.append([
        InlineKeyboardButton("➕ Start New Job", callback_data="ub#new"),
        InlineKeyboardButton("❮ Bᴀᴄᴋ", callback_data="back")
    ])
    
    if mid:
        try: return await bot.edit_message_text(chat_id, mid, txt, reply_markup=InlineKeyboardMarkup(kb))
        except: pass
    return await bot.send_message(chat_id, txt, reply_markup=InlineKeyboardMarkup(kb))


@Client.on_message(filters.private & filters.command(['bypass', 'bypass_menu']))
@require_feature("url_bypass")
async def bypass_jobs_cmd(bot, message):
    await _send_bypass_menu(bot, message.from_user.id, message.chat.id)

@Client.on_callback_query(filters.regex(r'^ub_menu_refresh$'))
async def bypass_menu_refresh_cb(bot, query):
    await query.answer()
    await _send_bypass_menu(bot, query.from_user.id, query.message.chat.id, query.message.id)

@Client.on_callback_query(filters.regex(r'^ub_(?:bot_)?settings$'))
async def bypass_bot_settings_cb(bot, query):
    await query.answer()
    uid = query.from_user.id
    active_bot = await db.get_bypass_bot(uid)
    txt = (
        "<b>⚙️ URL Bypass Settings</b>\n\n"
        f"Active Bypass Bot: <b>@{active_bot}</b>\n\n"
        "<i>Select a preset bypass bot or enter a custom bot username. "
        "Your Userbot will send shortener URLs to this bot for bypassing.</i>"
    )
    kb = [
        [InlineKeyboardButton("🤖 Default (@Nick_Bypass_Bot)", callback_data="ub_set_bot:Nick_Bypass_Bot")],
        [InlineKeyboardButton("✏️ Custom Bypass Bot", callback_data="ub_set_bot_custom")],
        [InlineKeyboardButton("❮ Bᴀᴄᴋ", callback_data="ub_menu_refresh")]
    ]
    try:
        await bot.edit_message_text(query.message.chat.id, query.message.id, txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=PM)
    except Exception:
        await bot.send_message(query.message.chat.id, txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=PM)

@Client.on_callback_query(filters.regex(r'^ub_set_bot:(.+)$'))
async def bypass_set_bot_cb(bot, query):
    bot_name = query.matches[0].group(1).strip().lstrip('@')
    await db.set_bypass_bot(query.from_user.id, bot_name)
    await query.answer(f"✅ Active Bypass Bot set to @{bot_name}!", show_alert=True)
    await _send_bypass_menu(bot, query.from_user.id, query.message.chat.id, query.message.id)

@Client.on_callback_query(filters.regex(r'^ub_set_bot_custom$'))
async def bypass_set_bot_custom_cb(bot, query):
    await query.answer()
    uid = query.from_user.id
    chat_id = query.message.chat.id
    try: await query.message.delete()
    except Exception: pass

    try:
        res = await _ask(bot, uid,
            "<b>» URL Bypass — Custom Bot Username</b>\n\n"
            "Send the <b>@username</b> of your target bypass bot:\n\n"
            "<blockquote>Examples: <code>@Nick_Bypass_Bot</code>, <code>@MyBypassBot</code></blockquote>",
            reply_markup=ReplyKeyboardMarkup([[CANCEL_BTN]], resize_keyboard=True, one_time_keyboard=True))
    except asyncio.TimeoutError:
        return await bot.send_message(chat_id, "<i>Timed out.</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())
    if _is_cancel(res.text):
        return await bot.send_message(chat_id, "<i>Cancelled!</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())

    raw_text = res.text.strip()
    m = re.search(r'(?:t\.me/|@)?([a-zA-Z0-9_]{3,})', raw_text)
    custom_uname = m.group(1).strip() if m else raw_text.lstrip('@').strip()
    
    if not custom_uname:
        return await bot.send_message(chat_id, "<b>Invalid username. Please send a valid @bot_username.</b>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())

    await db.set_bypass_bot(uid, custom_uname)
    await bot.send_message(chat_id, f"✅ <b>Bypass Bot successfully updated and saved as @{custom_uname}!</b>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())
    await _send_bypass_menu(bot, uid, chat_id)

@Client.on_callback_query(filters.regex(r'^ub_(pause|resume|stop|reset|del):(.+)$'))
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
        
    elif action == "resume":
        await _update_bypass_job(jid, {"status": "running"})
        if jid not in _ub_tasks:
            _ub_paused[jid] = asyncio.Event()
            _ub_paused[jid].set()
            _ub_tasks[jid] = asyncio.create_task(_ub_run_job(jid))
        else:
            if jid in _ub_paused: _ub_paused[jid].set()
        await query.answer("Job resumed.")
        
    elif action == "stop":
        await _update_bypass_job(jid, {"status": "stopped"})
        if jid in _ub_tasks:
            _ub_tasks[jid].cancel()
            _ub_tasks.pop(jid, None)
        await query.answer("Job stopped.")

    elif action == "reset":
        await _update_bypass_job(jid, {"status": "running", "done": 0, "failed": [], "queue": []})
        if jid in _ub_tasks:
            _ub_tasks[jid].cancel()
            _ub_tasks.pop(jid, None)
        _ub_paused[jid] = asyncio.Event()
        _ub_paused[jid].set()
        _ub_tasks[jid] = asyncio.create_task(_ub_run_job(jid))
        await query.answer("Job reset and restarted.")

    elif action == "del":
        if jid in _ub_tasks:
            _ub_tasks[jid].cancel()
            _ub_tasks.pop(jid, None)
        if jid in _ub_paused:
            _ub_paused.pop(jid, None)
        await _delete_bypass_job(jid)
        await query.answer("Job deleted.")
        
    await _send_bypass_menu(bot, query.from_user.id, query.message.chat.id, query.message.id)


# ── Entry points ──────────────────────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r'^ub#bypass$'))
@require_feature("url_bypass")
async def bypass_cb(bot, query):
    await query.answer()
    await _send_bypass_menu(bot, query.from_user.id, query.message.chat.id, query.message.id)

@Client.on_callback_query(filters.regex(r'^ub#new$'))
@require_feature("url_bypass")
async def bypass_new_cb(bot, query):
    await query.answer()
    user_id = query.from_user.id
    chat_id = query.message.chat.id
    try: await query.message.delete()
    except Exception: pass
    await _bypass_flow(bot, user_id, chat_id)


# ── Setup flow ────────────────────────────────────────────────────────────────
async def _bypass_flow(bot, user_id: int, chat_id: int):
    if user_id in _sessions:
        await bot.send_message(chat_id,
            "<b>»  A bypass job is already running!</b>\nSend /bypass_stop to cancel.",
            parse_mode=PM)
        return

    # Step 1: Userbot Selection
    bots     = await db.get_bots(user_id)
    userbots = [b for b in bots if not b.get('is_bot', True)]
    if not userbots:
        await bot.send_message(chat_id,
            "<b>»  No Userbots Found!</b>\n\nAdd one:\nSettings → Accounts → Add Userbot",
            parse_mode=PM)
        return

    ub_btns = []
    if len(userbots) > 1:
        ub_btns.append([KeyboardButton("👥 All Available Userbots (Auto-Rotation)")])
    for b in userbots:
        ub_btns.append([KeyboardButton(f"👤 {b.get('name','?')}  [{b.get('id','')}]")])
    ub_btns.append([CANCEL_BTN])

    try:
        r1 = await _ask(bot, user_id,
            "<b>»  URL Bypass — Step 1/8 (Userbot)</b>\n\n"
            "Select the <b>Userbot(s)</b> to run this job:\n\n"
            "<blockquote expandable>"
            "• <b>👥 All Available Userbots</b> — Automatically rotates between your userbots (e.g. 3 files per account per hour) for maximum speed with 0 rate limit bans!\n"
            "• <b>👤 Single Userbot</b> — Runs only on the selected account."
            "</blockquote>",
            reply_markup=ReplyKeyboardMarkup(ub_btns, resize_keyboard=True, one_time_keyboard=True))
    except asyncio.TimeoutError:
        return await bot.send_message(chat_id, "<i>Timed out.</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())
    if _is_cancel(r1.text):
        return await bot.send_message(chat_id, "<i>Cancelled!</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())

    bot_ids = []
    if "all available userbots" in r1.text.lower():
        bot_ids = [str(b['id']) for b in userbots]
        ub_name = f"All ({len(userbots)} Userbots Auto-Rotation)"
    else:
        bot_id = None
        if '[' in r1.text and ']' in r1.text:
            try: bot_id = r1.text.split('[')[-1].split(']')[0].strip()
            except Exception: pass
        if not bot_id: bot_id = str(userbots[0].get('id', ''))
        sel_ub  = next((b for b in userbots if str(b.get('id','')) == str(bot_id)), userbots[0])
        bot_ids = [str(bot_id)]
        ub_name = sel_ub.get('name', f'Userbot {bot_id}')

    # Step 2: Source Channel
    try:
        r2 = await _ask(bot, user_id,
            "<b>»  URL Bypass — Step 2/8 (Source Channel)</b>\n\n"
            "Send the <b>Source Channel</b> (where posts and inline button links are):\n\n"
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

    # Step 3: Target / Destination Channel
    try:
        r3_dest = await _ask(bot, user_id,
            "<b>»  URL Bypass — Step 3/8 (Destination Forwarding)</b>\n\n"
            "Send the <b>Target / Destination Channel</b> where the original story post (Image + Caption) and downloaded video files should be forwarded:\n\n"
            "<blockquote expandable>"
            "• <code>https://t.me/targetchannel</code>\n"
            "• <code>-1001234567890</code>\n"
            "• Forward any message from the target channel\n\n"
            "<i>Tap <b>⏩ SKIP</b> if you only want Userbot to receive files in private DM without forwarding to a channel.</i>"
            "</blockquote>",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("⏩ SKIP (Userbot DM Only)")], [UNDO_BTN, CANCEL_BTN]], resize_keyboard=True, one_time_keyboard=True))
    except asyncio.TimeoutError:
        return await bot.send_message(chat_id, "<i>Timed out.</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())
    if _is_cancel(r3_dest.text):
        return await bot.send_message(chat_id, "<i>Cancelled!</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())

    target_channel_id = None
    target_channel_title = "None (DM Only)"
    if "skip" not in r3_dest.text.lower():
        fwd_dest = getattr(r3_dest, 'forward_from_chat', None)
        target_channel_id, target_channel_title = _resolve_channel(r3_dest.text, fwd_dest)
        if target_channel_id:
            try:
                t_ci = await bot.get_chat(target_channel_id)
                target_channel_title = t_ci.title or target_channel_title
                target_channel_id = t_ci.id
            except Exception: pass

    # Step 4: Range
    try:
        r4 = await _ask(bot, user_id,
            f"<b>»  URL Bypass — Step 4/8 (Range)</b>\n\n"
            f"Source Channel: <b>{channel_title}</b>\n\n"
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
    if _is_cancel(r4.text):
        return await bot.send_message(chat_id, "<i>Cancelled!</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())

    scan_start = 0; scan_end = 0
    rt = r4.text.strip()
    if rt.lower() != 'all':
        if ':' in rt:
            try: scan_start = int(rt.split(':')[0].strip())
            except Exception: pass
            try: scan_end   = int(rt.split(':')[1].strip())
            except Exception: pass
        else:
            try: scan_start = int(rt)
            except Exception: pass

    # Step 5: Buttons Selection
    try:
        r5 = await _ask(bot, user_id,
            "<b>»  URL Bypass — Step 5/8 (Buttons)</b>\n\n"
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
    if _is_cancel(r5.text):
        return await bot.send_message(chat_id, "<i>Cancelled!</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())

    allowed_buttons = None
    if r5.text.strip().lower() != 'all':
        allowed_buttons = []
        for x in r5.text.replace(',', ' ').split():
            try: allowed_buttons.append(int(x.strip()))
            except Exception: pass
        if not allowed_buttons: allowed_buttons = None

    # Step 6: Order
    try:
        r6 = await _ask(bot, user_id,
            "<b>»  URL Bypass — Step 6/8 (Order)</b>\n\n"
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
    if _is_cancel(r6.text):
        return await bot.send_message(chat_id, "<i>Cancelled!</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())

    order       = 'old_to_new' if 'Old → New' in r6.text else 'new_to_old'
    order_label = '🕐 Old → New' if order == 'old_to_new' else '🕑 New → Old'

    # Step 7: Hourly Rate Limit Quota
    try:
        r7 = await _ask(bot, user_id,
            "<b>»  URL Bypass — Step 7/8 (Hourly Rate Limit)</b>\n\n"
            "Set the <b>Hourly Download Quota per Userbot</b>:\n\n"
            "<blockquote>"
            "• <b>⚡ 3 Files / Hour (Recommended)</b> — Safely avoids target bot bans. Each Userbot fetches 3 files, then automatically rotates to the next Userbot or sleeps until the 1-hour cooldown resets.\n"
            "• <b>5 Files / Hour</b> — 5 files per hour per Userbot.\n"
            "• <b>⚡ Unlimited (No Quota)</b> — Continuous downloading without hourly limits."
            "</blockquote>",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("⚡ 3 Files / Hour (Recommended)")],
                 [KeyboardButton("5 Files / Hour"), KeyboardButton("⚡ Unlimited (No Quota)")],
                 [UNDO_BTN, CANCEL_BTN]],
                resize_keyboard=True, one_time_keyboard=True))
    except asyncio.TimeoutError:
        return await bot.send_message(chat_id, "<i>Timed out.</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())
    if _is_cancel(r7.text):
        return await bot.send_message(chat_id, "<i>Cancelled!</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())

    hourly_quota = 3
    if "5" in r7.text:
        hourly_quota = 5
    elif "unlimited" in r7.text.lower():
        hourly_quota = 0

    # Step 8: Pacing Delay
    try:
        r8 = await _ask(bot, user_id,
            "<b>»  URL Bypass — Step 8/8 (Pacing Delay)</b>\n\n"
            "Set <b>Pacing Delay</b> between individual links:\n\n"
            "<blockquote>"
            "• <code>0</code> — No extra delay (Fastest)\n"
            "• <code>1</code> — Wait 1 minute\n"
            "• <code>2</code> — Wait 2 minutes"
            "</blockquote>",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("0"), KeyboardButton("1"), KeyboardButton("2")],
                 [UNDO_BTN, CANCEL_BTN]],
                resize_keyboard=True, one_time_keyboard=True))
    except asyncio.TimeoutError:
        return await bot.send_message(chat_id, "<i>Timed out.</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())
    if _is_cancel(r8.text):
        return await bot.send_message(chat_id, "<i>Cancelled!</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())

    pacing_delay = 0
    try: pacing_delay = int(r8.text.strip())
    except Exception: pass

    # Confirm
    range_label = f"<code>{scan_start}:{scan_end}</code>" if (scan_start or scan_end) else "ALL"
    btn_label = "ALL" if not allowed_buttons else ", ".join(map(str, allowed_buttons))
    quota_label = f"{hourly_quota} files/hr per Userbot" if hourly_quota > 0 else "Unlimited"

    try:
        r_conf = await _ask(bot, user_id,
            f"<b>»  URL Bypass — Confirmation</b>\n\n"
            f"<b>»  Userbots:</b> {ub_name}\n"
            f"<b>»  Source Channel:</b> {channel_title}\n"
            f"<b>»  Target Channel:</b> {target_channel_title}\n"
            f"<b>»  Scan Range:</b> {range_label}\n"
            f"<b>»  Buttons:</b> {btn_label}\n"
            f"<b>»  Order:</b> {order_label}\n"
            f"<b>»  Hourly Quota:</b> <code>{quota_label}</code>\n"
            f"<b>»  Pacing Delay:</b> <code>{pacing_delay} min</code>\n\n"
            "<i>Tap Confirm to start scraping & auto-forwarding!</i>",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("✅ Confirm & Start")], [CANCEL_BTN]],
                resize_keyboard=True, one_time_keyboard=True))
    except asyncio.TimeoutError:
        return await bot.send_message(chat_id, "<i>Timed out.</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())
    if _is_cancel(r_conf.text) or '✅' not in r_conf.text:
        return await bot.send_message(chat_id, "<i>Cancelled!</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())

    # Create job in DB
    bypass_bot = await db.get_bypass_bot(user_id)
    job_id = str(uuid.uuid4())
    job = {
        "job_id": job_id, "user_id": user_id, "status": "running",
        "bot_ids": bot_ids, "bot_id": bot_ids[0] if bot_ids else "",
        "ub_name": ub_name, "chat_id": chat_id,
        "channel_id": channel_id, "channel_title": channel_title,
        "target_channel_id": target_channel_id, "target_channel_title": target_channel_title,
        "order": order, "scan_start": scan_start, "scan_end": scan_end,
        "allowed_buttons": allowed_buttons, "hourly_quota": hourly_quota,
        "pacing_delay": pacing_delay, "bypass_bot": bypass_bot,
        "done": 0, "failed": [], "queue": [], "created_at": time.time()
    }
    await _save_bypass_job(job)
    _ub_paused[job_id] = asyncio.Event()
    _ub_paused[job_id].set()

    status_msg = await bot.send_message(
        chat_id,
        f"<b>»  URL Bypass — Starting</b>\n\n"
        f"»  Job ID: <code>{job_id[:8]}</code>\n"
        f"»  Userbots: <b>{ub_name}</b>\n"
        f"»  Bypass Bot: <b>@{bypass_bot}</b>\n"
        f"⏳ Connecting userbot pool...",
        parse_mode=PM, reply_markup=ReplyKeyboardRemove()
    )
    _status_msgs[job_id] = (chat_id, status_msg.id)
    job["status_msg_id"] = status_msg.id
    await _save_bypass_job(job)
    
    task = asyncio.create_task(_ub_run_job(job_id))
    _ub_tasks[job_id] = task


# ── Job runner ────────────────────────────────────────────────────────────────
async def _ub_run_job(job_id: str):
    ub_pool: dict[str, Client] = {}
    try:
        job = await _get_bypass_job(job_id)
        if not job or job.get("status") in ("stopped", "failed", "completed"):
            return
            
        user_id = job["user_id"]
        chat_id = job["chat_id"]
        bypass_bot_uname = job.get("bypass_bot") or await db.get_bypass_bot(user_id)
        bypass_bot_uname = bypass_bot_uname.strip().lstrip('@')
        
        bot_ids = job.get("bot_ids") or ([str(job.get("bot_id"))] if job.get("bot_id") else [])
        if not bot_ids:
            bots = await db.get_bots(user_id)
            userbots = [b for b in bots if not b.get('is_bot', True)]
            bot_ids = [str(b['id']) for b in userbots]

        logger.info(f"[Bypass] Loading userbot pool {bot_ids} for job {job_id[:8]}...")
        for b_id in bot_ids:
            u_cli = await _load_ub(user_id, b_id)
            if u_cli:
                ub_pool[str(b_id)] = u_cli

        if not ub_pool:
            await _update_bypass_job(job_id, {"status": "failed", "error": "Userbot connect fail"})
            return await _upd(BOT_INSTANCE, job_id, chat_id, "<b>»  Failed to connect userbot(s)!</b>")

        bot_id_list = list(ub_pool.keys())
        first_ub = ub_pool[bot_id_list[0]]
        await _upd(BOT_INSTANCE, job_id, chat_id, f"✅ Connected {len(ub_pool)} Userbot(s)! Setting up queue...")
        
        channel_id = job["channel_id"]
        target_channel_id = job.get("target_channel_id")

        # Ensure userbots join source channel
        for b_id, u_cli in ub_pool.items():
            try:
                await asyncio.wait_for(u_cli.join_chat(channel_id), timeout=15)
            except Exception: pass
            if target_channel_id:
                try:
                    await asyncio.wait_for(u_cli.join_chat(target_channel_id), timeout=15)
                except Exception: pass

        queue = job.get("queue", [])
        if not queue:
            queue = await _scan(BOT_INSTANCE, job_id, chat_id, first_ub, channel_id, job["order"], job["scan_start"], job["scan_end"], job["allowed_buttons"])
            if not queue:
                await _update_bypass_job(job_id, {"status": "completed"})
                return await _upd(BOT_INSTANCE, job_id, chat_id, "<b>»  No Shortener / Deep Links Found!</b>")
            job["queue"] = queue
            await _update_bypass_job(job_id, {"queue": queue})
        
        total = len(queue)
        pacing = job.get("pacing_delay", 0)
        hourly_quota = job.get("hourly_quota", 3)

        # Quota Tracking: b_id -> list of successful processing timestamps in epoch seconds
        ub_history: dict[str, list[float]] = {b_id: [] for b_id in bot_id_list}
        current_ub_idx = 0

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

            # ── Multi-Userbot Hourly Quota Controller ──
            selected_b_id = None
            if hourly_quota > 0:
                while not selected_b_id:
                    now = time.time()
                    # Clean up timestamps older than 1 hour (3600 seconds)
                    for b in bot_id_list:
                        ub_history[b] = [t for t in ub_history[b] if now - t < 3600]

                    # Check which userbot has available quota
                    for i in range(len(bot_id_list)):
                        cand_idx = (current_ub_idx + i) % len(bot_id_list)
                        cand_b_id = bot_id_list[cand_idx]
                        if len(ub_history[cand_b_id]) < hourly_quota:
                            selected_b_id = cand_b_id
                            current_ub_idx = cand_idx
                            break

                    if not selected_b_id:
                        # All userbots have exhausted their quota for the current 60-minute window
                        # Find earliest timestamp when a slot frees up
                        all_active_ts = [ub_history[b][0] for b in bot_id_list if ub_history[b]]
                        earliest_t = min(all_active_ts) if all_active_ts else now
                        wait_seconds = max(1, int(3600 - (now - earliest_t)) + 3)
                        
                        logger.info(f"[Bypass] All userbots reached {hourly_quota} quota. Sleeping for {wait_seconds}s...")
                        for w_sec in range(wait_seconds):
                            if job_id not in _ub_tasks: break
                            ev = _ub_paused.get(job_id)
                            if ev and not ev.is_set(): await ev.wait()
                            job = await _get_bypass_job(job_id)
                            if not job or job.get("status") in ("stopped", "failed"): break

                            if w_sec % 10 == 0 or w_sec == 0:
                                rem_m = (wait_seconds - w_sec) // 60
                                rem_s = (wait_seconds - w_sec) % 60
                                ub_status_str = " | ".join(f"UB {b[:6]}: {len(ub_history[b])}/{hourly_quota}" for b in bot_id_list)
                                await _upd(BOT_INSTANCE, job_id, chat_id,
                                    f"⏳ <b>Hourly Rate Limit Safety Cooldown</b>\n"
                                    f"<code>{_progress_bar(done, total)}</code>\n\n"
                                    f"<b>»  Quota Status :</b> <code>{ub_status_str}</code>\n"
                                    f"<b>»  Next Batch   :</b> <code>{done+1} - {min(done+hourly_quota, total)}</code>\n"
                                    f"<b>»  Auto-Resumes :</b> in <code>{rem_m:02d}m {rem_s:02d}s</code>\n"
                                    f"<b>»  Total Done   :</b> <code>{done} / {total}</code>\n\n"
                                    f"<i>🚫 /bypass to manage</i>"
                                )
                            await asyncio.sleep(1)
            else:
                selected_b_id = bot_id_list[current_ub_idx % len(bot_id_list)]

            curr_ub = ub_pool[selected_b_id]
            curr_ub_quota_count = len(ub_history[selected_b_id]) + 1
            quota_display = f"{curr_ub_quota_count}/{hourly_quota}" if hourly_quota > 0 else "Active"

            bar = _progress_bar(done, total)
            await _upd(BOT_INSTANCE, job_id, chat_id,
                f"\U0001f504 <b>URL Sʜᴏʀᴛᴇɴᴇʀ Bʏᴘᴀss</b>\n"
                f"<code>{bar}</code>\n\n"
                f"<b>\u00bb  Active UB  :</b> <code>UB {selected_b_id[:6]} ({quota_display})</code>\n"
                f"<b>\u00bb  Bypass Bot :</b> <code>@{bypass_bot_uname}</code>\n"
                f"<b>\u00bb  Pᴏsᴛ ID    :</b> <code>{post_id}</code>\n"
                f"<b>\u00bb  Lɪɴᴋ      :</b> <code>{done+1} / {total}</code>\n"
                f"<b>\u00bb  Lᴀʙᴇʟ    :</b> <code>{label[:35]}</code>\n"
                f"<b>\u00bb  Sᴛᴇᴘ     :</b> Processing post...\n\n"
                f"<i>\u26d4 /bypass to manage</i>"
            )

            # ── 1. Forward/Copy Original Source Post to Target Channel ──
            if target_channel_id:
                try:
                    if not hasattr(curr_ub, '_network_lock'): curr_ub._network_lock = asyncio.Lock()
                    async with curr_ub._network_lock:
                        await curr_ub.copy_message(chat_id=target_channel_id, from_chat_id=channel_id, message_id=post_id)
                except Exception as e:
                    logger.warning(f"[Bypass] Failed to copy post {post_id} to target channel: {e}")

            # ── 2. Bypass / Resolve Bot Deep Link ──
            bypassed = None
            bot_uname, param = _parse_start(short_url)
            if bot_uname and param:
                bypassed = short_url
            else:
                attempt = 0
                while not bypassed:
                    job = await _get_bypass_job(job_id)
                    if not job or job.get("status") in ("stopped", "failed"): break
                    if job_id not in _ub_tasks: break

                    attempt += 1
                    if attempt > 1:
                        wait_time = random.randint(30, 60)
                        for w_sec in range(wait_time):
                            if job_id not in _ub_tasks: break
                            ev = _ub_paused.get(job_id)
                            if ev and not ev.is_set(): await ev.wait()

                            job = await _get_bypass_job(job_id)
                            if not job or job.get("status") in ("stopped", "failed"): break

                            if w_sec % 5 == 0:
                                await _upd(BOT_INSTANCE, job_id, chat_id,
                                    f"⚠️ <b>@{bypass_bot_uname} did not respond!</b>\n"
                                    f"<b>Link:</b> <code>{done+1} / {total}</code>\n"
                                    f"<b>Attempt {attempt - 1} failed.</b>\n"
                                    f"⏳ Retrying in <code>{wait_time - w_sec}s</code>...\n\n"
                                    f"<i>🚫 /bypass to manage</i>"
                                )
                            await asyncio.sleep(1)

                        job = await _get_bypass_job(job_id)
                        if not job or job.get("status") in ("stopped", "failed"): break
                        if job_id not in _ub_tasks: break

                    sent_time = time.time()
                    try:
                        if not hasattr(curr_ub, '_network_lock'): curr_ub._network_lock = asyncio.Lock()
                        async with curr_ub._network_lock:
                            sent_msg = await asyncio.wait_for(curr_ub.send_message(bypass_bot_uname, short_url), timeout=20)
                            if sent_msg and getattr(sent_msg, 'date', None):
                                sent_time = sent_msg.date.timestamp()
                    except FloodWait as fw:
                        from plugins.arya_logger import log_admin_dm
                        if fw.value >= 60:
                            asyncio.create_task(log_admin_dm(BOT_INSTANCE, "FloodWait", "Userbot", fw.value, f"URL Bypass Send @{bypass_bot_uname}"))
                        await _upd(BOT_INSTANCE, job_id, chat_id,
                            f"⏳ <b>FloodWait:</b> Waiting {fw.value}s before retrying to send to @{bypass_bot_uname}..."
                        )
                        await asyncio.sleep(fw.value + 2)
                        continue
                    except Exception as e:
                        logger.warning(f"[Bypass] Send to @{bypass_bot_uname} failed (attempt {attempt}): {e}")
                        await asyncio.sleep(5)
                        continue
            
                    t0 = sent_time - 30
                    for check_step in range(75):
                        await asyncio.sleep(0.4)
                        if job_id not in _ub_tasks: break
                        
                        job = await _get_bypass_job(job_id)
                        if not job or job.get("status") in ("stopped", "failed"): break

                        try:
                            if not hasattr(curr_ub, '_network_lock'): curr_ub._network_lock = asyncio.Lock()
                            async with curr_ub._network_lock:
                                async for m in curr_ub.get_chat_history(bypass_bot_uname, limit=5):
                                    ts = m.date.timestamp() if m.date else 0
                                    if ts < t0: break
                                    c = _parse_bypassed(m.text or m.caption or '')
                                    if c and c != short_url:
                                        bypassed = c
                                        break
                            if bypassed: break
                        except FloodWait as fw:
                            from plugins.arya_logger import log_admin_dm
                            if fw.value >= 60:
                                asyncio.create_task(log_admin_dm(BOT_INSTANCE, "FloodWait", "Userbot", fw.value, f"URL Bypass Check @{bypass_bot_uname}"))
                            await asyncio.sleep(fw.value)
                        except Exception as e:
                            logger.warning(f"Error checking @{bypass_bot_uname}: {e}")

            job = await _get_bypass_job(job_id)
            if not job or job.get("status") in ("stopped", "failed"): break
            if job_id not in _ub_tasks: break

            if not bypassed:
                continue

            bot_uname, param = _parse_start(bypassed)
            if not bot_uname:
                failed.append((label, "Not a ?start= link"))
                done += 1
                await _update_bypass_job(job_id, {"done": done, "failed": failed})
                await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
                continue

            await _upd(BOT_INSTANCE, job_id, chat_id,
                f"\U0001f504 <b>URL Sʜᴏʀᴛᴇɴᴇʀ Bʏᴘᴀss</b>\n"
                f"<code>{bar}</code>\n\n"
                f"<b>\u00bb  Active UB  :</b> <code>UB {selected_b_id[:6]} ({quota_display})</code>\n"
                f"<b>\u00bb  Lɪɴᴋ      :</b> <code>{done+1} / {total}</code>\n"
                f"<b>\u00bb  Sᴛᴇᴘ     :</b> Sending /start to @{bot_uname}...\n\n"
                f"<i>\u26d4 /bypass to manage</i>"
            )

            # ── 3. Send /start {param} to Target Bot ──
            start_ok = False
            start_attempt = 0
            wait_since = time.time() - 10
            while not start_ok:
                job = await _get_bypass_job(job_id)
                if not job or job.get("status") in ("stopped", "failed"): break
                if job_id not in _ub_tasks: break

                start_attempt += 1
                if start_attempt > 1:
                    await asyncio.sleep(15)
                    if job_id not in _ub_tasks: break

                try:
                    if not hasattr(curr_ub, '_network_lock'): curr_ub._network_lock = asyncio.Lock()
                    async with curr_ub._network_lock:
                        sent_msg = await asyncio.wait_for(curr_ub.send_message(bot_uname, f"/start {param}"), timeout=20)
                        if sent_msg and getattr(sent_msg, 'date', None):
                            wait_since = sent_msg.date.timestamp() - 10
                        else:
                            wait_since = time.time() - 10
                    start_ok = True
                except FloodWait as fw:
                    from plugins.arya_logger import log_admin_dm
                    if fw.value >= 60:
                        asyncio.create_task(log_admin_dm(BOT_INSTANCE, "FloodWait", "Userbot", fw.value, f"URL Bypass Start @{bot_uname}"))
                    await _upd(BOT_INSTANCE, job_id, chat_id,
                        f"⏳ <b>FloodWait:</b> Waiting {fw.value}s before retrying /start to @{bot_uname}..."
                    )
                    await asyncio.sleep(fw.value + 2)
                except Exception as e:
                    logger.warning(f"[Bypass] /start failed for @{bot_uname} (attempt {start_attempt}): {e}")
                    err_str = str(e).lower()
                    if "username_not_occupied" in err_str or "username_invalid" in err_str or "peer_id_invalid" in err_str:
                        failed.append((label, f"/start permanent fail: {e}"))
                        break
                    if start_attempt >= 5:
                        failed.append((label, f"/start failed after 5 attempts: {e}"))
                        break

            job = await _get_bypass_job(job_id)
            if not job or job.get("status") in ("stopped", "failed"): break
            if job_id not in _ub_tasks: break

            if not start_ok:
                done += 1
                await _update_bypass_job(job_id, {"done": done, "failed": failed})
                await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
                continue

            # ── 4. Capture Incoming Video/Media Files ──
            files = 0; last_file = wait_since; got_file = False
            adaptive = BASE_IDLE_SEC
            received_media_ids = set()
            received_media_msgs = []

            while True:
                await asyncio.sleep(2)
                if job_id not in _ub_tasks: break
                try:
                    nc = 0
                    if not hasattr(curr_ub, '_network_lock'): curr_ub._network_lock = asyncio.Lock()
                    async with curr_ub._network_lock:
                        async for m in curr_ub.get_chat_history(bot_uname, limit=20):
                            ts = m.date.timestamp() if m.date else 0
                            if ts < wait_since: break
                            if m.media or m.document or m.video or m.audio or m.voice:
                                nc += 1
                                if ts > last_file: last_file = ts; got_file = True
                                if m.id not in received_media_ids:
                                    received_media_ids.add(m.id)
                                    received_media_msgs.append(m)
                    
                    if nc > files:
                        files = nc
                        adaptive = BASE_IDLE_SEC + int(files * 0.7)
                        
                except FloodWait as fw:
                    from plugins.arya_logger import log_admin_dm
                    if fw.value >= 60:
                        asyncio.create_task(log_admin_dm(BOT_INSTANCE, "FloodWait", "Userbot", fw.value, "URL Bypass Fetch Files"))
                    await asyncio.sleep(fw.value)
                except Exception as e:
                    logger.warning(f"Error fetching files: {e}")
                    
                idle   = time.time() - last_file
                waited = time.time() - wait_since
                if got_file and idle >= adaptive: break
                if not got_file and waited > 90:  break
                
                await _upd(BOT_INSTANCE, job_id, chat_id,
                    f"\U0001f504 <b>URL Sʜᴏʀᴛᴇɴᴇʀ Bʏᴘᴀss</b>\n"
                    f"<code>{_progress_bar(done, total)}</code>\n\n"
                    f"<b>\u00bb  Active UB  :</b> <code>UB {selected_b_id[:6]} ({quota_display})</code>\n"
                    f"<b>\u00bb  Lɪɴᴋ      :</b> <code>{done+1} / {total}</code>\n"
                    f"<b>\u00bb  Fɪʟᴇs     :</b> <code>{files}</code> received\n"
                    f"<b>\u00bb  Iᴅʟᴇ Tɪᴍᴇ :</b> <code>{int(idle)}s / {adaptive}s</code>\n\n"
                    f"<i>\u26d4 /bypass to manage</i>"
                )

            # ── 5. Forward Captured Video/Media to Target Channel ──
            if target_channel_id and received_media_msgs:
                received_media_msgs.sort(key=lambda m: m.id)
                for m_msg in received_media_msgs:
                    try:
                        if not hasattr(curr_ub, '_network_lock'): curr_ub._network_lock = asyncio.Lock()
                        async with curr_ub._network_lock:
                            await curr_ub.copy_message(chat_id=target_channel_id, from_chat_id=m_msg.chat.id, message_id=m_msg.id)
                    except Exception as e:
                        logger.warning(f"[Bypass] Failed to forward video message {m_msg.id} to target channel: {e}")

            # Record success in quota history
            ub_history[selected_b_id].append(time.time())
            done += 1
            await _update_bypass_job(job_id, {"done": done, "failed": failed})
            
            if pacing > 0 and done < total:
                wait_sec = pacing * 60
                await _upd(BOT_INSTANCE, job_id, chat_id,
                    f"⏳ <b>Pacing Delay:</b> Waiting {pacing} min for Live Job synchronization...\n"
                    f"<i>Job will resume automatically. /bypass to manage.</i>"
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
            await _upd(BOT_INSTANCE, job_id, chat_id,
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
        for b_id, ub_inst in ub_pool.items():
            if b_id in _ub_refcounts:
                _ub_refcounts[b_id] -= 1
                logger.info(f"[Bypass] Job {job_id[:8]} released Userbot {b_id} (Refs: {_ub_refcounts[b_id]})")
                if _ub_refcounts[b_id] <= 0:
                    try:
                        from plugins.test import release_client
                        await release_client(ub_inst.name)
                        logger.info(f"[Bypass] Fully stopped Userbot {b_id}")
                    except Exception as e:
                        logger.error(f"Failed to release ub {b_id}: {e}")
                    _active_ubs.pop(b_id, None)
                    _ub_refcounts.pop(b_id, None)

# ── Stop ──────────────────────────────────────────────────────────────────────
@Client.on_message(filters.private & filters.command(['stopbypass', 'bypass_stop']))
async def bypass_stop_cmd(bot, message):
    await message.reply_text("Please use /bypass to manage and stop your jobs.", parse_mode=PM)
