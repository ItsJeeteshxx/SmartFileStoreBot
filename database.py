from os import environ 
from config import Config
import motor.motor_asyncio
from pymongo import MongoClient

async def mongodb_version():
    x = MongoClient(Config.DATABASE_URI)
    mongodb_version = x.server_info()['version']
    return mongodb_version

def parse_duration_to_seconds(val, default_unit='m') -> int:
    """
    Parses flexible duration input to seconds.
    Examples:
        '15' -> 900 (if default_unit == 'm') or 54000 (if 'h')
        '15m', '15min', '15 mins', '15 minutes' -> 900
        '2h', '2hr', '2 hrs', '2 hours' -> 7200
        '1d', '1 day', '7d', '7 days' -> 604800
        '90s', '90 sec', '90 seconds' -> 90
    """
    if isinstance(val, (int, float)):
        if default_unit == 's':
            return int(val)
        elif default_unit == 'm':
            return int(val * 60)
        elif default_unit == 'h':
            return int(val * 3600)
        elif default_unit == 'd':
            return int(val * 86400)
        return int(val)

    s = str(val).strip().lower()
    if not s:
        raise ValueError("Empty duration string")

    import re
    m = re.match(r'^(\d+(?:\.\d+)?)\s*([a-z]*)$', s)
    if not m:
        raise ValueError(f"Invalid duration format: '{val}'")

    num = float(m.group(1))
    unit = m.group(2).strip()

    if not unit:
        unit = default_unit

    if unit in ('s', 'sec', 'secs', 'second', 'seconds'):
        return int(num)
    elif unit in ('m', 'min', 'mins', 'minute', 'minutes'):
        return int(num * 60)
    elif unit in ('h', 'hr', 'hrs', 'hour', 'hours'):
        return int(num * 3600)
    elif unit in ('d', 'day', 'days'):
        return int(num * 86400)
    elif unit in ('w', 'week', 'weeks'):
        return int(num * 604800)
    elif unit in ('mo', 'month', 'months'):
        return int(num * 2592000)
    else:
        raise ValueError(f"Unknown duration unit: '{unit}'")


