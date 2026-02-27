#!/usr/bin/env python3
"""
Polymarket Fast Market Watcher — Real-time WebSocket price monitor.

Connects to Binance and Polymarket WebSocket feeds for sub-second
price data. Runs three strategies (momentum, spread, fade) continuously.
Paper trades when divergence thresholds are met.

Usage:
    python3 watcher.py                    # run all strategies
    python3 watcher.py --strategy momentum
    python3 watcher.py --dry-run          # log signals, don't trade

Architecture:
    Binance WS → BTC price stream (100ms updates)
    Polymarket REST → orderbook polling (every 5s for active markets)
    Strategies evaluate on every price update
    Trades written to ledgers (same format as trader.py)
"""

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

try:
    import websockets
    HAS_WS = True
except ImportError:
    HAS_WS = False

BOT_DIR = Path(__file__).parent
LEDGER_DIR = BOT_DIR / "ledgers"
STATE_PATH = BOT_DIR / "watcher_state.json"
PID_PATH = BOT_DIR / "watcher.pid"

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
BINANCE_KLINE_WS = "wss://stream.binance.com:9443/ws/btcusdt@kline_1m"

# Market hours: 9:55 AM - 4:30 PM ET (when BTC 5-min markets are live)
MARKET_OPEN_HOUR = 9   # ET
MARKET_OPEN_MIN = 50
MARKET_CLOSE_HOUR = 16
MARKET_CLOSE_MIN = 35

# Fee formula for crypto fast markets
FEE_RATE = 0.25
FEE_EXPONENT = 2


