import time
import os
import urllib.request
import urllib.parse
import json
from datetime import datetime

SYMBOLS = ["BTCUSDT", "ETHUSDT", "IOTXUSDT"]
API_URL = "https://api.binance.com/api/v3/ticker/24hr?symbol={}"
KLINES_URL = "https://api.binance.com/api/v3/klines?symbol={}&interval=1h&limit=150"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

# Tracks last alert time per symbol+signal to avoid repeats within 10 minutes
_last_alert: dict = {}


def fetch_ticker(symbol):
    url = API_URL.format(symbol)
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read())


def fetch_indicators(symbol):
    url = KLINES_URL.format(symbol)
    with urllib.request.urlopen(url, timeout=5) as resp:
        klines = json.loads(resp.read())
    closes = [float(k[4]) for k in klines]
    rsi = calculate_rsi(closes, period=14)
    _, _, macd_hist = calculate_macd(closes)
    fvg = detect_fvg(klines)
    return rsi, macd_hist, fvg


def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(diff, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-diff, 0)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_ema_series(closes, period):
    if len(closes) < period:
        return []
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    result = [ema]
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
        result.append(ema)
    return result


def calculate_macd(closes):
    # EMA26 needs 26 candles, then signal needs 9 more → minimum 35
    if len(closes) < 35:
        return None, None, None
    ema12 = calculate_ema_series(closes, 12)
    ema26 = calculate_ema_series(closes, 26)
    # ema12 starts at index 11, ema26 starts at index 25 → offset = 14
    offset = 26 - 12
    macd_line = [ema12[i + offset] - ema26[i] for i in range(len(ema26))]
    signal_series = calculate_ema_series(macd_line, 9)
    if not signal_series:
        return None, None, None
    macd_val = macd_line[-1]
    signal_val = signal_series[-1]
    return macd_val, signal_val, macd_val - signal_val


def detect_fvg(klines):
    """Scan last 50 candles for the most recent Fair Value Gap."""
    candles = klines[-50:]
    for i in range(len(candles) - 2, 0, -1):
        c1_high = float(candles[i - 1][2])
        c1_low  = float(candles[i - 1][3])
        c3_high = float(candles[i + 1][2])
        c3_low  = float(candles[i + 1][3])
        if c3_low > c1_high and (c3_low - c1_high) / c1_high >= 0.003:
            return ("Bullish", c1_high, c3_low)
        if c3_high < c1_low and (c1_low - c3_high) / c1_low >= 0.003:
            return ("Bearish", c3_high, c1_low)
    return None


def _fvg_price(p):
    if p >= 1000:
        return f"{p:,.0f}"
    if p >= 1:
        return f"{p:.4f}"
    return f"{p:.6f}"


def format_fvg(fvg):
    if fvg is None:
        return "-"
    direction, low, high = fvg
    return f"{direction} {_fvg_price(low)}-{_fvg_price(high)}"


def combined_signal(rsi, macd_hist):
    if rsi is None:
        return "HOLD"
    if rsi < 30 and macd_hist is not None and macd_hist > 0:
        return "STRONG BUY"
    if rsi > 70 and macd_hist is not None and macd_hist < 0:
        return "STRONG SELL"
    if rsi < 30:
        return "BUY"
    if rsi > 70:
        return "SELL"
    return "HOLD"


def format_price(price):
    price = float(price)
    if price >= 1:
        return f"${price:,.2f}"
    return f"${price:.6f}"


def format_change(change):
    change = float(change)
    sign = "+" if change >= 0 else ""
    color = "\033[92m" if change >= 0 else "\033[91m"
    reset = "\033[0m"
    return f"{color}{sign}{change:.2f}%{reset}"


def format_volume(volume, price):
    usd_volume = float(volume) * float(price)
    if usd_volume >= 1_000_000_000:
        return f"${usd_volume / 1_000_000_000:.2f}B"
    if usd_volume >= 1_000_000:
        return f"${usd_volume / 1_000_000:.2f}M"
    return f"${usd_volume:,.0f}"


