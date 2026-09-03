import os
import sys
import asyncio
import logging
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from database import db
from config import Config
from pyrogram import Client
from pyrogram.errors import FloodWait

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("OngoingTester")


async def run_live_test():
    print("=" * 90)
    print("🔍 ARYA ONGOING STORY LIVE MONITOR — MULTI-BOT SCANNER & SYNC")
    print("=" * 90)

    all_clients: list[Client] = []

    # 1. Connect Management Bot (MGMT_BOT_TOKEN)
    mgmt_bot = Client(
        name="mgmt_bot_test",
        api_id=Config.API_ID,
        api_hash=Config.API_HASH,
        bot_token=Config.MGMT_BOT_TOKEN,
        in_memory=True
    )
    try:
        await mgmt_bot.start()
        me = await mgmt_bot.get_me()
        print(f"✅ Connected Management Bot: @{me.username} (ID: {me.id})")
        all_clients.append(mgmt_bot)
    except Exception as e:
        print(f"⚠️ Could not connect Management Bot: {e}")

    # 2. Connect All Store Bots from MongoDB
    try:
        store_bots_docs = await db.db.premium_bots.find({"status": {"$ne": "inactive"}}).to_list(length=None)
        print(f"📦 Found {len(store_bots_docs)} Store Bots in MongoDB:")
        for b in store_bots_docs:
            tok = b.get("bot_token") or b.get("token")
            uname = b.get("bot_username") or b.get("username")
            if tok and tok != getattr(Config, "MGMT_BOT_TOKEN", ""):
                cli = Client(
                    name=f"bot_{uname or 'store'}",
                    api_id=Config.API_ID,
                    api_hash=Config.API_HASH,
                    bot_token=tok,
                    in_memory=True
                )
                try:
                    await cli.start()
                    all_clients.append(cli)
                    print(f"  ✅ Connected Store Bot: @{uname}")
                except Exception as e:
                    print(f"  ⚠️ Failed to connect Store Bot @{uname}: {e}")
    except Exception as e:
        print(f"⚠️ Error querying store bots: {e}")

    print(f"\n🚀 Total Active Bot Clients: {len(all_clients)}")

    # 3. Find all ongoing stories
    query = {
        "$or": [
            {"status": {"$in": ["Ongoing", "ongoing", "ONGOING"]}},
            {"parts.badge": "ongoing"},
            {"parts.is_ongoing": True},
            {"is_ongoing": True},
            {"status": {"$nin": ["Completed", "completed", "COMPLETED", "Unfinished", "unfinished", "Stucked", "stucked"]}}
        ],
        "is_completed": {"$ne": True}
    }

    stories = await db.db.premium_stories.find(query).to_list(length=None)
    print(f"📋 Checking {len(stories)} ongoing stories in MongoDB:\n")

    updated_count = 0
    up_to_date_count = 0
    no_access_count = 0

    for idx, s in enumerate(stories, 1):
        s_id = str(s.get("_id"))
        s_title = s.get("story_name_en") or s.get("story_name") or s.get("title") or "Untitled"
        raw_source = s.get("source") or s.get("source_channel") or s.get("channel_id")
        current_end_id = s.get("end_id") or s.get("end_message_id") or 0
        current_episodes = s.get("episodes") or "0"
        parts = s.get("parts") or []

        if not raw_source:
            continue

        source_id = raw_source
        try:
            source_id = int(source_id)
            if source_id > 0 and len(str(source_id)) >= 9:
                source_id = int(f"-100{source_id}")
        except Exception:
            pass

        print(f"[{idx}] Story: '{s_title}' (ID: {s_id})")
        print(f"    • Channel: {source_id} | DB End ID: {current_end_id} | DB Episodes: {current_episodes} | Parts: {len(parts)}")

        # Try to resolve channel using ANY available bot client
        active_client = None
        for cli in all_clients:
            try:
                chat = await cli.get_chat(source_id)
                active_client = cli
                print(f"    • Channel Resolved via @{cli.me.username}: '{chat.title}'")
                break
            except Exception:
                continue

        if not active_client:
            print(f"    ❌ Cannot access channel {source_id}")
            print("       (Add any of your bots as Admin to this channel to monitor new episodes!)")
            print("-" * 90)
            no_access_count += 1
            continue

        # Determine base end_id
        try:
            end_id = int(current_end_id)
        except ValueError:
            end_id = 0

        parts_end_ids = [int(p.get("end_id") or 0) for p in parts if p.get("end_id")]
        if parts_end_ids:
            end_id = max(end_id, max(parts_end_ids))

        # Fetch batch of next message IDs using get_messages
        ids_to_fetch = list(range(end_id + 1, end_id + 150))
        print(f"    • Scanning Message IDs: {end_id + 1} to {end_id + 150}...")

        try:
            msgs = await active_client.get_messages(source_id, ids_to_fetch)
        except FloodWait as fw:
            await asyncio.sleep(fw.value)
            msgs = await active_client.get_messages(source_id, ids_to_fetch)
        except Exception as ex:
            print(f"    ❌ Error fetching messages: {ex}")
            print("-" * 90)
            continue

        new_audio_count = 0
        last_found_id = -1
        highest_ep = -1

        for msg in msgs:
            if not msg or msg.empty:
                continue

            if msg.id > last_found_id:
                last_found_id = msg.id

            if not msg.audio and not msg.document and not msg.voice:
                continue

            new_audio_count += 1
            fname = getattr(msg.audio or msg.document or msg.voice, "file_name", "")
            caption = msg.caption or ""
            title = getattr(msg.audio, "title", "") if msg.audio else ""

            # Extract episode
            text = f"{fname} {title} {caption}".strip()
            m = re.search(r'(?:[eE]p(?:isode)?|[eE]pisode|[eE]p|\b[eE]\b|एपिसोड|कड़ी|kadi|ch(?:apter)?|अध्याय)[\.\-\s_:#]*(\d+)', text, re.IGNORECASE)
            if m:
                ep_val = int(m.group(1))
                if ep_val > highest_ep:
                    highest_ep = ep_val
            else:
                nums = re.findall(r'\b\d+\b', text)
                if nums:
                    valid_nums = [int(n) for n in nums if int(n) < 50000]
                    if valid_nums and valid_nums[-1] > highest_ep:
                        highest_ep = valid_nums[-1]

        if last_found_id > end_id:
            new_end_id = last_found_id
            new_eps = str(highest_ep) if highest_ep > -1 else current_episodes

            update_data = {
                "end_id": new_end_id,
                "end_message_id": new_end_id,
            }
            if highest_ep > -1:
                update_data["episodes"] = new_eps

            # Update parts
            if parts:
                updated_parts = []
                found_ong = False
                for p_idx, p in enumerate(parts):
                    p_c = dict(p)
                    b_val = str(p_c.get("badge") or p_c.get("badge_type") or "").lower()
                    is_ong = bool(p_c.get("is_ongoing") or b_val == "ongoing")
                    is_last = (p_idx == len(parts) - 1)
                    if is_ong or (not found_ong and is_last and str(s.get("status", "")).lower() == "ongoing"):
                        found_ong = True
                        p_c["end_id"] = new_end_id
                        p_c["is_ongoing"] = True
                        p_c["badge"] = "ongoing"
                        if highest_ep > -1:
                            curr_p_eps = str(p_c.get("episodes") or "").strip()
                            m_p = re.search(r"(\d+)", curr_p_eps)
                            if m_p:
                                start_ep = int(m_p.group(1))
                                p_c["episodes"] = f"{start_ep}-{highest_ep}"
                            else:
                                p_c["episodes"] = str(highest_ep)
                    updated_parts.append(p_c)
                update_data["parts"] = updated_parts

            await db.db.premium_stories.update_one({"_id": s["_id"]}, {"$set": update_data})
            print(f"    🟢 SUCCESS! Found {new_audio_count} new audio files!")
            print(f"       -> End ID updated: {current_end_id} -> {new_end_id}")
            print(f"       -> Episodes updated: {current_episodes} -> {new_eps}")
            if parts:
                for p in update_data.get("parts", []):
                    if p.get("is_ongoing"):
                        print(f"       -> Ongoing Part '{p.get('name')}' Updated: Episodes '{p.get('episodes')}', End ID '{p.get('end_id')}'")
            updated_count += 1
        else:
            print(f"    ⚪ Up to date: No new messages found after ID {end_id}.")
            up_to_date_count += 1

        print("-" * 90)

    for cli in all_clients:
        try:
            await cli.stop()
        except Exception:
            pass

    print("\n" + "=" * 90)
    print("📊 SCAN SUMMARY:")
    print(f"  🟢 Stories Updated with New Episodes : {updated_count}")
    print(f"  ⚪ Stories Already Up To Date        : {up_to_date_count}")
    print(f"  ❌ Channels Missing Bot Admin Access : {no_access_count}")
    print("=" * 90)


if __name__ == "__main__":
    asyncio.run(run_live_test())
