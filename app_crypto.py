import os, json, uuid, traceback
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from flask import Flask, request, jsonify
import requests
from coinbase import jwt_generator  # from coinbase-advanced-py

CB_BASE = os.getenv("CB_BASE", "https://api.coinbase.com")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "10"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# Your CCXT-style symbol from the other bot; we accept it and map to Coinbase product_id:
TV_ETH_PERP_SYMBOL = os.getenv("TV_ETH_PERP_SYMBOL", "ETH/USD:USD-301220")

# Optional hard override once you learn the exact product id (e.g., ETH-PERP-INTX or ETH-PERP)
CB_PRODUCT_ID_OVERRIDE = os.getenv("CB_PRODUCT_ID_OVERRIDE", "").strip() or None

app = Flask(__name__)

# ---------------------- auth helpers ----------------------
def _normalize_pem(s: str) -> str:
    if not s:
        return ""
    s = s.strip().strip('"').strip("'")
    if "\\n" in s and "\n" not in s:
        s = s.replace("\\n", "\n")
    return s

def _env():
    return {
        "CB_API_KEY": os.getenv("CB_API_KEY"),            # organizations/{org_id}/apiKeys/{key_id}
        "CB_API_SECRET": _normalize_pem(os.getenv("CB_API_SECRET") or "")  # PEM with real newlines ok
    }

def _bearer_headers(method: str, path: str):
    env = _env()
    if not env["CB_API_KEY"] or not env["CB_API_SECRET"]:
        raise RuntimeError("Missing CB_API_KEY or CB_API_SECRET")
    jwt_uri = jwt_generator.format_jwt_uri(method, path)
    token = jwt_generator.build_rest_jwt(jwt_uri, env["CB_API_KEY"], env["CB_API_SECRET"])
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# ---------------------- utils ----------------------
def _D(x) -> Decimal:
    return Decimal(str(x))

def _quantize(value: Decimal, increment: Decimal | None) -> str:
    if increment and increment != 0:
        q = value.quantize(increment, rounding=ROUND_DOWN)
    else:
        q = value.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    return format(q.normalize(), "f")

_PRODUCT_CACHE: dict[str, dict] = {}
_RESOLVED_ID_CACHE: dict[str, str] = {}

