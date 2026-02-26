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
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
CHAINLINK_FEED_ID = "0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8"
CHAINLINK_API = "https://data.chain.link/api/query-timescale"

TOTAL_POSITION = 30.0

TRANCHES = [
    {"id": 1, "trigger_elapsed": 120, "base_size": 30.0},  # single tranche, full size, 2min in
]

def scale_size(base, confidence, entry_price=None):
    """Scale position size by confidence and entry price zone.
    
    From our data:
    - C2 is the sweet spot (59% WR, $4.37/trade) → size up
    - C1 is marginal (42% WR) → size down
    - C3+ is losing → size down
    - Cheap shares (<0.25) have huge asymmetry → size up
    - Expensive shares (>0.55) have 81% WR → size up  
    - Mid-range (0.25-0.55) is danger zone → size down
    """
    confidence = max(1, min(5, confidence))
    
    # Confidence multiplier: C1=0.4, C2=1.3, C3=0.7, C4=0.9, C5=1.0
    conf_mult = {1: 0.4, 2: 1.3, 3: 0.7, 4: 0.9, 5: 1.0}[confidence]
    
    # Entry price multiplier (applied when we know the price)
    price_mult = 1.0
    if entry_price is not None:
        if entry_price < 0.25:
            price_mult = 1.4   # huge asymmetry, size up
        elif entry_price > 0.55:
            price_mult = 1.3   # high win rate, size up
        elif 0.30 <= entry_price <= 0.50:
            price_mult = 0.6   # danger zone, size down
    
    return round(base * conf_mult * price_mult, 2)


