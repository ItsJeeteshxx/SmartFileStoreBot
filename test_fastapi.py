from fastapi import FastAPI, APIRouter

app = FastAPI()
api_router = APIRouter()

@api_router.get("/stories")
def get_stories():
    return {"hello": "world"}

app.include_router(api_router, prefix="/api")

if __name__ == "__main__":
    import uvicorn
    import asyncio
    import httpx

    async def test():
        await asyncio.sleep(2)
        async with httpx.AsyncClient() as client:
            res = await client.get("http://localhost:8000/api/stories")
            print(f"TEST RESULT /api/stories: {res.status_code} - {res.text}")
            res2 = await client.get("http://localhost:8000/stories")
            print(f"TEST RESULT /stories: {res2.status_code} - {res2.text}")

    asyncio.get_event_loop().create_task(test())
    uvicorn.run(app, host="0.0.0.0", port=8000)
