
import asyncio
import logging
import json
import tempfile
import time
from datetime import datetime, timezone
from bson.objectid import ObjectId
from pymongo.errors import PyMongoError
from pyrogram import Client, filters, enums
from pyrogram.errors import MessageNotModified
from pyrogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, ReplyKeyboardRemove,
    CallbackQuery
)
from database import db
from config import Config
import utils
from utils import native_ask
def _is_cancel(msg):
    if hasattr(msg, "data") and msg.data == "ask_cancel":
        return True
    if hasattr(msg, "text") and msg.text and ("c\u1d00\u0274\u1d04\u1d07\u029f" in msg.text.lower() or "/cancel" in msg.text.lower()):
        return True
    return False

import time

logger = logging.getLogger(__name__)


def _clean_markup_for_pyrogram(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    if not markup or not getattr(markup, "inline_keyboard", None):
        return markup
    cleaned_rows = []
    for row in markup.inline_keyboard:
        cleaned_row = []
        for btn in row:
            kwargs = {"text": getattr(btn, "text", "") or " "}
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
            b = InlineKeyboardButton(**kwargs)
            if hasattr(btn, "icon_custom_emoji_id") and btn.icon_custom_emoji_id:
                b.icon_custom_emoji_id = str(btn.icon_custom_emoji_id)
            cleaned_row.append(b)
        cleaned_rows.append(cleaned_row)
    return InlineKeyboardMarkup(cleaned_rows)


def _ikb(text: str, callback_data: str = None, url: str = None, switch_inline_query_current_chat: str = None, switch_inline_query: str = None, icon_custom_emoji_id: str = None) -> InlineKeyboardButton:
    kwargs = {"text": text or " "}
    if callback_data is not None: kwargs["callback_data"] = callback_data
    if url is not None: kwargs["url"] = url
    if switch_inline_query_current_chat is not None: kwargs["switch_inline_query_current_chat"] = switch_inline_query_current_chat
    elif switch_inline_query is not None: kwargs["switch_inline_query"] = switch_inline_query
    b = InlineKeyboardButton(**kwargs)
    if icon_custom_emoji_id:
        b.icon_custom_emoji_id = str(icon_custom_emoji_id)
    return b


def _get_top_mgmt_emoji_row() -> list:
    return [
        _ikb(" ", callback_data="mk#add_story", icon_custom_emoji_id="5920332557466997677"),
        _ikb(" ", callback_data="mk#manage_stories", icon_custom_emoji_id="6026337676091726218"),
        _ikb(" ", callback_data="mk#users", icon_custom_emoji_id="6021487472603568286"),
        _ikb(" ", callback_data="mk#settings", icon_custom_emoji_id="6021637109264160908"),
        _ikb(" ", callback_data="mk#fb_panel_0", icon_custom_emoji_id="5945256248390721326"),
    ]


async def _send_or_edit_mgmt_bot_api(client, chat_id: int, text: str, markup: InlineKeyboardMarkup, message_id: int = None) -> bool:
    import aiohttp
    import json
    import re
    import os
    bot_token = getattr(Config, "MGMT_BOT_TOKEN", "") or os.environ.get("MGMT_BOT_TOKEN", "") or getattr(client, "bot_token", "") or getattr(Config, "BOT_TOKEN", "") or os.environ.get("BOT_TOKEN", "")
    if not bot_token:
        return False

    api_text = re.sub(r'<emoji id="(\d+)">([^<]*)</emoji>', r'<tg-emoji emoji-id="\1">\2</tg-emoji>', text)
    api_kb = []
    for row in markup.inline_keyboard:
        row_list = []
        for btn in row:
            d = {"text": btn.text or " "}
            if getattr(btn, "callback_data", None) is not None: d["callback_data"] = btn.callback_data
            if getattr(btn, "url", None) is not None: d["url"] = btn.url
            if getattr(btn, "switch_inline_query_current_chat", None) is not None:
                d["switch_inline_query_current_chat"] = btn.switch_inline_query_current_chat
            elif getattr(btn, "switch_inline_query", None) is not None:
                d["switch_inline_query"] = btn.switch_inline_query
            if hasattr(btn, "icon_custom_emoji_id") and btn.icon_custom_emoji_id:
                d["icon_custom_emoji_id"] = str(btn.icon_custom_emoji_id)
            row_list.append(d)
        api_kb.append(row_list)

    payload = {
        "chat_id": int(chat_id),
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": api_kb
        }
    }
    url = f"https://api.telegram.org/bot{bot_token}/"
    method = "editMessageText" if message_id else "sendMessage"
    if message_id:
        payload["message_id"] = int(message_id)
        payload["text"] = api_text
    else:
        payload["text"] = api_text

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{url}{method}", json=payload, timeout=aiohttp.ClientTimeout(total=5.0)) as resp:
                data = await resp.json()
                if data.get("ok"):
                    return True
                logger.debug(f"MGMT Bot API {method} returned: {data}")
                return False
    except Exception as e:
        logger.debug(f"Bot API call exception: {e}")
        return False


async def _edit_or_send_mgmt_view(client, chat_id: int, text: str, markup: InlineKeyboardMarkup, message_id: int = None, query=None):
    mid = message_id or (getattr(query.message, "id", None) if query and getattr(query, "message", None) else None)
    ok = await _send_or_edit_mgmt_bot_api(client, chat_id, text, markup, message_id=mid)
    if not ok:
        import re
        clean_text = re.sub(r'<emoji id="\d+">([^<]*)</emoji>', r'\1', text)
        clean_text = re.sub(r'<tg-emoji emoji-id="\d+">([^<]*)</tg-emoji>', r'\1', clean_text)
        clean_kb = _clean_markup_for_pyrogram(markup)
        if query and getattr(query, "message", None):
            try:
                return await query.message.edit_text(clean_text, reply_markup=clean_kb, parse_mode=enums.ParseMode.HTML)
            except Exception:
                pass
        elif mid:
            try:
                return await client.edit_message_text(chat_id, mid, clean_text, reply_markup=clean_kb, parse_mode=enums.ParseMode.HTML)
            except Exception:
                pass
        try:
            return await client.send_message(chat_id, clean_text, reply_markup=clean_kb, parse_mode=enums.ParseMode.HTML)
        except Exception:
            pass




def _is_owner(user_id: int) -> bool:
    try:
        uid = int(user_id)
        if uid in (1071421266, 6867086884):  # Explicit permanent owner IDs
            return True
        import re
        owner_set = set(getattr(Config, "OWNER_IDS", []) or []) | set(getattr(Config, "SUDO_USERS", []) or [])
        for k in ("BOT_OWNER_ID", "OWNER_ID", "ADMINS", "OWNER_IDS", "ADMIN"):
            val = getattr(Config, k, "")
            if val:
                for match in re.findall(r'\d+', str(val)):
                    owner_set.add(int(match))
        return uid in owner_set
    except Exception:
        return False

async def _is_owner_db(user_id: int) -> bool:
    if _is_owner(user_id):
        return True
    try:
        cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
        db_owners = cfg.get("owner_ids", [])
        if int(user_id) in [int(x) for x in db_owners if str(x).isdigit()]:
            return True
    except Exception:
        pass
async def _find_premium_bot(b_id):
    if not b_id:
        return None
    try:
        b_int = int(b_id)
        bot = await db.db.premium_bots.find_one({"$or": [{"id": b_int}, {"id": str(b_id)}, {"_id": b_int}, {"_id": str(b_id)}]})
        if bot:
            return bot
    except Exception:
        pass
    return await db.db.premium_bots.find_one({"$or": [{"id": str(b_id)}, {"username": str(b_id).lstrip('@')}]})


async def _deny_if_not_owner(client, user_id: int):
    try:
        uid = int(user_id)
    except Exception:
        return True
    if uid in (1071421266, 6867086884):
        return False
    if _is_owner(uid) or (await _is_owner_db(uid)):
        return False
    logger.warning(f"[AUTH] Non-owner access attempt: {uid}")
    await client.send_message(user_id, f"❌ Access denied. This panel is for owners only.\n\n(Your Telegram ID is: `{user_id}`)\nAdd this ID to your BOT_OWNER_ID in .env")
    return True


async def _safe_answer(query, *args, **kwargs):
    try:
        return await query.answer(*args, **kwargs)
    except Exception:
        return None


async def resolve_seller_client(target_user_id, bot_id_or_str=None):
    from plugins.userbot.market_seller import market_clients
    seller_cli = None
    try:
        if bot_id_or_str and str(bot_id_or_str) != "mini_app" and str(bot_id_or_str) in market_clients:
            seller_cli = market_clients[str(bot_id_or_str)]
        
        if not seller_cli and target_user_id:
            user_doc = await db.db.users.find_one({"id": int(target_user_id)})
            if user_doc and user_doc.get("bot_ids"):
                for bid in user_doc["bot_ids"]:
                    if str(bid) in market_clients:
                        seller_cli = market_clients[str(bid)]
                        break
        
        if not seller_cli and market_clients:
            seller_cli = list(market_clients.values())[0]
    except Exception as e:
        logger.error(f"Error in resolve_seller_client: {e}")
    return seller_cli


async def _render_home(client, chat_id: int, *, edit_message=None):
    try:
        bots = await db.db.premium_bots.count_documents({})
        stories = await db.db.premium_stories.count_documents({})
        pendings = await db.db.premium_checkout.count_documents({"status": "pending_admin_approval"})
        approved = await db.db.premium_checkout.count_documents({"status": "approved"})
        buyers = await db.db.users.count_documents({"purchases.0": {"$exists": True}})
        total_users = await db.db.users.count_documents({})
        db_ch = await db.db.premium_channels.count_documents({"type": "db"})
        dl_ch = await db.db.premium_channels.count_documents({"type": "delivery"})
        
        try:
            req_query = {"text": {"$regex": "^\\[REQUEST\\]", "$options": "i"}, "status": "open"}
            reqs_count = await db.db.premium_feedback.count_documents(req_query)
        except Exception:
            reqs_count = 0
            
        try:
            total_fbs = await db.db.premium_feedback.count_documents({"status": "open"})
            supp_count = max(0, total_fbs - reqs_count)
        except Exception:
            supp_count = 0

        txt = (
            '<emoji id="5296790785981718487">🏪</emoji> <b>Arya Marketplace Dashboard</b>\n'
            '━━━━━━━━━━━━━━━━━━━━━\n\n'
            '<emoji id="5936143551854285132">📊</emoji> <b>System Overview</b>\n'
            f'<code>  Store Bots       ›  {bots}</code>\n'
            f'<code>  Active Stories   ›  {stories}</code>\n'
            f'<code>  Total Customers  ›  {buyers}</code>\n'
            f'<code>  Registered Users ›  {total_users}</code>\n'
            f'<code>  Source Channels  ›  {db_ch}</code>\n'
            f'<code>  Delivery Pool    ›  {dl_ch}</code>\n\n'
            '<emoji id="6021637109264160908">⚙️</emoji> <b>Management Details</b>\n'
            f'<code>  Pending Orders   ›  {pendings}</code>\n'
            f'<code>  Completed Sales  ›  {approved}</code>\n'
            f'<code>  Story Requests   ›  {reqs_count}</code>\n'
            f'<code>  Support Tickets  ›  {supp_count}</code>\n'
            f'<code>  Engine Status    ›  Online ✅</code>\n'
            '━━━━━━━━━━━━━━━━━━━━━'
        )

        kb = [
            _get_top_mgmt_emoji_row(),
            [
                _ikb("Approval", callback_data="mk#pending", icon_custom_emoji_id="5413643931139219521"),
                _ikb("Requests", callback_data="mk#reqs_0", icon_custom_emoji_id="5766915217552315762")
            ],
            [
                _ikb("Support Tab", callback_data="mk#fb_panel_0", icon_custom_emoji_id="5945256248390721326"),
                _ikb("Channels", callback_data="mk#channels", icon_custom_emoji_id="6021454607513819417")
            ],
            [
                _ikb("Add New Story", callback_data="mk#add_story", icon_custom_emoji_id="5920332557466997677")
            ],
            [
                _ikb("Manage Stories", callback_data="mk#manage_stories", icon_custom_emoji_id="6026337676091726218")
            ],
            [
                _ikb("Bots", callback_data="mk#accounts", icon_custom_emoji_id="6021683099773966917"),
                _ikb("Costumers", callback_data="mk#users", icon_custom_emoji_id="6021487472603568286")
            ],
            [
                _ikb("Settings", callback_data="mk#settings", icon_custom_emoji_id="6021637109264160908")
            ],
            [
                InlineKeyboardButton("ᴄ", callback_data="mk#close"),
                InlineKeyboardButton("ʟ", callback_data="mk#close"),
                _ikb(" ", callback_data="mk#close", icon_custom_emoji_id="5774077015388852135"),
                InlineKeyboardButton("ꜱ", callback_data="mk#close"),
                InlineKeyboardButton("ᴇ", callback_data="mk#close")
            ]
        ]
        markup = InlineKeyboardMarkup(kb)

        mid = getattr(edit_message, "id", None) if edit_message else None
        ok = await _send_or_edit_mgmt_bot_api(client, chat_id, txt, markup, message_id=mid)
        if ok:
            return

        clean_kb = _clean_markup_for_pyrogram(markup)
        if edit_message:
            try:
                return await edit_message.edit_text(txt, reply_markup=clean_kb, parse_mode=enums.ParseMode.HTML)
            except Exception:
                pass
        return await client.send_message(chat_id, txt, reply_markup=clean_kb, parse_mode=enums.ParseMode.HTML)
    except Exception as e:
        logger.error(f"Critical error in _render_home: {e}", exc_info=True)
        emergency_txt = "<b>Arya Marketplace Dashboard</b>\n\n<i>Ecosystem Active &amp; Operational.</i>"
        emergency_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Add New Story", callback_data="mk#add_story")],
            [InlineKeyboardButton("Manage Stories", callback_data="mk#manage_stories")],
            [InlineKeyboardButton("Settings", callback_data="mk#settings")],
            [InlineKeyboardButton("Close", callback_data="mk#close")]
        ])
        if edit_message:
            return await edit_message.edit_text(emergency_txt, reply_markup=emergency_kb, parse_mode=enums.ParseMode.HTML)
        return await client.send_message(chat_id, emergency_txt, reply_markup=emergency_kb, parse_mode=enums.ParseMode.HTML)




def _cfg_list(cfg: dict, key: str):
    v = cfg.get(key)
    return v if isinstance(v, list) else []


@Client.on_message(filters.command("start") & filters.private)
async def mgmt_start(client, message):
    user_id = message.from_user.id
    uname = getattr(message.from_user, "username", "") or ""
    bot_un = getattr(client.me, "username", "UnknownBot")
    logger.info(f"===> [MGMT_START] /start received from user_id={user_id} (@{uname}) on bot @{bot_un}")
    if await _deny_if_not_owner(client, user_id):
        logger.warning(f"===> [MGMT_START] Denied user_id={user_id}")
        return
    logger.info(f"===> [MGMT_START] Authorized user_id={user_id}. Rendering home dashboard...")
    return await _render_home(client, user_id)

# Parse ID helper
def parse_id(msg) -> int:
    if getattr(msg, 'forward_from_message_id', None):
        return msg.forward_from_message_id
    text = (getattr(msg, 'text', None) or getattr(msg, 'caption', None) or "").strip().rstrip('/')
    if text.isdigit(): return int(text)
    if "t.me/" in text:
        parts = text.split('/')
        if parts[-1].isdigit(): return int(parts[-1])
    raise ValueError("Invalid Message ID or Link")


def parse_chat_from_link(text: str):
    t = (text or "").strip()
    if "t.me/c/" in t:
        import re
        m = re.search(r"t\.me/c/(\d+)/(\d+)", t)
        if m:
            return int("-100" + m.group(1)), int(m.group(2))
    if "t.me/" in t:
        import re
        m = re.search(r"t\.me/([^/\s]+)/(\d+)", t.replace("https://", "").replace("http://", ""))
        if m:
            return m.group(1), int(m.group(2))
    return None, None


# In-memory state: users waiting to send banner photo



async def _render_settings(client, query):
    """Renders the top-level settings categories."""
    txt = (
        "<b>Settings Panel</b>\n\n"
        "Select a section below to configure system parameters:\n\n"
        "• <b>Payments:</b> UPI slots, Checkout Pages (1 &amp; 2), Cashfree Gateway, and Gmail Auto-Verification.\n"
        "• <b>More:</b> Groq AI Key and T&amp;C requirement settings.\n\n"
        "<i>Tap any option below:</i>"
    )
    kb = [
        [InlineKeyboardButton("Payments", callback_data="mk#settings_payments")],
        [InlineKeyboardButton("More", callback_data="mk#settings_more")],
        [InlineKeyboardButton("« Back to Dashboard", callback_data="mk#back")]
    ]
    if hasattr(query, "message") and query.message:
        await query.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=enums.ParseMode.HTML)
    else:
        await client.send_message(query.from_user.id, txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=enums.ParseMode.HTML)


async def _render_payments_settings(client, query):
    """Renders the Payments Settings panel: UPI, Checkout 1 & 2, Cashfree, Gmail verify."""
    cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    checkout_mode = cfg.get("checkout_mode", "v1")
    
    gmail_user = cfg.get("gmail_user", "")
    gmail_pwd = cfg.get("gmail_app_password", "")
    gmail_verify_on = cfg.get("gmail_verification_enabled", False)

    upi1 = cfg.get("upi_id", "") or (await db.get_config("upi_id") or "")
    upi2 = cfg.get("upi_id_2", "")
    upi3 = cfg.get("upi_id_3", "")
    upi4 = cfg.get("upi_id_4", "")

    upi1_status = "✅" if upi1 else "❌"
    upi2_status = "✅" if upi2 else "❌"
    upi3_status = "✅" if upi3 else "❌"
    upi4_status = "✅" if upi4 else "❌"

    chk_v1_btn = f"Checkout Page 1: {'✅' if checkout_mode == 'v1' else '❌'}"
    chk_v2_btn = f"Checkout Page 2: {'✅' if checkout_mode == 'v2' else '❌'}"
    
    cf_enabled = cfg.get("cashfree_enabled", False)
    cf_app_id = cfg.get("cashfree_app_id", "") or cfg.get("cashfree_api_id", "")
    cf_status = "✅" if cf_enabled and cf_app_id else "❌"
    
    gmail_user_status = "✅" if gmail_user else "❌"
    gmail_pwd_status = "✅" if gmail_pwd else "❌"
    gmail_verify_btn = f"Gmail Auto-Verify: {'✅' if gmail_verify_on else '❌'}"

    kb = [
        [
            InlineKeyboardButton(f"UPI 1: {upi1_status}", callback_data="mk#set_upi_1"),
            InlineKeyboardButton(f"UPI 2: {upi2_status}", callback_data="mk#set_upi_2")
        ],
        [
            InlineKeyboardButton(f"UPI 3: {upi3_status}", callback_data="mk#set_upi_3"),
            InlineKeyboardButton(f"UPI 4: {upi4_status}", callback_data="mk#set_upi_4")
        ],
        [
            InlineKeyboardButton(chk_v1_btn, callback_data="mk#toggle_checkout_v1"),
            InlineKeyboardButton(chk_v2_btn, callback_data="mk#toggle_checkout_v2")
        ],
        [InlineKeyboardButton(f"Cashfree Settings: {cf_status}", callback_data="mk#cashfree_menu")],
        [InlineKeyboardButton(f"Set Gmail: {gmail_user_status}", callback_data="mk#set_gmail_user")],
        [InlineKeyboardButton(f"Set Gmail Pwd: {gmail_pwd_status}", callback_data="mk#set_gmail_pwd")],
        [InlineKeyboardButton(gmail_verify_btn, callback_data="mk#toggle_gmail_verify")],
        [InlineKeyboardButton("« Back to Settings", callback_data="mk#settings")]
    ]
    txt = (
        "<b>Payments &amp; Gateway Settings</b>\n\n"
        "• <b>UPI Slots:</b> Rotate up to 4 UPI IDs for direct transfer.\n"
        "• <b>Checkout Mode:</b> Page 1 (Razorpay + Manual UPI) / Page 2 (Direct UPI + Cashfree + Crypto).\n"
        "• <b>Cashfree:</b> Cards, NetBanking, and UPI payment gateway.\n"
        "• <b>Gmail Verification:</b> Automated verification for Direct UPI payments.\n\n"
        "<i>Tap any option below to configure or toggle:</i>"
    )
    if hasattr(query, "message") and query.message:
        await query.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=enums.ParseMode.HTML)
    else:
        await client.send_message(query.from_user.id, txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=enums.ParseMode.HTML)


async def _render_more_settings(client, query):
    """Renders the More Settings panel: Groq AI and T&C requirement."""
    groq_key_raw = await db.get_config("groq_api_key")
    groq_status = "✅" if groq_key_raw else "❌"

    cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    tnc_on = cfg.get("tnc_enabled", True)

    tnc_btn = f"T&C Requirement: {'✅' if tnc_on else '❌'}"

    kb = [
        [InlineKeyboardButton(f"Groq AI Key: {groq_status}", callback_data="mk#set_groq")],
        [InlineKeyboardButton(tnc_btn, callback_data="mk#toggle_tnc")],
        [InlineKeyboardButton("« Back to Settings", callback_data="mk#settings")]
    ]
    txt = (
        "<b>More System Settings</b>\n\n"
        "• <b>Groq AI:</b> Transliterate story names and translate descriptions.\n"
        "• <b>T&amp;C Requirement:</b> Require users to accept terms before purchasing.\n\n"
        "<i>Tap any option below:</i>"
    )
    if hasattr(query, "message") and query.message:
        await query.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=enums.ParseMode.HTML)
    else:
        await client.send_message(query.from_user.id, txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=enums.ParseMode.HTML)


async def _render_cashfree_settings(client, query):
    """Renders the Cashfree Payment Gateway settings menu."""
    cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    cf_enabled = cfg.get("cashfree_enabled", False)
    cf_app_id = cfg.get("cashfree_app_id", "")
    cf_secret = cfg.get("cashfree_secret_key", "")
    cf_env = cfg.get("cashfree_env", "production")

    cf_toggle_btn = f"Cashfree Gateway: {'✅' if cf_enabled else '❌'}"
    app_id_lbl = f"App ID: {'✅' if cf_app_id else '❌'}"
    secret_lbl = f"Secret Key: {'✅' if cf_secret else '❌'}"
    env_lbl = f"Environment: {cf_env.upper()}"

    kb = [
        [InlineKeyboardButton(cf_toggle_btn, callback_data="mk#toggle_cashfree")],
        [InlineKeyboardButton(app_id_lbl, callback_data="mk#set_cf_app_id")],
        [InlineKeyboardButton(secret_lbl, callback_data="mk#set_cf_secret")],
        [InlineKeyboardButton(env_lbl, callback_data="mk#toggle_cf_env")],
        [InlineKeyboardButton("« Back to Settings", callback_data="mk#settings")]
    ]
    txt = (
        "<b>💳 Cashfree Payment Gateway Settings</b>\n\n"
        f"<b>Status:</b> {'✅ Active & Accepting Payments' if cf_enabled and cf_app_id and cf_secret else '❌ Inactive / Disabled'}\n"
        f"<b>Environment:</b> <code>{cf_env}</code>\n"
        f"<b>App ID:</b> <code>{cf_app_id or 'Not Set'}</code>\n"
        f"<b>Secret Key:</b> <code>{'••••••••••••••••' if cf_secret else 'Not Set'}</code>\n\n"
        "<i>💡 Customers can pay instantly via Credit/Debit Cards, NetBanking, and UPI (GPay, PhonePe, Paytm). "
        "Upon successful payment, digital files are delivered automatically.</i>"
    )
    await query.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=enums.ParseMode.HTML)


