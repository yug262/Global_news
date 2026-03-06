"""
Prediction Monitor — Background worker that tracks AI predictions.

Runs every 3 minutes:
  1. Loads all non-finalized predictions
  2. Fetches current prices
  3. Updates MFE / MAE
  4. Finalizes when duration expires

Usage:
    python prediction_monitor.py
"""

import time
import traceback
from datetime import datetime, timezone, timedelta

from app.core.db import fetch_all, execute_query
from app.core.tools import _safe_last_close

# ── Config ──────────────────────────────────────────────
CHECK_INTERVAL_SECONDS = 30  # 3 minutes

# Reverse map: yfinance symbol → coingecko id  (for crypto fallback)
_YF_TO_COINGECKO = {
    "BTC-USD": "bitcoin",
    "ETH-USD": "ethereum",
    "SOL-USD": "solana",
    "XRP-USD": "ripple",
    "ADA-USD": "cardano",
    "DOGE-USD": "dogecoin",
    "AVAX-USD": "avalanche-2",
    "LINK-USD": "chainlink",
    "DOT-USD": "polkadot",
    "MATIC-USD": "matic-network",
    "SHIB-USD": "shiba-inu",
    "LTC-USD": "litecoin",
    "UNI-USD": "uniswap",
    "ATOM-USD": "cosmos",
}


def _log(msg: str):
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode(), flush=True)


def _fetch_price(symbol: str) -> float | None:
    """
    Get the latest real-time price for a symbol.
    Uses yfinance 1-minute intraday tick so indices/equities update during market hours.
    Falls back to CoinGecko for crypto assets.
    """
    import yfinance as yf

    try:
        # 1d period at 5m interval = intraday price (updates every 5 min)
        tk = yf.Ticker(symbol)
        hist = tk.history(period="1d", interval="5m")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception:
        pass

    # Fallback to daily close (for weekends or off-hours)
    try:
        price = _safe_last_close(symbol)
        if price is not None:
            return price
    except Exception:
        pass

    # CoinGecko fallback for crypto
    cg_id = _YF_TO_COINGECKO.get(symbol)
    if cg_id:
        try:
            from app.core.tools import get_crypto_prices
            prices = get_crypto_prices([cg_id])
            if cg_id in prices:
                return float(prices[cg_id])
        except Exception:
            pass

    return None


def _compute_move_pct(start_price: float, current_price: float) -> float:
    """Percentage move from start to current."""
    if start_price == 0:
        return 0.0
    return ((current_price - start_price) / start_price) * 100.0


def _finalize_prediction(pred: dict, final_price: float, now: datetime):
    """Determine final status and update DB."""
    pred_id = pred["id"]
    start_price = float(pred["start_price"])
    direction = (pred["direction"] or "").strip()
    predicted_move = float(pred["predicted_move_pct"])
    mfe = float(pred["mfe_pct"] or 0)
    final_move = _compute_move_pct(start_price, final_price)

    # Determine status
    if direction.lower() in ("positive", "bullish"):
        if mfe >= predicted_move * 1.5:
            status = "overperformed"
        elif mfe >= predicted_move:
            status = "hit"
        elif final_move > 0:
            status = "underperformed"
        else:
            status = "wrong"
    elif direction.lower() in ("negative", "bearish"):
        if mfe >= predicted_move * 1.5:
            status = "overperformed"
        elif mfe >= predicted_move:
            status = "hit"
        elif final_move < 0:
            # Moved in correct (negative) direction but not enough
            status = "underperformed"
        else:
            status = "wrong"
    elif direction.lower() == "neutral":
        if abs(final_move) <= 0.2:
            status = "hit"
        else:
            status = "wrong"
    else:
        status = "expired"

    execute_query(
        """UPDATE predictions SET
            finalized = TRUE,
            finalized_at = %s,
            final_price = %s,
            final_move_pct = %s,
            status = %s,
            last_checked_at = %s,
            last_price = %s,
            last_move_pct = %s
        WHERE id = %s""",
        (now, final_price, round(final_move, 4), status, now,
         final_price, round(final_move, 4), pred_id),
    )
    _log(f"  ✅ FINALIZED #{pred_id} {pred['asset']} → {status} "
         f"(final={final_move:+.2f}%, mfe={mfe:.2f}%)")


def check_predictions():
    """Main loop iteration: check and update all pending predictions."""
    preds = fetch_all(
        "SELECT * FROM predictions WHERE finalized = FALSE AND status = 'pending'"
    )

    if not preds:
        _log("[PRED] No pending predictions.")
        return

    _log(f"[PRED] Checking {len(preds)} pending prediction(s)...")
    now = datetime.now(timezone.utc)

    for pred in preds:
        pred_id = pred["id"]
        symbol = pred["asset"]
        try:
            current_price = _fetch_price(symbol)
            if current_price is None:
                _log(f"  ⚠️ #{pred_id} {symbol}: price unavailable, skipping")
                continue

            start_price = float(pred["start_price"])
            direction = (pred["direction"] or "").strip().lower()
            predicted_move = float(pred["predicted_move_pct"])
            raw_move = _compute_move_pct(start_price, current_price)

            # MFE / MAE calculation depends on direction
            old_mfe = float(pred["mfe_pct"] or 0)
            old_mae = float(pred["mae_pct"] or 0)

            if direction in ("positive", "bullish"):
                favorable = raw_move   # positive move is favorable
                adverse = raw_move     # negative move is adverse
                new_mfe = max(old_mfe, favorable)
                new_mae = min(old_mae, adverse)
            elif direction in ("negative", "bearish"):
                favorable = -raw_move  # downward move is favorable (store as positive)
                adverse = -raw_move    # upward move is adverse
                new_mfe = max(old_mfe, favorable)
                new_mae = min(old_mae, adverse)
            else:
                # Neutral: MFE = max abs move, MAE same
                new_mfe = max(old_mfe, abs(raw_move))
                new_mae = min(old_mae, -abs(raw_move))

            # Check if duration expired
            start_time = pred["start_time"]
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=timezone.utc)
            duration_minutes = int(pred["expected_duration_minutes"])
            expiry_time = start_time + timedelta(minutes=duration_minutes)

            if now >= expiry_time:
                # Update MFE/MAE one last time before finalizing
                execute_query(
                    "UPDATE predictions SET mfe_pct = %s, mae_pct = %s WHERE id = %s",
                    (round(new_mfe, 4), round(new_mae, 4), pred_id),
                )
                pred["mfe_pct"] = new_mfe
                _finalize_prediction(pred, current_price, now)
            else:
                # Just update tracking fields
                execute_query(
                    """UPDATE predictions SET
                        last_checked_at = %s,
                        last_price = %s,
                        last_move_pct = %s,
                        mfe_pct = %s,
                        mae_pct = %s
                    WHERE id = %s""",
                    (now, current_price, round(raw_move, 4),
                     round(new_mfe, 4), round(new_mae, 4), pred_id),
                )
                remaining = expiry_time - now
                _log(f"  📊 #{pred_id} {symbol}: move={raw_move:+.2f}% "
                     f"mfe={new_mfe:.2f}% mae={new_mae:.2f}% "
                     f"(expires in {remaining})")

        except Exception as e:
            _log(f"  ❌ #{pred_id} {symbol}: ERROR {e}")
            traceback.print_exc()
            try:
                execute_query(
                    "UPDATE predictions SET status = 'error', error = %s WHERE id = %s",
                    (str(e)[:500], pred_id),
                )
            except Exception:
                pass



