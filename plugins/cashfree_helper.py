"""
Cashfree Payment Gateway Helper for Arya Delivery Bot Unlimited Pass
"""
import os
import re
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
    except Exception as e:
        logger.warning(f"Failed to read delivery_rate_limit_config: {e}")

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


async def create_cashfree_pass_order(user_id: int, user_name: str, days: int, amount: float) -> dict:
    """
    Create a direct Cashfree Payment Link / Order for an Unlimited Delivery Pass.
    Returns official Cashfree link (https://payments.cashfree.com/...)
    """
    creds = await get_cashfree_credentials()
    if not creds["configured"]:
        return {
            "success": False,
            "error": "Cashfree Gateway is not configured. Please configure it in Settings → Delivery Bots → Rate Limit & Pass → Cashfree Config."
        }

    clean_name = re.sub(r'[^a-zA-Z0-9\s]', '', str(user_name or "User")).strip()
    customer_name = clean_name[:40] if clean_name else "User"
    order_num = await db.get_next_pass_order_number()
    order_id = f"PASS-{user_id}-{days}D-{order_num}"

    headers = {
        "x-client-id": creds["app_id"],
        "x-client-secret": creds["secret_key"],
        "x-api-version": "2023-08-01",
        "Content-Type": "application/json"
    }

    # 1. Try Cashfree Direct Payment Links API (/pg/links) for short official payments.cashfree.com link
    link_payload = {
        "link_id": order_id,
        "link_amount": round(float(amount), 2),
        "link_currency": "INR",
        "link_purpose": f"{days} Day Unlimited Delivery Pass",
        "customer_details": {
            "customer_id": f"cust_{user_id}",
            "customer_name": customer_name,
            "customer_email": f"user_{user_id}@t.me",
            "customer_phone": "9999999999"
        },
        "link_notify": {
            "send_sms": False,
            "send_email": False
        },
        "link_meta": {
            "upi_intent": True
        }
    }

    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{creds['base_url']}/links", json=link_payload, headers=headers) as resp:
                res_json = await resp.json()
                logger.info(f"Cashfree /links response: status={resp.status}, body={res_json}")

                if resp.status in (200, 201) and res_json.get("link_url"):
                    checkout_pay_link = res_json.get("link_url")
                    order_doc = {
                        "order_id": order_id,
                        "type": "link",
                        "user_id": int(user_id),
                        "user_name": customer_name,
                        "days": int(days),
                        "amount": float(amount),
                        "status": "PENDING",
                        "checkout_pay_link": checkout_pay_link,
                        "created_at": time.time()
                    }
                    await db.create_pass_order(order_doc)
                    return {
                        "success": True,
                        "order_id": order_id,
                        "checkout_pay_link": checkout_pay_link,
                        "amount": float(amount),
                        "days": int(days)
                    }

            # 2. Fallback: Cashfree /orders API with official payments.cashfree.com hosted page
            order_payload = {
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
                    "return_url": f"https://t.me"
                },
                "order_note": f"{days} Day Unlimited Delivery Pass"
            }
            async with session.post(f"{creds['base_url']}/orders", json=order_payload, headers=headers) as resp:
                res_json = await resp.json()
                logger.info(f"Cashfree /orders response: status={resp.status}, body={res_json}")

                if resp.status not in (200, 201):
                    err_msg = res_json.get("message") or res_json.get("detail") or f"HTTP {resp.status}"
                    return {"success": False, "error": f"Cashfree Error: {err_msg}"}

                payment_session_id = res_json.get("payment_session_id")
                cf_order_id = res_json.get("cf_order_id") or order_id
                
                # Direct Cashfree standard hosted checkout link
                cf_host = "payments-test.cashfree.com" if creds["is_sandbox"] else "payments.cashfree.com"
                checkout_pay_link = f"https://{cf_host}/order/#/{payment_session_id}"

                order_doc = {
                    "order_id": order_id,
                    "cf_order_id": str(cf_order_id),
                    "payment_session_id": payment_session_id,
                    "type": "order",
                    "user_id": int(user_id),
                    "user_name": customer_name,
                    "days": int(days),
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
                    "days": int(days)
                }

    except Exception as e:
        logger.error(f"Cashfree order creation exception: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def verify_cashfree_pass_order(order_id: str) -> dict:
    """
    Verify status of a Cashfree Pass Order by querying Cashfree API server-to-server.
    Checks both /links/{order_id} and /orders/{order_id}.
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
            # 1. Check /links/{order_id}
            async with session.get(f"{creds['base_url']}/links/{order_id}", headers=headers) as resp:
                if resp.status == 200:
                    res_json = await resp.json()
                    status = str(res_json.get("link_status", "")).upper()
                    is_paid = (status == "PAID")
                    return {
                        "success": True,
                        "is_paid": is_paid,
                        "status": status,
                        "order_id": order_id,
                        "order_amount": res_json.get("link_amount") or res_json.get("link_amount_paid")
                    }

            # 2. Check /orders/{order_id}
            async with session.get(f"{creds['base_url']}/orders/{order_id}", headers=headers) as resp:
                res_json = await resp.json()
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
