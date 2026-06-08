import asyncio
from database import db

async def main():
    print("Connecting to DB...")
    cursor = db.share_links.find().sort("created_at", -1).limit(10)
    links = [doc async for doc in cursor]
    print(f"Found {len(links)} recent share links.")
    for l in links:
        mids = l.get('message_ids', [])
        print(f"UUID: {l['_id']} | Mids Count: {len(mids)} | Mids: {mids[:5]}...")

if __name__ == "__main__":
    asyncio.run(main())
