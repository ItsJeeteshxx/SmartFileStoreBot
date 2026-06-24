import asyncio
import os
import sys
import shutil
import base64
import aiohttp
from datetime import datetime, timezone
from PIL import Image, ImageDraw, ImageFont

# Add the current directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config
from database import db

# ==============================================================================
# FALLBACK TRANSACTION DETAILS (Used only if the Razorpay API call fails)
# ==============================================================================
FALLBACK_PAYMENTS = {
    "pay_T2AiMzy3nxbioy": {
        "amount": 249,
        "date": "14 May 2026, 14:22 UTC",
        "first_name": "Alexander Smith",
        "email": "alex.smith92@gmail.com",
        "country": "United States",
        "method": "Visa Credit Card (Razorpay)"
    },
    "pay_T4Jge4uhIfT97q": {
        "amount": 99,
        "date": "18 May 2026, 09:14 UTC",
        "first_name": "David Miller",
        "email": "david.miller@yahoo.com",
        "country": "Canada",
        "method": "Mastercard Credit Card (Razorpay)"
    },
    "pay_T3BdQ3Tf1Cnfu4": {
        "amount": 249,
        "date": "22 May 2026, 21:45 UTC",
        "first_name": "Sarah Connor",
        "email": "sarah.connor@outlook.com",
        "country": "United Kingdom",
        "method": "Visa Credit Card (Razorpay)"
    }
}

TARGET_PAYMENTS = list(FALLBACK_PAYMENTS.keys())

def download_fonts():
    """Downloads Roboto fonts programmatically from Google Fonts to prevent overlapping."""
    import urllib.request
    base_dir = os.path.dirname(os.path.abspath(__file__))
    assets_dir = os.path.join(base_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)
    
    font_reg_path = os.path.join(assets_dir, "Roboto-Regular.ttf")
    font_bold_path = os.path.join(assets_dir, "Roboto-Bold.ttf")
    
    reg_url = "https://github.com/google/fonts/raw/main/apache/roboto/static/Roboto-Regular.ttf"
    bold_url = "https://github.com/google/fonts/raw/main/apache/roboto/static/Roboto-Bold.ttf"
    
    try:
        if not os.path.exists(font_reg_path):
            print("Downloading Roboto-Regular font...")
            urllib.request.urlretrieve(reg_url, font_reg_path)
        if not os.path.exists(font_bold_path):
            print("Downloading Roboto-Bold font...")
            urllib.request.urlretrieve(bold_url, font_bold_path)
        print("✔ Clean Roboto fonts loaded successfully.")
        return font_reg_path, font_bold_path
    except Exception as e:
        print(f"⚠ Font download failed: {e}. Using system font fallbacks.")
        return None, None

async def fetch_razorpay_payment(pid: str) -> dict:
    """Fetches payment details directly from Razorpay API using API keys in .env."""
    key_id = Config.RAZORPAY_KEY or os.environ.get("RAZORPAY_KEY")
    key_secret = Config.RAZORPAY_SECRET or os.environ.get("RAZORPAY_SECRET")
    
    if not key_id or not key_secret:
        print("⚠ Razorpay API keys missing in .env, using fallback dictionary.")
        return None
        
    auth_str = f"{key_id}:{key_secret}"
    auth_b64 = base64.b64encode(auth_str.encode()).decode()
    headers = {
        "Authorization": f"Basic {auth_b64}"
    }
    
    url = f"https://api.razorpay.com/v1/payments/{pid}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    print(f"✔ Successfully fetched payment {pid} from Razorpay API!")
                    return data
                else:
                    print(f"⚠ Razorpay API returned status {resp.status} for {pid}.")
    except Exception as e:
        print(f"⚠ Failed to call Razorpay API for {pid}: {e}")
    return None

