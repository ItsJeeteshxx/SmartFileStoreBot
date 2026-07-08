import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyrogram import Client
from config import Config

# Instantiate a client without starting it
client = Client("test_session_name_xyz", api_id=12345, api_hash="dummy")

print("Client name:", getattr(client, "name", None))
print("Client session_name:", getattr(client, "session_name", None))
