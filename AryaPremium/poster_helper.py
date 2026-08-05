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

async def send_story_to_channel(bot_token: str, channel_id: str, story_doc: dict, watermark_config: dict = None) -> dict:
    """
    Downloads banner, applies watermark (if enabled), builds keyboard, and posts to Telegram channel.
    Returns dict with success status and message_id if successful.
    """
    story_name = story_doc.get("story_name_en") or story_doc.get("title") or "Unknown Story"
    status = story_doc.get("status") or "Ongoing"
    platform = story_doc.get("platform") or "N/A"
    genre = story_doc.get("genre") or "N/A"
    episodes = str(story_doc.get("episodes") or "1")
    price = str(story_doc.get("price") or "0")
    desc = story_doc.get("description") or ""

    # Clean description to about 2 lines (approx 150 chars)
    desc_clean = desc.strip()
    if len(desc_clean) > 150:
        desc_clean = desc_clean[:147] + "..."

    # Formatted text as requested: Bold headings and values
    caption = (
        f"♨️ **Story : {story_name}**\n"
        f"🔰 **Status : {status}**\n"
        f"🖥 **Platform : {platform}**\n"
        f"🧩 **Genre : {genre}**\n"
        f"🎬 **Episodes : {episodes}**\n\n"
        f"**[ Price :  ₹{price}  ]**\n\n"
        f"**Story Description :-**\n"
        f"**{desc_clean}**"
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

    # Resolve photo image bytes
    photo_bytes = None
    img_url = story_doc.get("poster_url") or story_doc.get("image")
    if img_url and img_url.startswith("http"):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(img_url)
                if resp.status_code == 200:
                    photo_bytes = resp.content
        except Exception as e:
            logger.error(f"Failed to download story image from {img_url}: {e}")

    # Apply watermark if enabled
    if photo_bytes and watermark_config and watermark_config.get("watermark_enabled"):
        # Resolve path to WatermarkIMG.png in root folder
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        watermark_path = os.path.join(base_dir, "WatermarkIMG.png")
        
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
                "parse_mode": "Markdown",
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
            "parse_mode": "Markdown",
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
