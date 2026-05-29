import sqlite3
import asyncio
from pathlib import Path
import pyrogram.storage.sqlite_storage

# Save original open
original_open = pyrogram.storage.sqlite_storage.SQLiteStorage.open

async def patched_open(self):
    await original_open(self)
    try:
        with self.conn:
            self.conn.execute("""
            CREATE TABLE IF NOT EXISTS update_state (
                id   INTEGER PRIMARY KEY,
                pts  INTEGER,
                qts  INTEGER,
                date INTEGER,
                seq  INTEGER
            );
            """)
            self.conn.execute("""
            CREATE TABLE IF NOT EXISTS usernames (
                id       INTEGER,
                username TEXT,
                FOREIGN KEY (id) REFERENCES peers(id)
            );
            """)
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_usernames_username ON usernames (username);")
    except Exception as e:
        print(f"Error inside patched open: {e}")

# Replace
pyrogram.storage.sqlite_storage.SQLiteStorage.open = patched_open

async def test():
    # Instantiate SQLiteStorage
    storage = pyrogram.storage.sqlite_storage.SQLiteStorage("test_dummy", Path("."))
    # Open the storage (this will create test_dummy.session and run patched_open)
    await storage.open()
    # Check if tables exist
    cursor = storage.conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [r[0] for r in cursor.fetchall()]
    print("Tables in DB:", tables)
    await storage.close()
    import os
    if os.path.exists("test_dummy.session"):
        os.remove("test_dummy.session")

asyncio.run(test())
