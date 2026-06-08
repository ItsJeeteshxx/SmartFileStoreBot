import os
target = "c:/Users/User/Downloads/AryaBotNew/TryAryaBot/plugins/settings.py"

with open(target, "r", encoding="utf-8") as f:
    content = f.read()

# Replace CH_KEYS list
content = content.replace(
    "          ('ch_errors',    \"❌ Error Alerts\"),\n      ]",
    "          ('ch_errors',    \"❌ Error Alerts\"),\n          ('ch_share',     \"📤 Share Logs\"),\n      ]"
)

# Replace CH_MAP
content = content.replace(
    "          'ch_errors':    \"❌ Error Alerts\",\n      }",
    "          'ch_errors':    \"❌ Error Alerts\",\n          'ch_share':     \"📤 Share Logs\",\n      }"
)

# Replace count
content = content.replace(
    "['ch_bans', 'ch_new_users', 'ch_batch', 'ch_live', 'ch_cleaner', 'ch_errors']",
    "['ch_bans', 'ch_new_users', 'ch_batch', 'ch_live', 'ch_cleaner', 'ch_errors', 'ch_share']"
)

with open(target, "w", encoding="utf-8") as f:
    f.write(content)

print("Updated settings.py")
