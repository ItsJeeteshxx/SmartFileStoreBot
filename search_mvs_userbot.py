import sqlite3
import asyncio
from pyrogram import Client
from config import Config

# Drop the update_state table to fix schema version mismatch
conn = sqlite3.connect("my_account.session")
cursor = conn.cursor()
try:
    cursor.execute("DROP TABLE update_state;")
    conn.commit()
    print("Successfully dropped update_state table.")
except Exception as e:
    print(f"Error dropping table (it might not exist or other error): {e}")
conn.close()

async def main():
    app = Client(
        "my_account",
        api_id=Config.API_ID,
        api_hash=Config.API_HASH
    )
    await app.start()
    print("Userbot started successfully!")
    
    chat_id = -1003196305926
    print(f"Searching channel {chat_id} using userbot...")
    
    # Search for files with "143" or "144" in name
    count = 0
    async for m in app.search_messages(chat_id, query="MVS", limit=100):
        fname = ""
        if m.audio:
            fname = m.audio.file_name or ""
        elif m.document:
            fname = m.document.file_name or ""
            
        print(f"Msg ID {m.id}: {fname} (Caption: {m.caption or ''})")
        count += 1
        
    print(f"Search complete. Found {count} messages.")
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
