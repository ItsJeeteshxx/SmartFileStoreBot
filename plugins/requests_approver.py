"""
Join Requests Approver Agentic System - Arya Forward Bot
=========================================================
Automatically approves pending & live incoming join requests in Telegram Channels & Groups.
Auto-skips deleted accounts, suspended users, and users who reached Telegram's 500 channels limit.
Provides clean UI matching URL Bypass with detailed final execution reports.
"""
from __future__ import annotations
import asyncio, re, time, logging, random, uuid
from typing import Optional
from pyrogram import Client, filters, enums, ContinuePropagation
from pyrogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from pyrogram.errors import (
    FloodWait, UserChannelsTooMuch, UsersTooMuch,
    UserDeleted, UserIdInvalid, HideRequesterMissing, UserAlreadyParticipant
)
from database import db
from plugins.owner_utils import require_feature
from bot import BOT_INSTANCE

logger = logging.getLogger(__name__)
PM = enums.ParseMode.HTML

REQ_COLL = "requests_jobs"

_req_tasks: dict[str, asyncio.Task] = {}
_req_paused: dict[str, asyncio.Event] = {}
_last_req_edits: dict[int, float] = {}
_pending_req_edits: dict[str, asyncio.Task] = {}

_active_req_ubs: dict = {}
_req_ub_refcounts: dict = {}

async def _save_requests_job(job: dict):
    await db.db[REQ_COLL].replace_one({"job_id": job["job_id"]}, job, upsert=True)

async def _get_requests_job(jid: str):
    return await db.db[REQ_COLL].find_one({"job_id": jid})

async def _get_all_requests_jobs(uid: int):
    return [j async for j in db.db[REQ_COLL].find({"user_id": uid}).sort("created_at", -1)]

async def _delete_requests_job(jid: str):
    await db.db[REQ_COLL].delete_one({"job_id": jid})

async def _update_requests_job(jid: str, kw: dict):
    await db.db[REQ_COLL].update_one({"job_id": jid}, {"$set": kw})


_req_waiting: dict = {}

CANCEL_BTN = KeyboardButton("⛔ Cᴀɴᴄᴇʟ")
UNDO_BTN   = KeyboardButton("↩️ Uɴᴅᴏ")

def _is_cancel(t): return "⛔" in t or "cancel" in t.lower()
def _is_undo(t):   return "↩️" in t or "undo" in t.lower()


# ── Input router ──────────────────────────────────────────────────────────────
@Client.on_message(filters.private, group=-18)
async def _req_input_router(bot, message):
    uid = message.from_user.id if message.from_user else None
    if uid and uid in _req_waiting:
        fut = _req_waiting.pop(uid)
        if not fut.done():
            fut.set_result(message)
    raise ContinuePropagation

async def _ask_req(bot, user_id: int, text: str, reply_markup=None, timeout: int = 300):
    loop = asyncio.get_event_loop()
    fut  = loop.create_future()
    old  = _req_waiting.pop(user_id, None)
    if old and not old.done():
        old.cancel()
    _req_waiting[user_id] = fut
    await bot.send_message(user_id, text, parse_mode=PM, reply_markup=reply_markup,
                           disable_web_page_preview=True)
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        _req_waiting.pop(user_id, None)
        raise


# Tracks current status message per job: job_id → (chat_id, msg_id)
_status_req_msgs: dict = {}

def _progress_bar(done: int, total: int, width: int = 10) -> str:
    if not total:
        return f"[░░░░░░░░░░] 0/0 (0%)"
    pct = done / total
    filled = round(width * pct)
    bar = '█' * filled + '░' * (width - filled)
    return f"[{bar}] {done}/{total} ({int(pct*100)}%)"

