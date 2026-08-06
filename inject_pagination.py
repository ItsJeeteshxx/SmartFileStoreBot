"""
All changes injected into market_seller.py using binary read/write to avoid encoding issues.
Changes:
1. Help menu pages: Add Support button + Feedback/Suggestions button
2. Marketplace story listing: Add pagination + name truncation
3. Story matching: Handle truncated names
4. _process_text: Add NEXT/PREV nav handler
5. _process_text: Add feedback submission handler
6. _process_callback: Add feedback_start and feedback reply state handlers
"""

path = 'AryaPremium/plugins/userbot/market_seller.py'

# Read raw bytes, decode as UTF-8
with open(path, 'rb') as f:
    raw = f.read()

content = raw.decode('utf-8').replace('\r\n', '\n')
changes = []

# ─────────────────────────────────────────────────────────────────
# CHANGE 1a: Help page 0 keyboard - add Support + Feedback
# ─────────────────────────────────────────────────────────────────
old = """        kb = [
            [InlineKeyboardButton(f"{_sc('TERMS')}", callback_data="mb#help_tc"),
             InlineKeyboardButton(f"{_sc('REFUND')}", callback_data="mb#help_refund")],
            [InlineKeyboardButton(f"\u0939\u093f\u0902\u0926\u0940 (NEXT) \u276d", callback_data="mb#help_page_1")],
            [InlineKeyboardButton(f"\u00ab \u276e {_sc('MAIN MENU')}", callback_data="mb#main_back")]
        ]"""
new = """        kb = [
            [InlineKeyboardButton(f"{_sc('TERMS')}", callback_data="mb#help_tc"),
             InlineKeyboardButton(f"{_sc('REFUND')}", callback_data="mb#help_refund"),
             InlineKeyboardButton("Support", url="https://t.me/+KPVtaAm9k-RmMjdl")],
            [InlineKeyboardButton(f"\ud83d\udcac {_sc('FEEDBACK / SUGGESTIONS')}", callback_data="mb#feedback_start")],
            [InlineKeyboardButton(f"\u0939\u093f\u0902\u0926\u0940 (NEXT) \u276d", callback_data="mb#help_page_1")],
            [InlineKeyboardButton(f"\u00ab \u276e {_sc('MAIN MENU')}", callback_data="mb#main_back")]
        ]"""
if old in content:
    content = content.replace(old, new, 1)
    changes.append("1a. Help p0 kb: OK")
else:
    changes.append("1a. Help p0 kb: MISS")

# ─────────────────────────────────────────────────────────────────
# CHANGE 1b: Help page 1 keyboard - add Support + Feedback
# ─────────────────────────────────────────────────────────────────
old = """        kb = [
            [InlineKeyboardButton(f"{_sc('TERMS')}", callback_data="mb#help_tc"),
             InlineKeyboardButton(f"{_sc('REFUND')}", callback_data="mb#help_refund")],
            [InlineKeyboardButton(f"\u276c PREV (English)", callback_data="mb#help_page_0")],
            [InlineKeyboardButton(f"\u00ab \u276e {_sc('MAIN MENU')}", callback_data="mb#main_back")]
        ]"""
new = """        kb = [
            [InlineKeyboardButton(f"{_sc('TERMS')}", callback_data="mb#help_tc"),
             InlineKeyboardButton(f"{_sc('REFUND')}", callback_data="mb#help_refund"),
             InlineKeyboardButton("Support", url="https://t.me/+KPVtaAm9k-RmMjdl")],
            [InlineKeyboardButton(f"\ud83d\udcac {_sc('FEEDBACK / SUGGESTIONS')}", callback_data="mb#feedback_start")],
            [InlineKeyboardButton(f"\u276c PREV (English)", callback_data="mb#help_page_0")],
            [InlineKeyboardButton(f"\u00ab \u276e {_sc('MAIN MENU')}", callback_data="mb#main_back")]
        ]"""
if old in content:
    content = content.replace(old, new, 1)
    changes.append("1b. Help p1 kb: OK")
else:
    changes.append("1b. Help p1 kb: MISS")

