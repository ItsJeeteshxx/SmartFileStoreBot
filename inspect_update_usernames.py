from pyrogram.storage import SQLiteStorage
import inspect

try:
    print(inspect.getsource(SQLiteStorage.update_usernames))
except Exception as e:
    print("Error:", e)
