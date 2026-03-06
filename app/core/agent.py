# agent.py
"""
Macro Trading Intelligence Agent (Gemini)
- Takes title + optional summary + published timestamp + source
- Pulls market data via tools
- Measures what's already priced since publish (reaction vs ATR)
- Outputs remaining impact from NOW
- Saves agent outputs into DB columns (impact_score, confidence, etc.)
"""

import os
import json
import time
import traceback
import re
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from google import genai
from google.genai import types

from app.core.prompt import SYSTEM_PROMPT
from app.core.schema import SCHEMA_TEMPLATE

from app.core.tools import (
    get_crypto_prices,
    get_forex_prices,
    get_global_markets,
    get_market_sentiment,
    search_recent_news,
    get_macro_context,
    get_economic_calendar,
    get_interest_rate_differentials,
    get_news_source_credibility,
    calculate_reaction,
    get_asset_atr,
    classify_reaction_status,
    detect_reaction_headline
)

load_dotenv()

BASE_DELAY = 5
MAX_RETRIES = 3
MODEL_NAME = os.getenv("MODEL_NAME")

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

def _log(msg: str):
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode(), flush=True)

def _calculate_news_age(published_iso: str) -> tuple[str, str, float]:
    try:
        pub_dt = datetime.fromisoformat(published_iso.strip())
        if pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        hours_old = (datetime.now(timezone.utc) - pub_dt).total_seconds() / 3600.0
        minutes = int(hours_old * 60)

        if minutes < 60:
            human = f"{minutes} minute{'s' if minutes != 1 else ''} ago"
        elif hours_old < 24:
            h = int(hours_old)
            human = f"{h} hour{'s' if h != 1 else ''} ago"
        else:
            d = int(hours_old / 24)
            human = f"{d} day{'s' if d != 1 else ''} ago"

        if hours_old < 1:
            label = "Fresh"
        elif hours_old < 4:
            label = "Recent"
        elif hours_old < 12:
            label = "Stale"
        else:
            label = "Old"

        return label, human, hours_old
    except Exception:
        return "Fresh", "just now", 0.0

MAJOR_FOREX_PAIRS = [
    "EUR/USD", "USD/JPY", "GBP/USD", "USD/CHF",
    "AUD/USD", "USD/CAD", "NZD/USD", "DXY",
]

TOP_CRYPTO = [
    "bitcoin", "ethereum", "solana", "ripple",
    "cardano", "dogecoin", "avalanche-2", "chainlink",
]

_HEADLINE_ASSET_MAP = {
    # Crypto
    "bitcoin": "BTC-USD", "btc": "BTC-USD",
    "ethereum": "ETH-USD", "eth": "ETH-USD",
    "solana": "SOL-USD", "sol": "SOL-USD",
    "ripple": "XRP-USD", "xrp": "XRP-USD",
    "cardano": "ADA-USD", "ada": "ADA-USD",
    "dogecoin": "DOGE-USD", "doge": "DOGE-USD",
    # Macro
    "gold": "GC=F",
    "oil": "CL=F", "crude": "CL=F",
    "dollar": "DX-Y.NYB", "dxy": "DX-Y.NYB",
    "s&p": "^GSPC", "nasdaq": "^IXIC", "dow": "^DJI",
    "nikkei": "^N225", "dax": "^GDAXI",
}

def _detect_assets_from_title(title: str) -> list[str]:
    tl = (title or "").lower()
    found = []
    used = set()
    for k, sym in _HEADLINE_ASSET_MAP.items():
        if k in tl and sym not in used:
            found.append(sym)
            used.add(sym)
    return found[:5]

def fetch_all_market_data() -> dict:
    out = {}
    try:
        out["forex"] = get_forex_prices(MAJOR_FOREX_PAIRS)
    except Exception as e:
        _log(f"⚠️ forex: {e}")
        out["forex"] = {}

    try:
        out["crypto"] = get_crypto_prices(TOP_CRYPTO)
    except Exception as e:
        _log(f"⚠️ crypto: {e}")
        out["crypto"] = {}

    try:
        out["markets"] = get_global_markets()
    except Exception as e:
        _log(f"⚠️ markets: {e}")
        out["markets"] = {}

    try:
        out["sentiment"] = get_market_sentiment()
    except Exception as e:
        _log(f"⚠️ sentiment: {e}")
        out["sentiment"] = {}

    try:
        out["macro"] = get_macro_context()
    except Exception as e:
        _log(f"⚠️ macro: {e}")
        out["macro"] = {}

    return out

def _check_recent_movements(symbols: list[str], published_iso: str) -> dict:
    movements = {}
    for sym in symbols:
        try:
            reaction = calculate_reaction(sym, published_iso)
            atr = get_asset_atr(sym)

            if not reaction or "reaction_pct" not in reaction:
                continue

            reaction_pct = float(reaction["reaction_pct"])
            atr_pct = float(atr.get("atr_pct_reference") or 1.0)
            status = classify_reaction_status(reaction_pct, atr_pct)

            key = sym.replace("-USD", "").replace("=F", "").replace("^", "")
            movements[key] = {
                "symbol": sym,
                "reaction_pct": round(reaction_pct, 4),
                "news_price": reaction.get("news_price"),
                "current_price": reaction.get("current_price"),
                "atr_pct_reference": round(atr_pct, 4),
                "reaction_status": status,
            }
        except Exception:
            continue
    return movements

