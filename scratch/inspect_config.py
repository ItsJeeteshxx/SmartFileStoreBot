import asyncio
import os
from decouple import config
import motor.motor_asyncio

async def main():
    mongo_uri = config("DATABASE", default="") or config("MONGO_URI", default="")
    client = motor.motor_asyncio.AsyncIOMotorClient(mongo_uri)
    db = client.arya
    doc = await db.mini_app_config.find_one({"_key": "feature_toggles"})
    print("DB feature_toggles:", doc)

if __name__ == "__main__":
    asyncio.run(main())