async def _upd_req(bot, job_id: str, chat_id: int, text: str, force: bool = False):
    """Edit status message strictly; edit the exact same message repeatedly to prevent chat spam."""
    global _status_req_msgs
    old = _status_req_msgs.get(job_id)
    
    if not old and job_id:
        try:
            job = await _get_requests_job(job_id)
            if job and job.get("status_msg_id"):
                old = (chat_id, job["status_msg_id"])
                _status_req_msgs[job_id] = old
        except Exception: pass

    if old:
        c_id, msg_id = old
        last_t = _last_req_edits.get(msg_id, 0)
        now_t = time.time()
        
        # Throttled rate limit (1.2s to prevent Telegram FloodWait on edit_message_text)
        if not force and (now_t - last_t < 1.2):
            if job_id not in _pending_req_edits or _pending_req_edits[job_id].done():
                async def _delayed_edit():
                    await asyncio.sleep(1.2)
                    await _upd_req(bot, job_id, chat_id, text, force=True)
                _pending_req_edits[job_id] = asyncio.create_task(_delayed_edit())
            return
            
        try:
            await bot.edit_message_text(
                c_id, msg_id, text,
                parse_mode=PM, disable_web_page_preview=True
            )
            _last_req_edits[msg_id] = time.time()
            return  # edited successfully
        except FloodWait as fw:
            _last_req_edits[msg_id] = time.time() + fw.value
            return
        except Exception:
            pass  # fall through → send new status msg if previous was deleted
            
    try:
        sent = await bot.send_message(
            chat_id, text, parse_mode=PM, disable_web_page_preview=True
        )
        _status_req_msgs[job_id] = (chat_id, sent.id)
        _last_req_edits[sent.id] = time.time()
        await _update_requests_job(job_id, {"status_msg_id": sent.id})
    except Exception as e:
        logger.warning(f"[Requests] _upd_req failed: {e}")


# ── Userbot loader ────────────────────────────────────────────────────────────
async def _load_req_ub(user_id: int, bot_id: str):
    """Start a fresh independent userbot client for join requests, or reuse active."""
    bot_id = str(bot_id)
    if bot_id in _active_req_ubs and _active_req_ubs[bot_id].is_connected:
        _req_ub_refcounts[bot_id] = _req_ub_refcounts.get(bot_id, 0) + 1
        return _active_req_ubs[bot_id]
        
    bots     = await db.get_bots(user_id)
    userbots = [b for b in bots if not b.get('is_bot', True)]
    target   = next((b for b in userbots if str(b.get('id','')) == str(bot_id)), None)
    if not target:
        return None
    session = target.get('session') or target.get('session_string')
    if not session:
        return None
    try:
        from plugins.test import CLIENT, start_clone_bot
        _c_mgr = CLIENT()
        _fresh_ub = _c_mgr.client(target)
        ub = await start_clone_bot(_fresh_ub, data=target)
        
        _active_req_ubs[bot_id] = ub
        _req_ub_refcounts[bot_id] = 1
        return ub
    except Exception as e:
        logger.error(f"[Requests] Userbot {bot_id} start failed: {e}")
        return None


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


def _req_emoji(status: str) -> str:
    return {
        "running": "🟢",
        "paused": "⏸",
        "stopped": "🔴",
        "completed": "✅",
        "failed": "❌"
    }.get(status, "⭘")


