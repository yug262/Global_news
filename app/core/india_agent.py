import os
import json
import logging
import re
from typing import Dict, Any, Optional
from google import genai
from google.genai import types
from dotenv import load_dotenv
from app.core.prompt import INDIAN_MARKET_CLASSIFY_PROMPT
load_dotenv()

logger = logging.getLogger("india_agent")
logger.setLevel(logging.INFO)

if not logger.handlers:
    _ch = logging.StreamHandler()
    _ch.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S UTC"))
    logger.addHandler(_ch)

MODEL_NAME = os.getenv("MODEL_NAME", "gemini-3.1-flash-lite-preview")
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))



import asyncio

async def analyze_indian_news(title: str, description: str = "") -> Optional[Dict[str, Any]]:
    """
    Analyzes Indian news using Gemini and the provided strict prompt.
    """
    if not os.getenv("GEMINI_API_KEY") or not client:
        logger.error("Gemini API key not configured.")
        return None

    try:
        user_msg = f"Headline: {title}\nDescription: {description[:500]}"
        
        response = None
        for attempt in range(3):
            try:
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=MODEL_NAME,
                    contents=[user_msg],
                    config=types.GenerateContentConfig(
                        system_instruction=INDIAN_MARKET_CLASSIFY_PROMPT,
                        temperature=0.1,
                        max_output_tokens=300,
                        response_mime_type="application/json"
                    )
                )
                if hasattr(response, "usage_metadata") and response.usage_metadata:
                    logger.info(f"[TOKEN USAGE - INDIAN_FILTER] In: {response.usage_metadata.prompt_token_count} | Out: {response.usage_metadata.candidates_token_count} | Total: {response.usage_metadata.total_token_count}")
                break
            except Exception as e:
                if attempt < 2 and ("503" in str(e) or "UNAVAILABLE" in str(e) or "overload" in str(e).lower()):
                    logger.warning(f"Gemini API overloaded. Retrying {attempt+1}/3 in 3s...")
                    await asyncio.sleep(3)
                else:
                    raise e

        if not response or not response.text:
            logger.warning(f"Empty response from Gemini for: {title[:50]}...")
            return None

        # Clean markdown code blocks if the model wrapped it
        raw_text = response.text.strip()
        if raw_text.startswith("```json"):
            raw_text = raw_text[7:]
        elif raw_text.startswith("```"):
            raw_text = raw_text[3:]
            
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3]
            
        raw_text = raw_text.strip()
        logger.info(f"RAW TEXT FROM GEMINI: {raw_text}")

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            import ast
            logger.error("Failed standard JSON parse, attempting literal_eval fallback")
            data = ast.literal_eval(raw_text)
        
        # 1. Validation & Enum Clamping
        ALLOWED_CATEGORIES = {"corporate_event", "government_policy", "macro_data", "global_macro_impact", "commodity_macro", "sector_trend", "institutional_activity", "sentiment_indicator", "price_action_noise", "routine_market_update", "other"}
        ALLOWED_RELEVANCE = {"High Useful", "Useful", "Medium", "Neutral", "Noisy"}

        category = str(data.get("category", "routine_market_update")).strip()
        if category not in ALLOWED_CATEGORIES:
            category = "other"

        relevance = str(data.get("relevance", "Noisy")).strip()
        if relevance not in ALLOWED_RELEVANCE:
            relevance = "Noisy"

        # 2. Strict Parse Mentions
        company_mentions = data.get("company_mentions", [])
        if not isinstance(company_mentions, list):
            company_mentions = []
            
        # 3. Call Resolver safely
        if not company_mentions:
            resolved_symbols = []
        else:
            from app.ind.tools import strict_resolve_symbols
            resolved_symbols = strict_resolve_symbols(company_mentions)
            
        # 4. Final Output Formation
        return {
            "category": category,
            "relevance": relevance,
            "reason": str(data.get("reason", "No specific reason provided.")),
            "symbols": resolved_symbols
        }

    except Exception as e:
        logger.error(f"Error during Indian news analysis: {e}")
        return None

