import os
import random
import logging
import asyncio
from datetime import datetime, timezone
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
        return "UPI (QR)"
    m = str(method_raw).strip().lower()
    if "dodo" in m:
        return "Dodo Payments"
    elif "qr" in m or "utr" in m or m == "upi" or "upi (qr)" in m or "manual_upi" in m:
        return "UPI (QR)"
    elif "payu" in m:
        return "PayU PG"
    elif "paytm" in m:
        return "Paytm PG"
    elif "razorpay" in m or "rzp" in m:
        return "Razorpay PG"
    elif "oxapay" in m or "crypto" in m:
        return "Crypto"
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
    is_admin_manual: bool = False,
    bot_username: str = "UseAryaBot"
) -> str:
    # 1. Header Quoteblock (ONLY Purchase Complete header)
    quote_header = "<blockquote><b>𝗣𝘂𝗿𝗰𝗵𝗮𝘀𝗲 𝗖𝗼𝗺𝗽𝗹𝗲𝘁𝗲 🧾</b></blockquote>"

    # 2. Random Greeting (Normal text, NOT in quote block)
    greeting_tmpl = random.choice(GREETING_TEMPLATES)
    clean_user_name = (user_name or "User").strip()
    greeting_text = greeting_tmpl.format(user_name=clean_user_name)

    # 3. Details Block (Wrapped inside 1 entire Quoteblock)
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
        "<blockquote>"
        f"<b>𝗦𝘁𝗼𝗿𝘆:</b> {clean_story_name}\n"
        f"<b>𝗔𝗺𝗼𝘂𝗻𝘁:</b> ₹{clean_amount}\n"
        f"<b>𝗢𝗿𝗱𝗲𝗿 𝗜𝗗:</b> <code>{clean_order_id}</code>\n"
        f"<b>𝗦𝘁𝗮𝘁𝘂𝘀:</b> {clean_status}\n"
        f"<b>𝗣𝗮𝘆𝗺𝗲𝗻𝘁 𝗠𝗲𝘁𝗵𝗼𝗱:</b> {clean_pm}\n"
        f"<b>𝗩𝗲𝗿𝗶𝗳𝗶𝗲𝗱 𝗕𝘆:</b> {clean_vb}"
        "</blockquote>"
    )

    # 4. Ongoing note if status is Ongoing (Quoteblock)
    ongoing_block = ""
    if is_ongoing or clean_status.lower() == "ongoing":
        ongoing_block = (
            "\n\n<blockquote>This story is currently Ongoing, so your purchase doesn't stop here. "
            "Every future episode will be added to your library automatically as soon as it's released.</blockquote>"
        )

    # 5. Access Instructions:
    # "Your purchased story is available anytime:" in a Quote Block
    access_header_quote = "<blockquote>Your purchased story is available anytime:</blockquote>"

    # Below it, NOT in quote block, with direct link ONLY to Mini App Purchased section
    clean_bot = bot_username.replace("@", "") if bot_username else "UseAryaBot"
    purchased_link = f"https://t.me/{clean_bot}/apminibyarya?startapp=purchased"

    access_items = (
        f"• <b>Mini App</b> → <b>Library</b> → <b><a href=\"{purchased_link}\">Purchased Stories</a></b>"
    )

    # 6. Bottom Quoteblocks (NO 1-line gap between them)
    channel_link = "https://t.me/AryaPremiumTG"
    bottom_quotes = (
        "<blockquote>Need it again later ? Just open Arya Premium Mini App anytime to access your stories.</blockquote>\n"
        '<blockquote>The guide below will answer almost everything before you ask. <a href="https://t.me/StoriesLinkopningguide"><b>Guide</b></a></blockquote>\n'
        '<blockquote>Thank you for supporting Arya Premium. Every purchase helps us bring you more stories.\n\n'
        f'📢 <b>If you want to receive new updates related to Arya Premium in the future:</b>\n'
        f'👉 <b><a href="{channel_link}">Join Arya Premium Channel (@AryaPremiumTG)</a></b></blockquote>'
    )

    return f"{quote_header}\n\n{greeting_text}\n\n{details_block}{ongoing_block}\n\n{access_header_quote}\n{access_items}\n\n{bottom_quotes}"

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

        # ── Atomic Idempotency Claim Check ──
        # Guarantees that EXACTLY ONE DM receipt is sent per order attempt across webhooks/polling/retries
        rec_oid = order_id or (order_doc.get("order_id") if order_doc else None) or (order_doc.get("payment_id") if order_doc else None)
        if not rec_oid:
            s_list = story_ids or (order_doc.get("story_ids") if order_doc else [])
            rec_oid = "_".join(str(s) for s in s_list) if s_list else "unknown"

        claim_key = f"receipt_{tg_id_int}_{rec_oid}"
        if db and hasattr(db, "db"):
            try:
                claim = await db.db.sent_receipts.find_one_and_update(
                    {"_id": claim_key},
                    {"$setOnInsert": {"user_id": tg_id_int, "sent_at": datetime.now(timezone.utc)}},
                    upsert=True
                )
                if claim is not None:
                    logger.info(f"[Receipt DM] Receipt already sent for claim_key={claim_key}, skipping duplicate DM.")
                    return
            except Exception as claim_err:
                logger.warning(f"[Receipt DM] Claim check exception: {claim_err}")

        # Resolve Bot Token first so we can fetch live Telegram user chat info if needed
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

        # ── Fetch User Real Name (Order doc -> DB -> Live Telegram getChat API) ──
        resolved_name = user_name
        if not resolved_name or str(resolved_name).strip().lower() in ("friend", "user", ""):
            if order_doc:
                resolved_name = order_doc.get("first_name") or order_doc.get("payer_name") or order_doc.get("username")
            
            if (not resolved_name or str(resolved_name).strip().lower() in ("friend", "user", "")) and db and hasattr(db, "db"):
                try:
                    u_doc = await db.db.users.find_one({"telegram_id": tg_id_int}) or await db.db.users.find_one({"_id": tg_id_int})
                    if u_doc:
                        resolved_name = u_doc.get("first_name") or u_doc.get("display_name") or u_doc.get("username")
                except Exception as u_err:
                    logger.debug(f"Could not fetch DB user_name: {u_err}")

            # Live Telegram API getChat fallback to guarantee real Telegram name
            if (not resolved_name or str(resolved_name).strip().lower() in ("friend", "user", "")) and bot_token:
                try:
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        async with session.get(f"https://api.telegram.org/bot{bot_token}/getChat?chat_id={tg_id_int}", timeout=3) as gc_resp:
                            gc_data = await gc_resp.json()
                            if gc_data.get("ok"):
                                chat_res = gc_data.get("result", {})
                                resolved_name = chat_res.get("first_name") or chat_res.get("username")
                except Exception as gc_err:
                    logger.debug(f"getChat name fetch error: {gc_err}")

        final_user_name = resolved_name if resolved_name and str(resolved_name).strip() else "User"

        # Extract order fields if order_doc is provided
        if order_doc:
            order_id = order_id or order_doc.get("order_id") or order_doc.get("id")
            if amount is None:
                amount = order_doc.get("total", order_doc.get("amount", 0))
            payment_method = payment_method or order_doc.get("source", order_doc.get("payment_method", "UPI (UTR)"))
            story_ids = story_ids or order_doc.get("story_ids", [])
            verified_by = verified_by or order_doc.get("verified_by")

        # Ensure order_id is never raw checkout_ or legacy OD_ or blank
        raw_oid = str(order_id or "").strip()
        if not raw_oid or raw_oid.startswith("checkout_") or raw_oid.startswith("OD-") or raw_oid.startswith("OD_"):
            try:
                from plugins.userbot.market_seller import _make_arya_bot_order_id
                first_sid = story_ids[0] if (story_ids and len(story_ids) > 0) else None
                order_id = await _make_arya_bot_order_id(tg_id_int, str(first_sid) if first_sid else None)
            except Exception:
                from datetime import datetime as _dt
                import random
                prefix = "AB" if is_admin_manual else "AM"
                order_id = f"{prefix}-{tg_id_int}-{_dt.now().strftime('%d%m')}-{random.randint(10000, 99999)}"

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
            user_name=final_user_name,
            story_name=story_name_str,
            amount=amount or 0,
            order_id=order_id or "OD-COMPLETED",
            story_status=primary_status,
            payment_method=payment_method or "UPI (UTR)",
            verified_by=verified_by or "Auto Verified By System",
            is_ongoing=is_ongoing,
            is_admin_manual=is_admin_manual,
            bot_username=bot_username
        )

        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id": tg_id_int,
                    "text": full_message,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True
                },
                timeout=10
            ) as resp:
                res = await resp.json()
                if res.get("ok"):
                    logger.info(f"Purchase Complete DM successfully sent to user {tg_id_int}")
                else:
                    logger.warning(f"send_purchase_success_dm Telegram API response: {res}")

        # ── Trigger Automatic Instant Delivery if enabled ──
        asyncio.create_task(trigger_auto_delivery_for_order(db, tg_id_int, order_doc))

    except Exception as e:
        logger.error(f"send_purchase_success_dm exception: {e}", exc_info=True)


