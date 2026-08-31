_share_bot_token_cache = {}
_shared_bot_api_session = None

def _get_shared_bot_api_session():
    global _shared_bot_api_session
    import aiohttp
    if _shared_bot_api_session is None or _shared_bot_api_session.closed:
        connector = aiohttp.TCPConnector(
            limit=100,
            ttl_dns_cache=300,
            keepalive_timeout=60,
            enable_cleanup_closed=True
        )
        _shared_bot_api_session = aiohttp.ClientSession(connector=connector)
    return _shared_bot_api_session
"""
Share Bot — Delivery Agent
==========================
Handles deep-link delivery of batched episodes to users.
Handler functions are defined at module level so they can be passed to
add_handler() after the client is started (Pyrogram 2.x requirement).
"""
import logging
import asyncio
import time
import random
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import UserNotParticipant
from pyrogram.handlers import MessageHandler, CallbackQueryHandler, ChatJoinRequestHandler
from database import db
from config import Config

logger = logging.getLogger(__name__)

share_clients: dict = {}   # { bot_id_str: Client }
active_downloads: set = set()

# Peer cache: tracks already-resolved chat_ids per client session.
# Avoids redundant get_chat() calls on every delivery request.
_peer_cache: dict = {}    # { (client_id, chat_id): timestamp }
_PEER_CACHE_TTL = 3600    # 1 hour — re-warm after this long

# Join-request tracking: records that a user has a pending join request.
# Format: "{chat_id}_{user_id}": timestamp_of_first_request
# TTL extended to 10 days because admin approval can take days.
_jr_approved: dict = {}
_JR_TTL = 864000          # 10 days

# ── Anti-Abuse: 3-Strike Rapid Request Tracker ───────────────────────────────
# In-memory dict to track per-user delivery timestamps and strike counts.
# These complement the DB strike records — DB survives restarts, memory is fast.
#   _abuse_last_delivery[user_id] = float  (unix timestamp of last delivery)
#   _abuse_strikes[user_id]       = int    (rapid-request offense count)
_abuse_last_delivery: dict = {}   # { user_id: float }
_abuse_strikes: dict       = {}   # { user_id: int }

# Channel health cache: tracks unresolvable/invalid force-subscribe channels
# Format: { chat_id_int: { "status": "invalid", "expires": timestamp } }
_channel_health_cache: dict = {}

# Anti-abuse configuration cache: caches the config to avoid redundant database calls on every start
_anti_abuse_config_cache: dict = {}
_ANTI_ABUSE_CACHE_TTL = 60  # Cache anti-abuse configuration for 1 minute


async def _check_and_record_rapid_request(client, message, user_id: int, bot_id: str) -> bool:
    """Legacy anti-abuse replaced by rate limit & pass system."""
    return False

    # ── Master switch: skip everything if Anti-Abuse is disabled ────────────
    now_time = _t.time()
    cached_cfg = _anti_abuse_config_cache.get('cfg')
    cached_ts = _anti_abuse_config_cache.get('ts', 0.0)
    
    if cached_cfg and (now_time - cached_ts) < _ANTI_ABUSE_CACHE_TTL:
        abuse_cfg = cached_cfg
    else:
        try:
            abuse_cfg = await db.get_anti_abuse_config()
            _anti_abuse_config_cache['cfg'] = abuse_cfg
            _anti_abuse_config_cache['ts'] = now_time
        except Exception:
            abuse_cfg = {'enabled': True, 'cooldown_secs': _Cfg.ABUSE_COOLDOWN_SECS, 'max_strikes': _Cfg.ABUSE_MAX_STRIKES}

    if not abuse_cfg.get('enabled', True):
        return False   # Anti-Abuse is OFF — allow all requests without any check

    # Owners / co-owners / whitelisted users / Paid Users (with >= 1 purchased story) are always exempt
    if await _is_any_owner(user_id) or await db.is_whitelisted(user_id) or await db.is_paid_user(user_id):
        return False

    # DB-stored values take priority; fall back to Config env vars
    cooldown    = int(abuse_cfg.get('cooldown_secs', _Cfg.ABUSE_COOLDOWN_SECS))
    max_strikes = int(abuse_cfg.get('max_strikes',   _Cfg.ABUSE_MAX_STRIKES))

    now = _t.time()
    last_delivery = _abuse_last_delivery.get(user_id, 0.0)

    # --- Within cooldown window? ---
    if last_delivery > 0 and (now - last_delivery) < cooldown:
        # Increment strike count
        current = _abuse_strikes.get(user_id, 0) + 1
        _abuse_strikes[user_id] = current

        # Persist to DB so strikes survive a bot restart
        try:
            await db.update_user_strike(user_id, current, last_strike_ts=now)
        except Exception:
            pass

        bot_name = client.me.first_name if getattr(client, 'me', None) else "DeliveryBot"
        u_name = message.from_user.first_name or str(user_id) if message.from_user else str(user_id)

        if current >= max_strikes:
            # ── Silent permanent ban ──────────────────────────────────────────
            try:
                await db.ban_user(user_id, f"Auto-ban: rapid bulk file requests ({current} strikes)")
            except Exception:
                pass
            try:
                await db.reset_user_strike(user_id)
            except Exception:
                pass
            # Clear in-memory state
            _abuse_strikes.pop(user_id, None)
            _abuse_last_delivery.pop(user_id, None)

            # Fire ban log to channel
            import asyncio as _aio
            _aio.create_task(_log.log_ban(
                user_id=user_id,
                user_name=u_name,
                strike_count=current,
                bot_name=bot_name,
                bot_id=str(bot_id or ""),
                reason=f"Exceeded rapid request limit ({current} strikes within {cooldown}s cooldown)"
            ))
            logger.info(f"[Abuse] BANNED user {user_id} silently after {current} rapid strikes")
            # Return True to abort delivery; completely silent with no reply to user
            return True
            
        else:
            # Under the limit: Log the warning to the admin channel, but DO NOT warn the user
            # and allow the delivery to proceed normally by returning False
            import asyncio as _aio
            _aio.create_task(_log.log_warn(
                user_id=user_id, user_name=u_name,
                strike_count=current, max_strikes=max_strikes,
                bot_name=bot_name, bot_id=str(bot_id or ""),
            ))
            logger.info(f"[Abuse] Strike {current}/{max_strikes} for user {user_id} (allowed)")
            return False

    else:
        # Outside cooldown window — reset strike counter
        if _abuse_strikes.get(user_id, 0) > 0:
            _abuse_strikes[user_id] = 0
            try:
                await db.reset_user_strike(user_id)
            except Exception:
                pass

    return False   # proceed normally


# 
# Arya Bot Font constants
# 
ARYA_VERSION = "V1.0"
UPDATE_LINK   = "https://t.me/AryaBotUpdatesTG"
SUPPORT_LINK  = "https://t.me/+KPVtaAm9k-RmMjdl"

DEFAULT_PREMIUM_AD_TEXT = (
    "◎ सूचना: समय और मेहनत दोनों बचाइए!\n\n"
    "▣ क्या आप ऑटो डिलीट होने वाली फ़ाइलों, बार-बार अलग-अलग चैनल जॉइन करने और कई तरह की पाबंदियों से परेशान हैं?\n\n"
    "◑ 𝗔𝗿𝘆𝗮 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗕𝗼𝘁 / 𝗠𝗶𝗻𝗶 𝗔𝗽𝗽 पर 220+ Pocket FM, Kuku FM और Pratilipi की स्टोरीज़ हिन्दी व English में उपलब्ध हैं।\n\n"
    "⧉ किफायती कीमत • सुरक्षित भुगतान • कई भुगतान विकल्प • नई स्टोरीज़ नियमित रूप से जोड़ी जाती हैं।\n\n"
    "◎ नीचे दिए गए \"𝗢𝗽𝗲𝗻 𝗦𝘁𝗼𝗿𝗲\" बटन पर क्लिक करके 𝗔𝗿𝘆𝗮 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗠𝗶𝗻𝗶 𝗔𝗽𝗽 खोलें, अपनी पसंदीदा स्टोरीज़ खरीदें और आसानी से अपनी फ़ाइलें प्राप्त करें।\n\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "◎ 𝗔𝗟𝗘𝗥𝗧: 𝗦𝗧𝗢𝗣 𝗪𝗔𝗦𝗧𝗜𝗡𝗚 𝗬𝗢𝗨𝗥 𝗧𝗜𝗠𝗘!\n\n"
    "▣ Tired of Auto Delete Files, joining multiple channels, and unnecessary restrictions?\n\n"
    "◑ 𝗔𝗿𝘆𝗮 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗕𝗼𝘁 / 𝗠𝗶𝗻𝗶 𝗔𝗽𝗽 gives you access to 220+ Pocket FM, Kuku FM, and Pratilipi stories in Hindi & English.\n\n"
    "⧉ Affordable Pricing • Secure Payments • Multiple Payment Methods • New Stories Added Regularly.\n\n"
    "◎ Click the \"𝗢𝗽𝗲𝗻 𝗦𝘁𝗼𝗿𝗲\" button below to open the 𝗔𝗿𝘆𝗮 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗠𝗶𝗻𝗶 𝗔𝗽𝗽, purchase your favourite stories, and get your files easily."
)

# 
# Helpers
# 

def format_msg(text: str, user) -> str:
    if not text:
        return ""
    try:
        full = (user.first_name or "") + (" " + user.last_name if user.last_name else "")
        return text.format(
            first_name=user.first_name or "",
            last_name=user.last_name or "",
            full_name=full.strip(),
            mention=user.mention or user.first_name or "User",
        )
    except Exception:
        return text

def _get_readable_file_size(size_in_bytes: int) -> str:
    if not size_in_bytes:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_in_bytes < 1024.0:
            return f"{size_in_bytes:.2f} {unit}"
        size_in_bytes /= 1024.0
    return f"{size_in_bytes:.2f} PB"

def _sc(text: str) -> str:
    return text.translate(str.maketrans(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"
    ))

def _get_base_header(user) -> str:
    # Only first name, no last name — used in non-welcome contexts (About, Help)
    u_name = user.first_name or "User"
    return f"›› ʜᴇʏ, <a href='tg://user?id={user.id}'>{u_name}</a>\n\n"

def _get_welcome_text(user, bot_name, custom_wel=None, lang='en') -> str:
    if custom_wel:
        return format_msg(custom_wel, user)
    first = user.first_name or "User"
    if lang == 'hi':
        return (
            f"<blockquote expandable>›› ʜᴇʏ, <a href='tg://user?id={user.id}'>{first}</a><emoji id=\"6041919344995209164\">❣️</emoji></blockquote>\n"
            f"<blockquote expandable><b>»  {bot_name} में आपका स्वागत है!</b></blockquote>\n"
            f"<blockquote expandable>मैं एक फ़ाइल डिलीवरी बॉट हूँ। चैनल से किसी भी लिंक बटन पर टैप करें और मैं आपको फ़ाइलें सीधे यहाँ भेज दूंगा।</blockquote>\n"
            f"<blockquote expandable>अधिक जानकारी के लिए सहायता पर क्लिक करें।</blockquote>"
        )
    return (
        # Block 1: Greeting with first name only
        f"<blockquote expandable>›› ʜᴇʏ, <a href='tg://user?id={user.id}'>{first}</a><emoji id=\"6041919344995209164\">❣️</emoji></blockquote>\n"
        # Block 2: Welcome line
        f"<blockquote expandable><b>»  {_sc('Welcome to')} {bot_name}!</b></blockquote>\n"
        # Block 3: Description
        f"<blockquote expandable>{_sc('I am a file delivery bot. Tap any link button from the channel and I will send you the files directly here.')}</blockquote>\n"
        # Block 4: Help hint
        f"<blockquote expandable>{_sc('Click Help for more info.')}</blockquote>"
    )


def _get_help_text(user, lang='en') -> str:
    if lang == 'hi':
        return (
            _get_base_header(user) +
            "<b>सहायता मेनू</b>\n\n"
            "मैं एक फ़ाइल डिलीवरी बॉट हूँ। आप चैनल में दिए गए शेयर करने योग्य लिंक का उपयोग करके फ़ाइलों तक पहुँच सकते हैं।\n\n"
            "<b>फ़ाइलें कैसे प्राप्त करें:</b>\n"
            "➲  चैनल खोलें और लिंक बटन पर टैप करें\n"
            "➲  मैं फ़ाइलें सीधे आपके DM में भेज दूंगा\n"
            "➲  यदि फोर्स-सब्सक्राइब चालू है, तो पहले आवश्यक चैनल से जुड़ें\n"
            "➲  यदि फ़ाइलें डिलीट हो गई हैं, तो वही बटन फिर से दबाएं\n\n"
            "<b>उपलब्ध कमांड्स:</b>\n"
            "➲  /start — बॉट शुरू करें\n"
            "➲  /help — सहायता मेनू देखें\n\n"
            "<b>बॉट जानकारी:</b>\n"
            "➲  सभी डिलीवरी सुरक्षित और एन्क्रिप्टेड हैं\n"
            "➲  फ़ाइलें एक निश्चित समय के बाद ऑटो-डिलीट हो सकती हैं"
        )
    return _get_base_header(user) + _sc(
        "Help Menu\n\n"
        "I am a permanent file store bot. You can access stored files by using "
        "a shareable link given by me from the channel.\n\n"
        "How to Get Files:\n"
        "➲  Open the channel and tap a link button\n"
        "➲  I will send the files directly to your DM\n"
        "➲  If force-subscribe is enabled, join required channels first\n"
        "➲  If your files are deleted, tap the same button again\n\n"
        "Available Commands:\n"
        "➲  /start — check if I'm alive\n"
        "➲  Click any episode link button in the channel to receive files\n\n"
        "Bot Info:\n"
        "➲  All deliveries are encrypted and protected\n"
        "➲  Files may auto-delete after a set time (copyright protection)\n"
        "➲  Simply click your link button again to re-download"
    )


async def delete_later(client, chat_id, msg_ids: list, notice_id: int, delay_secs: int):
    await asyncio.sleep(delay_secs)
    for mid in msg_ids:
        try:
            await client.delete_messages(chat_id, mid)
        except Exception:
            pass
    try:
        if notice_id:
            await client.delete_messages(chat_id, notice_id)
    except Exception:
        pass


async def _warm_peer(client, chat_id) -> None:
    """
    Resolve chat_id in the client's peer cache.
    Skips the network call if we've resolved it in the past hour.
    This avoids the 200-400ms latency spike on every delivery request.
    """
    import time
    client_id = getattr(client, 'me', None)
    client_id = client_id.id if client_id else id(client)
    
    try:
        ch_id_int = int(chat_id)
    except (ValueError, TypeError):
        ch_id_int = chat_id
        
    key = (client_id, ch_id_int)
    if key in _peer_cache and (time.time() - _peer_cache[key]) < _PEER_CACHE_TTL:
        return   # already warm — skip the network call
    
    from bot import BOT_INSTANCE
    from plugins.utils import safe_resolve_peer
    try:
        resolved = await safe_resolve_peer(client, ch_id_int, bot=BOT_INSTANCE)
        if resolved:
            _peer_cache[key] = time.time()
            return
    except Exception:
        pass
        
    try:
        await client.get_chat(ch_id_int)
        _peer_cache[key] = time.time()
    except Exception:
        pass


_fsub_user_cache = {}  # { "uid_chatid": expiration_timestamp }

async def check_all_subscriptions(client, user_id: int, fsub_channels: list, bot_id: str = None) -> list:
    """
    Returns list of channel dicts the user has NOT joined.
    
    Verifies all channels in parallel for maximum speed.
    Normal channels are ALWAYS verified live with no caching (per user request).
    JR Channels are cached for 2 mins upon successful join request DB match.
    """
    import time
    import asyncio
    from pyrogram.errors import UserNotParticipant, PeerIdInvalid, ChannelInvalid
    now = time.time()
    
    async def _check_single(ch):
        chat_id = ch.get('chat_id')
        if not chat_id:
            return None

        is_jr = ch.get('join_request', False)

        # Resolve numeric chat_id
        from bot import BOT_INSTANCE
        from plugins.utils import safe_resolve_peer
        
        ch_id_int = int(chat_id) if str(chat_id).lstrip('-').isdigit() else chat_id
        cache_key = f"{user_id}_{ch_id_int}"
        
        # Check channel health cache first
        if ch_id_int in _channel_health_cache:
            health = _channel_health_cache[ch_id_int]
            if health['status'] == 'invalid' and now < health['expires']:
                if is_jr:
                    try:
                        ch_id_for_query = int(ch_id_int)
                    except (ValueError, TypeError):
                        ch_id_for_query = ch_id_int
                        
                    jr_query = {"user_id": int(user_id)}
                    if isinstance(ch_id_for_query, int):
                        jr_query["$or"] = [{"chat_id": ch_id_for_query}, {"chat_id": str(ch_id_for_query)}]
                    else:
                        cln = str(ch_id_for_query).lstrip("@").lower()
                        jr_query["$or"] = [{"chat_id": ch_id_for_query}, {"chat_id": str(ch_id_for_query)}, {"username": cln}]

                    jr_doc = await db.db["pending_jrs"].find_one(jr_query)
                    
                    if jr_doc and (now - jr_doc.get("timestamp", 0) < _JR_TTL):
                        _fsub_user_cache[cache_key] = now + 120
                        logger.info(f"FSub: JR grant for user {user_id} in cached invalid channel {ch_id_int}")
                        return None
                    else:
                        ch_copy = dict(ch)
                        ch_copy['needs_request'] = True
                        return ch_copy
                else:
                    ch_copy = dict(ch)
                    ch_copy['never_joined'] = True
                    return ch_copy

        if cache_key in _fsub_user_cache and _fsub_user_cache[cache_key] > now:
            return None

        member = None
        is_channel_invalid = False
        
        # 1. Try checking membership via main bot first (highly cached, admin of FSub channels)
        if BOT_INSTANCE and getattr(BOT_INSTANCE, "me", None):
            try:
                member = await BOT_INSTANCE.get_chat_member(ch_id_int, user_id)
            except UserNotParticipant:
                pass  # member stays None → handled below in the UserNotParticipant block
            except Exception:
                try:
                    if await safe_resolve_peer(BOT_INSTANCE, chat_id):
                        member = await BOT_INSTANCE.get_chat_member(ch_id_int, user_id)
                except UserNotParticipant:
                    pass
                except Exception:
                    pass

        # 2. Fallback to delivery bot client if main bot failed or was unavailable
        if member is None:
            try:
                member = await client.get_chat_member(ch_id_int, user_id)
            except UserNotParticipant:
                pass
            except Exception:
                try:
                    resolved = await safe_resolve_peer(client, chat_id, bot=BOT_INSTANCE)
                    if resolved:
                        member = await client.get_chat_member(ch_id_int, user_id)
                    else:
                        is_channel_invalid = True
                except UserNotParticipant:
                    pass
                except Exception:
                    is_channel_invalid = True

        if is_channel_invalid:
            # Cache the invalid status for 30 seconds
            _channel_health_cache[ch_id_int] = {
                'status': 'invalid',
                'expires': now + 30
            }
            logger.error(f"FSub check: Channel {ch_id_int} is unresolvable by all clients. Caching invalid status for 30 seconds.")
            ch_copy = dict(ch)
            ch_copy['never_joined'] = True
            return ch_copy

        try:
            if member is None:
                # If we couldn't resolve the chat or get membership at all,
                # we must NOT bypass FSub. Instead, raise UserNotParticipant to force FSub verification alert!
                # This guarantees that Force Subscribe is NEVER bypassed or skipped on errors!
                raise UserNotParticipant()

            if getattr(member, 'status', None) in (enums.ChatMemberStatus.LEFT, enums.ChatMemberStatus.BANNED):
                raise UserNotParticipant()
            else:
                # Aggressively Cache SUCCESS for 2 hours to prevent FloodWaits across bulk link-clicks!
                _fsub_user_cache[cache_key] = now + 7200
                return None
        except UserNotParticipant:
            _fsub_user_cache.pop(cache_key, None)
            
            if is_jr:
                try:
                    ch_id_for_query = int(ch_id_int)
                except (ValueError, TypeError):
                    ch_id_for_query = ch_id_int
                    
                jr_query = {"user_id": int(user_id)}
                if isinstance(ch_id_for_query, int):
                    jr_query["$or"] = [{"chat_id": ch_id_for_query}, {"chat_id": str(ch_id_for_query)}]
                else:
                    cln = str(ch_id_for_query).lstrip("@").lower()
                    jr_query["$or"] = [{"chat_id": ch_id_for_query}, {"chat_id": str(ch_id_for_query)}, {"username": cln}]

                jr_doc = await db.db["pending_jrs"].find_one(jr_query)
                
                if jr_doc and (now - jr_doc.get("timestamp", 0) < _JR_TTL):
                    _fsub_user_cache[cache_key] = now + 120
                    logger.info(f"FSub: JR grant for user {user_id} in {ch_id_int}")
                    return None
                else:
                    ch_copy = dict(ch)
                    ch_copy['needs_request'] = True
                    return ch_copy
            else:
                ch_copy = dict(ch)
                ch_copy['never_joined'] = True
                return ch_copy
        except Exception as e:
            logger.warning(f"FSub check skipped for {chat_id}: {e}")
            return None

    tasks = [_check_single(ch) for ch in fsub_channels]
    results = await asyncio.gather(*tasks)
    return [r for r in results if r is not None]



# 
# Module-level handler functions (required for add_handler to work)
# 

async def _fsub_record_jr(client, request):
    """
    Record that a user has sent a join request to a JR channel in persistent DB.
    Stores chat_id as INT to ensure consistent type for later lookups.
    """
    import time
    bot_id = str(client.me.id) if client.me else None
    fsub_chs = await db.get_bot_fsub_channels(bot_id) if bot_id else []
    if not fsub_chs:
        fsub_chs = await db.get_share_fsub_channels()

    req_ch_id = request.chat.id    # integer from Telegram
    req_user_id = request.from_user.id  # integer

    for ch in fsub_chs:
        ch_id = ch.get('chat_id')
        # Normalize for comparison
        try:
            ch_id_cmp = int(ch_id)
        except (ValueError, TypeError):
            ch_id_cmp = str(ch_id).lstrip('@').lower()

        req_username = str(getattr(request.chat, 'username', '') or '').lower()
        ch_username  = str(ch_id).lstrip('@').lower()

        matched = (
            req_ch_id == ch_id_cmp
            or (req_username and req_username == ch_username)
        )
        if matched and ch.get('join_request'):
            # Always store as int so the lookup in check_all_subscriptions matches
            await db.db["pending_jrs"].update_one(
                {"user_id": int(req_user_id), "chat_id": int(req_ch_id)},
                {"$set": {"timestamp": time.time(), "username": req_username}},
                upsert=True
            )
            # Also evict the FSub cache so next check hits DB fresh
            cache_key = f"{req_user_id}_{req_ch_id}"
            _fsub_user_cache.pop(cache_key, None)
            logger.info(f"JR recorded for user {req_user_id} in {req_ch_id} (TTL: 10d)")
            return


