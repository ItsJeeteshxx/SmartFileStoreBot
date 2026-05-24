import sys
import os
sys.path.append(os.getcwd())

import asyncio
from dotenv import load_dotenv

async def main():
    # Force load .env override
    load_dotenv(override=True)
    
    db_name = os.environ.get("DATABASE_NAME", "arya")
    uri = os.environ.get("DATABASE", "mongodb+srv://AryabyDeepti:GQOkCie8TUQ6cgcW@cluster0.d1gjtf6.mongodb.net/?appName=Cluster0")
    
    print("Database URI:", uri)
    print("Database Name:", db_name)
    
    import motor.motor_asyncio
    client = motor.motor_asyncio.AsyncIOMotorClient(uri)
    db = client[db_name]
    
    print("Collections:")
    for col_name in sorted(await db.list_collection_names()):
        count = await db[col_name].count_documents({})
        print(f" - {col_name}: {count} documents")
        
if __name__ == "__main__":
    asyncio.run(main())
