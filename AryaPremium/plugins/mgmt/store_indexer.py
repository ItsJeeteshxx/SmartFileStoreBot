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


def parse_duration_to_seconds(dur_str: str) -> int:
    """Parses duration string like '1h 32m 23s' or '45m' or '01:32:23' into integer seconds."""
    if not dur_str:
        return 0
    dur_clean = str(dur_str).strip()
    hrs, mins, secs = 0, 0, 0
    h_m = re.search(r'(\d+)\s*h(?:our|ours|r)?', dur_clean, re.I)
    m_m = re.search(r'(\d+)\s*m(?:in|ins|inute|inutes)?', dur_clean, re.I)
    s_m = re.search(r'(\d+)\s*s(?:ec|ecs|econd|econds)?', dur_clean, re.I)
    if h_m: hrs = int(h_m.group(1))
    if m_m: mins = int(m_m.group(1))
    if s_m: secs = int(s_m.group(1))
    if not (h_m or m_m or s_m):
        parts = re.split(r'[:.]', dur_clean)
        if len(parts) == 3 and all(p.strip().isdigit() for p in parts):
            hrs, mins, secs = int(parts[0]), int(parts[1]), int(parts[2])
        elif len(parts) == 2 and all(p.strip().isdigit() for p in parts):
            mins, secs = int(parts[0]), int(parts[1])
    return hrs * 3600 + mins * 60 + secs


def extract_expected_episodes_count(ep_str: str) -> int:
    """Extracts integer total episode count from '70', '1 to 50', '1-50', etc."""
    if not ep_str:
        return 0
    ep_clean = str(ep_str).strip()
    m_range = re.search(r'(?:to|-)\s*(\d+)', ep_clean, re.I)
    if m_range:
        return int(m_range.group(1))
    m_num = re.search(r'(\d+)', ep_clean)
    if m_num:
        return int(m_num.group(1))
    return 0


def parse_episode_numbers(raw_caption: str, fallback_idx: int = 1) -> list[int]:
    """
    Parses episode numbers from video captions like:
    'He Died Unwanted, Came Back Rich Episode - 4 , He Died Unwanted, Came Back Rich Episode - 5' -> [4, 5]
    'Episode 1 to 5' -> [1, 2, 3, 4, 5]
    'He Died Unwanted, Came Back Rich Episode - 1' -> [1]
    'Episode 10' -> [10]
    """
    if not raw_caption:
        return [fallback_idx]
    
    # Check range first (e.g. Episode 1 to 5, 1-10, etc.)
    m_range = re.search(r'(?:(?:Episode|Ep|Part)\s*[-:.]?\s*)?(\d+)\s*(?:to|-)\s*(\d+)', raw_caption, flags=re.I)
    if m_range:
        s, e = int(m_range.group(1)), int(m_range.group(2))
        if 1 <= s <= e <= s + 200:
            return list(range(s, e + 1))

    matches = re.findall(r'(?:Episode|Ep|Part)\s*[-:.]?\s*(\d+)', raw_caption, flags=re.I)
    if matches:
        return [int(m) for m in matches]

    return [fallback_idx]


def is_story_completion_message(text: str) -> bool:
    """
    Detects show completion marker messages like:
    'Hey, the story is complete. Hope you like it 🫶🏻.If you’re looking for another story, then try…    @StoriesByJeetXNew'
    """
    if not text:
        return False
    clean = text.lower()
    markers = [
        "storiesbyjeetxnew",
        "@storiesbyjeetxnew",
        "story is complete",
        "the story is complete",
        "show is complete",
        "the show is complete",
        "hope you like it",
        "looking for another story",
        "then try…",
        "then try...",
        "story complete",
        "show complete",
        "kahaani poori ho gayi",
        "kahani poori ho gayi",
        "kahaani khatam",
        "kahani khatam",
        "end of story",
        "end of show",
    ]
    return any(m in clean for m in markers)


def is_story_header_message(raw_text: str, custom_format: str = None) -> bool:
    """
    Detects whether a message represents a new story/show metadata details header
    (e.g., has 'Story:', 'Show:', 'Title:', '📚', 'Author:', etc.)
    and is NOT a story completion message.
    """
    if not raw_text:
        return False
    if is_story_completion_message(raw_text):
        return False
    
    clean = raw_text.strip()
    if custom_format and any(tag in custom_format for tag in ["{title}", "{author}", "{language}", "{episodes}", "{duration}"]):
        meta = parse_show_caption(clean, custom_format=custom_format)
        if meta.get("title") and (meta.get("author") or meta.get("episodes") or meta.get("language") or meta.get("duration")):
            return True

    # 1. Has story book emoji
    if any(b in clean for b in ["📚", "📗", "📕", "📘", "📙", "📖", "📓"]):
        return True

    # 2. Has Title/Story marker combined with another show metadata field
    has_title_marker = bool(re.search(r'(?:[📚📗📕📘📙📖📓]|\b(?:Story|Title|Show)\s*:)', clean, re.I))
    has_other_marker = bool(re.search(r'(?:[👤🧑👨👥]|\b(?:Author|Writer)\s*:|[🗣🌐🎙]|\b(?:Language|Lang)\s*:|[🎬🎞🎥🍿]|\b(?:Episodes?|Total\s*Episodes?)\s*:|[⏱⏰⏳⌛🕐]|\b(?:Duration|Time|Length)\s*:)', clean, re.I))
    if has_title_marker and has_other_marker:
        return True

    # 3. Explicit 'Story :' or 'Title :' or 'Show :' at line start
    if re.search(r'^\s*(?:📽️|🎬|🎥|📺|🍿|[📚📗📕📘📙📖📓])?\s*(?:Story|Title|Show)\s*:\s*\S+', clean, re.I | re.MULTILINE):
        return True

    return False


