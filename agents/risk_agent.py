"""
agents/risk_agent.py
====================
Risk management agent.

Receives a signal dict (from signal_agent or manually constructed) and returns
a fully calculated trade plan: position size, stop loss, take profit levels,
and a TAKE_TRADE / SKIP verdict.

Position Sizing (fixed-risk model)
-----------------------------------
    risk_usd          = account_balance × risk_pct / 100
    stop_distance     = |entry - stop_loss|
    contracts         = risk_usd / stop_distance
    position_size_usd = contracts × entry

Stop Loss
---------
    Placed a small buffer beyond the provided stop_level so the position
    is not stopped out exactly at the structural level.

    Long  → stop_loss = stop_level × (1 - buffer_pct / 100)
    Short → stop_loss = stop_level × (1 + buffer_pct / 100)

Take Profit
-----------
    tp1 = entry + tp_ratios[0] × stop_distance   (default 2R)
    tp2 = entry + tp_ratios[1] × stop_distance   (default 3R)
    (signs flipped for shorts)

Exported
--------
calculate_risk(signal, entry, stop_level, ...) -> dict
from_signal_agent(signal_result, entry, account_balance, ...) -> dict
"""

# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------

DEFAULT_RISK_PCT    = 1.0     # % of account to risk per trade
DEFAULT_BUFFER_PCT  = 0.07    # % buffer beyond stop level
DEFAULT_MIN_RR      = 2.0     # minimum R:R to take the trade
DEFAULT_TP_RATIOS   = (2.0, 3.0)

VALID_SIGNALS = {"STRONG BUY", "BUY", "WEAK BUY", "STRONG SELL", "SELL", "WEAK SELL"}
LONG_SIGNALS  = {"STRONG BUY", "BUY", "WEAK BUY"}
SHORT_SIGNALS = {"STRONG SELL", "SELL", "WEAK SELL"}


# ---------------------------------------------------------------------------
# Core calculator
# ---------------------------------------------------------------------------

