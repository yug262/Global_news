"""
Global Macro Intelligence — Tools Layer
Free data sources only.
"""

import requests
import yfinance as yf
from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone
import re

COINGECKO_URL = "https://api.coingecko.com/api/v3"
FEAR_GREED_URL = "https://api.alternative.me/fng/"

# -----------------------------
# Helpers
# -----------------------------

def classify_reaction_status(reaction_pct: float, atr_pct_reference: float) -> str:
    """
    Compare move vs ATR to label priced-in state.
    """
    if atr_pct_reference is None or atr_pct_reference <= 0:
        return "normal_reaction"
    ratio = abs(reaction_pct) / atr_pct_reference
    if ratio < 0.3:
        return "underreacted"
    if ratio < 1.0:
        return "normal_reaction"
    return "fully_priced"


def _safe_last_close(symbol: str):
    try:
        t = yf.Ticker(symbol)
        h = t.history(period="1d")
        if h.empty:
            return None
        return float(h["Close"].iloc[-1])
    except Exception:
        return None


# -----------------------------
# Forex prices
# -----------------------------

def get_forex_prices(pairs: list[str]) -> dict:
    _SYMBOL_MAP = {
        "DXY": "DX-Y.NYB",
        "GOLD": "GC=F",
        "OIL": "CL=F",
    }
    out = {}
    for pair in pairs:
        symbol = _SYMBOL_MAP.get(pair, pair.replace("/", "") + "=X")
        out[pair] = _safe_last_close(symbol)
    return out


# -----------------------------
# Crypto prices (CoinGecko)
# -----------------------------

def get_crypto_prices(coin_ids: list[str] = None) -> dict:
    if not coin_ids:
        coin_ids = ["bitcoin", "ethereum"]
    try:
        ids = ",".join(coin_ids)
        url = f"{COINGECKO_URL}/simple/price"
        params = {"ids": ids, "vs_currencies": "usd"}
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        return {k: float(v["usd"]) for k, v in data.items() if "usd" in v}
    except Exception:
        return {}


# -----------------------------
# Global markets
# -----------------------------

def get_global_markets() -> dict:
    symbols = {
        "SPX": "^GSPC",
        "NASDAQ": "^IXIC",
        "DOW": "^DJI",
        "VIX": "^VIX",
        "US10Y": "^TNX",
        "GOLD": "GC=F",
        "OIL": "CL=F",
        "DXY": "DX-Y.NYB",
    }
    out = {}
    for name, sym in symbols.items():
        out[name] = _safe_last_close(sym)
    return out


# -----------------------------
# Sentiment
# -----------------------------

def get_market_sentiment() -> dict:
    try:
        r = requests.get(FEAR_GREED_URL, timeout=10)
        data = r.json()["data"][0]
        return {
            "fear_greed_value": int(data["value"]),
            "fear_greed_classification": data["value_classification"],
        }
    except Exception:
        return {}


# -----------------------------
# Macro context (light)
# -----------------------------

def get_macro_context() -> dict:
    try:
        dxy = yf.Ticker("DX-Y.NYB").history(period="5d")["Close"]
        us10y = yf.Ticker("^TNX").history(period="5d")["Close"]
        return {
            "dxy_trend_5d_pct": round(float(dxy.pct_change().sum() * 100), 2),
            "us10y_trend_5d_pct": round(float(us10y.pct_change().sum() * 100), 2),
        }
    except Exception:
        return {}


# -----------------------------
# Economic calendar placeholder
# -----------------------------

import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone

def get_economic_calendar() -> dict:
    """
    Scrapes Investing.com economic calendar for high-impact events.
    No API key required.
    """

    try:
        url = "https://www.investing.com/economic-calendar/"
        headers = {
            "User-Agent": "Mozilla/5.0"
        }

        r = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")

        events = []

        rows = soup.select("tr.js-event-item")

        for row in rows[:20]:

            try:
                event = row.select_one(".event").text.strip()
                country = row.select_one(".flagCur").text.strip()

                impact_icons = row.select(".sentiment i.grayFullBullishIcon")
                impact = len(impact_icons)

                time_cell = row.select_one(".time")
                time_text = time_cell.text.strip()

                events.append({
                    "country": country,
                    "event": event,
                    "impact_level": impact,
                    "time": time_text
                })

            except:
                continue

        return {
            "events_found": len(events),
            "events": events
        }

    except Exception as e:
        return {"error": str(e)}


