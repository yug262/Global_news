import re
from datetime import datetime, timezone
from dateutil import parser

# ---------------------------
# DETERMINISTIC VOCABULARY
# ---------------------------

# Maps lowercase keyword to (Actor ID, Category)
ACTORS = {
    # INDIAN REGULATORS & GOV
    "rbi": ("RBI", "INDIA"),
    "reserve bank of india": ("RBI", "INDIA"),
    "sebi": ("SEBI", "INDIA"),
    "nse": ("NSE", "INDIA"),
    "bse": ("BSE", "INDIA"),
    "modi": ("GOV_INDIA", "INDIA"),
    "sitharaman": ("FINMIN", "INDIA"),
    "finance ministry": ("FINMIN", "INDIA"),
    "supreme court": ("SUPREME_COURT", "INDIA"),
    
    # INDIAN CORPORATES (Top)
    "reliance": ("RELIANCE", "INDIA"),
    "adani": ("ADANI", "INDIA"),
    "tcs": ("TCS", "INDIA"),
    "infosys": ("INFOSYS", "INDIA"),
    "hdfc": ("HDFC", "INDIA"),
    "sbi": ("SBI", "INDIA"),
    "icici": ("ICICI", "INDIA"),
    "bharti airtel": ("AIRTEL", "INDIA"),
    "larsen": ("LT", "INDIA"),
    "itc": ("ITC", "INDIA"),
    "wipro": ("WIPRO", "INDIA"),
    "tata motors": ("TATAMOTORS", "INDIA"),
    "tata steel": ("TATASTEEL", "INDIA"),
    "mahindra": ("M_AND_M", "INDIA"),
    "bajaj finance": ("BAJFINANCE", "INDIA"),
    "maruti": ("MARUTI", "INDIA"),
    
    # GLOBAL CENTRAL BANKS & REGULATORS
    "fed": ("FED", "GLOBAL"),
    "federal reserve": ("FED", "GLOBAL"),
    "jerome powell": ("FED", "GLOBAL"),
    "ecb": ("ECB", "GLOBAL"),
    "christine lagarde": ("ECB", "GLOBAL"),
    "boj": ("BOJ", "GLOBAL"),
    "bank of japan": ("BOJ", "GLOBAL"),
    "boe": ("BOE", "GLOBAL"),
    "bank of england": ("BOE", "GLOBAL"),
    "rba": ("RBA", "GLOBAL"),
    "snb": ("SNB", "GLOBAL"),
    "pboc": ("PBOC", "GLOBAL"),
    
    # GLOBAL ENTITIES / REGIONAL
    "opec": ("OPEC", "GLOBAL"),
    "sec": ("SEC", "GLOBAL"),
    
    # FX & COMMODITIES (Major drivers)
    "us dollar": ("USD", "GLOBAL"),
    "usd": ("USD", "GLOBAL"),
    "euro": ("EUR", "GLOBAL"),
    "yen": ("JPY", "GLOBAL"),
    "gold": ("GOLD", "GLOBAL"),
    "crude oil": ("OIL", "GLOBAL"),
    "brent": ("OIL", "GLOBAL"),
    
    # GLOBAL TECH / CORPORATES
    "apple": ("APPLE", "GLOBAL"),
    "nvidia": ("NVIDIA", "GLOBAL"),
    "microsoft": ("MICROSOFT", "GLOBAL"),
    "tesla": ("TESLA", "GLOBAL"),
    "alphabet": ("GOOGLE", "GLOBAL"),
    "google": ("GOOGLE", "GLOBAL"),
    "meta": ("META", "GLOBAL"),
    "amazon": ("AMAZON", "GLOBAL"),
    "tsmc": ("TSMC", "GLOBAL"),
    
    # CRYPTO
    "bitcoin": ("BITCOIN", "CRYPTO"),
    "btc": ("BITCOIN", "CRYPTO"),
    "ethereum": ("ETHEREUM", "CRYPTO"),
    "eth": ("ETHEREUM", "CRYPTO"),
    "solana": ("SOLANA", "CRYPTO"),
    "binance": ("BINANCE", "CRYPTO"),
    "coinbase": ("COINBASE", "CRYPTO"),
    "ftx": ("FTX", "CRYPTO"),
    "tether": ("TETHER", "CRYPTO"),
    "microstrategy": ("MICROSTRATEGY", "CRYPTO"),
}

