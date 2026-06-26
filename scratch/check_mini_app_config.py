import asyncio
from motor.motor_asyncio import AsyncIOMotorClient

async def main():
    mongo_uri = "mongodb+srv://AryabyDeepti:GQOkCie8TUQ6cgcW@cluster0.d1gjtf6.mongodb.net/?appName=Cluster0"
    client = AsyncIOMotorClient(mongo_uri)
    
    # Check databases
    db_names = await client.list_database_names()
    print("Databases:", db_names)
    
    # premium database
    db = client["forward-bot"]
    
    print("\n--- Collections in forward-bot ---")
    cols = await db.list_collection_names()
    print(cols)
    
    print("\n--- premium_bots ---")
    async for bot in db.premium_bots.find({}):
        print(bot)
        
    print("\n--- mini_app_config ---")
    async for cfg in db.mini_app_config.find({}):
        print(cfg)
        
    # Check "arya" database just in case
    if "arya" in db_names:
        db_arya = client["arya"]
        print("\n--- Collections in arya ---")
        cols_arya = await db_arya.list_collection_names()
        print(cols_arya)
        
        print("\n--- premium_bots in arya ---")
        async for bot in db_arya.premium_bots.find({}):
            print(bot)

if __name__ == "__main__":
    asyncio.run(main())
