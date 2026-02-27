#!/usr/bin/env python3
"""
Polymarket News Trading Bot (Paper)

Monitors breaking news via Brave Search, matches to open Polymarket
markets, uses LLM reasoning to estimate probability shifts, and
paper trades when edge exceeds threshold.

Architecture:
  1. Fetch top active Polymarket markets (cache refreshed every 10 min)
  2. Search for breaking news related to those markets
  3. LLM evaluates: does this news shift the probability?
  4. If shift > threshold and market price hasn't adjusted → paper trade
  5. Track P&L in ledger (shared resolution engine with trader.py)

No API keys needed — uses Brave Search (via OpenClaw tool) and
Polymarket public APIs.
"""

import argparse
import json
import hashlib
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

# ─── Paths ────────────────────────────────────────────────────────────
BOT_DIR = Path(__file__).parent
LEDGER_DIR = BOT_DIR / "ledgers"
STATE_PATH = BOT_DIR / "newsbot_state.json"
MARKETS_CACHE_PATH = BOT_DIR / "markets_cache.json"

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# Fee: crypto fast markets have fees; event markets are mostly 0%
FEE_RATE_EVENT = 0.0   # most event markets are fee-free
FEE_RATE_CRYPTO = 0.25
FEE_EXPONENT = 2

# ─── Config ───────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "min_edge": 0.08,              # min probability shift to trade (8%)
    "max_position": 50.0,          # $ per trade
    "min_volume_24h": 10000,       # skip illiquid markets
    "max_markets": 30,             # top N markets to monitor
    "market_cache_ttl": 600,       # seconds before refreshing market list
    "news_lookback_hours": 2,      # how far back to search for news
    "seen_ttl_hours": 6,           # how long to remember seen news
    "categories": ["politics", "crypto", "geopolitics", "economics", "tech"],
}


# ─── HTTP ─────────────────────────────────────────────────────────────
def fetch_json(url, timeout=10):
    req = Request(url, headers={"User-Agent": "polymarket-newsbot/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (URLError, json.JSONDecodeError) as e:
        return None


# ─── State Management ─────────────────────────────────────────────────
def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"seen_news": {}, "last_market_refresh": 0, "signals_generated": 0, "trades_placed": 0}

def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2))

def load_ledger():
    LEDGER_DIR.mkdir(exist_ok=True)
    p = LEDGER_DIR / "newsbot.json"
    if p.exists():
        return json.loads(p.read_text())
    return {
        "strategy": "newsbot",
        "trades": [],
        "open_positions": [],
        "stats": {"total_pnl": 0, "wins": 0, "losses": 0, "total_trades": 0,
                  "gross_profit": 0, "gross_loss": 0, "total_fees": 0},
    }

def save_ledger(ledger):
    LEDGER_DIR.mkdir(exist_ok=True)
    p = LEDGER_DIR / "newsbot.json"
    p.write_text(json.dumps(ledger, indent=2))


# ─── Market Discovery ────────────────────────────────────────────────
def fetch_active_markets(cfg):
    """Fetch top active Polymarket events by 24h volume."""
    url = (f"{GAMMA_BASE}/events?limit={cfg['max_markets']}&active=true&closed=false"
           f"&order=volume24hr&ascending=false")
    data = fetch_json(url)
    if not data:
        return []

    markets = []
    for event in data:
        vol24 = event.get("volume24hr", 0) or 0
        if vol24 < cfg["min_volume_24h"]:
            continue

        title = event.get("title", "")
        event_id = event.get("id", "")
        event_slug = event.get("slug", "")

        # For multi-market events, pick the highest-volume active market
        # For single-market events, just use it
        active_markets = [m for m in event.get("markets", [])
                         if not m.get("closed") and m.get("active")]
        if not active_markets:
            continue

        # Pick best market (most volume, or first)
        best = max(active_markets, key=lambda m: float(m.get("volume", 0) or 0))

        prices = json.loads(best.get("outcomePrices", "[]"))
        outcomes = json.loads(best.get("outcomes", "[]"))
        tokens = json.loads(best.get("clobTokenIds", "[]"))

        if len(prices) < 2 or len(tokens) < 2:
            continue

        markets.append({
            "event_id": event_id,
            "event_title": title,
            "event_slug": event_slug,
            "market_id": best.get("id", ""),
            "question": best.get("question", title),
            "slug": best.get("slug", ""),
            "description": best.get("description", "")[:500],
            "outcomes": outcomes,
            "prices": [float(p) for p in prices],
            "tokens": tokens,
            "volume_24h": vol24,
            "end_date": best.get("endDate", ""),
            "enable_order_book": best.get("enableOrderBook", False),
            "fees_enabled": best.get("feesEnabled", False),
            "num_markets": len(active_markets),
        })

    return markets