def fetch_json(url, timeout=8):
    req = Request(url, headers={"User-Agent": "polymarket-watcher/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except:
        return None


def load_ledger(name):
    LEDGER_DIR.mkdir(exist_ok=True)
    p = LEDGER_DIR / f"{name}.json"
    if p.exists():
        return json.loads(p.read_text())
    return {
        "strategy": name,
        "trades": [],
        "open_positions": [],
        "stats": {"total_pnl": 0, "wins": 0, "losses": 0, "total_trades": 0,
                  "gross_profit": 0, "gross_loss": 0, "total_fees": 0},
    }


def save_ledger(name, ledger):
    LEDGER_DIR.mkdir(exist_ok=True)
    (LEDGER_DIR / f"{name}.json").write_text(json.dumps(ledger, indent=2))


def calc_fee(shares, price):
    """Calculate Polymarket crypto market fee."""
    return shares * FEE_RATE * (price * (1 - price)) ** FEE_EXPONENT


class MarketState:
    """Tracks current state of BTC fast markets on Polymarket."""

    def __init__(self):
        self.markets = []
        self.orderbooks = {}  # token_id -> {bids, asks, mid}
        self.last_refresh = 0
        self.refresh_interval = 30  # refresh market list every 30s

    def refresh_markets(self):
        """Find active BTC Up or Down fast markets."""
        now = time.time()
        if now - self.last_refresh < self.refresh_interval:
            return

        url = f"{GAMMA_BASE}/events?limit=50&active=true&closed=false&order=volume24hr&ascending=false"
        data = fetch_json(url)
        if data:
            # ONLY BTC 5-min fast markets — nothing else
            data = [e for e in data if "Up or Down" in e.get("title", "") and "BTC" in e.get("title", "").upper()]

        if not data:
            return

        self.markets = []
        for event in data:
            for m in event.get("markets", []):
                if m.get("closed") or not m.get("active"):
                    continue
                try:
                    outcomes = json.loads(m.get("outcomes", "[]"))
                    prices = json.loads(m.get("outcomePrices", "[]"))
                    tokens = json.loads(m.get("clobTokenIds", "[]"))
                    if len(outcomes) >= 2 and len(tokens) >= 2:
                        self.markets.append({
                            "id": m["id"],
                            "question": m.get("question", ""),
                            "slug": m.get("slug", ""),
                            "outcomes": outcomes,
                            "prices": [float(p) for p in prices],
                            "tokens": tokens,
                            "end_date": m.get("endDate", ""),
                            "event_title": event.get("title", ""),
                            "event_slug": event.get("slug", ""),
                        })
                except:
                    pass

        self.last_refresh = now

    def update_orderbooks(self):
        """Fetch orderbooks for all active markets."""
        for m in self.markets:
            for i, token in enumerate(m["tokens"][:2]):
                book = fetch_json(f"{CLOB_BASE}/book?token_id={token}")
                if book:
                    bids = sorted([{"p": float(b["price"]), "s": float(b["size"])}
                                   for b in book.get("bids", [])],
                                  key=lambda x: -x["p"])
                    asks = sorted([{"p": float(a["price"]), "s": float(a["size"])}
                                   for a in book.get("asks", [])],
                                  key=lambda x: x["p"])
                    mid = (bids[0]["p"] + asks[0]["p"]) / 2 if bids and asks else m["prices"][i]
                    best_bid = bids[0]["p"] if bids else 0
                    best_ask = asks[0]["p"] if asks else 1
                    self.orderbooks[token] = {
                        "bids": bids, "asks": asks, "mid": mid,
                        "best_bid": best_bid, "best_ask": best_ask,
                        "spread": best_ask - best_bid,
                    }


class BinanceState:
    """Tracks BTC price from Binance."""

    def __init__(self):
        self.price = 0.0
        self.prices_1m = []  # (timestamp, price) for last 5 min
        self.last_update = 0

    def update(self, price, ts=None):
        self.price = price
        self.last_update = ts or time.time()
        self.prices_1m.append((self.last_update, price))
        # Keep 5 min of history
        cutoff = self.last_update - 300
        self.prices_1m = [(t, p) for t, p in self.prices_1m if t > cutoff]

    @property
    def momentum_1m(self):
        """1-minute price change as fraction."""
        if len(self.prices_1m) < 2:
            return 0.0
        now = self.prices_1m[-1]
        one_min_ago = [p for t, p in self.prices_1m if t <= now[0] - 55]
        if not one_min_ago:
            return 0.0
        return (now[1] - one_min_ago[-1]) / one_min_ago[-1]

    @property
    def momentum_5m(self):
        """5-minute price change as fraction."""
        if len(self.prices_1m) < 2:
            return 0.0
        now = self.prices_1m[-1]
        five_min_ago = [p for t, p in self.prices_1m if t <= now[0] - 290]
        if not five_min_ago:
            return 0.0
        return (now[1] - five_min_ago[-1]) / five_min_ago[-1]


class StrategyEngine:
    """Evaluates all three strategies on each tick."""

    def __init__(self, strategies=None, dry_run=False, position_size=25.0):
        self.strategies = strategies or ["momentum", "spread", "fade"]
        self.dry_run = dry_run
        self.position_size = position_size
        self.last_trade_time = {}  # strategy -> timestamp (cooldown)
        self.cooldown = 120  # seconds between trades per strategy
        self.trade_count = 0
        self.signal_count = 0

    def evaluate(self, market_state: MarketState, binance: BinanceState):
        """Run all strategies against current state. Returns list of trades."""
        trades = []
        now = time.time()

        for market in market_state.markets:
            for strat in self.strategies:
                # Cooldown check
                last = self.last_trade_time.get(strat, 0)
                if now - last < self.cooldown:
                    continue

                signal = None
                if strat == "momentum":
                    signal = self._momentum(market, market_state, binance)
                elif strat == "spread":
                    signal = self._spread(market, market_state)
                elif strat == "fade":
                    signal = self._fade(market, market_state, binance)

                if signal:
                    self.signal_count += 1
                    trade = self._execute(strat, market, signal)
                    if trade:
                        trades.append(trade)
                        self.last_trade_time[strat] = now
                        self.trade_count += 1

        return trades

    def _momentum(self, market, ms: MarketState, binance: BinanceState):
        """Buy the direction BTC is moving if Polymarket hasn't caught up."""
        mom = binance.momentum_1m
        if abs(mom) < 0.001:  # need >0.1% move
            return None

        # Determine expected direction
        up_idx = 0 if "Up" in market["outcomes"][0] else 1
        down_idx = 1 - up_idx

        up_token = market["tokens"][up_idx]
        down_token = market["tokens"][down_idx]

        up_book = ms.orderbooks.get(up_token, {})
        down_book = ms.orderbooks.get(down_token, {})

        if not up_book or not down_book:
            return None

        if mom > 0.001:
            # BTC going up — Up token should be expensive
            fair_up = min(0.95, 0.5 + mom * 50)  # rough scaling
            market_up = up_book.get("best_ask", market["prices"][up_idx])
            edge = fair_up - market_up
            if edge > 0.03:  # >3% edge
                return {"side_idx": up_idx, "side": market["outcomes"][up_idx],
                        "price": market_up, "fair": fair_up, "edge": edge,
                        "reason": f"BTC +{mom*100:.2f}%, Up ask={market_up:.3f}, est fair={fair_up:.3f}"}
        elif mom < -0.001:
            fair_down = min(0.95, 0.5 + abs(mom) * 50)
            market_down = down_book.get("best_ask", market["prices"][down_idx])
            edge = fair_down - market_down
            if edge > 0.03:
                return {"side_idx": down_idx, "side": market["outcomes"][down_idx],
                        "price": market_down, "fair": fair_down, "edge": edge,
                        "reason": f"BTC {mom*100:.2f}%, Down ask={market_down:.3f}, est fair={fair_down:.3f}"}

        return None

    def _spread(self, market, ms: MarketState):
        """Buy both sides if Up + Down < $1 (guaranteed profit)."""
        tokens = market["tokens"][:2]
        books = [ms.orderbooks.get(t, {}) for t in tokens]

        if not all(books):
            return None

        ask_0 = books[0].get("best_ask", 1)
        ask_1 = books[1].get("best_ask", 1)
        total = ask_0 + ask_1

        if total >= 0.98:  # need at least 2% gap after fees
            return None

        gap = 1.0 - total
        # Estimate fees
        fee_0 = calc_fee(self.position_size / ask_0, ask_0)
        fee_1 = calc_fee(self.position_size / ask_1, ask_1)
        net_profit = (gap * self.position_size) - fee_0 - fee_1

        if net_profit <= 0:
            return None

        return {"side_idx": -1, "side": "BOTH",
                "price": total, "fair": 1.0, "edge": gap,
                "both_asks": [ask_0, ask_1],
                "reason": f"Spread arb: {ask_0:.3f}+{ask_1:.3f}={total:.3f}, gap={gap:.1%}, net=${net_profit:.2f}"}

    def _fade(self, market, ms: MarketState, binance: BinanceState):
        """Buy the cheap side when momentum is weak/reversing."""
        mom_1m = binance.momentum_1m
        mom_5m = binance.momentum_5m

        # Want: short-term momentum weakening against longer trend
        # Or: strong move that's likely to mean-revert
        if abs(mom_1m) > 0.003:  # too much momentum, don't fade
            return None

        up_idx = 0 if "Up" in market["outcomes"][0] else 1
        down_idx = 1 - up_idx

        up_book = ms.orderbooks.get(market["tokens"][up_idx], {})
        down_book = ms.orderbooks.get(market["tokens"][down_idx], {})

        if not up_book or not down_book:
            return None

        # Find the cheap side
        up_ask = up_book.get("best_ask", 0.5)
        down_ask = down_book.get("best_ask", 0.5)

        # Buy cheap side if it's significantly below 50%
        if up_ask < 0.42:
            edge = 0.50 - up_ask
            if edge > 0.06:
                return {"side_idx": up_idx, "side": market["outcomes"][up_idx],
                        "price": up_ask, "fair": 0.50, "edge": edge,
                        "reason": f"Fade: Up cheap at {up_ask:.3f}, mom flat ({mom_1m*100:.2f}%), est revert to ~50%"}
        elif down_ask < 0.42:
            edge = 0.50 - down_ask
            if edge > 0.06:
                return {"side_idx": down_idx, "side": market["outcomes"][down_idx],
                        "price": down_ask, "fair": 0.50, "edge": edge,
                        "reason": f"Fade: Down cheap at {down_ask:.3f}, mom flat ({mom_1m*100:.2f}%), est revert to ~50%"}

        return None

    def _execute(self, strategy, market, signal):
        """Paper trade execution."""
        ledger = load_ledger(strategy)

        # Check for existing position in same market
        for pos in ledger["open_positions"]:
            if pos.get("slug") == market["slug"]:
                return None  # already positioned

        price = signal["price"]
        if price <= 0 or price >= 1:
            return None

        shares = self.position_size / price
        fee = calc_fee(shares, price)
        net_ev = (signal["fair"] - price) * shares - fee

        trade = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "strategy": strategy,
            "market": market["question"],
            "event_title": market["event_title"],
            "event_slug": market.get("event_slug", ""),
            "slug": market["slug"],
            "market_end": market["end_date"],
            "side": signal["side"],
            "entry_price": price,
            "shares": shares,
            "cost": self.position_size,
            "fee": fee,
            "fair_value": signal["fair"],
            "net_ev": net_ev,
            "reason": signal["reason"],
            "resolved": False,
            "outcome": None,
            "pnl": None,
        }

        if self.dry_run:
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"  [{ts}] 🔔 SIGNAL [{strategy}] {signal['side']} @ {price:.3f} "
                  f"(edge={signal['edge']:.1%}) | {market['question'][:50]}")
            print(f"           {signal['reason']}")
            return None

        ledger["open_positions"].append(trade)
        save_ledger(strategy, ledger)

        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"  [{ts}] 🎲 TRADE [{strategy}] {signal['side']} @ {price:.3f} "
              f"(edge={signal['edge']:.1%}, EV=${net_ev:+.2f})")
        print(f"           {signal['reason']}")

        return trade


