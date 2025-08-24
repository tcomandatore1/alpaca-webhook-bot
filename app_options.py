import os, asyncio, httpx
from datetime import datetime, timezone, date
from typing import Optional, Literal, Dict, List, Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, StopOrderRequest
from alpaca.trading.stream import TradingStream

# =========================
# Environment / Clients
# =========================
# NOTE: keep the env var names you’re already using on Render
ALPACA_API_KEY    = os.environ["ALPACA_API_KEY"]
ALPACA_API_SECRET = os.environ["ALPACA_SECRET_KEY"]
ALPACA_PAPER      = os.getenv("ALPACA_PAPER", "true").lower() == "true"

# Market data bases
DATA_STOCKS_BASE = "https://data.alpaca.markets/v2"   # stocks quotes/trades/bars
ALPACA_FEED = os.getenv("ALPACA_FEED", "iex")         # "iex" (default) or "sip" if you have it

# Trading client + trade updates stream
trading = TradingClient(ALPACA_API_KEY, ALPACA_API_SECRET, paper=ALPACA_PAPER)
stream  = TradingStream(ALPACA_API_KEY, ALPACA_API_SECRET, paper=ALPACA_PAPER)

# REST base for quotes & contract lookup (paper)
ALPACA_REST_BASE = "https://paper-api.alpaca.markets"

# Shared auth headers for REST calls
HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_API_SECRET
}

# Auto-flatten config (LOCAL time is America/Los_Angeles)
LOCAL_TZ  = ZoneInfo("America/Los_Angeles")
EOD_HHMM  = os.getenv("EOD_FLATTEN_HHMM", "13:00")  # 13:00 PT by default
EOD_ON    = os.getenv("EOD_ENABLED", "true").lower() == "true"

# Track OCO children: parent entry id -> {tp_id, sl_id}
OCO_BOOK: Dict[str, Dict[str, Optional[str]]] = {}
STREAM_RUNNING = False
EOD_THREAD_STARTED = False
_eod_last_run_date = None

app = FastAPI(title="VWAP Options Trader", version="1.4")

# =========================
# Models
# =========================
class TradeRequest(BaseModel):
    underlying: str = Field(..., examples=["SPY", "QQQ"])
    side: Literal["long_call", "long_put"] = "long_call"   # long_call=buy call; long_put=buy put
    expiry: Optional[str] = None                           # YYYY-MM-DD; if None -> nearest >= today
    strike: Optional[float] = None                         # if None -> ATM (closest to spot)
    # target_delta retained for compatibility; ignored by ATM selector
    target_delta: float = Field(0.50, ge=0.05, le=0.95)
    contracts: int = Field(1, ge=1)
    order_type: Literal["market", "limit"] = "market"
    limit_price: Optional[float] = None
    tp_pct: float = Field(0.35, gt=0)                      # +35% default
    sl_pct: float = Field(0.50, gt=0)                      # -50% default
    client_tag: Optional[str] = None

class SimpleTrade(BaseModel):
    # Minimal payload for TradingView alerts that only send the underlying + signal
    underlying: str
    signal: Literal["long", "short"]                       # long -> buy call, short -> buy put
    contracts: int = 1
    # target_delta retained for backwards-compat; ignored by ATM logic
    target_delta: float = 0.50
    tp_pct: float = 0.35
    sl_pct: float = 0.50
    order_type: Literal["market", "limit"] = "market"
    limit_price: Optional[float] = None

