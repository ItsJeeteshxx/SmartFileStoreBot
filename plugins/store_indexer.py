import os
import re
import time
import uuid
import asyncio
import logging
from pyrogram import Client, filters
from pyrogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from pyrogram.errors import FloodWait, RPCError

from config import Config
from database import db
from plugins.url_bypass import process_poster_image

logger = logging.getLogger("StoreIndexer")
PM = "html"

CANCEL_BTN = KeyboardButton("⛔ Cancel")
UNDO_BTN   = KeyboardButton("↩️ Back")

def _is_cancel(text: str) -> bool:
    if not text:
        return False
    return any(w in text.lower() for w in ["cancel", "⛔", "/cancel"])


def _clean_show_title(raw_text: str) -> str:
    """
    Cleans raw caption/text to extract pure Show Title:
    - Strips leading and trailing emojis (🎬, 🎥, 📺, 🍿, etc.)
    - Removes promo words, dividers (━━━━), footers (Powered by...).
    """
    if not raw_text:
        return ""
    
    # First split by newline or common inline promo delimiters
    first_chunk = re.split(r'[\n\r]|📺|━|🤖|📢|💾|⚠️', raw_text)[0].strip()
    if not first_chunk:
        return ""

    title = first_chunk
    # Remove prefix tags like "📽️ Show :", "Show:", "Title:"
    title = re.sub(r'^(?:📽️|🎬|🎥|📺|🍿)?\s*(?:show|title|name)\s*:\s*', '', title, flags=re.I).strip()
    
    # Strip emojis
    strip_emojis = ["🎬", "📽️", "🎥", "📺", "🍿", "📹", "🔹", "🔸", "▫️", "▪️", "▶️", "👉", "✨", "🔥", "⚡", "🌟", "👑", "💎"]
    for emo in strip_emojis:
        if title.startswith(emo):
            title = title[len(emo):].strip()
        if title.endswith(emo):
            title = title[:-len(emo)].strip()

    # Remove extra spaces
    title = re.sub(r'\s+', ' ', title).strip()
    return title


def _normalize_title(title: str) -> str:
    """Creates normalized lowercase key for deduplication."""
    cleaned = re.sub(r'[^a-zA-Z0-9]', '', title.lower())
    return cleaned


def _format_video_duration(seconds: int) -> str:
    """Formats duration into friendly readable string (e.g. 45 min 20 sec)."""
    if not seconds or seconds <= 0:
        return "Full Movie / Episodes"
    mins = seconds // 60
    secs = seconds % 60
    hrs = mins // 60
    rem_mins = mins % 60
    if hrs > 0:
        return f"{hrs}h {rem_mins}m {secs}s" if secs > 0 else f"{hrs}h {rem_mins}m"
    return f"{mins}m {secs}s" if secs > 0 else f"{mins}m"


def build_showcase_caption(title: str, platform: str = "Story TV", genre: str = "Drama / Romance", duration: str = "Full Show", price: int = 19) -> str:
    """
    Constructs the exact requested public showcase channel caption with custom emoji IDs:
    - 5937999673510858217 for Show / Video
    - 6026337676091726218 for Platform
    - 6024065724291488135 for Genre
    - 5807622114424924272 for Duration
    - 5904462880941545555 for Price
    """
    clean_title = _clean_show_title(title)
    caption = (
        f'<emoji id="5937999673510858217">📽️</emoji> <b>Show :</b> {clean_title}\n'
        f'<emoji id="6026337676091726218">🖥</emoji> <b>Platform :</b> {platform}\n'
        f'<emoji id="6024065724291488135">🧩</emoji> <b>Genre :</b> {genre}\n'
        f'<emoji id="5807622114424924272">🎬</emoji> <b>Duration :</b> {duration}\n'
        f'<emoji id="5904462880941545555">💰</emoji> <b>Price :</b> ₹{price}\n\n'
        f"<blockquote expandable>\n"
        f"❏ Note: This is a paid show. Access will be available after purchase.\n"
        f"❏ नोट: यह शो फ्री नहीं है, इसे देखने के लिए खरीदना होगा।\n"
        f"</blockquote>"
    )
    return caption


