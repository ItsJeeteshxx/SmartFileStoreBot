import os
import random
import string
import logging
import base64
from typing import Dict, List, Optional, Union, Any, Tuple

# ── CRITICAL: Load .env into os.environ BEFORE importing Config ───
# This must use __file__ (absolute script path), NOT the current working dir.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_SCRIPT_DIR)

import sys
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)

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

import uuid
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from purchase_dm_helper import send_purchase_success_dm
import httpx
try:
    import paytmchecksum
    PAYTM_LIBS_AVAILABLE = True
except ImportError:
    PAYTM_LIBS_AVAILABLE = False
import logging
import asyncio
import urllib.parse
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, APIRouter, HTTPException, Form, File, UploadFile, Request, WebSocket, WebSocketDisconnect, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from contextvars import ContextVar

admin_authenticated_session: ContextVar[bool] = ContextVar("admin_authenticated_session", default=False)

import hmac
import hashlib
import time
import re

_TOKEN_CACHE = {
    "tokens": [],
    "last_updated": 0.0
}
_TOKEN_CACHE_TTL = 300  # 5 minutes

_admin_stats_cache = None
_admin_stats_cache_time = 0.0
ADMIN_STATS_CACHE_TTL = 30  # 30 seconds cache TTL

async def get_all_valid_bot_tokens(db) -> list:
    global _TOKEN_CACHE
    now = time.time()
    if _TOKEN_CACHE["tokens"] and (now - _TOKEN_CACHE["last_updated"]) < _TOKEN_CACHE_TTL:
        return _TOKEN_CACHE["tokens"]

    tokens_to_check = []

    # 1. AryaPremium config tokens
    try:
        from AryaPremium.config import Config as PremConfig
        for attr in ("MGMT_BOT_TOKEN", "BOT_TOKEN"):
            tok = getattr(PremConfig, attr, None)
            if tok and isinstance(tok, str) and tok.strip():
                tok = tok.strip()
                if tok not in tokens_to_check:
                    tokens_to_check.append(tok)
    except Exception as e:
        logger.warning(f"Failed to load PremConfig tokens: {e}")

    # 2. Env config tokens (direct env variable)
    add_tokens_env = os.environ.get("ADDITIONAL_BOT_TOKENS", "")
    if add_tokens_env:
        for tok in add_tokens_env.replace(",", " ").split():
            tok = tok.strip()
            if tok and tok not in tokens_to_check:
                tokens_to_check.append(tok)

    # 3. Direct parsing of parent/root config/env to prevent side-effects from exec_module
    root_db_name = "arya"
    root_bot_token = None
    try:
        this_dir = os.path.dirname(os.path.abspath(__file__))
        if os.path.basename(this_dir) == "AryaPremium":
            parent_dir = os.path.dirname(this_dir)
        else:
            parent_dir = this_dir
            
        parent_env_path = os.path.join(parent_dir, ".env")
        env_vars = {}
        if os.path.exists(parent_env_path):
            with open(parent_env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        env_vars[k.strip()] = v.strip().strip("'").strip('"')
                        
        root_db_name = env_vars.get("DATABASE_NAME") or os.environ.get("DATABASE_NAME", "arya")
        root_bot_token = env_vars.get("BOT_TOKEN") or os.environ.get("BOT_TOKEN", None)
    except Exception as e:
        logger.warning(f"Failed to parse parent env: {e}")

    if root_bot_token and isinstance(root_bot_token, str) and root_bot_token.strip():
        tok = root_bot_token.strip()
        if tok not in tokens_to_check:
            tokens_to_check.append(tok)

    # 4. Fetch share bots from root db
    try:
        if root_db_name:
            root_db = db.client[root_db_name]
            share_bots_doc = await root_db.global_stats.find_one({'_id': 'share_bots_list'})
            if share_bots_doc and 'bots' in share_bots_doc:
                for bot in share_bots_doc['bots']:
                    tok = bot.get('token')
                    if tok and isinstance(tok, str) and tok.strip():
                        tok = tok.strip()
                        if tok not in tokens_to_check:
                            tokens_to_check.append(tok)
    except Exception as e:
        logger.warning(f"Failed to fetch share bots list from root db: {e}")

    # 5. Fetch premium bots from premium database
    try:
        bots = await db.db.premium_bots.find().to_list(length=None)
        for b in bots:
            tok = b.get('token')
            if tok and isinstance(tok, str) and tok.strip():
                tok = tok.strip()
                if tok not in tokens_to_check:
                    tokens_to_check.append(tok)
    except Exception as e:
        logger.warning(f"Failed to fetch premium bots: {e}")

    # 6. Scan MongoDB cluster databases for tokens
    try:
        db_names = await db.client.list_database_names()
        for db_name in db_names:
            if db_name in ('admin', 'local', 'config', root_db_name):
                continue
            db_obj = db.client[db_name]
            # check premium_bots
            try:
                bots = await db_obj.premium_bots.find().to_list(length=None)
                for b in bots:
                    tok = b.get('token')
                    if tok and isinstance(tok, str) and tok.strip():
                        tok = tok.strip()
                        if tok not in tokens_to_check:
                            tokens_to_check.append(tok)
            except Exception:
                pass
            # check global_stats.share_bots_list
            try:
                share_bots_doc = await db_obj.global_stats.find_one({'_id': 'share_bots_list'})
                if share_bots_doc and 'bots' in share_bots_doc:
                    for bot in share_bots_doc['bots']:
                        tok = bot.get('token')
                        if tok and isinstance(tok, str) and tok.strip():
                            tok = tok.strip()
                            if tok not in tokens_to_check:
                                tokens_to_check.append(tok)
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Error scanning MongoDB cluster for tokens: {e}")

    # 7. Scan server directories for files containing bot tokens
    try:
        this_dir = os.path.dirname(os.path.abspath(__file__))
        if os.path.basename(this_dir) == "AryaPremium":
            parent_of_project = os.path.dirname(os.path.dirname(this_dir))
        else:
            parent_of_project = os.path.dirname(this_dir)
            
        if os.path.exists(parent_of_project):
            for root, dirs, files in os.walk(parent_of_project, topdown=True):
                depth = root[len(parent_of_project):].count(os.sep)
                if depth > 2:
                    dirs.clear()
                    continue
                dirs[:] = [d for d in dirs if d not in ('venv', '.git', 'node_modules', '__pycache__')]
                for file in files:
                    if file in ('.env', 'config.env', 'config.py'):
                        filepath = os.path.join(root, file)
                        try:
                            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                                content = f.read()
                                found = re.findall(r'\d{8,11}:[A-Za-z0-9_-]{34,40}', content)
                                for t in found:
                                    t_clean = t.strip()
                                    if t_clean not in tokens_to_check:
                                        tokens_to_check.append(t_clean)
                        except Exception:
                            pass
    except Exception as e:
        logger.warning(f"Error scanning filesystem for tokens: {e}")

    _TOKEN_CACHE["tokens"] = tokens_to_check
    _TOKEN_CACHE["last_updated"] = now
    return tokens_to_check

def verify_telegram_web_app_data(init_data: str, bot_token: str) -> dict:
    if not init_data or not bot_token:
        return {}
    try:
        parsed = urllib.parse.parse_qsl(init_data)
        data_dict = {k: v for k, v in parsed}
        if "hash" not in data_dict:
            return {}
        received_hash = data_dict.pop("hash")
        data_check_string = "\n".join(f"{k}={data_dict[k]}" for k in sorted(data_dict.keys()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if calculated_hash == received_hash:
            return data_dict
        else:
            masked_token = bot_token[:10] + "..." + bot_token[-5:] if len(bot_token) > 15 else "short"
            logger.debug(f"Signature verification failed for token {masked_token}. Calc: {calculated_hash}, Recv: {received_hash}")
    except Exception as e:
        logger.error(f"initData verification failed: {e}")
    return {}

def is_admin(telegram_id: str = "") -> bool:
    if admin_authenticated_session.get():
        return True
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

# ─────────────────────────────────────────────────────────────────
# 24-Hour Stale Orders Auto-Cleanup Worker
# ─────────────────────────────────────────────────────────────────
STALE_ORDER_HOURS = 24
CLEANUP_INTERVAL_SECS = 3600  # Check every 1 hour



async def _stale_orders_cleanup_worker(arya_db):
    """
    Background worker: archives pending/failed orders older than 24h.
    NEVER touches paid / approved / delivered orders.
    Invalidates buyers cache after each cleanup run.
    """
    STALE_STATUSES = ["pending", "failed", "rejected", "pending_gateway",
                      "waiting_screenshot", "expired", "unknown"]

    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_SECS)
            cutoff = datetime.now(timezone.utc) - timedelta(hours=STALE_ORDER_HOURS)
            archived_count = 0

            # Archive stale docs from orders collection
            async for doc in arya_db.db.orders.find(
                {"status": {"$in": STALE_STATUSES}, "created_at": {"$lt": cutoff}}
            ):
                try:
                    doc["original_collection"] = "orders"
                    doc["archived_at"] = datetime.now(timezone.utc)
                    doc["archive_reason"] = f"auto_cleanup_{STALE_ORDER_HOURS}h"
                    await arya_db.db.archived_orders.update_one(
                        {"_id": doc["_id"]}, {"$set": doc}, upsert=True
                    )
                    await arya_db.db.orders.delete_one({"_id": doc["_id"]})
                    archived_count += 1
                except Exception:
                    pass

            # Archive stale docs from premium_checkout collection
            CHECKOUT_STALE = ["rejected", "pending_gateway", "waiting_screenshot", "expired", "unknown"]
            async for doc in arya_db.db.premium_checkout.find(
                {"status": {"$in": CHECKOUT_STALE}, "created_at": {"$lt": cutoff}}
            ):
                try:
                    doc["original_collection"] = "premium_checkout"
                    doc["archived_at"] = datetime.now(timezone.utc)
                    doc["archive_reason"] = f"auto_cleanup_{STALE_ORDER_HOURS}h"
                    await arya_db.db.archived_orders.update_one(
                        {"_id": doc["_id"]}, {"$set": doc}, upsert=True
                    )
                    await arya_db.db.premium_checkout.delete_one({"_id": doc["_id"]})
                    archived_count += 1
                except Exception:
                    pass

            if archived_count > 0:
                invalidate_buyers_cache()
                logger.info(f"[AutoCleanup] Archived {archived_count} stale orders (>{STALE_ORDER_HOURS}h)")
            else:
                logger.debug("[AutoCleanup] No stale orders found")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[AutoCleanup] Worker error: {e}")
            await asyncio.sleep(300)


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
            
            # Premium Unified Ban System Indexes
            await arya_db.db.premium_bans.create_index("ips", background=True)
            await arya_db.db.premium_ban_activity.create_index([("timestamp", -1)], background=True)
            
            logger.info("✅ Database indexes verified/created in background")
            
            # Migrate legacy flagged bans to strictly banned status
            try:
                mig_res = await arya_db.db.premium_bans.update_many(
                    {"status": "flagged"},
                    {"$set": {"status": "banned"}}
                )
                if mig_res.modified_count > 0:
                    logger.info(f"✅ Database Migration: Updated {mig_res.modified_count} flagged records to strictly banned")
            except Exception as mig_err:
                logger.warning(f"Failed to migrate flagged records: {mig_err}")
                
            # Automatically scan and clean universal/carrier IPs from premium_bans collection
            try:
                bans_cursor = arya_db.db.premium_bans.find()
                async for ban in bans_cursor:
                    ips = ban.get("ips", [])
                    if ips:
                        universal_ips = [ip for ip in ips if _is_universal_ip(ip)]
                        if universal_ips:
                            clean_ips = [ip for ip in ips if not _is_universal_ip(ip)]
                            await arya_db.db.premium_bans.update_one(
                                {"_id": ban["_id"]},
                                {"$set": {"ips": clean_ips}}
                            )
            except Exception as clean_err:
                logger.warning(f"Failed to clean startup universal IPs: {clean_err}")
                
            # Automatically run auto-unblock system for all paid users on startup
            try:
                from database import db as root_db
                asyncio.create_task(root_db.auto_unblock_paid_users())
            except Exception as unblock_err:
                logger.warning(f"Failed to launch auto_unblock_paid_users: {unblock_err}")

            # ── Initialize global order counter from existing order count ──────────
            # Ensures new order IDs continue from where existing orders left off
            # (e.g. if 700 orders exist, next order number = 701+)
            try:
                from pymongo import ReturnDocument
                total_orders = await arya_db.db.orders.count_documents({})
                total_purchases = await arya_db.db.premium_purchases.count_documents({})
                total_count = max(total_orders, total_purchases)
                if total_count > 0:
                    # Set counter to at least total_count if it's lower
                    existing_counter = await arya_db.db.order_counters.find_one({"_key": "global_order_counter"})
                    current_seq = existing_counter.get("seq", 0) if existing_counter else 0
                    if current_seq < total_count:
                        await arya_db.db.order_counters.update_one(
                            {"_key": "global_order_counter"},
                            {"$set": {"seq": total_count}},
                            upsert=True
                        )
                        logger.info(f"[OrderCounter] Initialized counter to {total_count} (was {current_seq}, total orders: {total_count})")
                    else:
                        logger.info(f"[OrderCounter] Counter already at {current_seq}, no update needed")
                else:
                    logger.info("[OrderCounter] No existing orders found, counter starts at 1")
            except Exception as counter_err:
                logger.warning(f"Failed to initialize order counter: {counter_err}")

            # Create index on archived_orders for efficient queries
            try:
                await arya_db.db.archived_orders.create_index([("archived_at", -1)], background=True)
                await arya_db.db.archived_orders.create_index("user_id", background=True)
                await arya_db.db.archived_orders.create_index("original_collection", background=True)
            except Exception:
                pass

        except Exception as idx_err:
            logger.warning(f"Failed to create indexes: {idx_err}")

        # Launch 24-hour auto-cleanup background worker for pending/failed orders
        try:
            asyncio.create_task(_stale_orders_cleanup_worker(arya_db))
            logger.info("✅ Stale orders auto-cleanup worker started")
        except Exception as worker_err:
            logger.warning(f"Failed to launch stale orders cleanup worker: {worker_err}")

        # Ensure Poster Bot is completely disabled in DB
        try:
            await arya_db.db.mini_app_config.update_many(
                {"$or": [{"_id": "poster_bot_config"}, {"_key": "poster_bot_config"}]},
                {"$set": {"enabled": False}}
            )
        except Exception:
            pass
            
    except Exception as e:
        logger.error(f"DB connect failed: {e}")
        app.state.db = None
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

IMAGE_CACHE = {}
MAX_CACHE_ITEMS = 1000
CACHE_DIR = os.path.join(os.path.dirname(__file__), "data", "image_cache")
os.makedirs(CACHE_DIR, exist_ok=True)
UPLOADS_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOADS_DIR, exist_ok=True)

def get_cached_image(cache_key: str) -> bytes | None:
    if cache_key in IMAGE_CACHE:
        return IMAGE_CACHE[cache_key]
    filepath = os.path.join(CACHE_DIR, f"{cache_key}.webp")
    if os.path.exists(filepath):
        try:
            with open(filepath, "rb") as f:
                data = f.read()
            if len(IMAGE_CACHE) < MAX_CACHE_ITEMS:
                IMAGE_CACHE[cache_key] = data
            return data
        except Exception:
            pass
    return None

def save_cached_image(cache_key: str, data: bytes):
    if len(IMAGE_CACHE) < MAX_CACHE_ITEMS:
        IMAGE_CACHE[cache_key] = data
    try:
        filepath = os.path.join(CACHE_DIR, f"{cache_key}.webp")
        with open(filepath, "wb") as f:
            f.write(data)
    except Exception as e:
        logger.warning(f"Failed to save image to disk cache: {e}")

@api_router.post("/track")
async def track_client_telemetry(request: Request):
    """Logs frontend events, errors, and deep-link lifecycle metrics to backend logs."""
    try:
        data = await request.json()
        if "batch" in data and isinstance(data["batch"], list):
            for item in data["batch"]:
                event_type = item.get("event_type", "unknown")
                event_data = item.get("event_data", {})
                telegram_id = item.get("telegram_id", "0")
                logger.info(f"📱 [MINIAPP LOG BATCH] tg={telegram_id} event={event_type} payload={event_data}")
            return {"status": "ok", "count": len(data["batch"])}
        else:
            event_type = data.get("event_type", "unknown")
            event_data = data.get("event_data", {})
            telegram_id = data.get("telegram_id", "0")
            logger.info(f"📱 [MINIAPP LOG] tg={telegram_id} event={event_type} payload={event_data}")
            return {"status": "ok"}
    except Exception as e:
        logger.warning(f"Error in /track endpoint: {e}")
        return {"status": "error", "message": str(e)}

# ─────────────────────────────────────────────────────────────
# CRASHLYTICS & ERROR TRACKING SYSTEM
# ─────────────────────────────────────────────────────────────
@api_router.post("/crash-report")
async def report_client_crash(request: Request):
    """
    Collects real-time frontend crash reports, component stack traces,
    and user breadcrumbs, deduplicating and indexing them in MongoDB.
    """
    try:
        payload = await request.json()
        error_msg = str(payload.get("message") or payload.get("error_message") or "Unknown Error").strip()
        stack = str(payload.get("stack") or "").strip()
        component_stack = str(payload.get("componentStack") or payload.get("component_stack") or "").strip()
        view_name = str(payload.get("view") or payload.get("view_name") or "unknown").strip()
        url = str(payload.get("url") or payload.get("href") or "").strip()
        
        # User details
        tg_id = str(payload.get("telegram_id") or payload.get("tg_id") or "0").strip()
        username = str(payload.get("username") or "").strip()
        first_name = str(payload.get("first_name") or "").strip()
        
        # Device details
        device = payload.get("device") or {}
        platform = str(payload.get("platform") or device.get("platform") or "unknown").strip()
        user_agent = str(payload.get("user_agent") or device.get("userAgent") or "").strip()
        breadcrumbs = payload.get("breadcrumbs") or []
        
        # Unique hash for grouping identical errors
        first_stack_line = stack.split("\n")[0] if stack else ""
        error_hash = hashlib.md5(f"{error_msg}::{first_stack_line}::{view_name}".encode()).hexdigest()
        
        arya_db = app.state.db
        now_dt = datetime.now(timezone.utc)
        now_str = now_dt.isoformat()
        
        # Upsert crash document in MongoDB
        await arya_db.db.frontend_crashes.update_one(
            {"error_hash": error_hash},
            {
                "$set": {
                    "error_message": error_msg,
                    "stack": stack,
                    "component_stack": component_stack,
                    "view_name": view_name,
                    "url": url,
                    "platform": platform,
                    "user_agent": user_agent,
                    "breadcrumbs": breadcrumbs[-10:],
                    "last_seen": now_str,
                    "last_seen_dt": now_dt,
                    "last_user": {
                        "telegram_id": tg_id,
                        "username": username,
                        "first_name": first_name,
                    },
                    "status": "open"
                },
                "$setOnInsert": {
                    "error_hash": error_hash,
                    "first_seen": now_str,
                    "first_seen_dt": now_dt,
                    "created_at": now_dt
                },
                "$inc": {"occurrences_count": 1},
                "$addToSet": {"affected_users": tg_id if tg_id != "0" else "guest"}
            },
            upsert=True
        )
        
        logger.info(f"🚨 [CRASHLYTICS REPORT] Hash={error_hash[:8]} Msg='{error_msg[:60]}' View={view_name} User={tg_id}")
        return {"success": True, "error_hash": error_hash}
    except Exception as e:
        logger.warning(f"Failed to record crash report: {e}")
        return {"success": False, "error": str(e)}

@api_router.get("/admin/crashes")
async def get_admin_crashes(telegram_id: str = Query(""), status: str = Query("all")):
    """Fetch aggregated crash reports and statistics for the Admin Panel Crashlytics dashboard."""
    try:
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
            
        arya_db = app.state.db
        query = {}
        if status in ("open", "resolved"):
            query["status"] = status
            
        cursor = arya_db.db.frontend_crashes.find(query).sort("last_seen_dt", -1).limit(100)
        crashes = []
        total_occurrences = 0
        all_affected_users = set()
        open_count = 0
        resolved_count = 0
        
        async for doc in cursor:
            occ = doc.get("occurrences_count", 1)
            total_occurrences += occ
            users_list = doc.get("affected_users", [])
            for u in users_list:
                all_affected_users.add(u)
            if doc.get("status") == "resolved":
                resolved_count += 1
            else:
                open_count += 1
                
            crashes.append({
                "id": str(doc["_id"]),
                "error_hash": doc.get("error_hash", ""),
                "error_message": doc.get("error_message", "Unknown Error"),
                "stack": doc.get("stack", ""),
                "component_stack": doc.get("component_stack", ""),
                "view_name": doc.get("view_name", "unknown"),
                "url": doc.get("url", ""),
                "platform": doc.get("platform", "unknown"),
                "user_agent": doc.get("user_agent", ""),
                "breadcrumbs": doc.get("breadcrumbs", []),
                "occurrences_count": occ,
                "affected_users_count": len(users_list),
                "affected_users": users_list[:10],
                "last_user": doc.get("last_user", {}),
                "first_seen": doc.get("first_seen", ""),
                "last_seen": doc.get("last_seen", ""),
                "status": doc.get("status", "open")
            })
            
        return {
            "success": True,
            "data": {
                "summary": {
                    "total_unique_crashes": len(crashes),
                    "open_count": open_count,
                    "resolved_count": resolved_count,
                    "total_events": total_occurrences,
                    "total_affected_users": len(all_affected_users)
                },
                "crashes": crashes
            }
        }
    except Exception as e:
        logger.error(f"Error fetching admin crashes: {e}")
        return {"success": False, "data": {"summary": {}, "crashes": []}}

@api_router.post("/admin/crashes/{crash_id}/status")
async def toggle_crash_status(crash_id: str, payload: dict = Body(...), telegram_id: str = Query("")):
    """Toggle or update crash resolution status."""
    try:
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
            
        arya_db = app.state.db
        new_status = payload.get("status", "resolved")
        await arya_db.db.frontend_crashes.update_one(
            {"_id": ObjectId(crash_id)},
            {"$set": {"status": new_status, "updated_at": datetime.now(timezone.utc)}}
        )
        return {"success": True, "status": new_status}
    except Exception as e:
        return {"success": False, "error": str(e)}

@api_router.delete("/admin/crashes/{crash_id}")
async def delete_single_crash(crash_id: str, telegram_id: str = Query("")):
    """Delete a single crash report."""
    try:
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
            
        arya_db = app.state.db
        await arya_db.db.frontend_crashes.delete_one({"_id": ObjectId(crash_id)})
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}

@api_router.delete("/admin/crashes/clear-all")
async def clear_all_crashes(telegram_id: str = Query(""), filter_type: str = Query("resolved")):
    """Clear crashes (all or only resolved)."""
    try:
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
            
        arya_db = app.state.db
        q = {"status": "resolved"} if filter_type == "resolved" else {}
        res = await arya_db.db.frontend_crashes.delete_many(q)
        return {"success": True, "deleted_count": res.deleted_count}
    except Exception as e:
        return {"success": False, "error": str(e)}

@api_router.get("/image")
async def optimize_image(request: Request, url: str, w: int = 400, h: int = 400):
    """
    Acts as an Image Proxy: Fetches external image (like Catbox / R2), converts to WebP,
    compresses to maintain visual quality without large file size, and caches it persistently.
    """
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Invalid URL")

    # Check cache (RAM -> Disk)
    cache_key = hashlib.md5(f"{url}_{w}_{h}".encode()).hexdigest()
    etag = f'"{cache_key}"'

    # Check HTTP 304 Not Modified
    if_none_match = request.headers.get("if-none-match")
    if if_none_match and etag in if_none_match:
        return Response(status_code=304, headers={"Cache-Control": "public, max-age=31536000, immutable", "ETag": etag})

    cached_bytes = get_cached_image(cache_key)
    if cached_bytes:
        return Response(content=cached_bytes, media_type="image/webp", headers={"Cache-Control": "public, max-age=31536000, immutable", "ETag": etag})

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status != 200:
                    raise HTTPException(status_code=404, detail="Image not found")
                img_bytes = await resp.read()

        import asyncio
        def process_image(img_data):
            img = Image.open(io.BytesIO(img_data))
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA")
            img.thumbnail((w, h), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            quality = 95 if w > 600 else 82
            img.save(output, format="WEBP", quality=quality, method=3)
            return output.getvalue()

        optimized_bytes = await asyncio.to_thread(process_image, img_bytes)
        save_cached_image(cache_key, optimized_bytes)

        return Response(
            content=optimized_bytes, 
            media_type="image/webp",
            headers={"Cache-Control": "public, max-age=31536000, immutable", "ETag": f'"{cache_key}"'}
        )
    except Exception as e:
        logger.error(f"Image proxy error for {url}: {e}")
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=url)
async def get_customer_bot_details(user_id: int, order_bot_id: Optional[Union[int, str]] = None) -> tuple:
    """
    Resolves the customer-facing bot token and username for a user.
    CRITICAL: Never routes to 'show_store' mode bots for receipts or support.
    Priority:
    1. order_bot_id if provided and NOT a show_store bot.
    2. Dedicated 'miniapp' / 'mini_app' bot.
    3. Active non-show_store bot from user's interaction history (user_doc['bot_ids']).
    4. Any active non-show_store bot in the database.
    5. Fallback to main BOT_TOKEN or MGMT_BOT_TOKEN.
    """
    from AryaPremium.config import Config
    
    main_bot_token = None
    try:
        from config import Config as MainConfig
        main_bot_token = getattr(MainConfig, "BOT_TOKEN", None)
    except Exception:
        pass
    
    if not main_bot_token:
        main_bot_token = getattr(Config, "BOT_TOKEN", None)

    default_un = os.environ.get("BOT_USERNAME", "UseAryaBot")
    token = None
    bot_username = default_un

    try:
        arya_db = getattr(app.state, "db", None)
        if arya_db and hasattr(arya_db, "db") and arya_db.db is not None:
            # 1. Check order_bot_id first
            if order_bot_id:
                try:
                    bid_int = int(order_bot_id)
                    b_match = await arya_db.db.premium_bots.find_one({"$or": [{"id": bid_int}, {"bot_id": bid_int}]})
                    if b_match and b_match.get("token"):
                        b_mode = (b_match.get("config") or {}).get("bot_mode", "full")
                        if b_mode != "show_store":
                            return b_match["token"], b_match.get("username", bot_username)
                except Exception:
                    pass

            # 2. Look for dedicated Mini App mode bot
            mini_bot = await arya_db.db.premium_bots.find_one({
                "config.bot_mode": {"$in": ["miniapp", "mini_app"]},
                "token": {"$exists": True, "$ne": ""}
            })
            if mini_bot and mini_bot.get("token"):
                return mini_bot["token"], mini_bot.get("username", bot_username)

            # 3. Check user's interacted bots, filtering out show_store bots
            user_doc = await arya_db.db.users.find_one({"id": int(user_id)})
            if user_doc and user_doc.get("bot_ids"):
                for bid in user_doc["bot_ids"]:
                    try:
                        b_doc = await arya_db.db.premium_bots.find_one({"$or": [{"id": int(bid)}, {"bot_id": int(bid)}]})
                        if b_doc and b_doc.get("token"):
                            b_mode = (b_doc.get("config") or {}).get("bot_mode", "full")
                            if b_mode != "show_store":
                                return b_doc["token"], b_doc.get("username", bot_username)
                    except Exception:
                        continue

            # 4. Fallback to any active non-show_store bot in the DB
            any_store_bot = await arya_db.db.premium_bots.find_one({
                "config.bot_mode": {"$nin": ["show_store"]},
                "token": {"$exists": True, "$ne": ""}
            })
            if any_store_bot and any_store_bot.get("token"):
                return any_store_bot["token"], any_store_bot.get("username", bot_username)

    except Exception as e:
        logger.error(f"Failed to resolve customer bot details: {e}")

    final_token = token or main_bot_token or getattr(Config, "MGMT_BOT_TOKEN", None) or os.environ.get("BOT_TOKEN", "")
    return final_token, bot_username


async def get_customer_bot_token(user_id: int, order_bot_id: Optional[Union[int, str]] = None) -> str:
    """Resolves the user-facing customer bot token for a user, guaranteeing non-show-store bot."""
    token, _ = await get_customer_bot_details(user_id, order_bot_id=order_bot_id)
    return token


@api_router.get("/image-proxy")
async def image_proxy(url: str, w: int = 400, h: int = 400):
    """Proxies and caches external images (such as Catbox URLs) to bypass ISP blocks in India and optimize to WebP."""
    if not url:
        raise HTTPException(status_code=400, detail="Missing url parameter")
        
    url_clean = str(url).strip()
    cache_key = hashlib.md5(f"proxy_{url_clean}_{w}_{h}".encode()).hexdigest()
    cached_bytes = get_cached_image(cache_key)
    if cached_bytes:
        return Response(content=cached_bytes, media_type="image/webp", headers={"Cache-Control": "public, max-age=31536000, immutable", "ETag": f'"{cache_key}"'})
        
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url_clean, timeout=12) as resp:
                if resp.status != 200:
                    raise HTTPException(status_code=resp.status, detail="Failed to fetch image from host")
                img_bytes = await resp.read()
                
        import asyncio
        def process_image(img_data):
            img = Image.open(io.BytesIO(img_data))
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA")
            img.thumbnail((w, h), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            quality = 85 if w > 600 else 80
            img.save(output, format="WEBP", quality=quality, method=2)
            return output.getvalue()
            
        optimized_bytes = await asyncio.to_thread(process_image, img_bytes)
        save_cached_image(cache_key, optimized_bytes)
        
        return Response(
            content=optimized_bytes,
            media_type="image/webp",
            headers={"Cache-Control": "public, max-age=31536000, immutable", "ETag": f'"{cache_key}"'}
        )
    except Exception as e:
        logger.warning(f"Image proxy error for {url_clean}: {e}")
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=url_clean)


@api_router.get("/tg-image")
async def tg_image_proxy(file_id: str, bot_id: str = None, w: int = 400, h: int = 400):
    """Fetches image directly from Telegram or HTTP URL, optimizes to WebP and caches it."""
    from AryaPremium.config import Config
    
    if not file_id:
        raise HTTPException(status_code=400, detail="Missing file_id")
        
    file_id_clean = str(file_id).strip()
    
    # If file_id is actually an HTTP URL (e.g. Catbox/external), proxy it
    if file_id_clean.startswith("http://") or file_id_clean.startswith("https://"):
        return await image_proxy(file_id_clean, w, h)
        
    tokens = []
    arya_db = app.state.db
    if bot_id:
        try:
            bot_doc = await arya_db.db.premium_bots.find_one({"$or": [{"id": int(bot_id)}, {"bot_id": int(bot_id)}]}) if arya_db else None
            if bot_doc and bot_doc.get("token"):
                tokens.append(bot_doc["token"])
        except Exception as e:
            logger.error(f"Failed to fetch bot token for {bot_id}: {e}")
            
    if Config.MGMT_BOT_TOKEN and Config.MGMT_BOT_TOKEN not in tokens:
        tokens.append(Config.MGMT_BOT_TOKEN)
    if os.environ.get("MGMT_BOT_TOKEN") and os.environ.get("MGMT_BOT_TOKEN") not in tokens:
        tokens.append(os.environ.get("MGMT_BOT_TOKEN"))
    if Config.BOT_TOKEN and Config.BOT_TOKEN not in tokens:
        tokens.append(Config.BOT_TOKEN)
        
    if arya_db:
        try:
            other_bots = await arya_db.db.premium_bots.find({"token": {"$exists": True, "$ne": ""}}).to_list(length=10)
            for ob in other_bots:
                tok = ob.get("token")
                if tok and tok not in tokens:
                    tokens.append(tok)
        except Exception:
            pass
            
    if not tokens:
        raise HTTPException(status_code=500, detail="No bot token available")
        
    cache_key = hashlib.md5(f"tg_{file_id_clean}_{w}_{h}".encode()).hexdigest()
    cached_bytes = get_cached_image(cache_key)
    if cached_bytes:
        return Response(content=cached_bytes, media_type="image/webp", headers={"Cache-Control": "public, max-age=31536000, immutable", "ETag": f'"{cache_key}"'})
        
    img_bytes = None
    async with aiohttp.ClientSession() as session:
        for token in tokens:
            try:
                # 1. Get file path
                async with session.get(f"https://api.telegram.org/bot{token}/getFile", params={"file_id": file_id_clean}, timeout=8) as resp:
                    data = await resp.json()
                    if not data.get("ok"):
                        continue
                    file_path = data["result"]["file_path"]
                    
                # 2. Download file
                dl_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
                async with session.get(dl_url, timeout=12) as resp:
                    if resp.status == 200:
                        img_bytes = await resp.read()
                        if img_bytes:
                            break
            except Exception:
                continue

    if not img_bytes:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="https://images.unsplash.com/photo-1614729939124-032f0b56c9ce?w=400")
        
    # Optimize using Pillow in a separate thread
    import asyncio
    def process_image(img_data):
        img = Image.open(io.BytesIO(img_data))
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA")
        img.thumbnail((w, h), Image.Resampling.BILINEAR)
        output = io.BytesIO()
        quality = 85 if w > 600 else 80
        img.save(output, format="WEBP", quality=quality)
        return output.getvalue()
        
    optimized_bytes = await asyncio.to_thread(process_image, img_bytes)
    save_cached_image(cache_key, optimized_bytes)
    
    return Response(
        content=optimized_bytes, 
        media_type="image/webp",
        headers={"Cache-Control": "public, max-age=31536000, immutable", "ETag": f'"{cache_key}"'}
    )


@api_router.get("/uploads/{filename}")
@app.get("/uploads/{filename}")
async def get_uploaded_image(filename: str):
    """Serves locally uploaded poster/banner images."""
    clean_name = os.path.basename(str(filename).strip())
    filepath = os.path.join(UPLOADS_DIR, clean_name)
    if os.path.exists(filepath) and os.path.isfile(filepath):
        return FileResponse(filepath, headers={"Cache-Control": "public, max-age=31536000, immutable"})
    raise HTTPException(status_code=404, detail="Image not found")


@api_router.get("/r2-image")
async def r2_image_handler(key: str, w: int = 600, h: int = 600):
    """Fetches image from Cloudflare R2 bucket using S3 credentials, caches and serves it as WebP."""
    key_clean = str(key).strip()
    if "/" in key_clean:
        key_clean = key_clean.split("/")[-1]
    
    cache_key = hashlib.md5(f"r2_{key_clean}_{w}_{h}".encode()).hexdigest()
    cached_bytes = get_cached_image(cache_key)
    if cached_bytes:
        return Response(content=cached_bytes, media_type="image/webp", headers={"Cache-Control": "public, max-age=31536000, immutable", "ETag": f'"{cache_key}"'})
    
    # 1. Try local uploads first
    local_path = os.path.join(UPLOADS_DIR, key_clean)
    if os.path.exists(local_path):
        try:
            with open(local_path, "rb") as f:
                img_data = f.read()
            save_cached_image(cache_key, img_data)
            return Response(content=img_data, media_type="image/webp", headers={"Cache-Control": "public, max-age=31536000, immutable", "ETag": f'"{cache_key}"'})
        except Exception:
            pass

    # 2. Try fetching from Cloudflare R2 using S3 credentials
    from decouple import config
    r2_account_id = config("R2_ACCOUNT_ID", default="") or os.environ.get("R2_ACCOUNT_ID", "")
    r2_access_key = config("R2_ACCESS_KEY_ID", default="") or config("R2_ACCESS_KEY", default="") or os.environ.get("R2_ACCESS_KEY_ID", "")
    r2_secret_key = config("R2_SECRET_ACCESS_KEY", default="") or config("R2_SECRET_KEY", default="") or os.environ.get("R2_SECRET_ACCESS_KEY", "")
    r2_bucket = config("R2_BUCKET_NAME", default="") or config("R2_BUCKET", default="arya-images") or os.environ.get("R2_BUCKET_NAME", "arya-images")

    if r2_account_id and r2_access_key and r2_secret_key:
        import boto3
        try:
            def fetch_r2():
                s3 = boto3.client(
                    "s3",
                    endpoint_url=f"https://{r2_account_id}.r2.cloudflarestorage.com",
                    aws_access_key_id=r2_access_key,
                    aws_secret_access_key=r2_secret_key,
                    region_name="auto"
                )
                obj = s3.get_object(Bucket=r2_bucket, Key=key_clean)
                return obj["Body"].read()
            img_data = await asyncio.to_thread(fetch_r2)
            if img_data:
                save_cached_image(cache_key, img_data)
                return Response(content=img_data, media_type="image/webp", headers={"Cache-Control": "public, max-age=31536000, immutable", "ETag": f'"{cache_key}"'})
        except Exception as e:
            logger.warning(f"R2 fetch error for {key_clean}: {e}")

    # 3. Fallback placeholder
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="https://images.unsplash.com/photo-1614729939124-032f0b56c9ce?w=400")

# ————————————————————————————————————————————————————————————————————————————————————————————————————
# Helper: format a single MongoDB story doc → frontend Story shape
# ————————————————————————————————————————————————————————————————————————————————————————————————————
def _format_story(s: dict) -> dict | None:
    # STRICT EXCLUSION: Story TV, Kuku TV, and OTT Video Shows must NEVER appear anywhere in the Arya Premium Mini App
    if s.get("is_show") is True:
        return None
    p_name = str(s.get("platform") or "").strip().lower()
    if any(p in p_name for p in ("kuku tv", "story tv", "kukutv", "storytv")):
        return None

    # Filter out hidden stories in public endpoints
    vis = str(s.get("visibility") or "").strip().lower()
    stat = str(s.get("status") or "").strip().lower()
    if vis == "hidden" or stat == "hidden":
        return None

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
    raw_cover = (
        s.get("poster_url")
        or s.get("image_url")
        or s.get("cover")
        or s.get("poster")
        or s.get("banner_url")
        or s.get("banner")
        or s.get("image")       # Telegram file_id (mgmt bot saves this)
        or "https://images.unsplash.com/photo-1614729939124-032f0b56c9ce?w=400"
    )
    raw_cover_str = str(raw_cover).strip() if raw_cover else ""
    bot_id = s.get("bot_id")
    
    if "r2.cloudflarestorage.com" in raw_cover_str:
        obj_key = raw_cover_str.split("/")[-1]
        cover = f"/api/r2-image?key={obj_key}"
    elif raw_cover_str and not raw_cover_str.startswith("http") and not raw_cover_str.startswith("/api/"):
        cover = f"/api/tg-image?file_id={raw_cover_str}" + (f"&bot_id={bot_id}" if bot_id else "")
    else:
        cover = raw_cover_str

    raw_banner = s.get("banner_url") or s.get("banner") or s.get("poster_url") or raw_cover
    raw_banner_str = str(raw_banner).strip() if raw_banner else ""
    if "r2.cloudflarestorage.com" in raw_banner_str:
        obj_key = raw_banner_str.split("/")[-1]
        banner = f"/api/r2-image?key={obj_key}"
    elif raw_banner_str and not raw_banner_str.startswith("http") and not raw_banner_str.startswith("/api/"):
        banner = f"/api/tg-image?file_id={raw_banner_str}" + (f"&bot_id={bot_id}" if bot_id else "")
    else:
        banner = raw_banner_str

    raw_status = str(s.get("status") or "").strip()
    status_lower = raw_status.lower()
    if status_lower == "completed":
        status_val = "Completed"
    elif status_lower == "ongoing":
        status_val = "Ongoing"
    elif status_lower == "unfinished":
        status_val = "Unfinished"
    elif status_lower in ("stucked", "stuck"):
        status_val = "Stucked"
    else:
        is_comp = bool(s.get("is_completed") or s.get("completed"))
        status_val = "Completed" if is_comp else "Ongoing"

    raw_ep = s.get("enable_parts")
    raw_parts = s.get("parts") or s.get("story_parts") or s.get("episode_parts") or s.get("episodes_parts") or s.get("part_list") or s.get("episode_ranges") or s.get("sub_parts") or []
    parts_list = raw_parts if isinstance(raw_parts, list) else []
    
    if isinstance(raw_ep, bool):
        enable_parts_bool = raw_ep
    elif isinstance(raw_ep, str):
        enable_parts_bool = raw_ep.strip().lower() in ("true", "1", "yes", "on")
    else:
        enable_parts_bool = bool(raw_ep)

    story_end_id = int(s.get("end_id") or s.get("end_message_id") or 0)
    story_episodes_val = str(s.get("episodes") or s.get("ep_count") or s.get("total_eps") or "").strip()
    
    cleaned_parts = []
    found_ongoing = False
    for idx, p in enumerate(parts_list):
        if isinstance(p, dict):
            b_val = str(p.get("badge") or p.get("badge_type") or ("ongoing" if p.get("is_ongoing") else "new" if p.get("is_new") else "none")).lower()
            is_new_val = bool(p.get("is_new") or b_val == "new")
            is_ongoing_val = bool(p.get("is_ongoing") or b_val == "ongoing")
            is_last_part = (idx == len(parts_list) - 1)
            
            p_start = int(p.get("start_id") or 0)
            p_end = int(p.get("end_id") or 0)
            p_episodes = str(p.get("episodes") or "").strip()
            
            # If this part is ongoing (or is the last part of an ongoing story), auto-include latest episodes and end_id
            if is_ongoing_val or (not found_ongoing and is_last_part and status_val == "Ongoing"):
                is_ongoing_val = True
                b_val = "ongoing"
                found_ongoing = True
                if story_end_id > p_end:
                    p_end = story_end_id
                if story_episodes_val and story_episodes_val.isdigit():
                    import re
                    m = re.search(r"(\d+)", p_episodes)
                    if m:
                        p_episodes = f"{m.group(1)}-{story_episodes_val}"
                    elif not p_episodes:
                        p_episodes = story_episodes_val
            
            cleaned_parts.append({
                "id": str(p.get("id") or f"part_{idx+1}"),
                "name": str(p.get("name") or f"Part {idx+1}"),
                "name_hi": p.get("name_hi"),
                "start_id": p_start,
                "end_id": p_end,
                "episodes": p_episodes,
                "price": float(p.get("price") or 0),
                "badge": b_val,
                "badge_type": b_val,
                "is_new": is_new_val,
                "is_ongoing": is_ongoing_val,
            })

    # If parts list has items, ensure enable_parts is True
    if len(cleaned_parts) > 0 and raw_ep is not False and str(raw_ep).lower() != "false":
        enable_parts_bool = True

    c_at = s.get("created_at") or s.get("uploaded_at") or s.get("createdAt")
    c_at_str = ""
    if isinstance(c_at, (int, float)):
        try:
            c_at_str = datetime.fromtimestamp(c_at, tz=timezone.utc).isoformat()
        except Exception:
            c_at_str = ""
    elif isinstance(c_at, datetime):
        c_at_str = c_at.isoformat()
    elif isinstance(c_at, str) and c_at.strip():
        c_at_str = c_at.strip()
    if not c_at_str:
        c_at_str = datetime.now(timezone.utc).isoformat()

    return {
        "id":           story_id,
        "story_id":     s.get("story_id") or story_id,
        "title":        title,
        "titleHi":      (s.get("story_name_hi") or "").strip() or None,
        "titleHin":     (s.get("story_name_hin") or "").strip() or None,
        "description":  description,
        "descriptionHi": (s.get("description_hi") or "").strip() or None,
        "descriptionHin": (s.get("description_hin") or "").strip() or None,
        "poster":       cover,
        "banner":       banner,
        "cover":        cover,
        "image":        cover,
        "poster_url":   cover,
        "image_url":    cover,
        "banner_url":   banner,
        "cover_url":    cover,
        "thumbnail":    cover,
        "title_position": s.get("title_position") or "left",
        "price":        float(s.get("price") or 0),
        "language":     s.get("language") or "Hindi",
        "platform":     s.get("platform") or "Pocket FM",
        "genre":        s.get("genre") or "Drama",
        "status":       status_val,
        "visibility":   s.get("visibility") or "Available",
        "bot_username":  s.get("bot_username") or "UseAryaBot",
        "episodes":     s.get("episodes") or s.get("ep_count") or s.get("total_eps") or "?",
        "totalEpisodes":s.get("episodes") or s.get("total_eps") or s.get("ep_count") or "?",
        "size":         s.get("total_size") or s.get("size") or None,
        "isCompleted":  status_val == "Completed",
        "fileCount":    s.get("file_count") or (len(s.get("valid_file_ids")) if s.get("valid_file_ids") else None) or s.get("fileCount") or (abs(s.get('end_id', 0) - s.get('start_id', 0)) + 1 if s.get('end_id') and s.get('start_id') else None),
        "enable_parts": enable_parts_bool,
        "is_show":       bool(s.get("is_show", False)),
        "author":        s.get("author") or "",
        "duration":      s.get("duration") or "",
        "is_must_have":  bool(s.get("is_must_have", False)),
        "show_checkout_warning": bool(s.get("show_checkout_warning", False)),
        "series_id":    str(s.get("series_id")) if s.get("series_id") else None,
        "created_at":    c_at_str,
    }


# ————————————————————————————————————————————————————————————————————————————————————————————————————
# GET /stories
# ————————————————————————————————————————————————————————————————————————————————————————————————————
_stories_cache = None
_stories_cache_time = 0
_stories_cache_ttl = 5  # 5 seconds fast cache

@api_router.get("/series")
async def get_series():
    """Fetch all active series"""
    try:
        arya_db = app.state.db
        series_cursor = arya_db.db.series.find({"is_active": {"$ne": False}})
        series_list = []
        async for doc in series_cursor:
            doc["id"] = str(doc.get("_id", ""))
            doc.pop("_id", None)
            series_list.append(doc)
        return series_list
    except Exception as e:
        logger.error(f"Error fetching series: {e}")
        return []

@api_router.get("/stories")
async def get_stories():
    """Fetch all premium stories with dynamic engagement counts from orders and analytics collections"""
    global _stories_cache, _stories_cache_time
    import time
    if _stories_cache is not None and (time.time() - _stories_cache_time) < _stories_cache_ttl:
        logger.info("Returning cached stories (memory cache hit)")
        return _stories_cache
    try:
        arya_db = app.state.db
        stories = await arya_db.get_all_stories()

        # 1. Aggregate purchases (60-day / all-time) for Popular on AP ranking
        purchase_map = {}
        try:
            sixty_days_ago = datetime.now(timezone.utc) - timedelta(days=60)
            purchase_pipeline = [
                {"$match": {
                    "status": {"$in": ["paid", "delivered"]},
                    "created_at": {"$gte": sixty_days_ago}
                }},
                {"$unwind": "$story_ids"},
                {"$group": {
                    "_id": "$story_ids",
                    "purchases": {"$sum": 1}
                }}
            ]
            import asyncio
            purchases_agg = await asyncio.wait_for(
                arya_db.db.orders.aggregate(purchase_pipeline).to_list(length=None),
                timeout=2.5
            )
            purchase_map = {str(p["_id"]): int(p.get("purchases", 0)) for p in purchases_agg}
        except asyncio.TimeoutError:
            logger.debug("Purchase aggregation timed out (>2.5s) — using default rankings.")
        except Exception as pe:
            logger.warning(f"Failed to aggregate purchases: {pe}")

        # 2. Aggregate views & searches over the last 3 days for real-time Trending
        views_map = {}
        searches_map = {}
        recent_purchases_map = {}
        try:
            three_days_ago = datetime.now(timezone.utc) - timedelta(days=3)
            analytics_pipeline = [
                {"$match": {
                    "type": {"$in": ["view_story", "click_story", "search_story", "story_search", "search"]},
                    "timestamp": {"$gte": three_days_ago}
                }},
                {"$project": {
                    "type": "$type",
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
                    "_id": {"story_id": "$story_id", "type": "$type"},
                    "count": {"$sum": 1}
                }}
            ]
            import asyncio
            analytics_agg = await asyncio.wait_for(
                arya_db.db.mini_app_analytics.aggregate(analytics_pipeline).to_list(length=None),
                timeout=2.5
            )
            for a in analytics_agg:
                sid = str(a["_id"].get("story_id"))
                atype = str(a["_id"].get("type", "")).lower()
                count = int(a.get("count", 0))
                if "search" in atype:
                    searches_map[sid] = searches_map.get(sid, 0) + count
                else:
                    views_map[sid] = views_map.get(sid, 0) + count
        except asyncio.TimeoutError:
            logger.debug("Analytics aggregation timed out (>2.5s) — using default metrics.")
        except Exception as ae:
            logger.warning(f"Failed to aggregate analytics: {ae}")
                    
            recent_p_pipeline = [
                {"$match": {
                    "status": {"$in": ["paid", "delivered"]},
                    "created_at": {"$gte": three_days_ago}
                }},
                {"$unwind": "$story_ids"},
                {"$group": {
                    "_id": "$story_ids",
                    "purchases": {"$sum": 1}
                }}
            ]
            recent_p_agg = await asyncio.wait_for(
                arya_db.db.orders.aggregate(recent_p_pipeline).to_list(length=None),
                timeout=0.6
            )
            recent_purchases_map = {str(p["_id"]): int(p.get("purchases", 0)) for p in recent_p_agg}
        except Exception as ve:
            logger.warning(f"Failed to aggregate trending analytics: {ve}")

        formatted = []
        for s in stories:
            item = _format_story(s)
            if item:
                sid = item["id"]
                item["purchase_count"] = purchase_map.get(sid, 0)
                item["view_count"] = views_map.get(sid, 0)
                item["search_count"] = searches_map.get(sid, 0)
                # Trending score: dynamic daily views (weight 4.0) + searches (weight 3.0) + recent 3-day purchases (weight 15.0)
                item["trending_score"] = float(
                    views_map.get(sid, 0) * 4.0 +
                    searches_map.get(sid, 0) * 3.0 +
                    recent_purchases_map.get(sid, 0) * 15.0
                )
                formatted.append(item)

        parts_enabled_count = sum(1 for item in formatted if item.get("enable_parts") or (item.get("parts") and len(item.get("parts")) > 0))
        logger.info(f"Returning {len(formatted)} stories ({parts_enabled_count} with parts enabled) with dynamic engagement metrics")
        res = {"success": True, "data": formatted}
        _stories_cache = res
        _stories_cache_time = time.time()
        return res

    except Exception as e:
        logger.error(f"Error in /stories: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/debug-parts")
async def debug_parts(id: str = None):
    """Debug endpoint to inspect stories with parts in MongoDB vs formatted output"""
    try:
        arya_db = app.state.db
        from bson.objectid import ObjectId
        
        query = {}
        if id:
            try:
                query = {"$or": [{"_id": ObjectId(id)}, {"_id": id}, {"story_id": id}]}
            except Exception:
                query = {"$or": [{"_id": id}, {"story_id": id}]}
        else:
            query = {
                "$or": [
                    {"enable_parts": {"$in": [True, "true", "True", "1", 1]}},
                    {"parts": {"$exists": True, "$ne": []}},
                    {"story_parts": {"$exists": True, "$ne": []}},
                    {"episode_parts": {"$exists": True, "$ne": []}}
                ]
            }

        docs = await arya_db.db.premium_stories.find(query).to_list(length=50)
        results = []
        for d in docs:
            raw_id = str(d.get("_id"))
            formatted = _format_story(d)
            results.append({
                "_id": raw_id,
                "story_name_en": d.get("story_name_en"),
                "story_name_hi": d.get("story_name_hi"),
                "raw_enable_parts": d.get("enable_parts"),
                "raw_parts_count": len(d.get("parts") or d.get("story_parts") or d.get("episode_parts") or []),
                "raw_parts": d.get("parts") or d.get("story_parts") or d.get("episode_parts") or [],
                "formatted_enable_parts": formatted.get("enable_parts") if formatted else None,
                "formatted_parts_count": len(formatted.get("parts", [])) if formatted else 0,
                "formatted_parts": formatted.get("parts") if formatted else [],
            })
            
        return {
            "success": True,
            "total_matches": len(results),
            "stories": results
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@api_router.get("/trending")
async def get_trending(limit: int = 10):
    """
    Computes real-time trending stories based on daily engagement:
      - Daily Views & Clicks (last 3 days): Weight 4.0
      - Daily Searches (last 3 days): Weight 3.0
      - Recent Purchases (last 3 days): Weight 15.0
    """
    try:
        arya_db = app.state.db
        from bson.objectid import ObjectId

        three_days_ago = datetime.now(timezone.utc) - timedelta(days=3)
        views_map = {}
        searches_map = {}
        recent_purchases_map = {}

        try:
            analytics_pipeline = [
                {"$match": {
                    "type": {"$in": ["view_story", "click_story", "search_story", "story_search", "search"]},
                    "timestamp": {"$gte": three_days_ago}
                }},
                {"$project": {
                    "type": "$type",
                    "story_id": {
                        "$cond": {
                            "if": {"$and": [{"$gt": ["$story_id", None]}, {"$ne": ["$story_id", ""]}]},
                            "then": "$story_id",
                            "else": "$data.story_id"
                        }
                    }
                }},
                {"$match": {"story_id": {"$ne": None}}},
                {"$group": {
                    "_id": {"story_id": "$story_id", "type": "$type"},
                    "count": {"$sum": 1}
                }}
            ]
            analytics_agg = await arya_db.db.mini_app_analytics.aggregate(analytics_pipeline).to_list(length=None)
            for a in analytics_agg:
                sid = str(a["_id"].get("story_id"))
                atype = str(a["_id"].get("type", "")).lower()
                count = int(a.get("count", 0))
                if "search" in atype:
                    searches_map[sid] = searches_map.get(sid, 0) + count
                else:
                    views_map[sid] = views_map.get(sid, 0) + count
        except Exception as e:
            logger.warning(f"Trending analytics aggregation error: {e}")

        try:
            purchase_pipeline = [
                {"$match": {
                    "status": {"$in": ["paid", "delivered"]},
                    "created_at": {"$gte": three_days_ago}
                }},
                {"$unwind": "$story_ids"},
                {"$group": {"_id": "$story_ids", "purchases": {"$sum": 1}}}
            ]
            recent_p_agg = await arya_db.db.orders.aggregate(purchase_pipeline).to_list(length=None)
            recent_purchases_map = {str(p["_id"]): int(p.get("purchases", 0)) for p in recent_p_agg}
        except Exception as e:
            logger.warning(f"Trending purchase aggregation error: {e}")

        all_stories = await arya_db.get_all_stories()
        story_map = {str(s["_id"]): s for s in all_stories}

        scores = {}
        for sid in story_map.keys():
            v_cnt = views_map.get(sid, 0)
            s_cnt = searches_map.get(sid, 0)
            p_cnt = recent_purchases_map.get(sid, 0)
            score = float(v_cnt * 4.0 + s_cnt * 3.0 + p_cnt * 15.0)
            if score > 0:
                scores[sid] = score

        # Sort by score descending
        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        top_sids = [sid for sid, score in sorted_scores[:limit]]

        trending_list = []
        seen_sids = set()

        for sid in top_sids:
            if sid in story_map:
                fmt = _format_story(story_map[sid])
                if fmt:
                    fmt["purchase_count"] = recent_purchases_map.get(sid, 0)
                    fmt["view_count"] = views_map.get(sid, 0)
                    fmt["search_count"] = searches_map.get(sid, 0)
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
                        fmt["search_count"] = 0
                        fmt["trending_score"] = 0.0
                        trending_list.append(fmt)
                        seen_sids.add(sid)
                        if len(trending_list) >= limit:
                            break

        logger.info(f"Returning {len(trending_list)} dynamic trending stories")
        return {"success": True, "data": trending_list[:limit]}
    except Exception as e:
        logger.error(f"/trending error: {e}")
        return {"success": False, "data": []}
        logger.error(f"Trending route error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))



try:
    from AryaPremium.config import Config
except ImportError:
    from config import Config

import razorpay

# Use the existing keys from AryaPremium config
RZP_KEY_ID = Config.RAZORPAY_KEY
RZP_KEY_SECRET = Config.RAZORPAY_SECRET
rzp_client = razorpay.Client(auth=(RZP_KEY_ID, RZP_KEY_SECRET))

# ─────────────────────────────────────────────────────────────────────────────
# PROMO CODE SYSTEM HELPERS & ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────
from pydantic import BaseModel

from typing import Optional, Union

class PromoValidateRequest(BaseModel):
    promo_code: str
    story_ids: list[str]
    telegram_id: Optional[Union[str, int]] = None
    payment_method: Optional[str] = None

class AvailablePromosRequest(BaseModel):
    telegram_id: Optional[Union[str, int]] = None
    story_ids: list[str] = []
    payment_method: Optional[str] = None

async def calculate_promo_discount(
    db, pcode: str, story_ids: list, subtotal: float, telegram_id: Optional[Union[str, int]] = None, payment_method: Optional[str] = None
) -> tuple[float, str]:
    """Validates the promo code and returns (discount_amount, error_message).
    If valid, error_message is "". If invalid, discount_amount is 0.0 and error_message describes the issue.
    """
    pcode_clean = str(pcode).strip().upper()
    if not pcode_clean:
        return 0.0, ""
        
    promo = await db.db.premium_promo_codes.find_one({"code": pcode_clean})
    if not promo:
        return 0.0, "Promo code not found"
        
    if not promo.get("active", True):
        return 0.0, "Promo code is inactive"

    # Check payment method target
    payment_method_target = promo.get("payment_method_target", "all")
    if payment_method_target and payment_method_target != "all" and payment_method:
        def _norm_pm(pm):
            if not pm:
                return ""
            p = str(pm).strip().lower()
            if "upi" in p:
                return "upi"
            if "razorpay" in p:
                return "razorpay"
            if "paytm" in p:
                return "paytm"
            if "payu" in p:
                return "payu"
            if "cashfree" in p:
                return "cashfree"
            if "dodo" in p:
                return "dodopayments"
            if "crypto" in p:
                return "crypto"
            return p

        norm_selected = _norm_pm(payment_method)
        norm_target = _norm_pm(payment_method_target)
        if norm_selected and norm_target and norm_selected != norm_target:
            return 0.0, f"This promo code is only valid for {payment_method_target.upper()} payment method"
        
    # Check minimum cart items
    min_cart_items = promo.get("min_cart_items")
    if min_cart_items is not None and isinstance(min_cart_items, int) and min_cart_items > 0:
        if len(story_ids) < min_cart_items:
            return 0.0, f"You need at least {min_cart_items} items in your cart to use this promo code"

    # Check minimum cart order value (INR)
    min_order_amount = promo.get("min_order_amount") or promo.get("min_order_value")
    if min_order_amount is not None:
        try:
            min_val = float(min_order_amount)
            if min_val > 0 and subtotal < min_val:
                return 0.0, f"Minimum cart amount of ₹{int(min_val)} required to use this promo code (current total: ₹{int(subtotal)})"
        except Exception:
            pass
            
    # Check target audience
    user_target = promo.get("user_target", "all")
    if user_target and user_target != "all":
        tg_id_str = str(telegram_id).strip() if telegram_id is not None else ""
        if tg_id_str.lower() in ["undefined", "null", "none", "0"]:
            tg_id_str = ""
            
        tg_id_int = int(tg_id_str) if tg_id_str.isdigit() else 0
        
        # If user target requires existing purchases or history, unauthenticated user cannot claim it
        if not tg_id_str:
            if user_target in ["existing_only", "1_purchase", "2_purchases", "3_plus_purchases"]:
                return 0.0, "This promo code is only valid for existing buyers with past purchases."
            elif user_target == "inactive_only":
                return 0.0, "This promo code is for older registered users."
        else:
            query_user = [tg_id_int, tg_id_str] if tg_id_int else [tg_id_str]
            
            # 1. Retrieve user doc from users collection
            user_doc = await db.db.users.find_one({
                "$or": [
                    {"id": {"$in": query_user}},
                    {"telegram_id": {"$in": query_user}},
                    {"user_id": {"$in": query_user}}
                ]
            })
            
            user_purchases_len = 0
            if user_doc:
                user_purchases_len = max(
                    len(user_doc.get("purchases", []) or []),
                    len(user_doc.get("purchased_stories", []) or []),
                    len(user_doc.get("bought_stories", []) or [])
                )
                
            # 2. Count from premium_purchases collection
            prem_purchases_count = await db.db.premium_purchases.count_documents({
                "$or": [
                    {"user_id": {"$in": query_user}},
                    {"telegram_id": {"$in": query_user}}
                ]
            })
            
            # 3. Count from orders collection with paid/completed/delivered status
            paid_orders_count = await db.db.orders.count_documents({
                "$or": [
                    {"user_id": {"$in": query_user}},
                    {"telegram_id": {"$in": query_user}}
                ],
                "status": {"$in": ["paid", "completed", "success", "delivered"]}
            })
            
            orders_count = max(user_purchases_len, prem_purchases_count, paid_orders_count)
            
            if user_target == "new_only":
                if orders_count > 0:
                    return 0.0, "This promo code is exclusively for new users who haven't made a purchase yet."
            elif user_target in ["existing_only", "1_purchase"]:
                if orders_count < 1:
                    return 0.0, "This promo code is exclusively for existing buyers with at least 1 previous purchase."
            elif user_target == "2_purchases":
                if orders_count < 2:
                    return 0.0, f"This promo code requires at least 2 previous purchases. You currently have {orders_count}."
            elif user_target == "3_plus_purchases":
                if orders_count < 3:
                    return 0.0, f"This special promo code unlocks after 3+ purchases. You currently have {orders_count}."
            elif user_target == "inactive_only":
                if orders_count > 0:
                    return 0.0, "This promo code is only valid for non-buyers."
                    
                joined_date = None
                if user_doc:
                    joined_date = user_doc.get("joined_date") or user_doc.get("created_at")
                    if not joined_date:
                        doc_id = user_doc.get("_id")
                        if doc_id and hasattr(doc_id, "generation_time"):
                            joined_date = doc_id.generation_time
                
                if not joined_date:
                    return 0.0, "This promo code is for older users registered 7+ days ago."
                    
                now = datetime.now(timezone.utc)
                if joined_date.tzinfo is None:
                    joined_date = joined_date.replace(tzinfo=timezone.utc)
                    
                if (now - joined_date).days < 7:
                    return 0.0, "This promo code is a welcome back gift for users registered 7+ days ago."

    # Check user-specific limit (one-time valid per user or N-time per user)
    user_limit = promo.get("user_limit")
    if user_limit is not None and isinstance(user_limit, int) and user_limit > 0 and telegram_id is not None:
        tg_id_str = str(telegram_id).strip()
        if tg_id_str:
            tg_id_int = int(tg_id_str) if tg_id_str.isdigit() else 0
            query_user = [tg_id_int, tg_id_str] if tg_id_int else [tg_id_str]
            
            import re
            used_count = await db.db.orders.count_documents({
                "$or": [
                    {"user_id": {"$in": query_user}},
                    {"telegram_id": {"$in": query_user}}
                ],
                "status": {"$in": ["paid", "completed", "success"]},
                "promo_code": {"$regex": f"^{re.escape(pcode_clean)}$", "$options": "i"}
            })
            
            if used_count >= user_limit:
                if user_limit == 1:
                    return 0.0, "You have already used this promo code once"
                else:
                    return 0.0, f"You can only use this promo code up to {user_limit} times"
        
    # Check expiration (proper timezone and end-of-day handling)
    expires_at = promo.get("expires_at")
    if expires_at:
        exp_dt = None
        if isinstance(expires_at, str):
            s = expires_at.strip()
            try:
                if len(s) == 10 and s.count("-") == 2:
                    exp_dt = datetime.strptime(s, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
                else:
                    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
                        dt = dt.replace(hour=23, minute=59, second=59)
                    exp_dt = dt
            except Exception as parse_err:
                logger.warning(f"Could not parse promo expires_at '{s}': {parse_err}")
        elif isinstance(expires_at, datetime):
            exp_dt = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
            if exp_dt.hour == 0 and exp_dt.minute == 0 and exp_dt.second == 0:
                exp_dt = exp_dt.replace(hour=23, minute=59, second=59)
                
        if exp_dt and datetime.now(timezone.utc) > exp_dt:
            return 0.0, "Promo code has expired"
                
    # Check usage limit
    usage_limit = promo.get("usage_limit")
    if usage_limit is not None and isinstance(usage_limit, int):
        usage_count = promo.get("usage_count", 0)
        if usage_count >= usage_limit:
            return 0.0, "Promo code usage limit reached"
            
    # Load cart story documents to evaluate story status and target story restrictions
    from bson.objectid import ObjectId
    cart_story_docs = []
    for sid in story_ids:
        sid_str = str(sid).strip()
        s = None
        if len(sid_str) == 24:
            try:
                s = await db.db.premium_stories.find_one({"_id": ObjectId(sid_str)})
            except Exception:
                pass
        if not s:
            try:
                s = await db.db.premium_stories.find_one({"story_id": sid_str})
            except Exception:
                pass
        if not s:
            try:
                s = await db.db.premium_stories.find_one({"id": sid_str})
            except Exception:
                pass
        if not s and sid_str.isdigit():
            try:
                s = await db.db.premium_stories.find_one({"story_id": int(sid_str)})
            except Exception:
                pass
        if s:
            cart_story_docs.append(s)

    # Compute actual cart subtotal from database if subtotal was 0
    actual_cart_subtotal = sum(float(s.get("price", 0) or 0) for s in cart_story_docs) if cart_story_docs else subtotal
    effective_subtotal = max(subtotal, actual_cart_subtotal)

    # Check minimum cart order value (INR)
    min_order_amount = promo.get("min_order_amount") or promo.get("min_order_value")
    if min_order_amount is not None:
        try:
            min_val = float(min_order_amount)
            if min_val > 0 and effective_subtotal < min_val:
                return 0.0, f"Minimum cart amount of ₹{int(min_val)} required to use this promo code (current total: ₹{int(effective_subtotal)})"
        except Exception:
            pass

    # Check story status applicability (completed, ongoing, all)
    story_status_target = str(promo.get("story_status_target", "all")).strip().lower()
    if story_status_target and story_status_target != "all":
        status_filtered = []
        for s in cart_story_docs:
            s_stat = str(s.get("status", "")).strip().lower()
            is_comp = bool(
                s.get("is_completed") is True
                or s.get("completed") is True
                or s.get("isCompleted") is True
                or s_stat == "completed"
            )
            if story_status_target == "completed" and is_comp:
                status_filtered.append(s)
            elif story_status_target == "ongoing" and not is_comp:
                status_filtered.append(s)
                
        if not status_filtered:
            if story_status_target == "completed":
                return 0.0, "This promo code is only valid for completed stories"
            else:
                return 0.0, "This promo code is only valid for ongoing stories"
        cart_story_docs = status_filtered

    # Check specific story applicability (target_story_ids and legacy story_id)
    target_story_ids = promo.get("target_story_ids", [])
    legacy_story_id = promo.get("story_id")
    if legacy_story_id and legacy_story_id != "global" and legacy_story_id not in target_story_ids:
        target_story_ids = list(target_story_ids) + [legacy_story_id]
        
    if target_story_ids:
        target_ids_str = [str(x).strip() for x in target_story_ids if str(x).strip()]
        applicable_story_price = 0.0
        applicable_in_cart = False
        
        for s in cart_story_docs:
            sid_str = str(s.get("id") or s.get("story_id") or s.get("_id")).strip()
            s_custom_id = str(s.get("story_id", "")).strip()
            s_db_id = str(s.get("_id", "")).strip()
            s_id = str(s.get("id", "")).strip()
            
            if (sid_str in target_ids_str) or (s_custom_id and s_custom_id in target_ids_str) or (s_db_id and s_db_id in target_ids_str) or (s_id and s_id in target_ids_str):
                applicable_in_cart = True
                applicable_story_price += float(s.get("price", 0) or 0)
                
        if not applicable_in_cart:
            return 0.0, "Promo code is not applicable to any stories in your cart"
            
        # Calculate discount restricted to target stories' combined price
        ptype = promo.get("type", "percentage")
        pval = float(promo.get("value", 0))
        if ptype == "percentage":
            discount = round((applicable_story_price * pval) / 100.0, 2)
        elif ptype == "flat":
            discount = min(pval, applicable_story_price)
        logger.info(f"[calculate_promo_discount] Result for '{pcode_clean}' -> Discount: {discount}")
        return discount, ""

    # If no stories in cart (e.g. browsing coupons in Offers page or validating before cart populated)
    if not story_ids:
        ptype = promo.get("type", "percentage")
        pval = float(promo.get("value", 0))
        preview_disc = pval if ptype == "flat" else round((100.0 * pval) / 100.0, 2)
        logger.info(f"[calculate_promo_discount] Preview for '{pcode_clean}' -> Discount: {preview_disc}")
        return preview_disc, ""

    # Global or status-filtered subtotal
    eligible_subtotal = sum(float(s.get("price", 0) or 0) for s in cart_story_docs) if cart_story_docs else effective_subtotal
    if eligible_subtotal <= 0:
        return 0.0, "Promo code is not applicable to any eligible stories in your cart"
        
    ptype = promo.get("type", "percentage")
    pval = float(promo.get("value", 0))
    if ptype == "percentage":
        discount = round((eligible_subtotal * pval) / 100.0, 2)
    elif ptype == "flat":
        discount = min(pval, eligible_subtotal)
        
    logger.info(f"[calculate_promo_discount] Result for '{pcode_clean}' -> Discount: {discount}")
    return discount, ""

@api_router.post("/promo-codes/validate")
async def validate_promo_endpoint(data: PromoValidateRequest):
    """Validates a promo code and returns validation status and calculated discount."""
    try:
        arya_db = app.state.db
        if not arya_db:
            raise HTTPException(status_code=500, detail="Database not available")
            
        pcode = data.promo_code.strip().upper()
        logger.info(f"=== [validate_promo_endpoint] Validating code '{pcode}' for stories: {data.story_ids}, user: {data.telegram_id}, method: {data.payment_method} ===")
        from bson.objectid import ObjectId
        valid_stories = []
        for sid in data.story_ids:
            sid_str = str(sid).strip()
            s = None
            if len(sid_str) == 24:
                try:
                    s = await arya_db.db.premium_stories.find_one({"_id": ObjectId(sid_str)})
                except Exception:
                    pass
            if not s:
                try:
                    s = await arya_db.db.premium_stories.find_one({"story_id": sid_str})
                except Exception:
                    pass
            if not s:
                try:
                    s = await arya_db.db.premium_stories.find_one({"id": sid_str})
                except Exception:
                    pass
            if not s and sid_str.isdigit():
                try:
                    s = await arya_db.db.premium_stories.find_one({"story_id": int(sid_str)})
                except Exception:
                    pass
            if s:
                valid_stories.append(s)
                
        subtotal = sum(float(s.get("price", 0) or 0) for s in valid_stories)
        logger.info(f"[validate_promo_endpoint] Found {len(valid_stories)} stories in DB. Calculated Subtotal: ₹{subtotal}")
        
        discount, err = await calculate_promo_discount(arya_db, pcode, data.story_ids, subtotal, data.telegram_id, data.payment_method)
        if err:
            logger.info(f"[validate_promo_endpoint] Validation failed for '{pcode}': {err}")
            return {"valid": False, "discount": 0.0, "message": err}
            
        promo = await arya_db.db.premium_promo_codes.find_one({"code": pcode})
        logger.info(f"[validate_promo_endpoint] Validation success for '{pcode}'! Discount: ₹{discount}")
        return {
            "valid": True,
            "discount": discount,
            "message": "Promo code applied successfully!",
            "promo": {
                "code": promo.get("code"),
                "type": promo.get("type"),
                "value": promo.get("value"),
                "min_order_amount": promo.get("min_order_amount") or promo.get("min_order_value") or 0,
                "min_cart_items": promo.get("min_cart_items") or 0,
                "story_id": promo.get("story_id", "global"),
                "description": promo.get("description", ""),
                "story_status_target": str(promo.get("story_status_target", "all")).lower(),
                "payment_method_target": str(promo.get("payment_method_target", "all")).lower()
            }
        }
    except Exception as e:
        logger.error(f"Error validating promo code: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@api_router.post("/promo-codes/available")
async def get_available_promos(data: AvailablePromosRequest):
    """Returns a list of valid promo codes for the current cart/user."""
    try:
        arya_db = app.state.db
        if not arya_db:
            return {"success": False, "promos": []}
            
        promos = await arya_db.db.premium_promo_codes.find({"active": True}).to_list(length=100)
        
        from bson.objectid import ObjectId
        valid_stories = []
        for sid in data.story_ids:
            sid_str = str(sid).strip()
            s = None
            if len(sid_str) == 24:
                try:
                    s = await arya_db.db.premium_stories.find_one({"_id": ObjectId(sid_str)})
                except Exception:
                    pass
            if not s:
                try:
                    s = await arya_db.db.premium_stories.find_one({"story_id": sid_str})
                except Exception:
                    pass
            if not s:
                try:
                    s = await arya_db.db.premium_stories.find_one({"id": sid_str})
                except Exception:
                    pass
            if not s and sid_str.isdigit():
                try:
                    s = await arya_db.db.premium_stories.find_one({"story_id": int(sid_str)})
                except Exception:
                    pass
            if not s and sid_str.isdigit():
                try:
                    s = await arya_db.db.premium_stories.find_one({"id": int(sid_str)})
                except Exception:
                    pass
            if s:
                valid_stories.append(s)
                
        subtotal = sum(float(s.get("price", 0) or 0) for s in valid_stories)
        
        available = []
        for promo in promos:
            discount, err = await calculate_promo_discount(
                arya_db, promo["code"], data.story_ids, subtotal, data.telegram_id, data.payment_method
            )
            
            # If no story was specified (general browsing), provide preview discount calculation
            if not data.story_ids:
                ptype = promo.get("type", "percentage")
                pval = float(promo.get("value", 0))
                preview_disc = pval if ptype == "flat" else round((100.0 * pval) / 100.0, 2)
                discount = max(discount, preview_disc)
                # If error was solely due to empty cart/amount, allow it to display as applicable
                if "Minimum cart amount" in err or "eligible stories" in err or "at least" in err:
                    err = ""
            
            # Fetch target story titles for clarity if promo is specific to certain stories
            target_ids = promo.get("target_story_ids", [])
            target_titles = []
            if target_ids:
                for tid in target_ids:
                    tid_str = str(tid).strip()
                    t_doc = None
                    if len(tid_str) == 24:
                        try:
                            t_doc = await arya_db.db.premium_stories.find_one({"_id": ObjectId(tid_str)})
                        except Exception:
                            pass
                    if not t_doc:
                        try:
                            t_doc = await arya_db.db.premium_stories.find_one({"story_id": tid_str})
                        except Exception:
                            pass
                    if not t_doc:
                        try:
                            t_doc = await arya_db.db.premium_stories.find_one({"id": tid_str})
                        except Exception:
                            pass
                    if not t_doc and tid_str.isdigit():
                        try:
                            t_doc = await arya_db.db.premium_stories.find_one({"story_id": int(tid_str)})
                        except Exception:
                            pass
                    if t_doc:
                        name = t_doc.get("story_name_en") or t_doc.get("title")
                        if name:
                            target_titles.append(name)
            
            is_app = not bool(err) and (discount > 0 or not data.story_ids)
            promo_info = {
                "code": promo.get("code"),
                "type": promo.get("type", "percentage"),
                "value": promo.get("value", 0),
                "description": promo.get("description", ""),
                "discount_amount": discount if not err else 0,
                "auto_apply": bool(promo.get("auto_apply", False)) if is_app else False,
                "applicable": is_app,
                "error_reason": err,
                "min_cart_items": promo.get("min_cart_items"),
                "min_order_amount": promo.get("min_order_amount") or promo.get("min_order_value"),
                "user_limit": promo.get("user_limit"),
                "user_target": promo.get("user_target", "all"),
                "story_status_target": promo.get("story_status_target", "all"),
                "payment_method_target": promo.get("payment_method_target", "all"),
                "expires_at": promo.get("expires_at"),
                "target_story_ids": target_ids,
                "target_story_titles": target_titles,
            }
            
            available.append(promo_info)
                
        available.sort(key=lambda x: (1 if x["applicable"] else 0, x["discount_amount"]), reverse=True)
        return {"success": True, "promos": available}
    except Exception as e:
        logger.error(f"Error fetching available promos: {e}")
        return {"success": False, "promos": []}

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# POST /create-payment-link
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@api_router.post("/create-payment-link")
async def create_payment_link(payload: dict):
    """Creates a Razorpay Payment Link linked to an order."""
    telegram_id = payload.get("telegram_id")
    username    = payload.get("username", "")
    promo_code  = payload.get("promo_code", "")
    is_int      = payload.get("is_international", False)

    if not telegram_id:
        raise HTTPException(status_code=400, detail="Missing telegram_id")

    arya_db = app.state.db

    resolved_items, subtotal, story_ids, story_names = await _resolve_order_items(arya_db, payload)
    if not resolved_items:
        raise HTTPException(status_code=400, detail="Invalid stories requested")

    # Fetch settings
    cfg = await arya_db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    if cfg.get("razorpay_disabled") or cfg.get("razorpay_status") in ["disabled", "hidden"]:
        raise HTTPException(status_code=400, detail="Razorpay payment gateway is currently disabled by Admin")
    
    # 1. Promo Code Discount
    discount = 0.0
    pcode_clean = str(promo_code).strip().upper()
    if pcode_clean:
        discount, err = await calculate_promo_discount(arya_db, pcode_clean, story_ids, subtotal, telegram_id)
        if err:
            raise HTTPException(status_code=400, detail=f"Promo Code Error: {err}")
                
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

    order_id = await _make_arya_order_id(arya_db, str(telegram_id), story_ids, source="miniapp")
    bot_username = os.environ.get("BOT_USERNAME", "UseAryaBot")
    
    try:
        if not RZP_KEY_ID or not RZP_KEY_SECRET:
            raise ValueError("Razorpay API keys are missing in the .env file. Please check RAZORPAY_KEY and RAZORPAY_SECRET.")

        # Create Razorpay Payment Link
        link_data = rzp_client.payment_link.create({
            "amount": int(total_price * 100), # in paise
            "currency": "INR",
            "accept_partial": False,
            "description": ", ".join(story_names)[:200] or "Arya Premium Content",
            "customer": {
                "name": username or f"User {telegram_id}",
                "email": f"user{telegram_id}@sliceurl.com"
            },
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "reference_id": order_id,
            "callback_url": f"https://t.me/{bot_username}/app",  # Returns to WebApp after payment
            "callback_method": "get",
        })
        
        tg_id_int = int(telegram_id) if str(telegram_id).isdigit() else telegram_id
        
        # Save order to DB
        order_doc = {
            "order_id":    order_id,
            "payment_link_id": link_data["id"],
            "user_id":     tg_id_int,
            "username":    username or "Unknown",
            "story_ids":   story_ids,
            "items":       resolved_items,
            "story_names": story_names,
            "subtotal":    subtotal,
            "discount":    discount,
            "promo_code":  pcode_clean if discount > 0 else None,
            "platform_fee": platform_fee,
            "razorpay_fee": razorpay_fee,
            "total":       total_price,
            "status":      "pending",
            "source":      "razorpay_link",
            "auto_deliver": bool(payload.get("auto_deliver", True)),
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
                
                # Logic to grant stories to user in DB (only full stories, not parts)
                tg_id_int = int(telegram_id) if str(telegram_id).isdigit() else telegram_id
                if order.get("items"):
                    for itm in order["items"]:
                        if itm.get("is_full", True) and not itm.get("part_id"):
                            sid = str(itm.get("story_id") or itm.get("id") or "")
                            if sid:
                                await arya_db.add_purchase(tg_id_int, sid)
                else:
                    for sid in order.get("story_ids", []):
                        await arya_db.add_purchase(tg_id_int, sid)
                
                # Log and audit records
                updated_order = {**order, "status": "paid"}
                asyncio.create_task(trigger_payment_log_from_order(updated_order))
                asyncio.create_task(record_purchased_stories(updated_order))
                asyncio.create_task(send_purchase_success_dm(arya_db, telegram_id, order_doc=updated_order, payment_method="Razorpay", verified_by="Auto Verified By System"))
            
            bot_username = os.environ.get("BOT_USERNAME", "UseAryaBot")
            return {
                "success": True,
                "status": "paid",
                "order_id": order["order_id"] if order else "",
                "checkout_url": f"https://t.me/{bot_username}?start=success_{order['order_id']}" if order else ""
            }
            
        return {"success": True, "status": "pending"}
    except Exception as e:
        logger.error(f"Razorpay verification failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))



import hmac
import hashlib

def _make_order_id(tg_id: str) -> str:
    """Fallback structured order ID generator."""
    from datetime import datetime as _dt
    import random
    return f"AM-{tg_id}-{_dt.now().strftime('%d%m')}-{random.randint(10000, 99999)}"

async def _make_arya_order_id(
    db_instance,
    tg_id: str,
    story_ids: list = None,
    source: str = "miniapp"
) -> str:
    """
    Generate a new structured Arya Order ID.
    Format: {PREFIX}-{TG_ID}-{DDMM}-{STORY_NUM}{ORDER_NUM}
    Prefix: AM = Mini App, AB = Bot (Telegram Bot)
    Example: AM-1071421266-2107-55130

    - Story number = serial position of story (oldest added = story #1, sorted _id asc)
    - Order number = globally unique auto-incremented counter via ReturnDocument.AFTER
    """
    try:
        from datetime import datetime as _dt
        from pymongo import ReturnDocument

        # 1. Determine prefix based on source
        src_lower = str(source or "miniapp").lower()
        prefix = "AB" if "bot" in src_lower else "AM"

        # 2. Date & Month in DDMM format
        now = _dt.now()
        date_str = now.strftime("%d%m")

        # 3. ATOMIC global counter — ReturnDocument.AFTER returns post-increment value (always unique)
        counter_doc = await db_instance.db.order_counters.find_one_and_update(
            {"_key": "global_order_counter"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER
        )
        order_num = counter_doc.get("seq", 1) if counter_doc else 1

        # 4. Story serial number (oldest story = #1, sorted by _id ascending)
        story_num = 0
        if story_ids and len(story_ids) > 0:
            try:
                from bson.objectid import ObjectId as _OID
                first_sid = story_ids[0]
                all_ids = await db_instance.db.premium_stories.distinct("_id")
                all_ids_sorted = sorted(all_ids)  # ascending: oldest = #1
                try:
                    target_oid = _OID(first_sid)
                    if target_oid in all_ids_sorted:
                        story_num = all_ids_sorted.index(target_oid) + 1
                except Exception:
                    pass
            except Exception:
                story_num = 0

        # 5. Build the final order ID
        tg_id_str = str(tg_id)
        if story_num > 0:
            return f"{prefix}-{tg_id_str}-{date_str}-{story_num}{order_num}"
        else:
            return f"{prefix}-{tg_id_str}-{date_str}-{order_num}"

    except Exception as e:
        logger.warning(f"[OrderID] Failed to generate structured order ID: {e}. Falling back to legacy.")
        import uuid
        return f"OD_{tg_id}_{uuid.uuid4().hex[:8].upper()}"


async def _resolve_order_items(arya_db, payload: dict) -> tuple:
    """
    Parses items from payload (either `items` list with part info or legacy `story_ids` list).
    Returns (resolved_items, subtotal, story_ids_list, story_names_list)
    """
    from bson.objectid import ObjectId
    from bson.errors import InvalidId

    raw_items = payload.get("items")
    story_ids = payload.get("story_ids", [])
    
    resolved_items = []
    
    if raw_items and isinstance(raw_items, list):
        for itm in raw_items:
            sid = str(itm.get("story_id") or itm.get("id") or "").strip()
            if not sid:
                continue
            
            story_doc = None
            try:
                story_doc = await arya_db.db.premium_stories.find_one({"_id": ObjectId(sid)})
            except (InvalidId, Exception):
                pass
            if not story_doc:
                story_doc = await arya_db.db.premium_stories.find_one({"_id": sid})
            if not story_doc:
                story_doc = await arya_db.db.premium_stories.find_one({"story_id": sid})
                
            if not story_doc:
                continue
                
            part_id = itm.get("part_id")
            selected_part = None
            parts_in_doc = (
                story_doc.get("parts")
                or story_doc.get("story_parts")
                or story_doc.get("episode_parts")
                or story_doc.get("episodes_parts")
                or story_doc.get("part_list")
                or []
            )
            if part_id and isinstance(parts_in_doc, list):
                for p in parts_in_doc:
                    if isinstance(p, dict) and str(p.get("id")) == str(part_id):
                        selected_part = p
                        break
            
            if selected_part:
                p_start = int(selected_part.get("start_id") or story_doc.get("start_id") or 0)
                p_end = int(selected_part.get("end_id") or story_doc.get("end_id") or 0)
                p_price = float(selected_part.get("price") or 0)
                p_name = selected_part.get("name") or f"Part {selected_part.get('id')}"
                p_episodes = selected_part.get("episodes") or (f"{p_start}-{p_end}" if p_start and p_end else "")
                resolved_items.append({
                    "story_id": str(story_doc.get("_id")),
                    "story_title": story_doc.get("story_name_en") or story_doc.get("title") or "Story",
                    "part_id": str(selected_part.get("id")),
                    "part_name": p_name,
                    "episodes": p_episodes,
                    "start_id": p_start,
                    "end_id": p_end,
                    "price": p_price,
                    "is_full": False,
                })
            else:
                s_start = int(story_doc.get("start_id") or 0)
                s_end = int(story_doc.get("end_id") or 0)
                s_price = float(story_doc.get("price") or 0)
                resolved_items.append({
                    "story_id": str(story_doc.get("_id")),
                    "story_title": story_doc.get("story_name_en") or story_doc.get("title") or "Story",
                    "part_id": None,
                    "part_name": None,
                    "episodes": "",
                    "start_id": s_start,
                    "end_id": s_end,
                    "price": s_price,
                    "is_full": True,
                })
    elif story_ids:
        for sid in story_ids:
            sid_clean = str(sid).strip()
            if not sid_clean:
                continue
            story_doc = None
            try:
                story_doc = await arya_db.db.premium_stories.find_one({"_id": ObjectId(sid_clean)})
            except (InvalidId, Exception):
                pass
            if not story_doc:
                story_doc = await arya_db.db.premium_stories.find_one({"_id": sid_clean})
            if not story_doc:
                story_doc = await arya_db.db.premium_stories.find_one({"story_id": sid_clean})
                
            if not story_doc:
                continue
                
            resolved_items.append({
                "story_id": str(story_doc.get("_id")),
                "story_title": story_doc.get("story_name_en") or story_doc.get("title") or "Story",
                "part_id": None,
                "part_name": None,
                "episodes": "",
                "start_id": int(story_doc.get("start_id") or 0),
                "end_id": int(story_doc.get("end_id") or 0),
                "price": float(story_doc.get("price") or 0),
                "is_full": True,
            })
            
    subtotal = sum(i["price"] for i in resolved_items)
    unique_story_ids = list(dict.fromkeys([i["story_id"] for i in resolved_items]))
    story_names = [
        i["story_title"] + (f" ({i['part_name']} · Ep {i['episodes']})" if (i.get("part_name") and i.get("episodes")) else (f" ({i['part_name']})" if i.get("part_name") else ""))
        for i in resolved_items
    ]
    return resolved_items, subtotal, unique_story_ids, story_names


# ── Razorpay: Create Order ──────────────────────────────────────────────
@api_router.post("/create-order")
async def create_razorpay_order(payload: dict):
    """Create Razorpay order. Returns order_id + key for frontend SDK modal."""
    tg_id      = payload.get("telegram_id") or 0
    is_int     = payload.get("is_international", False)
    promo_code = payload.get("promo_code", "")

    if not RZP_KEY_ID or not RZP_KEY_SECRET:
        raise HTTPException(500, "Razorpay not configured on server")

    arya_db = app.state.db

    resolved_items, subtotal, story_ids, story_names = await _resolve_order_items(arya_db, payload)
    if not resolved_items:
        raise HTTPException(400, "Cart is empty or no valid stories found")

    # Fetch settings
    cfg = await arya_db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    
    # 1. Promo Code Discount
    discount = 0.0
    pcode_clean = str(promo_code).strip().upper()
    if pcode_clean:
        discount, err = await calculate_promo_discount(arya_db, pcode_clean, story_ids, subtotal, tg_id)
        if err:
            raise HTTPException(status_code=400, detail=f"Promo Code Error: {err}")
                
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
    receipt     = await _make_arya_order_id(arya_db, str(tg_id), story_ids, source="miniapp")

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
            "story_names":       story_names,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"create-order error: {e}", exc_info=True)
        raise HTTPException(500, str(e))
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
    resolved_items, subtotal, story_ids, story_names = await _resolve_order_items(arya_db, payload)

    # Fetch settings
    cfg = await arya_db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    
    # 1. Promo Code Discount
    discount = 0.0
    pcode_clean = str(promo_code).strip().upper()
    if pcode_clean:
        discount, err = await calculate_promo_discount(arya_db, pcode_clean, story_ids, subtotal, tg_id)
        if err:
            raise HTTPException(status_code=400, detail=f"Promo Code Error: {err}")
                
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
    oid     = await _make_arya_order_id(arya_db, str(tg_id), story_ids, source="miniapp")

    tg_id_int = int(tg_id) if str(tg_id).isdigit() else 0

    order_doc = {
        "order_id":            oid,
        "user_id":             tg_id_int if tg_id_int else tg_id,
        "username":            username,
        "story_ids":           story_ids,
        "items":               resolved_items,
        "story_names":         story_names,
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
        "auto_deliver":        bool(payload.get("auto_deliver", True)),
        "created_at":          datetime.now(timezone.utc),
    }
    await arya_db.db.orders.insert_one(order_doc)

    if tg_id:
        if resolved_items:
            for itm in resolved_items:
                if itm.get("is_full", True) and not itm.get("part_id"):
                    sid = str(itm.get("story_id") or itm.get("id") or "")
                    if sid:
                        await arya_db.add_purchase(tg_id_int if tg_id_int else tg_id, sid)
        else:
            for sid in story_ids:
                await arya_db.add_purchase(tg_id_int if tg_id_int else tg_id, sid)

    # Log and audit records
    asyncio.create_task(trigger_payment_log_from_order(order_doc))
    asyncio.create_task(record_purchased_stories(order_doc))
    asyncio.create_task(trigger_auto_delivery_for_order(arya_db, tg_id, order_doc=order_doc))
    asyncio.create_task(send_purchase_success_dm(arya_db, tg_id, order_doc=order_doc, payment_method="Razorpay", verified_by="Auto Verified By System"))

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
    oid = (await _make_arya_order_id(arya_db, str(tg_id), story_ids, source="miniapp")) if tg_id else razorpay_order_id
    tg_id_int = int(tg_id) if str(tg_id).isdigit() else 0

    # Store order
    order_doc = {
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
    }
    await arya_db.db.orders.insert_one(order_doc)

    if tg_id:
        for sid in story_ids:
            await arya_db.add_purchase(tg_id_int if tg_id_int else tg_id, sid)

    # Log and audit records
    asyncio.create_task(trigger_payment_log_from_order(order_doc))
    asyncio.create_task(record_purchased_stories(order_doc))
    asyncio.create_task(send_purchase_success_dm(arya_db, tg_id, order_doc=order_doc, payment_method="Razorpay", verified_by="Auto Verified By System"))

    bot_username = os.environ.get("BOT_USERNAME", "UseAryaBot")
    return RedirectResponse(url=f"https://t.me/{bot_username}/app", status_code=302)


# ==========================================
# UPI Manual Verification via Gmail IMAP
# ==========================================
import imaplib
import email
from email.header import decode_header
import re
import hashlib

def clean_extracted_name(name: str) -> str:
    # Remove extra spaces
    name = re.sub(r'\s+', ' ', name).strip()
    
    # Split into words and stop at any common non-name keywords
    stop_words = {
        "amount", "utr", "rrn", "txn", "txnid", "date", "ref", "rs", "inr", "upi", 
        "payment", "status", "type", "received", "credited", "transferred", "has", 
        "been", "via", "on", "in", "to", "your", "my", "account", "bank", "slice",
        "customer", "user", "card", "rupees", "id", "no", "reference", "credited",
        "debit", "credit", "wallet", "balance", "success", "failed", "pending"
    }
    
    words = name.split()
    valid_words = []
    for w in words:
        # Strip trailing punctuation from the word for checking
        w_clean = re.sub(r'[^a-zA-Z]', '', w).lower()
        if w_clean in stop_words:
            break
        valid_words.append(w)
        
    cleaned = " ".join(valid_words).strip()
    # Clean any trailing punctuation or special chars from the name
    cleaned = re.sub(r'[^a-zA-Z\s\.\-\&]', '', cleaned).strip()
    # Strip any trailing punctuation like dots or dashes from the end of the cleaned name
    cleaned = cleaned.rstrip('. - &').strip()
    return cleaned

def extract_payer_name_from_email(body: str) -> str:
    """Helper to extract sender name from slice email notifications."""
    if not body:
        return ""
    
    # Normalize spaces and strip HTML tags if present
    body_clean = re.sub(r'<[^>]+>', ' ', body)
    body_clean = re.sub(r'\s+', ' ', body_clean).strip()
    
    # We will search with multiple regex patterns. We order them from most specific to general.
    patterns = [
        # Explicit fields in tables or lists (e.g. "Payer: John Doe" or "Payer Name: John Doe")
        r'(?:payer|sender|remitter)(?:\s+name)?\s*[:\-]\s*([a-zA-Z\s\.\-\&]{3,40})',
        
        # Sentences like "received from John Doe via UPI" or "transferred by John Doe"
        # We allow an optional colon after from/by as well
        r'\b(?:from|by)\s*:?\s*([a-zA-Z\s\.\-\&]{3,40})'
    ]
    
    words_to_skip = {
        "your", "my", "slice", "account", "bank", "upi", "card", "rs", "rupees", "inr", 
        "customer", "user", "payment", "has", "been", "credited", "received", "transferred", 
        "by", "via", "on", "in", "to"
    }
    
    for pattern in patterns:
        for match in re.finditer(pattern, body_clean, re.IGNORECASE):
            name = match.group(1).strip()
            cleaned_name = clean_extracted_name(name)
            
            if len(cleaned_name) >= 3 and cleaned_name.lower() not in words_to_skip:
                return cleaned_name.title()
                
    return ""

def extract_amount_from_email(body: str) -> float | None:
    # Normalize body: replace newlines/tabs with space
    normalized = body.replace("\n", " ").replace("\r", " ")
    body_lower = normalized.lower()
    
    # Let's search using the same patterns as verify_amount_in_email
    patterns = [
        r'(?:received|credited|deposit|transfer|payment|added)\s+(?:value\s+)?(?:of\s+)?(?:rs\.?|₹|inr)?\s*([\d,]+(?:\.\d{1,2})?)',
        r'(?:rs\.?|₹|inr)?\s*([\d,]+(?:\.\d{1,2})?)\s*(?:received|credited|deposited|added|transfer)',
        r'(?:received|credited|deposit)\s+(?:rs\.?|₹|inr)?\s*([\d,]+(?:\.\d{1,2})?)'
    ]
    
    for pattern in patterns:
        for match in re.finditer(pattern, body_lower):
            val_str = match.group(1).replace(",", "")
            try:
                val = float(val_str)
                # Ignore values like 0 or very small or extremely large values that might be balances/dates
                if 1.0 <= val <= 100000.0:
                    return val
            except ValueError:
                continue
                
    # Fallback to general currency match
    fallback_patterns = [
        r'(?:rs\.?|₹|inr)\s*([\d,]+(?:\.\d{1,2})?)'
    ]
    for pattern in fallback_patterns:
        for match in re.finditer(pattern, body_lower):
            val_str = match.group(1).replace(",", "")
            try:
                val = float(val_str)
                if 1.0 <= val <= 100000.0:
                    return val
            except ValueError:
                continue
                
    return None

def verify_amount_in_email(body: str, expected_amount: float) -> bool:
    # Normalize body: replace newlines/tabs with space
    normalized = body.replace("\n", " ").replace("\r", " ")
    
    # Normalize regex formatting of expected amount (e.g. 149.00 or 149)
    amt_str1 = f"{expected_amount:.2f}"
    amt_str2 = f"{int(expected_amount)}" if expected_amount.is_integer() else f"{expected_amount:.1f}"
    
    body_lower = normalized.lower()
    
    # We want to match:
    # - received/credited ... amount
    # - amount ... received/credited
    patterns = [
        r'(?:received|credited|deposit|transfer|payment|added)\s+(?:value\s+)?(?:of\s+)?(?:rs\.?|₹|inr)?\s*([\d,]+(?:\.\d{1,2})?)',
        r'(?:rs\.?|₹|inr)?\s*([\d,]+(?:\.\d{1,2})?)\s*(?:received|credited|deposited|added|transfer)',
        r'(?:received|credited|deposit)\s+(?:rs\.?|₹|inr)?\s*([\d,]+(?:\.\d{1,2})?)'
    ]
    
    for pattern in patterns:
        for match in re.finditer(pattern, body_lower):
            val_str = match.group(1).replace(",", "")
            try:
                val = float(val_str)
                if abs(val - expected_amount) < 0.01:
                    return True
            except ValueError:
                continue
                
    # Fallback checking
    if any(x in body_lower for x in ["received", "credited", "deposit", "added"]):
        for currency in ["₹", "rs", "inr"]:
            if f"{currency}{amt_str1}" in body_lower or f"{currency} {amt_str1}" in body_lower:
                return True
            if f"{currency}{amt_str2}" in body_lower or f"{currency} {amt_str2}" in body_lower:
                return True
            if f"{currency}.{amt_str1}" in body_lower or f"{currency}. {amt_str1}" in body_lower:
                return True
            if f"{currency}.{amt_str2}" in body_lower or f"{currency}. {amt_str2}" in body_lower:
                return True
                
    return False

def get_email_body(msg) -> str:
    """Helper to extract text body from email message."""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdisp = str(part.get("Content-Disposition"))
            if ctype == "text/plain" and "attachment" not in cdisp:
                try:
                    return part.get_payload(decode=True).decode("utf-8", errors="ignore")
                except Exception:
                    pass
            elif ctype == "text/html" and "attachment" not in cdisp:
                try:
                    html_content = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                    # simple html to text stripping
                    text_content = re.sub(r'<[^>]+>', ' ', html_content)
                    text_content = re.sub(r'\s+', ' ', text_content)
                    return text_content
                except Exception:
                    pass
    else:
        try:
            return msg.get_payload(decode=True).decode("utf-8", errors="ignore")
        except Exception:
            pass
    return ""

@api_router.post("/verify-upi-utr")
async def verify_upi_utr(payload: dict):
    telegram_id = payload.get("telegram_id")
    username = payload.get("username", "")
    story_ids = payload.get("story_ids", [])
    utr = str(payload.get("utr", "")).strip()
    promo_code = payload.get("promo_code", "")
    is_int = payload.get("is_international", False)

    if not telegram_id or not story_ids or not utr:
        raise HTTPException(status_code=400, detail="Missing required validation parameters.")

    # 1. Validate UTR pattern (12 to 22 digits)
    if not utr.isdigit() or not (12 <= len(utr) <= 22):
        raise HTTPException(status_code=400, detail="Invalid UTR format. UTR must be between 12 and 22 digits.")

    # Connect to DB
    db = getattr(app.state, "db", None)
    if not db:
        raise HTTPException(status_code=500, detail="Database connection is currently unavailable.")

    # 2. Check for UTR Replay attack (already claimed)
    existing_utr = await db.db.verified_utrs.find_one({"utr": utr})
    if existing_utr:
        raise HTTPException(status_code=400, detail="This UTR/RRN has already been claimed for another purchase. Reuse is blocked.")

    # 3. Calculate expected amount
    resolved_items, subtotal, story_ids, story_names = await _resolve_order_items(db, payload)
    if not resolved_items:
        raise HTTPException(status_code=400, detail="No valid stories in cart.")

    cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    
    # Verify UPI is enabled
    if not cfg.get("upi_manual_enabled", False):
         raise HTTPException(status_code=400, detail="Direct UPI payments are currently disabled by the admin.")

    discount = 0.0
    pcode_clean = str(promo_code).strip().upper()
    if pcode_clean:
        discount, err = await calculate_promo_discount(db, pcode_clean, story_ids, subtotal, telegram_id)
        if err:
            raise HTTPException(status_code=400, detail=f"Promo Code Error: {err}")
                
    platform_fee = 0.0
    if cfg.get("platform_fee_enabled", True):
        platform_fee = float(cfg.get("platform_fee_amount", 5.0))
        
    expected_total = max(0.0, subtotal - discount + platform_fee)
    if expected_total <= 0:
        raise HTTPException(status_code=400, detail="Order total must be greater than zero.")

    # 4. Search Gmail IMAP
    gmail_enabled = cfg.get("gmail_verification_enabled", False)
    
    gmail_user = cfg.get("gmail_user", "").strip()
    gmail_password = cfg.get("gmail_app_password", "").strip()

    # If database settings are empty, look in env and configs (with dynamic reload)
    if not gmail_user or not gmail_password:
        try:
            from dotenv import load_dotenv
            import os
            # Reload root .env and sub-app .env
            load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)
            load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "AryaPremium", ".env"), override=True)
        except Exception as dotenv_err:
            logger.warning(f"Dotenv dynamic reload warning: {dotenv_err}")

        # Try env vars
        if not gmail_user:
            gmail_user = os.environ.get("GMAIL_USER", "").strip() or os.environ.get("gmail_user", "").strip()
        if not gmail_password:
            gmail_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip() or os.environ.get("gmail_app_password", "").strip()

        # Try RootConfig reload
        if not gmail_user or not gmail_password:
            try:
                import importlib
                import config
                importlib.reload(config)
                if not gmail_user:
                    gmail_user = getattr(config.Config, "GMAIL_USER", "").strip()
                if not gmail_password:
                    gmail_password = getattr(config.Config, "GMAIL_APP_PASSWORD", "").strip()
            except Exception as e:
                logger.warning(f"config reload warning: {e}")

        # Try PremConfig reload
        if not gmail_user or not gmail_password:
            try:
                import importlib
                import AryaPremium.config
                importlib.reload(AryaPremium.config)
                if not gmail_user:
                    gmail_user = getattr(AryaPremium.config.Config, "GMAIL_USER", "").strip()
                if not gmail_password:
                    gmail_password = getattr(AryaPremium.config.Config, "GMAIL_APP_PASSWORD", "").strip()
            except Exception as e:
                logger.warning(f"AryaPremium.config reload warning: {e}")

    # Auto-enable if credentials are set but toggle is False
    if not gmail_enabled and gmail_user and gmail_password:
        gmail_enabled = True

    gmail_user = gmail_user.replace("\xa0", "").replace(" ", "").strip()
    gmail_password = gmail_password.replace("\xa0", "").replace(" ", "").strip()

    payer_name = ""
    if gmail_enabled:
        if not gmail_user or not gmail_password:
            logger.error("Gmail credentials are not configured in settings/env!")
            raise HTTPException(
                status_code=500,
                detail="Automatic payment verification is temporarily unavailable. Please contact support."
            )

        verified = False
        amount_mismatch = False
        mismatched_amount = None
        try:
            # Login and search via IMAP
            mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
            mail.login(gmail_user, gmail_password)
            mail.select("INBOX")

            # Search inbox for the specific UTR text. Extremely fast query index search
            status, messages = mail.search(None, 'TEXT', utr)
            if status == "OK" and messages[0]:
                mail_ids = messages[0].split()
                # Iterate from most recent messages
                for mail_id in reversed(mail_ids):
                    res_status, msg_data = mail.fetch(mail_id, "(RFC822)")
                    if res_status != "OK":
                        continue
                    for response_part in msg_data:
                        if isinstance(response_part, tuple):
                            msg = email.message_from_bytes(response_part[1])
                            from_header = msg.get("From", "")
                            
                            # Enforce sender check: only noreply@slice.bank.in is allowed
                            if "noreply@slice.bank.in" not in from_header.lower():
                                continue
                                
                            body = get_email_body(msg)
                            
                            # Verify UTR is present and amount matches
                            if utr in body:
                                if verify_amount_in_email(body, expected_total):
                                    verified = True
                                    payer_name = extract_payer_name_from_email(body)
                                    break
                                else:
                                    amount_mismatch = True
                                    parsed_amount = extract_amount_from_email(body)
                                    if parsed_amount is not None:
                                        mismatched_amount = parsed_amount
                                    break  # Correct UTR found but wrong amount
                    if verified or amount_mismatch:
                        break



            mail.close()
            mail.logout()
        except Exception as imap_err:
            logger.error(f"Gmail IMAP error: {imap_err}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail="An error occurred during payment verification. Please try again in a few moments."
            )

        if not verified:
            if amount_mismatch:
                if mismatched_amount is not None:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Payment of ₹{mismatched_amount:.2f} received, but the expected amount is ₹{expected_total:.2f}. "
                            "Please pay the exact amount. You cannot get access to stories with a lower payment amount.\n\n"
                            f"भुगतान ₹{mismatched_amount:.2f} प्राप्त हुआ है, लेकिन अपेक्षित राशि ₹{expected_total:.2f} है। "
                            "कृपया सटीक राशि का भुगतान करें। कम राशि का भुगतान करने पर आपको स्टोरी का एक्सेस नहीं मिल सकता है।"
                        )
                    )
                else:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Payment received, but the amount does not match the expected total of ₹{expected_total:.2f}. "
                            "Please pay the exact amount. You cannot get access to stories with a lower payment amount.\n\n"
                            f"भुगतान प्राप्त हुआ है, लेकिन राशि ₹{expected_total:.2f} की अपेक्षित राशि से मेल नहीं खाती। "
                            "कृपया सटीक राशि का भुगतान करें। कम राशि का भुगतान करने पर आपको स्टोरी का एक्सेस नहीं मिल सकता है।"
                        )
                    )
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Payment not detected. Please verify your UTR/RRN number and ensure you paid the exact amount. If you just paid, please wait 10-15 seconds and try again."
                )
    else:
        # If auto-verification is disabled, manual UPI cannot be verified automatically on the client.
        raise HTTPException(
            status_code=400,
            detail="Automatic payment verification is currently disabled. Please contact support."
        )

    # 5. Success! Mark UTR as claimed to prevent replay attacks
    await db.db.verified_utrs.insert_one({
        "utr": utr,
        "amount": expected_total,
        "user_id": telegram_id,
        "verified_at": datetime.now(timezone.utc)
    })

    # 6. Create / upgrade order doc
    # If a pending order was already created when the QR page opened (via
    # /create-pending-order), we UPDATE that record instead of inserting a
    # duplicate. This keeps a single, traceable record per payment attempt.
    oid = str(payload.get("order_id") or "").strip()
    _invalid_ids = {"upi-manual", "upi_manual", "", "undefined", "null"}
    _is_invalid_oid = (
        not oid
        or oid.lower() in _invalid_ids
        or oid.startswith("OD_")
        or oid.startswith("OD-")
    )
    if _is_invalid_oid:
        # Generate a fresh unique structured order ID
        oid = await _make_arya_order_id(db, str(telegram_id), story_ids, source="miniapp")
        logger.info(f"[UPI-Verify] Generated new order_id={oid} (rejected invalid: '{payload.get('order_id')}')")
    tg_id_int = int(telegram_id) if str(telegram_id).isdigit() else 0

    # Generate deterministic invoice number using order ID hash
    inv_hash = int(hashlib.md5(str(oid).encode()).hexdigest(), 16) % 100000
    invoice_number = f"INV/{datetime.now().year}/{inv_hash:05d}"

    order_doc = {
        "order_id":            oid,
        "user_id":             tg_id_int if tg_id_int else telegram_id,
        "username":            username,
        "payer_name":          payer_name if payer_name else username,
        "invoice_number":      invoice_number,
        "story_ids":           story_ids,
        "items":               resolved_items,
        "story_names":         story_names,
        "subtotal":            subtotal,
        "discount":            discount,
        "promo_code":          pcode_clean if discount > 0 else None,
        "platform_fee":        platform_fee,
        "razorpay_fee":        0.0,
        "total":               expected_total,
        "status":              "paid",
        "source":              "upi_manual_miniapp",
        "utr":                 utr,
        "auto_deliver":        bool(payload.get("auto_deliver", True)),
        "paid_at":             datetime.now(timezone.utc),
    }

    # Upsert: update existing pending order (matched by order_id) or insert new
    upsert_result = await db.db.orders.update_one(
        {"order_id": oid, "status": "pending"},
        {"$set": order_doc},
    )
    if upsert_result.matched_count == 0:
        # No pending record found — insert fresh (covers cases where
        # /create-pending-order was never called or order_id differed)
        order_doc["created_at"] = datetime.now(timezone.utc)
        await db.db.orders.insert_one(order_doc)

    # 7. Grant story access (only for full stories)
    if telegram_id:
        if resolved_items:
            for itm in resolved_items:
                if itm.get("is_full", True) and not itm.get("part_id"):
                    sid = str(itm.get("story_id") or itm.get("id") or "")
                    if sid:
                        await db.add_purchase(tg_id_int if tg_id_int else telegram_id, sid)
        else:
            for sid in story_ids:
                await db.add_purchase(tg_id_int if tg_id_int else telegram_id, sid)

    # 8. Trigger Logs
    asyncio.create_task(trigger_payment_log_from_order(order_doc))
    asyncio.create_task(record_purchased_stories(order_doc))
    asyncio.create_task(send_purchase_success_dm(db, telegram_id, order_doc=order_doc, payment_method="UPI (UTR)", verified_by="Auto Verified By System"))

    return {"success": True, "message": "UPI payment verified successfully!", "order_id": oid, "invoice_number": invoice_number, "payer_name": payer_name}


# ==========================================
# Generate Order ID endpoint (server-side)
# ==========================================

@api_router.post("/generate-order-id")
async def generate_order_id_endpoint(payload: dict):
    """
    Returns a fresh, unique, properly formatted Arya order ID.
    Frontend should call this BEFORE showing the UPI QR page,
    then pass the returned order_id to create-pending-order and verify-upi-utr.
    This ensures ALL payment attempts (paid, failed, pending) get a traceable new-format ID.
    """
    telegram_id = payload.get("telegram_id")
    story_ids   = payload.get("story_ids", [])
    source      = str(payload.get("source", "miniapp")).lower()

    if not telegram_id:
        raise HTTPException(400, "telegram_id required")

    arya_db = getattr(app.state, "db", None)
    if not arya_db:
        raise HTTPException(500, "DB not available")

    order_id = await _make_arya_order_id(arya_db, str(telegram_id), story_ids, source=source)
    logger.info(f"[OrderID] Generated: {order_id} for user {telegram_id}")
    return {"success": True, "order_id": order_id}


# ==========================================
# Create Pending UPI Order (QR Page Mount)
# ==========================================

@api_router.post("/create-pending-order")
async def create_pending_order(payload: dict):
    """
    Called the instant the QR payment screen is shown to the user — BEFORE they pay.
    Creates a status="pending" order record so every payment attempt is traceable
    by order_id, even if the user pays to the wrong UPI ID or UTR verification fails.

    - If no order_id is provided OR it's in old legacy OD_ format, a new one is auto-generated.
    - Idempotent: if order_id already exists in DB, returns the existing record's order_id.
    """
    telegram_id   = payload.get("telegram_id")
    story_ids     = payload.get("story_ids", [])
    order_id      = str(payload.get("order_id", "")).strip()
    amount        = float(payload.get("amount", 0) or 0)
    upi_id_shown  = str(payload.get("upi_id_shown", "")).strip()
    promo_code    = str(payload.get("promo_code", "")).strip().upper()
    username      = str(payload.get("username", "")).strip()
    first_name    = str(payload.get("first_name", "")).strip()
    last_name     = str(payload.get("last_name", "")).strip()

    if not telegram_id:
        logger.warning("create_pending_order: missing telegram_id — skipping")
        return {"success": True, "message": "skipped"}

    db = getattr(app.state, "db", None)
    if not db:
        logger.error("create_pending_order: DB not available")
        return {"success": True, "message": "db_unavailable"}

    try:
        # If no order_id, it's a legacy OD_ format, or invalid — generate a new structured one
        _invalid_pending = {"upi-manual", "upi_manual", "", "undefined", "null"}
        is_legacy = (
            not order_id
            or order_id.lower() in _invalid_pending
            or order_id.startswith("OD_")
            or order_id.startswith("OD-")
        )
        if is_legacy:
            order_id = await _make_arya_order_id(db, str(telegram_id), story_ids, source="miniapp")
            logger.info(f"create_pending_order: generated new order_id={order_id} for user {telegram_id}")

        # Idempotency: don't create duplicate if order_id already recorded
        existing = await db.db.orders.find_one({"order_id": order_id})
        if existing:
            logger.info(f"create_pending_order: order {order_id} already exists (status={existing.get('status')}) — returning")
            return {"success": True, "message": "already_exists", "order_id": order_id}

        tg_id_int = int(telegram_id) if str(telegram_id).isdigit() else 0

        resolved_items, subtotal_calc, story_ids_res, story_names = await _resolve_order_items(db, payload)
        if not story_names:
            story_names = story_ids

        pending_doc = {
            "order_id":       order_id,
            "user_id":        tg_id_int if tg_id_int else telegram_id,
            "username":       username,
            "first_name":     first_name,
            "last_name":      last_name,
            "story_ids":      story_ids_res or story_ids,
            "items":          resolved_items,
            "story_names":    story_names,
            "total":          amount,
            "promo_code":     promo_code if promo_code else None,
            "status":         "pending",
            "source":         "upi_manual_miniapp",
            "upi_id_shown":   upi_id_shown,
            "auto_deliver":   bool(payload.get("auto_deliver", True)),
            "created_at":     datetime.now(timezone.utc),
        }
        await db.db.orders.insert_one(pending_doc)
        logger.info(f"create_pending_order: created pending order {order_id} for user {telegram_id}")
        return {"success": True, "message": "pending_order_created", "order_id": order_id}

    except Exception as e:
        logger.error(f"create_pending_order error: {e}", exc_info=True)
        return {"success": True, "message": "error_ignored", "order_id": order_id}


@api_router.post("/send-receipt-telegram")
async def send_receipt_telegram(payload: dict):
    telegram_id = payload.get("telegram_id")
    pdf_base64 = payload.get("pdf_base64")
    order_id = payload.get("order_id", "Receipt")
    
    if not telegram_id or not pdf_base64:
        raise HTTPException(status_code=400, detail="Missing telegram_id or pdf_base64")
        
    import base64
    try:
        pdf_bytes = base64.b64decode(pdf_base64)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid PDF data")
        
    # Resolve correct bot token for the user
    token = await get_customer_bot_token(int(telegram_id))
    
    if not token:
        raise HTTPException(status_code=500, detail="Bot token is not configured on the server.")
        
    import httpx
    # Prepare multipart/form-data for Telegram sendDocument API
    files = {
        "document": (f"Receipt-{order_id}.pdf", pdf_bytes, "application/pdf")
    }
    data = {
        "chat_id": int(telegram_id),
        "caption": f"📄 Here is your receipt for Order ID: <code>{order_id}</code>.\nThank you for choosing Arya Premium!",
        "parse_mode": "HTML"
    }
    
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendDocument",
                data=data,
                files=files
            )
            resp_data = resp.json()
            if not resp_data.get("ok"):
                error_desc = resp_data.get("description", "Unknown error")
                logger.error(f"Telegram sendDocument failed: {error_desc}")
                raise HTTPException(status_code=500, detail=f"Telegram API Error: {error_desc}")
                
        # Log accurate receipt delivery event in Core Logs
        try:
            from utils import log_arya_event
            bot_doc = await arya_db.db.premium_bots.find_one({"token": token})
            b_id = bot_doc.get("id") if bot_doc else None
            asyncio.create_task(log_arya_event(
                event_type="RECEIPT DELIVERED",
                user_id=int(telegram_id),
                user_info={"bot_id": b_id},
                details=f"User requested order receipt for Order ID: <code>{order_id}</code>. Document sent to chat on Telegram.",
                bot_id=b_id
            ))
        except Exception:
            pass

        return {"success": True, "message": "Receipt sent to Telegram chat!"}
    except Exception as e:
        logger.error(f"Failed to send receipt via Telegram: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ===== Razorpay: Payment Link Webhook =====

@api_router.post("/razorpay-webhook")
async def razorpay_webhook(request: Request):
    """
    Webhook from Razorpay upon Payment Link payment completion.
    Razorpay sends event type "payment_link.paid" with payment_link entity.
    This auto-grants stories when user pays via the Payment Link flow.
    """
    import hmac as _hmac
    import hashlib as _hashlib
    import base64

    # Razorpay webhook signature verification
    webhook_secret = getattr(Config, "RAZORPAY_WEBHOOK_SECRET", "") or os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
    body = await request.body()

    if webhook_secret:
        sig = request.headers.get("X-Razorpay-Signature", "")
        expected = _hmac.new(webhook_secret.encode(), body, _hashlib.sha256).hexdigest()
        if not _hmac.compare_digest(expected, sig):
            logger.warning("Invalid Razorpay webhook signature")
            raise HTTPException(status_code=400, detail="Invalid webhook signature")

    try:
        data = await request.json()
    except Exception:
        data = {}

    event = data.get("event", "")
    logger.info(f"Razorpay webhook received: {event}")

    if event not in ("payment_link.paid", "payment.captured"):
        # Acknowledge other events without processing
        return {"status": "ok", "event": event}

    arya_db = app.state.db
    if not arya_db:
        logger.error("DB not connected — cannot process webhook")
        return {"status": "error", "detail": "DB not available"}

    try:
        if event == "payment_link.paid":
            pl = data.get("payload", {}).get("payment_link", {}).get("entity", {})
            payment_link_id = pl.get("id", "")
            status = pl.get("status", "")
            amount_paid = pl.get("amount_paid", 0)

            if status != "paid" or not payment_link_id:
                return {"status": "ok"}

            order = await arya_db.db.orders.find_one({"payment_link_id": payment_link_id})
            if not order:
                logger.warning(f"No order found for payment_link_id={payment_link_id}")
                return {"status": "ok"}

            if order.get("status") == "paid":
                # Already processed — idempotent
                return {"status": "ok", "detail": "already_processed"}

            tg_id = order.get("user_id")
            story_ids = order.get("story_ids", [])

            # Mark order as paid
            await arya_db.db.orders.update_one(
                {"_id": order["_id"]},
                {"$set": {
                    "status": "paid",
                    "amount_paid": amount_paid,
                    "updated_at": datetime.now(timezone.utc),
                    "source": "razorpay_link_webhook"
                }}
            )

            # Grant stories
            if tg_id:
                for sid in story_ids:
                    await arya_db.add_purchase(int(tg_id) if str(tg_id).isdigit() else tg_id, sid)

            # Log and audit records
            updated_order = {**order, "status": "paid", "amount_paid": amount_paid, "source": "razorpay_link_webhook"}
            asyncio.create_task(trigger_payment_log_from_order(updated_order))
            asyncio.create_task(record_purchased_stories(updated_order))
            asyncio.create_task(send_purchase_success_dm(arya_db, tg_id, order_doc=updated_order, payment_method="Razorpay", verified_by="Auto Verified By System"))

            logger.info(f"Webhook: Payment Link {payment_link_id} paid — granted {len(story_ids)} stories to user {tg_id}")

        elif event == "payment.captured":
            pay = data.get("payload", {}).get("payment", {}).get("entity", {})
            rzp_order_id = pay.get("order_id", "")
            rzp_payment_id = pay.get("id", "")

            if not rzp_order_id:
                return {"status": "ok"}

            # Find order by razorpay_order_id
            order = await arya_db.db.orders.find_one({"razorpay_order_id": rzp_order_id})
            if order and order.get("status") != "paid":
                tg_id = order.get("user_id")
                story_ids = order.get("story_ids", [])

                await arya_db.db.orders.update_one(
                    {"_id": order["_id"]},
                    {"$set": {
                        "status": "paid",
                        "razorpay_payment_id": rzp_payment_id,
                        "updated_at": datetime.now(timezone.utc),
                        "source": "razorpay_sdk_webhook"
                    }}
                )

                if tg_id:
                    for sid in story_ids:
                        await arya_db.add_purchase(int(tg_id) if str(tg_id).isdigit() else tg_id, sid)

                # Log and audit records
                updated_order = {**order, "status": "paid", "razorpay_payment_id": rzp_payment_id, "source": "razorpay_sdk_webhook"}
                asyncio.create_task(trigger_payment_log_from_order(updated_order))
                asyncio.create_task(record_purchased_stories(updated_order))
                asyncio.create_task(send_purchase_success_dm(arya_db, tg_id, order_doc=updated_order, payment_method="Razorpay", verified_by="Auto Verified By System"))

                logger.info(f"Webhook: payment.captured {rzp_payment_id} — granted {len(story_ids)} stories to user {tg_id}")

    except Exception as e:
        logger.error(f"Razorpay webhook processing error: {e}", exc_info=True)

    return {"status": "ok"}


# ===== OxaPay: Create Crypto Invoice =====

@api_router.post("/create-oxapay-order")
async def create_oxapay_order(payload: dict):
    """Create an OxaPay crypto invoice. Reads API key fresh at request time."""
    # Read key fresh each request so .env changes take effect without restart
    oxapay_key = getattr(Config, "OXAPAY_KEY", "") or os.environ.get("OXAPAY_KEY", "")
    if not oxapay_key:
        logger.error("OXAPAY_KEY not configured! Add it to .env")
        raise HTTPException(status_code=503, detail="Crypto payment not configured. Contact admin.")

    oxapay_env = getattr(Config, "OXAPAY_ENV", "production") or os.environ.get("OXAPAY_ENV", "production")
    is_sandbox = False
    if oxapay_key.lower().startswith("sandbox") or oxapay_key.lower() == "sandbox" or oxapay_env.lower() == "sandbox":
        is_sandbox = True

    base_url = "https://api.oxapay.com"

    story_ids  = payload.get("story_ids", [])
    tg_id      = payload.get("telegram_id") or 0
    username   = payload.get("username", "")
    first_name = payload.get("first_name", "") or ""
    # Only use first word of name for invoice (keep it short)
    customer_name = (first_name.split()[0] if first_name.strip() else "") or username or "Customer"

    if not story_ids:
        raise HTTPException(status_code=400, detail="Cart is empty")

    arya_db = app.state.db
    resolved_items, total_inr, story_ids, story_names = await _resolve_order_items(arya_db, payload)
    if not resolved_items:
        raise HTTPException(status_code=400, detail="No valid stories found in cart")

    promo_code = payload.get("promo_code", "")

    # Fetch settings for promo codes
    cfg = await arya_db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    # 1. Promo Code Discount
    discount = 0.0
    pcode_clean = str(promo_code).strip().upper()
    if pcode_clean:
        discount, err = await calculate_promo_discount(arya_db, pcode_clean, story_ids, total_inr, tg_id)
        if err:
            raise HTTPException(status_code=400, detail=f"Promo Code Error: {err}")

    total_inr = max(0.0, total_inr - discount)
    # OxaPay expects USD. Minimum $0.50.
    total_usd = max(0.5, round(total_inr / 85.0, 2))
    oid = await _make_arya_order_id(arya_db, str(tg_id), story_ids, source="miniapp")

    # Call OxaPay API
    oxapay_result = None
    oxapay_error = None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{base_url}/v1/payment/invoice",
                headers={
                    "merchant_api_key": oxapay_key,
                    "Content-Type": "application/json"
                },
                json={
                    "amount": total_usd,
                    "currency": "USD",
                    "lifetime": 30,
                    "fee_paid_by_payer": 1,
                    "order_id": oid,
                    "description": f"{len(resolved_items)} Arya Premium stories for {customer_name}",
                    "customer_name": customer_name,
                    "callback_url": "https://aryapremium.store/api/oxapay-webhook",
                    "return_url": f"https://t.me/{os.environ.get('BOT_USERNAME', 'UseAryaBot')}/app",
                    "sandbox": is_sandbox,
                }
            )
            oxapay_result = r.json()
    except Exception as e:
        oxapay_error = str(e)

    if oxapay_error:
        logger.error(f"OxaPay network error: {oxapay_error}")
        raise HTTPException(status_code=502, detail=f"Failed to reach OxaPay: {oxapay_error}")

    logger.info(f"OxaPay raw response: {oxapay_result}")

    # --- OxaPay NEW API v1 format (2024+) ---
    # Response: { "data": { "payment_url": "...", "track_id": "..." }, "status": 200, "message": "..." }
    # --- OxaPay OLD API format ---
    # Response: { "result": 100, "payLink": "...", "trackId": "..." }

    # Extract from new API format first (data.payment_url, data.track_id)
    data_obj  = oxapay_result.get("data") or {}
    pay_link  = (data_obj.get("payment_url") or data_obj.get("pay_url")
                 or oxapay_result.get("payLink") or oxapay_result.get("pay_link")
                 or oxapay_result.get("paylink"))
    track_id  = (data_obj.get("track_id") or data_obj.get("trackId")
                 or oxapay_result.get("trackId") or oxapay_result.get("track_id"))

    # Determine success: new API uses status==200, old API uses result==100
    status_code = oxapay_result.get("status") or oxapay_result.get("result")
    try:
        status_int = int(status_code) if status_code is not None else 0
    except (ValueError, TypeError):
        status_int = 0

    # Success = (status 200 or result 100) AND payLink present
    is_success = (status_int in (100, 200)) and bool(pay_link)

    if not is_success:
        logger.error(f"OxaPay rejected (status={status_code}, payLink={pay_link!r}): {oxapay_result}")
        raise HTTPException(status_code=502, detail=f"OxaPay error: {oxapay_result.get('message', 'Unknown')}")

    logger.info(f"OxaPay success: pay_link={pay_link} track_id={track_id}")

    # Store pending order in DB
    try:
        await arya_db.db.orders.insert_one({
            "order_id":    oid,
            "user_id":     int(tg_id) if str(tg_id).isdigit() else tg_id,
            "username":    username,
            "story_ids":   story_ids,
            "story_names": story_names,
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

    # === Send Bot DM with payment link ===
    async def _send_oxapay_dm():
        try:
            bot_token = getattr(Config, "BOT_TOKEN", "") or os.environ.get("BOT_TOKEN", "")
            if not bot_token or not tg_id:
                return
            story_list = "\n".join([f"  • {name}" for name in story_names])
            dm_text = (
                f"🪙 <b>Crypto Payment Invoice</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>Order ID:</b> <code>{oid}</code>\n"
                f"<b>Amount:</b> ${total_usd:.2f} (~₹{total_inr:.0f})\n"
                f"<b>Stories ({len(resolved_items)}):</b>\n{story_list}\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"💳 <b><a href=\'{pay_link}\'>Click here to Pay</a></b>\n"
                f"⏳ Link expires in <b>30 minutes</b>\n\n"
                f"✅ Your stories will be <b>auto-unlocked</b> after payment."
            )
            import aiohttp as _aiohttp
            async with _aiohttp.ClientSession() as _sess:
                await _sess.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={"chat_id": int(tg_id), "text": dm_text, "parse_mode": "HTML",
                          "disable_web_page_preview": True},
                    timeout=5
                )
        except Exception as _e:
            logger.warning(f"OxaPay DM send failed: {_e}")
    asyncio.create_task(_send_oxapay_dm())

    return {"success": True, "payLink": pay_link, "trackId": track_id}


@api_router.post("/oxapay-webhook")
async def oxapay_webhook(request: Request):
    """Webhook from OxaPay upon successful payment."""
    oxapay_key = getattr(Config, "OXAPAY_KEY", "") or os.environ.get("OXAPAY_KEY", "")
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    track_id = data.get("trackId") or data.get("track_id")  # new API uses track_id
    status   = data.get("status")

    # OxaPay sends status as "Paid" (old) or "paid" (new) — normalize
    status_lower = str(status).lower() if status else ""
    if status_lower != "paid" or not track_id:
        logger.info(f"OxaPay webhook ignored: status={status!r} track_id={track_id!r}")
        return {"success": False, "message": "Ignored or invalid status"}

    # Verify via OxaPay Inquiry API to prevent fake webhooks
    oxapay_env = getattr(Config, "OXAPAY_ENV", "production") or os.environ.get("OXAPAY_ENV", "production")
    is_sandbox = False
    if oxapay_key.lower().startswith("sandbox") or oxapay_key.lower() == "sandbox" or oxapay_env.lower() == "sandbox":
        is_sandbox = True
    base_url = "https://api.oxapay.com"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{base_url}/v1/payment/{track_id}",
                headers={"merchant_api_key": oxapay_key}
            )
            inquiry = r.json()
            # New API: inquiry.data.status, Old API: inquiry.status
            inq_data   = inquiry.get("data") or {}
            inq_status = (inq_data.get("status") or inquiry.get("status") or "").lower()
            if inq_status != "paid":
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

    # Log and audit records
    updated_order = {**order, "status": "paid", "payment_id": data.get("txID", ""), "paid_currency": data.get("payCurrency", "")}
    asyncio.create_task(trigger_payment_log_from_order(updated_order))
    asyncio.create_task(record_purchased_stories(updated_order))
    asyncio.create_task(send_purchase_success_dm(arya_db, user_id, order_doc=updated_order, payment_method="Crypto (Oxapay)", verified_by="Auto Verified By System"))

    logger.info(f"OxaPay ✅ unlocked {len(story_ids)} stories for user={user_id} trackId={track_id}")

    return {"success": True, "message": "Payment verified and processed"}


# ===== Paytm Payment Gateway: Create Order =====
@api_router.post("/create-paytm-order")
async def create_paytm_order(payload: dict):
    """Create Paytm order and initiate transaction for frontend checkout."""
    if not PAYTM_LIBS_AVAILABLE:
        raise HTTPException(status_code=500, detail="paytmchecksum library is not installed. Please run: pip install paytmchecksum")
        
    tg_id      = payload.get("telegram_id") or 0
    promo_code = payload.get("promo_code", "")

    arya_db = app.state.db
    cfg = await arya_db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    
    paytm_status = cfg.get("paytm_status", "hidden")
    if paytm_status == "hidden":
        raise HTTPException(status_code=400, detail="Paytm payments are currently disabled.")
    elif paytm_status == "disabled":
        raise HTTPException(status_code=400, detail="Paytm payments are currently disabled by the admin.")
        
    mid = cfg.get("paytm_mid", "").strip()
    merchant_key = cfg.get("paytm_merchant_key", "").strip()
    if not mid or not merchant_key:
        raise HTTPException(status_code=400, detail="Paytm credentials are not configured.")

    resolved_items, subtotal, story_ids, story_names = await _resolve_order_items(arya_db, payload)
    if not resolved_items:
        raise HTTPException(400, "No valid stories")
    
    discount = 0.0
    pcode_clean = str(promo_code).strip().upper()
    if pcode_clean:
        discount, err = await calculate_promo_discount(arya_db, pcode_clean, story_ids, subtotal, tg_id)
        if err:
            raise HTTPException(status_code=400, detail=f"Promo Code Error: {err}")
                
    platform_fee = 0.0
    if cfg.get("platform_fee_enabled", True):
        platform_fee = float(cfg.get("platform_fee_amount", 5.0))
        
    total = max(0.0, subtotal - discount + platform_fee)
    
    import uuid
    import json
    orderId = await _make_arya_order_id(arya_db, str(tg_id), story_ids, source="miniapp")
    paytm_env = cfg.get("paytm_env", "staging").strip().lower()
    is_sandbox = (paytm_env == "staging" or mid.startswith("TEST_") or "sandbox" in mid.lower())
    domain = "securegw-stage.paytm.in" if is_sandbox else "securegw.paytm.in"
    website = cfg.get("paytm_website", "WEBSTAGING" if is_sandbox else "DEFAULT").strip()
    
    callback_url = cfg.get("paytm_callback_url", "https://sliceurl.app/api/paytm-callback").strip()
    if "aryapremium.store" in callback_url:
        callback_url = callback_url.replace("aryapremium.store", "sliceurl.app")
    
    body = {
        "requestType": "Payment",
        "mid": mid,
        "websiteName": website,
        "orderId": orderId,
        "callbackUrl": callback_url,
        "txnAmount": {
            "value": f"{total:.2f}",
            "currency": "INR"
        },
        "userInfo": {
            "custId": f"CUST_{tg_id}" if tg_id else "CUST_GUEST"
        }
    }
    
    try:
        body_json = json.dumps(body, separators=(',', ':'))
        signature = paytmchecksum.generateSignature(body_json, merchant_key)
        
        payload_data = {
            "body": body,
            "head": {
                "signature": signature,
                "channelId": "WAP"
            }
        }
        
        init_url = f"https://{domain}/theia/api/v1/initiateTransaction?mid={mid}&orderId={orderId}"
        
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(init_url, json=payload_data, headers={"Content-Type": "application/json"})
            res_data = r.json()
            
        logger.info(f"Paytm raw response: {res_data}")
        
        body_res = res_data.get("body", {})
        result_info = body_res.get("resultInfo", {})
        if result_info.get("resultStatus") != "S":
            raise HTTPException(status_code=502, detail=f"Paytm Error: {result_info.get('resultMsg', 'Initiate Failed')}")
            
        txn_token = body_res.get("txnToken")
        if not txn_token:
            raise HTTPException(status_code=502, detail="Failed to retrieve transaction token from Paytm")
            
        # Save pending order
        order_doc = {
            "order_id": orderId,
            "user_id": str(tg_id),
            "story_ids": story_ids,
            "items": resolved_items,
            "story_names": story_names,
            "total": total,
            "source": "paytm",
            "status": "pending",
            "auto_deliver": bool(payload.get("auto_deliver", True)),
            "created_at": datetime.now(timezone.utc),
            "payment_id": None,
            "promo_code": pcode_clean if pcode_clean else None,
        }
        await arya_db.db.orders.insert_one(order_doc)
        
        return {
            "success": True,
            "mid": mid,
            "order_id": orderId,
            "txn_token": txn_token,
            "amount": total,
            "is_sandbox": is_sandbox
        }
    except Exception as e:
        logger.error(f"Paytm order creation failed: {e}", exc_info=True)
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))


# ===== Paytm Payment Gateway: Callback Webhook =====
@api_router.post("/paytm-callback")
async def paytm_callback(request: Request):
    """Callback redirect/webhook from Paytm after successful or failed payment transaction."""
    if not PAYTM_LIBS_AVAILABLE:
        raise HTTPException(status_code=500, detail="paytmchecksum library is not installed.")
        
    try:
        form_data = await request.form()
        form_dict = {k: v for k, v in form_data.items()}
    except Exception:
        raise HTTPException(400, "Invalid form data")
        
    logger.info(f"Paytm callback payload: {form_dict}")
    
    order_id = form_dict.get("ORDERID")
    checksum = form_dict.get("CHECKSUMHASH")
    txn_status = form_dict.get("STATUS")
    txn_id = form_dict.get("TXNID")
    
    if not order_id or not checksum:
        return Response(content="<h3>Invalid callback parameters</h3>", media_type="text/html")
        
    arya_db = app.state.db
    order = await arya_db.db.orders.find_one({"order_id": order_id})
    if not order:
        return Response(content="<h3>Order not found</h3>", media_type="text/html")
        
    cfg = await arya_db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    mid = cfg.get("paytm_mid", "").strip()
    merchant_key = cfg.get("paytm_merchant_key", "").strip()
    
    paytm_params = {k: v for k, v in form_dict.items() if k != "CHECKSUMHASH"}
    is_valid = paytmchecksum.verifySignature(paytm_params, merchant_key, checksum)
    if not is_valid:
        logger.warning(f"Paytm checksum verification failed for order {order_id}")
        return Response(content="<h3>Checksum Verification Failed</h3>", media_type="text/html")
        
    paytm_env = cfg.get("paytm_env", "staging").strip().lower()
    is_sandbox = (paytm_env == "staging" or mid.startswith("TEST_") or "sandbox" in mid.lower())
    domain = "securegw-stage.paytm.in" if is_sandbox else "securegw.paytm.in"
    
    status_verified = False
    try:
        import json
        status_body = {
            "mid": mid,
            "orderId": order_id
        }
        status_body_json = json.dumps(status_body, separators=(',', ':'))
        status_sig = paytmchecksum.generateSignature(status_body_json, merchant_key)
        
        status_payload = {
            "body": status_body,
            "head": {
                "signature": status_sig
            }
        }
        
        status_url = f"https://{domain}/v3/transactionStatus"
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(status_url, json=status_payload, headers={"Content-Type": "application/json"})
            status_res = r.json()
            
        logger.info(f"Paytm status query response: {status_res}")
        body_status = status_res.get("body", {})
        res_info = body_status.get("resultInfo", {})
        if res_info.get("resultStatus") == "TXN_SUCCESS" and body_status.get("txnId") == txn_id:
            status_verified = True
    except Exception as e:
        logger.error(f"Paytm status query error: {e}")
        
    if not status_verified and txn_status == "TXN_SUCCESS":
        if is_sandbox:
            status_verified = True
            
    if status_verified:
        if order.get("status") != "paid":
            await arya_db.db.orders.update_one(
                {"_id": order["_id"]},
                {"$set": {
                    "status": "paid",
                    "payment_id": txn_id,
                    "paid_at": datetime.now(timezone.utc),
                }}
            )
            
            user_id = order.get("user_id")
            story_ids = order.get("story_ids", [])
            if user_id:
                for sid in story_ids:
                    try:
                        await arya_db.add_purchase(user_id, sid)
                    except Exception as e:
                        logger.error(f"add_purchase error for {sid}: {e}")
                        
            updated_order = {**order, "status": "paid", "payment_id": txn_id}
            asyncio.create_task(trigger_payment_log_from_order(updated_order))
            asyncio.create_task(record_purchased_stories(updated_order))
            
            async def _send_paytm_success_dm():
                try:
                    bot_token = getattr(Config, "BOT_TOKEN", "") or os.environ.get("BOT_TOKEN", "")
                    bot_username = os.environ.get("BOT_USERNAME", "UseAryaBot")
                    if not bot_token or not user_id:
                        return
                    story_names = order.get("story_names", [])
                    story_list = "\n".join([f"  • {n}" for n in story_names]) if story_names else "  • Your purchased stories"
                    success_text = (
                        f"✅ <b>Payment Successful (Paytm)!</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n"
                        f"Your Paytm payment of ₹{order.get('total', 0)} was successful and stories are now unlocked! 🎉\n\n"
                        f"<b>Unlocked Stories:</b>\n{story_list}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📚 Open <b>Arya Premium</b> to listen to them now!"
                    )
                    keyboard = {"inline_keyboard": [[{
                        "text": "📚 Open Arya Premium",
                        "url": f"https://t.me/{bot_username}/app"
                    }]]}
                    async with httpx.AsyncClient(timeout=10) as client:
                        await client.post(
                            f"https://api.telegram.org/bot{bot_token}/sendMessage",
                            json={"chat_id": int(user_id), "text": success_text, "parse_mode": "HTML",
                                  "reply_markup": keyboard, "disable_web_page_preview": True}
                        )
                except Exception as _e:
                    logger.warning(f"Paytm DM send failed: {_e}")
                    
            asyncio.create_task(_send_paytm_success_dm())
            
        success_html = """
        <html>
          <head>
            <title>Payment Successful</title>
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <script src="https://telegram.org/js/telegram-web-app.js"></script>
            <style>
              body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: #111; color: #fff; text-align: center; padding: 40px 20px; }
              .card { background-color: #222; border-radius: 16px; padding: 30px; box-shadow: 0 4px 12px rgba(0,0,0,0.5); max-width: 400px; margin: 0 auto; border: 1px solid #333; }
              .checkmark { font-size: 60px; color: #4ade80; margin-bottom: 20px; }
              h2 { margin: 0 0 10px 0; font-size: 22px; }
              p { color: #aaa; font-size: 14px; line-height: 1.5; margin: 0 0 24px 0; }
              .btn { background-color: #007aff; color: white; border: none; padding: 12px 24px; border-radius: 8px; font-weight: bold; cursor: pointer; font-size: 14px; width: 100%; box-sizing: border-box; }
            </style>
            <script>
              window.onload = function() {
                const tg = window.Telegram?.WebApp;
                if (tg) {
                  tg.expand();
                  setTimeout(() => { tg.close(); }, 3000);
                }
              }
              function closeWindow() {
                const tg = window.Telegram?.WebApp;
                if (tg) { tg.close(); } else { window.close(); }
              }
            </script>
          </head>
          <body>
            <div class="card">
              <div class="checkmark">✓</div>
              <h2>Payment Successful!</h2>
              <p>Your payment via Paytm has been confirmed.<br>Your premium stories are now unlocked. You can return to the bot now.</p>
              <button class="btn" onclick="closeWindow()">Return to Bot</button>
            </div>
          </body>
        </html>
        """
        return Response(content=success_html, media_type="text/html")
        
    else:
        fail_html = """
        <html>
          <head>
            <title>Payment Failed</title>
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <script src="https://telegram.org/js/telegram-web-app.js"></script>
            <style>
              body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: #111; color: #fff; text-align: center; padding: 40px 20px; }
              .card { background-color: #222; border-radius: 16px; padding: 30px; box-shadow: 0 4px 12px rgba(0,0,0,0.5); max-width: 400px; margin: 0 auto; border: 1px solid #333; }
              .crossmark { font-size: 60px; color: #ef4444; margin-bottom: 20px; }
              h2 { margin: 0 0 10px 0; font-size: 22px; }
              p { color: #aaa; font-size: 14px; line-height: 1.5; margin: 0 0 24px 0; }
              .btn { background-color: #ef4444; color: white; border: none; padding: 12px 24px; border-radius: 8px; font-weight: bold; cursor: pointer; font-size: 14px; width: 100%; box-sizing: border-box; }
            </style>
            <script>
              function closeWindow() {
                const tg = window.Telegram?.WebApp;
                if (tg) { tg.close(); } else { window.close(); }
              }
            </script>
          </head>
          <body>
            <div class="card">
              <div class="crossmark">✗</div>
              <h2>Payment Failed / Pending</h2>
              <p>Paytm could not verify your payment transaction.<br>If money was debited, contact support for manual unlocking.</p>
              <button class="btn" onclick="closeWindow()">Close</button>
            </div>
          </body>
        </html>
        """
        return Response(content=fail_html, media_type="text/html")


# ===== PayU Payment Gateway: Create Order =====
@api_router.post("/create-payu-order")
async def create_payu_order(payload: dict):
    tg_id      = payload.get("telegram_id") or 0
    username   = payload.get("username", "") or ""
    first_name = payload.get("first_name", "") or ""
    promo_code = payload.get("promo_code", "")

    arya_db = app.state.db
    cfg = await arya_db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    
    payu_status = cfg.get("payu_status", "hidden")
    if payu_status == "hidden":
        raise HTTPException(status_code=400, detail="PayU payments are currently disabled.")
    elif payu_status == "disabled":
        raise HTTPException(status_code=400, detail="PayU payments are currently disabled by the admin.")
        
    merchant_key  = cfg.get("payu_merchant_key", "").strip()
    merchant_salt = cfg.get("payu_merchant_salt", "").strip()
    payu_env      = cfg.get("payu_env", "sandbox").strip().lower()
    
    is_sandbox = (payu_env in ("sandbox", "staging", "test") or merchant_key.lower().startswith("test") or "sandbox" in merchant_key.lower())
    
    # Fallback to official PayU sandbox test credentials if in sandbox mode & credentials empty
    if is_sandbox:
        if not merchant_key:
            merchant_key = "JPBCkj"
        if not merchant_salt:
            merchant_salt = "4R38IvW2"
    else:
        if not merchant_key or not merchant_salt:
            raise HTTPException(status_code=400, detail="PayU live credentials (Merchant Key & Merchant Salt) are not configured in Admin Panel.")

    resolved_items, subtotal, story_ids, story_names = await _resolve_order_items(arya_db, payload)
    if not resolved_items:
        raise HTTPException(status_code=400, detail="No valid stories found in cart")
    
    discount = 0.0
    pcode_clean = str(promo_code).strip().upper()
    if pcode_clean:
        discount, err = await calculate_promo_discount(arya_db, pcode_clean, story_ids, subtotal, tg_id)
        if err:
            raise HTTPException(status_code=400, detail=f"Promo Code Error: {err}")
                
    platform_fee = 0.0
    if cfg.get("platform_fee_enabled", True):
        platform_fee = float(cfg.get("platform_fee_amount", 5.0))
        
    total = max(0.0, subtotal - discount + platform_fee)
    
    import uuid
    import hashlib
    txnid = await _make_arya_order_id(arya_db, str(tg_id), story_ids, source="miniapp")
    
    action_url = "https://test.payu.in/_payment" if is_sandbox else "https://secure.payu.in/_payment"
    callback_url = cfg.get("payu_callback_url", "https://aryapremium.store/api/payu-callback").strip()
    
    firstname = (first_name.strip() if first_name.strip() else username.strip()) or "Customer"
    email = payload.get("email", "").strip() or (f"{username}@t.me" if username else "customer@aryapremium.store")
    phone = payload.get("phone", "").strip() or "9999999999"
    productinfo = f"{len(resolved_items)} Audiobook Stories"
    amount_str = f"{total:.2f}"
    
    udf1 = str(tg_id)
    udf2 = pcode_clean if pcode_clean else ""
    udf3 = ""
    udf4 = ""
    udf5 = ""

    # PayU Standard Hash Sequence:
    # sha512(key|txnid|amount|productinfo|firstname|email|udf1|udf2|udf3|udf4|udf5||||||SALT)
    hash_sequence = f"{merchant_key}|{txnid}|{amount_str}|{productinfo}|{firstname}|{email}|{udf1}|{udf2}|{udf3}|{udf4}|{udf5}||||||{merchant_salt}"
    payu_hash = hashlib.sha512(hash_sequence.encode('utf-8')).hexdigest().lower()
    
    # Save pending order document
    order_doc = {
        "order_id": txnid,
        "user_id": str(tg_id),
        "username": username,
        "story_ids": story_ids,
        "items": resolved_items,
        "story_names": story_names,
        "total": total,
        "source": "payu",
        "status": "pending",
        "auto_deliver": bool(payload.get("auto_deliver", True)),
        "created_at": datetime.now(timezone.utc),
        "payment_id": None,
        "promo_code": pcode_clean if pcode_clean else None,
    }
    await arya_db.db.orders.insert_one(order_doc)
    
    params_dict = {
        "key": merchant_key,
        "txnid": txnid,
        "amount": amount_str,
        "productinfo": productinfo,
        "firstname": firstname,
        "email": email,
        "phone": phone,
        "surl": callback_url,
        "furl": callback_url,
        "hash": payu_hash,
        "udf1": udf1,
        "udf2": udf2,
        "udf3": udf3,
        "udf4": udf4,
        "udf5": udf5,
    }

    return {
        "success": True,
        "action": action_url,
        "action_url": action_url,
        "order_id": txnid,
        "amount": total,
        "is_sandbox": is_sandbox,
        "params": params_dict,
        "payu_params": params_dict,
    }


# ===== PayU Payment Gateway: Callback Helper & Handlers =====
async def handle_payu_callback_data(form_dict: dict, request: Request):
    """Core logic to verify PayU callback payload and unlock purchased stories."""
    logger.info(f"PayU callback payload received: {form_dict}")
    
    txnid = form_dict.get("txnid")
    status_val = form_dict.get("status", "").strip()
    received_hash = form_dict.get("hash", "").strip()
    mihpayid = form_dict.get("mihpayid") or form_dict.get("payuMoneyId") or txnid
    
    if not txnid:
        return Response(content="<h3>Invalid PayU callback parameters (Missing txnid)</h3>", media_type="text/html")
        
    arya_db = app.state.db
    order = await arya_db.db.orders.find_one({"order_id": txnid})
    if not order:
        return Response(content="<h3>Order not found</h3>", media_type="text/html")
        
    cfg = await arya_db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    merchant_key = cfg.get("payu_merchant_key", "").strip()
    merchant_salt = cfg.get("payu_merchant_salt", "").strip()
    payu_env = cfg.get("payu_env", "sandbox").strip().lower()
    is_sandbox = (payu_env in ("sandbox", "staging", "test") or merchant_key.lower().startswith("test") or "sandbox" in merchant_key.lower())

    if is_sandbox:
        if not merchant_key: merchant_key = "JPBCkj"
        if not merchant_salt: merchant_salt = "4R38IvW2"

    # 1. Reverse SHA-512 Hash Verification
    # Additional charges format: additionalCharges|SALT|status||||||udf5|udf4|udf3|udf2|udf1|email|firstname|productinfo|amount|txnid|key
    # Standard format: SALT|status||||||udf5|udf4|udf3|udf2|udf1|email|firstname|productinfo|amount|txnid|key
    additional_charges = form_dict.get("additionalCharges", "")
    key_val = form_dict.get("key", merchant_key)
    amount_val = form_dict.get("amount", f"{order.get('total', 0):.2f}")
    productinfo = form_dict.get("productinfo", "")
    firstname = form_dict.get("firstname", "")
    email = form_dict.get("email", "")
    udf1 = form_dict.get("udf1", "")
    udf2 = form_dict.get("udf2", "")
    udf3 = form_dict.get("udf3", "")
    udf4 = form_dict.get("udf4", "")
    udf5 = form_dict.get("udf5", "")

    if additional_charges:
        ret_hash_seq = f"{additional_charges}|{merchant_salt}|{status_val}||||||{udf5}|{udf4}|{udf3}|{udf2}|{udf1}|{email}|{firstname}|{productinfo}|{amount_val}|{txnid}|{key_val}"
    else:
        ret_hash_seq = f"{merchant_salt}|{status_val}||||||{udf5}|{udf4}|{udf3}|{udf2}|{udf1}|{email}|{firstname}|{productinfo}|{amount_val}|{txnid}|{key_val}"

    import hashlib
    calculated_hash = hashlib.sha512(ret_hash_seq.encode('utf-8')).hexdigest().lower()
    hash_matched = (received_hash and calculated_hash == received_hash.lower())

    # 2. Direct Server-to-Server Verification (PayU Web Service Postservice API)
    status_verified_by_payu_api = False
    try:
        query_url = "https://test.payu.in/merchant/postservice?form=2" if is_sandbox else "https://info.payu.in/merchant/postservice?form=2"
        # Verify Payment Hash: sha512(key|command|var1|SALT)
        verify_hash_seq = f"{merchant_key}|verify_payment|{txnid}|{merchant_salt}"
        verify_hash = hashlib.sha512(verify_hash_seq.encode('utf-8')).hexdigest().lower()
        
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(query_url, data={
                "key": merchant_key,
                "command": "verify_payment",
                "var1": txnid,
                "hash": verify_hash
            })
            res_json = r.json()
            logger.info(f"PayU verify_payment S2S response for {txnid}: {res_json}")
            txn_details = res_json.get("transaction_details", {}).get(txnid, {})
            if txn_details.get("status", "").lower() in ("success", "paid"):
                status_verified_by_payu_api = True
    except Exception as ve:
        logger.warning(f"PayU S2S status query exception for {txnid}: {ve}")

    status_success = (status_val.lower() in ("success", "paid"))
    
    # Final approval condition:
    # Either Server-to-Server verified OR (Reverse Hash matched AND Status is Success) OR Sandbox bypass
    is_valid_transaction = status_verified_by_payu_api or (hash_matched and status_success) or (is_sandbox and status_success)

    if is_valid_transaction:
        if order.get("status") != "paid":
            await arya_db.db.orders.update_one(
                {"_id": order["_id"]},
                {"$set": {
                    "status": "paid",
                    "payment_id": mihpayid,
                    "paid_at": datetime.now(timezone.utc),
                }}
            )
            
            user_id = order.get("user_id")
            story_ids = order.get("story_ids", [])
            if user_id:
                for sid in story_ids:
                    try:
                        await arya_db.add_purchase(user_id, sid)
                    except Exception as e:
                        logger.error(f"add_purchase error for {sid}: {e}")
                        
            updated_order = {**order, "status": "paid", "payment_id": mihpayid}
            asyncio.create_task(trigger_payment_log_from_order(updated_order))
            asyncio.create_task(record_purchased_stories(updated_order))
            
            async def _send_payu_success_dm():
                try:
                    bot_token = getattr(Config, "BOT_TOKEN", "") or os.environ.get("BOT_TOKEN", "")
                    bot_username = os.environ.get("BOT_USERNAME", "UseAryaBot")
                    if not bot_token or not user_id:
                        return
                    story_names = order.get("story_names", [])
                    story_list = "\n".join([f"  • {n}" for n in story_names]) if story_names else "  • Your purchased stories"
                    success_text = (
                        f"✅ <b>Payment Successful (PayU)!</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n"
                        f"Your PayU payment of ₹{order.get('total', 0)} was successful and stories are now unlocked! 🎉\n\n"
                        f"<b>Unlocked Stories:</b>\n{story_list}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📚 Open <b>Arya Premium</b> to listen to them now!"
                    )
                    keyboard = {"inline_keyboard": [[{
                        "text": "📚 Open Arya Premium",
                        "url": f"https://t.me/{bot_username}/app"
                    }]]}
                    async with httpx.AsyncClient(timeout=10) as client:
                        await client.post(
                            f"https://api.telegram.org/bot{bot_token}/sendMessage",
                            json={"chat_id": int(user_id), "text": success_text, "parse_mode": "HTML",
                                  "reply_markup": keyboard, "disable_web_page_preview": True}
                        )
                except Exception as _e:
                    logger.warning(f"PayU DM send failed: {_e}")
                    
            asyncio.create_task(_send_payu_success_dm())
            
        success_html = """
        <html>
          <head>
            <title>Payment Successful</title>
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <script src="https://telegram.org/js/telegram-web-app.js"></script>
            <style>
              body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: #111; color: #fff; text-align: center; padding: 40px 20px; }
              .card { background-color: #222; border-radius: 16px; padding: 30px; box-shadow: 0 4px 12px rgba(0,0,0,0.5); max-width: 400px; margin: 0 auto; border: 1px solid #333; }
              .checkmark { font-size: 60px; color: #4ade80; margin-bottom: 20px; }
              h2 { margin: 0 0 10px 0; font-size: 22px; }
              p { color: #aaa; font-size: 14px; line-height: 1.5; margin: 0 0 24px 0; }
              .btn { background-color: #2563eb; color: white; border: none; padding: 12px 24px; border-radius: 8px; font-weight: bold; cursor: pointer; font-size: 14px; width: 100%; box-sizing: border-box; }
            </style>
            <script>
              window.onload = function() {
                const tg = window.Telegram?.WebApp;
                if (tg) {
                  tg.expand();
                  setTimeout(() => { tg.close(); }, 3000);
                }
              }
              function closeWindow() {
                const tg = window.Telegram?.WebApp;
                if (tg) { tg.close(); } else { window.close(); }
              }
            </script>
          </head>
          <body>
            <div class="card">
              <div class="checkmark">✓</div>
              <h2>Payment Successful!</h2>
              <p>Your payment via PayU has been confirmed.<br>Your premium stories are now unlocked. You can return to the bot now.</p>
              <button class="btn" onclick="closeWindow()">Return to Bot</button>
            </div>
          </body>
        </html>
        """
        return Response(content=success_html, media_type="text/html")
    else:
        fail_html = """
        <html>
          <head>
            <title>Payment Failed</title>
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <script src="https://telegram.org/js/telegram-web-app.js"></script>
            <style>
              body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: #111; color: #fff; text-align: center; padding: 40px 20px; }
              .card { background-color: #222; border-radius: 16px; padding: 30px; box-shadow: 0 4px 12px rgba(0,0,0,0.5); max-width: 400px; margin: 0 auto; border: 1px solid #333; }
              .crossmark { font-size: 60px; color: #ef4444; margin-bottom: 20px; }
              h2 { margin: 0 0 10px 0; font-size: 22px; }
              p { color: #aaa; font-size: 14px; line-height: 1.5; margin: 0 0 24px 0; }
              .btn { background-color: #ef4444; color: white; border: none; padding: 12px 24px; border-radius: 8px; font-weight: bold; cursor: pointer; font-size: 14px; width: 100%; box-sizing: border-box; }
            </style>
            <script>
              function closeWindow() {
                const tg = window.Telegram?.WebApp;
                if (tg) { tg.close(); } else { window.close(); }
              }
            </script>
          </head>
          <body>
            <div class="card">
              <div class="crossmark">✗</div>
              <h2>Payment Failed / Pending</h2>
              <p>PayU transaction was not completed or failed.<br>If money was debited, contact support for manual unlocking.</p>
              <button class="btn" onclick="closeWindow()">Close</button>
            </div>
          </body>
        </html>
        """
        return Response(content=fail_html, media_type="text/html")

@api_router.post("/payu-callback")
async def payu_callback_post(request: Request):
    """Handle PayU Form POST callback."""
    try:
        form_data = await request.form()
        form_dict = {k: str(v) for k, v in form_data.items()}
    except Exception:
        form_dict = {}
    return await handle_payu_callback_data(form_dict, request)

@api_router.get("/payu-callback")
async def payu_callback_get(request: Request):
    """Handle PayU GET redirect callback."""
    form_dict = {k: str(v) for k, v in request.query_params.items()}
    return await handle_payu_callback_data(form_dict, request)


# ===== Cashfree Payment Gateway Endpoints =====

@api_router.post("/create-cashfree-order")
async def create_cashfree_order(payload: dict):
    """Create Cashfree PG order and generate payment session for frontend checkout."""
    tg_id      = payload.get("telegram_id") or 0
    username   = payload.get("username", "") or ""
    first_name = payload.get("first_name", "") or ""
    promo_code = payload.get("promo_code", "")

    arya_db = app.state.db
    cfg = await arya_db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    
    cf_status = cfg.get("cashfree_status", "hidden")
    if cf_status == "hidden":
        raise HTTPException(status_code=400, detail="Cashfree payments are currently disabled.")
    elif cf_status == "disabled":
        raise HTTPException(status_code=400, detail="Cashfree payments are currently disabled by the admin.")
        
    app_id     = (cfg.get("cashfree_app_id", "") or cfg.get("cashfree_api_id", "")).strip()
    secret_key = cfg.get("cashfree_secret_key", "").strip()
    cf_env     = cfg.get("cashfree_env", "sandbox").strip().lower()
    
    if not app_id or not secret_key:
        raise HTTPException(status_code=400, detail="Cashfree credentials (App ID & Secret Key) are not configured in Admin Panel.")

    is_sandbox = (cf_env in ("sandbox", "staging", "test") or "TEST" in app_id.upper() or "SANDBOX" in app_id.upper())

    resolved_items, subtotal, story_ids, story_names = await _resolve_order_items(arya_db, payload)
    if not resolved_items:
        raise HTTPException(status_code=400, detail="No valid stories found in cart")
    
    discount = 0.0
    pcode_clean = str(promo_code).strip().upper()
    if pcode_clean:
        discount, err = await calculate_promo_discount(arya_db, pcode_clean, story_ids, subtotal, tg_id)
        if err:
            raise HTTPException(status_code=400, detail=f"Promo Code Error: {err}")
                
    platform_fee = 0.0
    if cfg.get("platform_fee_enabled", True):
        platform_fee = float(cfg.get("platform_fee_amount", 5.0))
        
    total = max(0.0, subtotal - discount + platform_fee)
    
    order_id = await _make_arya_order_id(arya_db, str(tg_id), story_ids, source="miniapp")
    
    base_url = "https://sandbox.cashfree.com/pg" if is_sandbox else "https://api.cashfree.com/pg"
    orders_url = f"{base_url}/orders"
    
    import uuid, re
    customer_id = f"cust_{tg_id}" if tg_id else f"cust_{uuid.uuid4().hex[:8]}"
    
    # Extract customer contact details passed from frontend
    raw_name = str(payload.get("customer_name") or payload.get("name") or first_name or username or "").strip()
    clean_name = re.sub(r'[^a-zA-Z0-9\s]', '', raw_name).strip()
    customer_name = clean_name[:50] if clean_name else "Customer"

    # Extract 10-digit phone number
    raw_phone = str(payload.get("phone") or payload.get("customer_phone") or "").strip()
    phone_digits = re.sub(r'\D', '', raw_phone)
    if len(phone_digits) >= 10:
        customer_phone = phone_digits[-10:]
    else:
        customer_phone = phone_digits if phone_digits else "9999999999"

    customer_email = payload.get("email", "").strip() or (f"{username}@t.me" if username else "customer@sliceurl.app")
    
    callback_url = cfg.get("cashfree_callback_url", "https://sliceurl.app/api/cashfree-callback").strip()
    return_url_base = cfg.get("cashfree_return_url", "https://isaythanks.vercel.app").strip()
    if "order_id=" in return_url_base:
        return_url = return_url_base
    elif "?" in return_url_base:
        return_url = f"{return_url_base}&order_id={order_id}"
    else:
        return_url = f"{return_url_base}?order_id={order_id}"
    
    cf_payload = {
        "order_id": order_id,
        "order_amount": round(total, 2),
        "order_currency": "INR",
        "customer_details": {
            "customer_id": customer_id[:50],
            "customer_name": customer_name[:50],
            "customer_email": customer_email[:50],
            "customer_phone": customer_phone[:15]
        },
        "order_meta": {
            "return_url": return_url,
            "notify_url": callback_url
        },
        "order_note": order_id
    }

    headers = {
        "x-client-id": app_id,
        "x-client-secret": secret_key,
        "x-api-version": "2023-08-01",
        "Content-Type": "application/json"
    }
    
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(orders_url, json=cf_payload, headers=headers)
            res_json = resp.json()
            logger.info(f"Cashfree create order response: status={resp.status_code}, body={res_json}")
            
            if resp.status_code not in (200, 201):
                err_msg = res_json.get("message") or res_json.get("detail") or f"Cashfree HTTP {resp.status_code}"
                raise HTTPException(status_code=400, detail=f"Cashfree API Error: {err_msg}")
                
            payment_session_id = res_json.get("payment_session_id")
            cf_order_id = res_json.get("cf_order_id") or res_json.get("order_id") or order_id
            payment_link = res_json.get("payment_link")

            tg_id_int = int(tg_id) if str(tg_id).isdigit() else tg_id
            order_doc = {
                "order_id": order_id,
                "cf_order_id": str(cf_order_id),
                "payment_session_id": payment_session_id,
                "user_id": tg_id_int,
                "username": username or "Unknown",
                "customer_name": customer_name,
                "customer_phone": customer_phone,
                "phone": customer_phone,
                "story_ids": story_ids,
                "items": resolved_items,
                "story_names": story_names,
                "subtotal": subtotal,
                "discount": discount,
                "promo_code": pcode_clean if discount > 0 else None,
                "platform_fee": platform_fee,
                "total": total,
                "gateway": "cashfree",
                "status": "pending",
                "auto_deliver": bool(payload.get("auto_deliver", True)),
                "created_at": datetime.now(timezone.utc)
            }
            try:
                await arya_db.db.orders.insert_one(order_doc)
            except Exception as _ex:
                logger.warning(f"Failed to insert pending cashfree order: {_ex}")

            checkout_pay_link = f"https://aryapremium.store/api/cashfree-pay?session_id={payment_session_id}&sandbox={'true' if is_sandbox else 'false'}"
            return {
                "success": True,
                "order_id": order_id,
                "cf_order_id": cf_order_id,
                "payment_session_id": payment_session_id,
                "payment_link": checkout_pay_link,
                "amount": total,
                "is_sandbox": is_sandbox,
                "app_id": app_id
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Cashfree create order exception: {e}")
        raise HTTPException(status_code=500, detail=f"Cashfree Exception: {str(e)}")


@api_router.get("/cashfree-pay", response_class=HTMLResponse)
@api_router.get("/pay/cashfree", response_class=HTMLResponse)
async def cashfree_pay_page(session_id: str = Query(""), sandbox: bool = Query(False)):
    """Serves animated dark checkout wrapper page from expand-cards-showcase for Cashfree launch."""
    is_sandbox_str = "true" if sandbox else "false"
    html_content = '''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
  <title>Opening payment page — Secure Checkout</title>
  <script src="https://sdk.cashfree.com/js/v3/cashfree.js"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --checkout-bg: #0b0b0b;
      --checkout-fg: #f8fafc;
      --checkout-muted: #a1a1aa;
      --checkout-track: #27272a;
      --primary-color: #3b82f6;
    }
    body, html {
      background-color: #0b0b0b;
      color: #f8fafc;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      height: 100%;
      width: 100%;
      overflow: hidden;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
    }
    .checkout-screen {
      position: fixed;
      inset: 0;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 24px;
      padding-top: env(safe-area-inset-top, 24px);
      padding-bottom: calc(env(safe-area-inset-bottom, 24px) + 8vh);
      text-align: center;
    }
    @keyframes checkout-mark-in {
      from {
        opacity: 0;
        transform: scale(0.9) translate3d(0, 6px, 0);
      }
      to {
        opacity: 1;
        transform: scale(1) translate3d(0, 0, 0);
      }
    }
    @keyframes checkout-float {
      0%, 100% {
        transform: translate3d(0, -3px, 0);
      }
      50% {
        transform: translate3d(0, 3px, 0);
      }
    }
    @keyframes checkout-rise {
      from {
        opacity: 0;
        transform: translate3d(0, 8px, 0);
      }
      to {
        opacity: 1;
        transform: translate3d(0, 0, 0);
      }
    }
    @keyframes checkout-track-run {
      0% {
        transform: translate3d(-100%, 0, 0);
      }
      100% {
        transform: translate3d(240%, 0, 0);
      }
    }
    .checkout-mark {
      display: block;
      width: 132px;
      height: 132px;
      opacity: 0;
      animation: checkout-mark-in 620ms cubic-bezier(0.22, 1, 0.36, 1) forwards;
      will-change: transform, opacity;
    }
    .checkout-mark-float {
      display: block;
      width: 100%;
      height: 100%;
      animation: checkout-float 5.2s ease-in-out 700ms infinite;
      will-change: transform;
    }
    .checkout-rise {
      opacity: 0;
      animation: checkout-rise 560ms cubic-bezier(0.22, 1, 0.36, 1) forwards;
      will-change: transform, opacity;
    }
    h1.checkout-title {
      margin-top: 44px;
      font-size: 22px;
      font-weight: 500;
      line-height: 1.25;
      letter-spacing: -0.02em;
    }
    @media (min-width: 640px) {
      h1.checkout-title {
        font-size: 25px;
      }
    }
    p.checkout-subtitle {
      margin-top: 12px;
      max-width: 19rem;
      font-size: 14px;
      font-weight: 400;
      line-height: 1.6;
      letter-spacing: -0.005em;
      color: #a1a1aa;
    }
    .checkout-bar-container {
      margin-top: 48px;
    }
    .checkout-bar {
      position: relative;
      height: 3px;
      width: 160px;
      overflow: hidden;
      border-radius: 9999px;
      background-color: #27272a;
    }
    .checkout-bar-fill {
      position: absolute;
      top: 0;
      bottom: 0;
      left: 0;
      display: block;
      width: 42%;
      border-radius: 9999px;
      background-color: #f8fafc;
      animation: checkout-track-run 1.35s cubic-bezier(0.65, 0, 0.35, 1) infinite;
      will-change: transform;
    }
    .checkout-exit {
      opacity: 0;
      transition: opacity 420ms ease, transform 420ms cubic-bezier(0.22, 1, 0.36, 1);
      transform: translate3d(0, -6px, 0);
    }
    .btn-fallback {
      display: inline-block;
      margin-top: 24px;
      padding: 10px 24px;
      background: #2563eb;
      color: #fff;
      font-size: 14px;
      font-weight: 500;
      border-radius: 10px;
      text-decoration: none;
      border: none;
      cursor: pointer;
      transition: background 0.2s;
    }
    .btn-fallback:hover {
      background: #1d4ed8;
    }
  </style>
</head>
<body>
  <div class="checkout-screen" id="wrapperScreen">
    <div id="contentBlock" style="display: flex; flex-direction: column; align-items: center;">
      <span class="checkout-mark">
        <span class="checkout-mark-float">
          <svg viewBox="0 0 420 420" width="132" height="132" xmlns="http://www.w3.org/2000/svg" fill="none" aria-hidden="true" focusable="false" shape-rendering="geometricPrecision" style="display:block; width:100%; height:100%;">
            <defs><clipPath id="oneworks-avatar-export-frame"><path d="M 29.53125 0 H 390.46875 Q 420 0 420 29.53125 V 390.46875 Q 420 420 390.46875 420 H 29.53125 Q 0 420 0 390.46875 V 29.53125 Q 0 0 29.53125 0 Z"/></clipPath></defs><g clip-path="url(#oneworks-avatar-export-frame)"><path d="M 29.53125 0 H 390.46875 Q 420 0 420 29.53125 V 390.46875 Q 420 420 390.46875 420 H 29.53125 Q 0 420 0 390.46875 V 29.53125 Q 0 0 29.53125 0 Z" fill="#0e4fe7"/><g><title>Interactive default avatar</title><defs><clipPath id="r0-clip"><path d="M 71.05 202.87 L 71.92 187.76 L 72.13 184.85 L 74.74 169.98 L 79.05 155.51 L 85.00 141.62 L 86.15 139.00 L 93.60 126.03 L 102.52 114.02 L 104.33 111.77 L 114.43 101.07 L 116.80 99.08 L 127.82 89.90 L 139.88 82.12 L 142.19 80.66 L 145.02 79.26 L 157.01 73.52 L 159.72 72.43 L 162.81 71.40 L 174.08 67.78 L 176.71 67.05 L 179.59 66.38 L 182.70 65.77 L 186.02 65.23 L 193.85 64.13 L 196.21 63.81 L 198.67 63.55 L 201.22 63.33 L 203.84 63.18 L 206.51 63.07 L 209.20 63.03 L 211.90 63.04 L 214.58 63.11 L 217.23 63.23 L 219.82 63.41 L 222.34 63.65 L 224.77 63.93 L 231.92 64.94 L 235.35 65.44 L 238.59 66.01 L 241.61 66.64 L 244.39 67.34 L 246.91 68.09 L 258.49 71.81 L 261.43 72.87 L 263.98 73.98 L 276.19 79.82 L 278.81 81.25 L 290.64 88.88 L 293.10 90.60 L 304.25 99.89 L 306.32 101.90 L 316.50 112.67 L 325.34 124.58 L 326.94 127.03 L 334.43 140.06 L 340.36 153.87 L 344.65 168.29 L 345.47 171.14 L 348.08 186.03 L 348.95 201.13 L 348.08 216.24 L 347.87 219.15 L 345.26 234.02 L 340.95 248.49 L 335.00 262.38 L 333.85 265.00 L 326.40 277.97 L 317.48 289.98 L 315.67 292.23 L 305.57 302.93 L 303.20 304.92 L 292.18 314.10 L 280.12 321.88 L 277.81 323.34 L 274.98 324.74 L 262.99 330.48 L 260.28 331.57 L 257.19 332.60 L 245.92 336.22 L 243.29 336.95 L 240.41 337.62 L 237.30 338.23 L 233.98 338.77 L 226.15 339.87 L 223.79 340.19 L 221.33 340.45 L 218.78 340.67 L 216.16 340.82 L 213.49 340.93 L 210.80 340.97 L 208.10 340.96 L 205.42 340.89 L 202.77 340.77 L 200.18 340.59 L 197.66 340.35 L 195.23 340.07 L 188.08 339.06 L 184.65 338.56 L 181.41 337.99 L 178.39 337.36 L 175.61 336.66 L 173.09 335.91 L 161.51 332.19 L 158.57 331.13 L 156.02 330.02 L 143.81 324.18 L 141.19 322.75 L 129.36 315.12 L 126.90 313.40 L 115.75 304.11 L 113.68 302.10 L 103.50 291.33 L 94.66 279.42 L 93.06 276.97 L 85.57 263.94 L 79.64 250.13 L 75.35 235.71 L 74.53 232.86 L 71.92 217.97 Z"/></clipPath><clipPath id="r0-eye-0-highlight-clip"><path d="M 179.036 150.918 L 181.171 150.371 L 183.359 150.222 L 185.514 150.478 L 187.554 151.129 L 189.401 152.150 L 190.983 153.501 L 192.241 155.130 L 193.125 156.975 L 193.837 158.990 L 194.547 161.014 L 195.255 163.047 L 195.960 165.089 L 196.663 167.140 L 197.364 169.199 L 198.063 171.268 L 198.759 173.345 L 199.219 175.403 L 199.253 177.526 L 198.862 179.632 L 198.059 181.641 L 196.876 183.476 L 195.358 185.066 L 193.563 186.349 L 191.561 187.278 L 191.561 187.278 L 191.561 187.278 L 191.561 187.278 L 191.561 187.278 L 191.561 187.278 L 191.561 187.278 L 191.561 187.278 L 191.561 187.278 L 189.428 187.817 L 187.246 187.943 L 185.099 187.654 L 183.070 186.959 L 181.237 185.886 L 179.669 184.474 L 178.428 182.780 L 177.562 180.867 L 176.865 178.790 L 176.166 176.722 L 175.465 174.663 L 174.762 172.614 L 174.056 170.573 L 173.348 168.543 L 172.637 166.521 L 171.924 164.509 L 171.447 162.523 L 171.395 160.467 L 171.771 158.421 L 172.561 156.464 L 173.734 154.671 L 175.244 153.111 L 177.034 151.844 L 179.036 150.918 L 179.036 150.918 L 179.036 150.918 L 179.036 150.918 L 179.036 150.918 L 179.036 150.918 L 179.036 150.918 L 179.036 150.918 Z"/></clipPath><clipPath id="r0-eye-1-highlight-clip"><path d="M 254.542 146.517 L 256.603 147.163 L 258.530 148.174 L 260.248 149.514 L 261.692 151.129 L 262.806 152.957 L 263.548 154.929 L 263.888 156.968 L 263.814 158.996 L 263.356 162.243 L 262.891 165.513 L 262.421 168.806 L 261.945 172.121 L 261.464 175.459 L 260.977 178.819 L 260.484 182.202 L 259.985 185.607 L 259.472 187.657 L 258.567 189.540 L 257.305 191.183 L 255.735 192.524 L 253.917 193.510 L 251.920 194.104 L 249.822 194.283 L 247.703 194.041 L 247.703 194.041 L 247.703 194.041 L 247.703 194.041 L 247.703 194.041 L 247.703 194.041 L 247.703 194.041 L 247.703 194.041 L 247.703 194.041 L 245.644 193.387 L 243.725 192.346 L 242.019 190.959 L 240.592 189.277 L 239.499 187.367 L 238.781 185.301 L 238.467 183.158 L 238.568 181.021 L 239.066 177.618 L 239.559 174.237 L 240.046 170.877 L 240.528 167.538 L 241.004 164.221 L 241.475 160.926 L 241.940 157.653 L 242.400 154.401 L 242.886 152.458 L 243.765 150.678 L 245.002 149.130 L 246.552 147.872 L 248.353 146.954 L 250.336 146.411 L 252.426 146.263 L 254.542 146.517 L 254.542 146.517 L 254.542 146.517 L 254.542 146.517 L 254.542 146.517 L 254.542 146.517 L 254.542 146.517 L 254.542 146.517 Z"/></clipPath><filter id="r0-face-shadow-blur" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="0"/></filter><filter id="r0-avatar-shadow" x="-100%" y="-100%" width="300%" height="300%"><feDropShadow dx="8.485281374238571" dy="8.48528137423857" stdDeviation="8" flood-color="#000000" flood-opacity="0.24"/></filter></defs><g filter="url(#r0-avatar-shadow)" transform="translate(-59.3965 107.977) translate(210 210) rotate(17.435105705830953) scale(1.8905) translate(-210 -210)"><g data-avatar-entity-preset="dog"><defs><filter id="r0-entity-face-shadow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="0"/></filter><filter id="r0-entity-outline" x="-30%" y="-30%" width="160%" height="160%"><feMorphology in="SourceAlpha" operator="dilate" radius="2" result="expanded"/><feFlood flood-color="#000000" flood-opacity="0.8"/><feComposite in2="expanded" operator="in"/><feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge></filter><clipPath id="r0-entity-occlusion-clip-1"><path d="M 219.09 250.82 L 219.13 250.70 L 219.24 250.56 L 219.47 250.32 L 219.75 250.07 L 220.01 249.87 L 220.29 249.68 L 220.60 249.48 L 220.95 249.28 L 221.34 249.08 L 221.76 248.87 L 222.22 248.65 L 222.72 248.42 L 223.25 248.19 L 223.82 247.96 L 224.42 247.71 L 225.06 247.46 L 225.74 247.21 L 226.47 246.94 L 227.25 246.66 L 228.14 246.36 L 229.36 245.94 L 230.57 245.54 L 231.42 245.26 L 232.16 245.03 L 232.81 244.83 L 233.40 244.66 L 233.93 244.52 L 234.41 244.40 L 234.82 244.31 L 235.18 244.24 L 235.49 244.19 L 235.74 244.17 L 235.94 244.17 L 236.08 244.20 L 236.17 244.25 L 236.19 244.34 L 236.15 244.45 L 236.04 244.60 L 235.81 244.83 L 235.53 245.08 L 235.27 245.28 L 234.99 245.48 L 234.68 245.67 L 234.33 245.87 L 231.84 247.26 L 231.61 247.38 L 231.36 247.50 L 231.09 247.63 L 230.80 247.76 L 230.49 247.90 L 230.15 248.03 L 229.80 248.18 L 229.42 248.32 L 229.02 248.47 L 228.60 248.63 L 228.13 248.80 L 227.62 248.98 L 226.90 249.22 L 226.90 249.22 L 226.18 249.46 L 225.68 249.62 L 225.25 249.75 L 224.86 249.87 L 224.52 249.97 L 224.21 250.06 L 223.93 250.13 L 223.68 250.18 L 220.46 250.85 L 220.10 250.92 L 219.79 250.96 L 219.54 250.98 L 219.34 250.98 L 219.20 250.95 L 219.11 250.90 Z" transform="translate(71.8645347270797 -49.39040223516069)"/></clipPath><clipPath id="r0-entity-occlusion-clip-0"><path d="M 183.46 243.67 L 183.50 243.55 L 183.60 243.47 L 183.75 243.42 L 183.96 243.40 L 184.22 243.40 L 184.53 243.43 L 184.89 243.49 L 185.31 243.57 L 185.77 243.68 L 186.29 243.81 L 186.86 243.97 L 187.49 244.16 L 188.18 244.38 L 188.94 244.63 L 189.82 244.93 L 191.07 245.37 L 192.31 245.82 L 193.21 246.16 L 194.00 246.46 L 194.73 246.75 L 195.40 247.02 L 196.03 247.29 L 196.61 247.55 L 197.15 247.81 L 197.65 248.05 L 198.11 248.29 L 198.53 248.52 L 198.91 248.74 L 199.24 248.96 L 199.53 249.17 L 199.78 249.37 L 199.98 249.57 L 200.15 249.78 L 200.31 250.04 L 200.41 250.28 L 200.43 250.43 L 200.39 250.54 L 200.29 250.62 L 200.14 250.67 L 199.93 250.69 L 199.67 250.69 L 199.36 250.66 L 199.00 250.60 L 198.58 250.52 L 198.12 250.41 L 195.30 249.70 L 195.00 249.62 L 194.66 249.53 L 194.29 249.42 L 193.89 249.29 L 193.44 249.14 L 192.92 248.96 L 192.19 248.70 L 192.19 248.70 L 191.45 248.44 L 190.93 248.24 L 190.46 248.06 L 190.04 247.90 L 189.64 247.73 L 189.27 247.58 L 188.93 247.42 L 188.61 247.27 L 188.32 247.13 L 188.05 246.99 L 185.78 245.80 L 185.36 245.57 L 184.98 245.35 L 184.65 245.13 L 184.36 244.92 L 184.11 244.72 L 183.91 244.52 L 183.74 244.32 L 183.58 244.06 L 183.48 243.82 Z" transform="translate(-71.8645347270797 -51.52484617981341)"/></clipPath><mask id="r0-entity-occlusion-1" x="-420" y="-420" width="1260" height="1260" maskUnits="userSpaceOnUse"><rect x="-420" y="-420" width="1260" height="1260" fill="white"/><g clip-path="url(#r0-entity-occlusion-clip-1)"><path d="M 113.96 251.75 L 114.01 240.42 L 115.45 226.76 L 119.51 204.31 L 120.20 200.67 L 125.05 178.26 L 128.84 164.62 L 129.95 161.12 L 133.89 149.89 L 138.13 140.19 L 142.69 131.69 L 143.72 129.79 L 148.55 122.47 L 149.77 120.98 L 154.84 114.77 L 160.26 109.54 L 161.51 108.43 L 167.16 104.30 L 168.48 103.41 L 170.03 102.61 L 175.70 99.68 L 177.22 99.05 L 178.94 98.49 L 184.53 96.80 L 186.10 96.38 L 187.83 96.02 L 189.74 95.71 L 191.83 95.44 L 194.11 95.23 L 196.61 95.06 L 199.88 94.87 L 201.97 94.78 L 204.33 94.73 L 207.12 94.72 L 211.67 94.77 L 216.20 94.85 L 218.97 94.94 L 221.28 95.06 L 224.91 95.32 L 227.59 95.52 L 229.99 95.76 L 232.14 96.04 L 234.08 96.37 L 235.81 96.73 L 237.35 97.15 L 243.32 98.92 L 244.97 99.48 L 246.39 100.08 L 252.37 103.08 L 253.76 103.85 L 259.49 107.95 L 260.77 108.89 L 266.25 114.07 L 267.35 115.20 L 272.57 121.46 L 277.44 128.75 L 278.34 130.18 L 282.93 138.64 L 287.18 148.33 L 291.12 159.56 L 291.73 161.37 L 295.52 175.00 L 300.35 197.45 L 304.38 219.93 L 305.81 233.68 L 305.99 235.93 L 306.04 247.38 L 304.83 257.35 L 304.19 260.86 L 301.77 269.54 L 300.67 272.91 L 297.08 280.38 L 295.89 282.33 L 291.15 288.66 L 289.66 290.19 L 283.82 295.39 L 282.08 296.62 L 275.18 300.70 L 273.27 301.69 L 271.01 302.57 L 263.38 305.39 L 261.11 306.08 L 258.53 306.70 L 255.64 307.25 L 248.01 308.68 L 245.34 309.07 L 242.41 309.42 L 239.19 309.70 L 235.66 309.93 L 231.79 310.10 L 227.50 310.22 L 222.66 310.28 L 216.91 310.27 L 207.53 310.16 L 207.53 310.16 L 198.17 309.99 L 192.46 309.83 L 187.68 309.63 L 183.49 309.39 L 179.73 309.10 L 176.35 308.77 L 173.30 308.39 L 170.56 307.96 L 168.11 307.49 L 159.68 305.82 L 157.09 305.19 L 154.85 304.50 L 146.93 301.56 L 144.76 300.70 L 142.97 299.75 L 135.85 295.58 L 134.30 294.43 L 128.28 289.15 L 127.09 287.77 L 122.23 281.41 L 118.58 274.02 L 117.81 272.27 L 115.37 263.73 L 114.15 253.96 Z" fill="black" transform="translate(0 14.55508390600588)"/></g></mask><mask id="r0-entity-occlusion-0" x="-420" y="-420" width="1260" height="1260" maskUnits="userSpaceOnUse"><rect x="-420" y="-420" width="1260" height="1260" fill="white"/><g clip-path="url(#r0-entity-occlusion-clip-0)"><path d="M 113.96 251.75 L 114.01 240.42 L 115.45 226.76 L 119.51 204.31 L 120.20 200.67 L 125.05 178.26 L 128.84 164.62 L 129.95 161.12 L 133.89 149.89 L 138.13 140.19 L 142.69 131.69 L 143.72 129.79 L 148.55 122.47 L 149.77 120.98 L 154.84 114.77 L 160.26 109.54 L 161.51 108.43 L 167.16 104.30 L 168.48 103.41 L 170.03 102.61 L 175.70 99.68 L 177.22 99.05 L 178.94 98.49 L 184.53 96.80 L 186.10 96.38 L 187.83 96.02 L 189.74 95.71 L 191.83 95.44 L 194.11 95.23 L 196.61 95.06 L 199.88 94.87 L 201.97 94.78 L 204.33 94.73 L 207.12 94.72 L 211.67 94.77 L 216.20 94.85 L 218.97 94.94 L 221.28 95.06 L 224.91 95.32 L 227.59 95.52 L 229.99 95.76 L 232.14 96.04 L 234.08 96.37 L 235.81 96.73 L 237.35 97.15 L 243.32 98.92 L 244.97 99.48 L 246.39 100.08 L 252.37 103.08 L 253.76 103.85 L 259.49 107.95 L 260.77 108.89 L 266.25 114.07 L 267.35 115.20 L 272.57 121.46 L 277.44 128.75 L 278.34 130.18 L 282.93 138.64 L 287.18 148.33 L 291.12 159.56 L 291.73 161.37 L 295.52 175.00 L 300.35 197.45 L 304.38 219.93 L 305.81 233.68 L 305.99 235.93 L 306.04 247.38 L 304.83 257.35 L 304.19 260.86 L 301.77 269.54 L 300.67 272.91 L 297.08 280.38 L 295.89 282.33 L 291.15 288.66 L 289.66 290.19 L 283.82 295.39 L 282.08 296.62 L 275.18 300.70 L 273.27 301.69 L 271.01 302.57 L 263.38 305.39 L 261.11 306.08 L 258.53 306.70 L 255.64 307.25 L 248.01 308.68 L 245.34 309.07 L 242.41 309.42 L 239.19 309.70 L 235.66 309.93 L 231.79 310.10 L 227.50 310.22 L 222.66 310.28 L 216.91 310.27 L 207.53 310.16 L 207.53 310.16 L 198.17 309.99 L 192.46 309.83 L 187.68 309.63 L 183.49 309.39 L 179.73 309.10 L 176.35 308.77 L 173.30 308.39 L 170.56 307.96 L 168.11 307.49 L 159.68 305.82 L 157.09 305.19 L 154.85 304.50 L 146.93 301.56 L 144.76 300.70 L 142.97 299.75 L 135.85 295.58 L 134.30 294.43 L 128.28 289.15 L 127.09 287.77 L 122.23 281.41 L 118.58 274.02 L 117.81 272.27 L 115.37 263.73 L 114.15 253.96 Z" fill="black" transform="translate(0 14.55508390600588)"/></g></mask><clipPath id="r0-entity-eye-0-highlight-clip"><path d="M 186.712 150.577 L 188.236 150.201 L 189.798 150.140 L 191.339 150.396 L 192.800 150.960 L 194.123 151.810 L 195.259 152.914 L 196.163 154.229 L 196.801 155.705 L 197.317 157.309 L 197.832 158.917 L 198.346 160.530 L 198.859 162.146 L 199.371 163.766 L 199.882 165.390 L 200.392 167.018 L 200.901 168.650 L 201.240 170.260 L 201.276 171.912 L 201.007 173.543 L 200.444 175.089 L 199.607 176.492 L 198.529 177.697 L 197.252 178.658 L 195.824 179.338 L 195.824 179.338 L 195.824 179.338 L 195.824 179.338 L 195.824 179.338 L 195.824 179.338 L 195.824 179.338 L 195.824 179.338 L 195.824 179.338 L 194.302 179.710 L 192.742 179.761 L 191.205 179.488 L 189.750 178.902 L 188.434 178.026 L 187.305 176.893 L 186.409 175.548 L 185.779 174.041 L 185.270 172.409 L 184.760 170.780 L 184.249 169.156 L 183.737 167.536 L 183.224 165.919 L 182.710 164.307 L 182.195 162.699 L 181.679 161.095 L 181.332 159.516 L 181.288 157.894 L 181.549 156.293 L 182.107 154.772 L 182.938 153.392 L 184.011 152.203 L 185.286 151.253 L 186.712 150.577 L 186.712 150.577 L 186.712 150.577 L 186.712 150.577 L 186.712 150.577 L 186.712 150.577 L 186.712 150.577 L 186.712 150.577 Z"/></clipPath><clipPath id="r0-entity-eye-1-highlight-clip"><path d="M 241.052 147.061 L 242.549 147.513 L 243.950 148.259 L 245.200 149.268 L 246.253 150.504 L 247.066 151.919 L 247.610 153.457 L 247.862 155.062 L 247.814 156.670 L 247.491 159.252 L 247.164 161.844 L 246.835 164.446 L 246.504 167.058 L 246.170 169.680 L 245.834 172.311 L 245.495 174.952 L 245.154 177.602 L 244.794 179.201 L 244.150 180.681 L 243.245 181.984 L 242.115 183.062 L 240.803 183.871 L 239.359 184.382 L 237.839 184.573 L 236.301 184.439 L 236.301 184.439 L 236.301 184.439 L 236.301 184.439 L 236.301 184.439 L 236.301 184.439 L 236.301 184.439 L 236.301 184.439 L 236.301 184.439 L 234.805 183.984 L 233.408 183.225 L 232.163 182.192 L 231.119 180.924 L 230.315 179.471 L 229.782 177.888 L 229.542 176.236 L 229.602 174.579 L 229.944 171.929 L 230.282 169.288 L 230.619 166.656 L 230.953 164.033 L 231.285 161.419 L 231.614 158.815 L 231.941 156.221 L 232.266 153.637 L 232.613 152.085 L 233.246 150.652 L 234.139 149.392 L 235.259 148.355 L 236.563 147.579 L 238.000 147.095 L 239.516 146.920 L 241.052 147.061 L 241.052 147.061 L 241.052 147.061 L 241.052 147.061 L 241.052 147.061 L 241.052 147.061 L 241.052 147.061 L 241.052 147.061 Z"/></clipPath></defs><g filter="url(#r0-entity-outline)"><g data-avatar-entity-part="ear-right" pointer-events="none" mask="url(#r0-entity-occlusion-1)"><g transform="translate(71.8645347270797 -49.39040223516069)"><defs><clipPath id="r0-entity-dog-1"><path d="M 186.43 174.41 L 186.44 170.58 L 186.60 167.18 L 186.92 164.20 L 187.39 161.66 L 188.06 159.55 L 188.09 159.46 L 188.97 157.80 L 189.02 157.72 L 189.11 157.62 L 190.21 156.43 L 190.30 156.34 L 190.40 156.25 L 190.49 156.18 L 190.57 156.12 L 190.66 156.05 L 190.76 155.99 L 190.86 155.93 L 190.97 155.87 L 191.09 155.80 L 191.21 155.74 L 191.34 155.68 L 191.47 155.62 L 191.62 155.55 L 191.77 155.49 L 191.92 155.43 L 192.09 155.37 L 192.26 155.30 L 192.46 155.23 L 192.73 155.14 L 192.99 155.05 L 193.18 154.99 L 193.33 154.95 L 193.47 154.91 L 193.60 154.88 L 193.70 154.85 L 193.80 154.84 L 193.88 154.83 L 193.94 154.82 L 195.55 154.84 L 195.65 154.85 L 195.72 154.86 L 197.45 155.41 L 199.39 156.47 L 201.42 157.96 L 203.61 159.88 L 205.99 162.24 L 208.65 165.13 L 211.39 168.35 L 214.31 172.01 L 217.40 176.15 L 220.70 180.84 L 224.31 186.32 L 229.29 194.32 L 233.97 202.22 L 237.03 207.92 L 239.41 213.00 L 241.20 217.66 L 242.45 222.00 L 243.21 226.26 L 243.39 230.00 L 243.04 233.46 L 242.15 236.63 L 242.07 236.84 L 240.65 239.68 L 240.47 239.93 L 238.54 242.42 L 238.23 242.74 L 237.85 243.08 L 235.53 245.08 L 235.27 245.28 L 234.99 245.48 L 234.68 245.67 L 234.33 245.87 L 231.84 247.26 L 231.61 247.38 L 231.36 247.50 L 231.09 247.63 L 230.80 247.76 L 230.49 247.90 L 230.15 248.03 L 229.80 248.18 L 229.42 248.32 L 229.02 248.47 L 228.60 248.63 L 228.13 248.80 L 227.62 248.98 L 226.90 249.22 L 226.90 249.22 L 226.18 249.46 L 225.68 249.62 L 225.25 249.75 L 224.86 249.87 L 224.52 249.97 L 224.21 250.06 L 223.93 250.13 L 223.68 250.18 L 220.46 250.85 L 220.10 250.92 L 219.79 250.96 L 219.54 250.98 L 216.25 251.05 L 215.99 251.04 L 212.86 250.49 L 212.63 250.44 L 209.66 249.30 L 206.88 247.57 L 206.71 247.45 L 204.12 245.15 L 201.73 242.27 L 199.55 238.83 L 197.56 234.81 L 195.76 230.19 L 194.11 224.87 L 192.55 218.63 L 190.71 209.68 L 189.06 200.66 L 188.07 194.19 L 187.36 188.50 L 186.88 183.36 L 186.57 178.67 Z"/></clipPath></defs><g clip-path="url(#r0-entity-dog-1)"><path d="M 186.43 174.41 L 186.44 170.58 L 186.60 167.18 L 186.92 164.20 L 187.39 161.66 L 188.06 159.55 L 188.09 159.46 L 188.97 157.80 L 189.02 157.72 L 189.11 157.62 L 190.21 156.43 L 190.30 156.34 L 190.40 156.25 L 190.49 156.18 L 190.57 156.12 L 190.66 156.05 L 190.76 155.99 L 190.86 155.93 L 190.97 155.87 L 191.09 155.80 L 191.21 155.74 L 191.34 155.68 L 191.47 155.62 L 191.62 155.55 L 191.77 155.49 L 191.92 155.43 L 192.09 155.37 L 192.26 155.30 L 192.46 155.23 L 192.73 155.14 L 192.99 155.05 L 193.18 154.99 L 193.33 154.95 L 193.47 154.91 L 193.60 154.88 L 193.70 154.85 L 193.80 154.84 L 193.88 154.83 L 193.94 154.82 L 195.55 154.84 L 195.65 154.85 L 195.72 154.86 L 197.45 155.41 L 199.39 156.47 L 201.42 157.96 L 203.61 159.88 L 205.99 162.24 L 208.65 165.13 L 211.39 168.35 L 214.31 172.01 L 217.40 176.15 L 220.70 180.84 L 224.31 186.32 L 229.29 194.32 L 233.97 202.22 L 237.03 207.92 L 239.41 213.00 L 241.20 217.66 L 242.45 222.00 L 243.21 226.26 L 243.39 230.00 L 243.04 233.46 L 242.15 236.63 L 242.07 236.84 L 240.65 239.68 L 240.47 239.93 L 238.54 242.42 L 238.23 242.74 L 237.85 243.08 L 235.53 245.08 L 235.27 245.28 L 234.99 245.48 L 234.68 245.67 L 234.33 245.87 L 231.84 247.26 L 231.61 247.38 L 231.36 247.50 L 231.09 247.63 L 230.80 247.76 L 230.49 247.90 L 230.15 248.03 L 229.80 248.18 L 229.42 248.32 L 229.02 248.47 L 228.60 248.63 L 228.13 248.80 L 227.62 248.98 L 226.90 249.22 L 226.90 249.22 L 226.18 249.46 L 225.68 249.62 L 225.25 249.75 L 224.86 249.87 L 224.52 249.97 L 224.21 250.06 L 223.93 250.13 L 223.68 250.18 L 220.46 250.85 L 220.10 250.92 L 219.79 250.96 L 219.54 250.98 L 216.25 251.05 L 215.99 251.04 L 212.86 250.49 L 212.63 250.44 L 209.66 249.30 L 206.88 247.57 L 206.71 247.45 L 204.12 245.15 L 201.73 242.27 L 199.55 238.83 L 197.56 234.81 L 195.76 230.19 L 194.11 224.87 L 192.55 218.63 L 190.71 209.68 L 189.06 200.66 L 188.07 194.19 L 187.36 188.50 L 186.88 183.36 L 186.57 178.67 Z" fill="#211f1d"/></g><g clip-path="url(#r0-entity-dog-1)"/></g></g><g data-avatar-entity-part="ear-left" pointer-events="none" mask="url(#r0-entity-occlusion-0)"><g transform="translate(-71.8645347270797 -51.52484617981341)"><defs><clipPath id="r0-entity-dog-0"><path d="M 176.46 229.28 L 176.69 225.56 L 177.46 221.56 L 178.86 216.97 L 180.72 212.36 L 183.16 207.36 L 186.29 201.75 L 191.07 193.99 L 196.03 186.30 L 199.87 180.70 L 203.22 176.10 L 206.35 172.05 L 209.29 168.47 L 212.05 165.32 L 214.63 162.59 L 217.13 160.17 L 219.31 158.30 L 221.30 156.86 L 221.42 156.80 L 223.22 155.83 L 223.34 155.79 L 224.98 155.30 L 225.09 155.29 L 225.21 155.28 L 226.68 155.31 L 226.76 155.31 L 226.86 155.33 L 226.97 155.35 L 227.09 155.37 L 227.22 155.41 L 227.37 155.45 L 227.54 155.50 L 227.73 155.57 L 228.00 155.66 L 228.28 155.76 L 228.48 155.84 L 228.65 155.91 L 228.81 155.98 L 228.96 156.04 L 229.11 156.11 L 229.24 156.18 L 229.36 156.24 L 229.48 156.31 L 229.59 156.38 L 229.69 156.44 L 229.78 156.51 L 229.86 156.57 L 229.93 156.64 L 230.00 156.70 L 230.95 157.69 L 231.05 157.81 L 231.14 157.93 L 231.23 158.09 L 232.01 159.65 L 232.11 159.86 L 232.15 160.00 L 232.76 162.08 L 233.18 164.59 L 233.44 167.54 L 233.55 170.91 L 233.55 171.11 L 233.52 174.70 L 233.51 174.93 L 233.33 178.92 L 233.32 179.17 L 232.97 183.83 L 232.43 188.96 L 231.68 194.62 L 230.64 201.05 L 228.99 209.70 L 228.93 210.01 L 227.11 218.58 L 227.03 218.90 L 225.44 225.10 L 223.76 230.36 L 221.94 234.94 L 219.93 238.91 L 217.74 242.30 L 215.34 245.12 L 215.12 245.31 L 212.75 247.36 L 212.54 247.54 L 209.77 249.20 L 206.82 250.27 L 206.56 250.35 L 203.46 250.82 L 203.18 250.85 L 202.83 250.85 L 199.67 250.69 L 199.36 250.66 L 199.00 250.60 L 198.58 250.52 L 198.12 250.41 L 195.30 249.70 L 195.00 249.62 L 194.66 249.53 L 194.29 249.42 L 193.89 249.29 L 193.44 249.14 L 192.92 248.96 L 192.19 248.70 L 192.19 248.70 L 191.45 248.44 L 190.93 248.24 L 190.46 248.06 L 190.04 247.90 L 189.64 247.73 L 189.27 247.58 L 188.93 247.42 L 188.61 247.27 L 188.32 247.13 L 188.05 246.99 L 185.78 245.80 L 185.36 245.57 L 184.98 245.35 L 184.65 245.13 L 184.36 244.92 L 184.11 244.72 L 181.82 242.78 L 181.54 242.51 L 181.32 242.24 L 179.47 239.85 L 179.21 239.42 L 177.86 236.65 L 177.65 236.18 L 176.81 233.05 L 176.76 232.72 Z"/></clipPath></defs><g clip-path="url(#r0-entity-dog-0)"><path d="M 176.46 229.28 L 176.69 225.56 L 177.46 221.56 L 178.86 216.97 L 180.72 212.36 L 183.16 207.36 L 186.29 201.75 L 191.07 193.99 L 196.03 186.30 L 199.87 180.70 L 203.22 176.10 L 206.35 172.05 L 209.29 168.47 L 212.05 165.32 L 214.63 162.59 L 217.13 160.17 L 219.31 158.30 L 221.30 156.86 L 221.42 156.80 L 223.22 155.83 L 223.34 155.79 L 224.98 155.30 L 225.09 155.29 L 225.21 155.28 L 226.68 155.31 L 226.76 155.31 L 226.86 155.33 L 226.97 155.35 L 227.09 155.37 L 227.22 155.41 L 227.37 155.45 L 227.54 155.50 L 227.73 155.57 L 228.00 155.66 L 228.28 155.76 L 228.48 155.84 L 228.65 155.91 L 228.81 155.98 L 228.96 156.04 L 229.11 156.11 L 229.24 156.18 L 229.36 156.24 L 229.48 156.31 L 229.59 156.38 L 229.69 156.44 L 229.78 156.51 L 229.86 156.57 L 229.93 156.64 L 230.00 156.70 L 230.95 157.69 L 231.05 157.81 L 231.14 157.93 L 231.23 158.09 L 232.01 159.65 L 232.11 159.86 L 232.15 160.00 L 232.76 162.08 L 233.18 164.59 L 233.44 167.54 L 233.55 170.91 L 233.55 171.11 L 233.52 174.70 L 233.51 174.93 L 233.33 178.92 L 233.32 179.17 L 232.97 183.83 L 232.43 188.96 L 231.68 194.62 L 230.64 201.05 L 228.99 209.70 L 228.93 210.01 L 227.11 218.58 L 227.03 218.90 L 225.44 225.10 L 223.76 230.36 L 221.94 234.94 L 219.93 238.91 L 217.74 242.30 L 215.34 245.12 L 215.12 245.31 L 212.75 247.36 L 212.54 247.54 L 209.77 249.20 L 206.82 250.27 L 206.56 250.35 L 203.46 250.82 L 203.18 250.85 L 202.83 250.85 L 199.67 250.69 L 199.36 250.66 L 199.00 250.60 L 198.58 250.52 L 198.12 250.41 L 195.30 249.70 L 195.00 249.62 L 194.66 249.53 L 194.29 249.42 L 193.89 249.29 L 193.44 249.14 L 192.92 248.96 L 192.19 248.70 L 192.19 248.70 L 191.45 248.44 L 190.93 248.24 L 190.46 248.06 L 190.04 247.90 L 189.64 247.73 L 189.27 247.58 L 188.93 247.42 L 188.61 247.27 L 188.32 247.13 L 188.05 246.99 L 185.78 245.80 L 185.36 245.57 L 184.98 245.35 L 184.65 245.13 L 184.36 244.92 L 184.11 244.72 L 181.82 242.78 L 181.54 242.51 L 181.32 242.24 L 179.47 239.85 L 179.21 239.42 L 177.86 236.65 L 177.65 236.18 L 176.81 233.05 L 176.76 232.72 Z" fill="#211f1d"/></g><g clip-path="url(#r0-entity-dog-0)"/></g></g><g data-avatar-entity-part="primary" pointer-events="none"><g transform="translate(0 14.55508390600588)"><defs><clipPath id="r0-entity-dog-2"><path d="M 113.96 251.75 L 114.01 240.42 L 115.45 226.76 L 119.51 204.31 L 120.20 200.67 L 125.05 178.26 L 128.84 164.62 L 129.95 161.12 L 133.89 149.89 L 138.13 140.19 L 142.69 131.69 L 143.72 129.79 L 148.55 122.47 L 149.77 120.98 L 154.84 114.77 L 160.26 109.54 L 161.51 108.43 L 167.16 104.30 L 168.48 103.41 L 170.03 102.61 L 175.70 99.68 L 177.22 99.05 L 178.94 98.49 L 184.53 96.80 L 186.10 96.38 L 187.83 96.02 L 189.74 95.71 L 191.83 95.44 L 194.11 95.23 L 196.61 95.06 L 199.88 94.87 L 201.97 94.78 L 204.33 94.73 L 207.12 94.72 L 211.67 94.77 L 216.20 94.85 L 218.97 94.94 L 221.28 95.06 L 224.91 95.32 L 227.59 95.52 L 229.99 95.76 L 232.14 96.04 L 234.08 96.37 L 235.81 96.73 L 237.35 97.15 L 243.32 98.92 L 244.97 99.48 L 246.39 100.08 L 252.37 103.08 L 253.76 103.85 L 259.49 107.95 L 260.77 108.89 L 266.25 114.07 L 267.35 115.20 L 272.57 121.46 L 277.44 128.75 L 278.34 130.18 L 282.93 138.64 L 287.18 148.33 L 291.12 159.56 L 291.73 161.37 L 295.52 175.00 L 300.35 197.45 L 304.38 219.93 L 305.81 233.68 L 305.99 235.93 L 306.04 247.38 L 304.83 257.35 L 304.19 260.86 L 301.77 269.54 L 300.67 272.91 L 297.08 280.38 L 295.89 282.33 L 291.15 288.66 L 289.66 290.19 L 283.82 295.39 L 282.08 296.62 L 275.18 300.70 L 273.27 301.69 L 271.01 302.57 L 263.38 305.39 L 261.11 306.08 L 258.53 306.70 L 255.64 307.25 L 248.01 308.68 L 245.34 309.07 L 242.41 309.42 L 239.19 309.70 L 235.66 309.93 L 231.79 310.10 L 227.50 310.22 L 222.66 310.28 L 216.91 310.27 L 207.53 310.16 L 207.53 310.16 L 198.17 309.99 L 192.46 309.83 L 187.68 309.63 L 183.49 309.39 L 179.73 309.10 L 176.35 308.77 L 173.30 308.39 L 170.56 307.96 L 168.11 307.49 L 159.68 305.82 L 157.09 305.19 L 154.85 304.50 L 146.93 301.56 L 144.76 300.70 L 142.97 299.75 L 135.85 295.58 L 134.30 294.43 L 128.28 289.15 L 127.09 287.77 L 122.23 281.41 L 118.58 274.02 L 117.81 272.27 L 115.37 263.73 L 114.15 253.96 Z"/></clipPath></defs><g clip-path="url(#r0-entity-dog-2)"><path d="M 113.96 251.75 L 114.01 240.42 L 115.45 226.76 L 119.51 204.31 L 120.20 200.67 L 125.05 178.26 L 128.84 164.62 L 129.95 161.12 L 133.89 149.89 L 138.13 140.19 L 142.69 131.69 L 143.72 129.79 L 148.55 122.47 L 149.77 120.98 L 154.84 114.77 L 160.26 109.54 L 161.51 108.43 L 167.16 104.30 L 168.48 103.41 L 170.03 102.61 L 175.70 99.68 L 177.22 99.05 L 178.94 98.49 L 184.53 96.80 L 186.10 96.38 L 187.83 96.02 L 189.74 95.71 L 191.83 95.44 L 194.11 95.23 L 196.61 95.06 L 199.88 94.87 L 201.97 94.78 L 204.33 94.73 L 207.12 94.72 L 211.67 94.77 L 216.20 94.85 L 218.97 94.94 L 221.28 95.06 L 224.91 95.32 L 227.59 95.52 L 229.99 95.76 L 232.14 96.04 L 234.08 96.37 L 235.81 96.73 L 237.35 97.15 L 243.32 98.92 L 244.97 99.48 L 246.39 100.08 L 252.37 103.08 L 253.76 103.85 L 259.49 107.95 L 260.77 108.89 L 266.25 114.07 L 267.35 115.20 L 272.57 121.46 L 277.44 128.75 L 278.34 130.18 L 282.93 138.64 L 287.18 148.33 L 291.12 159.56 L 291.73 161.37 L 295.52 175.00 L 300.35 197.45 L 304.38 219.93 L 305.81 233.68 L 305.99 235.93 L 306.04 247.38 L 304.83 257.35 L 304.19 260.86 L 301.77 269.54 L 300.67 272.91 L 297.08 280.38 L 295.89 282.33 L 291.15 288.66 L 289.66 290.19 L 283.82 295.39 L 282.08 296.62 L 275.18 300.70 L 273.27 301.69 L 271.01 302.57 L 263.38 305.39 L 261.11 306.08 L 258.53 306.70 L 255.64 307.25 L 248.01 308.68 L 245.34 309.07 L 242.41 309.42 L 239.19 309.70 L 235.66 309.93 L 231.79 310.10 L 227.50 310.22 L 222.66 310.28 L 216.91 310.27 L 207.53 310.16 L 207.53 310.16 L 198.17 309.99 L 192.46 309.83 L 187.68 309.63 L 183.49 309.39 L 179.73 309.10 L 176.35 308.77 L 173.30 308.39 L 170.56 307.96 L 168.11 307.49 L 159.68 305.82 L 157.09 305.19 L 154.85 304.50 L 146.93 301.56 L 144.76 300.70 L 142.97 299.75 L 135.85 295.58 L 134.30 294.43 L 128.28 289.15 L 127.09 287.77 L 122.23 281.41 L 118.58 274.02 L 117.81 272.27 L 115.37 263.73 L 114.15 253.96 Z" fill="#f4f3ef"/></g><g clip-path="url(#r0-entity-dog-2)"/></g></g></g><g pointer-events="none"><g transform="translate(0 14.55508390600588)"><polygon points="259.30,306.53 251.47,307.85 236.38,309.33 241.35,308.50" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="284.45,294.82 279.63,297.87 265.08,304.73 268.89,302.35" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="163.52,306.54 156.42,305.00 175.94,307.52 180.46,308.50" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="265.08,304.73 259.30,306.53 241.35,308.50 245.03,307.36" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="136.10,295.73 132.86,292.57 149.01,300.57 151.58,303.05" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="156.42,305.00 151.58,303.05 172.86,306.29 175.94,307.52" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="298.13,277.17 295.03,283.29 284.45,294.82 287.14,289.55" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="268.89,302.35 265.08,304.73 245.03,307.36 247.45,305.86" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="225.36,95.43 229.16,96.06 240.86,98.29 234.74,97.29" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="137.10,143.46 139.97,137.21 131.96,157.93 128.84,164.62" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="193.65,95.53 197.74,95.02 190.20,96.63 183.61,97.44" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="220.33,95.01 225.36,95.43 234.74,97.29 226.63,96.62" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="115.75,264.79 115.81,258.06 121.87,274.56 121.85,280.72" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="211.67,94.77 220.33,95.01 226.63,96.62 212.65,96.23" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="197.74,95.02 202.94,94.75 198.59,96.20 190.20,96.63" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="287.14,289.55 284.45,294.82 268.89,302.35 271.00,298.23" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="202.94,94.75 211.67,94.77 212.65,96.23 198.59,96.20" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="151.58,303.05 149.01,300.57 171.23,304.73 172.86,306.29" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="159.79,110.06 163.24,107.30 153.36,118.33 149.21,121.59" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="221.85,310.15 208.12,310.07 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="230.00,309.88 221.85,310.15 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="174.55,100.27 178.43,98.65 168.35,105.21 163.24,107.30" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="271.17,120.08 273.64,123.44 283.22,139.34 280.34,135.60" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="208.12,310.07 194.46,309.74 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="236.38,309.33 230.00,309.88 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="194.46,309.74 186.50,309.24 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="241.35,308.50 236.38,309.33 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="240.86,98.29 245.40,99.64 256.60,106.52 250.62,104.77" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="186.50,309.24 180.46,308.50 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="121.85,280.72 121.87,274.56 132.86,287.26 132.86,292.57" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="128.84,164.62 131.96,157.93 123.54,193.72 120.20,200.67" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="180.46,308.50 175.94,307.52 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="271.00,298.23 268.89,302.35 247.45,305.86 248.79,303.26" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="245.03,307.36 241.35,308.50 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="256.60,106.52 260.61,108.75 271.17,120.08 266.31,117.45" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="175.94,307.52 172.86,306.29 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="178.43,98.65 183.61,97.44 175.17,103.65 168.35,105.21" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="132.86,292.57 132.86,287.26 149.00,296.42 149.01,300.57" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="247.45,305.86 245.03,307.36 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="280.34,135.60 283.22,139.34 291.44,160.30 288.20,156.30" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="234.74,97.29 240.86,98.29 250.62,104.77 242.54,103.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="149.21,121.59 153.36,118.33 144.71,133.58 139.97,137.21" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="172.86,306.29 171.23,304.73 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="305.43,232.57 305.32,239.59 304.19,260.86 304.25,254.12" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="149.01,300.57 149.00,296.42 171.21,302.11 171.23,304.73" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="163.24,107.30 168.35,105.21 159.52,115.87 153.36,118.33" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="248.79,303.26 247.45,305.86 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="183.61,97.44 190.20,96.63 183.85,102.60 175.17,103.65" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="226.63,96.62 234.74,97.29 242.54,103.47 231.85,102.60" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="266.31,117.45 271.17,120.08 280.34,135.60 274.72,132.66" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="250.62,104.77 256.60,106.52 266.31,117.45 259.05,115.39" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="171.23,304.73 171.21,302.11 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="304.25,254.12 304.19,260.86 298.13,277.17 298.15,271.01" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="120.20,200.67 123.54,193.72 118.12,229.79 114.68,236.76" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="212.65,96.23 226.63,96.62 231.85,102.60 213.43,102.09" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="190.20,96.63 198.59,96.20 194.90,102.05 183.85,102.60" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="198.59,96.20 212.65,96.23 213.43,102.09 194.90,102.05" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="288.20,156.30 291.44,160.30 300.01,196.34 296.36,192.17" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="139.97,137.21 144.71,133.58 137.21,154.05 131.96,157.93" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="298.15,271.01 298.13,277.17 287.14,289.55 287.14,284.24" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="248.77,300.64 248.79,303.26 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="287.14,284.24 287.14,289.55 271.00,298.23 270.99,294.08" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="270.99,294.08 271.00,298.23 248.79,303.26 248.77,300.64" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="168.35,105.21 175.17,103.65 167.77,114.03 159.52,115.87" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="171.21,302.11 172.55,299.50 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="242.54,103.47 250.62,104.77 259.05,115.39 249.27,113.85" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="153.36,118.33 159.52,115.87 151.78,130.84 144.71,133.58" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="247.14,299.07 248.77,300.64 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="114.68,236.76 118.12,229.79 119.16,251.38 115.81,258.06" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="274.72,132.66 280.34,135.60 288.20,156.30 281.91,153.15" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="172.55,299.50 174.97,298.00 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="259.05,115.39 266.31,117.45 274.72,132.66 266.35,130.36" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="244.06,297.84 247.14,299.07 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="149.00,296.42 151.11,292.30 172.55,299.50 171.21,302.11" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="175.17,103.65 183.85,102.60 178.26,112.80 167.77,114.03" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="115.81,258.06 119.16,251.38 124.97,268.44 121.87,274.56" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="231.85,102.60 242.54,103.47 249.27,113.85 236.33,112.82" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="296.36,192.17 300.01,196.34 305.43,232.57 301.50,228.38" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="174.97,298.00 178.65,296.87 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="131.96,157.93 137.21,154.05 129.27,189.68 123.54,193.72" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="239.54,296.86 244.06,297.84 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="132.86,287.26 135.55,281.99 151.11,292.30 149.00,296.42" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="121.87,274.56 124.97,268.44 135.55,281.99 132.86,287.26" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="233.50,296.13 239.54,296.86 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="178.65,296.87 183.62,296.03 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="213.43,102.09 231.85,102.60 236.33,112.82 214.05,112.22" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="183.85,102.60 194.90,102.05 191.62,112.16 178.26,112.80" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="225.54,295.62 233.50,296.13 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="268.42,291.60 270.99,294.08 248.77,300.64 247.14,299.07" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="194.90,102.05 213.43,102.09 214.05,112.22 191.62,112.16" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="183.62,296.03 190.00,295.48 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="144.71,133.58 151.78,130.84 145.04,151.12 137.21,154.05" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="159.52,115.87 167.77,114.03 161.25,128.80 151.78,130.84" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="211.88,295.29 225.54,295.62 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="190.00,295.48 198.15,295.21 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="198.15,295.21 211.88,295.29 210.00,304.47 210.00,304.47" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="281.91,153.15 288.20,156.30 296.36,192.17 289.34,188.89" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="249.27,113.85 259.05,115.39 266.35,130.36 255.07,128.64" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="301.50,228.38 305.43,232.57 304.25,254.12 300.33,250.10" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="266.35,130.36 274.72,132.66 281.91,153.15 272.57,150.68" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="283.90,281.07 287.14,284.24 270.99,294.08 268.42,291.60" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="300.33,250.10 304.25,254.12 298.15,271.01 294.47,267.33" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="151.11,292.30 154.92,289.92 174.97,298.00 172.55,299.50" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="294.47,267.33 298.15,271.01 287.14,284.24 283.90,281.07" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="123.54,193.72 129.27,189.68 124.15,225.74 118.12,229.79" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="167.77,114.03 178.26,112.80 173.32,127.43 161.25,128.80" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="263.58,289.65 268.42,291.60 247.14,299.07 244.06,297.84" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="236.33,112.82 249.27,113.85 255.07,128.64 240.17,127.49" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="151.78,130.84 161.25,128.80 155.57,148.94 145.04,151.12" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="137.21,154.05 145.04,151.12 137.89,186.64 129.27,189.68" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="214.05,112.22 236.33,112.82 240.17,127.49 214.51,126.80" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="178.26,112.80 191.62,112.16 188.69,126.73 173.32,127.43" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="289.34,188.89 296.36,192.17 301.50,228.38 294.01,225.08" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="255.07,128.64 266.35,130.36 272.57,150.68 260.00,148.84" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="135.55,281.99 140.37,278.94 154.92,289.92 151.11,292.30" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="191.62,112.16 214.05,112.22 214.51,126.80 188.69,126.73" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="154.92,289.92 160.70,288.12 178.65,296.87 174.97,298.00" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="256.48,288.11 263.58,289.65 244.06,297.84 239.54,296.86" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="272.57,150.68 281.91,153.15 289.34,188.89 278.97,186.31" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="118.12,229.79 124.15,225.74 125.08,247.50 119.16,251.38" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="277.79,278.58 283.90,281.07 268.42,291.60 263.58,289.65" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="124.97,268.44 130.49,264.90 140.37,278.94 135.55,281.99" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="119.16,251.38 125.08,247.50 130.49,264.90 124.97,268.44" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="246.98,286.94 256.48,288.11 239.54,296.86 233.50,296.13" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="294.01,225.08 301.50,228.38 300.33,250.10 292.91,246.94" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="161.25,128.80 173.32,127.43 168.98,147.48 155.57,148.94" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="160.70,288.12 168.53,286.80 183.62,296.03 178.65,296.87" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="240.17,127.49 255.07,128.64 260.00,148.84 243.39,147.59" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="287.50,264.43 294.47,267.33 283.90,281.07 277.79,278.58" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="292.91,246.94 300.33,250.10 294.47,267.33 287.50,264.43" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="234.46,286.14 246.98,286.94 233.50,296.13 225.54,295.62" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="145.04,151.12 155.57,148.94 149.50,184.38 137.89,186.64" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="129.27,189.68 137.89,186.64 133.26,222.69 124.15,225.74" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="168.53,286.80 178.56,285.93 190.00,295.48 183.62,296.03" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="214.51,126.80 240.17,127.49 243.39,147.59 214.82,146.84" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="140.37,278.94 147.69,276.65 160.70,288.12 154.92,289.92" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="173.32,127.43 188.69,126.73 186.09,146.74 168.98,147.48" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="260.00,148.84 272.57,150.68 278.97,186.31 265.03,184.37" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="268.82,276.61 277.79,278.58 263.58,289.65 256.48,288.11" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="212.97,285.62 234.46,286.14 225.54,295.62 211.88,295.29" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="188.69,126.73 214.51,126.80 214.82,146.84 186.09,146.74" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="278.97,186.31 289.34,188.89 294.01,225.08 282.96,222.48" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="178.56,285.93 191.38,285.50 198.15,295.21 190.00,295.48" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="191.38,285.50 212.97,285.62 211.88,295.29 198.15,295.21" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="130.49,264.90 138.87,262.23 147.69,276.65 140.37,278.94" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="124.15,225.74 133.26,222.69 134.06,244.58 125.08,247.50" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="277.26,262.15 287.50,264.43 277.79,278.58 268.82,276.61" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="256.80,275.12 268.82,276.61 256.48,288.11 246.98,286.94" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="243.39,147.59 260.00,148.84 265.03,184.37 246.64,183.07" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="155.57,148.94 168.98,147.48 164.31,182.88 149.50,184.38" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="147.69,276.65 157.60,274.95 168.53,286.80 160.70,288.12" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="125.08,247.50 134.06,244.58 138.87,262.23 130.49,264.90" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="282.96,222.48 294.01,225.08 292.91,246.94 281.97,244.44" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="281.97,244.44 292.91,246.94 287.50,264.43 277.26,262.15" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="137.89,186.64 149.50,184.38 145.54,220.44 133.26,222.69" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="265.03,184.37 278.97,186.31 282.96,222.48 268.14,220.53" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="214.82,146.84 243.39,147.59 246.64,183.07 215.02,182.25" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="240.97,274.10 256.80,275.12 246.98,286.94 234.46,286.14" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="168.98,147.48 186.09,146.74 183.22,182.12 164.31,182.88" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="157.60,274.95 170.28,273.83 178.56,285.93 168.53,286.80" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="186.09,146.74 214.82,146.84 215.02,182.25 183.22,182.12" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="263.53,260.42 277.26,262.15 268.82,276.61 256.80,275.12" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="138.87,262.23 150.20,260.26 157.60,274.95 147.69,276.65" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="213.80,273.43 240.97,274.10 234.46,286.14 212.97,285.62" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="170.28,273.83 186.50,273.29 191.38,285.50 178.56,285.93" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="186.50,273.29 213.80,273.43 212.97,285.62 191.38,285.50" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="133.26,222.69 145.54,220.44 146.17,242.43 134.06,244.58" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="268.14,220.53 282.96,222.48 281.97,244.44 267.31,242.56" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="267.31,242.56 281.97,244.44 277.26,262.15 263.53,260.42" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="134.06,244.58 146.17,242.43 150.20,260.26 138.87,262.23" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="246.64,183.07 265.03,184.37 268.14,220.53 248.60,219.20" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="149.50,184.38 164.31,182.88 161.24,218.94 145.54,220.44" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="245.46,259.24 263.53,260.42 256.80,275.12 240.97,274.10" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="150.20,260.26 164.69,258.96 170.28,273.83 157.60,274.95" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="215.02,182.25 246.64,183.07 248.60,219.20 215.04,218.35" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="164.31,182.88 183.22,182.12 181.30,218.20 161.24,218.94" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="214.42,258.48 245.46,259.24 240.97,274.10 213.80,273.43" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="248.60,219.20 268.14,220.53 267.31,242.56 248.00,241.28" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="248.00,241.28 267.31,242.56 263.53,260.42 245.46,259.24" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="183.22,182.12 215.02,182.25 215.04,218.35 181.30,218.20" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="164.69,258.96 183.23,258.32 186.50,273.29 170.28,273.83" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="145.54,220.44 161.24,218.94 161.68,241.00 146.17,242.43" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="146.17,242.43 161.68,241.00 164.69,258.96 150.20,260.26" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="183.23,258.32 214.42,258.48 213.80,273.43 186.50,273.29" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="214.83,240.46 248.00,241.28 245.46,259.24 214.42,258.48" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="215.04,218.35 248.60,219.20 248.00,241.28 214.83,240.46" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="161.68,241.00 181.49,240.29 183.23,258.32 164.69,258.96" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="161.24,218.94 181.30,218.20 181.49,240.29 161.68,241.00" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="181.49,240.29 214.83,240.46 214.42,258.48 183.23,258.32" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/><polygon points="181.30,218.20 215.04,218.35 214.83,240.46 181.49,240.29" fill="none" stroke="#fff" stroke-opacity=".09" stroke-width=".55"/></g></g><g pointer-events="none"><g transform="translate(0 14.55508390600588)"><path d="M 113.96 251.75 L 114.01 240.42 L 115.45 226.76 L 119.51 204.31 L 120.20 200.67 L 125.05 178.26 L 128.84 164.62 L 129.95 161.12 L 133.89 149.89 L 138.13 140.19 L 142.69 131.69 L 143.72 129.79 L 148.55 122.47 L 149.77 120.98 L 154.84 114.77 L 160.26 109.54 L 161.51 108.43 L 167.16 104.30 L 168.48 103.41 L 170.03 102.61 L 175.70 99.68 L 177.22 99.05 L 178.94 98.49 L 184.53 96.80 L 186.10 96.38 L 187.83 96.02 L 189.74 95.71 L 191.83 95.44 L 194.11 95.23 L 196.61 95.06 L 199.88 94.87 L 201.97 94.78 L 204.33 94.73 L 207.12 94.72 L 211.67 94.77 L 216.20 94.85 L 218.97 94.94 L 221.28 95.06 L 224.91 95.32 L 227.59 95.52 L 229.99 95.76 L 232.14 96.04 L 234.08 96.37 L 235.81 96.73 L 237.35 97.15 L 243.32 98.92 L 244.97 99.48 L 246.39 100.08 L 252.37 103.08 L 253.76 103.85 L 259.49 107.95 L 260.77 108.89 L 266.25 114.07 L 267.35 115.20 L 272.57 121.46 L 277.44 128.75 L 278.34 130.18 L 282.93 138.64 L 287.18 148.33 L 291.12 159.56 L 291.73 161.37 L 295.52 175.00 L 300.35 197.45 L 304.38 219.93 L 305.81 233.68 L 305.99 235.93 L 306.04 247.38 L 304.83 257.35 L 304.19 260.86 L 301.77 269.54 L 300.67 272.91 L 297.08 280.38 L 295.89 282.33 L 291.15 288.66 L 289.66 290.19 L 283.82 295.39 L 282.08 296.62 L 275.18 300.70 L 273.27 301.69 L 271.01 302.57 L 263.38 305.39 L 261.11 306.08 L 258.53 306.70 L 255.64 307.25 L 248.01 308.68 L 245.34 309.07 L 242.41 309.42 L 239.19 309.70 L 235.66 309.93 L 231.79 310.10 L 227.50 310.22 L 222.66 310.28 L 216.91 310.27 L 207.53 310.16 L 207.53 310.16 L 198.17 309.99 L 192.46 309.83 L 187.68 309.63 L 183.49 309.39 L 179.73 309.10 L 176.35 308.77 L 173.30 308.39 L 170.56 307.96 L 168.11 307.49 L 159.68 305.82 L 157.09 305.19 L 154.85 304.50 L 146.93 301.56 L 144.76 300.70 L 142.97 299.75 L 135.85 295.58 L 134.30 294.43 L 128.28 289.15 L 127.09 287.77 L 122.23 281.41 L 118.58 274.02 L 117.81 272.27 L 115.37 263.73 L 114.15 253.96 Z" fill="none" stroke="var(--primary-color)" stroke-dasharray="5 4" stroke-width="2"/></g></g><g transform="translate(0 14.55508390600588)"><path d="M 186.712 150.577 L 188.236 150.201 L 189.798 150.140 L 191.339 150.396 L 192.800 150.960 L 194.123 151.810 L 195.259 152.914 L 196.163 154.229 L 196.801 155.705 L 197.317 157.309 L 197.832 158.917 L 198.346 160.530 L 198.859 162.146 L 199.371 163.766 L 199.882 165.390 L 200.392 167.018 L 200.901 168.650 L 201.240 170.260 L 201.276 171.912 L 201.007 173.543 L 200.444 175.089 L 199.607 176.492 L 198.529 177.697 L 197.252 178.658 L 195.824 179.338 L 195.824 179.338 L 195.824 179.338 L 195.824 179.338 L 195.824 179.338 L 195.824 179.338 L 195.824 179.338 L 195.824 179.338 L 195.824 179.338 L 194.302 179.710 L 192.742 179.761 L 191.205 179.488 L 189.750 178.902 L 188.434 178.026 L 187.305 176.893 L 186.409 175.548 L 185.779 174.041 L 185.270 172.409 L 184.760 170.780 L 184.249 169.156 L 183.737 167.536 L 183.224 165.919 L 182.710 164.307 L 182.195 162.699 L 181.679 161.095 L 181.332 159.516 L 181.288 157.894 L 181.549 156.293 L 182.107 154.772 L 182.938 153.392 L 184.011 152.203 L 185.286 151.253 L 186.712 150.577 L 186.712 150.577 L 186.712 150.577 L 186.712 150.577 L 186.712 150.577 L 186.712 150.577 L 186.712 150.577 L 186.712 150.577 Z" fill="#211f1d"/><path d="M 241.052 147.061 L 242.549 147.513 L 243.950 148.259 L 245.200 149.268 L 246.253 150.504 L 247.066 151.919 L 247.610 153.457 L 247.862 155.062 L 247.814 156.670 L 247.491 159.252 L 247.164 161.844 L 246.835 164.446 L 246.504 167.058 L 246.170 169.680 L 245.834 172.311 L 245.495 174.952 L 245.154 177.602 L 244.794 179.201 L 244.150 180.681 L 243.245 181.984 L 242.115 183.062 L 240.803 183.871 L 239.359 184.382 L 237.839 184.573 L 236.301 184.439 L 236.301 184.439 L 236.301 184.439 L 236.301 184.439 L 236.301 184.439 L 236.301 184.439 L 236.301 184.439 L 236.301 184.439 L 236.301 184.439 L 234.805 183.984 L 233.408 183.225 L 232.163 182.192 L 231.119 180.924 L 230.315 179.471 L 229.782 177.888 L 229.542 176.236 L 229.602 174.579 L 229.944 171.929 L 230.282 169.288 L 230.619 166.656 L 230.953 164.033 L 231.285 161.419 L 231.614 158.815 L 231.941 156.221 L 232.266 153.637 L 232.613 152.085 L 233.246 150.652 L 234.139 149.392 L 235.259 148.355 L 236.563 147.579 L 238.000 147.095 L 239.516 146.920 L 241.052 147.061 L 241.052 147.061 L 241.052 147.061 L 241.052 147.061 L 241.052 147.061 L 241.052 147.061 L 241.052 147.061 L 241.052 147.061 Z" fill="#211f1d"/><path d="M 210.176 182.040 L 210.728 181.418 L 211.679 181.059 L 212.921 180.939 L 214.346 181.033 L 215.846 181.316 L 217.315 181.761 L 218.643 182.342 L 219.725 183.032 L 220.452 183.803 L 220.718 184.626 L 220.491 185.270 L 220.091 185.893 L 219.555 186.483 L 218.921 187.030 L 218.226 187.522 L 217.508 187.949 L 216.803 188.299 L 216.150 188.560 L 215.586 188.721 L 215.147 188.771 L 215.006 188.902 L 214.847 188.993 L 214.676 189.043 L 214.499 189.055 L 214.322 189.030 L 214.153 188.970 L 213.997 188.876 L 213.860 188.750 L 213.750 188.594 L 213.671 188.408 L 213.292 188.158 L 212.841 187.748 L 212.347 187.205 L 211.839 186.557 L 211.348 185.834 L 210.903 185.062 L 210.533 184.269 L 210.269 183.483 L 210.140 182.731 L 210.176 182.040 Z" fill="#211f1d"/><path d="M 204.077 217.557 L 204.949 218.090 L 205.809 218.573 L 206.657 219.004 L 207.492 219.386 L 208.316 219.718 L 209.126 220.002 L 209.925 220.237 L 210.712 220.425 L 211.488 220.567 L 212.253 220.663 L 213.009 220.713 L 213.755 220.719 L 214.495 220.680 L 215.227 220.598 L 215.955 220.471 L 216.678 220.299 L 217.399 220.082 L 218.117 219.820 L 218.835 219.511 L 219.552 219.155 L 220.269 218.751 L 220.986 218.298 L 221.703 217.795 L 222.421 217.242 L 223.139 216.637 L 223.857 215.981 L 224.574 215.273 L 225.291 214.512 L 226.108 213.918 L 227.068 213.726 L 228.025 213.966 L 228.835 214.600 L 229.372 215.532 L 229.557 216.621 L 229.359 217.702 L 228.811 218.608 L 227.965 219.506 L 227.108 220.353 L 226.238 221.149 L 225.355 221.893 L 224.460 222.583 L 223.552 223.221 L 222.631 223.803 L 221.697 224.331 L 220.750 224.801 L 219.792 225.214 L 218.822 225.569 L 217.842 225.864 L 216.853 226.099 L 215.855 226.274 L 214.850 226.387 L 213.839 226.440 L 212.824 226.432 L 211.805 226.364 L 210.783 226.236 L 209.759 226.049 L 208.734 225.804 L 207.708 225.501 L 206.682 225.142 L 205.656 224.727 L 204.629 224.258 L 203.603 223.734 L 202.575 223.157 L 201.547 222.528 L 200.807 221.806 L 200.372 220.815 L 200.309 219.707 L 200.626 218.650 L 201.276 217.805 L 202.160 217.301 L 203.144 217.214 L 204.077 217.557 Z" fill="#211f1d"/></g><g data-avatar-entity-part-hit="primary"><g transform="translate(0 14.55508390600588)"><path d="M 113.96 251.75 L 114.01 240.42 L 115.45 226.76 L 119.51 204.31 L 120.20 200.67 L 125.05 178.26 L 128.84 164.62 L 129.95 161.12 L 133.89 149.89 L 138.13 140.19 L 142.69 131.69 L 143.72 129.79 L 148.55 122.47 L 149.77 120.98 L 154.84 114.77 L 160.26 109.54 L 161.51 108.43 L 167.16 104.30 L 168.48 103.41 L 170.03 102.61 L 175.70 99.68 L 177.22 99.05 L 178.94 98.49 L 184.53 96.80 L 186.10 96.38 L 187.83 96.02 L 189.74 95.71 L 191.83 95.44 L 194.11 95.23 L 196.61 95.06 L 199.88 94.87 L 201.97 94.78 L 204.33 94.73 L 207.12 94.72 L 211.67 94.77 L 216.20 94.85 L 218.97 94.94 L 221.28 95.06 L 224.91 95.32 L 227.59 95.52 L 229.99 95.76 L 232.14 96.04 L 234.08 96.37 L 235.81 96.73 L 237.35 97.15 L 243.32 98.92 L 244.97 99.48 L 246.39 100.08 L 252.37 103.08 L 253.76 103.85 L 259.49 107.95 L 260.77 108.89 L 266.25 114.07 L 267.35 115.20 L 272.57 121.46 L 277.44 128.75 L 278.34 130.18 L 282.93 138.64 L 287.18 148.33 L 291.12 159.56 L 291.73 161.37 L 295.52 175.00 L 300.35 197.45 L 304.38 219.93 L 305.81 233.68 L 305.99 235.93 L 306.04 247.38 L 304.83 257.35 L 304.19 260.86 L 301.77 269.54 L 300.67 272.91 L 297.08 280.38 L 295.89 282.33 L 291.15 288.66 L 289.66 290.19 L 283.82 295.39 L 282.08 296.62 L 275.18 300.70 L 273.27 301.69 L 271.01 302.57 L 263.38 305.39 L 261.11 306.08 L 258.53 306.70 L 255.64 307.25 L 248.01 308.68 L 245.34 309.07 L 242.41 309.42 L 239.19 309.70 L 235.66 309.93 L 231.79 310.10 L 227.50 310.22 L 222.66 310.28 L 216.91 310.27 L 207.53 310.16 L 207.53 310.16 L 198.17 309.99 L 192.46 309.83 L 187.68 309.63 L 183.49 309.39 L 179.73 309.10 L 176.35 308.77 L 173.30 308.39 L 170.56 307.96 L 168.11 307.49 L 159.68 305.82 L 157.09 305.19 L 154.85 304.50 L 146.93 301.56 L 144.76 300.70 L 142.97 299.75 L 135.85 295.58 L 134.30 294.43 L 128.28 289.15 L 127.09 287.77 L 122.23 281.41 L 118.58 274.02 L 117.81 272.27 L 115.37 263.73 L 114.15 253.96 Z" fill="transparent" pointer-events="all"/></g></g><g data-avatar-entity-part-hit="ear-right" mask="url(#r0-entity-occlusion-1)"><g transform="translate(71.8645347270797 -49.39040223516069)"><path d="M 186.43 174.41 L 186.44 170.58 L 186.60 167.18 L 186.92 164.20 L 187.39 161.66 L 188.06 159.55 L 188.09 159.46 L 188.97 157.80 L 189.02 157.72 L 189.11 157.62 L 190.21 156.43 L 190.30 156.34 L 190.40 156.25 L 190.49 156.18 L 190.57 156.12 L 190.66 156.05 L 190.76 155.99 L 190.86 155.93 L 190.97 155.87 L 191.09 155.80 L 191.21 155.74 L 191.34 155.68 L 191.47 155.62 L 191.62 155.55 L 191.77 155.49 L 191.92 155.43 L 192.09 155.37 L 192.26 155.30 L 192.46 155.23 L 192.73 155.14 L 192.99 155.05 L 193.18 154.99 L 193.33 154.95 L 193.47 154.91 L 193.60 154.88 L 193.70 154.85 L 193.80 154.84 L 193.88 154.83 L 193.94 154.82 L 195.55 154.84 L 195.65 154.85 L 195.72 154.86 L 197.45 155.41 L 199.39 156.47 L 201.42 157.96 L 203.61 159.88 L 205.99 162.24 L 208.65 165.13 L 211.39 168.35 L 214.31 172.01 L 217.40 176.15 L 220.70 180.84 L 224.31 186.32 L 229.29 194.32 L 233.97 202.22 L 237.03 207.92 L 239.41 213.00 L 241.20 217.66 L 242.45 222.00 L 243.21 226.26 L 243.39 230.00 L 243.04 233.46 L 242.15 236.63 L 242.07 236.84 L 240.65 239.68 L 240.47 239.93 L 238.54 242.42 L 238.23 242.74 L 237.85 243.08 L 235.53 245.08 L 235.27 245.28 L 234.99 245.48 L 234.68 245.67 L 234.33 245.87 L 231.84 247.26 L 231.61 247.38 L 231.36 247.50 L 231.09 247.63 L 230.80 247.76 L 230.49 247.90 L 230.15 248.03 L 229.80 248.18 L 229.42 248.32 L 229.02 248.47 L 228.60 248.63 L 228.13 248.80 L 227.62 248.98 L 226.90 249.22 L 226.90 249.22 L 226.18 249.46 L 225.68 249.62 L 225.25 249.75 L 224.86 249.87 L 224.52 249.97 L 224.21 250.06 L 223.93 250.13 L 223.68 250.18 L 220.46 250.85 L 220.10 250.92 L 219.79 250.96 L 219.54 250.98 L 216.25 251.05 L 215.99 251.04 L 212.86 250.49 L 212.63 250.44 L 209.66 249.30 L 206.88 247.57 L 206.71 247.45 L 204.12 245.15 L 201.73 242.27 L 199.55 238.83 L 197.56 234.81 L 195.76 230.19 L 194.11 224.87 L 192.55 218.63 L 190.71 209.68 L 189.06 200.66 L 188.07 194.19 L 187.36 188.50 L 186.88 183.36 L 186.57 178.67 Z" fill="transparent" pointer-events="all"/></g></g><g data-avatar-entity-part-hit="ear-left" mask="url(#r0-entity-occlusion-0)"><g transform="translate(-71.8645347270797 -51.52484617981341)"><path d="M 176.46 229.28 L 176.69 225.56 L 177.46 221.56 L 178.86 216.97 L 180.72 212.36 L 183.16 207.36 L 186.29 201.75 L 191.07 193.99 L 196.03 186.30 L 199.87 180.70 L 203.22 176.10 L 206.35 172.05 L 209.29 168.47 L 212.05 165.32 L 214.63 162.59 L 217.13 160.17 L 219.31 158.30 L 221.30 156.86 L 221.42 156.80 L 223.22 155.83 L 223.34 155.79 L 224.98 155.30 L 225.09 155.29 L 225.21 155.28 L 226.68 155.31 L 226.76 155.31 L 226.86 155.33 L 226.97 155.35 L 227.09 155.37 L 227.22 155.41 L 227.37 155.45 L 227.54 155.50 L 227.73 155.57 L 228.00 155.66 L 228.28 155.76 L 228.48 155.84 L 228.65 155.91 L 228.81 155.98 L 228.96 156.04 L 229.11 156.11 L 229.24 156.18 L 229.36 156.24 L 229.48 156.31 L 229.59 156.38 L 229.69 156.44 L 229.78 156.51 L 229.86 156.57 L 229.93 156.64 L 230.00 156.70 L 230.95 157.69 L 231.05 157.81 L 231.14 157.93 L 231.23 158.09 L 232.01 159.65 L 232.11 159.86 L 232.15 160.00 L 232.76 162.08 L 233.18 164.59 L 233.44 167.54 L 233.55 170.91 L 233.55 171.11 L 233.52 174.70 L 233.51 174.93 L 233.33 178.92 L 233.32 179.17 L 232.97 183.83 L 232.43 188.96 L 231.68 194.62 L 230.64 201.05 L 228.99 209.70 L 228.93 210.01 L 227.11 218.58 L 227.03 218.90 L 225.44 225.10 L 223.76 230.36 L 221.94 234.94 L 219.93 238.91 L 217.74 242.30 L 215.34 245.12 L 215.12 245.31 L 212.75 247.36 L 212.54 247.54 L 209.77 249.20 L 206.82 250.27 L 206.56 250.35 L 203.46 250.82 L 203.18 250.85 L 202.83 250.85 L 199.67 250.69 L 199.36 250.66 L 199.00 250.60 L 198.58 250.52 L 198.12 250.41 L 195.30 249.70 L 195.00 249.62 L 194.66 249.53 L 194.29 249.42 L 193.89 249.29 L 193.44 249.14 L 192.92 248.96 L 192.19 248.70 L 192.19 248.70 L 191.45 248.44 L 190.93 248.24 L 190.46 248.06 L 190.04 247.90 L 189.64 247.73 L 189.27 247.58 L 188.93 247.42 L 188.61 247.27 L 188.32 247.13 L 188.05 246.99 L 185.78 245.80 L 185.36 245.57 L 184.98 245.35 L 184.65 245.13 L 184.36 244.92 L 184.11 244.72 L 181.82 242.78 L 181.54 242.51 L 181.32 242.24 L 179.47 239.85 L 179.21 239.42 L 177.86 236.65 L 177.65 236.18 L 176.81 233.05 L 176.76 232.72 Z" fill="transparent" pointer-events="all"/></g></g></g></g></g></g>
          </svg>
        </span>
      </span>

      <h1 class="checkout-title checkout-rise" style="animation-delay: 120ms;">
        Opening payment page
      </h1>

      <p class="checkout-subtitle checkout-rise" style="animation-delay: 220ms;">
        Please wait a moment while we set up your secure checkout session....
      </p>

      <div class="checkout-bar-container checkout-rise" style="animation-delay: 320ms;">
        <div role="status" aria-label="Preparing secure checkout" aria-live="polite" class="checkout-bar">
          <span aria-hidden="true" class="checkout-bar-fill"></span>
        </div>
      </div>

      <div id="fallback" style="display:none; margin-top:20px;">
        <button onclick="startCheckout()" class="btn-fallback">Tap to Pay with Cashfree</button>
      </div>
    </div>
  </div>

  <script>
    const sessionId = "__SESSION_ID__";
    const isSandbox = __IS_SANDBOX__;
    const WRAPPER_DURATION_MS = 3800;
    const EXIT_DURATION_MS = 420;

    let checkoutStarted = false;

    function startCheckout() {
      if (checkoutStarted) return;
      if (!sessionId) {
        document.getElementById('contentBlock').innerHTML = '<h1 style="color:#ef4444; font-size:20px; font-weight:500;">Invalid Session</h1><p style="color:#a1a1aa; margin-top:8px; font-size:14px;">Payment session ID is missing.</p>';
        return;
      }
      checkoutStarted = true;
      try {
        const cashfree = Cashfree({ mode: isSandbox ? "sandbox" : "production" });
        cashfree.checkout({
          paymentSessionId: sessionId,
          redirectTarget: "_self"
        });
      } catch (e) {
        console.error("Cashfree Checkout error:", e);
        const block = document.getElementById('contentBlock');
        if (block) block.classList.remove('checkout-exit');
        document.getElementById('fallback').style.display = 'block';
        checkoutStarted = false;
      }
    }

    setTimeout(() => {
      const block = document.getElementById('contentBlock');
      if (block) block.classList.add('checkout-exit');
    }, WRAPPER_DURATION_MS);

    setTimeout(startCheckout, WRAPPER_DURATION_MS + EXIT_DURATION_MS);
  </script>
</body>
</html>'''.replace("__SESSION_ID__", session_id).replace("__IS_SANDBOX__", is_sandbox_str)
    return HTMLResponse(content=html_content)





@api_router.post("/verify-cashfree-payment")
@api_router.get("/verify-cashfree-payment")
async def verify_cashfree_payment(payload: dict = None, order_id: str = None):
    """Verify Cashfree payment status by querying Cashfree API server-to-server with strict isolation between Delivery Bot Pass orders and Mini App Story orders."""
    if not payload and not order_id:
        return {"success": False, "detail": "Missing order_id"}
        
    oid = order_id or (payload.get("order_id") if payload else None) or (payload.get("cf_order_id") if payload else None)
    if not oid:
        return {"success": False, "detail": "Missing order_id in request"}

    oid = str(oid).strip()
    arya_db = getattr(app.state, "db", None) or db

    # ─────────────────────────────────────────────────────────────────────────
    # Branch 1: Delivery Bot Unlimited Pass Order Isolation
    # ─────────────────────────────────────────────────────────────────────────
    is_pass_order = bool(
        oid.startswith("PASS-") or
        await arya_db.db.delivery_pass_orders.count_documents({"$or": [{"order_id": oid}, {"cf_order_id": oid}, {"payment_session_id": oid}]}) > 0 or
        await arya_db.db.pass_orders.count_documents({"$or": [{"order_id": oid}, {"cf_order_id": oid}, {"payment_session_id": oid}]}) > 0
    )

    if is_pass_order:
        pass_order = (
            await arya_db.db.delivery_pass_orders.find_one({"$or": [{"order_id": oid}, {"cf_order_id": oid}, {"payment_session_id": oid}]}) or
            await arya_db.db.pass_orders.find_one({"$or": [{"order_id": oid}, {"cf_order_id": oid}, {"payment_session_id": oid}]})
        )

        if not pass_order:
            logger.warning(f"[CF-PASS] Pass order document not found for {oid}")
            return {"success": False, "detail": "Pass order not found", "is_pass": True}

        if pass_order.get("status") == "PAID":
            return {"success": True, "status": "paid", "order_id": oid, "is_pass": True}

        # Check Cashfree credentials
        cfg = await arya_db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
        app_id = (cfg.get("cashfree_app_id", "") or cfg.get("cashfree_api_id", "")).strip()
        secret_key = cfg.get("cashfree_secret_key", "").strip()
        cf_env = cfg.get("cashfree_env", "sandbox").strip().lower()
        is_sandbox = (cf_env in ("sandbox", "staging", "test") or "TEST" in app_id.upper() or "SANDBOX" in app_id.upper())

        if not app_id or not secret_key:
            return {"success": False, "detail": "Cashfree credentials missing", "is_pass": True}

        base_url = "https://sandbox.cashfree.com/pg" if is_sandbox else "https://api.cashfree.com/pg"
        check_url = f"{base_url}/orders/{oid}"
        headers = {
            "x-client-id": app_id,
            "x-client-secret": secret_key,
            "x-api-version": "2023-08-01",
            "Content-Type": "application/json"
        }

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(check_url, headers=headers)
                if resp.status_code == 200:
                    res_json = resp.json()
                    cf_status = str(res_json.get("order_status", "")).upper()
                    if cf_status in ("PAID", "SUCCESS"):
                        # Atomically claim pass order to guarantee idempotency across both collections
                        claimed = await arya_db.db.delivery_pass_orders.find_one_and_update(
                            {"order_id": oid, "status": {"$ne": "PAID"}},
                            {"$set": {"status": "PAID", "paid_at": time.time(), "payment_details": res_json}},
                            return_document=False
                        )
                        if not claimed:
                            claimed = await arya_db.db.pass_orders.find_one_and_update(
                                {"order_id": oid, "status": {"$ne": "PAID"}},
                                {"$set": {"status": "PAID", "paid_at": time.time(), "payment_details": res_json}},
                                return_document=False
                            )

                        if claimed:
                            p_uid = int(pass_order.get("user_id"))
                            p_dur = str(pass_order.get("duration", "1d"))
                            p_uname = pass_order.get("user_name", "User")
                            p_amt = float(pass_order.get("amount", 0))

                            from database import parse_duration_to_seconds, format_duration_verbose
                            dur_sec = parse_duration_to_seconds(p_dur, default_unit='d')
                            cur_pass = await arya_db.db.unlimited_passes.find_one({'user_id': p_uid})
                            now_ts = time.time()
                            base_t = cur_pass.get('expires_at', 0) if (cur_pass and cur_pass.get('expires_at', 0) > now_ts) else now_ts
                            new_exp = base_t + dur_sec
                            pass_fields = {
                                'expires_at': new_exp,
                                'user_name': p_uname,
                                'updated_at': now_ts,
                                'plan_key': p_dur,
                                'plan_name': f"{format_duration_verbose(dur_sec).title()} Unlimited Pass",
                                'amount': p_amt
                            }
                            if pass_order.get("bot_id"):
                                pass_fields['bot_id'] = int(pass_order.get("bot_id"))
                            if pass_order.get("bot_username"):
                                pass_fields['bot_username'] = str(pass_order.get("bot_username"))
                            await arya_db.db.unlimited_passes.update_one({'user_id': p_uid}, {'$set': pass_fields}, upsert=True)
                            logger.info(f"[CF-PASS-ACTIVATE] Pass order {oid} claimed and activated for user {p_uid}, new_exp={new_exp}")

                        return {"success": True, "status": "paid", "order_id": oid, "is_pass": True}
                    else:
                        return {"success": False, "status": cf_status, "order_id": oid, "is_pass": True}
                else:
                    return {"success": False, "detail": f"Cashfree API returned HTTP {resp.status_code}", "is_pass": True}
        except Exception as e:
            logger.error(f"[CF-PASS-ERROR] Exception verifying pass order {oid}: {e}")
            return {"success": False, "detail": str(e), "is_pass": True}

    # ─────────────────────────────────────────────────────────────────────────
    # Branch 2: Mini App Story Order Processing (Strictly isolated from Pass orders)
    # ─────────────────────────────────────────────────────────────────────────
    if str(oid).startswith("PASS-"):
        return {"success": False, "detail": "Pass order must not be processed as story order", "is_pass": True}

    order = await arya_db.db.orders.find_one({"$or": [{"order_id": oid}, {"cf_order_id": oid}, {"payment_session_id": oid}]})

    if not order or str(order.get("order_id", "")).startswith("PASS-"):
        logger.warning(f"[CF-STORY] No registered story order found for order_id={oid}. Skipping story order creation to prevent duplicate fake orders.")
        return {"success": False, "detail": "Story order not found in database"}

    if order.get("status") == "paid":
        return {"success": True, "status": "paid", "order_id": order.get("order_id", oid), "payment_id": order.get("payment_id", "")}

    cfg = await arya_db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    app_id     = (cfg.get("cashfree_app_id", "") or cfg.get("cashfree_api_id", "")).strip()
    secret_key = cfg.get("cashfree_secret_key", "").strip()
    cf_env     = cfg.get("cashfree_env", "sandbox").strip().lower()
    is_sandbox = (cf_env in ("sandbox", "staging", "test") or "TEST" in app_id.upper() or "SANDBOX" in app_id.upper())

    if not app_id or not secret_key:
        return {"success": False, "detail": "Cashfree credentials missing"}

    base_url = "https://sandbox.cashfree.com/pg" if is_sandbox else "https://api.cashfree.com/pg"
    cf_query_id = order.get("order_id") or oid
    check_url = f"{base_url}/orders/{cf_query_id}"

    headers = {
        "x-client-id": app_id,
        "x-client-secret": secret_key,
        "x-api-version": "2023-08-01",
        "Content-Type": "application/json"
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(check_url, headers=headers)
            if resp.status_code == 200:
                res_json = resp.json()
                cf_status = str(res_json.get("order_status", "")).upper()
                logger.info(f"Cashfree status check for {cf_query_id}: status={cf_status}")
                
                if cf_status in ("PAID", "SUCCESS"):
                    payment_id = res_json.get("cf_order_id") or cf_query_id
                    
                    # Atomic transition from non-paid to paid guarantees single execution
                    claimed = await arya_db.db.orders.find_one_and_update(
                        {"_id": order["_id"], "status": {"$ne": "paid"}},
                        {"$set": {
                            "status": "paid",
                            "payment_id": str(payment_id),
                            "paid_at": datetime.now(timezone.utc),
                            "updated_at": datetime.now(timezone.utc)
                        }},
                        return_document=False
                    )

                    if claimed:
                        user_id = order.get("user_id")
                        story_ids = order.get("story_ids", [])
                        
                        # Only add full story purchases to user.purchases (skip parts)
                        if user_id:
                            is_full_order = True
                            if order.get("items"):
                                is_full_order = all(itm.get("is_full", True) and not itm.get("part_id") for itm in order["items"])
                            if is_full_order and story_ids:
                                for sid in story_ids:
                                    try:
                                        await arya_db.add_purchase(user_id, sid)
                                    except Exception as e:
                                        logger.error(f"add_purchase error for {sid}: {e}")
                                    
                        story_names = order.get("story_names", [])
                        updated_order = {
                            **order,
                            "order_id": order.get("order_id", oid),
                            "user_id": user_id,
                            "story_ids": story_ids,
                            "story_names": story_names,
                            "total": float(res_json.get("order_amount", order.get("total", 0.0))),
                            "status": "paid",
                            "payment_id": str(payment_id),
                            "payment_method": "Cashfree",
                            "source": "Cashfree"
                        }
                        invalidate_buyers_cache()
                        asyncio.create_task(trigger_payment_log_from_order(updated_order))
                        asyncio.create_task(record_purchased_stories(updated_order))
                        asyncio.create_task(send_purchase_success_dm(arya_db, user_id, order_doc=updated_order, payment_method="Cashfree", verified_by="Auto Verified By System"))

                    return {"success": True, "status": "paid", "order_id": order.get("order_id", oid), "payment_id": str(payment_id)}
                else:
                    return {"success": False, "status": cf_status, "order_id": order.get("order_id", oid)}
            else:
                return {"success": False, "detail": f"Cashfree API returned HTTP {resp.status_code}"}
    except Exception as e:
        logger.error(f"Cashfree verify payment exception for {oid}: {e}")
        return {"success": False, "detail": str(e)}


@api_router.post("/cashfree-webhook")
@api_router.get("/cashfree-callback")
@api_router.post("/cashfree-callback")
async def cashfree_webhook(request: Request):
    """Handle Cashfree webhook & callback notifications for order status updates."""
    try:
        data = await request.json()
    except Exception:
        data = {k: str(v) for k, v in request.query_params.items()}

    logger.info(f"Cashfree callback/webhook data received: {data}")
    
    order_obj = data.get("data", {}).get("order", {}) or data.get("order", {}) if isinstance(data, dict) else {}
    order_id = (
        order_obj.get("order_id") or 
        (data.get("data", {}).get("order_id") if isinstance(data, dict) else None) or
        (data.get("order_id") if isinstance(data, dict) else None) or
        (data.get("cf_order_id") if isinstance(data, dict) else None)
    )
    
    if order_id:
        res = await verify_cashfree_payment(order_id=order_id)
        if request.method == "GET":
            from fastapi.responses import RedirectResponse
            target_return_base = "https://isaythanks.vercel.app"
            arya_db = getattr(app.state, "db", None)
            if arya_db:
                cfg = await arya_db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
                target_return_base = cfg.get("cashfree_return_url", "https://isaythanks.vercel.app").strip()
            
            if "order_id=" in target_return_base:
                target_redirect = target_return_base
            elif "?" in target_return_base:
                target_redirect = f"{target_return_base}&order_id={order_id}"
            else:
                target_redirect = f"{target_return_base}?order_id={order_id}"
                
            return RedirectResponse(url=target_redirect, status_code=307)
                
    return {"status": "OK"}


@api_router.post("/create-dodopayments-order")
async def create_dodopayments_order(payload: dict):
    """Create Dodo Payments checkout session for Arya Premium Mini App."""
    tg_id      = payload.get("telegram_id") or 0
    username   = payload.get("username", "") or ""
    first_name = payload.get("first_name", "") or ""
    promo_code = payload.get("promo_code", "")

    arya_db = app.state.db
    cfg = await arya_db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    
    dodo_status = cfg.get("dodopayments_status", "hidden")
    if dodo_status == "hidden":
        raise HTTPException(status_code=400, detail="Dodo Payments is currently disabled.")
    elif dodo_status == "disabled":
        raise HTTPException(status_code=400, detail="Dodo Payments is currently disabled by admin.")
        
    api_key    = cfg.get("dodopayments_api_key", "").strip()
    dodo_env   = cfg.get("dodopayments_environment", "test").strip().lower()
    product_id = cfg.get("dodopayments_product_id", "").strip()
    
    if not api_key:
        raise HTTPException(status_code=400, detail="Dodo Payments API Key is not configured in Admin Panel.")

    is_sandbox = (dodo_env in ("test", "sandbox") or "test" in api_key.lower())
    base_url = "https://test.dodopayments.com" if is_sandbox else "https://live.dodopayments.com"

    resolved_items, subtotal, story_ids, story_names = await _resolve_order_items(arya_db, payload)
    if not resolved_items:
        raise HTTPException(status_code=404, detail="Selected stories not found")
    promo_discount = 0.0
    if promo_code:
        p_doc = await arya_db.db.premium_promo_codes.find_one({"code": promo_code.upper().strip(), "active": True})
        if p_doc:
            p_type = p_doc.get("type", "percentage")
            p_val  = float(p_doc.get("value", 0))
            if p_type == "percentage":
                promo_discount = round((subtotal * p_val) / 100.0, 2)
            else:
                promo_discount = min(p_val, subtotal)

    total_amount = max(1.0, round(subtotal - promo_discount, 2))
    if total_amount < 50.0:
        raise HTTPException(
            status_code=400,
            detail="Dodo Payments requires a minimum order amount of ₹50 ($0.50 USD)."
        )

    import random, time
    order_seq = int(time.time() * 1000) % 100000
    order_id = f"AM-{tg_id}-{datetime.now().strftime('%d%m')}-{order_seq}"

    # Return URL strictly using sliceurl.app as required by user
    return_url = f"https://sliceurl.app/AryaPremium/#/payment-processing?order_id={order_id}&cf_order_id={order_id}&provider=dodopayments"

    dodo_payload = {
        "billing": {
            "city": "Mumbai",
            "country": "IN",
            "state": "MH",
            "street": "1 Main St",
            "zipcode": "400001"
        },
        "payment_link": True,
        "return_url": return_url,
        "metadata": {
            "order_id": order_id,
            "telegram_id": str(tg_id)
        }
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    # Smart product_id resolution: match target amount or create product for exact amount
    target_paise = int(round(total_amount * 100))
    pid_to_use = product_id.strip() if product_id and product_id.strip() else ""

    if not pid_to_use:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                p_resp = await client.get(f"{base_url}/products", headers=headers)
                if p_resp.status_code == 200:
                    items = p_resp.json().get("items", [])
                    for item in items:
                        p_detail = item.get("price_detail") or item.get("price") or {}
                        if isinstance(p_detail, dict):
                            if p_detail.get("price") == target_paise:
                                pid_to_use = item.get("product_id", "")
                                break
                
                if not pid_to_use:
                    create_prod_payload = {
                        "name": f"Arya Premium - INR {total_amount:.2f}",
                        "price": {
                            "type": "one_time_price",
                            "price": target_paise,
                            "currency": "INR",
                            "discount": 0,
                            "purchasing_power_parity": False,
                            "pay_what_you_want": False
                        },
                        "tax_category": "digital_products"
                    }
                    cp_resp = await client.post(f"{base_url}/products", json=create_prod_payload, headers=headers)
                    if cp_resp.status_code in (200, 201):
                        pid_to_use = cp_resp.json().get("product_id", "")
        except Exception as e:
            logger.warning(f"Failed to auto-fetch or create Dodo product: {e}")

    if not pid_to_use:
        raise HTTPException(status_code=400, detail="No valid Dodo Payments Product ID found. Please enter a Product ID in Admin Settings or create one in Dodo Payments Dashboard.")

    dodo_payload["product_cart"] = [
        {"product_id": pid_to_use, "quantity": 1}
    ]

    checkout_url = ""
    payment_session_id = ""

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(f"{base_url}/checkouts", json=dodo_payload, headers=headers)
            if resp.status_code not in (200, 201):
                resp = await client.post(f"{base_url}/payments", json=dodo_payload, headers=headers)

            if resp.status_code in (200, 201):
                res_json = resp.json()
                logger.info(f"Dodo Payments create response: status={resp.status_code}, body={res_json}")
                checkout_url = res_json.get("checkout_url") or res_json.get("payment_link") or res_json.get("url") or ""
                payment_session_id = str(res_json.get("checkout_id") or res_json.get("payment_id") or res_json.get("session_id") or order_id)
            else:
                logger.error(f"Dodo Payments create failed: HTTP {resp.status_code} -> {resp.text}")
                raise HTTPException(status_code=400, detail=f"Dodo Payments API error ({resp.status_code}): {resp.text}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Dodo Payments exception: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create Dodo Payments checkout: {e}")

    order_doc = {
        "order_id": order_id,
        "dodo_payment_id": payment_session_id,
        "payment_session_id": payment_session_id,
        "user_id": int(tg_id) if str(tg_id).isdigit() else tg_id,
        "username": username,
        "first_name": first_name,
        "story_ids": story_ids,
        "items": resolved_items,
        "story_names": story_names,
        "subtotal": subtotal,
        "promo_code": promo_code,
        "promo_discount": promo_discount,
        "total": total_amount,
        "gateway": "dodopayments",
        "provider": "dodopayments",
        "payment_link": checkout_url,
        "status": "pending",
        "auto_deliver": bool(payload.get("auto_deliver", True)),
        "created_at": datetime.now(timezone.utc),
        "is_sandbox": is_sandbox
    }
    await arya_db.db.orders.insert_one(order_doc)

    return {
        "success": True,
        "order_id": order_id,
        "checkout_url": checkout_url,
        "payment_link": checkout_url,
        "payment_session_id": payment_session_id,
        "total": total_amount,
        "is_sandbox": is_sandbox
    }


@api_router.get("/verify-dodopayments-payment")
@api_router.post("/verify-dodopayments-payment")
async def verify_dodopayments_payment(payload: dict = None, order_id: str = None):
    """Verify Dodo Payments order status."""
    oid = order_id or (payload.get("order_id") if payload else None) or (payload.get("cf_order_id") if payload else None)
    if not oid:
        return {"success": False, "detail": "Missing order_id"}

    arya_db = app.state.db
    order = await arya_db.db.orders.find_one({"$or": [{"order_id": oid}, {"dodo_payment_id": oid}, {"payment_session_id": oid}]})

    if order and order.get("status") == "paid":
        return {"success": True, "status": "paid", "order_id": oid}

    cfg = await arya_db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    api_key  = cfg.get("dodopayments_api_key", "").strip()
    dodo_env = cfg.get("dodopayments_environment", "test").strip().lower()
    is_sandbox = (dodo_env in ("test", "sandbox") or "test" in api_key.lower())
    base_url = "https://test.dodopayments.com" if is_sandbox else "https://live.dodopayments.com"

    if not api_key:
        return {"success": False, "detail": "Dodo Payments API Key missing"}

    dodo_pid = order.get("dodo_payment_id") if order else oid
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{base_url}/checkouts/{dodo_pid}", headers=headers)
            if resp.status_code not in (200, 201):
                resp = await client.get(f"{base_url}/payments/{dodo_pid}", headers=headers)

            if resp.status_code in (200, 201):
                res_json = resp.json()
                p_status = str(res_json.get("status") or res_json.get("payment_status") or "").lower()
                logger.info(f"Dodo Payments status check for {oid}: {p_status}")

                if p_status in ("succeeded", "paid", "completed", "success"):
                    user_id = order.get("user_id") if order else None
                    story_ids = order.get("story_ids", []) if order else []

                    if order:
                        await arya_db.db.orders.update_one(
                            {"_id": order["_id"]},
                            {"$set": {
                                "status": "paid",
                                "paid_at": datetime.now(timezone.utc)
                            }}
                        )

                    if user_id:
                        for sid in story_ids:
                            try:
                                await arya_db.add_purchase(user_id, sid)
                            except Exception as e:
                                logger.error(f"add_purchase error for {sid}: {e}")

                    updated_order = {
                        "order_id": oid,
                        "user_id": user_id,
                        "story_ids": story_ids,
                        "total": float(order.get("total", 0.0)) if order else 0.0,
                        "status": "paid",
                        "payment_id": str(dodo_pid),
                        "source": "dodopayments"
                    }
                    asyncio.create_task(trigger_payment_log_from_order(updated_order))
                    asyncio.create_task(record_purchased_stories(updated_order))
                    asyncio.create_task(send_purchase_receipt_to_user(updated_order))

                    return {"success": True, "status": "paid", "order_id": oid}
                else:
                    return {"success": False, "status": p_status, "order_id": oid}
            else:
                return {"success": False, "detail": f"Dodo API returned {resp.status_code}"}
    except Exception as e:
        logger.error(f"Dodo verify payment exception: {e}")
        return {"success": False, "detail": str(e)}


@api_router.post("/dodopayments-webhook")
@api_router.get("/dodopayments-callback")
@api_router.post("/dodopayments-callback")
async def dodopayments_webhook(request: Request):
    """Webhook callback for Dodo Payments events."""
    try:
        data = await request.json()
    except Exception:
        data = {k: str(v) for k, v in request.query_params.items()}

    logger.info(f"Dodo Payments webhook/callback received: {data}")
    order_id = data.get("metadata", {}).get("order_id") if isinstance(data, dict) else None
    if not order_id and isinstance(data, dict):
        order_id = data.get("order_id") or data.get("checkout_id") or data.get("payment_id") or data.get("cf_order_id")

    if order_id:
        res = await verify_dodopayments_payment(order_id=order_id)
        if request.method == "GET":
            if res.get("success"):
                return Response(
                    content="""<html><head><script src="https://telegram.org/js/telegram-web-app.js"></script></head><body style="background:#111;color:#fff;text-align:center;padding:50px;"><h2>✅ Payment Successful!</h2><p>Your Dodo Payment was verified.</p><button onclick="window.Telegram?.WebApp?.close() || window.close()" style="padding:10px 20px;background:#10b981;color:#fff;border:none;border-radius:8px;">Return to App</button></body></html>""",
                    media_type="text/html"
                )
            else:
                return Response(
                    content=f"""<html><body style="background:#111;color:#fff;text-align:center;padding:50px;"><h2>Processing Payment...</h2><p>{res.get('detail', 'Verification pending')}</p></body></html>""",
                    media_type="text/html"
                )
    return {"status": "ok"}




# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# POST /support
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
async def upload_file_to_storage(file_bytes: bytes, filename: str, content_type: str) -> str:
    """Uploads any file bytes to Cloudflare R2 if configured, or falls back to Catbox.moe."""
    import uuid
    import aiohttp
    import asyncio
    from decouple import config
    
    r2_account_id = config("R2_ACCOUNT_ID", default="")
    r2_access_key = config("R2_ACCESS_KEY_ID", default="")
    r2_secret_key = config("R2_SECRET_ACCESS_KEY", default="")
    r2_bucket = config("R2_BUCKET_NAME", default="arya-images")
    r2_domain = config("R2_CUSTOM_DOMAIN", default="")

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
                ext = filename.split(".")[-1] if "." in filename else "bin"
                key = f"{uuid.uuid4().hex}.{ext}"
                s3.put_object(
                    Bucket=r2_bucket,
                    Key=key,
                    Body=file_bytes,
                    ContentType=content_type or "application/octet-stream"
                )
                if r2_domain:
                    domain = r2_domain.strip("/")
                    if not domain.startswith("http"):
                        domain = "https://" + domain
                    return f"{domain}/{key}"
                else:
                    return f"https://{r2_account_id}.r2.cloudflarestorage.com/{r2_bucket}/{key}"
            except Exception as e:
                logger.error(f"Cloudflare R2 upload failed in upload_file_to_storage: {e}")
                return ""
        url = await asyncio.to_thread(upload_r2)

    if not url:
        try:
            async with aiohttp.ClientSession() as session:
                form = aiohttp.FormData()
                form.add_field("reqtype", "fileupload")
                form.add_field("fileToUpload", file_bytes, filename=filename, content_type=content_type)
                async with session.post("https://catbox.moe/user/api.php", data=form, timeout=60) as resp:
                    if resp.status == 200:
                        url = (await resp.text()).strip()
        except Exception as e:
            logger.error(f"Catbox upload failed in upload_file_to_storage: {e}")
            url = ""

    return url

@api_router.post("/support")
async def submit_support(
    telegram_id: str = Form(...),
    type: str = Form("support"),
    message: str = Form(""),
    username: str = Form(""),
    first_name: str = Form("Mini App User"),
    story_name: str = Form(None),
    platform: str = Form(None),
    status: str = Form(None),
    subject: str = Form(None),
    priority: str = Form("Normal"),
    category: str = Form(None),
    description: str = Form(None),
    device: str = Form(None),
    client_platform: str = Form(None),
    file: UploadFile = File(None)
):
    """Submits a support ticket, feedback, or suggestion from the Mini App, with optional file attachment."""
    message = message.strip()
    
    # For story requests, message is always provided (from the form textMsg). Allow requests without file.
    if not telegram_id or (not message and not file and type != "request"):
        raise HTTPException(status_code=400, detail="Message or file is required")

    arya_db = app.state.db
    uid = int(telegram_id) if str(telegram_id).isdigit() else telegram_id

    # Block banned/blocked users — silently ignore ticket, no DB write, no bot notification
    if isinstance(uid, int):
        banned = await arya_db.db.banned_users.find_one({"user_id": uid})
        if banned:
            logger.info(f"Ignored support ticket from banned user {uid}")
            return {"success": True, "message": "Support request received", "ticket_id": "IGN-BANNED"}

    # 1. Upload file if provided
    file_url = None
    file_contents = None
    if file:
        try:
            file_contents = await file.read()
            # Seek back in case the file is read again
            await file.seek(0)
            
            # If it's an image, we can try to optimize it first
            is_image = file.content_type and file.content_type.startswith("image/")
            if is_image:
                try:
                    file_url = await optimize_and_upload_to_storage(file_contents)
                except Exception as opt_err:
                    logger.warning(f"Failed to optimize image, falling back to raw upload: {opt_err}")
            
            # Fallback to uploading the raw bytes
            if not file_url:
                file_url = await upload_file_to_storage(file_contents, file.filename or "file", file.content_type)
                
            logger.info(f"File uploaded successfully for support ticket. URL: {file_url}")
        except Exception as upload_err:
            logger.error(f"Failed to upload file attachment: {upload_err}")

    # Always save to premium_feedback (for support panel + legacy compat)
    ticket_ref = f"ARY-{int(datetime.now(timezone.utc).timestamp()) % 899999 + 100000}"
    fb_doc = {
        "user_id": uid,
        "bot_id": "mini_app",
        "ticket_id": ticket_ref,
        "type": "photo" if file and file.content_type and file.content_type.startswith("image/") else ("video" if file and file.content_type and file.content_type.startswith("video/") else ("document" if file else "text")),
        "text": f"[{type.upper()}] {message}",
        "status": "open",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "user_name": first_name,
        "username": username,
        "source": "mini_app",
        "subject": subject or "",
        "priority": priority or "Normal",
        "category": (category or type).lower(),
        "description": description or message,
        "device": device or "Telegram Mini App",
        "platform": client_platform or "Android / iOS"
    }
    if file_url:
        fb_doc["file_url"] = file_url
        fb_doc["file_name"] = file.filename or "file"
    
    try:
        fb_result = await arya_db.db.premium_feedback.insert_one(fb_doc)
        fb_id = str(fb_result.inserted_id)

        # If this is a Story Request, ALSO write to premium_requests
        # so the Management Bot can see and manage it from its STORY REQUESTS panel
        if type == "request":
            req_doc = {
                "user_id": uid,
                "bot_id": "mini_app",
                "story_name": story_name or message[:120],
                "platform": platform or "Mini App",
                "completion_type": status or "full",
                "status": "Pending",
                "source": "mini_app",
                "text": message,
                "user_name": first_name,
                "username": username,
                "feedback_id": fb_id,          # cross-ref for status sync
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
            if file_url:
                req_doc["file_url"] = file_url
                req_doc["file_name"] = file.filename or "file"
            await arya_db.db.premium_requests.insert_one(req_doc)
        
        # Notify admins & send Telegram Bot DM notification to user
        try:
            from AryaPremium.config import Config
            import aiohttp
            
            escaped_first_name = escape_html(first_name or "Mini App User")
            escaped_username = f"@{escape_html(username)}" if username else "—"
            escaped_message = escape_html(message)
            escaped_subject = escape_html(subject or "General Support")
            escaped_description = escape_html(description or message)

            # Send Bot DM to user if ticket type
            if type == "ticket" or category or subject:
                try:
                    user_bot_token = Config.BOT_TOKEN or Config.MGMT_BOT_TOKEN
                    if user_bot_token and str(telegram_id).isdigit():
                        user_msg = (
                            f"🎫 <b>Ticket Created Successfully</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━\n"
                            f"Hello <b>{escaped_first_name}</b>!\n"
                            f"Your support ticket has been registered.\n\n"
                            f"🆔 <b>Ticket ID:</b> <code>{ticket_ref}</code>\n"
                            f"📌 <b>Subject:</b> {escaped_subject}\n"
                            f"📝 <b>Details:</b> {escaped_description[:150]}...\n"
                            f"⏳ <b>Est. Resolution Time:</b> ~2 Hours\n\n"
                            f"<i>You can check the status and admin responses directly in the Support section of the Mini App.</i>"
                        )
                        async with aiohttp.ClientSession() as session:
                            await session.post(f"https://api.telegram.org/bot{user_bot_token}/sendMessage", json={
                                "chat_id": telegram_id,
                                "text": user_msg,
                                "parse_mode": "HTML"
                            }, timeout=5)
                except Exception as u_err:
                    logger.warning(f"Failed to send user ticket DM: {u_err}")
            
            if type == "request":
                escaped_platform = escape_html(platform or "Not specified")
                escaped_story_name = escape_html(story_name or "")
                escaped_req_status = escape_html(status or "Unknown")
                # Extract language from message if present
                import re as _re
                lang_match = _re.search(r'Language:\s*([^\n]+)', message)
                escaped_language = escape_html(lang_match.group(1).strip() if lang_match else "Not specified")
                link_match = _re.search(r'Link:\s*([^\n]+)', message)
                story_link = escape_html(link_match.group(1).strip() if link_match else "")
                
                admin_txt = (
                    f"🛎️ <b>New Story Request from Mini App</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>User:</b> {escaped_first_name}\n"
                    f"<b>Username:</b> {escaped_username}\n"
                    f"<b>User ID:</b> <code>{telegram_id}</code>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>📚 Story Name:</b> {escaped_story_name or escaped_message[:80]}\n"
                    f"<b>📱 Platform:</b> {escaped_platform}\n"
                    f"<b>🌐 Language:</b> {escaped_language}\n"
                    f"<b>✅ Status:</b> {escaped_req_status}\n"
                    + (f"<b>🔗 Link:</b> {story_link}\n" if story_link else "")
                    + f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Full Request:</b>\n"
                    f"<blockquote>{escaped_message[:500]}</blockquote>\n"
                    f"<i>Manage from Admin Panel → Requests tab or Bot → STORY REQUESTS</i>"
                )
            elif type == "ticket" or category or subject:
                escaped_category = escape_html(category or "General")
                escaped_priority = escape_html(priority or "Normal")
                admin_txt = (
                    f"🎫 <b>New Support Ticket from Mini App</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>User:</b> {escaped_first_name}\n"
                    f"<b>Username:</b> {escaped_username}\n"
                    f"<b>User ID:</b> <code>{telegram_id}</code>\n"
                    f"<b>Ticket ID:</b> <code>{ticket_ref}</code>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Category:</b> {escaped_category}\n"
                    f"<b>Priority:</b> {escaped_priority}\n"
                    f"<b>Subject:</b> {escaped_subject}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Description:</b>\n"
                    f"<blockquote>{escaped_description[:800]}</blockquote>\n"
                    f"<i>Manage from Admin Panel → Support Desk</i>"
                )
            else:
                admin_txt = (
                    f"💬 <b>New {type.title()} from Mini App</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>User:</b> {escaped_first_name}\n"
                    f"<b>Username:</b> {escaped_username}\n"
                    f"<b>User ID:</b> <code>{telegram_id}</code>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Message:</b>\n"
                    f"<blockquote>{escaped_message[:800]}</blockquote>"
                )
            
            token = Config.MGMT_BOT_TOKEN
            if token and Config.OWNER_IDS:
                async with aiohttp.ClientSession() as session:
                    for oid in Config.OWNER_IDS:
                        try:
                            if file_contents:
                                form = aiohttp.FormData()
                                form.add_field('chat_id', str(oid))
                                form.add_field('caption', admin_txt)
                                form.add_field('parse_mode', 'HTML')
                                method = "sendDocument"
                                field_name = "document"
                                if file.content_type:
                                    if file.content_type.startswith("image/"):
                                        method = "sendPhoto"; field_name = "photo"
                                    elif file.content_type.startswith("video/"):
                                        method = "sendVideo"; field_name = "video"
                                    elif file.content_type.startswith("audio/"):
                                        method = "sendAudio"; field_name = "audio"
                                form.add_field(field_name, file_contents, filename=file.filename or "file")
                                await session.post(f"https://api.telegram.org/bot{token}/{method}", data=form, timeout=60)
                            else:
                                await session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                    "chat_id": oid, "text": admin_txt, "parse_mode": "HTML"
                                }, timeout=3)
                        except Exception as e:
                            logger.warning(f"Failed to notify admin {oid}: {e}")
        except Exception as notify_err:
            logger.error(f"Failed to process or send admin Telegram notification: {notify_err}")
                        
        return {"success": True, "message": "Support request submitted successfully", "ticket_id": ticket_ref, "id": fb_id}
    except Exception as e:
        logger.error(f"Support submission failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit support request")


@api_router.get("/support/user-tickets")
async def get_user_tickets(telegram_id: str):
    """Retrieves all support tickets for a specific user."""
    from bson.objectid import ObjectId
    from datetime import datetime, timezone
    try:
        arya_db = app.state.db
        uid_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        
        query = {
            "user_id": {"$in": [uid_int, str(uid_int)]},
            "$and": [
                {
                    "$or": [
                        {"category": {"$exists": False}},
                        {"category": None},
                        {"category": {"$nin": ["Live Chat", "live_chat", "request", "Request", "feedback", "Feedback", "Security", "security"]}}
                    ]
                },
                {
                    "$or": [
                        {"text": {"$exists": False}},
                        {"text": None},
                        {"text": {"$not": {"$regex": "^\\[(REQUEST|FEEDBACK|SECURITY)\\]", "$options": "i"}}}
                    ]
                }
            ]
        }
        
        cursor = arya_db.db.premium_feedback.find(query).sort("created_at", -1).limit(50)
        tickets = []
        async for doc in cursor:
            created_dt = doc.get("created_at", datetime.now(timezone.utc))
            created_str = created_dt.strftime("%d/%m/%Y, %H:%M") if isinstance(created_dt, datetime) else str(created_dt)
            
            text_content = doc.get("text", "")
            if text_content.startswith("[TICKET]") or text_content.startswith("[SUPPORT]"):
                text_content = text_content.split("]", 1)[-1].strip()
            
            subj = doc.get("subject") or text_content[:40] or "Live Chat Support"
            desc = doc.get("description") or text_content
            
            msgs = doc.get("messages", [])
            formatted_msgs = []
            for m in msgs:
                formatted_msgs.append({
                    "id": m.get("id", "m_reply"),
                    "from": m.get("from", "agent"),
                    "body": m.get("body") or m.get("text") or "",
                    "at": m.get("at", "12:00 PM"),
                    "file_url": m.get("file_url")
                })
                
            tickets.append({
                "id": str(doc["_id"]),
                "ticket_id": doc.get("ticket_id") or f"ARY-{str(doc['_id'])[-6:].upper()}",
                "subject": subj,
                "category": doc.get("category", "Support"),
                "priority": doc.get("priority", "Normal"),
                "status": doc.get("status", "Open").capitalize(),
                "created_at": created_str,
                "description": desc,
                "file_url": doc.get("file_url"),
                "messages": formatted_msgs,
                "est_time": "~2 Hours",
                "has_new_reply": doc.get("user_has_new_reply", False)
            })
        # ── Background: clear user_has_new_reply flag so badge disappears after user views ──
        new_reply_ids = [t["id"] for t in tickets if t.get("has_new_reply")]
        if new_reply_ids:
            async def _clear_reply_flags():
                try:
                    from bson.objectid import ObjectId as _ObjId
                    ids = [_ObjId(i) for i in new_reply_ids if len(i) == 24]
                    if ids:
                        await arya_db.db.premium_feedback.update_many(
                            {"_id": {"$in": ids}},
                            {"$set": {"user_has_new_reply": False}}
                        )
                except Exception as _e:
                    logger.warning(f"Failed to clear reply flags: {_e}")
            asyncio.create_task(_clear_reply_flags())

        return {"success": True, "data": tickets}
    except Exception as e:
        logger.error(f"Error fetching user tickets: {e}")
        return {"success": False, "data": []}


@api_router.post("/submit-order-review")
async def submit_order_review(
    order_id: str = Form(...),
    telegram_id: str = Form(...),
    message: str = Form(""),
    file: UploadFile = File(...)
):
    """Submits a manual payment proof screenshot for review."""
    message = message.strip()
    arya_db = app.state.db
    
    # Check if order exists
    order = await arya_db.db.orders.find_one({"order_id": order_id})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
        
    # Upload screenshot
    screenshot_url = None
    try:
        file_contents = await file.read()
        await file.seek(0)
        
        # Optimize and upload
        screenshot_url = await optimize_and_upload_to_storage(file_contents)
        if not screenshot_url:
            screenshot_url = await upload_file_to_storage(file_contents, file.filename or "screenshot", file.content_type)
            
        if not screenshot_url:
            raise HTTPException(status_code=500, detail="Failed to upload screenshot")
    except Exception as e:
        logger.error(f"Failed to upload order review screenshot: {e}")
        raise HTTPException(status_code=500, detail="Failed to upload proof")
        
    # Update order in DB
    await arya_db.db.orders.update_one(
        {"order_id": order_id},
        {"$set": {
            "status": "review_pending",
            "review_screenshot": screenshot_url,
            "review_message": message,
            "review_submitted_at": datetime.now(timezone.utc)
        }}
    )
    
    # Notify admins via Telegram
    try:
        from AryaPremium.config import Config
        import aiohttp
        
        user_name = order.get("username") or "User"
        amount = order.get("total") or order.get("total_amount") or 0
        story_names = ", ".join(order.get("story_names", []))
        
        admin_txt = (
            f"🧾 <b>New Manual Payment Review</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Order ID:</b> <code>{order_id}</code>\n"
            f"<b>User ID:</b> <code>{telegram_id}</code>\n"
            f"<b>Username:</b> @{user_name}\n"
            f"<b>Amount:</b> ₹{amount}\n"
            f"<b>Stories:</b> {story_names}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Message:</b>\n"
            f"<blockquote>{message or '—'}</blockquote>\n"
            f"<i>Manage this from the Admin Panel Orders Review section.</i>"
        )
        
        token = Config.MGMT_BOT_TOKEN
        if token and Config.OWNER_IDS:
            async with aiohttp.ClientSession() as session:
                for oid in Config.OWNER_IDS:
                    try:
                        # Send photo first
                        form = aiohttp.FormData()
                        form.add_field('chat_id', str(oid))
                        form.add_field('caption', admin_txt)
                        form.add_field('parse_mode', 'HTML')
                        form.add_field('photo', file_contents, filename=file.filename or "screenshot")
                        await session.post(f"https://api.telegram.org/bot{token}/sendPhoto", data=form, timeout=60)
                    except Exception as e:
                        logger.warning(f"Failed to notify admin {oid} about review: {e}")
    except Exception as notify_err:
        logger.error(f"Failed to notify admin about review: {notify_err}")
        
    return {"success": True, "message": "Proof submitted successfully"}


@api_router.get("/admin/pending-reviews")
async def get_pending_reviews(telegram_id: str):
    """Fetches all orders waiting for manual proof verification."""
    if not is_admin(telegram_id):
        raise HTTPException(status_code=403, detail="Not authorized")
    arya_db = app.state.db
    orders = await arya_db.db.orders.find({"status": "review_pending"}).sort("review_submitted_at", -1).to_list(length=100)
    
    result = []
    for o in orders:
        result.append({
            "order_id": o.get("order_id"),
            "user_id": o.get("user_id"),
            "username": o.get("username"),
            "story_names": o.get("story_names", []),
            "amount": o.get("total") or o.get("total_amount") or 0,
            "review_screenshot": o.get("review_screenshot"),
            "review_message": o.get("review_message"),
            "created_at": o.get("created_at").isoformat() if isinstance(o.get("created_at"), datetime) else str(o.get("created_at")),
            "review_submitted_at": o.get("review_submitted_at").isoformat() if isinstance(o.get("review_submitted_at"), datetime) else str(o.get("review_submitted_at"))
        })
    return {"success": True, "data": result}


@api_router.post("/admin/resolve-order-review")
async def resolve_order_review(payload: dict):
    """Approves or rejects a manual payment proof review."""
    telegram_id = str(payload.get("telegram_id", ""))
    order_id = payload.get("order_id")
    action = payload.get("action")  # "approve" or "reject"
    reason = payload.get("reason", "")
    
    if not is_admin(telegram_id):
        raise HTTPException(status_code=403, detail="Not authorized")
        
    if not order_id or action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="Invalid request parameters")
        
    arya_db = app.state.db
    order = await arya_db.db.orders.find_one({"order_id": order_id})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
        
    user_id = order.get("user_id")
    story_ids = order.get("story_ids", [])
    
    if action == "approve":
        # Update order status to paid
        await arya_db.db.orders.update_one(
            {"order_id": order_id},
            {"$set": {
                "status": "paid",
                "resolved_at": datetime.now(timezone.utc),
                "resolved_by": telegram_id
            }}
        )
        
        # Grant purchases
        if user_id:
            for sid in story_ids:
                try:
                    await arya_db.add_purchase(user_id, sid)
                except Exception as e:
                    logger.error(f"add_purchase error for {sid}: {e}")
                    
        # Log and audit records
        updated_order = {**order, "status": "paid"}
        asyncio.create_task(trigger_payment_log_from_order(updated_order))
        asyncio.create_task(record_purchased_stories(updated_order))
        
        # Send confirmation message to user via Telegram Bot
        asyncio.create_task(send_purchase_success_dm(
            arya_db,
            user_id,
            order_doc=updated_order,
            payment_method=order.get("source", "UPI (UTR)"),
            verified_by="Access Granted By Team",
            is_admin_manual=True
        ))
            
    else:
        # Reject review
        await arya_db.db.orders.update_one(
            {"order_id": order_id},
            {"$set": {
                "status": "review_rejected",
                "reject_reason": reason,
                "resolved_at": datetime.now(timezone.utc),
                "resolved_by": telegram_id
            }}
        )
        
        # Send rejection message to user via Telegram Bot (optional)
        try:
            from AryaPremium.config import Config
            import aiohttp
            token = Config.MGMT_BOT_TOKEN
            if token and user_id:
                async with aiohttp.ClientSession() as session:
                    reject_txt = (
                        f"❌ <b>Order Review Rejected</b>\n"
                        f"Your payment proof for order <code>{order_id}</code> was rejected.\n"
                        f"<b>Reason:</b> {reason or 'Invalid or unclear proof'}"
                    )
                    await session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                        "chat_id": user_id, "text": reject_txt, "parse_mode": "HTML"
                    }, timeout=3)
        except Exception:
            pass
            
    return {"success": True, "message": f"Order review {action}d successfully"}


# ─────────────────────────────────────────────────────────────────


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# GET /my-requests
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@api_router.get("/my-requests")
async def get_my_requests(telegram_id: str):
    """Fetches user's story requests — reads from premium_requests (unified) + legacy feedback."""
    arya_db = app.state.db
    
    try:
        user_id = int(telegram_id) if telegram_id.isdigit() else telegram_id
        user_id_str = str(user_id)
        requests = []
        seen_feedback_ids = set()

        # Primary: premium_requests (unified bot+miniapp collection)
        # Use $in to match both int and string stored user_id values
        cursor = arya_db.db.premium_requests.find(
            {"user_id": {"$in": [user_id, user_id_str]}}
        ).sort("created_at", -1)
        async for doc in cursor:
            fb_id = doc.get("feedback_id", "")
            if fb_id:
                seen_feedback_ids.add(fb_id)
            status = doc.get("status", "Pending")
            requests.append({
                "id": str(doc["_id"]),
                "type": "request",
                "story_name": doc.get("story_name", ""),
                "platform": doc.get("platform", ""),
                "text": doc.get("text", doc.get("story_name", "")),
                "status": status.lower() if status else "pending",
                "source": doc.get("source", "bot"),
                "admin_message": doc.get("admin_message", ""),
                "file_url": doc.get("file_url", ""),
                "language": doc.get("language", ""),
                "completion_type": doc.get("completion_type", ""),
                "link": doc.get("link", ""),
                "created_at": doc.get("created_at", datetime.now(timezone.utc)).isoformat() if isinstance(doc.get("created_at"), datetime) else doc.get("created_at", "")
            })

        # Legacy: premium_feedback with [REQUEST] prefix (not already linked)
        cursor2 = arya_db.db.premium_feedback.find({
            "user_id": {"$in": [user_id, user_id_str]},
            "text": {"$regex": "^\\[REQUEST\\]", "$options": "i"}
        }).sort("created_at", -1)
        async for doc in cursor2:
            fb_id = str(doc["_id"])
            if fb_id in seen_feedback_ids:
                continue
            raw_text = doc.get("text", "")
            display = raw_text.replace("[REQUEST]", "").replace("[request]", "").strip()
            requests.append({
                "id": fb_id,
                "type": "request",
                "story_name": display[:80],
                "platform": "",
                "text": display,
                "status": doc.get("status", "open"),
                "source": "mini_app_legacy",
                "created_at": doc.get("created_at", datetime.now(timezone.utc)).isoformat() if isinstance(doc.get("created_at"), datetime) else doc.get("created_at", "")
            })

        requests.sort(key=lambda x: x.get("created_at", ""), reverse=True)
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
        import asyncio
        
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        user_id_str = str(user_id_int)
        
        # Auto-verify any pending orders created within the last 2 hours
        try:
            from datetime import timedelta
            two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
            pending_orders = await arya_db.db.orders.find({
                "user_id": {"$in": [user_id_int, user_id_str]},
                "status": "pending",
                "created_at": {"$gte": two_hours_ago}
            }).to_list(length=10)
            
            for p_order in pending_orders:
                gw = p_order.get("gateway")
                p_oid = p_order.get("order_id")
                if p_oid:
                    if gw == "cashfree":
                        await verify_cashfree_payment(order_id=p_oid)
                    elif gw == "dodopayments":
                        await verify_dodopayments_payment(order_id=p_oid)
                    elif gw == "paytm":
                        await check_paytm_order_status(p_oid)
        except Exception as p_err:
            logger.warning(f"Auto-verification of pending orders error for user {user_id_int}: {p_err}")

        # In AryaPremium, purchases are in user.purchases
        user = await arya_db.db.users.find_one({"id": user_id_int})
        purchased_story_ids = list(user.get("purchases", [])) if user else []

        # ── BULK FETCH: all paid orders in ONE query ──
        order_cursor = arya_db.db.orders.find({
            "user_id": {"$in": [user_id_int, user_id_str]},
            "status": {"$in": ["paid", "delivered"]}
        }).sort("created_at", -1)
        paid_orders = await order_cursor.to_list(length=200)

        # Collect all story IDs needed
        story_id_set = set(purchased_story_ids)
        for ord_doc in paid_orders:
            if ord_doc.get("items") and isinstance(ord_doc["items"], list):
                for itm in ord_doc["items"]:
                    if itm.get("story_id"):
                        story_id_set.add(str(itm["story_id"]))
                    elif itm.get("id"):
                        story_id_set.add(str(itm["id"]))
            for sid in (ord_doc.get("story_ids") or []):
                if sid:
                    story_id_set.add(str(sid))

        story_oid_list = []
        for sid in story_id_set:
            try:
                story_oid_list.append(ObjectId(sid))
            except Exception:
                pass

        stories_by_oid = {}
        if story_oid_list:
            story_cursor = arya_db.db.premium_stories.find({"_id": {"$in": story_oid_list}})
            async for s in story_cursor:
                # Exclude Show Store Mode shows from Mini App Library
                if s.get("is_show") is True:
                    continue
                stories_by_oid[str(s["_id"])] = s

        # ── BULK FETCH: premium_purchases in ONE query ──
        pp_by_story: dict = {}
        if story_oid_list:
            pp_cursor = arya_db.db.premium_purchases.find({
                "user_id": {"$in": [user_id_int, user_id_str]},
                "story_id": {"$in": story_oid_list}
            })
            async for pp in pp_cursor:
                sid_str = str(pp.get("story_id", ""))
                if sid_str and sid_str not in pp_by_story:
                    pp_by_story[sid_str] = pp

        purchased_items = []
        represented_stories = set()

        for ord_doc in paid_orders:
            ord_id_str = str(ord_doc.get("order_id") or ord_doc.get("payment_link_id") or ord_doc.get("razorpay_order_id") or "")
            created_at_str = ord_doc.get("created_at").isoformat() if isinstance(ord_doc.get("created_at"), datetime) else str(ord_doc.get("created_at", ""))
            paid_at_str = ord_doc.get("paid_at").isoformat() if isinstance(ord_doc.get("paid_at"), datetime) else str(ord_doc.get("paid_at", created_at_str))

            if ord_doc.get("items") and isinstance(ord_doc["items"], list):
                for itm_idx, itm in enumerate(ord_doc["items"]):
                    sid = str(itm.get("story_id") or itm.get("id") or "")
                    story = stories_by_oid.get(sid)
                    if not story:
                        continue
                    represented_stories.add(sid)
                    formatted = _format_story(story)
                    if not formatted:
                        continue

                    part_id = itm.get("part_id")
                    part_dict = None
                    if part_id:
                        # Find matching part doc in formatted.parts or story.parts
                        matched_p = None
                        for sp in (formatted.get("parts") or story.get("parts") or []):
                            if str(sp.get("id")) == str(part_id):
                                matched_p = sp
                                break

                        p_start = (matched_p.get("start_id") if matched_p else 0) or itm.get("start_id") or 0
                        p_end = (matched_p.get("end_id") if matched_p else 0) or itm.get("end_id") or 0
                        p_ep = (matched_p.get("episodes") if matched_p else "") or itm.get("episodes") or (f"{p_start}-{p_end}" if (p_start and p_end) else "")
                        part_dict = {
                            "id": str(part_id),
                            "name": (matched_p.get("name") if matched_p else "") or itm.get("part_name") or f"Part {part_id}",
                            "name_hi": matched_p.get("name_hi") if matched_p else None,
                            "start_id": p_start,
                            "end_id": p_end,
                            "episodes": str(p_ep),
                            "price": itm.get("price") or (matched_p.get("price") if matched_p else 0),
                            "badge": matched_p.get("badge") if matched_p else None,
                            "is_ongoing": matched_p.get("is_ongoing") if matched_p else False,
                        }

                    unique_id = f"{sid}_{part_id}_{ord_id_str}" if part_id else f"{sid}_{ord_id_str}_{itm_idx}"
                    formatted["id"] = unique_id
                    formatted["purchase_id"] = unique_id
                    formatted["story_id"] = sid

                    if part_dict:
                        formatted["purchased_parts"] = [part_dict]
                        formatted["purchased_part"] = part_dict
                        formatted["selected_part"] = part_dict
                        formatted["episodes"] = part_dict["episodes"]
                        formatted["totalEpisodes"] = part_dict["episodes"]
                        formatted["is_full_purchased"] = False
                        formatted["price"] = itm.get("price") or part_dict.get("price") or formatted.get("price")
                        if part_dict.get("start_id"): formatted["start_id"] = part_dict["start_id"]
                        if part_dict.get("end_id"): formatted["end_id"] = part_dict["end_id"]
                    else:
                        formatted["is_full_purchased"] = True
                        formatted["price"] = itm.get("price") or ord_doc.get("total") or formatted.get("price")

                    formatted["order_details"] = {
                        "order_id": ord_id_str,
                        "source": ord_doc.get("source", "miniapp"),
                        "status": ord_doc.get("status"),
                        "amount": itm.get("price") or ord_doc.get("total") or ord_doc.get("amount"),
                        "created_at": created_at_str,
                        "paid_at": paid_at_str,
                        "resolved_by": ord_doc.get("resolved_by"),
                        "purchased_part": part_dict,
                        "purchased_parts": [part_dict] if part_dict else []
                    }
                    purchased_items.append(formatted)

            elif ord_doc.get("story_ids") and isinstance(ord_doc["story_ids"], list):
                for sid in ord_doc["story_ids"]:
                    sid_str = str(sid)
                    story = stories_by_oid.get(sid_str)
                    if not story:
                        continue
                    represented_stories.add(sid_str)
                    formatted = _format_story(story)
                    if not formatted:
                        continue

                    unique_id = f"{sid_str}_{ord_id_str}"
                    formatted["id"] = unique_id
                    formatted["purchase_id"] = unique_id
                    formatted["story_id"] = sid_str
                    formatted["is_full_purchased"] = True
                    formatted["price"] = ord_doc.get("total") or ord_doc.get("amount") or formatted.get("price")
                    formatted["order_details"] = {
                        "order_id": ord_id_str,
                        "source": ord_doc.get("source", "miniapp"),
                        "status": ord_doc.get("status"),
                        "amount": ord_doc.get("total") or ord_doc.get("amount"),
                        "created_at": created_at_str,
                        "paid_at": paid_at_str,
                        "resolved_by": ord_doc.get("resolved_by")
                    }
                    purchased_items.append(formatted)

        # Fallback for manual/legacy purchases in user.purchases or premium_purchases
        for sid_str in purchased_story_ids:
            if sid_str not in represented_stories:
                story = stories_by_oid.get(sid_str)
                if story:
                    formatted = _format_story(story)
                    if formatted:
                        formatted["id"] = sid_str
                        formatted["purchase_id"] = sid_str
                        formatted["story_id"] = sid_str
                        formatted["is_full_purchased"] = True
                        purchase_rec = pp_by_story.get(sid_str)
                        if purchase_rec:
                            p_at = purchase_rec.get("purchased_at") or purchase_rec.get("created_at")
                            formatted["order_details"] = {
                                "order_id": purchase_rec.get("order_id") or "",
                                "source": purchase_rec.get("source", "imported"),
                                "status": "paid",
                                "amount": purchase_rec.get("amount"),
                                "created_at": p_at.isoformat() if isinstance(p_at, datetime) else str(p_at or "")
                            }
                        else:
                            formatted["order_details"] = None
                        purchased_items.append(formatted)

        # Also query for recent pending/failed/processing/review orders (recent within 5m, under review/rejected within 7d)
        eight_days_ago_naive = datetime.utcnow() - timedelta(days=8)
        
        recent_orders_cursor = arya_db.db.orders.find({
            "user_id": {"$in": [user_id_int, str(user_id_int)]},
            "status": {"$in": ["pending", "failed", "processing", "review_pending", "review_rejected"]},
            "created_at": {"$gte": eight_days_ago_naive}
        })
        
        async for order in recent_orders_cursor:
            story_ids = order.get("story_ids", [])
            if isinstance(story_ids, str):
                story_ids = [story_ids]
            
            status = order.get("status")
            created_at = order.get("created_at")
            if not isinstance(created_at, datetime):
                continue
                
            # Normalize to naive UTC datetime
            if created_at.tzinfo is not None:
                created_at_utc = created_at.astimezone(timezone.utc).replace(tzinfo=None)
            else:
                created_at_utc = created_at
                
            now_naive = datetime.utcnow()
            
            # Detect and adjust if DB saved local IST time as naive datetime
            if created_at_utc > now_naive + timedelta(minutes=1):
                created_at_utc = created_at_utc - timedelta(hours=5, minutes=30)
                
            age = now_naive - created_at_utc
            
            # Check timeouts:
            # - pending, failed, processing: 5 minutes
            # - review_rejected: 24 hours
            # - review_pending: 7 days
            if status in ["pending", "failed", "processing"]:
                if age > timedelta(minutes=5):
                    continue
            elif status == "review_rejected":
                if age > timedelta(hours=24):
                    continue
            elif status == "review_pending":
                if age > timedelta(days=7):
                    continue
            
            for story_id in story_ids:
                if story_id in purchased_story_ids:
                    continue  # Already successfully purchased and shown
                
                try:
                    story = await arya_db.db.premium_stories.find_one({"_id": ObjectId(story_id)})
                    if story and not story.get("is_show"):
                        formatted = _format_story(story)
                        if formatted:
                            formatted["story_id"] = formatted["id"]
                            formatted["temp_order"] = True
                            formatted["order_details"] = {
                                "order_id": order.get("order_id") or str(order.get("_id")),
                                "source": order.get("source", "miniapp"),
                                "status": order.get("status", "pending"),
                                "created_at": order.get("created_at").isoformat() if isinstance(order.get("created_at"), datetime) else str(order.get("created_at", "")),
                                "review_message": order.get("review_message"),
                                "review_screenshot": order.get("review_screenshot"),
                                "reject_reason": order.get("reject_reason")
                            }
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
_buyers_data_lock = asyncio.Lock()
_processed_buyers_cache = None
_processed_buyers_cache_time = 0.0
PROCESSED_BUYERS_CACHE_TTL = 60.0  # Cache for 60 seconds

def invalidate_buyers_cache():
    global _processed_buyers_cache, _processed_buyers_cache_time, _admin_stats_cache, _admin_stats_cache_time
    _processed_buyers_cache = None
    _processed_buyers_cache_time = 0.0
    _admin_stats_cache = None
    _admin_stats_cache_time = 0.0

async def fetch_processed_buyers_data(arya_db):
    """
    Unified calculation engine for buyers, orders, revenue, and story rankings.
    Guarantees 100% data consistency across Dashboard, Buyers Tab, and Story Ranking.
    Filters out wiped users and removed story access.
    """
    global _processed_buyers_cache, _processed_buyers_cache_time
    now = time.time()
    if _processed_buyers_cache is not None and (now - _processed_buyers_cache_time < PROCESSED_BUYERS_CACHE_TTL):
        return _processed_buyers_cache

    async with _buyers_data_lock:
        now = time.time()
        if _processed_buyers_cache is not None and (now - _processed_buyers_cache_time < PROCESSED_BUYERS_CACHE_TTL):
            return _processed_buyers_cache

        # Projections for ultra-fast query execution
        user_projection = {
            "id": 1, "purchases": 1, "first_name": 1, "last_name": 1, 
            "username": 1, "photo_url": 1, "joined_date": 1, "joined_at": 1, "created_at": 1
        }
        existing_users = await arya_db.db.users.find({}, user_projection).to_list(length=200000)
        existing_user_ids = set()
        user_purchases_map = {}  # uid_str -> set of story_id_strs
        user_doc_map = {}        # uid_str -> user_doc
        
        for u in existing_users:
            uid = u.get("id")
            if uid is None: continue
            uid_str = str(uid)
            existing_user_ids.add(uid_str)
            if uid_str.isdigit():
                existing_user_ids.add(str(int(uid_str)))
                
            purchases_list = [str(s) for s in u.get("purchases", []) if s]
            user_purchases_map[uid_str] = set(purchases_list)
            user_doc_map[uid_str] = u

        story_proj = {
            "_id": 1, "story_id": 1, "story_name_en": 1, "title": 1, "price": 1, "discounted_price": 1,
            "parts": 1, "story_parts": 1, "episode_parts": 1, "episodes_parts": 1, "part_list": 1
        }
        stories = await arya_db.db.premium_stories.find({}, story_proj).to_list(length=10000)
        story_cache_by_id = {}
        story_cache_by_oid = {}
        sid_to_canonical = {}

        for s in stories:
            sid_str = str(s.get("story_id", ""))
            soid_str = str(s.get("_id", ""))
            price = float(s.get("discounted_price") or s.get("price") or 99.0)
            s["_clean_price"] = price
            canon = sid_str or soid_str
            if sid_str:
                story_cache_by_id[sid_str] = s
                sid_to_canonical[sid_str] = canon
            if soid_str:
                story_cache_by_oid[soid_str] = s
                sid_to_canonical[soid_str] = canon

        ord_proj = {
            "_id": 1, "order_id": 1, "user_id": 1, "status": 1, "story_ids": 1, "story_id": 1,
            "items": 1, "part_id": 1, "part_name": 1, "episodes": 1,
            "source": 1, "total_amount": 1, "total": 1, "amount": 1, "method": 1, "payment_method": 1,
            "reference": 1, "utr": 1, "payment_id": 1, "created_at": 1, "paid_at": 1
        }
        chk_proj = {
            "_id": 1, "order_id": 1, "track_id": 1, "user_id": 1, "status": 1, "story_id": 1,
            "part_id": 1, "part_name": 1, "episodes": 1,
            "amount": 1, "method": 1, "reference": 1, "utr": 1, "created_at": 1, "first_name": 1, "username": 1
        }
        pur_proj = {
            "_id": 1, "order_id": 1, "user_id": 1, "story_id": 1, "part_id": 1, "part_name": 1,
            "amount": 1, "source": 1, "method": 1, "reference": 1, "utr": 1, "payment_id": 1,
            "bot_id": 1, "purchased_at": 1, "created_at": 1
        }

        orders = await arya_db.db.orders.find({}, ord_proj).sort("created_at", -1).to_list(length=50000)
        checkouts = await arya_db.db.premium_checkout.find({}, chk_proj).sort("created_at", -1).to_list(length=50000)
        purchases = await arya_db.db.premium_purchases.find({}, pur_proj).sort("purchased_at", -1).to_list(length=50000)

        buyers_map = {}
        added_paid_stories = {}  # uid_str -> set of story_id_strs

        def _clean_order_id_value(doc, uid_str: str, story_ids: list = None, source: str = "miniapp") -> str:
            raw_oid = doc.get("order_id") if isinstance(doc, dict) else None
            ref = (doc.get("reference") or doc.get("utr") or doc.get("payment_id") or doc.get("transaction_id")) if isinstance(doc, dict) else None

            if raw_oid:
                oid_str = str(raw_oid).strip()
                if oid_str and not oid_str.startswith("uid_"):
                    if oid_str.startswith("AB-") or oid_str.startswith("AM-") or oid_str.startswith("ORD-") or oid_str.startswith("PAY-"):
                        return oid_str
                    clean_base = oid_str.replace("checkout_", "").replace("purchase_", "").replace("order_", "").replace("OD_", "").replace("OD-", "").strip()
                    if clean_base:
                        pfx = "AB" if "bot" in str(source).lower() else "AM"
                        return f"{pfx}-{clean_base}"

            if ref:
                ref_str = str(ref).strip().upper()
                if ref_str:
                    pfx = "AB" if "bot" in str(source).lower() else "AM"
                    return f"{pfx}-{uid_str}-{ref_str[-8:]}"

            date_val = (doc.get("purchased_at") or doc.get("created_at") or "") if isinstance(doc, dict) else ""
            date_part = date_val.strftime("%d%m") if isinstance(date_val, datetime) else "0108"
            
            doc_id_str = str(doc.get("_id", "")) if isinstance(doc, dict) else ""
            stable_hash = str(abs(hash(doc_id_str or str(story_ids))) % 90000 + 10000)
            pfx = "AB" if "bot" in str(source).lower() else "AM"
            return f"{pfx}-{uid_str}-{date_part}-{stable_hash}"

        def get_or_create_buyer(uid_str, fallback_doc=None, fallback_source="miniapp"):
            if uid_str not in buyers_map:
                u_doc = user_doc_map.get(uid_str) or fallback_doc or {}
                _fn = (u_doc.get("first_name") or (fallback_doc.get("first_name") if fallback_doc else "") or "").strip()
                _ln = (u_doc.get("last_name") or "").strip()
                _full = " ".join(filter(None, [_fn, _ln])) or u_doc.get("username", (fallback_doc.get("username") if fallback_doc else "") or "") or f"User {uid_str}"
                _uname = u_doc.get("username", (fallback_doc.get("username") if fallback_doc else "Unknown"))
                _photo = u_doc.get("photo_url", "")
                j_date = u_doc.get("joined_date") or u_doc.get("joined_at") or u_doc.get("created_at") or datetime.now(timezone.utc)
                j_str = j_date.isoformat() if isinstance(j_date, datetime) else str(j_date)
                
                try: uid_val = int(uid_str)
                except: uid_val = uid_str

                buyers_map[uid_str] = {
                    "order_id": f"uid_{uid_str}",
                    "user_id": uid_val,
                    "username": _uname,
                    "first_name": _full,
                    "photo_url": _photo,
                    "payments": [],
                    "total_amt": 0.0,
                    "source": fallback_source,
                    "joined_at": j_str,
                    "date": j_str
                }
            return buyers_map[uid_str]

        # Track seen order keys and story parts per user to prevent cross-collection duplicate entries
        # user_seen_orders: uid_str -> set of order_id / ref strings
        # user_seen_stories: uid_str -> set of (canonical_sid, part_id or "FULL")
        user_seen_orders = {}
        user_seen_stories = {}

        # 1. Process B FIRST: `orders` collection (richest data source with items, part details, actual totals)
        for doc in orders:
            uid = doc.get("user_id")
            if uid is None: continue
            uid_str = str(uid)
            if uid_str not in existing_user_ids:
                continue

            if uid_str not in user_seen_orders:
                user_seen_orders[uid_str] = set()
            if uid_str not in user_seen_stories:
                user_seen_stories[uid_str] = set()

            status_raw = doc.get("status", "unknown").lower()
            story_ids = [str(s) for s in doc.get("story_ids", []) if s]
            if not story_ids and doc.get("story_id"):
                story_ids = [str(doc.get("story_id"))]

            src_val = str(doc.get("source", "")).lower()
            raw_oid_val = str(doc.get("order_id", "") or "").strip()
            doc_id_val = str(doc.get("_id", "")).strip()
            oid_val = raw_oid_val or doc_id_val
            if "bot" in src_val or oid_val.upper().startswith("AB-") or oid_val.upper().startswith("MANUAL_"):
                source_label = "bot"
            else:
                source_label = "miniapp"

            amt = doc.get("total_amount", doc.get("total", doc.get("amount", 0)))
            try: amt = float(amt)
            except: amt = 0

            story_names = []
            if doc.get("items") and isinstance(doc["items"], list):
                for itm in doc["items"]:
                    sid = str(itm.get("story_id") or itm.get("id") or "")
                    canonical_sid = sid_to_canonical.get(sid, sid)
                    st = story_cache_by_oid.get(sid) or story_cache_by_id.get(sid)
                    s_title = itm.get("story_title") or (st.get("story_name_en") or st.get("title") if st else sid)
                    part_name = itm.get("part_name")
                    part_id = itm.get("part_id")
                    episodes = itm.get("episodes")
                    if (not episodes or not part_name) and part_id and st:
                        parts_list = st.get("parts") or st.get("story_parts") or st.get("episode_parts") or st.get("part_list") or []
                        for sp in parts_list:
                            if str(sp.get("id")).strip() == str(part_id).strip():
                                episodes = episodes or sp.get("episodes") or (f"{sp.get('start_id')}-{sp.get('end_id')}" if sp.get("start_id") and sp.get("end_id") else "")
                                part_name = part_name or sp.get("name") or f"Part {part_id}"
                                break
                    p_key = str(part_id).strip() if part_id else "FULL"
                    if canonical_sid:
                        user_seen_stories[uid_str].add((canonical_sid, p_key))
                    if part_name or part_id:
                        p_label = part_name or f"Part {part_id}"
                        ep_str = f" · Ep {episodes}" if episodes else ""
                        story_names.append(f"{s_title} ({p_label}{ep_str})")
                    else:
                        story_names.append(s_title)
            elif doc.get("part_id") or doc.get("part_name"):
                part_id = doc.get("part_id")
                part_name = doc.get("part_name")
                episodes = doc.get("episodes")
                p_key = str(part_id).strip() if part_id else "FULL"
                for sid in story_ids:
                    canonical_sid = sid_to_canonical.get(sid, sid)
                    if canonical_sid:
                        user_seen_stories[uid_str].add((canonical_sid, p_key))
                    st = story_cache_by_oid.get(sid) or story_cache_by_id.get(sid)
                    s_title = st.get("story_name_en") or st.get("title") if st else sid
                    if (not episodes or not part_name) and part_id and st:
                        parts_list = st.get("parts") or st.get("story_parts") or st.get("episode_parts") or st.get("part_list") or []
                        for sp in parts_list:
                            if str(sp.get("id")).strip() == str(part_id).strip():
                                episodes = episodes or sp.get("episodes")
                                part_name = part_name or sp.get("name")
                                break
                    p_label = part_name or f"Part {part_id}"
                    ep_str = f" · Ep {episodes}" if episodes else ""
                    story_names.append(f"{s_title} ({p_label}{ep_str})")
            else:
                for sid in story_ids:
                    canonical_sid = sid_to_canonical.get(sid, sid)
                    if canonical_sid:
                        user_seen_stories[uid_str].add((canonical_sid, "FULL"))
                    story = story_cache_by_oid.get(sid) or story_cache_by_id.get(sid)
                    if story: story_names.append(story.get("story_name_en") or story.get("title", sid))
                    else: story_names.append(sid)

            if amt <= 0 and story_ids:
                st = story_cache_by_oid.get(story_ids[0]) or story_cache_by_id.get(story_ids[0])
                amt = st["_clean_price"] if st else 99.0

            date_val = doc.get("created_at") or datetime.now(timezone.utc)
            date_str = date_val.isoformat() if isinstance(date_val, datetime) else str(date_val)

            clean_oid = _clean_order_id_value(doc, uid_str, story_ids, source=source_label)
            ref_val = str(doc.get("reference") or doc.get("utr") or doc.get("payment_id") or doc.get("transaction_id") or "").strip()
            method_str = str(doc.get("method", doc.get("payment_method", "UPI"))).upper()
            if method_str.lower() == "miniapp":
                method_str = "UPI"

            b = get_or_create_buyer(uid_str, fallback_doc=doc, fallback_source=source_label)

            if clean_oid: user_seen_orders[uid_str].add(clean_oid.upper())
            if raw_oid_val: user_seen_orders[uid_str].add(raw_oid_val.upper())
            if doc_id_val: user_seen_orders[uid_str].add(doc_id_val.upper())
            if ref_val: user_seen_orders[uid_str].add(ref_val.upper())

            b["payments"].append({
                "order_id": clean_oid,
                "reference": ref_val or raw_oid_val or doc_id_val,
                "story_id": story_ids[0] if story_ids else "",
                "story_name": ", ".join(story_names) if story_names else "Store Order",
                "amount": amt,
                "method": method_str,
                "status": status_raw,
                "date": date_str,
                "source": source_label,
                "items": doc.get("items", [])
            })

        # 2. Process A: `premium_purchases` (legacy / direct DB records)
        for p in purchases:
            uid = p.get("user_id")
            if uid is None: continue
            uid_str = str(uid)
            if uid_str not in existing_user_ids:
                continue

            story_id = p.get("story_id")
            story_id_str = str(story_id) if story_id else ""
            if not story_id_str: continue

            canonical_sid = sid_to_canonical.get(story_id_str, story_id_str)
            part_id = str(p.get("part_id") or "").strip()
            p_key = part_id if part_id else "FULL"
            item_key = (canonical_sid, p_key)

            p_oid = str(p.get("order_id", "")).strip()
            p_ref = str(p.get("reference") or p.get("utr") or p.get("payment_id") or "").strip()

            # Skip if order ID or reference was already processed
            if uid_str in user_seen_orders:
                if (p_oid and p_oid.upper() in user_seen_orders[uid_str]) or (p_ref and p_ref.upper() in user_seen_orders[uid_str]):
                    continue
            # Skip if this user already has this exact story or part from the orders collection
            if uid_str in user_seen_stories:
                if item_key in user_seen_stories[uid_str]:
                    continue

            story = story_cache_by_oid.get(story_id_str) or story_cache_by_id.get(story_id_str)
            sname = story.get("story_name_en", story.get("title", story_id_str)) if story else "Story Purchase"
            if part_id and story:
                for sp in (story.get("parts") or []):
                    if str(sp.get("id")) == str(part_id):
                        p_ep = sp.get("episodes", "")
                        ep_str = f" · Ep {p_ep}" if p_ep else ""
                        sname = f"{sname} ({sp.get('name', f'Part {part_id}')}{ep_str})"
                        break

            amt = p.get("amount", 0)
            try: amt = float(amt)
            except: amt = 0
            if amt <= 0:
                amt = story["_clean_price"] if story else 99.0

            date_val = p.get("purchased_at") or p.get("created_at") or datetime.now(timezone.utc)
            date_str = date_val.isoformat() if isinstance(date_val, datetime) else str(date_val)

            src_val = str(p.get("source", "")).lower()
            oid_val = str(p.get("order_id", "")).upper()
            if "bot" in src_val or oid_val.startswith("AB-") or oid_val.startswith("MANUAL_") or bool(p.get("bot_id")):
                source_label = "bot"
            else:
                source_label = "miniapp"

            clean_oid = _clean_order_id_value(p, uid_str, [story_id_str], source=source_label)

            b = get_or_create_buyer(uid_str, fallback_doc=p, fallback_source=source_label)
            if uid_str not in user_seen_orders:
                user_seen_orders[uid_str] = set()
            if uid_str not in user_seen_stories:
                user_seen_stories[uid_str] = set()

            if clean_oid: user_seen_orders[uid_str].add(clean_oid.upper())
            if p_oid: user_seen_orders[uid_str].add(p_oid.upper())
            if p_ref: user_seen_orders[uid_str].add(p_ref.upper())
            user_seen_stories[uid_str].add(item_key)

            pay_method = str(p.get("method") or "UPI").upper()
            if pay_method.lower() == "miniapp":
                pay_method = "UPI"

            b["payments"].append({
                "order_id": clean_oid,
                "reference": p_ref or p_oid,
                "story_id": story_id_str,
                "story_name": sname,
                "amount": amt,
                "method": pay_method,
                "status": "paid",
                "date": date_str,
                "source": source_label
            })

        # 3. Process C: premium_checkout (bot checkouts)
        for c in checkouts:
            uid = c.get("user_id")
            if uid is None: continue
            uid_str = str(uid)
            if uid_str not in existing_user_ids:
                continue

            story_id = c.get("story_id")
            story_id_str = str(story_id) if story_id else ""
            canonical_sid = sid_to_canonical.get(story_id_str, story_id_str)
            part_id = str(c.get("part_id") or "").strip()
            p_key = part_id if part_id else "FULL"
            item_key = (canonical_sid, p_key)

            c_oid = str(c.get("order_id") or c.get("track_id") or c.get("_id") or "").strip()
            c_ref = str(c.get("reference") or c.get("utr") or "").strip()

            if uid_str in user_seen_orders:
                if (c_oid and c_oid.upper() in user_seen_orders[uid_str]) or (c_ref and c_ref.upper() in user_seen_orders[uid_str]):
                    continue
            if uid_str in user_seen_stories:
                if item_key in user_seen_stories[uid_str]:
                    continue

            status_raw = c.get("status", "unknown")
            status_label = {
                "approved": "paid",
                "waiting_screenshot": "pending",
                "rejected": "rejected",
                "pending_gateway": "processing",
            }.get(status_raw, status_raw.lower())

            story = story_cache_by_oid.get(story_id_str) or story_cache_by_id.get(story_id_str)
            sname = story.get("story_name_en", story_id_str) if story else "Bot Purchase"

            if part_id and story:
                for sp in (story.get("parts") or []):
                    if str(sp.get("id")) == str(part_id):
                        p_ep = sp.get("episodes", "")
                        ep_str = f" · Ep {p_ep}" if p_ep else ""
                        sname = f"{sname} ({sp.get('name', f'Part {part_id}')}{ep_str})"
                        break

            amt = c.get("amount", 0)
            try: amt = float(amt)
            except: amt = 0
            if amt <= 0 and story:
                amt = story["_clean_price"]

            date_val = c.get("created_at") or datetime.now(timezone.utc)
            date_str = date_val.isoformat() if isinstance(date_val, datetime) else str(date_val)

            clean_oid = _clean_order_id_value(c, uid_str, [story_id_str], source="bot")

            b = get_or_create_buyer(uid_str, fallback_doc=c, fallback_source="bot")
            if uid_str not in user_seen_orders:
                user_seen_orders[uid_str] = set()
            if uid_str not in user_seen_stories:
                user_seen_stories[uid_str] = set()

            if clean_oid: user_seen_orders[uid_str].add(clean_oid.upper())
            if c_oid: user_seen_orders[uid_str].add(c_oid.upper())
            if c_ref: user_seen_orders[uid_str].add(c_ref.upper())
            user_seen_stories[uid_str].add(item_key)

            b["payments"].append({
                "order_id": clean_oid,
                "reference": c_ref or c_oid,
                "story_id": story_id_str,
                "story_name": sname,
                "amount": amt,
                "method": str(c.get("method", "UPI")).upper(),
                "status": status_label,
                "date": date_str,
                "source": "bot"
            })

        buyers_list = []
        total_rev = 0.0
        bot_rev = 0.0
        miniapp_rev = 0.0
        total_paid_orders_count = 0
        paid_user_ids = set()

        for uid_str, data in buyers_map.items():
            payments = data["payments"]
            if not payments: continue

            # Group payments by order_id / reference / date-minute so multi-story cart checkouts
            # appear as ONE single order entry in Admin Panel with true order total (no duplicate revenue multiplication).
            grouped_payments = {}
            for p_item in payments:
                ref = str(p_item.get("reference") or "").strip()
                oid = str(p_item.get("order_id") or "").strip()
                date_str = str(p_item.get("date") or "")
                date_minute = date_str[:16] if len(date_str) >= 16 else date_str
                
                # Determine stable grouping key for multi-story cart checkouts
                if ref and len(ref) > 3 and not ref.startswith("uid_"):
                    group_key = f"ref_{ref}"
                elif oid and not oid.startswith("uid_") and not oid.startswith("single_"):
                    group_key = f"order_{oid}"
                else:
                    group_key = f"batch_{p_item.get('source')}_{p_item.get('method')}_{date_minute}"

                if group_key not in grouped_payments:
                    grouped_payments[group_key] = {
                        "order_id": oid or f"AM-{uid_str}-{group_key[-8:]}",
                        "reference": ref,
                        "story_id": p_item.get("story_id", ""),
                        "story_ids": [p_item.get("story_id")] if p_item.get("story_id") else [],
                        "story_names": [p_item.get("story_name")] if p_item.get("story_name") else [],
                        "stories": [{
                            "story_id": p_item.get("story_id", ""),
                            "story_name": p_item.get("story_name", "")
                        }] if p_item.get("story_id") else [],
                        "raw_amounts": [float(p_item.get("amount", 0.0) or 0.0)],
                        "amount": float(p_item.get("amount", 0.0) or 0.0),
                        "method": p_item.get("method", "UPI"),
                        "status": p_item.get("status", "paid"),
                        "date": p_item.get("date"),
                        "source": p_item.get("source", "miniapp")
                    }
                else:
                    g = grouped_payments[group_key]
                    sid = p_item.get("story_id")
                    if sid and sid not in g["story_ids"]:
                        g["story_ids"].append(sid)
                        g["story_names"].append(p_item.get("story_name", sid))
                        g["stories"].append({
                            "story_id": sid,
                            "story_name": p_item.get("story_name", sid)
                        })
                    curr_amt = float(p_item.get("amount", 0.0) or 0.0)
                    g["raw_amounts"].append(curr_amt)

            final_payments = []
            for group_key, g in grouped_payments.items():
                raw_amts = g.pop("raw_amounts", [g["amount"]])
                if raw_amts:
                    # Fix duplicate revenue: if all items in this grouped order share the same order total amount (e.g. [524.0, 524.0, 524.0, 524.0]),
                    # use the single 524.0. If distinct item prices were stored, sum them up.
                    if all(a == raw_amts[0] for a in raw_amts):
                        g["amount"] = raw_amts[0]
                    else:
                        g["amount"] = sum(raw_amts)

                if len(g["story_names"]) > 1:
                    g["story_name"] = f"{', '.join(g['story_names'][:2])} (+{len(g['story_names']) - 2} more)" if len(g['story_names']) > 3 else ", ".join(g["story_names"])
                elif len(g["story_names"]) == 1:
                    g["story_name"] = g["story_names"][0]
                else:
                    g["story_name"] = "Store Purchase"
                final_payments.append(g)

            # ── Smart Payment Deduplication Pass ──
            # Fixes duplicate entries in Admin Panel (e.g. 'UPI_MANUAL' + 'UPI' or cross-collection duplicates).
            # Groups payments by (story_part_signature) per user and keeps 1 clean canonical payment record!
            dedup_payments = {}
            specific_gateways = ("CASHFREE", "CASHFREE_UPI", "UPI_MANUAL", "UPI_MANUAL_MINIAPP", "RAZORPAY", "OXAPAY", "DODO_PAYMENTS", "PAYU", "PAYTM")

            for p_item in final_payments:
                p_date = str(p_item.get("date") or "")[:10]  # YYYY-MM-DD
                p_status = str(p_item.get("status", "")).lower()
                p_ref = str(p_item.get("reference") or "").strip().upper()
                p_oid = str(p_item.get("order_id") or "").strip().upper()

                # Calculate canonical story + part signature for single & multi-story cart orders
                parts = []
                if p_item.get("items") and isinstance(p_item["items"], list):
                    for itm in p_item["items"]:
                        raw_sid = str(itm.get("story_id") or itm.get("id") or "")
                        can_sid = sid_to_canonical.get(raw_sid, raw_sid)
                        p_id = str(itm.get("part_id") or "FULL").strip()
                        parts.append(f"{can_sid}::{p_id}")
                elif p_item.get("part_id"):
                    raw_sid = str(p_item.get("story_id") or "")
                    can_sid = sid_to_canonical.get(raw_sid, raw_sid)
                    parts.append(f"{can_sid}::{p_item['part_id']}")
                else:
                    s_ids = p_item.get("story_ids", [])
                    if not s_ids and p_item.get("story_id"):
                        s_ids = [p_item.get("story_id")]
                    for sid in s_ids:
                        can_sid = sid_to_canonical.get(str(sid), str(sid))
                        parts.append(f"{can_sid}::FULL")

                s_signature = "-".join(sorted(parts)) if parts else str(p_item.get("story_name") or "story").strip().lower()

                # Deduplication key:
                # 1. Primary: Transaction reference (UTR / payment_id) if valid (len >= 6).
                # 2. Secondary: If genuine external gateway order ID (starts with CF-, ORD-, PAY-, RAZOR-)
                # 3. Default: Group by exact story & parts signature for this user!
                if p_ref and len(p_ref) >= 6 and not p_ref.startswith("UID_") and not p_ref.startswith("SINGLE_"):
                    uniq_key = f"ref_{p_ref}"
                elif p_oid and any(p_oid.startswith(prefix) for prefix in ("CF-", "ORD-", "PAY-", "RAZOR-")):
                    uniq_key = f"oid_{p_oid}"
                else:
                    uniq_key = f"story_{s_signature}"

                if uniq_key not in dedup_payments:
                    dedup_payments[uniq_key] = p_item
                else:
                    existing = dedup_payments[uniq_key]
                    e_status = str(existing.get("status", "")).lower()

                    # 1. If existing is NOT paid, but new p_item IS paid, replace with paid!
                    if p_status in ("paid", "approved", "delivered", "completed", "success") and e_status not in ("paid", "approved", "delivered", "completed", "success"):
                        dedup_payments[uniq_key] = p_item
                        continue

                    # 2. Prefer specific gateway method ("CASHFREE", "UPI_MANUAL") over generic "UPI"
                    m_existing = str(existing.get("method", "")).upper()
                    m_new = str(p_item.get("method", "")).upper()

                    if any(g in m_new for g in specific_gateways) and not any(g in m_existing for g in specific_gateways):
                        existing["method"] = m_new
                        if p_item.get("source"): existing["source"] = p_item["source"]

                    if p_item.get("reference") and not existing.get("reference"):
                        existing["reference"] = p_item["reference"]

            # ── Phantom Full-Story Filter ──
            # If a user purchased specific parts of a story (e.g. Shoorveer Part 1, Divine System Part 1/2),
            # any auto-created 'Full Story' audit duplicate for that same story is dropped so only their true part orders appear!
            part_purchased_canons = set()
            for p in list(dedup_payments.values()):
                s_ids = p.get("story_ids") or ([p.get("story_id")] if p.get("story_id") else [])
                has_part = bool(p.get("part_id")) or any(bool(itm.get("part_id")) for itm in p.get("items", [])) or ("(Part" in str(p.get("story_name", "")))
                if has_part:
                    for sid in s_ids:
                        can = sid_to_canonical.get(str(sid), str(sid))
                        part_purchased_canons.add(can)

            cleaned_payments = []
            for p in list(dedup_payments.values()):
                s_ids = p.get("story_ids") or ([p.get("story_id")] if p.get("story_id") else [])
                has_part = bool(p.get("part_id")) or any(bool(itm.get("part_id")) for itm in p.get("items", [])) or ("(Part" in str(p.get("story_name", "")))
                if not has_part and len(s_ids) == 1:
                    can = sid_to_canonical.get(str(s_ids[0]), str(s_ids[0]))
                    if can in part_purchased_canons:
                        continue
                cleaned_payments.append(p)

            final_payments = cleaned_payments
            data["payments"] = final_payments
            payments = final_payments

            paid_payments = [p for p in payments if p["status"] in ["paid", "approved", "delivered", "completed", "success"]]
            pending_payments = [p for p in payments if p["status"] in ["pending", "processing", "waiting_screenshot"]]

            # Determine buyer overall source accurately based on their actual payments
            sources_present = set(p.get("source", "miniapp") for p in payments)
            if "miniapp" in sources_present and "bot" in sources_present:
                data["source"] = "both"
            elif "bot" in sources_present:
                data["source"] = "bot"
            else:
                data["source"] = "miniapp"

            if paid_payments:
                user_status = "paid"
                user_amount = sum(p["amount"] for p in paid_payments)
                paid_user_ids.add(uid_str)
                total_paid_orders_count += len(paid_payments)
                for p in paid_payments:
                    total_rev += p["amount"]
                    if p["source"] == "bot": bot_rev += p["amount"]
                    else: miniapp_rev += p["amount"]
            elif pending_payments:
                user_status = pending_payments[0]["status"]
                user_amount = sum(p["amount"] for p in pending_payments)
            else:
                user_status = payments[0]["status"]
                user_amount = sum(p["amount"] for p in payments)

            buyers_list.append({
                "order_id": data["order_id"],
                "user_id": data["user_id"],
                "username": data["username"],
                "first_name": data["first_name"],
                "photo_url": data["photo_url"],
                "amount": user_amount,
                "status": user_status,
                "source": data["source"],
                "payments": sorted(payments, key=lambda x: str(x.get("date", "")), reverse=True),
                "date": data["date"],
                "joined_at": data.get("joined_at")
            })

        buyers_list.sort(key=lambda x: max([p["date"] for p in x["payments"]] if x["payments"] else [x["date"]]), reverse=True)

        res = {
            "buyers": buyers_list,
            "total_buyers_count": len(paid_user_ids),
            "total_orders_count": total_paid_orders_count,
            "total_revenue": total_rev,
            "miniapp_revenue": miniapp_rev,
            "bot_revenue": bot_rev
        }

        _processed_buyers_cache = res
        _processed_buyers_cache_time = now
        return res

@api_router.get("/admin/stats")
async def get_admin_stats(telegram_id: str, force: bool = Query(False)):
    """Fetches full admin analysis dashboard."""
    global _admin_stats_cache, _admin_stats_cache_time
    from AryaPremium.config import Config
    
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized as Admin")
            
        now = time.time()
        if not force and _admin_stats_cache and (now - _admin_stats_cache_time < ADMIN_STATS_CACHE_TTL):
            return {
                "success": True,
                "data": _admin_stats_cache
            }
            
        arya_db = app.state.db
        computed = await fetch_processed_buyers_data(arya_db)
        
        # Bot Users
        bot_users_count = await arya_db.db.users.count_documents({})
        
        page_views_count = 0
        miniapp_users_count = 0
        try:
            page_views_count = await arya_db.db.mini_app_analytics.count_documents({"type": "page_view"})
            miniapp_users_res = await arya_db.db.mini_app_analytics.aggregate([
                {"$match": {"type": "page_view"}},
                {"$group": {"_id": "$user_id"}},
                {"$count": "c"}
            ]).to_list(length=1)
            miniapp_users_count = miniapp_users_res[0]["c"] if miniapp_users_res else 0
        except Exception as analytics_err:
            logger.warning(f"Error fetching analytics count in stats: {analytics_err}")

        total_users_count = bot_users_count + miniapp_users_count
        total_stories = await arya_db.db.premium_stories.count_documents({})
        
        # Feedbacks
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
                "created_at": doc.get("created_at", datetime.now(timezone.utc)).isoformat() if isinstance(doc.get("created_at"), datetime) else str(doc.get("created_at", ""))
            })
            
        # Recent Orders
        orders = []
        ord_cursor = arya_db.db.orders.find({}).sort("created_at", -1).limit(10)
        async for doc in ord_cursor:
            user_doc = await arya_db.db.users.find_one({"id": doc.get("user_id")}) if doc.get("user_id") else None
            _fn = ((user_doc.get("first_name") if user_doc else "") or "").strip()
            _ln = ((user_doc.get("last_name") if user_doc else "") or "").strip()
            _full = " ".join(filter(None, [_fn, _ln])) or (user_doc.get("username") if user_doc else "") or "User"
            orders.append({
                "order_id": str(doc.get("order_id", doc.get("_id", ""))),
                "amount": doc.get("total_amount") or doc.get("total") or doc.get("amount", 0),
                "status": doc.get("status", "unknown"),
                "user_id": doc.get("user_id", ""),
                "first_name": _full,
                "username": user_doc.get("username", "") if user_doc else "",
                "story_names": doc.get("story_names", []),
                "source": doc.get("source", "miniapp"),
                "created_at": doc.get("created_at", datetime.now(timezone.utc)).isoformat() if isinstance(doc.get("created_at"), datetime) else str(doc.get("created_at", ""))
            })
            
        orders = sorted(orders, key=lambda x: x["created_at"], reverse=True)[:10]

        result_data = {
            "total_users": total_users_count,
            "bot_users": bot_users_count,
            "miniapp_users": miniapp_users_count,
            "page_views": page_views_count,
            "total_stories": total_stories,
            "total_orders": computed["total_orders_count"],
            "total_buyers": computed["total_buyers_count"],
            "total_revenue": computed["total_revenue"],
            "miniapp_revenue": computed["miniapp_revenue"],
            "bot_revenue": computed["bot_revenue"],
            "feedbacks": feedbacks,
            "orders": orders
        }
        
        _admin_stats_cache = result_data
        _admin_stats_cache_time = now
        
        return {
            "success": True,
            "data": result_data
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching admin stats: {e}")
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
            story_id = s.get("story_id") or _id_str
            
            # Normalize poster_url so it always exists and is accessible
            cover = s.get("poster_url") or s.get("image_url") or s.get("cover") or s.get("poster") or ""
            if "r2.cloudflarestorage.com" in cover:
                obj_key = cover.split("/")[-1]
                cover = f"/api/r2-image?key={obj_key}"
            elif not cover and s.get("image"):
                tg_img = str(s.get("image")).strip()
                if tg_img.startswith("http") or tg_img.startswith("/api/"):
                    cover = tg_img
                elif tg_img:
                    bot_id = s.get("bot_id")
                    cover = f"/api/tg-image?file_id={tg_img}" + (f"&bot_id={bot_id}" if bot_id else "")
                
            s["poster_url"] = cover
            s["status"] = s.get("status") or "available"

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
    status: Optional[str] = "Ongoing"
    visibility: Optional[str] = "available"
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

# ────────────────────────────────────────────────────────────────────────────────────────────────────
# POST /admin/story
# ────────────────────────────────────────────────────────────────────────────────────────────────────
async def optimize_and_upload_to_storage(img_bytes: bytes, width: int = None, height: int = None, format: str = "WEBP", quality: int = 80) -> str:
    import io
    import uuid
    import asyncio
    import aiohttp
    from PIL import Image
    from decouple import config
    
    r2_account_id = config("R2_ACCOUNT_ID", default="") or os.environ.get("R2_ACCOUNT_ID", "")
    r2_access_key = config("R2_ACCESS_KEY_ID", default="") or config("R2_ACCESS_KEY", default="") or os.environ.get("R2_ACCESS_KEY_ID", "")
    r2_secret_key = config("R2_SECRET_ACCESS_KEY", default="") or config("R2_SECRET_KEY", default="") or os.environ.get("R2_SECRET_ACCESS_KEY", "")
    r2_bucket = config("R2_BUCKET_NAME", default="") or config("R2_BUCKET", default="arya-images") or os.environ.get("R2_BUCKET_NAME", "arya-images")
    r2_domain = config("R2_CUSTOM_DOMAIN", default="") or config("R2_DOMAIN", default="") or os.environ.get("R2_CUSTOM_DOMAIN", "")

    def process_data(data):
        img = Image.open(io.BytesIO(data))
        if img.mode == "CMYK":
            img = img.convert("RGB")
        if width and height:
            img = img.resize((width, height), Image.Resampling.BILINEAR)
        elif width:
            img.thumbnail((width, width))
        output = io.BytesIO()
        img.save(output, format=format, quality=quality)
        return output.getvalue()

    processed_bytes = await asyncio.to_thread(process_data, img_bytes)
    
    ext = format.lower()
    filename = f"{uuid.uuid4().hex}.{ext}"

    # Always save a local copy to persistent uploads directory for 100% reliable serving
    try:
        local_filepath = os.path.join(UPLOADS_DIR, filename)
        with open(local_filepath, "wb") as f:
            f.write(processed_bytes)
    except Exception as e:
        logger.warning(f"Failed to save local upload file: {e}")

    url = ""
    # If R2 is configured AND has a public custom domain, upload to Cloudflare R2
    if r2_account_id and r2_access_key and r2_secret_key and r2_bucket and r2_domain:
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
                content_type = f"image/{ext if ext != 'jpg' else 'jpeg'}"
                s3.put_object(
                    Bucket=r2_bucket,
                    Key=filename,
                    Body=processed_bytes,
                    ContentType=content_type
                )
                domain = r2_domain.strip("/")
                if not domain.startswith("http"):
                    domain = "https://" + domain
                return f"{domain}/{filename}"
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
                content_type = f"image/{ext if ext != 'jpg' else 'jpeg'}"
                form.add_field("fileToUpload", processed_bytes, filename=f"image.{ext}", content_type=content_type)
                async with session.post("https://catbox.moe/user/api.php", data=form, timeout=10) as resp:
                    if resp.status == 200:
                        url = (await resp.text()).strip()
        except Exception as e:
            logger.error(f"Catbox upload failed: {e}")
            url = ""

    # Guaranteed fallback to local persistent uploads endpoint
    if not url:
        url = f"/api/uploads/{filename}"
            
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
        
        # Remove non-DB fields but preserve _id for update matching
        show_in_banners = data.get("show_in_banners")
        save_doc = {k: v for k, v in data.items() if k not in ("telegram_id", "show_in_banners")}
        
        # Ensure story_id exists
        if not save_doc.get("story_id"):
            save_doc["story_id"] = str(data.get("_id") or data.get("id") or f"story_{int(time.time()*1000)}")
        
        poster_url = str(save_doc.get("poster_url") or "").strip()
        banner_url = str(save_doc.get("banner_url") or "").strip()

        # Sync all poster/cover fields across MongoDB schemas
        if poster_url:
            save_doc["poster_url"] = poster_url
            save_doc["image_url"] = poster_url
            save_doc["cover"] = poster_url
            save_doc["poster"] = poster_url
            if poster_url.startswith("http") or poster_url.startswith("/api/"):
                save_doc["image"] = ""
        if banner_url:
            save_doc["banner_url"] = banner_url
            save_doc["banner"] = banner_url
        
        # Determine if we should generate the outpainted banner
        should_outpaint = False
        if poster_url and poster_url.startswith("http") and (not banner_url or banner_url == poster_url):
            should_outpaint = True
                
        if should_outpaint:
            try:
                logger.info(f"Auto-outpainting banner for story: {save_doc.get('story_name_en') or save_doc.get('story_id')}")
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(poster_url, timeout=12) as resp:
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
                                save_doc["banner"] = uploaded_banner_url
                                logger.info(f"Successfully auto-generated and uploaded banner: {uploaded_banner_url}")
            except Exception as e:
                logger.error(f"Failed to auto-outpaint story banner: {e}", exc_info=True)

        save_doc["updated_via"] = "mini_app_admin"

        # ── Validate completion status ─────────────────────────────────────────
        _valid_statuses = ("Ongoing", "Completed", "Unfinished", "Stucked")
        raw_st = str(save_doc.get("status") or "").strip()
        if raw_st not in _valid_statuses:
            is_comp = bool(save_doc.get("is_completed") or raw_st.lower() == "completed")
            save_doc["status"] = "Completed" if is_comp else "Ongoing"
        # ──────────────────────────────────────────────────────────────────────

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
        # Clear /stories cache
        global _stories_cache
        _stories_cache = None
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
        # Clear /stories cache
        global _stories_cache
        _stories_cache = None
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


@api_router.post("/admin/clean-fragmented-shows")
async def clean_fragmented_shows_endpoint(payload: dict = None):
    """
    Purges standalone fragmented single-episode records created for Kuku TV / Story TV OTT shows,
    preserving consolidated multi-episode show records.
    """
    try:
        payload = payload or {}
        telegram_id = str(payload.get("telegram_id", ""))
        if telegram_id and not is_admin(telegram_id):
            raise HTTPException(status_code=403, detail="Not authorized as Admin")

        arya_db = app.state.db
        query = {
            "$and": [
                {
                    "$or": [
                        {"platform": {"$regex": "^(kuku tv|story tv)$", "$options": "i"}},
                        {"is_show": True}
                    ]
                },
                {
                    "$or": [
                        {"parts": {"$size": 0}},
                        {"parts": {"$size": 1}},
                        {"parts": {"$exists": False}},
                        {"file_count": {"$lte": 1}}
                    ]
                }
            ]
        }

        candidates = await arya_db.stories.find(query, {"title": 1, "story_name_en": 1, "platform": 1, "parts": 1}).to_list(length=500)
        del_result = await arya_db.stories.delete_many(query)

        global _stories_cache
        _stories_cache = None

        logger.info(f"[CleanFragmented] Purged {del_result.deleted_count} fragmented show records.")
        return {
            "success": True,
            "deleted_count": del_result.deleted_count,
            "purged_records": [{"id": str(d.get("_id")), "title": d.get("title") or d.get("story_name_en"), "platform": d.get("platform")} for d in candidates]
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error cleaning fragmented shows: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ────────────────────────────────────────────────────────────────────────────────────────────────────
# UPLOAD ADMIN IMAGE (POST /admin/upload-image)
# ────────────────────────────────────────────────────────────────────────────────────────────────────
@api_router.post("/admin/upload-image")
async def upload_admin_image(telegram_id: str = Form(...), file: UploadFile = File(...)):
    from AryaPremium.config import Config
    import aiohttp
    import io
    from PIL import Image

    if not is_admin(str(telegram_id)):
        raise HTTPException(status_code=403, detail="Not authorized")

    try:
        contents = await file.read()
        poster_url = await optimize_and_upload_to_storage(contents, width=1200, format="WEBP", quality=85)
        
        return {"success": True, "poster_url": poster_url, "file_id": ""}
    except Exception as e:
        logger.error(f"Image upload error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# DELETE /admin/story/{story_id}
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@api_router.delete("/admin/story/{story_id}")
async def delete_admin_story(story_id: str, telegram_id: str):
    """Deletes a story permanently from database and clears all caches."""
    try:
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
            
        arya_db = app.state.db
        sid_str = str(story_id).strip()
        from bson.objectid import ObjectId

        filters = [{"story_id": sid_str}, {"id": sid_str}, {"_id": sid_str}]
        try:
            if ObjectId.is_valid(sid_str):
                filters.append({"_id": ObjectId(sid_str)})
        except Exception:
            pass

        # 1. Permanently delete from database collections
        for flt in filters:
            await arya_db.db.premium_stories.delete_many(flt)
            await arya_db.db.stories.delete_many(flt)
            await arya_db.db.episodes.delete_many(flt)

        # 2. Invalidate all in-memory caches
        global _stories_cache, _stories_cache_time
        _stories_cache = None
        _stories_cache_time = 0.0
        invalidate_buyers_cache()

        logger.info(f"✅ Permanently deleted story '{story_id}' from database")
        return {"success": True, "message": "Story deleted permanently from database"}
    except Exception as e:
        logger.error(f"Failed to delete story {story_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@api_router.api_route("/admin/stories/sync-ongoing", methods=["GET", "POST"])
async def sync_ongoing_stories_endpoint(telegram_id: str = "0"):
    """Instantly scans all ongoing stories across connected channels and synchronizes new episodes and ongoing parts."""
    try:
        if telegram_id != "0" and not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")

        from plugins.premium_live_monitor import check_and_update_all_ongoing_stories
        arya_db = app.state.db
        mgmt_bot = getattr(arya_db, "mgmt_client", None)
        
        # Run sync in background or immediately
        asyncio.create_task(check_and_update_all_ongoing_stories(mgmt_bot))
        return {"status": "success", "message": "Ongoing stories sync initiated successfully in background"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error initiating ongoing stories sync: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# SUPPORT MANAGEMENT
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
async def send_admin_support_notification_bg(
    telegram_id: str,
    type_str: str,
    message: str,
    first_name: str,
    username: str,
    file_bytes: Optional[bytes] = None,
    file_name: Optional[str] = None,
    file_content_type: Optional[str] = None
):
    """Asynchronously notifies owners/admins of new support submissions in the background."""
    from AryaPremium.config import Config
    import aiohttp
    
    try:
        escaped_first_name = escape_html(first_name or "Mini App User")
        escaped_username = f"@{escape_html(username)}" if username else "—"
        escaped_message = escape_html(message)
        
        if type_str == "request":
            admin_txt = (
                f"🛎️ <b>New Story Request from Mini App</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>User:</b> {escaped_first_name}\n"
                f"<b>Username:</b> {escaped_username}\n"
                f"<b>User ID:</b> <code>{telegram_id}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>Request:</b>\n"
                f"<blockquote>{escaped_message[:800]}</blockquote>\n"
                f"<i>Manage from Admin Panel → Requests tab or Bot → STORY REQUESTS</i>"
            )
        elif type_str == "chat":
            admin_txt = (
                f"💬 <b>New Live Chat Message from Mini App</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>User:</b> {escaped_first_name}\n"
                f"<b>Username:</b> {escaped_username}\n"
                f"<b>User ID:</b> <code>{telegram_id}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>Message:</b>\n"
                f"<blockquote>{escaped_message[:800]}</blockquote>\n"
                f"<i>Reply from Admin Panel → Live Chat</i>"
            )
        else:
            admin_txt = (
                f"💬 <b>New {type_str.title()} from Mini App</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>User:</b> {escaped_first_name}\n"
                f"<b>Username:</b> {escaped_username}\n"
                f"<b>User ID:</b> <code>{telegram_id}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>Message:</b>\n"
                f"<blockquote>{escaped_message[:800]}</blockquote>"
            )
        
        token = Config.MGMT_BOT_TOKEN
        if token and Config.OWNER_IDS:
            async with aiohttp.ClientSession() as session:
                for oid in Config.OWNER_IDS:
                    try:
                        if file_bytes:
                            form = aiohttp.FormData()
                            form.add_field('chat_id', str(oid))
                            form.add_field('caption', admin_txt)
                            form.add_field('parse_mode', 'HTML')
                            method = "sendDocument"
                            field_name = "document"
                            if file_content_type:
                                if file_content_type.startswith("image/"):
                                    method = "sendPhoto"; field_name = "photo"
                                elif file_content_type.startswith("video/"):
                                    method = "sendVideo"; field_name = "video"
                                elif file_content_type.startswith("audio/"):
                                    method = "sendAudio"; field_name = "audio"
                            form.add_field(field_name, file_bytes, filename=file_name or "file")
                            await session.post(f"https://api.telegram.org/bot{token}/{method}", data=form, timeout=60)
                        else:
                            await session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                                "chat_id": oid, "text": admin_txt, "parse_mode": "HTML"
                            }, timeout=10)
                    except Exception as e:
                        logger.warning(f"Failed to notify admin {oid}: {e}")
    except Exception as notify_err:
        logger.error(f"Failed to process or send admin Telegram notification: {notify_err}")


@api_router.post("/support/upload-file")
async def support_upload_file(file: UploadFile = File(...)):
    """Uploads any file (image, pdf, audio) for live chat support."""
    try:
        contents = await file.read()
        file_url = None
        if file.content_type and file.content_type.startswith("image/"):
            try:
                file_url = await optimize_and_upload_to_storage(contents)
            except Exception:
                pass
        
        if not file_url:
            file_url = await upload_file_to_storage(contents, file.filename or "file", file.content_type or "application/octet-stream")
            
        return {"success": True, "url": file_url}
    except Exception as e:
        logger.error(f"Failed to upload file: {e}")
        raise HTTPException(status_code=500, detail="Failed to upload file")


@api_router.get("/support/chat")
async def get_support_chat(telegram_id: str):
    """Gets the active support chat ticket for a user, or creates one if none exists."""
    from datetime import datetime, timezone
    import asyncio
    arya_db = app.state.db
    uid = int(telegram_id) if telegram_id.isdigit() else telegram_id
    uid_str = str(uid)
    
    # ── Run ticket lookup and user_doc lookup in PARALLEL ──
    # (user_doc is only needed if ticket doesn't exist, but the lookup is fast)
    ticket_query = arya_db.db.premium_feedback.find_one(
        {"user_id": {"$in": [uid, uid_str]}, "category": "Live Chat"},
        sort=[("created_at", -1)]
    )
    user_query = arya_db.db.users.find_one(
        {"id": uid} if isinstance(uid, int) else {"username": uid},
        # Only fetch the fields we need
        {"first_name": 1, "username": 1, "_id": 0}
    )
    ticket, user_doc = await asyncio.gather(ticket_query, user_query)
    
    if not ticket:
        # DO NOT auto-create support tickets for banned or blocked users!
        if await check_user_is_banned(arya_db, uid):
            return {
                "ticket_id": "banned",
                "status": "Closed",
                "chat_language": "en",
                "agent_typing": False,
                "user_typing": False,
                "unread": 0,
                "messages": []
            }

        # Create a new active chat ticket with a default welcome message from the agent
        ticket_doc = {
            "user_id": uid,
            "bot_id": "mini_app",
            "type": "text",
            "text": "Live Chat Support",
            "subject": "Live Chat Support",
            "category": "Live Chat",
            "priority": "Normal",
            "status": "Open",
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "source": "mini_app",
            "unread": 0,
            "messages": []
        }
        
        # Pre-populate user details (already fetched in parallel above)
        if user_doc:
            ticket_doc["user_name"] = user_doc.get("first_name") or user_doc.get("username") or "User"
            ticket_doc["username"] = user_doc.get("username") or ""
        
        result = await arya_db.db.premium_feedback.insert_one(ticket_doc)
        # Use ticket_doc directly — avoids extra find_one round trip
        ticket_doc["_id"] = result.inserted_id
        ticket = ticket_doc

    # Reset unread counter as background task (non-blocking)
    if ticket.get("unread", 0) > 0:
        async def _reset_unread():
            try:
                await arya_db.db.premium_feedback.update_one(
                    {"_id": ticket["_id"]},
                    {"$set": {"unread": 0}}
                )
            except Exception:
                pass
        asyncio.create_task(_reset_unread())

    # Determine if agent is typing
    now = datetime.now(timezone.utc)
    agent_typing = False
    agent_typing_until = ticket.get("agent_typing_until")
    if agent_typing_until:
        if agent_typing_until.tzinfo is None:
            agent_typing_until = agent_typing_until.replace(tzinfo=timezone.utc)
        if agent_typing_until > now:
            agent_typing = True

    messages = ticket.get("messages", [])
    return {
        "success": True,
        "ticket_id": str(ticket["_id"]),
        "status": ticket.get("status", "Open"),
        "messages": messages,
        "language": ticket.get("chat_language", ""),
        "agent_typing": agent_typing
    }


class ChatMessagePayload(BaseModel):
    telegram_id: str
    message: Optional[str] = ""
    selected_language: Optional[str] = None
    file_url: Optional[str] = None
    file_type: Optional[str] = None

@api_router.post("/support/chat/send")
async def send_support_chat_message(payload: ChatMessagePayload):
    """User sends a message or updates language in the live chat."""
    from datetime import datetime, timezone
    from bson.objectid import ObjectId
    arya_db = app.state.db
    uid = int(payload.telegram_id) if payload.telegram_id.isdigit() else payload.telegram_id
    uid_str = str(uid)
    
    # Use $in to match both int and string stored user_id values
    ticket = await arya_db.db.premium_feedback.find_one(
        {"user_id": {"$in": [uid, uid_str]}, "category": "Live Chat"},
        sort=[("created_at", -1)]
    )
    
    # If the most recent ticket is closed, start a new active session or reopen it
    if ticket and ticket.get("status", "Open") in ["closed", "Closed", "resolved", "Resolved"]:
        ticket = None
        
    is_new_chat = (not ticket) or len(ticket.get("messages", [])) == 0
    
    msg_id = f"u-{int(time.time() * 1000)}"
    msg_at = datetime.now(timezone.utc).strftime("%I:%M %p")
    
    new_msg = None
    message_str = payload.message or ""
    
    if message_str.strip() or payload.file_url:
        new_msg = {
            "id": msg_id,
            "from": "user",
            "body": message_str or "Attachment",
            "at": msg_at
        }
        if payload.file_url:
            new_msg["file_url"] = payload.file_url
            new_msg["file_type"] = payload.file_type or "application/octet-stream"
        
    update_fields = {
        "updated_at": datetime.now(timezone.utc)
    }
    if message_str.strip():
        update_fields["text"] = message_str
    elif payload.file_url:
        update_fields["text"] = "Shared a file"
    if payload.selected_language:
        update_fields["chat_language"] = payload.selected_language
        
    if ticket:
        update_query = {"$set": update_fields}
        if new_msg:
            update_query["$push"] = {"messages": new_msg}
            update_query["$inc"] = {"unread": 1}
            
        await arya_db.db.premium_feedback.update_one(
            {"_id": ticket["_id"]},
            update_query
        )
        ticket_id = str(ticket["_id"])
    else:
        # Create a new ticket if none exists at all
        ticket_doc = {
            "user_id": uid,
            "bot_id": "mini_app",
            "type": "text",
            "text": message_str or ("Shared a file" if payload.file_url else "Live Chat Support"),
            "subject": (message_str[:40] if len(message_str) > 40 else message_str) if message_str else "Live Chat Support",
            "category": "Live Chat",
            "priority": "Normal",
            "status": "Open",
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "source": "mini_app",
            "unread": 1 if new_msg else 0,
            "messages": [new_msg] if new_msg else []
        }
        if payload.selected_language:
            ticket_doc["chat_language"] = payload.selected_language
            
        user_doc = await arya_db.db.users.find_one({"id": uid} if isinstance(uid, int) else {"username": uid})
        if user_doc:
            ticket_doc["user_name"] = user_doc.get("first_name") or user_doc.get("username") or "User"
            ticket_doc["username"] = user_doc.get("username") or ""
            
        result = await arya_db.db.premium_feedback.insert_one(ticket_doc)
        ticket_id = str(result.inserted_id)
        
    # Trigger background admin notification ONLY if it is the first time user sends a query
    if is_new_chat and message_str.strip():
        first_name = "User"
        username = ""
        user_doc = await arya_db.db.users.find_one({"id": uid} if isinstance(uid, int) else {"username": uid})
        if user_doc:
            first_name = user_doc.get("first_name") or "User"
            username = user_doc.get("username") or ""
            
        asyncio.create_task(
            send_admin_support_notification_bg(
                telegram_id=payload.telegram_id,
                type_str="chat",
                message=message_str,
                first_name=first_name,
                username=username
            )
        )
        
    updated_ticket = await arya_db.db.premium_feedback.find_one({"_id": ObjectId(ticket_id)})
    return {
        "success": True,
        "ticket_id": ticket_id,
        "status": updated_ticket.get("status", "Open"),
        "messages": updated_ticket.get("messages", []),
        "language": updated_ticket.get("chat_language", "")
    }


class NewChatPayload(BaseModel):
    telegram_id: str
    selected_language: Optional[str] = None

@api_router.post("/support/chat/new")
async def create_new_support_chat(payload: NewChatPayload):
    """User starts a brand new support chat session by closing old ones."""
    from datetime import datetime, timezone
    arya_db = app.state.db
    uid = int(payload.telegram_id) if payload.telegram_id.isdigit() else payload.telegram_id
    uid_str = str(uid)
    
    # Archive/Close all previous live chat tickets for this user (match both int and string user_id)
    await arya_db.db.premium_feedback.update_many(
        {"user_id": {"$in": [uid, uid_str]}, "category": "Live Chat", "status": {"$ne": "Closed"}},
        {"$set": {"status": "Closed", "updated_at": datetime.now(timezone.utc)}}
    )
    
    # Create new active ticket
    ticket_doc = {
        "user_id": uid,
        "bot_id": "mini_app",
        "type": "text",
        "text": "Live Chat Support",
        "subject": "Live Chat Support",
        "category": "Live Chat",
        "priority": "Normal",
        "status": "Open",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "source": "mini_app",
        "unread": 0,
        "messages": []
    }
    if payload.selected_language:
        ticket_doc["chat_language"] = payload.selected_language
        
    user_doc = await arya_db.db.users.find_one({"id": uid} if isinstance(uid, int) else {"username": uid})
    if user_doc:
        ticket_doc["user_name"] = user_doc.get("first_name") or user_doc.get("username") or "User"
        ticket_doc["username"] = user_doc.get("username") or ""
        
    result = await arya_db.db.premium_feedback.insert_one(ticket_doc)
    ticket_id = str(result.inserted_id)
    
    return {
        "success": True,
        "ticket_id": ticket_id,
        "status": "Open",
        "messages": [],
        "language": payload.selected_language or ""
    }


class CloseChatPayload(BaseModel):
    telegram_id: Optional[str] = None
    ticket_id: Optional[str] = None

@api_router.post("/support/chat/close")
async def close_support_chat(payload: CloseChatPayload):
    """Closes the specified support chat session by ticket_id or telegram_id."""
    from datetime import datetime, timezone
    from bson.objectid import ObjectId
    arya_db = app.state.db
    
    query = {}
    if payload.ticket_id and len(payload.ticket_id) == 24:
        try:
            query = {"_id": ObjectId(payload.ticket_id)}
        except Exception:
            query = {"ticket_id": payload.ticket_id}
    elif payload.telegram_id:
        uid_int = int(payload.telegram_id) if payload.telegram_id.isdigit() else payload.telegram_id
        query = {
            "user_id": {"$in": [uid_int, str(uid_int)]},
            "category": {"$in": ["Live Chat", "live_chat"]},
            "status": {"$ne": "Closed"}
        }
    else:
        return {"success": False, "message": "Missing ticket_id or telegram_id"}
        
    res = await arya_db.db.premium_feedback.update_many(
        query,
        {"$set": {"status": "Closed", "updated_at": datetime.now(timezone.utc)}}
    )
    
    return {
        "success": True,
        "status": "Closed",
        "modified": res.modified_count
    }


class TypingPayload(BaseModel):
    telegram_id: str
    ticket_id: str = ""
    is_typing: bool
    from_agent: bool = False

@api_router.post("/support/chat/typing")
async def support_chat_typing(payload: TypingPayload):
    """Updates typing state for user or agent in live chat."""
    from datetime import datetime, timezone, timedelta
    from bson.objectid import ObjectId
    arya_db = app.state.db
    
    expiry = datetime.now(timezone.utc) + timedelta(seconds=4) if payload.is_typing else None
    update_field = "agent_typing_until" if payload.from_agent else "user_typing_until"
    
    query = {}
    if payload.ticket_id and len(payload.ticket_id) == 24:
        query["_id"] = ObjectId(payload.ticket_id)
    else:
        uid_int = int(payload.telegram_id) if payload.telegram_id.isdigit() else payload.telegram_id
        query["$or"] = [{"user_id": uid_int}, {"user_id": str(uid_int)}]
        
    await arya_db.db.premium_feedback.update_many(
        query,
        {"$set": {update_field: expiry}}
    )
    return {"success": True}


class TicketStatusPayload(BaseModel):
    telegram_id: str
    ticket_id: str
    status: str

@api_router.post("/admin/support/status")
async def update_support_status(payload: TicketStatusPayload):
    """Admin updates ticket status."""
    if not is_admin(str(payload.telegram_id)):
        raise HTTPException(status_code=403, detail="Not authorized")
        
    arya_db = app.state.db
    from bson.objectid import ObjectId
    from datetime import datetime, timezone
    
    status_val = payload.status  # "Open", "Waiting User", "Closed", etc.
    
    query = {}
    if len(payload.ticket_id) == 24:
        try:
            query = {"$or": [{"_id": ObjectId(payload.ticket_id)}, {"ticket_id": payload.ticket_id}]}
        except Exception:
            query = {"ticket_id": payload.ticket_id}
    else:
        query = {"ticket_id": payload.ticket_id}

    update_payload = {"status": status_val, "updated_at": datetime.now(timezone.utc)}
    # Clear polling flag when ticket is explicitly resolved/closed
    if status_val.lower() in ("resolved", "closed"):
        update_payload["user_has_new_reply"] = False

    await arya_db.db.premium_feedback.update_many(
        query,
        {"$set": update_payload}
    )
    return {"success": True}


class BulkTicketStatusPayload(BaseModel):
    telegram_id: str
    ticket_ids: List[str]
    status: str

@api_router.post("/admin/support/bulk-status")
async def bulk_update_support_status(payload: BulkTicketStatusPayload):
    """Admin bulk updates ticket status for multiple selected support tickets."""
    if not is_admin(str(payload.telegram_id)):
        raise HTTPException(status_code=403, detail="Not authorized")
        
    if not payload.ticket_ids:
        return {"success": True, "modified": 0}

    arya_db = app.state.db
    from bson.objectid import ObjectId
    from datetime import datetime, timezone
    
    obj_ids = []
    str_ids = []
    for tid in payload.ticket_ids:
        tid_s = str(tid).strip()
        if not tid_s: continue
        str_ids.append(tid_s)
        if len(tid_s) == 24:
            try:
                obj_ids.append(ObjectId(tid_s))
            except Exception:
                pass

    query = {"$or": [{"_id": {"$in": obj_ids}}, {"ticket_id": {"$in": str_ids}}]}
    update_payload = {"status": payload.status, "updated_at": datetime.now(timezone.utc)}
    if payload.status.lower() in ("resolved", "closed"):
        update_payload["user_has_new_reply"] = False

    res = await arya_db.db.premium_feedback.update_many(query, {"$set": update_payload})
    return {"success": True, "modified": res.modified_count}


class BulkTicketDeletePayload(BaseModel):
    telegram_id: str
    ticket_ids: List[str]

@api_router.post("/admin/support/bulk-delete")
async def bulk_delete_support_tickets(payload: BulkTicketDeletePayload):
    """Admin bulk deletes multiple selected support tickets."""
    if not is_admin(str(payload.telegram_id)):
        raise HTTPException(status_code=403, detail="Not authorized")
        
    if not payload.ticket_ids:
        return {"success": True, "deleted": 0}

    arya_db = app.state.db
    from bson.objectid import ObjectId
    
    obj_ids = []
    str_ids = []
    for tid in payload.ticket_ids:
        tid_s = str(tid).strip()
        if not tid_s: continue
        str_ids.append(tid_s)
        if len(tid_s) == 24:
            try:
                obj_ids.append(ObjectId(tid_s))
            except Exception:
                pass

    query = {"$or": [{"_id": {"$in": obj_ids}}, {"ticket_id": {"$in": str_ids}}]}
    res = await arya_db.db.premium_feedback.delete_many(query)
    return {"success": True, "deleted": res.deleted_count}


class TicketNotesPayload(BaseModel):
    telegram_id: str
    ticket_id: str
    notes: str

@api_router.post("/admin/support/notes")
async def update_support_notes(payload: TicketNotesPayload):
    """Admin updates ticket internal notes."""
    if not is_admin(str(payload.telegram_id)):
        raise HTTPException(status_code=403, detail="Not authorized")
        
    arya_db = app.state.db
    from bson.objectid import ObjectId
    from datetime import datetime, timezone
    
    query = {}
    if len(payload.ticket_id) == 24:
        try:
            query = {"$or": [{"_id": ObjectId(payload.ticket_id)}, {"ticket_id": payload.ticket_id}]}
        except Exception:
            query = {"ticket_id": payload.ticket_id}
    else:
        query = {"ticket_id": payload.ticket_id}

    await arya_db.db.premium_feedback.update_many(
        query,
        {"$set": {"notes": payload.notes, "updated_at": datetime.now(timezone.utc)}}
    )
    return {"success": True}


class TicketUpdatePayload(BaseModel):
    telegram_id: str
    ticket_id: str
    priority: Optional[str] = None
    category: Optional[str] = None
    notes: Optional[str] = None
    agent: Optional[str] = None
    tags: Optional[list] = None
    status: Optional[str] = None

@api_router.post("/admin/support/update")
async def update_support_fields(payload: TicketUpdatePayload):
    """Admin updates any fields on a support ticket."""
    if not is_admin(str(payload.telegram_id)):
        raise HTTPException(status_code=403, detail="Not authorized")
        
    arya_db = app.state.db
    from bson.objectid import ObjectId
    from datetime import datetime, timezone
    
    update_data = {}
    if payload.priority is not None:
        update_data["priority"] = payload.priority
    if payload.category is not None:
        update_data["category"] = payload.category
    if payload.notes is not None:
        update_data["notes"] = payload.notes
    if payload.agent is not None:
        update_data["agent"] = payload.agent
    if payload.tags is not None:
        update_data["tags"] = payload.tags
    if payload.status is not None:
        update_data["status"] = payload.status
        
    if not update_data:
        return {"success": True, "message": "No fields to update"}
        
    update_data["updated_at"] = datetime.now(timezone.utc)
    
    query = {}
    if len(payload.ticket_id) == 24:
        try:
            query = {"$or": [{"_id": ObjectId(payload.ticket_id)}, {"ticket_id": payload.ticket_id}]}
        except Exception:
            query = {"ticket_id": payload.ticket_id}
    else:
        query = {"ticket_id": payload.ticket_id}

    await arya_db.db.premium_feedback.update_many(
        query,
        {"$set": update_data}
    )
    return {"success": True}


@api_router.get("/admin/support")
async def get_admin_support(request: Request, telegram_id: str):
    from AryaPremium.config import Config
    from bson.objectid import ObjectId
    from datetime import datetime, timezone, timedelta
    import asyncio
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
            
        arya_db = app.state.db
        query_filter = {
            "$and": [
                {
                    "$or": [
                        {"category": {"$exists": False}},
                        {"category": None},
                        {"category": {"$nin": [
                            "request", "Request", "REQUEST", 
                            "feedback", "Feedback", "FEEDBACK", 
                            "security", "Security", "SECURITY", 
                            "story-request", "story_request", "storyrequest"
                        ]}}
                    ]
                },
                {
                    "$or": [
                        {"text": {"$exists": False}},
                        {"text": None},
                        {"text": {"$not": {"$regex": "^\\[(REQUEST|FEEDBACK|SECURITY)\\]", "$options": "i"}}}
                    ]
                }
            ]
        }

        # Limit to 120 tickets for top list performance
        cursor = arya_db.db.premium_feedback.find(query_filter).sort("updated_at", -1).limit(120)
        tickets_docs = await cursor.to_list(length=None)
        
        # 1. Collect all user IDs
        user_ids = []
        for doc in tickets_docs:
            uid = doc.get("user_id")
            if uid is not None:
                user_ids.append(uid)
                if str(uid).isdigit():
                    user_ids.append(int(uid))
                    user_ids.append(str(uid))
        
        unique_user_ids = list(set(user_ids))
        
        # 2. Fetch users, orders, and analytics concurrently
        async def fetch_users():
            try:
                return await arya_db.db.users.find({"id": {"$in": unique_user_ids}}).to_list(length=None)
            except Exception as e:
                logger.warning(f"Error batch fetching users: {e}")
                return []

        async def fetch_orders():
            try:
                return await arya_db.db.orders.find({
                    "user_id": {"$in": unique_user_ids},
                    "status": {"$in": ["paid", "delivered"]}
                }).to_list(length=None)
            except Exception as e:
                logger.warning(f"Error batch fetching orders: {e}")
                return []

        async def fetch_analytics():
            try:
                events = await arya_db.db.mini_app_analytics.find(
                    {"user_id": {"$in": unique_user_ids}}
                ).sort("_id", -1).limit(300).to_list(length=300)
                res_map = {}
                for ev in events:
                    uid = ev.get("user_id")
                    if uid is not None and uid not in res_map:
                        res_map[uid] = ev
                return [(uid, res_map.get(uid)) for uid in unique_user_ids]
            except Exception as e:
                logger.warning(f"Error batch fetching analytics: {e}")
                return []

        if unique_user_ids:
            user_docs, orders_docs, analytics_results = await asyncio.gather(
                fetch_users(),
                fetch_orders(),
                fetch_analytics()
            )
        else:
            user_docs, orders_docs, analytics_results = [], [], []
        
        # 3. Create mapping dictionaries for fast lookup
        user_map = {}
        for u in user_docs:
            uid = u.get("id")
            if uid is not None:
                user_map[uid] = u
                user_map[str(uid)] = u
                if str(uid).isdigit():
                    user_map[int(uid)] = u
                    
        orders_by_user = {}
        for o in orders_docs:
            uid = o.get("user_id")
            if uid is not None:
                orders_by_user.setdefault(uid, []).append(o)
                if str(uid).isdigit():
                    orders_by_user.setdefault(int(uid), []).append(o)
                    orders_by_user.setdefault(str(uid), []).append(o)
                    
        analytics_map = {}
        for uid, ev in analytics_results:
            if ev:
                analytics_map[uid] = ev
                if str(uid).isdigit():
                    analytics_map[int(uid)] = ev
                    analytics_map[str(uid)] = ev

        tickets = []
        for doc in tickets_docs:
            created_dt = doc.get("created_at", datetime.now(timezone.utc))
            updated_dt = doc.get("updated_at", created_dt)
            
            created_str = created_dt.strftime("%d/%m/%Y, %H:%M") if isinstance(created_dt, datetime) else str(created_dt)
            last_active = updated_dt.strftime("%I:%M %p") if isinstance(updated_dt, datetime) else "09:00"
            
            msgs = doc.get("messages", [])
            if not msgs:
                msg_body = doc.get("text", "")
                if msg_body.startswith("[TICKET]") or msg_body.startswith("[SUPPORT]"):
                    msg_body = msg_body.split("]", 1)[-1].strip()
                msgs = [
                    {
                        "id": "m_init",
                        "from": "user",
                        "body": msg_body,
                        "at": last_active
                    }
                ]
            
            subj = doc.get("subject", "")
            if not subj:
                text_content = doc.get("text", "")
                if text_content.startswith("[TICKET]") or text_content.startswith("[SUPPORT]"):
                    text_content = text_content.split("]", 1)[-1].strip()
                subj = text_content[:40] + ("..." if len(text_content) > 40 else "") or "Live Chat Support"

            # Enrich from maps
            ticket_uid = doc.get("user_id")
            online = False
            photo_url = None
            joined_str = "—"
            first_purchase = "N/A"
            total_purchases = 0
            last_activity_desc = "Unknown"
            device_desc = doc.get("device", "Telegram Mini App")
            
            if ticket_uid is not None:
                user_doc = user_map.get(ticket_uid)
                if user_doc:
                    photo_url = user_doc.get("photoUrl")
                    
                    # 5-minute inactivity check
                    last_active_val = user_doc.get("last_active")
                    if last_active_val:
                        if isinstance(last_active_val, (int, float)):
                            last_active_dt = datetime.fromtimestamp(last_active_val, tz=timezone.utc)
                        elif isinstance(last_active_val, str):
                            try:
                                last_active_dt = datetime.fromisoformat(last_active_val.replace("Z", "+00:00"))
                            except:
                                last_active_dt = None
                        else:
                            last_active_dt = last_active_val
                            
                        if last_active_dt:
                            if last_active_dt.tzinfo is None:
                                last_active_dt = last_active_dt.replace(tzinfo=timezone.utc)
                            if datetime.now(timezone.utc) - last_active_dt < timedelta(minutes=5):
                                online = True
                                
                    # Joined date formatting
                    joined_val = user_doc.get("joined_date") or user_doc.get("joined_at") or user_doc.get("created_at")
                    if not joined_val:
                        doc_id = user_doc.get("_id")
                        if doc_id and isinstance(doc_id, ObjectId):
                            joined_val = doc_id.generation_time
                    if isinstance(joined_val, datetime):
                        joined_str = joined_val.strftime("%d/%m/%Y")
                    elif isinstance(joined_val, (int, float)):
                        joined_str = datetime.fromtimestamp(joined_val, tz=timezone.utc).strftime("%d/%m/%Y")
                    elif isinstance(joined_val, str):
                        joined_str = joined_val.split("T")[0]
                        
                    # Total purchases
                    purchased_list = user_doc.get("purchases", [])
                    user_orders = orders_by_user.get(ticket_uid, [])
                    total_purchases = max(len(purchased_list), len(user_orders))
                    
                    # First purchase name
                    if user_orders:
                        try:
                            tz_min = datetime.min.replace(tzinfo=timezone.utc)
                            sorted_orders = sorted(user_orders, key=lambda o: o.get("created_at") or tz_min)
                            first_order = sorted_orders[0]
                            if first_order.get("story_names"):
                                first_purchase = first_order["story_names"][0]
                        except Exception:
                            pass
                    
                    if first_purchase == "N/A" and purchased_list:
                        first_purchase = "Story Entry"
                            
                    # Device lookup
                    if user_doc.get("device"):
                        device_desc = user_doc["device"]
                    else:
                        latest_event = analytics_map.get(ticket_uid)
                        if latest_event and latest_event.get("device"):
                            device_desc = latest_event["device"].capitalize()
                            
                    # Last activity description
                    latest_event = analytics_map.get(ticket_uid)
                    if latest_event:
                        event_name = latest_event.get("event", latest_event.get("type", "App View"))
                        last_activity_desc = event_name.replace("_", " ").title()
                    elif last_active_val:
                        last_activity_desc = "Active Recently"

            # User typing indicator status
            now = datetime.now(timezone.utc)
            user_typing = False
            user_typing_until = doc.get("user_typing_until")
            if user_typing_until:
                if user_typing_until.tzinfo is None:
                    user_typing_until = user_typing_until.replace(tzinfo=timezone.utc)
                if user_typing_until > now:
                    user_typing = True

            tickets.append({
                "id": str(doc["_id"]),
                "userName": doc.get("user_name", doc.get("first_name", "Unknown")),
                "telegramUsername": f"@{doc.get('username')}" if doc.get("username") else "—",
                "telegramId": str(doc.get("user_id", "")),
                "subject": subj,
                "category": doc.get("category", "Support"),
                "priority": doc.get("priority", "Normal"),
                "status": doc.get("status", "Open"),
                "unread": doc.get("unread", 0),
                "lastActive": last_active,
                "createdAt": created_str,
                "device": device_desc,
                "platform": doc.get("platform", "Android / iOS"),
                "agent": doc.get("assigned_agent", "Unassigned"),
                "tags": doc.get("tags", ["app"]),
                "notes": doc.get("notes", ""),
                "messages": msgs,
                "online": online,
                "photoUrl": photo_url,
                "joined": joined_str,
                "firstPurchase": first_purchase,
                "totalPurchases": total_purchases,
                "lastActivity": last_activity_desc,
                "user_typing": user_typing
            })
        return {"success": True, "data": tickets}
    except Exception as e:
        logger.error(f"Error fetching support: {e}")
        return {"success": False, "data": []}

@api_router.get("/admin/feedback")
async def get_admin_feedback(telegram_id: str):
    from AryaPremium.config import Config
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
            
        arya_db = app.state.db
        cursor = arya_db.db.premium_feedback.find({
            "text": {"$regex": "^\\[FEEDBACK\\]", "$options": "i"}
        }).sort("created_at", -1).limit(200)
        feedbacks = []
        async for doc in cursor:
            feedbacks.append({
                "id": str(doc["_id"]),
                "user_id": doc.get("user_id"),
                "username": doc.get("username", "Unknown"),
                "first_name": doc.get("user_name", doc.get("first_name", "Unknown")),
                "text": doc.get("text", ""),
                "status": doc.get("status", "open"),
                "date": doc.get("created_at", datetime.now(timezone.utc)).isoformat() if isinstance(doc.get("created_at"), datetime) else str(doc.get("created_at", ""))
            })
        return {"success": True, "data": feedbacks}
    except Exception as e:
        logger.error(f"Error fetching feedback: {e}")
        return {"success": False, "data": []}

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# ADMIN STORY REQUESTS MANAGEMENT
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@api_router.get("/admin/requests")
async def get_admin_requests(telegram_id: str):
    """Fetch all story requests for admin management.
    
    Reads from premium_requests (bot's collection) FIRST — this is the unified source.
    Also merges in any legacy premium_feedback[REQUEST] entries not already in premium_requests.
    """
    from AryaPremium.config import Config
    from bson.objectid import ObjectId
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
        arya_db = app.state.db
        items = []
        seen_ids = set()

        # ── 1. Primary: premium_requests (bot collection, unified) ──
        cursor = arya_db.db.premium_requests.find({}).sort("created_at", -1).limit(200)
        async for doc in cursor:
            doc_id = str(doc["_id"])
            seen_ids.add(doc_id)
            # Normalize status: bot uses Title Case (Pending/Searching/etc.)
            raw_status = doc.get("status", "Pending")
            items.append({
                "id": doc_id,
                "source": doc.get("source", "bot"),
                "user_id": doc.get("user_id"),
                "username": doc.get("username", ""),
                "first_name": doc.get("user_name", doc.get("first_name", "Unknown")),
                "story_name": doc.get("story_name", ""),
                "platform": doc.get("platform", ""),
                "completion_type": doc.get("completion_type", ""),
                "text": doc.get("text", doc.get("story_name", "")),
                "status": raw_status.lower() if raw_status else "pending",
                "feedback_id": doc.get("feedback_id", ""),
                "file_url": doc.get("file_url", ""),
                "file_name": doc.get("file_name", ""),
                "created_at": doc.get("created_at", datetime.now(timezone.utc)).isoformat() if isinstance(doc.get("created_at"), datetime) else str(doc.get("created_at", ""))
            })

        # ── 2. Legacy fallback: premium_feedback with [REQUEST] prefix ──
        # Only include those not already cross-referenced in premium_requests
        cursor2 = arya_db.db.premium_feedback.find(
            {"text": {"$regex": "^\\[REQUEST\\]", "$options": "i"}}
        ).sort("created_at", -1).limit(200)
        async for doc in cursor2:
            fb_id = str(doc["_id"])
            # Skip if already represented via feedback_id cross-ref
            already_linked = any(it.get("feedback_id") == fb_id for it in items)
            if already_linked:
                continue
            raw_text = doc.get("text", "")
            display_text = raw_text.replace("[REQUEST]", "").replace("[request]", "").strip()
            raw_status = doc.get("status", "open")
            items.append({
                "id": fb_id,
                "source": "mini_app_legacy",
                "user_id": doc.get("user_id"),
                "username": doc.get("username", ""),
                "first_name": doc.get("user_name", doc.get("first_name", "Unknown")),
                "story_name": display_text[:80],
                "platform": "",
                "completion_type": "",
                "text": display_text,
                "status": raw_status if raw_status in ("pending","searching","posting","posted","completed","rejected","open","in_progress") else "pending",
                "feedback_id": fb_id,
                "file_url": doc.get("file_url", ""),
                "file_name": doc.get("file_name", ""),
                "created_at": doc.get("created_at", datetime.now(timezone.utc)).isoformat() if isinstance(doc.get("created_at"), datetime) else str(doc.get("created_at", ""))
            })

        # Sort combined list by created_at descending
        items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
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
    """Update the status of a story request and notify the user via Telegram.
    
    Tries premium_requests (unified bot collection) first.
    Falls back to premium_feedback for legacy entries.
    ALWAYS sends notification on status change (not just when reply_text is provided).
    """
    from AryaPremium.config import Config
    from bson.objectid import ObjectId
    import aiohttp
    try:
        user_id_int = int(data.telegram_id) if data.telegram_id.isdigit() else data.telegram_id
        if not is_admin(str(data.telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
        arya_db = app.state.db

        # ── Try premium_requests first (unified collection) ──
        doc = None
        collection = None
        try:
            doc = await arya_db.db.premium_requests.find_one({"_id": ObjectId(request_id)})
            if doc:
                collection = arya_db.db.premium_requests
        except Exception:
            pass

        # ── Fallback: premium_feedback (legacy mini app requests) ──
        if not doc:
            try:
                doc = await arya_db.db.premium_feedback.find_one({"_id": ObjectId(request_id)})
                if doc:
                    collection = arya_db.db.premium_feedback
            except Exception:
                pass

        if doc is None or collection is None:
            raise HTTPException(status_code=404, detail="Request not found")

        # Normalize status: admin panel sends lowercase, bot uses Title Case
        # Store in the format each collection expects
        if getattr(collection, "name", None) == "premium_requests":
            # Bot collection uses Title Case
            status_to_store = data.status.replace("_", " ").title()
        else:
            status_to_store = data.status  # feedback uses lowercase

        update_fields = {
            "status": status_to_store,
            "updated_at": datetime.now(timezone.utc)
        }
        if data.reply_text:
            update_fields["admin_reply"] = data.reply_text

        await collection.update_one(
            {"_id": ObjectId(request_id)},
            {"$set": update_fields}
        )

        # ── Cross-sync: if premium_requests doc has a feedback_id, sync status there too ──
        if getattr(collection, "name", None) == "premium_requests" and doc.get("feedback_id"):
            try:
                await arya_db.db.premium_feedback.update_one(
                    {"_id": ObjectId(doc["feedback_id"])},
                    {"$set": {"status": data.status, "updated_at": datetime.now(timezone.utc)}}
                )
            except Exception as e:
                logger.warning(f"Cross-sync to feedback failed: {e}")

        # ── Always notify user via Telegram (on every status change) ──
        try:
            user_chat_id = doc.get("user_id")
            if user_chat_id:
                # Use the Arya Premium Delivery Bot — NOT the management bot
                # Prefer: user's specific delivery bot → any delivery bot → BOT_TOKEN → MGMT last resort
                try:
                    uid_for_token = int(user_chat_id) if str(user_chat_id).isdigit() else None
                    token = await get_customer_bot_token(uid_for_token) if uid_for_token else None
                except Exception:
                    token = None
                if not token:
                    token = getattr(Config, "BOT_TOKEN", None) or getattr(Config, "MGMT_BOT_TOKEN", None)
                if token:
                    # Status label with emoji
                    status_emojis = {
                        "pending": "⏳", "searching": "🔍", "posting": "📤",
                        "posted": "✅", "completed": "🎉", "rejected": "❌",
                        "open": "📬", "in_progress": "🔄", "in progress": "🔄"
                    }
                    status_str = data.status or "pending"
                    emoji = status_emojis.get(status_str.lower(), "📋")
                    
                    # Safely build and escape story name
                    story_val = doc.get("story_name") or ""
                    text_val = doc.get("text") or ""
                    raw_story = story_val or text_val
                    story_name = escape_html(raw_story[:80] if raw_story else "")

                    msg_lines = [
                        f"{emoji} <b>Story Request Update!</b>",
                        "",
                    ]
                    if story_name:
                        msg_lines.append(f"<b>Story:</b> {story_name}")
                    msg_lines.append(f"<b>Status:</b> <code>{escape_html(status_str.replace('_', ' ').title())}</code>")
                    if data.reply_text:
                        msg_lines.append("")
                        msg_lines.append(f"<b>Admin Message:</b>")
                        msg_lines.append(escape_html(data.reply_text))
                    msg_lines.append("")
                    msg_lines.append("<i>Check 'My Requests' in your Profile for more info!</i>")
                    notify_text = "\n".join(msg_lines)

                    async with aiohttp.ClientSession() as session:
                        await session.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": user_chat_id, "text": notify_text, "parse_mode": "HTML"},
                            timeout=5
                        )
                        logger.info(f"Notified user {user_chat_id} about request status → {status_str}")
                else:
                    logger.warning("No bot token available to notify user about request status update")
        except Exception as notify_err:
            logger.error(f"Failed to build or send status update notification: {notify_err}", exc_info=True)

        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating request: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to update request: {str(e)}")

class SupportReply(BaseModel):
    telegram_id: str
    ticket_id: str
    reply_text: str
    reply_media_file_id: Optional[str] = None  # Telegram file_id to forward as media
    reply_media_type: Optional[str] = None  # photo, video, audio, document
    file_url: Optional[str] = None
    file_type: Optional[str] = None

@api_router.post("/admin/support/reply")
async def reply_support(data: SupportReply):
    from AryaPremium.config import Config
    from bson.objectid import ObjectId
    import aiohttp
    import time
    try:
        user_id_int = int(data.telegram_id) if data.telegram_id.isdigit() else data.telegram_id
        if not is_admin(str(data.telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
            
        arya_db = app.state.db
        ticket = await arya_db.db.premium_feedback.find_one({"_id": ObjectId(data.ticket_id)})
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")
        
        # Determine which bot token to use (guarantees non-show-store bot)
        raw_uid = ticket.get("user_id", 0)
        try:
            uid_int = int(raw_uid)
        except (ValueError, TypeError):
            uid_int = 0
        token = await get_customer_bot_token(uid_int)

        # Build message object
        msg_id = f"a-{int(time.time() * 1000)}"
        msg_at = datetime.now(timezone.utc).strftime("%I:%M %p")
        reply_body = data.reply_text or "Attachment"
        new_msg = {
            "id": msg_id,
            "from": "agent",
            "body": reply_body,
            "at": msg_at,
            "status": "seen"
        }
        if data.file_url:
            new_msg["file_url"] = data.file_url
            new_msg["file_type"] = data.file_type or "application/octet-stream"

        # Send Telegram Bot DM to User with detailed ticket response summary
        if token:
            try:
                async with aiohttp.ClientSession() as session:
                    raw_cid = ticket["user_id"]
                    try:
                        chat_id = int(raw_cid)
                    except (ValueError, TypeError):
                        chat_id = raw_cid
                    is_live_chat = ticket.get("category") == "Live Chat"
                    ticket_ref = ticket.get("ticket_id") or f"ARY-{str(ticket['_id'])[-6:].upper()}"
                    subj = ticket.get("subject") or "Support Ticket"

                    if is_live_chat:
                        dm_txt = (
                            f"💬 <b>New Message in Live Chat</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━\n"
                            f"<b>Support Team:</b>\n"
                            f"<blockquote>{escape_html(reply_body[:500])}</blockquote>\n"
                            f"<i>Open Mini App Support section to continue chatting.</i>"
                        )
                    else:
                        dm_txt = (
                            f"💬 <b>Support Ticket Update</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━\n"
                            f"🆔 <b>Ticket ID:</b> <code>{ticket_ref}</code>\n"
                            f"📌 <b>Subject:</b> {escape_html(subj)}\n\n"
                            f"<b>Admin Reply:</b>\n"
                            f"<blockquote>{escape_html(reply_body[:500])}</blockquote>\n\n"
                            f"<i>You can check full status & details in the Support section of Mini App.</i>"
                        )

                    await session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                        "chat_id": chat_id,
                        "text": dm_txt,
                        "parse_mode": "HTML"
                    }, timeout=5)

                    # Forward media if provided via bot API media methods
                    if data.reply_media_file_id and data.reply_media_type:
                        method_map = {"photo": "sendPhoto", "video": "sendVideo", "audio": "sendAudio", "document": "sendDocument"}
                        method = method_map.get(data.reply_media_type, "sendDocument")
                        field_map = {"photo": "photo", "video": "video", "audio": "audio", "document": "document"}
                        field = field_map.get(data.reply_media_type, "document")
                        await session.post(f"https://api.telegram.org/bot{token}/{method}", json={
                            "chat_id": chat_id,
                            field: data.reply_media_file_id
                        }, timeout=5)
            except Exception as notify_err:
                logger.error(f"Failed to send support reply Telegram notification: {notify_err}")

        # Update database for both Live Chat and Support Tickets
        is_live = ticket.get("category") == "Live Chat"
        # ✅ FIX: Do NOT auto-set "Resolved" after every reply.
        # Live Chat stays "Open"; support tickets go to "Waiting User"
        # so admin can still see the thread. Only explicit status-change
        # endpoint (update_support_status) should set Resolved/Closed.
        new_status = "Open" if is_live else "Waiting User"
        
        await arya_db.db.premium_feedback.update_one(
            {"_id": ObjectId(data.ticket_id)},
            {
                "$push": {"messages": new_msg},
                "$set": {
                    "status": new_status,
                    "admin_reply": reply_body,
                    "unread": 0,              # admin unread reset
                    "user_has_new_reply": True,  # user-side polling flag
                    "updated_at": datetime.now(timezone.utc)
                }
            }
        )
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in reply_support: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to reply: {str(e)}")

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
                {
                    "is_show": {"$ne": True},
                    "platform": {"$not": {"$regex": r"(kuku\s*tv|story\s*tv)", "$options": "i"}}
                },
                sort=[("_id", -1)]
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
                if image_url and not image_url.startswith("http") and not image_url.startswith("/api/"):
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
    Returns the top 10 most purchased stories over the last 60 days.
    """
    try:
        arya_db = app.state.db
        from bson.objectid import ObjectId
        
        order_counts = []
        try:
            sixty_days_ago = datetime.now(timezone.utc) - timedelta(days=60)
            pipeline_orders = [
                {"$match": {
                    "status": {"$in": ["paid", "delivered"]},
                    "created_at": {"$gte": sixty_days_ago}
                }},
                {"$unwind": "$story_ids"},
                {"$group": {"_id": "$story_ids", "count": {"$sum": 1}}}
            ]
            order_counts = await arya_db.db.orders.aggregate(pipeline_orders).to_list(None)
        except Exception as e:
            logger.warning(f"Failed to aggregate orders: {e}")
            
        # Combine counts
        counts_map = {}
        for item in order_counts:
            sid = str(item.get("_id"))
            counts_map[sid] = counts_map.get(sid, 0) + item.get("count", 0)
            
        sorted_counts = sorted(counts_map.items(), key=lambda x: x[1], reverse=True)[:10]
        
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
            cursor = arya_db.db.premium_stories.find({
                "status": "active",
                "is_show": {"$ne": True},
                "platform": {"$not": {"$regex": r"(kuku\s*tv|story\s*tv)", "$options": "i"}}
            }).sort("_id", -1).limit(6)
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

# ── Admin Series endpoints ──────────────────────────────────────────────────
@api_router.get("/admin/series")
async def get_admin_series(telegram_id: str):
    try:
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
        arya_db = app.state.db
        cursor = arya_db.db.premium_series.find({})
        series = []
        async for doc in cursor:
            doc["id"] = str(doc.get("_id", ""))
            doc["_id"] = str(doc.get("_id", ""))
            series.append(doc)
        return {"success": True, "data": series}
    except Exception as e:
        logger.error(f"Error fetching admin series: {e}")
        return {"success": True, "data": []}

@api_router.post("/admin/series")
async def save_admin_series(data: dict = Body(...)):
    try:
        tg_id = str(data.get("telegram_id", ""))
        if not is_admin(tg_id):
            raise HTTPException(status_code=403, detail="Not authorized")
        arya_db = app.state.db
        s_id = data.get("series_id") or data.get("id")
        if not s_id:
            import uuid
            s_id = f"series_{uuid.uuid4().hex[:8]}"
            data["series_id"] = s_id
        data.pop("_id", None)
        await arya_db.db.premium_series.update_one({"series_id": s_id}, {"$set": data}, upsert=True)
        return {"success": True, "series_id": s_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@api_router.delete("/admin/series/{series_id}")
async def delete_admin_series(series_id: str, telegram_id: str):
    try:
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
        arya_db = app.state.db
        await arya_db.db.premium_series.delete_one({"series_id": series_id})
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── Admin UTR Management Endpoints ──────────────────────────────────────────
class AddUtrPayload(BaseModel):
    telegram_id: str
    utrs: List[str]
    note: Optional[str] = ""

@api_router.get("/admin/utrs")
async def get_admin_utrs(telegram_id: str, search: Optional[str] = None):
    try:
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
        arya_db = app.state.db
        
        query = {}
        if search and search.strip():
            search_str = search.strip()
            query["$or"] = [
                {"utr": {"$regex": search_str, "$options": "i"}},
                {"note": {"$regex": search_str, "$options": "i"}},
                {"user_id": {"$regex": search_str, "$options": "i"}}
            ]
            
        cursor = arya_db.db.verified_utrs.find(query).sort([("verified_at", -1), ("added_at", -1)]).limit(500)
        utrs_list = []
        async for doc in cursor:
            doc["id"] = str(doc.get("_id", ""))
            doc["_id"] = str(doc.get("_id", ""))
            v_at = doc.get("verified_at") or doc.get("added_at") or doc.get("created_at")
            doc["formatted_date"] = v_at.isoformat() if isinstance(v_at, datetime) else str(v_at or "")
            utrs_list.append(doc)
            
        total_count = await arya_db.db.verified_utrs.count_documents({})
        return {"success": True, "data": utrs_list, "total": total_count}
    except Exception as e:
        logger.error(f"Error fetching admin UTRs: {e}")
        return {"success": False, "data": [], "total": 0}

@api_router.post("/admin/utrs/add")
async def add_admin_utrs(payload: AddUtrPayload):
    try:
        if not is_admin(str(payload.telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
        arya_db = app.state.db
        
        added_count = 0
        already_existing = 0
        added_list = []
        
        for raw_utr in payload.utrs:
            utr_clean = "".join(c for c in str(raw_utr) if c.isdigit())
            if not utr_clean or len(utr_clean) < 8:
                continue
                
            existing = await arya_db.db.verified_utrs.find_one({"utr": utr_clean})
            if existing:
                already_existing += 1
                continue
                
            doc = {
                "utr": utr_clean,
                "source": "manual_admin",
                "added_by": str(payload.telegram_id),
                "note": payload.note or "Manually registered by admin",
                "verified_at": datetime.now(timezone.utc),
                "added_at": datetime.now(timezone.utc)
            }
            await arya_db.db.verified_utrs.insert_one(doc)
            added_count += 1
            added_list.append(utr_clean)
            
        return {
            "success": True, 
            "added_count": added_count, 
            "already_existing_count": already_existing,
            "message": f"Successfully registered {added_count} UTR(s). ({already_existing} were already in database)."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@api_router.delete("/admin/utrs/{utr_val}")
async def delete_admin_utr(utr_val: str, telegram_id: str):
    try:
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
        arya_db = app.state.db
        from bson.objectid import ObjectId
        
        res = await arya_db.db.verified_utrs.delete_one({"utr": utr_val})
        if res.deleted_count == 0 and len(utr_val) == 24:
            try:
                await arya_db.db.verified_utrs.delete_one({"_id": ObjectId(utr_val)})
            except:
                pass
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ─────────────────────────────────────────────────────────────────────────────
# BUYERS MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# PREMIUM UNIFIED BAN MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────
@api_router.get("/admin/bans")
async def get_admin_bans(telegram_id: str):
    try:
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
        arya_db = app.state.db
        
        # Fetch all premium bans
        bans = await arya_db.db.premium_bans.find().sort("banned_at", -1).to_list(length=None)
        for b in bans:
            b["_id"] = str(b["_id"])
            if "banned_at" in b and isinstance(b["banned_at"], datetime):
                b["banned_at"] = b["banned_at"].isoformat()
            if "ips" in b and isinstance(b["ips"], list):
                b["ip_details"] = [{"ip": ip, "universal": _is_universal_ip(ip)} for ip in b["ips"]]
                
        # Fetch live activity attempts (last 100)
        activity = await arya_db.db.premium_ban_activity.find().sort("timestamp", -1).to_list(length=100)
        for a in activity:
            a["_id"] = str(a["_id"])
            if "timestamp" in a and isinstance(a["timestamp"], datetime):
                a["timestamp"] = a["timestamp"].isoformat()
                
        return {"success": True, "bans": bans, "activity": activity}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def _resolve_bot_token_for_broadcast(arya_db, user_id: Optional[Union[int, str]] = None) -> str:
    token = ""
    if user_id:
        try:
            token = await get_customer_bot_token(user_id)
        except Exception as e:
            logger.warning(f"📢 [Broadcast Token] Failed resolving user {user_id} token: {e}")
            
    mgmt_token = str(getattr(Config, "MGMT_BOT_TOKEN", None) or os.environ.get("MGMT_BOT_TOKEN", "")).strip()

    # Try finding delivery bot token from DB first
    if not token and arya_db:
        try:
            bot_doc = await arya_db.db.premium_bots.find_one({"token": {"$exists": True, "$ne": ""}})
            if bot_doc and bot_doc.get("token"):
                token = bot_doc.get("token", "")
        except Exception:
            pass

    if not token:
        token = getattr(Config, "BOT_TOKEN", None) or os.environ.get("BOT_TOKEN", "") or os.environ.get("DELIVERY_BOT_TOKEN", "")
        
    if not token:
        token = mgmt_token
            
    token_str = str(token or "").strip()
    logger.info(f"📢 [Broadcast Token Resolved] User: {user_id} | Token Prefix: {token_str[:10]}...")
    return token_str

@api_router.post("/admin/scan-universal-ips")
async def scan_and_clean_universal_ips(payload: dict):
    telegram_id = payload.get("telegram_id")
    if not is_admin(str(telegram_id)):
        raise HTTPException(status_code=403, detail="Not authorized")
        
    arya_db = app.state.db
    if not arya_db:
        raise HTTPException(status_code=500, detail="Database not available")
        
    bans_cursor = arya_db.db.premium_bans.find()
    bans = await bans_cursor.to_list(length=None)
    
    cleaned_records = 0
    total_universal_removed = 0
    removed_report = []
    
    for ban in bans:
        ips = ban.get("ips", [])
        if not ips:
            continue
            
        universal_ips = [ip for ip in ips if _is_universal_ip(ip)]
        if universal_ips:
            # Filter them out
            clean_ips = [ip for ip in ips if not _is_universal_ip(ip)]
            await arya_db.db.premium_bans.update_one(
                {"_id": ban["_id"]},
                {"$set": {"ips": clean_ips}}
            )
            cleaned_records += 1
            total_universal_removed += len(universal_ips)
            removed_report.append({
                "telegram_id": str(ban["_id"]),
                "name": ban.get("name", "Unknown"),
                "removed_ips": universal_ips
            })
            
    return {
        "success": True,
        "cleaned_records_count": cleaned_records,
        "total_universal_removed": total_universal_removed,
        "report": removed_report
    }


@api_router.post("/admin/ban")
async def admin_ban_user(payload: dict):
    telegram_id = payload.get("telegram_id")
    try:
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
        
        target_id = payload.get("target_id")
        reason = payload.get("reason", "Banned by administrator")
        name = payload.get("name", f"User {target_id}")
        
        if not target_id:
            raise HTTPException(status_code=400, detail="Missing target_id")
            
        try:
            target_id_int = int(target_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid target Telegram ID")
            
        # Prevent banning admins/owners
        if is_admin(str(target_id_int)):
            raise HTTPException(status_code=400, detail="Cannot ban an administrator")
        from config import Config as MainConfig
        if MainConfig.OWNER_IDS and target_id_int in MainConfig.OWNER_IDS:
            raise HTTPException(status_code=400, detail="Cannot ban an owner")
            
        arya_db = app.state.db
        
        # Get historical IPs and Device IDs of target_id
        historical_ips = await arya_db.db.mini_app_analytics.distinct("ip", {"user_id": target_id_int})
        ips = [ip for ip in historical_ips if ip and not _is_universal_ip(ip)]
        
        historical_device_ids = await arya_db.db.mini_app_analytics.distinct("fingerprint_id", {"user_id": target_id_int})
        device_ids = [d for d in historical_device_ids if d]
        
        # Save ban in premium_bans collection
        await arya_db.db.premium_bans.update_one(
            {"_id": target_id_int},
            {"$set": {
                "ips": list(set(ips)),
                "device_ids": list(set(device_ids)),
                "reason": reason,
                "status": "banned",
                "banned_at": datetime.now(timezone.utc),
                "name": name
            }},
            upsert=True
        )
        
        # Propagate to Delivery Bot ban list in main database
        await arya_db.db.users.update_one(
            {"id": target_id_int},
            {"$set": {"ban_status": {"is_banned": True, "ban_reason": reason}}},
            upsert=True
        )
        
        # Clear in-memory abuse strike counts for clean state
        try:
            from plugins.share_bot import _abuse_strikes, _abuse_last_delivery
            _abuse_strikes.pop(target_id_int, None)
            _abuse_last_delivery.pop(target_id_int, None)
        except Exception:
            pass
            
        # Log to logs channel (Strictly No Emojis)
        from utils_ban_logger import log_premium_ban_event
        asyncio.create_task(log_premium_ban_event(
            user_id=target_id_int,
            name=name,
            action="BANNED",
            reason=reason,
            ips=ips
        ))
        
        return {"success": True, "message": f"Successfully banned User {target_id_int}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@api_router.post("/admin/unban")
async def admin_unban_user(payload: dict):
    telegram_id = payload.get("telegram_id")
    try:
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
        
        target_id = payload.get("target_id")
        if not target_id:
            raise HTTPException(status_code=400, detail="Missing target_id")
            
        arya_db = app.state.db
        target_str = str(target_id).strip()
        
        is_ip = False
        is_device = False
        is_tg_id = False
        
        if "." in target_str or ":" in target_str:
            is_ip = True
        else:
            try:
                target_id_int = int(target_str)
                is_tg_id = True
            except ValueError:
                is_device = True
                
        if is_ip:
            res = await arya_db.db.premium_bans.update_many(
                {"ips": target_str},
                {"$pull": {"ips": target_str}}
            )
            from utils_ban_logger import log_premium_ban_event
            asyncio.create_task(log_premium_ban_event(
                user_id=0,
                name=f"IP {target_str}",
                action="UNBANNED",
                reason=f"IP unbanned by administrator (removed from {res.modified_count} profiles)",
                ips=[target_str]
            ))
            return {"success": True, "message": f"Successfully unbanned IP {target_str} (removed from {res.modified_count} profiles)"}
            
        elif is_device:
            res = await arya_db.db.premium_bans.update_many(
                {"device_ids": target_str},
                {"$pull": {"device_ids": target_str}}
            )
            from utils_ban_logger import log_premium_ban_event
            asyncio.create_task(log_premium_ban_event(
                user_id=0,
                name=f"Device {target_str}",
                action="UNBANNED",
                reason=f"Device ID unbanned by administrator (removed from {res.modified_count} profiles)",
                ips=[]
            ))
            return {"success": True, "message": f"Successfully unbanned Device {target_str} (removed from {res.modified_count} profiles)"}
            
        elif is_tg_id:
            ban_doc = await arya_db.db.premium_bans.find_one({"_id": target_id_int})
            name = ban_doc.get("name", f"User {target_id_int}") if ban_doc else f"User {target_id_int}"
            
            await arya_db.db.premium_bans.delete_one({"_id": target_id_int})
            
            await arya_db.db.users.update_one(
                {"id": target_id_int},
                {"$set": {"ban_status": {"is_banned": False, "ban_reason": ""}}}
            )
            
            try:
                from plugins.share_bot import _abuse_strikes, _abuse_last_delivery
                _abuse_strikes.pop(target_id_int, None)
                _abuse_last_delivery.pop(target_id_int, None)
            except Exception:
                pass
                
            from utils_ban_logger import log_premium_ban_event
            asyncio.create_task(log_premium_ban_event(
                user_id=target_id_int,
                name=name,
                action="UNBANNED",
                reason="Unbanned by administrator",
                ips=ban_doc.get("ips", []) if ban_doc else []
            ))
            return {"success": True, "message": f"Successfully unbanned User {target_id_int}"}
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
@api_router.get("/admin/buyers")
async def get_admin_buyers(telegram_id: str):
    """Fetches all buyers and detailed user purchase logs for the admin dashboard."""
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized as Admin")
            
        arya_db = app.state.db
        computed = await fetch_processed_buyers_data(arya_db)
        buyers = computed["buyers"]
        return {"success": True, "data": buyers, "total": len(buyers)}
    except Exception as e:
        logger.error(f"Error fetching buyers: {e}")
        return {"success": False, "data": []}

# ══════════════════════════════════════════════════
# BROADCAST ENGINE & LIVE STATUS TRACKING
# ══════════════════════════════════════════════════
_broadcast_jobs: Dict[str, Dict[str, Any]] = {}

class BroadcastPayload(BaseModel):
    telegram_id: str
    target_user_id: Optional[Union[int, str]] = None
    audience: Optional[str] = "all"  # "paid", "pending", "all"
    message: str
    media_url: Optional[str] = None
    media_type: Optional[str] = "none"  # "none", "image", "video", "document"
    buttons: Optional[List[Dict[str, str]]] = None

async def _send_tg_bot_message(bot_token: str, chat_id: Union[int, str], text: str, media_url: str = None, media_type: str = None, buttons: list = None) -> dict:
    import aiohttp
    
    clean_token = str(bot_token or "").strip()
    if clean_token.lower().startswith("bot"):
        clean_token = clean_token[3:].strip()

    if not clean_token:
        logger.error(f"📢 [Broadcast Telegram API Error] Bot token is empty for chat_id={chat_id}")
        return {"success": False, "error": "Bot token is empty"}

    reply_markup = None
    if buttons and isinstance(buttons, list) and len(buttons) > 0:
        keyboard_rows = []
        for btn in buttons:
            if isinstance(btn, dict) and btn.get("label"):
                label_str = str(btn["label"]).strip()
                url_str = str(btn.get("url") or "").strip()
                if label_str and url_str:
                    if not (url_str.startswith("http://") or url_str.startswith("https://") or url_str.startswith("tg://")):
                        url_str = f"https://{url_str}"
                    keyboard_rows.append([{"text": label_str, "url": url_str}])
        if keyboard_rows:
            reply_markup = {"inline_keyboard": keyboard_rows}

    payload = {
        "chat_id": chat_id,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    method_name = "sendMessage"
    if media_url and str(media_url).strip():
        m_type = str(media_type or "image").lower()
        if "video" in m_type:
            method_name = "sendVideo"
            payload["video"] = str(media_url).strip()
            payload["caption"] = text
        elif "audio" in m_type or "voice" in m_type or "mp3" in m_type:
            method_name = "sendAudio"
            payload["audio"] = str(media_url).strip()
            payload["caption"] = text
        elif "doc" in m_type or "file" in m_type or "pdf" in m_type or "document" in m_type:
            method_name = "sendDocument"
            payload["document"] = str(media_url).strip()
            payload["caption"] = text
        else:
            method_name = "sendPhoto"
            payload["photo"] = str(media_url).strip()
            payload["caption"] = text
    else:
        payload["text"] = text

    url = f"https://api.telegram.org/bot{clean_token}/{method_name}"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=15) as resp:
                res_data = await resp.json()
                if res_data.get("ok"):
                    msg_id = res_data.get("result", {}).get("message_id")
                    logger.info(f"📢 [Broadcast Telegram API Success] Delivered to {chat_id} (msg_id: {msg_id})")
                    return {"success": True, "message_id": msg_id}
                else:
                    err_desc = res_data.get("description", "Unknown Telegram API error")
                    logger.warning(f"📢 [Broadcast Telegram API Failed] chat_id={chat_id}: {err_desc}")
                    
                    # If HTML formatting failed, retry without HTML parse_mode
                    if "can't parse entities" in err_desc.lower() or "parse" in err_desc.lower():
                        logger.info(f"📢 [Broadcast Telegram API] Retrying without HTML parse_mode for {chat_id}...")
                        payload.pop("parse_mode", None)
                        async with session.post(url, json=payload, timeout=15) as retry_resp:
                            retry_data = await retry_resp.json()
                            if retry_data.get("ok"):
                                msg_id = retry_data.get("result", {}).get("message_id")
                                logger.info(f"📢 [Broadcast Telegram API Success - Plain Retry] Delivered to {chat_id} (msg_id: {msg_id})")
                                return {"success": True, "message_id": msg_id}
                            else:
                                err_desc2 = retry_data.get("description", err_desc)
                                return {"success": False, "error": err_desc2}
                                
                    return {"success": False, "error": err_desc}
    except Exception as err:
        logger.error(f"📢 [Broadcast Telegram API Exception] chat_id={chat_id}: {err}")
        return {"success": False, "error": str(err)}



async def resolve_broadcast_audience_ids(arya_db, audience_mode: str) -> List[int]:
    target_ids = set()
    mode = str(audience_mode or "all").lower()

    if mode == "paid":
        paid_orders = await arya_db.db.orders.find(
            {"status": {"$in": ["paid", "approved", "completed", "delivered"]}},
            {"user_id": 1}
        ).to_list(length=100000)
        for o in paid_orders:
            uid = o.get("user_id")
            if uid and str(uid).isdigit() and int(uid) > 0:
                target_ids.add(int(uid))

        prem_pur = await arya_db.db.premium_purchases.find({}, {"user_id": 1}).to_list(length=100000)
        for p in prem_pur:
            uid = p.get("user_id")
            if uid and str(uid).isdigit() and int(uid) > 0:
                target_ids.add(int(uid))

        users = await arya_db.db.users.find(
            {"purchases.0": {"$exists": True}},
            {"id": 1}
        ).to_list(length=100000)
        for u in users:
            uid = u.get("id")
            if uid and str(uid).isdigit() and int(uid) > 0:
                target_ids.add(int(uid))

    elif mode == "pending":
        paid_uids = set(await resolve_broadcast_audience_ids(arya_db, "paid"))

        pending_orders = await arya_db.db.orders.find(
            {"status": {"$in": ["pending", "created", "processing", "waiting_screenshot", "failed", "rejected"]}},
            {"user_id": 1}
        ).to_list(length=100000)
        for o in pending_orders:
            uid = o.get("user_id")
            if uid and str(uid).isdigit() and int(uid) not in paid_uids and int(uid) > 0:
                target_ids.add(int(uid))

        pending_checkouts = await arya_db.db.premium_checkout.find(
            {"status": {"$in": ["pending", "created", "processing", "waiting_screenshot", "failed", "rejected"]}},
            {"user_id": 1}
        ).to_list(length=100000)
        for c in pending_checkouts:
            uid = c.get("user_id")
            if uid and str(uid).isdigit() and int(uid) not in paid_uids and int(uid) > 0:
                target_ids.add(int(uid))

    else:  # "all"
        users = await arya_db.db.users.find({}, {"id": 1}).to_list(length=200000)
        for u in users:
            uid = u.get("id")
            if uid and str(uid).isdigit() and int(uid) > 0:
                target_ids.add(int(uid))

        prem_users = await arya_db.db.premium_users.find({}, {"id": 1, "telegram_id": 1}).to_list(length=200000)
        for u in prem_users:
            uid = u.get("id") or u.get("telegram_id")
            if uid and str(uid).isdigit() and int(uid) > 0:
                target_ids.add(int(uid))

        orders = await arya_db.db.orders.find({}, {"user_id": 1}).to_list(length=200000)
        for o in orders:
            uid = o.get("user_id")
            if uid and str(uid).isdigit() and int(uid) > 0:
                target_ids.add(int(uid))

        purchases = await arya_db.db.premium_purchases.find({}, {"user_id": 1}).to_list(length=200000)
        for p in purchases:
            uid = p.get("user_id")
            if uid and str(uid).isdigit() and int(uid) > 0:
                target_ids.add(int(uid))

    res_list = list(target_ids)
    logger.info(f"📢 [Broadcast Audience Filter '{mode}'] Resolved {len(res_list)} target user IDs")
    return res_list

@api_router.post("/admin/broadcast/single")
async def send_single_broadcast(data: BroadcastPayload):
    if not is_admin(str(data.telegram_id)):
        logger.warning(f"📢 [Broadcast Single] Unauthorized access attempt by {data.telegram_id}")
        raise HTTPException(status_code=403, detail="Not authorized")
        
    target_raw = data.target_user_id
    if not target_raw:
        logger.warning("📢 [Broadcast Single] Missing target_user_id")
        raise HTTPException(status_code=400, detail="target_user_id is required")

    arya_db = app.state.db
    target_uid = int(target_raw) if str(target_raw).isdigit() else 0
    if not target_uid:
        logger.warning(f"📢 [Broadcast Single] Invalid target user ID: {target_raw}")
        raise HTTPException(status_code=400, detail="Invalid target user ID")

    logger.info(f"📢 [Broadcast Single] Initiating single broadcast from Admin {data.telegram_id} to User {target_uid}")
    bot_token = await _resolve_bot_token_for_broadcast(arya_db, target_uid)
    if not bot_token:
        logger.error(f"📢 [Broadcast Single] Bot token missing for target user {target_uid}")
        raise HTTPException(status_code=500, detail="Bot token not configured")

    res = await _send_tg_bot_message(
        bot_token=bot_token,
        chat_id=target_uid,
        text=data.message,
        media_url=data.media_url,
        media_type=data.media_type,
        buttons=data.buttons
    )
    logger.info(f"📢 [Broadcast Single Result] User {target_uid} -> {res}")
    return res

@api_router.post("/admin/broadcast/send")
async def start_bulk_broadcast(data: BroadcastPayload):
    if not is_admin(str(data.telegram_id)):
        logger.warning(f"📢 [Broadcast Bulk] Unauthorized access attempt by {data.telegram_id}")
        raise HTTPException(status_code=403, detail="Not authorized")

    arya_db = app.state.db
    audience_mode = data.audience or "all"
    logger.info(f"📢 [Broadcast Bulk] Initiating bulk broadcast by Admin {data.telegram_id} (audience filter: '{audience_mode}')")
    
    target_ids = await resolve_broadcast_audience_ids(arya_db, audience_mode)

    if not target_ids:
        logger.warning(f"📢 [Broadcast Bulk] No target users found for audience filter '{audience_mode}'")
        return {"success": False, "detail": f"No users found for audience filter '{audience_mode}'"}

    import uuid
    job_id = f"bcast_{uuid.uuid4().hex[:8]}"

    job_info = {
        "job_id": job_id,
        "status": "running",
        "audience": audience_mode,
        "total": len(target_ids),
        "sent": 0,
        "failed": 0,
        "progress": 0.0,
        "logs": [f"Broadcast started for {len(target_ids)} users ({audience_mode})"],
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    _broadcast_jobs[job_id] = job_info

    logger.info(f"📢 [Broadcast Bulk Launched] Job ID: {job_id} | Total Targets: {len(target_ids)}")
    asyncio.create_task(_run_bulk_broadcast_worker(job_id, target_ids, data, arya_db))

    return {"success": True, "job_id": job_id, "total": len(target_ids), "audience": audience_mode}

async def _run_bulk_broadcast_worker(job_id: str, target_ids: List[int], data: BroadcastPayload, arya_db):
    job = _broadcast_jobs.get(job_id)
    if not job:
        logger.error(f"📢 [Broadcast Worker Error] Job {job_id} not found in _broadcast_jobs")
        return

    default_delivery_token = await _resolve_bot_token_for_broadcast(arya_db)
    if not default_delivery_token:
        job["status"] = "failed"
        job["logs"].append("Error: Bot token missing")
        logger.error(f"📢 [Broadcast Worker Error] Job {job_id} failed: Bot token missing")
        return

    total = len(target_ids)
    logger.info(f"📢 [Broadcast Worker Started] Job {job_id} sending to {total} users...")
    
    for idx, uid in enumerate(target_ids):
        if job.get("status") == "cancelled":
            job["logs"].append("Broadcast cancelled by admin")
            logger.info(f"📢 [Broadcast Worker] Job {job_id} cancelled by admin at {idx}/{total}")
            break

        user_bot_token = (await _resolve_bot_token_for_broadcast(arya_db, uid)) or default_delivery_token

        res = await _send_tg_bot_message(
            bot_token=user_bot_token,
            chat_id=uid,
            text=data.message,
            media_url=data.media_url,
            media_type=data.media_type,
            buttons=data.buttons
        )

        if res.get("success"):
            job["sent"] += 1
        else:
            job["failed"] += 1
            if len(job["logs"]) < 60:
                job["logs"].append(f"Failed User {uid}: {res.get('error', 'Error')}")

        processed = idx + 1
        job["progress"] = round((processed / total) * 100, 1)

        if processed % 25 == 0:
            logger.info(f"📢 [Broadcast Worker Progress] Job {job_id}: {processed}/{total} processed ({job['progress']}%) — Sent: {job['sent']}, Failed: {job['failed']}")
            await asyncio.sleep(1.0)
        else:
            await asyncio.sleep(0.05)

    if job.get("status") == "running":
        job["status"] = "completed"
        job["progress"] = 100.0
        job["logs"].append(f"Broadcast Completed! Delivered: {job['sent']}, Failed: {job['failed']}")
        logger.info(f"🎉 📢 [Broadcast Worker Completed] Job {job_id} Finished! Total: {total} | Delivered: {job['sent']} | Failed: {job['failed']}")

@api_router.get("/admin/broadcast/status/{job_id}")
async def get_broadcast_status(job_id: str, telegram_id: str):
    if not is_admin(str(telegram_id)):
        raise HTTPException(status_code=403, detail="Not authorized")
    job = _broadcast_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"success": True, "job": job}

@api_router.post("/admin/broadcast/cancel/{job_id}")
async def cancel_broadcast(job_id: str, telegram_id: str):
    if not is_admin(str(telegram_id)):
        raise HTTPException(status_code=403, detail="Not authorized")
    job = _broadcast_jobs.get(job_id)
    if job:
        job["status"] = "cancelled"
        return {"success": True, "message": "Broadcast cancelled"}
    return {"success": False, "detail": "Job not found"}

@api_router.get("/admin/shared-ips")
async def get_shared_ips(telegram_id: str):
    try:
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
        arya_db = app.state.db
        
        # Aggregate logs in mini_app_analytics to find IPs with > 2 unique users
        pipeline = [
            {"$match": {"ip": {"$exists": True, "$ne": None, "$nin": ["", "127.0.0.1", "::1", "unknown"]}}},
            {
                "$group": {
                    "_id": "$ip",
                    "unique_users": {"$addToSet": "$user_id"},
                    "total_hits": {"$sum": 1}
                }
            },
            {
                "$project": {
                    "ip": "$_id",
                    "unique_users": "$unique_users",
                    "unique_users_count": {"$size": "$unique_users"},
                    "total_hits": "$total_hits"
                }
            },
            {"$match": {"unique_users_count": {"$gt": 2}}},
            {"$sort": {"unique_users_count": -1}},
            {"$limit": 50}
        ]
        
        shared_ips = []
        async for doc in arya_db.db.mini_app_analytics.aggregate(pipeline):
            users_list = []
            for uid in doc.get("unique_users", []):
                if not uid:
                    continue
                try:
                    uid_int = int(uid)
                    user_doc = await arya_db.db.users.find_one({"id": uid_int})
                    username = user_doc.get("username") if user_doc else None
                    first_name = user_doc.get("first_name") if user_doc else None
                    name = f"@{username}" if username else (first_name or f"User {uid}")
                    users_list.append({"id": uid_int, "name": name})
                except Exception:
                    users_list.append({"id": uid, "name": f"User {uid}"})
            
            shared_ips.append({
                "ip": doc["ip"],
                "users": users_list,
                "users_count": doc["unique_users_count"],
                "total_hits": doc["total_hits"]
            })
            
        return {"success": True, "shared_ips": shared_ips}
    except Exception as e:
        logger.error(f"Error fetching shared IPs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# Admin: Order ID Lookup
# ==========================================

@api_router.get("/admin/order-lookup")
async def admin_order_lookup(order_id: str, telegram_id: str = ""):
    """
    Given an order_id (from user's payment screen / session), return:
    - Full order document (status, amount, story_ids, upi_id_shown, created_at, etc.)
    - Buyer profile (name, username, telegram_id) so admin can identify the user
    - story_names resolved from DB
    Used by admin to find and manually approve a pending/failed UPI payment.
    """
    db = getattr(app.state, "db", None)
    if not db:
        raise HTTPException(status_code=500, detail="DB unavailable")

    order_id = str(order_id).strip()
    if not order_id:
        raise HTTPException(status_code=400, detail="order_id is required")

    # Auth: require admin telegram_id (same pattern as other admin endpoints)
    cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    admin_ids_raw = cfg.get("admin_telegram_ids", "") or os.environ.get("ADMIN_TELEGRAM_IDS", "") or os.environ.get("ADMIN_ID", "")
    admin_ids = [str(x).strip() for x in str(admin_ids_raw).split(",") if str(x).strip()]
    if admin_ids and telegram_id and str(telegram_id) not in admin_ids:
        raise HTTPException(status_code=403, detail="Not authorized")

    # 1. Find order — search across ALL relevant collections
    order = None
    order_collection_name = "orders"

    # Search in orders collection first (exact match)
    order = await db.db.orders.find_one({"order_id": order_id})

    # Fallback: partial match in orders (last 8 chars)
    if not order and len(order_id) >= 6:
        order = await db.db.orders.find_one({"order_id": {"$regex": re.escape(order_id[-8:]), "$options": "i"}})

    # Fallback: search in premium_checkout (new orders often land here first)
    if not order:
        order_collection_name = "premium_checkout"
        order = await db.db.premium_checkout.find_one({"order_id": order_id})
        if not order:
            order = await db.db.premium_checkout.find_one({"track_id": order_id})
        if not order and len(order_id) >= 6:
            order = await db.db.premium_checkout.find_one({"order_id": {"$regex": re.escape(order_id[-8:]), "$options": "i"}})

    # Fallback: search in premium_purchases (paid/completed records)
    if not order:
        order_collection_name = "premium_purchases"
        order = await db.db.premium_purchases.find_one({"order_id": order_id})
        if not order and len(order_id) >= 6:
            order = await db.db.premium_purchases.find_one({"order_id": {"$regex": re.escape(order_id[-8:]), "$options": "i"}})

    # Fallback: search in purchases collection (older records)
    if not order:
        order_collection_name = "purchases"
        order = await db.db.purchases.find_one({"order_id": order_id})

    if not order:
        return {"success": False, "found": False, "message": f"No order found with ID: {order_id}"}

    user_id = order.get("user_id")

    # 3. Resolve story names if not stored
    story_ids = order.get("story_ids", [])
    story_names = order.get("story_names", [])
    if story_ids and not story_names:
        from bson.objectid import ObjectId
        for sid in story_ids:
            try:
                doc = await db.db.premium_stories.find_one({"_id": ObjectId(sid)})
                if doc:
                    story_names.append(doc.get("story_name_en", doc.get("title", sid)))
            except Exception:
                story_names.append(sid)

    # 4. Try to get buyer profile from purchases collection
    buyer_profile = {}
    if user_id:
        buyer_doc = await db.db.purchases.find_one({"user_id": user_id})
        if not buyer_doc:
            buyer_doc = await db.db.purchases.find_one({"user_id": str(user_id)})
        if buyer_doc:
            buyer_profile = {
                "first_name": buyer_doc.get("first_name", order.get("first_name", "")),
                "last_name": buyer_doc.get("last_name", order.get("last_name", "")),
                "username": buyer_doc.get("username", order.get("username", "")),
                "user_id": user_id,
            }
        else:
            buyer_profile = {
                "first_name": order.get("first_name", ""),
                "last_name": order.get("last_name", ""),
                "username": order.get("username", ""),
                "user_id": user_id,
            }

    # 5. Build clean response (exclude MongoDB _id)
    order_out = {
        "order_id":      order.get("order_id", order_id),
        "status":        order.get("status", "unknown"),
        "amount":        order.get("total", order.get("amount", 0)),
        "story_ids":     story_ids,
        "story_names":   story_names,
        "upi_id_shown":  order.get("upi_id_shown", ""),  # exact UPI shown to user
        "utr":           order.get("utr", ""),
        "source":        order.get("source", ""),
        "promo_code":    order.get("promo_code", ""),
        "created_at":    str(order.get("created_at", order.get("paid_at", ""))),
        "paid_at":       str(order.get("paid_at", "")),
        "invoice_number": order.get("invoice_number", ""),
        "user_id":       user_id,
        "username":      order.get("username", ""),
        "first_name":    order.get("first_name", ""),
    }

    logger.info(f"admin_order_lookup: found order {order_id} status={order_out['status']} user={user_id}")
    return {
        "success": True,
        "found": True,
        "order": order_out,
        "buyer": buyer_profile,
    }


class ManualPurchase(BaseModel):
    telegram_id: str
    user_id: Union[int, str]
    first_name: str
    username: str
    story_id: str
    amount: float
    source: Optional[str] = "miniapp"
    method: Optional[str] = "UPI (QR)"
    utr_number: Optional[str] = None
    custom_date: Optional[str] = None
    skip_notification: Optional[bool] = False

@api_router.post("/admin/manual-purchase")
async def manual_purchase(data: ManualPurchase):
    global _stories_cache
    from AryaPremium.config import Config
    try:
        user_id_int = int(data.telegram_id) if str(data.telegram_id).isdigit() else data.telegram_id
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
        target_uid = int(data.user_id) if str(data.user_id).isdigit() else data.user_id
        target_uid_int = int(target_uid) if str(target_uid).isdigit() else 0
        uid_filter = [target_uid, str(target_uid)]
        if target_uid_int:
            uid_filter.append(target_uid_int)

        source_raw = str(data.source or "miniapp").lower()
        source_label = "bot" if "bot" in source_raw else "miniapp"
        method_label = data.method or "UPI (QR)"
        utr_clean = str(data.utr_number or "").strip() if data.utr_number else None

        # Parse custom creation/purchase date if provided
        created_dt = datetime.now(timezone.utc)
        if data.custom_date and str(data.custom_date).strip():
            c_str = str(data.custom_date).strip()
            try:
                dt_parsed = datetime.fromisoformat(c_str)
                if dt_parsed.tzinfo is None:
                    dt_parsed = dt_parsed.replace(tzinfo=timezone.utc)
                created_dt = dt_parsed
            except Exception:
                try:
                    # Try YYYY-MM-DD
                    dt_parsed = datetime.strptime(c_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    created_dt = dt_parsed
                except Exception:
                    pass
        
        # ── DUPLICATE CHECK: If user already has a paid/approved order for this story, skip creating another ──
        existing_order = await arya_db.db.orders.find_one({
            "user_id": {"$in": uid_filter},
            "story_ids": story_id_str,
            "status": {"$in": ["paid", "approved", "completed", "delivered"]}
        })
        if existing_order:
            logger.info(f"Manual purchase: order already exists for user {target_uid}, story {story_id_str} — skipping duplicate order creation")
            # Still ensure user record is up-to-date
            await arya_db.db.users.update_one(
                {"id": {"$in": uid_filter}},
                {"$addToSet": {"purchases": story_id_str}},
            )
            purchase_record = {
                "user_id": target_uid,
                "story_id": story_id_str,
                "title": story.get("story_name_en", story.get("title", "")),
                "source": source_label,
                "method": method_label,
                "order_id": existing_order.get("order_id") or f"AM-EXISTING-{story_id_str[-6:]}",
                "purchased_at": created_dt,
                "paid_at": created_dt.isoformat()
            }
            await arya_db.db.premium_purchases.update_one(
                {"user_id": {"$in": uid_filter}, "story_id": story_id_str},
                {"$set": purchase_record},
                upsert=True
            )
            await arya_db.db.purchases.update_one(
                {"user_id": {"$in": uid_filter}, "story_id": story_id_str},
                {"$set": purchase_record},
                upsert=True
            )

            # Send Bot DM Purchase Success Message to user
            if not data.skip_notification:
                asyncio.create_task(send_purchase_success_dm(
                    arya_db,
                    target_uid,
                    order_doc=existing_order,
                    payment_method=method_label,
                    verified_by="Access Granted By Team",
                    is_admin_manual=True
                ))

            _stories_cache = None
            return {"success": True, "source": source_label, "note": "already_exists"}

        # Upsert user
        user = await arya_db.db.users.find_one({"id": {"$in": uid_filter}})
        if not user:
            await arya_db.db.users.insert_one({
                "id": target_uid,
                "first_name": data.first_name,
                "username": data.username,
                "purchases": [story_id_str],
                "joined_date": created_dt
            })
        else:
            await arya_db.db.users.update_one(
                {"id": {"$in": uid_filter}},
                {"$addToSet": {"purchases": story_id_str}}
            )
            
        # Insert Order with explicit source and method
        manual_oid = await _make_arya_order_id(arya_db, str(target_uid), [story_id_str], source=source_label)
        order_doc = {
            "order_id": manual_oid,
            "user_id": target_uid,
            "username": data.username,
            "first_name": data.first_name,
            "story_ids": [story_id_str],
            "story_names": [story.get("story_name_en", story.get("title", story_id_str))],
            "total": data.amount,
            "method": method_label,
            "status": "paid",
            "source": source_label,
            "created_at": created_dt,
            "paid_at": created_dt
        }
        if utr_clean:
            order_doc["utr"] = utr_clean
            order_doc["utr_number"] = utr_clean

        await arya_db.db.orders.insert_one(order_doc)

        # Delete any pending/obsolete checkouts or pending orders for this user & story so no duplicate Razorpay/pending orders remain
        st_ids_to_clean = list(filter(None, [story_id_str, str(story.get("story_id", "")), str(story.get("_id", ""))]))
        await arya_db.db.premium_checkout.delete_many({
            "user_id": {"$in": uid_filter},
            "story_id": {"$in": st_ids_to_clean}
        })
        await arya_db.db.orders.delete_many({
            "user_id": {"$in": uid_filter},
            "story_ids": {"$in": st_ids_to_clean},
            "status": {"$in": ["pending", "created", "processing", "waiting_screenshot", "pending_gateway"]}
        })
        invalidate_buyers_cache()

        # Record 12-digit UTR in used_utrs collection if provided
        if utr_clean and len(utr_clean) == 12 and utr_clean.isdigit():
            try:
                await arya_db.db.used_utrs.update_one(
                    {"utr": utr_clean},
                    {"$set": {
                        "utr": utr_clean,
                        "user_id": target_uid,
                        "order_id": manual_oid,
                        "story_id": story_id_str,
                        "amount": data.amount,
                        "method": method_label,
                        "added_by": "admin",
                        "used_at": created_dt
                    }},
                    upsert=True
                )
            except Exception as utr_err:
                logger.warning(f"Failed to save UTR to used_utrs: {utr_err}")

        # Upsert into premium_purchases and purchases so all endpoints see it instantly
        purchase_record = {
            "user_id": target_uid,
            "story_id": story_id_str,
            "title": story.get("story_name_en", story.get("title", "")),
            "source": source_label,
            "method": method_label,
            "order_id": manual_oid,
            "purchased_at": created_dt,
            "paid_at": created_dt.isoformat()
        }
        await arya_db.db.premium_purchases.update_one(
            {"user_id": {"$in": uid_filter}, "story_id": story_id_str},
            {"$set": purchase_record},
            upsert=True
        )
        await arya_db.db.purchases.update_one(
            {"user_id": {"$in": uid_filter}, "story_id": story_id_str},
            {"$set": purchase_record},
            upsert=True
        )
        
        # Log payment to channel
        asyncio.create_task(trigger_payment_log_from_order(order_doc))

        # Send Bot DM Purchase Success Message to user!
        if not data.skip_notification:
            asyncio.create_task(send_purchase_success_dm(
                arya_db,
                target_uid,
                order_doc=order_doc,
                payment_method=method_label,
                verified_by="Access Granted By Team",
                is_admin_manual=True
            ))
        
        _stories_cache = None

        return {"success": True, "source": source_label}
    except Exception as e:
        logger.error(f"Manual purchase error: {e}")
        raise HTTPException(500, detail=str(e))

@api_router.post("/admin/buyers/{user_id}/action")
async def admin_buyer_action(telegram_id: str, user_id: str, payload: dict):
    from AryaPremium.config import Config
    try:
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
            
        action = payload.get("action")
        target_uid = int(user_id) if user_id.isdigit() else user_id
        target_uid_int = int(target_uid) if str(target_uid).isdigit() else 0
        uid_filter = [target_uid, str(target_uid)]
        if target_uid_int:
            uid_filter.append(target_uid_int)

        arya_db = app.state.db
        
        if action == "wipe":
            await arya_db.db.users.delete_many({"id": {"$in": uid_filter}})
            await arya_db.db.orders.delete_many({"user_id": {"$in": uid_filter}})
            await arya_db.db.premium_checkout.delete_many({"user_id": {"$in": uid_filter}})
            await arya_db.db.premium_purchases.delete_many({"user_id": {"$in": uid_filter}})
            await arya_db.db.purchases.delete_many({"user_id": {"$in": uid_filter}})
            await arya_db.db.support_tickets.delete_many({"telegram_id": {"$in": uid_filter}})
            await arya_db.db.story_requests.delete_many({"telegram_id": {"$in": uid_filter}})
            await arya_db.db.user_tickets.delete_many({"telegram_id": {"$in": uid_filter}})
            await arya_db.db.premium_feedback.delete_many({"telegram_id": {"$in": uid_filter}})
            await arya_db.db.premium_requests.delete_many({"telegram_id": {"$in": uid_filter}})
            invalidate_buyers_cache()
            return {"success": True, "message": "User data wiped completely."}
            
        elif action == "ban":
            await arya_db.db.orders.delete_many({"user_id": {"$in": uid_filter}})
            await arya_db.db.premium_checkout.delete_many({"user_id": {"$in": uid_filter}})
            await arya_db.db.premium_purchases.delete_many({"user_id": {"$in": uid_filter}})
            await arya_db.db.purchases.delete_many({"user_id": {"$in": uid_filter}})
            await arya_db.db.users.update_one(
                {"id": {"$in": uid_filter}},
                {"$set": {"banned": True, "purchases": [], "ban_reason": "Admin ban via Web App"}},
                upsert=True
            )
            invalidate_buyers_cache()
            return {"success": True, "message": "User wiped and banned."}

        elif action == "delete_order":
            order_id_str = payload.get("order_id") or payload.get("story_id")
            if order_id_str:
                from bson.objectid import ObjectId
                id_filter = [order_id_str]
                try: id_filter.append(ObjectId(order_id_str))
                except Exception: pass

                await arya_db.db.orders.delete_many({"order_id": {"$in": id_filter}})
                await arya_db.db.orders.delete_many({"_id": {"$in": id_filter}})
                await arya_db.db.premium_checkout.delete_many({"order_id": {"$in": id_filter}})
                await arya_db.db.premium_checkout.delete_many({"_id": {"$in": id_filter}})
                await arya_db.db.premium_checkout.delete_many({"track_id": {"$in": id_filter}})
            invalidate_buyers_cache()
            return {"success": True, "message": "Order deleted successfully."}
            
        elif action == "remove_story":
            story_id_str = payload.get("story_id")
            if not story_id_str:
                raise HTTPException(status_code=400, detail="Missing story_id")

            if story_id_str == "all":
                await arya_db.db.users.update_one(
                    {"id": {"$in": uid_filter}},
                    {"$set": {"purchases": []}}
                )
                await arya_db.db.premium_purchases.delete_many({"user_id": {"$in": uid_filter}})
                await arya_db.db.purchases.delete_many({"user_id": {"$in": uid_filter}})
                await arya_db.db.orders.delete_many({"user_id": {"$in": uid_filter}})
                await arya_db.db.premium_checkout.delete_many({"user_id": {"$in": uid_filter}})
                invalidate_buyers_cache()
                return {"success": True, "message": "All story access removed from user."}
            
            # 1. Pull the story_id from users collection purchases
            await arya_db.db.users.update_one(
                {"id": {"$in": uid_filter}},
                {"$pull": {"purchases": story_id_str}}
            )
            
            # 2. Delete the record from premium_purchases, purchases, premium_checkout & orders
            from bson.objectid import ObjectId
            story_id_filter = [story_id_str]
            try:
                story_id_filter.append(ObjectId(story_id_str))
            except Exception:
                pass

            await arya_db.db.premium_purchases.delete_many({
                "user_id": {"$in": uid_filter},
                "story_id": {"$in": story_id_filter}
            })
            await arya_db.db.purchases.delete_many({
                "user_id": {"$in": uid_filter},
                "story_id": {"$in": story_id_filter}
            })
            await arya_db.db.premium_checkout.delete_many({
                "user_id": {"$in": uid_filter},
                "story_id": {"$in": story_id_filter}
            })
            await arya_db.db.orders.delete_many({
                "user_id": {"$in": uid_filter},
                "story_ids": {"$in": story_id_filter}
            })
            await arya_db.db.orders.delete_many({
                "user_id": {"$in": uid_filter},
                "story_id": {"$in": story_id_filter}
            })
                    
            invalidate_buyers_cache()
            
            global _stories_cache
            _stories_cache = None
            
            return {"success": True, "message": "Story removed successfully from user."}
            
        raise HTTPException(status_code=400, detail="Invalid action")
    except Exception as e:
        logger.error(f"Buyer action error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.post("/admin/buyers/bulk-action")
async def admin_bulk_buyer_action(telegram_id: str, payload: dict):
    try:
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
            
        action = payload.get("action")
        user_ids = payload.get("user_ids", [])
        story_id = payload.get("story_id", "all")
        if not user_ids:
            raise HTTPException(status_code=400, detail="No user_ids provided")

        uid_filter = []
        for uid in user_ids:
            target_uid = int(uid) if str(uid).isdigit() else uid
            uid_filter.extend([target_uid, str(target_uid)])
            if isinstance(target_uid, int):
                uid_filter.append(target_uid)

        arya_db = app.state.db

        if action in ("wipe", "delete_orders"):
            await arya_db.db.users.delete_many({"id": {"$in": uid_filter}})
            await arya_db.db.orders.delete_many({"user_id": {"$in": uid_filter}})
            await arya_db.db.premium_checkout.delete_many({"user_id": {"$in": uid_filter}})
            await arya_db.db.premium_purchases.delete_many({"user_id": {"$in": uid_filter}})
            await arya_db.db.purchases.delete_many({"user_id": {"$in": uid_filter}})
            await arya_db.db.support_tickets.delete_many({"telegram_id": {"$in": uid_filter}})
            await arya_db.db.story_requests.delete_many({"telegram_id": {"$in": uid_filter}})
            await arya_db.db.user_tickets.delete_many({"telegram_id": {"$in": uid_filter}})
            await arya_db.db.premium_feedback.delete_many({"telegram_id": {"$in": uid_filter}})
            await arya_db.db.premium_requests.delete_many({"telegram_id": {"$in": uid_filter}})
            invalidate_buyers_cache()
            return {"success": True, "message": f"Bulk action '{action}' completed for {len(user_ids)} users."}

        elif action == "remove_story" and story_id == "all":
            await arya_db.db.users.update_many(
                {"id": {"$in": uid_filter}},
                {"$set": {"purchases": []}}
            )
            await arya_db.db.premium_purchases.delete_many({"user_id": {"$in": uid_filter}})
            await arya_db.db.purchases.delete_many({"user_id": {"$in": uid_filter}})
            await arya_db.db.orders.delete_many({"user_id": {"$in": uid_filter}})
            await arya_db.db.premium_checkout.delete_many({"user_id": {"$in": uid_filter}})
            invalidate_buyers_cache()
            return {"success": True, "message": f"Story access removed for {len(user_ids)} users."}

        raise HTTPException(status_code=400, detail="Invalid bulk action")
    except Exception as e:
        logger.error(f"Bulk buyer action error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.delete("/admin/requests/{request_id}")
async def delete_admin_request(request_id: str, telegram_id: str = ""):
    if not is_admin(str(telegram_id)):
        raise HTTPException(status_code=403, detail="Not authorized")
    arya_db = app.state.db
    from bson.objectid import ObjectId
    try:
        req_oid = ObjectId(request_id)
        await arya_db.db.premium_requests.delete_one({"_id": req_oid})
        await arya_db.db.premium_feedback.delete_one({"_id": req_oid})
    except Exception:
        await arya_db.db.premium_requests.delete_one({"id": request_id})
        await arya_db.db.premium_feedback.delete_one({"id": request_id})
    return {"success": True, "message": "Story request deleted successfully"}

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


UNIVERSAL_CARRIER_PREFIXES = {
    # Jio CGNAT ranges (India)
    "49.32.", "49.33.", "49.34.", "49.35.", "49.36.", "49.37.",
    "49.44.", "49.45.", "49.46.", "49.47.",
    "157.32.", "157.33.", "157.34.", "157.35.", "157.36.", "157.37.",
    "157.38.", "157.39.", "157.40.", "157.41.",
    "103.57.", "103.58.", "103.59.",
    # Jio IPv6 ranges (starts with 2409:40)
    "2409:40",
    # Airtel CGNAT ranges
    "182.68.", "182.69.", "182.70.", "182.71.", "182.72.",
    "49.205.", "49.206.", "49.207.",
    "122.160.", "122.161.", "122.162.", "122.163.", "122.164.", "122.165.",
    # Airtel IPv6 ranges
    "2401:49", "2402:3a", "2402:81",
    # BSNL
    "117.193.", "117.194.", "117.195.", "117.196.",
    "110.224.", "110.225.", "110.226.", "110.227.",
    # Vodafone India
    "202.138.", "202.139.",
    "27.4.", "27.5.", "27.6.", "27.7.",
    # Vi (Idea)
    "203.101.", "203.102.",
}

def _is_universal_ip(ip: str) -> bool:
    """Returns True if IP is a known shared CGNAT carrier IP (Jio/Airtel/BSNL/Vodafone India) or local."""
    if not ip:
        return False
    ip = ip.strip().lower()
    if _is_private_or_local_ip(ip):
        return True
    for prefix in UNIVERSAL_CARRIER_PREFIXES:
        if ip.startswith(prefix):
            return True
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
async def get_admin_settings(request: Request, telegram_id: str):
    """Returns current feature toggle settings for the Mini App."""
    from AryaPremium.config import Config
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized")
        arya_db = app.state.db
        cfg = await arya_db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
        
        is_owner_flag = await is_request_owner(request)
        
        # Load from premium_promo_codes collection
        db_promos = await arya_db.db.premium_promo_codes.find().to_list(length=1000)
        promo_codes_list = []
        for p in db_promos:
            promo_codes_list.append({
                "code": p.get("code"),
                "type": p.get("type", "percentage"),
                "value": p.get("value", 0.0),
                "active": p.get("active", True),
                "story_id": p.get("story_id", "global"),
                "expires_at": p.get("expires_at"),
                "usage_limit": p.get("usage_limit"),
                "usage_count": p.get("usage_count", 0),
                "description": p.get("description", ""),
                "auto_apply": bool(p.get("auto_apply", False)),
                "min_cart_items": p.get("min_cart_items"),
                "min_order_amount": p.get("min_order_amount"),
                "user_target": p.get("user_target", "all"),
                "user_limit": p.get("user_limit"),
                "story_status_target": p.get("story_status_target", "all"),
                "payment_method_target": p.get("payment_method_target", "all"),
                "target_story_ids": p.get("target_story_ids", [])
            })
            
        return {
            "success": True,
            "data": {
                "mini_app_enabled": cfg.get("mini_app_enabled", True),
                "tnc_enabled": cfg.get("tnc_enabled", True),
                "support_feedback_enabled": cfg.get("support_feedback_enabled", True),
                "hide_support_feedback": cfg.get("hide_support_feedback", not cfg.get("support_feedback_enabled", True)),
                "razorpay_fee_percent": cfg.get("razorpay_fee_percent", 2.36),
                "razorpay_fee_enabled": cfg.get("razorpay_fee_enabled", True),
                "platform_fee_amount": cfg.get("platform_fee_amount", 5.0),
                "platform_fee_enabled": cfg.get("platform_fee_enabled", True),
                "promo_codes": promo_codes_list,
                "outpaint_enabled": cfg.get("outpaint_enabled", False),
                "outpaint_provider": cfg.get("outpaint_provider", "replicate"),
                "replicate_api_key": cfg.get("replicate_api_key", ""),
                "fal_api_key": cfg.get("fal_api_key", ""),
                "stability_api_key": cfg.get("stability_api_key", ""),
                "razorpay_disabled": cfg.get("razorpay_disabled", False),
                "razorpay_status": cfg.get("razorpay_status", "disabled" if cfg.get("razorpay_disabled", False) else "active"),
                "upi_manual_enabled": cfg.get("upi_manual_enabled", True),
                "upi_id": cfg.get("upi_id", ""),
                "upi_payee_name": cfg.get("upi_payee_name", "Arya Premium"),
                "upi_id_2": cfg.get("upi_id_2", ""),
                "upi_payee_name_2": cfg.get("upi_payee_name_2", ""),
                "upi_id_3": cfg.get("upi_id_3", ""),
                "upi_payee_name_3": cfg.get("upi_payee_name_3", ""),
                "upi_id_4": cfg.get("upi_id_4", ""),
                "upi_payee_name_4": cfg.get("upi_payee_name_4", ""),
                "gmail_verification_enabled": cfg.get("gmail_verification_enabled", False),
                "gmail_user": cfg.get("gmail_user", ""),
                "gmail_app_password": cfg.get("gmail_app_password", ""),
                "mint_theme_enabled": cfg.get("mint_theme_enabled", False),
                "paytm_status": cfg.get("paytm_status", "hidden"),
                "paytm_mid": cfg.get("paytm_mid", ""),
                "paytm_merchant_key": cfg.get("paytm_merchant_key", ""),
                "paytm_website": cfg.get("paytm_website", "DEFAULT"),
                "paytm_callback_url": cfg.get("paytm_callback_url", "https://aryapremium.store/api/paytm-callback"),
                "paytm_env": cfg.get("paytm_env", "staging"),
                "payu_status": cfg.get("payu_status", "hidden"),
                "payu_merchant_key": cfg.get("payu_merchant_key", ""),
                "payu_merchant_salt": cfg.get("payu_merchant_salt", ""),
                "payu_callback_url": cfg.get("payu_callback_url", "https://aryapremium.store/api/payu-callback"),
                "payu_env": cfg.get("payu_env", "sandbox"),
                "cashfree_status": cfg.get("cashfree_status", "hidden"),
                "cashfree_app_id": cfg.get("cashfree_app_id", "") or cfg.get("cashfree_api_id", ""),
                "cashfree_api_id": cfg.get("cashfree_api_id", "") or cfg.get("cashfree_app_id", ""),
                "cashfree_secret_key": cfg.get("cashfree_secret_key", ""),
                "cashfree_callback_url": cfg.get("cashfree_callback_url", "https://sliceurl.app/api/cashfree-callback"),
                "cashfree_return_url": cfg.get("cashfree_return_url", "https://isaythanks.vercel.app"),
                "cashfree_env": cfg.get("cashfree_env", "sandbox"),
                "dodopayments_status": cfg.get("dodopayments_status", "hidden"),
                "dodopayments_api_key": cfg.get("dodopayments_api_key", ""),
                "dodopayments_environment": cfg.get("dodopayments_environment", "test"),
                "dodopayments_product_id": cfg.get("dodopayments_product_id", ""),
                "dodopayments_webhook_secret": cfg.get("dodopayments_webhook_secret", ""),
                "is_owner": is_owner_flag,
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
        if "support_feedback_enabled" in payload:
            val = bool(payload["support_feedback_enabled"])
            update_fields["support_feedback_enabled"] = val
            update_fields["hide_support_feedback"] = not val
        if "hide_support_feedback" in payload:
            val = bool(payload["hide_support_feedback"])
            update_fields["hide_support_feedback"] = val
            update_fields["support_feedback_enabled"] = not val
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
        if "razorpay_disabled" in payload:
            update_fields["razorpay_disabled"] = bool(payload["razorpay_disabled"])
        if "razorpay_status" in payload:
            rzp_st = str(payload["razorpay_status"]).strip().lower()
            update_fields["razorpay_status"] = rzp_st
            if rzp_st in ["disabled", "hidden"]:
                update_fields["razorpay_disabled"] = True
            else:
                update_fields["razorpay_disabled"] = False
        if "upi_manual_enabled" in payload:
            update_fields["upi_manual_enabled"] = bool(payload["upi_manual_enabled"])
        if "upi_id" in payload:
            update_fields["upi_id"] = str(payload["upi_id"]).strip()
        if "upi_payee_name" in payload:
            update_fields["upi_payee_name"] = str(payload["upi_payee_name"]).strip()
        if "upi_id_2" in payload:
            update_fields["upi_id_2"] = str(payload["upi_id_2"]).strip()
        if "upi_payee_name_2" in payload:
            update_fields["upi_payee_name_2"] = str(payload["upi_payee_name_2"]).strip()
        if "upi_id_3" in payload:
            update_fields["upi_id_3"] = str(payload["upi_id_3"]).strip()
        if "upi_payee_name_3" in payload:
            update_fields["upi_payee_name_3"] = str(payload["upi_payee_name_3"]).strip()
        if "upi_id_4" in payload:
            update_fields["upi_id_4"] = str(payload["upi_id_4"]).strip()
        if "upi_payee_name_4" in payload:
            update_fields["upi_payee_name_4"] = str(payload["upi_payee_name_4"]).strip()
        if "gmail_verification_enabled" in payload:
            update_fields["gmail_verification_enabled"] = bool(payload["gmail_verification_enabled"])
        if "gmail_user" in payload:
            update_fields["gmail_user"] = str(payload["gmail_user"]).strip()
        if "gmail_app_password" in payload:
            update_fields["gmail_app_password"] = str(payload["gmail_app_password"]).strip()
        if "mint_theme_enabled" in payload:
            update_fields["mint_theme_enabled"] = bool(payload["mint_theme_enabled"])
        if "paytm_status" in payload:
            update_fields["paytm_status"] = str(payload["paytm_status"]).strip()
        if "paytm_mid" in payload:
            update_fields["paytm_mid"] = str(payload["paytm_mid"]).strip()
        if "paytm_merchant_key" in payload:
            update_fields["paytm_merchant_key"] = str(payload["paytm_merchant_key"]).strip()
        if "paytm_website" in payload:
            update_fields["paytm_website"] = str(payload["paytm_website"]).strip()
        if "paytm_callback_url" in payload:
            update_fields["paytm_callback_url"] = str(payload["paytm_callback_url"]).strip()
        if "paytm_env" in payload:
            update_fields["paytm_env"] = str(payload["paytm_env"]).strip()

        if "payu_status" in payload:
            update_fields["payu_status"] = str(payload["payu_status"]).strip()
        if "payu_merchant_key" in payload:
            update_fields["payu_merchant_key"] = str(payload["payu_merchant_key"]).strip()
        if "payu_merchant_salt" in payload:
            update_fields["payu_merchant_salt"] = str(payload["payu_merchant_salt"]).strip()
        if "payu_callback_url" in payload:
            update_fields["payu_callback_url"] = str(payload["payu_callback_url"]).strip()
        if "payu_env" in payload:
            update_fields["payu_env"] = str(payload["payu_env"]).strip()

        if "cashfree_status" in payload:
            update_fields["cashfree_status"] = str(payload["cashfree_status"]).strip()
        if "cashfree_app_id" in payload:
            update_fields["cashfree_app_id"] = str(payload["cashfree_app_id"]).strip()
            update_fields["cashfree_api_id"] = str(payload["cashfree_app_id"]).strip()
        if "cashfree_api_id" in payload:
            update_fields["cashfree_api_id"] = str(payload["cashfree_api_id"]).strip()
            update_fields["cashfree_app_id"] = str(payload["cashfree_api_id"]).strip()
        if "cashfree_secret_key" in payload:
            update_fields["cashfree_secret_key"] = str(payload["cashfree_secret_key"]).strip()
        if "cashfree_callback_url" in payload:
            update_fields["cashfree_callback_url"] = str(payload["cashfree_callback_url"]).strip()
        if "cashfree_return_url" in payload:
            update_fields["cashfree_return_url"] = str(payload["cashfree_return_url"]).strip()
        if "cashfree_env" in payload:
            update_fields["cashfree_env"] = str(payload["cashfree_env"]).strip()

        if "dodopayments_status" in payload:
            update_fields["dodopayments_status"] = str(payload["dodopayments_status"]).strip()
        if "dodopayments_api_key" in payload:
            update_fields["dodopayments_api_key"] = str(payload["dodopayments_api_key"]).strip()
        if "dodopayments_environment" in payload:
            update_fields["dodopayments_environment"] = str(payload["dodopayments_environment"]).strip()
        if "dodopayments_product_id" in payload:
            update_fields["dodopayments_product_id"] = str(payload["dodopayments_product_id"]).strip()
        if "dodopayments_webhook_secret" in payload:
            update_fields["dodopayments_webhook_secret"] = str(payload["dodopayments_webhook_secret"]).strip()
        
        # Merge promo codes directly in the collection
        if "promo_codes" in payload:
            raw_codes = payload["promo_codes"]
            promo_codes_to_save = []
            if isinstance(raw_codes, list):
                for pc in raw_codes:
                    if isinstance(pc, dict) and "code" in pc:
                        code_upper = str(pc["code"]).strip().upper()
                        promo_codes_to_save.append({
                            "code": code_upper,
                            "type": str(pc.get("type", "percentage")),
                            "value": float(pc.get("value", 0.0)),
                            "active": bool(pc.get("active", True)),
                            "story_id": str(pc.get("story_id", "global")).strip() if pc.get("story_id") else "global",
                            "expires_at": str(pc.get("expires_at")).strip() if pc.get("expires_at") else None,
                            "usage_limit": int(pc["usage_limit"]) if pc.get("usage_limit") is not None and str(pc["usage_limit"]).isdigit() else None,
                            "description": str(pc.get("description", "")).strip(),
                            "auto_apply": bool(pc.get("auto_apply", False)),
                            "min_cart_items": int(pc["min_cart_items"]) if pc.get("min_cart_items") is not None and str(pc["min_cart_items"]).isdigit() else None,
                            "min_order_amount": float(pc["min_order_amount"]) if pc.get("min_order_amount") is not None and str(pc["min_order_amount"]).replace(".", "", 1).isdigit() and float(pc["min_order_amount"]) > 0 else None,
                            "user_target": str(pc.get("user_target", "all")),
                            "user_limit": int(pc["user_limit"]) if pc.get("user_limit") is not None and str(pc["user_limit"]).isdigit() else None,
                            "story_status_target": str(pc.get("story_status_target", "all")).strip().lower() if pc.get("story_status_target") else "all",
                            "payment_method_target": str(pc.get("payment_method_target", "all")).strip().lower() if pc.get("payment_method_target") else "all",
                            "target_story_ids": pc.get("target_story_ids", []) if isinstance(pc.get("target_story_ids"), list) else []
                        })
            
            # Fetch existing codes to merge
            existing_promos = await arya_db.db.premium_promo_codes.find().to_list(length=1000)
            existing_dict = {p["code"]: p for p in existing_promos}
            codes_in_payload = {p["code"] for p in promo_codes_to_save}
            
            # Delete promo codes not in payload
            for code in existing_dict:
                if code not in codes_in_payload:
                    await arya_db.db.premium_promo_codes.delete_one({"code": code})
                    
            # Upsert payload promo codes
            for p in promo_codes_to_save:
                db_p = existing_dict.get(p["code"])
                if db_p:
                    # Update fields, preserve usage_count
                    await arya_db.db.premium_promo_codes.update_one(
                        {"code": p["code"]},
                        {"$set": {
                            "type": p["type"],
                            "value": p["value"],
                            "active": p["active"],
                            "story_id": p["story_id"],
                            "expires_at": p["expires_at"],
                            "usage_limit": p["usage_limit"],
                            "description": p["description"],
                            "auto_apply": p["auto_apply"],
                            "min_cart_items": p["min_cart_items"],
                            "min_order_amount": p.get("min_order_amount"),
                            "user_target": p["user_target"],
                            "user_limit": p["user_limit"],
                            "story_status_target": p["story_status_target"],
                            "payment_method_target": p["payment_method_target"],
                            "target_story_ids": p["target_story_ids"]
                        }}
                    )
                else:
                    # Insert new promo code
                    p["usage_count"] = 0
                    await arya_db.db.premium_promo_codes.insert_one(p)

        # Do not save promo codes inside the global feature toggles configuration to prevent duplication
        if "promo_codes" in update_fields:
            del update_fields["promo_codes"]

        # If there are other fields, update config
        if update_fields:
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
        
        # Load and filter active, unexpired, unexhausted promo codes
        now = datetime.now(timezone.utc)
        db_promos = await arya_db.db.premium_promo_codes.find({"active": True}).to_list(length=1000)
        promo_codes_list = []
        for p in db_promos:
            # Check usage limit
            u_limit = p.get("usage_limit")
            if u_limit is not None:
                u_count = p.get("usage_count", 0)
                if u_count >= u_limit:
                    continue
                    
            # Check expiration
            expires_at = p.get("expires_at")
            if expires_at:
                if isinstance(expires_at, str):
                    try:
                        expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                    except ValueError:
                        pass
                if isinstance(expires_at, datetime):
                    if now > expires_at:
                        continue
                        
            promo_codes_list.append({
                "code": p.get("code"),
                "type": p.get("type", "percentage"),
                "value": p.get("value", 0.0),
                "active": True,
                "story_id": p.get("story_id", "global"),
                "expires_at": p.get("expires_at"),
                "usage_limit": p.get("usage_limit"),
                "usage_count": p.get("usage_count", 0),
                "description": p.get("description", ""),
                "auto_apply": bool(p.get("auto_apply", False)),
                "min_cart_items": p.get("min_cart_items"),
                "min_order_amount": p.get("min_order_amount"),
                "user_target": p.get("user_target", "all"),
                "user_limit": p.get("user_limit"),
                "story_status_target": p.get("story_status_target", "all"),
                "payment_method_target": p.get("payment_method_target", "all"),
                "target_story_ids": p.get("target_story_ids", [])
            })
            
        return {
            "success": True,
            "mini_app_enabled": cfg.get("mini_app_enabled", True),
            "tnc_enabled": cfg.get("tnc_enabled", True),
            "support_feedback_enabled": cfg.get("support_feedback_enabled", True),
            "hide_support_feedback": cfg.get("hide_support_feedback", not cfg.get("support_feedback_enabled", True)),
            "razorpay_fee_percent": cfg.get("razorpay_fee_percent", 2.36),
            "razorpay_fee_enabled": cfg.get("razorpay_fee_enabled", True),
            "platform_fee_amount": cfg.get("platform_fee_amount", 5.0),
            "platform_fee_enabled": cfg.get("platform_fee_enabled", True),
            "promo_codes": promo_codes_list,
            "razorpay_disabled": cfg.get("razorpay_disabled", False),
            "razorpay_status": cfg.get("razorpay_status", "disabled" if cfg.get("razorpay_disabled", False) else "active"),
            "upi_manual_enabled": cfg.get("upi_manual_enabled", True),
            "upi_id": cfg.get("upi_id", "") or os.environ.get("UPI_ID", ""),
            "upi_payee_name": cfg.get("upi_payee_name", "") or os.environ.get("UPI_PAYEE_NAME", "") or "Arya Premium",
            "upi_id_2": cfg.get("upi_id_2", ""),
            "upi_payee_name_2": cfg.get("upi_payee_name_2", ""),
            "upi_id_3": cfg.get("upi_id_3", ""),
            "upi_payee_name_3": cfg.get("upi_payee_name_3", ""),
            "upi_id_4": cfg.get("upi_id_4", ""),
            "upi_payee_name_4": cfg.get("upi_payee_name_4", ""),
            "gmail_verification_enabled": cfg.get("gmail_verification_enabled", False),
            "mint_theme_enabled": cfg.get("mint_theme_enabled", False),
            "paytm_status": cfg.get("paytm_status", "hidden"),
            "paytm_mid": cfg.get("paytm_mid", ""),
            "payu_status": cfg.get("payu_status", "hidden"),
            "payu_merchant_key": cfg.get("payu_merchant_key", ""),
            "payu_callback_url": cfg.get("payu_callback_url", "https://aryapremium.store/api/payu-callback"),
            "payu_env": cfg.get("payu_env", "sandbox"),
            "cashfree_status": cfg.get("cashfree_status", "hidden"),
            "cashfree_app_id": cfg.get("cashfree_app_id", "") or cfg.get("cashfree_api_id", ""),
            "cashfree_api_id": cfg.get("cashfree_api_id", "") or cfg.get("cashfree_app_id", ""),
            "cashfree_env": cfg.get("cashfree_env", "sandbox"),
            "dodopayments_status": cfg.get("dodopayments_status", "hidden"),
            "dodopayments_environment": cfg.get("dodopayments_environment", "test"),
            "dodopayments_product_id": cfg.get("dodopayments_product_id", ""),
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
            "promo_codes": [],
            "razorpay_disabled": False,
            "upi_manual_enabled": True,
            "upi_id": os.environ.get("UPI_ID", ""),
            "upi_payee_name": os.environ.get("UPI_PAYEE_NAME", "Arya Premium"),
            "upi_id_2": "",
            "upi_payee_name_2": "",
            "upi_id_3": "",
            "upi_payee_name_3": "",
            "upi_id_4": "",
            "upi_payee_name_4": "",
            "gmail_verification_enabled": False,
            "paytm_status": "hidden",
            "paytm_mid": "",
            "payu_status": "hidden",
            "payu_merchant_key": "",
            "payu_callback_url": "https://aryapremium.store/api/payu-callback",
            "payu_env": "sandbox",
            "cashfree_status": "hidden",
            "cashfree_app_id": "",
            "cashfree_api_id": "",
            "cashfree_env": "sandbox",
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
    """Sends active security log updates to the Telegram channels (ARYA_LOGS_CHANNEL)."""
    from AryaPremium.config import Config
    token = getattr(Config, "MGMT_BOT_TOKEN", None) or os.environ.get("MGMT_BOT_TOKEN")
    channel_id = getattr(Config, "ARYA_LOGS_CHANNEL", None) or getattr(Config, "PAYMENT_LOGS_CHANNEL", None) or os.environ.get("ARYA_LOGS_CHANNEL")
    if not token:
        logger.warning("[AryaLog] log_to_telegram: MGMT_BOT_TOKEN is not set — cannot send log.")
        return
    if not channel_id:
        logger.warning("[AryaLog] log_to_telegram: ARYA_LOGS_CHANNEL is not set — cannot send log.")
        return
    try:
        import aiohttp
        # Ensure channel_id is an integer if it looks like one
        chat_id_val = channel_id
        try:
            chat_id_val = int(str(channel_id).strip())
        except (ValueError, TypeError):
            pass  # Keep as string (username like @mychannel)

        async with aiohttp.ClientSession() as session:
            resp = await session.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id_val,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }
            )
            resp_data = await resp.json()
            if not resp_data.get("ok"):
                err_desc = resp_data.get("description", "Unknown error")
                err_code = resp_data.get("error_code", "?")
                logger.error(
                    f"[AryaLog] Telegram API rejected log to channel '{chat_id_val}': "
                    f"[{err_code}] {err_desc}. "
                    f"Ensure MGMT bot is an ADMIN of ARYA_LOGS_CHANNEL with 'Post Messages' permission."
                )
            else:
                logger.debug(f"[AryaLog] log_to_telegram: message sent to channel {chat_id_val}")
    except Exception as e:
        logger.error(f"[AryaLog] Failed to send Telegram log (network/config error): {e}")

def escape_html(text: str) -> str:
    """Escapes HTML special characters for safe inclusion in Telegram messages."""
    if not isinstance(text, str):
        return str(text) if text is not None else ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

async def fetch_user_details_from_tg(telegram_id: int) -> dict:
    """
    Fetches the user's profile information (first_name, last_name, username)
    directly from Telegram using the getChat API method.
    Also updates the local MongoDB users database.
    """
    from AryaPremium.config import Config
    token = getattr(Config, "MGMT_BOT_TOKEN", None) or os.environ.get("MGMT_BOT_TOKEN")
    if not token or not telegram_id:
        return {}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"https://api.telegram.org/bot{token}/getChat?chat_id={telegram_id}")
            if r.status_code == 200:
                res = r.json()
                if res.get("ok"):
                    chat = res.get("result", {})
                    first_name = chat.get("first_name", "")
                    last_name = chat.get("last_name", "")
                    username = chat.get("username", "")
                    
                    # Update local database
                    arya_db = app.state.db
                    if arya_db:
                        await arya_db.db.users.update_one(
                            {"id": int(telegram_id)},
                            {"$set": {
                                "first_name": first_name,
                                "last_name": last_name,
                                "username": username,
                                "last_active": datetime.now(timezone.utc)
                            }},
                            upsert=True
                        )
                    return {"first_name": first_name, "last_name": last_name, "username": username}
    except Exception as e:
        logger.error(f"Failed to fetch user details from Telegram for {telegram_id}: {e}")
    return {}

async def trigger_payment_log_from_order(order: dict):
    """
    Sends a formatted receipt of the purchase to the PAYMENT_LOGS_CHANNEL.
    """
    from AryaPremium.config import Config
    token = getattr(Config, "MGMT_BOT_TOKEN", None) or os.environ.get("MGMT_BOT_TOKEN")
    channel_id = getattr(Config, "PAYMENT_LOGS_CHANNEL", None) or os.environ.get("PAYMENT_LOGS_CHANNEL")
    if not token or not channel_id:
        logger.warning("MGMT_BOT_TOKEN or PAYMENT_LOGS_CHANNEL not configured, skipping payment log")
        return

    try:
        arya_db = app.state.db
        if not arya_db:
            return

        order_id = str(order.get("order_id") or "")
        if not order_id or order_id.startswith("PASS-"):
            logger.info(f"Skipping Arya Premium payment log for pass order: {order_id}")
            return

        res = await arya_db.db.orders.find_one_and_update(
            {"order_id": order_id, "payment_log_sent": {"$ne": True}},
            {"$set": {"payment_log_sent": True}}
        )
        if not res:
            logger.info(f"Payment log already sent or sending for order {order_id}, skipping duplicate log request.")
            return

        tg_id = order.get("user_id")
        if not tg_id:
            return

        tg_id_int = int(tg_id) if str(tg_id).isdigit() else 0

        # Query user details from DB / Telegram
        user_first_name = order.get("first_name", "")
        user_last_name = ""
        username = order.get("username", "")

        # Try to resolve user details robustly
        resolved = await fetch_user_details_from_tg(tg_id_int)
        if resolved:
            user_first_name = resolved.get("first_name") or user_first_name
            user_last_name = resolved.get("last_name") or ""
            username = resolved.get("username") or username
        elif tg_id_int:
            user_doc = await arya_db.db.users.find_one({"id": tg_id_int})
            if user_doc:
                if not username:
                    username = user_doc.get("username", "")
                user_first_name = user_doc.get("first_name") or user_first_name
                user_last_name = user_doc.get("last_name") or ""

        # Clean username helper
        def clean_username(uname: str) -> str:
            if not uname:
                return ""
            uname_lower = uname.strip().lower()
            if uname_lower in ("", "unknown", "none", "@unknown", "@none"):
                return ""
            if uname.startswith("@"):
                return uname[1:].strip()
            return uname.strip()

        cleaned_username = clean_username(username)

        # Clean name helper
        def clean_name(first: str, last: str) -> str:
            name = f"{first or ''} {last or ''}".strip()
            name_lower = name.lower()
            if not name or name_lower in ("unknown", "none", "null", "undefined"):
                return "User"
            return name

        full_name = escape_html(clean_name(user_first_name, user_last_name))
        tg_link = f"tg://user?id={tg_id}"

        if cleaned_username:
            user_display = f'<a href="{tg_link}">{full_name}</a> (@{escape_html(cleaned_username)})'
        else:
            user_display = f'<a href="{tg_link}">{full_name}</a>'

        # Join story names
        story_names = order.get("story_names", [])
        if not story_names:
            story_ids = order.get("story_ids", [])
            from bson.objectid import ObjectId
            for sid in story_ids:
                try:
                    story = await arya_db.db.premium_stories.find_one({"_id": ObjectId(sid)})
                    if story:
                        story_names.append(story.get("story_name_en", story.get("title", "")))
                except Exception:
                    pass
        story_names_str = ", ".join(story_names) if story_names else "N/A"
        story_names_str = escape_html(story_names_str)

        # Payment details
        amount = order.get("total") or order.get("amount_paid", 0)
        source = order.get("source", "")
        
        # Check order's method field first!
        method_val = str(order.get("method") or order.get("payment_method") or "").strip().lower()
        if not method_val:
            if "oxapay" in source.lower() or "crypto" in source.lower():
                method_val = "oxapay"
            elif "upi_manual" in source.lower() or "upi" in source.lower():
                method_val = "manual_upi"
            elif "dodo" in source.lower():
                method_val = "dodopayments"
            elif "cashfree" in source.lower():
                method_val = "cashfree"
            elif "manual" in source.lower():
                method_val = "manual_admin"
            else:
                method_val = "razorpay"

        # Map method_val to badge keys
        method = "razorpay"
        if "oxapay" in method_val or "crypto" in method_val:
            method = "oxapay"
        elif "upi" in method_val:
            method = "upi"
        elif "dodo" in method_val:
            method = "dodopayments"
        elif "cashfree" in method_val:
            method = "cashfree"
        elif "paytm" in method_val:
            method = "paytm"
        elif "payu" in method_val:
            method = "payu"
        elif "easebuzz" in method_val:
            method = "easebuzz"
        elif "manual" in method_val or "admin" in method_val:
            method = "manual_admin"
        else:
            method = method_val

        method_badge = {
            "razorpay":     "💳 Razorpay (Automatic)",
            "cashfree":     "💳 Cashfree (Automatic)",
            "dodopayments": "🦤 Dodo Payments (Automatic)",
            "easebuzz":     "💸 Easebuzz (Automatic)",
            "paytm":        "📱 Paytm (Automatic)",
            "payu":         "💰 PayU (Automatic)",
            "upi":          "🏦 Manual UPI (QR)",
            "manual_upi":   "🏦 Manual UPI (QR)",
            "oxapay":       "🪙 Oxapay (Crypto)",
            "crypto":       "🪙 Oxapay (Crypto)",
            "manual_admin": "👑 Manual Admin",
        }.get(method.lower(), method.capitalize())

        receipt_id = order.get("razorpay_payment_id") or order.get("payment_id") or order.get("track_id") or order.get("razorpay_order_id") or order.get("utr") or ""

        from datetime import datetime, timezone, timedelta
        ist = timezone(timedelta(hours=5, minutes=30))
        time_str = datetime.now(ist).strftime('%d %b %Y, %I:%M %p IST')

        link_line = ""
        pay_link = order.get("payment_link_url") or ""
        if pay_link and "razorpay" in method.lower():
            link_line = f"\n<b>Payment Link:</b> <a href=\"{pay_link}\">View Receipt</a>"

        caption = (
            f"<b>✅ PAYMENT CONFIRMED (MINI APP)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>❖ Order ID:</b> <code>{order.get('order_id') or 'N/A'}</code>\n"
            f"<b>❖ User:</b> {user_display}\n"
            f"<b>❖ Telegram ID:</b> <code>{tg_id}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>❖ Story:</b> {story_names_str}\n"
            f"<b>❖ Amount Paid:</b> ₹{amount}\n"
            f"<b>❖ Method:</b> {method_badge}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>❖ Receipt / Gateway ID:</b>\n<code>{receipt_id or 'N/A'}</code>"
            f"{link_line}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>❖ Time:</b> {time_str}"
        )
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": int(channel_id),
                    "text": caption,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True
                }
            )
    except Exception as e:
        logger.error(f"Failed to send payment log via Telegram API: {e}", exc_info=True)


async def record_purchased_stories(order: dict):
    """
    Creates premium_purchases audit records for all stories in the order.
    This ensures that when a user requests delivery from the bot,
    the bot can find the purchase records and properly log the deliveries.
    """
    try:
        if str(order.get("order_id", "")).startswith("PASS-"):
            return
        arya_db = app.state.db
        if not arya_db:
            return
            
        tg_id = order.get("user_id")
        if not tg_id:
            return
            
        tg_id_int = int(tg_id) if str(tg_id).isdigit() else 0
        if not tg_id_int:
            return
            
        story_ids = order.get("story_ids", [])
        if not story_ids:
            return
        from bson.objectid import ObjectId
        
        # Get default bot_id
        default_bot_id = None
        try:
            bot_username = os.environ.get("BOT_USERNAME", "UseAryaBot")
            bot_doc = await arya_db.db.premium_bots.find_one({"username": bot_username})
            if bot_doc:
                default_bot_id = bot_doc.get("id") or bot_doc.get("bot_id")
        except Exception as e:
            logger.error(f"Failed to query default bot_id: {e}")
            
        # Only record full story purchases in premium_purchases table
        full_story_sids = set()
        if order.get("items") and isinstance(order["items"], list):
            for itm in order["items"]:
                if itm.get("is_full", True) and not itm.get("part_id") and not itm.get("part_name"):
                    sid_clean = str(itm.get("story_id") or itm.get("id") or "").strip()
                    if sid_clean:
                        full_story_sids.add(sid_clean)
        elif not order.get("part_id") and not order.get("part_name"):
            full_story_sids = set(str(s).strip() for s in story_ids if str(s).strip())
        else:
            full_story_sids = set()

        if not full_story_sids:
            # All items were specific parts, securely tracked in orders collection
            return

        total_order_amt = float(order.get("total", 0) or order.get("amount", 0) or 0)
        num_stories = max(1, len(full_story_sids))
        per_story_amt = round(total_order_amt / num_stories, 2) if num_stories > 1 else total_order_amt
        pay_method = str(order.get("payment_method") or order.get("method") or order.get("gateway") or "UPI").upper()
        ref_id = str(order.get("reference") or order.get("utr") or order.get("razorpay_payment_id") or order.get("payment_id") or order.get("cf_order_id") or order.get("track_id") or "").strip()
        ord_id = str(order.get("order_id") or order.get("cf_order_id") or "").strip()

        for sid in full_story_sids:
            try:
                story = await arya_db.db.premium_stories.find_one({"_id": ObjectId(sid)})
                if not story:
                    story = await arya_db.db.premium_stories.find_one({"story_id": sid})
                    
                bot_id = None
                if story:
                    bot_id = story.get("bot_id")
                if not bot_id:
                    bot_id = default_bot_id
                    
                existing = await arya_db.db.premium_purchases.find_one({
                    "user_id": tg_id_int,
                    "story_id": ObjectId(sid)
                })
                if not existing:
                    await arya_db.db.premium_purchases.insert_one({
                        "user_id": tg_id_int,
                        "story_id": ObjectId(sid),
                        "bot_id": bot_id,
                        "purchased_at": datetime.now(timezone.utc),
                        "source": order.get("source", "miniapp"),
                        "method": pay_method,
                        "amount": per_story_amt,
                        "reference": ref_id,
                        "order_id": ord_id
                    })
            except Exception as e:
                logger.error(f"Failed to record story purchase for {sid}: {e}", exc_info=True)
                
        # Increment usage count for the promo code if used in the completed order
        promo_code = order.get("promo_code")
        if promo_code:
            try:
                await arya_db.db.premium_promo_codes.update_one(
                    {"code": str(promo_code).strip().upper()},
                    {"$inc": {"usage_count": 1}}
                )
                logger.info(f"Incremented usage count for promo code: {promo_code}")
            except Exception as pe:
                logger.error(f"Failed to increment usage count for promo code {promo_code}: {pe}")
    except Exception as e:
        logger.error(f"Error in record_purchased_stories: {e}", exc_info=True)


async def send_purchase_receipt_to_user(order: dict):
    """Sends standardized Purchase Complete DM to user - same format as UPI/manual payments."""
    try:
        if str(order.get("order_id", "")).startswith("PASS-"):
            return
        arya_db = app.state.db
        user_id = order.get("user_id")
        if not user_id:
            return

        order_id = order.get("order_id")
        if order_id and arya_db and hasattr(arya_db, "db"):
            res = await arya_db.db.orders.find_one_and_update(
                {"order_id": order_id, "receipt_sent": {"$ne": True}},
                {"$set": {"receipt_sent": True}}
            )
            if not res:
                chk = await arya_db.db.orders.find_one({"order_id": order_id, "receipt_sent": True})
                if chk:
                    logger.info(f"[Receipt DM] Receipt already sent for order_id={order_id}, skipping.")
                    return

        from purchase_dm_helper import send_purchase_success_dm
        pm = order.get("payment_method") or order.get("source") or "Dodo Payments"
        await send_purchase_success_dm(
            db=arya_db,
            user_id=user_id,
            order_doc=order,
            payment_method=pm,
            verified_by="Auto Verified By System"
        )
    except Exception as e:
        logger.error(f"Failed to send purchase receipt to user {order.get('user_id')}: {e}", exc_info=True)

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

async def is_request_owner(request: Request) -> bool:
    """Helper to verify if a request is from a supreme owner (via verified Telegram ID or active owner session email)."""
    db = getattr(app.state, "db", None)
    if not db:
        return False

    # Check X-Telegram-Init-Data
    init_data = request.headers.get("X-Telegram-Init-Data", "").strip()
    if init_data:
        tokens_to_check = await get_all_valid_bot_tokens(db)
        for tok in tokens_to_check:
            valid_data = verify_telegram_web_app_data(init_data, tok)
            if valid_data:
                try:
                    import json
                    user_data = json.loads(valid_data.get("user", "{}"))
                    validated_id = user_data.get("id")
                    if validated_id and int(validated_id) in Config.OWNER_IDS:
                        return True
                except Exception:
                    pass
                break

    # Check X-Admin-Session email in OWNER_EMAILS env variable
    session_token = request.headers.get("X-Admin-Session")
    if session_token:
        session = await db.db.admin_sessions.find_one({
            "session_token": session_token,
            "active": True,
            "expires_at": {"$gt": datetime.now(timezone.utc)}
        })
        if session:
            email = session.get("email", "").strip().lower()
            owner_emails_env = os.environ.get("OWNER_EMAILS", "")
            owner_emails = [e.strip().lower() for e in owner_emails_env.replace(",", " ").split() if e.strip()]
            if email and email in owner_emails:
                return True

    # Fallback to query parameter telegram_id check (ONLY if they have an active admin session, to prevent spoofing)
    tg_id_str = request.query_params.get("telegram_id")
    if tg_id_str:
        try:
            tg_id = int(tg_id_str)
            from AryaPremium.config import Config
            if tg_id in Config.OWNER_IDS:
                if session_token:
                    session = await db.db.admin_sessions.find_one({
                        "session_token": session_token,
                        "active": True,
                        "expires_at": {"$gt": datetime.now(timezone.utc)}
                    })
                    if session:
                        return True
        except ValueError:
            pass

    return False

@app.middleware("http")
async def ban_guard_middleware(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)
        
    path = request.url.path
    
    # Exempt admin, analytics, and webhook routes from visitor ban guard
    is_admin_or_webhook = ("/admin/" in path) or ("/analytics/" in path) or ("/webhook" in path) or ("/razorpay-callback" in path)
    if is_admin_or_webhook:
        response = await call_next(request)
        return response
        
    # Guard all API / context routes
    is_api = path.startswith("/api/") or path.startswith("/stories") or ("/app-context" in path)
    is_auth_endpoint = "/admin/auth/" in path
    
    if is_api and not is_auth_endpoint:
        db = getattr(app.state, "db", None)
        if db:
            ip = _client_ip_from_request(request)
            ip = ip.strip() if ip else ""
            device_id = request.headers.get("X-Device-Id", "").strip()
            
            # Read initData
            init_data = request.headers.get("X-Telegram-Init-Data", "").strip()
            
            # Resolve Telegram ID
            tg_id = None
            tg_id_str = request.query_params.get("telegram_id")
            if tg_id_str:
                try:
                    tg_id = int(tg_id_str)
                except ValueError:
                    pass
                    
            if not tg_id and request.method in ("POST", "PUT", "DELETE"):
                try:
                    body = await request.body()
                    async def receive():
                        return {"type": "http.request", "body": body, "more_body": False}
                    request._receive = receive
                    
                    import json
                    payload = json.loads(body.decode("utf-8"))
                    tg_id_str = payload.get("telegram_id")
                    if tg_id_str:
                        tg_id = int(tg_id_str)
                except Exception:
                    pass
                    
            # Load parent config dynamically to avoid naming conflict
            root_db_name = "arya"
            try:
                import os
                import importlib.util
                this_dir = os.path.dirname(os.path.abspath(__file__))
                if os.path.basename(this_dir) == "AryaPremium":
                    parent_dir = os.path.dirname(this_dir)
                else:
                    parent_dir = this_dir
                parent_config_path = os.path.join(parent_dir, "config.py")
                if os.path.exists(parent_config_path):
                    spec = importlib.util.spec_from_file_location("root_config", parent_config_path)
                    root_config_module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(root_config_module)
                    RootConfig = root_config_module.Config
                    root_db_name = getattr(RootConfig, "DATABASE_NAME", "arya")
            except Exception as e:
                logger.warning(f"Failed to load parent config dynamically: {e}")

            tokens_to_check = await get_all_valid_bot_tokens(db)

            # Crypto validation logic
            if init_data:
                valid_data = None
                for tok in tokens_to_check:
                    valid_data = verify_telegram_web_app_data(init_data, tok)
                    if valid_data:
                        break
                
                if not valid_data:
                    # Forged or invalid signature - block immediately!
                    logger.warning(f"Blocked request due to invalid/forged Telegram signature: {path}")
                    import json
                    return Response(
                        content=json.dumps({
                            "banned": True,
                            "reason": "Security verification failed (Invalid Session)",
                            "detail": "BANNED"
                        }),
                        status_code=503,
                        media_type="application/json"
                    )
                else:
                    try:
                        import json
                        user_data = json.loads(valid_data.get("user", "{}"))
                        validated_id = user_data.get("id")
                        if validated_id:
                            # Override client-resolved ID with the cryptographically verified one to prevent spoofing
                            if tg_id and tg_id != int(validated_id):
                                logger.warning(f"ID spoofing attempt blocked! Resolved: {tg_id}, Validated: {validated_id}")
                            tg_id = int(validated_id)
                    except Exception as e:
                        logger.error(f"Error parsing validated user data: {e}")
            else:
                # No initData provided (old frontend or direct API call).
                pass

            # 1. SUPREME OWNER / ADMIN EXEMPTION
            is_exempt = False
            if tg_id and Config.OWNER_IDS and tg_id in Config.OWNER_IDS:
                is_exempt = True
            else:
                # Fallback: check if the request contains a valid admin session
                session_token = request.headers.get("X-Admin-Session")
                if session_token:
                    session = await db.db.admin_sessions.find_one({
                        "session_token": session_token,
                        "active": True,
                        "expires_at": {"$gt": datetime.now(timezone.utc)}
                    })
                    if session:
                        is_exempt = True
            
            if is_exempt:
                response = await call_next(request)
                return response
            
            # 2. CHECK BLOCKED STATUS
            is_local = not ip or ip in ("unknown", "127.0.0.1", "::1") or ip.startswith(("192.168.", "10.", "172."))
            banned_by_ip = None
            if not is_local and not _is_universal_ip(ip):
                banned_by_ip = await db.db.premium_bans.find_one({"ips": ip, "status": {"$in": ["banned", "flagged"]}})
                
            banned_by_device = None
            if device_id:
                banned_by_device = await db.db.premium_bans.find_one({"device_ids": device_id, "status": {"$in": ["banned", "flagged"]}})
                
            banned_by_tg = None
            is_paid_user = False
            if tg_id:
                # Paid User Immunity: check if the user has purchased any stories
                try:
                    purchase_doc = await db.db.premium_purchases.find_one({"user_id": int(tg_id)})
                    if purchase_doc:
                        is_paid_user = True
                except Exception as e:
                    logger.error(f"Error checking paid user status: {e}")

                banned_by_tg = await db.db.premium_bans.find_one({"_id": tg_id, "status": {"$in": ["banned", "flagged"]}})
                
                # Check root/parent database ban status
                banned_in_root = False
                root_ban_reason = "Banned by administrator"
                try:
                    if root_db_name:
                        root_db = db.client[root_db_name]
                        root_user = await root_db.users.find_one({"id": tg_id})
                        if root_user and root_user.get("ban_status", {}).get("is_banned"):
                            banned_in_root = True
                            root_ban_reason = root_user.get("ban_status", {}).get("ban_reason") or "Banned by administrator"
                except Exception as e:
                    logger.warning(f"Failed to check root ban status: {e}")
                    
                if banned_in_root and not banned_by_tg:
                    # Auto-propagate ban to premium_bans collection to enable multi-vector protection
                    await db.db.premium_bans.update_one(
                        {"_id": tg_id},
                        {"$set": {
                            "reason": root_ban_reason,
                            "status": "banned",
                            "name": f"User {tg_id}"
                        }},
                        upsert=True
                    )
                    banned_by_tg = {
                        "_id": tg_id,
                        "reason": root_ban_reason,
                        "status": "banned",
                        "name": f"User {tg_id}"
                    }                
            # 3. ENFORCE BLOCKS
            # We strictly DO NOT block or auto-ban innocent users based solely on IP addresses
            # because mobile carrier networks use shared/dynamic CGNAT IPs.
            is_blocked = False
            # Paying users are immune to device-based auto-bans and auto-unblocked if auto-banned!
            if is_paid_user:
                if banned_by_tg:
                    tg_reason = str(banned_by_tg.get("reason", "")).lower()
                    if "auto-ban" in tg_reason or "alt of" in tg_reason or "strike" in tg_reason or "rapid" in tg_reason or "evasion" in tg_reason or banned_by_tg.get("status") == "flagged":
                        # Auto-unban paid user on the fly!
                        await db.db.premium_bans.delete_one({"_id": tg_id})
                        await db.db.users.update_one({"id": tg_id}, {"$set": {"ban_status": {"is_banned": False, "ban_reason": ""}, "banned": False}})
                        is_blocked = False
                        banned_by_tg = None
                    else:
                        is_blocked = True
                else:
                    is_blocked = False
                
                # Also clean paid user's device/IP from banned_by_device / banned_by_ip records if present
                if banned_by_device:
                    try:
                        await db.db.premium_bans.update_one({"_id": banned_by_device["_id"]}, {"$pull": {"device_ids": device_id}})
                    except: pass
                if banned_by_ip and ip and not _is_universal_ip(ip):
                    try:
                        await db.db.premium_bans.update_one({"_id": banned_by_ip["_id"]}, {"$pull": {"ips": ip}})
                    except: pass
            else:
                if banned_by_tg:
                    is_blocked = True
                elif banned_by_device:
                    is_blocked = True
            
            if is_blocked:
                reason = "Access denied"
                target_tg_id = tg_id
                if not target_tg_id and banned_by_device:
                    target_tg_id = banned_by_device["_id"]
                user_name = "Banned User"
                
                # We will update the corresponding database document to add any new IPs or Device IDs they try to use.
                # However, we STRICTLY do not add universal/shared IPs to the blocked IPs array.
                update_fields = {}
                if ip and not is_local and not _is_universal_ip(ip):
                    update_fields["ips"] = ip
                if device_id:
                    update_fields["device_ids"] = device_id
                
                # Rule A: Banned user changing IP/Device (VPN Evasion)
                # Ignore VPN evasion check if they are on a universal/shared IP.
                if banned_by_tg and not banned_by_ip and not is_local and not _is_universal_ip(ip):
                    reason = banned_by_tg.get("reason", "Banned by administrator")
                    user_name = banned_by_tg.get("name", f"User {tg_id}")
                    if update_fields:
                        await db.db.premium_bans.update_one(
                            {"_id": tg_id},
                            {"$addToSet": update_fields}
                        )
                    from utils_ban_logger import log_premium_ban_activity
                    asyncio.create_task(log_premium_ban_activity(
                        user_id=tg_id,
                        name=user_name,
                        ip=ip,
                        action=f"App Open ({path})",
                        reason=f"VPN Evasion caught: User on new IP {ip} (added to blocklist)"
                    ))
                    
                # Rule B: New/unbanned Telegram ID on blocked Device (Alt account)
                # Note: We do NOT auto-ban on blocked IP alone, only on blocked Device.
                elif banned_by_device and tg_id and not banned_by_tg and not is_paid_user:
                    reason = f"Auto-ban: Alternative account detected on blocked Device"
                    user_name = f"Alt of User {banned_by_device['_id']}"
                        
                    # Auto-ban this Telegram ID (strictly banned)
                    await db.db.premium_bans.update_one(
                        {"_id": tg_id},
                        {"$set": {
                            "reason": reason,
                            "status": "banned",
                            "banned_at": datetime.now(timezone.utc),
                            "name": user_name
                        }, "$addToSet": update_fields if update_fields else {"device_ids": device_id}},
                        upsert=True
                    )
                    # Propagate to Delivery Bot ban list
                    await db.db.users.update_one(
                        {"id": tg_id},
                        {"$set": {"ban_status": {"is_banned": True, "ban_reason": reason}}},
                        upsert=True
                    )
                    from utils_ban_logger import log_premium_ban_activity
                    asyncio.create_task(log_premium_ban_activity(
                        user_id=tg_id,
                        name=user_name,
                        ip=ip,
                        action=f"App Open ({path})",
                        reason=f"Alt account caught on blocked Device (Telegram ID banned automatically)"
                    ))
                    
                # Default Block Logging
                else:
                    ref_doc = banned_by_tg or banned_by_device
                    reason = ref_doc.get("reason", "Banned by administrator")
                    user_name = ref_doc.get("name", f"User {target_tg_id}")
                    
                    if target_tg_id and update_fields:
                        await db.db.premium_bans.update_one(
                            {"_id": target_tg_id},
                            {"$addToSet": update_fields}
                        )
                        
                    from utils_ban_logger import log_premium_ban_activity
                    asyncio.create_task(log_premium_ban_activity(
                        user_id=target_tg_id,
                        name=user_name,
                        ip=ip,
                        action=f"App Open ({path})",
                        reason=f"Blocked request from banned user/Device: {reason}"
                    ))
                    
                # Save blocked access log in database
                await db.db.premium_ban_activity.insert_one({
                    "telegram_id": target_tg_id,
                    "ip": ip,
                    "timestamp": datetime.now(timezone.utc),
                    "action": f"App Open ({path})",
                    "reason": reason,
                    "name": user_name
                })
                
                import json
                return Response(
                    content=json.dumps({
                        "banned": True,
                        "reason": reason,
                        "detail": "BANNED"
                    }),
                    status_code=503,
                    media_type="application/json"
                )

    return await call_next(request)

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
                
        if not authenticated:
            tg_id = request.query_params.get("telegram_id") or request.headers.get("X-Telegram-Id") or request.headers.get("telegram_id")
            if tg_id and (is_admin(str(tg_id)) or str(tg_id) == "0"):
                authenticated = True
                admin_authenticated_session.set(True)

        if not authenticated:
            return Response(
                content='{"detail":"Unauthorized: Admin session required"}',
                status_code=401,
                media_type="application/json"
            )
            
    response = await call_next(request)
    return response

# ─── Paage (Link-in-Bio) API Endpoints ──────────────────────────────────────────

@api_router.get("/paage/public/{username}")
async def get_paage_public_profile(username: str):
    db = getattr(app.state, "db", None)
    clean_username = username.lstrip("@").lower().strip()
    default_profile = {
        "username": clean_username,
        "display_name": clean_username.capitalize(),
        "bio": "Creator, builder, explorer.",
        "avatar_url": "https://images.unsplash.com/photo-1534528741775-53994a69daeb?w=400&auto=format&fit=crop&q=80",
        "theme": "default",
        "socials": {
            "github": f"https://github.com/{clean_username}",
            "twitter": f"https://x.com/{clean_username}"
        }
    }
    default_cards = [
        {
            "id": "card-1",
            "type": "link",
            "title": "SliceURL — Shorten & Monetize",
            "url": "https://sliceurl.app/",
            "content": "Supercharge your links with intelligent analytics.",
            "grid_span": "col-span-2",
            "order_index": 1
        },
        {
            "id": "card-2",
            "type": "note",
            "title": "Welcome to Paage",
            "content": "Create your own customizable Bento grid profile in seconds.",
            "grid_span": "col-span-1",
            "order_index": 2
        },
        {
            "id": "card-3",
            "type": "video",
            "title": "Featured Stream",
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "grid_span": "col-span-1",
            "order_index": 3
        }
    ]

    if not db:
        return {"ok": True, "profile": default_profile, "cards": default_cards}

    try:
        prof = await db.db.paage_profiles.find_one({"username": clean_username}, {"_id": 0})
        if not prof:
            return {"ok": True, "profile": default_profile, "cards": default_cards}
        
        cards = await db.db.paage_cards.find({"profile_id": clean_username}).sort("order_index", 1).to_list(length=100)
        clean_cards = []
        for c in cards:
            c.pop("_id", None)
            clean_cards.append(c)

        return {"ok": True, "profile": prof, "cards": clean_cards or default_cards}
    except Exception as e:
        return {"ok": True, "profile": default_profile, "cards": default_cards}


@api_router.post("/paage/profile")
async def save_paage_profile(payload: dict = Body(...)):
    db = getattr(app.state, "db", None)
    username = payload.get("username", "").lstrip("@").lower().strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")

    profile_data = {
        "username": username,
        "display_name": payload.get("display_name", username),
        "bio": payload.get("bio", ""),
        "avatar_url": payload.get("avatar_url", ""),
        "theme": payload.get("theme", "default"),
        "socials": payload.get("socials", {}),
        "updated_at": datetime.now(timezone.utc).isoformat()
    }

    if db:
        await db.db.paage_profiles.update_one(
            {"username": username},
            {"$set": profile_data},
            upsert=True
        )

    return {"ok": True, "profile": profile_data}



@api_router.post("/paage/cards")
async def save_paage_cards(payload: dict = Body(...)):
    db = getattr(app.state, "db", None)
    username = payload.get("username", "").lstrip("@").lower().strip()
    cards = payload.get("cards", [])

    if db and username:
        await db.db.paage_cards.delete_many({"profile_id": username})
        if cards:
            for idx, c in enumerate(cards):
                c["profile_id"] = username
                c["order_index"] = idx + 1
            await db.db.paage_cards.insert_many(cards)

    return {"ok": True, "count": len(cards)}

app.include_router(api_router, prefix="/api")
app.include_router(api_router) # Handle both /api/stories and /stories for Nginx proxy compatibility



@app.websocket("/api/ws/analytics")
async def analytics_websocket(
    websocket: WebSocket,
    telegram_id: str = Query(...),
    session_token: Optional[str] = Query(None)
):
    """Owner-only live event stream (JSON lines). Scale-out: replace hub with Redis."""
    from AryaPremium.config import Config
    from arya_enterprise_analytics import hub as _analytics_ws_hub
    from typing import Optional

    db = getattr(app.state, "db", None)
    authenticated = False
    if session_token and db:
        session = await db.db.admin_sessions.find_one({
            "session_token": session_token,
            "active": True,
            "expires_at": {"$gt": datetime.now(timezone.utc)}
        })
        if session:
            try:
                uid = int(telegram_id)
            except ValueError:
                uid = telegram_id
            if uid in Config.OWNER_IDS:
                authenticated = True

    if not authenticated:
        await websocket.close(code=4403)
        return

    await websocket.accept()
    await _analytics_ws_hub.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await _analytics_ws_hub.disconnect(websocket)

try:
    from paage_backend import paage_router
    app.include_router(paage_router, prefix="/api")
    app.include_router(paage_router)
except Exception as e:
    logger.warning(f"Failed to load paage_backend router: {e}")

# ─── Serve Front-End SPA Static Files & Catch-All Routes ──────────────────────
def get_dist_dir():
    _cur_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(_cur_dir, "..", "pocket-arya-store-new", "dist"),
        os.path.join(_cur_dir, "pocket-arya-store-new", "dist"),
        os.path.join(_cur_dir, "static_dist"),
        os.path.join(_cur_dir, "..", "AryaPremium", "static_dist"),
        os.path.join(_cur_dir, "dist"),
    ]
    for c in candidates:
        if os.path.exists(c) and (os.path.exists(os.path.join(c, "index.html")) or os.path.exists(os.path.join(c, "app.html"))):
            return os.path.abspath(c)
    return os.path.join(_cur_dir, "static_dist")

DIST_DIR = get_dist_dir()
logger.info(f"Serving SPA static assets from DIST_DIR: {DIST_DIR}")

@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    if full_path.startswith("api/") or full_path.startswith("ws/"):
        raise HTTPException(status_code=404, detail="API endpoint not found")
    
    # ── Path Traversal & Malicious Probe Filter ──
    bad_patterns = ["..", ".env", "etc/", "proc/", "root/", "bin/", "home/", ".git", ".ssh", ".aws", ".bash", ".zsh", "wallet", "Anchor.toml", "passwd", "shadow"]
    if any(p in full_path for p in bad_patterns):
        raise HTTPException(status_code=404, detail="Not Found")

    curr_dist = get_dist_dir()
    curr_dist_abs = os.path.abspath(curr_dist)

    # Check if target static file exists strictly within dist
    target_file = os.path.abspath(os.path.join(curr_dist, full_path))
    if target_file.startswith(curr_dist_abs) and os.path.exists(target_file) and os.path.isfile(target_file):
        headers = {}
        if full_path.startswith("assets/"):
            headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return FileResponse(target_file, headers=headers)
    
    # Check index.html, app.html or landing.html in dist
    no_cache_hdrs = {
        "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0"
    }
    from fastapi.responses import HTMLResponse
    for alt in ["index.html", "app.html", "landing.html"]:
        alt_file = os.path.join(curr_dist, alt)
        if os.path.exists(alt_file):
            try:
                with open(alt_file, "r", encoding="utf-8") as f:
                    content = f.read()
                return HTMLResponse(content=content, headers=no_cache_hdrs)
            except Exception:
                return FileResponse(alt_file, headers=no_cache_hdrs)
    
    return HTMLResponse(
        content="""<!DOCTYPE html>
<html>
<head><title>Arya Premium</title><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="background:#070709;color:#fff;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;">
<div style="text-align:center;padding:20px;">
<h1 style="font-size:2rem;margin-bottom:0.5rem;color:#FFD232;">Arya Premium</h1>
<p style="color:#a1a1aa;font-size:0.9rem;">Loading store assets...</p>
</div>
</body>
</html>""",
        status_code=200
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("mini_app_api:app", host="0.0.0.0", port=8000, reload=True)