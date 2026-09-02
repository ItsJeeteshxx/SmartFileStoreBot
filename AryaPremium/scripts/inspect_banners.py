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
        return "EMPTY", "No image URL or file path provided"

    url_str = str(url).strip()

    # Case 1: Local file in /api/uploads/ or downloads/
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
            return "OK", f"Local disk file exists ({sz_kb:.1f} KB) at {os.path.basename(found_path)}"
        else:
            return "BROKEN", f"Local file MISSING on disk: uploads/{fname}"

    # Case 2: HTTP / HTTPS URL
    if url_str.startswith("http://") or url_str.startswith("https://"):
        try:
            async with session.get(url_str, timeout=aiohttp.ClientTimeout(total=8), allow_redirects=True) as resp:
                if resp.status == 200:
                    ct = resp.headers.get("Content-Type", "image/jpeg")
                    cl = resp.headers.get("Content-Length", "0")
                    sz_str = f"{int(cl)/1024:.1f} KB" if cl.isdigit() and int(cl) > 0 else "Unknown size"
                    return "OK", f"HTTP 200 OK ({ct}, {sz_str})"
                else:
                    return "BROKEN", f"HTTP Error {resp.status} ({resp.reason})"
        except Exception as e:
            return "BROKEN", f"Network fetch failed: {str(e)[:60]}"

    # Case 3: Telegram File ID
    if len(url_str) > 15 and not url_str.startswith("/"):
        return "TG_FILE_ID", f"Raw Telegram File ID ({url_str[:20]}...)"

    return "UNKNOWN", f"Unrecognized format: {url_str[:40]}"


async def inspect_all_banners():
    print("=" * 80)
    print("🔍 ARYA STORE — BANNERS & STORY COVERS DIAGNOSTIC TOOL")
    print("=" * 80)
    print(f"📁 Working Directory: {os.getcwd()}")
    db_name = getattr(Config, "DATABASE_NAME", "Unknown")
    db_url = getattr(Config, "DATABASE_URL", "Unknown")
    host = db_url.split('@')[-1] if '@' in db_url else 'localhost'
    print(f"🗄️ Database: {db_name} ({host})\n")

    async with aiohttp.ClientSession() as session:
        # 1. Inspect mini_app_banners collection
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print("📌 1. INSPECTING 'mini_app_banners' (Hero Banners Carousel)")
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        banners = await db.db.mini_app_banners.find({}).sort("order", 1).to_list(length=50)

        if not banners:
            print("⚠️ No manual banners found in 'mini_app_banners' collection.")
        else:
            print(f"Found {len(banners)} banner document(s) in collection:\n")
            for idx, b in enumerate(banners, 1):
                b_id = str(b.get("_id"))
                title = b.get("title") or b.get("story_name") or "Untitled Banner"
                badge = b.get("badge") or "NONE"
                target_link = b.get("target_link") or b.get("story_id") or "N/A"
                image_url = b.get("image_url") or b.get("image") or ""

                status, detail = await check_resource(session, image_url)
                status_icon = "🟢" if status == "OK" else ("🟡" if status == "TG_FILE_ID" else "🔴")

                print(f"[{idx}] {status_icon} Banner ID: {b_id}")
                print(f"    Title       : {title} (Badge: {badge})")
                print(f"    Target Story: {target_link}")
                print(f"    Image URL   : {image_url if image_url else '❌ NOT SET'}")
                print(f"    Status      : {status} -> {detail}")
                print("-" * 80)

        # 2. Inspect premium_stories collection (First 20 Stories)
        print("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print("📚 2. INSPECTING 'premium_stories' (Story Posters & Banners)")
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        stories = await db.db.premium_stories.find({}).sort("_id", -1).limit(20).to_list(length=20)

        if not stories:
            print("⚠️ No stories found in 'premium_stories' collection.")
        else:
            print(f"Inspecting {len(stories)} newest story records:\n")
            for idx, s in enumerate(stories, 1):
                s_id = str(s.get("_id"))
                s_title = s.get("story_name_en") or s.get("story_name") or s.get("title") or "Untitled"
                status_val = s.get("status") or "Available"
                poster_url = s.get("poster_url") or s.get("cover") or s.get("poster") or ""
                banner_url = s.get("banner_url") or s.get("banner") or ""
                tg_image = s.get("image") or ""

                p_status, p_detail = await check_resource(session, poster_url or tg_image)
                b_status, b_detail = await check_resource(session, banner_url)

                p_icon = "🟢" if p_status == "OK" else ("🟡" if p_status == "TG_FILE_ID" else "🔴")
                b_icon = "🟢" if b_status == "OK" else ("🟡" if b_status == "TG_FILE_ID" else "🔴")

                print(f"[{idx}] Story: {s_title} (ID: {s_id}, Status: {status_val})")
                print(f"    {p_icon} Poster URL: {poster_url if poster_url else (tg_image if tg_image else '❌ NOT SET')}")
                print(f"       -> {p_status}: {p_detail}")
                if banner_url:
                    print(f"    {b_icon} Banner URL: {banner_url}")
                    print(f"       -> {b_status}: {b_detail}")
                print("-" * 80)

    print("\n✅ Diagnostic inspection complete!")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(inspect_all_banners())