def load_or_refresh_markets(cfg, state):
    """Load markets from cache or refresh if stale."""
    now = time.time()
    if now - state.get("last_market_refresh", 0) < cfg["market_cache_ttl"]:
        if MARKETS_CACHE_PATH.exists():
            return json.loads(MARKETS_CACHE_PATH.read_text())

    print("  Refreshing market list...")
    markets = fetch_active_markets(cfg)
    if markets:
        MARKETS_CACHE_PATH.write_text(json.dumps(markets, indent=2))
        state["last_market_refresh"] = now
        save_state(state)
    return markets


# ─── News Search ──────────────────────────────────────────────────────
def search_news_brave(query, hours_back=2):
    """Search for recent news using Brave Search API (via web_search tool).
    Since we can't call the OpenClaw tool from Python directly,
    we use Brave's public search with freshness filter.
    """
    # Use Brave's public search API endpoint
    from urllib.parse import quote
    freshness = "pd"  # past day
    url = f"https://api.search.brave.com/res/v1/news/search?q={quote(query)}&freshness={freshness}&count=5"

    # We don't have the Brave API key in this script, so we'll use
    # a simpler approach: RSS feeds + direct news site scraping
    # For now, return empty and let the cron job use web_search tool
    return []


def generate_search_queries(markets):
    """Generate targeted news search queries from market titles."""
    queries = []
    for m in markets:
        title = m["event_title"]
        # Extract key entities/topics from market title
        # Skip crypto fast markets (handled by trader.py)
        if "Up or Down" in title:
            continue

        # Clean up title for search
        query = title.replace("?", "").replace("...", "").strip()
        # Add "latest news" to make it more news-focused
        queries.append({
            "query": f"{query} latest news",
            "market": m,
        })

    return queries


# ─── LLM Analysis Prompt ─────────────────────────────────────────────
def build_analysis_prompt(market, news_items):
    """Build prompt for LLM to analyze news impact on market."""
    outcomes = market["outcomes"]
    prices = market["prices"]

    price_str = ", ".join(f'{o}: {p*100:.1f}%' for o, p in zip(outcomes, prices))

    news_text = "\n".join(f"- [{n['source']}] {n['title']}: {n['snippet']}"
                          for n in news_items[:5])

    return f"""Analyze how this breaking news affects this prediction market.

MARKET: {market['question']}
CURRENT PRICES: {price_str}
DESCRIPTION: {market['description'][:300]}

RECENT NEWS:
{news_text}

Instructions:
1. Does this news materially change the probability of any outcome?
2. If yes, what should the new probability be? (be specific, give a number)
3. How confident are you? (low/medium/high)
4. Is the market likely to have already priced this in?

Respond in JSON:
{{
  "news_relevant": true/false,
  "affected_outcome": "outcome name or null",
  "current_prob": 0.XX,
  "estimated_prob": 0.XX,
  "shift": 0.XX,
  "confidence": "low/medium/high",
  "already_priced_in": true/false,
  "reasoning": "one sentence explanation",
  "trade_recommendation": "buy_yes/buy_no/no_trade"
}}"""