async def _process_start(client, message):
    """Handle /start [uuid] deep-link — deliver files to user."""
    user_id = message.from_user.id
    args = message.command
    bot_id = str(client.me.id) if client.me else None

    # Non-blocking background user tracking
    async def _track_user_background():
        try:
            _was_new_user = await db.add_share_bot_seen_user(bot_id, user_id)
            await db.add_share_bot_user(bot_id, user_id)
            if _was_new_user:
                import plugins.arya_logger as _log
                u_name = (message.from_user.first_name or str(user_id)) if message.from_user else str(user_id)
                b_name = client.me.first_name if getattr(client, 'me', None) else "DeliveryBot"
                await _log.log_new_user(user_id, u_name, b_name, bot_id or "")
        except Exception:
            pass

    asyncio.create_task(_track_user_background())

    # Plain /start — instant welcome screen
    if len(args) < 2:
        # Check ban status concurrently
        ban_status = await db.get_ban_status(user_id)
        if ban_status.get('is_banned'):
            reason = str(ban_status.get('reason', '')).lower()
            if 'rapid' in reason or 'strike' in reason:
                await db.unban_user(user_id)
            else:
                return
        await _send_welcome(client, message, bot_id)
        return

    uuid_str = args[1].strip()

    # Help command via deep-link (start=help)
    if uuid_str == "help":
        await _send_help(client, message, bot_id)
        return

    # ── High-Speed Parallel Pre-Flight Gather (Single Round-Trip) ───
    from plugins.banned import _is_any_owner
    (
        ban_status,
        link_data,
        is_owner,
        is_wl,
        pass_data,
        rl_cfg,
        protect_flag
    ) = await asyncio.gather(
        db.get_ban_status(user_id),
        db.get_share_link(uuid_str),
        _is_any_owner(user_id),
        db.is_whitelisted(user_id),
        db.get_user_unlimited_pass(user_id),
        db.get_delivery_rate_limit_config(),
        db.get_share_protect_global()
    )

    if ban_status.get('is_banned'):
        reason = str(ban_status.get('reason', '')).lower()
        if 'rapid' in reason or 'strike' in reason:
            await db.unban_user(user_id)
        else:
            logger.warning(f"[ShareBot] Banned user {user_id} blocked in _process_start")
            return

    if not link_data:
        await message.reply_text(
            "<b>‣  Link Expired or Invalid</b>\n\n"
            "This batch link no longer exists. Go back to the channel and click the button again."
        )
        return

    msg_ids     = link_data.get('message_ids', [])
    source_chat = link_data.get('source_chat')

    if not msg_ids or not source_chat:
        await message.reply_text("<b>‣  Database Error:</b> Missing file references.")
        return

    # ── Delivery Rate Limit & Cooldown Check ───
    if not is_owner:
        if not is_wl and not pass_data.get('active', False):
            if rl_cfg.get('enabled', True):
                import time as _t
                from database import format_duration_verbose, format_duration_friendly
                max_limit = int(rl_cfg.get('max_limit', 5))
                window_seconds = int(rl_cfg.get('window_seconds', int(rl_cfg.get('window_hours', 12)) * 3600))
                hits = await db.get_user_delivery_hits(user_id, window_seconds)
                if len(hits) >= max_limit:
                    oldest_hit = hits[0]['timestamp']
                    reset_time = oldest_hit + window_seconds
                    rem_sec = max(1, int(reset_time - _t.time()))
                    if rem_sec >= 3600:
                        rem_hours = rem_sec // 3600
                        rem_mins = (rem_sec % 3600) // 60
                        rem_time_str = f"{rem_hours:02d}h {rem_mins:02d}m"
                    elif rem_sec >= 60:
                        rem_mins = rem_sec // 60
                        rem_secs = rem_sec % 60
                        rem_time_str = f"{rem_mins:02d}m {rem_secs:02d}s"
                    else:
                        rem_time_str = f"{rem_sec:02d}s"
                    
                    win_verbose = format_duration_verbose(window_seconds)

                    # Send rate limit reached log to configured log channel in Quoteblock format
                    try:
                        rl_log_ch = rl_cfg.get('rate_limit_log_channel')
                        user_obj = message.from_user
                        from plugins.arya_logger import log_rate_limit_reached
                        bot_me = getattr(client, 'me', None)
                        b_fn = getattr(bot_me, 'first_name', None) or "Delivery Bot"
                        b_un = getattr(bot_me, 'username', None)
                        asyncio.create_task(log_rate_limit_reached(
                            user_id=user_id,
                            user_name=user_obj.first_name if user_obj else "User",
                            username=user_obj.username if user_obj else None,
                            hits_count=len(hits),
                            max_limit=max_limit,
                            window_str=win_verbose,
                            cooldown_str=rem_time_str,
                            bot_name=b_fn,
                            bot_username=b_un,
                            log_channel=rl_log_ch
                        ))
                    except Exception as _log_e:
                        logger.warning(f"Failed to schedule rate limit log: {_log_e}")

                    user_lang = await db.get_language(user_id)
                    is_hi = bool(user_lang == 'hi')
                    if is_hi:
                        limit_text = (
                            f'<emoji id="6215133834149629990">⏳</emoji> <b>रेट लिमिट पूरी हो गई है</b>\n\n'
                            f'आपने पिछले <b>{win_verbose}</b> में <b>{len(hits)} / {max_limit} लिंक्स</b> एक्सेस कर लिए हैं। <emoji id="6266794310671275367">🎬</emoji>\n\n'
                            f'सभी यूजर्स के लिए लिमिट <b>{max_limit} लिंक्स प्रति {win_verbose}</b> निर्धारित है।\n\n'
                            f'<emoji id="6217487596486922033">⏰</emoji> <b>कूलडाउन रीसेट होने में समय:</b> <code>{rem_time_str}</code>\n\n'
                            f'कृपया बाद में प्रयास करें या नीचे से अनलिमिटेड एक्सेस अनलॉक करें! <emoji id="6023566962624306038">👇</emoji>'
                        )
                        limit_api_kb = [
                            [
                                {
                                    "text": "अनलिमिटेड एक्सेस अनलॉक करें",
                                    "callback_data": "pass#unlock_menu",
                                    "icon_custom_emoji_id": "6030443364178992166"
                                }
                            ]
                        ]
                        unlock_kb = InlineKeyboardMarkup([[
                            InlineKeyboardButton("🔓 अनलिमिटेड एक्सेस अनलॉक करें", callback_data="pass#unlock_menu")
                        ]])
                    else:
                        limit_text = (
                            f'<emoji id="6215133834149629990">⏳</emoji> <b>Rate Limit Reached</b>\n\n'
                            f'You have already accessed <b>{len(hits)} / {max_limit} links</b> in the past <b>{win_verbose}</b>. <emoji id="6266794310671275367">🎬</emoji>\n\n'
                            f'The limit is <b>{max_limit} links per {win_verbose}</b> to ensure fair usage for everyone.\n\n'
                            f'<emoji id="6217487596486922033">⏳</emoji> <b>Cooldown resets in:</b> <code>{rem_time_str}</code>\n\n'
                            f'Please try again later or unlock unlimited access below! <emoji id="6023566962624306038">👇</emoji>'
                        )
                        limit_api_kb = [
                            [
                                {
                                    "text": "Unlock Unlimited Access",
                                    "callback_data": "pass#unlock_menu",
                                    "icon_custom_emoji_id": "6030443364178992166"
                                }
                            ]
                        ]
                        unlock_kb = InlineKeyboardMarkup([[
                            InlineKeyboardButton("🔓 Unlock Unlimited Access", callback_data="pass#unlock_menu")
                        ]])
                    sent_ok = await send_or_edit_with_custom_icons(
                        client=client,
                        chat_id=message.chat.id,
                        text=limit_text,
                        inline_keyboard=limit_api_kb
                    )
                    if not sent_ok:
                        await message.reply_text(limit_text, reply_markup=unlock_kb)

                    # Trigger intelligent Cooldown Reminders (Max 2 reminders)
                    asyncio.create_task(schedule_rate_limit_reminders(
                        client=client,
                        user_id=user_id,
                        user_name=user_obj.first_name if user_obj else "User",
                        rem_sec=rem_sec,
                        window_str=win_verbose
                    ))
                    return

    # 2. Force-Subscribe check (per-bot fsub)
    fsub_channels = await db.get_bot_fsub_channels(bot_id) if bot_id else []
    if not fsub_channels:
        fsub_channels = await db.get_share_fsub_channels()  # fallback global

    if fsub_channels:
        not_joined = await check_all_subscriptions(client, user_id, fsub_channels, bot_id)
        if not_joined:
            f_buttons = []
            channel_num = 1
            _ordinal_sfx = ['ꜱᴛ','ɴᴅ','ʀᴅ','ᴛʜ','ᴛʜ','ᴛʜ','ᴛʜ','ᴛʜ']
            for ch in not_joined:
                invite  = ch.get('invite_link', '')
                is_jr   = ch.get('join_request', False)
                sfx = _ordinal_sfx[min(channel_num - 1, 7)]
                label = f"{channel_num}{sfx} Cʜᴀɴɴᴇʟ"
                channel_num += 1
                if invite:
                    f_buttons.append(InlineKeyboardButton(label, url=invite))

            rows = []
            for i in range(0, len(f_buttons), 2):
                rows.append(f_buttons[i:i+2])
            rows.append([
                InlineKeyboardButton(
                    "Tʀʏ Aɢᴀɪɴ",
                    callback_data=f"fsub_chk_{uuid_str}"
                )
            ])

            # FSub message: custom DB text or auto-generated based on situation
            fsub_msg = await db.get_share_bot_text(bot_id, "fsub_msg") if bot_id else ""
            if not fsub_msg:
                fsub_msg = await db.get_share_text("fsub_msg", "")
            if fsub_msg:
                txt = format_msg(fsub_msg, message.from_user)
            else:
                user_name = message.from_user.first_name or "User"
                has_jr       = any(ch.get('needs_request') for ch in not_joined)
                never_joined = any(ch.get('never_joined') for ch in not_joined)

                if has_jr:
                    # JR channel — join request already pending or needs to be sent
                    txt = (
                        f"<b>🔒  Aᴄᴄᴇss Dᴇɴɪᴇᴅ</b>\n\n"
                        f"Hey <b>{user_name}</b>,\n"
                        f"You must send a <b>Jᴏɪɴ Rᴇǫᴜᴇsᴛ</b> to the channel(s) below."
                        f" Once your request is approved by the admin you will get access automatically.\n\n"
                        f"<i>Already sent a request? Tap <b>Tʀʏ Aɢᴀɪɴ</b> — your request is being reviewed!</i>"
                    )
                else:
                    # Normal channel — never joined
                    txt = (
                        f"<b>🔒  Aᴄᴄᴇss Dᴇɴɪᴇᴅ</b>\n\n"
                        f"Hey <b>{user_name}</b>,\n"
                        f"You must join our update channel(s) below to access these files.\n\n"
                        f"<b>Steps:</b>\n"
                        f"① Tap the channel button → Join\n"
                        f"② Tap <b>Tʀʏ Aɢᴀɪɴ</b> below to unlock your files instantly!"
                    )
            await message.reply_text(txt, reply_markup=InlineKeyboardMarkup(rows))
            return

    # 3. Warm peer cache for source channel (cached — near-instant on repeat requests)
    await _warm_peer(client, source_chat)

    # 5. Deliver
    dl_id = f"{user_id}_{uuid_str}"
    active_downloads.add(dl_id)

    # Concurrently fetch delivery configs (media, about, auto-delete, caption template, custom buttons)
    (
        fetching_media,
        bot_about,
        auto_del_global,
        cap_tpl_bot,
        cap_tpl_global,
        custom_btns_data
    ) = await asyncio.gather(
        db.get_bot_fetching_media(bot_id) if bot_id else asyncio.sleep(0, result=[]),
        db.get_share_bot_about(bot_id) if bot_id else asyncio.sleep(0, result={}),
        db.get_share_autodelete_global(),
        db.get_share_bot_text(bot_id, "custom_caption") if bot_id else asyncio.sleep(0, result=""),
        db.get_share_text("custom_caption", ""),
        db.get_share_bot_buttons(bot_id) if bot_id else asyncio.sleep(0, result=[])
    )

    auto_delete_mins = (bot_about.get('auto_delete', 0) if bot_about else 0) or auto_del_global
    cap_tpl = cap_tpl_bot or cap_tpl_global
    needs_msg_metadata = bool(cap_tpl and any(k in cap_tpl for k in ("{file_name}", "{file_size}", "{caption}")))

    # Show configurable fetching media (GIF / Photo / Video) or fallback to text
    user_lang = await db.get_language(user_id)
    is_hi = bool(user_lang == 'hi')
    cancel_lbl = "रद्द करें" if is_hi else "Cᴀɴᴄᴇʟ"
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton(cancel_lbl, callback_data=f"cancel_dl_{uuid_str}")]])
    if is_hi:
        fetch_text = '<emoji id="6215133834149629990">⏳</emoji>  आपकी फ़ाइलें सुरक्षित रूप से प्राप्त की जा रही हैं, कृपया प्रतीक्षा करें...'
    else:
        fetch_text = '<emoji id="6215133834149629990">⏳</emoji>  Fᴇᴛᴄʜɪɴɢ ʏᴏᴜʀ ꜰɪʟᴇs sᴇᴄᴜʀᴇʟʏ, ᴘʟᴇᴀsᴇ ᴡᴀɪᴛ...'
    sts = None

    if fetching_media:
        import random
        fm = random.choice(fetching_media)
        fid  = fm.get('file_id')
        ftyp = fm.get('media_type', 'photo')
        try:
            if ftyp == 'animation':
                sts = await client.send_animation(
                    user_id, animation=fid, caption=fetch_text,
                    reply_markup=cancel_kb
                )
            elif ftyp == 'video':
                sts = await client.send_video(
                    user_id, video=fid, caption=fetch_text,
                    reply_markup=cancel_kb
                )
            else:
                sts = await client.send_photo(
                    user_id, photo=fid, caption=fetch_text,
                    reply_markup=cancel_kb
                )
            logger.info(f"[Fetch] Sent {ftyp} to user {user_id} via bot {bot_id}")
        except Exception as _fe:
            logger.warning(
                f"[Fetch] Media send FAILED for bot={bot_id} user={user_id} "
                f"type={ftyp} file_id={fid[:30]}... error: {_fe}"
            )
            sts = None

    if sts is None:
        sts = await message.reply_text(fetch_text, reply_markup=cancel_kb)

    sent_ids   = []
    fail_count = 0
    custom_markup = None
    if custom_btns_data:
        row = []
        for btn in custom_btns_data:
            row.append(InlineKeyboardButton(text=btn['text'], url=btn['url']))
        if row:
            custom_markup = InlineKeyboardMarkup([row])

    from pyrogram.errors import FloodWait
    for msg_id in msg_ids:
        if dl_id not in active_downloads:
            break  # cancel handler already edited the status
        
        retry_count = 0
        while retry_count < 3:
            try:
                # Resolve placeholder values only if needed by custom caption
                file_name = "Unknown"
                file_size_str = "Unknown"
                orig_caption = ""

                if needs_msg_metadata:
                    try:
                        src_msg = await client.get_messages(chat_id=source_chat, message_ids=msg_id)
                        if src_msg:
                            orig_caption = src_msg.caption or ""
                            media = (src_msg.document or src_msg.audio or src_msg.video or
                                     src_msg.voice or src_msg.video_note or src_msg.photo)
                            if media:
                                if hasattr(media, "file_name") and media.file_name:
                                    file_name = media.file_name
                                elif hasattr(media, "title") and media.title:
                                    file_name = media.title
                                else:
                                    file_name = "Media_File"

                                if hasattr(media, "file_size") and media.file_size:
                                    file_size_str = _get_readable_file_size(media.file_size)
                    except Exception as _ge:
                        logger.warning(f"Failed to get source message metadata: {_ge}")

                if cap_tpl:
                    # First format user variables
                    rendered_cap = format_msg(cap_tpl, message.from_user)
                    # Next replace custom fillings placeholders
                    rendered_cap = rendered_cap.replace("{file_name}", file_name) \
                                               .replace("{file_size}", file_size_str) \
                                               .replace("{caption}", orig_caption)
                else:
                    rendered_cap = None

                kwargs = {
                    "chat_id": user_id,
                    "from_chat_id": source_chat,
                    "message_id": msg_id,
                    "protect_content": protect_flag,
                }
                if rendered_cap is not None:
                    kwargs["caption"] = rendered_cap
                if custom_markup:
                    kwargs["reply_markup"] = custom_markup
                    
                sent = await client.copy_message(**kwargs)
                if sent:
                    sent_ids.append(sent.id)
                break  # Success
                            
            except FloodWait as fw:
                logger.warning(f"FloodWait for {fw.value}s inside share delivery for user {user_id}")
                if fw.value >= 60:
                    try:
                        import plugins.arya_logger as arya_log
                        asyncio.create_task(arya_log.log_admin_dm("ShareBot FloodWait", f"Delivery blocked. Got FloodWait for {fw.value}s."))
                    except: pass
                try:
                    await message.reply_text(f"<i>⚠️ Telegram Rate Limit Reached! Waiting {fw.value} seconds to deliver remaining files...</i>")
                except: pass
                await asyncio.sleep(fw.value + 1)
                retry_count += 1
                
            except BaseException as copy_err:
                logger.warning(f"copy_message failed for msg {msg_id}: {copy_err}")
                fail_count += 1
                break  # Skip to next message on non-flood errors
                
        await asyncio.sleep(0.05)

    try:
        active_downloads.discard(dl_id)
    except:
        pass

    # ── Record delivery timestamp for 3-strike abuse detection ───────────────
    import time as _ab_time
    _abuse_last_delivery[user_id] = _ab_time.time()
    # Persist to DB so strikes can be evaluated even after a restart
    try:
        strike_rec = await db.get_user_strike(user_id)
        await db.update_user_strike(
            user_id,
            count=strike_rec.get('count', 0),
            last_delivery_ts=_ab_time.time()
        )
    except Exception:
        pass
    try:
        await sts.delete()
    except Exception:
        pass

    # ── Track delivery in DB for global Purge & Rate Limit ───────────────
    if sent_ids:
        try:
            await db.record_user_delivery_hit(user_id)
        except Exception as e:
            logger.warning(f"Failed to record delivery hit: {e}")
        if bot_id:
            try:
                await db.track_delivery(bot_id, user_id, sent_ids)
            except Exception as e:
                logger.error(f"Failed to track delivery for purge: {e}")

    total = len(sent_ids)
    if total == 0:
        await message.reply_text(
            "<b>‣  Dᴇʟɪᴠᴇʀʏ Fᴀɪʟᴇᴅ</b>\n\n"
            "Could not copy any files. "
            "Ensure the Share Bot is an <b>admin</b> in the Database Channel."
        )
        return

    fail_note = f"\n<i>({fail_count} file(s) could not be copied)</i>" if fail_count else ""

    if auto_delete_mins > 0:
        hrs    = auto_delete_mins // 60
        mins_r = auto_delete_mins % 60
        del_str = (f"{hrs}h {mins_r}m" if hrs and mins_r
                   else (f"{hrs} hours" if hrs else f"{auto_delete_mins} minutes"))
        del_tpl = (await db.get_share_bot_text(bot_id, "delete_msg") if bot_id else "") or \
                  await db.get_share_text("delete_msg", "")
        if del_tpl:
            txt = format_msg(del_tpl, message.from_user).replace("{time}", del_str)
        else:
            SMALLCAPS_MAP = {
                'a': 'ᴀ', 'b': 'ʙ', 'c': 'ᴄ', 'd': 'ᴅ', 'e': 'ᴇ', 'f': 'ꜰ', 'g': 'ɢ', 'h': 'ʜ',
                'i': 'ɪ', 'j': 'ᴊ', 'k': 'ᴋ', 'l': 'ʟ', 'm': 'ᴍ', 'n': 'ɴ', 'o': 'ᴏ', 'p': 'ᴘ',
                'q': 'ǫ', 'r': 'ʀ', 's': 'ꜱ', 't': 'ᴛ', 'u': 'ᴜ', 'v': 'ᴠ', 'w': 'ᴡ', 'x': 'x',
                'y': 'ʏ', 'z': 'ᴢ'
            }
            del_str_sc = "".join(SMALLCAPS_MAP.get(c.lower(), c) for c in del_str)
            txt = (
                f"◎ 𝗜𝗠𝗣𝗢𝗥𝗧𝗔𝗡𝗧: {total} FILE(S) DELIVERED!\n\n"
                f"▣ ᴅᴜᴇ ᴛᴏ ᴄᴏᴘʏʀɪɢʜᴛ ʀᴇꜱᴛʀɪᴄᴛɪᴏɴꜱ, ᴀʟʟ ꜰɪʟᴇꜱ ᴀɴᴅ ᴍᴇꜱꜱᴀɢᴇꜱ ᴡɪʟʟ ʙᴇ ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ ʀᴇᴍᴏᴠᴇᴅ ᴀꜰᴛᴇʀ {del_str_sc}.\n\n"
                f"◑ To access them again, simply open the same link button.{fail_note}\n\n"
                f"⧉ Missing a file or looking for a specific episode? Tap \"Stories Chat\" below.\n\n"
                f"⧉ Having trouble with the bot? Tap \"Arya Help\" below."
            )
        kb_help = InlineKeyboardMarkup([[
            InlineKeyboardButton("Arya Help", url="https://t.me/AryaHelpTG"),
            InlineKeyboardButton("Stories Chat", url="https://t.me/+EAc-6v1bmZ1iMDBl"),
        ]])
        notice = await message.reply_text(txt, reply_markup=kb_help)
        asyncio.create_task(
            delete_later(client, user_id, sent_ids, notice.id, auto_delete_mins * 60)
        )
    else:
        suc_tpl = (await db.get_share_bot_text(bot_id, "success_msg") if bot_id else "") or \
                  await db.get_share_text("success_msg", "")
        txt = (format_msg(suc_tpl, message.from_user) if suc_tpl
               else f"◎ 𝗜𝗠𝗣𝗢𝗥𝗧𝗔𝗡𝗧: {total} FILE(S) DELIVERED!\n\n"
                    f"▣ ᴅᴜᴇ ᴛᴏ ᴄᴏᴘʏʀɪɢʜᴛ ʀᴇꜱᴛʀɪᴄᴛɪᴏɴꜱ, ᴀʟʟ ꜰɪʟᴇꜱ ᴀɴᴅ ᴍᴇꜱꜱᴀɢᴇꜱ ᴡɪʟʟ ʙᴇ ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ ʀᴇᴍᴏᴠᴇᴅ ᴀꜰᴛᴇʀ 3 ʜᴏᴜʀꜱ.\n\n"
                    f"◑ To access them again, simply open the same link button.{fail_note}\n\n"
                    f"⧉ Missing a file or looking for a specific episode? Tap \"Stories Chat\" below.\n\n"
                    f"⧉ Having trouble with the bot? Tap \"Arya Help\" below.")
        kb_help = InlineKeyboardMarkup([[
            InlineKeyboardButton("Arya Help", url="https://t.me/AryaHelpTG"),
            InlineKeyboardButton("Stories Chat", url="https://t.me/+EAc-6v1bmZ1iMDBl"),
        ]])
        await message.reply_text(txt, reply_markup=kb_help)

    # ── Increment global delivery counter + Enhanced bilingual Thank-You ──
    if bot_id:
        await db.increment_bot_delivery_count(bot_id, total)
    grand_total = (await db.get_bot_delivery_count(bot_id)) if bot_id else total

    u_name = message.from_user.first_name or "you"
    last   = (" " + message.from_user.last_name) if getattr(message.from_user, "last_name", None) else ""
    full_name = f"{u_name}{last}"
    b_name = client.me.first_name if getattr(client, "me", None) else "this bot"

    don_body = (
        "◑ Thank you for using our service! Your files have been successfully delivered. "
        "These links are permanent and never expire, so you can tap the same button anytime "
        "to access your files again.\n\n"
        "⧉ If you enjoy our platform and want us to keep delivering amazing stories, "
        "please consider supporting us with a small donation.\n\n"
        "▣ Every contribution helps us maintain our servers and expand our library.\n\n"
        "────────────────\n\n"
        "◑ हमारी सेवा का उपयोग करने के लिए धन्यवाद! आपकी फाइलें सफलतापूर्वक डिलीवर हो गई हैं। "
        "ये लिंक स्थायी हैं और कभी expire नहीं होते, इसलिए आप भविष्य में कभी भी उसी बटन पर "
        "टैप करके अपनी फाइलें दोबारा प्राप्त कर सकते हैं।\n\n"
        "⧉ यदि आपको हमारी सेवा पसंद आई है और आप चाहते हैं कि हम निरंतर बेहतरीन कहानियाँ "
        "लाते रहें, तो कृपया donation देकर हमारा सहयोग करें।\n\n"
        "▣ आपका सहयोग हमारे सर्वर को बनाए रखने और हमारी लाइब्रेरी का विस्तार करने में सहायता करता है।"
    )

    thank_txt = (
        f"<b>»</b> <a href='tg://user?id={message.from_user.id}'>{full_name}</a>\n\n"
        f"◎ {total} FILE(S) SENT SUCCESSFULLY!\n\n"
        f"◈ Total delivered by {b_name}: {grand_total:,} files\n\n"
        f"{don_body}"
    )
    
    donate_btn = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Support via UPI", callback_data="sbd#donate")
        ],
        [
            InlineKeyboardButton("Support via Cashfree", url="https://cfpe.me/aryapremium")
        ]
    ])
    try:
        mode = await db.get_share_bot_text(bot_id, "post_delivery_mode") if bot_id else "random"
        if not mode:
            mode = "random"

        show_ad = False
        show_don = False

        if mode == "ad_only":
            show_ad = True
        elif mode == "donation_only":
            show_don = True
        elif mode == "off":
            show_ad = False
            show_don = False
        else:  # "random" or "both"
            import random
            show_ad = random.choice([True, False])
            show_don = not show_ad

        # Active Pass users should NOT see donation messages
        user_pass = await db.get_user_unlimited_pass(user_id)
        if user_pass.get('active', False):
            show_don = False

        if show_ad:
            custom_ad_text = await db.get_share_bot_text(bot_id, "premium_ad_text")
            ad_text = custom_ad_text if custom_ad_text else DEFAULT_PREMIUM_AD_TEXT
            ad_media = await db.get_bot_premium_ad_media(bot_id) if bot_id else None
            
            ad_buttons = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("𝗢𝗽𝗲𝗻 𝗦𝘁𝗼𝗿𝗲", url="https://t.me/UseAryaBot/apminibyarya"),
                    InlineKeyboardButton("Updates", url="https://t.me/AryaPremiumTG")
                ]
            ])
            
            if ad_media:
                mtype = ad_media.get('media_type')
                fid = ad_media.get('file_id')
                if mtype == 'animation':
                    await message.reply_animation(animation=fid, caption=ad_text, reply_markup=ad_buttons)
                elif mtype == 'video':
                    await message.reply_video(video=fid, caption=ad_text, reply_markup=ad_buttons)
                else:
                    await message.reply_photo(photo=fid, caption=ad_text, reply_markup=ad_buttons)
            else:
                await message.reply_text(ad_text, reply_markup=ad_buttons, disable_web_page_preview=True)
        elif show_don:
            await message.reply_text(thank_txt, reply_markup=donate_btn)
    except Exception as e:
        logger.warning(f"[ThankYou] send failed: {e}")

async def _send_welcome(client, message, bot_id: str = None):
    """Send the welcome message + Help/About buttons (sub-second response)."""
    user = message.from_user
    bot_name = client.me.first_name if client.me else "Delivery Bot"

    # Concurrently gather all welcome info in parallel
    custom_wel, global_wel, bot_about, user_lang = await asyncio.gather(
        db.get_share_bot_text(bot_id, "welcome_msg") if bot_id else asyncio.sleep(0, result=""),
        db.get_share_text("welcome_msg", ""),
        db.get_share_bot_about(bot_id) if bot_id else asyncio.sleep(0, result={}),
        db.get_language(user.id)
    )
    custom_wel = custom_wel or global_wel
    is_hi = bool(user_lang == 'hi')
    txt = _get_welcome_text(user, bot_name, custom_wel, lang=user_lang)
    bot_about = bot_about or {}
    welcome_img = random.choice(bot_about.get('menu_image_ids', [])) if bot_about and bot_about.get('menu_image_ids') else None

    lbl_prem = "Arya Premium" if not is_hi else "आर्या प्रीमियम"
    lbl_help = _sc("Help") if not is_hi else "सहायता"
    lbl_about = _sc("About") if not is_hi else "बारे में"
    lbl_upd = _sc("Update Channel") if not is_hi else "अपडेट चैनल"
    lbl_settings = _sc("Settings") if not is_hi else "सेटिंग्स"

    buttons = [
        [
            InlineKeyboardButton("»  " + _sc(lbl_prem), callback_data="sbd#premium"),
        ],
        [
            InlineKeyboardButton(lbl_help, callback_data="sbd#help"),
            InlineKeyboardButton(lbl_about, callback_data="sbd#about"),
        ],
        [
            InlineKeyboardButton(lbl_upd, url=UPDATE_LINK),
        ],
        [
            InlineKeyboardButton("⚙️ " + lbl_settings, callback_data="sbd#settings")
        ]
    ]
    markup = InlineKeyboardMarkup(buttons)

    welcome_api_kb = [
        [
            {"text": "»  " + _sc(lbl_prem), "callback_data": "sbd#premium"}
        ],
        [
            {"text": lbl_help, "callback_data": "sbd#help", "icon_custom_emoji_id": "6023911174188308145"},
            {"text": lbl_about, "callback_data": "sbd#about", "icon_custom_emoji_id": "6021625933759257863"}
        ],
        [
            {"text": lbl_upd, "url": UPDATE_LINK, "icon_custom_emoji_id": "6039422865189638057"}
        ],
        [
            {"text": lbl_settings, "callback_data": "sbd#settings", "icon_custom_emoji_id": "6021637109264160908"}
        ]
    ]

    try:
        if welcome_img:
            wid  = welcome_img.get('file_id') if isinstance(welcome_img, dict) else welcome_img
            wtyp = welcome_img.get('media_type', 'photo') if isinstance(welcome_img, dict) else 'photo'

            # First try Bot API HTTP to preserve custom animated emojis on buttons & caption
            sent_ok = await send_or_edit_with_custom_icons(
                client=client,
                chat_id=user.id,
                text=txt,
                inline_keyboard=welcome_api_kb,
                media_id=wid,
                media_type=wtyp
            )
            if sent_ok:
                return

            try:
                if wtyp == 'animation':
                    await client.send_animation(user.id, animation=wid, caption=txt, reply_markup=markup)
                elif wtyp == 'video':
                    await client.send_video(user.id, video=wid, caption=txt, reply_markup=markup)
                else:
                    await client.send_photo(user.id, photo=wid, caption=txt, reply_markup=markup)
                return
            except Exception as _media_err:
                logger.warning(f"[Welcome] Media send failed ({_media_err}), auto-clearing bad image and falling back to text")
                try:
                    if bot_id:
                        about = await db.get_share_bot_about(bot_id) or {}
                        img_ids = about.get('menu_image_ids', [])
                        bad_fid = wid
                        cleaned = [x for x in img_ids if (x.get('file_id') if isinstance(x, dict) else x) != bad_fid]
                        await db.db.share_config.update_one(
                            {'_id': f'bot_{bot_id}_about'},
                            {'$set': {'menu_image_ids': cleaned}},
                            upsert=True
                        )
                except Exception:
                    pass

        sent_ok = await send_or_edit_with_custom_icons(
            client=client,
            chat_id=user.id,
            text=txt,
            inline_keyboard=welcome_api_kb
        )
        if not sent_ok:
            await message.reply_text(txt, reply_markup=markup)
    except Exception as _wel_err:
        logger.warning(f"[Welcome] Text fallback also failed: {_wel_err}")
        pass

async def _send_premium_menu(client, query_or_msg, edit: bool = False):
    """Show the Arya Premium submenu."""
    txt = (
        f"<b>»  " + _sc("Arya Premium") + "</b>\n\n"
        f"<i>Yaha se aap bina kisi restriction ke stories buy karke sun sakte hain, "
        f"aapko yaha kisi tarah ke channels join karne ki jarurat nahi hain.</i>\n\n"
        f"<b>" + _sc("Features:") + "</b>\n"
        f"• Aap stories ko <b>Forward</b> aur <b>Save</b> kar sakte hain.\n"
        f"• Ye stories aapki <b>'My Stories'</b> me Lifetime tak safe rahengi.\n"
        f"• Zero ads and instant delivery."
    )
    buttons = [
        [
            InlineKeyboardButton("»  " + _sc("Bot"), url="https://t.me/UseAryaBot"),
            InlineKeyboardButton("»  " + _sc("Mini App"), url="https://t.me/UseAryaBot/apminibyarya")
        ],
        [
            InlineKeyboardButton("«  " + _sc("Back"), callback_data="sbd#back")
        ]
    ]
    markup = InlineKeyboardMarkup(buttons)
    if edit and hasattr(query_or_msg, "message"):
        await query_or_msg.message.edit_text(txt, reply_markup=markup, disable_web_page_preview=True)
    else:
        msg = query_or_msg.message if hasattr(query_or_msg, "message") else query_or_msg
        await msg.reply_text(txt, reply_markup=markup, disable_web_page_preview=True)


async def _send_help(client, message, bot_id: str = None):
    """Send the Help menu for /start help."""
    txt = _get_help_text(message.from_user)
    buttons = [
        [InlineKeyboardButton("»  " + _sc("Support"), url=SUPPORT_LINK)],
        [InlineKeyboardButton("«  " + _sc("Back"), callback_data="sbd#back")],
        [InlineKeyboardButton("»  " + _sc("Update Channel"), url=UPDATE_LINK)]
    ]
    try:
        await message.reply_text(txt, reply_markup=InlineKeyboardMarkup(buttons))
    except Exception:
        pass


async def _send_about(client, query_or_msg, bot_id: str = None, edit: bool = True):
    """Send or edit the About section inline — always edits the same message."""
    bot_name = client.me.first_name if client.me else "Delivery Bot"
    about = await db.get_share_bot_about(bot_id) if bot_id else {}

    owner_name   = about.get('owner_name', 'JeetX')
    owner_link   = about.get('owner_link', 'https://t.me/MeJeetX')
    update_chan  = about.get('update_chan', 'Arya Bot | Updates')
    update_link  = about.get('update_link', UPDATE_LINK)
    support_chan = about.get('support_chan', 'Light Chat')
    support_link = about.get('support_link', SUPPORT_LINK)
    from plugins.commands import get_bot_version
    version      = get_bot_version()
    about_text   = about.get('custom_text', None)
    
    msg = query_or_msg if hasattr(query_or_msg, 'photo') else getattr(query_or_msg, 'message', query_or_msg)
    user = getattr(query_or_msg, 'from_user', getattr(msg, 'from_user', None))

    if about_text:
        # Custom text: do NOT apply _sc — user may have hand-crafted formatting/links
        txt = _get_base_header(user) + about_text
    else:
        # Build the body with clickable HTML links — do NOT pass through _sc()
        # _sc() converts every ASCII char to Unicode small-caps, which destroys href URLs
        txt = (
            f"{_get_base_header(user)}"
            f"<b>»  ᴀʙᴏᴜᴛ ᴍᴇ</b>\n\n"
            f"<b>‣  ɴᴀᴍᴇ:</b>  {bot_name}\n"
            f"<b>‣  ᴏᴘᴇʀᴀᴛᴇᴅ ʙʏ:</b>  Arya Bot\n"
            f"<b>‣  ᴏᴡɴᴇʀ:</b>  <a href=\"{owner_link}\">{owner_name}</a>\n"
            f"<b>‣  ᴜᴘᴅᴀᴛᴇꜱ:</b>  <a href=\"{update_link}\">{update_chan}</a>\n"
            f"<b>‣  ꜱᴜᴘᴘᴏʀᴛ:</b>  <a href=\"{support_link}\">{support_chan}</a>\n"
            f"<b>‣  ᴠᴇʀꜱɪᴏɴ:</b>  {version}"
        )

    buttons = [[InlineKeyboardButton("«  " + _sc("Back"), callback_data="sbd#back")]]
    markup  = InlineKeyboardMarkup(buttons)

    is_media_msg = bool(getattr(msg, 'photo', None) or getattr(msg, 'animation', None) or getattr(msg, 'video', None))
    try:
        if is_media_msg:
            await msg.edit_caption(caption=txt, reply_markup=markup)
        else:
            await msg.edit_text(txt, reply_markup=markup,
                                disable_web_page_preview=True)
    except Exception as e:
        logger.warning(f"_send_about edit failed: {e}")

