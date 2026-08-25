import asyncio
from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler, CallbackQueryHandler

# ── Shared waiting futures store ──
_waiting_futures: dict = {}

# ── Smallcap font converter (shared helper) ──
def to_smallcap(text: str) -> str:
    return text.translate(str.maketrans(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"
    ))

def transliterate_to_hindi(text: str) -> str:
    """Converts Romanized script to Devanagari Hindi using Google Input Tools (sound-based)."""
    if not text: return ""
    # If already contains Devanagari, return as is
    if any('\u0900' <= c <= '\u097f' for c in text):
        return text
    try:
        import urllib.parse, requests
        # Filter out characters that might mess up the URL
        cleaned_text = text.replace("#", "").replace("&", "")
        url = f"https://inputtools.google.com/request?text={urllib.parse.quote(cleaned_text)}&itc=hi-t-i0-und&num=1&cp=0&cs=1&ie=utf-8&oe=utf-8&app=test"
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            data = r.json()
            if data[0] == "SUCCESS":
                # Extract first suggestion
                return data[1][0][1][0]
    except Exception:
        pass
    return text

def translate_to_hindi(text: str) -> str:
    """Default Hindi converter: Performs script transliteration to preserve recognition."""
    return transliterate_to_hindi(text)

def smart_translate_meaning(text: str) -> str:
    """Translates the meaning of English text into Hindi."""
    if not text: return ""
    if any('\u0900' <= c <= '\u097f' for c in text):
        return text
    try:
        from deep_translator import GoogleTranslator
        translated = GoogleTranslator(source='auto', target='hi').translate(text)
        return translated if translated else text
    except Exception:
        return text

def translate_to_english(text: str) -> str:
    """Translates text to English ONLY if it contains non-Roman characters."""
    if not text: return ""
    # If already all Roman characters (English/Hinglish), return as is
    # This prevents "Fauji" -> "Army" or "Aane" -> "Coming"
    if all(ord(c) < 128 for c in text):
        return text
    
    try:
        from deep_translator import GoogleTranslator
        translated = GoogleTranslator(source='auto', target='en').translate(text)
        return translated if translated else text
    except Exception:
        return text


# ── Groq AI Integration ─────────────────────────────────────────────────────

async def _get_groq_key() -> str | None:
    """Retrieve the Groq API key from DB config."""
    try:
        try:
            from AryaPremium.database import db
        except ImportError:
            from database import db
        val = await db.get_config("groq_api_key")
        return val.strip() if val and isinstance(val, str) else None
    except Exception:
        return None


