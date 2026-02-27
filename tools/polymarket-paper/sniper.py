#!/usr/bin/env python3
"""
Polymarket 5-Minute BTC Sniper

Streams real-time BTC price, compares against Polymarket 5-min "Up or Down"
markets, and trades when the fair probability diverges from market price.

The edge: Binance WebSocket updates in ~100ms. Polymarket UI traders are
seconds behind. We calculate the correct probability and buy mispriced contracts.

Fair value model:
  - Strike S = BTC price at window start (inferred from Binance history)
  - Current price P = live BTC price
  - Time remaining T = seconds until window end
  - σ = BTC realized volatility (rolling)
  - Fair P(Up) = Φ((P - S) / (σ * √T))  [normal CDF — digital option pricing]

Usage:
    python3 sniper.py                  # paper trade
    python3 sniper.py --dry-run        # signals only, no trades
    python3 sniper.py --live           # REAL trading (requires wallet config)
    python3 sniper.py --stats          # show paper trading results

Requirements:
    pip install websockets
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

try:
    import websockets
    HAS_WS = True
except ImportError:
    HAS_WS = False

# ─── Constants ────────────────────────────────────────────────────────
BOT_DIR = Path(__file__).parent
LEDGER_PATH = BOT_DIR / "ledgers" / "sniper.json"
STATE_PATH = BOT_DIR / "sniper_state.json"
CREDS_PATH = Path.home() / ".openclaw" / "workspace" / ".polymarket-creds.json"

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"

# Chainlink Data Streams (the actual resolution source)
CHAINLINK_FEED_ID = "0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8"
CHAINLINK_API = "https://data.chain.link/api/query-timescale"
CHAINLINK_PRICE_DECIMALS = 18  # price field has 18 decimal places

# Fee formula for crypto fast markets
FEE_RATE = 0.25
FEE_EXPONENT = 2


# ─── Math ─────────────────────────────────────────────────────────────
def norm_cdf(x):
    """Standard normal CDF using math.erfc for correctness."""
    return 0.5 * math.erfc(-x / math.sqrt(2))


def fair_prob_up(current_price, strike_price, time_remaining_s, volatility_per_s):
    """
    Fair probability that BTC will be >= strike at expiry.

    This is the digital/binary option pricing formula:
    P(Up) = Φ((P - S) / (σ * √T))

    Where σ is per-second volatility and T is seconds remaining.
    """
    if time_remaining_s <= 0:
        return 1.0 if current_price >= strike_price else 0.0
    if volatility_per_s <= 0:
        return 0.5

    sigma_t = volatility_per_s * math.sqrt(time_remaining_s)
    if sigma_t < 1e-10:
        return 1.0 if current_price >= strike_price else 0.0

    d = (current_price - strike_price) / sigma_t
    return norm_cdf(d)


def calc_fee(shares, price):
    """Polymarket crypto market fee."""
    if price <= 0 or price >= 1:
        return 0
    return shares * FEE_RATE * (price * (1 - price)) ** FEE_EXPONENT


# ─── HTTP ─────────────────────────────────────────────────────────────
def fetch_json(url, timeout=8):
    req = Request(url, headers={"User-Agent": "polymarket-sniper/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except:
        return None


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
            "gross_profit": 0, "gross_loss": 0, "total_fees": 0,
            "total_wagered": 0,
        },
    }


def save_ledger(ledger):
    LEDGER_PATH.parent.mkdir(exist_ok=True)
    LEDGER_PATH.write_text(json.dumps(ledger, indent=2))


# ─── Chainlink Price Feed ─────────────────────────────────────────────
def fetch_chainlink_prices():
    """Fetch latest Chainlink BTC/USD prices (the actual resolution source).
    Returns list of (timestamp, price) tuples, most recent first."""
    url = (f"{CHAINLINK_API}?query=LIVE_STREAM_REPORTS_QUERY"
           f"&variables=%7B%22feedId%22%3A%22{CHAINLINK_FEED_ID}%22%7D")
    data = fetch_json(url)
    if not data:
        return []

    nodes = data.get("data", {}).get("liveStreamReports", {}).get("nodes", [])
    results = []
    for node in nodes:
        try:
            ts_str = node["validFromTimestamp"]
            # Parse ISO timestamp
            dt = datetime.fromisoformat(ts_str.replace("+00:00", "+00:00"))
            ts = dt.timestamp()
            # Price is in 18-decimal format
            price = float(node["price"]) / (10 ** CHAINLINK_PRICE_DECIMALS)
            results.append((ts, price))
        except:
            continue
    return results


def get_chainlink_price_at(target_ts):
    """Get Chainlink BTC/USD price at a specific timestamp."""
    prices = fetch_chainlink_prices()
    if not prices:
        return None

    best = None
    best_diff = float('inf')
    for ts, price in prices:
        diff = abs(ts - target_ts)
        if diff < best_diff:
            best_diff = diff
            best = price

    # Only trust if within 60 seconds (Chainlink updates every second)
    return best if best_diff < 60 else None


# ─── Price Tracker ────────────────────────────────────────────────────
class PriceTracker:
    """
    Tracks BTC price from Binance with rolling volatility estimation.
    Stores price history to infer strike prices for 5-min windows.
    """

    def __init__(self):
        self.price = 0.0
        self.last_update = 0.0
        # Store (timestamp, price) for last 10 minutes
        self.history = deque(maxlen=12000)  # ~100ms ticks for 20 min
        # For volatility: store 1-second returns
        self.second_prices = deque(maxlen=600)  # 10 min of 1s prices
        self._last_second = 0
        self._volatility_cache = 0.0
        self._vol_cache_time = 0

    def update(self, price, ts=None):
        ts = ts or time.time()
        self.price = price
        self.last_update = ts
        self.history.append((ts, price))

        # Sample 1-second prices for volatility
        second = int(ts)
        if second > self._last_second:
            self.second_prices.append((second, price))
            self._last_second = second

    def get_price_at(self, target_ts):
        """Get the BTC price closest to a given timestamp.
        Falls back to Binance 1-minute klines if we don't have local history."""
        if self.history:
            best = None
            best_diff = float('inf')
            for ts, price in self.history:
                diff = abs(ts - target_ts)
                if diff < best_diff:
                    best_diff = diff
                    best = price
            if best_diff < 30:
                return best

        # Fallback: fetch from Binance klines
        return self._strike_from_klines(target_ts)

    def _strike_from_klines(self, target_ts):
        """Get BTC price at a specific timestamp from Binance klines."""
        cache_key = int(target_ts) // 60  # cache per minute
        if hasattr(self, '_kline_cache') and cache_key in self._kline_cache:
            return self._kline_cache[cache_key]

        if not hasattr(self, '_kline_cache'):
            self._kline_cache = {}

        try:
            # Fetch klines around the target time
            start_ms = int((target_ts - 120) * 1000)
            end_ms = int((target_ts + 120) * 1000)
            url = (f"https://api.binance.com/api/v3/klines"
                   f"?symbol=BTCUSDT&interval=1m&startTime={start_ms}&endTime={end_ms}")
            from urllib.request import urlopen, Request
            req = Request(url, headers={"User-Agent": "sniper/1.0"})
            import json as _json
            data = _json.loads(urlopen(req, timeout=5).read())

            if not data:
                return None

            # Find the candle containing our target timestamp
            for k in data:
                open_time = k[0] / 1000
                close_time = k[6] / 1000
                if open_time <= target_ts <= close_time:
                    # Interpolate: use open price (closest to start of interval)
                    open_price = float(k[1])
                    close_price = float(k[4])
                    # Linear interpolation within the candle
                    frac = (target_ts - open_time) / max(close_time - open_time, 1)
                    price = open_price + frac * (close_price - open_price)
                    self._kline_cache[cache_key] = price
                    return price

            # If target is before first candle, use first open
            if data:
                price = float(data[0][1])
                self._kline_cache[cache_key] = price
                return price
        except Exception:
            pass
        return None

    @property
    def volatility_per_second(self):
        """
        Rolling realized volatility (σ per second).
        Calculated from 1-second log returns over the last 5 minutes.

        We apply a 2x multiplier because:
        - Realized vol from recent history underestimates tail moves
        - Polymarket market makers use higher implied vol
        - The resolution window is short enough that microstructure noise matters
        """
        VOL_MULTIPLIER = 2.0  # Calibrated against market pricing

        now = time.time()
        if now - self._vol_cache_time < 5 and self._volatility_cache > 0:
            return self._volatility_cache

        prices = [(t, p) for t, p in self.second_prices if t > now - 300]
        if len(prices) < 30:
            # Default: ~0.01% per second (typical BTC 5-min vol) × multiplier
            return 0.0001 * self.price * VOL_MULTIPLIER if self.price > 0 else 10.0

        returns = []
        for i in range(1, len(prices)):
            if prices[i - 1][1] > 0:
                r = math.log(prices[i][1] / prices[i - 1][1])
                returns.append(r)

        if len(returns) < 10:
            return 0.0001 * self.price * VOL_MULTIPLIER if self.price > 0 else 10.0

        mean_r = sum(returns) / len(returns)
        var = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        # Convert from log-return vol to dollar vol, apply multiplier
        vol_per_s = math.sqrt(var) * self.price * VOL_MULTIPLIER

        self._volatility_cache = vol_per_s
        self._vol_cache_time = now
        return vol_per_s


