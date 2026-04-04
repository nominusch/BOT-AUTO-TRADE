# =====================
# IMPORT
# =====================
import os
import sys
import time
import threading
import requests
import pandas as pd
import ta
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from telegram import Bot
import importlib
import logging

# =====================
# LOGGING
# =====================
log_file = "bot.log"
logging.basicConfig(
    filename=log_file,
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

def log_and_msg(text, level="info"):
    send_msg(text)
    if level=="info":
        logging.info(text)
    elif level=="error":
        logging.error(text)
    elif level=="warn":
        logging.warning(text)

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
risk_per_trade = 0.01   # 1% modal per trade
max_daily_loss = -0.03  # -3% modal harian

sl_multiplier = 1.5
tp_multiplier = 3.0
trailing_pct = 0.005
dashboard_interval = 3600  # 1 jam

positions = {}   # multi-position support
daily_pnl = 0

lock = threading.Lock()  # thread-safe

loaded_skills = {}  # modul yang sudah di-load

# =====================
# UTILS
# =====================
def send_msg(text):
    try:
        bot.send_message(chat_id=CHAT_ID, text=text)
    except:
        logging.error(f"Gagal kirim pesan Telegram: {text}")

def get_balance():
    try:
        info = client.futures_account()
        for x in info["assets"]:
            if x["asset"] == "USDT":
                return float(x["availableBalance"])
    except (BinanceAPIException, BinanceRequestException) as e:
        logging.error(f"Error get_balance: {e}")
        return 0
    return 0

def get_qty(symbol, price):
    balance = get_balance()
    risk = balance * risk_per_trade
    qty = (risk * leverage) / price
    return round(qty, 3)

def get_data(symbol, interval):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit=100"
    try:
        df = pd.DataFrame(requests.get(url).json())
        df = df[[0,1,2,3,4,5]]
        df.columns = ["time","open","high","low","close","volume"]
        for col in ["close","high","low","open","volume"]:
            df[col] = df[col].astype(float)
        return df
    except Exception as e:
        logging.error(f"Error get_data {symbol} {interval}: {e}")
        return pd.DataFrame()

# =====================
# TREND & AI FILTER
# =====================
def trend_ai_filter(df_short, df_medium, df_long):
    def analyze(df):
        df["ema20"] = ta.trend.ema_indicator(df["close"], window=20)
        df["ema50"] = ta.trend.ema_indicator(df["close"], window=50)
        df["rsi"] = ta.momentum.rsi(df["close"], window=14)
        last = df.iloc[-1]
        prev = df.iloc[-2]
        trend = "SIDE"
        if last["close"] > last["ema20"] > last["ema50"] and 50 < last["rsi"] < 70:
            trend = "UP"
        elif last["close"] < last["ema20"] < last["ema50"] and 30 < last["rsi"] < 50:
            trend = "DOWN"
        body = abs(last["close"] - last["open"])
        candle_range = last["high"] - last["low"]
        volume_cond = last["volume"] > prev["volume"]
        ai_signal = (body > candle_range * 0.6) and volume_cond
        return trend, ai_signal

    trend_short, ai_short = analyze(df_short)
    trend_med, ai_med = analyze(df_medium)
    trend_long, ai_long = analyze(df_long)

    if trend_short == trend_med == trend_long and ai_short and ai_med and ai_long:
        return trend_short
    return "SIDE"

# =====================
# SIGNAL GENERATOR
# =====================
def generate_signal(df_short):
    rsi = ta.momentum.rsi(df_short["close"], window=14).iloc[-1]
    last = df_short.iloc[-1]
    prev = df_short.iloc[-2]
    if rsi < 30:
        return "BUY"
    elif rsi > 70:
        return "SELL"
    if last["close"] > prev["high"]:
        return "BUY"
    elif last["close"] < prev["low"]:
        return "SELL"
    return None

# =====================
# ATR DYNAMIC STOP / TP
# =====================
def calc_atr_sl_tp(df, side, entry_price):
    atr = ta.volatility.average_true_range(df["high"], df["low"], df["close"], window=14).iloc[-1]
    if side=="BUY":
        sl = entry_price - sl_multiplier * atr
        tp = entry_price + tp_multiplier * atr
    else:
        sl = entry_price + sl_multiplier * atr
        tp = entry_price - tp_multiplier * atr
    return sl, tp

# =====================
# ENTRY & EXIT
# =====================
def entry(symbol, side, df_long):
    global daily_pnl
    with lock:
        if daily_pnl <= max_daily_loss:
            log_and_msg("⚠️ DAILY MAX LOSS TERCAPAI, STOP TRADING HARI INI", "warn")
            return
    try:
        price = float(client.futures_symbol_ticker(symbol=symbol)["price"])
        client.futures_change_leverage(symbol=symbol, leverage=leverage)
        qty = get_qty(symbol, price)
        sl, tp = calc_atr_sl_tp(df_long, side, price)
        client.futures_create_order(symbol=symbol, side=side, type="MARKET", quantity=qty)
        with lock:
            positions[symbol] = {
                "entry": price,
                "qty": qty,
                "side": side,
                "sl": sl,
                "tp": tp,
                "trail_price": price
            }
        log_and_msg(f"🚀 ENTRY {symbol} {side} | Price: {price:.2f} | SL: {sl:.2f} | TP: {tp:.2f}")
    except Exception as e:
        log_and_msg(f"Error ENTRY {symbol}: {e}", "error")

def exit_position(symbol, reason=""):
    global daily_pnl
    try:
        with lock:
            pos = positions[symbol]
        price = float(client.futures_symbol_ticker(symbol=symbol)["price"])
        side = "SELL" if pos["side"]=="BUY" else "BUY"
        client.futures_create_order(symbol=symbol, side=side, type="MARKET", quantity=pos["qty"])
        pnl = (price - pos["entry"])/pos["entry"]
        if pos["side"]=="SELL": pnl *= -1
        with lock:
            daily_pnl += pnl
            del positions[symbol]
        log_and_msg(f"❌ EXIT {symbol} | PNL: {round(pnl*100,2)}% | {reason}")
    except Exception as e:
        log_and_msg(f"Error EXIT {symbol}: {e}", "error")

# =====================
# MANAGE POSITION (SL / TP / TRAILING)
# =====================
def manage_position(symbol, df_long):
    try:
        with lock:
            pos = positions[symbol]
        price = float(client.futures_symbol_ticker(symbol=symbol)["price"])
        profit = (price - pos["entry"])/pos["entry"]
        if pos["side"]=="SELL": profit *= -1

        if (pos["side"]=="BUY" and price<=pos["sl"]) or (pos["side"]=="SELL" and price>=pos["sl"]):
            exit_position(symbol, "STOP LOSS")
            return
        if (pos["side"]=="BUY" and price>=pos["tp"]) or (pos["side"]=="SELL" and price<=pos["tp"]):
            exit_position(symbol, "TAKE PROFIT")
            return

        with lock:
            if pos["side"]=="BUY":
                if price > pos["trail_price"]:
                    pos["trail_price"] = price
                elif price < pos["trail_price"]*(1-trailing_pct):
                    exit_position(symbol, "TRAILING STOP")
            elif pos["side"]=="SELL":
                if price < pos["trail_price"]:
                    pos["trail_price"] = price
                elif price > pos["trail_price"]*(1+trailing_pct):
                    exit_position(symbol, "TRAILING STOP")
    except Exception as e:
        log_and_msg(f"Error MANAGE {symbol}: {e}", "error")

# =====================
# DASHBOARD TELEGRAM
# =====================
def dashboard_loop():
    while True:
        try:
            with lock:
                pos_text = "\n".join([f"{s}: {p['side']} | Entry {p['entry']:.2f} | QTY {p['qty']}" for s,p in positions.items()]) or "No active positions"
                log_and_msg(f"📊 DASHBOARD\nDaily PNL: {round(daily_pnl*100,2)}%\nPositions:\n{pos_text}")
        except:
            pass
        time.sleep(dashboard_interval)

# =====================
# LOAD SKILLS
# =====================
def load_skills():
    skills_folder = "./.claude/skills/trading"
    if not os.path.exists(skills_folder):
        log_and_msg(f"❌ Folder skills tidak ditemukan: {skills_folder}", "warn")
        return

    for skill_name in os.listdir(skills_folder):
        skill_path = os.path.join(skills_folder, skill_name)
        init_file = os.path.join(skill_path, "__init__.py")
        if os.path.isdir(skill_path) and os.path.isfile(init_file):
            module_name = f".claude.skills.trading.{skill_name}.main"
            try:
                if module_name in sys.modules:
                    importlib.reload(sys.modules[module_name])
                else:
                    importlib.import_module(module_name)
                loaded_skills[skill_name] = module_name
                log_and_msg(f"✅ Skill '{skill_name}' berhasil di-load!")
            except Exception as e:
                log_and_msg(f"❌ Gagal load skill '{skill_name}': {e}", "error")

def auto_reload_skills(interval=60):
    while True:
        load_skills()
        time.sleep(interval)

# =====================
# MAIN
# =====================
log_and_msg("✅ AI AGENT AKTIF")

threading.Thread(target=dashboard_loop, daemon=True).start()
threading.Thread(target=auto_reload_skills, daemon=True).start()

while True:
    try:
        for symbol in pairs:
            df5 = get_data(symbol, "5m")
            df15 = get_data(symbol, "15m")
            df1h = get_data(symbol, "1h")

            trend_dir = trend_ai_filter(df5, df15, df1h)
            signal = generate_signal(df5)

            if symbol not in positions and signal and trend_dir != "SIDE":
                entry(symbol, signal, df1h)

            if symbol in positions:
                manage_position(symbol, df1h)
    except Exception as e:
        log_and_msg(f"Global Error: {e}", "error")
    time.sleep(15)
