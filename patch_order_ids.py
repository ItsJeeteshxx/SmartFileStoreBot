r"""
Patch script: Fix order ID generation in both mini_app_api.py files.
Run from the TryAryaBot directory.
"""
import sys
from pymongo import ReturnDocument  # just to verify it's importable

FILES = [
    "mini_app_api.py",
    "AryaPremium/mini_app_api.py",
]

# ── Patch 1: Fix _make_arya_order_id (ReturnDocument.AFTER + asc story ordering) ──────────────

OLD_FUNC = '''async def _make_arya_order_id(
    db_instance,
    tg_id: str,
    story_ids: list = None,
    source: str = "miniapp"
) -> str:
    """
    Generate a new structured Arya Order ID.
    Format: {PREFIX}-{TG_ID}-{DDMM}-{STORY_NUM}{ORDER_NUM}
    Prefix: AM = Mini App, AB = Bot (Telegram Bot)
    Example: AM-1071421266-2107-55130
    
    - Story number = position of first story in the global story list (sorted by _id desc)
    - Order number = auto-incremented global counter from DB (always unique)
    """
    try:
        from datetime import datetime as _dt
        
        # 1. Determine prefix based on source
        src_lower = str(source or "miniapp").lower()
        prefix = "AB" if "bot" in src_lower else "AM"
        
        # 2. Date & Month in DDMM format
        now = _dt.now()
        date_str = now.strftime("%d%m")
        
        # 3. Get global order number (auto-increment counter in DB)
        counter_doc = await db_instance.db.order_counters.find_one_and_update(
            {"_key": "global_order_counter"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=True  # returns the updated document
        )
        order_num = counter_doc.get("seq", 1) if counter_doc else 1
        
        # 4. Get story serial number (position in global story list sorted by _id desc)
        story_num = 0
        if story_ids and len(story_ids) > 0:
            try:
                from bson.objectid import ObjectId as _OID
                first_sid = story_ids[0]
                # Get ALL story IDs sorted by _id descending (newest = #1)
                all_ids = await db_instance.db.premium_stories.distinct("_id")
                all_ids_sorted = sorted(all_ids, reverse=True)
                try:
                    target_oid = _OID(first_sid)
                    if target_oid in all_ids_sorted:
                        story_num = all_ids_sorted.index(target_oid) + 1
                except Exception:
                    pass
            except Exception:
                story_num = 0
        
        # 5. Build the order ID
        tg_id_str = str(tg_id)
        if story_num > 0:
            return f"{prefix}-{tg_id_str}-{date_str}-{story_num}{order_num}"
        else:
            # Fallback if story number can't be determined
            return f"{prefix}-{tg_id_str}-{date_str}-{order_num}"
            
    except Exception as e:
        logger.warning(f"[OrderID] Failed to generate structured order ID: {e}. Falling back to legacy.")
        import uuid
        return f"OD_{tg_id}_{uuid.uuid4().hex[:8].upper()}"'''

