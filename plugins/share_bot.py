"""
Share Bot — Delivery Agent
==========================
Handles deep-link delivery of batched episodes to users.
Handler functions are defined at module level so they can be passed to
add_handler() after the client is started (Pyrogram 2.x requirement).
"""
import logging
import asyncio
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

def _get_welcome_text(user, bot_name, custom_wel=None) -> str:
    if custom_wel:
        return format_msg(custom_wel, user)
    first = user.first_name or "User"
    return (
        # Block 1: Greeting with first name only
        f"<blockquote expandable>›› ʜᴇʏ, <a href='tg://user?id={user.id}'>{first}</a>❣️</blockquote>\n"
        # Block 2: Welcome line
        f"<blockquote expandable><b>»  {_sc('Welcome to')} {bot_name}!</b></blockquote>\n"
        # Block 3: Description
        f"<blockquote expandable>{_sc('I am a file delivery bot. Tap any link button from the channel and I will send you the files directly here.')}</blockquote>\n"
        # Block 4: Help hint
        f"<blockquote expandable>{_sc('Click Help for more info.')}</blockquote>"
    )


def _get_help_text(user) -> str:
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
    
    # Strict ban check double-guard to prevent any delivery bot bypasses
    try:
        ban_status = await db.get_ban_status(user_id)
        if ban_status.get('is_banned'):
            reason = str(ban_status.get('reason', '')).lower()
            if 'rapid' in reason or 'strike' in reason:
                await db.unban_user(user_id)
                ban_status = {'is_banned': False}
            else:
                logger.warning(f"[ShareBot] Banned user {user_id} blocked in _process_start")
                return
    except Exception:
        pass
        
    args = message.command
    bot_id = str(client.me.id) if client.me else None

    # Track user for stats and broadcast; detect first-ever start for new-user log in background
    async def _track_user_background():
        try:
            _was_new_user = await db.add_share_bot_seen_user(bot_id, user_id)
            # Also record user usage stats
            await db.add_share_bot_user(bot_id, user_id)
            if _was_new_user:
                import plugins.arya_logger as _log
                u_name = (message.from_user.first_name or str(user_id)) if message.from_user else str(user_id)
                b_name = client.me.first_name if getattr(client, 'me', None) else "DeliveryBot"
                await _log.log_new_user(user_id, u_name, b_name, bot_id or "")
        except Exception:
            pass

    asyncio.create_task(_track_user_background())

    # Plain /start — show welcome
    if len(args) < 2:
        await _send_welcome(client, message, bot_id)
        return

    uuid_str = args[1].strip()

    # Help command via deep-link (start=help)
    if uuid_str == "help":
        await _send_help(client, message, bot_id)
        return

    # 1. Fetch link record from DB
    link_data = await db.get_share_link(uuid_str)
    if not link_data:
        await message.reply_text(
            "<b>‣  Link Expired or Invalid</b>\n\n"
            "This batch link no longer exists. Go back to the channel and click the button again."
        )
        return

    msg_ids     = link_data.get('message_ids', [])
    source_chat = link_data.get('source_chat')
    protect_flag = await db.get_share_protect_global()

    if not msg_ids or not source_chat:
        await message.reply_text("<b>‣  Database Error:</b> Missing file references.")
        return

    # ── Delivery Rate Limit & Cooldown Check ─────────────────────────────────
    from plugins.banned import _is_any_owner
    is_owner_or_whitelisted = (await _is_any_owner(user_id)) or (await db.is_whitelisted(user_id))
    
    if not is_owner_or_whitelisted:
        pass_data = await db.get_user_unlimited_pass(user_id)
        if not pass_data.get('active', False):
            rl_cfg = await db.get_delivery_rate_limit_config()
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

                    limit_text = (
                        f'<emoji id="6215133834149629990">⏳</emoji> <b>Rate Limit Reached</b>\n\n'
                        f'You have already accessed <b>{len(hits)} / {max_limit} links</b> in the past <b>{win_verbose}</b>. <emoji id="6266794310671275367">🎬</emoji>\n\n'
                        f'The limit is <b>{max_limit} links per {win_verbose}</b> to ensure fair usage for everyone.\n\n'
                        f'<emoji id="6217487596486922033">⏰</emoji> <b>Cooldown resets in:</b> <code>{rem_time_str}</code>\n\n'
                        f'<i>Please try again later or unlock unlimited access below! <emoji id="6023566962624306038">👇</emoji></i>'
                    )
                    unlock_kb = InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔒 Unlock Access Via Payment", callback_data="pass#unlock_menu")
                    ]])
                    await message.reply_text(limit_text, reply_markup=unlock_kb)
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

    # Send actual files
    sent_ids = []
    auto_delete_mins = (await db.get_share_bot_about(bot_id)).get('auto_delete', 0) if bot_id else 0
    if not auto_delete_mins:
        auto_delete_mins = await db.get_share_autodelete_global()

    # 5. Deliver
    dl_id = f"{user_id}_{uuid_str}"
    active_downloads.add(dl_id)

    # Show configurable fetching media (GIF / Photo / Video) or fallback to text
    fetching_media = await db.get_bot_fetching_media(bot_id) if bot_id else []
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("Cᴀɴᴄᴇʟ", callback_data=f"cancel_dl_{uuid_str}")]])
    fetch_text = "<i>»  Fᴇᴛᴄʜɪɴɢ ʏᴏᴜʀ ꜰɪʟᴇs sᴇᴄᴜʀᴇʟʏ, ᴘʟᴇᴀsᴇ ᴡᴀɪᴛ...</i>"
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
            # Log the exact error so we know WHY it failed
            logger.warning(
                f"[Fetch] Media send FAILED for bot={bot_id} user={user_id} "
                f"type={ftyp} file_id={fid[:30]}... error: {_fe}"
            )
            # Do NOT clear the DB — just fall back to text for this request.
            # File references can expire; the admin can re-upload to refresh.
            sts = None

    if sts is None:
        # Fallback: plain text status
        sts = await message.reply_text(fetch_text, reply_markup=cancel_kb)

    sent_ids   = []
    fail_count = 0
    cap_tpl    = (await db.get_share_bot_text(bot_id, "custom_caption") if bot_id else "") or \
                 await db.get_share_text("custom_caption", "")
                 
    custom_btns_data = await db.get_share_bot_buttons(bot_id) if bot_id else []
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
                # Resolve placeholder values for this specific message
                file_name = "Unknown"
                file_size_str = "Unknown"
                orig_caption = ""

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
    """Send the welcome message + Help/About buttons."""
    user = message.from_user
    bot_name = client.me.first_name if client.me else "Delivery Bot"

    # Fetch DB info concurrently to save time
    custom_wel_task = asyncio.create_task(db.get_share_bot_text(bot_id, "welcome_msg") if bot_id else asyncio.sleep(0))
    global_wel_task = asyncio.create_task(db.get_share_text("welcome_msg", ""))
    about_task = asyncio.create_task(db.get_share_bot_about(bot_id) if bot_id else asyncio.sleep(0))
    
    custom_wel = await custom_wel_task
    if not custom_wel:
        global_wel = await global_wel_task
        custom_wel = global_wel

    txt = _get_welcome_text(user, bot_name, custom_wel)

    bot_about = await about_task or {}
    welcome_img = random.choice(bot_about.get('menu_image_ids', [])) if bot_about and bot_about.get('menu_image_ids') else None

    buttons = [
        [
            InlineKeyboardButton("»  " + _sc("Arya Premium"), callback_data="sbd#premium"),
        ],
        [
            InlineKeyboardButton(_sc("Help"), callback_data="sbd#help"),
            InlineKeyboardButton(_sc("About"), callback_data="sbd#about"),
        ],
        [InlineKeyboardButton("»  " + _sc("Update Channel"), url=UPDATE_LINK)]
    ]
    markup = InlineKeyboardMarkup(buttons)

    try:
        if welcome_img:
            wid  = welcome_img.get('file_id') if isinstance(welcome_img, dict) else welcome_img
            wtyp = welcome_img.get('media_type', 'photo') if isinstance(welcome_img, dict) else 'photo'

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
        buttons = [
            [InlineKeyboardButton("»  " + _sc("Support"), url=SUPPORT_LINK)],
            [InlineKeyboardButton("«  " + _sc("Back"), callback_data="sbd#back")],
            [InlineKeyboardButton("»  " + _sc("Update Channel"), url=UPDATE_LINK)]
        ]
        markup = InlineKeyboardMarkup(buttons)
        try:
            if is_media_msg: await msg.edit_caption(caption=txt, reply_markup=markup)
            else: await msg.edit_text(txt, reply_markup=markup)
        except Exception: pass

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



    elif cmd == "back":
        await query.answer()
        bot_name = client.me.first_name if client.me else "Delivery Bot"
        custom_wel = (await db.get_share_bot_text(bot_id, "welcome_msg") if bot_id else "") or await db.get_share_text("welcome_msg", "")
        txt = _get_welcome_text(query.from_user, bot_name, custom_wel)
        
        buttons = [
            [
                InlineKeyboardButton(_sc("Help"), callback_data="sbd#help"),
                InlineKeyboardButton(_sc("About"), callback_data="sbd#about"),
            ],
            [InlineKeyboardButton("»  " + _sc("Update Channel"), url=UPDATE_LINK)]
        ]
        markup = InlineKeyboardMarkup(buttons)
        try:
            if is_media_msg: await msg.edit_caption(caption=txt, reply_markup=markup)
            else: await msg.edit_text(txt, reply_markup=markup)
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