# Maps lowercase keyword to Situation ID
SITUATIONS = {
    # CENTRAL BANK & MACRO
    "rate": "RATE_DECISION",
    "repo": "RATE_DECISION",
    "interest": "RATE_DECISION",
    "hike": "RATE_DECISION",
    "cut": "RATE_DECISION",
    "inflation": "INFLATION_DATA",
    "cpi": "INFLATION_DATA",
    "pce": "INFLATION_DATA",
    "ppi": "INFLATION_DATA",
    "employment": "EMPLOYMENT_DATA",
    "unemployment": "EMPLOYMENT_DATA",
    "nfp": "EMPLOYMENT_DATA",
    "payrolls": "EMPLOYMENT_DATA",
    "jobless": "EMPLOYMENT_DATA",
    "gdp": "GROWTH_DATA",
    "recession": "ECONOMIC_DOWNTURN",
    
    # CORPORATE
    "earnings": "EARNINGS_REPORT",
    "results": "EARNINGS_REPORT",
    "profit": "EARNINGS_REPORT",
    "q1": "EARNINGS_REPORT",
    "q2": "EARNINGS_REPORT",
    "q3": "EARNINGS_REPORT",
    "q4": "EARNINGS_REPORT",
    "dividend": "DIVIDEND_ANNOUNCEMENT",
    "acquisition": "MNA_DEAL",
    "merger": "MNA_DEAL",
    "buyout": "MNA_DEAL",
    "stake": "STAKE_SALE_BUY",
    "ipo": "IPO_LAUNCH",
    
    # REGULATION & CRISIS
    "investigation": "INVESTIGATION",
    "probe": "INVESTIGATION",
    "notice": "TRIBUNAL_NOTICE",
    "lawsuit": "LAWSUIT",
    "sue": "LAWSUIT",
    "penalty": "PENALTY",
    "scam": "FRAUD_SCAM",
    "fraud": "FRAUD_SCAM",
    "default": "DEBT_DEFAULT",
    "bankruptcy": "BANKRUPTCY",
    
    # GEOPOLITICAL & POLICY
    "budget": "UNION_BUDGET",
    "election": "ELECTION",
    "war": "GEOPOLITICAL_TENSION",
    "missile": "GEOPOLITICAL_TENSION",
    "sanction": "SANCTIONS",
    "tariff": "TARIFFS",
    
    # CRYPTO SPECIFIC
    "etf": "ETF_APPROVAL",
    "halving": "HALVING_EVENT",
    "airdrop": "NETWORK_AIRDROP",
    "hack": "EXCHANGE_HACK"
}


# ---------------------------
# ENGINE
# ---------------------------

def clean_text(text: str) -> str:
    """Removes non-alphanumeric chars (keeps spaces) and lowercases."""
    return re.sub(r'[^a-zA-Z0-9 ]', ' ', text.lower())

def extract_primary_actor_and_category(text: str):
    """Returns (Actor ID, Category) based on the earliest matched keyword."""
    # We find all matches and pick the one that appears first in the text to prioritize the main subject
    matches = []
    for keyword, (actor_id, category) in ACTORS.items():
        # Look for whole words to prevent 'eth' matching 'method'
        pattern = r'\b' + re.escape(keyword) + r'\b'
        match = re.search(pattern, text)
        if match:
            matches.append((match.start(), actor_id, category))
    
    if matches:
        matches.sort(key=lambda x: x[0])  # Sort by position in string
        return matches[0][1], matches[0][2]
    
    return None, None

def extract_primary_situation(text: str):
    """Returns Situation ID based on the earliest matched keyword."""
    matches = []
    for keyword, situation_id in SITUATIONS.items():
        pattern = r'\b' + re.escape(keyword) + r'\b'
        match = re.search(pattern, text)
        if match:
            matches.append((match.start(), situation_id))
    
    if matches:
        matches.sort(key=lambda x: x[0])
        return matches[0][1]
        
    return None


def resolve_event(news_title: str, published_date_str_or_obj=None):
    """
    Deterministically resolves a single perfect event bucket:
    [CATEGORY]_[ACTOR]_[SITUATION]_[YYYY_MM]
    """
    clean_title = clean_text(news_title)
    
    # 1. Parse Date
    date_obj = datetime.now(timezone.utc)
    if published_date_str_or_obj:
        if isinstance(published_date_str_or_obj, str):
            try:
                date_obj = parser.parse(published_date_str_or_obj)
            except:
                pass
        elif isinstance(published_date_str_or_obj, datetime):
            date_obj = published_date_str_or_obj

    # Force format YYYY_MM
    time_bucket = date_obj.strftime("%Y_%m")
    time_display = date_obj.strftime("%b %Y") # e.g. "Mar 2026"
    
    # 2. Extract Entities
    actor, category = extract_primary_actor_and_category(clean_title)
    situation = extract_primary_situation(clean_title)
    
    # 3. Decision Tree
    # If we have both an Actor and a Situation, it's a perfect event!
    if actor and situation:
        event_id = f"{category}_{actor}_{situation}_{time_bucket}"
        
        # Human readable mapping
        sit_words = situation.replace("_", " ").title()
        event_title = f"{actor} {sit_words} ({time_display})"
        
        return {
            "event_id": event_id,
            "event_title": event_title,
            "actor": actor,
            "situation": situation,
            "category": category,
            "time_bucket": time_bucket
        }
        
    # If we ONLY have an Actor, group it as a broad development for that month
    if actor:
        event_id = f"{category}_{actor}_DEVELOPMENTS_{time_bucket}"
        event_title = f"Key Developments: {actor} ({time_display})"
        return {
            "event_id": event_id,
            "event_title": event_title,
            "actor": actor,
            "situation": "DEVELOPMENTS",
            "category": category,
            "time_bucket": time_bucket
        }
        
    # If we ONLY have a Situation but no specific known actor...
    if situation:
        # We don't have a category, default to GLOBAL
        event_id = f"GLOBAL_BROAD_{situation}_{time_bucket}"
        sit_words = situation.replace("_", " ").title()
        event_title = f"Global {sit_words} Activity ({time_display})"
        return {
            "event_id": event_id,
            "event_title": event_title,
            "actor": "GENERAL",
            "situation": situation,
            "category": "GLOBAL",
            "time_bucket": time_bucket
        }

    # If neither, return the general bucket. The UI can choose to hide 'GENERAL_GENERAL' events.
    return {
        "event_id": "GENERAL_GENERAL",
        "event_title": "Unclassified Developments",
        "actor": "GENERAL",
        "situation": "GENERAL",
        "category": "GENERAL",
        "time_bucket": time_bucket
    }