async def _process_delivery_button(client, query):
    """Handle inline buttons on the welcome/help/about messages."""
    user_id = query.from_user.id
    try:
        ban_status = await db.get_ban_status(user_id)
        if ban_status.get('is_banned'):
            await query.answer("⛔ Operation not allowed.", show_alert=True)
            return
    except Exception:
        pass
        
    cmd = query.data.split('#')[1] if '#' in query.data else ''
    bot_id = str(client.me.id) if client.me else None
    msg = query.message
    is_media_msg = bool(getattr(msg, 'photo', None) or getattr(msg, 'animation', None) or getattr(msg, 'video', None))

    if cmd == "help":
        await query.answer()
        txt = _get_help_text(query.from_user)
        user_lang = await db.get_language(user_id)
        is_hi = bool(user_lang == 'hi')
        lbl_upd = _sc("Update Channel") if not is_hi else "अपडेट चैनल"
        lbl_settings = _sc("Settings") if not is_hi else "सेटिंग्स"
        buttons = [
            [InlineKeyboardButton("»  " + _sc("Support"), url=SUPPORT_LINK)],
            [InlineKeyboardButton("«  " + _sc("Back"), callback_data="sbd#back")],
            [InlineKeyboardButton(lbl_upd, url=UPDATE_LINK)],
            [InlineKeyboardButton("⚙️ " + lbl_settings, callback_data="sbd#settings")]
        ]
        help_api_kb = [
            [{"text": "»  " + _sc("Support"), "url": SUPPORT_LINK, "icon_custom_emoji_id": "6030833407339008632"}],
            [{"text": "«  " + _sc("Back"), "callback_data": "sbd#back"}],
            [{"text": lbl_upd, "url": UPDATE_LINK, "icon_custom_emoji_id": "6039422865189638057"}],
            [{"text": lbl_settings, "callback_data": "sbd#settings", "icon_custom_emoji_id": "6021637109264160908"}]
        ]
        markup = InlineKeyboardMarkup(buttons)
        try:
            if is_media_msg:
                sent_ok = await send_or_edit_with_custom_icons(
                    client=client,
                    chat_id=msg.chat.id,
                    text=txt,
                    inline_keyboard=help_api_kb,
                    message_id=msg.id,
                    is_media_edit=True
                )
                if not sent_ok:
                    await msg.edit_caption(caption=txt, reply_markup=markup)
            else:
                sent_ok = await send_or_edit_with_custom_icons(
                    client=client,
                    chat_id=msg.chat.id,
                    text=txt,
                    inline_keyboard=help_api_kb,
                    message_id=msg.id
                )
                if not sent_ok:
                    await msg.edit_text(txt, reply_markup=markup)
        except Exception: pass

    elif cmd == "settings":
        await query.answer()
        user_lang = await db.get_language(user_id)
        is_hi = bool(user_lang == 'hi')
        if is_hi:
            set_txt = (
                '<emoji id="6021637109264160908">⚙️</emoji> <b>यूजर सेटिंग्स</b>\n'
                "──────────────────────\n\n"
                "डिलीवरी बॉट और पास सब्सक्रिप्शन के लिए अपनी सेटिंग्स चुनें:"
            )
            lbl_lang = "भाषा"
            lbl_txns = "मेरे ट्रांसक्शन्स"
            lbl_back = "← वापस"
        else:
            set_txt = (
                '<emoji id="6021637109264160908">⚙️</emoji> <b>User Settings</b>\n'
                "──────────────────────\n\n"
                "Configure your delivery bot and pass subscription preferences:"
            )
            lbl_lang = "Language"
            lbl_txns = "My Transactions"
            lbl_back = "← Back"

        set_buttons = [
            [InlineKeyboardButton(lbl_lang, callback_data="pass#lang_menu")],
            [InlineKeyboardButton(lbl_txns, callback_data="pass#my_transactions")],
            [InlineKeyboardButton(lbl_back, callback_data="sbd#back")]
        ]
        set_api_kb = [
            [{"text": lbl_lang, "callback_data": "pass#lang_menu", "icon_custom_emoji_id": "6030768072296502910"}],
            [{"text": lbl_txns, "callback_data": "pass#my_transactions", "icon_custom_emoji_id": "6035297458907519073"}],
            [{"text": lbl_back, "callback_data": "sbd#back"}]
        ]
        try:
            if is_media_msg:
                sent_ok = await send_or_edit_with_custom_icons(
                    client=client,
                    chat_id=msg.chat.id,
                    text=set_txt,
                    inline_keyboard=set_api_kb,
                    message_id=msg.id,
                    is_media_edit=True
                )
                if not sent_ok:
                    await msg.edit_caption(caption=set_txt, reply_markup=InlineKeyboardMarkup(set_buttons))
            else:
                sent_ok = await send_or_edit_with_custom_icons(
                    client=client,
                    chat_id=msg.chat.id,
                    text=set_txt,
                    inline_keyboard=set_api_kb,
                    message_id=msg.id
                )
                if not sent_ok:
                    await msg.edit_text(set_txt, reply_markup=InlineKeyboardMarkup(set_buttons))
        except Exception:
            pass

    elif cmd == "premium":
        await query.answer()
        await _send_premium_menu(client, query, edit=True)

    elif cmd == "about":
        await query.answer()
        await _send_about(client, query, bot_id=bot_id, edit=True)

    elif cmd == "donate":
        await query.answer()
        sup_text = (
            "◎ 𝗦𝗨𝗣𝗣𝗢𝗥𝗧 𝗔𝗥𝗬𝗔\n\n"
            "▣ Your support helps keep our servers running and allows us to continue delivering high-quality content.\n\n"
            "◈ Direct UPI Details\n\n"
            "▸ UPI ID: <code>Q56571430@ybl</code>\n"
            "▸ Name: Jeetesh Meena\n\n"
            "◑ Select an amount below to generate a direct payment QR code."
        )
        buttons = [
            [
                InlineKeyboardButton("₹50", callback_data="sbd#pay_upi#50"),
                InlineKeyboardButton("₹100", callback_data="sbd#pay_upi#100"),
                InlineKeyboardButton("₹200", callback_data="sbd#pay_upi#200")
            ],
            [
                InlineKeyboardButton("₹500", callback_data="sbd#pay_upi#500"),
                InlineKeyboardButton("⧉ Custom Amount", callback_data="sbd#pay_upi#custom")
            ]
        ]
        try:
            await client.send_message(query.from_user.id, sup_text, reply_markup=InlineKeyboardMarkup(buttons))
        except Exception:
            pass

    elif cmd == "pay_upi":
        parts = query.data.split('#')
        am = parts[2] if len(parts) > 2 else "custom"
        await query.answer()
        
        if am == "custom":
            upi_uri = "upi://pay?pa=Q56571430@ybl&pn=Jeetesh%20Meena&tn=Payment%20for%20Support%20%5B%20Arya%20%5D&cu=INR"
            am_val = "Custom Amount"
        else:
            upi_uri = f"upi://pay?pa=Q56571430@ybl&pn=Jeetesh%20Meena&am={am}&tn=Payment%20for%20Support%20%5B%20Arya%20%5D&cu=INR"
            am_val = f"₹{am}"
            
        caption = (
            "◎ 𝗦𝗖𝗔𝗡 𝗢𝗥 𝗧𝗔𝗣 𝗧𝗢 𝗦𝗨𝗣𝗣𝗢𝗥𝗧\n\n"
            f"▸ Amount: {am_val}\n"
            "▸ UPI ID: <code>Q56571430@ybl</code>\n"
            "▸ Name: Jeetesh Meena\n\n"
            "◑ Scan the QR code above or use the payment options below to complete your support."
        )
        
        import urllib.parse
        encoded_uri = urllib.parse.quote(upi_uri)
        qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=500x500&margin=2&data={encoded_uri}"
        
        try:
            await msg.delete()
            await client.send_photo(query.from_user.id, photo=qr_url, caption=caption)
        except Exception as e:
            logger.error(f"Support QR Error: {e}")

    elif cmd == "razorpay":
        await query.answer()
        from config import Config
        rz_key = Config.RAZORPAY_KEY
        rz_secret = Config.RAZORPAY_SECRET
        if not rz_key or not rz_secret:
            error_txt = (
                "◎ 𝗥𝗔𝗭𝗢𝗥𝗣𝗔𝗬 𝗨𝗡𝗔𝗩𝗔𝗜𝗟𝗔𝗕𝗟𝗘\n\n"
                "▣ Unable to generate a Razorpay payment link at this time.\n\n"
                "◈ Razorpay keys are not configured.\n\n"
                "◑ Please use the UPI payment method instead.\n\n"
                "▸ UPI ID: <code>Q56571430@ybl</code>"
            )
            try:
                await client.send_message(
                    query.from_user.id,
                    error_txt,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("◈ Use UPI Instead", callback_data="sbd#donate")
                    ]])
                )
            except Exception:
                pass
            return

        # Show amount selection panel
        rz_txt = (
            "◎ 𝗦𝗨𝗣𝗣𝗢𝗥𝗧 𝗩𝗜𝗔 𝗥𝗔𝗭𝗢𝗥𝗣𝗔𝗬\n\n"
            "▣ Cards, Net Banking, UPI, and Wallets accepted.\n\n"
            "◈ International payments are also supported.\n\n"
            "◑ Select an amount below to generate your payment link."
        )
        rz_btns = [
            [
                InlineKeyboardButton("₹49",  callback_data="sbd#pay_rzp#49"),
                InlineKeyboardButton("₹99",  callback_data="sbd#pay_rzp#99"),
                InlineKeyboardButton("₹199", callback_data="sbd#pay_rzp#199"),
            ],
            [
                InlineKeyboardButton("₹499", callback_data="sbd#pay_rzp#499"),
                InlineKeyboardButton("₹999", callback_data="sbd#pay_rzp#999"),
                InlineKeyboardButton("⧉ Custom Amount", callback_data="sbd#pay_rzp#custom"),
            ],
            [InlineKeyboardButton("◈ Use UPI Instead", callback_data="sbd#donate")],
        ]
        try:
            await client.send_message(query.from_user.id, rz_txt, reply_markup=InlineKeyboardMarkup(rz_btns))
        except Exception:
            pass

    elif cmd == "pay_rzp":
        parts = query.data.split('#')
        am_str = parts[2] if len(parts) > 2 else "99"
        uid    = query.from_user.id
        u_name = getattr(query.from_user, 'first_name', 'User') or 'User'
        await query.answer()

        from config import Config
        import aiohttp, json as _json

        rz_key    = Config.RAZORPAY_KEY
        rz_secret = Config.RAZORPAY_SECRET

        if am_str == "custom":
            # Ask user to type custom amount
            ask_msg = await client.send_message(
                uid,
                "<b>📝 " + _sc("enter your custom amount (in ₹)") + "</b>\n\n"
                "<i>Type the amount you wish to donate (e.g. 150, 350, 1000):</i>\n"
                "/cancel to abort."
            )
            try:
                resp = await client.listen(chat_id=uid, timeout=120)
                txt = (resp.text or "").strip()
                await resp.delete()
                if txt.lower() in ("/cancel", "cancel"):
                    await ask_msg.edit_text("<i>Cancelled.</i>")
                    return
                if not txt.isdigit() or int(txt) < 1:
                    await ask_msg.edit_text("<i>Invalid amount. Please try again.</i>")
                    return
                amount = int(txt)
                await ask_msg.delete()
            except Exception:
                try: await ask_msg.edit_text("<i>Timed out.</i>")
                except: pass
                return
        else:
            amount = int(am_str)

        # Generate Razorpay Payment Link via API
        gen_msg = await client.send_message(uid, "<i>⏳ Generating your payment link...</i>")
        try:
            if not rz_key or not rz_secret:
                raise ValueError("Razorpay keys not configured")

            payload = {
                "amount": amount * 100,   # Razorpay uses paise
                "currency": "INR",
                "accept_partial": False,
                "description": f"Arya Bot Support — {u_name}",
                "customer": {"name": u_name},
                "notify": {"sms": False, "email": False},
                "reminder_enable": False,
                "notes": {"telegram_id": str(uid)},
                "callback_url": "",
                "callback_method": "",
            }
            auth = aiohttp.BasicAuth(rz_key, rz_secret)
            async with aiohttp.ClientSession(auth=auth) as sess:
                async with sess.post(
                    "https://api.razorpay.com/v1/payment_links",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    data = await resp.json()

            if resp.status != 200:
                raise ValueError(data.get("error", {}).get("description", "API error"))

            pay_url  = data["short_url"]
            link_id  = data["id"]

            link_txt = (
                f"<blockquote><b>" + _sc("your razorpay payment link") + "</b>\n\n"
                f"<b>‣ " + _sc("amount:") + "</b>  <code>₹{amount}</code>\n"
                f"<b>‣ " + _sc("link id:") + "</b>  <code>{link_id}</code>\n\n"
                f"<i>✅ Cards, Net Banking, UPI, Wallets accepted.\n"
                f"This link is valid for 24 hours and is unique to you.</i></blockquote>"
            )
            btn = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"Pay ₹{amount}", url=pay_url)
            ]])
            await gen_msg.delete()
            await client.send_message(uid, link_txt, reply_markup=btn)

        except Exception as rz_err:
            logger.error(f"[Razorpay] Link generation failed: {rz_err}")
            # Fallback: show UPI if Razorpay fails
            err_msg = str(rz_err)
            if "keys not configured" in err_msg.lower() or "keys not found" in err_msg.lower():
                err_detail = "Razorpay keys are not configured."
            else:
                err_detail = err_msg.strip()
                if err_detail and not err_detail.endswith('.'):
                    err_detail += '.'

            error_txt = (
                "◎ 𝗥𝗔𝗭𝗢𝗥𝗣𝗔𝗬 𝗨𝗡𝗔𝗩𝗔𝗜𝗟𝗔𝗕𝗟𝗘\n\n"
                "▣ Unable to generate a Razorpay payment link at this time.\n\n"
                f"◈ {err_detail}\n\n"
                "◑ Please use the UPI payment method instead.\n\n"
                "▸ UPI ID: <code>Q56571430@ybl</code>"
            )
            await gen_msg.edit_text(
                error_txt,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◈ Use UPI Instead", callback_data="sbd#donate")
                ]])
            )



    elif cmd == "back" or cmd == "main_menu":
        await query.answer()
        bot_name = client.me.first_name if client.me else "Delivery Bot"
        custom_wel = (await db.get_share_bot_text(bot_id, "welcome_msg") if bot_id else "") or await db.get_share_text("welcome_msg", "")
        user_lang = await db.get_language(query.from_user.id)
        is_hi = bool(user_lang == 'hi')
        txt = _get_welcome_text(query.from_user, bot_name, custom_wel, lang=user_lang)
        
        lbl_prem = "Arya Premium" if not is_hi else "आर्या प्रीमियम"
        lbl_help = _sc("Help") if not is_hi else "सहायता"
        lbl_about = _sc("About") if not is_hi else "बारे में"
        lbl_upd = _sc("Update Channel") if not is_hi else "अपडेट चैनल"
        lbl_settings = _sc("Settings") if not is_hi else "सेटिंग्स"

        buttons = [
            [
                InlineKeyboardButton("»  " + _sc(lbl_prem), callback_data="sbd#premium"),
            ],
            [
                InlineKeyboardButton(lbl_help, callback_data="sbd#help"),
                InlineKeyboardButton(lbl_about, callback_data="sbd#about"),
            ],
            [
                InlineKeyboardButton(lbl_upd, url=UPDATE_LINK),
            ],
            [
                InlineKeyboardButton("⚙️ " + lbl_settings, callback_data="sbd#settings")
            ]
        ]
        markup = InlineKeyboardMarkup(buttons)

        welcome_api_kb = [
            [
                {"text": "»  " + _sc(lbl_prem), "callback_data": "sbd#premium"}
            ],
            [
                {"text": lbl_help, "callback_data": "sbd#help", "icon_custom_emoji_id": "6023911174188308145"},
                {"text": lbl_about, "callback_data": "sbd#about", "icon_custom_emoji_id": "6021625933759257863"}
            ],
            [
                {"text": lbl_upd, "url": UPDATE_LINK, "icon_custom_emoji_id": "6039422865189638057"}
            ],
            [
                {"text": lbl_settings, "callback_data": "sbd#settings", "icon_custom_emoji_id": "6021637109264160908"}
            ]
        ]
        try:
            if is_media_msg:
                sent_ok = await send_or_edit_with_custom_icons(
                    client=client,
                    chat_id=msg.chat.id,
                    text=txt,
                    inline_keyboard=welcome_api_kb,
                    message_id=msg.id,
                    is_media_edit=True
                )
                if not sent_ok:
                    await msg.edit_caption(caption=txt, reply_markup=markup)
            else:
                sent_ok = await send_or_edit_with_custom_icons(
                    client=client,
                    chat_id=msg.chat.id,
                    text=txt,
                    inline_keyboard=welcome_api_kb,
                    message_id=msg.id
                )
                if not sent_ok:
                    await msg.edit_text(txt, reply_markup=markup)
        except Exception:
            pass
    else:
        await query.answer()


async def _process_delivery_cancel(client, query):
    """Handle cancel button during file delivery."""
    uuid_str = query.data.replace("cancel_dl_", "", 1)
    dl_id = f"{query.from_user.id}_{uuid_str}"
    if dl_id in active_downloads:
        active_downloads.discard(dl_id)
        await query.answer("Download cancelled.", show_alert=True)
        try:
            await query.message.edit_text("<b>🚫 Dᴏᴡɴʟᴏᴀᴅ Cᴀɴᴄᴇʟʟᴇᴅ.</b>")
        except Exception:
            pass
    else:
        await query.answer("Already finished or cancelled.", show_alert=True)

async def _process_fsub_check(client, query):
    """Handle Try Again callback for Force Subscribe."""
    uuid_str = query.data.replace("fsub_chk_", "", 1)
    
    # 1. Animation Step 1
    await query.message.edit_text("Lᴇᴛ ᴍᴇ ᴄʜᴇᴄᴋ ꜰᴏʀ ʏᴏᴜ...")
    
    bot_id = str(client.me.id) if client.me else None
    user_id = query.from_user.id
    
    # 2. Re-check FSub
    fsub_channels = await db.get_bot_fsub_channels(bot_id) if bot_id else []
    if not fsub_channels:
        fsub_channels = await db.get_share_fsub_channels()
        
    not_joined = []
    if fsub_channels:
        not_joined = await check_all_subscriptions(client, user_id, fsub_channels, bot_id)
        
    if not_joined:
        # Animation Step 2: Failed
        f_buttons = []
        channel_num = 1
        for ch in not_joined:
            invite  = ch.get('invite_link', '')
            is_jr   = ch.get('join_request', False)
            label   = f"Jᴏɪɴ Cʜᴀɴɴᴇʟ {channel_num}"
            channel_num += 1
            if invite:
                emoji = "» " if is_jr else "» "
                f_buttons.append(InlineKeyboardButton(f"{emoji} {label}", url=invite))

        rows = []
        for i in range(0, len(f_buttons), 2):
            rows.append(f_buttons[i:i+2])
        rows.append([
            InlineKeyboardButton(
                "Tʀʏ Aɢᴀɪɴ",
                callback_data=f"fsub_chk_{uuid_str}"
            )
        ])
        
        await query.message.edit_text(
            "I ᴄᴀɴɴᴏᴛ ɢɪᴠᴇ ʏᴏᴜ ᴀᴄᴄᴇꜱꜱ ʙᴇᴄᴀᴜꜱᴇ ʏᴏᴜ ʜᴀᴠᴇ ɴᴏᴛ ꜰᴜʟꜰɪʟʟᴇᴅ ᴛʜᴇ ʀᴇQᴜɪʀᴇᴍᴇɴᴛꜱ. Tʀʏ ᴀɢᴀɪɴ.",
            reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    # Animation Step 2: Success
    # Delete the check message and hand off to _process_start by spoofing a message
    try:
        await query.message.delete()
    except Exception: pass
    
    msg = query.message
    msg.from_user = query.from_user
    msg.command = ["start", uuid_str]
    await _process_start(client, msg)



# ── Kawaii Reminder Custom Emojis (Filtered for positive/cute interactions) ───
KAWAII_REMINDER_EMOJIS = [
    ("❤️", "5848252019713776041"),
    ("🎊", "5847925499120065335"),
    ("🎊", "5847937885805747359"),
    ("💕", "5850733759191586694"),
    ("💕", "5850346718213708275"),
    ("💕", "5850220665218538131"),
    ("💕", "5848278231899184821"),
    ("😴", "5848239474114306698"),
    ("😴", "5850225969503148548"),
    ("🥰", "5848298950821418909"),
    ("🥰", "5848390386380183682"),
    ("😘", "5848133826508757485"),
    ("😘", "5850193830262874586"),
    ("🎈", "5850502522447338230"),
    ("🥰", "5847932461262052099"),
    ("🥰", "5850603930920163907"),
    ("🎈", "5848050547092889314"),
    ("💙", "5848264045622205527"),
    ("❤️", "5850268661478070978"),
    ("🥰", "5850441039990495852"),
    ("🥰", "5848079941849062172"),
    ("💕", "5850690105143989976"),
    ("💕", "5848449970461483776"),
    ("❤️", "5848168525549542704"),
    ("❤️", "5848312668946963057"),
    ("😘", "5848277312776182595"),
    ("😘", "5850416717590698464"),
    ("🩷", "5848084868176550792"),
    ("🩷", "5847976403072456935"),
    ("✨", "5848479610030791324"),
    ("✨", "5848137567425272287"),
    ("👋", "5850325870442454209"),
    ("👋", "5850355368277842985"),
    ("😏", "5850651721021267320"),
    ("😳", "5850306568859425667"),
    ("❔", "5850371452930366117"),
    ("😍", "5847968113785576864"),
    ("‼️", "5848036073053100626"),
    ("🤩", "5850688052149623747"),
    ("☺️", "5848361086113292480"),
    ("✨", "5850463528439257665"),
    ("😢", "5848054296599337352"),
    ("😭", "5848318243814513869"),
    ("🌈", "5848404332138995312"),
    ("😏", "5850445021425178687"),
    ("😭", "5847933199996427721"),
    ("😍", "5850495590370122561"),
    ("🤔", "5850502432253025037"),
    ("🙇", "5850476228657551204"),
    ("🙏", "5848038628558641941"),
    ("😎", "5850502041411000133"),
    ("😘", "5848151040737680444"),
    ("😜", "5850506340673264483"),
    ("☺️", "5847981853385956294"),
    ("😁", "5847961057154310044"),
    ("😳", "5850393460342792008"),
    ("🥰", "5850672104936054606"),
    ("🥺", "5850529464777186361"),
    ("🥺", "5850456149685445756"),
    ("☺️", "5847986139763317354"),
    ("😉", "5848337296289438813"),
    ("😍", "5850188384244341454"),
    ("😂", "5850725040407976081"),
    ("🥺", "5850335173341617597"),
    ("🥺", "5848075311874317861"),
    ("🫤", "5848457301970655817"),
    ("☺️", "5850665872938508852"),
    ("👋", "5850698420200675761"),
    ("🥺", "5848093445226240381"),
    ("😯", "5850186520228535876"),
    ("😍", "5850607349714131016"),
    ("😪", "5850526166242302581"),
    ("😂", "5850599786276723541"),
    ("😐", "5850536182106036653"),
    ("😳", "5848000265910753463"),
    ("😜", "5848037198334532036"),
    ("☺️", "5850379566123588378"),
    ("😎", "5850192820945558613"),
    ("🫡", "5850248900333542155"),
    ("‼️", "5850577538346130136"),
    ("😃", "5850389603462159449"),
    ("😍", "5850722317398710493"),
    ("😏", "5848134595307903838"),
    ("✨", "5848050456898575301"),
    ("🩷", "5850631929811966291"),
    ("🌈", "5850582438903814187"),
    ("🙇", "5850640017235385099"),
    ("🙏", "5850192391448827964"),
    ("❔", "5848479957923142528"),
    ("‼️", "5847929364590631092"),
    ("☺️", "5848326361302703424"),
    ("😍", "5848341440932878268"),
    ("😁", "5848019515954174149"),
    ("😂", "5848349820414074768"),
    ("😜", "5850276882045477026"),
    ("😢", "5850301784265857748"),
    ("🫢", "5850689125891448128"),
    ("☺️", "5850248883153672421"),
    ("🤔", "5848435934508361589"),
    ("😭", "5850485643225864383"),
    ("😉", "5848099329331435525"),
    ("😍", "5850699678626093300"),
    ("☺️", "5848226919924898730"),
    ("😘", "5847984666589535250"),
    ("👍", "5850422756314716370"),
    ("🤨", "5848441225908067066"),
    ("🤨", "5850549921706417222"),
    ("😶", "5848149112297363951"),
    ("😶", "5848444906695040049"),
    ("😳", "5848359587169704834"),
    ("😀", "5848350048047340496"),
    ("😳", "5850229710419662994"),
    ("😳", "5850658562904169846"),
    ("😁", "5850331015813274096"),
    ("☺️", "5850725156372093951"),
    ("😳", "5850349342438725882"),
    ("😳", "5850258521060285547"),
    ("🥺", "5850409278707341919"),
    ("🥺", "5848245701816884635"),
    ("👍", "5848406440967936253"),
    ("💌", "5850176641803753392"),
    ("🦕", "5850409351721786284"),
    ("🛸", "5850700322871188207"),
    ("🏠", "5850713461176146954"),
    ("🩷", "5848291370204142990"),
    ("☕️", "5848214004958239776"),
    ("🍰", "5848146251849145548"),
    ("🚙", "5850392665773840870"),
    ("💫", "5850719383936047212"),
    ("✨", "5850638110269904627"),
    ("🩵", "5850617640455773442"),
    ("💕", "5850415549359593340"),
    ("💘", "5850577933483121553"),
    ("🩷", "5848266893185522044"),
    ("💘", "5850719353871276137"),
    ("🩷", "5850546992538720577"),
    ("😳", "5796517991877189249"),
    ("☺️", "5793918377021939684"),
    ("🤩", "5794427266222005394"),
    ("😳", "5794214665340853357"),
    ("☺️", "5794002257733232164"),
    ("☺️", "5796220866039652145"),
    ("😢", "5796420723752835884"),
    ("😭", "5796469235408444771"),
    ("☺️", "5794086615185891025"),
    ("☺️", "5796326152867945946"),
    ("😶", "5796282400036101142"),
    ("🐱", "5796350157440163851"),
    ("🥺", "5794411207339286580"),
    ("🫥", "5796311902166458160"),
    ("😘", "5794313690106830178"),
    ("🥰", "5796195113415744617"),
    ("😳", "5796318434811716192"),
    ("☺️", "5796449478558884019"),
    ("🤩", "5796273934655561367"),
    ("😳", "5796190930117598062"),
    ("☺️", "5794138665894551392"),
    ("☺️", "5796435872102489500"),
    ("😶", "5796428257125474474"),
    ("☺️", "5796151090000960903"),
    ("☺️", "5796254143446262590"),
    ("😢", "5796310270078899443"),
    ("😭", "5794166492987660945"),
    ("🐶", "5794376710161964211"),
    ("🥺", "5796214067106421639"),
    ("🫥", "5796329112100412694"),
    ("😘", "5794442710924402354"),
    ("🥰", "5796351622024011473"),
    ("🩷", "5794232352016179004"),
    ("🎁", "5794209537149901836"),
    ("🎁", "5796616144764804167"),
    ("💕", "5796353185392111831"),
    ("🥰", "5796345471630843323"),
    ("🥰", "5794335809188404698"),
    ("✅", "6107070704635616577"),
    ("🗓", "6107109342161411278"),
    ("😃", "6107053722334927124"),
    ("🎁", "6105098382638848920"),
    ("🍭", "6107316724657299003"),
    ("➡️", "6131950689972133532"),
    ("➡️", "6134211109785183075"),
    ("➡️", "6134352478633729472"),
    ("💙", "6134092285219969042"),
    ("⤴️", "6131767281983692112"),
    ("👉", "6132182635385987381"),
    ("📱", "6134464916582573284"),
    ("📱", "6134142282934263825"),
    ("📱", "6136622020957314310"),
    ("📱", "6134315705123742183"),
    ("📱", "6134026559335439774"),
    ("📱", "6134434740142352967"),
    ("👤", "6152280926257684465"),
    ("❤️", "6154381689251437980"),
    ("〰️", "6152430897925726578"),
    ("🌉", "6152032939140981100"),
    ("🔝", "6152140897438934569"),
    ("🛡", "6154180401314143967"),
    ("🧲", "6151947834364010808"),
    ("💼", "6152161023655683060"),
    ("📈", "6154290047534243641"),
    ("🔥", "6154579240567183937"),
    ("©", "6154495866662036822"),
    ("🚫", "6152164833291673623"),
    ("❌", "6151993038894801105"),
    ("👑", "6154731449913188477"),
    ("📱", "6156514071794427904"),
    ("📱", "6156937778908112226"),
    ("🔑", "6156731345599995679"),
    ("🛠", "6156655419168138186"),
    ("📨", "6156932478918467205"),
    ("👩💻", "6156623374417140947"),
    ("🤑", "6298394287738463334"),
    ("🤑", "6298788874973880694"),
    ("💰", "6298302830704860641"),
    ("⬅️", "6327723159113966200"),
    ("➡️", "6066536525577856112"),
    ("✔️", "6066765043607805843"),
    ("✉️", "6064478523278500909"),
    ("1️⃣", "6066875016245420973"),
    ("2️⃣", "6066814001940013417"),
    ("👛", "6084775744150448343"),
    ("💬", "6084741212613388660"),
    ("👩💻", "6084638640204422843"),
    ("💰", "6084695716024821348"),
    ("🫂", "6087067835052334984"),
    ("🤝", "6084419150195728292"),
    ("👤", "6086963871073968701"),
    ("⚙️", "6087118391112376527"),
    ("1️⃣", "6084733528916893740"),
    ("👤", "6086867401813532902"),
    ("🔢", "6086719749427831051"),
    ("🚀", "6084602502349595551"),
    ("🐁", "6084673519633834779"),
    ("📍", "6086715733633411656"),
    ("📊", "6086874613063624715"),
    ("2️⃣", "6089198632752389882"),
    ("3️⃣", "6086945664707600755"),
    ("📉", "6087132482900074858"),
    ("🕹", "6086667174733161703"),
    ("🟢", "6087027281971127830"),
    ("⚡", "6086766899578807133"),
    ("👋", "6088984206510137565"),
    ("📁", "6089089763921372178"),
    ("🔹", "6088875578197287717"),
    ("✏️", "6091344368348702302"),
    ("🔍", "6089396712349114126"),
    ("📴", "6088972489839354852"),
    ("📝", "6091346949624044877"),
    ("➕", "6089374558907802372"),
    ("✨", "6088884108002337674"),
    ("📣", "6091418598268477287"),
]


_active_cooldown_reminders: dict = {}  # {user_id: asyncio.Task}

def _cancel_cooldown_reminders(user_id: int):
    """Cancel any active cooldown reminder task for this user."""
    task = _active_cooldown_reminders.pop(user_id, None)
    if task and not task.done():
        try:
            task.cancel()
        except Exception:
            pass

def get_current_festival(lang='en') -> str:
    """Returns Indian festival season greeting if active, else None."""
    import datetime
    try:
        import pytz
        now = datetime.datetime.now(pytz.timezone('Asia/Kolkata'))
    except Exception:
        now = datetime.datetime.now()

    month = now.month
    day = now.day

    # 1. Raksha Bandhan & Independence Season (August)
    if month == 8:
        if lang == 'hi':
            return "रक्षाबंधन और आज़ादी स्पेशल महा-ऑफर"
        return "Raksha Bandhan & Festive Special Offer"
    # 2. Ganesh Chaturthi & Janmashtami Season (September)
    elif month == 9:
        if lang == 'hi':
            return "गणेश चतुर्थी और जन्माष्टमी स्पेशल ऑफर"
        return "Festive Season Special Offer"
    # 3. Navratri, Dussehra & Diwali (October - November)
    elif month in (10, 11):
        if lang == 'hi':
            return "दीपावली और दशहरा महा-ऑफर"
        return "Diwali & Festive Mega Offer"
    # 4. Christmas & New Year (December 20 - January 5)
    elif (month == 12 and day >= 20) or (month == 1 and day <= 5):
        if lang == 'hi':
            return "क्रिसमस और नए साल का स्पेशल ऑफर"
        return "Christmas & New Year Special Offer"
    # 5. Holi Season (March)
    elif month == 3:
        if lang == 'hi':
            return "होली महा-धमाका ऑफर"
        return "Holi Festive Mega Offer"

    return None


async def _send_random_cooldown_reminder(
    client,
    user_id: int,
    user_name: str,
    rem_sec: int,
    reminder_index: int = 1
):
    import random
    user_lang = await db.get_language(user_id)  # 'hi' or 'en' or None
    
    # Calculate friendly remaining time
    if rem_sec >= 3600:
        rh = rem_sec // 3600
        rm = (rem_sec % 3600) // 60
        cooldown_str = f"{rh:02d}h {rm:02d}m" if user_lang != 'hi' else f"{rh} घंटे {rm} मिनट"
    elif rem_sec >= 60:
        rm = rem_sec // 60
        rs = rem_sec % 60
        cooldown_str = f"{rm:02d}m {rs:02d}s" if user_lang != 'hi' else f"{rm} मिनट {rs} सेकंड"
    else:
        cooldown_str = f"{rem_sec}s" if user_lang != 'hi' else f"{rem_sec} सेकंड"

    # Select random single kawaii custom emoji
    def _re():
        emo, eid = random.choice(KAWAII_REMINDER_EMOJIS)
        return f'<emoji id="{eid}">{emo}</emoji>'

    # Determine language strictly: if 'hi' -> pure Hindi, if 'en' -> pure English, else random pure language
    if user_lang == 'hi':
        is_hi = True
    elif user_lang == 'en':
        is_hi = False
    else:
        is_hi = random.choice([True, False])

    fest_hi = get_current_festival(lang='hi')
    fest_en = get_current_festival(lang='en')

    if is_hi:
        templates = [
            # 1. Flirting & Romantic Tone (रोमांटिक / फ्लर्टी)
            (
                f"अरे <a href='tg://user?id={user_id}'>{user_name}</a> जी! {_re()}\n\n"
                f"आपका इंटरनेट धीमा है या फिर आप हमारे अनलिमिटेड पास के प्यार में पड़ रहे हैं? {_re()}\n\n"
                f"इस बोरिंग <b>{cooldown_str}</b> के कूलडाउन टाइमर को भूल जाइए और सिर्फ <b>₹15</b> में अनलिमिटेड सुपरफास्ट स्पीड का मज़ा लीजिए! {_re()}"
            ),
            # 2. Sarcastic & Witty (मजेदार / व्यंग्य)
            (
                f"सुनो <a href='tg://user?id={user_id}'>{user_name}</a>! {_re()}\n\n"
                f"क्या आप सच में अगले <b>{cooldown_str}</b> तक सिर्फ स्क्रीन को घूरने वाले हैं? {_re()}\n\n"
                f"इतना सब्र तो कोई नहीं करता! एक चाय के खर्चे (सिर्फ ₹15) में पूरे दिन का अनलिमिटेड पास मिल रहा है, फिर इंतज़ार कैसा? तुरंत पास अनलॉक करें! {_re()}"
            ),
            # 3. Professional & Direct Value (प्रोफेशनल व सटीक)
            (
                f"नमस्ते <a href='tg://user?id={user_id}'>{user_name}</a> {_re()}\n\n"
                f"आपकी फ्री डाउनलोड लिमिट पूरी हो चुकी है और अगला रीसेट <b>{cooldown_str}</b> बाद होगा।\n\n"
                f"बिना किसी लिमिट और बिना किसी डोनेशन मैसेज के 24/7 नॉन-स्टॉप एक्सेस के लिए अभी <b>पास सब्सक्रिप्शन</b> अनलॉक करें। {_re()}"
            ),
            # 4. Cute & Caring (केयरिंग व सपोर्टिव)
            (
                f"नमस्ते <a href='tg://user?id={user_id}'>{user_name}</a> {_re()}\n\n"
                f"हम नहीं चाहते कि आपको अपनी पसंदीदा फाइल्स के लिए घंटों इंतज़ार करना पड़े।\n\n"
                f"आपके लिए बेहद किफायती पास उपलब्ध है — इसे अभी एक्टिवेट करें और अनलिमिटेड फास्ट डाउनलोड्स का आनंद लें! {_re()}"
            ),
            # 5. Playful Curiosity (मजेदार उत्सुकता)
            (
                f"हेलो <a href='tg://user?id={user_id}'>{user_name}</a>! {_re()}\n\n"
                f"टाइमर अभी भी <b>{cooldown_str}</b> दिखा रहा है! क्या आप वाकई इतना लंबा इंतज़ार करेंगे? {_re()}\n\n"
                f"सिर्फ ₹15 में अनलिमिटेड पास लेकर अभी अपनी सारी पसंदीदा फाइल्स तुरंत डाउनलोड करें! {_re()}"
            ),
            # 6. Ultra-Affordable Snack Comparison (चाय / समोसा कम्पेरिजन)
            (
                f"सुनिए <a href='tg://user?id={user_id}'>{user_name}</a> जी {_re()}\n\n"
                f"जितने में एक चाय-समोसा आता है, उतने में आपको पूरे दिन का <b>अनलिमिटेड एक्सेस पास</b> मिल रहा है! {_re()}\n\n"
                f"फिर <b>{cooldown_str}</b> तक इंतज़ार करने की क्या ज़रूरत? नीचे क्लिक करें और तुरंत शुरू करें! {_re()}"
            )
        ]
        if fest_hi:
            templates.append(
                f"🎉 <b>{fest_hi}</b> {_re()}\n\n"
                f"नमस्ते <a href='tg://user?id={user_id}'>{user_name}</a>!\n\n"
                f"त्योहारों के इस खास मौके पर इंतज़ार कैसा? जब सिर्फ ₹15 में मिल रहा है <b>अनलिमिटेड एक्सेस पास</b>, तो <b>{cooldown_str}</b> तक कूलडाउन में क्यों रुकना? {_re()}\n\n"
                f"नीचे दिए गए बटन पर टैप करें और तुरंत सुपरफास्ट फाइल्स डाउनलोड करें! {_re()}"
            )
        btn_unlock = "अनलिमिटेड एक्सेस अनलॉक करें"
        btn_supp = "सहायता"
    else:
        templates = [
            # 1. Flirting & Cute
            (
                f"Hey <a href='tg://user?id={user_id}'>{user_name}</a> {_re()}\n\n"
                f"Is your network slow or are you just falling for our Unlimited Pass? {_re()}\n\n"
                f"Stop waiting on this boring <b>{cooldown_str}</b> cooldown timer! Treat yourself to unlimited high-speed downloads for just ₹15. You know you want it! {_re()}"
            ),
            # 2. Playful & Sarcastic
            (
                f"Yo <a href='tg://user?id={user_id}'>{user_name}</a>! {_re()}\n\n"
                f"Are you seriously waiting <b>{cooldown_str}</b> just to download another file? {_re()}\n\n"
                f"That timer is older than ancient history! Skip the whole wait for less than the price of a chai with an <b>Unlimited Pass</b> and enjoy instant uninterrupted downloads right now! {_re()}"
            ),
            # 3. Professional
            (
                f"Hello <a href='tg://user?id={user_id}'>{user_name}</a> {_re()}\n\n"
                f"Your download quota is currently paused for <b>{cooldown_str}</b> under our fair usage policy.\n\n"
                f"To bypass all cooldowns, eliminate donation messages, and unlock unlimited downloads 24/7, activate your <b>Unlimited Pass</b> now at subsidized pricing! {_re()}"
            ),
            # 4. Caring & Supportive
            (
                f"Dear <a href='tg://user?id={user_id}'>{user_name}</a> {_re()}\n\n"
                f"We hate seeing you wait on cooldown! You deserve smooth, instant downloads without any pause or interruptions.\n\n"
                f"Unlock unlimited high-speed access today with our affordable <b>Unlimited Pass</b> — zero limits, zero ads, pure speed! {_re()}"
            ),
            # 5. Playful Curiosity
            (
                f"Hey <a href='tg://user?id={user_id}'>{user_name}</a>! {_re()}\n\n"
                f"Your timer is still ticking down at <b>{cooldown_str}</b>! Why stare at the countdown? {_re()}\n\n"
                f"Grab an Unlimited Access Pass starting at just ₹15 and binge all your content with zero interruptions! {_re()}"
            ),
            # 6. Coffee / Snack Comparison
            (
                f"Listen up <a href='tg://user?id={user_id}'>{user_name}</a> {_re()}\n\n"
                f"For less than the price of a quick snack, you can get 24 hours of non-stop unlimited downloads! {_re()}\n\n"
                f"Why stay stuck on a <b>{cooldown_str}</b> timer? Tap below and get your Unlimited Pass now! {_re()}"
            )
        ]
        if fest_en:
            templates.append(
                f"🎉 <b>{fest_en}</b> {_re()}\n\n"
                f"Hey <a href='tg://user?id={user_id}'>{user_name}</a>!\\n\\n"
                f"Celebrate this festive season with zero limits and zero waiting! Why wait <b>{cooldown_str}</b> on cooldown when you can grab our festive <b>Unlimited Access Pass</b> at super cheap rates? {_re()}\n\n"
                f"Tap below and enjoy unlimited instant downloads right away! {_re()}"
            )
        btn_unlock = "Unlock Unlimited Access"
        btn_supp = "Support"

    text = random.choice(templates)
    
    api_kb = [
        [{"text": btn_unlock, "callback_data": "pass#unlock_menu", "icon_custom_emoji_id": "6030443364178992166"}],
        [{"text": btn_supp, "url": "https://t.me/AryaHelpTG", "icon_custom_emoji_id": "6030833407339008632"}]
    ]
    pyrogram_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔓 {btn_unlock}", callback_data="pass#unlock_menu")],
        [InlineKeyboardButton(f"🔒 {btn_supp}", url="https://t.me/AryaHelpTG")]
    ])

    try:
        sent_ok = await send_or_edit_with_custom_icons(
            client=client,
            chat_id=user_id,
            text=text,
            inline_keyboard=api_kb
        )
        if not sent_ok:
            await client.send_message(user_id, text, reply_markup=pyrogram_kb, disable_web_page_preview=True)
    except Exception as ex:
        logger.debug(f"Failed to send rate limit reminder to {user_id}: {ex}")


