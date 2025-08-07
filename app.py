from flask import Flask, request, jsonify
import requests
import os

app = Flask(__name__)

# --- Alpaca API Configuration ---
# Ensure these environment variables are set in your deployment environment
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
BASE_URL = "https://paper-api.alpaca.markets" # This is the paper trading endpoint

# --- Trading Kill Switch ---
# Set to True to enable trading, False to disable all order submissions.
# For paper trading, it's often safer to keep this as False by default
# unless you are actively testing and want orders to go through.
ENABLE_TRADING = True 


# --- Helper Functions ---

def get_position_qty(symbol):
    try:
        resp = requests.get(f"{BASE_URL}/v2/positions/{symbol}", headers=HEADERS)
        resp.raise_for_status()
        return int(resp.json()["qty"])
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            return 0
        print(f"Error checking position: {e.response.text}")
        raise

def get_buying_power():
    resp = requests.get(f"{BASE_URL}/v2/account", headers=HEADERS)
    resp.raise_for_status()
    return float(resp.json()["regt_buying_power"])

def is_market_open():
    resp = requests.get(f"{BASE_URL}/v2/clock", headers=HEADERS)
    resp.raise_for_status()
    return resp.json()["is_open"]


# --- Order Execution ---

def close_position(symbol, alert_price_str, market_is_open, strategy_type):
    qty_to_close = get_position_qty(symbol)
    if qty_to_close == 0:
        return jsonify({"message": f"No open position for {symbol} to close."}), 200

    order_qty = abs(qty_to_close)
    side = "sell" if strategy_type == "long" else "buy"

    try:
        if market_is_open:
            resp = requests.delete(f"{BASE_URL}/v2/positions/{symbol}", headers=HEADERS)
            resp.raise_for_status()
            return jsonify({"message": "Market close order submitted", "data": resp.json()}), 200
        else:
            limit_price = round(float(alert_price_str), 2)
            order_data = {
                "symbol": symbol,
                "qty": order_qty,
                "side": side,
                "type": "limit",
                "limit_price": str(limit_price),
                "time_in_force": "day",
                "extended_hours": True
            }
            resp = requests.post(f"{BASE_URL}/v2/orders", json=order_data, headers=HEADERS)
            resp.raise_for_status()
            return jsonify({"message": "Extended-hours close order submitted", "data": resp.json()}), 200
    except Exception as e:
        return jsonify({"error": f"Close position failed: {e}"}), 500


# --- Webhook Endpoint ---

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    print(f"Received alert: {data}")

    symbol = data.get("ticker")
    action = data.get("action")
    alert_price_str = data.get("price")

    if not all([symbol, action, alert_price_str]):
        return jsonify({"error": "Missing ticker, action, or price"}), 400

    if not ENABLE_TRADING:
        return jsonify({"message": "Trading disabled. No order submitted."}), 200

    market_is_open = is_market_open()
    entry_action = "buy" if STRATEGY_TYPE == "long" else "sell"
    exit_action = "sell" if STRATEGY_TYPE == "long" else "buy"

    if action == exit_action:
        return close_position(symbol, alert_price_str, market_is_open, STRATEGY_TYPE)

    elif action == entry_action:
        if get_position_qty(symbol) > 0:
            return jsonify({"message": f"Position already exists for {symbol}. Skipping entry."}), 200

        try:
            buying_power = get_buying_power()
            alert_price = float(alert_price_str)
            trade_allocation = buying_power * 0.10
            qty = int(trade_allocation // alert_price)

            if qty < 1:
                return jsonify({"message": f"Not enough buying power for 1 share at ${alert_price:.2f}."}), 200

            order_data = {
                "symbol": symbol,
                "qty": qty,
                "side": entry_action
            }

            if market_is_open:
                order_data["type"] = "market"
                # Do NOT include time_in_force for market orders
            else:
                limit_price = round(alert_price, 2)
                order_data.update({
                    "type": "limit",
                    "limit_price": str(limit_price),
                    "time_in_force": "day",
                    "extended_hours": True
                })

            resp = requests.post(f"{BASE_URL}/v2/orders", json=order_data, headers=HEADERS)
            resp.raise_for_status()
            return jsonify({"message": "Order placed", "data": resp.json()}), 200

        except Exception as e:
            print(f"Entry order error: {e}")
            return jsonify({"error": f"Entry order failed: {e}"}), 500

    return jsonify({"message": f"Ignored action '{action}' for strategy type '{STRATEGY_TYPE}'."}), 200


# --- Run App ---

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
