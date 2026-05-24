import asyncio
from pyrogram import Client
from config import Config

async def main():
    app = Client(
        "temp_bot_client",
        api_id=Config.API_ID,
        api_hash=Config.API_HASH,
        bot_token=Config.BOT_TOKEN
    )
    await app.start()
    print("Bot started.")
    
    # Target channel ID is in the target.txt or canbox_log.txt metadata. Let's find it.
    # In target.txt: Target Channel : Pocket Arya Store (ID: -1002047395029) or (ID: -1002097003456)
    # Let's try both target chat IDs or load them.
    # Wait, target.txt can tell us. Let's read the target chat ID.
    target_chats = [-1003825121436]
    for chat_id in target_chats:
        try:
            print(f"Trying target chat {chat_id}...")
            msgs = await app.get_messages(chat_id, [6106, 6107])
            for m in msgs:
                if m.empty:
                    print(f"Msg ID {m.id} is empty.")
                else:
                    print(f"Msg ID {m.id}: {m.audio.file_name if m.audio else 'not audio'}")
                    if m.forward_date:
                        print(f"  Forwarded from: {m.forward_from_chat} (ID: {m.forward_from_chat.id if m.forward_from_chat else 'None'})")
                        print(f"  Forward message ID: {m.forward_from_message_id}")
                    else:
                        print("  No forward info.")
        except Exception as e:
            print(f"Error for chat {chat_id}: {e}")
            
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
