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

async def _extract_episode_number(file_name: str, caption: str, title: str = ""):
    """
    Extracts the episode number from audio metadata (file_name, caption, title).
    Matches explicit episode labels and numbers without confusing 'Part X' or bitrate/dates.
    """
    fname = ""
    if file_name:
        fname, _ = os.path.splitext(file_name)

    text = f"{fname} {title or ''} {caption or ''}".strip()
    if not text:
        return None

    # 1. Match explicit episode labels: Ep 123, Episode 123, EP-123, एपिसोड 123, कड़ी 123, Ch 123, Chapter 123
    match = re.search(r'(?:[eE]p(?:isode)?|[eE]p|\b[eE]\b|एपिसोड|कड़ी|kadi|ch(?:apter)?|अध्याय)[\.\-\s_:#]*(\d+)', text, re.IGNORECASE)
    if match:
        return int(match.group(1))

    # 2. Match patterns like "E 3062" or "E-3062" or "#3062"
    match = re.search(r'(?:\b[eE][\.\-\s_]+|#)(\d+)\b', text)
    if match:
        return int(match.group(1))

    # 3. Match numbers in parentheses/brackets e.g. (1065) or [1065] (exclude 4-digit years)
    for m in re.finditer(r'[\(\[\{](\d+)[\)\]\}]', text):
        val = int(m.group(1))
        if val not in [2020, 2021, 2022, 2023, 2024, 2025, 2026, 2027]:
            return val

    # 4. Fallback: match standalone numbers, filtering out known bitrates, years, resolutions
    ignored_numbers = {64, 128, 192, 256, 320, 44100, 48000, 2020, 2021, 2022, 2023, 2024, 2025, 2026, 2027, 2028, 2029, 2030, 1080, 720, 480, 360}
    numbers = re.findall(r'\b\d+\b', text)
    if numbers:
        valid_eps = [int(n) for n in numbers if int(n) < 50000 and int(n) not in ignored_numbers]
        if valid_eps:
            return valid_eps[-1]

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

