# Crypto Trading Bot

A modular, paper-trading signal bot for crypto futures. Runs a polling loop every 5 minutes, evaluates technical setups across multiple symbols, sizes positions via a risk model, and sends Telegram alerts when a high-quality trade is found.

> **Current mode: PAPER TRADING** — no live order execution.

---

## Architecture

```
orchestrator.py          — main entry point & polling loop
│
├── data/
│   ├── binance_feed.py  — OHLCV candles + live ticker (Binance REST)
│   └── coinglass_feed.py — Funding rate, Open Interest, Long/Short ratio
│                           (Binance Futures endpoints, no API key required)
│
├── agents/
│   ├── signal_agent.py  — technical signal engine (see Signal Logic below)
│   └── risk_agent.py    — position sizing, SL, TP1/TP2, R:R verdict
│
└── backtest/
    └── engine.py        — walk-forward backtest (train/val/test split)
```

---

## Signal Logic

`signal_agent.py` evaluates the following on every 1h candle set:

| Indicator | Detail |
|---|---|
| RSI (14) | Overbought / oversold baseline |
| MACD | Histogram direction for momentum confirmation |
| Fair Value Gap (FVG) | Wick-based detection — fewer but higher-quality gaps |
| Equal Highs / Equal Lows | Liquidity pool identification |
| Liquidity Sweep | Recent sweep of a prior high/low |
| Order Block | Last opposing candle before a strong move |
| Trend Filter | EMA100 & EMA300 — blocks all counter-trend entries |

A `STRONG BUY` or `STRONG SELL` fires only when multiple conditions align **in the direction of the trend**.

---

## Risk Model

`risk_agent.py` runs after a strong signal and returns a full trade plan:

- **Stop loss** — derived from the signal structure (FVG edge, order block, swing)
- **TP1** — 2R from entry
- **TP2** — 3R from entry
- **Position size** — `(account_balance × risk_pct) / stop_distance`
- **Verdict** — `TAKE_TRADE` or `SKIP` (if R:R < 2.0)

---

## Paper Trading

When `PAPER_TRADE=true` (default), every `TAKE_TRADE` verdict is logged to `paper_trades.json`:

```json
[
  {
    "id": "BTCUSDT_1712345678",
    "timestamp": "2026-04-11T18:00:00",
    "symbol": "BTCUSDT",
    "direction": "LONG",
    "entry": 82500,
    "stop_loss": 80200,
    "tp1": 87100,
    "tp2": 89400,
    "risk_usd": 10.0,
    "outcome": null,
    "outcome_price": null,
    "outcome_time": null
  }
]
```

Outcomes (`WIN` / `LOSS`) are resolved automatically each cycle by scanning subsequent OHLCV candles.

---

## Telegram Alert Format

```
📋 PAPER TRADE
🟢 STRONG BUY — BTCUSDT
Entry:     $   82,500
Stop loss: $   80,200  (-2.8%)
TP1:       $   87,100  (+2R)
TP2:       $   89,400  (+3R)

RSI: 38 | MACD: bullish | Sweep: YES
Funding: -0.007% | L/S: 43/57
Stop basis: FVG lower edge
```

---

## Setup

### 1. Clone & configure

```bash
git clone <repo-url>
cd CLAUDE-Project
cp .env.example .env
# Edit .env with your credentials
```

### 2. `.env` reference

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
COINGLASS_API_KEY=your_coinglass_api_key_here   # optional — free endpoints used by default
SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT
ACCOUNT_BALANCE=1000
RISK_PCT=1.0
POLL_INTERVAL=300
ALERT_COOLDOWN=600
BACKTEST_SYMBOL=BTCUSDT
```

### 3. Run

**Foreground:**
```bash
python orchestrator.py
```

**Background (persistent):**
```bash
nohup python -u orchestrator.py > logs/bot.log 2>&1 &
```

**Follow logs:**
```bash
tail -f logs/bot.log
```

---

## Requirements

- Python 3.8+
- No third-party packages — standard library only

---

## Roadmap

- [ ] Collect 20+ paper trades and analyze WIN/LOSS ratio
- [ ] Fix backtest candle context window (EMA300 needs 350+ candles)
- [ ] Enable execution agent on Binance Testnet
- [ ] Add Liquidity Heatmap (CoinGlass paid tier)
- [ ] Add Order Flow analysis
