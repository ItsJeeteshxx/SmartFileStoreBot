"""

Marketplace Seller Bot

======================

Handles the customer UI for buying stories, T&C, and progressive delivery.

"""

import logging

import asyncio

import base64

import io

import re

import html

from datetime import datetime

from pyrogram import Client, filters, enums

from pyrogram.types import (

    InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, 

    ReplyKeyboardRemove, InputMediaPhoto, InputMediaVideo, InputMediaAnimation

)

from pyrogram.handlers import MessageHandler, CallbackQueryHandler

from pyrogram.errors import MessageNotModified

from database import db

from config import Config

from utils import native_ask, _deliver_purchased_story, to_smallcap

from plugins.userbot.razorpay_helpers import _create_rzp_link, _check_rzp_status
import os

MINI_APP_WELCOME_TEXT = (
    "<b>╰┈➤ Welcome to the world of Arya Premium!</b>\n\n"
    "<b>①</b> Explore <b>300+</b> stories from <b>Pocket FM</b>, <b>Kuku FM</b>, <b>Pratilipi FM</b> & other platforms, available in Hindi & English.\n\n"
    "<b>②</b> Buy your favorite stories directly using <b>UPI</b>, <b>Cards</b>, <b>NetBanking</b> or <b>Crypto</b>.\n\n"
    "<b>③</b> Easily discover stories by <b>Genre</b>, <b>Platform</b> or <b>Language</b> and own them instantly.\n\n"
    "↳ Tap <b>Open App</b> and start exploring Arya Premium ⤵"
)

MINI_APP_START_MARKUP = InlineKeyboardMarkup([
    [InlineKeyboardButton("Open App", url="https://t.me/UseAryaBot/apminibyarya")],
    [InlineKeyboardButton("✉️ Join Channel", url="https://t.me/AryaPremiumTG")]
])

def _get_arya_poster_path() -> str:
    possible_paths = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "arya_premium_poster.png"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "arya_premium_poster.png"),
        r"C:\Users\User\Downloads\new\Arya Premium Poster 1.png",
        os.path.join(os.getcwd(), "AryaPremium", "arya_premium_poster.png"),
        os.path.join(os.getcwd(), "arya_premium_poster.png"),
    ]
    for p in possible_paths:
        if p and os.path.exists(p):
            return p
    return ""

async def _make_arya_bot_order_id(user_id, story_id_str: str = None) -> str:
    """
    Generate a new structured Arya Order ID for the Telegram Bot.
    Format: AB-{TG_ID}-{DDMM}-{STORY_NUM}{ORDER_NUM}
    Prefix: AB = Telegram Bot
    Example: AB-1071421266-2107-55130

    - Story number = serial position of story (oldest added = #1, sorted _id asc)
    - Order number = globally unique auto-incremented counter via ReturnDocument.AFTER
    """
    from datetime import datetime as _dt
    from pymongo import ReturnDocument
    try:
        date_str = _dt.now().strftime("%d%m")

        # ATOMIC global counter — ReturnDocument.AFTER returns post-increment value (always unique)
        counter_doc = await db.db.order_counters.find_one_and_update(
            {"_key": "global_order_counter"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER
        )
        order_num = counter_doc.get("seq", 1) if counter_doc else 1

        # Story serial number (oldest story = #1, sorted by _id ascending)
        story_num = 0
        if story_id_str:
            try:
                from bson.objectid import ObjectId as _OID
                all_ids = await db.db.premium_stories.distinct("_id")
                all_ids_sorted = sorted(all_ids)  # ascending: oldest = #1
                target_oid = _OID(str(story_id_str))
                if target_oid in all_ids_sorted:
                    story_num = all_ids_sorted.index(target_oid) + 1
            except Exception:
                story_num = 0

        uid_str = str(user_id)
        if story_num > 0:
            return f"AB-{uid_str}-{date_str}-{story_num}{order_num}"
        else:
            return f"AB-{uid_str}-{date_str}-{order_num}"
    except Exception as e:
        logging.getLogger(__name__).warning(f"[BotOrderID] Error: {e}")
        import random, string as _s
        return f"OD-{user_id}-" + ''.join(random.choices(_s.ascii_uppercase + _s.digits, k=6))


from plugins.userbot.easebuzz_helpers import _create_easebuzz_link, _check_easebuzz_status

from plugins.userbot.premium_emoji import react_bg, REACTIONS_WELCOME, REACTIONS_SUCCESS, REACTIONS_GENERAL



from utils_upi import generate_upi_card



logger = logging.getLogger(__name__)

market_clients: dict = {}

dm_aborts = set()

def _sc(val): return to_smallcap(str(val))

def _bs(val): return f"<b>{val}</b>"

def to_mathbold(val): return f"<b>{val}</b>"



_BOT_CONFIG_CACHE = {} # {bot_id: (timestamp, doc)}
_FEATURE_TOGGLE_CACHE = {"ts": 0, "doc": {}}

async def _get_cached_bot_doc(bot_id: int):
    import time
    now = time.time()
    if bot_id in _BOT_CONFIG_CACHE:
        ts, doc = _BOT_CONFIG_CACHE[bot_id]
        if now - ts < 30.0:
            return doc
    doc = await db.db.premium_bots.find_one({"id": int(bot_id)})
    _BOT_CONFIG_CACHE[bot_id] = (now, doc)
    return doc

async def _get_cached_features():
    import time
    now = time.time()
    if now - _FEATURE_TOGGLE_CACHE["ts"] < 30.0:
        return _FEATURE_TOGGLE_CACHE["doc"]
    doc = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    _FEATURE_TOGGLE_CACHE["ts"] = now
    _FEATURE_TOGGLE_CACHE["doc"] = doc
    return doc

def get_user_fast_info(from_user=None, user_doc=None, user_id=None):
    fn = getattr(from_user, "first_name", "") or (user_doc or {}).get("first_name", "") or "User"
    ln = getattr(from_user, "last_name", "") or (user_doc or {}).get("last_name", "") or ""
    un = getattr(from_user, "username", "") or (user_doc or {}).get("username", "") or ""
    full_name = f"{fn} {ln}".strip() or "User"
    uname_str = f"@{un}" if un else "N/A"
    return full_name, uname_str, fn, ln, un

async def get_robust_user(client: Client, user_id: int):
    class DummyUser:
        def __init__(self, uid, fn="User", ln="", un=""):
            self.id = int(uid)
            self.first_name = fn
            self.last_name = ln
            self.username = un

    try:
        db_user = await db.db.users.find_one({"id": int(user_id)}, {"first_name": 1, "last_name": 1, "username": 1})
        if db_user:
            return DummyUser(
                user_id,
                fn=db_user.get("first_name") or "User",
                ln=db_user.get("last_name") or "",
                un=db_user.get("username") or ""
            )
    except Exception:
        pass

    return DummyUser(user_id)



def _is_cancel(msg):
    if not msg: return True
    if hasattr(msg, "data") and msg.data == "ask_cancel": return True
    txt = (getattr(msg, 'text', '') or '').strip().lower()
    if not txt: return False
    if txt.startswith("/"):
        return True
    cancel_keywords = [
        "cancel", "c\u1d00\u0274\u1d04\u1d07\u029f", "रद्द", "रद्द करें", "वापस",
        "back", "exit", "stop", "close", "menu", "main menu", "« back", "« cancel", "«", "❮", "❬", "❯"
    ]
    for kw in cancel_keywords:
        if kw in txt:
            return True
    if " [ ₹ " in txt or txt in ["pocket fm", "kuku fm", "kuku tv", "pratilipi fm", "headfone", "story tv", "other"]:
        return True
    return False





def _clean_upi_note(s: str) -> str:

    # Keep it short + app-compatible.

    s = (s or "").strip()

    s = re.sub(r"[^a-zA-Z0-9 _:/#-]+", "", s)

    return s[:40]





def _build_upi_uri(*, upi_id: str, payee_name: str, amount: int, note: str) -> str:

    """

    Build a conservative UPI URI to maximize compatibility across apps.



    - Always include: pa, am, cu

    - Include pn only when explicitly configured (avoid branding mismatch like "Arya YT")

    - Keep tn generic and short to reduce fraud/risk heuristics

    """

    import urllib.parse

    pa = (upi_id or "").strip()

    am = str(amount)

    q = [f"pa={pa}", f"am={am}", "cu=INR"]



    pn_clean = (payee_name or "").strip()

    if pn_clean:

        q.append(f"pn={urllib.parse.quote_plus(pn_clean)}")



    return "upi://pay?" + "&".join(q)





# Telegram URL buttons only allow http/https; pasted URLs often include invisible Unicode (ZWSP, BOM) → BUTTON_URL_INVALID.

_INVISIBLE_URL_JUNK = re.compile(r"[\u200b-\u200f\u2060\ufeff\ufe0f\u200d\u200c\u00a0]+")





def sanitize_https_redirect_base(url: str) -> str:

    if not url:

        return ""

    s = _INVISIBLE_URL_JUNK.sub("", str(url)).strip()

    s = s.rstrip("/").strip()

    if not (s.lower().startswith("http://") or s.lower().startswith("https://")):

        return ""

    # Block accidental query strings on base (we append /r/...)

    if "?" in s:

        s = s.split("?", 1)[0].rstrip("/")

    return s





def build_open_upi_app_https_url(base: str, upi_uri: str) -> str:

    """

    Short HTTPS URL for Telegram buttons: https://host/r/<base64url(upi_uri)>

    Avoids huge ?uri=... links and invisible-char breakage.

    """

    base = sanitize_https_redirect_base(base)

    if not base or not upi_uri:

        return ""

    tok = base64.urlsafe_b64encode(upi_uri.encode("utf-8")).decode("ascii").rstrip("=")

    href = f"{base}/r/{tok}"

    # Telegram inline button URL limit ~2048 bytes

    if len(href) > 2040:

        return ""

    return href





def _can_sliceurl_shorten(url: str) -> bool:

    if not url:

        return False

    u = url.strip()

    if u.startswith("http://") or u.startswith("https://"):

        return True

    return u.lower().startswith("upi://pay?") and "pa=" in u and "am=" in u





async def _sliceurl_api_shorten(url: str) -> str:

    """

    SliceURL api-public ?action=shorten — supports https://… and (after your deploy) upi://pay?…



    Env: SLICEURL_API_URL, SLICEURL_API_KEY (slc_…)

    """

    if not _can_sliceurl_shorten(url):

        return ""



    api_base = (getattr(Config, "SLICEURL_API_URL", None) or "").strip()

    key = (getattr(Config, "SLICEURL_API_KEY", None) or "").strip()



    if api_base and key.startswith("slc_"):

        try:

            import aiohttp

            post_url = f"{api_base.rstrip('/')}?action=shorten"

            headers = {

                "X-API-Key": key,

                "Content-Type": "application/json",

                "Accept": "application/json",

            }

            async with aiohttp.ClientSession() as session:

                async with session.post(

                    post_url,

                    json={"long_url": url},

                    headers=headers,

                    timeout=aiohttp.ClientTimeout(total=20),

                ) as resp:

                    status = resp.status

                    try:

                        data = await resp.json()

                    except Exception:

                        data = {}

            if status in (200, 201) and isinstance(data, dict) and data.get("success"):

                short = data.get("short_url")

                if isinstance(short, str) and short.startswith("http"):

                    return short

            logger.warning(f"SliceURL shorten rejected: status={status} body={data}")

        except Exception as e:

            logger.warning(f"SliceURL shorten failed: {e}")

        return ""



    # Strict mode: do not use legacy/generic shorteners.

    return ""




# ──────────────────────────────────────────────────────────────────────────────
# OxaPay (Crypto) helpers — used by SECURE CHECKOUT - 2
# ──────────────────────────────────────────────────────────────────────────────

async def _create_oxapay_invoice_bot(amount_inr: int, story_name: str) -> tuple:
    """
    Create an OxaPay crypto invoice for a given INR amount.
    Returns (payLink: str, trackId: str) on success, or (None, error_msg) on failure.
    Minimum: $0.50 USD (≈ ₹43).
    """
    oxapay_key = (getattr(Config, "OXAPAY_KEY", "") or "").strip()
    if not oxapay_key:
        return None, "OXAPAY_KEY not configured in .env — crypto payments disabled."

    # INR → USD (approximate rate ₹84 = $1, adjust as needed)
    usd_amount = round(amount_inr / 84.0, 2)
    if usd_amount < 0.50:
        usd_amount = 0.50

    try:
        import aiohttp
        payload = {
            "merchant": oxapay_key,
            "amount": usd_amount,
            "currency": "USD",
            "lifeTime": 60,          # invoice valid 60 minutes
            "feePaidByPayer": 1,     # buyer pays network fee
            "description": f"Arya Premium: {story_name[:50]}",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.oxapay.com/merchants/request",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                data = await resp.json()

        if data.get("result") == 100 and data.get("trackId"):
            return data.get("payLink", ""), data["trackId"]

        return None, data.get("message", f"OxaPay error code: {data.get('result', 'unknown')}")

    except Exception as e:
        logger.error(f"OxaPay invoice creation failed: {e}")
        return None, str(e)


async def _check_oxapay_invoice_bot(track_id: str) -> str:
    """
    Poll OxaPay to check an invoice's current status.
    Returns: 'paid' | 'waiting' | 'expired' | 'error'
    """
    oxapay_key = (getattr(Config, "OXAPAY_KEY", "") or "").strip()
    if not oxapay_key:
        return "error"
    try:
        import aiohttp
        payload = {"merchant": oxapay_key, "trackId": track_id}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.oxapay.com/merchants/inquiry",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()

        status = str(data.get("status", "")).strip().lower()
        if status in ("paid", "confirmed"):
            return "paid"
        if status == "expired":
            return "expired"
        return "waiting"

    except Exception as e:
        logger.error(f"OxaPay status check failed: {e}")
        return "error"







def _make_qr_png_bytes(data: str, *, logo_png_bytes: bytes | None = None) -> bytes:

    try:

        import qrcode

        from PIL import Image

    except Exception as e:

        # The runtime may not have optional deps installed; caller should fallback gracefully.

        raise RuntimeError("QR deps missing") from e



    qr = qrcode.QRCode(

        version=None,

        error_correction=qrcode.constants.ERROR_CORRECT_H,

        box_size=12,

        border=2,

    )

    qr.add_data(data)

    qr.make(fit=True)



    img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")



    if logo_png_bytes:

        try:

            logo = Image.open(io.BytesIO(logo_png_bytes)).convert("RGBA")

            max_w = int(img.size[0] * 0.22)

            max_h = int(img.size[1] * 0.22)

            logo.thumbnail((max_w, max_h))



            pad = max(6, int(img.size[0] * 0.012))

            bg_w, bg_h = logo.size[0] + pad * 2, logo.size[1] + pad * 2

            bg = Image.new("RGBA", (bg_w, bg_h), (255, 255, 255, 255))

            pos = ((img.size[0] - bg_w) // 2, (img.size[1] - bg_h) // 2)

            img.alpha_composite(bg, pos)

            img.alpha_composite(logo, (pos[0] + pad, pos[1] + pad))

        except Exception:

            pass



    out = io.BytesIO()
    out.name = "upi_qr.png"
    img.save(out, format="PNG", optimize=True)
    out.seek(0)
    return out



def _sc(text: str) -> str:

    return text.translate(str.maketrans(

        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",

        "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"

    ))



def _bs(text: str) -> str:

    return text.translate(str.maketrans(

        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",

        "𝗮𝗯𝗰𝗱𝗲𝗳𝗴𝗵𝗶𝗷𝗸𝗹𝗺𝗻𝗼𝗽𝗾𝗿𝘀𝘁𝘂𝘃𝘄𝘅𝘆𝘇𝗔𝗕𝗖𝗗𝗘𝗙𝗚𝗛𝗜𝗝𝗞𝗟𝗠𝗡𝗢𝗣𝗤𝗥𝗦𝗧𝗨𝗩𝗪𝗫𝗬𝗭𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵"

    ))



def _get_base_header(user) -> str:

    u_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "User"

    return f"<blockquote expandable><b>Hello {u_name}</b></blockquote>\n\n"



def clean_extracted_name(name: str) -> str:
    import re
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
    import re
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
    import re
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
    import re
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
    import re
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


async def _get_rotated_upi(bt_cfg):
    try:
        from AryaPremium.database import db
    except ImportError:
        from database import db
        
    cfg_feat = {}
    try:
        db_obj = getattr(db, "db", None)
        if db_obj is not None:
            cfg_feat = await db_obj.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    except Exception as ex:
        logger.warning(f"[_get_rotated_upi] Warning loading feature_toggles: {ex}")
        cfg_feat = {}

    upi_options = []
    
    u1 = cfg_feat.get("upi_id", "").strip()
    if not u1 and hasattr(db, "get_config"):
        try:
            u1 = (await db.get_config("upi_id") or "").strip()
        except Exception:
            u1 = ""
            
    pn1 = cfg_feat.get("upi_payee_name", "").strip() or (bt_cfg.get("upi_name") or "Merchant").strip()
    if u1:
        upi_options.append((u1, pn1))
        
    u2 = cfg_feat.get("upi_id_2", "").strip()
    pn2 = cfg_feat.get("upi_payee_name_2", "").strip() or (bt_cfg.get("upi_name") or "Merchant").strip()
    if u2:
        upi_options.append((u2, pn2))
        
    u3 = cfg_feat.get("upi_id_3", "").strip()
    pn3 = cfg_feat.get("upi_payee_name_3", "").strip() or (bt_cfg.get("upi_name") or "Merchant").strip()
    if u3:
        upi_options.append((u3, pn3))
        
    u4 = cfg_feat.get("upi_id_4", "").strip()
    pn4 = cfg_feat.get("upi_payee_name_4", "").strip() or (bt_cfg.get("upi_name") or "Merchant").strip()
    if u4:
        upi_options.append((u4, pn4))
        
    if not upi_options:
        upi_options = [("heyjeetx@naviaxis", (bt_cfg.get("upi_name") or "Merchant").strip())]
        
    import random
    return random.choice(upi_options)


# Language Texts

T = {

    "en": {

        "welcome": "Welcome to",

        "store": "Store",

        "intro": "Browse our premium collection. Tap Marketplace to explore stories by platform.",

        "tc_accept": "✅ I Accept the Terms",

        "tc_reject": "❌ I Reject",

        "no_stories": "No stories currently available.",

        "pay_upi": "Pay via UPI",

        "back": "❮ Back",

        "qr_msg": """<b><emoji id="6030410254276106984">💳</emoji> Complete Payment</b>

• Scan the QR code above.
• Amount: ₹{price}

<b>After paying, send the successful payment screenshot here.</b>""",

        "wait_ver": '<emoji id="5348471079482441278">⏳</emoji> Your payment is being verified, please wait (approx 5 minutes)...',
        "notify": "🔔 Notify Admin",
        "prof_title": '╔═⟦ <emoji id="6021487472603568286">👤</emoji> 𝗣𝗥𝗢𝗙𝗜𝗟𝗘 ⟧═╗',
        "prof_name": "ɴᴀᴍᴇ",
        "prof_uname": "ᴜꜱᴇʀɴᴀᴍᴇ",
        "prof_id": "ᴛɢ ɪᴅ",
        "prof_bought": "ᴘᴜʀᴄʜᴀꜱᴇꜱ",
        "prof_lang": "ʟᴀɴɢᴜᴀɢᴇ",
        "prof_join": "ᴊᴏɪɴᴇᴅ",
        "my_reqs": "MY REQUESTS",
        "set_lang": "Settings",
        "set_prompt": """<b><emoji id="6021637109264160908">⚙️</emoji> Settings</b>

Select your language:""",
        "req_main_title": "My Story Requests",
        "req_click": "Click on any request to view its status:",
        "req_empty": "You haven't made any story requests yet.",
        "back_prof": "« BACK TO PROFILE",
        "back_reqs": "« BACK TO REQUESTS",
        "req_details": "STORY REQUEST DETAILS",
        "req_name": "Name",
        "req_plat": "Platform",
        "req_type": "Type",
        "req_date": "Date",
        "req_status": "Status",
        "already_owned": "✅ You already own this story. Sending delivery options...",
        "wait_a_sec": "WAIT A SECOND...",
        "req_step1": "<b>📤 Story Request System</b>\n\n<i>(Note: The story you request will be a Paid service, please keep this in mind.)</i>\n\nPlease enter the exact name of the story you are looking for:",
        "req_step2": "Got it. Send me any sample files, links, or screenshots related to this story (to help us locate it). If you don’t have any, type /skip.",
        "req_done": "✅ <b>Request Submitted!</b>\nWe have received your request. You can track its status using the 'My Requests' button in your Profile.",
        "cant_find_btn": "CAN'T FIND? REQUEST NOW!",
        "req_search_prompt": """<b><emoji id="6025893082552081088">🔍</emoji> SEARCH / REQUEST STORY</b>

Type the <b>Story Name</b> you want to search or request:""",
        "req_cancel": "Process Cancelled.",
        "req_success": """✅ <b>Request Submitted!</b>

Our team will search for this story and update you soon. Check status in <b>Profile -> My Requests</b>."""
    },
    "hi": {
        "welcome": "स्वागत है",
        "store": "स्टोर",
        "intro": "प्रीमियम कलेक्शन ब्राउज़ करें। Marketplace पर टैप करें।",
        "tc_accept": "✅ मुझे शर्तें मंजूर हैं",
        "tc_reject": "❌ मैं अस्वीकार करता हूँ",
        "no_stories": "वर्तमान में कोई स्टोरी उपलब्ध नहीं है।",
        "pay_upi": "UPI से पेमेंट करें",
        "back": "❮ वापस",
        "qr_msg": """<b><emoji id="6030410254276106984">💳</emoji> पेमेंट पूरा करें</b>

• ऊपर QR स्कैन करें।
• राशि: ₹{price}

<b>पेमेंट के बाद स्क्रीनशॉट यहाँ भेजें।</b>""",
        "wait_ver": '<emoji id="5348471079482441278">⏳</emoji> आपके भुगतान का सत्यापन हो रहा है...',
        "notify": "🔔 एडमिन को सूचित करें",
        "prof_title": '╔═⟦ <emoji id="6021487472603568286">👤</emoji> आपकी प्रोफाइल ⟧═╗',
        "prof_name": "नाम",
        "prof_uname": "यूज़रनेम",
        "prof_id": "आईडी",
        "prof_bought": "खरीदी गई स्टोरीज",
        "prof_lang": "भाषा",
        "prof_join": "जुड़े हुए",
        "my_reqs": "मेरे अनुरोध",
        "set_lang": "सेटिंग्स",
        "set_prompt": """<b><emoji id="6021637109264160908">⚙️</emoji> सेटिंग्स</b>

अपनी पसंदीदा भाषा चुनें:""",

        "req_main_title": "📝 मेरे स्टोरी अनुरोध",

        "req_click": "किसी भी अनुरोध पर क्लिक करके उसका स्टेटस देखें:",

        "req_empty": "आपने अभी तक कोई स्टोरी अनुरोध नहीं किया है।",

        "back_prof": "« प्रोफाइल पर वापस",

        "back_reqs": "« अनुरोधों पर वापस",

        "req_details": "📝 स्टोरी अनुरोध विवरण",

        "req_name": "कहानी का नाम",

        "req_plat": "प्लेटफॉर्म",

        "req_type": "प्रकार (Type)",

        "req_date": "तारीख",

        "req_status": "स्टेटस",

        "already_owned": "✅ आप पहले ही इस स्टोरी को खरीद चुके हैं। डिलीवरी विकल्प भेजे जा रहे हैं...",

        "wait_a_sec": "कृपया प्रतीक्षा करें...",

        "cant_find_btn": "कहानी नहीं मिल रही? अनुरोध करें!",

        "req_search_prompt": """<b><emoji id="6025893082552081088">🔍</emoji> स्टोरी खोजें / अनुरोध करें</b>

उस <b>कहानी का नाम</b> लिखें जिसे आप खोजना या अनुरोध करना चाहते हैं:""",

        "req_cancel": "प्रक्रिया रद्द कर दी गई।",

        "req_step1": """<b>स्टेप 1/3:</b>

कृपया उस <b>कहानी का नाम</b> लिखें जिसका आप अनुरोध करना चाहते हैं:

<i>(नोट: जो कहानी आप रीक्वेस्ट कर रहे हैं वह पेड (Paid) होगी, तो कृपया इस बात का ध्यान रखते हुए रीक्वेस्ट करें।)</i>""",

        "req_step2": """<b>स्टेप 2/3:</b>

<b>प्लेटफॉर्म</b> चुनें (जैसे: Ullu, AltBalaji):""",

        "req_step3": """<b>स्टेप 3/3:</b>

आपको यह कैसे चाहिए? (जैसे: केवल एपिसोड, पूरी फिल्म, आदि):""",

        "req_success": """✅ <b>अनुरोध जमा हो गया!</b>



हमारी टीम इस कहानी को खोजेगी और जल्द ही आपको अपडेट करेगी। स्टेटस देखने के लिए <b>प्रोफाइल -> मेरे अनुरोध</b> पर जाएं।"""

    }

}



def _ikb(text: str, callback_data: str = None, url: str = None, switch_inline_query_current_chat: str = None, switch_inline_query: str = None, icon_custom_emoji_id: str = None) -> InlineKeyboardButton:
    kw = {}
    if callback_data is not None: kw["callback_data"] = callback_data
    if url is not None: kw["url"] = url
    if switch_inline_query_current_chat is not None: kw["switch_inline_query_current_chat"] = switch_inline_query_current_chat
    if switch_inline_query is not None: kw["switch_inline_query"] = switch_inline_query
    b = InlineKeyboardButton(text, **kw)
    if icon_custom_emoji_id:
        b.icon_custom_emoji_id = str(icon_custom_emoji_id)
    return b


def _get_top_emoji_row() -> list:
    return [
        _ikb(" ", callback_data="mb#main_marketplace", icon_custom_emoji_id="5920332557466997677"),
        _ikb(" ", callback_data="mb#my_buys", icon_custom_emoji_id="6026337676091726218"),
        _ikb(" ", callback_data="mb#main_profile", icon_custom_emoji_id="6021487472603568286"),
        _ikb(" ", callback_data="mb#main_settings", icon_custom_emoji_id="6021637109264160908"),
        _ikb(" ", callback_data="mb#main_help", icon_custom_emoji_id="5945256248390721326"),
    ]


def _get_main_menu(lang='en', is_show_store=False):
    my_shows_lbl = ("• मेरे शोज़ •" if lang == 'hi' else f"• {_bs('MY SHOWS')} •") if is_show_store else ("• मेरी स्टोरीज •" if lang == 'hi' else f"• {_bs('MY STORIES')} •")
    search_lbl = ("शो खोजें" if lang == 'hi' else "Search Shows") if is_show_store else ("अपनी स्टोरी खोजें" if lang == 'hi' else f"{_sc('Search Your Story')}")
    search_icon = "6266794310671275367" if is_show_store else "5282843764451195532"
    marketplace_lbl = ("• स्टोर •" if lang == 'hi' else f"• {_bs('STORE')} •") if is_show_store else ("• मार्केटप्लेस •" if lang == 'hi' else f"• {_bs('MARKETPLACE')} •")

    if lang == 'hi':
        kb = [
            _get_top_emoji_row(),
            [InlineKeyboardButton(marketplace_lbl, callback_data="mb#main_marketplace"),
             InlineKeyboardButton(my_shows_lbl, callback_data="mb#my_buys")],
            [InlineKeyboardButton("प्रोफाइल", callback_data="mb#main_profile"),
             InlineKeyboardButton("सपोर्ट", callback_data="mb#main_help")],
            [_ikb(search_lbl, switch_inline_query_current_chat="", icon_custom_emoji_id=search_icon)],
            [
                InlineKeyboardButton("ᴄ", callback_data="mb#main_close"),
                InlineKeyboardButton("ʟ", callback_data="mb#main_close"),
                _ikb(" ", callback_data="mb#main_close", icon_custom_emoji_id="5774077015388852135"),
                InlineKeyboardButton("ꜱ", callback_data="mb#main_close"),
                InlineKeyboardButton("ᴇ", callback_data="mb#main_close")
            ]
        ]
    else:
        kb = [
            _get_top_emoji_row(),
            [InlineKeyboardButton(marketplace_lbl, callback_data="mb#main_marketplace"),
             InlineKeyboardButton(my_shows_lbl, callback_data="mb#my_buys")],
            [InlineKeyboardButton(f"{_sc('Profile')}", callback_data="mb#main_profile"),
             InlineKeyboardButton(f"{_sc('Support')}", callback_data="mb#main_help")],
            [_ikb(search_lbl, switch_inline_query_current_chat="", icon_custom_emoji_id=search_icon)],
            [
                InlineKeyboardButton("ᴄ", callback_data="mb#main_close"),
                InlineKeyboardButton("ʟ", callback_data="mb#main_close"),
                _ikb(" ", callback_data="mb#main_close", icon_custom_emoji_id="5774077015388852135"),
                InlineKeyboardButton("ꜱ", callback_data="mb#main_close"),
                InlineKeyboardButton("ᴇ", callback_data="mb#main_close")
            ]
        ]
    return InlineKeyboardMarkup(kb)


def _get_premium_menu_markup(bt_cfg: dict, lang: str):
    """
    Adds optional URL buttons (Updates/Support) like your reference UI.
    Stored in premium_bots.config as `updates_url` / `support_url`.
    """
    rows = []
    updates_url = (bt_cfg.get("updates_url") or "").strip()
    support_url = (bt_cfg.get("support_url") or "").strip()
    is_show_store = (bt_cfg.get("bot_mode") == "show_store")

    if updates_url or support_url:
        r = []
        if updates_url:
            label = "अपडेट्स" if lang == 'hi' else _sc("UPDATES")
            r.append(InlineKeyboardButton(label, url=updates_url))
        if support_url:
            label = "सपोर्ट" if lang == 'hi' else _sc("SUPPORT")
            r.append(InlineKeyboardButton(label, url=support_url))
        if r:
            rows.append(r)

    base = _get_main_menu(lang, is_show_store=is_show_store).inline_keyboard
    # Insert URL row above Close
    if rows:
        base = base[:-1] + rows + base[-1:]
    return InlineKeyboardMarkup(base)





def _menu_card_text(user, bt_cfg: dict, bot_name: str, lang: str = 'en') -> str:

    from utils import translate_to_hindi

    u_mention = f'<a href="tg://user?id={user.id}">{html.escape((user.first_name or "User").strip())}</a>'

    

    # --- DEFAULTS ---

    def _is_def(val, default_val):

        if not val or not default_val: return False

        import re

        return re.sub(r'\s+', '', val) == re.sub(r'\s+', '', default_val)



    DEFAULT_WELCOME_EN = """›› ʜᴇʏ, {name} | {bot_name}"""

    DEFAULT_ABOUT_EN = """ʙʀᴏᴡꜱᴇ ᴘʀᴇᴍɪᴜᴍ ꜱᴛᴏʀɪᴇꜱ ꜰʀᴏᴍ pocket fm, kuku fm, headphone & more.

ᴛᴀᴘ marketplace ᴛᴏ ᴇxᴘʟᴏʀᴇ ꜱᴛᴏʀɪᴇꜱ ʙʏ platform."""

    DEFAULT_QUOTE_EN = """ǫᴜᴀʟɪᴛʏ ꜱᴛᴏʀɪᴇꜱ • ɪɴꜱᴛᴀɴᴛ ᴅᴇʟɪᴠᴇʀʏ • ᴀᴜᴛᴏᴍᴀᴛᴇᴅ"""

    DEFAULT_AUTHOR_EN = """— ᴀʀʏᴀ ᴘʀᴇᴍɪᴜᴍ"""

    

    DEFAULT_WELCOME_HI = """नमस्ते {name}, आपका आर्या बोट में स्वागत है।"""

    DEFAULT_ABOUT_HI = """यहाँ आपको प्रसिद्ध ऐप्स की कहानियाँ मिलेंगी, जिन्हें आप “मार्केटप्लेस” पर जाकर खरीद सकते हैं।"""

    DEFAULT_QUOTE_HI = """अगर आप मुझे मुख्य भूमिका में रखकर कोई कहानी लिखेंगे... तो वह निश्चित रूप से एक त्रासदी होगी।"""

    DEFAULT_AUTHOR_HI = """— अज्ञात"""



    # --- 1. Welcome Section ---

    text_en = bt_cfg.get("welcome")

    if text_en and text_en.lower() == "disable":

        welcome = ""

    else:

        if not text_en:

            text_en = DEFAULT_WELCOME_EN

        

        if lang == 'hi':

            if _is_def(text_en, DEFAULT_WELCOME_EN):

                welcome = DEFAULT_WELCOME_HI

            else:

                welcome = translate_to_hindi(text_en)

        else:

            welcome = text_en

        

        welcome = welcome.replace("{name}", u_mention).replace("{bot_name}", bot_name).replace("{user}", u_mention).replace("{first_name}", u_mention)



    # --- 2. About Section ---

    text_en = bt_cfg.get("about")

    if text_en and text_en.lower() == "disable":

        about = ""

    else:

        if not text_en:

            text_en = DEFAULT_ABOUT_EN

        

        if lang == 'hi':

            if _is_def(text_en, DEFAULT_ABOUT_EN):

                about = DEFAULT_ABOUT_HI

            else:

                about = translate_to_hindi(text_en)

        else:

            about = text_en



    # --- 3. Quote Section ---

    text_en = bt_cfg.get("quote")

    if text_en and text_en.lower() == "disable":

        quote = ""

    else:

        if not text_en:

            text_en = DEFAULT_QUOTE_EN

        

        if lang == 'hi':

            if _is_def(text_en, DEFAULT_QUOTE_EN):

                quote = DEFAULT_QUOTE_HI

            else:

                quote = translate_to_hindi(text_en)

        else:

            quote = text_en



    # --- 4. Author Section ---

    text_en = bt_cfg.get("quote_author")

    if text_en and text_en.lower() == "disable":

        author = ""

    else:

        if not text_en:

            text_en = DEFAULT_AUTHOR_EN

        

        if lang == 'hi':

            if _is_def(text_en, DEFAULT_AUTHOR_EN):

                author = DEFAULT_AUTHOR_HI

            else:

                author = translate_to_hindi(text_en)

        else:

            author = text_en

        

    blocks = []
    if welcome.strip():
        blocks.append(f'<blockquote expandable="true">{welcome.strip()}</blockquote>')
    if about.strip():
        blocks.append(f'<blockquote expandable="true">{about.strip()}</blockquote>')
    if quote.strip():
        blocks.append(f'<blockquote expandable="true">{quote.strip()}</blockquote>')
    if author.strip():
        blocks.append(f'<blockquote expandable="true"><b>{author.strip()}</b></blockquote>')
        
    return "\n".join(blocks)









async def _edit_main_menu_in_place(client, query, user, lang: str):
    """
    Edit current message back to main menu when possible.
    Supports random media rotation on navigation and preserves custom emojis.
    """
    bt = await db.db.premium_bots.find_one({"id": client.me.id})
    bt_cfg = bt.get("config", {}) if bt else {}
    bot_name = client.me.first_name
    msg_txt = _menu_card_text(user, bt_cfg, bot_name, lang)
    markup = _get_premium_menu_markup(bt_cfg, lang)

    items = [x for x in _cfg_list(bt_cfg, "menu_media") if isinstance(x, dict) and x.get("file_id")]
    if not items and (bt_cfg.get("menuimg") or "").strip():
        items = [{"type": "photo", "file_id": (bt_cfg.get("menuimg") or "").strip()}]

    is_media = bool(getattr(query.message, 'photo', None) or getattr(query.message, 'video', None) or getattr(query.message, 'animation', None))

    # First try Bot API edit to preserve custom emoji buttons
    try:
        ok = await _send_or_edit_seller_bot_api(
            client=client,
            chat_id=query.message.chat.id,
            text=msg_txt,
            markup=markup,
            message_id=query.message.id,
            is_media_edit=is_media
        )
        if ok:
            return
    except Exception as e:
        logger.debug(f"Bot API edit failed in _edit_main_menu_in_place: {e}")

    # Fallback if media edit is possible
    if items and is_media:
        import random
        media_item = random.choice(items)
        t = (media_item.get("type") or "photo").strip()
        fid = (media_item.get("file_id") or "").strip()
        try:
            input_media = None
            if t == "animation":
                input_media = InputMediaAnimation(fid, caption=msg_txt, parse_mode=enums.ParseMode.HTML)
            elif t == "video":
                input_media = InputMediaVideo(fid, caption=msg_txt, parse_mode=enums.ParseMode.HTML)
            else:
                input_media = InputMediaPhoto(fid, caption=msg_txt, parse_mode=enums.ParseMode.HTML)

            await query.message.edit_media(media=input_media, reply_markup=markup)
            return
        except Exception as e:
            logger.warning(f"Failed to rotate media on edit: {e}")

    res = await _safe_edit(query.message, text=msg_txt, markup=markup)
    if not res:
        await _send_main_menu(client, query.from_user.id, user, lang)



_seller_bot_token_cache = {}

async def _get_seller_bot_token(client) -> str:
    bot_id = getattr(getattr(client, "me", None), "id", None)
    if bot_id and bot_id in _seller_bot_token_cache:
        return _seller_bot_token_cache[bot_id]
    
    t = getattr(client, "bot_token", None)
    if t:
        if bot_id: _seller_bot_token_cache[bot_id] = t
        return t
        
    if bot_id:
        bt = await db.db.premium_bots.find_one({"id": int(bot_id)})
        if bt and (bt.get("token") or bt.get("bot_token")):
            tok = bt.get("token") or bt.get("bot_token")
            _seller_bot_token_cache[bot_id] = tok
            return tok
            
    from config import Config
    return getattr(Config, "BOT_TOKEN", "")

def _markup_to_bot_api_list(markup: InlineKeyboardMarkup) -> list:
    res = []
    for row in markup.inline_keyboard:
        row_list = []
        for btn in row:
            d = {"text": btn.text}
            if getattr(btn, "callback_data", None) is not None: d["callback_data"] = btn.callback_data
            if getattr(btn, "url", None) is not None: d["url"] = btn.url
            if getattr(btn, "switch_inline_query_current_chat", None) is not None:
                d["switch_inline_query_current_chat"] = btn.switch_inline_query_current_chat
            elif getattr(btn, "switch_inline_query", None) is not None:
                d["switch_inline_query"] = btn.switch_inline_query
            if hasattr(btn, "icon_custom_emoji_id") and btn.icon_custom_emoji_id:
                d["icon_custom_emoji_id"] = str(btn.icon_custom_emoji_id)
            row_list.append(d)
        res.append(row_list)
    return res

async def _send_or_edit_seller_bot_api(
    client,
    chat_id: int,
    text: str,
    markup: InlineKeyboardMarkup,
    message_id: int = None,
    media_id: str = None,
    media_type: str = "photo",
    is_media_edit: bool = False
) -> bool:
    import aiohttp
    import json
    import re

    bot_token = await _get_seller_bot_token(client)
    if not bot_token:
        return False

    api_text = re.sub(r'<emoji id="(\d+)">([^<]*)</emoji>', r'<tg-emoji emoji-id="\1">\2</tg-emoji>', text)
    api_kb = _markup_to_bot_api_list(markup)

    payload = {
        "chat_id": int(chat_id),
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": api_kb
        }
    }

    url = f"https://api.telegram.org/bot{bot_token}/"
    if media_id and not is_media_edit:
        payload["caption"] = api_text
        if media_type == "animation":
            method = "sendAnimation"
            payload["animation"] = media_id
        elif media_type == "video":
            method = "sendVideo"
            payload["video"] = media_id
        else:
            method = "sendPhoto"
            payload["photo"] = media_id
    elif is_media_edit and message_id:
        method = "editMessageCaption"
        payload["message_id"] = int(message_id)
        payload["caption"] = api_text
    elif message_id:
        method = "editMessageText"
        payload["message_id"] = int(message_id)
        payload["text"] = api_text
    else:
        method = "sendMessage"
        payload["text"] = api_text

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url + method, json=payload, timeout=aiohttp.ClientTimeout(total=3.0)) as resp:
                data = await resp.json()
                if data.get("ok"):
                    return True
                logger.debug(f"Seller Bot API {method} returned: {data}")
    except Exception as e:
        logger.debug(f"Seller Bot API {method} exception: {e}")

def _kb_btn(text: str, icon_custom_emoji_id: str = None) -> dict:
    d = {"text": text}
    if icon_custom_emoji_id:
        d["icon_custom_emoji_id"] = str(icon_custom_emoji_id)
    return d


async def _send_reply_keyboard_bot_api(
    client,
    chat_id: int,
    text: str,
    keyboard: list,
    resize_keyboard: bool = True
) -> bool:
    import aiohttp
    import json
    import re

    bot_token = await _get_seller_bot_token(client)
    if not bot_token:
        return False

    api_text = re.sub(r'<emoji id="(\d+)">([^<]*)</emoji>', r'<tg-emoji emoji-id="\1">\2</tg-emoji>', text)

    api_keyboard = []
    for row in keyboard:
        api_row = []
        for btn in row:
            if isinstance(btn, dict):
                api_row.append(btn)
            elif isinstance(btn, str):
                api_row.append({"text": btn})
            elif hasattr(btn, "text"):
                d = {"text": btn.text}
                if hasattr(btn, "icon_custom_emoji_id") and btn.icon_custom_emoji_id:
                    d["icon_custom_emoji_id"] = str(btn.icon_custom_emoji_id)
                api_row.append(d)
        api_keyboard.append(api_row)

    payload = {
        "chat_id": int(chat_id),
        "text": api_text,
        "parse_mode": "HTML",
        "reply_markup": {
            "keyboard": api_keyboard,
            "resize_keyboard": resize_keyboard
        }
    }

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=3.0)) as resp:
                data = await resp.json()
                if data.get("ok"):
                    return True
                logger.debug(f"Seller Bot API sendMessage (ReplyKeyboard) returned: {data}")
    except Exception as e:
        logger.debug(f"Seller Bot API sendMessage (ReplyKeyboard) exception: {e}")

    return False


def _clean_markup_for_pyrogram(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    if not markup or not getattr(markup, "inline_keyboard", None):
        return markup
    cleaned_rows = []
    for row in markup.inline_keyboard:
        cleaned_row = []
        for btn in row:
            kwargs = {"text": getattr(btn, "text", "")}
            if getattr(btn, "callback_data", None) is not None:
                kwargs["callback_data"] = btn.callback_data
            if getattr(btn, "url", None) is not None:
                kwargs["url"] = btn.url
            if getattr(btn, "switch_inline_query_current_chat", None) is not None:
                kwargs["switch_inline_query_current_chat"] = btn.switch_inline_query_current_chat
            elif getattr(btn, "switch_inline_query", None) is not None:
                kwargs["switch_inline_query"] = btn.switch_inline_query
            if getattr(btn, "web_app", None) is not None:
                kwargs["web_app"] = btn.web_app
            cleaned_row.append(InlineKeyboardButton(**kwargs))
        cleaned_rows.append(cleaned_row)
    return InlineKeyboardMarkup(cleaned_rows)

async def _safe_edit(msg, *, text: str, markup: InlineKeyboardMarkup):
    if not msg:
        return None
    is_media = bool(getattr(msg, 'photo', None) or getattr(msg, 'video', None) or getattr(msg, 'animation', None) or getattr(msg, 'document', None))
    client = getattr(msg, "_client", None)

    # Try Bot API edit if custom emoji buttons are present
    try:
        has_custom_emoji = any(
            hasattr(btn, "icon_custom_emoji_id") and btn.icon_custom_emoji_id
            for row in markup.inline_keyboard for btn in row
        )
        if has_custom_emoji and msg and hasattr(msg, "chat") and hasattr(msg, "id"):
            ok = await _send_or_edit_seller_bot_api(
                client=client,
                chat_id=msg.chat.id,
                text=text,
                markup=markup,
                message_id=msg.id,
                is_media_edit=is_media
            )
            if ok:
                return True
    except Exception as e:
        logger.debug(f"Bot API edit fallback: {e}")

    # Fallback to Pyrogram native MTProto edit (with sanitized markup)
    clean_kb = _clean_markup_for_pyrogram(markup)
    try:
        if is_media:
            return await msg.edit_caption(caption=text, reply_markup=clean_kb, parse_mode=enums.ParseMode.HTML)
        return await msg.edit_text(text, reply_markup=clean_kb, parse_mode=enums.ParseMode.HTML)
    except MessageNotModified:
        return None
    except Exception as ex:
        logger.warning(f"_safe_edit fallback triggered: {ex}")
        try:
            if client and hasattr(msg, "chat"):
                return await client.send_message(msg.chat.id, text, reply_markup=clean_kb, parse_mode=enums.ParseMode.HTML)
        except Exception:
            pass
        return None
    except Exception as e:
        logger.warning(f"Safe edit failed: {e}")
        return None





async def _clear_utr_state(user_id: int):
    """Clear UTR input waiting state for user when they navigate away from UPI payment page."""
    try:
        await db.db.users.update_one(
            {"id": int(user_id)},
            {"$unset": {"pending_utr_story_id": 1, "pending_utr_opened_at": 1, "last_utr_entered": 1, "state": 1}}
        )
    except Exception:
        pass


async def _send_my_stories_menu(client, user_id: int, user: dict, lang: str, page: int = 0, reply_to_message=None, edit_query=None):
    from bson.objectid import ObjectId
    uid_int = int(user_id) if str(user_id).isdigit() else user_id
    uid_str = str(user_id)

    raw_purchases = user.get('purchases', []) if isinstance(user, dict) else []
    
    # Fetch all paid orders for this user across all active statuses
    paid_orders = await db.db.orders.find({
        "$or": [
            {"user_id": {"$in": [uid_int, uid_str]}},
            {"telegram_id": {"$in": [uid_int, uid_str]}}
        ],
        "status": {"$in": ["paid", "delivered", "approved", "completed", "success"]}
    }).sort("created_at", -1).to_list(length=300)

    # Also fetch approved checkouts (from bot checkout flow)
    paid_checkouts = await db.db.premium_checkout.find({
        "user_id": {"$in": [uid_int, uid_str]},
        "status": {"$in": ["approved", "paid", "completed", "success"]}
    }).sort("created_at", -1).to_list(length=100)

    # Collect all needed story ObjectIds and string IDs
    story_id_set = set()
    for p in raw_purchases:
        if p: story_id_set.add(str(p).strip())
    for o in paid_orders:
        if o.get("items"):
            for itm in o["items"]:
                if itm.get("story_id"): story_id_set.add(str(itm["story_id"]).strip())
                elif itm.get("id"): story_id_set.add(str(itm["id"]).strip())
        for sid in (o.get("story_ids") or []):
            if sid: story_id_set.add(str(sid).strip())
        if o.get("story_id"):
            story_id_set.add(str(o["story_id"]).strip())
    for c in paid_checkouts:
        if c.get("story_id"):
            story_id_set.add(str(c["story_id"]).strip())

    p_oids = []
    p_str_ids = []
    for s_str in story_id_set:
        if isinstance(s_str, str) and len(s_str) == 24:
            try: p_oids.append(ObjectId(s_str))
            except: pass
        p_str_ids.append(s_str)

    query_filter = []
    if p_oids:
        query_filter.append({"_id": {"$in": p_oids}})
    if p_str_ids:
        query_filter.append({"_id": {"$in": p_str_ids}})
        query_filter.append({"story_id": {"$in": p_str_ids}})

    valid_stories = []
    if query_filter:
        valid_stories_cursor = db.db.premium_stories.find({"$or": query_filter})
        valid_stories = await valid_stories_cursor.to_list(length=1000)

    # Isolate Show Store Mode: only show purchased shows belonging to this bot / platform
    bt_doc = None
    if hasattr(client, "me") and client.me:
        try:
            bt_doc = await _get_cached_bot_doc(client.me.id)
        except Exception:
            pass
    bt_cfg = (bt_doc.get("config") or {}) if bt_doc else {}
    is_show_store = (bt_cfg.get("bot_mode") == "show_store")
    bot_platform = bt_cfg.get("platform_name")

    if is_show_store:
        filtered_valid = []
        cfg_plat = str(bot_platform or "").strip().lower()
        for s in valid_stories:
            if s.get("is_show") is True:
                s_bot_id = s.get("bot_id")
                s_plat = str(s.get("platform") or "").strip().lower()
                if (s_bot_id and client.me and int(s_bot_id) == int(client.me.id)) or (cfg_plat and s_plat == cfg_plat):
                    filtered_valid.append(s)
        valid_stories = filtered_valid

    stories_map = {}
    for s in valid_stories:
        stories_map[str(s['_id'])] = s
        if s.get("story_id"):
            stories_map[str(s["story_id"])] = s

    # Build purchase list items (distinct per part and per whole story)
    purchased_items = []
    seen_keys = set()
    full_story_sids = set()

    for o in paid_orders:
        if o.get("items"):
            for itm in o["items"]:
                sid = str(itm.get("story_id") or itm.get("id") or "").strip()
                part_id = itm.get("part_id")
                st = stories_map.get(sid)
                if not st and sid in story_id_set:
                    for k, v in stories_map.items():
                        if str(v.get("_id")) == sid or str(v.get("story_id")) == sid:
                            st = v
                            break
                if st:
                    canonical_sid = str(st["_id"])
                    if part_id:
                        key = f"{canonical_sid}_{part_id}"
                        if key not in seen_keys:
                            seen_keys.add(key)
                            matched_p = None
                            for sp in (st.get("parts") or []):
                                if str(sp.get("id")).strip() == str(part_id).strip():
                                    matched_p = sp
                                    break
                            p_name = itm.get("part_name") or (matched_p.get("name") if matched_p else f"Part {part_id}")
                            p_ep = itm.get("episodes") or (matched_p.get("episodes") if matched_p else "")
                            purchased_items.append({
                                "key": key,
                                "story_id": canonical_sid,
                                "part_id": str(part_id),
                                "part_name": p_name,
                                "episodes": p_ep,
                                "story": st
                            })
                    else:
                        full_story_sids.add(canonical_sid)
                        key = canonical_sid
                        if key not in seen_keys:
                            seen_keys.add(key)
                            purchased_items.append({
                                "key": key,
                                "story_id": canonical_sid,
                                "part_id": None,
                                "story": st
                            })
        else:
            sids = o.get("story_ids") or ([o.get("story_id")] if o.get("story_id") else [])
            for sid in sids:
                sid_str = str(sid).strip()
                st = stories_map.get(sid_str)
                if st:
                    canonical_sid = str(st["_id"])
                    full_story_sids.add(canonical_sid)
                    if canonical_sid not in seen_keys:
                        seen_keys.add(canonical_sid)
                        purchased_items.append({
                            "key": canonical_sid,
                            "story_id": canonical_sid,
                            "part_id": None,
                            "story": st
                        })

    # Also process paid_checkouts (bot purchases)
    for c in paid_checkouts:
        sid = str(c.get("story_id") or "").strip()
        part_id = c.get("part_id")
        st = stories_map.get(sid)
        if st:
            canonical_sid = str(st["_id"])
            if part_id:
                key = f"{canonical_sid}_{part_id}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    matched_p = None
                    for sp in (st.get("parts") or []):
                        if str(sp.get("id")).strip() == str(part_id).strip():
                            matched_p = sp
                            break
                    p_name = c.get("part_name") or (matched_p.get("name") if matched_p else f"Part {part_id}")
                    p_ep = c.get("episodes") or (matched_p.get("episodes") if matched_p else "")
                    purchased_items.append({
                        "key": key,
                        "story_id": canonical_sid,
                        "part_id": str(part_id),
                        "part_name": p_name,
                        "episodes": p_ep,
                        "story": st
                    })
            else:
                full_story_sids.add(canonical_sid)
                if canonical_sid not in seen_keys:
                    seen_keys.add(canonical_sid)
                    purchased_items.append({
                        "key": canonical_sid,
                        "story_id": canonical_sid,
                        "part_id": None,
                        "story": st
                    })

    # Add any remaining legacy purchases from user.purchases (if not already listed as part or full)
    for p in raw_purchases:
        pid_str = str(p).strip()
        st = stories_map.get(pid_str)
        if st:
            canonical_sid = str(st["_id"])
            has_parts = any(item.get("story_id") == canonical_sid and item.get("part_id") is not None for item in purchased_items)
            if canonical_sid not in seen_keys and canonical_sid not in full_story_sids and not has_parts:
                seen_keys.add(canonical_sid)
                purchased_items.append({
                    "key": canonical_sid,
                    "story_id": canonical_sid,
                    "part_id": None,
                    "story": st
                })

    PAGE_SIZE = 5
    total = len(purchased_items)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    page_purchases = purchased_items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    kb = []
    for item in page_purchases:
        st = item["story"]
        name_en = st.get('story_name_en', 'Story')
        name_hi = st.get('story_name_hi', name_en)
        base_title = name_hi if lang == 'hi' else name_en
        if item.get("part_id"):
            p_label = item.get("part_name", "Part")
            ep_range = item.get("episodes", "")
            ep_str = f" · Ep {ep_range}" if ep_range else ""
            s_name = f"📖 {base_title} ({p_label}{ep_str})"
            cb = f"mb#purchased_view_{item['story_id']}_{item['part_id']}"
        else:
            s_name = f"📖 {base_title}"
            cb = f"mb#purchased_view_{item['story_id']}"
        kb.append([InlineKeyboardButton(s_name, callback_data=cb)])

    if is_show_store:
        plat_disp = bot_platform or "Shows"
        if lang == 'hi':
            title, total_txt, desc = f"⟦ मेरे {plat_disp} शोज़ ⟧", "कुल शोज़ ⟶", f"आपके द्वारा खरीदे गए {plat_disp} के सभी शोज़ नीचे उपलब्ध हैं।"
            next_btn, prev_btn, back_btn = "आगे ❭", "❬ पीछे", "« वापस मेनू"
            empty_txt = f"{plat_disp} का कोई खरीदा हुआ शो नहीं मिला।"
        else:
            title, total_txt, desc = f"⟦ 𝗠𝗬 {plat_disp.upper()} 𝗦𝗛𝗢𝗪𝗦 ⟧", "ᴛᴏᴛᴀʟ ⟶", f"𝖠𝗅𝗅 {plat_disp} 𝗌𝗁𝗈𝗐𝗌 𝗒𝗈𝗎 𝗁𝖺𝗏𝖾 𝗉𝗎𝗋𝖼𝗁𝖺𝗌𝖾𝖽 𝖺𝗋𝖾 𝗅𝗂𝗌𝗍𝖾𝖽 𝖻𝖾𝗅𝗈𝗐."
            next_btn, prev_btn, back_btn = "𝗡𝗲𝘅𝘁 ❭", "❬ 𝗣𝗿𝗲𝘃", _sc("BACK")
            empty_txt = f"ɴᴏ {plat_disp.upper()} ꜱʜᴏᴡꜱ ꜰᴏᴜɴᴅ."
    elif lang == 'hi':
        title, total_txt, desc = "⟦ मेरी स्टोरीज ⟧", "कुल स्टोरी ⟶", "आपके अकाउंट में मौजूद सभी स्टोरीज नीचे दी गई हैं।"
        next_btn, prev_btn, back_btn = "आगे ❭", "❬ पीछे", "« वापस मेनू"
        empty_txt, market_btn_l = "कोई खरीद नहीं मिली।", "स्टोर खोलें"
    else:
        title, total_txt, desc = "⟦ 𝗠𝗬 𝗦𝗧𝗢𝗥𝗜𝗘𝗦 ⟧", "ᴛᴏᴛᴀʟ ⟶", "𝖠𝗅𝗅 𝗌𝗍𝗈𝗋𝗂𝖾𝗌 𝗅𝗂𝗌𝗍𝖾𝖽 𝖻𝖾𝗅𝗈𝗐 𝖺𝗋𝖾 𝖺𝗅𝗋𝖾𝖺𝖽𝗒 𝗈𝗇 𝗒𝗈𝗎𝗋 𝖺𝖼𝖼𝗈𝗎𝗇ᴛ."
        next_btn, prev_btn, back_btn = "𝗡𝗲𝘅𝘁 ❭", "❬ 𝗣𝗿𝗲𝘃", _sc("BACK")
        empty_txt, market_btn_l = "ɴᴏ ᴘᴜʀᴄʜᴀꜱᴇꜱ ꜰᴏᴜɴᴅ.", _sc("OPEN MARKETPLACE")

    if total_pages > 1:
        nav = []
        if page > 0: nav.append(InlineKeyboardButton(prev_btn, callback_data=f"mb#my_buys_page_{page - 1}"))
        nav.append(InlineKeyboardButton(f"ᴘᴀɢᴇ {page + 1}/{total_pages}", callback_data="mb#noop"))
        if page < total_pages - 1: nav.append(InlineKeyboardButton(next_btn, callback_data=f"mb#my_buys_page_{page + 1}"))
        kb.append(nav)

    kb.append([InlineKeyboardButton(back_btn, callback_data="mb#main_back")])

    txt_b = f"<b>{title}</b>\n\n<b>{total_txt}</b> {total}\n\n{desc}" if total > 0 else f"<b>{title}</b>\n\n<b>{total_txt}</b> 0\n\n{empty_txt}"
    if total == 0 and not is_show_store: kb.insert(0, [InlineKeyboardButton(market_btn_l, callback_data="mb#main_marketplace")])

    if edit_query:
        await _safe_edit(edit_query.message, text=txt_b, markup=InlineKeyboardMarkup(kb))
    elif reply_to_message:
        await reply_to_message.reply_text(txt_b, reply_markup=InlineKeyboardMarkup(kb), parse_mode=enums.ParseMode.HTML)
    else:
        await client.send_message(user_id, txt_b, reply_markup=InlineKeyboardMarkup(kb), parse_mode=enums.ParseMode.HTML)


async def _send_main_menu(client, user_id: int, user, lang: str, reply_to_message_id: int = None):
    bt = await _get_cached_bot_doc(client.me.id)
    bt_cfg = bt.get("config", {}) if bt else {}
    bot_name = client.me.first_name
    msg_txt = _menu_card_text(user, bt_cfg, bot_name, lang)
    markup = _get_premium_menu_markup(bt_cfg, lang)

    # Menu media rotation: supports Photo / GIF / Video.
    items = [x for x in _cfg_list(bt_cfg, "menu_media") if isinstance(x, dict) and x.get("file_id")]
    if not items and (bt_cfg.get("menuimg") or "").strip():
        items = [{"type": "photo", "file_id": (bt_cfg.get("menuimg") or "").strip()}]

    # Try Bot API send first to preserve custom animated emoji buttons
    try:
        has_custom_emoji = any(
            hasattr(btn, "icon_custom_emoji_id") and btn.icon_custom_emoji_id
            for row in markup.inline_keyboard for btn in row
        )
        if has_custom_emoji:
            m_item = items[0] if items else None
            m_fid = (m_item.get("file_id") or "").strip() if m_item else None
            m_type = (m_item.get("type") or "photo").strip() if m_item else "photo"
            ok = await _send_or_edit_seller_bot_api(
                client=client,
                chat_id=user_id,
                text=msg_txt,
                markup=markup,
                media_id=m_fid,
                media_type=m_type
            )
            if ok:
                return
    except Exception as e:
        logger.debug(f"Bot API send main menu error: {e}")



    if items:

        import random

        random.shuffle(items)

        for it in items[:30]:

            t = (it.get("type") or "photo").strip()

            fid = (it.get("file_id") or "").strip()

            if not fid:

                continue

            try:

                if t == "animation":



                    return await client.send_animation(

                        user_id,

                        animation=fid,

                        caption=msg_txt,

                        reply_markup=markup,

                        parse_mode=enums.ParseMode.HTML,

                        reply_to_message_id=reply_to_message_id

                    )

                if t == "video":

                    return await client.send_video(

                        user_id,

                        video=fid,

                        caption=msg_txt,

                        reply_markup=markup,

                        parse_mode=enums.ParseMode.HTML,

                        reply_to_message_id=reply_to_message_id

                    )

                return await client.send_photo(

                    user_id,

                    photo=fid,

                    caption=msg_txt,

                    reply_markup=markup,

                    parse_mode=enums.ParseMode.HTML,

                    reply_to_message_id=reply_to_message_id

                )

            except Exception as e:

                # Auto-heal: remove broken media entries (MEDIA_EMPTY / expired file_id)

                logger.warning(f"Menu media send failed; pruning. type={t} err={e}")

                try:

                    await db.db.premium_bots.update_one(

                        {"id": client.me.id},

                        {"$pull": {"config.menu_media": {"file_id": fid}}}

                    )

                except Exception:

                    pass

                return await client.send_message(user_id, msg_txt, reply_markup=markup, parse_mode=enums.ParseMode.HTML, reply_to_message_id=reply_to_message_id)



    return await client.send_message(user_id, msg_txt, reply_markup=markup, parse_mode=enums.ParseMode.HTML, reply_to_message_id=reply_to_message_id)





def _fmt_delivery_text(tpl: str, user, story, sent_count: int = 0, fail_count: int = 0) -> str:

    safe_tpl = tpl or ""

    return (

        safe_tpl

        .replace("{user_id}", str(user.id if user else ""))

        .replace("{user_name}", (user.first_name or "User") if user else "User")

        .replace("{story}", str(story.get("story_name_en", "Story")))

        .replace("{price}", str(story.get("price", 0)))

        .replace("{sent}", str(sent_count))

        .replace("{failed}", str(fail_count))

    )





async def _delete_later(client, user_id: int, msg_ids: list, wait_seconds: int):

    await asyncio.sleep(wait_seconds)

    for mid in msg_ids:

        try:

            await client.delete_messages(user_id, mid)

        except Exception:

            pass





# ─────────────────────────────────────────────────────────────────

# Story Detail Preview (shown before T&C on deep links)

# ─────────────────────────────────────────────────────────────────

async def _show_story_preview(client, user_id, story, lang):

    """Show story name, image, description and episode count. User clicks Continue -> T&C."""

    name = story.get(f'story_name_{lang}', story.get('story_name_en', 'Unknown'))

    ep_count = story.get('file_count') or (len(story.get('valid_file_ids')) if story.get('valid_file_ids') else None) or (abs(story.get('end_id', 0) - story.get('start_id', 0)) + 1 if story.get('end_id') else "?")

    platform = story.get('platform', 'Other')

    price = story.get('price', 0)

    desc = story.get('description', 'Premium audio story — exclusive content.')

    s_id = str(story['_id'])



    txt = (

        f"<b>📖 {_sc(name)}</b>\n\n"

        f"<b>{_sc('Platform')}:</b> {platform}\n"

        f"<b>{_sc('Episodes')}:</b> ~{ep_count}\n"

        f"<b>{_sc('Price')}:</b> ₹{price}\n\n"

        f"<blockquote expandable>{_sc(desc)}</blockquote>"

    )

    kb = [[InlineKeyboardButton(f"▶️ {_sc('CONTINUE TO PURCHASE')}", callback_data=f"mb#story_preview_continue_{s_id}")]]



    img = story.get('image_url')

    if img:

        try:

            await client.send_photo(

                user_id,

                photo=img,

                caption=txt,

                reply_markup=InlineKeyboardMarkup(kb),

                parse_mode=enums.ParseMode.HTML

            )

            return

        except Exception:

            pass

    await client.send_message(user_id, txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=enums.ParseMode.HTML)





# ─────────────────────────────────────────────────────────────────

# T&C (with Accept and Reject buttons - all inline, no native_ask)

# ─────────────────────────────────────────────────────────────────

def to_mathbold(text: str) -> str:

    return text.translate(str.maketrans(

        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",

        "𝐚𝐛𝐜𝐝𝐞𝐟𝐠𝐡𝐢𝐣𝐤𝐥𝐦𝐧𝐨𝐩𝐪𝐫𝐬𝐭𝐮𝐯𝐰𝐱𝐲𝐳𝐀𝐁𝐂𝐃𝐄𝐅𝐆𝐇𝐈𝐉𝐊𝐋𝐌𝐍𝐎𝐏𝐐𝐑𝐒𝐓𝐔𝐕𝐖𝐗𝐘𝐙𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗"

    ))



def to_mathitalic(text: str) -> str:

    return text.translate(str.maketrans(

        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",

        "𝑎𝑏𝑐𝑑𝑒𝑓𝑔ℎ𝑖𝑗𝑘𝑙𝑚𝑛𝑜𝑝𝑞𝑟𝑠𝑡𝑢𝑣𝑤𝑥𝑦𝑧𝐴𝐵𝐶𝐷𝐸𝐹𝐺𝐻𝐼𝐽𝐾𝐿𝑀𝑁𝑂𝑃𝑄𝑅𝑆𝑇𝑈𝑉𝑊𝑋𝑌𝑍"

    ))



async def _fetch_url_bytes(url: str) -> bytes | None:
    """Download image bytes from HTTP URL with timeout and User-Agent."""
    try:
        import aiohttp
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=6.0)) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    if data and len(data) > 500:
                        return data
    except Exception:
        pass
    return None


async def _send_story_photo_bytes(client, user_id: int, img_bytes: bytes, caption: str, reply_markup=None, story: dict = None) -> bool:
    """Uploads raw image bytes directly via Telegram Bot API multipart sendPhoto."""
    bot_token = await _get_seller_bot_token(client)
    if not bot_token:
        return False
    try:
        import aiohttp
        import json
        import re
        api_text = re.sub(r'<emoji id="(\d+)">([^<]*)</emoji>', r'<tg-emoji emoji-id="\1">\2</tg-emoji>', caption)
        api_kb = _markup_to_bot_api_list(reply_markup) if reply_markup else []

        data = aiohttp.FormData()
        data.add_field("chat_id", str(user_id))
        data.add_field("caption", api_text)
        data.add_field("parse_mode", "HTML")
        if api_kb:
            data.add_field("reply_markup", json.dumps({"inline_keyboard": api_kb}))
        data.add_field("photo", img_bytes, filename="story_banner.jpg", content_type="image/jpeg")

        url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=10.0)) as resp:
                res = await resp.json()
                if res.get("ok"):
                    # Cache the new file_id for THIS bot so future queries are instant
                    try:
                        photos = res.get("result", {}).get("photo", [])
                        if photos and story and story.get("_id"):
                            new_fid = photos[-1].get("file_id")
                            if new_fid:
                                await db.db.premium_stories.update_one({"_id": story["_id"]}, {"$set": {"image": new_fid}})
                    except Exception:
                        pass
                    return True
                else:
                    logger.debug(f"Bot API sendPhoto multipart returned: {res}")
    except Exception as e:
        logger.debug(f"Exception in _send_story_photo_bytes: {e}")
    return False


async def _send_story_photo(client, user_id: int, story: dict, caption: str, reply_markup=None, fallback_photo: str = None):
    """
    Robustly sends a story banner image to user across bots:
    1. Tests HTTP/CDN URLs (poster_url, banner_url, image_url, cover_url) via in-memory bytes upload.
    2. Tests direct file_id (with cross-bot download recovery if file_id was created on an old bot).
    3. Caches new valid file_id on story in MongoDB for this bot.
    4. Falls back gracefully to text message if no image could be delivered.
    """
    from pyrogram import enums
    import io

    # ── 1. Check for Public HTTP / CDN URLs first (Catbox, R2, Mini App) ──
    http_candidates = []
    for k in ("poster_url", "banner_url", "image_url", "cover_url", "cover", "thumbnail"):
        val = story.get(k)
        if val and isinstance(val, str):
            val = val.strip()
            if val.startswith("http://") or val.startswith("https://"):
                if val not in http_candidates: http_candidates.append(val)
            elif val.startswith("/") or val.startswith("uploads/") or val.startswith("static/"):
                full_url = "https://aryapremium.store/" + val.lstrip("/")
                if full_url not in http_candidates: http_candidates.append(full_url)

    if fallback_photo and isinstance(fallback_photo, str) and fallback_photo.startswith("http"):
        if fallback_photo not in http_candidates: http_candidates.append(fallback_photo)

    for h_url in http_candidates:
        img_bytes = await _fetch_url_bytes(h_url)
        if img_bytes:
            ok = await _send_story_photo_bytes(client, user_id, img_bytes, caption, reply_markup, story)
            if ok:
                return True
            try:
                # Try Pyrogram byte stream fallback
                return await client.send_photo(
                    chat_id=user_id,
                    photo=io.BytesIO(img_bytes),
                    caption=caption,
                    reply_markup=reply_markup,
                    parse_mode=enums.ParseMode.HTML
                )
            except Exception as e:
                logger.debug(f"Pyrogram BytesIO send_photo failed: {e}")

    # ── 2. Check direct file_id candidates ──
    fid_candidates = []
    for k in ("image", "poster", "banner"):
        val = story.get(k)
        if val and isinstance(val, str) and not val.startswith("http") and not val.startswith("/"):
            if val not in fid_candidates: fid_candidates.append(val)

    has_custom_emoji = reply_markup and any(
        hasattr(btn, "icon_custom_emoji_id") and btn.icon_custom_emoji_id
        for row in reply_markup.inline_keyboard for btn in row
    )

    # Try sending file_id directly
    for photo_ref in fid_candidates:
        if has_custom_emoji:
            try:
                ok = await _send_or_edit_seller_bot_api(
                    client=client,
                    chat_id=user_id,
                    text=caption,
                    markup=reply_markup,
                    media_id=photo_ref,
                    media_type="photo"
                )
                if ok:
                    return True
            except Exception:
                pass

        try:
            return await client.send_photo(
                chat_id=user_id,
                photo=photo_ref,
                caption=caption,
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.HTML
            )
        except Exception as e:
            logger.debug(f"Direct file_id send_photo failed: {e}")

    # ── 3. Cross-Bot File ID Recovery (if file_id belongs to old delivery bot) ──
    for photo_ref in fid_candidates:
        for b_str, other_cli in list(market_clients.items()):
            if getattr(getattr(other_cli, "me", None), "id", None) == getattr(getattr(client, "me", None), "id", None):
                continue
            try:
                dl = await other_cli.download_media(photo_ref, in_memory=True)
                if dl:
                    dl_bytes = bytes(dl.getbuffer())
                    if dl_bytes:
                        ok = await _send_story_photo_bytes(client, user_id, dl_bytes, caption, reply_markup, story)
                        if ok:
                            return True
            except Exception:
                continue

    # ── 4. Final Text Fallback ──
    if has_custom_emoji:
        try:
            ok = await _send_or_edit_seller_bot_api(
                client=client,
                chat_id=user_id,
                text=caption,
                markup=reply_markup
            )
            if ok:
                return True
        except Exception:
            pass

    return await client.send_message(
        chat_id=user_id,
        text=caption,
        reply_markup=reply_markup,
        parse_mode=enums.ParseMode.HTML
    )

async def _show_story_profile(client, user_id, story, lang):

    name = story.get(f'story_name_{lang}', story.get('story_name_en', 'Unknown'))

    status = story.get('status', 'Unknown')

    platform = story.get('platform', 'Unknown')

    genre = story.get('genre', 'Unknown')

    episodes = story.get('episodes', 'Unknown')

    image = story.get('image') or story.get('poster_url') or story.get('image_url')



    if lang == 'hi':

        status_lbl = "स्टेटस"

        plat_lbl = "प्लेटफॉर्म"

        genre_lbl = "जौनर"

        ep_lbl = "एपिसोड्स"

        desc_lbl = "कहानी का विवरण"

        confirm_btn = "आगे बढ़ें"
        back_btn = "❮ वापस"
        loading_txt = f"{name} लोड हो रही है..."
    else:
        status_lbl = "Status"
        plat_lbl = "Platform"
        genre_lbl = "Genre"
        ep_lbl = "Episodes"
        desc_lbl = "Story Description"
        confirm_btn = "Confirm"
        back_btn = f"❮ {_sc('BACK')}"
        loading_txt = _sc(f"LOADING {name.upper()}...")

    desc = story.get(f'description_{lang}', story.get('description', '')).strip()
    
    delivery_mode = story.get('delivery_mode', 'pool')
    del_hi = "केवल डायरेक्ट DM (कोई चैनल नहीं)" if delivery_mode == "dm_only" else "चैनल लिंक और DM"
    del_en = "Direct DM Only (No Channel Link)" if delivery_mode == "dm_only" else "Channel Invite + DM"
    del_lbl = "डिलीवरी" if lang == "hi" else "Delivery"
    del_val = del_hi if lang == "hi" else del_en
    
    price = int(story.get('price', 0))
    if price > 0:
        if price <= 50: mrp = 149
        elif price <= 100: mrp = 299
        elif price <= 200: mrp = 599
        elif price <= 300: mrp = 899
        else: mrp = int(price * 2.5)
        calc_off = int(((mrp - price) / mrp) * 100)
        p_lbl = "कीमत" if lang == "hi" else "Price"
        # Shopping app style: M.R.P: ̶₹̶̶1̶̶4̶̶9̶ Deal Price: ₹49
        price_line = f'<b><emoji id="5886285355279193209">🏷</emoji> {p_lbl}:</b> <s>₹{mrp}</s>  <b>₹{price}</b> <i>({calc_off}% OFF)</i>\n'
    else:
        price_line = ""

    files_lbl = "फ़ाइलें" if lang == "hi" else "Files"
    actual_files = story.get('file_count') or (len(story.get('valid_file_ids')) if story.get('valid_file_ids') else None)
    files_line = f'<b><emoji id="5805550320985578625">📁</emoji> {files_lbl}:</b> <b>{actual_files}</b>\n' if actual_files else ""

    if story.get('is_show') or story.get('duration'):
        # ── Show Store OTT Format (No paid note in bot preview) ──
        dur = story.get('duration', 'Full Show')
        p_val = story.get('price', 19)
        txt = (
            f'<emoji id="5937999673510858217">📽️</emoji> <b>Show :</b> {to_mathbold(name)}\n'
            f'<emoji id="6026337676091726218">🖥</emoji> <b>Platform :</b> <b>{platform}</b>\n'
            f'<emoji id="6024065724291488135">🧩</emoji> <b>Genre :</b> <b>{genre}</b>\n'
            f'<emoji id="5807622114424924272">🎬</emoji> <b>Duration :</b> <b>{dur}</b>\n'
            f'<emoji id="5904462880941545555">💰</emoji> <b>Price :</b> <b>₹{p_val}</b>\n'
        )
    else:
        header_txt = (
            f'<b><emoji id="5465432711218863135">♨️</emoji> Story:</b> {to_mathbold(name)}\n'
            f'<b><emoji id="6019118553326689234">🔰</emoji> {status_lbl}:</b> <b>{status}</b>\n'
            f'<b><emoji id="6019455905827920171">🖥</emoji> {plat_lbl}:</b> <b>{platform}</b>\n'
            f'<b><emoji id="6024065724291488135">🧩</emoji> {genre_lbl}:</b> <b>{genre}</b>\n'
            f"{price_line}"
            f'<b><emoji id="5937999673510858217">🎬</emoji> {ep_lbl}:</b> <b>{episodes}</b>\n'
            f"{files_line}"
            f'<b><emoji id="5776182936638329359">📥</emoji> {del_lbl}:</b> <i>{del_val}</i>\n\n'
        )

        if desc and desc.lower() != "none":
            MAX_DESC = 700
            desc_preview = desc[:120].rstrip() + ("…" if len(desc) > 120 else "")
            desc_full = desc if len(desc) <= MAX_DESC else desc[:MAX_DESC].rstrip() + "…"
            header_txt += (
                f'<emoji id="6021620268697393273">📝</emoji> <b>{desc_lbl}</b>\n'
                f"<blockquote expandable>"
                f"{to_mathbold(desc_full)}"
                f"</blockquote>\n"
            )

        txt = header_txt

    # Safety: if total text still exceeds 1020 chars, strip from description end
    MAX_CAPTION = 1020
    if len(txt) > MAX_CAPTION and image:
        # Rebuild with a shorter desc to guarantee image fits
        overflow = len(txt) - MAX_CAPTION
        # Trim desc_full further
        safe_desc = desc_full[:max(60, len(desc_full) - overflow - 10)].rstrip() + "…"
        txt = header_txt.replace(to_mathbold(desc_full), to_mathbold(safe_desc))
        
    demo_btn = "डेमो फ़ाइलें देखें" if lang == "hi" else "View Demo Files"
    kb = [
        [_ikb(confirm_btn, callback_data=f"mb#show_tc#{str(story['_id'])}", icon_custom_emoji_id="6273749318717412886")],
        [_ikb(demo_btn, callback_data=f"mb#demo#{str(story['_id'])}", icon_custom_emoji_id="5305388752162539722")],
        [InlineKeyboardButton(back_btn, callback_data="mb#return_main")]
    ]
    markup = InlineKeyboardMarkup(kb)

    from pyrogram import enums
    tmp = await client.send_message(user_id, f'<b>› › <emoji id="5348471079482441278">⏳</emoji> {loading_txt}</b>', reply_markup=ReplyKeyboardRemove(), parse_mode=enums.ParseMode.HTML)
    try:
        await _send_story_photo(client, user_id, story, caption=txt, reply_markup=markup)
        await tmp.delete()
    except Exception:
        pass


async def _show_tc(client, user_id, story_id, lang='en', from_user=None):
    if lang == 'hi':
        tc_title = "<b>⟦ नियम और शर्तें ⟧</b>"
        tc_subtitle = "खरीदने से पहले, कृपया निम्नलिखित पढ़ें और सहमत हों:"
        missing_title = "• <b>गायब एपिसोड</b>"
        missing_desc = "सार्वजनिक रूप से जारी न होने पर 3-4 एपिसोड अनुपलब्ध हो सकते हैं। ऐसा होने पर हम अपनी तरफ से कहानी की कीमत कम रखते हैं। यदि वे एपिसोड हमें बाद में मिलते हैं, तो उन्हें आपके वर्तमान एपिसोड्स में जोड़ दिया जाएगा। यदि 4 से अधिक एपिसोड गायब हैं, तो कृपया सपोर्ट से संपर्क करें।"
        quality_title = "• <b>क्वालिटी</b>"
        quality_desc = "पुराने एपिसोड्स की क्वालिटी कम हो सकती है। हम 100% क्वालिटी की गारंटी नहीं दे सकते, लेकिन हमेशा सर्वश्रेष्ठ वर्जन प्रदान करेंगे।"
        refund_title = "• <b>कोई रिफंड नहीं</b>"
        refund_desc = "एक बार भुगतान हो जाने और डिलीवरी शुरू होने के बाद कोई रिफंड नहीं दिया जाएगा। यदि आपका टेलीग्राम अकाउंट सस्पेंड या डिलीट हो जाता है, तो पूर्ण विवरण और प्रमाण के साथ एडमिन से संपर्क करें, आपको पुनः जोड़ दिया जाएगा। यदि आप गलती से अतिरिक्त राशि का भुगतान कर देते हैं, तो तुरंत प्रमाण के साथ संपर्क करें, आपको रिफंड मिल जाएगा (Razorpay पर प्लेटफ़ॉर्म फीस काटी जाएगी)।"
        fake_title = "• <b>नकली स्क्रीनशॉट</b>"
        fake_desc = "नकली या अमान्य भुगतान प्रमाण भेजने पर स्थायी रूप से प्रतिबंध लगा दिया जाएगा।"
        iaadnsa_note = "<b>• IAADNSA (भविष्य में T&C छोड़ें)</b>\nयदि आप चाहते हैं कि अगली बार कहानी खरीदते समय आपको यह नियम और शर्तें (T&C) पेज न दिखाई दे, तो <b>IAADNSA (I Accept And Do Not Show Again)</b> बटन पर क्लिक करें। इससे भविष्य में यह पेज अपने आप बाईपास हो जाएगा।"
        accept_btn = "I Accept"
        reject_btn = "Reject"
        iaadnsa_btn = "IAADNSA"
        back_btn = "‹ वापस"
    else:
        tc_title = "<b>⟦ 𝗧𝗘𝗥𝗠𝗦 & 𝗖𝗢𝗡𝗗𝗜𝗧𝗜𝗢𝗡𝗦 ⟧</b>"
        tc_subtitle = "𝖡𝖾𝖿𝗈𝗋𝖾 𝗉𝗎𝗋𝖼𝗁𝖺𝗌𝗂𝗇𝗀, 𝗉𝗅𝖾𝖺𝗌𝖾 𝗋𝖾𝖺𝖽 𝖺𝗇𝖽 𝖺𝗀𝗋𝖾𝖾 𝗍𝗈 𝗍𝗁𝖾 𝖿𝗈𝗅𝗅𝗈𝗐𝗂𝗇𝗀:"
        missing_title = "• <b>𝗠𝗶𝘀𝘀𝗶𝗻𝗴 𝗘𝗽𝗶𝘀𝗼𝗱𝗲𝘀</b>"
        missing_desc = "3-4 episodes may be missing if not publicly released. In such cases, we keep the story price lower from our side. If we find those episodes later, they will be automatically added to your current episodes. If more than 4 episodes are missing, please contact support."
        quality_title = "• <b>𝗤𝘂𝗮𝗹𝗶𝘁𝘆</b>"
        quality_desc = "𝖲𝗈𝗆𝖾 𝗈𝗅𝖽𝖾𝗋 𝖾𝗉𝗂𝗌𝗈𝖽𝖾𝗌 𝗆𝖺𝗏 𝗁𝖺𝗏𝖾 𝗋𝖾𝖽𝗎𝖼𝖾𝖽 𝗊𝗎𝖺𝗅𝗂𝗍𝗒. 𝖶𝖾 𝖼𝖺𝗇𝗇𝗈𝗍 𝗀𝗎𝖺𝗋𝖺𝗇𝗍𝖾𝖾 𝟣𝟢𝟢% 𝗊𝗎𝖺𝗅𝗂𝗍𝗒, 𝖻𝗎𝚝 𝖺𝗅𝗐𝖺𝗒𝗌 𝗉𝗋𝗈𝗏𝗂𝖽𝖾 𝖻𝖾𝗌𝗍 𝗏𝖾𝗋𝗌𝗂𝗈𝗇."
        refund_title = "• <b>𝗡𝗼 𝗥𝗲𝗳𝘂𝗻𝗱𝘀</b>"
        refund_desc = "No refunds once payment is confirmed and delivery starts. If your Telegram account gets suspended or deleted, contact the admin with complete details and proof to be added again. If you accidentally pay an extra amount, contact us immediately with proof for a refund (Razorpay platform fees will be deducted)."
        fake_title = "• <b>𝗙𝗮𝗸𝗲 𝗦𝗰𝗿𝗲𝗲𝗻𝘀𝗵𝗼𝘁𝘀</b>"
        fake_desc = "𝖥𝖺𝗄𝖾 𝗈𝗋 𝗂𝗇𝗏𝖺𝗅𝗂𝖽 𝗉𝖺𝗒𝗆𝖾𝗇𝗍 𝗉𝗋𝗈𝗈𝖿𝗌 𝗐𝗂𝗅𝗅 𝗅𝖾𝖺𝖽 𝗍𝗈 𝗉𝖾𝗋𝗆𝖺𝗇𝖾𝗇𝗍 𝖻𝖺𝗇."
        iaadnsa_note = "<b>• IAADNSA (Skip Future T&C)</b>\nIf you don't want to see this Terms & Conditions page for future purchases, click the <b>IAADNSA (I Accept And Do Not Show Again)</b> button. This will automatically accept the T&C and skip this page in the future."
        accept_btn = "𝗜 𝗔𝗰𝗰𝗲𝗽𝘁"
        reject_btn = "𝗥𝗲𝗷𝗲𝗰𝘁"
        iaadnsa_btn = "𝗜𝗔𝗔𝗗𝗡𝗦𝗔"
        back_btn = "‹ Back"

    from bson.objectid import ObjectId
    full_name, uname_str, _, _, _ = get_user_fast_info(from_user=from_user, user_id=user_id)
    
    s_obj = await db.db.premium_stories.find_one({"_id": ObjectId(story_id)}, {"story_name_hi": 1, "story_name_en": 1})
    s_name = s_obj.get(f'story_name_{lang}', s_obj.get('story_name_en', 'Unknown')) if s_obj else 'Unknown'

    user_details = f'👤 <b>User:</b> {full_name} ({uname_str}) | <b>ID:</b> <code>{user_id}</code>\n<emoji id="6023962911364357003">📖</emoji> <b>Story:</b> {s_name}\n'

    tc_text = (
        f"{tc_title}\n\n"
        f"{user_details}\n"
        f"{tc_subtitle}\n\n"
        f"<blockquote expandable>{missing_title}\n{missing_desc}</blockquote>\n"
        f"<blockquote expandable>{quality_title}\n{quality_desc}</blockquote>\n"
        f"<blockquote expandable>{refund_title}\n{refund_desc}</blockquote>\n"
        f"<blockquote expandable>{fake_title}\n{fake_desc}</blockquote>\n"
        f"<blockquote expandable>{iaadnsa_note}</blockquote>"
    )

    kb = [
        [_ikb(accept_btn, callback_data=f"mb#tc_accept_{story_id}", icon_custom_emoji_id="6273749318717412886"),
         InlineKeyboardButton(reject_btn, callback_data="mb#tc_reject")],
        [_ikb(iaadnsa_btn, callback_data=f"mb#tc_iaadnsa_{story_id}", icon_custom_emoji_id="6273749318717412886"),
         InlineKeyboardButton(back_btn, callback_data=f"mb#view_{story_id}")]
    ]
    from pyrogram import enums
    await client.send_message(user_id, tc_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=enums.ParseMode.HTML)



def _cfg_list(cfg: dict, key: str):

    v = (cfg or {}).get(key)

    return v if isinstance(v, list) else []





# ─────────────────────────────────────────────────────────────────

# Story Payment Detail

# ─────────────────────────────────────────────────────────────────

def _is_upi_restricted() -> bool:

    """Returns True if current IST time is between 9 PM (21:00) and 6 AM (06:00)."""

    from datetime import timezone, timedelta

    ist = timezone(timedelta(hours=5, minutes=30))

    now_ist = datetime.now(ist)

    h = now_ist.hour

    return h >= 21 or h < 6





def _upi_availability(bot_cfg: dict) -> dict:

    """

    Returns a dict:

      { 'available': bool, 'reason': str, 'until': str }

    

    upi_enabled values:

      True  → admin force-on (override auto-schedule)

      False → admin manually disabled

      None/missing → auto schedule (9 PM – 6 AM off)

    """

    from datetime import timezone, timedelta

    ist = timezone(timedelta(hours=5, minutes=30))

    now_ist = datetime.now(ist)

    h = now_ist.hour



    upi_enabled = bot_cfg.get('upi_enabled', None)  # None = use auto schedule



    if upi_enabled is False:

        # Admin manually turned off

        return {

            'available': False,

            'reason': 'manual',

            'until': None  # no auto-resume time

        }



    if upi_enabled is True:

        # Admin force-enabled (overrides schedule)

        return {'available': True, 'reason': 'force_on', 'until': None}



    # Auto schedule mode (upi_enabled is None / missing key)

    in_restricted = h >= 21 or h < 6

    if not in_restricted:

        return {'available': True, 'reason': 'schedule', 'until': None}

    else:

        # Calculate minutes until 6 AM IST

        minutes_to_6am = ((6 - h - 1) % 24) * 60 + (60 - now_ist.minute)

        if minutes_to_6am >= 60:

            until_str = f"{minutes_to_6am // 60}h {minutes_to_6am % 60}m"

        else:

            until_str = f"{minutes_to_6am}m"

        return {

            'available': False,

            'reason': 'schedule',

            'until': f"6:00 AM IST (in ~{until_str})"

        }





async def _show_story_details(client, msg_or_query, story, lang, bot_cfg: dict = None):

    # ── Checkout Mode Routing ──────────────────────────────────────────────────
    # Admin can switch between V1 (Razorpay + Manual UPI) and V2 (Direct UPI + Crypto)
    try:
        _feat = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
        chk_mode = _feat.get("checkout_mode", "v1")
        logger.info(f"[CHECKOUT] Resolved checkout_mode: '{chk_mode}' for story {story.get('_id')}")
        if chk_mode == "v2":
            return await _show_story_details_v2(client, msg_or_query, story, lang, bot_cfg=bot_cfg)
    except Exception as ex:
        logger.error(f"[CHECKOUT] Error checking checkout_mode: {ex}", exc_info=True)
    # ──────────────────────────────────────────────────────────────────────────

    from pyrogram.types import Message, CallbackQuery

    from pyrogram import enums

    is_msg = isinstance(msg_or_query, Message)

    user_id = msg_or_query.chat.id if is_msg else msg_or_query.from_user.id

    

    bot_cfg = bot_cfg or {}


    name = story.get(f'story_name_{lang}', story.get('story_name_en', 'Unknown'))

    price = int(story.get('price', 1))

    

    if price > 0:

        if price <= 50: mrp = 149

        elif price <= 100: mrp = 299

        elif price <= 200: mrp = 599

        elif price <= 300: mrp = 899

        else: mrp = int(price * 2.5)

        calc_off = int(((mrp - price) / mrp) * 100)

        p_str = f"<s>₹{mrp}</s>  <b>₹{price}</b> <i>({calc_off}% OFF)</i>"

    else:

        p_str = f"<b>₹{price}</b>"

    

    if lang == 'hi':
        title = "⟦ सुरक्षित चेकआउट ⟧"
        item_lbl = "आइटम"
        price_lbl = "कुल कीमत"
        rzp_title = "✅ ऑटोमैटिक पेमेंट (Razorpay)"
        rzp_desc = "• <b>फायदे:</b> तत्काल एक्सेस (No waiting), 24/7 सुलभ।\n• <b>पेमेंट मोड:</b> UPI, डेबिट कार्ड, वॉलेट, नेट बैंकिंग।\n• <b>वेरिफिकेशन:</b> पेमेंट सफल होते ही अपने आप।"
        upi_title = '<emoji id="5264895611517300926">🏦</emoji> मैनुअल पेमेंट (Manual UPI)'
        upi_desc = "• <b>प्रोसेस:</b> पे करें -> स्क्रीनशॉट भेजें -> एडमिन चेक करेगा।\n• <b>पेमेंट मोड:</b> केवल UPI ऐप्स (PhonePe, GPay, etc.)।\n• <b>वेरिफिकेशन:</b> इसमें 5-10 मिनट का समय लग सकता है।"
        pay_gateway_btn = "पेमेंट गेटवे से भुगतान (Razorpay)"
        pay_upi_btn = "मैनुअल यूपीआई (Manual UPI)"
        unavailable_upi = "यूपीआई भुगतान अभी बंद है।"
        back_btn = "❮ वापस"
    else:
        title = "⟦ 𝗦𝗘𝗖𝗨𝗥𝗘 𝗖𝗛𝗘𝗖𝗞𝗢𝗨𝗧 ⟧"
        item_lbl = "Item"
        price_lbl = "Total Price"
        rzp_title = "✅ 𝗔𝘂𝘁𝗼𝗺𝗮𝘁𝗶𝗰 𝗣𝗮𝘆𝗺𝗲𝗻𝘁 (𝗥𝗮𝘇𝗼𝗿𝗽𝗮𝘆)"
        rzp_desc = "• <b>Benefits:</b> Instant Access (No waiting), 24/7 available.\n• <b>Modes:</b> UPI, Debit Card, Wallets, Net Banking.\n• <b>Verification:</b> Automatically upon successful payment."
        upi_title = '<emoji id="5264895611517300926">🏦</emoji> 𝗠𝗮𝗻𝘂𝗮𝗹 𝗣𝗮𝘆𝗺𝗲𝗻𝘁 (𝗠𝗮𝗻𝘂𝗮𝗹 𝗨𝗣𝗜)'
        upi_desc = "• <b>Process:</b> Pay -> Send Screenshot -> Admin Verify.\n• <b>Modes:</b> Only UPI Apps (PhonePe, GPay, etc.).\n• <b>Verification:</b> Manual (Takes 5-10 minutes)."
        pay_gateway_btn = _sc('PAY VIA RAZORPAY')
        pay_upi_btn = _sc('PAY VIA MANUAL UPI')
        unavailable_upi = "UPI Currently Unavailable"
        back_btn = f"❮ {_sc('BACK')}"

    # ── UPI availability check ───────────────────────────────────────────────
    from .market_seller import _upi_availability
    upi_status = _upi_availability(bot_cfg)
    upi_ok = upi_status['available']

    # Build UPI block text based on availability
    if upi_ok:
        upi_block = f"<blockquote expandable=\"true\">{upi_title}\n{upi_desc}</blockquote>"
    else:
        # Reason-specific unavailability message
        if upi_status['reason'] == 'schedule':
            until_note = upi_status.get('until', '6:00 AM IST')
            if lang == 'hi':
                upi_block = (
                    f"<blockquote expandable=\"true\"><b>⏸ मैनुअल UPI अभी उपलब्ध नहीं है।</b>\n\n"
                    f"• रात्रि 9 बजे से सुबह 6 बजे के बीच सुरक्षा कारणों से मैनुअल UPI स्वचालित रूप से बंद रहता है।\n"
                    f"• UPI फिर से उपलब्ध होगा: <b>{until_note}</b>\n\n"
                    f"Razorpay से तत्काल पेमेंट करें — UPI, Debit Card, Net Banking सभी स्वीकार होते हैं।</blockquote>"
                )
            else:
                upi_block = (
                    f"<blockquote expandable=\"true\"><b>⏸ Manual UPI is currently unavailable.</b>\n\n"
                    f"• Manual UPI is automatically paused between 9 PM – 6 AM IST for security.\n"
                    f"• UPI will be available again at: <b>{until_note}</b>\n\n"
                    f"Use Razorpay for instant payment — accepts UPI, Debit Card &amp; Net Banking.</blockquote>"
                )
        else:  # manual off by admin
            if lang == 'hi':
                upi_block = (
                    f"<blockquote expandable=\"true\"><b>⏸ मैनुअल UPI अभी अस्थायी रूप से बंद है।</b>\n\n"
                    f"• एडमिन ने फिलहाल मैनुअल UPI बंद किया है।\n"
                    f"• Razorpay से पेमेंट करें — UPI, Debit Card, Net Banking स्वीकार।</blockquote>"
                )
            else:
                upi_block = (
                    f"<blockquote expandable=\"true\"><b>⏸ Manual UPI is temporarily unavailable.</b>\n\n"
                    f"• The admin has disabled Manual UPI for now.\n"
                    f"• Please use Razorpay to complete your payment — accepts UPI, Debit Card &amp; Net Banking.</blockquote>"
                )

    txt = (
        f"<b>{title}</b>\n\n"
        f"<b>{item_lbl} :</b> <code>{name}</code>\n"
        f"<b>{price_lbl} :</b> {p_str}\n\n"
        f"<blockquote expandable=\"true\">{rzp_title}\n{rzp_desc}</blockquote>\n"
        f"{upi_block}"
    )

    # Determine which payment methods are enabled for this specific story
    story_methods = story.get("payment_methods", ["upi", "razorpay"])
    show_razorpay = "razorpay" in story_methods
    show_upi = "upi" in story_methods

    kb = []
    # Razorpay row - only if enabled for this story
    if show_razorpay:
        kb.append([_ikb(pay_gateway_btn, callback_data=f"mb#pay#razorpay#{str(story['_id'])}", icon_custom_emoji_id="6030410254276106984")])
    
    # UPI row - only if enabled for this story
    if show_upi:
        if upi_ok:
            kb.append([_ikb(pay_upi_btn, callback_data=f"mb#pay#upi#{str(story['_id'])}", icon_custom_emoji_id="5766975922620076409")])

        else:

            kb.append([InlineKeyboardButton(f"⏸ {unavailable_upi}", callback_data="mb#noop")])

    

    # If neither method is enabled (fallback), show message

    if not show_razorpay and not show_upi:

        kb.append([InlineKeyboardButton("⚠️ No payment method available", callback_data="mb#noop")])

        

    kb.append([InlineKeyboardButton(back_btn, callback_data="mb#return_main")])

    markup = InlineKeyboardMarkup(kb)



    IMG_URL = "https://files.catbox.moe/4ud7fx.png"

    # Delete previous message as we are replacing text with an image
    try:
        if is_msg:
            await msg_or_query.delete()
        else:
            await msg_or_query.message.delete()
    except Exception:
        pass

    await _send_story_photo(
        client=client,
        user_id=user_id,
        story=story,
        caption=txt,
        reply_markup=markup,
        fallback_photo=IMG_URL
    )



async def _show_story_details_v2(client, msg_or_query, story, lang, bot_cfg: dict = None):
    """
    Shows V2 Checkout Page with Direct UPI, Cashfree (Cards/NetBanking/UPI), and Crypto (OxaPay).
    """
    from pyrogram.types import Message, CallbackQuery
    is_msg = isinstance(msg_or_query, Message)
    user_id = msg_or_query.chat.id if is_msg else msg_or_query.from_user.id
    name = story.get(f'story_name_{lang}', story.get('story_name_en', 'Unknown'))
    price = story.get('price', 0)
    p_str = f"₹{price}" if price > 0 else "FREE"

    bot_cfg = bot_cfg if isinstance(bot_cfg, dict) else {}

    from cashfree_helper import get_cashfree_config
    cf_cfg = await get_cashfree_config()
    show_cashfree = bool(cf_cfg.get("enabled", False))

    if lang == 'hi':
        title = "⟦ सुरक्षित चेकआउट ⟧"
        item_lbl = "कहानी"
        price_lbl = "कुल राशि"
        
        upi_title = '<emoji id="5264895611517300926">🏦</emoji> 𝗗𝗶𝗿𝗲𝗰𝘁 𝗨𝗣𝗜 𝗧𝗿𝗮𝗻𝘀𝗳𝗲𝗿 (𝗠𝗮𝗻𝘂𝗮𝗹 𝗨𝗣𝗜)'
        upi_desc = "• <b>प्रक्रिया:</b> किसी भी UPI ऐप से भुगतान करें → 12-अंकों का UTR दर्ज करें → ऑटो-वेरिफाई।\n• <b>माध्यम:</b> PhonePe, GPay, Paytm, BHIM आदि।\n• <b>सत्यापन:</b> स्वचालित सत्यापन (1-2 मिनट)।"
        
        cf_title = "💳 𝗣𝗮𝘆 𝘄𝗶𝘁𝗵 𝗖𝗮𝘀𝗵𝗳𝗿𝗲𝗲 (𝗜𝗻𝘀𝘁𝗮𝗻𝘁)"
        cf_desc = "• <b>लाभ:</b> तुरंत एक्सेस, 100% सुरक्षित पेमेंट गेटवे।\n• <b>माध्यम:</b> कार्ड्स (क्रेडिट/डेबिट), नेटबैंकिंग, UPI (GPay, PhonePe, Paytm), वॉलेट्स।\n• <b>सत्यापन:</b> तत्काल ऑटोमैटिक वेरिफिकेशन और डिलीवरी।"
        
        crypto_title = "₿ 𝗣𝗮𝘆 𝘄𝗶𝘁𝗵 𝗖𝗿𝘆𝗽𝘁𝗼 (𝗢𝘅𝗮𝗣𝗮𝘆)"
        crypto_desc = "• <b>लाभ:</b> तुरंत एक्सेस (कोई प्रतीक्षा नहीं), 24/7 उपलब्ध।\n• <b>माध्यम:</b> BTC, USDT, ETH, LTC और 300+ अन्य कॉइन्स।\n• <b>सत्यापन:</b> भुगतान के तुरंत बाद स्वचालित।"
        
        pay_upi_btn = "Pay Via UPI"
        pay_cf_btn = "Pay Via Cards , NetBanking"
        pay_crypto_btn = "Pay Via Crypto [ Oxapay ]"
        unavailable_upi = "यूपीआई भुगतान अभी बंद है।"
        back_btn = "❮ वापस"
    else:
        title = "⟦ 𝗦𝗘𝗖𝗨𝗥𝗘 𝗖𝗛𝗘𝗖𝗞𝗢𝗨𝗧 ⟧"
        item_lbl = "Item"
        price_lbl = "Total Price"
        
        upi_title = '<emoji id="5264895611517300926">🏦</emoji> 𝗗𝗶𝗿𝗲𝗰𝘁 𝗨𝗣𝗜 𝗧𝗿𝗮𝗻𝘀𝗳𝗲𝗿 (𝗠𝗮𝗻𝘂𝗮𝗹 𝗨𝗣𝗜)'
        upi_desc = "• <b>Process:</b> Pay directly using any UPI App → Enter 12-digit UTR → Auto Verify.\n• <b>Modes:</b> PhonePe, GPay, Paytm, BHIM, etc.\n• <b>Verification:</b> Automatic verification (Takes 1-2 mins)."
        
        cf_title = "💳 𝗣𝗮𝘆 𝘄𝗶𝘁𝗵 𝗖𝗮𝘀𝗵𝗳𝗿𝗲𝗲 (𝗜𝗻𝘀𝘁𝗮𝗻𝘁)"
        cf_desc = "• <b>Benefits:</b> Instant Access, 100% Secure Payment Gateway.\n• <b>Modes:</b> Cards (Credit/Debit), NetBanking, UPI (GPay, PhonePe, Paytm), Wallets.\n• <b>Verification:</b> Instant automated verification & immediate delivery."
        
        crypto_title = "₿ 𝗣𝗮𝘆 𝘄𝗶𝘁𝗵 𝗖𝗿𝘆𝗽𝘁𝗼 (𝗢𝘅𝗮𝗣𝗮𝘆)"
        crypto_desc = "• <b>Benefits:</b> Instant Access (No waiting), 24/7 available.\n• <b>Modes:</b> BTC, USDT, ETH, LTC, Doge & 300+ other coins.\n• <b>Verification:</b> Automatically verified upon payment."
        
        pay_upi_btn = "Pay Via UPI"
        pay_cf_btn = "Pay Via Cards , NetBanking"
        pay_crypto_btn = "Pay Via Crypto [ Oxapay ]"
        unavailable_upi = "UPI Currently Unavailable"
        back_btn = f"❮ {_sc('BACK')}"

    # UPI availability check
    upi_status = _upi_availability(bot_cfg)
    upi_ok = upi_status['available']

    if upi_ok:
        upi_block = f"<blockquote expandable=\"true\">{upi_title}\n{upi_desc}</blockquote>"
    else:
        if upi_status['reason'] == 'schedule':
            until_note = upi_status.get('until', '6:00 AM IST')
            if lang == 'hi':
                upi_block = (
                    f"<blockquote expandable=\"true\"><b>⏸ डायरेक्ट UPI अभी उपलब्ध नहीं है।</b>\n\n"
                    f"• रात्रि 9 बजे से सुबह 6 बजे के बीच सुरक्षा कारणों से डायरेक्ट UPI बंद रहता है।\n"
                    f"• UPI फिर से उपलब्ध होगा: <b>{until_note}</b>\n\n"
                    f"कृपया अन्य उपलब्ध भुगतान विकल्प का उपयोग करें।</blockquote>"
                )
            else:
                upi_block = (
                    f"<blockquote expandable=\"true\"><b>⏸ Direct UPI is currently unavailable.</b>\n\n"
                    f"• Direct UPI is paused between 9 PM – 6 AM IST for security.\n"
                    f"• UPI will be available again at: <b>{until_note}</b>\n\n"
                    f"Please use another available payment method.</blockquote>"
                )
        else:
            if lang == 'hi':
                upi_block = (
                    f"<blockquote expandable=\"true\"><b>⏸ डायरेक्ट UPI अभी अस्थायी रूप से बंद है।</b>\n\n"
                    f"• एडमिन ने फिलहाल डायरेक्ट UPI बंद किया है।\n"
                    f"• कृपया अन्य उपलब्ध भुगतान विकल्प का उपयोग करें।</blockquote>"
                )
            else:
                upi_block = (
                    f"<blockquote expandable=\"true\"><b>⏸ Direct UPI is temporarily unavailable.</b>\n\n"
                    f"• The admin has disabled Direct UPI for now.\n"
                    f"• Please use another available payment method.</blockquote>"
                )

    story_methods = story.get("payment_methods", ["upi", "razorpay"])
    show_upi = "upi" in story_methods
    oxapay_key = (getattr(Config, "OXAPAY_KEY", "") or "").strip()
    is_show_store_mode = bool(story.get("is_show") or (bot_cfg.get("bot_mode") == "show_store"))
    show_crypto = bool(oxapay_key) and not is_show_store_mode

    cf_block = f"<blockquote expandable=\"true\">{cf_title}\n{cf_desc}</blockquote>" if show_cashfree else ""
    crypto_block = f"<blockquote expandable=\"true\">{crypto_title}\n{crypto_desc}</blockquote>" if show_crypto else ""

    content_blocks = [upi_block]
    if show_cashfree and cf_block:
        content_blocks.append(cf_block)
    if show_crypto and crypto_block:
        content_blocks.append(crypto_block)

    txt = (
        f"<b>{title}</b>\n\n"
        f"<b>{item_lbl} :</b> <code>{name}</code>\n"
        f"<b>{price_lbl} :</b> {p_str}\n\n"
        + "\n".join(content_blocks)
    )

    kb = []
    if show_upi:
        if upi_ok:
            kb.append([_ikb(pay_upi_btn, callback_data=f"mb#pay2#upi#{str(story['_id'])}", icon_custom_emoji_id="5766975922620076409")])
        else:
            kb.append([InlineKeyboardButton(f"⏸ {unavailable_upi}", callback_data="mb#noop")])

    if show_cashfree:
        kb.append([_ikb(pay_cf_btn, callback_data=f"mb#pay2#cashfree#{str(story['_id'])}", icon_custom_emoji_id="6104751980641525812")])

    if show_crypto:
        kb.append([InlineKeyboardButton(pay_crypto_btn, callback_data=f"mb#pay2#crypto#{str(story['_id'])}")])

    kb.append([InlineKeyboardButton(back_btn, callback_data=f"mb#view_{str(story['_id'])}")])
    markup = InlineKeyboardMarkup(kb)

    IMG_URL = "https://files.catbox.moe/a6xw61.png"

    try:
        if is_msg:
            await msg_or_query.delete()
        else:
            await msg_or_query.message.delete()
    except Exception:
        pass

    await _send_story_photo(
        client=client,
        user_id=user_id,
        story=story,
        caption=txt,
        reply_markup=markup,
        fallback_photo=IMG_URL
    )


async def _process_start(client, message):
    user_id = message.from_user.id
    asyncio.create_task(_clear_utr_state(user_id))
    from pyrogram import enums

    # React to the /start command — fire-and-forget

    asyncio.create_task(react_bg(client, message.chat.id, message.id, pool=REACTIONS_WELCOME))

    

    ui = {
        "first_name": getattr(message.from_user, "first_name", ""),
        "last_name": getattr(message.from_user, "last_name", ""),
        "username": getattr(message.from_user, "username", ""),
        "bot_id": client.me.id
    }
    bot_ref = f"@{client.me.username}" if client.me.username else client.me.first_name
    is_new = await db.db.users.count_documents({"id": int(user_id)}) == 0

    from utils import log_arya_event
    args = message.command
    arg_payload = args[1].strip() if len(args) > 1 and args[1] else ""
    arg_p = f"#{arg_payload}" if arg_payload else ""

    if is_new:
        asyncio.create_task(log_arya_event(
            event_type="NEW USER JOIN",
            user_id=user_id,
            user_info=ui,
            details=f"New user started the store bot <b>{bot_ref}</b> for the first time." + (f"\nPayload: <code>{arg_payload}</code>" if arg_payload else ""),
            bot_id=client.me.id
        ))
    else:
        asyncio.create_task(log_arya_event(
            event_type="BOT STARTED",
            user_id=user_id,
            user_info=ui,
            details=f"User started the store bot <b>{bot_ref}</b>." + (f"\nPayload: <code>{arg_payload}</code>" if arg_payload else ""),
            bot_id=client.me.id
        ))

    user = await db.get_user(user_id, from_user=message.from_user, bot_id=client.me.id)

    # Track which delivery bots this user has started
    await db.db.users.update_one({"id": int(user_id)}, {"$addToSet": {"bot_ids": client.me.id}}, upsert=True)



    lang = user.get('lang', 'en')



    # ── Deep Link Handler: /start cf_<order_id> (Cashfree Payment Return) ──
    if len(args) > 1 and args[1].startswith("cf_"):
        order_id = args[1].strip()
        from cashfree_helper import check_cashfree_order_status
        from bson.objectid import ObjectId
        import time

        ord_doc = await db.db.orders.find_one({"order_id": order_id})
        if ord_doc:
            s_id = ord_doc.get("story_id") or (ord_doc.get("story_ids")[0] if ord_doc.get("story_ids") else None)
            story = await db.db.premium_stories.find_one({"_id": ObjectId(s_id)}) if s_id else None
            status_res = await check_cashfree_order_status(order_id)
            if status_res.get("is_paid") and story:
                await db.db.orders.update_one({"order_id": order_id}, {"$set": {"status": "paid", "paid_at": time.time()}})
                
                # Check if order was for a specific part
                part_info = None
                is_full = True
                if ord_doc.get("items"):
                    for itm in ord_doc["items"]:
                        if itm.get("part_id"):
                            part_info = itm
                            is_full = False
                            break
                        if itm.get("is_full", True):
                            is_full = True

                if is_full and not part_info:
                    await db.db.users.update_one({"id": int(user_id)}, {"$addToSet": {"purchases": ObjectId(s_id)}})
                    await db.db.premium_purchases.update_one(
                        {"user_id": int(user_id), "story_id": ObjectId(s_id)},
                        {"$set": {"user_id": int(user_id), "story_id": ObjectId(s_id), "source": "cashfree", "amount": story.get("price", 0), "order_id": order_id, "created_at": time.time()}},
                        upsert=True
                    )

                from utils import log_payment
                asyncio.create_task(log_payment(
                    amount=ord_doc.get("total", story.get("price", 0)),
                    user_id=user_id,
                    story_name=story.get('story_name_en', 'Story'),
                    payment_method="Cashfree",
                    order_id=order_id
                ))
                return await dispatch_delivery_choice(client, user_id, story, part_info=part_info)

    # ── Deep Link Handler (Bypass Force Join & Lang Prompt) ──

    if len(args) > 1 and args[1].startswith("demo_"):

        story_id = args[1].replace("demo_", "").strip()

        from bson.objectid import ObjectId

        from bson.errors import InvalidId

        

        story = None

        try:

            o_id = ObjectId(story_id)

            story = await db.db.premium_stories.find_one({"_id": o_id})

        except InvalidId:

            pass

            

        if not story:

            story = await db.db.premium_stories.find_one({"_id": story_id})

            

        if not story:
            story = await db.db.premium_stories.find_one({"story_id": story_id})

            

        if story:

            from utils import log_arya_event

            s_name = story.get(f"story_name_{lang}", story.get("story_name_en", "Unknown"))

            asyncio.create_task(log_arya_event(

                "VIEWED DEMO", 

                user_id, 

                {"first_name": getattr(message.from_user, "first_name", ""), "username": getattr(message.from_user, "username", "")}, 

                f"User requested demo files via deeplink for story: {s_name}"

            ))

            await message.reply_text("⏳ Processing your demo request...")

            from plugins.userbot.market_seller import _send_demo_files
            asyncio.create_task(_send_demo_files(client, user_id, story, lang))
            return

    # ── Deep Link Handler: /start mystories or /start purchased ──
    if len(args) > 1 and args[1].lower() in ("mystories", "my_stories", "purchased", "library"):
        return await _send_my_stories_menu(client, user_id, user, lang, reply_to_message=message)



    # ── Mini App Fast-Delivery: skip "Access Granted" screen, go straight to episode selection ──
    if len(args) > 1 and args[1].startswith("madeliver_"):
        raw_madeliver = args[1][10:].strip()  # strip "madeliver_" prefix
        parts = raw_madeliver.split("_", 1)
        story_id = parts[0]
        part_id = parts[1] if len(parts) > 1 else None

        from bson.objectid import ObjectId
        from bson.errors import InvalidId

        story = None
        try:
            o_id = ObjectId(story_id)
            story = await db.db.premium_stories.find_one({"_id": o_id})
        except InvalidId:
            pass

        if not story:
            story = await db.db.premium_stories.find_one({"_id": story_id})
        if not story:
            story = await db.db.premium_stories.find_one({"story_id": story_id})
        if not story:
            return await message.reply_text("❌ <b>Story not found!</b>\n\nThe link is invalid or this story has been removed.", parse_mode=enums.ParseMode.HTML)

        has_paid = await db.has_purchase(user_id, story_id, part_id=part_id)
        if not has_paid and story:
            has_paid = await db.has_purchase(user_id, str(story['_id']), part_id=part_id)
        if not has_paid and story and story.get('story_id'):
            has_paid = await db.has_purchase(user_id, str(story.get('story_id')), part_id=part_id)

        if not has_paid:
            # User doesn't actually own it — redirect to normal purchase flow
            return await message.reply_text(
                "❌ <b>Purchase not found.</b>\n\nPlease open the Mini App to purchase this story.",
                parse_mode=enums.ParseMode.HTML
            )

        # ── FAST PATH: directly trigger DM episode selection (no "Access Granted" screen) ──
        part_info = None
        if part_id:
            for p in (story.get("parts") or []):
                if str(p.get("id")) == str(part_id):
                    part_info = p
                    break

        if not part_info:
            user_order = await db.db.orders.find_one({
                "user_id": {"$in": [user_id, str(user_id)]},
                "story_ids": {"$in": [story_id, str(story.get('_id', ''))]},
                "status": {"$in": ["paid", "delivered"]}
            }, sort=[("created_at", -1)])
            if user_order and user_order.get("items"):
                for itm in user_order["items"]:
                    if (itm.get("story_id") == story_id or itm.get("story_id") == str(story.get('_id', ''))) and itm.get("part_id"):
                        for sp in (story.get("parts") or []):
                            if str(sp.get("id")) == str(itm.get("part_id")):
                                part_info = sp
                                break
                        break

        if part_info and part_info.get("start_id") and part_info.get("end_id"):
            start_id = int(part_info["start_id"])
            end_id   = int(part_info["end_id"])
            is_ong   = bool(part_info.get("is_ongoing") or str(part_info.get("badge") or "").lower() == "ongoing")
            story_end_id = int(story.get("end_id") or story.get("end_message_id") or 0)
            if is_ong and story_end_id > end_id:
                end_id = story_end_id
            p_id_str = str(part_info.get("id"))
        else:
            start_id = story.get('start_id')
            end_id   = story.get('end_id')
            p_id_str = None

        valid_file_ids = None
        if story.get("valid_file_ids"):
            if part_info and start_id and end_id:
                valid_file_ids = [mid for mid in story["valid_file_ids"] if start_id <= mid <= end_id]
                if valid_file_ids and end_id > max(valid_file_ids):
                    missing_tail = [m for m in range(max(valid_file_ids) + 1, end_id + 1)]
                    valid_file_ids = sorted(list(set(valid_file_ids + missing_tail)))
            else:
                valid_file_ids = story["valid_file_ids"]
                if valid_file_ids and end_id and int(end_id) > max(valid_file_ids):
                    missing_tail = [m for m in range(max(valid_file_ids) + 1, int(end_id) + 1)]
                    valid_file_ids = sorted(list(set(valid_file_ids + missing_tail)))
        elif start_id and end_id:
            valid_file_ids = list(range(int(start_id), int(end_id) + 1))

        total_files = len(valid_file_ids) if valid_file_ids else ((end_id - start_id) + 1 if (start_id and end_id and end_id >= start_id) else 1)
        s_id_str = str(story['_id'])

        if total_files > 40:
            if total_files > 300: chunk = 100
            elif total_files > 100: chunk = 50
            else: chunk = 30

            kb = []
            row = []
            for i in range(0, total_files, chunk):
                f_start = i + 1
                f_end   = min(i + chunk, total_files)
                lbl = f"{f_start} - {f_end}"
                is_last_chunk = (f_end == total_files)
                icon_id = "6147506120920405501" if is_last_chunk else "5341492148468465410"
                row.append(_kb_btn(lbl, icon_custom_emoji_id=icon_id))
                if len(row) == 2:
                    kb.append(row)
                    row = []
            if row:
                kb.append(row)

            full_btn   = "Full Delivery (All Files)" if lang != "hi" else "Full Delivery (सभी फ़ाइलें)"
            cancel_btn = "« " + ("Cancel" if lang != "hi" else "रद्द करें")
            kb.append([_kb_btn(full_btn, icon_custom_emoji_id="5805550320985578625")])
            kb.append([_kb_btn(cancel_btn)])

            set_state = {"dm_story_id_pending": s_id_str}
            if p_id_str:
                set_state["dm_part_id_pending"] = p_id_str
            else:
                set_state["dm_part_id_pending"] = None
            await db.db.users.update_one({"id": user_id}, {"$set": set_state})

            if lang == "hi":
                p_text = '<b><emoji id="6021620268697393273">ℹ️</emoji> फ़ाइलें चुनें:</b>\n\nआप कौन से भाग प्राप्त करना चाहते हैं? नीचे दिए गए मेन्यू बटन का उपयोग करें।'
            else:
                p_text = '<b><emoji id="6021620268697393273">ℹ️</emoji> Select Files:</b>\n\nWhich part would you like to receive? Please use the keyboard options below.'

            ok = await _send_reply_keyboard_bot_api(client, user_id, p_text, kb)
            if not ok:
                pyro_kb = [[b["text"] if isinstance(b, dict) else b for b in r] for r in kb]
                return await message.reply_text(p_text, reply_markup=ReplyKeyboardMarkup(pyro_kb, resize_keyboard=True), parse_mode=enums.ParseMode.HTML)
            return

        else:
            # Small story — deliver all files directly
            if lang == "hi":
                wait_txt = "<i>⏳ डिलीवरी शुरू हो रही है... आपकी फाइलें तैयार हो रही हैं।</i>"
            else:
                wait_txt = "<i>⏳ Initializing DM Delivery... Preparing your files.</i>"

            wait_msg = await message.reply_text(wait_txt, parse_mode=enums.ParseMode.HTML)
            asyncio.create_task(_do_dm_delivery(client, user_id, story, wait_msg, custom_msg_ids=valid_file_ids))
            return
            return

    if len(args) > 1 and (args[1].startswith("buy_") or args[1].startswith("story_")):

        if args[1].startswith("buy_"):
            story_id = args[1][4:].strip()
        else:
            story_id = args[1][6:].strip()
        logger.error(f"DEBUG_START_PRINT: extracted story_id: '{story_id}' from args: {args}")

        from bson.objectid import ObjectId

        from bson.errors import InvalidId

        

        story = None

        try:

            o_id = ObjectId(story_id)

            story = await db.db.premium_stories.find_one({"_id": o_id})

        except InvalidId:

            pass

            

        if not story:

            # Fallback 1: Story might be stored with a string _id

            story = await db.db.premium_stories.find_one({"_id": story_id})

            

        if not story:
            # Fallback 2: Story might be stored with story_id field
            story = await db.db.premium_stories.find_one({"story_id": story_id})
            
        if not story:
            logger.error(f"DEBUG: Story not found in DB for ID: {story_id}")
            return await message.reply_text("❌ <b>Story not found!</b>\n\nIt seems this story has been removed from the database, or the link is invalid.")
            
        if story:

            has_paid = await db.has_purchase(user_id, story_id)

            if has_paid:

                t_lang = T[lang]

                msg = t_lang["already_owned"]

                await message.reply_text(msg)

                from plugins.userbot.market_seller import dispatch_delivery_choice

                return await dispatch_delivery_choice(client, user_id, story)

            

            try:
                await message.delete()
            except Exception:
                pass
            # Direct story profile preview (same as selecting from Marketplace)
            return await _show_story_profile(client, user_id, story, lang)



    # ── Normal Start ──

    # Check if bot is configured in "miniapp" (Mini App Only / Store OFF) mode
    bt = await _get_cached_bot_doc(client.me.id)
    bot_cfg = (bt.get("config") or {}) if bt else {}
    bot_mode = bot_cfg.get("bot_mode", "full")

    if bot_mode == "miniapp":
        poster_file = _get_arya_poster_path()
        if poster_file and os.path.exists(poster_file):
            try:
                await message.reply_photo(
                    photo=poster_file,
                    caption=MINI_APP_WELCOME_TEXT,
                    reply_markup=MINI_APP_START_MARKUP,
                    parse_mode=enums.ParseMode.HTML
                )
            except Exception as e:
                logger.warning(f"Failed to reply_photo for miniapp start: {e}")
                await message.reply_text(
                    MINI_APP_WELCOME_TEXT,
                    reply_markup=MINI_APP_START_MARKUP,
                    parse_mode=enums.ParseMode.HTML,
                    disable_web_page_preview=True
                )
        else:
            await message.reply_text(
                MINI_APP_WELCOME_TEXT,
                reply_markup=MINI_APP_START_MARKUP,
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True
            )

        # Additional notice message for transferred bot services
        first_name = (getattr(message.from_user, 'first_name', '') or '').strip() or 'User'
        notice_text = (
            f"<b>Hey {first_name},</b>\n\n"
            f"<blockquote>हमने अपनी बॉट स्टोर सेवाओं को नए बॉट पर स्थानांतरित कर दिया है। यदि आप टेलीग्राम बॉट के माध्यम से कहानियां खरीदना चाहते हैं, तो आप हमारे नए बॉट का उपयोग कर सकते हैं। यह बॉट अब केवल मिनी ऐप के लिए समर्पित रहेगा, लेकिन आप /mystories द्वारा अपनी पहले से खरीदी गई कहानियों की डिलीवरी यहां प्राप्त कर सकते हैं। बाकी सभी सेवाएं नए बॉट पर शिफ्ट हो चुकी हैं।</blockquote>\n\n"
            f"<blockquote>We have transitioned our bot store services to our new bot. If you prefer purchasing stories directly via Telegram bot, please use our new bot. This current bot is now dedicated to the Mini App, though you can still access and receive your previously purchased stories here using /mystories. All other store operations have moved to our new bot.</blockquote>"
        )
        notice_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Open Storyfi", url="https://t.me/StoryfiBot")]
        ])
        return await client.send_message(
            user_id,
            notice_text,
            reply_markup=notice_markup,
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True
        )

    if 'lang' not in user:

        lang_prompt = (

            "<b>⟦ 𝗦𝗘𝗟𝗘𝗖𝗧 𝗟𝗔𝗡𝗚𝗨𝗔𝗚𝗘 ⟧</b>\n\n"

            "<blockquote expandable>"

            "<i>Choose your preferred language to continue.\n"

            "अपनी भाषा चुनें और आगे बढ़ें।</i>"

            "</blockquote>"

        )

        kb = [[InlineKeyboardButton("• English", callback_data=f"mb#lang#en{arg_p}"),

               InlineKeyboardButton("• हिंदी", callback_data=f"mb#lang#hi{arg_p}")]]

        return await message.reply_text(lang_prompt, reply_markup=InlineKeyboardMarkup(kb))



    # ── Force Join Logic (Unicode only, no emojis) ──

    INVITE_CHANNEL = "https://t.me/AryaPremiumTG"

    try:

        chat_member = await client.get_chat_member("@AryaPremiumTG", user_id)

        if chat_member.status in (enums.ChatMemberStatus.BANNED, enums.ChatMemberStatus.LEFT):

            raise Exception("Not joined")

    except Exception:

        if lang == 'hi':

            join_title = "𝗧𝗘𝗟𝗘𝗚𝗥𝗔𝗠 𝗖𝗛𝗔𝗡𝗡𝗘𝗟 𝗝𝗢𝗜𝗡 𝗞𝗔𝗥𝗘𝗡"

            join_txt = (

                "𝗕𝗼𝘁 𝗸𝗼 𝘂𝘀𝗲 𝗸𝗮𝗿𝗻𝗲 𝗸𝗲 𝗹𝗶𝘆𝗲 𝗮𝗮𝗽𝗸𝗼 𝗵𝘂𝗺𝗮𝗿𝗲 𝗰𝗵𝗮𝗻𝗻𝗲𝗹 𝗺𝗲𝗶𝗻 𝗷𝗼𝗶𝗻 𝗵𝗼𝗻𝗮 𝗵𝗼𝗴𝗮।\n\n"

                "<blockquote expandable>"

                "𝗝𝗼𝗶𝗻 𝗸𝗮𝗿𝗻𝗲 𝗸𝗲 𝗯𝗮𝗮𝗱 '𝗝𝗼𝗶𝗻𝗲𝗱' 𝗽𝗮𝗿 𝗰𝗹𝗶𝗰𝗸 𝗸𝗮𝗿𝗲𝗻। 𝗜𝘀𝘀𝗲 𝗮𝗮𝗽𝗸𝗼 𝘀𝗮𝗯𝗵𝗶 𝗮𝗱𝘃𝗮𝗻𝗰𝗲𝗱 𝗳𝗲𝗮𝘁𝘂𝗿𝗲𝘀 𝗮𝘂𝗿 𝘂𝗽𝗱𝗮𝘁𝗲𝘀 𝗺𝗶𝗹𝘁𝗲 𝗿𝗮𝗵𝗲𝗻𝗴𝗲।\n"

                "</blockquote>"

            )

            join_btn = "✓ 𝗝𝗢𝗜𝗡 𝗖𝗛𝗔𝗡𝗡𝗘𝗟"

            joined_btn = "✓ 𝗝𝗢𝗜𝗡 𝗞𝗔𝗥 𝗟𝗜𝗬𝗔"

        else:

            join_title = "𝗝𝗢𝗜𝗡 𝗢𝗨𝗥 𝗖𝗛𝗔𝗡𝗡𝗘𝗟"

            join_txt = (

                "𝗬𝗼𝘂 𝗺𝘂𝘀𝘁 𝗷𝗼𝗶𝗻 𝗼𝘂𝗿 𝗧𝗲𝗹𝗲𝗴𝗿𝗮𝗺 𝗰𝗵𝗮𝗻𝗻𝗲𝗹 𝘁𝗼 𝘂𝘀𝗲 𝘁𝗵𝗶𝘀 𝗯𝗼𝘁.\n\n"

                "<blockquote expandable>"

                "𝗔𝗳𝘁𝗲𝗿 𝗷𝗼𝗶𝗻𝗶𝗻𝗴, 𝗰𝗹𝗶𝗰𝗸 '𝗝𝗼𝗶𝗻𝗲𝗱' 𝘁𝗼 𝗰𝗼𝗻𝘁𝗶𝗻𝘂𝗲. 𝗬𝗼𝘂 𝘄𝗶𝗹𝗹 𝗴𝗲𝘁 𝗮𝗰𝗰𝗲𝘀𝘀 𝘁𝗼 𝗮𝗹𝗹 𝗽𝗿𝗲𝗺𝗶𝘂𝗺 𝘀𝘁𝗼𝗿𝗶𝗲𝘀 𝗮𝗻𝗱 𝗶𝗻𝘀𝘁𝗮𝗻𝘁 DELIVERY."

                "</blockquote>"

            )

            join_btn = "✓ 𝗝𝗢𝗜𝗡 𝗖𝗛𝗔𝗡𝗡𝗘𝗟"

            joined_btn = "✓ 𝗝𝗢𝗜𝗡𝗘𝗗"



        join_kb = [

            [InlineKeyboardButton(join_btn, url=INVITE_CHANNEL)],

            [InlineKeyboardButton(joined_btn, callback_data=f"mb#jchk{arg_p}")]

        ]

        return await message.reply_text(f"<b>{join_title}</b>\n\n{join_txt}", reply_markup=InlineKeyboardMarkup(join_kb))



    # Standard Main Menu

    wait_msg_txt = "WAIT A SECOND..." if lang == 'en' else "कृपया प्रतीक्षा करें..."

    wait_msg = await message.reply_text(f'<b>› › <emoji id="5348471079482441278">⏳</emoji> {wait_msg_txt}</b>', parse_mode=enums.ParseMode.HTML)

    await asyncio.sleep(0.4)

    await wait_msg.delete()



    await _send_main_menu(client, user_id, message.from_user, lang, reply_to_message_id=message.id)



async def _submit_feedback(client, message, user_id: int, user: dict, lang: str, content_type: str, text: str):

    try:

        from utils import log_arya_event

        ui = {"first_name": getattr(message.from_user, "first_name", ""), "last_name": getattr(message.from_user, "last_name", ""), "username": getattr(message.from_user, "username", "")}

        asyncio.create_task(log_arya_event("USER FEEDBACK", user_id, ui, f"User submitted feedback: {text}"))

    except Exception: pass



    """Shared helper: saves feedback to DB and notifies admins. Handles text/photo/video/animation/document."""

    from datetime import datetime, timezone as _tz

    caption_or_text = text or (getattr(message, 'caption', None) or "")

    fb_doc = {

        "user_id": user_id,

        "bot_id": client.me.id,

        "type": content_type,

        "text": caption_or_text,

        "status": "open",

        "created_at": datetime.now(_tz.utc),

        "user_name": f"{message.from_user.first_name or ''} {message.from_user.last_name or ''}".strip(),

        "username": getattr(message.from_user, 'username', '') or "",

    }

    result = await db.db.premium_feedback.insert_one(fb_doc)

    fb_id = str(result.inserted_id)



    # ── Beautiful Admin Notification ──

    uname_str = f"@{fb_doc['username']}" if fb_doc['username'] else "No Username"

    type_icon = {"photo": "🖼", "video": "🎬", "animation": "🎞", "document": "📎"}.get(content_type, "💬")

    admin_txt = (

        f"<b>📨 New Feedback Received</b>\n"

        f"━━━━━━━━━━━━━━━━━━━━\n"

        f"<b>👤 User:</b> {fb_doc['user_name']}\n"

        f"<b>🔗 Username:</b> {uname_str}\n"

        f"<b>🆔 User ID:</b> <code>{user_id}</code>\n"

        f"<b>{type_icon} Type:</b> {content_type.title()}\n"

        f"━━━━━━━━━━━━━━━━━━━━\n"

        f"<b>💬 Message:</b>\n"

        f"<blockquote>{caption_or_text[:800] if caption_or_text else '(media only)'}</blockquote>"

    )

    kb_admin = [[

        InlineKeyboardButton("✅ Resolve", callback_data=f"mk#fbresv_{fb_id}"),

        InlineKeyboardButton("💬 Reply", callback_data=f"mk#fbreply_{fb_id}_{user_id}")

    ]]



    try:

        from config import Config

        import os

        admins = list(getattr(Config, 'SUDO_USERS', None) or getattr(Config, 'OWNER_IDS', []))

        if hasattr(db, 'mgmt_client') and db.mgmt_client:

            # Download media via seller bot, then re-upload via mgmt_client

            # (file_ids are bot-specific — cross-bot direct use causes MEDIA_EMPTY)

            tmp_path = None

            if content_type in ("photo", "video", "animation", "document", "voice", "audio"):

                try:

                    file_ext = {"photo": ".jpg", "video": ".mp4", "animation": ".mp4", "document": "", "voice": ".ogg", "audio": ".mp3"}.get(content_type, "")

                    tmp_path = await client.download_media(message, file_name=f"downloads/fb_{fb_id}{file_ext}")

                except Exception as dl_err:

                    logger.warning(f"Feedback media download failed: {dl_err}")



            for admin_id in admins:

                try:

                    if tmp_path and content_type == "photo":

                        await db.mgmt_client.send_photo(admin_id, photo=tmp_path, caption=admin_txt, reply_markup=InlineKeyboardMarkup(kb_admin), parse_mode=enums.ParseMode.HTML)

                    elif tmp_path and content_type == "video":

                        await db.mgmt_client.send_video(admin_id, video=tmp_path, caption=admin_txt, reply_markup=InlineKeyboardMarkup(kb_admin), parse_mode=enums.ParseMode.HTML)

                    elif tmp_path and content_type == "animation":

                        await db.mgmt_client.send_animation(admin_id, animation=tmp_path, caption=admin_txt, reply_markup=InlineKeyboardMarkup(kb_admin), parse_mode=enums.ParseMode.HTML)

                    elif tmp_path and content_type == "document":

                        await db.mgmt_client.send_document(admin_id, document=tmp_path, caption=admin_txt, reply_markup=InlineKeyboardMarkup(kb_admin), parse_mode=enums.ParseMode.HTML)

                    elif tmp_path and content_type == "voice":

                        await db.mgmt_client.send_voice(admin_id, voice=tmp_path, caption=admin_txt, reply_markup=InlineKeyboardMarkup(kb_admin), parse_mode=enums.ParseMode.HTML)

                    elif tmp_path and content_type == "audio":

                        await db.mgmt_client.send_audio(admin_id, audio=tmp_path, caption=admin_txt, reply_markup=InlineKeyboardMarkup(kb_admin), parse_mode=enums.ParseMode.HTML)

                    else:

                        await db.mgmt_client.send_message(admin_id, admin_txt, reply_markup=InlineKeyboardMarkup(kb_admin), parse_mode=enums.ParseMode.HTML)

                except Exception:

                    pass

            # Cleanup temp file

            if tmp_path and os.path.exists(tmp_path):

                try: os.remove(tmp_path)

                except: pass

    except Exception:

        pass



    await db.update_user(user_id, {"state": None})

    if lang == 'hi':

        confirm = "✅ <b>आपका फीडबैक सफलतापूर्वक भेजा गया!</b>\n<i>हमारी टीम जल्द ही आपसे संपर्क करेगी।</i>"

    else:

        confirm = "✅ <b>Feedback submitted!</b>\n<i>Our team will review it and get back to you shortly.</i>"

    await message.reply_text(

        confirm,

        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"« {_sc('MAIN MENU')}", callback_data="mb#main_back")]]),

        parse_mode=enums.ParseMode.HTML

    )





async def _process_media(client, message):
    """Handles photo/video/animation/document messages. Routes media feedback to _submit_feedback."""
    if not message or not message.from_user:
        return
    if getattr(message, "outgoing", False) or getattr(message.from_user, "is_bot", False) or message.from_user.id == client.me.id:
        return

    user_id = message.from_user.id
    user = await db.get_user(user_id, from_user=message.from_user)
    lang = user.get('lang', 'en')

    # If user is in feedback_pending state, route to feedback system
    if user.get("state") == "feedback_pending":
        caption = getattr(message, 'caption', None) or ""
        cap_strip = caption.strip().lower()
        if cap_strip.startswith("/") or cap_strip in ["cancel", "रद्द", "रद्द करें", "back", "« back", "वापस"]:
            await db.db.users.update_one({"id": user_id}, {"$unset": {"state": 1}})
            return

        if message.photo:
            content_type = "photo"
        elif message.video:
            content_type = "video"
        elif message.animation:
            content_type = "animation"
        elif message.voice:
            content_type = "voice"
        elif message.audio:
            content_type = "audio"
        elif message.document:
            content_type = "document"
        else:
            content_type = "photo"
        await _submit_feedback(client, message, user_id, user, lang, content_type=content_type, text=caption)
        return

    # Only route photos to screenshot processor IF user actually has a pending checkout waiting for screenshot
    if message.photo:
        checkout = await db.db.premium_checkout.find_one(
            {"user_id": user_id, "bot_id": client.me.id, "status": "waiting_screenshot"}
        )
        if checkout:
            await _process_screenshot(client, message, checkout=checkout)
            return





async def _process_my_stories(client, message):

    user_id = message.from_user.id

    user = await db.get_user(user_id, from_user=message.from_user)

    lang = user.get('lang', 'en')

    from bson.objectid import ObjectId



    # ── 1. Bot purchases (via user.purchases[] field) ─────────────

    bot_purchase_ids = [str(p) for p in user.get('purchases', [])]



    # ── 2. Mini App Razorpay purchases (via orders collection) ────

    miniapp_purchase_ids = []

    try:

        async for order in db.db.orders.find(

            {"user_id": int(user_id), "status": "paid"},

            {"story_ids": 1}

        ):

            miniapp_purchase_ids.extend([str(s) for s in order.get("story_ids", [])])

    except Exception:

        pass



    # ── 3. Merge unique IDs, bot purchases first ──────────────────

    seen = set()

    merged_ids = []

    source_map = {}  # story_id → "bot" | "app" | "both"

    for pid in bot_purchase_ids:

        if pid not in seen:

            merged_ids.append(pid)

            seen.add(pid)

            source_map[pid] = "bot"

    for pid in miniapp_purchase_ids:

        if pid not in seen:

            merged_ids.append(pid)

            seen.add(pid)

            source_map[pid] = "app"

        elif pid in source_map:

            source_map[pid] = "both"



    total = len(merged_ids)

    PAGE_SIZE = 5

    page = 0

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    page_purchases = merged_ids[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]



    # ── 4. Build keyboard ─────────────────────────────────────────

    kb = []

    for pid in page_purchases:

        try:

            st = await db.db.premium_stories.find_one({"_id": ObjectId(pid)})

            if st:

                en_name = st.get('story_name_en', 'Story')

                hi_name = st.get('story_name_hi', en_name)

                s_name = hi_name if lang == 'hi' else en_name

                src = source_map.get(pid, "bot")

                badge = "🤖" if src == "bot" else ("📱" if src == "app" else "🔗")

                kb.append([InlineKeyboardButton(

                    f"{badge} {s_name}",

                    callback_data=f"mb#purchased_view_{pid}"

                )])

        except Exception:

            pass



    if lang == 'hi':

        title     = "⟦ मेरी स्टोरीज ⟧"

        total_txt = "कुल स्टोरी ⟶"

        desc      = "आपके अकाउंट में मौजूद सभी स्टोरीज नीचे दी गई हैं।\n🤖 = Bot से खरीदी | 📱 = Mini App से खरीदी"

        next_btn  = "आगे ❭"

        back_btn  = "« वापस मेनू"

        empty_txt = "कोई खरीद नहीं मिली।"

        market_btn = "स्टोर खोलें"

    else:

        title     = "⟦ 𝗠𝗬 𝗦𝗧𝗢𝗥𝗜𝗘𝗦 ⟧"

        total_txt = "ᴛᴏᴛᴀʟ ⟶"

        desc      = "All your purchased stories are listed below.\n🤖 = Bought via Bot  |  📱 = Bought via Mini App"

        next_btn  = "𝗡𝗲𝘅𝘁 ❭"

        back_btn  = "Back to Menu"

        empty_txt = "ɴᴏ ᴘᴜʀᴄʜᴀꜱᴇꜱ ꜰᴏᴜɴᴅ."

        market_btn = "OPEN MARKETPLACE"



    if total_pages > 1:

        nav = [

            InlineKeyboardButton(f"ᴘᴀɢᴇ 1/{total_pages}", callback_data="mb#noop"),

            InlineKeyboardButton(next_btn, callback_data="mb#my_buys_page_1"),

        ]

        kb.append(nav)



    kb.append([InlineKeyboardButton(back_btn, callback_data="mb#main_back")])



    if total > 0:

        txt_b = (

            f"<b>{title}</b>\n\n"

            f"<b>{total_txt}</b> {total}\n\n"

            f"{desc}"

        )

    else:

        txt_b = (

            f"<b>{title}</b>\n\n"

            f"<b>{total_txt}</b> 0\n\n"

            f"{empty_txt}"

        )

        kb.insert(0, [InlineKeyboardButton(market_btn, callback_data="mb#main_marketplace")])



    await client.send_message(user_id, txt_b, reply_markup=InlineKeyboardMarkup(kb))





async def _process_text(client, message):

    user_id = message.from_user.id

    # React to any user message in the bot — fire-and-forget

    asyncio.create_task(react_bg(client, message.chat.id, message.id, pool=REACTIONS_GENERAL))

    user = await db.get_user(user_id, from_user=message.from_user, bot_id=client.me.id)

    lang = user.get('lang', 'en')

    txt = message.text.strip()



    try:

        from utils import log_arya_event

        ui = {"first_name": getattr(message.from_user, "first_name", ""), "last_name": getattr(message.from_user, "last_name", ""), "username": getattr(message.from_user, "username", ""), "bot_id": client.me.id}

        if " [ ₹ " in txt:

            sName = txt.split(" [ ₹ ")[0].split(". ", 1)[-1].strip()

            asyncio.create_task(log_arya_event("STORY SELECTED", user_id, ui, f"User selected story: {sName}"))

        elif txt in ["🔎 SEARCH", "🔎 खोजें"]:

            asyncio.create_task(log_arya_event("USER INTERACTION", user_id, ui, "Clicked Search in Marketplace"))

        elif user.get("state") == "searching" and not txt.startswith("✖️") and not txt.startswith("❮"):

            asyncio.create_task(log_arya_event("USER INTERACTION", user_id, ui, f"Searched for: {txt}"))

        elif txt in ["Pocket FM", "Kuku FM", "Other"] or any(txt == p for p in await db.db.premium_stories.distinct('platform', {"bot_id": client.me.id})):

            asyncio.create_task(log_arya_event("USER INTERACTION", user_id, ui, f"Selected Platform: {txt}"))

    except Exception: pass





    txt_lower = txt.lower()

    # 1. Any Slash Command triggers state reset and command execution
    if txt.startswith("/"):
        if user.get("state") or user.get("pending_utr_story_id") or user.get("dm_story_id_pending"):
            await db.db.users.update_one(
                {"id": user_id},
                {"$unset": {"state": 1, "pending_utr_story_id": 1, "dm_story_id_pending": 1}}
            )

        cmd_word = txt_lower.split()[0]
        if cmd_word in ["/cancel", "/stop", "/abort"]:
            await message.reply_text("<i>❌ Process Cancelled!</i>", reply_markup=ReplyKeyboardRemove(), parse_mode=enums.ParseMode.HTML)
            return await _send_main_menu(client, user_id, message.from_user, lang)

        if cmd_word == "/start":
            return await _process_start(client, message)

        if cmd_word in ["/mystories", "/stories", "/library"]:
            return await _send_my_stories_menu(client, user_id, user, lang, reply_to_message=message)

        if cmd_word in ["/marketplace", "/arya", "/help", "/settings", "/profile"]:
            m = await message.reply_text('<i><emoji id="5348471079482441278">⏳</emoji> Loading...</i>', parse_mode=enums.ParseMode.HTML)
            class MockQuery:
                def __init__(self, msg, u, d):
                    self.message = msg
                    self.from_user = u
                    self.data = d
                async def answer(self, text="", show_alert=False):
                    pass
            mapping = {
                "/marketplace": "mb#main_marketplace",
                "/arya": "mb#about_arya_0",
                "/help": "mb#main_help",
                "/settings": "mb#main_settings",
                "/profile": "mb#main_profile"
            }
            return await _process_callback(client, MockQuery(m, message.from_user, mapping[cmd_word]))

    # 2. Episode Chunk Range Selection (Reply Keyboard)
    pending_s_id = user.get("dm_story_id_pending")
    pending_p_id = user.get("dm_part_id_pending")
    import re
    is_chunk_btn = bool(
        re.search(r"^\s*\d+\s*-\s*\d+\s*$", txt)
        or "full delivery" in txt_lower
        or "सभी फ़ाइलें" in txt_lower
        or (("cancel" in txt_lower or "रद्द" in txt_lower) and "«" in txt)
    )
    if pending_s_id and is_chunk_btn:
        try:
            await message.delete()
        except Exception:
            pass
        await db.db.users.update_one({"id": user_id}, {"$unset": {"dm_story_id_pending": 1, "dm_part_id_pending": 1}})
        
        if "cancel" in txt_lower or "रद्द" in txt_lower or txt.startswith("«"):
            return await message.reply_text("<i>❌ Delivery Selection Cancelled.</i>", reply_markup=ReplyKeyboardRemove(), parse_mode=enums.ParseMode.HTML)
        
        from bson.objectid import ObjectId
        from bson.errors import InvalidId
        story = None
        try:
            story = await db.db.premium_stories.find_one({"_id": ObjectId(pending_s_id)})
        except Exception:
            pass
        if not story:
            story = await db.db.premium_stories.find_one({"_id": pending_s_id})
        if not story:
            story = await db.db.premium_stories.find_one({"story_id": pending_s_id})
        
        if story:
            part_info = None
            if pending_p_id:
                for p in (story.get("parts") or []):
                    if str(p.get("id")) == str(pending_p_id):
                        part_info = p
                        break

            if not part_info:
                user_order = await db.db.orders.find_one({
                    "user_id": {"$in": [user_id, str(user_id)]},
                    "story_ids": {"$in": [pending_s_id, str(story.get('_id', ''))]},
                    "status": {"$in": ["paid", "delivered"]}
                }, sort=[("created_at", -1)])
                if user_order and user_order.get("items"):
                    for itm in user_order["items"]:
                        if (itm.get("story_id") == pending_s_id or itm.get("story_id") == str(story.get('_id', ''))) and itm.get("part_id"):
                            for sp in (story.get("parts") or []):
                                if str(sp.get("id")) == str(itm.get("part_id")):
                                    part_info = sp
                                    break
                            break

            if part_info and part_info.get("start_id") and part_info.get("end_id"):
                start_id = int(part_info["start_id"])
                end_id = int(part_info["end_id"])
                is_ong = bool(part_info.get("is_ongoing") or str(part_info.get("badge") or "").lower() == "ongoing")
                story_end_id = int(story.get("end_id") or story.get("end_message_id") or 0)
                if is_ong and story_end_id > end_id:
                    end_id = story_end_id
            else:
                start_id = story.get("start_id")
                end_id = story.get("end_id")

            valid_list = None
            if story.get("valid_file_ids"):
                if part_info and start_id and end_id:
                    valid_list = [mid for mid in story["valid_file_ids"] if start_id <= mid <= end_id]
                    if valid_list and end_id > max(valid_list):
                        missing_tail = [m for m in range(max(valid_list) + 1, end_id + 1)]
                        valid_list = sorted(list(set(valid_list + missing_tail)))
                else:
                    valid_list = story["valid_file_ids"]
                    if valid_list and end_id and int(end_id) > max(valid_list):
                        missing_tail = [m for m in range(max(valid_list) + 1, int(end_id) + 1)]
                        valid_list = sorted(list(set(valid_list + missing_tail)))
            elif start_id and end_id:
                valid_list = list(range(int(start_id), int(end_id) + 1))

            import re
            match = re.search(r"(\d+)\s*-\s*(\d+)", txt)
            custom_msg_ids = None
            if match:
                fs, fe = int(match.group(1)), int(match.group(2))
                if valid_list:
                    custom_msg_ids = valid_list[fs - 1 : fe]
                else:
                    c_start = start_id + fs - 1
                    c_end = min(start_id + fe - 1, end_id)
            else:
                # Full Delivery of this part/story
                fs, fe = 1, len(valid_list) if valid_list else ((end_id - start_id) + 1 if (start_id and end_id) else "All")
                if valid_list:
                    custom_msg_ids = valid_list
                else:
                    c_start, c_end = start_id, end_id

            m = await message.reply_text(f"<i>⏳ Initializing DM Delivery (Files {fs}-{fe})... Preparing your files.</i>", reply_markup=ReplyKeyboardRemove(), parse_mode=enums.ParseMode.HTML)
            if custom_msg_ids is not None:
                return asyncio.create_task(_do_dm_delivery(client, user_id, story, m, custom_msg_ids=custom_msg_ids))
            else:
                return asyncio.create_task(_do_dm_delivery(client, user_id, story, m, c_start, c_end))
        else:
            return await message.reply_text("❌ <i>Story not found.</i>", reply_markup=ReplyKeyboardRemove(), parse_mode=enums.ParseMode.HTML)

    # 3. Feedback submission state handler
    if user.get("state") == "feedback_pending":
        is_cancel = txt.strip().lower() in [
            "/cancel", "cancel", "रद्द", "रद्द करें", "back", "« back", 
            "back to menu", "वापस", "वापस मेनू", "« वापस मेनू", "« cancel", "«", "❬", "❮"
        ]
        if is_cancel:
            await db.update_user(user_id, {"state": None})
            await message.reply_text("<i>❌ Feedback cancelled.</i>", reply_markup=ReplyKeyboardRemove(), parse_mode=enums.ParseMode.HTML)
            return await _send_main_menu(client, user_id, message.from_user, lang)

        # Check if user sent a menu button / platform / story name instead of typing feedback
        all_plats_check = await db.db.premium_stories.distinct('platform', {"bot_id": client.me.id})
        is_menu_btn = (
            " [ ₹ " in txt or 
            txt in ["SEARCH", "खोजें", "VIEW ALL", "सभी देखें", "NEXT ❭", "❬ PREV", "अगला ❭", "❬ पिछला", "𝗕𝗮𝗰𝗸 𝘁𝗼 𝗠𝗲𝗻𝘂", "वापस मेनू", "CAN'T FIND? REQUEST NOW!", "कहानी नहीं मिल रही? अनुरोध करें!"] or
            txt in (T[lang]["cant_find_btn"], "CAN'T FIND? REQUEST NOW!", "कहानी नहीं मिल रही? अनुरोध करें!", "🔍 CAN'T FIND? REQUEST NOW!", "🔍 कहानी नहीं मिल रही? अनुरोध करें!") or
            any(txt == p for p in all_plats_check)
        )
        if is_menu_btn:
            await db.update_user(user_id, {"state": None})
        else:
            await _submit_feedback(client, message, user_id, user, lang, content_type="text", text=txt)
            return

    # 4. Check if bot is configured in "miniapp" (Mini App Only / Store OFF) mode
    bt = await _get_cached_bot_doc(client.me.id)
    bot_cfg = (bt.get("config") or {}) if bt else {}
    bot_mode = bot_cfg.get("bot_mode", "full")

    # If in miniapp mode and not in any active state (like UTR entry), ignore random text (only /start triggers welcome)
    pending_s_id_utr = user.get("pending_utr_story_id")
    is_any_menu_btn = (
        " [ ₹ " in txt
        or "view all" in txt.lower()
        or "view_all" in txt.lower()
        or "सभी देखें" in txt
        or "next" in txt.lower()
        or "prev" in txt.lower()
        or "अगला" in txt
        or "पिछला" in txt
        or "search" in txt.lower()
        or "खोजें" in txt
        or "back" in txt.lower()
        or "वापस" in txt
        or "menu" in txt.lower()
        or "cant_find" in txt.lower()
        or "request" in txt.lower()
        or "अनुरोध" in txt
    )
    if not pending_s_id_utr and not is_any_menu_btn and bot_mode == "miniapp":
        return

    # -- UTR Payment handler: user sends their 12-digit UTR in chat --
    # Flow: User sees UPI page → sends 12-digit UTR → bot INSTANTLY verifies via IMAP
    # No confirmation step, no button click needed.
    # pending_utr_story_id is set in DB when user opens the Direct UPI page.

    pending_s_id_utr = user.get("pending_utr_story_id")
    if pending_s_id_utr:
        # 5-minute timeout check (auto-expire UTR state if > 300 seconds)
        opened_at = user.get("pending_utr_opened_at")
        if opened_at:
            try:
                from datetime import datetime as _dt
                if isinstance(opened_at, _dt):
                    elapsed_sec = (_dt.utcnow() - opened_at).total_seconds()
                else:
                    elapsed_sec = 9999
                if elapsed_sec > 300:
                    await _clear_utr_state(user_id)
                    return
            except Exception:
                pass

        raw_input = txt.strip()

        if raw_input.startswith("/") or raw_input.lower() in ("cancel", "back", "menu", "exit", "stop"):
            await _clear_utr_state(user_id)
            return

        # Extract ONLY ASCII digits — handles copy-paste with \xa0, thin-spaces, etc.
        utr_candidate = ''.join(c for c in raw_input if c in '0123456789')

        # Strictly require AT LEAST 12 digits — ignore short numbers or non-numeric text completely!
        if len(utr_candidate) < 12:
            # Silent return — do NOT throw any "Invalid UTR" error!
            return

        # Take 12 digits candidate
        utr_candidate = utr_candidate[:12]

        if True:
            logger.info(f"[UTR] User {user_id} sent UTR: {utr_candidate} for story {pending_s_id_utr}")

            # Clear state immediately to prevent double-processing
            await db.db.users.update_one(
                {"id": user_id},
                {"$unset": {"pending_utr_story_id": 1, "last_utr_entered": 1, "state": 1}}
            )

            from bson.objectid import ObjectId

            # Check duplicate UTR
            existing_utr = await db.db.verified_utrs.find_one({"utr": utr_candidate})
            if existing_utr:
                logger.warning(f"[UTR] Duplicate UTR: {utr_candidate} by user {user_id}")
                if lang == 'hi':
                    err_txt = (
                        "❌ <b>UTR पहले से उपयोग हो चुका है!</b>\n"
                        f"<code>{utr_candidate}</code> — यह UTR पहले ही किसी अन्य खरीद के लिए उपयोग हो चुका है।\n\n"
                        "<i>यदि यह आपका सही UTR है तो सहायता से संपर्क करें।</i>"
                    )
                else:
                    err_txt = (
                        "❌ <b>UTR Already Claimed!</b>\n"
                        f"<code>{utr_candidate}</code> — This UTR has already been used for another purchase.\n\n"
                        "<i>If you believe this is a mistake, please contact support.</i>"
                    )
                return await message.reply_text(
                    err_txt,
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [_ikb(
                            "दूसरा UTR भेजें" if lang == 'hi' else "Send a Different UTR",
                            callback_data=f"mb#pay2#upi#{pending_s_id_utr}",
                            icon_custom_emoji_id="5807492110059838726"
                        )]
                    ])
                )
            # Check already purchased
            if await db.has_purchase(user_id, pending_s_id_utr):
                _own_msg = "आप पहले से इस कहानी के मालिक हैं!" if lang == 'hi' else "You already own this story!"
                return await message.reply_text(f"✅ <b>{_own_msg}</b>", parse_mode=enums.ParseMode.HTML)

            story = await db.db.premium_stories.find_one({"_id": ObjectId(pending_s_id_utr)})
            if not story:
                logger.error(f"[UTR] Story not found: {pending_s_id_utr}")
                return await message.reply_text("❌ Story not found. Contact support.", parse_mode=enums.ParseMode.HTML)

            # Load Gmail credentials
            cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
            gmail_user = cfg.get("gmail_user", "").strip()
            gmail_password = cfg.get("gmail_app_password", "").strip()
            import os as _os
            if not gmail_user:
                gmail_user = _os.environ.get("GMAIL_USER", "").strip() or _os.environ.get("gmail_user", "").strip()
            if not gmail_password:
                gmail_password = _os.environ.get("GMAIL_APP_PASSWORD", "").strip() or _os.environ.get("gmail_app_password", "").strip()

            gmail_user = gmail_user.replace("\xa0", "").replace(" ", "").strip()
            gmail_password = gmail_password.replace("\xa0", "").replace(" ", "").strip()

            logger.info(f"[UTR] Gmail credentials: user={'OK' if gmail_user else 'MISSING'}, pass={'OK' if gmail_password else 'MISSING'}")

            gmail_user = gmail_user.replace("\xa0", "").replace(" ", "").strip()
            gmail_password = gmail_password.replace("\xa0", "").replace(" ", "").strip()

            if not gmail_user or not gmail_password:
                _no_cfg = "ऑटो-वेरिफिकेशन कॉन्फिगर नहीं है। Admin से संपर्क करें।" if lang == 'hi' else "Auto-verification not configured. Contact admin."
                return await message.reply_text(
                    f"❌ <b>{_no_cfg}</b>\n\n<i>Your UTR: <code>{utr_candidate}</code> — save for manual verification.</i>",
                    parse_mode=enums.ParseMode.HTML
                )

            # Show verifying status message
            if lang == 'hi':
                ver_msg = await message.reply_text(
                    "⏳ <b>पेमेंट वेरिफाई हो रहा है... कृपया इंतजार करें।</b>\n<i>Bank alert check हो रहा है...</i>",
                    parse_mode=enums.ParseMode.HTML
                )
            else:
                ver_msg = await message.reply_text(
                    "⏳ <b>Verifying your payment... Please wait.</b>\n<i>Checking bank transaction alert via IMAP.</i>",
                    parse_mode=enums.ParseMode.HTML
                )

            expected_total = float(story["price"])

            def _check_imap_utr_sync():
                import imaplib, email as _email_lib
                result = {"verified": False, "amount_mismatch": False, "mismatched_amount": None, "error": None}
                try:
                    imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
                    imap.login(gmail_user, gmail_password)
                    imap.select("INBOX")
                    # Use bytes to bypass imaplib ASCII encoding limitation (\xa0 fix)
                    search_bytes = b'TEXT "' + utr_candidate.encode('ascii') + b'"'
                    status, messages = imap.search(None, search_bytes)
                    logger.info(f"[UTR][IMAP] Search {utr_candidate}: status={status}, hits={bool(messages[0] if messages else None)}")
                    if status == "OK" and messages and messages[0]:
                        for mail_id in reversed(messages[0].split()):
                            res_status, msg_data = imap.fetch(mail_id, "(RFC822)")
                            if res_status != "OK":
                                continue
                            for part in msg_data:
                                if isinstance(part, tuple):
                                    email_msg = _email_lib.message_from_bytes(part[1])
                                    from_hdr = email_msg.get("From", "")
                                    if "noreply@slice.bank.in" not in from_hdr.lower():
                                        logger.info(f"[UTR][IMAP] Skip email from: {from_hdr}")
                                        continue
                                    body = get_email_body(email_msg)
                                    if utr_candidate in body:
                                        if verify_amount_in_email(body, expected_total):
                                            result["verified"] = True
                                            logger.info(f"[UTR][IMAP] VERIFIED UTR {utr_candidate}")
                                        else:
                                            result["amount_mismatch"] = True
                                            parsed = extract_amount_from_email(body)
                                            result["mismatched_amount"] = parsed
                                            logger.warning(f"[UTR][IMAP] Amount mismatch: expected={expected_total}, found={parsed}")
                                        break
                            if result["verified"] or result["amount_mismatch"]:
                                break
                    else:
                        logger.warning(f"[UTR][IMAP] No emails found for UTR: {utr_candidate}")
                    imap.close()
                    imap.logout()
                except Exception as ex:
                    result["error"] = str(ex)
                    logger.error(f"[UTR][IMAP] Exception for UTR {utr_candidate}: {ex}")
                return result

            try:
                imap_result = await asyncio.to_thread(_check_imap_utr_sync)
            except Exception as ex:
                logger.error(f"[UTR] asyncio.to_thread failed: {ex}")
                await ver_msg.delete()
                return await message.reply_text(
                    f"❌ <b>Verification error!</b>\n<i>{ex}</i>",
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[_ikb("Retry", callback_data=f"mb#pay2#upi#{pending_s_id_utr}", icon_custom_emoji_id="5807492110059838726")]])
                )

            await ver_msg.delete()

            if imap_result.get("error"):
                err = imap_result["error"]
                logger.error(f"[UTR] IMAP returned error for {utr_candidate}: {err}")
                _retry_lbl = "पुनः प्रयास करें" if lang == 'hi' else "Retry"
                return await message.reply_text(
                    f"❌ <b>Verification failed!</b>\n<i>{err}</i>",
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[_ikb(_retry_lbl, callback_data=f"mb#pay2#upi#{pending_s_id_utr}", icon_custom_emoji_id="5807492110059838726")]])
                )

            if not imap_result["verified"]:
                if imap_result["amount_mismatch"]:
                    mismatched = imap_result["mismatched_amount"]
                    if lang == 'hi':
                        err_text = (
                            "❌ <b>राशि सही नहीं है!</b>\n"
                            f"अपेक्षित: ₹{expected_total:.0f}\n"
                            f"भेजी गई: ₹{mismatched:.0f}\n\n"
                            "सही राशि भेजें और पुनः प्रयास करें।"
                        )
                    else:
                        err_text = (
                            "❌ <b>Amount Mismatch!</b>\n"
                            f"Expected: ₹{expected_total:.0f}\n"
                            f"Detected: ₹{mismatched:.0f}\n\n"
                            "Please pay the exact amount and try again."
                        )
                else:
                    if lang == 'hi':
                        err_text = (
                            "❌ <b>पेमेंट नहीं मिला!</b>\n"
                            f"UTR <code>{utr_candidate}</code> और ₹{expected_total:.0f} का bank alert नहीं मिला।\n\n"
                            "<i>अगर अभी भुगतान किया है तो 15–30 सेकंड इंतजार करें और UTR दोबारा भेजें।</i>"
                        )
                    else:
                        err_text = (
                            "❌ <b>Payment Not Found!</b>\n"
                            f"No bank alert for UTR <code>{utr_candidate}</code> (₹{expected_total:.0f})\n\n"
                            "<i>If you just paid, wait 15–30 seconds for the bank email, then send your UTR again.</i>"
                        )
                _retry_btn = "पुनः प्रयास करें" if lang == 'hi' else "Retry"
                _back_btn = "« वापस" if lang == 'hi' else "« Back"
                return await message.reply_text(
                    err_text,
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [_ikb(_retry_btn, callback_data=f"mb#pay2#upi#{pending_s_id_utr}", icon_custom_emoji_id="5807492110059838726")],
                        [InlineKeyboardButton(_back_btn, callback_data=f"mb#pay_back#{pending_s_id_utr}")]
                    ])
                )

            try:
                from datetime import datetime as _dt
                # ✅ Payment VERIFIED — record purchase and grant access
                logger.info(f"[UTR] PAYMENT VERIFIED: user={user_id}, story={pending_s_id_utr}, utr={utr_candidate}")

                await db.db.verified_utrs.insert_one({
                    "utr": utr_candidate, "amount": expected_total,
                    "user_id": user_id, "verified_at": _dt.utcnow()
                })
                logger.info("[UTR] DB: verified_utrs record inserted.")

                await db.db.premium_checkout.update_one(
                    {"user_id": user_id, "bot_id": client.me.id, "story_id": ObjectId(pending_s_id_utr)},
                    {"$set": {"status": "approved", "updated_at": _dt.utcnow()}}
                )
                logger.info("[UTR] DB: premium_checkout updated to approved.")

                import random, string as _string
                order_id = await _make_arya_bot_order_id(user_id, str(pending_s_id_utr))
                await db.db.premium_purchases.insert_one({
                    "user_id": user_id, "story_id": ObjectId(pending_s_id_utr),
                    "bot_id": client.me.id, "purchased_at": _dt.utcnow(),
                    "source": "upi", "amount": expected_total,
                    "reference": utr_candidate, "order_id": order_id
                })
                logger.info(f"[UTR] DB: premium_purchases inserted. order_id={order_id}")

                await db.add_purchase(user_id, pending_s_id_utr)
                logger.info("[UTR] DB: Purchase access added to user profile.")

                from utils import log_payment, log_arya_event
                s_name = story.get("story_name_en", "Unknown")
                asyncio.create_task(log_arya_event(
                    event_type="PAYMENT PROCESSED", user_id=user_id,
                    user_info={"first_name": getattr(message.from_user, "first_name", ""), "last_name": getattr(message.from_user, "last_name", ""), "username": getattr(message.from_user, "username", ""), "bot_id": client.me.id},
                    details=f"Story: {s_name}\nGateway: Direct UPI (IMAP Auto-Verify)\nUTR: <code>{utr_candidate}</code>\nAmount: ₹{expected_total:.0f}\nOrder: {order_id}",
                    bot_id=client.me.id
                ))
                asyncio.create_task(log_payment(
                    user_id=user_id, user_first_name=getattr(message.from_user, "first_name", "User"),
                    username=getattr(message.from_user, "username", ""), s_name=s_name,
                    amount=expected_total, method="upi", receipt_id=utr_candidate,
                    order_id=order_id, user_last_name=getattr(message.from_user, "last_name", ""),
                    bot_id=client.me.id
                ))
                logger.info("[UTR] Spawning log payment tasks.")

                if lang == 'hi':
                    await message.reply_text(
                        "✅ <b>पेमेंट सफलतापूर्वक वेरिफाई हो गया!</b>\n<i>कहानी डिलीवरी तैयार हो रही है...</i>",
                        parse_mode=enums.ParseMode.HTML
                    )
                else:
                    await message.reply_text(
                        "✅ <b>Payment Verified Successfully!</b>\n<i>Access granted. Preparing your story delivery...</i>",
                        parse_mode=enums.ParseMode.HTML
                    )
                logger.info("[UTR] Success message sent to user.")

                story = await db.db.premium_stories.find_one({"_id": ObjectId(pending_s_id_utr)})
                logger.info(f"[UTR] Refetched story details: {story.get('story_name_en') if story else 'None'}")

                logger.info("[UTR] Dispatching delivery choice options...")
                await dispatch_delivery_choice(client, user_id, story)
                logger.info("[UTR] Delivery choice options dispatched successfully.")
                return
            except Exception as success_err:
                logger.error(f"[UTR] Exception in payment verification success block: {success_err}", exc_info=True)
                err_msg = (
                    "❌ <b>Access Grant Error!</b>\n"
                    "Payment was verified, but we encountered an error while granting access to the story.\n"
                    f"Error details: <code>{success_err}</code>\n\n"
                    "<i>Please contact support with your UTR to manually get the files.</i>"
                )
                if lang == 'hi':
                    err_msg = (
                        "❌ <b>एक्सेस देने में त्रुटि!</b>\n"
                        "पेमेंट वेरिफाई हो गया है, लेकिन कहानी का एक्सेस देने में त्रुटि हुई है।\n"
                        f"विवरण: <code>{success_err}</code>\n\n"
                        "<i>कृपया UTR के साथ सहायता (Support) से संपर्क करें।</i>"
                    )
                await message.reply_text(err_msg, parse_mode=enums.ParseMode.HTML)
                return
        return

    # Back to main menu
    if "𝗕𝗮𝗰𝗸 𝘁𝗼 𝗠𝗲𝗻𝘂" in txt or "BACK TO MAIN MENU" in txt or "वापस मेनू" in txt or txt.startswith("« Back") or txt.startswith("« वापस"):
        try:
            await message.delete()
        except Exception:
            pass
        m = await message.reply_text('<i><emoji id="5348471079482441278">⏳</emoji> Loading...</i>', reply_markup=ReplyKeyboardRemove(), parse_mode=enums.ParseMode.HTML)
        try:
            await m.delete()
        except:
            pass
        await _send_main_menu(client, user_id, message.from_user, lang)
        return

    # -- Marketplace NEXT/PREV pagination handler --
    _nav_next = _sc("NEXT") + " ❭"
    _nav_prev = "❬ " + _sc("PREV")
    _nav_next_hi = "अगला ❭"
    _nav_prev_hi = "❬ पिछला"

    _view_all = "📑 " + _sc("VIEW ALL")
    _view_all_hi = "📑 सभी देखें"

    is_view_all = (
        txt in (_view_all, _view_all_hi, "VIEW ALL", "सभी देखें", _sc("VIEW ALL"), "📑 " + _sc("VIEW ALL"), "📑 सभी देखें")
        or "view all" in txt.lower()
        or "view_all" in txt.lower()
        or "सभी देखें" in txt
        or "सभी कहानियाँ" in txt
        or "सभी कहानियां" in txt
        or "सभी स्टोरिज" in txt
        or "all stories" in txt.lower()
        or "v\u026a\u1d07\u1d33 \u1d00\u029f\u029f" in txt.lower()
        or "viewall" in txt.lower().replace(" ", "").replace("_", "")
        or "सभीदेखें" in txt.replace(" ", "")
    )

    if is_view_all:
        try:
            await message.delete()
        except Exception:
            pass
        u_doc = await db.db.users.find_one({"id": int(user_id)}) or {}
        plat = u_doc.get("_mkt_plat") or user.get("_mkt_plat")

        ALL_PAGE_SIZE = 70

        q_bot = {"$or": [{"bot_id": client.me.id}, {"bot_id": {"$exists": False}}, {"bot_id": None}]}
        if plat == "Other":
            q_plat = {
                "$or": [
                    {"platform": "Other"},
                    {"platform": {"$exists": False}},
                    {"platform": None},
                    {"platform": ""}
                ]
            }
        elif plat:
            q_plat = {"platform": {"$regex": f"^{re.escape(plat)}$", "$options": "i"}}
        else:
            q_plat = {}

        if q_plat:
            q_find = {"$and": [q_bot, q_plat]}
        else:
            q_find = q_bot

        total_s = await db.db.premium_stories.count_documents(q_find)
        if total_s == 0 and q_plat:
            total_s = await db.db.premium_stories.count_documents(q_plat)
            if total_s > 0:
                q_find = q_plat

        if total_s == 0:
            total_s = await db.db.premium_stories.count_documents({})
            if total_s > 0:
                q_find = {}
            else:
                empty_msg = "No stories available right now." if lang == 'en' else "वर्तमान में कोई कहानी उपलब्ध नहीं है।"
                return await message.reply_text(f"<i>{empty_msg}</i>", reply_markup=ReplyKeyboardRemove(), parse_mode=enums.ParseMode.HTML)

        total_pg = max(1, (total_s + ALL_PAGE_SIZE - 1) // ALL_PAGE_SIZE)
        cur_all_page = 0

        # Save mode and page in DB
        await db.db.users.update_one(
            {"id": int(user_id)},
            {"$set": {"_mkt_mode": "all", "_mkt_all_page": cur_all_page, "_mkt_plat": plat}}
        )

        all_stories = await db.db.premium_stories.find(
            q_find,
            {"story_name_en": 1, "story_name_hi": 1, "price": 1, "platform": 1, "_id": 1}
        ).sort("_id", -1).skip(cur_all_page * ALL_PAGE_SIZE).limit(ALL_PAGE_SIZE).to_list(length=ALL_PAGE_SIZE)

        kb = []
        MNL = 22
        start_idx = cur_all_page * ALL_PAGE_SIZE + 1
        for idx, s in enumerate(all_stories, start=start_idx):
            sn = s.get(f'story_name_{lang}', s.get('story_name_en', 'Story'))
            if len(sn) > MNL: sn = sn[:MNL - 1] + "…"
            btn_txt = f"{idx}. {sn} [ ₹ {s.get('price', 0)} ]"
            if idx <= 5:
                kb.append([_kb_btn(btn_txt, icon_custom_emoji_id="6271473763439612077")])
            else:
                kb.append([_kb_btn(btn_txt)])
        
        # Pagination row for View All (if > 70 stories)
        if total_pg > 1:
            nav_row = []
            if cur_all_page > 0:
                nav_row.append(_kb_btn("❬ " + (_sc("PREV") if lang == 'en' else "पिछला")))
            if cur_all_page < total_pg - 1:
                nav_row.append(_kb_btn((_sc("NEXT") if lang == 'en' else "अगला") + " ❭"))
            if nav_row:
                kb.append(nav_row)

        search_text = "SEARCH" if lang == 'en' else "खोजें"
        kb.append([_kb_btn(search_text, icon_custom_emoji_id="6025893082552081088")])
        kb.append([_kb_btn(T[lang]["cant_find_btn"], icon_custom_emoji_id="6025893082552081088")])
        kb.append([_kb_btn("« " + ("𝗕𝗮𝗰𝗸 𝘁𝗼 𝗠𝗲𝗻𝘂" if lang == 'en' else "वापस मेनू"))])

        title = "ALL STORIES" if lang == 'en' else "सभी स्टोरिज"
        plat_hdr = f" — {to_mathbold(plat)}" if plat else ""
        pg_info = f"<i>{_sc('Page')} {cur_all_page+1}/{total_pg} (Total: {total_s})</i>" if total_pg > 1 else f"<i>{_sc('Total Stories:') if lang == 'en' else 'कुल स्टोरिज:'} <b>{total_s}</b></i>"
        msg_text = (
            f'<b>⟦ <emoji id="5764638872000533034">📑</emoji> {title}{plat_hdr} ⟧</b>\n\n'
            f"<blockquote expandable>{pg_info}\n"
            f"{_sc('Tap any story below to view details and purchase:') if lang == 'en' else 'विवरण देखने और खरीदने के लिए नीचे किसी भी कहानी पर टैप करें:'}</blockquote>"
        )
        ok = await _send_reply_keyboard_bot_api(client, user_id, msg_text, kb)
        if not ok:
            pyro_kb = [[b["text"] if isinstance(b, dict) else b for b in r] for r in kb]
            await message.reply_text(
                msg_text,
                reply_markup=ReplyKeyboardMarkup(pyro_kb, resize_keyboard=True),
                parse_mode=enums.ParseMode.HTML
            )
        return

    is_nav_next = (
        txt in (_nav_next, _nav_next_hi, "NEXT ❭", "NEXT", "अगला", "अगला ❭", "ɴᴇxᴛ ❭", "ɴᴇxᴛ", "next ❭", "next")
        or "next" in txt_lower
        or "अगला" in txt
        or "ɴᴇxᴛ" in txt
    )
    is_nav_prev = (
        txt in (_nav_prev, _nav_prev_hi, "❬ PREV", "PREV", "पिछला", "❬ पिछला", "❬ ᴘʀᴇᴠ", "ᴘʀᴇᴠ", "prev", "❬ prev")
        or "prev" in txt_lower
        or "पिछला" in txt
        or "ᴘʀᴇᴠ" in txt
    )

    if is_nav_next or is_nav_prev:
        try:
            await message.delete()
        except Exception:
            pass
        u_fresh = await db.db.users.find_one({"id": int(user_id)}) or {}
        plat = u_fresh.get("_mkt_plat") or user.get("_mkt_plat")
        mkt_mode = u_fresh.get("_mkt_mode") or user.get("_mkt_mode", "normal")
        cur_all_p = int(u_fresh.get("_mkt_all_page", user.get("_mkt_all_page", 0)))
        cur_norm_p = int(u_fresh.get("_mkt_page", user.get("_mkt_page", 0)))

        is_next = is_nav_next

        q_bot = {"$or": [{"bot_id": client.me.id}, {"bot_id": {"$exists": False}}, {"bot_id": None}]}
        if plat == "Other":
            q_plat = {
                "$or": [
                    {"platform": "Other"},
                    {"platform": {"$exists": False}},
                    {"platform": None},
                    {"platform": ""}
                ]
            }
        elif plat:
            q_plat = {"platform": {"$regex": f"^{re.escape(plat)}$", "$options": "i"}}
        else:
            q_plat = {}

        if q_plat:
            q_find = {"$and": [q_bot, q_plat]}
        else:
            q_find = q_bot

        total_s = await db.db.premium_stories.count_documents(q_find)
        if total_s == 0 and q_plat:
            total_s = await db.db.premium_stories.count_documents(q_plat)
            if total_s > 0:
                q_find = q_plat

        if mkt_mode == "all":
            # ── View All Pagination (70 stories per page) ──
            ALL_PAGE_SIZE = 70
            total_pg = max(1, (total_s + ALL_PAGE_SIZE - 1) // ALL_PAGE_SIZE)
            new_page = cur_all_p + 1 if is_next else cur_all_p - 1
            new_page = max(0, min(new_page, total_pg - 1))

            await db.db.users.update_one({"id": int(user_id)}, {"$set": {"_mkt_all_page": new_page, "_mkt_mode": "all", "_mkt_plat": plat}})

            pg_stories = await db.db.premium_stories.find(
                q_find,
                {"story_name_en": 1, "story_name_hi": 1, "price": 1, "platform": 1, "_id": 1}
            ).sort("_id", -1).skip(new_page * ALL_PAGE_SIZE).limit(ALL_PAGE_SIZE).to_list(length=ALL_PAGE_SIZE)

            MNL = 22
            kb = []
            start_idx = new_page * ALL_PAGE_SIZE + 1
            for idx, s in enumerate(pg_stories, start=start_idx):
                sn = s.get(f'story_name_{lang}', s.get('story_name_en', 'Story'))
                if len(sn) > MNL: sn = sn[:MNL - 1] + "…"
                btn_txt = f"{idx}. {sn} [ ₹ {s.get('price', 0)} ]"
                if idx <= 5:
                    kb.append([_kb_btn(btn_txt, icon_custom_emoji_id="6271473763439612077")])
                else:
                    kb.append([_kb_btn(btn_txt)])

            nav_row = []
            if new_page > 0:
                nav_row.append(_kb_btn("❬ " + (_sc("PREV") if lang == 'en' else "पिछला")))
            if new_page < total_pg - 1:
                nav_row.append(_kb_btn((_sc("NEXT") if lang == 'en' else "अगला") + " ❭"))
            if nav_row:
                kb.append(nav_row)

            search_text = "SEARCH" if lang == 'en' else "खोजें"
            kb.append([_kb_btn(search_text, icon_custom_emoji_id="6025893082552081088")])
            kb.append([_kb_btn(T[lang]["cant_find_btn"], icon_custom_emoji_id="6025893082552081088")])
            kb.append([_kb_btn("« " + ("𝗕𝗮𝗰𝗸 𝘁𝗼 𝗠𝗲𝗻𝘂" if lang == 'en' else "वापस मेनू"))])

            title = "ALL STORIES" if lang == 'en' else "सभी स्टोरिज"
            plat_hdr = f" — {to_mathbold(plat)}" if plat else ""
            msg_text = (
                f'<b>⟦ <emoji id="5764638872000533034">📑</emoji> {title}{plat_hdr} ⟧</b>\n\n'
                f"<blockquote expandable><i>{_sc('Page')} {new_page+1}/{total_pg} (Total: {total_s})</i>\n"
                f"{_sc('Tap any story below to view details and purchase:') if lang == 'en' else 'विवरण देखने और खरीदने के लिए नीचे किसी भी कहानी पर टैप करें:'}</blockquote>"
            )
        else:
            # ── Normal Mode Pagination (15 stories per page) ──
            STORY_PAGE_SIZE = 15
            total_pg = max(1, (total_s + STORY_PAGE_SIZE - 1) // STORY_PAGE_SIZE)
            new_page = cur_norm_p + 1 if is_next else cur_norm_p - 1
            new_page = max(0, min(new_page, total_pg - 1))

            await db.db.users.update_one({"id": int(user_id)}, {"$set": {"_mkt_page": new_page, "_mkt_mode": "normal", "_mkt_plat": plat}})

            pg_stories = await db.db.premium_stories.find(
                q_find,
                {"story_name_en": 1, "story_name_hi": 1, "price": 1, "platform": 1, "_id": 1}
            ).sort("_id", -1).skip(new_page * STORY_PAGE_SIZE).limit(STORY_PAGE_SIZE).to_list(length=STORY_PAGE_SIZE)

            MNL = 22
            kb = []
            for idx, s in enumerate(pg_stories, start=new_page * STORY_PAGE_SIZE + 1):
                sn = s.get(f'story_name_{lang}', s.get('story_name_en', 'Story'))
                if len(sn) > MNL: sn = sn[:MNL - 1] + "…"
                btn_txt = f"{idx}. {sn} [ ₹ {s.get('price', 0)} ]"
                if idx <= 5:
                    kb.append([_kb_btn(btn_txt, icon_custom_emoji_id="6271473763439612077")])
                else:
                    kb.append([_kb_btn(btn_txt)])

            nav_row = []
            if new_page > 0:
                nav_row.append(_kb_btn("❬ " + (_sc("PREV") if lang == 'en' else "पिछला")))
            view_all_text = "VIEW ALL" if lang == 'en' else "सभी देखें"
            nav_row.append(_kb_btn(view_all_text, icon_custom_emoji_id="5764638872000533034"))
            if new_page < total_pg - 1:
                nav_row.append(_kb_btn((_sc("NEXT") if lang == 'en' else "अगला") + " ❭"))
            if nav_row:
                kb.append(nav_row)
            
            search_text = "SEARCH" if lang == 'en' else "खोजें"
            kb.append([_kb_btn(search_text, icon_custom_emoji_id="6025893082552081088")])
            kb.append([_kb_btn(T[lang]["cant_find_btn"], icon_custom_emoji_id="6025893082552081088")])
            kb.append([_kb_btn("« " + ("𝗕𝗮𝗰𝗸 𝘁𝗼 𝗠𝗲𝗻𝘂" if lang == 'en' else "वापस मेनू"))])

            title = "AVAILABLE STORIES" if lang == 'en' else "उपलब्ध स्टोरिज"
            plat_hdr = f" — {to_mathbold(plat)}" if plat else ""
            msg_text = (
                f"<b>⟦ {title}{plat_hdr} ⟧</b>\n"
                f"<blockquote expandable><i>{_sc('Page')} {new_page+1}/{total_pg}</i></blockquote>"
            )

        ok = await _send_reply_keyboard_bot_api(client, user_id, msg_text, kb)
        if not ok:
            pyro_kb = [[b["text"] if isinstance(b, dict) else b for b in r] for r in kb]
            await message.reply_text(
                msg_text,
                reply_markup=ReplyKeyboardMarkup(pyro_kb, resize_keyboard=True),
                parse_mode=enums.ParseMode.HTML
            )
        return

    # Check if it's a story selection e.g. "1. STORY NAME [ ₹ 49 ]"
    if " [ ₹ " in txt:
        try:
            await message.delete()
        except Exception:
            pass
        parts = txt.split(". ", 1)
        raw = parts[1] if len(parts) > 1 else txt
        sName = raw.split(" [ ₹ ")[0].strip()
        clean_name = sName.rstrip("…").strip()

        import re
        reg = f"^{re.escape(clean_name)}"
        reg_sub = re.escape(clean_name)
        story = await db.db.premium_stories.find_one({
            "$or": [
                {"story_name_en": {"$regex": reg, "$options": "i"}},
                {"story_name_hi": {"$regex": reg, "$options": "i"}},
                {"story_name_en": {"$regex": reg_sub, "$options": "i"}},
                {"story_name_hi": {"$regex": reg_sub, "$options": "i"}}
            ]
        })

        if not story:
            return await message.reply_text("<i>Story not found or removed.</i>", parse_mode=enums.ParseMode.HTML)

        # Clear search state upon story selection
        await db.db.users.update_one({"id": user_id}, {"$unset": {"state": 1}})

        has_paid = await db.has_purchase(user_id, str(story['_id']))
        if has_paid:
            t = T[lang]
            await message.reply_text(t["already_owned"], reply_markup=ReplyKeyboardRemove())
            return await dispatch_delivery_choice(client, user_id, story)

        return await _show_story_profile(client, user_id, story, lang)

    # Platform selection
    platforms = await db.db.premium_stories.distinct('platform', {"bot_id": client.me.id})
    platforms.append("Other")

    if txt in platforms:
        try:
            await message.delete()
        except Exception:
            pass
        # -- Paginated story listing per platform --
        STORY_PAGE_SIZE = 15
        s_page = int(user.get("_mkt_page", 0))
        if user.get("_mkt_plat") != txt:
            s_page = 0

        query_find = {"bot_id": client.me.id}
        if txt != "Other": query_find["platform"] = txt

        total_s = await db.db.premium_stories.count_documents(query_find)
        if total_s == 0:
            return await message.reply_text("<i>No stories found for this platform.</i>", parse_mode=enums.ParseMode.HTML)

        total_pages_s = max(1, (total_s + STORY_PAGE_SIZE - 1) // STORY_PAGE_SIZE)
        s_page = max(0, min(s_page, total_pages_s - 1))

        await db.db.users.update_one({"id": user_id}, {"$set": {"_mkt_plat": txt, "_mkt_page": s_page, "_mkt_mode": "normal"}})

        page_stories = await db.db.premium_stories.find(
            query_find,
            {"story_name_en": 1, "story_name_hi": 1, "price": 1, "platform": 1, "_id": 1}
        ).sort("_id", -1).skip(s_page * STORY_PAGE_SIZE).limit(STORY_PAGE_SIZE).to_list(length=STORY_PAGE_SIZE)

        MNL = 22
        kb = []
        for idx, s in enumerate(page_stories, start=s_page * STORY_PAGE_SIZE + 1):
            s_name = s.get(f'story_name_{lang}', s.get('story_name_en'))
            if len(s_name) > MNL: s_name = s_name[:MNL - 1] + "…"
            btn_txt = f"{idx}. {s_name} [ ₹ {s.get('price', 0)} ]"
            if idx <= 5:
                kb.append([_kb_btn(btn_txt, icon_custom_emoji_id="6271473763439612077")])
            else:
                kb.append([_kb_btn(btn_txt)])

        nav_row = []
        if s_page > 0: nav_row.append(_kb_btn("❬ " + (_sc("PREV") if lang == 'en' else "पिछला")))
        view_all_text = "VIEW ALL" if lang == 'en' else "सभी देखें"
        nav_row.append(_kb_btn(view_all_text, icon_custom_emoji_id="5764638872000533034"))
        if s_page < total_pages_s - 1: nav_row.append(_kb_btn(_sc("NEXT") + " ❭" if lang == 'en' else "अगला ❭"))
        if nav_row: kb.append(nav_row)
        
        search_text = "SEARCH" if lang == 'en' else "खोजें"
        kb.append([_kb_btn(search_text, icon_custom_emoji_id="6025893082552081088")])
        kb.append([_kb_btn(T[lang]["cant_find_btn"], icon_custom_emoji_id="6025893082552081088")])
        kb.append([_kb_btn("« " + ("𝗕𝗮𝗰𝗸 𝘁𝗼 𝗠𝗲𝗻𝘂" if lang == 'en' else "वापस मेनू"))])

        t = T[lang]
        title = "AVAILABLE STORIES" if lang == 'en' else "उपलब्ध स्टोरिज"
        desc = (
            f"All available stories and their prices are shown in the menu below. "
            f"Please tap or click on any story name from the keyboard menu below to view details and purchase it:"
        ) if lang == 'en' else (
            f"सभी उपलब्ध कहानियाँ और उनकी कीमतें नीचे मेनू में दिखाई गई हैं। "
            f"विवरण देखने और इसे खरीदने के लिए कृपया नीचे दिए गए कीबोर्ड मेनू से किसी भी कहानी के नाम पर टैप या क्लिक करें:"
        )

        msg_text = (
            f"<b>⟦ {title} — {to_mathbold(txt)} ⟧</b>\n\n"
            f"<blockquote expandable>"
            f"<i>{desc}</i>\n"
            f"</blockquote>"
        )
        ok = await _send_reply_keyboard_bot_api(client, user_id, msg_text, kb)
        if not ok:
            pyro_kb = [[b["text"] if isinstance(b, dict) else b for b in r] for r in kb]
            await message.reply_text(
                msg_text,
                reply_markup=ReplyKeyboardMarkup(pyro_kb, resize_keyboard=True),
                parse_mode=enums.ParseMode.HTML
            )
        return

    # ── REQUEST STORY trigger ──
    if txt in (T[lang]["cant_find_btn"], "CAN'T FIND? REQUEST NOW!", "कहानी नहीं मिल रही? अनुरोध करें!", "🔍 CAN'T FIND? REQUEST NOW!", "🔍 कहानी नहीं मिल रही? अनुरोध करें!"):
        try:
            await message.delete()
        except Exception:
            pass
        try:
            from utils import native_ask, log_arya_event
            from datetime import datetime, timezone

            cancel_btn_txt = "« " + (_sc("Cancel") if lang == 'en' else "रद्द करें")
            cancel_kb = ReplyKeyboardMarkup([[cancel_btn_txt]], resize_keyboard=True)

            # Step 1: Story Name
            s1_title = "STORY REQUEST" if lang == 'en' else "कहानी का अनुरोध"
            s1_txt = (
                f"<b>╔══════════════════════╗</b>\n"
                f"<b>        {to_mathbold(s1_title)}</b>\n"
                f"<b>╚══════════════════════╝</b>\n\n"
                f"<b>{_sc('STEP 1 / 3') if lang == 'en' else 'चरण 1 / 3'}</b>\n\n"
                f"<i>({_sc('Note: This is a paid service') if lang == 'en' else 'नोट: यह एक सशुल्क सेवा है'})</i>\n\n"
                f"<b>• {_sc('Enter Story Name:') if lang == 'en' else 'कहानी का नाम दर्ज करें:'}</b>\n"
                f"<i>{_sc('Please provide the full, exact name.') if lang == 'en' else 'कृपया पूरा और सही नाम लिखें।'}</i>"
            )
            ans1 = await native_ask(client, user_id, s1_txt, reply_markup=cancel_kb, parse_mode=enums.ParseMode.HTML)
            if ans1 and hasattr(ans1, 'delete'):
                try: await ans1.delete()
                except Exception: pass

            if _is_cancel(ans1):
                cancel_msg = "Process Cancelled!" if lang == 'en' else "प्रक्रिया रद्द कर दी गई!"
                await client.send_message(user_id, f"<i>❌ {cancel_msg}</i>", reply_markup=ReplyKeyboardRemove(), parse_mode=enums.ParseMode.HTML)
                return await _send_main_menu(client, user_id, message.from_user, lang)

            str_name = (ans1.text if hasattr(ans1, 'text') else str(ans1)).strip()

            # Step 2: Platform
            s2_txt = (
                f"<b>{_sc('STEP 2 / 3') if lang == 'en' else 'चरण 2 / 3'}</b>\n\n"
                f"<b>• {_sc('Enter Platform:') if lang == 'en' else 'प्लेटफ़ॉर्म का नाम दर्ज करें:'}</b>\n"
                f"<i>{_sc('e.g. Pocket FM, Kuku FM, etc.') if lang == 'en' else 'उदा. Pocket FM, Kuku FM, आदि।'}</i>"
            )
            ans2 = await native_ask(client, user_id, s2_txt, reply_markup=cancel_kb, parse_mode=enums.ParseMode.HTML)
            if ans2 and hasattr(ans2, 'delete'):
                try: await ans2.delete()
                except Exception: pass

            if _is_cancel(ans2):
                cancel_msg = "Process Cancelled!" if lang == 'en' else "प्रक्रिया रद्द कर दी गई!"
                await client.send_message(user_id, f"<i>❌ {cancel_msg}</i>", reply_markup=ReplyKeyboardRemove(), parse_mode=enums.ParseMode.HTML)
                return await _send_main_menu(client, user_id, message.from_user, lang)

            str_plat = (ans2.text if hasattr(ans2, 'text') else str(ans2)).strip()

            # Step 3: Type (Ongoing / Complete)
            type_ongoing = "ONGOING" if lang == 'en' else "चल रही है (ONGOING)"
            type_complete = "COMPLETE" if lang == 'en' else "पूरी हो चुकी (COMPLETE)"
            s3_kb = ReplyKeyboardMarkup([[type_ongoing, type_complete], [cancel_btn_txt]], resize_keyboard=True)
            s3_txt = (
                f"<b>{_sc('STEP 3 / 3') if lang == 'en' else 'चरण 3 / 3'}</b>\n\n"
                f"<b>• {_sc('Select Type:') if lang == 'en' else 'प्रकार चुनें:'}</b>\n"
                f"<i>{_sc('Is this story currently ongoing or finished?') if lang == 'en' else 'क्या यह कहानी अभी चल रही है या पूरी हो चुकी है?'}</i>"
            )
            ans3 = await native_ask(client, user_id, s3_txt, reply_markup=s3_kb, parse_mode=enums.ParseMode.HTML)
            if ans3 and hasattr(ans3, 'delete'):
                try: await ans3.delete()
                except Exception: pass

            if _is_cancel(ans3):
                cancel_msg = "Process Cancelled!" if lang == 'en' else "प्रक्रिया रद्द कर दी गई!"
                await client.send_message(user_id, f"<i>❌ {cancel_msg}</i>", reply_markup=ReplyKeyboardRemove(), parse_mode=enums.ParseMode.HTML)
                return await _send_main_menu(client, user_id, message.from_user, lang)

            str_status = (ans3.text if hasattr(ans3, 'text') else str(ans3)).strip()

            m_proc = await message.reply_text('<i><emoji id="5348471079482441278">⏳</emoji> Processing Request...</i>', reply_markup=ReplyKeyboardRemove(), parse_mode=enums.ParseMode.HTML)

            # Save to MongoDB
            req_doc = {
                "user_id": user_id,
                "bot_id": client.me.id,
                "story_name": str_name,
                "platform": str_plat,
                "completion_type": str_status,
                "status": "Sent",
                "created_at": datetime.now(timezone.utc)
            }
            await db.db.premium_requests.insert_one(req_doc)

            await log_arya_event(
                "NEW STORY REQUEST", user_id, user, 
                f"<b>Story:</b> {str_name}\n<b>Platform:</b> {str_plat}\n<b>Type:</b> {str_status}"
            )

            await m_proc.delete()
            asyncio.create_task(react_bg(client, user_id, message.id, pool=REACTIONS_SUCCESS))

            succ_txt = (
                "<b>✅ REQUEST SUBMITTED!</b>\n\n"
                "We have received your request. You can check the status anytime in your Profile.\n\n"
                "<i>Our team will review it shortly.</i>"
            )
            await client.send_message(user_id, succ_txt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_sc("BACK TO MENU"), callback_data="mb#main_back")]]))

        except (asyncio.TimeoutError, asyncio.CancelledError):
            await client.send_message(user_id, "⏳ Request cancelled.", reply_markup=ReplyKeyboardRemove())
            await _send_main_menu(client, user_id, message.from_user, lang)
        return

    # ── SEARCH trigger ──
    if txt == "🔍 " + ("SEARCH" if lang=='en' else "खोजें") or txt in ("SEARCH", "खोजें", "🔎 SEARCH", "🔎 खोजें"):
        try:
            await message.delete()
        except Exception:
            pass
        await message.reply_text(
            f'<b><emoji id="6025893082552081088">🔍</emoji> SEARCH</b>\n\n<i>Type a few words of the story name to search:</i>',
            reply_markup=ReplyKeyboardMarkup([["« " + "CANCEL"]], resize_keyboard=True),
            parse_mode=enums.ParseMode.HTML
        )
        await db.update_user(user_id, {"state": "searching"})
        return

    # ── CANCEL search ──
    if txt == "« " + "CANCEL" or (user.get("state") == "searching" and (txt.startswith("«") or txt.lower() in ["cancel", "रद्द", "back", "वापस"])):
        try:
            await message.delete()
        except Exception:
            pass
        await db.update_user(user_id, {"state": None})
        m = await message.reply_text("<i>❌ Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
        await asyncio.sleep(1.5)
        try: await m.delete()
        except: pass
        await _send_main_menu(client, user_id, message.from_user, lang)
        return

    # ── SEARCH query matching ──
    if user.get("state") == "searching":
        q = txt.lower().strip()
        if not q or len(q) < 2:
            return await message.reply_text("<i>Please type at least 2 characters to search.</i>")

        all_stories = await db.db.premium_stories.find({"$or": [{"bot_id": client.me.id}, {"bot_id": {"$exists": False}}, {"bot_id": None}]}).to_list(length=None)
        if not all_stories:
            all_stories = await db.db.premium_stories.find({}).to_list(length=None)
        matches = [s for s in all_stories if q in s.get("story_name_en", "").lower() or q in s.get("story_name_hi", "").lower()]
        if not matches:
            return await message.reply_text(f"<i>No stories matched '<b>{txt}</b>'. Try different keywords.</i>")

        if len(matches) == 1:
            await db.update_user(user_id, {"state": None})
            return await _show_story_profile(client, user_id, matches[0], lang)

        kb = []
        for idx, s in enumerate(matches, start=1):
            s_name = s.get(f'story_name_{lang}', s.get('story_name_en'))
            btn_txt = f"{idx}. {s_name} [ ₹ {s.get('price', 0)} ]"
            if idx <= 5:
                kb.append([_kb_btn(btn_txt, icon_custom_emoji_id="6271473763439612077")])
            else:
                kb.append([_kb_btn(btn_txt)])
        kb.append([_kb_btn("« " + ("CANCEL" if lang == 'en' else "रद्द करें"))])

        title_res = _sc("Search Results") if lang == 'en' else "खोज परिणाम"
        tap_info = _sc("Tap on a story name from the keyboard menu below to view its details and purchase options.") if lang == 'en' else "विवरण देखने और खरीदने के लिए नीचे दिए गए कीबोर्ड मेनू से किसी कहानी पर टैप करें।"
        msg_text = (
            f'<b><emoji id="5258274739041883702">🔍</emoji> {title_res} ({len(matches)})</b>\n\n'
            f"<blockquote expandable>{tap_info}</blockquote>"
        )
        ok = await _send_reply_keyboard_bot_api(client, user_id, msg_text, kb)
        if not ok:
            pyro_kb = [[b["text"] if isinstance(b, dict) else b for b in r] for r in kb]
            await message.reply_text(
                msg_text,
                reply_markup=ReplyKeyboardMarkup(pyro_kb, resize_keyboard=True),
                parse_mode=enums.ParseMode.HTML
            )
        return





async def _show_marketplace_platforms(client, query, lang='en'):
    platforms = await db.db.premium_stories.distinct('platform', {"bot_id": client.me.id})
    PRIORITY_PLATFORMS = ["Pocket FM", "Eight FM", "Kuku FM", "Kuku TV", "Pratilipi FM", "Headfone", "Story TV"]
    for _rm in ("Other",):
        if _rm in platforms:
            platforms.remove(_rm)

    sorted_plats = []
    for pp in PRIORITY_PLATFORMS:
        if pp in platforms:
            sorted_plats.append(pp)
            platforms.remove(pp)
    sorted_plats.extend(sorted(platforms))
    platforms = sorted_plats

    p_title = _sc("PLATFORM SELECTION") if lang == 'en' else "प्लेटफॉर्म चयन"
    p_desc = _sc("Choose a platform below to browse stories:") if lang == 'en' else "कहानियाँ ब्राउज़ करने के लिए नीचे दिए गए प्लेटफॉर्म में से चुनें:"

    txt = f"<b>🎧 {p_title}</b>\n\n<i>{p_desc}</i>"
    kb = []
    for i in range(0, len(platforms), 2):
        row = []
        for p in platforms[i:i+2]:
            row.append(InlineKeyboardButton(f"• {p} •", callback_data=f"mb#mkt_plat#{p}#0"))
        kb.append(row)

    search_lbl = _sc("SEARCH STORY") if lang == 'en' else "स्टोरी खोजें"
    back_lbl = _sc("MAIN MENU") if lang == 'en' else "मुख्य मेनू"

    kb.append([_ikb(f"  {search_lbl}", callback_data="mb#mkt_search", icon_custom_emoji_id="6025893082552081088")])
    kb.append([InlineKeyboardButton(f"« ❮ {back_lbl}", callback_data="mb#main_back")])

    await _safe_edit(query.message, text=txt, markup=InlineKeyboardMarkup(kb))


async def _show_marketplace_stories(client, query, platform_name: str, page: int = 0, lang='en'):
    PAGE_SIZE = 8
    q_find = {"bot_id": client.me.id}
    if platform_name != "Other":
        q_find["platform"] = platform_name

    total_count = await db.db.premium_stories.count_documents(q_find)
    if total_count == 0:
        empty_txt = "No stories found for this platform." if lang == 'en' else "इस प्लेटफॉर्म के लिए कोई कहानी नहीं मिली।"
        kb = [[InlineKeyboardButton(f"« ❮ {_sc('BACK') if lang == 'en' else 'वापस'}", callback_data="mb#main_marketplace")]]
        return await _safe_edit(query.message, text=f"<i>{empty_txt}</i>", markup=InlineKeyboardMarkup(kb))

    total_pages = max(1, (total_count + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    newest_5_ids = set()
    newest_cursor = await db.db.premium_stories.find(q_find, {"_id": 1}).sort("_id", -1).limit(5).to_list(length=5)
    for doc in newest_cursor:
        newest_5_ids.add(str(doc["_id"]))

    stories = await db.db.premium_stories.find(
        q_find,
        {"story_name_en": 1, "story_name_hi": 1, "price": 1, "platform": 1, "_id": 1}
    ).sort("_id", -1).skip(page * PAGE_SIZE).limit(PAGE_SIZE).to_list(length=PAGE_SIZE)

    kb = []
    for idx, s in enumerate(stories, start=page * PAGE_SIZE + 1):
        s_id = str(s["_id"])
        s_name = s.get(f'story_name_{lang}', s.get('story_name_en', 'Story'))
        if len(s_name) > 22:
            s_name = s_name[:20] + "…"
        price = s.get('price', 0)
        btn_text = f"{idx}. {s_name} [ ₹{price} ]"

        if s_id in newest_5_ids:
            kb.append([_ikb(btn_text, callback_data=f"mb#view_{s_id}", icon_custom_emoji_id="6271473763439612077")])
        else:
            kb.append([InlineKeyboardButton(btn_text, callback_data=f"mb#view_{s_id}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(_sc("PREV") if lang == 'en' else "पिछला", callback_data=f"mb#mkt_plat#{platform_name}#{page-1}"))

    view_all_lbl = _sc("VIEW ALL") if lang == 'en' else "सभी देखें"
    nav.append(_ikb(f"  {view_all_lbl}", callback_data=f"mb#mkt_all#{platform_name}#0", icon_custom_emoji_id="5764638872000533034"))

    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(_sc("NEXT") if lang == 'en' else "अगला", callback_data=f"mb#mkt_plat#{platform_name}#{page+1}"))
    kb.append(nav)

    search_lbl = _sc("SEARCH STORY") if lang == 'en' else "स्टोरी खोजें"
    kb.append([_ikb(f"  {search_lbl}", callback_data="mb#mkt_search", icon_custom_emoji_id="6025893082552081088")])
    kb.append([InlineKeyboardButton(f"« ❮ {_sc('BACK TO PLATFORMS') if lang == 'en' else 'प्लेटफॉर्म मेनू'}", callback_data="mb#main_marketplace")])

    title = _sc("AVAILABLE STORIES") if lang == 'en' else "उपलब्ध कहानियाँ"
    txt = (
        f"<b>⟦ {title} — {to_mathbold(platform_name)} ⟧</b>\n"
        f"<i>Page {page+1}/{total_pages} (Total: {total_count})</i>\n\n"
        f"<blockquote expandable>"
        f"<i>{_sc('Tap any story button below to view details and purchase options.')}</i>"
        f"</blockquote>"
    )
    await _safe_edit(query.message, text=txt, markup=InlineKeyboardMarkup(kb))


async def _show_marketplace_all_stories(client, query, platform_name: str, page: int = 0, lang='en'):
    PAGE_SIZE = 12
    q_find = {"bot_id": client.me.id}
    if platform_name != "Other":
        q_find["platform"] = platform_name

    total_count = await db.db.premium_stories.count_documents(q_find)
    total_pages = max(1, (total_count + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    newest_5_ids = set()
    newest_cursor = await db.db.premium_stories.find(q_find, {"_id": 1}).sort("_id", -1).limit(5).to_list(length=5)
    for doc in newest_cursor:
        newest_5_ids.add(str(doc["_id"]))

    stories = await db.db.premium_stories.find(
        q_find,
        {"story_name_en": 1, "story_name_hi": 1, "price": 1, "platform": 1, "_id": 1}
    ).sort("_id", -1).skip(page * PAGE_SIZE).limit(PAGE_SIZE).to_list(length=PAGE_SIZE)

    kb = []
    for idx, s in enumerate(stories, start=page * PAGE_SIZE + 1):
        s_id = str(s["_id"])
        s_name = s.get(f'story_name_{lang}', s.get('story_name_en', 'Story'))
        if len(s_name) > 22:
            s_name = s_name[:20] + "…"
        price = s.get('price', 0)
        btn_text = f"{idx}. {s_name} [ ₹{price} ]"
        if s_id in newest_5_ids:
            kb.append([_ikb(btn_text, callback_data=f"mb#view_{s_id}", icon_custom_emoji_id="6271473763439612077")])
        else:
            kb.append([InlineKeyboardButton(btn_text, callback_data=f"mb#view_{s_id}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(_sc("PREV") if lang == 'en' else "पिछला", callback_data=f"mb#mkt_all#{platform_name}#{page-1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(f"ᴘᴀɢᴇ {page+1}/{total_pages}", callback_data="mb#noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(_sc("NEXT") if lang == 'en' else "अगला", callback_data=f"mb#mkt_all#{platform_name}#{page+1}"))
    if nav:
        kb.append(nav)

    kb.append([InlineKeyboardButton(f"« ❮ {_sc('BACK TO STORIES') if lang == 'en' else 'वापस'}", callback_data=f"mb#mkt_plat#{platform_name}#0")])

    title = _sc("ALL STORIES") if lang == 'en' else "सभी कहानियाँ"
    txt = (
        f'<b>⟦ <emoji id="5764638872000533034">📑</emoji> {title} — {to_mathbold(platform_name)} ⟧</b>\n'
        f"<i>Page {page+1}/{total_pages} (Total: {total_count})</i>"
    )
    await _safe_edit(query.message, text=txt, markup=InlineKeyboardMarkup(kb))


async def _show_about_arya(client, query, page: int):

    if page == 0:
        txt = (
            f"<b>⟦ {_sc('ABOUT ARYA PREMIUM')} ⟧</b>\n\n"
            f"<blockquote expandable>"
            f"<i>{_sc('Welcome to Arya Premium — the ultimate, fully automated storefront for exclusive, high-quality stories.')}</i>"
            f"</blockquote>\n\n"
            f"<blockquote expandable>"
            f"<u>{_sc('WHAT IT IS:')}</u>\n"
            f"{_sc('Arya Premium is a state-of-the-art paid content delivery ecosystem. It enables users to browse, purchase, and instantly receive premium stories without any manual intervention.')}"
            f"</blockquote>\n\n"
            f"<blockquote expandable>"
            f"<u>{_sc('HOW IT WORKS:')}</u>\n"
            f"• {_sc('Browse the Marketplace to find your desired story.')}\n"
            f"• {_sc('Make a secure payment via automatic gateways (Razorpay) or manual UPI.')}\n"
            f"• {_sc('Upon successful validation, choose your preferred delivery method (Direct DM or Secure Channel).')}"
            f"</blockquote>\n\n"
            f"<blockquote expandable>"
            f"<u>{_sc('CORE FEATURES:')}</u>\n"
            f"• <b>{_sc('Instant Access:')}</b> {_sc('The moment your payment is verified, the content is unlocked forever.')}\n"
            f"• <b>{_sc('Permanent Library:')}</b> {_sc('All your purchases are safely stored in ')}<b>{_sc('My Stories')}</b>. {_sc('You never lose access.')}\n"
            f"• <b>{_sc('Seamless Experience:')}</b> {_sc('Clean UI, fast response times, and high-quality file delivery.')}"
            f"</blockquote>"
        )
        kb = [
            [InlineKeyboardButton(f"ɴᴇxᴛ ❭", callback_data="mb#about_arya_1")],
            [InlineKeyboardButton(f"« ❮ {_sc('BACK')}", callback_data="mb#main_back")]
        ]
    else:
        txt = (
            f"<b>⟦ {_sc('ABOUT ARYA BOT (PARENT)')} ⟧</b>\n\n"
            f"<blockquote expandable>"
            f"<i>{_sc('Arya Premium is proudly powered by the Main Arya Bot architecture — a trusted name in Telegram automation.')}</i>"
            f"</blockquote>\n\n"
            f"<blockquote expandable>"
            f"<u>{_sc('WHAT IT IS:')}</u>\n"
            f"{_sc('The parent Arya Bot is a highly advanced file management and delivery juggernaut, built to handle massive loads and complex operations.')}"
            f"</blockquote>\n\n"
            f"<blockquote expandable>"
            f"<u>{_sc('WHY CHOOSE US:')}</u>\n"
            f"• <b>{_sc('Instant Delivery:')}</b> {_sc('High-speed servers ensure files are forwarded to you with zero lag.')}\n"
            f"• <b>{_sc('Fully Automatic:')}</b> {_sc('No waiting for human admins. Everything is handled securely by code.')}\n"
            f"• <b>{_sc('Trusted Service:')}</b> {_sc('Used by thousands to manage and deliver files reliably every single day.')}"
            f"</blockquote>\n\n"
            f"<blockquote expandable>"
            f"<u>{_sc('FEATURES:')}</u>\n"
            f"• <b>{_sc('Batch Links:')}</b> {_sc('Group hundreds of files securely for public or private sharing.')}\n"
            f"• <b>{_sc('Live Sync:')}</b> {_sc('Real-time mirroring across multiple channels.')}\n"
            f"• <b>{_sc('Smart Management:')}</b> {_sc('Auto-approve logic, Force Subscribe walls, and deep user analytics.')}"
            f"</blockquote>"
        )
        kb = [
            [InlineKeyboardButton(f"❬ ᴘʀᴇᴠ", callback_data="mb#about_arya_0")],
            [InlineKeyboardButton(f"« ❮ {_sc('BACK')}", callback_data="mb#main_back")]
        ]

    await _safe_edit(query.message, text=txt, markup=InlineKeyboardMarkup(kb))


async def _show_help_menu(client, query):
    user_id = query.from_user.id
    user = await db.get_user(user_id, from_user=query.from_user)
    lang = user.get('lang', 'en')

    if lang == 'hi':
        txt = (
            f"<b>⟦ {_sc('सपोर्ट एवं सहायता केंद्र')} ⟧</b>\n\n"
            f"<blockquote expandable>"
            f"<i>{_sc('आर्या प्रीमियम स्टोर गाइड में आपका स्वागत है।')}</i>\n\n"
            f"<u>{_sc('प्रमुख फीचर्स:')}</u>\n"
            f"• <b>Search Your Story:</b> {_sc('होम मेनू से सीधे कीवर्ड डालकर पोस्टर के साथ खोजें।')}\n"
            f"• <b>Marketplace:</b> {_sc('70 प्रति पेज व View All से सभी स्टोरीज ब्राउज़ करें।')}\n"
            f"• <b>My Stories:</b> {_sc('अपनी खरीदी हुई सभी स्टोरीज को कभी भी डाउनलोड करें।')}\n"
            f"• <b>Profile / Settings:</b> {_sc('खाता आईडी देखें, नई स्टोरी रिक्वेस्ट करें व भाषा बदलें।')}\n\n"
            f"<u>{_sc('पेमेंट व डिलीवरी:')}</u>\n"
            f"• <b>Cashfree:</b> {_sc('Cards, NetBanking, UPI द्वारा तुरंत 100% सुरक्षित भुगतान।')}\n"
            f"• <b>Direct UPI:</b> {_sc('दिए गए UPI पर भेजें और 12-अंक UTR से ऑटो-वेरिफाई करें।')}\n"
            f"• <b>Crypto (OxaPay):</b> {_sc('BTC, USDT, ETH व अन्य कॉइन्स से भुगतान।')}\n"
            f"• <b>Instant Delivery:</b> {_sc('पेमेंट के बाद फाइलें तुरंत DM या चैनल में प्राप्त करें।')}\n"
            f"• <b>Regenerate Files:</b> {_sc('मिस हुई फाइलों को दोबारा प्राप्त करने के लिए।')}\n"
            f"</blockquote>"
        )
        kb = [
            [InlineKeyboardButton(f"{_sc('TERMS')}", callback_data="mb#help_tc"),
             InlineKeyboardButton(f"{_sc('REFUND')}", callback_data="mb#help_refund")],
            [_ikb(f"{_sc('FEEDBACK / SUGGESTIONS')}", callback_data="mb#feedback_start", icon_custom_emoji_id="6023852792697854544")],
            [InlineKeyboardButton(_sc("Contact Support"), url="https://t.me/+gFudInzITpo1Yjg1"),
             _ikb("Developer", callback_data="mb#help_dev", icon_custom_emoji_id="6021683099773966917")],
            [InlineKeyboardButton(f"« ❮ {_sc('MAIN MENU')}", callback_data="mb#main_back")]
        ]
    else:
        txt = (
            f"<b>⟦ {_sc('SUPPORT & HELP CENTER')} ⟧</b>\n\n"
            f"<blockquote expandable>"
            f"<i>{_sc('Welcome to Arya Premium Official Guide.')}</i>\n\n"
            f"<u>{_sc('KEY FEATURES:')}</u>\n"
            f"• <b>Search Your Story:</b> {_sc('Search stories instantly by keyword with cover preview.')}\n"
            f"• <b>Marketplace:</b> {_sc('Browse 70 stories per page or use View All.')}\n"
            f"• <b>My Stories:</b> {_sc('Access your library and redownload files anytime.')}\n"
            f"• <b>Profile / Settings:</b> {_sc('View account ID, request stories & switch language.')}\n\n"
            f"<u>{_sc('PAYMENT & DELIVERY:')}</u>\n"
            f"• <b>Cashfree:</b> {_sc('Pay instantly via Cards, NetBanking, or UPI.')}\n"
            f"• <b>Direct UPI:</b> {_sc('Pay via UPI and auto-verify using 12-digit UTR.')}\n"
            f"• <b>Crypto (OxaPay):</b> {_sc('Pay with BTC, USDT, ETH & 300+ coins.')}\n"
            f"• <b>Instant Delivery:</b> {_sc('Automated batch/DM file delivery upon payment.')}\n"
            f"• <b>Regenerate Files:</b> {_sc('Easily re-fetch missed episodes.')}\n"
            f"</blockquote>"
        )
        kb = [
            [InlineKeyboardButton(f"{_sc('TERMS')}", callback_data="mb#help_tc"),
             InlineKeyboardButton(f"{_sc('REFUND')}", callback_data="mb#help_refund")],
            [_ikb(f"{_sc('FEEDBACK / SUGGESTIONS')}", callback_data="mb#feedback_start", icon_custom_emoji_id="6023852792697854544")],
            [InlineKeyboardButton(_sc("Contact Support"), url="https://t.me/+gFudInzITpo1Yjg1"),
             _ikb("Developer", callback_data="mb#help_dev", icon_custom_emoji_id="6021683099773966917")],
            [InlineKeyboardButton(f"« ❮ {_sc('MAIN MENU')}", callback_data="mb#main_back")]
        ]

    await _safe_edit(query.message, text=txt, markup=InlineKeyboardMarkup(kb))





# ─────────────────────────────────────────────────────────────────

# Callback Handler

# ─────────────────────────────────────────────────────────────────

async def _process_callback(client, query):

    user_id = query.from_user.id

    # React to every button tap — fire-and-forget

    asyncio.create_task(react_bg(client, query.message.chat.id, query.message.id, pool=REACTIONS_GENERAL))

    user = await db.get_user(user_id, from_user=query.from_user, bot_id=client.me.id)

    lang = user.get('lang', 'en')

    data = query.data.split('#')
    cmd = data[1]

    # Quick early answer for standard instant-transition callbacks to eliminate button loading spinner
    if cmd in ("main_marketplace", "my_buys", "main_profile", "main_settings", "main_help", "main_back", "return_main", "noop", "show_tc", "mkt_plat", "mkt_all", "mkt_search", "main_search_story") or cmd.startswith(("my_buys_page_", "about_arya_", "tc_accept_", "tc_iaadnsa_", "mkt_")):
        try:
            await query.answer()
        except Exception:
            pass

    # Clear active text-input states if navigating away
    if cmd not in ("feedback_start", "mkt_search", "main_search_story"):
        if user.get("state") in ("feedback_pending", "searching"):
            await db.db.users.update_one({"id": user_id}, {"$unset": {"state": 1}})

    # Check if bot is configured in "miniapp" (Mini App Only / Store OFF) mode
    bt = await _get_cached_bot_doc(client.me.id)
    bot_cfg = (bt.get("config") or {}) if bt else {}
    bot_mode = bot_cfg.get("bot_mode", "full")

    if bot_mode == "miniapp":
        is_allowed = (
            cmd in ("my_buys", "main_close", "close", "feedback", "noop", "cancel_dm", "deliver_dm", "deliver_channel") or
            cmd.startswith("my_buys_page_") or
            cmd.startswith("purchased_view_") or
            cmd.startswith("access_") or
            cmd.startswith("deliver_") or
            cmd.startswith("dm_") or
            cmd.startswith("channel_") or
            cmd.startswith("my_story_") or
            cmd.startswith("deliv_") or
            cmd.startswith("dlv_") or
            cmd.startswith("ep_") or
            cmd.startswith("chunk_") or
            cmd.startswith("full_delivery_") or
            cmd.startswith("part_") or
            cmd.startswith("get_dm_") or
            cmd.startswith("get_channel_") or
            cmd.startswith("page_my_buys_") or
            cmd.startswith("fbresv_") or
            cmd.startswith("fbreply_") or
            cmd.startswith("pay2") or
            cmd.startswith("pay_back") or
            cmd.endswith("_check") or
            cmd.startswith("upi")
        )
        if not is_allowed:
            await query.answer("🛍️ Store is in Mini App! Tap Open App below.", show_alert=False)
            poster_file = _get_arya_poster_path()
            if poster_file and os.path.exists(poster_file):
                try:
                    await query.message.delete()
                    return await client.send_photo(
                        user_id,
                        photo=poster_file,
                        caption=MINI_APP_WELCOME_TEXT,
                        reply_markup=MINI_APP_START_MARKUP,
                        parse_mode=enums.ParseMode.HTML
                    )
                except Exception:
                    pass
            return await query.message.edit_text(
                MINI_APP_WELCOME_TEXT,
                reply_markup=MINI_APP_START_MARKUP,
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True
            )



    try:

        from utils import log_arya_event

        ui = {"first_name": getattr(query.from_user, "first_name", ""), "last_name": getattr(query.from_user, "last_name", ""), "username": getattr(query.from_user, "username", ""), "bot_id": client.me.id}

        act = ""

        if cmd.startswith("about_arya_"): act = "Viewed Arya About Page"

        elif cmd == "my_buys": act = "Opened My Stories"

        elif cmd == "main_profile": act = "Opened Profile"

        elif cmd == "main_settings": act = "Opened Settings"

        elif cmd == "main_help": act = "Opened Support"

        elif cmd == "main_close": act = "Clicked Close Button"

        elif cmd == "main_marketplace": act = f"Opened Marketplace on @{client.me.username}"

        elif cmd.startswith("my_reqs_"): act = "Checked My Requests"

        elif cmd == "toggle_sub": act = "Toggled New Story Alerts"

        elif cmd == "lang": act = f"Changed Language to {data[2] if len(data)>2 else 'Unknown'}"

        elif cmd == "show_tc": act = f"Proceeded to Confirm Story Details (T&C) for Story ID {data[2] if len(data)>2 else ''}"

        elif cmd == "demo": act = f"Viewed Demo Files for Story ID {data[2] if len(data)>2 else ''}"

        elif cmd == "help_tc": act = "Viewed T&C from Support"
        elif cmd == "pay": act = f"Selected Payment Method: {data[2] if len(data)>2 else ''} for Story ID {data[3] if len(data)>3 else ''}"
        elif cmd == "cf_status": act = f"Checked Cashfree Status for Order {data[2] if len(data)>2 else ''}"
        elif cmd == "pay2": act = f"Selected Payment Method (V2): {data[2] if len(data)>2 else ''} for Story ID {data[3] if len(data)>3 else ''}"
        elif cmd.endswith("_check"): act = f"Clicked Verify Payment for Story ID {data[2] if len(data)>2 else ''}"

        elif cmd == "upi_done": act = f"Clicked Payment Done for Manual UPI (Story ID {data[2] if len(data)>2 else ''})"

        elif cmd == "upi2_done": act = f"Clicked Payment Done for Direct UPI (Story ID {data[2] if len(data)>2 else ''})"

        if act: asyncio.create_task(log_arya_event("USER INTERACTION", user_id, ui, act, bot_id=client.me.id))

    except Exception: pass





    # ── Language Selection ──
    if cmd == "lang":
        chosen_lang = data[2] if len(data) > 2 else "en"
        if chosen_lang not in ("en", "hi"):
            chosen_lang = "en"

        await db.db.users.update_one(
            {"id": user_id},
            {"$set": {"lang": chosen_lang}},
            upsert=True
        )
        await query.answer("✓ Language set!" if chosen_lang == "en" else "✓ भाषा सेट हो गई!", show_alert=False)
        try:
            await query.message.delete()
        except Exception:
            pass

        # After language selection, check pending arg (deep link), then force-join, then main menu
        pending_arg = data[3] if len(data) > 3 else None

        # Force join check
        INVITE_CHANNEL = "https://t.me/AryaPremiumTG"
        is_joined = False
        try:
            chat_member = await client.get_chat_member("@AryaPremiumTG", user_id)
            if chat_member.status not in (enums.ChatMemberStatus.BANNED, enums.ChatMemberStatus.LEFT):
                is_joined = True
        except Exception:
            pass

        if not is_joined:
            arg_p = f"#{pending_arg}" if pending_arg else ""
            if chosen_lang == 'hi':
                join_title = "𝗧𝗘𝗟𝗘𝗚𝗥𝗔𝗠 𝗖𝗛𝗔𝗡𝗡𝗘𝗟 𝗝𝗢𝗜𝗡 𝗞𝗔𝗥𝗘𝗡"
                join_txt = (
                    "𝗕𝗼𝘁 𝗸𝗼 𝘂𝘀𝗲 𝗸𝗮𝗿𝗻𝗲 𝗸𝗲 𝗹𝗶𝘆𝗲 𝗮𝗮𝗽𝗸𝗼 𝗵𝘂𝗺𝗮𝗿𝗲 𝗰𝗵𝗮𝗻𝗻𝗲𝗹 𝗺𝗲𝗶𝗻 𝗷𝗼𝗶𝗻 𝗵𝗼𝗻𝗮 𝗵𝗼𝗴𝗮।\n\n"
                    "<blockquote expandable>"
                    "𝗝𝗼𝗶𝗻 𝗸𝗮𝗿𝗻𝗲 𝗸𝗲 𝗯𝗮𝗮𝗱 '𝗝𝗼𝗶𝗻𝗲𝗱' 𝗽𝗮𝗿 𝗰𝗹𝗶𝗰𝗸 𝗸𝗮𝗿𝗲𝗻।\n"
                    "</blockquote>"
                )
                join_btn = "✓ 𝗝𝗢𝗜𝗡 𝗖𝗛𝗔𝗡𝗡𝗘𝗟"
                joined_btn = "✓ 𝗝𝗢𝗜𝗡 𝗞𝗔𝗥 𝗟𝗜𝗬𝗔"
            else:
                join_title = "𝗝𝗢𝗜𝗡 𝗢𝗨𝗥 𝗖𝗛𝗔𝗡𝗡𝗘𝗟"
                join_txt = (
                    "𝗬𝗼𝘂 𝗺𝘂𝘀𝘁 𝗷𝗼𝗶𝗻 𝗼𝘂𝗿 𝗧𝗲𝗹𝗲𝗴𝗿𝗮𝗺 𝗰𝗵𝗮𝗻𝗻𝗲𝗹 𝘁𝗼 𝘂𝘀𝗲 𝘁𝗵𝗶𝘀 𝗯𝗼𝘁.\n\n"
                    "<blockquote expandable>"
                    "𝗔𝗳𝘁𝗲𝗿 𝗷𝗼𝗶𝗻𝗶𝗻𝗴, 𝗰𝗹𝗶𝗰𝗸 '𝗝𝗼𝗶𝗻𝗲𝗱' 𝘁𝗼 𝗰𝗼𝗻𝘁𝗶𝗻𝘂𝗲."
                    "</blockquote>"
                )
                join_btn = "✓ 𝗝𝗢𝗜𝗡 𝗖𝗛𝗔𝗡𝗡𝗘𝗟"
                joined_btn = "✓ 𝗝𝗢𝗜𝗡𝗘𝗗"

            join_kb = [
                [InlineKeyboardButton(join_btn, url=INVITE_CHANNEL)],
                [InlineKeyboardButton(joined_btn, callback_data=f"mb#jchk{arg_p}")]
            ]
            return await client.send_message(
                user_id,
                f"<b>{join_title}</b>\n\n{join_txt}",
                reply_markup=InlineKeyboardMarkup(join_kb),
                parse_mode=enums.ParseMode.HTML
            )

        # Already joined → go to main menu or process pending deep link
        if pending_arg:
            class MockMsg:
                from_user = query.from_user
                chat = query.message.chat
                command = ["start", pending_arg]
                id = query.message.id
                async def reply_text(self, text, **kw):
                    return await client.send_message(user_id, text, **kw)
            from plugins.userbot.market_seller import _process_start
            return await _process_start(client, MockMsg())

        return await _send_main_menu(client, user_id, query.from_user, chosen_lang)



    # ── Joined Check ──

    if cmd == "jchk" or cmd == "joined_check":

        try:

            from pyrogram import enums

            chat_member = await client.get_chat_member("@AryaPremiumTG", user_id)

            if chat_member.status not in (enums.ChatMemberStatus.BANNED, enums.ChatMemberStatus.LEFT):

                msg = "✓ Joined Success!" if lang == 'en' else "✓ आपने सफलतापूर्वक ज्वाइन कर लिया है!"

                await query.answer(msg, show_alert=True)

                try: await query.message.delete()

                except: pass

                

                pending_arg = data[2] if len(data) > 2 else None

                if pending_arg:

                    class MockMsg:

                        from_user = query.from_user

                        chat = query.message.chat

                        command = ["start", pending_arg]

                        id = query.message.id

                        async def reply_text(self, text, **kw):

                            return await client.send_message(user_id, text, **kw)

                    from plugins.userbot.market_seller import _process_start

                    return await _process_start(client, MockMsg())

                

                return await _send_main_menu(client, user_id, query.from_user, lang)

            else:

                msg = "Aapne abhi tak join nahi kiya hai। Kripya join karein aur phir check karein।" if lang == 'hi' else "You haven't joined yet. Please join the channel first."

                return await query.answer(msg, show_alert=True)

        except Exception:

            return await query.answer("Error checking status. Make sure you joined.", show_alert=True)



    # ── Skip Channel Prompt ──

    if cmd == "skip_channel_prompt":

        await query.answer()

        try: await query.message.delete()

        except: pass

        return await _send_main_menu(client, user_id, query.from_user, lang)



    # ── About Arya ──

    if cmd.startswith("about_arya_"):

        page = int(cmd.replace("about_arya_", ""))

        await query.answer()

        return await _show_about_arya(client, query, page)



    # ── Help Menu Pagination (Legacy fallback) ──

    if cmd.startswith("help_page_"):

        await query.answer()

        return await _show_help_menu(client, query)



    # ── Main Menu actions (inline buttons) ──

    if cmd.startswith("main_"):

        action = cmd.replace("main_", "")

        await query.answer()



        if action == "marketplace":
            bt_rec = await db.db.premium_bots.find_one({"id": client.me.id}) or {}
            bt_cfg_local = bt_rec.get("config", {}) or {}
            is_ss = (bt_cfg_local.get("bot_mode") == "show_store")

            platforms = await db.db.premium_stories.distinct('platform', {"bot_id": client.me.id})
            PRIORITY_PLATFORMS = ["Pocket FM", "Eight FM", "Kuku FM", "Kuku TV", "Pratilipi FM", "Headfone", "Story TV"]
            for _rm in ("Other",):
                if _rm in platforms:
                    platforms.remove(_rm)

            sorted_plats = []
            for pp in PRIORITY_PLATFORMS:
                if pp in platforms:
                    sorted_plats.append(pp)
                    platforms.remove(pp)
            sorted_plats.extend(sorted(platforms))
            platforms = sorted_plats

            await db.db.users.update_one({"id": user_id}, {"$set": {"_mkt_page": 0}})

            # ── If in Show Store mode OR only 1 platform, skip selection and jump directly ──
            if is_ss or len(platforms) == 1:
                if is_ss:
                    auto_plat = bt_cfg_local.get("platform_name") or (platforms[0] if platforms else "Story TV")
                else:
                    auto_plat = platforms[0]

                await db.db.users.update_one(
                    {"id": user_id},
                    {"$set": {"_mkt_plat": auto_plat, "_mkt_page": 0, "_mkt_mode": "normal"}}
                )
                try:
                    await query.message.delete()
                except Exception:
                    pass

                q_bot = {"$or": [{"bot_id": client.me.id}, {"bot_id": {"$exists": False}}, {"bot_id": None}]}
                q_plat = {"platform": {"$regex": f"^{re.escape(auto_plat)}$", "$options": "i"}}
                q_find = {"$and": [q_bot, q_plat]}
                if is_ss:
                    q_find["is_show"] = True

                PAGE_SIZE = 20
                total_s = await db.db.premium_stories.count_documents(q_find)

                # If 0 shows in show store mode
                if total_s == 0 and is_ss:
                    plat_title = to_mathbold(auto_plat)
                    msg_text = (
                        f'<b>⟦ <emoji id="5937999673510858217">📽️</emoji> {plat_title} ⟧</b>\n\n'
                        f"<i>{'No shows available in this store yet. Please check back later!' if lang == 'en' else 'इस स्टोर में अभी कोई शो उपलब्ध नहीं है। कृपया बाद में चेक करें!'}</i>"
                    )
                    kb = [[_kb_btn("« " + ("𝗕𝗮𝗰𝗸 𝘁𝗼 𝗠𝗲𝗻𝘂" if lang == 'en' else "वापस मेनू"))]]
                    ok = await _send_reply_keyboard_bot_api(client, user_id, msg_text, kb)
                    if not ok:
                        pyro_kb = [[b["text"] if isinstance(b, dict) else b for b in r] for r in kb]
                        await client.send_message(user_id, msg_text, reply_markup=ReplyKeyboardMarkup(pyro_kb, resize_keyboard=True), parse_mode=enums.ParseMode.HTML)
                    return

                total_pg = max(1, (total_s + PAGE_SIZE - 1) // PAGE_SIZE)
                stories_page = await db.db.premium_stories.find(
                    q_find, {"story_name_en": 1, "story_name_hi": 1, "price": 1, "platform": 1, "_id": 1}
                ).sort("_id", -1).limit(PAGE_SIZE).to_list(length=PAGE_SIZE)

                item_label = ("शो" if lang == 'hi' else "Show") if is_ss else ("कहानी" if lang == 'hi' else "Story")

                kb = []
                MNL = 22
                for idx, s in enumerate(stories_page, start=1):
                    sn = s.get(f'story_name_{lang}', s.get('story_name_en', item_label))
                    if len(sn) > MNL: sn = sn[:MNL - 1] + "…"
                    btn_txt = f"{idx}. {sn} [ ₹ {s.get('price', 0)} ]"
                    if idx <= 5:
                        kb.append([_kb_btn(btn_txt, icon_custom_emoji_id="6271473763439612077")])
                    else:
                        kb.append([_kb_btn(btn_txt)])

                if total_pg > 1:
                    nav_row = []
                    if total_pg > 1:
                        nav_row.append(_kb_btn((_sc("NEXT") if lang == 'en' else "अगला") + " ❭"))
                    if nav_row:
                        kb.append(nav_row)

                view_all_btn = "📑 " + (_sc("VIEW ALL") if lang == 'en' else "सभी देखें")
                search_text = "SEARCH" if lang == 'en' else "खोजें"
                kb.append([_kb_btn(view_all_btn)])
                kb.append([_kb_btn(search_text, icon_custom_emoji_id="6025893082552081088")])
                kb.append([_kb_btn(T[lang]["cant_find_btn"], icon_custom_emoji_id="6025893082552081088")])
                kb.append([_kb_btn("« " + ("𝗕𝗮𝗰𝗸 𝘁𝗼 𝗠𝗲𝗻𝘂" if lang == 'en' else "वापस मेनू"))])

                plat_title = to_mathbold(auto_plat)
                pg_info = f"<i>{_sc('Page') if lang == 'en' else 'पेज'} 1/{total_pg} (Total: {total_s})</i>" if total_pg > 1 else f"<i>{'Total:' if lang == 'en' else 'कुल:'} <b>{total_s}</b></i>"
                icon_tag = '<emoji id="5937999673510858217">📽️</emoji>' if is_ss else '<emoji id="5764638872000533034">📑</emoji>'
                tap_hint = (_sc("Tap any show below to view details and purchase:") if lang == 'en' else "विवरण देखने और खरीदने के लिए नीचे किसी भी शो पर टैप करें:") if is_ss else (_sc("Tap any story below to view details and purchase:") if lang == 'en' else "विवरण देखने और खरीदने के लिए नीचे किसी भी कहानी पर टैप करें:")
                msg_text = (
                    f'<b>⟦ {icon_tag} {plat_title} ⟧</b>\n\n'
                    f"<blockquote expandable>{pg_info}\n"
                    f"{tap_hint}</blockquote>"
                )
                ok = await _send_reply_keyboard_bot_api(client, user_id, msg_text, kb)
                if not ok:
                    pyro_kb = [[b["text"] if isinstance(b, dict) else b for b in r] for r in kb]
                    await client.send_message(user_id, msg_text, reply_markup=ReplyKeyboardMarkup(pyro_kb, resize_keyboard=True), parse_mode=enums.ParseMode.HTML)
                return

            if not platforms:
                msg_text = (
                    f'<b>⟦ <emoji id="5764638872000533034">📑</emoji> MARKETPLACE ⟧</b>\n\n'
                    f"<i>{'No stories available in this store yet. Please check back later!' if lang == 'en' else 'इस स्टोर में अभी कोई कहानी उपलब्ध नहीं है। कृपया बाद में चेक करें!'}</i>"
                )
                kb = [[_kb_btn("« " + ("𝗕𝗮𝗰𝗸 𝘁𝗼 𝗠𝗲𝗻𝘂" if lang == 'en' else "वापस मेनू"))]]
                try: await query.message.delete()
                except Exception: pass
                ok = await _send_reply_keyboard_bot_api(client, user_id, msg_text, kb)
                if not ok:
                    pyro_kb = [[b["text"] if isinstance(b, dict) else b for b in r] for r in kb]
                    await client.send_message(user_id, msg_text, reply_markup=ReplyKeyboardMarkup(pyro_kb, resize_keyboard=True), parse_mode=enums.ParseMode.HTML)
                return

            t = T[lang]
            kb = []
            for i in range(0, len(platforms), 2):
                row = platforms[i:i+2]
                kb.append(row)
            kb.append(["« " + ("𝗕𝗮𝗰𝗸 𝘁𝗼 𝗠𝗲𝗻𝘂" if lang=='en' else "वापस मेनू")])

            p_title = "🎧 Platform Selection" if lang == 'en' else "🎧 प्लेटफॉर्म चयन"
            p_desc = "Choose a platform from the keyboard below:" if lang == 'en' else "नीचे दिए गए कीबोर्ड से एक प्लेटफॉर्म चुनें:"

            await query.message.delete()
            return await client.send_message(
                user_id,
                f"<b>{p_title}</b>\n\n{p_desc}",
                reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
            )




        elif action == "profile":

            u = query.from_user

            joined = user.get('joined_date', 'N/A')

            if isinstance(joined, datetime):

                joined = joined.strftime('%d %b %Y')

            

            # Deduplicate purchases for count

            raw_p = user.get('purchases', [])

            purchases = list(set(str(p) for p in raw_p))

            

            uname = f"@{u.username}" if u.username else "N/A"

            lang_label = "English" if lang == 'en' else "हिंदी"

            name = f"{u.first_name or ''} {u.last_name or ''}".strip() or "Unknown"

            

            t = T[lang]

            txt_p = (

                f"<b>{t['prof_title']}</b>\n\n"

                f"<b>⧉ {t['prof_name']}        ⟶</b> {name}\n"

                f"<b>⧉ {t['prof_uname']}    ⟶</b> {uname}\n"

                f"<b>⧉ {t['prof_id']}       ⟶</b> <code>{u.id}</code>\n\n"

                "<b>╠══════════════════╣</b>\n\n"

                f"<b>⧉ {t['prof_bought']}   ⟶</b> {len(purchases)}\n"

                f"<b>⧉ {t['prof_lang']}    ⟶</b> {lang_label}\n"

                f"<b>⧉ {t['prof_join']}      ⟶</b> {joined}\n\n"

                "<b>╚══════════════════╝</b>"

            )

            req_label = _sc("MY REQUESTS") if lang == 'en' else "मेरे अनुरोध"
            set_label = _sc("Settings") if lang == 'en' else "सेटिंग्स"
            abt_label = _sc("About") if lang == 'en' else "अबाउट"
            back_label = _sc("BACK") if lang == 'en' else "वापस"

            kb = [
                [_ikb(req_label, callback_data="mb#my_reqs_0", icon_custom_emoji_id="5766915217552315762")],
                [
                    _ikb(set_label, callback_data="mb#main_settings", icon_custom_emoji_id="6021637109264160908"),
                    _ikb(abt_label, callback_data="mb#about_arya_0", icon_custom_emoji_id="6021620268697393273")
                ],
                [InlineKeyboardButton("« ❮ " + back_label, callback_data="mb#main_back")]
            ]
            await _safe_edit(query.message, text=txt_p, markup=InlineKeyboardMarkup(kb))
            return

        elif action == "settings":
            t = T[lang]
            subscribed = user.get("alerts_subscribed", False)   # Default OFF
            if lang == 'hi':
                sub_text = "अपडेट नोटिफिकेशन: चालू" if subscribed else "अपडेट नोटिफिकेशन: बंद"
                sub_emoji = "6021536113108196448" if subscribed else "6021440013214948027"
                settings_txt = (
                    '<emoji id="6021637109264160908">⚙️</emoji> <b>सेटिंग्स</b>\n\n'
                    '<emoji id="6030768072296502910">🌐</emoji> <b>भाषा:</b> अपनी पसंदीदा भाषा चुनें\n'
                    '<b>नोटिफिकेशन:</b> नई कहानियों का अलर्ट'
                )
            else:
                sub_text = "New Story Alerts: On" if subscribed else "New Story Alerts: Off"
                sub_emoji = "6021536113108196448" if subscribed else "6021440013214948027"
                settings_txt = (
                    '<emoji id="6021637109264160908">⚙️</emoji> <b>Settings</b>\n\n'
                    '<emoji id="6030768072296502910">🌐</emoji> <b>Language:</b> Choose your preferred language\n'
                    '<b>Notifications:</b> Get alerted when new stories arrive'
                )

            kb = [
                [
                    _ikb("English", callback_data="mb#lang#en", icon_custom_emoji_id="5293993521026453119"),
                    _ikb("हिंदी", callback_data="mb#lang#hi", icon_custom_emoji_id="5291933173674957761")
                ],
                [_ikb(sub_text, callback_data="mb#toggle_sub", icon_custom_emoji_id=sub_emoji)],
                [InlineKeyboardButton("« ❮ " + (_sc("BACK") if lang == 'en' else "वापस"), callback_data="mb#main_back")]
            ]
            await _safe_edit(query.message, text=settings_txt, markup=InlineKeyboardMarkup(kb))



        elif action == "search_story":
            await db.update_user(user_id, {"state": "searching"})
            s_title = _sc("SEARCH STORY") if lang == 'en' else "स्टोरी खोजें"
            s_prompt = _sc("Please type the story name or keywords in the chat below:") if lang == 'en' else "कृपया नीचे चैट में कहानी का नाम या कीवर्ड टाइप करें:"
            c_label = _sc("CANCEL") if lang == 'en' else "रद्द करें"
            txt_s = (
                f'<b><emoji id="5258274739041883702">🔍</emoji> {s_title}</b>\n\n'
                f'<i>{s_prompt}</i>'
            )
            kb = [[InlineKeyboardButton(f"« ❮ {c_label}", callback_data="mb#main_back")]]
            await _safe_edit(query.message, text=txt_s, markup=InlineKeyboardMarkup(kb))
            return

        elif action == "help":

            return await _show_help_menu(client, query)



        elif action == "close":

            await query.message.delete()



        elif action == "back":

            await _edit_main_menu_in_place(client, query, query.from_user, lang)



    elif cmd.startswith("my_reqs_"):

        page = int(cmd.replace("my_reqs_", ""))

        reqs = await db.db.premium_requests.find({"user_id": user_id, "bot_id": client.me.id}).sort("created_at", -1).to_list(length=100)

        t = T[lang]

        if not reqs:

            title = _sc("STORY REQUESTS") if lang == 'en' else "स्टोरी अनुरोध"

            empty = _sc("You haven't made any story requests yet.") if lang == 'en' else "आपने अभी तक कोई अनुरोध नहीं किया है।"

            kb = [[InlineKeyboardButton(_sc("BACK TO PROFILE"), callback_data="mb#main_profile")]]

            return await _safe_edit(query.message, text=f"<b>╔══════════════════════╗\n        {title}\n╚══════════════════════╝</b>\n\n{empty}", markup=InlineKeyboardMarkup(kb))

            

        items_per_page = 10

        total_pages = max(1, (len(reqs) + items_per_page - 1) // items_per_page)

        page = max(0, min(page, total_pages - 1))

        subset = reqs[page*items_per_page : (page+1)*items_per_page]

        

        l_title = _sc("MY STORY REQUESTS") if lang == 'en' else "मेरे स्टोरी अनुरोध"

        l_click = _sc("Track the status of your requests below:") if lang == 'en' else "अपने अनुरोधों की स्थिति नीचे ट्रैक करें:"

        

        txt_req = (

            f"<b>╔══════════════════════╗</b>\n"

            f"<b>        {l_title}</b>\n"

            f"<b>╚══════════════════════╝</b>\n\n"

            f"<b>⧉ PAGE {page+1} 𝗢𝗙 {total_pages}</b>\n"

            f"<i>{l_click}</i>\n"

        )

        kb = []

        for r in subset:

            sname = r.get('story_name', 'Unknown')

            if len(sname) > 22: sname = sname[:20] + ".."

            stt = r.get('status', 'Sent').upper()

            if lang == 'hi':

                stt = {"SENT": "भेजा गया", "PENDING": "लंबित", "SEARCHING": "ढूंढ रहे हैं", "POSTING": "अपलोड हो रहा है", "POSTED": "अपलोड हो गया", "COMPLETED": "पूरा हुआ"}.get(stt, stt)

            

            kb.append([InlineKeyboardButton(f"• {sname} [{stt}]", callback_data=f"mb#my_req_{str(r['_id'])}")])

            

        nav = []

        if page > 0: nav.append(InlineKeyboardButton(_sc("PREV"), callback_data=f"mb#my_reqs_{page-1}"))

        if page < total_pages - 1: nav.append(InlineKeyboardButton(_sc("NEXT"), callback_data=f"mb#my_reqs_{page+1}"))

        if nav: kb.append(nav)

        

        kb.append([InlineKeyboardButton(_sc("BACK TO PROFILE"), callback_data="mb#main_profile")])

        await _safe_edit(query.message, text=txt_req, markup=InlineKeyboardMarkup(kb))

        return



    elif cmd.startswith("my_req_"):

        req_id = cmd.replace("my_req_", "")

        try:

            from bson import ObjectId

            r = await db.db.premium_requests.find_one({"_id": ObjectId(req_id), "user_id": user_id})

        except: r = None

        if not r:

            return await query.answer("Request not found.", show_alert=True)

            

        t_str = r.get("created_at").strftime('%d %b %Y') if r.get("created_at") else "Unknown"

        status = r.get('status', 'Sent').upper()

        

        if lang == 'hi':

            title = "अनुरोध विवरण"

            fields = ["कहानी", "प्लेटफॉर्म", "प्रकार", "तारीख", "स्थिति"]

            st_map = {"SENT": "भेजा गया", "PENDING": "लंबित", "SEARCHING": "ढूंढ रहे हैं", "POSTING": "अपलोड हो रहा है", "POSTED": "अपलोड हो गया", "COMPLETED": "पूरा हुआ"}

            status_display = st_map.get(status, status)

        else:

            title = "REQUEST DETAILS"

            fields = ["STORY", "PLATFORM", "TYPE", "DATE", "STATUS"]

            status_display = status



        txt_d = (

            f"<b>╔══════════════════════╗</b>\n"

            f"<b>        {title}</b>\n"

            f"<b>╚══════════════════════╝</b>\n\n"

            f'<blockquote expandable="true">'

            f"<b>• {fields[0]}    ⟶</b> {r.get('story_name')}\n"

            f"<b>• {fields[1]} ⟶</b> {r.get('platform')}\n"

            f"<b>• {fields[2]}     ⟶</b> {r.get('completion_type', 'N/A')}\n"

            f"<b>• {fields[3]}     ⟶</b> {t_str}\n" 

            f'</blockquote>\n'

            f"<b>⧉ {fields[4]}</b>\n"

            f"<blockquote><b>[ {status_display} ]</b></blockquote>\n\n"

        )

        

        # Sub-status messages

        msg_sub = {

            "SENT": "<i>Our team will review your request soon.</i>",

            "PENDING": "<i>Processing your request...</i>",

            "SEARCHING": "<i>We are currently looking for this story.</i>",

            "POSTING": "<i>Almost ready! We are uploading files.</i>",

            "POSTED": "<i>Files are being finalized for delivery.</i>",

            "COMPLETED": "<i>✅ Success! Story is now available in Marketplace.</i>"

        }.get(status, "")

        

        if lang == 'hi':

            msg_sub = {

                "SENT": "<i>हमारी टीम जल्द ही आपके अनुरोध की समीक्षा करेगी।</i>",

                "PENDING": "<i>आपके अनुरोध पर कार्रवाई की जा रही है...</i>",

                "SEARCHING": "<i>हम फिलहाल इस कहानी की तलाश कर रहे हैं।</i>",

                "POSTING": "<i>लगभग तैयार! फाइलें अपलोड की जा रही हैं।</i>",

                "POSTED": "<i>डिलीवरी के लिए फाइलें तैयार की जा रही हैं।</i>",

                "COMPLETED": "<i>✅ सफलता! कहानी अब मार्केटप्लेस में उपलब्ध है।</i>"

            }.get(status, "")



        if msg_sub: txt_d += f"{msg_sub}"

        

        kb = [[InlineKeyboardButton(_sc("BACK TO REQUESTS"), callback_data="mb#my_reqs_0")]]

        await _safe_edit(query.message, text=txt_d, markup=InlineKeyboardMarkup(kb))

        return



    elif cmd == "toggle_sub":

        u = await db.get_user(user_id, from_user=query.from_user)

        current_sub = u.get("alerts_subscribed", False)   # Default OFF

        new_sub = not current_sub

        await db.update_user(user_id, {"alerts_subscribed": new_sub})



        lang = u.get("lang", "en")
        if lang == 'hi':
            sub_text = "अपडेट नोटिफिकेशन: चालू" if new_sub else "अपडेट नोटिफिकेशन: बंद"
            sub_emoji = "6021536113108196448" if new_sub else "6021440013214948027"
            settings_txt = (
                '<emoji id="6021637109264160908">⚙️</emoji> <b>सेटिंग्स</b>\n\n'
                '<emoji id="6030768072296502910">🌐</emoji> <b>भाषा:</b> अपनी पसंदीदा भाषा चुनें\n'
                '<b>नोटिफिकेशन:</b> नई कहानियों का अलर्ट'
            )
            alert_msg = "नोटिफिकेशन चालू किया!" if new_sub else "नोटिफिकेशन बंद किया!"
        else:
            sub_text = "New Story Alerts: On" if new_sub else "New Story Alerts: Off"
            sub_emoji = "6021536113108196448" if new_sub else "6021440013214948027"
            settings_txt = (
                '<emoji id="6021637109264160908">⚙️</emoji> <b>Settings</b>\n\n'
                '<emoji id="6030768072296502910">🌐</emoji> <b>Language:</b> Choose your preferred language\n'
                '<b>Notifications:</b> Get alerted when new stories arrive'
            )
            alert_msg = "Story alerts enabled!" if new_sub else "Story alerts disabled!"

        kb = [
            [
                _ikb("English", callback_data="mb#lang#en", icon_custom_emoji_id="5293993521026453119"),
                _ikb("हिंदी", callback_data="mb#lang#hi", icon_custom_emoji_id="5291933173674957761")
            ],
            [_ikb(sub_text, callback_data="mb#toggle_sub", icon_custom_emoji_id=sub_emoji)],
            [InlineKeyboardButton("« ❮ " + (_sc("BACK") if lang == 'en' else "वापस"), callback_data="mb#main_back")]
        ]

        await _safe_edit(query.message, text=settings_txt, markup=InlineKeyboardMarkup(kb))

        await query.answer(alert_msg, show_alert=False)

        return



    elif cmd == "return_main":

        await _edit_main_menu_in_place(client, query, query.from_user, lang)

        return



    elif cmd == "show_tc":

        s_id = data[2]

        

        u = await db.get_user(user_id, from_user=query.from_user)

        if u.get("tc_bypassed", False):

            from bson.objectid import ObjectId

            story = await db.db.premium_stories.find_one({"_id": ObjectId(s_id)})

            if story:

                _bt = await db.db.premium_bots.find_one({"id": client.me.id})

                _bt_cfg = (_bt or {}).get("config", {})

                await query.answer()

                

                from utils import log_arya_event

                user_obj = query.from_user

                s_name = story.get(f'story_name_{lang}', story.get('story_name_en', 'Unknown'))

                asyncio.create_task(log_arya_event(

                    event_type="T&C BYPASSED",

                    user_id=user_id,

                    user_info={"first_name": user_obj.first_name or "Unknown", "last_name": user_obj.last_name or "", "username": user_obj.username or ""},

                    details=f"User bypassed the Terms & Conditions page using IAADNSA flag while purchasing '{s_name}'."

                ))

                

                return await _show_story_details(client, query, story, lang, bot_cfg=_bt_cfg)

                

        try:

            await query.message.delete()

        except:

            pass

        # Check if T&C is globally disabled by admin

        _tnc_cfg1 = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}

        # Check if T&C is globally disabled by admin
        _tnc_cfg1 = await _get_cached_features()
        if not _tnc_cfg1.get("tnc_enabled", True):
            from bson.objectid import ObjectId as _ObjId1
            _s1 = await db.db.premium_stories.find_one({"_id": _ObjId1(s_id)})
            if _s1:
                _bt1 = await _get_cached_bot_doc(client.me.id)
                _bt_cfg1 = (_bt1 or {}).get("config", {})
                return await _show_story_details(client, query, _s1, lang, bot_cfg=_bt_cfg1)
        return await _show_tc(client, user_id, s_id, lang, from_user=query.from_user)

    elif cmd == "demo":
        s_id = data[2]
        from bson.objectid import ObjectId
        story = await db.db.premium_stories.find_one({"_id": ObjectId(s_id)})
        if not story: return await query.answer("Story not found!", show_alert=True)
        await query.answer()

        try:
            from utils import log_arya_event
            ui = {"first_name": getattr(query.from_user, "first_name", ""), "last_name": getattr(query.from_user, "last_name", ""), "username": getattr(query.from_user, "username", ""), "bot_id": client.me.id}
            sName = story.get(f'story_name_{lang}', story.get('story_name_en', 'Unknown'))
            asyncio.create_task(log_arya_event("VIEWED DEMO", user_id, ui, f"User viewed demo files for story: {sName}"))
        except: pass
        asyncio.create_task(_send_demo_files(client, user_id, story, lang))

    # -- My Buys (My Stories) --
    elif cmd == "my_buys" or cmd.startswith("my_buys_page_"):
        await query.answer()
        page = 0
        if cmd.startswith("my_buys_page_"):
            try: page = int(cmd.replace("my_buys_page_", ""))
            except: page = 0
        return await _send_my_stories_menu(client, user_id, user, lang, page=page, edit_query=query)

    elif cmd == "noop":
        await query.answer()

    # ── Language ──
    elif cmd == "lang":
        new_lang = data[2]
        pending_arg = data[3] if len(data) > 3 else None
        await query.answer("✓ Updates applied!", show_alert=False)
        m = await client.send_message(user_id, '<b>› › <emoji id="5348471079482441278">⏳</emoji> Yup, Bro updating...</b>', parse_mode=enums.ParseMode.HTML)
        await asyncio.sleep(2)
        await db.update_user(user_id, {"lang": new_lang})
        try: await m.delete()
        except: pass
        if pending_arg:
            class MockMsg:
                from_user = query.from_user
                chat = query.message.chat
                command = ["start", pending_arg]
                id = query.message.id
                async def reply_text(self, text, **kw):
                    return await client.send_message(user_id, text, **kw)
            from plugins.userbot.market_seller import _process_start
            return await _process_start(client, MockMsg())
            
        await _edit_main_menu_in_place(client, query, query.from_user, new_lang)

    # ── Story preview Continue button ──
    elif cmd.startswith("story_preview_continue_"):
        s_id = cmd.replace("story_preview_continue_", "")
        await query.answer()
        await query.message.delete()
        # Check if T&C is globally disabled by admin
        _tnc_cfg0 = await _get_cached_features()
        if not _tnc_cfg0.get("tnc_enabled", True):
            from bson.objectid import ObjectId as _ObjId0
            _s0 = await db.db.premium_stories.find_one({"_id": _ObjId0(s_id)})
            if _s0:
                _bt0 = await _get_cached_bot_doc(client.me.id)
                _bt_cfg0 = (_bt0 or {}).get("config", {})
                return await _show_story_details(client, query, _s0, lang, bot_cfg=_bt_cfg0)
        return await _show_tc(client, user_id, s_id, lang, from_user=query.from_user)

    # ── T&C Accept ──
    elif cmd.startswith("tc_iaadnsa_"):
        s_id = cmd.replace("tc_iaadnsa_", "")
        await db.update_user(user_id, {"tc_accepted": True, "tc_bypassed": True})
        await query.answer("Terms Accepted & Bypassed for Future!")
        
        from utils import log_arya_event
        user_obj = query.from_user
        from bson.objectid import ObjectId
        story = await db.db.premium_stories.find_one({"_id": ObjectId(s_id)})
        s_name = story.get(f'story_name_{lang}', story.get('story_name_en', 'Unknown')) if story else 'Unknown'

        asyncio.create_task(log_arya_event(
            event_type="T&C ACCEPTED (IAADNSA)",
            user_id=user_id,
            user_info={"first_name": user_obj.first_name or "Unknown", "last_name": user_obj.last_name or "", "username": user_obj.username or ""},
            details=f"User accepted the Terms & Conditions and selected IAADNSA to skip it for future purchases (Story: {s_name})."
        ))

        if story:
            _bt = await _get_cached_bot_doc(client.me.id)
            _bt_cfg = (_bt or {}).get("config", {})
            return await _show_story_details(client, query, story, lang, bot_cfg=_bt_cfg)

    elif cmd.startswith("tc_accept_"):
        s_id = cmd.replace("tc_accept_", "")
        await db.update_user(user_id, {"tc_accepted": True})
        await query.answer("Terms Accepted!")
        
        from utils import log_arya_event
        user_obj = query.from_user
        asyncio.create_task(log_arya_event(
            event_type="T&C ACCEPTED",
            user_id=user_id,
            user_info={"first_name": user_obj.first_name if user_obj else "Unknown", "last_name": user_obj.last_name if user_obj else "", "username": user_obj.username if user_obj else ""},
            details=f"User accepted the Terms & Conditions."
        ))

        from bson.objectid import ObjectId
        story = await db.db.premium_stories.find_one({"_id": ObjectId(s_id)})
        if story:
            _bt = await _get_cached_bot_doc(client.me.id)
            _bt_cfg = (_bt or {}).get("config", {})
            return await _show_story_details(client, query, story, lang, bot_cfg=_bt_cfg)

    elif cmd == "feedback_start":

        await query.answer()

        if lang == 'hi':

            fb_txt = (

                f"<b>⟦ {_sc('फीडबैक / सुझाव')} ⟧</b>\n\n"

                f"<blockquote expandable>अपनी राय, सुझाव या शिकायत नीचे लिखें।\n"

                f"आप <b>टेक्स्ट, फोटो या विडियो</b> भेज सकते हैं। /cancel कर निरस्त करें।</blockquote>"

            )

        else:

            fb_txt = (

                f"<b>⟦ {_sc('FEEDBACK / SUGGESTIONS')} ⟧</b>\n\n"

                f"<blockquote expandable>Share your <b>feedback, suggestions or complaints</b> below.\n"

                f"You can send <b>text, photos, or videos</b>. Type /cancel to abort.</blockquote>"

            )

        await db.update_user(user_id, {"state": "feedback_pending"})

        await _safe_edit(query.message, text=fb_txt, markup=InlineKeyboardMarkup([

            [InlineKeyboardButton(f"« {_sc('BACK')}", callback_data="mb#main_help")]

        ]))



    elif cmd == "help_tc":

        await query.answer()

        # Synchronized with updated T&C from purchase flow

        if lang == 'hi':

            tc_text = (

                f"<b>⟦ नियम और शर्तें ⟧</b>\n\n"

                f"खरीदने से पहले, कृपया निम्नलिखित पढ़ें और सहमत हों:\n\n"

                f"<blockquote expandable>"

                f"• <b>गायब एपिसोड</b>\n"

                f"सार्वजनिक रूप से जारी न होने पर 3-4 एपिसोड अनुपलब्ध हो सकते हैं। ऐसा होने पर हम अपनी तरफ से कहानी की कीमत कम रखते हैं। यदि वे एपिसोड हमें बाद में मिलते हैं, तो उन्हें आपके वर्तमान एपिसोड्स में जोड़ दिया जाएगा। यदि 4 से अधिक एपिसोड गायब हैं, तो कृपया सपोर्ट से संपर्क करें।\n"

                f"</blockquote>\n"

                f"<blockquote expandable>"

                f"• <b>क्वालिटी</b>\n"

                f"पुराने एपिसोड्स की क्वालिटी कम हो सकती है। हम 100% क्वालिटी की गारंटी नहीं दे सकते, लेकिन हमेशा सर्वश्रेष्ठ वर्जन प्रदान करेंगे।\n"

                f"</blockquote>\n"

                f"<blockquote expandable>"

                f"• <b>कोई रिफंड नहीं</b>\n"

                f"एक बार भुगतान हो जाने और डिलीवरी शुरू होने के बाद कोई रिफंड नहीं दिया जाएगा। यदि आपका टेलीग्राम अकाउंट सस्पेंड या डिलीट हो जाता है, तो पूर्ण विवरण और प्रमाण के साथ एडमिन से संपर्क करें, आपको पुनः जोड़ दिया जाएगा। यदि आप गलती से अतिरिक्त राशि का भुगतान कर देते हैं, तो तुरंत प्रमाण के साथ संपर्क करें, आपको रिफंड मिल जाएगा (Razorpay पर प्लेटफ़ॉर्म फीस काटी जाएगी)।\n"

                f"</blockquote>\n"

                f"<blockquote expandable>"

                f"• <b>नकली स्क्रीनशॉट</b>\n"

                f"नकली या अमान्य भुगतान प्रमाण भेजने पर स्थायी रूप से प्रतिबंध लगा दिया जाएगा।\n"

                f"</blockquote>"

            )

        else:

            tc_text = (

                f"<b>⟦ 𝗧𝗘𝗥𝗠𝗦 & 𝗖𝗢𝗡𝗗𝗜𝗧𝗜𝗢𝗡𝗦 ⟧</b>\n\n"

                f"𝖡𝖾𝖿𝗈𝗋𝖾 𝗉𝗎𝗋𝖼𝗁𝖺𝗌𝗂𝗇𝗀, 𝗉𝗅𝖾𝖺𝗌𝖾 𝗋𝖾𝖺𝖽 𝖺𝗇𝖽 𝖺𝗀𝗋𝖾𝖾 𝗍𝗈 𝗍𝗁𝖾 𝖿𝗈𝗅𝗅𝗈𝗐𝗂𝗇𝗀:\n\n"

                f"<blockquote expandable>"

                f"• <b>𝗠𝗶𝘀𝘀𝗶𝗻𝗴 𝗘𝗽𝗶𝘀𝗼𝗱𝗲𝘀</b>\n"

                f"3-4 episodes may be missing if not publicly released. In such cases, we keep the story price lower from our side. If we find those episodes later, they will be automatically added to your current episodes. If more than 4 episodes are missing, please contact support.\n"

                f"</blockquote>\n"

                f"<blockquote expandable>"

                f"• <b>𝗤𝘂𝗮𝗹𝗶𝘁𝘆</b>\n"

                f"𝖲𝗈𝗆𝖾 𝗈𝗅𝖽𝖾𝗋 𝖾𝗉𝗂𝗌𝗈𝖽𝖾𝗌 𝗆𝖺𝗒 𝗁𝖺𝗏𝖾 𝗋𝖾𝖽𝗎𝖼𝖾𝖽 𝗊𝗎𝖺𝗅𝗂𝗍𝗒.\n"

                f"𝖶𝖾 𝖼𝖺𝗇𝗇𝗈𝗍 𝗀𝗎𝖺𝗋𝖺𝗇𝗍𝖾𝖾 𝟣𝟢𝟢% 𝗊𝗎𝖺𝗅𝗂𝗍𝗒, 𝖻𝗎𝗍 𝖺𝗅𝗐𝖺𝗒𝗌 𝗉𝗋𝗈𝗏𝗂𝖽𝖾 𝖻𝖾𝗌𝗍 𝗏𝖾𝗋𝗌𝗂𝗈𝗇.\n"

                f"</blockquote>\n"

                f"<blockquote expandable>"

                f"• <b>𝗡𝗼 𝗥𝗲𝗳𝘂𝗻𝗱𝘀</b>\n"

                f"No refunds once payment is confirmed and delivery starts. If your Telegram account gets suspended or deleted, contact the admin with complete details and proof to be added again. If you accidentally pay an extra amount, contact us immediately with proof for a refund (Razorpay platform fees will be deducted).\n"

                f"</blockquote>\n"

                f"<blockquote expandable>"

                f"• <b>𝗙𝗮𝗸𝗲 𝗦𝗰𝗿𝗲𝗲𝗻𝘀𝗵𝗼𝘁𝘀</b>\n"

                f"𝖥𝖺𝗄𝖾 𝗈𝗋 𝗂𝗇𝗏𝖺𝗅𝗂𝖽 𝗉𝖺𝗒𝗆𝖾𝗇𝗍 𝗉𝗋𝗈𝗈𝖿𝗌 𝗐𝗂𝗅𝗅 𝗅𝖾𝖺𝖽 𝗍𝗈 𝗉𝖾𝗋𝗆𝖺𝗇𝖾𝗇𝗍 𝖻𝖺𝗇.\n"

                f"</blockquote>"

            )

        kb = [[InlineKeyboardButton(f"« ❮ {_sc('BACK')}", callback_data="mb#main_help")]]

        await query.message.edit_text(tc_text, reply_markup=InlineKeyboardMarkup(kb))

    elif cmd == "help_refund":

        await query.answer()

        refund_text = (

            f"<b>🔁 {_sc('REFUND POLICY')}</b>\n\n"

            + _sc("If you paid an incorrect/extra amount or did not receive the story, you may be eligible for a refund after verification.") + "\n\n"

            "<blockquote expandable>"

            + "⚠️ " + _sc("IMPORTANT") + "\n"

            + _sc("WE STORE ALL DATA: YOUR PROFILE, PAYMENT HISTORY, WHICH STORY YOU PAID FOR, HOW MUCH YOU PAID, TO WHOM IT WAS PAID, WHETHER YOU RECEIVED THE CHANNEL LINK OR DELIVERY, AND HOW MANY EPISODES WERE DELIVERED.") + "\n\n"

            + _sc("REFUND REQUESTS ARE REVIEWED INDIVIDUALLY AND PROCESSED AFTER FULL VERIFICATION. MISUSE OF REFUND REQUESTS WILL RESULT IN A BAN.")

            + "</blockquote>"

        )

        kb = [[InlineKeyboardButton(f"« ❮ {_sc('BACK')}", callback_data="mb#main_help")]]

        await query.message.edit_text(refund_text, reply_markup=InlineKeyboardMarkup(kb))



    # ── T&C Reject ──

    elif cmd == "tc_reject":

        await query.answer("Cancelled.", show_alert=False)

        

        from utils import log_arya_event

        user_obj = await get_robust_user(client, user_id)

        asyncio.create_task(log_arya_event(

            event_type="T&C REJECTED",

            user_id=user_id,

            user_info={"first_name": user_obj.first_name if user_obj else "Unknown", "last_name": user_obj.last_name if user_obj else "", "username": user_obj.username if user_obj else ""},

            details=f"User rejected the Terms & Conditions and aborted the purchase."

        ))



        await query.message.edit_text(

            "<b>❌ Purchase Cancelled</b>\n\n<i>You have rejected the Terms & Conditions. You can start over anytime from the Marketplace.</i>",

            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"❮ {_sc('MAIN MENU')}", callback_data="mb#main_back")]])

        )



    # ── Back (inline) ──

    elif cmd.startswith("show_tc#"):

        s_id = cmd.split("#")[1]

        

        u = await db.get_user(user_id, from_user=query.from_user)

        if u.get("tc_bypassed", False):

            from bson.objectid import ObjectId

            story = await db.db.premium_stories.find_one({"_id": ObjectId(s_id)})

            if story:

                _bt = await db.db.premium_bots.find_one({"id": client.me.id})

                _bt_cfg = (_bt or {}).get("config", {})

                await query.answer()



                from utils import log_arya_event

                user_obj = query.from_user

                s_name = story.get(f'story_name_{lang}', story.get('story_name_en', 'Unknown'))

                asyncio.create_task(log_arya_event(

                    event_type="T&C BYPASSED",

                    user_id=user_id,

                    user_info={"first_name": user_obj.first_name or "Unknown", "last_name": user_obj.last_name or "", "username": user_obj.username or ""},

                    details=f"User bypassed the Terms & Conditions page using IAADNSA flag while purchasing '{s_name}'."

                ))



                return await _show_story_details(client, query, story, lang, bot_cfg=_bt_cfg)



        try:

            await query.message.delete()

        except:

            pass

        # Check if T&C is globally disabled by admin

        _tnc_cfg2 = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}

        if not _tnc_cfg2.get("tnc_enabled", True):

            from bson.objectid import ObjectId as _ObjId2

            _s2 = await db.db.premium_stories.find_one({"_id": _ObjId2(s_id)})

            if _s2:

                _bt2 = await db.db.premium_bots.find_one({"id": client.me.id})

                _bt_cfg2 = (_bt2 or {}).get("config", {})

                return await _show_story_details(client, query, _s2, lang, bot_cfg=_bt_cfg2)

        return await _show_tc(client, user_id, s_id, lang)



    elif cmd == "back":

        await query.message.delete()



    # ── View Purchased Story Details ──
    elif cmd.startswith("purchased_view_"):
        raw_target = data[2] if len(data) > 2 else cmd.replace("purchased_view_", "")
        await query.answer()

        from bson.objectid import ObjectId
        from bson.errors import InvalidId

        target_parts = raw_target.split("_", 1)
        s_id = target_parts[0]
        part_id = target_parts[1] if len(target_parts) > 1 else None

        story = None
        try:
            story = await db.db.premium_stories.find_one({"_id": ObjectId(s_id)})
        except Exception:
            pass
        if not story:
            story = await db.db.premium_stories.find_one({"_id": s_id})
        if not story:
            story = await db.db.premium_stories.find_one({"story_id": s_id})

        if story:
            part_doc = None
            if part_id:
                for p in (story.get("parts") or []):
                    if str(p.get("id")) == str(part_id):
                        part_doc = p
                        break

            # If part_id not in callback, check if user's order was for a specific part
            if not part_doc:
                uid_int = int(user_id) if str(user_id).isdigit() else user_id
                user_part_order = await db.db.orders.find_one({
                    "user_id": {"$in": [uid_int, str(user_id)]},
                    "status": {"$in": ["paid", "delivered", "approved", "completed", "success"]},
                    "items": {"$elemMatch": {"story_id": str(story["_id"]), "part_id": {"$ne": None}}}
                }, sort=[("created_at", -1)])
                if user_part_order and user_part_order.get("items"):
                    for itm in user_part_order["items"]:
                        if (str(itm.get("story_id")) == str(story["_id"]) or str(itm.get("id")) == str(story["_id"])) and itm.get("part_id"):
                            part_id = str(itm["part_id"])
                            for p in (story.get("parts") or []):
                                if str(p.get("id")) == str(part_id):
                                    part_doc = p
                                    break
                            if part_doc:
                                break

            s_name = story.get(f'story_name_{lang}', story.get('story_name_en'))
            if part_doc:
                p_label = part_doc.get("name", f"Part {part_id}")
                s_name = f"{s_name} · {p_label}"
                ep_range = part_doc.get("episodes", "")
                is_ong = bool(part_doc.get("is_ongoing") or str(part_doc.get("badge") or "").lower() == "ongoing")
                start_p = int(part_doc.get("start_id", 0))
                end_p = int(part_doc.get("end_id", 0))
                story_end_id = int(story.get("end_id") or story.get("end_message_id") or 0)
                if is_ong and story_end_id > end_p:
                    end_p = story_end_id
                    story_eps = str(story.get("episodes") or "").strip()
                    if story_eps and story_eps.isdigit():
                        import re
                        m = re.search(r"(\d+)", str(ep_range))
                        if m:
                            ep_range = f"{m.group(1)}-{story_eps}"
                        elif not ep_range:
                            ep_range = story_eps
                episodes_display = ep_range if ep_range else "N/A"
                part_files = [mid for mid in story.get("valid_file_ids", []) if start_p <= mid <= end_p] if (start_p and end_p and story.get("valid_file_ids")) else []
                ep_count = len(part_files) if part_files else ((end_p - start_p) + 1 if (start_p and end_p) else "?")
                access_cb = f"mb#access_{s_id}_{part_id}"
            else:
                episodes_display = story.get('episodes', 'N/A')
                ep_count = story.get('file_count') or (len(story.get('valid_file_ids')) if story.get('valid_file_ids') else None) or (abs(story.get('end_id', 0) - story.get('start_id', 0)) + 1 if story.get('end_id') else "?")
                access_cb = f"mb#access_{s_id}"

            # Query orders collection first for accurate payment method and price
            uid_int = int(user_id) if str(user_id).isdigit() else user_id
            user_order = await db.db.orders.find_one({
                "user_id": {"$in": [uid_int, str(user_id)]},
                "status": {"$in": ["paid", "delivered", "approved", "completed", "success"]},
                "$or": [
                    {"items.story_id": str(story["_id"])},
                    {"story_ids": str(story["_id"])},
                    {"story_id": str(story["_id"])}
                ]
            }, sort=[("created_at", -1)])

            payment_label = "Verified"
            if user_order:
                src = str(user_order.get("source", user_order.get("method", user_order.get("gateway", "UPI")))).lower()
                amount_paid = user_order.get("total", user_order.get("amount", (part_doc.get("price") if part_doc else story.get("price", 0))))
                payment_label = {
                    "razorpay":   f"Razorpay (₹{amount_paid})",
                    "cashfree":   f"Cashfree (₹{amount_paid})",
                    "easebuzz":   f"Easebuzz (₹{amount_paid})",
                    "upi":        f"Manual UPI (₹{amount_paid})",
                    "manual_upi": f"Manual UPI (₹{amount_paid})",
                    "crypto":     f"Crypto (₹{amount_paid})",
                    "oxapay":     f"Crypto (₹{amount_paid})",
                }.get(src, f"{src.capitalize()} (₹{amount_paid})")
            else:
                purchase = await db.db.premium_purchases.find_one({"user_id": uid_int, "story_id": story.get("_id")})
                if purchase:
                    src = str(purchase.get("source", "manual")).lower()
                    amount_paid = purchase.get("amount", (part_doc.get("price") if part_doc else story.get('price', 0)))
                    payment_label = {
                        "razorpay":   f"Razorpay (₹{amount_paid})",
                        "cashfree":   f"Cashfree (₹{amount_paid})",
                        "easebuzz":   f"Easebuzz (₹{amount_paid})",
                        "upi":        f"Manual UPI (₹{amount_paid})",
                        "manual_upi": f"Manual UPI (₹{amount_paid})",
                        "crypto":     f"Crypto (₹{amount_paid})",
                        "oxapay":     f"Crypto (₹{amount_paid})",
                    }.get(src, f"{src.capitalize()} (₹{amount_paid})")

            if lang == 'hi':
                txt_req = (
                    "<b>⟦ स्टोरी विवरण ⟧</b>\n\n"
                    f"<b>{s_name}</b>\n\n"
                    "──────────────\n"
                    f"<b>प्लेटफॉर्म  ⟶</b> {story.get('platform', 'अन्य')}\n"
                    f"<b>एपिसोड्स   ⟶</b> {episodes_display}\n"
                    f"<b>फाइलें     ⟶</b> {ep_count}\n"
                    f"<b>स्थिति     ⟶</b> आपकी अपनी (Owned)\n"
                    f"<b>पेमेंट      ⟶</b> {payment_label}\n"
                    "──────────────\n"
                    'अपनी फाइलें प्राप्त करने के लिए नीचे टैप करें। <emoji id="6147439566107186310">👇</emoji>'
                )
                kb = [
                    [_ikb("डिलीवरी प्राप्त करें", callback_data=access_cb, icon_custom_emoji_id="6024030612933844303")],
                    [InlineKeyboardButton("« मेरी स्टोरीज पर वापस", callback_data="mb#my_buys")]
                ]
            else:
                txt_req = (
                    "<b>⟦ 𝗦𝗧𝗢𝗥𝗬 𝗠𝗘𝗧𝗔 ⟧</b>\n\n"
                    f"<b>{s_name}</b>\n\n"
                    "──────────────\n"
                    f"<b>ᴘʟᴀᴛꜰᴏʀᴍ ⟶</b> {story.get('platform', 'Other')}\n"
                    f"<b>ᴇᴘɪꜱᴏᴅᴇꜱ ⟶</b> {episodes_display}\n"
                    f"<b>ꜰɪʟᴇꜱ    ⟶</b> {ep_count}\n"
                    f"<b>ꜱᴛᴀᴛᴜꜱ   ⟶</b> ᴏᴡɴᴇᴅ\n"
                    f"<b>ᴘᴀʏᴍᴇɴᴛ  ⟶</b> {payment_label}\n"
                    "──────────────\n"
                    '𝖳𝖺𝗉 𝖻𝖾𝗅𝗈𝗐 𝗍𝗈 𝗋𝖾𝖼𝖾𝗂𝗏𝖾 𝗒𝗈𝗎𝗋 𝖿𝗂𝗅𝖾𝗌. <emoji id="6147439566107186310">👇</emoji>'
                )
                kb = [
                    [_ikb("Get Delivery", callback_data=access_cb, icon_custom_emoji_id="6024030612933844303")],
                    [InlineKeyboardButton(_bs("Back to My Stories"), callback_data="mb#my_buys")]
                ]

            await _safe_edit(query.message, text=txt_req, markup=InlineKeyboardMarkup(kb))

    # ── Marketplace Inline Navigation ──
    elif cmd == "mkt_plat":
        plat_name = data[2] if len(data) > 2 else "Pocket FM"
        page = int(data[3]) if len(data) > 3 else 0
        return await _show_marketplace_stories(client, query, plat_name, page, lang)

    elif cmd == "mkt_all":
        plat_name = data[2] if len(data) > 2 else "Pocket FM"
        page = int(data[3]) if len(data) > 3 else 0
        return await _show_marketplace_all_stories(client, query, plat_name, page, lang)

    elif cmd in ("mkt_search", "main_search_story"):
        await db.update_user(user_id, {"state": "searching"})
        s_title = _sc("SEARCH STORY") if lang == 'en' else "स्टोरी खोजें"
        s_prompt = _sc("Please type the story name or keywords in the chat below:") if lang == 'en' else "कृपया नीचे चैट में कहानी का नाम या कीवर्ड टाइप करें:"
        c_label = _sc("CANCEL") if lang == 'en' else "रद्द करें"
        back_cb = "mb#main_back" if cmd == "main_search_story" else "mb#main_marketplace"
        txt = (
            f'<b><emoji id="5258274739041883702">🔍</emoji> {s_title}</b>\n\n'
            f'<i>{s_prompt}</i>'
        )
        kb = [[InlineKeyboardButton(f"« ❮ {c_label}", callback_data=back_cb)]]
        return await _safe_edit(query.message, text=txt, markup=InlineKeyboardMarkup(kb))

    # ── Access purchased story directly ──
    elif cmd.startswith("access_"):
        raw_target = data[2] if len(data) > 2 else cmd.replace("access_", "")
        await query.answer()

        from bson.objectid import ObjectId
        from bson.errors import InvalidId

        target_parts = raw_target.split("_", 1)
        s_id = target_parts[0]
        part_id = target_parts[1] if len(target_parts) > 1 else None

        story = None
        try:
            story = await db.db.premium_stories.find_one({"_id": ObjectId(s_id)})
        except Exception:
            pass
        if not story:
            story = await db.db.premium_stories.find_one({"_id": s_id})
        if not story:
            story = await db.db.premium_stories.find_one({"story_id": s_id})

        if story:
            part_doc = None
            if part_id:
                for p in (story.get("parts") or []):
                    if str(p.get("id")) == str(part_id):
                        part_doc = p
                        break

            # Delete message to pop a new dialogue
            try: await query.message.delete()
            except: pass

            return await dispatch_delivery_choice(client, user_id, story, part_info=part_doc)



    # ── View story (from inline button if any) ──

    elif cmd.startswith("view_") or cmd == "view":

        from bson.objectid import ObjectId

        s_id = data[2] if cmd == "view" else cmd.replace("view_", "")

        story = await db.db.premium_stories.find_one({"_id": ObjectId(s_id)})

        if not story: return await query.answer("Story not found!", show_alert=True)



        has_paid = await db.has_purchase(user_id, s_id)

        if has_paid:

            await query.answer("You already own this!", show_alert=True)

            return await dispatch_delivery_choice(client, user_id, story)



        await query.message.delete()

        # Show profile/description card first (with expandable quote), then user can continue to T&C.

        return await _show_story_profile(client, user_id, story, lang)



    # ── Pay ──

    elif cmd == "pay":

        method = data[2]

        s_id = data[3]



        from bson.objectid import ObjectId

        story = await db.db.premium_stories.find_one({"_id": ObjectId(s_id)})

        if not story: return await query.answer("Story not found!", show_alert=True)



        # Block UPI if time-restricted or admin has disabled it per-bot

        if method == "cashfree":
            # Cashfree Payment Gateway order flow
            logger.info(f"[PAY2] User {user_id} clicked Cashfree / Cards / NetBanking option for story {s_id}")
            try:
                await query.answer()
            except Exception:
                pass

            from cashfree_helper import create_cashfree_order
            bot_username = getattr(getattr(client, "me", None), "username", "")
            user_name = query.from_user.first_name or "Buyer"
            
            cf_res = await create_cashfree_order(user_id=user_id, user_name=user_name, story=story, bot_username=bot_username)
            
            order_id = cf_res["order_id"]
            pay_link = cf_res.get("payment_link")
            s_name = story.get(f'story_name_{lang}', story.get('story_name_en', 'Story'))
            price = story.get('price', 0)

            if not pay_link:
                logger.error(f"[CF] pay_link is None/empty! cf_res={cf_res}")
                return await query.answer("❌ Payment link not generated. Please try again.", show_alert=True)

            logger.info(f"[CF] Showing payment screen to user {user_id}: order={order_id}, link={pay_link}")

            desc_cf = (
                f"<b>⟦ 💳 CASHFREE PAYMENT ⟧</b>\n\n"
                f"<b>• Story:</b> {to_mathbold(s_name)}\n"
                f"<b>• Amount:</b> ₹{price}\n"
                f"<b>• Order ID:</b> <code>{order_id}</code>\n\n"
                f"<i>Tap <b>Pay Now</b> below to pay securely via Credit/Debit Cards, NetBanking, or UPI.</i>\n\n"
                f"<i>After payment, tap <b>Check Status</b> for instant delivery.</i>"
            ) if lang == 'en' else (
                f"<b>⟦ 💳 कैशफ्री भुगतान ⟧</b>\n\n"
                f"<b>• कहानी:</b> {to_mathbold(s_name)}\n"
                f"<b>• राशि:</b> ₹{price}\n"
                f"<b>• ऑर्डर आईडी:</b> <code>{order_id}</code>\n\n"
                f"<i>Cards, NetBanking या UPI से भुगतान के लिए <b>Pay Now</b> दबाएं।</i>\n\n"
                f"<i>भुगतान के बाद <b>Check Status</b> दबाएं।</i>"
            )

            pay_now_lbl = "💳 Pay Now (Cards / NetBanking / UPI)" if lang == 'en' else "💳 अभी भुगतान करें"
            check_lbl = "🔄 Check Payment Status" if lang == 'en' else "🔄 स्टेटस चेक करें"
            back_lbl = "« Back" if lang == 'en' else "« वापस"

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(pay_now_lbl, url=pay_link)],
                [InlineKeyboardButton(check_lbl, callback_data=f"mb#cf_status#{order_id}#{s_id}")],
                [InlineKeyboardButton(back_lbl, callback_data=f"mb#pay_back#{s_id}")]
            ])

            try:
                await query.message.edit_text(desc_cf, reply_markup=kb, parse_mode=enums.ParseMode.HTML)
            except Exception as ex:
                logger.error(f"[CF] edit_text failed: {ex} — trying send_message fallback")
                try:
                    await client.send_message(query.message.chat.id, desc_cf, reply_markup=kb, parse_mode=enums.ParseMode.HTML)
                except Exception as ex2:
                    logger.error(f"[CF] send_message also failed: {ex2}")
                    await query.answer(f"✅ Order created! Order ID: {order_id}\n\nPay here: {pay_link}", show_alert=True)
            return

        elif method == "upi":

            bt_cfg = (await db.db.premium_bots.find_one({"id": client.me.id}) or {}).get("config", {})

            upi_status = _upi_availability(bt_cfg)

            # Also check story-level payment method restriction

            story_methods = story.get("payment_methods", ["upi", "razorpay"])

            upi_in_story = "upi" in story_methods

            if not upi_status["available"] or not upi_in_story:

                rzp_kb = [

                    [InlineKeyboardButton(f"💳 {_sc('PAY VIA RAZORPAY')}", callback_data=f"mb#pay#razorpay#{s_id}")],

                    [InlineKeyboardButton(f"❮ {_sc('BACK')}", callback_data="mb#return_main")]

                ]

                return await query.message.edit_text(

                    f"<b>🚫 {_sc('UPI UNAVAILABLE')}</b>\n\n"

                    f"<i>{_sc('Manual UPI payments are not available between 9 PM and 6 AM IST.')}</i>\n"

                    f"<i>{_sc('Manual verification is not active during this time.')}</i>\n\n"

                    f"{_sc('Please use')} <b>Razorpay</b> {_sc('for instant automatic payment and immediate delivery.')}\n"

                    f"{_sc('Supports UPI, Card, Net Banking, Wallets & more.')}",

                    reply_markup=InlineKeyboardMarkup(rzp_kb)

                )



        if method in ["razorpay", "easebuzz"]:

            await query.message.edit_text(f"🔐 <b>{_sc('PREPARING YOUR SECURE CHECKOUT')}...</b>\n<i>{_sc('Please wait a moment while we connect to the gateway.')}</i>")

            import time

            ref_id = f"st_{user_id}_{int(time.time())}"

            price = int(story["price"])

            desc = f"Story: {story.get('story_name_en', 'Premium Content')}"

            

            pl_id = None

            url = None

            if method == "razorpay":

                url, pl_id = await _create_rzp_link(price, desc, ref_id, user.get('first_name', "User"))

            else:

                url, pl_id = await _create_easebuzz_link(price, desc, ref_id, user.get('first_name', "User"))

            

            if url and method == "razorpay":

                try:

                    from utils import log_arya_event

                    u_info = {"first_name": user.get('first_name', ''), "last_name": user.get('last_name', ''), "username": user.get('username', '')}

                    asyncio.create_task(log_arya_event("PAYMENT LINK GENERATED", user_id, u_info, f"Generated Razorpay link for story: {desc}\nAmount: {price}\nLink: {url}"))

                except: pass

            if not url or (method == "razorpay" and not url.startswith("http")):

                empty_key_msg = f"{method.capitalize()} API keys not found in .env!"

                err_msg = pl_id if pl_id else empty_key_msg

                return await query.message.edit_text(f"❌ Could not generate API link for <b>{method}</b>.\n\n<code>{err_msg}</code>\n\nPlease check your .env configuration. For now, try Manual UPI.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"❮ {_sc('BACK')}", callback_data="mb#main_back")]]))



            await db.db.premium_checkout.update_one(

                {"user_id": user_id, "bot_id": client.me.id, "story_id": ObjectId(s_id)},

                {"$set": {

                    "status": "pending_gateway",

                    "bot_username": client.me.username,

                    "username": query.from_user.username or "",

                    "first_name": query.from_user.first_name or "",

                    "method": method,

                    "payment_id": pl_id,

                    "amount": price,

                    "pay_link_copy": url,

                    "updated_at": datetime.utcnow(),

                }, "$setOnInsert": {"created_at": datetime.utcnow()}},

                upsert=True

            )

            

            lbl_pay = "सुरक्षित भुगतान करें" if lang == 'hi' else f"Pay securely via {method.title()}"
            lbl_ver = "भुगतान सत्यापित करें" if lang == 'hi' else "Verify Payment"
            lbl_bck = "‹ वापस" if lang == 'hi' else "‹ Back"

            kb = [
                [_ikb(lbl_pay, url=url, icon_custom_emoji_id="6030410254276106984")],
                [_ikb(lbl_ver, callback_data=f"mb#{method}_check#{s_id}", icon_custom_emoji_id="5807492110059838726")],
                [InlineKeyboardButton(lbl_bck, callback_data="mb#return_main")]
            ]

            if lang == "hi":
                check_txt = (
                    f'<emoji id="5472030678633684592">💸</emoji> <b>सुरक्षित चेकआउट</b>\n\n'
                    f'<b><emoji id="6023962911364357003">📖</emoji> कहानी:</b> <code>{story.get("story_name_en", "Premium Story")}</code>\n'
                    f'<b><emoji id="5283232570660634549">💰</emoji> मूल्य:</b> <code>₹{price}</code>\n\n'
                    f"<blockquote expandable>"
                    f'<b><emoji id="6019328362479097179">🛡</emoji> {method.title()} से भुगतान कैसे करें?</b>\n\n'
                    f"1. नीचे दिए गए '{lbl_pay}' बटन पर क्लिक करें।\n"
                    f"2. आपको सुरक्षित पेमेंट गेटवे पर भेजा जाएगा।\n"
                    f"3. UPI या किसी भी माध्यम से भुगतान पूरा करें।\n"
                    f"4. भुगतान सफल होने के बाद वापस आकर '{lbl_ver}' पर क्लिक करें।\n"
                    f"5. बॉट तुरंत सत्यापित करके फाइल भेज देगा।"
                    f"</blockquote>\n\n"
                    f'<i><emoji id="6023761060786346622">⚡</emoji> त्वरित सत्यापन और फ़ाइल वितरण।</i>'
                )
            else:
                check_txt = (
                    f'<emoji id="5472030678633684592">💸</emoji> <b>SECURE CHECKOUT</b>\n\n'
                    f'<b><emoji id="6023962911364357003">📖</emoji> Story Name:</b> <code>{story.get("story_name_en", "Premium Story")}</code>\n'
                    f'<b><emoji id="5283232570660634549">💰</emoji> Total Price:</b> <code>₹{price}</code>\n\n'
                    f"<blockquote expandable>"
                    f'<b><emoji id="6019328362479097179">🛡</emoji> How to pay via {method.title()}:</b>\n\n'
                    f"1. Click the '{lbl_pay}' button below.\n"
                    f"2. You will be redirected to the secure payment gateway.\n"
                    f"3. Complete your payment using UPI, Card, or Netbanking.\n"
                    f"4. Come back to this chat and click '{lbl_ver}'.\n"
                    f"5. The bot will automatically verify and provide access instantly."
                    f"</blockquote>\n\n"
                    f'<i><emoji id="6023761060786346622">⚡</emoji> Instant automated verification & delivery.</i>'
                )

                

            await query.message.edit_text(check_txt, reply_markup=InlineKeyboardMarkup(kb))



        elif method == "upi":
            if getattr(db, "db", None) is None and hasattr(db, "connect"):
                await db.connect()
            db_obj = getattr(db, "db", None)
            bt = (await db_obj.premium_bots.find_one({"id": client.me.id})) if db_obj is not None else None
            bt_cfg = bt.get("config", {}) if bt else {}
            upi_id, p_name = await _get_rotated_upi(bt_cfg)

            s_price = str(story["price"])

            s_name = story.get(f'story_name_{lang}', story.get('story_name_en', 'Story'))

            

            # Generate Premium UPI Card

            qr_card = None

            try:

                qr_card = generate_upi_card(upi_id, s_price, s_name, payee_name=p_name)

            except Exception as e:

                logger.error(f"UPI Card generation failed: {e}")

                qr_card = None



            upi_uri = _build_upi_uri(

                upi_id=upi_id,

                payee_name=p_name,

                amount=int(story["price"]),

                note=f"Payment for {s_name[:20]}"

            )



            slice_api_url = (getattr(Config, "SLICEURL_API_URL", "") or "").strip()

            slice_api_key = (getattr(Config, "SLICEURL_API_KEY", "") or "").strip()

            slice_direct = ""

            if slice_api_url and slice_api_key.startswith("slc_"):

                slice_direct = await _sliceurl_api_shorten(upi_uri)

            button_url = slice_direct



            await db.db.premium_checkout.update_one(

                {"user_id": user_id, "bot_id": client.me.id, "story_id": ObjectId(s_id)},

                {"$set": {

                    "status": "pending_gateway",

                    "bot_username": client.me.username,

                    "username": query.from_user.username or "",

                    "first_name": query.from_user.first_name or "",

                    "method": "upi",

                    "amount": int(story["price"]),

                    "upi_uri": upi_uri,

                    "pay_link_copy": button_url,

                    "upi_id_shown": upi_id,

                    "upi_payee_name_shown": p_name,

                    "updated_at": datetime.utcnow(),

                }, "$setOnInsert": {"created_at": datetime.utcnow()}},

                upsert=True

            )



            p_name = (bt_cfg.get("upi_name") or "Merchant").strip()

            

            if lang == 'hi':

                txt = (

                    f"<b>⟦ {_sc('पेमेंट पूरा करें')} ⟧</b>\n\n"

                    f"<b>स्टेप 𝟷: ₹{s_price} का भुगतान करें</b>\n\n"

                    f"<blockquote>• QR कोड स्कैन करें या नीचे दिए गए विवरण का उपयोग करें:</blockquote>\n"

                    f"<blockquote><b>UPI ID:</b> <code>{upi_id}</code>\n"

                    f"<b>नाम:</b> <code>{p_name}</code>\n"

                    f"<b>राशि:</b> <code>₹{s_price}</code></blockquote>\n\n"

                    f"• सुनिश्चित करें कि राशि सही भरी गई है।\n\n"

                    f"<b>स्टेप य: भुगतान का सत्यापन</b>\n\n"

                    f"• भुगतान के बाद, अपना स्क्रीनशॉट अपलोड करने के लिए <b>पेमेंट हो गया</b> पर क्लिक करें।\n"

                    f"────────────────────"

                )

                kb = [

                    [InlineKeyboardButton("☑️ पेमेंट हो गया", callback_data=f"mb#upi_done#{s_id}")],

                    [InlineKeyboardButton("« ❮ वापस", callback_data=f"mb#pay_back#{s_id}")]

                ]

            else:

                txt = (

                    f"<b>⟦ {_sc('COMPLETE PAYMENT')} ⟧</b>\n\n"

                    f"<b>𝚂𝚝𝚎𝚙 𝟷: Pay ₹{s_price}</b>\n\n"

                    f"<blockquote>• Scan the QR code or pay using the details below:</blockquote>\n"

                    f"<blockquote><b>UPI ID:</b> <code>{upi_id}</code>\n"

                    f"<b>Name:</b> <code>{p_name}</code>\n"

                    f"<b>Amount:</b> <code>₹{s_price}</code></blockquote>\n\n"

                    f"• Make sure the amount is entered correctly.\n\n"

                    f"<b>𝚂𝚝𝚎𝚙 𝟸: Verify Payment</b>\n\n"

                    f"• After payment, click <b>PAYMENT DONE</b> to upload your screenshot.\n"

                    f"────────────────────"

                )

                kb = [

                    [InlineKeyboardButton(f"☑️ {_sc('PAYMENT DONE')}", callback_data=f"mb#upi_done#{s_id}")],

                    [InlineKeyboardButton(f"« ❮ {_sc('BACK')}", callback_data=f"mb#pay_back#{s_id}")]

                ]

            

            await query.message.delete()

            try:
                if qr_card:
                    if isinstance(qr_card, (bytes, bytearray)):
                        photo_obj = io.BytesIO(qr_card)
                        photo_obj.name = "upi_qr.png"
                        photo_obj.seek(0)
                    else:
                        photo_obj = qr_card
                        if hasattr(photo_obj, 'seek'):
                            photo_obj.seek(0)
                        if not getattr(photo_obj, 'name', None):
                            photo_obj.name = "upi_qr.png"
                    await client.send_photo(user_id, photo=photo_obj, caption=txt, reply_markup=InlineKeyboardMarkup(kb))
                else:
                    import urllib.parse
                    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=900x900&margin=1&data={urllib.parse.quote(upi_uri)}"
                    await client.send_photo(user_id, photo=qr_url, caption=txt, reply_markup=InlineKeyboardMarkup(kb))
            except Exception as e:
                logger.warning(f"UPI payment screen send failed: {e}")
                kb2 = [[InlineKeyboardButton(f"☑️ {'पेमेंट हो गया' if lang=='hi' else _sc('PAYMENT DONE')}", callback_data=f"mb#upi_done#{s_id}")]]
                await client.send_message(user_id, txt, reply_markup=InlineKeyboardMarkup(kb2))



    elif cmd == "pay_back":
        s_id = data[2]
        await query.answer()

        try:
            await query.message.delete()
        except:
            pass

        from bson.objectid import ObjectId
        story = await db.db.premium_stories.find_one({"_id": ObjectId(s_id)})
        if story:
            _bt = await _get_cached_bot_doc(client.me.id)
            _bt_cfg = (_bt or {}).get("config", {})
            return await _show_story_details(client, query, story, lang, bot_cfg=_bt_cfg)
        else:
            return await _edit_main_menu_in_place(client, query, query.from_user, lang)



    elif cmd == "upi_done":

        s_id = data[2]

        from bson.objectid import ObjectId

        await db.db.premium_checkout.update_one(

            {"user_id": user_id, "bot_id": client.me.id, "story_id": ObjectId(s_id)},

            {"$set": {"status": "waiting_screenshot", "updated_at": datetime.utcnow()}}

        )

        if lang == 'hi':

            await query.answer("कृपया अपना स्क्रीनशॉट भेजें।", show_alert=True)

            await query.message.reply_text(

                "<b>📸 पेमेंट स्क्रीनशॉट भेजें</b>\n\n"

                "सत्यापन शुरू करने के लिए कृपया अपने सफल भुगतान का स्क्रीनशॉट यहाँ भेजें।",

                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« वापस", callback_data=f"mb#pay#upi#{s_id}")]])

            )

        else:

            await query.answer(_sc("Please send your screenshot."), show_alert=True)

            await query.message.reply_text(

                f"<b>📸 {_sc('SEND PAYMENT SCREENSHOT')}</b>\n\n"

                f"{_sc('Please send your successful payment screenshot here to begin verification.')}",

                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"« {_sc('BACK')}", callback_data=f"mb#pay#upi#{s_id}")]])

            )




    elif cmd == "help_dev":
        txt_dev = (
            f"<b>⟦ {_sc('DEVELOPER & TECH SUPPORT')} ⟧</b>\n\n"
            f"<blockquote expandable>"
            f"<b>• Project Developer:</b> @MeJeetX\n"
            f"<b>• Architecture:</b> Arya Premium Store &amp; FastEngine\n"
            f"<b>• Telegram:</b> <a href=\"https://t.me/MeJeetX\">@MeJeetX</a>\n\n"
            f"<i>For custom bot development, infrastructure, or technical inquiries, click below to contact directly.</i>"
            f"</blockquote>"
        ) if lang == 'en' else (
            f"<b>⟦ {_sc('डेवलपर एवं तकनीकी सहायता')} ⟧</b>\n\n"
            f"<blockquote expandable>"
            f"<b>• मुख्य डेवलपर:</b> @MeJeetX\n"
            f"<b>• आर्किटेक्चर:</b> आर्या प्रीमियम स्टोर एवं फास्टइंजन\n"
            f"<b>• टेलीग्राम:</b> <a href=\"https://t.me/MeJeetX\">@MeJeetX</a>\n\n"
            f"<i>कस्टम बॉट डेवलपमेंट या तकनीकी सहायता के लिए नीचे दिए गए बटन से सीधे डेवलपर से संपर्क करें।</i>"
            f"</blockquote>"
        )
        kb_dev = [
            [_ikb("Contact Developer", url="https://t.me/MeJeetX", icon_custom_emoji_id="6021683099773966917")],
            [InlineKeyboardButton(f"« ❮ {_sc('BACK')}", callback_data="mb#main_help")]
        ]
        await _safe_edit(query.message, text=txt_dev, markup=InlineKeyboardMarkup(kb_dev))
        return

    # ── CASHFREE PAYMENT STATUS VERIFIER ──
    elif cmd == "cf_status":
        order_id = data[2]
        s_id = data[3]
        from cashfree_helper import check_cashfree_order_status
        from bson.objectid import ObjectId
        import time

        status_res = await check_cashfree_order_status(order_id)
        if status_res.get("is_paid"):
            await query.answer("✅ Payment Verified Successfully!", show_alert=False)
            story = await db.db.premium_stories.find_one({"_id": ObjectId(s_id)})
            if not story:
                story = await db.db.premium_stories.find_one({"_id": s_id})
            
            # Update order in DB
            await db.db.orders.update_one(
                {"order_id": order_id},
                {"$set": {"status": "paid", "paid_at": time.time()}}
            )
            # Add purchase to user
            await db.db.users.update_one(
                {"id": int(user_id)},
                {"$addToSet": {"purchases": ObjectId(s_id)}}
            )
            await db.db.premium_purchases.update_one(
                {"user_id": int(user_id), "story_id": ObjectId(s_id)},
                {"$set": {
                    "user_id": int(user_id),
                    "story_id": ObjectId(s_id),
                    "source": "cashfree",
                    "amount": story.get("price", 0) if story else 0,
                    "order_id": order_id,
                    "created_at": time.time()
                }},
                upsert=True
            )
            # Log payment
            from utils import log_payment
            s_name = story.get('story_name_en', 'Story') if story else 'Story'
            asyncio.create_task(log_payment(
                amount=story.get("price", 0) if story else 0,
                user_id=user_id,
                story_name=s_name,
                payment_method="Cashfree (Cards/NetBanking/UPI)",
                order_id=order_id
            ))
            # Delete payment prompt message
            try:
                await query.message.delete()
            except Exception:
                pass
            return await dispatch_delivery_choice(client, user_id, story)
        else:
            cf_st = status_res.get("status", "PENDING")
            if cf_st == "FAILED":
                return await query.answer("❌ Payment Failed. Please try again.", show_alert=True)
            elif cf_st == "EXPIRED":
                return await query.answer("⚠️ Payment Session Expired. Please initiate a new order.", show_alert=True)
            else:
                return await query.answer("⏳ Payment is still pending. Please complete the payment on Cashfree and tap Check Status.", show_alert=True)

    # ── SECURE CHECKOUT - 2 handlers ──

    elif cmd == "pay2":
        method = data[2]
        s_id = data[3]
        from bson.objectid import ObjectId
        story = await db.db.premium_stories.find_one({"_id": ObjectId(s_id)})
        if not story: return await query.answer("Story not found!", show_alert=True)

        if method == "cashfree":
            # Cashfree Payment Gateway order flow
            logger.info(f"[PAY2] User {user_id} clicked Cashfree / Cards / NetBanking option for story {s_id}")
            try:
                await query.answer()
            except Exception:
                pass

            from cashfree_helper import create_cashfree_order
            bot_username = getattr(getattr(client, "me", None), "username", "")
            user_name = query.from_user.first_name or "Buyer"
            
            cf_res = await create_cashfree_order(user_id=user_id, user_name=user_name, story=story, bot_username=bot_username)
            
            order_id = cf_res.get("order_id") or f"cf_{user_id}_{int(time.time())}"
            pay_link = cf_res.get("payment_link")

            s_name = story.get(f'story_name_{lang}', story.get('story_name_en', 'Story'))
            price = story.get('price', 0)

            if not pay_link:
                logger.error(f"[PAY2-CF] pay_link is None/empty! cf_res={cf_res}")
                return await query.answer("❌ Payment link not generated. Please try again.", show_alert=True)

            logger.info(f"[PAY2-CF] Showing payment screen to user {user_id}: order={order_id}, link={pay_link}")

            desc_cf = (
                f"<b>⟦ 💳 PAYMENT GATEWAY ⟧</b>\n\n"
                f"<b>• Story:</b> {to_mathbold(s_name)}\n"
                f"<b>• Amount:</b> ₹{price}\n"
                f"<b>• Order ID:</b> <code>{order_id}</code>\n\n"
                f"<i>Tap <b>Pay Now</b> below to pay securely via Credit/Debit Cards, NetBanking, or UPI (GPay, PhonePe, Paytm).</i>\n\n"
                f"<i>After completing payment, tap <b>Check Status</b> for instant automated delivery.</i>"
            ) if lang == 'en' else (
                f"<b>⟦ 💳 पेमेंट गेटवे ⟧</b>\n\n"
                f"<b>• कहानी:</b> {to_mathbold(s_name)}\n"
                f"<b>• राशि:</b> ₹{price}\n"
                f"<b>• ऑर्डर आईडी:</b> <code>{order_id}</code>\n\n"
                f"<i>कार्ड्स (क्रेडिट/डेबिट), नेटबैंकिंग, या UPI (GPay, PhonePe, Paytm) से भुगतान करने के लिए नीचे <b>Pay Now</b> पर टैप करें।</i>\n\n"
                f"<i>भुगतान पूरा करने के बाद, तत्काल डिलीवरी के लिए <b>Check Status</b> पर टैप करें।</i>"
            )

            pay_now_lbl = "💳 Pay Now (Cards / NetBanking / UPI)" if lang == 'en' else "💳 अभी भुगतान करें (Cards/UPI/NetBanking)"
            check_lbl = "🔄 Check Payment Status" if lang == 'en' else "🔄 स्टेटस चेक करें"
            back_lbl = "« ❮ " + (_sc("BACK") if lang == 'en' else "वापस")

            kb = [
                [InlineKeyboardButton(pay_now_lbl, url=pay_link)],
                [_ikb(check_lbl, callback_data=f"mb#cf_status#{order_id}#{s_id}", icon_custom_emoji_id="5807492110059838726")],
                [InlineKeyboardButton(back_lbl, callback_data=f"mb#pay_back#{s_id}")]
            ]

            await _safe_edit(query.message, text=desc_cf, markup=InlineKeyboardMarkup(kb))
            return

        elif method == "upi":
            # Direct UPI Transfer Screen
            logger.info(f"[PAY2] User {user_id} clicked Direct UPI option for story {s_id}")
            try:
                if getattr(db, "db", None) is None and hasattr(db, "connect"):
                    await db.connect()
                db_obj = getattr(db, "db", None)
                bt = (await db_obj.premium_bots.find_one({"id": client.me.id})) if db_obj is not None else None
                logger.info(f"[PAY2] Loaded bot config: {bool(bt)}")
                bt_cfg = bt.get("config", {}) if bt else {}
                upi_id, p_name = await _get_rotated_upi(bt_cfg)
                logger.info(f"[PAY2] Rotated UPI resolved: upi_id={upi_id}, payee={p_name}")
                
                s_price = str(story["price"])
                s_name = story.get(f'story_name_{lang}', story.get('story_name_en', 'Story'))
                logger.info(f"[PAY2] Story details: price={s_price}, name={s_name}")
            except Exception as ex:
                logger.error(f"[PAY2] Error setting up variables: {ex}", exc_info=True)
                return await query.answer(f"Setup error: {ex}", show_alert=True)
            
            try:
                upi_uri = _build_upi_uri(
                    upi_id=upi_id,
                    payee_name=p_name,
                    amount=int(story["price"]),
                    note=f"Payment for {s_name[:20]}"
                )
                logger.info(f"[PAY2] Built UPI URI: {upi_uri}")

                slice_api_url = (getattr(Config, "SLICEURL_API_URL", "") or "").strip()
                slice_api_key = (getattr(Config, "SLICEURL_API_KEY", "") or "").strip()
                slice_direct = ""
                if slice_api_url and slice_api_key.startswith("slc_"):
                    slice_direct = await _sliceurl_api_shorten(upi_uri)
                button_url = slice_direct
                logger.info(f"[PAY2] Shortened URL: {button_url}")
            except Exception as ex:
                logger.error(f"[PAY2] Error building UPI URI / Shortener: {ex}", exc_info=True)
                upi_uri = f"upi://pay?pa={upi_id}&pn={p_name}&am={s_price}&cu=INR"

            # Generate Simple Clean UPI QR Code (no template card)
            qr_card = None
            try:
                logger.info("[PAY2] Attempting to generate simple QR code image...")
                qr_card = _make_qr_png_bytes(upi_uri)
                logger.info(f"[PAY2] Simple QR code generation result: {'SUCCESS' if qr_card else 'FAILED'}")
            except Exception as e:
                logger.error(f"[PAY2] Simple QR code generation raised exception: {e}", exc_info=True)
                qr_card = None

            try:
                logger.info("[PAY2] Updating checkout in DB...")
                order_id = await _make_arya_bot_order_id(user_id, str(s_id))
                await db.db.premium_checkout.update_one(
                    {"user_id": user_id, "bot_id": client.me.id, "story_id": ObjectId(s_id)},
                    {"$set": {
                        "status": "pending_gateway",
                        "order_id": order_id,
                        "bot_username": client.me.username,
                        "username": query.from_user.username or "",
                        "first_name": query.from_user.first_name or "",
                        "method": "upi",
                        "amount": int(story["price"]),
                        "upi_uri": upi_uri,
                        "pay_link_copy": button_url,
                        "upi_id_shown": upi_id,
                        "upi_payee_name_shown": p_name,
                        "updated_at": datetime.utcnow(),
                    }, "$setOnInsert": {"created_at": datetime.utcnow()}},
                    upsert=True
                )
                logger.info("[PAY2] Checkout updated.")
            except Exception as ex:
                logger.error(f"[PAY2] Database checkout update failed: {ex}", exc_info=True)

            p_name = (bt_cfg.get("upi_name") or "Merchant").strip()

            try:
                logger.info("[PAY2] Setting pending_utr_story_id in user doc...")
                await db.db.users.update_one(
                    {"id": user_id},
                    {"$set": {"pending_utr_story_id": s_id, "pending_utr_opened_at": datetime.utcnow()}},
                    upsert=True
                )
                logger.info("[PAY2] User doc updated.")
            except Exception as ex:
                logger.error(f"[PAY2] Database user update failed: {ex}", exc_info=True)

            if lang == 'hi':
                txt = (
                    f"<b>⟦ ᴅɪʀᴇᴄᴛ ᴜᴘɪ ᴛʀᴀɴꜱꜰᴇʀ ⟧</b>\n\n"
                    f"<b>𝗦𝘁𝗲𝗽 𝟭: ₹{s_price} का भुगतान करें</b>\n\n"
                    f"<blockquote>• QR कोड स्कैन करें या नीचे दिए गए विवरण से भुगतान करें:</blockquote>\n"
                    f"<blockquote><b>UPI ID:</b> <code>{upi_id}</code>\n"
                    f"<b>नाम:</b> <code>{p_name}</code>\n"
                    f"<b>राशि:</b> <code>₹{s_price}</code></blockquote>\n\n"
                    f"<b>𝗦𝘁𝗲𝗽 𝟮: पेमेंट वेरिफाई</b>\n\n"
                    f"<blockquote>"
                    f'<emoji id="6030410254276106984">💳</emoji> भुगतान के बाद, अपनी Payment App (Paytm, PhonePe, GPay) के <b>Transaction Page</b> से <b>12 अंकों का UPI Reference Number / UTR Number</b> कॉपी करके यहाँ इस चैट में <b>भेजें</b>।\n\n'
                    f'<emoji id="6023761060786346622">⚡</emoji> UTR भेजते ही बोट तुरंत वेरिफाई करेगा — कोई बटन दबाने की जरूरत नहीं।'
                    f"</blockquote>\n"
                    f"────────────────────"
                )
                kb = [
                    [InlineKeyboardButton("« वापस", callback_data=f"mb#pay_back#{s_id}")]
                ]
            else:
                txt = (
                    f"<b>⟦ ᴅɪʀᴇᴄᴛ ᴜᴘɪ ᴛʀᴀɴꜱꜰᴇʀ ⟧</b>\n\n"
                    f"<b>𝗦𝘁𝗲𝗽 𝟭: Pay ₹{s_price}</b>\n\n"
                    f"<blockquote>• Scan the QR code or pay using the details below:</blockquote>\n"
                    f"<blockquote><b>UPI ID:</b> <code>{upi_id}</code>\n"
                    f"<b>Name:</b> <code>{p_name}</code>\n"
                    f"<b>Amount:</b> <code>₹{s_price}</code></blockquote>\n\n"
                    f"<b>𝗦𝘁𝗲𝗽 𝟮: Verify Payment</b>\n\n"
                    f"<blockquote>"
                    f'<emoji id="6030410254276106984">💳</emoji> After payment, open your Payment App (Paytm, PhonePe, GPay) → go to <b>Transaction Page</b> → copy the <b>12-digit UPI Reference Number / UTR Number</b> and <b>send it here in this chat.</b>\n\n'
                    f'<emoji id="6023761060786346622">⚡</emoji> As soon as you send the UTR, the bot will <b>instantly verify</b> your payment — no button needed.'
                    f"</blockquote>\n"
                    f"────────────────────"
                )
                kb = [
                    [InlineKeyboardButton(f"« Back", callback_data=f"mb#pay_back#{s_id}")]
                ]
            
            try:
                logger.info("[PAY2] Deleting previous inline message...")
                await query.message.delete()
                logger.info("[PAY2] Previous message deleted.")
            except Exception as ex:
                logger.warning(f"[PAY2] Failed to delete previous message: {ex}")

            try:
                if qr_card:
                    logger.info("[PAY2] Sending generated UPI QR Card photo...")
                    if isinstance(qr_card, (bytes, bytearray)):
                        photo_obj = io.BytesIO(qr_card)
                        photo_obj.name = "upi_qr.png"
                        photo_obj.seek(0)
                    else:
                        photo_obj = qr_card
                        if hasattr(photo_obj, 'seek'):
                            photo_obj.seek(0)
                        if not getattr(photo_obj, 'name', None):
                            photo_obj.name = "upi_qr.png"
                    await client.send_photo(user_id, photo=photo_obj, caption=txt, reply_markup=InlineKeyboardMarkup(kb))
                    logger.info("[PAY2] Photo sent successfully.")
                else:
                    import urllib.parse
                    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=900x900&margin=1&data={urllib.parse.quote(upi_uri)}"
                    logger.info(f"[PAY2] Sending fallback QR code URL: {qr_url}")
                    await client.send_photo(user_id, photo=qr_url, caption=txt, reply_markup=InlineKeyboardMarkup(kb))
                    logger.info("[PAY2] Fallback QR code photo sent successfully.")
            except Exception as e:
                logger.error(f"[PAY2] Failed to send photo (both custom and qrserver): {e}", exc_info=True)
                kb2 = [[InlineKeyboardButton("« वापस" if lang=='hi' else "« Back", callback_data=f"mb#pay_back#{s_id}")]]
                try:
                    logger.info("[PAY2] Attempting to send text-only fallback message...")
                    await client.send_message(user_id, txt, reply_markup=InlineKeyboardMarkup(kb2))
                    logger.info("[PAY2] Text-only fallback message sent.")
                except Exception as ex2:
                    logger.critical(f"[PAY2] Text-only fallback message ALSO failed: {ex2}", exc_info=True)
                    # Try a very basic text send
                    try:
                        await client.send_message(user_id, "❌ Error sending checkout screen. Please contact support.")
                    except:
                        pass
            
            # Answer the callback query to stop the loading spinner
            try:
                await query.answer()
            except:
                pass

        elif method == "crypto":
            # OxaPay Crypto Invoice generation
            await query.message.edit_text(f"🔐 <b>{_sc('PREPARING YOUR SECURE CHECKOUT')}...</b>\n<i>{_sc('Please wait a moment while we connect to the gateway.')}</i>")
            price = int(story["price"])
            s_name = story.get('story_name_en', 'Premium Content')
            
            url, track_id = await _create_oxapay_invoice_bot(price, s_name)
            if not url:
                err_msg = track_id or "Could not generate crypto payment link."
                return await query.message.edit_text(
                    f"❌ Could not generate payment gateway link for <b>Crypto</b>.\n\n<code>{err_msg}</code>\n\nPlease try Direct UPI Transfer.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"❮ {_sc('BACK')}", callback_data=f"mb#pay_back#{s_id}")]])
                )

            # Record checkout in DB
            order_id = await _make_arya_bot_order_id(user_id, str(s_id))
            await db.db.premium_checkout.update_one(
                {"user_id": user_id, "bot_id": client.me.id, "story_id": ObjectId(s_id)},
                {"$set": {
                    "status": "pending_gateway",
                    "order_id": order_id,
                    "bot_username": client.me.username,
                    "username": query.from_user.username or "",
                    "first_name": query.from_user.first_name or "",
                    "method": "crypto",
                    "payment_id": track_id,
                    "amount": price,
                    "pay_link_copy": url,
                    "updated_at": datetime.utcnow(),
                }, "$setOnInsert": {"created_at": datetime.utcnow()}},
                upsert=True
            )

            usd_amount = round(price / 84.0, 2)
            if usd_amount < 0.50:
                usd_amount = 0.50

            lbl_pay = "सुरक्षित क्रिप्टो भुगतान करें" if lang == 'hi' else f"₿ Pay securely via Crypto"
            lbl_ver = "क्रिप्टो भुगतान सत्यापित करें" if lang == 'hi' else "Verify Crypto Payment"
            lbl_bck = "‹ वापस" if lang == 'hi' else "‹ Back"

            kb = [
                [InlineKeyboardButton(lbl_pay, url=url)],
                [_ikb(lbl_ver, callback_data=f"mb#crypto2_check#{s_id}", icon_custom_emoji_id="5807492110059838726")],
                [InlineKeyboardButton(lbl_bck, callback_data=f"mb#pay_back#{s_id}")]
            ]

            if lang == "hi":
                check_txt = (
                    f'<emoji id="5472030678633684592">💸</emoji> <b>सुरक्षित चेकआउट</b>\n\n'
                    f'<b><emoji id="6023962911364357003">📖</emoji> कहानी:</b> <code>{story.get("story_name_en", "Premium Story")}</code>\n'
                    f'<b><emoji id="5283232570660634549">💰</emoji> कुल कीमत:</b> <code>₹{price} (~${usd_amount} USD)</code>\n\n'
                    f"<blockquote expandable>"
                    f'<b><emoji id="6019328362479097179">🛡</emoji> क्रिप्टो से भुगतान कैसे करें?</b>\n\n'
                    f"1. नीचे दिए गए '{lbl_pay}' बटन पर क्लिक करें।\n"
                    f"2. आपको OxaPay के सुरक्षित गेटवे पर भेजा जाएगा।\n"
                    f"3. समर्थित कॉइन (USDT, BTC, LTC आदि) चुनें और भुगतान करें।\n"
                    f"4. भुगतान पूरा होने के बाद वापस आकर '{lbl_ver}' पर क्लिक करें।\n"
                    f"5. बॉट तुरंत सत्यापित करके आपकी फ़ाइल भेज देगा।"
                    f"</blockquote>\n\n"
                    f'<i><emoji id="6023761060786346622">⚡</emoji> 24/7 स्वचालित सत्यापन और तत्काल डिलीवरी।</i>'
                )
            else:
                check_txt = (
                    f'<emoji id="5472030678633684592">💸</emoji> <b>SECURE CHECKOUT</b>\n\n'
                    f'<b><emoji id="6023962911364357003">📖</emoji> Story Name:</b> <code>{story.get("story_name_en", "Premium Story")}</code>\n'
                    f'<b><emoji id="5283232570660634549">💰</emoji> Total Price:</b> <code>₹{price} (~${usd_amount} USD)</code>\n\n'
                    f"<blockquote expandable>"
                    f'<b><emoji id="6019328362479097179">🛡</emoji> How to pay via Crypto (OxaPay):</b>\n\n'
                    f"1. Click the '{lbl_pay}' button below.\n"
                    f"2. Select your preferred coin (USDT, BTC, LTC, etc.) on OxaPay.\n"
                    f"3. Send the exact amount shown to the payment address.\n"
                    f"4. Once transaction is complete, return here and click '{lbl_ver}'.\n"
                    f"5. The bot will automatically verify and grant instant access."
                    f"</blockquote>\n\n"
                    f'<i><emoji id="6023761060786346622">⚡</emoji> 24/7 automated verification & instant delivery.</i>'
                )
            
            await query.message.edit_text(check_txt, reply_markup=InlineKeyboardMarkup(kb))

    elif cmd == "verify2_utr":
        try:
            s_id = data[2]
            from bson.objectid import ObjectId
            story = await db.db.premium_stories.find_one({"_id": ObjectId(s_id)})
            if not story: return await query.answer("Story not found!", show_alert=True)

            # Check if user already sent a UTR in chat
            user_doc = await db.db.users.find_one({"id": user_id}) or {}
            utr = (user_doc.get("last_utr_entered") or "").strip()

            if not utr:
                # User hasn't sent UTR yet
                if lang == 'hi':
                    return await query.answer(
                        "⚠️ पहले अपना 12-अंकों का UTR / Reference Number इस चैट में भेजें, फिर यह बटन दबाएं।",
                        show_alert=True
                    )
                else:
                    return await query.answer(
                        "⚠️ Please send your 12-digit UTR / Reference Number in this chat first, then click this button.",
                        show_alert=True
                    )

            await query.answer()

            # Check story already purchased
            if await db.has_purchase(user_id, s_id):
                return await client.send_message(
                    user_id,
                    "✅ <b>You already own this story!</b>",
                    parse_mode=enums.ParseMode.HTML
                )

            # Check UTR already claimed
            existing_utr = await db.db.verified_utrs.find_one({"utr": utr})
            if existing_utr:
                # Clear stale stored UTR
                await db.db.users.update_one({"id": user_id}, {"$unset": {"last_utr_entered": 1, "pending_utr_story_id": 1}})
                if lang == 'hi':
                    return await client.send_message(
                        user_id,
                        f"❌ <b>UTR पहले से उपयोग हो चुका है!</b>\n"
                        f"<code>{utr}</code> यह UTR पहले ही किसी अन्य खरीد के लिए उपयोग किया जा चुका है।\n\n"
                        f"यदि यह आपका सही UTR है, तो संपर्क सहायता से करें।",
                        parse_mode=enums.ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🔄 दूसरा UTR डालें", callback_data=f"mb#verify2_utr#{s_id}")],
                        ])
                    )
                else:
                    return await client.send_message(
                        user_id,
                        f"❌ <b>UTR Already Claimed!</b>\n"
                        f"<code>{utr}</code> — This UTR has already been used for another purchase.\n\n"
                        f"<i>If you believe this is a mistake, please contact support.</i>",
                        parse_mode=enums.ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🔄 Enter a Different UTR", callback_data=f"mb#clear_and_retry_utr#{s_id}")],
                        ])
                    )

            # Load Gmail credentials
            cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
            gmail_user = cfg.get("gmail_user", "").strip()
            gmail_password = cfg.get("gmail_app_password", "").strip()
            import os
            if not gmail_user:
                gmail_user = os.environ.get("GMAIL_USER", "").strip() or os.environ.get("gmail_user", "").strip()
            if not gmail_password:
                gmail_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip() or os.environ.get("gmail_app_password", "").strip()

            if not gmail_user or not gmail_password:
                return await client.send_message(
                    user_id,
                    "❌ <b>Auto-verification not configured!</b>\nPlease contact admin."
                    f"\n\n<i>Your UTR: <code>{utr}</code> — Save this for manual verification.</i>",
                    parse_mode=enums.ParseMode.HTML
                )

            # Clear UTR + state BEFORE IMAP (prevent double submission)
            await db.db.users.update_one(
                {"id": user_id},
                {"$unset": {"last_utr_entered": 1, "pending_utr_story_id": 1, "state": 1}}
            )

            ver_msg = await client.send_message(
                user_id,
                "⏳ <b>Verifying your payment... Please wait.</b>\n"
                "<i>Checking bank alerts via secure IMAP.</i>",
                parse_mode=enums.ParseMode.HTML
            )

            expected_total = float(story["price"])

            def _check_imap_sync():
                import imaplib, email as email_lib
                result = {"verified": False, "amount_mismatch": False, "mismatched_amount": None, "error": None}
                try:
                    # ── Sanitize UTR: extract only ASCII digits before IMAP search ──
                    # imaplib encodes commands as ASCII; any non-ASCII char (\xa0, etc.) causes UnicodeEncodeError
                    utr_clean = ''.join(c for c in utr if c in '0123456789')
                    if not utr_clean or len(utr_clean) < 8:
                        result["error"] = f"Invalid UTR after sanitization: '{utr_clean}'"
                        return result

                    mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
                    mail.login(gmail_user, gmail_password)
                    mail.select("INBOX")

                    # Use byte literal for IMAP search to bypass ASCII encoding issues
                    search_criteria = b'TEXT "' + utr_clean.encode('ascii') + b'"'
                    status, messages = mail.search(None, search_criteria)

                    if status == "OK" and messages[0]:
                        for mail_id in reversed(messages[0].split()):
                            res_status, msg_data = mail.fetch(mail_id, "(RFC822)")
                            if res_status != "OK":
                                continue
                            for response_part in msg_data:
                                if isinstance(response_part, tuple):
                                    email_msg = email_lib.message_from_bytes(response_part[1])
                                    from_header = email_msg.get("From", "")
                                    if "noreply@slice.bank.in" not in from_header.lower():
                                        continue
                                    body = get_email_body(email_msg)
                                    # Check both clean and original UTR in email body
                                    if utr_clean in body or utr in body:
                                        if verify_amount_in_email(body, expected_total):
                                            result["verified"] = True
                                        else:
                                            result["amount_mismatch"] = True
                                            parsed = extract_amount_from_email(body)
                                            if parsed is not None:
                                                result["mismatched_amount"] = parsed
                                        break
                            if result["verified"] or result["amount_mismatch"]:
                                break



                    mail.close()
                    mail.logout()
                except Exception as ex:
                    result["error"] = str(ex)
                return result

            try:
                imap_result = await asyncio.to_thread(_check_imap_sync)
            except Exception as ex:
                await ver_msg.delete()
                return await client.send_message(
                    user_id,
                    f"❌ <b>Verification error!</b>\nFailed to connect to verification server.\n<i>Detail: {ex}</i>",
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[_ikb("Retry", callback_data=f"mb#verify2_utr#{s_id}", icon_custom_emoji_id="5807492110059838726")]])
                )

            await ver_msg.delete()

            if imap_result.get("error"):
                err = imap_result["error"]
                logger.error(f"Gmail IMAP error for UTR {utr}: {err}")
                return await client.send_message(
                    user_id,
                    f"❌ <b>Verification failed!</b>\nCould not connect to mail server.\n<i>{err}</i>",
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[_ikb("Retry", callback_data=f"mb#verify2_utr#{s_id}", icon_custom_emoji_id="5807492110059838726")]])
                )

            if not imap_result["verified"]:
                if imap_result["amount_mismatch"]:
                    mismatched = imap_result["mismatched_amount"]
                    if lang == 'hi':
                        err_text = (
                            f"❌ <b>राशि में अंतर!</b>\n"
                            f"अपेक्षित: ₹{expected_total:.2f}\n"
                            f"प्राप्त: ₹{mismatched:.2f}\n\n"
                            f"सही राशि भेजें और पुनः प्रयास करें।"
                        )
                    else:
                        err_text = (
                            f"❌ <b>Amount Mismatch!</b>\n"
                            f"Expected: ₹{expected_total:.2f}\n"
                            f"Detected: ₹{mismatched:.2f}\n\n"
                            f"Please pay the exact amount and try again."
                        )
                else:
                    if lang == 'hi':
                        err_text = (
                            f"❌ <b>भुगतान नहीं मिला!</b>\n"
                            f"UTR <code>{utr}</code> और राशि ₹{expected_total} का कोई अलर्ट नहीं मिला।\n\n"
                            f"यदि आपने अभी भुगतान किया है, तो 15-30 सेकंड रुकें और पुनः प्रयास करें।"
                        )
                    else:
                        err_text = (
                            f"❌ <b>Payment not found!</b>\n"
                            f"No bank alert found for UTR: <code>{utr}</code> and amount: ₹{expected_total}\n\n"
                            f"<i>If you just paid, wait 15-30 seconds for the bank email, then retry.</i>"
                        )
                return await client.send_message(
                    user_id,
                    err_text,
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [_ikb("पुनः प्रयास करें" if lang == 'hi' else "Retry", callback_data=f"mb#verify2_utr#{s_id}", icon_custom_emoji_id="5807492110059838726")],
                        [InlineKeyboardButton("« वापस" if lang == 'hi' else "« Back", callback_data=f"mb#pay_back#{s_id}")]
                    ])
                )

            # ✅ Payment verified! Record and grant access
            await db.db.verified_utrs.insert_one({
                "utr": utr,
                "amount": expected_total,
                "user_id": user_id,
                "verified_at": datetime.utcnow()
            })
            await db.db.premium_checkout.update_one(
                {"user_id": user_id, "bot_id": client.me.id, "story_id": ObjectId(s_id)},
                {"$set": {"status": "approved", "updated_at": datetime.utcnow()}}
            )

            import random, string as _string
            order_id = await _make_arya_bot_order_id(user_id, str(s_id))

            await db.db.premium_purchases.insert_one({
                "user_id": user_id,
                "story_id": ObjectId(s_id),
                "bot_id": client.me.id,
                "purchased_at": datetime.utcnow(),
                "source": "upi",
                "amount": expected_total,
                "reference": utr,
                "order_id": order_id
            })
            await db.add_purchase(user_id, s_id)

            from utils import log_payment, log_arya_event
            user_info = await db.get_user(user_id, from_user=query.from_user)
            s_name = story.get("story_name_en", "Unknown")

            asyncio.create_task(log_arya_event(
                event_type="PAYMENT PROCESSED",
                user_id=user_id,
                user_info=user_info,
                details=f"Story: {s_name}\nGateway: Direct UPI\nUTR: <code>{utr}</code>\nAmount: ₹{expected_total}\nOrder: {order_id}"
            ))
            asyncio.create_task(log_payment(
                user_id=user_id,
                user_first_name=user_info.get("first_name", "User"),
                username=user_info.get("username", ""),
                s_name=s_name,
                amount=expected_total,
                method="upi",
                receipt_id=utr,
                order_id=order_id,
                user_last_name=user_info.get("last_name", "")
            ))

            await client.send_message(
                user_id,
                "✅ <b>Payment Verified Successfully!</b>\n<i>Access granted. Preparing your story delivery...</i>",
                parse_mode=enums.ParseMode.HTML
            )
            story = await db.db.premium_stories.find_one({"_id": ObjectId(s_id)})
            return await dispatch_delivery_choice(client, user_id, story)

        except Exception as e:
            logger.exception(f"Error in verify2_utr: {e}")
            try:
                await query.answer(f"❌ Error: {e}", show_alert=True)
            except Exception:
                pass

    elif cmd == "clear_and_retry_utr":
        s_id = data[2] if len(data) > 2 else None
        await query.answer()
        await db.db.users.update_one(
            {"id": user_id},
            {"$unset": {"last_utr_entered": 1}}
        )
        if s_id:
            await db.db.users.update_one(
                {"id": user_id},
                {"$set": {"pending_utr_story_id": s_id}},
                upsert=True
            )
        if lang == 'hi':
            await client.send_message(
                user_id,
                "✍️ अपना सही 12-अंकों का UTR/Reference Number इस चैट में भेजें, फिर Verify बटन दबाएं।",
                parse_mode=enums.ParseMode.HTML
            )
        else:
            await client.send_message(
                user_id,
                "✍️ Please send your correct 12-digit UTR/Reference Number in this chat, then click Verify button.",
                parse_mode=enums.ParseMode.HTML
            )

    elif cmd == "cancel_utr_input":
        await query.answer()
        await db.db.users.update_one(
            {"id": user_id},
            {"$unset": {"state": 1, "pending_utr_story_id": 1, "last_utr_entered": 1}}
        )
        s_id = data[2] if len(data) > 2 else None
        try:
            await query.message.delete()
        except Exception:
            pass
        if s_id:
            try:
                from bson.objectid import ObjectId
                story = await db.db.premium_stories.find_one({"_id": ObjectId(s_id)})
                if story:
                    await _show_story_details_v2(client, user_id, story, lang, query)
                    return
            except Exception:
                pass
        if lang == 'hi':
            await client.send_message(user_id, "<i>❌ UTR सत्यापन रद्द किया गया।</i>", parse_mode=enums.ParseMode.HTML)
        else:
            await client.send_message(user_id, "<i>❌ UTR verification cancelled.</i>", parse_mode=enums.ParseMode.HTML)

    elif cmd == "crypto2_check":
        s_id = data[2] if len(data) > 2 else None
        if not s_id: return await query.answer("Invalid.", show_alert=True)
        
        from bson.objectid import ObjectId
        checkout = await db.db.premium_checkout.find_one(
            {"user_id": user_id, "bot_id": client.me.id, "story_id": ObjectId(s_id), "status": "pending_gateway"}
        )
        if not checkout or not checkout.get("payment_id"):
            return await query.answer("No pending payment found. Generate link again.", show_alert=True)

        await query.answer("Checking payment status... please wait.", show_alert=False)
        m = await query.message.edit_text(f"🛡️ <b>{_sc('VERIFYING PAYMENT')}...</b>\n<i>{_sc('Checking crypto blockchain status via OxaPay.')}</i>")
        
        status = await _check_oxapay_invoice_bot(checkout["payment_id"])
        
        if status == "paid":
            # Payment confirmed!
            await db.db.premium_checkout.update_one(
                {"_id": checkout["_id"]},
                {"$set": {"status": "approved", "updated_at": datetime.utcnow()}}
            )
            await m.edit_text("✅ <b>Crypto Payment Confirmed successfully!</b>\nAdding to your unlocked stories...")
            
            story = await db.db.premium_stories.find_one({"_id": ObjectId(s_id)})
            
            if not await db.has_purchase(user_id, str(s_id)):
                import random, string
                order_id = await _make_arya_bot_order_id(user_id, str(s_id))
                
                await db.db.premium_purchases.insert_one({
                    "user_id": user_id,
                    "story_id": ObjectId(s_id),
                    "bot_id": client.me.id,
                    "purchased_at": datetime.utcnow(),
                    "source": "crypto",
                    "amount": checkout.get("amount", 0),
                    "reference": checkout.get("payment_id"),
                    "order_id": order_id
                })
                await db.add_purchase(user_id, str(s_id))
                
                # Log success
                from utils import log_payment, log_arya_event
                user_info = await db.get_user(user_id, from_user=query.from_user)
                s_name = story.get("story_name_en") if story else "Unknown"
                
                asyncio.create_task(log_arya_event(
                    event_type="PAYMENT PROCESSED",
                    user_id=user_id,
                    user_info=user_info,
                    details=f"Story: {s_name}\nGateway: CRYPTO (OxaPay)\nOrder ID: <code>{order_id}</code>\nAmount: ₹{checkout.get('amount', 0)}"
                ))
                
                asyncio.create_task(log_payment(
                    user_id=user_id,
                    user_first_name=user_info.get("first_name", "User"),
                    username=user_info.get('username', ''),
                    s_name=s_name,
                    amount=checkout.get("amount", 0),
                    method="crypto",
                    receipt_id=checkout.get("payment_id", ""),
                    pay_link=checkout.get("pay_link_copy", ""),
                    order_id=order_id,
                    user_last_name=user_info.get("last_name", "")
                ))
            
            story = await db.db.premium_stories.find_one({"_id": ObjectId(s_id)})
            return await dispatch_delivery_choice(client, user_id, story)
            
        else:
            await query.answer("Payment pending. Please complete transaction or try again in a minute.", show_alert=True)
            savage_msg = (
                f"<b>❌ 𝗣𝗮𝘆𝗺𝗲𝗻𝘁 𝗡𝗼𝘁 𝗙𝗼𝘂𝗻𝗱!</b>\n"
                f"<b>Status:</b> <code>{status.upper()}</code>\n\n"
                f"If you paid, wait a moment for blockchain confirmation, then click Verify again."
            )
            await m.edit_text(
                savage_msg,
                reply_markup=InlineKeyboardMarkup([
                    [_ikb("Verify Again", callback_data=f"mb#crypto2_check#{s_id}", icon_custom_emoji_id="5807492110059838726")],
                    [InlineKeyboardButton("« Back", callback_data=f"mb#show_tc#{s_id}")]
                ])
            )




    elif cmd.endswith("_check") and cmd.split("_")[0] in ("razorpay", "easebuzz"):

        s_id = data[2] if len(data) > 2 else None

        if not s_id: return await query.answer("Invalid.", show_alert=True)

        method = cmd.split("_")[0]

        

        from bson.objectid import ObjectId

        checkout = await db.db.premium_checkout.find_one(

            {"user_id": user_id, "bot_id": client.me.id, "story_id": ObjectId(s_id), "status": "pending_gateway"}

        )

        if not checkout or not checkout.get("payment_id"):

            return await query.answer("No pending payment found. Generate link again.", show_alert=True)

        

        await query.answer("Checking payment status... please wait.", show_alert=False)

        m = await query.message.edit_text(f"🛡️ <b>{_sc('VERIFYING PAYMENT')}...</b>\n<i>{_sc('Checking with')} {method.capitalize()} {_sc('servers.')}</i>")

        

        status = "failed"

        if method == "razorpay":

            status = await _check_rzp_status(checkout["payment_id"])

        else:

            status = await _check_easebuzz_status(checkout["payment_id"], checkout.get("amount", 0))

            

        if status == "paid":

            # Confirmed!

            await db.db.premium_checkout.update_one(

                {"_id": checkout["_id"]},

                {"$set": {"status": "approved", "updated_at": datetime.utcnow()}}

            )

            # Send notification

            await m.edit_text("✅ <b>Payment Confirmed successfully!</b>\nAdding to your unlocked stories...")

            

            # Use utility db function to add purchase 

            story = await db.db.premium_stories.find_one({"_id": ObjectId(s_id)})

            

            if not await db.has_purchase(user_id, str(s_id)):

                import random, string

                order_id = await _make_arya_bot_order_id(user_id, str(s_id))

                

                # Record in global audit collection

                await db.db.premium_purchases.insert_one({

                    "user_id": user_id,

                    "story_id": ObjectId(s_id),

                    "bot_id": client.me.id,

                    "purchased_at": datetime.utcnow(),

                    "source": method,

                    "amount": checkout.get("amount", 0),

                    "reference": checkout.get("payment_id"),

                    "order_id": order_id

                })

                # Add to user's personal unlocked list (crucial for Unlocked Stories menu)

                await db.add_purchase(user_id, str(s_id))

                

                # Log success

                from utils import log_payment, log_arya_event

                user_info = await db.get_user(user_id, from_user=query.from_user)

                s_name = story.get("story_name_en") if story else "Unknown"

                

                asyncio.create_task(log_arya_event(

                    event_type="PAYMENT PROCESSED",

                    user_id=user_id,

                    user_info=user_info,

                    details=f"Story: {s_name}\nGateway: {method.upper()}\nOrder ID: <code>{order_id}</code>\nAmount: ₹{checkout.get('amount', 0)}"

                ))

                

                asyncio.create_task(log_payment(

                    user_id=user_id,

                    user_first_name=user_info.get("first_name", "User"),

                    username=user_info.get('username', ''),

                    s_name=s_name,

                    amount=checkout.get("amount", 0),

                    method=method,

                    receipt_id=checkout.get("payment_id", ""),

                    pay_link=checkout.get("pay_link_copy", ""),

                    order_id=order_id,

                    user_last_name=user_info.get("last_name", "")

                ))

            

            # Re-fetch story to ensure we have channel/pool data for delivery options

            story = await db.db.premium_stories.find_one({"_id": ObjectId(s_id)})

            

            # Payment Receipt sending is DISABLED (user request)
            # from utils_invoice import send_invoice_to_user
            # asyncio.create_task(send_invoice_to_user(
            #     client=client,
            #     user_id=user_id,
            #     order_id=order_id,
            #     amount=checkout.get("amount", 0),
            #     method=method,
            #     story=story,
            #     checkout=checkout
            # ))

            

            return await dispatch_delivery_choice(client, user_id, story)

            

        else:

            await query.answer(f"Payment not completed yet (Status: {status}). Please pay and try again.", show_alert=True)

            # Revert to payment button state

            kb = [

                [InlineKeyboardButton(f"💳 {_sc('PAY VIA')} {_sc(method.upper())}", url=checkout.get("pay_link_copy", "https://t.me"))] if "pay_link_copy" in checkout else [],

                [InlineKeyboardButton(f"✅ {_sc('VERIFY PAYMENT')}", callback_data=f"mb#{method}_check#{s_id}")],

                [InlineKeyboardButton(f"« ❮ {_sc('BACK')}", callback_data="mb#return_main")]

            ]

            # Since url is not strictly saved, we might have lost it.

            # actually we didn't save the short url in db in the first replacement chunk. Let's fix that below if needed, but for now just show a simple back button.

            savage_msg = (

                f"<b>❌ 𝗣𝗮𝘆𝗺𝗲𝗻𝘁 𝗡𝗼𝘁 𝗙𝗼𝘂𝗻𝗱!</b>\n"

                f"<b>Status:</b> <code>{status}</code>\n\n"

                f"<i>Trying to sneak past without paying? Not happening! 😏</i>\n\n"

                f"If you actually paid and money was deducted, don't panic. Just take a breath, wait a minute, and verify again."

            )

            await m.edit_text(
                savage_msg,
                reply_markup=InlineKeyboardMarkup([[_ikb("Verify Again", callback_data=f"mb#{method}_check#{s_id}", icon_custom_emoji_id="5807492110059838726")], [InlineKeyboardButton("« Back", callback_data=f"mb#show_tc#{s_id}")]])
            )



    elif cmd in ("pay_link", "upi_uri"):

        # Copy SliceURL short link only (strict mode).

        s_id = data[2] if len(data) > 2 else None

        if not s_id:

            return await query.answer("Invalid request.", show_alert=True)

        from bson.objectid import ObjectId

        checkout = await db.db.premium_checkout.find_one(

            {"user_id": user_id, "bot_id": client.me.id, "story_id": ObjectId(s_id)}

        )

        link = (checkout or {}).get("pay_link_copy") or ""

        if not link and (checkout or {}).get("upi_uri"):

            link = await _sliceurl_api_shorten(checkout["upi_uri"])

        if not link:

            return await query.answer("SliceURL link unavailable. Check SLICEURL_API_URL/KEY and API deploy.", show_alert=True)

        await query.answer()

        await client.send_message(

            user_id,

            "🔗 <b>Payment link</b> — open in browser; it will launch UPI with amount filled.\n"

            f"<code>{link}</code>",

        )



    # ── Delivery choice (DM vs Channel) - handled via callbacks now ──

    elif cmd == "deliver_dm":
        s_id = data[2]
        part_id = data[3] if len(data) > 3 and not data[3].isdigit() else None

        from bson.objectid import ObjectId
        from bson.errors import InvalidId

        story = None
        try:
            story = await db.db.premium_stories.find_one({"_id": ObjectId(s_id)})
        except Exception:
            pass
        if not story:
            story = await db.db.premium_stories.find_one({"_id": s_id})
        if not story:
            story = await db.db.premium_stories.find_one({"story_id": s_id})

        if not story: return await query.answer("Story not found!", show_alert=True)

        part_info = None
        if part_id:
            for p in (story.get("parts") or []):
                if str(p.get("id")) == str(part_id):
                    part_info = p
                    break

        if not part_info:
            user_order = await db.db.orders.find_one({
                "user_id": {"$in": [user_id, str(user_id)]},
                "story_ids": {"$in": [s_id, str(story.get('_id', ''))]},
                "status": {"$in": ["paid", "delivered"]}
            }, sort=[("created_at", -1)])
            if user_order and user_order.get("items"):
                for itm in user_order["items"]:
                    if (itm.get("story_id") == s_id or itm.get("story_id") == str(story.get('_id', ''))) and itm.get("part_id"):
                        for sp in (story.get("parts") or []):
                            if str(sp.get("id")) == str(itm.get("part_id")):
                                part_info = sp
                                break
                        break

        if part_info and part_info.get("start_id") and part_info.get("end_id"):
            start_id = int(part_info["start_id"])
            end_id   = int(part_info["end_id"])
            is_ong   = bool(part_info.get("is_ongoing") or str(part_info.get("badge") or "").lower() == "ongoing")
            story_end_id = int(story.get("end_id") or story.get("end_message_id") or 0)
            if is_ong and story_end_id > end_id:
                end_id = story_end_id
            p_id_str = str(part_info.get("id"))
        else:
            start_id = story.get('start_id')
            end_id   = story.get('end_id')
            p_id_str = None

        valid_file_ids = None
        if story.get("valid_file_ids"):
            if part_info and start_id and end_id:
                valid_file_ids = [mid for mid in story["valid_file_ids"] if start_id <= mid <= end_id]
            else:
                valid_file_ids = story["valid_file_ids"]
        elif start_id and end_id:
            valid_file_ids = list(range(int(start_id), int(end_id) + 1))

        total_files = len(valid_file_ids) if valid_file_ids else ((end_id - start_id) + 1 if (start_id and end_id and end_id >= start_id) else 1)

        parts_data = len(data) > 3 and data[3].isdigit()

        if not parts_data and total_files > 40:
            if total_files > 300: chunk = 100
            elif total_files > 100: chunk = 50
            else: chunk = 30

            kb = []
            row = []
            for i in range(0, total_files, chunk):
                f_start = i + 1
                f_end = min(i + chunk, total_files)
                lbl = f"{f_start} - {f_end}"
                is_last_chunk = (f_end == total_files)
                icon_id = "6147506120920405501" if is_last_chunk else "5341492148468465410"
                row.append(_kb_btn(lbl, icon_custom_emoji_id=icon_id))
                if len(row) == 2:
                    kb.append(row)
                    row = []
            if row:
                kb.append(row)

            full_btn = "Full Delivery (All Files)" if lang != "hi" else "Full Delivery (सभी फ़ाइलें)"
            cancel_btn = "« " + ("Cancel" if lang != "hi" else "रद्द करें")
            kb.append([_kb_btn(full_btn, icon_custom_emoji_id="5805550320985578625")])
            kb.append([_kb_btn(cancel_btn)])

            set_state = {"dm_story_id_pending": s_id}
            if p_id_str:
                set_state["dm_part_id_pending"] = p_id_str
            else:
                set_state["dm_part_id_pending"] = None
            await db.db.users.update_one({"id": user_id}, {"$set": set_state})

            await query.answer()
            try: await query.message.delete()
            except: pass

            if lang == "hi":
                p_text = '<b><emoji id="6021620268697393273">ℹ️</emoji> फ़ाइलें चुनें:</b>\n\nआप कौन से भाग प्राप्त करना चाहते हैं? नीचे दिए गए मेन्यू बटन का उपयोग करें।'
            else:
                p_text = '<b><emoji id="6021620268697393273">ℹ️</emoji> Select Files:</b>\n\nWhich part would you like to receive? Please use the keyboard options below.'

            ok = await _send_reply_keyboard_bot_api(client, user_id, p_text, kb)
            if not ok:
                pyro_kb = [[b["text"] if isinstance(b, dict) else b for b in r] for r in kb]
                return await client.send_message(user_id, p_text, reply_markup=ReplyKeyboardMarkup(pyro_kb, resize_keyboard=True), parse_mode=enums.ParseMode.HTML)
            return

        c_start = int(data[3]) if (len(data) > 3 and data[3].isdigit()) else start_id
        c_end = int(data[4]) if (len(data) > 4 and data[4].isdigit()) else end_id

        await query.answer()
        await query.message.edit_text(
            f"<i>⏳ Initializing DM Delivery... Preparing your files.</i>",
            reply_markup=None
        )
        if valid_file_ids:
            asyncio.create_task(_do_dm_delivery(client, user_id, story, query.message, custom_msg_ids=valid_file_ids))
        else:
            asyncio.create_task(_do_dm_delivery(client, user_id, story, query.message, c_start, c_end))



    elif cmd == "cancel_dm":

        dm_aborts.add(user_id)

        await query.answer("⏹️ Stopping delivery...", show_alert=True)

        try:

            await query.message.edit_caption(

                "<b>⏹️ Delivery Stopped!</b>\n\n<i>Processing remaining requests and cleaning up...</i>",

                reply_markup=None

            )

        except:

            try: await query.message.edit_text("<b>⏹️ Delivery Stopped!</b>\n\n<i>Processing remaining requests...</i>", reply_markup=None)

            except: pass



    elif cmd == "deliver_channel":

        s_id = data[2]

        from bson.objectid import ObjectId

        story = await db.db.premium_stories.find_one({"_id": ObjectId(s_id)})

        if not story: return await query.answer("Story not found!", show_alert=True)

        await query.answer()

        await query.message.edit_text("<i>⏳ Generating secure 1-time channel link...</i>", reply_markup=None)

        asyncio.create_task(_do_channel_delivery(client, user_id, story, query.message))



    # ── Notify Admin ──

    elif cmd == "notify_admin":

        await query.answer("Admin has been notified. Please wait patiently.", show_alert=True)

        admins = Config.SUDO_USERS or Config.OWNER_IDS

        if not admins:

            return

        for admin in admins:

            try:

                await client.send_message(

                    admin,

                    f"🔔 <b>URGENT PING:</b>\nUser <code>{user_id}</code> is waiting for Payment Validation!\nCheck the Pending queue in Management Bot."

                )

            except Exception:

                pass





# ─────────────────────────────────────────────────────────────────

# Screenshot Handler

# ─────────────────────────────────────────────────────────────────

async def _process_screenshot(client, message, checkout=None):
    if not message or not message.from_user:
        return
    if getattr(message, "outgoing", False) or getattr(message.from_user, "is_bot", False) or message.from_user.id == client.me.id:
        return
    if not message.photo:
        return

    user_id = message.from_user.id
    user = await db.get_user(user_id, from_user=message.from_user)
    lang = user.get('lang', 'en')

    if not checkout:
        checkout = await db.db.premium_checkout.find_one(
            {"user_id": user_id, "bot_id": client.me.id, "status": "waiting_screenshot"}
        )
    if not checkout:
        return

    # Fake Detection
    p = message.photo
    if p.file_size < 50000:
        return await message.reply_text("❌ <b>Invalid Screenshot!</b>\n\nThe image is too small. Please send a clear, full payment screenshot.", quote=True)

    if p.height < p.width:
        return await message.reply_text("❌ <b>Invalid Screenshot!</b>\n\nPlease send a portrait-mode screenshot (not landscape).", quote=True)

    # Log verified payment screenshot with complete order details
    try:
        from utils import log_arya_event
        ui = {
            "first_name": getattr(message.from_user, "first_name", ""),
            "last_name": getattr(message.from_user, "last_name", ""),
            "username": getattr(message.from_user, "username", ""),
            "bot_id": client.me.id
        }
        s_name = checkout.get("story_name") or checkout.get("story_name_en") or "Story"
        amt = checkout.get("amount") or 0
        o_id = checkout.get("order_id") or str(checkout.get("_id", ""))
        asyncio.create_task(log_arya_event(
            "PAYMENT SCREENSHOT",
            message.from_user.id,
            ui,
            f"User uploaded payment screenshot for: <b>{s_name}</b>\nAmount: ₹{amt}\nOrder ID: <code>{o_id}</code>",
            bot_id=client.me.id
        ))
    except Exception:
        pass



    kb_user = [[InlineKeyboardButton(f"✆ {_sc('NOTIFY ADMIN')}", callback_data="mb#notify_admin")]]

    txt_user = (

        f"⏳ <b>{_sc('Your payment is being verified') if lang != 'hi' else 'आपके भुगतान की पुष्टि की जा रही है'}</b>\n"

        "<blockquote expandable>\n"

        f"<i>{_sc('Please wait (approx 5 minutes)...') if lang != 'hi' else 'कृपया प्रतीक्षा करें (लगभग 5 मिनट)...'}</i>\n\n"

        f"<b>{_sc('Time Remaining') if lang != 'hi' else 'शेष समय'} :</b> 05:00\n"

        "</blockquote>"

    )

    msg = await message.reply_text(txt_user, reply_markup=InlineKeyboardMarkup(kb_user))



    import os, hashlib

    if not os.path.exists("downloads"): os.makedirs("downloads")

    file_path = await client.download_media(message, file_name=f"downloads/proof_{checkout['_id']}.jpg")

    

    # Hash for duplicate detection

    with open(file_path, "rb") as f:

        file_hash = hashlib.sha256(f.read()).hexdigest()

        

    dup = await db.db.premium_checkout.find_one({"proof_hash": file_hash, "status": {"$in": ["pending_admin_approval", "approved"]}})

    if dup:

        os.remove(file_path)

        return await message.reply_text("❌ <b>Fraud Detected!</b>\n\nThis exact screenshot has already been used. Please provide a genuine, new payment screenshot or contact support.", quote=True)



    await db.db.premium_checkout.update_one(

        {"_id": checkout["_id"]},

        {"$set": {

            "status": "pending_admin_approval",

            "proof_path": file_path,

            "proof_hash": file_hash,

            "proof_file_id": message.photo.file_id,

            "status_msg_id": msg.id,

            "paid_at": datetime.utcnow(),

            "updated_at": datetime.utcnow(),

        }}

    )

    if db.mgmt_client:

        try:

            from config import Config

            admins = Config.SUDO_USERS or Config.OWNER_IDS

            from bson.objectid import ObjectId

            story = await db.db.premium_stories.find_one({"_id": ObjectId(str(checkout['story_id']))})

            s_name = story.get("story_name_en") if story else "Unknown"

            price = story.get("price", "0") if story else "0"

            

            kb_admin = [

                [InlineKeyboardButton("✅ Approve", callback_data=f"mk#approve#{checkout['_id']}"),

                 InlineKeyboardButton("❌ Reject", callback_data=f"mk#reject#{checkout['_id']}")]

            ]

            caption = (

                f"<b>🚨 NEW PAYMENT REQUEST</b>\n"

                f"────────────────────\n"

                f"<b>User:</b> {message.from_user.first_name} (<code>{user_id}</code>)\n"

                f"<b>Story:</b> {s_name}\n"

                f"<b>Amount:</b> ₹{price}\n"

                f"<b>Method:</b> Manual UPI\n"

                f"<b>Date:</b> {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"

                f"────────────────────\n"

                f"<i>Please verify the exact amount in the screenshot.</i>"

            )

            for admin_id in admins:

                try: await db.mgmt_client.send_photo(admin_id, photo=file_path, caption=caption, reply_markup=InlineKeyboardMarkup(kb_admin))

                except Exception: pass

        except Exception as e:

            logger.error(f"Mgmt DM send error: {e}")



    async def _live_timer(target_msg, remaining, checkout_id_str):

        try:

            for i in range(remaining, 0, -10):

                await asyncio.sleep(10)

                from bson.objectid import ObjectId

                chk = await db.db.premium_checkout.find_one({"_id": ObjectId(checkout_id_str)})

                if not chk or chk.get("status") != "pending_admin_approval":

                    break

                

                m = i // 60

                s = i % 60

                await target_msg.edit_text(

                    (f"⏳ <b>{_sc('Your payment is being verified') if lang != 'hi' else 'आपके भुगतान की पुष्टि की जा रही है'}</b>\n"

                     "<blockquote expandable>\n"

                     f"<i>{_sc('Please wait (approx 5 minutes)...') if lang != 'hi' else 'कृपया प्रतीक्षा करें (लगभग 5 मिनट)...'}</i>\n\n"

                     f"<b>{_sc('Time Remaining') if lang != 'hi' else 'शेष समय'} :</b> {m:02d}:{s:02d}\n"

                     "</blockquote>"),

                    reply_markup=InlineKeyboardMarkup(kb_user)

                )

            await target_msg.edit_text(

                f"⏳ <b>{_sc('Verification Timeout')}</b>\n\n<i>{_sc('If your payment is valid, it will be approved soon. For urgent queries, please contact Support.')}</i>",

                reply_markup=InlineKeyboardMarkup(kb_user)

                )

        except Exception: pass

    asyncio.create_task(_live_timer(msg, 300, str(checkout['_id'])))





# ─────────────────────────────────────────────────────────────────

# Delivery Choice (now fully Inline — no native_ask needed!)

# ─────────────────────────────────────────────────────────────────

async def dispatch_delivery_choice(client, user_id, story, part_info=None):
    user = await db.get_user(user_id)
    lang = user.get('lang', 'en')
    story_id_str = str(story['_id'])

    try:
        from utils import log_arya_event
        p_extra = f" (Part: {part_info.get('name')})" if part_info else ""
        ui = {
            "first_name": user.get("first_name", ""),
            "last_name": user.get("last_name", ""),
            "username": user.get("username", ""),
            "bot_id": client.me.id
        }
        asyncio.create_task(log_arya_event(
            "DELIVERY REQUEST",
            user_id,
            ui,
            f"User requested delivery options for story: {story.get('story_name_en', 'Unknown')}{p_extra}",
            bot_id=client.me.id
        ))
    except Exception: pass

    used_channels = user.get("used_channels", [])
    mode = story.get("delivery_mode") or ("single" if story.get("channel_id") else "pool")
    fallback = await db.db.premium_channels.find_one({"type": "delivery"})
    pool = story.get("channel_pool") or []
    has_any_delivery = bool(story.get('channel_id') or pool or (fallback and fallback.get("channel_id")))
    
    # Parts delivery is always DM-only
    can_use_channel = (story_id_str not in used_channels) and (mode != "dm_only") and has_any_delivery and (part_info is None)

    # Find purchase source to display
    from bson.objectid import ObjectId
    purchase = await db.db.premium_purchases.find_one({"user_id": int(user_id), "story_id": story.get("_id")})
    method_info = "Verified Purchase"
    if purchase:
        src = str(purchase.get("source", "manual")).lower()
        if src == "razorpay": method_info = "💳 Razorpay"
        elif src == "easebuzz": method_info = "💸 Easebuzz"
        elif src == "upi": method_info = "🏦 Manual UPI"
        else: method_info = f"🛒 {src.capitalize()}"

    s_name = story.get(f'story_name_{lang}', story.get('story_name_en'))
    if part_info:
        p_label = part_info.get("name", f"Part {part_info.get('id')}")
        s_name = f"{s_name} · {p_label}"

    if lang == 'hi':
        del_txt = (
            '<b><emoji id="6129783634158163466">✅</emoji> एक्सेस मिल गया है!</b>\n\n'
            f"<b>स्टोरी:</b> {s_name}\n"
            + (f"<b>भुगतान तरीका:</b> {method_info}\n" if method_info else "")
            + "\n"
            + '<b><emoji id="6021620268697393273">ℹ️</emoji> डिलीवरी की जानकारी</b>\n\n'
            + "<blockquote>• <b>DM डिलीवरी:</b> फाइलें सीधे यहां भेजी जाती हैं। उन्हें तुरंत सेव या फॉरवर्ड करें—वे कुछ समय बाद अपने आप डिलीट हो जाती हैं।</blockquote>\n"
            + "<blockquote>• <b>चैनल लिंक:</b> एक वन-टाइम प्राइवेट इनवाइट लिंक जेनरेट किया जाता है। प्रत्येक स्टोरी के लिए केवल एक चैनल लिंक की अनुमति है।</blockquote>\n"
            + "<blockquote>• <b>लाइफटाइम एक्सेस:</b> आप किसी भी खरीदी हुई स्टोरी को कभी भी <b>मुख्य मेनू ⟶ मेरी स्टोरीज</b> से एक्सेस कर सकते हैं।</blockquote>\n"
            + "──────────────\n\n"
            + "आप अपनी फाइलें कैसे प्राप्त करना चाहेंगे?"
        )
        dm_btn_txt = "⤓ DM में प्राप्त करें"
        chan_btn_txt = "➦ चैनल लिंक प्राप्त करें"
        back_btn_txt = "« ❮ मुख्य मेनू"
    else:
        del_txt = (
            '<b><emoji id="6129783634158163466">✅</emoji> Access Granted!</b>\n\n'
            f"<b>Product:</b> {s_name}\n"
            + (f"<b>Method:</b> {method_info}\n" if method_info else "")
            + "\n"
            + '<b><emoji id="6021620268697393273">ℹ️</emoji> Delivery Info</b>\n\n'
            + "<blockquote>• <b>DM Delivery:</b> Files are sent directly here. Save or forward them immediately—they auto-delete after some time.</blockquote>\n"
            + "<blockquote>• <b>Channel Link:</b> A one-time private invite link is generated. Each story allows only one channel link per account.</blockquote>\n"
            + "<blockquote>• <b>Lifetime Access:</b> You can re-access any purchased story anytime from <b>Main Menu ⟶ My Stories</b>.</blockquote>\n"
            + "──────────────\n\n"
            + "How would you like to receive your files?"
        )
        dm_btn_txt = f"⤓ {_sc('RECEIVE IN DM')}"
        chan_btn_txt = f"➦ {_sc('ACCESS CHANNEL LINK')}"
        back_btn_txt = f"« ❮ {_sc('MAIN MENU')}"

    dm_cb = f"mb#deliver_dm#{story_id_str}#{part_info.get('id')}" if part_info else f"mb#deliver_dm#{story_id_str}"
    kb = [[InlineKeyboardButton(dm_btn_txt, callback_data=dm_cb)]]
    if can_use_channel:
        kb.append([InlineKeyboardButton(chan_btn_txt, callback_data=f"mb#deliver_channel#{story_id_str}")])
    kb.append([InlineKeyboardButton(back_btn_txt, callback_data="mb#main_back")])



    # Forwarding protection notice

    fwd_enabled = story.get('forwarding_enabled', True)

    if not fwd_enabled:

        protect_note = (

            "\n\n<blockquote><b>🔒 Forwarding Disabled:</b> Files for this story are <b>protected</b>. Screenshots and forwarding are not allowed.</blockquote>"

            if lang != 'hi' else

            "\n\n<blockquote><b>🔒 फॉरवर्डिंग बंद है:</b> इस स्टोरी की फाइलें <b>सुरक्षित</b> हैं। स्क्रीनशॉट और फॉरवर्डिंग की अनुमति नहीं है।</blockquote>"

        )

        del_txt += protect_note



    await client.send_message(user_id, del_txt, reply_markup=InlineKeyboardMarkup(kb))



async def _auto_delete_demo(client, user_id, msg_ids):

    import asyncio

    await asyncio.sleep(300)

    for mid in msg_ids:

        try:

            await client.delete_messages(user_id, mid)

        except Exception:

            pass



async def _safe_copy_from_source(client, chat_id: int, from_chat_id: int, message_id: int, protect_content: bool = False, caption: str = None):
    """
    Safely copies a message from a source DB channel to the user.
    1. First tries client.copy_message directly.
    2. If client lacks permission (e.g. 2nd bot is not in the source channel), fetches
       the message using Management Bot (db.mgmt_client) or any Store Bot (market_clients)
       that has channel admin access, and then sends the file/media directly to the user!
    """
    kwargs = {
        "chat_id": chat_id,
        "from_chat_id": int(from_chat_id),
        "message_id": int(message_id),
        "protect_content": protect_content,
    }
    if caption:
        kwargs["caption"] = caption

    # 1. Try directly with current delivery bot client
    try:
        return await client.copy_message(**kwargs)
    except Exception as e1:
        err1 = str(e1).upper()
        if "MESSAGE_ID_INVALID" in err1 or "MESSAGE_EMPTY" in err1 or "MESSAGE NOT FOUND" in err1:
            raise e1

    # 2. Gather all fallback clients that might have channel access
    all_fallback_clients = []
    mgmt_cli = getattr(db, "mgmt_client", None)
    if mgmt_cli and getattr(mgmt_cli, "is_connected", False) and mgmt_cli != client:
        all_fallback_clients.append(mgmt_cli)
    for c in market_clients.values():
        if c != client and getattr(c, "is_connected", False):
            all_fallback_clients.append(c)

    # 3. Fetch source message from channel via fallback client, and deliver via client using file_id!
    for fallback_cli in all_fallback_clients:
        try:
            src_msg = await fallback_cli.get_messages(int(from_chat_id), int(message_id))
            if not src_msg or src_msg.empty:
                continue

            final_cap = caption if caption is not None else (src_msg.caption or "")

            # Send based on media type using client (the bot the user is chatting with)
            if src_msg.audio:
                return await client.send_audio(
                    chat_id=chat_id,
                    audio=src_msg.audio.file_id,
                    caption=final_cap,
                    duration=src_msg.audio.duration,
                    performer=src_msg.audio.performer,
                    title=src_msg.audio.title,
                    protect_content=protect_content
                )
            elif src_msg.document:
                return await client.send_document(
                    chat_id=chat_id,
                    document=src_msg.document.file_id,
                    caption=final_cap,
                    protect_content=protect_content
                )
            elif src_msg.video:
                return await client.send_video(
                    chat_id=chat_id,
                    video=src_msg.video.file_id,
                    caption=final_cap,
                    duration=src_msg.video.duration,
                    width=src_msg.video.width,
                    height=src_msg.video.height,
                    protect_content=protect_content
                )
            elif src_msg.photo:
                return await client.send_photo(
                    chat_id=chat_id,
                    photo=src_msg.photo.file_id,
                    caption=final_cap,
                    protect_content=protect_content
                )
            elif src_msg.voice:
                return await client.send_voice(
                    chat_id=chat_id,
                    voice=src_msg.voice.file_id,
                    caption=final_cap,
                    duration=src_msg.voice.duration,
                    protect_content=protect_content
                )
            elif src_msg.video_note:
                return await client.send_video_note(
                    chat_id=chat_id,
                    video_note=src_msg.video_note.file_id,
                    duration=src_msg.video_note.duration,
                    protect_content=protect_content
                )
            elif src_msg.animation:
                return await client.send_animation(
                    chat_id=chat_id,
                    animation=src_msg.animation.file_id,
                    caption=final_cap,
                    duration=src_msg.animation.duration,
                    protect_content=protect_content
                )
            elif src_msg.text:
                return await client.send_message(
                    chat_id=chat_id,
                    text=src_msg.text,
                    protect_content=protect_content
                )
            else:
                return await fallback_cli.copy_message(**kwargs)
        except Exception as fb_err:
            logger.debug(f"[MultiBotBridge] Fallback client {getattr(fallback_cli, 'name', 'bot')} failed for msg {message_id}: {fb_err}")
            continue

    raise e1


async def _send_demo_files(client, user_id, story, lang):
    import asyncio

    # ── Show Store OTT Mode: Send Universal Demo Sample with Explanatory Notice ──
    if story.get("is_show") or story.get("duration"):
        msg_ids = []
        try:
            demo_notice = (
                f'<emoji id="5305388752162539722">👁️</emoji> <b>Demo Preview File</b>\n'
                f"────────────────────\n"
                f"ℹ️ <i>This is a demo sample file provided for previewing video quality, audio clarity, and format.</i>\n\n"
                f"📌 <b>Important Note:</b>\n"
                f"<i>When you purchase this show, you will receive the complete full-length story with all episodes seamlessly combined into a single high-quality video file, exactly formatted like this sample.</i>\n"
                f"────────────────────\n"
                f"⏳ <i>This preview file will be automatically deleted after 5 minutes.</i>"
            )
            m_notice = await client.send_message(user_id, demo_notice, protect_content=True, parse_mode=enums.ParseMode.HTML)
            msg_ids.append(m_notice.id)

            parts = story.get("parts", [])
            ch_id = story.get("channel_id")
            if parts and ch_id:
                mid = parts[0].get("msg_id")
                if mid:
                    try:
                        sent = await _safe_copy_from_source(client, chat_id=user_id, from_chat_id=int(ch_id), message_id=int(mid), protect_content=True)
                        msg_ids.append(sent.id)
                    except Exception as e:
                        logger.warning(f"Failed copying show demo file: {e}")

            # Auto-delete demo files after 5 minutes (300 seconds)
            await asyncio.sleep(300)
            for dmid in msg_ids:
                try: await client.delete_messages(user_id, dmid)
                except Exception: pass
        except Exception as e:
            logger.error(f"Error in show _send_demo_files: {e}")
        return

    start = story.get("start_id")
    end = story.get("end_id")
    src = story.get("source")

    if not start or not end or not src:
        await client.send_message(user_id, "❌ Demo not available for this story.")
        return

    start, end, src = int(start), int(end), int(src)
    total = (end - start) + 1
    msg_ids = []

    

    try:

        if lang == "hi":
            txt = '<emoji id="5210956306952758910">👀</emoji> <b>डेमो फ़ाइलें भेजी जा रही हैं...</b>\n\n<i>नोट: एपिसोड हमेशा अलग-अलग नहीं दिए जाते हैं; वे ग्रुप फॉर्मेट/बड़ी फाइल में भी हो सकते हैं, इसलिए कृपया इसे ध्यान में रखें।\nये डेमो फाइल्स 5 मिनट बाद सख्ती से अपने आप डिलीट हो जाएंगी।</i>'
        else:
            txt = '<emoji id="5210956306952758910">👀</emoji> <b>Sending Demo Files...</b>\n\n<i>Note: Episodes are not necessarily provided separately; they may also be delivered in a group format, so please keep that in mind.\nThese demo files will be auto-deleted strictly after 5 minutes.</i>'

        m = await client.send_message(user_id, txt, protect_content=True, parse_mode=enums.ParseMode.HTML)
        msg_ids.append(m.id)

        lbl_start = '<emoji id="5224473711494581672">1️⃣</emoji> <b>स्टार्टिंग फ़ाइलें (शुरुआत)</b> <emoji id="6147439566107186310">👇</emoji>' if lang == "hi" else '<emoji id="5224473711494581672">1️⃣</emoji> <b>STARTING FILES</b> <emoji id="6147439566107186310">👇</emoji>'
        lbl_end = f'<emoji id="5224251017440285983">2️⃣</emoji> <b>अंतिम फ़ाइल (कुल: {total} फ़ाइलें)</b> <emoji id="6147439566107186310">👇</emoji>' if lang == "hi" else f'<emoji id="5224251017440285983">2️⃣</emoji> <b>ENDING FILE (Total: {total} files)</b> <emoji id="6147439566107186310">👇</emoji>'

        m_s = await client.send_message(user_id, lbl_start, protect_content=True, parse_mode=enums.ParseMode.HTML)
        msg_ids.append(m_s.id)

        

        s_count = 2 if total >= 2 else 1

        for mid in range(start, start + s_count):

            sent = await _safe_copy_from_source(client, chat_id=user_id, from_chat_id=src, message_id=mid, protect_content=True)

            msg_ids.append(sent.id)

            await asyncio.sleep(0.5)

            

        if total > 2:

            m_e = await client.send_message(user_id, lbl_end, protect_content=True, parse_mode=enums.ParseMode.HTML)

            msg_ids.append(m_e.id)

            sent_end = await _safe_copy_from_source(client, chat_id=user_id, from_chat_id=src, message_id=end, protect_content=True)

            msg_ids.append(sent_end.id)

            

        asyncio.create_task(_auto_delete_demo(client, user_id, msg_ids))



    except Exception as e:

        import logging

        logging.getLogger(__name__).error(f"Demo failed: {e}")



async def _do_dm_delivery(client, user_id, story, status_msg=None, part_start=None, part_end=None, custom_msg_ids=None):
    try:
        dm_aborts.discard(user_id)
        bt = await db.db.premium_bots.find_one({"id": client.me.id})
        bt_cfg = bt.get("config", {}) if bt else {}
        user_obj = await get_robust_user(client, user_id)
        src = story.get('source')
        start = part_start if part_start else story.get('start_id')
        end = part_end if part_end else story.get('end_id')
        story_id_str = str(story['_id'])
        
        if custom_msg_ids is not None:
            msg_range = custom_msg_ids
        elif story.get('valid_file_ids'):
            val_ids = story['valid_file_ids']
            if part_start and part_end:
                ps = min(int(part_start), int(part_end))
                pe = max(int(part_start), int(part_end))
                msg_range = [mid for mid in val_ids if ps <= mid <= pe]
            else:
                msg_range = val_ids
        else:
            if not src or not start or not end:
                await client.send_message(user_id, "❌ Story file range is not configured correctly. Please contact admin.")
                return
            msg_range = list(range(int(start), int(end) + 1))

        if not msg_range:
            await client.send_message(user_id, "❌ No valid files found in this selection. Please contact support.")
            return

        # Fetching Message with Media & Cancel Button
        fetch_config = bt_cfg.get("fetching_media")
        fetch_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⛔ CANCEL DELIVERY", callback_data=f"mb#cancel_dm#{story_id_str}")]])
        fetch_text = f"<b>⏳ Starting DM Delivery...</b>\n\n<i>Please wait while we fetch and deliver your files. This may take a few moments.</i>"
        
        fetch_msg = None
        if fetch_config:
            try:
                # Optimized multi-media handling (Photo/GIF/Video)
                f_id = fetch_config.get("file_id") if isinstance(fetch_config, dict) else fetch_config
                f_type = fetch_config.get("type", "photo") if isinstance(fetch_config, dict) else "photo"

                if f_type == "photo":
                    fetch_msg = await client.send_photo(user_id, photo=f_id, caption=fetch_text, reply_markup=fetch_kb)
                elif f_type == "animation":
                    fetch_msg = await client.send_animation(user_id, animation=f_id, caption=fetch_text, reply_markup=fetch_kb)
                elif f_type == "video":
                    fetch_msg = await client.send_video(user_id, video=f_id, caption=fetch_text, reply_markup=fetch_kb)
                
                if fetch_msg and status_msg: await status_msg.delete()
            except Exception: pass
        
        # Fallback to Story image or random menu image if custom fetching media fails/not set
        if not fetch_msg:
            alt_media = story.get("image") or bt_cfg.get("menuimg")
            if alt_media:
                try:
                    fetch_msg = await client.send_photo(user_id, photo=alt_media, caption=fetch_text, reply_markup=fetch_kb)
                    if status_msg: await status_msg.delete()
                except Exception: pass

        if not fetch_msg:
            if status_msg:
                try: fetch_msg = await status_msg.edit_text(fetch_text, reply_markup=fetch_kb)
                except: fetch_msg = await client.send_message(user_id, fetch_text, reply_markup=fetch_kb)
            else:
                fetch_msg = await client.send_message(user_id, fetch_text, reply_markup=fetch_kb)

        try:
            autodel = int(str(bt_cfg.get("autodel", "0")).strip() or "0")
        except Exception:
            autodel = 0

        sent_count = 0
        failed_count = 0
        sent_ids = []
        deleted_ids = []
        cap_tpl = bt_cfg.get("caption", "")
        
        aborted = False
        total_eps = len(msg_range)
        
        for idx, msg_id in enumerate(msg_range, start=1):
            if user_id in dm_aborts:
                aborted = True
                break
            
            # Progress update
            if idx % 10 == 0 or idx == 1:
                try:
                    p_text = f"<b>⏳ Delivering Files... ({idx}/{total_eps})</b>\n\n<i>Processing your request, please stay tuned.</i>"
                    if fetch_msg.caption:
                        await fetch_msg.edit_caption(p_text, reply_markup=fetch_kb)
                    else:
                        await fetch_msg.edit_text(p_text, reply_markup=fetch_kb)
                except Exception: pass

            try:
                kwargs = dict(
                    chat_id=user_id,
                    from_chat_id=int(src),
                    message_id=msg_id,
                    protect_content=bt_cfg.get("protect", False) or not story.get('forwarding_enabled', True),
                )
                if cap_tpl:
                    my_kwargs = dict(kwargs)
                    if "{original_caption}" in cap_tpl or "{file_name}" in cap_tpl:
                        try:
                            orig_msg = None
                            try:
                                orig_msg = await client.get_messages(int(src), msg_id)
                            except Exception:
                                from plugins.mgmt.market_mgmt import client as mgmt_cli
                                if mgmt_cli and getattr(mgmt_cli, "is_connected", False):
                                    orig_msg = await mgmt_cli.get_messages(int(src), msg_id)
                            orig_cap = (orig_msg.caption or orig_msg.text or "") if orig_msg else ""
                            doc = getattr(orig_msg, "document", None) or getattr(orig_msg, "video", None) or getattr(orig_msg, "audio", None)
                            fname = getattr(doc, "file_name", "") or ""
                            my_kwargs["caption"] = _fmt_delivery_text(cap_tpl, user_obj, story).replace("{original_caption}", orig_cap).replace("{file_name}", fname)
                        except Exception:
                            my_kwargs["caption"] = _fmt_delivery_text(cap_tpl, user_obj, story).replace("{original_caption}", "").replace("{file_name}", "")
                    else:
                        my_kwargs["caption"] = _fmt_delivery_text(cap_tpl, user_obj, story)
                    sent = await _safe_copy_from_source(client, **my_kwargs)
                else:
                    sent = await _safe_copy_from_source(client, **kwargs)

                sent_ids.append(sent.id)
                sent_count += 1
            except Exception as e:
                err_str = str(e).lower()
                if "message_id_invalid" in err_str or "message_empty" in err_str or "message not found" in err_str:
                    logger.debug(f"DM Delivery skipped deleted/empty msg {msg_id}")
                    deleted_ids.append(msg_id)
                else:
                    logger.warning(f"DM Delivery failed msg {msg_id}: {e}")
                    failed_count += 1

            await asyncio.sleep(0.08)

        # Real-time background self-healing: remove discovered dead IDs from valid_file_ids in MongoDB
        if deleted_ids and story_id_str:
            async def _heal_dead_ids():
                try:
                    from bson.objectid import ObjectId
                    s_oid = ObjectId(story_id_str)
                    st_doc = await db.db.premium_stories.find_one({"_id": s_oid})
                    if st_doc and st_doc.get("valid_file_ids"):
                        updated_valid = [x for x in st_doc["valid_file_ids"] if x not in deleted_ids]
                        await db.db.premium_stories.update_one(
                            {"_id": s_oid},
                            {"$set": {"valid_file_ids": updated_valid, "file_count": len(updated_valid)}}
                        )
                except Exception:
                    pass
            asyncio.create_task(_heal_dead_ids())



        # Finalize and Cleanup

        try: await fetch_msg.delete()

        except: pass

        dm_aborts.discard(user_id)



        s_name = story.get('story_name_en', 'Story')

        rep_tpl = (bt_cfg.get("delivery_report") or "").strip()

        

        if autodel <= 0:

            time_str = "Disabled"

        elif autodel < 60:

            time_str = f"{autodel} seconds"

        elif autodel % 3600 == 0:

            time_str = f"{autodel // 3600} hours"

        elif autodel % 60 == 0:

            time_str = f"{autodel // 60} minutes"

        else:

            time_str = f"{autodel} seconds"



        status_text = "Important" if not aborted else "Stopped"

        autodel_text = (
            f"⏳ <b>{_sc('Auto-Delete')}:</b> {_sc('Due to copyright, all messages will auto-delete after')} <b>{time_str}</b>. "
            f"{_sc('To re-access anytime, go to')} <b>{_sc('Main Menu')} ⟶ {_sc('My Stories')}</b>."
        ) if autodel > 0 else f'<emoji id="6120635817674149717">✅</emoji> <b>{_sc("Files Delivered")}</b> — {_sc("All sent files are now available below.")}'

        if rep_tpl:
            summary = _fmt_delivery_text(
                rep_tpl,
                user_obj,
                story,
                sent_count=sent_count,
                fail_count=failed_count,
            ).replace("{time}", time_str).replace("DELIVERY COMPLETE", _sc(status_text))
        else:
            if aborted:
                summary = (
                    f"⏹️ <b>{_sc('DELIVERY STOPPED')}</b>\n\n"
                    f"• {_sc('Files Sent')}: <b>{sent_count}</b>\n"
                    f"• {_sc('Failed')}: <b>{failed_count}</b>\n\n"
                    f"<i>{_sc('Delivery was cancelled. Files sent so far are available above.')}</i>"
                )
            else:
                summary = (
                    f'<emoji id="6120635817674149717">✅</emoji> <b>{_sc("DELIVERY COMPLETE")}!</b>\n\n'
                    f'<emoji id="6023694913995020551">📦</emoji> <b>{sent_count}</b> {_sc("file(s) delivered successfully")}.'
                    + (f"\n⚠️ <b>{failed_count}</b> {_sc('file(s) could not be sent.')}" if failed_count > 0 else "")
                    + f"\n\n{autodel_text}\n\n"
                    + f'<blockquote><emoji id="6021620268697393273">💡</emoji> <b>{_sc("Tip")}:</b> {_sc("Files missing or something went wrong? Use the Regenerate button below or contact us via")} <b>Arya Premium Chat [ Help ]</b>.</blockquote>'
                )

        kb_regen = [
            [_ikb(_sc("Regenerate Files"), callback_data=f"mb#deliver_dm#{story_id_str}", icon_custom_emoji_id="5807492110059838726")],
            [_ikb("Arya Premium Chat [ Help ]", url="https://t.me/+gFudInzITpo1Yjg1", icon_custom_emoji_id="6104800784354909891")],
        ]

        ok = await _send_or_edit_seller_bot_api(
            client=client,
            chat_id=user_id,
            text=summary,
            markup=InlineKeyboardMarkup(kb_regen)
        )
        if not ok:
            notice = await client.send_message(user_id, summary, reply_markup=InlineKeyboardMarkup(kb_regen), parse_mode=enums.ParseMode.HTML)



        from utils import log_delivery, log_arya_event

        

        from bson.objectid import ObjectId

        purchase = await db.db.premium_purchases.find_one({"user_id": int(user_id), "story_id": ObjectId(story_id_str)})

        order_id = purchase.get("order_id", "") if purchase else ""

        logged_deliveries = purchase.get("logged_deliveries", []) if purchase else []

        username = user_obj.username if user_obj else ""

        last_name = user_obj.last_name if user_obj else ""

        

        asyncio.create_task(log_arya_event(

            event_type="DELIVERY INITIATED",

            user_id=user_id,

            user_info={"first_name": user_obj.first_name if user_obj else "User", "last_name": last_name, "username": username},

            details=f"Story: {s_name}\nMethod: DM\nOrder ID: <code>{order_id}</code>\nStatus: Sent {sent_count}, Failed {failed_count}\nAuto-delete: {time_str}"

        ))



        if purchase:
            res = await db.db.premium_purchases.update_one(
                {"_id": purchase["_id"], "logged_deliveries": {"$ne": "dm"}},
                {"$addToSet": {"logged_deliveries": "dm"}}
            )
            if res.modified_count > 0:
                asyncio.create_task(log_delivery(
                    bot_username=client.me.username,
                    user_id=user_id,
                    user_first_name=user_obj.first_name if user_obj else "Unknown",
                    s_name=s_name,
                    d_type="dm",
                    status=f"Sent {sent_count}, Failed {failed_count}",
                    username=username,
                    order_id=order_id,
                    user_last_name=last_name,
                    bot_id=client.me.id
                ))



        if autodel > 0 and sent_ids:

            asyncio.create_task(_delete_later(client, user_id, sent_ids, autodel))



    except Exception as e:

        logger.error(f"DM Delivery error: {e}")

        await client.send_message(user_id, f"❌ Delivery failed: {e}\n\nPlease contact admin.")





# ─────────────────────────────────────────────────────────────────

# Channel Link Delivery

# ─────────────────────────────────────────────────────────────────

async def _do_channel_delivery(client, user_id, story, status_msg=None):

    user = await db.get_user(user_id)

    lang = user.get('lang', 'en')

    story_id_str = str(story['_id'])

    try:

        bt = await db.db.premium_bots.find_one({"id": client.me.id})

        bt_cfg = bt.get("config", {}) if bt else {}

        mode = story.get("delivery_mode") or ("single" if story.get("channel_id") else "pool")

        if mode == "dm_only":

            await client.send_message(user_id, "ℹ️ This story is configured for DM delivery only.")

            return await _do_dm_delivery(client, user_id, story)



        # Build candidate pool for rotation/failover

        candidates = []

        if story.get("channel_id"):

            candidates.append(int(story["channel_id"]))

        if isinstance(story.get("channel_pool"), list):

            for cid in story["channel_pool"]:

                try:

                    candidates.append(int(cid))

                except Exception:

                    pass

        if not candidates:

            # fallback to global delivery list

            globals_ = await db.db.premium_channels.find({"type": "delivery"}).to_list(length=300)

            candidates = [int(c["channel_id"]) for c in globals_]



        if not candidates:

            return await client.send_message(user_id, "❌ No delivery channels are configured for this story. Please use DM Delivery instead.")



        # Round-robin start index (stored in premium_settings)

        try:

            rr = await db.db.premium_settings.find_one_and_update(

                {"_id": "delivery_rr"},

                {"$inc": {"idx": 1}},

                upsert=True,

                return_document=True,

            )

            start_idx = int((rr or {}).get("idx", 0)) % len(candidates)

        except Exception:

            start_idx = 0



        def _rot(lst, s):

            return lst[s:] + lst[:s]



        channel_id = None

        last_err = None

        for cid in _rot(candidates, start_idx)[: min(25, len(candidates))]:

            try:

                # Force Pyrogram to resolve and cache the peer before attempting invite link

                try: await client.get_chat(int(cid))

                except Exception: pass

                

                # Validate bot can access/create link

                invite_link = await client.create_chat_invite_link(

                    int(cid),

                    member_limit=1,

                    name=f"user_{user_id}"

                )

                channel_id = int(cid)

                break

            except Exception as e:

                last_err = e

                continue



        if not channel_id:

            return await client.send_message(user_id, f"❌ Failed to generate invite link from delivery pool. Please use DM Delivery instead.\n<i>{last_err}</i>")



        # We already created invite_link in loop above when selecting channel_id

        # Create again to get the link object (safe if already created)

        invite_link = await client.create_chat_invite_link(int(channel_id), member_limit=1, name=f"user_{user_id}")

        

        from utils import log_delivery, log_arya_event

        user_obj = await get_robust_user(client, user_id)

        

        from bson.objectid import ObjectId

        purchase = await db.db.premium_purchases.find_one({"user_id": int(user_id), "story_id": ObjectId(story_id_str)})

        order_id = purchase.get("order_id", "") if purchase else ""

        logged_deliveries = purchase.get("logged_deliveries", []) if purchase else []

        username = user_obj.username if user_obj else ""

        last_name = user_obj.last_name if user_obj else ""

        s_name_h = story.get('story_name_en', 'Unknown Story')

        

        asyncio.create_task(log_arya_event(

            event_type="DELIVERY INITIATED",

            user_id=user_id,

            user_info={"first_name": user_obj.first_name if user_obj else "User", "last_name": last_name, "username": username},

            details=f"Story: {s_name_h}\nMethod: CHANNEL\nOrder ID: <code>{order_id}</code>\nGenerated Link Channel: {channel_id}"

        ))



        if purchase:
            res = await db.db.premium_purchases.update_one(
                {"_id": purchase["_id"], "logged_deliveries": {"$ne": "channel"}},
                {"$addToSet": {"logged_deliveries": "channel"}}
            )
            if res.modified_count > 0:
                asyncio.create_task(log_delivery(
                    bot_username=client.me.username,
                    user_id=user_id,
                    user_first_name=user_obj.first_name if user_obj else "Unknown",
                    s_name=s_name_h,
                    d_type="channel",
                    status=f"Link created (Channel ID: {channel_id})",
                    username=username,
                    order_id=order_id,
                    user_last_name=last_name,
                    bot_id=client.me.id
                ))



        # Mark this user as having used their channel link

        await db.db.users.update_one(

            {"id": int(user_id)},

            {"$addToSet": {"used_channels": story_id_str}}

        )



        s_name = story.get('story_name_en', 'Story')

        suc_tpl = (bt_cfg.get("success_msg") or "").strip()

        if suc_tpl:

            user_obj = await get_robust_user(client, user_id)

            txt = _fmt_delivery_text(suc_tpl, user_obj, story).replace("{channel_link}", invite_link.invite_link)

            msg = await client.send_message(user_id, txt, disable_web_page_preview=True)

            _schedule_auto_delete(msg, 86400)

        else:

            # Result message

            if lang == 'hi':

                txt = (

                    f"<b>आपका 1-टाइम एक्सेस लिंक तैयार है!</b>\n\n"

                    f"<b>{s_name_h}</b>\n\n"

                    f"{invite_link.invite_link}\n\n"

                    "<blockquote>"

                    f"<i>यह लिंक केवल 1 व्यक्ति के लिए है। एक बार उपयोग करने पर, यह एक्सपायर हो जाएगा।\n"

                    f"भविष्य में इस स्टोरी को /mystories का उपयोग करके सीधे DM में प्राप्त किया जा सकता है।</i>\n"

                    "</blockquote>"

                    "<blockquote>"

                    f"<i>⚠️ यह मैसेज 24 घंटे में अपने आप डिलीट हो जाएगा। "

                    f"जैसे ही आप ज्वाइन करेंगे, प्राइवेसी के लिए लिंक को तुरंत रद्द कर दिया जाएगा।</i>\n"

                    "</blockquote>"

                )

                back_btn_txt = "« ❮ मुख्य मेनू"

            else:

                txt = (

                    f"<b>Your 1-Time Access Link is Ready!</b>\n\n"

                    f"<b>{s_name_h}</b>\n\n"

                    f"{invite_link.invite_link}\n\n"

                    "<blockquote>"

                    f"<i>This link works for exactly 1 person only. Once used, it expires.\n"

                    f"Future access to this story will be via DM delivery only by using /mystories.</i>\n"

                    "</blockquote>"

                    "<blockquote>"

                    f"<i>⚠️ This message will auto-delete in 24 hours. "

                    f"Once you join, the link will be revoked automatically to ensure privacy.</i>\n"

                    "</blockquote>"

                )

                back_btn_txt = f"« ❮ {_sc('MAIN MENU')}"



            kb_link = [[InlineKeyboardButton(back_btn_txt, callback_data="mb#main_back")]]

            msg = await client.send_message(user_id, txt, disable_web_page_preview=True, reply_markup=InlineKeyboardMarkup(kb_link))

            _schedule_auto_delete(msg, 86400)



    except Exception as e:

        logger.error(f"Channel Link Error: {e}")

        await client.send_message(

            user_id,

            f"❌ Failed to generate channel link. Falling back to DM Delivery...\n<i>Error: {e}</i>"

        )

        await _do_dm_delivery(client, user_id, story)



def _schedule_auto_delete(msg, seconds: int):

    async def task():

        await asyncio.sleep(seconds)

        try:

            await msg.delete()

        except:

            pass

    asyncio.create_task(task())



# ─────────────────────────────────────────────────────────────────

# Join Revoker Helper

# ─────────────────────────────────────────────────────────────────

async def _process_chat_member(client, update):
    # 1. If a user joins the channel with an invite link -> Revoke it instantly to prevent reuse
    if getattr(update, "new_chat_member", None) and getattr(update, "invite_link", None):
        try:
            await client.revoke_chat_invite_link(update.chat.id, update.invite_link.invite_link)
        except Exception:
            pass

    # 2. If the bot itself was added as an administrator to a channel
    try:
        new_member = getattr(update, "new_chat_member", None)
        if not new_member:
            return

        user = getattr(new_member, "user", None)
        me = getattr(client, "me", None)
        if not me:
            try: me = await client.get_me()
            except Exception: me = None

        if user and me and user.id == me.id:
            chat = update.chat
            if not chat or getattr(chat, "type", None) not in (enums.ChatType.CHANNEL, enums.ChatType.SUPERGROUP):
                return

            status = getattr(new_member, "status", None)
            if status in (enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER):
                chat_id = chat.id
                chat_title = chat.title or str(chat_id)
                chat_uname = chat.username

                from datetime import datetime, timezone
                now_utc = datetime.now(timezone.utc)

                # A. Auto-register channel in premium_channels
                await db.db.premium_channels.update_one(
                    {"channel_id": chat_id},
                    {"$set": {
                        "channel_id": chat_id,
                        "name": chat_title,
                        "type": "delivery",
                        "username": chat_uname,
                        "added_by_bot": me.username or me.id,
                        "updated_at": now_utc
                    }},
                    upsert=True
                )
                logger.info(f"[AutoChannel] Bot @{me.username} added as Admin to '{chat_title}' ({chat_id}). Auto-registered in premium_channels!")

                # B. Auto-search for matching story in premium_stories
                clean_title = re.sub(r'^(?:📽️|🎬|🎥|📺|🍿)?\s*(?:show|title|name)\s*:\s*', '', chat_title, flags=re.I).strip()
                strip_emojis = ["🎬", "📽️", "🎥", "📺", "🍿", "📹", "🔹", "🔸", "▫️", "▪️", "▶️", "👉", "✨", "🔥", "⚡", "🌟", "👑", "💎"]
                for emo in strip_emojis:
                    if clean_title.startswith(emo): clean_title = clean_title[len(emo):].strip()
                    if clean_title.endswith(emo): clean_title = clean_title[:-len(emo)].strip()
                clean_title = re.sub(r'\s+', ' ', clean_title).strip()
                norm_key = re.sub(r'[^a-zA-Z0-9]', '', clean_title.lower())

                query = {
                    "$or": [
                        {"clean_title": norm_key},
                        {"title": {"$regex": f"^{re.escape(clean_title)}$", "$options": "i"}},
                        {"story_name_en": {"$regex": f"^{re.escape(clean_title)}$", "$options": "i"}},
                        {"story_name_hi": {"$regex": f"^{re.escape(clean_title)}$", "$options": "i"}}
                    ]
                }
                matching_story = await db.db.premium_stories.find_one(query)
                if matching_story:
                    await db.db.premium_stories.update_one(
                        {"_id": matching_story["_id"]},
                        {
                            "$set": {
                                "channel_id": chat_id,
                                "delivery_channel_id": chat_id
                            },
                            "$addToSet": {
                                "channel_pool": chat_id
                            }
                        }
                    )
                    st_name = matching_story.get("title") or matching_story.get("story_name_en") or "Story"
                    logger.info(f"[AutoChannel] 🎉 Successfully auto-linked Channel '{chat_title}' (ID: {chat_id}) with Story '{st_name}'!")
    except Exception as e:
        logger.warning(f"[AutoChannel] Error in _process_chat_member admin detector: {e}")





def _resolve_story_thumb_url(s: dict) -> str | None:
    """
    Resolves the best public HTTP URL for story thumbnail in inline search.
    Prioritizes HTTP CDN/Catbox URLs over Telegram file_ids.
    """
    for k in ("poster_url", "banner_url", "image_url", "cover_url", "cover", "thumbnail", "image", "banner"):
        val = s.get(k)
        if val and isinstance(val, str):
            val = val.strip()
            if val.startswith("http://") or val.startswith("https://"):
                return val
            if (val.startswith("/") or val.startswith("uploads/") or val.startswith("static/") or (val.endswith((".jpg", ".jpeg", ".png", ".webp")) and not val.startswith("AgAC") and len(val) < 80 and " " not in val)):
                return "https://aryapremium.store/" + val.lstrip("/")
    return None


async def _process_inline_query(client, inline_query):
    """
    Handles native Telegram Inline Query Search for stories.
    When user taps any result, it dispatches the story selection to the bot (/start story_id),
    and the bot sends the official Story Profile Card with banner photo, custom emojis, and buttons.
    """
    try:
        user_id = inline_query.from_user.id if inline_query.from_user else 0
        q = (inline_query.query or "").strip()
        bot_id = getattr(getattr(client, "me", None), "id", None)
        
        user = await db.get_user(user_id, from_user=inline_query.from_user, bot_id=bot_id) if user_id else {}
        lang = user.get('lang', 'en') if user else 'en'
        
        q_bot = {"$or": [{"bot_id": bot_id}, {"bot_id": {"$exists": False}}, {"bot_id": None}]} if bot_id else {}
        
        import re
        if q:
            reg = re.escape(q)
            q_cond = {
                "$or": [
                    {"story_name_en": {"$regex": reg, "$options": "i"}},
                    {"story_name_hi": {"$regex": reg, "$options": "i"}},
                    {"platform": {"$regex": reg, "$options": "i"}},
                    {"genre": {"$regex": reg, "$options": "i"}}
                ]
            }
            if q_bot:
                q_find = {"$and": [q_bot, q_cond]}
            else:
                q_find = q_cond
        else:
            q_find = q_bot if q_bot else {}

        stories = await db.db.premium_stories.find(q_find).sort("_id", -1).limit(50).to_list(length=50)
        if not stories:
            if q:
                reg = re.escape(q)
                stories = await db.db.premium_stories.find({
                    "$or": [
                        {"story_name_en": {"$regex": reg, "$options": "i"}},
                        {"story_name_hi": {"$regex": reg, "$options": "i"}},
                        {"platform": {"$regex": reg, "$options": "i"}},
                        {"genre": {"$regex": reg, "$options": "i"}}
                    ]
                }).sort("_id", -1).limit(50).to_list(length=50)
            else:
                stories = await db.db.premium_stories.find({}).sort("_id", -1).limit(50).to_list(length=50)

        from pyrogram.types import InlineQueryResultArticle, InputTextMessageContent
        results = []

        for idx, s in enumerate(stories):
            s_id = str(s['_id'])
            s_name = s.get(f'story_name_{lang}', s.get('story_name_en', 'Unknown Story'))
            platform = s.get('platform', 'Unknown')
            episodes = s.get('episodes', 'Unknown')
            price = int(s.get('price', 0))
            thumb = _resolve_story_thumb_url(s)

            desc_text = f"{platform} • {episodes} eps • ₹{price}"

            article_kwargs = {
                "id": f"{s_id}_{idx}",
                "title": s_name,
                "description": desc_text,
                "input_message_content": InputTextMessageContent(
                    f"/start story_{s_id}"
                )
            }
            if thumb:
                article_kwargs["thumb_url"] = thumb
                article_kwargs["thumb_width"] = 120
                article_kwargs["thumb_height"] = 120

            results.append(InlineQueryResultArticle(**article_kwargs))

        await inline_query.answer(
            results=results,
            cache_time=1,
            is_personal=True,
            switch_pm_text="🛒 Browse All Marketplace Stories" if lang == 'en' else "🛒 सभी कहानियाँ देखें",
            switch_pm_parameter="marketplace"
        )
    except Exception as e:
        logger.error(f"Inline query error: {e}", exc_info=True)
        try:
            await inline_query.answer(results=[], cache_time=1)
        except Exception:
            pass
