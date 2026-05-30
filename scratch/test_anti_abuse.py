"""
test_anti_abuse.py — Unit Tests for the 5-Strike Silent Ban Anti-Abuse System
=============================================================================
Tests the core logic of _check_and_record_rapid_request where:
1. LEGITIMATE users can request rapidly up to 4 times without warnings or blocks.
2. Only repeat scrapers (5+ rapid requests) are silently banned without any warning in chat.

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

fake_db = FakeDB()
results = []

def record(name, passed, detail=""):
    status = PASS if passed else FAIL
    results.append((status, name, detail))
    print(f"  {status}  {name}" + (f"  [{detail}]" if detail else ""))


async def main():
    # Import the function under test AFTER stubs are ready
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
            ABUSE_MAX_STRIKES=5,
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

        # ── Test 2: Rapid requests 1 to 4 → increment strike but allow normally without warning ──
        print("\n=== Test: Rapid requests 1 to 4 (within 60s cooldown) → strikes logged but delivery allowed ===")
        
        for strike in range(1, 5):
            sbot._abuse_last_delivery[uid] = NOW  # mark delivered
            msg_loop = FakeMessage(uid)
            result_loop = await sbot._check_and_record_rapid_request(client, msg_loop, uid, "bot1")
            
            record(f"Strike {strike} rapid request → allowed (returns False)", result_loop is False)
            record(f"Strike {strike} recorded in memory", sbot._abuse_strikes.get(uid, 0) == strike)
            record(f"No warning message sent to user on strike {strike}", not msg_loop.reply_text.called)

        # ── Test 3: Strike 5 → Exceeds limit → Silent Ban & Abort Delivery ────
        print("\n=== Test: Strike 5 → Exceeds limit → Silent ban & Abort ===")
        sbot._abuse_last_delivery[uid] = NOW
        msg_ban = FakeMessage(uid)
        result_ban = await sbot._check_and_record_rapid_request(client, msg_ban, uid, "bot1")
        
        record("Strike 5 rapid request → blocked (returns True)", result_ban is True)
        record("NO warning message sent to user (silent ban)", not msg_ban.reply_text.called)
        record("User banned in DB", uid in fake_db._bans)
        ban_reason = fake_db._bans.get(uid, "")
        record("Ban reason contains 'strike'", "strike" in ban_reason.lower())
        record("Strike data cleared from memory", uid not in sbot._abuse_strikes)
        record("Delivery ts cleared from memory", uid not in sbot._abuse_last_delivery)

        # ── Test 4: Request outside cooldown window → reset strikes, allow ────
        print("\n=== Test: Request after cooldown expiry → reset and allow ===")
        uid2 = 100002
        sbot._abuse_last_delivery[uid2] = NOW - 120  # 2 minutes ago (outside 60s window)
        sbot._abuse_strikes[uid2] = 3  # had 3 strikes
        msg4 = FakeMessage(uid2)
        result4 = await sbot._check_and_record_rapid_request(client, msg4, uid2, "bot1")
        record("Old cooldown expired → return False (allow)", result4 is False)
        record("Strikes reset to 0", sbot._abuse_strikes.get(uid2, 0) == 0)

        # ── Test 5: Owner is always exempt ─────────────────────────────────────
        print("\n=== Test: Owner is exempt from abuse detection ===")
        uid3 = 999999  # owner
        sbot._abuse_last_delivery[uid3] = NOW  # just delivered
        sbot._abuse_strikes[uid3] = 3
        msg5 = FakeMessage(uid3)
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