# ── Menu & Dashboard ──────────────────────────────────────────────────────────
async def _send_requests_menu(bot, uid, chat_id, mid=None):
    jobs = await _get_all_requests_jobs(uid)
    
    if not jobs:
        txt = (
            "ℹ️ <b>No Join Requests Approver jobs found.</b>\n\n"
            "Use the button below or send /requests to start a new job."
        )
        kb = [
            [InlineKeyboardButton("➕ Start New Requests Job", callback_data="req#new")],
            [InlineKeyboardButton("❮ Bᴀᴄᴋ", callback_data="back")]
        ]
        if mid:
            try: return await bot.edit_message_text(chat_id, mid, txt, reply_markup=InlineKeyboardMarkup(kb))
            except: pass
        return await bot.send_message(chat_id, txt, reply_markup=InlineKeyboardMarkup(kb))
        
    txt = f"<b>📨 Join Requests Approver Jobs</b>\n<i>You have {len(jobs)} total jobs.</i>\n\n"
    kb = []
    
    for i, j in enumerate(jobs, 1):
        jid = j["job_id"]
        status = j.get("status", "running")
        ev = _req_paused.get(jid)
        if status == "running" and ev and not ev.is_set():
            status = "paused"
            
        approved = j.get('approved', 0)
        skipped  = j.get('skipped_total', 0)
        total    = j.get('total_scanned', approved + skipped)
        
        status_emoji = _req_emoji(status)
        txt += f"<b>{i}. {j.get('channel_title', '?')}</b>\n"
        txt += f"   Status: {status_emoji} <code>{status.upper()}</code>\n"
        txt += f"   Progress: <code>{_progress_bar(approved + skipped, total, width=8)}</code>\n"
        txt += f"   Approved: <code>{approved}</code> | Skipped: <code>{skipped}</code>\n\n"
        
        row = []
        if status == "running":
            row.append(InlineKeyboardButton(f"⏸ Pause [{i}]", callback_data=f"req_pause:{jid}"))
            row.append(InlineKeyboardButton(f"🛑 Stop [{i}]", callback_data=f"req_stop:{jid}"))
        elif status == "paused":
            row.append(InlineKeyboardButton(f"▶️ Resume [{i}]", callback_data=f"req_resume:{jid}"))
            row.append(InlineKeyboardButton(f"🛑 Stop [{i}]", callback_data=f"req_stop:{jid}"))
        else:
            row.append(InlineKeyboardButton(f"▶️ Start [{i}]", callback_data=f"req_resume:{jid}"))
            row.append(InlineKeyboardButton(f"🔁 Reset [{i}]", callback_data=f"req_reset:{jid}"))
            
        row.append(InlineKeyboardButton(f"🗑 Delete [{i}]", callback_data=f"req_del:{jid}"))
        kb.append(row)
        
    kb.append([
        InlineKeyboardButton("🔄 Refresh", callback_data="req_menu_refresh"),
        InlineKeyboardButton("➕ Start New Job", callback_data="req#new")
    ])
    kb.append([
        InlineKeyboardButton("❮ Bᴀᴄᴋ", callback_data="back")
    ])
    
    if mid:
        try: return await bot.edit_message_text(chat_id, mid, txt, reply_markup=InlineKeyboardMarkup(kb))
        except: pass
    return await bot.send_message(chat_id, txt, reply_markup=InlineKeyboardMarkup(kb))


@Client.on_message(filters.private & filters.command(['requests', 'join_requests', 'approver']))
@require_feature("join_requests")
async def requests_jobs_cmd(bot, message):
    await _send_requests_menu(bot, message.from_user.id, message.chat.id)

@Client.on_callback_query(filters.regex(r'^req#main$'))
async def requests_menu_main_cb(bot, query):
    await query.answer()
    await _send_requests_menu(bot, query.from_user.id, query.message.chat.id, query.message.id)

@Client.on_callback_query(filters.regex(r'^req_menu_refresh$'))
async def requests_menu_refresh_cb(bot, query):
    await query.answer()
    await _send_requests_menu(bot, query.from_user.id, query.message.chat.id, query.message.id)