# ─── Signal Generation (standalone mode) ──────────────────────────────
def generate_signals_from_news(markets, news_data, cfg, state):
    """
    Match news to markets and generate trading signals.
    In cron mode, the LLM analysis happens in the agent session.
    In standalone mode, this outputs signals for manual review.
    """
    signals = []
    now = datetime.now(timezone.utc)

    for item in news_data:
        news_hash = hashlib.md5(item["title"].encode()).hexdigest()[:12]

        # Skip if already seen
        if news_hash in state.get("seen_news", {}):
            continue

        # Find matching markets
        title_lower = item["title"].lower()
        for m in markets:
            market_title_lower = m["event_title"].lower()
            question_lower = m["question"].lower()

            # Simple keyword matching (LLM does the heavy lifting later)
            keywords = set(market_title_lower.split()) | set(question_lower.split())
            # Remove common words
            keywords -= {"the", "a", "an", "is", "are", "will", "be", "in", "on",
                        "of", "to", "and", "or", "by", "for", "at", "with", "from",
                        "?", "...", "this", "that", "what", "who", "how", "when"}

            # Need at least 2 keyword matches
            matches = sum(1 for kw in keywords if len(kw) > 3 and kw in title_lower)
            if matches < 2:
                continue

            signals.append({
                "news": item,
                "news_hash": news_hash,
                "market": m,
                "keyword_matches": matches,
                "timestamp": now.isoformat(),
            })

        # Mark as seen
        state["seen_news"][news_hash] = now.isoformat()

    # Clean old seen entries
    cutoff = (now - timedelta(hours=cfg["seen_ttl_hours"])).isoformat()
    state["seen_news"] = {k: v for k, v in state["seen_news"].items() if v > cutoff}

    return signals


# ─── Paper Trade Execution ────────────────────────────────────────────
def execute_paper_trade(market, side, estimated_prob, cfg):
    """Simulate a paper trade on a Polymarket event market."""
    # Determine which outcome to buy
    outcomes = market["outcomes"]
    prices = market["prices"]
    tokens = market["tokens"]

    if side == "buy_yes":
        idx = 0
    elif side == "buy_no":
        idx = 1 if len(outcomes) > 1 else 0
    else:
        return None

    entry_price = prices[idx]
    token = tokens[idx]
    outcome_name = outcomes[idx] if idx < len(outcomes) else side

    # Get real price from order book if available
    if market.get("enable_order_book"):
        book = fetch_json(f"{CLOB_BASE}/book?token_id={token}")
        if book:
            asks = book.get("asks", [])
            if asks:
                best_ask = min(float(a["price"]) for a in asks)
                entry_price = best_ask

    # Calculate position
    if entry_price <= 0 or entry_price >= 1:
        return None

    shares = cfg["max_position"] / entry_price
    fee_rate = FEE_RATE_CRYPTO if market.get("fees_enabled") else FEE_RATE_EVENT
    fee = shares * fee_rate * (entry_price * (1 - entry_price)) ** FEE_EXPONENT

    # EV calculation
    gross_ev = estimated_prob * (1 - entry_price) * shares - (1 - estimated_prob) * entry_price * shares
    net_ev = gross_ev - fee

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "strategy": "newsbot",
        "market": market["question"],
        "event_title": market["event_title"],
        "slug": market["slug"],
        "market_end": market["end_date"],
        "side": outcome_name,
        "entry_price": entry_price,
        "shares": shares,
        "cost": cfg["max_position"],
        "fee": fee,
        "fair_value": estimated_prob,
        "net_ev": net_ev,
        "reason": "",  # filled by caller
        "resolved": False,
        "outcome": None,
        "pnl": None,
    }


# ─── Resolution ───────────────────────────────────────────────────────
def resolve_positions(ledger):
    """Check and resolve completed positions."""
    now = datetime.now(timezone.utc)
    resolved = 0

    for trade in ledger["open_positions"]:
        if trade.get("resolved"):
            continue

        end_str = trade.get("market_end", "")
        if end_str:
            try:
                end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                if now < end + timedelta(minutes=5):
                    continue
            except:
                pass

        # Check via API
        slug = trade.get("slug", "")
        if not slug:
            continue

        data = fetch_json(f"{GAMMA_BASE}/markets?slug={slug}")
        if not data:
            continue

        market = data[0] if data else None
        if not market or not market.get("closed"):
            continue

        prices = json.loads(market.get("outcomePrices", "[]"))
        outcomes = json.loads(market.get("outcomes", "[]"))
        if not prices or not outcomes:
            continue

        # Find winning outcome
        winning_idx = max(range(len(prices)), key=lambda i: float(prices[i]))
        winning_outcome = outcomes[winning_idx] if winning_idx < len(outcomes) else "?"

        won = trade["side"] == winning_outcome

        if won:
            pnl = (1.0 - trade["entry_price"]) * trade["shares"] - trade["fee"]
        else:
            pnl = -(trade["entry_price"] * trade["shares"] + trade["fee"])

        trade["resolved"] = True
        trade["outcome"] = "win" if won else "loss"
        trade["pnl"] = round(pnl, 4)
        trade["resolved_at"] = now.isoformat()
        trade["market_result"] = winning_outcome

        ledger["stats"]["total_pnl"] += pnl
        ledger["stats"]["total_trades"] += 1
        ledger["stats"]["total_fees"] += trade["fee"]
        if won:
            ledger["stats"]["wins"] += 1
        else:
            ledger["stats"]["losses"] += 1

        ledger["trades"].append(trade)
        resolved += 1

        emoji = "W" if won else "L"
        print(f"  [{emoji}] {trade['market'][:50]} → {winning_outcome}, PnL: ${pnl:+.2f}")

    ledger["open_positions"] = [t for t in ledger["open_positions"] if not t.get("resolved")]
    return resolved