async def check_and_update_single_story(client: Client, story: dict, db) -> dict:
    """
    Checks Telegram source channel for new messages using peer resolution + multi-batch scan:
    - Resolves channel peer with get_chat
    - Scans forward in batches of 150 IDs to find all new audio messages
    - Extracts episode number from file_name / audio title / caption
    - Never decreases episode count or end_id
    - Synchronizes ongoing part in parts list
    """
    story_id = str(story.get("_id", ""))
    story_name = story.get("story_name_en") or story.get("story_name") or story.get("title") or "Story"
    raw_source = story.get("source") or story.get("source_channel") or story.get("channel_id")

    if not raw_source:
        return {"updated": False, "reason": "no_source"}

    source_id = raw_source
    try:
        source_id = int(source_id)
        if source_id > 0 and len(str(source_id)) >= 9:
            source_id = int(f"-100{source_id}")
    except Exception:
        pass

    # Find client to query source channel (try mgmt_bot or market_clients fallback)
    clients_to_try = [client] if client else []
    try:
        from plugins.userbot.market_seller import market_clients
        for mc in market_clients.values():
            if mc not in clients_to_try:
                clients_to_try.append(mc)
    except Exception:
        pass

    active_client = None
    for cli in clients_to_try:
        try:
            await cli.get_chat(source_id)
            active_client = cli
            break
        except Exception:
            continue

    if not active_client:
        return {"updated": False, "reason": "no_active_client"}

    # 2. Determine current base end_id and current episodes
    story_end_id = int(story.get("end_id") or story.get("end_message_id") or 0)
    parts_end_ids = [int(p.get("end_id") or 0) for p in (story.get("parts") or []) if p.get("end_id")]
    base_end_id = max([story_end_id] + parts_end_ids + [0])

    curr_eps_raw = str(story.get("episodes") or "0")
    m_cur = re.search(r"\d+", curr_eps_raw)
    curr_eps_num = int(m_cur.group(0)) if m_cur else 0

    # 3. Scan forward in batches of 150 IDs (up to 1500 messages ahead)
    scan_cursor = base_end_id + 1
    consecutive_empty = 0
    highest_ep_num = curr_eps_num
    last_found_media_id = base_end_id
    all_new_audio_ids = []

    while consecutive_empty < 2 and scan_cursor <= base_end_id + 1500:
        ids_to_fetch = list(range(scan_cursor, scan_cursor + 150))
        try:
            msgs = await active_client.get_messages(source_id, ids_to_fetch)
        except FloodWait as fw:
            await asyncio.sleep(fw.value)
            break
        except Exception as e:
            logger.debug(f"[Premium Monitor] Error fetching msgs for {source_id}: {e}")
            break

        batch_had_any_msg = False
        for msg in msgs:
            if not msg or msg.empty:
                continue
            batch_had_any_msg = True

            # Check if this is an audio file or audio document
            is_audio = bool(msg.audio or msg.voice or (msg.document and getattr(msg.document, "mime_type", "").startswith("audio/")))
            if is_audio:
                all_new_audio_ids.append(msg.id)
                if msg.id > last_found_media_id:
                    last_found_media_id = msg.id

                fname = getattr(msg.audio or msg.document or msg.voice, "file_name", "")
                caption = msg.caption or ""
                title = getattr(msg.audio, "title", "") if msg.audio else ""

                ep_num = await _extract_episode_number(fname, caption, title)
                if ep_num and ep_num > highest_ep_num:
                    highest_ep_num = ep_num

        if not batch_had_any_msg:
            consecutive_empty += 1
        else:
            consecutive_empty = 0

        scan_cursor += 150

    # If no new media found, nothing to update
    if last_found_media_id <= base_end_id and highest_ep_num <= curr_eps_num:
        return {"updated": False, "reason": "up_to_date"}

    new_end_id = max(base_end_id, last_found_media_id)
    final_episodes = max(curr_eps_num, highest_ep_num)

    # 4. Update DB immediately
    update_data = {
        "end_id": new_end_id,
        "end_message_id": new_end_id
    }
    if final_episodes > 0:
        update_data["episodes"] = str(final_episodes)

    # Append new audio msg ids to valid_file_ids
    if story.get("valid_file_ids") is not None and all_new_audio_ids:
        existing_val_ids = story.get("valid_file_ids") or []
        update_data["valid_file_ids"] = sorted(list(set(existing_val_ids + all_new_audio_ids)))
        update_data["file_count"] = len(update_data["valid_file_ids"])

    # 5. Synchronize ongoing part in parts list
    raw_parts = story.get("parts") or story.get("story_parts") or []
    if isinstance(raw_parts, list) and len(raw_parts) > 0:
        updated_parts = []
        found_ongoing = False
        for idx, p in enumerate(raw_parts):
            p_copy = dict(p)
            b_val = str(p_copy.get("badge") or p_copy.get("badge_type") or "").lower()
            is_ong = bool(p_copy.get("is_ongoing") or b_val == "ongoing")
            is_last = (idx == len(raw_parts) - 1)

            if is_ong or (not found_ongoing and is_last and str(story.get("status", "")).lower() == "ongoing"):
                found_ongoing = True
                p_copy["end_id"] = new_end_id
                p_copy["is_ongoing"] = True
                if not p_copy.get("badge"):
                    p_copy["badge"] = "ongoing"

                curr_p_eps = str(p_copy.get("episodes") or "").strip()
                m_p = re.search(r"(\d+)", curr_p_eps)
                if m_p:
                    start_ep = int(m_p.group(1))
                    if final_episodes >= start_ep:
                        p_copy["episodes"] = f"{start_ep}-{final_episodes}"
                    else:
                        p_copy["episodes"] = str(final_episodes)
                else:
                    p_copy["episodes"] = str(final_episodes)

                # Sync part valid_file_ids if present
                if "valid_file_ids" in update_data:
                    p_start_id = int(p_copy.get("start_id") or 0)
                    p_copy["valid_file_ids"] = [mid for mid in update_data["valid_file_ids"] if p_start_id <= mid <= new_end_id]
                    p_copy["file_count"] = len(p_copy["valid_file_ids"])

            updated_parts.append(p_copy)
        update_data["parts"] = updated_parts

    await db.db.premium_stories.update_one(
        {"_id": story["_id"]},
        {"$set": update_data}
    )

    logger.info(
        f"[Premium Monitor] ✅ Updated DB for '{story_name}' -> "
        f"end_id: {base_end_id} -> {new_end_id}, "
        f"episodes: {curr_eps_num} -> {final_episodes}"
    )

    # Queue delayed announcement if new audio was added
    if final_episodes > curr_eps_num or len(all_new_audio_ids) > 0:
        _PENDING_NOTIFICATIONS[story_id] = {
            "story_id": story_id,
            "end_id": new_end_id,
            "highest_ep_num": final_episodes,
            "story_name": story_name
        }

    # Clear Mini App API cache immediately
    try:
        import mini_app_api
        mini_app_api._stories_cache = None
    except Exception:
        pass

    return {
        "updated": True,
        "story_name": story_name,
        "old_end": base_end_id,
        "new_end": new_end_id,
        "old_eps": curr_eps_num,
        "new_eps": final_episodes
    }

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

    updated_list = []
    for story in stories:
        try:
            res = await check_and_update_single_story(bot, story, db)
            if res.get("updated"):
                updated_list.append(res)
        except Exception as e:
            logger.error(f"[Premium Monitor] Error checking story '{story.get('story_name_en', story.get('story_name', 'Unknown'))}': {e}", exc_info=True)

    return {
        "total": len(stories),
        "updated_count": len(updated_list),
        "updated_stories": updated_list
    }

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

        # Poll every 60 seconds
        await asyncio.sleep(60)


