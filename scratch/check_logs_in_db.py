import asyncio
import sys

# Add root folder to sys.path
sys.path.insert(0, r"c:\Users\User\Downloads\AryaBotNew\TryAryaBot")

async def check_db_logs():
    from database import db
    doc = await db.stats.find_one({'_id': 'logs_config'})
    print("=== DATABASE LOGS CONFIG ===")
    print("Document:", doc)
    
if __name__ == "__main__":
    asyncio.run(check_db_logs())
