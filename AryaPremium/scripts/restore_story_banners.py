import os
import sys
import asyncio
import logging
from collections import defaultdict
from bson.objectid import ObjectId

# Setup import paths
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_THIS_DIR)
sys.path.insert(0, _PARENT_DIR)
sys.path.insert(0, os.getcwd())

try:
    from database import db as default_db
    from config import Config
except ImportError:
    try:
        from AryaPremium.database import db as default_db
        from AryaPremium.config import Config
    except ImportError:
        default_db = None
        Config = None

logger = logging.getLogger("RestoreBanners")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

CHECKOUT_IMAGE_PATTERNS = ["a6xw61", "4ud7fx"]


def is_checkout_image(val) -> bool:
    """Checks if a string value contains known checkout instruction image signatures."""
    if not val or not isinstance(val, str):
        return False
    v_low = val.lower()
    return any(pat in v_low for pat in CHECKOUT_IMAGE_PATTERNS)


async def restore_contaminated_banners(db=None, bot_client=None, dry_run: bool = False) -> dict:
    """
    Scans premium_stories in MongoDB for documents where the banner/image was contaminated
    by the checkout instructions image (e.g. https://files.catbox.moe/a6xw61.png, 4ud7fx.png,
    or the file_id resulting from uploading them).
    Restores the original banner from:
      1. poster_url (if not checkout url)
      2. banner_url (if not checkout url)
      3. image_url (if not checkout url)
      4. cover_url (if not checkout url)
      5. poster_file_id
      6. cover / poster / banner (if clean)
      7. Channel message (if poster_msg_id and channel_id exist and bot_client is provided)
    """
    db = db or default_db
    if not db:
        logger.error("❌ Database instance is not available.")
        return {"error": "No database"}

    logger.info("🔍 [RestoreBanners] Scanning premium_stories for contaminated checkout banners...")

    try:
        stories = await db.db.premium_stories.find({}).to_list(length=None)
    except Exception as e:
        logger.error(f"❌ Failed to fetch premium_stories from database: {e}")
        return {"error": str(e)}

    total_stories = len(stories)
    logger.info(f"📊 Total stories in collection: {total_stories}")

    # ── Step 1: Detect instruction image file IDs shared across multiple stories ──
    # When checkout uploaded the instruction image, Telegram returned a file_id that was saved
    # into story["image"] across multiple stories.
    image_fid_counts = defaultdict(list)
    for s in stories:
        img = s.get("image")
        if img and isinstance(img, str) and not img.startswith("http") and not img.startswith("/"):
            image_fid_counts[img].append(s)

    known_bad_fids = set()
    for fid, docs in image_fid_counts.items():
        if len(docs) >= 2:
            # Check if these stories have distinct titles or distinct poster_file_ids
            distinct_titles = {d.get("clean_title") or d.get("story_name_en") or str(d.get("_id")) for d in docs}
            if len(distinct_titles) > 1:
                known_bad_fids.add(fid)
                logger.info(f"⚠️ Identified shared checkout instruction file_id across {len(docs)} stories: {fid[:25]}... ({len(distinct_titles)} distinct titles)")

    # ── Step 2: Analyze each story for contamination & determine recovery ──
    contaminated_stories = []
    for s in stories:
        s_id = s.get("_id")
        title = s.get("clean_title") or s.get("story_name_en") or s.get("title") or str(s_id)
        current_img = s.get("image")
        current_poster_url = s.get("poster_url")
        current_banner_url = s.get("banner_url")
        current_img_url = s.get("image_url")
        poster_fid = s.get("poster_file_id")

        is_contaminated = False
        reasons = []

        if is_checkout_image(current_img):
            is_contaminated = True
            reasons.append("image field contains checkout image URL")

        if is_checkout_image(current_poster_url):
            is_contaminated = True
            reasons.append("poster_url contains checkout image URL")

        if is_checkout_image(current_banner_url):
            is_contaminated = True
            reasons.append("banner_url contains checkout image URL")

        if current_img in known_bad_fids:
            is_contaminated = True
            reasons.append(f"image matches known shared checkout instruction file_id ({current_img[:20]}...)")

        if not is_contaminated:
            continue

        # Find best clean replacement for the story banner
        clean_banner = None
        clean_banner_src = None

        # 1. Test public HTTP poster URLs
        for cand_k in ("poster_url", "banner_url", "image_url", "cover_url"):
            cand_val = s.get(cand_k)
            if cand_val and isinstance(cand_val, str):
                cand_val = cand_val.strip()
                if (cand_val.startswith("http://") or cand_val.startswith("https://") or cand_val.startswith("/")) and not is_checkout_image(cand_val):
                    clean_banner = cand_val
                    clean_banner_src = cand_k
                    break

        # 2. Test poster_file_id
        if not clean_banner and poster_fid and isinstance(poster_fid, str):
            poster_fid = poster_fid.strip()
            if poster_fid not in known_bad_fids and not is_checkout_image(poster_fid) and not poster_fid.startswith("http"):
                clean_banner = poster_fid
                clean_banner_src = "poster_file_id"

        # 3. Test other fields: cover, poster, banner
        if not clean_banner:
            for cand_k in ("cover", "poster", "banner"):
                cand_val = s.get(cand_k)
                if cand_val and isinstance(cand_val, str):
                    cand_val = cand_val.strip()
                    if cand_val not in known_bad_fids and not is_checkout_image(cand_val):
                        clean_banner = cand_val
                        clean_banner_src = cand_k
                        break

        # 4. Telegram Channel Message recovery if Pyrogram client provided
        if not clean_banner and bot_client:
            chan_id = s.get("channel_id") or s.get("source")
            p_msg_id = s.get("poster_msg_id") or (s.get("parts", [{}])[0].get("msg_id") if s.get("parts") else None)
            if chan_id and p_msg_id:
                try:
                    c_id = int(chan_id)
                    if c_id > 0 and len(str(c_id)) >= 9:
                        c_id = int(f"-100{c_id}")
                    p_msg = await bot_client.get_messages(c_id, int(p_msg_id))
                    if p_msg and (p_msg.photo or getattr(p_msg.video, 'thumbs', None)):
                        photo_obj = p_msg.photo or (p_msg.video.thumbs[0] if getattr(p_msg.video, 'thumbs', None) else None)
                        if photo_obj and getattr(photo_obj, 'file_id', None):
                            clean_banner = photo_obj.file_id
                            clean_banner_src = f"channel_{chan_id}_msg_{p_msg_id}"
                except Exception as ex:
                    logger.debug(f"Channel fetch fallback error for story {s_id}: {ex}")

        contaminated_stories.append({
            "doc": s,
            "title": title,
            "reasons": reasons,
            "current_img": current_img,
            "clean_banner": clean_banner,
            "clean_banner_src": clean_banner_src
        })

    logger.info(f"🚨 Found {len(contaminated_stories)} contaminated story banner records out of {total_stories} total stories.")

    # ── Step 3: Apply restorations in MongoDB ──
    restored_count = 0
    failed_count = 0

    for item in contaminated_stories:
        s = item["doc"]
        s_id = s["_id"]
        title = item["title"]
        clean_banner = item["clean_banner"]
        clean_src = item["clean_banner_src"]

        updates = {}
        unsets = {}

        if clean_banner:
            updates["image"] = clean_banner

        # Clean corrupted poster_url / banner_url fields if they were pointing to checkout images
        if is_checkout_image(s.get("poster_url")):
            if clean_banner and str(clean_banner).startswith("http"):
                updates["poster_url"] = clean_banner
            else:
                unsets["poster_url"] = ""

        if is_checkout_image(s.get("banner_url")):
            if clean_banner and str(clean_banner).startswith("http"):
                updates["banner_url"] = clean_banner
            else:
                unsets["banner_url"] = ""

        if is_checkout_image(s.get("image_url")):
            if clean_banner and str(clean_banner).startswith("http"):
                updates["image_url"] = clean_banner
            else:
                unsets["image_url"] = ""

        if not updates and not unsets:
            logger.warning(f"⚠️ Could not restore banner for '{title}' (ID: {s_id}): No pristine source found. Reasons: {item['reasons']}")
            failed_count += 1
            continue

        mongo_ops = {}
        if updates:
            mongo_ops["$set"] = updates
        if unsets:
            mongo_ops["$unset"] = unsets

        if dry_run:
            logger.info(f"[DRY RUN] Would restore '{title}' (ID: {s_id}) -> image restored from '{clean_src}': {str(clean_banner)[:35]}...")
            restored_count += 1
        else:
            try:
                await db.db.premium_stories.update_one({"_id": s_id}, mongo_ops)
                logger.info(f"✅ Restored story '{title}' (ID: {s_id}) -> image restored from '{clean_src}'")
                restored_count += 1
            except Exception as ex:
                logger.error(f"❌ Failed to update story '{title}' (ID: {s_id}): {ex}")
                failed_count += 1

    summary = {
        "total_scanned": total_stories,
        "contaminated_found": len(contaminated_stories),
        "restored_count": restored_count,
        "failed_count": failed_count,
        "dry_run": dry_run
    }
    logger.info(f"🏁 [RestoreBanners Finished] Scanned: {total_stories} | Contaminated: {len(contaminated_stories)} | Restored: {restored_count} | Unresolved: {failed_count}")
    return summary


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    logger.info(f"Starting Standalone Story Banner Restoration (dry_run={dry})...")
    try:
        res = asyncio.run(restore_contaminated_banners(dry_run=dry))
        print("\n--- Summary ---")
        for k, v in res.items():
            print(f"{k}: {v}")
    except Exception as e:
        logger.error(f"Fatal error during restore: {e}", exc_info=True)
