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
        f"   <b>█▓▒▒░░░ᑭᖇIᑕE - ( ₹{price_esc} )░░░▒▒▓█</b>\n"
        f"\n"
        f"<b>📖 Story Description :</b>\n"
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

    # Resolve photo image bytes prioritizing square cover/poster keys over horizontal banners
    photo_bytes = None
    img_url = None
    for attr in ["cover", "poster_url", "image_url", "image", "banner_url"]:
        val = story_doc.get(attr)
        if val:
            img_url = str(val).strip()
            break

    if img_url:
        if not img_url.startswith("http://") and not img_url.startswith("https://"):
            img_url = "https://aryapremium.store/" + img_url.lstrip("/")
        
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(img_url)
                if resp.status_code == 200:
                    photo_bytes = resp.content
        except Exception as e:
            logger.error(f"Failed to download story image from {img_url}: {e}")

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

    # Post to Telegram
    # Parse channel_id (if starts with digits or minus, cast to int)
    target_chat = channel_id
    if str(channel_id).replace("-", "").isdigit():
        target_chat = int(channel_id)

    async with httpx.AsyncClient(timeout=30) as client:
        if photo_bytes:
            # Send sendPhoto
            url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
            files = {"photo": ("banner.jpg", photo_bytes, "image/jpeg")}
            data = {
                "chat_id": target_chat,
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": __import__("json").dumps(reply_markup)
            }
            try:
                r = await client.post(url, data=data, files=files)
                res_json = r.json()
                if r.status_code == 200 and res_json.get("ok"):
                    msg_id = res_json.get("result", {}).get("message_id")
                    return {"success": True, "message_id": msg_id, "channel_id": str(target_chat)}
                else:
                    logger.error(f"Telegram sendPhoto failed: {res_json}")
                    # Fallback to sendMessage
            except Exception as e:
                logger.error(f"Exception during Telegram sendPhoto: {e}")
                # Fallback to sendMessage

        # Fallback to text message if photo fails or not available
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        data = {
            "chat_id": target_chat,
            "text": caption,
            "parse_mode": "HTML",
            "reply_markup": __import__("json").dumps(reply_markup)
        }
        try:
            r = await client.post(url, json=data)
            res_json = r.json()
            if r.status_code == 200 and res_json.get("ok"):
                msg_id = res_json.get("result", {}).get("message_id")
                return {"success": True, "message_id": msg_id, "channel_id": str(target_chat)}
            else:
                return {"success": False, "error": f"Telegram API Error: {res_json.get('description') or res_json}"}
        except Exception as e:
            return {"success": False, "error": f"Exception: {str(e)}"}
