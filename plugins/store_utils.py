"""
Store utilities.

These helpers are used by the Smart File Store / Batch Links
pipeline and are intentionally independent from the old URL
Bypass system.
"""

from __future__ import annotations

import os
import asyncio
import re
import time
import logging
import random
import uuid

from typing import Optional

from pyrogram import Client, filters, enums, ContinuePropagation
from pyrogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from pyrogram.errors import FloodWait

from database import db
from plugins.owner_utils import require_feature
from bot import BOT_INSTANCE

logger = logging.getLogger(__name__)
PM = enums.ParseMode.HTML

def _clean_video_caption(raw_text: str) -> str:
    """
    Cleans video caption:
    - Extracts ONLY the title line.
    - Removes leading '🎬', '🎥', '📺', '🍿', '📹' and similar video emojis.
    - Removes notice/auto-delete warnings, divider lines, and promotional footers.
    """
    if not raw_text:
        return ""

    lines = [line.strip() for line in raw_text.strip().splitlines() if line.strip()]
    if not lines:
        return ""

    promo_triggers = [
        "important notice", "notice", "auto-deleted", "auto deleted", "deleted in",
        "forward or save", "save it before", "powered by", "story tv", "watch now",
        "for free", "join channel", "click here", "subscribe", "t.me/", "http://", "https://", "@"
    ]

    title_line = ""
    for line in lines:
        l_str = line.strip()
        if not l_str:
            continue
        # Divider line
        if all(c in '━─═-—_~*• ' for c in l_str) and len(l_str) >= 3:
            continue
        l_lower = l_str.lower()
        if any(trig in l_lower for trig in promo_triggers):
            continue
        if any(w in l_str for w in ["𝗪𝗮𝘁𝗰𝗵", "𝗙𝗿𝗲𝗲", "𝗣𝗼𝘄𝗲𝗿𝗲𝗱", "𝗦𝘁𝗼𝗿𝘆", "𝗕𝗼𝘁", "𝗡𝗼𝘁𝗶𝗰𝗲", "𝗜𝗺𝗽𝗼𝗿𝘁𝗮𝗻𝘁", "𝗗𝗲𝗹𝗲𝘁𝗲𝗱"]):
            continue
        title_line = l_str
        break

    if not title_line and lines:
        title_line = lines[0]

    # Strip leading emojis like 🎬, 🎥, 📺, 🍿, 📹, etc.
    strip_emojis = ["🎬", "🎥", "📺", "🍿", "📹", "🔹", "🔸", "▫️", "▪️", "▶️", "👉", "✨", "🔥"]
    for emo in strip_emojis:
        if title_line.startswith(emo):
            title_line = title_line[len(emo):].strip()

    return title_line.strip()


def process_poster_image(input_path: str, target_size: tuple[int, int] = (600, 720)) -> str:
    """
    Resizes and naturally enhances poster images to exactly 600x720:
    - Preserves 100% of the image content (no cropping/cuts).
    - If aspect ratio differs, fits centered onto a subtle matched aesthetic background.
    - Applies subtle, natural clarity and balanced contrast (no over-whitening or highlight blowout).
    """
    from PIL import Image, ImageEnhance, ImageFilter
    
    try:
        with Image.open(input_path) as img:
            # Convert RGBA/Palette/Grayscale to RGB
            if img.mode in ("RGBA", "LA", "P"):
                rgb_img = Image.new("RGB", img.size, (18, 18, 20))
                if img.mode == "RGBA":
                    rgb_img.paste(img, mask=img.split()[3])
                else:
                    rgb_img.paste(img.convert("RGB"))
                img = rgb_img
            elif img.mode != "RGB":
                img = img.convert("RGB")

            target_w, target_h = target_size
            orig_w, orig_h = img.size

            # Scale to fit completely inside 600x720 without any cropping
            scale = min(target_w / orig_w, target_h / orig_h)
            new_w = max(1, int(orig_w * scale))
            new_h = max(1, int(orig_h * scale))

            resized_content = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

            if (new_w, new_h) != (target_w, target_h):
                # Create a 600x720 canvas
                canvas = Image.new("RGB", (target_w, target_h), (16, 16, 18))
                
                # Create a subtle blurred background from original to look ultra professional
                try:
                    bg_img = img.resize((target_w, target_h), Image.Resampling.BILINEAR)
                    bg_img = bg_img.filter(ImageFilter.GaussianBlur(radius=20))
                    bg_img = ImageEnhance.Brightness(bg_img).enhance(0.40)
                    canvas.paste(bg_img, (0, 0))
                except Exception:
                    pass

                # Center foreground image without any cuts
                pos_x = (target_w - new_w) // 2
                pos_y = (target_h - new_h) // 2
                canvas.paste(resized_content, (pos_x, pos_y))
                final_img = canvas
            else:
                final_img = resized_content

            # ── Balanced AI-Grade Natural Enhancements (Natural & Crisp, No Over-Whitening) ──
            # 1. Subtle Sharpness boost (clean text & outlines)
            final_img = ImageEnhance.Sharpness(final_img).enhance(1.15)
            # 2. Balanced natural contrast (no highlight clipping)
            final_img = ImageEnhance.Contrast(final_img).enhance(1.04)
            # 3. Rich natural color vibrancy
            final_img = ImageEnhance.Color(final_img).enhance(1.05)

            # Save back with high quality JPEG
            final_img.save(input_path, format="JPEG", quality=95, optimize=True, subsampling=0)
            return input_path
    except Exception as e:
        logger.warning(f"[Bypass] Poster enhance error: {e}")
        return input_path

