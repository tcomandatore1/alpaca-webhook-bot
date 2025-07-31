from flask import Flask, request, jsonify
import requests
import os

app = Flask(__name__)

# --- Alpaca API Configuration ---
# Ensure these are set in your Render environment variables
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
BASE_URL = "https://paper-api.alpaca.markets"

# --- Strategy Configuration ---
# Set this in Render: "long" for your long account, "short" for your short account
STRATEGY_TYPE = os.environ.get("STRATEGY_TYPE", "long").lower()


HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY
}

# --- Helper Functions ---

def position_exists(symbol):
    """Checks if a position exists for a given symbol."""
    try:
        response = requests.get(f"{BASE_URL}/v2/positions/{symbol}", headers=HEADERS)
        response.raise_for_status()
        return True
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            return False
        else:
            print(f"Error checking position for {symbol}: {e.response.text}")
            raise

def get_buying_power():
    """Retrieves the current 'daytrading_buying_power'."""
    account_response = requests.get(f"{BASE_URL}/v2/account", headers=HEADERS)
    account_response.raise_for_status()
    return float(account_response.json()["daytrading_buying_power"])

def get_latest_price(symbol):
    """
    Retrieves the latest bar's close price for a symbol.
    This is more reliable for all account types than the 'quotes' endpoint.
    """
    try:
        # Use the /bars/latest endpoint which is universally available
        bar_url = f"{BASE_URL}/v2/stocks/{symbol}/bars/latest"
        
        # We specify the 'feed' for free data plans, which is 'iex'
        params = {'feed': 'iex'}
        
        response = requests.get(bar_url, headers=HEADERS, params=params)
        response.raise_for_status() 
        
        # Return the close price 'c' of the latest 1-minute bar
        return float(response.json()["bar"]["c"])
    except requests.exceptions.HTTPError as e:
        print(f"Error fetching latest price for {symbol}: {e}")
        raise # Re-raise the exception to be caught in the webhook

def close_position(symbol):
    """Closes the entire position for a given symbol."""
    close_position_url = f"{BASE_URL}/v2/positions/{symbol}"
    try:
        response = requests.delete(close_position_url, headers=HEADERS)
        response.raise_for_status()
        print(f"Close order for {symbol} submitted successfully.")
        return jsonify({"message": "Close order submitted", "data": response.json()}), response.status_code
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            msg = f"No open position for {symbol} to close."
            print(msg)
            return jsonify({"message": msg}), 200
        else:
            print(f"Error closing position {symbol}: {e.response.text}")
            return jsonify({"error": f"Failed to close position: {e.response.text}"}), e.response.status_code

# --- Webhook Endpoint ---

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Receives alerts from TradingView and places orders based on STRATEGY_TYPE.
    """
    data = request.get_json()
    print(f"Received alert for '{STRATEGY_TYPE}' strategy: {data}")

    symbol = data.get("ticker")
    action = data.get("action") 

    if not symbol or action not in ["buy", "sell"]:
        return jsonify({"error": "Invalid payload, missing 'ticker' or valid 'action'"}), 400

    entry_action = "buy" if STRATEGY_TYPE == "long" else "sell"
    exit_action = "sell" if STRATEGY_TYPE == "long" else "buy"

    if action == exit_action:
        return close_position(symbol)

    elif action == entry_action:
        if position_exists(symbol):
            msg = f"Position already exists for {symbol}, skipping new entry order."
            print(msg)
            return jsonify({"message": msg}), 200

        try:
            buying_power = get_buying_power()
            price_to_use = get_latest_price(symbol)
            
            trade_allocation = buying_power * 0.10
            qty = int(trade_allocation // price_to_use)

            if qty < 1:
                return jsonify({"error": f"Not enough buying power for one share at ${price_to_use}."}), 400

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                msg = f"Ticker '{symbol}' not found or is not tradable on Alpaca. Skipping order."
                print(msg)
                return jsonify({"message": msg}), 200
            else:
                print(f"Alpaca API Error during calculation: {e.response.text}")
                return jsonify({"error": f"Alpaca API Error: {e.response.text}"}), 500
        except Exception as e:
            print(f"An unexpected error occurred during calculation: {e}")
            return jsonify({"error": str(e)}), 500

        order_data = {
            "symbol": symbol,
            "qty": qty,
            "side": entry_action,
            "type": "market",
            "time_in_force": "day",
        }

        try:
            order_response = requests.post(f"{BASE_URL}/v2/orders", json=order_data, headers=HEADERS)
            order_response.raise_for_status()
            print(f"Entry order ({entry_action}) for {qty} shares of {symbol} placed successfully.")
            return jsonify({"message": "Entry order placed", "data": order_response.json()}), order_response.status_code
        except requests.exceptions.HTTPError as e:
            print(f"Error placing entry order: {e.response.text}")
            return jsonify({"error": f"Failed to place entry order: {e.response.text}"}), e.response.status_code
            
    return jsonify({"message": f"Action '{action}' does not match expected actions for '{STRATEGY_TYPE}' strategy."}), 200

# --- Main Application Runner ---

if __name__ == "__main__":
    # For production on Render, it will use a WSGI server like Gunicorn
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
