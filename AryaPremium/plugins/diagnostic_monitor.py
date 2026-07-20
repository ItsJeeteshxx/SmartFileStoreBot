import asyncio
import os
import sys

# Ensure project root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from pyrogram import Client
from config import Config
from database import db
import logging

logging.basicConfig(level=logging.WARNING)

async def run_diagnostic():
    print("==================================================")
    print("   PREMIUM LIVE MONITOR - DIAGNOSTIC TOOL")
    print("==================================================")
    print("Connecting to MongoDB...")
    await db.connect()
    
    # We must use the exact session string or token as the main bot
    print(f"Starting Pyrogram Client for Bot ID: {Config.MGMT_BOT_TOKEN.split(':')[0]}...")
    bot = Client("AryaPremiumDiagnostic", api_id=Config.API_ID, api_hash=Config.API_HASH, bot_token=Config.MGMT_BOT_TOKEN, in_memory=True)
    await bot.start()
    me = await bot.get_me()
    print(f"Bot authenticated successfully as: @{me.username}")
    
    query = {
        "status": {"$nin": ["Completed", "Unfinished", "Stucked"]},
        "is_completed": {"$ne": True},
        "source": {"$exists": True, "$ne": None}
    }
    print("Fetching ongoing stories from database...")
    stories = await db.db.premium_stories.find(query).to_list(length=None)
    print(f"Found {len(stories)} ongoing stories.")
    print("--------------------------------------------------")
    
    for story in stories:
        story_name = story.get('story_name_en') or story.get('story_name') or 'Unknown'
        story_id = str(story["_id"])
        source_id = story.get("source")
        end_id = story.get("end_id") or story.get("end_message_id")
        
        print(f"\n[STORY] {story_name}")
        print(f" - Story ID: {story_id}")
        print(f" - Source Channel ID: {source_id}")
        print(f" - Current End ID in DB: {end_id}")
        
        try:
            source_id = int(source_id)
        except (ValueError, TypeError):
            print(f" [!] ERROR: Invalid source_id format ({source_id}). Skipping.")
            continue
            
        if not source_id:
            print(" [!] ERROR: source_id is empty/0. Skipping.")
            continue
            
        try:
            end_id = int(end_id) if end_id else 0
        except ValueError:
            end_id = 0
            
        channel_last_id = 0
        try:
            print(f" -> Fetching latest message ID from Telegram for channel {source_id}...")
            async for last_msg in bot.get_chat_history(source_id, limit=1):
                channel_last_id = last_msg.id
                break
            print(f" -> Success! Channel's absolute latest message ID is: {channel_last_id}")
        except Exception as e:
            print(f" [!!!] FATAL ERROR GETTING HISTORY: {e}")
            print(f"       Possible Reason: The bot @{me.username} is NOT an Admin in channel {source_id}.")
            continue
            
        if channel_last_id <= end_id:
            print(f" -> No new messages found. Channel latest ({channel_last_id}) <= DB end_id ({end_id})")
            continue
            
        if end_id == 0:
            end_id = max(0, channel_last_id - 100)
            print(f" -> Fast-forwarded end_id to {end_id} because DB end_id was 0.")
            
        fetch_end = min(end_id + 100, channel_last_id)
        ids_to_fetch = list(range(end_id + 1, fetch_end + 1))
        
        print(f" -> Fetching {len(ids_to_fetch)} messages from ID {end_id + 1} to {fetch_end}...")
        try:
            msgs = await bot.get_messages(source_id, ids_to_fetch)
            print(f" -> Success! Fetched {len(msgs)} messages.")
        except Exception as e:
            print(f" [!!!] FATAL ERROR FETCHING MESSAGES: {e}")
            continue
            
        valid_msgs = [m for m in msgs if not m.empty]
        print(f" -> Out of {len(msgs)} fetched, {len(valid_msgs)} are existing (not deleted).")
        
        audio_count = 0
        for msg in valid_msgs:
            if msg.audio or msg.document or msg.voice:
                audio_count += 1
                
        print(f" -> Found {audio_count} audio/document/voice files in this chunk.")
        if audio_count > 0:
            print(" -> This story is READY to be updated by the live monitor!")
        else:
            print(" -> No audio files found in this chunk. (Maybe text messages or deleted gaps?)")

    await bot.stop()
    print("\n==================================================")
    print("DIAGNOSTIC COMPLETE")
    print("==================================================")

if __name__ == "__main__":
    asyncio.run(run_diagnostic())
