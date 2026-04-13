from fastapi import FastAPI, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn
from datetime import datetime, timezone
from typing import Optional, Any, List, Dict
import os
import sys
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

load_dotenv()

# ---- Startup Environment Validation ----
REQUIRED_ENV_VARS = ["DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"]
missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
if missing:
    print(f"❌ FATAL: Missing required environment variables: {', '.join(missing)}")
    print("   Please set them in your .env file or environment.")
    sys.exit(1)

from app.core.db import fetch_all, fetch_one, execute_query, init_system_tables
from app.core.realtime import db_listener

# Thread pool for blocking database operations
executor = ThreadPoolExecutor(max_workers=20)

app = FastAPI(title="Indian News Intelligence API")

@app.on_event("startup")
async def startup_event():
    """Initializes system tables on application boot."""
    print("STARTING: Initializing Indian News Intelligence API...")
    # Initialize real-time synchronization tables
    await asyncio.get_event_loop().run_in_executor(executor, init_system_tables)
    
    # Start the continuous Postgres 'Strong Sync' Listener
    asyncio.create_task(db_listener())

# Initialization has been moved to the server's startup event 
# to ensure cleaner dependency handling. 
# init_system_tables()

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
# CORS — In production, restrict allow_origins to your actual frontend domain(s)
# e.g. allow_origins=["https://yourdomain.com"]
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