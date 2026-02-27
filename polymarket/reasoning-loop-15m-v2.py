#!/usr/bin/env python3
"""
Polymarket 15-Min BTC Reasoning Loop v2 — Continuous Monitoring with Delta Bands.

Instead of fixed T1/T2/T3 triggers, this version:
1. Polls BTC every 15s throughout the window (cheap Binance/Chainlink calls)
2. Runs a lightweight pre-filter (delta, momentum direction, HTF alignment)
3. Only calls the expensive agent when conditions enter a "tradeable band"
4. Caps at 3 agent calls per window to control costs
5. Tracks micro-trajectory (is delta growing/shrinking?) to time entries better

Delta Bands:
  - NO-GO: |Δ| < $15 or HTF strongly opposing
  - WATCH: |Δ| $15-30, signals mixed — monitor but don't call agent
  - ENTRY: |Δ| > $30, 2+ signals aligned — call agent
  - STRONG: |Δ| > $60, 3+ signals aligned — call agent with urgency flag

Entry Windows:
  - First entry allowed after 120s (2 min in) — need enough data
  - Last entry at 780s (13 min in) — need 2 min for resolution
  - Minimum 90s between agent calls (avoid rapid-fire during chop)

Usage:
    python3 reasoning-loop-15m-v2.py           # run live (paper ledger: reasoning-15m-v2.json)
    python3 reasoning-loop-15m-v2.py --dry-run # no actual trades
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
LEDGER_PATH = BOT_DIR / "ledgers" / "reasoning-15m-v2.json"
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
CHAINLINK_FEED_ID = "0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8"
CHAINLINK_API = "https://data.chain.link/api/query-timescale"

MAX_POSITION = 100.0
MIN_EDGE = 0.05

WINDOW_SECONDS = 900  # 15 minutes

# Continuous monitoring config
POLL_INTERVAL = 15          # seconds between BTC checks
FIRST_ENTRY_ELAPSED = 120   # earliest entry: 2 min in
LAST_ENTRY_ELAPSED = 780    # latest entry: 13 min in (2 min before close)
MIN_BETWEEN_AGENTS = 90     # minimum seconds between agent calls
MAX_AGENT_CALLS = 3         # max agent invocations per window

# Delta bands
DELTA_NOGO = 15       # |Δ| below this = no trade
DELTA_ENTRY = 30      # |Δ| above this + aligned signals = call agent
DELTA_STRONG = 60     # |Δ| above this = strong entry signal

# Trajectory tracking
TRAJECTORY_WINDOW = 4  # number of recent samples to track trend


def kelly_size(conviction, market_price):
    edge = conviction - market_price
    if edge <= MIN_EDGE or market_price >= 0.95:
        return 0
    kelly = edge / (1 - market_price)
    return round(min(MAX_POSITION, MAX_POSITION * kelly), 2)


def fetch_json(url, timeout=8):
    req = Request(url, headers={"User-Agent": "reasoning-loop-15m-v2/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except:
        return None


def log_pass(brief, reason, pass_type="agent"):
    try:
        ledger = json.loads(LEDGER_PATH.read_text()) if LEDGER_PATH.exists() else {}
        if "passes" not in ledger:
            ledger["passes"] = []
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pass_type": pass_type,
            "reason": reason,
            "delta": brief.get("delta_from_strike"),
            "btc": brief.get("chainlink_current"),
            "strike": brief.get("strike"),
            "htf_score": brief.get("htf_bias", {}).get("score") if isinstance(brief.get("htf_bias"), dict) else None,
        }
        ledger["passes"].append(entry)
        if len(ledger["passes"]) > 300:
            ledger["passes"] = ledger["passes"][-200:]
        LEDGER_PATH.write_text(json.dumps(ledger, indent=2))
    except:
        pass


def get_chainlink_prices():
    """Fetch latest Chainlink BTC/USD price."""
    query = {
        "query": '''
        SELECT
            report_blob_hex_timestamp AS t,
            report_blob_median_price AS p
        FROM report
        WHERE feed_id = $1
        ORDER BY t DESC
        LIMIT 3
        ''',
        "params": [CHAINLINK_FEED_ID],
    }
    try:
        req = Request(CHAINLINK_API, data=json.dumps(query).encode(),
                      headers={"Content-Type": "application/json", "User-Agent": "reasoning-loop-15m-v2/1.0"})
        with urlopen(req, timeout=8) as resp:
            rows = json.loads(resp.read())
        if rows:
            return [{"price": int(r["p"]) / 1e8, "ts": r["t"]} for r in rows]
    except:
        pass
    return []


def quick_btc_check():
    """Lightweight BTC price + delta check. No agent call."""
    cl = get_chainlink_prices()
    if not cl:
        return None
    btc = round(cl[0]["price"], 2)

    now = time.time()
    current_window = int(now) // WINDOW_SECONDS * WINDOW_SECONDS
    slug = f"btc-updown-15m-{current_window}"

    pm_data = fetch_json(f"{GAMMA_BASE}/events?slug={slug}")
    strike = None
    if pm_data:
        event = pm_data[0]
        for m in event.get("markets", []):
            if not m.get("closed"):
                try:
                    q = m.get("question", "")
                    # Extract strike from question or use opening price
                    prices = json.loads(m.get("outcomePrices", "[]"))
                    outcomes = json.loads(m.get("outcomes", "[]"))
                    tokens = json.loads(m.get("clobTokenIds", "[]"))
                    # Strike = Chainlink price at window open (first report after window start)
                    for c in reversed(cl):
                        ct = datetime.fromisoformat(c["ts"].replace("Z", "+00:00")).timestamp()
                        if ct >= current_window:
                            strike = c["price"]
                            break
                    if not strike:
                        strike = cl[-1]["price"]  # fallback to oldest
                except:
                    pass

    if not strike:
        return None

    delta = btc - strike
    return {
        "btc": btc,
        "strike": round(strike, 2),
        "delta": round(delta, 2),
        "abs_delta": round(abs(delta), 2),
        "direction": "UP" if delta > 0 else "DOWN" if delta < 0 else "FLAT",
        "slug": slug,
        "window": current_window,
    }


def quick_momentum_check():
    """Lightweight momentum from Binance 1m candles. No agent call."""
    candles = fetch_json("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=5")
    if not candles or len(candles) < 3:
        return {"direction": "unknown", "score": 0}

    closes = [float(c[4]) for c in candles]
    # Simple: are last 3 closes trending?
    trend = 0
    for i in range(1, len(closes)):
        if closes[i] > closes[i-1]:
            trend += 1
        elif closes[i] < closes[i-1]:
            trend -= 1

    score = trend / (len(closes) - 1)  # -1 to +1
    if score > 0.3:
        direction = "bullish"
    elif score < -0.3:
        direction = "bearish"
    else:
        direction = "mixed"

    return {"direction": direction, "score": round(score, 2)}


def assess_band(check, momentum, htf_score, trajectory):
    """Determine which delta band we're in and whether to call agent."""
    abs_d = check["abs_delta"]
    delta_dir = check["direction"]

    # HTF alignment: does macro trend agree with delta direction?
    htf_aligned = (
        (delta_dir == "UP" and htf_score > 0) or
        (delta_dir == "DOWN" and htf_score < 0) or
        abs(htf_score) <= 1  # neutral = no penalty
    )
    htf_opposing = (
        (delta_dir == "UP" and htf_score <= -3) or
        (delta_dir == "DOWN" and htf_score >= 3)
    )

    # Momentum alignment
    mom_aligned = (
        (delta_dir == "UP" and momentum["direction"] == "bullish") or
        (delta_dir == "DOWN" and momentum["direction"] == "bearish")
    )

    # Trajectory: is delta growing or shrinking?
    if len(trajectory) >= 2:
        delta_trend = trajectory[-1] - trajectory[0]
        delta_growing = (delta_dir == "UP" and delta_trend > 5) or (delta_dir == "DOWN" and delta_trend < -5)
        delta_shrinking = (delta_dir == "UP" and delta_trend < -5) or (delta_dir == "DOWN" and delta_trend > 5)
    else:
        delta_growing = False
        delta_shrinking = False

    # Band assessment
    if abs_d < DELTA_NOGO or htf_opposing:
        return {
            "band": "NO_GO",
            "call_agent": False,
            "reason": f"|Δ|={abs_d:.0f} < ${DELTA_NOGO}" if abs_d < DELTA_NOGO else f"HTF strongly opposing ({htf_score})",
        }

    aligned_count = sum([mom_aligned, htf_aligned, delta_growing])

    if abs_d >= DELTA_STRONG and aligned_count >= 2:
        return {
            "band": "STRONG",
            "call_agent": True,
            "reason": f"|Δ|={abs_d:.0f} ≥ ${DELTA_STRONG}, {aligned_count} signals aligned",
            "urgency": True,
        }

    if abs_d >= DELTA_ENTRY and aligned_count >= 1:
        # Don't enter if delta is shrinking fast (mean reversion)
        if delta_shrinking:
            return {
                "band": "WATCH",
                "call_agent": False,
                "reason": f"|Δ|={abs_d:.0f} entry zone but delta shrinking — waiting",
            }
        return {
            "band": "ENTRY",
            "call_agent": True,
            "reason": f"|Δ|={abs_d:.0f} ≥ ${DELTA_ENTRY}, {aligned_count} aligned, trajectory OK",
        }

    return {
        "band": "WATCH",
        "call_agent": False,
        "reason": f"|Δ|={abs_d:.0f} watch zone, {aligned_count} aligned — monitoring",
    }