async def schedule_rate_limit_reminders(
    client,
    user_id: int,
    user_name: str,
    rem_sec: int,
    window_str: str = ""
):
    """
    Schedules max 2 witty / sarcastic / love / professional reminders during the user's cooldown.
    Spaced out intelligently across hours (e.g. 3-4 hours apart) instead of instantly.
    Automatically aborts if user activates an Unlimited Pass before or during the interval.
    """
    _cancel_cooldown_reminders(user_id)

    async def _reminder_coro():
        try:
            # If cooldown is under 10 minutes, do not send any reminder
            if rem_sec < 600:
                return

            # Calculate sensible spaced-out delays based on cooldown duration
            if rem_sec >= 14400:  # 4 hours or more (e.g. 6h, 12h, 24h)
                delay_1 = int(rem_sec * 0.30)  # e.g., ~3.6 hours for 12h
                delay_2 = int(rem_sec * 0.70)  # e.g., ~8.4 hours for 12h
            elif rem_sec >= 3600:  # 1 to 4 hours
                delay_1 = int(rem_sec * 0.35)  # e.g., ~42 mins for 2h
                delay_2 = int(rem_sec * 0.75)  # e.g., ~1.5 hours for 2h
            else:  # 10 mins to 1 hour
                delay_1 = int(rem_sec * 0.40)
                delay_2 = int(rem_sec * 0.80)

            # --- REMINDER 1 ---
            await asyncio.sleep(delay_1)

            # Check if user already activated pass
            pass_info = await db.get_user_unlimited_pass(user_id)
            if pass_info.get('active'):
                return

            await _send_random_cooldown_reminder(
                client=client,
                user_id=user_id,
                user_name=user_name,
                rem_sec=max(1, rem_sec - delay_1),
                reminder_index=1
            )

            # --- REMINDER 2 ---
            remaining_sleep = delay_2 - delay_1
            if remaining_sleep > 300 and (rem_sec - delay_2) > 60:
                await asyncio.sleep(remaining_sleep)

                pass_info = await db.get_user_unlimited_pass(user_id)
                if pass_info.get('active'):
                    return

                await _send_random_cooldown_reminder(
                    client=client,
                    user_id=user_id,
                    user_name=user_name,
                    rem_sec=max(1, rem_sec - delay_2),
                    reminder_index=2
                )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Cooldown reminder error for user {user_id}: {e}")
        finally:
            _active_cooldown_reminders.pop(user_id, None)

    task = asyncio.create_task(_reminder_coro())
    _active_cooldown_reminders[user_id] = task

# ── Unlimited Delivery Pass Callback Handlers ─────────────────────────────────
_pending_utr_users: dict = {}  # {user_id: {'dur_key': str, 'amount': float, 'ts': float}}



async def _poll_upi_payment(
    client,
    user_id: int,
    order_id: str,
    dyn_amount: float,
    dur_key: str,
    count: str,
    unit: str,
    message_id: int,
    chat_id: int
):
    """
    Background polling task that checks Gmail IMAP every 5 seconds for up to 300 seconds.
    Automatically activates the pass upon detecting the credit email without user intervention.
    """
    from plugins.gmail_helper import find_upi_payment_by_amount
    from database import parse_duration_to_seconds

    start_time = time.time()
    dur_sec = parse_duration_to_seconds(dur_key, default_unit='d')

    for _ in range(60):  # 60 * 5s = 300s (5 minutes)
        await asyncio.sleep(5)

        # Check if pass is already active or order already marked PAID
        try:
            pass_info = await db.get_user_unlimited_pass(user_id)
            if pass_info.get('active'):
                _active_upi_amounts.pop(dyn_amount, None)
                _pending_utr_users.pop(user_id, None)
                return

            order_doc = await db.pass_orders.find_one({'order_id': order_id})
            if order_doc and order_doc.get('status') == 'PAID':
                _active_upi_amounts.pop(dyn_amount, None)
                _pending_utr_users.pop(user_id, None)
                return
        except Exception:
            pass

        # Check Gmail IMAP for payment matching this unique dynamic amount
        try:
            res = await find_upi_payment_by_amount(
                expected_amount=dyn_amount,
                order_time=start_time,
                window_seconds=360,
                user_id=user_id,
                order_id=order_id
            )
            if res.get('success'):
                extracted_utr = res.get('utr') or f"AUTO-{int(time.time())}"
                payer_name = res.get('payer_name') or "UPI Payer"

                # Check if claimed by another user
                is_claimed_by_other = await db.is_utr_claimed_by_other(extracted_utr, user_id=user_id, order_id=order_id)
                if is_claimed_by_other:
                    logger.warning(f"Auto-verified UTR {extracted_utr} already claimed by another user. Continuing poll...")
                    continue

                # Look up Telegram user name from order_doc or Telegram user database
                order_doc_paid = await db.pass_orders.find_one({'order_id': order_id})
                tg_user_name = "User"
                if order_doc_paid and order_doc_paid.get('user_name'):
                    tg_user_name = order_doc_paid.get('user_name')
                else:
                    try:
                        udoc = await db.col.find_one({'id': int(user_id)}, {'name': 1})
                        if udoc and udoc.get('name'):
                            tg_user_name = udoc.get('name')
                    except Exception:
                        pass

                # Record used UTR with Telegram user name
                await db.record_used_utr(
                    utr=extracted_utr,
                    user_id=user_id,
                    amount=dyn_amount,
                    order_id=order_id,
                    user_name=tg_user_name,
                    gateway="Pay Via UPI (INR)"
                )

                # Activate user pass in database with Telegram user name
                _cancel_cooldown_reminders(user_id)
                await db.activate_user_unlimited_pass(
                    user_id=user_id,
                    duration_seconds=dur_sec,
                    order_id=order_id,
                    amount=dyn_amount,
                    user_name=tg_user_name,
                    gateway=f"Pay Via UPI (INR) [Auto Verified {extracted_utr}]"
                )

                # Mark pass order as PAID with bank_payer_name kept separate
                await db.pass_orders.update_one(
                    {'order_id': order_id},
                    {'$set': {'status': 'PAID', 'paid_at': time.time(), 'utr': extracted_utr, 'bank_payer_name': payer_name}},
                    upsert=True
                )

                # Cleanup in-memory tracking
                _active_upi_amounts.pop(dyn_amount, None)
                _pending_utr_users.pop(user_id, None)
                _active_order_reminders.pop(f"{user_id}_{order_id}", None)

                # User confirmation message
                user_lang = await db.get_language(user_id)
                is_hi = bool(user_lang == 'hi')
                if is_hi:
                    success_text = (
                        f'<emoji id="6267118537752450044">🟢</emoji> <b>पेमेंट ऑटोमैटिकली वेरीफाई हो गया!</b>\n\n'
                        f"• <b>ऑर्डर आईडी:</b> <code>{order_id}</code>\n"
                        f"• <b>प्लान:</b> {count} {unit} अनलिमिटेड एक्सेस पास\n"
                        f"• <b>भुगतान राशि:</b> <code>₹{dyn_amount:.2f}</code>\n"
                        f"• <b>UTR / Ref:</b> <code>{extracted_utr}</code>\n"
                        f"• <b>स्थिति:</b> ✅ <b>सक्रिय और तैयार</b>\n\n"
                        f'<blockquote><emoji id="5850176641803753392">🎉</emoji> ᴛʜᴀɴᴋ ʏᴏᴜ! ʏᴏᴜʀ ᴜɴʟɪᴍɪᴛᴇᴅ ᴀᴄᴄᴇꜱꜱ ᴘᴀꜱꜱ ʜᴀꜱ ʙᴇᴇɴ ᴀᴄᴛɪᴠᴀᴛᴇᴅ. ᴇɴᴊᴏʏ ᴜɴʟɪᴍɪᴛᴇᴅ ɪɴꜱᴛᴀɴᴛ ᴅᴏᴡɴʟᴏᴀᴅꜱ ᴡɪᴛʜ ᴢᴇʀᴏ ʟɪᴍɪᴛꜱ!</blockquote>'
                    )
                    lbl_txns = "मेरे ट्रांसक्शन्स"
                    lbl_supp = "सहायता"
                else:
                    success_text = (
                        f'<emoji id="6267118537752450044">🟢</emoji> <b>Payment Automatically Verified!</b>\n\n'
                        f"• <b>Order ID:</b> <code>{order_id}</code>\n"
                        f"• <b>Plan:</b> {count} {unit} Unlimited Access Pass\n"
                        f"• <b>Amount Paid:</b> <code>₹{dyn_amount:.2f}</code>\n"
                        f"• <b>UTR / Ref:</b> <code>{extracted_utr}</code>\n"
                        f"• <b>Status:</b> ✅ <b>Active & Ready</b>\n\n"
                        f'<blockquote><emoji id="5850176641803753392">🎉</emoji> ᴛʜᴀɴᴋ ʏᴏᴜ! ʏᴏᴜʀ ᴜɴʟɪᴍɪᴛᴇᴅ ᴀᴄᴄᴇꜱꜱ ᴘᴀꜱꜱ ʜᴀꜱ ʙᴇᴇɴ ᴀᴄᴛɪᴠᴀᴛᴇᴅ. ᴇɴᴊᴏʏ ᴜɴʟɪᴍɪᴛᴇᴅ ɪɴꜱᴛᴀɴᴛ ᴅᴏᴡɴʟᴏᴀᴅꜱ ᴡɪᴛʜ ᴢᴇʀᴏ ʟɪᴍɪᴛꜱ!</blockquote>'
                    )
                    lbl_txns = "My Transactions"
                    lbl_supp = "Support"

                success_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"📜 {lbl_txns}", callback_data="pass#my_transactions")],
                    [InlineKeyboardButton(f"🔒 {lbl_supp}", url="https://t.me/AryaHelpTG")]
                ])
                success_api_kb = [
                    [{"text": lbl_txns, "callback_data": "pass#my_transactions", "icon_custom_emoji_id": "6035297458907519073"}],
                    [{"text": lbl_supp, "url": "https://t.me/AryaHelpTG", "icon_custom_emoji_id": "6030833407339008632"}]
                ]

                ui_updated = False
                if message_id:
                    try:
                        sent = await send_or_edit_with_custom_icons(
                            client=client,
                            chat_id=chat_id,
                            text=success_text,
                            inline_keyboard=success_api_kb,
                            message_id=message_id,
                            is_media_edit=True
                        )
                        if sent:
                            ui_updated = True
                    except Exception as ex:
                        logger.debug(f"HTTP edit error: {ex}")

                    if not ui_updated:
                        try:
                            await client.edit_message_caption(
                                chat_id=chat_id,
                                message_id=message_id,
                                caption=success_text,
                                reply_markup=success_kb
                            )
                            ui_updated = True
                        except Exception as ex:
                            logger.debug(f"Pyrogram caption edit error: {ex}")

                if not ui_updated:
                    try:
                        await client.send_message(chat_id=chat_id, text=success_text, reply_markup=success_kb)
                    except Exception as ex:
                        logger.error(f"Send success message error: {ex}")

                # Send log to configured payment log channel
                try:
                    rl_cfg = await db.get_delivery_rate_limit_config()
                    log_ch = rl_cfg.get('log_channel')
                    from plugins.arya_logger import log_pass_purchased
                    asyncio.create_task(log_pass_purchased(
                        user_id=user_id,
                        user_name=payer_name,
                        duration_str=f"{count} {unit}".title(),
                        amount=dyn_amount,
                        order_id=order_id,
                        expiry_ts=time.time() + dur_sec,
                        log_channel=log_ch,
                        gateway=f"Pay Via UPI (INR) [Auto Verified {extracted_utr}]"
                    ))
                except Exception as l_err:
                    logger.debug(f"Payment log dispatch error: {l_err}")

                # Notify bot owners
                from config import Config
                admin_id = Config.BOT_OWNER_ID[0] if Config.BOT_OWNER_ID else None
                if admin_id:
                    try:
                        await client.send_message(
                            chat_id=admin_id,
                            text=(
                                f"🔔 <b>[UPI Auto-Payment Success]</b>\n\n"
                                f"• <b>Customer:</b> <code>{user_id}</code>\n"
                                f"• <b>Order:</b> <code>{order_id}</code>\n"
                                f"• <b>Amount:</b> ₹{dyn_amount:.2f}\n"
                                f"• <b>Plan:</b> {dur_key}\n"
                                f"• <b>UTR:</b> <code>{extracted_utr}</code>\n"
                                f"• <b>Payer:</b> {payer_name}"
                            )
                        )
                    except Exception:
                        pass
                return
        except Exception as e:
            logger.debug(f"Poll UPI payment error: {e}")

    # Expire reservation if unpaid after 300s
    _active_upi_amounts.pop(dyn_amount, None)
    _pending_utr_users.pop(user_id, None)

async def _handle_share_bot_utr_message(client, message):
    if not message.from_user or not message.text:
        return
    user_id = message.from_user.id
    if user_id not in _pending_utr_users:
        return

    session_data = _pending_utr_users[user_id]
    
    # Auto-expire session if 5 minutes (300 seconds) have passed
    if time.time() - session_data.get('ts', 0) > 300:
        _pending_utr_users.pop(user_id, None)
        return

    dur_key = session_data['dur_key']
    expected_amount = session_data['amount']

    raw_text = (message.text or "").strip()
    if raw_text.lower() in ("cancel", "/cancel", "back", "/back"):
        _pending_utr_users.pop(user_id, None)
        await message.reply_text("<i>Payment order cancelled.</i>", quote=True)
        return

    clean_digits = raw_text.replace(" ", "").replace("-", "").strip()
    
    # If message contains non-numeric text (like English words, queries), DO NOT intercept!
    # Only process if user is actually typing numbers
    if not clean_digits.isdigit():
        return

    # Strictly accept only 12-digit reference number:
    if len(clean_digits) != 12:
        await message.reply_text(
            "⚠️ <b>Invalid Reference Number</b>\n\n"
            "Please send only the <b>12-digit payment reference / UTR number</b>.\n"
            "Example: <code>423456789012</code>\n\n"
            "<i>Note: Only 12-digit numbers are accepted. Type 'cancel' to abort.</i>",
            quote=True
        )
        return

    utr = clean_digits

    if await db.is_utr_used(utr):
        await message.reply_text(
            "❌ <b>Reference Number Already Used</b>\n\n"
            f"The reference number <code>{utr}</code> has already been claimed for another pass.\n"
            "Each payment transaction can only be redeemed once.",
            quote=True
        )
        return

    sts = await message.reply_text(
        f"🔄 <i>Verifying Payment Reference <code>{utr}</code> via Automated Gmail IMAP... Please wait.</i>",
        quote=True
    )

    from plugins.gmail_helper import verify_upi_payment_via_gmail
    res = await verify_upi_payment_via_gmail(utr, expected_amount)

    if res.get("success"):
        pending_info = _pending_utr_users.pop(user_id, {})
        order_id = pending_info.get('order_id') or f"PASS-{user_id}-1D-1"
        await db.mark_utr_used(utr, user_id, expected_amount, dur_key, user_name=u_name, order_id=order_id)
        _cancel_cooldown_reminders(user_id)
        b_id = getattr(getattr(client, "me", None), "id", None)
        b_uname = getattr(getattr(client, "me", None), "username", "")
        new_expiry = await db.grant_user_unlimited_pass(user_id, dur_key, user_name=u_name, bot_id=b_id, bot_username=b_uname, plan_key=dur_key, amount=expected_amount)

        from database import format_duration_verbose, parse_duration_to_seconds
        dur_sec = parse_duration_to_seconds(dur_key, default_unit='d')
        dur_verbose = format_duration_verbose(dur_sec)

        import datetime
        try:
            import pytz
            ist_tz = pytz.timezone('Asia/Kolkata')
            exp_dt = datetime.datetime.fromtimestamp(new_expiry, tz=ist_tz)
            exp_str = exp_dt.strftime('%d-%m-%Y %I:%M %p')
        except Exception:
            exp_str = datetime.datetime.fromtimestamp(new_expiry).strftime('%d-%m-%Y %I:%M %p')

        u_name = message.from_user.first_name or "User"
        success_text = (
            f'<emoji id="5224607267797606837">🎉</emoji> <b>UPI Payment Verified Successfully!</b>\n\n'
            f"Hey <b>{u_name}</b>, your <b>{dur_verbose.title()} Unlimited Access Pass</b> is now ACTIVE!\n\n"
            f"• <b>UTR / Ref No:</b> <code>{utr}</code>\n"
            f"• <b>Amount Verified:</b> ₹{expected_amount:.2f}\n"
            f"• <b>Valid Until:</b> <code>{exp_str}</code>\n"
            f'• <b>Status:</b> <emoji id="5411359377904934337">🟢</emoji> Unlimited Access (No Cooldown)\n\n'
            f"You can now access any batch and story links without cooldown. Enjoy!"
        )
        await sts.edit(success_text)

        rl_cfg = await db.get_delivery_rate_limit_config()
        log_ch = rl_cfg.get('log_channel')
        from plugins.arya_logger import log_pass_purchased
        asyncio.create_task(log_pass_purchased(
            user_id=user_id,
            user_name=u_name,
            duration_str=dur_verbose.title(),
            amount=expected_amount,
            order_id=order_id,
            expiry_ts=new_expiry,
            log_channel=log_ch,
            gateway="UPI (Gmail Auto)"
        ))
    elif res.get("amount_mismatch"):
        m_amt = res.get("mismatched_amount")
        await sts.edit(
            f"⚠️ <b>Payment Amount Mismatch</b>\n\n"
            f"We found the transaction for reference <code>{utr}</code>, but the received amount is "
            f"<b>₹{m_amt:.2f}</b> while the expected plan price is <b>₹{expected_amount:.2f}</b>.\n\n"
            f"Please pay the exact plan amount to activate your pass, or contact support."
        )
    else:
        retry_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Re-Verify Reference", callback_data=f"pass#upirecheck_{utr}_{dur_key}_{expected_amount}")],
            [InlineKeyboardButton("← Back", callback_data="pass#unlock_menu")]
        ])
        err_msg = res.get("error", "Payment reference number not found in bank email notifications yet.")
        await sts.edit(
            f"⏳ <b>Payment Not Detected Yet</b>\n\n"
            f"• <b>Reference / UTR:</b> <code>{utr}</code>\n"
            f"• <b>Expected Amount:</b> <code>₹{expected_amount:.2f}</code>\n\n"
            f"<i>{err_msg}</i>\n\n"
            f"<b>Tip:</b> Bank emails can take 10 to 30 seconds to arrive. "
            f"Please wait a few seconds and tap <b>'Re-Verify Reference'</b> below!",
            reply_markup=retry_kb
        )


@Client.on_message(filters.private & filters.text & ~filters.command(["start", "help", "about", "support", "updates", "broadcast", "premium", "norestrictions"]), group=10)
async def _main_bot_utr_interceptor(client, message):
    await _handle_share_bot_utr_message(client, message)


