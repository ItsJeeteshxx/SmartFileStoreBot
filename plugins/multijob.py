"""
Multi Job Plugin
================
A "Multi Job" is a batch-only background copy operation.
Unlike Live Job (which watches for NEW messages), Multi Job copies a specific
range of old messages from a source to a target and then stops (done).

Key features:
  • All source types: public/private channels, groups, DMs, topics
  • Dual destinations (same as Live Job)
  • Simultaneous jobs running in parallel
  • Full global filter support
  • Pause / Resume / Stop / Delete per-job
  • Survives bot restart (resumes running jobs)

Commands:
  /multijob  — Open the Multi Job manager
"""
import re
import os
import time
import asyncio
import logging
from database import db
from .test import CLIENT, start_clone_bot, force_evict_client
from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
)

logger = logging.getLogger(__name__)
_CLIENT = CLIENT()
from plugins.job_queue import AryaJobQueue

#  In-memory task registry 
_mj_tasks:  dict[str, asyncio.Task]  = {}
_mj_paused: dict[str, asyncio.Event] = {}   # set=running, clear=paused

#  Future-based ask() — immune to pyrofork stale-listener bug 
_mj_waiting: dict[int, asyncio.Future] = {}


def _is_connected(client) -> bool:
    """Safely check if a Pyrogram Client is currently connected."""
    if not client:
        return False
    try:
        is_conn = getattr(client, "is_connected", None)
        if is_conn is None:
            return False
        return is_conn() if callable(is_conn) else bool(is_conn)
    except Exception:
        return False


# ─── Client health-check / reconnect ────────────────────────────────────
async def _mj_ensure_client_alive(client):
    try:
        if not getattr(client, "is_initialized", False):
            await client.start()
            return client
        if not _is_connected(client):
            await client.connect()
    except Exception as e:
        pass
    return client


from pyrogram import ContinuePropagation

@Client.on_message(filters.private, group=-11)
async def _mj_input_router(bot, message):
    """Route all private messages to any waiting _mj_ask() futures."""
    uid = message.from_user.id if message.from_user else None
    if uid and uid in _mj_waiting:
        fut = _mj_waiting.pop(uid)
        if not fut.done():
            fut.set_result(message)
    raise ContinuePropagation


async def _mj_ask(bot, user_id: int, text: str, reply_markup=None, timeout: int = 300):
    """Send text and wait for the next private message from user_id."""
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    old = _mj_waiting.pop(user_id, None)
    if old and not old.done():
        old.cancel()
    _mj_waiting[user_id] = fut
    await bot.send_message(user_id, text, reply_markup=reply_markup)
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        _mj_waiting.pop(user_id, None)
        raise


# ══════════════════════════════════════════════════════════════════════════════
# DB helpers
# ══════════════════════════════════════════════════════════════════════════════

COLL = "multijobs"


async def _mj_save(job: dict):
    await db.db[COLL].replace_one({"job_id": job["job_id"]}, job, upsert=True)


async def _mj_get(job_id: str) -> dict | None:
    return await db.db[COLL].find_one({"job_id": job_id})


async def _mj_list(user_id: int) -> list[dict]:
    return [j async for j in db.db[COLL].find({"user_id": user_id})]


async def _mj_delete(job_id: str):
    await db.db[COLL].delete_one({"job_id": job_id})


async def _mj_update(job_id: str, **kwargs):
    await db.db[COLL].update_one({"job_id": job_id}, {"$set": kwargs})


async def _mj_inc(job_id: str, n: int = 1):
    await db.db[COLL].update_one({"job_id": job_id}, {"$inc": {"forwarded": n}})
    import asyncio
    asyncio.create_task(db.update_global_stats(batch_forward=n))


# ══════════════════════════════════════════════════════════════════════════════
# Filter helpers (global user filters)
# ══════════════════════════════════════════════════════════════════════════════

def _msg_in_topic(msg, from_thread_id: int) -> bool:
    """Return True if msg belongs to the given source topic.
    
    Pyrogram sets `message_thread_id` on ALL messages inside a topic — this is
    the definitive check. The reply_to chain is kept as a secondary fallback for
    clients/versions that may omit the field.
    """
    # ── Primary: message_thread_id is always set by Telegram on topic messages ──
    tid = getattr(msg, "message_thread_id", None)
    if tid is not None:
        return int(tid) == from_thread_id

    # ── Secondary: message IS the topic header itself (id == thread_id) ──
    m_id = getattr(msg, "id", None)
    if m_id is not None and int(m_id) == from_thread_id:
        return True

    # ── Tertiary: reply_to fields ─────────────────────────────────────────────
    # reply_to_message_id points to the parent — either the topic header OR
    # any message inside the topic.  We accept it if it equals the thread_id
    # (classic case) OR if we can walk one level up and find thread_id.
    rttm = (
        getattr(msg, "reply_to_story_message_id", None)
        or getattr(msg, "reply_to_message_id", None)
    )
    if rttm is not None and int(rttm) == from_thread_id:
        return True

    reply_to = getattr(msg, "reply_to_message", None)
    if reply_to is not None:
        # Check thread_id on the reply_to object itself
        rt_tid = getattr(reply_to, "message_thread_id", None)
        if rt_tid is not None and int(rt_tid) == from_thread_id:
            return True
        rt_top = getattr(reply_to, "topic_message", None)
        if rt_top is not None and rt_top == from_thread_id:
            return True
        # reply_to's own reply_to_message_id
        rt_msg = getattr(reply_to, "reply_to_message_id", None)
        if rt_msg is not None and int(rt_msg) == from_thread_id:
            return True
        # Two levels deep: message replies to A, A replies to topic header
        rtrt = getattr(reply_to, "reply_to_message", None)
        if rtrt is not None:
            rtrt_tid = getattr(rtrt, "message_thread_id", None)
            if rtrt_tid is not None and int(rtrt_tid) == from_thread_id:
                return True
            rtrt_top = getattr(rtrt, "topic_message", None)
            if rtrt_top is not None and rtrt_top == from_thread_id:
                return True
            rtrt_msg = getattr(rtrt, "reply_to_message_id", None)
            if rtrt_msg is not None and int(rtrt_msg) == from_thread_id:
                return True

    return False



def _is_audio_msg(msg) -> bool:
    if getattr(msg, 'audio', None):
        return True
    doc = getattr(msg, 'document', None)
    if doc:
        fn = (getattr(doc, 'file_name', '') or '').lower()
        mime = (getattr(doc, 'mime_type', '') or '').lower()
        if mime.startswith('audio/') or fn.endswith(('.mp3', '.m4a', '.flac', '.wav', '.aac', '.ogg', '.opus', '.wma')):
            return True
    return False

def _is_video_msg(msg) -> bool:
    if getattr(msg, 'video', None) or getattr(msg, 'video_note', None):
        return True
    doc = getattr(msg, 'document', None)
    if doc:
        fn = (getattr(doc, 'file_name', '') or '').lower()
        mime = (getattr(doc, 'mime_type', '') or '').lower()
        if mime.startswith('video/') or fn.endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.3gp', '.m4v')):
            return True
    return False

def _is_photo_msg(msg) -> bool:
    if getattr(msg, 'photo', None):
        return True
    doc = getattr(msg, 'document', None)
    if doc:
        fn = (getattr(doc, 'file_name', '') or '').lower()
        mime = (getattr(doc, 'mime_type', '') or '').lower()
        if mime.startswith('image/') or fn.endswith(('.jpg', '.jpeg', '.png', '.webp', '.heic', '.bmp')):
            return True
    return False

def _passes_filters(msg, disabled_types: list) -> bool:
    if not msg or getattr(msg, 'empty', False) or getattr(msg, 'service', False):
        return False

    if not disabled_types:
        return True

    is_text = bool(getattr(msg, 'text', None) and (not getattr(msg, 'media', None) or getattr(getattr(msg, 'media', None), 'value', str(getattr(msg, 'media', None))) == 'web_page'))
    if 'text' in disabled_types and is_text:
        return False

    if _is_audio_msg(msg):
        if 'audio' in disabled_types: return False
        return True

    if _is_video_msg(msg):
        if 'video' in disabled_types: return False
        return True

    if _is_photo_msg(msg):
        if 'photo' in disabled_types: return False
        return True

    if 'voice' in disabled_types and getattr(msg, 'voice', None): return False
    if 'animation' in disabled_types and getattr(msg, 'animation', None): return False
    if 'sticker' in disabled_types and getattr(msg, 'sticker', None): return False
    if 'poll' in disabled_types and getattr(msg, 'poll', None): return False
    if 'document' in disabled_types and getattr(msg, 'document', None): return False

    return True


# ══════════════════════════════════════════════════════════════════════════════
# Forward helper — supports dual destination + topic threads
# ══════════════════════════════════════════════════════════════════════════════