NEW_FUNC = '''async def _make_arya_order_id(
    db_instance,
    tg_id: str,
    story_ids: list = None,
    source: str = "miniapp"
) -> str:
    """
    Generate a new structured Arya Order ID.
    Format: {PREFIX}-{TG_ID}-{DDMM}-{STORY_NUM}{ORDER_NUM}
    Prefix: AM = Mini App, AB = Bot (Telegram Bot)
    Example: AM-1071421266-2107-55130

    - Story number = serial position of story (oldest added = story #1, sorted _id asc)
    - Order number = globally unique auto-incremented counter via ReturnDocument.AFTER
    """
    try:
        from datetime import datetime as _dt
        from pymongo import ReturnDocument

        # 1. Determine prefix based on source
        src_lower = str(source or "miniapp").lower()
        prefix = "AB" if "bot" in src_lower else "AM"

        # 2. Date & Month in DDMM format
        now = _dt.now()
        date_str = now.strftime("%d%m")

        # 3. ATOMIC global counter — ReturnDocument.AFTER returns post-increment value (always unique)
        counter_doc = await db_instance.db.order_counters.find_one_and_update(
            {"_key": "global_order_counter"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER
        )
        order_num = counter_doc.get("seq", 1) if counter_doc else 1

        # 4. Story serial number (oldest story = #1, sorted by _id ascending)
        story_num = 0
        if story_ids and len(story_ids) > 0:
            try:
                from bson.objectid import ObjectId as _OID
                first_sid = story_ids[0]
                all_ids = await db_instance.db.premium_stories.distinct("_id")
                all_ids_sorted = sorted(all_ids)  # ascending: oldest = #1
                try:
                    target_oid = _OID(first_sid)
                    if target_oid in all_ids_sorted:
                        story_num = all_ids_sorted.index(target_oid) + 1
                except Exception:
                    pass
            except Exception:
                story_num = 0

        # 5. Build the final order ID
        tg_id_str = str(tg_id)
        if story_num > 0:
            return f"{prefix}-{tg_id_str}-{date_str}-{story_num}{order_num}"
        else:
            return f"{prefix}-{tg_id_str}-{date_str}-{order_num}"

    except Exception as e:
        logger.warning(f"[OrderID] Failed to generate structured order ID: {e}. Falling back to legacy.")
        import uuid
        return f"OD_{tg_id}_{uuid.uuid4().hex[:8].upper()}"'''


# ── Patch 2: generate-order-id endpoint + updated create-pending-order ─────────────────────────
# (Only for mini_app_api.py, not AryaPremium which doesn't have these endpoints)

OLD_PENDING = '''# ==========================================
# Create Pending UPI Order (QR Page Mount)
# ==========================================

@api_router.post("/create-pending-order")
async def create_pending_order(payload: dict):
    """
    Called the instant the QR payment screen is shown to the user \u2014 BEFORE they pay.
    Creates a status=\\"pending\\" order record so every payment attempt is traceable
    by order_id, even if the user pays to the wrong UPI ID or UTR verification fails.

    Idempotent: if the order_id already exists in the DB the endpoint returns success
    without creating a duplicate.
    """
    telegram_id   = payload.get("telegram_id")
    story_ids     = payload.get("story_ids", [])
    order_id      = str(payload.get("order_id", "")).strip()
    amount        = float(payload.get("amount", 0) or 0)
    upi_id_shown  = str(payload.get("upi_id_shown", "")).strip()  # exact UPI ID displayed
    promo_code    = str(payload.get("promo_code", "")).strip().upper()
    username      = str(payload.get("username", "")).strip()
    first_name    = str(payload.get("first_name", "")).strip()
    last_name     = str(payload.get("last_name", "")).strip()

    # Soft validation \u2014 never block the user flow, just log and return ok
    if not telegram_id or not order_id:
        logger.warning("create_pending_order: missing telegram_id or order_id \u2014 skipping")
        return {"success": True, "message": "skipped"}

    db = getattr(app.state, "db", None)
    if not db:
        logger.error("create_pending_order: DB not available")
        return {"success": True, "message": "db_unavailable"}

    try:
        # Idempotency: don't create duplicate if order_id already recorded
        existing = await db.db.orders.find_one({"order_id": order_id})
        if existing:
            logger.info(f"create_pending_order: order {order_id} already exists (status={existing.get('status')}) \u2014 skipping")
            return {"success": True, "message": "already_exists"}

        tg_id_int = int(telegram_id) if str(telegram_id).isdigit() else 0

        # Resolve story names from DB for richer admin view
        story_names = []
        if story_ids:
            from bson.objectid import ObjectId
            for sid in story_ids:
                try:
                    doc = await db.db.premium_stories.find_one({"_id": ObjectId(sid)})
                    if doc:
                        story_names.append(doc.get("story_name_en", doc.get("title", sid)))
                except Exception:
                    story_names.append(sid)

        pending_doc = {
            "order_id":       order_id,
            "user_id":        tg_id_int if tg_id_int else telegram_id,
            "username":       username,
            "first_name":     first_name,
            "last_name":      last_name,
            "story_ids":      story_ids,
            "story_names":    story_names,
            "total":          amount,
            "promo_code":     promo_code if promo_code else None,
            "status":         "pending",
            "source":         "upi_manual_miniapp",
            "upi_id_shown":   upi_id_shown,   # audit trail \u2014 exact UPI ID user saw
            "created_at":     datetime.now(timezone.utc),
        }
        await db.db.orders.insert_one(pending_doc)
        logger.info(f"create_pending_order: created pending order {order_id} for user {telegram_id} (upi_id_shown={upi_id_shown})")
        return {"success": True, "message": "pending_order_created", "order_id": order_id}

    except Exception as e:
        # Never raise \u2014 this is a best-effort tracking call
        logger.error(f"create_pending_order error: {e}", exc_info=True)
        return {"success": True, "message": "error_ignored"}'''

