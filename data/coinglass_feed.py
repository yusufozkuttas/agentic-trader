"""
data/coinglass_feed.py
======================
Funding Rate, Open Interest, and Long/Short Ratio via Binance Futures endpoints.

No API key required — all endpoints are public.

Binance Futures base: https://fapi.binance.com

Exported (same interface as the original CoinGlass version)
--------
fetch_funding_rates(symbol, limit)           -> list[dict]
fetch_open_interest(symbol, period, limit)   -> list[dict]
fetch_long_short_ratio(symbol, period, limit)-> list[dict]
fetch_market_snapshot(symbol)                -> dict

Valid period strings: "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"
"""

import json
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FAPI  = "https://fapi.binance.com/fapi/v1"
_FDATA = "https://fapi.binance.com/futures/data"

_ENDPOINTS = {
    "funding":    _FAPI  + "/fundingRate",
    "open_interest_current": _FAPI + "/openInterest",
    "open_interest_hist":    _FDATA + "/openInterestHist",
    "long_short": _FDATA + "/globalLongShortAccountRatio",
    "top_trader_ls": _FDATA + "/topLongShortAccountRatio",
    "taker_ls":   _FDATA + "/takerlongshortRatio",
}

# Valid period values for Binance Futures data endpoints
VALID_PERIODS = {"5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"}


# ---------------------------------------------------------------------------
# Internal HTTP helper
# ---------------------------------------------------------------------------

def _get(endpoint: str, params: dict) -> list | dict:
    """GET *endpoint* with *params*, return parsed JSON (list or dict)."""
    url = endpoint + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "crypto-bot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise urllib.error.HTTPError(
            exc.url, exc.code, f"{exc.reason} — {raw}", exc.headers, None
        ) from exc


def _validate_period(period: str) -> str:
    if period not in VALID_PERIODS:
        raise ValueError(f"Invalid period '{period}'. Choose from: {sorted(VALID_PERIODS)}")
    return period


# ---------------------------------------------------------------------------
# Funding Rate
# ---------------------------------------------------------------------------

def fetch_funding_rates(symbol: str, limit: int = 10) -> list:
    """
    Return recent funding rate history for *symbol* (Binance Futures).

    Parameters
    ----------
    symbol : e.g. "BTCUSDT"
    limit  : number of records (max 1000)

    Each dict:
        symbol        : str
        funding_rate  : float   (e.g. 0.0001 = 0.01%)
        funding_rate_pct : float (e.g. 0.01 = 0.01%)
        funding_time  : datetime
        mark_price    : float | None
    """
    data = _get(_ENDPOINTS["funding"], {
        "symbol": symbol.upper(),
        "limit":  min(limit, 1000),
    })

    rows = []
    for item in data:
        rate = float(item.get("fundingRate", 0))
        ft   = item.get("fundingTime")
        mp   = item.get("markPrice")
        rows.append({
            "symbol":           item.get("symbol", symbol.upper()),
            "funding_rate":     rate,
            "funding_rate_pct": rate * 100,
            "funding_time":     datetime.fromtimestamp(ft / 1000) if ft else None,
            "mark_price":       float(mp) if mp else None,
        })
    return rows


# ---------------------------------------------------------------------------
# Open Interest
# ---------------------------------------------------------------------------

def fetch_open_interest_current(symbol: str) -> dict:
    """
    Return the current (single snapshot) open interest for *symbol*.

    Dict keys: symbol, open_interest (contracts), timestamp
    """
    data = _get(_ENDPOINTS["open_interest_current"], {"symbol": symbol.upper()})
    return {
        "symbol":        data.get("symbol", symbol.upper()),
        "open_interest": float(data.get("openInterest", 0)),
        "timestamp":     datetime.fromtimestamp(data.get("time", 0) / 1000),
    }


def fetch_open_interest(
    symbol: str,
    period: str = "1h",
    limit: int = 24,
) -> list:
    """
    Return open interest history for *symbol*.

    Parameters
    ----------
    symbol : e.g. "BTCUSDT"
    period : aggregation interval — "5m", "15m", "30m", "1h", "2h",
             "4h", "6h", "8h", "12h", "1d"
    limit  : number of records (max 500)

    Each dict:
        timestamp        : datetime
        open_interest    : float   (USD value)
        open_interest_contracts : float  (contract count)
    """
    _validate_period(period)
    data = _get(_ENDPOINTS["open_interest_hist"], {
        "symbol": symbol.upper(),
        "period": period,
        "limit":  min(limit, 500),
    })

    rows = []
    for item in data:
        ts = item.get("timestamp")
        rows.append({
            "timestamp":               datetime.fromtimestamp(ts / 1000) if ts else None,
            "open_interest":           float(item.get("sumOpenInterestValue", 0)),
            "open_interest_contracts": float(item.get("sumOpenInterest", 0)),
        })
    return rows