# ─── Market Tracker ──────────────────────────────────────────────────
class MarketTracker:
    """Discovers and tracks active BTC 5-min markets."""

    def __init__(self):
        self.markets = []
        self.known_slugs = {}  # slug -> market data (cached)
        self.strike_cache = {}  # slug -> strike price (from scraping)
        self.orderbooks = {}  # token_id -> book data
        self.last_refresh = 0
        self.last_book_update = 0

    def refresh(self):
        """Find active BTC 5-min Up or Down markets.
        
        These markets don't appear in generic Gamma API listings.
        We generate slugs from timestamps: btc-updown-5m-{unix_start}
        where unix_start is the 5-minute window boundary.
        """
        now = time.time()
        # Refresh every 15s, but skip slugs we already cached
        if now - self.last_refresh < 15:
            return

        # Generate slugs for current + next few 5-min windows
        current_window = int(now) // 300 * 300
        slugs_to_check = []
        for offset in range(-1, 6):  # previous, current, and next 5 windows
            ts = current_window + (offset * 300)
            slug = f"btc-updown-5m-{ts}"
            slugs_to_check.append((slug, ts))

        utc_now = datetime.now(timezone.utc)
        self.markets = []

        for slug, window_ts in slugs_to_check:
            # Use cache if we already know this market's tokens
            if slug in self.known_slugs:
                cached = self.known_slugs[slug]
                remaining = (cached["end_dt"] - utc_now).total_seconds()
                if remaining <= 0:
                    # Expired, remove from cache
                    del self.known_slugs[slug]
                    continue
                cached["remaining_s"] = remaining
                self.markets.append(cached)
                continue

            # Fetch from API (only for unknown slugs)
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
                    "window_start_ts": window_ts,
                    "window_end_ts": window_ts + 300,
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
                break  # one market per event

        # Sort by soonest expiry
        self.markets.sort(key=lambda x: x["remaining_s"])
        self.last_refresh = now

    def update_orderbooks(self):
        """Fetch orderbooks for active markets."""
        now = time.time()
        if now - self.last_book_update < 2:
            return
        self.last_book_update = now

        for market in self.markets:
            # Only fetch books for markets expiring in < 10 min
            if market["remaining_s"] > 600:
                continue

            for token in [market["up_token"], market["down_token"]]:
                book = fetch_json(f"{CLOB_BASE}/book?token_id={token}")
                if not book:
                    continue
                bids = sorted(
                    [{"p": float(b["price"]), "s": float(b["size"])} for b in book.get("bids", [])],
                    key=lambda x: -x["p"]
                )
                asks = sorted(
                    [{"p": float(a["price"]), "s": float(a["size"])} for a in book.get("asks", [])],
                    key=lambda x: x["p"]
                )
                self.orderbooks[token] = {
                    "bids": bids,
                    "asks": asks,
                    "best_bid": bids[0]["p"] if bids else 0,
                    "best_ask": asks[0]["p"] if asks else 1,
                    "bid_depth": sum(b["s"] for b in bids[:3]),
                    "ask_depth": sum(a["s"] for a in asks[:3]),
                }

    def get_market_price(self, token):
        """Get best ask (what we'd pay to buy) for a token."""
        book = self.orderbooks.get(token, {})
        return book.get("best_ask", None)

    def get_market_bid(self, token):
        """Get best bid (what we'd get selling) for a token."""
        book = self.orderbooks.get(token, {})
        return book.get("best_bid", None)

    def get_strike(self, slug):
        """Get the strike price ('Price to beat') for a market.
        Scrapes it from the Polymarket page HTML since the API doesn't expose it."""
        if slug in self.strike_cache:
            return self.strike_cache[slug]

        try:
            url = f"https://polymarket.com/event/{slug}"
            req = Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                "Accept": "text/html",
            })
            with urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="ignore")

            # Look for "Price to beat" followed by a dollar amount
            import re
            # Pattern: Price to beat$XX,XXX.XX or Price to beat $XX,XXX.XX
            match = re.search(r'Price to beat\s*\$?([\d,]+\.?\d*)', html)
            if match:
                price_str = match.group(1).replace(",", "")
                strike = float(price_str)
                self.strike_cache[slug] = strike
                return strike
        except Exception as e:
            pass  # Fall through to None
        return None