def parse_show_caption(raw_text: str, custom_format: str = None) -> dict:
    """
    Extracts structured show metadata (title, author, language, episodes, duration)
    from database channel message captions.
    Supports both custom format templates (e.g. with {title}, {author}, etc.) and smart regex matching.
    """
    if not raw_text:
        return {"title": "", "author": "", "language": "", "episodes": "", "total_episodes": 0, "duration": "", "duration_seconds": 0}

    # If it is a completion message, it is NOT show metadata!
    if is_story_completion_message(raw_text):
        return {"title": "", "author": "", "language": "", "episodes": "", "total_episodes": 0, "duration": "", "duration_seconds": 0}

    res = {
        "title": "",
        "author": "",
        "language": "",
        "episodes": "",
        "total_episodes": 0,
        "duration": "",
        "duration_seconds": 0
    }

    # 1. If custom format template is supplied, attempt matching
    if custom_format and any(tag in custom_format for tag in ["{title}", "{author}", "{language}", "{episodes}", "{duration}"]):
        try:
            pattern = re.escape(custom_format)
            for tag in ["{title}", "{author}", "{language}", "{episodes}", "{duration}"]:
                escaped_tag = re.escape(tag)
                clean_name = tag.strip("{}")
                pattern = pattern.replace(escaped_tag, f"(?P<{clean_name}>.+?)")
            pattern = pattern.replace(r"\ ", r"\s+")
            if pattern.endswith(r".+?)"):
                pattern = pattern[:-4] + r".+)"
            m = re.search(pattern, raw_text, flags=re.DOTALL | re.IGNORECASE)
            if m:
                for k, v in m.groupdict().items():
                    if v:
                        res[k] = v.strip()
        except Exception as e:
            logger.debug(f"[StoreIndexer] Custom format matching failed: {e}")

    # 2. Smart regex extraction with lookahead delimiters
    stop = r'(?=\n|\||[📚📗📕📘📙📖📓👤🧑👨👥🗣🌐🎙🎬🎞🎥🍿⏱⏰⌛⏳🕐📱🖥]|\b(?:Story|Title|Show|Name|Author|Writer|Language|Lang|Episodes?|Ep|Duration|Time|Length|Platform)\s*:|$)'

    if not res["title"]:
        tm = re.search(r'(?:[📚📗📕📘📙📖📓]\s*(?:Story|Title|Show|Name)?\s*:\s*|(?:\bStory|\bTitle)\s*:\s*)(.+?)' + stop, raw_text, flags=re.I)
        if tm:
            res["title"] = _clean_show_title(tm.group(1).strip())
        else:
            cleaned = _clean_show_title(raw_text)
            if cleaned and not re.match(r'^(?:Episode|Ep|Part)\s*[-:.]?\s*\d+', cleaned, re.I):
                res["title"] = cleaned

    if not res["author"]:
        am = re.search(r'(?:[👤🧑👨👥]\s*(?:Author|Writer)?\s*:\s*|(?:\bAuthor|\bWriter)\s*:\s*)(.+?)' + stop, raw_text, flags=re.I)
        if am:
            res["author"] = am.group(1).strip().strip("•-—|/")

    if not res["language"]:
        lm = re.search(r'(?:(?:🗣️|🗣|🌐|🎙️|🎙)\s*(?:Language|Lang)?\s*:\s*|(?:\bLanguage|\bLang)\s*:\s*)(.+?)' + stop, raw_text, flags=re.I)
        if lm:
            res["language"] = lm.group(1).strip().strip("•-—|/")

    if not res["episodes"]:
        em = re.search(r'(?:(?:🎬|🎞️|🎞|🎥|🍿)\s*(?:Total\s*Episodes?|Episodes?|Ep)?\s*:\s*|(?:\bTotal\s*Episodes?|\bEpisodes?)\s*:\s*)(.+?)' + stop, raw_text, flags=re.I)
        if em:
            cand = em.group(1).strip().strip("•-—|/")
            if re.search(r'(?:to|-)\s*\d+|\b\d+\s*episodes?|\btotal\b', cand, re.I) or bool(res["title"]):
                res["episodes"] = cand

    if res["episodes"]:
        res["total_episodes"] = extract_expected_episodes_count(res["episodes"])

    if not res["duration"]:
        dm = re.search(r'(?:(?:⏱️|⏱|⏰|⏳|⌛|🕐)\s*(?:Duration|Length|Time)?\s*:\s*|(?:\bDuration|\bTime)\s*:\s*)(.+?)' + stop, raw_text, flags=re.I)
        if dm:
            res["duration"] = dm.group(1).strip().strip("•-—|/")

    if res["duration"]:
        res["duration_seconds"] = parse_duration_to_seconds(res["duration"])

    return res


