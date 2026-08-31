import os
import time
import uuid
import asyncio
import logging
from pyrogram import Client, filters
from pyrogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    InlineQuery, InlineQueryResultPhoto, InlineQueryResultArticle, InputTextMessageContent
)
from pyrogram.errors import FloodWait, RPCError

from config import Config
from database import db
from plugins.cashfree_helper import get_cashfree_credentials
from plugins.url_bypass import _clean_video_caption

logger = logging.getLogger("StoreBot")
PM = "html"

# Default Demo File fallback message / sample
DEFAULT_DEMO_TEXT = (
    "📹 <b>Demo Video Show Sample</b>\n\n"
    "<i>Yeh ek demo sample show video hai. Aap jo bhi show buy karenge, wo bhi isi high-quality format me complete combined episodes ke sath deliver hoga.\n\n"
    "⚠️ <b>Important Note:</b>\n"
    "Digital content ek baar unlock hone ke baad access instant mil jata hai, isliye purchase se pehle show title aur details confirm kar lein.</i>"
)


# ── Cloned & Isolated Cashfree Payment Generator for Store Shows ─────────────
async def create_store_cashfree_order(
    user_id: int,
    user_name: str,
    show_id: str,
    show_title: str,
    amount: float,
    bot_id: str,
    bot_username: str
) -> dict:
    """
    Creates an isolated Cashfree PG order strictly for a Store Show.
    Does NOT modify or touch Delivery Bot pass configuration.
    """
    creds = await get_cashfree_credentials()
    if not creds.get("app_id") or not creds.get("secret_key"):
        return {"success": False, "error": "Cashfree is not configured in settings."}

    order_id = f"SHOW_{uuid.uuid4().hex[:10]}"
    cust_id = f"CUST_{user_id}"
    cust_name = (user_name or f"User_{user_id}")[:50]
    cust_phone = "9999999999"

    import aiohttp
    url = f"{creds['base_url']}/orders"
    headers = {
        "x-client-id": creds["app_id"],
        "x-client-secret": creds["secret_key"],
        "x-api-version": "2023-08-01",
        "Content-Type": "application/json"
    }
    payload = {
        "order_id": order_id,
        "order_amount": float(amount),
        "order_currency": "INR",
        "customer_details": {
            "customer_id": cust_id,
            "customer_name": cust_name,
            "customer_phone": cust_phone
        },
        "order_meta": {
            "return_url": f"https://t.me/{bot_username.lstrip('@')}?start=chkorder_{order_id}",
            "notify_url": "https://aryapremium.store/api/cashfree-webhook"
        },
        "order_note": f"Store Show: {show_title[:40]}"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                res_json = await resp.json()
                if resp.status not in (200, 201):
                    err_msg = res_json.get("message", str(res_json))
                    return {"success": False, "error": f"Cashfree Error: {err_msg}"}

                payment_session_id = res_json.get("payment_session_id")
                payment_link = (
                    res_json.get("payments", {}).get("url") or
                    f"https://aryapremium.store/api/cashfree-pay?session_id={payment_session_id}"
                )

                # Save order to store_orders collection
                order_doc = {
                    "order_id": order_id,
                    "payment_session_id": payment_session_id,
                    "user_id": user_id,
                    "user_name": user_name,
                    "bot_id": str(bot_id),
                    "bot_username": bot_username,
                    "show_id": show_id,
                    "show_title": show_title,
                    "amount": amount,
                    "gateway": "cashfree",
                    "status": "PENDING",
                    "payment_link": payment_link,
                    "created_at": time.time()
                }
                await db.create_store_order(order_doc)

                return {
                    "success": True,
                    "order_id": order_id,
                    "payment_link": payment_link,
                    "session_id": payment_session_id
                }
    except Exception as e:
        logger.error(f"Store Cashfree Order Creation Exception: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def verify_store_cashfree_order(order_id: str) -> dict:
    """Queries Cashfree server-to-server to check if order is paid."""
    creds = await get_cashfree_credentials()
    if not creds.get("app_id") or not creds.get("secret_key"):
        return {"success": False, "is_paid": False, "error": "Cashfree credentials missing"}

    import aiohttp
    url = f"{creds['base_url']}/orders/{order_id}"
    headers = {
        "x-client-id": creds["app_id"],
        "x-client-secret": creds["secret_key"],
        "x-api-version": "2023-08-01",
        "Content-Type": "application/json"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                res_json = await resp.json()
                if resp.status == 200:
                    order_status = res_json.get("order_status", "").upper()
                    is_paid = (order_status == "PAID")
                    return {"success": True, "is_paid": is_paid, "status": order_status, "data": res_json}
                else:
                    return {"success": False, "is_paid": False, "error": res_json.get("message", "Verify failed")}
    except Exception as e:
        logger.error(f"Store Cashfree Order Verify Exception for {order_id}: {e}")
        return {"success": False, "is_paid": False, "error": str(e)}


# ── Delivery & Auto-Delete Handler ───────────────────────────────────────────
async def deliver_purchased_show(client: Client, user_id: int, show_id: str, bot_id: str, order_id: str = ""):
    """
    Delivers full show video files directly to the user's DM.
    Starts 15-minute auto-delete countdown with user notice.
    Saves permanent record in store_user_shows.
    """
    show = await db.get_store_show(show_id)
    if not show:
        await client.send_message(user_id, "<b>Error:</b> Show not found in catalog. Please contact support.")
        return

    db_channel_id = show["channel_id"]
    video_msg_ids = show.get("video_msg_ids") or [show.get("video_msg_id")]

    # Register permanent ownership in DB
    await db.add_user_purchased_show(user_id, bot_id, show_id, order_id=order_id)
    if order_id:
        await db.update_store_order(order_id, {"status": "SUCCESS", "delivered_at": time.time()})

    delivered_msgs = []

    # Deliver each video file
    for v_id in video_msg_ids:
        if not v_id:
            continue
        try:
            v_msg = await client.get_messages(db_channel_id, v_id)
            if not v_msg or v_msg.empty:
                continue

            raw_cap = v_msg.caption or getattr(v_msg.document or v_msg.video, 'file_name', '') or ""
            clean_cap = _clean_video_caption(raw_cap) or show["title"]

            # Try copy_message without forward tag
            try:
                sent = await client.copy_message(
                    chat_id=user_id,
                    from_chat_id=db_channel_id,
                    message_id=v_id,
                    caption=f"🎬 <b>{clean_cap}</b>",
                    parse_mode=PM
                )
                if sent:
                    delivered_msgs.append(sent)
            except Exception as copy_err:
                logger.warning(f"copy_message failed for video {v_id}, downloading: {copy_err}")
                v_path = await client.download_media(v_msg)
                if v_path and os.path.exists(v_path):
                    sent = await client.send_document(
                        chat_id=user_id,
                        document=v_path,
                        caption=f"🎬 <b>{clean_cap}</b>",
                        parse_mode=PM
                    )
                    if sent:
                        delivered_msgs.append(sent)
                    try: os.remove(v_path)
                    except Exception: pass

        except FloodWait as fw:
            await asyncio.sleep(fw.value)
        except Exception as e:
            logger.error(f"Failed delivering video {v_id} to {user_id}: {e}")

    # Success Notice Message with 15-min Auto Delete Warning
    notice_text = (
        f"<emoji id=\"6107442434055086407\">✅</emoji> <b>Show Delivered Successfully!</b>\n\n"
        f"📽️ <b>Show :</b> {show['title']}\n"
        f"🎬 <b>Duration :</b> {show.get('duration', 'Full Show')}\n\n"
        f"┄┄┄┄┄┄┄┄┄┄┄ ➌ ┄┄┄┄┄┄┄┄┄┄\n"
        f"⚠️ <b>Auto-Delete Notice:</b>\n"
        f"<i>Yeh video files <b>15 minutes</b> me automatically delete ho jayengi. Kripya inhe apne <b>Saved Messages</b> me forward ya save kar lijiye!</i>\n\n"
        f"💡 <i>Aap is show ko kabhi bhi dobara <b>📹 My Shows</b> menu se free me re-deliver karwa sakte hain.</i>"
    )

    notice_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📹 My Shows", callback_data="store_myshows")],
        [InlineKeyboardButton("🛍️ Browse More Shows", callback_data="store_browse")]
    ])

    notice_msg = await client.send_message(user_id, notice_text, parse_mode=PM, reply_markup=notice_kb)

    # Schedule 15-minute auto-delete task
    if delivered_msgs:
        async def _auto_delete_job():
            await asyncio.sleep(900) # 15 minutes = 900 seconds
            for m in delivered_msgs:
                try: await client.delete_messages(user_id, m.id)
                except Exception: pass
            try:
                await notice_msg.edit_text(
                    f"🗑️ <b>Show Files Auto-Deleted (15 Min Expired)</b>\n\n"
                    f"Show <b>{show['title']}</b> ki video files security ke liye delete kar di gayi hain.\n\n"
                    f"Agar aapko dobara dekhna hai to aap <b>📹 My Shows</b> se 1-tap me re-deliver le sakte hain!",
                    parse_mode=PM,
                    reply_markup=notice_kb
                )
            except Exception:
                pass

        asyncio.create_task(_auto_delete_job())


# ── Store Bot Interactive UI Builders ─────────────────────────────────────────
def build_store_show_card(show: dict, price: int = 19) -> tuple[str, InlineKeyboardMarkup]:
    """Generates Show Card and Confirm/Demo buttons."""
    show_id = show["show_id"]
    title = show["title"]
    platform = show.get("platform", "Story TV")
    genre = show.get("genre", "Drama / Romance")
    duration = show.get("duration", "Full Show")
    cost = show.get("price", price)

    text = (
        f"<emoji id=\"6026337676091726218\">📽️</emoji> <b>Show :</b> {title}\n"
        f"<emoji id=\"6021683099773966917\">🖥</emoji> <b>Platform :</b> {platform}\n"
        f"<emoji id=\"5945256248390721326\">🧩</emoji> <b>Genre :</b> {genre}\n"
        f"<emoji id=\"5386367538735104399\">🎬</emoji> <b>Duration :</b> {duration}\n"
        f"<emoji id=\"5233326571099534068\">💰</emoji> <b>Price :</b> ₹{cost}\n\n"
        f"┄┄┄┄┄┄┄┄┄┄┄ ➌ ┄┄┄┄┄┄┄┄┄┄\n"
        f"<i>Is show ke sabhi episodes ek single combined high-quality video me include hain. Niche confirm karke payment complete karein.</i>"
    )

    buttons = [
        [InlineKeyboardButton(f"✅ Confirm & Buy (₹{cost})", callback_data=f"store_buy_{show_id}")],
        [InlineKeyboardButton("👁️ View Demo File", callback_data=f"store_demo_{show_id}")],
        [InlineKeyboardButton("🔙 Back to Store", callback_data="store_browse")]
    ]
    return text, InlineKeyboardMarkup(buttons)


def build_store_payment_methods(show_id: str, show_title: str, amount: int) -> tuple[str, InlineKeyboardMarkup]:
    """Generates Payment Method Selection (Cashfree Cards/NetBanking & UPI)."""
    text = (
        f"<emoji id=\"5904359114531675993\">💳</emoji> <b>Choose Payment Method</b>\n\n"
        f"<emoji id=\"6026337676091726218\">📽️</emoji> <b>Show :</b> {show_title}\n"
        f"<emoji id=\"5233326571099534068\">💰</emoji> <b>Total Amount :</b> ₹{amount}\n\n"
        f"┄┄┄┄┄┄┄┄┄┄┄ ➌ ┄┄┄┄┄┄┄┄┄┄\n"
        f"Select your preferred instant payment method:"
    )

    buttons = [
        [InlineKeyboardButton("💳 Pay Via Cards, NetBanking (Cashfree)", callback_data=f"store_pay_cf_{show_id}")],
        [InlineKeyboardButton("📱 Pay Via UPI (Direct QR / App)", callback_data=f"store_pay_upi_{show_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"store_view_{show_id}")]
    ]
    return text, InlineKeyboardMarkup(buttons)


# ── Store Bot Main Menu ───────────────────────────────────────────────────────
async def send_store_main_menu(client: Client, user_id: int, bot_id: str, name: str = ""):
    """Displays the clean, uncluttered Store Bot home menu with custom emojis."""
    user_name = name or f"User_{user_id}"
    total_shows = await db.count_store_shows()
    
    text = (
        f"<emoji id=\"6104800784354909891\">🎬</emoji> <b>Welcome to Pay-Per-Show OTT Store!</b>\n\n"
        f"Hey <b>{user_name}</b>, aap yaha <b>{total_shows}+</b> premium shows & stories direct buy karke instant full video access pa sakte hain.\n\n"
        f"┄┄┄┄┄┄┄┄┄┄┄ ➌ ┄┄┄┄┄┄┄┄┄┄\n"
        f"• <b>No Subscription Required</b> — Pay only for what you watch!\n"
        f"• <b>Combined Episodes</b> — Watch entire story in 1 single full file.\n"
        f"• <b>Lifetime My Shows</b> — Access your bought shows anytime.\n\n"
        f"Niche menu me se option select karein ya search karein:"
    )

    buttons = [
        [
            InlineKeyboardButton("📹 My Shows", callback_data="store_myshows"),
            InlineKeyboardButton("📦 My Orders", callback_data="store_myorders")
        ],
        [
            InlineKeyboardButton("🔍 Search Shows", switch_inline_query_current_chat=""),
            InlineKeyboardButton("🌐 Language", callback_data="store_lang")
        ],
        [
            InlineKeyboardButton("📖 Help & Tutorial", callback_data="store_help")
        ]
    ]

    api_buttons = [
        [
            {"text": "My Shows", "callback_data": "store_myshows", "icon_custom_emoji_id": "6026337676091726218"},
            {"text": "My Orders", "callback_data": "store_myorders", "icon_custom_emoji_id": "5920046907782074235"}
        ],
        [
            {"text": "Search Shows", "switch_inline_query_current_chat": "", "icon_custom_emoji_id": "5282843764451195532"},
            {"text": "Language", "callback_data": "store_lang", "icon_custom_emoji_id": "6021683099773966917"}
        ],
        [
            {"text": "Help & Tutorial", "callback_data": "store_help", "icon_custom_emoji_id": "5945256248390721326"}
        ]
    ]

    from plugins.share_bot import send_or_edit_with_custom_icons
    sent_ok = await send_or_edit_with_custom_icons(
        client=client,
        chat_id=user_id,
        text=text,
        inline_keyboard=api_buttons
    )
    if not sent_ok:
        try: await client.send_message(user_id, text, parse_mode=PM, reply_markup=InlineKeyboardMarkup(buttons))
        except Exception: pass
