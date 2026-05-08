import asyncio
import sys
sys.path.append('.')
from dotenv import load_dotenv
load_dotenv()

from database import db

async def fix_languages():
    print("Connecting to DB...")
    await db.connect()
    
    # Update stories
    result = await db.db.premium_stories.update_many(
        {"language": {"$exists": False}},
        {"$set": {"language": "Hindi"}}
    )
    print(f"Updated {result.modified_count} stories without a language field.")
    
    result2 = await db.db.premium_stories.update_many(
        {"language": None},
        {"$set": {"language": "Hindi"}}
    )
    print(f"Updated {result2.modified_count} stories with language=None.")

if __name__ == "__main__":
    asyncio.run(fix_languages())