@Client.on_callback_query(filters.regex(r'^mk#'))
async def market_callback(client, query):
    try:
        user_id = query.from_user.id
        if not (_is_owner(user_id) or (await _is_owner_db(user_id))):
            return await _safe_answer(query, "Access denied.", show_alert=True)
        data = query.data.split('#')
        cmd = data[1]

        # ════════════════════════════════════════════
        # CASHFREE GATEWAY SETTINGS
        # ════════════════════════════════════════════
        if cmd == "cashfree_menu":
            return await _render_cashfree_settings(client, query)

        elif cmd == "toggle_cashfree":
            cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
            curr = cfg.get("cashfree_enabled", False)
            new_st = not curr
            await db.db.mini_app_config.update_one({"_key": "feature_toggles"}, {"$set": {"cashfree_enabled": new_st}}, upsert=True)
            await _safe_answer(query, f"Cashfree Payments set to: {'ON' if new_st else 'OFF'}", show_alert=True)
            return await _render_cashfree_settings(client, query)

        elif cmd == "toggle_cf_env":
            cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
            curr_env = cfg.get("cashfree_env", "production")
            new_env = "sandbox" if curr_env == "production" else "production"
            await db.db.mini_app_config.update_one({"_key": "feature_toggles"}, {"$set": {"cashfree_env": new_env}}, upsert=True)
            await _safe_answer(query, f"Cashfree Environment set to: {new_env.upper()}", show_alert=True)
            return await _render_cashfree_settings(client, query)

        elif cmd == "set_cf_app_id":
            from pyrogram.types import CallbackQuery as _CQ
            await _safe_answer(query)
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Cancel", callback_data="ask_cancel")]])
            
            cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
            curr_app_id = cfg.get("cashfree_app_id", "") or cfg.get("cashfree_api_id", "")
            curr_hint = f"\n\n<i>Current App ID: <code>{curr_app_id}</code></i>" if curr_app_id else ""
            
            msg = await native_ask(
                client, user_id, 
                f"<b>🔑 Enter your Cashfree App ID / Client ID:</b>{curr_hint}\n\n"
                "<i>Paste your Cashfree PG App ID / Client ID below:</i>", 
                reply_markup=cancel_kb,
                parse_mode=enums.ParseMode.HTML
            )
            if isinstance(msg, _CQ) or not getattr(msg, 'text', None):
                return await client.send_message(
                    user_id, 
                    "<i>Process Cancelled. Cashfree App ID unchanged.</i>",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back to Cashfree Settings", callback_data="mk#cashfree_menu")]])
                )
            
            val = msg.text.strip()
            await db.db.mini_app_config.update_one(
                {"_key": "feature_toggles"}, 
                {"$set": {"cashfree_app_id": val, "cashfree_api_id": val}}, 
                upsert=True
            )
            await client.send_message(
                user_id, 
                f"✅ <b>Cashfree App ID saved successfully!</b>\n<code>{val}</code>",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Cashfree Settings", callback_data="mk#cashfree_menu")]]),
                parse_mode=enums.ParseMode.HTML
            )
            return

        elif cmd == "set_cf_secret":
            from pyrogram.types import CallbackQuery as _CQ
            await _safe_answer(query)
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Cancel", callback_data="ask_cancel")]])
            
            cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
            curr_secret = cfg.get("cashfree_secret_key", "")
            curr_hint = f"\n\n<i>Current Secret Key: <code>{curr_secret[:6]}••••••••</code></i>" if curr_secret else ""
            
            msg = await native_ask(
                client, user_id, 
                f"<b>🔒 Enter your Cashfree Client Secret Key:</b>{curr_hint}\n\n"
                "<i>Paste your Cashfree Secret Key below:</i>", 
                reply_markup=cancel_kb,
                parse_mode=enums.ParseMode.HTML
            )
            if isinstance(msg, _CQ) or not getattr(msg, 'text', None):
                return await client.send_message(
                    user_id, 
                    "<i>Process Cancelled. Cashfree Secret Key unchanged.</i>",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back to Cashfree Settings", callback_data="mk#cashfree_menu")]])
                )
            
            val = msg.text.strip()
            await db.db.mini_app_config.update_one(
                {"_key": "feature_toggles"}, 
                {"$set": {"cashfree_secret_key": val}}, 
                upsert=True
            )
            await client.send_message(
                user_id, 
                "✅ <b>Cashfree Secret Key saved securely!</b>",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Cashfree Settings", callback_data="mk#cashfree_menu")]]),
                parse_mode=enums.ParseMode.HTML
            )
            return

        if cmd == "close":
            return await query.message.delete()



        elif cmd == "back":
            await _safe_answer(query)
            await _render_home(client, query.from_user.id, edit_message=query.message)
            return

        elif cmd == "settings":
            await _safe_answer(query)
            await _render_settings(client, query)

        elif cmd == "settings_payments":
            await _safe_answer(query)
            await _render_payments_settings(client, query)

        elif cmd == "settings_more":
            await _safe_answer(query)
            await _render_more_settings(client, query)



        elif cmd == "toggle_tnc":
            await _safe_answer(query)
            cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
            current = cfg.get("tnc_enabled", True)
            new_val = not current
            await db.db.mini_app_config.update_one(
                {"_key": "feature_toggles"},
                {"$set": {"tnc_enabled": new_val}},
                upsert=True
            )
            status = "✅ ON" if new_val else "❌ OFF"
            await query.answer(f"T&C Requirement: {status}", show_alert=True)
            await _render_more_settings(client, query)

        elif cmd == "toggle_checkout_v1":
            await _safe_answer(query)
            await db.db.mini_app_config.update_one(
                {"_key": "feature_toggles"},
                {"$set": {"checkout_mode": "v1"}},
                upsert=True
            )
            await query.answer("Checkout Mode Set To: Page 1 (Razorpay + Manual UPI)", show_alert=True)
            await _render_payments_settings(client, query)

        elif cmd == "toggle_checkout_v2":
            await _safe_answer(query)
            await db.db.mini_app_config.update_one(
                {"_key": "feature_toggles"},
                {"$set": {"checkout_mode": "v2"}},
                upsert=True
            )
            await query.answer("Checkout Mode Set To: Page 2 (Direct UPI + Cashfree + Crypto)", show_alert=True)
            await _render_payments_settings(client, query)

        elif cmd == "toggle_gmail_verify":
            await _safe_answer(query)
            cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
            current = cfg.get("gmail_verification_enabled", False)
            new_val = not current
            await db.db.mini_app_config.update_one(
                {"_key": "feature_toggles"},
                {"$set": {"gmail_verification_enabled": new_val}},
                upsert=True
            )
            status = "✅ ON" if new_val else "❌ OFF"
            await query.answer(f"Gmail Auto-Verify: {status}", show_alert=True)
            await _render_payments_settings(client, query)

        # ── Support Panel (Feedback/Suggestions) ──
        elif cmd.startswith("fb_panel_"):
            await _safe_answer(query)
            page = int(cmd.replace("fb_panel_", ""))
            items_pp = 8
            filter_query = {"text": {"$not": {"$regex": "^\\[REQUEST\\]", "$options": "i"}}}
            total = await db.db.premium_feedback.count_documents(filter_query)
            total_pages = max(1, (total + items_pp - 1) // items_pp)
            if page >= total_pages: page = total_pages - 1
            if page < 0: page = 0
            fbs = await db.db.premium_feedback.find(filter_query).sort("created_at", -1).skip(page * items_pp).limit(items_pp).to_list(length=items_pp)
            open_c = await db.db.premium_feedback.count_documents(dict(filter_query, status="open"))
            solved_c = await db.db.premium_feedback.count_documents(dict(filter_query, status="solved"))
            kb = []
            for fb in fbs:
                fb_id = str(fb['_id'])
                status_icon = "🟢" if fb.get("status") == "solved" else "🔴"
                uname = fb.get("user_name") or str(fb.get("user_id"))
                if len(uname) > 16: uname = uname[:14] + ".."
                txt_preview = (fb.get("text") or "")[:25].replace("\n", " ")
                if len(txt_preview) == 25: txt_preview += ".."
                kb.append([InlineKeyboardButton(f"{status_icon} {uname} — {txt_preview}", callback_data=f"mk#fb_view_{fb_id}")])
            nav = []
            if page > 0: nav.append(InlineKeyboardButton("❬ Prev", callback_data=f"mk#fb_panel_{page-1}"))
            if page < total_pages - 1: nav.append(InlineKeyboardButton("Next ❭", callback_data=f"mk#fb_panel_{page+1}"))
            if nav: kb.append(nav)
            kb.append([InlineKeyboardButton("« " + utils.to_smallcap("Home"), callback_data="mk#back")])
            panel_txt = (
                f"<b>⟦ Support Panel ⟧</b>\n\n"
                f"<blockquote expandable>"
                f"Open: <code>{open_c}</code>   Solved: <code>{solved_c}</code>   Total: <code>{total}</code>\n\n"
                f"<i>Tap any entry to view details and reply.</i>"
                f"</blockquote>"
            )
            await query.message.edit_text(panel_txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=enums.ParseMode.HTML)

        elif cmd.startswith("fb_view_"):
            await _safe_answer(query)
            fb_id = cmd.replace("fb_view_", "")
            try:
                from bson import ObjectId
                fb = await db.db.premium_feedback.find_one({"_id": ObjectId(fb_id)})
            except: fb = None
            if not fb:
                return await _safe_answer(query, "Feedback not found.", show_alert=True)
            status = fb.get("status", "open")
            status_label = "🟢 Solved" if status == "solved" else "🔴 Open"
            created = fb.get("created_at")
            date_str = created.strftime('%d %b %Y, %H:%M UTC') if hasattr(created, 'strftime') else "N/A"
            uname_display = f"@{fb.get('username')}" if fb.get('username') else "N/A"
            fb_txt = (
                f"<b>⟦ Feedback Detail ⟧</b>\n\n"
                f"<blockquote>"
                f"<b>ID:</b> <code>{fb_id[:8]}</code>\n"
                f"<b>User:</b> {fb.get('user_name', 'N/A')} (<code>{fb.get('user_id')}</code>)\n"
                f"<b>Username:</b> {uname_display}\n"
                f"<b>Date:</b> {date_str}\n"
                f"<b>Status:</b> {status_label}"
                f"</blockquote>\n\n"
                f"<b>Message:</b>\n<blockquote expandable>{(fb.get('text') or '')[:1000]}</blockquote>"
            )
            resolve_label = "Mark Solved" if status == "open" else "Reopen"
            resolve_cb = f"mk#fbresv_{fb_id}"
            kb = [
                [InlineKeyboardButton(resolve_label, callback_data=resolve_cb),
                 InlineKeyboardButton("Reply to User", callback_data=f"mk#fbreply_{fb_id}_{fb.get('user_id')}")],
                [InlineKeyboardButton("Delete Entry", callback_data=f"mk#fbdel_{fb_id}")],
                [InlineKeyboardButton("« Back", callback_data="mk#fb_panel_0")]
            ]
            await query.message.edit_text(fb_txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=enums.ParseMode.HTML)

        elif cmd.startswith("fbresv_"):
            fb_id = cmd.replace("fbresv_", "")
            try:
                from bson import ObjectId
                fb = await db.db.premium_feedback.find_one({"_id": ObjectId(fb_id)})
            except: fb = None
            if not fb:
                return await _safe_answer(query, "Not found.", show_alert=True)
            new_status = "solved" if fb.get("status") == "open" else "open"
            await db.db.premium_feedback.update_one({"_id": fb['_id']}, {"$set": {"status": new_status, "resolved_at": datetime.utcnow()}})
            await _safe_answer(query, f"Marked as {new_status}!", show_alert=True)
            query.data = f"mk#fb_view_{fb_id}"
            return await market_callback(client, query)

        elif cmd.startswith("fbdel_"):
            fb_id = cmd.replace("fbdel_", "")
            try:
                from bson import ObjectId
                await db.db.premium_feedback.delete_one({"_id": ObjectId(fb_id)})
            except: pass
            await _safe_answer(query, "Feedback entry deleted.", show_alert=True)
            query.data = "mk#fb_panel_0"
            return await market_callback(client, query)

        elif cmd.startswith("fbreply_"):
            # format: fbreply_{fb_id}_{user_id}
            # Use rsplit to safely extract user_id from the end
            suffix = cmd[len("fbreply_"):]
            parts_r = suffix.rsplit("_", 1)  # split on LAST underscore => [fb_id, user_id]
            fb_id = parts_r[0] if len(parts_r) > 0 else ""
            target_uid = int(parts_r[1]) if len(parts_r) > 1 and parts_r[1].isdigit() else 0
            await _safe_answer(query)  # just ack the tap, don't delete message
            asyncio.create_task(_fb_reply_flow(client, user_id, fb_id, target_uid))

        elif cmd.startswith("reqs_"):
            page = int(cmd.replace("reqs_", ""))
            # Fetch all story requests — now includes both bot & mini app
            reqs = await db.db.premium_requests.find({}).sort("created_at", -1).to_list(length=None)
            
            if not reqs:
                if "query" in locals() and query:
                    return await query.answer("No story requests yet.", show_alert=True)
                return
                
            items_per_page = 10
            total_pages = max(1, (len(reqs) + items_per_page - 1) // items_per_page)
            if page < 0: page = 0
            if page >= total_pages: page = total_pages - 1
            
            subset = reqs[page*items_per_page : (page+1)*items_per_page]
            
            # Count by status for header
            pending_c = sum(1 for r in reqs if r.get('status', '').lower() in ('pending', 'sent', ''))
            active_c = sum(1 for r in reqs if r.get('status', '').lower() in ('searching', 'posting', 'posted'))
            done_c = sum(1 for r in reqs if r.get('status', '').lower() in ('completed', 'rejected'))
            
            txt_req = (
                f"<b>╔══════════════════════╗</b>\n"
                f"<b>        𝗦𝗧𝗢𝗥𝗬 𝗥𝗘𝗤𝗨𝗘𝗦𝗧𝗦</b>\n"
                f"<b>╚══════════════════════╝</b>\n\n"
                f"<b>⧉ PAGE {page+1} 𝗢𝗙 {total_pages}</b> | Total: {len(reqs)}\n"
                f"⏳ Pending: {pending_c}  🔄 Active: {active_c}  ✅ Done: {done_c}\n"
                f"<i>Click an entry to view details & update status:</i>\n"
            )
            kb = []
            for r in subset:
                # Show story name or first 22 chars of text
                sname = r.get('story_name') or r.get('text', 'Unknown')
                if len(sname) > 22: sname = sname[:20] + ".."
                stt = r.get('status', 'Pending').upper()
                # Source badge
                src = r.get('source', 'bot')
                src_tag = "📱" if src in ("mini_app", "mini_app_legacy") else "🤖"
                kb.append([InlineKeyboardButton(f"{src_tag} {sname} [{stt}]", callback_data=f"mk#req_{str(r['_id'])}")])
            
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton("PREVIOUS", callback_data=f"mk#reqs_{page-1}"))
            if page < total_pages - 1:
                nav.append(InlineKeyboardButton("NEXT", callback_data=f"mk#reqs_{page+1}"))
            if nav: kb.append(nav)
            
            kb.append([InlineKeyboardButton("BACK TO DASHBOARD", callback_data="mk#back")])
            
            await query.message.edit_text(txt_req, reply_markup=InlineKeyboardMarkup(kb), parse_mode=enums.ParseMode.HTML)
            return

        elif cmd.startswith("req_") and len(cmd) > 10:
            req_id = cmd.replace("req_", "")
            try:
                from bson import ObjectId
                r = await db.db.premium_requests.find_one({"_id": ObjectId(req_id)})
            except: r = None
            
            if not r:
                if "query" in locals() and query:
                    return await query.answer("Request not found.", show_alert=True)
                return
                
            t_str = r.get("created_at").strftime('%d %b %Y, %H:%M') if r.get("created_at") else "Unknown"
            status = r.get('status', 'Pending').upper()
            src = r.get('source', 'bot')
            src_label = "📱 Mini App" if src in ("mini_app", "mini_app_legacy") else "🤖 Bot"
            
            # For mini_app requests: story_name is the full text message from user
            story_display = r.get('story_name', 'Unknown')
            text_detail = r.get('text', '')
            if text_detail and text_detail != story_display:
                # Show text separately if different from story_name
                text_block = f'<blockquote expandable="true">{text_detail[:300]}</blockquote>\n'
            else:
                text_block = ''
            
            txt_d = (
                f"<b>╔══════════════════════╗</b>\n"
                f"<b>        𝗥𝗘𝗤𝗨𝗘𝗦𝗧 𝗗𝗘𝗧𝗔𝗜𝗟𝗦</b>\n"
                f"<b>╚══════════════════════╝</b>\n\n"
                f"<b>⧉ SOURCE:  {src_label}</b>\n\n"
                f"<b>⧉ STORY INFO</b>\n"
                f'<blockquote expandable="true">'
                f"<b>• NAME      ⟶</b> {story_display}\n"
                f"<b>• PLATFORM  ⟶</b> {r.get('platform', 'N/A')}\n"
                f"<b>• TYPE      ⟶</b> {r.get('completion_type', 'N/A')}\n"
                f"<b>• REQUESTED ⟶</b> {t_str}\n"
                f'</blockquote>\n'
                f"{text_block}"
                f"<b>⧉ USER INFO</b>\n"
                f'<blockquote expandable="true">'
                f"<b>• USER ID   ⟶</b> <code>{r.get('user_id')}</code>\n"
                f"<b>• USERNAME  ⟶</b> @{r.get('username') or 'N/A'}\n"
                f"<b>• BOT ID    ⟶</b> <code>{r.get('bot_id')}</code>\n"
                f'</blockquote>\n'
                f"<b>⧉ CURRENT STATUS</b>\n"
                f"<blockquote><b>[ {status} ]</b></blockquote>\n\n"
                f"<i>Update status to notify user:</i>"
            )
            
            kb = [
                [InlineKeyboardButton("⏳ PENDING", callback_data=f"mk#rstat#{req_id}#Pending"),
                 InlineKeyboardButton("🔍 SEARCHING", callback_data=f"mk#rstat#{req_id}#Searching")],
                [InlineKeyboardButton("📤 POSTING", callback_data=f"mk#rstat#{req_id}#Posting"),
                 InlineKeyboardButton("✅ POSTED", callback_data=f"mk#rstat#{req_id}#Posted")],
                [InlineKeyboardButton("🎉 COMPLETED", callback_data=f"mk#rstat#{req_id}#Completed")],
                [InlineKeyboardButton("❌ REJECT & REMOVE", callback_data=f"mk#req_rej#{req_id}")],
                [InlineKeyboardButton("BACK TO LIST", callback_data="mk#reqs_0")]
            ]
            await query.message.edit_text(txt_d, reply_markup=InlineKeyboardMarkup(kb), parse_mode=enums.ParseMode.HTML)
            return

        elif cmd == "rstat":
            req_id = data[2]
            new_status = data[3]
            try:
                from bson import ObjectId
                r = await db.db.premium_requests.find_one({"_id": ObjectId(req_id)})
            except: r = None
            if not r: 
                if "query" in locals() and query:
                    return await query.answer("Not found.", show_alert=True)
                return
            
            await db.db.premium_requests.update_one({"_id": r['_id']}, {"$set": {"status": new_status, "updated_at": datetime.now()}})
            
            # Log to Arya Core Log
            try:
                from utils import log_arya_event
                user_info = await db.get_user(r.get('user_id'))
                await log_arya_event(
                    "STORY REQUEST UPDATED", r.get('user_id'), user_info or {}, 

                    f"<b>Story:</b> {r.get('story_name')}\n<b>Old Status:</b> {r.get('status')}\n<b>New Status:</b> {new_status}"
                )
            except Exception as e:
                logger.error(f"Failed to log request update: {e}")
                
            # Alert User via Store Bot
            try:
                bot_id_str = str(r.get('bot_id'))
                from plugins.userbot.market_seller import market_clients
                seller_cli = market_clients.get(bot_id_str)
                if seller_cli:
                    u_id = r.get('user_id')
                    alert_txt = (
                        f"🛎️ <b>Update on your Story Request!</b>\n\n"
                        f"<b>Story:</b> {r.get('story_name')}\n"
                        f"<b>New Status:</b> <code>{new_status}</code>\n\n"
                        f"<i>Check 'My Requests' in your Profile for more info!</i>"
                    )
                    await seller_cli.send_message(u_id, alert_txt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Open Profile", callback_data="mb#main_profile")]]))
            except Exception as e:
                logger.error(f"Failed to DM user about request: {e}")
                
            if "query" in locals() and query:
                await query.answer(f"Status updated to {new_status} and user notified!", show_alert=True)
            
            # Reload manage screen directly
            query.data = f"mk#req_{req_id}"
            return await market_callback(client, query)

        elif cmd == "req_rej":
            req_id = data[2]
            try: await query.message.delete()
            except: pass
            if "query" in locals() and query: await query.answer()
            asyncio.create_task(_reject_request_flow(client, user_id, req_id))

        elif cmd == "back":
            if "query" in locals() and query:
                await query.answer()
            return await _render_home(client, user_id, edit_message=query.message)

        elif cmd in ["set_groq", "set_gmail_user", "set_gmail_pwd"] or cmd.startswith("set_upi"):
            await query.message.delete()
            asyncio.create_task(_settings_flow(client, user_id, cmd))

        elif cmd.startswith("st_pay_methods_"):
            # Toggle payment methods for a story: mk#st_pay_methods_<s_id>_<method>
            parts = cmd.split("_")
            # format: st_pay_methods_<s_id>_<method> where s_id is parts[3] and method is parts[4]
            s_id = parts[3]
            method = parts[4]  # 'upi' or 'razorpay'
            from bson.objectid import ObjectId
            story = await db.db.premium_stories.find_one({"_id": ObjectId(s_id)})
            if not story:
                return await _safe_answer(query, "Story not found!", show_alert=True)
            current = story.get("payment_methods", ["upi", "razorpay"])
            if method in current:
                if len(current) <= 1:
                    return await _safe_answer(query, "⚠️ At least one payment method must remain enabled!", show_alert=True)
                current.remove(method)
            else:
                current.append(method)
            await db.db.premium_stories.update_one({"_id": ObjectId(s_id)}, {"$set": {"payment_methods": current}})
            # Reload the story view
            query.data = f"mk#st_view_{s_id}"
            return await market_callback(client, query)

        # ── Channels System ──
        elif cmd == "channels":
            await _safe_answer(query)
            db_channels = await db.db.premium_channels.find({"type": "db"}).to_list(length=None)
            delivery_channels = await db.db.premium_channels.find({"type": "delivery"}).to_list(length=None)
            kb = [
                [InlineKeyboardButton(f"Source Channels ({len(db_channels)})", callback_data="mk#ch_list_db")],
                [InlineKeyboardButton("Add Source", callback_data="mk#ch_add_db"),
                 InlineKeyboardButton("Add Bulk", callback_data="mk#ch_bulk_db")],
                [InlineKeyboardButton(f"Delivery Channels ({len(delivery_channels)})", callback_data="mk#ch_list_delivery")],
                [InlineKeyboardButton("Add DC", callback_data="mk#ch_add_delivery"),
                 InlineKeyboardButton("Add Bulk", callback_data="mk#ch_bulk_delivery")],
                [InlineKeyboardButton("Sync Names", callback_data="mk#ch_sync_db"),
                 InlineKeyboardButton("Sync Delivery", callback_data="mk#ch_sync_delivery")],
                [InlineKeyboardButton("« Back", callback_data="mk#back")]
            ]
            await query.message.edit_text(
                "<b>Channels Manager</b>\n\n"
                "• <b>Source Channels:</b> Storage channels containing story files.\n"
                "• <b>Delivery Channels:</b> Private channels used to deliver instant invite links for buyers.\n\n"
                "<i>Manage your channels from one central panel.</i>",
                reply_markup=InlineKeyboardMarkup(kb)
            )

        elif cmd.startswith("ch_list_"):
            await _safe_answer(query)
            parts = cmd.split("_")
            ch_type = parts[2]
            page = int(parts[3]) if len(parts) > 3 else 0
            
            channels = await db.db.premium_channels.find({"type": ch_type}).to_list(length=None)
            label = "Source" if ch_type == "db" else "Delivery"
            
            items_per_page = 10
            total_pages = max(1, (len(channels) + items_per_page - 1) // items_per_page)
            if page >= total_pages: page = total_pages - 1
            if page < 0: page = 0
            
            start_idx = page * items_per_page
            end_idx = start_idx + items_per_page
            pg_chans = channels[start_idx:end_idx]
            
            kb = []
            for ch in pg_chans:
                cid = ch.get('channel_id')
                name = ch.get('name', str(cid))
                kb.append([InlineKeyboardButton(f"{name} ({cid})", callback_data=f"mk#ch_view_{ch_type}_{cid}")])
            
            nav_row = []
            if page > 0:
                nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"mk#ch_list_{ch_type}_{page-1}"))
            if total_pages > 1:
                nav_row.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="mk#ignore"))
            if page < total_pages - 1:
                nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"mk#ch_list_{ch_type}_{page+1}"))
            if nav_row:
                kb.append(nav_row)

            kb.append([
                InlineKeyboardButton(f"Add {label}", callback_data=f"mk#ch_add_{ch_type}"),
                InlineKeyboardButton("Add Bulk", callback_data=f"mk#ch_bulk_{ch_type}")
            ])
            if len(channels) > 1:
                kb.append([InlineKeyboardButton("Delete All", callback_data=f"mk#ch_delall_{ch_type}")])
            kb.append([InlineKeyboardButton("Sync Names", callback_data=f"mk#ch_sync_{ch_type}")])
            kb.append([InlineKeyboardButton("« Back", callback_data="mk#channels")])
            
            ch_lines = "\n".join(f"• <code>{c['channel_id']}</code> — {c.get('name', '?')}" for c in pg_chans) or "<i>None added yet.</i>"
            await query.message.edit_text(
                f"<b>{label} Channels {f'(Page {page+1}/{total_pages})' if total_pages > 1 else ''}</b>\n\n{ch_lines}",
                reply_markup=InlineKeyboardMarkup(kb)
            )

        elif cmd.startswith("ch_view_"):
            parts = cmd.split("_")
            ch_type = parts[2]
            ch_id = int(parts[3])
            await _safe_answer(query)
            kb = [
                [InlineKeyboardButton("🗑 Remove", callback_data=f"mk#ch_rm_{ch_type}_{ch_id}")],
                [InlineKeyboardButton("« Back", callback_data=f"mk#ch_list_{ch_type}")]
            ]
            await query.message.edit_text(
                f"<b>Channel Info</b>\n\n<b>ID:</b> <code>{ch_id}</code>\n<b>Type:</b> {ch_type}",
                reply_markup=InlineKeyboardMarkup(kb)
            )

        elif cmd.startswith("ch_rm_"):
            parts = cmd.split("_")
            ch_type = parts[2]
            ch_id = int(parts[3])
            await db.db.premium_channels.delete_one({"channel_id": ch_id, "type": ch_type})
            await _safe_answer(query, "Channel Removed!", show_alert=True)
            query.data = f"mk#ch_list_{ch_type}"
            return await market_callback(client, query)

        elif cmd.startswith("ch_delall_"):
            ch_type = cmd.replace("ch_delall_", "")
            await db.db.premium_channels.delete_many({"type": ch_type})
            await _safe_answer(query, "All channels deleted!", show_alert=True)
            query.data = "mk#channels"
            return await market_callback(client, query)

        elif cmd.startswith("ch_sync_"):
            ch_type = cmd.replace("ch_sync_", "")
            channels = await db.db.premium_channels.find({"type": ch_type}).to_list(length=None)
            updated = failed = 0
            for ch in channels:
                try:
                    info = await client.get_chat(ch["channel_id"])
                    title = getattr(info, "title", None) or ch.get("name") or str(ch["channel_id"])
                    await db.db.premium_channels.update_one(
                        {"_id": ch["_id"]},
                        {"$set": {"name": title}}
                    )
                    updated += 1
                except Exception:
                    failed += 1
            await _safe_answer(query, f"Sync done: {updated} updated, {failed} failed", show_alert=True)
            query.data = f"mk#ch_list_{ch_type}"
            return await market_callback(client, query)

        elif cmd.startswith("ch_add_"):
            ch_type = cmd.replace("ch_add_", "")
            await query.message.delete()
            asyncio.create_task(_add_channel_flow(client, user_id, ch_type))

        elif cmd == "ch_bulk_db":
            await query.message.delete()
            asyncio.create_task(_bulk_add_source_channels(client, user_id))

        elif cmd in ("ch_bulk_delivery", "ch_bulk_del"):
            await query.message.delete()
            asyncio.create_task(_bulk_add_delivery_channels(client, user_id))

        elif cmd == "accounts":
            await _safe_answer(query)
            bots = await db.db.premium_bots.find().to_list(length=10)
            kb = []
            for b in bots:
                kb.append([InlineKeyboardButton(f"{b.get('name', b.get('username', 'Bot'))}", callback_data=f"mk#bot_view_{b['id']}")])
            kb.append([InlineKeyboardButton('➕ Aᴅᴅ Bᴏᴛ', callback_data="mk#add_bot")])
            kb.append([InlineKeyboardButton("« Back", callback_data="mk#back")])
            await query.message.edit_text("<b>🤖 <u>Premium Accounts</u></b>\n\nSelect a bot to configure, or add a new one:", reply_markup=InlineKeyboardMarkup(kb))

        elif cmd == "users":
            await _safe_answer(query)
            # Merge bot buyers + mini app buyers
            bot_buyers = await db.db.users.find({"purchases.0": {"$exists": True}}).sort("id", -1).to_list(length=200)
            bot_buyer_ids = {u.get("id") for u in bot_buyers}
            # Also get mini app only buyers (paid via Razorpay but no bot purchases)
            miniapp_only_ids = set()
            try:
                async for order in db.db.orders.find({"status": "paid"}, {"user_id": 1}):
                    oid = order.get("user_id")
                    if oid and oid not in bot_buyer_ids:
                        miniapp_only_ids.add(oid)
            except Exception:
                pass

            kb = []
            for u in bot_buyers:
                uid = u.get("id")
                p_count = len(u.get('purchases', []))
                kb.append([InlineKeyboardButton(f"🤖 {uid} • {p_count} stories", callback_data=f"mk#usr_view_{uid}")])
            for uid in miniapp_only_ids:
                kb.append([InlineKeyboardButton(f"📱 {uid} • Mini App buyer", callback_data=f"mk#usr_view_{uid}")])

            total_buyers = len(bot_buyers) + len(miniapp_only_ids)
            if total_buyers:
                kb.append([InlineKeyboardButton("📢 Message All Buyers", callback_data="mk#usr_msg_all"),
                            InlineKeyboardButton("📤 Export All", callback_data="mk#usr_export_all")])
            kb.append([InlineKeyboardButton("« Back", callback_data="mk#back")])
            await query.message.edit_text(
                f"<b>👥 Buyers ({total_buyers})</b>\n\n🤖 = Bot buyer  |  📱 = Mini App only\n\nTap a user to manage.",
                reply_markup=InlineKeyboardMarkup(kb),
            )

        elif cmd.startswith("usr_view_"):
            uid = int(cmd.split("_")[2])
            user_doc = await db.db.users.find_one({"id": uid}) or {"id": uid}
            tg_user = None
            try:
                tg_user = await client.get_users(uid)
            except Exception:
                pass
            purchases = user_doc.get("purchases", [])
            joined = user_doc.get("joined_date", "N/A")
            lang = user_doc.get("lang", "en")

            # Also count mini app orders
            miniapp_order_story_ids = []
            try:
                async for order in db.db.orders.find({"user_id": uid, "status": "paid"}, {"story_ids": 1}):
                    miniapp_order_story_ids.extend([str(s) for s in order.get("story_ids", [])])
            except Exception:
                pass
            bot_ids_set = {str(p) for p in purchases}
            app_only_count = len(set(miniapp_order_story_ids) - bot_ids_set)
            all_story_count = len(bot_ids_set) + app_only_count

            checkouts = await db.db.premium_checkout.find({"user_id": uid}).sort("_id", -1).to_list(length=20)

            if hasattr(joined, "strftime"):
                joined = joined.strftime('%d %b %Y')

            db_fn = user_doc.get("first_name", "") or ""
            db_ln = user_doc.get("last_name", "") or ""
            db_un = user_doc.get("username", "") or ""
            
            tg_fn = getattr(tg_user, 'first_name', '') or ''
            tg_ln = getattr(tg_user, 'last_name', '') or ''
            tg_un = getattr(tg_user, 'username', '') or ''
            
            first_name = tg_fn or db_fn or "User"
            last_name = tg_ln or db_ln or ""
            username_val = tg_un or db_un or ""
            
            name = f"{first_name} {last_name}".strip() or "Unknown"
            uname = f"@{username_val}" if username_val else "N/A"
            lang_label = "English" if lang == 'en' else "हिंदी"

            lines = []
            for c in checkouts[:6]:
                st = await db.db.premium_stories.find_one({"_id": c.get("story_id")})
                sn = st.get("story_name_en", "Unknown") if st else "Deleted Story"
                stt = c.get('status', 'unknown')
                status_label = {
                    "approved": "PAID",
                    "waiting_screenshot": "PENDING",
                    "rejected": "REJECTED",
                    "pending_gateway": "PROCESSING",
                }.get(stt, stt.upper())
                mthd = c.get('method', 'unknown').upper()
                lines.append(f"<b>»</b> {sn}\n  <code>{mthd}</code>  ·  {utils.to_smallcap(status_label)}")
            history = "\n".join(lines) if lines else "  ɴᴏ ᴘᴀʏᴍᴇɴᴛ ʜɪꜱᴛᴏʀʏ ꜰᴏᴜɴᴅ"

            txt = (
                "<b>╔═⟦ 𝗣𝗥𝗢𝗙𝗜𝗟𝗘 ⟧═╗</b>\n\n"
                f"<b>⧉ ɴᴀᴍᴇ        ⟶</b> {name}\n"
                f"<b>⧉ ᴜꜱᴇʀɴᴀᴍᴇ    ⟶</b> {uname}\n"
                f"<b>⧉ ᴛɢ ɪᴅ       ⟶</b> <code>{uid}</code>\n\n"
                "<b>╠══════════════════╣</b>\n\n"
                f"<b>⧉ ᴘᴜʀᴄʜᴀꜱᴇꜱ   ⟶</b> {all_story_count}  (🤖 {len(purchases)} bot + 📱 {app_only_count} app)\n"
                f"<b>⧉ ʟᴀɴɢᴜᴀɢᴇ    ⟶</b> {lang_label}\n"
                f"<b>⧉ ᴊᴏɪɴᴇᴅ      ⟶</b> {joined}\n\n"
                "<b>╠══════════════════╣</b>\n\n"
                f"<b>⧉ ᴘᴀʏᴍᴇɴᴛ ʜɪꜱᴛᴏʀʏ</b>\n"
                f"{history}\n\n"
                "<b>╚══════════════════╝</b>"
            )
            kb = [
                [InlineKeyboardButton("📄 Export", callback_data=f"mk#usr_export_{uid}"),
                 InlineKeyboardButton("🧾 Payments", callback_data=f"mk#usr_pay_{uid}")],
                [InlineKeyboardButton("📩 Message User", callback_data=f"mk#usr_msg_{uid}")],
                [InlineKeyboardButton("🗑 Remove Story Access", callback_data=f"mk#usr_rmstory_{uid}")],
                [InlineKeyboardButton("💣 Complete Wipeout", callback_data=f"mk#usr_confirm_wipe_{uid}"),
                 InlineKeyboardButton("🚫 Ban User", callback_data=f"mk#usr_confirm_ban_{uid}")],
                [InlineKeyboardButton("« Back", callback_data="mk#users")],
            ]
            await query.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(kb))

        # ── Remove Story Access (select which story) ──────────────────────
        elif cmd.startswith("usr_rmstory_") and not cmd.startswith("usr_rmstory_confirm_"):
            uid = int(cmd.split("_")[2])
            user_doc = await db.db.users.find_one({"id": uid}) or {}
            purchases = [str(p) for p in user_doc.get("purchases", [])]
            # Also include mini app orders
            try:
                async for order in db.db.orders.find({"user_id": uid, "status": "paid"}, {"story_ids": 1}):
                    for s in order.get("story_ids", []):
                        if str(s) not in purchases:
                            purchases.append(str(s))
            except Exception:
                pass
            if not purchases:
                return await _safe_answer(query, "No purchases to remove.", show_alert=True)
            kb = []
            from bson.objectid import ObjectId
            for sid in purchases:
                try:
                    st = await db.db.premium_stories.find_one({"_id": ObjectId(sid)})
                    sname = st.get("story_name_en", sid)[:28] if st else sid[:28]
                except Exception:
                    sname = sid[:28]
                kb.append([InlineKeyboardButton(f"❌ {sname}", callback_data=f"mk#usr_rmstory_confirm_{uid}_{sid}")])
            kb.append([InlineKeyboardButton("« Back", callback_data=f"mk#usr_view_{uid}")])
            await _safe_answer(query)
            await query.message.edit_text(
                f"<b>🗑 Remove Story Access</b>\n\n"
                f"<b>User:</b> <code>{uid}</code>\n\n"
                f"Select the story to remove access from (user data remains intact):",
                reply_markup=InlineKeyboardMarkup(kb)
            )

        elif cmd.startswith("usr_rmstory_confirm_"):
            # format: usr_rmstory_confirm_{uid}_{story_id}
            parts = cmd.split("_")
            uid = int(parts[3])
            story_id = parts[4]
            from bson.objectid import ObjectId
            # Remove from user.purchases[]
            await db.db.users.update_one(
                {"id": uid},
                {"$pull": {"purchases": story_id}}
            )
            # Also remove from orders (mark story access revoked — keep order record)
            await db.db.orders.update_many(
                {"user_id": uid, "story_ids": story_id},
                {"$pull": {"story_ids": story_id}}
            )
            # Remove from checkouts for this story
            try:
                await db.db.premium_checkout.update_many(
                    {"user_id": uid, "story_id": ObjectId(story_id)},
                    {"$set": {"status": "access_revoked"}}
                )
            except Exception:
                pass
            await _safe_answer(query, "✅ Story access removed!", show_alert=True)
            query.data = f"mk#usr_view_{uid}"
            return await market_callback(client, query)

        # ── Complete Wipeout Confirm ──────────────────────────────────────
        elif cmd.startswith("usr_confirm_wipe_"):
            uid = int(cmd.split("_")[3])
            kb = [
                [InlineKeyboardButton("💣 YES — Complete Wipeout", callback_data=f"mk#usr_wipe_{uid}")],
                [InlineKeyboardButton("❌ Cancel", callback_data=f"mk#usr_view_{uid}")]
            ]
            await _safe_answer(query)
            await query.message.edit_text(
                f"<b>⚠️ COMPLETE WIPEOUT — User <code>{uid}</code></b>\n\n"
                "<blockquote expandable>"
                "This will permanently delete:\n"
                "• All purchases from user record\n"
                "• All orders from orders collection\n"
                "• All checkout/payment records\n"
                "• Entire user document from DB\n\n"
                "<b>User is NOT banned.</b> They can still use the bot but have no history.\n"
                "This action CANNOT be undone."
                "</blockquote>",
                reply_markup=InlineKeyboardMarkup(kb)
            )

        elif cmd.startswith("usr_wipe_"):
            uid = int(cmd.split("_")[2])
            await _safe_answer(query)
            # 1. Delete user doc
            await db.db.users.delete_one({"id": uid})
            # 2. Delete all orders
            await db.db.orders.delete_many({"user_id": {"$in": [uid, str(uid)]}})
            # 3. Delete all checkouts
            await db.db.premium_checkout.delete_many({"user_id": {"$in": [uid, str(uid)]}})
            # 4. Delete all feedback
            try:
                await db.db.premium_feedback.delete_many({"user_id": uid})
            except Exception:
                pass
            # 5. Delete all requests
            try:
                await db.db.premium_requests.delete_many({"user_id": uid})
            except Exception:
                pass
            await query.message.edit_text(
                f"<b>💣 Wipeout Complete</b>\n\n"
                f"All data for user <code>{uid}</code> has been permanently deleted.\n"
                f"User is NOT banned — they can still access the bot from scratch.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Buyers", callback_data="mk#users")]])
            )

        # ── Ban User Confirm ──────────────────────────────────────────────
        elif cmd.startswith("usr_confirm_ban_"):
            uid = int(cmd.split("_")[3])
            kb = [
                [InlineKeyboardButton("🚫 YES — Wipeout + Ban", callback_data=f"mk#usr_ban_{uid}")],
                [InlineKeyboardButton("❌ Cancel", callback_data=f"mk#usr_view_{uid}")]
            ]
            await _safe_answer(query)
            await query.message.edit_text(
                f"<b>🚫 BAN USER — <code>{uid}</code></b>\n\n"
                "<blockquote expandable>"
                "This will:\n"
                "• Delete all user data (same as Wipeout)\n"
                "• Add user to ban list in DB\n"
                "• Bot will block the user from accessing anything\n\n"
                "<b>This action CANNOT be undone.</b>"
                "</blockquote>",
                reply_markup=InlineKeyboardMarkup(kb)
            )

        elif cmd.startswith("usr_ban_"):
            uid = int(cmd.split("_")[2])
            await _safe_answer(query)
            # 1. Wipeout all data (same as wipe)
            await db.db.users.delete_one({"id": uid})
            await db.db.orders.delete_many({"user_id": uid})
            await db.db.premium_checkout.delete_many({"user_id": uid})
            try:
                await db.db.premium_feedback.delete_many({"user_id": uid})
            except Exception:
                pass
            try:
                await db.db.premium_requests.delete_many({"user_id": uid})
            except Exception:
                pass
            # 2. Add to ban list in DB
            await db.db.users.update_one(
                {"id": uid},
                {"$set": {"id": uid, "banned": True, "ban_reason": "Admin ban via dashboard"}},
                upsert=True
            )
            await query.message.edit_text(
                f"<b>🚫 User Banned + Wiped</b>\n\n"
                f"User <code>{uid}</code> has been banned and all data deleted.\n"
                f"They will be blocked if they try to use the bot.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Buyers", callback_data="mk#users")]])
            )

        # ── Message Buyer (single or all) ─────────────────────────────────
        elif cmd.startswith("usr_msg_"):
            uid_str = cmd.split("_")[2]
            await query.message.delete()
            await _safe_answer(query)
            if uid_str == "all":
                asyncio.create_task(_msg_all_buyers_flow(client, user_id))
            else:
                asyncio.create_task(_msg_single_buyer_flow(client, user_id, int(uid_str)))


        elif cmd.startswith("usr_pay_"):
            uid = int(cmd.split("_")[2])
            checkouts = await db.db.premium_checkout.find({"user_id": uid}).sort("_id", -1).to_list(length=30)
            kb = []
            for c in checkouts:
                sid = c.get("story_id")
                st = await db.db.premium_stories.find_one({"_id": sid})
                sn = st.get("story_name_en", "Unknown") if st else "Deleted"
                kb.append([InlineKeyboardButton(f"{sn} | {c.get('method','?')} | {c.get('status','?')}", callback_data=f"mk#usr_payv_{str(c['_id'])}")])
            kb.append([InlineKeyboardButton("« Back", callback_data=f"mk#usr_view_{uid}")])
            await query.message.edit_text(f"<b>🧾 Payment History for {uid}</b>", reply_markup=InlineKeyboardMarkup(kb))

        elif cmd.startswith("usr_payv_"):
            from bson.objectid import ObjectId
            pid = cmd.split("_")[2]
            c = await db.db.premium_checkout.find_one({"_id": ObjectId(pid)})
            if not c:
                return await _safe_answer(query, "Entry not found.", show_alert=True)
            st = await db.db.premium_stories.find_one({"_id": c.get("story_id")})
            sn = st.get("story_name_en", "Unknown") if st else "Deleted"
            txt = (
                f"<b>Payment Entry</b>\n\n"
                f"<b>User:</b> <code>{c.get('user_id')}</code>\n"
                f"<b>Story:</b> {sn}\n"
                f"<b>Method:</b> {c.get('method', 'unknown')}\n"
                f"<b>Status:</b> {c.get('status', 'unknown')}\n"
                f"<b>Created:</b> {c.get('created_at', 'N/A')}\n"
                f"<b>Paid:</b> {c.get('paid_at', 'N/A')}\n"
                f"<b>Approved:</b> {c.get('approved_at', 'N/A')}\n"
                f"<b>Reject Reason:</b> {c.get('reject_reason', 'N/A')}\n"
            )
            kb = [[InlineKeyboardButton("« Back", callback_data=f"mk#usr_pay_{c.get('user_id')}")]]
            await query.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(kb))
            if c.get("proof_file_id"):
                try:
                    await client.send_photo(user_id, c["proof_file_id"], caption=f"Manual proof • user {c.get('user_id')} • {sn}")
                except Exception:
                    pass

        elif cmd.startswith("usr_export_") or cmd == "usr_export_all":
            uid = None if cmd == "usr_export_all" else int(cmd.split("_")[2])
            payload = {}
            if uid is None:
                payload["users"] = await db.db.users.find({"purchases.0": {"$exists": True}}).to_list(length=500)
                payload["payments"] = await db.db.premium_checkout.find().sort("_id", -1).to_list(length=1000)
                back_cb = "mk#users"
                title = "premium_users_export_all.json"
            else:
                payload["user"] = await db.db.users.find_one({"id": uid})
                payload["payments"] = await db.db.premium_checkout.find({"user_id": uid}).sort("_id", -1).to_list(length=200)
                back_cb = f"mk#usr_view_{uid}"
                title = f"premium_user_{uid}_export.json"
            with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as fp:
                json.dump(payload, fp, default=str, indent=2, ensure_ascii=False)
                tmp = fp.name
            try:
                await client.send_document(user_id, tmp, file_name=title, caption="Export completed.")
            finally:
                try:
                    import os
                    os.remove(tmp)
                except Exception:
                    pass
            await query.message.edit_text("✅ Export sent.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data=back_cb)]]))

        elif cmd == "add_bot":
            await query.message.delete()
            asyncio.create_task(_add_store_bot_flow(client, user_id))

        elif cmd.startswith("bot_view_"):
            await _safe_answer(query)
            b_id = cmd.replace("bot_view_", "", 1) if cmd.startswith("bot_view_") else (data[2] if len(data) > 2 else cmd.split("_")[-1])
            bt = await _find_premium_bot(b_id)
            if not bt:
                logger.warning(f"[MGMT] bot_view failed: bot not found for b_id={b_id}")
                return await _safe_answer(query, "Bot not found!", show_alert=True)

            cfg = bt.get("config", {}) or {}
            ad_val = cfg.get("autodel", 0)
            if ad_val == 0:
                ad_state = "OFF"
            elif ad_val < 3600:
                ad_state = f"{ad_val // 60}M"
            elif ad_val == 86400:
                ad_state = "1D"
            else:
                ad_state = f"{ad_val // 3600}H"

            prot_state = "ON" if cfg.get("protect", False) else "OFF"
            upi_val = cfg.get("upi_enabled", None)
            if upi_val is True:
                upi_state = "FORCE ON"
            elif upi_val is False:
                upi_state = "OFF"
            else:
                upi_state = "Auto"

            try:
                b_id_int = int(bt.get("id") or b_id)
            except Exception:
                b_id_int = int(bt.get("id", 0))

            bot_mode = cfg.get("bot_mode", "full")
            if bot_mode == "show_store":
                mode_btn_text = "🎬 STORE: Show Store (OTT)"
                mode_desc = "🎬 <b>Show Store Mode</b> (Pay-Per-Show OTT, Auto-Indexer, 600×720 Showcase)"
            elif bot_mode == "full":
                mode_btn_text = "🟢 STORE: ON (Full)"
                mode_desc = "🟢 <b>Full Store Mode</b> (All bot menus active)"
            else:
                mode_btn_text = "🔴 STORE: OFF (Mini App)"
                mode_desc = "🔴 <b>Mini App Only Mode</b> (Store off, /mystories & delivery active)"

            story_count = 0
            total_bot_users = 0
            live_bot_users_24h = 0
            try:
                story_count = await db.db.premium_stories.count_documents({"bot_id": b_id_int})
            except Exception:
                pass

            log_ch_val = cfg.get("log_channel", None)
            log_ch_str = str(log_ch_val) if log_ch_val else "Default Global"

            ma_links_val = cfg.get("mini_app_deep_links", None)
            if ma_links_val is True:
                ma_links_state = "✅ ON"
            elif ma_links_val is False:
                ma_links_state = "❌ OFF"
            else:
                ma_links_state = "✅ ON (Default)"

            # ── Per-Bot Live & Total Users Analytics ──
            try:
                from datetime import datetime, timedelta, timezone
                now_utc = datetime.now(timezone.utc)
                cutoff_24h = now_utc - timedelta(hours=24)

                bot_stories = await db.db.premium_stories.find({"bot_id": b_id_int}, {"_id": 1, "story_id": 1}).to_list(length=3000)
                bot_s_ids = [str(s["_id"]) for s in bot_stories] + [s.get("story_id") for s in bot_stories if s.get("story_id")]

                # Total Lifetime Users who have used this specific bot
                total_bot_users = await db.db.users.count_documents({
                    "$or": [
                        {"used_bots": b_id_int},
                        {"bot_ids": b_id_int},
                        {f"bot_last_active.{b_id}": {"$exists": True}},
                        {"purchases": {"$in": bot_s_ids}} if bot_s_ids else {"_non_exist_": 1}
                    ]
                })

                # Currently Live / Active users in the last 24h on this bot
                live_bot_users_24h = await db.db.users.count_documents({
                    "$or": [
                        {f"bot_last_active.{b_id}": {"$gte": cutoff_24h}},
                        {"$and": [
                            {"$or": [{"used_bots": b_id_int}, {"bot_ids": b_id_int}, {"purchases": {"$in": bot_s_ids}} if bot_s_ids else {"_non_exist_": 1}]},
                            {"last_active": {"$gte": cutoff_24h}}
                        ]}
                    ]
                })
            except Exception:
                pass

            kb = [
                [InlineKeyboardButton(f"⚡ {mode_btn_text}", callback_data=f"mk#bot_mode_menu_{b_id}")],
            ]
            if bot_mode == "show_store":
                kb.append([InlineKeyboardButton("🔄 Auto Index", callback_data=f"mk#bot_show_idx_{b_id}")])

            ad_emoji = "5413643931139219521" if ad_val > 0 else "5413424119007978384"
            prot_emoji = "5413643931139219521" if cfg.get("protect", False) else "5413424119007978384"

            kb.extend([
                [_ikb("Logs Channel", callback_data=f"mk#bot_set_logch_{b_id}", icon_custom_emoji_id="6021454607513819417")],
                [_ikb("Detailed Live Stats", callback_data=f"mk#bot_stats_{b_id}", icon_custom_emoji_id="5936143551854285132")],
                [_ikb("Transfer to Another Bot", callback_data=f"mk#bot_migrate_menu_{b_id}", icon_custom_emoji_id="5807492110059838726")],
                [_ikb("Broadcast", callback_data=f"mk#bot_broadcast_{b_id}", icon_custom_emoji_id="6021418126061605425")],
                [_ikb("Welcome & About", callback_data=f"mk#p_wa_{b_id}", icon_custom_emoji_id="6041921818896372382")],
                [_ikb("Delivery Report MSG", callback_data=f"mk#pset_{b_id}_delivery_report", icon_custom_emoji_id="6023694913995020551")],
                [_ikb("Custom Caption", callback_data=f"mk#pset_{b_id}_caption", icon_custom_emoji_id="6019155812167981039")],
            ])

            if bot_mode != "show_store":
                kb.append([_ikb("Fetching Media (GIF/Img)", callback_data=f"mk#pset_{b_id}_fetching_media")])

            kb.extend([
                [
                    _ikb("Auto Delete", callback_data=f"mk#p_autodel_{b_id}", icon_custom_emoji_id=ad_emoji),
                    _ikb("Protection", callback_data=f"mk#p_protect_{b_id}", icon_custom_emoji_id=prot_emoji)
                ],
                [_ikb("UPI Config", callback_data=f"mk#p_upi_menu_{b_id}", icon_custom_emoji_id="6021637109264160908")],
            ])

            # Mini App Deep Links toggle — only in full/normal mode, not in show_store
            if bot_mode != "show_store":
                kb.append([InlineKeyboardButton(f"📱 Mini App Deep Links: {ma_links_state}", callback_data=f"mk#bot_toggle_malinks_{b_id}")])

            kb.extend([
                [_ikb("Remove Bot", callback_data=f"mk#bot_confirm_rm_{b_id}", icon_custom_emoji_id="6030400221232501136")],
                [InlineKeyboardButton("« Back", callback_data="mk#accounts")],
            ])

            # Mode emoji: green for show_store/full, red for miniapp/off
            if bot_mode == "show_store":
                mode_emoji = f'<emoji id="5413643931139219521">🟢</emoji>'
            elif bot_mode == "full":
                mode_emoji = f'<emoji id="5413643931139219521">🟢</emoji>'
            else:
                mode_emoji = f'<emoji id="5413424119007978384">🔴</emoji>'

            # Header: new emoji style shown in show_store, classic for others
            if bot_mode == "show_store":
                header = (
                    f'<emoji id="5296790785981718487">🔧</emoji> <b>Store Bot Profile & Stats</b>\n'
                    f'━━━━━━━━━━━━━━━━━━━━━\n\n'
                )
            else:
                header = (
                    f'<emoji id="5296790785981718487">🔧</emoji> <b>Store Bot Profile & Stats</b>\n'
                    f'━━━━━━━━━━━━━━━━━━━━━\n\n'
                )

            # Mini App Deep Links line — only show in non show_store mode
            ma_links_line = f'<emoji id="5312536423156654273">📱</emoji> <b>Mini App Deep Links:</b> <code>{ma_links_state}</code>\n' if bot_mode != "show_store" else ""

            view_text = (
                header
                + f'<emoji id="6030400221232501136">✏️</emoji> <b>Name:</b> {bt.get("name")}\n'
                + f'<emoji id="6021683099773966917">👤</emoji> <b>Username:-</b> @{bt.get("username")}\n'
                + f'<emoji id="5296786460949654303">🆔</emoji> <b>ID:-</b> <code>{bt.get("id")}</code>\n'
                + f'<emoji id="5235588635885054955">⚙️</emoji> <b>Mode:-</b> {mode_emoji} {mode_desc}\n'
                + f'<emoji id="6021745995275048956">📚</emoji> <b>Assigned Stories:-</b> <code>{story_count}</code>\n\n'
                + f'<emoji id="5936143551854285132">📊</emoji> <b>Bot User Metrics:</b>\n'
                + f'━━━━━━━━━━━━━━━━━━━━━\n\n'
                + f'<emoji id="5809949600152296075">🟢</emoji> <b>Live Users ( 24H ) :-</b> <code>{live_bot_users_24h}</code>\n'
                + f'<emoji id="6021690418398239007">👥</emoji> <b>Total Users:-</b> <code>{total_bot_users}</code>\n'
                + ma_links_line
                + f'<emoji id="6021435576513730578">📋</emoji> <b>Event Log Channel:</b> <code>{log_ch_str}</code>'
            )
            await _edit_or_send_mgmt_view(client, query.message.chat.id, view_text, InlineKeyboardMarkup(kb), query=query)


        elif cmd.startswith("bot_stats_"):
            b_id = cmd.split("_")[2]
            b_id_int = int(b_id)
            bt = await _find_premium_bot(b_id_int)
            if not bt:
                return await _safe_answer(query, "Bot not found!", show_alert=True)

            from datetime import datetime, timedelta, timezone
            now_utc = datetime.now(timezone.utc)
            cutoff_24h = now_utc - timedelta(hours=24)
            cutoff_7d = now_utc - timedelta(days=7)

            bot_stories = await db.db.premium_stories.find({"bot_id": b_id_int}, {"_id": 1, "story_id": 1, "price": 1}).to_list(length=3000)
            bot_s_ids = [str(s["_id"]) for s in bot_stories] + [s.get("story_id") for s in bot_stories if s.get("story_id")]

            total_users = await db.db.users.count_documents({
                "$or": [
                    {"used_bots": b_id_int},
                    {"bot_ids": b_id_int},
                    {f"bot_last_active.{b_id}": {"$exists": True}},
                    {"purchases": {"$in": bot_s_ids}} if bot_s_ids else {"_non_exist_": 1}
                ]
            })

            live_24h = await db.db.users.count_documents({
                "$or": [
                    {f"bot_last_active.{b_id}": {"$gte": cutoff_24h}},
                    {"$and": [
                        {"$or": [{"used_bots": b_id_int}, {"bot_ids": b_id_int}, {"purchases": {"$in": bot_s_ids}} if bot_s_ids else {"_non_exist_": 1}]},
                        {"last_active": {"$gte": cutoff_24h}}
                    ]}
                ]
            })

            active_7d = await db.db.users.count_documents({
                "$or": [
                    {f"bot_last_active.{b_id}": {"$gte": cutoff_7d}},
                    {"$and": [
                        {"$or": [{"used_bots": b_id_int}, {"bot_ids": b_id_int}, {"purchases": {"$in": bot_s_ids}} if bot_s_ids else {"_non_exist_": 1}]},
                        {"last_active": {"$gte": cutoff_7d}}
                    ]}
                ]
            })

            # Orders & Revenue on this bot
            paid_orders = await db.db.orders.find({
                "$or": [
                    {"bot_id": b_id_int},
                    {"story_id": {"$in": bot_s_ids}} if bot_s_ids else {"_non_exist_": 1}
                ],
                "status": {"$in": ["paid", "approved", "completed", "delivered"]}
            }).to_list(length=5000)

            total_revenue = sum(float(o.get("amount") or o.get("price") or 0) for o in paid_orders)
            unique_buyers = len(set(o.get("user_id") for o in paid_orders if o.get("user_id")))

            kb = [
                [InlineKeyboardButton("🔄 Refresh Stats", callback_data=f"mk#bot_stats_{b_id}")],
                [InlineKeyboardButton("📢 Broadcast to This Bot's Users", callback_data=f"mk#bot_broadcast_{b_id}")],
                [InlineKeyboardButton("« Back to Bot Profile", callback_data=f"mk#bot_view_{b_id}")]
            ]

            await query.message.edit_text(
                f"<b>📊 LIVE BOT ANALYTICS & STATS</b>\n\n"
                f"<b>🤖 Bot:</b> {bt.get('name')} (@{bt.get('username')})\n"
                f"<b>🆔 Bot ID:</b> <code>{bt.get('id')}</code>\n\n"
                f"<b>👥 USER ENGAGEMENT:</b>\n"
                f"• <b>🟢 Currently Live / Active (24h):</b> <code>{live_24h}</code> users\n"
                f"• <b>📅 Active This Week (7 Days):</b> <code>{active_7d}</code> users\n"
                f"• <b>🌐 Total Lifetime Users Used:</b> <code>{total_users}</code> users\n\n"
                f"<b>💰 SALES & ORDERS:</b>\n"
                f"• <b>🛒 Total Paid Orders:</b> <code>{len(paid_orders)}</code>\n"
                f"• <b>👤 Unique Buyers:</b> <code>{unique_buyers}</code>\n"
                f"• <b>💵 Total Revenue Generated:</b> <code>₹{total_revenue:,.2f}</code>\n"
                f"• <b>📚 Stories Assigned:</b> <code>{len(bot_stories)}</code>\n\n"
                f"<i>Updated at: {now_utc.strftime('%d %b %Y, %H:%M:%S UTC')}</i>",
                reply_markup=InlineKeyboardMarkup(kb)
            )

        elif cmd.startswith("bot_toggle_malinks_"):
            b_id = cmd.split("_")[3]
            b_id_int = int(b_id)
            bt = await _find_premium_bot(b_id_int)
            if not bt: return await _safe_answer(query, "Bot not found!", show_alert=True)
            cfg = bt.get("config", {}) or {}
            curr_val = cfg.get("mini_app_deep_links", True)
            new_val = not curr_val
            await db.db.premium_bots.update_one({"id": b_id_int}, {"$set": {"config.mini_app_deep_links": new_val}})
            status = "✅ ON" if new_val else "❌ OFF"
            await _safe_answer(query, f"Mini App Deep Links for @{bt.get('username')}: {status}", show_alert=True)
            query.data = f"mk#bot_view_{b_id}"
            return await market_callback(client, query)

        elif cmd.startswith("bot_set_logch_"):
            b_id = cmd.split("_")[3]
            await query.message.delete()
            asyncio.create_task(_bot_set_logch_flow(client, user_id, b_id))

        elif cmd.startswith("bot_mode_menu_"):
            b_id = cmd.split("_")[3]
            bt = await _find_premium_bot(b_id)
            if not bt: return await _safe_answer(query, "Bot not found!")
            cfg = bt.get("config", {}) or {}
            curr_mode = cfg.get("bot_mode", "full")
            story_count = await db.db.premium_stories.count_documents({"bot_id": int(b_id)})

            kb = [
                [InlineKeyboardButton("🟢 Full Store Mode (Normal)", callback_data=f"mk#bot_set_mode_{b_id}_full")],
                [InlineKeyboardButton("🎬 Show Store Mode (Pay-Per-Show OTT)", callback_data=f"mk#bot_set_mode_{b_id}_show_store")],
                [InlineKeyboardButton("🔴 Turn OFF Store (Mini App Only)", callback_data=f"mk#bot_mode_confirm_off_{b_id}")],
                [InlineKeyboardButton("« " + utils.to_smallcap("Back"), callback_data=f"mk#bot_view_{b_id}")],
            ]
            if curr_mode == "show_store":
                curr_label = "🎬 SHOW STORE (PAY-PER-SHOW OTT)"
            elif curr_mode == "full":
                curr_label = "🟢 FULL STORE (ACTIVE)"
            else:
                curr_label = "🔴 MINI APP ONLY (LITE / STORE OFF)"

            await query.message.edit_text(
                f"<b>⚡ BOT MODE SETTINGS (@{bt.get('username')})</b>\n\n"
                f"<b>Current Mode:</b> <code>{curr_label}</code>\n"
                f"<b>Assigned Stories:</b> <code>{story_count}</code>\n\n"
                "<b>Description of Modes:</b>\n"
                "• <b>🟢 Full Store Mode (Normal):</b>\n"
                "  Standard Telegram bot store with category browsing, inline search, language selection.\n\n"
                "• <b>🎬 Show Store Mode (Pay-Per-Show OTT):</b>\n"
                "  Auto-Indexer enabled, single combined episode shows, 600×720 showcase posts with deep links, My Shows library.\n\n"
                "• <b>🔴 Mini App Only (Store OFF):</b>\n"
                "  Disables bot store browsing. Users receive Mini App welcome poster & button.\n"
                "  <i>⚠️ <b>Delivery and purchases remain 100% active!</b></i>",
                reply_markup=InlineKeyboardMarkup(kb)
            )

        elif cmd.startswith("bot_mode_confirm_off_") or cmd.startswith("bot_migrate_menu_"):
            b_id = cmd.split("_")[4] if cmd.startswith("bot_mode_confirm_off_") else cmd.split("_")[3]
            bt = await _find_premium_bot(b_id)
            if not bt: return await _safe_answer(query, "Bot not found!")
            
            story_count = await db.db.premium_stories.count_documents({"bot_id": int(b_id)})
            other_bots = await db.db.premium_bots.find({"id": {"$ne": int(b_id)}}).to_list(length=10)

            kb = []
            if other_bots:
                for ob in other_bots:
                    kb.append([InlineKeyboardButton(f"🔄 Reassign {story_count} Stories to @{ob.get('username')}", callback_data=f"mk#bot_migrate_{b_id}_{ob['id']}")])
            
            kb.append([InlineKeyboardButton("⚡ Keep Stories on This Bot & Turn OFF Store", callback_data=f"mk#bot_set_mode_{b_id}_miniapp")])
            kb.append([InlineKeyboardButton("« Cancel", callback_data=f"mk#bot_view_{b_id}")])

            await query.message.edit_text(
                f"<b>🔄 CONNECTED BOT & STORIES MANAGEMENT</b>\n\n"
                f"<b>Bot:</b> @{bt.get('username')}\n"
                f"<b>Currently Assigned Stories:</b> <code>{story_count}</code>\n\n"
                "Aap is bot ki stories ko kisi doosre connected delivery bot me transfer karna chahte hain ya isi bot par rakh kar store OFF karna chahte hain?\n\n"
                "<i>💡 Kisi doosre bot ko select karne par sabhi stories us bot me shift ho jayengi aur user us naye bot se normal delivery bot ki tarah buy/browse/delivery kar sakega.</i>",
                reply_markup=InlineKeyboardMarkup(kb)
            )

        elif cmd.startswith("bot_migrate_"):
            parts = cmd.split("_")
            from_b_id = int(parts[2])
            to_b_id = int(parts[3])
            
            from_bt = await _find_premium_bot(from_b_id)
            to_bt = await _find_premium_bot(to_b_id)
            
            res = await db.db.premium_stories.update_many({"bot_id": from_b_id}, {"$set": {"bot_id": to_b_id}})
            # Set source bot to miniapp mode and target bot to full mode
            await db.db.premium_bots.update_one({"id": from_b_id}, {"$set": {"config.bot_mode": "miniapp"}})
            await db.db.premium_bots.update_one({"id": to_b_id}, {"$set": {"config.bot_mode": "full"}})
            
            from_un = from_bt.get("username", str(from_b_id)) if from_bt else str(from_b_id)
            to_un = to_bt.get("username", str(to_b_id)) if to_bt else str(to_b_id)
            
            await _safe_answer(query, f"✅ {res.modified_count} stories transferred to @{to_un}! @{from_un} is in Mini App Mode.", show_alert=True)
            query.data = f"mk#bot_view_{from_b_id}"
            return await market_callback(client, query)

        elif cmd.startswith("bot_set_mode_"):
            parts = cmd.split("_")
            b_id = int(parts[3])
            target_mode = parts[4]
            if len(parts) > 5:
                target_mode = f"{parts[4]}_{parts[5]}" # show_store
            
            bt = await _find_premium_bot(b_id)
            if not bt: return await _safe_answer(query, "Bot not found!")
            
            await db.db.premium_bots.update_one({"id": b_id}, {"$set": {"config.bot_mode": target_mode}})
            if target_mode == "show_store":
                label = "🎬 Show Store Mode (Pay-Per-Show OTT)"
            elif target_mode == "miniapp":
                label = "🔴 Mini App Only (Lite / Store OFF)"
            else:
                label = "🟢 Full Store Mode"
            await _safe_answer(query, f"✅ Bot Mode set to: {label}", show_alert=True)
            query.data = f"mk#bot_view_{b_id}"
            return await market_callback(client, query)

        elif cmd.startswith("bot_show_idx_"):
            b_id = cmd.split("bot_show_idx_")[1]
            bt = await _find_premium_bot(b_id)
            if not bt: return await _safe_answer(query, "Bot not found!")
            cfg = bt.get("config", {}) or {}
            
            src_ch = cfg.get("db_channel_id", "Not Configured")
            dst_ch = cfg.get("showcase_channel_id", "Not Configured")
            plat = cfg.get("platform_name", "Story TV")
            def_price = cfg.get("default_price", 19)
            total_shows = await db.db.premium_stories.count_documents({"bot_id": int(b_id)})

            kb = [
                [
                    InlineKeyboardButton(f"📁 Source DB: {src_ch}", callback_data=f"mk#bot_show_src_{b_id}"),
                    InlineKeyboardButton(f"📢 Showcase: {dst_ch}", callback_data=f"mk#bot_show_dst_{b_id}")
                ],
                [
                    InlineKeyboardButton(f"🖥 Platform: {plat}", callback_data=f"mk#bot_show_plat_{b_id}"),
                    InlineKeyboardButton(f"💰 Price: ₹{def_price}", callback_data=f"mk#bot_show_price_{b_id}")
                ],
                [InlineKeyboardButton("🚀 Scan & Index Database Channel (800+ Shows)", callback_data=f"mk#bot_show_scan_{b_id}")],
                [InlineKeyboardButton("📤 Publish Shows to Public Showcase Channel", callback_data=f"mk#bot_show_pub_{b_id}")],
                [InlineKeyboardButton("« " + utils.to_smallcap("Back"), callback_data=f"mk#bot_view_{b_id}")]
            ]

            await query.message.edit_text(
                f"<b>🔄 AUTO-INDEXER & SHOWCASE PUBLISHER (@{bt.get('username')})</b>\n\n"
                f"• <b>Total Catalog Shows:</b> <code>{total_shows}</code>\n"
                f"• <b>Source DB Channel:</b> <code>{src_ch}</code>\n"
                f"• <b>Showcase Channel:</b> <code>{dst_ch}</code>\n"
                f"• <b>Default Platform:</b> <code>{plat}</code>\n"
                f"• <b>Default Price:</b> <code>₹{def_price}</code>\n\n"
                "<i>Database channel ke poster aur video files ko auto-scan karke catalog index karein aur public showcase channel me 600×720 enhanced posters publish karein.</i>",
                reply_markup=InlineKeyboardMarkup(kb)
            )

        elif cmd.startswith("bot_show_src_"):
            b_id = cmd.split("bot_show_src_")[1]
            await query.message.delete()
            ask = await client.send_message(
                user_id,
                "<b>📁 Set Source Database Channel</b>\n\n"
                "Enter the Database Channel ID (e.g. <code>-1001234567890</code>) where your shows & videos are stored:\n\n"
                "Send /cancel to abort."
            )
            try:
                from utils import ask_user
                resp = await ask_user(client, user_id, timeout=60)
                if getattr(resp, 'text', None) and '/cancel' in resp.text:
                    await resp.delete()
                    return await ask.edit_text("<i>Cancelled.</i>", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"mk#bot_show_idx_{b_id}")]]))
                ch_id_val = int((resp.text or '').strip())
                await db.db.premium_bots.update_one({"id": int(b_id)}, {"$set": {"config.db_channel_id": ch_id_val}})
                await resp.delete()
                await ask.edit_text(f"✅ Source Database Channel set to <code>{ch_id_val}</code>.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"mk#bot_show_idx_{b_id}")]]))
            except Exception as e:
                await ask.edit_text(f"❌ Error: {e}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Retry", callback_data=f"mk#bot_show_src_{b_id}")]]))

        elif cmd.startswith("bot_show_dst_"):
            b_id = cmd.split("bot_show_dst_")[1]
            await query.message.delete()
            ask = await client.send_message(
                user_id,
                "<b>📢 Set Public Showcase Destination Channel</b>\n\n"
                "Enter the Public Channel ID (e.g. <code>-1009876543210</code>) where posters with [Buy Now] buttons will be posted:\n\n"
                "Send /cancel to abort."
            )
            try:
                from utils import ask_user
                resp = await ask_user(client, user_id, timeout=60)
                if getattr(resp, 'text', None) and '/cancel' in resp.text:
                    await resp.delete()
                    return await ask.edit_text("<i>Cancelled.</i>", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"mk#bot_show_idx_{b_id}")]]))
                ch_id_val = int((resp.text or '').strip())
                await db.db.premium_bots.update_one({"id": int(b_id)}, {"$set": {"config.showcase_channel_id": ch_id_val}})
                await resp.delete()
                await ask.edit_text(f"✅ Public Showcase Channel set to <code>{ch_id_val}</code>.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"mk#bot_show_idx_{b_id}")]]))
            except Exception as e:
                await ask.edit_text(f"❌ Error: {e}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Retry", callback_data=f"mk#bot_show_dst_{b_id}")]]))

        elif cmd.startswith("bot_show_plat_"):
            b_id = cmd.split("bot_show_plat_")[1]
            await query.message.delete()
            ask = await client.send_message(
                user_id,
                "<b>🖥 Set Default Platform Name</b>\n\n"
                "Enter the platform name for this show store (e.g. <code>Story TV</code> or <code>Pocket FM</code>):\n\n"
                "Send /cancel to abort."
            )
            try:
                from utils import ask_user
                resp = await ask_user(client, user_id, timeout=60)
                if getattr(resp, 'text', None) and '/cancel' in resp.text:
                    await resp.delete()
                    return await ask.edit_text("<i>Cancelled.</i>", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"mk#bot_show_idx_{b_id}")]]))
                plat_val = (resp.text or '').strip()
                await db.db.premium_bots.update_one({"id": int(b_id)}, {"$set": {"config.platform_name": plat_val}})
                await resp.delete()
                await ask.edit_text(f"✅ Default Platform set to <b>{plat_val}</b>.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"mk#bot_show_idx_{b_id}")]]))
            except Exception as e:
                await ask.edit_text(f"❌ Error: {e}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Retry", callback_data=f"mk#bot_show_plat_{b_id}")]]))

        elif cmd.startswith("bot_show_price_"):
            b_id = cmd.split("bot_show_price_")[1]
            await query.message.delete()
            ask = await client.send_message(
                user_id,
                "<b>💰 Set Default Show Price (₹)</b>\n\n"
                "Enter the default price for newly indexed shows (in INR):\n"
                "<b>Example:</b> <code>19</code> or <code>29</code>\n\n"
                "Send /cancel to abort."
            )
            try:
                from utils import ask_user
                resp = await ask_user(client, user_id, timeout=60)
                if getattr(resp, 'text', None) and '/cancel' in resp.text:
                    await resp.delete()
                    return await ask.edit_text("<i>Cancelled.</i>", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"mk#bot_show_idx_{b_id}")]]))
                price_val = int((resp.text or '').strip())
                if price_val < 1: raise ValueError('Invalid price')
                await db.db.premium_bots.update_one({"id": int(b_id)}, {"$set": {"config.default_price": price_val}})
                await resp.delete()
                await ask.edit_text(f"✅ Default Show Price set to <b>₹{price_val}</b>.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"mk#bot_show_idx_{b_id}")]]))
            except Exception as e:
                await ask.edit_text(f"❌ Error: {e}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Retry", callback_data=f"mk#bot_show_price_{b_id}")]]))

        elif cmd.startswith("bot_show_scan_"):
            b_id = cmd.split("bot_show_scan_")[1]
            bt = await _find_premium_bot(b_id)
            cfg = bt.get("config", {}) or {} if bt else {}
            src_ch = cfg.get("db_channel_id")
            if not src_ch:
                return await _safe_answer(query, "⚠️ Please set Source Database Channel first!", show_alert=True)

            await query.answer("🚀 Starting Auto-Scanner...", show_alert=False)
            status_msg = await query.message.edit_text(
                "<b>⏳ Scanning Database Channel...</b>\n\n"
                "• <i>Reading messages sequentially...</i>\n"
                "• <i>Pairing posters & video files...</i>\n"
                "• <i>Deduplicating titles...</i>"
            )

            try:
                from AryaPremium.plugins.mgmt.store_indexer import scan_and_index_channel
            except ImportError:
                from plugins.mgmt.store_indexer import scan_and_index_channel

            async def _prog_cb(indexed, dups):
                try:
                    await status_msg.edit_text(
                        f"<b>⏳ Scanning in Progress...</b>\n\n"
                        f"• <b>Indexed Shows:</b> <code>{indexed}</code>\n"
                        f"• <b>Duplicates Skipped:</b> <code>{dups}</code>"
                    )
                except Exception: pass

            res = await scan_and_index_channel(
                client=client,
                channel_id=src_ch,
                bot_id=int(b_id),
                default_price=cfg.get("default_price", 19),
                default_platform=cfg.get("platform_name", "Story TV"),
                progress_callback=_prog_cb
            )

            await status_msg.edit_text(
                f"<b>✅ Scan & Indexing Completed!</b>\n\n"
                f"• <b>Total Newly Indexed:</b> <code>{res['indexed']}</code>\n"
                f"• <b>Duplicates Skipped:</b> <code>{res['duplicates']}</code>\n"
                f"• <b>Database Channel:</b> <code>{src_ch}</code>\n\n"
                f"<i>Ab aap 'Publish to Public Showcase Channel' button se in sabhi shows ko public channel me post kar sakte hain!</i>",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📤 Publish to Public Showcase Channel", callback_data=f"mk#bot_show_pub_{b_id}")],
                    [InlineKeyboardButton("Back", callback_data=f"mk#bot_show_idx_{b_id}")]
                ])
            )

        elif cmd.startswith("bot_show_pub_"):
            b_id = cmd.split("bot_show_pub_")[1]
            bt = await _find_premium_bot(b_id)
            cfg = bt.get("config", {}) or {} if bt else {}
            dst_ch = cfg.get("showcase_channel_id")
            if not dst_ch:
                return await _safe_answer(query, "⚠️ Please set Public Showcase Channel first!", show_alert=True)

            b_uname = bt.get("username", "StoreBot") if bt else "StoreBot"
            await query.answer("📤 Publishing shows to Showcase Channel...", show_alert=False)
            status_msg = await query.message.edit_text("<b>⏳ Publishing 600×720 Showcase Posters...</b>\n\n<i>Processing shows...</i>")

            try:
                from AryaPremium.plugins.mgmt.store_indexer import publish_show_to_showcase
            except ImportError:
                from plugins.mgmt.store_indexer import publish_show_to_showcase

            shows = await db.db.premium_stories.find({"bot_id": int(b_id), "is_show": True}).to_list(length=1000)
            pub_count = 0

            for sh in shows:
                try:
                    ok = await publish_show_to_showcase(
                        client=client,
                        target_channel_id=dst_ch,
                        show=sh,
                        store_bot_username=b_uname,
                        tutorial_link=cfg.get("tutorial_url", "https://t.me/UseAryaBot")
                    )
                    if ok: pub_count += 1
                    if pub_count % 10 == 0:
                        try: await status_msg.edit_text(f"<b>⏳ Publishing in Progress...</b>\n\n• <b>Published:</b> <code>{pub_count} / {len(shows)}</code>")
                        except Exception: pass
                    await asyncio.sleep(1.5)
                except Exception as e:
                    logger.error(f"Failed publishing show {sh.get('title')}: {e}")

            await status_msg.edit_text(
                f"<b>🎉 Publishing Complete!</b>\n\n"
                f"• <b>Total Shows Published:</b> <code>{pub_count}</code>\n"
                f"• <b>Destination Channel:</b> <code>{dst_ch}</code>\n"
                f"• <b>Deep-Link Bot:</b> @{b_uname}\n\n"
                f"<i>Har post me 600×720 enhanced poster, bilingual expandable note, aur [🛍️ Buy Now] deep link buttons add ho chuke hain!</i>",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"mk#bot_show_idx_{b_id}")]])
            )
            query.data = f"mk#bot_view_{b_id}"
            return await market_callback(client, query)

        elif cmd.startswith("bot_broadcast_"):
            b_id = cmd.split("_", 2)[2]
            asyncio.create_task(_bot_broadcast_flow(client, user_id, b_id))
            if "query" in locals() and query:
                return await query.answer()

        elif cmd.startswith("bot_confirm_rm_"):
            b_id = data[2] if len(data) > 2 else cmd.split("_")[3]
            kb = [
                [InlineKeyboardButton("🚫 " + utils.to_smallcap("Yes, Remove It"), callback_data=f"mk#bot_rm_{b_id}")],
                [InlineKeyboardButton(utils.to_smallcap("Cancel"), callback_data=f"mk#bot_view_{b_id}")]
            ]
            await query.message.edit_text(
                "<b>⚠️ CRITICAL WARNING</b>\n\n"
                "Are you sure you want to completely remove this bot from your Marketplace?\n"
                "<i>This action will disconnect the bot and stop all incoming buyer processing.</i>",
                reply_markup=InlineKeyboardMarkup(kb)
            )

        elif cmd.startswith("p_autodel_"):
            parts = cmd.split("_")
            b_id = parts[2]
            
            if len(parts) == 3:
                bt = await _find_premium_bot(b_id)
                if not bt: return await _safe_answer(query, "Not found!")
                cfg = bt.get("config", {}) or {}
                ad_val = cfg.get("autodel", 0)
                
                curr_str = "OFF"
                if ad_val > 0:
                    if ad_val < 3600: curr_str = f"{ad_val // 60}M"
                    elif ad_val == 86400: curr_str = "1D"
                    else: curr_str = f"{ad_val // 3600}H"

                kb = [
                   [InlineKeyboardButton("OFF", callback_data=f"mk#p_autodel_{b_id}_0"),
                    InlineKeyboardButton("5M", callback_data=f"mk#p_autodel_{b_id}_300"),
                    InlineKeyboardButton("15M", callback_data=f"mk#p_autodel_{b_id}_900")],
                   [InlineKeyboardButton("30M", callback_data=f"mk#p_autodel_{b_id}_1800"),
                    InlineKeyboardButton("1H", callback_data=f"mk#p_autodel_{b_id}_3600"),
                    InlineKeyboardButton("3H", callback_data=f"mk#p_autodel_{b_id}_10800")],
                   [InlineKeyboardButton("6H", callback_data=f"mk#p_autodel_{b_id}_21600"),
                    InlineKeyboardButton("9H", callback_data=f"mk#p_autodel_{b_id}_32400"),
                    InlineKeyboardButton("12H", callback_data=f"mk#p_autodel_{b_id}_43200")],
                   [InlineKeyboardButton("15H", callback_data=f"mk#p_autodel_{b_id}_54000"),
                    InlineKeyboardButton("18H", callback_data=f"mk#p_autodel_{b_id}_64800"),
                    InlineKeyboardButton("21H", callback_data=f"mk#p_autodel_{b_id}_75600")],
                   [InlineKeyboardButton("24H", callback_data=f"mk#p_autodel_{b_id}_86400"),
                    InlineKeyboardButton("1D", callback_data=f"mk#p_autodel_{b_id}_86400")],
                   [InlineKeyboardButton("« Back", callback_data=f"mk#bot_view_{b_id}")]
                ]
                autodel_txt = (
                    f'<emoji id="5413643931139219521">⏳</emoji> <b>Auto Delete Configuration</b>\n'
                    f'━━━━━━━━━━━━━━━━━━━━━\n\n'
                    f"<b>Current setting:</b> <code>{curr_str}</code>\n\n"
                    f"Select the time after which delivered files should be automatically deleted from the user's DM:"
                )
                return await _edit_or_send_mgmt_view(client, query.message.chat.id, autodel_txt, InlineKeyboardMarkup(kb), query=query)
            else:
                new_val = int(parts[3])
                bt = await _find_premium_bot(b_id)
                if not bt: return await _safe_answer(query, "Not found!")
                cfg = bt.get("config", {}) or {}
                cfg["autodel"] = new_val
                await db.db.premium_bots.update_one({"id": int(b_id)}, {"$set": {"config": cfg}})
                await _safe_answer(query, f"Auto-Delete updated!", show_alert=True)
                query.data = f"mk#bot_view_{b_id}"
                return await market_callback(client, query)

        elif cmd.startswith("p_protect_"):
            b_id = cmd.split("_")[2]
            bt = await _find_premium_bot(b_id)
            if not bt: return await _safe_answer(query, "Not found!")
            cfg = bt.get("config", {}) or {}
            curr = cfg.get("protect", False)
            cfg["protect"] = not curr
            await db.db.premium_bots.update_one({"id": int(b_id)}, {"$set": {"config": cfg}})
            await _safe_answer(query, f"Content Protection set to {'ON' if cfg['protect'] else 'OFF'}", show_alert=True)
            query.data = f"mk#bot_view_{b_id}"
            return await market_callback(client, query)

        elif cmd.startswith("p_upi_menu_"):
            b_id = cmd.replace("p_upi_menu_", "", 1)
            bt = await _find_premium_bot(b_id)
            if not bt: return await _safe_answer(query, "Bot not found!")
            cfg = bt.get("config", {}) or {}
            upi_val = cfg.get("upi_enabled", None)
            if upi_val is True:
                upi_state = "🟢 FORCE ON"
                tgl_emoji = "5413643931139219521"
            elif upi_val is False:
                upi_state = "🔴 OFF"
                tgl_emoji = "5413424119007978384"
            else:
                upi_state = "🟡 Auto (Schedule Active)"
                tgl_emoji = "5413643931139219521"

            upi_name = cfg.get("upi_name") or "Not Set (Default)"
            upi_redirect = cfg.get("upi_redirect") or "Not Set (Default)"
            logo_set = "✅ Custom Logo Configured" if cfg.get("logo") else "❌ Default Logo"

            kb = [
                [_ikb(f"UPI Status: {upi_state}", callback_data=f"mk#p_upi_toggle_{b_id}", icon_custom_emoji_id=tgl_emoji)],
                [_ikb("UPI Payee Name", callback_data=f"mk#pset_{b_id}_upi_name", icon_custom_emoji_id="6030400221232501136"),
                 _ikb("Open-App Link", callback_data=f"mk#pset_{b_id}_upi_redirect", icon_custom_emoji_id="5312536423156654273")],
                [_ikb("Bot Logo (UPI QR)", callback_data=f"mk#pset_{b_id}_logo", icon_custom_emoji_id="6026089641730382702")],
                [InlineKeyboardButton("« Back", callback_data=f"mk#bot_view_{b_id}")],
            ]

            upi_txt = (
                f'<emoji id="6021637109264160908">🪙</emoji> <b>UPI Configuration</b>\n'
                f'━━━━━━━━━━━━━━━━━━━━━\n\n'
                f'<b>» Bot:</b> @{bt.get("username")}\n'
                f'<b>» UPI Status:</b> <code>{upi_state}</code>\n'
                f'<b>» Payee Name:</b> <code>{upi_name}</code>\n'
                f'<b>» Open-App Link:</b> <code>{upi_redirect}</code>\n'
                f'<b>» Bot Logo (QR):</b> <code>{logo_set}</code>\n\n'
                f'<i>Manage direct UPI payment settings, payee details, and enable/disable UPI below:</i>'
            )
            await _edit_or_send_mgmt_view(client, query.message.chat.id, upi_txt, InlineKeyboardMarkup(kb), query=query)

        elif cmd.startswith("p_upi_toggle_"):
            b_id = cmd.replace("p_upi_toggle_", "", 1)
            bt = await _find_premium_bot(b_id)
            if not bt: return await _safe_answer(query, "Not found!")
            cfg = bt.get("config", {}) or {}
            curr = cfg.get("upi_enabled", None)

            # Cycle: Auto -> OFF -> Force ON -> Auto
            if curr is None:
                cfg["upi_enabled"] = False
                new_label = "OFF (Disabled)"
            elif curr is False:
                cfg["upi_enabled"] = True
                new_label = "FORCE ON"
            else:
                cfg.pop("upi_enabled", None)
                cfg["upi_enabled"] = None
                new_label = "Auto (Schedule Active)"

            await db.db.premium_bots.update_one({"id": int(b_id)}, {"$set": {"config": cfg}})
            await _safe_answer(query, f"UPI set to: {new_label}", show_alert=True)
            query.data = f"mk#p_upi_menu_{b_id}"
            return await market_callback(client, query)

        elif cmd.startswith("p_upi_"):
            b_id = cmd.split("_")[2]
            query.data = f"mk#p_upi_menu_{b_id}"
            return await market_callback(client, query)

        elif cmd.startswith("bot_rm_"):
            b_id = int(cmd.split("_")[2])
            await db.db.premium_bots.delete_one({"id": b_id})
            await _safe_answer(query, "Bot Removed!", show_alert=True)
            from plugins.userbot.market_seller import market_clients
            if str(b_id) in market_clients:
                try:
                    await market_clients[str(b_id)].stop()
                    del market_clients[str(b_id)]
                except Exception:
                    pass
            query.data = "mk#accounts"
            return await market_callback(client, query)

        elif cmd == "manage_stories" or cmd.startswith("ms_list_"):
            await _safe_answer(query)
            page = 0
            if cmd.startswith("ms_list_"):
                try: page = int(cmd.replace("ms_list_", ""))
                except: page = 0
            items_pp = 8
            total = await db.db.premium_stories.count_documents({})
            total_pages = max(1, (total + items_pp - 1) // items_pp)
            if page >= total_pages: page = total_pages - 1
            if page < 0: page = 0
            stories = await db.db.premium_stories.find().sort("story_name_en", 1).skip(page * items_pp).limit(items_pp).to_list(length=items_pp)
            kb = []
            start_num = page * items_pp + 1
            for i, s in enumerate(stories):
                num = start_num + i
                raw = s.get('story_name_en', 'Unknown')
                name = raw if len(raw) <= 28 else raw[:26] + ".."  
                price = s.get('price', 0)
                num_str = f"{num:02d}."
                kb.append([InlineKeyboardButton(f"{num_str} {name}  ₹{price}", callback_data=f"mk#st_view_{str(s['_id'])}")])
            nav = []
            if page > 0: nav.append(InlineKeyboardButton("❬ Prev", callback_data=f"mk#ms_list_{page-1}"))
            if total_pages > 1: nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="mk#ignore"))
            if page < total_pages - 1: nav.append(InlineKeyboardButton("Next ❭", callback_data=f"mk#ms_list_{page+1}"))
            if nav: kb.append(nav)
            kb.append([InlineKeyboardButton("« Home", callback_data="mk#back")])
            panel_txt = (
                f"<b>⟦ Manage Stories ⟧</b>\n\n"
                f"<blockquote expandable>Total: <code>{total}</code>   Page: <code>{page+1}/{total_pages}</code>\n\n"
                f"<i>Tap a story to manage or edit it.</i></blockquote>"
            )
            await query.message.edit_text(panel_txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=enums.ParseMode.HTML)

        elif cmd.startswith("st_view_"):
            s_id = cmd.split("_")[2]
            from bson.objectid import ObjectId
            story = await db.db.premium_stories.find_one({"_id": ObjectId(s_id)})
            if not story:
                return await _safe_answer(query, "Not found!")
            await _safe_answer(query)

            sname_en = story.get('story_name_en', 'N/A')
            sname_hi = story.get('story_name_hi', 'N/A')
            platform  = story.get('platform', 'N/A')
            price     = story.get('price', 0)
            genre     = story.get('genre', 'N/A')
            episodes  = story.get('episodes', 'N/A')
            status    = story.get('status', 'N/A')
            desc_raw  = (story.get('description') or '').strip()
            desc      = desc_raw[:200] + ('...' if len(desc_raw) > 200 else '') if desc_raw else 'N/A'

            # Payment methods display
            pay_methods = story.get('payment_methods', ['upi', 'razorpay'])
            upi_icon = '✅' if 'upi' in pay_methods else '❌'
            rzp_icon = '✅' if 'razorpay' in pay_methods else '❌'

            # Forwarding status
            fwd_enabled = story.get('forwarding_enabled', True)
            fwd_icon = '✅' if fwd_enabled else '❌'
            fwd_label = 'ON' if fwd_enabled else 'OFF'

            detail_txt = (
                f"<b>⟦ {sname_en} ⟧</b>\n\n"
                f"<blockquote>"
                f"<b>⨉ Name (HI)   ⟶</b> {sname_hi}\n"
                f"<b>⨉ Platform    ⟶</b> {platform}\n"
                f"<b>⨉ Price       ⟶</b> ₹{price}\n"
                f"<b>⨉ Genre       ⟶</b> {genre}\n"
                f"<b>⨉ Episodes    ⟶</b> {episodes}\n"
                f"<b>⨉ Status      ⟶</b> {status}\n"
                f"<b>⨉ Payments    ⟶</b> UPI {upi_icon}  Razorpay {rzp_icon}\n"
                f"<b>⨉ Forwarding  ⟶</b> {fwd_icon} {fwd_label}\n"
                f"<b>⨉ DB ID       ⟶</b> <code>{s_id}</code>"
                f"</blockquote>\n\n"
                f"<i>{desc}</i>\n\n"
                f"<i>Select a field below to update:</i>"
            )
            kb = [
                [InlineKeyboardButton("Name (EN)", callback_data=f"mk#st_edit_{s_id}_name"),
                 InlineKeyboardButton("Name (HI)", callback_data=f"mk#st_edit_{s_id}_namehi"),
                 InlineKeyboardButton("Telugu Name", callback_data=f"mk#st_edit_{s_id}_namete")],
                [InlineKeyboardButton("Price", callback_data=f"mk#st_edit_{s_id}_price"),
                 InlineKeyboardButton("Image", callback_data=f"mk#st_edit_{s_id}_image")],
                [InlineKeyboardButton("Description", callback_data=f"mk#st_edit_{s_id}_desc"),
                 InlineKeyboardButton("Telugu Desc", callback_data=f"mk#st_edit_{s_id}_descte"),
                 InlineKeyboardButton("Status", callback_data=f"mk#st_edit_{s_id}_status")],
                [InlineKeyboardButton("Genre", callback_data=f"mk#st_edit_{s_id}_genre"),
                 InlineKeyboardButton("Episodes", callback_data=f"mk#st_edit_{s_id}_episodes")],
                [InlineKeyboardButton("DB Range", callback_data=f"mk#st_edit_{s_id}_eps")],
                # Payment method toggles
                [InlineKeyboardButton(f"{upi_icon} Manual UPI", callback_data=f"mk#st_pay_methods_{s_id}_upi"),
                 InlineKeyboardButton(f"{rzp_icon} Razorpay", callback_data=f"mk#st_pay_methods_{s_id}_razorpay")],
                # Forwarding toggle
                [InlineKeyboardButton(f"{fwd_icon} Forwarding: {fwd_label}", callback_data=f"mk#st_fwd_toggle_{s_id}")],
                [InlineKeyboardButton("🔗 Get Share Link", callback_data=f"mk#st_link_{s_id}"),
                 InlineKeyboardButton("Remove Story", callback_data=f"mk#st_confirm_rm_{s_id}")],
                [InlineKeyboardButton("« Back", callback_data="mk#ms_list_0")],
            ]
            await query.message.edit_text(detail_txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=enums.ParseMode.HTML)

        elif cmd.startswith("st_edit_"):
            parts = cmd.split("_")
            s_id = parts[2]
            action = parts[3]
            await query.message.delete()
            asyncio.create_task(_edit_story_flow(client, user_id, s_id, action))

        elif cmd.startswith("st_confirm_rm_"):
            s_id = cmd.split("_")[3]
            kb = [
                [InlineKeyboardButton("Yes, Remove It", callback_data=f"mk#st_rm_{s_id}")],
                [InlineKeyboardButton("Cancel", callback_data=f"mk#st_view_{s_id}")]
            ]
            await query.message.edit_text(
                "<b>⚠️ Confirm Removal</b>\n\n"
                "<blockquote>Are you sure you want to permanently remove this story from the Marketplace?\n"
                "<i>Users will no longer see or purchase it.</i></blockquote>",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode=enums.ParseMode.HTML
            )

        elif cmd.startswith("st_rm_"):
            s_id = cmd.split("_")[2]
            from bson.objectid import ObjectId
            await db.db.premium_stories.delete_one({"_id": ObjectId(s_id)})
            await _safe_answer(query, "Story removed!", show_alert=True)
            query.data = "mk#ms_list_0"
            return await market_callback(client, query)

        elif cmd.startswith("st_fwd_toggle_"):
            s_id = cmd.split("_")[3]
            from bson.objectid import ObjectId
            story = await db.db.premium_stories.find_one({"_id": ObjectId(s_id)})
            if not story: return await _safe_answer(query, "Story not found!", show_alert=True)
            curr_fwd = story.get('forwarding_enabled', True)
            new_fwd = not curr_fwd
            await db.db.premium_stories.update_one(
                {"_id": ObjectId(s_id)},
                {"$set": {"forwarding_enabled": new_fwd}}
            )
            status_txt = "ON ✅" if new_fwd else "OFF ❌"
            await _safe_answer(query, f"Forwarding set to {status_txt}", show_alert=True)
            query.data = f"mk#st_view_{s_id}"
            return await market_callback(client, query)

        elif cmd.startswith("st_link_"):
            s_id = cmd.split("_")[2]
            from bson.objectid import ObjectId
            story = await db.db.premium_stories.find_one({"_id": ObjectId(s_id)})
            bt_doc = await db.db.premium_bots.find_one({"id": int(story.get("bot_id", 0))}) if story.get("bot_id") else None
            bt_cfg_val = (bt_doc.get("config") or {}) if bt_doc else {}
            _mini_app_on = bt_cfg_val.get("mini_app_deep_links", None)
            if _mini_app_on is None:
                _ml_cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
                _mini_app_on = _ml_cfg.get("mini_app_enabled", True)
            
            bot_un = story.get("bot_username", "Bot")
            if _mini_app_on:
                deep_link = f"https://t.me/{bot_un}/apminibyarya?startapp=story_{s_id}"
                msg_txt = "📱 **Mini App Link:**\n"
            else:
                deep_link = f"https://t.me/{bot_un}?start=story_{s_id}"
                msg_txt = "🤖 **Bot Only Link:**\n"
                
            await client.send_message(user_id, f"{msg_txt}<code>{deep_link}</code>\n\n<i>Tap link to copy.</i>")
            await _safe_answer(query)
            return

        elif cmd == "pending":
            await _safe_answer(query)
            pendings = await db.db.premium_checkout.find({"status": "pending_admin_approval"}).to_list(length=30)
            kb = []
            for p in pendings:
                st = await db.db.premium_stories.find_one({"_id": p.get('story_id')})
                s_name = st.get('story_name_en', 'Unknown') if st else 'Deleted Story'
                kb.append([InlineKeyboardButton(f"⏳ {s_name} (User: {p.get('user_id')})", callback_data=f"mk#pnd_view_{str(p['_id'])}")])
            kb.append([InlineKeyboardButton("« Back", callback_data="mk#back")])
            await query.message.edit_text("<b>💸 Pending Payments Queue</b>\n\nHere are all the users waiting for payment verification. Select one to view their screenshot:", reply_markup=InlineKeyboardMarkup(kb))

        elif cmd.startswith("pnd_view_"):
            p_id = cmd.split("_")[2]
            from bson.objectid import ObjectId
            checkout = await db.db.premium_checkout.find_one({"_id": ObjectId(p_id)})
            if not checkout:
                return await _safe_answer(query, "Ticket not found!", show_alert=True)
            st = await db.db.premium_stories.find_one({"_id": checkout.get('story_id')})
            s_name = st.get('story_name_en', 'Unknown') if st else 'Deleted Story'
            txt = f"<b>🧾 Pending Approval</b>\n\n<b>User:</b> <code>{checkout.get('user_id')}</code>\n<b>Story:</b> {s_name}\n<b>Bot Context:</b> @{checkout.get('bot_username')}"
            kb = [
                [InlineKeyboardButton("✅ Approve", callback_data=f"mk#pnd_app_{p_id}"),
                 InlineKeyboardButton("❌ Reject", callback_data=f"mk#pnd_rej_{p_id}")],
                [InlineKeyboardButton("« Back", callback_data="mk#pending")]
            ]
            await query.message.delete()
            if checkout.get("proof_path"):
                await client.send_photo(user_id, photo=checkout.get("proof_path"), caption=txt, reply_markup=InlineKeyboardMarkup(kb))
            else:
                await client.send_message(user_id, txt + "\n\n<i>No local screenshot found.</i>", reply_markup=InlineKeyboardMarkup(kb))

        elif cmd.startswith("pnd_app_"):
            p_id = cmd.split("_")[2]
            from bson.objectid import ObjectId
            checkout = await db.db.premium_checkout.find_one({"_id": ObjectId(p_id)})
            if not checkout:
                return await _safe_answer(query, "Ticket not found!", show_alert=True)
            await db.db.premium_checkout.update_one(
                {"_id": ObjectId(p_id)},
                {"$set": {"status": "approved", "approved_at": datetime.utcnow(), "approved_by": user_id, "updated_at": datetime.utcnow()}}
            )
            await db.add_purchase(checkout['user_id'], str(checkout['story_id']))
            await query.message.delete()
            if "query" in locals() and query: await query.answer()
            await client.send_message(user_id, f"✅ Payment Approved for user `{checkout['user_id']}`!")
            
            st = await db.db.premium_stories.find_one({"_id": checkout['story_id']})
            if st:
                from utils import log_payment
                user_info = await db.get_user(checkout['user_id'])
                s_name = st.get("story_name_en", "Unknown")
                amount_int = int(st.get("price", "0"))
                asyncio.create_task(log_payment(
                    user_id=checkout['user_id'],
                    user_first_name=user_info.get("first_name", "User"),
                    s_name=s_name,
                    amount=amount_int,
                    method="manual_upi",
                    receipt_id=str(checkout.get('_id', '')),
                    photo_path=checkout.get("proof_path")
                ))

                order_id = checkout.get("order_id")
                if not order_id or str(order_id).startswith("OD-") or str(order_id).startswith("OD_"):
                    try:
                        from plugins.userbot.market_seller import _make_arya_bot_order_id
                        order_id = await _make_arya_bot_order_id(checkout['user_id'], str(checkout.get('story_id')))
                    except Exception:
                        from mini_app_api import _make_arya_order_id
                        order_id = await _make_arya_order_id(db, str(checkout['user_id']), [str(checkout.get('story_id'))], source="bot")

                # Payment Receipt sending is DISABLED (user request)
                # from utils_invoice import send_invoice_to_user
                # if u_cli:
                #     asyncio.create_task(send_invoice_to_user(
                #         client=u_cli, user_id=checkout['user_id'],
                #         order_id=order_id, amount=amount_int,
                #         method="MANUAL UPI", story=st, checkout=checkout
                #     ))

                from plugins.userbot.market_seller import market_clients, dispatch_delivery_choice
                u_cli = market_clients.get(str(checkout['bot_id']))
                if u_cli:
                    try:
                        await u_cli.delete_messages(checkout['user_id'], checkout.get('status_msg_id', 0))
                    except Exception:
                        pass
                    asyncio.create_task(dispatch_delivery_choice(u_cli, checkout['user_id'], st))
                
                try:
                    from purchase_dm_helper import send_purchase_success_dm
                    asyncio.create_task(send_purchase_success_dm(
                        db=db,
                        user_id=checkout['user_id'],
                        story_ids=[str(checkout['story_id'])],
                        order_id=order_id,
                        amount=amount_int,
                        payment_method="UPI (UTR)",
                        verified_by="Access Granted By Team",
                        is_admin_manual=True
                    ))
                except Exception as _dm_err:
                    logger.warning(f"Failed to send purchase DM on manual approval: {_dm_err}")

        elif cmd.startswith("pnd_rej_"):
            p_id = cmd.split("_")[2]
            try: await query.message.delete()
            except: pass
            if "query" in locals() and query: await query.answer()
            asyncio.create_task(_reject_payment_flow(client, user_id, p_id))

        elif cmd.startswith("p_wa_"):
            b_id = cmd.split("_")[2]
            kb = [
                [_ikb("Welcome Msg", callback_data=f"mk#welcome_cfg_{b_id}", icon_custom_emoji_id="6041921818896372382")],
                [_ikb("Menu Media", callback_data=f"mk#menu_media_{b_id}", icon_custom_emoji_id="6026089641730382702")],
                [InlineKeyboardButton("« Back", callback_data=f"mk#bot_view_{b_id}")],
            ]
            wa_txt = (
                f'<emoji id="6041921818896372382">📜</emoji> <b>Welcome & About</b>\n'
                f'━━━━━━━━━━━━━━━━━━━━━\n\n'
                f"Configure welcome text and menu media for your delivery store bot:\n\n"
                f"• <b>Welcome Msg:</b> Customize welcome text, about description, and quote\n"
                f"• <b>Menu Media:</b> Add random photos/GIFs/videos shown on main menu"
            )
            await _edit_or_send_mgmt_view(client, query.message.chat.id, wa_txt, InlineKeyboardMarkup(kb), query=query)

        elif cmd.startswith("welcome_cfg_"):
            b_id = cmd.split("_")[2]
            kb = [
                [InlineKeyboardButton("Welcome Msg", callback_data=f"mk#pset_{b_id}_welcome")],
                [InlineKeyboardButton("About", callback_data=f"mk#pset_{b_id}_about")],
                [InlineKeyboardButton("Quote", callback_data=f"mk#pset_{b_id}_quote"),
                 InlineKeyboardButton("Quote Author", callback_data=f"mk#pset_{b_id}_quote_author")],
                [InlineKeyboardButton("🔄 Reset to Default", callback_data=f"mk#welcome_reset_{b_id}")],
                [InlineKeyboardButton("« Back", callback_data=f"mk#p_wa_{b_id}")],
            ]
            wcfg_txt = (
                f'<emoji id="6041921818896372382">📜</emoji> <b>Welcome Message Settings</b>\n'
                f'━━━━━━━━━━━━━━━━━━━━━\n\n'
                f"Set each block shown in delivery main menu card, or reset all text to default."
            )
            await _edit_or_send_mgmt_view(client, query.message.chat.id, wcfg_txt, InlineKeyboardMarkup(kb), query=query)

        elif cmd.startswith("welcome_reset_"):
            b_id = cmd.split("_")[2]
            await db.db.premium_bots.update_one(
                {"id": int(b_id)},
                {"$unset": {
                    "config.welcome": "",
                    "config.about": "",
                    "config.quote": "",
                    "config.quote_author": ""
                }}
            )
            await _safe_answer(query, "✅ Welcome Message & About reset to default!", show_alert=True)
            query.data = f"mk#welcome_cfg_{b_id}"
            return await market_callback(client, query)

        elif cmd.startswith("menu_media_add_"):
            b_id = cmd.split("_")[3]
            await query.message.delete()
            asyncio.create_task(_menu_media_add_flow(client, user_id, b_id))

        elif cmd.startswith("menu_media_bulk_"):
            b_id = cmd.split("_")[3]
            await query.message.delete()
            asyncio.create_task(_menu_media_bulk_add_flow(client, user_id, b_id))

        elif cmd.startswith("menu_media_prev_"):
            parts = cmd.split("_")
            b_id = parts[3]
            idx = int(parts[4])
            await query.message.delete()
            asyncio.create_task(_menu_media_preview_flow(client, user_id, b_id, idx))

        elif cmd.startswith("menu_media_del_"):
            # mk#menu_media_del_<bId>_<idx>
            parts = cmd.split("_")
            b_id = parts[3]
            idx = int(parts[4])
            bot = await _find_premium_bot(b_id)
            if not bot:
                return await _safe_answer(query, "Bot not found!", show_alert=True)
            cfg = bot.get("config", {}) or {}
            items = _cfg_list(cfg, "menu_media")
            real_items = [x for x in items if isinstance(x, dict)]
            if idx < 1 or idx > len(real_items):
                return await _safe_answer(query, "Invalid item index", show_alert=True)
            real_items.pop(idx - 1)
            await db.db.premium_bots.update_one({"id": int(b_id)}, {"$set": {"config.menu_media": real_items}})
            query.data = f"mk#menu_media_{b_id}"
            return await market_callback(client, query)

        elif cmd.startswith("menu_media_"):
            b_id = cmd.split("_")[2]
            bot = await _find_premium_bot(b_id)
            if not bot:
                return await _safe_answer(query, "Bot not found!", show_alert=True)

            cfg = bot.get("config", {}) or {}
            items = _cfg_list(cfg, "menu_media")
            if not items and cfg.get("menuimg"):
                # Backward compatible: show legacy single image as item 1 (read-only hint)
                items = [{"type": "photo", "file_id": cfg.get("menuimg"), "legacy": True}]

            lines = [
                f'<emoji id="6026089641730382702">🖼️</emoji> <b>Menu Media</b>\n'
                f'━━━━━━━━━━━━━━━━━━━━━\n\n'
                f"<b>» Bot:</b> @{bot.get('username', '')}\n"
                f"<i>Shown randomly to users on /start. Supports Photo, GIF, Video. Max 30 items.</i>\n"
            ]
            if items:
                for i, it in enumerate(items, start=1):
                    t = (it or {}).get("type", "media")
                    legacy = " (legacy)" if (it or {}).get("legacy") else ""
                    lines.append(f"<b>{i}.</b> <code>{t}</code>{legacy}")
            else:
                lines.append("<blockquote>No media added yet.</blockquote>")

            kb = []
            if len([x for x in items if not (x or {}).get("legacy")]) < 30:
                kb.append([InlineKeyboardButton("➕ Add Media", callback_data=f"mk#menu_media_add_{b_id}")])
                kb.append([InlineKeyboardButton("📥 Bulk Add Media", callback_data=f"mk#menu_media_bulk_{b_id}")])
            if items:
                kb.append([InlineKeyboardButton("👁 Preview", callback_data=f"mk#menu_media_prev_{b_id}_1")])
            kb.append([InlineKeyboardButton("« Back", callback_data=f"mk#p_wa_{b_id}")])
            await _edit_or_send_mgmt_view(client, query.message.chat.id, "\n".join(lines), InlineKeyboardMarkup(kb), query=query)


        elif cmd.startswith("pset_"):
            parts = cmd.split("_")
            b_id = parts[1]
            key = "_".join(parts[2:])
            await query.message.delete()
            asyncio.create_task(_premium_bot_set(client, user_id, b_id, key, key.capitalize()))

        elif cmd == "add_story":
            try: await query.message.delete()
            except: pass
            if "query" in locals() and query: await query.answer()
            asyncio.create_task(_add_story_flow(client, user_id))

        elif cmd == "approve":
            p_id = data[2]
            try: await query.message.delete()
            except: pass
            if "query" in locals() and query: await query.answer()
            asyncio.create_task(_approve_payment_flow(client, user_id, p_id))

        elif cmd == "reject":
            p_id = data[2]
            try: await query.message.delete()
            except: pass
            if "query" in locals() and query: await query.answer()
            asyncio.create_task(_reject_payment_flow(client, user_id, p_id))
    except MessageNotModified:
        pass  # User tapped same button — silently ignore
    except PyMongoError as db_err:
        logger.error(f"Mongo operation failed in market_callback: {db_err}")
        await _safe_answer(query, "Database connection issue. Please try again.", show_alert=True)
        try:
            await client.send_message(query.from_user.id, "⚠️ Database is temporarily unreachable (SSL/connection issue). Please retry in a moment.")
        except Exception:
            pass
    except Exception as e:
        logger.error(f"market_callback error: {e}")
        if "query" in locals() and query:
            return await query.answer("Something went wrong. Please retry.", show_alert=True)