def _get_product(product_id: str) -> dict:
    if product_id in _PRODUCT_CACHE:
        return _PRODUCT_CACHE[product_id]
    path = f"/api/v3/brokerage/products/{product_id}"
    r = requests.get(CB_BASE + path, headers=_bearer_headers("GET", path), timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    _PRODUCT_CACHE[product_id] = j
    return j

def _scan_eth_perps() -> list[dict]:
    path = "/api/v3/brokerage/products"
    params = {
        "product_type": "FUTURE",
        "contract_expiry_type": "PERPETUAL",
        "get_all_products": "true"
    }
    r = requests.get(CB_BASE + path, headers=_bearer_headers("GET", path), params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    items = r.json().get("products", []) or []
    out = []
    for p in items:
        pid = p.get("product_id", "")
        disp = (p.get("display_name") or "")
        base = p.get("base_display_symbol") or p.get("base_display_symbol".replace("_","")) or ""
        # keep anything that looks like an ETH perp
        if "ETH" in (base or pid or disp) and "PERP" in (pid.upper() + disp.upper()):
            out.append(p)
    return out

def _resolve_cb_product_id(tv_symbol: str) -> tuple[str | None, dict]:
    """Map CCXT-like 'ETH/USD:USD-...' -> Coinbase product_id."""
    meta = {"override": CB_PRODUCT_ID_OVERRIDE, "tried": [], "picked": None, "scanned": []}

    # 0) Respect explicit override if provided
    if CB_PRODUCT_ID_OVERRIDE:
        try:
            _get_product(CB_PRODUCT_ID_OVERRIDE)
            meta["picked"] = CB_PRODUCT_ID_OVERRIDE
            return CB_PRODUCT_ID_OVERRIDE, meta
        except Exception as e:
            meta["tried"].append({"candidate": CB_PRODUCT_ID_OVERRIDE, "ok": False, "err": str(e)})

    # quick cache
    if tv_symbol in _RESOLVED_ID_CACHE:
        return _RESOLVED_ID_CACHE[tv_symbol], {"picked": _RESOLVED_ID_CACHE[tv_symbol], "cache": True}

    # 1) Some tv/ccxt strings may already match (rare)
    try:
        _get_product(tv_symbol)
        meta["picked"] = tv_symbol
        _RESOLVED_ID_CACHE[tv_symbol] = tv_symbol
        return tv_symbol, meta
    except Exception:
        meta["tried"].append({"candidate": tv_symbol, "ok": False})

    # 2) Common ETH perp ids to try directly
    for cand in ["ETH-PERP-INTX", "ETH-PERP"]:
        try:
            _get_product(cand)
            meta["tried"].append({"candidate": cand, "ok": True})
            meta["picked"] = cand
            _RESOLVED_ID_CACHE[tv_symbol] = cand
            return cand, meta
        except Exception as e:
            meta["tried"].append({"candidate": cand, "ok": False, "err": str(e)})

    # 3) Scan the catalog
    try:
        items = _scan_eth_perps()
        meta["scanned"] = [i.get("product_id") for i in items]
        # prefer tradable
        for i in items:
            if not i.get("trading_disabled") and not i.get("is_disabled"):
                pid = i.get("product_id")
                if pid:
                    meta["picked"] = pid
                    _RESOLVED_ID_CACHE[tv_symbol] = pid
                    return pid, meta
        # else pick first
        if items:
            pid = items[0].get("product_id")
            meta["picked"] = pid
            _RESOLVED_ID_CACHE[tv_symbol] = pid
            return pid, meta
    except Exception as e:
        meta["scan_error"] = str(e)

    return None, meta

# ---------------------- routes ----------------------
@app.get("/health")
def health():
    return {"ok": True}

@app.get("/authcheck")
def authcheck():
    try:
        path = "/api/v3/brokerage/accounts"
        r = requests.get(CB_BASE + path, headers=_bearer_headers("GET", path), timeout=REQUEST_TIMEOUT)
        try:
            j = r.json()
        except ValueError:
            j = None
        return jsonify({"ok": r.status_code == 200, "status": r.status_code, "json": j, "text": (None if j is not None else r.text)}), (200 if r.status_code == 200 else 400)
    except Exception as e:
        app.logger.error("authcheck error: %s\n%s", e, traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/productscan")
def productscan():
    try:
        items = _scan_eth_perps()
        slim = []
        for p in items:
            f = p.get("future_product_details") or {}
            slim.append({
                "product_id": p.get("product_id"),
                "display_name": p.get("display_name"),
                "trading_disabled": p.get("trading_disabled"),
                "venue": (f.get("venue") if isinstance(f, dict) else None),
                "contract_size": (f.get("contract_size") if isinstance(f, dict) else None),
                "expiry_type": (f.get("contract_expiry_type") if isinstance(f, dict) else None),
            })
        return {"ok": True, "count": len(slim), "products": slim}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 400

@app.post("/tv")
def tv():
    # Example payload from your Pine alerts / curl:
    # {"ticker":"ETH/USD:USD-301220","action":"buy","order_id":"Long","price":2500,"contracts":1,"timestamp":"..."}
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "invalid_json"}), 400

    tv_symbol = str(data.get("ticker", "")).strip()
    side_in = str(data.get("action", "")).strip().lower()
    base_id = str(data.get("order_id") or "tv").replace(" ", "_")
    client_order_id = f"{base_id}-{uuid.uuid4().hex[:8]}"[:64]
    price_hint = data.get("price")

    if side_in in ("buy", "long"):
        side = "BUY"
    elif side_in in ("sell", "short"):
        side = "SELL"
    else:
        return jsonify({"ok": False, "error": "bad_side_value", "got": side_in}), 400

    try:
        contracts = int(data.get("contracts"))
        if contracts <= 0:
            raise ValueError
    except Exception:
        return jsonify({"ok": False, "error": "contracts_must_be_positive_int"}), 400

    # Map CCXT-like symbol -> Coinbase product_id
    product_id, mapping_meta = _resolve_cb_product_id(tv_symbol)
    if not product_id:
        return jsonify({"ok": False, "error": "invalid_product_id", "mapping_meta": mapping_meta}), 400

    # Fetch product to get contract_size and base_increment
    try:
        prod = _get_product(product_id)
    except Exception as e:
        return jsonify({"ok": False, "error": "get_product_failed", "details": str(e), "product_id": product_id}), 400

    base_increment = prod.get("base_increment") or prod.get("baseIncrement")
    fpd = prod.get("future_product_details") or {}
    contract_size = fpd.get("contract_size")

    # Convert contracts -> base_size
    try:
        if contract_size:
            base_size_dec = _D(contract_size) * _D(contracts)
            assumed = False
        else:
            base_size_dec = _D("0.1") * _D(contracts)  # safe default for ETH perp
            assumed = True
        base_size_str = _quantize(base_size_dec, _D(base_increment) if base_increment else None)
    except (InvalidOperation, Exception) as e:
        return jsonify({"ok": False, "error": "size_calc_failed", "details": str(e)}), 400

    order = {
        "client_order_id": client_order_id,
        "product_id": product_id,
        "side": side,  # "BUY" / "SELL"
        "order_configuration": {
            "market_market_ioc": {
                "base_size": base_size_str
            }
        }
    }

    if DRY_RUN:
        return jsonify({
            "ok": True,
            "dry_run": True,
            "would_send": order,
            "mapping_meta": mapping_meta,
            "meta": {
                "tv_symbol": tv_symbol,
                "contracts": contracts,
                "contract_size": contract_size,
                "assumed_contract_size": assumed,
                "base_increment": base_increment,
                "price_hint": price_hint
            }
        })

    path = "/api/v3/brokerage/orders"
    try:
        r = requests.post(CB_BASE + path, headers=_bearer_headers("POST", path),
                          data=json.dumps(order, separators=(",", ":")), timeout=REQUEST_TIMEOUT)
    except Exception as e:
        app.logger.error("order error: %s\n%s", e, traceback.format_exc())
        return jsonify({"ok": False, "error": "exception", "details": str(e)}), 500

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
        "mapping_meta": mapping_meta,
        "meta": {
            "tv_symbol": tv_symbol,
            "contracts": contracts,
            "contract_size": contract_size,
            "base_increment": base_increment
        }
    }), (200 if ok else 400)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
