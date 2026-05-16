import re

with open('plugins/share_bot.py', 'r', encoding='utf-8') as f:
    content = f.read()

broadcast_logic = """
async def _process_share_broadcast(client, message):
    from config import Config
    if message.from_user.id not in Config.OWNER_IDS:
        return
    
    bot_id = str(client.me.id) if client.me else None
    if not bot_id:
        return
        
    b_msg = message.reply_to_message
    if not b_msg:
        await message.reply_text("Reply to a message to broadcast.")
        return
        
    sts = await message.reply_text("Broadcasting your messages via Delivery Bot...")
    
    users = await db.get_share_bot_users(bot_id)
    total_users = len(users)
    
    import time, datetime
    from pyrogram.errors import FloodWait, UserIsBlocked, InputUserDeactivated
    
    start_time = time.time()
    done = 0
    blocked = 0
    deleted = 0
    failed = 0
    success = 0
    
    async def copy_msg(user_id):
        try:
            await b_msg.copy(chat_id=user_id)
            return True, "Success"
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            return await copy_msg(user_id)
        except InputUserDeactivated:
            return False, "Deleted"
        except UserIsBlocked:
            return False, "Blocked"
        except Exception:
            return False, "Error"

    for u_id in users:
        pti, sh = await copy_msg(int(u_id))
        if pti:
            success += 1
            await asyncio.sleep(0.5)
        else:
            if sh == "Blocked": blocked += 1
            elif sh == "Deleted": deleted += 1
            else: failed += 1
            
        done += 1
        if done % 20 == 0:
            try:
                await sts.edit(f"Delivery Bot Broadcast:\\n\\nTotal Users {total_users}\\nCompleted: {done} / {total_users}\\nSuccess: {success}\\nBlocked: {blocked}\\nDeleted: {deleted}")
            except: pass
            
    time_taken = datetime.timedelta(seconds=int(time.time()-start_time))
    await sts.edit(f"Delivery Bot Broadcast Completed in {time_taken}.\\n\\nTotal Users {total_users}\\nCompleted: {done} / {total_users}\\nSuccess: {success}\\nBlocked: {blocked}\\nDeleted: {deleted}")

"""

if '_process_share_broadcast' not in content:
    content = content.replace('def register_share_handlers(app: Client):', broadcast_logic + '\ndef register_share_handlers(app: Client):')
    
    handler_registration = """    app.add_handler(MessageHandler(
        _process_share_broadcast,
        filters.private & filters.command("broadcast") & filters.reply
    ))
"""
    content = content.replace('    # Auto-approve join requests for JR channels so users get instant access', handler_registration + '    # Auto-approve join requests for JR channels so users get instant access')

    with open('plugins/share_bot.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("Added broadcast to share bot")
else:
    print("Already exists")
