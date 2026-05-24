import asyncio
from config import Config
from database import db

async def main():
    print("Connecting to DB...")
    # List first 10 stories in premium_stories
    cursor = db.db.premium_stories.find({})
    stories = await cursor.to_list(length=10)
    print(f"Found {len(stories)} stories in database:")
    for s in stories:
        print("Story ID:", s.get("_id"))
        print("  Name:", s.get("story_name_en"))
        print("  Demo fields:", {k: v for k, v in s.items() if "demo" in k.lower()})
        print("  Other fields:", list(s.keys()))
        print("---")

if __name__ == "__main__":
    asyncio.run(main())
