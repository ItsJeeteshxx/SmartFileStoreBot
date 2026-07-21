#!/usr/bin/env python3
"""
fix_duplicate_orders.py
─────────────────────────────────────────────────────────────────
Finds and removes DUPLICATE orders from the `orders` collection.

A "duplicate" is defined as:
    Same user_id + same story_id + status in (paid, approved, completed, delivered)

For each duplicate group, we KEEP the OLDEST order (first created) and
DELETE all newer duplicates.

Run this ONCE on the VPS inside the TryAryaBot folder:
    python3 fix_duplicate_orders.py

It will print a detailed report of what it finds and deletes.
"""

import asyncio
import os
import sys
from datetime import datetime, timezone

# ── Load environment ─────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

MONGO_URI = os.environ.get("MONGO_URI") or os.environ.get("DATABASE_URL") or os.environ.get("MONGODB_URI")
if not MONGO_URI:
    # Try reading from config
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from AryaPremium.config import Config
        MONGO_URI = getattr(Config, "MONGO_URI", None) or getattr(Config, "DATABASE_URL", None)
    except Exception as e:
        print(f"[WARN] Could not load Config: {e}")

if not MONGO_URI:
    print("[ERROR] Could not find MONGO_URI in environment or Config.")
    print("Please set the MONGO_URI environment variable and re-run.")
    sys.exit(1)

DB_NAME = os.environ.get("DB_NAME", "arya_premium")


async def main():
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
    except ImportError:
        print("[ERROR] motor is not installed. Run: pip install motor")
        sys.exit(1)

    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]

    print(f"[INFO] Connected to MongoDB. Database: {DB_NAME}")
    print("[INFO] Scanning for duplicate orders (same user_id + story_id, status=paid/approved)...")
    print()

    PAID_STATUSES = ["paid", "approved", "completed", "delivered"]
    total_duplicates_deleted = 0
    total_groups_found = 0

    # Aggregate: group by user_id + story_id (for orders with paid-like status)
    # We look at story_ids as an array — each element counts separately
    pipeline = [
        {"$match": {"status": {"$in": PAID_STATUSES}}},
        {"$unwind": "$story_ids"},
        {"$group": {
            "_id": {"user_id": "$user_id", "story_id": "$story_ids"},
            "order_ids": {"$push": "$_id"},
            "order_refs": {"$push": {"_id": "$_id", "order_id": "$order_id", "created_at": "$created_at", "source": "$source"}},
            "count": {"$sum": 1}
        }},
        {"$match": {"count": {"$gt": 1}}},
        {"$sort": {"count": -1}}
    ]

    duplicate_groups = []
    async for doc in db.orders.aggregate(pipeline):
        duplicate_groups.append(doc)

    if not duplicate_groups:
        print("[OK] No duplicate orders found! Database is clean.")
        client.close()
        return

    total_groups_found = len(duplicate_groups)
    print(f"[FOUND] {total_groups_found} user+story combinations with duplicate paid orders:\n")

    ids_to_delete = []
    for group in duplicate_groups:
        user_id = group["_id"]["user_id"]
        story_id = group["_id"]["story_id"]
        refs = group["order_refs"]
        count = group["count"]

        # Sort by created_at ascending (oldest first) — keep the first, delete the rest
        refs_sorted = sorted(
            refs,
            key=lambda r: r.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)
        )

        keep = refs_sorted[0]
        to_delete = refs_sorted[1:]

        print(f"  User {user_id} | Story {story_id} | {count} orders found:")
        print(f"    ✓ KEEP  → order_id={keep.get('order_id')} created={keep.get('created_at')} source={keep.get('source')}")
        for d in to_delete:
            print(f"    ✗ DELETE→ order_id={d.get('order_id')} created={d.get('created_at')} source={d.get('source')}")
            ids_to_delete.append(d["_id"])
        print()

    if not ids_to_delete:
        print("[OK] Nothing to delete.")
        client.close()
        return

    print(f"[SUMMARY] Will delete {len(ids_to_delete)} duplicate order(s) from {total_groups_found} group(s).")
    confirm = input("Type 'yes' to proceed with deletion, anything else to abort: ").strip().lower()
    if confirm != "yes":
        print("[ABORTED] No changes made.")
        client.close()
        return

    from bson import ObjectId
    result = await db.orders.delete_many({"_id": {"$in": ids_to_delete}})
    total_duplicates_deleted = result.deleted_count

    print(f"\n[DONE] Deleted {total_duplicates_deleted} duplicate order(s).")
    print("[INFO] The database now has at most ONE paid order per user+story combination.")
    print("[INFO] User access (users.purchases, premium_purchases, purchases) was NOT changed — users still have access.")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