# ─────────────────────────────────────────────────────────────────
# CHANGE 2: Story listing - pagination + name truncation
# ─────────────────────────────────────────────────────────────────
old = """        kb = []
        for idx, s in enumerate(stories, start=1):
            s_name = s.get(f'story_name_{lang}', s.get('story_name_en'))
            kb.append([f"{idx}. {s_name} [ \u20b9 {s.get('price', 0)} ]"])
        kb.append(["\ud83d\udd0d " + ("SEARCH" if lang=='en' else "\u0916\u094b\u091c\u0947\u0902")])
        kb.append([T[lang]["cant_find_btn"]])
        kb.append(["\u00ab " + ("\ud835\uddd5\ud835\uddee\ud835\uddf0\ud835\uddf8 \ud835\ude01\ud835\uddfc \ud835\udde0\ud835\uddf2\ud835\uddfb\ud835\ude02" if lang=='en' else "\u0935\u093e\u092a\u0938 \u092e\u0947\u0928\u0942")])"""
new = """        # -- Paginated story listing --
        STORY_PAGE_SIZE = 8
        s_page = int(user.get("_mkt_page", 0))
        if user.get("_mkt_plat") != txt:
            s_page = 0
        total_s = len(stories)
        total_pages_s = max(1, (total_s + STORY_PAGE_SIZE - 1) // STORY_PAGE_SIZE)
        s_page = max(0, min(s_page, total_pages_s - 1))
        await db.db.users.update_one({"id": user_id}, {"$set": {"_mkt_plat": txt, "_mkt_page": s_page}})
        page_stories = stories[s_page * STORY_PAGE_SIZE:(s_page + 1) * STORY_PAGE_SIZE]
        MAX_NAME_LEN = 22
        kb = []
        for idx, s in enumerate(page_stories, start=s_page * STORY_PAGE_SIZE + 1):
            s_name = s.get(f'story_name_{lang}', s.get('story_name_en'))
            if len(s_name) > MAX_NAME_LEN:
                s_name = s_name[:MAX_NAME_LEN - 1] + "\u2026"
            kb.append([f"{idx}. {s_name} [ \u20b9 {s.get('price', 0)} ]"])
        nav_row = []
        if s_page > 0:
            nav_row.append("\u276c " + (_sc("PREV") if lang == 'en' else "\u092a\u093f\u091b\u0932\u093e"))
        if s_page < total_pages_s - 1:
            nav_row.append(_sc("NEXT") + " \u276d" if lang == 'en' else "\u0905\u0917\u0932\u093e \u276d")
        if nav_row:
            kb.append(nav_row)
        kb.append(["\ud83d\udd0d " + ("SEARCH" if lang == 'en' else "\u0916\u094b\u091c\u0947\u0902")])
        kb.append([T[lang]["cant_find_btn"]])
        kb.append(["\u00ab " + ("\ud835\uddd5\ud835\uddee\ud835\uddf0\ud835\uddf8 \ud835\ude01\ud835\uddfc \ud835\udde0\ud835\uddf2\ud835\uddfb\ud835\ude02" if lang == 'en' else "\u0935\u093e\u092a\u0938 \u092e\u0947\u0928\u0942")])"""
if old in content:
    content = content.replace(old, new, 1)
    changes.append("2. Story listing paginated: OK")
else:
    changes.append("2. Story listing paginated: MISS - checking nearby")
    idx2 = content.find("enumerate(stories, start=1)")
    if idx2 > 0:
        changes.append("   -> Found enumerate at " + str(idx2))

# ─────────────────────────────────────────────────────────────────
# CHANGE 3: Story name matching - handle truncated names
# ─────────────────────────────────────────────────────────────────
old = """        story = None
        for st in stories:
            candidates = [
                st.get("story_name_en", ""),
                st.get("story_name_hi", ""),
                st.get("story_name_hi", ""),
            ]
            if sName in candidates:
                story = st
                break
        if not story:
            return await message.reply_text("<i>Story not found or removed.</i>")"""
