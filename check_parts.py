import asyncio, sys, os

# Fix path for ubuntu user
BASE = os.path.expanduser("~/bot/TryAryaForwardBot")
sys.path.insert(0, BASE)
os.chdir(BASE)

from motor.motor_asyncio import AsyncIOMotorClient
from AryaPremium.config import Config

async def check():
    client = AsyncIOMotorClient(Config.MONGO_URI)
    db = client[Config.DATABASE_NAME]

    # Find story by partial name match
    story = await db.premium_stories.find_one({"story_name_en": {"$regex": "Super Yoddha", "$options": "i"}})
    if not story:
        story = await db.premium_stories.find_one({"story_name_hi": {"$regex": "Super Yoddha", "$options": "i"}})
    if not story:
        story = await db.premium_stories.find_one({"enable_parts": True})
        print("NOTE: Super Yoddha not found, showing first parts-enabled story instead")

    if story:
        print("=== STORY INFO ===")
        print("Name EN:", story.get("story_name_en", "N/A"))
        print("Name HI:", story.get("story_name_hi", "N/A"))
        print("enable_parts:", story.get("enable_parts"))
        print("parts count:", len(story.get("parts", [])))
        parts = story.get("parts", [])
        if parts:
            print("First part:", parts[0])
        print()
        print("=== ALL KEYS IN DOCUMENT ===")
        for k in story.keys():
            v = story[k]
            if isinstance(v, list):
                print(f"  {k}: [list of {len(v)} items]")
            else:
                print(f"  {k}: {v}")
    else:
        print("No story found at all!")

    count = await db.premium_stories.count_documents({"enable_parts": True})
    print(f"\nTotal stories with enable_parts=True: {count}")

asyncio.run(check())
