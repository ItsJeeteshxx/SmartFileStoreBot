import asyncio
import sys
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

# Add current directory to path so we can import mini_app_api
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mini_app_api import calculate_promo_discount

# A simple runner for our tests
async def run_tests():
    print("=== Starting Promo Code Backend Verification ===")
    
    # 1. Setup mock database
    db_mock = MagicMock()
    db_mock.db = MagicMock()
    db_mock.db.premium_promo_codes = MagicMock()
    db_mock.db.premium_stories = MagicMock()
    db_mock.db.orders = MagicMock()
    db_mock.db.users = MagicMock()
    
    # Helper to setup database query results
    async def mock_find_one_promo(query):
        code = query.get("code")
        if code == "GLOBAL_PCT":
            return {
                "code": "GLOBAL_PCT",
                "type": "percentage",
                "value": 20.0,
                "active": True,
                "story_id": "global",
                "usage_limit": 100,
                "usage_count": 5
            }
        elif code == "GLOBAL_FLAT":
            return {
                "code": "GLOBAL_FLAT",
                "type": "flat",
                "value": 50.0,
                "active": True,
                "story_id": "global"
            }
        elif code == "STORY_LOCKED":
            return {
                "code": "STORY_LOCKED",
                "type": "percentage",
                "value": 50.0,
                "active": True,
                "story_id": "story123"
            }
        elif code == "EXPIRED":
            return {
                "code": "EXPIRED",
                "type": "percentage",
                "value": 10.0,
                "active": True,
                "story_id": "global",
                "expires_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            }
        elif code == "EXHAUSTED":
            return {
                "code": "EXHAUSTED",
                "type": "percentage",
                "value": 10.0,
                "active": True,
                "story_id": "global",
                "usage_limit": 5,
                "usage_count": 5
            }
        elif code == "INACTIVE":
            return {
                "code": "INACTIVE",
                "type": "percentage",
                "value": 10.0,
                "active": False,
                "story_id": "global"
            }
        elif code == "NEW_USER_ONLY":
            return {
                "code": "NEW_USER_ONLY",
                "type": "percentage",
                "value": 10.0,
                "active": True,
                "story_id": "global",
                "user_target": "new_only"
            }
        elif code == "BUYER_ONLY":
            return {
                "code": "BUYER_ONLY",
                "type": "percentage",
                "value": 10.0,
                "active": True,
                "story_id": "global",
                "user_target": "existing_only"
            }
        elif code == "INACTIVE_ONLY":
            return {
                "code": "INACTIVE_ONLY",
                "type": "percentage",
                "value": 10.0,
                "active": True,
                "story_id": "global",
                "user_target": "inactive_only"
            }
        elif code == "LIMIT_1":
            return {
                "code": "LIMIT_1",
                "type": "percentage",
                "value": 10.0,
                "active": True,
                "story_id": "global",
                "user_limit": 1
            }
        return None

    async def mock_find_one_story(query):
        story_id = query.get("story_id")
        _id = query.get("_id")
        
        if "$or" in query:
            for cond in query["$or"]:
                if "story_id" in cond:
                    story_id = cond["story_id"]
                if "_id" in cond:
                    _id = cond["_id"]
        
        # Mocking finding specific stories
        if story_id == "story123" or _id == "story123":
            return {
                "_id": "story123",
                "story_id": "story123",
                "story_name_en": "Romeo and Juliet",
                "price": 200.0
            }
        elif story_id == "story456" or _id == "story456":
            return {
                "_id": "story456",
                "story_id": "story456",
                "story_name_en": "Hamlet",
                "price": 150.0
            }
        return None

    async def mock_count_documents(query):
        user_id_clause = query.get("user_id")
        user_ids = []
        if isinstance(user_id_clause, dict) and "$in" in user_id_clause:
            user_ids = user_id_clause["$in"]
        else:
            user_ids = [user_id_clause]
            
        # Standardize matching user
        is_user = any(uid in [12345, "12345"] for uid in user_ids)
        is_new_user = any(uid in [67890, "67890"] for uid in user_ids)
        is_old_non_buyer = any(uid in [11111, "11111"] for uid in user_ids)
        is_new_non_buyer = any(uid in [22222, "22222"] for uid in user_ids)
        
        # Check if we are counting for specific promo code (usage per user limit)
        promo_code = query.get("promo_code")
        if promo_code == "LIMIT_1":
            if is_user:
                return 1 # already used once
            return 0
            
        status = query.get("status")
        if status == "paid":
            if is_user:
                return 5 # existing buyer
            if is_new_user or is_old_non_buyer or is_new_non_buyer:
                return 0 # new user
        return 0

    async def mock_find_one_user(query):
        uid = query.get("id")
        if uid == 11111:
            return {
                "id": 11111,
                "joined_date": datetime.now(timezone.utc) - timedelta(days=10) # registered >= 7 days ago
            }
        elif uid == 22222:
            return {
                "id": 22222,
                "joined_date": datetime.now(timezone.utc) - timedelta(days=2) # registered < 7 days ago
            }
        return None

    db_mock.db.premium_promo_codes.find_one = AsyncMock(side_effect=mock_find_one_promo)
    db_mock.db.premium_stories.find_one = AsyncMock(side_effect=mock_find_one_story)
    db_mock.db.orders.count_documents = AsyncMock(side_effect=mock_count_documents)
    db_mock.db.users.find_one = AsyncMock(side_effect=mock_find_one_user)

    # Test Cases
    # Case 1: Empty promo code code
    discount, err = await calculate_promo_discount(db_mock, "", ["story123"], 200.0)
    assert discount == 0.0 and err == "", f"Empty code failed: discount={discount}, err={err}"
    print("OK - Test 1: Empty promo code skipped correctly")

    # Case 2: Code not found
    discount, err = await calculate_promo_discount(db_mock, "UNKNOWN", ["story123"], 200.0)
    assert discount == 0.0 and err == "Promo code not found", f"Unknown code failed: discount={discount}, err={err}"
    print("OK - Test 2: Unknown promo code handled correctly")

    # Case 3: Inactive promo
    discount, err = await calculate_promo_discount(db_mock, "INACTIVE", ["story123"], 200.0)
    assert discount == 0.0 and err == "Promo code is inactive", f"Inactive code failed: discount={discount}, err={err}"
    print("OK - Test 3: Inactive promo code rejected correctly")

    # Case 4: Expired promo
    discount, err = await calculate_promo_discount(db_mock, "EXPIRED", ["story123"], 200.0)
    assert discount == 0.0 and err == "Promo code has expired", f"Expired code failed: discount={discount}, err={err}"
    print("OK - Test 4: Expired promo code rejected correctly")

    # Case 5: Exhausted usage limit
    discount, err = await calculate_promo_discount(db_mock, "EXHAUSTED", ["story123"], 200.0)
    assert discount == 0.0 and err == "Promo code usage limit reached", f"Exhausted code failed: discount={discount}, err={err}"
    print("OK - Test 5: Exhausted promo code usage limit rejected correctly")

    # Case 6: Valid global percentage discount
    discount, err = await calculate_promo_discount(db_mock, "GLOBAL_PCT", ["story123"], 200.0)
    assert discount == 40.0 and err == "", f"Global percentage failed: discount={discount}, err={err}"
    print("OK - Test 6: Global percentage promo code calculated correctly (20% of 200 = 40)")

    # Case 7: Valid global flat discount
    discount, err = await calculate_promo_discount(db_mock, "GLOBAL_FLAT", ["story123"], 200.0)
    assert discount == 50.0 and err == "", f"Global flat failed: discount={discount}, err={err}"
    print("OK - Test 7: Global flat promo code calculated correctly (50 flat of 200 = 50)")

    # Case 8: Story locked promo applied with correct story in cart
    discount, err = await calculate_promo_discount(db_mock, "STORY_LOCKED", ["story123", "story456"], 350.0)
    assert discount == 100.0 and err == "", f"Story locked matching failed: discount={discount}, err={err}"
    print("OK - Test 8: Story restricted promo code matches target story in cart correctly (50% of story123's 200 = 100)")

    # Case 9: Story locked promo applied with non-matching story in cart
    discount, err = await calculate_promo_discount(db_mock, "STORY_LOCKED", ["story456"], 150.0)
    assert discount == 0.0 and err == "Promo code is only valid for story: Romeo and Juliet", f"Story locked mismatch failed: discount={discount}, err={err}"
    print("OK - Test 9: Story restricted promo code rejected with correct warning description on cart mismatch")

    # Case 10: New User Only promo code on existing buyer (should fail)
    discount, err = await calculate_promo_discount(db_mock, "NEW_USER_ONLY", ["story123"], 200.0, 12345)
    assert discount == 0.0 and "only valid for new users" in err, f"New User only failed for buyer: discount={discount}, err={err}"
    print("OK - Test 10: New user promo on buyer rejected correctly")

    # Case 11: New User Only promo code on new user (should succeed)
    discount, err = await calculate_promo_discount(db_mock, "NEW_USER_ONLY", ["story123"], 200.0, 67890)
    assert discount == 20.0 and err == "", f"New User only failed for new user: discount={discount}, err={err}"
    print("OK - Test 11: New user promo on new user accepted correctly")

    # Case 12: Buyer Only promo code on new user (should fail)
    discount, err = await calculate_promo_discount(db_mock, "BUYER_ONLY", ["story123"], 200.0, 67890)
    assert discount == 0.0 and "only valid for existing buyers" in err, f"Buyer only failed for new user: discount={discount}, err={err}"
    print("OK - Test 12: Buyer only promo on new user rejected correctly")

    # Case 13: Buyer Only promo code on buyer (should succeed)
    discount, err = await calculate_promo_discount(db_mock, "BUYER_ONLY", ["story123"], 200.0, 12345)
    assert discount == 20.0 and err == "", f"Buyer only failed for buyer: discount={discount}, err={err}"
    print("OK - Test 13: Buyer only promo on buyer accepted correctly")

    # Case 14: Inactive Only promo code on new user registered recently (should fail)
    discount, err = await calculate_promo_discount(db_mock, "INACTIVE_ONLY", ["story123"], 200.0, 22222)
    assert discount == 0.0 and "only valid for older users" in err, f"Inactive only failed for recent user: discount={discount}, err={err}"
    print("OK - Test 14: Inactive promo on recent user rejected correctly")

    # Case 15: Inactive Only promo code on new user registered > 7 days ago (should succeed)
    discount, err = await calculate_promo_discount(db_mock, "INACTIVE_ONLY", ["story123"], 200.0, 11111)
    assert discount == 20.0 and err == "", f"Inactive only failed for old user: discount={discount}, err={err}"
    print("OK - Test 15: Inactive promo on old non-buyer accepted correctly")

    # Case 16: Per-user usage limit promo code on user who already used it (should fail)
    discount, err = await calculate_promo_discount(db_mock, "LIMIT_1", ["story123"], 200.0, 12345)
    assert discount == 0.0 and "already used" in err, f"Limit 1 failed for user: discount={discount}, err={err}"
    print("OK - Test 16: Per-user usage limit check rejected correctly")

    # Case 17: Per-user usage limit promo code on user who hasn't used it (should succeed)
    discount, err = await calculate_promo_discount(db_mock, "LIMIT_1", ["story123"], 200.0, 67890)
    assert discount == 20.0 and err == "", f"Limit 1 failed for new user: discount={discount}, err={err}"
    print("OK - Test 17: Per-user usage limit check accepted correctly")

    print("ALL BACKEND PROMO CODE VALIDATION TESTS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    asyncio.run(run_tests())
