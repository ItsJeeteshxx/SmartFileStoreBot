import re

with open('mini_app_api.py', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace(
    '''            buyers.append({
                "order_id": f"bot_{uid}",
                "user_id": uid,
                "username": u.get("username", "Unknown"),
                "first_name": u.get("first_name", ""),
                "amount": total_amt,
                "status": "paid",''',
    '''            buyers.append({
                "order_id": f"bot_{uid}",
                "user_id": uid,
                "username": u.get("username", "Unknown"),
                "first_name": u.get("first_name", ""),
                "photo_url": u.get("photo_url", ""),
                "amount": total_amt,
                "status": "paid",'''
)

content = content.replace(
    '''            try: uid_int = int(uid)
            except: uid_int = uid
            
            story_ids = doc.get("story_ids", [])''',
    '''            try: uid_int = int(uid)
            except: uid_int = uid
            
            u_doc = await arya_db.db.users.find_one({"id": uid_int})
            u_fname = u_doc.get("first_name", doc.get("first_name", "")) if u_doc else doc.get("first_name", "")
            u_uname = u_doc.get("username", doc.get("username", "Unknown")) if u_doc else doc.get("username", "Unknown")
            u_photo = u_doc.get("photo_url", "") if u_doc else ""
            
            story_ids = doc.get("story_ids", [])'''
)

content = content.replace(
    '''            amt = doc.get("total_amount", doc.get("amount", 0))''',
    '''            amt = doc.get("total_amount", doc.get("total", doc.get("amount", 0)))'''
)

content = content.replace(
    '''                "username": doc.get("username", "Unknown"),
                "first_name": doc.get("first_name", ""),
                "amount": amt,''',
    '''                "username": u_uname,
                "first_name": u_fname,
                "photo_url": u_photo,
                "amount": amt,'''
)

manual_purchase_code = '''
class ManualPurchase(BaseModel):
    telegram_id: str
    user_id: int
    first_name: str
    username: str
    story_id: str
    amount: float

@api_router.post("/admin/manual-purchase")
async def manual_purchase(data: ManualPurchase):
    from AryaPremium.config import Config
    try:
        user_id_int = int(data.telegram_id) if data.telegram_id.isdigit() else data.telegram_id
        if user_id_int not in Config.OWNER_IDS:
            raise HTTPException(status_code=403, detail="Not authorized")
            
        arya_db = app.state.db
        from bson.objectid import ObjectId
        
        # Verify story
        try:
            story = await arya_db.db.premium_stories.find_one({"_id": ObjectId(data.story_id)})
        except:
            story = await arya_db.db.premium_stories.find_one({"story_id": data.story_id})
            
        if not story:
            raise HTTPException(404, "Story not found")
            
        story_id_str = str(story["_id"])
        
        # Upsert user
        user = await arya_db.db.users.find_one({"id": data.user_id})
        if not user:
            await arya_db.db.users.insert_one({
                "id": data.user_id,
                "first_name": data.first_name,
                "username": data.username,
                "purchases": [story_id_str],
                "joined_date": datetime.now(timezone.utc)
            })
        else:
            await arya_db.db.users.update_one(
                {"id": data.user_id},
                {"$addToSet": {"purchases": story_id_str}}
            )
            
        # Insert Order
        await arya_db.db.orders.insert_one({
            "order_id": f"MANUAL_{data.user_id}_{int(datetime.now().timestamp())}",
            "user_id": data.user_id,
            "username": data.username,
            "first_name": data.first_name,
            "story_ids": [story_id_str],
            "story_names": [story.get("story_name_en", "")],
            "total": data.amount,
            "status": "paid",
            "source": "manual_admin",
            "created_at": datetime.now(timezone.utc)
        })
        
        return {"success": True}
    except Exception as e:
        logger.error(f"Manual purchase error: {e}")
        raise HTTPException(500, detail=str(e))

'''

if '@api_router.post("/admin/manual-purchase")' not in content:
    content = content.replace(
        '@api_router.post("/admin/buyers/{user_id}/action")',
        manual_purchase_code + '@api_router.post("/admin/buyers/{user_id}/action")'
    )

with open('mini_app_api.py', 'w', encoding='utf-8') as f:
    f.write(content)
print('Done!')
