import os
import uuid
import logging
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI, APIRouter, HTTPException, Form, File, UploadFile
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
        # Return raw stories for admin editing
        return {"success": True, "data": [{**s, "_id": str(s["_id"])} for s in stories]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

from pydantic import BaseModel
from typing import Optional, List

class StoryUpdate(BaseModel):
    telegram_id: str
    story_id: str
    bot_id: Optional[int] = None
    bot_username: Optional[str] = None
    start_id: Optional[int] = None
    end_id: Optional[int] = None
    source: Optional[int] = None
    story_name_en: str
    story_name_hi: str
    description: str
    description_hi: str
    episodes: str
    status: str
    genre: str
    language: str
    price: int
    discount_price: int
    payment_methods: List[str]
    platform: str
    delivery_mode: str
    channel_id: Optional[int] = None
    image: Optional[str] = None
    poster_url: str
    is_completed: bool

# ─────────────────────────────────────────────────────────────────
# POST /admin/story
# ─────────────────────────────────────────────────────────────────
@api_router.post("/admin/story")
async def save_admin_story(data: StoryUpdate):
    """Creates or updates a story."""
    from AryaPremium.config import Config
    try:
        user_id_int = int(data.telegram_id) if data.telegram_id.isdigit() else data.telegram_id
        if user_id_int not in Config.OWNER_IDS:
            raise HTTPException(status_code=403, detail="Not authorized")
            
        arya_db = app.state.db
        story_doc = {
            "story_id": data.story_id,
            "bot_id": data.bot_id,
            "bot_username": data.bot_username,
            "start_id": data.start_id,
            "end_id": data.end_id,
            "source": data.source,
            "story_name_en": data.story_name_en,
            "story_name_hi": data.story_name_hi,
            "description": data.description,
            "description_hi": data.description_hi,
            "episodes": data.episodes,
            "status": data.status,
            "genre": data.genre,
            "language": data.language,
            "price": data.price,
            "discount_price": data.discount_price,
            "payment_methods": data.payment_methods,
            "platform": data.platform,
            "delivery_mode": data.delivery_mode,
            "channel_id": data.channel_id,
            "image": data.image,
            "poster_url": data.poster_url,
            "is_completed": data.is_completed,
            "updated_via": "mini_app_admin"
        }
        await arya_db.save_story(story_doc)
        return {"success": True, "message": "Story saved successfully"}
    except Exception as e:
        logger.error(f"Error saving story: {e}")
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

app.include_router(api_router, prefix="/api")
app.include_router(api_router) # Handle both /api/stories and /stories for Nginx proxy compatibility

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("mini_app_api:app", host="0.0.0.0", port=8000, reload=True)