# ─── Sniper Engine ───────────────────────────────────────────────────
class SniperEngine:
    """
    Core trading logic. Calculates fair value and identifies mispricings.
    """

    def __init__(self, dry_run=False, position_size=25.0, min_edge=0.05,
                 min_time=15, max_time=240):
        self.dry_run = dry_run
        self.position_size = position_size
        self.min_edge = min_edge      # minimum probability edge to trade
        self.min_time = min_time      # don't trade < N seconds remaining
        self.max_time = max_time      # don't trade > N seconds remaining (too early)
        self.traded_windows = set()    # slugs we've already traded/signaled
        self.ledger = load_ledger()
        self._strike_cache = {}  # window_start_ts -> chainlink price
        self.trade_count = 0
        self.signal_count = 0
        self.last_log = 0

    def evaluate(self, prices: PriceTracker, markets: MarketTracker):
        """Evaluate all active markets for trading opportunities."""
        if prices.price <= 0:
            return []

        trades = []
        now = time.time()

        for market in markets.markets:
            remaining = market["window_end_ts"] - now

            # Time filters
            if remaining < self.min_time or remaining > self.max_time:
                continue

            # One trade per window max
            if market["slug"] in self.traded_windows:
                continue

            # Already positioned?
            open_slugs = {p["slug"] for p in self.ledger["open_positions"]}
            if market["slug"] in open_slugs:
                continue

            # Get strike price from cached Chainlink data at window start
            strike = self._strike_cache.get(market["window_start_ts"])
            if strike is None:
                # Try Chainlink API (only has ~60s of history)
                strike = get_chainlink_price_at(market["window_start_ts"])
            if strike is None:
                # Fallback to local price tracker
                strike = prices.get_price_at(market["window_start_ts"])
            if strike is None:
                continue

            # Calculate fair probability
            vol = prices.volatility_per_second
            fair_up = fair_prob_up(prices.price, strike, remaining, vol)
            fair_down = 1.0 - fair_up

            # Get market prices from orderbook
            market_ask_up = markets.get_market_price(market["up_token"])
            market_ask_down = markets.get_market_price(market["down_token"])

            if market_ask_up is None and market_ask_down is None:
                continue

            # Check for edge on Up side
            if market_ask_up is not None and market_ask_up < 0.95:
                edge_up = fair_up - market_ask_up
                if edge_up >= self.min_edge:
                    trade = self._make_trade(
                        market, "Up", market_ask_up, fair_up, edge_up,
                        strike, prices.price, remaining, vol, now
                    )
                    if trade:
                        trades.append(trade)
                        continue

            # Check for edge on Down side
            if market_ask_down is not None and market_ask_down < 0.95:
                edge_down = fair_down - market_ask_down
                if edge_down >= self.min_edge:
                    trade = self._make_trade(
                        market, "Down", market_ask_down, fair_down, edge_down,
                        strike, prices.price, remaining, vol, now
                    )
                    if trade:
                        trades.append(trade)

        return trades

    def _make_trade(self, market, side, entry_price, fair_value, edge,
                    strike, btc_price, remaining, vol, now):
        """Create and record a paper trade."""
        self.signal_count += 1

        shares = self.position_size / entry_price
        fee = calc_fee(shares, entry_price)
        net_ev = (fair_value - entry_price) * shares - fee

        if net_ev <= 0:
            return None

        ts_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
        direction = "↑" if side == "Up" else "↓"
        print(f"\n  [{ts_str}] {direction} SIGNAL: {side} @ {entry_price:.3f} "
              f"(fair={fair_value:.3f}, edge={edge:.1%})")
        print(f"           BTC=${btc_price:,.1f} strike=${strike:,.1f} "
              f"Δ={btc_price-strike:+,.1f} | {remaining:.0f}s left | σ={vol:.2f}/s")
        print(f"           EV=${net_ev:+.2f} | {market['question'][:55]}")

        # Mark this window as traded (even in dry-run, to avoid spam)
        self.traded_windows.add(market["slug"])

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
            "cost": self.position_size,
            "fee": round(fee, 6),
            "fair_value": round(fair_value, 4),
            "edge": round(edge, 4),
            "net_ev": round(net_ev, 4),
            "btc_price": btc_price,
            "strike_price": strike,
            "btc_delta": round(btc_price - strike, 2),
            "time_remaining": round(remaining, 1),
            "volatility": round(vol, 4),
            "resolved": False,
            "outcome": None,
            "pnl": None,
        }

        self.ledger["open_positions"].append(trade)
        self.ledger["stats"]["total_wagered"] += self.position_size
        save_ledger(self.ledger)

        self.trade_count += 1

        print(f"           🎲 PAPER TRADE: {shares:.1f} shares @ ${entry_price:.3f}")
        return trade

    def resolve_positions(self, markets: MarketTracker):
        """Check if any open positions have resolved."""
        now = datetime.now(timezone.utc)
        resolved_count = 0

        for trade in self.ledger["open_positions"]:
            if trade.get("resolved"):
                continue

            end = datetime.fromisoformat(trade["market_end"].replace("Z", "+00:00"))
            if now < end + timedelta(seconds=30):
                continue

            # Check resolution via Gamma events API (more reliable than markets API)
            data = fetch_json(f"{GAMMA_BASE}/events?slug={trade['slug']}")
            if not data or len(data) == 0:
                continue
            event = data[0]
            # Find the market within the event
            market = None
            for m in event.get("markets", []):
                if m.get("closed"):
                    market = m
                    break
            if not market:
                # Also check if event itself is closed
                if not event.get("closed"):
                    continue
                # Event closed but no closed market found — skip
                continue

            try:
                prices = json.loads(market.get("outcomePrices", "[]"))
                outcomes = json.loads(market.get("outcomes", "[]"))
            except:
                continue
            if not prices:
                continue

            # Find which outcome won
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
                s["gross_profit"] += pnl + trade["fee"]
            else:
                s["losses"] += 1
                s["gross_loss"] += abs(pnl) - trade["fee"]

            self.ledger["trades"].append(trade)
            resolved_count += 1

            emoji = "✅" if won else "❌"
            ts_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"\n  [{ts_str}] {emoji} RESOLVED: {trade['side']} @ {trade['entry_price']:.3f} "
                  f"→ {result} | PnL: ${pnl:+.2f}")
            print(f"           BTC Δ was {trade['btc_delta']:+,.1f} from strike")

        self.ledger["open_positions"] = [
            t for t in self.ledger["open_positions"] if not t.get("resolved")
        ]
        if resolved_count:
            save_ledger(self.ledger)
        return resolved_count

    def log_status(self, prices: PriceTracker, markets: MarketTracker):
        """Print status line every 15 seconds."""
        now = time.time()
        if now - self.last_log < 15:
            return
        self.last_log = now

        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        vol = prices.volatility_per_second
        n_markets = len([m for m in markets.markets if m["remaining_s"] < 600])
        s = self.ledger["stats"]
        pnl = s["total_pnl"]
        record = f"{s['wins']}W/{s['losses']}L"

        # Show nearest market's fair value
        fair_str = ""
        for market in markets.markets:
            remaining = market["window_end_ts"] - now
            if self.min_time < remaining < self.max_time:
                strike = prices.get_price_at(market["window_start_ts"])
                if strike:
                    fair = fair_prob_up(prices.price, strike, remaining, vol)
                    mkt_up = markets.get_market_price(market["up_token"])
                    edge = (fair - mkt_up) if mkt_up else 0
                    fair_str = (f" | fair_up={fair:.1%}"
                               f" mkt={mkt_up:.3f}" if mkt_up else ""
                               f" edge={edge:+.1%}" if mkt_up else ""
                               f" Δ${prices.price-strike:+,.0f} {remaining:.0f}s")
                    break

        print(f"  [{ts}] BTC=${prices.price:,.0f} σ={vol:.1f}/s | "
              f"{n_markets} active | {self.signal_count} signals {self.trade_count} trades | "
              f"PnL=${pnl:+.2f} {record}{fair_str}")