# -----------------------------
# Rate differentials (safe)
# -----------------------------

def get_interest_rate_differentials() -> dict:
    """
    Only returns US10Y from yfinance reliably.
    (Avoids wrong EUR proxy like ^IRX.)
    """
    try:
        us10y = _safe_last_close("^TNX")
        return {"us_10y": us10y, "note": "Add EU/JP yields only if you have a reliable source."}
    except Exception:
        return {}


# -----------------------------
# Duplicate / priced-in check via your DB
# -----------------------------

def search_recent_news(title: str, hours_back: int = 48, similarity_threshold: float = 0.88) -> dict:
    """
    Checks your own DB for similar titles in the last X hours.
    Returns priced_in=True if a very similar story existed and is old enough to be priced.
    """
    try:
        from db import fetch_all

        since = datetime.now(timezone.utc) - timedelta(hours=hours_back)
        rows = fetch_all(
            """
            SELECT title, published
            FROM news
            WHERE published >= %s
            ORDER BY published DESC
            LIMIT 300
            """,
            (since,),
        )

        best = {"score": 0.0, "title": None, "published": None}
        t = (title or "").lower().strip()

        for r in rows:
            tt = (r["title"] or "").lower().strip()
            s = SequenceMatcher(None, t, tt).ratio()
            if s > best["score"]:
                best = {"score": s, "title": r["title"], "published": r["published"]}

        if best["title"] is None:
            return {"priced_in": False, "match_score": 0.0}

        # Consider priced-in only if very similar AND older than 6 hours
        older_than_hrs = None
        if best["published"]:
            older_than_hrs = (datetime.now(timezone.utc) - best["published"]).total_seconds() / 3600.0

        priced_in = bool(best["score"] >= similarity_threshold and older_than_hrs is not None and older_than_hrs >= 6)

        return {
            "priced_in": priced_in,
            "match_score": round(best["score"], 3),
            "matched_title": best["title"],
            "matched_hours_ago": round(older_than_hrs, 2) if older_than_hrs is not None else None,
        }

    except Exception as e:
        return {"priced_in": False, "note": f"db_check_failed: {e}"}


# -----------------------------
# Source credibility (simple)
# -----------------------------

def get_news_source_credibility(source: str) -> dict:
    tier1 = ["reuters", "bloomberg", "wsj", "financial times", "ft.com"]
    tier2 = ["cnbc", "marketwatch", "coindesk", "theblock", "fxstreet", "investing.com"]

    s = (source or "").lower()
    if any(x in s for x in tier1):
        return {"credibility": "High"}
    if any(x in s for x in tier2):
        return {"credibility": "Medium"}
    return {"credibility": "Unknown"}


# -----------------------------
# ATR (volatility reference)
# -----------------------------

def get_asset_atr(symbol: str, period: int = 14) -> dict:
    try:
        t = yf.Ticker(symbol)
        df = t.history(period="30d")
        if df.empty:
            return {}

        df["H-L"] = df["High"] - df["Low"]
        df["H-PC"] = (df["High"] - df["Close"].shift(1)).abs()
        df["L-PC"] = (df["Low"] - df["Close"].shift(1)).abs()

        tr = df[["H-L", "H-PC", "L-PC"]].max(axis=1)
        atr = tr.rolling(period).mean().iloc[-1]
        price = df["Close"].iloc[-1]
        atr_pct = (atr / price) * 100

        return {
            "atr_value": round(float(atr), 6),
            "atr_pct_reference": round(float(atr_pct), 6),
        }
    except Exception:
        return {}


# -----------------------------
# Reaction since publish time
# -----------------------------