CLASSIFY_PROMPT = """You are an institutional trading news filter and classifier for forex and cryptocurrency markets.
Your job is to determine whether a headline represents a genuine market-moving event or low-value news content.
You must first determine whether the news represents a real catalyst before assigning a category.
Do NOT rely on keywords alone.
You must interpret the actual economic meaning of the headline.
STEP 1 — AUTHENTICITY CHECK
Determine whether the headline represents one of the following:
REAL_CATALYST
A genuine market event that introduces new information which could influence market expectations or capital flows.
CONTEXT_ONLY
Background commentary, previews of scheduled events, sector analysis, or sentiment discussion.
RECYCLED_NEWS
Old information being repeated without a new development.
PRICE_REPORT
Headlines that only describe price movement that already happened.
OPINION_OR_SPECULATION
Articles expressing opinions, speculation, or predictions without new data or institutional research.
STEP 2 — CATEGORY CLASSIFICATION
If the headline is a REAL_CATALYST or CONTEXT_ONLY event, classify it into ONE of these categories:
macro_data_release
Unexpected economic data releases (CPI, NFP, GDP, inflation surprises).
central_bank_policy
Official monetary policy decisions (rate hikes/cuts, QE changes).
central_bank_guidance
Forward guidance or speeches from central bank officials.
institutional_research
Market outlook or analysis from major financial institutions (Goldman Sachs, JPMorgan, MUFG, OCBC).
regulatory_policy
Government or regulatory actions affecting financial or crypto markets.
crypto_ecosystem_event
Major blockchain developments, integrations, protocol upgrades, or institutional partnerships.
liquidity_flows
Institutional capital flows such as ETF inflows/outflows or sovereign investments.
geopolitical_event
Wars, sanctions, political conflicts affecting markets.
systemic_risk_event
Financial system instability such as bank failures or liquidity crises.
commodity_supply_shock
Supply disruptions impacting commodities (OPEC cuts, mining shutdowns).
market_structure_event
Structural changes such as new ETFs, exchange rule changes, derivatives launches.
sector_trend_analysis
Broad sector or market trend analysis.
sentiment_indicator
Investor sentiment indicators or positioning data.
routine_market_update
Expected or low-impact market updates.
If the headline is PRICE_REPORT, RECYCLED_NEWS, or OPINION_OR_SPECULATION, classify it as:
price_action_noise
IMPORTANT RULES
Headlines describing price movement after the fact are always price_action_noise.
Articles explaining why markets moved earlier are also price_action_noise.
Opinion pieces without new data or institutional research are price_action_noise.
If a headline repeats old news without a new development, treat it as RECYCLED_NEWS → price_action_noise.
Always identify the primary event, not keywords.
Respond ONLY with valid JSON:
{
    "authenticity": "REAL_CATALYST | CONTEXT_ONLY | RECYCLED_NEWS | PRICE_REPORT | OPINION_OR_SPECULATION",
    "impact_level": "high | medium | low | none",
    "category": "macro_data_release | central_bank_policy | central_bank_guidance | institutional_research | regulatory_policy | crypto_ecosystem_event | liquidity_flows | geopolitical_event | systemic_risk_event | commodity_supply_shock | market_structure_event | sector_trend_analysis | sentiment_indicator | routine_market_update | price_action_noise",
    "reason": "one short sentence explaining the classification"
}"""

def classify_news_relevance(title: str, description: str = "") -> dict:
    """
    Lightweight Gemini call to classify a news article's category, impact_level, and reason
    for forex/crypto trading. Returns a dict with these fields.
    Falls back to a default 'none' impact dict on any error.
    """
    default_resp = {"category": "error", "impact_level": "none", "reason": "Classification failed or skipped"}
    if not os.getenv("GEMINI_API_KEY") or not client:
        return default_resp
    try:
        user_msg = f"Title: {title}"
        if description:
            user_msg += f"\nDescription: {description[:300]}"
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=[user_msg],
            config=types.GenerateContentConfig(
                system_instruction=CLASSIFY_PROMPT,
                temperature=0.1,
                max_output_tokens=300,
            ),
        )
        text = (response.text or "").strip()
        # Extract JSON from response — handle markdown blocks and potential trailing text
        json_match = re.search(r'\{.*\}', text, re.DOTALL)
        
        result = default_resp.copy()
        if json_match:
            try:
                json_str = json_match.group(0)
                # If the match is incomplete (e.g. missing closing brace due to truncation), try to fix it
                if json_str.count('{') > json_str.count('}'):
                    json_str += '}' * (json_str.count('{') - json_str.count('}'))
                data = json.loads(json_str)
                category = data.get("category", "unclassified")
                impact_level = data.get("impact_level", "none").lower()
                reason = data.get("reason", "")
                result = {
                    "category": category,
                    "impact_level": impact_level,
                    "reason": reason
                }
            except Exception:
                pass
        
        # Log for diagnostics
        try:
            os.makedirs("logs", exist_ok=True)
            with open("logs/classification.log", "a", encoding="utf-8") as f:
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"[{timestamp}] TITLE: {title}\n")
                f.write(f"[{timestamp}] RAW: {text}\n")
                f.write(f"[{timestamp}] FINAL: {result}\n")
                f.write("-" * 40 + "\n")
        except:
            pass
            
        return result
    except Exception as e:
        print(f"[CLASSIFY] Error classifying '{title[:50]}...': {e}", flush=True)
        return default_resp
