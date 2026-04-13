# Crypto Trading Bot — Session Summary

## Architecture
- orchestrator.py — main entry point (python orchestrator.py)
- data/binance_feed.py — OHLCV candles + live ticker (Binance REST)
- data/coinglass_feed.py — Funding Rate, OI, L/S Ratio (Binance Futures endpoints, no API key needed)
- agents/signal_agent.py — score-based signal engine (see Signal Logic below)
- agents/risk_agent.py — position sizing, SL, TP1/TP2, R:R verdict
- backtest/engine.py — walk-forward, train/val/test split, overfitting check

## Signal Logic (Score-Based)

### Bull Score (max 13pts)
- RSI < 30 → +3pts (overrides +2)
- RSI < 35 → +2pts
- Bullish FVG present → +2pts
- MACD histogram rising (hist > prev bar) → +1pt
- Funding rate < -0.01% → +1pt
- Short side L/S > 55% → +1pt
- Trend == BULLISH (EMA100 > EMA300, price above both) → +1pt
- Recent bullish liquidity sweep (last 3 candles) → +2pts
- Equal lows swept → +1pt

### Bear Score (mirror, max 13pts)
- RSI > 70 → +3pts (overrides +2)
- RSI > 65 → +2pts
- Bearish FVG present → +2pts
- MACD histogram falling → +1pt
- Funding rate > +0.01% → +1pt
- Long side L/S > 65% → +1pt
- Trend == BEARISH → +1pt
- Recent bearish liquidity sweep (last 3 candles) → +2pts
- Equal highs swept → +1pt

### Decision Thresholds
- ≥ 5pts AND dominant side → STRONG BUY / STRONG SELL (triggers risk agent + paper trade)
- 3–4pts AND dominant side → BUY / SELL (shown in log, no trade)
- Tie (bull == bear) or < 3pts → HOLD

## Current State
- Mode: PAPER TRADING (no live execution)
- Symbols: BTCUSDT, ETHUSDT, SOLUSDT
- Timeframe: 1h candles
- Risk: 1% per trade on $1,000 simulated balance
- Poll interval: 300s
- PAPER_TRADE=true (default, set in .env or orchestrator.py)
- Bot running via: nohup python -u orchestrator.py >> logs/bot.log 2>&1 &  (>> appends, never overwrites)
- Paper trades collected: 8 (3W / 2L / 2CANCELLED / 1OPEN)

## Terminal Display Format
Card layout, 4 lines per symbol:
```
Cycle #X  ·  2026-04-14 02:30:33  ·  300s interval
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  BTC  HOLD  ─ NEUTRAL
  RSI:80.2  Funding:-0.005%  L/S:45/55
  Bull  3pt  [▓▓▓░░░░░░░]  FVG↑  MACD↑
  Bear  3pt  [▓▓▓░░░░░░░]  RSI>70

  SOL  STRONG SELL  ─ NEUTRAL
  RSI:74.1  Funding:+0.010%  L/S:68/32
  Bull  3pt  [▓▓▓░░░░░░░]  FVG↑  MACD↑
  Bear  6pt  [▓▓▓▓▓▓░░░░]  RSI>70  Longs68%  Sweep↓
  📋 SHORT $86 → now $87  |  -0.38%  |  SL:$88  |  TP1:$83

  ── next cycle at 02:45:56 ──
```

## Next Steps
1. Collect 20+ paper trades
2. Analyze WIN/LOSS ratio
3. Fix backtest context window (needs 350+ candles for EMA300)
4. If results good → enable execution agent (Binance Testnet first)
5. Eventually add: Liquidity Heatmap (CoinGlass paid), Order Flow

## Known Issues
- Backtest produces too few trades (EMA300 needs 350 candle context window, currently 200)
- Test set is SACRED — never run manually until strategy is finalized

## Bug Fixes Applied
- Cooldown now triggers immediately when a paper trade is logged, regardless of Telegram success — prevents duplicate trade entries if Telegram is down

## Key Decisions Made
- 1h candles (not 4h) — strategy already built around it
- Wick-based FVG detection — fewer but higher quality signals
- Score-based signal engine — MACD alone is not sufficient; funding rate + L/S included
- Binance Futures for derivatives data — free, no API key needed
- Paper trade before live — collecting real signal data
- Test set is sacred — never touch until strategy finalized