def get_htf_score():
    """Calculate HTF bias score from 12h hourly candles."""
    htf_candles = fetch_json("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=12")
    if not htf_candles or len(htf_candles) < 6:
        return 0

    try:
        closes = [float(c[4]) for c in htf_candles]
        highs = [float(c[2]) for c in htf_candles]
        lows = [float(c[3]) for c in htf_candles]
        opens = [float(c[1]) for c in htf_candles]
        now_price = closes[-1]
        score = 0.0

        chg_12h = (now_price - closes[0]) / closes[0] * 100
        if chg_12h > 1: score += 1
        elif chg_12h > 0.3: score += 0.5
        elif chg_12h < -1: score -= 1
        elif chg_12h < -0.3: score -= 0.5

        if len(closes) >= 4:
            chg_4h = (now_price - closes[-4]) / closes[-4] * 100
            if chg_4h > 0.5: score += 1
            elif chg_4h > 0.15: score += 0.5
            elif chg_4h < -0.5: score -= 1
            elif chg_4h < -0.15: score -= 0.5

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

        green = sum(1 for o, c in zip(opens, closes) if c >= o)
        red = len(closes) - green
        if green >= 9: score += 1
        elif green >= 7: score += 0.5
        elif red >= 9: score -= 1
        elif red >= 7: score -= 0.5

        last3 = closes[-3:]
        if last3[2] > last3[1] > last3[0]: score += 1
        elif last3[2] > last3[1] or last3[1] > last3[0]: score += 0.5 if last3[2] > last3[0] else 0
        if last3[2] < last3[1] < last3[0]: score -= 1
        elif last3[2] < last3[1] or last3[1] < last3[0]: score -= 0.5 if last3[2] < last3[0] else 0

        h12_high = max(highs)
        h12_low = min(lows)
        if h12_high > h12_low:
            range_pos = (now_price - h12_low) / (h12_high - h12_low)
            if range_pos >= 0.8: score += 1
            elif range_pos >= 0.6: score += 0.5
            elif range_pos <= 0.2: score -= 1
            elif range_pos <= 0.4: score -= 0.5

        return round(score, 1)
    except:
        return 0