async def _reject_request_flow(client, user_id, req_id):
    from bson.objectid import ObjectId
    try: r = await db.db.premium_requests.find_one({"_id": ObjectId(req_id)})
    except: r = None
    if not r: return await client.send_message(user_id, "Request not found.")
    
    msg = await native_ask(client, user_id, f"<b>❌ REJECT REQUEST</b>\n\nStory: {r.get('story_name')}\n\nEnter the reason for rejection (this will be sent to the user):", reply_markup=ReplyKeyboardMarkup([["⛔ Cancel"]], resize_keyboard=True))
    if getattr(msg, 'text', None) and "Cancel" in msg.text:
         return await client.send_message(user_id, "<i>Cancelled.</i>", reply_markup=ReplyKeyboardRemove())
         
    reason = (getattr(msg, 'text', '') or 'Not specified.').strip()
    
    from datetime import datetime
    # Fix: Actually remove from list after rejection and notification
    await db.db.premium_requests.delete_one({"_id": r['_id']})
    
    bot_id_str = str(r.get('bot_id'))
    from plugins.userbot.market_seller import market_clients
    if bot_id_str in market_clients:
        t_cli = market_clients[bot_id_str]
        try:
            u_doc = await db.get_user(r.get('user_id'))
            t_lang = u_doc.get("lang", "en")
            if t_lang == "hi":
                alert = f"<b>⚠️ कहानी अनुरोध अस्वीकृत</b>\n\n<b>कहानी:</b> {r.get('story_name')}\n<b>कारण:</b> {reason}"
            else:
                alert = f"<b>⚠️ STORY REQUEST REJECTED</b>\n\n<b>Story:</b> {r.get('story_name')}\n<b>Reason:</b> {reason}"
            await t_cli.send_message(r.get('user_id'), alert)
        except Exception: pass
        
    await client.send_message(user_id, f"✅ Request rejected and user notified.\nReason: {reason}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« " + utils.to_smallcap("Back to List"), callback_data="mk#reqs_0")]]))


