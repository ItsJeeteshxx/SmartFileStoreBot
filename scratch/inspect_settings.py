import asyncio
import os
from motor.motor_asyncio import AsyncIOMotorClient

async def main():
    uri = "mongodb+srv://AryabyDeepti:GQOkCie8TUQ6cgcW@cluster0.d1gjtf6.mongodb.net/?appName=Cluster0"
    client = AsyncIOMotorClient(
        uri,
        tls=True,
        tlsAllowInvalidCertificates=True
    )
    
    # Try both 'arya' and 'forward-bot' databases
    for db_name in ["arya", "forward-bot"]:
        db = client[db_name]
        print(f"\n--- Database: {db_name} ---")
        try:
            cfg = await db.mini_app_config.find_one({"_key": "feature_toggles"})
            if cfg:
                print("Found feature_toggles config:")
                for k, v in cfg.items():
                    if "password" in k or "key" in k or "secret" in k:
                        print(f"  {k}: [MASKED: length {len(str(v))}]")
                    else:
                        print(f"  {k}: {v}")
            else:
                print("No feature_toggles config document found in mini_app_config collection.")
        except Exception as e:
            print(f"Error querying {db_name}: {e}")

if __name__ == "__main__":
    asyncio.run(main())
