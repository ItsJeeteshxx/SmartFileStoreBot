import io
import os
import re
import logging
import httpx
import asyncio
from PIL import Image, ImageEnhance

logger = logging.getLogger("AryaPosterHelper")

def overlay_watermark(banner_bytes: bytes, watermark_path: str, position: str = "bottom_right", opacity: float = 0.8) -> bytes:
    """
    Overlays a PNG watermark image onto a banner image.
    Calculates scale dynamically to keep the watermark proportional.
    """
    try:
        banner = Image.open(io.BytesIO(banner_bytes)).convert("RGBA")
    except Exception as e:
        logger.error(f"Failed to load banner image for watermarking: {e}")
        return banner_bytes

    if not os.path.exists(watermark_path):
        logger.warning(f"Watermark file not found at: {watermark_path} - skipping overlay")
        # Return banner converted back to RGB bytes
        out_buf = io.BytesIO()
        banner.convert("RGB").save(out_buf, format="JPEG", quality=85)
        return out_buf.getvalue()

    try:
        watermark = Image.open(watermark_path).convert("RGBA")
    except Exception as e:
        logger.error(f"Failed to open watermark file: {e}")
        out_buf = io.BytesIO()
        banner.convert("RGB").save(out_buf, format="JPEG", quality=85)
        return out_buf.getvalue()

    try:
        # Scale watermark to fit banner (e.g., width of watermark = 25% of banner width)
        b_width, b_height = banner.size
        w_width, w_height = watermark.size

        scale_factor = (b_width * 0.25) / w_width
        new_w = max(10, int(w_width * scale_factor))
        new_h = max(10, int(w_height * scale_factor))
        watermark = watermark.resize((new_w, new_h), Image.Resampling.LANCZOS)

        # Apply opacity to watermark alpha channel
        if opacity < 1.0:
            alpha = watermark.split()[3]
            alpha = ImageEnhance.Brightness(alpha).enhance(opacity)
            watermark.putalpha(alpha)

        # Calculate coordinates based on position
        margin = 20
        if position == "center":
            x = (b_width - new_w) // 2
            y = (b_height - new_h) // 2
        elif position == "top_left":
            x = margin
            y = margin
        elif position == "top_right":
            x = b_width - new_w - margin
            y = margin
        elif position == "bottom_left":
            x = margin
            y = b_height - new_h - margin
        else:  # bottom_right
            x = b_width - new_w - margin
            y = b_height - new_h - margin

        # Create transparent layer and paste watermark
        watermark_layer = Image.new("RGBA", banner.size, (0, 0, 0, 0))
        watermark_layer.paste(watermark, (x, y))

        # Composite the images
        combined = Image.alpha_composite(banner, watermark_layer)

        # Save back as JPEG bytes
        out_buf = io.BytesIO()
        combined.convert("RGB").save(out_buf, format="JPEG", quality=85)
        return out_buf.getvalue()
    except Exception as e:
        logger.error(f"Error during watermark overlay paste: {e}")
        out_buf = io.BytesIO()
        banner.convert("RGB").save(out_buf, format="JPEG", quality=85)
        return out_buf.getvalue()

def to_bold_serif(text: str) -> str:
    """
    Converts alphanumeric ASCII characters to Mathematical Bold Serif characters.
    """
    res = []
    for char in text:
        o = ord(char)
        if 65 <= o <= 90:  # A-Z
            res.append(chr(o + 119713))
        elif 97 <= o <= 122:  # a-z
            res.append(chr(o + 119711))
        elif 48 <= o <= 57:  # 0-9
            res.append(chr(o + 120734))
        else:
            res.append(char)
    return "".join(res)

def to_monospace(text: str) -> str:
    """
    Converts alphanumeric ASCII characters to Mathematical Monospace (typewriter) characters.
    Correct Unicode offsets:
      A-Z → U+1D670..U+1D689  offset = 120367
      a-z → U+1D68A..U+1D6A3  offset = 120361
      0-9 → U+1D7F6..U+1D7FF  offset = 120774
    """
    res = []
    for char in text:
        o = ord(char)
        if 65 <= o <= 90:   # A-Z
            res.append(chr(o + 120367))
        elif 97 <= o <= 122:  # a-z
            res.append(chr(o + 120361))
        elif 48 <= o <= 57:  # 0-9
            res.append(chr(o + 120774))
        else:
            res.append(char)
    return "".join(res)

