"""
orchestrator.py
===============
Main entry point for the trading signal bot.

On startup
----------
  1. Loads config from .env
  2. Runs walk-forward backtest (train + val) and prints health report
  3. Starts the 5-minute polling loop

Each cycle (every 5 minutes)
-----------------------------
  For each symbol:
    1. Fetch 200 x 1h candles  (binance_feed)
    2. Run all technical signals (signal_agent)
    3. Fetch market context     (coinglass_feed: funding, OI, L/S ratio)
    4. If signal is STRONG BUY or STRONG SELL:
         → run risk_agent to get trade plan
         → if verdict is TAKE_TRADE and cooldown has passed:
              → send Telegram alert

Telegram alert format
---------------------
  🟢 STRONG BUY — BTCUSDT
  Entry:     $72,932
  Stop loss: $70,648  (-3.1%)
  TP1:       $77,500  (+2R)
  TP2:       $79,783  (+3R)
  RSI: 57 | MACD: bullish | Sweep: YES
  Funding: -0.007% | L/S: 42/58

Config (.env)
-------------
  TELEGRAM_BOT_TOKEN   — bot token from BotFather
  TELEGRAM_CHAT_ID     — destination chat/channel ID
  SYMBOLS              — comma-separated, e.g. BTCUSDT,ETHUSDT,IOTXUSDT
  ACCOUNT_BALANCE      — paper account size in USD (default 1000)
  RISK_PCT             — % of account to risk per trade (default 1.0)
  POLL_INTERVAL        — seconds between cycles (default 300)
  ALERT_COOLDOWN       — seconds before re-alerting same coin (default 600)
  BACKTEST_SYMBOL      — symbol to backtest on startup (default first in SYMBOLS)
"""

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime

# ---------------------------------------------------------------------------
# .env loader (stdlib — no external dependencies)
# ---------------------------------------------------------------------------

def _load_dotenv(path: str = ".env"):
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


_load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_raw_symbols = os.environ.get("SYMBOLS", "BTCUSDT,ETHUSDT,IOTXUSDT")
SYMBOLS = [s.strip().upper() for s in _raw_symbols.split(",") if s.strip()]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "")
TELEGRAM_URL       = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

ACCOUNT_BALANCE  = float(os.environ.get("ACCOUNT_BALANCE",  "1000"))
RISK_PCT         = float(os.environ.get("RISK_PCT",         "1.0"))
POLL_INTERVAL    = int(os.environ.get("POLL_INTERVAL",     "300"))   # seconds
ALERT_COOLDOWN   = int(os.environ.get("ALERT_COOLDOWN",    "600"))   # seconds
BACKTEST_SYMBOL  = os.environ.get("BACKTEST_SYMBOL", SYMBOLS[0])

PAPER_TRADE           = os.environ.get("PAPER_TRADE", "true").lower() in ("1", "true", "yes")
PAPER_TRADES_FILE     = "paper_trades.json"
RUN_STARTUP_BACKTEST  = False  # 500-candle startup backtest is too few to be useful

# Signals that trigger a risk calculation
ACTIONABLE_SIGNALS = {"STRONG BUY", "STRONG SELL"}

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

_last_alert: dict[str, float] = {}   # symbol → last alert unix timestamp


# ---------------------------------------------------------------------------
# Agent imports (deferred to avoid import errors at module level)
# ---------------------------------------------------------------------------

