"""
OxaPay Crypto Payment Helper for Delivery Bot Unlimited Pass
============================================================
Creates cryptocurrency payment invoices via OxaPay API (v1)
and inquires payment verification status.
"""

import aiohttp
import logging
import os
import time
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


async def create_oxapay_pass_order(
    user_id: int,
    user_name: str,
    dur_key: str,
    amount_inr: float,
    oxapay_key: str = "",
    oxapay_env: str = "production"
) -> Dict[str, Any]:
    """
    Create a crypto payment invoice on OxaPay for an Unlimited Delivery Pass.
    Converts INR to USD (minimum $0.50 required by OxaPay).
    """
    from config import Config
    from database import db, format_duration_verbose, parse_duration_to_seconds

    # 1. Resolve key
    key = str(oxapay_key or "").strip()
    env = str(oxapay_env or "production").strip().lower()

    if not key:
        rl_cfg = await db.get_delivery_rate_limit_config()
        key = str(rl_cfg.get("oxapay_key") or key or "").strip()
        env = str(rl_cfg.get("oxapay_env") or env or "production").strip().lower()

    if not key:
        key = str(getattr(Config, "OXAPAY_KEY", "") or os.environ.get("OXAPAY_KEY", "") or "").strip()
        env = str(getattr(Config, "OXAPAY_ENV", "production") or os.environ.get("OXAPAY_ENV", "production") or "production").strip().lower()

    if not key:
        return {
            "success": False,
            "error": "OxaPay Merchant API key (OXAPAY_KEY) is not configured in settings or .env."
        }

    is_sandbox = bool("sandbox" in key.lower() or env == "sandbox")
    base_url = "https://api.oxapay.com"

    # OxaPay expects USD with minimum $0.50 (0.50 USDT = ₹46 INR, rate: 92 INR/USD)
    amount_usd = max(0.50, round(float(amount_inr) / 92.0, 2))

    dur_sec = parse_duration_to_seconds(dur_key, default_unit='d')
    dur_name = format_duration_verbose(dur_sec).title()
    from database import format_duration_friendly
    dur_tag = format_duration_friendly(dur_sec).upper()
    order_num = await db.get_next_pass_order_number()
    order_id = f"PASS-{user_id}-{dur_tag}-{order_num}"
    clean_name = (user_name.split()[0] if user_name.strip() else "User")[:30]

    payload = {
        "amount": amount_usd,
        "currency": "USD",
        "lifetime": 60,
        "fee_paid_by_payer": 1,
        "order_id": order_id,
        "description": f"{dur_name} Unlimited Pass for {clean_name}",
        "customer_name": clean_name,
        "sandbox": is_sandbox
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base_url}/v1/payment/invoice",
                headers={
                    "merchant_api_key": key,
                    "Content-Type": "application/json"
                },
                json=payload,
                timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                data = await resp.json()

        logger.info(f"[OxaPay] Invoice response: {data}")

        # Extract payment URL and track ID from modern or legacy API schema
        data_obj = data.get("data") or {}
        pay_link = (
            data_obj.get("payment_url") or
            data_obj.get("pay_url") or
            data.get("payLink") or
            data.get("pay_link") or
            data.get("paylink")
        )
        track_id = (
            data_obj.get("track_id") or
            data_obj.get("trackId") or
            data.get("trackId") or
            data.get("track_id")
        )

        status_code = data.get("status") or data.get("result")
        try:
            status_int = int(status_code) if status_code is not None else 0
        except (ValueError, TypeError):
            status_int = 0

        is_success = (status_int in (100, 200)) and bool(pay_link)

        # Legacy fallback if v1 returned non-success
        if not is_success:
            legacy_payload = {
                "merchant": key,
                "amount": amount_usd,
                "currency": "USD",
                "lifeTime": 60,
                "feePaidByPayer": 1,
                "orderId": order_id,
                "description": f"{dur_name} Unlimited Pass for {clean_name}"
            }
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{base_url}/merchants/request",
                        json=legacy_payload,
                        timeout=aiohttp.ClientTimeout(total=15)
                    ) as resp2:
                        data2 = await resp2.json()
                logger.info(f"[OxaPay] Legacy endpoint response: {data2}")
                l_pay = data2.get("payLink") or data2.get("pay_link")
                l_track = data2.get("trackId") or data2.get("track_id")
                l_code = data2.get("result") or data2.get("status")
                if l_code in (100, 200) and l_pay:
                    pay_link = l_pay
                    track_id = l_track
                    is_success = True
            except Exception as e:
                logger.warning(f"OxaPay legacy request failed: {e}")

        if not is_success:
            err_msg = data.get("message") or data.get("description") or "Unknown OxaPay error"
            return {
                "success": False,
                "error": f"OxaPay Error: {err_msg}"
            }

        return {
            "success": True,
            "order_id": order_id,
            "pay_link": pay_link,
            "track_id": str(track_id or order_id),
            "amount_usd": amount_usd,
            "amount_inr": amount_inr,
            "dur_name": dur_name
        }
    except Exception as e:
        logger.error(f"[OxaPay] Create order exception: {e}")
        return {
            "success": False,
            "error": f"Failed to connect to OxaPay: {e}"
        }


async def verify_oxapay_pass_order(
    track_id: str,
    oxapay_key: str = "",
    oxapay_env: str = "production"
) -> Dict[str, Any]:
    """
    Inquire OxaPay API to verify if crypto invoice is paid.
    """
    from config import Config
    from database import db

    key = str(oxapay_key or "").strip()
    if not key:
        rl_cfg = await db.get_delivery_rate_limit_config()
        key = str(rl_cfg.get("oxapay_key") or key or "").strip()

    if not key:
        key = str(getattr(Config, "OXAPAY_KEY", "") or os.environ.get("OXAPAY_KEY", "") or "").strip()

    if not key:
        return {
            "success": False,
            "error": "OxaPay Merchant API key not configured."
        }

    base_url = "https://api.oxapay.com"

    try:
        inq_status = ""
        inq_data = {}
        inquiry = {}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{base_url}/v1/payment/{track_id}",
                    headers={"merchant_api_key": key},
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    inquiry = await resp.json()

            logger.info(f"[OxaPay] Inquiry response for {track_id}: {inquiry}")
            inq_data = inquiry.get("data") or {}
            inq_status = str(inq_data.get("status") or inquiry.get("status") or "").lower()
        except Exception:
            pass

        if inq_status not in ("paid", "success", "confirmed"):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{base_url}/merchants/inquiry",
                        json={"merchant": key, "trackId": track_id},
                        timeout=aiohttp.ClientTimeout(total=15)
                    ) as resp2:
                        inq2 = await resp2.json()
                logger.info(f"[OxaPay] Legacy inquiry response: {inq2}")
                s2 = str(inq2.get("status") or inq2.get("result") or "").lower()
                if s2 in ("paid", "success", "100"):
                    inq_status = "paid"
            except Exception:
                pass

        is_paid = bool(inq_status in ("paid", "success", "confirmed"))

        return {
            "success": True,
            "status": inq_status,
            "paid": is_paid,
            "tx_id": inq_data.get("txID") or inq_data.get("tx_id") or inquiry.get("txID") or ""
        }
    except Exception as e:
        logger.error(f"[OxaPay] Verify inquiry error: {e}")
        return {
            "success": False,
            "error": str(e)
        }