# ---------------------------------------------------------------------------
# Long / Short Ratio
# ---------------------------------------------------------------------------

def fetch_long_short_ratio(
    symbol: str,
    period: str = "1h",
    limit: int = 24,
) -> list:
    """
    Return global long/short account ratio history for *symbol*.

    Parameters
    ----------
    symbol : e.g. "BTCUSDT"
    period : aggregation interval
    limit  : number of records (max 500)

    Each dict:
        timestamp   : datetime
        long_pct    : float   (e.g. 52.3 — percentage of long accounts)
        short_pct   : float
        long_short  : float   (ratio: long_pct / short_pct)
    """
    _validate_period(period)
    data = _get(_ENDPOINTS["long_short"], {
        "symbol": symbol.upper(),
        "period": period,
        "limit":  min(limit, 500),
    })

    rows = []
    for item in data:
        ts     = item.get("timestamp")
        long_a = item.get("longAccount")
        short_a = item.get("shortAccount")
        ls_ratio = item.get("longShortRatio")

        long_f  = float(long_a)   if long_a   is not None else None
        short_f = float(short_a)  if short_a  is not None else None
        ratio_f = float(ls_ratio) if ls_ratio  is not None else None

        rows.append({
            "timestamp":  datetime.fromtimestamp(ts / 1000) if ts else None,
            "long_pct":   long_f  * 100 if long_f  is not None else None,
            "short_pct":  short_f * 100 if short_f is not None else None,
            "long_short": ratio_f,
        })
    return rows


def fetch_taker_buy_sell_ratio(
    symbol: str,
    period: str = "1h",
    limit: int = 24,
) -> list:
    """
    Return taker buy/sell volume ratio (aggressive buyers vs sellers).

    Each dict:
        timestamp      : datetime
        buy_sell_ratio : float   (>1.0 = more aggressive buying)
        buy_volume     : float
        sell_volume    : float
    """
    _validate_period(period)
    data = _get(_ENDPOINTS["taker_ls"], {
        "symbol": symbol.upper(),
        "period": period,
        "limit":  min(limit, 500),
    })

    rows = []
    for item in data:
        ts = item.get("timestamp")
        rows.append({
            "timestamp":      datetime.fromtimestamp(ts / 1000) if ts else None,
            "buy_sell_ratio": float(item.get("buySellRatio", 0)),
            "buy_volume":     float(item.get("buyVol", 0)),
            "sell_volume":    float(item.get("sellVol", 0)),
        })
    return rows


# ---------------------------------------------------------------------------
# Combined snapshot
# ---------------------------------------------------------------------------

