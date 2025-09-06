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
    
    # Add better error handling for market loading
    try:
        app.logger.info("Loading markets...")
        _exchange.load_markets()
        
        if SYMBOL not in _exchange.markets:
            app.logger.error(f"Symbol {SYMBOL} not found in markets!")
            # Log available ETH symbols for debugging
            eth_symbols = [s for s in _exchange.markets.keys() if 'ETH' in s]
            app.logger.error(f"Available ETH symbols: {eth_symbols}")
            raise RuntimeError(f"Symbol {SYMBOL} not found")
            
        app.logger.info(f"✅ Symbol {SYMBOL} found in markets")
        market = _exchange.markets[SYMBOL]
        app.logger.info(f"Market details: type={market.get('type')}, active={market.get('active')}, contractSize={market.get('contractSize')}")
        
    except Exception as e:
        app.logger.error(f"Failed to load markets: {e}")
        _exchange = None
        raise
        
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
        
        # Test orderbook to verify connection
        try:
            orderbook = ex.fetch_order_book(SYMBOL, limit=5)
            orderbook_ok = bool(orderbook.get('bids') and orderbook.get('asks'))
            if orderbook_ok:
                best_bid = orderbook['bids'][0][0]
                best_ask = orderbook['asks'][0][0]
            else:
                best_bid = best_ask = None
        except Exception as ob_err:
            app.logger.error(f"Orderbook test failed: {ob_err}")
            orderbook_ok = False
            best_bid = best_ask = None
        
        return {
            "ok": SYMBOL in ex.markets,
            "symbol": SYMBOL,
            "market_id": m.get("id"),
            "type": m.get("type"),
            "contractSize": m.get("contractSize"),
            "limits": m.get("limits"),
            "orderbook_accessible": orderbook_ok,
            "best_bid": best_bid,
            "best_ask": best_ask,
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
        app.logger.info(f"📥 Received webhook: {data}")
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
        app.logger.info(f"🚀 Creating order: {side.upper()} {contracts} contracts")
        ex = get_exchange()
        
        # Log the exact call we're making
        app.logger.info(f"Calling: ex.create_order('{SYMBOL}', 'market', '{side}', {contracts}, None, {{'clientOrderId': '{client_order_id}'}})")
        
        # Some venues accept a client order id via params; ccxt will pass it through if supported.
        params = {"clientOrderId": client_order_id}
        
        # This is where your "index out of range" error is likely happening
        order = ex.create_order(SYMBOL, "market", side, contracts, None, params)
        
        app.logger.info(f"✅ Order created successfully: {order}")
        return jsonify({"ok": True, "order": order, "sent": sent})
        
    except Exception as e:
        # Enhanced error logging to pinpoint the issue
        app.logger.error(f"❌ Order failed: {e}")
        app.logger.error(f"Full traceback: {traceback.format_exc()}")
        
        error_info = {
            "error_type": type(e).__name__,
            "error_message": str(e),
            "sent_params": sent
        }
        
        # Special handling for index out of range
        if "index out of range" in str(e).lower():
            app.logger.error("🚨 This is likely a ccxt parsing issue with Coinbase API response")
            error_info["debug_hint"] = "ccxt may be having trouble parsing Coinbase's API response format"
            
        # Try to get more error context
        if hasattr(e, 'args'):
            error_info["error_args"] = e.args
            
        return jsonify({"ok": False, "error": "exchange_error", "details": error_info}), 400

# Add global error handler
@app.errorhandler(Exception)
def handle_exception(e):
    app.logger.error(f"Unhandled exception: {e}\n{traceback.format_exc()}")
    return jsonify({"ok": False, "error": "server_error", "message": str(e)}), 500

if __name__ == "__main__":
    # Enable better logging for debugging
    import logging
    logging.basicConfig(level=logging.INFO)
    
    app.logger.info("🚀 Starting Render webhook server")
    app.logger.info(f"Symbol: {SYMBOL}")
    app.logger.info(f"Dry run mode: {DRY_RUN}")
    app.logger.info(f"API key configured: {bool(COINBASE_API_KEY)}")
    app.logger.info(f"API secret configured: {bool(COINBASE_API_SECRET)}")
    
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
