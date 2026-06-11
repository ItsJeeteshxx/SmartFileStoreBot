import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
import sys

# Import config from AryaPremium to get the correct database URL
from AryaPremium.config import Config

async def fetch_logs():
    mongo_uri = getattr(Config, "MONGO_URI", None) or "mongodb://localhost:27017"
    print(f"Connecting to MongoDB at: {mongo_uri}")
    client = AsyncIOMotorClient(mongo_uri)
    db = client.pocket_arya_store
    
    print("--- AUTOMATIC BANS (FLAGGED USERS) ---")
    flagged = await db.premium_bans.find({"status": "flagged"}).to_list(length=100)
    if not flagged:
        print("No auto-banned (flagged) users found.")
    for u in flagged:
        print(f"ID: {u.get('_id')} | Name: {u.get('name')} | Reason: {u.get('reason')} | IPs: {u.get('ips')}")
        
    print("\n--- RECENT BAN ACTIVITY LOGS ---")
    activity = await db.premium_ban_activity.find().sort("timestamp", -1).limit(20).to_list(length=20)
    if not activity:
        print("No recent ban activity.")
    for a in activity:
        print(f"Time: {a.get('timestamp')} | Action: {a.get('action')} | Reason: {a.get('reason')}")

    # Remove the auto-flags to unban the user's alt accounts!
    print("\n--- CLEARING AUTO-FLAGS ---")
    result = await db.premium_bans.delete_many({"status": "flagged"})
    print(f"Cleared {result.deleted_count} auto-flagged users from the database.")
    
    await client.close()

if __name__ == "__main__":
    asyncio.run(fetch_logs())
