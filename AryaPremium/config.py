from os import environ
try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv("../.env")
except ImportError:
    pass

# Manual .env parser fallback
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

_env1 = _parse_env(".env")
_env2 = _parse_env("../.env")
_env3 = _parse_env("config.env")
_env4 = _parse_env("../config.env")

def _env(key, default=""):
    return environ.get(key) or _env1.get(key) or _env2.get(key) or _env3.get(key) or _env4.get(key) or default

class Config:
    API_ID        = int(_env("API_ID", "123456"))
    API_HASH      = _env("API_HASH", "")
    MONGO_URI     = _env("DATABASE_URI") or _env("DATABASE", "")
    DATABASE_NAME = _env("DATABASE_NAME", "forward-bot")
    OWNER_IDS     = [int(i.strip()) for i in _env("BOT_OWNER_ID", "0").split() if i.strip().isdigit()]
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