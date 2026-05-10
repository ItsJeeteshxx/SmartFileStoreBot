import asyncio
import aiohttp
from pyrogram import Client
from database import db
from config import Config
import os
import logging

logging.basicConfig(level=logging.INFO)

async def upload_to_catbox(file_path):
    async with aiohttp.ClientSession() as session:
        data = aiohttp.FormData()
        data.add_field("reqtype", "fileupload")
        data.add_field("userhash", "")
        data.add_field("fileToUpload", open(file_path, "rb"), filename=os.path.basename(file_path))
        async with session.post("https://catbox.moe/user/api.php", data=data) as resp:
            if resp.status == 200:
                return await resp.text()
            else:
                text = await resp.text()
                print(f"Catbox error {resp.status}: {text}")
    return None

async def main():
    print("Connecting to DB...")
    await db.connect()
    print("Starting client...")
    app = Client("cache_bot", api_id=Config.API_ID, api_hash=Config.API_HASH, bot_token=Config.MGMT_BOT_TOKEN)
    await app.start()
    
    stories = await db.db.premium_stories.find({"image": {"$ne": None}, "poster_url": {"$exists": False}}).to_list(length=None)
    print(f"Found {len(stories)} stories missing poster_url.")
    
    for s in stories:
        file_id = s["image"]
        print(f"Processing story {s.get('story_name_en', s.get('_id'))} - {file_id}")
        try:
            # Send photo to owner to refresh file_reference
            target_user = Config.OWNER_IDS[0] if Config.OWNER_IDS else "me"
            msg = await app.send_photo(target_user, photo=file_id)
            dl_path = await app.download_media(msg)
            if dl_path:
                url = await upload_to_catbox(dl_path)
                if url:
                    print(f" -> Uploaded! URL: {url}")
                    await db.db.premium_stories.update_one({"_id": s["_id"]}, {"$set": {"poster_url": url}})
                os.remove(dl_path)
            await msg.delete()
        except Exception as e:
            print(f" -> Failed: {e}")
            
    await app.stop()
    print("Finished caching all existing posters!")

if __name__ == "__main__":
    asyncio.run(main())
