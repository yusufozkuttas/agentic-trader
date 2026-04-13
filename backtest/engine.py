"""
backtest/engine.py
==================
Walk-forward backtesting engine.

Data split (500 candles)
    Train      : first 60%  (300 candles) — model development
    Validation : next  20%  (100 candles) — hyper-parameter tuning
    Test       : last  20%  (100 candles) — SACRED, never run here

Walk-forward logic
    Window size : 200 candles  (signal context)
    Step size   : 50  candles  (forward evaluation horizon)

    For each step position i within a split:
        context window = all_data[i-200 : i]
        forward window = all_data[i     : i+50]
        → run signal_agent + risk_agent on context
        → check if TP1 or SL is hit within the forward window

Trade outcome
    WIN  : price reaches TP1 before stop_loss
    LOSS : price hits stop_loss before TP1
    OPEN : neither hit within the 50-candle forward window

Metrics per split
    win_rate     : wins / (wins + losses)            [%]
    sharpe       : mean(R-returns) / std(R-returns)
    max_drawdown : largest peak-to-trough drop       [%]
    total_trades : TAKE_TRADE signals fired

Overfitting check
    If win_rate drops > 15 pp from train → validation → flagged as OVERFIT

Exported
--------
BacktestEngine(symbol, ...)   — main class
    .fetch_and_split()        — download data, compute split indices
    .run()                    — execute walk-forward on train + val
    .report()                 — print formatted summary
    .results                  — raw results dict
    .check_overfit()          — (bool, explanation_str)
"""

