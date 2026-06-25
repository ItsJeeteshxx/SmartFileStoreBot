import asyncio
from motor.motor_asyncio import AsyncIOMotorClient

async def main():
    uri = "mongodb+srv://AryabyDeepti:GQOkCie8TUQ6cgcW@cluster0.d1gjtf6.mongodb.net/?appName=Cluster0"
    client = AsyncIOMotorClient(uri)
    db = client["AryaPremium"]
    
    pids = [
        "pay_T2AiMzy3nxbioy",
        "pay_T4Jge4uhIfT97q",
        "pay_T3BdQ3Tf1Cnfu4"
    ]
    
    collections = await db.list_collection_names()
    print(f"Collections in AryaPremium: {collections}")
    
    for pid in pids:
        print(f"\nSearching for {pid}...")
        for col_name in collections:
            col = db[col_name]
            doc = await col.find_one({
                "$or": [
                    {"payment_id": pid},
                    {"order_id": pid},
                    {"transaction_id": pid},
                    {"checkout_id": pid},
                    {"_id": pid},
                    {"razorpay_payment_id": pid}
                ]
            })
            if doc:
                print(f"Found in '{col_name}': {doc}")
                break
        else:
            print("Not found in database.")

if __name__ == "__main__":
    asyncio.run(main())
