"""
Arya Premium Mini App API — Production Grade
Run: BOT_USERNAME=UseAryaBot python3 mini_app_api.py
"""
import os, random, string, hmac, hashlib, logging, base64

# ── CRITICAL: Load .env into os.environ BEFORE importing Config ───
# This must use __file__ (absolute script path), NOT the current working dir.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_SCRIPT_DIR)

def _inject_env(filepath):
    """Read a .env file and inject values into os.environ (only if key not already set)."""
    try:
        with open(filepath, "r") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    _k = _k.strip()
                    _v = _v.strip().strip("'").strip('"')
                    os.environ.setdefault(_k, _v)
    except Exception:
        pass

# Load parent .env first (has DATABASE), then local .env (may override)
_inject_env(os.path.join(_PARENT_DIR, ".env"))
_inject_env(os.path.join(_SCRIPT_DIR, ".env"))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_PARENT_DIR, ".env"), override=False)
    load_dotenv(os.path.join(_SCRIPT_DIR, ".env"), override=False)
except ImportError:
    pass
# ─────────────────────────────────────────────────────────────────

from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from config import Config
from database import db as arya_db

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("arya_api")

from bson.objectid import ObjectId

BOT_USERNAME = os.environ.get("BOT_USERNAME", "UseAryaBot")
RZP_KEY      = Config.RAZORPAY_KEY
RZP_SECRET   = Config.RAZORPAY_SECRET
BANNER_SIZE  = (1184, 556)  # enforced by mgmt bot

app = FastAPI(title="Arya Premium API")

# ── In-memory negative cache ───────────────────────────────────────────────────
# Stores story_ids whose Telegram fetch has permanently failed.
FAILED_IMAGES: set[str] = set()

# ── Image cache — MUST be defined before warmup/proxy functions use it ─────────
import asyncio
import aiofiles
from fastapi.responses import FileResponse

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "image_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# ── Pillow for compression (graceful degradation if not installed) ─────────────
try:
    from PIL import Image as PILImage
    import io as _pil_io
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False
    logger.warning("Pillow not installed — images stored uncompressed. Install: pip install Pillow")

@app.on_event("startup")
async def startup_event():
    try:
        await arya_db.connect()
        logger.info(f"✅ MongoDB connected | bot={BOT_USERNAME}")
    except Exception as startup_db_err:
        logger.error(f"Startup DB connection failed: {startup_db_err}")
    asyncio.create_task(_warmup_image_cache())


async def _warmup_image_cache():
    """
    PARALLEL startup warmup — processes all stories concurrently (5 at a time).
    50 stories: ~3s instead of 15s sequential.
    Non-blocking — server is immediately available while this runs.
    """
    try:
        await asyncio.sleep(15)  # Let server fully serve first users before any background work
        if arya_db.db is None:
            try:
                await arya_db.connect()
            except Exception as db_err:
                logger.error(f"Warmup DB connection failed: {db_err}")
                return

        if arya_db.db is None:
            logger.warning("Warmup skipped: DB not connected")
            return

        try:
            raw    = await arya_db.get_all_stories()
        except Exception as query_err:
            logger.error(f"Warmup story query failed: {query_err}")
            return

        total  = len(raw)
        logger.info(f"🔥 Image warmup starting — {total} stories")

        sem = asyncio.Semaphore(5)  # 5 concurrent Telegram requests

        async def _process_one(s):
            story_id = str(s.get("_id", ""))
            if not story_id:
                return "skip"

            cache_path = os.path.join(CACHE_DIR, f"{story_id}.webp")
            jpg_path   = os.path.join(CACHE_DIR, f"{story_id}.jpg")

            # Valid WebP already exists — skip
            if os.path.exists(cache_path) and os.path.getsize(cache_path) > 500:
                return "cached"

            # Old JPEG exists — convert to WebP (non-blocking async read + thread executor)
            if os.path.exists(jpg_path) and os.path.getsize(jpg_path) > 1000:
                if HAS_PILLOW:
                    try:
                        async with aiofiles.open(jpg_path, "rb") as f:
                            jpg_bytes = await f.read()
                        webp_bytes = await _compress_to_webp_async(jpg_bytes)
                        async with aiofiles.open(cache_path, "wb") as f:
                            await f.write(webp_bytes)
                        os.remove(jpg_path)  # Remove old JPEG
                        return "converted"
                    except Exception:
                        pass  # Fall through to Telegram fetch
                else:
                    return "cached"  # No Pillow — serve JPEG as-is

            if story_id in FAILED_IMAGES:
                return "failed"

            # Direct URL stories don't need Telegram fetch
            raw_poster = s.get("poster_url") or s.get("cover") or s.get("image_url")
            if _is_url(raw_poster):
                return "skip"

            file_id = (
                s.get("image") or s.get("image_id") or
                s.get("poster_id") or s.get("banner_id")
            )
            if not file_id:
                return "skip"

            async with sem:
                ok = await _fetch_and_cache_image(story_id, file_id, cache_path, s.get("bot_id"))
                if ok:
                    return "fetched"
                else:
                    FAILED_IMAGES.add(story_id)
                    return "failed"

        results  = await asyncio.gather(*[_process_one(s) for s in raw])
        counts   = {k: results.count(k) for k in ("cached", "fetched", "converted", "skip", "failed")}
        logger.info(
            f"✅ Warmup done — {counts['cached']} cached | {counts.get('converted',0)} jpg→webp | "
            f"{counts['fetched']} fetched | {counts['skip']} skipped | "
            f"{counts['failed']} failed"
        )
    except Exception as e:
        logger.error(f"Warmup error: {e}", exc_info=True)


