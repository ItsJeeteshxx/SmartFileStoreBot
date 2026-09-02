import os
import sys
import asyncio
import aiohttp
from bson.objectid import ObjectId

# Setup import paths
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

try:
    from database import db
    from config import Config
except ImportError:
    try:
        from AryaPremium.database import db
        from AryaPremium.config import Config
    except ImportError:
        print("❌ Could not import database or config. Make sure you run from AryaPremium or TryAryaBot directory.")
        sys.exit(1)


async def check_resource(session: aiohttp.ClientSession, url: str) -> tuple[str, str]:
    """Checks if an image URL or local file exists and is accessible."""
    if not url or not str(url).strip():
        return "EMPTY", "No image URL or file path provided (Showing Default Logo)"

    url_str = str(url).strip()

    # Case 1: Local file in /api/uploads/
    if url_str.startswith("/api/uploads/"):
        fname = url_str.replace("/api/uploads/", "")
        possible_paths = [
            os.path.join(os.getcwd(), "uploads", fname),
            os.path.join(os.getcwd(), "AryaPremium", "uploads", fname),
            os.path.join(os.path.expanduser("~"), "bot", "TryAryaForwardBot", "AryaPremium", "uploads", fname)
        ]
        found_path = None
        for p in possible_paths:
            if os.path.exists(p):
                found_path = p
                break

        if found_path:
            sz_kb = os.path.getsize(found_path) / 1024
            return "OK", f"Local disk file exists ({sz_kb:.1f} KB)"
        else:
            return "BROKEN", f"Local file MISSING on disk: uploads/{fname} (Shows Logo)"

    # Case 2: HTTP / HTTPS URL
    if url_str.startswith("http://") or url_str.startswith("https://"):
        try:
            async with session.get(url_str, timeout=aiohttp.ClientTimeout(total=8), allow_redirects=True) as resp:
                if resp.status == 200:
                    ct = resp.headers.get("Content-Type", "image/jpeg")
                    cl = resp.headers.get("Content-Length", "0")
                    sz_str = f"{int(cl)/1024:.1f} KB" if cl.isdigit() and int(cl) > 0 else "OK"
                    return "OK", f"HTTP 200 OK ({ct}, {sz_str})"
                else:
                    return "BROKEN", f"HTTP Error {resp.status} ({resp.reason}) -> Shows Logo"
        except Exception as e:
            return "BROKEN", f"Network fetch failed: {str(e)[:60]} -> Shows Logo"

    # Case 3: Telegram File ID
    if len(url_str) > 15 and not url_str.startswith("/"):
        return "TG_FILE_ID", f"Raw Telegram File ID (Needs Bot Stream / May show Logo if bot has no access)"

    return "UNKNOWN", f"Unrecognized format: {url_str[:40]}"


async def inspect_all_stories():
    print("=" * 90)
    print("🔍 ARYA STORE — FULL DATABASE STORY POSTER IMAGE AUDIT")
    print("=" * 90)
    print(f"📁 Directory: {os.getcwd()}")
    db_name = getattr(Config, "DATABASE_NAME", "Unknown")
    db_url = getattr(Config, "DATABASE_URL", "Unknown")
    host = db_url.split('@')[-1] if '@' in db_url else 'localhost'
    print(f"🗄️ Database: {db_name} ({host})\n")

    async with aiohttp.ClientSession() as session:
        # Fetch ALL stories from MongoDB without limit
        print("⏳ Fetching all stories from MongoDB 'premium_stories' collection...")
        stories = await db.db.premium_stories.find({}).sort("_id", -1).to_list(length=None)

        if not stories:
            print("⚠️ No stories found in 'premium_stories' collection.")
            return

        total_count = len(stories)
        print(f"✅ Found {total_count} total stories in database. Analyzing each poster image...\n")

        valid_list = []
        tg_file_id_list = []
        broken_list = []
        empty_list = []

        for idx, s in enumerate(stories, 1):
            s_id = str(s.get("_id"))
            s_title = s.get("story_name_en") or s.get("story_name") or s.get("title") or "Untitled"
            status_val = s.get("status") or "Available"
            
            # Primary poster resolution
            poster = s.get("poster_url") or s.get("image_url") or s.get("cover") or s.get("poster") or s.get("image") or ""
            
            status, detail = await check_resource(session, poster)
            
            entry = {
                "idx": idx,
                "id": s_id,
                "title": s_title,
                "status_val": status_val,
                "poster": poster,
                "status": status,
                "detail": detail
            }

            if status == "OK":
                valid_list.append(entry)
            elif status == "TG_FILE_ID":
                tg_file_id_list.append(entry)
            elif status == "EMPTY":
                empty_list.append(entry)
            else:
                broken_list.append(entry)

        # ── Print Problematic / Blank / Logo Stories ──
        problem_count = len(tg_file_id_list) + len(broken_list) + len(empty_list)
        
        print("=" * 90)
        print(f"🚨 ISSUES FOUND: {problem_count} STORIES HAVE BLANK / LOGO / UNOPTIMIZED POSTERS (Out of {total_count})")
        print("=" * 90)

        if broken_list:
            print(f"\n🔴 [1] BROKEN / 404 POSTERS ({len(broken_list)} Stories):")
            print("These image links return 404 / network error, causing the Arya Logo fallback.")
            print("-" * 90)
            for item in broken_list:
                print(f"  • Story : {item['title']} (ID: {item['id']})")
                print(f"    URL   : {item['poster']}")
                print(f"    Error : {item['detail']}")
                print("-" * 90)

        if empty_list:
            print(f"\n🔴 [2] EMPTY / NO POSTER SET ({len(empty_list)} Stories):")
            print("These stories have no image set in MongoDB, causing blank/logo display.")
            print("-" * 90)
            for item in empty_list:
                print(f"  • Story : {item['title']} (ID: {item['id']})")
                print(f"    Status: {item['status_val']}")
                print("-" * 90)

        if tg_file_id_list:
            print(f"\n🟡 [3] RAW TELEGRAM FILE_IDs ({len(tg_file_id_list)} Stories):")
            print("These stories have Telegram file_ids instead of web image URLs.")
            print("If the bot cannot fetch the file (e.g. file_id from different bot / expired), it shows the Arya Logo.")
            print("-" * 90)
            for item in tg_file_id_list:
                print(f"  • Story   : {item['title']} (ID: {item['id']})")
                print(f"    File ID : {item['poster']}")
                print("-" * 90)

        print("\n" + "=" * 90)
        print("📊 SUMMARY OF AUDIT:")
        print(f"  🟢 Valid & Loading Working Posters : {len(valid_list)}")
        print(f"  🟡 Raw Telegram File IDs           : {len(tg_file_id_list)}")
        print(f"  🔴 Broken / 404 Links              : {len(broken_list)}")
        print(f"  🔴 Empty / Missing Posters         : {len(empty_list)}")
        print(f"  📁 Total Stories in Database       : {total_count}")
        print("=" * 90)


if __name__ == "__main__":
    asyncio.run(inspect_all_stories())
