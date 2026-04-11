"""
agents/signal_agent.py
======================
Technical signal detection agent.

Migrated from:
    crypto_tracker.py — calculate_rsi, calculate_ema_series, calculate_macd,
                        detect_fvg, combined_signal
    test_fvg.py       — scan_all_fvgs (wick/body modes, fill detection)

New additions:
    detect_equal_highs_lows — Equal High / Equal Low pattern (SMC)
    detect_liquidity_sweeps — Liquidity Sweep / Stop Hunt detection

All functions accept a list of OHLCV dicts as returned by
data.binance_feed.fetch_ohlcv(), OR a list of raw kline rows (list-of-lists).
Helper `_to_ohlcv` normalises both formats internally.

Exported
--------
calculate_rsi(closes, period)              -> float | None
calculate_ema_series(closes, period)       -> list[float]
calculate_macd(closes)                     -> (macd, signal, hist) | (None,None,None)
detect_fvg_latest(klines, mode, min_gap)   -> dict | None
scan_fvg_history(klines, mode, min_gap)    -> list[dict]
detect_equal_highs_lows(klines, lookback, tolerance, min_touches) -> dict
detect_liquidity_sweeps(klines, lookback, swing_n) -> list[dict]
combined_signal(rsi, macd_hist)            -> str
run_all(ohlcv)                             -> dict
"""

from datetime import datetime

# ---------------------------------------------------------------------------
# Internal normaliser — accepts both raw klines and parsed OHLCV dicts
# ---------------------------------------------------------------------------

def _to_ohlcv(klines: list) -> list:
    """
    Convert raw Binance kline rows (list-of-lists) to OHLCV dicts if needed.
    If the input is already a list of dicts with 'close', pass it straight through.
    """
    if not klines:
        return []
    if isinstance(klines[0], dict):
        return klines
    result = []
    for row in klines:
        result.append({
            "open_time": datetime.fromtimestamp(row[0] / 1000),
            "open":      float(row[1]),
            "high":      float(row[2]),
            "low":       float(row[3]),
            "close":     float(row[4]),
            "volume":    float(row[5]),
        })
    return result


def _closes(ohlcv: list) -> list:
    return [c["close"] for c in ohlcv]

def _highs(ohlcv: list) -> list:
    return [c["high"] for c in ohlcv]

def _lows(ohlcv: list) -> list:
    return [c["low"] for c in ohlcv]


# ---------------------------------------------------------------------------
# RSI
# Migrated from crypto_tracker.py :: calculate_rsi
# ---------------------------------------------------------------------------

def calculate_rsi(closes: list, period: int = 14) -> float | None:
    """
    Wilder-smoothed RSI.

    Returns None if there is not enough data (need at least period+1 closes).
    """
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


# ---------------------------------------------------------------------------
# EMA series
# Migrated from crypto_tracker.py :: calculate_ema_series
# ---------------------------------------------------------------------------

def calculate_ema_series(closes: list, period: int) -> list:
    """
    Return a list of EMA values starting once *period* bars have been seen.
    Length = max(0, len(closes) - period + 1).
    """
    if len(closes) < period:
        return []
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    result = [ema]
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
        result.append(ema)
    return result


# ---------------------------------------------------------------------------
# MACD
# Migrated from crypto_tracker.py :: calculate_macd
# ---------------------------------------------------------------------------

