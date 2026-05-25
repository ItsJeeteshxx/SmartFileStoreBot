import asyncio
import os
import sys
import aiohttp

# Add AryaPremium to the path
sys.path.insert(0, os.path.join(os.getcwd(), 'AryaPremium'))

from database import db
from ai_outpaint_service import process_outpaint
from mini_app_api import optimize_and_upload_to_storage

async def migrate_existing_banners():
    print("Initializing Database connection...")
    try:
        await db.connect()
    except Exception as e:
        print(f"Database connection failed: {e}")
        return

    print("Fetching existing premium stories...")
    stories = await db.db.premium_stories.find({}).to_list(length=None)
    print(f"Found {len(stories)} stories in database.")

    success_count = 0
    fail_count = 0

    for story in stories:
        story_id = str(story.get("_id"))
        story_name = story.get("story_name_en") or story.get("story_id")
        poster_url = story.get("poster_url") or story.get("cover") or story.get("image_url")
        existing_banner = story.get("banner_url")

        if not poster_url:
            print(f"[-] Skipping story '{story_name}' (No cover poster URL found)")
            continue

        if existing_banner:
            print(f"[!] Story '{story_name}' already has a banner: {existing_banner}")
            # If the user wants to re-run, we can prompt or force it. But let's run outpaint to ensure ALL get correct banners.
            print(f"[~] Re-generating outpainted banner for '{story_name}' to ensure optimal cinematic widescreen layout...")

        print(f"[+] Outpainting story banner for: '{story_name}'")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(poster_url) as resp:
                    if resp.status != 200:
                        print(f"  [x] Failed to download poster from: {poster_url}")
                        fail_count += 1
                        continue
                    poster_bytes = await resp.read()

            # Process outpainting using the active provider or the high-quality blurred fallback
            outpainted_bytes = await process_outpaint(poster_bytes)

            # Upload the wide banner to R2 / Catbox with quality compression
            uploaded_banner_url = await optimize_and_upload_to_storage(
                outpainted_bytes, width=1184, height=556, quality=80
            )

            if uploaded_banner_url:
                await db.db.premium_stories.update_one(
                    {"_id": story["_id"]},
                    {"$set": {"banner_url": uploaded_banner_url}}
                )
                print(f"  [✓] Banner successfully created and uploaded: {uploaded_banner_url}")
                success_count += 1
            else:
                print("  [x] Upload failed.")
                fail_count += 1

        except Exception as e:
            print(f"  [x] Error outpainting banner for '{story_name}': {e}")
            fail_count += 1

        # Brief sleep between calls to respect rate limits and API quotas
        await asyncio.sleep(1.5)

    print("\n--- Outpaint Migration Summary ---")
    print(f"Successfully processed: {success_count} stories.")
    print(f"Failed/Skipped: {fail_count} stories.")
    print("----------------------------------\n")

if __name__ == "__main__":
    asyncio.run(migrate_existing_banners())
