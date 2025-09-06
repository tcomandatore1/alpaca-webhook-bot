import os, json, uuid
from flask import Flask, request, jsonify
import requests
from coinbase import jwt_generator  # builds JWT for Coinbase Advanced Trade

# ===== Env (set in Render) =========================================
CB_BASE = os.getenv("CB_BASE", "https://api.coinbase.com")
# IMPORTANT: api_key is the full key *name* like: organizations/{org_id}/apiKeys/{key_id}
CB_API_KEY = os.environ["CB_API_KEY"]
# IMPORTANT: api_secret is the *PEM* private key, multi-line preserved with \n
CB_API_SECRET = os.environ["CB_API_SECRET"]

ALLOWED_PRODUCT_ID = os.getenv("ALLOWED_PRODUCT_ID", "ETH/USD:USD-301220")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "10"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

app = Flask(__name__)

def bearer_headers(method: str, path: str):
    """
    Coinbase Advanced Trade v3 uses a per-request JWT (ES256).
    We sign (method + path) and send it as Authorization: Bearer <jwt>.
    """
    jwt_uri = jwt_generator.format_jwt_uri(method, path)
    token = jwt_generator.build_rest_jwt(jwt_uri, CB_API_KEY, CB_API_SECRET)
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/authcheck")
def authcheck():
    path = "/api/v3/brokerage/accounts"
    headers = bearer_headers("GET", path)
    try:
        r = requests.get(CB_BASE + path, headers=headers, timeout=REQUEST_TIMEOUT)
        try:
            j = r.json()
        except ValueError:
            j = None
        return jsonify({"ok": r.status_code == 200, "status": r.status_code,
                        "json": j, "text": (None if j is not None else r.text)}), (200 if r.status_code == 200 else 400)
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": "network_error", "details": str(e)}), 502

@app.post("/tv")
def tv():
    """
    Expects TradingView JSON via alert_message:
    {
      "ticker": "ETH/USD:USD-301220",
      "action": "buy" | "sell",
      "order_id": "Long" | "Short" | "TP/SL",
      "price": 2500.12,
      "contracts": 1,
      "timestamp": "..."
    }
    """
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "invalid_json"}), 400

    product_id = str(data.get("ticker", "")).strip()
    side = str(data.get("action", "")).lower()
    client_order_id = (str(data.get("order_id") or uuid.uuid4()).replace(" ", "_"))[:64]
    price_hint = data.get("price")

    # contracts must be an integer (perps API expects count, not base size)
    contracts_raw = data.get("contracts")
    try:
        contracts = int(contracts_raw)
    except Exception:
        return jsonify({"ok": False, "error": "contracts_must_be_int", "got": contracts_raw}), 400
    if contracts <= 0:
        return jsonify({"ok": False, "error": "bad_contracts"}), 400

    if product_id != ALLOWED_PRODUCT_ID:
        return jsonify({"ok": False, "error": "product_not_allowed", "got": product_id}), 400
    if side not in ("buy", "sell"):
        return jsonify({"ok": False, "error": "bad_side"}), 400

    # Build MARKET IOC order using contract_size
    order = {
        "client_order_id": client_order_id,
        "product_id": product_id,
        "side": side,
        "order_configuration": {
            "market_market_ioc": {
                "contract_size": str(contracts)
            }
        }
    }

    if DRY_RUN:
        return jsonify({"ok": True, "dry_run": True, "would_send": order, "meta": {"price_hint": price_hint}})

    body = json.dumps(order, separators=(",", ":"))
    path = "/api/v3/brokerage/orders"
    headers = bearer_headers("POST", path)

    try:
        r = requests.post(CB_BASE + path, headers=headers, data=body, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": "network_error", "details": str(e)}), 502

    # Safe parse
    try:
        resp_json = r.json() if r.text else {}
    except ValueError:
        resp_json = None

    ok = r.status_code in (200, 201)
    return jsonify({
        "ok": ok,
        "status": r.status_code,
        "coinbase_json": resp_json,
        "coinbase_text": (None if resp_json is not None else r.text),
        "sent": order,
        "meta": {"price_hint": price_hint, "response_headers": dict(r.headers)}
    }), (200 if ok else 400)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