async def start_auto_delivery_queue_worker(market_clients: dict, mgmt_bot=None, db=None):
    """
    Background worker running inside main.py (arya-premium.service).
    Polls pending_auto_deliveries collection in MongoDB and executes DM delivery
    using active Pyrogram store clients which are members of channel resources.
    """
    logger.info("[AutoDeliveryQueue] Worker loop started in main bot process...")
    if not db:
        try:
            from AryaPremium.database import db as global_db
            db = global_db
        except ImportError:
            try:
                from database import db as global_db
                db = global_db
            except ImportError:
                pass

    while True:
        try:
            await asyncio.sleep(2)
            if not db or not hasattr(db, "db") or db.db is None:
                continue

            job = await db.db.pending_auto_deliveries.find_one_and_update(
                {"status": "pending"},
                {"$set": {"status": "processing", "processed_at": datetime.now(timezone.utc)}},
                sort=[("created_at", 1)]
            )
            if not job:
                continue

            user_id = job.get("user_id")
            story_ids = job.get("story_ids", [])
            logger.info(f"[AutoDeliveryQueue] Picked auto delivery job for user {user_id}, story_ids: {story_ids}")

            _do_dm_delivery = None
            try:
                from plugins.userbot.market_seller import _do_dm_delivery
            except ImportError:
                try:
                    from AryaPremium.plugins.userbot.market_seller import _do_dm_delivery
                except ImportError:
                    pass

            if not _do_dm_delivery:
                logger.warning("[AutoDeliveryQueue] _do_dm_delivery not importable. Re-queueing job...")
                await db.db.pending_auto_deliveries.update_one({"_id": job["_id"]}, {"$set": {"status": "pending"}})
                await asyncio.sleep(5)
                continue

            # Pick an active store bot client that has channel permissions
            selected_client = None
            if market_clients:
                selected_client = next(iter(market_clients.values()), None)
            if not selected_client:
                selected_client = mgmt_bot

            if not selected_client:
                logger.warning(f"[AutoDeliveryQueue] No active Pyrogram store client found. Re-queueing job {job['_id']}...")
                await db.db.pending_auto_deliveries.update_one({"_id": job["_id"]}, {"$set": {"status": "pending"}})
                await asyncio.sleep(5)
                continue

            items = job.get("items") or [{"story_id": sid} for sid in story_ids]
            total_items = len(items)

            # If multiple items: notify user upfront, then deliver one by one with gap
            if total_items > 1:
                try:
                    await selected_client.send_message(
                        user_id,
                        f"📦 <b>Auto Instant Delivery Starting</b>\n\n"
                        f"You have <b>{total_items} items/parts</b> in your order.\n"
                        f"They will be delivered <b>one by one</b> automatically.\n\n"
                        f"⏳ Please wait — your first item is being sent now...",
                        parse_mode="html"
                    )
                    await asyncio.sleep(2)
                except Exception:
                    pass

            for i, itm in enumerate(items, start=1):
                try:
                    sid = itm.get("story_id") or itm.get("id")
                    if not sid:
                        continue
                    part_start = itm.get("start_id")
                    part_end = itm.get("end_id")
                    part_name = itm.get("part_name")

                    from bson.objectid import ObjectId
                    s_obj_id = ObjectId(sid) if isinstance(sid, str) and len(sid) == 24 else sid
                    s_doc = await db.db.premium_stories.find_one({"_id": s_obj_id})
                    if not s_doc:
                        s_doc = await db.db.premium_stories.find_one({"story_id": sid})

                    if s_doc:
                        story_name = s_doc.get("story_name_en") or s_doc.get("story_name") or f"Story {i}"
                        display_name = f"{story_name} ({part_name})" if part_name else story_name
                        logger.info(
                            f"[AutoDeliveryQueue] Delivering item {i}/{total_items} "
                            f"'{display_name}' (range: {part_start} to {part_end}) to user {user_id}..."
                        )

                        # Notify user which story/part is being delivered (only for multi-item orders)
                        if total_items > 1:
                            try:
                                await selected_client.send_message(
                                    user_id,
                                    f"📖 <b>Delivering Item {i} of {total_items}</b>\n"
                                    f"<i>{display_name}</i>\n\n"
                                    f"⏳ Sending files now...",
                                    parse_mode="html"
                                )
                                await asyncio.sleep(1)
                            except Exception:
                                pass

                        # Deliver the exact story/part range
                        await _do_dm_delivery(
                            selected_client,
                            user_id,
                            s_doc,
                            part_start=part_start,
                            part_end=part_end
                        )

                        # Wait between items so delivery is clearly sequential
                        if total_items > 1 and i < total_items:
                            await asyncio.sleep(8)

                except Exception as sid_err:
                    logger.error(f"[AutoDeliveryQueue] Delivery error for item {itm}: {sid_err}", exc_info=True)

            # Final completion message for multi-item orders
            if total_items > 1:
                try:
                    await selected_client.send_message(
                        user_id,
                        f"✅ <b>All {total_items} items delivered successfully!</b>\n\n"
                        f"Enjoy your stories 🎉",
                        parse_mode="html"
                    )
                except Exception:
                    pass

            await db.db.pending_auto_deliveries.update_one({"_id": job["_id"]}, {"$set": {"status": "completed"}})

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[AutoDeliveryQueue] Exception in worker loop: {e}", exc_info=True)
            await asyncio.sleep(5)