# =========================
# Helpers (ATM selection + OCO exits)
# =========================
async def get_underlying_price(underlying: str) -> float:
    """
    Fetch latest underlying price from Alpaca market data.
    Prefer latest quote ask; fall back to latest trade or bar if needed.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        # Try latest QUOTE first
        try:
            q_url = f"{DATA_STOCKS_BASE}/stocks/{underlying.upper()}/quotes/latest"
            qr = await client.get(q_url, headers=HEADERS, params={"feed": ALPACA_FEED})
            if qr.status_code == 200:
                q = qr.json().get("quote") or {}
                ap = q.get("ap") or q.get("bp")
                if ap is not None:
                    return float(ap)
        except Exception:
            pass

        # Fall back to latest TRADE
        try:
            t_url = f"{DATA_STOCKS_BASE}/stocks/{underlying.upper()}/trades/latest"
            tr = await client.get(t_url, headers=HEADERS, params={"feed": ALPACA_FEED})
            if tr.status_code == 200:
                px = (tr.json().get("trade") or {}).get("p")
                if px is not None:
                    return float(px)
        except Exception:
            pass

        # Fall back to latest BAR (close)
        try:
            b_url = f"{DATA_STOCKS_BASE}/stocks/{underlying.upper()}/bars/latest"
            br = await client.get(b_url, headers=HEADERS, params={"feed": ALPACA_FEED})
            if br.status_code == 200:
                c = (br.json().get("bar") or {}).get("c")
                if c is not None:
                    return float(c)
        except Exception:
            pass

    raise HTTPException(status_code=502, detail="Unable to fetch underlying price from market data.")


async def choose_contract_symbol(underlying: str, expiry: Optional[str], is_call: bool,
                                 strike: Optional[float], _target_delta_unused: float) -> str:
    """
    ATM selection:
      1) Pull contracts via /v2/options/contracts?underlying_symbols=<UND>
      2) Choose nearest expiration date >= today (or the provided expiry)
      3) Filter by type (call/put)
      4) If 'strike' provided, pick exact strike; otherwise pick ATM (closest strike to current ask)
    """
    # 1) Pull contracts
    url = f"{ALPACA_REST_BASE}/v2/options/contracts"
    params = {
        "underlying_symbols": underlying.upper(),
        "limit": 1000
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, headers=HEADERS, params=params)
        r.raise_for_status()
        data = r.json()

    contracts = data.get("option_contracts", [])
    if not contracts:
        raise HTTPException(status_code=404, detail="No option contracts returned for underlying.")

    # 2) Choose expiration
    today = date.today()
    # keep only expiries >= today
    contracts = [c for c in contracts if date.fromisoformat(c["expiration_date"]) >= today]

    if expiry:
        expiry_contracts = [c for c in contracts if c["expiration_date"] == expiry]
        if not expiry_contracts:
            raise HTTPException(status_code=404, detail=f"No contracts for requested expiry {expiry}.")
    else:
        contracts.sort(key=lambda c: c["expiration_date"])
        nearest_exp = contracts[0]["expiration_date"]
        expiry_contracts = [c for c in contracts if c["expiration_date"] == nearest_exp]

    # 3) Filter by type
    typ = "call" if is_call else "put"
    expiry_contracts = [c for c in expiry_contracts if (c.get("type") == typ)]
    if not expiry_contracts:
        raise HTTPException(status_code=404, detail=f"No {typ} contracts at chosen expiry.")

    # 4) Strike selection
    if strike is not None:
        for c in expiry_contracts:
            if abs(float(c["strike_price"]) - float(strike)) < 1e-6:
                return c["symbol"]
        raise HTTPException(status_code=404, detail=f"Requested strike {strike} not found at chosen expiry.")

    # ATM = strike closest to spot
    spot = await get_underlying_price(underlying)
    expiry_contracts.sort(key=lambda c: abs(float(c["strike_price"]) - spot))
    return expiry_contracts[0]["symbol"]

async def wait_for_fill(order_id: str, poll_sec: float = 0.5, timeout_sec: float = 90.0) -> float:
    deadline = datetime.now(timezone.utc).timestamp() + timeout_sec
    while datetime.now(timezone.utc).timestamp() < deadline:
        o = trading.get_order_by_id(order_id)
        if o.filled_qty and float(o.filled_qty) >= float(o.qty):
            return float(o.filled_avg_price or o.limit_price or o.stop_price)
        if o.status in ("canceled", "rejected", "expired"):
            raise HTTPException(status_code=409, detail=f"Entry order {o.status}")
        await asyncio.sleep(poll_sec)
    raise HTTPException(status_code=504, detail="Entry fill timeout")

async def place_oco_children(symbol: str, qty: int, entry_avg: float,
                             tp_pct: float, sl_pct: float, parent_id: str):
    """
    Simulate OCO exits on the option premium:
      - TP = entry * (1 + tp_pct)
      - SL = entry * (1 - sl_pct)
    Cancel the sibling when one leg fills (handled by trade update stream).
    """
    tp_px = round(entry_avg * (1 + tp_pct), 2)
    sl_px = round(entry_avg * (1 - sl_pct), 2)

    tp_req = LimitOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL,
                               time_in_force=TimeInForce.DAY, limit_price=tp_px)
    tp = trading.submit_order(tp_req)

    sl_req = StopOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL,
                              time_in_force=TimeInForce.DAY, stop_price=sl_px)
    sl = trading.submit_order(sl_req)

    OCO_BOOK[parent_id] = {"tp_id": tp.id, "sl_id": sl.id}

# =========================
# Streaming: cancel sibling on fill
# =========================
@stream.subscribe_trade_updates
async def on_trade_update(u):
    try:
        event = u.event
        oid   = u.order.id
        for parent, kids in list(OCO_BOOK.items()):
            for leg in ("tp_id", "sl_id"):
                if kids.get(leg) == oid:
                    if event == "fill":
                        sib = kids["sl_id"] if leg == "tp_id" else kids["tp_id"]
                        if sib:
                            try: trading.cancel_order_by_id(sib)
                            except Exception: pass
                        OCO_BOOK.pop(parent, None)
                    elif event in ("canceled", "rejected", "expired", "done_for_day"):
                        OCO_BOOK.pop(parent, None)
    except Exception:
        # keep stream alive
        pass

def ensure_stream():
    global STREAM_RUNNING
    if not STREAM_RUNNING:
        import threading
        t = threading.Thread(target=stream.run, daemon=True)
        t.start()
        STREAM_RUNNING = True

# =========================
# EOD auto-flatten @ LOCAL H:M (PT)
# =========================
async def flatten_all_options():
    try:
        trading.cancel_orders()
    except Exception:
        pass
    try:
        positions = trading.get_all_positions()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch positions: {e}")

    for p in positions:
        try:
            if str(p.asset_class).lower().startswith("option"):
                qty = abs(int(float(p.qty)))
                if qty == 0:
                    continue
                side = OrderSide.SELL if float(p.qty) > 0 else OrderSide.BUY
                trading.submit_order(MarketOrderRequest(
                    symbol=p.symbol, qty=qty, side=side, time_in_force=TimeInForce.DAY
                ))
        except Exception:
            pass

async def eod_loop():
    global _eod_last_run_date
    if not EOD_ON:
        return
    while True:
        try:
            now = datetime.now(LOCAL_TZ)
            if now.strftime("%H:%M") >= EOD_HHMM and _eod_last_run_date != now.date().isoformat():
                await flatten_all_options()
                _eod_last_run_date = now.date().isoformat()
        except Exception:
            pass
        await asyncio.sleep(20)

def ensure_eod_thread():
    global EOD_THREAD_STARTED
    if EOD_ON and not EOD_THREAD_STARTED:
        import threading
        def _runner():
            asyncio.run(eod_loop())
        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        EOD_THREAD_STARTED = True

# =========================
# API
# =========================
@app.on_event("startup")
def _start():
    ensure_stream()
    ensure_eod_thread()

@app.get("/")
def root():
    return {"ok": True, "msg": "VWAP Options Trader up", "see": "/health"}

@app.get("/health")
def health():
    return {"ok": True, "paper": ALPACA_PAPER, "oco_tracked": len(OCO_BOOK),
            "eod_time": EOD_HHMM, "tz": str(LOCAL_TZ)}

@app.get("/positions")
def positions():
    pos = []
    for p in trading.get_all_positions():
        pos.append({
            "symbol": p.symbol,
            "asset_class": str(p.asset_class),
            "qty": p.qty,
            "avg_entry": p.avg_entry_price
        })
    return {"ok": True, "positions": pos}

@app.get("/orders")
def orders():
    open_orders = trading.get_orders(status="open")
    return {"ok": True, "count": len(open_orders),
            "orders": [{"id": o.id, "symbol": o.symbol, "side": str(o.side),
                        "type": str(o.type), "status": o.status} for o in open_orders]}

@app.post("/eod_flatten")
async def eod_flatten():
    await flatten_all_options()
    return {"ok": True, "note": "Canceled open orders and submitted market closes for all option positions."}

@app.post("/force_close")
def force_close(payload: dict):
    """
    Idempotent close on alert:
      - pass {"underlying":"SPY"}  -> close ALL SPY option positions
      - or   {"symbol":"<OCC>"}    -> close that specific option
    """
    symbol: Optional[str] = payload.get("symbol")
    underlying: Optional[str] = payload.get("underlying")

    # Cancel open orders for that target first
    try:
        for o in trading.get_orders(status="open"):
            if (symbol and o.symbol == symbol) or (underlying and underlying.upper() in o.symbol):
                try: trading.cancel_order_by_id(o.id)
                except Exception: pass
    except Exception:
        pass

    flattened: List[Dict[str, Any]] = []
    try:
        for p in trading.get_all_positions():
            if not str(p.asset_class).lower().startswith("option"):
                continue
            if symbol and p.symbol != symbol:
                continue
            if underlying and (underlying.upper() not in p.symbol):
                continue
            qty = abs(int(float(p.qty)))
            if qty == 0:
                flattened.append({"symbol": p.symbol, "status": "already_flat"})
                continue
            side = OrderSide.SELL if float(p.qty) > 0 else OrderSide.BUY
            ord = trading.submit_order(MarketOrderRequest(
                symbol=p.symbol, qty=qty, side=side, time_in_force=TimeInForce.DAY
            ))
            flattened.append({"symbol": p.symbol, "status": "submitted", "order_id": ord.id})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not flattened:
        return {"ok": True, "note": "No matching option positions found (already flat)."}
    return {"ok": True, "results": flattened}

# ----- Full-featured trade endpoint (side required; now uses ATM selection) -----
@app.post("/trade")
async def trade(req: TradeRequest):
    is_call = req.side == "long_call"
    symbol  = await choose_contract_symbol(
        underlying=req.underlying, expiry=req.expiry, is_call=is_call,
        strike=req.strike, _target_delta_unused=req.target_delta
    )

    # Entry
    if req.order_type == "limit":
        if req.limit_price is None:
            raise HTTPException(status_code=422, detail="limit_price required for limit orders")
        entry_req = LimitOrderRequest(symbol=symbol, qty=req.contracts, side=OrderSide.BUY,
                                      time_in_force=TimeInForce.DAY, limit_price=req.limit_price)
    else:
        entry_req = MarketOrderRequest(symbol=symbol, qty=req.contracts, side=OrderSide.BUY,
                                       time_in_force=TimeInForce.DAY)

    entry = trading.submit_order(entry_req)
    avg   = await wait_for_fill(entry.id)

    # OCO exits on the option premium
    await place_oco_children(symbol, req.contracts, avg, req.tp_pct, req.sl_pct, entry.id)

    return {"ok": True, "contract": symbol, "entry_id": entry.id,
            "avg_entry_price": avg, "oco_children": OCO_BOOK.get(entry.id)}

# ----- Simple endpoint for TradingView alerts that only send underlying + signal -----
@app.post("/trade_simple")
async def trade_simple(req: SimpleTrade):
    sig = req.signal.lower().strip()
    side = "long_call" if sig == "long" else "long_put"
    full = TradeRequest(
        underlying=req.underlying, side=side, expiry=None, strike=None,
        target_delta=req.target_delta, contracts=req.contracts,
        order_type=req.order_type, limit_price=req.limit_price,
        tp_pct=req.tp_pct, sl_pct=req.sl_pct
    )
    return await trade(full)

@app.post("/dry_run_pick")
async def dry_run_pick(req: SimpleTrade):
    sig = req.signal.lower().strip()
    is_call = sig == "long"
    sym = await choose_contract_symbol(
        underlying=req.underlying, expiry=None, is_call=is_call, strike=None, _target_delta_unused=0.5
    )
    return {"ok": True, "chosen_contract": sym}
