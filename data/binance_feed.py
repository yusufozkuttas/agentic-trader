"""
data/binance_feed.py
====================
Binance data feed: REST (ticker + OHLCV) and liquidation WebSocket.

REST functions are synchronous and use only the standard library.
The WebSocket liquidation stream runs in a background thread.

Exported
--------
fetch_ticker(symbol)          -> dict   (Binance 24hr ticker payload)
fetch_klines(symbol, interval, limit) -> list[list]  (raw kline rows)
fetch_ohlcv(symbol, interval, limit)  -> list[dict]  (parsed OHLCV dicts)
LiquidationStream             -> background thread, stores recent liquidations
"""

import json
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE = "https://api.binance.com"
_TICKER_URL  = _BASE + "/api/v3/ticker/24hr?symbol={symbol}"
_KLINES_URL  = _BASE + "/api/v3/klines"
_WS_BASE     = "wss://fstream.binance.com/stream?streams={stream}"

# Milliseconds per interval — used for paginated fetches
_INTERVAL_MS: dict[str, int] = {
    "1m":  60_000,
    "3m":  180_000,
    "5m":  300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h":  3_600_000,
    "2h":  7_200_000,
    "4h":  14_400_000,
    "6h":  21_600_000,
    "8h":  28_800_000,
    "12h": 43_200_000,
    "1d":  86_400_000,
    "1w":  604_800_000,
}

_BATCH_SIZE = 1000   # Binance hard cap per request


# ---------------------------------------------------------------------------
# REST — ticker
# ---------------------------------------------------------------------------

def fetch_ticker(symbol: str) -> dict:
    """
    Return the Binance 24hr rolling ticker for *symbol*.

    Keys include: symbol, lastPrice, priceChangePercent, volume, highPrice,
    lowPrice, quoteVolume, openPrice.

    Raises urllib.error.URLError / http.client.HTTPException on network errors.
    """
    url = _TICKER_URL.format(symbol=symbol.upper())
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# REST — klines / OHLCV
# ---------------------------------------------------------------------------

def fetch_klines(
    symbol: str,
    interval: str = "1h",
    limit: int = 200,
    start_time: int | None = None,
) -> list:
    """
    Return raw Binance kline rows (list of lists).

    Each row:
        [open_time, open, high, low, close, volume,
         close_time, quote_asset_volume, num_trades,
         taker_buy_base_vol, taker_buy_quote_vol, ignore]

    All price/volume values are strings — convert as needed.

    Parameters
    ----------
    symbol     : e.g. "BTCUSDT"
    interval   : Binance interval string — "1m", "5m", "15m", "1h", "4h", "1d" …
    limit      : number of candles (max 1000)
    start_time : optional Unix timestamp in milliseconds; if provided, returns
                 candles starting from this time instead of the most recent ones
    """
    params: dict = {
        "symbol":   symbol.upper(),
        "interval": interval,
        "limit":    min(limit, _BATCH_SIZE),
    }
    if start_time is not None:
        params["startTime"] = start_time
    url = _KLINES_URL + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


def fetch_klines_extended(
    symbol: str,
    interval: str = "1h",
    total: int = 3000,
    batch_size: int = _BATCH_SIZE,
) -> list:
    """
    Fetch more than 1000 klines by paginating backwards with startTime.

    Binance caps each request at 1000 candles. This function issues multiple
    requests — each starting where the previous one left off — and stitches
    the results together in chronological order.

    Parameters
    ----------
    symbol     : e.g. "BTCUSDT"
    interval   : e.g. "1h"
    total      : total candles to return (e.g. 3000 → ~125 days of 1h data)
    batch_size : candles per request, capped at 1000

    Returns a list of raw kline rows (same format as fetch_klines), sorted
    oldest → newest, deduplicated by open_time.
    """
    if interval not in _INTERVAL_MS:
        raise ValueError(
            f"Unknown interval '{interval}'. "
            f"Known intervals: {sorted(_INTERVAL_MS)}"
        )

    interval_ms = _INTERVAL_MS[interval]
    batch_size  = min(batch_size, _BATCH_SIZE)

    # Compute earliest start time we need
    now_ms      = int(time.time() * 1000)
    start_ms    = now_ms - total * interval_ms

    all_rows: list = []
    current_start = start_ms

    while len(all_rows) < total:
        remaining = total - len(all_rows)
        this_batch = min(batch_size, remaining)

        rows = fetch_klines(symbol, interval, this_batch, start_time=current_start)
        if not rows:
            break   # no more data from Binance

        all_rows.extend(rows)

        # Advance start time to one interval after the last returned candle
        last_open_time = rows[-1][0]
        current_start  = last_open_time + interval_ms

        # If Binance returned fewer rows than requested, we've hit the present
        if len(rows) < this_batch:
            break

        # Small pause to be polite to the API (stays well within rate limits)
        time.sleep(0.12)

    # Deduplicate by open_time (first field) in case batches overlap
    seen: set = set()
    deduped: list = []
    for row in all_rows:
        if row[0] not in seen:
            seen.add(row[0])
            deduped.append(row)

    # Sort chronologically and trim to requested total
    deduped.sort(key=lambda r: r[0])
    return deduped[-total:]


