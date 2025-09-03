import os
import requests
from datetime import datetime, date, time
import pytz
from flask import Flask, request, jsonify
import json

app = Flask(__name__)

# --- Alpaca API Configuration ---
# Ensure these environment variables are set in your deployment environment
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
ALPACA_PAPER = os.environ.get("ALPACA_PAPER", "true").lower() == "true"

# Set base URL based on paper trading flag
BASE_URL = "https://paper-api.alpaca.markets" if ALPACA_PAPER else "https://api.alpaca.markets"

# --- Trading Kill Switch ---
# Set to True to enable live trading, False to disable all order submissions.
ENABLE_TRADING = True

# --- Strategy Configuration ---
STRATEGY_NAME = "BREAKOUT_930"

# --- Market Hours Configuration ---
ENFORCE_MARKET_HOURS = True
AUTO_CLOSE_BEFORE_MINUTES = 5

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY
}

# --- Daily Trading Tracker ---
# Simple file-based tracking to ensure only one trade per day
TRADES_LOG_FILE = "daily_trades.json"

def load_daily_trades():
    """Load the daily trades log from file"""
    try:
        with open(TRADES_LOG_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_daily_trades(trades_log):
    """Save the daily trades log to file"""
    with open(TRADES_LOG_FILE, 'w') as f:
        json.dump(trades_log, f, indent=2)

def has_traded_today(symbol):
    """Check if we've already traded this symbol today"""
    trades_log = load_daily_trades()
    today_str = date.today().isoformat()
    return trades_log.get(today_str, {}).get(symbol, False)

def mark_traded_today(symbol):
    """Mark that we've traded this symbol today"""
    trades_log = load_daily_trades()
    today_str = date.today().isoformat()
    
    if today_str not in trades_log:
        trades_log[today_str] = {}
    
    trades_log[today_str][symbol] = True
    save_daily_trades(trades_log)

def cleanup_old_trades():
    """Clean up trades older than 7 days to keep file size manageable"""
    trades_log = load_daily_trades()
    today = date.today()
    
    dates_to_remove = []
    for date_str in trades_log.keys():
        trade_date = date.fromisoformat(date_str)
        if (today - trade_date).days > 7:
            dates_to_remove.append(date_str)
    
    for date_str in dates_to_remove:
        del trades_log[date_str]
    
    if dates_to_remove:
        save_daily_trades(trades_log)

# --- Helper Functions ---

def get_position_qty(symbol):
    """
    Retrieves the quantity of an open position for a given symbol.
    Returns 0 if no position exists.
    Positive value = long position, Negative value = short position
    """
    try:
        response = requests.get(f"{BASE_URL}/v2/positions/{symbol}", headers=HEADERS)
        response.raise_for_status()
        return float(response.json()["qty"])
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            return 0
        else:
            print(f"Error checking position for {symbol}: {e.response.text}")
            raise

def get_buying_power():
    """
    Retrieves the total account 'equity' and calculates 10% allocation.
    For short selling, also considers available buying power for margin requirements.
    """
    account_response = requests.get(f"{BASE_URL}/v2/account", headers=HEADERS)
    account_response.raise_for_status()
    account_data = account_response.json()
    
    total_equity = float(account_data["equity"])
    available_buying_power = float(account_data["buying_power"])
    
    # Calculate 10% of total equity
    desired_allocation = total_equity * 0.10
    
    # For short selling, we need to ensure adequate buying power (margin requirements)
    # Short selling typically requires 150% of the short value in buying power
    max_short_allocation = available_buying_power * 0.67  # Conservative estimate
    
    print(f"Total Equity: ${total_equity:.2f}")
    print(f"Available Buying Power: ${available_buying_power:.2f}")
    print(f"10% Allocation: ${desired_allocation:.2f}")
    print(f"Max Short Allocation (conservative): ${max_short_allocation:.2f}")
    
    return min(desired_allocation, available_buying_power)

def is_market_open():
    """Checks if the market is currently open."""
    clock_response = requests.get(f"{BASE_URL}/v2/clock", headers=HEADERS)
    clock_response.raise_for_status()
    return clock_response.json()["is_open"]

def get_current_et_time():
    """Get current time in Eastern Time"""
    et_tz = pytz.timezone('US/Eastern')
    return datetime.now(et_tz)

def is_within_trading_hours():
    """
    Check if current time is within allowed trading hours:
    - Pre-market: 4:00 AM - 9:30 AM ET
    - Regular market: 9:30 AM - 4:00 PM ET
    """
    if not ENFORCE_MARKET_HOURS:
        return True
    
    current_et = get_current_et_time()
    current_time = current_et.time()
    current_weekday = current_et.weekday()
    
    # Only trade on weekdays (Monday=0 to Friday=4)
    if current_weekday > 4:
        return False
    
    # Define trading hours
    pre_market_start = time(4, 0)
    regular_start = time(9, 30)
    regular_end = time(16, 0)
    
    # Check if within pre-market or regular hours
    if pre_market_start <= current_time < regular_end:
        return True
    
    return False

def is_near_market_close():
    """Check if we're within AUTO_CLOSE_BEFORE_MINUTES of market close (4:00 PM ET)"""
    if not ENFORCE_MARKET_HOURS:
        return False
    
    current_et = get_current_et_time()
    current_time = current_et.time()
    current_weekday = current_et.weekday()
    
    if current_weekday > 4:
        return False
    
    # Calculate close buffer time
    close_hour = 15 if AUTO_CLOSE_BEFORE_MINUTES >= 60 else 16
    close_minute = (60 - AUTO_CLOSE_BEFORE_MINUTES) if AUTO_CLOSE_BEFORE_MINUTES < 60 else (60 - (AUTO_CLOSE_BEFORE_MINUTES - 60))
    close_buffer_time = time(close_hour, close_minute)
    market_close = time(16, 0)
    
    if close_buffer_time <= current_time < market_close:
        return True
    
    return False

def get_all_positions():
    """Get all open positions"""
    try:
        response = requests.get(f"{BASE_URL}/v2/positions", headers=HEADERS)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as e:
        print(f"Error fetching positions: {e.response.text}")
        return []

def close_all_positions():
    """Close all open positions before market close"""
    positions = get_all_positions()
    
    if not positions:
        print("No open positions to close.")
        return True
    
    print(f"AUTO-CLOSE: Closing {len(positions)} positions before market close")
    
    success_count = 0
    for position in positions:
        try:
            symbol = position['symbol']
            close_url = f"{BASE_URL}/v2/positions/{symbol}"
            response = requests.delete(close_url, headers=HEADERS)
            response.raise_for_status()
            print(f"AUTO-CLOSE: Closed position for {symbol}")
            success_count += 1
        except Exception as e:
            print(f"AUTO-CLOSE ERROR: Failed to close {symbol}: {e}")
    
    return success_count == len(positions)

def close_position(symbol, alert_price_str, market_is_open):
    """
    Closes the entire position for a given symbol.
    Handles both long and short positions automatically.
    """
    current_qty = get_position_qty(symbol)
    if current_qty == 0:
        msg = f"No open position for {symbol} to close."
        print(msg)
        return jsonify({"message": msg}), 200

    # Determine if it's a long or short position
    is_long_position = current_qty > 0
    qty_to_close = abs(current_qty)
    
    print(f"Closing {'LONG' if is_long_position else 'SHORT'} position of {qty_to_close} shares for {symbol}")

    if market_is_open:
        # During market hours, use market order via DELETE endpoint
        close_position_url = f"{BASE_URL}/v2/positions/{symbol}"
        try:
            response = requests.delete(close_position_url, headers=HEADERS)
            response.raise_for_status()
            print(f"Market close order for {symbol} submitted successfully.")
            return jsonify({"message": f"Market close order submitted for {'LONG' if is_long_position else 'SHORT'} position", "data": response.json()}), response.status_code
        except requests.exceptions.HTTPError as e:
            print(f"Error closing position {symbol}: {e.response.text}")
            return jsonify({"error": f"Failed to close position: {e.response.text}"}), e.response.status_code
    else:
        # During extended hours, use limit order
        try:
            alert_price = float(alert_price_str)
            
            # For closing: long position -> sell, short position -> buy
            exit_side = "sell" if is_long_position else "buy"
            limit_price = round(alert_price, 2)

            order_data = {
                "symbol": symbol,
                "qty": int(qty_to_close),
                "side": exit_side,
                "type": "limit",
                "limit_price": str(limit_price),
                "time_in_force": "day",
                "extended_hours": True
            }

            order_response = requests.post(f"{BASE_URL}/v2/orders", json=order_data, headers=HEADERS)
            order_response.raise_for_status()
            print(f"Limit close order ({exit_side}) for {symbol} placed successfully for extended hours.")
            return jsonify({"message": f"Limit close order submitted for {'LONG' if is_long_position else 'SHORT'} position", "data": order_response.json()}), order_response.status_code
        except (ValueError, TypeError):
            return jsonify({"error": f"Invalid price format received for closing order: {alert_price_str}"}), 400
        except requests.exceptions.HTTPError as e:
            print(f"Error placing limit close order: {e.response.text}")
            return jsonify({"error": f"Failed to place limit close order: {e.response.text}"}), e.response.status_code

# --- Main Webhook Endpoint ---

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Receives alerts from TradingView and places orders for breakout strategy.
    
    Expected alert format from TradingView:
    {
        "ticker": "SYMBOL",  # Required: Any valid stock symbol
        "action": "buy",     # "buy" or "sell" 
        "price": "150.25",
        "message": "Long entry above first candle high"  # optional
    }
    
    Action mapping based on Pine Script alert messages:
    - "buy" = Long entry OR Short exit
    - "sell" = Short entry OR Long exit
    """
    try:
        data = request.get_json()
        print(f"Received alert for {STRATEGY_NAME}: {data}")
    except Exception:
        return jsonify({"error": "Invalid JSON payload"}), 400

    # Extract data (ticker is required)
    symbol = data.get("ticker")
    action = data.get("action", "").lower()
    alert_price_str = data.get("price")
    message = data.get("message", "")

    if not all([symbol, action, alert_price_str]):
        return jsonify({"error": "Invalid payload, missing ticker, action, or price"}), 400
    
    symbol = symbol.upper()  # Ensure uppercase for consistency
    
    if action not in ["buy", "sell"]:
        return jsonify({"error": "Action must be 'buy' or 'sell'"}), 400
    
    # Check trading enabled flag
    if not ENABLE_TRADING:
        msg = f"Trading is currently disabled. No order submitted for {symbol}."
        print(msg)
        return jsonify({"message": msg, "trading_disabled": True}), 200
    
    # Cleanup old trades log
    cleanup_old_trades()
    
    # === MARKET HOURS CHECKS ===
    
    # Auto-close check
    if ENFORCE_MARKET_HOURS and is_near_market_close():
        print("AUTO-CLOSE TRIGGERED: Near market close, closing all positions...")
        close_success = close_all_positions()
        msg = f"Auto-close triggered {AUTO_CLOSE_BEFORE_MINUTES} min before market close."
        return jsonify({"message": msg, "auto_close": True}), 200
    
    # Trading hours check
    if ENFORCE_MARKET_HOURS and not is_within_trading_hours():
        current_et = get_current_et_time()
        msg = f"BLOCKED: Trading outside allowed hours. Current time: {current_et.strftime('%I:%M %p ET')}"
        print(msg)
        return jsonify({"message": msg, "blocked_time": current_et.isoformat()}), 200
    
    # Check market status
    market_is_open = is_market_open()
    
    # Get current position
    current_qty = get_position_qty(symbol)
    is_long_position = current_qty > 0
    is_short_position = current_qty < 0
    has_position = current_qty != 0
    
    print(f"Current position for {symbol}: {current_qty} shares")
    
    try:
        alert_price = float(alert_price_str)
        if alert_price <= 0:
            return jsonify({"error": "Invalid price received from alert."}), 400
    except (ValueError, TypeError):
        return jsonify({"error": f"Invalid price format: {alert_price_str}"}), 400
    
    # === STRATEGY LOGIC ===
    # Based on Pine Script:
    # - "buy" alert_message for Long entries and Short exits
    # - "sell" alert_message for Short entries and Long exits
    
    if action == "buy":
        if is_short_position:
            # SHORT EXIT: Close short position
            print(f"SHORT EXIT signal received for {symbol}")
            return close_position(symbol, alert_price_str, market_is_open)
        elif not has_position:
            # LONG ENTRY: Enter long position
            print(f"LONG ENTRY signal received for {symbol}")
            
            # Check daily trade limit
            if has_traded_today(symbol):
                msg = f"Already traded {symbol} today. Only one trade per day allowed."
                print(msg)
                return jsonify({"message": msg, "daily_limit_reached": True}), 200
            
            # Calculate position size (10% of equity)
            buying_power = get_buying_power()
            trade_allocation = buying_power
            qty = int(trade_allocation // alert_price)
            
            if qty < 1:
                msg = f"Not enough funds for one share at ${alert_price:.2f} with 10% equity allocation."
                print(msg)
                return jsonify({"message": msg}), 200
            
            # Prepare order
            order_data = {
                "symbol": symbol,
                "qty": qty,
                "side": "buy",
                "time_in_force": "day",
            }
            
            if market_is_open:
                print("Market open: Placing MARKET order for LONG entry")
                order_data["type"] = "market"
            else:
                print("Market closed: Placing LIMIT order for LONG entry")
                order_data["type"] = "limit"
                order_data["limit_price"] = str(round(alert_price, 2))
                order_data["extended_hours"] = True
            
            # Submit order
            try:
                order_response = requests.post(f"{BASE_URL}/v2/orders", json=order_data, headers=HEADERS)
                order_response.raise_for_status()
                
                # Mark as traded today
                mark_traded_today(symbol)
                
                print(f"LONG entry order for {qty} shares of {symbol} placed successfully.")
                return jsonify({
                    "message": f"LONG entry order placed for {qty} shares", 
                    "data": order_response.json(),
                    "daily_trade_logged": True
                }), order_response.status_code
            except requests.exceptions.HTTPError as e:
                print(f"Error placing LONG entry order: {e.response.text}")
                return jsonify({"error": f"Failed to place LONG entry order: {e.response.text}"}), e.response.status_code
        else:
            # Already have long position
            msg = f"Already have LONG position for {symbol}, ignoring buy signal."
            print(msg)
            return jsonify({"message": msg}), 200
    
    elif action == "sell":
        if is_long_position:
            # LONG EXIT: Close long position
            print(f"LONG EXIT signal received for {symbol}")
            return close_position(symbol, alert_price_str, market_is_open)
        elif not has_position:
            # SHORT ENTRY: Enter short position
            print(f"SHORT ENTRY signal received for {symbol}")
            
            # Check daily trade limit
            if has_traded_today(symbol):
                msg = f"Already traded {symbol} today. Only one trade per day allowed."
                print(msg)
                return jsonify({"message": msg, "daily_limit_reached": True}), 200
            
            # Calculate position size for short (10% of equity)
            buying_power = get_buying_power()
            trade_allocation = buying_power
            qty = int(trade_allocation // alert_price)
            
            if qty < 1:
                msg = f"Not enough funds for short position at ${alert_price:.2f} with 10% equity allocation."
                print(msg)
                return jsonify({"message": msg}), 200
            
            # Prepare short order
            order_data = {
                "symbol": symbol,
                "qty": qty,
                "side": "sell",
                "time_in_force": "day",
            }
            
            if market_is_open:
                print("Market open: Placing MARKET order for SHORT entry")
                order_data["type"] = "market"
            else:
                print("Market closed: Placing LIMIT order for SHORT entry")
                order_data["type"] = "limit"
                order_data["limit_price"] = str(round(alert_price, 2))
                order_data["extended_hours"] = True
            
            # Submit short order
            try:
                order_response = requests.post(f"{BASE_URL}/v2/orders", json=order_data, headers=HEADERS)
                order_response.raise_for_status()
                
                # Mark as traded today
                mark_traded_today(symbol)
                
                print(f"SHORT entry order for {qty} shares of {symbol} placed successfully.")
                return jsonify({
                    "message": f"SHORT entry order placed for {qty} shares", 
                    "data": order_response.json(),
                    "daily_trade_logged": True
                }), order_response.status_code
            except requests.exceptions.HTTPError as e:
                print(f"Error placing SHORT entry order: {e.response.text}")
                return jsonify({"error": f"Failed to place SHORT entry order: {e.response.text}"}), e.response.status_code
        else:
            # Already have short position
            msg = f"Already have SHORT position for {symbol}, ignoring sell signal."
            print(msg)
            return jsonify({"message": msg}), 200

# --- Status and Info Endpoints ---

@app.route("/status", methods=["GET"])
def status():
    """Returns current status and configuration"""
    current_et = get_current_et_time()
    within_hours = is_within_trading_hours()
    near_close = is_near_market_close()
    market_open = is_market_open()
    
    positions = get_all_positions()
    
    # Check today's trades
    trades_log = load_daily_trades()
    today_str = date.today().isoformat()
    today_trades = trades_log.get(today_str, {})
    
    status_info = {
        "strategy": STRATEGY_NAME,
        "current_time": {
            "eastern": current_et.strftime("%Y-%m-%d %I:%M:%S %p ET"),
            "iso": current_et.isoformat(),
            "weekday": current_et.strftime("%A")
        },
        "market_status": {
            "alpaca_market_open": market_open,
            "within_trading_hours": within_hours,
            "near_market_close": near_close,
            "trading_allowed": within_hours and not near_close
        },
        "configuration": {
            "enforce_market_hours": ENFORCE_MARKET_HOURS,
            "auto_close_before_minutes": AUTO_CLOSE_BEFORE_MINUTES,
            "enable_trading": ENABLE_TRADING,
            "paper_trading": ALPACA_PAPER
        },
        "positions": {
            "count": len(positions),
            "details": [{
                "symbol": pos["symbol"],
                "qty": float(pos["qty"]),
                "side": "LONG" if float(pos["qty"]) > 0 else "SHORT",
                "market_value": float(pos["market_value"]),
                "avg_entry_price": float(pos["avg_entry_price"])
            } for pos in positions]
        },
        "daily_trades": {
            "today": today_str,
            "symbols_traded": list(today_trades.keys()),
            "trade_limit_per_day": 1
        }
    }
    
    # Add Pacific Time for convenience
    pt_tz = pytz.timezone('US/Pacific')
    current_pt = current_et.astimezone(pt_tz)
    status_info["current_time"]["pacific"] = current_pt.strftime("%Y-%m-%d %I:%M:%S %p PT")
    
    return jsonify(status_info), 200

@app.route("/trades", methods=["GET"])
def trades():
    """Returns recent daily trades log"""
    trades_log = load_daily_trades()
    return jsonify({"trades_log": trades_log}), 200

@app.route("/clear_daily_trades", methods=["POST"])
def clear_daily_trades():
    """Clear today's trades log (for testing or manual override)"""
    trades_log = load_daily_trades()
    today_str = date.today().isoformat()
    
    if today_str in trades_log:
        del trades_log[today_str]
        save_daily_trades(trades_log)
        return jsonify({"message": f"Cleared trades for {today_str}"}), 200
    else:
        return jsonify({"message": f"No trades found for {today_str}"}), 200

@app.route("/", methods=["GET"])
def root():
    """Root endpoint"""
    return jsonify({
        "status": "ok",
        "strategy": STRATEGY_NAME,
        "message": "Breakout Trading Bot is running",
        "endpoints": {
            "webhook": "/webhook (POST)",
            "status": "/status (GET)",
            "trades": "/trades (GET)",
            "clear_daily_trades": "/clear_daily_trades (POST)"
        }
    }), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=False)
