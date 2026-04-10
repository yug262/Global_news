from fastapi import FastAPI, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn
from datetime import datetime, timezone
from typing import Optional, Any, List, Dict
import os
import asyncio
from concurrent.futures import ThreadPoolExecutor
from app.core.db import fetch_all, fetch_one, execute_query

# Thread pool for blocking database operations
executor = ThreadPoolExecutor(max_workers=20)

app = FastAPI(title="Indian News Intelligence API")
from app.api.indian_router import router as indian_router
app.include_router(indian_router)
SERVER_START = datetime.now(timezone.utc)

# Async helpers to run blocking DB operations
async def run_in_executor(func, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, lambda: func(*args))

# Async wrapper with timeout
async def run_with_timeout(func, timeout_sec, *args):
    try:
        loop = asyncio.get_event_loop()
        future = loop.run_in_executor(executor, lambda: func(*args))
        return await asyncio.wait_for(future, timeout=timeout_sec)
    except asyncio.TimeoutError:
        raise TimeoutError(f"Operation timed out after {timeout_sec} seconds")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add response headers middleware for caching and performance
@app.middleware("http")
async def add_cache_headers(request, call_next):
    response = await call_next(request)
    # Add cache control headers for active Indian endpoints
    if request.url.path.startswith("/api/indian_sources"):
        response.headers["Cache-Control"] = "public, max-age=3600"  # 1 hour cache
    elif request.url.path.startswith("/api/indian_news"):
        response.headers["Cache-Control"] = "public, max-age=5"  # 5 second cache
    return response

# ===== API Endpoints =====
@app.get("/api/health")
def health_check():
    return {"status": "ok", "version": "1.0.1_fallbacks_active"}



# API server entry point
    
if __name__ == "__main__":
    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8000"))
    reload_enabled = os.getenv("API_RELOAD", "false").lower() in ("1", "true", "yes")
    print(f"Starting API Server on http://{host}:{port} (reload={reload_enabled})")
    uvicorn.run("server:app", host=host, port=port, reload=reload_enabled)