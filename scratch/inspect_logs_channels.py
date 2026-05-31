import asyncio
import sys
import os

sys.path.insert(0, r"c:\Users\User\Downloads\AryaBotNew\TryAryaBot")

# Parse .env
env_vars = {}
try:
    with open(r"c:\Users\User\Downloads\AryaBotNew\TryAryaBot\.env", "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env_vars[k.strip()] = v.strip().strip("'").strip('"')
except Exception as e:
    print(f"Could not load .env: {e}")

os.environ["DATABASE"] = env_vars.get("DATABASE", "")
os.environ["DATABASE_URI"] = env_vars.get("DATABASE", "")

async def main():
    from database import db
    print("Checking configured log channels in MongoDB...")
    try:
        cfg = await db.get_logs_config()
        print("\n--- Logs Configuration from Database ---")
        for k, v in cfg.items():
            print(f"  {k}: {v}")
    except Exception as e:
        print(f"Error fetching logs config: {e}")

    # Check env variables as well
    print("\n--- Environment Variables in .env ---")
    for key in ["PREMIUM_BAN_LOGS_CHANNEL", "ARYA_LOGS_CHANNEL", "DELIVERY_LOGS_CHANNEL", "BOT_OWNER_ID", "OWNER_IDS"]:
        print(f"  {key}: {env_vars.get(key, 'Not defined')}")

if __name__ == "__main__":
    asyncio.run(main())