@Client.on_callback_query(filters.regex(r'^req_(pause|resume|stop|reset|del):(.+)$'))
async def requests_action_cb(bot, query):
    action, jid = query.matches[0].groups()
    job = await _get_requests_job(jid)
    if not job:
        return await query.answer("Job not found.", show_alert=True)
    if job["user_id"] != query.from_user.id:
        return await query.answer("Not your job.", show_alert=True)
        
    if action == "pause":
        if jid in _req_paused: _req_paused[jid].clear()
        await _update_requests_job(jid, {"status": "paused"})
        await query.answer("Job paused.")
        
    elif action == "resume":
        await _update_requests_job(jid, {"status": "running"})
        if jid not in _req_tasks:
            _req_paused[jid] = asyncio.Event()
            _req_paused[jid].set()
            _req_tasks[jid] = asyncio.create_task(_requests_run_job(jid))
        else:
            if jid in _req_paused: _req_paused[jid].set()
        await query.answer("Job resumed.")
        
    elif action == "stop":
        await _update_requests_job(jid, {"status": "stopped"})
        if jid in _req_tasks:
            _req_tasks[jid].cancel()
            _req_tasks.pop(jid, None)
        await query.answer("Job stopped.")

    elif action == "reset":
        await _update_requests_job(jid, {
            "status": "running", "approved": 0, "skipped_deleted": 0,
            "skipped_limit": 0, "skipped_total": 0, "failed_count": 0, "total_scanned": 0
        })
        if jid in _req_tasks:
            _req_tasks[jid].cancel()
            _req_tasks.pop(jid, None)
        _req_paused[jid] = asyncio.Event()
        _req_paused[jid].set()
        _req_tasks[jid] = asyncio.create_task(_requests_run_job(jid))
        await query.answer("Job reset and restarted.")

    elif action == "del":
        if jid in _req_tasks:
            _req_tasks[jid].cancel()
            _req_tasks.pop(jid, None)
        if jid in _req_paused:
            _req_paused.pop(jid, None)
        await _delete_requests_job(jid)
        await query.answer("Job deleted.")
        
    await _send_requests_menu(bot, query.from_user.id, query.message.chat.id, query.message.id)


@Client.on_callback_query(filters.regex(r'^req#new$'))
@require_feature("join_requests")
async def requests_new_cb(bot, query):
    await query.answer()
    user_id = query.from_user.id
    chat_id = query.message.chat.id
    try: await query.message.delete()
    except Exception: pass
    await _requests_flow(bot, user_id, chat_id)