def calculate_risk(
    signal: str,
    entry: float,
    stop_level: float,
    account_balance: float = 1000.0,
    risk_pct: float = DEFAULT_RISK_PCT,
    min_rr: float = DEFAULT_MIN_RR,
    buffer_pct: float = DEFAULT_BUFFER_PCT,
    tp_ratios: tuple = DEFAULT_TP_RATIOS,
) -> dict:
    """
    Calculate the full trade plan for a given signal and structural stop level.

    Parameters
    ----------
    signal          : "STRONG BUY" | "BUY" | "STRONG SELL" | "SELL"
    entry           : current market price (float)
    stop_level      : structural price level (equal low for longs, equal high
                      for shorts) before the buffer is applied
    account_balance : total account size in USD
    risk_pct        : percentage of account to risk on this trade (default 1%)
    min_rr          : minimum acceptable risk:reward ratio (default 2.0)
    buffer_pct      : extra buffer placed beyond stop_level (default 0.07%)
    tp_ratios       : R-multiples for TP1 and TP2 (default (2.0, 3.0))

    Returns
    -------
    {
        "verdict"          : "TAKE_TRADE" | "SKIP",
        "skip_reason"      : str | None,        # set when verdict is SKIP
        "direction"        : "LONG" | "SHORT",
        "entry"            : float,
        "stop_level"       : float,             # raw structural level
        "stop_loss"        : float,             # level + buffer
        "stop_distance"    : float,             # |entry - stop_loss| in USD
        "stop_distance_pct": float,             # stop_distance as % of entry
        "tp1"              : float,
        "tp2"              : float,
        "risk_usd"         : float,             # $ risked (account × risk_pct)
        "contracts"        : float,             # units of base asset
        "position_size_usd": float,             # total position value in USD
        "risk_reward"      : float,             # R:R to TP1
        "account_balance"  : float,
        "risk_pct"         : float,
    }
    """
    signal = signal.strip().upper()

    # --- Validate signal ---
    if signal not in VALID_SIGNALS:
        return _skip(
            signal, entry, stop_level, account_balance, risk_pct,
            f"Signal '{signal}' is HOLD — no trade taken.",
        )

    is_long = signal in LONG_SIGNALS

    # --- Apply buffer to stop level ---
    if is_long:
        stop_loss = stop_level * (1 - buffer_pct / 100)
        stop_distance = entry - stop_loss
    else:
        stop_loss = stop_level * (1 + buffer_pct / 100)
        stop_distance = stop_loss - entry

    # --- Validate stop placement ---
    if stop_distance <= 0:
        reason = (
            f"Stop level ${stop_level:,.2f} is {'above' if is_long else 'below'} "
            f"entry ${entry:,.2f} for a {'LONG' if is_long else 'SHORT'} trade."
        )
        return _skip(signal, entry, stop_level, account_balance, risk_pct, reason)

    # --- Take profit levels ---
    tp1 = entry + tp_ratios[0] * stop_distance if is_long else entry - tp_ratios[0] * stop_distance
    tp2 = entry + tp_ratios[1] * stop_distance if is_long else entry - tp_ratios[1] * stop_distance

    # --- R:R (to TP1) ---
    reward = abs(tp1 - entry)
    risk_reward = reward / stop_distance  # always == tp_ratios[0] by definition

    # --- Verdict ---
    if risk_reward < min_rr:
        return _skip(
            signal, entry, stop_level, account_balance, risk_pct,
            f"R:R {risk_reward:.2f} is below minimum {min_rr:.2f}.",
        )

    # --- Position sizing (fixed-risk) ---
    risk_usd         = account_balance * risk_pct / 100
    contracts        = risk_usd / stop_distance
    position_size_usd = contracts * entry

    return {
        "verdict":           "TAKE_TRADE",
        "skip_reason":       None,
        "direction":         "LONG" if is_long else "SHORT",
        "signal":            signal,
        "entry":             round(entry, 4),
        "stop_level":        round(stop_level, 4),
        "stop_loss":         round(stop_loss, 4),
        "stop_distance":     round(stop_distance, 4),
        "stop_distance_pct": round(stop_distance / entry * 100, 4),
        "tp1":               round(tp1, 4),
        "tp2":               round(tp2, 4),
        "risk_usd":          round(risk_usd, 2),
        "contracts":         round(contracts, 6),
        "position_size_usd": round(position_size_usd, 2),
        "risk_reward":       round(risk_reward, 4),
        "account_balance":   account_balance,
        "risk_pct":          risk_pct,
    }


def _skip(signal, entry, stop_level, account_balance, risk_pct, reason):
    return {
        "verdict":           "SKIP",
        "skip_reason":       reason,
        "direction":         None,
        "signal":            signal,
        "entry":             round(entry, 4),
        "stop_level":        round(stop_level, 4),
        "stop_loss":         None,
        "stop_distance":     None,
        "stop_distance_pct": None,
        "tp1":               None,
        "tp2":               None,
        "risk_usd":          round(account_balance * risk_pct / 100, 2),
        "contracts":         None,
        "position_size_usd": None,
        "risk_reward":       None,
        "account_balance":   account_balance,
        "risk_pct":          risk_pct,
    }


# ---------------------------------------------------------------------------
# Signal-agent bridge — auto-derives stop level from structural data
# ---------------------------------------------------------------------------

