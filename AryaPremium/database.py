from typing import Optional, List, Dict, Any, Union
from motor.motor_asyncio import AsyncIOMotorClient
try:
    from AryaPremium.config import Config
except ImportError:
    from config import Config
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

class PremiumDatabase:
    def __init__(self):
        self.client = None
        self.db = None
        self.mgmt_client = None
        # Collections
        self.stories = None
        self.bots = None
        self.purchases = None
        self.users = None
        self.settings = None

        try:
            mongo_uri = getattr(Config, "MONGO_URI", None) or getattr(Config, "DATABASE_URI", None) or getattr(Config, "DATABASE", None)
            db_name = getattr(Config, "DATABASE_NAME", "arya_premium")
            if mongo_uri:
                self.client = AsyncIOMotorClient(mongo_uri)
                self.db = self.client[db_name]
                self.users = self.db.users
                self.stories = self.db.premium_stories
                self.bots = self.db.premium_bots
                self.purchases = self.db.premium_purchases
                self.settings = self.db.premium_settings
        except Exception as ex:
            logger.warning(f"PremiumDatabase synchronous pre-init warning: {ex}")

    async def connect(self):
        mongo_uri = getattr(Config, "MONGO_URI", None) or getattr(Config, "DATABASE_URI", None) or getattr(Config, "DATABASE", None)
        if not mongo_uri:
            logger.error("No MongoDB URI configured.")
            return

        self.client = AsyncIOMotorClient(mongo_uri)
        self.db = self.client[Config.DATABASE_NAME]
        
        # We share users with the main bot if needed, but premium has its own ecosystem collections
        self.users = self.db.users
        self.stories = self.db.premium_stories
        self.bots = self.db.premium_bots
        self.purchases = self.db.premium_purchases
        self.settings = self.db.premium_settings
        
        logger.info("Connected to MongoDB -> Premium DB System initialized.")

    # ─────────────────────────────────────────────────────────────────
    # Global System Configs
    # ─────────────────────────────────────────────────────────────────
    async def get_config(self, key: str, default=None):
        doc = await self.settings.find_one({"_id": "global_config"})
        if not doc: return default
        return doc.get(key, default)

    async def set_config(self, key: str, value):
        await self.settings.update_one(
            {"_id": "global_config"},
            {"$set": {key: value}},
            upsert=True
        )

    # ─────────────────────────────────────────────────────────────────
    # Story Management
    # ─────────────────────────────────────────────────────────────────
    async def get_all_stories(self):
        if self.stories is None:
            await self.connect()
        if self.stories is None:
            raise Exception("Database connection failed. Check MONGO_URI.")
        cursor = self.stories.find({})
        return await cursor.to_list(length=None)

    async def get_story(self, story_id: str):
        if self.stories is None:
            await self.connect()
        return await self.stories.find_one({"story_id": story_id})

    async def save_story(self, data: dict):
        if self.stories is None:
            await self.connect()
        from bson.objectid import ObjectId
        story_id = str(data.get("story_id") or "").strip()
        doc_id = data.get("_id") or data.get("id")
        
        query = None
        # 1. Try finding by MongoDB _id first
        if doc_id:
            try:
                if isinstance(doc_id, ObjectId):
                    query = {"_id": doc_id}
                elif isinstance(doc_id, str) and ObjectId.is_valid(doc_id):
                    query = {"$or": [{"_id": ObjectId(doc_id)}, {"story_id": doc_id}, {"id": doc_id}]}
                else:
                    query = {"$or": [{"story_id": str(doc_id)}, {"id": str(doc_id)}]}
            except Exception:
                pass
                
        # 2. Fallback to finding by story_id
        if not query and story_id:
            if ObjectId.is_valid(story_id):
                try:
                    query = {"$or": [{"story_id": story_id}, {"_id": ObjectId(story_id)}, {"id": story_id}]}
                except Exception:
                    query = {"story_id": story_id}
            else:
                query = {"$or": [{"story_id": story_id}, {"id": story_id}]}
                
        existing = await self.stories.find_one(query) if query else None
        
        # Clean update data
        clean_data = {k: v for k, v in data.items() if k not in ("_id",)}
        
        # Clean parts list if present
        if "parts" in clean_data and isinstance(clean_data["parts"], list):
            story_end_id = int(clean_data.get("end_id") or (existing.get("end_id") if existing else 0) or 0)
            story_eps_val = str(clean_data.get("episodes") or (existing.get("episodes") if existing else "") or "").strip()
            cleaned_parts = []
            found_ongoing = False
            max_part_end = 0
            max_part_ep = 0

            for idx, p in enumerate(clean_data["parts"]):
                if isinstance(p, dict):
                    b_val = str(p.get("badge") or p.get("badge_type") or ("ongoing" if p.get("is_ongoing") else "new" if p.get("is_new") else "none"))
                    is_new_val = bool(p.get("is_new") or b_val == "new")
                    is_ongoing_val = bool(p.get("is_ongoing") or b_val == "ongoing")
                    is_last_part = (idx == len(clean_data["parts"]) - 1)
                    
                    p_start = int(p.get("start_id") or 0)
                    p_end = int(p.get("end_id") or 0)
                    p_episodes = str(p.get("episodes") or "").strip()
                    
                    if is_ongoing_val or (not found_ongoing and is_last_part and str(clean_data.get("status") or (existing.get("status") if existing else "")).lower() == "ongoing"):
                        is_ongoing_val = True
                        b_val = "ongoing"
                        found_ongoing = True
                        if story_end_id > p_end:
                            p_end = story_end_id
                        if story_eps_val and story_eps_val.isdigit():
                            import re
                            m = re.search(r"(\d+)", p_episodes)
                            if m:
                                p_episodes = f"{m.group(1)}-{story_eps_val}"
                            elif not p_episodes:
                                p_episodes = story_eps_val

                    if p_end > max_part_end:
                        max_part_end = p_end

                    import re
                    m_ep_all = re.findall(r"\d+", p_episodes)
                    if m_ep_all:
                        last_num = int(m_ep_all[-1])
                        if last_num > max_part_ep:
                            max_part_ep = last_num
                    
                    cleaned_parts.append({
                        "id": str(p.get("id") or f"part_{idx+1}"),
                        "name": str(p.get("name") or f"Part {idx+1}"),
                        "name_hi": p.get("name_hi"),
                        "start_id": p_start,
                        "end_id": p_end,
                        "episodes": p_episodes,
                        "price": float(p.get("price") or 0),
                        "badge": b_val,
                        "badge_type": b_val,
                        "is_new": is_new_val,
                        "is_ongoing": is_ongoing_val,
                    })

            clean_data["parts"] = cleaned_parts
            if max_part_end > int(clean_data.get("end_id") or 0):
                clean_data["end_id"] = max_part_end
                clean_data["end_message_id"] = max_part_end
            if max_part_ep > 0:
                curr_eps = int(clean_data["episodes"]) if (str(clean_data.get("episodes", "")).isdigit()) else 0
                if max_part_ep > curr_eps:
                    clean_data["episodes"] = str(max_part_ep)
            if len(cleaned_parts) > 0 and clean_data.get("enable_parts") is not False:
                clean_data["enable_parts"] = True
                
        p_url = str(clean_data.get("poster_url") or "").strip()
        if p_url:
            clean_data["poster_url"] = p_url
            clean_data["image_url"] = p_url
            clean_data["cover"] = p_url
            clean_data["poster"] = p_url
            if p_url.startswith("http") or p_url.startswith("/api/"):
                clean_data["image"] = ""

        if existing:
            await self.stories.update_one({"_id": existing["_id"]}, {"$set": clean_data})
        else:
            if doc_id and isinstance(doc_id, str) and ObjectId.is_valid(doc_id):
                clean_data["_id"] = ObjectId(doc_id)
            await self.stories.insert_one(clean_data)
    async def delete_story(self, story_id: str):
        from bson.objectid import ObjectId
        sid_str = str(story_id).strip()
        filters = [{"story_id": sid_str}, {"id": sid_str}, {"_id": sid_str}]
        try:
            if ObjectId.is_valid(sid_str):
                filters.append({"_id": ObjectId(sid_str)})
        except Exception:
            pass

        for flt in filters:
            await self.stories.delete_many(flt)
            await self.db.stories.delete_many(flt)
            await self.db.episodes.delete_many(flt)

    # ─────────────────────────────────────────────────────────────────
    # Users, State & Access Management
    # ─────────────────────────────────────────────────────────────────
    async def get_user(self, user_id: int, from_user=None, bot_id: int = None):
        user = await self.users.find_one({"id": int(user_id)})
        update_fields = {}
        now = datetime.now(timezone.utc)
        if from_user:
            fn = getattr(from_user, "first_name", "") or ""
            ln = getattr(from_user, "last_name", "") or ""
            un = getattr(from_user, "username", "") or ""
            if not user or user.get("first_name") != fn or user.get("last_name") != ln or user.get("username") != un:
                update_fields.update({
                    "first_name": fn,
                    "last_name": ln,
                    "username": un,
                })
        if bot_id:
            update_fields[f"bot_last_active.{bot_id}"] = now
            update_fields["last_active"] = now

        if not user:
            user = {
                "id": int(user_id),
                "lang": "en",
                "tc_accepted": False,
                "purchases": [],
                "used_channels": [],
                "used_bots": [int(bot_id)] if bot_id else [],
                "alerts_subscribed": False,   # OFF by default — user must explicitly opt-in
                "joined_date": now,
                "last_active": now,
            }
            if update_fields:
                user.update(update_fields)
            await self.users.insert_one(user)
        else:
            set_ops = {}
            if update_fields:
                set_ops["$set"] = update_fields
            add_ops = {}
            if bot_id:
                add_ops["$addToSet"] = {"used_bots": int(bot_id)}
            
            update_doc = {}
            if set_ops: update_doc.update(set_ops)
            if add_ops: update_doc.update(add_ops)
            
            if update_doc:
                await self.users.update_one({"id": int(user_id)}, update_doc)
                if "$set" in update_doc:
                    user.update(update_doc["$set"])
        return user

    async def update_user(self, user_id: int, data: dict):
        await self.users.update_one({"id": int(user_id)}, {"$set": data}, upsert=True)

    async def has_purchase(self, user_id: int, story_id: str, part_id: Optional[str] = None) -> bool:
        """Checks if a user has purchased a given story or specific part across users, orders, premium_purchases, and premium_checkout collections."""
        if not user_id or not story_id:
            return False
        try:
            uid_int = int(user_id) if str(user_id).isdigit() else user_id
            uid_str = str(user_id)
            u_filter = [uid_int, uid_str]
            sid_str = str(story_id).strip()

            # Build list of possible story ID aliases (ObjectId string vs custom story_id)
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

            # If part_id is requested, check orders specifically for that part
            if part_id:
                part_id_str = str(part_id).strip()
                order = await self.db.orders.find_one({
                    "user_id": {"$in": u_filter},
                    "status": {"$in": ["paid", "delivered", "completed", "success"]},
                    "items": {
                        "$elemMatch": {
                            "$or": [
                                {"story_id": {"$in": story_aliases_list}},
                                {"id": {"$in": story_aliases_list}}
                            ],
                            "part_id": part_id_str
                        }
                    }
                })
                if order:
                    return True

                # Also check if whole story is owned (whole story ownership grants all parts)
                user = await self.users.find_one({"id": {"$in": u_filter}})
                if user:
                    purchases = [str(p) for p in user.get("purchases", [])]
                    for alias in story_aliases_list:
                        if alias in purchases:
                            return True
                return False

            # 1. Check users collection
            user = await self.users.find_one({"id": {"$in": u_filter}})
            if user:
                purchases = [str(p) for p in user.get("purchases", [])]
                for alias in story_aliases_list:
                    if alias in purchases:
                        return True

            # 2. Check orders collection (paid, delivered, completed, success) for whole story
            order = await self.db.orders.find_one({
                "user_id": {"$in": u_filter},
                "status": {"$in": ["paid", "delivered", "completed", "success"]},
                "$or": [
                    {"story_id": {"$in": story_aliases_list}},
                    {"story_ids": {"$in": story_aliases_list}},
                    {"items.id": {"$in": story_aliases_list}},
                    {"items.story_id": {"$in": story_aliases_list}}
                ]
            })
            if order:
                # Only add to user.purchases if order was for the full story (no part_id)
                has_parts_only = False
                if order.get("items"):
                    for itm in order["items"]:
                        if (itm.get("story_id") in story_aliases_list or itm.get("id") in story_aliases_list) and itm.get("part_id"):
                            has_parts_only = True
                            break
                if not has_parts_only:
                    try:
                        await self.add_purchase(uid_int, sid_str)
                    except Exception:
                        pass
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
                try:
                    await self.add_purchase(uid_int, sid_str)
                except Exception:
                    pass
                return True

            # 4. Check premium_checkout collection (approved/paid manual UPI checkout)
            checkout = await self.db.premium_checkout.find_one({
                "user_id": {"$in": u_filter},
                "status": {"$in": ["approved", "completed", "paid", "success"]},
                "$or": [
                    {"story_id": {"$in": story_aliases_list}},
                    {"story_ids": {"$in": story_aliases_list}}
                ]
            })
            if checkout:
                try:
                    await self.add_purchase(uid_int, sid_str)
                except Exception:
                    pass
                return True

            return False
        except Exception as e:
            logger.error(f"Error in has_purchase: {e}")
            return False

    async def add_purchase(self, user_id: int, story_id: str):
        if not user_id or not story_id:
            return
        uid_int = int(user_id) if str(user_id).isdigit() else user_id
        uid_str = str(user_id)
        sid_str = str(story_id).strip()

        try:
            await self.users.update_one({"id": uid_int}, {"$addToSet": {"purchases": sid_str}}, upsert=True)
            if uid_str != str(uid_int):
                await self.users.update_one({"id": uid_str}, {"$addToSet": {"purchases": sid_str}}, upsert=False)
        except Exception as ue:
            logger.warning(f"Failed to update users collection in add_purchase: {ue}")

        try:
            await self.db.premium_purchases.update_one(
                {"user_id": uid_int, "story_id": sid_str},
                {"$set": {"user_id": uid_int, "story_id": sid_str, "created_at": datetime.now(timezone.utc)}},
                upsert=True
            )
        except Exception as pe:
            logger.warning(f"Failed to upsert premium_purchases in add_purchase: {pe}")


    async def is_paid_user(self, user_id) -> bool:
        """Check if user is a paid user (has at least 1 purchased story or completed order)."""
        if not user_id:
            return False
        try:
            uid_int = int(user_id) if str(user_id).isdigit() else user_id
            uid_str = str(user_id)
            u_filter = [uid_int, uid_str]
            
            user = await self.users.find_one({
                "$or": [{"id": {"$in": u_filter}}, {"_id": {"$in": u_filter}}],
                "purchases.0": {"$exists": True}
            })
            if user and user.get("purchases"):
                return True

            order = await self.db.orders.find_one({
                "user_id": {"$in": u_filter},
                "status": {"$in": ["paid", "delivered", "completed", "success"]}
            })
            if order:
                return True

            purchase = await self.purchases.find_one({
                "user_id": {"$in": u_filter}
            })
            if purchase:
                return True

            checkout = await self.db.premium_checkout.find_one({
                "user_id": {"$in": u_filter},
                "status": "approved"
            })
            if checkout:
                return True

            return False
        except Exception:
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

            if users_col is not None:
                async for doc in users_col.find({"purchases.0": {"$exists": True}}, {"id": 1}):
                    uid = doc.get("id")
                    if uid is not None:
                        paid_uids.add(str(uid))
                        try: paid_uids.add(int(uid))
                        except: pass
                    
            if hasattr(db_obj, 'orders') and db_obj.orders is not None:
                async for doc in db_obj.orders.find({"status": {"$in": ["paid", "delivered", "completed", "success"]}}, {"user_id": 1}):
                    uid = doc.get("user_id")
                    if uid is not None:
                        paid_uids.add(str(uid))
                        try: paid_uids.add(int(uid))
                        except: pass

            purchases_col = getattr(self, 'purchases', None)
            if purchases_col is None and db_obj is not None:
                purchases_col = getattr(db_obj, 'premium_purchases', None)
            if purchases_col is not None:
                async for doc in purchases_col.find({}, {"user_id": 1}):
                    uid = doc.get("user_id")
                    if uid is not None:
                        paid_uids.add(str(uid))
                        try: paid_uids.add(int(uid))
                        except: pass

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

            if hasattr(db_obj, 'premium_bans') and db_obj.premium_bans is not None:
                bans_cursor = db_obj.premium_bans.find({"_id": {"$in": paid_uids_list}})
                unbanned_count = 0
                async for ban in bans_cursor:
                    ban_id = ban.get("_id")
                    reason = str(ban.get("reason", "")).lower()
                    is_share_or_admin = any(k in reason for k in ("rapid", "strike", "share", "admin", "manual"))
                    if not is_share_or_admin and ("alt of" in reason or "evasion" in reason):
                        await db_obj.premium_bans.delete_one({"_id": ban_id})
                        unbanned_count += 1
                        logger.info(f"✅ Auto-Unbanned Paid User {ban_id} from premium_bans")

            await self.users.update_many(
                {
                    "id": {"$in": paid_uids_list},
                    "ban_status.is_banned": True,
                    "ban_status.ban_reason": {"$regex": "^auto-ban: (alt of|evasion)", "$options": "i"}
                },
                {"$set": {
                    "ban_status.is_banned": False,
                    "ban_status.ban_reason": "",
                    "banned": False
                }}
            )

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

            async for u in self.users.find({"id": {"$in": paid_uids_list}}):
                for ip in u.get("ips", []):
                    if ip: paid_ips.add(ip)
                if u.get("last_ip"): paid_ips.add(u.get("last_ip"))
                for dev in u.get("device_ids", []):
                    if dev: paid_devices.add(dev)
                if u.get("device_id"): paid_devices.add(u.get("device_id"))

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

    async def get_subscribed_users(self):
        """Returns list of all users who have explicitly opted in (alerts_subscribed=True).
        Users with missing key or False are excluded — subscriptions are OFF by default."""
        cursor = self.users.find({"alerts_subscribed": True})
        return await cursor.to_list(length=None)

    # ─────────────────────────────────────────────────────────────────
    # Connected Bots Management
    # ─────────────────────────────────────────────────────────────────
    async def get_connected_bots(self):
        cursor = self.bots.find({})
        return await cursor.to_list(length=None)
        
    async def add_connected_bot(self, token: str, bot_id: int, bot_username: str):
        await self.bots.update_one(
            {"bot_id": bot_id},
            {"$set": {"token": token, "bot_username": bot_username}},
            upsert=True
        )

    async def remove_connected_bot(self, bot_id: int):
        await self.bots.delete_one({"bot_id": bot_id})

    async def remove_ban(self, id):
        uid_int = int(id)
        ban_status = dict(
            is_banned=False,
            ban_reason='',
            reason=''
        )
        try:
            await self.users.update_many(
                {'$or': [
                    {'id': uid_int},
                    {'id': str(uid_int)},
                    {'_id': uid_int},
                    {'_id': str(uid_int)}
                ]},
                {
                    '$set': {
                        'ban_status': ban_status,
                        'banned': False,
                        'ban_reason': ''
                    },
                    '$unset': {
                        'abuse_strike': '',
                        'is_banned': ''
                    }
                }
            )
        except Exception:
            pass

        try:
            await self.db.premium_bans.delete_many({
                '$or': [
                    {'_id': uid_int},
                    {'_id': str(uid_int)},
                    {'user_id': uid_int},
                    {'user_id': str(uid_int)}
                ]
            })
        except Exception:
            pass

        try:
            await self.db.banned_users.delete_many({
                '$or': [
                    {'user_id': uid_int},
                    {'user_id': str(uid_int)},
                    {'_id': uid_int},
                    {'_id': str(uid_int)}
                ]
            })
        except Exception:
            pass

    async def unban_user(self, user_id):
        return await self.remove_ban(user_id)

db = PremiumDatabase()
