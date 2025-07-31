from flask import Flask, request, jsonify
import requests
import os

app = Flask(__name__)

# --- Alpaca API Configuration ---
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
BASE_URL = "https://paper-api.alpaca.markets"

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
            raise # Re-raise other HTTP errors

def get_buying_power():
    """Retrieves the current buying power."""
    account_response = requests.get(f"{BASE_URL}/v2/account", headers=HEADERS)
    account_response.raise_for_status()
    return float(account_response.json()["buying_power"])

def get_latest_price(symbol):
    """Retrieves the latest ask price for a symbol."""
    quote_url = f"{BASE_URL}/v2/stocks/{symbol}/quotes/latest"
    quote_response = requests.get(quote_url, headers=HEADERS)
    quote_response.raise_for_status()
    # Using the ask price as it's the price you'd pay to buy
    return float(quote_response.json()["quote"]["ap"])

# --- Webhook Endpoint ---
@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Receives alerts from TradingView and places orders with Alpaca.
    """
    data = request.get_json()
    print(f"Received alert: {data}")

    symbol = data.get("ticker")
    action = data.get("action")

    if not symbol or action not in ["buy", "sell"]:
        return jsonify({"error": "Invalid payload, missing 'ticker' or 'action'"}), 400

    if action == "sell":
        # Use the dedicated "close position" endpoint.
        close_position_url = f"{BASE_URL}/v2/positions/{symbol}"
        
        try:
            response = requests.delete(close_position_url, headers=HEADERS)
            response.raise_for_status()
            print(f"Close order for {symbol} submitted successfully.")
            return jsonify({"message": "Close order submitted", "data": response.json()}), response.status_code
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                msg = f"No open position for {symbol} to sell."
                print(msg)
                return jsonify({"message": msg}), 200
            else:
                print(f"Error closing position {symbol}: {e.response.text}")
                return jsonify({"error": f"Failed to close position: {e.response.text}"}), e.response.status_code

    elif action == "buy":
        # 1. Prevent buying if a position already exists.
        if position_exists(symbol):
            msg = f"Position already exists for {symbol}, skipping buy order."
            print(msg)
            return jsonify({"message": msg}), 200

        # 2. Calculate order quantity.
        try:
            buying_power = get_buying_power()
            latest_price = get_latest_price(symbol)
            
            # Allocate 10% of buying power to this trade
            trade_allocation = buying_power * 0.10
            qty = int(trade_allocation // latest_price)

            if qty < 1:
                return jsonify({"error": "Not enough buying power for one share."}), 400
        except Exception as e:
            print(f"Error during buy calculation: {e}")
            return jsonify({"error": str(e)}), 500

        # 3. Place the buy order.
        order_data = {
            "symbol": symbol,
            "qty": qty,
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
            "extended_hours": True
        }

        try:
            order_response = requests.post(f"{BASE_URL}/v2/orders", json=order_data, headers=HEADERS)
            order_response.raise_for_status()
            print("Buy order placed successfully.")
            return jsonify({"message": "Buy order placed", "data": order_response.json()}), order_response.status_code
        except requests.exceptions.HTTPError as e:
            print(f"Error placing buy order: {e.response.text}")
            return jsonify({"error": f"Failed to place buy order: {e.response.text}"}), e.response.status_code

# --- Main Application Runner ---
if __name__ == "__main__":
    # For production, use a proper WSGI server like Gunicorn
    app.run(debug=True)