# ── Interactive Setup Flow ────────────────────────────────────────────────────
async def _requests_flow(bot, user_id: int, chat_id: int):
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
        r1 = await _ask_req(bot, user_id,
            "<b>»  Join Requests Approver — Step 1/4</b>\n\n"
            "Select the <b>Userbot</b> to run this job:\n\n"
            "<blockquote>The userbot must be an Admin with 'Invite Users' permission in the channel/group.</blockquote>",
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

    # Step 2: Channel / Group
    try:
        r2 = await _ask_req(bot, user_id,
            "<b>»  Join Requests Approver — Step 2/4</b>\n\n"
            "Send the <b>Channel or Group</b> username / ID / link:\n\n"
            "<blockquote expandable>"
            "• <code>https://t.me/channelname</code>\n"
            "• <code>-1001234567890</code>\n"
            "• Forward any message from the channel\n"
            "</blockquote>",
            reply_markup=ReplyKeyboardMarkup([[UNDO_BTN, CANCEL_BTN]], resize_keyboard=True, one_time_keyboard=True))
    except asyncio.TimeoutError:
        return await bot.send_message(chat_id, "<i>Timed out.</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())
    if _is_cancel(r2.text):
        return await bot.send_message(chat_id, "<i>Cancelled!</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())

    fwd_chat = getattr(r2, 'forward_from_chat', None)
    channel_id, channel_title = _resolve_channel(r2.text, fwd_chat)
    if not channel_id:
        await bot.send_message(chat_id, "<b>Could not resolve channel/group.</b>\nSend /requests to retry.",
            parse_mode=PM, reply_markup=ReplyKeyboardRemove())
        return
    try:
        ci = await bot.get_chat(channel_id)
        channel_title = ci.title or channel_title
        channel_id    = ci.id
    except Exception:
        pass

    # Step 3: Processing Mode
    try:
        r3 = await _ask_req(bot, user_id,
            f"<b>»  Join Requests Approver — Step 3/4</b>\n\n"
            f"Channel: <b>{channel_title}</b>\n\n"
            "Choose <b>Approval Mode</b>:\n\n"
            "<blockquote>"
            "• <b>Old & Live Requests</b> — Approve old pending requests + auto-approve live new requests\n"
            "• <b>Old Requests Only</b> — Scan and approve existing pending requests only\n"
            "• <b>Live Requests Only</b> — Auto-approve live incoming requests only"
            "</blockquote>",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("» Old & Live Requests")],
                 [KeyboardButton("» Old Requests Only"), KeyboardButton("» Live Requests Only")],
                 [UNDO_BTN, CANCEL_BTN]],
                resize_keyboard=True, one_time_keyboard=True))
    except asyncio.TimeoutError:
        return await bot.send_message(chat_id, "<i>Timed out.</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())
    if _is_cancel(r3.text):
        return await bot.send_message(chat_id, "<i>Cancelled!</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())

    mode = "both"
    if "Old Requests Only" in r3.text: mode = "old_only"
    elif "Live Requests Only" in r3.text: mode = "live_only"
    
    mode_label = {
        "both": "⚡ Old & Live Requests",
        "old_only": "📜 Old Requests Only",
        "live_only": "🔴 Live Requests Only"
    }.get(mode, "Old & Live Requests")

    # Step 4: Auto-Skip Protection Settings
    try:
        r4 = await _ask_req(bot, user_id,
            "<b>»  Join Requests Approver — Step 4/4</b>\n\n"
            "Enable <b>Auto-Skip Protection</b>?\n\n"
            "<blockquote>"
            "• <b>YES (Recommended)</b> — Automatically bypasses Deleted Accounts, Suspended Users, "
            "and users who reached Telegram's 500 channel limit.\n"
            "• <b>NO</b> — Try approving all accounts regardless of status."
            "</blockquote>",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("✅ YES (Recommended)"), KeyboardButton("❌ NO")],
                 [UNDO_BTN, CANCEL_BTN]],
                resize_keyboard=True, one_time_keyboard=True))
    except asyncio.TimeoutError:
        return await bot.send_message(chat_id, "<i>Timed out.</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())
    if _is_cancel(r4.text):
        return await bot.send_message(chat_id, "<i>Cancelled!</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())

    auto_skip = "NO" not in r4.text

    # Step 5: Confirm
    try:
        r5 = await _ask_req(bot, user_id,
            f"<b>»  Join Requests Approver — Confirm</b>\n\n"
            f"»  Userbot: <b>{ub_name}</b>\n"
            f"»  Target: <b>{channel_title}</b>\n"
            f"»  Mode: <b>{mode_label}</b>\n"
            f"»  Auto-Skip Invalid Users: <b>{'✅ YES' if auto_skip else '❌ NO'}</b>\n\n"
            "<i>Tap Confirm to start approving requests.</i>",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("✅ Confirm & Start")], [CANCEL_BTN]],
                resize_keyboard=True, one_time_keyboard=True))
    except asyncio.TimeoutError:
        return await bot.send_message(chat_id, "<i>Timed out.</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())
    if _is_cancel(r5.text) or '✅' not in r5.text:
        return await bot.send_message(chat_id, "<i>Cancelled!</i>", parse_mode=PM, reply_markup=ReplyKeyboardRemove())

    job_id = str(uuid.uuid4())
    job = {
        "job_id": job_id, "user_id": user_id, "status": "running",
        "bot_id": bot_id, "ub_name": ub_name, "chat_id": chat_id,
        "channel_id": channel_id, "channel_title": channel_title,
        "mode": mode, "auto_skip": auto_skip,
        "approved": 0, "skipped_deleted": 0, "skipped_limit": 0,
        "skipped_total": 0, "failed_count": 0, "total_scanned": 0,
        "created_at": time.time()
    }
    await _save_requests_job(job)
    _req_paused[job_id] = asyncio.Event()
    _req_paused[job_id].set()

    status_msg = await bot.send_message(
        chat_id,
        f"<b>»  Join Requests Approver — Starting</b>\n\n"
        f"»  Job ID: <code>{job_id[:8]}</code>\n"
        f"⏳ Connecting userbot...",
        parse_mode=PM, reply_markup=ReplyKeyboardRemove()
    )
    _status_req_msgs[job_id] = (chat_id, status_msg.id)
    job["status_msg_id"] = status_msg.id
    await _save_requests_job(job)
    
    task = asyncio.create_task(_requests_run_job(job_id))
    _req_tasks[job_id] = task


# ── Execution Engine ──────────────────────────────────────────────────────────
async def _requests_run_job(job_id: str):
    ub = None
    try:
        job = await _get_requests_job(job_id)
        if not job or job.get("status") in ("stopped", "failed", "completed"):
            return
            
        user_id = job["user_id"]
        chat_id = job["chat_id"]
        channel_id = job["channel_id"]
        channel_title = job["channel_title"]
        mode = job.get("mode", "both")
        auto_skip = job.get("auto_skip", True)
        
        logger.info(f"[Requests] Loading userbot {job['bot_id']} for {channel_title}...")
        ub = await _load_req_ub(user_id, job["bot_id"])
        if not ub:
            await _update_requests_job(job_id, {"status": "failed", "error": "Userbot connect fail"})
            return await _upd_req(BOT_INSTANCE, job_id, chat_id, "<b>»  Failed to connect userbot!</b>")

        await _upd_req(BOT_INSTANCE, job_id, chat_id, f"✅ Connected userbot <b>{job['ub_name']}</b>! Scanning requests...")
        
        approved = job.get("approved", 0)
        skipped_deleted = job.get("skipped_deleted", 0)
        skipped_limit   = job.get("skipped_limit", 0)
        skipped_total   = job.get("skipped_total", 0)
        failed_count    = job.get("failed_count", 0)
        total_scanned   = job.get("total_scanned", 0)

        # ── PHASE 1: Process Existing / Old Pending Join Requests ─────────────────────
        if mode in ("both", "old_only"):
            try:
                async for req in ub.get_chat_join_requests(channel_id):
                    # Check pause / stop state
                    ev = _req_paused.get(job_id)
                    if ev and not ev.is_set():
                        await ev.wait()
                        
                    job = await _get_requests_job(job_id)
                    if not job or job.get("status") in ("stopped", "failed"):
                        break

                    total_scanned += 1
                    req_user = getattr(req, "user", None)
                    u_id = req_user.id if req_user else getattr(req, "user_id", None)
                    
                    # Pre-check: Deleted accounts or bot accounts
                    is_deleted = getattr(req_user, "is_deleted", False) if req_user else False
                    is_bot_user = getattr(req_user, "is_bot", False) if req_user else False
                    
                    if auto_skip and (is_deleted or is_bot_user):
                        skipped_deleted += 1
                        skipped_total += 1
                        logger.info(f"[Requests {job_id[:8]}] Auto-skipped deleted/bot user {u_id}")
                        await _update_requests_job(job_id, {
                            "total_scanned": total_scanned,
                            "skipped_deleted": skipped_deleted,
                            "skipped_total": skipped_total
                        })
                        continue

                    # Attempt approval with exception handling
                    success = False
                    retry_cnt = 0
                    while not success and retry_cnt < 3:
                        retry_cnt += 1
                        try:
                            await ub.approve_chat_join_request(channel_id, u_id)
                            approved += 1
                            success = True
                        except (UserChannelsTooMuch, UsersTooMuch):
                            if auto_skip:
                                skipped_limit += 1
                                skipped_total += 1
                                logger.info(f"[Requests {job_id[:8]}] Auto-skipped user {u_id} (500 channel limit reached)")
                            else:
                                failed_count += 1
                            success = True  # handled
                        except (UserDeleted, UserIdInvalid, HideRequesterMissing):
                            if auto_skip:
                                skipped_deleted += 1
                                skipped_total += 1
                                logger.info(f"[Requests {job_id[:8]}] Auto-skipped deleted/invalid user {u_id}")
                            else:
                                failed_count += 1
                            success = True  # handled
                        except UserAlreadyParticipant:
                            approved += 1
                            success = True
                        except FloodWait as fw:
                            logger.warning(f"[Requests {job_id[:8]}] FloodWait {fw.value}s during join request approval")
                            await _upd_req(BOT_INSTANCE, job_id, chat_id,
                                f"⏳ <b>FloodWait:</b> Waiting {fw.value}s before approving requests..."
                            )
                            await asyncio.sleep(fw.value + 1)
                        except Exception as e:
                            logger.warning(f"[Requests {job_id[:8]}] Approve user {u_id} error: {e}")
                            if retry_cnt >= 3:
                                failed_count += 1
                            await asyncio.sleep(1)

                    await _update_requests_job(job_id, {
                        "approved": approved,
                        "skipped_deleted": skipped_deleted,
                        "skipped_limit": skipped_limit,
                        "skipped_total": skipped_total,
                        "failed_count": failed_count,
                        "total_scanned": total_scanned
                    })

                    # Live UI Update
                    bar = _progress_bar(approved + skipped_total, max(total_scanned, 1))
                    await _upd_req(BOT_INSTANCE, job_id, chat_id,
                        f"\U0001f4e8 <b>Jᴏɪɴ Rᴇǫᴜᴇsᴛs Aᴘᴘʀᴏᴠᴇʀ</b>\n"
                        f"<code>{bar}</code>\n\n"
                        f"<b>» Target     :</b> <code>{channel_title}</code>\n"
                        f"<b>» Scanned    :</b> <code>{total_scanned}</code>\n"
                        f"<b>» Approved   :</b> <code>{approved}</code>\n"
                        f"<b>» Auto-Skip  :</b> <code>{skipped_total}</code> (Deleted: {skipped_deleted}, 500 Limit: {skipped_limit})\n"
                        f"<b>» Status     :</b> Approving pending requests...\n\n"
                        f"<i>🚫 /requests to manage</i>"
                    )

                    await asyncio.sleep(0.3) # Fast pacing

            except Exception as e:
                logger.error(f"[Requests] Scan join requests error: {e}", exc_info=True)

        # ── Completion / Live Transition ──────────────────────────────────────────────
        job = await _get_requests_job(job_id)
        if job and job.get("status") == "running":
            await _update_requests_job(job_id, {"status": "completed"})
            
            report = (
                f"✅ <b>Jᴏɪɴ Rᴇǫᴜᴇsᴛs Aᴘᴘʀᴏᴠᴀʟ Cᴏᴍᴘʟᴇᴛᴇ!</b>\n"
                f"<code>{_progress_bar(total_scanned, max(total_scanned, 1))}</code>\n\n"
                f"<b>📊 Fɪɴᴀʟ Exᴇᴄᴜᴛɪᴏɴ Rᴇᴘᴏʀᴛ:</b>\n"
                f"<b>» Target Chat        :</b> <code>{channel_title}</code>\n"
                f"<b>» Total Scanned      :</b> <code>{total_scanned}</code>\n"
                f"<b>» Approved Users     :</b> <code>{approved}</code>\n"
                f"<b>» Auto-Skipped Users :</b> <code>{skipped_total}</code>\n"
                f"   ├ <code>{skipped_deleted}</code> Deleted/Bot Accounts\n"
                f"   └ <code>{skipped_limit}</code> Reached 500 Channel Limit\n"
                f"<b>» Failed/Errors      :</b> <code>{failed_count}</code>\n"
            )
            await _upd_req(BOT_INSTANCE, job_id, chat_id, report, force=True)

    except asyncio.CancelledError:
        pass
    except Exception as e:
        await _update_requests_job(job_id, {"status": "failed", "error": str(e)})
        logger.error(f"[Requests] job error: {e}", exc_info=True)
    finally:
        _req_tasks.pop(job_id, None)
        if ub and job and "bot_id" in job:
            bot_id = str(job["bot_id"])
            if bot_id in _req_ub_refcounts:
                _req_ub_refcounts[bot_id] -= 1
                if _req_ub_refcounts[bot_id] <= 0:
                    try:
                        from plugins.test import release_client
                        await release_client(ub.name)
                    except Exception: pass
                    _active_req_ubs.pop(bot_id, None)
                    _req_ub_refcounts.pop(bot_id, None)
