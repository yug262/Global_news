from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from typing import Optional, Any, List, Dict
from datetime import datetime, timezone
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import logging
import requests

logger = logging.getLogger("indian_router")

from app.core.agent import analyze_indian_news, save_indian_analysis   
from app.core.db import fetch_all, fetch_one, get_latest_indian_update_id
from app.core.realtime import manager, trigger_analysis_completed, trigger_analysis_failed

router = APIRouter()
INDIAN_SERVER_START = datetime.now(timezone.utc)
executor = ThreadPoolExecutor(max_workers=20)

async def run_with_timeout(func, timeout_sec, *args):
    try:
        loop = asyncio.get_event_loop()
        future = loop.run_in_executor(executor, lambda: func(*args))
        return await asyncio.wait_for(future, timeout=timeout_sec)
    except asyncio.TimeoutError:
        raise TimeoutError(f"Operation timed out after {timeout_sec} seconds")


@router.get("/api/events/india")
async def get_indian_events():
    """Get active Indian market events from the indian_news."""
    query = """
    SELECT event_id, MIN(event_title) as event_title, COUNT(*) as article_count, MAX(published) as latest_update
    FROM indian_news
    WHERE event_id IS NOT NULL AND event_id != 'GENERAL_GENERAL'
    GROUP BY event_id
    ORDER BY latest_update DESC
    LIMIT 50
    """
    try:
        events = await run_with_timeout(lambda: fetch_all(query), 10)
        for ev in events:
            if isinstance(ev['latest_update'], datetime):
                ev['latest_update'] = ev['latest_update'].isoformat()
        return {"status": "success", "data": events}
    except Exception as e:
        logger.error(f"Failed to fetch events: {e}")
        return {"status": "error", "message": "Failed to fetch events"}

@router.get("/api/indian_news")
async def get_indian_news(source: str = Query(None, description="Filter news by source name"), 
             limit: int = Query(20, description="Max number of articles to return"),
             today_only: bool = Query(False, description="Only fetch today's news"),
             relevance: str = Query(None, description="Filter news by relevance"),
             analyzed_only: bool = Query(False, description="Only fetch analyzed news"),
             event_id: str = Query(None, description="Filter news by exact event ID"),
             offset: int = Query(0, description="Number of items to skip for pagination"),
             search: str = Query(None, description="Search in title, description, and source")):
    """Get Indian news articles, sorted by newest first."""
    
    query = """SELECT id, title, link, published, source, description, image_url,
        impact_score, impact_summary, analyzed, created_at,
        analysis_data, news_relevance, news_category,
        news_impact_level, news_reason, symbols,
        market_bias, signal_bucket, primary_symbol, executive_summary, event_id, event_title
    FROM indian_news WHERE 1=1"""
    params: List[Any] = []
    
    if today_only:
        today = datetime.now(timezone.utc).date()
        query += " AND DATE(published) = %s"
        params.append(today)
        
    if source and source.lower() != "all":
        query += " AND source = %s"
        params.append(source)
    
    if relevance and relevance.lower() != "all":
        query += " AND LOWER(news_relevance) = %s"
        params.append(relevance.lower())
        
    if analyzed_only:
        query += " AND analyzed = TRUE"
        
    if event_id:
        query += " AND event_id = %s"
        params.append(event_id)
    
    if search and search.strip():
        search_term = f"%{search.strip()}%"
        query += " AND (title ILIKE %s OR description ILIKE %s OR source ILIKE %s)"
        params.extend([search_term, search_term, search_term])
        
    query += " ORDER BY published DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])
    
    try:
        articles = await run_with_timeout(lambda: fetch_all(query, params), 20)
        # Convert datetime objects to string for JSON serialization
        for article in articles:
            if isinstance(article['published'], datetime):
                article['published'] = article['published'].isoformat()
            if isinstance(article.get('created_at'), datetime):
                article['created_at'] = article['created_at'].isoformat()

        return {"status": "success", "count": len(articles), "data": articles}
    except Exception as e:
        logger.error(f"Failed to fetch indian news: {e}")
        return {"status": "error", "message": "Failed to fetch news"}


