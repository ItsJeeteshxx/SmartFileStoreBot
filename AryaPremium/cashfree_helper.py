import aiohttp
import logging
import uuid
import time
from database import db
from config import Config

logger = logging.getLogger("AryaCashfree")


async def get_cashfree_config(bot_cfg: dict = None) -> dict:
    """Fetches Cashfree credentials from bot_cfg or MongoDB feature_toggles / config."""
    try:
        from AryaPremium.database import db
    except ImportError:
        from database import db

    bot_cfg = bot_cfg or {}
    try:
        cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    except Exception as ex:
        logger.warning(f"[CF] Failed to fetch feature_toggles: {ex}")
        cfg = {}

    cf_status = str(cfg.get("cashfree_status", "")).strip().lower()
    cf_enabled_flag = cfg.get("cashfree_enabled", None)

    # Check bot-specific override
    bot_cf_flag = None
    if "pay_methods" in bot_cfg and isinstance(bot_cfg["pay_methods"], dict):
        bot_cf_flag = bot_cfg["pay_methods"].get("cashfree", None)
    if bot_cf_flag is None:
        bot_cf_flag = bot_cfg.get("pay_cashfree_enabled", bot_cfg.get("cashfree_enabled", None))

    app_id = (
        bot_cfg.get("cashfree_app_id")
        or cfg.get("cashfree_app_id") 
        or cfg.get("cashfree_api_id") 
        or getattr(Config, "CASHFREE_APP_ID", "") 
        or ""
    ).strip()

    secret_key = (
        bot_cfg.get("cashfree_secret_key")
        or cfg.get("cashfree_secret_key") 
        or getattr(Config, "CASHFREE_SECRET_KEY", "") 
        or ""
    ).strip()

    env = (
        bot_cfg.get("cashfree_env")
        or cfg.get("cashfree_env") 
        or getattr(Config, "CASHFREE_ENV", "production") 
        or "production"
    ).strip().lower()

    is_sandbox = (
        env in ("sandbox", "staging", "test")
        or "TEST" in app_id.upper()
        or "SANDBOX" in app_id.upper()
    )
    base_url = "https://sandbox.cashfree.com/pg" if is_sandbox else "https://api.cashfree.com/pg"
    is_configured = bool(app_id and secret_key)

    is_disabled = (cf_status in ("disabled", "hidden", "false", "0") or cf_enabled_flag is False)
    is_explicitly_enabled = (cf_status in ("active", "enabled", "visible", "true", "1") or cf_enabled_flag is True)

    # Global enabled status
    enabled = is_configured and (is_explicitly_enabled or (cf_status not in ("hidden", "disabled") and not is_disabled))

    # Apply bot-level toggle override if explicitly specified
    if bot_cf_flag is False:
        enabled = False
    elif bot_cf_flag is True and is_configured:
        enabled = True

    logger.info(f"[CF] Config: enabled={enabled}, is_configured={is_configured}, is_sandbox={is_sandbox}, env={env}, base_url={base_url}")
    return {
        "enabled": enabled,
        "is_configured": is_configured,
        "is_sandbox": is_sandbox,
        "app_id": app_id,
        "secret_key": secret_key,
        "env": "sandbox" if is_sandbox else "production",
        "base_url": base_url,
        "callback_url": cfg.get("cashfree_callback_url", "https://sliceurl.app/api/cashfree-callback"),
        "return_url": cfg.get("cashfree_return_url", "https://isaythanks.vercel.app")
    }


