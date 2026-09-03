import os
import sys
import asyncio
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from database import db
from config import Config
from pyrogram import Client
from utils import scan_and_index_story

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("DeadFileCleaner")


async def clean_all_stories(force_full_scan: bool = False, ongoing_only: bool = False, target_story: str = None):
    print("=" * 90)
    print("🧹 ARYA PREMIUM — AUTOMATIC DEAD FILE CLEANER & CHUNK REPAIR ENGINE")
    print("=" * 90)

    # 1. Connect bot client
    all_clients = []
    mgmt_bot = Client(
        name="mgmt_bot_cleaner",
        api_id=Config.API_ID,
        api_hash=Config.API_HASH,
        bot_token=Config.MGMT_BOT_TOKEN,
        in_memory=True
    )
    await mgmt_bot.start()
    all_clients.append(mgmt_bot)
    print(f"✅ Connected Management Bot: @{mgmt_bot.me.username}")

    try:
        store_bots_docs = await db.db.premium_bots.find({"status": {"$ne": "inactive"}}).to_list(length=None)
        for b in store_bots_docs:
            tok = b.get("bot_token") or b.get("token")
            uname = b.get("bot_username") or b.get("username")
            if tok and tok != getattr(Config, "MGMT_BOT_TOKEN", ""):
                try:
                    cli = Client(
                        name=f"bot_{uname or 'store'}_cleaner",
                        api_id=Config.API_ID,
                        api_hash=Config.API_HASH,
                        bot_token=tok,
                        in_memory=True
                    )
                    await cli.start()
                    all_clients.append(cli)
                except Exception:
                    pass
    except Exception:
        pass

    # 2. Query stories
    query = {}
    if ongoing_only:
        query = {"$or": [{"status": {"$in": ["Ongoing", "ongoing", "ONGOING"]}}, {"parts.badge": "ongoing"}, {"parts.is_ongoing": True}]}
    elif target_story:
        import re
        clean_target = target_story.strip()
        escaped_pattern = re.escape(clean_target)
        query = {
            "$or": [
                {"story_name_en": {"$regex": escaped_pattern, "$options": "i"}},
                {"story_name": {"$regex": escaped_pattern, "$options": "i"}},
                {"title": {"$regex": escaped_pattern, "$options": "i"}},
                {"clean_title": {"$regex": re.sub(r'[^a-zA-Z0-9]', '', clean_target.lower()), "$options": "i"}}
            ]
        }

    stories = await db.db.premium_stories.find(query).sort("_id", -1).to_list(length=None)
    print(f"\n📚 Stories to Inspect: {len(stories)}\n")

    repaired_count = 0
    skipped_count = 0
    no_access_count = 0

    for idx, story in enumerate(stories, 1):
        s_id = str(story.get("_id"))
        s_title = story.get("story_name_en") or story.get("story_name") or story.get("title") or "Story"
        raw_source = story.get("source") or story.get("source_channel") or story.get("channel_id")
        start_id = story.get("start_id")
        end_id = story.get("end_id") or story.get("end_message_id")
        valid_file_ids = story.get("valid_file_ids")

        if not raw_source or not start_id or not end_id:
            continue

        try:
            source_id = int(raw_source)
            if source_id > 0 and len(str(source_id)) >= 9:
                source_id = int(f"-100{source_id}")
            start_id = int(start_id)
            end_id = int(end_id)
        except Exception:
            continue

        # Check if story is already 100% clean and valid
        raw_range_len = (end_id - start_id) + 1
        is_already_clean = (
            not force_full_scan
            and not target_story
            and isinstance(valid_file_ids, list)
            and len(valid_file_ids) > 0
            and max(valid_file_ids) >= end_id
            and min(valid_file_ids) >= start_id
            and story.get("file_count") == len(valid_file_ids)
        )

        if is_already_clean:
            # Fast skip clean stories in 0.0001s!
            skipped_count += 1
            continue

        # Needs repair/scan: find active bot client
        active_client = None
        for cli in all_clients:
            try:
                await cli.get_chat(source_id)
                active_client = cli
                break
            except Exception:
                continue

        if not active_client:
            no_access_count += 1
            continue

        print(f"[{idx}] Repairing '{s_title}' (Channel: {source_id}, Range: {start_id}..{end_id})...")
        try:
            fresh_valid_ids = await scan_and_index_story(active_client, story, save_to_db=True, db=db)
            dead_count = raw_range_len - len(fresh_valid_ids)
            print(f"    🟢 Repaired: {len(fresh_valid_ids)} valid files indexed (Removed {dead_count} dead/empty message gaps).")
            repaired_count += 1
        except Exception as err:
            print(f"    ❌ Error scanning story: {err}")

    for cli in all_clients:
        try:
            await cli.stop()
        except Exception:
            pass

    print("\n" + "=" * 90)
    print("📊 CLEANER ENGINE SUMMARY:")
    print(f"  🟢 Stories Repaired & Re-Indexed : {repaired_count}")
    print(f"  ⚡ Clean Stories Fast-Skipped   : {skipped_count}")
    print(f"  ⚠️ Channels Bot Cannot Access   : {no_access_count}")
    print("=" * 90)


if __name__ == "__main__":
    force = "--force" in sys.argv or "-f" in sys.argv
    ongoing = "--ongoing" in sys.argv or "-o" in sys.argv
    story_arg = None
    if "--story" in sys.argv:
        try:
            s_idx = sys.argv.index("--story")
            if s_idx + 1 < len(sys.argv):
                story_arg = sys.argv[s_idx + 1]
        except Exception:
            pass
    asyncio.run(clean_all_stories(force_full_scan=force, ongoing_only=ongoing, target_story=story_arg))