def calculate_reaction(symbol: str, published_iso: str) -> dict:
    """
    Calculates move from the first available candle *at or after* publish time to now.
    Handles off-hours and missing intraday data automatically.

    Returns:
      {
        "news_price": float|None,
        "current_price": float|None,
        "reaction_pct": float|None,
        "method": "intraday_15m" | "daily" | "last_close" | "failed",
        "used_timestamp_utc": str|None
      }
    """
    try:
        pub_dt = datetime.fromisoformat(published_iso.strip())
        if pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        pub_dt = pub_dt.astimezone(timezone.utc)

        now_dt = datetime.now(timezone.utc)

        t = yf.Ticker(symbol)

        # ---------- 1) Try intraday 15m ----------
        # Use a wider window so we catch the next tradable candle after publish.
        start = pub_dt - timedelta(hours=6)
        end = now_dt + timedelta(minutes=5)

        df = t.history(start=start, end=end, interval="15m")

        if df is not None and not df.empty:
            # yfinance index is usually tz-aware; normalize to UTC to compare safely
            idx = df.index
            if getattr(idx, "tz", None) is None:
                # assume UTC if missing timezone (rare)
                df.index = df.index.tz_localize(timezone.utc)
            else:
                df.index = df.index.tz_convert(timezone.utc)

            # Pick first candle at/after publish time (NOT before)
            after = df[df.index >= pub_dt]
            if not after.empty:
                news_price = float(after["Close"].iloc[0])
                used_ts = after.index[0]
                current_price = float(df["Close"].iloc[-1])
                reaction_pct = ((current_price - news_price) / news_price) * 100

                return {
                    "news_price": round(news_price, 6),
                    "current_price": round(current_price, 6),
                    "reaction_pct": round(reaction_pct, 6),
                    "method": "intraday_15m",
                    "used_timestamp_utc": used_ts.isoformat(),
                }

        # ---------- 2) Fallback to daily ----------
        # If intraday is missing (market closed or symbol limits), use daily bars.
        df_d = t.history(period="30d", interval="1d")
        if df_d is not None and not df_d.empty:
            # Make index comparable
            idx = df_d.index
            if getattr(idx, "tz", None) is None:
                # daily index can be naive; treat as UTC date boundaries
                # We'll just compare by date.
                pub_date = pub_dt.date()
                # pick first bar with date >= pub_date
                row = df_d[df_d.index.date >= pub_date]
                if not row.empty:
                    news_price = float(row["Close"].iloc[0])
                    used_ts = row.index[0]
                else:
                    news_price = float(df_d["Close"].iloc[-1])
                    used_ts = df_d.index[-1]
            else:
                df_d.index = df_d.index.tz_convert(timezone.utc)
                row = df_d[df_d.index >= pub_dt]
                if not row.empty:
                    news_price = float(row["Close"].iloc[0])
                    used_ts = row.index[0]
                else:
                    news_price = float(df_d["Close"].iloc[-1])
                    used_ts = df_d.index[-1]

            current_price = float(df_d["Close"].iloc[-1])
            reaction_pct = ((current_price - news_price) / news_price) * 100

            return {
                "news_price": round(news_price, 6),
                "current_price": round(current_price, 6),
                "reaction_pct": round(reaction_pct, 6),
                "method": "daily",
                "used_timestamp_utc": used_ts.isoformat() if hasattr(used_ts, "isoformat") else str(used_ts),
            }

        # ---------- 3) Last close fallback ----------
        last_close = t.history(period="5d")["Close"]
        if last_close is not None and len(last_close) > 0:
            current_price = float(last_close.iloc[-1])
            return {
                "news_price": None,
                "current_price": round(current_price, 6),
                "reaction_pct": None,
                "method": "last_close",
                "used_timestamp_utc": None,
            }

        return {"method": "failed"}

    except Exception as e:
        return {"method": "failed", "error": str(e)}

def detect_reaction_headline(title: str) -> dict:
    """
    Detect headlines that describe a realized move like 'surge 8%' / 'drops 6%'.
    This often means the big move already happened before/at publish.
    """
    t = (title or "").lower()

    move_verbs = [
        "surge","soar","jump","rally","climb","rise","gain","advance",
        "drop","tumble","slump","fall","sink","slide","plunge","selloff",
    ]

    has_move_verb = any(v in t for v in move_verbs)

    percents = re.findall(r"(\d+(?:\.\d+)?)\s*%", t)
    headline_move_pct = max([float(x) for x in percents], default=None)

    reaction_headline = bool(has_move_verb and headline_move_pct is not None)

    new_catalyst_words = [
        "rate hike","rate cut","raises rates","cuts rates",
        "sanctions","imposes","strike","attack","missile",
        "approved","approval","etf","ban","lawsuit","sec",
        "bankruptcy","defaults","bailout","emergency"
    ]
    has_new_catalyst = any(w in t for w in new_catalyst_words)

    return {
        "reaction_headline": reaction_headline,
        "headline_move_pct": headline_move_pct,
        "has_new_catalyst": has_new_catalyst,
    }