import math
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from data.binance_feed    import fetch_ohlcv, fetch_ohlcv_extended
from agents.signal_agent  import run_all
from agents.risk_agent    import from_signal_agent

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WINDOW_SIZE    = 200
STEP_SIZE      = 25
TRAIN_PCT      = 0.60
VAL_PCT        = 0.20
# TEST_PCT     = 0.20  ← sacred, not referenced here intentionally


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class BacktestEngine:
    """
    Walk-forward backtesting engine.

    Usage
    -----
        engine = BacktestEngine("BTCUSDT")
        engine.fetch_and_split()
        engine.run()
        engine.report()
    """

    MIN_TRADES_TARGET   = 20   # robust backtest target
    INSUFFICIENT_FLOOR  = 10   # below this → "INSUFFICIENT DATA" warning

    def __init__(
        self,
        symbol: str,
        interval: str = "1h",
        total_candles: int = 3000,
        train_pct: float = TRAIN_PCT,
        val_pct: float = VAL_PCT,
        account_balance: float = 1000.0,
        risk_pct: float = 1.0,
        min_rr: float = 2.0,
        overfit_threshold: float = 0.15,   # 15 percentage-point drop
        tp_r: float = 2.0,                 # R-multiple awarded for a WIN
        weak_buy_rsi: float = 45.0,
        weak_sell_rsi: float = 55.0,
    ):
        self.symbol            = symbol.upper()
        self.interval          = interval
        self.total_candles     = total_candles
        self.train_pct         = train_pct
        self.val_pct           = val_pct
        self.account_balance   = account_balance
        self.risk_pct          = risk_pct
        self.min_rr            = min_rr
        self.overfit_threshold = overfit_threshold
        self.tp_r              = tp_r
        self.weak_buy_rsi      = weak_buy_rsi
        self.weak_sell_rsi     = weak_sell_rsi
        self.threshold_lowered = False     # flagged if auto-lowering fired

        self.all_data:  list = []
        self.train_end: int  = 0
        self.val_end:   int  = 0
        self.results:   dict = {}

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def fetch_and_split(self) -> "BacktestEngine":
        """Download candles and compute split boundaries."""
        print(f"Fetching {self.total_candles} x {self.interval} candles for {self.symbol}...")
        if self.total_candles > 1000:
            self.all_data = fetch_ohlcv_extended(self.symbol, self.interval, self.total_candles)
        else:
            self.all_data = fetch_ohlcv(self.symbol, self.interval, self.total_candles)
        n = len(self.all_data)

        self.train_end = int(n * self.train_pct)
        self.val_end   = self.train_end + int(n * self.val_pct)
        # self.val_end → n  is the test set — untouched

        t_start = self.all_data[0]["open_time"].strftime("%Y-%m-%d %H:%M")
        t_end   = self.all_data[-1]["open_time"].strftime("%Y-%m-%d %H:%M")
        print(f"  Total    : {n} candles  [{t_start} → {t_end}]")
        print(f"  Train    : [0 : {self.train_end})    "
              f"{self.all_data[0]['open_time'].strftime('%Y-%m-%d')} → "
              f"{self.all_data[self.train_end-1]['open_time'].strftime('%Y-%m-%d')}")
        print(f"  Val      : [{self.train_end} : {self.val_end})  "
              f"{self.all_data[self.train_end]['open_time'].strftime('%Y-%m-%d')} → "
              f"{self.all_data[self.val_end-1]['open_time'].strftime('%Y-%m-%d')}")
        print(f"  Test     : [{self.val_end} : {n})  ← SACRED (not run)")
        return self

    # ------------------------------------------------------------------
    # Walk-forward core
    # ------------------------------------------------------------------

    def _walk_forward(self, split_start: int, split_end: int, label: str) -> list:
        """
        Run walk-forward evaluation on a data split.

        For each step position i within [split_start, split_end]:
            context  = all_data[i - WINDOW_SIZE : i]
            forward  = all_data[i : i + STEP_SIZE]

        Context may reach into earlier splits (allowed — it's historical data).
        Forward window is strictly within the current split.
        """
        trades = []

        # First valid i: need WINDOW_SIZE bars of context
        # Last  valid i: need STEP_SIZE bars forward within the split
        positions = range(
            max(WINDOW_SIZE, split_start),
            split_end - STEP_SIZE + 1,
            STEP_SIZE,
        )

        total_pos = len(positions)
        if total_pos == 0:
            print(f"  [{label}] No walk-forward windows fit — split too small.")
            return trades

        print(f"  [{label}] {total_pos} window(s)  "
              f"(context={WINDOW_SIZE}, step={STEP_SIZE})")

        for step_num, i in enumerate(positions, 1):
            context = self.all_data[i - WINDOW_SIZE : i]
            forward = self.all_data[i : i + STEP_SIZE]

            entry = context[-1]["close"]
            w_start = context[0]["open_time"].strftime("%m-%d %H:%M")
            w_end   = context[-1]["open_time"].strftime("%m-%d %H:%M")

            # Run agents
            sig  = run_all(context)
            plan = from_signal_agent(
                sig, entry,
                account_balance=self.account_balance,
                risk_pct=self.risk_pct,
                min_rr=self.min_rr,
            )

            trade = {
                "step":         step_num,
                "window_start": context[0]["open_time"],
                "window_end":   context[-1]["open_time"],
                "entry":        entry,
                "signal":       sig["signal"],
                "rsi":          sig["rsi"],
                "macd_hist":    sig["macd_hist"],
                "verdict":      plan["verdict"],
                "plan":         plan,
                "outcome":      None,
                "r_return":     0.0,   # in R-multiples
            }

            if plan["verdict"] == "TAKE_TRADE":
                outcome = self._check_outcome(plan, forward)
                trade["outcome"] = outcome
                if outcome == "WIN":
                    trade["r_return"] = self.tp_r
                elif outcome == "LOSS":
                    trade["r_return"] = -1.0
                # OPEN → 0.0

                print(f"    step {step_num:02d} [{w_start}→{w_end}]  "
                      f"{sig['signal']:<11}  {plan['direction']}  "
                      f"entry=${entry:,.0f}  sl=${plan['stop_loss']:,.0f}  "
                      f"tp1=${plan['tp1']:,.0f}  → {outcome}")
            else:
                print(f"    step {step_num:02d} [{w_start}→{w_end}]  "
                      f"{sig['signal']:<11}  SKIP"
                      + (f"  ({plan['skip_reason'][:60]})" if plan.get("skip_reason") else ""))

            trades.append(trade)

        return trades

    # ------------------------------------------------------------------
    # Outcome resolution
    # ------------------------------------------------------------------

    def _check_outcome(self, plan: dict, forward: list) -> str:
        """
        Scan forward candles to determine if TP1 or SL is hit first.
        Returns "WIN", "LOSS", or "OPEN".
        """
        tp1 = plan["tp1"]
        sl  = plan["stop_loss"]
        direction = plan["direction"]

        for candle in forward:
            if direction == "LONG":
                if candle["high"] >= tp1:
                    return "WIN"
                if candle["low"] <= sl:
                    return "LOSS"
            else:  # SHORT
                if candle["low"] <= tp1:
                    return "WIN"
                if candle["high"] >= sl:
                    return "LOSS"
        return "OPEN"

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def _compute_metrics(self, trades: list) -> dict:
        """Compute performance metrics for a list of trade records."""
        take_trades = [t for t in trades if t["verdict"] == "TAKE_TRADE"]
        closed      = [t for t in take_trades if t["outcome"] in ("WIN", "LOSS")]
        wins        = [t for t in closed if t["outcome"] == "WIN"]
        losses      = [t for t in closed if t["outcome"] == "LOSS"]
        open_trades = [t for t in take_trades if t["outcome"] == "OPEN"]

        win_rate = len(wins) / len(closed) if closed else None

        # R-return series (include OPEN as 0 to penalise dead capital)
        r_returns = [t["r_return"] for t in take_trades]

        # Sharpe (on R-returns, no annualisation — sample too small)
        if len(r_returns) >= 2:
            mean_r = sum(r_returns) / len(r_returns)
            var_r  = sum((r - mean_r) ** 2 for r in r_returns) / (len(r_returns) - 1)
            std_r  = math.sqrt(var_r)
            sharpe = mean_r / std_r if std_r > 1e-9 else None
        else:
            sharpe = None

        # Equity curve (compound with fixed risk %)
        equity = 1.0
        peak   = 1.0
        max_dd = 0.0
        equity_curve = [1.0]
        for t in take_trades:
            equity *= 1 + t["r_return"] * self.risk_pct / 100
            equity_curve.append(equity)
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd

        total_return = (equity - 1.0) * 100

        return {
            "total_windows": len(trades),
            "total_trades":  len(take_trades),
            "closed_trades": len(closed),
            "wins":          len(wins),
            "losses":        len(losses),
            "open_trades":   len(open_trades),
            "win_rate":      round(win_rate * 100, 2) if win_rate is not None else None,
            "sharpe":        round(sharpe, 4)          if sharpe  is not None else None,
            "max_drawdown":  round(max_dd * 100, 2),
            "total_return":  round(total_return, 2),
            "equity_curve":  equity_curve,
        }

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self) -> "BacktestEngine":
        """Execute walk-forward on train and validation splits."""
        if not self.all_data:
            self.fetch_and_split()

        print(f"\n{'─'*60}")
        print("TRAIN walk-forward")
        print(f"{'─'*60}")
        train_trades = self._walk_forward(0, self.train_end, "TRAIN")

        print(f"\n{'─'*60}")
        print("VALIDATION walk-forward")
        print(f"{'─'*60}")
        val_trades = self._walk_forward(self.train_end, self.val_end, "VAL")

        self.results = {
            "train": {
                "trades":  train_trades,
                "metrics": self._compute_metrics(train_trades),
            },
            "val": {
                "trades":  val_trades,
                "metrics": self._compute_metrics(val_trades),
            },
        }
        return self

    # ------------------------------------------------------------------
    # Robust runner — auto-lowers WEAK thresholds if trades insufficient
    # ------------------------------------------------------------------

    def run_robust(self) -> "BacktestEngine":
        """
        Run walk-forward. If total closed trades across train + val is below
        MIN_TRADES_TARGET (20), automatically raise the WEAK signal RSI band
        to 50/50 and re-run with a clear log message.

        If still below INSUFFICIENT_FLOOR (10) after the retry, the report
        will print an "INSUFFICIENT DATA" warning instead of metrics.
        """
        self.run()

        total_closed = (
            self.results["train"]["metrics"]["closed_trades"] +
            self.results["val"]["metrics"]["closed_trades"]
        )

        if total_closed < self.MIN_TRADES_TARGET and not self.threshold_lowered:
            print(f"\n{'!'*60}")
            print(f"  ⚠  Only {total_closed} closed trades — target is "
                  f"{self.MIN_TRADES_TARGET}.")
            print(f"  Lowering WEAK thresholds:")
            print(f"    WEAK BUY  RSI < {self.weak_buy_rsi:.0f}  →  RSI < 50")
            print(f"    WEAK SELL RSI > {self.weak_sell_rsi:.0f}  →  RSI > 50")
            print(f"  Re-running with relaxed thresholds...")
            print(f"{'!'*60}\n")

            self.weak_buy_rsi      = 50.0
            self.weak_sell_rsi     = 50.0
            self.threshold_lowered = True
            self.run()

        return self

    # ------------------------------------------------------------------
    # Overfitting check
    # ------------------------------------------------------------------

    def check_overfit(self) -> tuple:
        """
        Returns (is_overfit: bool | None, explanation: str).
        None means insufficient data to judge.
        """
        tm = self.results["train"]["metrics"]
        vm = self.results["val"]["metrics"]
        tw = tm.get("win_rate")
        vw = vm.get("win_rate")

        if tw is None and vw is None:
            return None, "No closed trades in either split — cannot assess overfitting."
        if tw is None:
            return None, f"No closed train trades  (val win_rate={vw}%)."
        if vw is None:
            return None, f"No closed val trades  (train win_rate={tw}%)."

        drop_pp = tw - vw          # percentage-point drop
        threshold_pp = self.overfit_threshold * 100
        is_overfit = drop_pp > threshold_pp
        return is_overfit, (
            f"Train {tw:.1f}%  →  Val {vw:.1f}%  "
            f"(drop = {drop_pp:.1f} pp  |  threshold = {threshold_pp:.0f} pp)"
        )

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    def report(self) -> "BacktestEngine":
        """Print a formatted summary report to stdout."""
        if not self.results:
            print("No results yet — call .run() first.")
            return self

        tm = self.results["train"]["metrics"]
        vm = self.results["val"]["metrics"]
        is_overfit, overfit_str = self.check_overfit()

        def _fmt_wr(v):
            return f"{v:.1f}%" if v is not None else "N/A (no closed trades)"

        def _fmt_f(v, suffix=""):
            return f"{v:.4f}{suffix}" if v is not None else "N/A"

        W = 62
        total_closed = tm["closed_trades"] + vm["closed_trades"]

        print(f"\n{'='*W}")
        print(f"  BACKTEST REPORT — {self.symbol} {self.interval}")
        print(f"  Account: ${self.account_balance:,.0f}  |  Risk: {self.risk_pct}%/trade"
              f"  |  Min R:R: {self.min_rr}")
        if self.threshold_lowered:
            print(f"  ⚠  THRESHOLDS LOWERED: WEAK RSI band relaxed to 50/50")
            print(f"     (original 45/55 produced < {self.MIN_TRADES_TARGET} closed trades)")
        print(f"{'='*W}")
        print(f"  Total closed trades (train + val): {total_closed}", end="")
        if total_closed < self.INSUFFICIENT_FLOOR:
            print(f"  ← INSUFFICIENT DATA — results unreliable")
        elif total_closed < self.MIN_TRADES_TARGET:
            print(f"  ← below target of {self.MIN_TRADES_TARGET} — interpret with caution")
        else:
            print(f"  ← sufficient for analysis")

        header = f"  {'Metric':<22} {'Train':>15} {'Validation':>15}"
        print(header)
        print(f"  {'─'*58}")

        rows = [
            ("Windows evaluated",  tm["total_windows"],  vm["total_windows"]),
            ("TAKE_TRADE signals", tm["total_trades"],   vm["total_trades"]),
            ("Closed trades",      tm["closed_trades"],  vm["closed_trades"]),
            ("  Wins",             tm["wins"],            vm["wins"]),
            ("  Losses",           tm["losses"],          vm["losses"]),
            ("  Open (timeout)",   tm["open_trades"],     vm["open_trades"]),
        ]
        for label, tv, vv in rows:
            print(f"  {label:<22} {str(tv):>15} {str(vv):>15}")

        print(f"  {'─'*58}")

        metric_rows = [
            ("Win rate",       _fmt_wr(tm["win_rate"]),          _fmt_wr(vm["win_rate"])),
            ("Sharpe ratio",   _fmt_f(tm["sharpe"]),             _fmt_f(vm["sharpe"])),
            ("Max drawdown",   _fmt_f(tm["max_drawdown"], "%"),  _fmt_f(vm["max_drawdown"], "%")),
            ("Total return",   _fmt_f(tm["total_return"], "%"),  _fmt_f(vm["total_return"], "%")),
        ]
        for label, tv, vv in metric_rows:
            print(f"  {label:<22} {tv:>15} {vv:>15}")

        print(f"  {'─'*58}")

        # Overfitting verdict
        if is_overfit is None:
            verdict = "UNKNOWN"
            color   = ""
        elif is_overfit:
            verdict = "⚠  OVERFIT DETECTED"
            color   = ""
        else:
            verdict = "✓  No overfitting"
            color   = ""

        print(f"\n  Overfitting check : {verdict}")
        print(f"  {overfit_str}")
        print(f"\n  Test set          : UNTOUCHED (run manually when ready)")
        print(f"{'='*W}\n")
        return self


