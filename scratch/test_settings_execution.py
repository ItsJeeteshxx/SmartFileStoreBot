import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set dummy env vars so config doesn't complain
os.environ["API_ID"] = "12345"
os.environ["API_HASH"] = "dummy"
os.environ["BOT_TOKEN"] = "dummy"
os.environ["DATABASE"] = "mongodb://localhost:27017"
os.environ["DATABASE_NAME"] = "arya"

# Mock the database before importing settings
import database
database.db = AsyncMock()

# Mock get_language
database.db.get_language = AsyncMock(return_value="en")

# Mock get_configs
database.db.get_configs = AsyncMock(return_value={
    'bot_mode': 'forward',
    'menu_image_id': None,
    'filters': {}
})

# Mock other database methods called during settings#sharebot
database.db.get_share_bots = AsyncMock(return_value=[])
database.db.get_share_protect_global = AsyncMock(return_value=False)
database.db.get_logs_config = AsyncMock(return_value={})
database.db.get_anti_abuse_config = AsyncMock(return_value={})

from plugins.settings import settings_query

async def main():
    print("Simulating CallbackQuery settings#main...")
    bot = AsyncMock()
    query = MagicMock()
    query.from_user.id = 5123283499
    query.data = "settings#main"
    query.message = AsyncMock()
    
    # Mock message attributes
    query.message.chat.id = 12345
    query.message.photo = None
    
    try:
        await settings_query(bot, query)
        print("Success for settings#main!")
    except Exception as e:
        print("Error for settings#main:", e)
        import traceback
        traceback.print_exc()

    print("\nSimulating CallbackQuery settings#sharebot...")
    query.data = "settings#sharebot"
    try:
        await settings_query(bot, query)
        print("Success for settings#sharebot!")
    except Exception as e:
        print("Error for settings#sharebot:", e)
        import traceback
        traceback.print_exc()

asyncio.run(main())