NEW_PENDING = '''# ==========================================
# Generate Order ID endpoint (server-side)
# ==========================================

@api_router.post("/generate-order-id")
async def generate_order_id_endpoint(payload: dict):
    """
    Returns a fresh, unique, properly formatted Arya order ID.
    Frontend should call this BEFORE showing the UPI QR page,
    then pass the returned order_id to create-pending-order and verify-upi-utr.
    This ensures ALL payment attempts (paid, failed, pending) get a traceable new-format ID.
    """
    telegram_id = payload.get("telegram_id")
    story_ids   = payload.get("story_ids", [])
    source      = str(payload.get("source", "miniapp")).lower()

    if not telegram_id:
        raise HTTPException(400, "telegram_id required")

    arya_db = getattr(app.state, "db", None)
    if not arya_db:
        raise HTTPException(500, "DB not available")

    order_id = await _make_arya_order_id(arya_db, str(telegram_id), story_ids, source=source)
    logger.info(f"[OrderID] Generated: {order_id} for user {telegram_id}")
    return {"success": True, "order_id": order_id}


# ==========================================
# Create Pending UPI Order (QR Page Mount)
# ==========================================

@api_router.post("/create-pending-order")
async def create_pending_order(payload: dict):
    """
    Called the instant the QR payment screen is shown to the user \u2014 BEFORE they pay.
    Creates a status="pending" order record so every payment attempt is traceable
    by order_id, even if the user pays to the wrong UPI ID or UTR verification fails.

    - If no order_id is provided OR it's in old legacy OD_ format, a new one is auto-generated.
    - Idempotent: if order_id already exists in DB, returns the existing record's order_id.
    """
    telegram_id   = payload.get("telegram_id")
    story_ids     = payload.get("story_ids", [])
    order_id      = str(payload.get("order_id", "")).strip()
    amount        = float(payload.get("amount", 0) or 0)
    upi_id_shown  = str(payload.get("upi_id_shown", "")).strip()
    promo_code    = str(payload.get("promo_code", "")).strip().upper()
    username      = str(payload.get("username", "")).strip()
    first_name    = str(payload.get("first_name", "")).strip()
    last_name     = str(payload.get("last_name", "")).strip()

    if not telegram_id:
        logger.warning("create_pending_order: missing telegram_id \u2014 skipping")
        return {"success": True, "message": "skipped"}

    db = getattr(app.state, "db", None)
    if not db:
        logger.error("create_pending_order: DB not available")
        return {"success": True, "message": "db_unavailable"}

    try:
        # If no order_id or it's in old OD_ legacy format, generate a new structured one server-side
        is_legacy = not order_id or order_id.startswith("OD_") or order_id.startswith("OD-")
        if is_legacy:
            order_id = await _make_arya_order_id(db, str(telegram_id), story_ids, source="miniapp")
            logger.info(f"create_pending_order: generated new order_id={order_id} for user {telegram_id}")

        # Idempotency: don't create duplicate if order_id already recorded
        existing = await db.db.orders.find_one({"order_id": order_id})
        if existing:
            logger.info(f"create_pending_order: order {order_id} already exists (status={existing.get('status')}) \u2014 returning")
            return {"success": True, "message": "already_exists", "order_id": order_id}

        tg_id_int = int(telegram_id) if str(telegram_id).isdigit() else 0

        # Resolve story names from DB for richer admin view
        story_names = []
        if story_ids:
            from bson.objectid import ObjectId
            for sid in story_ids:
                try:
                    doc = await db.db.premium_stories.find_one({"_id": ObjectId(sid)})
                    if doc:
                        story_names.append(doc.get("story_name_en", doc.get("title", sid)))
                except Exception:
                    story_names.append(sid)

        pending_doc = {
            "order_id":       order_id,
            "user_id":        tg_id_int if tg_id_int else telegram_id,
            "username":       username,
            "first_name":     first_name,
            "last_name":      last_name,
            "story_ids":      story_ids,
            "story_names":    story_names,
            "total":          amount,
            "promo_code":     promo_code if promo_code else None,
            "status":         "pending",
            "source":         "upi_manual_miniapp",
            "upi_id_shown":   upi_id_shown,
            "created_at":     datetime.now(timezone.utc),
        }
        await db.db.orders.insert_one(pending_doc)
        logger.info(f"create_pending_order: created pending order {order_id} for user {telegram_id}")
        return {"success": True, "message": "pending_order_created", "order_id": order_id}

    except Exception as e:
        logger.error(f"create_pending_order error: {e}", exc_info=True)
        return {"success": True, "message": "error_ignored", "order_id": order_id}'''