async def _mj_forward(
    client, msg,
    to_chat: int, remove_caption: bool, cap_tpl: str | None, forward_tag: bool = False,
    thread_id: int = None,
    to_chat_2: int = None, thread_id_2: int = None,
    replacements: dict = None,
    remove_links_flag: bool = False
):
    from plugins.regix import custom_caption, remove_all_links
    import re

    # Compute caption
    new_caption = None
    new_text = None
    is_text_replaced = False

    if msg.media:
        new_caption = custom_caption(msg, cap_tpl, apply_smart_clean=remove_caption, remove_links_flag=remove_links_flag)
            
        if replacements and new_caption:
            for old_txt, new_txt_str in replacements.items():
                if old_txt is None: continue
                new_str = "" if new_txt_str is None else str(new_txt_str)
                try: new_caption = re.sub(str(old_txt), new_str, str(new_caption), flags=re.IGNORECASE)
                except Exception: new_caption = str(new_caption).replace(str(old_txt), new_str)
    else:
        new_text = getattr(msg.text, "html", str(msg.text)) if msg.text else ""
        if remove_links_flag and new_text:
            new_text = remove_all_links(new_text)
            is_text_replaced = True
            
        if replacements and new_text:
            orig_text = new_text
            for old_txt, new_txt_str in replacements.items():
                if old_txt is None: continue
                new_str = "" if new_txt_str is None else str(new_txt_str)
                try: new_text = re.sub(str(old_txt), new_str, str(new_text), flags=re.IGNORECASE)
                except Exception: new_text = str(new_text).replace(str(old_txt), new_str)
            if orig_text != new_text:
                is_text_replaced = True

    async def _send_one(chat, thread):
        # Use local flag — do NOT mutate nonlocal forward_tag as it would
        # contaminate the second destination call and all future messages.
        use_forward_tag = forward_tag
        if new_caption is not None or is_text_replaced:
            # Telegram CANNOT modify text/captions of natively forwarded messages.
            # If the user wants to wipe captions, remove links, or replace text, we MUST use copy_message.
            use_forward_tag = False

        kw = {"message_thread_id": thread} if thread else {}
        if new_caption is not None:
            kw["caption"] = new_caption

        for _send_attempt in range(30):
            try:
                if use_forward_tag:
                    try:
                        await client.forward_messages(
                            chat_id=chat, from_chat_id=msg.chat.id,
                            message_ids=msg.id, **kw
                        )
                        return True, None, False
                    except Exception as fwd_err:
                        logger.warning(
                            f"[MultiJob _send_one] Native forward failed for msg {msg.id}: {fwd_err}. "
                            "Falling back to copy_message."
                        )
                        use_forward_tag = False

                if not use_forward_tag:
                    if is_text_replaced and not msg.media:
                        if not new_text or not new_text.strip():
                            return True, None, False  # silently skip empty text
                        await client.send_message(chat_id=chat, text=new_text, **kw)
                    else:
                        await client.copy_message(
                            chat_id=chat, from_chat_id=msg.chat.id,
                            message_id=msg.id, **kw
                        )
                return True, None, False  # success
            except FloodWait as fw:
                # Respect Telegram's rate limit — wait and retry
                logger.warning(f"[MultiJob _send_one] FloodWait {fw.value}s to {chat}")
                await asyncio.sleep(fw.value + 2)
                continue
            except Exception as exc:
                err = str(exc).upper()
                if any(x in err for x in ["PEER_ID_INVALID", "CHAT_WRITE_FORBIDDEN", "USER_BANNED", "CHANNEL_PRIVATE", "CHAT_ADMIN_REQUIRED"]):
                    raise ValueError(f"Fatal Chat Error: {exc}")

                # Refresh on file reference expiry
                if any(x in err for x in ["FILE_REFERENCE", "FILEREF", "MEDIA_EMPTY"]):
                    from plugins.utils import get_fresh_message
                    try:
                        fresh_m = await get_fresh_message(client, msg.chat.id, msg.id)
                        if fresh_m:
                            msg = fresh_m
                            try:
                                await client.copy_message(chat_id=chat, from_chat_id=msg.chat.id, message_id=msg.id, **kw)
                                return True, None, False
                            except Exception:
                                pass
                    except Exception:
                        pass

                if any(x in err for x in ["RESTRICTED", "PROTECTED", "FILE_REFERENCE", "FILEREF", "MEDIA_EMPTY", "FILE_ID_INVALID"]):
                    # Try copy → forward fallback once for protected content
                    try:
                        await client.forward_messages(chat_id=chat, from_chat_id=msg.chat.id, message_ids=msg.id, **kw)
                        return True, None, False
                    except Exception:
                        pass
                    
                    # --- Fallback to Download/Re-upload for restricted sources ---
                    try:
                        media_obj = getattr(msg, msg.media.value, None) if msg.media else None
                        original_name = getattr(media_obj, 'file_name', None) if media_obj else None
                        if msg.media:
                            import os
                            os.makedirs("downloads", exist_ok=True)
                            safe_name = f"downloads/{msg.id}_{original_name}" if original_name else f"downloads/{msg.id}"
                            fp = None
                            for _dl_try in range(3):
                                try:
                                    if os.path.exists(safe_name):
                                        try: os.remove(safe_name)
                                        except: pass
                                    fp = await client.download_media(msg, file_name=safe_name)
                                    if fp and os.path.exists(str(fp)) and os.path.getsize(str(fp)) > 0: 
                                        await db.update_global_stats(total_files_downloaded=1, total_data_usage_bytes=os.path.getsize(str(fp)))
                                        break
                                    else:
                                        if fp and os.path.exists(str(fp)):
                                            try: os.remove(str(fp))
                                            except: pass
                                        fp = None
                                        logger.warning(f"[MultiJob _send_one] Download attempt {_dl_try + 1}/3 produced 0 B for msg {msg.id}. Retrying...")
                                        await asyncio.sleep(2)
                                except FloodWait as fw:
                                    logger.warning(f"[MultiJob _send_one] Download FloodWait {fw.value}s for msg {msg.id}")
                                    await asyncio.sleep(fw.value + 2)
                                    try: client = await _mj_ensure_client_alive(client)
                                    except Exception: pass
                                except Exception as dl_e:
                                    err_dl = str(dl_e).upper()
                                    if "FILE_REFERENCE" in err_dl or "FILEREF" in err_dl:
                                        from plugins.utils import get_fresh_message
                                        try:
                                            fresh_m = await get_fresh_message(client, msg.chat.id, msg.id)
                                            if fresh_m:
                                                msg = fresh_m
                                        except Exception: pass
                                    if any(x in err_dl for x in ("FILE_ID_INVALID", "MSG_ID_INVALID", "MEDIA_EMPTY")):
                                        logger.warning(f"[MultiJob _send_one] Permanent download error for msg {msg.id}: {dl_e}")
                                        return False, str(dl_e), True
                                    
                                    logger.warning(f"[MultiJob _send_one] Transient download error for msg {msg.id} (attempt {_dl_try + 1}/3): {dl_e}. Retrying in 2s...")
                                    await asyncio.sleep(2)
                                    continue

                            if not fp or not os.path.exists(str(fp)) or os.path.getsize(str(fp)) == 0:
                                if fp and os.path.exists(str(fp)):
                                    try: os.remove(str(fp))
                                    except: pass
                                logger.warning(f"[MultiJob _send_one] Msg {msg.id}: download failed (media expired, deleted, or 0 B) — skipping.")
                                return False, "MediaExpiredOrEmpty", True
                            
                            up_kw = {"chat_id": chat, "caption": kw.get("caption", msg.caption or "")}
                            if thread: up_kw["message_thread_id"] = thread
                            
                            uploaded = False
                            for _ul_try in range(3):
                                try:
                                    if msg.photo:      await client.send_photo(photo=fp, **up_kw)
                                    elif msg.video:    await client.send_video(video=fp, file_name=original_name, **up_kw)
                                    elif msg.document: await client.send_document(document=fp, file_name=original_name, **up_kw)
                                    elif msg.audio:    await client.send_audio(audio=fp, file_name=original_name, **up_kw)
                                    elif msg.voice:    await client.send_voice(voice=fp, **up_kw)
                                    elif msg.animation: await client.send_animation(animation=fp, **up_kw)
                                    elif msg.sticker:  await client.send_sticker(sticker=fp, **up_kw)
                                    uploaded = True
                                    break
                                except FloodWait as fw:
                                    logger.warning(f"[MultiJob _send_one] Upload FloodWait {fw.value}s to {chat}")
                                    await asyncio.sleep(fw.value + 2)
                                    try: client = await _mj_ensure_client_alive(client)
                                    except Exception: pass
                                except Exception as ul_e:
                                    err_ul = str(ul_e).upper()
                                    if any(x in err_ul for x in ("FILE_REFERENCE_EXPIRED", "FILE_ID_INVALID", "MSG_ID_INVALID", "MEDIA_EMPTY", "FILE SIZE EQUALS TO 0")):
                                        logger.warning(f"[MultiJob _send_one] Permanent upload error for msg {msg.id}: {ul_e}")
                                        return False, str(ul_e), True
                                    
                                    logger.warning(f"[MultiJob _send_one] Transient upload error for msg {msg.id} (attempt {_ul_try + 1}/3): {ul_e}. Retrying in 2s...")
                                    await asyncio.sleep(2)
                                    continue
                                    
                            if not uploaded:
                                raise Exception("UploadFailed")
                            
                            import os
                            await db.update_global_stats(total_files_uploaded=1, total_data_usage_bytes=os.path.getsize(str(fp)) if fp and os.path.exists(str(fp)) else 0)
                            if os.path.exists(fp): os.remove(fp)
                            return True, None, False
                        else:
                            await client.send_message(chat_id=chat, text=new_text if new_text is not None else getattr(msg.text, "html", str(msg.text)) if msg.text else "", **kw)
                        return True, None, False
                    except Exception as fallback_e:
                        fb_err = str(fallback_e).upper()
                        if any(x in fb_err for x in ("FILE_REFERENCE_EXPIRED", "FILE_ID_INVALID", "MSG_ID_INVALID", "MEDIA_EMPTY")):
                            logger.warning(f"[MultiJob _send_one] Permanent error in fallback for msg {msg.id}: {fallback_e}")
                            return False, str(fallback_e), True
                        logger.debug(f"[MultiJob _send_one] Fallback failed to {chat}: {fallback_e}")
                        return False, str(fallback_e), False

                # If transient, try to heal before retrying
                is_transient = any(k in err for k in ("TIMEOUT", "CONNECTION", "BROKEN PIPE", "ERRNO 32", "READ", "RESET", "NOT BEEN STARTED", "DISCONNECTED", "NOT CONNECTED", "PING", "FLOOD"))
                
                # For transient errors, retry up to 5 attempts (not 30, which wasted 39 minutes!)
                if _send_attempt >= 4 or not is_transient:
                    logger.warning(f"[MultiJob _send_one] Forward failed for msg {msg.id} to {chat}: {exc}")
                    if is_transient and _send_attempt >= 4:
                        raise ConnectionError(f"Transient error persisted: {exc}")
                    return False, str(exc), False
                await asyncio.sleep(3)
                try: client = await _mj_ensure_client_alive(client)
                except Exception: pass
                continue

    success1, err1, skip1 = await _send_one(to_chat, thread_id)
    success2, err2, skip2 = False, None, False
    if to_chat_2:
        success2, err2, skip2 = await _send_one(to_chat_2, thread_id_2)
    if to_chat_2:
        return (success1 and success2), (err1 or err2), (skip1 or skip2)
    else:
        return success1, err1, skip1


# ══════════════════════════════════════════════════════════════════════════════
# Core batch runner
# ══════════════════════════════════════════════════════════════════════════════

BATCH_SIZE = 200