def generate_upi_qr_bytes(upi_id: str, amount: float, payee_name: str = "Merchant", order_id: str = ""):
    """
    Generates a high-quality QR code image buffer for UPI payment.
    Falls back to HTTP QR service if local qrcode library encounters any issue.
    """
    import io
    import urllib.parse
    pn_clean = urllib.parse.quote_plus(payee_name or "Merchant")
    tn_clean = urllib.parse.quote_plus(order_id or "Delivery Pass")
    upi_payload = f"upi://pay?pa={upi_id}&pn={pn_clean}&am={amount:.2f}&cu=INR&tn={tn_clean}"

    try:
        import qrcode
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=2
        )
        qr.add_data(upi_payload)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        buf = io.BytesIO()
        buf.name = "upi_qr.png"
        img.save(buf, "PNG")
        buf.seek(0)
        return buf
    except Exception as e:
        logger.warning(f"Local QR generation fallback: {e}")
        import requests
        encoded = urllib.parse.quote(upi_payload)
        resp = requests.get(f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&data={encoded}", timeout=10)
        buf = io.BytesIO(resp.content)
        buf.name = "upi_qr.png"
        buf.seek(0)
        return buf


_active_upi_amounts: dict[float, float] = {}  # {amount: expiry_ts}

async def generate_unique_dynamic_upi_amount(base_amount: float) -> float:
    """
    Generates a guaranteed unique dynamic amount in [base_amount - 0.49, base_amount + 0.50]
    with 2 decimal places (e.g. 14.51, 15.49, 15.23 for base 15.0).
    Strictly ensures no two active/pending orders share the same dynamic amount concurrently.
    """
    import random
    now = time.time()
    
    # Cleanup expired active reservations
    expired = [amt for amt, exp in _active_upi_amounts.items() if exp < now]
    for amt in expired:
        _active_upi_amounts.pop(amt, None)

    # Check database for active pending UPI orders created in last 6 minutes
    recent_cutoff = now - 360
    taken_amounts = set(_active_upi_amounts.keys())
    try:
        db_active = await db.pass_orders.find({
            'status': 'PENDING',
            'gateway': 'Pay Via UPI (INR)',
            'created_at': {'$gte': recent_cutoff}
        }).to_list(100)
        for o in db_active:
            if 'amount' in o:
                taken_amounts.add(round(float(o['amount']), 2))
    except Exception:
        pass

    all_offsets = [p for p in range(-49, 51) if p != 0]
    random.shuffle(all_offsets)

    chosen_amount = None
    for p in all_offsets:
        candidate = round(float(base_amount) + (p / 100.0), 2)
        if candidate > 0 and candidate not in taken_amounts:
            chosen_amount = candidate
            break

    if chosen_amount is None:
        chosen_amount = round(float(base_amount) + (random.choice(all_offsets) / 100.0), 2)

    _active_upi_amounts[chosen_amount] = now + 360  # Reserve for 6 mins
    return chosen_amount

def generate_dynamic_upi_amount(base_amount: float) -> float:
    """Synchronous fallback helper."""
    import random
    all_offsets = [p for p in range(-49, 51) if p != 0]
    return max(1.0, round(float(base_amount) + (random.choice(all_offsets) / 100.0), 2))


def format_plan_name_friendly(dur_key: str, lang: str = 'en') -> str:
    """Helper to return friendly plan string like '7 Days' or '7 दिन'."""
    dur_str = str(dur_key).lower().strip()
    if lang == 'hi':
        if dur_str in ('7d', '7days', '7day', '7'):
            return "7 दिन"
        elif dur_str.endswith('mo'):
            n = dur_str[:-2]
            return f"{n} {'महीना' if n == '1' else 'महीने'}"
        elif dur_str.endswith('month') or dur_str.endswith('months'):
            n = dur_str.replace('months', '').replace('month', '').strip()
            return f"{n} {'महीना' if n == '1' else 'महीने'}"
        elif dur_str.endswith('d'):
            n = dur_str[:-1]
            return f"{n} {'दिन' if n == '1' else 'दिन'}"
        elif dur_str.endswith('h'):
            n = dur_str[:-1]
            return f"{n} घंटे"
        elif dur_str.endswith('m'):
            n = dur_str[:-1]
            return f"{n} मिनट"
        elif dur_str.endswith('y') or dur_str.endswith('yr') or dur_str == '365d':
            return "1 साल"
        return dur_str
    else:
        if dur_str in ('7d', '7days', '7day', '7'):
            return "7 Days"
        elif dur_str.endswith('mo'):
            n = dur_str[:-2]
            return f"{n} {'Month' if n == '1' else 'Months'}"
        elif dur_str.endswith('month') or dur_str.endswith('months'):
            n = dur_str.replace('months', '').replace('month', '').strip()
            return f"{n} {'Month' if n == '1' else 'Months'}"
        elif dur_str.endswith('d'):
            n = dur_str[:-1]
            return f"{n} {'Day' if n == '1' else 'Days'}"
        elif dur_str.endswith('h'):
            n = dur_str[:-1]
            return f"{n} Hours"
        elif dur_str.endswith('m'):
            n = dur_str[:-1]
            return f"{n} Minutes"
        elif dur_str.endswith('y') or dur_str.endswith('yr') or dur_str == '365d':
            return "1 Year"
        return dur_str.title()


def format_plan_button_label(dur_key: str, price, lang: str = 'en') -> str:
    """Returns clean label: '1 Day - ₹15' or '1 दिन - ₹15'."""
    dur_str = str(dur_key).lower().strip()
    p_val = int(price) if float(price).is_integer() else price
    if lang == 'hi':
        if dur_str in ('7d', '7days', '7day', '7'):
            return f"7 दिन - ₹{p_val}"
        elif dur_str.endswith('mo'):
            num = dur_str[:-2]
            return f"{num} {'महीना' if num == '1' else 'महीने'} - ₹{p_val}"
        elif dur_str.endswith('month') or dur_str.endswith('months'):
            num = dur_str.replace('months', '').replace('month', '').strip()
            return f"{num} {'महीना' if num == '1' else 'महीने'} - ₹{p_val}"
        elif dur_str.endswith('d'):
            num = dur_str[:-1]
            return f"{num} {'दिन' if num == '1' else 'दिन'} - ₹{p_val}"
        elif dur_str.endswith('h'):
            num = dur_str[:-1]
            return f"{num} घंटे - ₹{p_val}"
        elif dur_str.endswith('m'):
            num = dur_str[:-1]
            return f"{num} मिनट - ₹{p_val}"
        elif dur_str.endswith('y') or dur_str.endswith('yr') or dur_str == '365d':
            return f"1 साल - ₹{p_val}"
        else:
            return f"{dur_str} - ₹{p_val}"
    else:
        if dur_str in ('7d', '7days', '7day', '7'):
            return f"7 Days - ₹{p_val}"
        elif dur_str.endswith('mo'):
            num = dur_str[:-2]
            return f"{num} {'Month' if num == '1' else 'Months'} - ₹{p_val}"
        elif dur_str.endswith('month') or dur_str.endswith('months'):
            num = dur_str.replace('months', '').replace('month', '').strip()
            return f"{num} {'Month' if num == '1' else 'Months'} - ₹{p_val}"
        elif dur_str.endswith('d'):
            num = dur_str[:-1]
            return f"{num} {'Day' if num == '1' else 'Days'} - ₹{p_val}"
        elif dur_str.endswith('h'):
            num = dur_str[:-1]
            return f"{num} Hours - ₹{p_val}"
        elif dur_str.endswith('m'):
            num = dur_str[:-1]
            return f"{num} Minutes - ₹{p_val}"
        else:
            return f"{dur_str.title()} - ₹{p_val}"


PLAN_CUSTOM_EMOJIS = [
    ("5219943216781995020", "🔹"),  # 1st Plan
    ("6021577980449396555", "🔸"),  # 2nd Plan
    ("5415825426633202840", "⚡"),  # 3rd Plan (7 Days)
    ("6269048584386122161", "💎"),  # 4th Plan
    ("6156730271858169904", "👑"),  # 5th Plan
]

def calculate_plan_savings(dur_key: str, price: float, prices: dict) -> str:
    """Calculate automatic savings amount relative to base daily rate."""
    from database import parse_duration_to_seconds
    try:
        dur_sec = parse_duration_to_seconds(dur_key, default_unit='d')
        dur_days = dur_sec / 86400.0

        # Find base daily rate from shortest plan >= 1 day
        day_rates = []
        for k, p in prices.items():
            s = parse_duration_to_seconds(k, default_unit='d')
            d = s / 86400.0
            if d >= 1.0:
                day_rates.append((d, float(p) / d, float(p)))

        if not day_rates:
            return ""

        day_rates.sort(key=lambda x: x[0])
        base_rate_per_day = day_rates[0][1]

        if dur_days > 1.0 and base_rate_per_day > 0:
            standard_cost = dur_days * base_rate_per_day
            if standard_cost > price:
                save_amount = int(round(standard_cost - price))
                if save_amount > 0:
                    return f" (Save ₹{save_amount})"
    except Exception:
        pass
    return ""



# Active payment reminders cache
_active_order_reminders = {}

async def schedule_pass_payment_reminder(
    client,
    user_id: int,
    order_id: str,
    gateway_name: str,
    amount_str: str,
    dur_verbose: str,
    pay_url: str = None,
    dur_key: str = "1d",
    dyn_amount: float = 15.0
):
    """
    Schedules max 2 payment completion reminders under 10 minutes (at 3 mins and 7 mins).
    After 10 minutes, the order reminder task expires and ceases completely.
    Aborts immediately if user completes payment, verifies, cancels, or buys a pass.
    """
    task_key = f"{user_id}_{order_id}"
    _active_order_reminders[task_key] = time.time()

    async def _send_single_rem(rem_idx: int):
        user_lang = await db.get_language(user_id)
        is_hi = bool(user_lang == 'hi')
        u_name = "there"
        try:
            chat_member = await client.get_chat(user_id)
            if chat_member and chat_member.first_name:
                u_name = chat_member.first_name
        except Exception:
            pass

        if is_hi:
            plan_name = format_plan_name_friendly(dur_key, lang='hi')
            if rem_idx == 1:
                header = '<emoji id="6034898821517940846">⏰</emoji> <b>पेमेंट रिमाइंडर (1/2) — अपना ऑर्डर पूरा करें</b>'
                note = "आपका पेमेंट इनवॉइस पेंडिंग है। कृपया समय रहते पेमेंट पूरा करें।"
            else:
                header = '<emoji id="6034898821517940846">⏳</emoji> <b>अंतिम पेमेंट रिमाइंडर (2/2) — इनवॉइस समाप्त होने वाला है</b>'
                note = "आपका पेमेंट इनवॉइस अगले 3 मिनट में समाप्त (Expire) हो जाएगा।"

            rem_text = (
                f"{header}\n\n"
                f"नमस्ते <b>{u_name}</b>, आपका <b>{plan_name} अनलिमिटेड एक्सेस पास</b> पेमेंट की प्रतीक्षा कर रहा है!\n\n"
                f"• <b>ऑर्डर ID:</b> <code>{order_id}</code>\n"
                f"• <b>बकाया राशि:</b> <code>{amount_str}</code>\n"
                f"• <b>पेमेंट मेथड:</b> {gateway_name}\n\n"
                f"<blockquote><emoji id=\"5773677501825945508\">⚡️</emoji> {note} बिना किसी रुकावट व लिमिट के अनलिमिटेड डाउनलोड्स का आनंद लें!</blockquote>\n\n"
                f"यदि आप पहले ही भुगतान कर चुके हैं या कोई सहायता चाहिए, तो कृपया सपोर्ट से संपर्क करें।"
            )
            btn_pay_text = "Pay Now"
            btn_status_text = "पेमेंट स्टेटस चेक करें"
            btn_supp_text = "सहायता"
        else:
            plan_name = format_plan_name_friendly(dur_key, lang='en')
            if rem_idx == 1:
                header = '<emoji id="6034898821517940846">⏰</emoji> <b>Payment Reminder (1/2) — Complete Your Order</b>'
                note = "Your payment invoice is pending. Please complete the payment to activate."
            else:
                header = '<emoji id="6034898821517940846">⏳</emoji> <b>Final Reminder (2/2) — Invoice Expiring Soon</b>'
                note = "Your payment invoice will expire in 3 minutes."

            rem_text = (
                f"{header}\n\n"
                f"Hey <b>{u_name}</b>, your <b>{plan_name} Unlimited Access Pass</b> order is waiting for payment!\n\n"
                f"• <b>Order ID:</b> <code>{order_id}</code>\n"
                f"• <b>Amount Due:</b> <code>{amount_str}</code>\n"
                f"• <b>Payment Method:</b> {gateway_name}\n\n"
                f'<blockquote><emoji id=\"5773677501825945508\">⚡️</emoji> {note} Activate your pass now to enjoy uninterrupted downloads with zero limits!</blockquote>\n\n'
                f"If you have already paid or need assistance, please feel free to contact our support team."
            )
            btn_pay_text = "Pay Now"
            btn_status_text = "Check Payment Status"
            btn_supp_text = "Support"

        rem_buttons = []
        rem_api_buttons = []
        if pay_url:
            rem_buttons.append([InlineKeyboardButton(btn_pay_text, url=pay_url)])
            rem_api_buttons.append([{"text": btn_pay_text, "url": pay_url, "icon_custom_emoji_id": "5807527002374151568"}])
        elif "UPI" in gateway_name:
            rem_buttons.append([InlineKeyboardButton(btn_status_text, callback_data=f"pass#upistatus_{order_id}_{dur_key}_{dyn_amount}")])
            rem_api_buttons.append([{"text": btn_status_text, "callback_data": f"pass#upistatus_{order_id}_{dur_key}_{dyn_amount}", "icon_custom_emoji_id": "5807492110059838726"}])

        rem_buttons.append([InlineKeyboardButton(btn_supp_text, url="https://t.me/AryaHelpTG")])
        rem_api_buttons.append([{"text": btn_supp_text, "url": "https://t.me/AryaHelpTG", "icon_custom_emoji_id": "6030833407339008632"}])

        sent_ok = await send_or_edit_with_custom_icons(
            client=client,
            chat_id=user_id,
            text=rem_text,
            inline_keyboard=rem_api_buttons
        )
        if not sent_ok:
            await client.send_message(
                chat_id=user_id,
                text=rem_text,
                reply_markup=InlineKeyboardMarkup(rem_buttons)
            )

    async def _reminder_coro():
        try:
            # --- REMINDER 1: At 3 minutes (180 seconds) ---
            await asyncio.sleep(180)
            if task_key not in _active_order_reminders:
                return

            pass_info = await db.get_user_unlimited_pass(user_id)
            if pass_info.get('active'):
                return

            order_doc = await db.pass_orders.find_one({'order_id': order_id})
            if order_doc and order_doc.get('status') in ('PAID', 'CANCELLED', 'EXPIRED'):
                return

            utr_doc = await db.used_utrs.find_one({'order_id': order_id})
            if utr_doc:
                return

            await _send_single_rem(1)

            # --- REMINDER 2: At 7 minutes (240 seconds more = 420s total, under 10 mins) ---
            await asyncio.sleep(240)
            if task_key not in _active_order_reminders:
                return

            pass_info = await db.get_user_unlimited_pass(user_id)
            if pass_info.get('active'):
                return

            order_doc = await db.pass_orders.find_one({'order_id': order_id})
            if order_doc and order_doc.get('status') in ('PAID', 'CANCELLED', 'EXPIRED'):
                return

            utr_doc = await db.used_utrs.find_one({'order_id': order_id})
            if utr_doc:
                return

            await _send_single_rem(2)

            # --- EXPIRE AT 10 MINUTES (180 seconds more = 600s total) ---
            await asyncio.sleep(180)
            await db.pass_orders.update_one(
                {'order_id': order_id, 'status': 'PENDING'},
                {'$set': {'status': 'EXPIRED'}}
            )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Payment reminder error for order {order_id}: {e}")
        finally:
            _active_order_reminders.pop(task_key, None)

    asyncio.create_task(_reminder_coro())


async def start_pass_cashfree_auto_verifier(
    client,
    user_id: int,
    user_name: str,
    order_id: str,
    dur_key: str,
    amount: float,
    dur_verbose: str,
    invoice_msg_id: int = None
):
    """
    Background automated real-time verifier for Cashfree Pass Orders.
    Checks Cashfree API every 5 seconds for up to 10 minutes.
    As soon as the payment is completed, it automatically:
      1. Claims order atomically via database (strictly preventing double credit).
      2. Activates the unlimited pass.
      3. Edits the invoice message in-place to the success celebration screen.
      4. Cancels reminders and logs transaction to log channel.
    """
    task_key = f"{user_id}_{order_id}"
    logger.info(f"[PASS-AUTO-VERIFY] Started real-time auto-verifier for user {user_id}, order {order_id}")
    for _ in range(120): # 120 * 5s = 600s (10 mins)
        await asyncio.sleep(5)
        try:
            if task_key not in _active_order_reminders and _active_order_reminders.get(f"done_{task_key}"):
                break
            
            order_doc = await db.get_pass_order(order_id)
            if not order_doc:
                break
            if order_doc.get("status") == "PAID":
                logger.info(f"[PASS-AUTO-VERIFY] Order {order_id} already marked PAID. Exiting auto-verifier.")
                break
            if order_doc.get("status") in ("CANCELLED", "EXPIRED"):
                logger.info(f"[PASS-AUTO-VERIFY] Order {order_id} is {order_doc.get('status')}. Stopping auto-verifier.")
                break

            from plugins.cashfree_helper import verify_cashfree_pass_order
            v_res = await verify_cashfree_pass_order(order_id)
            if v_res.get("is_paid"):
                claimed = await db.mark_pass_order_paid_atomic(order_id, v_res)
                if claimed:
                    _cancel_cooldown_reminders(user_id)
                    _active_order_reminders[f"done_{task_key}"] = True
                    b_id = getattr(getattr(client, "me", None), "id", None)
                    b_uname = getattr(getattr(client, "me", None), "username", "")
                    new_expiry = await db.grant_user_unlimited_pass(user_id, dur_key, user_name=user_name, bot_id=b_id, bot_username=b_uname, plan_key=dur_key, amount=amount)
                    
                    import datetime
                    try:
                        import pytz
                        ist_tz = pytz.timezone('Asia/Kolkata')
                        exp_dt = datetime.datetime.fromtimestamp(new_expiry, tz=ist_tz)
                        exp_str = exp_dt.strftime('%d-%m-%Y %I:%M %p')
                    except Exception:
                        exp_str = datetime.datetime.fromtimestamp(new_expiry).strftime('%d-%m-%Y %I:%M %p')

                    success_text = (
                        f'<emoji id="5224607267797606837">🎉</emoji> <b>Unlimited Pass Activated Automatically!</b>\n\n'
                        f"Hey <b>{user_name}</b>, your payment of <b>₹{amount:.2f}</b> was <b>automatically verified</b>!\n\n"
                        f"<b>Plan:</b> {dur_verbose.title()} Unlimited Access Pass\n"
                        f"<b>Order ID:</b> <code>{order_id}</code>\n"
                        f"<b>Valid Until:</b> <code>{exp_str}</code>\n"
                        f'<b>Status:</b> <emoji id="5411359377904934337">🟢</emoji> Unlimited Access (No Cooldown)\n\n'
                        f"You can now access any batch and story links without cooldown. Enjoy!"
                    )

                    # Try editing invoice message in-place
                    edited = False
                    if invoice_msg_id:
                        try:
                            await client.edit_message_text(user_id, invoice_msg_id, success_text)
                            edited = True
                        except Exception:
                            pass
                    if not edited:
                        try:
                            await client.send_message(user_id, success_text)
                        except Exception:
                            pass

                    # Log to dedicated pass log channel
                    rl_cfg = await db.get_delivery_rate_limit_config()
                    log_ch = rl_cfg.get('log_channel')
                    from plugins.arya_logger import log_pass_purchased
                    asyncio.create_task(log_pass_purchased(
                        user_id=user_id,
                        user_name=user_name,
                        duration_str=dur_verbose.title(),
                        amount=amount,
                        order_id=order_id,
                        expiry_ts=new_expiry,
                        log_channel=log_ch,
                        gateway="Cashfree PG (Auto-Verified)"
                    ))
                break
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug(f"[PASS-AUTO-VERIFY] Exception while polling order {order_id}: {e}")

async def send_or_edit_with_custom_icons(
    client,
    chat_id: int,
    text: str,
    inline_keyboard: list,
    message_id: int = None,
    parse_mode: str = "HTML",
    media_id: str = None,
    media_type: str = "photo",
    is_media_edit: bool = False,
    photo_bytes: bytes = None
) -> any:
    """
    Sends or edits a message using Telegram Bot API HTTP endpoint.
    This enables `icon_custom_emoji_id` on inline keyboard buttons and
    custom animated emojis (<tg-emoji>) in text and captions across Photos/Animations/Videos.
    """
    import aiohttp
    import json
    import re
    from config import Config

    bot_token = getattr(client, "bot_token", None)
    if not bot_token and client and hasattr(client, "me") and client.me:
        bot_token = _share_bot_token_cache.get(str(client.me.id))
    if not bot_token:
        bot_token = getattr(Config, "BOT_TOKEN", "")

    if not bot_token:
        return False

    try:
        c_id = int(chat_id)
        m_id = int(message_id) if message_id is not None else None
    except (ValueError, TypeError):
        return False

    # Convert Pyrogram <emoji id="..."> tags to Bot API <tg-emoji emoji-id="..."> tags
    api_text = re.sub(r'<emoji id="(\d+)">([^<]*)</emoji>', r'<tg-emoji emoji-id="\1">\2</tg-emoji>', text)
    url = f"https://api.telegram.org/bot{bot_token}/"

    if photo_bytes:
        try:
            form = aiohttp.FormData()
            form.add_field("chat_id", str(c_id))
            form.add_field("caption", api_text)
            form.add_field("parse_mode", parse_mode)
            form.add_field("reply_markup", json.dumps({"inline_keyboard": inline_keyboard}))
            form.add_field("photo", photo_bytes, filename="qr.png", content_type="image/png")
            session = _get_shared_bot_api_session()
            async with session.post(url + "sendPhoto", data=form, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                data = await resp.json()
                if data.get("ok"):
                    return data.get("result", True)
                logger.warning(f"Bot API sendPhoto bytes returned error: {data}")
        except Exception as e:
            logger.warning(f"Bot API sendPhoto bytes exception: {e}")
        return False

    payload = {
        "chat_id": c_id,
        "parse_mode": parse_mode,
        "reply_markup": {
            "inline_keyboard": inline_keyboard
        }
    }

    url = f"https://api.telegram.org/bot{bot_token}/"
    if media_id and not is_media_edit:
        payload["caption"] = api_text
        if media_type == "animation":
            method = "sendAnimation"
            payload["animation"] = media_id
        elif media_type == "video":
            method = "sendVideo"
            payload["video"] = media_id
        else:
            method = "sendPhoto"
            payload["photo"] = media_id
    elif is_media_edit and m_id:
        method = "editMessageCaption"
        payload["message_id"] = m_id
        payload["caption"] = api_text
    elif m_id:
        method = "editMessageText"
        payload["message_id"] = m_id
        payload["text"] = api_text
    else:
        method = "sendMessage"
        payload["text"] = api_text

    try:
        session = _get_shared_bot_api_session()
        async with session.post(url + method, json=payload, timeout=aiohttp.ClientTimeout(total=2.5)) as resp:
            data = await resp.json()
            if data.get("ok"):
                return True
            logger.warning(f"Bot API {method} returned error: {data}")
    except Exception as e:
        logger.warning(f"Bot API {method} exception: {e}")

    return False


@Client.on_callback_query(filters.regex(r'^pass#'))
async def _process_pass_callback(client, query):
    data = query.data
    user_id = query.from_user.id
    user_name = query.from_user.first_name or "User"

    # Clear pending UTR session if user navigates to any other menu/back
    if not data.startswith("pass#upibuy_") and not data.startswith("pass#upirecheck_"):
        _pending_utr_users.pop(user_id, None)

    if data == "pass#close":
        try:
            await query.message.delete()
        except Exception:
            try:
                await query.message.edit_text("❌ Panel Closed.")
            except Exception:
                pass
        return

    if data in ("pass#setlang_hi", "pass#setlang_en"):
        new_lang = "hi" if data == "pass#setlang_hi" else "en"
        await db.set_language(user_id, new_lang)
        alert_msg = "✅ भाषा बदलकर हिंदी कर दी गई है!" if new_lang == "hi" else "✅ Language switched to English!"
        try:
            await query.answer(alert_msg, show_alert=True)
        except Exception:
            pass
        data = "pass#unlock_menu"

    if data == "pass#lang_menu":
        user_lang = await db.get_language(user_id)
        is_hi = bool(user_lang == 'hi')
        lang_text = (
            '<emoji id="6030768072296502910">🌐</emoji> <b>Select Language / भाषा चुनें</b>\n'
            "──────────────────────\n\n"
            "Please choose your preferred language for Delivery Bot & Pass Subscription:\n"
            "डिलीवरी बॉट और पास सब्सक्रिप्शन के लिए अपनी पसंदीदा भाषा चुनें:\n\n"
            f"<b>Current Language:</b> {'🇮🇳 हिन्दी (Hindi)' if is_hi else '🇬🇧 English'}"
        )
        lang_buttons = [
            [InlineKeyboardButton("🇮🇳 हिन्दी (Hindi)" + ("  ✅" if is_hi else ""), callback_data="pass#setlang_hi")],
            [InlineKeyboardButton("🇬🇧 English" + ("  ✅" if not is_hi else ""), callback_data="pass#setlang_en")],
            [InlineKeyboardButton("← Back / वापस", callback_data="pass#unlock_menu")]
        ]
        lang_api_kb = [
            [{"text": "हिन्दी (Hindi)" + ("  ✅" if is_hi else ""), "callback_data": "pass#setlang_hi", "icon_custom_emoji_id": "5291933173674957761"}],
            [{"text": "English" + ("  ✅" if not is_hi else ""), "callback_data": "pass#setlang_en", "icon_custom_emoji_id": "5293993521026453119"}],
            [{"text": "← Back / वापस", "callback_data": "pass#unlock_menu"}]
        ]
        if getattr(query.message, "photo", None):
            try: await query.message.delete()
            except Exception: pass
            sent_ok = await send_or_edit_with_custom_icons(
                client=client,
                chat_id=query.message.chat.id,
                text=lang_text,
                inline_keyboard=lang_api_kb
            )
            if not sent_ok:
                await client.send_message(chat_id=query.message.chat.id, text=lang_text, reply_markup=InlineKeyboardMarkup(lang_buttons))
        else:
            sent_ok = await send_or_edit_with_custom_icons(
                client=client,
                chat_id=query.message.chat.id,
                text=lang_text,
                inline_keyboard=lang_api_kb,
                message_id=query.message.id
            )
            if not sent_ok:
                await query.message.edit_text(lang_text, reply_markup=InlineKeyboardMarkup(lang_buttons))

    elif data == "pass#guide_menu":
        user_lang = await db.get_language(user_id)
        is_hi = bool(user_lang == 'hi')
        pass_support_link = "https://t.me/AryaHelpTG"

        if is_hi:
            guide_text = (
                '<emoji id="6019130940012370273">📖</emoji> <b>आर्या सब्सक्रिप्शन गाइड और सपोर्ट</b> <emoji id="6041919344995209164">❤️</emoji>\n'
                "──────────────────────\n\n"
                '<emoji id="5881806211195605908">⭐️</emoji> <b>अनलिमिटेड एक्सेस पास क्या है?</b>\n'
                'अनलिमिटेड पास लेने पर आपको सभी ऑडियो कहानियां, बैच फाइल्स और एपिसोड्स <b>बिना किसी कूलडाउन इंतज़ार</b> और <b>बिना किसी डोनेशन मैसेज</b> के 100% तुरंत प्राप्त होते हैं।\n\n'
                "──────────────────────\n"
                '<emoji id="6019224342666157570">💳</emoji> <b>पेमेंट करने की स्टेप-बाय-स्टेप गाइड:</b>\n\n'
                '<emoji id="6084733528916893740">1️⃣</emoji> <b>Cashfree गेटवे (तुरंत ऑटो-वेरिफिकेशन):</b>\n'
                '• <b>Cashfree द्वारा भुगतान करें</b> पर टैप करके अपना पसंदीदा प्लान चुनें।\n'
                '• <b>UPI (GPay / PhonePe / Paytm / CRED)</b>, <b>डेबिट/क्रेडिट कार्ड</b>, या <b>नेट बैंकिंग</b> से भुगतान करें।\n'
                '• भुगतान पूरा होते ही बॉट पर वापस आएं — पास <b>3 से 5 सेकंड में ऑटोमैटिकली एक्टिव</b> हो जाएगा!\n\n'
                '<emoji id="6089198632752389882">2️⃣</emoji> <b>UPI (QR कोड / डायनामिक UTR):</b>\n'
                '• <b>UPI (QR) द्वारा भुगतान करें</b> पर टैप करके प्लान चुनें।\n'
                '• दिए गए QR कोड को स्कैन करके ठीक वही राशि पे करें।\n'
                '• अपने UPI ऐप (PhonePe/Paytm/GPay) की रसीद से <b>12-अंकों का UTR / Transaction No.</b> कॉपी करें।\n'
                '• बॉट में 12-अंकों का UTR भेजें — हमारा सिस्टम तुरंत वेरिफाई करके पास एक्टिव कर देगा!\n\n'
                '<emoji id="6086945664707600755">3️⃣</emoji> <b>Crypto (Oxapay गेटवे):</b>\n'
                '• <b>Crypto द्वारा भुगतान करें</b> चुनें और अपनी पसंदीदा करेंसी (<b>USDT, BTC, LTC, TRX</b>) चुनें।\n'
                '• इनवॉइस में दिए गए एड्रेस पर सटीक क्रिप्टो राशि ट्रांसफर करें।\n'
                '• ब्लॉकचेन कन्फर्मेशन मिलते ही पास अपने आप एक्टिव हो जाएगा!\n\n'
                "──────────────────────\n"
                '<emoji id="5456140674028019486">⚡️</emoji> <b>सहायता और सपोर्ट समय:</b>\n'
                '• <b>सपोर्ट समय:</b> सुबह 8:00 AM से शाम 7:00 PM IST\n'
                '• <b>जवाब मिलने का समय:</b> 30 मिनट से 2 घंटे तक का समय लग सकता है\n'
                '• यदि आपको कोई समस्या आ रही है या पेमेंट वेरिफाई नहीं हुआ, तो नीचे दिए गए <b>सहायता</b> बटन पर टैप करके सपोर्ट टीम से संपर्क करें!'
            )
            lbl_support = "सहायता"
            lbl_back = "← वापस"
        else:
            guide_text = (
                '<emoji id="6019130940012370273">📖</emoji> <b>Arya Subscription Guide & Support</b> <emoji id="6041919344995209164">❤️</emoji>\n'
                "──────────────────────\n\n"
                '<emoji id="5881806211195605908">⭐️</emoji> <b>What is Unlimited Access Pass?</b>\n'
                'Unlimited Pass gives you instant, 100% restriction-free access to all audios, batch files, and stories with <b>ZERO cooldown waiting time</b> and <b>NO donation messages</b>.\n\n'
                "──────────────────────\n"
                '<emoji id="6019224342666157570">💳</emoji> <b>Step-by-Step Payment Guide:</b>\n\n'
                '<emoji id="6084733528916893740">1️⃣</emoji> <b>Cashfree Gateway (Instant Auto-Verify):</b>\n'
                '• Tap on <b>Pay Via Cashfree</b> & select your plan.\n'
                '• Pay securely using <b>UPI (GPay / PhonePe / Paytm / CRED)</b>, <b>Debit / Credit Card</b>, or <b>Net Banking</b>.\n'
                '• After payment, return to the bot — your pass activates <b>instantly (in 3–5 seconds)</b>!\n\n'
                '<emoji id="6089198632752389882">2️⃣</emoji> <b>Pay Via UPI (QR Code / Dynamic UTR):</b>\n'
                '• Tap on <b>Pay Via UPI (QR)</b> & choose your duration.\n'
                '• Scan the dynamic QR code or pay the exact displayed amount.\n'
                '• Copy the <b>12-digit UTR / Transaction Reference ID</b> from your UPI app receipt.\n'
                '• Send the 12-digit UTR in chat or tap verify — our auto-system activates your pass immediately!\n\n'
                '<emoji id="6086945664707600755">3️⃣</emoji> <b>Crypto (Oxapay Gateway):</b>\n'
                '• Tap on <b>Pay Via Crypto</b> & select your currency (<b>USDT, BTC, LTC, TRX, etc.</b>).\n'
                '• Transfer the exact crypto amount to the generated invoice address.\n'
                '• Once the blockchain confirms (1 network confirmation), the pass activates automatically!\n\n'
                "──────────────────────\n"
                '<emoji id="5456140674028019486">⚡️</emoji> <b>Support & Assistance:</b>\n'
                '• <b>Support Hours:</b> 8:00 AM to 7:00 PM IST\n'
                '• <b>Expected Response Time:</b> 30 minutes to 2 hours\n'
                '• If your payment is delayed or you need any help, tap the <b>Support</b> button below to message our support desk!'
            )
            lbl_support = "Support"
            lbl_back = "← Back"

        guide_buttons = [
            [InlineKeyboardButton(f"🔒 {lbl_support}", url=pass_support_link)],
            [InlineKeyboardButton(lbl_back, callback_data="pass#unlock_menu")]
        ]
        guide_api_kb = [
            [{"text": lbl_support, "url": pass_support_link, "icon_custom_emoji_id": "6030833407339008632"}],
            [{"text": lbl_back, "callback_data": "pass#unlock_menu", "icon_custom_emoji_id": "5879857507198833579"}]
        ]

        if getattr(query.message, "photo", None):
            try: await query.message.delete()
            except Exception: pass
            sent_ok = await send_or_edit_with_custom_icons(
                client=client,
                chat_id=query.message.chat.id,
                text=guide_text,
                inline_keyboard=guide_api_kb
            )
            if not sent_ok:
                await client.send_message(chat_id=query.message.chat.id, text=guide_text, reply_markup=InlineKeyboardMarkup(guide_buttons))
        else:
            sent_ok = await send_or_edit_with_custom_icons(
                client=client,
                chat_id=query.message.chat.id,
                text=guide_text,
                inline_keyboard=guide_api_kb,
                message_id=query.message.id
            )
            if not sent_ok:
                await query.message.edit_text(guide_text, reply_markup=InlineKeyboardMarkup(guide_buttons))

    elif data == "pass#unlock_menu":
        rl_cfg = await db.get_delivery_rate_limit_config()
        prices = rl_cfg.get('prices', {'1d': 15, '3d': 30, '7d': 55, '1mo': 250, '6mo': 1199})
        uiver = rl_cfg.get('pass_ui_version', 'v1')
        user_lang = await db.get_language(user_id)
        is_hi = bool(user_lang == 'hi')

        if is_hi:
            title_header_v1 = '<emoji id="5773677501825945508">👑</emoji> <b>पास सब्सक्रिप्शन प्लान्स</b> <emoji id="6041919344995209164">❤️</emoji>\n──────────────────────\n\n'
            title_header_v2 = '<emoji id="5773677501825945508">👑</emoji> <b>पास सब्सक्रिप्शन प्लान्स V2</b> <emoji id="6041919344995209164">❤️</emoji>\n──────────────────────\n\n'
            benefits_sec = (
                '<emoji id="5881806211195605908">⭐️</emoji> <b>पास के मुख्य फायदे:</b>\n'
                '• <emoji id="5774077015388852135">🚫</emoji> <b>कोई डोनेशन मैसेज नहीं:</b> बिना किसी डोनेशन मैसेज के 100% क्लीन एक्सपीरियंस।\n'
                '• <emoji id="5805331990618053402">⚡️</emoji> <b>कोई एक्सेस लिमिट नहीं:</b> बिना किसी कूलडाउन के सभी ऑडियो कहानियां लगातार सुनें।\n\n'
            )
            lbl_txns = "मेरे ट्रांसक्शन्स"
            lbl_help = "हेल्प"
            lbl_close = "बंद करें"
        else:
            title_header_v1 = '<emoji id="5773677501825945508">👑</emoji> <b>Pass Subscription Plans</b> <emoji id="6041919344995209164">❤️</emoji>\n──────────────────────\n\n'
            title_header_v2 = '<emoji id="5773677501825945508">👑</emoji> <b>Pass Subscription Plans V2</b> <emoji id="6041919344995209164">❤️</emoji>\n──────────────────────\n\n'
            benefits_sec = (
                '<emoji id="5881806211195605908">⭐️</emoji> <b>Pass Benefits:</b>\n'
                '• <emoji id="5774077015388852135">🚫</emoji> <b>No Donation Messages:</b> 100% clean experience without any donation messages.\n'
                '• <emoji id="5805331990618053402">⚡️</emoji> <b>No Access Limits:</b> Unlimited story listening access without cooldown.\n\n'
            )
            lbl_txns = "My Transactions"
            lbl_help = "Help"
            lbl_close = "Close"

        if uiver == 'v2':
            # V2 (Single Gateway Flow - Cashfree or UPI based on settings):
            v2_gw = rl_cfg.get('v2_gateway', 'cashfree')
            v2_prompt = '<emoji id="6019224342666157570">💳</emoji> <b>नीचे अपना पसंदीदा पास प्लान चुनें:</b>' if is_hi else '<emoji id="6019224342666157570">💳</emoji> <b>Select your desired Pass plan below:</b>'
            methods_text = (
                f"{title_header_v2}"
                f"{benefits_sec}"
                f"{v2_prompt}"
            )

            plan_buttons = []
            plan_api_kb = []
            for idx, (dur_key, price) in enumerate(prices.items()):
                p_val = int(price) if float(price).is_integer() else price
                label = format_plan_button_label(dur_key, p_val, lang=user_lang)
                emoji_id, _ = PLAN_CUSTOM_EMOJIS[idx % len(PLAN_CUSTOM_EMOJIS)]
                cb = f"pass#upibuy_{dur_key}_{p_val}" if v2_gw == 'upi' else f"pass#cfbuy_{dur_key}_{p_val}"
                plan_buttons.append([InlineKeyboardButton(label, callback_data=cb)])
                plan_api_kb.append([{"text": label, "callback_data": cb, "icon_custom_emoji_id": emoji_id}])

            methods_buttons = list(plan_buttons)
            methods_buttons.append([InlineKeyboardButton(f"📜 {lbl_txns}", callback_data="pass#my_transactions")])
            methods_buttons.append([
                InlineKeyboardButton(f"📖 {lbl_help}", callback_data="pass#guide_menu"),
                InlineKeyboardButton(" ", callback_data="pass#lang_menu"),
                InlineKeyboardButton(lbl_close, callback_data="pass#close")
            ])

            methods_api_kb = list(plan_api_kb)
            methods_api_kb.append([{"text": lbl_txns, "callback_data": "pass#my_transactions", "icon_custom_emoji_id": "6035297458907519073"}])
            methods_api_kb.append([
                {"text": lbl_help, "callback_data": "pass#guide_menu", "icon_custom_emoji_id": "6019130940012370273"},
                {"text": " ", "callback_data": "pass#lang_menu", "icon_custom_emoji_id": "6030768072296502910"},
                {"text": lbl_close, "callback_data": "pass#close", "icon_custom_emoji_id": "5807651380332076999"}
            ])

        else:
            # V1 (Multi-Gateway Flow):
            # Pass Benefits + Available Plans Text + Select preferred method + Gateway Buttons
            plan_lines = []
            for idx, (dur_key, price) in enumerate(prices.items()):
                dur_str = str(dur_key).lower().strip()
                if dur_str.endswith('mo'):
                    num = dur_str[:-2]
                    unit = ("महीना" if num == "1" else "महीने") if is_hi else ("Month" if num == "1" else "Months")
                elif dur_str.endswith('month') or dur_str.endswith('months'):
                    num = dur_str.replace('months', '').replace('month', '').strip()
                    unit = ("महीना" if num == "1" else "महीने") if is_hi else ("Month" if num == "1" else "Months")
                elif dur_str.endswith('d'):
                    num = dur_str[:-1]
                    unit = ("दिन" if num == "1" else "दिन") if is_hi else ("Day" if num == "1" else "Days")
                elif dur_str.endswith('h'):
                    num = dur_str[:-1]
                    unit = "घंटे" if is_hi else "Hours"
                elif dur_str.endswith('m'):
                    num = dur_str[:-1]
                    unit = "मिनट" if is_hi else "Minutes"
                else:
                    num = dur_str
                    unit = "दिन" if is_hi else "Days"
                p_val = int(price) if float(price).is_integer() else price
                usd_val = max(0.50, round(float(price) / 92.0, 2))
                savings_tag = calculate_plan_savings(dur_key, price, prices)
                plan_lines.append(f'→   {num} {unit}: ₹{p_val} | ${usd_val:.2f}{savings_tag}')

            plans_str = "\n".join(plan_lines)

            sub_header = '<emoji id="6019224342666157570">💳</emoji> <b>नीचे अपना पसंदीदा पेमेंट मेथड चुनें:</b>' if is_hi else '<emoji id="6019224342666157570">💳</emoji> <b>Select your preferred payment method below:</b>'
            avail_title = '<emoji id="6007983438294949171">💎</emoji> <b>उपलब्ध प्लान्स:</b>' if is_hi else '<emoji id="6007983438294949171">💎</emoji> <b>Available Plans:</b>'

            methods_text = (
                f"{title_header_v1}"
                f"{benefits_sec}"
                f"{avail_title}\n"
                f"{plans_str}\n\n"
                f"{sub_header}"
            )

            oxapay_enabled = rl_cfg.get('oxapay_enabled', True)
            upi_enabled = rl_cfg.get('upi_enabled', True)

            methods_buttons = []
            methods_api_kb = []
            if upi_enabled:
                methods_buttons.append([InlineKeyboardButton("💳 Pay Via UPI ( QR )" if not is_hi else "💳 UPI ( QR ) द्वारा भुगतान करें", callback_data="pass#method_upi")])
                methods_api_kb.append([{"text": "Pay Via UPI ( QR )" if not is_hi else "UPI ( QR ) द्वारा भुगतान करें", "callback_data": "pass#method_upi", "icon_custom_emoji_id": "5766975922620076409"}])

            cf_lbl_en = "Pay Via Cashfree"
            cf_lbl_hi = "Cashfree द्वारा भुगतान करें"
            methods_buttons.append([InlineKeyboardButton("⚡ Pay Via Cashfree" if not is_hi else "⚡ Cashfree द्वारा भुगतान करें", callback_data="pass#method_cashfree")])
            methods_api_kb.append([{"text": cf_lbl_hi if is_hi else cf_lbl_en, "callback_data": "pass#method_cashfree", "icon_custom_emoji_id": "6271408183583969564"}])

            if oxapay_enabled:
                methods_buttons.append([InlineKeyboardButton("🌐 Pay Via Crypto (Oxapay)" if not is_hi else "🌐 Crypto द्वारा भुगतान करें", callback_data="pass#method_crypto")])
                methods_api_kb.append([{"text": "Pay Via Crypto (Oxapay)" if not is_hi else "Crypto द्वारा भुगतान करें", "callback_data": "pass#method_crypto", "icon_custom_emoji_id": "5283232570660634549"}])

            methods_buttons.append([InlineKeyboardButton(f"📜 {lbl_txns}", callback_data="pass#my_transactions")])
            methods_buttons.append([
                InlineKeyboardButton(f"📖 {lbl_help}", callback_data="pass#guide_menu"),
                InlineKeyboardButton(" ", callback_data="pass#lang_menu"),
                InlineKeyboardButton(lbl_close, callback_data="pass#close")
            ])

            methods_api_kb.append([{"text": lbl_txns, "callback_data": "pass#my_transactions", "icon_custom_emoji_id": "6035297458907519073"}])
            methods_api_kb.append([
                {"text": lbl_help, "callback_data": "pass#guide_menu", "icon_custom_emoji_id": "6019130940012370273"},
                {"text": " ", "callback_data": "pass#lang_menu", "icon_custom_emoji_id": "6030768072296502910"},
                {"text": lbl_close, "callback_data": "pass#close", "icon_custom_emoji_id": "5807651380332076999"}
            ])

        methods_kb = InlineKeyboardMarkup(methods_buttons)

        if getattr(query.message, "photo", None):
            try: await query.message.delete()
            except Exception: pass
            sent_ok = await send_or_edit_with_custom_icons(
                client=client,
                chat_id=query.message.chat.id,
                text=methods_text,
                inline_keyboard=methods_api_kb
            )
            if not sent_ok:
                await client.send_message(chat_id=query.message.chat.id, text=methods_text, reply_markup=methods_kb)
        else:
            sent_ok = await send_or_edit_with_custom_icons(
                client=client,
                chat_id=query.message.chat.id,
                text=methods_text,
                inline_keyboard=methods_api_kb,
                message_id=query.message.id
            )
            if not sent_ok:
                await query.message.edit_text(methods_text, reply_markup=methods_kb)

    elif data == "pass#method_cashfree":
        rl_cfg = await db.get_delivery_rate_limit_config()
        prices = rl_cfg.get('prices', {'1d': 15, '3d': 30, '7d': 55, '1mo': 250, '6mo': 1199})
        user_lang = await db.get_language(user_id)
        is_hi = bool(user_lang == 'hi')
        
        plan_buttons = []
        plan_api_kb = []
        for idx, (dur_key, price) in enumerate(prices.items()):
            p_val = int(price) if float(price).is_integer() else price
            label = format_plan_button_label(dur_key, p_val, lang=user_lang)
            emoji_id, _ = PLAN_CUSTOM_EMOJIS[idx % len(PLAN_CUSTOM_EMOJIS)]
            cb = f"pass#cfbuy_{dur_key}_{p_val}"
            plan_buttons.append([InlineKeyboardButton(label, callback_data=cb)])
            plan_api_kb.append([{"text": label, "callback_data": cb, "icon_custom_emoji_id": emoji_id}])

        back_lbl = "← वापस" if is_hi else "← Back"
        plan_buttons.append([InlineKeyboardButton(back_lbl, callback_data="pass#unlock_menu")])
        plan_api_kb.append([{"text": back_lbl, "callback_data": "pass#unlock_menu"}])

        if is_hi:
            text = (
                '<emoji id="5920332557466997677">⚡</emoji> <b>Cashfree द्वारा भुगतान करें</b>\n'
                "──────────────────────\n\n"
                "UPI, Cards, NetBanking से तुरंत भुगतान।\n\n"
                "अपना पसंदीदा पास प्लान चुनें:"
            )
        else:
            text = (
                '<emoji id="5920332557466997677">⚡</emoji> <b>Pay with Cashfree</b>\n'
                "──────────────────────\n\n"
                "Instant payment with UPI, Cards, NetBanking.\n\n"
                "Select your desired Pass plan:"
            )
        if getattr(query.message, "photo", None):
            try: await query.message.delete()
            except Exception: pass
            sent_ok = await send_or_edit_with_custom_icons(
                client=client,
                chat_id=query.message.chat.id,
                text=text,
                inline_keyboard=plan_api_kb
            )
            if not sent_ok:
                await client.send_message(chat_id=query.message.chat.id, text=text, reply_markup=InlineKeyboardMarkup(plan_buttons))
        else:
            sent_ok = await send_or_edit_with_custom_icons(
                client=client,
                chat_id=query.message.chat.id,
                text=text,
                inline_keyboard=plan_api_kb,
                message_id=query.message.id
            )
            if not sent_ok:
                await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(plan_buttons))

    elif data == "pass#method_upi":
        rl_cfg = await db.get_delivery_rate_limit_config()
        if not rl_cfg.get('upi_enabled', True):
            return await query.answer("⚠️ Pay Via UPI is currently disabled by administrator.", show_alert=True)
        prices = rl_cfg.get('prices', {'1d': 15, '3d': 30, '7d': 55, '1mo': 250, '6mo': 1199})
        user_lang = await db.get_language(user_id)
        is_hi = bool(user_lang == 'hi')
        
        plan_buttons = []
        plan_api_kb = []
        for idx, (dur_key, price) in enumerate(prices.items()):
            p_val = int(price) if float(price).is_integer() else price
            label = format_plan_button_label(dur_key, p_val, lang=user_lang)
            emoji_id, _ = PLAN_CUSTOM_EMOJIS[idx % len(PLAN_CUSTOM_EMOJIS)]
            cb = f"pass#upibuy_{dur_key}_{p_val}"
            plan_buttons.append([InlineKeyboardButton(label, callback_data=cb)])
            plan_api_kb.append([{"text": label, "callback_data": cb, "icon_custom_emoji_id": emoji_id}])

        back_lbl = "← वापस" if is_hi else "← Back"
        plan_buttons.append([InlineKeyboardButton(back_lbl, callback_data="pass#unlock_menu")])
        plan_api_kb.append([{"text": back_lbl, "callback_data": "pass#unlock_menu"}])

        if is_hi:
            text = (
                '<emoji id="5766975922620076409">💳</emoji> <b>UPI ( QR ) द्वारा भुगतान करें</b>\n'
                "──────────────────────\n\n"
                "Paytm, PhonePe, GPay, BHIM या किसी भी UPI ऐप से तुरंत भुगतान करें।\n\n"
                '<emoji id="6019224342666157570">💳</emoji> <b>नीचे अपना पसंदीदा पास प्लान चुनें:</b>'
            )
        else:
            text = (
                '<emoji id="5766975922620076409">💳</emoji> <b>Pay Via UPI ( QR )</b>\n'
                "──────────────────────\n\n"
                "Instant payment with Paytm, PhonePe, GPay, BHIM, or any UPI app.\n\n"
                '<emoji id="6019224342666157570">💳</emoji> <b>Select your desired Pass plan below:</b>'
            )
        if getattr(query.message, "photo", None):
            try: await query.message.delete()
            except Exception: pass
            sent_ok = await send_or_edit_with_custom_icons(
                client=client,
                chat_id=query.message.chat.id,
                text=text,
                inline_keyboard=plan_api_kb
            )
            if not sent_ok:
                await client.send_message(chat_id=query.message.chat.id, text=text, reply_markup=InlineKeyboardMarkup(plan_buttons))
        else:
            sent_ok = await send_or_edit_with_custom_icons(
                client=client,
                chat_id=query.message.chat.id,
                text=text,
                inline_keyboard=plan_api_kb,
                message_id=query.message.id
            )
            if not sent_ok:
                await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(plan_buttons))

    elif data == "pass#method_crypto":
        rl_cfg = await db.get_delivery_rate_limit_config()
        if not rl_cfg.get('oxapay_enabled', True):
            return await query.answer("⚠️ Crypto payments are currently disabled by administrator.", show_alert=True)

        prices = rl_cfg.get('prices', {'1d': 15, '3d': 30, '7d': 55, '1mo': 250, '6mo': 1199})
        user_lang = await db.get_language(user_id)
        is_hi = bool(user_lang == 'hi')
        
        plan_buttons = []
        plan_api_kb = []
        for idx, (dur_key, price) in enumerate(prices.items()):
            p_val = int(price) if float(price).is_integer() else price
            usd_val = round(float(price) / 92.0, 2)
            # OxaPay minimum is $0.50 USD (0.50 USDT = ₹46 INR)
            if usd_val >= 0.50:
                label = f"{format_plan_button_label(dur_key, p_val, lang=user_lang)} [${usd_val:.2f}]"
                emoji_id, _ = PLAN_CUSTOM_EMOJIS[idx % len(PLAN_CUSTOM_EMOJIS)]
                cb = f"pass#oxabuy_{dur_key}_{p_val}"
                plan_buttons.append([InlineKeyboardButton(label, callback_data=cb)])
                plan_api_kb.append([{"text": label, "callback_data": cb, "icon_custom_emoji_id": emoji_id}])

        back_lbl = "← वापस" if is_hi else "← Back"
        plan_buttons.append([InlineKeyboardButton(back_lbl, callback_data="pass#unlock_menu")])
        plan_api_kb.append([{"text": back_lbl, "callback_data": "pass#unlock_menu"}])

        if len(plan_buttons) <= 1:
            if is_hi:
                text = (
                    '<emoji id="5283232570660634549">🌐</emoji> <b>Crypto ( OxaPay ) द्वारा भुगतान करें</b>\n'
                    "──────────────────────\n\n"
                    "⚠️ <b>कोई योग्य क्रिप्टो प्लान उपलब्ध नहीं है</b>\n\n"
                    "OxaPay पर न्यूनतम पेमेंट <b>$0.50 USD (~₹46)</b> होना अनिवार्य है।\n"
                    "वर्तमान में कोई भी प्लान इस न्यूनतम राशि को पूरा नहीं करता।\n\n"
                    "👉 कृपया इसके स्थान पर <b>UPI (INR)</b> या <b>Cashfree</b> से भुगतान करें!"
                )
            else:
                text = (
                    '<emoji id="5283232570660634549">🌐</emoji> <b>Pay with Crypto ( OxaPay )</b>\n'
                    "──────────────────────\n\n"
                    "⚠️ <b>No Eligible Crypto Plans Available</b>\n\n"
                    "OxaPay requires a minimum order amount of <b>$0.50 USD (~₹46)</b>.\n"
                    "None of the current configured plans meet this minimum.\n\n"
                    "👉 Please pay using <b>UPI (INR)</b> or <b>Cashfree</b> instead!"
                )
        else:
            if is_hi:
                text = (
                    '<emoji id="5283232570660634549">🌐</emoji> <b>Crypto ( OxaPay ) द्वारा भुगतान करें</b>\n'
                    "──────────────────────\n\n"
                    "USDT, BTC, SOL, TON के ज़रिए तुरंत भुगतान करें।\n\n"
                    '<emoji id="6026080811277621020">💡</emoji> <b>नोट:</b> OxaPay की न्यूनतम सीमा $0.50 USD (~₹46) है। केवल योग्य प्लान नीचे दिखाए गए हैं:\n\n'
                    '<emoji id="6019224342666157570">💳</emoji> <b>नीचे अपना पसंदीदा पास प्लान चुनें:</b>'
                )
            else:
                text = (
                    '<emoji id="5283232570660634549">🌐</emoji> <b>Pay with Crypto ( OxaPay )</b>\n'
                    "──────────────────────\n\n"
                    "Instant payment with USDT, BTC, SOL, TON.\n\n"
                    '<emoji id="6026080811277621020">💡</emoji> <b>Note:</b> OxaPay has a minimum order limit of $0.50 USD (~₹46). Only eligible plans are displayed below:\n\n'
                    '<emoji id="6019224342666157570">💳</emoji> <b>Select your desired Pass plan below:</b>'
                )
        if getattr(query.message, "photo", None):
            try: await query.message.delete()
            except Exception: pass
            sent_ok = await send_or_edit_with_custom_icons(
                client=client,
                chat_id=query.message.chat.id,
                text=text,
                inline_keyboard=plan_api_kb
            )
            if not sent_ok:
                await client.send_message(chat_id=query.message.chat.id, text=text, reply_markup=InlineKeyboardMarkup(plan_buttons))
        else:
            sent_ok = await send_or_edit_with_custom_icons(
                client=client,
                chat_id=query.message.chat.id,
                text=text,
                inline_keyboard=plan_api_kb,
                message_id=query.message.id
            )
            if not sent_ok:
                await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(plan_buttons))

    elif data == "pass#my_transactions":
        user_lang = await db.get_language(user_id)
        is_hi = bool(user_lang == 'hi')

        # Show instant loading message with custom animated emoji 5220046725493828505
        load_text = (
            '<emoji id="5220046725493828505">⏳</emoji> <b>ट्रांसक्शन्स लोड हो रहे हैं, कृपया प्रतीक्षा करें...</b>'
            if is_hi else
            '<emoji id="5220046725493828505">⏳</emoji> <b>Loading transactions, please wait...</b>'
        )
        try:
            await send_or_edit_with_custom_icons(
                client=client,
                chat_id=query.message.chat.id,
                text=load_text,
                inline_keyboard=[],
                message_id=query.message.id
            )
        except Exception:
            pass

        pass_info = await db.get_user_unlimited_pass(user_id)

        if pass_info.get('active'):
            import datetime
            try:
                import pytz
                ist_tz = pytz.timezone('Asia/Kolkata')
                exp_dt = datetime.datetime.fromtimestamp(pass_info['expires_at'], tz=ist_tz)
                exp_str = exp_dt.strftime('%d-%m-%Y %I:%M %p')
            except Exception:
                exp_str = datetime.datetime.fromtimestamp(pass_info['expires_at']).strftime('%d-%m-%Y %I:%M %p')
            status_line = f'<emoji id="6267118537752450044">🟢</emoji> <b>{"सक्रिय" if is_hi else "Active"}</b> ({"वैधता" if is_hi else "Valid until"}: <code>{exp_str}</code>)'
        else:
            status_line = f'<emoji id="6264989883241076562">⚪</emoji> <b>{"कोई एक्टिव पास नहीं" if is_hi else "No Active Pass"}</b>' 

        parts = data.split("_")
        t_page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

        all_txns = await db.get_user_pass_transactions(user_id, limit=100, paid_only=False)

        per_page = 5
        import math
        total_pages = max(1, math.ceil(len(all_txns) / per_page)) if all_txns else 1
        t_page = max(0, min(t_page, total_pages - 1))

        txns = all_txns[t_page * per_page : (t_page + 1) * per_page] if all_txns else []

        def get_circle_digit(num: int) -> str:
            circle_map = {
                1: "➊", 2: "➋", 3: "➌", 4: "➍", 5: "➎",
                6: "➏", 7: "➐", 8: "➑", 9: "➒", 10: "➓",
                11: "⓫", 12: "⓬", 13: "⓭", 14: "⓮", 15: "⓯",
                16: "⓰", 17: "⓱", 18: "⓲", 19: "⓳", 20: "⓴"
            }
            return circle_map.get(num, f"[{num}]")

        nav_buttons = []
        api_nav_buttons = []

        if txns:
            t_items = []
            for i, txn in enumerate(txns, 1):
                global_idx = (t_page * per_page) + i
                t_time = txn.get('time', 0)
                try:
                    import datetime, pytz
                    ist_tz = pytz.timezone('Asia/Kolkata')
                    t_dt = datetime.datetime.fromtimestamp(t_time, tz=ist_tz)
                    t_str = t_dt.strftime('%d/%m/%Y | %I:%M %p')
                except Exception:
                    import datetime
                    t_str = datetime.datetime.fromtimestamp(t_time).strftime('%d/%m/%Y | %I:%M %p') if t_time else "N/A"

                p_name = str(txn.get('plan') or 'Pass')
                dur_verb = p_name
                try:
                    from database import parse_duration_to_seconds, format_duration_verbose
                    dur_verb = format_duration_verbose(parse_duration_to_seconds(p_name, default_unit='d'))
                except Exception:
                    dur_verb = p_name

                try:
                    amt = f"₹{float(txn.get('amount', 0)):.2f}"
                except Exception:
                    amt = "₹0.00"

                gw = txn.get('gateway', 'Cashfree')
                st = str(txn.get('status', 'PENDING')).upper()
                if st == 'PAID':
                    st_str = '<emoji id="6019175208240289774">✅</emoji> ( पेड )' if is_hi else '<emoji id="6019175208240289774">✅</emoji> ( Paid )'
                elif st == 'FAILED':
                    st_str = '<emoji id="5847933199996427721">❌</emoji> ( फेल्ड )' if is_hi else '<emoji id="5847933199996427721">❌</emoji> ( Failed )'
                else:
                    st_str = '<emoji id="5258113901106580375">⏳</emoji> ( पेंडिंग )' if is_hi else '<emoji id="5258113901106580375">⏳</emoji> ( Pending )'

                oid = txn.get('id', 'N/A')
                c_badge = get_circle_digit(global_idx)
                sep_line = f"┄┄┄┄┄┄┄ {c_badge} ┄┄┄┄┄┄┄"

                if is_hi:
                    t_items.append(
                        f"{sep_line}\n\n"
                        f"<emoji id=\"6021683099773966917\">🆔</emoji> <b>ऑर्डर आईडी:-</b> <code>{oid}</code>\n"
                        f"<emoji id=\"6021435576513730578\">👑</emoji> <b>प्लान:-</b> {str(dur_verb).title()} ({amt})\n"
                        f"<emoji id=\"6030443364178992166\">💳</emoji> <b>पेमेंट मोड:-</b> {gw}\n"
                        f"<emoji id=\"5807800879553715710\">📊</emoji> <b>स्थिति:-</b> {st_str}\n"
                        f"<emoji id=\"6023880246128810031\">📅</emoji> <b>लेनदेन तारीख:-</b> <code>{t_str}</code>"
                    )
                else:
                    t_items.append(
                        f"{sep_line}\n\n"
                        f"<emoji id=\"6021683099773966917\">🆔</emoji> <b>Order ID:-</b> <code>{oid}</code>\n"
                        f"<emoji id=\"6021435576513730578\">👑</emoji> <b>Plan:-</b> {str(dur_verb).title()} ({amt})\n"
                        f"<emoji id=\"6030443364178992166\">💳</emoji> <b>Payment Mode:-</b> {gw}\n"
                        f"<emoji id=\"5807800879553715710\">📊</emoji> <b>Status:-</b> {st_str}\n"
                        f"<emoji id=\"6023880246128810031\">📅</emoji> <b>TXN Date:-</b> <code>{t_str}</code>"
                    )
            txns_body = "\n\n\n".join(t_items)

            if total_pages > 1:
                p_row = []
                p_api_row = []
                if t_page > 0:
                    p_row.append(InlineKeyboardButton("◀️ Prev" if not is_hi else "◀️ पिछला", callback_data=f"pass#my_transactions_{t_page - 1}"))
                    p_api_row.append({"text": "◀️ Prev" if not is_hi else "◀️ पिछला", "callback_data": f"pass#my_transactions_{t_page - 1}"})
                else:
                    p_row.append(InlineKeyboardButton("⏺", callback_data=f"pass#my_transactions_{t_page}"))
                    p_api_row.append({"text": "⏺", "callback_data": f"pass#my_transactions_{t_page}"})

                p_row.append(InlineKeyboardButton(f"{t_page + 1}/{total_pages}", callback_data=f"pass#my_transactions_{t_page}"))
                p_api_row.append({"text": f"{t_page + 1}/{total_pages}", "callback_data": f"pass#my_transactions_{t_page}"})

                if t_page < total_pages - 1:
                    p_row.append(InlineKeyboardButton("Next ▶️" if not is_hi else "अगला ▶️", callback_data=f"pass#my_transactions_{t_page + 1}"))
                    p_api_row.append({"text": "Next ▶️" if not is_hi else "अगला ▶️", "callback_data": f"pass#my_transactions_{t_page + 1}"})
                else:
                    p_row.append(InlineKeyboardButton("⏺", callback_data=f"pass#my_transactions_{t_page}"))
                    p_api_row.append({"text": "⏺", "callback_data": f"pass#my_transactions_{t_page}"})

                nav_buttons.append(p_row)
                api_nav_buttons.append(p_api_row)
        else:
            txns_body = "आपके खाते पर कोई पिछला लेनदेन नहीं मिला।" if is_hi else "No previous transactions found on your account."

        back_lbl = "← वापस" if is_hi else "← Back"
        if is_hi:
            text = (
                '<emoji id="6035297458907519073">📜</emoji> <b>मेरे ट्रांसक्शन्स और पास स्थिति</b>\n'
                "──────────────────────\n"
                f"<b>यूजर:</b> {user_name} (<code>{user_id}</code>)\n"
                f"<b>पास स्थिति:</b> {status_line}\n"
                "──────────────────────\n"
                "<b>हाल के लेनदेन:</b>\n\n"
                f"{txns_body}"
            )
        else:
            text = (
                '<emoji id="6035297458907519073">📜</emoji> <b>My Transactions & Pass Status</b>\n'
                "──────────────────────\n"
                f"<b>User:</b> {user_name} (<code>{user_id}</code>)\n"
                f"<b>Pass Status:</b> {status_line}\n"
                "──────────────────────\n"
                "<b>Recent Transactions:</b>\n\n"
                f"{txns_body}"
            )
        kb_rows = list(nav_buttons)
        kb_rows.append([InlineKeyboardButton(back_lbl, callback_data="pass#unlock_menu")])
        api_kb_rows = list(api_nav_buttons)
        api_kb_rows.append([{"text": back_lbl, "callback_data": "pass#unlock_menu"}])
        kb = InlineKeyboardMarkup(kb_rows)
        api_kb = api_kb_rows
        if getattr(query.message, "photo", None):
            try: await query.message.delete()
            except Exception: pass
            sent_ok = await send_or_edit_with_custom_icons(
                client=client,
                chat_id=query.message.chat.id,
                text=text,
                inline_keyboard=api_kb
            )
            if not sent_ok:
                await client.send_message(chat_id=query.message.chat.id, text=text, reply_markup=kb)
        else:
            sent_ok = await send_or_edit_with_custom_icons(
                client=client,
                chat_id=query.message.chat.id,
                text=text,
                inline_keyboard=api_kb,
                message_id=query.message.id
            )
            if not sent_ok:
                await query.message.edit_text(text, reply_markup=kb)

    elif data.startswith("pass#upibuy_"):
        parts = data.split("_")
        dur_key = parts[1]
        amount = float(parts[2])

        user_lang = await db.get_language(user_id)
        is_hi = bool(user_lang == 'hi')

        # Show instant loading message with custom animated emoji 5220046725493828505
        load_text = (
            '<emoji id="5220046725493828505">⏳</emoji> <b>UPI QR कोड लोड हो रहा है, कृपया प्रतीक्षा करें...</b>'
            if is_hi else
            '<emoji id="5220046725493828505">⏳</emoji> <b>Loading UPI QR Code, please wait...</b>'
        )
        try:
            await send_or_edit_with_custom_icons(
                client=client,
                chat_id=query.message.chat.id,
                text=load_text,
                inline_keyboard=[],
                message_id=query.message.id
            )
        except Exception:
            pass

        dur_str = str(dur_key).lower().strip()
        if dur_str.endswith('mo'):
            count = dur_str[:-2]
            unit = "Month" if count == "1" else "Months"
        elif dur_str.endswith('month') or dur_str.endswith('months'):
            count = dur_str.replace('months', '').replace('month', '').strip()
            unit = "Month" if count == "1" else "Months"
        elif dur_str.endswith('d'):
            count = dur_str[:-1]
            unit = "Days"
        elif dur_str.endswith('h'):
            count = dur_str[:-1]
            unit = "Hours"
        elif dur_str.endswith('m'):
            count = dur_str[:-1]
            unit = "Minutes"
        else:
            count = dur_str
            unit = "Days"

        base_amt = float(amount)
        # Generate guaranteed unique collision-free dynamic amount among active orders
        dyn_amount = await generate_unique_dynamic_upi_amount(base_amt)

        # Generate unified sequential order ID: PASS-{user_id}-{dur_tag}-{order_num}
        from database import parse_duration_to_seconds, format_duration_friendly
        dur_sec = parse_duration_to_seconds(dur_key, default_unit='d')
        dur_tag = format_duration_friendly(dur_sec).upper()
        order_num = await db.get_next_pass_order_number()
        order_id = f"PASS-{user_id}-{dur_tag}-{order_num}"

        rl_cfg = await db.get_delivery_rate_limit_config()
        from config import Config
        raw_upi = str(rl_cfg.get("upi_id") or getattr(Config, "UPI_ID", "") or os.environ.get("UPI_ID", "") or "").strip()
        payee_name = str(rl_cfg.get("upi_name") or "Arya Delivery Pass").strip()

        if not raw_upi:
            err_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("← Back", callback_data="pass#method_upi")]
            ])
            return await query.message.edit_text(
                "⚠️ <b>UPI Not Configured</b>\n\n"
                "The bot administrator has not configured a UPI ID yet. "
                "Please use <b>Cashfree</b> or <b>Crypto</b> instead!",
                reply_markup=err_kb
            )

        # Register pending order session with dynamic amount and unified order ID
        _pending_utr_users[user_id] = {
            'dur_key': dur_key,
            'amount': dyn_amount,
            'base_amount': base_amt,
            'order_id': order_id,
            'ts': time.time()
        }

        # Save order document to MongoDB
        try:
            await db.pass_orders.insert_one({
                'order_id': order_id,
                'user_id': user_id,
                'user_name': user_name,
                'plan': dur_key,
                'amount': dyn_amount,
                'base_amount': base_amt,
                'gateway': 'Pay Via UPI (INR)',
                'status': 'PENDING',
                'created_at': time.time(),
                'expires_at': time.time() + 600
            })
        except Exception:
            pass

        # Build clean UPI payment URI and QR URL with dynamic amount
        import urllib.parse
        pn_clean = urllib.parse.quote_plus(payee_name or "Merchant")
        tn_clean = urllib.parse.quote_plus(order_id)
        upi_payload = f"upi://pay?pa={raw_upi}&pn={pn_clean}&am={dyn_amount:.2f}&cu=INR&tn={tn_clean}"

        user_lang = await db.get_language(user_id)
        is_hi = bool(user_lang == 'hi')
        uiver = rl_cfg.get('pass_ui_version', 'v1')
        back_cb = "pass#unlock_menu" if uiver == 'v2' else "pass#method_upi"

        if is_hi:
            plan_name = format_plan_name_friendly(dur_key, lang='hi')
            caption = (
                '<emoji id="5766975922620076409">⚡️</emoji> <b>UPI पेमेंट ऑर्डर बनाया गया!</b>\n\n'
                "──────────────────────\n"
                f"• <b>प्लान:</b> {plan_name} का अनलिमिटेड पास\n"
                f"• <b>भुगतान की सटीक राशि:</b> <code>₹{dyn_amount:.2f}</code>\n"
                f"• <b>UPI ID:</b> <code>{raw_upi}</code> (कॉपी करने के लिए टैप करें)\n"
                f"• <b>ऑर्डर ID:</b> <code>{order_id}</code>\n\n"
                '<emoji id="5807800879553715710">📲</emoji> <b>भुगतान निर्देश:</b>\n'
                "1. ऊपर दिए गए QR कोड को स्कैन करें या सीधे UPI ID पर पेमेंट करें।\n"
                f"2. बिल्कुल सटीक <b>₹{dyn_amount:.2f}</b> का भुगतान करें (पैसे कम या ज्यादा न करें)।\n"
                "3. <b>ऑटोमैटिक वेरिफिकेशन:</b> आपको UTR सबमिट करने की कोई आवश्यकता नहीं है! पेमेंट करने के 5-15 सेकंड में सिस्टम ऑटोमैटिकली पास एक्टिवेट कर देगा।\n\n"
                '<emoji id="6034898821517940846">⏰</emoji> <b>भुगतान की प्रतीक्षा में...</b> (10 मिनट के लिए वैध)\n'
                "जैसे ही आपका पेमेंट प्राप्त होगा, आपका अनलिमिटेड पास तुरंत सक्रिय हो जाएगा!"
            )
            btn_status = "पेमेंट स्टेटस चेक करें"
            btn_back = "← वापस"
        else:
            plan_name = format_plan_name_friendly(dur_key, lang='en')
            caption = (
                '<emoji id="5766975922620076409">⚡️</emoji> <b>UPI Payment Order Created!</b>\n\n'
                "──────────────────────\n"
                f"• <b>Plan:</b> {plan_name} Unlimited Access Pass\n"
                f"• <b>Exact Amount to Pay:</b> <code>₹{dyn_amount:.2f}</code>\n"
                f"• <b>UPI ID:</b> <code>{raw_upi}</code> (Tap to Copy)\n"
                f"• <b>Order ID:</b> <code>{order_id}</code>\n\n"
                '<emoji id="5807800879553715710">📲</emoji> <b>Payment Instructions:</b>\n'
                "1. Scan the QR code above or pay directly to the UPI ID.\n"
                f"2. Pay EXACTLY <b>₹{dyn_amount:.2f}</b> (do not round off paise).\n"
                "3. <b>Zero Hassle:</b> You do NOT need to submit UTR! Our automated system verifies payment within 5-15 seconds.\n\n"
                '<emoji id="6034898821517940846">⏰</emoji> <b>Waiting for Payment...</b> (Valid for 10 Minutes)\n'
                "Your unlimited access pass will activate automatically as soon as payment is detected!"
            )
            btn_status = "Check Payment Status"
            btn_back = "← Back"

        photo_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🔄 {btn_status}", callback_data=f"pass#upistatus_{order_id}_{dur_key}_{dyn_amount}")],
            [InlineKeyboardButton(btn_back, callback_data=back_cb)]
        ])
        photo_api_kb = [
            [{"text": btn_status, "callback_data": f"pass#upistatus_{order_id}_{dur_key}_{dyn_amount}", "icon_custom_emoji_id": "5807492110059838726"}],
            [{"text": btn_back, "callback_data": back_cb}]
        ]

        # Generate QR buffer with dynamic amount and unified order ID
        qr_buf = generate_upi_qr_bytes(raw_upi, dyn_amount, payee_name, order_id)
        qr_bytes = qr_buf.getvalue()

        try:
            await query.message.delete()
        except Exception:
            pass

        sent_res = await send_or_edit_with_custom_icons(
            client=client,
            chat_id=query.message.chat.id,
            text=caption,
            inline_keyboard=photo_api_kb,
            photo_bytes=qr_bytes
        )

        sent_msg_id = sent_res.get("message_id") if isinstance(sent_res, dict) else None
        if not sent_res:
            sent_msg = await client.send_photo(
                chat_id=query.message.chat.id,
                photo=qr_buf,
                caption=caption,
                reply_markup=photo_kb
            )
            sent_msg_id = sent_msg.id if sent_msg else None

        # Start real-time background polling for automated payment verification
        asyncio.create_task(_poll_upi_payment(
            client=client,
            user_id=user_id,
            order_id=order_id,
            dyn_amount=dyn_amount,
            dur_key=dur_key,
            count=count,
            unit=unit,
            message_id=sent_msg_id,
            chat_id=query.message.chat.id
        ))

        # Schedule automatic reminder under 5 minutes (3 mins) if payment not completed
        asyncio.create_task(schedule_pass_payment_reminder(
            client=client,
            user_id=user_id,
            order_id=order_id,
            gateway_name="Pay Via UPI (INR)",
            amount_str=f"₹{dyn_amount:.2f}",
            dur_verbose=f"{count} {unit}",
            dur_key=dur_key,
            dyn_amount=dyn_amount
        ))

    elif data.startswith("pass#upistatus_"):
        parts = data.split("_")
        order_id = parts[1]
        dur_key = parts[2]
        dyn_amount = float(parts[3])

        user_lang = await db.get_language(user_id)
        is_hi = bool(user_lang == 'hi')

        try:
            await query.answer("बैंक से पेमेंट चेक किया जा रहा है..." if is_hi else "Checking payment with bank...", show_alert=False)
        except Exception:
            pass

        # Check if pass already active
        pass_info = await db.get_user_unlimited_pass(user_id)
        if pass_info.get('active'):
            return await query.answer("✅ आपका अनलिमिटेड एक्सेस पास पहले से एक्टिव है!" if is_hi else "✅ Your Unlimited Access Pass is already active!", show_alert=True)

        from plugins.gmail_helper import find_upi_payment_by_amount
        from database import parse_duration_to_seconds, format_duration_verbose
        dur_sec = parse_duration_to_seconds(dur_key, default_unit='d')
        dur_verbose = format_duration_verbose(dur_sec)

        res = await find_upi_payment_by_amount(
            expected_amount=dyn_amount,
            user_id=user_id,
            order_id=order_id
        )
        if res.get('success'):
            extracted_utr = res.get('utr') or f"AUTO-{int(time.time())}"
            payer_name = res.get('payer_name') or "UPI Payer"

            # Check if claimed by another user
            is_claimed_by_other = await db.is_utr_claimed_by_other(extracted_utr, user_id=user_id, order_id=order_id)
            if is_claimed_by_other:
                return await query.answer("⚠️ यह पेमेंट पहले ही किसी अन्य पास के लिए प्रोसेस किया जा चुका है!" if is_hi else "⚠️ This payment was already processed for another pass!", show_alert=True)

            tg_user_name = query.from_user.first_name if query.from_user else "User"
            order_doc_paid = await db.pass_orders.find_one({'order_id': order_id})
            if order_doc_paid and order_doc_paid.get('user_name'):
                tg_user_name = order_doc_paid.get('user_name')

            await db.record_used_utr(
                utr=extracted_utr,
                user_id=user_id,
                amount=dyn_amount,
                order_id=order_id,
                user_name=tg_user_name,
                gateway="Pay Via UPI (INR)"
            )
            _cancel_cooldown_reminders(user_id)
            await db.activate_user_unlimited_pass(
                user_id=user_id,
                duration_seconds=dur_sec,
                order_id=order_id,
                amount=dyn_amount,
                user_name=tg_user_name,
                gateway=f"Pay Via UPI (INR) [Auto Verified {extracted_utr}]"
            )
            await db.pass_orders.update_one(
                {'order_id': order_id},
                {'$set': {'status': 'PAID', 'paid_at': time.time(), 'utr': extracted_utr, 'bank_payer_name': payer_name}},
                upsert=True
            )

            _active_upi_amounts.pop(dyn_amount, None)
            _pending_utr_users.pop(user_id, None)
            _active_order_reminders.pop(f"{user_id}_{order_id}", None)

            if is_hi:
                plan_name = format_plan_name_friendly(dur_key, lang='hi')
                success_text = (
                    f'<emoji id="6267118537752450044">🟢</emoji> <b>पेमेंट ऑटोमैटिकली वेरीफाई हो गया!</b>\n\n'
                    f"• <b>ऑर्डर ID:</b> <code>{order_id}</code>\n"
                    f"• <b>प्लान:</b> {plan_name} अनलिमिटेड एक्सेस पास\n"
                    f"• <b>भुगतान की गई राशि:</b> <code>₹{dyn_amount:.2f}</code>\n"
                    f"• <b>UTR / संदर्भ:</b> <code>{extracted_utr}</code>\n"
                    f"• <b>स्टेटस:</b> ✅ <b>सक्रिय और तैयार</b>\n\n"
                    f'<blockquote><emoji id="5850176641803753392">🎉</emoji> धन्यवाद! आपका अनलिमिटेड एक्सेस पास एक्टिवेट हो चुका है। अब बिना किसी लिमिट के अनलिमिटेड फाइल्स डाउनलोड करें!</blockquote>'
                )
                lbl_txns = "📜 मेरे ट्रांसक्शन्स"
                lbl_supp = "🔒 सहायता"
            else:
                plan_name = format_plan_name_friendly(dur_key, lang='en')
                success_text = (
                    f'<emoji id="6267118537752450044">🟢</emoji> <b>Payment Automatically Verified!</b>\n\n'
                    f"• <b>Order ID:</b> <code>{order_id}</code>\n"
                    f"• <b>Plan:</b> {plan_name} Unlimited Access Pass\n"
                    f"• <b>Amount Paid:</b> <code>₹{dyn_amount:.2f}</code>\n"
                    f"• <b>UTR / Ref:</b> <code>{extracted_utr}</code>\n"
                    f"• <b>Status:</b> ✅ <b>Active & Ready</b>\n\n"
                    f'<blockquote><emoji id="5850176641803753392">🎉</emoji> ᴛʜᴀɴᴋ ʏᴏᴜ! ʏᴏᴜʀ ᴜɴʟɪᴍɪᴛᴇᴅ ᴀᴄᴄᴇꜱꜱ ᴘᴀꜱꜱ ʜᴀꜱ ʙᴇᴇɴ ᴀᴄᴛɪᴠᴀᴛᴇᴅ. ᴇɴᴊᴏʏ ᴜɴʟɪᴍɪᴛᴇᴅ ɪɴꜱᴛᴀɴᴛ ᴅᴏᴡɴʟᴏᴀᴅꜱ ᴡɪᴛʜ ᴢᴇʀᴏ ʟɪᴍɪᴛꜱ!</blockquote>'
                )
                lbl_txns = "📜 My Transactions"
                lbl_supp = "🔒 Support"

            success_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(lbl_txns, callback_data="pass#my_transactions")],
                [InlineKeyboardButton(lbl_supp, url="https://t.me/AryaHelpTG")]
            ])
            try:
                await query.message.edit_caption(caption=success_text, reply_markup=success_kb)
            except Exception:
                try:
                    await query.message.delete()
                except Exception:
                    pass
                await client.send_message(chat_id=query.message.chat.id, text=success_text, reply_markup=success_kb)

            return await query.answer("✅ पेमेंट सफलतापूर्वक वेरीफाई हो गया!" if is_hi else "✅ Payment Verified Successfully!", show_alert=True)
        else:
            if is_hi:
                return await query.answer(
                    f"⏳ अभी पेमेंट प्राप्त नहीं हुआ है।\n\n"
                    f"कृपया सुनिश्चित करें कि आपने सटीक ₹{dyn_amount:.2f} का भुगतान किया है। "
                    f"बैंक नोटिफिकेशन आने में 5-20 सेकंड लगते हैं। कृपया कुछ पलों में पुनः प्रयास करें।",
                    show_alert=True
                )
            else:
                return await query.answer(
                    f"⏳ Payment not detected yet.\n\n"
                    f"Please ensure you paid exactly ₹{dyn_amount:.2f}. "
                    f"Bank notification emails usually arrive within 5-20 seconds. Please try again in a moment.",
                    show_alert=True
                )

    elif data.startswith("pass#upisubmit_"):
        parts = data.split("_")
        dur_key = parts[1]
        amount = float(parts[2])

        _pending_utr_users[user_id] = {
            'dur_key': dur_key,
            'amount': amount,
            'ts': time.time()
        }

        from database import parse_duration_to_seconds, format_duration_verbose
        dur_sec = parse_duration_to_seconds(dur_key, default_unit='d')
        dur_verbose = format_duration_verbose(dur_sec)

        prompt_text = (
            f"✍️ <b>Submit 12-Digit UTR Number</b>\n\n"
            f"<b>Plan:</b> {dur_verbose.title()} Unlimited Access\n"
            f"<b>Expected Amount:</b> <code>₹{amount:.2f}</code>\n\n"
            f"Please reply with your <b>12-digit UTR / Reference number</b> (e.g. <code>423456789012</code>) in this chat now.\n\n"
            f"<i>Our automated Gmail verification engine will verify the credit and activate your pass within seconds!</i>"
        )
        cancel_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("← Back", callback_data=f"pass#upibuy_{dur_key}_{amount}")]
        ])
        await query.message.edit_text(prompt_text, reply_markup=cancel_kb)

    elif data.startswith("pass#upirecheck_"):
        parts = data.split("_")
        utr = parts[1]
        dur_key = parts[2]
        expected_amount = float(parts[3])

        try:
            await query.answer("Re-checking bank notification emails...", show_alert=False)
        except Exception:
            pass

        if await db.is_utr_used(utr):
            return await query.answer("❌ This UTR has already been redeemed!", show_alert=True)

        from plugins.gmail_helper import verify_upi_payment_via_gmail
        res = await verify_upi_payment_via_gmail(utr, expected_amount)

        if res.get("success"):
            pending = _pending_utr_users.pop(user_id, {})
            order_id = pending.get('order_id') or f"UPI_{user_id}_{int(time.time())}"
            await db.mark_utr_used(utr, user_id, expected_amount, dur_key, user_name=u_name, order_id=order_id)
            _cancel_cooldown_reminders(user_id)
            b_id = getattr(getattr(client, "me", None), "id", None)
            b_uname = getattr(getattr(client, "me", None), "username", "")
            new_expiry = await db.grant_user_unlimited_pass(user_id, dur_key, user_name=u_name, bot_id=b_id, bot_username=b_uname, plan_key=dur_key, amount=expected_amount)
            
            from database import format_duration_verbose, parse_duration_to_seconds
            dur_sec = parse_duration_to_seconds(dur_key, default_unit='d')
            dur_verbose = format_duration_verbose(dur_sec)

            import datetime
            try:
                import pytz
                ist_tz = pytz.timezone('Asia/Kolkata')
                exp_dt = datetime.datetime.fromtimestamp(new_expiry, tz=ist_tz)
                exp_str = exp_dt.strftime('%d-%m-%Y %I:%M %p')
            except Exception:
                exp_str = datetime.datetime.fromtimestamp(new_expiry).strftime('%d-%m-%Y %I:%M %p')

            success_text = (
                f'<emoji id="5224607267797606837">🎉</emoji> <b>UPI Payment Verified Successfully!</b>\n\n'
                f"Hey <b>{user_name}</b>, your <b>{dur_verbose.title()} Unlimited Access Pass</b> is now ACTIVE!\n\n"
                f"<b>UTR / RRN:</b> <code>{utr}</code>\n"
                f"<b>Amount Verified:</b> ₹{expected_amount:.2f}\n"
                f"<b>Valid Until:</b> <code>{exp_str}</code>\n"
                f'<b>Status:</b> <emoji id="5411359377904934337">🟢</emoji> Unlimited Access (No Cooldown)\n\n'
                f"You can now access any batch and story links without cooldown. Enjoy!"
            )
            await query.message.edit_text(success_text)

            rl_cfg = await db.get_delivery_rate_limit_config()
            log_ch = rl_cfg.get('log_channel')
            from plugins.arya_logger import log_pass_purchased
            asyncio.create_task(log_pass_purchased(
                user_id=user_id,
                user_name=user_name,
                duration_str=dur_verbose.title(),
                amount=expected_amount,
                order_id=order_id,
                expiry_ts=new_expiry,
                log_channel=log_ch,
                gateway="UPI (Gmail Auto)"
            ))
        elif res.get("amount_mismatch"):
            m_amt = res.get("mismatched_amount")
            await query.message.edit_text(
                f"⚠️ <b>Payment Amount Mismatch</b>\n\n"
                f"Received amount is <b>₹{m_amt:.2f}</b>, but expected is <b>₹{expected_amount:.2f}</b>.\n\n"
                f"Please pay the exact plan amount to activate your pass, or contact support."
            )
        else:
            await query.answer("⏳ Still not detected. Please wait 10-15 seconds and tap again.", show_alert=True)

    elif data.startswith("pass#oxabuy_"):
        parts = data.split("_")
        dur_key = parts[1]
        amount_inr = float(parts[2])

        user_lang = await db.get_language(user_id)
        is_hi = bool(user_lang == 'hi')

        usd_check = round(float(amount_inr) / 92.0, 2)
        if usd_check < 0.50:
            return await query.answer(
                "⚠️ न्यूनतम क्रिप्टो पेमेंट $0.50 USD (~₹46) है। कृपया कोई बड़ा प्लान चुनें या UPI का उपयोग करें।" if is_hi else "⚠️ Minimum crypto payment is $0.50 USD (~₹46). Please choose a larger plan or use UPI.",
                show_alert=True
            )

        # Show instant loading message with custom animated emoji 5220046725493828505
        load_text = (
            '<emoji id="5220046725493828505">⏳</emoji> <b>क्रिप्टो इनवॉइस बनाई जा रही है, कृपया प्रतीक्षा करें...</b>'
            if is_hi else
            '<emoji id="5220046725493828505">⏳</emoji> <b>Generating Crypto Invoice, please wait...</b>'
        )
        try:
            await send_or_edit_with_custom_icons(
                client=client,
                chat_id=query.message.chat.id,
                text=load_text,
                inline_keyboard=[],
                message_id=query.message.id
            )
        except Exception:
            pass

        from plugins.oxapay_helper import create_oxapay_pass_order
        res = await create_oxapay_pass_order(user_id, user_name, dur_key, amount_inr)

        if not res.get("success"):
            err_text = res.get("error", "Failed to generate crypto invoice.")
            err_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 पुनः प्रयास करें" if is_hi else "🔄 Retry", callback_data=data)],
                [InlineKeyboardButton("← वापस" if is_hi else "← Back", callback_data="pass#method_crypto")]
            ])
            return await query.message.edit_text(
                f"❌ <b>{'क्रिप्टो इनवॉइस विफल' if is_hi else 'Crypto Invoice Failed'}</b>\n\n{err_text}",
                reply_markup=err_kb
            )

        pay_link = res["pay_link"]
        track_id = res["track_id"]
        amount_usd = res["amount_usd"]
        order_id = res["order_id"]
        dur_name = res["dur_name"]
        dur_name_hi = format_plan_name_friendly(dur_key, lang='hi')
        btn_pay_lbl = f"Pay Now ( ${amount_usd:.2f} )"
        btn_verify_lbl = "पेमेंट वेरीफाई करें" if is_hi else "Verify Payment"
        btn_cancel_lbl = "अपना ऑर्डर कैंसिल करें" if is_hi else "Cancel your order"
        btn_back_lbl = "← वापस" if is_hi else "← Back"

        # Save order document to MongoDB
        try:
            await db.pass_orders.insert_one({
                'order_id': order_id,
                'track_id': str(track_id or order_id),
                'user_id': user_id,
                'user_name': user_name,
                'plan': dur_key,
                'duration_key': dur_key,
                'amount': amount_inr,
                'amount_usd': amount_usd,
                'gateway': 'Crypto ( Oxapay )',
                'status': 'PENDING',
                'created_at': time.time(),
                'expires_at': time.time() + 600
            })
        except Exception:
            pass

        if is_hi:
            inv_text = (
                f'<emoji id="5283232570660634549">🌐</emoji> <b>क्रिप्टो पेमेंट इनवॉइस — अनलिमिटेड डिलीवरी पास</b>\n\n'
                f"• <b>प्लान:</b> {dur_name_hi} अनलिमिटेड एक्सेस\n"
                f"• <b>राशि:</b> <code>${amount_usd:.2f} USD</code> (~₹{amount_inr:.0f})\n"
                f"• <b>ऑर्डर ID:</b> <code>{order_id}</code>\n\n"
                f"<blockquote>नीचे दिए गए <b>Pay Now</b> बटन पर टैप करके OxaPay के ज़रिए अपनी पसंदीदा क्रिप्टोकरेंसी (USDT, BTC, ETH, TRX, BNB, LTC, SOL आदि) से भुगतान करें। पेमेंट भेजने के बाद <b>Verify Payment</b> पर टैप करें!</blockquote>"
            )
        else:
            inv_text = (
                f'<emoji id="5283232570660634549">🌐</emoji> <b>Crypto Payment Invoice — Unlimited Delivery Pass</b>\n\n'
                f"• <b>Plan:</b> {dur_name} Unlimited Access\n"
                f"• <b>Amount:</b> <code>${amount_usd:.2f} USD</code> (~₹{amount_inr:.0f})\n"
                f"• <b>Order ID:</b> <code>{order_id}</code>\n\n"
                f"<blockquote>Tap the button <b>Pay Now</b> below to pay using your preferred cryptocurrency (USDT, BTC, ETH, TRX, BNB, LTC, SOL, etc.) via OxaPay. After sending crypto, tap <b>Verify Payment</b> to activate!</blockquote>"
            )

        inv_api_kb = [
            [{"text": btn_pay_lbl, "url": pay_link, "icon_custom_emoji_id": "5807527002374151568"}],
            [{"text": btn_verify_lbl, "callback_data": f"pass#oxaverify_{track_id}_{dur_key}_{amount_inr}_{order_id}", "icon_custom_emoji_id": "5807492110059838726"}],
            [{"text": btn_cancel_lbl, "callback_data": f"pass#cancel_{order_id}", "icon_custom_emoji_id": "5774077015388852135"}],
            [{"text": btn_back_lbl, "callback_data": "pass#method_crypto"}]
        ]
        inv_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(btn_pay_lbl, url=pay_link)],
            [InlineKeyboardButton(btn_verify_lbl, callback_data=f"pass#oxaverify_{track_id}_{dur_key}_{amount_inr}_{order_id}")],
            [InlineKeyboardButton(btn_cancel_lbl, callback_data=f"pass#cancel_{order_id}")],
            [InlineKeyboardButton(btn_back_lbl, callback_data="pass#method_crypto")]
        ])
        sent_ok = await send_or_edit_with_custom_icons(
            client=client,
            chat_id=query.message.chat.id,
            text=inv_text,
            inline_keyboard=inv_api_kb,
            message_id=query.message.id
        )
        if not sent_ok:
            await query.message.edit_text(inv_text, reply_markup=inv_kb)

        # Schedule automatic reminder (max 2 times under 10 minutes) if OxaPay crypto invoice not completed
        asyncio.create_task(schedule_pass_payment_reminder(
            client=client,
            user_id=user_id,
            order_id=order_id,
            gateway_name="Pay Via Crypto (OxaPay)",
            amount_str=f"${amount_usd:.2f} USD (~₹{amount_inr:.0f})",
            dur_verbose=dur_name,
            pay_url=pay_link,
            dur_key=dur_key,
            dyn_amount=amount_inr
        ))

    elif data.startswith("pass#oxaverify_"):
        parts = data.split("_")
        track_id = parts[1]
        dur_key = parts[2]
        amount_inr = float(parts[3])
        order_id = parts[4] if len(parts) > 4 else f"PASS-{user_id}-1D-1"

        user_lang = await db.get_language(user_id)
        is_hi = bool(user_lang == 'hi')

        try:
            await query.answer("OxaPay से ब्लॉकचेन पेमेंट चेक किया जा रहा है..." if is_hi else "Checking blockchain payment status with OxaPay...", show_alert=False)
        except Exception:
            pass

        from plugins.oxapay_helper import verify_oxapay_pass_order
        v_res = await verify_oxapay_pass_order(track_id)

        if v_res.get("paid"):
            _cancel_cooldown_reminders(user_id)
            try:
                await db.pass_orders.update_one(
                    {"$or": [{"order_id": order_id}, {"track_id": track_id}]},
                    {"$set": {"status": "PAID", "paid_at": time.time()}}
                )
            except Exception:
                pass
            b_id = getattr(getattr(client, "me", None), "id", None)
            b_uname = getattr(getattr(client, "me", None), "username", "")
            new_expiry = await db.grant_user_unlimited_pass(user_id, dur_key, user_name=user_name, bot_id=b_id, bot_username=b_uname, plan_key=dur_key)
            from database import format_duration_verbose, parse_duration_to_seconds
            dur_sec = parse_duration_to_seconds(dur_key, default_unit='d')
            dur_verbose = format_duration_verbose(dur_sec)

            import datetime
            try:
                import pytz
                ist_tz = pytz.timezone('Asia/Kolkata')
                exp_dt = datetime.datetime.fromtimestamp(new_expiry, tz=ist_tz)
                exp_str = exp_dt.strftime('%d-%m-%Y %I:%M %p')
            except Exception:
                exp_str = datetime.datetime.fromtimestamp(new_expiry).strftime('%d-%m-%Y %I:%M %p')

            if is_hi:
                plan_name = format_plan_name_friendly(dur_key, lang='hi')
                success_text = (
                    f'<emoji id="5224607267797606837">🎉</emoji> <b>क्रिप्टो पेमेंट सफलतापूर्वक वेरीफाई हो गया!</b>\n\n'
                    f"नमस्ते <b>{user_name}</b>, आपका <b>{plan_name} अनलिमिटेड एक्सेस पास</b> अब सक्रिय है!\n\n"
                    f"• <b>ट्रैक ID:</b> <code>{track_id}</code>\n"
                    f"• <b>वैधता:</b> <code>{exp_str}</code>\n"
                    f'• <b>स्टेटस:</b> <emoji id="5411359377904934337">🟢</emoji> अनलिमिटेड एक्सेस (कोई कूलडाउन नहीं)\n\n'
                    f"अब आप बिना किसी लिमिट के सारी फाइल्स तुरंत एक्सेस कर सकते हैं!"
                )
            else:
                plan_name = format_plan_name_friendly(dur_key, lang='en')
                success_text = (
                    f'<emoji id="5224607267797606837">🎉</emoji> <b>Crypto Payment Verified Successfully!</b>\n\n'
                    f"Hey <b>{user_name}</b>, your <b>{plan_name} Unlimited Access Pass</b> is now ACTIVE!\n\n"
                    f"• <b>Track ID:</b> <code>{track_id}</code>\n"
                    f"• <b>Valid Until:</b> <code>{exp_str}</code>\n"
                    f'• <b>Status:</b> <emoji id="5411359377904934337">🟢</emoji> Unlimited Access (No Cooldown)\n\n'
                    f"You can now access any batch and story links without cooldown. Enjoy!"
                )
            await query.message.edit_text(success_text)

            rl_cfg = await db.get_delivery_rate_limit_config()
            log_ch = rl_cfg.get('log_channel')
            from plugins.arya_logger import log_pass_purchased
            asyncio.create_task(log_pass_purchased(
                user_id=user_id,
                user_name=user_name,
                duration_str=dur_verbose.title(),
                amount=amount_inr,
                order_id=order_id,
                expiry_ts=new_expiry,
                log_channel=log_ch,
                gateway="Crypto (OxaPay)"
            ))
        else:
            await query.answer(
                "⏳ Payment Pending: OxaPay has not confirmed the transaction on the blockchain yet. "
                "If you recently sent the transaction, please wait 1-2 minutes for network confirmations and tap Verify again.",
                show_alert=True
            )

    elif data.startswith("pass#cfbuy_") or data.startswith("pass#buy_"):
        parts = data.split("_")
        dur_key = parts[1]
        amount = float(parts[2])

        user_lang = await db.get_language(user_id)
        is_hi = bool(user_lang == 'hi')

        # Show instant loading message with custom animated emoji 5220046725493828505
        load_text = (
            '<emoji id="5220046725493828505">⏳</emoji> <b>पेमेंट इनवॉइस बनाई जा रही है, कृपया प्रतीक्षा करें...</b>'
            if is_hi else
            '<emoji id="5220046725493828505">⏳</emoji> <b>Creating Payment Invoice, please wait...</b>'
        )
        try:
            await send_or_edit_with_custom_icons(
                client=client,
                chat_id=query.message.chat.id,
                text=load_text,
                inline_keyboard=[],
                message_id=query.message.id
            )
        except Exception:
            pass

        from database import parse_duration_to_seconds, format_duration_verbose
        dur_sec = parse_duration_to_seconds(dur_key, default_unit='d')
        dur_verbose = format_duration_verbose(dur_sec)

        from plugins.cashfree_helper import create_cashfree_pass_order
        b_id = getattr(getattr(client, "me", None), "id", None)
        b_uname = getattr(getattr(client, "me", None), "username", "")
        res = await create_cashfree_pass_order(user_id, user_name, dur_key, amount, bot_id=b_id, bot_username=b_uname)

        if not res.get("success"):
            err_text = res.get('error', 'Failed to generate payment link')
            err_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Retry", callback_data=data)],
                [InlineKeyboardButton("← Back", callback_data="pass#method_cashfree")]
            ])
            return await query.message.edit_text(
                f"❌ <b>Payment Order Failed</b>\n\n{err_text}",
                reply_markup=err_kb
            )

        order_id = res["order_id"]
        checkout_pay_link = res["checkout_pay_link"]
        p_label = int(amount) if float(amount).is_integer() else amount
        user_lang = await db.get_language(user_id)
        is_hi = bool(user_lang == 'hi')

        if is_hi:
            inv_text = (
                f'<emoji id="5920332557466997677">⚡</emoji> <b>पेमेंट इनवॉइस — अनलिमिटेड डिलीवरी पास</b>\n\n'
                f"<b>नाम:</b> {user_name}\n"
                f"<b>यूजर आईडी:</b> <code>{user_id}</code>\n"
                f"<b>प्लान:</b> {dur_verbose.title()} अनलिमिटेड डिलीवरी पास\n"
                f"<b>राशि:</b> ₹{amount:.2f}\n"
                f"<b>ऑर्डर आईडी:</b> <code>{order_id}</code>\n\n"
                f"<blockquote>UPI ( Paytm GPay, PhonePe ), Cards ( Visa Rupay and MasterCard ) या NetBanking से पेमेंट पूरा करने के लिए नीचे दिए गए <b>Pay Now</b> बटन पर टैप करें। पेमेंट करने के बाद, एक्टिवेट करने के लिए <b>Verify Payment</b> पर टैप करें!</blockquote>"
            )
            btn_pay_lbl = f"Pay Now ( ₹{p_label} )"
            btn_verify_lbl = "Verify Payment"
            btn_cancel_lbl = "Cancel your order"
            btn_back_lbl = "← Back"
        else:
            inv_text = (
                f'<emoji id="5920332557466997677">⚡</emoji> <b>Payment Invoice — Unlimited Delivery Pass</b>\n\n'
                f"<b>Name:</b> {user_name}\n"
                f"<b>User ID:</b> <code>{user_id}</code>\n"
                f"<b>Plan:</b> {dur_verbose.title()} Unlimited Delivery Pass\n"
                f"<b>Amount:</b> ₹{amount:.2f}\n"
                f"<b>Order ID:</b> <code>{order_id}</code>\n\n"
                f"<blockquote>Tap the button <b>Pay Now</b> below to complete payment via UPI ( Paytm GPay, PhonePe ), Cards ( Visa Rupay and MasterCard ) , or NetBanking. After payment, tap <b>Verify Payment</b> to activate!</blockquote>"
            )
            btn_pay_lbl = f"Pay Now ( ₹{p_label} )"
            btn_verify_lbl = "Verify Payment"
            btn_cancel_lbl = "Cancel your order"
            btn_back_lbl = "← Back"

        inv_api_kb = [
            [{"text": btn_pay_lbl, "url": checkout_pay_link, "icon_custom_emoji_id": "5807527002374151568"}],
            [{"text": btn_verify_lbl, "callback_data": f"pass#verify_{order_id}_{dur_key}_{amount}", "icon_custom_emoji_id": "5807492110059838726"}],
            [{"text": btn_cancel_lbl, "callback_data": f"pass#cancel_{order_id}", "icon_custom_emoji_id": "5774077015388852135"}],
            [{"text": btn_back_lbl, "callback_data": "pass#unlock_menu"}]
        ]
        inv_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(btn_pay_lbl, url=checkout_pay_link)],
            [InlineKeyboardButton(btn_verify_lbl, callback_data=f"pass#verify_{order_id}_{dur_key}_{amount}")],
            [InlineKeyboardButton(btn_cancel_lbl, callback_data=f"pass#cancel_{order_id}")],
            [InlineKeyboardButton(btn_back_lbl, callback_data="pass#unlock_menu")]
        ])
        sent_ok = await send_or_edit_with_custom_icons(
            client=client,
            chat_id=query.message.chat.id,
            text=inv_text,
            inline_keyboard=inv_api_kb,
            message_id=query.message.id
        )
        if not sent_ok:
            await query.message.edit_text(inv_text, reply_markup=inv_kb)

        # 1. Launch real-time background auto-verifier (polls Cashfree every 5s & auto-activates on payment)
        asyncio.create_task(start_pass_cashfree_auto_verifier(
            client=client,
            user_id=user_id,
            user_name=user_name,
            order_id=order_id,
            dur_key=dur_key,
            amount=amount,
            dur_verbose=dur_verbose,
            invoice_msg_id=query.message.id
        ))

        # 2. Schedule automatic reminder (max 2 times under 10 minutes) if Cashfree invoice not completed
        asyncio.create_task(schedule_pass_payment_reminder(
            client=client,
            user_id=user_id,
            order_id=order_id,
            gateway_name="⚡ Cashfree Payments",
            amount_str=f"₹{amount:.2f}",
            dur_verbose=dur_verbose,
            pay_url=checkout_pay_link,
            dur_key=dur_key,
            dyn_amount=amount
        ))

    elif data.startswith("pass#verify_"):
        parts = data.split("_")
        order_id = "_".join(parts[1:-2])
        dur_key = parts[-2]
        amount = float(parts[-1])

        try:
            await query.answer("Verifying payment with gateway...", show_alert=False)
        except Exception:
            pass

        from database import parse_duration_to_seconds, format_duration_verbose
        dur_sec = parse_duration_to_seconds(dur_key, default_unit='d')
        dur_verbose = format_duration_verbose(dur_sec)

        from plugins.cashfree_helper import verify_cashfree_pass_order
        v_res = await verify_cashfree_pass_order(order_id)

        if v_res.get("is_paid"):
            claimed = await db.mark_pass_order_paid_atomic(order_id, v_res)
            if claimed:
                # First-time claim -> activate pass duration
                _cancel_cooldown_reminders(user_id)
                b_id = getattr(getattr(client, "me", None), "id", None)
                b_uname = getattr(getattr(client, "me", None), "username", "")
                new_expiry = await db.grant_user_unlimited_pass(user_id, dur_key, user_name=user_name, bot_id=b_id, bot_username=b_uname, plan_key=dur_key, amount=amount)
                
                import datetime
                try:
                    import pytz
                    ist_tz = pytz.timezone('Asia/Kolkata')
                    exp_dt = datetime.datetime.fromtimestamp(new_expiry, tz=ist_tz)
                    exp_str = exp_dt.strftime('%d-%m-%Y %I:%M %p')
                except Exception:
                    exp_str = datetime.datetime.fromtimestamp(new_expiry).strftime('%d-%m-%Y %I:%M %p')

                success_text = (
                    f'<emoji id="5224607267797606837">🎉</emoji> <b>Unlimited Pass Activated Successfully!</b>\n\n'
                    f"Hey <b>{user_name}</b>, your <b>{dur_verbose.title()} Unlimited Access Pass</b> is now ACTIVE!\n\n"
                    f"<b>Order ID:</b> <code>{order_id}</code>\n"
                    f"<b>Valid Until:</b> <code>{exp_str}</code>\n"
                    f'<b>Status:</b> <emoji id="5411359377904934337">🟢</emoji> Unlimited Access (No Cooldown)\n\n'
                    f"You can now access any batch and story links without cooldown. Enjoy!"
                )
                await query.message.edit_text(success_text)

                # Log to dedicated pass log channel in Quoteblock format
                rl_cfg = await db.get_delivery_rate_limit_config()
                log_ch = rl_cfg.get('log_channel')
                from plugins.arya_logger import log_pass_purchased
                asyncio.create_task(log_pass_purchased(
                    user_id=user_id,
                    user_name=user_name,
                    duration_str=dur_verbose.title(),
                    amount=amount,
                    order_id=order_id,
                    expiry_ts=new_expiry,
                    log_channel=log_ch,
                    gateway="Cashfree PG"
                ))
            else:
                # Already claimed (e.g. by auto-verifier or earlier tap). DO NOT ADD EXTRA DURATION!
                cur_pass = await db.get_user_unlimited_pass(user_id)
                new_expiry = cur_pass.get("expires_at", time.time())
                import datetime
                try:
                    import pytz
                    ist_tz = pytz.timezone('Asia/Kolkata')
                    exp_dt = datetime.datetime.fromtimestamp(new_expiry, tz=ist_tz)
                    exp_str = exp_dt.strftime('%d-%m-%Y %I:%M %p')
                except Exception:
                    exp_str = datetime.datetime.fromtimestamp(new_expiry).strftime('%d-%m-%Y %I:%M %p')

                already_text = (
                    f'<emoji id="5224607267797606837">✅</emoji> <b>Unlimited Pass is Already Active!</b>\n\n'
                    f"Hey <b>{user_name}</b>, your <b>{dur_verbose.title()} Unlimited Access Pass</b> is already active.\n\n"
                    f"<b>Valid Until:</b> <code>{exp_str}</code>\n"
                    f'<b>Status:</b> <emoji id="5411359377904934337">🟢</emoji> Unlimited Access Active\n\n'
                    f"Enjoy unlimited access with zero cooldown!"
                )
                await query.message.edit_text(already_text)
        else:
            try:
                await query.answer(
                    "⚠️ Payment Not Received: If you have made the payment, please wait 5-10 seconds for gateway confirmation.",
                    show_alert=True
                )
            except Exception:
                pass

    elif data.startswith("pass#cancel_"):
        parts = data.split("_")
        cancel_order_id = "_".join(parts[1:])
        if cancel_order_id:
            await db.pass_orders.update_one({"order_id": cancel_order_id}, {"$set": {"status": "CANCELLED"}})
        user_lang = await db.get_language(user_id)
        is_hi = bool(user_lang == 'hi')
        await query.message.edit_text("पेमेंट इनवॉइस कैंसिल कर दिया गया।" if is_hi else "Payment invoice cancelled.")


    elif data == "pass#back":
        # Return to rate limit message
        rl_cfg = await db.get_delivery_rate_limit_config()
        from database import format_duration_verbose, format_duration_friendly
        max_limit = int(rl_cfg.get('max_limit', 5))
        window_seconds = int(rl_cfg.get('window_seconds', int(rl_cfg.get('window_hours', 12)) * 3600))
        hits = await db.get_user_delivery_hits(user_id, window_seconds)
        user_lang = await db.get_language(user_id)
        is_hi = bool(user_lang == 'hi')
        
        import time as _t
        rem_sec = window_seconds
        if hits:
            oldest_hit = hits[0]['timestamp']
            rem_sec = max(1, int((oldest_hit + window_seconds) - _t.time()))
        if rem_sec >= 3600:
            rem_hours = rem_sec // 3600
            rem_mins = (rem_sec % 3600) // 60
            rem_time_str = f"{rem_hours:02d}h {rem_mins:02d}m" if not is_hi else f"{rem_hours} घंटे {rem_mins} मिनट"
        elif rem_sec >= 60:
            rem_mins = rem_sec // 60
            rem_secs = rem_sec % 60
            rem_time_str = f"{rem_mins:02d}m {rem_secs:02d}s" if not is_hi else f"{rem_mins} मिनट {rem_secs} सेकंड"
        else:
            rem_time_str = f"{rem_sec:02d}s" if not is_hi else f"{rem_sec} सेकंड"

        win_verbose = format_duration_verbose(window_seconds)

        if is_hi:
            limit_text = (
                f'<emoji id="6215133834149629990">⏳</emoji> <b>रेट लिमिट पूरी हो गई है</b>\n\n'
                f'आपने पिछले <b>{win_verbose}</b> में <b>{len(hits)} / {max_limit} लिंक्स</b> एक्सेस कर लिए हैं। <emoji id="6266794310671275367">🎬</emoji>\n\n'
                f'सभी यूजर्स के लिए लिमिट <b>{max_limit} लिंक्स प्रति {win_verbose}</b> निर्धारित है।\n\n'
                f'<emoji id="6217487596486922033">⏰</emoji> <b>कूलडाउन रीसेट होने में समय:</b> <code>{rem_time_str}</code>\n\n'
                f'कृपया बाद में प्रयास करें या नीचे से अनलिमिटेड एक्सेस अनलॉक करें! <emoji id="6023566962624306038">👇</emoji>'
            )
            limit_api_kb = [
                [
                    {
                        "text": "अनलिमिटेड एक्सेस अनलॉक करें",
                        "callback_data": "pass#unlock_menu",
                        "icon_custom_emoji_id": "6030443364178992166"
                    }
                ]
            ]
            unlock_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔓 अनलिमिटेड एक्सेस अनलॉक करें", callback_data="pass#unlock_menu")
            ]])
        else:
            limit_text = (
                f'<emoji id="6215133834149629990">⏳</emoji> <b>Rate Limit Reached</b>\n\n'
                f'You have already accessed <b>{len(hits)} / {max_limit} links</b> in the past <b>{win_verbose}</b>. <emoji id="6266794310671275367">🎬</emoji>\n\n'
                f'The limit is <b>{max_limit} links per {win_verbose}</b> to ensure fair usage for everyone.\n\n'
                f'<emoji id="6217487596486922033">⏳</emoji> <b>Cooldown resets in:</b> <code>{rem_time_str}</code>\n\n'
                f'Please try again later or unlock unlimited access below! <emoji id="6023566962624306038">👇</emoji>'
            )
            limit_api_kb = [
                [
                    {
                        "text": "Unlock Unlimited Access",
                        "callback_data": "pass#unlock_menu",
                        "icon_custom_emoji_id": "6030443364178992166"
                    }
                ]
            ]
            unlock_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔓 Unlock Unlimited Access", callback_data="pass#unlock_menu")
            ]])
        sent_ok = await send_or_edit_with_custom_icons(
            client=client,
            chat_id=query.message.chat.id,
            text=limit_text,
            inline_keyboard=limit_api_kb,
            message_id=query.message.id
        )
        if not sent_ok:
            unlock_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔓 Unlock Access Via Payment", callback_data="pass#unlock_menu")
            ]])
            await query.message.edit_text(limit_text, reply_markup=unlock_kb)