# ── Unlimited Delivery Pass Callback Handlers ─────────────────────────────────
_pending_utr_users: dict = {}  # {user_id: {'dur_key': str, 'amount': float, 'ts': float}}


async def _handle_share_bot_utr_message(client, message):
    if not message.from_user or not message.text:
        return
    user_id = message.from_user.id
    if user_id not in _pending_utr_users:
        return

    session_data = _pending_utr_users[user_id]
    dur_key = session_data['dur_key']
    expected_amount = session_data['amount']

    raw_text = message.text.strip()
    if raw_text.lower() in ("cancel", "/cancel", "back", "/back"):
        _pending_utr_users.pop(user_id, None)
        await message.reply_text("<i>UTR submission cancelled.</i>", quote=True)
        return

    import re
    match = re.search(r'\b(\d{12})\b', raw_text)
    if not match:
        clean_digits = re.sub(r'\D', '', raw_text)
        if len(clean_digits) == 12:
            utr = clean_digits
        else:
            await message.reply_text(
                "⚠️ <b>Invalid UTR Format</b>\n\n"
                "Please enter a valid <b>12-digit UTR / Reference number</b> (e.g. <code>423456789012</code>).\n\n"
                "<i>You can find this in your payment receipt from PhonePe, GPay, Paytm, Slice, etc. Type 'cancel' to cancel.</i>",
                quote=True
            )
            return
    else:
        utr = match.group(1)

    if await db.is_utr_used(utr):
        await message.reply_text(
            "❌ <b>UTR Already Redeemed</b>\n\n"
            f"The UTR <code>{utr}</code> has already been claimed for another pass or order. "
            "Each payment transaction can only be redeemed once.",
            quote=True
        )
        return

    sts = await message.reply_text(
        f"🔄 <i>Verifying UTR <code>{utr}</code> via Automated Gmail IMAP... Please wait.</i>",
        quote=True
    )

    from plugins.gmail_helper import verify_upi_payment_via_gmail
    res = await verify_upi_payment_via_gmail(utr, expected_amount)

    if res.get("success"):
        _pending_utr_users.pop(user_id, None)
        await db.mark_utr_used(utr, user_id, expected_amount, dur_key)
        new_expiry = await db.grant_user_unlimited_pass(user_id, dur_key)

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
            f"🎉 <b>UPI Payment Verified Successfully!</b>\n\n"
            f"Hey <b>{u_name}</b>, your <b>{dur_verbose.title()} Unlimited Access Pass</b> is now ACTIVE!\n\n"
            f"<b>UTR / RRN:</b> <code>{utr}</code>\n"
            f"<b>Amount Verified:</b> ₹{expected_amount:.2f}\n"
            f"<b>Valid Until:</b> <code>{exp_str}</code>\n"
            f"<b>Status:</b> Unlimited Access (No Cooldown)\n\n"
            f"<i>You can now access any batch and story links without cooldown. Enjoy!</i>"
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
            order_id=f"UPI_{utr}",
            expiry_ts=new_expiry,
            log_channel=log_ch,
            gateway="UPI (Gmail Auto)"
        ))
    elif res.get("amount_mismatch"):
        m_amt = res.get("mismatched_amount")
        await sts.edit(
            f"⚠️ <b>Payment Amount Mismatch</b>\n\n"
            f"We found the transaction for UTR <code>{utr}</code>, but the received amount is "
            f"<b>₹{m_amt:.2f}</b> while the expected plan price is <b>₹{expected_amount:.2f}</b>.\n\n"
            f"<i>Please pay the exact plan amount to activate your pass, or contact support.</i>"
        )
    else:
        retry_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Re-Verify UTR", callback_data=f"pass#upirecheck_{utr}_{dur_key}_{expected_amount}")],
            [InlineKeyboardButton("❮ Back to Payment Methods", callback_data="pass#unlock_menu")]
        ])
        err_msg = res.get("error", "UTR not found in bank email notifications yet.")
        await sts.edit(
            f"⏳ <b>Payment Not Detected Yet</b>\n\n"
            f"<b>UTR:</b> <code>{utr}</code>\n"
            f"<b>Expected Amount:</b> <code>₹{expected_amount:.2f}</code>\n\n"
            f"<i>{err_msg}</i>\n\n"
            f"<b>Tip:</b> Bank emails can take 10 to 30 seconds to arrive. "
            f"Please wait a few seconds and tap <b>'Re-Verify UTR'</b> below!",
            reply_markup=retry_kb
        )


