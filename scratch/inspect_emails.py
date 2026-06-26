import asyncio
import imaplib
import email
from motor.motor_asyncio import AsyncIOMotorClient

async def inspect():
    db_uri = "mongodb+srv://AryabyDeepti:GQOkCie8TUQ6cgcW@cluster0.d1gjtf6.mongodb.net/?appName=Cluster0"
    client = AsyncIOMotorClient(db_uri)
    db = client["forward-bot"] # Let's check both databases: forward-bot and arya
    
    cfg = await db.mini_app_config.find_one({"_key": "feature_toggles"})
    if not cfg:
        db = client["arya"]
        cfg = await db.mini_app_config.find_one({"_key": "feature_toggles"})
        
    if not cfg:
        print("Could not find mini_app_config in arya or forward-bot!")
        return
        
    gmail_user = cfg.get("gmail_user", "").strip()
    gmail_password = cfg.get("gmail_app_password", "").strip()
    
    print("Gmail User:", gmail_user)
    if not gmail_user or not gmail_password:
        print("Gmail credentials not configured in DB!")
        return
        
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(gmail_user, gmail_password)
        mail.select("INBOX")
        
        # Search for recent emails from slice
        status, messages = mail.search(None, 'FROM', 'noreply@slice.bank.in')
        if status == "OK" and messages[0]:
            mail_ids = messages[0].split()
            print(f"Found {len(mail_ids)} emails from slice.")
            # Print the 3 most recent emails
            for mail_id in reversed(mail_ids[-3:]):
                res_status, msg_data = mail.fetch(mail_id, "(RFC822)")
                if res_status != "OK":
                    continue
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        print("=" * 60)
                        print("From:", msg.get("From"))
                        print("Subject:", msg.get("Subject"))
                        print("Date:", msg.get("Date"))
                        
                        # Extract body
                        body = ""
                        if msg.is_multipart():
                            for part in msg.walk():
                                content_type = part.get_content_type()
                                content_disp = str(part.get("Content-Disposition"))
                                if content_type == "text/plain" and "attachment" not in content_disp:
                                    try:
                                        body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                                    except:
                                        pass
                                elif content_type == "text/html" and "attachment" not in content_disp:
                                    try:
                                        body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                                    except:
                                        pass
                        else:
                            try:
                                body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
                            except:
                                pass
                        
                        # Clean HTML tags if html
                        import re
                        clean_body = re.sub(r'<[^>]+>', ' ', body)
                        clean_body = re.sub(r'\s+', ' ', clean_body).strip()
                        print("Body Snippet (first 400 chars):", clean_body[:400])
                        print("Full Clean Body:", clean_body)
        else:
            print("No emails found from noreply@slice.bank.in!")
    except Exception as e:
        print("IMAP Error:", e)

if __name__ == "__main__":
    asyncio.run(inspect())
