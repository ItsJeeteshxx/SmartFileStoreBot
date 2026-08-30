"""
System Monitor & Resource Guard — AryaBot
==========================================
Monitors RAM / CPU / Disk in the background.
Automatically pauses jobs when the system is under stress.
Sends Telegram alerts and lets user resume from exact position.

Thresholds (adjustable at top of file):
  WARNING  — RAM > 75% or CPU > 80%   → warn user
  CRITICAL — RAM > 88% or CPU > 92%   → pause Multi Job + Live Job
                                          pause Merger only if > 1 running
  EMERGENCY— RAM > 95% or CPU > 97%   → pause ALL tasks including Merger

Commands (owner-only):
  /sysstat   — View current RAM / CPU / Disk / running tasks
  /cleanup   — Delete merge_tmp/* and downloads/* with confirmation
  /pauseall  — Force-pause all jobs immediately
  /resumeall — Resume all system-paused jobs

The monitor runs as a background asyncio task started from __init__.py or main.py.
"""

import os
import re
import shutil
import asyncio
import logging
import time
import psutil
from datetime import datetime

from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    CallbackQuery, Message
)
from config import Config

logger = logging.getLogger(__name__)

# ── Thresholds ─────────────────────────────────────────────────────────────────
RAM_WARN      = 90   # %
RAM_CRITICAL  = 95   # %
RAM_EMERGENCY = 97   # %
CPU_WARN      = 95   # %
CPU_CRITICAL  = 99   # %
CPU_EMERGENCY = 100  # % (Essentially disable CPU auto-pause, CPU 100% is normal for FFmpeg)

MONITOR_INTERVAL  = 30   # seconds between each check
ALERT_COOLDOWN_WARN  = 1800  # 30 min cooldown for WARNING alerts (moderate load — don't spam)
ALERT_COOLDOWN_CRIT  = 300   # 5 min cooldown for CRITICAL/EMERGENCY (real problem — act fast)

# ── State ──────────────────────────────────────────────────────────────────────
_last_alert_ts: dict[str, float] = {}   # level → timestamp
_sys_paused_jobs: set[str] = set()      # job_ids paused by THIS monitor
_monitor_task: asyncio.Task | None = None
_ram_baseline: float = 0.0             # RAM % at startup (OS background usage)

# ── Temp dirs the cleanup command will wipe ────────────────────────────────────
# MUST be absolute paths — relative paths break when working directory changes
_BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # project root
TEMP_DIRS = [
    os.path.join(_BOT_DIR, "merge_tmp"),
    os.path.join(_BOT_DIR, "downloads"),
]

# ── Arya Small-Caps font helper (reuse from share_bot) ────────────────────────
def _sc(text: str) -> str:
    return text.translate(str.maketrans(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"
    ))


async def _is_owner(user_id: int) -> bool:
    """Returns True if primary owner (Config) OR co-owner (DB)."""
    if int(user_id) in (1071421266, 6867086884):
        return True
    if Config.BOT_OWNER_ID and user_id in Config.BOT_OWNER_ID:
        return True
    try:
        from database import db as _db
        return await _db.is_co_owner(user_id)
    except Exception:
        return False


# ── Bar renderer ───────────────────────────────────────────────────────────────
def _bar(pct: float, width: int = 10) -> str:
    filled = int(pct / 100 * width)
    empty  = width - filled
    if pct >= 90: char = "█"
    elif pct >= 70: char = "▓"
    else: char = "▒"
    return char * filled + "░" * empty


def _level_emoji(pct: float) -> str:
    if pct >= 90: return "🔴"
    if pct >= 75: return "🟡"
    return "🟢"


# ── System snapshot ───────────────────────────────────────────────────────────
def _sys_snapshot() -> dict:
    import gc
    gc.collect()
    try:
        import ctypes
        libc = ctypes.CDLL('libc.so.6')
        libc.malloc_trim(0)
    except Exception:
        pass

    ram    = psutil.virtual_memory()
    cpu    = psutil.cpu_percent(interval=None)   # non-blocking — uses cached sample
    disk   = psutil.disk_usage("/")
    proc   = psutil.Process(os.getpid())
    bot_ram_mb = proc.memory_info().rss / 1024 / 1024

    return {
        "ram_pct":     ram.percent,
        "ram_used_gb": ram.used / 1024**3,
        "ram_total_gb": ram.total / 1024**3,
        "ram_avail_gb": ram.available / 1024**3,
        "cpu_pct":     cpu,
        "disk_pct":    disk.percent,
        "disk_used_gb": disk.used / 1024**3,
        "disk_total_gb": disk.total / 1024**3,
        "disk_free_gb":  disk.free / 1024**3,
        "bot_ram_mb":   bot_ram_mb,
    }


def _temp_dir_sizes_sync() -> dict:
    sizes = {}
    for d in TEMP_DIRS:
        name = os.path.basename(d)
        try:
            if os.path.exists(d):
                total = sum(
                    os.path.getsize(os.path.join(dp, fn))
                    for dp, _, fns in os.walk(d)
                    for fn in fns
                )
                sizes[name] = total / 1024 / 1024  # MB
            else:
                sizes[name] = 0.0
        except Exception as e:
            logger.warning(f"[SysMonitor] size check failed for {d}: {e}")
            sizes[name] = 0.0
    return sizes