def _clean_show_title(raw_text: str) -> str:
    """
    Cleans raw caption/text to extract pure Show Title:
    - Strips leading and trailing emojis (🎬, 🎥, 📺, 🍿, etc.)
    - Removes promo words, dividers (━━━━), footers (Powered by...).
    - Aggressively strips any episode/part/season indicators (e.g. 'Episode - 1', 'Ep 1', 'Part 1', etc.)
      so that episode indicators never leak into the official show title.
    """
    if not raw_text:
        return ""
    
    first_chunk = re.split(r'[\n\r]|📺|━|🤖|📢|💾|⚠️', raw_text)[0].strip()
    if not first_chunk:
        return ""

    title = first_chunk
    title = re.sub(r'^(?:📽️|🎬|🎥|📺|🍿)?\s*(?:show|title|name|story)\s*:\s*', '', title, flags=re.I).strip()
    
    strip_emojis = ["📚", "📗", "📕", "📘", "📙", "📖", "📓", "🎬", "📽️", "🎥", "📺", "🍿", "📹", "🔹", "🔸", "▫️", "▪️", "▶️", "👉", "✨", "🔥", "⚡", "🌟", "👑", "💎"]
    for emo in strip_emojis:
        if title.startswith(emo):
            title = title[len(emo):].strip()
        if title.endswith(emo):
            title = title[:-len(emo)].strip()

    # Split if multiple chained episodes appear (e.g. 'Title Episode - 4 , Title Episode - 5')
    title = re.split(r'(?:Episode|Ep|Part)\s*[-:.]?\s*\d+\s*,\s*', title, flags=re.I)[0].strip()

    # Aggressively strip episode/part/season suffixes:
    # 1. Matches 'Episode - 1', 'Episode 1', 'Ep - 1', 'Ep. 1', 'Part - 1', 'Season 1 Episode 5', etc.
    title = re.sub(r'[\s\-–—:|/\[(]+(?:Episode|Ep|Part|P|Season\s*\d+\s*Episode|S\d+\s*E)\s*[-:.]?\s*\d+.*$', '', title, flags=re.I).strip()
    # 2. Matches trailing 'Episode', 'Ep', 'Part'
    title = re.sub(r'[\s\-–—:|/\[(]+(?:Episode|Ep|Part)\b.*$', '', title, flags=re.I).strip()
    # 3. Matches trailing hyphen followed by numbers (e.g. 'Show Name - 1')
    title = re.sub(r'\s+[-–—]\s*\d+\s*$', '', title).strip()

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


def build_showcase_caption(
    title: str,
    platform: str = "Story TV",
    genre: str = "Drama / Romance",
    duration: str = "Full Show",
    price: int = 19,
    author: str = "",
    language: str = "",
    episodes: str = ""
) -> str:
    """
    Constructs the public showcase channel caption with custom emoji IDs:
    - 5937999673510858217 for Show / Video
    - 6026337676091726218 for Platform
    - 6024065724291488135 for Genre
    - 5807622114424924272 for Duration
    - 5904462880941545555 for Price
    """
    clean_title = _clean_show_title(title)
    cap_lines = [f'<emoji id="5937999673510858217">📽️</emoji> <b>Show :</b> {clean_title}']
    if author:
        cap_lines.append(f'👤 <b>Author :</b> {author}')
    if language:
        cap_lines.append(f'🗣 <b>Language :</b> {language}')
    if episodes:
        cap_lines.append(f'🎬 <b>Episodes :</b> {episodes}')
    cap_lines.extend([
        f'<emoji id="6026337676091726218">🖥</emoji> <b>Platform :</b> {platform}',
        f'<emoji id="6024065724291488135">🧩</emoji> <b>Genre :</b> {genre}',
        f'<emoji id="5807622114424924272">🎬</emoji> <b>Duration :</b> {duration}',
        f'<emoji id="5904462880941545555">💰</emoji> <b>Price :</b> ₹{price}\n',
        f"<blockquote expandable>",
        f"❏ Note: This is a paid show. Access will be available after purchase.",
        f"❏ नोट: यह शो फ्री नहीं है, इसे देखने के लिए खरीदना होगा।",
        f"</blockquote>"
    ])
    return "\n".join(cap_lines)


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


_commit_lock = asyncio.Lock()

