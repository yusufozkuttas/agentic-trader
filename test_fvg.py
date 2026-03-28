import urllib.request
import json
from datetime import datetime

KLINES_URL = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=200"

# Binance kline indices
O, H, L, C = 1, 2, 3, 4


def fetch_klines():
    with urllib.request.urlopen(KLINES_URL, timeout=10) as resp:
        return json.loads(resp.read())


def scan_all_fvgs(klines, mode="wick"):
    """
    Scan all candles for FVG patterns (gap >= 0.3% of price).

    mode="wick" : High/Low based — wicks included (Mode 1)
      Bullish: candle[i+1].low  > candle[i-1].high
      Bearish: candle[i+1].high < candle[i-1].low

    mode="body" : Open/Close based — wicks excluded (Mode 2)
      Bullish: candle[i+1].body_bottom > candle[i-1].body_top
      Bearish: candle[i+1].body_top    < candle[i-1].body_bottom
    """
    fvgs = []
    for i in range(1, len(klines) - 1):
        ts = datetime.fromtimestamp(klines[i][0] / 1000)

        if mode == "wick":
            c1_top    = float(klines[i - 1][H])
            c1_bottom = float(klines[i - 1][L])
            c3_top    = float(klines[i + 1][H])
            c3_bottom = float(klines[i + 1][L])
        else:  # body
            c1_top    = max(float(klines[i - 1][O]), float(klines[i - 1][C]))
            c1_bottom = min(float(klines[i - 1][O]), float(klines[i - 1][C]))
            c3_top    = max(float(klines[i + 1][O]), float(klines[i + 1][C]))
            c3_bottom = min(float(klines[i + 1][O]), float(klines[i + 1][C]))

        if c3_bottom > c1_top and (c3_bottom - c1_top) / c1_top >= 0.003:
            fvg = {
                "index": i,
                "type": "Bullish",
                "zone_low": c1_top,
                "zone_high": c3_bottom,
                "gap_pct": (c3_bottom - c1_top) / c1_top * 100,
                "datetime": ts,
            }
        elif c3_top < c1_bottom and (c1_bottom - c3_top) / c1_bottom >= 0.003:
            fvg = {
                "index": i,
                "type": "Bearish",
                "zone_low": c3_top,
                "zone_high": c1_bottom,
                "gap_pct": (c1_bottom - c3_top) / c1_bottom * 100,
                "datetime": ts,
            }
        else:
            continue

        # Check if subsequent candles filled the zone (always use wick for fill check)
        fvg["filled"] = False
        fvg["filled_at"] = None
        for j in range(i + 2, len(klines)):
            c_high = float(klines[j][H])
            c_low  = float(klines[j][L])
            if c_low <= fvg["zone_high"] and c_high >= fvg["zone_low"]:
                fvg["filled"] = True
                fvg["filled_at"] = datetime.fromtimestamp(klines[j][0] / 1000)
                break

        fvgs.append(fvg)
    return fvgs


def fmt_price(p):
    return f"{p:,.2f}"


