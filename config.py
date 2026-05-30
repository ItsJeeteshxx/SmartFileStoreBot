import os
from os import environ

def _parse_env(path):
    env_vars = {}
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env_vars[k.strip()] = v.strip().strip("'").strip('"')
    except Exception:
        pass
    return env_vars

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_e1 = _parse_env(os.path.join(_THIS_DIR, ".env"))
_e2 = _parse_env(os.path.join(_THIS_DIR, "AryaPremium", ".env"))

def _env(key, default=""):
    return environ.get(key) or _e1.get(key) or _e2.get(key) or default

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


class Config:
    # -------- TELEGRAM --------
    API_ID = int(_env("API_ID", "0"))
    API_HASH = _env("API_HASH", "")
    BOT_TOKEN = _env("BOT_TOKEN", "")
    BOT_SESSION = _env("BOT_SESSION", "bot")

    # -------- DATABASE --------
    DATABASE_URI = (
        _env("DATABASE_URI") or
        _env("DATABASE") or
        ""
    )

    DATABASE_NAME = _env("DATABASE_NAME", "arya")

    # -------- OWNER (FIXED) --------
    # Reads from BOTH "OWNER_IDS" and "BOT_OWNER_ID" env vars (either or both can be set)
    import re
    _raw_ids = (
        environ.get("OWNER_IDS", "") + " " +
        environ.get("BOT_OWNER_ID", "")
    )
    OWNER_IDS = list(set([int(i) for i in re.findall(r'\d+', _raw_ids)]))

    BOT_OWNER_ID = OWNER_IDS

    # -------- RAZORPAY --------
    RAZORPAY_KEY    = environ.get("RAZORPAY_KEY", "")
    RAZORPAY_SECRET = environ.get("RAZORPAY_SECRET", "")

    # -------- ANTI-ABUSE SYSTEM --------
    # How many seconds after delivery during which re-requests count as a "rapid" strike
    ABUSE_COOLDOWN_SECS = int(_env("ABUSE_COOLDOWN_SECS", "60"))
    # How many rapid strikes before a silent permanent ban is issued
    ABUSE_MAX_STRIKES   = int(_env("ABUSE_MAX_STRIKES", "3"))


class temp(object):
    lock = {}
    CANCEL = {}
    PAUSE = {}
    forwardings = 0
    BANNED_USERS = []
    IS_FRWD_CHAT = []
    # Download directory for all temp files (cleaner, merger, etc.)
    # Set at runtime by main.py; fallback to ./downloads if not set
    import os as _os
    DOWNLOAD_DIR = _os.path.abspath(
        _os.environ.get("DOWNLOAD_DIR", _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "downloads"))
    )
