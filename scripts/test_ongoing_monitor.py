import os
import sys
import asyncio
import logging

# Setup import paths
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from database import db
from config import Config
from pyrogram import Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("OngoingTester")


async def run_live_test():
    print("=" * 90)
    print("🔍 ARYA ONGOING STORY LIVE MONITOR — DIRECT TERMINAL TESTER & SYNC")
    print("=" * 90)
    print(f"📁 Directory: {os.getcwd()}")
    db_name = getattr(Config, "DATABASE_NAME", "Unknown")
    db_url = getattr(Config, "DATABASE_URL", "Unknown")
    host = db_url.split('@')[-1] if '@' in db_url else 'localhost'
    print(f"🗄️ Database : {db_name} ({host})\n")

    # Connect Mgmt Bot
    mgmt_bot = Client(
        name="mgmt_bot_test",
        api_id=Config.API_ID,
        api_hash=Config.API_HASH,
        bot_token=Config.MGMT_BOT_TOKEN,
        in_memory=True
    )

    print("📡 Connecting Telegram Bot to verify channel permissions...")
    try:
        await mgmt_bot.start()
        me = await mgmt_bot.get_me()
        print(f"✅ Successfully connected as @{me.username} (ID: {me.id})\n")
    except Exception as e:
        print(f"❌ Failed to connect Bot: {e}")
        return

    # Query ongoing stories
    query = {
        "$or": [
            {"status": {"$in": ["Ongoing", "ongoing", "ONGOING"]}},
            {"parts.badge": "ongoing"},
            {"parts.is_ongoing": True},
            {"is_ongoing": True},
            {"status": {"$nin": ["Completed", "completed", "COMPLETED", "Unfinished", "unfinished", "Stucked", "stucked"]}}
        ],
        "is_completed": {"$ne": True}
    }

    stories = await db.db.premium_stories.find(query).to_list(length=None)
    print(f"📋 Found {len(stories)} ongoing stories in MongoDB:\n")

    for idx, s in enumerate(stories, 1):
        s_id = str(s.get("_id"))
        s_title = s.get("story_name_en") or s.get("story_name") or s.get("title") or "Untitled"
        raw_source = s.get("source") or s.get("source_channel") or s.get("channel_id")
        current_end_id = s.get("end_id") or s.get("end_message_id") or 0
        current_episodes = s.get("episodes") or "0"
        parts = s.get("parts") or []

        print(f"[{idx}] Story: '{s_title}' (ID: {s_id})")
        print(f"    • Source Channel in DB : {raw_source}")
        print(f"    • Current End ID in DB  : {current_end_id}")
        print(f"    • Current Episodes in DB: {current_episodes}")
        print(f"    • Parts configured      : {len(parts)}")

        if not raw_source:
            print("    ❌ FAILED: No source channel ID configured in story!")
            print("-" * 90)
            continue

        # Format channel ID
        source_id = raw_source
        try:
            source_id = int(source_id)
            if source_id > 0 and len(str(source_id)) >= 9:
                source_id = int(f"-100{source_id}")
        except Exception:
            pass

        print(f"    • Formatted Source ID   : {source_id}")

        # Test channel access
        channel_last_id = 0
        try:
            async for last_msg in mgmt_bot.get_chat_history(source_id, limit=1):
                channel_last_id = last_msg.id
                break
            print(f"    • Real Telegram Channel Last Msg ID: {channel_last_id}")
        except Exception as e:
            print(f"    ❌ TELEGRAM ERROR accessing channel {source_id}: {type(e).__name__} - {e}")
            print("       (Note: The bot must be an Admin/Member in this private source channel to read messages!)")
            print("-" * 90)
            continue

        # Check if new messages exist and update
        try:
            from plugins.premium_live_monitor import check_and_update_single_story
            updated = await check_and_update_single_story(mgmt_bot, s, db)
            if updated:
                # Re-fetch from DB to verify updated values
                refetched = await db.db.premium_stories.find_one({"_id": s["_id"]})
                new_end = refetched.get("end_id")
                new_eps = refetched.get("episodes")
                print(f"    🟢 SUCCESS: Story updated in DB!")
                print(f"       -> New End ID  : {new_end} (was {current_end_id})")
                print(f"       -> New Episodes: {new_eps} (was {current_episodes})")
                new_parts = refetched.get("parts") or []
                for p in new_parts:
                    if p.get("is_ongoing") or str(p.get("badge", "")).lower() == "ongoing":
                        print(f"       -> Ongoing Part '{p.get('name')}' Updated: Episodes '{p.get('episodes')}', End ID '{p.get('end_id')}'")
            else:
                if channel_last_id <= int(current_end_id or 0):
                    print(f"    ⚪ UP TO DATE: Channel has no newer messages (Channel: {channel_last_id} <= DB: {current_end_id}).")
                else:
                    print(f"    ⚠️ Messages {current_end_id}..{channel_last_id} scanned, but no audio messages with episode tags were found.")
        except Exception as ex:
            print(f"    ❌ Error during sync: {ex}")

        print("-" * 90)

    try:
        await mgmt_bot.stop()
    except Exception:
        pass

    print("\n✅ Ongoing monitor test and sync complete!")
    print("=" * 90)


if __name__ == "__main__":
    asyncio.run(run_live_test())
