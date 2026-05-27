import os
import uuid
import logging
import asyncio
import urllib.parse
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, APIRouter, HTTPException, Form, File, UploadFile, Request, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from contextvars import ContextVar

admin_authenticated_session: ContextVar[bool] = ContextVar("admin_authenticated_session", default=False)

def is_admin(telegram_id: str = "") -> bool:
    if admin_authenticated_session.get():
        return True
    if not telegram_id:
        return False
    from AryaPremium.config import Config
    try:
        uid = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if uid in Config.OWNER_IDS:
            return True
    except Exception:
        pass
    return False


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Use AryaPremium's own database module (already tested, working)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
import sys
import importlib

# Add AryaPremium to path so we can import its database
_arya_path = os.path.join(os.path.dirname(__file__), "AryaPremium")
if _arya_path not in sys.path:
    sys.path.insert(0, _arya_path)

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Lifespan: connect/disconnect
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        from AryaPremium.database import db as arya_db
        await arya_db.connect()
        app.state.db = arya_db
        logger.info("âœ… Connected to MongoDB via AryaPremium DB module")
        
        # Background index creation for performance optimization
        try:
            analytics_coll = arya_db.db.mini_app_analytics
            await analytics_coll.create_index([("type", 1), ("timestamp", -1)], background=True)
            await analytics_coll.create_index("timestamp", background=True)
            await analytics_coll.create_index("user_id", background=True)
            await analytics_coll.create_index("session_id", background=True)
            
            feedback_coll = arya_db.db.premium_feedback
            await feedback_coll.create_index([("created_at", -1)], background=True)
            
            orders_coll = arya_db.db.orders
            await orders_coll.create_index([("created_at", -1)], background=True)
            await orders_coll.create_index("user_id", background=True)
            
            checkout_coll = arya_db.db.premium_checkout
            await checkout_coll.create_index([("created_at", -1)], background=True)
            await checkout_coll.create_index("user_id", background=True)
            await checkout_coll.create_index("status", background=True)
            
            logger.info("✅ Database indexes verified/created in background")
        except Exception as idx_err:
            logger.warning(f"Failed to create indexes: {idx_err}")
            
    except Exception as e:
        logger.error(f"DB connect failed: {e}")
        raise
    yield
    logger.info("Disconnected from MongoDB")

