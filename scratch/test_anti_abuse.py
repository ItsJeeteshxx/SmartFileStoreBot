"""
test_anti_abuse.py — Unit Tests for the 3-Strike Anti-Abuse System
====================================================================
Tests the core logic of _check_and_record_rapid_request without
requiring a live Telegram connection or MongoDB.

Run:  python scratch/test_anti_abuse.py
"""
import asyncio
import sys
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

# Force UTF-8 output on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# Add parent dir to path so we can import from the project
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

PASS = "[PASS]"
FAIL = "[FAIL]"

# ── Minimal stubs to avoid Telegram / MongoDB connections during tests ─────────

class FakeMessage:
    def __init__(self, user_id, first_name="TestUser"):
        self.from_user = MagicMock()
        self.from_user.id = user_id
        self.from_user.first_name = first_name
        self.command = ["start", "some_uuid"]
        self.reply_text = AsyncMock()

class FakeClient:
    def __init__(self):
        self.me = MagicMock()
        self.me.first_name = "TestDeliveryBot"
        self.me.id = 99999999

class FakeDB:
    def __init__(self):
        self._strikes = {}
        self._bans = {}

    async def get_user_strike(self, user_id):
        return self._strikes.get(user_id, {
            'count': 0, 'last_delivery_ts': 0.0, 'last_strike_ts': 0.0
        })

    async def update_user_strike(self, user_id, count, last_delivery_ts=None, last_strike_ts=None):
        rec = self._strikes.get(user_id, {
            'count': 0, 'last_delivery_ts': 0.0, 'last_strike_ts': 0.0
        })
        rec['count'] = count
        if last_delivery_ts is not None:
            rec['last_delivery_ts'] = last_delivery_ts
        if last_strike_ts is not None:
            rec['last_strike_ts'] = last_strike_ts
        self._strikes[user_id] = rec

    async def reset_user_strike(self, user_id):
        self._strikes.pop(user_id, None)

    async def ban_user(self, user_id, reason=""):
        self._bans[user_id] = reason

    async def get_ban_status(self, user_id):
        return {
            'is_banned': user_id in self._bans,
            'ban_reason': self._bans.get(user_id, '')
        }


# ── Helper to call _check_and_record_rapid_request with mocked deps ─────────

async def run_check(fn, client, message, user_id, bot_id, is_owner=False):
    """Run the abuse check with faked config and owner check."""
    with patch('plugins.share_bot.db', fake_db), \
         patch('plugins.share_bot.Config') as mock_cfg, \
         patch('plugins.share_bot._is_any_owner' if False else 'plugins.banned._is_any_owner',
               new=AsyncMock(return_value=is_owner)), \
         patch('plugins.arya_logger.log_ban', new=AsyncMock()), \
         patch('plugins.arya_logger.log_warn', new=AsyncMock()):
        mock_cfg.ABUSE_COOLDOWN_SECS = 60
        mock_cfg.ABUSE_MAX_STRIKES = 3
        return await fn(client, message, user_id, bot_id)


# ── Test runner ────────────────────────────────────────────────────────────────

fake_db = FakeDB()
results = []

def record(name, passed, detail=""):
    status = PASS if passed else FAIL
    results.append((status, name, detail))
    print(f"  {status}  {name}" + (f"  [{detail}]" if detail else ""))