new = """        story = None
        MAX_NAME_LEN = 22
        for st in stories:
            for field in ("story_name_en", "story_name_hi"):
                full_name = st.get(field, "")
                trunc = full_name[:MAX_NAME_LEN - 1] + "\u2026" if len(full_name) > MAX_NAME_LEN else full_name
                if sName in (full_name, trunc):
                    story = st
                    break
            if story:
                break
        if not story:
            return await message.reply_text("<i>Story not found or removed.</i>", parse_mode=enums.ParseMode.HTML)"""
if old in content:
    content = content.replace(old, new, 1)
    changes.append("3. Story match fixed: OK")
else:
    changes.append("3. Story match fixed: MISS")

# ─────────────────────────────────────────────────────────────────
# CHANGE 4: Inject NEXT/PREV navigation handler in _process_text
# ─────────────────────────────────────────────────────────────────
nav_handler = """    # -- Marketplace NEXT/PREV pagination nav --
    _nav_next_en = _sc("NEXT") + " \u276d"
    _nav_prev_en = "\u276c " + _sc("PREV")
    _nav_next_hi = "\u0905\u0917\u0932\u093e \u276d"
    _nav_prev_hi = "\u276c \u092a\u093f\u091b\u0932\u093e"
    if txt in (_nav_next_en, _nav_prev_en, _nav_next_hi, _nav_prev_hi):
        plat = user.get("_mkt_plat")
        cur_page = int(user.get("_mkt_page", 0))
        if plat:
            is_next = txt in (_nav_next_en, _nav_next_hi)
            new_page = cur_page + 1 if is_next else cur_page - 1
            STORY_PAGE_SIZE = 8
            query_find = {"bot_id": client.me.id}
            if plat != "Other": query_find["platform"] = plat
            stories = await db.db.premium_stories.find(query_find).to_list(length=None)
            total_pages_s = max(1, (len(stories) + STORY_PAGE_SIZE - 1) // STORY_PAGE_SIZE)
            new_page = max(0, min(new_page, total_pages_s - 1))
            await db.db.users.update_one({"id": user_id}, {"$set": {"_mkt_page": new_page}})
            page_stories = stories[new_page * STORY_PAGE_SIZE:(new_page + 1) * STORY_PAGE_SIZE]
            MAX_NAME_LEN = 22
            kb = []
            for idx, s in enumerate(page_stories, start=new_page * STORY_PAGE_SIZE + 1):
                s_name = s.get(f'story_name_{lang}', s.get('story_name_en'))
                if len(s_name) > MAX_NAME_LEN:
                    s_name = s_name[:MAX_NAME_LEN - 1] + "\u2026"
                kb.append([f"{idx}. {s_name} [ \u20b9 {s.get('price', 0)} ]"])
            nav_row = []
            if new_page > 0:
                nav_row.append("\u276c " + (_sc("PREV") if lang == 'en' else "\u092a\u093f\u091b\u0932\u093e"))
            if new_page < total_pages_s - 1:
                nav_row.append(_sc("NEXT") + " \u276d" if lang == 'en' else "\u0905\u0917\u0932\u093e \u276d")
            if nav_row:
                kb.append(nav_row)
            kb.append(["\ud83d\udd0d " + ("SEARCH" if lang == 'en' else "\u0916\u094b\u091c\u0947\u0902")])
            kb.append([T[lang]["cant_find_btn"]])
            kb.append(["\u00ab " + ("\ud835\uddd5\ud835\uddee\ud835\uddf0\ud835\uddf8 \ud835\ude01\ud835\uddfc \ud835\udde0\ud835\uddf2\ud835\uddfb\ud835\ude02" if lang == 'en' else "\u0935\u093e\u092a\u0938 \u092e\u0947\u0928\u0942")])
            title = "AVAILABLE STORIES" if lang == 'en' else "\u0909\u092a\u0932\u092c\u094d\u0927 \u0938\u094d\u091f\u094b\u0930\u093f\u091c"
            return await message.reply_text(
                f"<b>\u27e6 {title} \u2014 {to_mathbold(plat)} \u27e7</b>\\n"
                f"<blockquote expandable><i>{_sc('Page')} {new_page+1}/{total_pages_s}</i></blockquote>",
                reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
                parse_mode=enums.ParseMode.HTML
            )
        return

    # Check if it's a story selection"""

