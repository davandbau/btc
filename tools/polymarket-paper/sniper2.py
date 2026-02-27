#!/usr/bin/env python3
"""
Polymarket 5-Minute BTC Sniper v2 — Momentum/Orderflow Strategy

Instead of fair-value modeling (which failed), this reads directional
signals from Binance orderflow and bets WITH the momentum early in
each 5-minute window while Polymarket is still near 50/50.

Signals:
  1. Order book imbalance (bid/ask volume ratio)
  2. Trade flow aggression (buy vs sell taker volume)
  3. Candle momentum (last 3 one-minute candles)
  4. Price vs strike (Chainlink delta)

Entry: First 90 seconds of each window when market is still 40-60%
Exit: Hold to expiry (binary outcome)

Usage:
    python3 sniper2.py                  # paper trade
    python3 sniper2.py --dry-run        # signals only
    python3 sniper2.py --stats          # show results
"""

import argparse
import asyncio
import json
import math
import os
import signal
import sys
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

# ─── Constants ────────────────────────────────────────────────────────
BOT_DIR = Path(__file__).parent
LEDGER_PATH = BOT_DIR / "ledgers" / "sniper.json"

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# Chainlink
CHAINLINK_FEED_ID = "0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8"
CHAINLINK_API = "https://data.chain.link/api/query-timescale"
CHAINLINK_DECIMALS = 18

# Fees
FEE_RATE = 0.25
FEE_EXPONENT = 2


def calc_fee(shares, price):
    if price <= 0 or price >= 1:
        return 0
    return shares * FEE_RATE * (price * (1 - price)) ** FEE_EXPONENT


