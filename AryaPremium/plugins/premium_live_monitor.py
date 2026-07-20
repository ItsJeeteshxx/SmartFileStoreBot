import asyncio
import logging
import re
import os
from pyrogram import Client
from pyrogram.errors import FloodWait

logger = logging.getLogger(__name__)

# Track pending notifications: story_id -> { "end_id": int, "highest_ep_num": int, "story_name": str }
_PENDING_NOTIFICATIONS = {}

async def _extract_episode_number(file_name: str, caption: str, title: str = ""):
    # Strip file extension if present to avoid matching '.mp3' as a number
    fname = ""
    if file_name:
        fname, _ = os.path.splitext(file_name)
        
    text = f"{fname} {title or ''} {caption or ''}".strip()
    if not text:
        return None
        
    # 1. Match explicit episode labels (English and Hindi)
    # e.g., "Episode 3062", "Ep 3062", "एपिसोड 3062", "भाग 3062", "Part 3062", "Ch 3062", "E3062"
    match = re.search(r'(?:[eE]p(?:isode)?|एपिसोड|भाग|part|ch(?:apter)?|कड़ी|kadi)[\.\-\s_:]*(\d+)', text, re.IGNORECASE)
    if match:
        return int(match.group(1))
        
    # 2. Match "E 3062" or "E-3062" specifically
    match = re.search(r'\b[eE][\.\-\s_]+(\d+)\b', text)
    if match:
        return int(match.group(1))
    
    # 3. Fallback: extract standalone numbers
    numbers = re.findall(r'\b\d+\b', text)
    if numbers:
        # Filter out insanely large numbers (like 693759, which are likely IDs/hashes)
        valid_eps = [int(n) for n in numbers if int(n) < 50000]
        if valid_eps:
            # Return the last valid number (usually closest to the extension)
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

    import html
    story_name = html.escape(str(pending_info["story_name"]))
    ep_num = pending_info["highest_ep_num"]
    total_eps = str(ep_num) # Assuming the latest episode number is the total count
    
    # If we couldn't extract an episode number, we skip announcement (or say "New")
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

async def start_premium_live_monitor(bot: Client):
    """
    Background job that runs every 5 minutes to poll source channels 
    for new audio episodes of ongoing premium stories.
    """
    logger.info("Starting Premium Live Monitor (5-minute polling loop)...")
    
    # Allow bot to fully connect before first poll
    await asyncio.sleep(10)
    
    while True:
        try:
            from database import db
            # 1. Fetch all ongoing stories (exclude Completed, Unfinished, and Stucked)
            query = {
                "status": {"$nin": ["Completed", "Unfinished", "Stucked"]},
                "is_completed": {"$ne": True},
                "source": {"$exists": True, "$ne": None}
            }
            stories = await db.db.premium_stories.find(query).to_list(length=None)
            
            for story in stories:
                story_id = str(story["_id"])
                source_id = story.get("source")
                end_id = story.get("end_id") or story.get("end_message_id")
                
                try:
                    source_id = int(source_id)
                except (ValueError, TypeError):
                    pass
                    
                if not source_id:
                    continue
                    
                try:
                    end_id = int(end_id) if end_id else 0
                except ValueError:
                    end_id = 0
                    
                # 1. Fetch the next chunk of messages (up to 200 max allowed by Pyrogram)
                # We use 200 to ensure we skip over any long chains of text/deleted messages
                fetch_start = end_id + 1
                fetch_end = end_id + 200
                ids_to_fetch = list(range(fetch_start, fetch_end + 1))
                
                try:
                    msgs = await bot.get_messages(source_id, ids_to_fetch)
                except FloodWait as fw:
                    await asyncio.sleep(fw.value)
                    continue
                except Exception as e:
                    logger.warning(f"[Premium Monitor] Error fetching msgs for channel {source_id}: {e}")
                    continue
                
                highest_ep_num = -1
                last_audio_id = -1
                
                for msg in msgs:
                    if msg.empty:
                        continue
                        
                    if not msg.audio and not msg.document and not msg.voice:
                        continue
                        
                    # ONLY advance ID for actual media files (so we don't point end_id to a text message)
                    if msg.id > last_audio_id:
                        last_audio_id = msg.id
                        
                    fname = getattr(msg.audio or msg.document or msg.voice, "file_name", "")
                    caption = msg.caption or ""
                    
                    # Audio files have a specific 'title' attribute which is what Telegram displays in the player.
                    # We MUST include this because often 'Ep 123' is in the title, while file_name is a raw ID.
                    title = getattr(msg.audio, "title", "") if msg.audio else ""
                    
                    ep_num = await _extract_episode_number(fname, caption, title)
                    
                    if ep_num and ep_num > highest_ep_num:
                        highest_ep_num = ep_num
                        
                # If no audio messages were found in this chunk, we assume we hit the end
                # (or we hit a massive wall of text messages, which we will just retry next poll)
                if last_audio_id == -1:
                    # Check if this story has a pending announcement that we can now safely send
                    if story_id in _PENDING_NOTIFICATIONS:
                        pending_info = _PENDING_NOTIFICATIONS[story_id]
                        if pending_info["end_id"] == end_id:
                            await send_announcement(bot, pending_info)
                            logger.info(f"[Premium Monitor] Sent delayed announcement for '{pending_info['story_name']}'")
                            del _PENDING_NOTIFICATIONS[story_id]
                    continue
                        
                # We advance end_id to the last_audio_id we found.
                new_end_id = last_audio_id
                
                # 5. Update DB immediately
                update_data = {
                    "end_id": new_end_id, 
                    "end_message_id": new_end_id
                }
                if highest_ep_num > -1:
                    update_data["episodes"] = str(highest_ep_num)
                    
                await db.db.premium_stories.update_one(
                    {"_id": story["_id"]},
                    {"$set": update_data}
                )
                
                story_name = story.get("story_name_en") or story.get("story_name") or story.get("title") or "Story"
                
                # Mark as pending for delayed announcement ONLY if we found audio episodes
                if highest_ep_num > -1:
                    _PENDING_NOTIFICATIONS[story_id] = {
                        "end_id": new_end_id,
                        "highest_ep_num": highest_ep_num,
                        "story_name": story_name
                    }
                    logger.info(f"[Premium Monitor] Updated DB for '{story_name}' -> end_id: {new_end_id}. Queued for announcement.")
                else:
                    # If we only scanned text/deleted messages, we carry over pending if it exists
                    if story_id in _PENDING_NOTIFICATIONS:
                        _PENDING_NOTIFICATIONS[story_id]["end_id"] = new_end_id
                    logger.info(f"[Premium Monitor] Updated DB for '{story_name}' -> end_id: {new_end_id} (No audio found).")
                    
            # Clear FastAPI cache so UI updates instantly
            try:
                import mini_app_api
                mini_app_api._stories_cache = None
            except ImportError:
                pass
                
        except Exception as e:
            logger.error(f"[Premium Monitor] Loop error: {e}")
            
        # Wait 5 minutes (300 seconds) before next poll
        await asyncio.sleep(300)
