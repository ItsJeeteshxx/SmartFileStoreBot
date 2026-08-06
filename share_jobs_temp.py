"""
Share Batch Links Automator
===========================
Generates File-Sharing deep links from a hidden database channel
and automatically posts the grouped batch buttons into a Public Channel.
"""
import uuid
import math
import asyncio
import logging
from pyrogram import Client, filters, ContinuePropagation
from pyrogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton
)
from database import db
from plugins.test import CLIENT

logger = logging.getLogger(__name__)
_CLIENT = CLIENT()

def to_custom_font(text: str, style: str) -> str:
    if style == "Default": return text
    res = ""
    for c in text:
        if style == "𝑅𝑒𝑔𝑢𝑙𝑢𝑠":
            if 'a' <= c <= 'z': res += chr(0x1D44E + ord(c) - ord('a'))
            elif 'A' <= c <= 'Z': res += chr(0x1D434 + ord(c) - ord('A'))
            else: res += c
        elif style == "𝑨𝒍𝒕𝒂𝒊𝒓":
            if 'a' <= c <= 'z': res += chr(0x1D482 + ord(c) - ord('a'))
            elif 'A' <= c <= 'Z': res += chr(0x1D468 + ord(c) - ord('A'))
            else: res += c
        elif style == "𝐋𝐔𝐃":
            if 'a' <= c <= 'z': res += chr(0x1D41A + ord(c) - ord('a'))
            elif 'A' <= c <= 'Z': res += chr(0x1D400 + ord(c) - ord('A'))
            else: res += c
        else:
            res += c
    return res

# â”€â”€ Self-contained Future-based ask() â€” avoids cross-module routing conflicts â”€â”€
_sj_waiting: dict[int, asyncio.Future] = {}

@Client.on_message(filters.private, group=-14)
async def _sj_input_router(bot, message):
    """Route private messages to share_jobs _ask() futures."""
    uid = message.from_user.id if message.from_user else None
    if uid and uid in _sj_waiting:
        fut = _sj_waiting.pop(uid)
        if not fut.done():
            fut.set_result(message)
    raise ContinuePropagation

async def _ask(bot, user_id: int, text: str, reply_markup=None, timeout: int = 300):
    """Send text and wait for the next private message from user_id."""
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    old = _sj_waiting.pop(user_id, None)
    if old and not old.done():
        old.cancel()
    _sj_waiting[user_id] = fut
    await bot.send_message(user_id, text, reply_markup=reply_markup)
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        _sj_waiting.pop(user_id, None)
        raise

def _sc(text: str) -> str:
    return text.translate(str.maketrans(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "á´€Ê™á´„á´…á´‡êœ°É¢ÊœÉªá´Šá´‹ÊŸá´É´á´á´˜Ç«Ê€êœ±á´›á´œá´ á´¡xÊá´¢á´€Ê™á´„á´…á´‡êœ°É¢ÊœÉªá´Šá´‹ÊŸá´É´á´á´˜Ç«Ê€êœ±á´›á´œá´ á´¡xÊá´¢"
    ))

new_share_job = {}