async def _fb_reply_flow(client, user_id, fb_id: str, target_uid: int):
    """Admin flow: compose and send a reply to a user's feedback."""
    from bson.objectid import ObjectId

    # Fetch feedback doc
    try:
        fb = await db.db.premium_feedback.find_one({"_id": ObjectId(fb_id)})
    except Exception:
        fb = None
    if not fb:
        return await client.send_message(user_id, "<i>Feedback entry not found.</i>", parse_mode=enums.ParseMode.HTML)

    preview = (fb.get("text") or "")[:120].replace("\n", " ")
    prompt = (
        f"<b>⟦ Reply to User ⟧</b>\n\n"
        f"<blockquote>"
        f"<b>User:</b> {fb.get('user_name', 'N/A')} (<code>{target_uid}</code>)\n"
        f"<b>Preview:</b> {preview or 'N/A'}"
        f"</blockquote>\n\n"
        f"<i>Send your reply below (text, photo, or video).\nType /cancel to abort.</i>"
    )
    cancel_kb = ReplyKeyboardMarkup([["⛔ Cancel"]], resize_keyboard=True, one_time_keyboard=True)
    reply_msg = await native_ask(client, user_id, prompt, reply_markup=cancel_kb, parse_mode=enums.ParseMode.HTML)

    if _is_cancel(reply_msg):
        return await client.send_message(
            user_id, "<i>Reply cancelled.</i>",
            reply_markup=ReplyKeyboardRemove(), parse_mode=enums.ParseMode.HTML
        )

    # Determine which seller bot client belongs to this feedback
    bot_id = fb.get("bot_id")
    seller_cli = None
    try:
        from plugins.userbot.market_seller import market_clients
        if bot_id and str(bot_id) in market_clients:
            seller_cli = market_clients.get(str(bot_id))
        else:
            # Try resolving from user's bot_ids
            user_doc = await db.db.users.find_one({"id": int(target_uid)})
            if user_doc and user_doc.get("bot_ids"):
                for bid in user_doc["bot_ids"]:
                    if str(bid) in market_clients:
                        seller_cli = market_clients[str(bid)]
                        break
        # Fallback to first available seller client
        if not seller_cli and market_clients:
            seller_cli = list(market_clients.values())[0]
    except Exception as e:
        logger.error(f"Failed to find seller bot for {target_uid} in _fb_reply_flow: {e}")

    if not seller_cli:
        # Can't reach user without the seller bot — warn admin
        await client.send_message(
            user_id,
            "<b>Warning:</b> No active seller bot found for this feedback entry.\n"
            "<i>Please send the reply manually via the correct seller bot.</i>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Support Panel", callback_data="mk#fb_panel_0")]]),
            parse_mode=enums.ParseMode.HTML
        )
        return

    # Clean reply header
    reply_header = "<b>Reply from Arya Premium Support</b>\n\n"
    back_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("« Main Menu", callback_data="mb#main_back")
    ]])

    delivered = False
    tmp_path = None

    try:
        import os
        # Determine media type in admin's reply message
        has_media = any([
            reply_msg.photo, reply_msg.video, reply_msg.animation,
            reply_msg.document, reply_msg.voice, reply_msg.audio
        ])

        if has_media:
            # Download via mgmt bot, re-upload via seller_cli
            # (file_ids are bot-specific — cross-bot direct use causes MEDIA_EMPTY)
            if reply_msg.photo: ext = ".jpg"
            elif reply_msg.video: ext = ".mp4"
            elif reply_msg.animation: ext = ".mp4"
            elif reply_msg.voice: ext = ".ogg"
            elif reply_msg.audio: ext = ".mp3"
            else: ext = ""

            tmp_path = await client.download_media(reply_msg, file_name=f"downloads/fbreply_{fb_id}{ext}")

        caption_text = reply_header + (reply_msg.caption or "")

        if tmp_path and reply_msg.photo:
            await seller_cli.send_photo(target_uid, photo=tmp_path, caption=caption_text, reply_markup=back_kb, parse_mode=enums.ParseMode.HTML)
        elif tmp_path and reply_msg.video:
            await seller_cli.send_video(target_uid, video=tmp_path, caption=caption_text, reply_markup=back_kb, parse_mode=enums.ParseMode.HTML)
        elif tmp_path and reply_msg.animation:
            await seller_cli.send_animation(target_uid, animation=tmp_path, caption=caption_text, reply_markup=back_kb, parse_mode=enums.ParseMode.HTML)
        elif tmp_path and reply_msg.voice:
            await seller_cli.send_voice(target_uid, voice=tmp_path, caption=caption_text, reply_markup=back_kb, parse_mode=enums.ParseMode.HTML)
        elif tmp_path and reply_msg.audio:
            await seller_cli.send_audio(target_uid, audio=tmp_path, caption=caption_text, reply_markup=back_kb, parse_mode=enums.ParseMode.HTML)
        elif tmp_path and reply_msg.document:
            await seller_cli.send_document(target_uid, document=tmp_path, caption=caption_text, reply_markup=back_kb, parse_mode=enums.ParseMode.HTML)
        else:
            txt = reply_header + (reply_msg.text or "")
            await seller_cli.send_message(target_uid, txt, reply_markup=back_kb, parse_mode=enums.ParseMode.HTML)
        delivered = True
    except Exception as e:
        logger.error(f"_fb_reply_flow: failed to deliver to {target_uid}: {e}")
    finally:
        if tmp_path:
            try:
                import os
                if os.path.exists(tmp_path): os.remove(tmp_path)
            except: pass

    if delivered:
        try:
            from datetime import datetime as _dt
            await db.db.premium_feedback.update_one(
                {"_id": fb["_id"]},
                {"$set": {"status": "solved", "resolved_at": _dt.utcnow(), "reply_sent": True}}
            )
        except Exception:
            pass
        await client.send_message(
            user_id,
            f"✅ <b>Reply sent successfully!</b>\nFeedback <code>#{fb_id[:8]}</code> marked as <b>solved</b>.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Support Panel", callback_data="mk#fb_panel_0"),
                InlineKeyboardButton("« Home", callback_data="mk#back")
            ]]),
            parse_mode=enums.ParseMode.HTML
        )
    else:
        await client.send_message(
            user_id,
            f"❌ <b>Failed to deliver reply to <code>{target_uid}</code>.</b>\n"
            f"<i>The user may have blocked the bot. Feedback remains open.</i>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Support Panel", callback_data="mk#fb_panel_0")
            ]]),
            parse_mode=enums.ParseMode.HTML
        )


