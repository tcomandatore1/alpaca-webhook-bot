from flask import Flask, request, jsonify
import requests
import os
from datetime import datetime, time
import pytz

app = Flask(__name__)

# --- Alpaca API Configuration ---
# Ensure these environment variables are set in your deployment environment
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
BASE_URL = "https://api.alpaca.markets" # Changed to LIVE trading endpoint

# --- Trading Kill Switch ---
# Set to True to enable live trading, False to disable all order submissions.
ENABLE_TRADING = True # Set to True for trading, False for no trades to go through

# --- Strategy Configuration ---
# Set the strategy type (e.g., "long" or "short")
STRATEGY_TYPE = os.environ.get("STRATEGY_TYPE", "long").lower()

# --- Market Hours Configuration ---
# Enable market hours restrictions (True to enforce market hours, False to allow all hours)
ENFORCE_MARKET_HOURS = True
# Minutes before market close to automatically close all positions (default: 5 minutes)
AUTO_CLOSE_BEFORE_MINUTES = 5

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY
}

# --- Helper Functions ---

def get_position_qty(symbol):
    """
    Retrieves the quantity of an open position for a given symbol.
    Returns 0 if no position exists.
    """
    try:
        response = requests.get(f"{BASE_URL}/v2/positions/{symbol}", headers=HEADERS)
        response.raise_for_status()
        return int(response.json()["qty"])
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            return 0
        else:
            print(f"Error checking position for {symbol}: {e.response.text}")
            raise

