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
    try:
        m = await app.get_messages(chat_id, 56105)
        print(f"Msg ID {m.id}: {m.media.value if m.media else 'text'}")
        if m.audio:
            print("audio file_name:", m.audio.file_name)
    except Exception as e:
        print("Error getting message:", e)
        
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
