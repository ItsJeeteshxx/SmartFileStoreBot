import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import db

async def main():
    uids = [1011040900, 7149369044, 5123283499, 1071421266]
    print("Checking ban/strike status of users...")
    
    for uid in uids:
        print(f"\nUser: {uid}")
        ban_status = await db.get_ban_status(uid)
        print(f"  Ban Status: {ban_status}")
        
        strike_rec = await db.db.anti_abuse_strikes.find_one({'_id': uid})
        print(f"  Anti-abuse Strikes in DB: {strike_rec}")
        
        user_doc = await db.col.find_one({'id': uid})
        if user_doc:
            print(f"  User Doc keys: {list(user_doc.keys())}")
            if 'ban_status' in user_doc:
                print(f"    ban_status: {user_doc['ban_status']}")
        else:
            print("  No user doc found in users collection.")

asyncio.run(main())
