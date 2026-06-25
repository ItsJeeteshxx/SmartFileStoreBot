import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
import sys

async def main():
    uri = "mongodb+srv://AryabyDeepti:GQOkCie8TUQ6cgcW@cluster0.d1gjtf6.mongodb.net/?appName=Cluster0"
    print("Connecting to MongoDB...")
    client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5000)
    db = client["forward-bot"] # The default DB name is "forward-bot", let's check both
    
    for dbname in ["forward-bot", "arya", "TryAryaForwardBot"]:
        db = client[dbname]
        print(f"\n--- Checking DB: {dbname} ---")
        try:
            cfg = await db.mini_app_config.find_one({"_key": "feature_toggles"})
            if cfg:
                print(f"Found feature_toggles in {dbname}:")
                # print without app password for safety, or print parts of it
                safe_cfg = {k: (v[:4] + "***" if k == "gmail_app_password" and isinstance(v, str) else v) for k, v in cfg.items()}
                print(safe_cfg)
            else:
                print(f"No feature_toggles document in {dbname}.")
        except Exception as e:
            print(f"Error querying {dbname}: {e}")

if __name__ == "__main__":
    asyncio.run(main())