GREEN = "\033[92m"
RED = "\033[91m"
RESET = "\033[0m"


def format_macd(val):
    if val is None:
        return "N/A"
    if abs(val) >= 1:
        return f"{val:+.2f}"
    if abs(val) >= 0.0001:
        return f"{val:+.6f}"
    return f"{val:+.2e}"


def format_signal(rsi, macd_hist):
    rsi_str = f"{rsi:>6.2f}" if rsi is not None else f"{'N/A':>6}"
    macd_str = f"{format_macd(macd_hist):>10}"
    signal = combined_signal(rsi, macd_hist)
    if signal in ("STRONG BUY", "BUY"):
        sig_colored = f"{GREEN}{signal:>11}{RESET}"
    elif signal in ("STRONG SELL", "SELL"):
        sig_colored = f"{RED}{signal:>11}{RESET}"
    else:
        sig_colored = f"{'HOLD':>11}"
    return f"  {rsi_str}  {macd_str}  {sig_colored}"


def send_telegram_alert(symbol, signal, rsi, macd_hist, price, fvg=None):
    key = f"{symbol}:{signal}"
    now = time.time()
    if now - _last_alert.get(key, 0) < 600:
        return
    _last_alert[key] = now

    emoji = "🟢" if "BUY" in signal else "🔴"
    coin = symbol.replace("USDT", "")
    price_str = format_price(price)
    macd_str = format_macd(macd_hist)
    message = f"{emoji} {signal} | {coin} | RSI: {rsi:.1f} | MACD: {macd_str} | Price: {price_str}"

    if fvg is not None:
        direction, low, high = fvg
        aligns = (direction == "Bullish" and "BUY" in signal) or \
                 (direction == "Bearish" and "SELL" in signal)
        fvg_tag = " ⚡ FVG ALIGNED" if aligns else ""
        message += f" | FVG: {format_fvg(fvg)}{fvg_tag}"

    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": message}).encode()
    req = urllib.request.Request(TELEGRAM_URL, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception:
        pass


def clear():
    os.system("cls" if os.name == "nt" else "clear")


def display(tickers, indicators):
    clear()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    width = 122

    print("=" * width)
    print(f"  Crypto Price Tracker        {now}")
    print("=" * width)
    print(f"  {'Coin':<8} {'Price':>12} {'24h Change':>13} {'Volume (USD)':>14}  {'RSI(14)':>6}  {'MACD Hist':>10}  {'Signal':>11}  {'FVG (1h)':>25}")
    print("-" * width)

    for t, (rsi, macd_hist, fvg) in zip(tickers, indicators):
        name = t["symbol"].replace("USDT", "")
        price = format_price(t["lastPrice"])
        change = format_change(t["priceChangePercent"])
        volume = format_volume(t["volume"], t["lastPrice"])
        signal = format_signal(rsi, macd_hist)
        fvg_str = format_fvg(fvg)
        if fvg is not None:
            direction = fvg[0]
            fvg_colored = f"{GREEN if direction == 'Bullish' else RED}{fvg_str}{RESET}"
        else:
            fvg_colored = fvg_str
        print(f"  {name:<8} {price:>12} {change:>21} {volume:>14}{signal}  {fvg_colored}")

    print("=" * width)
    print("  Refreshing every 10s  |  Press Ctrl+C to exit")
    print("=" * width)


def main():
    print("Starting crypto tracker...")
    while True:
        try:
            tickers = [fetch_ticker(s) for s in SYMBOLS]
            indicators = []
            for s in SYMBOLS:
                try:
                    indicators.append(fetch_indicators(s))
                except Exception:
                    indicators.append((None, None, None))
            for t, (rsi, macd_hist, fvg) in zip(tickers, indicators):
                signal = combined_signal(rsi, macd_hist)
                if signal != "HOLD":
                    send_telegram_alert(t["symbol"], signal, rsi, macd_hist, t["lastPrice"], fvg)
            display(tickers, indicators)
        except Exception as e:
            clear()
            print(f"Error fetching data: {e}")
        time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nExited.")
