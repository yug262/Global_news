import os
import sys
import websocket
import json
import random
import string
import threading
import time
import requests
import psycopg2
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))

from app.core.db import DB_CONFIG

# =========================
# API KEY ROTATION
# =========================
class APIKeyRotator:
    """Manages API key rotation every 2 hours across 30 keys (only during market hours)."""
    def __init__(self, api_keys):
        """
        Initialize with list of API keys.
        Args:
            api_keys: List of 30 API keys
        """
        self.api_keys = api_keys if api_keys else []
        self.current_index = 0
        self.last_rotation = datetime.now()
        self.rotation_interval = timedelta(hours=2)
        self.lock = threading.Lock()
        self.is_market_open = False  # Only rotate during market hours
        
    def set_market_status(self, is_open):
        """Update market status to control rotation."""
        with self.lock:
            self.is_market_open = is_open
        
    def get_current_key(self):
        """Get the current API key and rotate if 2 hours have passed (only during market hours)."""
        with self.lock:
            now = datetime.now()
            # Only rotate if market is open and 2 hours have passed since last rotation
            if self.is_market_open and now - self.last_rotation >= self.rotation_interval:
                prev_index = self.current_index
                self.current_index = (self.current_index + 1) % len(self.api_keys)
                self.last_rotation = now
                # Check if we wrapped around from key #30 to key #1
                if self.current_index == 0 and prev_index > 0:
                    print(f"🔄 [API ROTATION] Completed full cycle! Restarting from key #1 at {now.strftime('%Y-%m-%d %H:%M:%S')}")
                else:
                    print(f"🔄 [API ROTATION] Switched to key #{self.current_index + 1} at {now.strftime('%Y-%m-%d %H:%M:%S')}")
            
            return self.api_keys[self.current_index] if self.api_keys else None
    
    def get_all_keys_info(self):
        """Get information about all keys and current key."""
        with self.lock:
            return {
                "total_keys": len(self.api_keys),
                "current_key_index": self.current_index,
                "current_key_number": self.current_index + 1,
                "last_rotation": self.last_rotation,
                "next_rotation": self.last_rotation + self.rotation_interval,
            }

# Initialize API Key Rotator with 30 keys from environment or hardcoded
def init_api_rotator():
    """Initialize API key rotator with keys from environment variables or config."""
    api_keys = []
    
    # Try to load from environment variables (API_KEY_1, API_KEY_2, ..., API_KEY_30)
    for i in range(1, 31):
        key = os.getenv(f"API_KEY_{i}", None)
        if key:
            api_keys.append(key)
    
    # If not enough keys from env, you can set them here or load from a config file
    if len(api_keys) < 30:
        print(f"⚠️ Only {len(api_keys)} API keys loaded from environment (need 30)")
        # Uncomment and add your keys below if needed:
        # api_keys = [
        #     "key1", "key2", ..., "key30"
        # ]
    
    if not api_keys:
        print("⚠️ No API keys configured. API rotation will not work properly.")
        print("ℹ️ Please set API_KEY_1 through API_KEY_30 environment variables.")
        api_keys = [f"dummy_key_{i}" for i in range(1, 31)]  # Fallback dummy keys
    
    return APIKeyRotator(api_keys)

# Global API rotator instance
API_ROTATOR = None

# =========================
# CONFIGURATION
# =========================
# Finnhub key for pair sync (optional, as we can also use TV symbol search)
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")

# Mandatory exotic pairs to ensure they are always tracked
MANDATORY_EXOTICS = [
    "RUBUSD", "USDIRR", "USDILS", "USDINR", "USDSAR", "USDRUB", 
    "EURINR", "GBPINR", "JPYINR", "USDCNH", "USDTRY", "USDZAR",
    "USDMXN", "USDBRL", "USDKRW", "USDIDR"
]

# Standard major/minor pairs to ensure coverage if API discovery fails
DEFAULT_MAJORS = [
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD",
    "EURGBP", "EURJPY", "GBPJPY", "EURCHF", "AUDJPY", "EURCAD", "EURAUD"
]

# =========================
# DATABASE SETUP
# =========================
def init_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True
    

