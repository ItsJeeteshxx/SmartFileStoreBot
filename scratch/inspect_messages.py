import asyncio
import os
import sys

# Ensure project root is in path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from pyrogram import Client

async def main():
    api_id = Config.API_ID
    api_hash = Config.API_HASH
    
    print(f"API_ID: {api_id}")
    print(f"API_HASH: {api_hash}")
    
    client = Client("my_account", api_id=api_id, api_hash=api_hash)
    
    try:
        await client.start()
        print("Client started successfully!")
        
        chat_id = -1003862081761
        print(f"Fetching last 10 messages from chat {chat_id}...")
        
        async for msg in client.get_chat_history(chat_id, limit=10):
            print("="*60)
            print(f"Message ID: {msg.id}")
            print(f"Text/Caption: {msg.text or msg.caption}")
            print(f"message_thread_id (attrib): {getattr(msg, 'message_thread_id', None)}")
            print(f"reply_to_top_message_id (attrib): {getattr(msg, 'reply_to_top_message_id', None)}")
            
            # Print raw reply_to info if present
            reply_to = getattr(msg, "reply_to", None)
            if reply_to:
                print(f"reply_to (type={type(reply_to)}): {reply_to}")
                print(f"reply_to_msg_id: {getattr(reply_to, 'reply_to_msg_id', None)}")
                print(f"reply_to_top_id: {getattr(reply_to, 'reply_to_top_id', None)}")
                print(f"forum_topic: {getattr(reply_to, 'forum_topic', None)}")
            else:
                print("reply_to: None")
                
    except Exception as e:
        print(f"Error occurred: {e}", file=sys.stderr)
    finally:
        try:
            await client.stop()
        except:
            pass

if __name__ == "__main__":
    asyncio.run(main())
