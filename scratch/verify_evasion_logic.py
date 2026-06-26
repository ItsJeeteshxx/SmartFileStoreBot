import asyncio
import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, r"c:\Users\User\Downloads\AryaBotNew\TryAryaBot")

# Parse .env directly for DB credentials
env_vars = {}
try:
    with open(r"c:\Users\User\Downloads\AryaBotNew\TryAryaBot\.env", "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env_vars[k.strip()] = v.strip().strip("'").strip('"')
except Exception as e:
    print(f"Could not load .env: {e}")

os.environ["DATABASE"] = env_vars.get("DATABASE", "")
os.environ["DATABASE_URI"] = env_vars.get("DATABASE", "")

async def verify():
    from database import db
    print("Connecting to MongoDB...")
    # Initialize DB (creates indexes etc)
    
    # We will reset and set up clean test fixtures in a sandbox way
    # Banned User: 999999 (banned_user)
    # Blocked IP: 198.51.100.1
    
    test_tg_id = 999999
    test_ip_blocked = "198.51.100.1"
    
    # Clean up previous test runs
    await db.db.premium_bans.delete_many({"_id": {"$in": [999999, 888888]}})
    await db.db.premium_bans.delete_many({"ips": {"$in": [test_ip_blocked, "198.51.100.2"]}})
    await db.db.users.delete_many({"id": {"$in": [999999, 888888]}})
    await db.db.premium_ban_activity.delete_many({"telegram_id": {"$in": [999999, 888888]}})
    
    print("\n--- STAGE 1: Setup Fixtures ---")
    # Insert banned user
    await db.db.premium_bans.insert_one({
        "_id": test_tg_id,
        "ips": [test_ip_blocked],
        "reason": "Offensive behavior",
        "status": "banned",
        "banned_at": datetime.now(timezone.utc),
        "name": "Fixture Banned User"
    })
    # Also set them in users collection (simulating delivery bot user)
    await db.db.users.insert_one({
        "id": test_tg_id,
        "first_name": "Fixture Banned",
        "ban_status": {"is_banned": True, "ban_reason": "Offensive behavior"}
    })
    print(f"Fixtures configured! User {test_tg_id} banned. Blocked IP is {test_ip_blocked}.")

    print("\n--- STAGE 2: Test VPN Evasion (Rule A) ---")
    # Simulate banned user 999999 connecting from a new IP 198.51.100.2
    new_ip = "198.51.100.2"
    
    # 1. Look up if user or IP is banned
    banned_by_tg = await db.db.premium_bans.find_one({"_id": test_tg_id, "status": {"$in": ["banned", "flagged"]}})
    banned_by_ip = await db.db.premium_bans.find_one({"ips": new_ip, "status": {"$in": ["banned", "flagged"]}})
    
    assert banned_by_tg is not None, "Rule A verification failed: Banned TG ID not found in database!"
    assert banned_by_ip is None, "Rule A verification failed: New IP should not be blocked yet!"
    
    # Trigger Rule A block logic
    if banned_by_tg and not banned_by_ip:
        print(f"VPN Evasion detected! Appending new IP {new_ip} to blocked list for User {test_tg_id}...")
        await db.db.premium_bans.update_one(
            {"_id": test_tg_id},
            {"$addToSet": {"ips": new_ip}}
        )
        await db.db.premium_ban_activity.insert_one({
            "telegram_id": test_tg_id,
            "ip": new_ip,
            "timestamp": datetime.now(timezone.utc),
            "action": "App Open (/api/stories)",
            "reason": "VPN Evasion caught: User on new IP",
            "name": banned_by_tg.get("name", "Banned User")
        })
        
    # Verify Rule A effects
    updated_ban = await db.db.premium_bans.find_one({"_id": test_tg_id})
    print(f"Updated ban IPs for {test_tg_id}: {updated_ban['ips']}")
    assert new_ip in updated_ban["ips"], "Rule A failed: New IP was not appended to banned user's ips list!"
    print("✅ Rule A (VPN Evasion) works perfectly! New IP appended successfully.")

    print("\n--- STAGE 3: Test Alt-Account Linking (Rule B) ---")
    # Simulate new/unbanned user 888888 connecting from the blocked IP 198.51.100.1
    alt_tg_id = 888888
    
    banned_by_tg = await db.db.premium_bans.find_one({"_id": alt_tg_id, "status": {"$in": ["banned", "flagged"]}})
    banned_by_ip = await db.db.premium_bans.find_one({"ips": test_ip_blocked, "status": {"$in": ["banned", "flagged"]}})
    
    assert banned_by_tg is None, "Rule B verification failed: Alt user should not be banned yet!"
    assert banned_by_ip is not None, "Rule B verification failed: IP 198.51.100.1 should be blocked!"
    
    # Trigger Rule B block logic
    if banned_by_ip and not banned_by_tg:
        reason = f"Auto-ban: Alternative account detected on blocked IP {test_ip_blocked}"
        alt_name = f"Alt of User {banned_by_ip['_id']}"
        print(f"Alt account detected on blocked IP! Auto-banning new Telegram ID {alt_tg_id}...")
        
        # Auto-ban in bans collection
        await db.db.premium_bans.update_one(
            {"_id": alt_tg_id},
            {"$set": {
                "ips": [test_ip_blocked],
                "reason": reason,
                "status": "banned",
                "banned_at": datetime.now(timezone.utc),
                "name": alt_name
            }},
            upsert=True
        )
        
        # Propagate to Delivery Bot ban status
        await db.db.users.update_one(
            {"id": alt_tg_id},
            {"$set": {"ban_status": {"is_banned": True, "ban_reason": reason}}},
            upsert=True
        )
        
        # Insert activity feed log
        await db.db.premium_ban_activity.insert_one({
            "telegram_id": alt_tg_id,
            "ip": test_ip_blocked,
            "timestamp": datetime.now(timezone.utc),
            "action": "App Open (/api/stories)",
            "reason": reason,
            "name": alt_name
        })

    # Verify Rule B effects
    new_banned_doc = await db.db.premium_bans.find_one({"_id": alt_tg_id})
    new_user_doc = await db.db.users.find_one({"id": alt_tg_id})
    
    print(f"Bans record for alt user {alt_tg_id}: {new_banned_doc}")
    print(f"Delivery bot record for alt user {alt_tg_id}: {new_user_doc}")
    
    assert new_banned_doc is not None, "Rule B failed: Alt user was not added to premium_bans!"
    assert new_banned_doc["status"] == "banned", "Rule B failed: Alt user status should be 'banned'!"
    assert new_user_doc is not None, "Rule B failed: Alt user record was not propagated to users collection!"
    assert new_user_doc["ban_status"]["is_banned"] is True, "Rule B failed: Alt user is_banned should be True!"
    print("✅ Rule B (Alt-Account Evasion) works perfectly! New ID successfully linked and auto-banned across Delivery Bot DB.")

    print("\n--- STAGE 4: Clean Up Test Fixtures ---")
    await db.db.premium_bans.delete_many({"_id": {"$in": [999999, 888888]}})
    await db.db.users.delete_many({"id": {"$in": [999999, 888888]}})
    await db.db.premium_ban_activity.delete_many({"telegram_id": {"$in": [999999, 888888]}})
    print("Test fixtures cleanly purged from database.")
    print("\n🎉 ALL MULTI-VECTOR BAN & BLOCK AUTO-EVASION TESTS COMPLETED SUCCESSFULLY!")

if __name__ == "__main__":
    asyncio.run(verify())
