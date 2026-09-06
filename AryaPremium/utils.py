import asyncio
from pyrogram import Client, filters
from pyrogram.errors import FloodWait, RPCError
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


async def groq_transliterate_telugu(text: str) -> str:
    """
    Uses Groq AI to transliterate a story title from English/Hindi to
    accurate Telugu script. Falls back to deep_translator.
    """
    if not text: return ""
    if any('\u0c00' <= c <= '\u0c7f' for c in text):
        return text

    api_key = await _get_groq_key()
    if not api_key:
        try:
            from deep_translator import GoogleTranslator
            return GoogleTranslator(source='auto', target='te').translate(text)
        except Exception:
            return text

    try:
        import httpx
        prompt = (
            "You are a Telugu transliteration and translation expert. Your task is to convert the given story title "
            "from English/Hindi into natural, accurate Telugu script. "
            "Preserve the original meaning and phonetics. "
            "Return ONLY the transliterated/translated Telugu text, nothing else — no explanation, no quotes.\n\n"
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
                if result:
                    return result
    except Exception:
        pass

    try:
        from deep_translator import GoogleTranslator
        return GoogleTranslator(source='auto', target='te').translate(text)
    except Exception:
        return text


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
        lang_name = "Telugu" if target_lang == "te" else ("Hindi" if target_lang == "hi" else "English")
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
    if target_lang == "te":
        try:
            from deep_translator import GoogleTranslator
            return GoogleTranslator(source='auto', target='te').translate(text)
        except Exception:
            return text
    elif target_lang == "hi":
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
    uid = query.from_user.id if query.from_user else None
    if uid:
        key = _ask_key(bot, uid)
        if key and key in _waiting_futures:
            fut = _waiting_futures.pop(key, None)
            if fut and not fut.done():
                if query.data in ["ask_cancel", "ask_skip"]:
                    fut.set_result(query)
                    try: await query.answer()
                    except: pass
                    return
                else:
                    fut.cancel()
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
    except (asyncio.TimeoutError, asyncio.CancelledError):
        _waiting_futures.pop(key, None)
        raise

async def ask_user(bot, user_id: int, text: str = None, reply_markup=None, timeout: int = 60, parse_mode=None):
    """Waits for user input. If text is provided, sends it first via native_ask."""
    if text is not None:
        return await native_ask(bot, user_id, text=text, reply_markup=reply_markup, timeout=timeout, parse_mode=parse_mode)
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    key = _ask_key(bot, user_id)

    old = _waiting_futures.pop(key, None)
    if old and not old.done():
        old.cancel()

    _waiting_futures[key] = fut
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
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

    channel_id = getattr(Config, "PAYMENT_LOGS_CHANNEL", None) or os.environ.get("PAYMENT_LOGS_CHANNEL") or getattr(Config, "ARYA_LOGS_CHANNEL", None) or os.environ.get("ARYA_LOGS_CHANNEL")
    if not channel_id:
        import logging; logging.getLogger(__name__).warning("[AryaLog] log_payment: PAYMENT_LOGS_CHANNEL not configured — skipping.")
        return

    target_bot_id = bot_id
    store_bot_uname = ""
    if bot_username:
        store_bot_uname = bot_username.lstrip("@").strip()

    if not target_bot_id and order_id and hasattr(db, "db") and db.db is not None:
        try:
            order_doc = await db.db.orders.find_one({"order_id": order_id})
            if order_doc:
                target_bot_id = order_doc.get("bot_id")
                if not store_bot_uname and order_doc.get("bot_username"):
                    store_bot_uname = order_doc.get("bot_username").lstrip("@").strip()
        except Exception:
            pass

    if not store_bot_uname and target_bot_id and hasattr(db, "db") and db.db is not None:
        try:
            bot_doc = await db.db.premium_bots.find_one({"id": int(target_bot_id)})
            if bot_doc:
                store_bot_uname = bot_doc.get("username", "")
        except Exception:
            pass

    store_bot_display = f"@{store_bot_uname}" if store_bot_uname else "@StoreBot"

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

        # Auto-resolve user profile from DB if name or username is missing/generic
        if hasattr(db, "db") and db.db is not None and user_id:
            try:
                user_doc = await db.db.users.find_one({"$or": [{"id": int(user_id)}, {"telegram_id": int(user_id)}, {"id": str(user_id)}]})
                if user_doc:
                    db_first = user_doc.get("first_name") or user_doc.get("name") or ""
                    db_last = user_doc.get("last_name") or ""
                    db_uname = user_doc.get("username") or ""
                    if not user_first_name or str(user_first_name).strip().lower() in ("user", "unknown", "none", ""):
                        user_first_name = db_first
                    if not user_last_name:
                        user_last_name = db_last
                    if not username:
                        username = db_uname
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
            f"<b>❖ Store Bot:</b> {store_bot_display}\n"
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

    channel_id = getattr(Config, "DELIVERY_LOGS_CHANNEL", None) or os.environ.get("DELIVERY_LOGS_CHANNEL") or getattr(Config, "ARYA_LOGS_CHANNEL", None) or os.environ.get("ARYA_LOGS_CHANNEL")
    if not channel_id:
        import logging; logging.getLogger(__name__).warning("[AryaLog] log_delivery: DELIVERY_LOGS_CHANNEL not configured — skipping.")
        return

    target_bot_id = bot_id
    store_bot_uname = ""
    if bot_username:
        store_bot_uname = bot_username.lstrip("@").strip()

    if not target_bot_id and order_id and hasattr(db, "db") and db.db is not None:
        try:
            ord_doc = await db.db.orders.find_one({"order_id": order_id})
            if ord_doc:
                target_bot_id = ord_doc.get("bot_id")
                if not store_bot_uname and ord_doc.get("bot_username"):
                    store_bot_uname = ord_doc.get("bot_username").lstrip("@").strip()
        except Exception:
            pass

    if not store_bot_uname and target_bot_id and hasattr(db, "db") and db.db is not None:
        try:
            bot_doc = await db.db.premium_bots.find_one({"id": int(target_bot_id)})
            if bot_doc:
                store_bot_uname = bot_doc.get("username", "")
        except Exception:
            pass

    store_bot_display = f"@{store_bot_uname}" if store_bot_uname else "@StoreBot"

    try:
        # Auto-resolve user profile from DB if name or username is missing/generic
        if hasattr(db, "db") and db.db is not None and user_id:
            try:
                user_doc = await db.db.users.find_one({"$or": [{"id": int(user_id)}, {"telegram_id": int(user_id)}, {"id": str(user_id)}]})
                if user_doc:
                    db_first = user_doc.get("first_name") or user_doc.get("name") or ""
                    db_last = user_doc.get("last_name") or ""
                    db_uname = user_doc.get("username") or ""
                    if not user_first_name or str(user_first_name).strip().lower() in ("user", "unknown", "none", ""):
                        user_first_name = db_first
                    if not user_last_name:
                        user_last_name = db_last
                    if not username:
                        username = db_uname
            except Exception:
                pass
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
            f"<b>Store Bot:</b> {store_bot_display}\n"
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

    # ── Guard: A bot must NEVER be logged as a user in Core Logs ──
    if target_bot_id and user_id and int(user_id) == int(target_bot_id):
        return
    if (user_info or {}).get("is_bot"):
        return
    if hasattr(db, "db") and db.db is not None and user_id:
        try:
            bot_match = await db.db.premium_bots.find_one({"id": int(user_id)})
            if bot_match:
                return
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

    store_bot_uname = ""
    if target_bot_id and hasattr(db, "db") and db.db is not None:
        try:
            bot_doc = await db.db.premium_bots.find_one({"id": int(target_bot_id)})
            if bot_doc:
                store_bot_uname = bot_doc.get("username", "")
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
        joined_date_val = (user_info or {}).get("joined_date")

        # ── Automatically Resolve Real User Profile from MongoDB ──
        if hasattr(db, "db") and db.db is not None and user_id:
            try:
                user_doc = await db.db.users.find_one({
                    "$or": [{"id": int(user_id)}, {"telegram_id": int(user_id)}, {"id": str(user_id)}]
                })
                if user_doc:
                    db_first = user_doc.get("first_name") or user_doc.get("name") or ""
                    db_last = user_doc.get("last_name") or ""
                    db_uname = user_doc.get("username") or ""
                    if not user_first_name or str(user_first_name).strip().lower() in ("user", "unknown", "none", ""):
                        user_first_name = db_first
                    if not user_last_name:
                        user_last_name = db_last
                    if not username:
                        username = db_uname
                    if not joined_date_val:
                        joined_date_val = user_doc.get("joined_date") or user_doc.get("created_at")

                # Secondary fallback to orders collection if still missing
                if not user_first_name or str(user_first_name).strip().lower() in ("user", "unknown", "none", ""):
                    ord_doc = await db.db.orders.find_one(
                        {"$or": [{"user_id": int(user_id)}, {"user_id": str(user_id)}, {"telegram_id": int(user_id)}]},
                        sort=[("created_at", -1)]
                    )
                    if ord_doc:
                        user_first_name = ord_doc.get("first_name") or ord_doc.get("name") or ord_doc.get("customer_name") or ""
                        if not user_last_name:
                            user_last_name = ord_doc.get("last_name") or ""
                        if not username:
                            username = ord_doc.get("username") or ""
            except Exception:
                pass

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

        joined = joined_date_val or time_str
        if isinstance(joined, (int, float)):
            try:
                joined = datetime.fromtimestamp(joined, ist).strftime('%d %b %Y, %I:%M %p IST')
            except Exception:
                joined = time_str
        elif isinstance(joined, datetime):
            joined = joined.astimezone(ist).strftime('%d %b %Y, %I:%M %p IST')
        elif not isinstance(joined, str) or not joined.strip():
            joined = "N/A"

        bot_line = f"<b>Store Bot:</b> @{store_bot_uname}\n" if store_bot_uname else ""

        text = (
            f"<b>🛡️ ARYA CORE LOG | {event_type}</b>\n"
            f"────────────────────\n"
            f"{bot_line}"
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





async def upload_to_catbox(file_or_bytes, filename: str = "poster.webp") -> Optional[str]:
    """Uploads file path or bytes to Catbox.moe CDN."""
    import aiohttp, os, io
    file_obj = None
    close_file = False
    try:
        if isinstance(file_or_bytes, (bytes, bytearray)):
            file_obj = io.BytesIO(file_or_bytes)
            fname = filename
        elif hasattr(file_or_bytes, 'read'):
            file_obj = file_or_bytes
            fname = filename
        elif isinstance(file_or_bytes, (str, os.PathLike)):
            p_str = str(file_or_bytes)
            if not os.path.exists(p_str):
                return None
            file_obj = open(p_str, 'rb')
            fname = os.path.basename(p_str)
            close_file = True
        else:
            return None

        async with aiohttp.ClientSession() as session:
            data = aiohttp.FormData()
            data.add_field('reqtype', 'fileupload')
            data.add_field('userhash', '')
            data.add_field('fileToUpload', file_obj, filename=fname)
            async with session.post('https://catbox.moe/user/api.php', data=data, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                if resp.status == 200:
                    url = (await resp.text()).strip()
                    if url.startswith("http"):
                        return url
    except Exception as e:
        logger.debug(f"[Catbox] Upload error: {e}")
    finally:
        if close_file and file_obj:
            try: file_obj.close()
            except Exception: pass
    return None


async def scan_and_index_story(client, story_doc: dict, save_to_db: bool = True, db=None) -> list:
    """
    Scans the source channel for the story between start_id and end_id in batches of 200.
    Filters out empty/deleted messages and identifies all valid media/content messages.
    Updates `valid_file_ids`, `file_count`, and each part's `valid_file_ids` and `file_count`.
    """
    import logging
    logger = logging.getLogger(__name__)
    
    if db is None:
        try:
            from database import db as default_db
            db = default_db
        except Exception:
            pass
            
    src = story_doc.get('source') or story_doc.get('source_channel') or story_doc.get('channel_id')
    start_id = story_doc.get('start_id')
    end_id = story_doc.get('end_id') or story_doc.get('end_message_id')
    
    if not src or not start_id or not end_id:
        return story_doc.get("valid_file_ids") or []
        
    s = min(int(start_id), int(end_id))
    e = max(int(start_id), int(end_id))
    
    # ── Client Resolution (try mgmt_bot and market_clients fallback) ──
    clients_to_try = [client] if client else []
    try:
        from plugins.userbot.market_seller import market_clients
        for mc in market_clients.values():
            if mc not in clients_to_try:
                clients_to_try.append(mc)
    except Exception:
        pass

    active_client = client
    for cli in clients_to_try:
        try:
            await cli.get_chat(int(src))
            active_client = cli
            break
        except Exception:
            continue

    all_ids = list(range(s, e + 1))
    valid_ids = []
    discovered_poster_bytes = None
    batch_size = 200
    
    for i in range(0, len(all_ids), batch_size):
        chunk = all_ids[i:i + batch_size]
        try:
            msgs = await active_client.get_messages(int(src), chunk)
            if not isinstance(msgs, list):
                msgs = [msgs]
            for m in msgs:
                if not m or getattr(m, "empty", False) or getattr(m, "service", False):
                    continue
                # Check if message has media or content
                has_content = bool(
                    getattr(m, "document", None) or
                    getattr(m, "video", None) or
                    getattr(m, "audio", None) or
                    getattr(m, "voice", None) or
                    getattr(m, "photo", None) or
                    getattr(m, "animation", None) or
                    getattr(m, "text", None)
                )
                if has_content:
                    valid_ids.append(m.id)
                    # If story has no R2 poster yet, grab first photo or thumbnail
                    if not discovered_poster_bytes:
                        try:
                            if m.photo or getattr(m.video, 'thumbs', None) or getattr(m.document, 'thumbs', None):
                                media_buf = await active_client.download_media(m, in_memory=True)
                                if media_buf:
                                    discovered_poster_bytes = media_buf.getbuffer().tobytes() if hasattr(media_buf, 'getbuffer') else bytes(media_buf)
                        except Exception:
                            pass
        except FloodWait as fw:
            logger.info(f"FloodWait hit during scan in {src}: sleeping {fw.value}s")
            await asyncio.sleep(fw.value + 1)
            # retry chunk once
            try:
                msgs = await active_client.get_messages(int(src), chunk)
                if not isinstance(msgs, list): msgs = [msgs]
                for m in msgs:
                    if m and not getattr(m, "empty", False) and not getattr(m, "service", False):
                        if getattr(m, "document", None) or getattr(m, "video", None) or getattr(m, "audio", None) or getattr(m, "voice", None) or getattr(m, "photo", None) or getattr(m, "text", None):
                            valid_ids.append(m.id)
            except Exception: pass
        except Exception as err:
            logger.warning(f"Scan batch {chunk[0]}-{chunk[-1]} in {src} failed: {err}")
            await asyncio.sleep(0.5)
        await asyncio.sleep(0.35) # safe pause to avoid Telegram FloodWait
        
    valid_ids = sorted(list(set(valid_ids)))

    # If poster bytes were found and story has no public CDN URL, upload to R2 / Catbox
    current_poster = str(story_doc.get("poster_url") or story_doc.get("image") or "")
    if discovered_poster_bytes and ("r2.dev" not in current_poster and "r2.cloudflarestorage" not in current_poster and not current_poster.startswith("http")):
        try:
            try:
                from AryaPremium.r2_helper import upload_image_to_r2
            except ImportError:
                from r2_helper import upload_image_to_r2
            
            clean_title = story_doc.get("story_name_en") or story_doc.get("title") or "story"
            r2_url = await upload_image_to_r2(discovered_poster_bytes, width=600, height=720, format="WEBP", quality=85, clean_title=clean_title)
            if not r2_url:
                r2_url = await upload_to_catbox(discovered_poster_bytes)
            if r2_url:
                updates_poster = {
                    "poster_url": r2_url,
                    "banner_url": r2_url,
                    "image_url": r2_url,
                    "image": r2_url,
                    "cover": r2_url,
                    "poster": r2_url,
                    "banner": r2_url,
                    "r2_migrated": True
                }
                story_doc.update(updates_poster)
        except Exception as ex:
            logger.debug(f"Failed to auto-upload scanned poster to R2: {ex}")
            
    # Process parts if present
    parts = story_doc.get("parts") or []
    updated_parts = []
    found_ongoing = False
    story_end_id = int(story_doc.get("end_id") or (max(valid_ids) if valid_ids else 0) or 0)
    for idx, p in enumerate(parts):
        if isinstance(p, dict):
            b_val = str(p.get("badge") or p.get("badge_type") or "").lower()
            is_ong = bool(p.get("is_ongoing") or b_val == "ongoing")
            is_last_part = (idx == len(parts) - 1)
            
            p_start = int(p.get("start_id") or 0)
            p_end = int(p.get("end_id") or 0)
            
            if is_ong or (not found_ongoing and is_last_part and str(story_doc.get("status", "")).lower() == "ongoing"):
                is_ong = True
                found_ongoing = True
                if story_end_id > p_end:
                    p_end = story_end_id

            if p_start and p_end:
                ps = min(p_start, p_end)
                pe = max(p_start, p_end)
                p_val_ids = [mid for mid in valid_ids if ps <= mid <= pe]
            else:
                p_val_ids = []
            p_copy = dict(p)
            p_copy["end_id"] = p_end
            p_copy["is_ongoing"] = is_ong
            if is_ong and not p_copy.get("badge"):
                p_copy["badge"] = "ongoing"
            p_copy["valid_file_ids"] = p_val_ids
            p_copy["file_count"] = len(p_val_ids)
            updated_parts.append(p_copy)
            
    updates = {
        "valid_file_ids": valid_ids,
        "file_count": len(valid_ids),
    }
    p_url = story_doc.get("poster_url") or story_doc.get("banner_url") or story_doc.get("image_url") or story_doc.get("image") or story_doc.get("cover")
    if p_url:
        updates["poster_url"] = p_url
        updates["banner_url"] = p_url
        updates["image_url"] = p_url
        updates["image"] = p_url
        updates["cover"] = p_url
        updates["poster"] = p_url
        updates["banner"] = p_url
        if "r2.dev" in str(p_url) or "r2.cloudflarestorage" in str(p_url) or "catbox.moe" in str(p_url):
            updates["r2_migrated"] = True
    if updated_parts:
        updates["parts"] = updated_parts
        
    story_doc["valid_file_ids"] = valid_ids
    story_doc["file_count"] = len(valid_ids)
    if updated_parts:
        story_doc["parts"] = updated_parts
        
    if save_to_db and db and hasattr(db, "db") and story_doc.get("_id"):
        from bson.objectid import ObjectId
        raw_id = story_doc["_id"]
        q_or = [{"_id": raw_id}]
        try:
            if not isinstance(raw_id, ObjectId) and ObjectId.is_valid(str(raw_id)):
                q_or.append({"_id": ObjectId(str(raw_id))})
            elif isinstance(raw_id, ObjectId):
                q_or.append({"_id": str(raw_id)})
        except Exception:
            pass
        if story_doc.get("story_id"):
            q_or.append({"story_id": str(story_doc["story_id"])})
        await db.db.premium_stories.update_one({"$or": q_or}, {"$set": updates})
        
    return valid_ids


async def scan_and_index_all_stories(client, db=None, progress_cb=None, skip_clean: bool = True):
    """
    Iterates over all stories in MongoDB and scans/indexes valid files from Telegram DB channel.
    Instantly skips stories that are already 100% clean and indexed without making any API calls.
    """
    import logging
    logger = logging.getLogger(__name__)
    
    if db is None:
        try:
            from database import db as default_db
            db = default_db
        except Exception:
            pass
            
    if not db or not hasattr(db, "db"):
        return {"total": 0, "success": 0, "skipped": 0, "failed": 0}
        
    stories = await db.db.premium_stories.find({}).to_list(length=None)
    total = len(stories)
    success = 0
    skipped = 0
    failed = 0
    
    logger.info(f"Starting bulk sync for {total} stories (skip_clean={skip_clean})...")
    
    for idx, story in enumerate(stories, 1):
        s_name = story.get("story_name_en") or story.get("story_name") or story.get("title") or str(story.get("_id"))
        st_id = story.get("start_id")
        en_id = story.get("end_id") or story.get("end_message_id")
        val_ids = story.get("valid_file_ids")

        # Fast skip if already clean (0 API calls, 0 FloodWait)
        if (
            skip_clean
            and st_id and en_id
            and isinstance(val_ids, list)
            and len(val_ids) > 0
            and max(val_ids) >= int(en_id)
            and min(val_ids) >= int(st_id)
            and story.get("file_count") == len(val_ids)
        ):
            skipped += 1
            if progress_cb:
                await progress_cb(idx, total, f"{s_name} (Clean)", len(val_ids), True)
            continue

        try:
            valid_ids = await scan_and_index_story(client, story, save_to_db=True, db=db)
            success += 1
            logger.info(f"[{idx}/{total}] Indexed '{s_name}': {len(valid_ids)} valid files.")
            if progress_cb:
                await progress_cb(idx, total, s_name, len(valid_ids), True)
        except Exception as e:
            failed += 1
            logger.error(f"[{idx}/{total}] Failed to index '{s_name}': {e}")
            if progress_cb:
                await progress_cb(idx, total, s_name, 0, False)
        await asyncio.sleep(0.08)
        
    return {"total": total, "success": success, "skipped": skipped, "failed": failed}

