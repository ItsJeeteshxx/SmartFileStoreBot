import asyncio
from config import Config
from database import db

async def check():
    await db.connect()
    
    story_id = "69e5a6f7af3d4ca94a26b3d8"
    from bson.objectid import ObjectId
    try:
        o_id = ObjectId(story_id)
        story = await db.db.premium_stories.find_one({"_id": o_id})
        print(f"By ObjectId: {story is not None}")
    except Exception as e:
        print(f"ObjectId error: {e}")
        
    story_str = await db.db.premium_stories.find_one({"_id": story_id})
    print(f"By string _id: {story_str is not None}")
    
    story_id_field = await db.db.premium_stories.find_one({"story_id": story_id})
    print(f"By story_id field: {story_id_field is not None}")
    
    # Try just finding ANY story
    any_story = await db.db.premium_stories.find_one({})
    if any_story:
        print(f"Sample story ID: {any_story.get('_id')} (type: {type(any_story.get('_id'))})")
    else:
        print("No stories in premium_stories at all!")

if __name__ == "__main__":
    asyncio.run(check())
