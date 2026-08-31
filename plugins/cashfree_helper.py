"""
Cashfree Payment Gateway Helper for Arya Delivery Bot Unlimited Pass
"""
import os
import re
import uuid
import time
import logging
import aiohttp
from database import db

logger = logging.getLogger(__name__)

async def get_cashfree_credentials() -> dict:
    """Fetch Cashfree credentials from Delivery Rate Limit Config, Mini App Config, or Environment."""
    rl_cfg = {}
    try:
        rl_cfg = await db.get_delivery_rate_limit_config() or {}
    except Exception:
        pass

    cfg = {}
    try:
        if hasattr(db, 'db') and db.db is not None:
            cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    except Exception as e:
        logger.warning(f"Failed to read mini_app_config: {e}")

    app_id = (
        rl_cfg.get("cashfree_app_id", "") or
        cfg.get("cashfree_app_id", "") or
        cfg.get("cashfree_api_id", "") or
        os.environ.get("CASHFREE_APP_ID", "") or
        os.environ.get("CASHFREE_API_ID", "")
    ).strip()

    secret_key = (
        rl_cfg.get("cashfree_secret_key", "") or
        cfg.get("cashfree_secret_key", "") or
        os.environ.get("CASHFREE_SECRET_KEY", "")
    ).strip()

    cf_env = (
        rl_cfg.get("cashfree_env", "") or
        cfg.get("cashfree_env", "") or
        os.environ.get("CASHFREE_ENV", "production")
    ).strip().lower()

    is_sandbox = (
        cf_env in ("sandbox", "staging", "test") or
        "TEST" in app_id.upper() or
        "SANDBOX" in app_id.upper()
    )

    base_url = "https://sandbox.cashfree.com/pg" if is_sandbox else "https://api.cashfree.com/pg"

    return {
        "app_id": app_id,
        "secret_key": secret_key,
        "is_sandbox": is_sandbox,
        "base_url": base_url,
        "configured": bool(app_id and secret_key)
    }


async def create_cashfree_pass_order(user_id: int, user_name: str, duration: str, amount: float, bot_id: int = None, bot_username: str = "") -> dict:
    """
    Create a Cashfree PG order for an Unlimited Delivery Pass.
    Returns dictionary with order_id, payment_session_id, and checkout_pay_link.
    """
    creds = await get_cashfree_credentials()
    if not creds["configured"]:
        return {
            "success": False,
            "error": "Cashfree Gateway is not configured. Please configure it in Settings → Delivery Bots → Rate Limit & Pass → Cashfree Config."
        }

    from database import parse_duration_to_seconds, format_duration_friendly, format_duration_verbose
    dur_str = str(duration).strip()
    dur_sec = parse_duration_to_seconds(dur_str, default_unit='d')
    dur_tag = format_duration_friendly(dur_sec).upper()
    dur_verbose = format_duration_verbose(dur_sec)

    clean_name = re.sub(r'[^a-zA-Z0-9\s]', '', str(user_name or "User")).strip()
    customer_name = clean_name[:40] if clean_name else "User"
    order_num = await db.get_next_pass_order_number()
    order_id = f"PASS-{user_id}-{dur_tag}-{order_num}"

    payload = {
        "order_id": order_id,
        "order_amount": round(float(amount), 2),
        "order_currency": "INR",
        "customer_details": {
            "customer_id": f"cust_{user_id}",
            "customer_name": customer_name,
            "customer_email": f"user_{user_id}@t.me",
            "customer_phone": "9999999999"
        },
        "order_meta": {
            "return_url": f"https://t.me/{bot_username}?start=cf_{order_id}" if bot_username else "https://t.me/AryaV2XBot",
            "notify_url": "https://aryapremium.store/api/cashfree-webhook"
        },
        "order_note": f"{dur_verbose.title()} Unlimited Delivery Pass"
    }

    headers = {
        "x-client-id": creds["app_id"],
        "x-client-secret": creds["secret_key"],
        "x-api-version": "2023-08-01",
        "Content-Type": "application/json"
    }

    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{creds['base_url']}/orders", json=payload, headers=headers) as resp:
                res_json = await resp.json()
                logger.info(f"Cashfree Pass Order Create response: status={resp.status}, body={res_json}")

                if resp.status not in (200, 201):
                    err_msg = res_json.get("message") or res_json.get("detail") or f"HTTP {resp.status}"
                    return {"success": False, "error": f"Cashfree Error: {err_msg}"}

                payment_session_id = res_json.get("payment_session_id")
                cf_order_id = res_json.get("cf_order_id") or order_id
                
                # Hosted checkout link
                checkout_pay_link = (
                    f"https://aryapremium.store/api/cashfree-pay?session_id={payment_session_id}"
                    f"&sandbox={'true' if creds['is_sandbox'] else 'false'}"
                )

                # Persist order to database
                order_doc = {
                    "order_id": order_id,
                    "cf_order_id": str(cf_order_id),
                    "payment_session_id": payment_session_id,
                    "user_id": int(user_id),
                    "user_name": customer_name,
                    "bot_id": int(bot_id) if bot_id else None,
                    "bot_username": str(bot_username) if bot_username else "",
                    "duration": dur_str,
                    "duration_seconds": dur_sec,
                    "days": round(dur_sec / 86400.0, 2),
                    "amount": float(amount),
                    "status": "PENDING",
                    "checkout_pay_link": checkout_pay_link,
                    "created_at": time.time()
                }
                await db.create_pass_order(order_doc)

                return {
                    "success": True,
                    "order_id": order_id,
                    "payment_session_id": payment_session_id,
                    "checkout_pay_link": checkout_pay_link,
                    "amount": float(amount),
                    "duration": dur_str,
                    "duration_verbose": dur_verbose,
                    "days": round(dur_sec / 86400.0, 2)
                }

    except Exception as e:
        logger.error(f"Cashfree order creation exception: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def verify_cashfree_pass_order(order_id: str) -> dict:
    """
    Verify status of a Cashfree Pass Order by querying Cashfree API server-to-server.
    """
    creds = await get_cashfree_credentials()
    if not creds["configured"]:
        return {"success": False, "is_paid": False, "error": "Cashfree credentials missing"}

    headers = {
        "x-client-id": creds["app_id"],
        "x-client-secret": creds["secret_key"],
        "x-api-version": "2023-08-01",
        "Content-Type": "application/json"
    }

    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{creds['base_url']}/orders/{order_id}", headers=headers) as resp:
                res_json = await resp.json()
                logger.info(f"Cashfree Pass Verify status: status={resp.status}, body={res_json}")

                if resp.status == 200:
                    cf_status = str(res_json.get("order_status", "")).upper()
                    is_paid = (cf_status == "PAID")
                    return {
                        "success": True,
                        "is_paid": is_paid,
                        "status": cf_status,
                        "order_id": order_id,
                        "cf_order_id": res_json.get("cf_order_id"),
                        "order_amount": res_json.get("order_amount")
                    }
                else:
                    return {
                        "success": False,
                        "is_paid": False,
                        "error": res_json.get("message", f"HTTP {resp.status}")
                    }
    except Exception as e:
        logger.error(f"Cashfree pass verify exception for {order_id}: {e}")
        return {"success": False, "is_paid": False, "error": str(e)}