async def _settings_flow(client, user_id, cmd):

    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="ask_cancel")]])
    if cmd.startswith("set_upi"):
        parts = cmd.split("_")
        upi_idx = parts[2] if len(parts) > 2 else "1"
        
        id_key = "upi_id" if upi_idx == "1" else f"upi_id_{upi_idx}"
        name_key = "upi_payee_name" if upi_idx == "1" else f"upi_payee_name_{upi_idx}"
        
        cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
        curr_val = cfg.get(id_key, "")
        if not curr_val and upi_idx == "1":
            curr_val = await db.get_config("upi_id") or ""
            
        current_hint = f"\n\n<i>Current UPI ID: <code>{curr_val}</code></i>" if curr_val else ""
        
        msg = await native_ask(
            client,
            user_id,
            f"<b>❪ SET UPI ID {upi_idx} ❫</b>{current_hint}\n\n"
            "Enter the UPI ID (e.g. <code>heyjeetx@naviaxis</code>) or reply <code>skip</code> / <code>clear</code> to clear this slot:",
            reply_markup=cancel_kb
        )
        from pyrogram.types import CallbackQuery as _CQ
        if isinstance(msg, _CQ) or not getattr(msg, 'text', None):
            return await client.send_message(
                user_id,
                "<i>Process Cancelled. UPI ID unchanged.</i>",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Payments", callback_data="mk#settings_payments")]])
            )
            
        entered = msg.text.strip()
        if entered.lower() in ("skip", "/skip", "clear", "none"):
            await db.db.mini_app_config.update_one(
                {"_key": "feature_toggles"},
                {"$set": {id_key: "", name_key: ""}},
                upsert=True
            )
            if upi_idx == "1":
                await db.set_config("upi_id", "")
            return await client.send_message(
                user_id,
                f"✅ UPI ID {upi_idx} cleared successfully!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Payments", callback_data="mk#settings_payments")]])
            )
            
        new_upi = entered
        
        curr_name = cfg.get(name_key, "")
        current_name_hint = f"\n\n<i>Current Payee Name: <code>{curr_name}</code></i>" if curr_name else ""
        
        msg_name = await native_ask(
            client,
            user_id,
            f"<b>❪ SET PAYEE NAME FOR UPI ID {upi_idx} ❫</b>{current_name_hint}\n\n"
            f"Enter the Payee (Merchant) Name for <code>{new_upi}</code>:",
            reply_markup=cancel_kb
        )
        if isinstance(msg_name, _CQ) or not getattr(msg_name, 'text', None):
            return await client.send_message(
                user_id,
                "<i>Process Cancelled. UPI ID unchanged.</i>",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Payments", callback_data="mk#settings_payments")]])
            )
            
        new_name = msg_name.text.strip()
        
        await db.db.mini_app_config.update_one(
            {"_key": "feature_toggles"},
            {"$set": {id_key: new_upi, name_key: new_name}},
            upsert=True
        )
        if upi_idx == "1":
            await db.set_config("upi_id", new_upi)
            
        await client.send_message(
            user_id,
            f"✅ <b>UPI ID {upi_idx} saved!</b>\n"
            f"<b>UPI ID:</b> <code>{new_upi}</code>\n"
            f"<b>Payee Name:</b> <code>{new_name}</code>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Settings", callback_data="mk#settings")]]),
            parse_mode=enums.ParseMode.HTML
        )

    elif cmd == "set_groq":
        from pyrogram.types import CallbackQuery as _CQ
        current_key = await db.get_config("groq_api_key")
        current_hint = f"\n\n<i>Current key: <code>{(current_key or '')[:12]}…</code></i>" if current_key else ""
        msg = await native_ask(
            client, user_id,
            f"<b>❪ SET GROQ AI API KEY ❫</b>{current_hint}\n\n"
            "Enter your Groq API key (from <a href='https://console.groq.com/keys'>console.groq.com</a>):\n"
            "<i>This is used for smart Hindi transliteration and description translation in story creation.</i>",
            reply_markup=cancel_kb
        )
        if isinstance(msg, _CQ) or not getattr(msg, 'text', None):
            return await client.send_message(user_id, "<i>Cancelled. Groq key unchanged.</i>")
        new_key = msg.text.strip()
        if not new_key.startswith("gsk_"):
            return await client.send_message(
                user_id,
                "❌ Invalid Groq API key format. Keys start with <code>gsk_</code>. Please try again.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Settings", callback_data="mk#settings")]]),
                parse_mode=enums.ParseMode.HTML
            )
        await db.set_config("groq_api_key", new_key)
        await client.send_message(
            user_id,
            f"✅ <b>Groq AI Key saved!</b>\n<code>{new_key[:12]}…</code>\n\n"
            "<i>New stories and edits will now use Groq AI for Hindi transliteration and translation.</i>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Settings", callback_data="mk#settings")]]),
            parse_mode=enums.ParseMode.HTML
        )

    elif cmd == "set_gmail_user":
        from pyrogram.types import CallbackQuery as _CQ
        cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
        current_email = cfg.get("gmail_user", "")
        current_hint = f"\n\n<i>Current Gmail: <code>{current_email}</code></i>" if current_email else ""
        
        msg = await native_ask(
            client, user_id,
            f"<b>❪ SET GMAIL EMAIL ADDRESS ❫</b>{current_hint}\n\n"
            "Enter the Gmail email address used for transaction notifications:\n"
            "<i>This must be the Gmail inbox receiving Slice Bank alerts.</i>",
            reply_markup=cancel_kb
        )
        if isinstance(msg, _CQ) or not getattr(msg, 'text', None):
            return await client.send_message(
                user_id, 
                "<i>Process Cancelled. Gmail email address unchanged.</i>",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Payments", callback_data="mk#settings_payments")]])
            )
            
        new_email = msg.text.strip()
        await db.db.mini_app_config.update_one(
            {"_key": "feature_toggles"},
            {"$set": {"gmail_user": new_email}},
            upsert=True
        )
        await db.set_config("gmail_user", new_email)
        
        await client.send_message(
            user_id,
            f"✅ <b>Gmail Email Address saved!</b>\n<code>{new_email}</code>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Settings", callback_data="mk#settings")]]),
            parse_mode=enums.ParseMode.HTML
        )
        
    elif cmd == "set_gmail_pwd":
        from pyrogram.types import CallbackQuery as _CQ
        cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
        current_pwd = cfg.get("gmail_app_password", "")
        current_hint = f"\n\n<i>Current Password: <code>{(current_pwd or '')[:6]}…</code></i>" if current_pwd else ""
        
        msg = await native_ask(
            client, user_id,
            f"<b>❪ SET GMAIL APP PASSWORD ❫</b>{current_hint}\n\n"
            "Enter the 16-character Google App Password (not your main password):\n"
            "<i>Generate this app-specific password via your Google Account Settings under 2-Step Verification.</i>",
            reply_markup=cancel_kb
        )
        if isinstance(msg, _CQ) or not getattr(msg, 'text', None):
            return await client.send_message(
                user_id, 
                "<i>Process Cancelled. Gmail App Password unchanged.</i>",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Payments", callback_data="mk#settings_payments")]])
            )
            
        new_pwd = msg.text.strip().replace(" ", "")
        await db.db.mini_app_config.update_one(
            {"_key": "feature_toggles"},
            {"$set": {"gmail_app_password": new_pwd}},
            upsert=True
        )
        await db.set_config("gmail_app_password", new_pwd)
        
        await client.send_message(
            user_id,
            f"✅ <b>Gmail App Password saved!</b>\n<code>{new_pwd[:4]}…</code>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Settings", callback_data="mk#settings")]]),
            parse_mode=enums.ParseMode.HTML
        )
        
    elif cmd == "set_db":
        await client.send_message(user_id, "Channels are now managed from the Channels panel.", reply_markup=ReplyKeyboardRemove())


async def _bot_set_logch_flow(client, user_id, b_id):
    from pyrogram.types import CallbackQuery as _CQ
    bt = await _find_premium_bot(b_id)
    if not bt:
        return await client.send_message(user_id, "❌ Bot not found!")
    
    cfg = bt.get("config", {}) or {}
    curr_log = cfg.get("log_channel", "Default Global")
    
    msg = await native_ask(
        client,
        user_id,
        f"<b>📋 SET EVENT LOG CHANNEL</b>\n\n"
        f"<b>Bot:</b> @{bt.get('username')}\n"
        f"<b>Current Log Channel:</b> <code>{curr_log}</code>\n\n"
        f"Forward a message from your target Log Channel or send the Channel ID (e.g. <code>-1001234567890</code>):\n\n"
        f"<i>💡 General bot activity logs will go to this channel. Payment & Delivery logs will continue to go to their dedicated channels.\n\n"
        f"Send <code>reset</code> or <code>default</code> to use the global default channel.</i>",
        reply_markup=ReplyKeyboardMarkup([["⛔ Cancel", "🔄 Reset to Default"]], resize_keyboard=True, one_time_keyboard=True)
    )
    
    if isinstance(msg, _CQ):
        return await client.send_message(user_id, "<i>Process Cancelled!</i>", reply_markup=ReplyKeyboardRemove())

    txt = getattr(msg, 'text', '') or ''
    if "Cancel" in txt or not txt:
        return await client.send_message(user_id, "<i>Process Cancelled!</i>", reply_markup=ReplyKeyboardRemove())
    
    if "Reset" in txt or txt.strip().lower() in ("reset", "default"):
        await db.db.premium_bots.update_one({"id": int(b_id)}, {"$unset": {"config.log_channel": ""}})
        return await client.send_message(user_id, f"✅ Log channel for @{bt.get('username')} reset to **Default Global** channel!", reply_markup=ReplyKeyboardRemove())

    ch_id = None
    if getattr(msg, 'forward_from_chat', None):
        ch_id = msg.forward_from_chat.id
    else:
        try:
            ch_id = int(txt.strip())
        except ValueError:
            if txt.strip().startswith("@"):
                try:
                    c = await client.get_chat(txt.strip())
                    ch_id = c.id
                except Exception as e:
                    return await client.send_message(user_id, f"❌ Failed to find channel: {e}", reply_markup=ReplyKeyboardRemove())

    if not ch_id:
        return await client.send_message(user_id, "❌ Invalid channel. Please forward a message from the channel or enter a valid channel ID.", reply_markup=ReplyKeyboardRemove())

    await db.db.premium_bots.update_one({"id": int(b_id)}, {"$set": {"config.log_channel": ch_id}})
    return await client.send_message(user_id, f"✅ Event Log Channel for @{bt.get('username')} updated to <code>{ch_id}</code>!", reply_markup=ReplyKeyboardRemove())


async def _add_store_bot_flow(client, user_id):
    msg = await native_ask(client, user_id, "<b>❪ ADD CONNECTED BOT ❫</b>\n\nForward the Bot Token from @BotFather:", reply_markup=ReplyKeyboardMarkup([["⛔ Cancel"]], resize_keyboard=True, one_time_keyboard=True))
    from pyrogram.types import CallbackQuery as _CQ
    txt = getattr(msg, 'text', '') or ''
    if isinstance(msg, _CQ) or "Cancel" in txt or not txt:
        return await client.send_message(user_id, "<i>Process Cancelled!</i>", reply_markup=ReplyKeyboardRemove())

    import re
    msg_text = txt
    # Extract Bot Token format safely
    match = re.search(r'\d{8,11}:[a-zA-Z0-9_-]{35,}', msg_text)
    if not match:
        return await client.send_message(user_id, "❌ **Error:** No valid Bot Token found in your message. Provide just the token or forward it directly.", reply_markup=ReplyKeyboardRemove())
        
    token = match.group(0)
    
    try:
        from config import Config
        test_cli = Client(name=f"test_{user_id}", api_id=Config.API_ID, api_hash=Config.API_HASH, bot_token=token, in_memory=True)
        await test_cli.start()
        me = await test_cli.get_me()
        await test_cli.stop()
    except Exception as e:
        # Graceful error
        msg_err = str(e)
        if "ACCESS_TOKEN_INVALID" in msg_err: msg_err = "The Token you provided is invalid or revoked. Please get a fresh token from @BotFather."
        return await client.send_message(user_id, f"❌ **Bot Token Error:**\n{msg_err}", reply_markup=ReplyKeyboardRemove())

    await db.db.premium_bots.update_one({"id": me.id}, {"$set": {"id": me.id, "username": me.username, "name": me.first_name, "token": token}}, upsert=True)
    
    # Live Boot logic to prevent needing a restart
    from plugins.userbot.market_seller import _process_start, _process_callback, _process_screenshot, _process_text, _process_inline_query, market_clients
    try:
        from pyrogram.handlers import MessageHandler, CallbackQueryHandler, InlineQueryHandler
        from utils import setup_ask_router
        new_cli = Client(name=f"market_{me.id}", api_id=Config.API_ID, api_hash=Config.API_HASH, bot_token=token, in_memory=False)
        setup_ask_router(new_cli)
        new_cli.add_handler(MessageHandler(_process_start, filters.command("start") & filters.private))
        new_cli.add_handler(CallbackQueryHandler(_process_callback, filters.regex(r'^mb#')))
        new_cli.add_handler(MessageHandler(_process_screenshot, filters.photo & filters.private))
        new_cli.add_handler(MessageHandler(_process_text, filters.text & filters.private))
        new_cli.add_handler(InlineQueryHandler(_process_inline_query))
        await new_cli.start()
        market_clients[str(me.id)] = new_cli
    except Exception as e:
        logger.error(f"Failed to auto-boot Premium bot: {e}")
        
    await client.send_message(user_id, f"✅ Premium Bot @{me.username} successfully added and **Live Booted!**\nIt is now actively listening for buyers.", reply_markup=ReplyKeyboardRemove())