app = FastAPI(title="Arya Premium Mini App API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api_router = APIRouter()

import aiohttp
import io
import hashlib
from fastapi.responses import Response
from PIL import Image

# In-memory LRU cache for image bytes (simple dict to prevent memory leaks if it gets too large)
IMAGE_CACHE = {}
MAX_CACHE_ITEMS = 500

@api_router.get("/image")
async def optimize_image(url: str):
    """
    Acts as an Image Proxy: Fetches external image (like Catbox), converts to WebP,
    compresses to maintain visual quality without large file size, and caches it.
    """
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Invalid URL")

    # Check cache
    url_hash = hashlib.md5(url.encode()).hexdigest()
    if url_hash in IMAGE_CACHE:
        return Response(content=IMAGE_CACHE[url_hash], media_type="image/webp", headers={"Cache-Control": "public, max-age=31536000, immutable"})

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status != 200:
                    raise HTTPException(status_code=404, detail="Image not found")
                img_bytes = await resp.read()

        # Optimize using Pillow
        img = Image.open(io.BytesIO(img_bytes))
        
        # Convert to RGB if needed (WebP supports RGBA, but we drop alpha for poster if we want, or keep it)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA")
            
        # Resize if extremely large (e.g., > 1200px) to save bandwidth, else keep original resolution
        max_size = (1200, 1200)
        img.thumbnail(max_size, Image.Resampling.LANCZOS)
        
        # Save as WebP
        output = io.BytesIO()
        img.save(output, format="WEBP", quality=85, method=6) # method=6 is max compression effort
        optimized_bytes = output.getvalue()
        
        # Manage cache size
        if len(IMAGE_CACHE) > MAX_CACHE_ITEMS:
            # simple clear, or we could pop random. In-memory is fast enough to just clear.
            IMAGE_CACHE.clear()
            
        IMAGE_CACHE[url_hash] = optimized_bytes
        
        return Response(
            content=optimized_bytes, 
            media_type="image/webp",
            headers={"Cache-Control": "public, max-age=31536000, immutable"}
        )
    except Exception as e:
        logger.error(f"Image proxy error for {url}: {e}")
        # If optimization fails, we can redirect to the original URL
        return Response(status_code=302, headers={"Location": url})

@api_router.get("/tg-image")
async def tg_image_proxy(file_id: str, bot_id: str = None):
    """Fetches image directly from Telegram using a file_id, optimizes to WebP and caches it."""
    from AryaPremium.config import Config
    
    token = None
    if bot_id:
        try:
            arya_db = app.state.db
            bot_doc = await arya_db.db.premium_bots.find_one({"$or": [{"id": int(bot_id)}, {"bot_id": int(bot_id)}]})
            if bot_doc and bot_doc.get("token"):
                token = bot_doc["token"]
        except Exception as e:
            logger.error(f"Failed to fetch bot token for {bot_id}: {e}")
            
    if not token:
        token = Config.MGMT_BOT_TOKEN or os.environ.get("MGMT_BOT_TOKEN")
        
    if not token:
        raise HTTPException(status_code=500, detail="No bot token available")
        
    if file_id in IMAGE_CACHE:
        return Response(content=IMAGE_CACHE[file_id], media_type="image/webp", headers={"Cache-Control": "public, max-age=31536000, immutable"})
        
    try:
        async with aiohttp.ClientSession() as session:
            # 1. Get file path
            async with session.get(f"https://api.telegram.org/bot{token}/getFile", params={"file_id": file_id}, timeout=10) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    raise HTTPException(status_code=404, detail="getFile failed")
                file_path = data["result"]["file_path"]
                
            # 2. Download file
            dl_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
            async with session.get(dl_url, timeout=15) as resp:
                if resp.status != 200:
                    raise HTTPException(status_code=404, detail="File download failed")
                img_bytes = await resp.read()
                
        # Optimize using Pillow in a separate thread
        import asyncio
        def process_image(img_data):
            img = Image.open(io.BytesIO(img_data))
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA")
            img.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            img.save(output, format="WEBP", quality=85, method=6)
            return output.getvalue()
            
        optimized_bytes = await asyncio.to_thread(process_image, img_bytes)
        
        if len(IMAGE_CACHE) > MAX_CACHE_ITEMS:
            IMAGE_CACHE.clear()
            
        IMAGE_CACHE[file_id] = optimized_bytes
        
        return Response(
            content=optimized_bytes, 
            media_type="image/webp",
            headers={"Cache-Control": "public, max-age=31536000, immutable"}
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"TG Image proxy error for {file_id}: {e}")
        # Return a fallback or 404
        raise HTTPException(status_code=404, detail="Image fetch failed")

# ————————————————————————————————————————————————————————————————————————————————————————————————————
# Helper: format a single MongoDB story doc → frontend Story shape
# ————————————————————————————————————————————————————————————————————————————————————————————————————
def _format_story(s: dict) -> dict | None:
    # ID — never null
    story_id = str(s["_id"]) if s.get("_id") else None
    if not story_id:
        return None

    # TITLE — real value, never "Unknown"
    title = (
        s.get("story_name_en")
        or s.get("story_name_hi")
        or s.get("story_name")
        or s.get("name")
        or s.get("title")
    )
    if title:
        title = title.strip()
    if not title:
        return None  # skip stories with no title

    # DESCRIPTION — clean UTF-8
    description = s.get("description") or s.get("description_hi") or ""
    try:
        description = description.encode("utf-8", errors="ignore").decode("utf-8").strip()
    except Exception:
        description = ""

    # COVER — prefer HTTP URL, fallback to Telegram file_id, then placeholder
    cover = (
        s.get("poster_url")
        or s.get("cover")
        or s.get("image_url")
        or s.get("image")       # Telegram file_id (mgmt bot saves this)
        or "https://images.unsplash.com/photo-1614729939124-032f0b56c9ce?w=400"
    )
    if cover and not cover.startswith("http"):
        bot_id = s.get("bot_id")
        cover = f"/api/tg-image?file_id={cover}" + (f"&bot_id={bot_id}" if bot_id else "")

    banner = s.get("banner_url") or cover
    if banner and not banner.startswith("http"):
        bot_id = s.get("bot_id")
        banner = f"/api/tg-image?file_id={banner}" + (f"&bot_id={bot_id}" if bot_id else "")

    return {
        "id":           story_id,
        "title":        title,
        "titleHi":      (s.get("story_name_hi") or "").strip() or None,
        "titleHin":     (s.get("story_name_hin") or "").strip() or None,
        "description":  description,
        "descriptionHi": (s.get("description_hi") or "").strip() or None,
        "descriptionHin": (s.get("description_hin") or "").strip() or None,
        "poster":       cover,
        "banner":       banner,
        "cover":        cover,
        "title_position": s.get("title_position") or "left",
        "price":        float(s.get("price") or 0),
        "language":     s.get("language") or "Hindi",
        "platform":     s.get("platform") or "Pocket FM",
        "genre":        s.get("genre") or "Drama",
        "status":       "available",
        "episodes":     s.get("episodes") or s.get("ep_count") or s.get("total_eps") or "?",
        "totalEpisodes":s.get("episodes") or s.get("total_eps") or s.get("ep_count") or "?",
        "size":         s.get("total_size") or s.get("size") or None,
        "isCompleted":  bool(s.get("is_completed") or s.get("completed") or
                            (s.get("status", "") == "Completed")),
        "fileCount":    s.get("fileCount") or (abs(s.get('end_id', 0) - s.get('start_id', 0)) + 1 if s.get('end_id') and s.get('start_id') else None),
        "is_must_have":  bool(s.get("is_must_have", False)),
    }


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# GET /stories
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@api_router.get("/stories")
async def get_stories():
    """Fetch all premium stories with dynamic engagement counts from orders and analytics collections"""
    try:
        arya_db = app.state.db
        stories = await arya_db.get_all_stories()

        # Aggregate purchases (orders with status paid/delivered) in the last 30 days
        thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
        purchase_pipeline = [
            {"$match": {
                "status": {"$in": ["paid", "delivered"]},
                "created_at": {"$gte": thirty_days_ago}
            }},
            {"$unwind": "$story_ids"},
            {"$group": {
                "_id": "$story_ids",
                "purchases": {"$sum": 1}
            }}
        ]
        purchases_agg = await arya_db.db.orders.aggregate(purchase_pipeline).to_list(length=None)
        purchase_map = {str(p["_id"]): int(p.get("purchases", 0)) for p in purchases_agg}

        # Aggregate story views (clicks) in the last 7 days
        seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
        views_pipeline = [
            {"$match": {
                "type": "view_story",
                "timestamp": {"$gte": seven_days_ago}
            }},
            {"$project": {
                "story_id": {
                    "$cond": {
                        "if": {"$and": [{"$gt": ["$story_id", None]}, {"$ne": ["$story_id", ""]}]},
                        "then": "$story_id",
                        "else": "$data.story_id"
                    }
                }
            }},
            {"$match": {
                "story_id": {"$ne": None}
            }},
            {"$group": {
                "_id": "$story_id",
                "views": {"$sum": 1}
            }}
        ]
        views_agg = await arya_db.db.mini_app_analytics.aggregate(views_pipeline).to_list(length=None)
        views_map = {str(v["_id"]): int(v.get("views", 0)) for v in views_agg}

        formatted = []
        for s in stories:
            item = _format_story(s)
            if item:
                sid = item["id"]
                item["purchase_count"] = purchase_map.get(sid, 0)
                item["view_count"] = views_map.get(sid, 0)
                item["trending_score"] = float(item["purchase_count"] * 100.0 + item["view_count"] * 1.0)
                formatted.append(item)

        logger.info(f"Returning {len(formatted)} stories with dynamic engagement metrics")
        return {"success": True, "data": formatted}

    except Exception as e:
        logger.error(f"Error in /stories: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/trending")
async def get_trending(limit: int = 10):
    """
    Computes real-time trending stories:
      - Purchases: Count instances of story IDs in paid/delivered orders. (Weight: 100)
      - Views: Count 'view_story' events in mini_app_analytics. (Weight: 1)
    """
    try:
        arya_db = app.state.db
        from bson.objectid import ObjectId

        # 1. Aggregate purchases over the last 30 days
        thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
        purchase_pipeline = [
            {"$match": {
                "status": {"$in": ["paid", "delivered"]},
                "created_at": {"$gte": thirty_days_ago}
            }},
            {"$unwind": "$story_ids"},
            {"$group": {
                "_id": "$story_ids",
                "purchases": {"$sum": 1}
            }}
        ]
        purchases_agg = await arya_db.db.orders.aggregate(purchase_pipeline).to_list(length=None)
        
        # 2. Aggregate views (clicks on story card) over the last 7 days
        seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
        views_pipeline = [
            {"$match": {
                "type": "view_story",
                "timestamp": {"$gte": seven_days_ago}
            }},
            {"$project": {
                "story_id": {
                    "$cond": {
                        "if": {"$and": [{"$gt": ["$story_id", None]}, {"$ne": ["$story_id", ""]}]},
                        "then": "$story_id",
                        "else": "$data.story_id"
                    }
                }
            }},
            {"$match": {
                "story_id": {"$ne": None}
            }},
            {"$group": {
                "_id": "$story_id",
                "views": {"$sum": 1}
            }}
        ]
        views_agg = await arya_db.db.mini_app_analytics.aggregate(views_pipeline).to_list(length=None)

        # 3. Combine scores
        scores = {}
        for p in purchases_agg:
            sid = str(p["_id"])
            scores[sid] = scores.get(sid, 0.0) + float(p.get("purchases", 0)) * 100.0

        for v in views_agg:
            sid = str(v["_id"])
            scores[sid] = scores.get(sid, 0.0) + float(v.get("views", 0)) * 1.0

        # Sort by score descending
        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        top_sids = [sid for sid, score in sorted_scores[:limit]]

        # Fetch actual story documents
        all_stories = await arya_db.get_all_stories()
        story_map = {str(s["_id"]): s for s in all_stories}

        trending_list = []
        seen_sids = set()

        for sid in top_sids:
            if sid in story_map:
                fmt = _format_story(story_map[sid])
                if fmt:
                    fmt["purchase_count"] = int(next((p.get("purchases", 0) for p in purchases_agg if str(p["_id"]) == sid), 0))
                    fmt["view_count"] = int(next((v.get("views", 0) for v in views_agg if str(v["_id"]) == sid), 0))
                    fmt["trending_score"] = scores.get(sid, 0.0)
                    trending_list.append(fmt)
                    seen_sids.add(sid)

        # Fallback to fill up to limit using newest stories
        if len(trending_list) < limit:
            newest_stories = sorted(all_stories, key=lambda x: x.get("_id"), reverse=True)
            for s in newest_stories:
                sid = str(s["_id"])
                if sid not in seen_sids:
                    fmt = _format_story(s)
                    if fmt:
                        fmt["purchase_count"] = 0
                        fmt["view_count"] = 0
                        fmt["trending_score"] = 0.0
                        trending_list.append(fmt)
                        seen_sids.add(sid)
                        if len(trending_list) >= limit:
                            break

        logger.info(f"Returning {len(trending_list)} dynamic trending stories")
        return {"success": True, "data": trending_list[:limit]}
    except Exception as e:
        logger.error(f"Trending route error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))



from AryaPremium.config import Config

import razorpay

# Use the existing keys from AryaPremium config
RZP_KEY_ID = Config.RAZORPAY_KEY
RZP_KEY_SECRET = Config.RAZORPAY_SECRET
rzp_client = razorpay.Client(auth=(RZP_KEY_ID, RZP_KEY_SECRET))

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# POST /create-payment-link
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@api_router.post("/create-payment-link")
async def create_payment_link(payload: dict):
    """Creates a Razorpay Payment Link linked to an order."""
    telegram_id = payload.get("telegram_id")
    story_ids   = payload.get("story_ids", [])
    username    = payload.get("username", "")
    promo_code  = payload.get("promo_code", "")
    is_int      = payload.get("is_international", False)

    if not telegram_id or not story_ids:
        raise HTTPException(status_code=400, detail="Missing telegram_id or story_ids")

    arya_db = app.state.db

    # Validate stories exist
    from bson.objectid import ObjectId
    valid_stories = []
    for sid in story_ids:
        try:
            doc = await arya_db.db.premium_stories.find_one({"_id": ObjectId(sid)})
            if doc:
                valid_stories.append(doc)
        except Exception:
            pass

    if not valid_stories:
        raise HTTPException(status_code=400, detail="Invalid stories requested")

    # Fetch settings
    cfg = await arya_db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    
    subtotal = sum(float(s.get("price", 0) or 0) for s in valid_stories)
    
    # 1. Promo Code Discount
    discount = 0.0
    pcode_clean = str(promo_code).strip().upper()
    if pcode_clean:
        active_promos = cfg.get("promo_codes", [])
        promo_match = next((p for p in active_promos if p.get("code") == pcode_clean and p.get("active")), None)
        if promo_match:
            ptype = promo_match.get("type", "percentage")
            pval = float(promo_match.get("value", 0))
            if ptype == "percentage":
                discount = round((subtotal * pval) / 100.0, 2)
            elif ptype == "flat":
                discount = min(pval, subtotal)
                
    # 2. Platform Fee
    platform_fee = 0.0
    if cfg.get("platform_fee_enabled", True):
        platform_fee = float(cfg.get("platform_fee_amount", 5.0))
        
    # 3. Razorpay Fee
    razorpay_fee = 0.0
    if cfg.get("razorpay_fee_enabled", True):
        rzp_rate = 3.54 if is_int else float(cfg.get("razorpay_fee_percent", 2.36))
        chargeable_amount = max(0.0, subtotal - discount + platform_fee)
        razorpay_fee = round((chargeable_amount * rzp_rate) / 100.0, 2)
        
    total_price = max(0.0, subtotal - discount + platform_fee + razorpay_fee)
    if total_price <= 0:
        raise HTTPException(status_code=400, detail="Invalid price")

    order_id = f"OD_{uuid.uuid4().hex[:8].upper()}"
    bot_username = os.environ.get("BOT_USERNAME", "UseAryaBot")
    
    try:
        if not RZP_KEY_ID or not RZP_KEY_SECRET:
            raise ValueError("Razorpay API keys are missing in the .env file. Please check RAZORPAY_KEY and RAZORPAY_SECRET.")

        # Create Razorpay Payment Link
        link_data = rzp_client.payment_link.create({
            "amount": int(total_price * 100), # in paise
            "currency": "INR",
            "accept_partial": False,
            "description": ", ".join([s.get("story_name_en") or s.get("title") or s.get("story_name_hi") or "Arya Premium Content" for s in valid_stories])[:200] or "Arya Premium Content",
            "customer": {
                "name": username or f"User {telegram_id}",
                "email": f"user{telegram_id}@sliceurl.com"
            },
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "reference_id": order_id,
            "callback_url": f"https://t.me/{bot_username}", # Redirect back to bot after payment
            "callback_method": "get"
        })
        
        tg_id_int = int(telegram_id) if str(telegram_id).isdigit() else telegram_id
        
        # Save order to DB
        order_doc = {
            "order_id":    order_id,
            "payment_link_id": link_data["id"],
            "user_id":     tg_id_int,
            "username":    username or "Unknown",
            "story_ids":   story_ids,
            "story_names": [s.get("story_name_en", s.get("title", "")) for s in valid_stories],
            "subtotal":    subtotal,
            "discount":    discount,
            "promo_code":  pcode_clean if discount > 0 else None,
            "platform_fee": platform_fee,
            "razorpay_fee": razorpay_fee,
            "total":       total_price,
            "status":      "pending",
            "source":      "razorpay_link",
            "created_at":  datetime.now(timezone.utc),
        }
        await arya_db.db.orders.insert_one(order_doc)
        logger.info(f"Payment Link created: {link_data['id']} for user {telegram_id}")

        return {
            "success": True,
            "payment_link_id": link_data["id"],
            "payment_link_url": link_data["short_url"]
        }
    except Exception as e:
        logger.error(f"Razorpay link creation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# POST /check-payment-link
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@api_router.post("/check-payment-link")
async def check_payment_link(id: str, payload: dict):
    """Verifies the status of a Razorpay Payment Link."""
    telegram_id = payload.get("telegram_id")
    
    try:
        # Fetch payment link status from Razorpay
        link_data = rzp_client.payment_link.fetch(id)
        status = link_data.get("status")
        
        if status == "paid":
            # Update order in DB
            arya_db = app.state.db
            order = await arya_db.db.orders.find_one({"payment_link_id": id})
            if order and order.get("status") != "paid":
                await arya_db.db.orders.update_one(
                    {"_id": order["_id"]},
                    {"$set": {"status": "paid", "updated_at": datetime.now(timezone.utc)}}
                )
                
                # Logic to grant stories to user in DB goes here
                for sid in order.get("story_ids", []):
                    await arya_db.add_purchase(int(telegram_id) if str(telegram_id).isdigit() else telegram_id, sid)
            
            bot_username = os.environ.get("BOT_USERNAME", "UseAryaBot")
            return {
                "success": True,
                "status": "paid",
                "checkout_url": f"https://t.me/{bot_username}?start=success_{order['order_id']}" if order else ""
            }
            
        return {"success": True, "status": "pending"}
    except Exception as e:
        logger.error(f"Razorpay verification failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))



import hmac
import hashlib

def _make_order_id(tg_id: str) -> str:
    import uuid
    return f"OD_{tg_id}_{uuid.uuid4().hex[:8].upper()}"

# â”€â”€ Razorpay: Create Order â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@api_router.post("/create-order")
async def create_razorpay_order(payload: dict):
    """Create Razorpay order. Returns order_id + key for frontend SDK modal."""
    story_ids  = payload.get("story_ids", [])
    tg_id      = payload.get("telegram_id") or 0
    is_int     = payload.get("is_international", False)
    promo_code = payload.get("promo_code", "")

    if not story_ids:
        raise HTTPException(400, "Cart is empty")
    if not RZP_KEY_ID or not RZP_KEY_SECRET:
        raise HTTPException(500, "Razorpay not configured on server")

    arya_db = app.state.db
    from bson.objectid import ObjectId
    valid_stories = []
    for sid in story_ids:
        try:
            doc = await arya_db.db.premium_stories.find_one({"_id": ObjectId(sid)})
            if doc:
                valid_stories.append(doc)
        except Exception:
            pass

    if not valid_stories:
        raise HTTPException(400, "No valid stories")

    # Fetch settings
    cfg = await arya_db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    
    subtotal = sum(float(s.get("price", 0) or 0) for s in valid_stories)
    
    # 1. Promo Code Discount
    discount = 0.0
    pcode_clean = str(promo_code).strip().upper()
    if pcode_clean:
        active_promos = cfg.get("promo_codes", [])
        promo_match = next((p for p in active_promos if p.get("code") == pcode_clean and p.get("active")), None)
        if promo_match:
            ptype = promo_match.get("type", "percentage")
            pval = float(promo_match.get("value", 0))
            if ptype == "percentage":
                discount = round((subtotal * pval) / 100.0, 2)
            elif ptype == "flat":
                discount = min(pval, subtotal)
                
    # 2. Platform Fee
    platform_fee = 0.0
    if cfg.get("platform_fee_enabled", True):
        platform_fee = float(cfg.get("platform_fee_amount", 5.0))
        
    # 3. Razorpay Fee
    razorpay_fee = 0.0
    if cfg.get("razorpay_fee_enabled", True):
        rzp_rate = 3.54 if is_int else float(cfg.get("razorpay_fee_percent", 2.36))
        chargeable_amount = max(0.0, subtotal - discount + platform_fee)
        razorpay_fee = round((chargeable_amount * rzp_rate) / 100.0, 2)
        
    total_price = max(0.0, subtotal - discount + platform_fee + razorpay_fee)
    if total_price <= 0:
        raise HTTPException(400, "Invalid total amount (must be greater than 0)")
        
    total_paise = int(total_price * 100)
    receipt     = _make_order_id(tg_id)

    import httpx
    import base64
    auth_header = "Basic " + base64.b64encode(f"{RZP_KEY_ID}:{RZP_KEY_SECRET}".encode()).decode()

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
            "key":               RZP_KEY_ID,
            "receipt":           receipt,
            "story_names":       [s.get("story_name_en", s.get("title", "")) for s in valid_stories],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"create-order error: {e}", exc_info=True)
        raise HTTPException(500, str(e))


# â”€â”€ Razorpay: Verify Payment (HMAC) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@api_router.post("/verify-payment")
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
    promo_code     = payload.get("promo_code", "")
    is_int         = payload.get("is_international", False)

    if not all([rzp_order_id, rzp_payment_id, rzp_signature]):
        raise HTTPException(400, "Missing payment verification fields")

    # HMAC-SHA256 verification
    expected = hmac.new(
        RZP_KEY_SECRET.encode("utf-8"),
        f"{rzp_order_id}|{rzp_payment_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, rzp_signature):
        logger.warning(f"Invalid Razorpay signature for {rzp_payment_id}")
        raise HTTPException(400, "Payment verification failed — invalid signature")

    # Signature OK — store order + unlock content
    arya_db = app.state.db
    from bson.objectid import ObjectId
    valid_stories = []
    for sid in story_ids:
        try:
            doc = await arya_db.db.premium_stories.find_one({"_id": ObjectId(sid)})
            if doc:
                valid_stories.append(doc)
        except Exception:
            pass

    # Fetch settings
    cfg = await arya_db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    
    subtotal = sum(float(s.get("price", 0) or 0) for s in valid_stories)
    
    # 1. Promo Code Discount
    discount = 0.0
    pcode_clean = str(promo_code).strip().upper()
    if pcode_clean:
        active_promos = cfg.get("promo_codes", [])
        promo_match = next((p for p in active_promos if p.get("code") == pcode_clean and p.get("active")), None)
        if promo_match:
            ptype = promo_match.get("type", "percentage")
            pval = float(promo_match.get("value", 0))
            if ptype == "percentage":
                discount = round((subtotal * pval) / 100.0, 2)
            elif ptype == "flat":
                discount = min(pval, subtotal)
                
    # 2. Platform Fee
    platform_fee = 0.0
    if cfg.get("platform_fee_enabled", True):
        platform_fee = float(cfg.get("platform_fee_amount", 5.0))
        
    # 3. Razorpay Fee
    razorpay_fee = 0.0
    if cfg.get("razorpay_fee_enabled", True):
        rzp_rate = 3.54 if is_int else float(cfg.get("razorpay_fee_percent", 2.36))
        chargeable_amount = max(0.0, subtotal - discount + platform_fee)
        razorpay_fee = round((chargeable_amount * rzp_rate) / 100.0, 2)
        
    total = max(0.0, subtotal - discount + platform_fee + razorpay_fee)
    oid     = _make_order_id(str(tg_id))

    tg_id_int = int(tg_id) if str(tg_id).isdigit() else 0

    await arya_db.db.orders.insert_one({
        "order_id":            oid,
        "user_id":             tg_id_int if tg_id_int else tg_id,
        "username":            username,
        "story_ids":           story_ids,
        "story_names":         [s.get("story_name_en", s.get("title", "")) for s in valid_stories],
        "subtotal":            subtotal,
        "discount":            discount,
        "promo_code":          pcode_clean if discount > 0 else None,
        "platform_fee":        platform_fee,
        "razorpay_fee":        razorpay_fee,
        "total":               total,
        "status":              "paid",
        "source":              "razorpay_miniapp",
        "razorpay_order_id":   rzp_order_id,
        "razorpay_payment_id": rzp_payment_id,
        "created_at":          datetime.now(timezone.utc),
    })

    if tg_id:
        for sid in story_ids:
            await arya_db.add_purchase(tg_id_int if tg_id_int else tg_id, sid)

    return {"success": True, "message": "Payment verified successfully"}


from fastapi import Form
from fastapi.responses import RedirectResponse

@api_router.post("/razorpay-callback")
async def razorpay_callback(
    razorpay_payment_id: str = Form(...),
    razorpay_order_id: str = Form(...),
    razorpay_signature: str = Form(...)
):
    """
    Callback URL for Razorpay when redirect flow is used (e.g. Wallets, Netbanking).
    """
    if not all([razorpay_order_id, razorpay_payment_id, razorpay_signature]):
        raise HTTPException(400, "Missing payment verification fields")

    expected = hmac.new(
        RZP_KEY_SECRET.encode("utf-8"),
        f"{razorpay_order_id}|{razorpay_payment_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, razorpay_signature):
        logger.warning(f"Invalid Razorpay signature for {razorpay_payment_id}")
        raise HTTPException(400, "Payment verification failed — invalid signature")

    # Fetch order from Razorpay to get notes
    import httpx
    import base64
    auth_header = "Basic " + base64.b64encode(f"{RZP_KEY_ID}:{RZP_KEY_SECRET}".encode()).decode()
    
    notes = {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"https://api.razorpay.com/v1/orders/{razorpay_order_id}",
                headers={"Authorization": auth_header}
            )
            if r.status_code == 200:
                notes = r.json().get("notes", {})
    except Exception as e:
        logger.error(f"Failed to fetch order notes: {e}")

    tg_id = notes.get("telegram_id", "")
    story_ids_str = notes.get("story_ids", "")
    story_ids = story_ids_str.split(",") if story_ids_str else []
    username = notes.get("username", "")

    arya_db = app.state.db
    from bson.objectid import ObjectId
    valid_stories = []
    if story_ids:
        for sid in story_ids:
            try:
                doc = await arya_db.db.premium_stories.find_one({"_id": ObjectId(sid)})
                if doc:
                    valid_stories.append(doc)
            except Exception:
                pass

    total = sum(float(s.get("price", 0) or 0) for s in valid_stories)
    oid = _make_order_id(str(tg_id)) if tg_id else razorpay_order_id
    tg_id_int = int(tg_id) if str(tg_id).isdigit() else 0

    # Store order
    await arya_db.db.orders.insert_one({
        "order_id":            oid,
        "user_id":             tg_id_int if tg_id_int else tg_id,
        "username":            username,
        "story_ids":           story_ids,
        "story_names":         [s.get("story_name_en", s.get("title", "")) for s in valid_stories],
        "total":               total,
        "status":              "paid",
        "source":              "razorpay_callback",
        "razorpay_order_id":   razorpay_order_id,
        "razorpay_payment_id": razorpay_payment_id,
        "created_at":          datetime.now(timezone.utc),
    })

    if tg_id:
        for sid in story_ids:
            await arya_db.add_purchase(tg_id_int if tg_id_int else tg_id, sid)

    bot_username = os.environ.get("BOT_USERNAME", "UseAryaBot")
    return RedirectResponse(url=f"https://t.me/{bot_username}/app", status_code=302)


# ===== OxaPay: Create Crypto Invoice =====

@api_router.post("/create-oxapay-order")
async def create_oxapay_order(payload: dict):
    """Create an OxaPay crypto invoice. Reads API key fresh at request time."""
    # Read key fresh each request so .env changes take effect without restart
    oxapay_key = getattr(Config, "OXAPAY_KEY", "") or os.environ.get("OXAPAY_KEY", "")
    if not oxapay_key or oxapay_key == "sandbox":
        logger.error("OXAPAY_KEY not configured! Add it to .env as OXAPAY_KEY=your_key")
        raise HTTPException(status_code=503, detail="Crypto payment not configured. Contact admin.")

    story_ids = payload.get("story_ids", [])
    tg_id     = payload.get("telegram_id") or 0
    username  = payload.get("username", "")

    if not story_ids:
        raise HTTPException(status_code=400, detail="Cart is empty")

    arya_db = app.state.db
    from bson.objectid import ObjectId
    valid_stories = []
    for sid in story_ids:
        try:
            doc = await arya_db.db.premium_stories.find_one({"_id": ObjectId(sid)})
            if doc:
                valid_stories.append(doc)
        except Exception:
            pass

    if not valid_stories:
        raise HTTPException(status_code=400, detail="No valid stories found in cart")

    total_inr = sum(float(s.get("price", 0) or 0) for s in valid_stories)
    promo_code = payload.get("promo_code", "")

    # Fetch settings for promo codes
    cfg = await arya_db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    discount = 0.0
    pcode_clean = str(promo_code).strip().upper()
    if pcode_clean:
        active_promos = cfg.get("promo_codes", [])
        promo_match = next((p for p in active_promos if p.get("code") == pcode_clean and p.get("active")), None)
        if promo_match:
            ptype = promo_match.get("type", "percentage")
            pval = float(promo_match.get("value", 0))
            if ptype == "percentage":
                discount = round((total_inr * pval) / 100.0, 2)
            elif ptype == "flat":
                discount = min(pval, total_inr)

    total_inr = max(0.0, total_inr - discount)
    # OxaPay expects USD. Minimum $0.50.
    total_usd = max(0.5, round(total_inr / 85.0, 2))
    oid = _make_order_id(str(tg_id))

    # Call OxaPay API
    oxapay_result = None
    oxapay_error = None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.oxapay.com/merchants/request",
                json={
                    "merchant": oxapay_key,
                    "amount": total_usd,
                    "currency": "USD",
                    "lifeTime": 30,
                    "feePaidByPayer": 1,
                    "orderId": oid,
                    "description": f"{len(valid_stories)} Arya Premium stories for {tg_id}",
                    "returnUrl": "https://t.me/UseAryaBot/app",
                }
            )
            oxapay_result = r.json()
    except Exception as e:
        oxapay_error = str(e)

    if oxapay_error:
        logger.error(f"OxaPay network error: {oxapay_error}")
        raise HTTPException(status_code=502, detail="Failed to reach OxaPay. Try again.")

    if oxapay_result.get("result") != 100:
        logger.error(f"OxaPay rejected: {oxapay_result}")
        raise HTTPException(status_code=502, detail=f"OxaPay error: {oxapay_result.get('message', 'Unknown')}")

    pay_link  = oxapay_result.get("payLink") or oxapay_result.get("pay_link")
    track_id  = oxapay_result.get("trackId") or oxapay_result.get("track_id")

    # Store pending order in DB
    try:
        await arya_db.db.orders.insert_one({
            "order_id":    oid,
            "user_id":     int(tg_id) if str(tg_id).isdigit() else tg_id,
            "username":    username,
            "story_ids":   story_ids,
            "story_names": [s.get("story_name_en", s.get("title", "")) for s in valid_stories],
            "total":       total_inr,
            "total_usd":   total_usd,
            "status":      "pending",
            "source":      "oxapay_miniapp",
            "track_id":    track_id,
            "pay_link":    pay_link,
            "created_at":  datetime.now(timezone.utc),
        })
    except Exception as e:
        logger.error(f"DB insert error for OxaPay order: {e}")

    logger.info(f"OxaPay invoice created: order={oid} usd={total_usd} user={tg_id}")
    return {"success": True, "payLink": pay_link, "trackId": track_id}


@api_router.post("/oxapay-webhook")
async def oxapay_webhook(request: Request):
    """Webhook from OxaPay upon successful payment."""
    oxapay_key = getattr(Config, "OXAPAY_KEY", "") or os.environ.get("OXAPAY_KEY", "")
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    track_id = data.get("trackId")
    status   = data.get("status")

    if status != "Paid" or not track_id:
        return {"success": False, "message": "Ignored or invalid status"}

    # Verify via OxaPay Inquiry API to prevent fake webhooks
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://api.oxapay.com/merchants/inquiry",
                json={"merchant": oxapay_key, "trackId": track_id}
            )
            inquiry = r.json()
            if inquiry.get("result") != 100 or inquiry.get("status") != "Paid":
                logger.warning(f"OxaPay verification failed: {track_id} → {inquiry}")
                return {"success": False, "message": "Verification failed"}
    except Exception as e:
        logger.error(f"OxaPay inquiry error: {e}")
        return {"success": False, "message": "Inquiry error"}

    # Verified! Unlock stories
    arya_db = app.state.db
    order = await arya_db.db.orders.find_one({"track_id": track_id})

    if not order:
        logger.warning(f"OxaPay webhook: order not found for trackId={track_id}")
        return {"success": False, "message": "Order not found"}

    if order.get("status") == "paid":
        return {"success": True, "message": "Already processed"}

    await arya_db.db.orders.update_one(
        {"_id": order["_id"]},
        {"$set": {
            "status":       "paid",
            "payment_id":   data.get("txID", ""),
            "paid_currency": data.get("payCurrency", ""),
            "paid_at":      datetime.now(timezone.utc),
        }}
    )

    user_id   = order.get("user_id")
    story_ids = order.get("story_ids", [])
    if user_id:
        for sid in story_ids:
            try:
                await arya_db.add_purchase(user_id, sid)
            except Exception as e:
                logger.error(f"add_purchase error for {sid}: {e}")

    logger.info(f"OxaPay ✅ unlocked {len(story_ids)} stories for user={user_id} trackId={track_id}")
    return {"success": True, "message": "Payment verified and processed"}



# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# POST /support
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@api_router.post("/support")
async def submit_support(
    telegram_id: str = Form(...),
    type: str = Form("support"),
    message: str = Form(""),
    username: str = Form(""),
    first_name: str = Form("Mini App User"),
    file: UploadFile = File(None)
):
    """Submits a support ticket, feedback, or suggestion from the Mini App, with optional file attachment."""
    message = message.strip()
    
    if not telegram_id or (not message and not file):
        raise HTTPException(status_code=400, detail="Message or file is required")

    arya_db = app.state.db
    
    # Save to premium_feedback collection so it appears in Management Bot Support Panel
    fb_doc = {
        "user_id": int(telegram_id) if str(telegram_id).isdigit() else telegram_id,
        "bot_id": "mini_app",
        "type": "photo" if file and file.content_type and file.content_type.startswith("image/") else ("video" if file and file.content_type and file.content_type.startswith("video/") else ("document" if file else "text")),
        "text": f"[{type.upper()}] {message}",
        "status": "open",
        "created_at": datetime.now(timezone.utc),
        "user_name": first_name,
        "username": username,
    }
    
    try:
        await arya_db.db.premium_feedback.insert_one(fb_doc)
        
        # Notify admins via Telegram API using Management Bot Token
        from AryaPremium.config import Config
        import aiohttp
        
        if type == "request":
            admin_txt = (
                f"<b>New Story Request from Mini App</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>User:</b> {first_name}\n"
                f"<b>Username:</b> @{username}\n"
                f"<b>User ID:</b> <code>{telegram_id}</code>\n"
                f"<b>Type:</b> Story Request\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>Details:</b>\n"
                f"<blockquote>{message[:800]}</blockquote>"
            )
        else:
            admin_txt = (
                f"<b>New Feedback from Mini App</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>User:</b> {first_name}\n"
                f"<b>Username:</b> @{username}\n"
                f"<b>User ID:</b> <code>{telegram_id}</code>\n"
                f"<b>Type:</b> {type.title()}\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>Message:</b>\n"
                f"<blockquote>{message[:800]}</blockquote>"
            )
        
        token = Config.MGMT_BOT_TOKEN
        if token and Config.OWNER_IDS:
            async with aiohttp.ClientSession() as session:
                file_bytes = None
                if file:
                    file_bytes = await file.read()
                
                for oid in Config.OWNER_IDS:
                    try:
                        if file_bytes:
                            form = aiohttp.FormData()
                            form.add_field('chat_id', str(oid))
                            form.add_field('caption', admin_txt)
                            form.add_field('parse_mode', 'HTML')
                            
                            method = "sendDocument"
                            field_name = "document"
                            if file.content_type:
                                if file.content_type.startswith("image/"):
                                    method = "sendPhoto"
                                    field_name = "photo"
                                elif file.content_type.startswith("video/"):
                                    method = "sendVideo"
                                    field_name = "video"
                                elif file.content_type.startswith("audio/"):
                                    method = "sendAudio"
                                    field_name = "audio"
                                    
                            form.add_field(field_name, file_bytes, filename=file.filename or "file")
                            await session.post(f"https://api.telegram.org/bot{token}/{method}", data=form, timeout=60)
                        else:
                            await session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": oid,
                                "text": admin_txt,
                                "parse_mode": "HTML"
                            }, timeout=3)
                    except Exception as e:
                        logger.warning(f"Failed to notify admin {oid}: {e}")
                        
        return {"success": True, "message": "Support request submitted successfully"}
    except Exception as e:
        logger.error(f"Support submission failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit support request")


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# GET /my-requests
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@api_router.get("/my-requests")
async def get_my_requests(telegram_id: str):
    """Fetches user's requests and support tickets."""
    arya_db = app.state.db
    
    try:
        user_id = int(telegram_id) if telegram_id.isdigit() else telegram_id
        cursor = arya_db.db.premium_feedback.find({
            "user_id": user_id,
            "text": {"$regex": "^\\[REQUEST\\]", "$options": "i"}
        }).sort("created_at", -1)
        
        requests = []
        async for doc in cursor:
            requests.append({
                "id": str(doc.get("_id", "")),
                "type": doc.get("type", "text"),
                "text": doc.get("text", ""),
                "status": doc.get("status", "open"),
                "created_at": doc.get("created_at", datetime.now(timezone.utc)).isoformat() if isinstance(doc.get("created_at"), datetime) else doc.get("created_at", "")
            })
            
        return {"success": True, "data": requests}
    except Exception as e:
        logger.error(f"Failed to fetch requests: {e}")
        return {"success": False, "data": []}

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# GET /my-purchases
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@api_router.get("/my-purchases")
async def get_my_purchases(telegram_id: str):
    """Fetches user's purchased stories."""
    arya_db = app.state.db
    try:
        from bson.objectid import ObjectId
        
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        
        # In AryaPremium, purchases are in user.purchases
        user = await arya_db.users.find_one({"id": user_id_int})
        if not user:
            return {"success": True, "data": []}
            
        purchased_story_ids = user.get("purchases", [])
        
        purchased_items = []
        for story_id in purchased_story_ids:
            try:
                story = await arya_db.db.premium_stories.find_one({"_id": ObjectId(story_id)})
                if story:
                    formatted = _format_story(story)
                    if formatted:
                        formatted["story_id"] = formatted["id"]
                        
                        # Find if there's an order via Mini App for this story
                        order = await arya_db.db.orders.find_one({
                            "user_id": {"$in": [user_id_int, str(user_id_int)]},
                            "story_ids": story_id,
                            "status": {"$in": ["paid", "delivered"]}
                        })
                        
                        if order:
                            formatted["order_details"] = {
                                "order_id": order.get("order_id") or order.get("payment_link_id") or order.get("razorpay_order_id"),
                                "source": order.get("source", "miniapp"),
                                "status": order.get("status"),
                                "created_at": order.get("created_at").isoformat() if isinstance(order.get("created_at"), datetime) else str(order.get("created_at", ""))
                            }
                        else:
                            formatted["order_details"] = None

                        purchased_items.append(formatted)
            except Exception:
                pass
                
        return {"success": True, "data": purchased_items}
    except Exception as e:
        logger.error(f"Failed to fetch my-purchases: {e}")
        return {"success": False, "data": []}

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# GET /admin/stats
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@api_router.get("/admin/stats")
async def get_admin_stats(telegram_id: str):
    """Fetches full admin analysis dashboard."""
    from AryaPremium.config import Config
    
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized as Admin")
            
        arya_db = app.state.db
        
        # Bot Users (users who have interacted with the Telegram bot)
        bot_users_count = await arya_db.db.users.count_documents({})
        
        # Count unique Mini App users (visitors) from analytics
        from arya_enterprise_analytics import visitor_id_expression
        bot_filter = {
            "data.client_user_agent": {
                "$not": {
                    "$regex": "bot|crawler|spider|ping|uptime|status|http|curl|wget|python|node|axios|fetch|headless|selenium|puppeteer|playwright|scrape|scan|checker",
                    "$options": "i"
                }
            }
        }
        
        page_views_count = await arya_db.db.mini_app_analytics.count_documents({"type": "page_view", **bot_filter})
        
        miniapp_users_pipeline = [
            {"$match": {"type": "page_view", **bot_filter}},
            {"$project": {"visitor_id": visitor_id_expression()}},
            {"$group": {"_id": "$visitor_id"}},
            {"$count": "c"}
        ]
        miniapp_users_res = await arya_db.db.mini_app_analytics.aggregate(miniapp_users_pipeline).to_list(length=1)
        miniapp_users_count = miniapp_users_res[0]["c"] if miniapp_users_res else 0
        total_users_count = bot_users_count + miniapp_users_count
        
        # Total Stories
        total_stories = await arya_db.db.premium_stories.count_documents({})
        
        # Mini App Revenue (orders with status=paid)
        miniapp_rev_pipeline = [
            {"$match": {"status": "paid"}},
            {"$group": {"_id": None, "total": {"$sum": {"$cond": [{"$gt": ["$total_amount", 0]}, "$total_amount", {"$ifNull": ["$total", 0]}]}}}}
        ]
        miniapp_rev_res = await arya_db.db.orders.aggregate(miniapp_rev_pipeline).to_list(length=1)
        miniapp_revenue = miniapp_rev_res[0]["total"] if miniapp_rev_res else 0
        
        # Bot Revenue (from premium_checkout with status=approved)
        bot_rev_pipeline = [
            {"$match": {"status": "approved"}},
            {"$group": {"_id": None, "total": {"$sum": {"$toDouble": {"$ifNull": ["$amount", 0]}}}}}
        ]
        bot_rev_res = await arya_db.db.premium_checkout.aggregate(bot_rev_pipeline).to_list(length=1)
        bot_revenue = bot_rev_res[0]["total"] if bot_rev_res else 0
        total_revenue = (miniapp_revenue or 0) + (bot_revenue or 0)
        
        # Recent Feedbacks
        feedbacks = []
        fb_cursor = arya_db.db.premium_feedback.find({}).sort("created_at", -1).limit(10)
        async for doc in fb_cursor:
            feedbacks.append({
                "id": str(doc.get("_id", "")),
                "user_id": doc.get("user_id"),
                "username": doc.get("username"),
                "first_name": doc.get("first_name", ""),
                "type": doc.get("type"),
                "text": doc.get("text"),
                "status": doc.get("status"),
                "created_at": doc.get("created_at", datetime.now(timezone.utc)).isoformat() if isinstance(doc.get("created_at"), datetime) else doc.get("created_at", "")
            })
            
        # Recent Orders
        orders = []
        ord_cursor = arya_db.db.orders.find({}).sort("created_at", -1).limit(10)
        async for doc in ord_cursor:
            user_doc = await arya_db.db.users.find_one({"id": doc.get("user_id")}) if doc.get("user_id") else None
            if not user_doc: continue
            _fn = (user_doc.get("first_name") or "").strip()
            _ln = (user_doc.get("last_name") or "").strip()
            _full = " ".join(filter(None, [_fn, _ln])) or user_doc.get("username", "") or "User"
            orders.append({
                "order_id": str(doc.get("order_id", doc.get("_id", ""))),
                "amount": doc.get("total_amount") or doc.get("total") or doc.get("amount", 0),
                "status": doc.get("status", "unknown"),
                "user_id": doc.get("user_id", ""),
                "first_name": _full,
                "username": user_doc.get("username", ""),
                "story_names": doc.get("story_names", []),
                "source": doc.get("source", "miniapp"),
                "created_at": doc.get("created_at", datetime.now(timezone.utc)).isoformat() if isinstance(doc.get("created_at"), datetime) else str(doc.get("created_at", ""))
            })
            
        bot_ord_cursor = arya_db.db.premium_checkout.find({}).sort("created_at", -1).limit(10)
        async for doc in bot_ord_cursor:
            user_doc = await arya_db.db.users.find_one({"id": doc.get("user_id")}) if doc.get("user_id") else None
            if not user_doc: continue
            _fn2 = (user_doc.get("first_name") or "").strip()
            _ln2 = (user_doc.get("last_name") or "").strip()
            _full2 = " ".join(filter(None, [_fn2, _ln2])) or user_doc.get("username", "") or "User"
            username = user_doc.get("username", "")
            
            story_doc = await arya_db.db.premium_stories.find_one({"_id": doc.get("story_id")}) if doc.get("story_id") else None
            story_name = story_doc.get("story_name_en", "Story") if story_doc else "Story"

            orders.append({
                "order_id": f"bot_{doc.get('_id', '')}",
                "amount": doc.get("amount", 0),
                "status": doc.get("status", "unknown"),
                "user_id": doc.get("user_id", ""),
                "first_name": _full2,
                "username": username,
                "story_names": [story_name],
                "source": "bot",
                "created_at": doc.get("created_at", datetime.now(timezone.utc)).isoformat() if isinstance(doc.get("created_at"), datetime) else str(doc.get("created_at", ""))
            })
            
        # Sort and take top 10
        orders = sorted(orders, key=lambda x: x["created_at"], reverse=True)[:10]
            
        return {
            "success": True,
            "data": {
                "total_users": total_users_count,
                "bot_users": bot_users_count,
                "miniapp_users": miniapp_users_count,
                "total_stories": total_stories,
                "total_revenue": total_revenue,
                "miniapp_revenue": miniapp_revenue,
                "bot_revenue": bot_revenue,
                "recent_feedback": feedbacks,
                "recent_orders": orders,
                "page_views": page_views_count
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch admin stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# GET /admin/stories
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@api_router.get("/admin/stories")
async def get_admin_stories(telegram_id: str):
    """Fetches all stories for admin management."""
    from AryaPremium.config import Config
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized as Admin")
            
        arya_db = app.state.db
        stories = await arya_db.get_all_stories()
        result = []
        for s in stories:
            _id_str = str(s["_id"])
            # Always ensure story_id is set â€” fallback to _id if missing
            story_id = s.get("story_id") or _id_str
            
            # Normalize poster_url so it always exists
            cover = s.get("poster_url") or s.get("cover") or s.get("image_url") or s.get("image") or ""
            if cover and not cover.startswith("http"):
                bot_id = s.get("bot_id")
                cover = f"/api/tg-image?file_id={cover}" + (f"&bot_id={bot_id}" if bot_id else "")
                
            s["poster_url"] = cover

            result.append({**s, "_id": _id_str, "story_id": story_id})
        return {"success": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

from pydantic import BaseModel
from typing import Optional, List

class StoryUpdate(BaseModel):
    telegram_id: str
    story_id: Optional[str] = None
    _id: Optional[str] = None  # MongoDB id fallback
    bot_id: Optional[int] = None
    bot_username: Optional[str] = None
    start_id: Optional[int] = None
    end_id: Optional[int] = None
    source: Optional[int] = None
    story_name_en: Optional[str] = ""
    story_name_hi: Optional[str] = ""
    story_name_hin: Optional[str] = ""
    description: Optional[str] = ""
    description_hi: Optional[str] = ""
    episodes: Optional[str] = "1"
    status: Optional[str] = "available"
    genre: Optional[str] = ""
    language: Optional[str] = "Hindi"
    price: Optional[int] = 0
    discount_price: Optional[int] = 0
    payment_methods: Optional[List[str]] = ["upi"]
    platform: Optional[str] = ""
    delivery_mode: Optional[str] = "pool"
    channel_id: Optional[int] = None
    image: Optional[str] = None
    poster_url: Optional[str] = ""
    is_completed: Optional[bool] = False

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# POST /admin/story
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
async def optimize_and_upload_to_storage(img_bytes: bytes, width: int = None, height: int = None, format: str = "JPEG", quality: int = 75) -> str:
    import io
    import uuid
    import asyncio
    import aiohttp
    from PIL import Image
    from decouple import config
    
    r2_account_id = config("R2_ACCOUNT_ID", default="")
    r2_access_key = config("R2_ACCESS_KEY_ID", default="")
    r2_secret_key = config("R2_SECRET_ACCESS_KEY", default="")
    r2_bucket = config("R2_BUCKET_NAME", default="arya-images")
    r2_domain = config("R2_CUSTOM_DOMAIN", default="")

    def process_data(data):
        img = Image.open(io.BytesIO(data))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        if width and height:
            img = img.resize((width, height), Image.Resampling.LANCZOS)
        elif width:
            img.thumbnail((width, width))
        output = io.BytesIO()
        img.save(output, format=format, quality=quality, optimize=True)
        return output.getvalue()

    processed_bytes = await asyncio.to_thread(process_data, img_bytes)
    
    url = ""
    if r2_account_id and r2_access_key and r2_secret_key and r2_bucket:
        import boto3
        def upload_r2():
            try:
                s3 = boto3.client(
                    "s3",
                    endpoint_url=f"https://{r2_account_id}.r2.cloudflarestorage.com",
                    aws_access_key_id=r2_access_key,
                    aws_secret_access_key=r2_secret_key,
                    region_name="auto"
                )
                filename = f"{uuid.uuid4().hex}.jpg"
                s3.put_object(
                    Bucket=r2_bucket,
                    Key=filename,
                    Body=processed_bytes,
                    ContentType="image/jpeg"
                )
                if r2_domain:
                    domain = r2_domain.strip("/")
                    if not domain.startswith("http"):
                        domain = "https://" + domain
                    return f"{domain}/{filename}"
                else:
                    return f"https://{r2_account_id}.r2.cloudflarestorage.com/{r2_bucket}/{filename}"
            except Exception as e:
                logger.error(f"Cloudflare R2 upload failed: {e}")
                return ""
        url = await asyncio.to_thread(upload_r2)

    # Fallback to Catbox
    if not url:
        try:
            async with aiohttp.ClientSession() as session:
                form = aiohttp.FormData()
                form.add_field("reqtype", "fileupload")
                form.add_field("fileToUpload", processed_bytes, filename="image.jpg", content_type="image/jpeg")
                async with session.post("https://catbox.moe/user/api.php", data=form, timeout=10) as resp:
                    if resp.status == 200:
                        url = (await resp.text()).strip()
        except Exception as e:
            logger.error(f"Catbox upload failed: {e}")
            url = ""
            
    return url


@api_router.post("/admin/story")
async def save_admin_story(request: Request):
    """Creates or updates a story — accepts any JSON payload."""
    from AryaPremium.config import Config
    try:
        data = await request.json()
        telegram_id = str(data.get("telegram_id", ""))
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
        
        # Remove non-DB fields
        show_in_banners = data.get("show_in_banners")
        save_doc = {k: v for k, v in data.items() if k not in ("telegram_id", "_id", "show_in_banners")}
        
        # Ensure story_id exists
        if not save_doc.get("story_id"):
            raise HTTPException(status_code=400, detail="story_id is required")
        
        # Check if we should automatically outpaint and upload widescreen banner
        poster_url = save_doc.get("poster_url")
        banner_url = save_doc.get("banner_url")
        
        # Determine if we should generate the outpainted banner
        should_outpaint = False
        if poster_url and (not banner_url or banner_url == poster_url):
            should_outpaint = True
        elif poster_url:
            # Check if poster changed from existing story
            try:
                arya_db = app.state.db
                from bson.objectid import ObjectId
                query = {"story_id": save_doc.get("story_id")}
                if len(str(save_doc.get("story_id"))) == 24:
                    query = {"$or": [{"story_id": save_doc.get("story_id")}, {"_id": ObjectId(str(save_doc.get("story_id")))}]}
                existing = await arya_db.db.premium_stories.find_one(query)
                if existing and (existing.get("poster_url") != poster_url or not existing.get("banner_url")):
                    should_outpaint = True
            except:
                should_outpaint = True
                
        if should_outpaint:
            try:
                logger.info(f"Auto-outpainting banner for story: {save_doc.get('story_name_en') or save_doc.get('story_id')}")
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(poster_url) as resp:
                        if resp.status == 200:
                            poster_bytes = await resp.read()
                            
                            # Perform outpainting
                            from ai_outpaint_service import process_outpaint
                            title_pos = save_doc.get("title_position", "left")
                            outpainted_bytes = await process_outpaint(poster_bytes, title_pos)
                            
                            # Optimize and upload widescreen banner
                            uploaded_banner_url = await optimize_and_upload_to_storage(
                                outpainted_bytes, width=1184, height=556, quality=80
                            )
                            if uploaded_banner_url:
                                save_doc["banner_url"] = uploaded_banner_url
                                logger.info(f"Successfully auto-generated and uploaded banner: {uploaded_banner_url}")
            except Exception as e:
                logger.error(f"Failed to auto-outpaint story banner: {e}", exc_info=True)

        save_doc["updated_via"] = "mini_app_admin"
        arya_db = app.state.db
        await arya_db.save_story(save_doc)

        # Sync to manual custom banners if show_in_banners is set
        if show_in_banners is not None:
            if show_in_banners:
                existing_banner = await arya_db.db.mini_app_banners.find_one({"target_link": save_doc["story_id"]})
                banner_img = save_doc.get("banner_url") or save_doc.get("poster_url")
                if not existing_banner:
                    banners_count = await arya_db.db.mini_app_banners.count_documents({})
                    banner_doc = {
                        "image_url": banner_img,
                        "target_link": save_doc["story_id"],
                        "order": banners_count,
                        "button_text": "Shop Now",
                        "button_position": save_doc.get("title_position") or "left",
                        "button_bg": "#000000",
                        "button_color": "#ffffff",
                        "title": save_doc.get("story_name_en") or "",
                        "badge": "TRENDING",
                    }
                    await arya_db.db.mini_app_banners.insert_one(banner_doc)
                    logger.info(f"Auto-pinned story {save_doc['story_id']} to manual banners.")
                else:
                    await arya_db.db.mini_app_banners.update_one(
                        {"target_link": save_doc["story_id"]},
                        {"$set": {
                            "image_url": banner_img,
                            "button_position": save_doc.get("title_position") or "left",
                            "title": save_doc.get("story_name_en") or "",
                        }}
                    )
            else:
                await arya_db.db.mini_app_banners.delete_many({"target_link": save_doc["story_id"]})
                logger.info(f"Auto-removed story {save_doc['story_id']} from manual banners.")
        return {"success": True, "message": "Story saved successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving story: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@api_router.post("/admin/stories/adjust-prices")
async def adjust_all_story_prices(payload: dict):
    """Adjusts the price of all premium stories by a given positive or negative offset."""
    from AryaPremium.config import Config
    try:
        telegram_id = str(payload.get("telegram_id", ""))
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized as Admin")
        
        amount = payload.get("amount")
        if amount is None:
            raise HTTPException(status_code=400, detail="amount parameter is required")
        try:
            amount = int(amount)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="amount must be a valid integer")
        
        arya_db = app.state.db
        result = await arya_db.stories.update_many(
            {},
            {"$inc": {"price": amount}}
        )
        logger.info(f"Admin {telegram_id} adjusted all story prices by {amount}. Modified {result.modified_count} documents.")
        return {
            "success": True, 
            "message": f"Successfully updated story prices by {amount}", 
            "modified_count": result.modified_count
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error bulk adjusting prices: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# UPLOAD ADMIN IMAGE (POST /admin/upload-image)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@api_router.post("/admin/upload-image")
async def upload_admin_image(telegram_id: str = Form(...), file: UploadFile = File(...)):
    from AryaPremium.config import Config
    import aiohttp
    import io
    from PIL import Image

    user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
    if not is_admin(str(telegram_id)):
        raise HTTPException(status_code=403, detail="Not authorized")

    try:
        contents = await file.read()
        
        import asyncio
        import os
        import uuid
        from decouple import config
        
        r2_account_id = config("R2_ACCOUNT_ID", default="")
        r2_access_key = config("R2_ACCESS_KEY_ID", default="")
        r2_secret_key = config("R2_SECRET_ACCESS_KEY", default="")
        r2_bucket = config("R2_BUCKET_NAME", default="arya-images")
        r2_domain = config("R2_CUSTOM_DOMAIN", default="")
        
        def process_and_upload(data_bytes):
            # Compress image
            img = Image.open(io.BytesIO(data_bytes))
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.thumbnail((800, 800))
            output = io.BytesIO()
            img.save(output, format="JPEG", quality=75, optimize=True)
            compressed_bytes = output.getvalue()
            
            url = ""
            if r2_account_id and r2_access_key and r2_secret_key and r2_bucket:
                import boto3
                try:
                    s3 = boto3.client(
                        "s3",
                        endpoint_url=f"https://{r2_account_id}.r2.cloudflarestorage.com",
                        aws_access_key_id=r2_access_key,
                        aws_secret_access_key=r2_secret_key,
                        region_name="auto"
                    )
                    filename = f"{uuid.uuid4().hex}.jpg"
                    s3.put_object(
                        Bucket=r2_bucket,
                        Key=filename,
                        Body=compressed_bytes,
                        ContentType="image/jpeg"
                    )
                    if r2_domain:
                        domain = r2_domain.strip("/")
                        if not domain.startswith("http"):
                            domain = "https://" + domain
                        url = f"{domain}/{filename}"
                    else:
                        url = f"https://{r2_account_id}.r2.cloudflarestorage.com/{r2_bucket}/{filename}"
                except Exception as e:
                    logger.error(f"Cloudflare R2 upload failed: {e}")
            
            return compressed_bytes, url
            
        img_bytes, poster_url = await asyncio.to_thread(process_and_upload, contents)
        
        file_id = ""
        
        # Fallback to Catbox
        if not poster_url:
            try:
                async with aiohttp.ClientSession() as session:
                    form = aiohttp.FormData()
                    form.add_field("reqtype", "fileupload")
                    form.add_field("fileToUpload", img_bytes, filename="poster.jpg", content_type="image/jpeg")
                    async with session.post("https://catbox.moe/user/api.php", data=form, timeout=6) as resp:
                        if resp.status == 200:
                            poster_url = await resp.text()
            except Exception as e:
                logger.error(f"Catbox upload failed: {e}")
                poster_url = ""
        
        # Upload to Telegram to get file_id
        token = getattr(Config, "MGMT_BOT_TOKEN", None) or getattr(Config, "BOT_TOKEN", None)
        if token:
            try:
                async with aiohttp.ClientSession() as session:
                    form = aiohttp.FormData()
                    form.add_field("chat_id", str(user_id_int))
                    form.add_field("photo", img_bytes, filename="poster.jpg", content_type="image/jpeg")
                    form.add_field("caption", f"Auto-uploaded poster from Mini App Admin")
                    async with session.post(f"https://api.telegram.org/bot{token}/sendPhoto", data=form, timeout=6) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            photos = data.get("result", {}).get("photo", [])
                            if photos:
                                file_id = photos[-1]["file_id"]
            except Exception as e:
                logger.error(f"Telegram upload failed: {e}")
        
        return {"success": True, "poster_url": poster_url, "file_id": file_id}
    except Exception as e:
        logger.error(f"Image upload error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# DELETE /admin/story/{story_id}
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@api_router.delete("/admin/story/{story_id}")
async def delete_admin_story(story_id: str, telegram_id: str):
    """Deletes a story."""
    from AryaPremium.config import Config
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
            
        arya_db = app.state.db
        await arya_db.delete_story(story_id)
        return {"success": True, "message": "Story deleted successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# SUPPORT MANAGEMENT
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@api_router.get("/admin/support")
async def get_admin_support(telegram_id: str):
    from AryaPremium.config import Config
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
            
        arya_db = app.state.db
        cursor = arya_db.db.premium_feedback.find({
            "status": {"$ne": "resolved"},
            "text": {"$not": {"$regex": "^\\[REQUEST\\]", "$options": "i"}}
        }).sort("created_at", -1).limit(100)
        tickets = []
        async for doc in cursor:
            tickets.append({
                "id": str(doc["_id"]),
                "user_id": doc.get("user_id"),
                "username": doc.get("username", "Unknown"),
                "first_name": doc.get("user_name", doc.get("first_name", "Unknown")),
                "text": doc.get("text", ""),
                "type": doc.get("type", "text"),  # text, photo, video, audio, document
                "file_id": doc.get("file_id", ""),  # Telegram file_id for media
                "file_url": doc.get("file_url", ""),  # CDN URL if available
                "status": doc.get("status", "open"),
                "date": doc.get("created_at", datetime.now(timezone.utc)).isoformat() if isinstance(doc.get("created_at"), datetime) else str(doc.get("created_at", ""))
            })
        return {"success": True, "data": tickets}
    except Exception as e:
        logger.error(f"Error fetching support: {e}")
        return {"success": False, "data": []}

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# ADMIN STORY REQUESTS MANAGEMENT
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@api_router.get("/admin/requests")
async def get_admin_requests(telegram_id: str):
    """Fetch all story requests (type=REQUEST) for admin management."""
    from AryaPremium.config import Config
    from bson.objectid import ObjectId
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
        arya_db = app.state.db
        cursor = arya_db.db.premium_feedback.find(
            {"text": {"$regex": "^\\[REQUEST\\]", "$options": "i"}}
        ).sort("created_at", -1).limit(200)
        items = []
        async for doc in cursor:
            raw_text = doc.get("text", "")
            # Strip [REQUEST] prefix for display
            display_text = raw_text.replace("[REQUEST]", "").replace("[request]", "").strip()
            items.append({
                "id": str(doc["_id"]),
                "user_id": doc.get("user_id"),
                "username": doc.get("username", ""),
                "first_name": doc.get("user_name", doc.get("first_name", "Unknown")),
                "text": display_text,
                "raw_text": raw_text,
                "file_url": doc.get("file_url", ""),
                "status": doc.get("status", "open"),
                "created_at": doc.get("created_at", datetime.now(timezone.utc)).isoformat() if isinstance(doc.get("created_at"), datetime) else str(doc.get("created_at", ""))
            })
        return {"success": True, "data": items}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching admin requests: {e}")
        return {"success": False, "data": []}

class RequestStatusUpdate(BaseModel):
    telegram_id: str
    status: str  # "open" | "in_progress" | "completed" | "rejected"
    reply_text: Optional[str] = None

@api_router.patch("/admin/requests/{request_id}")
async def update_request_status(request_id: str, data: RequestStatusUpdate):
    """Update the status of a story request, optionally notifying the user via Telegram."""
    from AryaPremium.config import Config
    from bson.objectid import ObjectId
    import aiohttp
    try:
        user_id_int = int(data.telegram_id) if data.telegram_id.isdigit() else data.telegram_id
        if not is_admin(str(data.telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
        arya_db = app.state.db
        doc = await arya_db.db.premium_feedback.find_one({"_id": ObjectId(request_id)})
        if not doc:
            raise HTTPException(status_code=404, detail="Request not found")
        update_fields = {"status": data.status}
        if data.reply_text:
            update_fields["admin_reply"] = data.reply_text
        await arya_db.db.premium_feedback.update_one(
            {"_id": ObjectId(request_id)},
            {"$set": update_fields}
        )
        # Optionally notify user
        if data.reply_text:
            token = None
            try:
                user_doc = await arya_db.db.users.find_one({"id": int(doc["user_id"])})
                if user_doc and user_doc.get("bot_ids"):
                    for bid in user_doc["bot_ids"]:
                        bot_doc = await arya_db.db.premium_bots.find_one({"$or": [{"id": int(bid)}, {"bot_id": int(bid)}]})
                        if bot_doc and bot_doc.get("token"):
                            token = bot_doc["token"]
                            break
            except Exception as e:
                logger.error(f"Failed to resolve seller bot token: {e}")
            
            if not token:
                try:
                    bot_doc = await arya_db.db.premium_bots.find_one({"token": {"$exists": True, "$ne": ""}})
                    if bot_doc:
                        token = bot_doc["token"]
                except Exception:
                    pass
            
            if not token:
                token = getattr(Config, "MGMT_BOT_TOKEN", None) or getattr(Config, "BOT_TOKEN", None)
            
            if token and doc.get("user_id"):
                status_label = {"open": "[OPEN]", "in_progress": "[IN PROGRESS]", "completed": "[COMPLETED]", "rejected": "[REJECTED]"}.get(data.status, "[UPDATE]")
                try:
                    async with aiohttp.ClientSession() as session:
                        await session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                            "chat_id": doc["user_id"],
                            "text": f"<b>{status_label} Story Request Update</b>\n\n<b>Status:</b> {data.status.replace('_',' ').title()}\n\n{data.reply_text}",
                            "parse_mode": "HTML"
                        }, timeout=5)
                except Exception as e:
                    logger.warning(f"Failed to notify user: {e}")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating request: {e}")
        raise HTTPException(status_code=500, detail="Failed to update request")

class SupportReply(BaseModel):
    telegram_id: str
    ticket_id: str
    reply_text: str
    reply_media_file_id: Optional[str] = None  # Telegram file_id to forward as media
    reply_media_type: Optional[str] = None  # photo, video, audio, document

@api_router.post("/admin/support/reply")
async def reply_support(data: SupportReply):
    from AryaPremium.config import Config
    from bson.objectid import ObjectId
    import aiohttp
    try:
        user_id_int = int(data.telegram_id) if data.telegram_id.isdigit() else data.telegram_id
        if not is_admin(str(data.telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
            
        arya_db = app.state.db
        ticket = await arya_db.db.premium_feedback.find_one({"_id": ObjectId(data.ticket_id)})
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")
        
        # Determine which bot token to use (seller bot preferred, fallback to config)
        token = None
        try:
            user_doc = await arya_db.db.users.find_one({"id": int(ticket["user_id"])})
            if user_doc and user_doc.get("bot_ids"):
                for bid in user_doc["bot_ids"]:
                    bot_doc = await arya_db.db.premium_bots.find_one({"$or": [{"id": int(bid)}, {"bot_id": int(bid)}]})
                    if bot_doc and bot_doc.get("token"):
                        token = bot_doc["token"]
                        break
        except Exception as e:
            logger.error(f"Failed to resolve seller bot token: {e}")
            
        if not token:
            try:
                bot_doc = await arya_db.db.premium_bots.find_one({"token": {"$exists": True, "$ne": ""}})
                if bot_doc:
                    token = bot_doc["token"]
            except Exception:
                pass

        if not token:
            token = getattr(Config, "MGMT_BOT_TOKEN", None) or getattr(Config, "BOT_TOKEN", None)

        if token:
            async with aiohttp.ClientSession() as session:
                chat_id = ticket["user_id"]
                # Send text reply
                if data.reply_text:
                    await session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                        "chat_id": chat_id,
                        "text": f"<b>Admin Reply:</b>\n\n{data.reply_text}",
                        "parse_mode": "HTML"
                    })
                # Forward media if provided
                if data.reply_media_file_id and data.reply_media_type:
                    method_map = {
                        "photo": "sendPhoto", "video": "sendVideo",
                        "audio": "sendAudio", "document": "sendDocument"
                    }
                    method = method_map.get(data.reply_media_type, "sendDocument")
                    field_map = {
                        "photo": "photo", "video": "video",
                        "audio": "audio", "document": "document"
                    }
                    field = field_map.get(data.reply_media_type, "document")
                    await session.post(f"https://api.telegram.org/bot{token}/{method}", json={
                        "chat_id": chat_id,
                        field: data.reply_media_file_id
                    })
        
        # Mark resolved
        await arya_db.db.premium_feedback.update_one(
            {"_id": ObjectId(data.ticket_id)},
            {"$set": {"status": "resolved", "admin_reply": data.reply_text}}
        )
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# BANNERS MANAGEMENT
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@api_router.get("/banners")
async def get_banners():
    """
    Returns up to 10 hero banners:
     - 1 auto: most-purchased story (trending)
     - 1 auto: newest story added
     - up to 8 manual: from mini_app_banners collection
    """
    try:
        arya_db = app.state.db
        from bson.objectid import ObjectId
        result = []

        # Auto: Trending (most purchased story)
        try:
            top_order = await arya_db.db.orders.find_one(
                {"status": {"$in": ["paid", "delivered"]}},
                sort=[("created_at", -1)]
            )
            if top_order:
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
                                "image": fmt["banner"] or fmt["poster"],
                                "title": fmt["title"],
                                "subtitle": "ðŸ”¥ Trending Now",
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
                        "image": fmt["banner"] or fmt["poster"],
                        "title": fmt["title"],
                        "subtitle": "âœ¨ New Release",
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
                image_url = b.get("image_url") or ""
                # If image_url isn't an http link, assume it's a file ID
                if image_url and not image_url.startswith("http"):
                    image_url = f"/api/tg-image?file_id={image_url}"
                result.append({
                    "id": bid,
                    "type": "manual",
                    "story_id": b.get("target_link"),
                    "image": image_url,
                    "title": b.get("title", ""),
                    "subtitle": b.get("subtitle", ""),
                    "badge": b.get("badge", ""),
                    "button_text": b.get("button_text", ""),
                    "button_link": b.get("target_link", ""),
                    "button_position": b.get("button_position", "left"),
                    "button_bg": b.get("button_bg", "#000000"),
                    "button_color": b.get("button_color", "#ffffff"),
                })
        except Exception as e:
            logger.warning(f"Manual banners error: {e}")

        return {"success": True, "data": result[:10]}
    except Exception as e:
        logger.error(f"/banners error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@api_router.get("/popular")
async def get_popular():
    """
    Returns the top 10 most purchased stories.
    """
    try:
        arya_db = app.state.db
        from bson.objectid import ObjectId
        
        user_counts = []
        order_counts = []
        try:
            pipeline_users = [
                {"$unwind": "$purchases"},
                {"$group": {"_id": "$purchases", "count": {"$sum": 1}}}
            ]
            user_counts = await arya_db.db.users.aggregate(pipeline_users).to_list(None)
        except Exception as e:
            logger.warning(f"Failed to aggregate users: {e}")
            
        try:
            pipeline_orders = [
                {"$match": {"status": "paid"}},
                {"$unwind": "$story_ids"},
                {"$group": {"_id": "$story_ids", "count": {"$sum": 1}}}
            ]
            order_counts = await arya_db.db.orders.aggregate(pipeline_orders).to_list(None)
        except Exception as e:
            logger.warning(f"Failed to aggregate orders: {e}")
            
        # Combine counts
        counts_map = {}
        for item in user_counts:
            sid = str(item.get("_id"))
            counts_map[sid] = counts_map.get(sid, 0) + item.get("count", 0)
            
        for item in order_counts:
            sid = str(item.get("_id"))
            counts_map[sid] = counts_map.get(sid, 0) + item.get("count", 0)
            
        sorted_counts = sorted(counts_map.items(), key=lambda x: x[1], reverse=True)[:9]
        
        result = []
        for sid, count in sorted_counts:
            if not sid or sid == "None": continue
            try:
                story = await arya_db.db.premium_stories.find_one({"_id": ObjectId(sid)})
                if story:
                    fmt = _format_story(story)
                    if fmt:
                        fmt["buy_count"] = count
                        result.append(fmt)
            except Exception as e:
                pass
                
        # If still empty for some reason, fallback to hardcoded top recent stories
        if not result:
            cursor = arya_db.db.premium_stories.find({"status": "active"}).sort("_id", -1).limit(6)
            async for s in cursor:
                fmt = _format_story(s)
                if fmt:
                    fmt["buy_count"] = 1
                    result.append(fmt)
                    
        return {"success": True, "data": result}
    except Exception as e:
        logger.error(f"/popular error: {e}")
        return {"success": False, "data": []}


@api_router.get("/admin/banners")
async def get_admin_banners(telegram_id: str):
    from AryaPremium.config import Config
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
        arya_db = app.state.db
        cursor = arya_db.db.mini_app_banners.find({}).sort("order", 1)
        banners = []
        async for doc in cursor:
            banners.append({
                "id": str(doc["_id"]),
                "image_url": doc.get("image_url", ""),
                "target_link": doc.get("target_link", ""),
                "order": doc.get("order", 0),
                "button_text": doc.get("button_text", ""),
                "button_position": doc.get("button_position", "left"),
                "button_bg": doc.get("button_bg", "#000000"),
                "button_color": doc.get("button_color", "#ffffff"),
            })
        return {"success": True, "data": banners}
    except Exception as e:
        logger.error(f"Error fetching banners: {e}")
        return {"success": False, "data": []}

class BannerUpdate(BaseModel):
    telegram_id: str
    id: Optional[str] = None
    image_url: str
    target_link: str
    order: int
    button_text: Optional[str] = None
    button_position: Optional[str] = None
    button_bg: Optional[str] = None
    button_color: Optional[str] = None

@api_router.post("/admin/banner")
async def save_admin_banner(data: BannerUpdate):
    from AryaPremium.config import Config
    from bson.objectid import ObjectId
    try:
        user_id_int = int(data.telegram_id) if data.telegram_id.isdigit() else data.telegram_id
        if not is_admin(str(data.telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
        arya_db = app.state.db
        doc = {
            "image_url": data.image_url,
            "target_link": data.target_link,
            "order": data.order,
            "button_text": data.button_text,
            "button_position": data.button_position,
            "button_bg": data.button_bg,
            "button_color": data.button_color,
        }
        if data.id and data.id != "new":
            await arya_db.db.mini_app_banners.update_one({"_id": ObjectId(data.id)}, {"$set": doc})
        else:
            await arya_db.db.mini_app_banners.insert_one(doc)

        # Sync image_url to premium_stories if target_link points to a story
        if data.target_link:
            story = await arya_db.db.premium_stories.find_one({"story_id": data.target_link})
            if story:
                await arya_db.db.premium_stories.update_one(
                    {"story_id": data.target_link},
                    {"$set": {"banner_url": data.image_url}}
                )

        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@api_router.delete("/admin/banner")
async def delete_admin_banner(telegram_id: str, banner_id: str):
    from AryaPremium.config import Config
    from bson.objectid import ObjectId
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
        arya_db = app.state.db
        await arya_db.db.mini_app_banners.delete_one({"_id": ObjectId(banner_id)})
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# BUYERS MANAGEMENT
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@api_router.get("/admin/buyers")
async def get_admin_buyers(telegram_id: str):
    from AryaPremium.config import Config
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
        arya_db = app.state.db
        
        # Auto-expire pending/processing checkouts and orders (run in background to avoid blocking buyers list fetch)
        from datetime import timedelta
        expiry_24h = datetime.now(timezone.utc) - timedelta(hours=24)
        expiry_7m = datetime.now(timezone.utc) - timedelta(minutes=7)
        
        async def run_cleanup():
            try:
                # 1. Clear checkouts/orders in pending/processing/waiting states older than 24 hours
                await arya_db.db.premium_checkout.update_many(
                    {
                        "status": {"$in": ["pending_gateway", "pending", "waiting_screenshot", "processing"]},
                        "created_at": {"$lt": expiry_24h}
                    },
                    {"$set": {"status": "failed"}}
                )
                await arya_db.db.orders.update_many(
                    {
                        "status": {"$in": ["pending", "processing"]},
                        "created_at": {"$lt": expiry_24h}
                    },
                    {"$set": {"status": "failed"}}
                )
                
                # 2. Clear fast-expiry checkouts/orders older than 7 minutes
                await arya_db.db.premium_checkout.update_many(
                    {
                        "status": {"$in": ["pending_gateway", "pending"]},
                        "created_at": {"$lt": expiry_7m}
                    },
                    {"$set": {"status": "failed"}}
                )
                await arya_db.db.orders.update_many(
                    {
                        "status": "pending",
                        "created_at": {"$lt": expiry_7m}
                    },
                    {"$set": {"status": "failed"}}
                )
            except Exception as e:
                logger.error(f"Failed to auto-expire checkouts in background: {e}")
                
        asyncio.create_task(run_cleanup())

        # Fetch all recent checkouts (Bot) and orders (MiniApp) and merge by User
        buyers_map = {}
        
        # Pre-fetch all premium stories once to prevent N+1 query loops
        stories_list = await arya_db.db.premium_stories.find({}, {"story_id": 1, "story_name_en": 1}).to_list(length=10000)
        story_cache_by_id = {}
        story_cache_by_oid = {}
        for s in stories_list:
            sid = s.get("story_id")
            if sid:
                story_cache_by_id[str(sid)] = s
            oid = s.get("_id")
            if oid:
                story_cache_by_oid[str(oid)] = s
        
        # 1. Fetch Bot checkouts
        checkouts = await arya_db.db.premium_checkout.find({}).sort("_id", -1).limit(100).to_list(length=100)
        
        # Pre-fetch users and stories to optimize DB calls
        uids = list(set([c.get("user_id") for c in checkouts] + [o.get("user_id") for o in await arya_db.db.orders.find({}).sort("_id", -1).limit(100).to_list(length=100)]))
        user_docs_list = await arya_db.db.users.find({"id": {"$in": [uid for uid in uids if isinstance(uid, int) or (isinstance(uid, str) and uid.isdigit())]}}).to_list(length=500)
        user_cache = {u.get("id"): u for u in user_docs_list}
        
        for c in checkouts:
            uid = c.get("user_id")
            if not uid: continue
            try: uid = int(uid)
            except: pass
            
            if uid not in buyers_map:
                u = user_cache.get(uid)
                if u:
                    _ufn = (u.get("first_name") or "").strip()
                    _uln = (u.get("last_name") or "").strip()
                    _ufull = " ".join(filter(None, [_ufn, _uln])) or u.get("username", "") or "User"
                    _uname = u.get("username", "Unknown")
                    _photo = u.get("photo_url", "")
                else:
                    _uname = c.get("username") or "Unknown"
                    _ufull = _uname if _uname != "Unknown" else f"User {uid}"
                    _photo = ""
                    try:
                        asyncio.create_task(arya_db.db.users.update_one(
                            {"id": int(uid)},
                            {"$setOnInsert": {
                                "id": int(uid),
                                "username": _uname,
                                "first_name": _uname,
                                "joined_date": datetime.now(timezone.utc),
                                "purchases": [],
                                "language": "en"
                            }},
                            upsert=True
                        ))
                    except:
                        pass
                
                buyers_map[uid] = {
                    "user_id": uid,
                    "username": _uname,
                    "first_name": _ufull,
                    "photo_url": _photo,
                    "payments": [],
                    "total_amt": 0,
                    "date": c.get("created_at", datetime.now(timezone.utc)).isoformat() if isinstance(c.get("created_at"), datetime) else str(c.get("created_at", "")),
                    "source": "bot"
                }
            
            story_id = c.get("story_id")
            story = None
            if story_id:
                story = story_cache_by_oid.get(str(story_id)) or story_cache_by_id.get(str(story_id))
            sname = story.get("story_name_en", str(story_id)) if story else (str(story_id) if story_id else "Deleted Story")
            amt = c.get("amount", 0)
            try: amt = float(amt)
            except: amt = 0
            
            status_label = {
                "approved": "paid",
                "waiting_screenshot": "pending",
                "rejected": "rejected",
                "pending_gateway": "processing",
            }.get(c.get("status", "unknown"), c.get("status", "unknown").lower())
            
            buyers_map[uid]["total_amt"] += amt
            buyers_map[uid]["payments"].append({
                "story_name": sname,
                "amount": amt,
                "method": c.get("method", "unknown").upper(),
                "status": status_label,
                "date": c.get("created_at", datetime.now(timezone.utc)).isoformat() if isinstance(c.get("created_at"), datetime) else str(c.get("created_at", "")),
                "source": "bot"
            })
            
        # 2. Fetch Mini app orders
        orders = await arya_db.db.orders.find({}).sort("created_at", -1).limit(100).to_list(length=100)
        for doc in orders:
            uid = doc.get("user_id")
            if not uid: continue
            try: uid = int(uid)
            except: pass
            
            if uid not in buyers_map:
                u = user_cache.get(uid)
                if u:
                    _ofn = (u.get("first_name") or doc.get("first_name") or "").strip()
                    _oln = (u.get("last_name") or "").strip()
                    _ofull = " ".join(filter(None, [_ofn, _oln])) or u.get("username", doc.get("username", "")) or "User"
                    _uname = u.get("username", doc.get("username", "Unknown"))
                    _photo = u.get("photo_url", "")
                else:
                    _uname = doc.get("username") or "Unknown"
                    _ofull = _uname if _uname != "Unknown" else f"User {uid}"
                    _photo = ""
                    try:
                        asyncio.create_task(arya_db.db.users.update_one(
                            {"id": int(uid)},
                            {"$setOnInsert": {
                                "id": int(uid),
                                "username": _uname,
                                "first_name": _uname,
                                "joined_date": datetime.now(timezone.utc),
                                "purchases": [],
                                "language": "en"
                            }},
                            upsert=True
                        ))
                    except:
                        pass
                
                buyers_map[uid] = {
                    "user_id": uid,
                    "username": _uname,
                    "first_name": _ofull,
                    "photo_url": _photo,
                    "payments": [],
                    "total_amt": 0,
                    "date": doc.get("created_at", datetime.now(timezone.utc)).isoformat() if isinstance(doc.get("created_at"), datetime) else str(doc.get("created_at", "")),
                    "source": doc.get("source", "miniapp")
                }
            else:
                if buyers_map[uid]["source"] == "bot":
                    buyers_map[uid]["source"] = "both"
                
            story_ids = doc.get("story_ids", [])
            if not story_ids and doc.get("story_id"):
                story_ids = [doc.get("story_id")]
                
            story_names = []
            for sid in story_ids:
                story = story_cache_by_oid.get(str(sid)) or story_cache_by_id.get(str(sid))
                if story:
                    story_names.append(story.get("story_name_en", str(sid)))
                else:
                    story_names.append(str(sid))
            
            date_str = doc.get("created_at", datetime.now(timezone.utc)).isoformat() if isinstance(doc.get("created_at"), datetime) else str(doc.get("created_at", ""))
            amt = doc.get("total_amount", doc.get("total", doc.get("amount", 0)))
            try: amt = float(amt)
            except: amt = 0
            
            buyers_map[uid]["total_amt"] += amt
            buyers_map[uid]["payments"].append({
                "story_name": ", ".join(story_names) if story_names else "App Purchase",
                "amount": amt,
                "method": "RAZORPAY",
                "status": doc.get("status", "unknown").lower(),
                "date": date_str,
                "source": doc.get("source", "miniapp")
            })

        buyers = []
        for uid, data in buyers_map.items():
            payments = data["payments"]
            # Determine user status
            has_paid = any(p["status"] == "paid" for p in payments)
            has_pending_or_processing = any(p["status"] in ["pending", "processing"] for p in payments)
            
            if has_paid:
                user_status = "paid"
                # Only count paid orders amount for paid users
                user_amount = sum(p["amount"] for p in payments if p["status"] == "paid")
            elif has_pending_or_processing:
                first_pending_or_proc = next((p for p in payments if p["status"] in ["pending", "processing"]), None)
                user_status = first_pending_or_proc["status"] if first_pending_or_proc else "pending"
                # Sum pending/processing payments
                user_amount = sum(p["amount"] for p in payments if p["status"] in ["pending", "processing"])
            else:
                user_status = payments[0]["status"] if payments else "failed"
                user_amount = sum(p["amount"] for p in payments)
                
            buyers.append({
                "order_id": f"uid_{uid}",
                "user_id": uid,
                "username": data["username"],
                "first_name": data["first_name"],
                "photo_url": data["photo_url"],
                "amount": user_amount,
                "status": user_status,
                "source": data["source"],
                "payments": sorted(payments, key=lambda x: x["date"], reverse=True),
                "date": data["date"]
            })
            
        buyers.sort(key=lambda x: max([p["date"] for p in x["payments"]] if x["payments"] else [x["date"]]), reverse=True)
            
        return {"success": True, "data": buyers[:200]}
    except Exception as e:
        logger.error(f"Error fetching buyers: {e}")
        return {"success": False, "data": []}


class ManualPurchase(BaseModel):
    telegram_id: str
    user_id: int
    first_name: str
    username: str
    story_id: str
    amount: float

@api_router.post("/admin/manual-purchase")
async def manual_purchase(data: ManualPurchase):
    from AryaPremium.config import Config
    try:
        user_id_int = int(data.telegram_id) if data.telegram_id.isdigit() else data.telegram_id
        if not is_admin(str(data.telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
            
        arya_db = app.state.db
        from bson.objectid import ObjectId
        
        # Verify story
        try:
            story = await arya_db.db.premium_stories.find_one({"_id": ObjectId(data.story_id)})
        except:
            story = await arya_db.db.premium_stories.find_one({"story_id": data.story_id})
            
        if not story:
            raise HTTPException(404, "Story not found")
            
        story_id_str = str(story["_id"])
        
        # Upsert user
        user = await arya_db.db.users.find_one({"id": data.user_id})
        if not user:
            await arya_db.db.users.insert_one({
                "id": data.user_id,
                "first_name": data.first_name,
                "username": data.username,
                "purchases": [story_id_str],
                "joined_date": datetime.now(timezone.utc)
            })
        else:
            await arya_db.db.users.update_one(
                {"id": data.user_id},
                {"$addToSet": {"purchases": story_id_str}}
            )
            
        # Insert Order
        await arya_db.db.orders.insert_one({
            "order_id": f"MANUAL_{data.user_id}_{int(datetime.now().timestamp())}",
            "user_id": data.user_id,
            "username": data.username,
            "first_name": data.first_name,
            "story_ids": [story_id_str],
            "story_names": [story.get("story_name_en", "")],
            "total": data.amount,
            "status": "paid",
            "source": "manual_admin",
            "created_at": datetime.now(timezone.utc)
        })
        
        return {"success": True}
    except Exception as e:
        logger.error(f"Manual purchase error: {e}")
        raise HTTPException(500, detail=str(e))

@api_router.post("/admin/buyers/{user_id}/action")
async def admin_buyer_action(telegram_id: str, user_id: str, payload: dict):
    from AryaPremium.config import Config
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
            
        action = payload.get("action")
        target_uid = int(user_id) if user_id.isdigit() else user_id
        arya_db = app.state.db
        
        if action == "wipe":
            await arya_db.db.users.delete_one({"id": target_uid})
            await arya_db.db.orders.delete_many({"user_id": {"$in": [target_uid, str(target_uid)]}})
            await arya_db.db.premium_checkout.delete_many({"user_id": target_uid})
            return {"success": True, "message": "User data wiped completely."}
            
        elif action == "ban":
            await arya_db.db.users.delete_one({"id": target_uid})
            await arya_db.db.orders.delete_many({"user_id": {"$in": [target_uid, str(target_uid)]}})
            await arya_db.db.premium_checkout.delete_many({"user_id": target_uid})
            await arya_db.db.users.update_one(
                {"id": target_uid},
                {"$set": {"id": target_uid, "banned": True, "ban_reason": "Admin ban via Web App"}},
                upsert=True
            )
            return {"success": True, "message": "User wiped and banned."}
            
        raise HTTPException(status_code=400, detail="Invalid action")
    except Exception as e:
        logger.error(f"Buyer action error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# ANALYTICS TRACKING â€” IP Geolocation + Device + Referrer
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class TrackEvent(BaseModel):
    telegram_id: str
    event_type: str  # "search", "view_story", "add_to_cart", "open_app"
    event_data: dict


def _parse_ua(ua: str) -> dict:
    """Parse User-Agent to extract device_type, browser, os."""
    ua_lower = ua.lower()
    # Device type
    if any(k in ua_lower for k in ("mobile", "android", "iphone", "ipod", "webos", "blackberry")):
        device_type = "tablet" if any(k in ua_lower for k in ("ipad", "tablet")) else "mobile"
    else:
        device_type = "desktop"
    # Browser
    if "telegram" in ua_lower:  browser = "Telegram"
    elif "whatsapp" in ua_lower: browser = "WhatsApp"
    elif "instagram" in ua_lower: browser = "Instagram"
    elif "fban" in ua_lower or "fbav" in ua_lower: browser = "Facebook"
    elif "edg" in ua_lower:   browser = "Edge"
    elif "chrome" in ua_lower: browser = "Chrome"
    elif "firefox" in ua_lower: browser = "Firefox"
    elif "safari" in ua_lower: browser = "Safari"
    elif "opera" in ua_lower or "opr" in ua_lower: browser = "Opera"
    else: browser = "Other"
    # OS
    if "windows" in ua_lower: os_name = "Windows"
    elif "mac os" in ua_lower: os_name = "macOS"
    elif "android" in ua_lower: os_name = "Android"
    elif "ios" in ua_lower or "iphone" in ua_lower or "ipad" in ua_lower: os_name = "iOS"
    elif "linux" in ua_lower: os_name = "Linux"
    else: os_name = "Other"
    return {"device_type": device_type, "browser": browser, "os": os_name}


def _is_private_or_local_ip(ip: str) -> bool:
    if not ip or ip in ("unknown", "127.0.0.1", "::1"):
        return True
    ip = ip.strip().lower()
    if ip.startswith(("192.168.", "10.", "172.16.", "172.17.", "172.18.", "172.19.", "172.2", "172.30.", "172.31.", "fc00:", "fe80:")):
        return True
    if ip.startswith("172."):
        try:
            second = int(ip.split(".")[1])
            if 16 <= second <= 31:
                return True
        except (ValueError, IndexError):
            pass
    return False


def _client_ip_from_request(request: Request) -> str:
    """Best-effort real client IP behind Cloudflare, Vercel, Fly, or other reverse proxies."""
    h = request.headers

    def _first_public_ip(raw: str | None) -> str | None:
        if not raw:
            return None
        for part in raw.split(","):
            ip = part.strip()
            if ip and not _is_private_or_local_ip(ip):
                if ip.startswith("[") and "]" in ip:
                    ip = ip[1 : ip.index("]")]
                if ":" in ip and "." in ip:
                    if ip.lower().startswith("::ffff:"):
                        ip = ip.split(":")[-1]
                if not _is_private_or_local_ip(ip):
                    return ip
        return None

    for key in (
        "cf-connecting-ip",
        "true-client-ip",
        "fastly-client-ip",
        "fly-client-ip",
        "x-real-ip",
    ):
        v = h.get(key)
        if v:
            ip = v.split(",")[0].strip()
            if ip and not _is_private_or_local_ip(ip):
                return ip

    vercel = h.get("x-vercel-forwarded-for")
    ip = _first_public_ip(vercel)
    if ip:
        return ip

    xff = h.get("x-forwarded-for") or h.get("X-Forwarded-For")
    ip = _first_public_ip(xff)
    if ip:
        return ip

    ch = request.client.host if request.client else None
    if ch and not _is_private_or_local_ip(ch):
        return ch.strip()
    return "unknown"


async def _geo_provider(session: aiohttp.ClientSession, url: str, parser) -> dict | None:
    """Query a single geo provider with timeout."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=3),
                               headers={"User-Agent": "AryaBot-Analytics/1.0"}) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
            return parser(data)
    except Exception:
        return None


_geo_lookup_cache: dict[str, tuple[float, dict]] = {}
_GEO_LOOKUP_TTL = 300.0  # seconds — shorter to reduce stale ISP DB mismatches


def _geo_empty() -> dict:
    return {"country": "Unknown", "city": "Unknown", "region": "Unknown", "latitude": None, "longitude": None}


def _norm_geo_token(s: str | None) -> str:
    if not s or not isinstance(s, str):
        return ""
    t = s.strip().lower()
    if t in ("unknown", "null", "none", "-", ""):
        return ""
    return t


def _geo_consensus_from_rows(results: list[dict]) -> dict:
    """When ipwho.is fails: pick region by vote, then city preferring rows that agree on region."""
    if not results:
        return _geo_empty()

    region_votes: dict[str, int] = {}
    for r in results:
        reg = _norm_geo_token(r.get("region"))
        if reg:
            region_votes[reg] = region_votes.get(reg, 0) + 1
    best_region = max(region_votes, key=region_votes.__getitem__) if region_votes else ""

    city_votes: dict[str, int] = {}
    for r in results:
        city = _norm_geo_token(r.get("city"))
        if not city:
            continue
        reg = _norm_geo_token(r.get("region"))
        if best_region and reg and best_region not in reg and reg not in best_region:
            continue
        city_votes[city] = city_votes.get(city, 0) + 1

    if not city_votes:
        for r in results:
            city = _norm_geo_token(r.get("city"))
            if city:
                city_votes[city] = city_votes.get(city, 0) + 1

    best_city = max(city_votes, key=city_votes.__getitem__) if city_votes else "Unknown"

    country = "Unknown"
    for r in results:
        c = (r.get("country") or "").strip()
        if c and _norm_geo_token(c):
            country = c
            break

    display_region = best_region.title() if best_region else "Unknown"
    if display_region == "Unknown":
        for r in results:
            reg = (r.get("region") or "").strip()
            if reg and _norm_geo_token(reg):
                display_region = reg
                break

    return {
        "country": country or "Unknown",
        "city": best_city if best_city != "Unknown" else "Unknown",
        "region": display_region,
        "latitude": None,
        "longitude": None,
    }


def _haversine_km(lat1: float | None, lon1: float | None, lat2: float | None, lon2: float | None) -> float:
    from math import atan2, cos, radians, sin, sqrt

    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return 1e9
    r = 6371.0
    p1, p2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dl = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return r * c


def _regions_loosely_match(a: str | None, b: str | None) -> bool:
    ra, rb = _norm_geo_token(a), _norm_geo_token(b)
    if not ra or not rb:
        return False
    if ra == rb:
        return True
    return ra in rb or rb in ra


def _normalize_geo_row(
    country: str | None,
    region: str | None,
    city: str | None,
    lat: object | None,
    lon: object | None,
) -> dict:
    try:
        lat_f = float(lat) if lat is not None else None
    except (TypeError, ValueError):
        lat_f = None
    try:
        lon_f = float(lon) if lon is not None else None
    except (TypeError, ValueError):
        lon_f = None
    if lat_f is not None and (lat_f < -90 or lat_f > 90):
        lat_f = None
    if lon_f is not None and (lon_f < -180 or lon_f > 180):
        lon_f = None
    return {
        "country": (country or "Unknown").strip() or "Unknown",
        "region": (region or "Unknown").strip() or "Unknown",
        "city": (city or "Unknown").strip() or "Unknown",
        "latitude": lat_f,
        "longitude": lon_f,
    }


def _merge_ipwho_ipapi(iw_raw: dict | None, ia_raw: dict | None) -> dict:
    """Blend ipwho.is + ipapi.co when both succeed — reduces wrong city within same state (ISP edge POP)."""
    iw = None
    if isinstance(iw_raw, dict) and iw_raw.get("success") and _norm_geo_token(iw_raw.get("country")):
        iw = _normalize_geo_row(
            iw_raw.get("country"),
            iw_raw.get("region"),
            iw_raw.get("city"),
            iw_raw.get("latitude"),
            iw_raw.get("longitude"),
        )
    ia = None
    if isinstance(ia_raw, dict) and not ia_raw.get("error") and _norm_geo_token(ia_raw.get("country")):
        ia = _normalize_geo_row(
            ia_raw.get("country_name") or ia_raw.get("country"),
            ia_raw.get("region"),
            ia_raw.get("city"),
            ia_raw.get("latitude"),
            ia_raw.get("longitude"),
        )
    if not iw and not ia:
        return _geo_empty()
    if iw and not ia:
        return iw
    if ia and not iw:
        return ia
    assert iw is not None and ia is not None
    if iw["city"] == ia["city"]:
        return iw
    if _norm_geo_token(iw["country"]) != _norm_geo_token(ia["country"]):
        return iw
    dist = _haversine_km(iw["latitude"], iw["longitude"], ia["latitude"], ia["longitude"])
    if _regions_loosely_match(iw["region"], ia["region"]) and dist < 220 and ia["city"] and ia["city"] != "Unknown":
        if (
            iw["latitude"] is not None
            and ia["latitude"] is not None
            and iw["longitude"] is not None
            and ia["longitude"] is not None
        ):
            lat_m = (iw["latitude"] + ia["latitude"]) / 2
            lon_m = (iw["longitude"] + ia["longitude"]) / 2
        else:
            lat_m = iw["latitude"] if iw["latitude"] is not None else ia["latitude"]
            lon_m = iw["longitude"] if iw["longitude"] is not None else ia["longitude"]
        return {
            "country": iw["country"],
            "region": iw["region"] if len(str(iw["region"])) >= len(str(ia["region"])) else ia["region"],
            "city": ia["city"],
            "latitude": lat_m,
            "longitude": lon_m,
        }
    return iw


def _apply_client_geo_override(geo: dict, ed: dict) -> tuple[dict, str]:
    """Optional labels/coords from the Mini App (recommended when IP geo is wrong for mobile ISPs)."""
    g = dict(geo)
    src = "ip"
    cg = ed.get("client_geo") if isinstance(ed.get("client_geo"), dict) else {}

    def _pick_str(*keys: str) -> str | None:
        for k in keys:
            v = ed.get(k)
            if isinstance(v, str) and _norm_geo_token(v):
                return v.strip()
            v = cg.get(k) if isinstance(cg, dict) else None
            if isinstance(v, str) and _norm_geo_token(v):
                return v.strip()
        return None

    cc = _pick_str("geo_country", "client_country", "country")
    cr = _pick_str("geo_region", "client_region", "region")
    ci = _pick_str("geo_city", "client_city", "city")
    if cc:
        g["country"] = cc
        src = ed.get("geo_source") or "client"
    if cr:
        g["region"] = cr
        src = ed.get("geo_source") or "client"
    if ci:
        g["city"] = ci
        src = ed.get("geo_source") or "client"

    for coord, ed_keys, cg_keys in (
        ("latitude", ("geo_lat", "client_lat", "lat"), ("latitude", "client_lat")),
        ("longitude", ("geo_lon", "client_lng", "lng"), ("longitude", "client_lng")),
    ):
        v = None
        for k in ed_keys:
            if isinstance(ed.get(k), (int, float)):
                v = ed.get(k)
                break
        if v is None and isinstance(cg, dict):
            for k in cg_keys:
                if isinstance(cg.get(k), (int, float)):
                    v = cg.get(k)
                    break
        if v is not None:
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if coord == "latitude" and -90 <= fv <= 90:
                g["latitude"] = fv
                src = "client"
            if coord == "longitude" and -180 <= fv <= 180:
                g["longitude"] = fv
                src = "client"
    return g, src


async def _get_geo(ip: str) -> dict:
    """Parallel ipwho.is + ipapi.co, merged for India/same-region city disagreements; cached per IP."""
    import time as _time

    now = _time.time()
    cached = _geo_lookup_cache.get(ip)
    if cached and (now - cached[0]) < _GEO_LOOKUP_TTL:
        return dict(cached[1])

    if not ip or ip == "unknown" or _is_private_or_local_ip(ip):
        g = _geo_empty()
        _geo_lookup_cache[ip] = (now, g)
        return dict(g)

    async with aiohttp.ClientSession() as session:
        iw_task = _geo_provider(
            session,
            f"https://ipwho.is/{ip}?fields=success,country,region,city,latitude,longitude",
            lambda d: d if isinstance(d, dict) and d.get("success") else None,
        )
        ia_task = _geo_provider(
            session,
            f"https://ipapi.co/{ip}/json/",
            lambda d: d if isinstance(d, dict) and not d.get("error") else None,
        )
        iw_raw, ia_raw = await asyncio.gather(iw_task, ia_task, return_exceptions=True)
        iw_ok = iw_raw if isinstance(iw_raw, dict) else None
        ia_ok = ia_raw if isinstance(ia_raw, dict) else None
        g = _merge_ipwho_ipapi(iw_ok, ia_ok)
        if g["country"] == "Unknown" or not _norm_geo_token(g.get("country")):
            providers = [
                (f"https://freeipapi.com/api/json/{ip}",
                 lambda d: {"country": d.get("countryName"), "city": d.get("cityName"), "region": d.get("regionName")}),
                (f"https://get.geojs.io/v1/ip/geo/{ip}.json",
                 lambda d: {"country": d.get("country"), "city": d.get("city"), "region": d.get("region")}),
            ]
            raw_results = await asyncio.gather(
                *[_geo_provider(session, url, parser) for url, parser in providers],
                return_exceptions=True,
            )
            results = [r for r in raw_results if isinstance(r, dict) and r]
            g = _geo_consensus_from_rows(results) if results else _geo_empty()

    _geo_lookup_cache[ip] = (now, g)
    if len(_geo_lookup_cache) > 6000:
        for k, _ in sorted(_geo_lookup_cache.items(), key=lambda x: x[1][0])[:1500]:
            _geo_lookup_cache.pop(k, None)
    return dict(g)

COUNTRY_CURRENCY_MAP = {
    "India": "INR", "Nepal": "NPR", "Sri Lanka": "LKR", "Bangladesh": "BDT", "Pakistan": "PKR",
    "United Arab Emirates": "AED", "Saudi Arabia": "SAR", "Qatar": "QAR", "Kuwait": "KWD",
    "Bahrain": "BHD", "Oman": "OMR", "Malaysia": "MYR", "Singapore": "SGD", "Thailand": "THB",
    "Indonesia": "IDR", "Philippines": "PHP", "Vietnam": "VND", "United States": "USD",
    "United Kingdom": "GBP", "Canada": "CAD", "Australia": "AUD", "New Zealand": "NZD",
    "Switzerland": "CHF", "Sweden": "SEK", "Norway": "NOK", "Denmark": "DKK", "Japan": "JPY",
    "China": "CNY", "South Korea": "KRW", "South Africa": "ZAR", "Nigeria": "NGN",
    "Kenya": "KES", "Tanzania": "TZS", "Egypt": "EGP",
    "Germany": "EUR", "France": "EUR", "Italy": "EUR", "Spain": "EUR", "Netherlands": "EUR",
    "Belgium": "EUR", "Greece": "EUR", "Portugal": "EUR", "Austria": "EUR", "Finland": "EUR",
    "Ireland": "EUR", "Luxembourg": "EUR", "Malta": "EUR", "Cyprus": "EUR", "Estonia": "EUR",
    "Latvia": "EUR", "Lithuania": "EUR", "Slovakia": "EUR", "Slovenia": "EUR",
}

# Country code → currency (backup for when only ISO code is returned)
COUNTRY_CODE_CURRENCY_MAP = {
    "IN": "INR", "NP": "NPR", "LK": "LKR", "BD": "BDT", "PK": "PKR",
    "AE": "AED", "SA": "SAR", "QA": "QAR", "KW": "KWD", "BH": "BHD", "OM": "OMR",
    "MY": "MYR", "SG": "SGD", "TH": "THB", "ID": "IDR", "PH": "PHP", "VN": "VND",
    "US": "USD", "GB": "GBP", "CA": "CAD", "AU": "AUD", "NZ": "NZD",
    "CH": "CHF", "SE": "SEK", "NO": "NOK", "DK": "DKK", "JP": "JPY",
    "CN": "CNY", "KR": "KRW", "ZA": "ZAR", "NG": "NGN", "KE": "KES",
    "TZ": "TZS", "EG": "EGP",
    "DE": "EUR", "FR": "EUR", "IT": "EUR", "ES": "EUR", "NL": "EUR",
    "BE": "EUR", "GR": "EUR", "PT": "EUR", "AT": "EUR", "FI": "EUR",
    "IE": "EUR", "LU": "EUR", "MT": "EUR", "CY": "EUR", "EE": "EUR",
    "LV": "EUR", "LT": "EUR", "SK": "EUR", "SI": "EUR",
}

# In-memory cache: ip → (currency, country, timestamp)
_ip_currency_cache: dict = {}
_IP_CACHE_TTL = 600  # 10 minutes

@api_router.get("/app-context")
async def get_app_context(request: Request):
    """Auto-detect location and suggested currency for the user.
    Works with VPN IPs too. Results cached 10 mins per IP.
    """
    ip = _client_ip_from_request(request)
    ip = ip.strip()

    # Check cache first
    import time as _time
    now_ts = _time.time()
    cached = _ip_currency_cache.get(ip)
    if cached and (now_ts - cached["ts"]) < _IP_CACHE_TTL:
        return {"ip": ip, "country": cached["country"], "currency": cached["currency"]}

    # Private / local IPs → default INR
    if not ip or ip in ("unknown", "127.0.0.1", "::1") or ip.startswith(("192.168.", "10.", "172.")):
        return {"ip": ip, "country": "India", "currency": "INR"}

    country_name = "Unknown"
    country_code = ""
    currency = "INR"

    # Try providers in priority order — ipinfo.io handles VPNs best
    providers = [
        # ipinfo.io — best VPN detection, returns ISO code
        (
            f"https://ipinfo.io/{ip}/json",
            lambda d: {"name": None, "code": d.get("country", "")}
        ),
        # ipwho.is — full country name
        (
            f"https://ipwho.is/{ip}?fields=success,country,country_code",
            lambda d: {"name": d.get("country"), "code": d.get("country_code", "")} if d.get("success") else None
        ),
        # ipapi.co — good fallback
        (
            f"https://ipapi.co/{ip}/json/",
            lambda d: {"name": d.get("country_name"), "code": d.get("country_code", "")}
        ),
        # freeipapi.com
        (
            f"https://freeipapi.com/api/json/{ip}",
            lambda d: {"name": d.get("countryName"), "code": d.get("countryCode", "")}
        ),
    ]

    async with aiohttp.ClientSession() as session:
        for url, parser in providers:
            try:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=3),
                    headers={"User-Agent": "AryaBot/1.0"}
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        result = parser(data)
                        if result:
                            code = (result.get("code") or "").strip().upper()
                            name = (result.get("name") or "").strip()
                            # Try currency by country name first, then by code
                            if name and name in COUNTRY_CURRENCY_MAP:
                                country_name = name
                                country_code = code
                                currency = COUNTRY_CURRENCY_MAP[name]
                                break
                            elif code and code in COUNTRY_CODE_CURRENCY_MAP:
                                country_code = code
                                country_name = name or code
                                currency = COUNTRY_CODE_CURRENCY_MAP[code]
                                break
                            elif name and name not in ("Unknown", "", "None"):
                                # Country found but not in our currency map → keep INR
                                country_name = name
                                break
            except Exception:
                continue

    # Cache the result
    _ip_currency_cache[ip] = {"country": country_name, "currency": currency, "ts": now_ts}
    # Limit cache size
    if len(_ip_currency_cache) > 5000:
        oldest = sorted(_ip_currency_cache.items(), key=lambda x: x[1]["ts"])[:1000]
        for k, _ in oldest:
            _ip_currency_cache.pop(k, None)

    return {"ip": ip, "country": country_name, "currency": currency}


def _live_event_summary(event_type: str, ed: dict, doc: dict) -> str:
    page = str(doc.get("page") or ed.get("page") or "").strip()
    story = str(doc.get("story_id") or ed.get("story_id") or "").strip()
    ch = str(doc.get("chapter_id") or ed.get("chapter_id") or "").strip()
    if event_type == "page_view":
        return f"Enter · {page}" if page else "App opened"
    if event_type == "view_story":
        return f"Story · {story}" if story else "Story opened"
    if event_type == "session_duration":
        return "Session heartbeat"
    if event_type == "search":
        q = str(ed.get("query") or "").strip()
        return f"Search · {q[:48]}" if q else "Search"
    if event_type.startswith("checkout_"):
        return event_type.replace("_", " ").title()
    if ch:
        return f"{event_type} · ch {ch}"
    return (event_type or "event").replace("_", " ").strip().title()


# Event deduplication cache: dict mapping signature -> timestamp
_dedup_cache: dict[str, float] = {}

def _is_duplicate_event(visitor_id: str, event_type: str, ed: dict) -> bool:
    """
    Returns True if an identical event from the same visitor was logged in the last 4 seconds.
    Supports deduplicating rapid/duplicate clicks, reloads, page_views, and heartbeats.
    """
    import time as _time
    page = str(ed.get("page") or "").strip()
    story_id = str(ed.get("story_id") or "").strip()
    query = str(ed.get("query") or "").strip()
    chapter_id = str(ed.get("chapter_id") or "").strip()
    
    sig = f"{visitor_id}:{event_type}:{page}:{story_id}:{query}:{chapter_id}"
    now = _time.time()
    
    # Clean up stale entries to prevent memory leak
    if len(_dedup_cache) > 10000:
        stale = [k for k, ts in _dedup_cache.items() if (now - ts) > 10]
        for k in stale:
            _dedup_cache.pop(k, None)
            
    last_seen = _dedup_cache.get(sig)
    if last_seen and (now - last_seen) < 4.0:
        return True
        
    _dedup_cache[sig] = now
    return False


@api_router.post("/track")
async def track_event(data: TrackEvent, request: Request):
    """Track a mini-app event with full IP geolocation + device info."""
    try:
        ua  = request.headers.get("user-agent", "")
        # Filter out bots, crawlers, pingers, and uptime checkers to avoid fake analytics data
        ua_lower = ua.lower() if ua else ""
        bot_keywords = (
            "bot", "crawler", "spider", "ping", "uptime", "status", "http", "curl", 
            "wget", "python", "node", "axios", "fetch", "headless", "selenium", 
            "puppeteer", "playwright", "scrape", "scan", "checker"
        )
        if any(k in ua_lower for k in bot_keywords):
            is_real_client = any(ok in ua_lower for ok in ("mozilla", "chrome", "safari", "firefox", "telegram", "whatsapp", "instagram", "facebook"))
            is_explicit_bot = any(bk in ua_lower for bk in ("googlebot", "bingbot", "yandexbot", "ahrefsbot", "semrushbot", "crawler", "spider", "bot"))
            if is_explicit_bot or not is_real_client:
                # Return success but do not log in the database
                return {"success": True}

        user_id_int = int(data.telegram_id) if data.telegram_id.isdigit() else data.telegram_id
        arya_db = app.state.db
        ed = data.event_data or {}

        # Extract client IP (Cloudflare / Vercel / Fly / X-Forwarded-For)
        ip = _client_ip_from_request(request)
        ref = request.headers.get("referer") or ed.get("referrer")
        
        sess_id = ed.get("session_id")
        fp_id = ed.get("fingerprint_id")
        
        visitor_id = ""
        if isinstance(user_id_int, int) and user_id_int > 0:
            visitor_id = f"user_{user_id_int}"
        elif sess_id:
            visitor_id = f"sess_{sess_id}"
        elif fp_id:
            visitor_id = f"fp_{fp_id}"
        else:
            visitor_id = f"ip_{ip or 'unknown'}"
            
        if _is_duplicate_event(visitor_id, data.event_type, ed):
            return {"success": True}

        # â”€â”€ Register / Update User in db.users â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        user_data = ed.get("user_data")
        if user_id_int > 0 and isinstance(user_data, dict):
            await arya_db.db.users.update_one(
                {"id": user_id_int},
                {"$set": {
                    "first_name": user_data.get("first_name", ""),
                    "last_name": user_data.get("last_name", ""),
                    "username": user_data.get("username", ""),
                    "photo_url": user_data.get("photo_url", ""),
                    "last_active": datetime.now(timezone.utc)
                },
                "$setOnInsert": {
                    "joined_date": datetime.now(timezone.utc),
                    "purchases": [],
                    "language": "en"
                }},
                upsert=True
            )

        # ── Parse UA & Standardize ────────────────────────────────
        ua_info = _parse_ua(ua)
        device_type = ed.get("client_device_type")
        browser = ed.get("client_browser")
        os_name = ed.get("client_os")
        
        if not device_type or device_type not in ("mobile", "tablet", "desktop"):
            device_type = ua_info["device_type"]
        if not browser or browser in ("Unknown", "Other", ""):
            browser = ua_info["browser"]
        if not os_name or os_name in ("Unknown", "Other", ""):
            os_name = ua_info["os"]
            
        if browser:
            b_low = browser.lower()
            if "telegram" in b_low: browser = "Telegram"
            elif "whatsapp" in b_low: browser = "WhatsApp"
            elif "instagram" in b_low: browser = "Instagram"
            elif "facebook" in b_low or "fban" in b_low or "fbav" in b_low: browser = "Facebook"
            elif "chrome" in b_low: browser = "Chrome"
            elif "safari" in b_low: browser = "Safari"
            elif "firefox" in b_low: browser = "Firefox"
            elif "edge" in b_low or "edg" in b_low: browser = "Edge"
            elif "opera" in b_low or "opr" in b_low: browser = "Opera"
            else: browser = browser.title()
            
        if os_name:
            o_low = os_name.lower()
            if "windows" in o_low: os_name = "Windows"
            elif "mac" in o_low or "macos" in o_low: os_name = "macOS"
            elif "android" in o_low: os_name = "Android"
            elif "ios" in o_low or "iphone" in o_low or "ipad" in o_low: os_name = "iOS"
            elif "linux" in o_low: os_name = "Linux"
            else: os_name = os_name.title()

        # ── Referrer source ────────────────────────────────────
        def _ref_source(r, u):
            if not r:
                if "telegram" in u.lower(): return "Telegram"
                return "Direct"
            rl = r.lower()
            if "t.me" in rl or "telegram" in rl: return "Telegram"
            if "whatsapp" in rl: return "WhatsApp"
            if "instagram" in rl: return "Instagram"
            if "facebook" in rl or "fb.com" in rl: return "Facebook"
            if "google" in rl: return "Google"
            return "Web"
        referrer_source = _ref_source(ref, ua)

        # ── Geolocation (async — don't block if slow) ─────────
        try:
            geo = await asyncio.wait_for(_get_geo(ip), timeout=5)
        except asyncio.TimeoutError:
            geo = _geo_empty()

        geo, geo_source = _apply_client_geo_override(geo, ed)

        # ── Vercel Edge Geo Headers Override (Highly Accurate) ──
        v_city = request.headers.get("x-vercel-ip-city")
        v_region = request.headers.get("x-vercel-ip-country-region")
        v_country = request.headers.get("x-vercel-ip-country")
        
        if v_city and _norm_geo_token(v_city):
            geo["city"] = urllib.parse.unquote(v_city) if "%" in v_city else v_city
            geo_source = "vercel_edge"
        if v_region and _norm_geo_token(v_region):
            geo["region"] = urllib.parse.unquote(v_region) if "%" in v_region else v_region
        if v_country and _norm_geo_token(v_country):
            geo["country"] = v_country

        map_lat = geo.get("latitude")
        map_lng = geo.get("longitude")
        v_lat = request.headers.get("x-vercel-ip-latitude")
        v_lng = request.headers.get("x-vercel-ip-longitude")
        if v_lat and v_lng:
            try:
                map_lat, map_lng = float(v_lat), float(v_lng)
            except ValueError: pass

        if isinstance(ed.get("lat"), (int, float)):
            map_lat = float(ed["lat"])
        if isinstance(ed.get("lng"), (int, float)):
            map_lng = float(ed["lng"])

        doc: dict = {
            "user_id":  user_id_int,
            "type":     data.event_type,
            "data":     ed,
            "ip":       ip,
            "country":  geo["country"],
            "city":     geo["city"],
            "region":   geo["region"],
            "geo_source": geo_source,
            "device":   device_type,
            "browser":  browser,
            "os":       os_name,
            "referrer": referrer_source,
            "timestamp": datetime.now(timezone.utc),
        }
        if map_lat is not None and -90 <= map_lat <= 90:
            doc["map_lat"] = map_lat
        if map_lng is not None and -180 <= map_lng <= 180:
            doc["map_lng"] = map_lng
        for k in (
            "timezone", "language", "isp", "lat", "lng", "screen_w", "screen_h", "color_scheme",
            "connection_type", "telegram_premium", "telegram_lang", "story_id", "page", "genre",
            "scroll_depth", "duration_ms", "load_ms", "api_ms", "error_text", "network_type",
            "chapter_id", "episode_id", "click_target", "element", "button_id", "utm_source", "utm_campaign",
            "session_id", "fingerprint_id",
        ):
            if k in ed and ed[k] is not None:
                doc[k] = ed[k]
        if isinstance(ed.get("nav_path"), list):
            doc["nav_path"] = ed["nav_path"]

        if data.event_type == "session_duration":
            if sess_id:
                await arya_db.db.mini_app_analytics.update_one(
                    {"session_id": sess_id, "type": "session_duration"},
                    {
                        "$set": {
                            "data.duration": ed.get("duration", 0),
                            "duration_ms": ed.get("duration_ms", ed.get("duration", 0) * 1000),
                            "timestamp": datetime.now(timezone.utc)
                        },
                        "$setOnInsert": {
                            "user_id": user_id_int,
                            "ip": ip,
                            "country": geo["country"],
                            "city": geo["city"],
                            "region": geo["region"],
                            "geo_source": geo_source,
                            "device": device_type,
                            "browser": browser,
                            "os": os_name,
                            "referrer": referrer_source,
                            "fingerprint_id": fp_id
                        }
                    },
                    upsert=True
                )
                return {"success": True}

        ins = await arya_db.db.mini_app_analytics.insert_one(doc)
        try:
            allowed_broadcasts = (
                "page_view", "view_story", "search", "login", "signup", 
                "play_audio", "share", "premium_action",
                "checkout_view", "checkout_pay_start", "checkout_success", "checkout_error"
            )
            if data.event_type in allowed_broadcasts:
                from arya_enterprise_analytics import hub as _analytics_hub
                asyncio.create_task(
                    _analytics_hub.broadcast(
                        {
                            "channel": "live",
                            "id": str(ins.inserted_id),
                            "type": data.event_type,
                            "summary": _live_event_summary(data.event_type, ed, doc),
                            "user_id": user_id_int,
                            "country": geo.get("country"),
                            "city": geo.get("city"),
                            "region": geo.get("region"),
                            "geo_source": geo_source,
                            "lat": doc.get("map_lat"),
                            "lng": doc.get("map_lng"),
                            "device": device_type,
                            "browser": browser,
                            "story_id": doc.get("story_id"),
                            "page": doc.get("page"),
                            "ts": doc["timestamp"].isoformat(),
                        }
                    )
                )
        except Exception:
            pass
        return {"success": True}
    except Exception as e:
        logging.warning(f"[track] failed: {e}")
        return {"success": False}


@api_router.get("/admin/analytics")
async def get_analytics(telegram_id: str):
    from AryaPremium.config import Config
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
            
        arya_db = app.state.db
        
        search_cursor = arya_db.db.mini_app_analytics.find({"type": "search"}).sort("timestamp", -1).limit(20)
        searches = []
        async for doc in search_cursor:
            searches.append({"user_id": doc["user_id"], "query": doc["data"].get("query", ""), "time": doc["timestamp"].isoformat() if isinstance(doc["timestamp"], datetime) else str(doc["timestamp"])})
            
        view_cursor = arya_db.db.mini_app_analytics.find({"type": "view_story"}).sort("timestamp", -1).limit(20)
        views = []
        async for doc in view_cursor:
            views.append({"user_id": doc["user_id"], "story_id": doc["data"].get("story_id", ""), "time": doc["timestamp"].isoformat() if isinstance(doc["timestamp"], datetime) else str(doc["timestamp"])})

        return {"success": True, "data": {"recent_searches": searches, "recent_views": views}}
    except Exception as e:
        return {"success": False, "data": {}}


@api_router.get("/admin/location-analytics")
async def get_location_analytics(telegram_id: str, days: int = 30):
    """Rich location + device analytics for the admin panel â€” SliceURL-style."""
    from AryaPremium.config import Config
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")

        arya_db = app.state.db
        since = datetime.now(timezone.utc) - __import__("datetime").timedelta(days=days)
        bot_filter = {
            "data.client_user_agent": {
                "$not": {
                    "$regex": "bot|crawler|spider|ping|uptime|status|http|curl|wget|python|node|axios|fetch|headless|selenium|puppeteer|playwright|scrape|scan|checker",
                    "$options": "i"
                }
            }
        }
        pipeline_base = {"timestamp": {"$gte": since}, **bot_filter}

        async def _top(field: str, limit: int = 10) -> list:
            pipeline = [
                {"$match": {**pipeline_base, field: {"$exists": True, "$ne": "Unknown", "$ne": None, "$ne": ""}}},
                {"$group": {"_id": f"${field}", "count": {"$sum": 1}}},
                {"$sort": {"count": -1}},
                {"$limit": limit},
            ]
            result = []
            async for doc in arya_db.db.mini_app_analytics.aggregate(pipeline):
                result.append({"name": doc["_id"], "count": doc["count"]})
            return result

        # Hourly trend for last 48h
        now = datetime.now(timezone.utc)
        h48_since = now - __import__("datetime").timedelta(hours=48)
        hourly_pipeline = [
            {"$match": {"timestamp": {"$gte": h48_since}, **bot_filter}},
            {"$group": {
                "_id": {
                    "y": {"$year": "$timestamp"},
                    "mo": {"$month": "$timestamp"},
                    "d": {"$dayOfMonth": "$timestamp"},
                    "h": {"$hour": "$timestamp"}
                },
                "count": {"$sum": 1}
            }},
            {"$sort": {"_id.y": 1, "_id.mo": 1, "_id.d": 1, "_id.h": 1}},
        ]
        hourly = []
        async for doc in arya_db.db.mini_app_analytics.aggregate(hourly_pipeline):
            _id = doc["_id"]
            label = f"{_id.get('d',1):02d}/{_id.get('mo',1):02d} {_id.get('h',0):02d}:00"
            hourly.append({"label": label, "count": doc["count"]})

        # Unique visitors (distinct Telegram user_ids + unique IPs for anonymous, excluding bots)
        visitor_pipeline = [
            {"$match": {"timestamp": {"$gte": since}, **bot_filter}},
            {"$project": {
                "visitor_id": {
                    "$cond": [
                        {"$and": [
                            {"$ne": ["$user_id", None]},
                            {"$ne": ["$user_id", 0]},
                            {"$ne": ["$user_id", "0"]},
                            {"$ne": ["$user_id", "null"]},
                            {"$ne": ["$user_id", "undefined"]}
                        ]},
                        {"$concat": ["user_", {"$toString": "$user_id"}]},
                        {"$concat": ["ip_", {"$ifNull": ["$ip", "unknown"]}]}
                    ]
                }
            }},
            {"$group": {"_id": "$visitor_id"}},
            {"$count": "c"}
        ]
        visitor_res = await arya_db.db.mini_app_analytics.aggregate(visitor_pipeline).to_list(length=1)
        unique_users_count = visitor_res[0]["c"] if visitor_res else 0
        total_events = await arya_db.db.mini_app_analytics.count_documents({"timestamp": {"$gte": since}, **bot_filter})

        session_pipeline = [
            {"$match": {"type": "session_duration", "timestamp": {"$gte": since}, **bot_filter}},
            {"$group": {
                "_id": {
                    "user_id": "$user_id",
                    "day": {"$dateToString": {"format": "%Y-%m-%d", "date": "$timestamp"}}
                },
                "max_duration": {"$max": "$data.duration"}
            }},
            {"$group": {
                "_id": None,
                "avg_duration": {"$avg": "$max_duration"},
                "total_duration": {"$sum": "$max_duration"}
            }}
        ]
        avg_session = 0
        total_session = 0
        async for doc in arya_db.db.mini_app_analytics.aggregate(session_pipeline):
            avg_session = doc.get("avg_duration", 0)
            total_session = doc.get("total_duration", 0)

        countries, cities, devices, browsers, os_list, referrers = await asyncio.gather(
            _top("country", 15),
            _top("city", 15),
            _top("device", 10),
            _top("browser", 10),
            _top("os", 10),
            _top("referrer", 10),
        )

        # Heatmap (day of week vs hour)
        heatmap_pipeline = [
            {"$match": {"timestamp": {"$gte": since}, **bot_filter}},
            {"$group": {
                "_id": {
                    "dayOfWeek": {"$dayOfWeek": "$timestamp"}, # 1 (Sun) to 7 (Sat)
                    "hour": {"$hour": "$timestamp"}
                },
                "count": {"$sum": 1}
            }}
        ]
        heatmap = []
        async for doc in arya_db.db.mini_app_analytics.aggregate(heatmap_pipeline):
            heatmap.append({"day": doc["_id"]["dayOfWeek"] - 1, "hour": doc["_id"]["hour"], "count": doc["count"]})

        # Live Activity / Click Log (last 50 events)
        live_cursor = arya_db.db.mini_app_analytics.find({"timestamp": {"$gte": since}, **bot_filter}).sort("timestamp", -1).limit(50)
        live_activity = []
        async for doc in live_cursor:
            live_activity.append({
                "time": doc["timestamp"].isoformat() if isinstance(doc["timestamp"], datetime) else str(doc["timestamp"]),
                "country": doc.get("country", "Unknown"),
                "city": doc.get("city", "Unknown"),
                "device": doc.get("device", "Unknown"),
                "browser": doc.get("browser", "Unknown"),
                "os": doc.get("os", "Unknown"),
                "referrer": doc.get("referrer", "Direct"),
                "type": doc.get("type", "unknown")
            })

        # Top Pages (for type="page_view")
        top_pages_pipeline = [
            {"$match": {**pipeline_base, "type": "page_view", "data.page": {"$exists": True}}},
            {"$group": {"_id": "$data.page", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": 10},
        ]
        pages = []
        async for doc in arya_db.db.mini_app_analytics.aggregate(top_pages_pipeline):
            pages.append({"name": doc["_id"], "count": doc["count"]})

        return {
            "success": True,
            "data": {
                "summary": {
                    "total_events":   total_events,
                    "unique_visitors": unique_users_count,
                    "days":           days,
                    "avg_session_seconds": avg_session,
                    "total_session_seconds": total_session,
                },
                "hourly_trend": hourly,
                "heatmap":      heatmap,
                "live_activity": live_activity,
                "countries":    countries,
                "cities":       cities,
                "devices":      devices,
                "browsers":     browsers,
                "os":           os_list,
                "referrers":    referrers,
                "pages":        pages,
            }
        }
    except Exception as e:
        logging.error(f"[location-analytics] {e}")
        return {"success": False, "data": {}}


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN SETTINGS — GET / POST
# Stores: mini_app_enabled (bool), tnc_enabled (bool)
# ─────────────────────────────────────────────────────────────────────────────
@api_router.get("/admin/settings")
async def get_admin_settings(telegram_id: str):
    """Returns current feature toggle settings for the Mini App."""
    from AryaPremium.config import Config
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
        arya_db = app.state.db
        cfg = await arya_db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
        return {
            "success": True,
            "data": {
                "mini_app_enabled": cfg.get("mini_app_enabled", True),
                "tnc_enabled": cfg.get("tnc_enabled", True),
                "razorpay_fee_percent": cfg.get("razorpay_fee_percent", 2.36),
                "razorpay_fee_enabled": cfg.get("razorpay_fee_enabled", True),
                "platform_fee_amount": cfg.get("platform_fee_amount", 5.0),
                "platform_fee_enabled": cfg.get("platform_fee_enabled", True),
                "promo_codes": cfg.get("promo_codes", []),
                "outpaint_enabled": cfg.get("outpaint_enabled", False),
                "outpaint_provider": cfg.get("outpaint_provider", "replicate"),
                "replicate_api_key": cfg.get("replicate_api_key", ""),
                "fal_api_key": cfg.get("fal_api_key", ""),
                "stability_api_key": cfg.get("stability_api_key", ""),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"get_admin_settings error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.post("/admin/settings")
async def update_admin_settings(payload: dict):
    """Update feature toggle settings."""
    from AryaPremium.config import Config
    try:
        telegram_id = str(payload.get("telegram_id", ""))
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
        arya_db = app.state.db
        update_fields = {}
        if "mini_app_enabled" in payload:
            update_fields["mini_app_enabled"] = bool(payload["mini_app_enabled"])
        if "tnc_enabled" in payload:
            update_fields["tnc_enabled"] = bool(payload["tnc_enabled"])
        if "razorpay_fee_percent" in payload:
            update_fields["razorpay_fee_percent"] = float(payload["razorpay_fee_percent"])
        if "razorpay_fee_enabled" in payload:
            update_fields["razorpay_fee_enabled"] = bool(payload["razorpay_fee_enabled"])
        if "platform_fee_amount" in payload:
            update_fields["platform_fee_amount"] = float(payload["platform_fee_amount"])
        if "platform_fee_enabled" in payload:
            update_fields["platform_fee_enabled"] = bool(payload["platform_fee_enabled"])
        if "outpaint_enabled" in payload:
            update_fields["outpaint_enabled"] = bool(payload["outpaint_enabled"])
        if "outpaint_provider" in payload:
            update_fields["outpaint_provider"] = str(payload["outpaint_provider"]).strip().lower()
        if "replicate_api_key" in payload:
            update_fields["replicate_api_key"] = str(payload["replicate_api_key"]).strip()
        if "fal_api_key" in payload:
            update_fields["fal_api_key"] = str(payload["fal_api_key"]).strip()
        if "stability_api_key" in payload:
            update_fields["stability_api_key"] = str(payload["stability_api_key"]).strip()
        if "promo_codes" in payload:
            raw_codes = payload["promo_codes"]
            promo_codes = []
            if isinstance(raw_codes, list):
                for pc in raw_codes:
                    if isinstance(pc, dict) and "code" in pc:
                        promo_codes.append({
                            "code": str(pc["code"]).strip().upper(),
                            "type": str(pc.get("type", "percentage")),
                            "value": float(pc.get("value", 0)),
                            "active": bool(pc.get("active", True))
                        })
            update_fields["promo_codes"] = promo_codes

        if not update_fields:
            raise HTTPException(status_code=400, detail="No valid fields to update")
        await arya_db.db.mini_app_config.update_one(
            {"_key": "feature_toggles"},
            {"$set": update_fields},
            upsert=True
        )
        logger.info(f"Admin {telegram_id} updated settings: {update_fields}")
        return {"success": True, "data": update_fields}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"update_admin_settings error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


from fastapi import BackgroundTasks

@api_router.post("/admin/outpaint-migration")
async def run_outpaint_migration(payload: dict, background_tasks: BackgroundTasks):
    """Triggers background outpaint migration for all existing stories or active slider stories only."""
    try:
        telegram_id = str(payload.get("telegram_id", ""))
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
            
        async def do_migration():
            logger.info("Starting background outpaint migration...")
            arya_db = app.state.db
            from bson.objectid import ObjectId
            
            banners_only = bool(payload.get("test_only", False)) or bool(payload.get("banners_only", False))
            
            if banners_only:
                logger.info("Outpaint migration running in BANNERS-ONLY / TEST mode to save Replicate credits.")
                target_story_ids = set()
                
                # 1. Add trending story ID
                try:
                    pipeline = [
                        {"$match": {"status": {"$in": ["paid", "delivered"]}}},
                        {"$unwind": "$story_ids"},
                        {"$group": {"_id": "$story_ids", "count": {"$sum": 1}}},
                        {"$sort": {"count": -1}},
                        {"$limit": 1}
                    ]
                    agg = await arya_db.db.orders.aggregate(pipeline).to_list(1)
                    if agg:
                        target_story_ids.add(str(agg[0]["_id"]))
                except Exception as e:
                    logger.warning(f"Error fetching trending in migration: {e}")
                    
                # 2. Add newest story ID
                try:
                    newest = await arya_db.db.premium_stories.find_one({}, sort=[("_id", -1)])
                    if newest:
                        target_story_ids.add(str(newest["_id"]))
                except Exception as e:
                    logger.warning(f"Error fetching newest in migration: {e}")
                    
                # 3. Add manual banner story IDs
                try:
                    manual_cursor = arya_db.db.mini_app_banners.find({})
                    async for b in manual_cursor:
                        t_link = b.get("target_link")
                        if t_link:
                            target_story_ids.add(str(t_link))
                except Exception as e:
                    logger.warning(f"Error fetching manual banners in migration: {e}")
                
                # Convert string IDs back to ObjectIds
                query_ids = []
                for sid in target_story_ids:
                    try:
                        query_ids.append(ObjectId(sid))
                    except:
                        pass
                
                if query_ids:
                    stories = await arya_db.db.premium_stories.find({"_id": {"$in": query_ids}}).to_list(length=None)
                else:
                    stories = []
            else:
                stories = await arya_db.db.premium_stories.find({}).to_list(length=None)
                
            force = bool(payload.get("force", True))  # Defaults to True to refresh all banners asymmetrically
            for story in stories:
                poster_url = story.get("poster_url") or story.get("cover") or story.get("image_url")
                if poster_url and (force or not story.get("banner_url") or story.get("banner_url") == poster_url):
                    try:
                        import aiohttp
                        async with aiohttp.ClientSession() as session:
                            async with session.get(poster_url) as resp:
                                if resp.status == 200:
                                    poster_bytes = await resp.read()
                                    from ai_outpaint_service import process_outpaint
                                    title_pos = story.get("title_position", "left")
                                    outpainted_bytes = await process_outpaint(poster_bytes, title_pos)
                                    uploaded_banner_url = await optimize_and_upload_to_storage(
                                        outpainted_bytes, width=1184, height=556, quality=80
                                    )
                                    if uploaded_banner_url:
                                        await arya_db.db.premium_stories.update_one(
                                            {"_id": story["_id"]},
                                            {"$set": {"banner_url": uploaded_banner_url}}
                                        )
                                        logger.info(f"Successfully migrated story {story.get('story_id')}: {uploaded_banner_url}")
                    except Exception as e:
                        logger.error(f"Migration error for story {story.get('story_id')}: {e}")
                    await asyncio.sleep(1.5) # stagger requests
            logger.info("Background outpaint migration complete!")

        background_tasks.add_task(do_migration)
        return {"success": True, "message": "Migration successfully started in the background."}
    except Exception as e:
        logger.error(f"Error starting outpaint migration: {e}")
        return {"success": False, "message": str(e)}

@api_router.get("/settings")
async def get_public_settings():
    """Public endpoint: returns feature flags readable by the Mini App frontend."""
    try:
        arya_db = app.state.db
        cfg = await arya_db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
        return {
            "success": True,
            "mini_app_enabled": cfg.get("mini_app_enabled", True),
            "tnc_enabled": cfg.get("tnc_enabled", True),
            "razorpay_fee_percent": cfg.get("razorpay_fee_percent", 2.36),
            "razorpay_fee_enabled": cfg.get("razorpay_fee_enabled", True),
            "platform_fee_amount": cfg.get("platform_fee_amount", 5.0),
            "platform_fee_enabled": cfg.get("platform_fee_enabled", True),
            "promo_codes": cfg.get("promo_codes", []),
        }
    except Exception as e:
        logger.warning(f"get_public_settings error: {e}")
        return {
            "success": True,
            "mini_app_enabled": True,
            "tnc_enabled": True,
            "razorpay_fee_percent": 2.36,
            "razorpay_fee_enabled": True,
            "platform_fee_amount": 5.0,
            "platform_fee_enabled": True,
            "promo_codes": []
        }


@api_router.get("/analytics/enterprise-dashboard")
async def enterprise_dashboard(
    telegram_id: str,
    response: Response,
    days: int = 30,
    query: Optional[str] = None,
    telegram_only: bool = False,
    premium_only: bool = False,
    new_users: bool = False,
    returning_users: bool = False,
):
    """Enterprise analytics JSON for the Next.js intelligence console (Mongo-backed)."""
    try:
        from AryaPremium.config import Config
        from arya_enterprise_analytics import build_enterprise_dashboard, filters_from_query

        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"

        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
        arya_db = app.state.db
        flt = filters_from_query(
            days=days,
            query=query,
            telegram_only=telegram_only,
            premium_only=premium_only,
            new_users=new_users,
            returning_users=returning_users,
        )
        return await build_enterprise_dashboard(arya_db, flt)
    except Exception as e:
        logger.exception("Failed to build enterprise dashboard analytics")
        raise HTTPException(status_code=500, detail=f"Request failed: {str(e)}")

# ─────────────────────────────────────────────────────────────────
# ADMIN STANDALONE AUTHENTICATION & SESSIONS
# ─────────────────────────────────────────────────────────────────
import smtplib
import secrets
from email.mime.text import MIMEText
from email.header import Header

async def send_smtp_email(to_email: str, subject: str, text_content: str) -> bool:
    """Sends an email notification via SMTP config defined in feature toggles or environment variables."""
    db = getattr(app.state, "db", None)
    cfg = {}
    if db:
        try:
            cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
        except Exception:
            pass
            
    smtp_host = cfg.get("smtp_host") or os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    smtp_port_str = cfg.get("smtp_port") or os.environ.get("SMTP_PORT") or "587"
    smtp_user = cfg.get("smtp_user") or os.environ.get("SMTP_USER")
    smtp_pass = cfg.get("smtp_pass") or os.environ.get("SMTP_PASSWORD")
    smtp_sender = cfg.get("smtp_sender") or os.environ.get("SMTP_SENDER") or smtp_user
    
    if not smtp_user or not smtp_pass:
        logger.warning("SMTP credentials not configured. Cannot send email.")
        return False
        
    try:
        smtp_port = int(smtp_port_str)
    except ValueError:
        smtp_port = 587
        
    try:
        msg = MIMEText(text_content, 'plain', 'utf-8')
        msg['Subject'] = Header(subject, 'utf-8')
        msg['From'] = smtp_sender
        msg['To'] = to_email
        
        def _send():
            if smtp_port == 465:
                server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10)
            else:
                server = smtplib.SMTP(smtp_host, smtp_port, timeout=10)
                server.ehlo()
                server.starttls()
                server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_sender, [to_email], msg.as_string())
            server.quit()
            
        await asyncio.to_thread(_send)
        logger.info(f"OTP email sent successfully to {to_email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email via SMTP: {e}")
        return False

async def log_to_telegram(text: str):
    """Sends active security log updates to the Telegram channels."""
    from AryaPremium.config import Config
    token = getattr(Config, "MGMT_BOT_TOKEN", None) or os.environ.get("MGMT_BOT_TOKEN")
    channel_id = getattr(Config, "ARYA_LOGS_CHANNEL", None) or getattr(Config, "PAYMENT_LOGS_CHANNEL", None) or os.environ.get("ARYA_LOGS_CHANNEL")
    if not token or not channel_id:
        return
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": channel_id,
                    "text": text,
                    "parse_mode": "HTML"
                }
            )
    except Exception as e:
        logger.error(f"Failed to send Telegram log: {e}")

@api_router.post("/admin/auth/setup-email")
async def setup_admin_email(telegram_id: str = Form(...), email: str = Form(...)):
    """One-time setup: Allows Telegram OWNER_IDS to register admin email in DB without needing a session.
    This bypasses the chicken-and-egg problem of needing email to login but needing login to set email.
    """
    from AryaPremium.config import Config
    try:
        uid = int(telegram_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid telegram_id")
    if uid not in Config.OWNER_IDS:
        raise HTTPException(status_code=403, detail="Not authorized — only bot owners can use this endpoint")

    email_clean = email.strip().lower()
    if "@" not in email_clean or "." not in email_clean:
        raise HTTPException(status_code=400, detail="Invalid email address")

    db = getattr(app.state, "db", None)
    if not db:
        raise HTTPException(status_code=500, detail="Database not available")

    # Read existing owner_emails from DB
    cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    existing = cfg.get("owner_emails", "")
    existing_set = set(e.strip().lower() for e in existing.replace(",", " ").split() if e.strip())
    existing_set.add(email_clean)
    new_emails_str = ",".join(sorted(existing_set))

    await db.db.mini_app_config.update_one(
        {"_key": "feature_toggles"},
        {"$set": {"owner_emails": new_emails_str}},
        upsert=True
    )
    logger.info(f"Owner {telegram_id} registered admin email: {email_clean}")
    await log_to_telegram(
        f"<b>Admin Email Registered</b>\n"
        f"By Telegram ID: <code>{telegram_id}</code>\n"
        f"Email: <code>{email_clean}</code>\n"
        f"All admin emails: <code>{new_emails_str}</code>"
    )
    return {"success": True, "message": f"Email {email_clean} registered as admin. You can now login.", "all_emails": new_emails_str}


@api_router.post("/admin/auth/send-otp")
async def send_admin_otp(email: str = Form(...)):
    """Generates a 6-digit verification code, stores it, sends via SMTP or posts to Telegram log."""
    email_clean = email.strip().lower()
    from AryaPremium.config import Config
    owner_emails_str = os.environ.get("OWNER_EMAILS", "")
    
    db = getattr(app.state, "db", None)
    cfg = {}
    if db:
        try:
            cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
        except Exception:
            pass
            
    db_emails_str = cfg.get("owner_emails", "")
    
    allowed_emails = set()
    for s in [owner_emails_str, db_emails_str]:
        if s:
            for email_part in s.replace(",", " ").split():
                if email_part.strip():
                    allowed_emails.add(email_part.strip().lower())
                    
    if not allowed_emails:
        logger.warning("No OWNER_EMAILS configured in system. Admin email login is blocked.")
        raise HTTPException(status_code=403, detail="Admin email not configured. Use /api/admin/auth/setup-email with your Telegram ID to register first.")
        
    if email_clean not in allowed_emails:
        raise HTTPException(status_code=403, detail=f"Email not authorized as Admin. Use setup-email endpoint to register, or check OWNER_EMAILS env variable.")
        
    otp = "".join(secrets.choice("0123456789") for _ in range(6))
    expiry = datetime.now(timezone.utc).timestamp() + 300 # 5 minutes
    
    if db:
        await db.db.admin_otps.update_one(
            {"email": email_clean},
            {"$set": {"otp": otp, "expires_at": expiry, "verified": False}},
            upsert=True
        )
        
    email_sent = await send_smtp_email(
        to_email=email_clean,
        subject="Arya Premium Console — 2FA OTP Code",
        text_content=f"Your Arya Premium Admin Console login verification code is: {otp}\n\nThis OTP is valid for 5 minutes. Do not share it with anyone."
    )
    
    log_msg = (
        f"<b>Admin OTP Request</b>\n"
        f"Email: <code>{email_clean}</code>\n"
        f"OTP: <code>{otp}</code>\n"
        f"Status: {'Sent via SMTP' if email_sent else 'SMTP Config Missing/Failed — Code logged as fallback'}"
    )
    await log_to_telegram(log_msg)
    
    return {"success": True, "message": "OTP sent successfully", "fallback_sent": not email_sent}


@api_router.post("/admin/auth/verify-otp")
async def verify_admin_otp(request: Request, email: str = Form(...), otp: str = Form(...)):
    """Verifies OTP and generates a secure session valid for up to 10 days."""
    email_clean = email.strip().lower()
    otp_clean = otp.strip()
    
    db = getattr(app.state, "db", None)
    if not db:
        raise HTTPException(status_code=500, detail="Database connection not available")
        
    otp_doc = await db.db.admin_otps.find_one({"email": email_clean})
    if not otp_doc:
        raise HTTPException(status_code=400, detail="OTP not requested or invalid")
        
    current_time = datetime.now(timezone.utc).timestamp()
    if otp_doc["expires_at"] < current_time:
        raise HTTPException(status_code=400, detail="OTP has expired")
        
    if otp_doc["otp"] != otp_clean:
        raise HTTPException(status_code=400, detail="Invalid OTP code")
        
    await db.db.admin_otps.delete_one({"email": email_clean})
    
    session_token = secrets.token_hex(32)
    created_at = datetime.now(timezone.utc)
    expires_at = created_at + timedelta(days=10)
    
    ip_addr = request.headers.get("X-Forwarded-For") or request.client.host
    if ip_addr and "," in ip_addr:
        ip_addr = ip_addr.split(",")[0].strip()
        
    user_agent = request.headers.get("User-Agent") or "Unknown Browser"
    
    session_doc = {
        "session_token": session_token,
        "email": email_clean,
        "ip": ip_addr,
        "user_agent": user_agent,
        "created_at": created_at,
        "expires_at": expires_at,
        "active": True
    }
    
    await db.db.admin_sessions.insert_one(session_doc)
    
    await log_to_telegram(
        f"<b>Admin Logged In</b>\n"
        f"Email: <code>{email_clean}</code>\n"
        f"IP: <code>{ip_addr}</code>\n"
        f"User Agent: <code>{user_agent}</code>"
    )
    
    return {
        "success": True,
        "session_token": session_token,
        "expires_at": expires_at.isoformat(),
        "email": email_clean
    }

@api_router.get("/admin/auth/sessions")
async def get_admin_sessions(request: Request):
    """Lists all active device sessions for the authenticated administrator."""
    session_token = request.headers.get("X-Admin-Session")
    db = getattr(app.state, "db", None)
    if not session_token or not db:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    current_session = await db.db.admin_sessions.find_one({
        "session_token": session_token,
        "active": True,
        "expires_at": {"$gt": datetime.now(timezone.utc)}
    })
    if not current_session:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    sessions_cursor = db.db.admin_sessions.find({
        "email": current_session["email"],
        "active": True,
        "expires_at": {"$gt": datetime.now(timezone.utc)}
    })
    
    sessions_list = await sessions_cursor.to_list(length=None)
    
    result = []
    for s in sessions_list:
        is_curr = s["session_token"] == session_token
        masked_token = s["session_token"][:6] + "..." + s["session_token"][-6:]
        result.append({
            "token_id": s["session_token"],
            "masked_token": masked_token,
            "ip": s.get("ip") or "Unknown",
            "user_agent": s.get("user_agent") or "Unknown",
            "created_at": s["created_at"].isoformat() if isinstance(s["created_at"], datetime) else str(s["created_at"]),
            "expires_at": s["expires_at"].isoformat() if isinstance(s["expires_at"], datetime) else str(s["expires_at"]),
            "is_current": is_curr
        })
        
    return {"success": True, "sessions": result}

@api_router.post("/admin/auth/revoke-session")
async def revoke_admin_session(request: Request, token_to_revoke: str = Form(...)):
    """Terminates a specific device session."""
    session_token = request.headers.get("X-Admin-Session")
    db = getattr(app.state, "db", None)
    if not session_token or not db:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    current_session = await db.db.admin_sessions.find_one({
        "session_token": session_token,
        "active": True,
        "expires_at": {"$gt": datetime.now(timezone.utc)}
    })
    if not current_session:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    await db.db.admin_sessions.update_one(
        {"session_token": token_to_revoke, "email": current_session["email"]},
        {"$set": {"active": False}}
    )
    
    return {"success": True, "message": "Session revoked"}

@api_router.post("/admin/auth/logout")
async def admin_logout(request: Request):
    """Expires and revokes the active session token."""
    session_token = request.headers.get("X-Admin-Session")
    db = getattr(app.state, "db", None)
    if session_token and db:
        await db.db.admin_sessions.update_one(
            {"session_token": session_token},
            {"$set": {"active": False}}
        )
    return {"success": True, "message": "Logged out"}

@app.middleware("http")
async def admin_auth_middleware(request: Request, call_next):
    """Intercepts and guards all administrative and analytics endpoints."""
    path = request.url.path
    is_admin_path = ("/admin/" in path) or ("/analytics/" in path)
    is_auth_endpoint = "/admin/auth/" in path
    
    if is_admin_path and not is_auth_endpoint:
        session_token = request.headers.get("X-Admin-Session")
        db = getattr(app.state, "db", None)
        
        authenticated = False
        if session_token and db:
            session = await db.db.admin_sessions.find_one({
                "session_token": session_token,
                "active": True,
                "expires_at": {"$gt": datetime.now(timezone.utc)}
            })
            if session:
                authenticated = True
                admin_authenticated_session.set(True)
                
        # Telegram ID validation fallback for Mini App query checks
        if not authenticated:
            telegram_id = request.query_params.get("telegram_id")
            if telegram_id:
                from AryaPremium.config import Config
                try:
                    uid = int(telegram_id)
                except ValueError:
                    uid = telegram_id
                if uid in Config.OWNER_IDS:
                    authenticated = True
                    
        if not authenticated:
            return Response(
                content='{"detail":"Unauthorized: Admin session required"}',
                status_code=401,
                media_type="application/json"
            )
            
    response = await call_next(request)
    return response

app.include_router(api_router, prefix="/api")
app.include_router(api_router) # Handle both /api/stories and /stories for Nginx proxy compatibility



@app.websocket("/api/ws/analytics")
async def analytics_websocket(websocket: WebSocket, telegram_id: str = Query(...)):
    """Owner-only live event stream (JSON lines). Scale-out: replace hub with Redis."""
    from AryaPremium.config import Config
    from arya_enterprise_analytics import hub as _analytics_ws_hub

    try:
        uid = int(telegram_id)
    except ValueError:
        await websocket.close(code=4400)
        return
    if uid not in Config.OWNER_IDS:
        await websocket.close(code=4403)
        return
    await websocket.accept()
    await _analytics_ws_hub.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await _analytics_ws_hub.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("mini_app_api:app", host="0.0.0.0", port=8000, reload=True)