import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import db

async def main():
    print("Finding recent share links...")
    async for link in db.share_links.find().sort("_id", -1).limit(5):
        print(f"UUID: {link.get('_id')}")
        print(f"Source Chat: {link.get('source_chat')}")
        print(f"Message IDs: {link.get('message_ids')}")
        print("-" * 30)

asyncio.run(main())
