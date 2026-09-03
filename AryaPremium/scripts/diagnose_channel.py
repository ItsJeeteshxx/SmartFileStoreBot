import os
import sys
import asyncio
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from database import db
from config import Config
from pyrogram import Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


async def diagnose():
    print("=" * 90)
    print("🔍 TARGET CHANNEL DIAGNOSTIC TOOL")
    print("=" * 90)

    # 1. Connect all active bots
    clients: list[Client] = []

    mgmt_bot = Client(
        name="mgmt_bot_diag",
        api_id=Config.API_ID,
        api_hash=Config.API_HASH,
        bot_token=Config.MGMT_BOT_TOKEN,
        in_memory=True
    )
    await mgmt_bot.start()
    clients.append(mgmt_bot)
    print(f"✅ Management Bot: @{mgmt_bot.me.username} (ID: {mgmt_bot.me.id})")

    store_bots = await db.db.premium_bots.find({"status": {"$ne": "inactive"}}).to_list(length=None)
    for b in store_bots:
        tok = b.get("bot_token") or b.get("token")
        uname = b.get("bot_username") or b.get("username")
        if tok and tok != getattr(Config, "MGMT_BOT_TOKEN", ""):
            try:
                cli = Client(
                    name=f"bot_{uname or 'store'}_diag",
                    api_id=Config.API_ID,
                    api_hash=Config.API_HASH,
                    bot_token=tok,
                    in_memory=True
                )
                await cli.start()
                clients.append(cli)
                print(f"✅ Store Bot: @{uname} (ID: {cli.me.id})")
            except Exception as e:
                print(f"⚠️ Could not connect @{uname}: {e}")

    # Ask user for story ID or channel ID or test all ongoing
    print("\n" + "=" * 90)
    print("Let's test all ongoing channels with detailed error reasons:")
    print("=" * 90)

    stories = await db.db.premium_stories.find({"status": {"$in": ["Ongoing", "ongoing", "ONGOING"]}}).to_list(length=None)
    for s in stories:
        s_title = s.get("story_name_en") or s.get("story_name") or s.get("title")
        raw_source = s.get("source") or s.get("source_channel") or s.get("channel_id")
        if not raw_source:
            continue

        try:
            source_id = int(raw_source)
            if source_id > 0 and len(str(source_id)) >= 9:
                source_id = int(f"-100{source_id}")
        except Exception:
            source_id = raw_source

        print(f"\n📖 Story: '{s_title}' | Channel ID: {source_id}")

        found_admin = False
        for cli in clients:
            try:
                chat = await cli.get_chat(source_id)
                print(f"  🟢 @{cli.me.username} CAN ACCESS -> Title: '{chat.title}' (Type: {chat.type})")
                
                # Check admin rights
                try:
                    member = await cli.get_chat_member(source_id, cli.me.id)
                    print(f"     Status: {member.status} | Can Read Messages: True")
                except Exception as me_err:
                    print(f"     Member status check: {me_err}")

                found_admin = True
                break
            except Exception as e:
                err_str = f"{type(e).__name__}: {str(e)}"
                print(f"  🔴 @{cli.me.username} FAIL -> {err_str}")

        if not found_admin:
            print(f"  ⚠️ SUMMARY: None of the {len(clients)} connected bots have access to channel {source_id}.")
            print(f"     Please add @{mgmt_bot.me.username} as Administrator into this Telegram channel!")

    for cli in clients:
        try:
            await cli.stop()
        except Exception:
            pass

    print("\n" + "=" * 90)
    print("Diagnostic Complete!")


if __name__ == "__main__":
    asyncio.run(diagnose())