async def run_polling_loop(engine, market_state, binance):
    """Fallback polling mode when websockets not available."""
    print("  Running in polling mode (5s interval)")
    print("  Install websockets for real-time: pip install websockets\n")

    tick = 0
    while True:
        try:
            # Refresh markets every 30s
            market_state.refresh_markets()

            # Update orderbooks every 5s
            market_state.update_orderbooks()

            # Update BTC price from Binance REST
            data = fetch_json("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
            if data:
                binance.update(float(data["price"]))

            # Evaluate strategies
            trades = engine.evaluate(market_state, binance)

            # Status line every 30s
            tick += 1
            if tick % 6 == 0:
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                mom = binance.momentum_1m * 100
                n_markets = len(market_state.markets)
                print(f"  [{ts}] BTC=${binance.price:,.0f} mom={mom:+.2f}% | "
                      f"{n_markets} markets | {engine.signal_count} signals, "
                      f"{engine.trade_count} trades")

        except Exception as e:
            print(f"  Error: {e}")

        await asyncio.sleep(5)


async def run_websocket_loop(engine, market_state, binance):
    """Real-time mode with Binance WebSocket + Polymarket polling."""
    print("  Running in WebSocket mode (real-time BTC prices)\n")

    async def binance_feed():
        """Stream BTC prices from Binance."""
        reconnect_delay = 1
        while True:
            try:
                async with websockets.connect(BINANCE_WS) as ws:
                    reconnect_delay = 1
                    async for msg in ws:
                        data = json.loads(msg)
                        price = float(data.get("p", 0))
                        ts = data.get("T", 0) / 1000
                        if price > 0:
                            binance.update(price, ts)
            except Exception as e:
                print(f"  Binance WS error: {e}, reconnecting in {reconnect_delay}s")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)

    async def orderbook_poller():
        """Poll Polymarket orderbooks every 5s."""
        while True:
            try:
                market_state.refresh_markets()
                market_state.update_orderbooks()
            except Exception as e:
                print(f"  Orderbook poll error: {e}")
            await asyncio.sleep(5)

    async def strategy_loop():
        """Evaluate strategies every second."""
        tick = 0
        while True:
            try:
                if binance.price > 0 and market_state.markets:
                    trades = engine.evaluate(market_state, binance)

                tick += 1
                if tick % 30 == 0:
                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    mom = binance.momentum_1m * 100
                    n_markets = len(market_state.markets)
                    n_books = len(market_state.orderbooks)
                    print(f"  [{ts}] BTC=${binance.price:,.0f} mom={mom:+.2f}% | "
                          f"{n_markets} mkts, {n_books} books | "
                          f"{engine.signal_count} signals, {engine.trade_count} trades")
            except Exception as e:
                print(f"  Strategy error: {e}")

            await asyncio.sleep(1)

    await asyncio.gather(binance_feed(), orderbook_poller(), strategy_loop())