def fetch_json(url, timeout=8):
    req = Request(url, headers={"User-Agent": "reasoning-loop/2.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except:
        return None


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

                    # CLOB orderbook for best bid/ask
                    for side_name, token_idx in [("up", up_idx), ("down", down_idx)]:
                        if tokens:
                            clob = fetch_json(f"{CLOB_BASE}/book?token_id={tokens[token_idx]}")
                            if clob:
                                best_bid = float(clob["bids"][0]["price"]) if clob.get("bids") else None
                                best_ask = float(clob["asks"][0]["price"]) if clob.get("asks") else None
                                brief["polymarket"][f"{side_name}_best_bid"] = best_bid
                                brief["polymarket"][f"{side_name}_best_ask"] = best_ask
                except:
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

def trigger_agent(brief, tranche, prior_decisions, dry_run=False):
    """Spawn an OpenClaw agent to make a trade decision."""
    tranche_id = tranche["id"]
    base_size = tranche["base_size"]
    trade_cmd = f"python3 {BOT_DIR / 'reasoning-trader.py'}"

    brief_json = json.dumps(brief, indent=2, default=str)

    prior_context = ""
    if prior_decisions:
        prior_context = "\n\nPRIOR TRANCHES THIS WINDOW:\n"
        for pd in prior_decisions:
            prior_context += f"  T{pd['tranche']}: {pd['action']} (conf={pd.get('confidence','?')}) — {pd['reasoning'][:80]}\n"
        prior_context += "\nYou can: add to the same side, take the opposite side, or PASS. Each tranche is independent.\n"

    message = f"""BTC 5-min Up/Down market — TRANCHE {tranche_id}/1.

MARKET BRIEF:
```json
{brief_json}
```
{prior_context}
RULES:
- "Up" wins if Chainlink BTC/USD at window end >= strike. "Down" if < strike.
- Shares pay $1 if correct, $0 if wrong.
- Buy at best_ask for your side. Remaining: ~{brief.get('remaining_s', '?')}s.
- Position size scales with your confidence (see below).

⚠️ CRITICAL — MOMENTUM BEATS REVERSAL (from our own data):
  Momentum bets: 75% win rate, +$70 PnL
  Reversal bets: 28% win rate, -$28 PnL
  At |Δ| < $75: reversal wins only 18% of the time!
  RULE: If BTC is ABOVE strike (Δ > 0), bet UP. If BELOW strike (Δ < 0), bet DOWN.
  Only consider reversal if: |Δ| > $150 AND momentum_alignment is "strong" in the opposite direction AND multiple HTF candles support reversal.
  When in doubt, GO WITH THE DELTA.

ANALYZE (signals ranked by importance):
1. **delta_from_strike** — THE most important signal. Positive delta → lean Up. Negative → lean Down. DO NOT fight the delta unless you have overwhelming evidence.
2. **momentum_alignment** — score (-1 to +1) and strength. When "strong" + aligned with delta direction = high conviction trade. When it contradicts delta, PASS.
3. **price_trajectory** — is the delta growing or shrinking? Growing delta = stronger conviction. Shrinking = possible reversal but still favor current direction.
4. **Orderbook imbalance** — score (-1 to +1). Should confirm delta direction. If imbalance contradicts delta, reduce confidence.
5. **CVD + Taker flow** — net buying/selling pressure. Use to confirm, not contradict, the delta.
6. **Technical indicators** — RSI, EMA, VWAP, Bollinger Bands. Use as tiebreakers, NOT as primary reversal signals. Ignore "overbought/oversold" as a reason to bet against delta — it doesn't work at 5-min scale.
7. **Futures signals** — OI, basis, long/short ratio. Background context only.
8. **Polymarket pricing** — is the ask price fair? Edge = true_prob - ask_price.
9. **Risk/reward** — don't buy > 0.75 unless nearly certain.

DO NOT use Hurst regime or RSI overbought/oversold as reasons to bet AGAINST the current delta. Our data proves this loses money.

CONFIDENCE SCORING (1-5) — C2 is our historical sweet spot:
  1 = Slight lean, barely any edge. Size: ${scale_size(base_size, 1)}
  2 = Good signal, supporting data aligns with delta. Size: ${scale_size(base_size, 2)} ← BEST historical performance
  3 = Strong edge, multiple signals confirm. Size: ${scale_size(base_size, 3)}
  4 = Very strong setup, clear mispricing. Size: ${scale_size(base_size, 4)}
  5 = Slam dunk, everything aligns. Size: ${scale_size(base_size, 5)}

PRICE ZONE MULTIPLIER (apply ON TOP of confidence size):
  Entry < 0.25: multiply size by 1.4 (huge asymmetry — small loss if wrong, big win if right)
  Entry > 0.55: multiply size by 1.3 (81% historical win rate)
  Entry 0.30-0.50: multiply size by 0.6 (danger zone — worst risk/reward)
  Other: 1.0x
Prefer C2 — our data shows C2 has 59% WR and $4.37/trade. C1 is too tentative, C3+ overthinks it.

This is paper trading — we WANT data. Trade when you see any edge (>5%). Don't wait for certainty — by then the market has priced it in and there's no edge left.

IF TRADING, respond with EXACTLY this format on the FIRST line, then run the command:
TRADE [UP/DOWN] [CONFIDENCE 1-5] [PRICE]

Then execute:
```
{trade_cmd} --trade "SIDE" "PRICE" "T{tranche_id}/C[conf]: reasoning" --size SIZE
```
Where SIZE is the scaled amount from the confidence table above.

IF PASSING: respond with EXACTLY: PASS [CONFIDENCE 0] — reason

First resolve open positions:
```
{trade_cmd} --resolve
```

Be fast."""

    cmd = ["openclaw", "agent", "--agent", "main", "-m", message]

    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"  [{ts}] 🧠 T{tranche_id} — triggering agent (base ${base_size:.0f}, {brief.get('remaining_s', '?')}s left)...")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        output = result.stdout.strip() if result.stdout else ""
        decision = {"tranche": tranche_id, "action": "UNKNOWN", "reasoning": ""}

        if output:
            lines = output.strip().split('\n')
            for line in lines[-10:]:
                if line.strip():
                    print(f"  [{ts}]    {line.strip()[:100]}")

            # Parse decision and confidence from output
            full = output.upper()

            # Try to extract confidence
            conf_match = re.search(r'CONFIDENCE\s+(\d)', full)
            if conf_match:
                decision["confidence"] = int(conf_match.group(1))

            if "PASS" in full:
                decision["action"] = "PASS"
                decision["confidence"] = decision.get("confidence", 0)
                decision["reasoning"] = output.strip()[-100:]
            elif "TRADE" in full and "UP" in full and "DOWN" not in full.split("TRADE")[1][:20]:
                decision["action"] = "BUY_UP"
                decision["reasoning"] = output.strip()[-100:]
            elif "TRADE" in full and "DOWN" in full:
                decision["action"] = "BUY_DOWN"
                decision["reasoning"] = output.strip()[-100:]
            elif "--TRADE" in full:
                if '"UP"' in full:
                    decision["action"] = "BUY_UP"
                elif '"DOWN"' in full:
                    decision["action"] = "BUY_DOWN"
                decision["reasoning"] = output.strip()[-100:]

            # Log confidence and scaled size
            conf = decision.get("confidence", 3)
            sized = scale_size(base_size, conf)
            if decision["action"] not in ("PASS", "UNKNOWN"):
                print(f"  [{ts}]    📊 Confidence: {conf}/5 → ${sized:.2f} (base ${base_size:.0f})")

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

def run_loop(dry_run=False):
    print(f"{'='*65}")
    print(f"🧠 Polymarket 5-Min BTC Reasoning Loop — Tranched Entry")
    print(f"   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"   Mode: {'DRY RUN' if dry_run else 'PAPER TRADING'}")
    print(f"   Single tranche: T1@120s(${TRANCHES[0]['base_size']:.0f}) — ${TOTAL_POSITION:.0f} total")
    print(f"   Sizing: confidence 1-5 scales 0.5x-1.5x base")
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
                    "triggered": set(),
                    "decisions": [],
                }

            state = window_state[current_window]

            # Status log every 30s
            if now - last_status > 30:
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                window_str = datetime.fromtimestamp(current_window, tz=timezone.utc).strftime("%H:%M")
                ledger_path = BOT_DIR / "ledgers" / "reasoning.json"
                if ledger_path.exists():
                    ledger = json.loads(ledger_path.read_text())
                    s = ledger["stats"]
                    triggered = ",".join(f"T{t}" for t in sorted(state["triggered"])) or "—"
                    print(f"  [{ts}] Window {window_str} | {elapsed:.0f}s in, {remaining:.0f}s left | "
                          f"tranches: {triggered} | PnL=${s['total_pnl']:+.2f} {s['wins']}W/{s['losses']}L")
                else:
                    print(f"  [{ts}] Window {window_str} | {elapsed:.0f}s in, {remaining:.0f}s left")
                last_status = now

            # Check each tranche
            for tranche in TRANCHES:
                tid = tranche["id"]
                trigger_at = tranche["trigger_elapsed"]

                if tid in state["triggered"]:
                    continue

                # Trigger if we've passed the elapsed threshold (with 5s tolerance)
                if elapsed >= trigger_at and elapsed < trigger_at + 30:
                    state["triggered"].add(tid)

                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    print(f"\n  [{ts}] 📊 T{tid} — Building market brief...")
                    brief = build_brief()

                    if "chainlink_current" not in brief or "polymarket" not in brief:
                        print(f"  [{ts}] ⚠️  Incomplete data, skipping T{tid}")
                        continue

                    delta = brief.get("delta_from_strike", 0)
                    cl = brief.get("chainlink_current", 0)
                    print(f"  [{ts}] 📈 BTC=${cl:,.2f} | Δ={delta:+.2f} | {remaining:.0f}s left")

                    # Pre-filter: skip agent call if delta too small (coin flip territory)
                    if abs(delta) < 15:
                        print(f"  [{ts}] ⏭️  |Δ|={abs(delta):.0f} < $15 — no edge, skipping agent call")
                        continue

                    # Skip T2/T3 if earlier tranche in this window is losing
                    if tid > 1 and state["decisions"]:
                        prior_trades = [d for d in state["decisions"] if d.get("action", "").startswith("BUY")]
                        if prior_trades:
                            last_trade = prior_trades[-1]
                            last_side = "Up" if last_trade["action"] == "BUY_UP" else "Down"
                            delta = brief.get("delta_from_strike", 0)
                            losing = (last_side == "Up" and delta < -10) or (last_side == "Down" and delta > 10)
                            if losing:
                                ts2 = datetime.now(timezone.utc).strftime("%H:%M:%S")
                                print(f"  [{ts2}] ⛔ T{tid} SKIPPED — earlier {last_side} position underwater (Δ={delta:+.0f})")
                                continue

                    decision = trigger_agent(brief, tranche, state["decisions"], dry_run=dry_run)
                    state["decisions"].append(decision)

            # Background resolve every 15s
            if now - last_resolve > 15:
                last_resolve = now
                try:
                    trade_cmd = str(BOT_DIR / "reasoning-trader.py")
                    subprocess.run(["python3", trade_cmd, "--resolve"], capture_output=True, text=True, timeout=15)
                except Exception:
                    pass

            # Clean old window state (keep last 10)
            if len(window_state) > 10:
                for old_w in sorted(window_state.keys())[:-10]:
                    del window_state[old_w]

            # Sleep adaptively
            next_tranche_in = float("inf")
            for tranche in TRANCHES:
                if tranche["id"] not in state["triggered"]:
                    wait = tranche["trigger_elapsed"] - elapsed
                    if wait > 0:
                        next_tranche_in = min(next_tranche_in, wait)

            if next_tranche_in == float("inf"):
                time.sleep(min(10, max(1, remaining - 5)))
            elif next_tranche_in > 15:
                time.sleep(10)
            elif next_tranche_in > 3:
                time.sleep(2)
            else:
                time.sleep(1)

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
    args = parser.parse_args()
    run_loop(dry_run=args.dry_run)
