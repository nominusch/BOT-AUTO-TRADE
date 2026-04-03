import requests
import pandas as pd
import ta
import time
import os
from binance.client import Client
from telegram import Bot

# =====================
# ENV
# =====================
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

client = Client(API_KEY, API_SECRET)
bot = Bot(token=TOKEN)

# =====================
# CONFIG
# =====================
pairs = ["BTCUSDT", "ETHUSDT", "BNBUSDT"]

leverage = 3
risk_per_trade = 0.01   # 1%
max_daily_loss = -0.03  # -3%

positions = {}
daily_pnl = 0

# =====================
# DATA
# =====================
def get_data(symbol, interval):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit=100"
    df = pd.DataFrame(requests.get(url).json())
    df = df[[0,1,2,3,4,5]]
    df.columns = ["time","open","high","low","close","volume"]
    df["close"] = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)
    return df

# =====================
# TREND PRO
# =====================
def trend(df):
    df["ema20"] = ta.trend.ema_indicator(df["close"], window=20)
    df["ema50"] = ta.trend.ema_indicator(df["close"], window=50)
    df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)

    last = df.iloc[-1]

    if last["close"] > last["ema20"] > last["ema50"] > last["ema200"] and 50 < last["rsi"] < 70:
        return "UP"
    elif last["close"] < last["ema20"] < last["ema50"] < last["ema200"] and 30 < last["rsi"] < 50:
        return "DOWN"
    return "SIDE"

# =====================
# AI FILTER
# =====================
def ai_filter(df):
    last = df.iloc[-1]
    prev = df.iloc[-2]

    body = abs(last["close"] - last["open"])
    candle_range = last["high"] - last["low"]

    if body > candle_range * 0.6 and last["volume"] > prev["volume"]:
        return True
    return False

# =====================
# BALANCE
# =====================
def get_balance():
    info = client.futures_account()
    for x in info["assets"]:
        if x["asset"] == "USDT":
            return float(x["availableBalance"])
    return 0

# =====================
# POSITION SIZE
# =====================
def get_qty(symbol, price):
    balance = get_balance()
    risk = balance * risk_per_trade
    qty = (risk * leverage) / price
    return round(qty, 3)

# =====================
# ENTRY
# =====================
def entry(symbol, side):
    price = float(client.futures_symbol_ticker(symbol=symbol)["price"])

    client.futures_change_leverage(symbol=symbol, leverage=leverage)

    qty = get_qty(symbol, price)

    client.futures_create_order(
        symbol=symbol,
        side=side,
        type="MARKET",
        quantity=qty
    )

    positions[symbol] = {
        "entry": price,
        "qty": qty,
        "side": side
    }

    bot.send_message(chat_id=CHAT_ID, text=f"🚀 FUTURES {symbol} {side}\nPrice: {price}")

# =====================
# EXIT
# =====================
def exit(symbol):
    global daily_pnl

    pos = positions[symbol]
    price = float(client.futures_symbol_ticker(symbol=symbol)["price"])

    side = "SELL" if pos["side"] == "BUY" else "BUY"

    client.futures_create_order(
        symbol=symbol,
        side=side,
        type="MARKET",
        quantity=pos["qty"]
    )

    pnl = (price - pos["entry"]) / pos["entry"]
    if pos["side"] == "SELL":
        pnl *= -1

    daily_pnl += pnl

    bot.send_message(chat_id=CHAT_ID, text=f"❌ CLOSE {symbol}\nPNL: {round(pnl*100,2)}%")

    del positions[symbol]

# =====================
# TRAILING PRO
# =====================
def manage(symbol):
    pos = positions[symbol]
    price = float(client.futures_symbol_ticker(symbol=symbol)["price"])

    profit = (price - pos["entry"]) / pos["entry"]
    if pos["side"] == "SELL":
        profit *= -1

    # STOP LOSS
    if profit <= -0.005:
        exit(symbol)

    # TRAILING
    elif profit >= 0.005:
        if pos["side"] == "BUY" and price < pos["entry"] * 1.002:
            exit(symbol)
        elif pos["side"] == "SELL" and price > pos["entry"] * 0.998:
            exit(symbol)

# =====================
# START NOTIF
# =====================
bot.send_message(chat_id=CHAT_ID, text="✅ FUTURES BOT AKTIF")

# =====================
# MAIN LOOP
# =====================
while True:
    try:
        for symbol in pairs:

            if daily_pnl <= max_daily_loss:
                continue

            df5 = get_data(symbol, "5m")
            df15 = get_data(symbol, "15m")
            df1h = get_data(symbol, "1h")

            t5 = trend(df5)
            t15 = trend(df15)
            t1h = trend(df1h)

            if symbol not in positions:
                if t5 == t15 == t1h == "UP" and ai_filter(df5):
                    entry(symbol, "BUY")
                elif t5 == t15 == t1h == "DOWN" and ai_filter(df5):
                    entry(symbol, "SELL")

            else:
                manage(symbol)

    except Exception as e:
        bot.send_message(chat_id=CHAT_ID, text=f"Error: {e}")

    time.sleep(15)