# 
# Registration & Startup
# 


async def _process_share_broadcast(client, message):
    from config import Config
    if message.from_user.id not in Config.OWNER_IDS:
        return
    
    bot_id = str(client.me.id) if client.me else None
    if not bot_id:
        return
        
    b_msg = message.reply_to_message
    if not b_msg:
        await message.reply_text("Reply to a message to broadcast.")
        return
        
    sts = await message.reply_text("Broadcasting your messages via Delivery Bot...")
    
    users = await db.get_share_bot_users(bot_id)
    total_users = len(users)
    
    import time, datetime
    from pyrogram.errors import FloodWait, UserIsBlocked, InputUserDeactivated
    
    start_time = time.time()
    done = 0
    blocked = 0
    deleted = 0
    failed = 0
    success = 0
    
    async def copy_msg(user_id):
        try:
            await b_msg.copy(chat_id=user_id, reply_markup=b_msg.reply_markup)
            return True, "Success"
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            return await copy_msg(user_id)
        except InputUserDeactivated:
            return False, "Deleted"
        except UserIsBlocked:
            return False, "Blocked"
        except Exception:
            return False, "Error"

    for u_id in users:
        pti, sh = await copy_msg(int(u_id))
        if pti:
            success += 1
            await asyncio.sleep(0.5)
        else:
            if sh == "Blocked":
                blocked += 1
                await db.set_share_bot_user_status(bot_id, int(u_id), blocked=True)
            elif sh == "Deleted":
                deleted += 1
                await db.set_share_bot_user_status(bot_id, int(u_id), deactivated=True)
            else:
                failed += 1
            
        done += 1
        if done % 20 == 0:
            try:
                await sts.edit(f"Delivery Bot Broadcast:\n\nTotal Users {total_users}\nCompleted: {done} / {total_users}\nSuccess: {success}\nBlocked: {blocked}\nDeleted: {deleted}")
            except: pass
            
    time_taken = datetime.timedelta(seconds=int(time.time()-start_time))
    await sts.edit(f"Delivery Bot Broadcast Completed in {time_taken}.\n\nTotal Users {total_users}\nCompleted: {done} / {total_users}\nSuccess: {success}\nBlocked: {blocked}\nDeleted: {deleted}")


