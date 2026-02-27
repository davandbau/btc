#!/usr/bin/env python3
"""
Multi-Outcome Mispricing Scanner for Polymarket
Finds events where outcome probabilities don't sum to 100%,
indicating arbitrage or value opportunities.

Usage:
  python3 mispricing.py --scan              # Find mispriced markets
  python3 mispricing.py --scan --min-gap 3  # Min gap % to show
  python3 mispricing.py --trade EVENT_SLUG SIDE AMOUNT "reason"
  python3 mispricing.py --stats             # Show paper trading stats
"""

import argparse, json, os, sys, time, datetime, math
from urllib.request import urlopen, Request
from urllib.error import URLError

GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"
LEDGER_PATH = os.path.join(os.path.dirname(__file__), "ledgers", "mispricing.json")

# --- Polymarket fee model ---
def calc_fee(price, amount=10.0):
    """Polymarket fee: C * 0.25 * (p*(1-p))^2 where C is contract count"""
    p = max(0.01, min(0.99, price))
    contracts = amount / p
    fee = contracts * 0.25 * (p * (1 - p)) ** 2
    return fee

# --- API helpers ---
def fetch_json(url):
    try:
        req = Request(url, headers={"User-Agent": "PolyMispricing/1.0"})
        with urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  [ERR] {url[:80]}: {e}", file=sys.stderr)
        return None

def fetch_events(limit=200):
    """Fetch all open events with multiple markets."""
    all_events = []
    for offset in range(0, limit, 50):
        data = fetch_json(f"{GAMMA}/events?closed=false&limit=50&offset={offset}")
        if not data:
            break
        all_events.extend(data)
        if len(data) < 50:
            break
    return all_events

def get_orderbook_midpoint(token_id):
    """Get midpoint price from CLOB orderbook."""
    data = fetch_json(f"{CLOB}/book?token_id={token_id}")
    if not data:
        return None
    bids = data.get("bids", [])
    asks = data.get("asks", [])
    best_bid = float(bids[0]["price"]) if bids else 0
    best_ask = float(asks[0]["price"]) if asks else 1
    if best_bid == 0 and best_ask == 1:
        return None
    return (best_bid + best_ask) / 2

# --- Classification ---
def classify_event(event):
    """
    Classify event type:
    - 'mutually_exclusive': outcomes are exclusive (should sum to ~1.0)
    - 'timeline': "by date X" markets (don't need to sum to 1.0)
    - 'independent': unrelated sub-markets
    """
    markets = event.get("markets", [])
    if len(markets) < 3:
        return "too_few"

    questions = [m.get("question", "").lower() for m in markets]

    title = event.get("title", "").lower()

    # Timeline detection: "by March", "by December", "in 2025", etc.
    timeline_keywords = ["by ", "in 2025", "in 2026", "in 2027", "before "]
    timeline_count = sum(1 for q in questions if any(k in q for k in timeline_keywords))
    if timeline_count > len(questions) * 0.6:
        return "timeline"

    # Independent: multiple things can be true simultaneously
    independent_keywords = [
        "top 4", "top four", "qualify", "advance", "relegated", "relegat",
        "before gta", "what will happen before",
        "which countries", "which teams", "which clubs",
        "above", "fdv above", "fdv one day", "market cap",  # threshold markets (>$500M, >$1B, etc.)
    ]
    if any(k in title for k in independent_keywords):
        return "independent"
    # Also check questions for threshold patterns
    threshold_count = sum(1 for q in questions if any(k in q for k in ["above", "more than", "at least", "advance", "qualify", "finish in the top", "relegate", "relegated"]))
    if threshold_count > len(questions) * 0.5:
        return "independent"

    # Check if outcomes look mutually exclusive
    # (different candidates/options for same question)
    return "mutually_exclusive"

