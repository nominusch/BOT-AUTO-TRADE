import requests
import pandas as pd
import ta
import time
from binance.client import Client
from telegram import Bot
import os

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

client = Client(API_KEY, API_SECRET)
bot = Bot(token=TOKEN)

pairs = ["BTCUSDT", "ETHUSDT", "BNBUSDT"]
lot_size_usdt = 20

positions = {}

# =====================
# DATA
# =====================
def get_data(symbol, interval):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit=100"
    df = pd.DataFrame(requests.get(url).json())
    df = df[[0,1,2,3,4,5]]
    df.columns = ["time","open","high","low","close","volume"]
    df["close"] = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)
    return df

# =====================
# TREND
# =====================
def trend(df):
    df["ema20"] = ta.trend.ema_indicator(df["close"], window=20)
    df["ema50"] = ta.trend.ema_indicator(df["close"], window=50)
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)

    last = df.iloc[-1]

    if last["close"] > last["ema20"] > last["ema50"] and last["rsi"] > 50:
        return "UP"
    elif last["close"] < last["ema20"] < last["ema50"] and last["rsi"] < 50:
        return "DOWN"
    return "SIDE"

# =====================
# AI FILTER (SMART ENTRY)
# =====================
def ai_filter(df):
    last = df.iloc[-1]
    prev = df.iloc[-2]

    body = abs(last["close"] - last["open"])
    candle_range = last["high"] - last["low"]

    # candle kuat + volume naik
    if body > candle_range * 0.6 and last["volume"] > prev["volume"]:
        return True
    return False

# =====================
# ENTRY
# =====================
def entry(symbol, side):
    price = float(client.get_symbol_ticker(symbol=symbol)["price"])
    qty = round(lot_size_usdt / price, 5)

    if side == "BUY":
        client.order_market_buy(symbol=symbol, quantity=qty)
    else:
        client.order_market_sell(symbol=symbol, quantity=qty)

    positions[symbol] = {
        "entry": price,
        "qty": qty
    }

    bot.send_message(chat_id=CHAT_ID, text=f"🚀 {symbol} {side}\nPrice: {price}")

# =====================
# EXIT
# =====================
def exit(symbol):
    pos = positions[symbol]
    client.order_market_sell(symbol=symbol, quantity=pos["qty"])
    bot.send_message(chat_id=CHAT_ID, text=f"❌ EXIT {symbol}")
    del positions[symbol]

# =====================
# TRAILING
# =====================
def manage(symbol):
    pos = positions[symbol]
    price = float(client.get_symbol_ticker(symbol=symbol)["price"])

    profit = (price - pos["entry"]) / pos["entry"]

    # SL
    if profit <= -0.005:
        exit(symbol)

    # TRAILING
    elif profit >= 0.01:
        if price < pos["entry"] * 1.008:
            exit(symbol)

# =====================
# MAIN LOOP
# =====================
while True:
    try:
        for symbol in pairs:

            df5 = get_data(symbol, "5m")
            df15 = get_data(symbol, "15m")
            df1h = get_data(symbol, "1h")

            t5 = trend(df5)
            t15 = trend(df15)
            t1h = trend(df1h)

            if symbol not in positions:
                if t5 == t15 == t1h == "UP" and ai_filter(df5):
                    entry(symbol, "BUY")

            else:
                manage(symbol)

    except Exception as e:
        bot.send_message(chat_id=CHAT_ID, text=f"Error: {e}")

    time.sleep(15)