# ─── Main Cycle (for cron/agent use) ──────────────────────────────────
def run_scan(cfg=None):
    """
    Scan markets and output them for the agent to process.
    The agent (via cron) will:
    1. Run this script to get market+news pairs
    2. Use its own LLM to analyze each signal
    3. Call this script again with --trade to execute
    """
    if cfg is None:
        cfg = DEFAULT_CONFIG.copy()

    state = load_state()
    ledger = load_ledger()

    print(f"\n{'='*60}")
    print(f"📰 Polymarket News Bot — Scan")
    print(f"   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*60}")

    # Resolve any open positions
    resolved = resolve_positions(ledger)
    if resolved:
        print(f"\n📊 Resolved {resolved} position(s)")
        save_ledger(ledger)

    # Load/refresh markets
    print(f"\n🔍 Loading active markets...")
    markets = load_or_refresh_markets(cfg, state)
    # Filter out crypto fast markets (handled by trader.py)
    markets = [m for m in markets if "Up or Down" not in m["question"]]
    print(f"  {len(markets)} event markets tracked")

    if not markets:
        print("  No eligible markets")
        save_state(state)
        save_ledger(ledger)
        return {"markets": [], "signals": []}

    # Output market summary for agent
    print(f"\n📋 Top Markets by Volume:")
    for m in markets[:15]:
        prices_str = " | ".join(f"{o}:{p*100:.0f}%" for o, p in zip(m["outcomes"][:3], m["prices"][:3]))
        print(f"  ${m['volume_24h']:>12,.0f} | {prices_str:30s} | {m['event_title'][:45]}")

    # Cross-reference open positions against Kalshi
    if ledger["open_positions"]:
        print(f"\n📊 Kalshi Cross-Reference (open positions):")
        for pos in ledger["open_positions"]:
            if pos.get("resolved"):
                continue
            match = kalshi_cross_check(pos["market"], pos.get("side"))
            if match:
                entry = pos.get("entry_price", 0)
                div = match["kalshi_price"] - entry
                agrees = "✅ agrees" if div > 0 else "⚠️ disagrees"
                print(f"     vs entry {entry:.3f}: divergence {div:+.1%} {agrees}")
            else:
                print(f"     {pos['market'][:50]}: no Kalshi match")

    # Output search queries the agent should run
    queries = generate_search_queries(markets[:15])
    print(f"\n🔎 Suggested news searches ({len(queries)}):")
    for q in queries[:10]:
        print(f"  → {q['query'][:70]}")

    # Stats
    s = ledger["stats"]
    print(f"\n📊 News Bot Stats:")
    print(f"   P&L: ${s['total_pnl']:+.2f} | {s['wins']}W/{s['losses']}L ({s['total_trades']} trades)")
    print(f"   Open: {len(ledger['open_positions'])}")

    save_state(state)
    save_ledger(ledger)

    return {
        "markets": markets[:15],
        "queries": queries[:10],
    }


def kalshi_cross_check(question, side=None):
    """Cross-reference a Polymarket question against Kalshi prices."""
    try:
        from kalshi import KalshiFeed
        feed = KalshiFeed()
        match = feed.find_match(question, side)
        if match:
            print(f"  📊 Kalshi cross-ref: {match['kalshi_title'][:60]}")
            print(f"     Price: {match['kalshi_price']:.1%} (bid={match['kalshi_yes_bid']:.1%} ask={match['kalshi_yes_ask']:.1%})")
            print(f"     Volume: {match['kalshi_volume']:,} | Match score: {match['match_score']}")
        return match
    except Exception as e:
        print(f"  ⚠️ Kalshi cross-ref failed: {e}")
        return None