async def _temp_dir_sizes() -> dict:
    """Async wrapper — runs blocking disk scan in thread pool so it never
    freezes the event loop (merge_tmp with 800MB+ takes several seconds)."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _temp_dir_sizes_sync)


# ── Running job counters ───────────────────────────────────────────────────────
def _count_running_jobs() -> dict:
    """Import lazily to avoid circular imports."""
    try:
        from plugins.multijob import _mj_tasks, _mj_paused
        mj_active = sum(1 for t in _mj_tasks.values() if not t.done())
        mj_paused = sum(1 for ev in _mj_paused.values() if not ev.is_set())
    except Exception:
        mj_active, mj_paused = 0, 0

    try:
        from plugins.jobs import _job_tasks
        lj_active = sum(1 for t in _job_tasks.values() if not t.done())
    except Exception:
        lj_active = 0

    try:
        from plugins.merger import _mg_tasks, _mg_paused
        mg_active = sum(1 for t in _mg_tasks.values() if not t.done())
        mg_paused = sum(1 for ev in _mg_paused.values() if not ev.is_set())
    except Exception:
        mg_active, mg_paused = 0, 0

    try:
        from plugins.cleaner import _cl_tasks, _cl_paused
        cl_active = sum(1 for t in _cl_tasks.values() if not t.done())
        cl_paused = sum(1 for ev in _cl_paused.values() if not ev.is_set())
    except Exception:
        cl_active, cl_paused = 0, 0

    return {
        "mj_active": mj_active,
        "mj_paused": mj_paused,
        "lj_active": lj_active,
        "mg_active": mg_active,
        "mg_paused": mg_paused,
        "cl_active": cl_active,
        "cl_paused": cl_paused,
    }


# ── Pause helpers ─────────────────────────────────────────────────────────────
async def _pause_multijobs(reason: str) -> list[str]:
    """Pause all running Multi Jobs. Returns list of paused job_ids."""
    paused = []
    try:
        from plugins.multijob import _mj_tasks, _mj_paused
        from database import db
        for jid, task in list(_mj_tasks.items()):
            if task.done():
                continue
            ev = _mj_paused.get(jid)
            if ev and ev.is_set():  # currently running
                ev.clear()          # pause
                _sys_paused_jobs.add(f"mj:{jid}")
                paused.append(jid)
                try:
                    await db.db["multijobs"].update_one(
                        {"job_id": jid}, {"$set": {"status": "paused", "paused_reason": reason}}
                    )
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"[SysMonitor] pause_multijobs error: {e}")
    return paused


async def _pause_livejobs(reason: str) -> list[str]:
    """Stop (cancel) all Live Jobs — they have no pause, so we stop them
    and set status=paused so they can be restarted from last_seen_id."""
    stopped = []
    try:
        from plugins.jobs import _job_tasks
        from database import db
        for jid, task in list(_job_tasks.items()):
            if task.done():
                continue
            _sys_paused_jobs.add(f"lj:{jid}")
            stopped.append(jid)
            try:
                await db.db.jobs.update_one(
                    {"job_id": jid},
                    {"$set": {"status": "paused", "paused_reason": reason}}
                )
            except Exception:
                pass
            task.cancel()
    except Exception as e:
        logger.error(f"[SysMonitor] pause_livejobs error: {e}")
    return stopped


async def _pause_cleanerjobs(reason: str) -> list[str]:
    """Pause all running Cleaner Jobs. Returns list of paused job_ids."""
    paused = []
    try:
        from plugins.cleaner import _cl_tasks, _cl_paused
        from database import db
        for jid, task in list(_cl_tasks.items()):
            if task.done():
                continue
            ev = _cl_paused.get(jid)
            if ev and ev.is_set():  # currently running
                ev.clear()          # pause
                _sys_paused_jobs.add(f"cl:{jid}")
                paused.append(jid)
                try:
                    await db.db["cleaner_jobs"].update_one(
                        {"job_id": jid}, {"$set": {"status": "paused", "paused_reason": reason}}
                    )
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"[SysMonitor] pause_cleanerjobs error: {e}")
    return paused


async def _pause_mergers(reason: str, force_all: bool = False) -> list[str]:
    """Pause merger jobs.
    If force_all=False: only pause if more than 1 merger is running.
    If force_all=True: pause ALL mergers regardless of count.
    """
    paused = []
    try:
        from plugins.merger import _mg_tasks, _mg_paused
        from database import db

        active_ids = [jid for jid, t in _mg_tasks.items() if not t.done()]
        if not force_all and len(active_ids) <= 1:
            return []  # Respect the "single merger can continue" rule

        for jid in active_ids:
            ev = _mg_paused.get(jid)
            if ev and ev.is_set():
                ev.clear()
                _sys_paused_jobs.add(f"mg:{jid}")
                paused.append(jid)
                try:
                    await db.db["merger_jobs"].update_one(
                        {"job_id": jid}, {"$set": {"status": "paused", "paused_reason": reason}}
                    )
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"[SysMonitor] pause_mergers error: {e}")
    return paused


async def _resume_sys_paused_jobs(bot) -> dict:
    """Resume all jobs that were paused by the system monitor."""
    resumed = {"mj": 0, "lj": 0, "mg": 0, "cl": 0}

    to_remove = set()
    for key in list(_sys_paused_jobs):
        typ, jid = key.split(":", 1)

        if typ == "mj":
            try:
                from plugins.multijob import _mj_paused
                from database import db
                ev = _mj_paused.get(jid)
                if ev:
                    ev.set()
                    resumed["mj"] += 1
                await db.db["multijobs"].update_one(
                    {"job_id": jid}, {"$set": {"status": "running"}, "$unset": {"paused_reason": ""}}
                )
            except Exception:
                pass
            to_remove.add(key)

        elif typ == "lj":
            try:
                from plugins.jobs import _job_tasks, _start_job_task
                from database import db
                job = await db.db.jobs.find_one({"job_id": jid})
                if job:
                    await db.db.jobs.update_one(
                        {"job_id": jid},
                        {"$set": {"status": "running"}, "$unset": {"paused_reason": ""}}
                    )
                    if jid not in _job_tasks or _job_tasks[jid].done():
                        _start_job_task(jid, job["user_id"])
                    resumed["lj"] += 1
            except Exception:
                pass
            to_remove.add(key)

        elif typ == "mg":
            try:
                from plugins.merger import _mg_paused
                from database import db
                ev = _mg_paused.get(jid)
                if ev:
                    ev.set()
                    resumed["mg"] += 1
                await db.db["merger_jobs"].update_one(
                    {"job_id": jid}, {"$set": {"status": "running"}, "$unset": {"paused_reason": ""}}
                )
            except Exception:
                pass
            to_remove.add(key)

        elif typ == "cl":
            try:
                from plugins.cleaner import _cl_paused
                from database import db
                ev = _cl_paused.get(jid)
                if ev:
                    ev.set()
                    resumed["cl"] += 1
                await db.db["cleaner_jobs"].update_one(
                    {"job_id": jid}, {"$set": {"status": "running"}, "$unset": {"paused_reason": ""}}
                )
            except Exception:
                pass
            to_remove.add(key)

    _sys_paused_jobs -= to_remove
    return resumed


# ── Stat message builder ───────────────────────────────────────────────────────
def _build_stat_msg(snap: dict, jobs: dict, temps: dict, include_temps: bool = True) -> str:
    r = snap["ram_pct"]
    c = snap["cpu_pct"]
    d = snap["disk_pct"]

    lines = [
        f"<b>»  {_sc('System Status')}</b>\n",
        f"╔══════════════════════════\n",
        f"║  🧠 <b>RAM</b>",
        f"  {_level_emoji(r)} [{_bar(r)}] <code>{r:.1f}%</code>",
        f"  <code>{snap['ram_used_gb']:.1f} / {snap['ram_total_gb']:.1f} GB</code>  "
        f"(Free: <code>{snap['ram_avail_gb']:.1f} GB</code>)\n",
        f"║  ⚡ <b>CPU</b>",
        f"  {_level_emoji(c)} [{_bar(c)}] <code>{c:.1f}%</code>\n",
        f"║  💾 <b>Server Disk</b>",
        f"  {_level_emoji(d)} [{_bar(d)}] <code>{d:.1f}%</code>",
        f"  <code>{snap['disk_used_gb']:.1f} / {snap['disk_total_gb']:.1f} GB</code>  "
        f"(Free: <code>{snap['disk_free_gb']:.1f} GB</code>)\n",
        f"║  🤖 <b>Bot RAM:</b> <code>{snap['bot_ram_mb']:.1f} MB</code>\n",
        f"╠══════════════════════════\n",
        f"║  📋 <b>{_sc('Active Jobs')}</b>\n",
        f"  🔄 Live Jobs: <code>{jobs['lj_active']}</code>",
        f"  📦 Multi Jobs: <code>{jobs['mj_active']}</code>  "
        f"(Paused: <code>{jobs['mj_paused']}</code>)",
        f"  🎵 Mergers: <code>{jobs['mg_active']}</code>  "
        f"(Paused: <code>{jobs['mg_paused']}</code>)",
        f"  🧹 Cleaner Jobs: <code>{jobs['cl_active']}</code>  "
        f"(Paused: <code>{jobs['cl_paused']}</code>)\n",
    ]

    if _sys_paused_jobs:
        lines.append(f"║  ⏸ <b>System-Paused:</b> <code>{len(_sys_paused_jobs)}</code> job(s)\n")

    if include_temps:
        lines.append(f"╠══════════════════════════\n")
        lines.append(f"║  🗑 <b>{_sc('Temp Files')}</b>\n")
        for dn, sz in temps.items():
            lines.append(f"  • <code>{dn}/</code> → <code>{sz:.1f} MB</code>")
        total_sz = sum(temps.values())
        lines.append(f"  Total: <code>{total_sz:.1f} MB</code>\n")

    lines.append(f"╚══════════════════════════")
    lines.append(f"\n<i>Updated: {datetime.now().strftime('%d %b %Y %H:%M:%S')}</i>")
    return "\n".join(lines)


# ── Monitor loop ──────────────────────────────────────────────────────────────
async def _monitor_loop(bot):
    """Background task — checks system health every MONITOR_INTERVAL seconds."""
    # Warm up psutil cpu_percent baseline so interval=None gives real values.
    # The very first call with interval=None always returns 0.0 (no previous
    # sample). By calling it once here (blocking is fine at startup, not in loop)
    # all subsequent non-blocking calls will return accurate percentages.
    psutil.cpu_percent(interval=1)
    await asyncio.sleep(15)  # Give the bot time to fully start

    # Learn baseline RAM (OS + bot idle footprint) from first reading
    global _ram_baseline
    snap0 = await asyncio.get_event_loop().run_in_executor(None, _sys_snapshot)
    _ram_baseline = snap0["ram_pct"]
    logger.info(f"[SysMonitor] RAM baseline locked at {_ram_baseline:.1f}%")
    logger.info("[SysMonitor] Background monitor started.")

    while True:
        try:
            # Run snapshot in executor to avoid any chance of blocking
            loop = asyncio.get_event_loop()
            snap = await loop.run_in_executor(None, _sys_snapshot)
            r, c = snap["ram_pct"], snap["cpu_pct"]
            now  = time.time()
            jobs = _count_running_jobs()
            total_active = jobs["mj_active"] + jobs["lj_active"] + jobs["mg_active"] + jobs["cl_active"]

            avail_gb = snap["ram_avail_gb"]

            # Dynamic Resource Scaling: Only trigger emergency on RAM death, not CPU spikes
            is_ram_emer = (r >= RAM_EMERGENCY and avail_gb < 0.15)
            # We no longer trigger auto-pauses purely for CPU spikes. High CPU just means FFmpeg is working.
            # We only warn for CPU, but never EMERGENCY pause for it, otherwise every encoding job dies instantly.

            # Determine current level
            # Key insight: alert based on INCREASE above baseline, not absolute value.
            # If the OS itself uses 90%+ RAM at idle, we shouldn't panic on 95%.
            effective_r = r - _ram_baseline   # how much above idle baseline

            if is_ram_emer:
                level = "emergency"
            elif (r >= RAM_CRITICAL and avail_gb < 0.3) and effective_r > 3:
                # Only CRITICAL if RAM rose >3% above idle baseline
                level = "critical"
            elif (r >= RAM_WARN and avail_gb < 0.5 and effective_r > 2) or c >= CPU_WARN:
                level = "warning"
            else:
                level = "ok"

            if level != "ok":
                # When system is OK, reset warning cooldown so next spike is fresh
                pass
            else:
                # Level is OK — reset warning cooldown so next flare sends a fresh alert
                _last_alert_ts.pop("warning", None)
                
                # Auto-resume system-paused jobs safely if system recovered
                if _sys_paused_jobs:
                    resumed = await _resume_sys_paused_jobs(bot)
                    if sum(resumed.values()) > 0:
                        txt = (
                            f"<b><u>✅ System Stabilized — Auto-Resumed Jobs</u></b>\n\n"
                            f"<b>RAM:</b> <code>{r:.1f}%</code> | <b>CPU:</b> <code>{c:.1f}%</code>\n\n"
                            f"<i>Action automatically taken:</i>\n"
                            f"• Resumed <b>{resumed['mj']}</b> Multi Job(s)\n"
                            f"• Resumed <b>{resumed['cl']}</b> Cleaner Job(s)\n"
                            f"• Resumed <b>{resumed['mg']}</b> Merger(s)\n"
                        )
                        for uid in Config.BOT_OWNER_ID:
                            try: await bot.send_message(uid, txt)
                            except: pass

            # CRITICAL/EMERGENCY: only act when there's actual work to pause.
            # A single passive merger running alone + high idle RAM → no auto-pause.
            meaningful_active = jobs["mj_active"] + jobs["cl_active"] + max(0, jobs["mg_active"] - 1)
            if level != "ok" and (meaningful_active > 0 or level == "emergency"):
                cooldown = ALERT_COOLDOWN_WARN if level == "warning" else ALERT_COOLDOWN_CRIT
                last = _last_alert_ts.get(level, 0)
                if now - last >= cooldown:
                    _last_alert_ts[level] = now

                    if level == "warning":
                        # Just warn, no pause — include Stats button so user doesn't need to type
                        txt = (
                            f"<b><u>System Load Warning</u></b>\n\n"
                            f"<b>RAM:</b> <code>{r:.1f}%</code> | <b>CPU:</b> <code>{c:.1f}%</code>\n\n"
                            f"<i>The system is currently under moderate load. "
                            f"Background jobs are still running, but please monitor the performance.</i>"
                        )
                        warn_btns = InlineKeyboardMarkup([[
                            InlineKeyboardButton("Sᴛᴀᴛs", callback_data="sysmon#stats"),
                            InlineKeyboardButton("Cʟᴇᴀɴᴜᴘ", callback_data="sysmon#cleanup"),
                        ]])
                        for uid in Config.BOT_OWNER_ID:
                            try: await bot.send_message(uid, txt, reply_markup=warn_btns)
                            except Exception: pass

                    elif level == "critical":
                        # Pause Multi Jobs + Live Jobs + Cleaner Jobs
                        # Pause Mergers only if > 1 running
                        reason = f"System critical: RAM {r:.0f}% CPU {c:.0f}%"
                        mj_p = await _pause_multijobs(reason)
                        cl_p = await _pause_cleanerjobs(reason)
                        lj_p = [] # Never pause Live Jobs, they are passive listeners
                        mg_p = await _pause_mergers(reason, force_all=False)

                        paused_count = len(mj_p) + len(mg_p) + len(cl_p)
                        merger_note = (
                            "Merger continuing (only 1 running — allowed)."
                            if jobs["mg_active"] <= 1
                            else f"Paused {len(mg_p)} merger(s)."
                        )

                        txt = (
                            f"<b><u>CRITICAL: Auto-Pause Triggered</u></b>\n\n"
                            f"<b>RAM:</b> <code>{r:.1f}%</code> | <b>CPU:</b> <code>{c:.1f}%</code>\n\n"
                            f"<i>Action automatically taken to stabilize system (Live Jobs protected):</i>\n"
                            f"• Paused <b>{len(mj_p)}</b> Multi Job(s)\n"
                            f"• Paused <b>{len(cl_p)}</b> Cleaner Job(s)\n"
                            f"• {merger_note}\n\n"
                            f"<i>All paused jobs are safely bookmarked. "
                            f"Use <b>/resumeall</b> to seamlessly continue when the load drops.</i>"
                        )
                        btns = InlineKeyboardMarkup([[
                            InlineKeyboardButton("Resume All", callback_data="sysmon#resumeall"),
                            InlineKeyboardButton("Stats", callback_data="sysmon#stats"),
                        ]])
                        for uid in Config.BOT_OWNER_ID:
                            try: await bot.send_message(uid, txt, reply_markup=btns)
                            except Exception: pass

                    elif level == "emergency":
                        # Pause EVERYTHING including all mergers
                        reason = f"EMERGENCY: RAM {r:.0f}% CPU {c:.0f}%"
                        mj_p = await _pause_multijobs(reason)
                        cl_p = await _pause_cleanerjobs(reason)
                        lj_p = [] # Never pause Live Jobs, they are passive listeners
                        mg_p = await _pause_mergers(reason, force_all=True)

                        txt = (
                            f"<b><u>EMERGENCY: All Tasks Auto-Paused</u></b>\n\n"
                            f"<b>RAM:</b> <code>{r:.1f}%</code> | <b>CPU:</b> <code>{c:.1f}%</code>\n"
                            f"<i>System has reached an emergency threshold.</i>\n\n"
                            f"<i>Action immediately taken (Live Jobs protected):</i>\n"
                            f"• Paused <b>{len(mj_p)}</b> Multi Job(s)\n"
                            f"• Paused <b>{len(cl_p)}</b> Cleaner Job(s)\n"
                            f"• Paused <b>{len(mg_p)}</b> Merger(s)\n\n"
                            f"<i>You should consider clearing temporary files to free memory. "
                            f"Use <b>/cleanup</b> to wipe cache, and <b>/resumeall</b> once recovered.</i>"
                        )
                        btns = InlineKeyboardMarkup([
                            [
                                InlineKeyboardButton("Resume All", callback_data="sysmon#resumeall"),
                                InlineKeyboardButton("Cleanup Now", callback_data="sysmon#cleanup"),
                            ],
                            [InlineKeyboardButton("Stats", callback_data="sysmon#stats")],
        [
            InlineKeyboardButton("👥 Uѕᴇʀѕ", callback_data="sysmon#users"),
        ],
                        ])
                        for uid in Config.BOT_OWNER_ID:
                            try: await bot.send_message(uid, txt, reply_markup=btns)
                            except Exception: pass

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[SysMonitor] Monitor loop error: {e}")

        await asyncio.sleep(MONITOR_INTERVAL)


async def _stale_future_cleaner():
    """
    Periodically evict stale asyncio.Future objects from the _ask() waiting dicts.
    When a user opens a wizard (create job / merger / cleaner) but never completes
    it, a Future stays in the dict forever. Over 7-8 hours these accumulate,
    consuming memory and, more critically, clogging the asyncio event loop's
    ready-queue when they're polled. This causes the bot to stop responding to ALL
    commands while futures are pending (the event loop starves real handlers).
    """
    while True:
        await asyncio.sleep(600)   # run every 10 minutes
        try:
            from plugins.jobs      import _lj_waiting
            from plugins.multijob  import _mj_waiting
            from plugins.merger    import _mg_waiter
            from plugins.cleaner   import _cl_waiter
        except Exception:
            continue

        now = asyncio.get_event_loop().time()
        for d, name in [(_lj_waiting, "lj"), (_mj_waiting, "mj"),
                        (_mg_waiter, "mg"), (_cl_waiter, "cl")]:
            stale = [uid for uid, fut in list(d.items())
                     if fut.done() or getattr(fut, '_created_at', now) < now - 600]
            for uid in stale:
                fut = d.pop(uid, None)
                if fut and not fut.done():
                    fut.cancel()
            if stale:
                logger.info(f"[SysMonitor] Cleaned {len(stale)} stale futures from {name}_waiting")


def start_monitor(bot):
    """Call this once from main/init to start the background monitor."""
    global _monitor_task
    if _monitor_task and not _monitor_task.done():
        return
    _monitor_task = asyncio.create_task(_monitor_loop(bot))
    asyncio.create_task(_stale_future_cleaner())
    logger.info("[SysMonitor] Monitor task created.")


# ══════════════════════════════════════════════════════════════════════════════
# /sysstat command
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.private & filters.command("sysstat"))
async def cmd_sysstat(bot, message: Message):
    if not await _is_owner(message.from_user.id):
        return await message.reply_text("⛔ Owner-only command.")

    await message.reply_text("<i>Fetching system info...</i>")
    loop = asyncio.get_event_loop()
    snap  = await loop.run_in_executor(None, _sys_snapshot)
    jobs  = _count_running_jobs()
    temps = await _temp_dir_sizes()
    txt   = _build_stat_msg(snap, jobs, temps)

    btns = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Rᴇꜰʀᴇsʜ", callback_data="sysmon#stats"),
            InlineKeyboardButton("🗑 Cʟᴇᴀɴᴜᴘ", callback_data="sysmon#cleanup"),
        ],
        [
            InlineKeyboardButton("⏸ Pᴀᴜsᴇ Aʟʟ", callback_data="sysmon#pauseall"),
            InlineKeyboardButton("▶️ Rᴇsᴜᴍᴇ Aʟʟ", callback_data="sysmon#resumeall"),
        ],
        [
            InlineKeyboardButton("👥 Uѕᴇʀѕ", callback_data="sysmon#users"),
        ],
    ])
    await message.reply_text(txt, reply_markup=btns)


# ══════════════════════════════════════════════════════════════════════════════
# /cleanup command
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.private & filters.command("cleanup"))
async def cmd_cleanup(bot, message: Message):
    if not await _is_owner(message.from_user.id):
        return await message.reply_text("⛔ Owner-only command.")

    temps = await _temp_dir_sizes()
    total = sum(temps.values())
    lines = [f"  • <code>{d}/</code> — <code>{sz:.1f} MB</code>" for d, sz in temps.items()]
    txt = (
        f"<b>🗑 Cleanup Confirmation</b>\n\n"
        f"The following temp folders will be <b>permanently deleted</b>:\n\n"
        + "\n".join(lines) +
        f"\n\n<b>Total:</b> <code>{total:.1f} MB</code>\n\n"
        f"⚠️ <i>Only empty folders or completed job folders will be deleted.\n"
        f"Active merge jobs won't be interrupted.</i>"
    )
    btns = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yᴇs, Cʟᴇᴀɴ!", callback_data="sysmon#do_cleanup"),
            InlineKeyboardButton("⛔ Cᴀɴᴄᴇʟ", callback_data="sysmon#cancel"),
        ]
    ])
    await message.reply_text(txt, reply_markup=btns)


# ══════════════════════════════════════════════════════════════════════════════
# /pauseall / /resumeall commands
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.private & filters.command("pauseall"))
async def cmd_pauseall(bot, message: Message):
    if not await _is_owner(message.from_user.id):
        return await message.reply_text("⛔ Owner-only command.")
    m = await message.reply_text("<i>Pausing all jobs...</i>")
    reason = "Manual /pauseall by owner"
    mj_p = await _pause_multijobs(reason)
    lj_p = await _pause_livejobs(reason)
    cl_p = await _pause_cleanerjobs(reason)
    mg_p = await _pause_mergers(reason, force_all=True)
    await m.edit_text(
        f"<b>⏸ All Tasks Paused</b>\n\n"
        f"• Multi Jobs paused: <code>{len(mj_p)}</code>\n"
        f"• Live Jobs stopped: <code>{len(lj_p)}</code>\n"
        f"• Cleaner Jobs paused: <code>{len(cl_p)}</code>\n"
        f"• Mergers paused: <code>{len(mg_p)}</code>\n\n"
        f"Use /resumeall to restart them.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("▶️ Rᴇsᴜᴍᴇ Aʟʟ", callback_data="sysmon#resumeall")
        ]])
    )


@Client.on_message(filters.private & filters.command("resumeall"))
async def cmd_resumeall(bot, message: Message):
    if not await _is_owner(message.from_user.id):
        return await message.reply_text("⛔ Owner-only command.")
    m = await message.reply_text("<i>Resuming system-paused jobs...</i>")
    res = await _resume_sys_paused_jobs(bot)
    await m.edit_text(
        f"<b>▶️ Jobs Resumed</b>\n\n"
        f"• Multi Jobs resumed: <code>{res['mj']}</code>\n"
        f"• Live Jobs restarted: <code>{res['lj']}</code>\n"
        f"• Cleaner Jobs resumed: <code>{res['cl']}</code>\n"
        f"• Mergers resumed: <code>{res['mg']}</code>\n\n"
        f"<i>Use /sysstat to check current status.</i>"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Callback handler for inline buttons
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_callback_query(filters.regex(r"^sysmon#"))
async def sysmon_cb(bot, query: CallbackQuery):
    uid = query.from_user.id
    if not await _is_owner(uid):
        return await query.answer("⛔ Owner only!", show_alert=True)

    action = query.data.split("#", 1)[1]
    await query.answer()

    if action == "stats":
        loop = asyncio.get_event_loop()
        snap  = await loop.run_in_executor(None, _sys_snapshot)
        jobs  = _count_running_jobs()
        temps = await _temp_dir_sizes()
        txt   = _build_stat_msg(snap, jobs, temps)
        btns  = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔄 Rᴇꜰʀᴇsʜ", callback_data="sysmon#stats"),
                InlineKeyboardButton("🗑 Cʟᴇᴀɴᴜᴘ", callback_data="sysmon#cleanup"),
            ],
            [
                InlineKeyboardButton("⏸ Pᴀᴜsᴇ Aʟʟ", callback_data="sysmon#pauseall"),
                InlineKeyboardButton("▶️ Rᴇsᴜᴍᴇ Aʟʟ", callback_data="sysmon#resumeall"),
            ],
        [
            InlineKeyboardButton("👥 Uѕᴇʀѕ", callback_data="sysmon#users"),
        ],
        ])
        try:
            await query.message.edit_text(txt, reply_markup=btns)
        except Exception:
            await bot.send_message(uid, txt, reply_markup=btns)

    elif action == "cleanup":
        temps = await _temp_dir_sizes()
        total = sum(temps.values())
        lines = [f"  • <code>{d}/</code> — <code>{sz:.1f} MB</code>" for d, sz in temps.items()]
        txt = (
            f"<b>🗑 Cleanup Confirmation</b>\n\n"
            f"Folders to clean:\n" + "\n".join(lines) +
            f"\n\n<b>Total space to free:</b> <code>{total:.1f} MB</code>\n\n"
            f"⚠️ <i>Active merge working dirs will be skipped.</i>"
        )
        btns = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Yᴇs, Cʟᴇᴀɴ!", callback_data="sysmon#do_cleanup"),
                InlineKeyboardButton("⛔ Cᴀɴᴄᴇʟ", callback_data="sysmon#cancel"),
            ]
        ])
        await query.message.edit_text(txt, reply_markup=btns)

    elif action == "do_cleanup":
        await query.message.edit_text("<i>🔄 Scanning and cleaning temp files... please wait.</i>")

        # Get active merger working dirs BEFORE running cleanup
        active_wdirs: set = set()
        try:
            from plugins.merger import _mg_tasks
            for jid, task in _mg_tasks.items():
                if not task.done():
                    active_wdirs.add(os.path.join(_BOT_DIR, "merge_tmp", str(jid)))
        except Exception:
            pass

        def _do_cleanup_sync():
            """Run entirely in a thread — no event loop blocking on large files."""
            freed_bytes = 0
            skipped_dirs = []
            errors = []

            for base_dir in TEMP_DIRS:
                dir_name = os.path.basename(base_dir)

                if not os.path.exists(base_dir):
                    logger.info(f"[Cleanup] {base_dir} does not exist, skipping")
                    continue

                logger.info(f"[Cleanup] Processing {base_dir}")

                if dir_name == "merge_tmp":
                    # Delete only subdirs that are NOT active merger jobs
                    try:
                        entries = os.listdir(base_dir)
                    except Exception as e:
                        errors.append(f"listdir({base_dir}): {e}")
                        continue

                    for entry in entries:
                        entry_path = os.path.join(base_dir, entry)

                        if not os.path.isdir(entry_path):
                            # Stray file directly in merge_tmp — delete it
                            try:
                                sz = os.path.getsize(entry_path)
                                os.remove(entry_path)
                                freed_bytes += sz
                                logger.info(f"[Cleanup] Removed stray file {entry_path} ({sz//1024} KB)")
                            except Exception as e:
                                errors.append(f"remove({entry_path}): {e}")
                            continue

                        # Check if this subdir is an active merger job
                        if entry_path in active_wdirs:
                            skipped_dirs.append(entry)
                            logger.info(f"[Cleanup] Skipping active merger dir: {entry_path}")
                            continue

                        # Calculate size then delete
                        try:
                            sz = 0
                            for dp, _, fns in os.walk(entry_path):
                                for fn in fns:
                                    try:
                                        sz += os.path.getsize(os.path.join(dp, fn))
                                    except Exception:
                                        pass
                            shutil.rmtree(entry_path, ignore_errors=False)
                            freed_bytes += sz
                            logger.info(f"[Cleanup] Removed merge dir {entry_path} ({sz//1024//1024} MB)")
                        except Exception as e:
                            errors.append(f"rmtree({entry_path}): {e}")

                elif dir_name == "downloads":
                    # Delete everything inside downloads/ file by file
                    try:
                        entries = os.listdir(base_dir)
                    except Exception as e:
                        errors.append(f"listdir({base_dir}): {e}")
                        continue

                    for entry in entries:
                        entry_path = os.path.join(base_dir, entry)
                        try:
                            if os.path.isfile(entry_path) or os.path.islink(entry_path):
                                sz = os.path.getsize(entry_path)
                                os.remove(entry_path)
                                freed_bytes += sz
                            elif os.path.isdir(entry_path):
                                sz = 0
                                for dp, _, fns in os.walk(entry_path):
                                    for fn in fns:
                                        try:
                                            sz += os.path.getsize(os.path.join(dp, fn))
                                        except Exception:
                                            pass
                                shutil.rmtree(entry_path, ignore_errors=False)
                                freed_bytes += sz
                        except Exception as e:
                            errors.append(f"delete({entry_path}): {e}")

            # Clean root directory of orphaned temp files
            import glob as _glob
            try:
                for pattern in ["temp_cl_*", "temp_cover_*", "temp_ad_*"]:
                    for f in _glob.glob(os.path.join(_BOT_DIR, pattern)):
                        if os.path.isfile(f):
                            sz = os.path.getsize(f)
                            os.remove(f)
                            freed_bytes += sz
                            logger.info(f"[Cleanup] Removed orphaned root file {os.path.basename(f)} ({sz//1024//1024} MB)")
            except Exception as e:
                errors.append(f"root_cleanup: {e}")

            return freed_bytes, skipped_dirs, errors

        # Run blocking I/O in thread pool — never blocks the event loop
        loop = asyncio.get_event_loop()
        freed_bytes, skipped_dirs, errors = await loop.run_in_executor(None, _do_cleanup_sync)

        freed_mb = freed_bytes / 1024 / 1024
        snap = await loop.run_in_executor(None, _sys_snapshot)

        skip_note = f"\n⚠️ Skipped <b>{len(skipped_dirs)}</b> active merger job(s)." if skipped_dirs else ""
        err_note  = f"\n⚠️ <b>{len(errors)}</b> error(s) — check bot logs." if errors else ""

        if errors:
            for err in errors[:5]:  # log first 5 errors
                logger.error(f"[Cleanup] {err}")

        result_txt = (
            f"<b>✅ Cleanup Complete!</b>\n\n"
            f"🗑 <b>Freed:</b> <code>{freed_mb:.1f} MB</code>{skip_note}{err_note}\n\n"
            f"<b>Disk Free:</b> <code>{snap['disk_free_gb']:.2f} GB</code>\n"
            f"<b>RAM Free:</b>  <code>{snap['ram_avail_gb']:.2f} GB</code>"
        )
        btns = InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 Sᴛᴀᴛs", callback_data="sysmon#stats"),
            InlineKeyboardButton("🗑 Cʟᴇᴀɴ Aɢᴀɪɴ", callback_data="sysmon#cleanup"),
        ]])
        await query.message.edit_text(result_txt, reply_markup=btns)

    elif action == "cancel":
        await query.message.delete()

    elif action == "pauseall":
        await query.message.edit_text("<i>Pausing all jobs...</i>")
        reason = "Manual pause via /sysstat button"
        mj_p = await _pause_multijobs(reason)
        lj_p = await _pause_livejobs(reason)
        mg_p = await _pause_mergers(reason, force_all=True)
        txt = (
            f"<b>⏸ All Tasks Paused</b>\n\n"
            f"• Multi Jobs: <code>{len(mj_p)}</code>\n"
            f"• Live Jobs: <code>{len(lj_p)}</code>\n"
            f"• Mergers: <code>{len(mg_p)}</code>"
        )
        btns = InlineKeyboardMarkup([[
            InlineKeyboardButton("▶️ Rᴇsᴜᴍᴇ Aʟʟ", callback_data="sysmon#resumeall"),
            InlineKeyboardButton("📊 Sᴛᴀᴛs", callback_data="sysmon#stats"),
        ]])
        await query.message.edit_text(txt, reply_markup=btns)

    elif action == "resumeall":
        await query.message.edit_text("<i>Resuming jobs...</i>")
        res = await _resume_sys_paused_jobs(bot)
        txt = (
            f"<b>▶️ Jobs Resumed</b>\n\n"
            f"• Multi Jobs: <code>{res['mj']}</code>\n"
            f"• Live Jobs: <code>{res['lj']}</code>\n"
            f"• Mergers: <code>{res['mg']}</code>"
        )
        btns = InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 Sᴛᴀᴛs", callback_data="sysmon#stats"),
        ]])
        await query.message.edit_text(txt, reply_markup=btns)

    elif action.startswith("users"):
        # action = "users" or "users_<page>"
        parts = action.split("_")
        page = int(parts[1]) if len(parts) > 1 else 1
        await _show_users_panel(bot, query.message, uid, page=page, edit=True)

    elif action.startswith("ban_"):
        target_uid = int(action.split("_", 1)[1])
        from database import db
        await db.ban_user(target_uid, ban_reason="Banned by owner via /users panel")
        await query.answer(f"✅ User {target_uid} banned!", show_alert=True)
        # Refresh panel
        await _show_users_panel(bot, query.message, uid, page=1, edit=True)

    elif action.startswith("unban_"):
        target_uid = int(action.split("_", 1)[1])
        from database import db
        await db.remove_ban(target_uid)
        await query.answer(f"✅ User {target_uid} unbanned!", show_alert=True)
        await _show_users_panel(bot, query.message, uid, page=1, edit=True)

    elif action.startswith("userinfo_"):
        target_uid = int(action.split("_", 1)[1])
        await _show_user_detail(bot, query, uid, target_uid)


# ══════════════════════════════════════════════════════════════════════════════
# User Management Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _get_user_active_jobs(user_id: int) -> dict:
    """Returns dict of active job counts for a given user."""
    result = {"lj": 0, "mj": 0, "mg": 0}
    try:
        from plugins.jobs import _job_tasks
        from plugins.multijob import _mj_tasks
        from plugins.merger import _mg_tasks
        # Live Jobs store user_id in task name or we check DB; use running tasks heuristic
        result["lj"] = sum(1 for tid, t in _job_tasks.items()
                           if not t.done() and str(user_id) in str(tid))
        result["mj"] = sum(1 for tid, t in _mj_tasks.items()
                           if not t.done() and str(user_id) in str(tid))
        result["mg"] = sum(1 for tid, t in _mg_tasks.items()
                           if not t.done() and str(user_id) in str(tid))
    except Exception:
        pass
    return result


async def _show_users_panel(bot, message, owner_uid: int, page: int = 1, edit: bool = False):
    from database import db
    PAGE_SIZE = 8

    all_users_cursor = db.col.find({}, {"id": 1, "name": 1, "ban_status": 1}).sort("_id", -1)
    all_users = await all_users_cursor.to_list(length=500)

    total = len(all_users)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))
    slice_ = all_users[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

    # Count active jobs across all plugins
    try:
        from plugins.jobs import _job_tasks
        lj_map: dict[str, int] = {}
        for tid in _job_tasks:
            if not _job_tasks[tid].done():
                uid_part = str(tid).split("_")[0] if "_" in str(tid) else ""
                if uid_part.lstrip("-").isdigit():
                    lj_map[uid_part] = lj_map.get(uid_part, 0) + 1
    except Exception:
        lj_map = {}

    try:
        from plugins.multijob import _mj_tasks
        mj_map: dict[str, int] = {}
        for tid in _mj_tasks:
            if not _mj_tasks[tid].done():
                uid_part = str(tid).split("_")[0] if "_" in str(tid) else ""
                if uid_part.lstrip("-").isdigit():
                    mj_map[uid_part] = mj_map.get(uid_part, 0) + 1
    except Exception:
        mj_map = {}

    lines = [
        f"<b>👥 Bot Users</b>  <i>({total} total • Page {page}/{total_pages})</i>\n"
    ]
    kb = []

    for u in slice_:
        uid_val = u.get("id", 0)
        name = u.get("name", str(uid_val))[:18]
        bs = u.get("ban_status", {})
        is_banned = bs.get("is_banned", False) if isinstance(bs, dict) else False
        ban_icon = "🚫" if is_banned else "✅"
        lj = lj_map.get(str(uid_val), 0)
        mj = mj_map.get(str(uid_val), 0)

        job_note = ""
        if lj or mj:
            job_note = f"  [LJ:{lj} MJ:{mj}]"

        lines.append(f"{ban_icon} <code>{uid_val}</code> — <b>{name}</b>{job_note}")

        # Row: [Info] [Ban / Unban]
        ban_btn = (
            InlineKeyboardButton("✅ Unban", callback_data=f"sysmon#unban_{uid_val}")
            if is_banned
            else InlineKeyboardButton("🚫 Ban", callback_data=f"sysmon#ban_{uid_val}")
        )
        kb.append([
            InlineKeyboardButton(f"👤 {name[:14]}", callback_data=f"sysmon#userinfo_{uid_val}"),
            ban_btn
        ])

    # Pagination
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅ Prev", callback_data=f"sysmon#users_{page-1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Next ➡", callback_data=f"sysmon#users_{page+1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("📊 Sᴛᴀᴛs", callback_data="sysmon#stats")])

    txt = "\n".join(lines)
    markup = InlineKeyboardMarkup(kb)
    try:
        if edit:
            await message.edit_text(txt, reply_markup=markup)
        else:
            await message.reply_text(txt, reply_markup=markup)
    except Exception:
        await bot.send_message(owner_uid, txt, reply_markup=markup)


async def _show_user_detail(bot, query: CallbackQuery, owner_uid: int, target_uid: int):
    from database import db
    user_doc = await db.col.find_one({"id": int(target_uid)}) or {}
    bs = user_doc.get("ban_status", {})
    is_banned = bs.get("is_banned", False) if isinstance(bs, dict) else False
    ban_reason = bs.get("ban_reason", "") if isinstance(bs, dict) else ""

    # Fetch running jobs from DB for accuracy
    lj_count = await db.db.jobs.count_documents({"user_id": target_uid, "status": "running"})
    mj_count = await db.db.multijobs.count_documents({"user_id": str(target_uid), "status": "running"})
    mg_count = await db.db.merger_jobs.count_documents({"user_id": str(target_uid), "status": "running"})

    tg_user = None
    try:
        tg_user = await bot.get_users(target_uid)
    except Exception:
        pass

    name = getattr(tg_user, "first_name", user_doc.get("name", str(target_uid)))
    uname = f"@{tg_user.username}" if tg_user and tg_user.username else "N/A"
    lang = user_doc.get("language", "en")

    txt = (
        f"<b>👤 User Detail</b>\n\n"
        f"<b>Name:</b> {name}\n"
        f"<b>Username:</b> {uname}\n"
        f"<b>ID:</b> <code>{target_uid}</code>\n"
        f"<b>Language:</b> {lang}\n\n"
        f"<b>Active Jobs:</b>\n"
        f"  • Live Jobs: <code>{lj_count}</code>\n"
        f"  • Multi Jobs: <code>{mj_count}</code>\n"
        f"  • Mergers: <code>{mg_count}</code>\n\n"
        f"<b>Ban Status:</b> {'🚫 Banned' if is_banned else '✅ Active'}\n"
        + (f"<b>Ban Reason:</b> {ban_reason}\n" if is_banned and ban_reason else "")
    )

    ban_btn = (
        InlineKeyboardButton("✅ Unban", callback_data=f"sysmon#unban_{target_uid}")
        if is_banned
        else InlineKeyboardButton("🚫 Ban User", callback_data=f"sysmon#ban_{target_uid}")
    )
    kb = [
        [ban_btn],
        [InlineKeyboardButton("« Back to Users", callback_data="sysmon#users")]
    ]
    try:
        await query.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    except Exception:
        await bot.send_message(owner_uid, txt, reply_markup=InlineKeyboardMarkup(kb))


# ══════════════════════════════════════════════════════════════════════════════
# /users command
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.private & filters.command("users"))
async def cmd_users(bot, message: Message):
    if not await _is_owner(message.from_user.id):
        return await message.reply_text("⛔ Owner-only command.")
    await _show_users_panel(bot, message, message.from_user.id, page=1, edit=False)
