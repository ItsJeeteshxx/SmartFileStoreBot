"""
owner_utils.py — Arya Bot
==========================
Centralized helpers for:
  1. Owner / Co-owner checks  (is_owner, is_any_owner, owner_filter)
  2. Feature disable system    (is_feature_enabled, require_feature)

Feature keys (used in DB + UI):
  live_job   — Live Job forwarding
  multi_job  — Multi Job batch forwarding
  merger     — Merger (audio merge)
  cleaner    — Cleaner Job
  batch_links — Batch Links / Share Bot delivery
"""

import asyncio
import logging
from functools import wraps

from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery

from config import Config

logger = logging.getLogger(__name__)

import time as _time

# ── Cache (30s TTL) to avoid DB hit on every button click ─────────────────────
_owner_cache: dict[int, tuple[bool, float]] = {}   # uid → (result, expires_at)
_feature_cache: dict[str, tuple[bool, float]] = {}  # feature → (result, expires_at)
_CACHE_TTL = 30  # seconds

def is_owner(user_id: int) -> bool:
    """Primary owner check (sync — reads from env/Config)."""
    return user_id in Config.BOT_OWNER_ID


async def is_any_owner(user_id: int) -> bool:
    """Returns True for primary owners AND co-owners stored in DB. Cached 30s."""
    if is_owner(user_id):
        return True
    now = _time.monotonic()
    cached = _owner_cache.get(user_id)
    if cached and cached[1] > now:
        return cached[0]
    from database import db
    result = await db.is_co_owner(user_id)
    _owner_cache[user_id] = (result, now + _CACHE_TTL)
    return result


# Pyrogram filter that allows both primary and co-owners
class _AnyOwnerFilter(filters.Filter):
    async def __call__(self, _, __, message: Message):
        uid = getattr(message.from_user, 'id', None)
        if uid is None:
            return False
        return await is_any_owner(uid)

any_owner_filter = _AnyOwnerFilter()


# ── 2.  Feature disable system ────────────────────────────────────────────────

# Canonical feature names shown in the UI
FEATURE_LABELS = {
    "live_job":    "Lɪᴠᴇ Jᴏʙ",
    "multi_job":   "Mᴜʟᴛɪ Jᴏʙ",
    "merger":      "Mᴇʀɢᴇʀ",
    "cleaner":     "Cʟᴇᴀɴᴇʀ",
    "batch_links": "Bᴀᴛᴄʜ Lɪɴᴋs",
    "url_bypass":  "Uʀʟ Bʏᴘᴀss",
    "join_requests": "Jᴏɪɴ Rᴇǫᴜᴇsᴛs",
    "normal_fwd":  "Nᴏʀᴍᴀʟ Fᴏʀᴡᴀʀᴅ",
}

_DISABLED_MSG = (
    "🔒 <b>{feature} is temporarily disabled by the admin.</b>\n\n"
    "<i>Please contact the admin for more information.</i>"
)


async def is_feature_enabled(feature: str) -> bool:
    """Returns True if the feature is enabled. Cached 30s to avoid per-click DB hits."""
    now = _time.monotonic()
    cached = _feature_cache.get(feature)
    if cached and cached[1] > now:
        return cached[0]
    from database import db
    doc = await db.stats.find_one({'_id': 'disabled_features'})
    disabled = set(doc.get('features', [])) if doc else set()
    result = feature not in disabled
    _feature_cache[feature] = (result, now + _CACHE_TTL)
    return result


async def get_disabled_features() -> set:
    """Returns the set of currently disabled feature keys."""
    from database import db
    doc = await db.stats.find_one({'_id': 'disabled_features'})
    return set(doc.get('features', [])) if doc else set()


async def set_feature_disabled(feature: str, disabled: bool):
    """Enable or disable a feature globally."""
    from database import db
    if disabled:
        await db.stats.update_one(
            {'_id': 'disabled_features'},
            {'$addToSet': {'features': feature}},
            upsert=True
        )
    else:
        await db.stats.update_one(
            {'_id': 'disabled_features'},
            {'$pull': {'features': feature}},
        )
    # Invalidate cache immediately after change
    _feature_cache.pop(feature, None)


def require_feature(feature: str):
    """
    Decorator for Pyrogram message/callback handlers.
    If the feature is disabled, sends the admin-disabled message and stops.
    Owners always bypass this check.

    Usage:
        @Client.on_message(...)
        @require_feature("multi_job")
        async def my_handler(bot, message): ...
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(bot, update):
            # Determine user_id from either Message or CallbackQuery
            if isinstance(update, CallbackQuery):
                uid = update.from_user.id
            else:
                uid = getattr(getattr(update, 'from_user', None), 'id', None)

            # Owners bypass all feature restrictions
            if uid and await is_any_owner(uid):
                return await func(bot, update)

            # Check if feature is enabled
            if not await is_feature_enabled(feature):
                label = FEATURE_LABELS.get(feature, feature)
                disabled_msg = _DISABLED_MSG.format(feature=label)
                try:
                    if isinstance(update, CallbackQuery):
                        await update.answer(
                            f"🔒 {label} is temporarily disabled by admin.",
                            show_alert=True
                        )
                    else:
                        await update.reply_text(disabled_msg)
                except Exception:
                    pass
                return  # Stop — don't call the handler

            return await func(bot, update)
        return wrapper
    return decorator