async def groq_transliterate_hindi(text: str) -> str:
    """
    Uses Groq AI to transliterate a story title from English/Hinglish to
    accurate Hindi (Devanagari) script. Falls back to Google Input Tools.
    """
    if not text: return ""
    # If already Devanagari, return as-is
    if any('\u0900' <= c <= '\u097f' for c in text):
        return text

    api_key = await _get_groq_key()
    if not api_key:
        return transliterate_to_hindi(text)  # fallback

    try:
        import httpx
        prompt = (
            "You are a Hindi transliteration expert. Your task is to convert the given story title "
            "from English/Hinglish into accurate Hindi Devanagari script. "
            "Preserve the original meaning and phonetics. "
            "Return ONLY the transliterated Hindi text, nothing else — no explanation, no quotes.\n\n"
            f"Title: {text}"
        )
        payload = {
            "model": "llama3-70b-8192",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 100,
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post("https://api.groq.com/openai/v1/chat/completions", json=payload, headers=headers)
            if resp.status_code == 200:
                result = resp.json()["choices"][0]["message"]["content"].strip()
                return result if result else transliterate_to_hindi(text)
    except Exception:
        pass
    return transliterate_to_hindi(text)  # fallback


async def groq_translate_description(text: str, target_lang: str = "hi") -> str:
    """
    Uses Groq AI to translate a story description.
    target_lang: 'hi' (English→Hindi) or 'en' (Hindi→English).
    Falls back to deep_translator on failure.
    """
    if not text or text.strip().lower() == "none": return text

    api_key = await _get_groq_key()
    if not api_key:
        # fallback
        if target_lang == "hi":
            return smart_translate_meaning(text)
        return translate_to_english(text)

    try:
        import httpx
        lang_name = "Hindi" if target_lang == "hi" else "English"
        prompt = (
            f"You are a professional story translator. Translate the following story description into natural, "
            f"engaging {lang_name}. Keep names of characters and places as they are. "
            f"Return ONLY the translated text, nothing else.\n\n"
            f"Description: {text}"
        )
        payload = {
            "model": "llama3-70b-8192",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 500,
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post("https://api.groq.com/openai/v1/chat/completions", json=payload, headers=headers)
            if resp.status_code == 200:
                result = resp.json()["choices"][0]["message"]["content"].strip()
                return result if result else (smart_translate_meaning(text) if target_lang == "hi" else translate_to_english(text))
    except Exception:
        pass
    # fallback
    if target_lang == "hi":
        return smart_translate_meaning(text)
    return translate_to_english(text)


def _ask_key(bot: Client, user_id: int):
    return (getattr(getattr(bot, "me", None), "id", 0), int(user_id))

async def _input_router(bot, message):
    uid = message.from_user.id if message.from_user else None
    key = _ask_key(bot, uid) if uid else None
    if key and key in _waiting_futures:
        fut = _waiting_futures.pop(key)
        if not fut.done():
            fut.set_result(message)
            from pyrogram import StopPropagation
            raise StopPropagation
    message.continue_propagation()

async def _cb_input_router(bot, query):
    uid = query.from_user.id
    if query.data in ["ask_cancel", "ask_skip"]:
        key = _ask_key(bot, uid)
        if key and key in _waiting_futures:
            fut = _waiting_futures.pop(key)
            if not fut.done():
                fut.set_result(query)
            try: await query.answer()
            except: pass
            return
    query.continue_propagation()

def setup_ask_router(bot: Client):
    bot.add_handler(MessageHandler(_input_router, filters.private), group=-100)
    bot.add_handler(CallbackQueryHandler(_cb_input_router), group=-100)

async def native_ask(bot, user_id: int, text: str, reply_markup=None, timeout: int = 300, parse_mode=None):
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    key = _ask_key(bot, user_id)

    old = _waiting_futures.pop(key, None)
    if old and not old.done():
        old.cancel()

    _waiting_futures[key] = fut
    send_kwargs = {"reply_markup": reply_markup}
    if parse_mode is not None:
        send_kwargs["parse_mode"] = parse_mode
    await bot.send_message(user_id, text, **send_kwargs)
    
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        _waiting_futures.pop(key, None)
        raise

async def _deliver_purchased_story(bot_id: str, user_id: int, story: dict):
    """Delegates to the market_seller delivery engine after payment approval."""
    from plugins.userbot.market_seller import market_clients, dispatch_delivery_choice
    import logging
    logger = logging.getLogger(__name__)

    seller_cli = market_clients.get(str(bot_id))
    if not seller_cli:
        logger.error(f"Cannot deliver: store bot {bot_id} not running.")
        return

    # Show the delivery choice screen to the user (DM vs Channel as inline buttons)
    await dispatch_delivery_choice(seller_cli, user_id, story)

async def _safe_send_log(client, channel_id, text: str, photo_path: str = None, bot_id: int = None):
    if not channel_id:
        return
    try:
        chat_id_val = int(str(channel_id).strip()) if str(channel_id).strip().lstrip("-").isdigit() else str(channel_id).strip()
    except Exception:
        chat_id_val = channel_id

    # 1. Gather clients in priority order: bot's own client -> passed client -> mgmt_client
    clients_to_try = []
    try:
        from plugins.userbot.market_seller import market_clients
        if bot_id and str(bot_id) in market_clients:
            b_cli = market_clients[str(bot_id)]
            if getattr(b_cli, "is_connected", False):
                clients_to_try.append(b_cli)
    except Exception:
        pass

    if client and getattr(client, "is_connected", False) and client not in clients_to_try:
        clients_to_try.append(client)

    try:
        from database import db
        if getattr(db, "mgmt_client", None) and getattr(db.mgmt_client, "is_connected", False) and db.mgmt_client not in clients_to_try:
            clients_to_try.append(db.mgmt_client)
    except Exception:
        pass

    for cli in clients_to_try:
        try:
            if photo_path:
                await cli.send_photo(chat_id_val, photo=photo_path, caption=text)
            else:
                await cli.send_message(chat_id_val, text=text, disable_web_page_preview=True)
            return
        except Exception:
            continue

    # 2. HTTP Telegram Bot API Fallback
    try:
        from AryaPremium.config import Config
    except ImportError:
        from config import Config
    import os, aiohttp
    token = getattr(Config, "MGMT_BOT_TOKEN", None) or getattr(Config, "BOT_TOKEN", None) or os.environ.get("MGMT_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
    if token:
        try:
            async with aiohttp.ClientSession() as session:
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                payload = {
                    "chat_id": chat_id_val,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True
                }
                async with session.post(url, json=payload, timeout=10) as resp:
                    pass
        except Exception:
            pass

async def log_payment(user_id: int, user_first_name: str, s_name: str, amount, method: str,
                      receipt_id: str = "", photo_path: str = None, username: str = "", pay_link: str = "", order_id: str = "", user_last_name: str = "", bot_id: int = None, bot_username: str = ""):
    try:
        from AryaPremium.config import Config
        from AryaPremium.database import db
    except ImportError:
        from config import Config
        from database import db

    channel_id = None
    target_bot_id = bot_id
    if not target_bot_id and order_id and hasattr(db, "db") and db.db is not None:
        try:
            order_doc = await db.db.orders.find_one({"order_id": order_id})
            if order_doc:
                target_bot_id = order_doc.get("bot_id")
        except Exception:
            pass

    if not target_bot_id and bot_username and hasattr(db, "db") and db.db is not None:
        try:
            b_clean = bot_username.lstrip("@").strip()
            bot_doc = await db.db.premium_bots.find_one({"username": {"$regex": f"^{b_clean}$", "$options": "i"}})
            if bot_doc:
                target_bot_id = bot_doc.get("id")
        except Exception:
            pass

    if target_bot_id and hasattr(db, "db") and db.db is not None:
        try:
            bot_doc = await db.db.premium_bots.find_one({"id": int(target_bot_id)})
            if bot_doc:
                custom_ch = (bot_doc.get("config") or {}).get("log_channel")
                if custom_ch:
                    channel_id = custom_ch
        except Exception:
            pass

    if not channel_id:
        channel_id = getattr(Config, "PAYMENT_LOGS_CHANNEL", None) or os.environ.get("PAYMENT_LOGS_CHANNEL") or getattr(Config, "ARYA_LOGS_CHANNEL", None) or os.environ.get("ARYA_LOGS_CHANNEL")
    if not channel_id:
        import logging; logging.getLogger(__name__).warning("[AryaLog] log_payment: PAYMENT_LOGS_CHANNEL not configured — skipping.")
        return

    try:
        if order_id and hasattr(db, "db") and db.db is not None:
            try:
                res = await db.db.orders.find_one_and_update(
                    {"order_id": order_id},
                    {"$set": {"payment_log_sent": True}},
                    upsert=True
                )
                if res and res.get("payment_log_sent") is True:
                    return
            except Exception:
                pass

        from datetime import datetime, timezone, timedelta
        ist = timezone(timedelta(hours=5, minutes=30))
        time_str = datetime.now(ist).strftime('%d %b %Y, %I:%M %p IST')
        
        method_badge = {
            "razorpay":  "💳 Razorpay (Automatic)",
            "easebuzz":  "💸 Easebuzz (Automatic)",
            "upi":       "🏦 Manual UPI",
            "manual_upi":"🏦 Manual UPI",
        }.get(method.lower(), method.capitalize())

        def clean_username(uname: str) -> str:
            if not uname or str(uname).strip().lower() in ("", "unknown", "none", "@unknown", "@none"):
                return ""
            return uname[1:].strip() if uname.startswith("@") else uname.strip()

        cleaned_username = clean_username(username)

        def clean_name(first: str, last: str) -> str:
            name = f"{first or ''} {last or ''}".strip()
            return "User" if not name or name.lower() in ("unknown", "none", "null", "undefined") else name

        def escape_html(text: str) -> str:
            return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") if text else ""

        full_name_esc = escape_html(clean_name(user_first_name, user_last_name))
        tg_link = f"tg://user?id={user_id}"
        user_display = f'<a href="{tg_link}">{full_name_esc}</a> (@{escape_html(cleaned_username)})' if cleaned_username else f'<a href="{tg_link}">{full_name_esc}</a>'

        link_line = f"\n<b>Payment Link:</b> <a href=\"{pay_link}\">View Receipt</a>" if pay_link and "razorpay" in method.lower() else ""

        caption = (
            f"<b>✅ PAYMENT CONFIRMED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>❖ Order ID:</b> <code>{order_id or 'N/A'}</code>\n"
            f"<b>❖ User:</b> {user_display}\n"
            f"<b>❖ Telegram ID:</b> <code>{user_id}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>❖ Story:</b> {escape_html(s_name)}\n"
            f"<b>❖ Amount Paid:</b> ₹{amount}\n"
            f"<b>❖ Method:</b> {method_badge}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>❖ Receipt / Gateway ID:</b>\n<code>{receipt_id or 'N/A'}</code>"
            f"{link_line}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>❖ Time:</b> {time_str}"
        )
        await _safe_send_log(getattr(db, "mgmt_client", None), channel_id, caption, photo_path=photo_path, bot_id=target_bot_id)
    except Exception as e:
        import logging; logging.getLogger(__name__).error(f"Payment log error: {e}")

async def log_delivery(bot_username: str, user_id: int, user_first_name: str, s_name: str, d_type: str, status: str, username: str = "", order_id: str = "", user_last_name: str = "", bot_id: int = None):
    try:
        from AryaPremium.config import Config
        from AryaPremium.database import db
    except ImportError:
        from config import Config
        from database import db

    channel_id = None
    target_bot_id = bot_id
    if not target_bot_id and bot_username and hasattr(db, "db") and db.db is not None:
        try:
            b_clean = bot_username.lstrip("@").strip()
            bot_doc = await db.db.premium_bots.find_one({"username": {"$regex": f"^{b_clean}$", "$options": "i"}})
            if bot_doc:
                target_bot_id = bot_doc.get("id")
        except Exception:
            pass

    if target_bot_id and hasattr(db, "db") and db.db is not None:
        try:
            bot_doc = await db.db.premium_bots.find_one({"id": int(target_bot_id)})
            if bot_doc:
                custom_ch = (bot_doc.get("config") or {}).get("log_channel")
                if custom_ch:
                    channel_id = custom_ch
        except Exception:
            pass

    if not channel_id:
        channel_id = getattr(Config, "DELIVERY_LOGS_CHANNEL", None) or os.environ.get("DELIVERY_LOGS_CHANNEL") or getattr(Config, "ARYA_LOGS_CHANNEL", None) or os.environ.get("ARYA_LOGS_CHANNEL")
    if not channel_id:
        import logging; logging.getLogger(__name__).warning("[AryaLog] log_delivery: DELIVERY_LOGS_CHANNEL not configured — skipping.")
        return

    try:
        from datetime import datetime, timezone, timedelta
        ist = timezone(timedelta(hours=5, minutes=30))
        time_str = datetime.now(ist).strftime('%d %b %Y, %I:%M %p IST')
        
        def clean_username(uname: str) -> str:
            if not uname or str(uname).strip().lower() in ("", "unknown", "none", "@unknown", "@none"):
                return ""
            return uname[1:].strip() if uname.startswith("@") else uname.strip()

        cleaned_username = clean_username(username)

        def clean_name(first: str, last: str) -> str:
            name = f"{first or ''} {last or ''}".strip()
            return "User" if not name or name.lower() in ("unknown", "none", "null", "undefined") else name

        def escape_html(text: str) -> str:
            return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") if text else ""

        full_name_esc = escape_html(clean_name(user_first_name, user_last_name))
        tg_link = f"tg://user?id={user_id}"
        user_display = f'<a href="{tg_link}">{full_name_esc}</a> (@{escape_html(cleaned_username)})' if cleaned_username else f'<a href="{tg_link}">{full_name_esc}</a>'

        text = (
            f"<b>📦 DELIVERY EVENT</b>\n"
            f"────────────────────\n"
            f"<b>Order ID:</b> <code>{order_id or 'N/A'}</code>\n"
            f"<b>Store Bot:</b> @{bot_username or 'Unknown'}\n"
            f"<b>User:</b> {user_display}\n"
            f"<b>Telegram ID:</b> <code>{user_id}</code>\n"
            f"<b>Story:</b> {escape_html(s_name)}\n"
            f"<b>Method:</b> {d_type.upper()}\n"
            f"<b>Status:</b> {status}\n"
            f"<b>Date:</b> {time_str}"
        )
        await _safe_send_log(getattr(db, "mgmt_client", None), channel_id, text, bot_id=target_bot_id)
    except Exception as e:
        import logging; logging.getLogger(__name__).error(f"Delivery log error: {e}")

async def log_arya_event(event_type: str, user_id: int, user_info: dict, details: str, bot_id: int = None, bot_username: str = ""):
    try:
        from AryaPremium.config import Config
        from AryaPremium.database import db
    except ImportError:
        from config import Config
        from database import db

    channel_id = None
    target_bot_id = bot_id or (user_info or {}).get("bot_id")
    if not target_bot_id and bot_username and hasattr(db, "db") and db.db is not None:
        try:
            b_clean = bot_username.lstrip("@").strip()
            bot_doc = await db.db.premium_bots.find_one({"username": {"$regex": f"^{b_clean}$", "$options": "i"}})
            if bot_doc:
                target_bot_id = bot_doc.get("id")
        except Exception:
            pass

    if target_bot_id and hasattr(db, "db") and db.db is not None:
        try:
            bot_doc = await db.db.premium_bots.find_one({"id": int(target_bot_id)})
            if bot_doc:
                custom_ch = (bot_doc.get("config") or {}).get("log_channel")
                if custom_ch:
                    channel_id = custom_ch
        except Exception:
            pass

    if not channel_id:
        channel_id = getattr(Config, "ARYA_LOGS_CHANNEL", None) or os.environ.get("ARYA_LOGS_CHANNEL") or getattr(Config, "DELIVERY_LOGS_CHANNEL", None) or os.environ.get("DELIVERY_LOGS_CHANNEL")

    if not channel_id:
        import logging; logging.getLogger(__name__).warning("[AryaLog] log_arya_event: ARYA_LOGS_CHANNEL not configured — skipping.")
        return

    try:
        from datetime import datetime, timezone, timedelta
        ist = timezone(timedelta(hours=5, minutes=30))
        time_str = datetime.now(ist).strftime('%d %b %Y, %I:%M %p IST')

        username = (user_info or {}).get("username", "")
        user_first_name = (user_info or {}).get("first_name", "")
        user_last_name = (user_info or {}).get("last_name", "")

        def clean_username(uname: str) -> str:
            if not uname or str(uname).strip().lower() in ("", "unknown", "none", "@unknown", "@none"):
                return ""
            return uname[1:].strip() if uname.startswith("@") else uname.strip()

        cleaned_username = clean_username(username)

        def clean_name(first: str, last: str) -> str:
            name = f"{first or ''} {last or ''}".strip()
            return "User" if not name or name.lower() in ("unknown", "none", "null", "undefined") else name

        def escape_html(text: str) -> str:
            return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") if text else ""

        full_name_esc = escape_html(clean_name(user_first_name, user_last_name))
        tg_link = f"tg://user?id={user_id}"
        user_display = f'<a href="{tg_link}">{full_name_esc}</a> (@{escape_html(cleaned_username)})' if cleaned_username else f'<a href="{tg_link}">{full_name_esc}</a>'

        joined = (user_info or {}).get("joined_date", time_str)
        if isinstance(joined, datetime):
            joined = joined.astimezone(ist).strftime('%d %b %Y, %I:%M %p IST')
        elif not isinstance(joined, str):
            joined = "N/A"

        text = (
            f"<b>🛡️ ARYA CORE LOG | {event_type}</b>\n"
            f"────────────────────\n"
            f"<b>User:</b> {user_display}\n"
            f"<b>Telegram ID:</b> <code>{user_id}</code>\n"
            f"<b>Joined:</b> {joined}\n"
            f"────────────────────\n"
            f"<b>Details:</b>\n{details}\n"
            f"────────────────────\n"
            f"<b>Time:</b> {time_str}"
        )
        await _safe_send_log(getattr(db, "mgmt_client", None), channel_id, text, bot_id=target_bot_id)
    except Exception as e:
        import logging; logging.getLogger(__name__).error(f"Arya core log error: {e}")





async def upload_to_catbox(file_path):
    import aiohttp, os
    async with aiohttp.ClientSession() as session:
        data = aiohttp.FormData()
        data.add_field('reqtype', 'fileupload')
        data.add_field('userhash', '')
        data.add_field('fileToUpload', open(file_path, 'rb'), filename=os.path.basename(file_path))
        async with session.post('https://catbox.moe/user/api.php', data=data) as resp:
            if resp.status == 200:
                return await resp.text()
    return None