def classify_batch(items: list[tuple[str, str]]) -> list[dict]:
    """
    Classify a batch of (title, description) pairs in parallel.
    Returns list of relevance dicts in the same order.
    """
    if not items:
        return []
    
    default_resp = {"category": "error", "impact_level": "none", "reason": "Classification failed or skipped"}
    results = [default_resp] * len(items)
    def _classify(idx, title, desc):
        return idx, classify_news_relevance(title, desc)
    with ThreadPoolExecutor(max_workers=min(len(items), 5)) as executor:
        futures = {
            executor.submit(_classify, i, title, desc): i
            for i, (title, desc) in enumerate(items)
        }
        for future in as_completed(futures):
            try:
                idx, label = future.result()
                results[idx] = label
            except Exception:
                pass
    return results
    
def analyze_news(title: str, published_iso: str, summary: str = "", source: str = "") -> dict | None:

    """
    Returns JSON matching schema template.
    """
    analysis_time = datetime.now(timezone.utc).isoformat()

    age_label, age_human, hours_old = _calculate_news_age(published_iso)

    # priced-in duplicate check using DB
    news_check = search_recent_news(title, hours_back=48)
    priced_in_by_history = bool(news_check.get("priced_in", False))

    for attempt in range(MAX_RETRIES):
        try:
            _log(f"[ATTEMPT {attempt+1}/{MAX_RETRIES}] {title[:80]}")

            market_data = fetch_all_market_data()

            # movement since publish (dynamic)
            symbols = _detect_assets_from_title(title)
            movements = _check_recent_movements(symbols, published_iso) if symbols else {}

            # choose a dominant movement (largest abs move)
            dominant = None
            for k, mv in movements.items():
                if dominant is None or abs(mv["reaction_pct"]) > abs(dominant["reaction_pct"]):
                    dominant = mv

            reaction_pct = dominant["reaction_pct"] if dominant else 0.0
            atr_pct_reference = dominant["atr_pct_reference"] if dominant else 0.0
            reaction_status = dominant["reaction_status"] if dominant else "normal_reaction"

            # source credibility (use real DB source)
            source_cred = get_news_source_credibility(source)

            # extra data (only when relevant)
            extra_data = {}
            tl = title.lower()
            if any(w in tl for w in ["rate", "cpi", "inflation", "nfp", "jobs", "gdp", "pmi", "fed", "ecb", "boj", "rbi"]):
                extra_data["economic_calendar"] = get_economic_calendar()
                extra_data["rate_differentials"] = get_interest_rate_differentials()

            schema_text = json.dumps(SCHEMA_TEMPLATE, indent=2)

            movement_text = "None"
            if movements:
                lines = []
                for name, mv in movements.items():
                    direction = "down" if mv["reaction_pct"] < 0 else "up"
                    lines.append(
                        f"{name}: {direction} {abs(mv['reaction_pct']):.2f}% since publish "
                        f"(ATR {mv['atr_pct_reference']:.2f}%, status {mv['reaction_status']})"
                    )
                movement_text = "\n".join(lines)

            prompt = f"""
Return JSON matching this exact template (all keys must exist, unknown = "" or 0 or []):
{schema_text}

NEWS:
- title: {title}
- summary: {summary}
- source: {source}
- timestamp_utc: {published_iso}
- analysis_timestamp_utc: {analysis_time}

DYNAMIC REACTION INPUTS (computed from market data):
- reaction_pct: {reaction_pct}
- atr_pct_reference: {atr_pct_reference}
- reaction_status: {reaction_status}
- already_priced_in_by_history: {priced_in_by_history}
- db_duplicate_check: {json.dumps(news_check, default=str)}

LIVE MARKET DATA (use these; do NOT fabricate):
- forex: {json.dumps(market_data.get("forex", {}), default=str)}
- crypto: {json.dumps(market_data.get("crypto", {}), default=str)}
- global_markets: {json.dumps(market_data.get("markets", {}), default=str)}
- sentiment: {json.dumps(market_data.get("sentiment", {}), default=str)}
- macro: {json.dumps(market_data.get("macro", {}), default=str)}
- source_credibility: {json.dumps(source_cred, default=str)}
{f"- economic_calendar: {json.dumps(extra_data.get('economic_calendar', {}), default=str)}" if "economic_calendar" in extra_data else ""}
{f"- rate_differentials: {json.dumps(extra_data.get('rate_differentials', {}), default=str)}" if "rate_differentials" in extra_data else ""}

RECENT MOVEMENTS SINCE PUBLISH (what already happened):
{movement_text}

RULE REMINDER:
- Your impact_score + expected_move_pct MUST be REMAINING impact from NOW onward.
- If reaction_status=fully_priced, cap primary_impact_score at 4 unless crisis.
- If news is Old (>12h), cap primary_impact_score at 3.
- Headline-only mode: if summary empty, cap direction_probability_pct at 70 unless explicit action.
Return STRICT JSON only. No markdown. No extra text.
"""

            resp = model.generate_content(prompt)
            text = resp.text

            try:
                result = json.loads(text)
            except json.JSONDecodeError:
                m = re.search(r"\{.*\}", text, re.DOTALL)
                if not m:
                    continue
                result = json.loads(m.group(0))

            # inject simple timing info
            result.setdefault("event_metadata", {})
            result["event_metadata"]["analysis_timestamp_utc"] = analysis_time

            # tag priced-in info (for DB)
            result["_meta"] = {
                "news_age_label": age_label,
                "news_age_human": age_human,
                "news_hours_old": round(hours_old, 2),
                "priced_in_by_history": priced_in_by_history,
                "reaction_status": reaction_status,
                "reaction_pct": reaction_pct,
                "atr_pct_reference": atr_pct_reference,
            }

            return result

        except Exception as e:
            _log(f"ERROR: {e}")
            traceback.print_exc()
            time.sleep(BASE_DELAY * (attempt + 1))

    return None


