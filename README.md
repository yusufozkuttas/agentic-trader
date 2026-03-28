# Crypto Tracker

A terminal-based cryptocurrency tracker with technical analysis indicators and Telegram alerts.

## Features

- **Live price feed** — Fetches real-time data from Binance for BTC, ETH, and IOTX
- **RSI(14)** — Relative Strength Index calculated from 1h candles
- **MACD** — MACD histogram for momentum direction
- **FVG detection** — Scans recent candles for Fair Value Gaps (bullish/bearish)
- **Signal engine** — Generates BUY / SELL / STRONG BUY / STRONG SELL / HOLD signals
- **Telegram alerts** — Sends a message when a non-HOLD signal is detected (rate-limited to once per 10 minutes per symbol)
- **Auto-refresh** — Updates every 10 seconds

## Files

| File | Description |
|------|-------------|
| `crypto_tracker.py` | Main tracker — live terminal dashboard + Telegram alerts |
| `test_fvg.py` | Standalone FVG scanner for BTCUSDT (wick vs body mode comparison) |

## Requirements

- Python 3.8+
- No third-party packages — uses only the standard library

## Usage

### Run the live tracker

```bash
python crypto_tracker.py
```

### Run the FVG scanner

```bash
python test_fvg.py
```

The FVG scanner fetches the last 200 hourly candles for BTCUSDT and compares two detection modes:
- **Mode 1 (Wick)** — gap measured using candle highs/lows
- **Mode 2 (Body)** — gap measured using open/close prices only

## Signal Logic

| Condition | Signal |
|-----------|--------|
| RSI < 30 and MACD Hist > 0 | STRONG BUY |
| RSI > 70 and MACD Hist < 0 | STRONG SELL |
| RSI < 30 | BUY |
| RSI > 70 | SELL |
| Otherwise | HOLD |

## Configuration

Before running, update the Telegram credentials in `crypto_tracker.py`:

```python
TELEGRAM_BOT_TOKEN = "your_bot_token"
TELEGRAM_CHAT_ID   = "your_chat_id"
```

To track different coins, edit the `SYMBOLS` list:

```python
SYMBOLS = ["BTCUSDT", "ETHUSDT", "IOTXUSDT"]
```