def fetch_json(url, timeout=8):
    req = Request(url, headers={"User-Agent": "polymarket-sniper2/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except:
        return None


# ─── Chainlink ────────────────────────────────────────────────────────
def fetch_chainlink_latest():
    """Get latest Chainlink BTC/USD price."""
    url = (f"{CHAINLINK_API}?query=LIVE_STREAM_REPORTS_QUERY"
           f"&variables=%7B%22feedId%22%3A%22{CHAINLINK_FEED_ID}%22%7D")
    data = fetch_json(url)
    if not data:
        return None, None
    nodes = data.get("data", {}).get("liveStreamReports", {}).get("nodes", [])
    if not nodes:
        return None, None
    node = nodes[0]
    try:
        price = float(node["price"]) / (10 ** CHAINLINK_DECIMALS)
        dt = datetime.fromisoformat(node["validFromTimestamp"].replace("+00:00", "+00:00"))
        return price, dt.timestamp()
    except:
        return None, None


# ─── Binance Signals ─────────────────────────────────────────────────
class BinanceSignals:
    """Reads directional signals from Binance orderflow."""

    def __init__(self):
        self.price = 0.0
        self.last_update = 0.0

    def read_all(self):
        """Read all signals. Returns a dict with scores and raw data."""
        signals = {
            "book_imbalance": self._order_book_imbalance(),
            "trade_flow": self._trade_flow(),
            "candle_momentum": self._candle_momentum(),
            "composite": 0.0,  # filled below
            "direction": "none",
            "confidence": 0.0,
            "price": self.price,
        }

        # Composite score: -1 (strong bearish) to +1 (strong bullish)
        scores = []
        weights = []

        # Order book: weight 0.25
        if signals["book_imbalance"]["score"] is not None:
            scores.append(signals["book_imbalance"]["score"])
            weights.append(0.25)

        # Trade flow: weight 0.40 (most predictive)
        if signals["trade_flow"]["score"] is not None:
            scores.append(signals["trade_flow"]["score"])
            weights.append(0.40)

        # Candle momentum: weight 0.35
        if signals["candle_momentum"]["score"] is not None:
            scores.append(signals["candle_momentum"]["score"])
            weights.append(0.35)

        if not scores:
            return signals

        total_weight = sum(weights)
        composite = sum(s * w for s, w in zip(scores, weights)) / total_weight
        signals["composite"] = composite
        signals["confidence"] = abs(composite)

        if composite > 0.15:
            signals["direction"] = "up"
        elif composite < -0.15:
            signals["direction"] = "down"
        else:
            signals["direction"] = "neutral"

        return signals

    def _order_book_imbalance(self):
        """Bid/ask volume imbalance from top 20 levels."""
        book = fetch_json("https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=20")
        if not book:
            return {"score": None, "ratio": None}

        bids_vol = sum(float(b[1]) for b in book["bids"])
        asks_vol = sum(float(a[1]) for a in book["asks"])

        if asks_vol == 0 and bids_vol == 0:
            return {"score": 0, "ratio": 1.0, "bids": 0, "asks": 0}

        ratio = bids_vol / asks_vol if asks_vol > 0 else 10.0
        # Score: log ratio, capped at ±1
        score = max(-1.0, min(1.0, math.log(max(ratio, 0.01)) / 2.0))

        return {"score": score, "ratio": round(ratio, 3),
                "bids": round(bids_vol, 3), "asks": round(asks_vol, 3)}

    def _trade_flow(self):
        """Buy vs sell aggressor volume from recent trades."""
        trades = fetch_json("https://api.binance.com/api/v3/aggTrades?symbol=BTCUSDT&limit=200")
        if not trades:
            return {"score": None}

        # Update price from latest trade
        if trades:
            self.price = float(trades[-1]["p"])
            self.last_update = float(trades[-1]["T"]) / 1000

        buy_vol = sum(float(t["q"]) for t in trades if not t["m"])
        sell_vol = sum(float(t["q"]) for t in trades if t["m"])
        total = buy_vol + sell_vol

        if total == 0:
            return {"score": 0, "buy_pct": 50, "sell_pct": 50}

        buy_pct = buy_vol / total * 100
        # Score: (buy% - 50) / 50, so 75% buy = +0.5, 25% buy = -0.5
        score = max(-1.0, min(1.0, (buy_pct - 50) / 40))

        return {"score": score, "buy_pct": round(buy_pct, 1),
                "sell_pct": round(100 - buy_pct, 1),
                "buy_vol": round(buy_vol, 3), "sell_vol": round(sell_vol, 3)}

    def _candle_momentum(self):
        """Direction and strength from last 3 one-minute candles."""
        klines = fetch_json("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=4")
        if not klines or len(klines) < 3:
            return {"score": None}

        # Use last 3 completed candles (skip the current incomplete one)
        candles = klines[-4:-1] if len(klines) >= 4 else klines[-3:]

        total_move = 0
        vol_weighted_move = 0
        total_vol = 0

        for k in candles:
            o, c, vol = float(k[1]), float(k[4]), float(k[5])
            move = (c - o) / o * 100  # percent move
            total_move += move
            vol_weighted_move += move * vol
            total_vol += vol

        avg_move = vol_weighted_move / total_vol if total_vol > 0 else total_move / 3
        # Score: scale so 0.1% avg move = ±0.5
        score = max(-1.0, min(1.0, avg_move / 0.2))

        green = sum(1 for k in candles if float(k[4]) >= float(k[1]))
        red = len(candles) - green

        return {"score": score, "avg_move_pct": round(avg_move, 4),
                "total_move_pct": round(total_move, 4),
                "green": green, "red": red}


# ─── Market Tracker ──────────────────────────────────────────────────
class MarketTracker:
    """Discovers and tracks active BTC 5-min markets."""

    def __init__(self):
        self.markets = []
        self.known_slugs = {}
        self.orderbooks = {}
        self.last_refresh = 0
        self.last_book_update = 0

    def refresh(self):
        now = time.time()
        if now - self.last_refresh < 15:
            return

        current_window = int(now) // 300 * 300
        utc_now = datetime.now(timezone.utc)
        self.markets = []

        for offset in range(-1, 6):
            ts = current_window + (offset * 300)
            slug = f"btc-updown-5m-{ts}"

            if slug in self.known_slugs:
                cached = self.known_slugs[slug]
                remaining = (cached["end_dt"] - utc_now).total_seconds()
                if remaining <= 0:
                    del self.known_slugs[slug]
                    continue
                cached["remaining_s"] = remaining
                self.markets.append(cached)
                continue

            data = fetch_json(f"{GAMMA_BASE}/events?slug={slug}")
            if not data:
                continue
            event = data[0]
            if event.get("closed"):
                continue

            for m in event.get("markets", []):
                if m.get("closed") or not m.get("active"):
                    continue
                end_str = m.get("endDate", "")
                if not end_str:
                    continue
                try:
                    end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                except:
                    continue
                remaining = (end - utc_now).total_seconds()
                if remaining <= 0:
                    continue
                try:
                    tokens = json.loads(m.get("clobTokenIds", "[]"))
                    outcomes = json.loads(m.get("outcomes", "[]"))
                    prices = json.loads(m.get("outcomePrices", "[]"))
                except:
                    continue
                if len(tokens) < 2 or len(outcomes) < 2:
                    continue

                up_idx = 0 if "Up" in outcomes[0] else 1
                down_idx = 1 - up_idx
                market_data = {
                    "question": m.get("question", event.get("title", "")),
                    "slug": slug,
                    "end_date": end_str,
                    "end_dt": end,
                    "remaining_s": remaining,
                    "window_start_ts": ts,
                    "window_end_ts": ts + 300,
                    "tokens": tokens,
                    "outcomes": outcomes,
                    "up_idx": up_idx,
                    "down_idx": down_idx,
                    "up_token": tokens[up_idx],
                    "down_token": tokens[down_idx],
                    "price_up": float(prices[up_idx]) if prices else 0.5,
                    "price_down": float(prices[down_idx]) if len(prices) > 1 else 0.5,
                    "condition_id": m.get("conditionId", ""),
                }
                self.known_slugs[slug] = market_data
                self.markets.append(market_data)
                break

        self.markets.sort(key=lambda x: x["remaining_s"])
        self.last_refresh = now

    def update_orderbooks(self):
        now = time.time()
        if now - self.last_book_update < 2:
            return
        self.last_book_update = now

        for market in self.markets:
            if market["remaining_s"] > 600:
                continue
            for token in [market["up_token"], market["down_token"]]:
                book = fetch_json(f"{CLOB_BASE}/book?token_id={token}")
                if not book:
                    continue
                bids = sorted([{"p": float(b["price"]), "s": float(b["size"])}
                               for b in book.get("bids", [])], key=lambda x: -x["p"])
                asks = sorted([{"p": float(a["price"]), "s": float(a["size"])}
                               for a in book.get("asks", [])], key=lambda x: x["p"])
                self.orderbooks[token] = {
                    "bids": bids, "asks": asks,
                    "best_bid": bids[0]["p"] if bids else 0,
                    "best_ask": asks[0]["p"] if asks else 1,
                }

    def get_ask(self, token):
        return self.orderbooks.get(token, {}).get("best_ask", None)

    def get_bid(self, token):
        return self.orderbooks.get(token, {}).get("best_bid", None)


# ─── Ledger ───────────────────────────────────────────────────────────
def load_ledger():
    LEDGER_PATH.parent.mkdir(exist_ok=True)
    if LEDGER_PATH.exists():
        return json.loads(LEDGER_PATH.read_text())
    return {
        "strategy": "sniper",
        "trades": [],
        "open_positions": [],
        "stats": {
            "total_pnl": 0, "wins": 0, "losses": 0, "total_trades": 0,
            "gross_profit": 0, "gross_loss": 0, "total_fees": 0, "total_wagered": 0,
        },
    }


def save_ledger(ledger):
    LEDGER_PATH.parent.mkdir(exist_ok=True)
    LEDGER_PATH.write_text(json.dumps(ledger, indent=2))


# ─── Engine ───────────────────────────────────────────────────────────
class SniperEngine:

    def __init__(self, dry_run=False, position_size=25.0, min_confidence=0.25,
                 entry_window=180, max_price=0.60, tranches=4):
        self.dry_run = dry_run
        self.position_size = position_size
        self.min_confidence = min_confidence
        self.entry_window = entry_window       # full window for all tranches
        self.max_price = max_price
        self.tranches = tranches               # number of entry tranches
        self.tranche_size = position_size / tranches
        # Track per-window state: slug -> {side, entries, last_entry_time}
        self.window_state = {}
        self.ledger = load_ledger()
        self.strike_cache = {}
        self.trade_count = 0
        self.signal_count = 0
        self.last_log = 0

    def evaluate(self, signals, markets, chainlink_price):
        """Check for trading opportunity based on orderflow signals.
        
        Tranche logic:
        - Tranche 1 (0-30s): Enter if signal confidence >= min_confidence
        - Tranche 2 (30-60s): Add if signal STILL agrees with tranche 1
        - Tranche 3 (60-120s): Add if signal agrees AND price improved (cheaper)
        - Tranche 4 (120-180s): Add only if strong confirmation AND good price
        
        If signal FLIPS against our position, stop adding (but don't exit).
        """
        now = time.time()

        for market in markets.markets:
            elapsed = now - market["window_start_ts"]
            remaining = market["window_end_ts"] - now

            if elapsed < 5 or remaining < 30:
                continue

            slug = market["slug"]
            state = self.window_state.get(slug)

            if state is None:
                # === TRANCHE 1: Initial entry ===
                if elapsed > 45:  # only open new positions in first 45s
                    continue
                if signals["direction"] == "neutral":
                    continue
                if signals["confidence"] < self.min_confidence:
                    continue

                side = "Up" if signals["direction"] == "up" else "Down"
                token = market["up_token"] if side == "Up" else market["down_token"]
                entry_price = markets.get_ask(token)
                if entry_price is None or entry_price >= self.max_price:
                    continue

                strike = self.strike_cache.get(market["window_start_ts"])
                delta = chainlink_price - strike if strike else 0

                self.signal_count += 1
                trade = self._execute_trade(
                    market, side, entry_price, signals, strike,
                    chainlink_price, delta, remaining, now, tranche=1
                )
                if trade:
                    self.window_state[slug] = {
                        "side": side,
                        "entries": 1,
                        "last_entry_time": now,
                        "best_price": entry_price,
                        "initial_confidence": signals["confidence"],
                    }
                return trade

            else:
                # === TRANCHES 2-4: Add to position ===
                if state["entries"] >= self.tranches:
                    continue
                # Min 30s between tranches
                if now - state["last_entry_time"] < 30:
                    continue

                side = state["side"]
                token = market["up_token"] if side == "Up" else market["down_token"]
                entry_price = markets.get_ask(token)
                if entry_price is None or entry_price >= self.max_price:
                    continue

                tranche_num = state["entries"] + 1
                signal_agrees = (
                    (side == "Up" and signals["direction"] == "up") or
                    (side == "Down" and signals["direction"] == "down")
                )

                # Tranche 2: signal must still agree
                if tranche_num == 2:
                    if not signal_agrees:
                        continue
                    if signals["confidence"] < self.min_confidence * 0.8:
                        continue

                # Tranche 3: signal agrees AND price improved (cheaper entry)
                elif tranche_num == 3:
                    if not signal_agrees:
                        continue
                    if entry_price >= state["best_price"]:
                        continue  # only add if we're getting a better price

                # Tranche 4: strong confirmation + good price
                elif tranche_num == 4:
                    if not signal_agrees:
                        continue
                    if signals["confidence"] < self.min_confidence * 1.2:
                        continue
                    if entry_price >= state["best_price"] * 0.95:
                        continue  # need meaningfully better price

                strike = self.strike_cache.get(market["window_start_ts"])
                delta = chainlink_price - strike if strike else 0

                self.signal_count += 1
                trade = self._execute_trade(
                    market, side, entry_price, signals, strike,
                    chainlink_price, delta, remaining, now, tranche=tranche_num
                )
                if trade:
                    state["entries"] = tranche_num
                    state["last_entry_time"] = now
                    state["best_price"] = min(state["best_price"], entry_price)
                return trade

        return None

    def _execute_trade(self, market, side, entry_price, signals, strike,
                       btc_price, delta, remaining, now, tranche=1):
        cost = self.tranche_size
        shares = cost / entry_price
        fee = calc_fee(shares, entry_price)

        ts_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
        arrow = "↑" if side == "Up" else "↓"
        book = signals["book_imbalance"]
        flow = signals["trade_flow"]
        candle = signals["candle_momentum"]

        print(f"\n  [{ts_str}] {arrow} T{tranche}/{self.tranches} {side} @ {entry_price:.3f} "
              f"(conf={signals['confidence']:.1%}, ${cost:.0f})")
        print(f"           Composite={signals['composite']:+.3f} | "
              f"Book={book.get('score',0):+.2f} (ratio={book.get('ratio','?')}) | "
              f"Flow={flow.get('score',0):+.2f} (buy={flow.get('buy_pct','?')}%) | "
              f"Candles={candle.get('score',0):+.2f} ({candle.get('green',0)}g/{candle.get('red',0)}r)")
        if strike:
            print(f"           BTC=${btc_price:,.1f} strike=${strike:,.1f} "
                  f"Δ={delta:+,.1f} | {remaining:.0f}s left")
        print(f"           {market['question'][:55]}")

        if self.dry_run:
            return None

        trade = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "market": market["question"],
            "slug": market["slug"],
            "market_end": market["end_date"],
            "window_start_ts": market["window_start_ts"],
            "side": side,
            "token": market["up_token"] if side == "Up" else market["down_token"],
            "entry_price": entry_price,
            "shares": round(shares, 4),
            "cost": cost,
            "fee": round(fee, 6),
            "fair_value": None,
            "edge": round(signals["confidence"], 4),
            "net_ev": None,
            "btc_price": btc_price,
            "strike_price": strike,
            "btc_delta": round(delta, 2) if strike else 0,
            "time_remaining": round(remaining, 1),
            "volatility": 0,
            "tranche": tranche,
            "composite_signal": round(signals["composite"], 4),
            "book_score": round(signals["book_imbalance"].get("score", 0), 4),
            "flow_score": round(signals["trade_flow"].get("score", 0), 4),
            "candle_score": round(signals["candle_momentum"].get("score", 0), 4),
            "resolved": False,
            "outcome": None,
            "pnl": None,
        }

        self.ledger["open_positions"].append(trade)
        self.ledger["stats"]["total_wagered"] += cost
        save_ledger(self.ledger)
        self.trade_count += 1

        print(f"           🎲 T{tranche}: {shares:.1f} shares @ ${entry_price:.3f} (${cost:.0f})")
        return trade

    def resolve_positions(self):
        now = datetime.now(timezone.utc)
        resolved_count = 0

        for trade in self.ledger["open_positions"]:
            if trade.get("resolved"):
                continue
            end = datetime.fromisoformat(trade["market_end"].replace("Z", "+00:00"))
            if now < end + timedelta(seconds=30):
                continue

            data = fetch_json(f"{GAMMA_BASE}/events?slug={trade['slug']}")
            if not data:
                continue
            event = data[0]
            market = None
            for m in event.get("markets", []):
                if m.get("closed"):
                    market = m
                    break
            if not market:
                if not event.get("closed"):
                    continue
                continue

            try:
                prices = json.loads(market.get("outcomePrices", "[]"))
                outcomes = json.loads(market.get("outcomes", "[]"))
            except:
                continue
            if not prices:
                continue

            up_idx = 0 if "Up" in outcomes[0] else 1
            resolved_up = float(prices[up_idx]) > 0.9
            result = "Up" if resolved_up else "Down"
            won = trade["side"] == result

            if won:
                pnl = (1.0 - trade["entry_price"]) * trade["shares"] - trade["fee"]
            else:
                pnl = -(trade["entry_price"] * trade["shares"] + trade["fee"])

            trade["resolved"] = True
            trade["outcome"] = "win" if won else "loss"
            trade["pnl"] = round(pnl, 4)
            trade["resolved_at"] = now.isoformat()
            trade["market_result"] = result

            s = self.ledger["stats"]
            s["total_pnl"] += pnl
            s["total_trades"] += 1
            s["total_fees"] += trade["fee"]
            if won:
                s["wins"] += 1
            else:
                s["losses"] += 1

            self.ledger["trades"].append(trade)
            resolved_count += 1

            emoji = "✅" if won else "❌"
            ts_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"\n  [{ts_str}] {emoji} RESOLVED: {trade['side']} @ "
                  f"{trade['entry_price']:.3f} → {result} | PnL: ${pnl:+.2f}")
            print(f"           Signal was: composite={trade.get('composite_signal',0):+.3f}")

        self.ledger["open_positions"] = [
            t for t in self.ledger["open_positions"] if not t.get("resolved")
        ]
        if resolved_count:
            save_ledger(self.ledger)
        return resolved_count

    def log_status(self, signals, chainlink_price):
        now = time.time()
        if now - self.last_log < 15:
            return
        self.last_log = now

        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        s = self.ledger["stats"]
        pnl = s["total_pnl"]
        record = f"{s['wins']}W/{s['losses']}L"
        direction = signals["direction"]
        conf = signals["confidence"]
        comp = signals["composite"]

        print(f"  [{ts}] BTC=${chainlink_price:,.0f} | "
              f"{direction} (conf={conf:.0%} comp={comp:+.2f}) | "
              f"{self.signal_count} signals {self.trade_count} trades | "
              f"PnL=${pnl:+.2f} {record}")


