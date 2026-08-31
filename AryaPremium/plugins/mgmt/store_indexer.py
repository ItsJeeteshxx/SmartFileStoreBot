import os
import re
import time
import uuid
import asyncio
import logging
from pyrogram import Client, filters
from pyrogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from pyrogram.errors import FloodWait, RPCError

try:
    from AryaPremium.database import db
    from AryaPremium.config import Config
except ImportError:
    from database import db
    from config import Config

logger = logging.getLogger("AryaPremiumStoreIndexer")
PM = "html"


def _clean_show_title(raw_text: str) -> str:
    """
    Cleans raw caption/text to extract pure Show Title:
    - Strips leading and trailing emojis (🎬, 🎥, 📺, 🍿, etc.)
    - Removes promo words, dividers (━━━━), footers (Powered by...).
    """
    if not raw_text:
        return ""
    
    first_chunk = re.split(r'[\n\r]|📺|━|🤖|📢|💾|⚠️', raw_text)[0].strip()
    if not first_chunk:
        return ""

    title = first_chunk
    title = re.sub(r'^(?:📽️|🎬|🎥|📺|🍿)?\s*(?:show|title|name)\s*:\s*', '', title, flags=re.I).strip()
    
    strip_emojis = ["🎬", "📽️", "🎥", "📺", "🍿", "📹", "🔹", "🔸", "▫️", "▪️", "▶️", "👉", "✨", "🔥", "⚡", "🌟", "👑", "💎"]
    for emo in strip_emojis:
        if title.startswith(emo):
            title = title[len(emo):].strip()
        if title.endswith(emo):
            title = title[:-len(emo)].strip()

    title = re.sub(r'\s+', ' ', title).strip()
    return title


def _normalize_title(title: str) -> str:
    """Creates normalized lowercase key for deduplication."""
    cleaned = re.sub(r'[^a-zA-Z0-9]', '', title.lower())
    return cleaned


def _format_video_duration(seconds: int) -> str:
    """Formats duration into friendly readable string (e.g. 01h 24m or 45m 20s)."""
    if not seconds or seconds <= 0:
        return "Full Show"
    mins = seconds // 60
    secs = seconds % 60
    hrs = mins // 60
    rem_mins = mins % 60
    if hrs > 0:
        return f"{hrs}h {rem_mins}m" if rem_mins > 0 else f"{hrs}h"
    return f"{mins}m {secs}s" if secs > 0 else f"{mins}m"


def process_poster_image(input_path: str, target_size=(600, 720)) -> str:
    """
    Resizes image to exactly 600x720 and applies subtle enhancement.
    """
    try:
        from PIL import Image, ImageEnhance, ImageFilter
        with Image.open(input_path) as img:
            img = img.convert("RGB")
            img_resized = img.resize(target_size, Image.Resampling.LANCZOS)
            enh_contrast = ImageEnhance.Contrast(img_resized)
            img_contrast = enh_contrast.enhance(1.05)
            enh_sharpness = ImageEnhance.Sharpness(img_contrast)
            img_final = enh_sharpness.enhance(1.10)

            out_path = f"{os.path.splitext(input_path)[0]}_enhanced_600x720.jpg"
            img_final.save(out_path, format="JPEG", quality=92, optimize=True)
            return out_path
    except Exception as e:
        logger.warning(f"PIL Enhancement failed: {e}")
        return input_path


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
    buy_url = f"https://t.me/{bot_uname}?start=s_{show_id}"
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
    bot_id: int,
    start_msg_id: int = 1,
    end_msg_id: int = 0,
    default_price: int = 19,
    default_platform: str = "Story TV",
    default_genre: str = "Drama / Romance",
    progress_callback = None
) -> dict:
    """
    Scans a database channel sequentially, pairs Poster Image + Video files,
    creates clean records in premium_stories, and returns statistics.
    """
    indexed_count = 0
    duplicate_count = 0
    duplicate_titles = []

    current_poster_msg = None
    current_poster_title = ""

    logger.info(f"[StoreIndexer] Scanning DB channel {channel_id} for bot {bot_id}...")
    msg_id = start_msg_id
    consecutive_empty = 0
    max_id = end_msg_id if end_msg_id > 0 else 1000000

    while msg_id <= max_id:
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
                
                duration_sec = 0
                if msg.video:
                    duration_sec = getattr(msg.video, 'duration', 0) or 0
                duration_str = _format_video_duration(duration_sec)

                show_title = current_poster_title or vid_title or f"Show #{msg.id}"
                clean_title = _clean_show_title(show_title)
                norm_key = _normalize_title(clean_title)

                # Deduplication Check
                existing = await db.db.premium_stories.find_one({"clean_title": norm_key})
                if existing:
                    duplicate_count += 1
                    duplicate_titles.append(clean_title)
                    continue

                poster_id = current_poster_msg.id if current_poster_msg else msg.id
                show_id = str(uuid.uuid4())[:8]

                show_doc = {
                    "story_id": show_id,
                    "title": clean_title,
                    "clean_title": norm_key,
                    "platform": default_platform,
                    "genre": default_genre,
                    "duration": duration_str,
                    "duration_seconds": duration_sec,
                    "price": default_price,
                    "bot_id": int(bot_id),
                    "channel_id": channel_id,
                    "poster_msg_id": poster_id,
                    "parts": [{
                        "part": 1,
                        "file_id": getattr(msg.video or msg.document, 'file_id', ''),
                        "file_unique_id": getattr(msg.video or msg.document, 'file_unique_id', ''),
                        "msg_id": msg.id,
                        "channel_id": channel_id,
                        "file_name": getattr(msg.video or msg.document, 'file_name', f"{clean_title}.mp4")
                    }],
                    "is_show": True,
                    "created_at": time.time()
                }

                await db.db.premium_stories.insert_one(show_doc)
                indexed_count += 1
                logger.info(f"[StoreIndexer] Indexed #{indexed_count}: '{clean_title}' (ShowID: {show_id})")

                current_poster_msg = None
                current_poster_title = ""

            if progress_callback and indexed_count % 10 == 0:
                try: await progress_callback(indexed_count, duplicate_count)
                except Exception: pass

        msg_id += 50

    return {
        "indexed": indexed_count,
        "duplicates": duplicate_count,
        "duplicate_titles": duplicate_titles
    }


