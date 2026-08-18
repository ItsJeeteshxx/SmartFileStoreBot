import asyncio, sys, os
from bson.objectid import ObjectId

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
os.chdir(BASE)

from config import Config
from motor.motor_asyncio import AsyncIOMotorClient
from mini_app_api import _format_story

mongo_uri = Config.MONGO_URI or Config.DATABASE_URI or getattr(Config, "DATABASE", "")
print("Using Mongo URI:", mongo_uri[:25] + "..." if len(mongo_uri) > 25 else mongo_uri)
print("Using Database Name:", Config.DATABASE_NAME)

client = AsyncIOMotorClient(mongo_uri)
db = client[Config.DATABASE_NAME]

async def main():
    print("=" * 60)
    print("ALL STORIES WITH PARTS ENABLED OR PARTS LIST IN MONGO:")
    print("=" * 60)
    cursor = db.premium_stories.find({
        "$or": [
            {"enable_parts": {"$in": [True, "true", "True", "1", 1]}},
            {"parts": {"$exists": True, "$ne": []}},
            {"story_parts": {"$exists": True, "$ne": []}},
            {"episode_parts": {"$exists": True, "$ne": []}}
        ]
    })
    count = 0
    async for s in cursor:
        count += 1
        print(f"\n--- STORY #{count} ---")
        print("  _id:", s.get("_id"))
        print("  story_name_en:", s.get("story_name_en"))
        print("  story_name_hi:", s.get("story_name_hi"))
        print("  raw enable_parts:", s.get("enable_parts"))
        print("  raw parts count:", len(s.get("parts") or s.get("story_parts") or s.get("episode_parts") or []))
        print("  raw parts:", s.get("parts") or s.get("story_parts") or s.get("episode_parts"))
        
        fmt = _format_story(s)
        print("  -> FORMATTED enable_parts:", fmt.get("enable_parts") if fmt else "SKIPPED/NULL")
        print("  -> FORMATTED parts count:", len(fmt.get("parts", [])) if fmt else 0)
        print("  -> FORMATTED parts:", fmt.get("parts") if fmt else None)

    if count == 0:
        print("\n⚠️ NO STORIES FOUND WITH enable_parts OR parts IN MONGODB!")
        print("Let's check 'Super Yoddha' specifically:")
        sy = await db.premium_stories.find_one({"story_name_en": {"$regex": "Super Yoddha", "$options": "i"}})
        if sy:
            print("  Super Yoddha doc:", sy)
        else:
            print("  Super Yoddha not found in premium_stories collection.")

    # Also check the specific ID from user log 69f77ad44f650e13783ff9e6
    print("\n" + "=" * 60)
    print("CHECKING STORY ID FROM USER LOG (69f77ad44f650e13783ff9e6):")
    print("=" * 60)
    try:
        doc = await db.premium_stories.find_one({"_id": ObjectId("69f77ad44f650e13783ff9e6")})
        if doc:
            print("  _id:", doc.get("_id"))
            print("  story_name_en:", doc.get("story_name_en"))
            print("  enable_parts:", doc.get("enable_parts"))
            print("  parts:", doc.get("parts"))
            fmt = _format_story(doc)
            print("  -> FORMATTED enable_parts:", fmt.get("enable_parts") if fmt else "NULL")
        else:
            print("  Story 69f77ad44f650e13783ff9e6 not found.")
    except Exception as e:
        print("  Error looking up 69f77ad44f650e13783ff9e6:", e)

    # Check user 1071421266 purchases
    print("\n" + "=" * 60)
    print("CHECKING USER 1071421266 PURCHASES IN DB:")
    print("=" * 60)
    u = await db.users.find_one({"$or": [{"id": 1071421266}, {"telegram_id": 1071421266}, {"id": "1071421266"}]})
    if u:
        purchased_ids = [str(x) for x in (u.get("purchases") or [])]
        print("  User found:", u.get("name") or u.get("username") or u.get("id"))
        print("  Purchased story IDs count:", len(purchased_ids))
        print("  Is Super Yoddha (69f77ad44f650e13783ff9e6) purchased by user?:", "69f77ad44f650e13783ff9e6" in purchased_ids)
    else:
        print("  User 1071421266 not found in users collection.")

    print("\n" + "=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