async def trigger_auto_delivery_for_order(db, user_id: Union[int, str], order_doc: Optional[Dict[str, Any]] = None):
    """
    Pushes auto-delivery task to pending_auto_deliveries queue in MongoDB.
    The main.py process (arya-premium.service) will pick it up and deliver story files
    using store bots that have permission to read/copy from source channel.
    """
    try:
        if not order_doc or not user_id:
            return

        tg_id_int = int(user_id) if str(user_id).isdigit() else 0
        if not tg_id_int:
            return

        # Check auto_deliver flag (must be explicitly True, defaults to False if off/missing)
        if not order_doc.get("auto_deliver"):
            logger.info(f"[AutoDelivery] Order {order_doc.get('order_id')} has auto_deliver={order_doc.get('auto_deliver')}, skipping auto delivery.")
            return

        story_ids = order_doc.get("story_ids", [])
        if not story_ids and order_doc.get("story_id"):
            story_ids = [order_doc.get("story_id")]

        if not story_ids:
            return

        # Idempotency claim check
        rec_oid = order_doc.get("order_id") or order_doc.get("payment_id") or "_".join(str(s) for s in story_ids)
        delivery_claim_key = f"auto_deliv_{tg_id_int}_{rec_oid}"
        if db and hasattr(db, "db"):
            try:
                claim = await db.db.sent_receipts.find_one_and_update(
                    {"_id": delivery_claim_key},
                    {"$setOnInsert": {"user_id": tg_id_int, "delivered_at": datetime.now(timezone.utc)}},
                    upsert=True
                )
                if claim is not None:
                    logger.info(f"[AutoDelivery] Delivery already queued for claim_key={delivery_claim_key}, skipping.")
                    return

                # Push task to MongoDB pending_auto_deliveries queue
                await db.db.pending_auto_deliveries.insert_one({
                    "claim_key": delivery_claim_key,
                    "user_id": tg_id_int,
                    "story_ids": story_ids,
                    "items": order_doc.get("items", []),
                    "order_id": rec_oid,
                    "status": "pending",
                    "created_at": datetime.now(timezone.utc)
                })
                logger.info(f"[AutoDelivery] Queued auto delivery task for order {rec_oid} to user {tg_id_int}")
            except Exception as c_err:
                logger.warning(f"[AutoDelivery] Claim/queue check exception: {c_err}")

    except Exception as e:
        logger.error(f"[AutoDelivery] Top-level exception in trigger_auto_delivery_for_order: {e}", exc_info=True)