def build_brief():
    """Full brief for agent — reuses the same build_brief() from v1."""
    # Import the full brief builder from the original loop
    # For now, inline a self-contained version
    brief = {}
    now = time.time()
    current_window = int(now) // WINDOW_SECONDS * WINDOW_SECONDS
    elapsed = now - current_window
    remaining = (current_window + WINDOW_SECONDS) - now

    brief["window_start"] = current_window
    brief["elapsed_s"] = round(elapsed, 1)
    brief["remaining_s"] = round(remaining, 1)

    # Chainlink
    cl = get_chainlink_prices()
    if cl:
        brief["chainlink_current"] = round(cl[0]["price"], 2)
        # Strike = price at window open
        for c in reversed(cl):
            ct = datetime.fromisoformat(c["ts"].replace("Z", "+00:00")).timestamp()
            if ct >= current_window:
                brief["strike"] = round(c["price"], 2)
                break
        if "strike" not in brief:
            brief["strike"] = round(cl[-1]["price"], 2)
        brief["delta_from_strike"] = round(brief["chainlink_current"] - brief["strike"], 2)

    # Binance candles (1m)
    candles = fetch_json("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=15")
    if candles:
        closes = [float(c[4]) for c in candles]
        volumes = [float(c[5]) for c in candles]

        # Trend summary
        green = sum(1 for i in range(1, len(closes)) if closes[i] >= closes[i-1])
        red = len(closes) - 1 - green
        brief["candle_summary"] = {
            "green": green,
            "red": red,
            "range": round(max(closes) - min(closes), 2),
            "latest_close": closes[-1],
            "avg_volume": round(sum(volumes) / len(volumes), 4),
        }

        # EMA
        def ema_calc(data, period):
            k = 2 / (period + 1)
            val = data[0]
            for d in data[1:]:
                val = d * k + val * (1 - k)
            return val
        if len(closes) >= 12:
            ema8 = ema_calc(closes, 8)
            ema21 = ema_calc(closes, 21) if len(closes) >= 21 else ema_calc(closes, len(closes))
            brief["ema"] = {
                "ema8": round(ema8, 2),
                "ema21": round(ema21, 2),
                "signal": "bullish" if ema8 > ema21 else "bearish",
            }

        # VWAP
        vwap_num = sum(float(c[4]) * float(c[5]) for c in candles)
        vwap_den = sum(float(c[5]) for c in candles)
        if vwap_den > 0:
            brief["vwap"] = round(vwap_num / vwap_den, 2)
            brief["price_vs_vwap"] = "above" if closes[-1] > brief["vwap"] else "below"

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

    # Orderbook imbalance score
    if book and book.get("bids") and book.get("asks"):
        bid_pressure = 0
        ask_pressure = 0
        mid = (float(book["bids"][0][0]) + float(book["asks"][0][0])) / 2
        for i, b in enumerate(book["bids"][:10]):
            dist = abs(float(b[0]) - mid) / mid
            bid_pressure += float(b[1]) * (1 - dist * 100)
        for i, a in enumerate(book["asks"][:10]):
            dist = abs(float(a[0]) - mid) / mid
            ask_pressure += float(a[1]) * (1 - dist * 100)
        total = bid_pressure + ask_pressure
        if total > 0:
            imbalance = (bid_pressure - ask_pressure) / total
            brief["orderbook_imbalance"] = {
                "score": round(imbalance, 3),
                "label": "strong_buy" if imbalance > 0.3 else "strong_sell" if imbalance < -0.3 else "buy" if imbalance > 0.1 else "sell" if imbalance < -0.1 else "balanced",
            }

    # CVD (Cumulative Volume Delta) from recent trades
    trades = fetch_json("https://api.binance.com/api/v3/trades?symbol=BTCUSDT&limit=100")
    if trades:
        buy_vol = sum(float(t["qty"]) for t in trades if not t["isBuyerMaker"])
        sell_vol = sum(float(t["qty"]) for t in trades if t["isBuyerMaker"])
        cvd = buy_vol - sell_vol
        total_vol = buy_vol + sell_vol
        large_trades = [t for t in trades if float(t["qty"]) * float(t["price"]) > 5000]
        large_buys = sum(1 for t in large_trades if not t["isBuyerMaker"])
        large_sells = sum(1 for t in large_trades if t["isBuyerMaker"])
        brief["cvd"] = {
            "net": round(cvd, 3),
            "buy_pct": round(buy_vol / total_vol * 100, 1) if total_vol > 0 else 50,
            "signal": "bullish" if cvd > 0.5 else "bearish" if cvd < -0.5 else "neutral",
            "large_buys": large_buys,
            "large_sells": large_sells,
        }

    # Momentum alignment
    alignment_signals = []
    if brief.get("delta_from_strike"):
        alignment_signals.append(1 if brief["delta_from_strike"] > 0 else -1)
    if brief.get("ema", {}).get("signal"):
        alignment_signals.append(1 if brief["ema"]["signal"] == "bullish" else -1)
    if brief.get("orderbook_imbalance", {}).get("score"):
        alignment_signals.append(1 if brief["orderbook_imbalance"]["score"] > 0 else -1)
    if brief.get("cvd", {}).get("signal"):
        alignment_signals.append(1 if brief["cvd"]["signal"] == "bullish" else -1 if brief["cvd"]["signal"] == "bearish" else 0)
    if brief.get("price_vs_vwap"):
        alignment_signals.append(1 if brief["price_vs_vwap"] == "above" else -1)

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
    slug = f"btc-updown-15m-{current_window}"
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
                        "slug": slug,
                    }

                    # CLOB midpoint + sorted orderbook
                    for side_name, token_idx in [("up", up_idx), ("down", down_idx)]:
                        if tokens:
                            mid = fetch_json(f"{CLOB_BASE}/midpoint?token_id={tokens[token_idx]}")
                            if mid and mid.get("mid"):
                                brief["polymarket"][f"{side_name}_mid"] = round(float(mid["mid"]), 4)
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

    # HTF bias
    brief["htf_bias"] = {"score": get_htf_score()}
    if brief["htf_bias"]["score"] >= 4: brief["htf_bias"]["label"] = "strong_bullish"
    elif brief["htf_bias"]["score"] >= 2: brief["htf_bias"]["label"] = "bullish"
    elif brief["htf_bias"]["score"] <= -4: brief["htf_bias"]["label"] = "strong_bearish"
    elif brief["htf_bias"]["score"] <= -2: brief["htf_bias"]["label"] = "bearish"
    else: brief["htf_bias"]["label"] = "neutral"

    # Funding rate
    funding = fetch_json("https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT")
    if funding:
        brief["funding_rate"] = round(float(funding.get("lastFundingRate", 0)) * 100, 4)

    # Futures
    futures_price = fetch_json("https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT")
    if futures_price and brief.get("chainlink_current"):
        fp = float(futures_price.get("price", 0))
        spot = brief["chainlink_current"]
        basis = fp - spot
        brief["futures"] = {
            "price": round(fp, 2),
            "basis": round(basis, 2),
            "basis_pct": round(basis / spot * 100, 4) if spot else 0,
        }

    return brief