@router.get("/api/indian_sources")
async def get_indian_sources():
    """Get list of distinct Indian news sources available (with NULL check)."""
    try:
        rows = await run_with_timeout(
            lambda: fetch_all("SELECT DISTINCT source FROM indian_news WHERE source IS NOT NULL ORDER BY source"),
            10
        )
        sources = [r["source"] for r in rows if r.get("source")]
        return {"status": "success", "data": sources}
    except Exception as e:
        logger.error(f"Failed to fetch sources: {e}")
        return {"status": "error", "message": "Failed to fetch sources"}


@router.get("/api/nse/holidays")
async def get_nse_holidays():
    """Return the list of NSE holidays fetched dynamically from NSE."""
    def _fetch_holidays():
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.nseindia.com/resources/exchange-trading-holidays",
        }
        session = requests.Session()
        session.get("https://www.nseindia.com", headers=headers, timeout=5)
        res = session.get("https://www.nseindia.com/api/holiday-master?type=trading", headers=headers, timeout=5)
        
        holidays = {}
        if res.status_code == 200:
            data = res.json()
            for segment in ["CM", "EQUITY"]:
                if segment in data:
                    for item in data[segment]:
                        try:
                            dt = datetime.strptime(item["tradingDate"], "%d-%b-%Y")
                            holidays[dt.strftime("%Y-%m-%d")] = item["description"]
                        except: continue
                    break
        
        if not holidays:
            # Simple fallback for 2026 if API fails
            holidays = { "2026-03-31": "Shri Mahavir Jayanti" } 
        return holidays

    try:
        holidays = await run_with_timeout(_fetch_holidays, 15)
        return {"status": "success", "data": holidays}
    except Exception as e:
        logger.error(f"Failed to fetch NSE holidays: {e}")
        return {"status": "error", "message": "Failed to fetch holidays"}


# ---- NSE LIVE CHART API ----

@router.get("/api/nse/pairs")
async def get_nse_pairs(q: str = Query("", description="Search query")):
    """Return the list of available NSE pairs (only those with candle data)."""
    try:
        if q:
            rows = await run_with_timeout(
                lambda: fetch_all(
                    "SELECT DISTINCT symbol FROM nse_candles_3m WHERE symbol ILIKE %s ORDER BY symbol LIMIT 50",
                    (f"%{q}%",)
                ), 10
            )
        else:
            rows = await run_with_timeout(
                lambda: fetch_all("SELECT DISTINCT symbol FROM nse_candles_3m ORDER BY symbol LIMIT 100"),
                10
            )
        
        return {"status": "success", "data": [r["symbol"] for r in rows]}
    except Exception as e:
        logger.error(f"Failed to fetch NSE pairs: {e}")
        return {"status": "error", "message": "Failed to fetch pairs"}

@router.get("/api/nse/candles")
async def get_nse_candles(symbol: str = Query(..., description="Symbol e.g. TCS"), limit: int = Query(200)):
    """Return latest 3-minute candles for an NSE symbol, newest first."""
    def _fetch_candles():
        clean_symbol = symbol.replace("NSE:", "").upper()
        resolved_symbol = clean_symbol
        
        rows = fetch_all(
            """SELECT time, open, high, low, close
            FROM nse_candles_3m
            WHERE symbol = %s
            ORDER BY time DESC
            LIMIT %s""",
            (clean_symbol, limit)
        )

        # fuzzy fallback
        if not rows:
            fuzzy_row = fetch_one(
                "SELECT symbol FROM nse_candles_3m WHERE symbol ILIKE %s LIMIT 1",
                (f"%{clean_symbol}%",)
            )
            if fuzzy_row:
                candidate = fuzzy_row["symbol"]
                rows = fetch_all(
                    """SELECT time, open, high, low, close
                    FROM nse_candles_3m
                    WHERE symbol = %s
                    ORDER BY time DESC
                    LIMIT %s""",
                    (candidate, limit)
                )
                resolved_symbol = candidate

        data = []
        for r in rows:
            t = r["time"]
            if hasattr(t, "isoformat"):
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                t_str = t.isoformat()
            else:
                t_str = str(t)
                
            data.append({
                "time": t_str,
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
            })
        return resolved_symbol, data

    try:
        resolved_symbol, data = await run_with_timeout(_fetch_candles, 15)
        return {"status": "success", "symbol": resolved_symbol, "data": data}
    except Exception as e:
        logger.error(f"Failed to fetch candles for {symbol}: {e}")
        return {"status": "error", "message": "Failed to fetch candle data"}