def escape_html(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

async def send_story_to_channel(bot_token: str, channel_id: str, story_doc: dict, watermark_config: dict = None) -> dict:
    """
    Downloads banner, applies watermark (if enabled), builds keyboard, and posts to Telegram channel.
    Returns dict with success status and message_id if successful.
    """
    story_name = story_doc.get("story_name_en") or story_doc.get("title") or "Unknown Story"
    status = story_doc.get("status") or "Ongoing"
    platform = story_doc.get("platform") or "N/A"
    genre = story_doc.get("genre") or "N/A"
    price = str(story_doc.get("price") or "0")
    desc = story_doc.get("description") or ""

    # Extract first genre tag
    first_genre = "N/A"
    if genre and genre != "N/A":
        parts = re.split(r'[,/;|\\]', str(genre))
        if parts:
            first_genre = parts[0].strip()

    # Format episodes
    status_lower = str(status).lower()
    is_completed = story_doc.get("is_completed") or story_doc.get("isCompleted") or ("completed" in status_lower) or ("complete" in status_lower)
    ep_count = str(story_doc.get("episodes") or story_doc.get("ep_count") or story_doc.get("total_eps") or "1").strip()
    if is_completed:
        episodes_str = f"{ep_count}/{ep_count}"
    else:
        episodes_str = f"{ep_count}/∞"

    # Clean description to 10-15 words (combining first 6 and last 6) and translate to unicode monospace
    desc_clean = desc.strip()
    words = desc_clean.split()
    if len(words) > 12:
        desc_clean = " ".join(words[:6]) + " ... " + " ".join(words[-6:])
    else:
        desc_clean = " ".join(words)
    desc_monospace = to_monospace(desc_clean)

    # HTML Escaping for variables
    story_name_esc = escape_html(story_name)
    status_esc = escape_html(status)
    platform_esc = escape_html(platform)
    first_genre_esc = escape_html(first_genre)
    episodes_str_esc = escape_html(episodes_str)
    price_esc = escape_html(price)

    # Formatted caption as HTML: Bold labels and values, gap after episodes, price line (user format, centered), gap, blockquote description
    # Center price using leading spaces since Telegram HTML doesn't support center-align
    caption = (
        f"♨️ <b>Story :</b> <b>{story_name_esc}</b>\n"
        f"🔰 <b>Status :</b> <b>{status_esc}</b>\n"
        f"🖥 <b>Platform :</b> <b>{platform_esc}</b>\n"
        f"🧩 <b>Genre :</b> <b>{first_genre_esc}</b>\n"
        f"🎬 <b>Episodes :</b> <b>{episodes_str_esc}</b>\n"
        f"\n"
        f"   █▓▒▒░░░ᑭᖇIᑕE - ₹<b>{price_esc}</b> ░░░▒▒▓█\n"
        f"\n"
        f"<b>Story Description :</b>\n"
        f"<blockquote>{desc_monospace}</blockquote>"
    )

    bot_un = story_doc.get("bot_username")
    if not bot_un:
        bot_un = "UseAryaBot"
    bot_un = str(bot_un).lstrip("@").strip()

    story_id = str(story_doc.get("_id") or story_doc.get("story_id"))
    buy_url = f"https://t.me/{bot_un}/apminibyarya?startapp=story_{story_id}"
    tutorial_url = "https://t.me/StoriesFinderBot?start=guide"

    # 2 Inline buttons
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "Buy Now", "url": buy_url},
                {"text": "Tutorial", "url": tutorial_url}
            ]
        ]
    }

    # ── Image Resolution & Download ──────────────────────────────────────────
    # Try ALL image fields in priority order; skip empty/None values.
    # For each, attempt a HEAD first (fast check), then GET.
    # Validate content-type is an image, enforce Telegram's 10 MB sendPhoto limit.
    # If image is too large, auto-resize it before sending.
    IMAGE_FIELDS = ["cover", "poster_url", "image_url", "image", "banner_url", "thumbnail"]
    TELEGRAM_MAX_BYTES = 9 * 1024 * 1024  # 9 MB safety margin (Telegram limit is 10 MB)

    def _build_absolute_url(raw: str) -> str:
        raw = raw.strip()
        if raw.startswith("http://") or raw.startswith("https://"):
            return raw
        # Relative URL — prepend base site
        return "https://aryapremium.store/" + raw.lstrip("/")

    async def _try_download_image(url: str) -> bytes | None:
        """Download image from URL. Returns bytes if valid image, else None."""
        try:
            async with httpx.AsyncClient(
                timeout=20,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 AryaPremiumBot/1.0"}
            ) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    logger.warning(f"[PosterBot] HTTP {resp.status_code} for image URL: {url}")
                    return None
                content_type = resp.headers.get("content-type", "")
                if not any(t in content_type for t in ("image/", "application/octet-stream")):
                    logger.warning(f"[PosterBot] Non-image content-type '{content_type}' for URL: {url}")
                    return None
                data = resp.content
                if not data or len(data) < 512:
                    logger.warning(f"[PosterBot] Suspiciously small response ({len(data)} bytes) for URL: {url}")
                    return None
                logger.info(f"[PosterBot] Downloaded image {len(data)//1024}KB from {url}")
                return data
        except Exception as e:
            logger.warning(f"[PosterBot] Failed to download from {url}: {e}")
            return None

    def _resize_if_needed(img_bytes: bytes) -> bytes:
        """If image exceeds Telegram limit, resize down proportionally."""
        if len(img_bytes) <= TELEGRAM_MAX_BYTES:
            return img_bytes
        try:
            img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            # Scale down progressively until under limit
            for quality in [75, 60, 45]:
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=quality, optimize=True)
                result = buf.getvalue()
                if len(result) <= TELEGRAM_MAX_BYTES:
                    logger.info(f"[PosterBot] Resized image to {len(result)//1024}KB at quality={quality}")
                    return result
            # Last resort: halve dimensions
            w, h = img.size
            img = img.resize((w // 2, h // 2), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=60)
            result = buf.getvalue()
            logger.info(f"[PosterBot] Halved dimensions → {len(result)//1024}KB")
            return result
        except Exception as e:
            logger.error(f"[PosterBot] Resize failed: {e}")
            return img_bytes

    async def _try_download_telegram_file_id(file_id: str, token: str) -> bytes | None:
        """Download image directly from Telegram servers using file_id."""
        if not token:
            return None
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                gf_res = await client.get(f"https://api.telegram.org/bot{token}/getFile", params={"file_id": file_id})
                if gf_res.status_code == 200 and gf_res.json().get("ok"):
                    file_path = gf_res.json().get("result", {}).get("file_path")
                    if file_path:
                        dl_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
                        img_res = await client.get(dl_url)
                        if img_res.status_code == 200 and len(img_res.content) > 512:
                            logger.info(f"[PosterBot] Downloaded image from Telegram file_id ({len(img_res.content)//1024}KB)")
                            return img_res.content
        except Exception as e:
            logger.warning(f"[PosterBot] Failed to download Telegram file_id {file_id}: {e}")
        return None

    photo_bytes = None
    attempted_urls = []

    for field in IMAGE_FIELDS:
        raw_val = story_doc.get(field)
        if not raw_val or not str(raw_val).strip():
            continue
        val_str = str(raw_val).strip()

        # Check if value is a Telegram file_id
        is_file_id = (
            val_str.startswith("AgAC") or 
            val_str.startswith("BAAC") or 
            val_str.startswith("CAAC") or 
            (not val_str.startswith("http") and "/" not in val_str and len(val_str) > 20)
        )

        data = None
        if is_file_id:
            logger.info(f"[PosterBot] Field '{field}' has Telegram file_id: {val_str[:15]}...")
            data = await _try_download_telegram_file_id(val_str, bot_token)
        else:
            abs_url = _build_absolute_url(val_str)
            if abs_url in attempted_urls:
                continue
            attempted_urls.append(abs_url)
            data = await _try_download_image(abs_url)

        if data:
            photo_bytes = _resize_if_needed(data)
            logger.info(f"[PosterBot] Successfully acquired image from field='{field}'")
            break
        else:
            logger.warning(f"[PosterBot] Field '{field}' image failed, trying next field...")

    if not photo_bytes:
        logger.error(f"[PosterBot] No valid image found for story '{story_name}' — tried fields: {IMAGE_FIELDS}, URLs: {attempted_urls}. Will post text-only.")

    # Apply watermark if enabled
    if photo_bytes and watermark_config and watermark_config.get("watermark_enabled"):
        # Resolve path to custom_watermark.png — check multiple candidate dirs:
        # 1. Same dir as this file (AryaPremium/)
        # 2. Parent dir (project root)
        # 3. Current working directory
        this_dir = os.path.dirname(os.path.abspath(__file__))
        parent_dir = os.path.dirname(this_dir)
        cwd = os.getcwd()
        candidate_paths = [
            os.path.join(this_dir, "custom_watermark.png"),
            os.path.join(parent_dir, "custom_watermark.png"),
            os.path.join(cwd, "custom_watermark.png"),
            os.path.join(this_dir, "WatermarkIMG.png"),
            os.path.join(parent_dir, "WatermarkIMG.png"),
            os.path.join(cwd, "WatermarkIMG.png"),
        ]
        watermark_path = None
        for cp in candidate_paths:
            if os.path.exists(cp):
                watermark_path = cp
                break
        
        if watermark_path:
            pos = watermark_config.get("watermark_position", "bottom_right")
            opac = float(watermark_config.get("watermark_opacity", 0.8))
            
            # Execute image editing task in executor thread to prevent blocking ASGI event loop
            photo_bytes = await asyncio.to_thread(
                overlay_watermark,
                photo_bytes,
                watermark_path,
                pos,
                opac
            )
            logger.info(f"[PosterBot] Applied watermark from: {watermark_path}")
        else:
            logger.warning("[PosterBot] No watermark file found in any candidate path; skipping watermark overlay")

    # ── Post to Telegram ─────────────────────────────────────────────────────
    import json as _json

    # Telegram caption limit is 1024 characters
    if len(caption) > 1024:
        caption = caption[:1020] + "..."
        logger.warning(f"[PosterBot] Caption truncated to 1024 chars for story '{story_name}'")

    # Parse channel_id (if numeric/negative, cast to int)
    target_chat = channel_id
    if str(channel_id).lstrip("-").isdigit():
        target_chat = int(channel_id)

    # ── Userbot Mode Check (Pyrogram / Telethon String Session) ───────────────
    poster_mode = (watermark_config or {}).get("poster_mode", "bot_api")
    session_string = str((watermark_config or {}).get("session_string", "")).strip()
    api_id_val = str((watermark_config or {}).get("api_id", "")).strip()
    api_hash_val = str((watermark_config or {}).get("api_hash", "")).strip()

    if poster_mode == "userbot" and session_string:
        logger.info(f"[PosterBot Userbot] Attempting send via Userbot String Session to {target_chat}...")
        # 1. Try Pyrogram first
        try:
            from pyrogram import Client as PyroClient
            import io as _io
            api_id_int = int(api_id_val) if api_id_val.isdigit() else 6
            api_hash_str = api_hash_val or "eb06630096e540092c71336c1380cd08"
            
            async with PyroClient("poster_userbot", api_id=api_id_int, api_hash=api_hash_str, session_string=session_string, in_memory=True) as app:
                if photo_bytes:
                    sent_msg = await app.send_photo(
                        chat_id=target_chat,
                        photo=_io.BytesIO(photo_bytes),
                        caption=caption,
                        parse_mode="html"
                    )
                else:
                    sent_msg = await app.send_message(
                        chat_id=target_chat,
                        text=caption,
                        parse_mode="html"
                    )
                msg_id = getattr(sent_msg, "id", None) or getattr(sent_msg, "message_id", 1)
                logger.info(f"[PosterBot Userbot] ✅ Pyrogram post success! msg_id={msg_id}")
                return {"success": True, "message_id": msg_id, "channel_id": str(target_chat)}
        except Exception as pyro_err:
            logger.warning(f"[PosterBot Userbot] Pyrogram failed: {pyro_err}. Trying Telethon or Bot API fallback...")

        # 2. Try Telethon as secondary userbot library
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            import io as _io
            api_id_int = int(api_id_val) if api_id_val.isdigit() else 6
            api_hash_str = api_hash_val or "eb06630096e540092c71336c1380cd08"
            
            async with TelegramClient(StringSession(session_string), api_id_int, api_hash_str) as client:
                if photo_bytes:
                    photo_file = _io.BytesIO(photo_bytes)
                    photo_file.name = "cover.jpg"
                    sent_msg = await client.send_file(
                        entity=target_chat,
                        file=photo_file,
                        caption=caption,
                        parse_mode="html"
                    )
                else:
                    sent_msg = await client.send_message(
                        entity=target_chat,
                        message=caption,
                        parse_mode="html"
                    )
                msg_id = getattr(sent_msg, "id", 1)
                logger.info(f"[PosterBot Userbot] ✅ Telethon post success! msg_id={msg_id}")
                return {"success": True, "message_id": msg_id, "channel_id": str(target_chat)}
        except Exception as tele_err:
            logger.warning(f"[PosterBot Userbot] Telethon failed: {tele_err}. Falling back to Bot API.")

    reply_markup_json = _json.dumps(reply_markup)
    photo_sent = False

    if photo_bytes:
        logger.info(f"[PosterBot] Sending sendPhoto ({len(photo_bytes)//1024}KB) to {target_chat}")
        url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(
                    url,
                    data={
                        "chat_id": str(target_chat),
                        "caption": caption,
                        "parse_mode": "HTML",
                        "reply_markup": reply_markup_json,
                    },
                    files={"photo": ("cover.jpg", photo_bytes, "image/jpeg")},
                )
            res_json = r.json()
            if r.status_code == 200 and res_json.get("ok"):
                msg_id = res_json.get("result", {}).get("message_id")
                logger.info(f"[PosterBot] ✅ sendPhoto success. msg_id={msg_id}")
                return {"success": True, "message_id": msg_id, "channel_id": str(target_chat)}
            else:
                tg_err = res_json.get("description") or str(res_json)
                logger.error(f"[PosterBot] ❌ sendPhoto failed (HTTP {r.status_code}): {tg_err}")
                # If error is about the file/image itself, still try sendMessage fallback
        except Exception as e:
            logger.error(f"[PosterBot] Exception in sendPhoto: {e}")

    # Fallback: send as text-only message (no photo)
    logger.warning(f"[PosterBot] Falling back to sendMessage (text-only) for story '{story_name}'")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                url,
                json={
                    "chat_id": target_chat,
                    "text": caption,
                    "parse_mode": "HTML",
                    "reply_markup": reply_markup,
                },
            )
        res_json = r.json()
        if r.status_code == 200 and res_json.get("ok"):
            msg_id = res_json.get("result", {}).get("message_id")
            logger.info(f"[PosterBot] ✅ sendMessage fallback success. msg_id={msg_id}")
            return {"success": True, "message_id": msg_id, "channel_id": str(target_chat)}
        else:
            err = res_json.get("description") or str(res_json)
            logger.error(f"[PosterBot] ❌ sendMessage also failed: {err}")
            return {"success": False, "error": f"Telegram API Error: {err}"}
    except Exception as e:
        logger.error(f"[PosterBot] Exception in sendMessage fallback: {e}")
        return {"success": False, "error": f"Exception: {str(e)}"}
