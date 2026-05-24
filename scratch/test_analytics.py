import asyncio
import os
import sys

# Add parent dir to path so we can import modules
sys.path.append(os.getcwd())

async def main():
    print("Loading config...")
    # Import the root config first to avoid module collision
    import config
    print(f"DATABASE_URI in root config: {hasattr(config.Config, 'DATABASE_URI')}")
    
    print("Initializing Database...")
    from database import Database, db
    
    print("Testing MongoDB Connection...")
    try:
        ver = await db.db.command("serverStatus")
        print(f"Connected. MongoDB Version: {ver.get('version')}")
    except Exception as e:
        print(f"MongoDB connection failed: {e}")
        return

    print("Importing analytics functions...")
    from arya_enterprise_analytics import build_enterprise_dashboard, filters_from_query
    
    flt = filters_from_query(
        days=30,
        query=None,
        telegram_only=False,
        premium_only=False,
        new_users=False,
        returning_users=False,
    )
    
    print("Building enterprise dashboard...")
    try:
        result = await build_enterprise_dashboard(db, flt)
        print("Success! Keys in dashboard:")
        print(list(result.keys()))
    except Exception as e:
        print("FAILED with exception:")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