def build_showcase_buttons(show_id: str, store_bot_username: str, tutorial_url: str = "https://t.me/UseAryaBot") -> tuple[InlineKeyboardMarkup, list]:
    """
    Constructs the exact requested 2-row inline keyboard with custom emoji icons:
    Row 1: [ 🛍️ Buy Now ] (6030664675253820292)
    Row 2: [ 🔎 Search ] (6267186570034419608) [ 📹 Tutorial ] (6266794310671275367)
    """
    bot_uname = store_bot_username.strip().lstrip('@')
    buy_url = f"https://t.me/{bot_uname}?start=buy_{show_id}"
    search_url = f"https://t.me/{bot_uname}?start=search"
    if not tutorial_url:
        tutorial_url = f"https://t.me/{bot_uname}?start=help"

    buttons = [
        [InlineKeyboardButton("🛍️ Buy Now", url=buy_url)],
        [
            InlineKeyboardButton("🔎 Search", url=search_url),
            InlineKeyboardButton("📹 Tutorial", url=tutorial_url)
        ]
    ]

    api_buttons = [
        [{"text": "Buy Now", "url": buy_url, "icon_custom_emoji_id": "6030664675253820292"}],
        [
            {"text": "Search", "url": search_url, "icon_custom_emoji_id": "6267186570034419608"},
            {"text": "Tutorial", "url": tutorial_url, "icon_custom_emoji_id": "6266794310671275367"}
        ]
    ]
    return InlineKeyboardMarkup(buttons), api_buttons


# ── Database Channel Sequential Auto-Scanner ──────────────────────────────────
async def scan_and_index_channel(
    client: Client,
    channel_id: int,
    start_msg_id: int = 1,
    end_msg_id: int = 0,
    default_price: int = 19,
    default_platform: str = "Story TV",
    default_genre: str = "Drama / Romance",
    progress_callback = None
) -> dict:
    """
    Scans a database channel from start_msg_id to end_msg_id.
    Pairs Poster Image Messages with following Video Messages.
    Saves clean indexed shows into MongoDB and returns statistics.
    """
    indexed_count = 0
    duplicate_count = 0
    skipped_count = 0
    duplicate_titles = []

    current_poster_msg = None
    current_poster_title = ""

    # Fetch total or message history
    logger.info(f"[StoreIndexer] Starting scan on channel {channel_id} (Range: {start_msg_id} -> {end_msg_id or 'END'})...")
    
    # We iterate messages using get_messages or get_chat_history
    msg_id = start_msg_id
    consecutive_empty = 0
    max_id = end_msg_id if end_msg_id > 0 else 1000000

    while msg_id <= max_id:
        # Fetch batch of 50 messages
        batch_ids = list(range(msg_id, min(msg_id + 50, max_id + 1)))
        try:
            msgs = await client.get_messages(channel_id, batch_ids)
        except FloodWait as fw:
            await asyncio.sleep(fw.value)
            continue
        except Exception as e:
            logger.warning(f"[StoreIndexer] Error fetching batch {msg_id}: {e}")
            msgs = []

        if not msgs:
            consecutive_empty += 1
            if consecutive_empty >= 5 and end_msg_id == 0:
                break
            msg_id += 50
            continue

        consecutive_empty = 0

        for msg in msgs:
            if not msg or msg.empty:
                continue

            # Case 1: Message is a Photo / Poster
            if msg.photo:
                raw_caption = msg.caption or ""
                poster_title = _clean_show_title(raw_caption)
                if poster_title:
                    current_poster_msg = msg
                    current_poster_title = poster_title

            # Case 2: Message is a Video / Document Video
            elif msg.video or (msg.document and msg.document.mime_type and "video" in msg.document.mime_type):
                raw_cap = msg.caption or getattr(msg.document or msg.video, 'file_name', '') or ""
                vid_title = _clean_show_title(raw_cap)
                
                # Check duration
                duration_sec = 0
                if msg.video:
                    duration_sec = getattr(msg.video, 'duration', 0) or 0
                duration_str = _format_video_duration(duration_sec)

                # Pair with preceding poster or video's own title
                show_title = current_poster_title or vid_title or f"Show #{msg.id}"
                clean_title = _clean_show_title(show_title)
                norm_key = _normalize_title(clean_title)

                # Deduplication Check
                existing = await db.get_store_show_by_title(norm_key)
                if existing:
                    duplicate_count += 1
                    duplicate_titles.append(clean_title)
                    logger.warning(f"[StoreIndexer] Duplicate show skipped: {clean_title} (Msg ID: {msg.id})")
                    continue

                poster_id = current_poster_msg.id if current_poster_msg else msg.id
                show_id = str(uuid.uuid4())[:8]

                show_doc = {
                    "show_id": show_id,
                    "title": clean_title,
                    "clean_title": norm_key,
                    "platform": default_platform,
                    "genre": default_genre,
                    "duration": duration_str,
                    "duration_seconds": duration_sec,
                    "price": default_price,
                    "channel_id": channel_id,
                    "poster_msg_id": poster_id,
                    "video_msg_ids": [msg.id],
                    "video_msg_id": msg.id,
                    "created_at": time.time()
                }

                await db.save_store_show(show_doc)
                indexed_count += 1
                logger.info(f"[StoreIndexer] Indexed #{indexed_count}: '{clean_title}' (ID: {show_id}, Poster: {poster_id}, Video: {msg.id})")

                # Reset current poster so next show needs new poster
                current_poster_msg = None
                current_poster_title = ""

            if progress_callback and indexed_count % 10 == 0:
                try:
                    await progress_callback(indexed_count, duplicate_count)
                except Exception:
                    pass

        msg_id += 50

    return {
        "indexed": indexed_count,
        "duplicates": duplicate_count,
        "duplicate_titles": duplicate_titles[:10]
    }