async def _add_story_flow(client, user_id):
    try:
        sj = {}
        step = 1
        source_chat = None
        undo_cancel_kb = ReplyKeyboardMarkup([["↩️ Undo", "⛔ Cancel"]], resize_keyboard=True)

        while step <= 14:
            # ── STEP 1: SELECT STORE BOT ──────────────────────────────────────
            if step == 1:
                bots = await db.db.premium_bots.find().to_list(length=None)
                if not bots:
                    return await client.send_message(user_id, "<b>‣ No Connected Bots available. Please Add Connected Bot first.</b>", reply_markup=ReplyKeyboardRemove())

                # All available store bots in one row
                bot_row = [f"@{b['username']}" for b in bots]
                bot_kb = ReplyKeyboardMarkup([bot_row, ["⛔ Cancel"]], resize_keyboard=True)

                msg_bot = await native_ask(
                    client,
                    user_id,
                    "<b>❪ STEP 1: SELECT STORE BOT ❫</b>\n\nChoose the bot to sell this story via:",
                    reply_markup=bot_kb
                )
                txt = (getattr(msg_bot, "text", "") or "").strip()
                if txt in ("⛔ Cancel", "Cancel", "Cᴀɴᴄᴇʟ"):
                    return await client.send_message(user_id, "<i>Process Cancelled!</i>", reply_markup=ReplyKeyboardRemove())

                usr = txt.replace("@", "").strip()
                sel_bot = next((b for b in bots if b["username"] == usr), None)
                if sel_bot:
                    sj["bot_id"] = sel_bot["id"]
                    sj["bot_username"] = sel_bot["username"]
                    step = 2
                else:
                    await client.send_message(user_id, "❌ Invalid bot selection. Please choose from the keyboard.")
                continue

            # ── STEP 2: SOURCE DB CHANNEL ─────────────────────────────────────
            elif step == 2:
                db_channels = await db.db.premium_channels.find({"type": "db"}).to_list(length=None)
                if db_channels:
                    src_kb_list = [[f"{c.get('name', c['channel_id'])} ({c['channel_id']})"] for c in db_channels]
                    src_kb_list += [["Manual / Forward / Link"], ["↩️ Undo", "⛔ Cancel"]]
                    prompt = "<b>❪ STEP 2: SOURCE CHANNEL ❫</b>\n\nSelect a Source (DB) channel or choose Manual mode:"
                else:
                    src_kb_list = [["Manual / Forward / Link"], ["↩️ Undo", "⛔ Cancel"]]
                    prompt = "<b>❪ STEP 2: SOURCE CHANNEL ❫</b>\n\nNo saved source channels. Select Manual mode or forward a message:"

                msg_src = await native_ask(
                    client,
                    user_id,
                    prompt,
                    reply_markup=ReplyKeyboardMarkup(src_kb_list, resize_keyboard=True),
                )
                txt = (getattr(msg_src, "text", "") or "").strip()
                if txt in ("↩️ Undo", "Undo"):
                    step = 1
                    continue
                if txt in ("⛔ Cancel", "Cancel", "Cᴀɴᴄᴇʟ"):
                    return await client.send_message(user_id, "<i>Process Cancelled!</i>", reply_markup=ReplyKeyboardRemove())

                if getattr(msg_src, "forward_from_chat", None):
                    source_chat = msg_src.forward_from_chat.id
                    sj["source"] = source_chat
                    step = 3
                    continue

                if txt != "Manual / Forward / Link":
                    for ch in db_channels:
                        key = f"{ch.get('name', ch['channel_id'])} ({ch['channel_id']})"
                        if key == txt:
                            source_chat = ch["channel_id"]
                            sj["source"] = source_chat
                            break
                    if not source_chat and txt.lstrip("-").isdigit():
                        source_chat = int(txt)
                        sj["source"] = source_chat
                step = 3
                continue

            # ── STEP 3: START MESSAGE ─────────────────────────────────────────
            elif step == 3:
                msg_s = await native_ask(
                    client,
                    user_id,
                    "<b>❪ STEP 3: START MESSAGE ❫</b>\n\nForward the <b>first message</b> of the story (or send its link / message ID):",
                    reply_markup=undo_cancel_kb,
                )
                txt = (getattr(msg_s, "text", "") or "").strip()
                if txt in ("↩️ Undo", "Undo"):
                    step = 2
                    continue
                if txt in ("⛔ Cancel", "Cancel", "Cᴀɴᴄᴇʟ"):
                    return await client.send_message(user_id, "<i>Process Cancelled!</i>", reply_markup=ReplyKeyboardRemove())

                try:
                    sj["start_id"] = parse_id(msg_s)
                    if getattr(msg_s, "forward_from_chat", None):
                        source_chat = msg_s.forward_from_chat.id
                    elif getattr(msg_s, "text", None):
                        ch, _mid = parse_chat_from_link(msg_s.text)
                        if ch:
                            source_chat = ch
                    step = 4
                except Exception:
                    await client.send_message(user_id, "❌ Invalid start message. Please forward an episode or send a valid link.")
                continue

            # ── STEP 4: LAST MESSAGE ──────────────────────────────────────────
            elif step == 4:
                msg_e = await native_ask(
                    client,
                    user_id,
                    "<b>❪ STEP 4: LAST MESSAGE ❫</b>\n\nForward the <b>last message</b> of the story (or send its link / message ID):",
                    reply_markup=undo_cancel_kb,
                )
                txt = (getattr(msg_e, "text", "") or "").strip()
                if txt in ("↩️ Undo", "Undo"):
                    step = 3
                    continue
                if txt in ("⛔ Cancel", "Cancel", "Cᴀɴᴄᴇʟ"):
                    return await client.send_message(user_id, "<i>Process Cancelled!</i>", reply_markup=ReplyKeyboardRemove())

                try:
                    sj["end_id"] = parse_id(msg_e)
                    if getattr(msg_e, "forward_from_chat", None) and not source_chat:
                        source_chat = msg_e.forward_from_chat.id
                    elif getattr(msg_e, "text", None) and not source_chat:
                        ch, _mid = parse_chat_from_link(msg_e.text)
                        if ch:
                            source_chat = ch

                    if sj["start_id"] > sj["end_id"]:
                        sj["start_id"], sj["end_id"] = sj["end_id"], sj["start_id"]

                    if isinstance(source_chat, str):
                        try:
                            source_chat = (await client.get_chat(source_chat)).id
                        except Exception:
                            pass

                    if not source_chat:
                        await client.send_message(
                            user_id,
                            "❌ Could not detect source channel ID. Please forward an episode message from the source channel."
                        )
                        continue
                    sj["source"] = source_chat
                    step = 5
                except Exception:
                    await client.send_message(user_id, "❌ Invalid last message. Please forward an episode or send a valid link.")
                continue

            # ── STEP 5: LANGUAGE ──────────────────────────────────────────────
            elif step == 5:
                kb_lang = ReplyKeyboardMarkup(
                    [["Hindi", "English", "Hinglish"], ["Telugu", "Tamil", "Marathi"], ["↩️ Undo", "⛔ Cancel"]],
                    resize_keyboard=True
                )
                msg_lang = await native_ask(
                    client,
                    user_id,
                    "<b>❪ STEP 5: LANGUAGE ❫</b>\n\nSelect or type the language of this story:",
                    reply_markup=kb_lang
                )
                txt = (getattr(msg_lang, "text", "") or "").strip()
                if txt in ("↩️ Undo", "Undo"):
                    step = 4
                    continue
                if txt in ("⛔ Cancel", "Cancel", "Cᴀɴᴄᴇʟ"):
                    return await client.send_message(user_id, "<i>Process Cancelled!</i>", reply_markup=ReplyKeyboardRemove())

                sj["language"] = txt or "Hindi"
                step = 6
                continue

            # ── STEP 6: STORY NAME ────────────────────────────────────────────
            elif step == 6:
                msg_name = await native_ask(
                    client,
                    user_id,
                    f"<b>❪ STEP 6: STORY NAME ❫</b>\n\nEnter the story name:\n<i>(Language: <b>{sj.get('language', 'Hindi')}</b>)</i>",
                    reply_markup=undo_cancel_kb
                )
                txt = (getattr(msg_name, "text", "") or "").strip()
                if txt in ("↩️ Undo", "Undo"):
                    step = 5
                    continue
                if txt in ("⛔ Cancel", "Cancel", "Cᴀɴᴄᴇʟ"):
                    return await client.send_message(user_id, "<i>Process Cancelled!</i>", reply_markup=ReplyKeyboardRemove())

                name_input = txt
                lang_sel = (sj.get("language") or "").lower()

                # For Telugu: Do NOT transliterate or translate into Hindi! Keep original
                if lang_sel == "telugu":
                    sj["story_name_en"] = name_input
                    sj["story_name_hi"] = name_input
                    sj["story_name_te"] = name_input
                    step = 7
                    continue

                # For Hindi / English / Hinglish: Transliterate with Groq AI
                waiting_msg = await client.send_message(user_id, "⏳ <i>Processing name with Groq AI...</i>")
                sj["story_name_en"] = utils.translate_to_english(name_input)
                sj["story_name_hi"] = await utils.groq_transliterate_hindi(name_input)
                try: await waiting_msg.delete()
                except Exception: pass

                # Quick Hindi name confirmation
                confirm_kb = ReplyKeyboardMarkup(
                    [["✅ Correct, Continue"], ["✏️ Type Correct Hindi Name"], ["↩️ Undo", "⛔ Cancel"]],
                    resize_keyboard=True
                )
                msg_hi_conf = await native_ask(
                    client, user_id,
                    f"<b>❪ STEP 6.1: HINDI NAME CONFIRM ❫</b>\n\n"
                    f"<b>EN:</b> {sj['story_name_en']}\n"
                    f"<b>HI (Auto):</b> {sj['story_name_hi']}\n\n"
                    f"Is the Hindi name correct?",
                    reply_markup=confirm_kb
                )
                conf_txt = (getattr(msg_hi_conf, "text", "") or "").strip()
                if conf_txt in ("↩️ Undo", "Undo"):
                    step = 6
                    continue
                if conf_txt in ("⛔ Cancel", "Cancel", "Cᴀɴᴄᴇʟ"):
                    return await client.send_message(user_id, "<i>Process Cancelled!</i>", reply_markup=ReplyKeyboardRemove())

                if "✏️" in conf_txt or "Type" in conf_txt:
                    msg_hi_manual = await native_ask(
                        client, user_id,
                        "<b>✏️ Enter the correct Hindi name:</b>",
                        reply_markup=undo_cancel_kb
                    )
                    man_txt = (getattr(msg_hi_manual, "text", "") or "").strip()
                    if man_txt in ("↩️ Undo", "Undo"):
                        step = 6
                        continue
                    if man_txt in ("⛔ Cancel", "Cancel", "Cᴀɴᴄᴇʟ"):
                        return await client.send_message(user_id, "<i>Process Cancelled!</i>", reply_markup=ReplyKeyboardRemove())
                    if man_txt:
                        sj["story_name_hi"] = man_txt
                step = 7
                continue

            # ── STEP 7: STORY IMAGE ───────────────────────────────────────────
            elif step == 7:
                img_kb = ReplyKeyboardMarkup([["⏩ Skip Image"], ["↩️ Undo", "⛔ Cancel"]], resize_keyboard=True)
                msg_img = await native_ask(
                    client,
                    user_id,
                    f"<b>❪ STEP 7: STORY IMAGE ❫</b>\n\n<b>EN:</b> {sj.get('story_name_en')}\n<b>HI:</b> {sj.get('story_name_hi')}\n\nSend the cover image for this story (or tap Skip):",
                    reply_markup=img_kb
                )
                txt = (getattr(msg_img, "text", "") or "").strip()
                if txt in ("↩️ Undo", "Undo"):
                    step = 6
                    continue
                if txt in ("⛔ Cancel", "Cancel", "Cᴀɴᴄᴇʟ"):
                    return await client.send_message(user_id, "<i>Process Cancelled!</i>", reply_markup=ReplyKeyboardRemove())

                if getattr(msg_img, "photo", None):
                    await client.send_message(user_id, "<i>Uploading image to Cloudflare R2 & store bot...</i>")
                    try:
                        from plugins.userbot.market_seller import market_clients
                        from r2_helper import upload_image_to_r2
                        dl = await client.download_media(msg_img.photo.file_id)

                        clean_name = sj.get("story_name_en") or sj.get("story_name_hi") or "story"
                        r2_url = await upload_image_to_r2(dl, clean_title=clean_name)
                        if r2_url:
                            sj["poster_url"] = r2_url
                            sj["banner_url"] = r2_url
                            sj["image_url"] = r2_url
                            sj["image"] = r2_url
                            sj["cover"] = r2_url
                        else:
                            from utils import upload_to_catbox
                            catbox_url = await upload_to_catbox(dl)
                            if catbox_url:
                                sj["poster_url"] = catbox_url
                                sj["banner_url"] = catbox_url
                                sj["image_url"] = catbox_url
                                sj["image"] = catbox_url
                                sj["cover"] = catbox_url

                        store_cli = market_clients.get(str(sj["bot_id"]))
                        if store_cli and not sj.get("poster_url"):
                            try:
                                ul = await store_cli.send_photo(user_id, photo=dl)
                                sj["image"] = ul.photo.file_id
                            except Exception:
                                pass

                        try:
                            import os; os.remove(dl)
                        except Exception: pass
                    except Exception as e:
                        logger.warning(f"[AddStory] Image upload error: {e}")
                        sj["image"] = msg_img.photo.file_id
                else:
                    sj["image"] = None
                step = 8
                continue

            # ── STEP 8: STORY DESCRIPTION ─────────────────────────────────────
            elif step == 8:
                desc_kb = ReplyKeyboardMarkup([["⏩ Skip Description"], ["↩️ Undo", "⛔ Cancel"]], resize_keyboard=True)
                msg_desc = await native_ask(
                    client,
                    user_id,
                    "<b>❪ STEP 8: STORY DESCRIPTION ❫</b>\n\nEnter the description/synopsis of the story (or tap Skip):",
                    reply_markup=desc_kb
                )
                txt = (getattr(msg_desc, "text", "") or "").strip()
                if txt in ("↩️ Undo", "Undo"):
                    step = 7
                    continue
                if txt in ("⛔ Cancel", "Cancel", "Cᴀɴᴄᴇʟ"):
                    return await client.send_message(user_id, "<i>Process Cancelled!</i>", reply_markup=ReplyKeyboardRemove())

                if "Skip" in txt:
                    sj["description"] = "None"
                    sj["description_hi"] = "None"
                else:
                    desc_input = txt
                    lang_sel = (sj.get("language") or "").lower()
                    if lang_sel == "telugu":
                        sj["description"] = desc_input
                        sj["description_hi"] = desc_input
                    else:
                        waiting_msg = await client.send_message(user_id, "⏳ <i>Translating description with Groq AI...</i>")
                        sj["description"] = utils.translate_to_english(desc_input)
                        sj["description_hi"] = await utils.groq_translate_description(desc_input, target_lang="hi")
                        try: await waiting_msg.delete()
                        except Exception: pass
                step = 9
                continue

            # ── STEP 9: EPISODES ──────────────────────────────────────────────
            elif step == 9:
                msg_eps = await native_ask(
                    client,
                    user_id,
                    "<b>❪ STEP 9: EPISODES ❫</b>\n\nHow many episodes? e.g. '595 / 595' or '100+':",
                    reply_markup=undo_cancel_kb
                )
                txt = (getattr(msg_eps, "text", "") or "").strip()
                if txt in ("↩️ Undo", "Undo"):
                    step = 8
                    continue
                if txt in ("⛔ Cancel", "Cancel", "Cᴀɴᴄᴇʟ"):
                    return await client.send_message(user_id, "<i>Process Cancelled!</i>", reply_markup=ReplyKeyboardRemove())

                sj["episodes"] = txt or "N/A"
                step = 10
                continue

            # ── STEP 10: STATUS ───────────────────────────────────────────────
            elif step == 10:
                kb_status = ReplyKeyboardMarkup([["Completed", "Ongoing"], ["↩️ Undo", "⛔ Cancel"]], resize_keyboard=True)
                msg_status = await native_ask(
                    client,
                    user_id,
                    "<b>❪ STEP 10: STATUS ❫</b>\n\nIs the story Completed or Ongoing?",
                    reply_markup=kb_status
                )
                txt = (getattr(msg_status, "text", "") or "").strip()
                if txt in ("↩️ Undo", "Undo"):
                    step = 9
                    continue
                if txt in ("⛔ Cancel", "Cancel", "Cᴀɴᴄᴇʟ"):
                    return await client.send_message(user_id, "<i>Process Cancelled!</i>", reply_markup=ReplyKeyboardRemove())

                sj["status"] = txt or "Completed"
                step = 11
                continue

            # ── STEP 11: GENRE ────────────────────────────────────────────────
            elif step == 11:
                kb_genre = ReplyKeyboardMarkup(
                    [
                        ["Drama", "Fantasy"],
                        ["Romance", "Horror"],
                        ["Suspense & Thriller", "Rebirth"],
                        ["Suspense", "Other"],
                        ["↩️ Undo", "⛔ Cancel"]
                    ],
                    resize_keyboard=True
                )
                msg_genre = await native_ask(
                    client,
                    user_id,
                    "<b>❪ STEP 11: GENRE ❫</b>\n\nSelect a genre from the options or choose Other:",
                    reply_markup=kb_genre
                )
                txt = (getattr(msg_genre, "text", "") or "").strip()
                if txt in ("↩️ Undo", "Undo"):
                    step = 10
                    continue
                if txt in ("⛔ Cancel", "Cancel", "Cᴀɴᴄᴇʟ"):
                    return await client.send_message(user_id, "<i>Process Cancelled!</i>", reply_markup=ReplyKeyboardRemove())

                if txt == "Other":
                    msg_custom_genre = await native_ask(
                        client,
                        user_id,
                        "<b>Enter custom genre name (e.g. Action, Comedy, Sci-Fi):</b>",
                        reply_markup=undo_cancel_kb
                    )
                    c_txt = (getattr(msg_custom_genre, "text", "") or "").strip()
                    if c_txt in ("↩️ Undo", "Undo"):
                        step = 11
                        continue
                    if c_txt in ("⛔ Cancel", "Cancel", "Cᴀɴᴄᴇʟ"):
                        return await client.send_message(user_id, "<i>Process Cancelled!</i>", reply_markup=ReplyKeyboardRemove())
                    sj["genre"] = c_txt or "Other"
                else:
                    sj["genre"] = txt or "Drama"
                step = 12
                continue

            # ── STEP 12: PRICE IN INR ─────────────────────────────────────────
            elif step == 12:
                msg_price = await native_ask(
                    client,
                    user_id,
                    "<b>❪ STEP 12: PRICE IN INR ❫</b>\n\nEnter the price (e.g. <code>100</code>):",
                    reply_markup=undo_cancel_kb
                )
                txt = (getattr(msg_price, "text", "") or "").strip()
                if txt in ("↩️ Undo", "Undo"):
                    step = 11
                    continue
                if txt in ("⛔ Cancel", "Cancel", "Cᴀɴᴄᴇʟ"):
                    return await client.send_message(user_id, "<i>Process Cancelled!</i>", reply_markup=ReplyKeyboardRemove())

                try:
                    price = int(txt)
                    if price < 1:
                        raise ValueError("price")
                    sj["price"] = price
                    step = 13
                except Exception:
                    await client.send_message(user_id, "❌ Price must be a positive integer number.")
                continue

            # ── STEP 13: PLATFORM ─────────────────────────────────────────────
            elif step == 13:
                pf_kb = ReplyKeyboardMarkup(
                    [
                        ["Pocket FM", "Eight FM"],
                        ["Kuku FM", "Kuku TV"],
                        ["Pratilipi FM", "Headfone"],
                        ["Story TV", "Custom"],
                        ["↩️ Undo", "⛔ Cancel"],
                    ],
                    resize_keyboard=True
                )
                msg_plat = await native_ask(
                    client,
                    user_id,
                    "<b>❪ STEP 13: PLATFORM ❫</b>\n\nSelect the platform this story is from:",
                    reply_markup=pf_kb
                )
                txt = (getattr(msg_plat, "text", "") or "").strip()
                if txt in ("↩️ Undo", "Undo"):
                    step = 12
                    continue
                if txt in ("⛔ Cancel", "Cancel", "Cᴀɴᴄᴇʟ"):
                    return await client.send_message(user_id, "<i>Process Cancelled!</i>", reply_markup=ReplyKeyboardRemove())

                if txt == "Custom":
                    msg_custom_plat = await native_ask(
                        client,
                        user_id,
                        "<b>❪ CUSTOM PLATFORM ❫</b>\n\nType the platform name (e.g. Audible, Spotify, etc.):",
                        reply_markup=undo_cancel_kb
                    )
                    c_txt = (getattr(msg_custom_plat, "text", "") or "").strip()
                    if c_txt in ("↩️ Undo", "Undo"):
                        step = 13
                        continue
                    if c_txt in ("⛔ Cancel", "Cancel", "Cᴀɴᴄᴇʟ"):
                        return await client.send_message(user_id, "<i>Process Cancelled!</i>", reply_markup=ReplyKeyboardRemove())
                    sj["platform"] = c_txt or "Custom"
                else:
                    sj["platform"] = txt or "Pocket FM"
                step = 14
                continue

            # ── STEP 14: DELIVERY MODE ────────────────────────────────────────
            elif step == 14:
                delivery_channels = await db.db.premium_channels.find({"type": "delivery"}).to_list(length=300)
                kb_mode = ReplyKeyboardMarkup(
                    [["Use GLOBAL Pool (Auto-Rotate)"], ["Single Delivery Channel"], ["DM Only"], ["↩️ Undo", "⛔ Cancel"]],
                    resize_keyboard=True
                )
                msg_mode = await native_ask(
                    client,
                    user_id,
                    "<b>❪ STEP 14: DELIVERY MODE ❫</b>\n\nChoose how buyers will receive the one-time channel link:",
                    reply_markup=kb_mode,
                )
                txt = (getattr(msg_mode, "text", "") or "").strip()
                if txt in ("↩️ Undo", "Undo"):
                    step = 13
                    continue
                if txt in ("⛔ Cancel", "Cancel", "Cᴀɴᴄᴇʟ"):
                    return await client.send_message(user_id, "<i>Process Cancelled!</i>", reply_markup=ReplyKeyboardRemove())

                if txt == "DM Only":
                    sj["delivery_mode"] = "dm_only"
                    sj["channel_id"] = None
                elif txt == "Single Delivery Channel":
                    sj["delivery_mode"] = "single"
                    if delivery_channels:
                        ask = await native_ask(
                            client,
                            user_id,
                            "<b>❪ DELIVERY CHANNEL PICKER ❫</b>\n\nSend a delivery channel ID, or type part of its saved name to search:",
                            reply_markup=undo_cancel_kb,
                        )
                        a_txt = (getattr(ask, "text", "") or "").strip()
                        if a_txt in ("↩️ Undo", "Undo"):
                            step = 14
                            continue
                        if a_txt in ("⛔ Cancel", "Cancel", "Cᴀɴᴄᴇʟ"):
                            return await client.send_message(user_id, "<i>Process Cancelled!</i>", reply_markup=ReplyKeyboardRemove())

                        chosen = None
                        if a_txt.lstrip("-").isdigit():
                            chosen = int(a_txt)
                        else:
                            ql = a_txt.lower()
                            matches = [ch for ch in delivery_channels if ql in (ch.get("name") or "").lower()]
                            if matches:
                                chosen = matches[0]["channel_id"]
                        sj["channel_id"] = chosen
                    else:
                        custom = await native_ask(
                            client,
                            user_id,
                            "<b>❪ CUSTOM DELIVERY CHANNEL ❫</b>\n\nForward any message from the delivery channel or send its numeric chat ID:",
                            reply_markup=undo_cancel_kb,
                        )
                        c_txt = (getattr(custom, "text", "") or "").strip()
                        if c_txt in ("↩️ Undo", "Undo"):
                            step = 14
                            continue
                        if c_txt in ("⛔ Cancel", "Cancel", "Cᴀɴᴄᴇʟ"):
                            return await client.send_message(user_id, "<i>Process Cancelled!</i>", reply_markup=ReplyKeyboardRemove())
                        try:
                            sj["channel_id"] = custom.forward_from_chat.id if getattr(custom, "forward_from_chat", None) else int(c_txt)
                        except Exception:
                            sj["channel_id"] = None
                else:
                    sj["delivery_mode"] = "pool"
                    sj["channel_pool"] = [c["channel_id"] for c in delivery_channels] if delivery_channels else []

                from datetime import datetime, timezone
                now_utc = datetime.now(timezone.utc)
                sj.setdefault("created_at", now_utc)
                sj.setdefault("uploaded_at", now_utc)
                sj.setdefault("visibility", "available")
                sj.setdefault("status", sj.get("status") or "Completed")

                # Default payment methods
                sj.setdefault("payment_methods", ["upi", "razorpay", "cashfree"])
                sj.setdefault("forwarding_enabled", True)

                # Save Story to DB
                result = await db.db.premium_stories.insert_one(sj)
                story_id = str(result.inserted_id)
                await db.db.premium_stories.update_one(
                    {"_id": result.inserted_id},
                    {"$set": {
                        "story_id": story_id,
                        "visibility": "available",
                        "created_at": now_utc,
                        "uploaded_at": now_utc
                    }}
                )

                try:
                    from utils import scan_and_index_story
                    store_cli_obj = market_clients.get(str(sj.get("bot_id")))
                    asyncio.create_task(scan_and_index_story(store_cli_obj or client, sj, save_to_db=True, db=db))
                except Exception:
                    pass

                bt_doc = await db.db.premium_bots.find_one({"id": int(sj.get("bot_id", 0))}) if sj.get("bot_id") else None
                bt_cfg_val = (bt_doc.get("config") or {}) if bt_doc else {}
                _mini_app_on = bt_cfg_val.get("mini_app_deep_links", None)
                if _mini_app_on is None:
                    _ml_cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
                    _mini_app_on = _ml_cfg.get("mini_app_enabled", True)

                if _mini_app_on:
                    deep_link = f"https://t.me/{sj['bot_username']}/apminibyarya?startapp=story_{story_id}"
                else:
                    deep_link = f"https://t.me/{sj['bot_username']}?start=story_{story_id}"

                await client.send_message(
                    user_id,
                    f"✅ <b>Story successfully added to Storefront!</b>\n\n"
                    f"The Connected bot <code>@{sj['bot_username']}</code> is now actively selling <b>{sj['story_name_en']}</b> for ₹{sj['price']}!\n\n"
                    f"🔗 <b>Direct Purchase Link:</b>\n<code>{deep_link}</code>",
                    reply_markup=ReplyKeyboardRemove(),
                    parse_mode=enums.ParseMode.HTML
                )

                # Broadcast alert to opted-in users
                store_cli = market_clients.get(str(sj["bot_id"]))
                if store_cli:
                    async def _send_sub_alert():
                        all_users = await db.db.users.find(
                            {"alerts_subscribed": True},
                            {"id": 1, "lang": 1}
                        ).to_list(length=None)

                        story_name = sj.get("story_name_en", "Unknown")
                        story_name_hi = sj.get("story_name_hi", story_name)
                        price = sj.get("price", 0)
                        image = sj.get("image")

                        caption_en = (
                            f"<b>New Story Available</b>\n\n"
                            f"<b>{story_name}</b>\n"
                            f"<i>Price: ₹{price}</i>\n\n"
                            f"<a href='{deep_link}'>Tap to View & Purchase</a>"
                        )
                        caption_hi = (
                            f"<b>नई कहानी उपलब्ध है</b>\n\n"
                            f"<b>{story_name_hi}</b>\n"
                            f"<i>कीमत: ₹{price}</i>\n\n"
                            f"<a href='{deep_link}'>देखने और खरीदने के लिए टैप करें</a>"
                        )

                        for u in all_users:
                            uid = u.get("id")
                            if not uid: continue
                            cap = caption_hi if u.get("lang") == "hi" else caption_en
                            try:
                                if image:
                                    await store_cli.send_photo(uid, photo=image, caption=cap)
                                else:
                                    await store_cli.send_message(uid, cap)
                                await asyncio.sleep(0.07)
                            except Exception:
                                pass

                    asyncio.create_task(_send_sub_alert())
                return
    except Exception as e:
        logger.error(f"Error in _add_story_flow: {e}")
        await client.send_message(user_id, f"❌ An error occurred during story creation: {e}", reply_markup=ReplyKeyboardRemove())


