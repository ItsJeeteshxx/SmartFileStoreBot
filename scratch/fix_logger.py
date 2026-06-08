import os

target = "c:/Users/User/Downloads/AryaBotNew/TryAryaBot/plugins/arya_logger.py"

with open(target, "r", encoding="utf-8") as f:
    content = f.read()

ist_helper = """
def _ist_str() -> str:
    from datetime import datetime, timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%Y-%m-%d %I:%M:%S %p IST")

"""

if "def _ist_str" not in content:
    # Insert right before log_ban
    content = content.replace("async def log_ban(", ist_helper + "async def log_ban(")

# Replace all old UTC timestamps
content = content.replace(
    'ts = _t.strftime("%Y-%m-%d %H:%M:%S UTC", _t.gmtime())',
    'ts = _ist_str()'
)

# Add log_share_delivery if not exists
share_logger = """
async def log_share_delivery(
    user_id: int,
    user_name: str,
    bot_name: str,
    bot_id: str,
    file_name: str,
) -> None:
    \"\"\"Log when a Share Bot delivers a file to a user.\"\"\"
    ts = _ist_str()
    text = (
        f"<b>📤 File Delivered</b>\\n"
        f"━━━━━━━━━━━━━━━━━━━━\\n"
        f"<b>User:</b> <a href='tg://user?id={user_id}'>{_esc(user_name)}</a>  "
        f"[<code>{user_id}</code>]\\n"
        f"<b>File:</b> {_esc(file_name)}\\n"
        f"<b>Bot:</b> {_esc(bot_name)} (<code>{bot_id}</code>)\\n"
        f"<b>Time:</b> <code>{ts}</code>"
    )
    await _send(text, 'ch_share')

"""

if "log_share_delivery" not in content:
    content += "\n" + share_logger

# Add ch_share to the cache fallback in _get_cfg
if "'ch_share'" not in content:
    content = content.replace(
        "'ch_errors':    0,",
        "'ch_errors':    0,\n                'ch_share':     0,"
    )

with open(target, "w", encoding="utf-8") as f:
    f.write(content)

print("arya_logger.py updated with IST and log_share_delivery.")