def is_market_hours():
    """Check if we're within BTC fast market trading hours (ET)."""
    from datetime import timezone as tz
    utc_now = datetime.now(tz.utc)
    # ET is UTC-5 (EST) or UTC-4 (EDT). Approximate: use -5 for now.
    et_hour = (utc_now.hour - 5) % 24
    et_min = utc_now.minute
    
    open_mins = MARKET_OPEN_HOUR * 60 + MARKET_OPEN_MIN
    close_mins = MARKET_CLOSE_HOUR * 60 + MARKET_CLOSE_MIN
    now_mins = et_hour * 60 + et_min
    
    return open_mins <= now_mins <= close_mins


async def run_with_schedule(engine, market_state, binance, ws=False):
    """Run trading loop during market hours, sleep otherwise."""
    while True:
        if not is_market_hours():
            ts = datetime.now(timezone.utc).strftime('%H:%M:%S')
            print(f"  [{ts}] Outside market hours (9:50AM-4:35PM ET) — sleeping 5m")
            await asyncio.sleep(300)
            continue

        ts = datetime.now(timezone.utc).strftime('%H:%M:%S')
        print(f"  [{ts}] Market hours — active")

        # Run the appropriate loop (these run forever, so we break out
        # by checking hours inside strategy_loop / polling_loop)
        if ws:
            await run_websocket_loop_scheduled(engine, market_state, binance)
        else:
            await run_polling_loop_scheduled(engine, market_state, binance)


