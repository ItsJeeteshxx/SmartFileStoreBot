import os
import asyncio
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

# Attempt to load fonts
try:
    FONT_NORMAL = ImageFont.truetype("arial.ttf", 24)
    FONT_BOLD = ImageFont.truetype("arialbd.ttf", 26)
    FONT_LARGE = ImageFont.truetype("arialbd.ttf", 32)
except IOError:
    FONT_NORMAL = ImageFont.load_default()
    FONT_BOLD = ImageFont.load_default()
    FONT_LARGE = ImageFont.load_default()

COORDS = {
    "order_id": (330, 320),
    "order_date": (330, 360),
    "name": (330, 400),
    "user_id": (330, 440),
    "username": (330, 480),
    "story_name": (330, 540),
    "episode_range": (330, 580),
    "duration": (330, 640),
    "start_date": (330, 680),
    "end_date": (330, 720),
    "payment_method": (330, 780),
    "status": (330, 820),
    "subtotal": (330, 940),
    "discount": (330, 980),
    "tax_charges": (330, 1020),
    "total": (330, 1080),
    "more_stories": (150, 1160),
    "bottom": (400, 1250)
}

COLORS = {
    "default": (50, 50, 50),
    "blue": (30, 100, 200),
    "green": (40, 160, 40),
    "total": (0, 0, 0)
}

def limit_name(name, max_len=18):
    return name[:max_len] + "..." if len(name) > max_len else name

def generate_invoice_image(
    order_id: str,
    order_date: str,
    first_name: str,
    user_id: int,
    username: str,
    story_name: str,
    episode_range: str,
    start_date: str,
    end_date: str,
    payment_method: str,
    amount: int,
    total_stories: int,
    duration: str = "N/A"
) -> str:
    base_dir = os.path.dirname(__file__)
    possible_paths = [
        os.path.join(base_dir, "assets", "invoice_template_raw.png"),
        os.path.join(base_dir, "..", "assets", "invoice_template_raw.png"),
        os.path.abspath("assets/invoice_template_raw.png")
    ]
    
    template_path = None
    for p in possible_paths:
        if os.path.exists(p):
            template_path = p
            break
            
    if not template_path:
        # Give a clean error if user misses it during VPS deployment
        raise FileNotFoundError("Invoice template missing! Expected at: assets/invoice_template_raw.png")
        
    output_path = os.path.join(base_dir, "downloads", f"invoice_{order_id}.png")
        
    img = Image.open(template_path).convert("RGBA")
    draw = ImageDraw.Draw(img)
    
    name_str = limit_name(first_name.strip())
    
    raw_uname = username.strip() if username else ""
    if raw_uname and raw_uname.lower() != "none":
        uname_str = f"@{raw_uname}" if not raw_uname.startswith("@") else raw_uname
    else:
        uname_str = "N/A"
        
    tax_line = "₹0.00 / ₹0.00"
    
    # Hide the background {placeholders} with light rectangles
    bg_color = (253, 245, 250)
    # Order section
    draw.rectangle([320, 310, 550, 500], fill=bg_color)
    # Story section
    draw.rectangle([320, 530, 550, 600], fill=bg_color)
    # Date section
    draw.rectangle([320, 630, 550, 740], fill=bg_color)
    # Payment section
    draw.rectangle([320, 770, 550, 850], fill=bg_color)
    # Bill summary section
    draw.rectangle([320, 930, 550, 1040], fill=bg_color)
    # Total section
    draw.rectangle([320, 1070, 550, 1120], fill=bg_color)
    # Right side "Thank you" box price cover
    draw.rectangle([700, 630, 900, 750], fill=(255, 255, 255))
    
    def write_text(key, text, font=FONT_NORMAL, color=COLORS["default"]):
        if key in COORDS and text:
            draw.text(COORDS[key], str(text), fill=color, font=font)
            
    write_text("order_id", order_id)
    write_text("order_date", order_date)
    write_text("name", name_str)
    write_text("user_id", user_id)
    write_text("username", uname_str, color=COLORS["blue"])
    
    write_text("story_name", story_name, font=FONT_BOLD)
    write_text("episode_range", episode_range)
    
    write_text("duration", duration)
    write_text("start_date", start_date)
    write_text("end_date", end_date)
    
    write_text("payment_method", payment_method.upper())
    write_text("status", "PAID", font=FONT_BOLD, color=COLORS["green"])
    
    write_text("subtotal", f"₹{amount}")
    write_text("discount", "₹0.00")
    write_text("tax_charges", tax_line)
    write_text("total", f"₹{amount}", font=FONT_LARGE, color=COLORS["total"])
    
    promo_text = f"Explore {total_stories}+ premium stories.\nInstant access • Clean links • Fast delivery"
    write_text("more_stories", promo_text)
    
    bottom_text = "@AryaPremiumTG\n@mejeetx\n@ItsNewtonPlanet"
    write_text("bottom", bottom_text)
    
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    final = img.convert("RGB")
    final.save(output_path, "PNG")
    
    return output_path