# ─── Main Loop ────────────────────────────────────────────────────────
async def run(engine: SniperEngine):
    binance = BinanceSignals()
    markets = MarketTracker()
    chainlink_price = 0.0

    print(f"\n{'='*65}")
    print(f"🎯 Polymarket 5-Min BTC Sniper v2 — Momentum/Orderflow")
    print(f"   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"   Mode: {'DRY RUN' if engine.dry_run else 'PAPER TRADING'}")
    print(f"   Position: ${engine.position_size} | Min confidence: {engine.min_confidence:.0%}")
    print(f"   Entry window: first {engine.entry_window}s | Max price: ${engine.max_price}")
    print(f"{'='*65}\n")

    async def market_poller():
        loop = asyncio.get_event_loop()
        while True:
            try:
                await loop.run_in_executor(None, markets.refresh)
                await loop.run_in_executor(None, markets.update_orderbooks)
            except Exception as e:
                print(f"  ⚠️  Market poll: {e}")
            await asyncio.sleep(3)

    async def chainlink_poller():
        nonlocal chainlink_price
        loop = asyncio.get_event_loop()
        while True:
            try:
                price, ts = await loop.run_in_executor(None, fetch_chainlink_latest)
                if price:
                    chainlink_price = price
                    # Cache strikes at window boundaries
                    if ts:
                        window_ts = int(ts) // 300 * 300
                        if abs(ts - window_ts) < 2:
                            if window_ts not in engine.strike_cache:
                                engine.strike_cache[window_ts] = price
                                ts_str = datetime.fromtimestamp(
                                    window_ts, tz=timezone.utc
                                ).strftime("%H:%M:%S")
                                print(f"  📌 Strike: ${price:,.2f} at {ts_str} UTC")
            except:
                pass
            await asyncio.sleep(2)

    async def strategy_loop():
        nonlocal chainlink_price
        await asyncio.sleep(8)
        loop = asyncio.get_event_loop()
        while True:
            try:
                if chainlink_price > 0 and markets.markets:
                    signals = await loop.run_in_executor(None, binance.read_all)
                    engine.evaluate(signals, markets, chainlink_price)
                    engine.resolve_positions()
                    engine.log_status(signals, chainlink_price)
            except Exception as e:
                print(f"  ⚠️  Strategy: {e}")
                import traceback
                traceback.print_exc()
            await asyncio.sleep(3)

    await asyncio.gather(market_poller(), chainlink_poller(), strategy_loop())


def show_stats():
    ledger = load_ledger()
    s = ledger["stats"]
    print(f"\n{'='*60}")
    print(f"🎯 Sniper v2 Statistics")
    print(f"{'='*60}")
    print(f"  Total PnL:    ${s['total_pnl']:+.2f}")
    print(f"  Record:       {s['wins']}W / {s['losses']}L ({s['total_trades']} total)")
    if s["total_trades"] > 0:
        wr = s["wins"] / s["total_trades"] * 100
        print(f"  Win rate:     {wr:.1f}%")
        print(f"  Avg PnL:      ${s['total_pnl']/s['total_trades']:+.2f}/trade")
        print(f"  Total wagered: ${s['total_wagered']:.2f}")
        if s["total_wagered"] > 0:
            print(f"  ROI:          {s['total_pnl']/s['total_wagered']*100:+.1f}%")

    if ledger["open_positions"]:
        print(f"\n  Open ({len(ledger['open_positions'])}):")
        for t in ledger["open_positions"]:
            print(f"    {t['side']:4s} @ {t['entry_price']:.3f} | "
                  f"comp={t.get('composite_signal',0):+.3f} | {t['market'][:45]}")

    recent = ledger["trades"][-10:]
    if recent:
        print(f"\n  Recent:")
        for t in reversed(recent):
            emoji = "✅" if t["outcome"] == "win" else "❌"
            print(f"    {emoji} {t['side']:4s} @ {t['entry_price']:.3f} "
                  f"→ ${t['pnl']:+.2f} | comp={t.get('composite_signal',0):+.3f} | "
                  f"{t['market'][:40]}")


def main():
    parser = argparse.ArgumentParser(description="Polymarket 5-Min BTC Sniper v2")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--size", type=float, default=25.0)
    parser.add_argument("--confidence", type=float, default=0.25,
                        help="Min composite signal confidence (0-1)")
    parser.add_argument("--entry-window", type=int, default=90,
                        help="Enter only in first N seconds of window")
    parser.add_argument("--max-price", type=float, default=0.60,
                        help="Don't buy above this price")
    args = parser.parse_args()

    if args.stats:
        show_stats()
        return

    engine = SniperEngine(
        dry_run=args.dry_run,
        position_size=args.size,
        min_confidence=args.confidence,
        entry_window=args.entry_window,
        max_price=args.max_price,
    )

    def shutdown(sig, frame):
        print(f"\n\n  Shutting down... {engine.trade_count} trades.")
        show_stats()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    asyncio.run(run(engine))


if __name__ == "__main__":
    main()
