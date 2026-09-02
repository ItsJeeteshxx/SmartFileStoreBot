import asyncio
import logging
import re
import os
import html
from pyrogram import Client
from pyrogram.errors import FloodWait, RPCError

logger = logging.getLogger(__name__)

# Track pending notifications: story_id -> { "end_id": int, "highest_ep_num": int, "story_name": str }
_PENDING_NOTIFICATIONS = {}

async def _extract_episode_number(file_name: str, caption: str, title: str = "", current_ep_num: int = 0):
    """
    Extracts the episode number from audio metadata (file_name, caption, title).
    Uses strict episode regex first so 'Part 4' is not mistaken for episode 4.
    """
    fname_clean = re.sub(r'\.[a-zA-Z0-9]+$', '', file_name) if file_name else ""
    text = f"{fname_clean} {title or ''} {caption or ''}".strip()
    if not text:
        return None

    # 1. Match explicit episode labels (English and Hindi) - do NOT match 'part'
    match = re.search(r'(?:[eE]p(?:isode)?|[eE]pisode|[eE]p|\b[eE]\b|एपिसोड|कड़ी|kadi|ch(?:apter)?|अध्याय)[\.\-\s_:#]*(\d+)', text, re.IGNORECASE)
    if match:
        return int(match.group(1))

    # 2. Match numbers in parentheses/brackets e.g. (1065) or [1065]
    match = re.search(r'[\(\[\{](\d+)[\)\]\}]', text)
    if match:
        return int(match.group(1))

    # 3. Match standalone numbers
    numbers = [int(n) for n in re.findall(r'\b\d+\b', text) if int(n) < 50000]
    if numbers:
        if current_ep_num > 0:
            valid_candidates = [n for n in numbers if n >= current_ep_num - 5]
            if valid_candidates:
                return valid_candidates[-1]
        return numbers[-1]

    return None