def format_duration_friendly(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        mins = seconds // 60
        sec = seconds % 60
        return f"{mins}m" if sec == 0 else f"{mins}m {sec}s"
    elif seconds < 86400:
        hrs = seconds // 3600
        rem_m = (seconds % 3600) // 60
        return f"{hrs}h" if rem_m == 0 else f"{hrs}h {rem_m}m"
    else:
        days = seconds // 86400
        rem_h = (seconds % 86400) // 3600
        if rem_h == 0 and days % 30 == 0:
            months = days // 30
            return f"{months}mo"
        return f"{days}d" if rem_h == 0 else f"{days}d {rem_h}h"


def format_duration_verbose(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} second" if seconds == 1 else f"{seconds} seconds"
    elif seconds < 3600:
        mins = seconds // 60
        sec = seconds % 60
        if sec == 0:
            return f"{mins} minute" if mins == 1 else f"{mins} minutes"
        return f"{mins} min {sec} sec"
    elif seconds < 86400:
        hrs = seconds // 3600
        rem_m = (seconds % 3600) // 60
        if rem_m == 0:
            return f"{hrs} hour" if hrs == 1 else f"{hrs} hours"
        return f"{hrs} hr {rem_m} min"
    else:
        days = seconds // 86400
        rem_h = (seconds % 86400) // 3600
        if rem_h == 0:
            if days % 30 == 0:
                months = days // 30
                return f"{months} month" if months == 1 else f"{months} months"
            return f"{days} day" if days == 1 else f"{days} days"
        return f"{days} day{'s' if days != 1 else ''} {rem_h} hr"


def parse_pricing_input(text: str) -> dict:
    """
    Parses flexible pricing input from admin.
    Supported formats:
    - Key-value pairs: '30m:10 1h:15 1d:20 3d:30 7d:50' or '30m:10, 1d:15, 3d:30, 7d:50'
    - Space/comma separated numbers: '15 30 50' (maps to 1d, 3d, 7d)
    """
    import re
    cleaned = text.replace(',', ' ').strip()
    tokens = [t.strip() for t in cleaned.split() if t.strip()]
    if not tokens:
        raise ValueError("Empty pricing input")

    if all(re.match(r'^\d+(?:\.\d+)?$', t) for t in tokens):
        nums = [float(t) for t in tokens]
        if len(nums) == 5:
            return {'1d': nums[0], '3d': nums[1], '7d': nums[2], '1mo': nums[3], '6mo': nums[4]}
        elif len(nums) == 3:
            return {'1d': nums[0], '3d': nums[1], '7d': nums[2]}
        elif len(nums) == 1:
            return {'1d': nums[0]}
        else:
            default_keys = ['1d', '3d', '7d', '1mo', '6mo']
            return {default_keys[i] if i < len(default_keys) else f"{i+1}d": n for i, n in enumerate(nums)}

    res = {}
    for tok in tokens:
        sep = ':' if ':' in tok else ('=' if '=' in tok else ('-' if '-' in tok else ''))
        if not sep:
            raise ValueError(f"Invalid plan format '{tok}'. Expected format like 30m:10 or 1d:15")
        parts = tok.split(sep, 1)
        dur_str = parts[0].strip()
        price_str = parts[1].strip()
        sec = parse_duration_to_seconds(dur_str, default_unit='d')
        if sec <= 0:
            raise ValueError(f"Invalid duration in '{tok}'")
        price = float(price_str)
        if price <= 0:
            raise ValueError(f"Price must be positive in '{tok}'")
        res[dur_str] = price

    if not res:
        raise ValueError("No valid plans found")
    return res


def format_pricing_summary(prices: dict) -> str:
    """Formats prices dictionary for display in menus."""
    items = []
    for k, v in prices.items():
        dur_sec = parse_duration_to_seconds(k, default_unit='d')
        friendly = format_duration_friendly(dur_sec).upper()
        p_val = f"₹{int(v)}" if float(v).is_integer() else f"₹{v:.2f}"
        items.append(f"{friendly}:{p_val}")
    return " | ".join(items)


class Database:
    
    def __init__(self, uri, database_name):
        self._client = motor.motor_asyncio.AsyncIOMotorClient(
            uri,
            tls=True,
            tlsAllowInvalidCertificates=True,   # Fix for Ubuntu 22.04 OpenSSL 3.0 TLSV1_ALERT_INTERNAL_ERROR
            serverSelectionTimeoutMS=30000,
            connectTimeoutMS=30000,
            socketTimeoutMS=30000,
        )
        self.db = self._client[database_name]
        self.bot = self.db.bots
        self.col = self.db.users
        self.nfy = self.db.notify
        self.chl = self.db.channels
        self.stats = self.db.global_stats
        self.share_links = self.db.share_links
        self.share_config = self.db.share_config  # global share bot settings
        self.premium_bans = self.db.premium_bans
        self.premium_ban_activity = self.db.premium_ban_activity
        self.share_deliveries = self.db.share_deliveries
        self.share_users = self.db.share_users
        self.delivery_hits = self.db.delivery_hits
        self.unlimited_passes = self.db.unlimited_passes
        self.pass_orders = self.db.delivery_pass_orders
        self.used_utrs = self.db.used_utrs
        self.store_shows = self.db.store_shows
        self.store_orders = self.db.store_orders
        self.store_user_shows = self.db.store_user_shows
        self.store_config = self.db.store_config
        
        self._ban_status_cache = {}  # {user_id: (ban_status_dict, expiry)}
        self._bot_cfg_cache = {}     # {bot_id: (cfg_dict, expiry)}
        self._share_cfg_cache = None  # (cfg_dict, expiry)
        self._user_cache = {}        # {user_id: (user_doc, expiry)}
        self._rl_cfg_cache = None    # (cfg_dict, expiry)
        self._store_cfg_cache = {}   # {bot_id: (cfg_dict, expiry)}

        
    async def set_share_bot_token(self, token: str):
        # Migrated: now handles multiple bots via array push, preserving backwards compatibility for singles initially if desired, or just override.
        pass

    async def get_share_bot_token(self):
        # Legacy
        doc = await self.stats.find_one({'_id': 'share_bot'})
        return doc.get('token') if doc else None

    async def get_share_bot_config(self, bot_id: str = "") -> dict:
        """Fetch share bot about/config dict."""
        if bot_id:
            return await self.get_share_bot_about(bot_id)
        doc = await self.stats.find_one({'_id': 'share_config'})
        return doc or {}
        
    async def get_share_bots(self) -> list:
        """Returns list of all configured share bots from DB."""
        doc = await self.stats.find_one({'_id': 'share_bots_list'})
        if doc and 'bots' in doc:
            return doc['bots']
        return []

    async def get_share_protect_global(self) -> bool:
        """Global toggle to protect share bot deliveries."""
        doc = await self.stats.find_one({'_id': 'share_config'})
        return doc.get('protect', False) if doc else False

    async def set_share_protect_global(self, protect: bool):
        await self.stats.update_one({'_id': 'share_config'}, {'$set': {'protect': protect}}, upsert=True)

    async def get_task_routing(self) -> dict:
        """Returns routing map: e.g. {'merger': 'google_worker', 'cleaner': 'main'}"""
        doc = await self.stats.find_one({'_id': 'task_routing'})
        return doc.get('routing', {}) if doc else {}

    async def set_task_routing(self, routing: dict):
        await self.stats.update_one({'_id': 'task_routing'}, {'$set': {'routing': routing}}, upsert=True)

    async def add_share_bot(self, b_id: int, token: str, username: str, name: str):
        """Adds a new share bot. Prevents duplicates by ID."""
        b_id_str = str(b_id)
        # Remove existing entry with same ID first (upsert-style)
        await self.stats.update_one(
            {'_id': 'share_bots_list'},
            {'$pull': {'bots': {'id': b_id_str}}},
            upsert=True
        )
        bot_dict = {'id': b_id_str, 'token': token, 'username': username, 'name': name}
        await self.stats.update_one(
            {'_id': 'share_bots_list'},
            {'$push': {'bots': bot_dict}},
            upsert=True
        )

    async def remove_share_bot(self, b_id: str):
        """Removes a share bot by its string ID."""
        await self.stats.update_one(
            {'_id': 'share_bots_list'},
            {'$pull': {'bots': {'id': str(b_id)}}}
        )

    async def set_share_protect_global(self, protect: bool):
        await self._set_share_cfg(protect=protect)

    async def get_share_protect_global(self) -> bool:
        return (await self._share_cfg()).get('protect', True)

    async def set_share_autodelete(self, user_id: int, minutes: int):
        await self.col.update_one({'_id': user_id}, {'$set': {'share_autodelete': minutes}}, upsert=True)

    async def get_share_autodelete(self, user_id: int) -> int:
        doc = await self.col.find_one({'_id': user_id})
        return doc.get('share_autodelete', 0) if doc else 0

    # ── Global Share Config ──────────────────────────────────────
    async def _share_cfg(self) -> dict:
        import time as _t
        now = _t.time()
        if hasattr(self, '_share_cfg_cache') and self._share_cfg_cache is not None:
            val, expiry = self._share_cfg_cache
            if now < expiry:
                return val
        doc = await self.share_config.find_one({'_id': 'global'})
        res = doc or {}
        if hasattr(self, '_share_cfg_cache'):
            self._share_cfg_cache = (res, now + 30)  # cache global config for 30s
        return res

    async def _set_share_cfg(self, **kwargs):
        await self.share_config.update_one({'_id': 'global'}, {'$set': kwargs}, upsert=True)
        # Evict cache
        if hasattr(self, '_share_cfg_cache'):
            self._share_cfg_cache = None

    # Auto-delete (global, minutes)
    async def get_share_autodelete_global(self) -> int:
        return (await self._share_cfg()).get('auto_delete', 0)

    async def set_share_autodelete_global(self, minutes: int):
        await self._set_share_cfg(auto_delete=minutes)

    # Buttons per post (global)
    async def get_share_buttons_per_post(self) -> int:
        return (await self._share_cfg()).get('buttons_per_post', 10)

    async def set_share_buttons_per_post(self, n: int):
        await self._set_share_cfg(buttons_per_post=n)

    # Force-subscribe channels list [{chat_id, title, invite_link, join_request}]
    async def get_share_fsub_channels(self) -> list:
        return (await self._share_cfg()).get('fsub_channels', [])

    async def set_share_fsub_channels(self, channels: list):
        await self._set_share_cfg(fsub_channels=channels)

    # Customizable Texts (global fallback)
    async def get_share_text(self, key: str, default: str = "") -> str:
        return (await self._share_cfg()).get(key, default)

    async def set_share_text(self, key: str, value: str):
        if not value:
            await self.share_config.update_one({'_id': 'global'}, {'$unset': {key: ""}}, upsert=True)
        else:
            await self._set_share_cfg(**{key: value})
        # Evict cache
        if hasattr(self, '_share_cfg_cache'):
            self._share_cfg_cache = None

    # AI Image Enhancer Config
    async def get_enhancer_config(self) -> dict:
        cfg = await self._share_cfg()
        return cfg.get('enhancer', {
            'api_key': '',
            'enabled': False,
            'model': 'esrgan',
            'scale': 2
        })

    async def update_enhancer_config(self, **kwargs):
        cfg = await self.get_enhancer_config()
        cfg.update(kwargs)
        await self._set_share_cfg(enhancer=cfg)

    # ── Per-Bot Config ────────────────────────────────────────────
    async def _bot_cfg(self, bot_id: str) -> dict:
        if not bot_id:
            return {}
        import time as _t
        now = _t.time()
        if hasattr(self, '_bot_cfg_cache'):
            if bot_id in self._bot_cfg_cache:
                val, expiry = self._bot_cfg_cache[bot_id]
                if now < expiry:
                    return val
        doc = await self.share_config.find_one({'_id': f'bot_{bot_id}'})
        res = doc or {}
        if hasattr(self, '_bot_cfg_cache'):
            self._bot_cfg_cache[bot_id] = (res, now + 30)  # cache bot config for 30s
        return res

    async def _set_bot_cfg(self, bot_id: str, **kwargs):
        if not bot_id:
            return
        await self.share_config.update_one(
            {'_id': f'bot_{bot_id}'}, {'$set': kwargs}, upsert=True
        )
        # Evict cache
        if hasattr(self, '_bot_cfg_cache'):
            self._bot_cfg_cache.pop(bot_id, None)

    # Per-bot customizable texts (welcome_msg, delete_msg, success_msg, custom_caption, fsub_msg)
    async def get_share_bot_text(self, bot_id: str, key: str, default: str = "") -> str:
        return (await self._bot_cfg(bot_id)).get(key, default)

    async def set_share_bot_text(self, bot_id: str, key: str, value: str):
        if not bot_id:
            return
        if not value:
            await self.share_config.update_one(
                {'_id': f'bot_{bot_id}'}, {'$unset': {key: ""}}, upsert=True
            )
        else:
            await self._set_bot_cfg(bot_id, **{key: value})

    # Per-bot fsub channels
    async def get_bot_fsub_channels(self, bot_id: str) -> list:
        return (await self._bot_cfg(bot_id)).get('fsub_channels', [])

    async def set_bot_fsub_channels(self, bot_id: str, channels: list):
        await self._set_bot_cfg(bot_id, fsub_channels=channels)

    # Per-bot Custom Buttons
    async def get_share_bot_buttons(self, bot_id: str) -> list:
        return (await self._bot_cfg(bot_id)).get('custom_buttons', [])

    async def set_share_bot_buttons(self, bot_id: str, buttons: list):
        await self._set_bot_cfg(bot_id, custom_buttons=buttons)

    # FSub approval tracking
    async def save_user_fsub_approved(self, bot_id: str, user_id: int):
        """Mark user as FSub-approved for this bot"""
        await self.col.update_one(
            {'id': user_id},
            {'$addToSet': {'fsub_approved_bots': bot_id}},
            upsert=True
        )

    async def is_user_fsub_approved(self, bot_id: str, user_id: int) -> bool:
        """Check if user has been approved for FSub on this bot"""
        result = await self.col.find_one(
            {'id': user_id, 'fsub_approved_bots': bot_id}
        )
        return result is not None

    # Per-bot About section
    async def get_share_bot_about(self, bot_id: str) -> dict:
        return (await self._bot_cfg(bot_id)).get('about', {})

    async def set_share_bot_about(self, bot_id: str, about: dict):
        await self._set_bot_cfg(bot_id, about=about)

    async def get_bot_premium_ad_media(self, bot_id: str) -> dict:
        return (await self._bot_cfg(bot_id)).get('premium_ad_media', {})

    async def set_bot_premium_ad_media(self, bot_id: str, media: dict):
        if not media:
            await self.share_config.update_one(
                {'_id': f'bot_{bot_id}'}, {'$unset': {'premium_ad_media': ""}}
            )
            if hasattr(self, '_bot_cfg_cache'):
                self._bot_cfg_cache.pop(bot_id, None)
        else:
            await self._set_bot_cfg(bot_id, premium_ad_media=media)

    # Per-bot delivery counter
    async def increment_bot_delivery_count(self, bot_id: str, count: int = 1):
        """Increment total files delivered by this bot."""
        if not bot_id: return
        await self.share_config.update_one(
            {'_id': f'bot_{bot_id}'},
            {'$inc': {'total_delivered': count}},
            upsert=True
        )

    async def get_bot_delivery_count(self, bot_id: str) -> int:
        """Return total files ever delivered by this bot."""
        if not bot_id: return 0
        doc = await self.share_config.find_one({'_id': f'bot_{bot_id}'})
        return (doc or {}).get('total_delivered', 0)

    # Per-bot user tracking
    async def add_share_bot_user(self, bot_id: str, user_id: int):
        """Track that this user has used this share bot."""
        if not bot_id: return
        try:
            await self.col.update_one(
                {'id': int(user_id)},
                {
                    '$addToSet': {'used_share_bots': str(bot_id)},
                    '$set': {'id': int(user_id)}
                },
                upsert=True
            )
        except Exception:
            pass

    # Per-bot fetching media (GIF/image/video shown while delivering files)
    async def get_bot_fetching_media(self, bot_id: str) -> list:
        """Return list of {'file_id': ..., 'media_type': 'photo'|'animation'|'video'} or []."""
        fm = (await self._bot_cfg(bot_id)).get('fetching_media', [])
        if isinstance(fm, dict):
            return [fm] if fm.get('file_id') else []
        return fm

    async def set_bot_fetching_media(self, bot_id: str, fetch_list: list):
        await self._set_bot_cfg(bot_id, fetching_media=fetch_list)

    async def clear_bot_fetching_media(self, bot_id: str):
        if not bot_id: return
        await self.share_config.update_one(
            {'_id': f'bot_{bot_id}'}, {'$unset': {'fetching_media': ''}}, upsert=True
        )

    # When a bot is removed, clean up its config too
    async def remove_share_bot_config(self, bot_id: str):
        await self.share_config.delete_one({'_id': f'bot_{bot_id}'})

    # ── AI Enhancer Config ──────────────────────────────────────────────────
    async def get_enhancer_config(self) -> dict:
        doc = await self.share_config.find_one({'_id': 'ai_enhancer_cfg'})
        return doc or {}

    async def update_enhancer_config(self, **kwargs):
        await self.share_config.update_one({'_id': 'ai_enhancer_cfg'}, {'$set': kwargs}, upsert=True)

    # ── Channel Index (full file list per database channel) ───────
    async def save_channel_index(self, chat_id: int, entries: list, meta: dict = None):
        """Save (or replace) the full scan index for a channel."""
        import time
        doc = {
            '_id': f'ch_index_{chat_id}',
            'chat_id': chat_id,
            'entries': entries,
            'count': len(entries),
            'scanned_at': time.time(),
            'meta': meta or {},
        }
        await self.share_config.update_one(
            {'_id': f'ch_index_{chat_id}'}, {'$set': doc}, upsert=True
        )

    async def get_channel_index(self, chat_id: int):
        """Return the stored index doc for this channel, or None."""
        return await self.share_config.find_one({'_id': f'ch_index_{chat_id}'})

    async def get_channel_index_meta(self, chat_id: int):
        """Return only the index metadata doc without loading the huge entries array."""
        return await self.share_config.find_one({'_id': f'ch_index_{chat_id}'}, {'entries': 0})

    async def delete_channel_index(self, chat_id: int):
        """Remove the index for a channel."""
        await self.share_config.delete_one({'_id': f'ch_index_{chat_id}'})

    async def bulk_update_channel_index_entries(self, chat_id: int, entries: list):
        """Append or update a batch of entries atomically without loading the entire huge document."""
        if not entries:
            return
        import time
        msg_ids = [e['msg_id'] for e in entries]
        # 1. Pull existing duplicate msg_ids to avoid duplicates
        await self.share_config.update_one(
            {'_id': f'ch_index_{chat_id}'},
            {'$pull': {'entries': {'msg_id': {'$in': msg_ids}}}},
            upsert=False
        )
        # 2. Push new entries directly to the array in MongoDB
        await self.share_config.update_one(
            {'_id': f'ch_index_{chat_id}'},
            {
                '$push': {'entries': {'$each': entries}},
                '$set': {'scanned_at': time.time()},
                '$inc': {'count': len(entries)},
            },
            upsert=True
        )



    # Per-bot Users Tracker
    async def add_share_bot_user(self, bot_id: str, user_id: int):
        if not bot_id: return
        import time
        try:
            await self.share_users.update_one(
                {'bot_id': str(bot_id), 'user_id': int(user_id)},
                {
                    '$setOnInsert': {'first_seen': time.time()},
                    '$unset': {'blocked': '', 'deactivated': ''}
                },
                upsert=True
            )
        except Exception:
            pass

    async def get_share_bot_users(self, bot_id: str) -> list:
        cursor = self.share_users.find({
            'bot_id': str(bot_id),
            'blocked': {'$ne': True},
            'deactivated': {'$ne': True}
        })
        return [doc['user_id'] async for doc in cursor]

    async def get_share_bot_users_stats(self, bot_id: str) -> dict:
        total = await self.share_users.count_documents({'bot_id': str(bot_id)})
        blocked = await self.share_users.count_documents({'bot_id': str(bot_id), 'blocked': True})
        deactivated = await self.share_users.count_documents({'bot_id': str(bot_id), 'deactivated': True})
        active = total - blocked - deactivated
        return {
            'total': total,
            'blocked': blocked,
            'deactivated': deactivated,
            'active': active
        }

    async def set_share_bot_user_status(self, bot_id: str, user_id: int, blocked: bool = False, deactivated: bool = False):
        if not bot_id: return
        update_doc = {}
        if blocked:
            update_doc['blocked'] = True
        if deactivated:
            update_doc['deactivated'] = True
        if update_doc:
            try:
                await self.share_users.update_one(
                    {'bot_id': str(bot_id), 'user_id': int(user_id)},
                    {'$set': update_doc},
                    upsert=True
                )
            except Exception:
                pass

    # save_share_link — access_hash allows Share Bot to rebuild peer cache at delivery time
    async def save_share_link(self, uuid_str: str, message_ids: list, source_chat,
                              protect: bool = True, access_hash: int = 0):
        doc = {
            '_id': uuid_str,
            'message_ids': message_ids,
            'source_chat': source_chat,
            'protect': protect,
            'access_hash': access_hash,
        }
        await self.share_links.update_one({'_id': uuid_str}, {'$set': doc}, upsert=True)


    async def get_share_link(self, uuid_str: str):
        return await self.share_links.find_one({'_id': uuid_str})
        
    async def get_sys_mode(self) -> str:
        doc = await self.opt.find_one({"_id": "SYS_MODE"})
        return doc.get("mode", "vps") if doc else "vps"
        
    async def set_sys_mode(self, mode: str):
        await self.opt.update_one({"_id": "SYS_MODE"}, {"$set": {"mode": mode}}, upsert=True)

    async def get_global_stats(self):
        import time
        doc = await self.stats.find_one({'_id': 'bot_stats'})
        if not doc:
            doc = {
                '_id': 'bot_stats',
                'live_forward': 0,
                'batch_forward': 0,
                'normal_forward': 0,
                'total_files_downloaded': 0,
                'total_files_uploaded': 0,
                'total_data_usage_bytes': 0,
                'bot_start_time': time.time()
            }
            await self.stats.insert_one(doc)
        return doc
        
    async def update_global_stats(self, **kwargs):
        """Pass fields to update as keyword arguments, e.g. update_global_stats(live_forward=1)"""
        if not kwargs: return
        await self.stats.update_one({'_id': 'bot_stats'}, {'$inc': kwargs}, upsert=True)
        
    async def reset_global_stats(self):
        import time
        await self.stats.update_one({'_id': 'bot_stats'}, {'$set': {
            'live_forward': 0,
            'batch_forward': 0,
            'normal_forward': 0,
            'total_files_downloaded': 0,
            'total_files_uploaded': 0,
            'total_data_usage_bytes': 0,
            'bot_start_time': time.time()
        }}, upsert=True)
        
    def new_user(self, id, name):
        return dict(
            id = id,
            name = name,
            ban_status=dict(
                is_banned=False,
                ban_reason="",
            ),
        )
      
    async def add_user(self, id, name):
        user = self.new_user(id, name)
        await self.col.insert_one(user)
        self._invalidate_user_cache(id)
    
    async def is_user_exist(self, id):
        user = await self._get_user_doc(id)
        return bool(user)
    
    async def total_users_bots_count(self):
        bcount = await self.bot.count_documents({})
        count = await self.col.count_documents({})
        return count, bcount

    async def total_channels(self):
        docs = await self.chl.distinct("chat_id")
        return len(docs)
    
    async def remove_ban(self, id):
        ban_status = dict(
            is_banned=False,
            ban_reason=''
        )
        await self.col.update_one({'id': int(id)}, {
            '$set': {'ban_status': ban_status},
            '$unset': {'abuse_strike': ''}
        }, upsert=True)
        # Evict cache
        if hasattr(self, '_ban_status_cache'):
            self._ban_status_cache.pop(int(id), None)
        self._invalidate_user_cache(id)
    
    async def _get_user_doc(self, user_id: int) -> dict:
        import time as _t
        now = _t.time()
        uid_int = int(user_id)
        if hasattr(self, '_user_cache') and uid_int in self._user_cache:
            doc, expiry = self._user_cache[uid_int]
            if now < expiry:
                return doc
        try:
            doc = await self.col.find_one({'id': uid_int})
            res = doc or {}
            if hasattr(self, '_user_cache'):
                self._user_cache[uid_int] = (res, now + 30)  # cache for 30s
            return res
        except Exception:
            return {}

    def _invalidate_user_cache(self, user_id: int):
        if hasattr(self, '_user_cache'):
            self._user_cache.pop(int(user_id), None)

    async def ban_user(self, user_id, ban_reason="No Reason"):
        ban_status = dict(
            is_banned=True,
            ban_reason=ban_reason
        )
        await self.col.update_one({'id': int(user_id)}, {'$set': {'ban_status': ban_status}}, upsert=True)
        # Evict cache
        if hasattr(self, '_ban_status_cache'):
            self._ban_status_cache.pop(int(user_id), None)
        self._invalidate_user_cache(user_id)

    async def get_ban_status(self, id):
        default = dict(
            is_banned=False,
            ban_reason=''
        )
        try:
            user_id_int = int(id)
        except (ValueError, TypeError):
            return default
            
        # Check cache
        import time as _t
        now = _t.time()
        if hasattr(self, '_ban_status_cache'):
            if user_id_int in self._ban_status_cache:
                val, expiry = self._ban_status_cache[user_id_int]
                if now < expiry:
                    return val

        # Immunity check: Paid users are immune to auto-bans
        try:
            if await self.is_paid_user(user_id_int):
                # If paid user has local ban status with an auto-ban reason, unban them automatically
                user = await self._get_user_doc(user_id_int)
                if user and user.get('ban_status', {}).get('is_banned'):
                    reason = str(user.get('ban_status', {}).get('ban_reason', '')).lower()
                    if "auto-ban" in reason or "alt of" in reason or "strike" in reason or "rapid" in reason or "evasion" in reason:
                        await self.remove_ban(user_id_int)
                        user = await self._get_user_doc(user_id_int)
                
                # Check premium_bans collection for auto-ban
                prem_ban = await self.db.premium_bans.find_one({'_id': user_id_int})
                if prem_ban and prem_ban.get('status') in ('banned', 'flagged'):
                    reason = str(prem_ban.get('reason', '')).lower()
                    if "auto-ban" in reason or "alt of" in reason or "strike" in reason or "rapid" in reason or "evasion" in reason or prem_ban.get('status') == 'flagged':
                        await self.db.premium_bans.delete_one({'_id': user_id_int})
                        prem_ban = None
                
                if not (user and user.get('ban_status', {}).get('is_banned')) and not prem_ban:
                    if hasattr(self, '_ban_status_cache'):
                        self._ban_status_cache[user_id_int] = (default, now + 120)
                    return default
        except Exception as _p_err:
            pass

        # 1. Check local bot collection ban status in 'arya' DB using cached doc
        user = await self._get_user_doc(user_id_int)
        if user and user.get('ban_status', {}).get('is_banned'):
            res = user.get('ban_status', default)
            if hasattr(self, '_ban_status_cache'):
                # Cache banned user for 30s
                self._ban_status_cache[user_id_int] = (res, now + 30)
            return res
            
        # 2. Check premium_bans collection (same 'arya' DB — used by mini app admin panel)
        try:
            prem_ban = await self.db.premium_bans.find_one({'_id': user_id_int})
            if prem_ban and prem_ban.get('status') in ('banned', 'flagged'):
                res = {
                    'is_banned': True,
                    'ban_reason': prem_ban.get('reason', 'Banned by administrator')
                }
                if hasattr(self, '_ban_status_cache'):
                    # Cache banned user for 30s
                    self._ban_status_cache[user_id_int] = (res, now + 30)
                return res
        except Exception:
            pass
            
        if hasattr(self, '_ban_status_cache'):
            # Cache non-banned user for 120s
            self._ban_status_cache[user_id_int] = (default, now + 120)
        return default

    async def is_paid_user(self, user_id) -> bool:
        """Check if user is a paid user (has at least 1 purchased story or completed order)."""
        if not user_id:
            return False
        try:
            uid_int = int(user_id) if str(user_id).isdigit() else user_id
            uid_str = str(user_id)
            u_filter = [uid_int, uid_str]
            
            # 1. Check users purchases array
            user = await self.col.find_one({
                "$or": [{"id": {"$in": u_filter}}, {"_id": {"$in": u_filter}}],
                "purchases.0": {"$exists": True}
            })
            if user and user.get("purchases"):
                return True

            # 2. Check orders collection
            order = await self.db.orders.find_one({
                "user_id": {"$in": u_filter},
                "status": {"$in": ["paid", "delivered", "completed", "success"]}
            })
            if order:
                return True

            # 3. Check premium_purchases collection
            purchase = await self.db.premium_purchases.find_one({
                "user_id": {"$in": u_filter}
            })
            if purchase:
                return True

            # 4. Check premium_checkout collection
            checkout = await self.db.premium_checkout.find_one({
                "user_id": {"$in": u_filter},
                "status": "approved"
            })
            if checkout:
                return True

            return False
        except Exception:
            return False

    async def has_purchase(self, user_id: int, story_id: str) -> bool:
        """Checks if a user has purchased a given story across users, orders, premium_purchases, and premium_checkout collections."""
        if not user_id or not story_id:
            return False
        try:
            uid_int = int(user_id) if str(user_id).isdigit() else user_id
            uid_str = str(user_id)
            u_filter = [uid_int, uid_str]
            sid_str = str(story_id).strip()

            story_aliases = set([sid_str])
            try:
                from bson.objectid import ObjectId
                from bson.errors import InvalidId
                story_doc = None
                try:
                    o_id = ObjectId(sid_str)
                    story_doc = await self.db.premium_stories.find_one({"_id": o_id})
                except InvalidId:
                    pass
                if not story_doc:
                    story_doc = await self.db.premium_stories.find_one({"_id": sid_str})
                if not story_doc:
                    story_doc = await self.db.premium_stories.find_one({"story_id": sid_str})

                if story_doc:
                    story_aliases.add(str(story_doc["_id"]))
                    if story_doc.get("story_id"):
                        story_aliases.add(str(story_doc["story_id"]))
            except Exception as se:
                logger.warning(f"Error fetching story aliases in has_purchase: {se}")

            story_aliases_list = list(story_aliases)

            # 1. Check users collection
            user = await self.col.find_one({"id": {"$in": u_filter}})
            if user:
                purchases = [str(p) for p in user.get("purchases", [])]
                for alias in story_aliases_list:
                    if alias in purchases:
                        return True

            # 2. Check orders collection
            order = await self.db.orders.find_one({
                "user_id": {"$in": u_filter},
                "status": {"$in": ["paid", "delivered", "completed", "success"]},
                "$or": [
                    {"story_id": {"$in": story_aliases_list}},
                    {"story_ids": {"$in": story_aliases_list}},
                    {"items.id": {"$in": story_aliases_list}}
                ]
            })
            if order:
                return True

            # 3. Check premium_purchases collection
            purchase = await self.db.premium_purchases.find_one({
                "user_id": {"$in": u_filter},
                "$or": [
                    {"story_id": {"$in": story_aliases_list}},
                    {"story_ids": {"$in": story_aliases_list}}
                ]
            })
            if purchase:
                return True

            # 4. Check premium_checkout collection
            checkout = await self.db.premium_checkout.find_one({
                "user_id": {"$in": u_filter},
                "status": {"$in": ["approved", "completed", "paid", "success"]},
                "$or": [
                    {"story_id": {"$in": story_aliases_list}},
                    {"story_ids": {"$in": story_aliases_list}}
                ]
            })
            if checkout:
                return True

            return False
        except Exception as e:
            logger.error(f"Error in has_purchase: {e}")
            return False

    async def auto_unblock_paid_users(self):
        """
        Scans all paid users across orders, premium_purchases, premium_checkout, and users.
        If any paid user was auto-banned / auto-blocked (in premium_bans or users.ban_status),
        it automatically lifts the ban, clears ban_status, and removes their IP, device_id, etc.
        """
        try:
            logger.info("⚡ Running Auto-Unblock System for Paid Users...")
            paid_uids = set()
            
            db_obj = getattr(self, 'db', None)
            if db_obj is None:
                logger.info("auto_unblock_paid_users: DB object not initialized yet, skipping.")
                return

            users_col = getattr(self, 'users', None)
            if users_col is None:
                users_col = getattr(self, 'col', None)
            if users_col is None and db_obj is not None:
                users_col = getattr(db_obj, 'users', None)

            # 1. Collect from users.purchases
            if users_col is not None:
                async for doc in users_col.find({"purchases.0": {"$exists": True}}, {"id": 1}):
                    uid = doc.get("id")
                    if uid is not None:
                        paid_uids.add(str(uid))
                        try: paid_uids.add(int(uid))
                        except: pass
                    
            # 2. Collect from orders
            if hasattr(db_obj, 'orders') and db_obj.orders is not None:
                async for doc in db_obj.orders.find({"status": {"$in": ["paid", "delivered", "completed", "success"]}}, {"user_id": 1}):
                    uid = doc.get("user_id")
                    if uid is not None:
                        paid_uids.add(str(uid))
                        try: paid_uids.add(int(uid))
                        except: pass

            # 3. Collect from premium_purchases
            purchases_col = getattr(self, 'purchases', None)
            if purchases_col is None and db_obj is not None:
                purchases_col = getattr(db_obj, 'premium_purchases', None)
            if purchases_col is not None:
                async for doc in db_obj.premium_purchases.find({}, {"user_id": 1}):
                    uid = doc.get("user_id")
                    if uid is not None:
                        paid_uids.add(str(uid))
                        try: paid_uids.add(int(uid))
                        except: pass

            # 4. Collect from premium_checkout
            if hasattr(db_obj, 'premium_checkout') and db_obj.premium_checkout is not None:
                async for doc in db_obj.premium_checkout.find({"status": "approved"}, {"user_id": 1}):
                    uid = doc.get("user_id")
                    if uid is not None:
                        paid_uids.add(str(uid))
                        try: paid_uids.add(int(uid))
                        except: pass

            if not paid_uids:
                return

            paid_uids_list = list(paid_uids)

            # 5. Delete auto-bans for paid users from premium_bans
            await self.col.update_many(
                {"id": {"$in": paid_uids_list}, "$or": [{"ban_status.ban_reason": {"$regex": "auto-ban|alt of|strike|rapid|evasion", "$options": "i"}}, {"ban_status.is_banned": True}]},
                {"$set": {
                    "ban_status.is_banned": False,
                    "ban_status.ban_reason": "",
                    "banned": False
                }}
            )

            # 7. Collect paid users' IPs and Device IDs
            paid_ips = set()
            paid_devices = set()
            
            async for a_doc in self.db.mini_app_analytics.find({"user_id": {"$in": paid_uids_list}}, {"ip": 1, "data.device_id": 1, "data.fp": 1}):
                ip = a_doc.get("ip")
                if ip and ip not in ["", "127.0.0.1", "::1", "unknown"]:
                    paid_ips.add(ip)
                data_obj = a_doc.get("data") or {}
                d_id = data_obj.get("device_id") or data_obj.get("fp")
                if d_id:
                    paid_devices.add(d_id)

            async for u in self.col.find({"id": {"$in": paid_uids_list}}):
                for ip in u.get("ips", []):
                    if ip: paid_ips.add(ip)
                if u.get("last_ip"): paid_ips.add(u.get("last_ip"))
                for dev in u.get("device_ids", []):
                    if dev: paid_devices.add(dev)
                if u.get("device_id"): paid_devices.add(u.get("device_id"))

            # 8. Pull paid IPs and Device IDs out of ALL records in premium_bans
            if paid_ips or paid_devices:
                conds = []
                if paid_ips: conds.append({"ips": {"$in": list(paid_ips)}})
                if paid_devices: conds.append({"device_ids": {"$in": list(paid_devices)}})
                if conds:
                    all_bans = self.db.premium_bans.find({"$or": conds})
                    async for b_doc in all_bans:
                        c_ips = [i for i in b_doc.get("ips", []) if i not in paid_ips]
                        c_devs = [d for d in b_doc.get("device_ids", []) if d not in paid_devices]
                        r = str(b_doc.get("reason", "")).lower()
                        is_auto = "auto-ban" in r or "alt of" in r or "strike" in r or "rapid" in r
                        if is_auto and (not c_ips or not c_devs or b_doc.get("_id") in paid_uids):
                            await self.db.premium_bans.delete_one({"_id": b_doc["_id"]})
                        else:
                            await self.db.premium_bans.update_one(
                                {"_id": b_doc["_id"]},
                                {"$set": {"ips": c_ips, "device_ids": c_devs}}
                            )

            logger.info(f"✅ Auto-Unblock completed. Cleaned {unbanned_count} paid user ban records.")
        except Exception as e:
            logger.error(f"Failed to auto_unblock_paid_users: {e}")

    async def get_all_users(self):
        return self.col.find({
            'blocked': {'$ne': True},
            'deactivated': {'$ne': True}
        })
    
    async def delete_user(self, user_id):
        await self.col.delete_many({'id': int(user_id)})
        self._invalidate_user_cache(user_id)

    async def reactivate_user(self, user_id):
        await self.col.update_one(
            {'id': int(user_id)},
            {'$unset': {'blocked': '', 'deactivated': ''}}
        )
        self._invalidate_user_cache(user_id)

    async def set_user_status(self, user_id: int, blocked: bool = False, deactivated: bool = False):
        update_doc = {}
        if blocked:
            update_doc['blocked'] = True
        if deactivated:
            update_doc['deactivated'] = True
        if update_doc:
            await self.col.update_one({'id': int(user_id)}, {'$set': update_doc})
            self._invalidate_user_cache(user_id)
 
    async def get_banned(self):
        users = self.col.find({'ban_status.is_banned': True})
        b_users = [user['id'] async for user in users]
        return b_users

    # ── Whitelist System ───────────────────────────────────────────────────────
    async def is_whitelisted(self, user_id: int) -> bool:
        user = await self.col.find_one({'id': int(user_id)})
        return user.get('is_whitelisted', False) if user else False

    async def whitelist_user(self, user_id: int) -> bool:
        await self.col.update_one({'id': int(user_id)}, {'$set': {'is_whitelisted': True}}, upsert=True)
        return True

    async def unwhitelist_user(self, user_id: int) -> bool:
        await self.col.update_one({'id': int(user_id)}, {'$set': {'is_whitelisted': False}}, upsert=True)
        return True

    async def get_whitelisted_users(self) -> list:
        cursor = self.col.find({'is_whitelisted': True})
        return [u async for u in cursor]

    async def update_configs(self, id, configs):
        await self.col.update_one({'id': int(id)}, {'$set': {'configs': configs}})
        self._invalidate_user_cache(id)
         
    async def get_configs(self, id):
        default = {
            'caption': None,
            'duplicate': True,
            'download': False,
            'forward_tag': False,
            'file_size': 0,
            'size_limit': None,
            'extension': None,
            'keywords': None,
            'protect': None,
            'button': None,
            'menu_image_id': None,
            'db_uri': None,
            'duration': 0,
            'bypass_bot': 'Nick_Bypass_Bot',
            'filters': {
               'poll': True,
               'text': True,
               'audio': True,
               'voice': True,
               'video': True,
               'photo': True,
               'document': True,
               'animation': True,
               'sticker': True,
               'rm_caption': False
            }
        }
        user = await self._get_user_doc(id)
        if user:
            user_configs = user.get('configs', {})
            # Merge with default to ensure new fields are populated
            merged = default.copy()
            merged.update(user_configs)
            if 'filters' in user_configs:
                merged_filters = default['filters'].copy()
                merged_filters.update(user_configs['filters'])
                merged['filters'] = merged_filters
            return merged
        return default 
       
    async def add_bot(self, datas):
       is_bot = datas.get('is_bot', True)
       user_id = datas.get('user_id')
       
       # Enforce account limits: 10 for Normal Bots, 8 for Userbots
       limit = 10 if is_bot else 8
       count = await self.bot.count_documents({'user_id': user_id, 'is_bot': is_bot})
       
       is_owner = (await self.is_co_owner(user_id)) or (user_id in Config.OWNER_IDS)
       if not is_owner and count >= limit: 
           return "LIMIT_REACHED"
       exists = await self.bot.find_one({'user_id': datas['user_id'], 'id': datas['id']})
       if exists: return "EXISTS"
       
       total = await self.bot.count_documents({'user_id': datas['user_id']})
       datas['active'] = True if total == 0 else False
       await self.bot.insert_one(datas)
       return True
    
    async def remove_bot(self, user_id, bot_id=None):
       if bot_id:
           await self.bot.delete_one({'user_id': int(user_id), 'id': int(bot_id)})
       else:
           await self.bot.delete_many({'user_id': int(user_id)})
      
    async def get_bot(self, user_id: int, bot_id=None):
       query = {'user_id': user_id}
       if bot_id: query['id'] = int(bot_id)
       bots = self.bot.find(query)
       bots_list = [b async for b in bots]
       if not bots_list: return None
       if bot_id: return bots_list[0]
       
       for b in bots_list:
           if b.get('active'): return b
       return bots_list[0]
                                          
    async def get_bots(self, user_id: int):
       bots = self.bot.find({'user_id': user_id})
       return [b async for b in bots]
       
    async def set_active_bot(self, user_id: int, bot_id: int):
        # Only deactivate accounts of the same type (bot or userbot), not all
        target = await self.bot.find_one({'user_id': user_id, 'id': int(bot_id)})
        if not target: return
        is_bot = target.get('is_bot', True)
        await self.bot.update_many({'user_id': user_id, 'is_bot': is_bot}, {'$set': {'active': False}})
        await self.bot.update_one({'user_id': user_id, 'id': int(bot_id)}, {'$set': {'active': True}})
     
    async def get_active_bot(self, user_id: int):
        """Get the active normal bot for this user."""
        bots = [b async for b in self.bot.find({'user_id': user_id, 'is_bot': True})]
        for b in bots:
            if b.get('active'): return b
        return bots[0] if bots else None

    async def get_active_userbot(self, user_id: int):
        """Get the active userbot for this user."""
        ubots = [b async for b in self.bot.find({'user_id': user_id, 'is_bot': False})]
        for b in ubots:
            if b.get('active'): return b
        return ubots[0] if ubots else None
                                          
    async def is_bot_exist(self, user_id):
       bot = await self.bot.find_one({'user_id': user_id})
       return bool(bot)
                                          
    async def in_channel(self, user_id, chat_id) -> bool:
       try:
           channel = await self.chl.find_one({"user_id": int(user_id), "chat_id": int(chat_id)})
           return bool(channel)
       except (ValueError, TypeError):
           # If chat_id is a string (e.g. username), check by username
           clean_username = str(chat_id).replace("@", "").replace("https://t.me/", "").strip()
           channel = await self.chl.find_one({
               "user_id": int(user_id), 
               "username": {"$regex": f"^{clean_username}$", "$options": "i"}
           })
           return bool(channel)
    
    async def add_channel(self, user_id: int, chat_id: int, title, username):
       channel = await self.in_channel(user_id, chat_id)
       if channel:
         return False
       return await self.chl.insert_one({"user_id": user_id, "chat_id": chat_id, "title": title, "username": username})
    
    async def remove_channel(self, user_id: int, chat_id: int):
       channel = await self.in_channel(user_id, chat_id )
       if not channel:
         return False
       return await self.chl.delete_many({"user_id": int(user_id), "chat_id": int(chat_id)})
    
    async def get_channel_details(self, user_id: int, chat_id: int):
       return await self.chl.find_one({"user_id": int(user_id), "chat_id": int(chat_id)})
       
    async def get_user_channels(self, user_id: int):
       channels = self.chl.find({"user_id": int(user_id)})
       return [channel async for channel in channels]
     
    async def get_filters(self, user_id):
       filters = []
       filter = (await self.get_configs(user_id))['filters']
       for k, v in filter.items():
          if v == False:
            filters.append(str(k))
       return filters

    async def get_bypass_bot(self, user_id: int) -> str:
        configs = await self.get_configs(user_id)
        bot_uname = configs.get('bypass_bot') or 'Nick_Bypass_Bot'
        return bot_uname.strip().lstrip('@')

    async def set_bypass_bot(self, user_id: int, bot_username: str):
        bot_username = bot_username.strip().lstrip('@')
        await self.col.update_one(
            {'id': int(user_id)},
            {'$set': {'configs.bypass_bot': bot_username}},
            upsert=True
        )
        self._invalidate_user_cache(user_id)
              
    async def add_frwd(self, user_id):
       return await self.nfy.insert_one({'user_id': int(user_id)})
    
    async def rmve_frwd(self, user_id=0, all=False):
       data = {} if all else {'user_id': int(user_id)}
       return await self.nfy.delete_many(data)
    
    async def get_all_frwd(self):
       return self.nfy.find({})
    async def get_language(self, user_id: int) -> str:
        """Return user's preferred language: 'en', 'hi', or 'hinglish'. Default 'en'."""
        user = await self._get_user_doc(user_id)
        if user:
            return user.get('language', 'en')
        return 'en'

    async def get_user_selected_language(self, user_id: int):
        """Return user's explicitly chosen language ('hi', 'en', etc.) or None if not chosen."""
        user = await self._get_user_doc(user_id)
        if user and user.get('language'):
            return str(user.get('language')).lower().strip()
        return None

    async def set_language(self, user_id: int, lang: str):
        await self.col.update_one({'id': int(user_id)}, {'$set': {'language': lang}}, upsert=True)
        self._invalidate_user_cache(user_id)

    async def get_total_users_count(self) -> int:
        return await self.col.count_documents({})

    async def get_active_forwardings_count(self) -> int:
        """Count users who are currently running a forwarding task."""
        return await self.nfy.count_documents({})

    async def get_active_jobs_count(self) -> int:
        """Count running Live Jobs."""
        return await self.db.jobs.count_documents({'status': 'running'})


    # ── AI Enhancer Config ────────────────────────────────────────────────────
    async def get_enhancer_config(self) -> dict:
        """Returns the global AI Enhancer config dict."""
        doc = await self.stats.find_one({'_id': 'ai_enhancer_config'})
        return doc or {}

    async def update_enhancer_config(self, **kwargs):
        """Update one or more keys in the AI Enhancer config."""
        await self.stats.update_one(
            {'_id': 'ai_enhancer_config'},
            {'$set': kwargs},
            upsert=True
        )

    # ── Protected Chats ───────────────────────────────────────────────────────
    # A "protected chat" is any source channel/group/bot DM that the owner has
    # marked as off-limits. If a user's job tries to source from one, it gets
    # blocked with a custom error message before any forwarding happens.

    async def get_protected_chats(self) -> list:
        """Returns list of dicts: [{chat_id, title, reason}]"""
        doc = await self.stats.find_one({'_id': 'protected_chats'})
        return doc.get('chats', []) if doc else []

    async def add_protected_chat(self, chat_id: int, title: str = '', reason: str = '') -> bool:
        """Add a chat to the protected list. Returns False if already exists."""
        existing = await self.get_protected_chats()
        if any(str(c['chat_id']) == str(chat_id) for c in existing):
            return False
        await self.stats.update_one(
            {'_id': 'protected_chats'},
            {'$push': {'chats': {'chat_id': str(chat_id), 'title': title, 'reason': reason}}},
            upsert=True
        )
        return True

    async def remove_protected_chat(self, chat_id: int) -> bool:
        """Remove a chat from the protected list. Returns False if not found."""
        existing = await self.get_protected_chats()
        new = [c for c in existing if str(c['chat_id']) != str(chat_id)]
        if len(new) == len(existing):
            return False
        await self.stats.update_one(
            {'_id': 'protected_chats'},
            {'$set': {'chats': new}},
            upsert=True
        )
        return True

    async def is_chat_protected(self, chat_id) -> dict | None:
        """Returns the protected chat doc if chat_id is protected, else None."""
        chats = await self.get_protected_chats()
        for c in chats:
            db_cid = str(c['chat_id'])
            query_cid = str(chat_id)
            if db_cid == query_cid:
                return c
            # Soft match for usernames
            db_clean = db_cid.replace("@", "").replace("https://t.me/", "").strip().lower()
            q_clean = query_cid.replace("@", "").replace("https://t.me/", "").strip().lower()
            if db_clean and q_clean and db_clean == q_clean:
                return c
        return None

    # ── Multi-Owner / Co-Owner System ─────────────────────────────────────────
    # The primary owner(s) come from .env BOT_OWNER_ID.
    # Co-owners are stored in DB and have the same privileges EXCEPT they
    # cannot add/remove other co-owners (only primary can do that).

    async def get_co_owners(self) -> list:
        """Returns list of co-owner user IDs (ints)."""
        doc = await self.stats.find_one({'_id': 'co_owners'})
        return [int(x) for x in doc.get('ids', [])] if doc else []

    async def add_co_owner(self, user_id: int) -> bool:
        """Add a co-owner. Returns False if already exists."""
        existing = await self.get_co_owners()
        if user_id in existing:
            return False
        await self.stats.update_one(
            {'_id': 'co_owners'},
            {'$addToSet': {'ids': str(user_id)}},
            upsert=True
        )
        return True

    async def remove_co_owner(self, user_id: int) -> bool:
        """Remove a co-owner. Returns False if not found."""
        existing = await self.get_co_owners()
        if user_id not in existing:
            return False
        await self.stats.update_one(
            {'_id': 'co_owners'},
            {'$pull': {'ids': str(user_id)}},
        )
        return True

    async def is_co_owner(self, user_id: int) -> bool:
        co = await self.get_co_owners()
        return user_id in co

    # ── Per-User Limits ───────────────────────────────────────────────────────
    # Owners are unlimited. Regular users are capped by global default or
    # per-user overrides set by the owner.
    # Limit keys: max_live_jobs, max_multi_jobs, max_merge_jobs, max_accounts

    async def get_global_user_limits(self) -> dict:
        """Returns global defaults applied to non-owner users."""
        doc = await self.stats.find_one({'_id': 'global_user_limits'})
        defaults = {'max_live_jobs': 65, 'max_multi_jobs': 2,
                    'max_merge_jobs': 1, 'max_accounts': 5}
        if not doc:
            return defaults
        res = {**defaults, **doc}
        if res.get('max_live_jobs', 0) <= 45:
            res['max_live_jobs'] = 65
        return res

    async def set_global_user_limits(self, **kwargs):
        await self.stats.update_one(
            {'_id': 'global_user_limits'},
            {'$set': kwargs},
            upsert=True
        )

    async def get_user_limits(self, user_id: int) -> dict:
        """Per-user override. Falls back to global limits if not set."""
        doc = await self.col.find_one({'_id': user_id})
        overrides = doc.get('limits', {}) if doc else {}
        defaults = await self.get_global_user_limits()
        return {**defaults, **overrides}

    async def set_user_limit(self, user_id: int, key: str, value: int):
        """Set a specific limit for a user. value=-1 means unlimited."""
        await self.col.update_one(
            {'_id': user_id},
            {'$set': {f'limits.{key}': value}},
            upsert=True
        )

    async def reset_user_limits(self, user_id: int):
        """Remove per-user limit overrides, reverting to global defaults."""
        await self.col.update_one(
            {'_id': user_id},
            {'$unset': {'limits': ''}}
        )

    async def get_shortener_apis(self) -> dict:
        doc = await self.db.config.find_one({"_id": "shortener_apis"})
        return doc or {"_id": "shortener_apis", "arolinks": "", "urlshortx": ""}

    async def update_shortener_apis(self, key: str, val: str):
        await self.db.config.update_one(
            {"_id": "shortener_apis"},
            {"$set": {key: val}},
            upsert=True
        )


    # ── Abuse / Strike Tracking ───────────────────────────────────────────────
    # Each strike record stores {count, last_delivery_ts, last_strike_ts}
    # so the delivery bot can decide whether the window has expired.

    async def get_user_strike(self, user_id: int) -> dict:
        """Return the current abuse-strike record for this user."""
        doc = await self.col.find_one({'id': int(user_id)})
        return (doc or {}).get('abuse_strike', {
            'count': 0,
            'last_delivery_ts': 0.0,
            'last_strike_ts': 0.0,
        })

    async def update_user_strike(self, user_id: int, count: int,
                                  last_delivery_ts: float = None,
                                  last_strike_ts: float = None) -> None:
        """Update the abuse-strike record atomically."""
        import time as _t
        now = _t.time()
        update_fields = {'abuse_strike.count': count}
        if last_delivery_ts is not None:
            update_fields['abuse_strike.last_delivery_ts'] = last_delivery_ts
        if last_strike_ts is not None:
            update_fields['abuse_strike.last_strike_ts'] = last_strike_ts
        await self.col.update_one(
            {'id': int(user_id)},
            {'$set': update_fields},
            upsert=True
        )

    async def reset_user_strike(self, user_id: int) -> None:
        """Reset all strike data for this user (called after ban or manual reset)."""
        await self.col.update_one(
            {'id': int(user_id)},
            {'$unset': {'abuse_strike': ''}},
        )

    # ── Logs Channel Config ───────────────────────────────────────────────────
    # Stored in global_stats so owners can set it from the Settings UI
    # without needing to touch .env or restart the bot.

    async def get_logs_config(self) -> dict:
        """
        Returns the logs configuration dict — one channel ID per log type:
        {
          'ch_bans':      int or 0,   # Channel for ban/warn events
          'ch_new_users': int or 0,   # Channel for new-user events
          'ch_batch':     int or 0,   # Channel for batch-link creation
          'ch_live':      int or 0,   # Channel for live-job events
          'ch_cleaner':   int or 0,   # Channel for cleaner-job events
          'ch_errors':    int or 0,   # Channel for error events
        }
        Each key is an independent Telegram channel.  0 = not configured (silent).
        """
        doc = await self.stats.find_one({'_id': 'logs_config'})
        defaults = {
            'ch_bans':      0,
            'ch_new_users': 0,
            'ch_batch':     0,
            'ch_live':      0,
            'ch_cleaner':   0,
            'ch_errors':    0,
        }
        if not doc:
            return defaults
        # Auto-migrate old schema: if old 'channel_id' key exists and no new keys,
        # keep returning zeros so UI prompts fresh configuration.
        result = {**defaults}
        for k, v in doc.items():
            if k != '_id' and k in defaults:
                result[k] = v
        return result

    async def set_logs_config(self, **kwargs) -> None:
        """Set one or more logs config keys (ch_bans, ch_new_users, etc.)."""
        # Only persist recognised keys to avoid storing old schema fields
        _VALID = {'ch_bans', 'ch_new_users', 'ch_batch', 'ch_live', 'ch_cleaner', 'ch_errors'}
        filtered = {k: v for k, v in kwargs.items() if k in _VALID}
        if not filtered:
            return
        await self.stats.update_one(
            {'_id': 'logs_config'},
            {'$set': filtered},
            upsert=True
        )

    # ── Anti-Abuse Toggle ─────────────────────────────────────────────────────
    # Stored in global_stats so owner can toggle from Settings UI without
    # touching .env or restarting the bot.

    async def get_anti_abuse_enabled(self) -> bool:
        """Returns True if the Anti-Abuse system is enabled (default: True)."""
        doc = await self.stats.find_one({'_id': 'anti_abuse_config'})
        if not doc:
            return True   # ON by default
        return doc.get('enabled', True)

    async def set_anti_abuse_enabled(self, enabled: bool) -> None:
        """Enable or disable the Anti-Abuse system globally."""
        await self.stats.update_one(
            {'_id': 'anti_abuse_config'},
            {'$set': {'enabled': enabled}},
            upsert=True
        )

    async def get_anti_abuse_config(self) -> dict:
        """
        Returns full Anti-Abuse config:
        {
          'enabled':      bool,  # Master on/off switch
          'cooldown_secs': int,  # Seconds between requests before counting as rapid
          'max_strikes':   int,  # Strikes before auto-ban
        }
        """
        doc = await self.stats.find_one({'_id': 'anti_abuse_config'})
        defaults = {
            'enabled':       True,
            'cooldown_secs': 60,
            'max_strikes':   5,
        }
        if not doc:
            return defaults
        result = {**defaults}
        for k in defaults:
            if k in doc:
                result[k] = doc[k]
        return result

    async def set_anti_abuse_config(self, **kwargs) -> None:
        """Set one or more anti-abuse config keys."""
        _VALID = {'enabled', 'cooldown_secs', 'max_strikes'}
        filtered = {k: v for k, v in kwargs.items() if k in _VALID}
        if not filtered:
            return
        await self.stats.update_one(
            {'_id': 'anti_abuse_config'},
            {'$set': filtered},
            upsert=True
        )



    async def add_share_bot_seen_user(self, bot_id: str, user_id: int) -> bool:
        """
        Record that user_id has started bot_id for the first time.
        Returns True if this IS a new user for this specific bot, False if already seen.
        """
        if not bot_id:
            return False
        import time
        res = await self.share_users.update_one(
            {'bot_id': str(bot_id), 'user_id': int(user_id)},
            {'$setOnInsert': {'first_seen': time.time()}},
            upsert=True
        )
        return bool(res.upserted_id)

    # ── Share Bot Delivery Tracker (For Purging) ──────────────────────────────
    async def track_delivery(self, bot_id: str, user_id: int, msg_ids: list):
        if not bot_id or not msg_ids: return
        import time
        doc = {
            'bot_id': str(bot_id),
            'user_id': int(user_id),
            'msg_ids': msg_ids,
            'timestamp': time.time()
        }
        await self.share_deliveries.insert_one(doc)

    async def get_deliveries(self, bot_id: str):
        cursor = self.share_deliveries.find({'bot_id': str(bot_id)})
        return [doc async for doc in cursor]

    async def remove_delivery_record(self, doc_id):
        from bson.objectid import ObjectId
        await self.share_deliveries.delete_one({'_id': ObjectId(doc_id) if isinstance(doc_id, str) else doc_id})

    async def clear_all_deliveries(self, bot_id: str):
        await self.share_deliveries.delete_many({'bot_id': str(bot_id)})

    # ── Delivery Rate Limit & Pass Methods ────────────────────────────────────
    async def get_delivery_rate_limit_config(self) -> dict:
        """Returns delivery rate limit and pass config (cached for 15s)."""
        import time as _t
        now = _t.time()
        if hasattr(self, '_rl_cfg_cache') and self._rl_cfg_cache is not None:
            cached_val, expiry = self._rl_cfg_cache
            if now < expiry:
                return dict(cached_val)

        doc = await self.stats.find_one({'_id': 'delivery_rate_limit_config'})
        defaults = {
            'enabled': True,
            'max_limit': 5,
            'window_seconds': 43200,
            'window_hours': 12,
            'log_channel': None,               # Pass purchase log channel
            'rate_limit_log_channel': None,    # Rate limit hit log channel
            'prices': {'1d': 15, '3d': 30, '7d': 55, '1mo': 250, '6mo': 1199},
            'cashfree_app_id': '',
            'cashfree_secret_key': '',
            'cashfree_env': 'production',
            'upi_id': '',
            'upi_name': 'Arya Delivery Pass',
            'gmail_user': '',
            'gmail_app_password': '',
            'oxapay_key': '',
            'oxapay_env': 'production',
            'oxapay_enabled': True,
            'upi_enabled': True,
            'pass_ui_version': 'v1',
            'v2_gateway': 'cashfree'
        }
        if not doc:
            return defaults
        res = {**defaults}
        for k, v in doc.items():
            if k != '_id':
                res[k] = v if v is not None else defaults.get(k, '')
        # If prices is not set or empty, set default
        p = res.get('prices')
        if not isinstance(p, dict) or not p:
            res['prices'] = defaults['prices']
        # Ensure window_seconds is properly initialized and synced
        if 'window_seconds' not in doc and 'window_hours' in doc:
            res['window_seconds'] = int(float(doc['window_hours']) * 3600)
        elif 'window_seconds' in doc:
            res['window_hours'] = round(float(doc['window_seconds']) / 3600.0, 2)
        if hasattr(self, '_rl_cfg_cache'):
            self._rl_cfg_cache = (dict(res), now + 15)  # Cache for 15s
        return res

    async def set_delivery_rate_limit_config(self, **kwargs) -> None:
        """Update delivery rate limit and pass config."""
        _VALID = {
            'enabled', 'max_limit', 'window_seconds', 'window_hours', 'log_channel',
            'rate_limit_log_channel', 'prices', 'cashfree_app_id', 'cashfree_secret_key',
            'cashfree_env', 'upi_id', 'upi_name', 'gmail_user', 'gmail_app_password',
            'oxapay_key', 'oxapay_env', 'oxapay_enabled', 'upi_enabled', 'pass_ui_version',
            'v2_gateway'
        }
        filtered = {k: v for k, v in kwargs.items() if k in _VALID}
        if not filtered:
            return
        if 'window_seconds' in filtered and 'window_hours' not in filtered:
            filtered['window_hours'] = round(float(filtered['window_seconds']) / 3600.0, 2)
        elif 'window_hours' in filtered and 'window_seconds' not in filtered:
            filtered['window_seconds'] = int(float(filtered['window_hours']) * 3600)
            
        if hasattr(self, '_rl_cfg_cache'):
            self._rl_cfg_cache = None
        await self.stats.update_one(
            {'_id': 'delivery_rate_limit_config'},
            {'$set': filtered},
            upsert=True
        )

    async def get_all_claimed_utrs(self) -> set:
        """Returns set of all UTR strings that have already been recorded to prevent duplicate email matching."""
        try:
            docs = await self.used_utrs.find({}, {'utr': 1}).to_list(5000)
            return {str(d.get('utr', '')).strip() for d in docs if d.get('utr')}
        except Exception:
            return set()

    async def is_utr_claimed_by_other(self, utr: str, user_id: int, order_id: str = "") -> bool:
        """
        Check if a UTR has already been claimed by a DIFFERENT user or for an already active pass.
        Returns False if the UTR was used by THIS user for this pending order or if the user's pass is inactive.
        """
        if not utr:
            return False
        clean_utr = str(utr).strip()
        doc = await self.used_utrs.find_one({'utr': clean_utr})
        if not doc:
            return False
        
        doc_uid = doc.get('user_id')
        doc_oid = doc.get('order_id', '')
        
        # If it was recorded for the same user
        if int(doc_uid) == int(user_id):
            p_info = await self.get_user_unlimited_pass(user_id)
            # If user pass is inactive OR same order, allow claim/re-activation
            if not p_info.get('active') or (order_id and doc_oid == order_id):
                return False
        
        # Otherwise it belongs to another user
        return True

    async def get_other_claimed_utrs(self, user_id: int = None, order_id: str = "") -> set:
        """
        Returns set of UTRs claimed by OTHER users.
        Does NOT exclude the current user's UTR so their legitimate payment is never skipped.
        """
        try:
            if not user_id:
                docs = await self.used_utrs.find({}, {'utr': 1}).to_list(5000)
                return {str(d.get('utr', '')).strip() for d in docs if d.get('utr')}

            docs = await self.used_utrs.find({
                'user_id': {'$ne': int(user_id)}
            }, {'utr': 1, 'user_id': 1}).to_list(5000)
            
            return {str(d.get('utr', '')).strip() for d in docs if d.get('utr')}
        except Exception:
            return set()

    async def is_utr_used(self, utr: str) -> bool:
        """Check if a UTR has already been claimed/used for pass activation."""
        doc = await self.used_utrs.find_one({'utr': str(utr).strip()})
        return bool(doc)

    async def mark_utr_used(self, utr: str, user_id: int, amount: float, plan: str, user_name: str = "", order_id: str = ""):
        """Mark a UTR as consumed to prevent replay attacks."""
        import time
        doc = {
            'utr': str(utr).strip(),
            'user_id': int(user_id),
            'amount': float(amount),
            'plan': str(plan),
            'used_at': time.time()
        }
        if user_name:
            doc['user_name'] = str(user_name).strip()
        if order_id:
            doc['order_id'] = str(order_id).strip()
        await self.used_utrs.update_one(
            {'utr': str(utr).strip()},
            {'$set': doc},
            upsert=True
        )

    async def record_used_utr(self, utr: str, user_id: int, amount: float, order_id: str = "", plan: str = "1d", user_name: str = "", gateway: str = "Pay Via UPI (INR)"):
        """Record used UTR helper (alias for mark_utr_used)."""
        return await self.mark_utr_used(utr=utr, user_id=user_id, amount=amount, plan=plan, user_name=user_name, order_id=order_id)

    async def get_user_pass_transactions(self, user_id: int, limit: int = 15, paid_only: bool = False) -> list:
        """Fetch pass orders (PAID, PENDING, FAILED or PAID-only) and verified UTRs for user."""
        import time
        now = time.time()
        orders = await self.pass_orders.find({'user_id': int(user_id)}).sort('created_at', -1).limit(limit * 3).to_list(limit * 3)
        utrs = await self.used_utrs.find({'user_id': int(user_id)}).sort('used_at', -1).limit(limit).to_list(limit)
        results = []
        seen_order_ids = set()

        for o in orders:
            oid = str(o.get('order_id', '')).strip()
            if not oid:
                continue
            seen_order_ids.add(oid)

            gw_raw = str(o.get('gateway') or '').lower()
            if 'oxa' in oid.lower() or 'crypto' in gw_raw or 'oxapay' in gw_raw:
                gw_display = "Crypto ( Oxapay )"
            elif 'upi' in oid.lower() or 'upi' in gw_raw:
                gw_display = "Manual UPI"
            else:
                gw_display = "Cashfree"

            raw_status = str(o.get('status') or 'PENDING').upper()
            c_time = float(o.get('paid_at') or o.get('created_at') or o.get('time') or now)

            if raw_status in ('PAID', 'SUCCESS', 'COMPLETED'):
                final_status = 'PAID'
            elif raw_status in ('FAILED', 'CANCELLED', 'EXPIRED'):
                final_status = 'FAILED'
            else:
                # PENDING: check if older than 10 minutes (600 seconds)
                exp_ts = float(o.get('expires_at') or (c_time + 600))
                if (now - c_time > 600) or (now > exp_ts):
                    final_status = 'FAILED'
                else:
                    final_status = 'PENDING'

            if paid_only and final_status != 'PAID':
                continue

            plan_val = o.get('duration_key') or o.get('plan') or o.get('duration') or '1d'

            results.append({
                'id': oid,
                'amount': float(o.get('amount', 0.0)),
                'plan': str(plan_val),
                'status': final_status,
                'time': c_time,
                'gateway': gw_display
            })

        for u in utrs:
            oid = u.get('order_id') or f"UPI_{user_id}_{int(u.get('used_at', 0))}"
            if oid not in seen_order_ids:
                seen_order_ids.add(oid)
                results.append({
                    'id': oid,
                    'utr': u.get('utr', ''),
                    'amount': float(u.get('amount', 0.0)),
                    'plan': str(u.get('plan') or '1d'),
                    'status': 'PAID',
                    'time': float(u.get('used_at', 0)),
                    'gateway': "Manual UPI"
                })

        results.sort(key=lambda x: x.get('time', 0), reverse=True)
        return results[:limit]

    async def record_user_delivery_hit(self, user_id: int):
        """Record a successful delivery hit with timestamp."""
        import time
        await self.delivery_hits.insert_one({
            'user_id': int(user_id),
            'timestamp': time.time()
        })

    async def get_user_delivery_hits(self, user_id: int, window_seconds: int = 43200) -> list:
        """Get user delivery timestamps within rolling window (oldest to newest)."""
        import time
        cutoff = time.time() - window_seconds
        cursor = self.delivery_hits.find({
            'user_id': int(user_id),
            'timestamp': {'$gte': cutoff}
        }).sort('timestamp', 1)
        return [doc async for doc in cursor]

    async def get_user_unlimited_pass(self, user_id: int) -> dict:
        """Check if user has an active unlimited delivery pass."""
        import time
        doc = await self.unlimited_passes.find_one({'user_id': int(user_id)})
        expires_at = float(doc.get('expires_at', 0.0)) if doc else 0.0
        now = time.time()
        active = bool(expires_at > now)
        rem_sec = max(0, int(expires_at - now))
        days_left = max(0.0, rem_sec / 86400.0)
        
        if not active:
            time_left_str = "Expired"
        elif rem_sec < 3600:
            time_left_str = f"{rem_sec // 60}m"
        elif rem_sec < 86400:
            time_left_str = f"{rem_sec // 3600}h {(rem_sec % 3600) // 60}m"
        else:
            time_left_str = f"{round(days_left, 1)}d"

        return {
            'active': active,
            'expires_at': expires_at,
            'days_left': round(days_left, 1),
            'rem_seconds': rem_sec,
            'time_left_str': time_left_str,
            'bot_id': doc.get('bot_id') if doc else None,
            'bot_username': doc.get('bot_username') if doc else None,
            'user_name': doc.get('user_name') if doc else ""
        }

    async def set_user_unlimited_pass(self, user_id: int, expiry_timestamp: float, user_name: str = "", bot_id: int = None, bot_username: str = "", plan_key: str = "", plan_name: str = "", amount: float = 0.0, savings: float = 0.0):
        """Set or update unlimited pass expiry and optional plan details."""
        import time
        doc = {'expires_at': float(expiry_timestamp), 'updated_at': time.time()}
        if user_name:
            doc['user_name'] = str(user_name).strip()
        if bot_id:
            doc['bot_id'] = int(bot_id)
        if bot_username:
            doc['bot_username'] = str(bot_username).strip()
        if plan_key:
            doc['plan_key'] = str(plan_key).strip()
        if plan_name:
            doc['plan_name'] = str(plan_name).strip()
        if amount > 0:
            doc['amount'] = float(amount)
        if savings > 0:
            doc['savings'] = float(savings)
        await self.unlimited_passes.update_one(
            {'user_id': int(user_id)},
            {'$set': doc},
            upsert=True
        )

    async def grant_user_unlimited_pass(self, user_id: int, duration, user_name: str = "", bot_id: int = None, bot_username: str = "", plan_key: str = "", plan_name: str = "", amount: float = 0.0, savings: float = 0.0) -> float:
        """Extend or activate unlimited pass for specified duration (days int or duration str like '30m', '2h', '7d') and return new expiry."""
        import time
        if isinstance(duration, (int, float)) and duration < 1000:
            duration_seconds = float(duration) * 86400.0
            dur_key_str = f"{int(duration)}d"
        elif isinstance(duration, str):
            dur_key_str = duration
            duration_seconds = float(parse_duration_to_seconds(duration, default_unit='d'))
        else:
            dur_key_str = str(duration)
            duration_seconds = float(duration)

        p_key = plan_key or dur_key_str
        if not plan_name and p_key:
            try:
                p_name = f"{format_duration_verbose(duration_seconds).title()} Unlimited Pass"
            except Exception:
                p_name = f"{p_key} Unlimited Pass"
        else:
            p_name = plan_name

        cur = await self.get_user_unlimited_pass(user_id)
        now = time.time()
        base_time = cur['expires_at'] if (cur['active'] and cur['expires_at'] > now) else now
        new_expiry = base_time + duration_seconds
        b_id = bot_id or cur.get('bot_id')
        b_uname = bot_username or cur.get('bot_username')
        await self.set_user_unlimited_pass(
            user_id=user_id,
            expiry_timestamp=new_expiry,
            user_name=user_name,
            bot_id=b_id,
            bot_username=b_uname,
            plan_key=p_key,
            plan_name=p_name,
            amount=amount,
            savings=savings
        )
        return new_expiry

    async def activate_user_unlimited_pass(self, user_id: int, duration_seconds: float, order_id: str = "", amount: float = 0.0, gateway: str = "", user_name: str = "") -> float:
        """Helper to activate or extend user pass by duration seconds."""
        return await self.grant_user_unlimited_pass(user_id=user_id, duration=duration_seconds, user_name=user_name)

    async def reduce_user_unlimited_pass(self, user_id: int, duration) -> float:
        """Reduce user's unlimited pass by specified duration (days int or duration str like '1d', '3h', '30m')."""
        import time
        if isinstance(duration, (int, float)) and duration < 1000:
            duration_seconds = float(duration) * 86400.0
        elif isinstance(duration, str):
            duration_seconds = float(parse_duration_to_seconds(duration, default_unit='d'))
        else:
            duration_seconds = float(duration)

        cur = await self.get_user_unlimited_pass(user_id)
        now = time.time()
        if not cur['active']:
            return 0.0

        new_expiry = cur['expires_at'] - duration_seconds
        if new_expiry <= now:
            await self.revoke_user_unlimited_pass(user_id)
            return 0.0
        else:
            await self.set_user_unlimited_pass(user_id, new_expiry)
            return new_expiry

    async def revoke_user_unlimited_pass(self, user_id: int):
        """Revoke user's unlimited pass while preserving history and records."""
        import time
        await self.unlimited_passes.update_one(
            {'user_id': int(user_id)},
            {'$set': {'expires_at': 0.0, 'revoked_at': time.time()}},
            upsert=False
        )

    async def create_pass_order(self, order_dict: dict):
        """Save a pending pass order."""
        await self.pass_orders.insert_one(order_dict)

    async def get_pass_order(self, order_id: str) -> dict:
        """Fetch a pass order by order_id."""
        return await self.pass_orders.find_one({'order_id': order_id})

    async def mark_pass_order_paid(self, order_id: str, payment_details: dict = None):
        """Mark pass order as PAID."""
        import time
        await self.pass_orders.update_one(
            {'order_id': order_id},
            {'$set': {'status': 'PAID', 'paid_at': time.time(), 'payment_details': payment_details or {}}}
        )

    async def mark_pass_order_paid_atomic(self, order_id: str, payment_details: dict = None) -> dict:
        """
        Atomically marks a pass order as PAID only if it is currently NOT PAID.
        Returns the order document if transition succeeded, or None if already PAID/processed.
        This provides strict idempotency and completely prevents double credit / multiple activations.
        """
        import time
        doc = await self.pass_orders.find_one_and_update(
            {'order_id': order_id, 'status': {'$ne': 'PAID'}},
            {'$set': {'status': 'PAID', 'paid_at': time.time(), 'payment_details': payment_details or {}}},
            return_document=False
        )
        return doc


    async def get_all_pass_customers(self) -> list:
        """
        Fetch all users who hold or ever held a pass, or have pass transactions.
        Returns sorted list of dicts:
        {
            'user_id': int,
            'name': str,
            'active': bool,
            'expires_at': float,
            'days_left': float,
            'time_left_str': str,
            'updated_at': float
        }
        """
        import time
        now = time.time()
        users_map = {}

        # 1. Check unlimited_passes
        async for doc in self.unlimited_passes.find({}):
            uid = doc.get('user_id')
            if not uid:
                continue
            uid = int(uid)
            exp = float(doc.get('expires_at', 0))
            u_name = doc.get('user_name', '')
            users_map[uid] = {
                'user_id': uid,
                'name': u_name,
                'expires_at': exp,
                'updated_at': float(doc.get('updated_at', exp))
            }

        # 2. Check used_utrs
        async for doc in self.used_utrs.find({}):
            uid = doc.get('user_id')
            if not uid:
                continue
            uid = int(uid)
            u_name = doc.get('user_name', '')
            if uid not in users_map:
                users_map[uid] = {
                    'user_id': uid,
                    'name': u_name,
                    'expires_at': 0.0,
                    'updated_at': float(doc.get('used_at', 0))
                }
            elif not users_map[uid]['name'] and u_name:
                users_map[uid]['name'] = u_name

        # 3. Check pass_orders
        async for doc in self.pass_orders.find({'status': 'PAID'}):
            uid = doc.get('user_id')
            if not uid:
                continue
            uid = int(uid)
            u_name = doc.get('user_name') or doc.get('customer_name', '')
            if uid not in users_map:
                users_map[uid] = {
                    'user_id': uid,
                    'name': u_name,
                    'expires_at': 0.0,
                    'updated_at': float(doc.get('paid_at') or doc.get('created_at', 0))
                }
            elif not users_map[uid]['name'] and u_name:
                users_map[uid]['name'] = u_name

        # Priority: ALWAYS resolve Telegram user names for ALL customer IDs from Telegram users collection (self.col)
        all_uids = list(users_map.keys())
        if all_uids:
            try:
                cursor = self.col.find({'id': {'$in': all_uids}}, {'id': 1, 'name': 1})
                async for udoc in cursor:
                    u_id = udoc.get('id')
                    tg_name = udoc.get('name')
                    if u_id in users_map and tg_name:
                        users_map[u_id]['name'] = tg_name
            except Exception:
                pass

        results = []
        for uid, data in users_map.items():
            name = data.get('name') or f"User {uid}"
            data['name'] = str(name)[:25]

            exp = data['expires_at']
            active = bool(exp > now)
            rem_sec = int(exp - now) if active else 0
            days_left = (exp - now) / 86400.0 if active else 0.0

            if not active:
                if exp > 0:
                    time_left_str = "Expired"
                else:
                    time_left_str = "No Active Pass"
            elif rem_sec < 3600:
                time_left_str = f"{rem_sec // 60}m"
            elif rem_sec < 86400:
                time_left_str = f"{rem_sec // 3600}h {(rem_sec % 3600) // 60}m"
            else:
                time_left_str = f"{round(days_left, 1)}d"

            data['active'] = active
            data['days_left'] = round(days_left, 1)
            data['rem_seconds'] = rem_sec
            data['time_left_str'] = time_left_str
            results.append(data)

        # Sort: active first (descending by expires_at), then expired (descending by expires_at / updated_at)
        results.sort(key=lambda x: (1 if x['active'] else 0, x['expires_at'], x['updated_at']), reverse=True)
        return results

    async def get_customer_full_details(self, user_id: int) -> dict:
        """Fetch customer profile, pass status, joined date, first buy date, language, and full transaction history."""
        user_id = int(user_id)
        pass_info = await self.get_user_unlimited_pass(user_id)
        
        name = ""
        username = ""
        joined_ts = None
        # Priority 1: Check Telegram users collection (self.col)
        try:
            u_doc = await self.col.find_one({'id': user_id})
            if u_doc:
                if u_doc.get('name'):
                    name = u_doc['name']
                if u_doc.get('username'):
                    username = str(u_doc['username']).lstrip('@')
                if u_doc.get('created_at'):
                    joined_ts = float(u_doc['created_at'])
                elif u_doc.get('_id'):
                    joined_ts = u_doc['_id'].generation_time.timestamp()
        except Exception:
            pass

        # Check mini app users collection if username still empty
        if not username:
            try:
                mu_doc = await self.users.find_one({'user_id': user_id}) or await self.users.find_one({'id': user_id})
                if mu_doc and mu_doc.get('username'):
                    username = str(mu_doc['username']).lstrip('@')
            except Exception:
                pass

        # Priority 2: Check unlimited_passes
        pass_doc = None
        try:
            pass_doc = await self.unlimited_passes.find_one({'user_id': user_id})
            if pass_doc:
                if not name and pass_doc.get('user_name'):
                    name = pass_doc['user_name']
                if not username and pass_doc.get('username'):
                    username = str(pass_doc['username']).lstrip('@')
                if not joined_ts and pass_doc.get('_id'):
                    joined_ts = pass_doc['_id'].generation_time.timestamp()
        except Exception:
            pass

        # Priority 3: Check pass_orders
        if not name:
            try:
                order_doc = await self.pass_orders.find_one({'user_id': user_id, 'user_name': {'$exists': True, '$ne': ''}})
                if order_doc and order_doc.get('user_name'):
                    name = order_doc['user_name']
            except Exception:
                pass

        if not name:
            name = f"User {user_id}"

        txns = await self.get_user_pass_transactions(user_id, limit=25, paid_only=True)

        # First Buy Timestamp: earliest paid transaction
        first_buy_ts = None
        if txns:
            valid_times = [t.get('time', 0) for t in txns if t.get('time', 0) > 0]
            if valid_times:
                first_buy_ts = min(valid_times)

        # Language
        user_lang = "en"
        try:
            user_lang = await self.get_language(user_id) or "en"
        except Exception:
            pass

        return {
            'user_id': user_id,
            'name': name,
            'username': username,
            'joined_ts': joined_ts,
            'first_buy_ts': first_buy_ts,
            'language': user_lang,
            'pass_info': pass_info,
            'transactions': txns
        }

    async def get_next_pass_order_number(self) -> int:
        """Atomically get next unique sequential order number."""
        import time
        try:
            from pymongo import ReturnDocument
            doc = await self.stats.find_one_and_update(
                {'_id': 'pass_order_counter'},
                {'$inc': {'count': 1}},
                upsert=True,
                return_document=ReturnDocument.AFTER
            )
            return doc.get('count', 1)
        except Exception:
            return int(time.time() % 100000)

    async def ensure_indexes(self):
        try:
            await self.col.create_index("id", unique=True, background=True)
        except Exception: pass
        try:
            await self.bot.create_index("user_id", background=True)
        except Exception: pass
        try:
            await self.chl.create_index("user_id", background=True)
        except Exception: pass
        try:
            await self.db.jobs.create_index("user_id", background=True)
        except Exception: pass
        try:
            await self.db.cleaner_jobs.create_index("user_id", background=True)
        except Exception: pass
        try:
            await self.share_deliveries.create_index("bot_id", background=True)
        except Exception: pass
        try:
            await self.share_users.create_index([("bot_id", 1), ("user_id", 1)], unique=True, background=True)
        except Exception: pass
        try:
            await self.delivery_hits.create_index([("user_id", 1), ("timestamp", -1)], background=True)
        except Exception: pass
        try:
            await self.unlimited_passes.create_index("user_id", unique=True, background=True)
        except Exception: pass
        try:
            await self.pass_orders.create_index("order_id", unique=True, background=True)
        except Exception: pass

        # Self-migration routine: migrate old seen_users_* and bot_* configs to the new share_users collection
        try:
            import logging
            import time
            from pymongo import UpdateOne
            mig_logger = logging.getLogger(__name__)
            
            # 1. Migrate seen_users_{bot_id} documents from stats collection
            async for doc in self.stats.find({"_id": {"$regex": "^seen_users_"}}):
                doc_id = doc.get("_id", "")
                bot_id = doc_id.replace("seen_users_", "", 1)
                user_ids = doc.get("ids", [])
                if not bot_id or not user_ids:
                    continue
                mig_logger.info(f"[Migration] Migrating {len(user_ids)} users from old {doc_id} stats document via bulk_write...")
                
                requests = [
                    UpdateOne(
                        {'bot_id': str(bot_id), 'user_id': int(uid)},
                        {'$setOnInsert': {'first_seen': time.time()}},
                        upsert=True
                    ) for uid in user_ids
                ]
                
                for i in range(0, len(requests), 1000):
                    batch = requests[i:i+1000]
                    await self.share_users.bulk_write(batch, ordered=False)
                
                # Delete the old document since it's fully migrated
                await self.stats.delete_one({"_id": doc_id})
                mig_logger.info(f"[Migration] Successfully migrated {len(user_ids)} users and deleted old {doc_id}.")
                
            # 2. Migrate bot_{bot_id} config 'users' array from share_config collection
            async for doc in self.share_config.find({"_id": {"$regex": "^bot_"}, "users": {"$exists": True}}):
                doc_id = doc.get("_id", "")
                bot_id = doc_id.replace("bot_", "", 1)
                # Ignore stats docs like seen_users if any got here
                if bot_id.startswith("seen_users_") or not bot_id:
                    continue
                user_ids = doc.get("users", [])
                if not user_ids:
                    continue
                mig_logger.info(f"[Migration] Migrating {len(user_ids)} users from old {doc_id} config document via bulk_write...")
                
                requests = [
                    UpdateOne(
                        {'bot_id': str(bot_id), 'user_id': int(uid)},
                        {'$setOnInsert': {'first_seen': time.time()}},
                        upsert=True
                    ) for uid in user_ids
                ]
                
                for i in range(0, len(requests), 1000):
                    batch = requests[i:i+1000]
                    await self.share_users.bulk_write(batch, ordered=False)
                
                # Unset the users array so it doesn't bloat the config doc
                await self.share_config.update_one({"_id": doc_id}, {"$unset": {"users": ""}})
                mig_logger.info(f"[Migration] Successfully migrated {len(user_ids)} users and cleaned old 'users' field from {doc_id}.")
                
        except Exception as _m_err:
            import logging
            logging.getLogger(__name__).error(f"[Migration] Exception in user migration: {_m_err}")

    # ── Store / Pay-Per-Show Catalog & Orders ─────────────────────────────────
    async def save_store_show(self, show_data: dict):
        """Save or update an indexed show in store_shows collection."""
        show_id = show_data.get('show_id')
        if not show_id:
            import uuid
            show_id = str(uuid.uuid4())[:8]
            show_data['show_id'] = show_id
        await self.store_shows.update_one(
            {'$or': [{'show_id': show_id}, {'clean_title': show_data.get('clean_title', '')}]},
            {'$set': show_data},
            upsert=True
        )
        return show_id

    async def get_store_show(self, show_id: str) -> dict | None:
        """Fetch a single show by its show_id."""
        return await self.store_shows.find_one({'show_id': str(show_id)})

    async def get_store_show_by_title(self, clean_title: str) -> dict | None:
        """Fetch a show by clean title (deduplication check)."""
        return await self.store_shows.find_one({'clean_title': clean_title})

    async def get_all_store_shows(self, limit: int = 50, offset: int = 0) -> list:
        """Returns paginated list of all shows."""
        cursor = self.store_shows.find().sort('created_at', -1).skip(offset).limit(limit)
        return await cursor.to_list(length=limit)

    async def count_store_shows(self) -> int:
        """Returns total count of indexed shows."""
        return await self.store_shows.count_documents({})

    async def search_store_shows(self, query: str, limit: int = 20) -> list:
        """Fuzzy searches shows by title/genre/platform."""
        if not query:
            return await self.get_all_store_shows(limit=limit)
        cursor = self.store_shows.find({
            '$or': [
                {'title': {'$regex': query, '$options': 'i'}},
                {'clean_title': {'$regex': query, '$options': 'i'}},
                {'platform': {'$regex': query, '$options': 'i'}},
                {'genre': {'$regex': query, '$options': 'i'}}
            ]
        }).limit(limit)
        return await cursor.to_list(length=limit)

    async def delete_store_show(self, show_id: str):
        """Delete a show from store catalog."""
        await self.store_shows.delete_one({'show_id': str(show_id)})

    # ── Store Orders ──────────────────────────────────────────────────────────
    async def create_store_order(self, order_dict: dict):
        """Creates a store purchase order."""
        import time as _t
        if 'created_at' not in order_dict:
            order_dict['created_at'] = _t.time()
        order_dict.setdefault('status', 'PENDING')
        await self.store_orders.update_one({'order_id': order_dict['order_id']}, {'$set': order_dict}, upsert=True)

    async def get_store_order(self, order_id: str) -> dict | None:
        """Fetch a store order by order_id."""
        return await self.store_orders.find_one({'order_id': str(order_id)})

    async def update_store_order(self, order_id: str, update_dict: dict):
        """Updates store order details (e.g. status='SUCCESS', payment_id, etc.)."""
        await self.store_orders.update_one({'order_id': str(order_id)}, {'$set': update_dict})

    async def get_user_store_orders(self, user_id: int, limit: int = 20) -> list:
        """Returns recent purchase orders for a user."""
        cursor = self.store_orders.find({'user_id': int(user_id)}).sort('created_at', -1).limit(limit)
        return await cursor.to_list(length=limit)

    async def get_all_store_orders(self, limit: int = 50, offset: int = 0) -> list:
        """Returns all store orders for admin log/stats."""
        cursor = self.store_orders.find().sort('created_at', -1).skip(offset).limit(limit)
        return await cursor.to_list(length=limit)

    # ── User Purchased Shows ──────────────────────────────────────────────────
    async def add_user_purchased_show(self, user_id: int, bot_id: str, show_id: str, order_id: str = ""):
        """Records permanent ownership of a show for a user."""
        import time as _t
        doc = {
            'user_id': int(user_id),
            'bot_id': str(bot_id),
            'show_id': str(show_id),
            'order_id': str(order_id),
            'purchased_at': _t.time(),
            'last_downloaded': _t.time(),
            'download_count': 1
        }
        await self.store_user_shows.update_one(
            {'user_id': int(user_id), 'show_id': str(show_id)},
            {'$set': doc, '$inc': {'download_count': 1}},
            upsert=True
        )

    async def has_user_purchased_show(self, user_id: int, show_id: str) -> bool:
        """Checks if user has already purchased the show."""
        doc = await self.store_user_shows.find_one({'user_id': int(user_id), 'show_id': str(show_id)})
        return bool(doc)

    async def get_user_purchased_shows(self, user_id: int, limit: int = 50) -> list:
        """Returns list of show_ids purchased by the user."""
        cursor = self.store_user_shows.find({'user_id': int(user_id)}).sort('purchased_at', -1).limit(limit)
        return await cursor.to_list(length=limit)

    async def get_store_customers(self, bot_id: str = "") -> int:
        """Counts total unique customers who bought shows."""
        pipeline = []
        if bot_id:
            pipeline.append({'$match': {'bot_id': str(bot_id)}})
        pipeline.append({'$group': {'_id': '$user_id'}})
        pipeline.append({'$count': 'total'})
        res = await self.store_user_shows.aggregate(pipeline).to_list(1)
        return res[0]['total'] if res else 0

    # ── Store Bot Mode & Configurations ───────────────────────────────────────
    async def get_store_bot_config(self, bot_id: str) -> dict:
        """Fetches isolated configuration for a Store Bot."""
        import time as _t
        now = _t.time()
        b_str = str(bot_id)
        if b_str in self._store_cfg_cache:
            val, expiry = self._store_cfg_cache[b_str]
            if now < expiry:
                return val
        doc = await self.store_config.find_one({'_id': f"store_bot_{b_str}"})
        res = doc or {}
        self._store_cfg_cache[b_str] = (res, now + 30)
        return res

    async def set_store_bot_config(self, bot_id: str, **kwargs):
        """Updates isolated configuration for a Store Bot."""
        b_str = str(bot_id)
        await self.store_config.update_one({'_id': f"store_bot_{b_str}"}, {'$set': kwargs}, upsert=True)
        if b_str in self._store_cfg_cache:
            del self._store_cfg_cache[b_str]

    async def is_store_bot_mode(self, bot_id: str) -> bool:
        """Checks if a bot is running in Store Bot Mode."""
        cfg = await self.get_store_bot_config(bot_id)
        return cfg.get('is_store_mode', False)

    async def set_store_bot_mode(self, bot_id: str, is_store: bool):
        """Sets bot mode to Store Bot or Normal Delivery."""
        await self.set_store_bot_config(bot_id, is_store_mode=is_store)

db = Database(Config.DATABASE_URI, Config.DATABASE_NAME)
