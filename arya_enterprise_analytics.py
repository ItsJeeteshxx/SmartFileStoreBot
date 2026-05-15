"""
Arya Premium — enterprise analytics aggregations + realtime hub.
Reads from existing MongoDB collections (mini_app_analytics, story_events, users, orders, premium_stories).
Optional PostgreSQL/Supabase tables: see supabase/migrations/001_arya_analytics.sql
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
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


@dataclass
class AnalyticsFilters:
    days: int = 30
    country: Optional[str] = None
    city: Optional[str] = None
    story_id: Optional[str] = None
    device: Optional[str] = None
    telegram_only: bool = False
    premium_only: bool = False
    new_users: bool = False
    returning_users: bool = False

    def match_stage(self) -> dict[str, Any]:
        since = datetime.now(timezone.utc) - timedelta(days=max(1, min(self.days, 365)))
        parts: list[dict[str, Any]] = [{"timestamp": {"$gte": since}}]
        if self.country:
            parts.append({"country": {"$regex": self.country, "$options": "i"}})
        if self.city:
            parts.append({"city": {"$regex": self.city, "$options": "i"}})
        if self.story_id:
            parts.append(
                {
                    "$or": [
                        {"data.story_id": self.story_id},
                        {"story_id": self.story_id},
                    ]
                }
            )
        if self.device:
            parts.append({"device": {"$regex": self.device, "$options": "i"}})
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
        return dt.isoformat()
    return str(dt or "")


async def _safe_agg(coll, pipeline: list, default: list | dict | None = None):
    try:
        cur = coll.aggregate(pipeline)
        return await cur.to_list(length=None)
    except Exception as e:
        logger.warning("aggregate failed: %s", e)
        return default if default is not None else []


async def build_enterprise_dashboard(db, flt: AnalyticsFilters) -> dict[str, Any]:
    """Single payload for the Next.js enterprise analytics page."""
    m = flt.match_stage()
    if "$and" in m:
        since = m["$and"][0]["timestamp"]["$gte"]
    else:
        since = m["timestamp"]["$gte"]
    arya_db = db
    analytics = arya_db.db.mini_app_analytics
    story_events = arya_db.db.story_events
    users = arya_db.db.users
    orders = arya_db.db.orders
    stories = arya_db.db.premium_stories
    checkouts = arya_db.db.premium_checkout

    # ── Hero metrics (users / orders span wider than analytics filter for revenue truth) ──
    total_users = await users.count_documents({})
    distinct_analytics_users = await analytics.distinct("user_id", m)
    n_distinct = len([x for x in distinct_analytics_users if x])
    active_cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)
    active_now = len(
        await analytics.distinct("user_id", {"timestamp": {"$gte": active_cutoff}})
    )

    # Returning: users with >1 calendar day of activity in window
    ret_pipeline = [
        {"$match": m},
        {"$group": {"_id": "$user_id", "days": {"$addToSet": {"$dateToString": {"format": "%Y-%m-%d", "date": "$timestamp"}}}}},
        {"$project": {"_id": 1, "n": {"$size": "$days"}}},
        {"$match": {"n": {"$gte": 2}, "_id": {"$ne": None}}},
        {"$count": "c"},
    ]
    ret = await _safe_agg(analytics, ret_pipeline, [])
    returning_count = ret[0]["c"] if ret else 0

    mini_paid = await orders.count_documents({"status": {"$in": ["paid", "delivered"]}})
    bot_appr = await checkouts.count_documents({"status": "approved"})
    rev_mini = await _safe_agg(
        orders,
        [
            {"$match": {"status": "paid"}},
            {"$group": {"_id": None, "t": {"$sum": {"$ifNull": ["$total_amount", {"$ifNull": ["$total", 0]}]}}}},
        ],
        [],
    )
    rev_bot = await _safe_agg(
        checkouts,
        [
            {"$match": {"status": "approved"}},
            {"$group": {"_id": None, "t": {"$sum": {"$toDouble": {"$ifNull": ["$amount", 0]}}}}},
        ],
        [],
    )
    mini_rev = float(rev_mini[0]["t"]) if rev_mini else 0.0
    bot_rev = float(rev_bot[0]["t"]) if rev_bot else 0.0
    total_revenue = mini_rev + bot_rev

    premium_users = await users.count_documents({"purchases": {"$exists": True, "$ne": [], "$not": {"$size": 0}}})
    premium_conversion = (premium_users / total_users * 100) if total_users else 0.0

    session_match = {**m, "type": "session_duration"}
    sess = await _safe_agg(
        analytics,
        [
            {"$match": session_match},
            {"$group": {"_id": None, "avg": {"$avg": "$data.duration"}, "n": {"$sum": 1}}},
        ],
        [],
    )
    avg_session = float(sess[0]["avg"]) if sess and sess[0].get("avg") else 0.0
    session_events = int(sess[0]["n"]) if sess else 0

    hero = {
        "total_users": total_users,
        "unique_in_window": n_distinct,
        "active_now": active_now,
        "total_revenue": round(total_revenue, 2),
        "miniapp_revenue": round(mini_rev, 2),
        "bot_revenue": round(bot_rev, 2),
        "sessions_tracked": session_events,
        "avg_session_seconds": round(avg_session, 1),
        "returning_in_window": returning_count,
        "premium_users": premium_users,
        "premium_conversion_pct": round(premium_conversion, 2),
        "orders_paid_or_delivered": mini_paid + bot_appr,
    }

    # ── Live feed ──
    live_cursor = analytics.find(m).sort("timestamp", -1).limit(80)
    live_feed = []
    async for doc in live_cursor:
        live_feed.append(
            {
                "time": _iso(doc.get("timestamp")),
                "type": doc.get("type"),
                "user_id": doc.get("user_id"),
                "country": doc.get("country"),
                "city": doc.get("city"),
                "device": doc.get("device"),
                "browser": doc.get("browser"),
                "os": doc.get("os"),
                "referrer": doc.get("referrer"),
                "story_id": doc.get("story_id") or (doc.get("data") or {}).get("story_id"),
                "page": doc.get("page") or (doc.get("data") or {}).get("page"),
            }
        )

    # ── Map points (country aggregates + jitter) ──
    map_pipeline = [
        {"$match": m},
        {"$group": {"_id": {"c": "$country", "city": "$city"}, "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 60},
    ]
    map_rows = await _safe_agg(analytics, map_pipeline, [])
    map_points = []
    for row in map_rows:
        cid = row["_id"]
        cname = (cid or {}).get("c") or "Unknown"
        city = (cid or {}).get("city") or ""
        lat, lon = COUNTRY_CENTROIDS.get(cname, COUNTRY_CENTROIDS["Unknown"])
        if lat == 0 and lon == 0:
            continue

        map_points.append(
            {
                "country": cname,
                "city": city,
                "count": row["count"],
                "lat": lat + (random.random() - 0.5) * 4,
                "lon": lon + (random.random() - 0.5) * 4,
            }
        )

    # ── Hourly trend ──
    hourly_pipeline = [
        {"$match": m},
        {"$group": {"_id": {"$hour": "$timestamp"}, "count": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]
    hourly_raw = await _safe_agg(analytics, hourly_pipeline, [])
    hourly = [{"hour": r["_id"], "events": r["count"]} for r in hourly_raw]

    # ── Device / browser / OS ──
    async def top_field(field: str, limit: int = 8):
        p = [
            {"$match": {**m, field: {"$exists": True, "$nin": [None, "", "Unknown"]}}},
            {"$group": {"_id": f"${field}", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": limit},
        ]
        rows = await _safe_agg(analytics, p, [])
        return [{"name": r["_id"], "value": r["count"]} for r in rows]

    browsers, devices, os_list = await asyncio.gather(
        top_field("browser", 10), top_field("device", 8), top_field("os", 8)
    )
    mobile = sum(d["value"] for d in devices if str(d["name"]).lower() in ("mobile", "tablet"))
    desktop = sum(d["value"] for d in devices if str(d["name"]).lower() == "desktop")
    twv = await analytics.count_documents({**m, "browser": "Telegram"})

    # ── Geo ──
    countries = await top_field("country", 15)
    cities = await top_field("city", 15)
    heatmap_pipeline = [
        {"$match": m},
        {"$group": {"_id": {"d": {"$dayOfWeek": "$timestamp"}, "h": {"$hour": "$timestamp"}}, "count": {"$sum": 1}}},
    ]
    hm = await _safe_agg(analytics, heatmap_pipeline, [])
    heatmap = [{"day": x["_id"]["d"] - 1, "hour": x["_id"]["h"], "count": x["count"]} for x in hm]

    # ── Story performance (story_events + premium_stories) ──
    story_since = since
    se_match = {"ts": {"$gte": story_since}}
    if flt.story_id:
        se_match["story_id"] = flt.story_id
    top_viewed = await _safe_agg(
        story_events,
        [
            {"$match": se_match},
            {"$group": {"_id": "$story_id", "views": {"$sum": 1}}},
            {"$sort": {"views": -1}},
            {"$limit": 12},
        ],
        [],
    )
    top_viewed_fmt = []
    for r in top_viewed:
        sid = str(r["_id"])
        st = None
        if len(sid) == 24:
            try:
                st = await stories.find_one({"_id": ObjectId(sid)})
            except InvalidId:
                st = None
        if st is None:
            st = await stories.find_one({"story_id": sid})
        title = None
        if st:
            title = st.get("story_name_en") or st.get("story_name_hi") or st.get("title") or sid
        top_viewed_fmt.append({"story_id": sid, "title": title or sid, "views": r["views"]})

    genre_pipeline = [
        {"$match": {}},
        {"$group": {"_id": "$genre", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 10},
    ]
    genres = await _safe_agg(stories, genre_pipeline, [])

    # Drop-off / completion placeholders from story_events ratio (view vs purchase events)
    se_counts = await _safe_agg(
        story_events,
        [
            {"$match": se_match},
            {"$group": {"_id": "$event", "c": {"$sum": 1}}},
        ],
        [],
    )
    ev_map = {str(x["_id"]): x["c"] for x in se_counts}
    views_n = ev_map.get("view", 0) + ev_map.get("open", 0)
    purch_n = ev_map.get("purchase", 0)
    completion_proxy = (purch_n / views_n * 100) if views_n else 0.0

    stories_section = {
        "top_viewed": top_viewed_fmt,
        "genres": [{"name": g["_id"] or "Unknown", "count": g["count"]} for g in genres],
        "avg_listen_proxy_sec": round(avg_session, 1),
        "story_completion_proxy_pct": round(min(100, completion_proxy), 2),
        "dropoff_proxy_pct": round(max(0, 100 - completion_proxy), 2) if views_n else 0,
        "replay_rate_pct": None,
        "episode_analytics_note": "Wire chapter-level events via /api/track event_data.chapter_id",
    }

    # ── Search ──
    search_pipeline = [
        {"$match": {**m, "type": "search"}},
        {"$group": {"_id": "$data.query", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 20},
    ]
    top_searches = await _safe_agg(analytics, search_pipeline, [])
    failed_search = await analytics.count_documents({**m, "type": "search_failed"})

    # ── Performance (client-sent) ──
    perf_match = {**m, "type": {"$in": ["performance", "perf", "web_vitals"]}}
    perf_avg = await _safe_agg(
        analytics,
        [
            {"$match": perf_match},
            {"$group": {"_id": None, "avg_load": {"$avg": "$data.load_ms"}, "avg_api": {"$avg": "$data.api_ms"}, "n": {"$sum": 1}}},
        ],
        [],
    )
    performance = {
        "samples": int(perf_avg[0]["n"]) if perf_avg else 0,
        "avg_page_load_ms": round(float(perf_avg[0]["avg_load"]), 1) if perf_avg and perf_avg[0].get("avg_load") else None,
        "avg_api_ms": round(float(perf_avg[0]["avg_api"]), 1) if perf_avg and perf_avg[0].get("avg_api") else None,
        "slow_pages": await _safe_agg(
            analytics,
            [
                {"$match": {**m, "type": "performance", "data.page": {"$exists": True}}},
                {"$group": {"_id": "$data.page", "avg": {"$avg": "$data.load_ms"}}},
                {"$match": {"avg": {"$gte": 2500}}},
                {"$sort": {"avg": -1}},
                {"$limit": 8},
            ],
            [],
        ),
        "console_errors": await analytics.count_documents({**m, "type": "console_error"}),
    }

    # ── Intelligence ──
    peak = await _safe_agg(
        analytics,
        [
            {"$match": m},
            {"$group": {"_id": {"$hour": "$timestamp"}, "c": {"$sum": 1}}},
            {"$sort": {"c": -1}},
            {"$limit": 1},
        ],
        [],
    )
    peak_hour = peak[0]["_id"] if peak else None

    growth_pipeline = [
        {"$match": {"joined_date": {"$gte": since}}},
        {"$group": {"_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$joined_date"}}, "n": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]
    growth = await _safe_agg(users, growth_pipeline, [])

    # ── User journey (simplified from page_view) ──
    j_pipeline = [
        {"$match": {**m, "type": "page_view", "data.page": {"$exists": True}}},
        {"$group": {"_id": "$data.page", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 8},
    ]
    pages = await _safe_agg(analytics, j_pipeline, [])
    journey = {
        "nodes": [{"id": p["_id"], "count": p["count"]} for p in pages],
        "flow_note": "Sequence analytics: emit event_data.nav_path as ordered list from the mini app.",
    }

    # ── Behavior placeholders (requires richer client events) ──
    behavior = {
        "rage_clicks": await analytics.count_documents({**m, "type": "rage_click"}),
        "dead_clicks": await analytics.count_documents({**m, "type": "dead_click"}),
        "scroll_samples": await analytics.count_documents({**m, "type": "scroll_depth"}),
        "bounce_rate_proxy": None,
    }

    telegram_section = {
        "mini_app_opens": await analytics.count_documents({**m, "type": "open_app"}),
        "telegram_browser_sessions": twv,
        "telegram_premium_flagged": await analytics.count_documents({**m, "telegram_premium": True}),
    }

    return {
        "success": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {"since": _iso(since), "days": flt.days},
        "hero": hero,
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
            "states": await top_field("region", 12),
        },
        "stories": stories_section,
        "search": {
            "top": [{"query": (t["_id"] or "").strip() or "(empty)", "count": t["count"]} for t in top_searches if t["_id"] is not None],
            "failed_searches": failed_search,
            "trending_note": "Compare 7d vs prior 7d window in future revision.",
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
            "country": flt.country,
            "city": flt.city,
            "story_id": flt.story_id,
            "device": flt.device,
            "telegram_only": flt.telegram_only,
            "premium_only": flt.premium_only,
            "new_users": flt.new_users,
            "returning_users": flt.returning_users,
        },
    }


def filters_from_query(
    days: int = 30,
    country: Optional[str] = None,
    city: Optional[str] = None,
    story_id: Optional[str] = None,
    device: Optional[str] = None,
    telegram_only: bool = False,
    premium_only: bool = False,
    new_users: bool = False,
    returning_users: bool = False,
) -> AnalyticsFilters:
    return AnalyticsFilters(
        days=days,
        country=country or None,
        city=city or None,
        story_id=story_id or None,
        device=device or None,
        telegram_only=telegram_only,
        premium_only=premium_only,
        new_users=new_users,
        returning_users=returning_users,
    )
