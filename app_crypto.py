import os, time, hmac, hashlib, base64, json, uuid
from flask import Flask, request, jsonify
import requests

# ====== Config (env vars on Render) ==========================================
CB_BASE = os.getenv("CB_BASE", "https://api.coinbase.com")  # Advanced Trade v3 base
CB_API_KEY = os.environ["CB_API_KEY"]
CB_API_SECRET = os.environ["CB_API_SECRET"]                 # base64 secret from Coinbase
CB_PASSPHRASE = os.getenv("CB_PASSPHRASE", "")              # optional; header added only if present

# Safety: only allow this product id (Coinbase perp identifier)
ALLOWED_PRODUCT_ID = os.getenv("ALLOWED_PRODUCT_ID", "ETH/USD:USD-301220")

# Optional defaults
DEFAULT_TIF = os.getenv("DEFAULT_TIF", "IOC")               # IOC / FOK for market, GTC for limit
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "10"))

# =============================================================================
app = Flask(__name__)

def sign_cb(method: str, path: str, body: str = ""):
    """
    Coinbase Advanced Trade v3 HMAC signature
    """
    ts = str(int(time.time()))
    prehash = ts + method.upper() + path + (body or "")
    secret = base64.b64decode(CB_API_SECRET)
    sig = hmac.new(secret, prehash.encode(), hashlib.sha256).digest()
    return ts, base64.b64encode(sig).decode()

def cb_headers(ts: str, sig: str):
    headers = {
        "Content-Type": "application/json",
        "CB-ACCESS-KEY": CB_API_KEY,
        "CB-ACCESS-SIGN": sig,
        "CB-ACCESS-TIMESTAMP": ts,
    }
    if CB_PASSPHRASE:
        headers["CB-ACCESS-PASSPHRASE"] = CB_PASSPHRASE
    return headers

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/tv")  # TradingView webhook endpoint
def tv():
    """
    Expected TradingView JSON (from your Pine alert_message):
    {
      "ticker": "ETH/USD:USD-301220",
      "action": "buy" | "sell",
      "order_id": "Long" | "Short" | "TP/SL",
      "price": 2500.12,              # optional hint for logging
      "contracts": 1,                 # integer number of contracts
      "timestamp": "2025-09-06T17:18:00Z"
    }
    """
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "invalid_json"}), 400

    product_id = str(data.get("ticker", "")).strip()
    side = str(data.get("action", "")).lower()
    contracts = data.get("contracts")
    client_order_id = (str(data.get("order_id") or uuid.uuid4()).replace(" ", "_"))[:64]
    price_hint = data.get("price")  # for logs only

    # Basic validation
    if product_id != ALLOWED_PRODUCT_ID:
        return jsonify({"ok": False, "error": "product_not_allowed", "got": product_id}), 400
    if side not in ("buy", "sell"):
        return jsonify({"ok": False, "error": "bad_side"}), 400
    try:
        contracts = int(contracts)
    except Exception:
        return jsonify({"ok": False, "error": "contracts_must_be_int"}), 400
    if contracts <= 0:
        return jsonify({"ok": False, "error": "bad_contracts"}), 400

    # ---- Build MARKET order for PERP using contract_size ---------------------
    # For perps, Coinbase expects the number of contracts, not base_size.
    order = {
        "client_order_id": client_order_id,  # idempotency key
        "product_id": product_id,            # e.g., "ETH/USD:USD-301220"
        "side": side,                        # "buy" or "sell"
        "order_configuration": {
            "market_market_ioc": {           # MARKET order (IOC or FOK behavior server-side)
                "contract_size": str(contracts)
            }
        }
    }

    # If you later want LIMIT orders, you can add logic here to switch to:
    # order["order_configuration"] = {
    #   "limit_limit_gtc": {
    #       "limit_price": "XXXX",
    #       "contract_size": str(contracts),
    #       "post_only": False
    #   }
    # }

    body = json.dumps(order, separators=(",", ":"))
    path = "/api/v3/brokerage/orders"
    ts, sig = sign_cb("POST", path, body)
    headers = cb_headers(ts, sig)

    try:
        r = requests.post(CB_BASE + path, headers=headers, data=body, timeout=REQUEST_TIMEOUT)
        ok = r.status_code in (200, 201)
        resp_json = r.json() if r.text else {}
        return jsonify({
            "ok": ok,
            "status": r.status_code,
            "coinbase": resp_json,
            "sent": order,
            "meta": {"price_hint": price_hint}
        }), (200 if ok else 400)
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": "network_error", "details": str(e)}), 502

if __name__ == "__main__":
    # Render sets PORT env; default to 8000 for local runs
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
