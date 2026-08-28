import asyncio
import os
import sys
import logging

sys.stdout.reconfigure(encoding='utf-8')
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("StoryScanner")

# Set paths
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pyrogram import Client
from config import Config
from database import db
from utils import scan_and_index_all_stories

async def main():
    logger.info("Initializing Pyrogram Management Client...")
    app = Client(
        "StoryScannerSession",
        api_id=Config.API_ID,
        api_hash=Config.API_HASH,
        bot_token=Config.BOT_TOKEN
    )
    await app.start()
    me = await app.get_me()
    logger.info(f"Connected as @{me.username} ({me.id})")
    
    async def cli_progress(idx, total, name, valid_count, ok):
        status = f"✅ {valid_count} files" if ok else "❌ Failed"
        print(f"[{idx}/{total}] {name[:30]:<30} -> {status}", flush=True)

    print("=========================================================")
    print("  ARYA PREMIUM BULK STORY FILE SCANNER & AUTO-INDEXER   ")
    print("=========================================================")
    results = await scan_and_index_all_stories(app, db=db, progress_cb=cli_progress)
    
    print("\n---------------------------------------------------------")
    print(f"Summary: Total: {results['total']} | Success: {results['success']} | Failed: {results['failed']}")
    print("---------------------------------------------------------")
    
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
