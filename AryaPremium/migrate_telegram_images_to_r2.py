#!/usr/bin/env python3
"""
Migrate Telegram File ID Images to Cloudflare R2
=================================================
This script scans all databases and collections across all MongoDB URIs
found in AryaForwardBot and riyanew_disk, checks every single collection
for story/media image fields, downloads Telegram file_ids, optimizes them,
uploads them to Cloudflare R2, and updates MongoDB.

Usage:
    python AryaPremium/migrate_telegram_images_to_r2.py [--dry-run]
"""

import os
import sys
import io
import re
import uuid
import glob
import asyncio
import argparse
import aiohttp
from PIL import Image

_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_DIR)
_GRANDPARENT = os.path.dirname(_PARENT)

def _read_env_file(filepath):
    env_dict = {}
    try:
        with open(filepath, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    _k = _k.strip()
                    _v = _v.strip().strip("'").strip('"')
                    env_dict[_k] = _v
    except Exception:
        pass
    return env_dict

def _extract_mongo_uris_from_file(filepath):
    uris = set()
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
            matches = re.findall(r'mongodb(?:\+srv)?://[^\s"\'\n]+', content)
            for m in matches:
                uris.add(m.strip().strip('"').strip("'"))
    except Exception:
        pass
    return uris

# Find all MongoDB URIs from all files in AryaForwardBot and riyanew_disk
all_mongo_uris = set()
all_bot_tokens = set()

search_dirs = [
    _DIR,
    _PARENT,
    os.path.join(_GRANDPARENT, "riyanew_disk"),
    "/home/ubuntu/riyanew_disk",
    os.getcwd()
]

for sdir in search_dirs:
    if not os.path.exists(sdir):
        continue
    for fname in [".env", "config.env", "config.py", "settings.py", "database.py", "mongo_search.py", "stories_bot.py"]:
        fpath = os.path.join(sdir, fname)
        if os.path.isfile(fpath):
            # Parse key-values
            env_map = _read_env_file(fpath)
            for k, v in env_map.items():
                if "mongo" in k.lower() or "database" in k.lower() or "db" in k.lower():
                    if "mongodb" in v:
                        all_mongo_uris.add(v)
                if "token" in k.lower() and len(v) > 20 and ":" in v:
                    all_bot_tokens.add(v)
            # Regex scan
            for u in _extract_mongo_uris_from_file(fpath):
                all_mongo_uris.add(u)

# System environment variables
for k, v in os.environ.items():
    if ("mongo" in k.lower() or "database" in k.lower() or "db" in k.lower()) and "mongodb" in str(v):
        all_mongo_uris.add(v)
    if "token" in k.lower() and len(str(v)) > 20 and ":" in str(v):
        all_bot_tokens.add(v)

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID") or "d738aa13a7944050a7edb60cc5cd91bb"
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY_ID") or os.environ.get("R2_ACCESS_KEY") or ""
R2_SECRET_KEY = os.environ.get("R2_SECRET_ACCESS_KEY") or os.environ.get("R2_SECRET_KEY") or ""
R2_BUCKET = os.environ.get("R2_BUCKET_NAME") or "arya-images"
R2_DOMAIN = os.environ.get("R2_CUSTOM_DOMAIN") or "https://pub-d738aa13a7944050a7edb60cc5cd91bb.r2.dev"


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
        async with aiohttp.ClientSession() as session:
            async with session.get(clean_id, timeout=aiohttp.ClientTimeout(total=15.0)) as resp:
                if resp.status == 200:
                    return await resp.read()
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
    
    parser = argparse.ArgumentParser(description="Migrate story images from Telegram to Cloudflare R2")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without writing changes to MongoDB")
    parser.add_argument("--force", action="store_true", help="Re-upload even if already has a URL")
    args = parser.parse_args()

    print("=" * 60)
    print("🚀 Telegram Images ➔ Cloudflare R2 Migration Tool")
    print("=" * 60)
    print(f"📦 Discovered MongoDB URIs: {len(all_mongo_uris)}")
    print(f"🔑 Discovered Bot Tokens: {len(all_bot_tokens)}")
    print(f"☁️ Cloudflare R2 Bucket: {R2_BUCKET}")
    print(f"🌐 Cloudflare Domain: {R2_DOMAIN}")
    if args.dry_run:
        print("⚠️ DRY RUN MODE: No changes will be written to MongoDB.")
    print("-" * 60)

    total_migrated = 0
    total_skipped = 0
    total_failed = 0

    token_list = list(all_bot_tokens)

    for uri in all_mongo_uris:
        masked_uri = uri.split('@')[-1] if '@' in uri else uri[:25]
        print(f"\n📡 Connecting to Cluster: {masked_uri}...")
        try:
            client = motor.motor_asyncio.AsyncIOMotorClient(uri)
            all_dbs = await client.list_database_names()
            target_dbs = [d for d in all_dbs if d not in ("admin", "local", "config")]
        except Exception as e:
            print(f"❌ Failed to connect to {masked_uri}: {e}")
            continue

        for dname in target_dbs:
            db = client[dname]
            try:
                col_list = await db.list_collection_names()
            except Exception:
                continue

            for col_name in col_list:
                if col_name.startswith("system."):
                    continue

                col = db[col_name]
                try:
                    count = await col.count_documents({})
                except Exception:
                    continue

                if count == 0:
                    continue

                # Check first document to see if this collection has images / stories
                sample = await col.find_one({})
                if not sample:
                    continue

                # Check if sample has any media or story fields
                has_image_field = any(
                    k in sample for k in [
                        "poster_url", "poster", "image_url", "cover", "image", 
                        "image_path", "thumb", "thumbnail", "photo", "banner_url", "banner"
                    ]
                ) or ("parts" in sample and isinstance(sample["parts"], list))

                if not has_image_field:
                    continue

                print(f"\n📂 Database: '{dname}' ➔ Collection: '{col_name}' ({count} documents with image fields found)")
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
                        or doc.get("image_path")
                        or doc.get("thumb")
                        or doc.get("thumbnail")
                        or doc.get("photo")
                        or doc.get("banner_url")
                        or doc.get("banner")
                    )

                    if not raw_img and doc.get("parts") and isinstance(doc["parts"], list) and len(doc["parts"]) > 0:
                        first_part = doc["parts"][0]
                        if isinstance(first_part, dict):
                            raw_img = first_part.get("file_id") or first_part.get("thumb")

                    if not raw_img:
                        total_skipped += 1
                        continue

                    raw_str = str(raw_img).strip()

                    # Check if it's already an R2 / Cloudflare link
                    is_r2 = (
                        "r2.cloudflarestorage.com" in raw_str
                        or "r2.dev" in raw_str
                        or (R2_DOMAIN and R2_DOMAIN.strip("/") in raw_str)
                    )

                    if is_r2 and not args.force:
                        print(f"   • '{title}': ⏭️ Already on Cloudflare R2")
                        total_skipped += 1
                        continue

                    print(f"\n   ⚡ Processing '{title}' (ID: {doc.get('story_id', doc_id)})")
                    print(f"      Source Image: {raw_str[:70]}...")

                    img_bytes = await download_telegram_file(raw_str, token_list)
                    if not img_bytes:
                        print(f"      ❌ Could not download image from Telegram or Source URL.")
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