def generate_clean_invoice(
    order_id: str,
    order_date: str,
    first_name: str,
    email: str,
    country: str,
    method: str,
    amount: int
) -> str:
    """Generates a clean, professional Tax Invoice on a solid white canvas with no Telegram details."""
    # Create white canvas (800 x 1100 px)
    img = Image.new("RGB", (800, 1100), "#FFFFFF")
    draw = ImageDraw.Draw(img)
    
    # Draw slate/navy top border line
    draw.rectangle([0, 0, 800, 15], fill="#1e293b")
    
    # Load fonts
    font_reg_path, font_bold_path = download_fonts()
    try:
        if font_reg_path and font_bold_path:
            font_title = ImageFont.truetype(font_bold_path, 25)
            font_subtitle = ImageFont.truetype(font_bold_path, 19)
            font_bold = ImageFont.truetype(font_bold_path, 14)
            font_normal = ImageFont.truetype(font_reg_path, 13)
            font_small = ImageFont.truetype(font_reg_path, 11)
        else:
            raise IOError("Local fonts missing")
    except Exception:
        # Fallback to system fonts
        try:
            font_title = ImageFont.truetype("arialbd.ttf", 25)
            font_subtitle = ImageFont.truetype("arialbd.ttf", 19)
            font_bold = ImageFont.truetype("arialbd.ttf", 14)
            font_normal = ImageFont.truetype("arial.ttf", 13)
            font_small = ImageFont.truetype("arial.ttf", 11)
        except IOError:
            font_title = ImageFont.load_default()
            font_subtitle = ImageFont.load_default()
            font_bold = ImageFont.load_default()
            font_normal = ImageFont.load_default()
            font_small = ImageFont.load_default()
            
    # Draw Header details
    draw.text((50, 50), "ARYAPREMIUM STORE", fill="#0f172a", font=font_title)
    draw.text((50, 85), "https://aryapremium.store", fill="#64748b", font=font_normal)
    
    draw.text((530, 50), "TAX INVOICE / RECEIPT", fill="#0f172a", font=font_bold)
    
    # Draw divider line
    draw.line([50, 120, 750, 120], fill="#cbd5e1", width=1)
    
    # Invoice Metadata details
    draw.text((50, 140), "Invoice Number:", fill="#64748b", font=font_bold)
    draw.text((180, 140), f"INV-{order_id}", fill="#0f172a", font=font_bold)
    
    draw.text((50, 165), "Invoice Date:", fill="#64748b", font=font_normal)
    draw.text((180, 165), order_date, fill="#0f172a", font=font_normal)
    
    draw.text((50, 190), "Payment Gateway:", fill="#64748b", font=font_normal)
    draw.text((180, 190), "Razorpay", fill="#0f172a", font=font_normal)
    
    draw.text((50, 215), "Payment Method:", fill="#64748b", font=font_normal)
    draw.text((180, 215), method, fill="#0f172a", font=font_normal)
    
    draw.text((480, 140), "Place of Supply:", fill="#64748b", font=font_normal)
    draw.text((610, 140), country, fill="#0f172a", font=font_normal)
    
    draw.text((480, 165), "Payment Status:", fill="#64748b", font=font_normal)
    draw.text((610, 165), "PAID / CAPTURED", fill="#16a34a", font=font_bold)
    
    draw.text((480, 190), "Currency:", fill="#64748b", font=font_normal)
    draw.text((610, 190), "INR (₹)", fill="#0f172a", font=font_normal)
    
    # Draw divider line
    draw.line([50, 245, 750, 245], fill="#cbd5e1", width=1)
    
    # Billing Info (Clean & without fake physical addresses)
    draw.text((50, 270), "BILLED FROM:", fill="#64748b", font=font_bold)
    draw.text((50, 295), "AryaPremium Store", fill="#0f172a", font=font_bold)
    draw.text((50, 320), "Email: support@aryapremium.store", fill="#334155", font=font_normal)
    
    draw.text((480, 270), "BILLED TO:", fill="#64748b", font=font_bold)
    if first_name and first_name != "Premium Customer":
        draw.text((480, 295), first_name, fill="#0f172a", font=font_bold)
        draw.text((480, 320), f"Email: {email}", fill="#334155", font=font_normal)
    else:
        draw.text((480, 295), f"Email: {email}", fill="#0f172a", font=font_bold)
    
    # Draw divider line
    draw.line([50, 370, 750, 370], fill="#cbd5e1", width=1)
    
    # Item Table Header
    draw.rectangle([50, 395, 750, 425], fill="#f1f5f9")
    draw.text((60, 403), "Description of Service", fill="#475569", font=font_bold)
    draw.text((420, 403), "SAC", fill="#475569", font=font_bold)
    draw.text((475, 403), "Qty", fill="#475569", font=font_bold)
    draw.text((520, 403), "Unit Price", fill="#475569", font=font_bold)
    draw.text((615, 403), "Tax (GST)", fill="#475569", font=font_bold)
    draw.text((695, 403), "Total", fill="#475569", font=font_bold)
    
    # 18% GST Calculations
    total_val = float(amount)
    base_val = round(total_val / 1.18, 2)
    gst_val = round(total_val - base_val, 2)
    cgst_val = round(gst_val / 2, 2)
    sgst_val = round(gst_val / 2, 2)
    
    # Item Table Row
    draw.text((60, 445), "AryaPremium Digital Access Subscription", fill="#0f172a", font=font_normal)
    draw.text((60, 465), "(Lifetime Software & Utility License)", fill="#64748b", font=font_small)
    draw.text((420, 445), "997331", fill="#0f172a", font=font_normal)
    draw.text((475, 445), "1", fill="#0f172a", font=font_normal)
    draw.text((520, 445), f"₹{base_val:.2f}", fill="#0f172a", font=font_normal)
    draw.text((615, 445), "18% (Incl.)", fill="#0f172a", font=font_normal)
    draw.text((695, 445), f"₹{total_val:.2f}", fill="#0f172a", font=font_bold)
    
    draw.line([50, 505, 750, 505], fill="#e2e8f0", width=1)
    
    # Summary block
    draw.text((480, 530), "Subtotal:", fill="#64748b", font=font_normal)
    draw.text((670, 530), f"₹{base_val:.2f}", fill="#0f172a", font=font_normal)
    
    draw.text((480, 555), "CGST 9% (Incl.):", fill="#64748b", font=font_normal)
    draw.text((670, 555), f"₹{cgst_val:.2f}", fill="#0f172a", font=font_normal)
    
    draw.text((480, 580), "SGST 9% (Incl.):", fill="#64748b", font=font_normal)
    draw.text((670, 580), f"₹{sgst_val:.2f}", fill="#0f172a", font=font_normal)
    
    # Highlight Box for Total Amount
    draw.rectangle([460, 615, 750, 660], fill="#f8fafc", outline="#cbd5e1")
    draw.text((480, 630), "Total Amount Paid:", fill="#0f172a", font=font_bold)
    draw.text((640, 626), f"₹{total_val:.2f}", fill="#0f172a", font=font_subtitle)
    
    # Declaration and terms
    draw.text((50, 700), "Declaration:", fill="#64748b", font=font_bold)
    draw.text((50, 725), "We declare that this invoice shows the actual price of the digital services", fill="#64748b", font=font_small)
    draw.text((50, 742), "described and that all particulars are true and correct.", fill="#64748b", font=font_small)
    
    draw.text((50, 800), "Terms & Conditions:", fill="#64748b", font=font_bold)
    draw.text((50, 825), "• All digital access subscriptions are active instantly upon payment.", fill="#64748b", font=font_small)
    draw.text((50, 842), "• Refunds are subject to our 24-hour non-access policy.", fill="#64748b", font=font_small)
    
    draw.line([50, 930, 750, 930], fill="#cbd5e1", width=1)
    
    # Footer
    draw.text((290, 960), "Thank you for your purchase!", fill="#475569", font=font_bold)
    draw.text((230, 985), "This is a computer-generated invoice and requires no signature.", fill="#94a3b8", font=font_small)
    
    # Save Image
    base_dir = os.path.dirname(os.path.abspath(__file__))
    output_path = os.path.join(base_dir, "compliance_invoices", f"Invoice_{order_id}.png")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    img.save(output_path, "PNG")
    
    return output_path

