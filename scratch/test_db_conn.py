import os
from motor.motor_asyncio import AsyncIOMotorClient
import asyncio

async def test():
    # Read MONGO_URI from .env
    mongo_uri = "mongodb+srv://AryabyDeepti:GQOkCie8TUQ6cgcW@cluster0.d1gjtf6.mongodb.net/?appName=Cluster0"
    print(f"Connecting to {mongo_uri}...")
    try:
        client = AsyncIOMotorClient(mongo_uri)
        db = client["arya"]
        # Trigger connection by list_collection_names
        cols = await db.list_collection_names()
        print("Success! Collections:", cols)
    except Exception as e:
        print("Failed to connect:", e)

asyncio.run(test())