# =========================
# PAIR SYNC (24H)
# =========================
def sync_pairs():
    print("🔄 Syncing forex pairs from multiple sources...")
    
    if not FINNHUB_API_KEY:
        print("❌ ERROR: FINNHUB_API_KEY not found in environment variables.")
        print("⚠️ Falling back to default major and mandatory exotic pairs.")
        
        # Build default list from hardcoded majors and exotics
        # We assume FX_IDC for these as it's the most generic provider
        fallback_pairs = [f"FX_IDC:{p}" for p in (DEFAULT_MAJORS + MANDATORY_EXOTICS)]
        
        # Save them to DB so they persist
        try:
            with psycopg2.connect(**DB_CONFIG) as conn:
                conn.autocommit = True
                with conn.cursor() as cur:
                    for symbol in fallback_pairs:
                        cur.execute("INSERT INTO forex_pairs (symbol) VALUES (%s) ON CONFLICT DO NOTHING", (symbol,))
            print(f"✅ Synced {len(fallback_pairs)} default/mandatory pairs to database.")
        except Exception as db_err:
            print(f"⚠️ Failed to save default pairs to DB: {db_err}")
            
        return fallback_pairs

    try:
        exchanges = ["oanda", "fxcm", "forex.com"]
        canonical_to_provider = {} # pair -> preferred_full_symbol

        # Helper to get canonical pair name (e.g., 'EURUSD')
        def get_canonical(s):
            if ":" in s:
                s = s.split(":")[1]
            return s.replace("_", "").replace("/", "").upper()

        for ex in exchanges:
            url = f"https://finnhub.io/api/v1/forex/symbol?exchange={ex}&token={FINNHUB_API_KEY}"
            
            # Add rotating API key to request if available
            headers = {}
            if API_ROTATOR:
                api_key = API_ROTATOR.get_current_key()
                headers["X-API-Key"] = api_key
                
            res = requests.get(url, headers=headers, timeout=12)
            if res.status_code == 200:
                data = res.json()
                for item in data:
                    full_sym = item["symbol"]
                    # Normalize: 'OANDA:EUR_USD' -> 'OANDA:EURUSD'
                    parts = full_sym.split(":")
                    if len(parts) == 2:
                        provider = parts[0]
                        pair = parts[1].replace("_", "").replace("/", "").upper()
                        norm_full = f"{provider}:{pair}"
                    else:
                        pair = full_sym.replace("_", "").replace("/", "").upper()
                        norm_full = f"FX_IDC:{pair}"
                    
                    canon = get_canonical(full_sym)
                    if len(canon) < 6: continue

                    # Priority: OANDA > FXCM > others
                    if canon not in canonical_to_provider:
                        canonical_to_provider[canon] = norm_full
                    elif "OANDA" in norm_full:
                        canonical_to_provider[canon] = norm_full
                    elif "FXCM" in norm_full and "OANDA" not in canonical_to_provider[canon]:
                        canonical_to_provider[canon] = norm_full

        # Add mandatory exotics if not already present
        for ex_pair in MANDATORY_EXOTICS:
            canon = ex_pair.upper()
            if canon not in canonical_to_provider:
                # Default to FX_IDC for these exotics as it provides broader coverage
                if canon == "RUBUSD": canonical_to_provider[canon] = "FX_IDC:RUBUSD"
                elif canon == "USDIRR": canonical_to_provider[canon] = "FX_IDC:USDIRR"
                elif canon == "USDILS": canonical_to_provider[canon] = "FX_IDC:USDILS"
                elif canon == "USDINR": canonical_to_provider[canon] = "FX_IDC:USDINR"
                elif canon == "USDSAR": canonical_to_provider[canon] = "FX_IDC:USDSAR"
                else: canonical_to_provider[canon] = f"FX_IDC:{canon}"

        unique_symbols = list(canonical_to_provider.values())
        
        with psycopg2.connect(**DB_CONFIG) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                for symbol in unique_symbols:
                    cur.execute("INSERT INTO forex_pairs (symbol) VALUES (%s) ON CONFLICT DO NOTHING", (symbol,))
        
        print(f"✅ Synced {len(unique_symbols)} unique symbols for {len(canonical_to_provider)} forex pairs.")
        return unique_symbols
    except Exception as e:
        print(f"❌ Error syncing pairs: {e}")
    return []