old_anchor = "    # Check if it's a story selection e.g."
# Find the anchor after the Back-to-menu block
idx_anchor = content.find(old_anchor)
if idx_anchor > 0:
    content = content[:idx_anchor] + nav_handler + " e.g." + content[idx_anchor + len(old_anchor):]
    changes.append("4. NEXT/PREV nav handler: OK")
else:
    changes.append("4. NEXT/PREV nav handler: MISS")

# ─────────────────────────────────────────────────────────────────
# CHANGE 5: Feedback submission handler in _process_text
# ─────────────────────────────────────────────────────────────────
feedback_text_handler = """    # -- Feedback submission handler --
    if user.get("state") == "feedback_pending":
        if txt.strip().lower() == "/cancel":
            await db.update_user(user_id, {"state": None})
            await message.reply_text("<i>\u274c Feedback cancelled.</i>", reply_markup=ReplyKeyboardRemove(), parse_mode=enums.ParseMode.HTML)
            return await _send_main_menu(client, user_id, message.from_user, lang)
        content_type = "text"
        if message.photo: content_type = "photo"
        elif message.video: content_type = "video"
        elif message.document: content_type = "document"
        from datetime import datetime, timezone
        fb_doc = {
            "user_id": user_id,
            "bot_id": client.me.id,
            "type": content_type,
            "text": txt,
            "status": "open",
            "created_at": datetime.now(timezone.utc),
            "user_name": f"{message.from_user.first_name or ''} {message.from_user.last_name or ''}".strip(),
            "username": message.from_user.username or "",
        }
        result = await db.db.premium_feedback.insert_one(fb_doc)
        fb_id = str(result.inserted_id)
        try:
            from config import Config
            admins = getattr(Config, 'SUDO_USERS', []) or getattr(Config, 'OWNER_IDS', [])
            admin_txt = (
                f"<b>\ud83d\udce8 NEW FEEDBACK #{fb_id[:8]}</b>\\n"
                f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\\n"
                f"<b>User:</b> {fb_doc['user_name']} (<code>{user_id}</code>)\\n"
                f"<b>@:</b> @{fb_doc['username'] or 'N/A'}  |  <b>Type:</b> {content_type}\\n"
                f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\\n"
                f"<b>Message:</b> {txt[:500]}"
            )
            kb_admin = [
                [InlineKeyboardButton(f"\u2705 {_sc('Resolve')} #{fb_id[:8]}", callback_data=f"mk#fbresv_{fb_id}"),
                 InlineKeyboardButton(f"\ud83d\udcac {_sc('Reply')}", callback_data=f"mk#fbreply_{fb_id}_{user_id}")]
            ]
            if hasattr(db, 'mgmt_client') and db.mgmt_client:
                for admin_id in admins:
                    try:
                        if content_type == "photo":
                            await db.mgmt_client.send_photo(admin_id, photo=message.photo.file_id, caption=admin_txt, reply_markup=InlineKeyboardMarkup(kb_admin), parse_mode=enums.ParseMode.HTML)
                        elif content_type == "video":
                            await db.mgmt_client.send_video(admin_id, video=message.video.file_id, caption=admin_txt, reply_markup=InlineKeyboardMarkup(kb_admin), parse_mode=enums.ParseMode.HTML)
                        else:
                            await db.mgmt_client.send_message(admin_id, admin_txt, reply_markup=InlineKeyboardMarkup(kb_admin), parse_mode=enums.ParseMode.HTML)
                    except Exception:
                        pass
        except Exception:
            pass
        await db.update_user(user_id, {"state": None})
        if lang == 'hi':
            confirm_txt = "\u2705 <b>\u0906\u092a\u0915\u093e \u092b\u0940\u0921\u092c\u0948\u0915 \u0938\u092b\u0932\u0924\u093e\u092a\u0942\u0930\u094d\u0935\u0915 \u092d\u0947\u091c\u093e \u0917\u092f\u093e!</b>\\n<i>\u0939\u092e\u093e\u0930\u0940 \u091f\u0940\u092e \u091c\u0932\u094d\u0926 \u0939\u0940 \u0906\u092a\u0938\u0947 \u0938\u0902\u092a\u0930\u094d\u0915 \u0915\u0930\u0947\u0917\u0940\u0964</i>"
        else:
            confirm_txt = "\u2705 <b>Feedback submitted!</b>\\n<i>Our team will review it and get back to you shortly.</i>"
        await message.reply_text(
            confirm_txt,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"\u00ab {_sc('MAIN MENU')}", callback_data="mb#main_back")]]),
            parse_mode=enums.ParseMode.HTML
        )
        return

    # Handle DM Part Selection"""