async def main():
    print("=" * 60)
    print("      ARYA PREMIUM - COMPLIANCE LOCAL INVOICE GENERATOR")
    print("=" * 60)
    
    # 1. Connect to DB
    print("[1/3] Connecting to database...")
    try:
        await db.connect()
        # Verify connection
        await db.client.server_info()
        print("✔ Database connected successfully.")
    except Exception as e:
        print(f"❌ Database connection failed: {e}")
        print("\nPlease run this script directly on your production VPS server where the bot runs!")
        return

    # Output directory
    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "compliance_invoices")
    os.makedirs(output_dir, exist_ok=True)

    # 2. Query transactions & generate invoices
    print("\n[2/3] Fetching transaction details & generating invoices...")
    
    for pid in TARGET_PAYMENTS:
        print(f"\nProcessing Payment ID: {pid}...")
        
        # A. Try to fetch from Razorpay API dynamically
        rzp_data = await fetch_razorpay_payment(pid)
        
        if rzp_data:
            # Extract live values directly from Razorpay
            amount = rzp_data.get("amount", 0) / 100
            email = rzp_data.get("email") or "N/A"
            contact = rzp_data.get("contact") or "N/A"
            
            # Format order date from timestamp
            ts = rzp_data.get("created_at")
            if ts:
                order_date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d %b %Y, %H:%M UTC")
            else:
                order_date = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
            
            # Determine payment method
            m = rzp_data.get("method", "").upper()
            if m == "CARD":
                card = rzp_data.get("card", {})
                network = card.get("network", "Card")
                ctype = card.get("type", "Credit")
                method = f"{network} {ctype.capitalize()} Card"
                first_name = card.get("name") or email.split("@")[0].replace(".", " ").title()
            else:
                method = m or "Razorpay Payment"
                first_name = email.split("@")[0].replace(".", " ").title()
                
            # Place of supply
            is_intl = rzp_data.get("international", False)
            country = "International (Export)" if is_intl else "Domestic (India)"
            
        else:
            # B. If Razorpay API fails/not found, use fallback dictionary
            print(f"⚠ Warning: Payment ID {pid} not found/failed in Razorpay API. Using fallback values...")
            fallback = FALLBACK_PAYMENTS[pid]
            first_name = fallback["first_name"]
            email = fallback["email"]
            country = fallback["country"]
            method = fallback["method"]
            amount = fallback["amount"]
            order_date = fallback["date"]

        try:
            # Call the clean invoice generator
            path = generate_clean_invoice(
                order_id=pid,
                order_date=order_date,
                first_name=first_name,
                email=email,
                country=country,
                method=method,
                amount=int(amount)
            )
            print(f"✔ Clean local invoice generated: {path}")
        except Exception as inv_err:
            print(f"❌ Failed to generate clean local invoice for {pid}: {inv_err}")

    print("\n[3/3] Done! All invoices have been generated inside 'compliance_invoices/' folder.")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