async def _run_multijob(job_id: str, user_id: int, bot=None):
    job = await _mj_get(job_id)
    if not job:
        return

    if job_id not in _mj_paused:
        ev = asyncio.Event()
        ev.set()
        _mj_paused[job_id] = ev
    pause_ev = _mj_paused[job_id]

    client = None
    _mj_queue_acquired = False
    try:
        acc = await db.get_bot(user_id, job["account_id"])
        if not acc:
            await _mj_update(job_id, status="error", error="Account not found")
            return

        is_bot  = acc.get("is_bot", True)

        is_force = job.get("force_active", False)
        if is_force:
            await _mj_update(job_id, force_active=False)
            pos = 0
        else:
            # ── Queue system: limit concurrent Multi Jobs ─────────────────────────
            pos = await AryaJobQueue.acquire(job_id, "multijob")
            _mj_queue_acquired = True
            
        if pos > 0 and bot:
            try:
                await bot.send_message(user_id,
                    f"⏳ <b>Multi Job Queued</b>\n\n"
                    f"All {AryaJobQueue.max_slots('multijob')} Multi Job slots are busy.\n"
                    f"Job <code>[{job_id[-6:]}]</code> will start automatically at position #{pos}.\n"
                    f"You can close Telegram; it runs in the background.")
            except Exception: pass
            # Now we hold the slot — notify start
            try:
                await bot.send_message(user_id,
                    f"▶️ Multi Job <code>[{job_id[-6:]}]</code> now has a slot and is starting.")
            except Exception: pass

        # ─── Initial client check ─────────────────────────
        client = await start_clone_bot(_CLIENT.client(acc))
        client = await _mj_ensure_client_alive(client)

        from_chat   = job["from_chat"]
        to_chat     = job["to_chat"]
        to_thread   = job.get("to_thread_id")
        to_chat_2   = job.get("to_chat_2")
        to_thread_2 = job.get("to_thread_id_2")
        
        for _att in range(3):
            try:
                me = await client.get_me()
                break
            except Exception as e:
                if _att == 2: raise e
                await __import__('asyncio').sleep(5)
                
        if str(from_chat).lower() in [x.lower() for x in (str(me.id), me.username, "me", "saved") if x]:
            from_chat = user_id
            await _mj_update(job_id, from_chat=from_chat)

        if str(to_chat).lower() in [x.lower() for x in (str(me.id), me.username, "me", "saved") if x]:
            to_chat = user_id
            await _mj_update(job_id, to_chat=to_chat)

        if to_chat_2 and str(to_chat_2).lower() in [x.lower() for x in (str(me.id), me.username, "me", "saved") if x]:
            to_chat_2 = user_id
            await _mj_update(job_id, to_chat_2=to_chat_2)

        # ── Protected Chat Guard ───────────────────────────────────────────────
        from plugins.utils import check_chat_protection
        prot_err = await check_chat_protection(job["user_id"], from_chat)
        if prot_err:
            await _mj_update(job_id, status="error", error=prot_err)
            try:
                await bot.send_message(job["user_id"], prot_err)
            except Exception:
                pass
            return
        # ──────────────────────────────────────────────────────────────────────

        end_id      = int(job.get("end_id") or 0)
        current     = int(job.get("current_id") or job.get("start_id") or 1)
        processed_ids = job.get("processed_ids") or []
        await _mj_update(job_id, status="running", error="")
        logger.info(f"[MultiJob {job_id}] Started. current={current} end={end_id}")

        # ── Resolve exact source chat type & detect if source is a DM/bot chat ──
        # CRITICAL: DM/bot sources must use get_chat_history, NOT get_messages.
        # get_messages on a user/bot ID from a bot client fetches from global inbox = saved messages.
        is_dm_source = False
        from pyrogram.enums import ChatType
        from plugins.utils import safe_resolve_peer

        try:
            await safe_resolve_peer(client, from_chat, bot=bot)
            try:
                peer_chat = await client.get_chat(from_chat)
            except Exception:
                peer_chat = await bot.get_chat(from_chat)

            if peer_chat.type in (ChatType.PRIVATE, ChatType.BOT):
                is_dm_source = True
            from_chat = peer_chat.id  # Lock in numeric ID
        except Exception as warn_e:
            logger.warning(f"[MultiJob {job_id}] Source resolve warning: {warn_e}")
            if isinstance(from_chat, int) and from_chat > 0:
                is_dm_source = True
            elif isinstance(from_chat, str) and from_chat.lower() in ("me", "saved"):
                is_dm_source = True

        # Safety: normal bot accounts cannot read DM message history.
        # If source is DM/bot and forwarding account is a normal bot, abort early.
        if is_dm_source and is_bot:
            await _mj_update(job_id, status="error",
                              error="Bot accounts cannot read DM history. Use a Userbot for DM sources.")
            try:
                from bot import BOT_INSTANCE
                await BOT_INSTANCE.send_message(user_id,
                    f"⚠️ <b>Multi Job Error</b>\n\n"
                    f"Source <b>{job.get('from_title', str(from_chat))}</b> is a Bot/User DM, "
                    f"but the selected account is a <b>Normal Bot</b>.\n\n"
                    f"Normal bots <b>cannot read message history</b> from DMs.\n"
                    f"Please recreate this job using a <b>Userbot</b> account.")
            except Exception: pass
            return

        try:
            for _wchat in [to_chat] + ([to_chat_2] if to_chat_2 else []):
                await safe_resolve_peer(client, _wchat, bot=bot)
        except Exception:
            pass

        consecutive_empty = 0
        batch_cycle = 0  # counter to refresh configs periodically
        
        # Load user settings once (refresh every 20 batches to pick up changes)
        disabled_types = await db.get_filters(user_id)
        configs        = await db.get_configs(user_id)
        filters_dict   = configs.get('filters', {})
        remove_caption = filters_dict.get('rm_caption', False)
        cap_tpl        = configs.get('caption')
        forward_tag    = configs.get('forward_tag', False)
        sleep_secs     = max(0.0, float(configs.get('duration', 0.0) or 0.0))
        replacements   = configs.get('replacements', {})

        #  Destination progress bar 
        acc_name = acc.get('name', 'Userbot')

        def _mj_prog_text(fwd: int, total: int, status: str = "running") -> str:
            if status == "done":
                return (
                    f"➤ <b>✓ ɩᴜʟᴛɪ ᴊᴏʙ ᴄᴏᴍᴘʟᴇᴛᴇ!</b>\n"
                    f"➤ <b>ᴀᴄᴄᴏᴜɴᴛ:</b> <code>{acc_name}</code>\n\n"
                    f"➤ ᴀʟʟ <u>{fwd}</u> ғɪʟᴇѕ ʜᴀᴠᴇ ʙᴇᴇɴ ᴍᴏᴠᴇᴅ ѕᴜᴄᴄᴇѕѕғᴜʟʟʟʸ!\n\n"
                    f"<i>ᴘᴏᴡᴇʀᴇᴅ ʙʸ ᴀʀʸᴀ ғᴏʀᴡᴀʀᴅ ʙᴏᴛ</i>"
                )
            elif status == "stopped":
                return (
                    f"➤ <b>⏹ ᴊᴏʙ ѕᴛᴏᴘᴘᴇᴅ</b>\n"
                    f"➤ <b>ᴀᴄᴄᴏᴜɴᴛ:</b> <code>{acc_name}</code>\n\n"
                    f"➤ ғɪʟᴇѕ ѕᴇɴᴛ: <code>{fwd}</code> / <code>{total if total else '?'}</code>\n\n"
                    f"<i>ᴘᴏᴡᴇʀᴇᴅ ʙʸ ᴀʀʸᴀ ғᴏʀᴡᴀʀᴅ ʙᴏᴛ</i>"
                )
            elif status == "error":
                return (
                    f"➤ <b>⚠️ ᴊᴏʙ ᴇʀʀᴏʀ</b>\n"
                    f"➤ <b>ᴀᴄᴄᴏᴜɴᴛ:</b> <code>{acc_name}</code>\n\n"
                    f"➤ ғɪʟᴇѕ ѕᴇɴᴛ ʙᴇғᴏʀᴇ ᴇʀʀᴏʀ: <code>{fwd}</code>\n\n"
                    f"<i>ᴘᴏᴡᴇʀᴇᴅ ʙʸ ᴀʀʸᴀ ғᴏʀᴡᴀʀᴅ ʙᴏᴛ</i>"
                )
            else:
                total_str = str(total) if total else '?'
                return (
                    f"<b>➤ {acc_name}</b>\n"
                    f"➤ ᴛʀаɴѕғᴇʀʀɪɴɢ ғɪʟᴇѕ ᴘʟᴇᴀѕᴇ ᴡᴀɪᴛ...\n\n"
                    f"➤ <b>ғɪʟᴇѕ ѕᴇɴᴛ:</b> <code>{fwd}</code> / <code>{total_str}</code>\n\n"
                    f"<i>ᴘᴏᴡᴇʀᴇᴅ ʙʸ ᴀʀʸᴀ ғᴏʀᴡᴀʀᴅ ʙᴏᴛ</i>"
                )

        mj_total = max(0, end_id - int(job.get("start_id") or 1)) if end_id > 0 else 0
        mj_prog_msg_id = job.get("prog_msg_id", None)
        if not mj_prog_msg_id:
            try:
                sent = await client.send_message(to_chat, _mj_prog_text(0, mj_total), parse_mode=__import__("pyrogram.enums", fromlist=["ParseMode"]).ParseMode.HTML)
                mj_prog_msg_id = sent.id
                await _mj_update(job_id, prog_msg_id=mj_prog_msg_id)
                try: await client.pin_chat_message(to_chat, mj_prog_msg_id, disable_notification=True)
                except Exception: pass
            except Exception:
                mj_prog_msg_id = None

        mj_last_prog_update = 0.0
        mj_start_time = time.time()
        mj_fwd_at_start = int(job.get("forwarded", 0))

        # \u2500\u2500 BOT DM BATCH (userbot + non-channel source) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        # get_messages(id_list) without a channel peer queries the GLOBAL inbox.
        if not is_bot and is_dm_source:
            logger.info(f"[MultiJob {job_id}] DM source — collecting via get_chat_history")
            # CRITICAL Resume FIX: Use current, not start_id, otherwise we duplicate or fail offset!
            start_id_val = current
            dm_msgs = []
            try:
                # Ensure client is alive before huge history fetch
                client = await _mj_ensure_client_alive(client)
                async for m in client.get_chat_history(from_chat):
                    if m.empty or m.service:
                        continue
                    if m.id < start_id_val:
                        break
                    if end_id > 0 and m.id > end_id:
                        continue
                    dm_msgs.append(m)
            except Exception as e:
                logger.warning(f"[MultiJob {job_id}] DM collect error: {e}")

            if job.get("smart_order", True):
                processed_ids = job.get("processed_ids") or []
                dm_msgs = [m for m in dm_msgs if m.id not in processed_ids and m.id >= current]
                from plugins.utils import get_natural_sort_key
                dm_msgs.sort(key=get_natural_sort_key)
                logger.info(f"[MultiJob {job_id}] DM batch: {len(dm_msgs)} msgs collected and sorted naturally.")
            else:
                dm_msgs = [m for m in dm_msgs if m.id >= current]
                dm_msgs.sort(key=lambda m: m.id)
                logger.info(f"[MultiJob {job_id}] DM batch: {len(dm_msgs)} msgs collected. Smart order is disabled; processing in raw order.")

            processed_ids = job.get("processed_ids") or []
            all_ids = [m.id for m in dm_msgs]

            async def mark_msg_processed(msg_id, is_checkpoint=False):
                nonlocal processed_ids, current
                upd = {}
                if job.get("smart_order", True):
                    remaining_ids = [mid for mid in all_ids if mid not in processed_ids]
                    if is_checkpoint:
                        current = min(remaining_ids) if remaining_ids else (end_id or msg_id)
                    else:
                        if msg_id not in processed_ids:
                            processed_ids.append(msg_id)
                        remaining_ids = [mid for mid in all_ids if mid not in processed_ids]
                        current = min(remaining_ids) if remaining_ids else (end_id + 1 if end_id > 0 else msg_id + 1)
                        upd["processed_ids"] = processed_ids
                else:
                    if is_checkpoint:
                        current = msg_id
                    else:
                        current = msg_id + 1
                upd["current_id"] = current
                await _mj_update(job_id, **upd)

            for idx, msg in enumerate(dm_msgs):
                await pause_ev.wait()
                fresh2 = await _mj_get(job_id)
                if not fresh2 or fresh2.get("status") in ("stopped",):
                    return

                if not _passes_filters(msg, disabled_types):
                    await mark_msg_processed(msg.id)
                    continue
                _remove_links = 'links' in disabled_types

                # CHECKPOINT: record we're AT this message before forwarding
                await mark_msg_processed(msg.id, is_checkpoint=True)

                client = await _mj_ensure_client_alive(client)

                # Preemptively fetch fresh message reference if job has been running > 5m or idx >= 20
                if msg.media and ((time.time() - mj_start_time) > 300 or idx >= 20):
                    from plugins.utils import get_fresh_message
                    try:
                        fresh_m = await get_fresh_message(client, from_chat, msg.id)
                        if fresh_m:
                            msg = fresh_m
                    except Exception:
                        pass

                success = False
                err = None
                skip = False
                try:
                    success, err, skip = await _mj_forward(client, msg, to_chat, remove_caption, cap_tpl, forward_tag,
                                       to_thread, to_chat_2, to_thread_2, replacements, _remove_links)
                except FloodWait as fw:
                    logger.warning(f"[MultiJob {job_id}] DM loop FloodWait {fw.value}s for msg {msg.id}")
                    await asyncio.sleep(fw.value + 2)
                    try: client = await _mj_ensure_client_alive(client)
                    except Exception: pass
                    try:
                        success, err, skip = await _mj_forward(client, msg, to_chat, remove_caption, cap_tpl, forward_tag,
                                           to_thread, to_chat_2, to_thread_2, replacements, _remove_links)
                    except Exception as fw_e:
                        success = False
                        err = str(fw_e)
                except Exception as dm_fwd_err:
                    logger.warning(f"[MultiJob {job_id}] DM forward exception for msg {msg.id}: {dm_fwd_err}")
                    success = False
                    err = str(dm_fwd_err)
                
                await mark_msg_processed(msg.id)
                if success:
                    await _mj_inc(job_id, 1)
                else:
                    logger.warning(f"[MultiJob {job_id}] DM: Forward of msg {msg.id} failed ({err}) — advancing past")

                now_mj = time.time()
                if mj_prog_msg_id and (now_mj - mj_last_prog_update) >= 10:
                    mj_last_prog_update = now_mj
                    try:
                        fresh_j = await _mj_get(job_id)
                        _fwd = fresh_j.get("forwarded", 0) if fresh_j else 0
                        from pyrogram.enums import ParseMode
                        await client.edit_message_text(to_chat, mj_prog_msg_id, _mj_prog_text(_fwd, mj_total, "running"), parse_mode=ParseMode.HTML)
                    except Exception: pass

                if sleep_secs > 0:
                    await asyncio.sleep(sleep_secs)

            await _mj_update(job_id, status="done", current_id=current, processed_ids=[])
            fj = await _mj_get(job_id)
            _fwd = fj.get("forwarded", 0) if fj else len(dm_msgs)
            if client and mj_prog_msg_id:
                try:
                    from pyrogram.enums import ParseMode
                    await client.edit_message_text(to_chat, mj_prog_msg_id, _mj_prog_text(_fwd, mj_total, "done"), parse_mode=ParseMode.HTML)
                    await client.unpin_chat_message(to_chat, mj_prog_msg_id)
                except Exception: pass
            if bot and fj:
                try:
                    await bot.send_message(user_id,
                        f"<b>\u2705 Multi Job Complete!</b>\n\n"
                        f"<b>Name:</b> {fj.get('name', job_id[-6:])}\n"
                        f"<b>Source:</b> {fj.get('from_title','?')}\n"
                        f"<b>Dest:</b> {fj.get('to_title','?')}\n"
                        f"<b>Forwarded:</b> {_fwd} messages")
                except Exception: pass
            return

        # \u2500\u2500 CHANNEL/GROUP: original ID-range loop \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

        while True:
            # Pause check
            await pause_ev.wait()

            # Stop check
            fresh = await _mj_get(job_id)
            if not fresh or fresh.get("status") in ("stopped", "error"):
                # Finalize progress bar on external stop
                if client and mj_prog_msg_id:
                    try:
                        fj = await _mj_get(job_id)
                        _fwd = fj.get("forwarded", 0) if fj else 0
                        await client.edit_message_text(to_chat, mj_prog_msg_id, _mj_prog_text(_fwd, mj_total, "stopped"), parse_mode=__import__("pyrogram.enums", fromlist=["ParseMode"]).ParseMode.HTML)
                    except Exception: pass
                break

            # End check
            if end_id > 0 and current > end_id:
                await _mj_update(job_id, status="done", current_id=current)
                logger.info(f"[MultiJob {job_id}] Done — reached end_id {end_id}")
                # Finalize destination progress bar
                if client and mj_prog_msg_id:
                    try:
                        fj = await _mj_get(job_id)
                        _fwd = fj.get("forwarded", 0) if fj else 0
                        await client.edit_message_text(to_chat, mj_prog_msg_id, _mj_prog_text(_fwd, mj_total, "done"), parse_mode=__import__("pyrogram.enums", fromlist=["ParseMode"]).ParseMode.HTML)
                        await client.unpin_chat_message(to_chat, mj_prog_msg_id)
                        async def _del_done_prog():
                            await asyncio.sleep(300)
                            try: await client.delete_messages(to_chat, mj_prog_msg_id)
                            except Exception: pass
                        asyncio.create_task(_del_done_prog())
                    except Exception: pass
                # Send completion report
                done_job = await _mj_get(job_id)
                if done_job:
                    try:
                        await bot.send_message(
                            user_id,
                            f"<b>✅ Multi Job Complete!</b>\n\n"
                            f"<b>Name:</b> {done_job.get('name', job_id[-6:])}\n"
                            f"<b>Source:</b> {done_job.get('from_title','?')}\n"
                            f"<b>Dest:</b> {done_job.get('to_title','?')}\n"
                            f"<b>Forwarded:</b> {done_job.get('forwarded', 0)} messages\n"
                            f"<b>Range:</b> {done_job.get('start_id',1)} → {end_id}\n\n"
                            f"<i>Use /multijob to manage jobs.</i>"
                        )
                    except Exception:
                        pass
                break

            # Refresh configs every 20 batches
            batch_cycle += 1
            if batch_cycle % 20 == 1:
                disabled_types = await db.get_filters(user_id)
                configs        = await db.get_configs(user_id)
                filters_dict   = configs.get('filters', {})
                remove_caption = filters_dict.get('rm_caption', False)
                cap_tpl        = configs.get('caption')
                forward_tag    = configs.get('forward_tag', False)
                sleep_secs     = max(0.0, float(configs.get('duration', 0.0) or 0.0))
                replacements   = configs.get('replacements', {})

            # Build batch
            batch_end = current + BATCH_SIZE - 1
            if end_id > 0:
                batch_end = min(batch_end, end_id)
            batch_ids = list(range(current, batch_end + 1))

            # Fetch messages
            msgs = []
            from_thread = job.get("from_thread")
            use_get_messages = False
            if from_thread and int(from_thread) > 0:
                use_get_messages = True

            try:
                if use_get_messages:
                    # Fetch by exact IDs to prevent mixed-topic pagination skipped messages
                    msgs = await client.get_messages(from_chat, batch_ids)
                    if not isinstance(msgs, list):
                        msgs = [msgs]
                else:
                    # 1. Prefer get_chat_history (robust, does not return fake empty messages under rate limit)
                    # offset_id = batch_end + 1 retrieves messages with ID <= batch_end downwards.
                    batch_hist = []
                    async for m in client.get_chat_history(from_chat, limit=BATCH_SIZE, offset_id=batch_end + 1):
                        if m.id < current:
                            # Since get_chat_history goes backwards, once we see ID < current, we can stop fetching
                            break
                        batch_hist.append(m)
                    # Reverse to make it chronological (current -> batch_end)
                    msgs = list(reversed(batch_hist))
            except Exception as hist_err:
                logger.warning(f"[MultiJob {job_id}] get_chat_history failed: {hist_err}. Falling back to get_messages.")
                # 2. Fallback to get_messages by specific IDs
                try:
                    msgs = await client.get_messages(from_chat, batch_ids)
                    if not isinstance(msgs, list):
                        msgs = [msgs]
                except FloodWait as fw:
                    await asyncio.sleep(fw.value + 2)
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    err_str = str(e).upper()
                    if any(x in err_str for x in ["PEER_ID_INVALID", "CHANNEL_INVALID", "USERNAME_INVALID", "CHAT_ID_INVALID"]):
                        logger.error(f"[MultiJob {job_id}] Fatal Source Error: {e}")
                        await _mj_update(job_id, status="error", error=f"Source Invalid: {e}")
                        break
                        
                    is_transient = any(k in err_str for k in (
                        "TIMEOUT", "CONNECTION", "BROKEN PIPE", "ERRNO 32", "READ", "RESET", "DISCONNECT",
                        "NOT BEEN STARTED", "NOT CONNECTED", "CLOSED DATABASE",
                        "NETWORK", "SOCKET", "PING", "MULTIJOB_RECONNECT_FAILED"
                    ))
                    if is_transient:
                        logger.warning(f"[MultiJob {job_id}] Transient fetch error at {current}: {e}. Healing client...")
                        try:
                            client = await _mj_ensure_client_alive(client)
                        except Exception: pass
                        await asyncio.sleep(10)
                        continue

                    # Unknown / non-transient API error — do NOT skip hundreds of IDs.
                    logger.warning(f"[MultiJob {job_id}] Unknown fetch error at {current}: {e} — retrying batch in 30s")
                    await asyncio.sleep(30)
                    continue

            valid = [m for m in msgs if m and not m.empty]
            if job.get("smart_order", True):
                valid = [m for m in valid if m.id not in processed_ids]
            
            if job.get("smart_order", True):
                from plugins.utils import get_natural_sort_key
                valid.sort(key=get_natural_sort_key)
            else:
                valid.sort(key=lambda m: m.id)
            
            # Cross-chat filter: verify messages belong to the expected source chat.
            # Apply to ALL integer IDs to prevent the global inbox from leaking in.
            filtered = []
            for m in valid:
                if isinstance(from_chat, int):
                    if m.chat is None: continue
                    if m.chat.id != from_chat: continue
                # String usernames: Pyrogram resolves peer correctly
                filtered.append(m)
            valid = filtered

            if not valid:
                consecutive_empty += 1
                if consecutive_empty >= 200:  # 200 * 200 IDs = 40,000 gaps before giving up
                    await _mj_update(job_id, status="done", current_id=current, processed_ids=[])
                    logger.info(f"[MultiJob {job_id}] Done — no more messages after {current}")
                    # Finalize destination progress bar
                    if client and mj_prog_msg_id:
                        try:
                            fj = await _mj_get(job_id)
                            _fwd = fj.get("forwarded", 0) if fj else 0
                            await client.edit_message_text(to_chat, mj_prog_msg_id, _mj_prog_text(_fwd, mj_total, "done"), parse_mode=__import__("pyrogram.enums", fromlist=["ParseMode"]).ParseMode.HTML)
                            await client.unpin_chat_message(to_chat, mj_prog_msg_id)
                            async def _del_empty_prog():
                                await asyncio.sleep(300)
                                try: await client.delete_messages(to_chat, mj_prog_msg_id)
                                except Exception: pass
                            asyncio.create_task(_del_empty_prog())
                        except Exception: pass
                    # Send completion report
                    done_job2 = await _mj_get(job_id)
                    if done_job2:
                        try:
                            await bot.send_message(
                                user_id,
                                f"<b>✅ Multi Job Complete!</b>\n\n"
                                f"<b>Name:</b> {done_job2.get('name', job_id[-6:])}\n"
                                f"<b>Source:</b> {done_job2.get('from_title','?')}\n"
                                f"<b>Dest:</b> {done_job2.get('to_title','?')}\n"
                                f"<b>Forwarded:</b> {done_job2.get('forwarded', 0)} messages\n\n"
                                f"<i>No more messages found after ID {current}.</i>"
                            )
                        except Exception:
                            pass
                    break
                current += BATCH_SIZE
                processed_ids = []
                await _mj_update(job_id, current_id=current, consecutive_empty=consecutive_empty, processed_ids=[])
                await asyncio.sleep(2)
                continue

            consecutive_empty = 0

            from_thread = job.get("from_thread")
            if from_thread and int(from_thread) > 0:
                from_thread = int(from_thread)
                valid = [m for m in valid if _msg_in_topic(m, from_thread)]

            all_ids = [m.id for m in valid]

            async def mark_msg_processed(msg_id, is_checkpoint=False):
                nonlocal processed_ids, current
                upd = {}
                if job.get("smart_order", True):
                    remaining_ids = [mid for mid in all_ids if mid not in processed_ids]
                    if is_checkpoint:
                        current = min(remaining_ids) if remaining_ids else (batch_end or msg_id)
                    else:
                        if msg_id not in processed_ids:
                            processed_ids.append(msg_id)
                        remaining_ids = [mid for mid in all_ids if mid not in processed_ids]
                        current = min(remaining_ids) if remaining_ids else (batch_end + 1)
                        upd["processed_ids"] = processed_ids
                else:
                    if is_checkpoint:
                        current = msg_id
                    else:
                        current = msg_id + 1
                upd["current_id"] = current
                await _mj_update(job_id, **upd)

            # Forward each valid message
            for idx, msg in enumerate(valid):
                await pause_ev.wait()

                fresh2 = await _mj_get(job_id)
                if not fresh2 or fresh2.get("status") in ("stopped",):
                    return

                if not _passes_filters(msg, disabled_types):
                    await mark_msg_processed(msg.id)
                    continue

                # ── CHECKPOINT before forwarding ──────────────────────────────────
                await mark_msg_processed(msg.id, is_checkpoint=True)
                # ─────────────────────────────────────────────────────────────────

                # Heal client connection before forward
                client = await _mj_ensure_client_alive(client)

                _remove_links = 'links' in disabled_types
                success, err, skipped = await _mj_forward(client, msg, to_chat, remove_caption, cap_tpl, forward_tag,
                                   to_thread, to_chat_2, to_thread_2, replacements, _remove_links)

                if success or skipped:
                    # Advance cursor past this message only if it succeeded or was skipped due to a permanent error
                    await mark_msg_processed(msg.id)
                    if success:
                        await _mj_inc(job_id, 1)
                    else:
                        logger.warning(f"[MultiJob {job_id}] Skipped msg {msg.id} due to permanent error: {err}")
                else:
                    # Check if the error is connection/network transient.
                    # If so, raise an exception to trigger client healing / auto-resume.
                    # Otherwise, it is a message-specific error (like download/upload/expired media),
                    # so we log a warning, advance the cursor, and continue.
                    err_upper = str(err).upper()
                    _CONN_TRANSIENT = (
                        "CONNECTION", "TIMEOUT", "SOCKET", "RESET", "DISCONNECT", "NOT CONNECTED", "FLOOD",
                        "BROKEN PIPE", "ERRNO 32", "READ", "PING", "NOT BEEN STARTED", "DISCONNECTED"
                    )
                    if any(kw in err_upper for kw in _CONN_TRANSIENT):
                        raise Exception(f"Transient connection error for msg {msg.id}: {err}")
                    else:
                        logger.warning(f"[MultiJob {job_id}] Skipping msg {msg.id} due to copy error: {err}")
                        await mark_msg_processed(msg.id)

                if sleep_secs > 0:
                    await asyncio.sleep(sleep_secs)

            # Advance cursor — guard against valid being empty after topic-filter
            if valid:
                if job.get("smart_order", True):
                    current = batch_end + 1
                    processed_ids = []
                else:
                    current = valid[-1].id + 1
            else:
                current += BATCH_SIZE  # skip the batch that had no topic-matching msgs
                processed_ids = []
            await _mj_update(job_id, current_id=current, processed_ids=processed_ids)

            #  Update destination progress bar (every 10s) 
            now_mj = time.time()
            if mj_prog_msg_id and (now_mj - mj_last_prog_update) >= 10:
                mj_last_prog_update = now_mj
                try:
                    fresh_j = await _mj_get(job_id)
                    _fwd = fresh_j.get("forwarded", 0) if fresh_j else 0
                    from pyrogram.enums import ParseMode
                    await client.edit_message_text(to_chat, mj_prog_msg_id, _mj_prog_text(_fwd, mj_total, "running"), parse_mode=ParseMode.HTML)
                except Exception:
                    pass

    except asyncio.CancelledError:
        logger.info(f"[MultiJob {job_id}] Cancelled")
        await _mj_update(job_id, status="stopped")
        if client and mj_prog_msg_id:
            try:
                fj = await _mj_get(job_id)
                _fwd = fj.get("forwarded", 0) if fj else 0
                await client.edit_message_text(to_chat, mj_prog_msg_id, _mj_prog_text(_fwd, mj_total, "stopped"), parse_mode=__import__("pyrogram.enums", fromlist=["ParseMode"]).ParseMode.HTML)
                await client.unpin_chat_message(to_chat, mj_prog_msg_id)
                async def _del_cancelled_prog():
                    await asyncio.sleep(180)
                    try: await client.delete_messages(to_chat, mj_prog_msg_id)
                    except Exception: pass
                asyncio.create_task(_del_cancelled_prog())
            except Exception: pass
    except Exception as e:
        err_str = str(e)
        err_upper = err_str.upper()
        _MJ_TRANSIENT = (
            "CONNECTION", "BROKEN PIPE", "ERRNO 32", "TIMEOUT", "NETWORK", "PING", "SOCKET", "RESET",
            "NOT BEEN STARTED", "NOT CONNECTED", "DISCONNECTED",
            "CONNECTION LOST", "CLOSED DATABASE",
            "MULTIJOB_RECONNECT_FAILED",   # raised by _mj_ensure_client_alive
        )
        if any(kw in err_upper for kw in _MJ_TRANSIENT):
            # Transient network / connection error — auto-restart instead of
            # permanently marking the job as error so no files are missed.
            logger.warning(f"[MultiJob {job_id}] Transient outer error: {err_str} — auto-restarting in 30s")
            await _mj_update(job_id, error=f"[Auto-reconnect] {err_str[:80]}")
            # Keep status=running so the UI stays green and the job resumes.
            async def _mj_auto_resume():
                await asyncio.sleep(30)
                _mj_start_task(job_id, user_id, bot=bot)
            asyncio.create_task(_mj_auto_resume())
        elif "AUTH_KEY_DUPLICATED" in err_str:
            logger.warning(f"[MultiJob {job_id}] AUTH_KEY_DUPLICATED — pausing")
            if client:
                client_name = getattr(client, 'name', None)
                if client_name:
                    await force_evict_client(client_name)
                    client = None
            await _mj_update(job_id, status="paused",
                             error="Session conflict (AUTH_KEY_DUPLICATED). Restart the job.")
        else:
            logger.error(f"[MultiJob {job_id}] Fatal: {e}", exc_info=True)
            await _mj_update(job_id, status="error", error=err_str[:120])
        # Only mark progress bar as error for truly fatal failures.
        # For transient/AUTH errors, the job is auto-restarting — keep current bar state.
        is_transient_outer = any(kw in err_upper for kw in _MJ_TRANSIENT)
        if client and mj_prog_msg_id and not is_transient_outer and "AUTH_KEY_DUPLICATED" not in err_str:
            try:
                fj = await _mj_get(job_id)
                _fwd = fj.get("forwarded", 0) if fj else 0
                await client.edit_message_text(to_chat, mj_prog_msg_id, _mj_prog_text(_fwd, mj_total, "error"), parse_mode=__import__("pyrogram.enums", fromlist=["ParseMode"]).ParseMode.HTML)
            except Exception: pass

    finally:
        _mj_tasks.pop(job_id, None)
        _mj_paused.pop(job_id, None)
        if _mj_queue_acquired:
            AryaJobQueue.release(job_id, "multijob")
        if client:
            from plugins.test import release_client
            client_name = getattr(client, 'name', None)
            if client_name:
                await release_client(client_name)
            else:
                pass


