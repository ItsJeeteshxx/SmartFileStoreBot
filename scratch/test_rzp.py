import asyncio
import aiohttp
import base64
import json

async def fetch_razorpay_payment(pid: str):
    key_id = "rzp_live_Se4oKhmXsx44yM"
    key_secret = "FDKbI5mq9u4d0VGGtrEFdgAQ"
    
    auth_str = f"{key_id}:{key_secret}"
    auth_b64 = base64.b64encode(auth_str.encode()).decode()
    headers = {
        "Authorization": f"Basic {auth_b64}"
    }
    
    url = f"https://api.razorpay.com/v1/payments/{pid}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                return {"error": resp.status, "text": await resp.text()}

async def main():
    pids = [
        "pay_T2AiMzy3nxbioy",
        "pay_T4Jge4uhIfT97q",
        "pay_T3BdQ3Tf1Cnfu4"
    ]
    for pid in pids:
        print(f"Fetching {pid}...")
        data = await fetch_razorpay_payment(pid)
        print(json.dumps(data, indent=2))
        print("="*80)

if __name__ == "__main__":
    asyncio.run(main())