def save_analysis(news_id: int, analysis: dict):
    """
    Saves the agent's output into existing DB columns your UI reads.
    """
    from db import execute_query

    core = analysis.get("core_impact_assessment", {})
    regime = analysis.get("market_regime_context", {})
    prob = analysis.get("probability_and_confidence", {})
    time_mod = analysis.get("time_modeling", {})
    directional = analysis.get("directional_bias", {})
    meta = analysis.get("_meta", {})

    forex_items = directional.get("forex", []) or []
    crypto_items = directional.get("crypto", []) or []

    query = """
        UPDATE news SET
            analyzed               = TRUE,
            analysis_data          = %s,
            impact_score           = %s,
            impact_summary         = %s,
            affected_markets       = %s,
            impact_duration        = %s,
            market_mode            = %s,
            usd_bias               = %s,
            crypto_bias            = %s,
            execution_window       = %s,
            confidence             = %s,
            conviction_score       = %s,
            volatility_regime      = %s,
            dollar_liquidity_state = %s,
            news_age_label         = %s,
            news_age_human         = %s,
            news_priced_in         = %s
        WHERE id = %s
    """

    params = (
        json.dumps(analysis),
        int(core.get("primary_impact_score", 0) or 0),
        (analysis.get("executive_summary", "") or "")[:500],
        json.dumps(core.get("market_category_scores", {}) or {}),
        (time_mod.get("impact_duration", "") or "")[:100],
        (regime.get("dominant_market_regime", "") or "")[:50],
        (forex_items[0].get("direction", "") if forex_items else "")[:20],
        (crypto_items[0].get("direction", "") if crypto_items else "")[:20],
        (time_mod.get("reaction_speed", "") or "")[:50],
        str(prob.get("overall_confidence_score", ""))[:50],
        int(prob.get("direction_probability_pct", 0) or 0),
        (regime.get("volatility_expectation", "") or "")[:50],
        (regime.get("liquidity_condition_assumption", "") or "")[:50],
        (meta.get("news_age_label", "Fresh") or "")[:20],
        (meta.get("news_age_human", "") or "")[:50],
        bool(meta.get("priced_in_by_history", False) or (meta.get("reaction_status") == "fully_priced")),
        news_id,
    )

    execute_query(query, params)
    _log(f"[SAVE] news_id={news_id}")

def _log(msg: str):
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode(), flush=True)

def _calculate_news_age(published_iso: str) -> tuple[str, str, float]:
    try:
        pub_dt = datetime.fromisoformat(published_iso.strip())
        if pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        hours_old = (datetime.now(timezone.utc) - pub_dt).total_seconds() / 3600.0
        minutes = int(hours_old * 60)

        if minutes < 60:
            human = f"{minutes} minute{'s' if minutes != 1 else ''} ago"
        elif hours_old < 24:
            h = int(hours_old)
            human = f"{h} hour{'s' if h != 1 else ''} ago"
        else:
            d = int(hours_old / 24)
            human = f"{d} day{'s' if d != 1 else ''} ago"

        if hours_old < 1:
            label = "Fresh"
        elif hours_old < 4:
            label = "Recent"
        elif hours_old < 12:
            label = "Stale"
        else:
            label = "Old"

        return label, human, hours_old
    except Exception:
        return "Fresh", "just now", 0.0

MAJOR_FOREX_PAIRS = [
    "EUR/USD", "USD/JPY", "GBP/USD", "USD/CHF",
    "AUD/USD", "USD/CAD", "NZD/USD", "DXY",
]

TOP_CRYPTO = [
    "bitcoin", "ethereum", "solana", "ripple",
    "cardano", "dogecoin", "avalanche-2", "chainlink",
]

