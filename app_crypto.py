import os, time, hmac, hashlib, base64, json, uuid
from flask import Flask, request, jsonify
import requests

CB_BASE = os.getenv("CB_BASE", "https://api.coinbase.com")
CB_API_KEY = os.environ["CB_API_KEY"]
CB_API_SECRET = os.environ["CB_API_SECRET"]                 # base64 secret from Coinbase (exactly as shown when you created the key)
CB_PASSPHRASE = os.getenv("CB_PASSPHRASE", "")
ALLOWED_PRODUCT_ID = os.getenv("ALLOWED_PRODUCT_ID", "ETH/USD:USD-301220")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "10"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"   # set to true to test without sending orders

app = Flask(__name__)

def sign_cb(method: str, path: str, body: str = ""):
    ts = str(int(time.time()))
    prehash = ts + method.upper() + path + (body or "")
    secret = base64.b64decode(CB_API_SECRET)  # MUST be base64-decoded
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

@app.post("/tv")
def tv():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "invalid_json"}), 400

    product_id = str(data.get("ticker", "")).strip()
    side = str(data.get("action", "")).lower()
    contracts_raw = data.get("contracts")
    client_order_id = (str(data.get("order_id") or uuid.uuid4()).replace(" ", "_"))[:64]
    price_hint = data.get("price")

    if product_id != ALLOWED_PRODUCT_ID:
        return jsonify({"ok": False, "error": "product_not_allowed", "got": product_id}), 400
    if side not in ("buy", "sell"):
        return jsonify({"ok": False, "error": "bad_side"}), 400
    try:
        contracts = int(contracts_raw)
    except Exception:
        return jsonify({"ok": False, "error": "contracts_must_be_int", "got": contracts_raw}), 400
    if contracts <= 0:
        return jsonify({"ok": False, "error": "bad_contracts"}), 400

    order = {
        "client_order_id": client_order_id,
        "product_id": product_id,
        "side": side,
        "order_configuration": {
            "market_market_ioc": {
                "contract_size": str(contracts)  # <- perps expect number of contracts
            }
        }
    }

    # Allow dry testing without touching Coinbase
    if DRY_RUN:
        return jsonify({
            "ok": True,
            "dry_run": True,
            "would_send": order,
            "meta": {"price_hint": price_hint}
        })

    body = json.dumps(order, separators=(",", ":"))
    path = "/api/v3/brokerage/orders"
    ts, sig = sign_cb("POST", path, body)
    headers = cb_headers(ts, sig)

    try:
        r = requests.post(CB_BASE + path, headers=headers, data=body, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": "network_error", "details": str(e)}), 502

    # Safe JSON parse (don’t crash on HTML/text errors)
    try:
        resp_json = r.json() if r.text else {}
    except ValueError:
        resp_json = None  # not JSON (likely HTML/plain text)

    ok = r.status_code in (200, 201)
    return jsonify({
        "ok": ok,
        "status": r.status_code,
        "coinbase_json": resp_json,
        "coinbase_text": (None if resp_json is not None else r.text),
        "sent": order,
        "meta": {
            "price_hint": price_hint,
            "response_headers": dict(r.headers)
        }
    }), (200 if ok else 400)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
