import asyncio
import logging
import re
import os
from pyrogram import Client
from pyrogram.errors import FloodWait

logger = logging.getLogger(__name__)

# Track pending notifications: story_id -> { "end_id": int, "highest_ep_num": int, "story_name": str }
_PENDING_NOTIFICATIONS = {}

async def _extract_episode_number(file_name: str, caption: str):
    # Strip file extension if present to avoid matching '.mp3' as a number
    fname = ""
    if file_name:
        fname, _ = os.path.splitext(file_name)
        
    text = f"{fname} {caption or ''}".strip()
    if not text:
        return None
        
    # Match patterns like "Episode 12", "Ep 12", "E 12", "Episode-12", "Ep. 12", "E12"
    match = re.search(r'(?:[eE]p(?:isode)?[\.\-\s_]*|[eE][\.\-\s_]*)(\d+)', text)
    if match:
        return int(match.group(1))
    
    # Fallback: Just extract the last standalone number found in the name
    numbers = re.findall(r'\b\d+\b', text)
    if numbers:
        return int(numbers[-1])
        
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

    story_name = pending_info["story_name"]
    ep_num = pending_info["highest_ep_num"]
    total_eps = ep_num # Assuming the latest episode number is the total count
    
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
            # 1. Fetch all ongoing stories
            query = {
                "status": {"$ne": "Completed"},
                "is_completed": {"$ne": True},
                "channel_id": {"$exists": True, "$ne": None}
            }
            stories = await db.db.premium_stories.find(query).to_list(length=None)
            
            for story in stories:
                story_id = str(story["_id"])
                channel_id = story.get("channel_id")
                end_id = story.get("end_id") or story.get("end_message_id")
                
                if not channel_id or not end_id:
                    continue
                    
                try:
                    end_id = int(end_id)
                except ValueError:
                    continue
                    
                # Fetch next 100 messages after end_id
                ids_to_fetch = list(range(end_id + 1, end_id + 101))
                
                try:
                    msgs = await bot.get_messages(channel_id, ids_to_fetch)
                except FloodWait as fw:
                    await asyncio.sleep(fw.value)
                    continue
                except Exception as e:
                    logger.warning(f"[Premium Monitor] Error fetching msgs for channel {channel_id}: {e}")
                    continue
                
                highest_valid_id = end_id
                highest_ep_num = -1
                
                for msg in msgs:
                    if msg.empty:
                        continue
                        
                    highest_valid_id = max(highest_valid_id, msg.id)
                        
                    if not msg.audio and not msg.document and not msg.voice:
                        continue
                        
                    fname = getattr(msg.audio or msg.document or msg.voice, "file_name", "")
                    caption = msg.caption or ""
                    
                    ep_num = await _extract_episode_number(fname, caption)
                    
                    if ep_num and ep_num > highest_ep_num:
                        highest_ep_num = ep_num
                        
                if highest_valid_id > end_id:
                    # New files were uploaded! Update DB immediately.
                    update_data = {
                        "end_id": highest_valid_id, 
                        "end_message_id": highest_valid_id
                    }
                    if highest_ep_num > -1:
                        update_data["episodes"] = str(highest_ep_num)
                        
                    await db.db.premium_stories.update_one(
                        {"_id": story["_id"]},
                        {"$set": update_data}
                    )
                    
                    story_name = story.get("story_name_en") or story.get("story_name") or story.get("title") or "Story"
                    
                    # Mark as pending. We will NOT send the announcement yet.
                    _PENDING_NOTIFICATIONS[story_id] = {
                        "end_id": highest_valid_id,
                        "highest_ep_num": highest_ep_num,
                        "story_name": story_name
                    }
                    logger.info(f"[Premium Monitor] Updated DB for '{story_name}' -> end_id: {highest_valid_id}. Queued for announcement.")
                    
                else:
                    # No new files uploaded in this 5-minute window!
                    # Check if this story has a pending announcement
                    if story_id in _PENDING_NOTIFICATIONS:
                        pending_info = _PENDING_NOTIFICATIONS[story_id]
                        # If the DB's end_id matches our pending end_id, it means
                        # no new files arrived since our last update 5 mins ago. 
                        # Time to send the announcement!
                        if pending_info["end_id"] == end_id:
                            await send_announcement(bot, pending_info)
                            logger.info(f"[Premium Monitor] Sent delayed announcement for '{pending_info['story_name']}'")
                            del _PENDING_NOTIFICATIONS[story_id]
                    
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