def execute_trade_cli(market_slug, side, estimated_prob, reason, cfg=None):
    """Execute a paper trade from CLI (called by agent after LLM analysis)."""
    if cfg is None:
        cfg = DEFAULT_CONFIG.copy()

    ledger = load_ledger()

    # Find market in cache
    if MARKETS_CACHE_PATH.exists():
        markets = json.loads(MARKETS_CACHE_PATH.read_text())
    else:
        markets = fetch_active_markets(cfg)

    market = None
    for m in markets:
        if m["slug"] == market_slug or m["event_slug"] == market_slug:
            market = m
            break

    if not market:
        print(f"✗ Market not found: {market_slug}")
        return False

    # Check for duplicate
    open_slugs = {t["slug"] for t in ledger["open_positions"]}
    if market["slug"] in open_slugs:
        print(f"⏭️ Already positioned in {market['slug']}")
        return False

    trade = execute_paper_trade(market, side, estimated_prob, cfg)
    if not trade:
        print(f"✗ Could not execute trade")
        return False

    trade["reason"] = reason
    trade["fair_value"] = estimated_prob

    ledger["open_positions"].append(trade)
    save_ledger(ledger)

    print(f"🎲 PAPER TRADE: {trade['side']} @ {trade['entry_price']:.3f}")
    print(f"   Market: {trade['market'][:60]}")
    print(f"   Shares: {trade['shares']:.1f} | Cost: ${trade['cost']:.2f} | EV: ${trade['net_ev']:+.2f}")
    print(f"   Reason: {reason}")

    return True


def show_stats():
    """Show news bot stats."""
    ledger = load_ledger()
    resolved = resolve_positions(ledger)
    if resolved:
        save_ledger(ledger)

    s = ledger["stats"]
    print(f"\n{'='*60}")
    print(f"📰 News Bot Statistics")
    print(f"{'='*60}")
    print(f"  P&L:        ${s['total_pnl']:+.2f}")
    print(f"  Record:     {s['wins']}W / {s['losses']}L ({s['total_trades']} trades)")
    if s["total_trades"] > 0:
        print(f"  Win rate:   {s['wins']/s['total_trades']*100:.1f}%")
        print(f"  Avg/trade:  ${s['total_pnl']/s['total_trades']:+.2f}")
    print(f"  Open:       {len(ledger['open_positions'])}")

    if ledger["open_positions"]:
        print(f"\n  Open Positions:")
        for t in ledger["open_positions"]:
            print(f"    {t['side']:6s} @ {t['entry_price']:.3f} | {t['market'][:50]}...")
            print(f"           EV: ${t.get('net_ev',0):+.2f} | {t.get('reason','')[:50]}")


# ─── CLI ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Polymarket News Trading Bot")
    parser.add_argument("--scan", action="store_true", help="Scan markets + generate search queries")
    parser.add_argument("--trade", nargs=4, metavar=("SLUG", "SIDE", "PROB", "REASON"),
                        help="Execute paper trade: slug buy_yes/buy_no prob reason")
    parser.add_argument("--stats", action="store_true", help="Show statistics")
    parser.add_argument("--markets", action="store_true", help="List tracked markets")
    parser.add_argument("--size", type=float, help="Position size ($)")
    parser.add_argument("--kalshi", type=str, help="Cross-check a question against Kalshi")

    args = parser.parse_args()
    cfg = DEFAULT_CONFIG.copy()
    if args.size:
        cfg["max_position"] = args.size

    if args.kalshi:
        match = kalshi_cross_check(args.kalshi)
        if not match:
            print("  No Kalshi match found")
    elif args.stats:
        show_stats()
    elif args.trade:
        slug, side, prob, reason = args.trade
        execute_trade_cli(slug, side, float(prob), reason, cfg)
    elif args.markets:
        state = load_state()
        markets = load_or_refresh_markets(cfg, state)
        markets = [m for m in markets if "Up or Down" not in m["question"]]
        for m in markets:
            prices_str = " | ".join(f"{o}:{p*100:.0f}%" for o, p in zip(m["outcomes"][:3], m["prices"][:3]))
            print(f"  {m['slug'][:40]:40s} | {prices_str:30s} | {m['event_title'][:40]}")
    else:
        run_scan(cfg)


if __name__ == "__main__":
    main()
