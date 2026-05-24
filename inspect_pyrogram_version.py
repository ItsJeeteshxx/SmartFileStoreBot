from pyrogram.storage import SQLiteStorage
import inspect

print("SQLiteStorage version:", SQLiteStorage.SCHEMA_VERSION)
# Let's inspect the update method code
try:
    print(inspect.getsource(SQLiteStorage.update))
except Exception as e:
    print("Error getting source:", e)
