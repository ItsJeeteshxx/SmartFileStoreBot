from motor.motor_asyncio import AsyncIOMotorClient
import asyncio

async def fix_languages():
    print("Connecting to DB...")
    client = AsyncIOMotorClient("mongodb://localhost:27017")
    db = client["AryaPremium"]
    
    # Update stories
    result = await db.premium_stories.update_many(
        {"language": {"$exists": False}},
        {"$set": {"language": "Hindi"}}
    )
    print(f"Updated {result.modified_count} stories without a language field.")
    
    result2 = await db.premium_stories.update_many(
        {"language": None},
        {"$set": {"language": "Hindi"}}
    )
    print(f"Updated {result2.modified_count} stories with language=None.")

    client.close()

if __name__ == "__main__":
    asyncio.run(fix_languages())
