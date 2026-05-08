from motor.motor_asyncio import AsyncIOMotorClient
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
        if not Config.MONGO_URI:
            logger.error("No MongoDB URI configured.")
            return

        self.client = AsyncIOMotorClient(Config.MONGO_URI)
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
        # Insert or update
        story_id = data.get("story_id")
        await self.stories.update_one({"story_id": story_id}, {"$set": data}, upsert=True)
        
    async def delete_story(self, story_id: str):
        await self.stories.delete_one({"story_id": story_id})

    # ─────────────────────────────────────────────────────────────────
    # Users, State & Access Management
    # ─────────────────────────────────────────────────────────────────
    async def get_user(self, user_id: int):
        user = await self.users.find_one({"id": int(user_id)})
        if not user:
            user = {
                "id": int(user_id),
                "lang": "en",
                "tc_accepted": False,
                "purchases": [],
                "used_channels": [],
                "subscribed": True,
                "joined_date": datetime.now(timezone.utc),
            }
            await self.users.insert_one(user)
        return user

    async def update_user(self, user_id: int, data: dict):
        await self.users.update_one({"id": int(user_id)}, {"$set": data}, upsert=True)

    async def has_purchase(self, user_id: int, story_id: str):
        user = await self.get_user(user_id)
        return story_id in user.get("purchases", [])
        
    async def add_purchase(self, user_id: int, story_id: str):
        await self.users.update_one({"id": int(user_id)}, {"$addToSet": {"purchases": story_id}}, upsert=True)

    async def get_subscribed_users(self):
        """Returns list of all users who have subscribed=True (or missing subscribed key defaulting to True)."""
        cursor = self.users.find({"subscribed": {"$ne": False}})
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
