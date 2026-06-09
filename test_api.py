import asyncio
import sys
from mini_app_api import get_popular, get_trending, app
from database import Database

async def test():
    try:
        app.state.db = Database()
        await app.state.db.connect()
        print("Connected to DB")
        pop = await get_popular()
        print("Popular OK")
        trend = await get_trending(limit=5)
        print("Trending OK")
    except Exception as e:
        print(f"Error: {e}")

asyncio.run(test())