async def send_invoice_to_user(client, user_id: int, order_id: str, amount: int, method: str, story: dict, checkout: dict = None):
    from database import db
    total_stories = await db.db.premium_stories.count_documents({})
    
    if not checkout: checkout = {}
    
    # Fetch user data from Telegram directly
    first_name = "User"
    username = ""
    try:
        user_obj = await client.get_users(user_id)
        first_name = limit_name(user_obj.first_name or user_obj.title or "User")
        username = user_obj.username or ""
    except Exception:
        pass
        
    if first_name == "User" and not username:
        try:
            db_user = await db.db.users.find_one({"id": int(user_id)})
            if db_user:
                first_name = limit_name(db_user.get("first_name") or "User")
                username = db_user.get("username") or ""
        except Exception:
            pass
        
    raw_uname = username.strip() if username else ""
    if raw_uname and raw_uname.lower() != "none":
        uname_display = f"@{raw_uname}" if not raw_uname.startswith("@") else raw_uname
    else:
        uname_display = "N/A"
    
    story_name = story.get("story_name_en", "Premium Story")
    
    # 1. EPISODE RANGE FIX
    start = story.get("start_id", 0)
    end = story.get("end_id", 0)
    if start and end:
        episode_range = f"{start} - {end}"
    else:
        episode_range = "N/A"
    
    # 4. DATE + DURATION SYSTEM
    order_date = datetime.utcnow().strftime("%d %b %Y, %H:%M")
    start_date = checkout.get("start_date") or datetime.utcnow().strftime("%d %b %Y")
    end_date = checkout.get("end_date") or "Lifetime"

    duration = "Lifetime"
    try:
        if end_date != "Lifetime" and start_date:
            d1 = datetime.strptime(start_date, "%d %b %Y")
            d2 = datetime.strptime(end_date, "%d %b %Y")
            duration = f"{(d2 - d1).days} Days"
    except Exception:
        pass
    
    try:
        path = generate_invoice_image(
            order_id=order_id,
            order_date=order_date,
            first_name=first_name,
            user_id=user_id,
            username=username,
            story_name=story_name,
            episode_range=episode_range,
            start_date=start_date,
            end_date=end_date,
            payment_method=method,
            amount=amount,
            total_stories=total_stories,
            duration=duration
        )
        
        # 10. CAPTION UPGRADE
        caption = (
            f"🧾 Payment Receipt\n\n"
            f"📚 {story_name}\n"
            f"👤 {first_name} ({uname_display})\n\n"
            f"💰 Amount: ₹{amount}\n"
            f"💳 Method: {method.upper()}\n\n"
            f"📅 Valid Till: {end_date}\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"✔ Payment Confirmed\n"
            f"📄 Invoice attached"
        )
        
        # 9. TELEGRAM DELIVERY (DOCUMENT)
        from pyrogram import enums
        await client.send_document(
            user_id,
            document=path,
            caption=caption
        )
        
        # 11. CLEANUP SYSTEM
        if os.path.exists(path):
            os.remove(path)
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[Invoice Error]: {e}")
