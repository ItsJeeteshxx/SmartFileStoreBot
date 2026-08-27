"""
Gmail IMAP Auto-Verification Helper for UPI Payments
====================================================
Automatically connects to Gmail via IMAP SSL to verify UPI payments
by searching for the 12-digit UTR/RRN number, verifying the credited
amount, and extracting the payer's name.
"""

import asyncio
import email
import imaplib
import logging
import os
import re
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


def clean_extracted_name(name: str) -> str:
    name = re.sub(r'\s+', ' ', name).strip()
    stop_words = {
        "amount", "utr", "rrn", "txn", "txnid", "date", "ref", "rs", "inr", "upi",
        "payment", "status", "type", "received", "credited", "transferred", "has",
        "been", "via", "on", "in", "to", "your", "my", "account", "bank", "slice",
        "customer", "user", "card", "rupees", "id", "no", "reference", "debit",
        "credit", "wallet", "balance", "success", "failed", "pending"
    }
    words = name.split()
    valid_words = []
    for w in words:
        w_clean = re.sub(r'[^a-zA-Z]', '', w).lower()
        if w_clean in stop_words:
            break
        valid_words.append(w)

    cleaned = " ".join(valid_words).strip()
    cleaned = re.sub(r'[^a-zA-Z\s\.\-\&]', '', cleaned).strip()
    return cleaned.rstrip('. - &').strip()


def extract_payer_name_from_email(body: str) -> str:
    """Helper to extract sender name from bank / slice email notifications."""
    if not body:
        return ""
    body_clean = re.sub(r'<[^>]+>', ' ', body)
    body_clean = re.sub(r'\s+', ' ', body_clean).strip()

    patterns = [
        r'(?:payer|sender|remitter)(?:\s+name)?\s*[:\-]\s*([a-zA-Z\s\.\-\&]{3,40})',
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
            cleaned = clean_extracted_name(name)
            if len(cleaned) >= 3 and cleaned.lower() not in words_to_skip:
                return cleaned.title()
    return ""


def extract_amount_from_email(body: str) -> Optional[float]:
    normalized = body.replace("\n", " ").replace("\r", " ")
    body_lower = normalized.lower()
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
                if 1.0 <= val <= 100000.0:
                    return val
            except ValueError:
                continue

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
    normalized = body.replace("\n", " ").replace("\r", " ")
    amt_str1 = f"{expected_amount:.2f}"
    amt_str2 = f"{int(expected_amount)}" if expected_amount.is_integer() else f"{expected_amount:.1f}"
    body_lower = normalized.lower()

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
    """Helper to extract text or HTML body from email message."""
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
                    text_content = re.sub(r'<[^>]+>', ' ', html_content)
                    return re.sub(r'\s+', ' ', text_content)
                except Exception:
                    pass
    else:
        try:
            return msg.get_payload(decode=True).decode("utf-8", errors="ignore")
        except Exception:
            pass
    return ""


def _sync_imap_search(gmail_user: str, gmail_password: str, utr: str, expected_amount: float) -> Dict[str, Any]:
    """Blocking IMAP connection and message scan (run in separate thread)."""
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(gmail_user, gmail_password)
        mail.select("INBOX")

        status, messages = mail.search(None, 'TEXT', utr)
        if status != "OK" or not messages[0]:
            try:
                mail.close()
                mail.logout()
            except Exception:
                pass
            return {"found": False, "verified": False}

        mail_ids = messages[0].split()
        verified = False
        amount_mismatch = False
        mismatched_amount = None
        payer_name = ""

        for mail_id in reversed(mail_ids):
            res_status, msg_data = mail.fetch(mail_id, "(RFC822)")
            if res_status != "OK":
                continue
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    body = get_email_body(msg)

                    if utr in body:
                        if verify_amount_in_email(body, expected_amount):
                            verified = True
                            payer_name = extract_payer_name_from_email(body)
                            break
                        else:
                            parsed_amount = extract_amount_from_email(body)
                            if parsed_amount is not None:
                                amount_mismatch = True
                                mismatched_amount = parsed_amount
                                break

            if verified or amount_mismatch:
                break



        try:
            mail.close()
            mail.logout()
        except Exception:
            pass

        return {
            "found": True,
            "verified": verified,
            "amount_mismatch": amount_mismatch,
            "mismatched_amount": mismatched_amount,
            "payer_name": payer_name
        }
    except Exception as e:
        logger.error(f"[Gmail IMAP] Sync search error: {e}")
        return {"found": False, "verified": False, "error": str(e)}


async def verify_upi_payment_via_gmail(
    utr: str,
    expected_amount: float,
    gmail_user: str = "",
    gmail_password: str = ""
) -> Dict[str, Any]:
    """
    Verify UPI payment asynchronously via Gmail IMAP.
    """
    from config import Config
    from database import db

    u = str(gmail_user or "").strip()
    p = str(gmail_password or "").strip()

    if not u or not p:
        rl_cfg = await db.get_delivery_rate_limit_config()
        u = str(rl_cfg.get("gmail_user") or u or "").strip()
        p = str(rl_cfg.get("gmail_app_password") or p or "").strip()

    if not u or not p:
        u = str(getattr(Config, "GMAIL_USER", "") or os.environ.get("GMAIL_USER", "") or u or "").strip()
        p = str(getattr(Config, "GMAIL_APP_PASSWORD", "") or os.environ.get("GMAIL_APP_PASSWORD", "") or p or "").strip()

    if not u or not p:
        return {
            "success": False,
            "error": "Gmail credentials (GMAIL_USER / GMAIL_APP_PASSWORD) not configured in settings or .env."
        }

    clean_u = u.replace("\xa0", "").replace(" ", "").strip()
    clean_p = p.replace("\xa0", "").replace(" ", "").strip()

    res = await asyncio.to_thread(_sync_imap_search, clean_u, clean_p, utr, expected_amount)

    if res.get("error"):
        return {
            "success": False,
            "error": res["error"]
        }

    if not res.get("found"):
        return {
            "success": False,
            "error": "UTR not found in inbox yet. Bank emails may take 10-30 seconds to arrive.",
            "not_found": True
        }

    if res.get("amount_mismatch"):
        return {
            "success": False,
            "amount_mismatch": True,
            "mismatched_amount": res.get("mismatched_amount"),
            "expected_amount": expected_amount,
            "error": f"Amount mismatch: received ₹{res.get('mismatched_amount')}, expected ₹{expected_amount}"
        }

    if res.get("verified"):
        return {
            "success": True,
            "payer_name": res.get("payer_name") or "UPI Payer",
            "amount": expected_amount
        }

    return {
        "success": False,
        "error": "Payment could not be verified. Please verify your UTR and try again."
    }
