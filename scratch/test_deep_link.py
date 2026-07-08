import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set dummy env vars
os.environ["API_ID"] = "12345"
os.environ["API_HASH"] = "dummy"
os.environ["BOT_TOKEN"] = "dummy"
os.environ["DATABASE"] = "mongodb://localhost:27017"
os.environ["DATABASE_NAME"] = "arya"

import database
database.db = AsyncMock()

# Mock DB methods for deep link processing
database.db.get_ban_status = AsyncMock(return_value={'is_banned': False})
database.db.get_share_link = AsyncMock(return_value={
    '_id': 'some_uuid',
    'message_ids': [12345],
    'source_chat': -10022334455,
    'protect': False
})
database.db.get_bot_fsub_channels = AsyncMock(return_value=[])
database.db.get_bot_fetching_media = AsyncMock(return_value=[])
database.db.get_share_bot_text = AsyncMock(return_value="")
database.db.get_share_text = AsyncMock(return_value="")
database.db.get_share_protect_global = AsyncMock(return_value=False)
database.db.get_user_strike = AsyncMock(return_value={'count': 0})
database.db.update_user_strike = AsyncMock()

from plugins.share_bot import _process_start

async def main():
    print("Simulating deep link /start share_some-uuid...")
    client = AsyncMock()
    # Mock client.me
    client.me = MagicMock()
    client.me.id = 7991415910
    client.me.username = "test_bot"
    
    # Mock copy_message to return a dummy message
    dummy_sent = MagicMock()
    dummy_sent.id = 9999
    client.copy_message = AsyncMock(return_value=dummy_sent)
    
    # Mock message
    message = MagicMock()
    message.from_user.id = 1011040900
    message.from_user.first_name = "User"
    message.from_user.last_name = ""
    message.from_user.mention = "User"
    message.command = ["start", "share_some-uuid"]
    message.reply_text = AsyncMock()
    
    try:
        await _process_start(client, message)
        print("Success for deep link start command!")
    except Exception as e:
        print("Error:", e)
        import traceback
        traceback.print_exc()

asyncio.run(main())