# ---------------------------------------------------------------------------
# Standalone test-set runner (call explicitly when you are ready)
# ---------------------------------------------------------------------------

def run_on_test(engine: BacktestEngine) -> dict:
    """
    Run the walk-forward evaluation on the HELD-OUT test set.

    *** Only call this once, when you are done developing. ***
    Calling it multiple times invalidates the sanctity of the test set.
    """
    if not engine.all_data:
        raise RuntimeError("Engine has no data. Call fetch_and_split() first.")
    if not engine.results:
        raise RuntimeError("Engine has not been run on train/val yet. Call run() first.")

    print("\n" + "!"*62)
    print("  RUNNING ON TEST SET — this should only happen once.")
    print("!"*62)

    test_trades  = engine._walk_forward(engine.val_end, len(engine.all_data), "TEST")
    test_metrics = engine._compute_metrics(test_trades)
    engine.results["test"] = {"trades": test_trades, "metrics": test_metrics}

    tm = engine.results["train"]["metrics"]
    vm = engine.results["val"]["metrics"]
    tst = test_metrics

    print(f"\n  {'Metric':<22} {'Train':>12} {'Val':>12} {'TEST':>12}")
    print(f"  {'─'*58}")
    for label, tk, vk in [
        ("Win rate",     "win_rate",     "win_rate"),
        ("Sharpe",       "sharpe",       "sharpe"),
        ("Max drawdown", "max_drawdown", "max_drawdown"),
    ]:
        tv  = f"{tm[tk]:.2f}%" if tm[tk]  is not None else "N/A"
        vv  = f"{vm[vk]:.2f}%" if vm[vk]  is not None else "N/A"
        tstv = f"{tst[vk]:.2f}%" if tst[vk] is not None else "N/A"
        print(f"  {label:<22} {tv:>12} {vv:>12} {tstv:>12}")

    return test_metrics


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    symbols = sys.argv[1:] if len(sys.argv) > 1 else ["BTCUSDT", "ETHUSDT"]

    for symbol in symbols:
        print(f"\n{'#'*62}")
        print(f"#  {symbol}")
        print(f"{'#'*62}")

        engine = BacktestEngine(
            symbol          = symbol,
            interval        = "1h",
            total_candles   = 3000,
            account_balance = 1000.0,
            risk_pct        = 1.0,
            min_rr          = 2.0,
            weak_buy_rsi    = 45.0,
            weak_sell_rsi   = 55.0,
        )

        engine.fetch_and_split()
        engine.run_robust()
        engine.report()