def get_buying_power():
    """
    Retrieves the 'regt_buying_power' from the account, which is the reliable
    figure for calculating trade size.
    """
    account_response = requests.get(f"{BASE_URL}/v2/account", headers=HEADERS)
    account_response.raise_for_status()
    return float(account_response.json()["regt_buying_power"])

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
    Returns True if within trading hours, False otherwise.
    """
    if not ENFORCE_MARKET_HOURS:
        return True
    
    current_et = get_current_et_time()
    current_time = current_et.time()
    current_weekday = current_et.weekday()  # 0=Monday, 6=Sunday
    
    # Only trade on weekdays (Monday=0 to Friday=4)
    if current_weekday > 4:  # Weekend
        return False
    
    # Define trading hours
    pre_market_start = time(4, 0)    # 4:00 AM ET
    regular_start = time(9, 30)      # 9:30 AM ET  
    regular_end = time(16, 0)        # 4:00 PM ET
    
    # Check if within pre-market (4:00 AM - 9:30 AM ET)
    if pre_market_start <= current_time < regular_start:
        return True
    
    # Check if within regular market hours (9:30 AM - 4:00 PM ET)
    if regular_start <= current_time < regular_end:
        return True
    
    return False

def is_near_market_close():
    """
    Check if we're within AUTO_CLOSE_BEFORE_MINUTES of market close (4:00 PM ET)
    """
    if not ENFORCE_MARKET_HOURS:
        return False
    
    current_et = get_current_et_time()
    current_time = current_et.time()
    current_weekday = current_et.weekday()
    
    # Only check on weekdays
    if current_weekday > 4:
        return False
    
    # Market closes at 4:00 PM ET
    market_close = time(16, 0)
    # Calculate the time to start closing (e.g., 3:55 PM for 5-minute buffer)
    close_hour = 15 if AUTO_CLOSE_BEFORE_MINUTES >= 60 else 16
    close_minute = (60 - AUTO_CLOSE_BEFORE_MINUTES) if AUTO_CLOSE_BEFORE_MINUTES < 60 else (60 - (AUTO_CLOSE_BEFORE_MINUTES - 60))
    close_buffer_time = time(close_hour, close_minute)
    
    # If it's past the buffer time but before market close
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
            # Use market order to close quickly
            close_url = f"{BASE_URL}/v2/positions/{symbol}"
            response = requests.delete(close_url, headers=HEADERS)
            response.raise_for_status()
            print(f"AUTO-CLOSE: Closed position for {symbol}")
            success_count += 1
        except Exception as e:
            print(f"AUTO-CLOSE ERROR: Failed to close {symbol}: {e}")
    
    return success_count == len(positions)

def close_position(symbol, alert_price_str, market_is_open, strategy_type):
    """
    Closes the entire position for a given symbol.
    - Uses a market order during regular hours for a prompt exit.
    - Uses a limit order during extended hours to ensure the order can be placed.
    """
    qty_to_close = get_position_qty(symbol)
    if qty_to_close == 0:
        msg = f"No open position for {symbol} to close."
        print(msg)
        return jsonify({"message": msg}), 200

    if market_is_open:
        # During market hours, a market order is fast and reliable.
        # The DELETE endpoint liquidates the position using a market order.
        close_position_url = f"{BASE_URL}/v2/positions/{symbol}"
        try:
            response = requests.delete(close_position_url, headers=HEADERS)
            response.raise_for_status()
            print(f"Market close order for {symbol} submitted successfully.")
            return jsonify({"message": "Market close order submitted", "data": response.json()}), response.status_code
        except requests.exceptions.HTTPError as e:
            print(f"Error closing position {symbol}: {e.response.text}")
            return jsonify({"error": f"Failed to close position: {e.response.text}"}), e.response.status_code
    else:
        # During extended hours, a market order will likely fail.
        # We must use a limit order with the 'extended_hours' flag.
        try:
            alert_price = float(alert_price_str)
            
            # The side for closing depends on the strategy type
            # If long, exit_action is 'sell'. If short, exit_action is 'buy'.
            exit_side = "sell" if strategy_type == "long" else "buy"

            # Use the alert price as the limit price for the order.
            # For a sell limit, you want to sell at or above this price.
            # For a buy limit (to cover short), you want to buy at or below this price.
            limit_price = round(alert_price, 2)

            order_data = {
                "symbol": symbol,
                "qty": qty_to_close,
                "side": exit_side,  # Dynamically set 'buy' or 'sell' to close position
                "type": "limit",
                "limit_price": str(limit_price),
                "time_in_force": "day",  # Good 'Til Canceled, so the order persists until filled or cancelled
                "extended_hours": True
            }

            order_response = requests.post(f"{BASE_URL}/v2/orders", json=order_data, headers=HEADERS)
            order_response.raise_for_status()
            print(f"Limit close order ({exit_side}) for {symbol} placed successfully for extended hours.")
            return jsonify({"message": "Limit close order submitted", "data": order_response.json()}), order_response.status_code
        except (ValueError, TypeError):
            return jsonify({"error": f"Invalid price format received for closing order: {alert_price_str}"}), 400
        except requests.exceptions.HTTPError as e:
            print(f"Error placing limit close order: {e.response.text}")
            return jsonify({"error": f"Failed to place limit close order: {e.response.text}"}), e.response.status_code


# --- Webhook Endpoint ---

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Receives alerts from TradingView and places orders.
    - Uses 10% of buying power for trade size.
    - Places MARKET orders during regular hours.
    - Places LIMIT orders during extended hours.
    - Enforces market hours restrictions if enabled.
    """
    data = request.get_json()
    print(f"Received alert for '{STRATEGY_TYPE}' strategy: {data}")

    symbol = data.get("ticker")
    action = data.get("action")
    alert_price_str = data.get("price")

    if not all([symbol, action, alert_price_str]):
        return jsonify({"error": "Invalid payload, missing ticker, action, or price"}), 400
    
    # Check the ENABLE_TRADING flag
    if not ENABLE_TRADING:
        msg = f"Trading is currently disabled. No order submitted for {symbol}."
        print(msg)
        return jsonify({"message": msg}), 200
    
    # === MARKET HOURS CHECKS ===
    
    # 1. Check if we should auto-close positions before market close
    if ENFORCE_MARKET_HOURS and is_near_market_close():
        print("AUTO-CLOSE TRIGGERED: Near market close, closing all positions...")
        close_success = close_all_positions()
        msg = f"Auto-close triggered {AUTO_CLOSE_BEFORE_MINUTES} min before market close. Positions {'closed successfully' if close_success else 'closure had errors'}."
        return jsonify({"message": msg, "auto_close": True}), 200
    
    # 2. Check if we're within allowed trading hours
    if ENFORCE_MARKET_HOURS and not is_within_trading_hours():
        current_et = get_current_et_time()
        msg = f"BLOCKED: Trading outside allowed hours. Current time: {current_et.strftime('%I:%M %p ET')}. Allowed: 4:00 AM - 4:00 PM ET (weekdays)."
        print(msg)
        return jsonify({"message": msg, "blocked_time": current_et.isoformat()}), 200
        
    # Check market status once for the whole request
    market_is_open = is_market_open()

    entry_action = "buy" if STRATEGY_TYPE == "long" else "sell"
    exit_action = "sell" if STRATEGY_TYPE == "long" else "buy"

    if action == exit_action:
        # Call the updated close_position function with the market status and alert price
        # Pass STRATEGY_TYPE to close_position to determine the correct exit side ('buy' for short, 'sell' for long)
        return close_position(symbol, alert_price_str, market_is_open, STRATEGY_TYPE)

    elif action == entry_action:
        if get_position_qty(symbol) > 0:
            msg = f"Position already exists for {symbol}, skipping new entry order."
            print(msg)
            return jsonify({"message": msg}), 200

        try:
            # --- 1. Calculate Trade Size (10% of Buying Power) ---
            buying_power = get_buying_power()
            alert_price = float(alert_price_str)
            
            if alert_price <= 0:
                return jsonify({"error": "Invalid price received from alert."}), 400

            trade_allocation = buying_power * 0.10
            qty = int(trade_allocation // alert_price)

            if qty < 1:
                msg = f"Not enough buying power for one share at ${alert_price:.2f} with 10% allocation."
                print(msg)
                return jsonify({"message": msg}), 200

            # --- 2. Determine Order Type (Market vs. Limit) ---
            order_data = {
                "symbol": symbol,
                "qty": qty,
                "side": entry_action,
                "time_in_force": "day",
            }

            if market_is_open:
                print("Market is open. Placing a MARKET order.")
                order_data["type"] = "market"
            else:
                print("Market is closed. Placing a LIMIT order for extended hours.")
                # For both buy and sell limit orders, use the alert price directly.
                # This ensures the order attempts to fill at the price the alert was triggered.
                limit_price = round(alert_price, 2)
                
                order_data["type"] = "limit"
                order_data["limit_price"] = str(limit_price)
                order_data["extended_hours"] = True
                # For extended hours, 'day' time_in_force might not be ideal.
                # Using 'gtc' (Good 'Til Canceled) ensures the order persists.
                order_data["time_in_force"] = "day"  

        except (ValueError, TypeError):
            return jsonify({"error": f"Invalid price format received: {alert_price_str}"}), 400
        except Exception as e:
            print(f"An unexpected error occurred during order preparation: {e}")
            return jsonify({"error": str(e)}), 500

        # --- 3. Submit the Order ---
        try:
            order_response = requests.post(f"{BASE_URL}/v2/orders", json=order_data, headers=HEADERS)
            order_response.raise_for_status()
            print(f"Order ({order_data['type']}) for {qty} shares of {symbol} placed successfully.")
            return jsonify({"message": "Order placed", "data": order_response.json()}), order_response.status_code
        except requests.exceptions.HTTPError as e:
            print(f"Error placing entry order: {e.response.text}")
            return jsonify({"error": f"Failed to place entry order: {e.response.text}"}), e.response.status_code

    return jsonify({"message": f"Action '{action}' does not match expected actions for '{STRATEGY_TYPE}' strategy."}), 200

# --- Status Endpoint ---

@app.route("/status", methods=["GET"])
def status():
    """
    Returns current market hours status and trading configuration
    """
    current_et = get_current_et_time()
    within_hours = is_within_trading_hours()
    near_close = is_near_market_close()
    market_open = is_market_open()
    
    # Get position count
    positions = get_all_positions()
    position_count = len(positions)
    
    status_info = {
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
            "strategy_type": STRATEGY_TYPE
        },
        "trading_hours": {
            "pre_market": "4:00 AM - 9:30 AM ET",
            "regular_market": "9:30 AM - 4:00 PM ET", 
            "after_hours": "BLOCKED (4:00 PM - 4:00 AM ET next day)"
        },
        "positions": {
            "count": position_count,
            "symbols": [pos["symbol"] for pos in positions] if positions else []
        }
    }
    
    # Add timezone conversion for Pacific Time users
    pt_tz = pytz.timezone('US/Pacific')
    current_pt = current_et.astimezone(pt_tz)
    status_info["current_time"]["pacific"] = current_pt.strftime("%Y-%m-%d %I:%M:%S %p PT")
    status_info["trading_hours"]["pacific_equivalent"] = {
        "pre_market": "1:00 AM - 6:30 AM PT",
        "regular_market": "6:30 AM - 1:00 PM PT"
    }
    
    return jsonify(status_info), 200

# --- Main Application Runner ---

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
