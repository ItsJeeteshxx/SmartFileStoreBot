import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
import sys

# Import config to get the correct database URL
from config import Config

async def fetch_logs():
    mongo_uri = getattr(Config, "MONGO_URI", None) or "mongodb://localhost:27017"
    print(f"Connecting to MongoDB at: {mongo_uri}")
    client = AsyncIOMotorClient(mongo_uri)
    db = client.pocket_arya_store
    
    print("--- AUTOMATIC BANS (AUTO-BANNED/FLAGGED USERS) ---")
    flagged = await db.premium_bans.find({
        "$or": [
            {"status": "flagged"},
            {"status": "banned", "reason": {"$regex": "^Auto-ban"}}
        ]
    }).to_list(length=100)
    if not flagged:
        print("No auto-banned users found.")
    for u in flagged:
        print(f"ID: {u.get('_id')} | Name: {u.get('name')} | Reason: {u.get('reason')} | IPs: {u.get('ips')}")
        
    print("\n--- RECENT BAN ACTIVITY LOGS ---")
    activity = await db.premium_ban_activity.find().sort("timestamp", -1).limit(20).to_list(length=20)
    if not activity:
        print("No recent ban activity.")
    for a in activity:
        print(f"Time: {a.get('timestamp')} | Action: {a.get('action')} | Reason: {a.get('reason')}")

    # Remove the auto-flags/auto-bans to unban the user's alt accounts!
    print("\n--- CLEARING AUTO-BANS ---")
    result = await db.premium_bans.delete_many({
        "$or": [
            {"status": "flagged"},
            {"status": "banned", "reason": {"$regex": "^Auto-ban"}}
        ]
    })
    print(f"Cleared {result.deleted_count} auto-banned/flagged users from the database.")
    
    await client.close()

if __name__ == "__main__":
    asyncio.run(fetch_logs())
