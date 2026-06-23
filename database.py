from os import environ 
from config import Config
import motor.motor_asyncio
from pymongo import MongoClient

async def mongodb_version():
    x = MongoClient(Config.DATABASE_URI)
    mongodb_version = x.server_info()['version']
    return mongodb_version

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
        
    async def set_share_bot_token(self, token: str):
        # Migrated: now handles multiple bots via array push, preserving backwards compatibility for singles initially if desired, or just override.
        pass

    async def get_share_bot_token(self):
        # Legacy
        doc = await self.stats.find_one({'_id': 'share_bot'})
        return doc.get('token') if doc else None
        
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
        doc = await self.share_config.find_one({'_id': 'global'})
        return doc or {}

    async def _set_share_cfg(self, **kwargs):
        await self.share_config.update_one({'_id': 'global'}, {'$set': kwargs}, upsert=True)

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
        doc = await self.share_config.find_one({'_id': f'bot_{bot_id}'})
        return doc or {}

    async def _set_bot_cfg(self, bot_id: str, **kwargs):
        if not bot_id:
            return
        await self.share_config.update_one(
            {'_id': f'bot_{bot_id}'}, {'$set': kwargs}, upsert=True
        )

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

    async def update_channel_index_entry(self, chat_id: int, entry: dict):
        """Append or update a single entry (indexed by msg_id) in the channel index."""
        import time
        await self.share_config.update_one(
            {'_id': f'ch_index_{chat_id}'},
            {
                '$pull': {'entries': {'msg_id': entry['msg_id']}},
            },
            upsert=True
        )
        await self.share_config.update_one(
            {'_id': f'ch_index_{chat_id}'},
            {
                '$push': {'entries': entry},
                '$set': {'scanned_at': time.time()},
                '$inc': {'count': 1},
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
                {'$setOnInsert': {'first_seen': time.time()}},
                upsert=True
            )
        except Exception:
            pass

    async def get_share_bot_users(self, bot_id: str) -> list:
        cursor = self.share_users.find({'bot_id': str(bot_id)})
        return [doc['user_id'] async for doc in cursor]

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
    
    async def is_user_exist(self, id):
        user = await self.col.find_one({'id':int(id)})
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
    
    async def ban_user(self, user_id, ban_reason="No Reason"):
        ban_status = dict(
            is_banned=True,
            ban_reason=ban_reason
        )
        await self.col.update_one({'id': int(user_id)}, {'$set': {'ban_status': ban_status}}, upsert=True)

    async def get_ban_status(self, id):
        default = dict(
            is_banned=False,
            ban_reason=''
        )
        try:
            user_id_int = int(id)
        except (ValueError, TypeError):
            return default
            
        # 1. Check local bot collection ban status in 'arya' DB
        user = await self.col.find_one({'id': user_id_int})
        if user and user.get('ban_status', {}).get('is_banned'):
            return user.get('ban_status', default)
            
        # 2. Check premium_bans collection (same 'arya' DB — used by mini app admin panel)
        try:
            prem_ban = await self.db.premium_bans.find_one({'_id': user_id_int})
            if prem_ban and prem_ban.get('status') in ('banned', 'flagged'):
                return {
                    'is_banned': True,
                    'ban_reason': prem_ban.get('reason', 'Banned by administrator')
                }
        except Exception:
            pass
            
        return default

    async def get_all_users(self):
        return self.col.find({})
    
    async def delete_user(self, user_id):
        await self.col.delete_many({'id': int(user_id)})
 
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
        user = await self.col.find_one({'id':int(id)})
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
       
       # Enforce account limits: 10 for Normal Bots, 4 for Userbots
       limit = 10 if is_bot else 4
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
              
    async def add_frwd(self, user_id):
       return await self.nfy.insert_one({'user_id': int(user_id)})
    
    async def rmve_frwd(self, user_id=0, all=False):
       data = {} if all else {'user_id': int(user_id)}
       return await self.nfy.delete_many(data)
    
    async def get_all_frwd(self):
       return self.nfy.find({})
    async def get_language(self, user_id: int) -> str:
        """Return user's preferred language: 'en', 'hi', or 'hinglish'. Default 'en'."""
        user = await self.col.find_one({'id': int(user_id)})
        if user:
            return user.get('language', 'en')
        return 'en'

    async def set_language(self, user_id: int, lang: str):
        await self.col.update_one({'id': int(user_id)}, {'$set': {'language': lang}}, upsert=True)

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

        # Self-migration routine: migrate old seen_users_* and bot_* configs to the new share_users collection
        try:
            import logging
            import time
            mig_logger = logging.getLogger(__name__)
            
            # 1. Migrate seen_users_{bot_id} documents from stats collection
            async for doc in self.stats.find({"_id": {"$regex": "^seen_users_"}}):
                doc_id = doc.get("_id", "")
                bot_id = doc_id.replace("seen_users_", "", 1)
                user_ids = doc.get("ids", [])
                if not bot_id or not user_ids:
                    continue
                mig_logger.info(f"[Migration] Migrating {len(user_ids)} users from old {doc_id} stats document...")
                
                inserted_count = 0
                for uid in user_ids:
                    try:
                        await self.share_users.update_one(
                            {'bot_id': str(bot_id), 'user_id': int(uid)},
                            {'$setOnInsert': {'first_seen': time.time()}},
                            upsert=True
                        )
                        inserted_count += 1
                    except Exception:
                        pass
                
                # Delete the old document since it's fully migrated
                await self.stats.delete_one({"_id": doc_id})
                mig_logger.info(f"[Migration] Successfully migrated {inserted_count} users and deleted old {doc_id}.")
                
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
                mig_logger.info(f"[Migration] Migrating {len(user_ids)} users from old {doc_id} config document...")
                
                inserted_count = 0
                for uid in user_ids:
                    try:
                        await self.share_users.update_one(
                            {'bot_id': str(bot_id), 'user_id': int(uid)},
                            {'$setOnInsert': {'first_seen': time.time()}},
                            upsert=True
                        )
                        inserted_count += 1
                    except Exception:
                        pass
                
                # Unset the users array so it doesn't bloat the config doc
                await self.share_config.update_one({"_id": doc_id}, {"$unset": {"users": ""}})
                mig_logger.info(f"[Migration] Successfully migrated {inserted_count} users and cleaned old 'users' field from {doc_id}.")
                
        except Exception as _m_err:
            import logging
            logging.getLogger(__name__).error(f"[Migration] Exception in user migration: {_m_err}")

db = Database(Config.DATABASE_URI, Config.DATABASE_NAME)
