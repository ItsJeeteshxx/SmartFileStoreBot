#!/usr/bin/env python3
"""
Migrate Telegram File ID Images to Cloudflare R2
=================================================
Strict Arya Bot story image migration to Cloudflare R2.
Scans only Arya databases ('arya', 'forward-bot', 'arya_premium').

Usage:
    python AryaPremium/migrate_telegram_images_to_r2.py [--dry-run]
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

def _inject_env(filepath):
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

for path in [
    os.path.join(_DIR, ".env"),
    os.path.join(_PARENT, ".env"),
    os.path.join(_DIR, "config.env"),
    os.path.join(_PARENT, "config.env"),
    os.path.join(os.getcwd(), ".env"),
]:
    _inject_env(path)

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

MONGO_URI = (
    os.environ.get("MONGO_URI")
    or os.environ.get("DATABASE_URI")
    or os.environ.get("DATABASE")
    or root_db_uri
    or prem_db_uri
    or ""
)

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID") or "d738aa13a7944050a7edb60cc5cd91bb"
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY_ID") or os.environ.get("R2_ACCESS_KEY") or ""
R2_SECRET_KEY = os.environ.get("R2_SECRET_ACCESS_KEY") or os.environ.get("R2_SECRET_KEY") or ""
R2_BUCKET = os.environ.get("R2_BUCKET_NAME") or "arya-images"
R2_DOMAIN = os.environ.get("R2_CUSTOM_DOMAIN") or "https://pub-d738aa13a7944050a7edb60cc5cd91bb.r2.dev"

MGMT_BOT_TOKEN = os.environ.get("MGMT_BOT_TOKEN") or ""
BOT_TOKEN = os.environ.get("BOT_TOKEN") or ""
SOURCE_BOT_TOKEN = os.environ.get("SOURCE_BOT_TOKEN") or ""


def upload_to_r2(img_bytes: bytes, filename: str, content_type: str = "image/webp") -> str:
    import boto3
    if not (R2_ACCOUNT_ID and R2_ACCESS_KEY and R2_SECRET_KEY and R2_BUCKET):
        raise ValueError("Missing Cloudflare R2 credentials in environment!")

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
    img = Image.open(io.BytesIO(img_raw_bytes))
    if img.mode == "CMYK":
        img = img.convert("RGB")
    elif img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")

    output = io.BytesIO()
    img.save(output, format="WEBP", quality=quality, method=4)
    return output.getvalue()


async def download_telegram_file(file_id: str, bot_tokens: list) -> bytes:
    clean_id = file_id.strip()
    if not clean_id:
        return None

    if "file_id=" in clean_id:
        import urllib.parse
        parsed = urllib.parse.urlparse(clean_id)
        params = urllib.parse.parse_qs(parsed.query)
        if params.get("file_id"):
            clean_id = params["file_id"][0]

    if clean_id.startswith("http://") or clean_id.startswith("https://"):
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        for attempt in range(3):
            try:
                async with aiohttp.ClientSession(headers=headers) as session:
                    async with session.get(clean_id, timeout=aiohttp.ClientTimeout(total=20.0)) as resp:
                        if resp.status == 200:
                            return await resp.read()
            except Exception:
                await asyncio.sleep(0.8)
        return None

    async with aiohttp.ClientSession() as session:
        for tok in bot_tokens:
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
    
    parser = argparse.ArgumentParser(description="Migrate story images to Cloudflare R2")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without writing changes to MongoDB")
    parser.add_argument("--force", action="store_true", help="Re-upload even if already has a URL")
    args = parser.parse_args()

    print("=" * 60)
    print("🚀 Arya Story Images ➔ Cloudflare R2 Migration Tool")
    print("=" * 60)

    if not MONGO_URI:
        print("❌ Error: MongoDB URI not found in .env")
        sys.exit(1)

    print(f"📦 Connecting to MongoDB: {MONGO_URI.split('@')[-1] if '@' in MONGO_URI else MONGO_URI[:25]}...")
    client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)

    # Strictly target ONLY Arya databases, never sample_mflix or system DBs
    target_dbs = ["arya", "forward-bot", "arya_premium"]
    print(f"🗄️ Target Arya Databases: {target_dbs}")

    tokens = []
    for tok in [MGMT_BOT_TOKEN, BOT_TOKEN, SOURCE_BOT_TOKEN]:
        if tok and tok not in tokens:
            tokens.append(tok)

    for dname in target_dbs:
        try:
            db_inst = client[dname]
            cursor = db_inst.premium_bots.find({"token": {"$exists": True, "$ne": ""}})
            async for b in cursor:
                t = b.get("token")
                if t and t not in tokens:
                    tokens.append(t)
        except Exception:
            pass

    print(f"🔑 Available Bot Tokens: {len(tokens)}")
    print(f"☁️ Cloudflare R2 Bucket: {R2_BUCKET}")
    print(f"🌐 Cloudflare Domain: {R2_DOMAIN}")
    if args.dry_run:
        print("⚠️ DRY RUN MODE: No changes will be written to MongoDB.")
    print("-" * 60)

    target_collections = ["stories", "premium_stories", "shows", "mini_app_banners"]
    total_migrated = 0
    total_skipped = 0
    total_failed = 0

    for dname in target_dbs:
        db = client[dname]
        try:
            col_list = await db.list_collection_names()
        except Exception:
            continue

        matched_cols = [c for c in col_list if c in target_collections]
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

            print(f"\n📂 Database: '{dname}' ➔ Collection: '{col_name}' ({count} documents)")
            cursor = col.find({})
            async for doc in cursor:
                doc_id = doc.get("_id")
                title = (
                    doc.get("title")
                    or doc.get("story_name_en")
                    or doc.get("story_name")
                    or doc.get("clean_title")
                    or doc.get("name")
                    or doc.get("story_id")
                    or str(doc_id)
                )

                raw_img = (
                    doc.get("poster_url")
                    or doc.get("poster")
                    or doc.get("image_url")
                    or doc.get("cover")
                    or doc.get("image")
                    or doc.get("thumb")
                    or doc.get("banner_url")
                    or doc.get("banner")
                )

                if not raw_img and doc.get("parts") and isinstance(doc["parts"], list) and len(doc["parts"]) > 0:
                    first_part = doc["parts"][0]
                    if isinstance(first_part, dict):
                        raw_img = first_part.get("file_id") or first_part.get("thumb")

                if not raw_img:
                    print(f"   • '{title}': (Delivery Story - No poster image saved)")
                    total_skipped += 1
                    continue

                raw_str = str(raw_img).strip()

                # Ensure metadata integrity for all stories
                missing_meta = {}
                if not doc.get("story_id"):
                    missing_meta["story_id"] = str(doc_id)
                if not doc.get("visibility"):
                    missing_meta["visibility"] = "available"
                if not doc.get("status"):
                    missing_meta["status"] = "Completed"
                if not doc.get("created_at"):
                    from datetime import datetime, timezone
                    missing_meta["created_at"] = datetime.now(timezone.utc)
                if not doc.get("uploaded_at"):
                    from datetime import datetime, timezone
                    missing_meta["uploaded_at"] = datetime.now(timezone.utc)

                if missing_meta and not args.dry_run:
                    await col.update_one({"_id": doc_id}, {"$set": missing_meta})

                # Check if it's already an R2 / Cloudflare link
                is_r2 = (
                    "r2.cloudflarestorage.com" in raw_str
                    or "r2.dev" in raw_str
                    or (R2_DOMAIN and R2_DOMAIN.strip("/") in raw_str)
                )

                if is_r2 and not args.force:
                    print(f"   • '{title}': ⏭️ Already on Cloudflare R2 ({raw_str[:50]}...)")
                    total_skipped += 1
                    continue

                print(f"\n   ⚡ Migrating '{title}'...")
                print(f"      Source Image: {raw_str[:70]}...")

                img_bytes = await download_telegram_file(raw_str, tokens)
                if not img_bytes:
                    print(f"      ❌ Could not download image from Telegram.")
                    total_failed += 1
                    continue

                try:
                    optimized_bytes = optimize_image(img_bytes, quality=80)
                    filename = f"{uuid.uuid4().hex}.webp"
                    
                    if not args.dry_run:
                        r2_url = upload_to_r2(optimized_bytes, filename, "image/webp")
                        update_data = {
                            "poster_url": r2_url,
                            "image_url": r2_url,
                            "cover": r2_url,
                            "image": r2_url,
                            "r2_migrated": True
                        }
                        if not doc.get("banner_url"):
                            update_data["banner_url"] = r2_url

                        await col.update_one({"_id": doc_id}, {"$set": update_data})
                        print(f"      ✅ Successfully Migrated to R2 ➔ {r2_url}")
                    else:
                        print(f"      [DRY-RUN] Would upload {len(optimized_bytes)} bytes to R2 as {filename}")

                    total_migrated += 1
                except Exception as e:
                    print(f"      ❌ Failed to upload to Cloudflare R2: {e}")
                    total_failed += 1

    print("\n" + "=" * 60)
    print("📊 MIGRATION SUMMARY:")
    print(f"   ✅ Successfully Migrated: {total_migrated}")
    print(f"   ⏭️ Already on R2 / Skipped: {total_skipped}")
    print(f"   ❌ Failed: {total_failed}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