async def _edit_story_flow(client, user_id, s_id, action):
    from bson.objectid import ObjectId
    s_id_obj = ObjectId(s_id)
    story = await db.db.premium_stories.find_one({"_id": s_id_obj})
    if not story: return await client.send_message(user_id, "Story missing.")
    
    label_map = {
        "name": "Name (EN)", "namehi": "Name (HI)", "price": "Price",
        "image": "Cover Image", "desc": "Description", "status": "Status",
        "genre": "Genre", "episodes": "Episodes", "eps": "DB Range"
    }
    label = label_map.get(action, action.capitalize())
    sname = story.get('story_name_en', 'Unknown')
    prompt_txt = (
        f"<b>⟦ Edit Story ⟧</b>\n\n"
        f"<blockquote><b>Story:</b> {sname}\n<b>Field:</b> {label}</blockquote>\n\n"
        f"<i>Send the new value for <b>{label}</b>. Type /cancel to abort.</i>"
    )
    if action == "status":
        prompt_txt += "\n\n💡 <i>Suggested Status values: <code>Ongoing</code>, <code>Completed</code>, <code>Unfinished</code>, <code>Stucked</code></i>"
    msg = await native_ask(client, user_id, prompt_txt, reply_markup=ReplyKeyboardMarkup([["⛔ Cancel"]], resize_keyboard=True), parse_mode=enums.ParseMode.HTML)
    from pyrogram.types import CallbackQuery as _CQ
    _txt = getattr(msg, 'text', '') or getattr(msg, 'caption', '') or ''
    is_media = getattr(msg, 'photo', None) is not None
    if isinstance(msg, _CQ) or "Cancel" in _txt or (not _txt and not is_media):
        back_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"« Back to Story", callback_data=f"mk#st_view_{s_id}")],
            [InlineKeyboardButton("« Story List", callback_data="mk#ms_list_0")]
        ])
        return await client.send_message(user_id, "<i>Cancelled.</i>", reply_markup=back_kb, parse_mode=enums.ParseMode.HTML)

    try:
        if action == "price":
            try:
                new_price = int(msg.text)
            except ValueError:
                back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Story", callback_data=f"mk#st_view_{s_id}")]])
                return await client.send_message(user_id, "❌ Valid integer price required.", reply_markup=back_kb, parse_mode=enums.ParseMode.HTML)
                
            old_price = int(story.get("price", 0))
            await db.db.premium_stories.update_one({"_id": s_id_obj}, {"$set": {"price": new_price}})
            
            if old_price > 0 and old_price != new_price:
                diff_type = "Drop 📉" if new_price < old_price else "Hike 📈"
                kb_notify = ReplyKeyboardMarkup([["Yes, Send Notification"], ["No"]], resize_keyboard=True, one_time_keyboard=True)
                ask_notif = await native_ask(
                    client, user_id, 
                    f"<b>📢 Send Price {diff_type} Notification?</b>\n\n"
                    f"Old Price: ₹{old_price}\n"
                    f"New Price: ₹{new_price}\n\n"
                    "Do you want to send a broadcast to all buyers and users about this change?", 
                    reply_markup=kb_notify
                )
                
                if getattr(ask_notif, 'text', None) and "Yes" in ask_notif.text:
                    await client.send_message(user_id, "<i>Broadcasting price update...</i>", reply_markup=ReplyKeyboardRemove())
                    try:
                        from plugins.userbot.market_seller import market_clients
                        store_cli = market_clients.get(str(story.get("bot_id")))
                        if store_cli:
                            bot_users = await db.db.users.find({}, {"id": 1}).to_list(length=None)
                            story_name = story.get('story_name_en', 'Premium Story')
                            genre = story.get('genre', 'N/A')
                            plat = story.get('platform', 'N/A')
                            episodes = story.get('episodes', 'N/A')
                            stt = story.get('status', 'N/A').upper()
                            
                            trend_icon = "📈" if new_price > old_price else "📉"
                            trend_text = "HIKE" if new_price > old_price else "DROP"
                            
                            # Premium UI for Price Broadcast
                            msg_text = (
                                f"<b>╔══════════════════════╗</b>\n"
                                f"<b>        𝗣𝗥𝗜𝗖𝗘 𝗨𝗣𝗗𝗔𝗧𝗘</b>\n"
                                f"<b>╚══════════════════════╝</b>\n\n"
                                f"<b>⧉ 𝗦𝗧𝗢𝗥𝗬 𝗗𝗘𝗧𝗔𝗜𝗟𝗦</b>\n"
                                f"<b>• Name     ⟶</b> {story_name}\n"
                                f"<b>• Genre    ⟶</b> {genre}\n"
                                f"<b>• Platform ⟶</b> {plat}\n"
                                f"<b>• Status   ⟶</b> {stt} ({episodes} Eps)\n\n"
                                f"<b>⧉ 𝗣𝗥𝗜𝗖𝗜𝗡𝗚 𝗖𝗛𝗔𝗡𝗚𝗘 [ {trend_icon} {trend_text} ]</b>\n"
                                f"<b>• Old Price ⟶</b> ₹{old_price}\n"
                                f"<b>• New Price ⟶</b> <ins>₹{new_price}</ins>\n\n"
                                f"<i>🛒 Click the button below to buy now at the updated price!</i>"
                            )
                            
                            bt_doc = await db.db.premium_bots.find_one({"id": int(story.get("bot_id", 0))}) if story.get("bot_id") else None
                            bt_cfg_val = (bt_doc.get("config") or {}) if bt_doc else {}
                            _mini_app_on = bt_cfg_val.get("mini_app_deep_links", None)
                            if _mini_app_on is None:
                                _ml_cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
                                _mini_app_on = _ml_cfg.get("mini_app_enabled", True)
                            if _mini_app_on:
                                buy_link = f"https://t.me/{story.get('bot_username')}/apminibyarya?startapp=story_{s_id}"
                            else:
                                buy_link = f"https://t.me/{story.get('bot_username')}?start=story_{s_id}"
                            kb_buy = InlineKeyboardMarkup([[InlineKeyboardButton("🛍️ VIEW & BUY STORY", url=buy_link)]])

                            sent = 0
                            for u in bot_users:
                                uid_int = u.get("id")
                                if uid_int:
                                    try:
                                        await store_cli.send_message(uid_int, msg_text, reply_markup=kb_buy, parse_mode=enums.ParseMode.HTML)
                                        sent += 1
                                        await asyncio.sleep(0.05)
                                    except Exception:
                                        pass
                            await client.send_message(user_id, f"<b>Broadcast complete.</b> Sent to {sent} users.",
                                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to Story", callback_data=f"mk#st_view_{s_id}"), InlineKeyboardButton("Story List", callback_data="mk#ms_list_0")]]),
                                parse_mode=enums.ParseMode.HTML)
                        else:
                            await client.send_message(user_id, "Store bot is offline.",
                                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to Story", callback_data=f"mk#st_view_{s_id}")]]),
                                parse_mode=enums.ParseMode.HTML)
                    except Exception as e:
                        logger.error(f"Broadcast error: {e}")
                        await client.send_message(user_id, "Error during broadcast.",
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to Story", callback_data=f"mk#st_view_{s_id}")]]),
                            parse_mode=enums.ParseMode.HTML)
                else:
                    await client.send_message(user_id, f"Price updated to \u20b9{new_price}. (No broadcast)",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to Story", callback_data=f"mk#st_view_{s_id}"), InlineKeyboardButton("Story List", callback_data="mk#ms_list_0")]]),
                        parse_mode=enums.ParseMode.HTML)
                return
        elif action == "name":
            name_en = utils.translate_to_english(msg.text)
            name_hi = utils.transliterate_to_hindi(msg.text)
            await db.db.premium_stories.update_one({"_id": s_id_obj}, {"$set": {"story_name_en": name_en, "story_name_hi": name_hi}})
        elif action == "namehi":
            # Direct update — admin typed the Hindi name manually
            await db.db.premium_stories.update_one({"_id": s_id_obj}, {"$set": {"story_name_hi": msg.text.strip()}})
        elif action == "namete":
            # Direct update — admin typed the Telugu name manually
            await db.db.premium_stories.update_one({"_id": s_id_obj}, {"$set": {"story_name_te": msg.text.strip()}})
        elif action == "descte":
            # Direct update — admin typed the Telugu description manually
            await db.db.premium_stories.update_one({"_id": s_id_obj}, {"$set": {"description_te": msg.text.strip()}})
        elif action == "image":
            if getattr(msg, 'photo', None):
                await client.send_message(user_id, "<i>Uploading image to store bot and CDN...</i>")
                try:
                    from plugins.userbot.market_seller import market_clients
                    from utils import upload_to_catbox
                    store_cli = market_clients.get(str(story.get("bot_id")))
                    dl = await client.download_media(msg.photo.file_id)
                    
                    catbox_url = await upload_to_catbox(dl)
                    updates = {}
                    if catbox_url:
                        updates["poster_url"] = catbox_url
                        updates["image_url"] = catbox_url
                        
                    if store_cli:
                        try:
                            ul = await store_cli.send_photo(user_id, photo=dl)
                            updates["image"] = ul.photo.file_id
                            updates["poster"] = ul.photo.file_id
                            updates["banner"] = ul.photo.file_id
                        except Exception:
                            updates["image"] = msg.photo.file_id
                            updates["poster"] = msg.photo.file_id
                            updates["banner"] = msg.photo.file_id
                    else:
                        updates["image"] = msg.photo.file_id
                        updates["poster"] = msg.photo.file_id
                        updates["banner"] = msg.photo.file_id
                    
                    await db.db.premium_stories.update_one({"_id": s_id_obj}, {"$set": updates})
                    try:
                        import os; os.remove(dl)
                    except Exception:
                        pass
                except Exception as e:
                    await db.db.premium_stories.update_one({"_id": s_id_obj}, {"$set": {
                        "image": msg.photo.file_id,
                        "poster": msg.photo.file_id,
                        "banner": msg.photo.file_id
                    }})
            else:
                back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Story", callback_data=f"mk#st_view_{s_id}")]])
                return await client.send_message(user_id, "❌ A valid photo is required.", reply_markup=back_kb, parse_mode=enums.ParseMode.HTML)
        elif action == "desc":
            desc_en = utils.translate_to_english(msg.text)
            desc_hi = utils.smart_translate_meaning(msg.text)
            await db.db.premium_stories.update_one({"_id": s_id_obj}, {"$set": {"description": desc_en, "description_hi": desc_hi}})
        elif action == "genre":
            await db.db.premium_stories.update_one({"_id": s_id_obj}, {"$set": {"genre": msg.text}})
        elif action == "episodes":
            await db.db.premium_stories.update_one({"_id": s_id_obj}, {"$set": {"episodes": msg.text}})
        elif action == "status":
            val = (msg.text or "").strip()
            lval = val.lower()
            if lval == "ongoing":
                val = "Ongoing"
            elif lval == "completed":
                val = "Completed"
            elif lval == "unfinished":
                val = "Unfinished"
            elif lval == "stucked":
                val = "Stucked"
            else:
                back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Story", callback_data=f"mk#st_view_{s_id}")]])
                return await client.send_message(
                    user_id,
                    "❌ <b>Invalid Status.</b> Status must be one of: <code>Ongoing</code>, <code>Completed</code>, <code>Unfinished</code>, <code>Stucked</code>.",
                    reply_markup=back_kb,
                    parse_mode=enums.ParseMode.HTML
                )
            await db.db.premium_stories.update_one({"_id": s_id_obj}, {"$set": {"status": val}})
        await client.send_message(
            user_id,
            f"✅ <b>{label} updated successfully.</b>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("« Back to Story", callback_data=f"mk#st_view_{s_id}"),
                 InlineKeyboardButton("« Story List", callback_data="mk#ms_list_0")]
            ]),
            parse_mode=enums.ParseMode.HTML
        )
    except Exception as e:
        await client.send_message(
            user_id,
            "❌ Invalid format. Please try again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Story", callback_data=f"mk#st_view_{s_id}")]]),
            parse_mode=enums.ParseMode.HTML
        )

async def _approve_payment_flow(client, user_id, p_id):
    try:
        from bson.objectid import ObjectId
        from datetime import datetime
        checkout = await db.db.premium_checkout.find_one({"_id": ObjectId(p_id)})
        if not checkout: return await client.send_message(user_id, "Ticket not found.")
        
        if checkout.get("status") in ("approved", "rejected"):
            return await client.send_message(user_id, f"Ticket is already {checkout.get('status')}.")
            
        await db.db.premium_checkout.update_one(
            {"_id": ObjectId(p_id)},
            {"$set": {"status": "approved", "updated_at": datetime.utcnow(), "reviewed_by": user_id}}
        )

        order_id = checkout.get("order_id")
        if not order_id or str(order_id).startswith("OD-") or str(order_id).startswith("OD_"):
            try:
                from plugins.userbot.market_seller import _make_arya_bot_order_id
                order_id = await _make_arya_bot_order_id(checkout['user_id'], str(checkout.get('story_id')))
            except Exception:
                from mini_app_api import _make_arya_order_id
                order_id = await _make_arya_order_id(db, str(checkout['user_id']), [str(checkout.get('story_id'))], source="bot")

        story = await db.db.premium_stories.find_one({"_id": checkout.get("story_id")})
        real_amt = checkout.get("amount") or (int(story.get("price", 0)) if story else 0)

        await db.add_purchase(checkout['user_id'], str(checkout.get('story_id')))
        await db.db.premium_purchases.insert_one({
            "user_id": checkout['user_id'],
            "story_id": checkout.get('story_id'),
            "bot_id": checkout.get('bot_id'),
            "purchased_at": datetime.utcnow(),
            "source": checkout.get("method", "upi"),
            "amount": real_amt,
            "order_id": order_id
        })
        try:
            from purchase_dm_helper import send_purchase_success_dm
            asyncio.create_task(send_purchase_success_dm(
                db=db,
                user_id=checkout['user_id'],
                story_ids=[str(checkout.get('story_id'))] if checkout.get('story_id') else [],
                order_id=order_id,
                amount=real_amt,
                payment_method=checkout.get("method", "UPI (UTR)"),
                verified_by="Access Granted By Team",
                is_admin_manual=True
            ))
        except Exception as _dm_err:
            logger.warning(f"Failed to send purchase DM on manual UTR approval: {_dm_err}")

        # Log payment
        from utils import log_payment, log_arya_event
        story = await db.db.premium_stories.find_one({"_id": checkout.get("story_id")})
        user_info = await db.get_user(checkout['user_id'])
        s_name = story.get("story_name_en") if story else "Unknown"
        real_amt = checkout.get("amount") or (int(story.get("price", 0)) if story else 0)
        
        asyncio.create_task(log_arya_event(
            event_type="MANUAL UPI VERIFIED",
            user_id=checkout['user_id'],
            user_info=user_info,
            details=f"Story: {s_name}\nOrder ID: <code>{order_id}</code>\nAmount: ₹{real_amt}\nApproved by Admin ID: {user_id}"
        ))
        
        asyncio.create_task(log_payment(
            user_id=checkout['user_id'],
            user_first_name=user_info.get("first_name", "User"),
            username=user_info.get('username', ''),
            s_name=s_name,
            amount=real_amt,
            method=checkout.get("method", "upi"),
            receipt_id=str(checkout.get("_id", "")),
            photo_path=checkout.get("proof_path"),
            order_id=order_id,
            user_last_name=user_info.get("last_name", "")
        ))

        await client.send_message(user_id, "✅ Payment Approved successfully!")
        from plugins.userbot.market_seller import market_clients, dispatch_delivery_choice
        u_cli = market_clients.get(str(checkout.get('bot_id')))
        if u_cli:
            try: await u_cli.delete_messages(checkout['user_id'], checkout.get('status_msg_id', 0))
            except: pass
            if story:
                asyncio.create_task(dispatch_delivery_choice(u_cli, checkout['user_id'], story))
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        await client.send_message(user_id, f"❌ Failed to approve payment: {e}\n\n<code>{err[-500:]}</code>")
            

async def _reject_payment_flow(client, user_id, p_id):
    try:
        msg = await native_ask(
            client, user_id, 
            f"<b>❌ Reject Payment</b>\n\nPlease enter the reason for rejecting this payment (this will be sent to the user).\n<i>Tip: You can send an image with caption as the reason!</i>", 
            reply_markup=ReplyKeyboardMarkup([["⛔ Cᴀɴᴄᴇʟ"]], resize_keyboard=True)
        )
        from pyrogram.types import CallbackQuery as _CQ
        _rtxt = getattr(msg, 'text', '') or ''
        if isinstance(msg, _CQ) or "Cancel" in _rtxt:
            return await client.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())

        reason = msg.text or getattr(msg, 'caption', None) or "No reason provided."
        photo_id = msg.photo.file_id if getattr(msg, 'photo', None) else None

        from bson.objectid import ObjectId
        checkout = await db.db.premium_checkout.find_one({"_id": ObjectId(p_id)})
        if not checkout: return await client.send_message(user_id, "Ticket not found.")
        
        if checkout.get("status") in ("approved", "rejected"):
            return await client.send_message(user_id, f"Ticket is already {checkout.get('status')}.")
        
        from datetime import datetime
        await db.db.premium_checkout.update_one(
            {"_id": ObjectId(p_id)},
            {"$set": {"status": "rejected", "reject_reason": reason, "updated_at": datetime.utcnow(), "reviewed_by": user_id}}
        )
        await client.send_message(user_id, f"✅ Payment Rejected. User has been notified.", reply_markup=ReplyKeyboardRemove())
        
        from plugins.userbot.market_seller import market_clients
        u_cli = market_clients.get(str(checkout.get('bot_id')))
        if u_cli:
            try: await u_cli.delete_messages(checkout['user_id'], checkout.get('status_msg_id', 0))
            except: pass
            
            u_doc = await db.get_user(checkout.get('user_id'))
            t_lang = u_doc.get("lang", "en")
            if t_lang == "hi":
                user_msg = f"<b>❌ भुगतान अस्वीकृत</b>\n\nआपका हालिया भुगतान सत्यापित नहीं हो सका।\n<b>व्यवस्थापक से कारण:</b>\n{reason}\n\n<i>कृपया सही विवरण/स्क्रीनशॉट के साथ पुनः प्रयास करें।</i>"
            else:
                user_msg = f"<b>❌ Payment Rejected</b>\n\nYour recent payment could not be verified.\n<b>Reason from Admin:</b>\n{reason}\n\n<i>If this is a mistake, please try again with a clear screenshot.</i>"

            if photo_id:
                try:
                    import os
                    dl = await client.download_media(msg)
                    await u_cli.send_photo(checkout['user_id'], photo=dl, caption=user_msg)
                    os.remove(dl)
                except Exception:
                    await u_cli.send_message(checkout['user_id'], user_msg)
            else:
                await u_cli.send_message(checkout['user_id'], user_msg)
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        await client.send_message(user_id, f"❌ Failed to reject payment: {e}\n\n<code>{err[-500:]}</code>")

async def _premium_bot_set(client, user_id, b_id, key, label):
    # Keep this minimal: no reply-keyboard "selection menu" unless absolutely required.
    if key == "logo":
        note = "<i>Upload a Photo to be used as your Bot Logo.</i>"
        header_emoji = "6026089641730382702"
        pretty_title = "Bot Logo (UPI QR)"
    elif key == "delivery_report":
        note = "<b>Available Variables:</b>\n<code>{story_name}</code>\n<code>{sent}</code>\n<code>{failed}</code>\n<code>{time}</code>"
        header_emoji = "6023694913995020551"
        pretty_title = "Delivery Report MSG"
    elif key == "caption":
        note = "<b>Available Variables:</b>\n<code>{story}</code>, <code>{price}</code>, <code>{original_caption}</code>, <code>{file_name}</code>\n<i>Allows standard HTML (e.g. &lt;b&gt;bold&lt;/b&gt;)</i>"
        header_emoji = "6019155812167981039"
        pretty_title = "Custom Caption"
    elif key == "welcome":
        note = "<i>Allows standard HTML syntax formatting.</i>"
        header_emoji = "6041921818896372382"
        pretty_title = "Welcome Message"
    elif key == "about":
        note = "<i>Allows standard HTML syntax formatting.</i>"
        header_emoji = "6041921818896372382"
        pretty_title = "About Text"
    elif key == "quote":
        note = "<i>Allows standard HTML syntax formatting.</i>"
        header_emoji = "6041921818896372382"
        pretty_title = "Quote Text"
    elif key == "quote_author":
        note = "<i>Allows standard HTML syntax formatting.</i>"
        header_emoji = "6041921818896372382"
        pretty_title = "Quote Author"
    elif key == "upi_name":
        note = "<i>Send the Payee Name to display on UPI payments.</i>"
        header_emoji = "6030400221232501136"
        pretty_title = "UPI Payee Name"
    elif key == "upi_redirect":
        note = "<i>Send the HTTPS redirect base URL, e.g. <code>https://aryastoriesupi.vercel.app</code></i>"
        header_emoji = "5312536423156654273"
        pretty_title = "UPI Open-App Link"
    elif key == "fetching_media":
        note = "<i>Send a Photo, GIF, or Video to be shown while fetching files.</i>"
        header_emoji = "6026089641730382702"
        pretty_title = "Fetching Media (GIF/Img)"
    else:
        note = "<i>Allows standard HTML syntax formatting.</i>"
        header_emoji = "6021637109264160908"
        pretty_title = label.replace('_', ' ').title()

    extra_note = ""
    if key in ["welcome", "about", "quote", "quote_author"]:
        extra_note = "Send <code>disable</code> to completely hide this section.\n"

    msg = await native_ask(
        client,
        user_id,
        f'<emoji id="{header_emoji}">✏️</emoji> <b>{pretty_title}</b>\n'
        f'━━━━━━━━━━━━━━━━━━━━━\n\n'
        f"Send the new {pretty_title} for your Store Bot.\n"
        f"{extra_note}"
        f"Tap <b>🔄 Reset to Default</b> or send <code>/reset</code> to revert to default.\n\n"
        f"{note}",
        reply_markup=ReplyKeyboardMarkup([["🔄 Reset to Default"], ["❮ Cancel"]], resize_keyboard=True)
    )
    
    txt = getattr(msg, 'text', "") or ""
    if "Cancel" in txt or "/cancel" in txt:
        back_target = f"mk#bot_view_{b_id}"
        if key in ["upi_redirect", "upi_name", "logo"]:
            back_target = f"mk#p_upi_menu_{b_id}"
        elif key in ["welcome", "about", "quote", "quote_author"]:
            back_target = f"mk#welcome_cfg_{b_id}"
        elif key in ["menu_media"]:
            back_target = f"mk#p_wa_{b_id}"
        back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data=back_target)]])
        return await client.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=back_kb)
    
    if txt == "/reset" or "Reset" in txt or txt.strip().lower() in ("reset", "/reset"):
        await db.db.premium_bots.update_one({"id": int(b_id)}, {"$unset": {f"config.{key}": ""}})
        tmp_rm = await client.send_message(user_id, "...", reply_markup=ReplyKeyboardRemove())
        await tmp_rm.delete()
        back_target = f"mk#bot_view_{b_id}"
        if key in ["upi_redirect", "upi_name", "logo"]:
            back_target = f"mk#p_upi_menu_{b_id}"
        elif key in ["welcome", "about", "quote", "quote_author"]:
            back_target = f"mk#welcome_cfg_{b_id}"
        elif key in ["menu_media"]:
            back_target = f"mk#p_wa_{b_id}"
        back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data=back_target)]])
        return await client.send_message(user_id, f"<i>✅ {pretty_title} has been Reset to default.</i>", reply_markup=back_kb)
        
    val = msg.text or msg.caption or ""
    if key in ["logo", "fetching_media"]:
        media_type = None
        if getattr(msg, "photo", None): media_type = "photo"
        elif getattr(msg, "animation", None): media_type = "animation"
        elif getattr(msg, "video", None): media_type = "video"

        if not media_type:
            return await client.send_message(user_id, f"❌ Valid Photo/GIF/Video required for {pretty_title}.", reply_markup=ReplyKeyboardRemove())

        # Re-upload via the target store bot (file_id is bot-specific).
        from plugins.userbot.market_seller import market_clients
        store_cli = market_clients.get(str(b_id))
        if not store_cli:
            return await client.send_message(
                user_id,
                "❌ Store bot is not running right now.\n\nRestart the ecosystem and try again.",
                reply_markup=ReplyKeyboardRemove()
            )

        import os
        if not os.path.exists("downloads"):
            os.makedirs("downloads")

        ext = ".jpg" if media_type == "photo" else (".gif" if media_type == "animation" else ".mp4")
        tmp_path = await client.download_media(msg, file_name=f"downloads/{key}_{b_id}_{int(time.time())}{ext}")
        sent = None
        try:
            # Upload via store bot to get store-bot-specific file_id; then delete immediately.
            if media_type == "photo":
                sent = await store_cli.send_photo(user_id, photo=tmp_path)
                val = {"type": "photo", "file_id": sent.photo.file_id if sent.photo else sent.document.file_id}
            elif media_type == "animation":
                sent = await store_cli.send_animation(user_id, animation=tmp_path)
                # Fallback in case Telegram sees it as document/video
                fid = (sent.animation.file_id if sent.animation else 
                      (sent.document.file_id if sent.document else (sent.video.file_id if sent.video else None)))
                if not fid:
                    raise AttributeError("Could not retrieve file_id from sent animation.")
                val = {"type": "animation", "file_id": fid}
            else:
                sent = await store_cli.send_video(user_id, video=tmp_path)
                val = {"type": "video", "file_id": sent.video.file_id if sent.video else sent.document.file_id}

        finally:
            try:
                if sent:
                    await store_cli.delete_messages(user_id, sent.id)
            except Exception:
                pass
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    if key == "upi_redirect":
        from plugins.userbot.market_seller import sanitize_https_redirect_base
        val = sanitize_https_redirect_base(val)
        if not val:
            return await client.send_message(
                user_id,
                "❌ Invalid redirect URL.\n\nSend only the HTTPS base, e.g.\n<code>https://aryastoriesupi.vercel.app</code>\n\n(no spaces, no hidden characters after paste).",
                reply_markup=ReplyKeyboardRemove(),
            )
        
    await db.db.premium_bots.update_one({"id": int(b_id)}, {"$set": {f"config.{key}": val}})
    
    tmp_m = await client.send_message(user_id, "...", reply_markup=ReplyKeyboardRemove())
    await tmp_m.delete()

    back_target = f"mk#bot_view_{b_id}"
    if key in ["upi_redirect", "upi_name", "logo"]:
        back_target = f"mk#p_upi_menu_{b_id}"
    elif key in ["welcome", "about", "quote", "quote_author"]:
        back_target = f"mk#welcome_cfg_{b_id}"
    elif key in ["menu_media"]:
        back_target = f"mk#p_wa_{b_id}"
    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data=back_target)]])

    await client.send_message(user_id, f"<i>✅ {pretty_title} successfully updated!</i>", reply_markup=back_kb)


async def _menu_media_add_flow(client, user_id: int, b_id: str):
    bot = await _find_premium_bot(b_id)
    if not bot:
        return await client.send_message(user_id, "❌ Bot not found.", reply_markup=ReplyKeyboardRemove())

    cfg = bot.get("config", {}) or {}
    items = _cfg_list(cfg, "menu_media")
    if len([x for x in items if isinstance(x, dict)]) >= 30:
        return await client.send_message(user_id, "⚠️ Max 30 menu media items reached. Delete one first.", reply_markup=ReplyKeyboardRemove())

    msg = await native_ask(
        client,
        user_id,
        "<b>➕ Add Menu Media</b>\n\nSend a <b>Photo</b>, <b>GIF</b>, or <b>Video</b>.\n\n<i>This will be shown randomly to users in the main menu.</i>",
        reply_markup=ReplyKeyboardRemove(),
    )

    media_type = None
    if getattr(msg, "photo", None):
        media_type = "photo"
    elif getattr(msg, "animation", None):
        media_type = "animation"
    elif getattr(msg, "video", None):
        media_type = "video"
    else:
        return await client.send_message(user_id, "❌ Please send a valid Photo / GIF / Video.", reply_markup=ReplyKeyboardRemove())

    from plugins.userbot.market_seller import market_clients
    store_cli = market_clients.get(str(b_id))
    if not store_cli:
        return await client.send_message(
            user_id,
            "❌ Store bot is not running right now.\n\nRestart the ecosystem and try again.",
            reply_markup=ReplyKeyboardRemove()
        )

    import os
    if not os.path.exists("downloads"):
        os.makedirs("downloads")

    ext = ".jpg" if media_type == "photo" else (".gif" if media_type == "animation" else ".mp4")
    tmp_path = await client.download_media(msg, file_name=f"downloads/menu_media_{b_id}_{int(time.time())}{ext}")
    sent = None
    try:
        # Upload via store bot to get store-bot-specific file_id; then delete immediately (no "set" message).
        if media_type == "photo":
            sent = await store_cli.send_photo(user_id, photo=tmp_path)
            file_id = getattr(sent.photo, "file_id", None) or ""
        elif media_type == "animation":
            sent = await store_cli.send_animation(user_id, animation=tmp_path)
            file_id = getattr(sent.animation, "file_id", None) or ""
        else:
            sent = await store_cli.send_video(user_id, video=tmp_path)
            file_id = getattr(sent.video, "file_id", None) or ""
    finally:
        try:
            if sent:
                await store_cli.delete_messages(user_id, sent.id)
        except Exception:
            pass
        try:
            os.remove(tmp_path)
        except Exception:
            pass

    if not file_id:
        return await client.send_message(user_id, "❌ Failed to capture media file_id. Try again.", reply_markup=ReplyKeyboardRemove())

    items = [x for x in items if isinstance(x, dict)]
    items.append({"type": media_type, "file_id": file_id})
    await db.db.premium_bots.update_one({"id": int(b_id)}, {"$set": {"config.menu_media": items}, "$unset": {"config.menuimg": ""}})

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👁 Preview", callback_data=f"mk#menu_media_prev_{b_id}_{len(items)}")],
        [InlineKeyboardButton("« Back", callback_data=f"mk#menu_media_{b_id}")]
    ])
    await client.send_message(user_id, "✅ Menu media added.", reply_markup=kb)


async def _menu_media_preview_flow(client, user_id: int, b_id: str, idx: int):
    bot = await _find_premium_bot(b_id)
    if not bot:
        return await client.send_message(user_id, "❌ Bot not found.", reply_markup=ReplyKeyboardRemove())
    cfg = bot.get("config", {}) or {}
    items = [x for x in _cfg_list(cfg, "menu_media") if isinstance(x, dict)]
    if idx < 1 or idx > len(items):
        return await client.send_message(user_id, "❌ Invalid item index.", reply_markup=ReplyKeyboardRemove())

    it = items[idx - 1]
    t = it.get("type")
    fid = it.get("file_id")
    txt = f"<b>Preview</b>\n\n<b>Item:</b> {idx}/{len(items)}\n<b>Type:</b> <code>{t}</code>"

    nav = []
    if idx > 1:
        nav.append(InlineKeyboardButton("⬅ Prev", callback_data=f"mk#menu_media_prev_{b_id}_{idx-1}"))
    if idx < len(items):
        nav.append(InlineKeyboardButton("Next ➡", callback_data=f"mk#menu_media_prev_{b_id}_{idx+1}"))

    kb_rows = []
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton("🗑 Delete this", callback_data=f"mk#menu_media_del_{b_id}_{idx}")])
    kb_rows.append([InlineKeyboardButton("« Back", callback_data=f"mk#menu_media_{b_id}")])
    kb = InlineKeyboardMarkup(kb_rows)

    try:
        if t == "photo":
            await client.send_photo(user_id, photo=fid, caption=txt, reply_markup=kb)
        elif t == "animation":
            await client.send_animation(user_id, animation=fid, caption=txt, reply_markup=kb)
        else:
            await client.send_video(user_id, video=fid, caption=txt, reply_markup=kb)
    except Exception:
        await client.send_message(user_id, txt + "\n\n⚠️ Failed to preview this media.", reply_markup=kb)