async def run_polling_loop_scheduled(engine, market_state, binance):
    """Polling mode with market hours check."""
    tick = 0
    while is_market_hours():
        try:
            market_state.refresh_markets()
            market_state.update_orderbooks()
            data = fetch_json("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
            if data:
                binance.update(float(data["price"]))
            engine.evaluate(market_state, binance)
            tick += 1
            if tick % 6 == 0:
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                mom = binance.momentum_1m * 100
                print(f"  [{ts}] BTC=${binance.price:,.0f} mom={mom:+.2f}% | "
                      f"{len(market_state.markets)} mkts | "
                      f"{engine.signal_count} signals, {engine.trade_count} trades")
        except Exception as e:
            print(f"  Error: {e}")
        await asyncio.sleep(5)


async def run_websocket_loop_scheduled(engine, market_state, binance):
    """WebSocket mode with market hours check."""

    async def binance_feed():
        reconnect_delay = 1
        while is_market_hours():
            try:
                async with websockets.connect(BINANCE_WS) as ws:
                    reconnect_delay = 1
                    async for msg in ws:
                        if not is_market_hours():
                            return
                        data = json.loads(msg)
                        price = float(data.get("p", 0))
                        ts = data.get("T", 0) / 1000
                        if price > 0:
                            binance.update(price, ts)
            except Exception as e:
                if not is_market_hours():
                    return
                print(f"  Binance WS error: {e}, reconnecting in {reconnect_delay}s")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)

    async def orderbook_poller():
        while is_market_hours():
            try:
                market_state.refresh_markets()
                market_state.update_orderbooks()
            except Exception as e:
                print(f"  Orderbook poll error: {e}")
            await asyncio.sleep(5)

    async def strategy_loop():
        tick = 0
        while is_market_hours():
            try:
                if binance.price > 0 and market_state.markets:
                    engine.evaluate(market_state, binance)
                tick += 1
                if tick % 30 == 0:
                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    mom = binance.momentum_1m * 100
                    print(f"  [{ts}] BTC=${binance.price:,.0f} mom={mom:+.2f}% | "
                          f"{len(market_state.markets)} mkts, {len(market_state.orderbooks)} books | "
                          f"{engine.signal_count} signals, {engine.trade_count} trades")
            except Exception as e:
                print(f"  Strategy error: {e}")
            await asyncio.sleep(1)

    await asyncio.gather(binance_feed(), orderbook_poller(), strategy_loop())


def main():
    parser = argparse.ArgumentParser(description="Polymarket Fast Market Watcher")
    parser.add_argument("--strategy", nargs="+", default=["momentum", "spread", "fade"],
                        help="Strategies to run")
    parser.add_argument("--dry-run", action="store_true", help="Log signals without trading")
    parser.add_argument("--size", type=float, default=25.0, help="Position size ($)")
    parser.add_argument("--polling", action="store_true", help="Force polling mode (no WebSocket)")
    args = parser.parse_args()

    # Write PID file
    PID_PATH.write_text(str(os.getpid()))

    print(f"\n{'='*60}")
    print(f"⚡ Polymarket Fast Market Watcher")
    print(f"   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"   Strategies: {', '.join(args.strategy)}")
    print(f"   Position size: ${args.size}")
    print(f"   Mode: {'dry-run' if args.dry_run else 'paper trading'}")
    print(f"{'='*60}\n")

    engine = StrategyEngine(
        strategies=args.strategy,
        dry_run=args.dry_run,
        position_size=args.size,
    )
    market_state = MarketState()
    binance = BinanceState()

    # Handle shutdown
    def shutdown(sig, frame):
        print(f"\n  Shutting down... {engine.trade_count} trades placed.")
        PID_PATH.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    use_ws = HAS_WS and not args.polling
    if use_ws:
        asyncio.run(run_with_schedule(engine, market_state, binance, ws=True))
    else:
        asyncio.run(run_with_schedule(engine, market_state, binance, ws=False))


if __name__ == "__main__":
    main()
