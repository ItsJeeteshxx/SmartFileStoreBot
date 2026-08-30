import aiohttp
import logging
import uuid
import time
from database import db
from config import Config

logger = logging.getLogger("AryaCashfree")


async def get_cashfree_config() -> dict:
    """Fetches Cashfree credentials from MongoDB feature_toggles / config."""
    cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    enabled = cfg.get("cashfree_enabled", False)
    app_id = cfg.get("cashfree_app_id") or cfg.get("cashfree_api_id") or getattr(Config, "CASHFREE_APP_ID", "") or ""
    secret_key = cfg.get("cashfree_secret_key") or getattr(Config, "CASHFREE_SECRET_KEY", "") or ""
    env = (cfg.get("cashfree_env") or getattr(Config, "CASHFREE_ENV", "production") or "production").lower()
    
    base_url = "https://sandbox.cashfree.com/pg" if env == "sandbox" else "https://api.cashfree.com/pg"
    return {
        "enabled": bool(enabled and app_id and secret_key),
        "is_configured": bool(app_id and secret_key),
        "app_id": str(app_id).strip(),
        "secret_key": str(secret_key).strip(),
        "env": env,
        "base_url": base_url
    }


async def create_cashfree_order(user_id: int, user_name: str, story: dict, bot_username: str = "") -> dict:
    """
    Creates a Cashfree Payment Gateway order via Cashfree PG API (v2023-08-01).
    Returns dict with success: bool, payment_link: str, order_id: str, error: str.
    """
    cf_cfg = await get_cashfree_config()
    price = float(story.get("price", 0))
    story_id = str(story["_id"])
    story_name = story.get("story_name_en", "Story")
    order_id = f"cf_{user_id}_{int(time.time())}_{uuid.uuid4().hex[:6]}"

    if not cf_cfg["app_id"] or not cf_cfg["secret_key"]:
        # Fallback to Arya Premium Mini App payment screen wrapper
        miniapp_pay_link = f"https://aryapremium.store/app?story_id={story_id}&buy=cashfree&user_id={user_id}"
        return {
            "success": True,
            "order_id": order_id,
            "payment_link": miniapp_pay_link,
            "amount": price
        }

    price = float(story.get("price", 0))
    if price <= 0:
        return {"success": False, "error": "Invalid story price."}

    story_id = str(story["_id"])
    story_name = story.get("story_name_en", "Story")
    # Clean unique order id: cf_<user_id>_<timestamp>_<hex>
    order_id = f"cf_{user_id}_{int(time.time())}_{uuid.uuid4().hex[:6]}"

    headers = {
        "x-client-id": cf_cfg["app_id"],
        "x-client-secret": cf_cfg["secret_key"],
        "x-api-version": "2023-08-01",
        "Content-Type": "application/json"
    }

    clean_user_name = "".join(c for c in (user_name or "Buyer") if c.isalnum() or c in " _-")[:40] or "Buyer"
    
    payload = {
        "order_id": order_id,
        "order_amount": price,
        "order_currency": "INR",
        "customer_details": {
            "customer_id": f"tg_{user_id}",
            "customer_name": clean_user_name,
            "customer_email": f"user_{user_id}@aryapremium.store",
            "customer_phone": "9999999999"
        },
        "order_meta": {
            "return_url": f"https://t.me/{bot_username}?start=cf_{order_id}" if bot_username else None,
            "notify_url": "https://aryapremium.store/api/cashfree-webhook"
        },
        "order_note": f"Purchase of {story_name[:30]}"
    }

    url = f"{cf_cfg['base_url']}/orders"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=12.0)) as resp:
                data = await resp.json()
                if resp.status in (200, 201) and (data.get("payment_session_id") or data.get("order_id")):
                    payment_session_id = data.get("payment_session_id", "")
                    is_sb = (cf_cfg["env"] == "sandbox")
                    # Use Arya Premium Mini App animated wrapper for Cashfree JS SDK checkout
                    payment_link = (
                        f"https://aryapremium.store/api/cashfree-pay?session_id={payment_session_id}&sandbox={'true' if is_sb else 'false'}"
                        if payment_session_id else
                        (data.get("payment_link") or (data.get("payments", {}).get("url") if isinstance(data.get("payments"), dict) else None))
                    )
                    
                    # Store order in MongoDB
                    await db.db.orders.insert_one({
                        "order_id": order_id,
                        "cf_order_id": data.get("cf_order_id"),
                        "payment_session_id": payment_session_id,
                        "user_id": int(user_id),
                        "story_ids": [story_id],
                        "story_id": story_id,
                        "story_name": story_name,
                        "amount": price,
                        "currency": "INR",
                        "status": "pending",
                        "gateway": "cashfree",
                        "payment_link": payment_link,
                        "created_at": time.time()
                    })

                    return {
                        "success": True,
                        "order_id": order_id,
                        "payment_link": payment_link,
                        "payment_session_id": payment_session_id,
                        "amount": price
                    }
                else:
                    err_msg = data.get("message") or data.get("description") or str(data)
                    logger.error(f"Cashfree create order failed: {data}")
                    return {"success": False, "error": err_msg}
    except Exception as e:
        logger.error(f"Cashfree create order exception: {e}")
        miniapp_pay_link = f"https://aryapremium.store/app?story_id={story_id}&buy=cashfree&user_id={user_id}"
        return {
            "success": True,
            "order_id": order_id,
            "payment_link": miniapp_pay_link,
            "amount": price
        }


async def check_cashfree_order_status(order_id: str) -> dict:
    """
    Checks the status of a Cashfree order from Cashfree PG API.
    Returns dict with status: 'PAID' | 'ACTIVE' | 'FAILED' | 'EXPIRED', is_paid: bool, raw: dict.
    """
    cf_cfg = await get_cashfree_config()
    if not cf_cfg["app_id"] or not cf_cfg["secret_key"]:
        return {"status": "ERROR", "is_paid": False, "error": "Not configured."}

    headers = {
        "x-client-id": cf_cfg["app_id"],
        "x-client-secret": cf_cfg["secret_key"],
        "x-api-version": "2023-08-01"
    }

    url = f"{cf_cfg['base_url']}/orders/{order_id}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8.0)) as resp:
                data = await resp.json()
                if resp.status == 200:
                    order_status = (data.get("order_status") or "").upper()
                    is_paid = (order_status == "PAID")
                    return {
                        "status": order_status,
                        "is_paid": is_paid,
                        "amount": data.get("order_amount"),
                        "raw": data
                    }
                else:
                    return {"status": "ERROR", "is_paid": False, "error": data.get("message", "Check failed")}
    except Exception as e:
        logger.error(f"Cashfree order status check error: {e}")
        return {"status": "ERROR", "is_paid": False, "error": str(e)}