def _mj_start_task(job_id: str, user_id: int, bot=None) -> asyncio.Task:
    ev = asyncio.Event()
    ev.set()
    _mj_paused[job_id] = ev
    task = asyncio.create_task(_run_multijob(job_id, user_id, bot=bot))
    _mj_tasks[job_id] = task
    return task


# ══════════════════════════════════════════════════════════════════════════════
# Resume on bot restart
# ══════════════════════════════════════════════════════════════════════════════

async def resume_multi_jobs(user_id: int = None, bot=None, stagger_secs: float = 3.0):
    """
    Resume all 'running' Multi Jobs after bot restart.
    Jobs are started with a `stagger_secs` delay between each to prevent
    simultaneous Telegram API flood and connection errors.
    """
    query = {"status": "running"}
    if user_id:
        query["user_id"] = user_id
    jobs_to_resume = []
    async for job in db.db[COLL].find(query):
        jid = job["job_id"]
        uid = job["user_id"]
        if jid not in _mj_tasks:
            jobs_to_resume.append((jid, uid))

    total = len(jobs_to_resume)
    if total:
        logger.info(f"[MultiJob] Resuming {total} multi-job(s) with {stagger_secs}s stagger...")
    for i, (jid, uid) in enumerate(jobs_to_resume):
        _mj_start_task(jid, uid, bot=bot)
        logger.info(f"[MultiJob] Resumed {i+1}/{total}: {jid} (user {uid})")
        if i < total - 1:
            await asyncio.sleep(stagger_secs)  # stagger to avoid Telegram flood




