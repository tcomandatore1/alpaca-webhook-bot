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

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    print(f"Received alert: {data}")

    symbol = data.get("ticker")
    action = data.get("action")
    qty = int(data.get("qty", 1))

    if action not in ["buy", "sell"]:
        return jsonify({"error": "Invalid action"}), 400

    if action == "sell":
        if not position_exists(symbol):
            msg = f"No open position in {symbol}, skipping sell order."
            print(msg)
            return jsonify({"message": msg}), 200

    order = {
        "symbol": symbol,
        "qty": qty,
        "side": action,
        "type": "market",
        "time_in_force": "gtc",
        "extended_hours": False 
    }

    response = requests.post(f"{BASE_URL}/v2/orders", json=order, headers=HEADERS)

    if response.status_code == 200:
        print("Order placed successfully")
        return jsonify({"message": "Order placed", "alpaca_response": response.json()}), 200
    else:
        print("Error placing order:", response.text)
        return jsonify({"error": response.text}), 500

if __name__ == "__main__":
    app.run()