def register_share_handlers(app: Client):
    """Register all handlers on a started Client instance."""
    from plugins.banned import ban_interceptor
    app.add_handler(MessageHandler(ban_interceptor, filters.all), group=-999)
    app.add_handler(CallbackQueryHandler(ban_interceptor, filters.all), group=-999)
    
    app.add_handler(MessageHandler(
        _process_share_broadcast,
        filters.private & filters.command("broadcast") & filters.reply
    ))
    # Auto-approve join requests for JR channels so users get instant access
    app.add_handler(ChatJoinRequestHandler(_fsub_record_jr))
    async def safe_process_start(client, message):
        try:
            await _process_start(client, message)
        except BaseException as e:
            import traceback
            import datetime
            with open("bot_crash.log", "a", encoding="utf-8") as f:
                f.write(f"\n[{datetime.datetime.now()}] Exception in _process_start:\n")
                f.write(traceback.format_exc() + "\n")
            raise

    app.add_handler(MessageHandler(
        safe_process_start,
        filters.private & filters.command("start")
    ))

    async def _cmd_about(client, message):
        bot_id = str(client.me.id) if client.me else None
        await _send_about(client, message, bot_id=bot_id, edit=False)
        
    async def _cmd_help(client, message):
        bot_id = str(client.me.id) if client.me else None
        await _send_help(client, message, bot_id)
        
    async def _cmd_premium(client, message):
        await _send_premium_menu(client, message, edit=False)
        
    async def _cmd_support(client, message):
        txt = "<b>»  " + _sc("Support") + "</b>\n\n<i>" + _sc("If you need help or have any questions, join our support group.") + "</i>"
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("»  " + _sc("Support Group"), url=SUPPORT_LINK)]])
        await message.reply_text(txt, reply_markup=markup, disable_web_page_preview=True)

    async def _cmd_updates(client, message):
        txt = "<b>»  " + _sc("Updates") + "</b>\n\n<i>" + _sc("Stay updated with our latest news and announcements.") + "</i>"
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("»  " + _sc("Update Channel"), url=UPDATE_LINK)]])
        await message.reply_text(txt, reply_markup=markup, disable_web_page_preview=True)

    app.add_handler(MessageHandler(_cmd_about, filters.private & filters.command("about")))
    app.add_handler(MessageHandler(_cmd_help, filters.private & filters.command("help")))
    app.add_handler(MessageHandler(_cmd_premium, filters.private & filters.command(["norestrictions", "premium"])))
    app.add_handler(MessageHandler(_cmd_support, filters.private & filters.command("support")))
    app.add_handler(MessageHandler(_cmd_updates, filters.private & filters.command("updates")))

    app.add_handler(CallbackQueryHandler(
        _process_delivery_button,
        filters.regex(r'^sbd#')
    ))
    app.add_handler(CallbackQueryHandler(
        _process_delivery_cancel,
        filters.regex(r'^cancel_dl_')
    ))
    app.add_handler(CallbackQueryHandler(
        _process_fsub_check,
        filters.regex(r'^fsub_chk_')
    ))
    async def safe_process_pass(client, query):
        try:
            await _process_pass_callback(client, query)
        except Exception as e:
            logger.exception(f"Exception in _process_pass_callback: {e}")
            try:
                await query.answer(f"⚠️ Error: {e}", show_alert=True)
            except Exception:
                pass

    app.add_handler(CallbackQueryHandler(
        safe_process_pass,
        filters.regex(r'^pass#')
    ))
    app.add_handler(MessageHandler(
        _handle_share_bot_utr_message,
        filters.private & filters.text & ~filters.command(["start", "help", "about", "support", "updates", "broadcast", "premium", "norestrictions"])
    ), group=10)

    # Add AI Enhancer support to Delivery Bot seamlessly
    try:
        from plugins.enhancer import enhance_offer_handler, enhance_execute_cb
        app.add_handler(MessageHandler(
            enhance_offer_handler,
            filters.private & (filters.photo | filters.document) & ~filters.forwarded
        ))
        app.add_handler(CallbackQueryHandler(
            enhance_execute_cb,
            filters.regex(r'^enh#do$')
        ))
    except ImportError: pass
    logger.info(f"Handlers registered on {app.name}")