# ══════════════════════════════════════════════════════════════════════════════
# UI helpers
# ══════════════════════════════════════════════════════════════════════════════

def _mj_emoji(status: str) -> str:
    return {
        "running": '<emoji id="5413643931139219521">🟢</emoji>',
        "paused": '<emoji id="5807622114424924272">⏸</emoji>',
        "stopped": '<emoji id="5413424119007978384">🔴</emoji>',
        "done": '<emoji id="6123181698193559460">✅</emoji>',
        "error": '<emoji id="6030400221232501136">❌</emoji>'
    }.get(status, "⭘")


async def _render_mj_list(bot, user_id: int, msg_or_query):
    jobs  = await _mj_list(user_id)
    is_cb = hasattr(msg_or_query, "message")

    if not jobs:
        text = (
            f'<emoji id="6037622221625626773">📦</emoji> <b>Multi Jobs</b>\n'
            f"────────────────────\n"
            f"<i>No jobs yet.\n\n"
            f"A <b>Multi Job</b> copies a specific range of messages from any "
            f"source channel/group to your target — fully in the background.\n\n"
            f'<emoji id="6123181698193559460">✅</emoji> All source types (public, private, DMs, topics)\n'
            f'<emoji id="6123181698193559460">✅</emoji> Dual destinations\n'
            f'<emoji id="6123181698193559460">✅</emoji> Multiple jobs run simultaneously\n'
            f'<emoji id="6123181698193559460">✅</emoji> Pause / Resume support\n'
            f'<emoji id="6123181698193559460">✅</emoji> Survives bot restarts\n\n'
            f"👇 Create your first Multi Job below!</i>"
        )
        btns = [
            [InlineKeyboardButton("➕ Create Multi Job", callback_data="mj#new")],
            [InlineKeyboardButton("Back", callback_data="back")]
        ]
        api_btns = [
            [{"text": "Create Multi Job", "callback_data": "mj#new", "icon_custom_emoji_id": "5807642902066634351"}],
            [{"text": "Back", "callback_data": "back"}]
        ]
    else:
        active_cnt = len([j for j in jobs if j.get("status") in ("running", "queued")])
        lines = [
            f'<emoji id="6037622221625626773">📦</emoji> <b>Your Multi Jobs</b>\n'
            f"────────────────────\n"
            f'<emoji id="5413643931139219521">🟢</emoji> <b>Active Tasks:-</b> <code>{active_cnt}</code>\n'
            f"────────────────────\n"
        ]
        for j in jobs:
            st   = _mj_emoji(j.get("status", "stopped"))
            fwd  = j.get("forwarded", 0)
            cur  = j.get("current_id", "?")
            end  = j.get("end_id", 0) or "∞"
            start_id = j.get("start_id", 1)
            fetched = cur - start_id if isinstance(cur, int) and isinstance(start_id, int) and cur >= start_id else 0
            err  = f" <code>[{j.get('error','')}]</code>" if j.get("status") == "error" else ""
            d2   = f" + {j.get('to_title_2','?')}" if j.get("to_chat_2") else ""
            default_name = f"Multi Job {j['job_id'][-6:]}"
            name = j.get("name", default_name)
            lines.append(
                f"{st} <b>{name}</b>\n"
                f"  └ <i>{j.get('from_title','?')} → {j.get('to_title','?')}{d2}</i>\n"
                f"  └ <code>[{j['job_id'][-6:]}]</code>  <emoji id=\"6123181698193559460\">✅</emoji>{fwd}  {fetched}  {cur}/{end}{err}\n"
            )
        import datetime
        now_str = datetime.datetime.now().strftime("%I:%M:%S %p")
        text = "\n".join(lines) + f"\n\n<i>Last refreshed: {now_str}</i>"

        btns_list = []
        api_btns_list = []
        for j in jobs:
            st   = j.get("status", "stopped")
            jid  = j["job_id"]
            short = jid[-6:]
            row = []
            api_row = []
            is_queued = False
            if st == "running":
                # MultiJob uses "running" even when waiting in AryaJobQueue
                from plugins.job_queue import AryaJobQueue
                try: 
                    if AryaJobQueue.queue_position(jid, "multijob") > 0:
                        is_queued = True
                except: pass

            if is_queued:
                row.append(InlineKeyboardButton(f"Force [{short}]", callback_data=f"mj#force_ask#{jid}"))
                api_row.append({"text": f"Force [{short}]", "callback_data": f"mj#force_ask#{jid}", "icon_custom_emoji_id": "5264895611517300926"})
                row.append(InlineKeyboardButton(f"Stop [{short}]", callback_data=f"mj#stop#{jid}"))
                api_row.append({"text": f"Stop [{short}]", "callback_data": f"mj#stop#{jid}", "icon_custom_emoji_id": "5807622114424924272"})
            elif st == "running":
                row.append(InlineKeyboardButton(f"Pause [{short}]", callback_data=f"mj#pause#{jid}"))
                api_row.append({"text": f"Pause [{short}]", "callback_data": f"mj#pause#{jid}", "icon_custom_emoji_id": "5807622114424924272"})
                row.append(InlineKeyboardButton(f"Stop [{short}]", callback_data=f"mj#stop#{jid}"))
                api_row.append({"text": f"Stop [{short}]", "callback_data": f"mj#stop#{jid}", "icon_custom_emoji_id": "5807622114424924272"})
            elif st == "paused":
                row.append(InlineKeyboardButton(f"Resume [{short}]", callback_data=f"mj#resume#{jid}"))
                api_row.append({"text": f"Resume [{short}]", "callback_data": f"mj#resume#{jid}", "icon_custom_emoji_id": "5413643931139219521"})
                row.append(InlineKeyboardButton(f"Stop [{short}]", callback_data=f"mj#stop#{jid}"))
                api_row.append({"text": f"Stop [{short}]", "callback_data": f"mj#stop#{jid}", "icon_custom_emoji_id": "5807622114424924272"})
            else:
                row.append(InlineKeyboardButton(f"Start [{short}]", callback_data=f"mj#start#{jid}"))
                api_row.append({"text": f"Start [{short}]", "callback_data": f"mj#start#{jid}", "icon_custom_emoji_id": "5413643931139219521"})
                row.append(InlineKeyboardButton(f"Reset [{short}]", callback_data=f"mj#reset#{jid}"))
                api_row.append({"text": f"Reset [{short}]", "callback_data": f"mj#reset#{jid}", "icon_custom_emoji_id": "6030657343744644592"})
            
            row.append(InlineKeyboardButton(f"Info [{short}]", callback_data=f"mj#info#{jid}"))
            api_row.append({"text": f"Info [{short}]", "callback_data": f"mj#info#{jid}", "icon_custom_emoji_id": "5807700854060357972"})
            
            row.append(InlineKeyboardButton(f"Name [{short}]", callback_data=f"mj#rename#{jid}"))
            api_row.append({"text": f"Name [{short}]", "callback_data": f"mj#rename#{jid}", "icon_custom_emoji_id": "6024110353296660793"})
            
            row.append(InlineKeyboardButton(f"Delete [{short}]",  callback_data=f"mj#del#{jid}"))
            api_row.append({"text": f"Delete [{short}]", "callback_data": f"mj#del#{jid}", "icon_custom_emoji_id": "6030400221232501136"})
            
            btns_list.append(row)
            api_btns_list.append(api_row)

        btns_list.append([InlineKeyboardButton("Create Multi Job", callback_data="mj#new")])
        api_btns_list.append([{"text": "Create Multi Job", "callback_data": "mj#new", "icon_custom_emoji_id": "5807642902066634351"}])
        
        btns_list.append([InlineKeyboardButton("Refresh", callback_data="mj#list")])
        api_btns_list.append([{"text": "Refresh", "callback_data": "mj#list", "icon_custom_emoji_id": "5893192487324880883"}])
        
        btns_list.append([InlineKeyboardButton("Back", callback_data="back")])
        api_btns_list.append([{"text": "Back", "callback_data": "back"}])
        
        btns = btns_list
        api_btns = api_btns_list

    from plugins.share_bot import send_or_edit_with_custom_icons
    target_chat_id = msg_or_query.message.chat.id if is_cb else msg_or_query.chat.id
    target_msg_id = msg_or_query.message.id if is_cb else None
    sent_ok = await send_or_edit_with_custom_icons(
        client=bot,
        chat_id=target_chat_id,
        text=text,
        inline_keyboard=api_btns,
        message_id=target_msg_id
    )
    if not sent_ok:
        try:
            if is_cb:
                await msg_or_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(btns))
            else:
                await msg_or_query.reply_text(text, reply_markup=InlineKeyboardMarkup(btns))
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# Commands
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.private & filters.command(["multijob", "multijobs", "mj"]))
async def multijob_cmd(bot, message):
    from plugins.owner_utils import is_feature_enabled, is_any_owner, FEATURE_LABELS, _DISABLED_MSG
    uid = message.from_user.id
    if not await is_any_owner(uid) and not await is_feature_enabled("multi_job"):
        return await message.reply_text(_DISABLED_MSG.format(feature=FEATURE_LABELS["multi_job"]))
    await _render_mj_list(bot, message.from_user.id, message)


