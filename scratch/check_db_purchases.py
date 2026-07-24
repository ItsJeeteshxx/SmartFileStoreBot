import asyncio
import os
from pymongo import MongoClient

def main():
    uri = "mongodb+srv://AryabyDeepti:GQOkCie8TUQ6cgcW@cluster0.d1gjtf6.mongodb.net/?appName=Cluster0"
    client = MongoClient(uri)
    db = client["arya"]
    
    print("Databases list:", client.list_database_names())
    print("Collections list in 'arya':", db.list_collection_names())
    
    # Let's count stories, users, purchases, and orders
    stories_count = db.premium_stories.count_documents({})
    users_count = db.users.count_documents({})
    purchases_count = db.premium_purchases.count_documents({})
    orders_count = db.orders.count_documents({})
    
    print(f"Stories: {stories_count}")
    print(f"Users: {users_count}")
    print(f"Premium Purchases: {purchases_count}")
    print(f"Orders: {orders_count}")
    
    # Print a sample user
    sample_user = db.users.find_one({"purchases": {"$exists": True, "$ne": []}})
    if sample_user:
        print("Sample user with purchases:")
        print(f"  ID: {sample_user.get('id')}")
        print(f"  Username: {sample_user.get('username')}")
        print(f"  Purchases (story IDs): {sample_user.get('purchases')}")
    else:
        print("No user found with purchases in array.")
        
    # Print a sample purchase record
    sample_purchase = db.premium_purchases.find_one({})
    if sample_purchase:
        print("Sample premium purchase doc:")
        print(sample_purchase)
    else:
        print("No premium purchases doc found.")

    # Print a sample order record
    sample_order = db.orders.find_one({"status": "paid"})
    if sample_order:
        print("Sample paid order:")
        print(f"  Order ID: {sample_order.get('order_id')}")
        print(f"  User ID: {sample_order.get('user_id')}")
        print(f"  Story IDs: {sample_order.get('story_ids')}")
        print(f"  Status: {sample_order.get('status')}")
    else:
        print("No paid order found.")

if __name__ == "__main__":
    main()