def trigger_agent(brief, entry_num, prior_decisions, dry_run=False):
    """Call the reasoning agent for a trade decision."""
    trade_cmd = f"python3 {BOT_DIR / 'reasoning-trader-15m.py'}"
    base_size = MAX_POSITION
    band = brief.get("_band", "ENTRY")
    urgency = brief.get("_urgency", False)

    compact = {k: v for k, v in brief.items() if not k.startswith("_")}

    # Save brief to file
    brief_file = BOT_DIR / "briefs" / f"{brief.get('window_start', 0)}_E{entry_num}.json"
    brief_file.parent.mkdir(parents=True, exist_ok=True)
    brief_file.write_text(json.dumps(compact, indent=2))

    prior_summary = ""
    if prior_decisions:
        for d in prior_decisions:
            if d.get("action", "").startswith("BUY"):
                side = "UP" if d["action"] == "BUY_UP" else "DOWN"
                prior_summary += f"\n- Prior entry #{d.get('entry_num',0)}: {side}, conviction {d.get('conviction',0)}%"

    message = f"""Market brief for BTC 15-min window:
```json
{json.dumps(compact, indent=2)}
```

Band: {band} {'⚡ URGENT' if urgency else ''}
Entry #{entry_num} of max {MAX_AGENT_CALLS}. {brief.get('remaining_s', '?')}s remaining in window.
{prior_summary}

RULES:
- "Up" wins if Chainlink BTC/USD at window end >= strike. "Down" if < strike.
- Shares pay $1 if correct, $0 if wrong.
- Entry cost ≈ midpoint (up_mid / down_mid). Check best_ask for actual fill price. Remaining: ~{brief.get('remaining_s', '?')}s.
- Position size scales with your confidence (see below).

SIGNAL HIERARCHY (in priority order):
1. **delta_from_strike** — The single most important signal. Positive = price above strike = UP. Negative = DOWN.
2. **momentum_alignment** — score (-1 to +1) and strength. When "strong" + aligned with delta direction = high conviction trade. When it contradicts delta, PASS.
3. **price_trajectory** — is the delta growing or shrinking? Growing delta = stronger conviction. Shrinking = possible reversal.
4. **Orderbook imbalance** — score (-1 to +1). Should confirm delta direction.
5. **CVD + Taker flow** — net buying/selling pressure. Use to confirm, not contradict, the delta.
6. **Technical indicators** — RSI, EMA, VWAP, Bollinger Bands. Tiebreakers only.
7. **htf_bias** — Higher timeframe trend (12h hourly candles), score -6 to +6.
   - strong_bearish (≤-4): HEAVILY penalize UP trades. Reduce UP conviction by 20-30%.
   - bearish (-2 to -3): Penalize UP trades, reduce conviction by 10-15%.
   - neutral (-1 to +1): No adjustment.
   - bullish (+2 to +3): Penalize DOWN trades, reduce conviction by 10-15%.
   - strong_bullish (≥+4): HEAVILY penalize DOWN trades. Reduce DOWN conviction by 20-30%.
8. **Futures signals** — OI, basis, long/short ratio. Background context only.
9. **Polymarket pricing** — is the ask price fair? Edge = true_prob - ask_price.
10. **Risk/reward** — don't buy > 0.75 unless nearly certain.

MANDATORY PASS CONDITIONS (if ANY of these are true, you MUST pass):
- 3+ signals contradict the delta direction (even if delta is large)
- momentum_alignment direction opposes delta AND strength is "strong"
- CVD sell_pct > 70% when delta says UP (or buy_pct > 70% when delta says DOWN)

POSITION SIZING — Kelly Criterion:
  You output your CONVICTION (0-100%) = your estimated probability that your side wins.
  The system computes: edge = conviction - market_price
  If edge < 5%: trade is rejected (no edge).
  Size = MAX_POSITION × (conviction - market_price) / (1 - market_price), capped at ${MAX_POSITION}.

IF TRADING, respond with EXACTLY this format on the FIRST line, then run the command:
TRADE [UP/DOWN] [CONVICTION 0-100] [PRICE]

Then execute:
```
{trade_cmd} --trade "SIDE" "PRICE" "E{entry_num}/conv[XX]: reasoning" --size SIZE --confidence XX --delta {compact.get('delta_from_strike', 0)} --strike {compact.get('strike', 0)} --momentum {compact.get('momentum_alignment', {}).get('score', 0) if isinstance(compact.get('momentum_alignment'), dict) else 0} --brief-file "{brief_file}"
```

IF PASSING: respond with EXACTLY: PASS [CONVICTION 0] — reason

First resolve open positions:
```
{trade_cmd} --resolve
```

This is paper trading — we WANT data. Trade when you see edge (>5%). Don't wait for certainty.
Be fast."""

    cmd = ["openclaw", "agent", "--agent", "polymarket-trader", "--session-id", "trading-15m-v2", "-m", message]

    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"  [{ts}] 🧠 E{entry_num} — triggering agent (base ${base_size:.0f}, {brief.get('remaining_s', '?')}s left, band={band})...")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        output = result.stdout.strip() if result.stdout else ""
        decision = {"entry_num": entry_num, "action": "UNKNOWN", "reasoning": ""}

        if output:
            lines = output.strip().split('\n')
            for line in lines[-10:]:
                if line.strip():
                    print(f"  [{ts}]    {line.strip()[:100]}")

            full = output.upper()
            conv_match = re.search(r'CONVICTION\s+(\d+)', full)
            if conv_match:
                decision["conviction"] = int(conv_match.group(1))

            if "PASS" in full and "CONVICTION" in full:
                decision["action"] = "PASS"
                decision["reasoning"] = output.strip()[-200:]
            elif '"UP"' in full or "'UP'" in full:
                decision["action"] = "BUY_UP"
            elif '"DOWN"' in full or "'DOWN'" in full:
                decision["action"] = "BUY_DOWN"

            if decision["action"] in ("UNKNOWN",):
                if "TRADE UP" in full:
                    decision["action"] = "BUY_UP"
                elif "TRADE DOWN" in full:
                    decision["action"] = "BUY_DOWN"
                decision["reasoning"] = output.strip()[-100:]

            conv = decision.get("conviction", 50) / 100.0
            pm = brief.get("polymarket", {})
            if decision.get("action") == "BUY_UP":
                entry_price = pm.get("up_mid", pm.get("up_best_ask", pm.get("up_price", 0.5)))
            else:
                entry_price = pm.get("down_mid", pm.get("down_best_ask", pm.get("down_price", 0.5)))
            sized = kelly_size(conv, entry_price)
            if decision["action"] not in ("PASS", "UNKNOWN"):
                edge = conv - entry_price
                print(f"  [{ts}]    📊 Conviction: {decision.get('conviction',0)}% | Edge: {edge*100:.1f}% | Kelly size: ${sized:.2f} (max ${MAX_POSITION:.0f})")

        if result.returncode != 0 and result.stderr:
            print(f"  [{ts}] ⚠️  Agent error: {result.stderr[:200]}")

        ts2 = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"  [{ts2}]    completed")
        return decision

    except subprocess.TimeoutExpired:
        ts2 = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"  [{ts2}] ⚠️  E{entry_num} agent timed out")
        return {"entry_num": entry_num, "action": "TIMEOUT", "reasoning": "Agent timed out"}
    except Exception as e:
        ts2 = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"  [{ts2}] ⚠️  E{entry_num} error: {e}")
        return {"entry_num": entry_num, "action": "ERROR", "reasoning": str(e)}


