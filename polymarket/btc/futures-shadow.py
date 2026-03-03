#!/usr/bin/env python3
"""
Futures Shadow Observer — runs alongside the sniper, never trades.

Connects to Binance + Bybit futures websockets and logs:
- Futures-spot spread (perp vs spot price)
- Liquidation events (direction, size)
- Open Interest changes
- Funding rate

Every 5-min window, records all signals and grades them against
the actual BTC outcome (from Chainlink strike prices).

Output: futures-shadow.jsonl (one JSON line per window)
Dashboard: futures-shadow-report.py (run after collecting data)
"""

import asyncio
import json
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request

# ─── Config ───
BOT_DIR = Path(__file__).parent
LOG_DIR = BOT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
DATA_FILE = LOG_DIR / "futures-shadow.jsonl"

CHAINLINK_FEED_ID = "0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8"
CHAINLINK_API = "https://data.chain.link/api/query-timescale"

# Track prices — large deques to survive 5+ min windows
spot_prices = {}       # exchange -> deque of (timestamp, price)
futures_prices = {}    # exchange -> deque of (timestamp, price)
liquidations = []      # (timestamp, side, size_usd, exchange)
oi_snapshots = []      # (timestamp, oi_value, exchange)

# Per-window snapshots (captured live, analyzed after close)
window_snapshots = {}  # window_start -> {spreads: [], liqs: []}

# Current window state
current_window = None
window_data = {
    "spot_samples": [],
    "futures_samples": [],
    "spreads": [],
    "liquidations": [],
    "oi_start": None,
    "oi_end": None,
    "funding_rate": None,
}

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"  [{ts}] {msg}", flush=True)

def fetch_json(url, timeout=5):
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None

