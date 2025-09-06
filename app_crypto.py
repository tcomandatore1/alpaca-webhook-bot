import os, json, uuid, traceback
from flask import Flask, request, jsonify

# --- CCXT (Coinbase futures via unified symbol) ---
import ccxt

# ----- Config -----
SYMBOL = os.getenv("SYMBOL", "ETH/USD:USD-301220")  # CCXT unified symbol (your working one)
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "10"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# Expect these in Render env (same names you used in your other bot)
COINBASE_API_KEY = os.getenv("COINBASE_API_KEY", "")
COINBASE_API_SECRET = os.getenv("COINBASE_API_SECRET", "")

app = Flask(__name__)

# Single shared exchange instance
_exchange = None

def get_exchange():
    global _exchange
    if _exchange is not None:
        return _exchange
    if not COINBASE_API_KEY or not COINBASE_API_SECRET:
        raise RuntimeError("Missing COINBASE_API_KEY or COINBASE_API_SECRET")

    # ccxt.coinbase with futures enabled (this matches your working bot)
    _exchange = ccxt.coinbase({
        "apiKey": COINBASE_API_KEY,
        "secret": COINBASE_API_SECRET,
        "enableRateLimit": True,
        "timeout": int(REQUEST_TIMEOUT * 1000),
        "options": {
            "defaultType": "future"  # IMPORTANT for perps/futures
        }
    })
    _exchange.load_markets()
    return _exchange

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/envcheck")
def envcheck():
    return {
        "has_api_key": bool(COINBASE_API_KEY),
        "has_api_secret": bool(COINBASE_API_SECRET),
        "symbol": SYMBOL
    }

@app.get("/ccxtcheck")
def ccxtcheck():
    try:
        ex = get_exchange()
        ex.load_markets(reload=True)
        m = ex.markets.get(SYMBOL, {})
        return {
            "ok": SYMBOL in ex.markets,
            "symbol": SYMBOL,
            "market_id": m.get("id"),
            "type": m.get("type"),
            "contractSize": m.get("contractSize"),
            "limits": m.get("limits"),
        }
    except Exception as e:
        app.logger.error("ccxtcheck error: %s\n%s", e, traceback.format_exc())
        return {"ok": False, "error": str(e)}, 500

@app.post("/tv")
def tv():
    """
    Expects TradingView alert JSON like:
    {
      "ticker": "ETH/USD:USD-301220",
      "action": "buy" | "sell" | "long" | "short",
      "order_id": "Long" | "Short" | "TP/SL",
      "price": 2500,            # ignored for MARKET
      "contracts": 1,           # integer: number of contracts (1 = 0.1 ETH)
      "timestamp": "..."
    }
    """
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "invalid_json"}), 400

    # Validate/normalize
    tv_symbol = str(data.get("ticker", "")).strip()
    if tv_symbol != SYMBOL:
        return jsonify({"ok": False, "error": "bad_symbol", "got": tv_symbol, "expected": SYMBOL}), 400

    action = str(data.get("action", "")).strip().lower()
    if action in ("buy", "long"):
        side = "buy"
    elif action in ("sell", "short"):
        side = "sell"
    else:
        return jsonify({"ok": False, "error": "bad_side_value", "got": action}), 400

    try:
        contracts = int(data.get("contracts"))
        if contracts <= 0:
            raise ValueError
    except Exception:
        return jsonify({"ok": False, "error": "contracts_must_be_positive_int"}), 400

    base_id = str(data.get("order_id") or "tv").replace(" ", "_")
    client_order_id = f"{base_id}-{uuid.uuid4().hex[:8]}"[:64]

    sent = {
        "symbol": SYMBOL,
        "type": "market",
        "side": side,
        "amount_contracts": contracts,
        "client_order_id": client_order_id
    }

    if DRY_RUN:
        return jsonify({"ok": True, "dry_run": True, "would_send": sent})

    # Place MARKET order in CONTRACTS (ccxt handles mapping for futures)
    try:
        ex = get_exchange()
        # Some venues accept a client order id via params; ccxt will pass it through if supported.
        params = {"clientOrderId": client_order_id}
        order = ex.create_order(SYMBOL, "market", side, contracts, None, params)
        return jsonify({"ok": True, "order": order, "sent": sent})
    except Exception as e:
        app.logger.error("order error: %s\n%s", e, traceback.format_exc())
        return jsonify({"ok": False, "error": "exchange_error", "details": str(e), "sent": sent}), 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