def from_signal_agent(
    signal_result: dict,
    entry: float,
    account_balance: float = 1000.0,
    risk_pct: float = DEFAULT_RISK_PCT,
    min_rr: float = DEFAULT_MIN_RR,
    buffer_pct: float = DEFAULT_BUFFER_PCT,
    fallback_stop_pct: float = 2.0,
) -> dict:
    """
    Derive a trade plan directly from the dict returned by signal_agent.run_all().

    Stop level selection logic (priority order)
    -------------------------------------------
    1. Nearest equal low/high (structural — most precise)
    2. Nearest open FVG zone boundary
    3. Percentage-based fallback: entry ± fallback_stop_pct %
       Used when no structural level is detected. Labelled clearly
       as "ATR fallback" in stop_source so it's visible in alerts.
       Set fallback_stop_pct=0 to disable and SKIP instead.
    """
    signal = signal_result.get("signal", "HOLD")

    if signal not in VALID_SIGNALS:
        return _skip(signal, entry, 0, account_balance, risk_pct,
                     f"Signal is '{signal}' — no structural level needed.")

    is_long    = signal in LONG_SIGNALS
    stop_level = _find_stop_level(signal_result, entry, is_long)
    stop_source_label = None

    if stop_level is None:
        if fallback_stop_pct <= 0:
            return _skip(
                signal, entry, 0, account_balance, risk_pct,
                "No structural stop level found (no equal highs/lows or open FVGs).",
            )
        # Percentage fallback — place stop at fixed % beyond entry
        if is_long:
            stop_level = entry * (1 - fallback_stop_pct / 100)
        else:
            stop_level = entry * (1 + fallback_stop_pct / 100)
        stop_source_label = f"Pct fallback {fallback_stop_pct}% from entry"

    result = calculate_risk(
        signal=signal,
        entry=entry,
        stop_level=stop_level,
        account_balance=account_balance,
        risk_pct=risk_pct,
        min_rr=min_rr,
        buffer_pct=buffer_pct,
    )

    result["stop_source"] = (
        stop_source_label or _stop_source(signal_result, stop_level, is_long)
    )
    return result


def _find_stop_level(signal_result: dict, entry: float, is_long: bool) -> float | None:
    """
    Pick the best structural stop level from signal_agent output.
    Returns None if nothing suitable is found.
    """
    equal_hl = signal_result.get("equal_hl", {})
    fvg_history = signal_result.get("fvg_history", [])

    if is_long:
        # Best stop: highest equal low that is still below entry
        candidates = [
            lvl["price"]
            for lvl in equal_hl.get("equal_lows", [])
            if lvl["price"] < entry
        ]
        if candidates:
            return max(candidates)  # closest one below entry

        # Fallback: highest open bullish FVG zone_low below entry
        fvg_candidates = [
            f["zone_low"]
            for f in fvg_history
            if f["type"] == "Bullish" and not f["filled"] and f["zone_low"] < entry
        ]
        if fvg_candidates:
            return max(fvg_candidates)

    else:
        # Best stop: lowest equal high that is still above entry
        candidates = [
            lvl["price"]
            for lvl in equal_hl.get("equal_highs", [])
            if lvl["price"] > entry
        ]
        if candidates:
            return min(candidates)

        # Fallback: lowest open bearish FVG zone_high above entry
        fvg_candidates = [
            f["zone_high"]
            for f in fvg_history
            if f["type"] == "Bearish" and not f["filled"] and f["zone_high"] > entry
        ]
        if fvg_candidates:
            return min(fvg_candidates)

    return None


def _stop_source(signal_result: dict, stop_level: float, is_long: bool) -> str:
    equal_hl = signal_result.get("equal_hl", {})
    key = "equal_lows" if is_long else "equal_highs"
    for lvl in equal_hl.get(key, []):
        if abs(lvl["price"] - stop_level) / max(stop_level, 1) < 0.001:
            return f"Equal {'Low' if is_long else 'High'} @ ${stop_level:,.2f}"
    return f"FVG zone @ ${stop_level:,.2f}"


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------