# ── 1-Hour Expiry Reminder Templates ──────────────────────────────────────────
PASS_EXPIRY_TEMPLATES_HI = [
    # 1. Suspense / Cliffhanger
    (
        '<emoji id="5386367538735104399">⏳</emoji> <b>कहानी के क्लाइमेक्स पर सन्नाटा नहीं चाहिए!</b>\n\n'
        'नमस्ते <b>{u_name}</b>, आपका {plan_name} <b>{exp_str}</b> (लगभग <b>{rem_mins} मिनट</b> बाद) समाप्त होने वाला है।\n\n'
        '• <b>प्लान:</b> {plan_name}\n'
        '• <b>बचत:</b> <emoji id="5233326571099534068">💸</emoji> ₹{saved_amount}\n\n'
        '<blockquote><emoji id="5201817814842755281">🫢</emoji> सोचो कहानी का सबसे बड़ा राज़ खुलने ही वाला हो और बीच में कूलडाउन लग जाए! अपनी पसंदीदा ऑडियो कहानियों को बिना किसी रुकावट लगातार सुनते रहने के लिए अभी रिन्यू करें।</blockquote>'
    ),
    # 2. Comedy / Fun
    (
        '<emoji id="5386367538735104399">⏳</emoji> <b>घड़ी की टिक-टिक शुरू... सिर्फ 1 घंटा बाकी!</b>\n\n'
        'अरे <b>{u_name}</b>, आपका {plan_name} आज <b>{exp_str}</b> (~<b>{rem_mins} मिनट</b> में) समाप्त होने वाला है!\n\n'
        '• <b>प्लान:</b> {plan_name}\n'
        '• <b>कुल बचत:</b> <emoji id="5409048419211682843">💵</emoji> ₹{saved_amount}\n\n'
        '<blockquote><emoji id="5361761791355398330">😜</emoji> फिर मत कहना कि ट्विस्ट के टाइम पर ब्रेक लग गया! नो कूलडाउन, नॉन-स्टॉप स्टोरी सुनने का मज़ा जारी रखने के लिए तुरंत पास रिन्यू करो!</blockquote>'
    ),
    # 3. Sarcasm / Witty
    (
        '<emoji id="5386367538735104399">⏳</emoji> <b>सबर का फल मीठा होता है, पर कहानियों में नहीं!</b>\n\n'
        'सुनो <b>{u_name}</b>, आपका {plan_name} <b>{exp_str}</b> (सिर्फ <b>{rem_mins} मिनट</b> में) एक्सपायर हो रहा है।\n\n'
        '• <b>प्लान:</b> {plan_name}\n'
        '• <b>सेविंग्स:</b> <emoji id="5244837092042750681">📈</emoji> ₹{saved_amount}\n\n'
        '<blockquote><emoji id="5445091140514620351">😏</emoji> इंतज़ार किसे पसंद है जब कहानी का अगला एपिसोड तुरंत सुनना हो? बिना कूलडाउन अपनी धुन में सुनते रहने के लिए पास रीचार्ज कर लो!</blockquote>'
    ),
    # 4. Savage / Drama
    (
        '<emoji id="5395695537687123235">🚨</emoji> <b>रहस्यमयी कहानियों का VIP सफर रुकने वाला है!</b>\n\n'
        '<b>{u_name}</b>, आपका {plan_name} <b>{exp_str}</b> (~<b>{rem_mins} मिनट</b> बाद) समाप्त हो जाएगा।\n\n'
        '• <b>प्लान:</b> {plan_name}\n'
        '• <b>सुरक्षित बचत:</b> <emoji id="5280818098960611598">🤑</emoji> ₹{saved_amount}\n\n'
        '<blockquote><emoji id="5208878706717636743">🗿</emoji> VIP लाइन से सीधे कूलडाउन कतार में जाने का कोई इरादा नहीं होना चाहिए! समय रहते रिन्यू करें और नॉन-स्टॉप सुनें।</blockquote>'
    ),
    # 5. Marketing / Direct
    (
        '<emoji id="5424972470023104089">🔥</emoji> <b>1 घंटा बाकी — अनलिमिटेड कहानियों का पास रिन्यू करें!</b>\n\n'
        'नमस्ते <b>{u_name}</b>, आपका {plan_name} आज <b>{exp_str}</b> को समाप्त हो रहा है।\n\n'
        '• <b>प्लान:</b> {plan_name}\n'
        '• <b>स्मार्ट बचत:</b> <emoji id="5341498088408234504">💯</emoji> ₹{saved_amount}\n\n'
        '<blockquote><emoji id="5289650686319929628">😎</emoji> नो लिमिट्स, नो वेटिंग — अपनी सभी ऑडियो स्टोरीज़ को बिना रुके सुनते रहने के लिए अभी रिन्यू करें।</blockquote>'
    )
]

PASS_EXPIRY_TEMPLATES_EN = [
    # 1. Suspense / Cliffhanger
    (
        '<emoji id="5386367538735104399">⏳</emoji> <b>Don\'t let the cliffhanger leave you hanging!</b>\n\n'
        'Hey <b>{u_name}</b>, your {plan_name} will expire at <b>{exp_str}</b> (in ~<b>{rem_mins} minutes</b>).\n\n'
        '• <b>Plan:</b> {plan_name}\n'
        '• <b>Savings:</b> <emoji id="5233326571099534068">💸</emoji> ₹{saved_amount}\n\n'
        '<blockquote><emoji id="5201817814842755281">🫢</emoji> Imagine the biggest mystery is about to unfold and you hit a cooldown! Renew now to keep listening to your favorite audio stories non-stop.</blockquote>'
    ),
    # 2. Comedy / Fun
    (
        '<emoji id="5386367538735104399">⏳</emoji> <b>Tick-Tock! Only 1 Hour Left on Your Pass!</b>\n\n'
        'Hey <b>{u_name}</b>, your {plan_name} is about to say goodbye at <b>{exp_str}</b> (~<b>{rem_mins} mins</b> left).\n\n'
        '• <b>Plan:</b> {plan_name}\n'
        '• <b>Total Saved:</b> <emoji id="5409048419211682843">💵</emoji> ₹{saved_amount}\n\n'
        '<blockquote><emoji id="5361761791355398330">😜</emoji> Don\'t let cooldowns ruin your storytelling groove! Renew your pass now for uninterrupted listening joy.</blockquote>'
    ),
    # 3. Sarcasm / Witty
    (
        '<emoji id="5386367538735104399">⏳</emoji> <b>Patience is a virtue... but not in stories!</b>\n\n'
        'Hey <b>{u_name}</b>, your {plan_name} expires at <b>{exp_str}</b> (in ~<b>{rem_mins} minutes</b>).\n\n'
        '• <b>Plan:</b> {plan_name}\n'
        '• <b>Money Saved:</b> <emoji id="5244837092042750681">📈</emoji> ₹{saved_amount}\n\n'
        '<blockquote><emoji id="5445091140514620351">😏</emoji> Who wants to wait between episodes when you can binge seamlessly? Keep zero-cooldown access by renewing today!</blockquote>'
    ),
    # 4. Savage / Drama
    (
        '<emoji id="5395695537687123235">🚨</emoji> <b>Your VIP story journey pauses in 1 hour!</b>\n\n'
        'Hey <b>{u_name}</b>, your {plan_name} will end at <b>{exp_str}</b>.\n\n'
        '• <b>Plan:</b> {plan_name}\n'
        '• <b>Total Saved:</b> <emoji id="5280818098960611598">🤑</emoji> ₹{saved_amount}\n\n'
        '<blockquote><emoji id="5208878706717636743">🗿</emoji> Going back to cooldown timers after enjoying VIP privilege? Renew your pass now and stay in power!</blockquote>'
    ),
    # 5. Marketing / Direct
    (
        '<emoji id="5424972470023104089">🔥</emoji> <b>1 Hour Remaining — Renew Your Story Pass!</b>\n\n'
        'Hey <b>{u_name}</b>, your {plan_name} expires at <b>{exp_str}</b>.\n\n'
        '• <b>Plan:</b> {plan_name}\n'
        '• <b>Smart Savings:</b> <emoji id="5341498088408234504">💯</emoji> ₹{saved_amount}\n\n'
        '<blockquote><emoji id="5289650686319929628">😎</emoji> No waiting, zero limits — renew now and keep listening to unlimited stories smoothly!</blockquote>'
    )
]

# ── Instant Pass Expired Templates (5 Distinct Tones) ─────────────────────────
PASS_EXPIRED_IMMEDIATE_TEMPLATES_HI = [
    # 1. Professional (प्रोफेशनल)
    (
        '<emoji id="5260293700088511294">⛔️</emoji> <b>आपका पास समाप्त (Expired) हो चुका है</b>\n\n'
        'नमस्ते <b>{u_name}</b>, आपके <b>{plan_name}</b> की वैधता अब समाप्त हो गई है।\n\n'
        '• <b>प्लान:</b> {plan_name}\n'
        '• <b>स्टेटस:</b> <emoji id="5260293700088511294">⛔️</emoji> समाप्त (Expired)\n'
        '• <b>कुल बचत:</b> <emoji id="5233326571099534068">💸</emoji> <b>₹{saved_amount}</b> की बचत की गई\n\n'
        '<blockquote><emoji id="5402461597237004802">🧐</emoji> बिना किसी कूलडाउन व रुकावट के अपनी पसंदीदा सभी ऑडियो कहानियों को नॉन-स्टॉप सुनते रहने के लिए अभी पास रिन्यू करें।</blockquote>'
    ),
    # 2. Savage (सैवेज)
    (
        '<emoji id="5395695537687123235">🚨</emoji> <b>VIP Era Over — पास अभी-अभी एक्सपायर हो गया!</b>\n\n'
        'सुनो <b>{u_name}</b>, आपका <b>{plan_name}</b> अब खत्म हो चुका है।\n\n'
        '• <b>पिछला प्लान:</b> {plan_name}\n'
        '• <b>जेब की बचत:</b> <emoji id="5280818098960611598">🤑</emoji> <b>₹{saved_amount}</b> सीधे बचाए\n'
        '• <b>करंट स्टेटस:</b> <emoji id="5240241223632954241">🚫</emoji> नॉर्मल यूजर (कूलडाउन चालू)\n\n'
        '<blockquote><emoji id="5208878706717636743">🗿</emoji> VIP जैसी शान से सुनने के बाद अब कूलडाउन लाइन में इंतज़ार करने का इरादा है क्या? तुरंत रिन्यू करो और बॉस की तरह नॉन-स्टॉप बिंज करो!</blockquote>'
    ),
    # 3. Sarcastic (व्यंग्यात्मक / सार्केस्टिक)
    (
        '<emoji id="5447644880824181073">⚠️</emoji> <b>मुबारक हो! कूलडाउन का इंतज़ार वापस आ गया!</b>\n\n'
        'अरे <b>{u_name}</b>, आपका <b>{plan_name}</b> आधिकारिक तौर पर एक्सपायर हो गया है।\n\n'
        '• <b>प्लान:</b> {plan_name}\n'
        '• <b>आपने बचाए थे:</b> <emoji id="5244837092042750681">📈</emoji> <b>₹{saved_amount}</b>\n'
        '• <b>स्टेटस:</b> <emoji id="5386367538735104399">⏳</emoji> अब हर एपिसोड पर टिक-टिक का इंतज़ार\n\n'
        '<blockquote><emoji id="5445091140514620351">😏</emoji> सस्पेंस के बीच में अटकने और घड़ी देखने का बड़ा शौक है? अगर नहीं, तो रिन्यू बटन दबाओ और कूलडाउन को हमेशा के लिए बाय बोलो!</blockquote>'
    ),
    # 4. Humour (मज़ेदार / कॉमेडी)
    (
        '<emoji id="5276032951342088188">💥</emoji> <b>कहानी के क्लाइमेक्स पर पास ने बोला "टाटा, बाय-बाय"!</b>\n\n'
        'अरे <b>{u_name}</b>, आपका <b>{plan_name}</b> अभी एक्सपायर हो गया!\n\n'
        '• <b>प्लान:</b> {plan_name}\n'
        '• <b>टोटल बचत:</b> <emoji id="5409048419211682843">💵</emoji> पूरे <b>₹{saved_amount}</b> जेब में बचाए\n'
        '• <b>हालत:</b> <emoji id="5456174445355875099">🙃</emoji> कूलडाउन मोड ऑन\n\n'
        '<blockquote><emoji id="5361761791355398330">😜</emoji> पॉपकॉर्न तैयार था और पास खत्म हो गया! मज़ा किरकिरा मत होने दो, 1 सेकंड में रिन्यू करो और नॉन-स्टॉप सुनो!</blockquote>'
    ),
    # 5. Marketing (मार्केटिंग / ROI & FOMO)
    (
        '<emoji id="5424972470023104089">🔥</emoji> <b>अनलॉक करें सुपरफास्ट स्पीड — आपका पास समाप्त हुआ!</b>\n\n'
        'प्रिय <b>{u_name}</b>, आपके <b>{plan_name}</b> की वैलिडिटी पूरी हो चुकी है।\n\n'
        '• <b>प्लान:</b> {plan_name}\n'
        '• <b>स्मार्ट सेविंग्स:</b> <emoji id="5341498088408234504">💯</emoji> <b>₹{saved_amount} की भारी बचत की</b>\n'
        '• <b>प्रीमियम फायदा:</b> <emoji id="5456140674028019486">⚡️</emoji> 0s Cooldown + अनलिमिटेड डाउनलोड्स\n\n'
        '<blockquote><emoji id="5427168083074628963">💎</emoji> <b>बिना रुके सुनते रहें:</b> अपनी ऑडियो स्टोरीज़ को बिना किसी ब्रेक के सुनने के लिए अभी सबसे किफायती प्लान में रिन्यू करें!</blockquote>'
    )
]

PASS_EXPIRED_IMMEDIATE_TEMPLATES_EN = [
    # 1. Professional
    (
        '<emoji id="5260293700088511294">⛔️</emoji> <b>Your Unlimited Pass Has Expired</b>\n\n'
        'Hello <b>{u_name}</b>, your <b>{plan_name}</b> has reached its expiration.\n\n'
        '• <b>Plan:</b> {plan_name}\n'
        '• <b>Status:</b> <emoji id="5260293700088511294">⛔️</emoji> Expired\n'
        '• <b>Total Saved:</b> <emoji id="5233326571099534068">💸</emoji> <b>₹{saved_amount}</b>\n\n'
        '<blockquote><emoji id="5402461597237004802">🧐</emoji> Continue enjoying uninterrupted, zero-cooldown access to all audio stories by renewing your pass today.</blockquote>'
    ),
    # 2. Savage
    (
        '<emoji id="5395695537687123235">🚨</emoji> <b>VIP Era Over — Your Pass Just Expired!</b>\n\n'
        'Hey <b>{u_name}</b>, your <b>{plan_name}</b> just expired.\n\n'
        '• <b>Previous Plan:</b> {plan_name}\n'
        '• <b>Money Saved:</b> <emoji id="5280818098960611598">🤑</emoji> <b>₹{saved_amount}</b>\n'
        '• <b>Current Status:</b> <emoji id="5240241223632954241">🚫</emoji> Standard Queue (Cooldown Active)\n\n'
        '<blockquote><emoji id="5208878706717636743">🗿</emoji> Really going back to waiting in cooldown queues after living the VIP life? Renew now and claim your unlimited throne back!</blockquote>'
    ),
    # 3. Sarcastic
    (
        '<emoji id="5447644880824181073">⚠️</emoji> <b>Congratulations! Cooldown waiting lines are back!</b>\n\n'
        'Hey <b>{u_name}</b>, your <b>{plan_name}</b> has officially expired.\n\n'
        '• <b>Plan:</b> {plan_name}\n'
        '• <b>You Saved:</b> <emoji id="5244837092042750681">📈</emoji> <b>₹{saved_amount}</b>\n'
        '• <b>Status:</b> <emoji id="5386367538735104399">⏳</emoji> Mandatory waiting timer active\n\n'
        '<blockquote><emoji id="5445091140514620351">😏</emoji> Missed watching the cooldown timer tick down episode by episode? If not, smash that Renew button and skip the wait!</blockquote>'
    ),
    # 4. Humour
    (
        '<emoji id="5276032951342088188">💥</emoji> <b>Right at the plot twist, your pass said "Goodbye"!</b>\n\n'
        'Hey <b>{u_name}</b>, your <b>{plan_name}</b> just ran out of gas!\n\n'
        '• <b>Plan:</b> {plan_name}\n'
        '• <b>Total Savings:</b> <emoji id="5409048419211682843">💵</emoji> <b>₹{saved_amount}</b> in your pocket\n'
        '• <b>Status:</b> <emoji id="5456174445355875099">🙃</emoji> Cooldown mode turned ON\n\n'
        '<blockquote><emoji id="5361761791355398330">😜</emoji> Popcorn\'s ready but cooldown just hit? Don\'t let the suspense kill you — renew now and binge non-stop!</blockquote>'
    ),
    # 5. Marketing
    (
        '<emoji id="5424972470023104089">🔥</emoji> <b>Unlock Non-Stop Superfast Audio Access!</b>\n\n'
        'Dear <b>{u_name}</b>, your <b>{plan_name}</b> validity has completed.\n\n'
        '• <b>Plan:</b> {plan_name}\n'
        '• <b>Smart Savings:</b> <emoji id="5341498088408234504">💯</emoji> <b>₹{saved_amount} Saved</b>\n'
        '• <b>Prime Benefits:</b> <emoji id="5456140674028019486">⚡️</emoji> Instant Delivery + 0s Cooldown + No Limits\n\n'
        '<blockquote><emoji id="5427168083074628963">💎</emoji> <b>Keep the momentum going:</b> Renew your pass now to continue non-stop audio storytelling at the best affordable rates!</blockquote>'
    )
]


async def get_user_pass_summary_details(uid: int, pass_doc: dict):
    """Resolve user's name, purchased plan name, and calculated savings."""
    u_name = pass_doc.get('user_name') or ""
    if not u_name:
        try:
            tg_u = await db.col.find_one({'id': int(uid)})
            if tg_u and tg_u.get('name'):
                u_name = tg_u['name']
        except Exception:
            pass
    if not u_name:
        u_name = "User"

    plan_name = pass_doc.get('plan_name') or ""
    plan_key = pass_doc.get('plan_key') or ""

    if not plan_name or not plan_key:
        try:
            latest_order = await db.pass_orders.find_one(
                {'user_id': int(uid), 'status': 'PAID'},
                sort=[('paid_at', -1), ('created_at', -1)]
            )
            if latest_order:
                plan_key = latest_order.get('dur_key') or latest_order.get('plan') or plan_key
                plan_name = latest_order.get('plan_name') or plan_name
        except Exception:
            pass

    if not plan_name and plan_key:
        from database import parse_duration_to_seconds, format_duration_verbose
        try:
            dur_sec = parse_duration_to_seconds(str(plan_key), default_unit='d')
            plan_name = f"{format_duration_verbose(dur_sec).title()} Unlimited Pass"
        except Exception:
            plan_name = f"{plan_key} Unlimited Pass"

    if not plan_name:
        plan_name = "Unlimited Story Pass"

    saved_amount = pass_doc.get('savings', 0)
    if not saved_amount or saved_amount <= 0:
        try:
            rl_cfg = await db.get_delivery_rate_limit_config()
            prices = rl_cfg.get('prices', {'1d': 15, '3d': 30, '7d': 55, '1mo': 250, '6mo': 1199})
            dur_k = plan_key or '7d'
            from database import parse_duration_to_seconds
            dur_sec = parse_duration_to_seconds(str(dur_k), default_unit='d')
            dur_days = max(1.0, dur_sec / 86400.0)

            base_p = float(prices.get('1d', 15))
            plan_p = float(prices.get(dur_k, base_p * dur_days))
            std_cost = dur_days * base_p
            if std_cost > plan_p:
                saved_amount = int(round(std_cost - plan_p))
            else:
                saved_amount = int(max(35, round(dur_days * 20)))
        except Exception:
            saved_amount = 50

    return u_name, plan_name, saved_amount


async def get_effective_notification_language(uid: int) -> bool:
    """
    Check if user selected a language ('hi' or 'en').
    If not explicitly chosen, return random choice between True (Hindi) and False (English).
    """
    selected_lang = await db.get_user_selected_language(uid)
    if selected_lang == 'hi':
        return True
    elif selected_lang == 'en':
        return False
    else:
        return random.choice([True, False])


_expiry_monitor_running = False

async def run_pass_expiry_monitor_loop():
    """
    Background worker that continuously monitors all user passes in unlimited_passes:
    1. Pass Expiring Soon (1 hour before):
       Sends renewal reminder (0 < expires_at - now <= 3600).
    2. Pass Expired (Immediate):
       Sends instant expiration alert as soon as expires_at <= now with user name,
       plan name, money saved, 5 diverse tones (Professional, Savage, Sarcastic,
       Humour, Marketing), and an interactive Renew Pass button.
    3. Respects user-selected language; if not chosen, randomly picks Hindi or English.
    4. Guarantees message is sent only once per expiry cycle using DB tracking timestamps.
    """
    global _expiry_monitor_running
    if _expiry_monitor_running:
        return
    _expiry_monitor_running = True
    logger.info("[PASS-EXPIRY-MONITOR] Started Pass Expiry & Instant Expiration Monitor Worker Loop.")

    while True:
        try:
            await asyncio.sleep(45)  # check every 45 seconds
            now = time.time()
            one_hour_ahead = now + 3600

            # ── 1. Pass 1-Hour Reminder Check ───────────────────────────────
            cursor_1h = db.unlimited_passes.find({
                'expires_at': {'$gt': now, '$lte': one_hour_ahead}
            })

            async for pass_doc in cursor_1h:
                uid = pass_doc.get('user_id')
                if not uid:
                    continue
                exp_ts = pass_doc.get('expires_at', 0)
                reminded_ts = pass_doc.get('reminded_1h_expiry')
                if reminded_ts == exp_ts:
                    continue

                # Determine which delivery bot client to use
                bot_id_saved = str(pass_doc.get('bot_id') or '')
                bot_uname_saved = str(pass_doc.get('bot_username') or '').lstrip('@').lower()

                target_client = None
                if bot_id_saved and bot_id_saved in share_clients:
                    target_client = share_clients[bot_id_saved]
                elif bot_uname_saved:
                    for cl in share_clients.values():
                        if cl.me and cl.me.username and cl.me.username.lower() == bot_uname_saved:
                            target_client = cl
                            break

                if not target_client and share_clients:
                    target_client = next(iter(share_clients.values()), None)

                if not target_client:
                    continue

                rem_sec = max(0, int(exp_ts - now))
                rem_mins = max(1, rem_sec // 60)
                u_name, plan_name, saved_amount = await get_user_pass_summary_details(uid, pass_doc)

                import datetime
                try:
                    import pytz
                    ist_tz = pytz.timezone('Asia/Kolkata')
                    exp_dt = datetime.datetime.fromtimestamp(exp_ts, tz=ist_tz)
                    exp_str = exp_dt.strftime('%d-%m-%Y %I:%M %p')
                except Exception:
                    exp_str = datetime.datetime.fromtimestamp(exp_ts).strftime('%d-%m-%Y %I:%M %p')

                is_hi = await get_effective_notification_language(uid)

                if is_hi:
                    template = random.choice(PASS_EXPIRY_TEMPLATES_HI)
                    rem_text = template.format(u_name=u_name, plan_name=plan_name, saved_amount=saved_amount, exp_str=exp_str, rem_mins=rem_mins)
                    btn_renew = "पास रिन्यू करें (Renew Pass)"
                else:
                    template = random.choice(PASS_EXPIRY_TEMPLATES_EN)
                    rem_text = template.format(u_name=u_name, plan_name=plan_name, saved_amount=saved_amount, exp_str=exp_str, rem_mins=rem_mins)
                    btn_renew = "Renew Pass"

                rem_api_buttons = [
                    [{"text": btn_renew, "callback_data": "pass#unlock_menu", "icon_custom_emoji_id": "5217822164362739968"}]
                ]
                rem_buttons = [
                    [InlineKeyboardButton(f"👑 {btn_renew}", callback_data="pass#unlock_menu")]
                ]

                try:
                    sent_ok = await send_or_edit_with_custom_icons(
                        client=target_client,
                        chat_id=uid,
                        text=rem_text,
                        inline_keyboard=rem_api_buttons
                    )
                    if not sent_ok:
                        await target_client.send_message(
                            chat_id=uid,
                            text=rem_text,
                            reply_markup=InlineKeyboardMarkup(rem_buttons)
                        )
                    await db.unlimited_passes.update_one(
                        {'user_id': int(uid)},
                        {'$set': {'reminded_1h_expiry': exp_ts}}
                    )
                    logger.info(f"[PASS-EXPIRY-MONITOR] Sent 1-hour expiry reminder to user {uid} via delivery bot @{target_client.me.username if target_client.me else 'bot'}")
                except Exception as ex:
                    logger.debug(f"[PASS-EXPIRY-MONITOR] Could not send reminder to user {uid}: {ex}")

            # ── 2. Pass Immediate Expired Notification Check ─────────────────
            # Check passes where expires_at <= now, active within last 7 days, and not yet notified
            cursor_expired = db.unlimited_passes.find({
                'expires_at': {'$gt': 0, '$lte': now, '$gte': now - (86400 * 7)}
            })

            async for pass_doc in cursor_expired:
                uid = pass_doc.get('user_id')
                if not uid:
                    continue
                exp_ts = pass_doc.get('expires_at', 0)
                notified_ts = pass_doc.get('notified_expired_ts')
                if notified_ts == exp_ts:
                    continue  # already notified for this exact expiration

                # Determine target delivery bot
                bot_id_saved = str(pass_doc.get('bot_id') or '')
                bot_uname_saved = str(pass_doc.get('bot_username') or '').lstrip('@').lower()

                target_client = None
                if bot_id_saved and bot_id_saved in share_clients:
                    target_client = share_clients[bot_id_saved]
                elif bot_uname_saved:
                    for cl in share_clients.values():
                        if cl.me and cl.me.username and cl.me.username.lower() == bot_uname_saved:
                            target_client = cl
                            break

                if not target_client and share_clients:
                    target_client = next(iter(share_clients.values()), None)

                if not target_client:
                    continue

                u_name, plan_name, saved_amount = await get_user_pass_summary_details(uid, pass_doc)
                is_hi = await get_effective_notification_language(uid)

                if is_hi:
                    template = random.choice(PASS_EXPIRED_IMMEDIATE_TEMPLATES_HI)
                    exp_text = template.format(u_name=u_name, plan_name=plan_name, saved_amount=saved_amount)
                    btn_renew = "👑 पास रिन्यू करें (Renew Pass)"
                else:
                    template = random.choice(PASS_EXPIRED_IMMEDIATE_TEMPLATES_EN)
                    exp_text = template.format(u_name=u_name, plan_name=plan_name, saved_amount=saved_amount)
                    btn_renew = "👑 Renew Pass"

                exp_api_buttons = [
                    [{"text": btn_renew, "callback_data": "pass#unlock_menu", "icon_custom_emoji_id": "5217822164362739968"}]
                ]
                exp_buttons = [
                    [InlineKeyboardButton(btn_renew, callback_data="pass#unlock_menu")]
                ]

                try:
                    sent_ok = await send_or_edit_with_custom_icons(
                        client=target_client,
                        chat_id=uid,
                        text=exp_text,
                        inline_keyboard=exp_api_buttons
                    )
                    if not sent_ok:
                        await target_client.send_message(
                            chat_id=uid,
                            text=exp_text,
                            reply_markup=InlineKeyboardMarkup(exp_buttons)
                        )
                    await db.unlimited_passes.update_one(
                        {'user_id': int(uid)},
                        {'$set': {'notified_expired_ts': exp_ts}}
                    )
                    logger.info(f"[PASS-EXPIRY-MONITOR] Sent INSTANT expiration alert to user {uid} via delivery bot @{target_client.me.username if target_client.me else 'bot'}")
                except Exception as ex:
                    logger.debug(f"[PASS-EXPIRY-MONITOR] Could not send instant expiry notice to user {uid}: {ex}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"[PASS-EXPIRY-MONITOR] Error in monitor loop: {e}")


async def start_share_bot():
    """Start all Share Bot clients from DB."""
    global share_clients

    # Stop existing clients first
    for cl in list(share_clients.values()):
        try:
            await cl.stop()
        except Exception:
            pass
    share_clients.clear()

    bots = await db.get_share_bots()
    if not bots:
        logger.warning("No Share Bots configured — skipping startup.")
        return

    for index, b in enumerate(bots):
        try:
            import os
            os.makedirs("sessions", exist_ok=True)
            sc = Client(
                name=f"share_bot_{b['id']}_{index}",
                bot_token=b['token'],
                api_id=Config.API_ID,
                api_hash=Config.API_HASH,
                workdir="sessions"
            )
            sc.bot_token = b['token']
            _share_bot_token_cache[str(b['id'])] = b['token']
            await sc.start()
            sc.is_initialized = True
            register_share_handlers(sc)
            # ← Always store as STRING so live_batch / other lookups via str() always match
            share_clients[str(b['id'])] = sc
            logger.info(f"Share Bot started: @{sc.me.username} [{b['name']}]")
        except Exception as e:
            logger.error(f"Failed to start Share Bot '{b['name']}': {e}")

    # Launch background pass expiry monitor loop
    asyncio.create_task(run_pass_expiry_monitor_loop())

