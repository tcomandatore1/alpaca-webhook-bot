import os, json, uuid, traceback, secrets, time
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import requests
import jwt
from cryptography.hazmat.primitives import serialization

# Load environment variables
load_dotenv()

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

def _build_jwt(method: str, path: str) -> str:
    """Build JWT token for Coinbase API - SAME AS WORKING HFT BOT"""
    try:
        # Remove quotes and handle the PEM key properly
        pem_key = COINBASE_API_SECRET.strip().strip('"')
        app.logger.debug(f"PEM key starts with: {pem_key[:30]}...")
        
        private_key = serialization.load_pem_private_key(pem_key.encode(), password=None)
        now = int(time.time())
        payload = {
            "sub": COINBASE_API_KEY,
            "iss": "cdp",
            "nbf": now,
            "exp": now + 120,
            "uri": f"{method} api.coinbase.com{path}",
        }
        headers = {"kid": COINBASE_API_KEY, "nonce": secrets.token_hex()}
        token = jwt.encode(payload, private_key, algorithm="ES256", headers=headers)
        return token
    except Exception as e:
        app.logger.error(f"JWT build failed: {e}")
        app.logger.error(f"API_KEY: {COINBASE_API_KEY[:20]}...")
        app.logger.error(f"API_SECRET starts: {COINBASE_API_SECRET[:50]}...")
        raise

def place_market_order_jwt(side: str, contracts: int, client_order_id: str) -> dict:
    """Place market order using JWT REST API - SAME AS WORKING HFT BOT"""
    try:
        # Get product_id from exchange
        ex = get_exchange()
        product_id = ex.markets[SYMBOL].get("id")
        if not product_id:
            raise RuntimeError(f"Could not get product_id for {SYMBOL}")
        
        # Build the order payload exactly like HFT bot
        base_size = str(int(contracts))
        oc = {"market_market_ioc": {"base_size": base_size}}
        payload = {
            "client_order_id": client_order_id,
            "product_id": product_id,
            "side": side.upper(),
            "order_configuration": oc
        }
        
        # Make the REST API call with JWT
        path = "/api/v3/brokerage/orders"
        token = _build_jwt("POST", path)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        
        app.logger.info(f"🚀 JWT REST API call: POST {path}")
        app.logger.info(f"Payload: {payload}")
        
        r = requests.post(f"https://api.coinbase.com{path}", 
                         headers=headers, 
                         data=json.dumps(payload), 
                         timeout=10)
        r.raise_for_status()
        resp = r.json()
        
        app.logger.info(f"✅ JWT REST API response: {resp}")
        return resp
        
    except Exception as e:
        app.logger.error(f"JWT REST API order failed: {e}")
        raise

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

    # Place MARKET order using JWT REST API (SAME AS WORKING HFT BOT)
    try:
        app.logger.info(f"🚀 Creating order: {side.upper()} {contracts} contracts using JWT REST API")
        
        # Use the same method as your working HFT bot
        order_result = place_market_order_jwt(side, contracts, client_order_id)
        
        app.logger.info(f"✅ Order created successfully via JWT: {order_result}")
        return jsonify({"ok": True, "order": order_result, "sent": sent, "method": "jwt_rest_api"})
        
    except Exception as e:
        # Enhanced error logging
        app.logger.error(f"❌ JWT REST API Order failed: {e}")
        app.logger.error(f"Full traceback: {traceback.format_exc()}")
        
        error_info = {
            "error_type": type(e).__name__,
            "error_message": str(e),
            "sent_params": sent,
            "method": "jwt_rest_api"
        }
        
        # Try to get more error context
        if hasattr(e, 'args'):
            error_info["error_args"] = e.args
            
        return jsonify({"ok": False, "error": "jwt_rest_api_error", "details": error_info}), 400

# Add global error handler
@app.get("/spottest")
def spottest():
    """Test if we can trade spot ETH (not futures) with current permissions"""
    try:
        ex = get_exchange()
        
        # Test spot ETH symbol instead of futures
        spot_symbol = "ETH/USD"
        
        # Check if spot symbol exists
        if spot_symbol not in ex.markets:
            return {"ok": False, "error": f"Spot symbol {spot_symbol} not found"}
        
        # Try a small spot order (this will fail but show different error if permissions work)
        try:
            # This should fail with different error if permissions are OK
            order = ex.create_order(spot_symbol, "market", "buy", 0.001, None, {})
            return {"ok": True, "spot_order_would_work": True}
        except Exception as spot_error:
            return {
                "ok": False, 
                "spot_error": str(spot_error),
                "spot_symbol": spot_symbol,
                "test_result": "This shows what error we get for SPOT trading with current permissions"
            }
            
    except Exception as e:
        return {"ok": False, "error": str(e)}

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