@router.get("/api/nse/news-markers")
async def get_nse_news_markers(symbol: Optional[str] = Query(None, description="Filter by NSE pair (e.g., TCS)")):
    """Return Indian news articles with their affected NSE stocks for chart overlay."""
    def _fetch_markers():
        if symbol:
            clean_symbol = symbol.replace("NSE:", "").upper()
            query = """
            SELECT id, title, published, symbols
            FROM indian_news
            WHERE symbols IS NOT NULL 
              AND array_length(symbols, 1) > 0
              AND (
                symbols @> ARRAY[%s]
              )
            ORDER BY published DESC
            LIMIT 500
            """
            return fetch_all(query, (clean_symbol,))
        else:
            query = """
            SELECT id, title, published, symbols
            FROM indian_news
            WHERE symbols IS NOT NULL 
              AND array_length(symbols, 1) > 0
            ORDER BY published DESC
            LIMIT 500
            """
            return fetch_all(query)

    try:
        rows = await run_with_timeout(_fetch_markers, 20)
        
        data = []
        for r in rows:
            syms = r.get("symbols", [])
            
            p = r["published"]
            if hasattr(p, "isoformat"):
                if p.tzinfo is None:
                    p = p.replace(tzinfo=timezone.utc)
                p_str = p.isoformat()
            else:
                p_str = str(p)
                
            data.append({
                "id": r["id"],
                "title": r["title"],
                "published": p_str,
                "affected_stocks": syms if isinstance(syms, list) else []
            })
        
        return {"status": "success", "symbol": symbol, "count": len(data), "data": data}
    except Exception as e:
        logger.error(f"Failed to fetch news markers: {e}")
        return {"status": "error", "message": "Failed to fetch news markers"}




try:
    current_dir = os.path.dirname(os.path.abspath(__file__))
except NameError:
    current_dir = os.getcwd()

# API Routes for News Feed

@router.get("/api/indian_marker")
async def get_indian_marker():
    """Ultra-lightweight endpoint to check if frontend feed needs refreshing."""
    try:
        # We track max_id (for new inserts) and analyzed_count (for finished analyses)
        row = await run_with_timeout(
            lambda: fetch_one(
                "SELECT MAX(id) as max_id, COUNT(CASE WHEN analyzed = true THEN 1 END) as analyzed_count "
                "FROM indian_news"
            ), 5
        )
        if not row or row["max_id"] is None:
            return {"status": "success", "marker": "empty"}
        
        marker = f"{row['max_id']}_{row['analyzed_count']}"
        return {"status": "success", "marker": marker}
    except Exception as e:
        logger.error(f"Failed to fetch marker: {e}")
        return {"status": "error", "message": "Failed to fetch marker"}


@router.get("/api/indian_stream")
async def indian_stream(request: Request):
    """
    Server-Sent Events (SSE) endpoint for real-time dashboard updates.
    This version uses a direct push-based architecture (ConnectionManager).
    """
    async def event_generator():
        # Subscribe to the Real-Time manager's queue
        queue = await manager.subscribe()
        try:
            while True:
                # Check if the client disconnected
                if await request.is_disconnected():
                    break
                
                try:
                    # Wait for a message from the queue with a 30s timeout for heartbeats
                    message = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield message
                except asyncio.TimeoutError:
                    # Send a heartbeat to keep the tunnel/connection alive
                    yield ": heartbeat\n\n"
        finally:
            # Cleanup on disconnect
            await manager.unsubscribe(queue)
    
    return StreamingResponse(event_generator(), media_type="text/event-stream")



