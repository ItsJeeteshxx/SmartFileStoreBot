"""
Arya Premium — enterprise analytics aggregations + realtime hub.
Reads from existing MongoDB collections (mini_app_analytics, story_events, users, orders, premium_stories).
Optional PostgreSQL/Supabase tables: see supabase/migrations/001_arya_analytics.sql
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from bson import ObjectId
from bson.errors import InvalidId

logger = logging.getLogger("arya_enterprise_analytics")

# Approximate centroids for map pulses (country-level)
COUNTRY_CENTROIDS: dict[str, tuple[float, float]] = {
    "India": (20.59, 78.96),
    "United States": (37.09, -95.71),
    "United Kingdom": (55.38, -3.44),
    "Canada": (56.13, -106.35),
    "Australia": (-25.27, 133.78),
    "Germany": (51.16, 10.45),
    "France": (46.23, 2.21),
    "Nepal": (28.39, 84.12),
    "Bangladesh": (23.68, 90.36),
    "Pakistan": (30.38, 69.35),
    "United Arab Emirates": (23.42, 53.85),
    "Singapore": (1.35, 103.82),
    "Malaysia": (4.21, 101.98),
    "Saudi Arabia": (23.89, 45.08),
    "Japan": (36.20, 138.25),
    "Brazil": (-14.24, -51.93),
    "Nigeria": (9.08, 8.68),
    "Unknown": (0, 0),
}

# Fallback map pins when events have no map_lat/map_lng (India: state/region centroid)
INDIA_REGION_COORDS: dict[str, tuple[float, float]] = {
    "madhya pradesh": (22.9734, 78.6569),
    "maharashtra": (19.7515, 75.7139),
    "uttar pradesh": (26.8467, 80.9462),
    "gujarat": (22.2587, 71.1924),
    "karnataka": (15.3173, 75.7139),
    "rajasthan": (27.0238, 74.2179),
    "telangana": (18.1124, 79.0193),
    "tamil nadu": (11.1271, 78.6569),
    "west bengal": (22.9868, 87.8550),
    "bihar": (25.0961, 85.3131),
    "punjab": (31.1471, 75.3412),
    "haryana": (29.0588, 76.0856),
    "delhi": (28.7041, 77.1025),
    "uttarakhand": (30.0668, 79.0193),
    "himachal pradesh": (31.1048, 77.1734),
    "jharkhand": (23.6102, 85.2799),
    "chhattisgarh": (21.2787, 81.8661),
    "odisha": (20.9517, 85.0985),
    "assam": (26.2006, 92.9376),
    "kerala": (10.8505, 76.2711),
    "andhra pradesh": (15.9129, 79.7400),
    "goa": (15.2993, 74.1240),
}


def _map_pin_coords(country: str, region: str, city: str, mlat: Any, mlng: Any) -> tuple[float, float]:
    try:
        la = float(mlat) if mlat is not None else None
        lo = float(mlng) if mlng is not None else None
    except (TypeError, ValueError):
        la, lo = None, None
    if la is not None and lo is not None and -90 <= la <= 90 and -180 <= lo <= 180:
        return la, lo
    reg = (region or "").strip().lower()
    cname = (country or "").strip()
    if cname == "India" and reg:
        for key, coords in INDIA_REGION_COORDS.items():
            if key in reg or reg in key:
                return coords
    return COUNTRY_CENTROIDS.get(cname, COUNTRY_CENTROIDS["Unknown"])


@dataclass
class AnalyticsFilters:
    days: int = 30
    query: Optional[str] = None
    telegram_only: bool = False
    premium_only: bool = False
    new_users: bool = False
    returning_users: bool = False

    def match_stage(self) -> dict[str, Any]:
        since = datetime.now(timezone.utc) - timedelta(days=max(1, min(self.days, 365)))
        parts: list[dict[str, Any]] = [
            {"timestamp": {"$gte": since}},
            {
                "data.client_user_agent": {
                    "$not": {
                        "$regex": "bot|crawler|spider|ping|uptime|status|http|curl|wget|python|node|axios|fetch|headless|selenium|puppeteer|playwright|scrape|scan|checker",
                        "$options": "i"
                    }
                }
            }
        ]
        if self.query:
            q = self.query.strip()
            parts.append({
                "$or": [
                    {"country": {"$regex": q, "$options": "i"}},
                    {"city": {"$regex": q, "$options": "i"}},
                    {"device": {"$regex": q, "$options": "i"}},
                    {"browser": {"$regex": q, "$options": "i"}},
                    {"story_id": {"$regex": q, "$options": "i"}},
                    {"data.story_id": {"$regex": q, "$options": "i"}},
                    {"page": {"$regex": q, "$options": "i"}},
                    {"data.page": {"$regex": q, "$options": "i"}},
                ]
            })
        if self.telegram_only:
            parts.append({"browser": "Telegram"})
        if len(parts) == 1:
            return parts[0]
        return {"$and": parts}


class AnalyticsHub:
    """In-memory WebSocket fan-out for live analytics (scale-out: replace with Redis pub/sub)."""

    def __init__(self) -> None:
        self._clients: set[Any] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket) -> None:
        async with self._lock:
            self._clients.add(websocket)

    async def disconnect(self, websocket) -> None:
        async with self._lock:
            self._clients.discard(websocket)

    async def broadcast(self, payload: dict) -> None:
        text = json.dumps(payload, default=str)
        dead = []
        async with self._lock:
            clients = list(self._clients)
        for ws in clients:
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)


hub = AnalyticsHub()


def _iso(dt: Any) -> str:
    if isinstance(dt, datetime):
        try:
            import pytz
            ist = pytz.timezone('Asia/Kolkata')
            return dt.astimezone(ist).isoformat()
        except ImportError:
            # Fallback if pytz is not installed, though it usually is
            ist_offset = timezone(timedelta(hours=5, minutes=30))
            return dt.astimezone(ist_offset).isoformat()
    return str(dt or "")


def _live_row_summary(doc: dict) -> str:
    t = str(doc.get("type") or "")
    ed = doc.get("data") or {} if isinstance(doc.get("data"), dict) else {}
    page = str(doc.get("page") or ed.get("page") or "").strip()
    story = str(doc.get("story_id") or ed.get("story_id") or "").strip()
    ch = str(doc.get("chapter_id") or ed.get("chapter_id") or "").strip()
    if t == "page_view":
        return f"Enter · {page}" if page else "App"
    if t == "view_story":
        return f"Story · {story}" if story else "Story"
    if t == "session_duration":
        return "Session"
    if t == "search":
        q = str(ed.get("query") or "").strip()
        return f"Search · {q[:40]}" if q else "Search"
    if t.startswith("checkout_"):
        return t.replace("_", " ").title()
    if ch:
        return f"{t} · ch {ch}"
    return t.replace("_", " ").title() if t else "Event"


async def _safe_agg(coll, pipeline: list, default: list | dict | None = None):
    try:
        cur = coll.aggregate(pipeline)
        return await cur.to_list(length=None)
    except Exception as e:
        logger.warning("aggregate failed: %s", e)
        return default if default is not None else []


def visitor_id_expression() -> dict[str, Any]:
    """
    Returns the MongoDB aggregation expression to compute visitor_id following this priority:
    1. telegram_user_id (if not None, not 0, not "0", not "null", not "undefined")
    2. session_id (if not None, not "", not "null", not "undefined")
    3. fingerprint_id (if not None, not "", not "null", not "undefined")
    4. IP fallback
    """
    is_valid_user = {
        "$and": [
            {"$ne": ["$user_id", None]},
            {"$ne": ["$user_id", 0]},
            {"$ne": ["$user_id", "0"]},
            {"$ne": ["$user_id", "null"]},
            {"$ne": ["$user_id", "undefined"]}
        ]
    }
    
    is_valid_session = {
        "$and": [
            {"$ne": ["$session_id", None]},
            {"$ne": ["$session_id", ""]},
            {"$ne": ["$session_id", "null"]},
            {"$ne": ["$session_id", "undefined"]}
        ]
    }

    is_valid_fp = {
        "$and": [
            {"$ne": ["$fingerprint_id", None]},
            {"$ne": ["$fingerprint_id", ""]},
            {"$ne": ["$fingerprint_id", "null"]},
            {"$ne": ["$fingerprint_id", "undefined"]}
        ]
    }

    return {
        "$cond": [
            is_valid_user,
            {"$concat": ["user_", {"$toString": "$user_id"}]},
            {
                "$cond": [
                    is_valid_session,
                    {"$concat": ["sess_", "$session_id"]},
                    {
                        "$cond": [
                            is_valid_fp,
                            {"$concat": ["fp_", "$fingerprint_id"]},
                            {"$concat": ["ip_", {"$ifNull": ["$ip", "unknown"]}]}
                        ]
                    }
                ]
            }
        ]
    }


async def build_enterprise_dashboard(db, flt: AnalyticsFilters) -> dict[str, Any]:
    """Single payload for the Next.js enterprise analytics page."""
    m = flt.match_stage()
    activity_m = {**m, "type": {"$nin": ["session_duration", "heartbeat", "console_error", "performance", "perf", "web_vitals"]}}
    if "$and" in m:
        since = m["$and"][0]["timestamp"]["$gte"]
    else:
        since = m["timestamp"]["$gte"]

    # m already includes the timestamp filter and the bot_filter

    arya_db = db
    analytics = arya_db.db.mini_app_analytics
    story_events = arya_db.db.story_events
    orders = arya_db.db.orders
    stories = arya_db.db.premium_stories

    # ── Hero metrics (parallel I/O) ──
    active_cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)
    ret_pipeline = [
        {"$match": m},
        {"$project": {
            "visitor_id": visitor_id_expression(),
            "timestamp": 1
        }},
        {"$group": {"_id": "$visitor_id", "days": {"$addToSet": {"$dateToString": {"format": "%Y-%m-%d", "date": "$timestamp", "timezone": "Asia/Kolkata"}}}}},
        {"$project": {"_id": 1, "n": {"$size": "$days"}}},
        {"$match": {"n": {"$gte": 2}}},
        {"$count": "c"},
    ]
    rev_mini_pipeline = [
        {"$match": {"status": "paid"}},
        {"$group": {"_id": None, "t": {"$sum": {"$ifNull": ["$total_amount", {"$ifNull": ["$total", 0]}]}}}},
    ]
    bot_filter_all_time = {
        "data.client_user_agent": {
            "$not": {
                "$regex": "bot|crawler|spider|ping|uptime|status|http|curl|wget|python|node|axios|fetch|headless|selenium|puppeteer|playwright|scrape|scan|checker",
                "$options": "i"
            }
        }
    }
    total_users_pipeline = [
        {"$match": {"type": "page_view", **bot_filter_all_time}},
        {"$project": {"visitor_id": visitor_id_expression()}},
        {"$group": {"_id": "$visitor_id"}},
        {"$count": "c"}
    ]
    users_coll = arya_db.db.users
    new_users_window_pipeline = [
        {"$match": {"joined_date": {"$gte": since}}},
        {"$count": "c"},
    ]
    premium_mini_pipeline = [
        {"$match": {"user_id": {"$gt": 0}}},
        {"$group": {"_id": "$user_id"}},
        {"$lookup": {"from": "users", "localField": "_id", "foreignField": "id", "as": "u"}},
        {"$match": {"u": {"$elemMatch": {"purchases": {"$exists": True, "$ne": []}}}}},
        {"$count": "c"},
    ]
    # Session length from explicit session_duration events (see mini_app_api track_event).
    session_match = {**m, "type": "session_duration"}
    sess_pipeline = [
        {"$match": session_match},
        {"$group": {"_id": None, "avg": {"$avg": "$data.duration"}, "n": {"$sum": 1}}},
    ]

    visitor_pipeline = [
        {"$match": m},
        {"$project": {
            "visitor_id": visitor_id_expression()
        }},
        {"$group": {"_id": "$visitor_id"}},
        {"$count": "c"}
    ]

    active_now_pipeline = [
        {"$match": {"timestamp": {"$gte": active_cutoff}, "user_id": {"$gt": 0}}},
        {"$group": {"_id": "$user_id"}},
        {"$count": "c"}
    ]

    (
        total_users_row,
        new_users_row,
        premium_mini_row,
        visitor_row,
        active_now_row,
        ret,
        mini_paid,
        rev_mini,
        total_events_n,
        sess,
    ) = await asyncio.gather(
        _safe_agg(analytics, total_users_pipeline, []),
        _safe_agg(users_coll, new_users_window_pipeline, []),
        _safe_agg(analytics, premium_mini_pipeline, []),
        _safe_agg(analytics, visitor_pipeline, []),
        _safe_agg(analytics, active_now_pipeline, []),
        _safe_agg(analytics, ret_pipeline, []),
        orders.count_documents({"status": {"$in": ["paid", "delivered"]}}),
        _safe_agg(orders, rev_mini_pipeline, []),
        analytics.count_documents(activity_m),
        _safe_agg(analytics, sess_pipeline, []),
    )
    total_users = int(total_users_row[0]["c"]) if total_users_row else 0
    new_users_in_window = int(new_users_row[0]["c"]) if new_users_row else 0
    premium_users = int(premium_mini_row[0]["c"]) if premium_mini_row else 0
    n_distinct = int(visitor_row[0]["c"]) if visitor_row else 0
    active_now = int(active_now_row[0]["c"]) if active_now_row else 0
    returning_count = ret[0]["c"] if ret else 0

    mini_rev = 0.0
    if rev_mini and rev_mini[0].get("t") is not None:
        try:
            mini_rev = float(rev_mini[0]["t"])
        except (ValueError, TypeError):
            pass
    bot_rev = 0.0
    total_revenue = mini_rev

    premium_conversion = (premium_users / total_users * 100) if total_users else 0.0

    avg_session = float(sess[0]["avg"]) if sess and sess[0].get("avg") else 0.0
    session_events = int(sess[0]["n"]) if sess else 0
    engagement_events_per_user = round(total_events_n / n_distinct, 2) if n_distinct else 0.0

    hero = {
        "scope": "mini_app",
        "total_users": total_users,
        "new_users_in_window": new_users_in_window,
        "unique_in_window": n_distinct,
        "active_now": active_now,
        "total_revenue": round(total_revenue, 2),
        "miniapp_revenue": round(mini_rev, 2),
        "bot_revenue": 0.0,
        "sessions_tracked": session_events,
        "avg_session_seconds": round(avg_session, 1),
        "returning_in_window": returning_count,
        "premium_users": premium_users,
        "premium_conversion_pct": round(premium_conversion, 2),
        "orders_paid_or_delivered": mini_paid,
        "engagement_events_per_user": engagement_events_per_user,
        "retention_pct": round(returning_count / n_distinct * 100, 2) if n_distinct else 0.0,
    }

    story_since = since
    se_match = {
        **m,
        "type": "view_story",
        "story_id": {"$exists": True, "$nin": [None, "", "Unknown"]}
    }
    if getattr(flt, "story_id", None):
        se_match["story_id"] = flt.story_id

    async def top_field(field: str, limit: int = 8):
        p = [
            {"$match": {**activity_m, field: {"$exists": True, "$nin": [None, "", "Unknown", "unknown", "null", "undefined", "—"]}}},
            # Group by visitor_id first to get unique users per field value
            {"$group": {
                "_id": {
                    "visitor_id": visitor_id_expression(),
                    "val": f"${field}"
                }
            }},
            # Group by field value and count unique visitors
            {"$group": {"_id": "$_id.val", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": limit},
        ]
        rows = await _safe_agg(analytics, p, [])
        return [{"name": r["_id"], "value": r["count"]} for r in rows]

    map_pipeline = [
        {"$match": activity_m},
        {"$group": {
            "_id": {
                "visitor_id": visitor_id_expression(),
                "c": "$country",
                "region": "$region",
                "city": "$city"
            },
            "lat": {"$avg": "$map_lat"},
            "lng": {"$avg": "$map_lng"}
        }},
        {"$group": {
            "_id": {
                "c": {
                    "$cond": [
                        {"$in": ["$_id.c", [None, "", "Unknown", "unknown", "null", "undefined", "—"]]},
                        "Unknown",
                        "$_id.c"
                    ]
                },
                "region": {
                    "$cond": [
                        {"$in": ["$_id.region", [None, "", "Unknown", "unknown", "null", "undefined", "—"]]},
                        "",
                        "$_id.region"
                    ]
                },
                "city": {
                    "$cond": [
                        {"$in": ["$_id.city", [None, "", "Unknown", "unknown", "null", "undefined", "—"]]},
                        "",
                        "$_id.city"
                    ]
                }
            },
            "count": {"$sum": 1},
            "mlat": {"$avg": "$lat"},
            "mlng": {"$avg": "$lng"}
        }},
        {"$sort": {"count": -1}},
        {"$limit": 60},
    ]
    hourly_pipeline = [
        {"$match": activity_m},
        {"$group": {
            "_id": {
                "h": {"$hour": {"date": "$timestamp", "timezone": "Asia/Kolkata"}},
                "visitor_id": visitor_id_expression()
            }
        }},
        {"$group": {"_id": "$_id.h", "count": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]
    heatmap_pipeline = [
        {"$match": activity_m},
        {"$group": {
            "_id": {
                "d": {"$dayOfWeek": {"date": "$timestamp", "timezone": "Asia/Kolkata"}},
                "h": {"$hour": {"date": "$timestamp", "timezone": "Asia/Kolkata"}},
                "visitor_id": visitor_id_expression()
            }
        }},
        {"$group": {
            "_id": {"d": "$_id.d", "h": "$_id.h"},
            "count": {"$sum": 1}
        }}
    ]
    twv_pipeline = [
        {"$match": {**activity_m, "browser": "Telegram"}},
        {"$project": {
            "visitor_id": visitor_id_expression()
        }},
        {"$group": {"_id": "$visitor_id"}},
        {"$count": "c"}
    ]
    top_viewed_pipeline = [
        {"$match": se_match},
        {"$group": {"_id": "$story_id", "views": {"$sum": 1}}},
        {"$sort": {"views": -1}},
        {"$limit": 12},
    ]
    search_pipeline = [
        {"$match": {**m, "type": "search", "data.query": {"$exists": True, "$nin": [None, "", "null"]}}},
        {"$group": {"_id": "$data.query", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 20},
    ]
    perf_match = {**m, "type": {"$in": ["performance", "perf", "web_vitals"]}}
    perf_avg_pipeline = [
        {"$match": perf_match},
        {"$group": {
            "_id": None,
            "avg_load": {"$avg": "$load_ms"},
            "avg_api": {"$avg": "$api_ms"},
            "n": {"$sum": 1}
        }},
    ]
    slow_pages_pipeline = [
        {"$match": {**m, "type": "performance", "page": {"$exists": True, "$nin": [None, ""]}}},
        {"$group": {"_id": "$page", "avg": {"$avg": "$load_ms"}}},
        {"$match": {"avg": {"$gte": 2500}}},
        {"$sort": {"avg": -1}},
        {"$limit": 8},
    ]
    peak_pipeline = [
        {"$match": m},
        {"$group": {"_id": {"$hour": {"date": "$timestamp", "timezone": "Asia/Kolkata"}}, "c": {"$sum": 1}}},
        {"$sort": {"c": -1}},
        {"$limit": 1},
    ]
    growth_pipeline = [
        {"$match": {"joined_date": {"$gte": since}}},
        {"$group": {"_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$joined_date", "timezone": "Asia/Kolkata"}}, "n": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]
    j_pipeline = [
        {"$match": {**m, "type": "page_view", "data.page": {"$exists": True}}},
        {"$group": {"_id": "$data.page", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 8},
    ]
    genre_pipeline = [
        {"$match": {}},
        {"$group": {"_id": "$genre", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 10},
    ]
    se_counts_pipeline = [
        {"$match": se_match},
        {"$group": {"_id": "$event", "c": {"$sum": 1}}},
    ]

    (
        live_docs,
        map_rows,
        hourly_raw,
        hm,
        top_viewed,
        browsers,
        devices,
        os_list,
        countries,
        cities,
        states_top,
        twv_rows,
        genres,
        views_count,
        purchase_count,
        top_searches,
        failed_search,
        perf_avg,
        slow_pages,
        console_errors_n,
        peak,
        growth,
        pages,
        rage_n,
        dead_n,
        scroll_n,
        mini_opens_n,
        tg_premium_n,
    ) = await asyncio.gather(
        analytics.find({**m}).sort("timestamp", -1).limit(500).to_list(500),
        _safe_agg(analytics, map_pipeline, []),
        _safe_agg(analytics, hourly_pipeline, []),
        _safe_agg(analytics, heatmap_pipeline, []),
        _safe_agg(analytics, top_viewed_pipeline, []),
        top_field("browser", 10),
        top_field("device", 8),
        top_field("os", 8),
        top_field("country", 15),
        top_field("city", 15),
        top_field("region", 12),
        _safe_agg(analytics, twv_pipeline, []),
        _safe_agg(stories, genre_pipeline, []),
        analytics.count_documents({**m, "type": "view_story"}),
        analytics.count_documents({**m, "type": "checkout_success"}),
        _safe_agg(analytics, search_pipeline, []),
        analytics.count_documents({**m, "type": "search_failed"}),
        _safe_agg(analytics, perf_avg_pipeline, []),
        _safe_agg(analytics, slow_pages_pipeline, []),
        analytics.count_documents({**m, "type": "console_error"}),
        _safe_agg(analytics, peak_pipeline, []),
        _safe_agg(users_coll, growth_pipeline, []),
        _safe_agg(analytics, j_pipeline, []),
        analytics.count_documents({**m, "type": "rage_click"}),
        analytics.count_documents({**m, "type": "dead_click"}),
        analytics.count_documents({**m, "type": "scroll_depth"}),
        analytics.count_documents({**m, "type": "open_app"}),
        analytics.count_documents({**m, "telegram_premium": True}),
    )

    twv = twv_rows[0]["c"] if twv_rows else 0

    live_feed = []
    for doc in live_docs:
        live_feed.append(
            {
                "time": _iso(doc.get("timestamp")),
                "ts": _iso(doc.get("timestamp")),
                "summary": _live_row_summary(doc),
                "type": doc.get("type"),
                "user_id": doc.get("user_id"),
                "country": doc.get("country"),
                "region": doc.get("region"),
                "city": doc.get("city"),
                "geo_source": doc.get("geo_source"),
                "lat": doc.get("map_lat"),
                "lng": doc.get("map_lng"),
                "device": doc.get("device"),
                "browser": doc.get("browser"),
                "os": doc.get("os"),
                "referrer": doc.get("referrer"),
                "story_id": doc.get("story_id") or (doc.get("data") or {}).get("story_id"),
                "page": doc.get("page") or (doc.get("data") or {}).get("page"),
            }
        )

    map_points = []
    for row in map_rows:
        cid = row.get("_id") or {}
        cname = cid.get("c") or "Unknown"
        city = (cid.get("city") or "").strip()
        region = (cid.get("region") or "").strip()
        lat, lon = _map_pin_coords(cname, region, city, row.get("mlat"), row.get("mlng"))
        if lat == 0 and lon == 0:
            continue
        map_points.append(
            {
                "country": cname,
                "region": region,
                "city": city or "—",
                "count": row["count"],
                "lat": lat,
                "lon": lon,
            }
        )

    hourly_map = {h: 0 for h in range(24)}
    for r in hourly_raw:
        h = r["_id"]
        if h is not None and isinstance(h, int) and 0 <= h < 24:
            hourly_map[h] = r["count"]
    hourly = [{"hour": f"{h:02d}:00", "events": hourly_map[h]} for h in range(24)]

    heatmap = []
    for x in hm:
        if isinstance(x.get("_id"), dict):
            d = x["_id"].get("d")
            h = x["_id"].get("h")
            if d is not None and h is not None:
                heatmap.append({"day": d - 1, "hour": h, "count": x["count"]})
    mobile = sum(d["value"] for d in devices if str(d["name"]).lower() in ("mobile", "tablet"))
    desktop = sum(d["value"] for d in devices if str(d["name"]).lower() == "desktop")

    async def _story_row(sid: str, views: int) -> dict[str, Any]:
        st = None
        if len(sid) == 24:
            try:
                st = await stories.find_one({"_id": ObjectId(sid)})
            except InvalidId:
                st = None
        if st is None:
            st = await stories.find_one({"story_id": sid})
        title = sid
        if st:
            title = st.get("story_name_en") or st.get("story_name_hi") or st.get("title") or sid
        return {"story_id": sid, "title": title, "views": views}

    top_viewed_fmt = (
        list(await asyncio.gather(*[_story_row(str(r["_id"]), int(r["views"])) for r in top_viewed]))
        if top_viewed
        else []
    )

    views_n = int(views_count)
    purch_n = int(purchase_count)
    completion_proxy = (purch_n / views_n * 100) if views_n else 0.0

    stories_section = {
        "top_viewed": top_viewed_fmt,
        "genres": [{"name": g["_id"] or "Unknown", "count": g["count"]} for g in genres],
        "avg_listen_proxy_sec": round(avg_session, 1),
        "story_completion_proxy_pct": round(min(100, completion_proxy), 2),
        "dropoff_proxy_pct": round(max(0, 100 - completion_proxy), 2) if views_n else 0,
        "replay_rate_pct": None,
    }

    performance = {
        "samples": int(perf_avg[0]["n"]) if perf_avg else 0,
        "avg_page_load_ms": round(float(perf_avg[0]["avg_load"]), 1) if perf_avg and perf_avg[0].get("avg_load") else None,
        "avg_api_ms": round(float(perf_avg[0]["avg_api"]), 1) if perf_avg and perf_avg[0].get("avg_api") else None,
        "slow_pages": slow_pages,
        "console_errors": int(console_errors_n),
    }

    peak_hour = peak[0]["_id"] if peak else None

    journey = {
        "nodes": [{"id": p["_id"], "count": p["count"]} for p in pages],
        "flow_note": "Emit event_data.nav_path from the client for sequence analytics.",
    }

    behavior = {
        "rage_clicks": int(rage_n),
        "dead_clicks": int(dead_n),
        "scroll_samples": int(scroll_n),
        "bounce_rate_proxy": None,
    }

    telegram_section = {
        "mini_app_opens": int(mini_opens_n),
        "telegram_browser_sessions": twv,
        "telegram_premium_flagged": int(tg_premium_n),
    }

    chapter_pipeline = [
        {"$match": {**m, "chapter_id": {"$exists": True, "$nin": [None, ""]}}},
        {"$group": {"_id": "$chapter_id", "c": {"$sum": 1}}},
        {"$sort": {"c": -1}},
        {"$limit": 15},
    ]
    click_xy_pipeline = [
        {"$match": {**m, "data.x": {"$exists": True}, "data.y": {"$exists": True}}},
        {"$project": {"x": "$data.x", "y": "$data.y"}},
        {"$limit": 400},
    ]
    ref_pipeline = [
        {"$match": {**activity_m, "referrer": {"$exists": True, "$nin": [None, "", "Unknown", "unknown", "null", "undefined", "—"]}}},
        {"$group": {
            "_id": {
                "visitor_id": visitor_id_expression(),
                "ref": "$referrer"
            }
        }},
        {"$group": {"_id": "$_id.ref", "c": {"$sum": 1}}},
        {"$sort": {"c": -1}},
        {"$limit": 12},
    ]

    (
        co_checkout_pv,
        co_view,
        co_pay,
        co_ok,
        co_err,
        click_log_docs,
        ch_rows,
        click_xy_rows,
        ref_rows,
    ) = await asyncio.gather(
        analytics.count_documents({**m, "type": "page_view", "data.page": "checkout"}),
        analytics.count_documents({**m, "type": "checkout_view"}),
        analytics.count_documents({**m, "type": "checkout_pay_start"}),
        analytics.count_documents({**m, "type": "checkout_success"}),
        analytics.count_documents({**m, "type": "checkout_error"}),
        analytics.find(
            {
                **m,
                "type": {
                    "$in": [
                        "checkout_view",
                        "checkout_pay_start",
                        "checkout_success",
                        "checkout_error",
                        "click",
                        "button_click",
                        "search_failed",
                    ]
                },
            }
        )
        .sort("timestamp", -1)
        .limit(80)
        .to_list(80),
        _safe_agg(analytics, chapter_pipeline, []),
        _safe_agg(analytics, click_xy_pipeline, []),
        _safe_agg(analytics, ref_pipeline, []),
    )

    opened_checkout = int(co_checkout_pv + co_view)
    checkout_funnel = {
        "opened_checkout": opened_checkout,
        "pay_started": int(co_pay),
        "completed": int(co_ok),
        "failed": int(co_err),
        "abandoned": max(0, opened_checkout - int(co_ok)),
        "conversion_pct": round((int(co_ok) / opened_checkout * 100), 2) if opened_checkout else 0.0,
    }

    click_logs_fmt = []
    for d in click_log_docs:
        ed = d.get("data") or {}
        click_logs_fmt.append(
            {
                "ts": _iso(d.get("timestamp")),
                "type": d.get("type"),
                "user_id": d.get("user_id"),
                "page": d.get("page") or ed.get("page"),
                "story_id": d.get("story_id") or ed.get("story_id"),
                "target": d.get("click_target") or ed.get("click_target") or ed.get("element"),
            }
        )

    chapter_top = [{"chapter_id": str(r["_id"]), "events": int(r["c"])} for r in ch_rows]
    click_points = [{"x": float(r["x"]), "y": float(r["y"])} for r in click_xy_rows if r.get("x") is not None and r.get("y") is not None]
    traffic_sources = [{"name": str(r["_id"]), "value": int(r["c"])} for r in ref_rows]

    retention_block = {
        "returning_users": returning_count,
        "users_in_window": n_distinct,
        "new_users_in_window": new_users_in_window,
        "retention_pct": round((returning_count / n_distinct * 100), 2) if n_distinct else 0.0,
    }

    stories_section["chapter_top"] = chapter_top

    return {
        "success": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {"since": _iso(since), "days": flt.days},
        "hero": hero,
        "retention": retention_block,
        "checkout": checkout_funnel,
        "click_logs": click_logs_fmt,
        "click_heatmap": {"points": click_points},
        "traffic": {"sources": traffic_sources},
        "live_feed": live_feed,
        "map_points": map_points,
        "charts": {"hourly_events": hourly},
        "users": {
            "browsers": browsers,
            "devices": devices,
            "operating_systems": os_list,
            "mobile_events": mobile,
            "desktop_events": desktop,
            "telegram_webview_events": twv,
        },
        "geo": {
            "countries": countries,
            "cities": cities,
            "heatmap": heatmap,
            "states": states_top,
        },
        "stories": stories_section,
        "search": {
            "top": [{"query": (t["_id"] or "").strip() or "(empty)", "count": t["count"]} for t in top_searches if t["_id"] is not None],
            "failed_searches": failed_search,
            "trending": [
                {"query": (t["_id"] or "").strip() or "(empty)", "count": t["count"]}
                for t in (top_searches[:12] if top_searches else [])
                if t.get("_id") is not None
            ],
        },
        "performance": performance,
        "telegram": telegram_section,
        "behavior": behavior,
        "journey": journey,
        "session_replay": {
            "enabled": False,
            "message": "Reserved for rrweb-based session replay ingestion.",
        },
        "intelligence": {
            "peak_traffic_hour_utc": peak_hour,
            "user_growth_by_day": [{"date": g["_id"], "new_users": g["n"]} for g in growth],
            "retention_returning_in_window": returning_count,
        },
        "filters_echo": {
            "query": flt.query,
            "telegram_only": flt.telegram_only,
            "premium_only": flt.premium_only,
            "new_users": flt.new_users,
            "returning_users": flt.returning_users,
        },
    }


def filters_from_query(
    days: int = 30,
    query: Optional[str] = None,
    telegram_only: bool = False,
    premium_only: bool = False,
    new_users: bool = False,
    returning_users: bool = False,
) -> AnalyticsFilters:
    return AnalyticsFilters(
        days=days,
        query=query or None,
        telegram_only=telegram_only,
        premium_only=premium_only,
        new_users=new_users,
        returning_users=returning_users,
    )
