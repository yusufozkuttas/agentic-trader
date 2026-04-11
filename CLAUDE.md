# Crypto Trading Bot — Session Summary

## Architecture
- orchestrator.py — main entry point (python orchestrator.py)
- data/binance_feed.py — OHLCV + liquidation websocket
- data/coinglass_feed.py — Funding Rate, OI, L/S Ratio (Binance Futures endpoints, no API key needed)
- agents/signal_agent.py — RSI, MACD, FVG, Equal H/L, Liquidity Sweep, Order Block, Trend Filter (EMA100/EMA300)
- agents/risk_agent.py — position sizing, SL, TP1/TP2, R:R verdict
- backtest/engine.py — walk-forward, train/val/test split, overfitting check

## Current State
- Mode: PAPER TRADING (no live execution)
- Symbols: BTCUSDT, ETHUSDT, SOLUSDT
- Timeframe: 1h candles
- Risk: 1% per trade on $1,000 simulated balance
- Poll interval: 300s
- Bot running via: nohup python -u orchestrator.py > logs/bot.log 2>&1 &

## Next Steps
1. Collect 20+ paper trades (1-2 weeks)
2. Analyze WIN/LOSS ratio
3. Fix backtest context window (needs 350+ candles for EMA300)
4. If results good → enable execution agent (Binance Testnet first)
5. Eventually add: Liquidity Heatmap (CoinGlass paid), Order Flow

## Known Issues
- Backtest produces too few trades (EMA300 needs 350 candle context window, currently 200)
- Test set is SACRED — never run manually until strategy is finalized

## Key Decisions Made
- 1h candles (not 4h) — strategy already built around it
- Wick-based FVG detection — fewer but higher quality signals
- Trend filter EMA100/EMA300 — blocks counter-trend trades
- Binance Futures for derivatives data — free, no API key needed
- Paper trade before live — collecting real signal data
