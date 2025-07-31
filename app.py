from flask import Flask, request, jsonify
import requests
import os

app = Flask(__name__)

ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
BASE_URL = "https://paper-api.alpaca.markets"

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY
}

def position_exists(symbol):
    response = requests.get(f"{BASE_URL}/v2/positions/{symbol}", headers=HEADERS)
    return response.status_code == 200

def get_buying_power():
    account_response = requests.get(f"{BASE_URL}/v2/account", headers=HEADERS)
    if account_response.status_code == 200:
        return float(account_response.json()["buying_power"])
    else:
        raise Exception("Failed to retrieve account info.")

def get_latest_price(symbol):
    quote_url = f"{BASE_URL}/v2/stocks/{symbol}/quotes/latest"
    quote_response = requests.get(quote_url, headers=HEADERS)
    if quote_response.status_code == 200:
        return float(quote_response.json()["quote"]["ap"])
    else:
        raise Exception("Failed to retrieve latest price.")

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    print(f"Received alert: {data}")

    symbol = data.get("ticker")
    action = data.get("action")

    if action not in ["buy", "sell"]:
        return jsonify({"error": "Invalid action"}), 400

    if action == "sell":
        if not position_exists(symbol):
            msg = f"No open position in {symbol}, skipping sell order."
            print(msg)
            return jsonify({"message": msg}), 200
        qty = "all"  # Could enhance this to fetch open quantity if needed
    else:
        # Buy logic with 10% buying power
        try:
            buying_power = get_buying_power()
            latest_price = get_latest_price(symbol)
            allocation = buying_power * 0.10
            qty = int(allocation // latest_price)
            if qty < 1:
                return jsonify({"error": "Not enough buying power to purchase even one share."}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    order = {
        "symbol": symbol,
        "qty": qty,
        "side": action,
        "type": "market",
        "time_in_force": "gtc",
        "extended_hours": True  # <== enable after-hours trading
    }

    response = requests.post(f"{BASE_URL}/v2/orders", json=order, headers=HEADERS)

    if response.status_code in [200, 201]:
        print("Order placed successfully")
        return jsonify({"message": "Order placed", "alpaca_response": response.json()}), 200
    else:
        print("Error placing order:", response.text)
        return jsonify({"error": response.text}), 500

if __name__ == "__main__":
    app.run()