def get_window_time():
    """Get current 5-min window start as epoch."""
    now = time.time()
    return int(now // 300) * 300

def get_chainlink_price(target_ts):
    """Get BTC price from Chainlink at a specific timestamp."""
    try:
        url = f"{CHAINLINK_API}?query=LIVE_STREAM_REPORTS_QUERY&variables=%7B%22feedId%22%3A%22{CHAINLINK_FEED_ID}%22%7D"
        data = fetch_json(url)
        if not data or "data" not in data:
            return None
        nodes = data["data"].get("liveStreamReports", {}).get("nodes", [])
        best_price, best_dist = None, float("inf")
        for n in nodes[:60]:
            ts = datetime.fromisoformat(n["validFromTimestamp"].replace("Z", "+00:00")).timestamp()
            d = abs(ts - target_ts)
            if d < best_dist:
                best_dist = d
                try:
                    best_price = float(n["price"]) / 1e18
                except (ValueError, TypeError):
                    pass
        return best_price
    except Exception:
        return None

def get_funding_rate():
    """Get current Binance BTCUSDT perp funding rate."""
    data = fetch_json("https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=1")
    if data and len(data) > 0:
        try:
            return float(data[0]["fundingRate"])
        except (ValueError, TypeError, KeyError):
            pass
    return None

def get_open_interest():
    """Get current Binance BTCUSDT perp open interest in USD."""
    data = fetch_json("https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT")
    if data:
        try:
            oi_btc = float(data["openInterest"])
            # Get mark price for USD conversion
            mark = fetch_json("https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT")
            if mark:
                price = float(mark["markPrice"])
                return oi_btc * price
        except (ValueError, TypeError, KeyError):
            pass
    return None

def get_spot_price():
    """Get Binance spot BTC price."""
    data = fetch_json("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
    if data:
        try:
            return float(data["price"])
        except (ValueError, TypeError):
            pass
    return None

# ─── Websocket Handlers ───

async def binance_futures_trades():
    """Binance BTCUSDT perpetual trades."""
    import websockets
    url = "wss://fstream.binance.com/ws/btcusdt@aggTrade"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                log("Binance futures connected")
                async for msg in ws:
                    try:
                        d = json.loads(msg)
                        price = float(d["p"])
                        qty = float(d["q"])
                        ts = time.time()
                        if "futures_binance" not in futures_prices:
                            futures_prices["futures_binance"] = deque(maxlen=500)
                        futures_prices["futures_binance"].append((ts, price, qty))
                    except (ValueError, TypeError, KeyError):
                        pass
        except Exception as e:
            log(f"Binance futures reconnecting: {e}")
            await asyncio.sleep(3)

async def binance_spot_trades():
    """Binance BTCUSDT spot trades."""
    import websockets
    url = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                log("Binance spot connected")
                async for msg in ws:
                    try:
                        d = json.loads(msg)
                        price = float(d["p"])
                        ts = time.time()
                        if "spot_binance" not in spot_prices:
                            spot_prices["spot_binance"] = deque(maxlen=500)
                        spot_prices["spot_binance"].append((ts, price))
                    except (ValueError, TypeError, KeyError):
                        pass
        except Exception as e:
            log(f"Binance spot reconnecting: {e}")
            await asyncio.sleep(3)

async def bybit_futures():
    """Bybit BTCUSDT perpetual trades + liquidations."""
    import websockets
    url = "wss://stream.bybit.com/v5/public/linear"
    sub = json.dumps({
        "op": "subscribe",
        "args": ["publicTrade.BTCUSDT", "allLiquidation.BTCUSDT"]
    })
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                await ws.send(sub)
                log("Bybit futures + liquidations connected")
                async for msg in ws:
                    try:
                        d = json.loads(msg)
                        topic = d.get("topic", "")
                        if "publicTrade" in topic and d.get("data"):
                            for trade in d["data"]:
                                try:
                                    price = float(trade["p"])
                                    ts = time.time()
                                    if "futures_bybit" not in futures_prices:
                                        futures_prices["futures_bybit"] = deque(maxlen=500)
                                    futures_prices["futures_bybit"].append((ts, price))
                                except (ValueError, TypeError):
                                    pass
                        elif "Liquidation" in topic and d.get("data"):
                            for liq in d["data"]:
                                try:
                                    side = liq.get("S", "")  # Buy = long liq'd, Sell = short liq'd
                                    size = float(liq.get("v", 0))
                                    price = float(liq.get("p", 0))
                                    usd_value = size * price
                                    direction = "long_liq" if side == "Buy" else "short_liq"
                                    liquidations.append((time.time(), direction, usd_value, "bybit"))
                                    if usd_value > 50000:
                                        log(f"💥 Bybit {direction}: ${usd_value:,.0f}")
                                except (ValueError, TypeError):
                                    pass
                    except (json.JSONDecodeError, KeyError):
                        pass
        except Exception as e:
            log(f"Bybit reconnecting: {e}")
            await asyncio.sleep(3)

async def binance_liquidations():
    """Binance forced liquidation orders."""
    import websockets
    url = "wss://fstream.binance.com/ws/btcusdt@forceOrder"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                log("Binance liquidations connected")
                async for msg in ws:
                    try:
                        d = json.loads(msg)
                        o = d.get("o", {})
                        side = o.get("S", "").lower()  # SELL = long liquidated, BUY = short liquidated
                        qty = float(o.get("q", 0))
                        price = float(o.get("p", 0))
                        usd_value = qty * price
                        direction = "long_liq" if side == "sell" else "short_liq"
                        liquidations.append((time.time(), direction, usd_value, "binance"))
                        if usd_value > 50000:
                            log(f"💥 Binance {direction}: ${usd_value:,.0f}")
                    except (ValueError, TypeError, KeyError):
                        pass
        except Exception as e:
            log(f"Binance liquidations reconnecting: {e}")
            await asyncio.sleep(3)

# ─── Window Analysis ───

def analyze_window(window_start):
    """Analyze all collected data for a 5-min window and write to JSONL."""
    window_end = window_start + 300
    
    # Get Chainlink prices for start and end
    strike = get_chainlink_price(window_start)
    settle = get_chainlink_price(window_end)
    
    if not strike or not settle:
        log(f"⚠ Missing Chainlink prices for window")
        return
    
    outcome = "Up" if settle > strike else "Down"
    delta = settle - strike
    
    # Futures-spot spreads from pre-collected snapshots
    snap = window_snapshots.get(window_start, {})
    spread_samples = snap.get("spreads", [])
    spreads = [{"spread": s} for s in spread_samples]
    
    # Liquidations during window
    window_liqs = []
    total_long_liq = 0
    total_short_liq = 0
    for ts, direction, usd, exchange in liquidations:
        if window_start <= ts <= window_end:
            window_liqs.append({"ts": ts, "direction": direction, "usd": usd, "exchange": exchange})
            if direction == "long_liq":
                total_long_liq += usd
            else:
                total_short_liq += usd
    
    # Average spread
    avg_spread = sum(s["spread"] for s in spreads) / len(spreads) if spreads else 0
    
    # Spread direction signal
    spread_signal = "Up" if avg_spread > 0.5 else ("Down" if avg_spread < -0.5 else "Neutral")
    
    # Liquidation signal
    liq_net = total_short_liq - total_long_liq  # positive = more shorts liquidated = bullish
    liq_signal = "Up" if liq_net > 10000 else ("Down" if liq_net < -10000 else "Neutral")
    
    # OI change
    oi_now = get_open_interest()
    
    # Funding
    funding = get_funding_rate()
    
    # Grade signals
    spread_correct = spread_signal == outcome if spread_signal != "Neutral" else None
    liq_correct = liq_signal == outcome if liq_signal != "Neutral" else None
    
    record = {
        "window_start": datetime.fromtimestamp(window_start, tz=timezone.utc).isoformat(),
        "window_end": datetime.fromtimestamp(window_end, tz=timezone.utc).isoformat(),
        "strike": round(strike, 2),
        "settle": round(settle, 2),
        "delta": round(delta, 2),
        "outcome": outcome,
        "avg_spread": round(avg_spread, 4),
        "spread_signal": spread_signal,
        "spread_correct": spread_correct,
        "spread_samples": len(spreads),
        "total_long_liq_usd": round(total_long_liq, 2),
        "total_short_liq_usd": round(total_short_liq, 2),
        "liq_net": round(liq_net, 2),
        "liq_signal": liq_signal,
        "liq_correct": liq_correct,
        "liq_count": len(window_liqs),
        "oi_usd": round(oi_now, 0) if oi_now else None,
        "funding_rate": funding,
    }
    
    # Write to JSONL
    with open(DATA_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")
    
    # Log summary
    emoji = "🟢" if spread_correct else ("🔴" if spread_correct is False else "⚪")
    liq_emoji = "🟢" if liq_correct else ("🔴" if liq_correct is False else "⚪")
    log(f"■  {outcome} (Δ{delta:+.1f}) | spread={avg_spread:+.2f} {emoji} | "
        f"liqs: {len(window_liqs)} (L${total_long_liq:,.0f}/S${total_short_liq:,.0f}) {liq_emoji} | "
        f"funding={funding if funding else 'n/a'}")

# ─── Main Loop ───

LIVE_STATE_FILE = BOT_DIR / "logs" / "futures-live.json"

async def live_state_writer():
    """Write current window liq totals + funding to a JSON file every 2s for the dashboard."""
    while True:
        try:
            now = time.time()
            window = int(now // 300) * 300
            cutoff = float(window)
            
            # Sum liquidations in current window
            long_liq = 0
            short_liq = 0
            liq_events = []
            for ts, side, usd, exchange in liquidations:
                if ts >= cutoff:
                    if side == "long_liq":
                        long_liq += usd
                    else:
                        short_liq += usd
                    liq_events.append({"ts": round(ts, 1), "side": side, "usd": round(usd), "exchange": exchange})
            
            # Get spread
            spread = None
            fut_price = None
            spot_price = None
            for key, dq in futures_prices.items():
                if dq and (now - dq[-1][0]) < 5:
                    fut_price = dq[-1][1]
                    break
            spot_dq = spot_prices.get("spot_binance", deque())
            if spot_dq and (now - spot_dq[-1][0]) < 5:
                spot_price = spot_dq[-1][1]
            if fut_price and spot_price:
                spread = round(fut_price - spot_price, 2)
            
            state = {
                "window": window,
                "long_liq": round(long_liq),
                "short_liq": round(short_liq),
                "net_pressure": round(short_liq - long_liq),
                "liq_count": len(liq_events),
                "recent_liqs": liq_events[-10:],
                "spread": spread,
                "fut_price": round(fut_price, 2) if fut_price else None,
                "spot_price": round(spot_price, 2) if spot_price else None,
                "updated": round(now, 1),
            }
            LIVE_STATE_FILE.write_text(json.dumps(state))
        except Exception:
            pass
        await asyncio.sleep(2)

async def spread_sampler():
    """Sample futures-spot spread every second and store per-window."""
    while True:
        try:
            now = time.time()
            window = int(now // 300) * 300
            
            # Get latest futures and spot prices
            fut_price = None
            spot_price = None
            
            for key, dq in futures_prices.items():
                if dq and (now - dq[-1][0]) < 5:
                    fut_price = dq[-1][1]
                    break
            
            spot_dq = spot_prices.get("spot_binance", deque())
            if spot_dq and (now - spot_dq[-1][0]) < 5:
                spot_price = spot_dq[-1][1]
            
            if fut_price and spot_price:
                spread = fut_price - spot_price
                if window not in window_snapshots:
                    window_snapshots[window] = {"spreads": [], "fut_prices": [], "spot_prices": []}
                window_snapshots[window]["spreads"].append(spread)
                window_snapshots[window]["fut_prices"].append(fut_price)
                window_snapshots[window]["spot_prices"].append(spot_price)
        except Exception:
            pass
        await asyncio.sleep(1)

async def window_tracker():
    """Track 5-min windows and analyze after each one resolves."""
    last_analyzed = 0
    
    while True:
        now = time.time()
        current = int(now // 300) * 300
        prev_window = current - 300
        
        # Analyze previous window if we haven't yet (wait 30s for settlement)
        if prev_window > last_analyzed and (now - current) > 30:
            log(f"Analyzing window {datetime.fromtimestamp(prev_window, tz=timezone.utc).strftime('%H:%M')}")
            try:
                analyze_window(prev_window)
            except Exception as e:
                log(f"⚠ Analysis error: {e}")
            last_analyzed = prev_window
            
            # Prune old snapshots and liquidations (keep last 15 min)
            cutoff_window = prev_window - 900
            for k in list(window_snapshots.keys()):
                if k < cutoff_window:
                    del window_snapshots[k]
            cutoff = now - 900
            while liquidations and liquidations[0][0] < cutoff:
                liquidations.pop(0)
        
        await asyncio.sleep(10)

async def main():
    print("=" * 65)
    print("◈ Futures Shadow Observer")
    print(f"   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"   Mode: OBSERVE ONLY (no trades)")
    print(f"   Feeds: Binance spot+futures, Bybit futures+liquidations")
    print(f"   Output: {DATA_FILE}")
    print("=" * 65)
    print()
    
    tasks = [
        asyncio.create_task(binance_spot_trades()),
        asyncio.create_task(binance_futures_trades()),
        asyncio.create_task(bybit_futures()),
        asyncio.create_task(binance_liquidations()),
        asyncio.create_task(spread_sampler()),
        asyncio.create_task(live_state_writer()),
        asyncio.create_task(window_tracker()),
    ]
    
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        log("Shutting down shadow observer...")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