# Replace the 'Handle DM Part Selection' anchor
old_dm = "    # Handle DM Part Selection"
if old_dm in content:
    content = content.replace(old_dm, feedback_text_handler, 1)
    changes.append("5. Feedback text handler: OK")
else:
    changes.append("5. Feedback text handler: MISS")

# ─────────────────────────────────────────────────────────────────
# CHANGE 6: Add feedback_start callback handler in _process_callback
# ─────────────────────────────────────────────────────────────────
feedback_cb_handler = """
    # -- Feedback start callback --
    elif cmd == "feedback_start":
        await query.answer()
        if lang == 'hi':
            fb_txt = (
                f"<b>\u27e6 {_sc('\u092b\u0940\u0921\u092c\u0948\u0915 / \u0938\u0941\u091d\u093e\u0935')} \u27e7</b>\\n\\n"
                f"<blockquote expandable>\u0905\u092a\u0928\u0940 \u0930\u093e\u092f, \u0938\u0941\u091d\u093e\u0935 \u092f\u093e \u0936\u093f\u0915\u093e\u092f\u0924 \u0928\u0940\u091a\u0947 \u0932\u093f\u0916\u0947\u0902\u0964\\n"
                f"\u0906\u092a <b>\u091f\u0947\u0915\u094d\u0938\u094d\u091f, \u092b\u094b\u091f\u094b \u092f\u093e \u0935\u093f\u0921\u093f\u092f\u094b</b> \u092d\u0947\u091c \u0938\u0915\u0924\u0947 \u0939\u0948\u0902\u0964 /cancel \u0915\u0930\u0928\u0947 \u0915\u0947 \u0932\u093f\u090f \u0932\u093f\u0916\u0947\u0902\u0964</blockquote>"
            )
        else:
            fb_txt = (
                f"<b>\u27e6 {_sc('FEEDBACK / SUGGESTIONS')} \u27e7</b>\\n\\n"
                f"<blockquote expandable>Share your <b>feedback, suggestions or complaints</b> below.\\n"
                f"You can send <b>text, photos, or videos</b>. Type /cancel to abort.</blockquote>"
            )
        await db.update_user(user_id, {"state": "feedback_pending"})
        await _safe_edit(query.message, text=fb_txt, markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"\u00ab {_sc('BACK')}", callback_data="mb#main_help")]
        ]))

"""

# Insert before the help_tc handler
anchor_tc = "\n    elif cmd == \"help_tc\":"
if anchor_tc in content:
    content = content.replace(anchor_tc, feedback_cb_handler + anchor_tc, 1)
    changes.append("6. Feedback cb handler: OK")
else:
    # Try another approach - insert after help_page_ block
    anchor_hp = "        return await _show_help_menu(client, query, page)\n"
    idx_hp = content.find(anchor_hp)
    if idx_hp > 0:
        insert_at = idx_hp + len(anchor_hp)
        content = content[:insert_at] + feedback_cb_handler + content[insert_at:]
        changes.append("6. Feedback cb handler (alt): OK")
    else:
        changes.append("6. Feedback cb handler: MISS")

# ─────────────────────────────────────────────────────────────────
# Write output
# ─────────────────────────────────────────────────────────────────
out_bytes = content.encode('utf-8')
with open(path, 'wb') as f:
    f.write(out_bytes)

import sys
for c in changes:
    sys.stdout.buffer.write((c + "\n").encode('utf-8'))
sys.stdout.buffer.write(b"DONE\n")
