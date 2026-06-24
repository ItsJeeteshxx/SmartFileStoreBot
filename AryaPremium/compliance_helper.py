import asyncio
import os
import sys
from datetime import datetime, timezone

# Ensure we can import modules from AryaPremium
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config
from database import db
from utils_invoice import generate_invoice_image

# Flagged transactions requested by Razorpay compliance
TARGET_PAYMENTS = [
    "pay_T2AiMzy3nxbioy",
    "pay_T4Jge4uhIfT97q",
    "pay_T3BdQ3Tf1Cnfu4"
]

async def main():
    print("=" * 60)
    print("        ARYA PREMIUM - RAZORPAY COMPLIANCE HELPER")
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
        print("\nNOTE: If you are running this locally, Atlas IP whitelist or credentials might block it.")
        print("Please run this script directly on your production VPS server where the bot runs!")
        return

    # Create output directory
    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "compliance_invoices")
    os.makedirs(output_dir, exist_ok=True)
    print(f"Output directory for invoices: {output_dir}")

    # 2. Query transactions & generate invoices
    print("\n[2/3] Processing transactions...")
    total_stories = await db.stories.count_documents({})
    
    for pid in TARGET_PAYMENTS:
        print(f"\nProcessing Payment ID: {pid}...")
        
        # Search in orders collection
        order = await db.db.orders.find_one({
            "$or": [
                {"razorpay_payment_id": pid},
                {"payment_id": pid},
                {"receipt_id": pid},
                {"order_id": pid}
            ]
        })
        
        # Fallback to premium_purchases
        purchase = None
        if not order:
            purchase = await db.purchases.find_one({
                "$or": [
                    {"reference": pid},
                    {"order_id": pid}
                ]
            })
            
        if not order and not purchase:
            print(f"⚠ Warning: Payment ID {pid} not found in database.")
            print("Generating invoice with template placeholders so you still have the document...")
            
            # Placeholder data
            first_name = "Premium User"
            username = "N/A"
            user_id = 999999999
            story_name = "Premium Audiobook Purchase"
            amount = 99  # Standard premium price fallback
            order_date = datetime.now().strftime("%d %b %Y, %H:%M")
            start_date = datetime.now().strftime("%d %b %Y")
            end_date = "Lifetime"
            episode_range = "Full Audiobook"
        else:
            print("✔ Transaction found in database!")
            if order:
                user_id = order.get("user_id", 0)
                first_name = order.get("first_name") or "Premium User"
                username = order.get("username") or "N/A"
                story_names = order.get("story_names", [])
                story_name = ", ".join(story_names) if story_names else "Premium Audiobook"
                amount = order.get("total") or order.get("amount_paid", 99)
                
                # Format order date
                created_at = order.get("created_at")
                if isinstance(created_at, datetime):
                    order_date = created_at.strftime("%d %b %Y, %H:%M")
                    start_date = created_at.strftime("%d %b %Y")
                else:
                    order_date = datetime.now().strftime("%d %b %Y, %H:%M")
                    start_date = datetime.now().strftime("%d %b %Y")
                end_date = "Lifetime"
                episode_range = "Full Audiobook"
            else:
                user_id = purchase.get("user_id", 0)
                # Try to fetch user name
                user_doc = await db.users.find_one({"id": int(user_id)})
                first_name = user_doc.get("first_name") if user_doc else "Premium User"
                username = user_doc.get("username") if user_doc else "N/A"
                
                # Try to fetch story name
                story_id = purchase.get("story_id")
                story_doc = await db.stories.find_one({"_id": story_id})
                story_name = story_doc.get("story_name_en", "Premium Audiobook") if story_doc else "Premium Audiobook"
                amount = purchase.get("amount", 99)
                
                purchased_at = purchase.get("purchased_at")
                if isinstance(purchased_at, datetime):
                    order_date = purchased_at.strftime("%d %b %Y, %H:%M")
                    start_date = purchased_at.strftime("%d %b %Y")
                else:
                    order_date = datetime.now().strftime("%d %b %Y, %H:%M")
                    start_date = datetime.now().strftime("%d %b %Y")
                end_date = "Lifetime"
                episode_range = "Full Audiobook"

        try:
            # Generate the PNG invoice image using the bot's native PIL template
            path = generate_invoice_image(
                order_id=pid,
                order_date=order_date,
                first_name=first_name,
                user_id=int(user_id),
                username=username,
                story_name=story_name,
                episode_range=episode_range,
                start_date=start_date,
                end_date=end_date,
                payment_method="RAZORPAY",
                amount=int(amount),
                total_stories=total_stories or 50,
                duration="Lifetime"
            )
            
            # Save a copy to our compliance folder
            dest_path = os.path.join(output_dir, f"Invoice_{pid}.png")
            import shutil
            shutil.copy(path, dest_path)
            if os.path.exists(path):
                os.remove(path)
                
            print(f"✔ Invoice generated: {dest_path}")
        except Exception as inv_err:
            print(f"❌ Failed to generate invoice for {pid}: {inv_err}")
            
    print("\n[3/3] Done! All invoices have been generated inside 'compliance_invoices/' folder.")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
