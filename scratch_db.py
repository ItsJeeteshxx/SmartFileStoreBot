import asyncio
from database import db

async def main():
    jobs = await db.db["multijobs"].find({}).to_list(100)
    for j in jobs:
        print(f"Job ID: {j.get('job_id') or j.get('_id')}")
        print(f"  User ID: {j.get('user_id')}")
        print(f"  Status: {j.get('status')}")
        print(f"  From Chat: {j.get('from_chat')}")
        print(f"  To Chat: {j.get('to_chat')}")
        print(f"  Start ID: {j.get('start_id')}")
        print(f"  End ID: {j.get('end_id')}")
        print(f"  Current ID: {j.get('current_id')}")
        print(f"  Smart Order: {j.get('smart_order')}")
        print("-" * 40)

if __name__ == "__main__":
    asyncio.run(main())
