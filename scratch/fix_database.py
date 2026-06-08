import os

target = "c:/Users/User/Downloads/AryaBotNew/TryAryaBot/database.py"

with open(target, "r", encoding="utf-8") as f:
    content = f.read()

content = content.replace(
    "            'ch_errors':    0,\n        }",
    "            'ch_errors':    0,\n            'ch_share':     0,\n        }"
)

content = content.replace(
    "_VALID = {'ch_bans', 'ch_new_users', 'ch_batch', 'ch_live', 'ch_cleaner', 'ch_errors'}",
    "_VALID = {'ch_bans', 'ch_new_users', 'ch_batch', 'ch_live', 'ch_cleaner', 'ch_errors', 'ch_share'}"
)

with open(target, "w", encoding="utf-8") as f:
    f.write(content)

print("Updated database.py")
