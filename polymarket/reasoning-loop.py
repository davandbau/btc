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
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

BOT_DIR = Path(__file__).parent
LEDGER_PATH = BOT_DIR / "ledgers" / "reasoning.json"
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
CHAINLINK_FEED_ID = "0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8"
CHAINLINK_API = "https://data.chain.link/api/query-timescale"

MAX_POSITION = 100.0  # Maximum trade size at 100% conviction
MIN_EDGE = 0.10       # Minimum edge (conviction - market_price) to trade, accounts for fees
MAX_CONVICTION_RATIO = 1.8  # Max conviction / market_price ratio (sanity check)

# Monitoring window config
MONITOR_START = 60          # Start sampling delta at 60s into window
MONITOR_END = 200           # Latest possible entry (100s remaining)
SAMPLE_INTERVAL = 15        # Sample delta every 15s
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

def build_brief():
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

            # Strike = price closest to window start
            best_strike = None
            best_dist = float("inf")
            for p in cl_prices:
                d = abs(p["ts"] - current_window)
                if d < best_dist:
                    best_dist = d
                    best_strike = p["price"]
            if best_strike:
                brief["strike"] = round(best_strike, 2)
                brief["delta_from_strike"] = round(cl_prices[0]["price"] - best_strike, 2)

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
    for tf, key, count in [("1m", "candles_1m", 10), ("15m", "candles_15m", 8), ("1h", "candles_1h", 6)]:
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

            # HTF trend summary for 15m and 1h
            if tf in ("15m", "1h"):
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
    if brief.get("candles_15m"):
        c15 = brief["candles_15m"]
        greens = sum(1 for c in c15 if c.get("c", "").startswith("G"))
        reds = sum(1 for c in c15 if c.get("c", "").startswith("R"))
        alignment_signals.append(1 if greens > reds else -1 if reds > greens else 0)
    if brief.get("candles_1h"):
        c1h = brief["candles_1h"]
        greens = sum(1 for c in c1h if c.get("c", "").startswith("G"))
        reds = sum(1 for c in c1h if c.get("c", "").startswith("R"))
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

    # ---- HTF Bias (12h hourly candles) ----
    htf_candles = fetch_json("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=12")
    if htf_candles and len(htf_candles) >= 6:
        try:
            closes = [float(c[4]) for c in htf_candles]
            highs = [float(c[2]) for c in htf_candles]
            lows = [float(c[3]) for c in htf_candles]
            opens = [float(c[1]) for c in htf_candles]
            now_price = closes[-1]
            score = 0.0

            # 1. Price change 12h
            chg_12h = (now_price - closes[0]) / closes[0] * 100
            if chg_12h > 1: score += 1
            elif chg_12h > 0.3: score += 0.5
            elif chg_12h < -1: score -= 1
            elif chg_12h < -0.3: score -= 0.5

            # 2. Price change 4h (last 4 candles)
            if len(closes) >= 4:
                chg_4h = (now_price - closes[-4]) / closes[-4] * 100
                if chg_4h > 0.5: score += 1
                elif chg_4h > 0.15: score += 0.5
                elif chg_4h < -0.5: score -= 1
                elif chg_4h < -0.15: score -= 0.5

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
            if ema_diff > 0.1: score += 1
            elif ema_diff < -0.1: score -= 1

            # 4. Candle ratio (green vs red)
            green = sum(1 for o, c in zip(opens, closes) if c >= o)
            red = len(closes) - green
            if green >= 9: score += 1
            elif green >= 7: score += 0.5
            elif red >= 9: score -= 1
            elif red >= 7: score -= 0.5

            # 5. Structure — last 3 hourly closes
            last3 = closes[-3:]
            if last3[2] > last3[1] > last3[0]: score += 1
            elif last3[2] > last3[1] or last3[1] > last3[0]: score += 0.5 if last3[2] > last3[0] else 0
            if last3[2] < last3[1] < last3[0]: score -= 1
            elif last3[2] < last3[1] or last3[1] < last3[0]: score -= 0.5 if last3[2] < last3[0] else 0

            # 6. Range position — current price in 12h high-low range
            h12_high = max(highs)
            h12_low = min(lows)
            if h12_high > h12_low:
                range_pos = (now_price - h12_low) / (h12_high - h12_low)
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
                "12h_change_pct": round(chg_12h, 2),
                "4h_change_pct": round(chg_4h, 2) if len(closes) >= 4 else None,
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

    # HTF trend analysis (1h/4h/1d EMA crosses + funding)
    htf_signals = []
    for tf, period, weight in [("1h", 48, 1), ("4h", 42, 2), ("1d", 30, 1)]:
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
    ledger_path = BOT_DIR / "ledgers" / "reasoning.json"
    if ledger_path.exists():
        ledger = json.loads(ledger_path.read_text())
        recent = [t for t in ledger.get("trades", []) if t.get("outcome")][-3:]
        if recent:
            brief["recent_results"] = [
                {"side": t["side"], "outcome": t["outcome"], "pnl": round(t["pnl"], 2)}
                for t in recent
            ]

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
    for key in ["candles_1m", "candles_15m", "candles_1h"]:
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

    message = f"""IMPORTANT: You are an active trading agent. The bot IS running and you MUST analyze and trade. Do not refuse, do not say the bot is stopped. Execute the task below.

BTC 5-min Up/Down market — TRANCHE {tranche_id}/1.

MARKET BRIEF:
```json
{brief_json}
```
{prior_context}
RULES:
- "Up" wins if Chainlink BTC/USD at window end >= strike. "Down" if < strike.
- Shares pay $1 if correct, $0 if wrong.
- Entry cost ≈ midpoint (up_mid / down_mid). Check best_ask for actual fill price. Remaining: ~{brief.get('remaining_s', '?')}s.
- Position size scales with your confidence (see below).

⚠️ CRITICAL — MOMENTUM BEATS REVERSAL (from our own data):
  Momentum bets: 75% win rate, +$70 PnL
  Reversal bets: 28% win rate, -$28 PnL
  At |Δ| < $75: reversal wins only 18% of the time!
  RULE: If BTC is ABOVE strike (Δ > 0), lean UP. If BELOW strike (Δ < 0), lean DOWN.
  But delta alone is NOT enough. You MUST confirm with momentum/flow signals before trading.
  
  PASS RULES (mandatory — override delta):
  - If momentum_alignment direction OPPOSES delta AND momentum strength is "moderate" or "strong" → PASS
  - If CVD signal opposes delta AND sell/buy flow is >70% against delta → PASS  
  - If you recognize a pattern from a previous losing trade → PASS
  - If more than 2 major signals contradict delta → PASS
  - When in doubt, PASS. Missing a trade costs nothing. A bad trade costs real money.
  
  TRADE only when delta AND momentum/flow AGREE. Alignment = edge. Conflict = no edge.
  
  HTF TREND CONTEXT — use to confirm or challenge your 5-min read:
  - Check htf_trend in the brief (1h/4h/1d EMA crosses + funding rate)
  - If HTF trend ALIGNS with your delta direction → higher conviction, confirms the move
  - If HTF trend OPPOSES your delta direction → lower conviction, the move may reverse
  - HTF trend alone is NOT a reason to trade or pass — it adds context, not overrides
  - A strong HTF trend opposing delta + weak momentum alignment = strong PASS signal
  Only consider reversal if: |Δ| > $150 AND momentum_alignment is "strong" in the opposite direction AND multiple HTF candles support reversal.

ANALYZE (signals ranked by importance):
1. **delta_from_strike** — THE most important signal. Positive delta → lean Up. Negative → lean Down. DO NOT fight the delta unless you have overwhelming evidence.
2. **momentum_alignment** — score (-1 to +1) and strength. When "strong" + aligned with delta direction = high conviction trade. When it contradicts delta, PASS.
3. **price_trajectory** — is the delta growing or shrinking? Growing delta = stronger conviction. Shrinking = possible reversal but still favor current direction.
4. **Orderbook imbalance** — score (-1 to +1). Should confirm delta direction. If imbalance contradicts delta, reduce confidence.
5. **CVD + Taker flow** — net buying/selling pressure. Use to confirm, not contradict, the delta.
6. **Technical indicators** — RSI, EMA, VWAP, Bollinger Bands. Use as tiebreakers, NOT as primary reversal signals. Ignore "overbought/oversold" as a reason to bet against delta — it doesn't work at 5-min scale.
7. **htf_bias** — Higher timeframe trend (12h hourly candles), score -6 to +6.
   - strong_bearish (≤-4): HEAVILY penalize UP trades. Reduce UP conviction by 20-30%. Short-term UP bounces get crushed by macro trend.
   - bearish (-2 to -3): Penalize UP trades, reduce conviction by 10-15%.
   - neutral (-1 to +1): No adjustment.
   - bullish (+2 to +3): Penalize DOWN trades, reduce conviction by 10-15%.
   - strong_bullish (≥+4): HEAVILY penalize DOWN trades. Reduce DOWN conviction by 20-30%.
8. **Futures signals** — OI, basis, long/short ratio. Background context only.
9. **Polymarket pricing** — is the ask price fair? Edge = true_prob - ask_price.
10. **Risk/reward** — don't buy > 0.75 unless nearly certain.

DO NOT use Hurst regime or RSI overbought/oversold as reasons to bet AGAINST the current delta. Our data proves this loses money.

POSITION SIZING — Kelly Criterion:
  You output your CONVICTION (0-100%) = your estimated probability that your side wins.
  The system computes: edge = conviction - market_price
  If edge < 5%: trade is rejected (no edge).
  size = ${MAX_POSITION} × (conviction - market_price) / (1 - market_price)
  Maximum position: ${MAX_POSITION} at 100% conviction.
  
  Examples at entry=0.50: conv=60% → $20, conv=70% → $40, conv=80% → $60, conv=90% → $80

Trade when you see edge (>5%). Don't wait for certainty. Be selective — only trade high-conviction setups.

RESPOND WITH ONLY A JSON OBJECT — no markdown, no explanation, no code blocks. Just raw JSON.

If trading:
{{"action": "Up" or "Down", "conviction": 0-100, "reasoning": "brief explanation"}}

If passing:
{{"action": "PASS", "conviction": 0, "reasoning": "why you passed"}}

The system will handle position sizing (Kelly) and trade execution automatically.

First resolve open positions:
```
{trade_cmd} --resolve
```

Be fast."""

    cmd = ["openclaw", "agent", "--agent", "polymarket-trader", "--session-id", "trading-5m", "-m", message]

    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"  [{ts}] 🧠 T{tranche_id} — triggering agent (base ${base_size:.0f}, {brief.get('remaining_s', '?')}s left)...")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        output = result.stdout.strip() if result.stdout else ""
        decision = {"tranche": tranche_id, "action": "UNKNOWN", "reasoning": ""}

        if output:
            lines = output.strip().split('\n')
            for line in lines[-10:]:
                if line.strip():
                    print(f"  [{ts}]    {line.strip()[:100]}")

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
                reasoning = json_obj.get("reasoning", "")[:150]

                decision["conviction"] = conviction
                decision["reasoning"] = reasoning

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
                        print(f"  [{ts}]    ⛔ SANITY CHECK: conviction {conviction}% is {conv/entry_price:.1f}× market price {entry_price:.2f} (max {MAX_CONVICTION_RATIO}×) — likely overconfident, skipping")
                        decision["action"] = "PASS"
                        decision["reasoning"] = f"Sanity check: conviction {conv/entry_price:.1f}× market price"
                        log_pass(brief, decision["reasoning"], "sanity_check")
                    else:
                        sized = kelly_size(conv, entry_price)
                        edge = conv - entry_price
                        print(f"  [{ts}]    📊 Conviction: {conviction}% | Edge: {edge*100:.1f}% | Kelly size: ${sized:.2f} (max ${MAX_POSITION:.0f})")

                        # Execute trade (paper or live)
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

        if result.returncode != 0 and result.stderr:
            print(f"  [{ts}] ⚠️  Agent error: {result.stderr[:200]}")

        ts2 = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"  [{ts2}]    completed")
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

