import os
target = "c:/Users/User/Downloads/AryaBotNew/TryAryaBot/plugins/share_bot.py"

with open(target, "r", encoding="utf-8") as f:
    content = f.read()

replacement = """
    if bot_id:
        await db.increment_bot_delivery_count(bot_id, total)
    grand_total = (await db.get_bot_delivery_count(bot_id)) if bot_id else total

    u_name = message.from_user.first_name or "you"
    last   = (" " + message.from_user.last_name) if getattr(message.from_user, "last_name", None) else ""
    full_name = f"{u_name}{last}"
    b_name = client.me.first_name if getattr(client, "me", None) else "this bot"

    try:
        import plugins.arya_logger as _alog
        import asyncio
        if total > 0:
            file_desc = f"{total} file(s) via batch {uuid_str}"
            asyncio.create_task(_alog.log_share_delivery(
                user_id, full_name, b_name, str(bot_id or "bot"), file_desc
            ))
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Failed to log share delivery: {e}")
"""

content = content.replace(
    '    if bot_id:\n        await db.increment_bot_delivery_count(bot_id, total)\n    grand_total = (await db.get_bot_delivery_count(bot_id)) if bot_id else total\n\n    u_name = message.from_user.first_name or "you"\n    last   = (" " + message.from_user.last_name) if getattr(message.from_user, "last_name", None) else ""\n    full_name = f"{u_name}{last}"\n    \n    b_name = client.me.first_name if getattr(client, "me", None) else "this bot"',
    replacement.strip('\n')
)

with open(target, "w", encoding="utf-8") as f:
    f.write(content)

print("Updated share_bot.py")