@Client.on_message(filters.private & filters.text & ~filters.command(["start", "help", "about", "support", "updates", "broadcast", "premium", "norestrictions"]), group=10)
async def _main_bot_utr_interceptor(client, message):
    await _handle_share_bot_utr_message(client, message)


@Client.on_callback_query(filters.regex(r'^pass#'))
async def _process_pass_callback(client, query):
    data = query.data
    user_id = query.from_user.id
    user_name = query.from_user.first_name or "User"

    if data == "pass#unlock_menu":
        rl_cfg = await db.get_delivery_rate_limit_config()
        prices = rl_cfg.get('prices', {'1d': 15, '3d': 30, '7d': 50, '15d': 99, '30d': 149})
        from database import parse_duration_to_seconds, format_duration_verbose
        
        plan_lines = []
        for dur_key, price in prices.items():
            dur_sec = parse_duration_to_seconds(dur_key, default_unit='d')
            verb_dur = format_duration_verbose(dur_sec)
            p_val = int(price) if float(price).is_integer() else price
            usd_val = max(0.50, round(float(price) / 85.0, 2))
            plan_lines.append(f"• {verb_dur.title()}: ₹{p_val} | ${usd_val:.2f}")

        plans_str = "\n".join(plan_lines)

        methods_text = (
            "👑 <b>Premium Membership Plans</b> ❤️\n"
            "──────────────────────\n"
            "⭐️ <b>Pass Benefits:</b>\n"
            "• ⚡️ <b>Multi-Source Caller Engine:</b> Deep identity search with up to 20 alternate name records\n"
            "• 🚀 <b>High Daily Search Limits (30 searches / day)</b>\n"
            "• 🚫 <b>100% Ad-Free Experience</b>\n\n"
            "💎 <b>Available Plans:</b>\n"
            f"{plans_str}\n\n"
            "💳 <b>Select your preferred payment method below:</b>"
        )

        about = await db.get_share_bot_config()
        support_link = (about.get('support_link') if about else None) or SUPPORT_LINK

        methods_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Pay Via UPI ( INR )", callback_data="pass#method_upi")],
            [InlineKeyboardButton("⚡ Pay Via Cashfree", callback_data="pass#method_cashfree")],
            [InlineKeyboardButton("🌐 Pay Via Crypto (Oxapay)", callback_data="pass#method_crypto")],
            [InlineKeyboardButton("📜 My Transactions", callback_data="pass#my_transactions")],
            [
                InlineKeyboardButton("🔒 Support", url=support_link),
                InlineKeyboardButton("← Back", callback_data="pass#back")
            ]
        ])
        await query.message.edit_text(methods_text, reply_markup=methods_kb)

    elif data == "pass#method_cashfree":
        rl_cfg = await db.get_delivery_rate_limit_config()
        prices = rl_cfg.get('prices', {'1d': 15, '3d': 30, '7d': 50, '15d': 99, '30d': 149})
        from database import parse_duration_to_seconds, format_duration_verbose
        
        plan_buttons = []
        icons = ['✷', '✺', '♞', '👑', '⚡', '🔥', '💎', '🚀']
        idx = 0
        for dur_key, price in prices.items():
            dur_sec = parse_duration_to_seconds(dur_key, default_unit='d')
            verb_dur = format_duration_verbose(dur_sec)
            p_val = int(price) if float(price).is_integer() else price
            icon = icons[idx % len(icons)]
            idx += 1
            plan_buttons.append([InlineKeyboardButton(f"⸢ {icon} {verb_dur.title()} ( ₹{p_val} ) ⸥", callback_data=f"pass#cfbuy_{dur_key}_{p_val}")])

        plan_buttons.append([InlineKeyboardButton("← Back", callback_data="pass#unlock_menu")])

        text = (
            "<b>⚡ Pay with Cashfree</b>\n"
            "──────────────────────\n\n"
            "Instant payment with UPI, Cards, NetBanking.\n\n"
            "Select your desired Pass plan:"
        )
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(plan_buttons))

    elif data == "pass#method_upi":
        rl_cfg = await db.get_delivery_rate_limit_config()
        prices = rl_cfg.get('prices', {'1d': 15, '3d': 30, '7d': 50, '15d': 99, '30d': 149})
        from database import parse_duration_to_seconds, format_duration_verbose
        
        plan_buttons = []
        icons = ['✷', '✺', '♞', '👑', '⚡', '🔥', '💎', '🚀']
        idx = 0
        for dur_key, price in prices.items():
            dur_sec = parse_duration_to_seconds(dur_key, default_unit='d')
            verb_dur = format_duration_verbose(dur_sec)
            p_val = int(price) if float(price).is_integer() else price
            icon = icons[idx % len(icons)]
            idx += 1
            plan_buttons.append([InlineKeyboardButton(f"⸢ {icon} {verb_dur.title()} ( ₹{p_val} ) ⸥", callback_data=f"pass#upibuy_{dur_key}_{p_val}")])

        plan_buttons.append([InlineKeyboardButton("← Back", callback_data="pass#unlock_menu")])

        text = (
            "<b>🇮🇳 Pay with UPI ( Manual )</b>\n"
            "──────────────────────\n\n"
            "Instant payment with Paytm, PhonePe, Gpay , BHIM, or any UPI app.\n\n"
            "Select your desired Pass plan:"
        )
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(plan_buttons))

    elif data == "pass#method_crypto":
        rl_cfg = await db.get_delivery_rate_limit_config()
        prices = rl_cfg.get('prices', {'1d': 15, '3d': 30, '7d': 50, '15d': 99, '30d': 149})
        from database import parse_duration_to_seconds, format_duration_verbose
        
        plan_buttons = []
        icons = ['✷', '✺', '♞', '👑', '⚡', '🔥', '💎', '🚀']
        idx = 0
        for dur_key, price in prices.items():
            dur_sec = parse_duration_to_seconds(dur_key, default_unit='d')
            verb_dur = format_duration_verbose(dur_sec)
            p_val = int(price) if float(price).is_integer() else price
            usd_val = max(0.50, round(float(price) / 85.0, 2))
            icon = icons[idx % len(icons)]
            idx += 1
            plan_buttons.append([InlineKeyboardButton(f"⸢ {icon} {verb_dur.title()} ( ${usd_val:.2f} | ₹{p_val} ) ⸥", callback_data=f"pass#oxabuy_{dur_key}_{p_val}")])

        plan_buttons.append([InlineKeyboardButton("← Back", callback_data="pass#unlock_menu")])

        text = (
            "<b>🌐 Pay with Crypto ( OxaPay )</b>\n"
            "──────────────────────\n\n"
            "Instant payment with USDT, BTC, SOL, TON.\n\n"
            "Select your desired Pass plan:"
        )
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(plan_buttons))

    elif data == "pass#my_transactions":
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
            status_line = f"🟢 <b>Active</b> (Valid until: <code>{exp_str}</code>)"
        else:
            status_line = "⚪ <b>No Active Pass</b>"

        txns = await db.get_user_pass_transactions(user_id, limit=5)
        if txns:
            txn_lines = []
            for t in txns:
                import datetime
                try:
                    import pytz
                    ist_tz = pytz.timezone('Asia/Kolkata')
                    t_dt = datetime.datetime.fromtimestamp(t['time'], tz=ist_tz)
                    t_str = t_dt.strftime('%d-%m-%Y %I:%M %p')
                except Exception:
                    t_str = datetime.datetime.fromtimestamp(t['time']).strftime('%d-%m-%Y %I:%M %p') if t['time'] else "N/A"
                
                from database import parse_duration_to_seconds, format_duration_verbose
                dur_verbose = format_duration_verbose(parse_duration_to_seconds(t['plan'], default_unit='d')) if t['plan'] else "Pass"
                txn_lines.append(
                    f"• <b>{dur_verbose.title()}</b> — ₹{t['amount']:.2f}\n"
                    f"  Status: <code>{t['status']}</code> | Gateway: <i>{t['gateway']}</i>\n"
                    f"  Date: <code>{t_str}</code>"
                )
            txns_body = "\n\n".join(txn_lines)
        else:
            txns_body = "<i>No previous transactions found on your account.</i>"

        text = (
            "📜 <b>My Transactions & Pass Status</b>\n"
            "──────────────────────\n"
            f"<b>User:</b> {user_name} (<code>{user_id}</code>)\n"
            f"<b>Pass Status:</b> {status_line}\n"
            "──────────────────────\n"
            "<b>Recent Purchases:</b>\n\n"
            f"{txns_body}"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="pass#unlock_menu")]])
        await query.message.edit_text(text, reply_markup=kb)

    elif data.startswith("pass#upibuy_"):
        parts = data.split("_")
        dur_key = parts[1]
        amount = float(parts[2])

        from database import parse_duration_to_seconds, format_duration_verbose
        dur_sec = parse_duration_to_seconds(dur_key, default_unit='d')
        dur_verbose = format_duration_verbose(dur_sec)

        rl_cfg = await db.get_delivery_rate_limit_config()
        from config import Config
        raw_upi = rl_cfg.get("upi_id", "").strip() or getattr(Config, "UPI_ID", "").strip() or os.environ.get("UPI_ID", "").strip()
        payee_name = rl_cfg.get("upi_name", "").strip() or "Arya Delivery Pass"

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

        import urllib.parse
        upi_uri = f"upi://pay?pa={raw_upi}&pn={urllib.parse.quote_plus(payee_name)}&am={amount:.2f}&cu=INR&tn={urllib.parse.quote_plus(f'{dur_verbose.title()} Pass')}"

        inv_text = (
            f"🧾 <b>UPI Payment Invoice — Unlimited Delivery Pass</b>\n\n"
            f"<b>Plan:</b> {dur_verbose.title()} Unlimited Access\n"
            f"<b>Amount to Pay:</b> <code>₹{amount:.2f}</code>\n\n"
            f"<b>UPI ID (Tap to Copy):</b>\n"
            f"<code>{raw_upi}</code>\n\n"
            f"<b>Payee Name:</b> <code>{payee_name}</code>\n\n"
            f"<blockquote expandable>ℹ️ <b>HOW TO PAY & ACTIVATE:</b>\n"
            f"1. Tap <b>'Open UPI App'</b> or copy the UPI ID above.\n"
            f"2. Pay the exact amount: <b>₹{amount:.2f}</b> via GPay, PhonePe, Paytm, Slice, or CRED.\n"
            f"3. After payment, copy the <b>12-digit UTR / Ref No / Transaction ID</b>.\n"
            f"4. Tap <b>'✍️ Submit 12-Digit UTR'</b> below and send your UTR here for instant automated verification!</blockquote>"
        )
        inv_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📱 Open UPI App", url=upi_uri)],
            [InlineKeyboardButton("✍️ Submit 12-Digit UTR", callback_data=f"pass#upisubmit_{dur_key}_{amount}")],
            [InlineKeyboardButton("← Back", callback_data="pass#method_upi")]
        ])
        await query.message.edit_text(inv_text, reply_markup=inv_kb)

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
            _pending_utr_users.pop(user_id, None)
            await db.mark_utr_used(utr, user_id, expected_amount, dur_key)
            new_expiry = await db.grant_user_unlimited_pass(user_id, dur_key)
            
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
                f"🎉 <b>UPI Payment Verified Successfully!</b>\n\n"
                f"Hey <b>{user_name}</b>, your <b>{dur_verbose.title()} Unlimited Access Pass</b> is now ACTIVE!\n\n"
                f"<b>UTR / RRN:</b> <code>{utr}</code>\n"
                f"<b>Amount Verified:</b> ₹{expected_amount:.2f}\n"
                f"<b>Valid Until:</b> <code>{exp_str}</code>\n"
                f"<b>Status:</b> Unlimited Access (No Cooldown)\n\n"
                f"<i>You can now access any batch and story links without cooldown. Enjoy!</i>"
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
                order_id=f"UPI_{utr}",
                expiry_ts=new_expiry,
                log_channel=log_ch,
                gateway="UPI (Gmail Auto)"
            ))
        elif res.get("amount_mismatch"):
            m_amt = res.get("mismatched_amount")
            await query.message.edit_text(
                f"⚠️ <b>Payment Amount Mismatch</b>\n\n"
                f"Received amount is <b>₹{m_amt:.2f}</b>, but expected is <b>₹{expected_amount:.2f}</b>.\n\n"
                f"<i>Please pay the exact plan amount to activate your pass, or contact support.</i>"
            )
        else:
            await query.answer("⏳ Still not detected. Please wait 10-15 seconds and tap again.", show_alert=True)

    elif data.startswith("pass#oxabuy_"):
        parts = data.split("_")
        dur_key = parts[1]
        amount_inr = float(parts[2])

        try:
            await query.answer("Generating crypto invoice via OxaPay...", show_alert=False)
        except Exception:
            pass

        from plugins.oxapay_helper import create_oxapay_pass_order
        res = await create_oxapay_pass_order(user_id, user_name, dur_key, amount_inr)

        if not res.get("success"):
            err_text = res.get("error", "Failed to generate crypto invoice.")
            err_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Retry", callback_data=data)],
                [InlineKeyboardButton("← Back", callback_data="pass#method_crypto")]
            ])
            return await query.message.edit_text(
                f"❌ <b>Crypto Invoice Failed</b>\n\n{err_text}",
                reply_markup=err_kb
            )

        pay_link = res["pay_link"]
        track_id = res["track_id"]
        amount_usd = res["amount_usd"]
        order_id = res["order_id"]
        dur_name = res["dur_name"]

        inv_text = (
            f"🌐 <b>Crypto Payment Invoice — Unlimited Delivery Pass</b>\n\n"
            f"<b>Plan:</b> {dur_name} Unlimited Access\n"
            f"<b>Amount:</b> <code>${amount_usd:.2f} USD</code> (~₹{amount_inr:.0f})\n"
            f"<b>Order ID:</b> <code>{order_id}</code>\n\n"
            f"<blockquote>Tap the button below to pay using your preferred cryptocurrency (USDT, BTC, ETH, TRX, BNB, LTC, SOL, etc.) via OxaPay. After sending crypto, tap <b>'Verify Payment'</b> to activate!</blockquote>"
        )
        inv_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🌐 Pay ${amount_usd:.2f} Crypto ➟", url=pay_link)],
            [InlineKeyboardButton("🔄 Verify Payment", callback_data=f"pass#oxaverify_{track_id}_{dur_key}_{amount_inr}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"pass#cancel_{order_id}")],
            [InlineKeyboardButton("← Back", callback_data="pass#method_crypto")]
        ])
        await query.message.edit_text(inv_text, reply_markup=inv_kb)

    elif data.startswith("pass#oxaverify_"):
        parts = data.split("_")
        track_id = parts[1]
        dur_key = parts[2]
        amount_inr = float(parts[3])

        try:
            await query.answer("Checking blockchain payment status with OxaPay...", show_alert=False)
        except Exception:
            pass

        from plugins.oxapay_helper import verify_oxapay_pass_order
        v_res = await verify_oxapay_pass_order(track_id)

        if v_res.get("paid"):
            new_expiry = await db.grant_user_unlimited_pass(user_id, dur_key)
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
                f"🎉 <b>Crypto Payment Verified Successfully!</b>\n\n"
                f"Hey <b>{user_name}</b>, your <b>{dur_verbose.title()} Unlimited Access Pass</b> is now ACTIVE!\n\n"
                f"<b>Track ID:</b> <code>{track_id}</code>\n"
                f"<b>Valid Until:</b> <code>{exp_str}</code>\n"
                f"<b>Status:</b> Unlimited Access (No Cooldown)\n\n"
                f"<i>You can now access any batch and story links without cooldown. Enjoy!</i>"
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
                order_id=f"OXA_{track_id}",
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

        try:
            await query.answer("Creating payment order...")
        except Exception:
            pass

        from database import parse_duration_to_seconds, format_duration_verbose
        dur_sec = parse_duration_to_seconds(dur_key, default_unit='d')
        dur_verbose = format_duration_verbose(dur_sec)

        from plugins.cashfree_helper import create_cashfree_pass_order
        res = await create_cashfree_pass_order(user_id, user_name, dur_key, amount)

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

        inv_text = (
            f"🧾 <b>Payment Invoice — Unlimited Delivery Pass</b>\n\n"
            f"<b>Name:</b> {user_name}\n"
            f"<b>User ID:</b> <code>{user_id}</code>\n"
            f"<b>Plan:</b> {dur_verbose.title()} Unlimited Delivery Pass\n"
            f"<b>Amount:</b> ₹{amount:.2f}\n"
            f"<b>Order ID:</b> <code>{order_id}</code>\n\n"
            f"<blockquote>𝑻𝒂𝒑 𝒕𝒉𝒆 𝒃𝒖𝒕𝒕𝒐𝒏 𝒃𝒆𝒍𝒐𝒘 𝒕𝒐 𝒄𝒐𝒎𝒑𝒍𝒆𝒕𝒆 𝒑𝒂𝒚𝒎𝒆𝒏𝒕 𝒗𝒊𝒂 𝑼𝑷𝑰, 𝑮𝒐𝒐𝒈𝒍𝒆 𝑷𝒂𝒚, 𝑷𝒉𝒐𝒏𝒆𝑷𝒆, 𝑷𝒂𝒚𝒕𝒎, 𝑸𝑹, 𝒐𝒓 𝑪𝒂𝒓𝒅. 𝑨𝒇𝒕𝒆𝒓 𝒑𝒂𝒚𝒎𝒆𝒏𝒕, 𝒕𝒂𝒑 𝑽𝒆𝒓𝒊𝒇𝒚 𝑷𝒂𝒚𝒎𝒆𝒏𝒕 𝒕𝒐 𝒂𝒄𝒕𝒊𝒗𝒂𝒕𝒆!</blockquote>"
        )
        inv_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Pay ₹{p_label} for {dur_verbose.title()}➟", url=checkout_pay_link)],
            [InlineKeyboardButton("🔄 Verify Payment", callback_data=f"pass#verify_{order_id}_{dur_key}_{amount}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"pass#cancel_{order_id}")],
            [InlineKeyboardButton("← Back", callback_data="pass#method_cashfree")]
        ])
        await query.message.edit_text(inv_text, reply_markup=inv_kb)

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
            await db.mark_pass_order_paid(order_id, v_res)
            new_expiry = await db.grant_user_unlimited_pass(user_id, dur_key)
            
            import datetime
            try:
                import pytz
                ist_tz = pytz.timezone('Asia/Kolkata')
                exp_dt = datetime.datetime.fromtimestamp(new_expiry, tz=ist_tz)
                exp_str = exp_dt.strftime('%d-%m-%Y %I:%M %p')
            except Exception:
                exp_str = datetime.datetime.fromtimestamp(new_expiry).strftime('%d-%m-%Y %I:%M %p')

            success_text = (
                f"🎉 <b>Unlimited Pass Activated Successfully!</b>\n\n"
                f"Hey <b>{user_name}</b>, your <b>{dur_verbose.title()} Unlimited Access Pass</b> is now ACTIVE!\n\n"
                f"<b>Valid Until:</b> <code>{exp_str}</code>\n"
                f"<b>Status:</b> Unlimited Access (No Cooldown)\n\n"
                f"<i>You can now access any batch and story links without cooldown. Enjoy!</i>"
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
            try:
                await query.answer(
                    "⚠️ Payment Not Received: If you have made the payment, please wait 5-10 seconds for the gateway to confirm and tap Verify again.",
                    show_alert=True
                )
            except Exception:
                pass

    elif data.startswith("pass#cancel_"):
        await query.message.edit_text("<i>Payment invoice cancelled.</i>")


    elif data == "pass#back":
        # Return to rate limit message
        rl_cfg = await db.get_delivery_rate_limit_config()
        from database import format_duration_verbose, format_duration_friendly
        max_limit = int(rl_cfg.get('max_limit', 5))
        window_seconds = int(rl_cfg.get('window_seconds', int(rl_cfg.get('window_hours', 12)) * 3600))
        hits = await db.get_user_delivery_hits(user_id, window_seconds)
        
        import time as _t
        rem_sec = window_seconds
        if hits:
            oldest_hit = hits[0]['timestamp']
            rem_sec = max(1, int((oldest_hit + window_seconds) - _t.time()))
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

        limit_text = (
            f'<emoji id="6215133834149629990">⏳</emoji> <b>Rate Limit Reached</b>\n\n'
            f'You have already accessed <b>{len(hits)} / {max_limit} links</b> in the past <b>{win_verbose}</b>. <emoji id="6266794310671275367">🎬</emoji>\n\n'
            f'The limit is <b>{max_limit} links per {win_verbose}</b> to ensure fair usage for everyone.\n\n'
            f'<emoji id="6217487596486922033">⏰</emoji> <b>Cooldown resets in:</b> <code>{rem_time_str}</code>\n\n'
            f'<i>Please try again later or unlock unlimited access below! <emoji id="6023566962624306038">👇</emoji></i>'
        )
        unlock_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔒 Unlock Access Via Payment", callback_data="pass#unlock_menu")
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
    app.add_handler(CallbackQueryHandler(
        _process_pass_callback,
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
            await sc.start()
            sc.is_initialized = True
            register_share_handlers(sc)
            # ← Always store as STRING so live_batch / other lookups via str() always match
            share_clients[str(b['id'])] = sc
            logger.info(f"Share Bot started: @{sc.me.username} [{b['name']}]")
        except Exception as e:
            logger.error(f"Failed to start Share Bot '{b['name']}': {e}")

