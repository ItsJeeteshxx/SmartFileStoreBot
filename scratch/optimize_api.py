import os

filepath = r"c:\Users\User\Downloads\AryaBotNew\TryAryaBot\mini_app_api.py"

with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

target = """@api_router.get("/admin/stats")
async def get_admin_stats(telegram_id: str):
    \"\"\"Fetches full admin analysis dashboard.\"\"\"
    from AryaPremium.config import Config
    
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized as Admin")
            
        arya_db = app.state.db"""

replacement = """# --- ADMIN STATS CACHE ---
import time
_admin_stats_cache = None
_admin_stats_cache_time = 0.0
ADMIN_STATS_CACHE_TTL = 300.0  # 5 minutes cache
# -------------------------

@api_router.get("/admin/stats")
async def get_admin_stats(telegram_id: str, force: bool = Query(False)):
    \"\"\"Fetches full admin analysis dashboard.\"\"\"
    global _admin_stats_cache, _admin_stats_cache_time
    from AryaPremium.config import Config
    
    try:
        user_id_int = int(telegram_id) if telegram_id.isdigit() else telegram_id
        if not is_admin(str(telegram_id)):
            raise HTTPException(status_code=403, detail="Not authorized as Admin")
            
        now = time.time()
        if not force and _admin_stats_cache and (now - _admin_stats_cache_time < ADMIN_STATS_CACHE_TTL):
            return {
                "success": True,
                "data": _admin_stats_cache
            }
            
        arya_db = app.state.db"""

if target in content:
    content = content.replace(target, replacement, 1)
    print("Step 1 replacement successful!")
else:
    print("ERROR: Target start not found!")

# Now replace the return statement with cache saving logic
target_end = """        return {
            "success": True,
            "data": {
                "total_users": total_users_count,
                "bot_users": bot_users_count,
                "miniapp_users": miniapp_users_count,
                "total_stories": total_stories,
                "total_revenue": total_revenue,
                "miniapp_revenue": miniapp_revenue,
                "bot_revenue": bot_revenue,
                "recent_feedback": feedbacks,
                "recent_orders": orders,
                "page_views": page_views_count
            }
        }
    except HTTPException:"""

replacement_end = """        result_data = {
            "total_users": total_users_count,
            "bot_users": bot_users_count,
            "miniapp_users": miniapp_users_count,
            "total_stories": total_stories,
            "total_revenue": total_revenue,
            "miniapp_revenue": miniapp_revenue,
            "bot_revenue": bot_revenue,
            "recent_feedback": feedbacks,
            "recent_orders": orders,
            "page_views": page_views_count
        }
        
        # Save cache
        _admin_stats_cache = result_data
        _admin_stats_cache_time = now
        
        return {
            "success": True,
            "data": result_data
        }
    except HTTPException:"""

if target_end in content:
    content = content.replace(target_end, replacement_end, 1)
    print("Step 2 replacement successful!")
else:
    print("ERROR: Target end not found!")

with open(filepath, "w", encoding="utf-8") as f:
    f.write(content)
print("File updated successfully!")
