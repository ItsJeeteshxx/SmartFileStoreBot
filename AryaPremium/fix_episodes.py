import asyncio
import os
import sys

from pyrogram import Client
from config import Config
from database import db
from plugins.premium_live_monitor import _extract_episode_number

async def fix_garbage_episodes():
    print("==================================================")
    print("   FIXING GARBAGE EPISODE NUMBERS")
    print("==================================================")
    await db.connect()
    
    print(f"Starting Pyrogram Client for Mgmt Bot...")
    bot = Client("AryaPremiumFixer", api_id=Config.API_ID, api_hash=Config.API_HASH, bot_token=Config.MGMT_BOT_TOKEN, in_memory=True)
    await bot.start()
    
    query = {"source": {"$exists": True, "$ne": None}}
    stories = await db.db.premium_stories.find(query).to_list(length=None)
    print(f"Found {len(stories)} total stories to verify.")
    
    fixed_count = 0
    
    for story in stories:
        story_name = story.get('story_name_en') or story.get('story_name') or 'Unknown'
        source_id = story.get("source")
        end_id = story.get("end_id") or story.get("end_message_id")
        current_eps = story.get("episodes")
        
        if not source_id or not end_id:
            continue
            
        try:
            source_id = int(source_id)
            end_id = int(end_id)
        except (ValueError, TypeError):
            continue
            
        try:
            # Fetch the actual last message
            msg = await bot.get_messages(source_id, end_id)
            if not msg or msg.empty:
                continue
                
            if not msg.audio and not msg.document and not msg.voice:
                continue
                
            fname = getattr(msg.audio or msg.document or msg.voice, "file_name", "")
            caption = msg.caption or ""
            title = getattr(msg.audio, "title", "") if msg.audio else ""
            
            # Use our newly fixed regex logic
            real_ep_num = await _extract_episode_number(fname, caption, title)
            
            if real_ep_num and str(real_ep_num) != str(current_eps):
                print(f"[FIXING] {story_name}")
                print(f"   -> Garbage Episode: {current_eps}")
                print(f"   -> Real Episode: {real_ep_num}")
                
                await db.db.premium_stories.update_one(
                    {"_id": story["_id"]},
                    {"$set": {"episodes": str(real_ep_num)}}
                )
                fixed_count += 1
            else:
                pass # Already correct or couldn't extract
                
        except Exception as e:
            print(f"Error checking {story_name}: {e}")

    # Clear cache so UI reflects changes immediately
    try:
        import mini_app_api
        mini_app_api._stories_cache = None
    except ImportError:
        pass

    await bot.stop()
    print("==================================================")
    print(f"DONE! Successfully fixed {fixed_count} stories.")
    print("==================================================")

if __name__ == "__main__":
    asyncio.run(fix_garbage_episodes())
