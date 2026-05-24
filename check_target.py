import asyncio
from pyrogram import Client
from config import Config

async def main():
    # Start bot client
    app = Client(
        "temp_bot_client",
        api_id=Config.API_ID,
        api_hash=Config.API_HASH,
        bot_token=Config.BOT_TOKEN
    )
    await app.start()
    print("Bot started.")
    
    # Fetch messages 6100 to 6115
    chat_id = -1003825121436
    message_ids = list(range(6100, 6116))
    msgs = await app.get_messages(chat_id, message_ids)
    for m in msgs:
        if m.empty:
            print(f"Msg ID {m.id}: EMPTY")
            continue
        
        # Get filename or title
        fname = ""
        if m.audio:
            fname = m.audio.file_name or "audio file"
        elif m.document:
            fname = m.document.file_name or "document file"
        elif m.video:
            fname = m.video.file_name or "video file"
        
        print(f"Msg ID {m.id}: {fname} (Caption: {m.caption or ''})")
        
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
