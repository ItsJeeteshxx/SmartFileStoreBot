import asyncio
import logging
import os

# --- PATCH PYROGRAM SQLITE SCHEMA ISSUES ---
try:
    import pyrogram.storage.sqlite_storage
    import re
    # Patch string schemas for fresh databases
    for name in dir(pyrogram.storage.sqlite_storage):
        val = getattr(pyrogram.storage.sqlite_storage, name)
        if isinstance(val, str):
            patched = val
            if "CREATE TABLE" in val:
                patched = re.sub(r"CREATE TABLE (?!IF NOT EXISTS)", "CREATE TABLE IF NOT EXISTS ", patched)
            if "CREATE INDEX" in val:
                patched = re.sub(r"CREATE INDEX (?!IF NOT EXISTS)", "CREATE INDEX IF NOT EXISTS ", patched)
            if patched != val:
                setattr(pyrogram.storage.sqlite_storage, name, patched)

    # Patch open() to ensure tables are created even if migrations skipped them
    original_open = pyrogram.storage.sqlite_storage.SQLiteStorage.open
    async def patched_open(self):
        await original_open(self)
        try:
            with self.conn:
                self.conn.execute("""
                CREATE TABLE IF NOT EXISTS update_state (
                    id   INTEGER PRIMARY KEY,
                    pts  INTEGER,
                    qts  INTEGER,
                    date INTEGER,
                    seq  INTEGER
                );
                """)
                self.conn.execute("""
                CREATE TABLE IF NOT EXISTS usernames (
                    id       INTEGER,
                    username TEXT,
                    FOREIGN KEY (id) REFERENCES peers(id)
                );
                """)
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_usernames_username ON usernames (username);")
        except Exception as tbl_err:
            logging.warning(f"Failed to verify/create SQLite tables: {tbl_err}")

    pyrogram.storage.sqlite_storage.SQLiteStorage.open = patched_open
except Exception as e:
    logging.warning(f"Failed to patch Pyrogram storage schemas: {e}")
# -------------------------------------------

from pyrogram import Client, compose, filters
try:
    from AryaPremium.config import Config
    from AryaPremium.database import db
except ImportError:
    from config import Config
    from database import db
from utils import setup_ask_router

import sys
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

