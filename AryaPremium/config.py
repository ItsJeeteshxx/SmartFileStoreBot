from os import environ
try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv("../.env")
except ImportError:
    pass

class Config:
    API_ID        = int(environ.get("API_ID", 123456))
    API_HASH      = environ.get("API_HASH", "")
    MONGO_URI     = environ.get("DATABASE_URI") or environ.get("DATABASE", "")
    DATABASE_NAME = environ.get("DATABASE_NAME", "forward-bot")
    OWNER_IDS     = [int(i.strip()) for i in environ.get("BOT_OWNER_ID", "0").split() if i.strip().isdigit()]
    # Backward-compatible alias used by some callbacks.
    SUDO_USERS    = OWNER_IDS
    PAYMENT_LOGS_CHANNEL = environ.get("PAYMENT_LOGS_CHANNEL", "")
    DELIVERY_LOGS_CHANNEL = environ.get("DELIVERY_LOGS_CHANNEL", "")
    ARYA_LOGS_CHANNEL = environ.get("ARYA_LOGS_CHANNEL", "")

    # Premium Configs
    MGMT_BOT_TOKEN  = environ.get("MGMT_BOT_TOKEN", "")
    RAZORPAY_KEY    = environ.get("RAZORPAY_KEY", "")
    RAZORPAY_SECRET = environ.get("RAZORPAY_SECRET", "")
    EASEBUZZ_KEY    = environ.get("EASEBUZZ_KEY", "")
    EASEBUZZ_SALT   = environ.get("EASEBUZZ_SALT", "")
    EASEBUZZ_ENV    = environ.get("EASEBUZZ_ENV", "test")
    UPI_ID          = environ.get("UPI_ID", "")
    UPI_QR_URL      = environ.get("UPI_QR_URL", "")
    # Optional: HTTPS base for Vercel redirect page (e.g. https://aryastoriesupi.vercel.app) — fallback if not set per-bot in DB.
    UPI_REDIRECT_URL = environ.get("UPI_REDIRECT_URL", "").strip()
    # SliceURL (https://github.com/jeeteshmeena/sliceurl-3722de3d) — api-public Edge Function:
    # Full URL like: https://<project>.supabase.co/functions/v1/api-public (see sliceurl.app /developers)
    SLICEURL_API_URL = environ.get("SLICEURL_API_URL", "").strip()
    # API key from SliceURL dashboard (starts with slc_)
    SLICEURL_API_KEY = environ.get("SLICEURL_API_KEY", "").strip()
    # Legacy fallback: generic POST endpoint (rarely needed if SLICEURL_API_URL is set)
    SLICEURL_SHORTEN_URL = environ.get("SLICEURL_SHORTEN_URL", "").strip()
    PAYMENT_TOS_URL = environ.get("PAYMENT_TOS_URL", "")
    REFUND_POLICY_URL = environ.get("REFUND_POLICY_URL", "")