async def create_cashfree_order(user_id: int, user_name: str, story: dict, bot_username: str = "", bot_cfg: dict = None, order_id: str = None) -> dict:
    """
    Creates a Cashfree Payment Gateway order via Cashfree PG API (v2023-08-01).
    Primary strategy: Cashfree Payment Links API (POST /pg/links) -> generates direct official Cashfree hosted link.
    Fallback strategy: Cashfree Orders API (POST /pg/orders).
    Returns dict with success: bool, payment_link: str, order_id: str, error: str.
    """
    cf_cfg = await get_cashfree_config(bot_cfg=bot_cfg)
    price = float(story.get("price", 0))
    story_id = str(story["_id"])
    story_name = story.get("story_name_en", "Story")
    if not order_id:
        order_id = f"AB-{user_id}-{int(time.time())}"

    if price <= 0:
        return {"success": False, "error": "Invalid story price."}

    miniapp_fallback_link = f"https://aryapremium.store/app?story_id={story_id}&buy=cashfree&user_id={user_id}"

    if not cf_cfg["is_configured"]:
        logger.warning("[CF] Credentials missing. Returning mini app fallback link.")
        return {
            "success": True,
            "order_id": order_id,
            "payment_link": miniapp_fallback_link,
            "amount": price
        }

    headers = {
        "x-client-id": cf_cfg["app_id"],
        "x-client-secret": cf_cfg["secret_key"],
        "x-api-version": "2023-08-01",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    clean_user_name = "".join(c for c in (user_name or "Buyer") if c.isalnum() or c in " _-")[:40] or "Buyer"

    # Ensure valid return_url (Cashfree API rejects null return_url)
    if bot_username:
        return_url = f"https://t.me/{bot_username}?start=order_{order_id}"
    else:
        return_url = cf_cfg.get("return_url") or f"https://aryapremium.store/app?order_id={order_id}"

    # ── Strategy 1: Direct Cashfree Hosted Payment Link (POST /pg/links) ──
    # Produces official https://payments.cashfree.com/links/... directly hosted by Cashfree.
    # Opens natively across all mobile devices, in-app Telegram webview, and Chrome without external JS SDKs.
    link_url = f"{cf_cfg['base_url']}/links"
    link_payload = {
        "link_id": order_id,
        "link_amount": round(price, 2),
        "link_currency": "INR",
        "link_purpose": f"Story: {story_name[:25]}",
        "customer_details": {
            "customer_id": f"tg_{user_id}",
            "customer_name": clean_user_name,
            "customer_email": f"user_{user_id}@aryapremium.store",
            "customer_phone": "9999999999"
        },
        "link_meta": {
            "return_url": return_url,
            "notify_url": "https://aryapremium.store/api/cashfree-webhook"
        },
        "link_auto_reminders": False,
        "link_notify": {
            "send_sms": False,
            "send_email": False
        }
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(link_url, json=link_payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10.0)) as resp:
                link_data = await resp.json()
                logger.info(f"[CF-LINK] Status={resp.status}, response={link_data}")
                if resp.status in (200, 201) and link_data.get("link_url"):
                    direct_pay_link = link_data["link_url"]
                    logger.info(f"[CF-LINK] Generated official Cashfree hosted payment link: {direct_pay_link}")
                    try:
                        await db.db.orders.insert_one({
                            "order_id": order_id,
                            "link_id": order_id,
                            "cf_link_id": link_data.get("cf_link_id"),
                            "user_id": int(user_id),
                            "story_ids": [story_id],
                            "story_id": story_id,
                            "story_name": story_name,
                            "bot_username": bot_username,
                            "amount": price,
                            "currency": "INR",
                            "status": "pending",
                            "gateway": "cashfree",
                            "is_link": True,
                            "payment_link": direct_pay_link,
                            "created_at": time.time()
                        })
                    except Exception as db_err:
                        logger.error(f"[CF] DB insert error: {db_err}")

                    return {
                        "success": True,
                        "order_id": order_id,
                        "payment_link": direct_pay_link,
                        "cf_link_id": link_data.get("cf_link_id"),
                        "amount": price
                    }
                else:
                    logger.warning(f"[CF-LINK] /links non-200 or missing link_url: {link_data}. Falling back to /orders")
    except Exception as ex_link:
        logger.warning(f"[CF-LINK] /links request exception: {ex_link}. Falling back to /orders")

    # ── Strategy 2: Fallback to Cashfree Orders API (POST /pg/orders) ──
    order_payload = {
        "order_id": order_id,
        "order_amount": round(price, 2),
        "order_currency": "INR",
        "customer_details": {
            "customer_id": f"tg_{user_id}",
            "customer_name": clean_user_name,
            "customer_email": f"user_{user_id}@aryapremium.store",
            "customer_phone": "9999999999"
        },
        "order_meta": {
            "return_url": return_url,
            "notify_url": "https://aryapremium.store/api/cashfree-webhook"
        },
        "order_note": f"Purchase of {story_name[:30]}"
    }

    order_url = f"{cf_cfg['base_url']}/orders"
    logger.info(f"[CF-ORDER] Creating fallback order: order_id={order_id}, amount={price}, url={order_url}")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(order_url, json=order_payload, headers=headers, timeout=aiohttp.ClientTimeout(total=12.0)) as resp:
                data = await resp.json()
                logger.info(f"[CF-ORDER] API Response: status={resp.status}, data={data}")
                if resp.status in (200, 201) and (data.get("payment_session_id") or data.get("order_id")):
                    payment_session_id = data.get("payment_session_id", "")
                    is_sb = cf_cfg.get("is_sandbox", False)
                    payment_link = (
                        data.get("payment_link") or
                        (f"https://aryapremium.store/api/cashfree-pay?session_id={payment_session_id}&sandbox={'true' if is_sb else 'false'}" if payment_session_id else miniapp_fallback_link)
                    )
                    
                    logger.info(f"[CF-ORDER] Order created successfully: order_id={order_id}, pay_link={payment_link}")

                    # Store order in MongoDB
                    try:
                        await db.db.orders.insert_one({
                            "order_id": order_id,
                            "cf_order_id": data.get("cf_order_id"),
                            "payment_session_id": payment_session_id,
                            "user_id": int(user_id),
                            "story_ids": [story_id],
                            "story_id": story_id,
                            "story_name": story_name,
                            "bot_username": bot_username,
                            "amount": price,
                            "currency": "INR",
                            "status": "pending",
                            "gateway": "cashfree",
                            "payment_link": payment_link,
                            "created_at": time.time()
                        })
                    except Exception as db_err:
                        logger.error(f"[CF] Failed to insert pending order: {db_err}")

                    return {
                        "success": True,
                        "order_id": order_id,
                        "payment_link": payment_link,
                        "payment_session_id": payment_session_id,
                        "amount": price
                    }
                else:
                    err_msg = data.get("message") or data.get("description") or str(data)
                    logger.error(f"[CF-ORDER] Create order FAILED: status={resp.status}, response={data}")
                    return {
                        "success": True,
                        "order_id": order_id,
                        "payment_link": miniapp_fallback_link,
                        "amount": price,
                        "warning": err_msg
                    }
    except Exception as e:
        logger.error(f"[CF-ORDER] Create order EXCEPTION: {type(e).__name__}: {e}", exc_info=True)
        return {
            "success": True,
            "order_id": order_id,
            "payment_link": miniapp_fallback_link,
            "amount": price
        }


async def check_cashfree_order_status(order_id: str) -> dict:
    """
    Checks the status of a Cashfree order from MongoDB / Cashfree PG API.
    Supports both Cashfree Payment Links (/pg/links/{id}) and Cashfree Orders (/pg/orders/{id}).
    Returns dict with status: 'PAID' | 'ACTIVE' | 'FAILED' | 'EXPIRED', is_paid: bool, raw: dict.
    """
    try:
        from AryaPremium.database import db
    except ImportError:
        from database import db

    # 1. Check MongoDB first (fast path if webhook already processed payment)
    try:
        db_order = await db.db.orders.find_one({
            "$or": [
                {"order_id": order_id},
                {"cf_order_id": order_id},
                {"payment_session_id": order_id},
                {"link_id": order_id}
            ]
        })
        if db_order and db_order.get("status") in ("paid", "PAID", "SUCCESS"):
            st_id = db_order.get("story_id") or (db_order.get("story_ids", [None])[0] if db_order.get("story_ids") else "")
            return {
                "status": "PAID",
                "is_paid": True,
                "amount": db_order.get("amount", 0),
                "order_id": order_id,
                "story_id": str(st_id) if st_id else "",
                "db_order": db_order
            }
    except Exception as ex:
        logger.warning(f"[CF-STATUS] Error querying DB: {ex}")

    cf_cfg = await get_cashfree_config()
    if not cf_cfg["is_configured"]:
        return {"status": "ERROR", "is_paid": False, "error": "Not configured."}

    headers = {
        "x-client-id": cf_cfg["app_id"],
        "x-client-secret": cf_cfg["secret_key"],
        "x-api-version": "2023-08-01",
        "Accept": "application/json"
    }

    # 2. Check via /pg/links/{order_id} first (if created as payment link)
    link_url = f"{cf_cfg['base_url']}/links/{order_id}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(link_url, headers=headers, timeout=aiohttp.ClientTimeout(total=8.0)) as resp:
                if resp.status == 200:
                    link_data = await resp.json()
                    link_status = str(link_data.get("link_status", "")).upper()
                    is_paid = (link_status in ("PAID", "SUCCESS"))
                    st_id = ""
                    if is_paid:
                        try:
                            await db.db.orders.update_one(
                                {"$or": [{"order_id": order_id}, {"link_id": order_id}]},
                                {"$set": {"status": "paid", "paid_at": time.time(), "link_status": link_status}}
                            )
                            db_o = await db.db.orders.find_one({"$or": [{"order_id": order_id}, {"link_id": order_id}]})
                            if db_o:
                                st_id = str(db_o.get("story_id") or (db_o.get("story_ids", [None])[0] if db_o.get("story_ids") else ""))
                        except Exception:
                            pass
                    return {
                        "status": link_status,
                        "is_paid": is_paid,
                        "amount": link_data.get("link_amount"),
                        "story_id": st_id,
                        "raw": link_data
                    }
    except Exception as ex_link:
        logger.debug(f"[CF-STATUS] /links/{order_id} check exception: {ex_link}")

    # 3. Check via /pg/orders/{order_id}
    order_url = f"{cf_cfg['base_url']}/orders/{order_id}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(order_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10.0)) as resp:
                data = await resp.json()
                if resp.status == 200:
                    order_status = (data.get("order_status") or "").upper()
                    is_paid = (order_status in ("PAID", "SUCCESS"))
                    st_id = ""
                    if is_paid:
                        try:
                            await db.db.orders.update_one(
                                {"$or": [{"order_id": order_id}, {"cf_order_id": order_id}]},
                                {"$set": {"status": "paid", "paid_at": time.time(), "cf_order_id": data.get("cf_order_id")}}
                            )
                            db_o = await db.db.orders.find_one({"$or": [{"order_id": order_id}, {"cf_order_id": order_id}]})
                            if db_o:
                                st_id = str(db_o.get("story_id") or (db_o.get("story_ids", [None])[0] if db_o.get("story_ids") else ""))
                        except Exception:
                            pass
                    return {
                        "status": order_status,
                        "is_paid": is_paid,
                        "amount": data.get("order_amount"),
                        "story_id": st_id,
                        "raw": data
                    }
                else:
                    return {"status": "ERROR", "is_paid": False, "error": data.get("message", "Check failed")}
    except Exception as e:
        logger.error(f"Cashfree order status check error: {e}")
        return {"status": "ERROR", "is_paid": False, "error": str(e)}