async def send_announcement(bot: Client, pending_info: dict):
    from config import Config
    channel_id_str = Config.PREMIUM_UPDATES_CHANNEL
    if not channel_id_str:
        return
        
    try:
        channel_id = int(channel_id_str)
    except ValueError:
        channel_id = channel_id_str

    story_name = html.escape(str(pending_info["story_name"]))
    ep_num = pending_info["highest_ep_num"]
    total_eps = str(ep_num)

    if ep_num == -1:
        ep_num = "New"
        total_eps = "Updated"

    msg_text = f"""<blockquote>प्रिय मित्रों , {story_name} में एपिसोड {ep_num} अपडेट कर दिए गए हैं। अब इस कहानी में कुल {total_eps} एपिसोड उपलब्ध हैं।</blockquote>
<blockquote expandable><u>नए एपिसोड सुनने के लिए आर्या प्रीमियम बोट या मिनी ऐप में जाएँ। वहाँ <b>"मेरी स्टोरीज"</b> अथवा ऐप की लाइब्रेरी में <b>"खरीदी गई"</b> सेक्शन खोलकर नए एपिसोड प्राप्त करें और उनका आनंद लें।</u></blockquote>
<blockquote><b>धन्यवाद।</b></blockquote>

<blockquote>Dear Listeners, Episodes {ep_num} have been added to {story_name}. The story now has a total of {total_eps} episodes available.</blockquote>
<blockquote expandable><u>To listen to the latest episodes, open the Arya Premium Bot or Mini App. Go to <b>"My Stories"</b> or the <b>"Purchased"</b> section in the app library to access and enjoy the newly updated episodes.</u></blockquote>
<blockquote><b>Thank you.</b></blockquote>"""

    try:
        from pyrogram.enums import ParseMode
        await bot.send_message(
            chat_id=channel_id,
            text=msg_text,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"[Premium Monitor] Failed to send announcement to {channel_id}: {e}")

    # ── Dispatch Bot DM notifications to users who purchased this ongoing story ──
    try:
        from database import db
        from pyrogram.enums import ParseMode
        from bson.objectid import ObjectId

        story_id_raw = str(pending_info.get("story_id", ""))
        
        match_list = []
        if story_id_raw:
            match_list.append(story_id_raw)
            if story_id_raw.isdigit():
                match_list.append(int(story_id_raw))
            if ObjectId.is_valid(story_id_raw):
                match_list.append(ObjectId(story_id_raw))
        if story_name:
            match_list.append(story_name)

        if match_list:
            user_ids_to_notify = set()

            purchased_query = {"purchases": {"$in": match_list}}
            users_list = await db.db.users.find(purchased_query).to_list(length=None)
            prem_users_list = await db.db.premium_users.find(purchased_query).to_list(length=None)

            for u in (users_list + prem_users_list):
                uid = u.get("id") or u.get("telegram_id")
                if uid and str(uid).isdigit():
                    user_ids_to_notify.add(int(uid))

            pur_query = {"story_id": {"$in": match_list}}
            prem_pur_list = await db.db.premium_purchases.find(pur_query).to_list(length=None)
            legacy_pur_list = await db.db.purchases.find(pur_query).to_list(length=None)

            for p in (prem_pur_list + legacy_pur_list):
                uid = p.get("user_id") or p.get("telegram_id")
                if uid and str(uid).isdigit():
                    user_ids_to_notify.add(int(uid))

            ord_query = {
                "status": {"$in": ["paid", "approved", "completed", "success", "Paid", "Approved"]},
                "$or": [
                    {"story_ids": {"$in": match_list}},
                    {"story_id": {"$in": match_list}},
                    {"story_names": {"$in": match_list}}
                ]
            }
            orders_list = await db.db.orders.find(ord_query).to_list(length=None)
            for o in orders_list:
                uid = o.get("user_id") or o.get("telegram_id")
                if uid and str(uid).isdigit():
                    user_ids_to_notify.add(int(uid))

            logger.info(f"[Premium Monitor] Found {len(user_ids_to_notify)} buyers to notify for ongoing story update '{story_name}'")

            for uid in user_ids_to_notify:
                buyer = await db.db.users.find_one({"id": uid}) or await db.db.purchases.find_one({"user_id": uid}) or {}
                if buyer.get("ongoing_updates_enabled", True) is False:
                    continue

                user_name = buyer.get("first_name") or buyer.get("name") or buyer.get("username") or "Listener"
                user_name_escaped = html.escape(str(user_name))
                
                dm_text = f"""<blockquote>प्रिय मित्र {user_name_escaped} , {story_name} में एपिसोड {ep_num} अपडेट कर दिए गए हैं। अब इस कहानी में कुल {total_eps} एपिसोड उपलब्ध हैं।
नए एपिसोड सुनने के लिए आर्या प्रीमियम बोट या मिनी ऐप में जाएँ। वहाँ "मेरी स्टोरीज" अथवा ऐप की लाइब्रेरी में "खरीदी गई" सेक्शन खोलकर नए एपिसोड प्राप्त करें और उनका आनंद लें।
धन्यवाद।</blockquote>

<blockquote>Dear Listener {user_name_escaped} , Episodes {ep_num} have been added to {story_name}. The story now has a total of {total_eps} episodes available.
To listen to the latest episodes, open the Arya Premium Bot or Mini App. Go to "My Stories" or the "Purchased" section in the app library to access and enjoy the newly updated episodes.
Thank you.</blockquote>"""

                try:
                    await bot.send_message(
                        chat_id=uid,
                        text=dm_text,
                        parse_mode=ParseMode.HTML
                    )
                    await asyncio.sleep(0.05)
                except Exception as dm_err:
                    logger.warning(f"[Premium Monitor] Could not send ongoing update DM to user {uid}: {dm_err}")
    except Exception as e:
        logger.error(f"[Premium Monitor] Error dispatching ongoing update DMs: {e}")