# ─── Main Loop ────────────────────────────────────────────────────────
async def run_ws(engine: SniperEngine):
    """Main event loop with Binance WebSocket + Polymarket polling."""
    prices = PriceTracker()
    markets = MarketTracker()

    print(f"\n{'='*65}")
    print(f"🎯 Polymarket 5-Min BTC Sniper")
    print(f"   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"   Mode: {'DRY RUN' if engine.dry_run else 'PAPER TRADING'}")
    print(f"   Position: ${engine.position_size} | Min edge: {engine.min_edge:.0%}")
    print(f"   Time window: {engine.min_time}s - {engine.max_time}s remaining")
    print(f"   WebSocket: {'YES' if HAS_WS else 'NO (polling)'}")
    print(f"{'='*65}\n")

    async def binance_ws():
        """Stream real-time BTC prices."""
        reconnect_delay = 1
        while True:
            try:
                async with websockets.connect(BINANCE_WS, ping_interval=20) as ws:
                    reconnect_delay = 1
                    print("  📡 Binance WebSocket connected")
                    async for msg in ws:
                        data = json.loads(msg)
                        price = float(data.get("p", 0))
                        ts = float(data.get("T", 0)) / 1000.0
                        if price > 0:
                            prices.update(price, ts)
            except Exception as e:
                print(f"  ⚠️  Binance WS: {e} — reconnecting in {reconnect_delay}s")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30)

    async def market_poller():
        """Poll Polymarket markets and orderbooks."""
        loop = asyncio.get_event_loop()
        while True:
            try:
                # Run blocking HTTP calls in thread pool to avoid stalling
                await loop.run_in_executor(None, markets.refresh)
                await loop.run_in_executor(None, markets.update_orderbooks)
            except Exception as e:
                print(f"  ⚠️  Market poll: {e}")
            await asyncio.sleep(3)

    async def chainlink_poller():
        """Poll Chainlink prices every 2 seconds (the actual resolution source).
        Also caches prices at 5-min window boundaries for strike lookup."""
        loop = asyncio.get_event_loop()
        while True:
            try:
                cl_prices = await loop.run_in_executor(None, fetch_chainlink_prices)
                if cl_prices:
                    # Update tracker with latest Chainlink price
                    ts, price = cl_prices[0]
                    prices.update(price, ts)

                    # Cache prices at window boundaries for strike lookup
                    for cl_ts, cl_price in cl_prices:
                        window_ts = int(cl_ts) // 300 * 300
                        if abs(cl_ts - window_ts) < 2:  # within 2s of boundary
                            if window_ts not in engine._strike_cache:
                                engine._strike_cache[window_ts] = cl_price
                                ts_str = datetime.fromtimestamp(
                                    window_ts, tz=timezone.utc
                                ).strftime("%H:%M:%S")
                                print(f"  📌 Strike cached: ${cl_price:,.2f} "
                                      f"at {ts_str} UTC (window {window_ts})")
            except:
                pass
            await asyncio.sleep(2)

    async def strategy_loop():
        """Evaluate strategy every second."""
        # Wait for initial data
        await asyncio.sleep(8)
        while True:
            try:
                if prices.price > 0:
                    engine.evaluate(prices, markets)
                    engine.resolve_positions(markets)
                    engine.log_status(prices, markets)
            except Exception as e:
                print(f"  ⚠️  Strategy: {e}")
                import traceback
                traceback.print_exc()
            await asyncio.sleep(1)

    print("  📡 Using Chainlink Data Streams (resolution source)")
    await asyncio.gather(chainlink_poller(), market_poller(), strategy_loop())


