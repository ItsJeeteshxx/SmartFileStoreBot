import asyncio
from motor.motor_asyncio import AsyncIOMotorClient

async def main():
    uri = "mongodb+srv://AryabyDeepti:GQOkCie8TUQ6cgcW@cluster0.d1gjtf6.mongodb.net/?appName=Cluster0"
    client = AsyncIOMotorClient(uri)
    db = client["arya"]
    
    # Let's count the documents in premium_feedback
    total = await db.premium_feedback.count_documents({})
    print(f"Total documents in premium_feedback: {total}")
    
    # Let's see some documents categories and text
    print("\nSample documents:")
    cursor = db.premium_feedback.find({}).limit(10)
    async for doc in cursor:
        print(f"ID: {doc.get('_id')}, Ticket ID: {doc.get('ticket_id')}, Category: {doc.get('category')}, Text: {doc.get('text')}, Status: {doc.get('status')}")
        
    client.close()

if __name__ == "__main__":
    asyncio.run(main())
