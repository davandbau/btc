#!/usr/bin/env python3
"""
Polymarket 5-Min BTC Reasoning Loop — Single Tranche, Momentum-First.

Watches 5-min BTC Up/Down windows, spawns OpenClaw sub-agent for trade decisions.
Single tranche at 120s into window, $30 base position, confidence-based sizing.
Pre-filters coin-flip deltas to reduce API calls.

Usage:
    python3 reasoning-loop.py           # run live
    python3 reasoning-loop.py --dry-run # no actual trades
"""

import argparse
import json
import math
import os
import atexit
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

# Direct Anthropic API (fast path, bypasses OpenClaw agent overhead)
try:
    import anthropic
    _AUTH = json.load(open(Path.home() / ".openclaw" / "agents" / "main" / "agent" / "auth.json"))
    _ANTHROPIC_CLIENT = anthropic.Anthropic(api_key=_AUTH["anthropic"]["key"])
    DIRECT_API = True
except Exception:
    DIRECT_API = False

BOT_DIR = Path(__file__).parent
LEDGER_PATH = BOT_DIR / "ledgers" / "reasoning.json"
_DEATH_LOG = BOT_DIR / "logs" / "death.log"

def _log_death(reason):
    """Log why the bot is exiting."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with open(_DEATH_LOG, "a") as f:
        f.write(f"[{ts}] {reason}\n")
    print(f"\n⚠️ [{ts}] {reason}", flush=True)

def _signal_handler(signum, frame):
    _log_death(f"Received signal {signum} ({signal.Signals(signum).name})")
    sys.exit(128 + signum)

signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGHUP, _signal_handler)
atexit.register(lambda: _log_death("atexit: clean Python exit"))
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
CHAINLINK_FEED_ID = "0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8"
CHAINLINK_API = "https://data.chain.link/api/query-timescale"

MAX_POSITION = 116.0  # Maximum trade size at 100% conviction
MIN_EDGE = 0.05       # Minimum edge (conviction - market_price) to trade, accounts for fees
MIN_CONVICTION = 0.70  # Minimum conviction to trade (filters marginal setups)
MAX_CONVICTION_RATIO = 1.8  # Max conviction / market_price ratio (sanity check)

# Monitoring window config
MONITOR_START = 30          # Start sampling delta at 30s into window
MONITOR_END = 180           # Two decision points: ~120-150s and ~150-180s
SAMPLE_INTERVAL = 10        # Sample delta every 10s (faster confirmation)
MIN_CONSISTENT = 3          # Need 3 of last 4 samples on same side
MAX_ENTRY_PRICE = None       # No cap — Kelly sizing handles edge (no edge = $0 size)
MAX_ENTRIES_PER_WINDOW = 2  # Primary + one scale-in
MAX_COMBINED_COST = 100     # Combined cap per window
SCALE_IN_DELTA_RATIO = 2.0  # Delta must double from first entry to scale in
SCALE_IN_MIN_CONSISTENT = 4 # Scale-in needs 4 of 4 samples consistent

def kelly_size(conviction, market_price):
    """Position sizing via Kelly Criterion.
    
    size = MAX_POSITION × (conviction - market_price) / (1 - market_price)
    
    conviction: 0.0-1.0 (agent's estimated probability of winning)
    market_price: entry price (market's implied probability)
    Returns dollar size, or 0 if edge < MIN_EDGE.
    """
    edge = conviction - market_price
    if conviction < MIN_CONVICTION:
        return 0.0
    if edge < MIN_EDGE:
        return 0.0
    kelly = edge / (1 - market_price)
    kelly = max(0.0, min(1.0, kelly))  # clamp to [0, 1]
    return round(MAX_POSITION * kelly, 2)


def fetch_json(url, timeout=8):
    req = Request(url, headers={"User-Agent": "reasoning-loop/2.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except:
        return None


def log_pass(brief, reason, pass_type="agent"):
    """Log a pass/skip to the ledger for post-hoc analysis."""
    try:
        ledger = json.loads(LEDGER_PATH.read_text()) if LEDGER_PATH.exists() else {}
        if "passes" not in ledger:
            ledger["passes"] = []
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pass_type": pass_type,  # "agent", "pre_filter", "underwater"
            "reason": reason[:200],
            "btc_price": brief.get("chainlink_current", 0) if isinstance(brief, dict) else 0,
            "delta": brief.get("delta_from_strike", 0) if isinstance(brief, dict) else 0,
            "strike": brief.get("strike_price", 0) if isinstance(brief, dict) else 0,
            "market_slug": brief.get("polymarket", {}).get("slug", "") if isinstance(brief, dict) else "",
        }
        # Add momentum/CVD if available
        if isinstance(brief, dict):
            mom = brief.get("momentum_alignment", {})
            entry["momentum_score"] = mom.get("score", None)
            entry["momentum_strength"] = mom.get("strength", None)
            tech = brief.get("technical", {})
            entry["cvd"] = tech.get("cvd_net", None)
        ledger["passes"].append(entry)
        # Keep last 200 passes
        ledger["passes"] = ledger["passes"][-200:]
        LEDGER_PATH.write_text(json.dumps(ledger, indent=2))
    except Exception as e:
        print(f"  ⚠️  Failed to log pass: {e}")


# ---- Market Data Gathering ----

def build_brief(cached_strike=None):
    """Gather all market data for the current window."""
    now = time.time()
    current_window = int(now) // 300 * 300
    elapsed = now - current_window
    remaining = (current_window + 300) - now
    brief = {
        "window_start": current_window,
        "window_utc": datetime.fromtimestamp(current_window, tz=timezone.utc).strftime("%H:%M"),
        "elapsed_s": round(elapsed, 0),
        "remaining_s": round(remaining, 0),
    }

    # Chainlink — current price + strike
    cl_url = f"{CHAINLINK_API}?query=LIVE_STREAM_REPORTS_QUERY&variables=%7B%22feedId%22%3A%22{CHAINLINK_FEED_ID}%22%7D"
    cl_data = fetch_json(cl_url)
    if cl_data and "data" in cl_data:
        nodes = cl_data["data"].get("liveStreamReports", {}).get("nodes", [])
        if nodes:
            cl_prices = []
            for n in nodes[:60]:
                ts = datetime.fromisoformat(n["validFromTimestamp"].replace("Z", "+00:00")).timestamp()
                price = float(n["price"]) / 1e18
                cl_prices.append({"ts": ts, "price": price})

            brief["chainlink_current"] = round(cl_prices[0]["price"], 2)

            # Get real-time Binance spot price for delta calculation
            _binance_price = None
            try:
                _ticker = fetch_json("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
                if _ticker and "price" in _ticker:
                    _binance_price = float(_ticker["price"])
                    brief["binance_spot"] = round(_binance_price, 2)
            except Exception:
                pass
            # Use Binance for delta if available, fall back to Chainlink
            _current_for_delta = _binance_price if _binance_price is not None else cl_prices[0]["price"]

            # Strike = cached from window start, or fallback to buffer search
            if cached_strike is not None:
                best_strike = cached_strike
            else:
                best_strike = None
                best_dist = float("inf")
                for p in cl_prices:
                    d = abs(p["ts"] - current_window)
                    if d < best_dist:
                        best_dist = d
                        best_strike = p["price"]
            if best_strike:
                brief["strike"] = round(best_strike, 2)
                brief["delta_from_strike"] = round(_current_for_delta - best_strike, 2)

            # Price trajectory since window start
            window_prices = []
            for p in reversed(cl_prices):
                if p["ts"] >= current_window:
                    window_prices.append({"t": round(p["ts"] - current_window), "p": round(p["price"], 2)})
            if window_prices:
                brief["price_trajectory"] = window_prices[-10:]

    # Binance order book
    book = fetch_json("https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=20")
    if book:
        bids = sum(float(b[1]) for b in book["bids"])
        asks = sum(float(a[1]) for a in book["asks"])
        brief["orderbook"] = {
            "bids_btc": round(bids, 3),
            "asks_btc": round(asks, 3),
            "ratio": round(bids / asks, 2) if asks > 0 else 999,
            "signal": "BUY pressure" if bids > asks * 1.5 else "SELL pressure" if asks > bids * 1.5 else "balanced",
        }

    # Binance recent trades + CVD (Cumulative Volume Delta)
    trades = fetch_json("https://api.binance.com/api/v3/aggTrades?symbol=BTCUSDT&limit=500")
    if trades:
        buy_vol = sum(float(t["q"]) for t in trades if not t["m"])
        sell_vol = sum(float(t["q"]) for t in trades if t["m"])
        total = buy_vol + sell_vol

        # CVD: cumulative buy - sell volume, chunked into ~30s buckets
        cvd_running = 0.0
        cvd_points = []
        bucket_start = float(trades[0]["T"]) / 1000
        bucket_buy = 0.0
        bucket_sell = 0.0
        for t in trades:
            ts = float(t["T"]) / 1000
            qty = float(t["q"])
            if ts - bucket_start > 30:
                cvd_running += bucket_buy - bucket_sell
                cvd_points.append(round(cvd_running, 3))
                bucket_start = ts
                bucket_buy = 0.0
                bucket_sell = 0.0
            if not t["m"]:
                bucket_buy += qty
            else:
                bucket_sell += qty
        cvd_running += bucket_buy - bucket_sell
        cvd_points.append(round(cvd_running, 3))

        # Detect large trades (>0.5 BTC individual)
        large_buys = sum(1 for t in trades if not t["m"] and float(t["q"]) > 0.5)
        large_sells = sum(1 for t in trades if t["m"] and float(t["q"]) > 0.5)

        brief["trade_flow"] = {
            "buy_pct": round(buy_vol / total * 100, 1) if total > 0 else 50,
            "sell_pct": round(sell_vol / total * 100, 1) if total > 0 else 50,
            "cvd_total": round(cvd_running, 3),
            "cvd_trend": cvd_points[-5:] if len(cvd_points) >= 5 else cvd_points,
            "cvd_signal": "bullish" if cvd_running > 1.0 else "bearish" if cvd_running < -1.0 else "neutral",
            "large_buys": large_buys,
            "large_sells": large_sells,
        }

    # Binance klines — 1m, 15m, 1h
    for tf, key, count in [("1m", "candles_1m", 30), ("5m", "candles_5m", 12), ("15m", "candles_15m", 8)]:
        klines = fetch_json(f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={tf}&limit={count}")
        if klines:
            candles = []
            closes = []
            highs = []
            lows = []
            volumes = []
            for k in klines:
                o, h, l, c, v = float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])
                closes.append(c)
                highs.append(h)
                lows.append(l)
                volumes.append(v)
                rng = round(h - l, 1)
                color = "Green" if c >= o else "Red"
                candles.append({"c": color, "range": rng, "close": round(c, 1), "vol": round(v, 2)})
            brief[key] = candles

            # HTF trend summary for 5m and 15m
            if tf in ("5m", "15m"):
                greens = sum(1 for c in candles if c["c"] == "Green")
                reds = len(candles) - greens
                net_move = round(closes[-1] - closes[0], 1) if len(closes) > 1 else 0
                brief[f"trend_{tf}"] = f"{greens}G/{reds}R net={net_move:+.1f}"

            # Technical indicators (computed from 1m candles)
            if tf == "1m" and len(closes) >= 6:
                ta = {}

                # RSI
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
                    ta["rsi_signal"] = "overbought" if ta["rsi_6"] > 70 else "oversold" if ta["rsi_6"] < 30 else "neutral"

                # EMA 9 and 21
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
                    ta["ema_fast"] = round(ema9, 1)
                    ta["ema_slow"] = round(ema21, 1)
                    ta["ema_signal"] = "bullish" if ema9 > ema21 else "bearish"

                # VWAP
                if volumes and sum(volumes) > 0:
                    typical_prices = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
                    cum_tp_vol = sum(tp * v for tp, v in zip(typical_prices, volumes))
                    cum_vol = sum(volumes)
                    vwap = cum_tp_vol / cum_vol
                    ta["vwap"] = round(vwap, 1)
                    ta["price_vs_vwap"] = round(closes[-1] - vwap, 1)
                    ta["vwap_signal"] = "above" if closes[-1] > vwap else "below"

                # Bollinger Bands
                if len(closes) >= 8:
                    period = min(len(closes), 10)
                    recent = closes[-period:]
                    sma = sum(recent) / len(recent)
                    std = (sum((x - sma) ** 2 for x in recent) / len(recent)) ** 0.5
                    upper = sma + 2 * std
                    lower = sma - 2 * std
                    avg_range = sum(h - l for h, l in zip(highs[-period:], lows[-period:])) / period
                    ta["bb_upper"] = round(upper, 1)
                    ta["bb_lower"] = round(lower, 1)
                    ta["bb_width"] = round(upper - lower, 1)
                    ta["bb_position"] = round((closes[-1] - lower) / (upper - lower) * 100, 1) if upper != lower else 50
                    ta["bb_signal"] = "squeeze" if std < avg_range * 0.3 else "expanding"

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
                        hurst = math.log(R / S) / math.log(n)
                        ta["hurst"] = round(hurst, 3)
                        ta["hurst_regime"] = "mean_reverting" if hurst < 0.45 else "trending" if hurst > 0.55 else "random_walk"

                # ADX / DI+ / DI-
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
                        # Smoothed averages (Wilder's method)
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
                        ta["adx_signal"] = "strong_trend" if dx > 25 else "trending" if dx > 20 else "ranging"
                        ta["trend_direction"] = "bullish" if di_plus > di_minus else "bearish"

                        # ATR ratio (current ATR vs average ATR)
                        ta["atr"] = round(atr_s, 2)
                        avg_tr = sum(tr_list) / len(tr_list)
                        ta["atr_ratio"] = round(atr_s / avg_tr, 2) if avg_tr > 0 else 1.0
                        ta["atr_signal"] = "expanding" if ta["atr_ratio"] > 1.2 else "contracting" if ta["atr_ratio"] < 0.8 else "normal"

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
                            chop = 100 * math.log10(atr_sum / hl_range) / math.log10(ci_period)
                            ta["choppiness"] = round(chop, 1)
                            ta["chop_signal"] = "choppy" if chop > 61.8 else "trending" if chop < 38.2 else "neutral"

                brief["technical"] = ta

    # --- Orderbook Imbalance Score ---
    if book and book.get("bids") and book.get("asks"):
        weighted_bid = 0.0
        weighted_ask = 0.0
        mid = (float(book["bids"][0][0]) + float(book["asks"][0][0])) / 2
        for i, b in enumerate(book["bids"][:10]):
            dist = mid - float(b[0])
            weight = 1 / (1 + dist / 10)
            weighted_bid += float(b[1]) * weight
        for i, a in enumerate(book["asks"][:10]):
            dist = float(a[0]) - mid
            weight = 1 / (1 + dist / 10)
            weighted_ask += float(a[1]) * weight
        total_w = weighted_bid + weighted_ask
        if total_w > 0:
            imbalance = (weighted_bid - weighted_ask) / total_w
            brief["orderbook_imbalance"] = {
                "score": round(imbalance, 3),
                "weighted_bid_btc": round(weighted_bid, 3),
                "weighted_ask_btc": round(weighted_ask, 3),
                "signal": "strong_buy" if imbalance > 0.3 else "buy" if imbalance > 0.1 else "strong_sell" if imbalance < -0.3 else "sell" if imbalance < -0.1 else "neutral",
            }

    # --- Multi-Timeframe Momentum Alignment Score ---
    alignment_signals = []
    if "technical" in brief:
        if brief["technical"].get("ema_signal"):
            alignment_signals.append(1 if brief["technical"]["ema_signal"] == "bullish" else -1)
        if brief["technical"].get("rsi_signal"):
            alignment_signals.append(1 if brief["technical"]["rsi_signal"] == "oversold" else -1 if brief["technical"]["rsi_signal"] == "overbought" else 0)
        if brief["technical"].get("vwap_signal"):
            alignment_signals.append(1 if brief["technical"]["vwap_signal"] == "above" else -1)
    if brief.get("candles_5m"):
        c5 = brief["candles_5m"]
        greens = sum(1 for c in c5 if c.get("c", "").startswith("G"))
        reds = sum(1 for c in c5 if c.get("c", "").startswith("R"))
        alignment_signals.append(1 if greens > reds else -1 if reds > greens else 0)
    if brief.get("candles_15m"):
        c15 = brief["candles_15m"]
        greens = sum(1 for c in c15 if c.get("c", "").startswith("G"))
        reds = sum(1 for c in c15 if c.get("c", "").startswith("R"))
        alignment_signals.append(1 if greens > reds else -1 if reds > greens else 0)
    if brief.get("trade_flow", {}).get("cvd_signal"):
        cvd_sig = brief["trade_flow"]["cvd_signal"]
        alignment_signals.append(1 if cvd_sig == "bullish" else -1 if cvd_sig == "bearish" else 0)
    if brief.get("taker_flow", {}).get("ratio"):
        ratio = brief["taker_flow"]["ratio"]
        alignment_signals.append(1 if ratio > 1.1 else -1 if ratio < 0.9 else 0)
    if alignment_signals:
        avg = sum(alignment_signals) / len(alignment_signals)
        aligned_count = sum(1 for s in alignment_signals if s == (1 if avg > 0 else -1))
        brief["momentum_alignment"] = {
            "score": round(avg, 2),
            "direction": "bullish" if avg > 0.2 else "bearish" if avg < -0.2 else "mixed",
            "aligned_signals": aligned_count,
            "total_signals": len(alignment_signals),
            "strength": "strong" if abs(avg) > 0.5 else "moderate" if abs(avg) > 0.2 else "weak",
        }

    # Polymarket odds + CLOB orderbook
    slug = f"btc-updown-5m-{current_window}"
    pm_data = fetch_json(f"{GAMMA_BASE}/events?slug={slug}")
    if pm_data:
        event = pm_data[0]
        for m in event.get("markets", []):
            if not m.get("closed"):
                try:
                    prices = json.loads(m.get("outcomePrices", "[]"))
                    outcomes = json.loads(m.get("outcomes", "[]"))
                    up_idx = 0 if "Up" in outcomes[0] else 1
                    down_idx = 1 - up_idx
                    tokens = json.loads(m.get("clobTokenIds", "[]"))

                    brief["polymarket"] = {
                        "up_price": round(float(prices[up_idx]), 3),
                        "down_price": round(float(prices[down_idx]), 3),
                        "up_token": tokens[up_idx] if tokens else "",
                        "down_token": tokens[down_idx] if tokens else "",
                    }

                    # CLOB midpoint + sorted orderbook for best bid/ask
                    for side_name, token_idx in [("up", up_idx), ("down", down_idx)]:
                        if tokens:
                            # Midpoint (authoritative market price)
                            mid = fetch_json(f"{CLOB_BASE}/midpoint?token_id={tokens[token_idx]}")
                            if mid and mid.get("mid"):
                                brief["polymarket"][f"{side_name}_mid"] = round(float(mid["mid"]), 4)

                            # Orderbook — MUST sort before reading
                            clob = fetch_json(f"{CLOB_BASE}/book?token_id={tokens[token_idx]}")
                            if clob:
                                bids_sorted = sorted(clob.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
                                asks_sorted = sorted(clob.get("asks", []), key=lambda x: float(x["price"]))
                                best_bid = float(bids_sorted[0]["price"]) if bids_sorted else None
                                best_ask = float(asks_sorted[0]["price"]) if asks_sorted else None
                                brief["polymarket"][f"{side_name}_best_bid"] = best_bid
                                brief["polymarket"][f"{side_name}_best_ask"] = best_ask
                except:
                    pass

    # ---- Short-term Bias (1h of 5m candles) ----
    # NOTE: Deliberately uses 5m candles (12 = 1 hour lookback). No 1h/4h/1d — too slow for 5-min windows.
    stf_candles = fetch_json("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=12")
    if stf_candles and len(stf_candles) >= 6:
        try:
            closes = [float(c[4]) for c in stf_candles]
            highs = [float(c[2]) for c in stf_candles]
            lows = [float(c[3]) for c in stf_candles]
            opens = [float(c[1]) for c in stf_candles]
            now_price = closes[-1]
            score = 0.0

            # 1. Price change 1h (full window)
            chg_1h = (now_price - closes[0]) / closes[0] * 100
            if chg_1h > 0.3: score += 1
            elif chg_1h > 0.1: score += 0.5
            elif chg_1h < -0.3: score -= 1
            elif chg_1h < -0.1: score -= 0.5

            # 2. Price change 30m (last 6 candles)
            if len(closes) >= 6:
                chg_30m = (now_price - closes[-6]) / closes[-6] * 100
                if chg_30m > 0.15: score += 1
                elif chg_30m > 0.05: score += 0.5
                elif chg_30m < -0.15: score -= 1
                elif chg_30m < -0.05: score -= 0.5

            # 3. EMA cross (EMA-6 vs EMA-12)
            def ema(data, period):
                k = 2 / (period + 1)
                val = data[0]
                for d in data[1:]:
                    val = d * k + val * (1 - k)
                return val
            ema6 = ema(closes, 6)
            ema12 = ema(closes, 12)
            ema_diff = (ema6 - ema12) / ema12 * 100
            if ema_diff > 0.02: score += 1
            elif ema_diff < -0.02: score -= 1

            # 4. Candle ratio (green vs red)
            green = sum(1 for o, c in zip(opens, closes) if c >= o)
            red = len(closes) - green
            if green >= 9: score += 1
            elif green >= 7: score += 0.5
            elif red >= 9: score -= 1
            elif red >= 7: score -= 0.5

            # 5. Structure — last 3 candle closes
            last3 = closes[-3:]
            if last3[2] > last3[1] > last3[0]: score += 1
            elif last3[2] > last3[1] or last3[1] > last3[0]: score += 0.5 if last3[2] > last3[0] else 0
            if last3[2] < last3[1] < last3[0]: score -= 1
            elif last3[2] < last3[1] or last3[1] < last3[0]: score -= 0.5 if last3[2] < last3[0] else 0

            # 6. Range position — current price in 1h high-low range
            h1_high = max(highs)
            h1_low = min(lows)
            if h1_high > h1_low:
                range_pos = (now_price - h1_low) / (h1_high - h1_low)
                if range_pos >= 0.8: score += 1
                elif range_pos >= 0.6: score += 0.5
                elif range_pos <= 0.2: score -= 1
                elif range_pos <= 0.4: score -= 0.5
            else:
                range_pos = 0.5

            # Label
            if score >= 4: label = "strong_bullish"
            elif score >= 2: label = "bullish"
            elif score <= -4: label = "strong_bearish"
            elif score <= -2: label = "bearish"
            else: label = "neutral"

            brief["htf_bias"] = {
                "score": round(score, 1),
                "label": label,
                "1h_change_pct": round(chg_1h, 2),
                "30m_change_pct": round(chg_30m, 2) if len(closes) >= 6 else None,
                "ema6_vs_12": round(ema_diff, 3),
                "green_candles": green,
                "red_candles": red,
                "range_position": round(range_pos, 2),
            }
        except Exception:
            pass

    # Binance funding rate
    funding = fetch_json("https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT")
    if funding:
        brief["funding_rate"] = round(float(funding.get("lastFundingRate", 0)) * 100, 4)

    # Binance futures open interest
    oi = fetch_json("https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT")
    if oi:
        brief["open_interest_btc"] = round(float(oi.get("openInterest", 0)), 2)

    # Futures price (for basis/spread)
    futures_price = fetch_json("https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT")
    if futures_price and brief.get("chainlink_current"):
        fp = float(futures_price.get("price", 0))
        spot = brief["chainlink_current"]
        basis = fp - spot
        brief["futures"] = {
            "price": round(fp, 2),
            "basis": round(basis, 2),
            "basis_pct": round(basis / spot * 100, 4) if spot > 0 else 0,
            "signal": "contango" if basis > 5 else "backwardation" if basis < -5 else "flat",
        }

    # Taker Buy/Sell Ratio
    taker = fetch_json("https://fapi.binance.com/fapi/v1/ticker/24hr?symbol=BTCUSDT")
    if taker:
        buy_vol = float(taker.get("volume", 0))
        quote_vol = float(taker.get("quoteVolume", 0))
        if quote_vol > 0:
            brief["taker_flow"] = {
                "ratio": round(buy_vol / (quote_vol / float(taker.get("weightedAvgPrice", 1))), 3) if float(taker.get("weightedAvgPrice", 1)) > 0 else 1.0,
            }

    # Long/Short ratio
    ls = fetch_json("https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol=BTCUSDT&period=5m&limit=1")
    if ls and len(ls) > 0:
        brief["long_short_ratio"] = {
            "ratio": round(float(ls[0].get("longShortRatio", 1)), 3),
            "longs_pct": round(float(ls[0].get("longAccount", 0.5)) * 100, 1),
            "shorts_pct": round(float(ls[0].get("shortAccount", 0.5)) * 100, 1),
        }

    # Coinbase price (cross-exchange)
    coinbase = fetch_json("https://api.coinbase.com/v2/prices/BTC-USD/spot")
    if coinbase and brief.get("chainlink_current"):
        cb_price = float(coinbase.get("data", {}).get("amount", 0))
        if cb_price > 0:
            spread = cb_price - brief["chainlink_current"]
            brief["cross_exchange"] = {
                "coinbase": round(cb_price, 2),
                "spread": round(spread, 2),
                "signal": "coinbase premium" if spread > 10 else "coinbase discount" if spread < -10 else "aligned",
            }

    # Short-term trend analysis (5m/15m EMA crosses + funding)
    # NOTE: Deliberately excludes 1h/4h/1d — too slow for 5-min windows
    htf_signals = []
    for tf, period, weight in [("5m", 30, 2), ("15m", 24, 1)]:
        kl = fetch_json(f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={tf}&limit={period}")
        if kl and len(kl) >= 21:
            closes = [float(k[4]) for k in kl]
            def _ema(data, p):
                m = 2 / (p + 1)
                v = sum(data[:p]) / p
                for d in data[p:]:
                    v = (d - v) * m + v
                return v
            ema9 = _ema(closes, 9)
            ema21 = _ema(closes, 21)
            direction = "bullish" if ema9 > ema21 else "bearish"
            htf_signals.append({"tf": tf, "direction": direction, "weight": weight,
                                "ema9": round(ema9, 0), "ema21": round(ema21, 0)})
    # Funding rate
    fr = fetch_json("https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=3")
    if fr:
        avg_fr = sum(float(f["fundingRate"]) for f in fr) / len(fr)
        fr_signal = "bearish" if avg_fr > 0.0005 else "bullish" if avg_fr < -0.0005 else "neutral"
        htf_signals.append({"tf": "funding", "direction": fr_signal, "weight": 1, "rate": round(avg_fr * 100, 4)})
    # Composite score
    bullish_w = sum(s["weight"] for s in htf_signals if s["direction"] == "bullish")
    bearish_w = sum(s["weight"] for s in htf_signals if s["direction"] == "bearish")
    score = bullish_w - bearish_w  # positive = bullish, negative = bearish
    composite = "bullish" if score >= 2 else "bearish" if score <= -2 else "neutral"
    brief["htf_trend"] = {
        "signals": htf_signals,
        "composite": composite,
        "score": score,
        "summary": f"{composite} ({score:+d}): " + ", ".join(f"{s['tf']}={s['direction']}" for s in htf_signals)
    }

    # Mempool fees
    mempool = fetch_json("https://mempool.space/api/v1/fees/recommended")
    if mempool:
        brief["mempool_fees"] = {
            "fastest": mempool.get("fastestFee"),
            "half_hour": mempool.get("halfHourFee"),
            "signal": "high" if mempool.get("fastestFee", 0) > 50 else "normal",
        }

    # Previous window results
    # Recent results removed — tilt guard handles streak risk mechanically.
    # Feeding W/L data to the LLM invited pattern-matching on noise.

    # Write regime/trend data for dashboard
    try:
        ta = brief.get("technical", {})
        regime_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "hurst": ta.get("hurst"),
            "hurst_regime": ta.get("hurst_regime"),
            "adx": ta.get("adx"),
            "di_plus": ta.get("di_plus"),
            "di_minus": ta.get("di_minus"),
            "adx_signal": ta.get("adx_signal"),
            "trend_direction": ta.get("trend_direction"),
            "atr": ta.get("atr"),
            "atr_ratio": ta.get("atr_ratio"),
            "atr_signal": ta.get("atr_signal"),
            "choppiness": ta.get("choppiness"),
            "chop_signal": ta.get("chop_signal"),
            "bb_width": ta.get("bb_width"),
            "bb_signal": ta.get("bb_signal"),
            "rsi_6": ta.get("rsi_6"),
            "ema_signal": ta.get("ema_signal"),
        }
        Path(LOGS_DIR / "regime-live.json").write_text(json.dumps(regime_data))
    except Exception:
        pass

    return brief


# ---- Agent Interaction ----

def trigger_agent(brief, tranche, prior_decisions, dry_run=False, live=False):
    """Spawn an OpenClaw agent to make a trade decision."""
    tranche_id = tranche["id"]
    base_size = MAX_POSITION  # for display only
    if live:
        trade_cmd = f"python3.12 {BOT_DIR / 'live-trader.py'}"
        trader_script = str(BOT_DIR / "live-trader.py")
        trader_python = "python3.12"
    else:
        trade_cmd = f"python3 {BOT_DIR / 'reasoning-trader.py'}"
        trader_script = str(BOT_DIR / "reasoning-trader.py")
        trader_python = "python3"

    # Save brief snapshot for post-hoc analysis
    briefs_dir = BOT_DIR / "briefs"
    briefs_dir.mkdir(exist_ok=True)
    brief_file = briefs_dir / f"{brief.get('window_start', 0)}_T{tranche_id}.json"
    brief_file.write_text(json.dumps(brief, indent=2, default=str))

    # Compact brief for the agent — strip raw candle arrays, keep summaries
    compact = dict(brief)
    for key in ["candles_1m", "candles_5m", "candles_15m"]:
        if key in compact:
            candles = compact[key]
            greens = sum(1 for c in candles if c.get("c") == "Green")
            reds = len(candles) - greens
            closes = [c["close"] for c in candles]
            net = round(closes[-1] - closes[0], 1) if len(closes) > 1 else 0
            avg_range = round(sum(c.get("range", 0) for c in candles) / len(candles), 1) if candles else 0
            last_close = closes[-1] if closes else 0
            compact[key] = f"{greens}G/{reds}R net={net:+.1f} avgRange={avg_range} last={last_close}"
    # Strip price_trajectory to last 3 points
    if "price_trajectory" in compact and isinstance(compact["price_trajectory"], list):
        compact["price_trajectory"] = compact["price_trajectory"][-3:]

    brief_json = json.dumps(compact, indent=2, default=str)

    prior_context = ""
    if prior_decisions:
        prior_context = "\n\nPRIOR TRANCHES THIS WINDOW:\n"
        for pd in prior_decisions:
            prior_context += f"  T{pd['tranche']}: {pd['action']} (conf={pd.get('confidence','?')}) — {pd['reasoning'][:80]}\n"
        prior_context += "\nYou can: add to the same side, take the opposite side, or PASS. Each tranche is independent.\n"

    # Load prompt template from file (v2+)
    prompt_path = BOT_DIR / "prompts" / "trading-v2.md"
    prompt_template = prompt_path.read_text()
    # Strip comment lines at top (lines starting with #)
    prompt_lines = prompt_template.split("\n")
    prompt_body = "\n".join(l for l in prompt_lines if not l.startswith("#"))
    message = prompt_body.format(
        tranche_id=tranche_id,
        brief_json=brief_json,
        prior_context=prior_context,
        remaining=brief.get("remaining_s", "?"),
    )

    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"  [{ts}] ◈ T{tranche_id} — triggering agent (base ${base_size:.0f}, {brief.get('remaining_s', '?')}s left)...")

    # Prepare LLM call log directory
    llm_log_dir = BOT_DIR / "logs" / "llm-calls"
    llm_log_dir.mkdir(parents=True, exist_ok=True)
    call_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    window_id = brief.get("window_start", 0)
    _llm_meta = {}

    try:
        output = ""
        if DIRECT_API:
            # Fast path: direct Anthropic API (~2-3s vs ~15s via OpenClaw agent)
            try:
                _start = time.time()
                api_messages = [{"role": "user", "content": message}]
                resp = _ANTHROPIC_CLIENT.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=600,
                    messages=api_messages,
                )
                output = resp.content[0].text.strip()
                _elapsed = time.time() - _start
                print(f"  [{ts}]    (direct API: {_elapsed:.1f}s, {resp.usage.input_tokens}+{resp.usage.output_tokens} tokens)")

                # Store for post-decision logging
                _llm_meta = {
                    "model": "claude-sonnet-4-6",
                    "elapsed_s": round(_elapsed, 2),
                    "request": {"messages": api_messages, "max_tokens": 600},
                    "response": {
                        "output": output,
                        "usage": {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens},
                        "stop_reason": resp.stop_reason,
                    },
                }

            except Exception as api_err:
                print(f"  [{ts}]    ⚠️ Direct API failed ({api_err}), falling back to OpenClaw agent...")
                output = ""

        if not output:
            # Fallback: OpenClaw agent subprocess
            cmd = ["openclaw", "agent", "--agent", "polymarket-trader", "--session-id", "trading-5m", "-m", message]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            output = result.stdout.strip() if result.stdout else ""

            # Store for post-decision logging
            _llm_meta = {
                "model": "openclaw-agent-fallback",
                "request": {"message_length": len(message), "message": message},
                "response": {"output": output, "stderr": (result.stderr or "")[:500]},
            }
        decision = {"tranche": tranche_id, "action": "UNKNOWN", "reasoning": ""}

        if output:
            # Parse JSON response from agent
            clean = output.strip()
            clean = re.sub(r'```json\s*', '', clean)
            clean = re.sub(r'```\s*', '', clean)
            clean = clean.strip()
            json_obj = None
            try:
                json_obj = json.loads(clean)
            except json.JSONDecodeError:
                start = clean.find('{')
                end = clean.rfind('}')
                if start >= 0 and end > start:
                    try:
                        json_obj = json.loads(clean[start:end+1])
                    except json.JSONDecodeError:
                        pass

            if json_obj and isinstance(json_obj, dict):
                action = json_obj.get("action", "").upper()
                conviction = int(json_obj.get("conviction", 0))
                reasoning = json_obj.get("reasoning", "")

                decision["conviction"] = conviction
                decision["reasoning"] = reasoning

                # Log clean agent decision
                print(f"  [{ts}]    Action: {action} | Conviction: {conviction}% | {reasoning}")

                if action == "PASS":
                    decision["action"] = "PASS"
                elif action == "UP":
                    decision["action"] = "BUY_UP"
                elif action == "DOWN":
                    decision["action"] = "BUY_DOWN"
                else:
                    decision["action"] = "UNKNOWN"
                    print(f"  [{ts}] ⚠️  Unknown action in JSON: {action}")

                if decision["action"] in ("BUY_UP", "BUY_DOWN"):
                    side = "Up" if decision["action"] == "BUY_UP" else "Down"
                    conv = conviction / 100.0
                    pm = brief.get("polymarket", {})
                    if side == "Up":
                        entry_price = pm.get("up_mid", pm.get("up_best_ask", pm.get("up_price", 0.5)))
                    else:
                        entry_price = pm.get("down_mid", pm.get("down_best_ask", pm.get("down_price", 0.5)))
                    # Sanity check: conviction can't be too far from market price
                    if entry_price > 0 and conv / entry_price > MAX_CONVICTION_RATIO:
                        print(f"  [{ts}]    ⊘ SANITY CHECK: conviction {conviction}% is {conv/entry_price:.1f}× market price {entry_price:.2f} (max {MAX_CONVICTION_RATIO}×) — likely overconfident, skipping")
                        decision["action"] = "PASS"
                        decision["reasoning"] = f"Sanity check: conviction {conv/entry_price:.1f}× market price"
                        log_pass(brief, decision["reasoning"], "sanity_check")
                    else:
                        sized = kelly_size(conv, entry_price)
                        edge = conv - entry_price
                        decision["size"] = sized
                        print(f"  [{ts}]    ≡ Conviction: {conviction}% | Edge: {edge*100:.1f}% | Kelly size: ${sized:.2f} (max ${MAX_POSITION:.0f})")

                        if sized <= 0:
                            decision["action"] = "PASS"
                            decision["reasoning"] = f"Kelly sized $0 — edge {edge*100:.1f}% insufficient"
                            log_pass(brief, decision["reasoning"], "kelly_reject")
                        else:
                            pass  # Continue to execute trade below

                        # Execute trade (paper or live)
                        if sized > 0:
                            # Gate 1: NO_TRADE blocks order placement
                            if (BOT_DIR / "NO_TRADE").exists() and not dry_run:
                                print(f"  [{ts}] 🔒 NO_TRADE: would {side} ${sized:.2f} @ {conviction}% conviction, edge={edge}, price={entry_price} — BLOCKED")
                                decision["blocked"] = True
                            else:
                                trade_cmd_exec = [
                                    trader_python, trader_script,
                                    "--trade", side, str(entry_price),
                                    f"T{tranche_id}/conv[{conviction}]: {reasoning[:80]}",
                                    "--size", str(sized),
                                    "--confidence", str(conviction),
                                    "--delta", str(compact.get('delta_from_strike', 0)),
                                    "--strike", str(compact.get('strike', 0)),
                                    "--momentum", str(compact.get('momentum_alignment', {}).get('score', 0) if isinstance(compact.get('momentum_alignment'), dict) else 0),
                                    "--brief-file", str(brief_file),
                                ] + (["--live"] if live else [])
                                try:
                                    trade_result = subprocess.run(trade_cmd_exec, capture_output=True, text=True, timeout=15)
                                    if trade_result.stdout:
                                        for tl in trade_result.stdout.strip().split('\n')[-3:]:
                                            print(f"  [{ts}]    {tl.strip()[:100]}")
                                except Exception as te:
                                    print(f"  [{ts}] ⚠️  Trade execution error: {te}")
            else:
                # Fallback: try regex parsing for backwards compatibility
                full = output.upper()
                conv_match = re.search(r'CONVICTION\s+(\d+)', full)
                if conv_match:
                    decision["conviction"] = int(conv_match.group(1))

                if "PASS" in full:
                    decision["action"] = "PASS"
                    decision["conviction"] = decision.get("conviction", 0)
                    decision["reasoning"] = output.strip()[-100:]
                elif "TRADE" in full and "UP" in full and "DOWN" not in full.split("TRADE")[1][:20]:
                    decision["action"] = "BUY_UP"
                    decision["reasoning"] = output.strip()[-100:]
                elif "TRADE" in full and "DOWN" in full:
                    decision["action"] = "BUY_DOWN"
                    decision["reasoning"] = output.strip()[-100:]

                print(f"  [{ts}] ⚠️  JSON parse failed, used regex fallback: {decision['action']}")

        if not DIRECT_API or not output:
            # Only check subprocess result if we used the fallback path
            try:
                if result.returncode != 0 and result.stderr:
                    print(f"  [{ts}] ⚠️  Agent error: {result.stderr[:200]}")
            except NameError:
                pass

        ts2 = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"  [{ts2}]    completed")

        # Write LLM call log with searchable filename
        # Format: W{window}_{action}_{conviction}_{timestamp}_T{tranche}.json
        # e.g. W1772478000_PASS_0_20260302T191500_T1.json
        #      W1772476500_UP_82_20260302T183500_T1.json
        try:
            d_action = decision.get("action", "UNKNOWN").replace("BUY_", "")
            d_conv = decision.get("conviction", 0)
            log_stem = f"W{window_id}_{d_action}_{d_conv}_{call_ts}_T{tranche_id}"
            llm_log = {
                "timestamp": call_ts,
                "window": window_id,
                "tranche": tranche_id,
                "decision": decision,
                **_llm_meta,
            }
            (llm_log_dir / f"{log_stem}.json").write_text(json.dumps(llm_log, indent=2, default=str))
        except Exception as log_err:
            print(f"  [{ts2}]    ⚠️ Failed to write LLM log: {log_err}")

        return decision

    except subprocess.TimeoutExpired:
        ts2 = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"  [{ts2}] ⚠️  T{tranche_id} agent timed out")
        return {"tranche": tranche_id, "action": "TIMEOUT", "reasoning": "Agent timed out"}
    except Exception as e:
        ts2 = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"  [{ts2}] ⚠️  T{tranche_id} error: {e}")
        return {"tranche": tranche_id, "action": "ERROR", "reasoning": str(e)}


# ---- Main Loop ----

def get_observation_snapshot():
    """Medium-weight snapshot for observation mode. Returns dict with key signals."""
    obs = {}
    
    # Order book pressure
    book = fetch_json("https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=20")
    if book:
        bids = sum(float(b[1]) for b in book["bids"])
        asks = sum(float(a[1]) for a in book["asks"])
        ratio = round(bids / asks, 2) if asks > 0 else 999
        obs["ob"] = "BUY" if ratio > 1.5 else "SELL" if ratio < 0.67 else "BAL"
        obs["ob_ratio"] = ratio

    # CVD from recent trades
    trades = fetch_json("https://api.binance.com/api/v3/aggTrades?symbol=BTCUSDT&limit=200")
    if trades:
        buy_vol = sum(float(t["q"]) for t in trades if not t["m"])
        sell_vol = sum(float(t["q"]) for t in trades if t["m"])
        total = buy_vol + sell_vol
        obs["cvd"] = "BUY" if buy_vol > sell_vol * 1.3 else "SELL" if sell_vol > buy_vol * 1.3 else "~"
        obs["buy_pct"] = round(buy_vol / total * 100) if total > 0 else 50

    # 5m and 15m trend from klines
    for tf, label in [("5m", "5m"), ("15m", "15m")]:
        klines = fetch_json(f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={tf}&limit=6")
        if klines:
            closes = [float(k[4]) for k in klines]
            greens = sum(1 for k in klines if float(k[4]) >= float(k[1]))
            net = round(closes[-1] - closes[0], 1)
            obs[label] = f"{greens}G/{len(klines)-greens}R {net:+.0f}"

    # RSI from 1m candles
    klines_1m = fetch_json("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=15")
    if klines_1m and len(klines_1m) >= 7:
        closes = [float(k[4]) for k in klines_1m]
        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i-1]
            gains.append(max(0, diff))
            losses.append(max(0, -diff))
        period = min(6, len(gains))
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            obs["rsi"] = 100
        else:
            rs = avg_gain / avg_loss
            obs["rsi"] = round(100 - (100 / (1 + rs)))

    # Funding rate
    funding = fetch_json("https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=1")
    if funding:
        rate = float(funding[0]["fundingRate"])
        obs["fund"] = f"{rate*100:+.3f}%"

    return obs


def get_quick_delta(cached_strike=None):
    """Fast delta check — Binance spot price vs Chainlink strike.
    Uses Binance for current price (real-time, no lag) and Chainlink only
    for strike capture (authoritative for settlement).
    Returns (current_price, strike, delta) or (current, strike, delta, strike_offset_s)
    when doing initial capture (no cached_strike)."""
    now = time.time()
    current_window = int(now) // 300 * 300

    # Get current price from Binance spot (real-time, no lag)
    current = None
    try:
        ticker = fetch_json("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
        if ticker and "price" in ticker:
            current = round(float(ticker["price"]), 2)
    except Exception:
        pass

    # Fallback to Chainlink if Binance fails
    if current is None:
        cl_url = f"{CHAINLINK_API}?query=LIVE_STREAM_REPORTS_QUERY&variables=%7B%22feedId%22%3A%22{CHAINLINK_FEED_ID}%22%7D"
        cl_data = fetch_json(cl_url)
        if not cl_data or "data" not in cl_data:
            return None, None, None
        nodes = cl_data["data"].get("liveStreamReports", {}).get("nodes", [])
        if not nodes:
            return None, None, None
        current = round(float(nodes[0]["price"]) / 1e18, 2)

    # Use cached strike if available (captured at window start via Chainlink)
    if cached_strike is not None:
        return current, round(cached_strike, 2), round(current - cached_strike, 2)

    # Initial capture: still use Chainlink for strike (authoritative for settlement)
    cl_url = f"{CHAINLINK_API}?query=LIVE_STREAM_REPORTS_QUERY&variables=%7B%22feedId%22%3A%22{CHAINLINK_FEED_ID}%22%7D"
    cl_data = fetch_json(cl_url)
    if not cl_data or "data" not in cl_data:
        return current, None, None
    nodes = cl_data["data"].get("liveStreamReports", {}).get("nodes", [])
    if not nodes:
        return current, None, None
    prices = []
    for n in nodes[:60]:
        ts = datetime.fromisoformat(n["validFromTimestamp"].replace("Z", "+00:00")).timestamp()
        price = float(n["price"]) / 1e18
        prices.append({"ts": ts, "price": price})

    best_strike = None
    best_dist = float("inf")
    for p in prices:
        d = abs(p["ts"] - current_window)
        if d < best_dist:
            best_dist = d
            best_strike = p["price"]
    if best_strike:
        return current, round(best_strike, 2), round(current - best_strike, 2), round(best_dist, 1)
    return current, None, None


def run_loop(dry_run=False, live=False):
    if live:
        mode_str = "[LIVE] TRADING"
    elif dry_run:
        mode_str = "DRY RUN"
    else:
        mode_str = "PAPER TRADING"
    print(f"{'='*65}")
    print(f"◈ Polymarket 5-Min BTC Reasoning Loop — Monitoring Window")
    print(f"   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"   Mode: {mode_str}")
    print(f"   Monitor: {MONITOR_START}-{MONITOR_END}s | Sample every {SAMPLE_INTERVAL}s")
    print(f"   Entry: {MIN_CONSISTENT}/4 consistent + |Δ|≥$30 + Kelly sizing (continuous 120-{MONITOR_END}s)")
    print(f"   Max {MAX_ENTRIES_PER_WINDOW} entries/window, ${MAX_COMBINED_COST} combined cap")
    print(f"   Sizing: Kelly Criterion (min edge {MIN_EDGE*100:.0f}%, min conviction {MIN_CONVICTION*100:.0f}%)")
    print(f"{'='*65}\n")

    # ---- Startup Health Check ----
    print("◈ Running startup health checks...")
    health_ok = True
    checks = []

    # 1. Chainlink price feed
    try:
        cl = fetch_json("https://api.geckoterminal.com/api/v2/simple/networks/eth/token_price/0x514910771af9ca656af840dff83e8264ecf986ca")
        if cl:
            checks.append(("Chainlink feed", "✅"))
        else:
            checks.append(("Chainlink feed", "❌ no data"))
            health_ok = False
    except Exception as e:
        checks.append(("Chainlink feed", f"❌ {e}"))
        health_ok = False

    # 2. Binance spot API
    try:
        ticker = fetch_json("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
        if ticker and "price" in ticker:
            checks.append(("Binance spot", f"✅ BTC=${float(ticker['price']):,.0f}"))
        else:
            checks.append(("Binance spot", "❌ no price"))
            health_ok = False
    except Exception as e:
        checks.append(("Binance spot", f"❌ {e}"))
        health_ok = False

    # 3. Binance futures API
    try:
        fut = fetch_json("https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT")
        if fut and "price" in fut:
            checks.append(("Binance futures", f"✅ BTC=${float(fut['price']):,.0f}"))
        else:
            checks.append(("Binance futures", "❌ no price"))
            health_ok = False
    except Exception as e:
        checks.append(("Binance futures", f"❌ {e}"))
        health_ok = False

    # 4. Polymarket CLOB
    try:
        clob_test = fetch_json(f"{CLOB_BASE}/time")
        if clob_test:
            checks.append(("Polymarket CLOB", "✅ reachable"))
        else:
            checks.append(("Polymarket CLOB", "❌ no response"))
            health_ok = False
    except Exception as e:
        checks.append(("Polymarket CLOB", f"❌ {e}"))
        health_ok = False

    # 5. Ledger integrity
    try:
        ledger = json.load(open(LEDGER_PATH))
        required_keys = ["trades", "stats", "open_positions"]
        missing_keys = [k for k in required_keys if k not in ledger]
        if missing_keys:
            checks.append(("Ledger", f"❌ missing keys: {', '.join(missing_keys)}"))
            health_ok = False
        else:
            s = ledger["stats"]
            checks.append(("Ledger", f"✅ {s.get('wins',0)}W/{s.get('losses',0)}L PnL=${s.get('total_pnl',0):+.2f} | {len(ledger['open_positions'])} open"))
    except Exception as e:
        checks.append(("Ledger", f"❌ {e}"))
        health_ok = False

    # 6. Anthropic API (quick test)
    try:
        import anthropic
        client = anthropic.Anthropic()
        # Just verify the key is valid by checking client creation
        checks.append(("Anthropic API", "✅ client initialized"))
    except Exception as e:
        checks.append(("Anthropic API", f"❌ {e}"))
        health_ok = False

    # 7. Build a test brief to verify all signals populate
    try:
        test_brief = build_brief()
        ta = test_brief.get("technical", {})
        missing_signals = []
        if ta.get("hurst") is None: missing_signals.append("hurst")
        if ta.get("rsi_6") is None: missing_signals.append("rsi_6")
        if ta.get("bb_position") is None: missing_signals.append("bb_position")
        if not test_brief.get("momentum_alignment"): missing_signals.append("momentum_alignment")
        if not test_brief.get("polymarket"): missing_signals.append("polymarket")
        if missing_signals:
            checks.append(("Brief signals", f"⚠️  missing: {', '.join(missing_signals)}"))
        else:
            checks.append(("Brief signals", "✅ all required signals present"))
    except Exception as e:
        checks.append(("Brief signals", f"❌ {e}"))

    # 8. NO_TRADE file
    no_trade = (BOT_DIR / "NO_TRADE").exists()
    if no_trade and not dry_run:
        checks.append(("NO_TRADE", "⊘ active — trading blocked until unlocked"))
    elif no_trade and dry_run:
        checks.append(("NO_TRADE", "⊘ active — bypassed in paper mode"))
    else:
        checks.append(("NO_TRADE", "🔓 not set — trading allowed"))

    # Print results
    for name, result in checks:
        print(f"   {name:<20} {result}")

    if not health_ok:
        print(f"\n❌ HEALTH CHECK FAILED — fix issues before unlocking trading")
    else:
        print(f"\n✅ All health checks passed")
    print()

    window_state = {}
    last_status = 0
    last_resolve = 0

    while True:
        try:
            now = time.time()
            current_window = int(now) // 300 * 300
            elapsed = now - current_window
            remaining = (current_window + 300) - now

            # Init window state
            if current_window not in window_state:
                # Capture strike: poll Chainlink for up to 10s to find the first
                # report AT or AFTER window start (matches Polymarket resolution)
                _cached_strike = None
                try:
                    _strike_found = False
                    time.sleep(8)  # Wait for CL buffer to populate with reports near window boundary
                    for _attempt in range(10):
                        cl_url = f"{CHAINLINK_API}?query=LIVE_STREAM_REPORTS_QUERY&variables=%7B%22feedId%22%3A%22{CHAINLINK_FEED_ID}%22%7D"
                        cl_data = fetch_json(cl_url)
                        if cl_data and "data" in cl_data:
                            nodes = cl_data["data"].get("liveStreamReports", {}).get("nodes", [])
                            # Find report closest to window start (before or after)
                            best_node = None
                            best_gap = float('inf')
                            for n in nodes:
                                report_ts = datetime.fromisoformat(n["validFromTimestamp"].replace("Z", "+00:00")).timestamp()
                                gap = abs(report_ts - current_window)
                                if gap < best_gap:
                                    best_gap = gap
                                    best_node = n
                                    best_report_ts = report_ts
                            if best_node and best_gap < 30:  # sanity: within 30s
                                n = best_node
                                report_ts = best_report_ts
                                if True:
                                    _cached_strike = round(float(n["price"]) / 1e18, 2)
                                    cl_offset = round(report_ts - current_window, 1)
                                    cl_side = "after" if cl_offset >= 0 else "before"
                                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                                    # Cross-check: fetch PM displayed strike
                                    _pm_strike_str = ""
                                    try:
                                        _pm_url = f"https://clob.polymarket.com/markets/btc-updown-5m-{int(current_window)}"
                                        _pm_r = requests.get(_pm_url, timeout=5)
                                        if _pm_r.ok:
                                            _pm_data = _pm_r.json()
                                            _pm_sp = _pm_data.get("strike_price") or _pm_data.get("strikePrice")
                                            if _pm_sp:
                                                _pm_strike_str = f" | PM strike: ${float(_pm_sp):,.2f} (diff: ${abs(_cached_strike - float(_pm_sp)):,.2f})"
                                    except Exception:
                                        pass
                                    print(f"  [{ts}] 📌 Strike cached: ${_cached_strike:,.2f} (CL report {abs(cl_offset)}s {cl_side} window start, attempt {_attempt+1}){_pm_strike_str}")
                                    _strike_found = True
                                    break
                        if _strike_found:
                            break
                        time.sleep(1)
                    if not _strike_found:
                        # Fallback: use closest available price
                        result = get_quick_delta()
                        if result and len(result) >= 3:
                            _, _cached_strike, _ = result[:3]
                            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                            print(f"  [{ts}] ⚠️ Strike fallback (no post-window report found): ${_cached_strike:,.2f}")
                except Exception as e:
                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    print(f"  [{ts}] ⚠️ Strike capture failed: {e}")
                window_state[current_window] = {
                    "delta_samples": [],       # list of (elapsed, delta) tuples
                    "ob_samples": [],          # list of (elapsed, ob_score) tuples
                    "last_sample": 0,          # timestamp of last sample
                    "entries": [],             # trades made this window
                    "entry_delta": None,       # delta at first entry
                    "entry_side": None,        # side of first entry
                    "combined_cost": 0,        # total cost this window
                    "agent_triggered": False,  # agent called this cycle
                    "decisions": [],
                    "done": False,             # no more entries possible
                    "cached_strike": _cached_strike,  # strike captured at window start
                    "guard_blocks": set(),     # distinct guard types that fired this window
                }

            state = window_state[current_window]

            # Status log every 30s
            if now - last_status > 30:
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                window_str = datetime.fromtimestamp(current_window, tz=timezone.utc).strftime("%H:%M")
                live_ledger = LEDGER_PATH  # reasoning.json
                if live_ledger.exists():
                    ledger = json.loads(live_ledger.read_text())
                    s = ledger["stats"]
                    n_entries = len(state["entries"])
                    samples = len(state["delta_samples"])
                    status_extra = f" | samples: {samples} | entries: {n_entries}/{MAX_ENTRIES_PER_WINDOW}"
                    print(f"  [{ts}] Window {window_str} | {elapsed:.0f}s in, {remaining:.0f}s left"
                          f"{status_extra} | PnL=${s['total_pnl']:+.2f} {s['wins']}W/{s['losses']}L")
                else:
                    print(f"  [{ts}] Window {window_str} | {elapsed:.0f}s in, {remaining:.0f}s left")
                last_status = now

            # ---- NO_TRADE observe mode announcement ----
            no_trade_file = BOT_DIR / "NO_TRADE"
            if no_trade_file.exists() and not dry_run:
                if not state.get("no_trade_warned"):
                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    print(f"  [{ts}] ⊘ NO_TRADE active — running in observe mode (no orders will be placed)")
                    state["no_trade_warned"] = True
            elif state.get("no_trade_warned"):
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"\n  [{ts}] 🔓 NO_TRADE lifted — TRADING IS NOW LIVE\n")
                state["no_trade_warned"] = False
            # ---- Monitoring Window Logic ----
            if not state["done"] and MONITOR_START <= elapsed <= MONITOR_END:

                # Sample delta at intervals
                if now - state["last_sample"] >= SAMPLE_INTERVAL:
                    state["last_sample"] = now
                    btc, strike, delta = get_quick_delta(cached_strike=state.get("cached_strike"))
                    # Sample orderbook alongside delta
                    try:
                        _ob_data = fetch_json("https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=20")
                        if _ob_data and _ob_data.get("bids") and _ob_data.get("asks"):
                            _mid = (float(_ob_data["bids"][0][0]) + float(_ob_data["asks"][0][0])) / 2
                            _wb = sum(float(b[1]) / (1 + (_mid - float(b[0])) / 10) for b in _ob_data["bids"][:10])
                            _wa = sum(float(a[1]) / (1 + (float(a[0]) - _mid) / 10) for a in _ob_data["asks"][:10])
                            _tot = _wb + _wa
                            if _tot > 0:
                                state["ob_samples"].append((round(elapsed), round((_wb - _wa) / _tot, 3)))
                    except Exception:
                        pass
                    if delta is not None:
                        state["delta_samples"].append((round(elapsed), delta))
                        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")

                        samples = state["delta_samples"]
                        n = len(samples)

                        # Check consistency: how many of last 4 on same side?
                        recent = samples[-4:] if n >= 4 else samples
                        pos_count = sum(1 for _, d in recent if d > 0)
                        neg_count = sum(1 for _, d in recent if d < 0)
                        consistent_side = None
                        consistent_count = 0
                        if pos_count >= MIN_CONSISTENT and delta > 0:
                            consistent_side = "Up"
                            consistent_count = pos_count
                        elif neg_count >= MIN_CONSISTENT and delta < 0:
                            consistent_side = "Down"
                            consistent_count = neg_count
                        # If current delta disagrees with majority, no consistency

                        # Check if delta is growing (last 3 samples)
                        growing = False
                        if n >= 3:
                            last3 = [abs(d) for _, d in samples[-3:]]
                            growing = last3[-1] >= last3[-2] >= last3[-3]

                        side_arrow = ('▲' if consistent_side == 'Up' else '▼') if consistent_side else ' '
                        momentum_icon = '↑' if growing else '◐'
                        dot = '●' if consistent_side else '○'
                        ob_str = ""
                        if state["ob_samples"]:
                            _last_ob = state["ob_samples"][-1][1]
                            ob_str = f" OB={_last_ob:+.2f}"
                        print(f"  [{ts}] Sample {n}: {dot} {consistent_count}/{len(recent)} {side_arrow}{momentum_icon if consistent_side else '·'} Δ={delta:+.1f}{ob_str}")

                        # ---- Entry Decision ----
                        n_entries = len(state["entries"])

                        if n_entries >= MAX_ENTRIES_PER_WINDOW:
                            continue

                        if abs(delta) < 30:
                            continue

                        # FIRST ENTRY: continuous attempts from 120s until MONITOR_END
                        # Agent is triggered every sample where conditions are met, until a trade or cutoff
                        agent_attempts = state.get("agent_attempts", 0)

                        if n_entries == 0 and consistent_side and n >= MIN_CONSISTENT and elapsed >= 120:
                            # Check if delta is not shrinking
                            if n >= 2 and abs(samples[-1][1]) < abs(samples[-2][1]) * 0.5:
                                print(f"  [{ts}] ⏭️  Delta shrinking fast, waiting...")
                                continue

                            attempt_label = f"attempt {agent_attempts + 1}"
                            ts2 = datetime.now(timezone.utc).strftime("%H:%M:%S")
                            print(f"\n  [{ts2}] ► CONFIRMED ({attempt_label}): {consistent_side} ({consistent_count}/{len(recent)}) | Δ={delta:+.1f} | Building brief...")
                            brief = build_brief(cached_strike=state.get("cached_strike"))
                            # Inject OB history into brief
                            if state["ob_samples"]:
                                obs = state["ob_samples"]
                                ob_scores = [s for _, s in obs]
                                brief["ob_history"] = {
                                    "samples": len(obs),
                                    "current": ob_scores[-1],
                                    "mean": round(sum(ob_scores) / len(ob_scores), 3),
                                    "min": min(ob_scores),
                                    "max": max(ob_scores),
                                    "trend": round(ob_scores[-1] - ob_scores[0], 3) if len(obs) > 1 else 0,
                                    "last_5": ob_scores[-5:],
                                }
                            state["agent_attempts"] = agent_attempts + 1

                            if "chainlink_current" not in brief or "polymarket" not in brief:
                                print(f"  [{ts2}] ⚠️  Incomplete data, skipping")
                                continue

                            # Require key regime/technical signals before trading
                            ta = brief.get("technical", {})
                            required_ta = {"hurst": ta.get("hurst"), "rsi_6": ta.get("rsi_6"), "bb_position": ta.get("bb_position")}
                            required_top = {"momentum_alignment": brief.get("momentum_alignment")}
                            missing = [k for k, v in {**required_ta, **required_top}.items() if v is None]
                            if missing:
                                print(f"  [{ts2}] ⚠️  Missing signals: {', '.join(missing)} — skipping")
                                continue

                            # Guard fatigue: if 3+ distinct guard types already fired this window, skip it
                            if len(state["guard_blocks"]) >= 3:
                                print(f"  [{ts2}] ⊘ Guard fatigue: {len(state['guard_blocks'])} distinct guards fired ({', '.join(sorted(state['guard_blocks']))}) — skipping window")
                                log_pass(brief, f"Guard fatigue: {len(state['guard_blocks'])} distinct guards: {', '.join(sorted(state['guard_blocks']))}", "guard_fatigue")
                                state["done"] = True
                                break

                            # Cheap entry + contra delta guard: don't bet against both market AND data
                            _entry_price = brief.get("polymarket", {}).get("up_mid" if consistent_side == "Up" else "down_mid", 0.5)
                            _delta = brief.get("delta_from_strike", 0)
                            _contra_delta = (consistent_side == "Up" and _delta < 0) or (consistent_side == "Down" and _delta > 0)
                            if _entry_price < 0.50 and _contra_delta:
                                print(f"  [{ts2}] ⊘ Cheap entry + contra delta: {consistent_side} @ {_entry_price:.3f} with delta {_delta:+.1f} — skipping")
                                log_pass(brief, f"Cheap contra: {consistent_side} entry={_entry_price:.3f} delta={_delta:+.1f}", "cheap_contra")
                                state["guard_blocks"].add("cheap_contra")
                                continue

                            # Trajectory contradiction guard: price moving against intended direction
                            _traj = brief.get("price_trajectory", [])
                            if len(_traj) >= 6:
                                _traj_prices = [p["p"] for p in _traj]
                                _consec_decline = 0
                                for _j in range(len(_traj_prices)-1, 0, -1):
                                    if _traj_prices[_j] < _traj_prices[_j-1]:
                                        _consec_decline += 1
                                    else:
                                        break
                                _consec_rise = 0
                                for _j in range(len(_traj_prices)-1, 0, -1):
                                    if _traj_prices[_j] > _traj_prices[_j-1]:
                                        _consec_rise += 1
                                    else:
                                        break
                                if consistent_side == "Up" and _consec_decline >= 5:
                                    print(f"  [{ts2}] ⊘ Trajectory contradiction: betting UP but {_consec_decline} consecutive declining samples — skipping")
                                    log_pass(brief, f"Trajectory contradiction: UP with {_consec_decline} declining samples", "trajectory_contradiction")
                                    state["guard_blocks"].add("trajectory")
                                    continue
                                elif consistent_side == "Down" and _consec_rise >= 5:
                                    print(f"  [{ts2}] ⊘ Trajectory contradiction: betting DOWN but {_consec_rise} consecutive rising samples — skipping")
                                    log_pass(brief, f"Trajectory contradiction: DOWN with {_consec_rise} rising samples", "trajectory_contradiction")
                                    state["guard_blocks"].add("trajectory")
                                    continue

                            # Orderbook contradiction guard: extreme OB imbalance opposing trade direction
                            ob_imb = brief.get("orderbook_imbalance", {})
                            ob_score = ob_imb.get("score", 0)
                            if consistent_side == "Up" and ob_score < -0.6:
                                print(f"  [{ts2}] ⊘ Orderbook contradiction: betting UP but OB score {ob_score:.2f} (strong sell) — skipping")
                                log_pass(brief, f"OB contradiction: UP vs OB score {ob_score:.2f}", "ob_contradiction")
                                state["guard_blocks"].add("ob_contradiction")
                                continue
                            elif consistent_side == "Down" and ob_score > 0.6:
                                print(f"  [{ts2}] ⊘ Orderbook contradiction: betting DOWN but OB score {ob_score:.2f} (strong buy) — skipping")
                                log_pass(brief, f"OB contradiction: DOWN vs OB score {ob_score:.2f}", "ob_contradiction")
                                state["guard_blocks"].add("ob_contradiction")
                                continue

                            # ADX floor guard: no trend = no trade
                            adx = ta.get("adx", 0)
                            if adx is not None and adx < 20:
                                print(f"  [{ts2}] ⊘ No trend: ADX {adx:.1f} < 20 — skipping")
                                log_pass(brief, f"ADX too low: {adx:.1f} < 20", "adx_floor")
                                state["guard_blocks"].add("adx_floor")
                                continue

                            # RSI extreme guard: don't bet into exhausted moves
                            rsi = ta.get("rsi_6", 50)
                            if rsi is not None:
                                if consistent_side == "Up" and rsi > 85:
                                    print(f"  [{ts2}] ⊘ RSI extreme: betting UP but RSI {rsi:.1f} (overbought) — skipping")
                                    log_pass(brief, f"RSI extreme: UP with RSI {rsi:.1f}", "rsi_extreme")
                                    state["guard_blocks"].add("rsi_extreme")
                                    continue
                                elif consistent_side == "Down" and rsi < 15:
                                    print(f"  [{ts2}] ⊘ RSI extreme: betting DOWN but RSI {rsi:.1f} (oversold) — skipping")
                                    log_pass(brief, f"RSI extreme: DOWN with RSI {rsi:.1f}", "rsi_extreme")
                                    state["guard_blocks"].add("rsi_extreme")
                                    continue

                            # Exhaustion guard: triple extreme = move is spent
                            hurst = ta.get("hurst", 0.5)
                            bb = ta.get("bb_position", 50)
                            if hurst is not None and rsi is not None and bb is not None:
                                if consistent_side == "Down" and hurst < 0.3 and rsi < 20 and bb < 10:
                                    print(f"  [{ts2}] ⊘ Exhaustion: DOWN but Hurst={hurst:.2f}, RSI={rsi:.1f}, BB={bb:.1f}% — oversold bounce likely")
                                    log_pass(brief, f"Exhaustion: Hurst={hurst:.2f} RSI={rsi:.1f} BB={bb:.1f}%", "exhaustion")
                                    state["guard_blocks"].add("exhaustion")
                                    continue
                                elif consistent_side == "Up" and hurst < 0.3 and rsi > 80 and bb > 90:
                                    print(f"  [{ts2}] ⊘ Exhaustion: UP but Hurst={hurst:.2f}, RSI={rsi:.1f}, BB={bb:.1f}% — overbought pullback likely")
                                    log_pass(brief, f"Exhaustion: Hurst={hurst:.2f} RSI={rsi:.1f} BB={bb:.1f}%", "exhaustion")
                                    state["guard_blocks"].add("exhaustion")
                                    continue

                            # Check entry price
                            pm = brief.get("polymarket", {})
                            if consistent_side == "Up":
                                entry_price = pm.get("up_mid", pm.get("up_best_ask", pm.get("up_price", 0.5)))
                            else:
                                entry_price = pm.get("down_mid", pm.get("down_best_ask", pm.get("down_price", 0.5)))

                            # Pre-agent price gate: if entry > 0.88, market is heavily priced in
                            if entry_price > 0.92:
                                print(f"  [{ts2}] ⊘ Price gate: {consistent_side} entry {entry_price:.2f} > 0.92 — market already priced in, skipping agent")
                                log_pass(brief, f"Price gate: entry {entry_price:.2f} > 0.92", "price_gate")
                                state["guard_blocks"].add("price_gate")
                                continue

                            # Trigger agent
                            tranche = {"id": 1}
                            decision = trigger_agent(brief, tranche, state["decisions"], dry_run=dry_run, live=live)
                            state["decisions"].append(decision)

                            if decision.get("action", "").startswith("BUY") and decision.get("size", 0) > 0:
                                decision["guard_blocks_before_entry"] = sorted(state["guard_blocks"])
                                state["entries"].append(decision)
                                state["entry_delta"] = delta
                                state["entry_side"] = consistent_side
                                state["combined_cost"] += decision.get("cost", 0)
                                entry_arrow = '▲' if consistent_side == 'Up' else '▼'
                                gb = state["guard_blocks"]
                                gb_str = f" (guards: {','.join(sorted(gb))})" if gb else ""
                                print(f"  [{ts2}] {entry_arrow} Entry 1: {consistent_side} | Δ={delta:+.1f}{gb_str}")
                            elif decision.get("action", "").startswith("BUY") and decision.get("size", 0) == 0:
                                print(f"  [{ts2}]    ⏭️  Edge too low, no entry (Kelly=$0)")
                                log_pass(brief, f"Kelly sized $0 — edge insufficient", "kelly_reject")
                            elif decision.get("action") == "PASS":
                                reason = decision.get("reasoning", "agent PASS")
                                print(f"  [{ts2}]    ⏭️  PASS on {attempt_label} — retrying next sample")
                                log_pass(brief, reason, "agent")

                        # SCALE-IN: need stricter confirmation + delta doubled
                        elif n_entries == 1 and state["entry_side"] and consistent_side == state["entry_side"]:
                            entry_delta = abs(state["entry_delta"])
                            current_delta = abs(delta)

                            # Delta must have grown significantly
                            if current_delta < entry_delta * SCALE_IN_DELTA_RATIO:
                                continue

                            # Need all 4 of last 4 consistent
                            if consistent_count < SCALE_IN_MIN_CONSISTENT or len(recent) < SCALE_IN_MIN_CONSISTENT:
                                continue

                            # Delta must be growing for last 3 samples
                            if not growing:
                                continue

                            # Check combined cost cap
                            remaining_budget = MAX_COMBINED_COST - state["combined_cost"]
                            if remaining_budget < 5:
                                continue

                            ts2 = datetime.now(timezone.utc).strftime("%H:%M:%S")
                            print(f"\n  [{ts2}] ► SCALE-IN: {consistent_side} ({consistent_count}/4) | "
                                  f"Δ={delta:+.1f} (was {state['entry_delta']:+.1f}, {current_delta/entry_delta:.1f}×) | Building brief...")
                            brief = build_brief(cached_strike=state.get("cached_strike"))

                            if "chainlink_current" not in brief or "polymarket" not in brief:
                                continue
                            ta2 = brief.get("technical", {})
                            missing2 = [k for k, v in {"hurst": ta2.get("hurst"), "rsi_6": ta2.get("rsi_6"), "bb_position": ta2.get("bb_position"), "momentum_alignment": brief.get("momentum_alignment")}.items() if v is None]
                            if missing2:
                                continue

                            pm = brief.get("polymarket", {})
                            if consistent_side == "Up":
                                entry_price = pm.get("up_mid", pm.get("up_best_ask", pm.get("up_price", 0.5)))
                            else:
                                entry_price = pm.get("down_mid", pm.get("down_best_ask", pm.get("down_price", 0.5)))

                            tranche = {"id": 2}
                            decision = trigger_agent(brief, tranche, state["decisions"], dry_run=dry_run, live=live)
                            state["decisions"].append(decision)

                            if decision.get("action", "").startswith("BUY"):
                                state["entries"].append(decision)
                                state["combined_cost"] += decision.get("cost", 0)
                                scale_arrow = '▲' if consistent_side == 'Up' else '▼'
                                print(f"  [{ts2}] {scale_arrow} Scale-in: {consistent_side} | Δ={delta:+.1f} | combined ${state['combined_cost']:.0f}")

            # Past monitoring window — mark trading done, continue observing
            if elapsed > MONITOR_END and not state["done"]:
                state["done"] = True
                if not state["entries"]:
                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    cached = state.get("cached_strike")
                    strike_str = f"${cached:,.2f}" if cached else "?"
                    print(f"  [{ts}] ■  Trading window closed — strike: {strike_str} — observing...")

            # Observation mode: disabled for now
            if False and state["done"] and remaining > 10 and now - state["last_sample"] >= SAMPLE_INTERVAL:
                state["last_sample"] = now
                btc, strike, delta = get_quick_delta(cached_strike=state.get("cached_strike"))
                if delta is not None:
                    state["delta_samples"].append((round(elapsed), delta))
                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    n = len(state["delta_samples"])
                    samples = state["delta_samples"]
                    recent = [d for _, d in samples[-4:]]
                    pos_count = sum(1 for d in recent if d > 0)
                    neg_count = sum(1 for d in recent if d < 0)
                    consistent_side = None
                    if pos_count >= 3: consistent_side = "Up"
                    elif neg_count >= 3: consistent_side = "Down"
                    side_arrow = ('▲' if consistent_side == 'Up' else '▼') if consistent_side else '○'
                    
                    # Every 3rd observation, fetch richer data
                    extra = ""
                    obs_count = n - len([s for s in samples if s[0] <= MONITOR_END])
                    if obs_count % 3 == 1:
                        try:
                            obs = get_observation_snapshot()
                            parts = []
                            if "ob" in obs: parts.append(f"OB:{obs['ob']}({obs['ob_ratio']})")
                            if "cvd" in obs: parts.append(f"CVD:{obs['cvd']}({obs['buy_pct']}%buy)")
                            if "rsi" in obs: parts.append(f"RSI:{obs['rsi']}")
                            if "15m" in obs: parts.append(f"15m:{obs['15m']}")
                            if "1h" in obs: parts.append(f"1h:{obs['1h']}")
                            if "fund" in obs: parts.append(f"F:{obs['fund']}")
                            extra = " | " + " ".join(parts)
                        except:
                            pass
                    
                    print(f"  [{ts}] ○ {n}: Δ={delta:+.1f} | {side_arrow} | btc=${btc:,.0f}{extra}")

            # Background resolve every 15s
            if now - last_resolve > 15:
                last_resolve = now
                try:
                    if live:
                        resolve_cmd = ["python3.12", str(BOT_DIR / "live-trader.py"), "--resolve"]
                    else:
                        resolve_cmd = ["python3", str(BOT_DIR / "reasoning-trader.py"), "--resolve"]
                    result = subprocess.run(resolve_cmd, capture_output=True, text=True, timeout=15)
                    # Trigger browser redemption if a win was resolved
                    # live-trader.py prints "+$" for wins (positive PnL)
                    if result.stdout and "+$" in result.stdout:
                        try:
                            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                            print(f"  [{ts}]   🔄 Triggering browser-based redemption...", flush=True)
                            r = subprocess.run(
                                [str(BOT_DIR.parent / "shared" / "redeem-browser.sh")],
                                capture_output=True, text=True, timeout=15
                            )
                            if r.returncode == 0:
                                print(f"  [{ts}]   ✅ Redeem task scheduled", flush=True)
                        except Exception as e:
                            print(f"  [{ts}]   ⚠ Redeem trigger error: {e}", flush=True)
                except Exception:
                    pass

            # Clean old window state (keep last 10)
            if len(window_state) > 10:
                for old_w in sorted(window_state.keys())[:-10]:
                    del window_state[old_w]

            # Sleep — tight during monitoring + observation, relaxed otherwise
            if MONITOR_START <= elapsed and remaining > 10:
                # During monitoring or observation: sleep until next sample
                next_sample = state["last_sample"] + SAMPLE_INTERVAL - now
                time.sleep(max(1, min(5, next_sample)))
            elif elapsed < MONITOR_START:
                # Before monitoring: sleep until it starts
                time.sleep(min(10, max(1, MONITOR_START - elapsed)))
            else:
                # Final seconds: relax until next window
                time.sleep(min(10, max(1, remaining - 5)))

        except KeyboardInterrupt:
            print("\n  Shutting down...")
            break
        except Exception as e:
            import traceback
            print(f"  ⚠️  Error: {e}")
            traceback.print_exc()
            time.sleep(10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket Reasoning Loop — Tranched")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--live", action="store_true", help="Execute real trades via live-trader.py")
    args = parser.parse_args()
    try:
        run_loop(dry_run=args.dry_run, live=args.live)
    except Exception as e:
        import traceback
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"\n  [{ts}] 💀 FATAL: {e}", flush=True)
        traceback.print_exc()
        sys.exit(1)
    finally:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"\n  [{ts}] ⚡ Process exiting (run_loop returned)", flush=True)