def show_stats():
    """Display paper trading statistics."""
    ledger = load_ledger()
    s = ledger["stats"]

    print(f"\n{'='*60}")
    print(f"🎯 Sniper Statistics")
    print(f"{'='*60}")
    print(f"  Total PnL:    ${s['total_pnl']:+.2f}")
    print(f"  Record:       {s['wins']}W / {s['losses']}L ({s['total_trades']} total)")
    if s["total_trades"] > 0:
        wr = s["wins"] / s["total_trades"] * 100
        print(f"  Win rate:     {wr:.1f}%")
        print(f"  Avg PnL:      ${s['total_pnl']/s['total_trades']:+.2f}/trade")
        print(f"  Total fees:   ${s['total_fees']:.2f}")
        print(f"  Total wagered: ${s['total_wagered']:.2f}")
        if s["total_wagered"] > 0:
            roi = s["total_pnl"] / s["total_wagered"] * 100
            print(f"  ROI:          {roi:+.1f}%")

    if ledger["open_positions"]:
        print(f"\n  Open positions ({len(ledger['open_positions'])}):")
        for t in ledger["open_positions"]:
            print(f"    {t['side']:4s} @ {t['entry_price']:.3f} "
                  f"(fair={t['fair_value']:.3f}, edge={t['edge']:.1%}) | "
                  f"{t['market'][:45]}")

    recent = ledger["trades"][-10:]
    if recent:
        print(f"\n  Recent trades:")
        for t in reversed(recent):
            emoji = "✅" if t["outcome"] == "win" else "❌"
            print(f"    {emoji} {t['side']:4s} @ {t['entry_price']:.3f} "
                  f"→ ${t['pnl']:+.2f} | edge={t['edge']:.1%} | "
                  f"BTC Δ{t['btc_delta']:+,.0f} | {t['market'][:40]}")


