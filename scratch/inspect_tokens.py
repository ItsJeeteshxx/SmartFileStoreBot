import asyncio
import os
import sys

# Add AryaPremium to path
_arya_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "AryaPremium"))
if _arya_path not in sys.path:
    sys.path.insert(0, _arya_path)

# Add parent path
_parent_path = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _parent_path not in sys.path:
    sys.path.insert(0, _parent_path)

async def main():
    try:
        from AryaPremium.database import db
        await db.connect()
        print("Connected to premium DB")
    except Exception as e:
        print(f"Failed to connect to premium DB: {e}")
        return

    tokens_to_check = []
    
    # 1. AryaPremium config
    from AryaPremium.config import Config as PremConfig
    print("MGMT_BOT_TOKEN from AryaPremium:", getattr(PremConfig, "MGMT_BOT_TOKEN", None))
    print("BOT_TOKEN from AryaPremium:", getattr(PremConfig, "BOT_TOKEN", None))
    for attr in ("MGMT_BOT_TOKEN", "BOT_TOKEN"):
        tok = getattr(PremConfig, attr, None)
        if tok and isinstance(tok, str) and tok.strip():
            tok = tok.strip()
            if tok not in tokens_to_check:
                tokens_to_check.append(tok)
                
    # 2. Root config
    try:
        from config import Config as RootConfig
        print("BOT_TOKEN from Root Config:", getattr(RootConfig, "BOT_TOKEN", None))
        print("Root Database Name:", getattr(RootConfig, "DATABASE_NAME", None))
        for attr in ("BOT_TOKEN",):
            tok = getattr(RootConfig, attr, None)
            if tok and isinstance(tok, str) and tok.strip():
                tok = tok.strip()
                if tok not in tokens_to_check:
                    tokens_to_check.append(tok)
    except Exception as e:
        print("Failed to load root config:", e)

    # 3. Share bots list from root db
    try:
        from config import Config as RootConfig
        root_db_name = RootConfig.DATABASE_NAME
        if root_db_name:
            root_db = db.client[root_db_name]
            share_bots_doc = await root_db.global_stats.find_one({'_id': 'share_bots_list'})
            if share_bots_doc and 'bots' in share_bots_doc:
                print(f"Found {len(share_bots_doc['bots'])} share bots in root db:")
                for bot in share_bots_doc['bots']:
                    name = bot.get('name')
                    username = bot.get('username')
                    tok = bot.get('token')
                    print(f" - {name} (@{username}): {tok[:15]}...{tok[-5:] if tok else ''}")
                    if tok and isinstance(tok, str) and tok.strip():
                        tok = tok.strip()
                        if tok not in tokens_to_check:
                            tokens_to_check.append(tok)
            else:
                print("No share bots list found in root db")
    except Exception as e:
        print("Failed to fetch share bots list from root db:", e)

    # 4. Premium bots from database
    try:
        bots = await db.db.premium_bots.find().to_list(length=None)
        print(f"Found {len(bots)} premium bots in database:")
        for b in bots:
            username = b.get('username')
            tok = b.get('token')
            print(f" - @{username}: {tok[:15]}...{tok[-5:] if tok else ''}")
            if tok and isinstance(tok, str) and tok.strip():
                tok = tok.strip()
                if tok not in tokens_to_check:
                    tokens_to_check.append(tok)
    except Exception as e:
        print("Failed to fetch premium bots:", e)

    print("\nFinal tokens list to check:")
    for t in tokens_to_check:
        print(f" - {t[:15]}...{t[-5:]}")

if __name__ == "__main__":
    asyncio.run(main())