async def _add_channel_flow(client, user_id, ch_type):
    """Wizard to add a DB or Delivery channel by forwarding a message from it."""
    label = "DB Source" if ch_type == "db" else "Delivery"
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="ask_cancel")]])

    msg = await native_ask(
        client, user_id,
        f"<b>❪ ADD {label.upper()} CHANNEL ❫</b>\n\n"
        f"Forward <b>any message</b> from the {'source story' if ch_type == 'db' else 'private delivery'} channel, "
        f"OR type its Chat ID directly (e.g. <code>-100123456789</code>):",
        reply_markup=cancel_kb
    )
    if not msg or (getattr(msg, 'text', None) and "Cᴀɴᴄᴇʟ" in msg.text):
        return await client.send_message(user_id, "<i>Process Cancelled Successfully!</i>")

    try:
        if getattr(msg, 'forward_from_chat', None):
            cid = msg.forward_from_chat.id
            name = msg.forward_from_chat.title or str(cid)
        else:
            cid = int(msg.text.strip())
            # Try to get info
            try:
                chat = await client.get_chat(cid)
                name = chat.title or str(cid)
            except Exception:
                name = str(cid)

        # Check for duplicates
        existing = await db.db.premium_channels.find_one({"channel_id": cid, "type": ch_type})
        if existing:
            return await client.send_message(user_id, f"⚠️ This channel is already in the {label} list.", reply_markup=ReplyKeyboardRemove())

        await db.db.premium_channels.insert_one({"channel_id": cid, "name": name, "type": ch_type})
        await client.send_message(user_id, f"✅ <b>{label} Channel Added!</b>\n\n<b>Name:</b> {name}\n<b>ID:</b> <code>{cid}</code>", reply_markup=ReplyKeyboardRemove())

    except ValueError:
        await client.send_message(user_id, "❌ Invalid Chat ID. Please forward a message or type a valid numeric ID.", reply_markup=ReplyKeyboardRemove())
    except Exception as e:
        await client.send_message(user_id, f"❌ Error: {e}", reply_markup=ReplyKeyboardRemove())


async def _bulk_add_source_channels(client, user_id: int):
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="ask_cancel")]])
    msg = await native_ask(
        client,
        user_id,
        "<b>❪ BULK ADD SOURCE CHANNELS ❫</b>\n\n"
        "Send Source (DB) channel IDs separated by spaces, commas, or new lines.\n\n"
        "<b>Example:</b>\n"
        "<code>-1001234567890\n-1009876543210\n-1001122334455</code>\n\n"
        "<i>💡 Tip: Ensure this bot is an Admin in the source channels.</i>",
        reply_markup=cancel_kb,
        parse_mode=enums.ParseMode.HTML
    )
    if not msg or (getattr(msg, "text", None) and "Cᴀɴᴄᴇʟ" in msg.text):
        return await client.send_message(user_id, "<i>Process Cancelled.</i>")

    raw = (msg.text or "").replace(",", " ").replace("\t", " ")
    parts = [p.strip() for p in raw.split() if p.strip()]
    ids = []
    for p in parts:
        if p.lstrip("-").isdigit():
            ids.append(int(p))

    if not ids:
        return await client.send_message(
            user_id, 
            "❌ <b>No valid numeric channel IDs found.</b>\n<i>Please send IDs starting with <code>-100...</code></i>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Channels", callback_data="mk#channels")]]),
            parse_mode=enums.ParseMode.HTML
        )

    added = exists = failed = 0
    for cid in ids:
        try:
            ex = await db.db.premium_channels.find_one({"channel_id": cid, "type": "db"})
            if ex:
                exists += 1
                continue
            try:
                chat = await client.get_chat(cid)
                name = getattr(chat, "title", None) or str(cid)
            except Exception:
                name = str(cid)
            await db.db.premium_channels.insert_one({"channel_id": cid, "name": name, "type": "db"})
            added += 1
        except Exception:
            failed += 1

    await client.send_message(
        user_id,
        f"✅ <b>Bulk Source Channels Added!</b>\n\n"
        f"• Added: <b>{added}</b>\n"
        f"• Already Existed: <b>{exists}</b>\n"
        f"• Failed: <b>{failed}</b>",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Channels Menu", callback_data="mk#channels")]]),
        parse_mode=enums.ParseMode.HTML
    )


async def _bulk_add_delivery_channels(client, user_id: int):
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="ask_cancel")]])
    msg = await native_ask(
        client,
        user_id,
        "<b>❪ BULK ADD DELIVERY CHANNELS ❫</b>\n\n"
        "Send delivery channel IDs separated by spaces, commas, or new lines.\n\n"
        "<b>Example:</b>\n"
        "<code>-100111...\n-100222...\n-100333...</code>\n\n"
        "<i>💡 Tip: Ensure this bot is an Admin in the delivery channels.</i>",
        reply_markup=cancel_kb,
        parse_mode=enums.ParseMode.HTML
    )
    if not msg or (getattr(msg, "text", None) and "Cᴀɴᴄᴇʟ" in msg.text):
        return await client.send_message(user_id, "<i>Process Cancelled.</i>")

    raw = (msg.text or "").replace(",", " ").replace("\t", " ")
    parts = [p.strip() for p in raw.split() if p.strip()]
    ids = []
    for p in parts:
        if p.lstrip("-").isdigit():
            ids.append(int(p))

    if not ids:
        return await client.send_message(
            user_id, 
            "❌ <b>No valid numeric channel IDs found.</b>\n<i>Please send IDs starting with <code>-100...</code></i>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Channels", callback_data="mk#channels")]]),
            parse_mode=enums.ParseMode.HTML
        )

    added = exists = failed = 0
    for cid in ids:
        try:
            ex = await db.db.premium_channels.find_one({"channel_id": cid, "type": "delivery"})
            if ex:
                exists += 1
                continue
            try:
                chat = await client.get_chat(cid)
                name = getattr(chat, "title", None) or str(cid)
            except Exception:
                name = str(cid)
            await db.db.premium_channels.insert_one({"channel_id": cid, "name": name, "type": "delivery"})
            added += 1
        except Exception:
            failed += 1

    await client.send_message(
        user_id,
        f"✅ <b>Bulk Delivery Channels Added!</b>\n\n"
        f"• Added: <b>{added}</b>\n"
        f"• Already Existed: <b>{exists}</b>\n"
        f"• Failed: <b>{failed}</b>",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Channels Menu", callback_data="mk#channels")]]),
        parse_mode=enums.ParseMode.HTML
    )


async def _menu_media_bulk_add_flow(client, user_id: int, b_id: str):
    bot = await _find_premium_bot(b_id)
    if not bot:
        return await client.send_message(user_id, "❌ Bot not found.", reply_markup=ReplyKeyboardRemove())

    from plugins.userbot.market_seller import market_clients
    store_cli = market_clients.get(str(b_id))
    if not store_cli:
        return await client.send_message(user_id, "❌ Store bot is not running right now.", reply_markup=ReplyKeyboardRemove())

    import os
    if not os.path.exists("downloads"):
        os.makedirs("downloads")

    done_kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done Adding", callback_data="ask_cancel")]])
    await client.send_message(
        user_id,
        "<b>📥 BULK MEDIA ADDER</b>\n\n"
        "Send <b>Photos</b>, <b>GIFs</b>, or <b>Videos</b> one by one.\n"
        "I will automatically process and save them to your menu media list.\n\n"
        "<i>Click the button below when you are finished.</i>",
        reply_markup=done_kb
    )

    count = 0
    while True:
        # Wait for next input without sending a new text message every time
        # We use native_ask with a special check or just a small status message
        msg = await native_ask(
            client, 
            user_id, 
            f"<b>📥 Bulk Adding... (#{count})</b>\n\nSend the next item or click 'Done' below.", 
            reply_markup=done_kb,
            timeout=600
        )
        if not msg:
            break
        
        if isinstance(msg, CallbackQuery):
            if msg.data == "ask_cancel":
                break
            continue
            
        if getattr(msg, "text", None) and (msg.text.lower() == "/start" or "done" in msg.text.lower()):
            break

        media_type = None
        if getattr(msg, "photo", None): media_type = "photo"
        elif getattr(msg, "animation", None): media_type = "animation"
        elif getattr(msg, "video", None): media_type = "video"
        
        if not media_type:
            if not isinstance(msg, CallbackQuery):
                await client.send_message(user_id, "❌ Please send a Photo, GIF, or Video.\nOr click <b>'Done Adding'</b>.", reply_markup=done_kb)
            continue

        # Re-fetch bot to check current count
        bot = await _find_premium_bot(b_id)
        items = _cfg_list(bot.get("config", {}), "menu_media")
        if len([x for x in items if isinstance(x, dict)]) >= 30:
            await client.send_message(user_id, "⚠️ Limit of 30 media items reached. Stopping bulk upload.")
            break

        ext = ".jpg" if media_type == "photo" else (".gif" if media_type == "animation" else ".mp4")
        tmp_path = await client.download_media(msg, file_name=f"downloads/bulk_{b_id}_{int(time.time())}{ext}")
        
        try:
            sent = None
            f_id = None
            if media_type == "photo":
                sent = await store_cli.send_photo(user_id, photo=tmp_path)
                f_id = getattr(sent.photo, "file_id", None)
            elif media_type == "animation":
                sent = await store_cli.send_animation(user_id, animation=tmp_path)
                f_id = getattr(sent.animation, "file_id", None)
            else:
                sent = await store_cli.send_video(user_id, video=tmp_path)
                f_id = getattr(sent.video, "file_id", None)

            if f_id:
                await db.db.premium_bots.update_one(
                    {"id": int(b_id)}, 
                    {"$push": {"config.menu_media": {"type": media_type, "file_id": f_id}}, "$unset": {"config.menuimg": ""}}
                )
                count += 1
                await client.send_message(user_id, f"✅ Media #{count} added! Send more or click 'Done'.", reply_markup=done_kb)
            
            if sent:
                await store_cli.delete_messages(user_id, sent.id)
        except Exception as e:
            await client.send_message(user_id, f"❌ Error processing item: {e}")
        finally:
            if os.path.exists(tmp_path):
                try: os.remove(tmp_path)
                except: pass

    await client.send_message(user_id, f"<b>🏁 Bulk Add Finished!</b>\n\nTotal media items added: <b>{count}</b>", reply_markup=ReplyKeyboardRemove())




async def _bot_broadcast_flow(client, user_id: int, b_id: str):
    from plugins.userbot.market_seller import market_clients
    from pyrogram.errors import UserIsBlocked, FloodWait, PeerIdInvalid, InputUserDeactivated
    import time
    from pyrogram.types import ReplyKeyboardMarkup, ReplyKeyboardRemove
    
    bt = await _find_premium_bot(b_id)
    if not bt:
        return await client.send_message(user_id, "❌ Bot not found in Database.")
    
    seller_cli = market_clients.get(str(b_id))
    if not seller_cli:
        return await client.send_message(user_id, "❌ Bot is not active or not started. Please ensure the delivery bot is running.")

    prompt_kb = ReplyKeyboardMarkup([[utils.to_smallcap("Back")], [utils.to_smallcap("Cancel Transaction")]], resize_keyboard=True)
    
    try:
        ans1 = await native_ask(client, user_id, 
            f"<b>📢 BROADCAST SYSTEM — {bt.get('bot_username', 'Bot')}</b>\n\n"
            f"Please send the message you want to broadcast.\n"
            f"It can be <b>Text, Photo, Video, Document, or even a Forwarded Post</b>.\n\n"
            f"<i>Send anything now, or click Cancel below.</i>",
            reply_markup=prompt_kb
        )
        
        if _is_cancel(ans1):
            return await client.send_message(user_id, "<i>❌ Broadcast Cancelled.</i>", reply_markup=ReplyKeyboardRemove())

        # Start Implementation
        status_msg = await client.send_message(user_id, "<b>⏳ Initializing Broadcast...</b>", reply_markup=ReplyKeyboardRemove())
        
        # Determine Users: Users who have started THIS bot (bot_ids contains b_id)
        # We need to handle the case where b_id is passed as a string but stored as int/vice versa
        try: target_bot_id = int(b_id)
        except: target_bot_id = b_id

        users_cursor = db.db.users.find({"bot_ids": target_bot_id})
        total_users = await db.db.users.count_documents({"bot_ids": target_bot_id})
        
        if total_users == 0:
            return await status_msg.edit_text("❌ No users found for this bot.\n\nNote: User tracking for broadcasts started just now. Please wait for users to interact with the bot first.")

        sent_count = 0
        delivered = 0
        failed = 0
        blocked = 0
        
        start_time = time.time()
        
        # Iterate users
        async for user_doc in users_cursor:
            target_id = user_doc['id']
            try:
                await seller_cli.copy_message(chat_id=target_id, from_chat_id=user_id, message_id=ans1.id)
                delivered += 1
            except FloodWait as e:
                await asyncio.sleep(e.value)
                # Retry once
                try: 
                    await seller_cli.copy_message(chat_id=target_id, from_chat_id=user_id, message_id=ans1.id)
                    delivered += 1
                except: failed += 1
            except (UserIsBlocked, PeerIdInvalid, InputUserDeactivated):
                blocked += 1
            except Exception as e:
                failed += 1
            
            sent_count += 1
            
            # Periodic update every 15 users
            if sent_count % 15 == 0 or sent_count == total_users:
                elapsed = time.time() - start_time
                speed = sent_count / elapsed if elapsed > 0 else 1
                rem_users = total_users - sent_count
                eta_s = rem_users / speed if speed > 0 else 0
                
                prog_txt = (
                    f"<b>📢 BROADCASTING IN PROGRESS...</b>\n\n"
                    f"<b>👤 Progress:</b> {sent_count}/{total_users}\n"
                    f"<b>✅ Success:</b> {delivered}\n"
                    f"<b>🚫 Blocked:</b> {blocked}\n"
                    f"<b>❌ Failed:</b> {failed}\n\n"
                    f"<b>⏱️ Speed:</b> {speed:.1f} users/sec\n"
                    f"<b>⏳ ETA:</b> {int(eta_s // 60)}m {int(eta_s % 60)}s"
                )
                try: await status_msg.edit_text(prog_txt)
                except: pass
        
        final_txt = (
            f"<b>🏁 BROADCAST COMPLETED!</b>\n\n"
            f"<b>📊 Final Stats:</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>👤 Total Targeted:</b> {total_users}\n"
            f"<b>✅ Delivered:</b> {delivered}\n"
            f"<b>🚫 Blocked/Inactive:</b> {blocked}\n"
            f"<b>❌ Other Failures:</b> {failed}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>⏱️ Total Time:</b> {int((time.time() - start_time) // 60)}m {int((time.time() - start_time) % 60)}s"
        )
        await status_msg.edit_text(final_txt)

    except asyncio.TimeoutError:
        await client.send_message(user_id, "⏳ Broadcast session timed out.", reply_markup=ReplyKeyboardRemove())
    except Exception as e:
        logger.error(f"Broadcast Flow Error: {e}")
        await client.send_message(user_id, f"❌ Broadcast failed: {e}", reply_markup=ReplyKeyboardRemove())





# ── Message Single Buyer Flow ─────────────────────────────────────────────────
async def _msg_single_buyer_flow(client, admin_id: int, target_uid: int):
    """Admin composes a message to send to a single buyer via the store bot."""
    from utils import native_ask
    try:
        # Wait for admin reply
        resp = await native_ask(
            client, admin_id, 
            f"<b>📩 Message to User <code>{target_uid}</code></b>\n\n"
            "Send the message you want to deliver (text, photo with caption, etc.).\n"
            "<i>Send /cancel to abort.</i>", 
            timeout=120,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="mk#back")]])
        )
        if not resp or (resp.text or "").strip().lower() == "/cancel":
            await client.send_message(admin_id, "<i>❌ Cancelled.</i>", parse_mode=enums.ParseMode.HTML)
            return

        # Get a store bot client to send
        from plugins.userbot.market_seller import market_clients
        seller_cli = next(iter(market_clients.values()), None) if market_clients else None
        send_client = seller_cli or client

        try:
            if resp.photo:
                await send_client.send_photo(target_uid, resp.photo.file_id, caption=resp.caption or "")
            elif resp.text:
                await send_client.send_message(target_uid, resp.text, parse_mode=enums.ParseMode.HTML)
            await client.send_message(
                admin_id,
                f"<b>✅ Message delivered to <code>{target_uid}</code></b>",
                parse_mode=enums.ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back to User", callback_data=f"mk#usr_view_{target_uid}")]])
            )
        except Exception as e:
            await client.send_message(admin_id, f"<b>❌ Failed:</b> <code>{e}</code>", parse_mode=enums.ParseMode.HTML)
    except asyncio.TimeoutError:
        await client.send_message(admin_id, "<i>⏰ Timed out.</i>", parse_mode=enums.ParseMode.HTML)


# ── Message All Buyers Flow ───────────────────────────────────────────────────
# ── Message All Buyers Flow ───────────────────────────────────────────────────
async def _msg_all_buyers_flow(client, admin_id: int):
    """Admin composes a broadcast message with Audience selection to all buyers (bot + mini app)."""
    from utils import native_ask
    from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from pyrogram.errors import UserIsBlocked, FloodWait, PeerIdInvalid, InputUserDeactivated

    try:
        # Step 1: Audience selection
        aud_msg = await client.send_message(
            admin_id,
            "<b>📢 BROADCAST SYSTEM — Select Audience</b>\n\n"
            "Choose who should receive this broadcast:\n"
            "1. <b>🟢 Paid Buyers Only</b> (Users with completed purchases)\n"
            "2. <b>🟠 Pending / Failed Buyers</b> (Users with pending checkouts)\n"
            "3. <b>🌐 All Users</b> (Entire bot & app database)\n\n"
            "<i>Reply with 1, 2, or 3 (or send /cancel to abort):</i>",
            parse_mode=enums.ParseMode.HTML
        )
        aud_resp = await native_ask(client, admin_id, "Enter 1, 2, or 3:", timeout=60)
        if not aud_resp or (aud_resp.text or "").strip().lower() == "/cancel":
            await client.send_message(admin_id, "<i>❌ Broadcast Cancelled.</i>", parse_mode=enums.ParseMode.HTML)
            return

        choice = (aud_resp.text or "").strip()
        if choice == "1":
            aud_mode = "paid"
            aud_label = "Paid Buyers Only"
        elif choice == "2":
            aud_mode = "pending"
            aud_label = "Pending/Failed Buyers"
        else:
            aud_mode = "all"
            aud_label = "All Users"

        # Step 2: Ask for Broadcast Message
        resp = await native_ask(
            client, admin_id, 
            f"<b>📢 Broadcast Target: {aud_label}</b>\n\n"
            "Send the message to broadcast.\n"
            "It can be <b>Text (with HTML/quotes), Photo, Video, Document, or a Forwarded Post</b>.\n\n"
            "<i>Send /cancel to abort.</i>", 
            timeout=120,
            parse_mode=enums.ParseMode.HTML
        )
        if not resp or (resp.text or "").strip().lower() == "/cancel":
            await client.send_message(admin_id, "<i>❌ Broadcast Cancelled.</i>", parse_mode=enums.ParseMode.HTML)
            return

        # Step 3: Resolve Audience IDs
        target_ids = set()
        if aud_mode == "paid":
            async for u in db.db.users.find({"purchases.0": {"$exists": True}}, {"id": 1}):
                if u.get("id"): target_ids.add(u.get("id"))
            async for o in db.db.orders.find({"status": {"$in": ["paid", "approved", "completed", "delivered"]}}, {"user_id": 1}):
                if o.get("user_id"): target_ids.add(o.get("user_id"))
        elif aud_mode == "pending":
            paid_set = set()
            async for u in db.db.users.find({"purchases.0": {"$exists": True}}, {"id": 1}):
                if u.get("id"): paid_set.add(u.get("id"))
            async for o in db.db.orders.find({"status": {"$in": ["paid", "approved", "completed", "delivered"]}}, {"user_id": 1}):
                if o.get("user_id"): paid_set.add(o.get("user_id"))

            async for o in db.db.orders.find({"status": {"$in": ["pending", "created", "processing", "waiting_screenshot", "failed"]}}, {"user_id": 1}):
                uid = o.get("user_id")
                if uid and uid not in paid_set: target_ids.add(uid)
            async for c in db.db.premium_checkout.find({"status": {"$in": ["pending", "created", "processing", "waiting_screenshot", "failed"]}}, {"user_id": 1}):
                uid = c.get("user_id")
                if uid and uid not in paid_set: target_ids.add(uid)
        else:
            async for u in db.db.users.find({}, {"id": 1}):
                if u.get("id"): target_ids.add(u.get("id"))

        if not target_ids:
            await client.send_message(admin_id, f"❌ No users found for audience filter <b>{aud_label}</b>.", parse_mode=enums.ParseMode.HTML)
            return

        # Get delivery client
        from plugins.userbot.market_seller import market_clients
        seller_cli = next(iter(market_clients.values()), None) if market_clients else None
        send_client = seller_cli or client

        sent = failed = blocked = 0
        total_targets = len(target_ids)
        status_msg = await client.send_message(admin_id, f"<i>⏳ Broadcasting to {total_targets} users ({aud_label})...</i>", parse_mode=enums.ParseMode.HTML)

        for idx, uid in enumerate(target_ids):
            try:
                await send_client.copy_message(chat_id=uid, from_chat_id=admin_id, message_id=resp.id)
                sent += 1
            except FloodWait as e:
                await asyncio.sleep(e.value)
                try:
                    await send_client.copy_message(chat_id=uid, from_chat_id=admin_id, message_id=resp.id)
                    sent += 1
                except Exception:
                    failed += 1
            except (UserIsBlocked, PeerIdInvalid, InputUserDeactivated):
                blocked += 1
            except Exception:
                failed += 1

            if (idx + 1) % 15 == 0 or (idx + 1) == total_targets:
                try:
                    pct = round(((idx + 1) / total_targets) * 100, 1)
                    await status_msg.edit_text(
                        f"<b>📢 BROADCASTING ({aud_label})</b>\n\n"
                        f"📊 Progress: <b>{pct}%</b> ({idx + 1}/{total_targets})\n"
                        f"✅ Sent: <b>{sent}</b> | ❌ Failed: <b>{failed}</b> | 🚫 Blocked: <b>{blocked}</b>",
                        parse_mode=enums.ParseMode.HTML
                    )
                except Exception:
                    pass

            await asyncio.sleep(0.05)

        try:
            await status_msg.delete()
        except Exception:
            pass

        await client.send_message(
            admin_id,
            f"<b>📢 Broadcast Complete ({aud_label})</b>\n\n"
            f"✅ Delivered: <b>{sent}</b>\n"
            f"🚫 Blocked: <b>{blocked}</b>\n"
            f"❌ Failed: <b>{failed}</b>\n"
            f"📊 Total Target: <b>{total_targets}</b>",
            parse_mode=enums.ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Buyers", callback_data="mk#users")]])
        )

    except asyncio.TimeoutError:
        await client.send_message(admin_id, "<i>⏰ Timed out.</i>", parse_mode=enums.ParseMode.HTML)
    except Exception as e:
        await client.send_message(admin_id, f"❌ Error during broadcast: {e}", parse_mode=enums.ParseMode.HTML)

@Client.on_message(filters.command(["sync_ongoing", "sync_ongoing_stories", "ongoing_sync"]) & filters.private)
async def mgmt_sync_ongoing(client, message):
    user_id = message.from_user.id
    if await _deny_if_not_owner(client, user_id):
        return

    wait_msg = await message.reply_text(
        "<b>🚀 Initializing Ongoing Stories & Parts Sync...</b>\n\n"
        "<i>Scanning channel messages for all active ongoing stories and updating episode ranges & parts in real-time...</i>",
        parse_mode=enums.ParseMode.HTML
    )

    try:
        from plugins.premium_live_monitor import check_and_update_all_ongoing_stories
        await check_and_update_all_ongoing_stories(client)
        
        # Query updated ongoing stories summary
        stories = await db.db.premium_stories.find({
            "$or": [
                {"status": {"$in": ["Ongoing", "ongoing", "ONGOING"]}},
                {"parts.badge": "ongoing"},
                {"parts.is_ongoing": True}
            ]
        }).to_list(length=None)

        summary_lines = []
        for s in stories[:15]:
            s_name = s.get("story_name_en") or s.get("story_name") or s.get("title") or "Story"
            eps = s.get("episodes") or "?"
            end_id = s.get("end_id") or s.get("end_message_id") or "?"
            ong_p_str = ""
            for p in (s.get("parts") or []):
                if str(p.get("badge") or "").lower() == "ongoing" or p.get("is_ongoing"):
                    ong_p_str = f" [Part {p.get('part', '?')}: {p.get('episodes', '?')}]"
                    break
            summary_lines.append(f"• <b>{s_name}</b>: {eps} Eps (End: {end_id}){ong_p_str}")

        text = (
            f"<b>✅ Ongoing Stories Sync Complete!</b>\n\n"
            f"• Total Ongoing Stories Checked: <b>{len(stories)}</b>\n\n"
            f"<b>📊 Latest Ongoing Status:</b>\n"
            + "\n".join(summary_lines)
            + f"\n\n<i>All new episodes and ongoing parts have been synchronized with MongoDB and Mini App!</i>"
        )
        await wait_msg.edit_text(text, parse_mode=enums.ParseMode.HTML)
    except Exception as e:
        await wait_msg.edit_text(f"❌ <b>Error syncing ongoing stories:</b> {e}", parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.command(["sync_all_stories", "sync_stories"]) & filters.private)
async def mgmt_sync_all_stories(client, message):
    user_id = message.from_user.id
    if await _deny_if_not_owner(client, user_id):
        return
    
    import time
    start_time = time.time()
    last_edit_time = 0

    status_msg = await message.reply_text(
        "<b>🔄 Initializing Bulk Story File Indexing...</b>\n\n"
        "<i>Scanning channel messages for all stories in background. Updates will be shown live here.</i>",
        parse_mode=enums.ParseMode.HTML
    )
    
    async def on_progress(idx, total, name, valid_count, ok):
        nonlocal last_edit_time
        curr_time = time.time()
        # Update every 3 seconds or on first/last
        if curr_time - last_edit_time >= 3.0 or idx == total or idx == 1:
            last_edit_time = curr_time
            elapsed = int(curr_time - start_time)
            el_m, el_s = divmod(elapsed, 60)
            
            # ETA calculation
            if idx > 0 and total > 0:
                pct = (idx / total) * 100
                rate = idx / max(1, elapsed)
                remaining_items = total - idx
                eta_seconds = int(remaining_items / rate) if rate > 0 else 0
                eta_m, eta_s = divmod(eta_seconds, 60)
                eta_str = f"{eta_m}m {eta_s}s" if eta_m > 0 else f"{eta_s}s"
            else:
                pct = 0
                eta_str = "Calculating..."
                
            # Progress bar [████████░░░░]
            bar_len = 10
            filled = int(round(bar_len * (pct / 100)))
            prog_bar = "█" * filled + "░" * (bar_len - filled)
            
            status_symbol = "✅" if ok else "⚠️"
            try:
                await status_msg.edit_text(
                    f"<b>🔄 Bulk Story Indexing in Progress</b>\n\n"
                    f"<code>[{prog_bar}] {pct:.1f}%</code> ({idx}/{total})\n\n"
                    f"<b>{status_symbol} Current:</b> {name[:35]}\n"
                    f"<b>📦 Valid Files:</b> {valid_count}\n\n"
                    f"⏱️ <b>Elapsed:</b> {el_m}m {el_s}s\n"
                    f"⏳ <b>Estimated Remaining:</b> {eta_str}\n\n"
                    f"<i>Please do not restart the bot while indexing is running.</i>",
                    parse_mode=enums.ParseMode.HTML
                )
            except Exception:
                pass
                
    from utils import scan_and_index_all_stories
    async def _run_bg():
        try:
            res = await scan_and_index_all_stories(client, db=db, progress_cb=on_progress, skip_clean=True)
            total_elapsed = int(time.time() - start_time)
            tot_m, tot_s = divmod(total_elapsed, 60)
            time_taken = f"{tot_m}m {tot_s}s" if tot_m > 0 else f"{tot_s}s"
            await status_msg.edit_text(
                f"<b>✅ Bulk Story File Indexing Complete!</b>\n\n"
                f"• Total Stories: <b>{res['total']}</b>\n"
                f"• Clean Stories Skipped: <b>{res.get('skipped', 0)}</b> <i>(Fast-skipped in 0s)</i>\n"
                f"• Stories Repaired & Synced: <b>{res['success']}</b>\n"
                f"• Failed: <b>{res['failed']}</b>\n"
                f"• Time Taken: <b>{time_taken}</b>\n\n"
                f"<i>All dead file gaps have been removed from chunk selection and file counts are 100% accurate!</i>",
                parse_mode=enums.ParseMode.HTML
            )
        except Exception as e:
            try:
                await status_msg.edit_text(f"❌ <b>Sync failed:</b> {e}", parse_mode=enums.ParseMode.HTML)
            except Exception:
                pass
                
    asyncio.create_task(_run_bg())


@Client.on_message(filters.command(["sync_story", "resync_story"]) & filters.private)
async def mgmt_sync_story(client, message):
    user_id = message.from_user.id
    if await _deny_if_not_owner(client, user_id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply_text("<b>Usage:</b> <code>/sync_story &lt;story_id or story_name&gt;</code>", parse_mode=enums.ParseMode.HTML)
        
    s_id_input = args[1].strip()
    from bson.objectid import ObjectId
    import re
    story = None
    try:
        story = await db.db.premium_stories.find_one({"_id": ObjectId(s_id_input)})
    except Exception:
        pass
    if not story:
        story = await db.db.premium_stories.find_one({"_id": s_id_input})
    if not story:
        story = await db.db.premium_stories.find_one({"story_id": s_id_input})
    if not story:
        # Search by name/title
        escaped_pattern = re.escape(s_id_input)
        story = await db.db.premium_stories.find_one({
            "$or": [
                {"story_name_en": {"$regex": escaped_pattern, "$options": "i"}},
                {"story_name": {"$regex": escaped_pattern, "$options": "i"}},
                {"title": {"$regex": escaped_pattern, "$options": "i"}},
                {"clean_title": {"$regex": re.sub(r'[^a-zA-Z0-9]', '', s_id_input.lower()), "$options": "i"}}
            ]
        })
        
    if not story:
        return await message.reply_text(f"❌ <b>Story '{s_id_input}' not found!</b>", parse_mode=enums.ParseMode.HTML)
        
    wait_msg = await message.reply_text("<i>⏳ Scanning and indexing story files...</i>", parse_mode=enums.ParseMode.HTML)
    from utils import scan_and_index_story
    try:
        valid_ids = await scan_and_index_story(client, story, save_to_db=True, db=db)
        name = story.get('story_name_en', story.get('story_name', 'Story'))
        s_id = story.get('start_id', '?')
        e_id = story.get('end_id', '?')
        await wait_msg.edit_text(
            f"<b>✅ Story Synced Successfully!</b>\n\n"
            f"<b>Story:</b> {name}\n"
            f"<b>Message Range:</b> {s_id} - {e_id}\n"
            f"<b>Valid Active Files:</b> <b>{len(valid_ids)}</b>\n\n"
            f"<i>Chunks and parts updated cleanly in database!</i>",
            parse_mode=enums.ParseMode.HTML
        )
    except Exception as e:
        await wait_msg.edit_text(f"❌ <b>Error syncing story:</b> {e}", parse_mode=enums.ParseMode.HTML)

