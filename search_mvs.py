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
    print(f"Searching channel {chat_id} for 'MVS'...")
    count = 0
    async for m in app.search_messages(chat_id, query="MVS", limit=100):
        fname = ""
        if m.audio:
            fname = m.audio.file_name or "audio"
        elif m.document:
            fname = m.document.file_name or "doc"
        print(f"Msg ID {m.id}: {fname} (Caption: {m.caption or ''})")
        count += 1
        
    print(f"Found {count} messages.")
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
