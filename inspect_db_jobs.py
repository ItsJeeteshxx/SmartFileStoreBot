import asyncio
from config import Config
from database import db

async def main():
    print("Connecting to DB...")
    # List all documents in the multijobs collection
    cursor = db.db.multijobs.find({})
    jobs = await cursor.to_list(length=100)
    print(f"Found {len(jobs)} jobs in multijobs collection:")
    for j in jobs:
        print("--- Job ID:", j.get("job_id"))
        print("  Name:", j.get("name"))
        print("  User ID:", j.get("user_id"))
        print("  From Chat:", j.get("from_chat"), f"({j.get('from_title')})")
        print("  To Chat:", j.get("to_chat"), f"({j.get('to_title')})")
        print("  Start ID:", j.get("start_id"))
        print("  End ID:", j.get("end_id"))
        print("  Current ID:", j.get("current_id"))
        print("  Status:", j.get("status"))
        print("  Error:", j.get("error"))
        print("  Forwarded:", j.get("forwarded"))

if __name__ == "__main__":
    asyncio.run(main())
