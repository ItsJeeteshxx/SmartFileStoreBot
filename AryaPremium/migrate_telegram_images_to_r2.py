#!/usr/bin/env python3
"""
Migrate Telegram File ID Images to Cloudflare R2
=================================================
This script scans all stories in MongoDB ('stories' & 'premium_stories' collections),
downloads images that are stored as Telegram file_ids (AgAC... or /api/tg-image),
optimizes them to WebP format, uploads them to Cloudflare R2, and updates MongoDB
with permanent Cloudflare R2 public URLs.

Usage:
    python migrate_telegram_images_to_r2.py [--dry-run]
"""

import os
import sys
import io
import re
import uuid
import asyncio
import argparse
import aiohttp
from PIL import Image
from decouple import config

# Auto-load .env
_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_DIR)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_DIR, ".env"))
    load_dotenv(os.path.join(_PARENT, ".env"))
except ImportError:
    pass

import motor.motor_asyncio
import boto3

# Configs
MONGO_URI = (
    os.environ.get("MONGO_URI")
    or os.environ.get("DATABASE_URI")
    or os.environ.get("DATABASE")
    or config("MONGO_URI", default="")
    or config("DATABASE_URI", default="")
)
DB_NAME = (
    os.environ.get("DATABASE_NAME")
    or config("DATABASE_NAME", default="forward-bot")
)

R2_ACCOUNT_ID = (
    os.environ.get("R2_ACCOUNT_ID")
    or config("R2_ACCOUNT_ID", default="")
)
R2_ACCESS_KEY = (
    os.environ.get("R2_ACCESS_KEY_ID")
    or os.environ.get("R2_ACCESS_KEY")
    or config("R2_ACCESS_KEY_ID", default="")
    or config("R2_ACCESS_KEY", default="")
)
R2_SECRET_KEY = (
    os.environ.get("R2_SECRET_ACCESS_KEY")
    or os.environ.get("R2_SECRET_KEY")
    or config("R2_SECRET_ACCESS_KEY", default="")
    or config("R2_SECRET_KEY", default="")
)
R2_BUCKET = (
    os.environ.get("R2_BUCKET_NAME")
    or os.environ.get("R2_BUCKET")
    or config("R2_BUCKET_NAME", default="arya-images")
    or config("R2_BUCKET", default="arya-images")
)
R2_DOMAIN = (
    os.environ.get("R2_CUSTOM_DOMAIN")
    or os.environ.get("R2_DOMAIN")
    or config("R2_CUSTOM_DOMAIN", default="")
    or config("R2_DOMAIN", default="")
)

MGMT_BOT_TOKEN = os.environ.get("MGMT_BOT_TOKEN") or config("MGMT_BOT_TOKEN", default="")
BOT_TOKEN = os.environ.get("BOT_TOKEN") or config("BOT_TOKEN", default="")
SOURCE_BOT_TOKEN = os.environ.get("SOURCE_BOT_TOKEN") or config("SOURCE_BOT_TOKEN", default="")


def upload_to_r2(img_bytes: bytes, filename: str, content_type: str = "image/webp") -> str:
    """Uploads bytes to Cloudflare R2 bucket and returns public URL."""
    if not (R2_ACCOUNT_ID and R2_ACCESS_KEY and R2_SECRET_KEY and R2_BUCKET):
        raise ValueError("Missing Cloudflare R2 credentials in environment / .env")

    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto"
    )

    s3.put_object(
        Bucket=R2_BUCKET,
        Key=filename,
        Body=img_bytes,
        ContentType=content_type
    )

    if R2_DOMAIN:
        domain = R2_DOMAIN.strip("/")
        if not domain.startswith("http"):
            domain = "https://" + domain
        return f"{domain}/{filename}"
    else:
        return f"https://pub-{R2_ACCOUNT_ID[:32]}.r2.dev/{filename}"


def optimize_image(img_raw_bytes: bytes, quality: int = 80) -> bytes:
    """Converts image bytes to optimized WebP format."""
    img = Image.open(io.BytesIO(img_raw_bytes))
    if img.mode == "CMYK":
        img = img.convert("RGB")
    elif img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")

    output = io.BytesIO()
    img.save(output, format="WEBP", quality=quality, method=4)
    return output.getvalue()


async def download_telegram_file(file_id: str, tokens: list) -> bytes:
    """Downloads a file_id from Telegram Bot API trying each token in tokens list."""
    clean_id = file_id.strip()
    if not clean_id:
        return None

    # Handle /api/tg-image?file_id=...
    if "file_id=" in clean_id:
        import urllib.parse
        parsed = urllib.parse.urlparse(clean_id)
        params = urllib.parse.parse_qs(parsed.query)
        if params.get("file_id"):
            clean_id = params["file_id"][0]

    # If it's already an HTTP URL (e.g. Catbox or other external URL)
    if clean_id.startswith("http://") or clean_id.startswith("https://"):
        async with aiohttp.ClientSession() as session:
            async with session.get(clean_id, timeout=aiohttp.ClientTimeout(total=15.0)) as resp:
                if resp.status == 200:
                    return await resp.read()
        return None

    # Telegram Bot API getFile
    async with aiohttp.ClientSession() as session:
        for tok in tokens:
            if not tok:
                continue
            try:
                get_file_url = f"https://api.telegram.org/bot{tok}/getFile?file_id={clean_id}"
                async with session.get(get_file_url, timeout=aiohttp.ClientTimeout(total=10.0)) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    if not data.get("ok"):
                        continue
                    file_path = data.get("result", {}).get("file_path")
                    if not file_path:
                        continue

                    download_url = f"https://api.telegram.org/file/bot{tok}/{file_path}"
                    async with session.get(download_url, timeout=aiohttp.ClientTimeout(total=20.0)) as dl_resp:
                        if dl_resp.status == 200:
                            return await dl_resp.read()
            except Exception:
                continue
    return None


