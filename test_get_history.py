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
    
    chat_id = -1003196305926
    print(f"Trying to get history of {chat_id}...")
    try:
        count = 0
        async for m in app.get_chat_history(chat_id, limit=5):
            print(f"Msg ID {m.id}: {m.media.value if m.media else 'text'}")
            count += 1
        print(f"Successfully fetched {count} messages.")
    except Exception as e:
        print(f"Error fetching history: {e}")
        
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