def main():
    parser = argparse.ArgumentParser(description="Polymarket 5-Min BTC Sniper")
    parser.add_argument("--dry-run", action="store_true", help="Log signals, don't trade")
    parser.add_argument("--stats", action="store_true", help="Show paper trading stats")
    parser.add_argument("--size", type=float, default=25.0, help="Position size ($)")
    parser.add_argument("--edge", type=float, default=0.05, help="Minimum edge to trade (0.05 = 5%%)")
    parser.add_argument("--min-time", type=int, default=15, help="Min seconds remaining")
    parser.add_argument("--max-time", type=int, default=240, help="Max seconds remaining")
    parser.add_argument("--poll", action="store_true", help="Force polling mode (no WebSocket)")
    parser.add_argument("--live", action="store_true", help="Real trading (NOT IMPLEMENTED YET)")
    args = parser.parse_args()

    if args.stats:
        show_stats()
        return

    if args.live:
        print("❌ Live trading not implemented yet. Run without --live for paper trading.")
        print("   Next step: configure wallet in .polymarket-creds.json and install py-clob-client")
        return

    engine = SniperEngine(
        dry_run=args.dry_run,
        position_size=args.size,
        min_edge=args.edge,
        min_time=args.min_time,
        max_time=args.max_time,
    )
    engine._force_polling = args.poll

    def shutdown(sig, frame):
        print(f"\n\n  Shutting down... {engine.trade_count} trades this session.")
        show_stats()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    asyncio.run(run_ws(engine))


if __name__ == "__main__":
    main()
