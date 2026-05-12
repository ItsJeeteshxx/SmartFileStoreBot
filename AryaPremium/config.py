import os
from os import environ

# Use absolute paths relative to THIS file's location — not the CWD
_THIS_DIR    = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR  = os.path.dirname(_THIS_DIR)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_THIS_DIR, ".env"))
    load_dotenv(os.path.join(_PARENT_DIR, ".env"))
except ImportError:
    pass

# Manual .env parser fallback (in case python-dotenv is not installed or fails)
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

_e1 = _parse_env(os.path.join(_THIS_DIR, ".env"))
_e2 = _parse_env(os.path.join(_PARENT_DIR, ".env"))
_e3 = _parse_env(os.path.join(_THIS_DIR, "config.env"))
_e4 = _parse_env(os.path.join(_PARENT_DIR, "config.env"))

def _env(key, default=""):
    return (environ.get(key)
            or _e1.get(key) or _e2.get(key)
            or _e3.get(key) or _e4.get(key)
            or default)

class Config:
    API_ID        = int(_env("API_ID", "123456"))
    API_HASH      = _env("API_HASH", "")
    # Support all common key names for MongoDB URI
    MONGO_URI     = _env("MONGO_URI") or _env("DATABASE_URI") or _env("DATABASE", "")
    DATABASE_NAME = _env("DATABASE_NAME", "forward-bot")
    OWNER_IDS     = [int(i.strip()) for i in _env("BOT_OWNER_ID", "0").replace(",", " ").split() if i.strip().isdigit()]
    # Backward-compatible alias used by some callbacks.
    SUDO_USERS    = OWNER_IDS
    PAYMENT_LOGS_CHANNEL = _env("PAYMENT_LOGS_CHANNEL", "")
    DELIVERY_LOGS_CHANNEL = _env("DELIVERY_LOGS_CHANNEL", "")
    ARYA_LOGS_CHANNEL = _env("ARYA_LOGS_CHANNEL", "")

    # Premium Configs
    MGMT_BOT_TOKEN  = _env("MGMT_BOT_TOKEN", "")
    RAZORPAY_KEY    = _env("RAZORPAY_KEY", "")
    RAZORPAY_SECRET = _env("RAZORPAY_SECRET", "")
    EASEBUZZ_KEY    = _env("EASEBUZZ_KEY", "")
    EASEBUZZ_SALT   = _env("EASEBUZZ_SALT", "")
    EASEBUZZ_ENV    = _env("EASEBUZZ_ENV", "test")
    UPI_ID          = _env("UPI_ID", "")
    UPI_QR_URL      = _env("UPI_QR_URL", "")
    # Optional: HTTPS base for Vercel redirect page (e.g. https://aryastoriesupi.vercel.app) — fallback if not set per-bot in DB.
    UPI_REDIRECT_URL = _env("UPI_REDIRECT_URL", "").strip()
    # SliceURL (https://github.com/jeeteshmeena/sliceurl-3722de3d) — api-public Edge Function:
    # Full URL like: https://<project>.supabase.co/functions/v1/api-public (see sliceurl.app /developers)
    SLICEURL_API_URL = _env("SLICEURL_API_URL", "").strip()
    # API key from SliceURL dashboard (starts with slc_)
    SLICEURL_API_KEY = _env("SLICEURL_API_KEY", "").strip()
    # Legacy fallback: generic POST endpoint (rarely needed if SLICEURL_API_URL is set)
    SLICEURL_SHORTEN_URL = _env("SLICEURL_SHORTEN_URL", "").strip()
    PAYMENT_TOS_URL = _env("PAYMENT_TOS_URL", "")
    REFUND_POLICY_URL = _env("REFUND_POLICY_URL", "")
    OXAPAY_KEY = _env("OXAPAY_KEY", "sandbox")