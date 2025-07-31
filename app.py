from flask import Flask, request, jsonify
import requests
import os

app = Flask(__name__)

# --- Alpaca API Configuration ---
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
BASE_URL = "https://paper-api.alpaca.markets"

# --- Strategy Configuration ---
STRATEGY_TYPE = os.environ.get("STRATEGY_TYPE", "long").lower()

# --- Trade Size Configuration ---
# This will now be used as the "notional" value for each trade.
TRADE_DOLLAR_AMOUNT = float(os.environ.get("TRADE_DOLLAR_AMOUNT", "5000.0"))


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
    """
    Retrieves the 'regt_buying_power'. This is more reliable for both
    long and short positions than 'daytrading_buying_power'.
    """
    account_response = requests.get(f"{BASE_URL}/v2/account", headers=HEADERS)
    account_response.raise_for_status()
    # Use the buying power figure that matches the dashboard
    return float(account_response.json()["regt_buying_power"])

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
            # --- MODIFIED: NOTIONAL ORDER LOGIC ---
            buying_power = get_buying_power()
            notional_value = TRADE_DOLLAR_AMOUNT
            
            # Safety Check: Ensure trade amount doesn't exceed buying power
            if notional_value > buying_power:
                msg = f"Trade amount ${notional_value:.2f} exceeds buying power ${buying_power:.2f}. Skipping."
                print(msg)
                return jsonify({"message": msg}), 200

            # We no longer need to get the price and calculate quantity beforehand.
            # We will submit a 'notional' order directly.
            order_data = {
                "symbol": symbol,
                "notional": str(notional_value), # Must be a string for the API
                "side": entry_action,
                "type": "market",
                "time_in_force": "day",
            }
        
        except Exception as e:
            print(f"An unexpected error occurred during order preparation: {e}")
            return jsonify({"error": str(e)}), 500

        try:
            order_response = requests.post(f"{BASE_URL}/v2/orders", json=order_data, headers=HEADERS)
            order_response.raise_for_status()
            print(f"Notional entry order ({entry_action}) for ${notional_value} of {symbol} placed successfully.")
            return jsonify({"message": "Notional order placed", "data": order_response.json()}), order_response.status_code
        except requests.exceptions.HTTPError as e:
            print(f"Error placing entry order: {e.response.text}")
            return jsonify({"error": f"Failed to place entry order: {e.response.text}"}), e.response.status_code
            
    return jsonify({"message": f"Action '{action}' does not match expected actions for '{STRATEGY_TYPE}' strategy."}), 200

# --- Main Application Runner ---

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
