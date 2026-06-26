import asyncio
import os
import sys
import re
import imaplib
import email

# Add parent directories to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "AryaPremium"))

def extract_payer_name_from_email(body: str) -> str:
    """Helper to extract sender name from slice email notifications."""
    match = re.search(r'from\s+([a-zA-Z\s\.\-\&]{3,40})(?:\s*\(upi|\s+upi|\s+via|\s+on\s+\d|\s+in\s+your|\r|\n|\.|$)', body, re.IGNORECASE)
    if match:
        name = match.group(1).strip()
        name = re.sub(r'\s+', ' ', name)
        words_to_skip = {"your", "my", "slice", "account", "bank", "upi", "card", "rs", "rupees", "inr", "customer", "user", "payment"}
        if name.lower() not in words_to_skip and len(name) >= 3:
            return name.title()
    return ""

def get_email_body(msg) -> str:
    """Helper to extract text body from email message."""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdisp = str(part.get("Content-Disposition"))
            if ctype == "text/plain" and "attachment" not in cdisp:
                try:
                    return part.get_payload(decode=True).decode("utf-8", errors="ignore")
                except Exception:
                    pass
            elif ctype == "text/html" and "attachment" not in cdisp:
                try:
                    html_content = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                    text_content = re.sub(r'<[^>]+>', ' ', html_content)
                    text_content = re.sub(r'\s+', ' ', text_content)
                    return text_content
                except Exception:
                    pass
    else:
        try:
            return msg.get_payload(decode=True).decode("utf-8", errors="ignore")
        except Exception:
            pass
    return ""

async def main():
    try:
        from AryaPremium.database import db
        from AryaPremium.config import Config
    except ImportError:
        try:
            from database import db
            from config import Config
        except ImportError:
            print("Error: Could not import database or config!")
            return

    print("Connecting to database...")
    await db.connect()
    
    cfg = await db.db.mini_app_config.find_one({"_key": "feature_toggles"}) or {}
    gmail_user = cfg.get("gmail_user", "").strip()
    gmail_password = cfg.get("gmail_app_password", "").strip()
    
    if not gmail_user or not gmail_password:
        print("Error: Gmail credentials are not configured in your settings/database!")
        return
        
    print(f"Gmail User: {gmail_user}")
    print("Connecting to Gmail IMAP...")
    
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(gmail_user, gmail_password)
        mail.select("INBOX")
        
        status, messages = mail.search(None, 'FROM', 'noreply@slice.bank.in')
        if status == "OK" and messages[0]:
            mail_ids = messages[0].split()
            print(f"Found {len(mail_ids)} emails from noreply@slice.bank.in")
            
            # Print last 5 emails
            for idx, mail_id in enumerate(reversed(mail_ids[-5:])):
                res_status, msg_data = mail.fetch(mail_id, "(RFC822)")
                if res_status != "OK":
                    continue
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        print("\n" + "="*80)
                        print(f"EMAIL #{idx+1}")
                        print(f"Subject: {msg.get('Subject')}")
                        print(f"Date: {msg.get('Date')}")
                        
                        body = get_email_body(msg)
                        print("-" * 50)
                        print("RAW BODY EXTRACT:")
                        print(body[:800])
                        print("-" * 50)
                        
                        # Test extraction regex
                        name = extract_payer_name_from_email(body)
                        print(f"EXTRACTED NAME: '{name}'")
                        print("="*80)
        else:
            print("No emails found from noreply@slice.bank.in!")
        
        mail.close()
        mail.logout()
    except Exception as e:
        print("IMAP Connection Error:", e)

if __name__ == "__main__":
    asyncio.run(main())
