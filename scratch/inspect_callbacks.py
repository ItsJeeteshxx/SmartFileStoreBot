import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyrogram import Client
from plugins.settings import settings_query, owners_cb, protected_chats_cb
from plugins.commands import helpcb, how_to_use, back, status, about, whats_new

print("Checking registered callback query handlers...")

# We can instantiate a dummy client and check its handlers
client = Client("dummy_client", api_id=12345, api_hash="dummy")

# But wait, decorators register handlers on Client class or instances.
# In Pyrogram, decorators Client.on_callback_query register handlers in Client.decorators or Client.handlers.
print("Client decorators:", len(Client.decorators))
for group, decorators in Client.decorators.items():
    print(f"Group {group}:")
    for d in decorators:
        func, handler = d
        print(f"  Function: {func.__name__}, Handler Type: {type(handler).__name__}")
        if hasattr(handler, 'filters'):
            print(f"    Filters: {handler.filters}")
