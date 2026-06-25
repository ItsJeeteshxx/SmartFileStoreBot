import asyncio
import os
import sys
from motor.motor_asyncio import AsyncIOMotorClient

# Add local path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from AryaPremium.config import Config as PremConfig

async def main():
    print("=== Config Values ===")
    print("Config.DATABASE_URI:", Config.DATABASE_URI)
    print("Config.DATABASE_NAME:", Config.DATABASE_NAME)
    print("PremConfig.DATABASE_NAME:", PremConfig.DATABASE_NAME)
    
    # Connect using Config.DATABASE_URI
    if not Config.DATABASE_URI:
        print("No DATABASE_URI found!")
        return
        
    client = AsyncIOMotorClient(Config.DATABASE_URI, serverSelectionTimeoutMS=5000)
    
    # Check default DB name from Config
    db_name = Config.DATABASE_NAME or "arya"
    db = client[db_name]
    print(f"\nAccessing database: {db_name}")
    
    try:
        cfg = await db.mini_app_config.find_one({"_key": "feature_toggles"})
        if cfg:
            print("Found feature_toggles document keys:")
            for k, v in cfg.items():
                if k == "gmail_app_password":
                    print(f"  {k}: [length {len(v)}]" if v else f"  {k}: [empty]")
                else:
                    print(f"  {k}: {v}")
        else:
            print("No feature_toggles document found in this database.")
    except Exception as e:
        print("Error:", e)
        
    # Also check PremConfig database name
    db_name_prem = PremConfig.DATABASE_NAME or "forward-bot"
    if db_name_prem != db_name:
        db_prem = client[db_name_prem]
        print(f"\nAccessing PremConfig database: {db_name_prem}")
        try:
            cfg_prem = await db_prem.mini_app_config.find_one({"_key": "feature_toggles"})
            if cfg_prem:
                print("Found feature_toggles document keys:")
                for k, v in cfg_prem.items():
                    if k == "gmail_app_password":
                        print(f"  {k}: [length {len(v)}]" if v else f"  {k}: [empty]")
                    else:
                        print(f"  {k}: {v}")
            else:
                print("No feature_toggles document found in this database.")
        except Exception as e:
            print("Error:", e)

if __name__ == "__main__":
    asyncio.run(main())