# ── Also fix MANUAL_ order ID in admin manual_purchase ─────────────────────────────────────────

OLD_MANUAL = '            "order_id": f"MANUAL_{target_uid}_{int(datetime.now().timestamp())}",'
NEW_MANUAL = '            "order_id": await _make_arya_order_id(arya_db, str(target_uid), [story_id_str], source=source_label),'

# Old: separate line, so we need to handle:
OLD_MANUAL_FULL = '''        # Insert Order with explicit source
        order_doc = {
            "order_id": f"MANUAL_{target_uid}_{int(datetime.now().timestamp())}",'''
NEW_MANUAL_FULL = '''        # Insert Order with explicit source
        manual_oid = await _make_arya_order_id(arya_db, str(target_uid), [story_id_str], source=source_label)
        order_doc = {
            "order_id": manual_oid,'''


def patch_file(filename, patches):
    with open(filename, 'r', encoding='utf-8') as f:
        content = f.read()

    for i, (old, new, label) in enumerate(patches, 1):
        if old in content:
            content = content.replace(old, new, 1)
            print(f"  [{filename}] Patch {i} ({label}): OK")
        else:
            print(f"  [{filename}] Patch {i} ({label}): NOT FOUND — skipping")

    with open(filename, 'w', encoding='utf-8') as f:
        f.write(content)


# Apply patches to both API files
for fname in FILES:
    print(f"\nPatching {fname}...")
    patch_file(fname, [
        (OLD_FUNC, NEW_FUNC, "_make_arya_order_id ReturnDocument.AFTER fix"),
    ])

# Main file only: generate-order-id + updated create-pending-order
print(f"\nPatching mini_app_api.py (endpoint changes)...")
patch_file("mini_app_api.py", [
    (OLD_PENDING, NEW_PENDING, "generate-order-id + create-pending-order update"),
    (OLD_MANUAL_FULL, NEW_MANUAL_FULL, "manual purchase order ID"),
])

print("\nAll patches done!")