async def check_and_update_single_story(client: Client, story: dict, db) -> bool:
    """
    Checks Telegram source channel for new audio episodes of a single ongoing story,
    updates story end_id, episodes count, valid_file_ids, and synchronizes ongoing parts.
    """
    story_id = str(story.get("_id", ""))
    story_name = story.get("story_name_en") or story.get("story_name") or story.get("title") or "Story"
    source_id = story.get("source") or story.get("source_channel") or story.get("channel_id")

    if not source_id:
        return False

    try:
        source_id = int(source_id)
    except (ValueError, TypeError):
        pass

    # 1. Determine the true maximum end_id across root story, parts, and valid_file_ids
    story_end_id = int(story.get("end_id") or story.get("end_message_id") or 0)
    parts_end_ids = [int(p.get("end_id") or 0) for p in (story.get("parts") or []) if p.get("end_id")]
    valid_f_ids = [int(x) for x in (story.get("valid_file_ids") or []) if isinstance(x, (int, float))]
    
    effective_end_id = max([story_end_id] + parts_end_ids + valid_f_ids + [0])
    if effective_end_id <= 0:
        effective_end_id = int(story.get("start_id") or 0)

    # 2. Determine current total episodes count
    curr_ep_str = str(story.get("episodes") or story.get("ep_count") or story.get("total_eps") or "").strip()
    m_ep = re.search(r"(\d+)", curr_ep_str)
    current_ep_num = int(m_ep.group(1)) if m_ep else 0

    # 3. Fetch next chunk of messages from source channel
    fetch_start = effective_end_id + 1
    fetch_end = effective_end_id + 200
    ids_to_fetch = list(range(fetch_start, fetch_end + 1))

    msgs = []
    # Try fetching via primary client or fallback store clients
    clients_to_try = [client] if client else []
    try:
        from plugins.userbot.market_seller import market_clients
        for mc in market_clients.values():
            if mc not in clients_to_try:
                clients_to_try.append(mc)
    except Exception:
        pass

    fetched = False
    for cli in clients_to_try:
        try:
            msgs = await cli.get_messages(source_id, ids_to_fetch)
            fetched = True
            break
        except FloodWait as fw:
            await asyncio.sleep(fw.value)
            continue
        except Exception as ex:
            logger.debug(f"[Premium Monitor] Client {getattr(cli, 'name', 'unknown')} failed for channel {source_id}: {ex}")
            continue

    if not fetched or not msgs:
        return False

    new_audio_ids = []
    highest_extracted_ep = -1

    for msg in msgs:
        if not msg or msg.empty:
            continue

        is_audio = bool(
            msg.audio or msg.voice or
            (msg.document and str(getattr(msg.document, "mime_type", "")).startswith(("audio/", "video/"))) or
            (msg.document and str(getattr(msg.document, "file_name", "")).lower().endswith((".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".opus", ".mp4", ".mkv")))
        )

        if not is_audio:
            continue

        new_audio_ids.append(msg.id)

        fname = getattr(msg.audio or msg.document or msg.voice, "file_name", "")
        caption = msg.caption or ""
        title = getattr(msg.audio, "title", "") if msg.audio else ""

        ep_val = await _extract_episode_number(fname, caption, title, current_ep_num)
        if ep_val and ep_val > highest_extracted_ep:
            highest_extracted_ep = ep_val

    if not new_audio_ids:
        # Check if there is a pending announcement to send
        if story_id in _PENDING_NOTIFICATIONS:
            pending_info = _PENDING_NOTIFICATIONS[story_id]
            if pending_info["end_id"] == effective_end_id and client:
                await send_announcement(client, pending_info)
                logger.info(f"[Premium Monitor] Sent delayed announcement for '{pending_info['story_name']}'")
                del _PENDING_NOTIFICATIONS[story_id]
        return False

    # 4. We found new audio episodes! Calculate new end_id and episode count
    new_end_id = max(new_audio_ids)
    
    if highest_extracted_ep >= current_ep_num:
        new_total_episodes = highest_extracted_ep
    else:
        new_total_episodes = current_ep_num + len(new_audio_ids)

    all_valid_ids = sorted(list(set(valid_f_ids + new_audio_ids)))

    update_data = {
        "end_id": new_end_id,
        "end_message_id": new_end_id,
        "episodes": str(new_total_episodes),
        "valid_file_ids": all_valid_ids,
        "file_count": len(all_valid_ids)
    }

    # 5. Synchronize ongoing parts in parts list
    raw_parts = story.get("parts") or story.get("story_parts") or []
    if isinstance(raw_parts, list) and len(raw_parts) > 0:
        updated_parts = []
        found_ongoing = False
        for idx, p in enumerate(raw_parts):
            p_copy = dict(p)
            b_val = str(p_copy.get("badge") or p_copy.get("badge_type") or "").lower()
            is_ong = bool(p_copy.get("is_ongoing") or b_val == "ongoing")
            is_last = (idx == len(raw_parts) - 1)

            if is_ong or (not found_ongoing and is_last):
                found_ongoing = True
                p_copy["end_id"] = new_end_id
                p_copy["is_ongoing"] = True
                p_copy["badge"] = "ongoing"
                p_copy["badge_type"] = "ongoing"

                curr_p_eps = str(p_copy.get("episodes") or "").strip()
                m_p = re.search(r"(\d+)", curr_p_eps)
                if m_p:
                    start_ep = int(m_p.group(1))
                    if new_total_episodes >= start_ep:
                        p_copy["episodes"] = f"{start_ep}-{new_total_episodes}"
                    else:
                        p_copy["episodes"] = str(new_total_episodes)
                else:
                    p_copy["episodes"] = str(new_total_episodes)

                p_start_id = int(p_copy.get("start_id") or 0)
                p_val_ids = [mid for mid in all_valid_ids if p_start_id <= mid <= new_end_id]
                p_copy["valid_file_ids"] = p_val_ids
                p_copy["file_count"] = len(p_val_ids)

            updated_parts.append(p_copy)
        update_data["parts"] = updated_parts

    await db.db.premium_stories.update_one(
        {"_id": story["_id"]},
        {"$set": update_data}
    )

    logger.info(
        f"[Premium Monitor] ✅ Successfully updated '{story_name}' -> "
        f"end_id: {new_end_id} (prev: {effective_end_id}), "
        f"episodes: {new_total_episodes} (prev: {current_ep_num}), "
        f"+{len(new_audio_ids)} new audio files added."
    )

    # Queue announcement
    _PENDING_NOTIFICATIONS[story_id] = {
        "story_id": story_id,
        "end_id": new_end_id,
        "highest_ep_num": new_total_episodes,
        "story_name": story_name
    }

    # Clear Mini App API cache immediately
    try:
        import mini_app_api
        mini_app_api._stories_cache = None
    except Exception:
        pass

    return True

async def check_and_update_all_ongoing_stories(bot: Client = None):
    """Scans and updates all ongoing stories across connected channels."""
    from database import db
    query = {
        "$or": [
            {"status": {"$in": ["Ongoing", "ongoing", "ONGOING"]}},
            {"parts.badge": "ongoing"},
            {"parts.is_ongoing": True},
            {"is_ongoing": True},
            {"status": {"$nin": ["Completed", "completed", "COMPLETED", "Unfinished", "unfinished", "Stucked", "stucked"]}}
        ],
        "is_completed": {"$ne": True},
        "source": {"$exists": True, "$ne": None, "$ne": ""}
    }
    stories = await db.db.premium_stories.find(query).to_list(length=None)
    logger.info(f"[Premium Monitor] Found {len(stories)} ongoing stories to check...")

    for story in stories:
        try:
            await check_and_update_single_story(bot, story, db)
        except Exception as e:
            logger.error(f"[Premium Monitor] Error checking story '{story.get('story_name_en', story.get('story_name', 'Unknown'))}': {e}", exc_info=True)

async def start_premium_live_monitor(bot: Client):
    """
    Background worker that runs every 60 seconds (1 minute) to poll source channels 
    for newly uploaded audio episodes of ongoing premium stories.
    """
    logger.info("🚀 Starting Premium Live Monitor (1-minute polling loop)...")
    
    # Allow bot and market clients to connect and warm up
    await asyncio.sleep(8)

    while True:
        try:
            await check_and_update_all_ongoing_stories(bot)
        except Exception as e:
            logger.error(f"[Premium Monitor] Polling loop error: {e}")

        # Poll every 60 seconds for fast real-time updates
        await asyncio.sleep(60)

