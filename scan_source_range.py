import asyncio
from pyrogram import Client
from config import Config
import re

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
    start_id = 57118
    end_id = 65000
    chunk_size = 200
    
    print(f"Scanning range {start_id} to {end_id}...")
    
    found_count = 0
    
    for i in range(start_id, end_id, chunk_size):
        chunk_end = min(i + chunk_size, end_id)
        msg_ids = list(range(i, chunk_end))
        try:
            msgs = await app.get_messages(chat_id, msg_ids)
            for m in msgs:
                if m.empty:
                    continue
                fname = ""
                if m.audio:
                    fname = m.audio.file_name or ""
                elif m.document:
                    fname = m.document.file_name or ""
                
                if fname:
                    # check if it contains MVS or vampire or digits
                    fname_lower = fname.lower()
                    if "mvs" in fname_lower or "vampire" in fname_lower:
                        print(f"Source Msg ID {m.id}: {fname}")
                        found_count += 1
                        
            # Sleep slightly to avoid flood
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f"Error at chunk {i}-{chunk_end}: {e}")
            await asyncio.sleep(5)
            
    print(f"Scan finished. Found {found_count} matching files.")
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