def fetch_market_snapshot(symbol: str, period: str = "1h") -> dict:
    """
    Fetch funding rate, open interest, and long/short ratio in one call.

    Returns
    -------
    {
        "symbol"        : str,
        "funding_rates" : list[dict],   # recent funding rate history
        "open_interest" : list[dict],   # OI history
        "long_short"    : list[dict],   # L/S ratio history
        "taker_ratio"   : list[dict],   # taker buy/sell ratio
        "summary": {
            "latest_funding_rate" : float | None,   # most recent rate (%)
            "latest_oi"           : float | None,   # USD value
            "latest_long_pct"     : float | None,
            "latest_short_pct"    : float | None,
            "latest_buy_sell"     : float | None,
            "sentiment"           : str,            # "LONG_DOMINANT" | "SHORT_DOMINANT" | "NEUTRAL"
        },
        "errors": {...}   # only present if any sub-fetch failed
    }
    """
    sym = symbol.upper()
    errors = {}

    try:
        funding = fetch_funding_rates(sym, limit=5)
    except Exception as exc:
        funding = []
        errors["funding"] = str(exc)

    try:
        oi = fetch_open_interest(sym, period, limit=24)
    except Exception as exc:
        oi = []
        errors["open_interest"] = str(exc)

    try:
        ls = fetch_long_short_ratio(sym, period, limit=24)
    except Exception as exc:
        ls = []
        errors["long_short"] = str(exc)

    try:
        taker = fetch_taker_buy_sell_ratio(sym, period, limit=24)
    except Exception as exc:
        taker = []
        errors["taker_ratio"] = str(exc)

    latest_funding = funding[-1]["funding_rate_pct"] if funding else None
    latest_oi      = oi[-1]["open_interest"]         if oi      else None
    latest_long    = ls[-1]["long_pct"]              if ls      else None
    latest_short   = ls[-1]["short_pct"]             if ls      else None
    latest_bs      = taker[-1]["buy_sell_ratio"]     if taker   else None

    # Derive a simple sentiment label
    if latest_long is not None and latest_short is not None:
        if latest_long > 55:
            sentiment = "LONG_DOMINANT"
        elif latest_short > 55:
            sentiment = "SHORT_DOMINANT"
        else:
            sentiment = "NEUTRAL"
    else:
        sentiment = "UNKNOWN"

    result = {
        "symbol":        sym,
        "funding_rates": funding,
        "open_interest": oi,
        "long_short":    ls,
        "taker_ratio":   taker,
        "summary": {
            "latest_funding_rate": latest_funding,
            "latest_oi":           latest_oi,
            "latest_long_pct":     latest_long,
            "latest_short_pct":    latest_short,
            "latest_buy_sell":     latest_bs,
            "sentiment":           sentiment,
        },
    }
    if errors:
        result["errors"] = errors
    return result


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    symbol = (sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT").upper()

    print(f"Binance Futures feed — symbol: {symbol}  (no API key required)\n")

    print(f"[1] fetch_funding_rates({symbol}, limit=5)")
    rates = fetch_funding_rates(symbol, limit=5)
    for r in rates:
        ts  = r["funding_time"].strftime("%Y-%m-%d %H:%M") if r["funding_time"] else "?"
        mp  = f"${r['mark_price']:,.2f}" if r["mark_price"] else "N/A"
        print(f"    {ts}  rate={r['funding_rate_pct']:+.4f}%  mark={mp}")

    print(f"\n[2] fetch_open_interest({symbol}, '1h', limit=3)")
    oi = fetch_open_interest(symbol, "1h", limit=3)
    for row in oi:
        ts   = row["timestamp"].strftime("%Y-%m-%d %H:%M") if row["timestamp"] else "?"
        oi_b = f"${row['open_interest']/1e9:.3f}B"
        cts  = f"{row['open_interest_contracts']:,.1f} contracts"
        print(f"    {ts}  OI={oi_b}  ({cts})")

    print(f"\n[3] fetch_long_short_ratio({symbol}, '1h', limit=5)")
    ls = fetch_long_short_ratio(symbol, "1h", limit=5)
    for row in ls:
        ts = row["timestamp"].strftime("%Y-%m-%d %H:%M") if row["timestamp"] else "?"
        long_s  = f"{row['long_pct']:.2f}%"  if row["long_pct"]  is not None else "N/A"
        short_s = f"{row['short_pct']:.2f}%" if row["short_pct"] is not None else "N/A"
        ratio_s = f"{row['long_short']:.4f}" if row["long_short"] is not None else "N/A"
        print(f"    {ts}  long={long_s}  short={short_s}  L/S={ratio_s}")

    print(f"\n[4] fetch_taker_buy_sell_ratio({symbol}, '1h', limit=3)")
    taker = fetch_taker_buy_sell_ratio(symbol, "1h", limit=3)
    for row in taker:
        ts = row["timestamp"].strftime("%Y-%m-%d %H:%M") if row["timestamp"] else "?"
        print(f"    {ts}  buy/sell={row['buy_sell_ratio']:.4f}"
              f"  buy={row['buy_volume']:.2f}  sell={row['sell_volume']:.2f}")

    print(f"\n[5] fetch_market_snapshot({symbol})")
    snap = fetch_market_snapshot(symbol)
    s = snap["summary"]
    fr   = f"{s['latest_funding_rate']:+.4f}%" if s["latest_funding_rate"] is not None else "N/A"
    oi_s = f"${s['latest_oi']/1e9:.3f}B"       if s["latest_oi"]           is not None else "N/A"
    long_s  = f"{s['latest_long_pct']:.2f}%"   if s["latest_long_pct"]     is not None else "N/A"
    short_s = f"{s['latest_short_pct']:.2f}%"  if s["latest_short_pct"]    is not None else "N/A"
    bs   = f"{s['latest_buy_sell']:.4f}"        if s["latest_buy_sell"]     is not None else "N/A"
    print(f"    funding rate : {fr}")
    print(f"    open interest: {oi_s}")
    print(f"    long / short : {long_s} / {short_s}")
    print(f"    buy/sell ratio: {bs}")
    print(f"    sentiment    : {s['sentiment']}")
    if "errors" in snap:
        print(f"    errors       : {snap['errors']}")

    print("\nAll checks passed.")