# Setup unbuffered logging for systemd journalctl
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Wait, we need the Management Bot token if it's separate from the connected bots.
async def main():
    print("==================================================", flush=True)
    print(">>> ARYA PREMIUM BOT ECOSYSTEM BOOTING...", flush=True)
    print(f">>> PID: {os.getpid()}", flush=True)
    print(f">>> API_ID: {Config.API_ID}", flush=True)
    print(f">>> MGMT_BOT_TOKEN: {'Configured' if Config.MGMT_BOT_TOKEN else 'MISSING'}", flush=True)
    print(f">>> MONGO_URI: {'Configured' if Config.MONGO_URI else 'MISSING'}", flush=True)
    print("==================================================", flush=True)

    logger.info("Initializing Premium Ecosystem Database...")
    await db.connect()

    if not Config.MGMT_BOT_TOKEN and Config.BOT_TOKEN:
        Config.MGMT_BOT_TOKEN = Config.BOT_TOKEN

    config_vars = ["API_ID", "API_HASH", "MGMT_BOT_TOKEN", "MONGO_URI", "DATABASE_NAME"]
    missing = [c for c in config_vars if not getattr(Config, c)]
    if missing:
        print(f"❌ CRITICAL CONFIG ERROR: Missing required configs: {', '.join(missing)}", flush=True)
        logger.error(f"Missing required configs: {', '.join(missing)}")
        return
    
    apps = []

    # ── 1. Load Management Bot ──
    try:
        from pyrogram import Client
        from utils import setup_ask_router
        mgmt_bot = Client(
            name="mgmt_bot",
            api_id=Config.API_ID,
            api_hash=Config.API_HASH,
            bot_token=Config.MGMT_BOT_TOKEN,
            plugins=dict(root="plugins.mgmt"),
            in_memory=False
        )
        setup_ask_router(mgmt_bot)
        db.mgmt_client = mgmt_bot
        apps.append(mgmt_bot)
    except Exception as e:
        logger.error(f"Failed to load mgmt_bot: {e}")

    # ── 2. Load Store Bots ──
    try:
        from plugins.userbot.market_seller import market_clients, _process_start, _process_screenshot, _process_callback, _process_text, _process_media, _process_my_stories, _process_chat_member, _process_inline_query
        from pyrogram.handlers import MessageHandler, CallbackQueryHandler, ChatMemberUpdatedHandler, InlineQueryHandler
        from pyrogram.errors import UserNotParticipant
        from pyrogram import StopPropagation
        from utils import setup_ask_router

        # ── Premium Ban Interceptor ──────────────────────────────────────────
        # Runs at group=-999 (highest priority) on ALL messages/callbacks
        # Checks ALL ban sources across both databases:
        #   1. forward-bot.premium_bans  (AryaPremium ban system)
        #   2. arya.premium_bans         (Main bot admin panel ban)
        #   3. arya.users.ban_status     (Main bot /ban command ban)
        async def _premium_ban_interceptor(client, update):
            try:
                user = getattr(update, 'from_user', None)
                if not user:
                    return
                user_id = user.id

                # Skip owners
                if Config.OWNER_IDS and user_id in Config.OWNER_IDS:
                    return

                is_blocked = False

                # 1. Check forward-bot.premium_bans (AryaPremium's own ban system)
                prem_ban = await db.db.premium_bans.find_one({"_id": user_id})
                if prem_ban and prem_ban.get("status") in ("banned", "flagged"):
                    is_blocked = True

                # 2 & 3. Check arya database (main bot bans) — uses same MongoDB cluster
                if not is_blocked:
                    try:
                        arya_db_ref = db.client["arya"]
                        # 2. arya.premium_bans (admin panel ban)
                        arya_prem_ban = await arya_db_ref.premium_bans.find_one({"_id": user_id})
                        if arya_prem_ban and arya_prem_ban.get("status") in ("banned", "flagged"):
                            is_blocked = True
                        # 3. arya.users.ban_status (main bot /ban command)
                        if not is_blocked:
                            arya_user = await arya_db_ref.users.find_one({"id": user_id})
                            if arya_user and arya_user.get("ban_status", {}).get("is_banned"):
                                is_blocked = True
                    except Exception:
                        pass  # Cross-DB check failed — fall through (don't block on error)

                if is_blocked:
                    # Silently drop — do NOT tell user they're banned
                    if hasattr(update, 'answer'):
                        try:
                            await update.answer("⛔ Access denied.", show_alert=False)
                        except Exception:
                            pass
                    raise StopPropagation

            except StopPropagation:
                raise
            except Exception:
                pass  # Never block on unexpected errors


        bots = await db.db.premium_bots.find().to_list(length=None)
        seen_tokens = set()
        if Config.MGMT_BOT_TOKEN:
            seen_tokens.add(Config.MGMT_BOT_TOKEN.strip())

        for b in bots:
            tok = (b.get('token') or "").strip()
            if not tok or tok in seen_tokens:
                if tok in seen_tokens:
                    logger.warning(f"Skipping duplicate/mgmt bot token for @{b.get('username')}")
                continue
            seen_tokens.add(tok)

            logger.info(f"Loading Market Bot: {b.get('username')}")
            cli = Client(
                name=f"market_{b['id']}", 
                api_id=Config.API_ID, 
                api_hash=Config.API_HASH, 
                bot_token=tok, 
                in_memory=False
            )
            setup_ask_router(cli)

            # ── Register ban interceptor FIRST (group=-999) ──────────────────
            cli.add_handler(MessageHandler(_premium_ban_interceptor, filters.private), group=-999)
            cli.add_handler(CallbackQueryHandler(_premium_ban_interceptor, filters.all), group=-999)

            # ── Register all normal handlers ──────────────────────────────────
            cli.add_handler(MessageHandler(_process_start, filters.command("start") & filters.private))
            cli.add_handler(MessageHandler(_process_my_stories, filters.command(["mystories", "stories"]) & filters.private))
            # Media handler (feedback + screenshot) — must come before _process_screenshot
            cli.add_handler(MessageHandler(_process_media, (filters.photo | filters.video | filters.animation | filters.document | filters.voice | filters.audio) & filters.private))
            cli.add_handler(MessageHandler(_process_text, filters.text & filters.private))
            cli.add_handler(CallbackQueryHandler(_process_callback, filters.regex(r'^mb#')))
            cli.add_handler(ChatMemberUpdatedHandler(_process_chat_member))
            cli.add_handler(InlineQueryHandler(_process_inline_query))
            market_clients[str(b['id'])] = cli
            apps.append(cli)
    except Exception as e:
         logger.error(f"Failed loading connected bots: {e}")


    if not apps:
        logger.error("No bots to run. Exiting.")
        return

    logger.info("Starting up Premium Ecosystem and Warming Cache...")
    from pyrogram import idle

    started_apps = []
    for app in apps:
        try:
            await app.start()
            started_apps.append(app)
            
            # Background task to warm up cache completely on any fresh restart/VPS migration
            async def warm(client):
                try:
                    from pyrogram.errors import FloodWait
                    async for _ in client.get_dialogs(limit=30):
                        pass
                    logger.info(f"[{client.name}] Successfully warmed up peer cache!")
                except FloodWait as e:
                    await asyncio.sleep(e.value)
                except Exception as e:
                    logger.debug(f"[{client.name}] Dialogs warmup interrupted: {e}")
                    
            asyncio.create_task(warm(app))
        except Exception as e:
            logger.error(f"Failed to start app {getattr(app, 'name', 'Unknown')}: {e}")

    if not started_apps:
        logger.error("No apps successfully started. Exiting.")
        return

    # Start Premium Live Monitor on Mgmt Bot (which is Admin in source channels)
    if 'mgmt_bot' in locals():
        try:
            from plugins.premium_live_monitor import start_premium_live_monitor
            asyncio.create_task(start_premium_live_monitor(mgmt_bot))
        except Exception as e:
            logger.warning(f"Could not start Premium Live Monitor: {e}")

    # Start Auto Instant Delivery Queue Worker (runs DM delivery via store bots)
    try:
        from purchase_dm_helper import start_auto_delivery_queue_worker
        mb_inst = mgmt_bot if 'mgmt_bot' in locals() else None
        asyncio.create_task(start_auto_delivery_queue_worker(market_clients, mb_inst, db))
        logger.info("✅ Auto Instant Delivery Queue Worker started in main.py")
    except Exception as e:
        logger.warning(f"Could not start Auto Delivery Queue Worker: {e}")

    # Keep bots running
    await idle()

    # Graceful shutdown
    for app in started_apps:
        try:
            await app.stop()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down Ecosystem...")
