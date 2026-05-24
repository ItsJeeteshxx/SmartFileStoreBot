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
    print(f"Scanning history of {chat_id}...")
    
    count = 0
    # Let's search using get_chat_history. Since we can't search, we just iterate.
    # To be fast, let's iterate up to 1000 messages.
    async for m in app.get_chat_history(chat_id, limit=1000):
        fname = ""
        if m.audio:
            fname = m.audio.file_name or ""
        elif m.document:
            fname = m.document.file_name or ""
        elif m.video:
            fname = m.video.file_name or ""
            
        if "MVS" in fname or "vampire" in fname.lower():
            print(f"Found in source: Msg ID {m.id}: {fname}")
            count += 1
            if count >= 30:
                break
                
    print(f"Scan complete. Found {count} matching messages in the last 1000.")
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
