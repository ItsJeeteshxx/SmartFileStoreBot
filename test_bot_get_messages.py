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
    # Let's try message IDs 55887, 55888 which we know exist in 9qola4.txt
    try:
        msgs = await app.get_messages(chat_id, [55887, 55888])
        for m in msgs:
            if m.empty:
                print(f"Message ID {m.id} is empty (deleted or inaccessible)")
            else:
                print(f"Msg ID {m.id}: {m.audio.file_name if m.audio else 'not audio'}")
    except Exception as e:
        print("Error getting messages:", e)
        
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