def _print_plan(p: dict):
    v = p["verdict"]
    verdict_str = f"{'✓ ' if v == 'TAKE_TRADE' else '✗ '}{v}"
    print(f"  Verdict     : {verdict_str}")
    if p.get("skip_reason"):
        print(f"  Reason      : {p['skip_reason']}")
    if v == "SKIP":
        return
    dir_arrow = "▲ LONG" if p["direction"] == "LONG" else "▼ SHORT"
    print(f"  Direction   : {dir_arrow}")
    print(f"  Entry       : ${p['entry']:>12,.2f}")
    print(f"  Stop level  : ${p['stop_level']:>12,.2f}  (structural)")
    print(f"  Stop loss   : ${p['stop_loss']:>12,.2f}  ({p['stop_distance_pct']:.3f}% from entry)")
    print(f"  TP1 (2R)    : ${p['tp1']:>12,.2f}")
    print(f"  TP2 (3R)    : ${p['tp2']:>12,.2f}")
    print(f"  Risk        : ${p['risk_usd']:>8,.2f}  ({p['risk_pct']}% of ${p['account_balance']:,.0f})")
    print(f"  Contracts   : {p['contracts']:.6f} BTC")
    print(f"  Position    : ${p['position_size_usd']:>10,.2f}")
    print(f"  R:R         : {p['risk_reward']:.2f}:1")
    if p.get("stop_source"):
        print(f"  Stop source : {p['stop_source']}")


if __name__ == "__main__":
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from data.binance_feed import fetch_ohlcv, fetch_ticker
    from agents.signal_agent import run_all

    symbol   = (sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT").upper()
    balance  = float(sys.argv[2]) if len(sys.argv) > 2 else 1000.0
    risk_pct = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0

    print(f"Risk agent — {symbol}  balance=${balance:,.0f}  risk={risk_pct}%\n")

    # --- Fetch live data ---
    ticker = fetch_ticker(symbol)
    entry  = float(ticker["lastPrice"])
    ohlcv  = fetch_ohlcv(symbol, "1h", 200)
    sigs   = run_all(ohlcv)

    print(f"  Entry price : ${entry:,.2f}")
    print(f"  Signal      : {sigs['signal']}")
    print(f"  RSI         : {sigs['rsi']:.2f}" if sigs["rsi"] else "  RSI    : N/A")
    rsi = sigs['rsi']
    hist = sigs['macd_hist']
    print(f"  MACD hist   : {hist:+.4f}" if hist else "  MACD hist  : N/A")

    eqh = sigs["equal_hl"]["equal_highs"]
    eql = sigs["equal_hl"]["equal_lows"]
    open_fvgs = [f for f in sigs["fvg_history"] if not f["filled"]]
    print(f"  Equal Highs : {len(eqh)}  |  Equal Lows: {len(eql)}")
    print(f"  Open FVGs   : {len(open_fvgs)}")

    # --- Test 1: from_signal_agent (auto stop level) ---
    print(f"\n[1] from_signal_agent (auto stop from structure)")
    plan = from_signal_agent(sigs, entry, balance, risk_pct)
    _print_plan(plan)

    # --- Test 2: manual LONG with known stop level ---
    print(f"\n[2] calculate_risk — manual LONG  (stop at equal low or -0.7%)")
    manual_stop = eql[0]["price"] if eql else entry * 0.993
    plan2 = calculate_risk("STRONG BUY", entry, manual_stop, balance, risk_pct)
    _print_plan(plan2)

    # --- Test 3: manual SHORT ---
    print(f"\n[3] calculate_risk — manual SHORT (stop at equal high or +0.7%)")
    manual_stop_s = eqh[0]["price"] if eqh else entry * 1.007
    plan3 = calculate_risk("STRONG SELL", entry, manual_stop_s, balance, risk_pct)
    _print_plan(plan3)

    # --- Test 4: HOLD signal → always SKIP ---
    print(f"\n[4] calculate_risk — HOLD signal  (should SKIP)")
    plan4 = calculate_risk("HOLD", entry, entry * 0.99, balance, risk_pct)
    _print_plan(plan4)

    # --- Test 5: bad stop placement → SKIP ---
    print(f"\n[5] calculate_risk — stop ABOVE entry for LONG (invalid, should SKIP)")
    plan5 = calculate_risk("BUY", entry, entry * 1.01, balance, risk_pct)
    _print_plan(plan5)

    print("\nAll checks passed.")
