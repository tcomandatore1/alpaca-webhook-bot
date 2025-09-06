import os, json, uuid, traceback
from flask import Flask, request, jsonify
import requests

# Try to import Coinbase JWT builder early and give a helpful error if missing.
try:
    from coinbase import jwt_generator  # from coinbase-advanced-py
except Exception as e:
    jwt_generator = None
    IMPORT_ERR = f"coinbase-advanced-py import failed: {e}"
else:
    IMPORT_ERR = None

CB_BASE = os.getenv("CB_BASE", "https://api.coinbase.com")
ALLOWED_PRODUCT_ID = os.getenv("ALLOWED_PRODUCT_ID", "ETH/USD:USD-301220")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "10"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

app = Flask(__name__)

def get_env():
    # Use getenv so missing vars don't crash at import time
    return {
        "CB_API_KEY": os.getenv("CB_API_KEY"),
        "CB_API_SECRET": os.getenv("CB_API_SECRET"),
    }

def bearer_headers(method: str, path: str):
    env = get_env()
    api_key = env["CB_API_KEY"]
    api_secret = env["CB_API_SECRET"]
    if not jwt_generator:
        raise RuntimeError(IMPORT_ERR or "coinbase-advanced-py not installed")
    if not api_key or not api_secret:
        raise RuntimeError("Missing CB_API_KEY or CB_API_SECRET in environment")

    # IMPORTANT: CB_API_KEY must be the full key name:
    # organizations/{org_id}/apiKeys/{key_id}
    # CB_API_SECRET must be the PEM private key; newlines OK.
    jwt_uri = jwt_generator.format_jwt_uri(method, path)
    token = jwt_generator.build_rest_jwt(jwt_uri, api_key, api_secret)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/authcheck")
def authcheck():
    try:
        headers = bearer_headers("GET", "/api/v3/brokerage/accounts")
        r = requests.get(CB_BASE + "/api/v3/brokerage/accounts", headers=headers, timeout=REQUEST_TIMEOUT)
        try:
            j = r.json()
        except ValueError:
            j = None
        return jsonify({
            "ok": r.status_code == 200,
            "status": r.status_code,
            "json": j,
            "text": (None if j is not None else r.text)
        }), (200 if r.status_code == 200 else 400)
    except Exception as e:
        app.logger.error("authcheck error: %s\n%s", e, traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/envcheck")
def envcheck():
    # Safe checker—does NOT return values, just which are present.
    env = get_env()
    return {
        "has_CB_API_KEY": bool(env["CB_API_KEY"]),
        "has_CB_API_SECRET": bool(env["CB_API_SECRET"]),
        "has_coinbase_lib": IMPORT_ERR is None,
        "allowed_product_id": ALLOWED_PRODUCT_ID
    }

@app.post("/tv")
def tv():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "invalid_json"}), 400

    product_id = str(data.get("ticker", "")).strip()
    side = str(data.get("action", "")).lower()
    order_id = (str(data.get("order_id") or uuid.uuid4()).replace(" ", "_"))[:64]
    price_hint = data.get("price")

    # Validate contracts (must be int for perps)
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

    order = {
        "client_order_id": order_id,
        "product_id": product_id,
        "side": side,
        "order_configuration": {
            "market_market_ioc": {
                "contract_size": str(contracts)   # perps expect number of contracts
            }
        }
    }

    if DRY_RUN:
        return jsonify({"ok": True, "dry_run": True, "would_send": order, "meta": {"price_hint": price_hint}})

    body = json.dumps(order, separators=(",", ":"))
    path = "/api/v3/brokerage/orders"

    try:
        headers = bearer_headers("POST", path)
        r = requests.post(CB_BASE + path, headers=headers, data=body, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        app.logger.error("order error: %s\n%s", e, traceback.format_exc())
        return jsonify({"ok": False, "error": "exception", "details": str(e)}), 500

    try:
        resp_json = r.json() if r.text else {}
    except ValueError:
        resp_json = None

    ok = r.status_code in (200, 201)
    resp = {
        "ok": ok,
        "status": r.status_code,
        "coinbase_json": resp_json,
        "coinbase_text": (None if resp_json is not None else r.text),
        "sent": order,
        "meta": {"price_hint": price_hint, "response_headers": dict(r.headers)}
    }
    return jsonify(resp), (200 if ok else 400)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
