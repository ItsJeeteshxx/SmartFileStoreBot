import asyncio, sys, os

BASE = os.path.expanduser("~/bot/TryAryaForwardBot/AryaPremium")
sys.path.insert(0, BASE)
os.chdir(BASE)

from motor.motor_asyncio import AsyncIOMotorClient
from config import Config

async def check():
    client = AsyncIOMotorClient(Config.MONGO_URI)
    db = client[Config.DATABASE_NAME]

    names = ["His Secret Fortune", "Qaidi Doctor", "Shiva", "Super Yoddha"]
    for n in names:
        story = await db.premium_stories.find_one({"story_name_en": {"$regex": n, "$options": "i"}})
        if story:
            print(f"=== {n} ===")
            print("  _id:", story.get("_id"))
            print("  poster_url:", story.get("poster_url"))
            print("  image:", story.get("image"))
            print("  cover:", story.get("cover"))
            print("  banner_url:", story.get("banner_url"))
            print("  enable_parts:", story.get("enable_parts"))
            print("  parts count:", len(story.get("parts", [])))
            print("  parts:", story.get("parts"))
            print()

asyncio.run(check())
