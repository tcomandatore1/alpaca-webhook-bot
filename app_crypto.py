import os, json, uuid, traceback
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from flask import Flask, request, jsonify
import requests

# Coinbase JWT helper (from coinbase-advanced-py)
try:
    from coinbase import jwt_generator
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
    return {
        "CB_API_KEY": os.getenv("CB_API_KEY"),     # organizations/{org_id}/apiKeys/{key_id}
        "CB_API_SECRET": os.getenv("CB_API_SECRET")  # PEM private key with real newlines (or \n normalized)
    }

def _normalize_pem(s: str) -> str:
    if not s:
        return ""
    s = s.strip().strip('"').strip("'")
    if "\\n" in s and "\n" not in s:
        s = s.replace("\\n", "\n")
    return s

def bearer_headers(method: str, path: str):
    if not jwt_generator:
        raise RuntimeError(IMPORT_ERR or "coinbase-advanced-py not installed")
    env = get_env()
    api_key = env["CB_API_KEY"]
    api_secret = _normalize_pem(env["CB_API_SECRET"])
    if not api_key or not api_secret:
        raise RuntimeError("Missing CB_API_KEY or CB_API_SECRET in environment")
    jwt_uri = jwt_generator.format_jwt_uri(method, path)
    token = jwt_generator.build_rest_jwt(jwt_uri, api_key, api_secret)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/envcheck")
def envcheck():
    env = get_env()
    pem = _normalize_pem(env["CB_API_SECRET"] or "")
    return {
        "has_CB_API_KEY": bool(env["CB_API_KEY"]),
        "has_CB_API_SECRET": bool(env["CB_API_SECRET"]),
        "has_coinbase_lib": IMPORT_ERR is None,
        "pem_starts_with_BEGIN": pem.startswith("-----BEGIN"),
        "allowed_product_id": ALLOWED_PRODUCT_ID
    }

# ---- Product cache & helpers -------------------------------------------------
_PRODUCT_CACHE = {}  # {product_id: {"contract_size": Decimal, "base_increment": Decimal}}

def _decimal_from_str(s: str) -> Decimal:
    return Decimal(str(s))

def _fetch_product(product_id: str):
    # Pull product details (includes future_product_details.contract_size)
    path = f"/api/v3/brokerage/products/{product_id}"
    headers = bearer_headers("GET", path)
    r = requests.get(CB_BASE + path, headers=headers, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    # Pull increments & contract size if present
    base_increment = j.get("base_increment") or j.get("baseIncrement")  # API sometimes uses snake/camel in docs
    future = j.get("future_product_details") or j.get("futureProductDetails") or {}
    contract_size = future.get("contract_size") or future.get("contractSize")
    out = {}
    if base_increment:
        try:
            out["base_increment"] = _decimal_from_str(base_increment)
        except Exception:
            pass
    if contract_size:
        try:
            out["contract_size"] = _decimal_from_str(contract_size)
        except Exception:
            pass
    return out

def _get_product_meta(product_id: str):
    meta = _PRODUCT_CACHE.get(product_id)
    if meta:
        return meta
    meta = _fetch_product(product_id)
    _PRODUCT_CACHE[product_id] = meta
    return meta

def _quantize(value: Decimal, increment: Decimal | None) -> str:
    if increment and increment != 0:
        q = value.quantize(increment, rounding=ROUND_DOWN)
    else:
        # default: 8 dp for safety
        q = value.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    # Strip trailing zeros for nicer output
    return format(q.normalize(), "f")

@app.get("/authcheck")
def authcheck():
    try:
        path = "/api/v3/brokerage/accounts"
        headers = bearer_headers("GET", path)
        r = requests.get(CB_BASE + path, headers=headers, timeout=REQUEST_TIMEOUT)
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

@app.get("/productcheck")
def productcheck():
    pid = request.args.get("id", ALLOWED_PRODUCT_ID)
    try:
        meta = _get_product_meta(pid)
        return {"ok": True, "product_id": pid, "meta": {k: str(v) for k, v in meta.items()}}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 400

@app.post("/tv")
def tv():
    # Expected JSON from TradingView:
    # {
    #   "ticker": "ETH/USD:USD-301220",
    #   "action": "buy" | "sell" | "long" | "short",
    #   "order_id": "Long" | "Short" | "TP/SL",
    #   "price": 2500.12,
    #   "contracts": 1,
    #   "timestamp": "..."
    # }
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "invalid_json"}), 400

    product_id = str(data.get("ticker", "")).strip()
    side_in = str(data.get("action", "")).strip().lower()
    if side_in in ("buy", "long"):
        side = "BUY"
    elif side_in in ("sell", "short"):
        side = "SELL"
    else:
        return jsonify({"ok": False, "error": "bad_side_value", "got": side_in}), 400

    base_id = str(data.get("order_id") or "tv").replace(" ", "_")
    client_order_id = f"{base_id}-{uuid.uuid4().hex[:8]}"[:64]
    price_hint = data.get("price")

    # Validate contracts
    contracts_raw = data.get("contracts")
    try:
        contracts_int = int(contracts_raw)
    except Exception:
        return jsonify({"ok": False, "error": "contracts_must_be_int", "got": contracts_raw}), 400
    if contracts_int <= 0:
        return jsonify({"ok": False, "error": "bad_contracts"}), 400

    if product_id != ALLOWED_PRODUCT_ID:
        return jsonify({"ok": False, "error": "product_not_allowed", "got": product_id}), 400

    # --- Convert contracts -> base_size using product metadata ---
    meta = {}
    try:
        meta = _get_product_meta(product_id)
    except Exception as e:
        app.logger.warning("product meta fetch failed: %s", e)

    contract_size = meta.get("contract_size")  # Decimal or None
    base_increment = meta.get("base_increment")  # Decimal or None

    try:
        if contract_size:
            base_size_dec = contract_size * Decimal(contracts_int)
            base_size_str = _quantize(base_size_dec, base_increment)
            assumed = False
        else:
            # Fallback assumption: 1 contract = 0.1 base (common for ETH perps)
            base_size_dec = Decimal("0.1") * Decimal(contracts_int)
            base_size_str = _quantize(base_size_dec, base_increment)
            assumed = True
    except (InvalidOperation, Exception) as e:
        return jsonify({"ok": False, "error": "size_calc_failed", "details": str(e)}), 400

    # MARKET IOC using base_size (per API spec)
    order_cfg = {"market_market_ioc": {"base_size": base_size_str}}
    order = {
        "client_order_id": client_order_id,
        "product_id": product_id,
        "side": side,  # "BUY" / "SELL"
        "order_configuration": order_cfg
    }

    if DRY_RUN:
        return jsonify({
            "ok": True,
            "dry_run": True,
            "would_send": order,
            "meta": {
                "price_hint": price_hint,
                "contracts": contracts_int,
                "contract_size": (str(contract_size) if contract_size else None),
                "base_increment": (str(base_increment) if base_increment else None),
                "assumed_contract_size": assumed
            }
        })

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
        "meta": {
            "price_hint": price_hint,
            "contracts": contracts_int,
            "contract_size": (str(contract_size) if contract_size else None),
            "base_increment": (str(base_increment) if base_increment else None)
        }
    }
    return jsonify(resp), (200 if ok else 400)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
