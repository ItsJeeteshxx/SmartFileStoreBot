import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

print("Testing imports...")
try:
    import config
    print("config imported successfully")
except Exception as e:
    print("Error importing config:", e)

try:
    import database
    print("database imported successfully")
except Exception as e:
    print("Error importing database:", e)

try:
    import plugins.settings
    print("plugins.settings imported successfully")
except Exception as e:
    print("Error importing plugins.settings:", e)

try:
    import plugins.share_bot
    print("plugins.share_bot imported successfully")
except Exception as e:
    print("Error importing plugins.share_bot:", e)

print("Testing imports complete.")
