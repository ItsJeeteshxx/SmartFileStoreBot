import sys
import pyrogram.storage.sqlite_storage
import re

print("Before patching:")
print("UPDATE_STATE_SCHEMA:", repr(pyrogram.storage.sqlite_storage.UPDATE_STATE_SCHEMA))

# Apply generic patch
try:
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
                print(f"Patched: {name}")
except Exception as e:
    print(f"Failed: {e}")

print("\nAfter patching:")
print("UPDATE_STATE_SCHEMA:", repr(pyrogram.storage.sqlite_storage.UPDATE_STATE_SCHEMA))
print("USERNAMES_SCHEMA:", repr(pyrogram.storage.sqlite_storage.USERNAMES_SCHEMA))