# ── Public Showcase Channel Publisher ─────────────────────────────────────────
async def publish_show_to_showcase(
    client: Client,
    target_channel_id: int,
    show_id: str,
    store_bot_username: str,
    tutorial_link: str = "https://t.me/UseAryaBot"
) -> bool:
    """
    Fetches the indexed show, processes poster to 600x720, and publishes
    it to the public showcase channel with full buttons and caption.
    """
    show = await db.get_store_show(show_id)
    if not show:
        logger.error(f"[Publisher] Show {show_id} not found in database.")
        return False

    db_channel_id = show["channel_id"]
    poster_msg_id = show.get("poster_msg_id") or show.get("video_msg_id")

    # Download poster image from database channel
    temp_poster_path = None
    processed_poster_path = None

    try:
        poster_msg = await client.get_messages(db_channel_id, poster_msg_id)
        if poster_msg and (poster_msg.photo or poster_msg.video):
            os.makedirs("downloads/store_posters", exist_ok=True)
            temp_poster_path = await client.download_media(poster_msg, file_name=f"downloads/store_posters/raw_{show_id}.jpg")
            if temp_poster_path and os.path.exists(temp_poster_path):
                processed_poster_path = process_poster_image(temp_poster_path, target_size=(600, 720))

        caption = build_showcase_caption(
            title=show["title"],
            platform=show.get("platform", "Story TV"),
            genre=show.get("genre", "Drama / Romance"),
            duration=show.get("duration", "Full Show"),
            price=show.get("price", 19)
        )

        buttons, api_buttons = build_showcase_buttons(
            show_id=show_id,
            store_bot_username=store_bot_username,
            tutorial_url=tutorial_link
        )

        poster_to_send = processed_poster_path if (processed_poster_path and os.path.exists(processed_poster_path)) else (temp_poster_path if (temp_poster_path and os.path.exists(temp_poster_path)) else None)

        if poster_to_send:
            try:
                from plugins.share_bot import send_or_edit_with_custom_icons
                await client.send_photo(
                    chat_id=target_channel_id,
                    photo=poster_to_send,
                    caption=caption,
                    parse_mode=PM,
                    reply_markup=buttons
                )
            except Exception as e:
                logger.warning(f"[Publisher] send_photo fallback: {e}")
                await client.send_message(chat_id=target_channel_id, text=caption, parse_mode=PM, reply_markup=buttons)
        else:
            await client.send_message(
                chat_id=target_channel_id,
                text=caption,
                parse_mode=PM,
                reply_markup=buttons
            )

        return True

    except Exception as e:
        logger.error(f"[Publisher] Failed to publish show {show_id}: {e}")
        return False
    finally:
        for p in [temp_poster_path, processed_poster_path]:
            if p and os.path.exists(p) and "raw_" in p:
                try: os.remove(p)
                except Exception: pass


