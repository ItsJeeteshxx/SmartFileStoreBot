from fastapi import FastAPI, APIRouter, Request, Response
from contextvars import ContextVar
import asyncio
import httpx
from contextlib import asynccontextmanager

test_var: ContextVar[str] = ContextVar("test_var", default="default")

async def test_client():
    await asyncio.sleep(2)
    async with httpx.AsyncClient() as client:
        res = await client.get("http://localhost:8000/api/stories")
        print(f"TEST RESULT: {res.status_code} - {res.text}", flush=True)
        # Shutdown uvicorn server
        import os
        os._exit(0)

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(test_client())
    yield

app = FastAPI(lifespan=lifespan)
api_router = APIRouter()

@app.middleware("http")
async def test_middleware(request: Request, call_next):
    test_var.set("middleware_value")
    response = await call_next(request)
    return response

@api_router.get("/stories")
def get_stories():
    val = test_var.get()
    return {"value": val}

app.include_router(api_router, prefix="/api")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
