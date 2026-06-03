import asyncio
import os
import sys

sys.path.append("c:\\Users\\User\\Downloads\\AryaBotNew\\TryAryaBot\\AryaPremium")
from database import db

async def main():
    await db.connect()
    res = await db.users.update_many({}, {"$set": {"subscribed": False}})
    print(f"Modified {res.modified_count} users. Set subscribed=False for everyone.")

if __name__ == "__main__":
    asyncio.run(main())