async def main():
    # Import the function under test AFTER stubs are ready
    # We patch at import time to avoid live connections
    with patch.dict('sys.modules', {
        'pyrogram': MagicMock(),
        'pyrogram.Client': MagicMock(),
        'pyrogram.filters': MagicMock(),
        'pyrogram.enums': MagicMock(),
        'pyrogram.types': MagicMock(),
        'pyrogram.errors': MagicMock(),
        'pyrogram.handlers': MagicMock(),
        'database': MagicMock(db=fake_db),
        'config': MagicMock(Config=MagicMock(
            ABUSE_COOLDOWN_SECS=60,
            ABUSE_MAX_STRIKES=3,
        )),
        'plugins.banned': MagicMock(_is_any_owner=AsyncMock(return_value=False)),
        'plugins.arya_logger': MagicMock(
            log_ban=AsyncMock(),
            log_warn=AsyncMock(),
        ),
        'bot': MagicMock(BOT_INSTANCE=MagicMock()),
    }):
        import importlib
        # Re-import after patching
        if 'plugins.share_bot' in sys.modules:
            del sys.modules['plugins.share_bot']
        sys.modules.pop('plugins.arya_logger', None)
        sys.modules.pop('plugins.banned', None)

        # Import the patched module
        import plugins.share_bot as sbot

        client = FakeClient()
        NOW = time.time()

        # ── Test 1: First delivery, no prior history → allow ─────────────────
        print("\n=== Test: Normal first delivery (no cooldown active) ===")
        uid = 100001
        sbot._abuse_last_delivery.pop(uid, None)
        sbot._abuse_strikes.pop(uid, None)
        fake_db._strikes.pop(uid, None)
        msg = FakeMessage(uid)
        result = await sbot._check_and_record_rapid_request(client, msg, uid, "bot1")
        record("No prior delivery → allow (return False)", result is False)

        # ── Test 2: Re-request within cooldown → Strike 1 (warn, block) ──────
        print("\n=== Test: Rapid re-request within 60s → Strike 1 ===")
        sbot._abuse_last_delivery[uid] = NOW  # just delivered
        msg = FakeMessage(uid)
        result = await sbot._check_and_record_rapid_request(client, msg, uid, "bot1")
        record("Rapid request → return True (block)", result is True)
        record("Strike 1 recorded in memory", sbot._abuse_strikes.get(uid, 0) == 1)
        record("Warning message sent", msg.reply_text.called)

        # ── Test 3: Second rapid request → Strike 2 (final warn, block) ──────
        print("\n=== Test: 2nd rapid request → Strike 2 ===")
        sbot._abuse_last_delivery[uid] = NOW  # still within window
        msg2 = FakeMessage(uid)
        result2 = await sbot._check_and_record_rapid_request(client, msg2, uid, "bot1")
        record("2nd rapid request → return True (block)", result2 is True)
        record("Strike 2 recorded in memory", sbot._abuse_strikes.get(uid, 0) == 2)
        record("Warning message sent again", msg2.reply_text.called)

        # ── Test 4: Third rapid request → Strike 3 → Silent ban ─────────────
        print("\n=== Test: 3rd rapid request → Strike 3 → Ban ===")
        sbot._abuse_last_delivery[uid] = NOW
        msg3 = FakeMessage(uid)
        result3 = await sbot._check_and_record_rapid_request(client, msg3, uid, "bot1")
        record("3rd rapid request → return True (block, silent)", result3 is True)
        record("NO warning message sent (silent ban)", not msg3.reply_text.called)
        record("User banned in DB", uid in fake_db._bans)
        ban_reason = fake_db._bans.get(uid, "")
        record("Ban reason contains 'strike'", "strike" in ban_reason.lower())
        record("Strike data cleared from memory", uid not in sbot._abuse_strikes)
        record("Delivery ts cleared from memory", uid not in sbot._abuse_last_delivery)

        # ── Test 5: Request outside cooldown window → reset strikes, allow ────
        print("\n=== Test: Request after cooldown expiry → reset and allow ===")
        uid2 = 100002
        sbot._abuse_last_delivery[uid2] = NOW - 120  # 2 minutes ago (outside 60s window)
        sbot._abuse_strikes[uid2] = 2  # had 2 strikes
        msg4 = FakeMessage(uid2)
        result4 = await sbot._check_and_record_rapid_request(client, msg4, uid2, "bot1")
        record("Old cooldown expired → return False (allow)", result4 is False)
        record("Strikes reset to 0", sbot._abuse_strikes.get(uid2, 0) == 0)

        # ── Test 6: Owner is always exempt ─────────────────────────────────────
        print("\n=== Test: Owner is exempt from abuse detection ===")
        uid3 = 999999  # owner
        sbot._abuse_last_delivery[uid3] = NOW  # just delivered
        sbot._abuse_strikes[uid3] = 2
        msg5 = FakeMessage(uid3)
        # Patch is_any_owner to return True for this test
        with patch('plugins.banned._is_any_owner', new=AsyncMock(return_value=True)):
            result5 = await sbot._check_and_record_rapid_request(client, msg5, uid3, "bot1")
        record("Owner → always return False (exempt)", result5 is False)

    # ── Summary ────────────────────────────────────────────────────────────────
    print("=" * 55)
    passed = sum(1 for s, _, _ in results if s == PASS)
    total  = len(results)
    print(f"Results: {passed}/{total} tests passed")
    if passed == total:
        print("[OK] All tests PASSED!")
    else:
        failed = [(n, d) for s, n, d in results if s == FAIL]
        print(f"[!!] {len(failed)} test(s) FAILED:")
        for n, d in failed:
            print(f"   - {n}")
    return passed == total


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