_HEADLINE_ASSET_MAP = {
    # Crypto
    "bitcoin": "BTC-USD", "btc": "BTC-USD",
    "ethereum": "ETH-USD", "eth": "ETH-USD",
    "solana": "SOL-USD", "sol": "SOL-USD",
    "ripple": "XRP-USD", "xrp": "XRP-USD",
    "cardano": "ADA-USD", "ada": "ADA-USD",
    "dogecoin": "DOGE-USD", "doge": "DOGE-USD",
    # Macro
    "gold": "GC=F",
    "oil": "CL=F", "crude": "CL=F",
    "dollar": "DX-Y.NYB", "dxy": "DX-Y.NYB",
    "s&p": "^GSPC", "nasdaq": "^IXIC", "dow": "^DJI",
    "nikkei": "^N225", "dax": "^GDAXI",
}

def _detect_assets_from_title(title: str) -> list[str]:
    tl = (title or "").lower()
    found = []
    used = set()
    for k, sym in _HEADLINE_ASSET_MAP.items():
        if k in tl and sym not in used:
            found.append(sym)
            used.add(sym)
    return found[:5]

def fetch_all_market_data() -> dict:
    out = {}
    try:
        out["forex"] = get_forex_prices(MAJOR_FOREX_PAIRS)
    except Exception as e:
        _log(f"⚠️ forex: {e}")
        out["forex"] = {}

    try:
        out["crypto"] = get_crypto_prices(TOP_CRYPTO)
    except Exception as e:
        _log(f"⚠️ crypto: {e}")
        out["crypto"] = {}

    try:
        out["markets"] = get_global_markets()
    except Exception as e:
        _log(f"⚠️ markets: {e}")
        out["markets"] = {}

    try:
        out["sentiment"] = get_market_sentiment()
    except Exception as e:
        _log(f"⚠️ sentiment: {e}")
        out["sentiment"] = {}

    try:
        out["macro"] = get_macro_context()
    except Exception as e:
        _log(f"⚠️ macro: {e}")
        out["macro"] = {}

    return out

def _check_recent_movements(symbols: list[str], published_iso: str) -> dict:
    movements = {}
    for sym in symbols:
        try:
            reaction = calculate_reaction(sym, published_iso)
            atr = get_asset_atr(sym)

            if not reaction or "reaction_pct" not in reaction:
                continue

            reaction_pct = float(reaction["reaction_pct"])
            atr_pct = float(atr.get("atr_pct_reference") or 1.0)
            status = classify_reaction_status(reaction_pct, atr_pct)

            key = sym.replace("-USD", "").replace("=F", "").replace("^", "")
            movements[key] = {
                "symbol": sym,
                "reaction_pct": round(reaction_pct, 4),
                "news_price": reaction.get("news_price"),
                "current_price": reaction.get("current_price"),
                "atr_pct_reference": round(atr_pct, 4),
                "reaction_status": status,
            }
        except Exception:
            continue
    return movements