def print_fvgs(fvgs, mode_label):
    GREEN  = "\033[92m"
    RED    = "\033[91m"
    YELLOW = "\033[93m"
    RESET  = "\033[0m"

    header = (f"{'#':<5} {'Idx':<6} {'Type':<10} {'Zone (Low - High)':^28} "
              f"{'Gap':>7}  {'Status':<8}  {'Filled At':<17}  {'Date/Time'}")
    print(f"\n{'=' * len(header)}")
    print(f"Mode: {mode_label}")
    print(f"{'=' * len(header)}")
    print(header)
    print("-" * len(header))

    for n, fvg in enumerate(fvgs, 1):
        color    = GREEN if fvg["type"] == "Bullish" else RED
        zone     = f"${fmt_price(fvg['zone_low'])} - ${fmt_price(fvg['zone_high'])}"
        dt       = fvg["datetime"].strftime("%Y-%m-%d %H:%M")
        gap      = f"{fvg['gap_pct']:.2f}%"
        if fvg["filled"]:
            status     = f"{YELLOW}FILLED{RESET}"
            filled_str = fvg["filled_at"].strftime("%Y-%m-%d %H:%M")
        else:
            status     = f"{GREEN}OPEN{RESET}  "
            filled_str = "-"

        print(f"{n:<5} {fvg['index']:<6} {color}{fvg['type']:<10}{RESET} "
              f"{zone:<28} {gap:>7}  {status}  {filled_str:<17}  {dt}")

    print()
    filled_count  = sum(1 for f in fvgs if f["filled"])
    open_count    = len(fvgs) - filled_count
    bullish_count = sum(1 for f in fvgs if f["type"] == "Bullish")
    bearish_count = sum(1 for f in fvgs if f["type"] == "Bearish")
    fill_rate     = filled_count / len(fvgs) * 100 if fvgs else 0

    print(f"Total FVGs : {len(fvgs)}  "
          f"({GREEN}Bullish: {bullish_count}{RESET}  |  {RED}Bearish: {bearish_count}{RESET})")
    print(f"Fill rate  : {YELLOW}{filled_count} FILLED{RESET} / {GREEN}{open_count} OPEN{RESET}"
          f"  →  {fill_rate:.0f}%")

    return {"total": len(fvgs), "bullish": bullish_count, "bearish": bearish_count,
            "filled": filled_count, "open": open_count, "fill_rate": fill_rate}


def print_comparison(stats_wick, stats_body):
    GREEN  = "\033[92m"
    RED    = "\033[91m"
    YELLOW = "\033[93m"
    CYAN   = "\033[96m"
    RESET  = "\033[0m"

    print(f"\n{CYAN}{'=' * 52}")
    print("COMPARISON SUMMARY")
    print(f"{'=' * 52}{RESET}")
    print(f"{'Metric':<20} {'Mode 1 (Wick)':>15} {'Mode 2 (Body)':>15}")
    print("-" * 52)

    rows = [
        ("Total FVGs",    stats_wick["total"],     stats_body["total"]),
        ("Bullish",       stats_wick["bullish"],    stats_body["bullish"]),
        ("Bearish",       stats_wick["bearish"],    stats_body["bearish"]),
        ("Filled",        stats_wick["filled"],     stats_body["filled"]),
        ("Open",          stats_wick["open"],        stats_body["open"]),
    ]
    for label, v1, v2 in rows:
        print(f"  {label:<18} {v1:>15} {v2:>15}")

    print(f"  {'Fill rate':<18} {stats_wick['fill_rate']:>14.0f}% {stats_body['fill_rate']:>14.0f}%")
    print("-" * 52)
    diff = stats_body["total"] - stats_wick["total"]
    if diff > 0:
        print(f"\n  Body mode finds {YELLOW}{diff} more FVG(s){RESET} than Wick mode.")
        print(f"  Wick mode is stricter — wicks bridging the gap disqualify it.")
    elif diff < 0:
        print(f"\n  Wick mode finds {YELLOW}{-diff} more FVG(s){RESET} than Body mode.")
        print(f"  Body mode is stricter — wicks that cross the gap don't count.")
    else:
        print(f"\n  Both modes found the same number of FVGs.")


def main():
    print("Fetching last 200 x 1h candles for BTCUSDT from Binance...")
    klines = fetch_klines()
    print(f"Fetched {len(klines)} candles")

    fvgs_wick = scan_all_fvgs(klines, mode="wick")
    fvgs_body = scan_all_fvgs(klines, mode="body")

    stats_wick = print_fvgs(fvgs_wick, "Mode 1 — Wick/High/Low based")
    stats_body = print_fvgs(fvgs_body, "Mode 2 — Body/Open/Close based")

    print_comparison(stats_wick, stats_body)


if __name__ == "__main__":
    main()
