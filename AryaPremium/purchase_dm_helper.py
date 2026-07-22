import os
import random
import logging
import asyncio
from typing import Optional, List, Union, Dict, Any

logger = logging.getLogger(__name__)

GREETING_TEMPLATES = [
    "Congratulations, {user_name}! Your payment landed safely, and your story has been added to your library.",
    "Well played, {user_name}. You didn't just buy today's episodes—you accidentally subscribed to future ones too.",
    "Breaking News, {user_name}! Your payment was successful... but your story isn't finished yet. More episodes are still on their way.",
    "Congratulations, {user_name}! Your payment landed safely, your story has been delivered to your library, and your bank balance has bravely accepted its fate.",
    "Hey {user_name}, your payment has been verified successfully. Your story is now part of your collection and is ready whenever you are."
]

def format_payment_method_display(method_raw: Optional[str]) -> str:
    if not method_raw:
        return "UPI (UTR)"
    m = str(method_raw).strip().lower()
    if "utr" in m or "upi" in m:
        return "UPI (UTR)"
    elif "oxapay" in m or "crypto" in m:
        return "Crypto (Oxapay)"
    elif "payu" in m:
        return "PayU"
    elif "paytm" in m or "pg" in m:
        return "PG (Paytm Payment Gateway)"
    elif "razorpay" in m or "rzp" in m:
        return "Razorpay"
    else:
        return str(method_raw).strip()

def format_verified_by_display(verified_by_raw: Optional[str] = None, is_admin_manual: bool = False) -> str:
    if is_admin_manual:
        return "Access Granted By Team"
    if verified_by_raw:
        v = str(verified_by_raw).strip().lower()
        if any(w in v for w in ["team", "admin", "manual", "access granted"]):
            return "Access Granted By Team"
    return "Auto Verified By System"

def build_purchase_complete_message(
    user_name: str,
    story_name: str,
    amount: Union[float, int, str],
    order_id: str,
    story_status: str,
    payment_method: str,
    verified_by: str = "Auto Verified By System",
    is_ongoing: bool = False,
    is_admin_manual: bool = False
) -> str:
    # 1. Random Greeting Quoteblock
    greeting_tmpl = random.choice(GREETING_TEMPLATES)
    clean_user_name = (user_name or "Friend").strip()
    greeting_text = greeting_tmpl.format(user_name=clean_user_name)
    
    quote_header = f"<blockquote><b>𝗣𝘂𝗿𝗰𝗵𝗮𝘀𝗲 𝗖𝗼𝗺𝗽𝗹𝗲𝘁𝗲 🧾</b>\n\n{greeting_text}</blockquote>"

    # 2. Details Block (Normal text, bold labels, copyable order ID in mono <code>)
    clean_story_name = str(story_name).strip() if story_name else "Story"
    
    try:
        f_amt = float(amount)
        clean_amount = f"{f_amt:.0f}" if f_amt.is_integer() else f"{f_amt:.2f}"
    except (ValueError, TypeError):
        clean_amount = str(amount or "0")

    clean_order_id = str(order_id or "ORDER").strip()
    clean_status = str(story_status).strip() if story_status else "Completed"
    clean_pm = format_payment_method_display(payment_method)
    clean_vb = format_verified_by_display(verified_by_raw=verified_by, is_admin_manual=is_admin_manual)

    details_block = (
        f"<b>𝗦𝘁𝗼𝗿𝘆:</b> {clean_story_name}\n"
        f"<b>𝗔𝗺𝗼𝘂𝗻𝘁:</b> ₹{clean_amount}\n"
        f"<b>𝗢𝗿𝗱𝗲𝗿 𝗜𝗗:</b> <code>{clean_order_id}</code>\n"
        f"<b>𝗦𝘁𝗮𝘁𝘂𝘀:</b> {clean_status}\n"
        f"<b>𝗣𝗮𝘆𝗺𝗲𝗻𝘁 𝗠𝗲𝘁𝗵𝗼𝗱:</b> {clean_pm}\n"
        f"<b>𝗩𝗲𝗿𝗶𝗳𝗶𝗲𝗱 𝗕𝘆:</b> {clean_vb}"
    )

    # 3. Ongoing note if status is Ongoing
    ongoing_block = ""
    if is_ongoing or clean_status.lower() == "ongoing":
        ongoing_block = (
            "\n\n<blockquote>This story is currently Ongoing, so your purchase doesn't stop here. "
            "Every future episode will be added to your library automatically as soon as it's released.</blockquote>"
        )

    # 4. Instructions & Guide Quoteblocks
    instructions_block = (
        "<blockquote>Your purchased story is available anytime:\n"
        "• <b>Mini App</b> → <b>Library</b> → <b>Purchased</b>\n"
        "• <b>Bot</b> → <b>My Stories</b></blockquote>\n\n"
        "<blockquote>Need it again later ? Just request delivery from there.</blockquote>\n\n"
        '<blockquote>The guide below will answer almost everything before you ask. <a href="https://t.me/StoriesLinkopningguide"><b>Guide</b></a></blockquote>\n\n'
        "<blockquote>Thank you for supporting Arya Premium. Every purchase helps us bring you more stories.</blockquote>"
    )

    return f"{quote_header}\n\n{details_block}{ongoing_block}\n\n{instructions_block}"