# ---- Main Loop ----

def run_loop(dry_run=False):
    print(f"{'='*65}")
    print(f"🧠 Polymarket 15-Min BTC v2 — Continuous Monitoring")
    print(f"   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"   Mode: {'DRY RUN' if dry_run else 'PAPER TRADING'}")
    print(f"   Bands: NO_GO <${DELTA_NOGO} | ENTRY ≥${DELTA_ENTRY} | STRONG ≥${DELTA_STRONG}")
    print(f"   Window: {FIRST_ENTRY_ELAPSED}s - {LAST_ENTRY_ELAPSED}s | Max {MAX_AGENT_CALLS} calls | {MIN_BETWEEN_AGENTS}s cooldown")
    print(f"   Sizing: Kelly Criterion (conviction 0-100%, min edge {MIN_EDGE*100:.0f}%)")
    print(f"{'='*65}\n")

    window_state = {}
    last_status = 0
    last_resolve = 0
    htf_cache = {"score": 0, "ts": 0}

    while True:
        try:
            now = time.time()
            current_window = int(now) // WINDOW_SECONDS * WINDOW_SECONDS
            elapsed = now - current_window
            remaining = (current_window + WINDOW_SECONDS) - now

            # Init window state
            if current_window not in window_state:
                window_state[current_window] = {
                    "agent_calls": 0,
                    "last_agent_call": 0,
                    "decisions": [],
                    "trajectory": [],      # list of delta values
                    "last_band": "—",
                    "last_check": 0,
                }

            state = window_state[current_window]

            # Refresh HTF score every 5 minutes
            if now - htf_cache["ts"] > 300:
                htf_cache["score"] = get_htf_score()
                htf_cache["ts"] = now

            # Status log every 30s
            if now - last_status > 30:
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                window_str = datetime.fromtimestamp(current_window, tz=timezone.utc).strftime("%H:%M")
                ledger = json.loads(LEDGER_PATH.read_text()) if LEDGER_PATH.exists() else {"stats": {"total_pnl": 0, "wins": 0, "losses": 0}}
                s = ledger.get("stats", {})
                traj_str = f"Δ={state['trajectory'][-1]:+.0f}" if state['trajectory'] else "Δ=?"
                print(f"  [{ts}] Window {window_str} | {elapsed:.0f}s in, {remaining:.0f}s left | "
                      f"band={state['last_band']} | calls={state['agent_calls']}/{MAX_AGENT_CALLS} | {traj_str} | "
                      f"HTF={htf_cache['score']:+.1f} | PnL=${s.get('total_pnl', 0):+.2f} {s.get('wins', 0)}W/{s.get('losses', 0)}L")
                last_status = now

            # Continuous monitoring: check every POLL_INTERVAL seconds
            if now - state["last_check"] >= POLL_INTERVAL:
                state["last_check"] = now

                # Quick BTC check (lightweight)
                check = quick_btc_check()
                if check and check["window"] == current_window:
                    # Track trajectory
                    state["trajectory"].append(check["delta"])
                    if len(state["trajectory"]) > 60:  # cap history
                        state["trajectory"] = state["trajectory"][-40:]

                    # Only evaluate entry if within entry window
                    if FIRST_ENTRY_ELAPSED <= elapsed <= LAST_ENTRY_ELAPSED:
                        # Check if we can still call agent
                        if state["agent_calls"] < MAX_AGENT_CALLS:
                            # Check cooldown
                            if now - state["last_agent_call"] >= MIN_BETWEEN_AGENTS:
                                # Quick momentum
                                momentum = quick_momentum_check()

                                # Get recent trajectory slice
                                traj_slice = state["trajectory"][-TRAJECTORY_WINDOW:]

                                # Assess band
                                assessment = assess_band(check, momentum, htf_cache["score"], traj_slice)
                                state["last_band"] = assessment["band"]

                                if assessment["call_agent"]:
                                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                                    entry_num = state["agent_calls"] + 1
                                    print(f"\n  [{ts}] 🎯 {assessment['band']} — {assessment['reason']}")
                                    print(f"  [{ts}] 📊 Building full brief for E{entry_num}...")

                                    # Build full brief
                                    brief = build_brief()
                                    brief["_band"] = assessment["band"]
                                    brief["_urgency"] = assessment.get("urgency", False)
                                    brief["_trajectory"] = traj_slice

                                    if "chainlink_current" not in brief or "polymarket" not in brief:
                                        print(f"  [{ts}] ⚠️  Incomplete data, skipping")
                                        continue

                                    delta = brief.get("delta_from_strike", 0)
                                    cl = brief.get("chainlink_current", 0)
                                    print(f"  [{ts}] 📈 BTC=${cl:,.2f} | Δ={delta:+.2f} | {remaining:.0f}s left")

                                    # Call agent
                                    decision = trigger_agent(brief, entry_num, state["decisions"], dry_run=dry_run)
                                    state["agent_calls"] += 1
                                    state["last_agent_call"] = now
                                    state["decisions"].append(decision)

                                    if decision.get("action") == "PASS":
                                        log_pass(brief, decision.get("reasoning", "agent PASS"), "agent")
                                else:
                                    # Log band status occasionally (every 2 minutes)
                                    if len(state["trajectory"]) % 8 == 0:
                                        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                                        print(f"  [{ts}] 📡 {assessment['band']}: {assessment['reason']}")

            # Background resolve every 15s
            if now - last_resolve > 15:
                last_resolve = now
                try:
                    trade_cmd = str(BOT_DIR / "reasoning-trader-15m.py")
                    subprocess.run(["python3", trade_cmd, "--resolve"], capture_output=True, text=True, timeout=15)
                except Exception:
                    pass

            # Clean old window state (keep last 5)
            if len(window_state) > 5:
                for old_w in sorted(window_state.keys())[:-5]:
                    del window_state[old_w]

            # Sleep
            time.sleep(min(POLL_INTERVAL, max(1, remaining - 2)))

        except KeyboardInterrupt:
            print("\n  Shutting down...")
            break
        except Exception as e:
            import traceback
            print(f"  ⚠️  Error: {e}")
            traceback.print_exc()
            time.sleep(10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket 15-Min v2 — Continuous Monitoring")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run_loop(dry_run=args.dry_run)