# ── Helper to commit grouped show document ──────────────────────────────────
async def _commit_show_record(
    client: Client,
    channel_id: int,
    bot_id: int,
    bot_uname: str,
    active_show: dict,
    default_price: int = 19,
    default_platform: str = "Story TV",
    default_genre: str = "Drama / Romance"
) -> dict:
    """
    Compiles all collected video episodes for active_show, uploads poster to Cloudflare R2,
    stores or updates the show in premium_stories, and returns the final show_doc.
    """
    raw_episodes = active_show.get("episodes") or []
    if not raw_episodes:
        return None

    async with _commit_lock:
        # Deduplicate episodes by message id
        seen_msg_ids = set()
        unique_eps = []
        for ep in raw_episodes:
            if ep["msg_id"] not in seen_msg_ids:
                seen_msg_ids.add(ep["msg_id"])
                unique_eps.append(ep)

        sorted_eps = sorted(unique_eps, key=lambda x: (x.get("part", 0), x.get("msg_id", 0)))
        if not sorted_eps:
            return None

        first_msg_id = sorted_eps[0]["msg_id"]
        last_msg_id = sorted_eps[-1]["msg_id"]
        valid_file_ids = [e["msg_id"] for e in sorted_eps]

        meta = active_show.get("meta") or {}
        show_title = meta.get("title") or active_show.get("title") or f"Show #{first_msg_id}"
        clean_title = _clean_show_title(show_title)
        norm_key = _normalize_title(clean_title)

        author = meta.get("author") or ""
        language = meta.get("language") or "English"
        episodes_str = meta.get("episodes") or str(len(sorted_eps))
        expected_eps = active_show.get("expected_episodes") or len(sorted_eps)
        total_episodes = max(expected_eps, len(sorted_eps))

        total_sec = meta.get("duration_seconds") or sum(e.get("duration", 0) for e in sorted_eps)
        dur_str = meta.get("duration") or _format_video_duration(total_sec)

        # Comprehensive deduplication query across keys
        query_or = []
        if norm_key:
            query_or.append({"clean_title": norm_key})
        if clean_title:
            safe_regex = f"^{re.escape(clean_title)}$"
            query_or.append({"title": {"$regex": safe_regex, "$options": "i"}})
            query_or.append({"story_name_en": {"$regex": safe_regex, "$options": "i"}})
            query_or.append({"story_name_hi": {"$regex": safe_regex, "$options": "i"}})
        if channel_id and first_msg_id:
            query_or.append({"channel_id": channel_id, "start_id": first_msg_id})
            query_or.append({"source": channel_id, "start_id": first_msg_id})
        if channel_id and valid_file_ids:
            query_or.append({"channel_id": channel_id, "valid_file_ids": first_msg_id})
            query_or.append({"source": channel_id, "valid_file_ids": first_msg_id})

        existing = await db.db.premium_stories.find_one({"$or": query_or}) if query_or else None
        is_duplicate = bool(existing)

        poster_msg = active_show.get("poster_msg")
        poster_id = poster_msg.id if poster_msg else first_msg_id

        tg_photo_file_id = ""
        if poster_msg:
            if getattr(poster_msg, 'photo', None):
                tg_photo_file_id = poster_msg.photo.file_id
            elif getattr(poster_msg, 'video', None) and getattr(poster_msg.video, 'thumbs', None):
                tg_photo_file_id = poster_msg.video.thumbs[0].file_id

        # If existing and already has a public CDN URL, reuse poster
        poster_url = (existing.get("poster_url", "") if existing else "") or ""
        if not poster_url or not str(poster_url).startswith("http"):
            try:
                target_media_msg = poster_msg if (poster_msg and (getattr(poster_msg, 'photo', None) or getattr(poster_msg, 'video', None))) else None
                if not target_media_msg:
                    try:
                        first_ep_msg = await client.get_messages(channel_id, first_msg_id)
                        if first_ep_msg and (first_ep_msg.photo or getattr(first_ep_msg.video, 'thumbs', None)):
                            target_media_msg = first_ep_msg
                    except Exception:
                        pass

                if target_media_msg:
                    media_dl = await client.download_media(target_media_msg)
                    if media_dl:
                        try:
                            from AryaPremium.r2_helper import upload_image_to_r2
                        except ImportError:
                            from r2_helper import upload_image_to_r2

                        poster_url = await upload_image_to_r2(
                            media_dl, width=600, height=720, format="WEBP", quality=85, clean_title=clean_title
                        )
                        if not poster_url:
                            try:
                                from AryaPremium.utils import upload_to_catbox
                            except ImportError:
                                from utils import upload_to_catbox
                            poster_url = await upload_to_catbox(media_dl)
                        try:
                            if os.path.exists(media_dl): os.remove(media_dl)
                        except Exception: pass
            except Exception as ex:
                logger.debug(f"[StoreIndexer] R2 poster upload skipped for {clean_title}: {ex}")

        final_poster = poster_url or (existing.get("poster_url", "") if existing else "")
        final_img = final_poster or tg_photo_file_id or (existing.get("image", "") if existing else "")

        parts_list = []
        for idx, ep in enumerate(sorted_eps, start=1):
            parts_list.append({
                "part": idx,
                "episodes_covered": ep.get("episodes_covered", [idx]),
                "msg_id": ep["msg_id"],
                "file_id": ep.get("file_id", ""),
                "file_unique_id": ep.get("file_unique_id", ""),
                "channel_id": channel_id,
                "file_name": ep.get("file_name", f"{clean_title}_Ep{idx}.mp4"),
                "caption": ep.get("caption", "")
            })

        from datetime import datetime, timezone
        now_utc = datetime.now(timezone.utc)

        if existing:
            await db.db.premium_stories.update_one(
                {"_id": existing["_id"]},
                {"$set": {
                    "title": clean_title,
                    "story_name_en": clean_title,
                    "story_name_hi": clean_title,
                    "clean_title": norm_key,
                    "author": author or existing.get("author", ""),
                    "language": language or existing.get("language", "English"),
                    "episodes": episodes_str,
                    "total_episodes": total_episodes,
                    "file_count": len(parts_list),
                    "duration": dur_str,
                    "duration_seconds": total_sec,
                    "source": channel_id,
                    "channel_id": channel_id,
                    "start_id": first_msg_id,
                    "end_id": last_msg_id,
                    "valid_file_ids": valid_file_ids,
                    "parts": parts_list,
                    "poster_file_id": tg_photo_file_id or existing.get("poster_file_id", ""),
                    "poster_msg_id": poster_id,
                    "poster_url": final_poster,
                    "banner_url": final_poster,
                    "image_url": final_poster,
                    "image": final_img,
                    "cover": final_img,
                    "poster": final_img,
                    "banner": final_img,
                    "is_show": True,
                    "status": "Completed",
                    "updated_at": now_utc
                }}
            )
            updated_doc = await db.db.premium_stories.find_one({"_id": existing["_id"]})
            logger.info(f"[StoreIndexer] Updated existing show (duplicate avoided): '{clean_title}' ({len(parts_list)} episodes, IDs #{first_msg_id}..#{last_msg_id})")
            return updated_doc, True

        show_id = str(uuid.uuid4())[:8]
        show_doc = {
            "story_id": show_id,
            "title": clean_title,
            "story_name_en": clean_title,
            "story_name_hi": clean_title,
            "clean_title": norm_key,
            "author": author,
            "language": language,
            "episodes": episodes_str,
            "total_episodes": total_episodes,
            "file_count": len(parts_list),
            "platform": default_platform,
            "genre": default_genre,
            "duration": dur_str,
            "duration_seconds": total_sec,
            "price": default_price,
            "bot_id": int(bot_id),
            "bot_username": bot_uname,
            "channel_id": channel_id,
            "source": channel_id,
            "poster_file_id": tg_photo_file_id,
            "poster_msg_id": poster_id,
            "poster_url": final_poster,
            "banner_url": final_poster,
            "image_url": final_poster,
            "image": final_img,
            "cover": final_img,
            "poster": final_img,
            "banner": final_img,
            "start_id": first_msg_id,
            "end_id": last_msg_id,
            "valid_file_ids": valid_file_ids,
            "parts": parts_list,
            "is_show": True,
            "visibility": "available",
            "status": "Completed",
            "created_at": now_utc,
            "uploaded_at": now_utc
        }
        await db.db.premium_stories.insert_one(show_doc)
        logger.info(f"[StoreIndexer] Indexed new show: '{clean_title}' (ShowID: {show_id}, {len(parts_list)} episodes, IDs #{first_msg_id}..#{last_msg_id})")
        return show_doc, False


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
    Scans a database channel sequentially, groups Poster Image + all consecutive Video episodes
    belonging to the show (delimited by completion text markers or next show poster),
    stores complete records in premium_stories with full parts array, uploads posters to Cloudflare R2,
    and tracks last_scanned_msg_id.
    """
    indexed_count = 0
    duplicate_count = 0
    duplicate_titles = []

    active_show = None

    # Fetch bot document to resolve username and last scanned position
    bot_doc = await db.db.premium_bots.find_one({"$or": [{"id": int(bot_id)}, {"bot_id": int(bot_id)}]})
    bot_cfg = (bot_doc.get("config") or {}) if bot_doc else {}
    bot_uname = bot_doc.get("username", "StoreBot") if bot_doc else "StoreBot"
    scan_fmt = bot_cfg.get("scan_format")

    # Resume from last scanned position if start_msg_id wasn't manually overridden
    saved_last_id = int(bot_cfg.get("last_scanned_msg_id", 0) or 0)
    if start_msg_id == 1 and saved_last_id > 0:
        msg_id = saved_last_id + 1
        logger.info(f"[StoreIndexer] Resuming scan for channel {channel_id} from Msg #{msg_id} (Last scanned: #{saved_last_id})...")
    else:
        msg_id = start_msg_id
        logger.info(f"[StoreIndexer] Scanning DB channel {channel_id} starting from Msg #{msg_id} for bot {bot_id}...")

    consecutive_empty = 0
    max_id = end_msg_id if end_msg_id > 0 else 1000000
    highest_seen_msg_id = saved_last_id

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

            if msg.id > highest_seen_msg_id:
                highest_seen_msg_id = msg.id

            raw_text = msg.text or msg.caption or ""

            # Check 1: Message is Story Completion Marker (End of active show)
            if is_story_completion_message(raw_text):
                logger.info(f"[StoreIndexer] Story completion marker detected at Msg #{msg.id}: {raw_text[:60]}...")
                if active_show and active_show.get("episodes"):
                    res_tuple = await _commit_show_record(
                        client=client,
                        channel_id=channel_id,
                        bot_id=bot_id,
                        bot_uname=bot_uname,
                        active_show=active_show,
                        default_price=default_price,
                        default_platform=default_platform,
                        default_genre=default_genre
                    )
                    if res_tuple:
                        doc, is_dup = res_tuple
                        if is_dup:
                            duplicate_count += 1
                            duplicate_titles.append(doc.get("title", active_show.get("title")))
                        else:
                            indexed_count += 1
                active_show = None
                continue

            # Check 2: Message is New Show Header or Poster Photo
            is_header = is_story_header_message(raw_text, custom_format=scan_fmt)
            is_poster_photo = bool(msg.photo and not is_story_completion_message(raw_text))

            if is_poster_photo or (msg.text and is_header):
                parsed_meta = parse_show_caption(raw_text, custom_format=scan_fmt)
                new_title = parsed_meta.get("title") or _clean_show_title(raw_text)

                # Boundary Check: If an active show already has episodes, finalize it before starting new show!
                if active_show and active_show.get("episodes"):
                    logger.info(f"[StoreIndexer] New show header detected at Msg #{msg.id}. Finalizing previous show '{active_show.get('title')}'...")
                    res_tuple = await _commit_show_record(
                        client=client,
                        channel_id=channel_id,
                        bot_id=bot_id,
                        bot_uname=bot_uname,
                        active_show=active_show,
                        default_price=default_price,
                        default_platform=default_platform,
                        default_genre=default_genre
                    )
                    if res_tuple:
                        doc, is_dup = res_tuple
                        if is_dup:
                            duplicate_count += 1
                            duplicate_titles.append(doc.get("title", active_show.get("title")))
                        else:
                            indexed_count += 1
                    active_show = None
                elif active_show and not active_show.get("episodes"):
                    # Buffer has no episodes yet: merge incoming metadata into it!
                    if new_title: active_show["title"] = new_title
                    if not active_show.get("meta"): active_show["meta"] = {}
                    if parsed_meta.get("author"): active_show["meta"]["author"] = parsed_meta["author"]
                    if parsed_meta.get("language"): active_show["meta"]["language"] = parsed_meta["language"]
                    if parsed_meta.get("episodes"):
                        active_show["meta"]["episodes"] = parsed_meta["episodes"]
                        active_show["expected_episodes"] = parsed_meta.get("total_episodes") or extract_expected_episodes_count(parsed_meta["episodes"])
                    if parsed_meta.get("duration"): active_show["meta"]["duration"] = parsed_meta["duration"]
                    if is_poster_photo: active_show["poster_msg"] = msg
                    continue

                # Initialize new active show buffer
                if new_title or is_poster_photo:
                    active_show = {
                        "title": new_title or f"Show #{msg.id}",
                        "meta": parsed_meta,
                        "poster_msg": msg,
                        "expected_episodes": parsed_meta.get("total_episodes") or extract_expected_episodes_count(parsed_meta.get("episodes")),
                        "episodes": [],
                        "channel_id": channel_id
                    }
                    logger.info(f"[StoreIndexer] Started new show buffer for '{active_show['title']}' (Msg #{msg.id}, Expected eps: {active_show['expected_episodes']})")
                continue

            # Check 3: Message is a Video / Document Video (Episode)
            is_video = bool(msg.video or (msg.document and msg.document.mime_type and "video" in msg.document.mime_type))
            if is_video:
                raw_cap = msg.caption or getattr(msg.document or msg.video, 'file_name', '') or ""
                
                if not active_show:
                    # Video arrived without a preceding poster or header
                    vid_parsed = parse_show_caption(raw_cap, custom_format=scan_fmt)
                    vid_title = vid_parsed.get("title") or _clean_show_title(raw_cap)
                    active_show = {
                        "title": vid_title or f"Show #{msg.id}",
                        "meta": vid_parsed,
                        "poster_msg": msg,
                        "expected_episodes": 0,
                        "episodes": [],
                        "channel_id": channel_id
                    }

                # Deduplicate episode by message ID
                if any(e.get("msg_id") == msg.id for e in active_show["episodes"]):
                    continue

                ep_nums = parse_episode_numbers(raw_cap, fallback_idx=len(active_show["episodes"]) + 1)
                primary_ep = ep_nums[0] if ep_nums else (len(active_show["episodes"]) + 1)
                file_uid = getattr(msg.video or msg.document, 'file_unique_id', '')
                file_id = getattr(msg.video or msg.document, 'file_id', '')
                file_name = getattr(msg.video or msg.document, 'file_name', f"{active_show['title']}_Ep{primary_ep}.mp4")

                active_show["episodes"].append({
                    "part": primary_ep,
                    "episodes_covered": ep_nums,
                    "msg_id": msg.id,
                    "file_id": file_id,
                    "file_unique_id": file_uid,
                    "channel_id": channel_id,
                    "file_name": file_name,
                    "caption": raw_cap,
                    "duration": getattr(msg.video, 'duration', 0) if msg.video else 0
                })
                continue

            if progress_callback and indexed_count % 5 == 0:
                try: await progress_callback(indexed_count, duplicate_count)
                except Exception: pass

        msg_id += 50

    # End of scan: finalize any remaining active show
    if active_show and active_show.get("episodes"):
        res_tuple = await _commit_show_record(
            client=client,
            channel_id=channel_id,
            bot_id=bot_id,
            bot_uname=bot_uname,
            active_show=active_show,
            default_price=default_price,
            default_platform=default_platform,
            default_genre=default_genre
        )
        if res_tuple:
            doc, is_dup = res_tuple
            if is_dup:
                duplicate_count += 1
                duplicate_titles.append(doc.get("title", active_show.get("title")))
            else:
                indexed_count += 1
        active_show = None

    # Persist the highest scanned message ID so we never repeat already scanned messages
    if highest_seen_msg_id > saved_last_id:
        await db.db.premium_bots.update_one(
            {"$or": [{"id": int(bot_id)}, {"bot_id": int(bot_id)}]},
            {"$set": {"config.last_scanned_msg_id": highest_seen_msg_id}}
        )
        await db.db.premium_channels.update_one(
            {"channel_id": channel_id},
            {"$set": {"last_scanned_id": highest_seen_msg_id}}
        )
        logger.info(f"[StoreIndexer] Updated last_scanned_msg_id to #{highest_seen_msg_id} for channel {channel_id}")

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
            price=show.get("price", 19),
            author=show.get("author", ""),
            language=show.get("language", ""),
            episodes=str(show.get("episodes", "")) if show.get("episodes") else ""
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
_active_live_shows = {}  # { channel_id: { "title": ..., "meta": ..., "poster_msg": ..., "expected_episodes": ..., "episodes": [], "time": ... } }
_recently_processed_live_msgs = set()

async def handle_live_channel_show_arrival(client: Client, message: Message):
    """
    Listens live to configured database channels.
    Auto-indexes new poster + video arrivals, groups all episodes of the show,
    and auto-posts the completed show to the Showcase Channel upon completion text marker
    or next show arrival.
    Only active when matching bot has config.auto_index_active == True.
    """
    if not message or not message.chat:
        return
    ch_id = message.chat.id

    # Deduplicate live message arrival events across multiple client instances
    msg_key = f"{ch_id}_{message.id}"
    if msg_key in _recently_processed_live_msgs:
        return
    _recently_processed_live_msgs.add(msg_key)
    if len(_recently_processed_live_msgs) > 2000:
        _recently_processed_live_msgs.clear()

    # Strictly match Show Store bot with Auto Index ACTIVE and matching DB Channel
    matching_bot = await db.db.premium_bots.find_one({
        "config.bot_mode": "show_store",
        "config.auto_index_active": True,
        "config.db_channel_id": ch_id
    })
    if not matching_bot:
        return

    b_id = matching_bot["id"]
    cfg = matching_bot.get("config", {}) or {}
    target_showcase = cfg.get("showcase_channel_id")
    b_uname = matching_bot.get("username", "StoreBot")
    scan_fmt = cfg.get("scan_format")
    def_price = cfg.get("default_price", 19)
    def_platform = cfg.get("platform_name", "Story TV")
    def_genre = cfg.get("genre", "Drama / Romance")

    raw_text = message.text or message.caption or ""

    # 1. Message is Story Completion Marker (End of active show)
    if is_story_completion_message(raw_text):
        active = _active_live_shows.pop(ch_id, None)
        if active and active.get("episodes"):
            logger.info(f"[LiveStoreWatcher] Story completion marker detected for '{active.get('title')}' in DB {ch_id}")
            res_tuple = await _commit_show_record(
                client=client,
                channel_id=ch_id,
                bot_id=b_id,
                bot_uname=b_uname,
                active_show=active,
                default_price=def_price,
                default_platform=def_platform,
                default_genre=def_genre
            )

            # Clear Mini App Cache so new show is instantly visible in Mini App
            try:
                import mini_app_api
                mini_app_api._stories_cache = None
            except Exception:
                pass

            if res_tuple:
                show_doc, is_dup = res_tuple
                if show_doc and target_showcase and not is_dup:
                    try:
                        await publish_show_to_showcase(
                            client=client,
                            target_channel_id=target_showcase,
                            show=show_doc,
                            store_bot_username=b_uname,
                            tutorial_link=cfg.get("tutorial_url", "https://t.me/UseAryaBot")
                        )
                        logger.info(f"[LiveStoreWatcher] Auto-published '{show_doc.get('title')}' to Showcase Channel {target_showcase}")
                    except Exception as ex:
                        logger.error(f"[LiveStoreWatcher] Auto-publish failed: {ex}")
        return

    # 2. Message is New Show Header or Poster Photo
    is_header = is_story_header_message(raw_text, custom_format=scan_fmt)
    is_poster_photo = bool(message.photo and not is_story_completion_message(raw_text))

    if is_poster_photo or (message.text and is_header):
        parsed = parse_show_caption(raw_text, custom_format=scan_fmt)
        t = _clean_show_title(parsed.get("title") or raw_text)
        if t or is_poster_photo:
            prev_act = _active_live_shows.get(ch_id)
            if prev_act and prev_act.get("episodes"):
                # Boundary Check: Previous show had episodes, finalize and commit it!
                logger.info(f"[LiveStoreWatcher] Finalizing previous show '{prev_act.get('title')}' before starting new show '{t}'")
                res_tuple = await _commit_show_record(
                    client=client,
                    channel_id=ch_id,
                    bot_id=b_id,
                    bot_uname=b_uname,
                    active_show=prev_act,
                    default_price=def_price,
                    default_platform=def_platform,
                    default_genre=def_genre
                )
                if res_tuple:
                    prev_doc, is_prev_dup = res_tuple
                    if prev_doc and target_showcase and not is_prev_dup:
                        try:
                            await publish_show_to_showcase(
                                client=client,
                                target_channel_id=target_showcase,
                                show=prev_doc,
                                store_bot_username=b_uname,
                                tutorial_link=cfg.get("tutorial_url", "https://t.me/UseAryaBot")
                            )
                        except Exception as ex:
                            logger.error(f"[LiveStoreWatcher] Auto-publish failed: {ex}")
            elif prev_act and not prev_act.get("episodes"):
                # Buffer has no episodes yet: merge incoming metadata into it
                if t: prev_act["title"] = t
                if parsed.get("author"): prev_act["meta"]["author"] = parsed["author"]
                if parsed.get("language"): prev_act["meta"]["language"] = parsed["language"]
                if parsed.get("episodes"):
                    prev_act["meta"]["episodes"] = parsed["episodes"]
                    prev_act["expected_episodes"] = parsed.get("total_episodes") or extract_expected_episodes_count(parsed["episodes"])
                if parsed.get("duration"): prev_act["meta"]["duration"] = parsed["duration"]
                if message.photo: prev_act["poster_msg"] = message
                return

            _active_live_shows[ch_id] = {
                "title": t or f"Show #{message.id}",
                "meta": parsed,
                "poster_msg": message,
                "expected_episodes": parsed.get("total_episodes") or extract_expected_episodes_count(parsed.get("episodes")),
                "episodes": [],
                "channel_id": ch_id,
                "time": time.time()
            }
            logger.info(f"[LiveStoreWatcher] Started live show buffer for '{_active_live_shows[ch_id]['title']}' (Msg #{message.id}) in DB {ch_id}")
        return

    # 3. Message is Video / Document Video (Episode)
    is_video = bool(message.video or (message.document and message.document.mime_type and "video" in message.document.mime_type))
    if is_video:
        raw_cap = message.caption or getattr(message.document or message.video, 'file_name', '') or ""
        if ch_id not in _active_live_shows:
            vid_parsed = parse_show_caption(raw_cap, custom_format=scan_fmt)
            vid_title = _clean_show_title(vid_parsed.get("title") or raw_cap)
            _active_live_shows[ch_id] = {
                "title": vid_title or f"Show #{message.id}",
                "meta": vid_parsed,
                "poster_msg": message,
                "expected_episodes": 0,
                "episodes": [],
                "channel_id": ch_id,
                "time": time.time()
            }

        # Deduplicate video episode by message ID
        if any(e.get("msg_id") == message.id for e in _active_live_shows[ch_id]["episodes"]):
            return

        ep_nums = parse_episode_numbers(raw_cap, fallback_idx=len(_active_live_shows[ch_id]["episodes"]) + 1)
        primary_ep = ep_nums[0] if ep_nums else (len(_active_live_shows[ch_id]["episodes"]) + 1)
        file_uid = getattr(message.video or message.document, 'file_unique_id', '')
        file_id = getattr(message.video or message.document, 'file_id', '')
        file_name = getattr(message.video or message.document, 'file_name', f"{_active_live_shows[ch_id]['title']}_Ep{primary_ep}.mp4")

        _active_live_shows[ch_id]["episodes"].append({
            "part": primary_ep,
            "episodes_covered": ep_nums,
            "msg_id": message.id,
            "file_id": file_id,
            "file_unique_id": file_uid,
            "channel_id": ch_id,
            "file_name": file_name,
            "caption": raw_cap,
            "duration": getattr(message.video, 'duration', 0) if message.video else 0
        })
        _active_live_shows[ch_id]["time"] = time.time()
        logger.info(f"[LiveStoreWatcher] Appended episode {primary_ep} for '{_active_live_shows[ch_id]['title']}' (Total eps: {len(_active_live_shows[ch_id]['episodes'])})")

        # Update last scanned ID
        await db.db.premium_bots.update_one(
            {"id": int(b_id)},
            {"$set": {"config.last_scanned_msg_id": message.id}}
        )
        return


async def cleanup_duplicate_and_corrupted_shows():
    """
    Cleans up existing shows in MongoDB:
    1. Removes any leaked episode numbers from titles (e.g. 'Simhasanam Episode - 1' -> 'Simhasanam').
    2. Deletes duplicate show records, keeping the most complete document.
    """
    try:
        shows = await db.db.premium_stories.find({"is_show": True}).to_list(length=3000)
        if not shows:
            return

        seen_norm_titles = {}  # { norm_key: doc }
        to_delete_ids = []

        for s in shows:
            s_id = s["_id"]
            orig_title = s.get("title") or s.get("story_name_en") or ""
            clean_title = _clean_show_title(orig_title)
            norm_key = _normalize_title(clean_title)

            # 1. Fix corrupted title in DB if episode number was present
            if clean_title and (clean_title != orig_title or s.get("clean_title") != norm_key):
                logger.info(f"[StoreIndexer] Auto-fixing show title in DB: '{orig_title}' -> '{clean_title}'")
                await db.db.premium_stories.update_one(
                    {"_id": s_id},
                    {"$set": {
                        "title": clean_title,
                        "story_name_en": clean_title,
                        "story_name_hi": clean_title,
                        "clean_title": norm_key
                    }}
                )
                s["title"] = clean_title
                s["clean_title"] = norm_key

            # 2. Identify duplicate documents
            ep_count = len(s.get("parts", [])) or int(s.get("total_episodes", 0) or 0)
            if norm_key in seen_norm_titles:
                prev_doc = seen_norm_titles[norm_key]
                prev_eps = len(prev_doc.get("parts", [])) or int(prev_doc.get("total_episodes", 0) or 0)

                # Keep the one with more episodes or newer
                if ep_count > prev_eps:
                    to_delete_ids.append(prev_doc["_id"])
                    seen_norm_titles[norm_key] = s
                else:
                    to_delete_ids.append(s_id)
            else:
                seen_norm_titles[norm_key] = s

        if to_delete_ids:
            logger.info(f"[StoreIndexer] Removing {len(to_delete_ids)} duplicate show records from MongoDB...")
            await db.db.premium_stories.delete_many({"_id": {"$in": to_delete_ids}})
            logger.info(f"[StoreIndexer] Duplicate shows successfully deleted.")
    except Exception as e:
        logger.error(f"[StoreIndexer] Error cleaning up duplicate shows: {e}", exc_info=True)