# ══════════════════════════════════════════════════════════════════════════════
# Callbacks
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_callback_query(filters.regex(r'^mj#list$'))
async def mj_list_cb(bot, query):
    from plugins.owner_utils import is_feature_enabled, is_any_owner, FEATURE_LABELS
    uid = query.from_user.id
    if not await is_any_owner(uid) and not await is_feature_enabled("multi_job"):
        return await query.answer(f"🔒 {FEATURE_LABELS['multi_job']} is disabled by admin.", show_alert=True)
    await query.answer()
    await _render_mj_list(bot, query.from_user.id, query)

@Client.on_callback_query(filters.regex(r'^mj#rename#'))
async def mj_rename_cb(bot, query):
    user_id = query.from_user.id
    job_id = query.data.split("#", 2)[2]
    await query.message.delete()
    
    r = await _mj_ask(bot, user_id,
        '<emoji id="6024110353296660793">✏️</emoji> <b>Edit Multi Job Name</b>\n\nSend a new name for this job:',
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("⛔ Cᴀɴᴄᴇʟ")]], resize_keyboard=True, one_time_keyboard=True))
    if "/cancel" not in r.text.lower():
        await db.db[COLL].update_one({"job_id": job_id}, {"$set": {"name": r.text.strip()[:100]}})
        await bot.send_message(user_id, f'<emoji id="6123181698193559460">✅</emoji> Multi Job renamed to <b>{r.text.strip()[:100]}</b>', reply_markup=ReplyKeyboardRemove())
    await _render_mj_list(bot, user_id, r)


@Client.on_callback_query(filters.regex(r'^mj#new$'))
async def mj_new_cb(bot, query):
    user_id = query.from_user.id
    await query.message.delete()
    await _create_mj_flow(bot, user_id)


@Client.on_callback_query(filters.regex(r'^mj#info#'))
async def mj_info_cb(bot, query):
    job_id = query.data.split("#", 2)[2]
    job = await _mj_get(job_id)
    if not job:
        return await query.answer("Job not found!", show_alert=True)

    import datetime
    created   = datetime.datetime.fromtimestamp(job.get("created", 0)).strftime("%d %b %Y %H:%M")
    st        = _mj_emoji(job.get("status", "stopped"))
    thread_id = job.get("to_thread_id")
    topic_lbl = f"\n<emoji id=\"5807800879553715710\">📌</emoji> <b>Topic Thread:</b> <code>{thread_id}</code>" if thread_id else ""
    
    start_id = job.get("start_id", 1)
    cur = job.get("current_id", "?")
    fetched = cur - start_id if isinstance(cur, int) and isinstance(start_id, int) and cur >= start_id else 0

    dest2_lbl = ""
    if job.get("to_chat_2"):
        t2 = job.get("to_thread_id_2")
        tp2 = f" [Thread {t2}]" if t2 else ""
        dest2_lbl = f"\n<emoji id=\"5807800879553715710\">📌</emoji> <b>Dest 2:</b> {job.get('to_title_2','?')}{tp2}"

    smart_val = job.get("smart_order", True)
    smart_lbl = "ON" if smart_val else "OFF (raw)"
    smart_icon = "6123181698193559460" if smart_val else "5413424119007978384"

    # Account info
    acc_lbl = "Default"
    acc_id = job.get("account_id")
    if acc_id:
        acc = await db.get_bot(job["user_id"], acc_id)
        if acc:
            kind = "Bot" if acc.get("is_bot", True) else "Userbot"
            name = acc.get("username") or acc.get("name") or "Unknown"
            acc_lbl = f"{kind}: @{name} (<code>{acc['id']}</code>)" if acc.get("username") else f"{kind}: {name} (<code>{acc['id']}</code>)"

    text = (
        f'<emoji id="6037622221625626773">📦</emoji> <b>Multi Job Info</b>\n'
        f"────────────────────\n"
        f'<emoji id="5332423642850536254">🆔</emoji> <b>ID:-</b> <code>{job_id[-6:]}</code>\n'
        f'<emoji id="6030400221232501136">👤</emoji> <b>Name:-</b> {job.get("name", "Default")}\n'
        f'<emoji id="6037622221625626773">🤖</emoji> <b>Account:-</b> {acc_lbl}\n'
        f'<emoji id="5807800879553715710">📊</emoji> <b>Status:-</b> {st} {job.get("status","?").capitalize()}\n'
        f'<emoji id="5807800879553715710">📥</emoji> <b>Source:-</b> {job.get("from_title","?")}\n'
        f'<emoji id="5807800879553715710">📤</emoji> <b>Dest 1:-</b> {job.get("to_title","?")}{topic_lbl}'
        f"{dest2_lbl}\n"
        f'<emoji id="5807800879553715710">📥</emoji> <b>Fetched messages:-</b> <code>{fetched}</code>\n'
        f'<emoji id="6123181698193559460">✅</emoji> <b>Forwarded:-</b> <code>{job.get("forwarded", 0)}</code>\n'
        f'<emoji id="6021683099773966917">🆔</emoji> <b>Current ID Progress:-</b> <code>{job.get("current_id", "?")} / {job.get("end_id", 0) or "∞"}</code>\n'
        f'<emoji id="5807800879553715710">🧠</emoji> <b>Smart Order:-</b> {"Enabled" if smart_val else "Disabled (raw chronological)"}\n'
        f'<emoji id="6023880246128810031">📅</emoji> <b>Created:-</b> <code>{created}</code>\n'
    )
    if job.get("error"):
        text += f"\n<b>Error:</b>\n<blockquote><code>{job['error']}</code></blockquote>"

    kb = [
        [InlineKeyboardButton(f"🧠 Smart Order: {smart_lbl}", callback_data=f"mj#togglesmart#{job_id}")],
        [InlineKeyboardButton("Back", callback_data="mj#list")]
    ]
    api_kb = [
        [{"text": f"Smart Order: {smart_lbl}", "callback_data": f"mj#togglesmart#{job_id}", "icon_custom_emoji_id": smart_icon}],
        [{"text": "Back", "callback_data": "mj#list"}]
    ]
    from plugins.share_bot import send_or_edit_with_custom_icons
    sent_ok = await send_or_edit_with_custom_icons(
        client=bot,
        chat_id=query.message.chat.id,
        text=text,
        inline_keyboard=api_kb,
        message_id=query.message.id
    )
    if not sent_ok:
        try:
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))
        except Exception:
            pass