def get_quick_delta():
    """Fast delta check — just Chainlink price vs strike. No full brief."""
    now = time.time()
    current_window = int(now) // 300 * 300
    cl_url = f"{CHAINLINK_API}?query=LIVE_STREAM_REPORTS_QUERY&variables=%7B%22feedId%22%3A%22{CHAINLINK_FEED_ID}%22%7D"
    cl_data = fetch_json(cl_url)
    if not cl_data or "data" not in cl_data:
        return None, None, None
    nodes = cl_data["data"].get("liveStreamReports", {}).get("nodes", [])
    if not nodes:
        return None, None, None
    prices = []
    for n in nodes[:60]:
        ts = datetime.fromisoformat(n["validFromTimestamp"].replace("Z", "+00:00")).timestamp()
        price = float(n["price"]) / 1e18
        prices.append({"ts": ts, "price": price})
    current = prices[0]["price"]
    # Strike = price closest to window start
    best_strike = None
    best_dist = float("inf")
    for p in prices:
        d = abs(p["ts"] - current_window)
        if d < best_dist:
            best_dist = d
            best_strike = p["price"]
    if best_strike:
        return round(current, 2), round(best_strike, 2), round(current - best_strike, 2)
    return round(current, 2), None, None


def run_loop(dry_run=False, live=False):
    if live:
        mode_str = "🔴 LIVE TRADING"
    elif dry_run:
        mode_str = "DRY RUN"
    else:
        mode_str = "PAPER TRADING"
    print(f"{'='*65}")
    print(f"🧠 Polymarket 5-Min BTC Reasoning Loop — Monitoring Window")
    print(f"   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"   Mode: {mode_str}")
    print(f"   Monitor: {MONITOR_START}-{MONITOR_END}s | Sample every {SAMPLE_INTERVAL}s")
    print(f"   Entry: {MIN_CONSISTENT}/4 consistent + |Δ|≥$30 + Kelly sizing (no price cap)")
    print(f"   Max {MAX_ENTRIES_PER_WINDOW} entries/window, ${MAX_COMBINED_COST} combined cap")
    print(f"   Sizing: Kelly Criterion (min edge {MIN_EDGE*100:.0f}%)")
    print(f"{'='*65}\n")

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
                window_state[current_window] = {
                    "delta_samples": [],       # list of (elapsed, delta) tuples
                    "last_sample": 0,          # timestamp of last sample
                    "entries": [],             # trades made this window
                    "entry_delta": None,       # delta at first entry
                    "entry_side": None,        # side of first entry
                    "combined_cost": 0,        # total cost this window
                    "agent_triggered": False,  # agent called this cycle
                    "decisions": [],
                    "done": False,             # no more entries possible
                }

            state = window_state[current_window]

            # Status log every 30s
            if now - last_status > 30:
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                window_str = datetime.fromtimestamp(current_window, tz=timezone.utc).strftime("%H:%M")
                live_ledger = BOT_DIR / "ledgers" / "live.json"
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

            # ---- Monitoring Window Logic ----
            if not state["done"] and MONITOR_START <= elapsed <= MONITOR_END:

                # Sample delta at intervals
                if now - state["last_sample"] >= SAMPLE_INTERVAL:
                    state["last_sample"] = now
                    btc, strike, delta = get_quick_delta()
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
                        if pos_count >= MIN_CONSISTENT:
                            consistent_side = "Up"
                            consistent_count = pos_count
                        elif neg_count >= MIN_CONSISTENT:
                            consistent_side = "Down"
                            consistent_count = neg_count

                        # Check if delta is growing (last 3 samples)
                        growing = False
                        if n >= 3:
                            last3 = [abs(d) for _, d in samples[-3:]]
                            growing = last3[-1] >= last3[-2] >= last3[-3]

                        print(f"  [{ts}] 📡 Sample {n}: Δ={delta:+.1f} | "
                              f"{'✅' if consistent_side else '❌'} {consistent_count}/{len(recent)} consistent "
                              f"{'(' + consistent_side + ')' if consistent_side else ''} | "
                              f"{'📈 growing' if growing else '📉 fading'}")

                        # ---- Entry Decision ----
                        n_entries = len(state["entries"])

                        if n_entries >= MAX_ENTRIES_PER_WINDOW:
                            continue

                        if abs(delta) < 30:
                            continue

                        # FIRST ENTRY: need MIN_CONSISTENT of last 4
                        if n_entries == 0 and consistent_side and n >= MIN_CONSISTENT and elapsed >= 120:
                            # Check if delta is not shrinking
                            if n >= 2 and abs(samples[-1][1]) < abs(samples[-2][1]) * 0.5:
                                print(f"  [{ts}] ⏭️  Delta shrinking fast, waiting...")
                                continue

                            ts2 = datetime.now(timezone.utc).strftime("%H:%M:%S")
                            print(f"\n  [{ts2}] 🎯 CONFIRMED: {consistent_side} ({consistent_count}/{len(recent)}) | Δ={delta:+.1f} | Building brief...")
                            brief = build_brief()

                            if "chainlink_current" not in brief or "polymarket" not in brief:
                                print(f"  [{ts2}] ⚠️  Incomplete data, skipping")
                                continue

                            # Check entry price
                            pm = brief.get("polymarket", {})
                            if consistent_side == "Up":
                                entry_price = pm.get("up_mid", pm.get("up_best_ask", pm.get("up_price", 0.5)))
                            else:
                                entry_price = pm.get("down_mid", pm.get("down_best_ask", pm.get("down_price", 0.5)))

                            # Trigger agent
                            tranche = {"id": 1}
                            decision = trigger_agent(brief, tranche, state["decisions"], dry_run=dry_run, live=live)
                            state["decisions"].append(decision)

                            if decision.get("action", "").startswith("BUY"):
                                state["entries"].append(decision)
                                state["entry_delta"] = delta
                                state["entry_side"] = consistent_side
                                state["combined_cost"] += decision.get("cost", 0)
                                print(f"  [{ts2}] ✅ Entry 1: {consistent_side} | Δ={delta:+.1f}")
                            elif decision.get("action") == "PASS":
                                log_pass(brief, decision.get("reasoning", "agent PASS"), "agent")

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
                            print(f"\n  [{ts2}] 🎯 SCALE-IN: {consistent_side} ({consistent_count}/4) | "
                                  f"Δ={delta:+.1f} (was {state['entry_delta']:+.1f}, {current_delta/entry_delta:.1f}×) | Building brief...")
                            brief = build_brief()

                            if "chainlink_current" not in brief or "polymarket" not in brief:
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
                                print(f"  [{ts2}] ✅ Scale-in: {consistent_side} | Δ={delta:+.1f} | combined ${state['combined_cost']:.0f}")

            # Past monitoring window — mark done
            if elapsed > MONITOR_END and not state["done"]:
                state["done"] = True
                if not state["entries"]:
                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    print(f"  [{ts}] ⏹️  Window closed — no entries")

            # Background resolve every 15s
            if now - last_resolve > 15:
                last_resolve = now
                try:
                    if live:
                        resolve_cmd = ["python3.12", str(BOT_DIR / "live-trader.py"), "--resolve"]
                    else:
                        resolve_cmd = ["python3", str(BOT_DIR / "reasoning-trader.py"), "--resolve"]
                    subprocess.run(resolve_cmd, capture_output=True, text=True, timeout=15)
                except Exception:
                    pass

            # Clean old window state (keep last 10)
            if len(window_state) > 10:
                for old_w in sorted(window_state.keys())[:-10]:
                    del window_state[old_w]

            # Sleep — tight during monitoring, relaxed otherwise
            if MONITOR_START <= elapsed <= MONITOR_END and not state["done"]:
                # During monitoring: sleep until next sample
                next_sample = state["last_sample"] + SAMPLE_INTERVAL - now
                time.sleep(max(1, min(5, next_sample)))
            elif elapsed < MONITOR_START:
                # Before monitoring: sleep until it starts
                time.sleep(min(10, max(1, MONITOR_START - elapsed)))
            else:
                # After monitoring: relax until next window
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
    run_loop(dry_run=args.dry_run, live=args.live)