# --- Scanning ---
def scan_markets(min_gap=1.5, use_clob=False, top_n=20):
    """
    Scan for mispriced multi-outcome markets.
    min_gap: minimum absolute gap % to flag
    use_clob: use live orderbook prices (slower but more accurate)
    """
    events = fetch_events()
    results = []

    for event in events:
        markets = event.get("markets", [])
        etype = classify_event(event)
        if etype != "mutually_exclusive":
            continue

        # Parse outcome prices
        outcomes = []
        for m in markets:
            raw = m.get("outcomePrices", "[]")
            prices = json.loads(raw) if isinstance(raw, str) else raw
            if not prices:
                continue
            try:
                yes_price = float(prices[0])
            except (ValueError, IndexError):
                continue

            # Optionally get live CLOB price
            if use_clob and m.get("clobTokenIds"):
                token_ids = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
                if token_ids:
                    mid = get_orderbook_midpoint(token_ids[0])
                    if mid is not None:
                        yes_price = mid

            outcomes.append({
                "question": m.get("question", ""),
                "slug": m.get("slug", ""),
                "yes_price": yes_price,
                "volume": float(m.get("volume", 0) or 0),
                "liquidity": float(m.get("liquidityNum", 0) or 0),
                "clob_token_ids": json.loads(m.get("clobTokenIds", "[]")) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds", []),
                "condition_id": m.get("conditionId", ""),
            })

        if len(outcomes) < 3:
            continue

        total_yes = sum(o["yes_price"] for o in outcomes)
        gap_pct = (1.0 - total_yes) * 100  # positive = underpriced, negative = overpriced
        total_volume = sum(o["volume"] for o in outcomes)
        total_liquidity = sum(o["liquidity"] for o in outcomes)

        if abs(gap_pct) < min_gap:
            continue

        # Skip very illiquid markets (< $10k total liquidity)
        if total_liquidity < 10000:
            continue

        # Skip absurdly mispriced (>100% gap) — likely classification error or dead markets
        if abs(gap_pct) > 100:
            continue

        # Score: gap size * liquidity factor
        liq_factor = min(1.0, total_liquidity / 50000) if total_liquidity > 0 else 0.1
        score = abs(gap_pct) * liq_factor

        results.append({
            "event": event.get("title", ""),
            "slug": event.get("slug", ""),
            "num_outcomes": len(outcomes),
            "sum_yes": total_yes,
            "gap_pct": gap_pct,
            "total_volume": total_volume,
            "total_liquidity": total_liquidity,
            "score": score,
            "outcomes": sorted(outcomes, key=lambda x: x["yes_price"], reverse=True),
            "type": "underpriced" if gap_pct > 0 else "overpriced",
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_n]

def print_scan(results):
    """Pretty-print scan results."""
    if not results:
        print("No mispriced markets found above threshold.")
        return

    print(f"\n{'='*70}")
    print(f"  MULTI-OUTCOME MISPRICING SCANNER")
    print(f"  {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Found {len(results)} mispriced events")
    print(f"{'='*70}\n")

    for i, r in enumerate(results):
        arrow = "📈" if r["gap_pct"] > 0 else "📉"
        print(f"{i+1}. {arrow} {r['event']}")
        print(f"   Outcomes: {r['num_outcomes']} | Sum(Yes): {r['sum_yes']:.4f} | Gap: {r['gap_pct']:+.2f}%")
        print(f"   Volume: ${r['total_volume']:,.0f} | Liquidity: ${r['total_liquidity']:,.0f} | Score: {r['score']:.2f}")
        print(f"   Type: {r['type'].upper()}")

        # Show top outcomes
        if r["gap_pct"] > 0:  # Underpriced - show cheapest (potential buys)
            print(f"   Cheapest outcomes (potential buys):")
            for o in sorted(r["outcomes"], key=lambda x: x["yes_price"])[:5]:
                if o["yes_price"] > 0:
                    print(f"     {o['question'][:55]:55s} Yes: {o['yes_price']:.4f}")
        else:  # Overpriced - show most expensive
            print(f"   Most expensive outcomes:")
            for o in r["outcomes"][:5]:
                print(f"     {o['question'][:55]:55s} Yes: {o['yes_price']:.4f}")
        print()

    # Trading suggestions
    print(f"{'='*70}")
    print("  TRADING SUGGESTIONS")
    print(f"{'='*70}\n")

    for r in results[:5]:
        if r["gap_pct"] > 2.0:  # Underpriced by >2%
            # Buy all outcomes strategy
            cost_per_set = r["sum_yes"]
            profit_per_set = 1.0 - cost_per_set
            fee_est = sum(calc_fee(o["yes_price"], 10) for o in r["outcomes"])
            net_profit = profit_per_set * 10 - fee_est  # per $10 deployed per outcome
            if net_profit > 0:
                print(f"  BUY ALL: {r['event']}")
                print(f"  Cost per set: ${cost_per_set:.4f} | Profit: ${profit_per_set:.4f} | Net (est): ${net_profit:.2f} per $10")
                print()
        elif r["gap_pct"] < -3.0:  # Overpriced by >3%
            # Identify the outcome most likely to resolve No
            cheapest = sorted(r["outcomes"], key=lambda x: x["yes_price"])
            print(f"  VALUE BUY: {r['event']}")
            print(f"  Market overpriced by {abs(r['gap_pct']):.1f}% — look for the most underpriced outcome:")
            for o in cheapest[:3]:
                if 0 < o["yes_price"] < 0.5:
                    implied = o["yes_price"] * 100
                    print(f"    {o['question'][:55]} @ {implied:.1f}% implied")
            print()

# --- Ledger & Trading ---
def load_ledger():
    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    if os.path.exists(LEDGER_PATH):
        with open(LEDGER_PATH) as f:
            return json.load(f)
    return {"trades": [], "open_positions": [], "sets": []}

def save_ledger(ledger):
    with open(LEDGER_PATH, "w") as f:
        json.dump(ledger, f, indent=2)

def place_trade(event_slug, side, amount, reason, price_override=None):
    """
    Paper trade on a mispriced market.
    side: 'buy_all' (buy all outcomes) or 'buy_SLUG' (buy specific outcome)
    """
    ledger = load_ledger()
    ts = datetime.datetime.utcnow().isoformat() + "Z"

    if side == "buy_all":
        # Buy one share of every outcome in the event
        events = fetch_events()
        target = None
        for e in events:
            if e.get("slug") == event_slug or event_slug in e.get("title", "").lower():
                target = e
                break
        if not target:
            print(f"Event not found: {event_slug}")
            return

        markets = target.get("markets", [])
        total_cost = 0
        positions = []
        for m in markets:
            prices = json.loads(m.get("outcomePrices", "[]")) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices", [])
            if not prices:
                continue
            yes_price = float(prices[0])
            if yes_price <= 0:
                continue
            fee = calc_fee(yes_price, amount)
            cost = amount + fee
            total_cost += cost
            pos = {
                "market_slug": m.get("slug", ""),
                "question": m.get("question", ""),
                "side": "yes",
                "entry_price": yes_price,
                "amount": amount,
                "fee": round(fee, 4),
                "cost": round(cost, 4),
                "timestamp": ts,
            }
            positions.append(pos)

        set_entry = {
            "type": "buy_all",
            "event": target.get("title", ""),
            "event_slug": event_slug,
            "positions": positions,
            "total_cost": round(total_cost, 4),
            "guaranteed_return": amount * len(positions),  # One outcome will win
            "expected_profit": round(amount - total_cost / len(positions), 4) if positions else 0,
            "reason": reason,
            "timestamp": ts,
            "status": "open",
        }
        ledger["sets"].append(set_entry)
        save_ledger(ledger)
        print(f"\n  ✅ BUY ALL SET placed on: {target.get('title','')}")
        print(f"  {len(positions)} positions | Total cost: ${total_cost:.2f}")
        print(f"  Guaranteed return: ${amount:.2f} (one outcome resolves Yes)")
        print(f"  Expected profit: ${amount - total_cost/len(positions):.2f} per outcome")

    else:
        # Buy specific outcome
        # side format: "buy_yes" or market slug
        print(f"  Single outcome trade: {side} on {event_slug} for ${amount}")
        # Find the market
        events = fetch_events()
        target_market = None
        for e in events:
            for m in e.get("markets", []):
                if m.get("slug") == side or side in m.get("question", "").lower():
                    target_market = m
                    break

        if not target_market:
            print(f"Market not found: {side}")
            return

        prices = json.loads(target_market.get("outcomePrices", "[]")) if isinstance(target_market.get("outcomePrices"), str) else target_market.get("outcomePrices", [])
        yes_price = float(prices[0]) if prices else 0.5
        if price_override:
            yes_price = float(price_override)

        fee = calc_fee(yes_price, amount)
        trade = {
            "market_slug": target_market.get("slug", ""),
            "question": target_market.get("question", ""),
            "side": "yes",
            "entry_price": yes_price,
            "amount": amount,
            "fee": round(fee, 4),
            "cost": round(amount + fee, 4),
            "reason": reason,
            "timestamp": ts,
            "status": "open",
        }
        ledger["open_positions"].append(trade)
        save_ledger(ledger)
        print(f"\n  ✅ BUY YES: {target_market.get('question','')}")
        print(f"  Price: {yes_price:.4f} | Amount: ${amount} | Fee: ${fee:.4f}")

def show_stats():
    """Show paper trading stats for mispricing strategy."""
    ledger = load_ledger()
    trades = ledger.get("trades", [])
    open_pos = ledger.get("open_positions", [])
    sets = ledger.get("sets", [])

    print(f"\n{'='*50}")
    print(f"  MISPRICING STRATEGY — PAPER TRADING STATS")
    print(f"{'='*50}\n")

    total_trades = len(trades)
    total_sets = len(sets)
    total_open = len(open_pos) + len([s for s in sets if s.get("status") == "open"])

    wins = [t for t in trades if t.get("pnl", 0) > 0]
    losses = [t for t in trades if t.get("pnl", 0) <= 0]
    total_pnl = sum(t.get("pnl", 0) for t in trades)

    print(f"  Resolved trades: {total_trades}")
    print(f"  Open positions:  {total_open}")
    print(f"  Buy-all sets:    {total_sets}")
    print(f"  Win rate:        {len(wins)/total_trades*100:.1f}%" if total_trades else "  Win rate:        N/A")
    print(f"  Total P&L:       ${total_pnl:+.2f}")

    if open_pos:
        print(f"\n  Open Positions:")
        for p in open_pos:
            print(f"    {p['question'][:50]} @ {p['entry_price']:.4f} (${p['amount']})")

    if sets:
        print(f"\n  Open Sets:")
        for s in sets:
            if s.get("status") == "open":
                print(f"    {s['event'][:50]} | {len(s['positions'])} outcomes | Cost: ${s['total_cost']:.2f}")

# --- Main ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-Outcome Mispricing Scanner")
    parser.add_argument("--scan", action="store_true", help="Scan for mispriced markets")
    parser.add_argument("--min-gap", type=float, default=1.5, help="Min gap %% to flag (default 1.5)")
    parser.add_argument("--clob", action="store_true", help="Use live CLOB prices (slower)")
    parser.add_argument("--top", type=int, default=20, help="Top N results")
    parser.add_argument("--trade", nargs=4, metavar=("EVENT", "SIDE", "AMOUNT", "REASON"), help="Place paper trade")
    parser.add_argument("--stats", action="store_true", help="Show stats")
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    if args.scan:
        results = scan_markets(min_gap=args.min_gap, use_clob=args.clob, top_n=args.top)
        if args.json:
            print(json.dumps(results, indent=2))
        else:
            print_scan(results)
    elif args.trade:
        event_slug, side, amount, reason = args.trade
        place_trade(event_slug, side, float(amount), reason)
    elif args.stats:
        show_stats()
    else:
        parser.print_help()
