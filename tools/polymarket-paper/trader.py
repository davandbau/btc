#!/usr/bin/env python3
"""
Polymarket Fast Market Paper Trader

Multi-strategy paper trading bot for BTC/ETH/SOL "Up or Down"
5-min and 15-min fast markets on Polymarket.

Strategies:
  momentum  — Buy the side matching Binance CEX price direction
  spread    — Buy both sides when combined cost < $1 (arb the spread)
  fade      — Fade overreactions: buy the cheap side when one side spikes

Paper trading only — no wallet or API keys required.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

# ─── Config ───────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "strategy": "momentum",
    "asset": "BTC",
    "window": "5m",
    "max_position": 25.0,            # $ per trade (per side for spread)
    "lookback_minutes": 5,
    "min_time_remaining": 45,
    "signal_source": "binance",

    # momentum strategy
    "entry_threshold": 0.04,         # min divergence from 50c
    "min_momentum_pct": 0.15,        # min BTC % move
    "volume_min_ratio": 0.3,

    # spread strategy
    "spread_max_combined": 0.96,     # max Up+Down price to enter (< $1 = profit)
    "spread_min_edge": 0.02,         # min $ profit after fees per $1 of shares
    "spread_size": 50.0,             # total $ across both sides

    # fade strategy
    "fade_min_divergence": 0.08,     # one side must be >= 8c from 50c
    "fade_momentum_cap": 0.10,       # only fade when momentum is WEAK (< this)
    "fade_size": 25.0,
}

LEDGER_DIR = Path(__file__).parent / "ledgers"
CONFIG_PATH = Path(__file__).parent / "config.json"

BINANCE_SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT"}
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

FEE_RATE = 0.25
FEE_EXPONENT = 2


# ─── HTTP ─────────────────────────────────────────────────────────────
def fetch_json(url, timeout=10):
    req = Request(url, headers={"User-Agent": "polymarket-paper-trader/2.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (URLError, json.JSONDecodeError) as e:
        print(f"  ✗ Fetch error {url[:60]}: {e}")
        return None


# ─── Ledger (per-strategy) ────────────────────────────────────────────
def ledger_path(strategy):
    LEDGER_DIR.mkdir(exist_ok=True)
    return LEDGER_DIR / f"{strategy}.json"

def load_ledger(strategy):
    p = ledger_path(strategy)
    if p.exists():
        ledger = json.loads(p.read_text())
        # Backfill fields missing from old ledger files
        if "strategy" not in ledger:
            ledger["strategy"] = strategy
        if "stats" not in ledger:
            ledger["stats"] = {"total_pnl": 0, "wins": 0, "losses": 0, "total_trades": 0,
                               "gross_profit": 0, "gross_loss": 0, "total_fees": 0}
        return ledger
    return {
        "strategy": strategy,
        "trades": [],
        "open_positions": [],
        "stats": {"total_pnl": 0, "wins": 0, "losses": 0, "total_trades": 0,
                  "gross_profit": 0, "gross_loss": 0, "total_fees": 0},
    }

def save_ledger(ledger):
    p = ledger_path(ledger["strategy"])
    p.write_text(json.dumps(ledger, indent=2))

def load_config():
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    return cfg


# ─── Market Discovery ────────────────────────────────────────────────
ASSET_NAMES = {"BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "XRP": "XRP"}

def discover_fast_markets(asset="BTC", window="5m"):
    url = f"{GAMMA_BASE}/markets?limit=50&active=true&closed=false&order=createdAt&ascending=false"
    data = fetch_json(url)
    if not data:
        return []

    now = datetime.now(timezone.utc)
    markets = []
    target_name = ASSET_NAMES.get(asset, asset)

    for m in data:
        q = m.get("question", "")
        slug = m.get("slug", "")
        if f"{target_name} Up or Down" not in q:
            continue
        if window == "5m" and "15m" in slug:
            continue
        if window == "15m" and "15m" not in slug:
            continue

        end_str = m.get("endDate", "")
        if not end_str:
            continue
        end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        remaining = (end - now).total_seconds()
        if remaining <= 0 or m.get("closed"):
            continue

        tokens = json.loads(m.get("clobTokenIds", "[]"))
        if len(tokens) < 2:
            continue
        prices = json.loads(m.get("outcomePrices", "[]"))

        markets.append({
            "question": q,
            "slug": slug,
            "end_date": end_str,
            "remaining_seconds": remaining,
            "token_up": tokens[0],
            "token_down": tokens[1],
            "price_up": float(prices[0]) if prices else 0.5,
            "price_down": float(prices[1]) if len(prices) > 1 else 0.5,
            "volume": float(m.get("volume", 0)),
            "condition_id": m.get("conditionId", ""),
        })

    markets.sort(key=lambda x: x["remaining_seconds"])
    return markets


# ─── Order Book ───────────────────────────────────────────────────────
def get_order_book(token_id):
    return fetch_json(f"{CLOB_BASE}/book?token_id={token_id}")

def get_best_price(book, side="asks"):
    orders = book.get(side, [])
    if not orders:
        return None
    prices = [float(o["price"]) for o in orders]
    return min(prices) if side == "asks" else max(prices)

def get_book_depth(book, side, levels=5):
    """Return list of (price, size) tuples for top N levels."""
    orders = book.get(side, [])
    parsed = [(float(o["price"]), float(o["size"])) for o in orders]
    parsed.sort(key=lambda x: x[0], reverse=(side == "bids"))
    return parsed[:levels]

def get_fillable_size(book, side, budget):
    """How many shares can we buy for $budget walking the book."""
    orders = book.get(side, [])
    parsed = [(float(o["price"]), float(o["size"])) for o in orders]
    parsed.sort(key=lambda x: x[0])  # cheapest first for asks

    total_shares = 0
    total_cost = 0
    avg_price = 0
    for price, size in parsed:
        cost_this_level = price * size
        if total_cost + cost_this_level <= budget:
            total_shares += size
            total_cost += cost_this_level
        else:
            remaining = budget - total_cost
            partial_shares = remaining / price
            total_shares += partial_shares
            total_cost += remaining
            break

    avg_price = total_cost / total_shares if total_shares > 0 else 0
    return {"shares": total_shares, "cost": total_cost, "avg_price": avg_price}


# ─── Binance Signal ──────────────────────────────────────────────────
def get_binance_signal(asset="BTC", lookback_minutes=5):
    symbol = BINANCE_SYMBOLS.get(asset, f"{asset}USDT")
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1m&limit={lookback_minutes + 1}"
    data = fetch_json(url)
    if not data or len(data) < 2:
        return None

    oldest_close = float(data[0][4])
    latest_close = float(data[-1][4])
    volumes = [float(k[5]) for k in data]
    avg_vol = sum(volumes[:-1]) / max(len(volumes) - 1, 1)
    latest_vol = volumes[-1]

    momentum_pct = ((latest_close - oldest_close) / oldest_close) * 100

    return {
        "price": latest_close,
        "price_old": oldest_close,
        "momentum_pct": momentum_pct,
        "abs_momentum_pct": abs(momentum_pct),
        "direction": "up" if momentum_pct > 0 else "down",
        "vol_ratio": latest_vol / avg_vol if avg_vol > 0 else 1.0,
    }


# ─── Fees ─────────────────────────────────────────────────────────────
def calc_fee(shares, price):
    return shares * FEE_RATE * (price * (1 - price)) ** FEE_EXPONENT

def effective_fee_pct(price):
    if price <= 0 or price >= 1:
        return 0
    return FEE_RATE * (price * (1 - price)) ** FEE_EXPONENT / price * 100


# ─── Strategy: MOMENTUM ──────────────────────────────────────────────
def strategy_momentum(market, signal, cfg):
    """Buy the side matching Binance momentum direction."""
    direction = signal["direction"]
    momentum = signal["abs_momentum_pct"]
    vol_ratio = signal["vol_ratio"]

    side = "Up" if direction == "up" else "Down"
    token = market["token_up"] if direction == "up" else market["token_down"]

    # Get real entry price from order book
    book = get_order_book(token)
    entry_price = market[f"price_{direction}"]
    if book:
        best_ask = get_best_price(book, "asks")
        if best_ask:
            entry_price = best_ask

    # Thresholds
    divergence = abs(entry_price - 0.50)
    if divergence < cfg["entry_threshold"]:
        return None, f"Divergence {divergence:.3f} < {cfg['entry_threshold']}"
    if momentum < cfg["min_momentum_pct"]:
        return None, f"Momentum {momentum:.3f}% < {cfg['min_momentum_pct']}%"
    if vol_ratio < cfg["volume_min_ratio"]:
        return None, f"Vol ratio {vol_ratio:.2f}x < {cfg['volume_min_ratio']}x"

    # Fair value estimate
    shift = min(momentum * 0.15 * min(vol_ratio, 2.0), 0.35)
    fair_value = 0.50 + shift

    shares = cfg["max_position"] / entry_price
    fee = calc_fee(shares, entry_price)
    gross_ev = fair_value * (1 - entry_price) * shares - (1 - fair_value) * entry_price * shares
    net_ev = gross_ev - fee

    if net_ev <= 0:
        return None, f"Negative EV: ${net_ev:.4f} (fair={fair_value:.3f}, entry={entry_price:.3f})"

    return [{
        "side": side,
        "entry_price": entry_price,
        "shares": shares,
        "cost": cfg["max_position"],
        "fee": fee,
        "fair_value": fair_value,
        "net_ev": net_ev,
        "reason": f"momentum {signal['momentum_pct']:+.3f}%, fair={fair_value:.3f}",
    }], None


# ─── Strategy: SPREAD ─────────────────────────────────────────────────
def strategy_spread(market, signal, cfg):
    """
    Buy BOTH sides when combined ask price < $1.
    Guaranteed profit on resolution if Up_ask + Down_ask < 1.00 (minus fees).
    This is what the k9Q2 account appears to be doing.
    """
    book_up = get_order_book(market["token_up"])
    book_down = get_order_book(market["token_down"])
    if not book_up or not book_down:
        return None, "Could not fetch order books"

    best_ask_up = get_best_price(book_up, "asks")
    best_ask_down = get_best_price(book_down, "asks")
    if not best_ask_up or not best_ask_down:
        return None, "No asks available on one or both sides"

    combined = best_ask_up + best_ask_down
    if combined >= cfg["spread_max_combined"]:
        return None, f"Combined {combined:.4f} >= max {cfg['spread_max_combined']} (no arb)"

    # Calculate profit per share pair
    # Buy 1 Up share + 1 Down share for $combined, payout = $1 always
    raw_profit_per_pair = 1.0 - combined

    # How many share pairs can we buy?
    half_budget = cfg["spread_size"] / 2
    fill_up = get_fillable_size(book_up, "asks", half_budget)
    fill_down = get_fillable_size(book_down, "asks", half_budget)

    # Use the smaller fill to keep pairs balanced
    pair_shares = min(fill_up["shares"], fill_down["shares"])
    if pair_shares < 1:
        return None, "Insufficient liquidity for spread"

    cost_up = fill_up["avg_price"] * pair_shares
    cost_down = fill_down["avg_price"] * pair_shares
    total_cost = cost_up + cost_down

    fee_up = calc_fee(pair_shares, fill_up["avg_price"])
    fee_down = calc_fee(pair_shares, fill_down["avg_price"])
    total_fee = fee_up + fee_down

    gross_profit = pair_shares * 1.0 - total_cost  # payout - cost
    net_profit = gross_profit - total_fee

    if net_profit < cfg["spread_min_edge"] * pair_shares:
        return None, f"Net profit ${net_profit:.4f} < min edge (${cfg['spread_min_edge'] * pair_shares:.4f})"

    trades = [
        {
            "side": "Up",
            "entry_price": fill_up["avg_price"],
            "shares": pair_shares,
            "cost": cost_up,
            "fee": fee_up,
            "fair_value": None,
            "net_ev": net_profit / 2,
            "reason": f"spread arb: combined={combined:.4f}, net=${net_profit:.4f}",
        },
        {
            "side": "Down",
            "entry_price": fill_down["avg_price"],
            "shares": pair_shares,
            "cost": cost_down,
            "fee": fee_down,
            "fair_value": None,
            "net_ev": net_profit / 2,
            "reason": f"spread arb: combined={combined:.4f}, net=${net_profit:.4f}",
        },
    ]
    return trades, None


# ─── Strategy: FADE ───────────────────────────────────────────────────
def strategy_fade(market, signal, cfg):
    """
    Fade overreactions: when one side has spiked but Binance momentum
    is weak, bet on mean reversion by buying the cheap side.
    """
    momentum = signal["abs_momentum_pct"]

    # Only fade when momentum is WEAK — the market overreacted
    if momentum > cfg["fade_momentum_cap"]:
        return None, f"Momentum {momentum:.3f}% > fade cap {cfg['fade_momentum_cap']}% (not fading strong moves)"

    # Find the cheap side
    if market["price_up"] < market["price_down"]:
        cheap_side, cheap_price, token = "Up", market["price_up"], market["token_up"]
    else:
        cheap_side, cheap_price, token = "Down", market["price_down"], market["token_down"]

    divergence = 0.5 - cheap_price
    if divergence < cfg["fade_min_divergence"]:
        return None, f"Divergence {divergence:.3f} < fade threshold {cfg['fade_min_divergence']}"

    # Get real price from book
    book = get_order_book(token)
    entry_price = cheap_price
    if book:
        best_ask = get_best_price(book, "asks")
        if best_ask:
            entry_price = best_ask

    # Fair value: since momentum is weak, assume ~50/50
    fair_value = 0.50

    shares = cfg["fade_size"] / entry_price
    fee = calc_fee(shares, entry_price)
    gross_ev = fair_value * (1 - entry_price) * shares - (1 - fair_value) * entry_price * shares
    net_ev = gross_ev - fee

    if net_ev <= 0:
        return None, f"Negative EV even at 50/50: ${net_ev:.4f}"

    return [{
        "side": cheap_side,
        "entry_price": entry_price,
        "shares": shares,
        "cost": cfg["fade_size"],
        "fee": fee,
        "fair_value": fair_value,
        "net_ev": net_ev,
        "reason": f"fade: cheap={cheap_side} @ {entry_price:.3f}, momentum only {momentum:.3f}%",
    }], None


STRATEGIES = {
    "momentum": strategy_momentum,
    "spread": strategy_spread,
    "fade": strategy_fade,
}


# ─── Resolution ───────────────────────────────────────────────────────
def resolve_open_positions(ledger):
    now = datetime.now(timezone.utc)
    resolved_count = 0

    for trade in ledger["open_positions"]:
        if trade.get("resolved"):
            continue

        end = datetime.fromisoformat(trade["market_end"].replace("Z", "+00:00"))
        if now < end + timedelta(minutes=2):
            continue

        data = fetch_json(f"{GAMMA_BASE}/markets?slug={trade['slug']}")
        if not data or len(data) == 0:
            continue
        market = data[0]
        if not market.get("closed"):
            continue

        prices = json.loads(market.get("outcomePrices", "[]"))
        if not prices:
            continue

        resolved_up = float(prices[0]) > 0.9
        our_side = trade["side"]
        won = (our_side == "Up" and resolved_up) or (our_side == "Down" and not resolved_up)

        if won:
            pnl = (1.0 - trade["entry_price"]) * trade["shares"] - trade["fee"]
        else:
            pnl = -(trade["entry_price"] * trade["shares"] + trade["fee"])

        trade["resolved"] = True
        trade["outcome"] = "win" if won else "loss"
        trade["pnl"] = round(pnl, 4)
        trade["resolved_at"] = now.isoformat()
        trade["market_result"] = "Up" if resolved_up else "Down"

        ledger["stats"]["total_pnl"] += pnl
        ledger["stats"]["total_trades"] += 1
        ledger["stats"]["total_fees"] += trade["fee"]
        if won:
            ledger["stats"]["wins"] += 1
            ledger["stats"]["gross_profit"] += pnl + trade["fee"]
        else:
            ledger["stats"]["losses"] += 1
            ledger["stats"]["gross_loss"] += abs(pnl) - trade["fee"]

        ledger["trades"].append(trade)
        resolved_count += 1

        emoji = "✅" if won else "❌"
        print(f"  {emoji} {trade['market'][:50]} → {trade['market_result']}")
        print(f"     {our_side} @ {trade['entry_price']:.3f} → PnL: ${pnl:+.4f}")

    ledger["open_positions"] = [t for t in ledger["open_positions"] if not t.get("resolved")]
    return resolved_count


# ─── Main Cycle ───────────────────────────────────────────────────────
def run_cycle(cfg):
    strategy = cfg["strategy"]
    asset = cfg["asset"]
    window = cfg["window"]
    strategy_fn = STRATEGIES.get(strategy)

    if not strategy_fn:
        print(f"✗ Unknown strategy: {strategy}. Available: {', '.join(STRATEGIES.keys())}")
        return

    print(f"\n{'='*60}")
    print(f"⚡ Paper Trader — {strategy.upper()} — {asset} {window}")
    print(f"   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*60}")

    ledger = load_ledger(strategy)

    # Resolve
    resolved = resolve_open_positions(ledger)
    if resolved:
        print(f"\n📊 Resolved {resolved} position(s)")
        save_ledger(ledger)

    # Discover
    print(f"\n🔍 Discovering {asset} fast markets...")
    markets = discover_fast_markets(asset, window)
    eligible = [m for m in markets if m["remaining_seconds"] >= cfg["min_time_remaining"]]
    print(f"  Found {len(markets)} total, {len(eligible)} eligible")

    if not eligible:
        print("  No eligible markets")
        save_ledger(ledger)
        return

    # Pick market: most divergence for momentum/fade, soonest for spread
    if strategy in ("momentum", "fade"):
        eligible.sort(key=lambda m: abs(m["price_up"] - 0.5), reverse=True)
    else:
        eligible.sort(key=lambda m: m["price_up"] + m["price_down"])  # lowest combined first

    target = eligible[0]
    print(f"\n🎯 {target['question']}")
    print(f"   {target['remaining_seconds']:.0f}s left | Up={target['price_up']:.3f} Down={target['price_down']:.3f} | Vol=${target['volume']:.2f}")

    # Skip if already positioned
    open_slugs = {t["slug"] for t in ledger["open_positions"]}
    if target["slug"] in open_slugs:
        print("  ⏭️ Already positioned")
        save_ledger(ledger)
        return

    # Get signal (all strategies use it, even if just for logging)
    print(f"\n📈 Binance {asset}...")
    signal = get_binance_signal(asset, cfg["lookback_minutes"])
    if not signal:
        print("  ✗ No price data")
        save_ledger(ledger)
        return

    arrow = "↑" if signal["direction"] == "up" else "↓"
    print(f"   ${signal['price']:,.2f} | {arrow} {signal['momentum_pct']:+.4f}% | vol {signal['vol_ratio']:.2f}x")

    # Run strategy
    print(f"\n🧠 Strategy: {strategy}...")
    trades, reason = strategy_fn(target, signal, cfg)

    if trades is None:
        print(f"  ⏸️ Skip: {reason}")
    else:
        for t in trades:
            print(f"  🎲 Buy {t['side']} @ {t['entry_price']:.3f} × {t['shares']:.1f} shares (${t['cost']:.2f})")
            print(f"     Fee: ${t['fee']:.4f} ({effective_fee_pct(t['entry_price']):.3f}%) | EV: ${t['net_ev']:+.4f}")
            print(f"     {t['reason']}")

            # Store as open position
            ledger["open_positions"].append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "strategy": strategy,
                "market": target["question"],
                "slug": target["slug"],
                "market_end": target["end_date"],
                "remaining_seconds": target["remaining_seconds"],
                "side": t["side"],
                "entry_price": t["entry_price"],
                "shares": t["shares"],
                "cost": t["cost"],
                "fee": t["fee"],
                "fair_value": t["fair_value"],
                "net_ev": t["net_ev"],
                "reason": t["reason"],
                "btc_price": signal["price"],
                "momentum_pct": signal["momentum_pct"],
                "resolved": False,
                "outcome": None,
                "pnl": None,
            })

    # Stats
    s = ledger["stats"]
    print(f"\n📊 {strategy.upper()} Stats:")
    print(f"   P&L: ${s['total_pnl']:+.4f} | {s['wins']}W/{s['losses']}L ({s['total_trades']} trades)")
    if s["total_trades"] > 0:
        print(f"   Win rate: {s['wins']/s['total_trades']*100:.1f}% | Avg: ${s['total_pnl']/s['total_trades']:+.4f}/trade")
        print(f"   Fees paid: ${s['total_fees']:.4f}")
    print(f"   Open: {len(ledger['open_positions'])}")

    save_ledger(ledger)


# ─── Stats ────────────────────────────────────────────────────────────
def show_stats(strategy=None):
    strategies = [strategy] if strategy else list(STRATEGIES.keys())

    print(f"\n{'='*60}")
    print(f"📊 Paper Trading Statistics")
    print(f"{'='*60}")

    for strat in strategies:
        ledger = load_ledger(strat)
        resolved = resolve_open_positions(ledger)
        if resolved:
            save_ledger(ledger)

        s = ledger["stats"]
        if s["total_trades"] == 0 and len(ledger["open_positions"]) == 0:
            continue

        print(f"\n  ── {strat.upper()} ──")
        print(f"  P&L:        ${s['total_pnl']:+.4f}")
        print(f"  Record:     {s['wins']}W / {s['losses']}L ({s['total_trades']} trades)")
        if s["total_trades"] > 0:
            print(f"  Win rate:   {s['wins']/s['total_trades']*100:.1f}%")
            print(f"  Avg/trade:  ${s['total_pnl']/s['total_trades']:+.4f}")
            print(f"  Fees:       ${s['total_fees']:.4f}")

        if ledger["open_positions"]:
            print(f"  Open ({len(ledger['open_positions'])}):")
            for t in ledger["open_positions"]:
                print(f"    {t['side']:4s} @ {t['entry_price']:.3f} | {t['market'][:45]}...")

        recent = ledger["trades"][-5:]
        if recent:
            print(f"  Recent:")
            for t in reversed(recent):
                e = "✅" if t["outcome"] == "win" else "❌"
                print(f"    {e} {t['side']:4s} @ {t['entry_price']:.3f} → ${t['pnl']:+.4f} | {t['market'][:40]}...")

    # Cross-strategy summary
    if len(strategies) > 1:
        total_pnl = 0
        total_trades = 0
        for strat in strategies:
            l = load_ledger(strat)
            total_pnl += l["stats"]["total_pnl"]
            total_trades += l["stats"]["total_trades"]
        if total_trades > 0:
            print(f"\n  ── COMBINED ──")
            print(f"  Total P&L:  ${total_pnl:+.4f} across {total_trades} trades")


# ─── CLI ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Polymarket Fast Market Paper Trader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
strategies:
  momentum  Buy side matching Binance CEX price direction
  spread    Buy both sides when combined cost < $1 (arb)
  fade      Buy cheap side when market overreacts vs weak momentum

examples:
  %(prog)s --strategy momentum
  %(prog)s --strategy spread --watch
  %(prog)s --strategy fade --asset ETH
  %(prog)s --stats                       # all strategies
  %(prog)s --stats --strategy momentum   # one strategy
""")
    parser.add_argument("--strategy", "-s", default=None, help="Strategy (momentum|spread|fade)")
    parser.add_argument("--watch", action="store_true", help="Run continuously")
    parser.add_argument("--stats", action="store_true", help="Show P&L statistics")
    parser.add_argument("--asset", default=None, help="Asset (BTC, ETH, SOL, XRP)")
    parser.add_argument("--window", default=None, help="Window (5m or 15m)")
    parser.add_argument("--size", type=float, default=None, help="Position size ($)")
    parser.add_argument("--interval", type=int, default=60, help="Watch interval (seconds)")
    # Strategy-specific overrides
    parser.add_argument("--threshold", type=float, default=None, help="[momentum] entry threshold")
    parser.add_argument("--momentum", type=float, default=None, help="[momentum] min momentum %")
    parser.add_argument("--max-combined", type=float, default=None, help="[spread] max combined price")
    parser.add_argument("--min-edge", type=float, default=None, help="[spread] min edge per share")
    parser.add_argument("--fade-div", type=float, default=None, help="[fade] min divergence")
    parser.add_argument("--fade-cap", type=float, default=None, help="[fade] max momentum to fade")

    args = parser.parse_args()
    cfg = load_config()

    # Apply CLI overrides
    if args.strategy:
        cfg["strategy"] = args.strategy
    if args.asset:
        cfg["asset"] = args.asset.upper()
    if args.window:
        cfg["window"] = args.window
    if args.size:
        cfg["max_position"] = args.size
        cfg["spread_size"] = args.size
        cfg["fade_size"] = args.size
    if args.threshold:
        cfg["entry_threshold"] = args.threshold
    if args.momentum:
        cfg["min_momentum_pct"] = args.momentum
    if args.max_combined is not None:
        cfg["spread_max_combined"] = args.max_combined
    if args.min_edge is not None:
        cfg["spread_min_edge"] = args.min_edge
    if args.fade_div is not None:
        cfg["fade_min_divergence"] = args.fade_div
    if args.fade_cap is not None:
        cfg["fade_momentum_cap"] = args.fade_cap

    if args.stats:
        show_stats(args.strategy)
        return

    if not cfg.get("strategy"):
        print("✗ Specify --strategy (momentum|spread|fade)")
        return

    if args.watch:
        print(f"🔄 Watch: {cfg['strategy']} every {args.interval}s (Ctrl+C to stop)")
        while True:
            try:
                run_cycle(cfg)
                time.sleep(args.interval)
            except KeyboardInterrupt:
                print("\n👋 Stopped.")
                show_stats(cfg["strategy"])
                return
    else:
        run_cycle(cfg)


if __name__ == "__main__":
    main()
