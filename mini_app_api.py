import os
import uuid
import logging
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI, APIRouter, HTTPException, Form, File, UploadFile, Request
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ─────────────────────────────────────────────────────────────────
# Use AryaPremium's own database module (already tested, working)
# ─────────────────────────────────────────────────────────────────
import sys
import importlib

# Add AryaPremium to path so we can import its database
_arya_path = os.path.join(os.path.dirname(__file__), "AryaPremium")
if _arya_path not in sys.path:
    sys.path.insert(0, _arya_path)

# ─────────────────────────────────────────────────────────────────
# Lifespan: connect/disconnect
# ─────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        from AryaPremium.database import db as arya_db
        await arya_db.connect()
        app.state.db = arya_db
        logger.info("✅ Connected to MongoDB via AryaPremium DB module")
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

# ─────────────────────────────────────────────────────────────────
# Helper: format a single MongoDB story doc → frontend Story shape
# ─────────────────────────────────────────────────────────────────
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

    return {
        "id":           story_id,
        "title":        title,
        "description":  description,
        "poster":       cover,
        "banner":       cover,
        "cover":        cover,
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
    }


# ─────────────────────────────────────────────────────────────────
# GET /stories
# ─────────────────────────────────────────────────────────────────
@api_router.get("/stories")
async def get_stories():
    """Fetch all premium stories using AryaPremium's db.get_all_stories()"""
    try:
        arya_db = app.state.db
        # Use the existing, tested method from AryaPremium/database.py
        stories = await arya_db.get_all_stories()

        formatted = []
        for s in stories:
            item = _format_story(s)
            if item:
                formatted.append(item)

        logger.info(f"Returning {len(formatted)} stories")
        return {"success": True, "data": formatted}

    except Exception as e:
        logger.error(f"Error in /stories: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


from AryaPremium.config import Config

import razorpay

# Use the existing keys from AryaPremium config
RZP_KEY_ID = Config.RAZORPAY_KEY
RZP_KEY_SECRET = Config.RAZORPAY_SECRET
rzp_client = razorpay.Client(auth=(RZP_KEY_ID, RZP_KEY_SECRET))

# ─────────────────────────────────────────────────────────────────
# POST /create-payment-link
# ─────────────────────────────────────────────────────────────────
@api_router.post("/create-payment-link")
async def create_payment_link(payload: dict):
    """Creates a Razorpay Payment Link linked to an order."""
    telegram_id = payload.get("telegram_id")
    story_ids   = payload.get("story_ids", [])
    username    = payload.get("username", "")

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

    total_price = sum(float(s.get("price", 0) or 0) for s in valid_stories)
    if total_price <= 0:
        raise HTTPException(status_code=400, detail="Invalid price")

    order_id = f"OD_{uuid.uuid4().hex[:8].upper()}"
    bot_username = os.environ.get("BOT_USERNAME", "AryaPremiumBot")
    
    try:
        if not RZP_KEY_ID or not RZP_KEY_SECRET:
            raise ValueError("Razorpay API keys are missing in the .env file. Please check RAZORPAY_KEY and RAZORPAY_SECRET.")

        # Create Razorpay Payment Link
        link_data = rzp_client.payment_link.create({
            "amount": int(total_price * 100), # in paise
            "currency": "INR",
            "accept_partial": False,
            "description": "SliceURL Services",
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
        
        # Save order to DB
        order_doc = {
            "order_id":    order_id,
            "payment_link_id": link_data["id"],
            "user_id":     telegram_id,
            "username":    username,
            "story_ids":   story_ids,
            "total_amount":total_price,
            "status":      "pending",
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

# ─────────────────────────────────────────────────────────────────
# POST /check-payment-link
# ─────────────────────────────────────────────────────────────────
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
            
            bot_username = os.environ.get("BOT_USERNAME", "AryaPremiumBot")
            return {
                "success": True,
                "status": "paid",
                "checkout_url": f"https://t.me/{bot_username}?start=success_{order['order_id']}" if order else ""
            }
            
        return {"success": True, "status": "pending"}
    except Exception as e:
        logger.error(f"Razorpay verification failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))



# ─────────────────────────────────────────────────────────────────
# POST /support
# ─────────────────────────────────────────────────────────────────
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
        
        admin_txt = (
            f"<b>📨 New Feedback from Mini App</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>👤 User:</b> {first_name}\n"
            f"<b>🔗 Username:</b> @{username}\n"
            f"<b>🆔 User ID:</b> <code>{telegram_id}</code>\n"
            f"<b>💬 Type:</b> {type.title()}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
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


# ─────────────────────────────────────────────────────────────────
# GET /my-requests
# ─────────────────────────────────────────────────────────────────
@api_router.get("/my-requests")
async def get_my_requests(telegram_id: str):
    """Fetches user's requests and support tickets."""
    arya_db = app.state.db
    
    try:
        user_id = int(telegram_id) if telegram_id.isdigit() else telegram_id
        cursor = arya_db.db.premium_feedback.find({"user_id": user_id}).sort("created_at", -1)
        
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

# ─────────────────────────────────────────────────────────────────
# GET /my-purchases
# ─────────────────────────────────────────────────────────────────
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
                        purchased_items.append(formatted)
            except Exception:
                pass
                
        return {"success": True, "data": purchased_items}
    except Exception as e:
        logger.error(f"Failed to fetch my-purchases: {e}")
        return {"success": False, "data": []}

# ─────────────────────────────────────────────────────────────────
# GET /admin/stats
# ─────────────────────────────────────────────────────────────────
@api_router.get("/admin/stats")
async def get_admin_stats(telegram_id: str):
    """Fetches full admin analysis dashboard."""
    from AryaPremium.config import Config
    
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if user_id_int not in Config.OWNER_IDS:
            raise HTTPException(status_code=403, detail="Not authorized as Admin")
            
        arya_db = app.state.db
        
        # Total Users
        total_users = await arya_db.users.count_documents({})
        
        # Total Stories
        total_stories = await arya_db.db.premium_stories.count_documents({})
        
        # Total Revenue (sum of total_amount where status='paid')
        pipeline = [{"$match": {"status": "paid"}}, {"$group": {"_id": None, "total": {"$sum": "$total_amount"}}}]
        rev_res = await arya_db.db.orders.aggregate(pipeline).to_list(length=1)
        total_revenue = rev_res[0]["total"] if rev_res else 0
        
        # Recent Feedbacks
        feedbacks = []
        fb_cursor = arya_db.db.premium_feedback.find({}).sort("created_at", -1).limit(10)
        async for doc in fb_cursor:
            feedbacks.append({
                "id": str(doc.get("_id", "")),
                "user_id": doc.get("user_id"),
                "username": doc.get("username"),
                "type": doc.get("type"),
                "text": doc.get("text"),
                "status": doc.get("status"),
                "created_at": doc.get("created_at", datetime.now(timezone.utc)).isoformat() if isinstance(doc.get("created_at"), datetime) else doc.get("created_at", "")
            })
            
        # Recent Orders
        orders = []
        ord_cursor = arya_db.db.orders.find({}).sort("created_at", -1).limit(10)
        async for doc in ord_cursor:
            orders.append({
                "order_id": doc.get("order_id"),
                "amount": doc.get("total_amount"),
                "status": doc.get("status"),
                "user_id": doc.get("user_id"),
                "username": doc.get("username"),
                "created_at": doc.get("created_at", datetime.now(timezone.utc)).isoformat() if isinstance(doc.get("created_at"), datetime) else doc.get("created_at", "")
            })
            
        return {
            "success": True,
            "data": {
                "total_users": total_users,
                "total_stories": total_stories,
                "total_revenue": total_revenue,
                "recent_feedback": feedbacks,
                "recent_orders": orders
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch admin stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ─────────────────────────────────────────────────────────────────
# GET /admin/stories
# ─────────────────────────────────────────────────────────────────
@api_router.get("/admin/stories")
async def get_admin_stories(telegram_id: str):
    """Fetches all stories for admin management."""
    from AryaPremium.config import Config
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if user_id_int not in Config.OWNER_IDS:
            raise HTTPException(status_code=403, detail="Not authorized as Admin")
            
        arya_db = app.state.db
        stories = await arya_db.get_all_stories()
        result = []
        for s in stories:
            _id_str = str(s["_id"])
            # Always ensure story_id is set — fallback to _id if missing
            story_id = s.get("story_id") or _id_str
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

# ─────────────────────────────────────────────────────────────────
# POST /admin/story
# ─────────────────────────────────────────────────────────────────
@api_router.post("/admin/story")
async def save_admin_story(request: Request):
    """Creates or updates a story — accepts any JSON payload."""
    from AryaPremium.config import Config
    try:
        data = await request.json()
        telegram_id = str(data.get("telegram_id", ""))
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if user_id_int not in Config.OWNER_IDS:
            raise HTTPException(status_code=403, detail="Not authorized")
        
        # Remove non-DB fields
        save_doc = {k: v for k, v in data.items() if k not in ("telegram_id", "_id")}
        
        # Ensure story_id exists
        if not save_doc.get("story_id"):
            raise HTTPException(status_code=400, detail="story_id is required")
        
        save_doc["updated_via"] = "mini_app_admin"
        arya_db = app.state.db
        await arya_db.save_story(save_doc)
        return {"success": True, "message": "Story saved successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving story: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ─────────────────────────────────────────────────────────────────
# UPLOAD ADMIN IMAGE (POST /admin/upload-image)
# ─────────────────────────────────────────────────────────────────
@api_router.post("/admin/upload-image")
async def upload_admin_image(telegram_id: str = Form(...), file: UploadFile = File(...)):
    from AryaPremium.config import Config
    import aiohttp
    import io
    from PIL import Image

    user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
    if user_id_int not in Config.OWNER_IDS:
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

# ─────────────────────────────────────────────────────────────────
# DELETE /admin/story/{story_id}
# ─────────────────────────────────────────────────────────────────
@api_router.delete("/admin/story/{story_id}")
async def delete_admin_story(story_id: str, telegram_id: str):
    """Deletes a story."""
    from AryaPremium.config import Config
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if user_id_int not in Config.OWNER_IDS:
            raise HTTPException(status_code=403, detail="Not authorized")
            
        arya_db = app.state.db
        await arya_db.delete_story(story_id)
        return {"success": True, "message": "Story deleted successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
# ─────────────────────────────────────────────────────────────────
# SUPPORT MANAGEMENT
# ─────────────────────────────────────────────────────────────────
@api_router.get("/admin/support")
async def get_admin_support(telegram_id: str):
    from AryaPremium.config import Config
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if user_id_int not in Config.OWNER_IDS:
            raise HTTPException(status_code=403, detail="Not authorized")
            
        arya_db = app.state.db
        cursor = arya_db.db.premium_feedback.find({"status": {"$ne": "resolved"}}).sort("created_at", -1).limit(100)
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
        if user_id_int not in Config.OWNER_IDS:
            raise HTTPException(status_code=403, detail="Not authorized")
            
        arya_db = app.state.db
        ticket = await arya_db.db.premium_feedback.find_one({"_id": ObjectId(data.ticket_id)})
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")
        
        # Determine which bot token to use (management bot preferred)
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

# ─────────────────────────────────────────────────────────────────
# BANNERS MANAGEMENT
# ─────────────────────────────────────────────────────────────────
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
        
        pipeline = [
            {"$match": {"status": {"$in": ["paid", "delivered"]}}},
            {"$unwind": "$story_ids"},
            {"$group": {"_id": "$story_ids", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": 10}
        ]
        
        agg = await arya_db.db.orders.aggregate(pipeline).to_list(10)
        result = []
        
        for item in agg:
            try:
                story_id = item["_id"]
                story = await arya_db.db.premium_stories.find_one({"_id": ObjectId(str(story_id))})
                if story:
                    fmt = _format_story(story)
                    if fmt:
                        fmt["buy_count"] = item["count"]
                        result.append(fmt)
            except Exception as e:
                logger.warning(f"Error formatting popular story {item.get('_id')}: {e}")
                
        return {"success": True, "data": result}
    except Exception as e:
        logger.error(f"/popular error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/admin/banners")
async def get_admin_banners(telegram_id: str):
    from AryaPremium.config import Config
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if user_id_int not in Config.OWNER_IDS:
            raise HTTPException(status_code=403, detail="Not authorized")
        arya_db = app.state.db
        cursor = arya_db.db.mini_app_banners.find({}).sort("order", 1)
        banners = []
        async for doc in cursor:
            banners.append({
                "id": str(doc["_id"]),
                "image_url": doc.get("image_url", ""),
                "target_link": doc.get("target_link", ""),
                "order": doc.get("order", 0)
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

@api_router.post("/admin/banner")
async def save_admin_banner(data: BannerUpdate):
    from AryaPremium.config import Config
    from bson.objectid import ObjectId
    try:
        user_id_int = int(data.telegram_id) if data.telegram_id.isdigit() else data.telegram_id
        if user_id_int not in Config.OWNER_IDS:
            raise HTTPException(status_code=403, detail="Not authorized")
        arya_db = app.state.db
        doc = {
            "image_url": data.image_url,
            "target_link": data.target_link,
            "order": data.order
        }
        if data.id and data.id != "new":
            await arya_db.db.mini_app_banners.update_one({"_id": ObjectId(data.id)}, {"$set": doc})
        else:
            await arya_db.db.mini_app_banners.insert_one(doc)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@api_router.delete("/admin/banner")
async def delete_admin_banner(telegram_id: str, banner_id: str):
    from AryaPremium.config import Config
    from bson.objectid import ObjectId
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if user_id_int not in Config.OWNER_IDS:
            raise HTTPException(status_code=403, detail="Not authorized")
        arya_db = app.state.db
        await arya_db.db.mini_app_banners.delete_one({"_id": ObjectId(banner_id)})
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ─────────────────────────────────────────────────────────────────
# BUYERS MANAGEMENT
# ─────────────────────────────────────────────────────────────────
@api_router.get("/admin/buyers")
async def get_admin_buyers(telegram_id: str):
    from AryaPremium.config import Config
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if user_id_int not in Config.OWNER_IDS:
            raise HTTPException(status_code=403, detail="Not authorized")
        arya_db = app.state.db
        
        # Get full order data
        cursor = arya_db.db.orders.find({}).sort("created_at", -1).limit(200)
        buyers = []
        async for doc in cursor:
            # Try to get story names
            story_ids = doc.get("story_ids", [])
            if not story_ids and doc.get("story_id"):
                story_ids = [doc.get("story_id")]
            story_names = []
            for sid in story_ids:
                story = await arya_db.db.premium_stories.find_one({"story_id": sid}, {"story_name_en": 1})
                if story:
                    story_names.append(story.get("story_name_en", sid))
                else:
                    story_names.append(sid)
            buyers.append({
                "order_id": str(doc.get("order_id", doc["_id"])),
                "user_id": doc.get("user_id"),
                "username": doc.get("username", "Unknown"),
                "first_name": doc.get("first_name", ""),
                "amount": doc.get("total_amount", doc.get("amount", 0)),
                "status": doc.get("status", "unknown"),
                "payment_id": doc.get("payment_id", doc.get("razorpay_payment_id", "")),
                "source": doc.get("source", "unknown"),
                "story_ids": story_ids,
                "story_names": story_names,
                "date": doc.get("created_at", datetime.now(timezone.utc)).isoformat() if isinstance(doc.get("created_at"), datetime) else str(doc.get("created_at", ""))
            })
        return {"success": True, "data": buyers}
    except Exception as e:
        logger.error(f"Error fetching buyers: {e}")
        return {"success": False, "data": []}

# ─────────────────────────────────────────────────────────────────
# ANALYTICS TRACKING
# ─────────────────────────────────────────────────────────────────
class TrackEvent(BaseModel):
    telegram_id: str
    event_type: str  # "search", "view_story", "add_to_cart", "open_app"
    event_data: dict

@api_router.post("/track")
async def track_event(data: TrackEvent):
    try:
        user_id_int = int(data.telegram_id) if data.telegram_id.isdigit() else data.telegram_id
        arya_db = app.state.db
        await arya_db.db.mini_app_analytics.insert_one({
            "user_id": user_id_int,
            "type": data.event_type,
            "data": data.event_data,
            "timestamp": datetime.now(timezone.utc)
        })
        return {"success": True}
    except Exception:
        return {"success": False}

@api_router.get("/admin/analytics")
async def get_analytics(telegram_id: str):
    from AryaPremium.config import Config
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if user_id_int not in Config.OWNER_IDS:
            raise HTTPException(status_code=403, detail="Not authorized")
            
        arya_db = app.state.db
        
        # Get latest searches
        search_cursor = arya_db.db.mini_app_analytics.find({"type": "search"}).sort("timestamp", -1).limit(20)
        searches = []
        async for doc in search_cursor:
            searches.append({"user_id": doc["user_id"], "query": doc["data"].get("query", ""), "time": doc["timestamp"].isoformat() if isinstance(doc["timestamp"], datetime) else str(doc["timestamp"])})
            
        # Get latest views
        view_cursor = arya_db.db.mini_app_analytics.find({"type": "view_story"}).sort("timestamp", -1).limit(20)
        views = []
        async for doc in view_cursor:
            views.append({"user_id": doc["user_id"], "story_id": doc["data"].get("story_id", ""), "time": doc["timestamp"].isoformat() if isinstance(doc["timestamp"], datetime) else str(doc["timestamp"])})

        return {"success": True, "data": {"recent_searches": searches, "recent_views": views}}
    except Exception as e:
        return {"success": False, "data": {}}

app.include_router(api_router, prefix="/api")
app.include_router(api_router) # Handle both /api/stories and /stories for Nginx proxy compatibility

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("mini_app_api:app", host="0.0.0.0", port=8000, reload=True)