def get_stored_pairs():
    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM forex_pairs")
        pairs = [row[0] for row in cur.fetchall()]
    conn.close()
    return pairs

# =========================
# TRADINGVIEW ENGINE
# =========================
candles = {}  # (symbol, bucket) -> {open, high, low, close, count}
candles_lock = threading.Lock()

def get_bucket(ts):
    minute = (ts.minute // 3) * 3
    return ts.replace(minute=minute, second=0, microsecond=0)

def process_tick(symbol, price):
    now = datetime.utcnow()
    bucket = get_bucket(now)
    key = (symbol, bucket)

    with candles_lock:
        if key not in candles:
            candles[key] = {"open": price, "high": price, "low": price, "close": price}
        else:
            c = candles[key]
            c["high"] = max(c["high"], price)
            c["low"] = min(c["low"], price)
            c["close"] = price

def flush_candles():
    now = datetime.utcnow()
    to_delete = []
    
    with candles_lock:
        stored_items = list(candles.items())

    if not stored_items:
        return

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True
    with conn.cursor() as cur:
        for (symbol, t), c in stored_items:
            # Save candle once the 3-minute bucket has passed
            if now >= t + timedelta(minutes=3):
                cur.execute("""
                INSERT INTO forex_candles_3m (symbol, time, open, high, low, close)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, time) DO NOTHING
                """, (symbol, t, c["open"], c["high"], c["low"], c["close"]))
                to_delete.append((symbol, t))
                print(f"💾 Saved 3m candle for {symbol} at {t}")

    with candles_lock:
        for key in to_delete:
            if key in candles:
                del candles[key]
    conn.close()

def cleanup_old_data():
    """Remove candles older than 24 hours from forex_candles_3m."""
    print("🧹 Cleaning up old forex candle data...")
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        conn.autocommit = True
        with conn.cursor() as cur:
            # Delete candles older than 24 hours
            cur.execute("DELETE FROM forex_candles_3m WHERE time < %s", (datetime.utcnow() - timedelta(hours=24),))
            print(f"✅ Cleanup complete: Removed {cur.rowcount} old candles.")
        conn.close()
    except Exception as e:
        print(f"❌ Cleanup Failed: {e}")

# WebSocket Helpers
def gen_session():
    return "qs_" + "".join(random.choice(string.ascii_lowercase) for _ in range(12))

def format_msg(msg):
    m = json.dumps(msg)
    return f"~m~{len(m)}~m~{m}"

def parse_messages(message):
    """
    Parses TradingView WebSocket messages. 
    Handles multi-packet messages (~m~len~m~json~m~len~m~json...)
    Returns list of (symbol, price) updates.
    """
    updates = []
    try:
        parts = message.split("~m~")
        for i in range(2, len(parts), 2):
            raw = parts[i]
            payload = json.loads(raw)
            if payload.get("m") == "qsd":
                p = payload.get("p", [])
                if len(p) >= 2:
                    data = p[1]
                    symbol = data.get("n")
                    v = data.get("v", {})
                    # Last price is preferred, fallback to bid or ask
                    price = v.get("lp") 
                    if price is None: price = v.get("bid")
                    if price is None: price = v.get("ask")
                    
                    if symbol and price is not None:
                        updates.append((symbol, price))
    except Exception:
        pass
    return updates

class TVStreamer:
    def __init__(self, symbols):
        self.symbols = symbols
        self.ws = None
        self.sessions = []

    def on_message(self, ws, message):
        if message.startswith("~h~"):
            ws.send(message)
            return

        updates = parse_messages(message)
        for symbol, price in updates:
            process_tick(symbol, price)

    def on_open(self, ws):
        self.sessions = []
        # Set market status to open when actively streaming
        if API_ROTATOR:
            API_ROTATOR.set_market_status(True)
        
        # TradingView allows ~100 symbols per session. 
        # We'll split our symbols into multiple sessions if needed.
        batch_size = 80 # Using 80 to be safe
        for i in range(0, len(self.symbols), batch_size):
            session = gen_session()
            self.sessions.append(session)
            
            ws.send(format_msg({"m": "quote_create_session", "p": [session]}))
            # Request Last Price, Bid, and Ask to cover all symbol types
            ws.send(format_msg({"m": "quote_set_fields", "p": [session, "lp", "bid", "ask"]}))
            
            batch = self.symbols[i : i + batch_size]
            for sym in batch:
                ws.send(format_msg({"m": "quote_add_symbols", "p": [session, sym]}))
        
        print(f"🛰️ Streaming {len(self.symbols)} symbols across {len(self.sessions)} sessions...")

    def on_error(self, ws, error):
        if "opcode=8" not in str(error):
            print(f"❌ WS Error: {error}")
        # Disable market status on error
        if API_ROTATOR:
            API_ROTATOR.set_market_status(False)

    def on_close(self, ws, a, b):
        # Disable market status when connection closes
        if API_ROTATOR:
            API_ROTATOR.set_market_status(False)
        print("🔌 Connection closed. Reconnecting in 10s...")
        time.sleep(10)
        self.start()

    def start(self):
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        self.ws = websocket.WebSocketApp(
            "wss://data.tradingview.com/socket.io/websocket",
            on_message=self.on_message,
            on_open=self.on_open,
            on_error=self.on_error,
            on_close=self.on_close,
            header=headers
        )
        self.ws.run_forever(origin="https://www.tradingview.com")

# =========================
# MAIN
# =========================
def main():
    global API_ROTATOR
    
    # Initialize API Key Rotator
    API_ROTATOR = init_api_rotator()
    print(f"✅ API Key Rotator initialized with {len(API_ROTATOR.api_keys)} keys")
    print(f"🔄 Rotation interval: Every 2 hours")
    
    init_db()
    
    # Initial Sync
    pairs = sync_pairs()
    
    # If sync failed (API error or DB error), try to get from DB
    if not pairs:
        print("⚠️ Sync returned no pairs, attempting to load from database...")
        pairs = get_stored_pairs()
    
    # Final safety fallback: If still no pairs, use hardcoded defaults
    if not pairs:
        print("⚠️ Database empty and sync failed. Using hardcoded fallback lists.")
        pairs = [f"FX_IDC:{p}" for p in (DEFAULT_MAJORS + MANDATORY_EXOTICS)]
    
    if not pairs:
        print("⚠️ No pairs found. Syncing might have failed.")
        return

    # Start Flush Thread
    def flusher():
        while True:
            try:
                flush_candles()
            except Exception as e:
                print(f"⚠️ Flush Error: {e}")
            time.sleep(30) # Check every 30s for completed buckets

    threading.Thread(target=flusher, daemon=True).start()

    # Start Pair Sync Thread (24h)
    def sync_loop():
        while True:
            time.sleep(86400)
            sync_pairs()

    threading.Thread(target=sync_loop, daemon=True).start()

    # Start Cleanup Thread (10m)
    def cleanup_run_loop():
        # First cleanup immediately on start
        try:
            cleanup_old_data()
        except Exception as e:
            print(f"⚠️ Cleanup Initial Error: {e}")
            
        while True:
            time.sleep(600) # Every 10 minutes
            try:
                cleanup_old_data()
            except Exception as e:
                print(f"⚠️ Cleanup Loop Error: {e}")

    threading.Thread(target=cleanup_run_loop, daemon=True).start()

    # Start API Key Rotation Monitor Thread (logs status every 30 minutes)
    def api_rotation_monitor():
        while True:
            time.sleep(1800)  # Every 30 minutes
            info = API_ROTATOR.get_all_keys_info()
            print(f"\n📊 [API ROTATION STATUS]")
            print(f"   Total Keys: {info['total_keys']}")
            print(f"   Current Key: #{info['current_key_number']}")
            print(f"   Last Rotation: {info['last_rotation'].strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"   Next Rotation: {info['next_rotation'].strftime('%Y-%m-%d %H:%M:%S')}\n")

    threading.Thread(target=api_rotation_monitor, daemon=True).start()

    # Start Streaming
    # Our new TVStreamer handles multi-session internally to bypass the 100-symbol limit.
    # Streamer will set market status to True when connected, False when disconnected
    streamer = TVStreamer(pairs)
    streamer.start()

if __name__ == "__main__":
    main()