# ── Live Auto-Poster for Database Channel Arrivals ───────────────────────────
_pending_live_posters = {} # { channel_id: { "msg_id": msg_id, "title": title, "time": timestamp } }

@Client.on_message(filters.channel)
async def auto_index_live_channel_listener(client: Client, message: Message):
    """
    Listens live to any configured database channels.
    When a new show (poster + video) arrives:
    1. Indexes it into MongoDB.
    2. Generates 600x720 enhanced poster.
    3. Automatically publishes to the configured showcase channel!
    """
    if not message or not message.chat:
        return
    ch_id = message.chat.id

    # Check if this channel is a configured db_channel for any store bot
    bots = await db.get_share_bots()
    matching_bot = None
    target_showcase = None
    b_uname = None

    for b in bots:
        b_id_str = str(b.get("id"))
        cfg = await db.get_store_bot_config(b_id_str)
        if cfg.get("is_store_mode") and cfg.get("db_channel_id") == ch_id:
            matching_bot = b_id_str
            target_showcase = cfg.get("showcase_channel_id")
            b_uname = b.get("username", "StoreBot")
            break

    if not matching_bot or not target_showcase:
        return

    # 1. Poster Photo arrived
    if message.photo:
        raw_cap = message.caption or ""
        t = _clean_show_title(raw_cap)
        if t:
            _pending_live_posters[ch_id] = {
                "msg_id": message.id,
                "title": t,
                "time": time.time()
            }
            logger.info(f"[LiveStoreWatcher] Cached pending poster for '{t}' (Msg: {message.id}) in DB {ch_id}")
        return

    # 2. Video arrived
    elif message.video or (message.document and message.document.mime_type and "video" in message.document.mime_type):
        raw_cap = message.caption or getattr(message.document or message.video, 'file_name', '') or ""
        vid_title = _clean_show_title(raw_cap)
        
        pending = _pending_live_posters.get(ch_id)
        # Check if pending poster was within last 10 minutes
        poster_id = message.id
        show_title = vid_title or f"Show #{message.id}"
        if pending and (time.time() - pending.get("time", 0)) < 600:
            poster_id = pending["msg_id"]
            show_title = pending["title"] or show_title
            del _pending_live_posters[ch_id]

        clean_t = _clean_show_title(show_title)
        norm_key = _normalize_title(clean_t)

        # Deduplication check
        existing = await db.get_store_show_by_title(norm_key)
        if existing:
            logger.warning(f"[LiveStoreWatcher] Duplicate show skipped: {clean_t}")
            return

        duration_sec = 0
        if message.video:
            duration_sec = getattr(message.video, 'duration', 0) or 0
        duration_str = _format_video_duration(duration_sec)

        show_id = str(uuid.uuid4())[:8]
        cfg = await db.get_store_bot_config(matching_bot)
        def_price = cfg.get("default_price", 19)

        show_doc = {
            "show_id": show_id,
            "title": clean_t,
            "clean_title": norm_key,
            "platform": "Story TV",
            "genre": "Drama / Romance",
            "duration": duration_str,
            "duration_seconds": duration_sec,
            "price": def_price,
            "channel_id": ch_id,
            "poster_msg_id": poster_id,
            "video_msg_ids": [message.id],
            "video_msg_id": message.id,
            "created_at": time.time()
        }

        await db.save_store_show(show_doc)
        logger.info(f"[LiveStoreWatcher] Auto-indexed new live show '{clean_t}' (ID: {show_id})")

        # Auto-publish to showcase channel
        try:
            await publish_show_to_showcase(
                client=client,
                target_channel_id=target_showcase,
                show_id=show_id,
                store_bot_username=b_uname,
                tutorial_link=cfg.get("tutorial_url", "https://t.me/UseAryaBot")
            )
            logger.info(f"[LiveStoreWatcher] Auto-published '{clean_t}' to Showcase Channel {target_showcase}")
        except Exception as e:
            logger.error(f"[LiveStoreWatcher] Auto-publish failed: {e}")
