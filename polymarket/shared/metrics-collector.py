#!/usr/bin/env python3
"""
Shared metrics collector — always-on daemon that polls Binance REST every 1s,
computes technical indicators, and writes:
  1. Live snapshot: shared/metrics-live/{asset}.json  (consumers read this)
  2. History:       shared/metrics-history/{asset}/{YYYY-MM-DD}.jsonl  (analytics)

No LLM calls. No trading logic. Pure data pipeline.
"""

import asyncio, aiohttp, json, math, os, sys, time, signal
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────

ASSETS = {
    "btc": {"symbol": "BTCUSDT"},
    "eth": {"symbol": "ETHUSDT"},
}

POLL_INTERVAL = 1.0  # seconds
BINANCE_BASE = "https://api.binance.com"

BASE_DIR = Path(__file__).resolve().parent
LIVE_DIR = BASE_DIR / "metrics-live"
HIST_DIR = BASE_DIR / "metrics-history"

# ── Indicator math (extracted from reasoning-loop.py) ───────────────────────

def compute_indicators(closes, highs, lows, volumes):
    """Compute all technical indicators from OHLCV arrays. Returns dict."""
    ta = {}
    if len(closes) < 6:
        return ta

    # RSI(6)
    if len(closes) >= 7:
        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            gains.append(max(0, diff))
            losses.append(max(0, -diff))
        period = min(6, len(gains))
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            ta["rsi_6"] = 100.0
        else:
            rs = avg_gain / avg_loss
            ta["rsi_6"] = round(100 - (100 / (1 + rs)), 1)

    # RSI(14)
    if len(closes) >= 15:
        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            gains.append(max(0, diff))
            losses.append(max(0, -diff))
        period = 14
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            ta["rsi_14"] = 100.0
        else:
            rs = avg_gain / avg_loss
            ta["rsi_14"] = round(100 - (100 / (1 + rs)), 1)

    # EMA 9 / 21
    def ema(data, period):
        if len(data) < period:
            return sum(data) / len(data)
        mult = 2 / (period + 1)
        val = sum(data[:period]) / period
        for d in data[period:]:
            val = (d - val) * mult + val
        return val

    if len(closes) >= 5:
        ema9 = ema(closes, min(9, len(closes)))
        ema21 = ema(closes, min(21, len(closes)))
        ta["ema_fast"] = round(ema9, 2)
        ta["ema_slow"] = round(ema21, 2)
        ta["ema_cross"] = "bullish" if ema9 > ema21 else "bearish"

    # VWAP
    if volumes and sum(volumes) > 0:
        typical_prices = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
        cum_tp_vol = sum(tp * v for tp, v in zip(typical_prices, volumes))
        cum_vol = sum(volumes)
        vwap = cum_tp_vol / cum_vol
        ta["vwap"] = round(vwap, 2)
        ta["vwap_delta"] = round(closes[-1] - vwap, 2)

    # Bollinger Bands
    if len(closes) >= 8:
        period = min(len(closes), 10)
        recent = closes[-period:]
        sma = sum(recent) / len(recent)
        std = (sum((x - sma) ** 2 for x in recent) / len(recent)) ** 0.5
        upper = sma + 2 * std
        lower = sma - 2 * std
        ta["bb_upper"] = round(upper, 2)
        ta["bb_lower"] = round(lower, 2)
        ta["bb_width"] = round(upper - lower, 2)
        ta["bb_position"] = round((closes[-1] - lower) / (upper - lower) * 100, 1) if upper != lower else 50.0

    # Hurst exponent
    if len(closes) >= 8:
        returns = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        n = len(returns)
        mean_r = sum(returns) / n
        devs = [r - mean_r for r in returns]
        cum_devs = []
        s = 0
        for d in devs:
            s += d
            cum_devs.append(s)
        R = max(cum_devs) - min(cum_devs)
        S = (sum(d ** 2 for d in devs) / n) ** 0.5
        if S > 0 and R > 0:
            ta["hurst"] = round(math.log(R / S) / math.log(n), 3)

    # ADX / DI+ / DI- / ATR
    if len(highs) >= 10 and len(lows) >= 10:
        period = min(14, len(highs) - 1)
        plus_dm, minus_dm, tr_list = [], [], []
        for i in range(1, len(highs)):
            h_diff = highs[i] - highs[i-1]
            l_diff = lows[i-1] - lows[i]
            plus_dm.append(h_diff if h_diff > l_diff and h_diff > 0 else 0)
            minus_dm.append(l_diff if l_diff > h_diff and l_diff > 0 else 0)
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
            tr_list.append(tr)
        if len(tr_list) >= period:
            atr_s = sum(tr_list[:period]) / period
            pdm_s = sum(plus_dm[:period]) / period
            mdm_s = sum(minus_dm[:period]) / period
            for i in range(period, len(tr_list)):
                atr_s = (atr_s * (period - 1) + tr_list[i]) / period
                pdm_s = (pdm_s * (period - 1) + plus_dm[i]) / period
                mdm_s = (mdm_s * (period - 1) + minus_dm[i]) / period
            di_plus = (pdm_s / atr_s * 100) if atr_s > 0 else 0
            di_minus = (mdm_s / atr_s * 100) if atr_s > 0 else 0
            di_sum = di_plus + di_minus
            dx = abs(di_plus - di_minus) / di_sum * 100 if di_sum > 0 else 0
            ta["adx"] = round(dx, 1)
            ta["di_plus"] = round(di_plus, 1)
            ta["di_minus"] = round(di_minus, 1)
            ta["atr"] = round(atr_s, 2)
            avg_tr = sum(tr_list) / len(tr_list)
            ta["atr_ratio"] = round(atr_s / avg_tr, 2) if avg_tr > 0 else 1.0

    # Choppiness Index
    if len(highs) >= 10 and len(lows) >= 10:
        ci_period = min(14, len(highs) - 1)
        tr_vals = []
        for i in range(1, len(highs)):
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
            tr_vals.append(tr)
        if len(tr_vals) >= ci_period:
            atr_sum = sum(tr_vals[-ci_period:])
            highest = max(highs[-ci_period:])
            lowest = min(lows[-ci_period:])
            hl_range = highest - lowest
            if hl_range > 0 and atr_sum > 0:
                ta["choppiness"] = round(100 * math.log10(atr_sum / hl_range) / math.log10(ci_period), 1)

    # Volume (latest 1m candle)
    if volumes:
        ta["volume_1m"] = round(volumes[-1], 4)

    return ta


