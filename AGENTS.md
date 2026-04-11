# Agent Architecture

## Orchestrator (orchestrator.py)
Main entry point. Coordinates all agents every 5 minutes.
Loads config from .env, sends Telegram alerts, logs paper trades.
Run: python orchestrator.py

## Signal Agent (agents/signal_agent.py)
Analyzes market conditions and produces a trade signal.
Inputs: OHLCV candles (1h), symbol
Outputs: RSI, MACD, FVG, Equal H/L, Liquidity Sweep, Order Block, trend filter, combined signal
Signals: STRONG_BUY / STRONG_SELL / WEAK_BUY / WEAK_SELL / HOLD

## Risk Agent (agents/risk_agent.py)
Takes signal and calculates trade parameters.
Inputs: signal dict, entry price, account balance
Outputs: stop_loss, TP1 (2R), TP2 (3R), position_size_usd, risk_usd, verdict
Verdict: TAKE_TRADE or SKIP (minimum R:R 2.0)

## Backtest Engine (backtest/engine.py)
Validates strategy on historical data before going live.
Split: 60% train / 20% validation / 20% test (sacred)
Method: walk-forward with overfitting detection
Run: python backtest/engine.py BTCUSDT

## Data: Binance Feed (data/binance_feed.py)
Fetches OHLCV candles from Binance REST API.
Supports pagination up to 3000 x 1h candles.

## Data: Derivatives Feed (data/coinglass_feed.py)
Fetches funding rate, open interest, long/short ratio, taker buy/sell ratio.
Source: Binance Futures API (free, no API key needed).
