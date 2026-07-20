import asyncio
import os
import sys

# Add path to load database and config modules
sys.path.append(os.getcwd())
sys.path.append(os.path.join(os.getcwd(), "AryaPremium"))

from database import db

async def main():
    cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    gmail_user = cfg.get("gmail_user", "")
    gmail_password = cfg.get("gmail_app_password", "")
    
    print("--- Gmail Credentials from DB ---")
    print(f"gmail_user: {repr(gmail_user)}")
    print(f"gmail_password: {repr(gmail_password)}")
    
    # Check if they have \xa0 or other non-ascii chars
    for idx, c in enumerate(gmail_user):
        if ord(c) > 127:
            print(f"Non-ascii char in gmail_user: {repr(c)} at position {idx} (ord={ord(c)})")
            
    for idx, c in enumerate(gmail_password):
        if ord(c) > 127:
            print(f"Non-ascii char in gmail_password: {repr(c)} at position {idx} (ord={ord(c)})")
            
    # Check environment variables
    env_user = os.environ.get("GMAIL_USER", "")
    env_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
    print("--- Environment Variables ---")
    print(f"GMAIL_USER: {repr(env_user)}")
    print(f"GMAIL_APP_PASSWORD: {repr(env_pass)}")
    
    for idx, c in enumerate(env_user):
        if ord(c) > 127:
            print(f"Non-ascii char in GMAIL_USER: {repr(c)} at position {idx} (ord={ord(c)})")
            
    for idx, c in enumerate(env_pass):
        if ord(c) > 127:
            print(f"Non-ascii char in GMAIL_APP_PASSWORD: {repr(c)} at position {idx} (ord={ord(c)})")

asyncio.run(main())
