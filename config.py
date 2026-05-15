from os import environ

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


class Config:
    # -------- TELEGRAM --------
    API_ID = int(environ.get("API_ID", 0))
    API_HASH = environ.get("API_HASH", "")
    BOT_TOKEN = environ.get("BOT_TOKEN", "")
    BOT_SESSION = environ.get("BOT_SESSION", "bot")

    # -------- DATABASE --------
    DATABASE_URI = (
        environ.get("DATABASE_URI") or
        environ.get("DATABASE") or
        ""
    )

    DATABASE_NAME = environ.get("DATABASE_NAME", "arya")

    # -------- OWNER (FIXED) --------
    # Reads from BOTH "OWNER_IDS" and "BOT_OWNER_ID" env vars (either or both can be set)
    _raw_ids = (
        environ.get("OWNER_IDS", "") + " " +
        environ.get("BOT_OWNER_ID", "")
    ).replace(",", " ").split()
    OWNER_IDS = list({int(i) for i in _raw_ids if i.strip().isdigit()})

    BOT_OWNER_ID = OWNER_IDS


class temp(object):
    lock = {}
    CANCEL = {}
    PAUSE = {}
    forwardings = 0
    BANNED_USERS = []
    IS_FRWD_CHAT = []