def analyze_news(title: str, published_iso: str, summary: str = "", source: str = "") -> dict | None:
    """
    Returns JSON matching schema template.
    """
    analysis_time = datetime.now(timezone.utc).isoformat()

    age_label, age_human, hours_old = _calculate_news_age(published_iso)

    # priced-in duplicate check using DB
    news_check = search_recent_news(title, hours_back=48)
    priced_in_by_history = bool(news_check.get("priced_in", False))

    for attempt in range(MAX_RETRIES):
        try:
            _log(f"[ATTEMPT {attempt+1}/{MAX_RETRIES}] {title[:80]}")

            market_data = fetch_all_market_data()

            # movement since publish (dynamic)
            symbols = _detect_assets_from_title(title)
            movements = _check_recent_movements(symbols, published_iso) if symbols else {}

            # choose a dominant movement (largest abs move)
            dominant = None
            for k, mv in movements.items():
                if dominant is None or abs(mv["reaction_pct"]) > abs(dominant["reaction_pct"]):
                    dominant = mv

            reaction_pct = dominant["reaction_pct"] if dominant else 0.0
            atr_pct_reference = dominant["atr_pct_reference"] if dominant else 0.0
            reaction_status = dominant["reaction_status"] if dominant else "normal_reaction"

            # ✅ Reaction-headline override (already moved headlines like "surge 8%")
            rh = detect_reaction_headline(title)

            reaction_headline = rh["reaction_headline"]
            headline_move_pct = rh["headline_move_pct"]
            has_new_catalyst = rh["has_new_catalyst"]

            # If headline itself says a big realized move, treat as post-move commentary
            # unless it clearly contains a new confirmed catalyst.
            if reaction_headline and headline_move_pct is not None and headline_move_pct >= 3 and not has_new_catalyst:
                # force priced-in logic
                reaction_status = "fully_priced"

            # source credibility (use real DB source)
            source_cred = get_news_source_credibility(source)

            # extra data (only when relevant)
            extra_data = {}
            tl = title.lower()
            if any(w in tl for w in ["rate", "cpi", "inflation", "nfp", "jobs", "gdp", "pmi", "fed", "ecb", "boj", "rbi"]):
                extra_data["economic_calendar"] = get_economic_calendar()
                extra_data["rate_differentials"] = get_interest_rate_differentials()

            schema_text = json.dumps(SCHEMA_TEMPLATE, indent=2)

            movement_text = "None"
            if movements:
                lines = []
                for name, mv in movements.items():
                    direction = "down" if mv["reaction_pct"] < 0 else "up"
                    lines.append(
                        f"{name}: {direction} {abs(mv['reaction_pct']):.2f}% since publish "
                        f"(ATR {mv['atr_pct_reference']:.2f}%, status {mv['reaction_status']})"
                    )
                movement_text = "\n".join(lines)

            prompt = f"""
Return JSON matching this exact template (all keys must exist, unknown = "" or 0 or []):
{schema_text}

NEWS:
- title: {title}
- summary: {summary}
- source: {source}
- timestamp_utc: {published_iso}
- analysis_timestamp_utc: {analysis_time}

DYNAMIC REACTION INPUTS (computed from market data):
- reaction_pct: {reaction_pct}
- atr_pct_reference: {atr_pct_reference}
- reaction_status: {reaction_status}
- already_priced_in_by_history: {priced_in_by_history}
- db_duplicate_check: {json.dumps(news_check, default=str)}

LIVE MARKET DATA (use these; do NOT fabricate):
- forex: {json.dumps(market_data.get("forex", {}), default=str)}
- crypto: {json.dumps(market_data.get("crypto", {}), default=str)}
- global_markets: {json.dumps(market_data.get("markets", {}), default=str)}
- sentiment: {json.dumps(market_data.get("sentiment", {}), default=str)}
- macro: {json.dumps(market_data.get("macro", {}), default=str)}
- source_credibility: {json.dumps(source_cred, default=str)}
{f"- economic_calendar: {json.dumps(extra_data.get('economic_calendar', {}), default=str)}" if "economic_calendar" in extra_data else ""}
{f"- rate_differentials: {json.dumps(extra_data.get('rate_differentials', {}), default=str)}" if "rate_differentials" in extra_data else ""}

RECENT MOVEMENTS SINCE PUBLISH (what already happened):
{movement_text}

REACTION-HEADLINE OVERRIDE (IMPORTANT):
- reaction_headline: {reaction_headline}
- headline_move_pct: {headline_move_pct}
- has_new_catalyst: {has_new_catalyst}

Rules:
- If reaction_headline=true AND headline_move_pct >= 3 AND has_new_catalyst=false:
  treat as post-move commentary.
  Bias should default to stabilization.
  primary_impact_score should be capped at 3–4.
  expected remaining move should be small.

RULE REMINDER:
- Your impact_score + expected_move_pct MUST be REMAINING impact from NOW onward.
- If reaction_status=fully_priced, cap primary_impact_score at 4 unless crisis.
- If news is Old (>12h), cap primary_impact_score at 3.
- Headline-only mode: if summary empty, cap direction_probability_pct at 70 unless explicit action.
Return STRICT JSON only. No markdown. No extra text.
"""

            resp = client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.4,
                    response_mime_type="application/json",
                )
            )
            text = resp.text

            try:
                result = json.loads(text)
            except json.JSONDecodeError:
                m = re.search(r"\{.*\}", text, re.DOTALL)
                if not m:
                    continue
                result = json.loads(m.group(0))

            # inject simple timing info
            result.setdefault("event_metadata", {})
            result["event_metadata"]["analysis_timestamp_utc"] = analysis_time

            # tag priced-in info (for DB)
            result["_meta"] = {
                "news_age_label": age_label,
                "news_age_human": age_human,
                "news_hours_old": round(hours_old, 2),
                "priced_in_by_history": priced_in_by_history,
                "reaction_status": reaction_status,
                "reaction_pct": reaction_pct,
                "atr_pct_reference": atr_pct_reference,
                "reaction_headline": reaction_headline,
                "headline_move_pct": headline_move_pct,
                "has_new_catalyst": has_new_catalyst,
            }

            return result

        except Exception as e:
            _log(f"ERROR: {e}")
            traceback.print_exc()
            time.sleep(BASE_DELAY * (attempt + 1))

    return None


def save_analysis(news_id: int, analysis: dict):
    """
    Saves the agent's output into existing DB columns your UI reads.
    """
    from db import execute_query

    core = analysis.get("core_impact_assessment", {})
    regime = analysis.get("market_regime_context", {})
    prob = analysis.get("probability_and_confidence", {})
    time_mod = analysis.get("time_modeling", {})
    directional = analysis.get("directional_bias", {})
    meta = analysis.get("_meta", {})

    forex_items = directional.get("forex", []) or []
    crypto_items = directional.get("crypto", []) or []

    query = """
        UPDATE news SET
            analyzed               = TRUE,
            analyzed_at            = NOW(),
            analysis_data          = %s,
            impact_score           = %s,
            impact_summary         = %s,
            affected_markets       = %s,
            impact_duration        = %s,
            market_mode            = %s,
            usd_bias               = %s,
            crypto_bias            = %s,
            execution_window       = %s,
            confidence             = %s,
            conviction_score       = %s,
            volatility_regime      = %s,
            dollar_liquidity_state = %s,
            news_age_label         = %s,
            news_age_human         = %s,
            news_priced_in         = %s
        WHERE id = %s
    """

    params = (
        json.dumps(analysis),
        int(core.get("primary_impact_score", 0) or 0),
        (analysis.get("executive_summary", "") or "")[:500],
        json.dumps(core.get("market_category_scores", {}) or {}),
        (time_mod.get("impact_duration", "") or "")[:100],
        (regime.get("dominant_market_regime", "") or "")[:50],
        (forex_items[0].get("direction", "") if forex_items else "")[:20],
        (crypto_items[0].get("direction", "") if crypto_items else "")[:20],
        (time_mod.get("reaction_speed", "") or "")[:50],
        str(prob.get("overall_confidence_score", ""))[:50],
        int(prob.get("direction_probability_pct", 0) or 0),
        (regime.get("volatility_expectation", "") or "")[:50],
        (regime.get("liquidity_condition_assumption", "") or "")[:50],
        (meta.get("news_age_label", "Fresh") or "")[:20],
        (meta.get("news_age_human", "") or "")[:50],
        bool(meta.get("priced_in_by_history", False) or (meta.get("reaction_status") == "fully_priced")),
        news_id,
    )

    execute_query(query, params)
    _log(f"[SAVE] news_id={news_id}")

    # Auto-create predictions from directional bias
    try:
        create_predictions(news_id, analysis)
    except Exception as pred_err:
        _log(f"[PRED] Failed to create predictions for news_id={news_id}: {pred_err}")