@router.get("/api/indian_stats")
async def get_indian_stats():
    """Get dashboard statistics for the footer, for Indian news."""
    try:
        row = await run_with_timeout(
            lambda: fetch_one(
                "SELECT COUNT(*) as total, "
                "COUNT(CASE WHEN analyzed = true THEN 1 END) as analyzed, "
                "COUNT(DISTINCT source) as sources "
                "FROM indian_news"
            ), 10
        )
        uptime_seconds = int((datetime.now(timezone.utc) - INDIAN_SERVER_START).total_seconds())
        return {
            "status": "success",
            "data": {
                "total_articles": row["total"] if row else 0,
                "analyzed_articles": row["analyzed"] if row else 0,
                "source_count": row["sources"] if row else 0,
                "uptime_seconds": uptime_seconds
            }
        }
    except Exception as e:
        logger.error(f"Failed to fetch stats: {e}")
        return {"status": "error", "message": "Failed to fetch stats"}

@router.post("/api/indian_analyze/{news_id}")
async def analyze_single_indian_article(news_id: int):
    """Analyze a single Indian news article by its DB id using the Indian Agent (async, non-blocking, with timeout)."""
  
    try:
        # Run blocking DB call in thread pool with 120 second timeout
        article = await run_with_timeout(
            lambda: fetch_one("SELECT id, title, published, description, source FROM indian_news WHERE id = %s", (news_id,)),
            120
        )
        if not article:
            return {"status": "error", "message": "Indian Article not found"}

        title = article["title"]
        published = str(article["published"])
        description = article.get("description", "") or ""
        source = article.get("source", "") or ""

        # Run analysis with 120 second timeout
        analysis = await run_with_timeout(
            lambda: analyze_indian_news(
                title=title, 
                published_iso=published, 
                summary=description, 
                source=source,
                current_news_id=news_id
            ),
            120
        )

        if analysis:
            try:
                # Save analysis with 30 second timeout
                await run_with_timeout(
                    lambda: save_indian_analysis(news_id, analysis),
                    30
                )
                print(f"[API] Indian Analysis saved for news_id={news_id}")
                
                # Success: Notify all devices via Pusher immediately
                await asyncio.to_thread(trigger_analysis_completed, news_id)
                
                # Return immediately to avoid timeouts (don't re-fetch from DB here)
                return {"status": "success", "news_id": news_id}

            except TimeoutError:
                print(f"[API] save_indian_analysis TIMEOUT for news_id={news_id}")
                await asyncio.to_thread(trigger_analysis_failed, news_id)
                return {"status": "error", "message": "Analysis completed but save timed out"}
            except Exception as save_err:
                print(f"[API] save_indian_analysis FAILED for news_id={news_id}: {save_err}")
                await asyncio.to_thread(trigger_analysis_failed, news_id)
                return {"status": "error", "message": f"Save failed: {save_err}"}
        else:
            print(f"[API] analyze_indian_news returned None for news_id={news_id}")
            await asyncio.to_thread(trigger_analysis_failed, news_id)
            return {"status": "error", "message": "Analysis failed — click to retry"}
    except TimeoutError as te:
        print(f"[API] TIMEOUT in indian_analyze endpoint for news_id={news_id}: {te}")
        await asyncio.to_thread(trigger_analysis_failed, news_id)
        return {"status": "error", "message": "Analysis timeout - took too long (2 min limit)"}
    except asyncio.CancelledError:
        print(f"[API] Indian Analysis cancelled for news_id={news_id}")
        await asyncio.to_thread(trigger_analysis_failed, news_id)
        return {"status": "error", "message": "Analysis was cancelled"}
    except Exception as e:
        print(f"[API] Exception in indian_analyze endpoint: {e}")
        await asyncio.to_thread(trigger_analysis_failed, news_id)
        return {"status": "error", "message": str(e)}