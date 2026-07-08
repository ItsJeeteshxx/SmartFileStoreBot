import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import db

async def main():
    print("Inspecting share_links collection...")
    count = await db.share_links.count_documents({})
    print(f"Total links: {count}")
    
    async for doc in db.share_links.find().sort("_id", -1).limit(5):
        print(doc)
        print("-" * 50)

asyncio.run(main())