async def _create_share_flow(bot, user_id, force_live=False):
    try:
        new_share_job[user_id] = {}
        share_bots = await db.get_share_bots()
        
        if not share_bots:
            return await bot.send_message(user_id, "<b>â€£  No Share Bots available. Please add a Bot Token in /settings -> Share Bots.</b>")
            
        kb = []
        for b in share_bots:
            kb.append([f"{b['name']} (@{b['username']})"])
            
        kb.append(["â›” Cá´€É´á´„á´‡ÊŸ"])
        kb.append(["Scan Database Channel"])
        
        msg = await _ask(bot, user_id, 
            "<b>âª SHARE LINKS: SELECT ACCOUNT â«</b>\n\nChoose the Share Bot you want to use for link generation and delivery:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True)
        )
        if not msg.text or (getattr(msg, 'text', None) and any(x in msg.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”'])) or "â›”" in msg.text or "Cá´€É´á´„á´‡ÊŸ" in msg.text:
            return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())

        #  Scan option 
        if "Scan Database" in msg.text:
            await bot.send_message(user_id, "<b>Â»  Opening Database Scanner...</b>", reply_markup=ReplyKeyboardRemove())
            from plugins.db_scanner import _scan_flow
            return await _scan_flow(bot, user_id)

        # Match bot selection
        import re
        sel = msg.text
        match = re.search(r"@([a-zA-Z0-9_]+)", sel)
        if not match:
            return await bot.send_message(user_id, "<b>â€£  Invalid selection.</b>", reply_markup=ReplyKeyboardRemove())

            
        username = match.group(1)
        selected_bot = next((b for b in share_bots if b['username'] == username), None)
        if not selected_bot:
            return await bot.send_message(user_id, "<b>â€£  Account not found.</b>", reply_markup=ReplyKeyboardRemove())
            
        new_share_job[user_id]['bot_id'] = selected_bot['id']

        chans = await db.get_user_channels(user_id)
        if not chans:
            return await bot.send_message(user_id, "<b>â€£  No channels added in /settings.</b>", reply_markup=ReplyKeyboardRemove())
            
        ch_kb = [[ch['title']] for ch in chans]
        ch_kb.append(["â›” Cá´€É´á´„á´‡ÊŸ"])
        msg = await _ask(bot, user_id, 
            "<b>âª STEP 2: SOURCE DATABASE â«</b>\n\nWhere are the files stored securely?", 
            reply_markup=ReplyKeyboardMarkup(ch_kb, resize_keyboard=True, one_time_keyboard=True)
        )
        if not msg.text or (getattr(msg, 'text', None) and any(x in msg.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”'])) or "â›”" in msg.text or "Cá´€É´á´„á´‡ÊŸ" in msg.text:
            return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
            
        title = msg.text.strip()
        ch = next((c for c in chans if c["title"] == title), None)
        if not ch:
            return await bot.send_message(user_id, "<b>â€£  Source Channel not found.</b>", reply_markup=ReplyKeyboardRemove())
        new_share_job[user_id]['source'] = int(ch['chat_id'])
        
        msg = await _ask(bot, user_id, 
            "<b>âª STEP 3: TARGET PUBLIC CHANNEL â«</b>\n\nWhere should I post the Share Links?", 
            reply_markup=ReplyKeyboardMarkup(ch_kb + [["â†©ï¸ UÉ´á´…á´", "â›” Cá´€É´á´„á´‡ÊŸ"]], resize_keyboard=True, one_time_keyboard=True)
        )
        if not msg.text or (getattr(msg, 'text', None) and any(x in msg.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”'])) or "Cancel" in msg.text:
            return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
        if getattr(msg, "text", None) and any(x in msg.text.lower() for x in ["/undo", "undo", "uÉ´á´…á´", "â†©ï¸"]): 
            # Go back to Step 2 â€” re-ask source channel then re-enter Step 3
            msg2 = await _ask(bot, user_id, 
                "<b>âª STEP 2 (REDO): SOURCE DATABASE â«</b>\n\nWhere are the files stored?", 
                reply_markup=ReplyKeyboardMarkup(ch_kb + [["â›” Cá´€É´á´„á´‡ÊŸ"]], resize_keyboard=True, one_time_keyboard=True)
            )
            if not msg2.text or (getattr(msg2, "text", None) and any(x in msg2.text.lower() for x in ["cancel", "cá´€É´á´„á´‡ÊŸ", "â›”", "/cancel"])): 
                return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
            title2 = msg2.text.replace("Â»  ", "").strip()
            ch2 = next((c for c in chans if c["title"] == title2), None)
            if ch2:
                new_share_job[user_id]['source'] = int(ch2['chat_id'])
            msg = await _ask(bot, user_id,
                "<b>âª STEP 3: TARGET PUBLIC CHANNEL â«</b>\n\nWhere should I post the Share Links?",
                reply_markup=ReplyKeyboardMarkup(ch_kb + [["â›” Cá´€É´á´„á´‡ÊŸ"]], resize_keyboard=True, one_time_keyboard=True)
            )
            if not msg.text or (getattr(msg, "text", None) and any(x in msg.text.lower() for x in ["cancel", "cá´€É´á´„á´‡ÊŸ", "â›”", "/cancel"])): 
                return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())

        title = msg.text.replace("Â»  ", "").strip()
        ch = next((c for c in chans if c["title"] == title), None)
        if not ch:
            return await bot.send_message(user_id, "<b>â€£  Target Channel not found.</b>", reply_markup=ReplyKeyboardRemove())
        new_share_job[user_id]['target'] = int(ch['chat_id'])

        # STEP 3.5: Target Group Topic
        markup_tt = ReplyKeyboardMarkup([["Skip"], ["â†©ï¸ UÉ´á´…á´", "â›” Cá´€É´á´„á´‡ÊŸ"]], resize_keyboard=True, one_time_keyboard=True)
        msg_tt = await _ask(bot, user_id,
            "<b>âª STEP 3.5: TARGET GROUP TOPIC â«</b>\n\nIf the destination is a Group with Topics enabled, please send the <b>Topic ID</b> (a number). Otherwise, just press <b>Skip</b>.\n\n<i>(To find it, copy a message link from the topic. The middle number is the Topic ID. e.g. /c/1234/<b>56</b>/78)</i>",
            reply_markup=markup_tt
        )
        if getattr(msg_tt, 'text', None) and any(x in msg_tt.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
        if getattr(msg_tt, "text", None) and any(x in msg_tt.text.lower() for x in ["/undo", "undo", "uÉ´á´…á´", "â†©ï¸"]):
            # Go back to Step 3
            msg3 = await _ask(bot, user_id, 
                "<b>âª STEP 3 (REDO): TARGET PUBLIC CHANNEL â«</b>\n\nWhere should I post the Share Links?", 
                reply_markup=ReplyKeyboardMarkup(ch_kb + [["â›” Cá´€É´á´„á´‡ÊŸ"]], resize_keyboard=True, one_time_keyboard=True)
            )
            if not msg3.text or (getattr(msg3, "text", None) and any(x in msg3.text.lower() for x in ["cancel", "cá´€É´á´„á´‡ÊŸ", "â›”", "/cancel"])): 
                return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
            title3 = msg3.text.replace("Â»  ", "").strip()
            ch3 = next((c for c in chans if c["title"] == title3), None)
            if ch3:
                new_share_job[user_id]['target'] = int(ch3['chat_id'])
            # re-ask topic
            msg_tt = await _ask(bot, user_id,
                "<b>âª STEP 3.5: TARGET GROUP TOPIC â«</b>\n\nIf the destination is a Group with Topics enabled, please send the <b>Topic ID</b>. Otherwise, press <b>Skip</b>.",
                reply_markup=markup_tt
            )
            if getattr(msg_tt, 'text', None) and any(x in msg_tt.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())

        tt_text = (msg_tt.text or msg_tt.caption or "").strip()
        if tt_text.lower() == "skip" or not tt_text.isdigit():
            new_share_job[user_id]['target_topic_id'] = None
        else:
            new_share_job[user_id]['target_topic_id'] = int(tt_text)

        markup = ReplyKeyboardMarkup([[KeyboardButton("â†©ï¸ UÉ´á´…á´"), KeyboardButton("â›” Cá´€É´á´„á´‡ÊŸ")]], resize_keyboard=True, one_time_keyboard=True)
            
        def parse_id(msg) -> int:
            if getattr(msg, 'forward_from_message_id', None):
                return msg.forward_from_message_id
                
            text = (msg.text or msg.caption or "").strip().rstrip('/')
            if text.isdigit(): return int(text)
            if "t.me/" in text:
                parts = text.split('/')
                if parts[-1].isdigit(): return int(parts[-1])
            raise ValueError("Invalid Message ID or Link (must be forwarded or contain ID)")
            
        markup_status = ReplyKeyboardMarkup([["Â»  Completed", "Â»  Ongoing"], ["â†©ï¸ UÉ´á´…á´", "â›” Cá´€É´á´„á´‡ÊŸ"]], resize_keyboard=True, one_time_keyboard=True)
        msg_status = await _ask(bot, user_id, 
            "<b>âª STEP 4: STORY STATUS â«</b>\n\nIs this story Completed or Ongoing?", 
            reply_markup=markup_status
        )
        if getattr(msg_status, 'text', None) and any(x in msg_status.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
        if getattr(msg_status, "text", None) and any(x in msg_status.text.lower() for x in ["/undo", "undo", "uÉ´á´…á´", "â†©ï¸"]):
            # Go back to Step 3
            msg3 = await _ask(bot, user_id,
                "<b>âª STEP 3 (REDO): TARGET PUBLIC CHANNEL â«</b>\n\nWhere should I post the Share Links?",
                reply_markup=ReplyKeyboardMarkup(ch_kb + [["â›” Cá´€É´á´„á´‡ÊŸ"]], resize_keyboard=True, one_time_keyboard=True)
            )
            if not msg3.text or (getattr(msg3, "text", None) and any(x in msg3.text.lower() for x in ["cancel", "cá´€É´á´„á´‡ÊŸ", "â›”", "/cancel"])): 
                return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
            title3 = msg3.text.replace("Â»  ", "").strip()
            ch3 = next((c for c in chans if c["title"] == title3), None)
            if ch3:
                new_share_job[user_id]['target'] = int(ch3['chat_id'])
            msg_status = await _ask(bot, user_id,
                "<b>âª STEP 4: STORY STATUS â«</b>\n\nIs this story Completed or Ongoing?",
                reply_markup=markup_status
            )
            if getattr(msg_status, 'text', None) and any(x in msg_status.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
        is_completed = "completed" in (msg_status.text or "").lower()
        new_share_job[user_id]['is_completed'] = is_completed

        msg_story = await _ask(bot, user_id, 
            "<b>âª STEP 5: STORY NAME â«</b>\n\nEnter the clean name of the Series/Story (e.g. <code>TDMB</code>):", 
            reply_markup=markup
        )
        if getattr(msg_story, 'text', None) and any(x in msg_story.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
        if getattr(msg_story, "text", None) and any(x in msg_story.text.lower() for x in ["/undo", "undo", "uÉ´á´…á´", "â†©ï¸"]):
            # Re-ask Step 4
            msg_status2 = await _ask(bot, user_id,
                "<b>âª STEP 4 (REDO): STORY STATUS â«</b>\n\nIs this story Completed or Ongoing?",
                reply_markup=markup_status
            )
            if getattr(msg_status2, 'text', None) and any(x in msg_status2.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
            new_share_job[user_id]['is_completed'] = "completed" in (msg_status2.text or "").lower()
            msg_story = await _ask(bot, user_id,
                "<b>âª STEP 5: STORY NAME â«</b>\n\nEnter the clean name of the Series/Story (e.g. <code>TDMB</code>):",
                reply_markup=markup
            )
            if getattr(msg_story, 'text', None) and any(x in msg_story.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
        new_share_job[user_id]['story'] = (msg_story.text or msg_story.caption or "").strip()
        
        markup_source = ReplyKeyboardMarkup([["Â»  Regular Channel", "Â»  Group Topic"], ["â†©ï¸ UÉ´á´…á´", "â›” Cá´€É´á´„á´‡ÊŸ"]], resize_keyboard=True, one_time_keyboard=True)
        msg_stype = await _ask(bot, user_id, 
            "<b>âª STEP 6: SOURCE STRUCTURE â«</b>\n\nAre the files in a normal Channel (requires start/end IDs)\nor inside a specific Group Topic (auto-scans entire topic)?", 
            reply_markup=markup_source
        )
        if getattr(msg_stype, 'text', None) and any(x in msg_stype.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
        if getattr(msg_stype, "text", None) and any(x in msg_stype.text.lower() for x in ["/undo", "undo", "uÉ´á´…á´", "â†©ï¸"]):
            # Re-ask story name
            msg_story2 = await _ask(bot, user_id,
                "<b>âª STEP 5 (REDO): STORY NAME â«</b>\n\nEnter story name:",
                reply_markup=markup
            )
            if getattr(msg_story2, 'text', None) and any(x in msg_story2.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
            new_share_job[user_id]['story'] = (msg_story2.text or "").strip()
            msg_stype = await _ask(bot, user_id,
                "<b>âª STEP 6: SOURCE STRUCTURE â«</b>\n\nChannel or Group Topic?",
                reply_markup=markup_source
            )
            if getattr(msg_stype, 'text', None) and any(x in msg_stype.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
        is_topic = "topic" in (msg_stype.text or "").lower()
        new_share_job[user_id]['is_topic'] = is_topic

        #  STEP 6.5: SELECT ACCOUNT 
        if is_topic:
            accounts = await db.get_bots(user_id)
            if not accounts:
                return await bot.send_message(user_id, "<b>âŒ No accounts found. Add one in /settings â†’ Accounts first.</b>")
                
            userbots = [a for a in accounts if not a.get("is_bot", True)]
            if not userbots:
                return await bot.send_message(user_id, "<b>âŒ You selected 'Group Topic', but you have no Userbot added!</b>\nBots cannot scan Group Topics. Please go to /settings â†’ Accounts and add a Userbot first.")
            valid_accounts = userbots
                
            acc_kb = [[KeyboardButton(f"Â»  Userbot: {a.get('name', '?')}")] for a in valid_accounts]
            acc_kb.append([KeyboardButton("â†©ï¸ UÉ´á´…á´"), KeyboardButton("â›” Cá´€É´á´„á´‡ÊŸ")])
            
            msg_acc = await _ask(bot, user_id,
                "<b>âª STEP 6.5: SCANNING ACCOUNT â«</b>\n\nChoose the Userbot to use for reading files from the Group Topic:\n"
                "<i>(âš ï¸ NOTE: Group Topics MUST be scanned by a Userbot.)</i>",
                reply_markup=ReplyKeyboardMarkup(acc_kb, resize_keyboard=True, one_time_keyboard=True)
            )
            if not msg_acc.text or (getattr(msg_acc, "text", None) and any(x in msg_acc.text.lower() for x in ["cancel", "cá´€É´á´„á´‡ÊŸ", "â›”", "/cancel"])): 
                return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
            if getattr(msg_acc, "text", None) and any(x in msg_acc.text.lower() for x in ["/undo", "undo", "uÉ´á´…á´", "â†©ï¸"]):
                # Re-ask source structure
                msg_stype2 = await _ask(bot, user_id,
                    "<b>âª STEP 6 (REDO): SOURCE STRUCTURE â«</b>\n\nChannel or Group Topic?",
                    reply_markup=markup_source
                )
                if getattr(msg_stype2, 'text', None) and any(x in msg_stype2.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
                is_topic = "topic" in (msg_stype2.text or "").lower()
                new_share_job[user_id]['is_topic'] = is_topic
                if not is_topic:
                    new_share_job[user_id]['account_id'] = None
                else:
                    msg_acc = await _ask(bot, user_id,
                        "<b>âª STEP 6.5: SCANNING ACCOUNT â«</b>\n\nChoose Userbot:",
                        reply_markup=ReplyKeyboardMarkup(acc_kb, resize_keyboard=True, one_time_keyboard=True)
                    )
                    if not msg_acc.text or (getattr(msg_acc, "text", None) and any(x in msg_acc.text.lower() for x in ["cancel", "cá´€É´á´„á´‡ÊŸ", "â›”", "/cancel"])): 
                        return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())

            if is_topic:
                acc_name = msg_acc.text.split(": ", 1)[-1].strip()
                sel_acc = next((a for a in valid_accounts if a.get("name") == acc_name), None)
                if not sel_acc:
                    return await bot.send_message(user_id, "<b>â€£ Account not found.</b>", reply_markup=ReplyKeyboardRemove())
                new_share_job[user_id]['account_id'] = sel_acc['id']
        else:
            new_share_job[user_id]['account_id'] = None  # Default to Main Bot for normal channels.

        if is_topic:
            msg_topic = await _ask(bot, user_id, 
                "<b>âª STEP 7: GROUP TOPIC LINK â«</b>\n\nPaste the link to the Topic (e.g. <code>https://t.me/c/123/45</code>):", 
                reply_markup=markup
            )
            if getattr(msg_topic, 'text', None) and any(x in msg_topic.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
            if getattr(msg_topic, "text", None) and any(x in msg_topic.text.lower() for x in ["/undo", "undo", "uÉ´á´…á´", "â†©ï¸"]):
                return await bot.send_message(user_id, "<b>â€£ Undo: Please restart the Batch Links flow from the menu.</b>", reply_markup=ReplyKeyboardRemove())
            topic_id = parse_id(msg_topic)
            new_share_job[user_id]['topic_id'] = topic_id
            new_share_job[user_id]['start_id'] = topic_id
            new_share_job[user_id]['end_id'] = topic_id
        else:
            msg_start = await _ask(bot, user_id, 
                "<b>âª STEP 7: START MESSAGE â«</b>\n\nForward the first message, send its Message ID, or paste its Link (e.g. <code>https://t.me/c/123/456</code>):", 
                reply_markup=markup
            )
            if getattr(msg_start, 'text', None) and any(x in msg_start.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
            if getattr(msg_start, "text", None) and any(x in msg_start.text.lower() for x in ["/undo", "undo", "uÉ´á´…á´", "â†©ï¸"]):
                # Re-ask Step 6
                msg_stype3 = await _ask(bot, user_id,
                    "<b>âª STEP 6 (REDO): SOURCE STRUCTURE â«</b>\n\nChannel or Group Topic?",
                    reply_markup=markup_source
                )
                if getattr(msg_stype3, 'text', None) and any(x in msg_stype3.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
                new_share_job[user_id]['is_topic'] = "topic" in (msg_stype3.text or "").lower()
                msg_start = await _ask(bot, user_id,
                    "<b>âª STEP 7: START MESSAGE â«</b>\n\nForward or paste the first message:",
                    reply_markup=markup
                )
                if getattr(msg_start, 'text', None) and any(x in msg_start.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
            start_id = parse_id(msg_start)
            new_share_job[user_id]['start_id'] = start_id
            
            msg_end = await _ask(bot, user_id, 
                "<b>âª STEP 8: LAST MESSAGE â«</b>\n\nForward the last message, send its Msg ID, or paste its Link:", 
                reply_markup=markup
            )
            if getattr(msg_end, 'text', None) and any(x in msg_end.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
            if getattr(msg_end, "text", None) and any(x in msg_end.text.lower() for x in ["/undo", "undo", "uÉ´á´…á´", "â†©ï¸"]):
                # Re-ask start_id
                msg_start2 = await _ask(bot, user_id,
                    "<b>âª STEP 7 (REDO): START MESSAGE â«</b>\n\nForward or paste the first message:",
                    reply_markup=markup
                )
                if getattr(msg_start2, 'text', None) and any(x in msg_start2.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
                start_id = parse_id(msg_start2)
                new_share_job[user_id]['start_id'] = start_id
                msg_end = await _ask(bot, user_id,
                    "<b>âª STEP 8: LAST MESSAGE â«</b>\n\nForward or paste the last message:",
                    reply_markup=markup
                )
                if getattr(msg_end, 'text', None) and any(x in msg_end.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
            end_id = parse_id(msg_end)
            new_share_job[user_id]['end_id'] = end_id
            
            if start_id > end_id:
                start_id, end_id = end_id, start_id
                new_share_job[user_id]['start_id'] = start_id
                new_share_job[user_id]['end_id'] = end_id
            
        msg_batch = await _ask(bot, user_id, 
            "<b>âª STEP 9: EPISODES PER BUTTON â«</b>\n\nHow many episodes per link button?\nExample: <code>20</code>", 
            reply_markup=markup
        )
        if getattr(msg_batch, 'text', None) and any(x in msg_batch.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
        if getattr(msg_batch, "text", None) and any(x in msg_batch.text.lower() for x in ["/undo", "undo", "uÉ´á´…á´", "â†©ï¸"]):
            # Re-ask end_id
            if not is_topic:
                msg_end2 = await _ask(bot, user_id,
                    "<b>âª STEP 8 (REDO): LAST MESSAGE â«</b>\n\nForward or paste the last message:",
                    reply_markup=markup
                )
                if getattr(msg_end2, 'text', None) and any(x in msg_end2.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
                new_share_job[user_id]['end_id'] = parse_id(msg_end2)
            msg_batch = await _ask(bot, user_id,
                "<b>âª STEP 9: EPISODES PER BUTTON â«</b>\n\nHow many episodes per link button?",
                reply_markup=markup
            )
            if getattr(msg_batch, 'text', None) and any(x in msg_batch.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
        
        raw_b = (msg_batch.text or msg_batch.caption or "20").strip()
        batch_size = int(raw_b) if raw_b.isdigit() else 20
        if batch_size < 1: batch_size = 20
        new_share_job[user_id]['batch_size'] = batch_size

        msg_bpp = await _ask(bot, user_id, 
            "<b>âª STEP 10: BUTTONS PER POST â«</b>\n\nHow many buttons should appear in one post in the channel?\nExample: <code>10</code>", 
            reply_markup=markup
        )
        if getattr(msg_bpp, 'text', None) and any(x in msg_bpp.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
        if getattr(msg_bpp, "text", None) and any(x in msg_bpp.text.lower() for x in ["/undo", "undo", "uÉ´á´…á´", "â†©ï¸"]):
            # Re-ask batch_size
            msg_batch2 = await _ask(bot, user_id,
                "<b>âª STEP 9 (REDO): EPISODES PER BUTTON â«</b>\n\nHow many episodes per link button?",
                reply_markup=markup
            )
            if getattr(msg_batch2, 'text', None) and any(x in msg_batch2.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
            raw_b2 = (msg_batch2.text or "20").strip()
            new_share_job[user_id]['batch_size'] = int(raw_b2) if raw_b2.isdigit() else 20
            msg_bpp = await _ask(bot, user_id,
                "<b>âª STEP 10: BUTTONS PER POST â«</b>\n\nHow many buttons per post?",
                reply_markup=markup
            )
            if getattr(msg_bpp, 'text', None) and any(x in msg_bpp.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
        
        raw_bpp = (msg_bpp.text or msg_bpp.caption or "10").strip()
        bpp = int(raw_bpp) if raw_bpp.isdigit() else 10
        if bpp < 1: bpp = 10
        new_share_job[user_id]['buttons_per_post'] = bpp

        shortener_apis = await db.get_shortener_apis()
        s_kb = [["Skip (Direct Link)"]]
        if shortener_apis.get("arolinks"): s_kb.append(["AroLinks"])
        if shortener_apis.get("urlshortx"): s_kb.append(["UrlShortX"])
        
        msg_short = await _ask(bot, user_id, 
            "<b>▶ STEP 10.1: LINK SHORTENER ◀</b>\n\nChoose a link shortener or skip for direct Telegram links:", 
            reply_markup=ReplyKeyboardMarkup(s_kb, resize_keyboard=True, one_time_keyboard=True)
        )
        if getattr(msg_short, 'text', None) and any(x in msg_short.text.lower() for x in ['cancel', 'cᴀɴᴄᴇʟ', '⛔']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
        short_choice = msg_short.text.strip()
        new_share_job[user_id]['shortener'] = short_choice if short_choice in ["AroLinks", "UrlShortX"] else None

        f_kb = [["Default"], ["𝑅𝑒𝑔𝑢𝑙𝑢𝑠", "𝑨𝒍𝒕𝒂𝒊𝒓"], ["𝐋𝐔𝐃", "Custom"]]
        msg_font = await _ask(bot, user_id, 
            "<b>▶ STEP 10.2: FONT STYLE ◀</b>\n\nChoose a font style for the button text (e.g. Story Name 1-10):", 
            reply_markup=ReplyKeyboardMarkup(f_kb, resize_keyboard=True, one_time_keyboard=True)
        )
        if getattr(msg_font, 'text', None) and any(x in msg_font.text.lower() for x in ['cancel', 'cᴀɴᴄᴇʟ', '⛔']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
        
        font_choice = msg_font.text.strip()
        if font_choice == "Custom":
            msg_cfont = await _ask(bot, user_id, "<b>Enter your custom prefix (e.g. '𝔐𝔶 𝔖𝔱𝔬𝔯𝔶'):</b>", reply_markup=ReplyKeyboardRemove())
            new_share_job[user_id]['font'] = msg_cfont.text.strip()
        else:
            new_share_job[user_id]['font'] = font_choice

        if force_live:
            msg_live = await _ask(bot, user_id, 
                "<b>âª STEP 11: LIVE MONITORING THRESHOLD â«</b>\n\nHow many new episodes should arrive before posting a new batch automatically?\n\n<i>Send a number (e.g. <code>10</code>).</i>", 
                reply_markup=markup
            )
            if getattr(msg_live, 'text', None) and any(x in msg_live.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
            raw_live = (msg_live.text or msg_live.caption or "10").strip()
            thresh = int(raw_live) if raw_live.isdigit() else 10
            if thresh < 1: thresh = 10
            new_share_job[user_id]['live_threshold'] = thresh
        else:
            msg_live = await _ask(bot, user_id, 
                "<b>âª STEP 11: LIVE MONITORING â«</b>\n\nHow many new episodes should arrive before posting a new batch automatically?\n\n<i>Send <code>0</code> or <code>Skip</code> to disable Live Monitoring. Send <code>10</code> to bundle 10 incoming files per batch.</i>", 
                reply_markup=markup
            )
            if getattr(msg_live, 'text', None) and any(x in msg_live.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']): return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
            if getattr(msg_live, "text", None) and any(x in msg_live.text.lower() for x in ["/undo", "undo", "uÉ´á´…á´", "â†©ï¸"]):
                return await bot.send_message(user_id, "<b>â€£ Undo: Please restart the Batch Links flow from the menu.</b>", reply_markup=ReplyKeyboardRemove())
            
            raw_live = (msg_live.text or msg_live.caption or "0").strip()
            new_share_job[user_id]['live_threshold'] = int(raw_live) if raw_live.isdigit() else 0

        sj = new_share_job[user_id]
        
        is_tp = sj.get('is_topic')
        sub_str = f"<b>Source Topic ID:</b> {sj.get('topic_id', 'N/A')}\n" if is_tp else f"<b>Msg ID Range:</b> {sj['start_id']} â†’ {sj['end_id']}\n"
        live_str = f"<b>Live Monitor:</b> {sj['live_threshold']} eps per batch\n" if sj['live_threshold'] > 0 else f"<b>Live Monitor:</b> <code>Disabled</code>\n"
        
        target_str = f"<code>{sj['target']}</code>"
        if sj.get('target_topic_id'):
            target_str += f" (Topic: <code>{sj['target_topic_id']}</code>)"

        markup_conf = ReplyKeyboardMarkup([["Gá´‡É´á´‡Ê€á´€á´›á´‡ & Pá´sá´› LÉªÉ´á´‹s"], ["â€£  Cancel"]], resize_keyboard=True, one_time_keyboard=True)
        conf_msg = await _ask(bot, user_id,
            f"<b>Â»  CONFIRM SHARE BATCH</b>\n\n"
            f"<b>Story Name:</b> {sj['story']}\n"
            f"<b>Status:</b> {'Completed' if sj.get('is_completed') else 'Ongoing'}\n"
            f"<b>Source:</b> <code>{sj['source']}</code> ({'Topic' if is_tp else 'Channel'})\n"
            f"<b>Target:</b> {target_str}\n"
            f"{sub_str}"
            f"<b>Episodes/Button:</b> {sj['batch_size']}\n"
            f"<b>Buttons/Post:</b> {sj['buttons_per_post']}\n"
            f"{live_str}"
            f"\n<i>Â»  Smart Parse active: Auto-groups duplicate eps smoothly.</i>",
            reply_markup=markup_conf
        )
        
        if not conf_msg.text or (getattr(conf_msg, 'text', None) and any(x in conf_msg.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”'])) or "Cancel" in conf_msg.text:
            new_share_job.pop(user_id, None)
            return await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
            
        if "Generate" in conf_msg.text or "Gá´‡É´á´‡Ê€á´€á´›á´‡" in conf_msg.text:
            await _build_share_links(bot, user_id, sj, conf_msg)
            
    except Exception as e:
        await bot.send_message(user_id, f"<b>Error during link setup:</b> {e}", reply_markup=ReplyKeyboardRemove())
    
@Client.on_callback_query(filters.regex(r'^sl#'))
async def sl_callback(bot, query):
    from plugins.owner_utils import is_feature_enabled, is_any_owner, FEATURE_LABELS
    user_id = query.from_user.id
    if not await is_any_owner(user_id) and not await is_feature_enabled("batch_links"):
        return await query.answer(f"ðŸ”’ {FEATURE_LABELS['batch_links']} is temporarily disabled by admin.", show_alert=True)
    data = query.data.split('#')
    cmd = data[1]

    if cmd == "start":
        kb = [
            [InlineKeyboardButton("ðŸ“¦ Cá´á´á´˜ÊŸá´‡á´›á´‡ Má´á´…á´‡ (OÉ´á´‡-TÉªá´á´‡)", callback_data="sl#complete")],
            [InlineKeyboardButton("ðŸ“¡ LÉªá´ á´‡ Aá´œá´›á´-Bá´€á´›á´„Êœ (OÉ´É¢á´ÉªÉ´É¢)", callback_data="lb#main")],
            [InlineKeyboardButton("âœ–ï¸ DÉªsá´Éªss", callback_data="start_cmd")]
        ]
        await query.message.edit_text(
            "<b><u>Bá´€á´›á´„Êœ LÉªÉ´á´‹s SÊsá´›á´‡á´</u></b>\n\nChoose your link generation mode:\n\n"
            "â€¢ <b>Cá´á´á´˜ÊŸá´‡á´›á´‡ Má´á´…á´‡:</b> Manually select a range to immediately generate Batch Buttons for existing files.\n"
            "â€¢ <b>OÉ´É¢á´ÉªÉ´É¢ LÉªá´ á´‡ Bá´€á´›á´„Êœ:</b> Runs infinitely in the background, bundling and posting new messages as they stream in.",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif cmd == "complete":
        await query.message.delete()
        asyncio.create_task(_create_share_flow(bot, user_id))

    elif cmd == "scan":
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass
        from plugins.db_scanner import _scan_flow
        asyncio.create_task(_scan_flow(bot, user_id))

async def _build_share_links(bot, user_id, sj, info_msg):
    sts = await info_msg.reply_text("<i>Â»  Initializing share worker...</i>", reply_markup=ReplyKeyboardRemove())

    async def safe_edit(text):
        try:
            await sts.edit_text(text)
        except Exception:
            try:
                await bot.send_message(user_id, text)
            except Exception:
                pass

    try:
        import plugins.share_bot as share_mod
        
        selected_bot_id = sj['bot_id']
        poster = share_mod.share_clients.get(selected_bot_id)
        
        if not poster or not getattr(poster, 'is_initialized', None):
            try:
                await share_mod.start_share_bot()  # reload bots if missing
                poster = share_mod.share_clients.get(selected_bot_id)
            except Exception:
                pass

        if not poster or not getattr(poster, 'is_initialized', None):
            return await safe_edit("â€£  Share Bot failed to start or connect. Check settings.")

        bot_usr = poster.me.username

        if sj.get("account_id"):
            await safe_edit("<i>Â»  Starting scanning client...</i>")
            try:
                from plugins.test import CLIENT, start_clone_bot
                acc = await db.get_bot(user_id, sj.get("account_id"))
                if not acc:
                    return await safe_edit("â€£  Scanning Account not found.")
                scanner_client = await start_clone_bot(CLIENT().client(acc))
                # Pre-fetch cache dialogs
                try:
                    await scanner_client.get_chat(sj['source'])
                except:
                    pass
            except Exception as e:
                return await safe_edit(f"â€£  Failed to start scanning account: {e}")
        else:
            scanner_client = bot

        await safe_edit("<i>Â»  Scanning database channel and generating links...</i>")

        # ===== DEFINITIVE CHANNEL_INVALID FIX =====
        # The Share Bot uses in_memory=True; it has ZERO peer cache after every restart.
        # SOLUTION: Use the MAIN BOT (which has a persistent SQLite session + is admin)
        # to resolve the InputPeerChannel, then invoke channels.GetMessages on the raw layer
        # of the SCANNING CLIENT directly â€” we never ask the Share Bot (worker) to touch the DB channel.
        # The Share Bot is only used for POSTING to the public target channel and for
        # DELIVERING files to users (it IS admin there by the user's configuration).
        from pyrogram.raw.functions.channels import GetMessages as ChannelGetMessages
        from pyrogram.raw.types import InputMessageID, InputPeerChannel

        source_chat_id = sj['source']

        # Step 1: Resolve the database channel peer using the SCANNING CLIENT (always works)
        try:
            db_peer = await scanner_client.resolve_peer(source_chat_id)
        except Exception as e:
            return await safe_edit(
                f"<b>â€£  Cannot Access Database Channel</b>\n\n"
                f"<code>{e}</code>\n\n"
                f"The scanning account must be a member or admin in the hidden database channel."
            )

        # Inject TARGET CHANNEL peer into poster so userbots don't get CHANNEL_INVALID
        target_chat_id = sj['target']
        try:
            from pyrogram.raw.types import InputPeerChannel as _IPC
            _tpeer = await bot.resolve_peer(target_chat_id)
            if isinstance(_tpeer, _IPC):
                await poster.storage.update_peers([(_tpeer.channel_id, _tpeer.access_hash, 'channel', None, None)])
        except Exception:
            pass  # non-fatal

        # Save db channel access_hash for delivery-time peer injection in the Share Bot
        db_access_hash   = db_peer.access_hash if hasattr(db_peer, 'access_hash') else 0
        protect          = await db.get_share_protect_global()
        buttons_per_post = sj.get('buttons_per_post', 10)


        source_chat_id = sj['source']
        current_id     = sj['start_id']
        end_ep         = sj['end_id']
        batch_size     = sj['batch_size']
        story          = sj['story']
        SCAN_CHUNK     = 100  # Telegram allows up to 100 IDs per GetMessages call

        import re as _re
        all_valid_msgs = []
        total_scanned = 0

        from plugins.utils import format_tg_error

        if sj.get('is_topic'):
            await safe_edit(f"<i>Â»  Scanning entire Group Topic {sj['topic_id']}...</i>")
            while True:
                try:
                    # Iterate all messages inside the topic
                    async for m in scanner_client.get_discussion_replies(sj['source'], sj['topic_id']):
                        if m and not m.empty:
                            all_valid_msgs.append(m)
                        total_scanned += 1
                        if total_scanned % 100 == 0:
                            try: await safe_edit(f"<i>Â»  Scanned {total_scanned} files from topic...</i>")
                            except: pass
                    # get_discussion_replies yields newest to oldest by default, so reverse it
                    all_valid_msgs.reverse()
                    break
                except Exception as e:
                    err_msg = format_tg_error(e, "Topic Scan Error")
                    await safe_edit(f"{err_msg}\n\n<i>Waiting for your response...</i>")
                    try:
                        ask_res = await _ask(bot, user_id,
                            f"{err_msg}\n\n<i>Fix the issue (e.g. ensure bot is Admin), then send ðŸ”„ Retry:</i>",
                            reply_markup=ReplyKeyboardMarkup([["ðŸ”„ Retry Scan"], ["âŒ Cancel Process"]], resize_keyboard=True),
                            timeout=600)
                        if not ask_res or not ask_res.text or any(x in ask_res.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']):
                            await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
                            return
                        try: await ask_res.delete()
                        except: pass
                    except asyncio.TimeoutError:
                        return await safe_edit("<b>â€£ Scan Error:</b> Timed out waiting for retry.")

        else:
            await safe_edit(f"<i>Â»  Scanning and analyzing files {current_id}â€“{end_ep}...</i>")
            while current_id <= end_ep:
                chunk_end = min(current_id + SCAN_CHUNK - 1, end_ep)
                msg_ids   = list(range(current_id, chunk_end + 1))

                while True:
                    try:
                        msgs = await scanner_client.get_messages(sj['source'], msg_ids)
                        if not isinstance(msgs, list): msgs = [msgs]
                        
                        for m in msgs:
                            if m and not m.empty:
                                all_valid_msgs.append(m)
                        break
                    except Exception as e:
                        err_str = str(e)
                        if "FLOOD_WAIT" in err_str or "420" in err_str:
                            mw = _re.search(r'wait of (\d+)', err_str)
                            wait_secs = (int(mw.group(1)) + 2) if mw else 15
                            await safe_edit(f"<i>Â»  Flood Wait {wait_secs}s... (scanned {total_scanned})</i>")
                            await asyncio.sleep(wait_secs)
                            continue
                        
                        err_msg = format_tg_error(e, "Scan Error")
                        await safe_edit(f"{err_msg}\n\n<i>Waiting for your response...</i>")
                        try:
                            ask_res = await _ask(bot, user_id,
                                f"{err_msg}\n\n<i>Fix the issue (e.g. ensure bot/clone is Admin), then send ðŸ”„ Retry:</i>",
                                reply_markup=ReplyKeyboardMarkup([["ðŸ”„ Retry Scan"], ["âŒ Cancel Process"]], resize_keyboard=True),
                                timeout=600)
                            if not ask_res or not ask_res.text or any(x in ask_res.text.lower() for x in ['cancel', 'cá´€É´á´„á´‡ÊŸ', 'â›”']):
                                await bot.send_message(user_id, "<i>Process Cancelled Successfully!</i>", reply_markup=ReplyKeyboardRemove())
                                return
                            try: await ask_res.delete()
                            except: pass
                            continue  # retry the scan!
                        except asyncio.TimeoutError:
                            return await safe_edit("<b>â€£ Scan Error:</b> Timed out waiting for retry.")
                
                total_scanned += len(msg_ids)
                current_id = chunk_end + 1
                await asyncio.sleep(0.3)

        if not all_valid_msgs:
            return await safe_edit("â€£  No files found in that range.")

        all_valid_msgs.sort(key=lambda x: x.id)  # chronological

        #  Episode extraction helpers (Priority: file_name > caption > audio_title) 

        _NOISE_RE = [
            # Resolutions MUST have p/i suffix
            _re.compile(r'(?i)\b(?:360|480|720|1080|2160|4k)[pi]\b'),
            # Codec/format labels
            _re.compile(r'(?i)\b(?:x264|x265|h\.?264|h\.?265|hevc|avc|aac|mp[34]|m4a|m4v|m4b|mkv|avi|mov|wmv|flv|flac|opus|ogg|wav|webm|3gp|mts|m2ts)\b'),
            # Filename Date/Time Encampments (Blocks auto-generated device timestamps from being seen as Ep 2025)
            _re.compile(r'(?i)(?:record|screenrecorder|vid|aud|voice|audio|img|pic|screenshot)[-_.0-9a-zA-Z]*\d{4}[-_.0-9]*'),
            _re.compile(r'(?i)\b20\d{2}[-_. ]?\d{2}[-_. ]?\d{2}[-_.0-9]*'),
            # File sizes
            _re.compile(r'(?i)\b\d+(?:\.\d+)?\s*(?:mb|gb|kb)\b'),
            # Track/season-episode labels like S01E05
            _re.compile(r'(?i)\b(?:s[0-9]{1,2}e[0-9]{1,2})(?=\s|$)'),
            # Common text noise
            _re.compile(r'(?i)\b(?:copy|final|v\d+|new|latest|audio|track)\b'),
        ]

        def _clean(text: str) -> str:
            for rx in _NOISE_RE:
                text = rx.sub(' ', text)
            # Normalize common delimiters to spaces to break words apart
            text = _re.sub(r'[_#\.]', ' ', text)
            return text
            
        def _extract_range_from_text(text: str):
            """
            Ultra-robust episode extraction combining both range detection
            and smart fallback logic for single episodes.
            """
            c = text
            # Step 1: Strip file extension (e.g. .mp3, .m4a, .mp4, .txt)
            dot = c.rfind('.')
            if dot > 0 and (len(c) - dot) <= 5: c = c[:dot]
            
            # Step 2: Strip TRAILING copy-counter suffixes like " (1)", " (2)", " (1)" BEFORE any number extraction
            # These are NOT episode numbers â€” they are duplicate markers added by Telegram/OS
            import re as _re
            c = _re.sub(r'\s*\(\d+\)\s*$', '', c).strip()
            
            # 1. Comma / Space sequence of numbers (e.g. 1 2 3 4 5)
            s = _re.search(r'(?<!\d)(\d{1,4}(?:(?:,\s*|\s+)\d{1,4}){2,})(?!\d)', c)
            if s:
                nums = [int(x) for x in _re.findall(r'\d+', s.group(1))]
                if max(nums) < 5000: return (min(nums), max(nums), True)

            # 2. Explicit range with '-', 'to' etc
            r = _re.search(r'(?<!\d)(\d{1,4}(?:(?:\s*[-\u2013\u2014]|(?i:\s+to\s+))\s*\d{1,4})+)(?!\d)', c)
            if r:
                nums = [int(x) for x in _re.findall(r'\d+', r.group(1))]
                if max(nums) < 5000 and len(nums) >= 2 and nums == sorted(nums) and len(set(nums)) == len(nums):
                    if (nums[-1] - nums[0]) < 1000:
                        return (min(nums), max(nums), True)

            # 3. Strategy zero-padded prefix "000047" or "047" with no letters before
            m = _re.match(r'^0*(\d{1,4})(?:[^0-9]|$)', c)
            if m:
                n = int(m.group(1))
                if 0 < n < 5000: return (n, n, False)

            # 4. Explicit keywords: "Ep 23", "Episode 23", "Part 23", "Ch 2", hindi
            kw = _re.search(r'(?i)(?:episode|epi|ep|chapter|ch|part|e|à¤à¤ªà¤¿à¤¸à¥‹à¤¡|à¤­à¤¾à¤—)[\s\-\:\.\#\_]*(\d{1,4})(?!\d)', c)
            if kw:
                n = int(kw.group(1))
                if 0 < n < 5000: return (n, n, False)

            # 5. Last number fallback: separate letters/numbers, strip years
            c2 = _re.sub(r'([a-zA-Z])(\d)', r'\1 \2', c)
            c2 = _re.sub(r'(\d)([a-zA-Z])', r'\1 \2', c2)
            c2 = _re.sub(r'(?i)\b19\d{2}\b|\b20\d{2}\b', ' ', c2) # strip lone years
            
            nums = [int(x) for x in _re.findall(r'(?<!\d)(\d{1,4})(?!\d)', c2) if 0 < int(x) < 5000]
            if nums:
                return (nums[-1], nums[-1], False)
                
            return None

        def _get_file_names(msg):
            """Collect file_name strings (without extension) from all media attributes."""
            import os as _os
            names = []
            for attr in ("audio", "voice", "document", "video"):
                media = getattr(msg, attr, None)
                if media:
                    fname = getattr(media, "file_name", None)
                    if fname:
                        # Strip extension to prevent '4' in '.m4a' / '3' in '.mp3' etc.
                        # from contaminating the number pool
                        base, _ = _os.path.splitext(str(fname))
                        names.append(base)
            return names

        def _get_audio_title(msg):
            """Get audio title tag (can contain misleading track numbers)."""
            for attr in ("audio", "voice"):
                media = getattr(msg, attr, None)
                if media:
                    t = getattr(media, "title", None)
                    if t: return str(t)
            return ""

        def extract_ep_individual(msg):
            """
            Deep episode extraction â€” individual mode.
            Now correctly uses range parsing so range files span multiple episodes.
            """
            # Priority 1: file_name only (most reliable)
            for fname in _get_file_names(msg):
                r = _extract_range_from_text(fname)
                if r: return r

            # Priority 2: caption text
            cap = msg.caption or msg.text or ""
            if cap.strip():
                r = _extract_range_from_text(cap)
                if r: return r

            # Priority 3: audio title (last resort â€” often has track numbers)
            t = _get_audio_title(msg)
            if t:
                r = _extract_range_from_text(t)
                if r: return r

            return (-1, -1, False)

        def extract_ep_grouped(msg):
            """
            Deep episode extraction â€” grouped mode.
            Tries to find a range (startâ€“end). Falls back to individual.
            """
            # Priority 1: file_name (range aware)
            for fname in _get_file_names(msg):
                r = _extract_range_from_text(fname)
                if r: return r

            # Priority 2: caption
            cap = msg.caption or msg.text or ""
            if cap.strip():
                r = _extract_range_from_text(cap)
                if r: return r

            # Priority 3: audio title
            t = _get_audio_title(msg)
            if t:
                r = _extract_range_from_text(t)
                if r: return r

            return (-1, -1, False)

        # â•â• TWO-PASS PARSING â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
        # PASS 1: Parse all msgs as individual to detect MODE
        pass1 = []
        unparseable_count = 0
        for msg in all_valid_msgs:
            ep_s, ep_e, is_r = extract_ep_individual(msg)
            if ep_s < 1:
                unparseable_count += 1
                continue
            pass1.append((msg, ep_s, ep_e, is_r))

        if not pass1:
            return await safe_edit("â€£  Could not extract any episode numbers from the scanned messages.")

        #  DETECT MODE 
        # Check if a significant fraction of files look like ranges ("57-79")
        range_hint_count = 0
        for msg in all_valid_msgs:
            cap = msg.caption or msg.text or ""
            if _re.search(r'(?<!\d)\d{1,4}\s*[-â€“â€”]\s*\d{1,4}(?!\d)', cap):
                range_hint_count += 1
        GROUPED_MODE = range_hint_count > (len(all_valid_msgs) * 0.50)

        # PASS 2: Re-parse with the correct extractor based on mode
        parsed_msgs = []
        unparseable_count = 0
        extractor = extract_ep_grouped if GROUPED_MODE else extract_ep_individual
        for msg in all_valid_msgs:
            ep_s, ep_e, is_r = extractor(msg)
            if ep_s < 1:
                unparseable_count += 1
                continue
            parsed_msgs.append((msg, ep_s, ep_e, is_r))

        if not parsed_msgs:
            return await safe_edit("â€£  Could not extract any episode numbers from the scanned messages.")

        total_count = len(all_valid_msgs)  # used in final report


        #  Build ep_to_msgs dict and track duplicates 
        ep_to_msgs: dict = {}      # ep_start â†’ [msg_ids]
        duplicate_eps:  list = []  # list of ep numbers with >1 file
        grouped_files:  list = []  # list of "(name, start-end)" for grouped files

        range_msg_ids = set()
        for msg, ep_s, ep_e, is_r in parsed_msgs:
            if is_r:
                range_msg_ids.add(msg.id)
                # Range file â€” track for report
                range_label = f"{ep_s}\u2013{ep_e}"
                grouped_files.append(range_label)
                if GROUPED_MODE:
                    # Grouped mode: 1 button per file, keyed by ep_s
                    if ep_s not in ep_to_msgs:
                        ep_to_msgs[ep_s] = []
                    if msg.id not in ep_to_msgs[ep_s]:
                        ep_to_msgs[ep_s].append(msg.id)
                else:
                    # Individual mode: EXPAND so every ep in range gets this msg_id
                    for expanded_ep in range(ep_s, min(ep_e + 1, ep_s + 500)):
                        if expanded_ep not in ep_to_msgs:
                            ep_to_msgs[expanded_ep] = []
                        if msg.id not in ep_to_msgs[expanded_ep]:
                            ep_to_msgs[expanded_ep].append(msg.id)
            else:
                if ep_s not in ep_to_msgs:
                    ep_to_msgs[ep_s] = []
                if msg.id not in ep_to_msgs[ep_s]:
                    ep_to_msgs[ep_s].append(msg.id)

        # Identify true duplicates (same ep_num, multiple messages)
        # Exclude generated range overlaps to prevent grouped files from appearing as duplicates
        duplicate_eps = []
        for ep, ids in ep_to_msgs.items():
            non_range_ids = [m for m in ids if m not in range_msg_ids]
            if len(non_range_ids) > 1:
                duplicate_eps.append(ep)
        duplicate_eps = sorted(set(duplicate_eps))


        all_ep_nums    = sorted(ep_to_msgs.keys())
        first_ep_num   = all_ep_nums[0] if all_ep_nums else 0
        last_ep_num    = all_ep_nums[-1] if all_ep_nums else 0

        # â”€â”€ Missing episode detection (ACCURATE 3-tier method) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # Tier 1: raw_missing = gaps in the labelled ep range
        # Tier 2: unassigned  = files that physically exist but couldn't be labelled
        # Tier 3: truly_missing = raw_missing minus unassigned (these truly don't exist)
        # We must NOT count unparseable files as missing â€” they are already embedded.
        missing_eps: list = []
        truly_missing_count: int = 0
        unassigned_count: int = 0

        if not GROUPED_MODE and all_ep_nums:
            expected_range = set(range(first_ep_num, last_ep_num + 1))
            present_set    = set(all_ep_nums)
            raw_missing    = sorted(expected_range - present_set)

            # Count files that physically exist but couldn't get an episode label
            added_msg_ids_pre = set()
            for mids in ep_to_msgs.values():
                added_msg_ids_pre.update(mids)
            unassigned_count = len([m for m in all_valid_msgs if m.id not in added_msg_ids_pre])

            # True gaps = raw gaps not covered by unassigned files
            truly_missing_count = max(0, len(raw_missing) - unassigned_count)
            # Only list episode numbers as missing if they exceed our unassigned buffer
            if truly_missing_count > 0:
                missing_eps = raw_missing[unassigned_count:]  # first N gaps are filled by unassigned files

        #  BUILD BUCKETS 
        # GROUPED_MODE: each file = 1 button using its own range label
        # INDIVIDUAL_MODE: bucket by batch_size
        buckets = []  # list of (label_start, label_end, [msg_ids])

        if GROUPED_MODE:
            parsed_ids = {m.id for m, _, _, _ in parsed_msgs}
            current_bucket_mids = None
            
            for m in sorted(all_valid_msgs, key=lambda x: x.id):
                if m.id in parsed_ids:
                    p_tuple = next(pt for pt in parsed_msgs if pt[0].id == m.id)
                    ep_s, ep_e = p_tuple[1], p_tuple[2]
                    
                    mids = ep_to_msgs.get(ep_s, [])
                    if mids and mids[0] == m.id:
                        current_bucket_mids = [m.id]
                        buckets.append([ep_s, ep_e, current_bucket_mids])
                    elif current_bucket_mids is not None:
                        current_bucket_mids.append(m.id)
                else:
                    if current_bucket_mids is None:
                        current_bucket_mids = [m.id]
                        buckets.append(["Extra", "Files", current_bucket_mids])
                    else:
                        current_bucket_mids.append(m.id)
        else:
            # Individual mode: dynamic-size buckets using chronological traversal
            msg_to_ep = {m.id: ep for m, ep, _, _ in parsed_msgs}
            msg_to_end = {m.id: ep_e for m, _, ep_e, _ in parsed_msgs}
            
            b_s = None
            b_e = None
            b_mids = []
            pending_unparsed = []

            all_msgs_sorted = sorted(all_valid_msgs, key=lambda x: x.id)

            for m in all_msgs_sorted:
                mid = m.id
                if mid in msg_to_ep:
                    ep = msg_to_ep[mid]
                    math_start = ((ep - 1) // batch_size) * batch_size + 1
                    math_end   = math_start + batch_size - 1
                    
                    if b_s is None:
                        b_s = math_start
                        b_e = math_end
                    elif ep > b_e:
                        if b_mids:
                            buckets.append([b_s, b_e, b_mids])
                        b_s = math_start
                        b_e = math_end
                        b_mids = []

                    # Flush any unparsed messages before this bucket started into this bucket
                    if pending_unparsed:
                        for umid in pending_unparsed:
                            if umid not in b_mids: b_mids.append(umid)
                        pending_unparsed = []

                    if mid not in b_mids:
                        b_mids.append(mid)
                        
                    span_e = msg_to_end.get(mid, ep)
                    if span_e > b_e:
                        b_e = span_e
                else:
                    # Unparseable message -> Embed it natively!
                    if b_s is None:
                        pending_unparsed.append(mid)
                    else:
                        if len(b_mids) >= batch_size:
                            buckets.append([b_s, b_e, b_mids])
                            b_s = b_e + 1
                            b_e = b_s + batch_size - 1
                            b_mids = []
                        if mid not in b_mids:
                            b_mids.append(mid)

            if b_s is not None and b_mids:
                buckets.append([b_s, b_e, b_mids])
            elif pending_unparsed:
                buckets.append(["Extra", "Files", pending_unparsed])
                pending_unparsed = []

            # Cap ONLY last bucket label at actual last ep (cosmetic only, if it has numeric ends)
            if buckets and buckets[-1][0] != "Extra" and last_ep_num:
                last_b = buckets[-1]
                buckets[-1] = (last_b[0], min(last_b[1], last_ep_num), last_b[2])

        #  PRE-SCAN INTERVENTION 
        prescan_report_lines = [
            f"<b>ðŸ” PRE-SCAN DIAGNOSIS</b>",
            f"\n<blockquote expandable>",
            f"Â»  <b>Files located:</b> {total_count}",
            f"ðŸŽ¯ <b>Detected Bounds:</b> {first_ep_num}â€“{last_ep_num}",
        ]
        
        if not GROUPED_MODE:
            if unassigned_count > 0:
                prescan_report_lines.append(f"ðŸ“Ž <b>{unassigned_count} files lack episode labels</b> (Will embed silently inside buttons)")
            if truly_missing_count > 0:
                miss_preview = ", ".join(str(e) for e in missing_eps[:15])
                if len(missing_eps) > 15: miss_preview += f" (+{len(missing_eps)-15} more)"
                prescan_report_lines.append(f"âŒ <b>{truly_missing_count} Truly Missing Episodes:</b> {miss_preview}")
            elif unassigned_count == 0:
                prescan_report_lines.append(f"âœ… <b>Zero Missing Episodes!</b> All slots correctly found.")
        elif duplicate_eps:
             prescan_report_lines.append(f"â€£  <b>Duplicates Detected:</b> {len(duplicate_eps)}")
             
        prescan_report_lines.append(f"</blockquote>")
        prescan_report_lines.append(f"\n<i>Do you want to proceed and generate links for these files?</i>")
        
        try:
            prescan_msg = await _ask(bot, user_id, "\n".join(prescan_report_lines), reply_markup=ReplyKeyboardMarkup([
                ["âœ… Proceed & Generate"],
                ["âŒ Cancel Job"]
            ], resize_keyboard=True, one_time_keyboard=True), timeout=1800)
            
            if getattr(prescan_msg, 'text', None) and "Cancel" in prescan_msg.text:
                await bot.send_message(user_id, "<b>âŒ Process Cancelled during Pre-Scan.</b>", reply_markup=ReplyKeyboardRemove())
                return await safe_edit("<b>âŒ Process Cancelled during Pre-Scan.</b>")
                
            await bot.send_message(user_id, "<i>Â»  Pre-Scan Accepted. Generating unique secure links...</i>", reply_markup=ReplyKeyboardRemove())
            await safe_edit("<i>Â»  Pre-Scan Accepted. Generating unique secure links...</i>")
        except Exception:
            await bot.send_message(user_id, "<b>â³ Pre-Scan Timed Out (30 mins). Job Cancelled.</b>", reply_markup=ReplyKeyboardRemove())
            return await safe_edit("<b>â³ Pre-Scan Timed Out (30 mins). Job Cancelled.</b>")

        raw_buttons = []
        for b_s, b_e, mids in buckets:
            if not mids:
                continue
            uuid_str = str(uuid.uuid4()).replace('-', '')[:16]
            await db.save_share_link(
                uuid_str, mids, source_chat_id,
                protect=protect, access_hash=db_access_hash
            )
            url = f"https://t.me/{bot_usr}?start={uuid_str}"
            
            # --- APPLY SHORTENER ---
            short_choice = sj.get('shortener')
            if short_choice:
                apis = await db.get_shortener_apis()
                api_key = apis.get("arolinks") if short_choice == "AroLinks" else apis.get("urlshortx")
                if api_key:
                    try:
                        import aiohttp
                        async with aiohttp.ClientSession() as session:
                            api_url = f"https://{short_choice.lower()}.com/api?api={api_key}&url={url}"
                            async with session.get(api_url, timeout=10) as resp:
                                data = await resp.json()
                                if data.get("status") == "success" and "shortenedUrl" in data:
                                    url = data["shortenedUrl"]
                    except Exception as e:
                        pass
                        
            btn_text = str(b_s) if (b_s == b_e or batch_size == 1) else f"{b_s}â€“{b_e}"
            font_style = sj.get('font', 'Default')
            if font_style != "Default":
                if font_style in ["𝑅𝑒𝑔𝑢𝑙𝑢𝑠", "𝑨𝒍𝒕𝒂𝒊𝒓", "𝐋𝐔𝐃"]:
                    prefix = to_custom_font(sj['story'], font_style)
                else:
                    prefix = font_style
                btn_text = f"{prefix} {btn_text}"
                
            raw_buttons.append({
                "btn":      InlineKeyboardButton(_sc(btn_text), url=url),
                "ep_start": b_s,
                "ep_end":   b_e,
            })

        # Calculate unparseable count for the display report (removed per user request)
        added_msg_ids = set()
        for mids in ep_to_msgs.values():
            added_msg_ids.update(mids)
        unparseable_msgs_list = [m.id for m in all_valid_msgs if m.id not in added_msg_ids]


        #  PHASE 3: Post to target channel 
        post_count = 0
        for i in range(0, len(raw_buttons), buttons_per_post):
            chunk = raw_buttons[i : i + buttons_per_post]
            first_ep = chunk[0]["ep_start"]
            last_ep  = chunk[-1]["ep_end"]
            def _bold_sans(s):
                res = ''
                for c in str(s):
                    if 'A' <= c <= 'Z':
                        res += chr(0x1D5D4 + ord(c) - ord('A'))
                    elif 'a' <= c <= 'z':
                        res += chr(0x1D5D4 + ord(c) - ord('a'))
                    else:
                        res += c
                return res
            
            txt = f"{_bold_sans(story)} ð—˜ð—£ð—¦ {first_ep} - {last_ep}"

            keyboard = []
            for j in range(0, len(chunk), 2):
                row = [c["btn"] for c in chunk[j:j + 2]]
                keyboard.append(row)
            keyboard.append([
                InlineKeyboardButton(_sc("tutorial"), url="https://t.me/StoriesLinkopningguide"),
                InlineKeyboardButton(_sc("support"), url="https://t.me/+KPVtaAm9k-RmMjdl")
            ])
            for attempt in range(6):
                try:
                    await poster.send_message(
                        chat_id=sj['target'], text=txt,
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        reply_to_message_id=sj.get('target_topic_id')
                    )
                    break
                except Exception as e:
                    err_str = str(e)
                    import re as _re2
                    if "FLOOD_WAIT" in err_str or "420" in err_str:
                        mw = _re2.search(r'wait of (\d+)', err_str)
                        wait_secs = (int(mw.group(1)) + 2) if mw else 35
                        await safe_edit(f"<i>Â»  Rate limit... waiting {wait_secs}s</i>")
                        await asyncio.sleep(wait_secs)
                        continue
                    else:
                        return await safe_edit(
                            f"<b>â€£  Failed to post to target channel:</b> <code>{e}</code>\n\n"
                            f"<i>Make sure the selected account is an admin in the target channel.</i>"
                        )
            else:
                return await safe_edit("â€£  Posting aborted after 6 retries due to FloodWait.")
            post_count += 1
            await asyncio.sleep(1)

        #  FINAL REPORT 
        mode_str = "ðŸ—‚ Grouped files (1 button/file)" if GROUPED_MODE else f"ðŸ“‘ Individual (batch size: {batch_size})"

        report_lines = [
            f"<b>Â»  Share Links Generated!</b>",
            f"\n<blockquote expandable>",
            f"Â»  <b>Files processed:</b> {total_count}",
            f"ðŸŽ¯ <b>Episode range:</b> {first_ep_num}â€“{last_ep_num}",
            f"Â»  <b>Link buttons created:</b> {len(raw_buttons)}",
            f"Â»  <b>Posts sent to channel:</b> {post_count}",
            f"Â»  <b>Mode:</b> {mode_str}",
        ]

        if grouped_files:
            gf_preview = ", ".join(grouped_files[:8])
            if len(grouped_files) > 8:
                gf_preview += f" (+{len(grouped_files)-8} more)"
            report_lines.append(f"ðŸ—‚ <b>Grouped files ({len(grouped_files)}):</b> {gf_preview}")

        if duplicate_eps:
            dup_preview = ", ".join(str(e) for e in duplicate_eps[:10])
            if len(duplicate_eps) > 10:
                dup_preview += f" (+{len(duplicate_eps)-10} more)"
            report_lines.append(f"â€£  <b>Duplicates detected ({len(duplicate_eps)}) â€” all files kept:</b> {dup_preview}")

        if not GROUPED_MODE:
            if unassigned_count > 0:
                report_lines.append(
                    f"ðŸ“Ž <b>Files with no episode label ({unassigned_count}):</b> "
                    f"<i>exist in DB but filename had no episode number â€” embedded chronologically (NOT missing)</i>"
                )
            if truly_missing_count > 0:
                miss_preview = ", ".join(str(e) for e in missing_eps[:15])
                if len(missing_eps) > 15:
                    miss_preview += f" (+{len(missing_eps)-15} more)"
                report_lines.append(
                    f"âŒ <b>Truly missing episodes ({truly_missing_count}) â€” not found in DB:</b> {miss_preview}"
                )
            elif unassigned_count == 0:
                report_lines.append(f"âœ… <b>No missing episodes</b> â€” all {last_ep_num - first_ep_num + 1} slots accounted for!")

        report_lines.append("</blockquote>")
        report_lines.append(f"")
        report_lines.append(f"<i>Users click any button to receive their episodes from @{bot_usr}.</i>")

        await safe_edit("\n".join(report_lines))

        #  SEND DOWNLOADABLE REPORT FILE 
        import io, datetime
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
        plain_report = []
        if unparseable_msgs_list:
            plain_report.append("-" * 60)
            plain_report.append(f"FILES WITH NO EPISODE LABEL: {len(unparseable_msgs_list)}")
            plain_report.append(f"  These files exist in the database but their filename had no")
            plain_report.append(f"  recognizable episode number. They are NOT missing â€” they were")
            plain_report.append(f"  automatically embedded into buttons at their chronological position.")
            plain_report.append("  IDs: " + ", ".join(str(m) for m in unparseable_msgs_list))
            
        plain_report += [
            "=" * 50,
            "  ARYA BOT  â€”  Share Links Generation Report",
            "=" * 50,
            f"Story    : {story.upper()}",
            f"Generated: {now.strftime('%Y-%m-%d %H:%M:%S IST')}",
            f"Bot      : @{bot_usr}",
            "-" * 50,
            f"Files processed      : {total_count}",
            f"Episode range        : {first_ep_num} â€“ {last_ep_num}",
            f"Link buttons created : {len(raw_buttons)}",
            f"Posts sent           : {post_count}",
            f"Mode                 : {'Grouped (1 button/file)' if GROUPED_MODE else f'Individual (batch={batch_size})'}",
        ]
        if grouped_files:
            plain_report.append("-" * 50)
            plain_report.append(f"GROUPED FILES ({len(grouped_files)}):")
            for gf in grouped_files:
                plain_report.append(f"  â€¢ {gf}")
        if duplicate_eps:
            plain_report.append("-" * 60)
            plain_report.append(f"DUPLICATES DETECTED â€” all files kept ({len(duplicate_eps)}):")
            plain_report.append("  " + ", ".join(str(e) for e in duplicate_eps))
        if not GROUPED_MODE:
            if unassigned_count > 0:
                plain_report.append("-" * 60)
                plain_report.append(f"FILES EMBEDDED WITHOUT EPISODE LABEL: {unassigned_count}")
                plain_report.append(f"  These files EXIST in the database but their filenames had")
                plain_report.append(f"  no readable episode number (e.g. auto-generated names).")
                plain_report.append(f"  They are NOT missing â€” they are already delivered inside buttons.")
            if truly_missing_count > 0:
                plain_report.append("-" * 60)
                plain_report.append(f"TRULY MISSING EPISODES (not in DB): {truly_missing_count}")
                plain_report.append("  " + ", ".join(str(e) for e in missing_eps))
            elif unassigned_count == 0:
                plain_report.append("-" * 60)
                plain_report.append("NO MISSING EPISODES â€” all slots accounted for!")

        plain_report += [
            "=" * 60,
            "ACCURACY NOTE:",
            "  'Files embedded without label' = PRESENT in DB, delivered to users.",
            "  'Truly missing' = NOT in DB at all, cannot be delivered.",
            "  Duplicates = multiple files for same ep â€” all included.",
            "-" * 60,
            "Powered by Arya Bot",
            "=" * 60,
        ]
        report_text = "\n".join(plain_report)
        report_bytes = io.BytesIO(report_text.encode('utf-8'))
        report_bytes.name = f"arya_report_{story.replace(' ','_')}.txt"

        import html
        try:
            usr_obj = await bot.get_users(user_id)
            u_name = html.escape(usr_obj.first_name) if usr_obj and usr_obj.first_name else "User"
            poster_me = await poster.get_me()
            p_name = html.escape(poster_me.first_name) if poster_me and poster_me.first_name else "Bot"
            bot_link = f"<a href='https://t.me/{bot_usr}'>{p_name}</a>"
            story_sz = _sc(story)

            if sj.get('is_completed'):
                dm_header  = f"â€ºâ€º {_sc('Hey')} <a href='tg://user?id={user_id}'>{u_name}</a>\n\n"
                ch_header  = f"â€ºâ€º {_sc('Hey Strangers')}\n\n"

                en_body = (
                    _sc("This ") + story_sz + _sc(" is completed by ") + bot_link +
                    _sc(". I've tried to ensure accuracy and provided a final report with details. "
                        "Missing episodes can occur naturallyâ€”nothing can be done. "
                        "If 10+ are missing, contact support. Unparsed files are safely mapped "
                        "inside buttons. Duplicates may appear if the source had identically "
                        "named files. I am not responsible for the content as these files are "
                        "purely forwarded via Arya bot, strictly not scraped.")
                )

                hi_body = (
                    f"à¤¯à¤¹ {story_sz} {bot_link} à¤¦à¥à¤µà¤¾à¤°à¤¾ à¤ªà¥‚à¤°à¥€ à¤•à¥€ à¤—à¤ˆ à¤¹à¥ˆà¥¤ à¤®à¥ˆà¤‚à¤¨à¥‡ à¤¸à¤Ÿà¥€à¤•à¤¤à¤¾ à¤¸à¥à¤¨à¤¿à¤¶à¥à¤šà¤¿à¤¤ à¤•à¤°à¤¨à¥‡ à¤•à¤¾ "
                    "à¤ªà¥à¤°à¤¯à¤¾à¤¸ à¤•à¤¿à¤¯à¤¾ à¤¹à¥ˆ à¤”à¤° à¤…à¤‚à¤¤à¤¿à¤® à¤°à¤¿à¤ªà¥‹à¤°à¥à¤Ÿ à¤¸à¤‚à¤²à¤—à¥à¤¨ à¤¹à¥ˆà¥¤ à¤—à¤¾à¤¯à¤¬ à¤à¤ªà¤¿à¤¸à¥‹à¤¡ à¤¸à¥à¤µà¤¾à¤­à¤¾à¤µà¤¿à¤• à¤¹à¥ˆà¤‚à¥¤ "
                    "à¤…à¤—à¤° 10+ à¤—à¤¾à¤¯à¤¬ à¤¹à¥ˆà¤‚, à¤¤à¥‹ à¤¸à¤ªà¥‹à¤°à¥à¤Ÿ à¤¸à¥‡ à¤¸à¤‚à¤ªà¤°à¥à¤• à¤•à¤°à¥‡à¤‚à¥¤ à¤…à¤¨à¤ªà¤¾à¤°à¥à¤¸ à¤«à¤¼à¤¾à¤‡à¤²à¥‡à¤‚ à¤¸à¥à¤°à¤•à¥à¤·à¤¿à¤¤ à¤°à¥‚à¤ª à¤¸à¥‡ "
                    "à¤¬à¤Ÿà¤¨à¥‹à¤‚ à¤®à¥‡à¤‚ à¤®à¥ˆà¤ª à¤•à¥€ à¤—à¤ˆ à¤¹à¥ˆà¤‚à¥¤ à¤¡à¥à¤ªà¥à¤²à¤¿à¤•à¥‡à¤Ÿ à¤«à¤¼à¤¾à¤‡à¤²à¥‡à¤‚ à¤¸à¥à¤°à¥‹à¤¤ à¤•à¥€ à¤µà¤œà¤¹ à¤¸à¥‡ à¤¹à¥‹ à¤¸à¤•à¤¤à¥€ à¤¹à¥ˆà¤‚à¥¤ à¤®à¥ˆà¤‚ "
                    "à¤¸à¤¾à¤®à¤—à¥à¤°à¥€ à¤•à¥‡ à¤²à¤¿à¤ à¤œà¤¿à¤®à¥à¤®à¥‡à¤¦à¤¾à¤° à¤¨à¤¹à¥€à¤‚ à¤¹à¥‚à¤ à¤•à¥à¤¯à¥‹à¤‚à¤•à¤¿ à¤¯à¥‡ à¤«à¤¼à¤¾à¤‡à¤²à¥‡à¤‚ à¤†à¤°à¥à¤¯à¤¾ à¤¬à¥‰à¤Ÿ à¤•à¥‡ à¤®à¤¾à¤§à¥à¤¯à¤® à¤¸à¥‡ "
                    "à¤…à¤—à¥à¤°à¥‡à¤·à¤¿à¤¤ à¤¹à¥ˆà¤‚, à¤¬à¤¿à¤²à¥à¤•à¥à¤² à¤¸à¥à¤•à¥à¤°à¥ˆà¤ª à¤¨à¤¹à¥€à¤‚ à¤•à¥€ à¤—à¤ˆ à¤¹à¥ˆà¤‚à¥¤"
                )

                dm_cap = (
                    f"<blockquote expandable>{dm_header}{en_body}</blockquote>\n\n<blockquote expandable>{hi_body}\n\n"
                    "<i>Note: If some existing files were wrongly marked as missing, you can use /deepscanbatch with this report to auto-correct them!</i></blockquote>"
                )
                ch_cap = (
                    f"<blockquote expandable>{ch_header}{en_body}</blockquote>\n\n<blockquote expandable>{hi_body}</blockquote>"
                )

            else:
                dm_header  = f"â€ºâ€º {_sc('Hey')} <a href='tg://user?id={user_id}'>{u_name}</a>\n\n"
                ch_header  = f"â€ºâ€º {_sc('Hey Strangers')}\n\n"
                
                en_body = _sc("All currently available files have been posted here. "
                           "New episodes will be added as they arrive. Enjoy and stay tuned!")
                           
                hi_body = ("à¤µà¤°à¥à¤¤à¤®à¤¾à¤¨ à¤®à¥‡à¤‚ à¤‰à¤ªà¤²à¤¬à¥à¤§ à¤¸à¤­à¥€ à¤«à¤¼à¤¾à¤‡à¤²à¥‡à¤‚ à¤¯à¤¹à¤¾à¤ à¤ªà¥‹à¤¸à¥à¤Ÿ à¤•à¤° à¤¦à¥€ à¤—à¤ˆ à¤¹à¥ˆà¤‚à¥¤ "
                           "à¤œà¥ˆà¤¸à¥‡ à¤¹à¥€ à¤¨à¤ à¤à¤ªà¤¿à¤¸à¥‹à¤¡ à¤†à¤à¤‚à¤—à¥‡, à¤‰à¤¨à¥à¤¹à¥‡à¤‚ à¤œà¥‹à¤¡à¤¼ à¤¦à¤¿à¤¯à¤¾ à¤œà¤¾à¤à¤—à¤¾à¥¤ à¤†à¤¨à¤‚à¤¦ à¤²à¥‡à¤‚ à¤”à¤° à¤œà¥à¤¡à¤¼à¥‡ à¤°à¤¹à¥‡à¤‚!")

                dm_cap = f"<blockquote expandable>{dm_header}{en_body}</blockquote>\n\n<blockquote expandable>{hi_body}\n\n<i>Note: If some existing files were wrongly marked as missing, you can use /deepscanbatch with this report to auto-correct them!</i></blockquote>"
                ch_cap = f"<blockquote expandable>{ch_header}{en_body}</blockquote>\n\n<blockquote expandable>{hi_body}</blockquote>"

            # Send to admin DM â€” independent of channel
            try:
                await bot.send_document(
                    user_id, report_bytes,
                    caption=dm_cap, parse_mode=__import__("pyrogram.enums", fromlist=["ParseMode"]).ParseMode.HTML,
                    file_name=report_bytes.name
                )
            except Exception as dm_err:
                logger.error(f"[Report] DM send failed: {dm_err}", exc_info=True)

            # Send to target channel â€” always attempted independently
            try:
                report_bytes.seek(0)
                await poster.send_document(
                    sj['target'], report_bytes,
                    caption=ch_cap, parse_mode=__import__("pyrogram.enums", fromlist=["ParseMode"]).ParseMode.HTML,
                    file_name=report_bytes.name,
                    reply_to_message_id=sj.get('target_topic_id')
                )
            except Exception as ch_err:
                logger.error(f"[Report] Channel send failed: {ch_err}", exc_info=True)

        except Exception as rep_err:
            logger.error(f"[Report] Could not prepare report: {rep_err}", exc_info=True)

        if sj.get('live_threshold', 0) > 0:
            try:
                from plugins.live_batch import _lb_save_job, _lb_paused, _lb_tasks, _lb_run_job
                job_id = str(uuid.uuid4())
                ljob = {
                    "job_id": job_id, "user_id": user_id, "status": "running",
                    "share_bot_id": selected_bot_id,
                    "account_id": sj.get('account_id', 'bot'),
                    "source": sj['source'],
                    "target": sj['target'],
                    "target_topic_id": sj.get('target_topic_id'),
                    "story": sj['story'],
                    "threshold": sj['live_threshold'],
                    "protect": True,
                    "last_seen_id": sj.get('end_id'),
                    "buffer_mids": [],
                    "forwarded": 0
                }
                await _lb_save_job(ljob)
                _lb_paused[job_id] = asyncio.Event()
                _lb_paused[job_id].set()
                _lb_tasks[job_id] = asyncio.create_task(_lb_run_job(job_id))
                await bot.send_message(user_id, f"<b>âœ… Live Batch Monitoring automatically activated for {sj['story']}!</b>\nMonitoring for new files arriving after Msg ID <code>{sj.get('end_id')}</code>.")
            except Exception as lb_err:
                logger.error(f"Live Batch Kickoff error: {lb_err}", exc_info=True)

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"Share link generation error:\n{tb}")
        retry_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("ðŸ” Rá´‡á´›Ê€Ê", callback_data="sl#complete"),
            InlineKeyboardButton("âœ–ï¸ DÉªsá´Éªss", callback_data="start_cmd")
        ]])
        err_txt = (
            f"<b>âŒ Error during link generation:</b>\n<code>{e}</code>\n\n"
            f"<i>Click Retry to start a new job, or Dismiss to cancel.</i>"
        )
        try:
            await sts.edit_text(err_txt, reply_markup=retry_kb)
        except Exception:
            await bot.send_message(user_id, err_txt, reply_markup=retry_kb)
    finally:
        new_share_job.pop(user_id, None)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# DEEP SCAN SELF-CORRECTION SYSTEM
# /deepscanbatch â€” Upload a scan report to diagnose and self-correct missing ep detection
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

import re as _deepre

def _deep_extract_ep(filename: str) -> tuple[int, int] | None:
    """
    Multi-strategy episode extractor for deep scan correction.
    Tries 6 progressively looser strategies. Returns (ep_start, ep_end) or None.
    """
    base = filename
    # Strip extension
    dot = base.rfind('.')
    if dot > 0: base = base[:dot]

    # Strategy 1: zero-padded prefix "000047" or "047" with no letters before
    m = _deepre.match(r'^0*(\d{1,4})(?:[^0-9]|$)', base)
    if m:
        n = int(m.group(1))
        if 0 < n < 5000: return (n, n)

    # Strategy 2: "Ep47", "EP 47", "episode47"
    m = _deepre.search(r'(?i)(?:episode|epi|ep|chapter|ch|part|e|à¤à¤ªà¤¿à¤¸à¥‹à¤¡|à¤­à¤¾à¤—)[\s\-\:\.\#\_]*(\d{1,4})(?!\d)', base)
    if m:
        n = int(m.group(1))
        if 0 < n < 5000: return (n, n)

    # Strategy 3: range "47-53" or "47â€“53"
    m = _deepre.search(r'(?<!\d)(\d{1,4})\s*[-â€“â€”]\s*(\d{1,4})(?!\d)', base)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if 0 < a < 5000 and a < b < 5000: return (a, b)

    # Strategy 4: underscored sequence "001_047"
    m = _deepre.search(r'_0*(\d{1,4})(?:[_\-\s]|$)', base)
    if m:
        n = int(m.group(1))
        if 0 < n < 5000: return (n, n)

    # Strategy 5: last number in filename (loosest)
    nums = [int(x) for x in _deepre.findall(r'(?<![0-9])\d{1,4}(?![0-9])', base) if 0 < int(x) < 5000]
    if nums: return (nums[-1], nums[-1])

    return None


def _analyze_scan_report(report_text: str) -> dict:
    """
    Parse a plain-text scan report file (as generated by share_jobs or db_scanner)
    and build a structured diagnosis.
    Returns: {
        story, total_files, ep_range, missing, unassigned,
        file_entries: [{msg_id, filename, ep, parsed_ok, suggested_ep}]
    }
    """
    lines = report_text.splitlines()
    result = {
        "story": "", "total_files": 0, "ep_range": (0, 0),
        "missing": [], "unassigned": [],
        "file_entries": [], "raw_lines": len(lines)
    }

    # Parse header info
    for line in lines[:30]:
        m = _deepre.search(r'Story\s*:\s*(.+)|Channel\s*:\s*(.+)', line)
        if m: result["story"] = m.group(1) or m.group(2) or ""
        m = _deepre.search(r'Files processed\s*:\s*(\d+)|Files\s*:\s*(\d+)', line)
        if m: result["total_files"] = int(m.group(1) or m.group(2) or 0)
        m = _deepre.search(r'Episode range\s*:\s*(\d+)\s*[â€“\-]\s*(\d+)', line)
        if m: result["ep_range"] = (int(m.group(1)), int(m.group(2)))
        m = _deepre.search(r"Truly missing.*?:\s*(.+)", line)
        if m:
            nums = [int(x) for x in _deepre.findall(r'\d+', m.group(1))]
            result["missing"].extend(nums)

    # Parse file entries
    for line in lines:
        line = line.strip()
        msg_id = None
        fname = None
        parsed_ep_str = ""
        
        # Format 1: db_scanner output "    1     15148  document  000047_Filename.mp3  [2.1MB]"
        m_db = _deepre.search(r'^\d+\s+(\d+)\s+(audio|voice|document|video|\?)\s+(.+?)(?:\s+\[\d.*?\]|\s+â†³.*)?$', line)
        if m_db:
            msg_id = int(m_db.group(1))
            fname = m_db.group(3).strip()
        else:
            # Format 2: Old share_jobs format "  123456  |  000047_Filename.mp3  |  Ep 47"
            m_old = _deepre.search(r'(\d{5,})\s*[|\-:]\s*([^\|]+?)(?:\s*[|\-:]\s*(.+))?$', line)
            if m_old:
                msg_id = int(m_old.group(1))
                fname = m_old.group(2).strip()
                parsed_ep_str = (m_old.group(3) or "").strip()

        if msg_id and fname and fname != '(no name)' and fname != 'FileName / Title':
            suggested = _deep_extract_ep(fname)
            entry = {
                "msg_id": msg_id,
                "filename": fname,
                "reported_ep": parsed_ep_str,
                "suggested_ep": suggested,
                "parsed_ok": suggested is not None,
            }
            result["file_entries"].append(entry)

    return result


@Client.on_message(filters.private & filters.command(["deepscanbatch", "batchdiag"]))
async def cmd_deep_scan_batch(bot, message):
    """
    /deepscanbatch â€” Upload a txt scan report, get a deep diagnosis of why files
    were wrongly marked as missing, and receive a corrected summary.
    """
    from config import Config
    uid = message.from_user.id


    help_txt = (
        "<b>Â»  Deep Scan Self-Correction</b>\n\n"
        "Upload the <b>.txt report file</b> from a previous Batch Links run "
        "(the one with episode entries and filenames).\n\n"
        "The bot will:\n"
        "â€¢ Re-parse all filenames with 5 fallback strategies\n"
        "â€¢ Identify which 'missing' episodes are actually present with bad filename\n"
        "â€¢ Generate a corrected diagnosis report\n"
        "â€¢ Show you exactly which files failed to parse and why\n\n"
        "<i>Send the .txt file now, or /cancel to abort.</i>"
    )
    await message.reply_text(help_txt)

    try:
        resp = await _ask(bot, uid, "ðŸ“Ž <i>Waiting for your scan report file...</i>", timeout=300)
    except asyncio.TimeoutError:
        return await bot.send_message(uid, "<i>Timed out. Use /deepscanbatch again.</i>")

    if resp.text and any(x in resp.text.lower() for x in ['/cancel', 'cancel', 'â›”']):
        return await bot.send_message(uid, "<i>Cancelled.</i>", reply_markup=ReplyKeyboardRemove())

    doc = resp.document
    if not doc:
        return await bot.send_message(uid, "âš ï¸ Please send a <b>.txt file</b> (not text message).")
    if doc.file_size > 5 * 1024 * 1024:
        return await bot.send_message(uid, "âš ï¸ File too large (max 5MB).")

    sts = await bot.send_message(uid, "<i>Downloading and analyzing report...</i>")

    try:
        buf = await bot.download_media(resp, in_memory=True)
        buf.seek(0)
        report_text = buf.read().decode('utf-8', errors='replace')
    except Exception as e:
        return await sts.edit_text(f"<b>âŒ Download failed:</b> <code>{e}</code>")

    await sts.edit_text("<i>Running deep analysis...</i>")

    diagnosis = _analyze_scan_report(report_text)

    # Re-analyze all filenames in the report
    file_entries = diagnosis["file_entries"]
    total_entries = len(file_entries)
    parsed_ok   = [e for e in file_entries if e["parsed_ok"]]
    failed_parse = [e for e in file_entries if not e["parsed_ok"]]

    # Find entries reported as missing but our deep extractor can parse
    corrected = []
    for e in failed_parse:
        sug = _deep_extract_ep(e["filename"])
        if sug:
            corrected.append(e)

    # Build diagnosis lines
    lines_out = [
        f"<b>Â»  Deep Scan Diagnosis</b>",
        f"<b>Story:</b> {diagnosis['story'] or 'Unknown'}",
        f"<b>Report lines:</b> {diagnosis['raw_lines']}",
        f"<b>File entries found:</b> {total_entries}",
        f"",
        f"<b>âœ… Correctly parsed by original system:</b> {len(parsed_ok)}",
        f"<b>âš ï¸ Failed original parse:</b> {len(failed_parse)}",
        f"<b>ðŸ”§ Deep extractor can fix:</b> {len(corrected)}",
        f"",
    ]

    if diagnosis["missing"]:
        lines_out.append(f"<b>âŒ Episodes reported as missing by original system:</b> {len(diagnosis['missing'])}")
        miss_str = ", ".join(str(e) for e in sorted(diagnosis["missing"])[:20])
        if len(diagnosis["missing"]) > 20:
            miss_str += f" (+{len(diagnosis['missing'])-20} more)"
        lines_out.append(f"  <code>{miss_str}</code>")
        lines_out.append("")

    if corrected:
        lines_out.append(f"<b>ðŸ”§ Files the deep extractor could parse (were NOT missing):</b>")
        for e in corrected[:15]:
            sug = e["suggested_ep"]
            ep_label = f"Ep {sug[0]}â€“{sug[1]}" if sug[0] != sug[1] else f"Ep {sug[0]}"
            lines_out.append(f"  â€¢ <code>{e['filename'][:50]}</code> â†’ <b>{ep_label}</b>")
        if len(corrected) > 15:
            lines_out.append(f"  ... and {len(corrected)-15} more files")
        lines_out.append("")

    if failed_parse:
        still_unknown = [e for e in failed_parse if not e.get("suggested_ep")]
        if still_unknown:
            lines_out.append(f"<b>â“ Truly unparseable files (even with deep scan):</b> {len(still_unknown)}")
            lines_out.append("<i>These files genuinely have no episode number in their name.</i>")
            lines_out.append("<i>â†’ They will be embedded chronologically in buttons (NOT missing).</i>")
            lines_out.append("")

    # Corrected missing count
    actually_missing = max(0, len(diagnosis["missing"]) - len(corrected))
    lines_out += [
        f"<b>â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€</b>",
        f"<b>ðŸŽ¯ CORRECTED VERDICT:</b>",
        f"  â€¢ Files originally flagged missing: <code>{len(diagnosis['missing'])}</code>",
        f"  â€¢ Files deep-scan can recover: <code>{len(corrected)}</code>",
        f"  â€¢ <b>Truly missing (not in DB at all): <code>{actually_missing}</code></b>",
        f"",
        f"<i>ðŸ’¡ To fix: Run Batch Links again â€” the re-runs benefit from the improved parser.</i>",
        f"<i>If filenames are genuinely missing episode numbers, rename them in the DB channel and re-run.</i>",
    ]

    # Save full corrected report as file
    import datetime
    import io
    now = datetime.datetime.now()
    report_bytes_out = io.BytesIO()
    full_report_lines = [
        "=" * 60,
        "  ARYA BOT â€” Deep Scan Correction Report",
        "=" * 60,
        f"Generated: {now.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Story    : {diagnosis['story'] or 'Unknown'}",
        f"Total entries in report: {total_entries}",
        "-" * 60,
        f"Correctly parsed by original: {len(parsed_ok)}",
        f"Failed original parse:        {len(failed_parse)}",
        f"Deep extractor can fix:       {len(corrected)}",
        f"Actually missing (confirmed):  {actually_missing}",
        "=" * 60,
        "FILES THAT DEEP SCAN RECOVERED (were NOT missing):",
        "-" * 60,
    ]
    for e in corrected:
        sug = e["suggested_ep"]
        full_report_lines.append(f"  MsgID {e['msg_id']:>10}  |  {e['filename'][:60]}  |  Ep {sug[0]}â€“{sug[1]}")

    full_report_lines += ["", "=" * 60, "TRULY UNPARSEABLE FILES (no ep number at all):", "-" * 60]
    for e in failed_parse:
        if not e.get("suggested_ep"):
            full_report_lines.append(f"  MsgID {e['msg_id']:>10}  |  {e['filename'][:60]}")

    report_bytes_out.write("\n".join(full_report_lines).encode('utf-8'))
    report_bytes_out.seek(0)
    report_bytes_out.name = f"deep_scan_{diagnosis['story'] or 'report'}_{now.strftime('%Y%m%d_%H%M')}.txt"

    final_txt = "\n".join(lines_out)
    try:
        await sts.edit_text(final_txt)
    except Exception:
        await bot.send_message(uid, final_txt)

    await bot.send_document(uid, report_bytes_out, caption="ðŸ“Ž Full deep scan correction report", file_name=report_bytes_out.name)