@Client.on_callback_query(filters.regex(r'^mj#togglesmart#'))
async def mj_toggle_smart_cb(bot, query):
    await query.answer()
    job_id = query.data.split("#", 2)[2]
    job = await _mj_get(job_id)
    if not job: return
    new_val = not job.get("smart_order", True)
    await _mj_update(job_id, smart_order=new_val)
    # Refresh info directly
    query.data = f"mj#info#{job_id}"
    await mj_info_cb(bot, query)


@Client.on_callback_query(filters.regex(r'^mj#pause#'))
async def mj_pause_cb(bot, query):
    job_id  = query.data.split("#", 2)[2]
    user_id = query.from_user.id
    job = await _mj_get(job_id)
    if not job or job.get("user_id") != user_id:
        return await query.answer("⛔ Unauthorized.", show_alert=True)
    ev = _mj_paused.get(job_id)
    if ev:
        ev.clear()
    await _mj_update(job_id, status="paused")
    await query.answer("⏸ Job paused.", show_alert=False)
    await _render_mj_list(bot, user_id, query)


@Client.on_callback_query(filters.regex(r'^mj#resume#'))
async def mj_resume_cb(bot, query):
    job_id  = query.data.split("#", 2)[2]
    user_id = query.from_user.id
    job = await _mj_get(job_id)
    if not job or job.get("user_id") != user_id:
        return await query.answer("⛔ Unauthorized.", show_alert=True)
    ev = _mj_paused.get(job_id)
    if ev and job_id in _mj_tasks and not _mj_tasks[job_id].done():
        ev.set()
        await _mj_update(job_id, status="running")
        await query.answer("▶️ Resumed!", show_alert=False)
    else:
        routing = await db.get_task_routing()
        target_node = routing.get("multijob")
        should_run_locally = (target_node == "main" or target_node is None)

        if should_run_locally:
            await _mj_update(job_id, status="running")
            _mj_start_task(job_id, user_id)
            await query.answer("▶️ Restarted from saved position!", show_alert=False)
        else:
            await _mj_update(job_id, status="queued")
            await query.answer(f"▶️ Queued for worker: {target_node}", show_alert=False)
    await _render_mj_list(bot, user_id, query)


@Client.on_callback_query(filters.regex(r'^mj#stop#'))
async def mj_stop_cb(bot, query):
    job_id  = query.data.split("#", 2)[2]
    user_id = query.from_user.id
    job = await _mj_get(job_id)
    if not job or job.get("user_id") != user_id:
        return await query.answer("⛔ Unauthorized.", show_alert=True)
    task = _mj_tasks.pop(job_id, None)
    if task and not task.done():
        task.cancel()
    ev = _mj_paused.pop(job_id, None)
    if ev: ev.set()
    await _mj_update(job_id, status="stopped")
    await query.answer("⏹ Job stopped.", show_alert=False)
    await _render_mj_list(bot, user_id, query)


@Client.on_callback_query(filters.regex(r'^mj#reset#'))
async def mj_reset_cb(bot, query):
    job_id  = query.data.split("#", 2)[2]
    user_id = query.from_user.id
    job = await _mj_get(job_id)
    if not job or job.get("user_id") != user_id:
        return await query.answer("⛔ Unauthorized.", show_alert=True)
    task = _mj_tasks.pop(job_id, None)
    if task and not task.done():
        task.cancel()
    ev = _mj_paused.pop(job_id, None)
    if ev: ev.set()
    start_id = int(job.get("start_id") or 1)
    await _mj_update(job_id,
        status="stopped",
        current_id=start_id,
        forwarded=0,
        consecutive_empty=0,
        error=""
    )
    await query.answer("🔁 Job reset to start!", show_alert=True)
    await _render_mj_list(bot, user_id, query)


@Client.on_callback_query(filters.regex(r'^mj#start#'))
async def mj_start_cb(bot, query):
    job_id  = query.data.split("#", 2)[2]
    user_id = query.from_user.id
    job = await _mj_get(job_id)
    if not job or job.get("user_id") != user_id:
        return await query.answer("⛔ Unauthorized.", show_alert=True)
    if job_id in _mj_tasks and not _mj_tasks[job_id].done():
        return await query.answer("Already running!", show_alert=True)
    await _mj_update(job_id, status="running")
    _mj_start_task(job_id, user_id)
    await query.answer("▶️ Job started!", show_alert=False)
    await _render_mj_list(bot, user_id, query)


@Client.on_callback_query(filters.regex(r'^mj#force_ask#'))
async def mj_force_ask_cb(bot, query):
    job_id = query.data.split("#", 2)[2]
    txt = (
        "⚠️ <b>WARNING: FORCE START</b>\n\n"
        "You are about to bypass the safety queue and force this Multi Job to start concurrently.\n\n"
        "<b>Potential Issues:</b>\n"
        "• <b>API Limits:</b> Forwarding too many messages simultaneously drastically increases your risk of FloodWaits or temporary bans.\n"
        "• <b>Server CPU:</b> Slower overall speed as tasks compete.\n\n"
        "Are you sure you want to force start this job immediately?"
    )
    kb = [
        [InlineKeyboardButton("✅ Yes, Force Start Anyway", callback_data=f"mj#force_do#{job_id}")],
        [InlineKeyboardButton("⛔ Cancel (Keep in Queue)", callback_data="mj#list")]
    ]
    return await query.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(kb))

@Client.on_callback_query(filters.regex(r'^mj#force_do#'))
async def mj_force_do_cb(bot, query):
    job_id  = query.data.split("#", 2)[2]
    user_id = query.from_user.id
    job = await _mj_get(job_id)
    if not job or job.get("user_id") != user_id:
        return await query.answer("⛔ Unauthorized.", show_alert=True)
        
    await _mj_update(job_id, status="running", force_active=True)
    
    # Release from AryaJobQueue explicitly to prevent stale lists
    try:
        from plugins.job_queue import AryaJobQueue
        if job_id in AryaJobQueue._waiting_order.get("multijob", []):
            AryaJobQueue._waiting_order["multijob"].remove(job_id)
    except Exception: pass
    
    task = _mj_tasks.get(job_id)
    if task and not task.done():
        task.cancel()
        await asyncio.sleep(0.5)
        
    _mj_start_task(job_id, user_id, bot=bot)
    await query.answer("🚀 Job forcefully activated!", show_alert=False)
    await _render_mj_list(bot, user_id, query)

@Client.on_callback_query(filters.regex(r'^mj#del#'))
async def mj_del_cb(bot, query):
    job_id  = query.data.split("#", 2)[2]
    user_id = query.from_user.id
    job = await _mj_get(job_id)
    if not job or job.get("user_id") != user_id:
        return await query.answer("⛔ Unauthorized.", show_alert=True)
    task = _mj_tasks.pop(job_id, None)
    if task and not task.done():
        task.cancel()
    ev = _mj_paused.pop(job_id, None)
    if ev: ev.set()
    await _mj_delete(job_id)
    await query.answer("🗑 Job deleted.", show_alert=False)
    await _render_mj_list(bot, user_id, query)


# ══════════════════════════════════════════════════════════════════════════════
# Create Multi Job — Interactive flow
# ══════════════════════════════════════════════════════════════════════════════

async def _mj_ask_dest(bot, user_id: int, channels: list, step_label: str, optional: bool = False, undo_btn: bool = False) -> tuple:
    """Ask user to pick a saved channel. Returns (chat_id, title, cancelled).
    cancelled=True means cancelled, cancelled='undo' means undo was pressed."""
    from plugins.utils import ask_channel_picker
    
    extra = []
    if undo_btn:
        extra.append("↩️ Uɴᴅᴏ")
        
    picked = await ask_channel_picker(bot, user_id, step_label, extra_options=extra)
    
    if not picked:
        return None, None, True
        
    if isinstance(picked, str):
        if picked == "↩️ Uɴᴅᴏ":
            return None, None, "undo"
            
    return picked['chat_id'], picked['title'], False


async def _mj_ask_topic(bot, user_id: int, dest_label: str, undo_btn: bool = False) -> int | None | str:
    """Ask for optional topic thread ID."""
    from pyrogram.types import KeyboardButton, ReplyKeyboardMarkup
    CANCEL_BTN = KeyboardButton("⛔ Cᴀɴᴄᴇʟ")
    UNDO_BTN   = KeyboardButton("↩️ Uɴᴅᴏ")
    rows = [[KeyboardButton("0 (No Topic)")]]
    if undo_btn:
        rows.append([UNDO_BTN, CANCEL_BTN])
    else:
        rows.append([CANCEL_BTN])
    r = await _mj_ask(bot, user_id,
        f"<b>Topic Thread for {dest_label} (Optional)</b>\n\n"
        "• Send the <b>Thread ID</b> if you want to post inside a specific group topic\n"
        "• Send <b>0</b> to post in the main chat\n\n"
        "<i>Find Thread ID: open topic in Telegram Web → number after <code>/topics/</code> in URL</i>",
        reply_markup=ReplyKeyboardMarkup(
            rows,
            resize_keyboard=True, one_time_keyboard=True
        ))
    t = r.text.strip() if r and r.text else "0"
    t_lower = t.lower()
    if "/cancel" in t_lower or "⛔" in t or "cᴀɴᴄᴇʟ" in t_lower:
        return "cancelled"
    if "/undo" in t_lower or "↩️" in t or "uɴᴅᴏ" in t_lower:
        return "undo"
    if t.isdigit() and int(t) > 0:
        return int(t)
    return None