def _import_agents():
    """Import all agents lazily so import errors surface clearly."""
    from data.binance_feed   import fetch_ohlcv, fetch_ticker
    from data.coinglass_feed import fetch_market_snapshot
    from agents.signal_agent import run_all
    from agents.risk_agent   import from_signal_agent
    return fetch_ohlcv, fetch_ticker, fetch_market_snapshot, run_all, from_signal_agent


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _send_telegram(message: str) -> bool:
    """
    Send a plain-text message to TELEGRAM_CHAT_ID.
    Returns True on success, False on failure (non-fatal).
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [Telegram] Credentials not set — skipping alert.")
        return False
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text":    message,
    }).encode()
    req = urllib.request.Request(TELEGRAM_URL, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as exc:
        print(f"  [Telegram] Send failed: {exc}")
        return False


def _can_alert(symbol: str) -> bool:
    """Return True if enough time has passed since the last alert for this symbol."""
    return time.time() - _last_alert.get(symbol, 0) >= ALERT_COOLDOWN


def _mark_alerted(symbol: str):
    _last_alert[symbol] = time.time()


# ---------------------------------------------------------------------------
# Paper trading
# ---------------------------------------------------------------------------

def _load_paper_trades() -> list:
    if not os.path.isfile(PAPER_TRADES_FILE):
        return []
    with open(PAPER_TRADES_FILE, encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError:
            return []


def _save_paper_trades(trades: list):
    with open(PAPER_TRADES_FILE, "w", encoding="utf-8") as fh:
        json.dump(trades, fh, indent=2)


def _log_paper_trade(symbol: str, plan: dict):
    trades = _load_paper_trades()
    trades.append({
        "id":            f"{symbol}_{int(time.time())}",
        "timestamp":     datetime.utcnow().isoformat(),
        "symbol":        symbol,
        "direction":     plan["direction"],
        "entry":         plan["entry"],
        "stop_loss":     plan["stop_loss"],
        "tp1":           plan["tp1"],
        "tp2":           plan["tp2"],
        "risk_usd":      plan["risk_usd"],
        "outcome":       None,
        "outcome_price": None,
        "outcome_time":  None,
    })
    _save_paper_trades(trades)
    print(f"  [Paper] Logged {plan['direction']} {symbol}  "
          f"entry=${plan['entry']:,.0f}  "
          f"sl=${plan['stop_loss']:,.0f}  "
          f"tp1=${plan['tp1']:,.0f}")


def _check_open_paper_trades(symbol: str, ohlcv: list):
    """
    Scan the latest OHLCV candles against every open paper trade for this symbol.
    Resolves WIN (price hits TP1) or LOSS (price hits SL), first hit wins.
    Updates paper_trades.json in-place.
    """
    trades = _load_paper_trades()
    open_trades = [t for t in trades if t["symbol"] == symbol and t["outcome"] is None]
    if not open_trades:
        return

    updated = False
    for trade in open_trades:
        opened_dt = datetime.fromisoformat(trade["timestamp"])
        direction = trade["direction"]
        tp1 = trade["tp1"]
        sl  = trade["stop_loss"]

        for candle in ohlcv:
            if candle["open_time"] <= opened_dt:
                continue
            if direction == "LONG":
                if candle["high"] >= tp1:
                    trade["outcome"]       = "WIN"
                    trade["outcome_price"] = tp1
                    trade["outcome_time"]  = candle["open_time"].isoformat()
                    break
                if candle["low"] <= sl:
                    trade["outcome"]       = "LOSS"
                    trade["outcome_price"] = sl
                    trade["outcome_time"]  = candle["open_time"].isoformat()
                    break
            else:  # SHORT
                if candle["low"] <= tp1:
                    trade["outcome"]       = "WIN"
                    trade["outcome_price"] = tp1
                    trade["outcome_time"]  = candle["open_time"].isoformat()
                    break
                if candle["high"] >= sl:
                    trade["outcome"]       = "LOSS"
                    trade["outcome_price"] = sl
                    trade["outcome_time"]  = candle["open_time"].isoformat()
                    break

        if trade["outcome"]:
            updated = True
            tag = f"{_GREEN}✓ WIN{_RESET}" if trade["outcome"] == "WIN" else f"{_RED}✗ LOSS{_RESET}"
            print(f"  [Paper] {tag}  {symbol} {direction}  "
                  f"@ ${trade['outcome_price']:,.0f}  "
                  f"({trade['outcome_time']})")

    if updated:
        _save_paper_trades(trades)


# ---------------------------------------------------------------------------
# Alert message builder
# ---------------------------------------------------------------------------

def _build_alert(symbol: str, plan: dict, sig: dict, ctx: dict) -> str:
    """
    Format the Telegram alert message.

    Parameters
    ----------
    plan : output of risk_agent.from_signal_agent()
    sig  : output of signal_agent.run_all()
    ctx  : output of coinglass_feed.fetch_market_snapshot()
    """
    direction = plan["direction"]
    emoji     = "🟢" if direction == "LONG" else "🔴"
    signal    = plan["signal"]
    coin      = symbol.replace("USDT", "")

    entry    = plan["entry"]
    sl       = plan["stop_loss"]
    tp1      = plan["tp1"]
    tp2      = plan["tp2"]
    sl_pct   = -plan["stop_distance_pct"] if direction == "LONG" else plan["stop_distance_pct"]
    tp1_r    = plan["risk_reward"]

    # TP2 R-multiple
    tp2_dist = abs(tp2 - entry)
    sl_dist  = plan["stop_distance"]
    tp2_r    = tp2_dist / sl_dist if sl_dist else 0

    # Signal context
    rsi_str  = f"{sig['rsi']:.0f}" if sig["rsi"] is not None else "N/A"
    macd_str = "bullish" if (sig["macd_hist"] or 0) > 0 else "bearish"
    sweep_str = "YES" if sig.get("sweeps_recent") else "NO"

    # Market context
    summary      = ctx.get("summary", {})
    funding      = summary.get("latest_funding_rate")
    long_pct     = summary.get("latest_long_pct")
    short_pct    = summary.get("latest_short_pct")
    funding_str  = f"{funding:+.3f}%" if funding is not None else "N/A"
    ls_str       = (f"{long_pct:.0f}/{short_pct:.0f}"
                    if long_pct is not None else "N/A")

    lines = [
        *(["📋 PAPER TRADE"] if PAPER_TRADE else []),
        f"{emoji} {signal} — {symbol}",
        f"Entry:     ${entry:>10,.0f}",
        f"Stop loss: ${sl:>10,.0f}  ({sl_pct:+.1f}%)",
        f"TP1:       ${tp1:>10,.0f}  (+{tp1_r:.0f}R)",
        f"TP2:       ${tp2:>10,.0f}  (+{tp2_r:.0f}R)",
        f"",
        f"RSI: {rsi_str} | MACD: {macd_str} | Sweep: {sweep_str}",
        f"Funding: {funding_str} | L/S: {ls_str}",
    ]
    if plan.get("stop_source"):
        lines.append(f"Stop basis: {plan['stop_source']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-symbol processing
# ---------------------------------------------------------------------------

def _process_symbol(
    symbol: str,
    fetch_ohlcv, fetch_ticker, fetch_market_snapshot, run_all, from_signal_agent,
) -> dict:
    """
    Run the full pipeline for one symbol.
    Returns a status dict for terminal display.
    """
    status = {
        "symbol":  symbol,
        "signal":  "ERROR",
        "verdict": None,
        "alerted": False,
        "error":   None,
    }

    try:
        # 1. Candles + signals
        ohlcv  = fetch_ohlcv(symbol, "1h", 200)
        ticker = fetch_ticker(symbol)
        entry  = float(ticker["lastPrice"])
        sig    = run_all(ohlcv)

        # 1a. Check open paper trades against latest candles
        if PAPER_TRADE:
            _check_open_paper_trades(symbol, ohlcv)

        status["signal"] = sig["signal"]
        status["rsi"]    = sig["rsi"]
        status["hist"]   = sig["macd_hist"]

        # 2. Market context (non-fatal if it fails)
        try:
            ctx = fetch_market_snapshot(symbol)
        except Exception as ctx_exc:
            print(f"  [ctx] {symbol} market snapshot failed: {ctx_exc}")
            ctx = {"summary": {}}

        status["funding"] = ctx.get("summary", {}).get("latest_funding_rate")
        status["ls"]      = (
            ctx.get("summary", {}).get("latest_long_pct"),
            ctx.get("summary", {}).get("latest_short_pct"),
        )

        # 3. Risk agent — only for strong signals
        if sig["signal"] in ACTIONABLE_SIGNALS:
            plan = from_signal_agent(
                sig, entry,
                account_balance=ACCOUNT_BALANCE,
                risk_pct=RISK_PCT,
            )
            status["verdict"] = plan["verdict"]

            # 4. Paper log + Telegram alert
            if plan["verdict"] == "TAKE_TRADE" and _can_alert(symbol):
                if PAPER_TRADE:
                    _log_paper_trade(symbol, plan)
                _mark_alerted(symbol)   # always set cooldown — prevents duplicate logs if Telegram is down
                msg     = _build_alert(symbol, plan, sig, ctx)
                success = _send_telegram(msg)
                if success:
                    status["alerted"] = True

    except Exception as exc:
        status["error"] = str(exc)

    return status


# ---------------------------------------------------------------------------
# Terminal display helpers
# ---------------------------------------------------------------------------

_GREEN  = "\033[92m"
_RED    = "\033[91m"
_YELLOW = "\033[93m"
_CYAN   = "\033[96m"
_DIM    = "\033[2m"
_RESET  = "\033[0m"

def _fmt_signal(signal: str) -> str:
    if "STRONG BUY" in signal:
        return f"{_GREEN}{signal:<11}{_RESET}"
    if "BUY" in signal:
        return f"{_GREEN}{signal:<11}{_RESET}"
    if "STRONG SELL" in signal:
        return f"{_RED}{signal:<11}{_RESET}"
    if "SELL" in signal:
        return f"{_RED}{signal:<11}{_RESET}"
    return f"{_DIM}{signal:<11}{_RESET}"

def _fmt_verdict(verdict) -> str:
    if verdict == "TAKE_TRADE":
        return f"{_GREEN}TAKE_TRADE{_RESET}"
    if verdict == "SKIP":
        return f"{_DIM}SKIP{_RESET}"
    return f"{_DIM}—{_RESET}"

def _print_cycle_header(cycle: int):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*70}")
    print(f"  Cycle #{cycle:<4}  {now}  |  {len(SYMBOLS)} symbols  |  "
          f"interval={POLL_INTERVAL}s")
    print(f"{'='*70}")
    print(f"  {'Symbol':<10} {'Signal':<13} {'RSI':>6}  {'MACD':>9}  "
          f"{'Verdict':<12}  {'Funding':>9}  {'L/S':>8}")
    print(f"  {'-'*66}")

def _print_status(st: dict):
    sym     = st["symbol"]
    signal  = _fmt_signal(st.get("signal", "ERROR"))
    rsi     = f"{st.get('rsi', 0) or 0:>6.1f}"
    hist    = st.get("hist") or 0
    macd    = f"{hist:>+9.4f}" if isinstance(hist, float) else f"{'N/A':>9}"
    verdict = _fmt_verdict(st.get("verdict"))
    funding = st.get("funding")
    ls      = st.get("ls", (None, None))
    fund_s  = f"{funding:>+9.3f}%" if funding is not None else f"{'N/A':>9}"
    ls_s    = (f"{ls[0]:.0f}/{ls[1]:.0f}" if ls[0] is not None else "N/A")

    alert_tag = f"  {_GREEN}✓ ALERT SENT{_RESET}" if st.get("alerted") else ""
    err_tag   = f"  {_RED}ERR: {st.get('error','')[:40]}{_RESET}" if st.get("error") else ""
    print(f"  {sym:<10} {signal:<13} {rsi}  {macd}  {verdict:<12}  "
          f"{fund_s}  {ls_s:>8}{alert_tag}{err_tag}")


# ---------------------------------------------------------------------------
# Startup backtest
# ---------------------------------------------------------------------------

def _run_startup_backtest():
    print(f"\n{'='*70}")
    print("  STARTUP BACKTEST — strategy health check")
    print(f"{'='*70}")
    print(f"  Symbol: {BACKTEST_SYMBOL}  |  500 x 1h candles  "
          f"|  train=60%  val=20%  test=20% (sacred)")

    try:
        from backtest.engine import BacktestEngine
        engine = BacktestEngine(
            symbol          = BACKTEST_SYMBOL,
            interval        = "1h",
            total_candles   = 500,
            account_balance = ACCOUNT_BALANCE,
            risk_pct        = RISK_PCT,
            min_rr          = 2.0,
        )
        engine.fetch_and_split()
        engine.run()
        engine.report()
    except Exception as exc:
        print(f"\n  {_YELLOW}Backtest failed: {exc}{_RESET}")
        print("  Continuing to live loop...\n")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    print(f"\n{_CYAN}{'='*70}")
    print("  Crypto Signal Bot — starting up")
    print(f"  Symbols   : {', '.join(SYMBOLS)}")
    print(f"  Balance   : ${ACCOUNT_BALANCE:,.0f}  |  Risk: {RISK_PCT}%/trade")
    print(f"  Interval  : {POLL_INTERVAL}s  |  Cooldown: {ALERT_COOLDOWN}s")
    print(f"  Telegram  : {'configured' if TELEGRAM_BOT_TOKEN else 'NOT SET'}")
    print(f"  Mode      : {'📋 PAPER TRADING (no live execution)' if PAPER_TRADE else '🔴 LIVE TRADING'}")
    print(f"{'='*70}{_RESET}\n")

    # Startup backtest
    if RUN_STARTUP_BACKTEST:
        _run_startup_backtest()

    # Import agents once
    fetch_ohlcv, fetch_ticker, fetch_market_snapshot, run_all, from_signal_agent = (
        _import_agents()
    )

    print(f"\n  Starting live loop — polling every {POLL_INTERVAL}s")
    print("  Press Ctrl+C to exit\n")

    cycle = 0
    while True:
        cycle += 1
        _print_cycle_header(cycle)

        for symbol in SYMBOLS:
            try:
                st = _process_symbol(
                    symbol,
                    fetch_ohlcv, fetch_ticker, fetch_market_snapshot,
                    run_all, from_signal_agent,
                )
            except Exception as exc:
                st = {"symbol": symbol, "signal": "ERROR",
                      "verdict": None, "alerted": False, "error": str(exc)}
            _print_status(st)

        # Cooldown status footer
        now = time.time()
        cooling = [
            s for s in SYMBOLS
            if now - _last_alert.get(s, 0) < ALERT_COOLDOWN
        ]
        if cooling:
            remaining = {
                s: int(ALERT_COOLDOWN - (now - _last_alert[s]))
                for s in cooling
            }
            parts = "  |  ".join(f"{s} {r}s" for s, r in remaining.items())
            print(f"\n  {_DIM}Cooldown active: {parts}{_RESET}")

        print(f"\n  Next cycle in {POLL_INTERVAL}s  "
              f"({datetime.fromtimestamp(now + POLL_INTERVAL).strftime('%H:%M:%S')})")

        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n\n{_DIM}  Interrupted by user. Exiting.{_RESET}\n")
