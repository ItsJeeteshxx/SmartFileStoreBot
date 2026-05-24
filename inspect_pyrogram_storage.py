from pyrogram.storage import SQLiteStorage
import inspect

for k, v in SQLiteStorage.__dict__.items():
    if not k.startswith("__"):
        print(k)

print("\n--- Source of update ---")
try:
    print(inspect.getsource(SQLiteStorage.update))
except Exception as e:
    print("Error getting source:", e)
