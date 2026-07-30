"""
Paage Backend Engine — Standalone Link-in-Bio REST API Router
Decoupled module for profile management, Bento cards, and public bio serving.
"""

from fastapi import APIRouter, HTTPException, Body
from datetime import datetime, timezone
import os

paage_router = APIRouter(prefix="/paage", tags=["Paage"])

@paage_router.get("/public/{username}")
async def get_paage_public_profile(username: str):
    clean_username = username.lstrip("@").lower().strip()
    
    default_profile = {
        "username": clean_username,
        "display_name": clean_username.capitalize(),
        "bio": "Creator, builder, explorer.",
        "avatar_url": "https://images.unsplash.com/photo-1534528741775-53994a69daeb?w=400&auto=format&fit=crop&q=80",
        "theme": "default",
        "socials": {
            "github": f"https://github.com/{clean_username}",
            "twitter": f"https://x.com/{clean_username}"
        }
    }
    
    default_cards = [
        {
            "id": "card-1",
            "type": "link",
            "title": "SliceURL — Shorten & Monetize",
            "url": "https://sliceurl.app/",
            "content": "Supercharge your links with intelligent analytics.",
            "grid_span": "col-span-2",
            "order_index": 1
        },
        {
            "id": "card-2",
            "type": "note",
            "title": "Welcome to Paage",
            "content": "Create your own customizable Bento grid profile in seconds.",
            "grid_span": "col-span-1",
            "order_index": 2
        },
        {
            "id": "card-3",
            "type": "video",
            "title": "Featured Stream",
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "grid_span": "col-span-1",
            "order_index": 3
        }
    ]

    try:
        from database import db
        prof = await db.paage_profiles.find_one({"username": clean_username}, {"_id": 0})
        if not prof:
            return {"ok": True, "profile": default_profile, "cards": default_cards}
        
        cards = await db.paage_cards.find({"profile_id": clean_username}).sort("order_index", 1).to_list(length=100)
        clean_cards = []
        for c in cards:
            c.pop("_id", None)
            clean_cards.append(c)

        return {"ok": True, "profile": prof, "cards": clean_cards or default_cards}
    except Exception:
        return {"ok": True, "profile": default_profile, "cards": default_cards}


@paage_router.post("/profile")
async def save_paage_profile(payload: dict = Body(...)):
    username = payload.get("username", "").lstrip("@").lower().strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")

    profile_data = {
        "username": username,
        "display_name": payload.get("display_name", username),
        "bio": payload.get("bio", ""),
        "avatar_url": payload.get("avatar_url", ""),
        "theme": payload.get("theme", "default"),
        "socials": payload.get("socials", {}),
        "updated_at": datetime.now(timezone.utc).isoformat()
    }

    try:
        from database import db
        await db.paage_profiles.update_one(
            {"username": username},
            {"$set": profile_data},
            upsert=True
        )
    except Exception:
        pass

    return {"ok": True, "profile": profile_data}


@paage_router.post("/cards")
async def save_paage_cards(payload: dict = Body(...)):
    username = payload.get("username", "").lstrip("@").lower().strip()
    cards = payload.get("cards", [])

    try:
        from database import db
        if username:
            await db.paage_cards.delete_many({"profile_id": username})
            if cards:
                for idx, c in enumerate(cards):
                    c["profile_id"] = username
                    c["order_index"] = idx + 1
                await db.paage_cards.insert_many(cards)
    except Exception:
        pass

    return {"ok": True, "count": len(cards)}
