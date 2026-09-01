#!/usr/bin/env python3
"""
Migrate Telegram File ID Images to Cloudflare R2
=================================================
This script scans all databases and story collections in MongoDB,
downloads images that are stored as Telegram file_ids (AgAC... or /api/tg-image),
optimizes them to WebP format, uploads them to Cloudflare R2, and updates MongoDB
with permanent Cloudflare R2 public URLs.

Usage:
    python AryaPremium/migrate_telegram_images_to_r2.py [--dry-run] [--db-name <DB>]
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

_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_DIR)

if _DIR not in sys.path:
    sys.path.insert(0, _DIR)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

def _inject_env(filepath):
    """Read a .env file and inject values into os.environ."""
    try:
        with open(filepath, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    _k = _k.strip()
                    _v = _v.strip().strip("'").strip('"')
                    os.environ.setdefault(_k, _v)
    except Exception:
        pass

# Inject from all possible env file locations
for path in [
    os.path.join(_DIR, ".env"),
    os.path.join(_PARENT, ".env"),
    os.path.join(_DIR, "config.env"),
    os.path.join(_PARENT, "config.env"),
    os.path.join(os.getcwd(), ".env"),
    os.path.join(os.getcwd(), "config.env"),
]:
    _inject_env(path)

# Try importing Root Config / Premium Config as fallback
root_db_uri = ""
prem_db_uri = ""
try:
    from config import Config as RootConfig
    root_db_uri = getattr(RootConfig, "DATABASE_URI", "") or getattr(RootConfig, "DATABASE", "") or ""
except Exception:
    pass

try:
    from AryaPremium.config import Config as PremConfig
    prem_db_uri = getattr(PremConfig, "MONGO_URI", "") or getattr(PremConfig, "DATABASE_URI", "") or ""
except Exception:
    pass

# MongoDB URI Resolution
MONGO_URI = (
    os.environ.get("MONGO_URI")
    or os.environ.get("DATABASE_URI")
    or os.environ.get("DATABASE")
    or os.environ.get("MONGODB_URI")
    or os.environ.get("MONGO_URL")
    or os.environ.get("DB_URI")
    or root_db_uri
    or prem_db_uri
    or ""
)

# Cloudflare R2 Credentials
R2_ACCOUNT_ID = (
    os.environ.get("R2_ACCOUNT_ID")
    or os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    or ""
)
R2_ACCESS_KEY = (
    os.environ.get("R2_ACCESS_KEY_ID")
    or os.environ.get("R2_ACCESS_KEY")
    or os.environ.get("CLOUDFLARE_R2_ACCESS_KEY_ID")
    or ""
)
R2_SECRET_KEY = (
    os.environ.get("R2_SECRET_ACCESS_KEY")
    or os.environ.get("R2_SECRET_KEY")
    or os.environ.get("CLOUDFLARE_R2_SECRET_ACCESS_KEY")
    or ""
)
R2_BUCKET = (
    os.environ.get("R2_BUCKET_NAME")
    or os.environ.get("R2_BUCKET")
    or "arya-images"
)
R2_DOMAIN = (
    os.environ.get("R2_CUSTOM_DOMAIN")
    or os.environ.get("R2_DOMAIN")
    or ""
)

# Bot Tokens for telegram file resolution
MGMT_BOT_TOKEN = os.environ.get("MGMT_BOT_TOKEN") or ""
BOT_TOKEN = os.environ.get("BOT_TOKEN") or ""
SOURCE_BOT_TOKEN = os.environ.get("SOURCE_BOT_TOKEN") or ""


def upload_to_r2(img_bytes: bytes, filename: str, content_type: str = "image/webp") -> str:
    """Uploads bytes to Cloudflare R2 bucket and returns public URL."""
    import boto3
    if not (R2_ACCOUNT_ID and R2_ACCESS_KEY and R2_SECRET_KEY and R2_BUCKET):
        raise ValueError(
            "Missing Cloudflare R2 credentials! Ensure R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY are set in .env"
        )

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

    # If it's already an HTTP URL (e.g. Catbox or external link)
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
    import motor.motor_asyncio
    
    parser = argparse.ArgumentParser(description="Migrate story images from Telegram to Cloudflare R2")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without writing changes to MongoDB")
    parser.add_argument("--mongo-uri", default="", help="Override MongoDB URI")
    parser.add_argument("--db-name", default="", help="Specific MongoDB Database Name")
    args = parser.parse_args()

    active_mongo_uri = args.mongo_uri or MONGO_URI

    print("=" * 60)
    print("🚀 Telegram Images ➔ Cloudflare R2 Migration Tool")
    print("=" * 60)

    if not active_mongo_uri:
        print("❌ Error: MongoDB URI not found in .env (checked MONGO_URI, DATABASE_URI, DATABASE)")
        sys.exit(1)

    print(f"📦 Connecting to MongoDB: {active_mongo_uri.split('@')[-1] if '@' in active_mongo_uri else active_mongo_uri[:25]}...")
    client = motor.motor_asyncio.AsyncIOMotorClient(active_mongo_uri)

    # Auto-discover databases on cluster
    if args.db_name:
        target_db_names = [args.db_name]
    else:
        try:
            all_dbs = await client.list_database_names()
            target_db_names = [d for d in all_dbs if d not in ("admin", "local", "config")]
        except Exception:
            target_db_names = ["arya", "forward-bot", "arya_premium"]

    print(f"🗄️ Discovered Databases on cluster: {target_db_names}")

    # Collect all available bot tokens from environment and DB
    tokens = []
    for tok in [MGMT_BOT_TOKEN, BOT_TOKEN, SOURCE_BOT_TOKEN]:
        if tok and tok not in tokens:
            tokens.append(tok)

    for dname in target_db_names:
        try:
            db_inst = client[dname]
            cursor = db_inst.premium_bots.find({"token": {"$exists": True, "$ne": ""}})
            async for b in cursor:
                t = b.get("token")
                if t and t not in tokens:
                    tokens.append(t)
        except Exception:
            pass

    print(f"🔑 Available Bot Tokens for image resolution: {len(tokens)}")
    print(f"☁️ Cloudflare R2 Bucket: {R2_BUCKET}")
    print(f"🌐 Cloudflare Domain: {R2_DOMAIN or 'Default R2 URL'}")
    if args.dry_run:
        print("⚠️ DRY RUN MODE: No changes will be written to MongoDB.")
    print("-" * 60)

    target_collections = ["stories", "premium_stories", "shows", "mini_app_banners"]
    total_migrated = 0
    total_skipped = 0
    total_failed = 0

    for dname in target_db_names:
        db = client[dname]
        try:
            col_list = await db.list_collection_names()
        except Exception:
            col_list = target_collections

        matched_cols = [c for c in col_list if c in target_collections or "stor" in c.lower() or "banner" in c.lower()]
        if not matched_cols:
            continue

        for col_name in matched_cols:
            col = db[col_name]
            try:
                count = await col.count_documents({})
            except Exception:
                continue

            if count == 0:
                continue

            print(f"\n📂 Database: '{dname}' ➔ Collection: '{col_name}' ({count} documents found)")
            cursor = col.find({})
            async for doc in cursor:
                doc_id = doc.get("_id")
                title = (
                    doc.get("title")
                    or doc.get("story_name_en")
                    or doc.get("story_name")
                    or doc.get("name")
                    or doc.get("story_id")
                    or doc.get("clean_title")
                    or "Untitled Story"
                )
                
                raw_img = (
                    doc.get("poster_url")
                    or doc.get("poster")
                    or doc.get("image_url")
                    or doc.get("cover")
                    or doc.get("image")
                    or doc.get("image_path")
                )

                if not raw_img:
                    total_skipped += 1
                    continue

                raw_str = str(raw_img).strip()

                # Check if it's already an R2 / Cloudflare link
                if ("r2.cloudflarestorage.com" in raw_str or 
                    "r2.dev" in raw_str or 
                    (R2_DOMAIN and R2_DOMAIN.strip("/") in raw_str)):
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