async def _compress_to_webp_async(raw_bytes: bytes, max_width: int = 320, quality: int = 82) -> bytes:
    """
    Async wrapper — runs Pillow in a thread executor so it NEVER blocks the event loop.
    Pillow's PILImage.open/resize/save are synchronous CPU ops — running them directly
    in async code would freeze ALL API responses during image processing.
    asyncio.to_thread() moves them to a worker thread safely.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _compress_to_webp_sync, raw_bytes, max_width, quality)


def _compress_to_webp_sync(raw_bytes: bytes, max_width: int = 320, quality: int = 82) -> bytes:
    """
    Synchronous Pillow conversion — ONLY called from thread executor, never directly from async.
    WebP at 320px/quality=82: ~10-18KB per poster (vs 80-150KB raw JPEG).
    """
    if not HAS_PILLOW:
        return raw_bytes
    try:
        img = PILImage.open(_pil_io.BytesIO(raw_bytes))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        if w > max_width:
            new_h = int(h * max_width / w)
            img = img.resize((max_width, new_h), PILImage.LANCZOS)
        buf = _pil_io.BytesIO()
        img.save(buf, format="WEBP", quality=quality, method=4)
        return buf.getvalue()
    except Exception as e:
        logger.debug(f"WebP sync error: {e}")
        return raw_bytes

# Keep _compress_to_webp as alias for backward compat (sync version)
_compress_to_webp = _compress_to_webp_sync


async def _fetch_and_cache_image(story_id: str, file_id: str, cache_path: str, bot_id=None) -> bool:
    """
    Fetch from Telegram, compress with Pillow, save to disk.
    Returns True on success. Thread-safe (uses asyncio file writes).
    """
    import httpx
    try:
        token = await _get_bot_token(bot_id)
        if not token:
            return False

        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"https://api.telegram.org/bot{token}/getFile",
                params={"file_id": file_id}
            )
            data = r.json()
            if not data.get("ok"):
                logger.debug(f"getFile failed [{story_id}]: {data.get('description')}")
                return False

            file_path = data["result"]["file_path"]
            img_r = await client.get(
                f"https://api.telegram.org/file/bot{token}/{file_path}"
            )
            if img_r.status_code != 200:
                return False

            # Convert to WebP in thread executor — never blocks event loop
            raw_bytes = img_r.content
            webp_bytes = await _compress_to_webp_async(raw_bytes, max_width=320, quality=82)

            async with aiofiles.open(cache_path, "wb") as f:
                await f.write(webp_bytes)

            logger.info(f"✅ Cached {story_id}: {len(raw_bytes)//1024}KB → {len(webp_bytes)//1024}KB webp")
            return True

    except Exception as e:
        logger.debug(f"Fetch error [{story_id}]: {e}")
        return False



app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ═══════════════════════════════════════════════════════════════════
# UTILITIES
# ═══════════════════════════════════════════════════════════════════

def _rand(n=6): return "".join(random.choices(string.ascii_uppercase + string.digits, k=n))

def _is_url(v): return bool(v and (str(v).startswith("http://") or str(v).startswith("https://")))

def _make_order_id(uid): return f"OD-{uid}-{_rand(6)}"

def _normalize_date(val) -> str | None:
    """Convert any date value (datetime, timestamp, string) to ISO string."""
    if val is None:
        return None
    from datetime import datetime
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, (int, float)):
        # Unix timestamp
        try:
            return datetime.utcfromtimestamp(val).isoformat()
        except Exception:
            return None
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def _format_story(s: dict) -> dict | None:

    story_id = str(s["_id"]) if s.get("_id") else None
    if not story_id: return None

    title = (
        s.get("story_name_en") or s.get("story_name_hi") or
        s.get("story_name") or s.get("name") or s.get("title")
    )
    if not title or not str(title).strip(): return None
    title = str(title).strip()

    description = (s.get("description") or s.get("description_hi") or "").strip()

    # Poster URL: prefer HTTP URL; else proxy via /api/image/{id}
    raw_poster = s.get("poster_url") or s.get("cover") or s.get("image_url")
    if _is_url(raw_poster):
        poster = raw_poster
    elif s.get("image"):
        poster = f"/api/image/{story_id}"
    else:
        poster = None

    # isCompleted: read from 'status' field, 'completed' field, or episode comparison
    story_status = str(s.get("status") or s.get("completed") or "").strip().lower()
    if story_status in ("completed", "complete", "done", "finished", "true", "1"):
        is_completed = True
    elif story_status in ("ongoing", "false", "0"):
        is_completed = False
    else:
        # Fallback logic: total_episodes == released_episodes
        eps = str(s.get("episodes") or s.get("ep_count") or "").strip()
        t_eps = str(s.get("total_episodes") or s.get("total_eps") or "").strip()
        if eps and t_eps and eps.isdigit() and t_eps.isdigit():
            is_completed = (int(eps) >= int(t_eps))
        else:
            is_completed = None

    # fileCount: computed from channel message range if available, or direct fields
    start_id = s.get("start_id")
    end_id   = s.get("end_id")
    if start_id and end_id:
        try:
            file_count = abs(int(end_id) - int(start_id)) + 1
        except Exception:
            file_count = None
    else:
        file_count = s.get("files_count") or s.get("file_count") or s.get("episodes_count") or s.get("total_files") or None

    return {
        "id":            story_id,
        "title":         title,
        "description":   description,
        "poster":        poster,
        "banner":        poster,
        "cover":         poster,
        "price":         float(s.get("price") or 0),
        "language":      s.get("language") or "Hindi",
        "platform":      s.get("platform") or "Pocket FM",
        "genre":         s.get("genre") or "Drama",
        "status":        "available",
        "storyStatus":   str(s.get("status") or ("Completed" if is_completed is True else ("Ongoing" if is_completed is False else ""))).strip(),
        "episodes":      s.get("episodes") or s.get("ep_count") or s.get("total_eps") or "?",
        "totalEpisodes": s.get("total_eps") or s.get("total_episodes") or "?",
        "size":          s.get("size") or s.get("total_size") or None,
        "fileCount":     file_count,
        "isCompleted":   is_completed,
        "bot_id":        s.get("bot_id"),
        # ── Real engagement & date fields (critical for trending + new releases) ──
        "purchase_count":  int(s.get("purchase_count") or s.get("purchases") or s.get("buy_count") or 0),
        "view_count":      int(s.get("view_count") or s.get("views") or s.get("opens") or 0),
        "search_count":    int(s.get("search_count") or s.get("searches") or 0),
        "trending_score":  float(s.get("trending_score") or 0),
        # created_at: try multiple field names, normalize to ISO string
        "created_at":      _normalize_date(
            s.get("created_at") or s.get("uploaded_at") or s.get("added_at") or
            s.get("date") or s.get("upload_date") or s.get("created")
        ),
        "uploaded_at":     _normalize_date(
            s.get("uploaded_at") or s.get("created_at") or s.get("added_at") or
            s.get("date") or s.get("upload_date")
        ),
        "is_must_have":    bool(s.get("is_must_have", False)),
    }



async def _get_bot_token(bot_id) -> str | None:
    """Get the Telegram bot token for image proxy."""
    if bot_id:
        try:
            bot = await arya_db.db.premium_bots.find_one({"id": int(bot_id)})
            if bot and bot.get("token"):
                return bot["token"]
        except Exception:
            pass
    # Fallback to mgmt bot token
    return Config.MGMT_BOT_TOKEN or os.environ.get("MGMT_BOT_TOKEN")


async def _get_stories_from_ids(story_ids: list) -> list:
    """Fetch story documents from DB given a list of string ObjectIds."""
    from bson.objectid import ObjectId
    result = []
    for sid in story_ids:
        try:
            doc = await arya_db.db.premium_stories.find_one({"_id": ObjectId(str(sid))})
            if doc:
                result.append(doc)
        except Exception:
            pass
    return result


# ═══════════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/health")
async def health():
    return {
        "status": "ok", 
        "bot": BOT_USERNAME, 
        "razorpay": bool(RZP_KEY),
        "db_uri": bool(Config.MONGO_URI),
        "db_connected": bool(arya_db.stories is not None)
    }


# ── Banners ───────────────────────────────────────────────────────
@app.get("/api/banners")
async def get_banners():
    """
    Returns up to 10 hero banners:
     - 1 auto: most-purchased story (trending)
     - 1 auto: newest story added
     - up to 8 manual: from mini_app_banners collection
    """
    try:
        result = []

        # Auto: Trending (most purchased story)
        try:
            top_order = await arya_db.db.orders.find_one(
                {"status": {"$in": ["paid", "delivered"]}},
                sort=[("created_at", -1)]
            )
            if top_order:
                # find most common story across all paid orders
                pipeline = [
                    {"$match": {"status": {"$in": ["paid", "delivered"]}}},
                    {"$unwind": "$story_ids"},
                    {"$group": {"_id": "$story_ids", "count": {"$sum": 1}}},
                    {"$sort": {"count": -1}},
                    {"$limit": 1}
                ]
                agg = await arya_db.db.orders.aggregate(pipeline).to_list(1)
                if agg:
                    trend_story = await arya_db.db.premium_stories.find_one(
                        {"_id": ObjectId(str(agg[0]["_id"]))}
                    )
                    if trend_story:
                        fmt = _format_story(trend_story)
                        if fmt:
                            result.append({
                                "id": f"trending_{fmt['id']}",
                                "type": "trending",
                                "story_id": fmt["id"],
                                "image": fmt["poster"] or fmt["banner"],
                                "title": fmt["title"],
                                "subtitle": "🔥 Trending Now",
                                "badge": "TRENDING",
                            })
        except Exception as e:
            logger.warning(f"Trending banner error: {e}")

        # Auto: Newest story
        try:
            newest = await arya_db.db.premium_stories.find_one(
                {}, sort=[("_id", -1)]
            )
            if newest:
                fmt = _format_story(newest)
                if fmt:
                    result.append({
                        "id": f"new_{fmt['id']}",
                        "type": "new",
                        "story_id": fmt["id"],
                        "image": fmt["poster"] or fmt["banner"],
                        "title": fmt["title"],
                        "subtitle": "✨ New Release",
                        "badge": "NEW",
                    })
        except Exception as e:
            logger.warning(f"Newest banner error: {e}")

        # Manual banners from DB (up to 8)
        try:
            cursor = arya_db.db.mini_app_banners.find({}).sort("order", 1).limit(8)
            manual = await cursor.to_list(length=8)
            for b in manual:
                bid = str(b["_id"])
                result.append({
                    "id": bid,
                    "type": "manual",
                    "story_id": b.get("story_id"),
                    "image": f"/api/image/banner/{bid}",
                    "title": b.get("title", ""),
                    "subtitle": b.get("subtitle", ""),
                    "badge": b.get("badge", ""),
                })
        except Exception as e:
            logger.warning(f"Manual banners error: {e}")

        return {"success": True, "data": result[:10]}
    except Exception as e:
        logger.error(f"/api/banners error: {e}", exc_info=True)
        raise HTTPException(500, str(e))


# ── Banner Image Proxy ────────────────────────────────────────────
@app.get("/api/image/banner/{banner_id}")
async def banner_image_proxy(banner_id: str):
    """Proxy banner image from Telegram file_id."""
    import httpx
    try:
        banner = await arya_db.db.mini_app_banners.find_one({"_id": ObjectId(banner_id)})
    except Exception:
        raise HTTPException(404, "Invalid banner id")
    if not banner:
        raise HTTPException(404, "Banner not found")

    file_id = banner.get("file_id")
    if not file_id:
        raise HTTPException(404, "No image")
    if _is_url(file_id):
        from fastapi.responses import RedirectResponse
        return RedirectResponse(file_id)

    token = await _get_bot_token(None)  # use mgmt bot token
    if not token:
        raise HTTPException(500, "No bot token")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"https://api.telegram.org/bot{token}/getFile",
                params={"file_id": file_id}
            )
            data = r.json()
            if not data.get("ok"):
                raise HTTPException(404, data.get("description", "getFile failed"))
            file_path = data["result"]["file_path"]
            img = await client.get(
                f"https://api.telegram.org/file/bot{token}/{file_path}"
            )
            return Response(
                content=img.content,
                media_type=img.headers.get("content-type", "image/jpeg"),
                headers={"Cache-Control": "public, max-age=86400"}
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Banner image fetch failed: {e}")


# ── Stories ───────────────────────────────────────────────────────
@app.get("/api/stories")
async def get_stories():
    try:
        if arya_db.db is None:
            await arya_db.connect()
        raw = await arya_db.get_all_stories()
        stories = [r for r in (_format_story(s) for s in raw) if r]
        logger.info(f"Serving {len(stories)} stories")
        return {"success": True, "data": stories}
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"stories error: {e}\n{tb}")
        raise HTTPException(500, f"{str(e)} | TRACEBACK: {tb}")


# ── Engagement Tracking ───────────────────────────────────────────
@app.post("/api/track")
async def track_event(payload: dict):
    """
    Track user engagement events for live trending.
    Events: open | search | view | purchase
    Writes to story_events collection (lightweight, no heavy indexes).
    Simultaneously updates per-story counters on premium_stories.
    Does NOT break any existing delivery/payment logic.
    """
    story_id  = payload.get("story_id", "")
    event     = payload.get("event", "view")   # open | search | view | purchase
    tg_id     = payload.get("telegram_id", 0)

    if not story_id or event not in ("open", "search", "view", "purchase"):
        return {"ok": False}

    try:
        if arya_db.db is None:
            await arya_db.connect()

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)

        # Write lightweight event record
        await arya_db.db.story_events.insert_one({
            "story_id": story_id,
            "event":    event,
            "tg_id":    tg_id,
            "ts":       now,
        })

        # Increment counter on the story itself for persistence
        count_field = {
            "open":     "view_count",
            "search":   "search_count",
            "view":     "view_count",
            "purchase": "purchase_count",
        }.get(event, "view_count")

        try:
            await arya_db.db.premium_stories.update_one(
                {"_id": ObjectId(story_id)},
                {"$inc": {count_field: 1}}
            )
        except Exception:
            pass  # Don't fail track if story_id is invalid

        return {"ok": True}
    except Exception as e:
        logger.debug(f"Track error: {e}")
        return {"ok": False}


# ── Real Trending ──────────────────────────────────────────────────
@app.get("/api/trending")
async def get_trending(limit: int = 10):
    """
    Computes REAL trending from story_events over the last 7 days.
    Scoring: purchase=10pts, open=3pts, search=2pts, view=1pt
    Falls back to purchase_count on stories if events collection is empty.
    """
    try:
        if arya_db.db is None:
            await arya_db.connect()

        from datetime import datetime, timezone, timedelta
        since = datetime.now(timezone.utc) - timedelta(days=7)

        # Aggregate recent events by story_id with weighted score
        pipeline = [
            {"$match": {"ts": {"$gte": since}}},
            {"$group": {
                "_id": "$story_id",
                "score": {"$sum": {
                    "$switch": {
                        "branches": [
                            {"case": {"$eq": ["$event", "purchase"]}, "then": 10},
                            {"case": {"$eq": ["$event", "open"]},     "then": 3},
                            {"case": {"$eq": ["$event", "search"]},   "then": 2},
                            {"case": {"$eq": ["$event", "view"]},     "then": 1},
                        ],
                        "default": 1
                    }
                }}
            }},
            {"$sort": {"score": -1}},
            {"$limit": limit},
        ]

        cursor = arya_db.db.story_events.aggregate(pipeline)
        results = await cursor.to_list(length=limit)

        if not results:
            # Fallback: sort stories by purchase_count from DB
            raw = await arya_db.get_all_stories()
            raw_sorted = sorted(
                raw,
                key=lambda s: int(s.get("purchase_count") or s.get("purchases") or 0),
                reverse=True
            )[:limit]
            stories = [r for r in (_format_story(s) for s in raw_sorted) if r]
            return {"success": True, "data": stories, "source": "fallback"}

        # Fetch actual story documents for top trending IDs
        top_ids = [r["_id"] for r in results]
        score_map = {r["_id"]: r["score"] for r in results}

        stories_out = []
        for sid in top_ids:
            try:
                raw = await arya_db.db.premium_stories.find_one({"_id": ObjectId(sid)})
                if raw:
                    formatted = _format_story(raw)
                    if formatted:
                        formatted["trending_score"] = score_map.get(sid, 0)
                        stories_out.append(formatted)
            except Exception:
                continue

        return {"success": True, "data": stories_out, "source": "live"}
    except Exception as e:
        logger.error(f"Trending error: {e}")
        raise HTTPException(500, str(e))


# ── Auto-process story poster on add/update ───────────────────────
@app.post("/api/process-story")
async def process_story_image(payload: dict):
    """
    Called by the Telegram bot when a new story is added or its poster changes.
    Pre-fetches and caches the image so the FIRST user request is instant.
    Runs asynchronously — returns immediately and processes in background.
    """
    story_id = payload.get("story_id", "")
    if not story_id:
        return {"ok": False, "error": "story_id required"}

    async def _bg_process():
        try:
            if arya_db.db is None:
                await arya_db.connect()
            story = await arya_db.db.premium_stories.find_one({"_id": ObjectId(story_id)})
            if not story:
                return
            cache_path = os.path.join(CACHE_DIR, f"{story_id}.jpg")
            # Remove old cache if poster changed
            if os.path.exists(cache_path):
                os.remove(cache_path)
            FAILED_IMAGES.discard(story_id)  # Clear negative cache

            file_id = (
                story.get("image") or story.get("image_id") or
                story.get("poster_id") or story.get("banner_id")
            )
            if not file_id:
                return
            ok = await _fetch_and_cache_image(story_id, file_id, cache_path, story.get("bot_id"))
            logger.info(f"Auto-process story {story_id}: {'✅' if ok else '❌'}")
        except Exception as e:
            logger.error(f"Auto-process error [{story_id}]: {e}")

    asyncio.create_task(_bg_process())
    return {"ok": True, "queued": story_id}


@app.get("/api/image/{story_id}")
async def get_image(story_id: str):
    """
    Serve story poster with 3-tier strategy:
    1. Disk cache  → instant (0ms, immutable headers)
    2. Negative cache → instant SVG (known-bad, skip Telegram)
    3. Telegram fetch → save to disk, return image
    Never returns 404 or broken image.
    """
    webp_path = os.path.join(CACHE_DIR, f"{story_id}.webp")
    jpg_path  = os.path.join(CACHE_DIR, f"{story_id}.jpg")  # Legacy fallback

    # ── Tier 1a: WebP cache (optimal — 40-60% smaller) ────────────────
    if os.path.exists(webp_path) and os.path.getsize(webp_path) > 500:
        return FileResponse(
            path=webp_path,
            media_type="image/webp",
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "ETag": f'"{story_id}-webp"',
                "Vary": "Accept",
            }
        )

    # ── Tier 1b: Legacy JPEG cache (served until warmup converts it) ───
    if os.path.exists(jpg_path) and os.path.getsize(jpg_path) > 1000:
        return FileResponse(
            path=jpg_path,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "public, max-age=86400",  # Shorter TTL — will be replaced by WebP
                "ETag": f'"{story_id}-jpg"',
            }
        )

    # ── Tier 2: Negative cache (skip known-bad Telegram file_ids) ─────
    if story_id in FAILED_IMAGES:
        logger.debug(f"Negative cache hit: {story_id}")
        return _svg_placeholder(story_id)

    # ── Tier 3: Telegram fetch ─────────────────────────────────────────
    try:
        if arya_db.stories is None:
            await arya_db.connect()

        story = await arya_db.db.premium_stories.find_one({"_id": ObjectId(story_id)})
        if not story:
            FAILED_IMAGES.add(story_id)
            return _svg_placeholder(story_id)

        # Check for direct URL first (no Telegram call needed)
        raw_url = story.get("poster_url") or story.get("cover") or story.get("image_url")
        if _is_url(raw_url):
            from fastapi.responses import RedirectResponse
            return RedirectResponse(raw_url, status_code=302,
                                    headers={"Cache-Control": "public, max-age=86400"})

        file_id = (
            story.get("image") or story.get("image_id") or
            story.get("poster_id") or story.get("banner_id")
        )
        if not file_id:
            FAILED_IMAGES.add(story_id)
            return _svg_placeholder(story_id)

        if _is_url(file_id):
            from fastapi.responses import RedirectResponse
            return RedirectResponse(file_id, status_code=302,
                                    headers={"Cache-Control": "public, max-age=86400"})

        # Fetch from Telegram → save as WebP
        success = await _fetch_and_cache_image(story_id, file_id, webp_path, story.get("bot_id"))
        if not success:
            FAILED_IMAGES.add(story_id)
            logger.warning(f"⚠ Poster failed for {story_id}")
            return _svg_placeholder(story_id)

        # Serve freshly cached WebP
        return FileResponse(
            path=webp_path,
            media_type="image/webp",
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "ETag": f'"{story_id}-webp"',
            }
        )


    except Exception as e:
        logger.warning(f"Image proxy error [{story_id}]: {e}")
        return _svg_placeholder(story_id)



def _svg_placeholder(story_id: str) -> Response:
    """Return a branded SVG placeholder instead of 404."""
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300" viewBox="0 0 300 300">
  <rect width="300" height="300" fill="#1a1a2e"/>
  <rect x="100" y="100" width="100" height="100" rx="20" fill="#c9a22733"/>
  <text x="150" y="145" text-anchor="middle" fill="#c9a227" font-size="28" font-family="sans-serif">A</text>
  <text x="150" y="175" text-anchor="middle" fill="#666" font-size="10" font-family="sans-serif">AryaPremium</text>
</svg>'''
    return Response(
        content=svg.encode(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/api/image-status")
async def image_status():
    """
    Diagnostic: returns image cache health stats.
    Shows how many images are cached, failed, and lists failed poster IDs.
    Useful for debugging poster loading issues for specific stories.
    """
    cached_files  = os.listdir(CACHE_DIR) if os.path.exists(CACHE_DIR) else []
    cached_count  = len([f for f in cached_files if f.endswith(".jpg")])
    return {
        "cached_on_disk": cached_count,
        "failed_count":   len(FAILED_IMAGES),
        "failed_ids":     list(FAILED_IMAGES)[:50],  # first 50 only
        "cache_dir":      CACHE_DIR,
    }

@app.post("/api/image-retry")
async def image_retry(payload: dict):
    """
    Admin: clear a specific story_id from the negative cache so it will be retried.
    POST body: { "story_id": "..." }
    """
    story_id = payload.get("story_id", "")
    if story_id in FAILED_IMAGES:
        FAILED_IMAGES.discard(story_id)
        # Also delete disk cache file if corrupt
        cache_path = os.path.join(CACHE_DIR, f"{story_id}.jpg")
        if os.path.exists(cache_path):
            os.remove(cache_path)
        return {"ok": True, "message": f"Cleared retry block for {story_id}"}
    return {"ok": True, "message": "story_id was not in failed list"}


@app.post("/api/checkout")
async def checkout(payload: dict):
    """Create pending order → return Telegram bot deep-link for UPI payment."""
    story_ids  = payload.get("story_ids", [])
    tg_id      = payload.get("telegram_id") or 0
    username   = payload.get("username", "")

    if not story_ids:
        raise HTTPException(400, "Cart is empty")

    stories = await _get_stories_from_ids(story_ids)
    if not stories:
        raise HTTPException(400, "No valid stories")

    total  = sum(float(s.get("price") or 0) for s in stories)
    oid    = _make_order_id(tg_id)

    await arya_db.db.orders.insert_one({
        "order_id":    oid,
        "user_id":     tg_id,
        "username":    username,
        "story_ids":   story_ids,
        "story_names": [s.get("story_name_en", "") for s in stories],
        "total":       total,
        "status":      "pending",
        "source":      "bot_upi",
        "created_at":  datetime.now(timezone.utc),
    })
    logger.info(f"Order {oid} created (UPI/bot) for {tg_id}, ₹{total}")

    bot = os.environ.get("BOT_USERNAME", BOT_USERNAME)
    return {
        "success":      True,
        "order_id":     oid,
        "total":        total,
        "checkout_url": f"https://t.me/{bot}?start=order_{oid}",
    }


# ── Razorpay: Create Order ────────────────────────────────────────
@app.post("/api/create-order")
async def create_razorpay_order(payload: dict):
    """Create Razorpay order. Returns order_id + key for frontend SDK modal."""
    story_ids = payload.get("story_ids", [])
    tg_id     = payload.get("telegram_id") or 0

    if not story_ids:
        raise HTTPException(400, "Cart is empty")
    if not RZP_KEY or not RZP_SECRET:
        raise HTTPException(500, "Razorpay not configured on server")

    stories = await _get_stories_from_ids(story_ids)
    if not stories:
        raise HTTPException(400, "No valid stories")

    total_paise = int(sum(float(s.get("price") or 0) for s in stories) * 100)
    receipt     = _make_order_id(tg_id)

    import httpx
    auth_header = "Basic " + base64.b64encode(f"{RZP_KEY}:{RZP_SECRET}".encode()).decode()

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.razorpay.com/v1/orders",
                json={
                    "amount":   total_paise,
                    "currency": "INR",
                    "receipt":  receipt,
                    "notes":    {"telegram_id": str(tg_id), "story_ids": ",".join(story_ids)},
                },
                headers={"Authorization": auth_header, "Content-Type": "application/json"},
            )
            if r.status_code != 200:
                logger.error(f"Razorpay create-order failed: {r.text}")
                raise HTTPException(502, "Razorpay order creation failed")
            rzp = r.json()

        return {
            "success":           True,
            "razorpay_order_id": rzp["id"],
            "amount":            total_paise,
            "currency":          "INR",
            "key":               RZP_KEY,
            "receipt":           receipt,
            "story_names":       [s.get("story_name_en", "") for s in stories],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"create-order error: {e}", exc_info=True)
        raise HTTPException(500, str(e))


# ── Razorpay: Verify Payment (HMAC) ──────────────────────────────
@app.post("/api/verify-payment")
async def verify_payment(payload: dict):
    """
    Verify Razorpay HMAC signature after successful payment.
    Called automatically by the frontend handler — no user action needed.
    """
    rzp_order_id   = payload.get("razorpay_order_id", "")
    rzp_payment_id = payload.get("razorpay_payment_id", "")
    rzp_signature  = payload.get("razorpay_signature", "")
    story_ids      = payload.get("story_ids", [])
    tg_id          = payload.get("telegram_id") or 0
    username       = payload.get("username", "")

    if not all([rzp_order_id, rzp_payment_id, rzp_signature]):
        raise HTTPException(400, "Missing payment verification fields")

    # HMAC-SHA256 verification
    expected = hmac.new(
        RZP_SECRET.encode("utf-8"),
        f"{rzp_order_id}|{rzp_payment_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, rzp_signature):
        logger.warning(f"Invalid Razorpay signature for {rzp_payment_id}")
        raise HTTPException(400, "Payment verification failed — invalid signature")

    # Signature OK — store order + unlock content
    stories = await _get_stories_from_ids(story_ids)
    total   = sum(float(s.get("price") or 0) for s in stories)
    oid     = _make_order_id(tg_id)

    tg_id_int = int(tg_id) if tg_id else 0

    await arya_db.db.orders.insert_one({
        "order_id":            oid,
        "user_id":             tg_id_int,  # Always stored as int for consistent querying
        "username":            username,
        "story_ids":           story_ids,
        "story_names":         [s.get("story_name_en", "") for s in stories],
        "total":               total,
        "status":              "paid",
        "source":              "razorpay_miniapp",
        "razorpay_order_id":   rzp_order_id,
        "razorpay_payment_id": rzp_payment_id,
        "created_at":          datetime.now(timezone.utc),
    })

    if tg_id_int:
        for sid in story_ids:
            await arya_db.add_purchase(tg_id_int, sid)  # Adds to user's purchases[] field — syncs bot + miniapp

    logger.info(f"Payment verified: {oid} | {rzp_payment_id} | user={tg_id_int} | ₹{total}")

    bot = os.environ.get("BOT_USERNAME", BOT_USERNAME)
    return {
        "success":      True,
        "order_id":     oid,
        "total":        total,
        "checkout_url": f"https://t.me/{bot}?start=buy_{story_ids[0]}" if len(story_ids) == 1 else f"https://t.me/{bot}",
    }


# ── My Purchases — Bot + Mini App synced ──────────────────────────
@app.get("/api/my-purchases")
async def my_purchases(telegram_id: int):
    """
    Return all purchased stories for a user.
    Merges:
      - Bot purchases (stored via arya_db.add_purchase)
      - Mini App Razorpay purchases (stored in orders collection)
    """
    if not telegram_id:
        raise HTTPException(400, "telegram_id required")

    try:
        # 1. Bot purchases — field is "purchases", key is "id" (not user_id)
        user = await arya_db.db.users.find_one({"id": int(telegram_id)})
        user_story_ids: list = []
        if user:
            # DB stores as "purchases" field (see database.py add_purchase)
            user_story_ids = user.get("purchases", []) or []

        # 2. Mini App Razorpay orders
        order_story_ids: list = []
        order_map: dict = {}  # story_id → order details for the success screen
        async for order in arya_db.db.orders.find(
            {"user_id": int(telegram_id), "status": "paid"},
        ):
            for sid in order.get("story_ids", []):
                order_story_ids.append(sid)
                order_map[str(sid)] = {
                    "order_id":   order.get("order_id", ""),
                    "payment_id": order.get("razorpay_payment_id", ""),
                    "amount":     order.get("total", 0),
                    "source":     order.get("source", "razorpay_miniapp"),
                    "paid_at":    order.get("created_at", "").isoformat() if hasattr(order.get("created_at", ""), "isoformat") else str(order.get("created_at", "")),
                }

        # 3. Merge unique IDs (bot + miniapp)
        all_ids = list(dict.fromkeys(user_story_ids + order_story_ids))
        if not all_ids:
            return {"success": True, "data": []}

        # 4. Fetch story details for each ID
        results = []
        for sid in all_ids:
            try:
                story = await arya_db.db.premium_stories.find_one({"_id": ObjectId(str(sid))})
                if not story:
                    continue
                formatted = _format_story(story)
                if not formatted:
                    continue
                # Attach order details if purchased via mini app
                od = order_map.get(str(sid))
                results.append({
                    "story_id":    formatted["id"],
                    "title":       formatted["title"],
                    "poster":      formatted.get("poster"),
                    "price":       formatted.get("price"),
                    "platform":    formatted.get("platform"),
                    "genre":       formatted.get("genre"),
                    "isCompleted": formatted.get("isCompleted", False),
                    "episodes":    formatted.get("episodes"),
                    "source":      od.get("source", "bot") if od else "bot",
                    "order_details": od,  # None for bot purchases, dict for miniapp
                })
            except Exception:
                continue

        logger.info(f"my-purchases: user={telegram_id} → {len(results)} stories ({len(user_story_ids)} bot, {len(set(order_story_ids))} miniapp)")
        return {"success": True, "data": results}

    except Exception as e:
        logger.error(f"my-purchases error: {e}", exc_info=True)

        raise HTTPException(500, "Failed to load purchases")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("mini_app_api:app", host="0.0.0.0", port=8000, reload=True)