async def send_purchase_success_dm(
    db,
    user_id: Union[int, str],
    order_doc: Optional[Dict[str, Any]] = None,
    story_ids: Optional[List[str]] = None,
    order_id: Optional[str] = None,
    amount: Optional[Union[float, int, str]] = None,
    payment_method: Optional[str] = None,
    verified_by: Optional[str] = None,
    is_admin_manual: bool = False,
    user_name: Optional[str] = None
):
    """
    Sends the standardized Purchase Complete DM to the user on Telegram.
    Accepts order_doc or individual parameters.
    """
    try:
        if not user_id:
            return

        tg_id_int = int(user_id) if str(user_id).isdigit() else 0
        if not tg_id_int:
            return

        # Fetch user info if user_name is not provided
        if not user_name:
            user_name = "Friend"
            try:
                if db and hasattr(db, "db"):
                    u_doc = await db.db.users.find_one({"telegram_id": tg_id_int})
                    if not u_doc:
                        u_doc = await db.db.users.find_one({"_id": tg_id_int})
                    if u_doc:
                        user_name = u_doc.get("first_name") or u_doc.get("display_name") or u_doc.get("username") or "Friend"
            except Exception as u_err:
                logger.debug(f"Could not fetch user_name for DM: {u_err}")

        # Extract order fields if order_doc is provided
        if order_doc:
            order_id = order_id or order_doc.get("order_id") or order_doc.get("id")
            if amount is None:
                amount = order_doc.get("total", order_doc.get("amount", 0))
            payment_method = payment_method or order_doc.get("source", order_doc.get("payment_method", "UPI (UTR)"))
            story_ids = story_ids or order_doc.get("story_ids", [])
            verified_by = verified_by or order_doc.get("verified_by")

        # Fetch story details to get names and status
        story_names = []
        is_ongoing = False
        story_status_list = []

        if order_doc and order_doc.get("story_names"):
            raw_names = order_doc.get("story_names")
            if isinstance(raw_names, list):
                story_names = [str(n) for n in raw_names if n]
            elif isinstance(raw_names, str):
                story_names = [raw_names]

        if story_ids and db and hasattr(db, "db"):
            from bson.objectid import ObjectId
            for sid in story_ids:
                try:
                    s_obj_id = ObjectId(sid) if isinstance(sid, str) and len(sid) == 24 else sid
                    s_doc = await db.db.premium_stories.find_one({"_id": s_obj_id})
                    if s_doc:
                        s_name = s_doc.get("story_name_en") or s_doc.get("title")
                        if s_name and s_name not in story_names:
                            story_names.append(s_name)
                        st = s_doc.get("status", "") or ("Ongoing" if s_doc.get("isCompleted") is False else "Completed")
                        story_status_list.append(st)
                        if st.lower() == "ongoing" or s_doc.get("isCompleted") is False:
                            is_ongoing = True
                except Exception as s_err:
                    logger.debug(f"Story fetch error: {s_err}")

        story_name_str = ", ".join(story_names) if story_names else "Story"

        if is_ongoing:
            primary_status = "Ongoing"
        elif story_status_list:
            primary_status = story_status_list[0]
        else:
            primary_status = "Completed"

        full_message = build_purchase_complete_message(
            user_name=user_name,
            story_name=story_name_str,
            amount=amount or 0,
            order_id=order_id or "OD-COMPLETED",
            story_status=primary_status,
            payment_method=payment_method or "UPI (UTR)",
            verified_by=verified_by or "Auto Verified By System",
            is_ongoing=is_ongoing,
            is_admin_manual=is_admin_manual
        )

        # Resolve Bot Token
        bot_token = ""
        try:
            from mini_app_api import get_customer_bot_token
            bot_token = await get_customer_bot_token(tg_id_int)
        except Exception:
            pass

        if not bot_token:
            try:
                from AryaPremium.config import Config
                bot_token = getattr(Config, "BOT_TOKEN", "") or os.environ.get("BOT_TOKEN", "")
            except Exception:
                bot_token = os.environ.get("BOT_TOKEN", "")

        if not bot_token:
            logger.warning(f"send_purchase_success_dm: No bot token found for user {tg_id_int}")
            return

        bot_username = os.environ.get("BOT_USERNAME", "UseAryaBot")
        keyboard = {"inline_keyboard": [[{
            "text": "📚 Open Arya Premium",
            "url": f"https://t.me/{bot_username}/app"
        }]]}

        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id": tg_id_int,
                    "text": full_message,
                    "parse_mode": "HTML",
                    "reply_markup": keyboard,
                    "disable_web_page_preview": True
                },
                timeout=10
            ) as resp:
                res = await resp.json()
                if res.get("ok"):
                    logger.info(f"Purchase Complete DM successfully sent to user {tg_id_int}")
                else:
                    logger.warning(f"send_purchase_success_dm Telegram API response: {res}")

    except Exception as e:
        logger.error(f"send_purchase_success_dm exception: {e}", exc_info=True)