async def main():
    parser = argparse.ArgumentParser(description="Migrate story images from Telegram to Cloudflare R2")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without writing changes to MongoDB")
    args = parser.parse_args()

    print("=" * 60)
    print("🚀 Telegram Images ➔ Cloudflare R2 Migration Tool")
    print("=" * 60)

    if not MONGO_URI:
        print("❌ Error: MONGO_URI / DATABASE_URI is missing in .env")
        sys.exit(1)

    print(f"📦 Connecting to MongoDB: {MONGO_URI.split('@')[-1] if '@' in MONGO_URI else MONGO_URI[:20]}...")
    client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]

    # Collect all available bot tokens
    tokens = []
    for tok in [MGMT_BOT_TOKEN, BOT_TOKEN, SOURCE_BOT_TOKEN]:
        if tok and tok not in tokens:
            tokens.append(tok)

    try:
        cursor = db.premium_bots.find({"token": {"$exists": True, "$ne": ""}})
        async for b in cursor:
            t = b.get("token")
            if t and t not in tokens:
                tokens.append(t)
    except Exception:
        pass

    print(f"🔑 Available Bot Tokens for resolution: {len(tokens)}")
    print(f"☁️ Cloudflare R2 Bucket: {R2_BUCKET}")
    print(f"🌐 Cloudflare Domain: {R2_DOMAIN or 'Default R2 URL'}")
    if args.dry_run:
        print("⚠️ DRY RUN MODE: No changes will be written to database.")
    print("-" * 60)

    collections = ["stories", "premium_stories"]
    total_migrated = 0
    total_skipped = 0
    total_failed = 0

    for col_name in collections:
        col = db[col_name]
        try:
            count = await col.count_documents({})
        except Exception:
            continue

        if count == 0:
            continue

        print(f"\n📂 Checking Collection: '{col_name}' ({count} documents found)")
        cursor = col.find({})
        async for doc in cursor:
            doc_id = doc.get("_id")
            title = doc.get("title") or doc.get("story_name_en") or doc.get("story_id") or "Untitled Story"
            
            raw_img = (
                doc.get("poster_url")
                or doc.get("poster")
                or doc.get("image_url")
                or doc.get("cover")
                or doc.get("image")
            )

            if not raw_img:
                total_skipped += 1
                continue

            raw_str = str(raw_img).strip()

            # Check if it's already an R2 / Cloudflare link
            if ("r2.cloudflarestorage.com" in raw_str or 
                "r2.dev" in raw_str or 
                (R2_DOMAIN and R2_DOMAIN.strip("/") in raw_str)):
                # Already on R2
                total_skipped += 1
                continue

            # It's a Telegram file_id, Catbox, or tg-image endpoint!
            print(f"\n⚡ Processing: '{title}' (ID: {doc.get('story_id', doc_id)})")
            print(f"   Current Source: {raw_str[:60]}...")

            img_bytes = await download_telegram_file(raw_str, tokens)
            if not img_bytes:
                print(f"   ❌ Could not download image from Telegram or Source URL.")
                total_failed += 1
                continue

            try:
                optimized_bytes = optimize_image(img_bytes, quality=80)
                filename = f"{uuid.uuid4().hex}.webp"
                
                if not args.dry_run:
                    r2_url = upload_to_r2(optimized_bytes, filename, "image/webp")
                    # Update document in MongoDB
                    update_data = {
                        "poster_url": r2_url,
                        "image_url": r2_url,
                        "cover": r2_url,
                        "r2_migrated": True
                    }
                    if not doc.get("banner_url"):
                        update_data["banner_url"] = r2_url

                    await col.update_one({"_id": doc_id}, {"$set": update_data})
                    print(f"   ✅ Successfully Migrated to R2 ➔ {r2_url}")
                else:
                    print(f"   [DRY-RUN] Would upload {len(optimized_bytes)} bytes to R2 as {filename}")

                total_migrated += 1
            except Exception as e:
                print(f"   ❌ Failed to upload to Cloudflare R2: {e}")
                total_failed += 1

    print("\n" + "=" * 60)
    print("📊 MIGRATION SUMMARY:")
    print(f"   ✅ Successfully Migrated: {total_migrated}")
    print(f"   ⏭️ Already on R2 / Skipped: {total_skipped}")
    print(f"   ❌ Failed: {total_failed}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