# ── Data fetching ───────────────────────────────────────────────────────────

async def fetch_klines(session, symbol, limit=35, interval="1m"):
    """Fetch klines from Binance REST."""
    url = f"{BINANCE_BASE}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                opens = [float(k[1]) for k in data]
                highs = [float(k[2]) for k in data]
                lows = [float(k[3]) for k in data]
                closes = [float(k[4]) for k in data]
                volumes = [float(k[5]) for k in data]
                return opens, highs, lows, closes, volumes
    except Exception:
        pass
    return None, None, None, None, None

async def fetch_ticker(session, symbol):
    """Fetch latest price from Binance ticker."""
    url = f"{BINANCE_BASE}/api/v3/ticker/price?symbol={symbol}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return float(data["price"])
    except Exception:
        pass
    return None

async def fetch_orderbook(session, symbol, depth=20):
    """Fetch orderbook snapshot for imbalance calculation."""
    url = f"{BINANCE_BASE}/api/v3/depth?symbol={symbol}&limit={depth}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            if resp.status == 200:
                data = await resp.json()
                bid_vol = sum(float(b[1]) for b in data["bids"])
                ask_vol = sum(float(a[1]) for a in data["asks"])
                total = bid_vol + ask_vol
                return round(bid_vol / total, 3) if total > 0 else 0.5
    except Exception:
        pass
    return None


# ── Write functions ─────────────────────────────────────────────────────────

def write_live(asset, snapshot):
    """Atomic write to live snapshot file."""
    path = LIVE_DIR / f"{asset}.json"
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(snapshot, f)
    os.replace(tmp, path)

def append_history(asset, snapshot):
    """Append one line to today's JSONL history."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_dir = HIST_DIR / asset / today
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / "metrics.jsonl"
    with open(path, "a") as f:
        f.write(json.dumps(snapshot) + "\n")


# ── Main loop ───────────────────────────────────────────────────────────────

async def collect_asset(session, asset, config):
    """One tick of collection for a single asset."""
    symbol = config["symbol"]

    # Fetch 1m klines, 5m klines, ticker, and orderbook in parallel
    klines_1m_task = fetch_klines(session, symbol, 35)
    klines_5m_task = fetch_klines(session, symbol, 30, interval="5m")
    ticker_task = fetch_ticker(session, symbol)
    ob_task = fetch_orderbook(session, symbol)

    (o1, h1, l1, c1, v1), (o5, h5, l5, c5, v5), price, ob_imbalance = await asyncio.gather(
        klines_1m_task, klines_5m_task, ticker_task, ob_task
    )

    if c1 is None or price is None:
        return None

    # Compute indicators on 1m candles
    ta_1m = compute_indicators(c1, h1, l1, v1)

    # Compute indicators on 5m candles (prefixed with 5m_)
    ta_5m = {}
    if c5 is not None:
        raw_5m = compute_indicators(c5, h5, l5, v5)
        ta_5m = {f"5m_{k}": v for k, v in raw_5m.items()}

    # Build snapshot
    now = time.time()
    snapshot = {
        "ts": round(now, 3),
        "price": price,
        **ta_1m,
        **ta_5m,
    }
    if ob_imbalance is not None:
        snapshot["ob_imbalance"] = ob_imbalance

    return snapshot


async def main():
    LIVE_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[metrics-collector] Starting — assets: {list(ASSETS.keys())}, interval: {POLL_INTERVAL}s")
    print(f"[metrics-collector] Live: {LIVE_DIR}")
    print(f"[metrics-collector] History: {HIST_DIR}")

    tick = 0
    async with aiohttp.ClientSession() as session:
        while True:
            t0 = time.time()
            for asset, config in ASSETS.items():
                try:
                    snapshot = await collect_asset(session, asset, config)
                    if snapshot:
                        write_live(asset, snapshot)
                        append_history(asset, snapshot)
                except Exception as e:
                    print(f"[metrics-collector] {asset} error: {e}")

            tick += 1
            if tick % 60 == 0:
                assets_ok = []
                for a in ASSETS:
                    p = LIVE_DIR / f"{a}.json"
                    if p.exists():
                        d = json.load(open(p))
                        assets_ok.append(f"{a}=${d['price']:.0f}")
                print(f"[metrics-collector] tick={tick} {' '.join(assets_ok)}")

            elapsed = time.time() - t0
            await asyncio.sleep(max(0, POLL_INTERVAL - elapsed))


if __name__ == "__main__":
    # Graceful shutdown
    loop = asyncio.new_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: sys.exit(0))
    try:
        loop.run_until_complete(main())
    except (KeyboardInterrupt, SystemExit):
        print("[metrics-collector] Shutting down")