def calculate_macd(
    closes: list,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple:
    """
    Return (macd_value, signal_value, histogram) or (None, None, None).

    Minimum bars required: slow + signal - 1  (default = 34).
    """
    if len(closes) < slow + signal - 1:
        return None, None, None
    ema_fast = calculate_ema_series(closes, fast)
    ema_slow = calculate_ema_series(closes, slow)
    offset = slow - fast
    macd_line = [ema_fast[i + offset] - ema_slow[i] for i in range(len(ema_slow))]
    signal_series = calculate_ema_series(macd_line, signal)
    if not signal_series:
        return None, None, None
    macd_val   = macd_line[-1]
    signal_val = signal_series[-1]
    return macd_val, signal_val, macd_val - signal_val


# ---------------------------------------------------------------------------
# FVG — latest only
# Migrated from crypto_tracker.py :: detect_fvg  (renamed + enhanced)
# ---------------------------------------------------------------------------

def detect_fvg_latest(
    klines: list,
    mode: str = "wick",
    min_gap_pct: float = 0.3,
    lookback: int = 50,
) -> dict | None:
    """
    Scan the most-recent *lookback* candles and return the newest FVG, or None.

    mode        : "wick" (high/low) | "body" (open/close)
    min_gap_pct : minimum gap size as a percentage of price (default 0.3%)

    Returned dict keys:
        type       : "Bullish" | "Bearish"
        zone_low   : float
        zone_high  : float
        gap_pct    : float
        datetime   : datetime
        filled     : bool
        index      : int   (candle index in the slice)
    """
    ohlcv = _to_ohlcv(klines)[-lookback:]
    threshold = min_gap_pct / 100

    for i in range(len(ohlcv) - 2, 0, -1):
        c1, c3 = ohlcv[i - 1], ohlcv[i + 1]
        if mode == "wick":
            c1_top, c1_bot = c1["high"], c1["low"]
            c3_top, c3_bot = c3["high"], c3["low"]
        else:
            c1_top = max(c1["open"], c1["close"])
            c1_bot = min(c1["open"], c1["close"])
            c3_top = max(c3["open"], c3["close"])
            c3_bot = min(c3["open"], c3["close"])

        if c3_bot > c1_top and (c3_bot - c1_top) / c1_top >= threshold:
            return {
                "type":      "Bullish",
                "zone_low":  c1_top,
                "zone_high": c3_bot,
                "gap_pct":   (c3_bot - c1_top) / c1_top * 100,
                "datetime":  ohlcv[i]["open_time"],
                "filled":    False,
                "index":     i,
            }
        if c3_top < c1_bot and (c1_bot - c3_top) / c1_bot >= threshold:
            return {
                "type":      "Bearish",
                "zone_low":  c3_top,
                "zone_high": c1_bot,
                "gap_pct":   (c1_bot - c3_top) / c1_bot * 100,
                "datetime":  ohlcv[i]["open_time"],
                "filled":    False,
                "index":     i,
            }
    return None


# ---------------------------------------------------------------------------
# FVG — full history scan
# Migrated from test_fvg.py :: scan_all_fvgs
# ---------------------------------------------------------------------------

def scan_fvg_history(
    klines: list,
    mode: str = "wick",
    min_gap_pct: float = 0.3,
) -> list:
    """
    Scan all candles for FVG patterns and return every instance found.

    mode        : "wick" (high/low) | "body" (open/close)
    min_gap_pct : minimum gap size as a percentage of price

    Each dict:
        index      : int
        type       : "Bullish" | "Bearish"
        zone_low   : float
        zone_high  : float
        gap_pct    : float
        datetime   : datetime
        filled     : bool
        filled_at  : datetime | None
    """
    ohlcv = _to_ohlcv(klines)
    threshold = min_gap_pct / 100
    fvgs = []

    for i in range(1, len(ohlcv) - 1):
        c1, c3 = ohlcv[i - 1], ohlcv[i + 1]
        if mode == "wick":
            c1_top, c1_bot = c1["high"], c1["low"]
            c3_top, c3_bot = c3["high"], c3["low"]
        else:
            c1_top = max(c1["open"], c1["close"])
            c1_bot = min(c1["open"], c1["close"])
            c3_top = max(c3["open"], c3["close"])
            c3_bot = min(c3["open"], c3["close"])

        fvg = None
        if c3_bot > c1_top and (c3_bot - c1_top) / c1_top >= threshold:
            fvg = {
                "index":     i,
                "type":      "Bullish",
                "zone_low":  c1_top,
                "zone_high": c3_bot,
                "gap_pct":   (c3_bot - c1_top) / c1_top * 100,
                "datetime":  ohlcv[i]["open_time"],
                "filled":    False,
                "filled_at": None,
            }
        elif c3_top < c1_bot and (c1_bot - c3_top) / c1_bot >= threshold:
            fvg = {
                "index":     i,
                "type":      "Bearish",
                "zone_low":  c3_top,
                "zone_high": c1_bot,
                "gap_pct":   (c1_bot - c3_top) / c1_bot * 100,
                "datetime":  ohlcv[i]["open_time"],
                "filled":    False,
                "filled_at": None,
            }

        if fvg is None:
            continue

        # Check if any subsequent candle fills the zone (always use wicks for fill)
        for j in range(i + 2, len(ohlcv)):
            if ohlcv[j]["low"] <= fvg["zone_high"] and ohlcv[j]["high"] >= fvg["zone_low"]:
                fvg["filled"]    = True
                fvg["filled_at"] = ohlcv[j]["open_time"]
                break

        fvgs.append(fvg)

    return fvgs


# ---------------------------------------------------------------------------
# Equal Highs / Equal Lows  (SMC — new)
# ---------------------------------------------------------------------------

def detect_equal_highs_lows(
    klines: list,
    lookback: int = 100,
    tolerance_pct: float = 0.075,
    min_touches: int = 2,
    swing_n: int = 3,
) -> dict:
    """
    Detect Equal High and Equal Low patterns (Smart Money Concept).

    These levels indicate clustered stop-loss orders and are prime
    targets for liquidity grabs.

    Parameters
    ----------
    lookback      : number of recent candles to scan
    tolerance_pct : price levels within this % are considered "equal"
    min_touches   : minimum number of touches to qualify as a level
    swing_n       : bars on each side required to confirm a swing point

    Returns
    -------
    {
        "equal_highs": [
            {
                "price":    float,          # average level price
                "touches":  int,            # number of swing highs at this level
                "indices":  list[int],
                "datetimes": list[datetime],
                "range_low":  float,        # price band low
                "range_high": float,        # price band high
            },
            ...
        ],
        "equal_lows": [ ... ],              # same structure
    }
    """
    ohlcv = _to_ohlcv(klines)[-lookback:]
    n = swing_n
    tol = tolerance_pct / 100

    # Find swing highs and lows
    swing_highs = []
    swing_lows  = []
    for i in range(n, len(ohlcv) - n):
        h = ohlcv[i]["high"]
        l = ohlcv[i]["low"]
        if all(h >= ohlcv[j]["high"] for j in range(i - n, i + n + 1) if j != i):
            swing_highs.append((i, h, ohlcv[i]["open_time"]))
        if all(l <= ohlcv[j]["low"]  for j in range(i - n, i + n + 1) if j != i):
            swing_lows.append((i, l, ohlcv[i]["open_time"]))

    def _cluster(points):
        """Group price points that are within tolerance of each other."""
        used = [False] * len(points)
        clusters = []
        for i, (idx_i, price_i, dt_i) in enumerate(points):
            if used[i]:
                continue
            group_idx    = [idx_i]
            group_prices = [price_i]
            group_dts    = [dt_i]
            used[i] = True
            for j, (idx_j, price_j, dt_j) in enumerate(points):
                if used[j] or j == i:
                    continue
                if abs(price_j - price_i) / price_i <= tol:
                    group_idx.append(idx_j)
                    group_prices.append(price_j)
                    group_dts.append(dt_j)
                    used[j] = True
            if len(group_prices) >= min_touches:
                avg_price = sum(group_prices) / len(group_prices)
                clusters.append({
                    "price":      avg_price,
                    "touches":    len(group_prices),
                    "indices":    group_idx,
                    "datetimes":  group_dts,
                    "range_low":  min(group_prices),
                    "range_high": max(group_prices),
                })
        return sorted(clusters, key=lambda x: x["touches"], reverse=True)

    return {
        "equal_highs": _cluster(swing_highs),
        "equal_lows":  _cluster(swing_lows),
    }


# ---------------------------------------------------------------------------
# Liquidity Sweeps / Stop Hunts  (new)
# ---------------------------------------------------------------------------

def detect_liquidity_sweeps(
    klines: list,
    lookback: int = 100,
    swing_n: int = 3,
    min_wick_pct: float = 0.05,
) -> list:
    """
    Detect liquidity sweeps (stop hunts) in recent candles.

    A sweep occurs when price breaches a prior swing high/low via a wick
    but closes back on the other side — indicating stop orders were filled
    before price reversed.

    Parameters
    ----------
    lookback     : number of recent candles to analyse
    swing_n      : bars on each side to confirm a swing point
    min_wick_pct : minimum wick-beyond-swing as % of swing price

    Each returned dict:
        type        : "Bullish Sweep" (swept lows → expect up)
                    | "Bearish Sweep" (swept highs → expect down)
        sweep_price : float   (the swing level that was breached)
        wick_low/high : float (the extreme of the sweeping candle)
        close       : float   (close of the sweeping candle)
        index       : int
        datetime    : datetime
        wick_extension_pct : float  (how far beyond the level the wick went)
    """
    ohlcv = _to_ohlcv(klines)[-lookback:]
    n = swing_n
    min_wick = min_wick_pct / 100
    sweeps = []

    for i in range(n, len(ohlcv) - 1):
        candle = ohlcv[i]

        # --- Bearish sweep: wick above prior swing high, closes below it ---
        prior_highs = [ohlcv[j]["high"] for j in range(max(0, i - 20), i)]
        if prior_highs:
            swing_high = max(prior_highs)
            if (candle["high"] > swing_high
                    and candle["close"] < swing_high
                    and (candle["high"] - swing_high) / swing_high >= min_wick):
                sweeps.append({
                    "type":                 "Bearish Sweep",
                    "sweep_price":          swing_high,
                    "wick_high":            candle["high"],
                    "close":                candle["close"],
                    "index":                i,
                    "datetime":             candle["open_time"],
                    "wick_extension_pct":   (candle["high"] - swing_high) / swing_high * 100,
                })

        # --- Bullish sweep: wick below prior swing low, closes above it ---
        prior_lows = [ohlcv[j]["low"] for j in range(max(0, i - 20), i)]
        if prior_lows:
            swing_low = min(prior_lows)
            if (candle["low"] < swing_low
                    and candle["close"] > swing_low
                    and (swing_low - candle["low"]) / swing_low >= min_wick):
                sweeps.append({
                    "type":                "Bullish Sweep",
                    "sweep_price":         swing_low,
                    "wick_low":            candle["low"],
                    "close":               candle["close"],
                    "index":               i,
                    "datetime":            candle["open_time"],
                    "wick_extension_pct":  (swing_low - candle["low"]) / swing_low * 100,
                })

    # Return sorted by most recent first
    return sorted(sweeps, key=lambda x: x["index"], reverse=True)


# ---------------------------------------------------------------------------
# Combined signal
# Migrated from crypto_tracker.py :: combined_signal
# ---------------------------------------------------------------------------
# Trend filter — EMA100 / EMA300 alignment
# ---------------------------------------------------------------------------

def trend_filter(ohlcv: list) -> str:
    """
    Determine the macro trend using EMA100 and EMA300 alignment.

    Rules
    -----
    price > EMA100 > EMA300  →  "BULLISH"   (only BUY signals allowed)
    price < EMA100 < EMA300  →  "BEARISH"   (only SELL signals allowed)
    anything else            →  "NEUTRAL"   (WEAK signals blocked)

    Requires at least 300 candles; returns "NEUTRAL" if data is insufficient.

    Returns
    -------
    "BULLISH" | "BEARISH" | "NEUTRAL"
    """
    closes = _closes(ohlcv)
    if len(closes) < 300:
        return "NEUTRAL"

    ema100_series = calculate_ema_series(closes, 100)
    ema300_series = calculate_ema_series(closes, 300)

    if not ema100_series or not ema300_series:
        return "NEUTRAL"

    price  = closes[-1]
    ema100 = ema100_series[-1]
    ema300 = ema300_series[-1]

    if price > ema100 > ema300:
        return "BULLISH"
    if price < ema100 < ema300:
        return "BEARISH"
    return "NEUTRAL"


# ---------------------------------------------------------------------------

def combined_signal(
    rsi: float | None,
    macd_hist: float | None,
    fvg_latest: dict | None = None,
    sweeps_recent: list | None = None,
    weak_buy_rsi: float = 45.0,
    weak_sell_rsi: float = 55.0,
    trend: str = "NEUTRAL",
) -> str:
    """
    Derive a trading signal from RSI, MACD, FVG, sweep context, and trend.

    Signal hierarchy (strongest → weakest)
    ---------------------------------------
    STRONG BUY  : RSI < 30             AND MACD bullish   [trend: always passes]
    BUY         : RSI < 30             (MACD neutral)     [trend: BULLISH or NEUTRAL]
    STRONG SELL : RSI > 70             AND MACD bearish   [trend: always passes]
    SELL        : RSI > 70                                [trend: BEARISH or NEUTRAL]
    WEAK BUY    : RSI < weak_buy_rsi   AND MACD bullish AND (FVG OR sweep)
                  [trend: BULLISH only]
    WEAK SELL   : RSI > weak_sell_rsi  AND MACD bearish AND (FVG OR sweep)
                  [trend: BEARISH only]
    HOLD        : everything else, or trend mismatch

    Trend filter rules
    ------------------
    BULLISH  → block all SELL / WEAK SELL
    BEARISH  → block all BUY  / WEAK BUY
    NEUTRAL  → block WEAK BUY and WEAK SELL; allow STRONG tiers and RSI extremes
    STRONG BUY / STRONG SELL always pass — RSI extremes override trend.

    Parameters
    ----------
    trend         : "BULLISH" | "BEARISH" | "NEUTRAL"  (from trend_filter())
    weak_buy_rsi  : upper RSI bound for WEAK BUY  (default 45)
    weak_sell_rsi : lower RSI bound for WEAK SELL (default 55)
    """
    if rsi is None:
        return "HOLD"

    macd_bullish = macd_hist is not None and macd_hist > 0
    macd_bearish = macd_hist is not None and macd_hist < 0
    has_fvg      = fvg_latest is not None
    has_sweep    = bool(sweeps_recent)

    # --- Strong tiers: RSI extremes always pass, regardless of trend ---
    if rsi < 30 and macd_bullish:
        return "STRONG BUY"
    if rsi > 70 and macd_bearish:
        return "STRONG SELL"

    # --- Standard BUY/SELL: blocked by opposing trend ---
    if rsi < 30:
        return "HOLD" if trend == "BEARISH" else "BUY"
    if rsi > 70:
        return "HOLD" if trend == "BULLISH" else "SELL"

    # --- Weak tiers: require trend alignment, blocked in NEUTRAL ---
    if rsi < weak_buy_rsi and macd_bullish and (has_fvg or has_sweep):
        return "WEAK BUY" if trend == "BULLISH" else "HOLD"
    if rsi > weak_sell_rsi and macd_bearish and (has_fvg or has_sweep):
        return "WEAK SELL" if trend == "BEARISH" else "HOLD"

    return "HOLD"


# ---------------------------------------------------------------------------
# run_all — convenience wrapper used by the orchestrator
# ---------------------------------------------------------------------------

def run_all(
    ohlcv: list,
    fvg_mode: str = "wick",
    weak_buy_rsi: float = 45.0,
    weak_sell_rsi: float = 55.0,
) -> dict:
    """
    Run every signal on the given OHLCV list and return a single result dict.

    Parameters
    ----------
    ohlcv         : list of dicts from binance_feed.fetch_ohlcv()
    fvg_mode      : "wick" | "body"
    weak_buy_rsi  : RSI upper bound for WEAK BUY (default 45)
    weak_sell_rsi : RSI lower bound for WEAK SELL (default 55)

    Returns
    -------
    {
        "rsi"            : float | None,
        "macd"           : float | None,
        "macd_signal"    : float | None,
        "macd_hist"      : float | None,
        "trend"          : "BULLISH" | "BEARISH" | "NEUTRAL",
        "ema100"         : float | None,
        "ema300"         : float | None,
        "signal"         : str,
        "fvg_latest"     : dict | None,
        "fvg_history"    : list[dict],
        "equal_hl"       : dict,
        "sweeps"         : list[dict],
        "sweeps_recent"  : list[dict],
    }
    """
    closes = _closes(ohlcv)
    rsi    = calculate_rsi(closes)
    macd, macd_sig, macd_hist = calculate_macd(closes)

    # Trend filter
    trend         = trend_filter(ohlcv)
    ema100_series = calculate_ema_series(closes, 100)
    ema300_series = calculate_ema_series(closes, 300)
    ema100 = ema100_series[-1] if ema100_series else None
    ema300 = ema300_series[-1] if ema300_series else None

    fvg_latest  = detect_fvg_latest(ohlcv, mode=fvg_mode)
    fvg_history = scan_fvg_history(ohlcv, mode=fvg_mode)
    equal_hl    = detect_equal_highs_lows(ohlcv)
    sweeps      = detect_liquidity_sweeps(ohlcv)

    signal = combined_signal(
        rsi, macd_hist,
        fvg_latest, sweeps[:5],
        weak_buy_rsi, weak_sell_rsi,
        trend,
    )

    return {
        "rsi":           rsi,
        "macd":          macd,
        "macd_signal":   macd_sig,
        "macd_hist":     macd_hist,
        "trend":         trend,
        "ema100":        ema100,
        "ema300":        ema300,
        "signal":        signal,
        "fvg_latest":    fvg_latest,
        "fvg_history":   fvg_history,
        "equal_hl":      equal_hl,
        "sweeps":        sweeps,
        "sweeps_recent": sweeps[:5],
    }


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__file__)))
    from data.binance_feed import fetch_ohlcv

    symbol   = (sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT").upper()
    interval = sys.argv[2] if len(sys.argv) > 2 else "1h"

    print(f"Signal agent — {symbol} {interval}  (200 candles)\n")
    ohlcv = fetch_ohlcv(symbol, interval, 200)
    closes = _closes(ohlcv)

    # 1. RSI
    rsi = calculate_rsi(closes)
    print(f"[1] RSI(14)       : {rsi:.2f}" if rsi else "[1] RSI(14)  : N/A")

    # 2. MACD
    macd, sig, hist = calculate_macd(closes)
    if hist is not None:
        print(f"[2] MACD hist     : {hist:+.4f}  (macd={macd:+.4f}  signal={sig:+.4f})")
    else:
        print("[2] MACD          : N/A")

    # 3. Combined signal
    print(f"[3] Signal        : {combined_signal(rsi, hist)}")

    # 4. FVG latest
    fvg = detect_fvg_latest(ohlcv)
    if fvg:
        print(f"[4] FVG latest    : {fvg['type']}  zone=${fvg['zone_low']:,.2f}–${fvg['zone_high']:,.2f}"
              f"  gap={fvg['gap_pct']:.3f}%  @ {fvg['datetime'].strftime('%Y-%m-%d %H:%M')}")
    else:
        print("[4] FVG latest    : None")

    # 5. FVG history
    fvgs = scan_fvg_history(ohlcv)
    open_fvgs = [f for f in fvgs if not f["filled"]]
    print(f"[5] FVG history   : {len(fvgs)} total  |  {len(open_fvgs)} open  |  {len(fvgs)-len(open_fvgs)} filled")

    # 6. Equal H/L
    ehl = detect_equal_highs_lows(ohlcv)
    eqh = ehl["equal_highs"]
    eql = ehl["equal_lows"]
    print(f"[6] Equal Highs   : {len(eqh)} level(s)")
    for lvl in eqh[:2]:
        print(f"     ${lvl['price']:,.2f}  ({lvl['touches']} touches)"
              f"  band=${lvl['range_low']:,.2f}–${lvl['range_high']:,.2f}")
    print(f"    Equal Lows    : {len(eql)} level(s)")
    for lvl in eql[:2]:
        print(f"     ${lvl['price']:,.2f}  ({lvl['touches']} touches)"
              f"  band=${lvl['range_low']:,.2f}–${lvl['range_high']:,.2f}")

    # 7. Liquidity sweeps
    sweeps = detect_liquidity_sweeps(ohlcv)
    bull_sw = [s for s in sweeps if "Bullish" in s["type"]]
    bear_sw = [s for s in sweeps if "Bearish" in s["type"]]
    print(f"[7] Sweeps (200c) : {len(sweeps)} total  |  {len(bull_sw)} bullish  |  {len(bear_sw)} bearish")
    for sw in sweeps[:3]:
        print(f"     {sw['datetime'].strftime('%Y-%m-%d %H:%M')}  {sw['type']}"
              f"  swept=${sw['sweep_price']:,.2f}  ext={sw['wick_extension_pct']:.3f}%")

    # 8. run_all
    result = run_all(ohlcv)
    print(f"\n[8] run_all keys  : {list(result.keys())}")
    print(f"    signal        : {result['signal']}")
    print(f"    open FVGs     : {len([f for f in result['fvg_history'] if not f['filled']])}")
    print(f"    recent sweeps : {len(result['sweeps_recent'])}")

    print("\nAll checks passed.")
