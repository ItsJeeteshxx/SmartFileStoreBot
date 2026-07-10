import asyncio
import os
import sys
from motor.motor_asyncio import AsyncIOMotorClient

# Add parent path to import config if needed
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from AryaPremium.config import Config

async def main():
    mongo_uri = Config.MONGO_URI
    db_name = "arya"
    print(f"Connecting to MongoDB: {mongo_uri} / DB: {db_name}")
    
    client = AsyncIOMotorClient(mongo_uri)
    db = client[db_name]
    
    collections = await db.list_collection_names()
    print("Collections in DB:")
    for col_name in collections:
        count = await db[col_name].count_documents({})
        print(f" - {col_name}: {count} documents")
        
    # Check details of orders and checkouts
    if "orders" in collections:
        statuses = await db.orders.aggregate([
            {"$group": {"_id": "$status", "count": {"$sum": 1}}}
        ]).to_list(length=100)
        print("\nOrders Statuses:")
        for s in statuses:
            print(f" - {s['_id']}: {s['count']}")
            
    if "premium_checkout" in collections:
        statuses = await db.premium_checkout.aggregate([
            {"$group": {"_id": "$status", "count": {"$sum": 1}}}
        ]).to_list(length=100)
        print("\nBot Checkouts Statuses:")
        for s in statuses:
            print(f" - {s['_id']}: {s['count']}")

if __name__ == "__main__":
    asyncio.run(main())