def fetch_ohlcv(symbol: str, interval: str = "1h", limit: int = 200) -> list:
    """
    Return OHLCV data as a list of dicts with typed values.

    Each dict:
        open_time  : datetime
        open       : float
        high       : float
        low        : float
        close      : float
        volume     : float      (base asset)
        close_time : datetime
        num_trades : int
    """
    return _parse_klines(fetch_klines(symbol, interval, limit))


def _parse_klines(raw: list) -> list:
    """Shared parser: raw kline rows → list of OHLCV dicts."""
    result = []
    for row in raw:
        result.append({
            "open_time":  datetime.fromtimestamp(row[0] / 1000),
            "open":       float(row[1]),
            "high":       float(row[2]),
            "low":        float(row[3]),
            "close":      float(row[4]),
            "volume":     float(row[5]),
            "close_time": datetime.fromtimestamp(row[6] / 1000),
            "num_trades": int(row[8]),
        })
    return result


def fetch_ohlcv_extended(
    symbol: str,
    interval: str = "1h",
    total: int = 3000,
) -> list:
    """
    Fetch more than 1000 candles and return parsed OHLCV dicts.

    Paginates Binance using startTime so the 1000-row cap is bypassed.
    Prints progress to stdout (one dot per batch) so callers know it's working.

    Parameters
    ----------
    symbol   : e.g. "BTCUSDT"
    interval : e.g. "1h"
    total    : total candles to return (3000 → ~125 days of 1h data)
    """
    batches_needed = -(-total // _BATCH_SIZE)   # ceiling division
    print(f"  Paginating {total} candles in {batches_needed} batch(es) "
          f"[{interval}]", end="", flush=True)

    raw = fetch_klines_extended(symbol, interval, total)
    print(f"  → {len(raw)} candles fetched")
    return _parse_klines(raw)


# ---------------------------------------------------------------------------
# WebSocket — liquidation stream
# ---------------------------------------------------------------------------

class LiquidationStream:
    """
    Subscribe to Binance futures liquidation orders for one or more symbols
    and store the most recent events in memory.

    Uses the standard library `ssl` + manual WebSocket framing so that no
    third-party packages are required.

    Usage
    -----
        stream = LiquidationStream(["BTCUSDT", "ETHUSDT"])
        stream.start()
        ...
        events = stream.get_recent(symbol="BTCUSDT", n=5)
        stream.stop()
    """

    def __init__(self, symbols: list, max_per_symbol: int = 100):
        self._symbols = [s.lower() for s in symbols]
        self._max = max_per_symbol
        self._events: dict[str, list] = {s: [] for s in self._symbols}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        """Start the background listener thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """Signal the background thread to exit."""
        self._stop_event.set()

    def get_recent(self, symbol: str, n: int = 10) -> list:
        """
        Return up to *n* most-recent liquidation events for *symbol*.

        Each event dict:
            symbol      : str
            side        : "BUY" | "SELL"   (BUY = short liquidation)
            price       : float
            quantity    : float
            usd_value   : float
            timestamp   : datetime
        """
        key = symbol.lower()
        with self._lock:
            return list(self._events.get(key, []))[-n:]

    def get_all(self) -> dict:
        """Return a copy of all stored events keyed by symbol (lowercase)."""
        with self._lock:
            return {k: list(v) for k, v in self._events.items()}

    # ------------------------------------------------------------------
    # Internal WebSocket implementation (stdlib only)
    # ------------------------------------------------------------------

    def _run(self):
        import socket
        import ssl
        import base64
        import hashlib
        import os

        streams = "/".join(f"{s}@forceOrder" for s in self._symbols)

        while not self._stop_event.is_set():
            try:
                self._connect_and_listen(streams, socket, ssl, base64, hashlib, os)
            except Exception:
                # Reconnect after a short pause unless stopped
                if not self._stop_event.is_set():
                    time.sleep(5)

    def _connect_and_listen(self, streams, socket, ssl, base64, hashlib, os):
        host = "fstream.binance.com"
        path = f"/stream?streams={streams}"
        port = 443

        ctx = ssl.create_default_context()
        raw_sock = socket.create_connection((host, port), timeout=10)
        sock = ctx.wrap_socket(raw_sock, server_hostname=host)

        # WebSocket handshake
        key_bytes = base64.b64encode(os.urandom(16))
        ws_key = key_bytes.decode()
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(handshake.encode())

        # Read HTTP response headers
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket handshake failed")
            resp += chunk

        # Frame receive loop
        buf = resp[resp.index(b"\r\n\r\n") + 4:]
        sock.settimeout(30)

        while not self._stop_event.is_set():
            buf = self._recv_frame(sock, buf)
            # buf after _recv_frame: remaining bytes after the frame
            # _recv_frame internally processes the payload
            buf = self._process_frames(sock, buf)

    def _recv_frame(self, sock, buf: bytes) -> bytes:
        """Receive bytes until we have at least one complete WebSocket frame."""
        while True:
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("Socket closed")
                buf += chunk
            except TimeoutError:
                pass  # Try again
            # Try to parse all complete frames in the buffer
            buf = self._process_frames_inner(buf)
            if len(buf) == 0 or (len(buf) >= 2):
                return buf

    def _process_frames(self, sock, buf: bytes) -> bytes:
        return self._process_frames_inner(buf)

    def _process_frames_inner(self, buf: bytes) -> bytes:
        """Parse and consume all complete frames in *buf*, return leftover bytes."""
        while len(buf) >= 2:
            b0, b1 = buf[0], buf[1]
            opcode = b0 & 0x0F
            masked = (b1 & 0x80) != 0
            payload_len = b1 & 0x7F

            header_len = 2
            if payload_len == 126:
                if len(buf) < 4:
                    break
                payload_len = int.from_bytes(buf[2:4], "big")
                header_len = 4
            elif payload_len == 127:
                if len(buf) < 10:
                    break
                payload_len = int.from_bytes(buf[2:10], "big")
                header_len = 10

            if masked:
                header_len += 4

            total_len = header_len + payload_len
            if len(buf) < total_len:
                break

            payload = buf[header_len:total_len]
            if masked:
                mask = buf[header_len - 4:header_len]
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == 0x1:  # text frame
                try:
                    self._handle_message(payload.decode("utf-8"))
                except Exception:
                    pass
            # opcode 0x8 = close, 0x9 = ping — ignore for simplicity

            buf = buf[total_len:]

        return buf

    def _handle_message(self, text: str):
        data = json.loads(text)
        # Stream envelope: {"stream": "btcusdt@forceOrder", "data": {...}}
        payload = data.get("data", data)
        order = payload.get("o", {})
        if not order:
            return

        symbol = order.get("s", "").lower()
        event = {
            "symbol":    order.get("s", ""),
            "side":      order.get("S", ""),   # BUY = short liquidated
            "price":     float(order.get("p", 0)),
            "quantity":  float(order.get("q", 0)),
            "usd_value": float(order.get("p", 0)) * float(order.get("q", 0)),
            "timestamp": datetime.fromtimestamp(order.get("T", 0) / 1000),
        }

        with self._lock:
            if symbol in self._events:
                self._events[symbol].append(event)
                if len(self._events[symbol]) > self._max:
                    self._events[symbol].pop(0)


# ---------------------------------------------------------------------------
# Quick self-test (python -m data.binance_feed  or  python data/binance_feed.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"

    print(f"[1] fetch_ticker({symbol})")
    ticker = fetch_ticker(symbol)
    print(f"    last price : {float(ticker['lastPrice']):,.2f}")
    print(f"    24h change : {float(ticker['priceChangePercent']):+.2f}%")
    print(f"    24h volume : {float(ticker['quoteVolume']):,.0f} USDT")

    print(f"\n[2] fetch_klines({symbol}, '1h', 5)  — last 5 rows")
    klines = fetch_klines(symbol, "1h", 5)
    for row in klines:
        ts = datetime.fromtimestamp(row[0] / 1000).strftime("%Y-%m-%d %H:%M")
        print(f"    {ts}  O={float(row[1]):,.2f}  H={float(row[2]):,.2f}"
              f"  L={float(row[3]):,.2f}  C={float(row[4]):,.2f}"
              f"  V={float(row[5]):,.2f}")

    print(f"\n[3] fetch_ohlcv({symbol}, '4h', 3)  — last 3 parsed rows")
    ohlcv = fetch_ohlcv(symbol, "4h", 3)
    for bar in ohlcv:
        print(f"    {bar['open_time'].strftime('%Y-%m-%d %H:%M')}"
              f"  O={bar['open']:,.2f}  H={bar['high']:,.2f}"
              f"  L={bar['low']:,.2f}  C={bar['close']:,.2f}"
              f"  V={bar['volume']:,.2f}")

    print(f"\n[4] LiquidationStream(['BTCUSDT']) — listen for 8 seconds")
    stream = LiquidationStream(["BTCUSDT"])
    stream.start()
    for i in range(8):
        time.sleep(1)
        events = stream.get_recent("BTCUSDT")
        print(f"    t+{i+1}s  liquidations received: {len(events)}", end="\r")
    stream.stop()
    final = stream.get_recent("BTCUSDT")
    print(f"\n    Done. Total liquidation events captured: {len(final)}")
    for ev in final[-3:]:
        print(f"      {ev['timestamp'].strftime('%H:%M:%S')}  {ev['side']:<4}"
              f"  qty={ev['quantity']}  val=${ev['usd_value']:,.0f}")

    print("\nAll checks passed.")