async def _create_mj_flow(bot, user_id: int):
    # Clear any stale future
    old = _mj_waiting.pop(user_id, None)
    if old and not old.done():
        old.cancel()

    CANCEL_BTN = KeyboardButton("⛔ Cᴀɴᴄᴇʟ")
    UNDO_BTN   = KeyboardButton("↩️ Uɴᴅᴏ")

    def _cancel(txt): return False if not txt else txt.strip().startswith("/cancel") or "⛔" in txt or "Cᴀɴᴄᴇʟ" in txt
    def _undo(txt):   return False if not txt else txt.strip().startswith("/undo") or "↩️" in txt or "Uɴᴅᴏ" in txt

    # ── Step 1: Name ──────────────────────────────────────────────
    name_r = await _mj_ask(bot, user_id,
        "<b>Create Multi Job — Step 1/6</b>\n\n"
        "Send a <b>name</b> for this job, or press <b>Default</b>.",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("Default")], [CANCEL_BTN]],
            resize_keyboard=True, one_time_keyboard=True))
    if _cancel(name_r.text):
        return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())

    job_name = name_r.text.strip()[:100]
    if job_name.lower() == "default":
        job_name = None

    # ── Step 2: Account ───────────────────────────────────────────
    accounts = await db.get_bots(user_id)
    if not accounts:
        return await bot.send_message(user_id,
            "<b>❌ No accounts found. Add one in /settings → Accounts first.</b>")

    def _acc_label(a):
        kind = "Bot" if a.get("is_bot", True) else "Userbot"
        name = a.get("username") or a.get("name", "Unknown")
        return f"{kind}: {name} [{a['id']}]"

    acc_btns = [[KeyboardButton(_acc_label(a))] for a in accounts]
    acc_btns.append([UNDO_BTN, CANCEL_BTN])

    acc_r = await _mj_ask(bot, user_id,
        "<b>Create Multi Job — Step 2/6</b>\n\n"
        "Choose which <b>account</b> to use:\n\n"
        "<blockquote expandable>"
        "🤖 <b>Bot</b> — works for public channels and groups where the bot is admin.\n"
        "👤 <b>Userbot</b> — required for:\n"
        "  • Private/restricted channels\n"
        "  • Forwarding with copy (no forward tag)\n"
        "  • Saved Messages as source\n"
        "  • Groups where bots are blocked"
        "</blockquote>",
        reply_markup=ReplyKeyboardMarkup(acc_btns, resize_keyboard=True, one_time_keyboard=True))

    if _cancel(acc_r.text):
        return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
    if _undo(acc_r.text):
        name_r = await _mj_ask(bot, user_id,
            "<b>Create Multi Job — Step 1/6</b>\n\n"
            "Send a <b>name</b> for this job, or press <b>Default</b>.",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("Default")], [CANCEL_BTN]],
                resize_keyboard=True, one_time_keyboard=True))
        if _cancel(name_r.text):
            return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
        job_name = name_r.text.strip()[:100]
        if job_name.lower() == "default":
            job_name = None

    acc_id = None
    if "[" in acc_r.text and "]" in acc_r.text:
        try: acc_id = int(acc_r.text.split('[')[-1].split(']')[0])
        except Exception: pass
    sel_acc = (await db.get_bot(user_id, acc_id)) if acc_id else accounts[0]
    is_bot  = sel_acc.get("is_bot", True)

    # ── Step 3: Source ────────────────────────────────────────────
    while True:
        src_r = await _mj_ask(bot, user_id,
            "<b>Step 3/6 — Source Chat</b>\n\n"
            "Send the <b>source channel, group, or chat</b> to copy messages from.\n\n"
            "<blockquote expandable>"
            "Accepted formats:\n"
            "• <code>@username</code> — public channel/group username\n"
            "• <code>https://t.me/username</code> — public link\n"
            "• <code>https://t.me/c/1234567890/1</code> — private channel link\n"
            "• <code>-1001234567890</code> — numeric chat ID (negative for channels/groups)\n"
            "• <code>me</code> — your own Saved Messages (Userbot only)\n\n"
            "📌 For private channels: use a Userbot that is already a member.\n"
            "📌 For public channels: Bot account works if it can read messages.\n"
            "📌 Group Topics: supported — you will be asked for a thread ID next.\n"
            "📌 Bot DM: use the bot's username or numeric ID."
            "</blockquote>",
            reply_markup=ReplyKeyboardMarkup(
                [[UNDO_BTN, CANCEL_BTN]], resize_keyboard=True, one_time_keyboard=True))

        if _cancel(src_r.text):
            return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
        if _undo(src_r.text):
            # Redo step 2
            acc_r2 = await _mj_ask(bot, user_id,
                "<b>↩️ Redo — Step 2/6: Account</b>\n\nChoose the account again:",
                reply_markup=ReplyKeyboardMarkup(acc_btns, resize_keyboard=True, one_time_keyboard=True))
            if _cancel(acc_r2.text):
                return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
            if "[" in acc_r2.text and "]" in acc_r2.text:
                try: acc_id = int(acc_r2.text.split('[')[-1].split(']')[0])
                except Exception: pass
            sel_acc = (await db.get_bot(user_id, acc_id)) if acc_id else accounts[0]
            is_bot  = sel_acc.get("is_bot", True)
            continue
        break

    from_chat_raw = src_r.text.strip()
    if from_chat_raw.lower() in ("me", "saved"):
        if is_bot:
            return await bot.send_message(user_id,
                "<b>❌ Saved Messages require a Userbot account.</b>",
                reply_markup=ReplyKeyboardRemove())
        from_chat  = "me"
        from_title = "Saved Messages"
    else:
        from_chat = from_chat_raw
        if from_chat.lstrip('-').isdigit():
            from_chat = int(from_chat)
            if from_chat > 0 and len(str(from_chat)) >= 13 and str(from_chat).startswith("100"):
                from_chat = -from_chat
        elif "t.me/c/" in from_chat:
            parts = from_chat.split("t.me/c/")[1].split("/")
            if parts[0].isdigit():
                if parts[0].startswith("100") and len(parts[0]) >= 13:
                    from_chat = int(f"-{parts[0]}")
                else:
                    from_chat = int(f"-100{parts[0]}")
        elif "t.me/" in from_chat:
            username = from_chat.split("t.me/")[1].split("/")[0].split("?")[0]
            if not username.startswith("+"):
                from_chat = username

        try:
            chat_obj   = await bot.get_chat(from_chat)
            from_title = (getattr(chat_obj, "title", None) or
                          getattr(chat_obj, "first_name", None) or str(from_chat))
        except Exception:
            from_title = str(from_chat)

    from_thread = await _mj_ask_topic(bot, user_id, "Source")

    # ── Step 4: Primary Destination ───────────────────────────────
    channels = await db.get_user_channels(user_id)
    if not channels:
        return await bot.send_message(user_id,
            "<b>❌ No target channels saved. Add via /settings → Channels.</b>",
            reply_markup=ReplyKeyboardRemove())

    while True:
        to_chat, to_title, cancelled = await _mj_ask_dest(bot, user_id, channels,
            "<b>Step 4/6 — Destination</b>\n\nWhere should messages be sent?\n\n"
            "<blockquote expandable>"
            "Choose from your saved channels/groups.\n"
            "To add a channel, go to /settings → Channels.\n"
            "The account you chose must be an admin with send permission."
            "</blockquote>",
            undo_btn=True)
        if cancelled == "undo":
            # Redo source
            continue  # will fall through — in practice they'd re-enter src step, but we keep it simple here
        elif cancelled:
            return
        break

    to_thread = await _mj_ask_topic(bot, user_id, "Destination")

    # ── Step 5: Message Range ─────────────────────────────────────
    while True:
        range_r = await _mj_ask(bot, user_id,
            "<b>Step 5/6 — Message Range</b>\n\n"
            "Which messages should be copied?\n\n"
            "<blockquote expandable>"
            "Options:\n"
            "• <b>ALL</b> — copy from the very first message (ID 1)\n"
            "• <code>500</code> — start from message ID 500 onwards\n"
            "• <code>100:5000</code> — copy only messages from ID 100 to 5000\n\n"
            "The job stops automatically when the range is complete.\n"
            "For continuous/unlimited copying, leave end ID as 0 or send ALL."
            "</blockquote>",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("ALL")], [UNDO_BTN, CANCEL_BTN]],
                resize_keyboard=True, one_time_keyboard=True))

        if _cancel(range_r.text):
            return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
        if _undo(range_r.text):
            # Redo destination
            to_chat, to_title, cancelled = await _mj_ask_dest(bot, user_id, channels,
                "<b>↩️ Redo — Step 4/6: Destination</b>\n\nChoose destination again:")
            if cancelled:
                return
            to_thread = await _mj_ask_topic(bot, user_id, "Destination")
            continue
        break

    start_id = from_thread if from_thread else 1
    end_id   = 0
    rtext    = range_r.text.strip().lower()
    if rtext != "all":
        if ":" in rtext:
            parts = rtext.split(":", 1)
            try: start_id = int(parts[0].strip())
            except Exception: pass
            try: end_id   = int(parts[1].strip())
            except Exception: pass
        else:
            try: start_id = int(rtext)
            except Exception: pass

    # ── Step 6/6: Smart Order ─────────────────────────────
    while True:
        smart_r = await _mj_ask(bot, user_id,
            "<b>Step 6/6 — Smart Order?</b>\n\n"
            "Should the bot automatically buffer and sort messages naturally (e.g. Ep 1, Ep 2, Part 1, Part 2) to correct any out-of-order uploads?\n\n"
            "<blockquote expandable>"
            "• <b>ON</b> — Sorts episodes/parts before forwarding\n"
            "• <b>OFF</b> — Forwards in raw chronological order\n"
            "</blockquote>",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("✅ YES (Enable Smart Order)")],
                 [KeyboardButton("❌ NO (Disable Smart Order)")],
                 [UNDO_BTN, CANCEL_BTN]],
                resize_keyboard=True, one_time_keyboard=True
            ))

        if _cancel(smart_r.text):
            return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
        if _undo(smart_r.text):
            # redo message range
            range_r2 = await _mj_ask(bot, user_id,
                "<b>↩️ Redo — Step 5/6: Message Range</b>\n\nWhich messages should be copied?",
                reply_markup=ReplyKeyboardMarkup(
                    [[KeyboardButton("ALL")], [UNDO_BTN, CANCEL_BTN]],
                    resize_keyboard=True, one_time_keyboard=True))
            if _cancel(range_r2.text):
                return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
            start_id = from_thread if from_thread else 1
            end_id = 0
            rtext = range_r2.text.strip().lower()
            if rtext != "all":
                if ":" in rtext:
                    parts = rtext.split(":", 1)
                    try: start_id = int(parts[0].strip())
                    except Exception: pass
                    try: end_id   = int(parts[1].strip())
                    except Exception: pass
                else:
                    try: start_id = int(rtext)
                    except Exception: pass
            continue
        break

    smart_order = "yes" in (smart_r.text or "").lower() or "✅" in (smart_r.text or "")

    # ── Save & Start ──────────────────────────────────────────────
    job_id = f"mj-{user_id}-{int(time.time())}"
    
    routing = await db.get_task_routing()
    target_node = routing.get("multijob")
    # If not explicitly routed, default to main
    should_run_locally = (target_node == "main" or target_node is None)

    job = {
        "job_id":         job_id,
        "user_id":        user_id,
        "name":           job_name if job_name else f"Multi Job {job_id[-6:]}",
        "account_id":     sel_acc["id"],
        "from_chat":      from_chat,
        "from_title":     from_title,
        "from_thread":    from_thread,
        "to_chat":        to_chat,
        "to_title":       to_title,
        "to_thread_id":   to_thread,
        "start_id":       start_id,
        "end_id":         end_id,
        "current_id":     start_id,
        "status":         "running" if should_run_locally else "queued",
        "created":        int(time.time()),
        "forwarded":      0,
        "consecutive_empty": 0,
        "error":          "",
        "smart_order":    smart_order,
    }
    await _mj_save(job)
    
    if should_run_locally:
        _mj_start_task(job_id, user_id, bot=bot)

    end_lbl   = f"to ID <code>{end_id}</code>" if end_id else "all messages"
    thread_lbl = f" → Topic <code>{to_thread}</code>" if to_thread else ""
    kind = "Bot" if is_bot else "Userbot"
    smart_lbl_msg = "✅ ON" if smart_order else "❌ OFF"
    
    run_msg = "<i>Running in background.\nUse /multijob to manage.</i>" if should_run_locally else f"<i>Queued for worker: <b>{target_node}</b>.\nUse /multijob to manage.</i>"

    await bot.send_message(
        user_id,
        f"<b>✅ Multi Job Created!</b>\n\n"
        f"<b>{from_title}</b> → <b>{to_title}</b>{thread_lbl}\n"
        f"<b>Account:</b> {kind}: {sel_acc.get('name','?')}\n"
        f"<b>Range:</b> From ID <code>{start_id}</code> · {end_lbl}\n"
        f"<b>Smart Order:</b> {smart_lbl_msg}\n"
        f"<b>Job ID:</b> <code>{job_id[-6:]}</code>\n\n"
        f"{run_msg}",
        reply_markup=ReplyKeyboardRemove()
    )