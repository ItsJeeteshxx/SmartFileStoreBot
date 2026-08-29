"""
Premium Emoji & Reaction Utility
=================================
Provides custom emoji reactions and premium emoji
for the Arya Premium userbot (requires a Telegram Premium account).

All functions are fire-and-forget safe — they silently ignore failures
so bot functionality is never broken by emoji features.
"""
import asyncio
import random
import logging

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Premium Custom Emoji Document IDs
# These are real Telegram Premium emoji document IDs.
# ─────────────────────────────────────────────────────────────────────────────

# Reaction emojis — used when user sends a command or message
REACTIONS_GENERAL = [
    "⚡",   # lightning
    "🔥",   # fire
    "❤",    # heart
    "👍",   # thumbs up
    "🎉",   # party
    "✨",   # sparkles
    "💎",   # diamond
    "🏆",   # trophy
]

REACTIONS_SUCCESS = ["🎉", "✅", "🔥", "💯", "❤"]
REACTIONS_WELCOME = ["👋", "🎉", "✨", "❤", "🙏"]
REACTIONS_PAYMENT = ["💸", "🎉", "✅", "🙏"]

# Premium custom emoji IDs (document_id) — for use in message text
# Format in HTML: <emoji id="DOCUMENT_ID">fallback_char</emoji>
# These are well-known Telegram Premium animated emoji document IDs
CUSTOM_EMOJI = {
    "hot":          ("5465432711218863135", "♨️"),
    "fire":         ("5465432711218863135", "♨️"),
    "shield_green": ("6019118553326689234", "🔰"),
    "desktop":      ("6019455905827920171", "🖥"),
    "puzzle":       ("6024065724291488135", "🧩"),
    "movie":        ("5937999673510858217", "🎬"),
    "tag":          ("5886285355279193209", "🏷"),
    "folder":       ("5805550320985578625", "📁"),
    "inbox":        ("5776182936638329359", "📥"),
    "confirm":      ("6273749318717412886", "✅"),
    "bank":         ("5264895611517300926", "🏦"),
    "pay_upi":      ("5766975922620076409", "🏦"),
    "lightning":    ("6023761060786346622", "⚡"),
    "card":         ("6030410254276106984", "💳"),
    "refresh":      ("5807492110059838726", "🔄"),
    "money_wings":  ("5472030678633684592", "💸"),
    "book":         ("6023962911364357003", "📖"),
    "money_bag":    ("5283232570660634549", "💰"),
    "shield":       ("6019328362479097179", "🛡"),
    "new_badge":    ("6271473763439612077", "🆕"),
    "search":       ("6025893082552081088", "🔍"),
    "view_all":     ("5764638872000533034", "📑"),
    "wait":         ("5348471079482441278", "⏳"),
    "profile":      ("6021487472603568286", "👤"),
    "settings":     ("6021637109264160908", "⚙️"),
}


def ce(name: str) -> str:
    """
    Return a premium custom emoji HTML tag by name.
    Falls back to a plain emoji if the name is unknown.
    Example: ce('fire') → '<emoji id="5368...">🔥</emoji>'
    """
    if name in CUSTOM_EMOJI:
        doc_id, fallback = CUSTOM_EMOJI[name]
        return f'<emoji id="{doc_id}">{fallback}</emoji>'
    return "✨"


async def react(client, chat_id: int, message_id: int,
                emoji: str = None, pool: list = None) -> bool:
    """
    Send a reaction to a message. Uses a random emoji from `pool`,
    or the specified `emoji`, or picks from REACTIONS_GENERAL.

    Returns True on success, False on failure (silent).
    """
    chosen = emoji or (random.choice(pool) if pool else random.choice(REACTIONS_GENERAL))
    try:
        await client.send_reaction(
            chat_id=chat_id,
            message_id=message_id,
            emoji=chosen
        )
        return True
    except Exception as e:
        # Don't log every reaction failure — it's noisy for non-premium accounts
        logger.debug(f"[PremiumEmoji] Reaction failed ({chosen}): {e}")
        return False


async def react_bg(client, chat_id: int, message_id: int,
                   emoji: str = None, pool: list = None):
    """
    Fire-and-forget version of react() — does not await, 
    does not block the caller.
    """
    asyncio.create_task(
        react(client, chat_id, message_id, emoji=emoji, pool=pool)
    )


async def send_premium_sticker(client, chat_id: int, sticker_key: str = "welcome") -> bool:
    """
    Send a premium animated sticker to the chat.
    Uses known Telegram premium sticker file_ids.
    Returns True on success, False on failure.
    """
    # Well-known premium sticker file_ids (Telegram official premium pack)
    STICKERS = {
        "welcome":  "CAACAgIAAxkBAAIB...",  # placeholder — set real IDs via /sticker command
        "party":    "CAACAgIAAxkBAAIB...",
        "success":  "CAACAgIAAxkBAAIB...",
        "love":     "CAACAgIAAxkBAAIB...",
    }
    fid = STICKERS.get(sticker_key)
    if not fid or fid.endswith("..."):
        return False  # No valid sticker ID configured
    try:
        await client.send_sticker(chat_id=chat_id, sticker=fid)
        return True
    except Exception as e:
        logger.debug(f"[PremiumEmoji] Sticker send failed ({sticker_key}): {e}")
        return False