# ── Asset normalization for predictions ──────────────────────

_CRYPTO_SYMBOL_MAP = {
    "bitcoin": "BTC-USD", "btc": "BTC-USD",
    "ethereum": "ETH-USD", "eth": "ETH-USD",
    "solana": "SOL-USD", "sol": "SOL-USD",
    "ripple": "XRP-USD", "xrp": "XRP-USD",
    "cardano": "ADA-USD", "ada": "ADA-USD",
    "dogecoin": "DOGE-USD", "doge": "DOGE-USD",
    "avalanche": "AVAX-USD", "avax": "AVAX-USD",
    "chainlink": "LINK-USD", "link": "LINK-USD",
    "polkadot": "DOT-USD", "dot": "DOT-USD",
    "litecoin": "LTC-USD", "ltc": "LTC-USD",
    "uniswap": "UNI-USD", "uni": "UNI-USD",
    "shiba inu": "SHIB-USD", "shib": "SHIB-USD",
    "polygon": "MATIC-USD", "matic": "MATIC-USD",
}

_FOREX_SYMBOL_MAP = {
    "eur/usd": "EURUSD=X", "eurusd": "EURUSD=X",
    "usd/jpy": "USDJPY=X", "usdjpy": "USDJPY=X",
    "gbp/usd": "GBPUSD=X", "gbpusd": "GBPUSD=X",
    "usd/chf": "USDCHF=X", "usdchf": "USDCHF=X",
    "aud/usd": "AUDUSD=X", "audusd": "AUDUSD=X",
    "usd/cad": "USDCAD=X", "usdcad": "USDCAD=X",
    "nzd/usd": "NZDUSD=X", "nzdusd": "NZDUSD=X",
    "dxy": "DX-Y.NYB", "dollar index": "DX-Y.NYB",
    "dollar": "DX-Y.NYB", "usd": "DX-Y.NYB",
}

_EQUITIES_SYMBOL_MAP = {
    "gold": "GC=F", "xau": "GC=F",
    "oil": "CL=F", "crude": "CL=F", "wti": "CL=F",
    "silver": "SI=F", "xag": "SI=F",
    "s&p": "^GSPC", "s&p 500": "^GSPC", "s&p500": "^GSPC", "spx": "^GSPC",
    "nasdaq": "NQ=F", "qqq": "^IXIC",
    "dow": "^DJI", "dow jones": "^DJI",
    "nikkei": "^N225",
    "dax": "^GDAXI",
    "ftse": "^FTSE",
}

_DURATION_MAP = {
    "short-term": 360,
    "intraday": 720,
    "medium-term": 1440,
    "multi-day": 4320,
    "long-term": 10080,
}


def _normalize_asset_symbol(asset_name: str, asset_class: str) -> str | None:
    """Convert human-readable asset name to a yfinance-compatible symbol."""
    name = (asset_name or "").strip().lower()
    if not name:
        return None

    # Already looks like a ticker
    if name.endswith("-usd") or "=" in name or name.startswith("^"):
        return asset_name.upper()

    if asset_class == "crypto":
        return _CRYPTO_SYMBOL_MAP.get(name)
    elif asset_class == "forex":
        return _FOREX_SYMBOL_MAP.get(name)
    elif asset_class == "global_equities":
        return _EQUITIES_SYMBOL_MAP.get(name)

    # Try all maps as fallback
    return (
        _CRYPTO_SYMBOL_MAP.get(name)
        or _FOREX_SYMBOL_MAP.get(name)
        or _EQUITIES_SYMBOL_MAP.get(name)
    )


def _parse_move_pct(raw: str | int | float) -> float:
    """Parse expected_move_pct from string like '0.5%' or '1-2%' to float."""
    if isinstance(raw, (int, float)):
        return abs(float(raw))
    s = str(raw).strip().replace("%", "")
    # Handle ranges like "1-2" → take average
    if "-" in s:
        parts = s.split("-")
        try:
            return abs(sum(float(p.strip()) for p in parts) / len(parts))
        except ValueError:
            pass
    try:
        return abs(float(s))
    except ValueError:
        return 0.5  # fallback