# ── Public Showcase Channel Publisher ─────────────────────────────────────────
async def publish_show_to_showcase(
    client: Client,
    target_channel_id: int,
    show: dict,
    store_bot_username: str,
    tutorial_link: str = "https://t.me/UseAryaBot"
) -> bool:
    """
    Publishes a show to the public showcase channel with 600x720 enhanced poster,
    exact custom emojis, and 2-row buttons.
    """
    db_channel_id = show.get("channel_id")
    poster_msg_id = show.get("poster_msg_id") or (show.get("parts", [{}])[0].get("msg_id"))
    show_id = show.get("story_id") or str(show.get("_id"))

    temp_poster_path = None
    processed_poster_path = None

    try:
        if db_channel_id and poster_msg_id:
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
        logger.error(f"[Publisher] Failed to publish show {show.get('title')}: {e}")
        return False
    finally:
        for p in [temp_poster_path, processed_poster_path]:
            if p and os.path.exists(p) and "raw_" in p:
                try: os.remove(p)
                except Exception: pass


# ── Live Auto-Poster for Database Channel Arrivals ───────────────────────────
_pending_live_posters = {} # { channel_id: { "msg_id": msg_id, "title": title, "time": timestamp } }

async def handle_live_channel_show_arrival(client: Client, message: Message):
    """
    Listens live to configured database channels.
    Auto-indexes new poster + video arrivals and auto-posts to the Showcase Channel.
    """
    if not message or not message.chat:
        return
    ch_id = message.chat.id

    matching_bot = await db.db.premium_bots.find_one({
        "config.bot_mode": "show_store",
        "config.db_channel_id": ch_id
    })
    if not matching_bot:
        return

    b_id = matching_bot["id"]
    cfg = matching_bot.get("config", {}) or {}
    target_showcase = cfg.get("showcase_channel_id")
    if not target_showcase:
        return
    b_uname = matching_bot.get("username", "StoreBot")

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
        poster_id = message.id
        show_title = vid_title or f"Show #{message.id}"
        if pending and (time.time() - pending.get("time", 0)) < 600:
            poster_id = pending["msg_id"]
            show_title = pending["title"] or show_title
            del _pending_live_posters[ch_id]

        clean_t = _clean_show_title(show_title)
        norm_key = _normalize_title(clean_t)

        existing = await db.db.premium_stories.find_one({"clean_title": norm_key})
        if existing:
            logger.warning(f"[LiveStoreWatcher] Duplicate show skipped: {clean_t}")
            return

        duration_sec = 0
        if message.video:
            duration_sec = getattr(message.video, 'duration', 0) or 0
        duration_str = _format_video_duration(duration_sec)

        show_id = str(uuid.uuid4())[:8]
        def_price = cfg.get("default_price", 19)
        def_platform = cfg.get("platform_name", "Story TV")

        show_doc = {
            "story_id": show_id,
            "title": clean_t,
            "clean_title": norm_key,
            "platform": def_platform,
            "genre": "Drama / Romance",
            "duration": duration_str,
            "duration_seconds": duration_sec,
            "price": def_price,
            "bot_id": int(b_id),
            "channel_id": ch_id,
            "poster_msg_id": poster_id,
            "parts": [{
                "part": 1,
                "file_id": getattr(message.video or message.document, 'file_id', ''),
                "file_unique_id": getattr(message.video or message.document, 'file_unique_id', ''),
                "msg_id": message.id,
                "channel_id": ch_id,
                "file_name": getattr(message.video or message.document, 'file_name', f"{clean_t}.mp4")
            }],
            "is_show": True,
            "created_at": time.time()
        }

        await db.db.premium_stories.insert_one(show_doc)
        logger.info(f"[LiveStoreWatcher] Auto-indexed live show '{clean_t}' (ShowID: {show_id})")

        try:
            await publish_show_to_showcase(
                client=client,
                target_channel_id=target_showcase,
                show=show_doc,
                store_bot_username=b_uname,
                tutorial_link=cfg.get("tutorial_url", "https://t.me/UseAryaBot")
            )
            logger.info(f"[LiveStoreWatcher] Auto-published '{clean_t}' to Showcase Channel {target_showcase}")
        except Exception as e:
            logger.error(f"[LiveStoreWatcher] Auto-publish failed: {e}")
