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

# Order tracking - maps order_id to Coinbase order details
active_orders = {}

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
        # Handle PEM key format - convert \n to actual newlines if needed
        pem_key = COINBASE_API_SECRET.strip().strip('"')
        
        # If the PEM key contains literal \n characters (from Render), convert them to actual newlines
        if '\\n' in pem_key:
            pem_key = pem_key.replace('\\n', '\n')
            app.logger.debug("Converted literal \\n to actual newlines")
            
        app.logger.debug(f"PEM key starts with: {pem_key[:30]}...")
        app.logger.debug(f"PEM key contains {pem_key.count(chr(10))} actual newlines")
        
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

def place_limit_order_jwt(side: str, contracts: int, price: float, client_order_id: str) -> dict:
    """Place limit order using JWT REST API"""
    try:
        # Get product_id from exchange
        ex = get_exchange()
        product_id = ex.markets[SYMBOL].get("id")
        if not product_id:
            raise RuntimeError(f"Could not get product_id for {SYMBOL}")
        
        # Build the limit order payload
        base_size = str(int(contracts))
        limit_price = str(float(price))  # Convert to string as required by API
        
        # Limit order configuration
        oc = {"limit_limit_gtc": {"base_size": base_size, "limit_price": limit_price}}
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
        
        app.logger.info(f"🎯 JWT REST API LIMIT ORDER call: POST {path}")
        app.logger.info(f"Payload: {payload}")
        
        r = requests.post(f"https://api.coinbase.com{path}", 
                         headers=headers, 
                         data=json.dumps(payload), 
                         timeout=10)
        r.raise_for_status()
        resp = r.json()
        
        app.logger.info(f"✅ JWT REST API LIMIT ORDER response: {resp}")
        return resp
        
    except Exception as e:
        app.logger.error(f"JWT REST API LIMIT ORDER failed: {e}")
        raise

def cancel_order_jwt(coinbase_order_id: str) -> dict:
    """Cancel order using JWT REST API"""
    try:
        # Make the REST API call with JWT
        path = f"/api/v3/brokerage/orders/batch_cancel"
        token = _build_jwt("POST", path)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        
        # Coinbase batch cancel expects array of order IDs
        payload = {"order_ids": [coinbase_order_id]}
        
        app.logger.info(f"🚫 JWT REST API CANCEL ORDER call: POST {path}")
        app.logger.info(f"Payload: {payload}")
        
        r = requests.post(f"https://api.coinbase.com{path}", 
                         headers=headers, 
                         data=json.dumps(payload), 
                         timeout=10)
        r.raise_for_status()
        resp = r.json()
        
        app.logger.info(f"✅ JWT REST API CANCEL ORDER response: {resp}")
        return resp
        
    except Exception as e:
        app.logger.error(f"JWT REST API CANCEL ORDER failed: {e}")
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
      "price": 2500,            # REQUIRED - uses LIMIT order at this price
      "contracts": 1,           # integer: number of contracts (1 = 0.1 ETH)
      "timestamp": "..."
    }
    
    LOGIC:
    - ALWAYS places LIMIT orders using the price field
    - Pine Script sends separate alerts for TP/SL hits with current price
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
    elif action == "cancel":
        side = None  # Cancel action doesn't need side
    else:
        return jsonify({"ok": False, "error": "bad_side_value", "got": action}), 400

    order_id = str(data.get("order_id", "")).strip()
    
    # Handle CANCEL action
    if action == "cancel":
        app.logger.info(f"🚫 Processing CANCEL request for order_id: {order_id}")
        
        # Check if we have this order tracked
        if order_id not in active_orders:
            app.logger.warning(f"Cancel requested for unknown order_id: {order_id}")
            return jsonify({"ok": False, "error": "order_not_found", "order_id": order_id}), 400
        
        if DRY_RUN:
            return jsonify({"ok": True, "dry_run": True, "would_cancel": order_id})
        
        # Cancel the order on Coinbase
        try:
            coinbase_order_id = active_orders[order_id]["coinbase_order_id"]
            app.logger.info(f"🚫 Canceling Coinbase order: {coinbase_order_id} (Pine Script order_id: {order_id})")
            
            cancel_result = cancel_order_jwt(coinbase_order_id)
            
            # Remove from active orders tracking
            del active_orders[order_id]
            
            app.logger.info(f"✅ Order canceled successfully: {cancel_result}")
            return jsonify({"ok": True, "cancel_result": cancel_result, "order_id": order_id, "method": "jwt_cancel_order"})
            
        except Exception as e:
            app.logger.error(f"❌ Cancel order failed: {e}")
            app.logger.error(f"Full traceback: {traceback.format_exc()}")
            return jsonify({"ok": False, "error": "cancel_failed", "order_id": order_id, "details": str(e)}), 400

    # Handle BUY/SELL actions (require contracts and price)
    try:
        contracts = int(data.get("contracts"))
        if contracts <= 0:
            raise ValueError
    except Exception:
        return jsonify({"ok": False, "error": "contracts_must_be_positive_int"}), 400

    # Get price - REQUIRED for all orders
    price = data.get("price")
    
    # Convert price to float - REQUIRED field
    try:
        limit_price = float(price)
        if limit_price <= 0:
            raise ValueError("Price must be positive")
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "valid_price_required", "got": price}), 400
    
    base_id = str(order_id or "tv").replace(" ", "_")
    client_order_id = f"{base_id}-{uuid.uuid4().hex[:8]}"[:64]

    sent = {
        "symbol": SYMBOL,
        "type": "limit",
        "side": side,
        "amount_contracts": contracts,
        "client_order_id": client_order_id,
        "price": limit_price,
        "order_id": order_id
    }

    if DRY_RUN:
        return jsonify({"ok": True, "dry_run": True, "would_send": sent})

    # ALWAYS place LIMIT order
    try:
        app.logger.info(f"🎯 Creating LIMIT order: {side.upper()} {contracts} contracts at ${limit_price} (order_id: {order_id})")
        order_result = place_limit_order_jwt(side, contracts, limit_price, client_order_id)
        
        # Track the order for potential cancellation
        coinbase_order_id = order_result.get("success_response", {}).get("order_id")  # Coinbase returns order_id in nested response
        if coinbase_order_id and order_id in ["Long", "Short"]:  # Only track entry orders that can be canceled
            active_orders[order_id] = {
                "coinbase_order_id": coinbase_order_id,
                "client_order_id": client_order_id,
                "side": side,
                "contracts": contracts,
                "price": limit_price,
                "timestamp": time.time()
            }
            app.logger.info(f"📊 Tracking order: {order_id} -> {coinbase_order_id}")
        else:
            app.logger.warning(f"⚠️  Could not extract order_id for tracking. Response: {order_result}")
        
        app.logger.info(f"✅ LIMIT Order created successfully: {order_result}")
        return jsonify({"ok": True, "order": order_result, "sent": sent, "method": "jwt_limit_order"})
        
    except Exception as e:
        # Enhanced error logging
        app.logger.error(f"❌ LIMIT Order failed: {e}")
        app.logger.error(f"Full traceback: {traceback.format_exc()}")
        
        error_info = {
            "error_type": type(e).__name__,
            "error_message": str(e),
            "sent_params": sent,
            "method": "jwt_limit_order"
        }
        
        # Try to get more error context
        if hasattr(e, 'args'):
            error_info["error_args"] = e.args
            
        return jsonify({"ok": False, "error": "jwt_limit_order_error", "details": error_info}), 400

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