_DURATION_MAP = {
    # Exact labels the agent emits
    "intraday": 60,
    "short-term": 360,        # 6 hours
    "medium-term": 2880,      # 2 days
    "long-term": 10080,       # 7 days
    # Common synonyms
    "hours": 60,
    "days": 1440,
    "weeks": 10080,
    # Specific ones the agent might write
    "1 hour": 60,
    "2 hours": 120,
    "4 hours": 240,
    "6 hours": 360,
    "8 hours": 480,
    "12 hours": 720,
    "1 day": 1440,
    "2 days": 2880,
    "3 days": 4320,
    "1 week": 10080,
    "2 weeks": 20160,
    "1 month": 43200,
    # Agent sometimes says these
    "day": 1440,
    "week": 10080,
    "month": 43200,
    "hour": 60,
    "minute": 1,
}


def _parse_duration_minutes(label: str) -> int:
    """Map duration label (from the agents) to minutes with regex fallback."""
    import re
    key = (label or "").strip().lower()

    # Direct map lookup first
    if key in _DURATION_MAP:
        return _DURATION_MAP[key]

    # Try regex: patterns like "4 hours", "1-2 days", "30 minutes"
    # Handle ranges like "1-2 days" → take the midpoint
    unit_to_min = {"minute": 1, "hour": 60, "day": 1440, "week": 10080, "month": 43200}
    m = re.search(r"([\d]+)\s*[-–to]+\s*([\d]+)\s*(minute|hour|day|week|month)", key)
    if m:
        low, high, unit = int(m.group(1)), int(m.group(2)), m.group(3)
        return int((low + high) / 2 * unit_to_min.get(unit, 60))

    m = re.search(r"([\d.]+)\s*(minute|hour|day|week|month)", key)
    if m:
        qty, unit = float(m.group(1)), m.group(2)
        return int(qty * unit_to_min.get(unit, 60))

    return 360  # fallback 6 hours


def create_predictions(news_id: int, analysis: dict):
    """
    Parse directional_bias from analysis and insert prediction rows.
    Called automatically after save_analysis.
    """
    from db import execute_query as _exec, fetch_one as _fetch_one
    from tools import _safe_last_close

    directional = analysis.get("directional_bias", {})
    if not directional:
        return

    time_mod = analysis.get("time_modeling", {})
    default_duration_label = time_mod.get("impact_duration", "Short-term") or "Short-term"

    # Use the exact moment analysis was triggered (just saved as analyzed_at)
    # This ensures start_time is the analyst's click time, not publish time
    news_row = _fetch_one("SELECT analyzed_at FROM news WHERE id = %s", (news_id,))
    now = (news_row["analyzed_at"] if news_row and news_row.get("analyzed_at") 
           else datetime.now(timezone.utc))
    created = 0

    for asset_class in ("crypto", "forex", "global_equities"):
        items = directional.get(asset_class, []) or []
        for item in items:
            try:
                raw_asset = item.get("asset") or item.get("pair") or item.get("index") or ""
                direction = item.get("direction", "Neutral") or "Neutral"

                # Skip neutral with no expected move
                raw_move = item.get("expected_move_pct", "0.5%")
                predicted_move = _parse_move_pct(raw_move)
                if predicted_move == 0 and direction.lower() == "neutral":
                    continue

                symbol = _normalize_asset_symbol(raw_asset, asset_class)
                if not symbol:
                    _log(f"[PRED] Could not normalize asset '{raw_asset}' ({asset_class}), skipping")
                    continue

                # Duration
                duration_label = item.get("expected_duration") or default_duration_label
                duration_minutes = _parse_duration_minutes(duration_label)

                # Fetch start price
                start_price = _safe_last_close(symbol)
                if start_price is None:
                    # CoinGecko fallback for crypto
                    if asset_class == "crypto":
                        try:
                            from tools import get_crypto_prices
                            cg_name = raw_asset.strip().lower()
                            prices = get_crypto_prices([cg_name])
                            if cg_name in prices:
                                start_price = float(prices[cg_name])
                        except Exception:
                            pass
                    if start_price is None:
                        _log(f"[PRED] No price for {symbol}, skipping")
                        continue

                # Target price
                if direction.lower() in ("positive", "bullish"):
                    target_price = start_price * (1 + predicted_move / 100)
                elif direction.lower() in ("negative", "bearish"):
                    target_price = start_price * (1 - predicted_move / 100)
                else:
                    target_price = start_price

                # Insert
                _exec(
                    """INSERT INTO predictions
                        (news_id, asset, asset_class, direction,
                         predicted_move_pct, expected_duration_label, expected_duration_minutes,
                         start_time, start_price, target_price)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (news_id, symbol, asset_class, direction,
                     predicted_move, duration_label, duration_minutes,
                     now, start_price, round(target_price, 6)),
                )
                created += 1
                _log(f"[PRED] Created: {symbol} {direction} {predicted_move}% "
                     f"({duration_label}) start={start_price}")

            except Exception as e:
                _log(f"[PRED] Error creating prediction for {raw_asset}: {e}")
                continue

    _log(f"[PRED] Created {created} prediction(s) for news_id={news_id}")



