import asyncio
import motor.motor_asyncio
import sys

async def main():
    client = motor.motor_asyncio.AsyncIOMotorClient('mongodb+srv://AryabyDeepti:GQOkCie8TUQ6cgcW@cluster0.d1gjtf6.mongodb.net/?appName=Cluster0')
    cfg = await client.AryaPremium.mini_app_config.find_one({"_key": "feature_toggles"})
    print("FEATURE TOGGLES PROMOS:", cfg.get("promo_codes") if cfg else None)
    
    promos = await client.AryaPremium.premium_promo_codes.find().to_list(100)
    print("PREMIUM PROMO CODES:", promos)
    sys.exit(0)

if __name__ == '__main__':
    asyncio.run(main())
