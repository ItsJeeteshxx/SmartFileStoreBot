import asyncio
import logging
import re
import os
from pyrogram import Client
from pyrogram.errors import FloodWait

logger = logging.getLogger(__name__)

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
    # We use negative lookahead/lookbehind to ensure it's not a year like 2024 if there are other numbers
    numbers = re.findall(r'\b\d+\b', text)
    if numbers:
        return int(numbers[-1])
        
    return None

async def start_premium_live_monitor(bot: Client):
    """
    Background job that runs every 10 minutes to poll source channels 
    for new audio episodes of ongoing premium stories.
    """
    logger.info("Starting Premium Live Monitor (10-minute polling loop)...")
    
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
                channel_id = story.get("channel_id")
                # Try multiple fields since end_id might be stored differently
                end_id = story.get("end_id") or story.get("end_message_id")
                
                # We need a valid channel_id and end_id to start fetching
                if not channel_id or not end_id:
                    continue
                    
                # Ensure end_id is an integer
                try:
                    end_id = int(end_id)
                except ValueError:
                    continue
                    
                # Fetch next 100 messages after end_id
                # This ensures we batch-fetch without API limits.
                ids_to_fetch = list(range(end_id + 1, end_id + 101))
                
                try:
                    msgs = await bot.get_messages(channel_id, ids_to_fetch)
                except FloodWait as fw:
                    await asyncio.sleep(fw.value)
                    continue
                except Exception as e:
                    # Could be ChatAdminRequired if bot lost admin rights, etc.
                    logger.warning(f"[Premium Monitor] Error fetching msgs for channel {channel_id}: {e}")
                    continue
                
                highest_valid_id = end_id
                highest_ep_num = -1
                
                for msg in msgs:
                    # Empty messages mean that ID hasn't been posted yet, or was deleted
                    if msg.empty:
                        continue
                        
                    # We always advance highest_valid_id for ANY non-empty message 
                    # so we don't get stuck if there's a text/photo message in between!
                    highest_valid_id = max(highest_valid_id, msg.id)
                        
                    # We ONLY care about audio files for episode extraction
                    if not msg.audio and not msg.document and not msg.voice:
                        continue
                        
                    fname = getattr(msg.audio or msg.document or msg.voice, "file_name", "")
                    caption = msg.caption or ""
                    
                    # Some documents aren't audio.
                    # But if the channel is purely for stories, we extract from any media with a name
                    ep_num = await _extract_episode_number(fname, caption)
                    
                    if ep_num and ep_num > highest_ep_num:
                        highest_ep_num = ep_num
                        
                # If we found messages (even non-audio) we advance end_id to avoid rescanning them forever
                if highest_valid_id > end_id:
                    update_data = {
                        "end_id": highest_valid_id, 
                        "end_message_id": highest_valid_id
                    }
                    
                    # Only update episodes count if we found a valid episode number in an audio file
                    if highest_ep_num > -1:
                        update_data["episodes"] = str(highest_ep_num)
                        
                    await db.db.premium_stories.update_one(
                        {"_id": story["_id"]},
                        {"$set": update_data}
                    )
                    
                    logger.info(f"[Premium Monitor] Updated Story '{story.get('story_name_en') or story.get('_id')}' -> end_id: {highest_valid_id}, eps: {highest_ep_num if highest_ep_num > -1 else 'unchanged'}")
                    
            # Clear FastAPI cache so UI updates instantly
            try:
                import mini_app_api
                mini_app_api._stories_cache = None
            except ImportError:
                pass
                
        except Exception as e:
            logger.error(f"[Premium Monitor] Loop error: {e}")
            
        # Wait 10 minutes (600 seconds) before next poll
        await asyncio.sleep(600)