@app.get("/test-limit")
def test_limit():
    """Test endpoint to verify limit order functionality"""
    try:
        ex = get_exchange()
        
        # Get current price to set a reasonable limit price
        ticker = ex.fetch_ticker(SYMBOL)
        current_price = ticker['last']
        
        # Set limit price 1% below current for buy (won't execute immediately)
        test_limit_price = current_price * 0.99
        
        return {
            "ok": True,
            "current_price": current_price,
            "test_limit_price": test_limit_price,
            "symbol": SYMBOL,
            "message": f"Limit order test price would be ${test_limit_price:.2f} (1% below current ${current_price:.2f})"
        }
        
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/active-orders")
def get_active_orders():
    """View currently tracked orders (for debugging)"""
    return {
        "ok": True,
        "active_orders": active_orders,
        "count": len(active_orders),
        "timestamp": time.time()
    }

@app.errorhandler(Exception)
def handle_exception(e):
    app.logger.error(f"Unhandled exception: {e}\n{traceback.format_exc()}")
    return jsonify({"ok": False, "error": "server_error", "message": str(e)}), 500

if __name__ == "__main__":
    # Enable better logging for debugging
    import logging
    logging.basicConfig(level=logging.INFO)
    
    app.logger.info("🚀 Starting Render webhook server with LIMIT ORDER & CANCEL support")
    app.logger.info(f"Symbol: {SYMBOL}")
    app.logger.info(f"Dry run mode: {DRY_RUN}")
    app.logger.info(f"API key configured: {bool(COINBASE_API_KEY)}")
    app.logger.info(f"API secret configured: {bool(COINBASE_API_SECRET)}")
    app.logger.info("📋 Order Logic:")
    app.logger.info("  - BUY/SELL webhooks → LIMIT ORDERS (at price from Pine Script)")
    app.logger.info("  - CANCEL webhooks → CANCEL tracked orders on Coinbase")
    app.logger.info("  - Entry signals: Long/Short at calculated entry price")
    app.logger.info("  - Exit signals: TP/SL at current market price")
    app.logger.info("  - Cancel signals: After X candles if not filled")
    
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
