import asyncio
import logging
import os

# --- DISABLE INTERACTIVE STDIN PROMPTS (HEADLESS DAEMON PROTECTION) ---
try:
    import pyrogram.client
    async def _headless_ainput(prompt=""):
        raise RuntimeError(f"Interactive console input is disabled in headless mode: {prompt}")
    pyrogram.client.ainput = _headless_ainput
except Exception:
    pass
# ----------------------------------------------------------------------

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
        
        try:
            from pyrogram.handlers import MessageHandler, ChatMemberUpdatedHandler
            from plugins.userbot.market_seller import _process_chat_member
            from plugins.mgmt.store_indexer import handle_live_channel_show_arrival
            mgmt_bot.add_handler(MessageHandler(handle_live_channel_show_arrival, filters.channel))
            mgmt_bot.add_handler(ChatMemberUpdatedHandler(_process_chat_member))
        except Exception as ex:
            logger.warning(f"Could not attach extra channel handlers to mgmt_bot: {ex}")

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
        # In-memory ban status cache: {user_id: (is_blocked, timestamp)}
        _BAN_CACHE = {}
        _BAN_CACHE_TTL = 60.0  # Cache ban status for 60 seconds

        async def _premium_ban_interceptor(client, update):
            try:
                user = getattr(update, 'from_user', None)
                if not user:
                    return
                user_id = user.id

                # Skip owners
                if Config.OWNER_IDS and user_id in Config.OWNER_IDS:
                    return

                import time
                now_ts = time.time()
                cached = _BAN_CACHE.get(user_id)
                if cached and (now_ts - cached[1]) < _BAN_CACHE_TTL:
                    is_blocked = cached[0]
                else:
                    is_blocked = False

                    # 1. Check forward-bot.premium_bans (AryaPremium's own ban system)
                    try:
                        prem_ban = await asyncio.wait_for(db.db.premium_bans.find_one({"_id": user_id}), timeout=1.5)
                        if prem_ban and prem_ban.get("status") in ("banned", "flagged"):
                            is_blocked = True
                    except Exception:
                        pass

                    # 2 & 3. Check arya database (main bot bans) — uses same MongoDB cluster
                    if not is_blocked and getattr(db, "client", None):
                        try:
                            arya_db_ref = db.client["arya"]
                            # 2. arya.premium_bans (admin panel ban)
                            arya_prem_ban = await asyncio.wait_for(arya_db_ref.premium_bans.find_one({"_id": user_id}), timeout=1.5)
                            if arya_prem_ban and arya_prem_ban.get("status") in ("banned", "flagged"):
                                is_blocked = True
                            # 3. arya.users.ban_status (main bot /ban command)
                            if not is_blocked:
                                arya_user = await asyncio.wait_for(arya_db_ref.users.find_one({"id": user_id}), timeout=1.5)
                                if arya_user and arya_user.get("ban_status", {}).get("is_banned"):
                                    is_blocked = True
                        except Exception:
                            pass  # Cross-DB check failed — fall through (don't block on error)

                    # Update cache (evict old entries if cache grows too large)
                    if len(_BAN_CACHE) > 10000:
                        _BAN_CACHE.clear()
                    _BAN_CACHE[user_id] = (is_blocked, now_ts)

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

            # ── Register all normal handlers (incoming user messages only) ──
            cli.add_handler(MessageHandler(_process_start, filters.command("start") & filters.private & filters.incoming & ~filters.me))
            cli.add_handler(MessageHandler(_process_my_stories, filters.command(["mystories", "stories"]) & filters.private & filters.incoming & ~filters.me))
            # Media handler (feedback + screenshot) — must come before _process_screenshot
            cli.add_handler(MessageHandler(_process_media, (filters.photo | filters.video | filters.animation | filters.document | filters.voice | filters.audio) & filters.private & filters.incoming & ~filters.me))
            cli.add_handler(MessageHandler(_process_text, filters.text & filters.private & filters.incoming & ~filters.me))
            cli.add_handler(CallbackQueryHandler(_process_callback, filters.regex(r'^mb#')))
            cli.add_handler(ChatMemberUpdatedHandler(_process_chat_member))
            cli.add_handler(InlineQueryHandler(_process_inline_query))
            
            # ── Live Channel Show Arrival & Indexer Listener ─────────────────
            try:
                from plugins.mgmt.store_indexer import handle_live_channel_show_arrival
                cli.add_handler(MessageHandler(handle_live_channel_show_arrival, filters.channel))
            except Exception as e:
                logger.warning(f"Could not register handle_live_channel_show_arrival on bot {b.get('username')}: {e}")

            b_mode = str((b.get('config') or {}).get('bot_mode') or b.get('bot_mode') or 'full').lower().strip()
            cli.bot_mode = b_mode
            cli.bot_id = b.get('id')
            cli.bot_username = (b.get('username') or '').replace('@', '').strip()

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
        except Exception as e:
            logger.error(f"Failed to start app {getattr(app, 'name', 'Unknown')}: {e}")

    if not started_apps:
        logger.error("No apps successfully started. Exiting.")
        return

    # Staggered background task to warm up cache safely without overwhelming Telegram DC
    async def _staggered_warmup(apps_to_warm):
        await asyncio.sleep(5)  # Wait for startup to settle
        for client in apps_to_warm:
            try:
                if getattr(client, "is_connected", False):
                    me = await asyncio.wait_for(client.get_me(), timeout=10.0)
                    logger.info(f"[{getattr(client, 'name', 'Client')}] Peer cache warmed up (@{getattr(me, 'username', 'bot')}).")
            except Exception as e:
                logger.debug(f"[{getattr(client, 'name', 'Client')}] Warmup skipped: {e}")
            await asyncio.sleep(1.0)
    
    asyncio.create_task(_staggered_warmup(list(started_apps)))

    # ── Client Liveness & Watchdog Worker ──
    # Pyrogram handles normal MTProto reconnections automatically in its background session worker.
    # The watchdog safely checks client liveness and ONLY restarts genuinely dead clients after
    # properly stopping them first. It never calls start() on an initialized client and never blocks on stdin.
    async def _client_watchdog():
        await asyncio.sleep(30)  # Let ecosystem settle after initial startup
        disconnect_cycles = {}
        mgmt_failed_reported = False

        while True:
            try:
                # 1. Verify Management Bot liveness
                if 'mgmt_bot' in locals() and mgmt_bot:
                    if mgmt_bot not in started_apps:
                        if not mgmt_failed_reported:
                            logger.warning("[Watchdog] Management Bot was not started (check MGMT_BOT_TOKEN).")
                            mgmt_failed_reported = True
                    else:
                        is_conn = getattr(mgmt_bot, "is_connected", False)
                        if not is_conn:
                            disconnect_cycles["mgmt_bot"] = disconnect_cycles.get("mgmt_bot", 0) + 1
                            logger.warning(f"[Watchdog] ⚠️ Management Bot disconnected (cycle {disconnect_cycles['mgmt_bot']}/6)...")
                            if disconnect_cycles["mgmt_bot"] >= 6:
                                logger.info("[Watchdog] Management Bot disconnected >3m. Performing safe restart...")
                                try:
                                    await asyncio.wait_for(mgmt_bot.stop(), timeout=5.0)
                                except Exception:
                                    pass
                                await asyncio.sleep(2)
                                try:
                                    await asyncio.wait_for(mgmt_bot.start(), timeout=15.0)
                                    disconnect_cycles["mgmt_bot"] = 0
                                    logger.info("[Watchdog] ✅ Management Bot safely reconnected!")
                                except Exception as rec_err:
                                    logger.error(f"[Watchdog] Failed to restart Management Bot: {rec_err}")
                        else:
                            disconnect_cycles["mgmt_bot"] = 0

                # 2. Verify Store Bots liveness
                for b_id, cli in list(market_clients.items()):
                    b_key = str(b_id)
                    is_conn = getattr(cli, "is_connected", False)
                    if not is_conn:
                        disconnect_cycles[b_key] = disconnect_cycles.get(b_key, 0) + 1
                        u_name = getattr(cli, 'bot_username', b_id)
                        logger.warning(f"[Watchdog] ⚠️ Market client @{u_name} disconnected (cycle {disconnect_cycles[b_key]}/6)...")
                        if disconnect_cycles[b_key] >= 6:
                            logger.info(f"[Watchdog] Market client @{u_name} disconnected >3m. Performing safe restart...")
                            try:
                                await asyncio.wait_for(cli.stop(), timeout=5.0)
                            except Exception:
                                pass
                            await asyncio.sleep(2)
                            try:
                                await asyncio.wait_for(cli.start(), timeout=15.0)
                                disconnect_cycles[b_key] = 0
                                logger.info(f"[Watchdog] ✅ Market client @{u_name} safely reconnected!")
                            except Exception as rec_err:
                                logger.error(f"[Watchdog] Failed to restart @{u_name}: {rec_err}")
                    else:
                        disconnect_cycles[b_key] = 0

                # 3. Dynamic Bot Auto-Discovery from MongoDB (hot-reload newly added bots without restart)
                try:
                    active_bots = await db.db.premium_bots.find({"status": {"$ne": "inactive"}}).to_list(length=None)
                    existing_bids = set(market_clients.keys())
                    for b in active_bots:
                        bid_str = str(b.get("id"))
                        tok = (b.get("token") or "").strip()
                        if bid_str not in existing_bids and tok and tok not in seen_tokens:
                            seen_tokens.add(tok)
                            logger.info(f"[Watchdog] Discovered new bot @{b.get('username')} in DB! Initializing dynamically...")
                            new_cli = Client(
                                name=f"market_{b['id']}", 
                                api_id=Config.API_ID, 
                                api_hash=Config.API_HASH, 
                                bot_token=tok, 
                                in_memory=False
                            )
                            setup_ask_router(new_cli)
                            new_cli.add_handler(MessageHandler(_premium_ban_interceptor, filters.private), group=-999)
                            new_cli.add_handler(CallbackQueryHandler(_premium_ban_interceptor, filters.all), group=-999)
                            new_cli.add_handler(MessageHandler(_process_start, filters.command("start") & filters.private & filters.incoming & ~filters.me))
                            new_cli.add_handler(MessageHandler(_process_my_stories, filters.command(["mystories", "stories"]) & filters.private & filters.incoming & ~filters.me))
                            new_cli.add_handler(MessageHandler(_process_media, (filters.photo | filters.video | filters.animation | filters.document | filters.voice | filters.audio) & filters.private & filters.incoming & ~filters.me))
                            new_cli.add_handler(MessageHandler(_process_text, filters.text & filters.private & filters.incoming & ~filters.me))
                            new_cli.add_handler(CallbackQueryHandler(_process_callback, filters.regex(r'^mb#')))
                            new_cli.add_handler(ChatMemberUpdatedHandler(_process_chat_member))
                            new_cli.add_handler(InlineQueryHandler(_process_inline_query))
                            
                            try:
                                from plugins.mgmt.store_indexer import handle_live_channel_show_arrival
                                new_cli.add_handler(MessageHandler(handle_live_channel_show_arrival, filters.channel))
                            except Exception:
                                pass

                            b_mode = str((b.get('config') or {}).get('bot_mode') or b.get('bot_mode') or 'full').lower().strip()
                            new_cli.bot_mode = b_mode
                            new_cli.bot_id = b.get('id')
                            new_cli.bot_username = (b.get('username') or '').replace('@', '').strip()

                            try:
                                await asyncio.wait_for(new_cli.start(), timeout=20.0)
                                market_clients[bid_str] = new_cli
                                started_apps.append(new_cli)
                                logger.info(f"[Watchdog] ✅ Dynamically added bot @{new_cli.bot_username} is now online!")
                            except Exception as start_err:
                                logger.error(f"[Watchdog] Failed to start dynamically discovered bot @{b.get('username')}: {start_err}")
                except Exception as dyn_err:
                    logger.debug(f"[Watchdog] Dynamic bot discovery check: {dyn_err}")

            except Exception as w_err:
                logger.warning(f"[Watchdog] Error in watchdog loop: {w_err}")

            await asyncio.sleep(30)

    asyncio.create_task(_client_watchdog())

    # Start Premium Live Monitor on Mgmt Bot (which is Admin in source channels)
    if 'mgmt_bot' in locals() and mgmt_bot in started_apps:
        try:
            from plugins.premium_live_monitor import start_premium_live_monitor
            asyncio.create_task(start_premium_live_monitor(mgmt_bot))
        except Exception as e:
            logger.warning(f"Could not start Premium Live Monitor: {e}")

    # Start Auto Instant Delivery Queue Worker (runs DM delivery via store bots)
    try:
        from purchase_dm_helper import start_auto_delivery_queue_worker
        mb_inst = mgmt_bot if ('mgmt_bot' in locals() and mgmt_bot in started_apps) else None
        asyncio.create_task(start_auto_delivery_queue_worker(market_clients, mb_inst, db))
        logger.info("✅ Auto Instant Delivery Queue Worker started in main.py")
    except Exception as e:
        logger.warning(f"Could not start Auto Delivery Queue Worker: {e}")

    # Start Weekly Self-Healing Dead File & Index Cleaner Worker (paced and safe)
    async def _weekly_dead_file_cleaner_task():
        await asyncio.sleep(3600)  # Wait 1 hour after boot before doing background maintenance
        while True:
            try:
                from utils import scan_and_index_story
                logger.info("[AutoCleaner] Running periodic dead file & index validation...")
                stories = await db.db.premium_stories.find({}).to_list(length=None)
                for s in stories:
                    raw_src = s.get("source") or s.get("source_channel") or s.get("channel_id")
                    st_id = s.get("start_id")
                    en_id = s.get("end_id") or s.get("end_message_id")
                    val_ids = s.get("valid_file_ids")
                    if not raw_src or not st_id or not en_id:
                        continue
                    try:
                        src_id = int(raw_src)
                        if src_id > 0 and len(str(src_id)) >= 9:
                            src_id = int(f"-100{src_id}")
                        st_id, en_id = int(st_id), int(en_id)
                    except Exception:
                        continue

                    # Fast-skip clean stories in 0.0001s
                    if isinstance(val_ids, list) and len(val_ids) > 0 and max(val_ids) >= en_id and min(val_ids) >= st_id and s.get("file_count") == len(val_ids):
                        continue

                    # Find active, connected bot to repair
                    target_cli = None
                    active_candidates = [c for c in started_apps if getattr(c, "is_connected", False)]
                    for c in active_candidates:
                        try:
                            await asyncio.wait_for(c.get_chat(src_id), timeout=5.0)
                            target_cli = c
                            break
                        except Exception:
                            continue
                    if target_cli:
                        try:
                            await scan_and_index_story(target_cli, s, save_to_db=True, db=db)
                        except Exception as sc_err:
                            logger.debug(f"[AutoCleaner] Skip scan: {sc_err}")
                        await asyncio.sleep(2.0)  # Safe pacing to prevent FloodWait
            except Exception as e:
                logger.warning(f"[AutoCleaner] Error during auto-clean: {e}")
            # Run every 7 days (7 * 24 * 3600 seconds)
            await asyncio.sleep(7 * 24 * 3600)

    asyncio.create_task(_weekly_dead_file_cleaner_task())

    # Auto-Restore any story banners that were contaminated by checkout instructions
    try:
        from scripts.restore_story_banners import restore_contaminated_banners
        mb_for_restore = mgmt_bot if 'mgmt_bot' in locals() else None
        asyncio.create_task(restore_contaminated_banners(db=db, bot_client=mb_for_restore))
    except Exception as e:
        logger.warning(f"Could not trigger story banner restoration task: {e}")

    # Auto-Clean duplicate and corrupted show titles in MongoDB
    try:
        from plugins.mgmt.store_indexer import cleanup_duplicate_and_corrupted_shows
        asyncio.create_task(cleanup_duplicate_and_corrupted_shows())
    except Exception as e:
        logger.warning(f"Could not trigger cleanup_duplicate_and_corrupted_shows: {e}")

    # Keep bots running
    await idle()

    # Graceful shutdown with per-app timeout to prevent systemd timeout kills
    for app in started_apps:
        try:
            await asyncio.wait_for(app.stop(), timeout=5.0)
        except Exception:
            pass


if __name__ == "__main__":
    def _handle_asyncio_exception(loop, context):
        """Global handler for unhandled asyncio task exceptions.
        Prevents bare 'Task exception was never retrieved' from crashing the process."""
        exc = context.get("exception")
        msg = exc if exc is not None else context.get("message")
        task = context.get("task")
        task_name = getattr(task, "get_name", lambda: "unknown")() if task else "unknown"

        # Suppress verbose FloodWait tracebacks in Pyrogram background update tasks
        try:
            from pyrogram.errors import FloodWait
            if isinstance(exc, FloodWait) or "FloodWait" in str(msg):
                logger.warning(f"[AsyncIO] Background task hit FloodWait in '{task_name}': {msg}")
                return
        except Exception:
            pass

        logger.error(f"[AsyncIO] Unhandled task exception in '{task_name}': {msg}", exc_info=exc)

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.set_exception_handler(_handle_asyncio_exception)
        loop.run_until_complete(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down Ecosystem...")
    except Exception as e:
        logger.critical(f"Ecosystem crashed with exception: {e}", exc_info=True)
        sys.exit(1)
