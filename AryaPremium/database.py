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
        story_id = data.get("story_id")
        query = {"story_id": story_id}
        
        # If story_id looks like a 24-char ObjectId, it might be an old story that didn't have story_id
        if story_id and len(str(story_id)) == 24:
            from bson.objectid import ObjectId
            try:
                obj_id = ObjectId(str(story_id))
                query = {"$or": [{"story_id": story_id}, {"_id": obj_id}]}
            except:
                pass
                
        existing = await self.stories.find_one(query)
        if existing:
            # Strip _id from data if present to avoid modifying immutable field
            data.pop("_id", None)
            await self.stories.update_one({"_id": existing["_id"]}, {"$set": data})
        else:
            await self.stories.insert_one(data)
    async def delete_story(self, story_id: str):
        await self.stories.delete_one({"story_id": story_id})

    # ─────────────────────────────────────────────────────────────────
    # Users, State & Access Management
    # ─────────────────────────────────────────────────────────────────
    async def get_user(self, user_id: int, from_user=None):
        user = await self.users.find_one({"id": int(user_id)})
        update_fields = {}
        if from_user:
            fn = getattr(from_user, "first_name", "") or ""
            ln = getattr(from_user, "last_name", "") or ""
            un = getattr(from_user, "username", "") or ""
            if not user or user.get("first_name") != fn or user.get("last_name") != ln or user.get("username") != un:
                update_fields.update({
                    "first_name": fn,
                    "last_name": ln,
                    "username": un,
                    "last_active": datetime.now(timezone.utc)
                })
        if not user:
            user = {
                "id": int(user_id),
                "lang": "en",
                "tc_accepted": False,
                "purchases": [],
                "used_channels": [],
                "alerts_subscribed": False,   # OFF by default — user must explicitly opt-in
                "joined_date": datetime.now(timezone.utc),
            }
            if update_fields:
                user.update(update_fields)
            await self.users.insert_one(user)
        elif update_fields:
            await self.users.update_one({"id": int(user_id)}, {"$set": update_fields})
            user.update(update_fields)
        return user

    async def update_user(self, user_id: int, data: dict):
        await self.users.update_one({"id": int(user_id)}, {"$set": data}, upsert=True)

    async def has_purchase(self, user_id: int, story_id: str):
        user = await self.get_user(user_id)
        return story_id in user.get("purchases", [])
        
    async def add_purchase(self, user_id: int, story_id: str):
        await self.users.update_one({"id": int(user_id)}, {"$addToSet": {"purchases": story_id}}, upsert=True)

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
            
            async for doc in self.users.find({"purchases.0": {"$exists": True}}, {"id": 1}):
                uid = doc.get("id")
                if uid is not None:
                    paid_uids.add(str(uid))
                    try: paid_uids.add(int(uid))
                    except: pass
                    
            async for doc in self.db.orders.find({"status": {"$in": ["paid", "delivered", "completed", "success"]}}, {"user_id": 1}):
                uid = doc.get("user_id")
                if uid is not None:
                    paid_uids.add(str(uid))
                    try: paid_uids.add(int(uid))
                    except: pass

            async for doc in self.purchases.find({}, {"user_id": 1}):
                uid = doc.get("user_id")
                if uid is not None:
                    paid_uids.add(str(uid))
                    try: paid_uids.add(int(uid))
                    except: pass

            async for doc in self.db.premium_checkout.find({"status": "approved"}, {"user_id": 1}):
                uid = doc.get("user_id")
                if uid is not None:
                    paid_uids.add(str(uid))
                    try: paid_uids.add(int(uid))
                    except: pass

            if not paid_uids:
                return

            paid_uids_list = list(paid_uids)

            bans_cursor = self.db.premium_bans.find({"_id": {"$in": paid_uids_list}})
            unbanned_count = 0
            async for ban in bans_cursor:
                ban_id = ban.get("_id")
                reason = str(ban.get("reason", "")).lower()
                is_manual = reason in ["banned by administrator", "admin ban"]
                if not is_manual or "auto-ban" in reason or "alt of" in reason or "strike" in reason or "rapid" in reason or "evasion" in reason:
                    await self.db.premium_bans.delete_one({"_id": ban_id})
                    unbanned_count += 1
                    logger.info(f"✅ Auto-Unbanned Paid User {ban_id} from premium_bans")

            await self.users.update_many(
                {"id": {"$in": paid_uids_list}, "$or": [{"ban_status.ban_reason": {"$regex": "auto-ban|alt of|strike|rapid|evasion", "$options": "i"}}, {"ban_status.is_banned": True}]},
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

db = PremiumDatabase()
