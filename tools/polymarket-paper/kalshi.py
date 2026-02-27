#!/usr/bin/env python3
"""
Kalshi Price Feed — Read-only cross-reference for Polymarket trading.

Pulls public market data from Kalshi's API (no auth needed) and matches
it against Polymarket markets to provide a "smart money" fair value signal.

Usage:
    # As library
    from kalshi import KalshiFeed
    feed = KalshiFeed()
    match = feed.find_match("Will the Fed cut rates in March 2026?")
    # → {"kalshi_price": 0.04, "kalshi_ticker": "KXFEDDECISION-26MAR-C25", ...}

    # CLI
    python3 kalshi.py --search "fed rate march"
    python3 kalshi.py --match "Will the Fed cut rates by 50bps in March?"
    python3 kalshi.py --compare  # compare all open newsbot positions
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
CACHE_DIR = Path(__file__).parent / "cache"
EVENTS_CACHE = CACHE_DIR / "kalshi_events.json"
MARKETS_CACHE = CACHE_DIR / "kalshi_markets.json"
CACHE_TTL = 600  # 10 min


def _fetch(url, timeout=15):
    """Fetch JSON from Kalshi API."""
    req = Request(url, headers={"User-Agent": "polymarket-paper/1.0", "Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (URLError, json.JSONDecodeError) as e:
        print(f"  [kalshi] fetch error: {e}", file=sys.stderr)
        return None


class KalshiFeed:
    def __init__(self, cache_ttl=CACHE_TTL):
        self.cache_ttl = cache_ttl
        CACHE_DIR.mkdir(exist_ok=True)

    def _load_cache(self, path):
        if path.exists():
            data = json.loads(path.read_text())
            if time.time() - data.get("_ts", 0) < self.cache_ttl:
                return data.get("items", [])
        return None

    def _save_cache(self, path, items):
        path.write_text(json.dumps({"_ts": time.time(), "items": items}, indent=2))

    def fetch_events(self, status="open", limit=500):
        """Fetch all open Kalshi events (paginated)."""
        cached = self._load_cache(EVENTS_CACHE)
        if cached is not None:
            return cached

        events = []
        cursor = None
        while True:
            url = f"{KALSHI_BASE}/events?status={status}&limit=200"
            if cursor:
                url += f"&cursor={cursor}"
            data = _fetch(url)
            if not data:
                break
            batch = data.get("events", [])
            events.extend(batch)
            cursor = data.get("cursor")
            if not cursor or len(batch) < 200:
                break

        self._save_cache(EVENTS_CACHE, events)
        return events

    def fetch_markets_for_event(self, event_ticker):
        """Fetch all markets for a specific event."""
        url = f"{KALSHI_BASE}/markets?event_ticker={event_ticker}&status=open"
        data = _fetch(url)
        if not data:
            return []
        return data.get("markets", [])

    def fetch_market(self, ticker):
        """Fetch a single market by ticker."""
        data = _fetch(f"{KALSHI_BASE}/markets/{ticker}")
        if not data:
            return None
        return data.get("market")

    def search_events(self, query):
        """Search events by keyword matching on title."""
        events = self.fetch_events()
        query_lower = query.lower()
        terms = [t for t in query_lower.split() if len(t) > 2]

        scored = []
        for e in events:
            title_lower = e.get("title", "").lower()
            subtitle_lower = e.get("sub_title", "").lower()
            combined = f"{title_lower} {subtitle_lower}"

            hits = sum(1 for t in terms if t in combined)
            if hits >= max(1, len(terms) // 2):
                scored.append((hits, e))

        scored.sort(key=lambda x: -x[0])
        return [e for _, e in scored[:10]]

    def find_match(self, polymarket_question, polymarket_side=None):
        """
        Given a Polymarket question, find the best matching Kalshi market
        and return its price as a cross-reference signal.

        Returns dict with kalshi_price, kalshi_ticker, match_quality, etc.
        or None if no match found.
        """
        # Extract key terms from the Polymarket question
        q = polymarket_question.lower()
        q = re.sub(r'[?!.,\']', '', q)

        # Try multiple search strategies
        key_terms = self._extract_key_terms(q)
        if not key_terms:
            return None

        # Strategy 1: Try direct event ticker guess for known patterns
        ticker_guesses = self._guess_event_tickers(q)
        
        # Strategy 2: keyword search with multiple queries  
        search_queries = [
            " ".join(key_terms),
            " ".join(key_terms[:3]),
        ]
        if len(key_terms) > 3:
            search_queries.append(" ".join(key_terms[1:4]))

        seen_events = set()
        best_match = None
        best_score = 0

        # Try ticker guesses first (most precise)
        for ticker in ticker_guesses:
            markets = self.fetch_markets_for_event(ticker)
            for market in markets:
                score = self._score_match(q, polymarket_side, market)
                if score > best_score:
                    best_score = score
                    best_match = market

        # Then try keyword search
        for sq in search_queries:
            events = self.search_events(sq)
            for event in events[:5]:
                et = event["event_ticker"]
                if et in seen_events:
                    continue
                seen_events.add(et)

                markets = self.fetch_markets_for_event(et)
                for market in markets:
                    score = self._score_match(q, polymarket_side, market)
                    if score > best_score:
                        best_score = score
                        best_match = market

        if not best_match or best_score < 2:
            return None

        # Extract price
        yes_bid = best_match.get("yes_bid", 0) or 0
        yes_ask = best_match.get("yes_ask", 0) or 0
        last_price = best_match.get("last_price", 0) or 0

        # Midpoint if both bid/ask available, else last price
        if yes_bid and yes_ask:
            kalshi_price = (yes_bid + yes_ask) / 200  # cents to decimal
        elif last_price:
            kalshi_price = last_price / 100
        else:
            return None

        return {
            "kalshi_price": kalshi_price,
            "kalshi_ticker": best_match.get("ticker", ""),
            "kalshi_title": best_match.get("title", ""),
            "kalshi_yes_bid": (yes_bid or 0) / 100,
            "kalshi_yes_ask": (yes_ask or 0) / 100,
            "kalshi_last": (last_price or 0) / 100,
            "kalshi_volume": best_match.get("volume", 0),
            "kalshi_open_interest": best_match.get("open_interest", 0),
            "match_score": best_score,
            "event_ticker": best_match.get("event_ticker", ""),
        }

    def _guess_event_tickers(self, question):
        """Guess Kalshi event tickers from question content using known patterns."""
        tickers = []
        q = question.lower()
        
        # Month/year extraction
        month_codes = {"january": "JAN", "february": "FEB", "march": "MAR",
                       "april": "APR", "may": "MAY", "june": "JUN", "july": "JUL",
                       "august": "AUG", "september": "SEP", "october": "OCT",
                       "november": "NOV", "december": "DEC"}
        month = None
        for name, code in month_codes.items():
            if name in q:
                month = code
                break
        
        year_match = re.search(r'\b(202[4-9])\b', q)
        year_short = year_match.group(1)[2:] if year_match else None
        
        date_suffix = f"-{year_short}{month}" if year_short and month else ""
        
        # Fed / interest rate
        if any(w in q for w in ["fed", "federal reserve", "interest rate", "fomc", "rate cut", "rate hike"]):
            if date_suffix:
                tickers.append(f"KXFEDDECISION{date_suffix}")
                tickers.append(f"KXFED{date_suffix}")
        
        # Iran
        if "iran" in q:
            if "strike" in q or "attack" in q or "bomb" in q or "military" in q:
                tickers.append("KXSTRIKEIRAN-26")
                tickers.append("KXUSIRAN-26")
            tickers.append("KXUSAIRANAGREEMENT-27")
        
        # Tariffs
        if "tariff" in q:
            tickers.append("KXTARIFF-26")
        
        # Bitcoin / crypto
        if any(w in q for w in ["bitcoin", "btc"]):
            tickers.append("KXBTC")
        
        return tickers

    def _extract_key_terms(self, question):
        """Extract meaningful search terms from a prediction market question."""
        stop_words = {
            "will", "the", "a", "an", "be", "is", "are", "by", "in", "on",
            "of", "to", "and", "or", "for", "at", "with", "from", "before",
            "after", "this", "that", "what", "who", "how", "when", "there",
            "have", "has", "had", "do", "does", "did", "not", "no", "yes",
            "any", "than", "more", "less", "its", "their", "his", "her",
            "would", "could", "should", "may", "might", "can", "much",
        }
        words = question.split()
        terms = [w for w in words if w not in stop_words and len(w) > 2]
        return terms[:8]

    def _score_match(self, poly_question, poly_side, kalshi_market):
        """Score how well a Kalshi market matches a Polymarket question."""
        kalshi_title = kalshi_market.get("title", "").lower()
        kalshi_ticker = kalshi_market.get("ticker", "").lower()
        kalshi_combined = f"{kalshi_title} {kalshi_ticker}"
        kalshi_combined = re.sub(r'[?!.,\']', '', kalshi_combined)

        poly_terms = set(self._extract_key_terms(poly_question))
        kalshi_terms = set(self._extract_key_terms(kalshi_title))

        # Word overlap
        overlap = poly_terms & kalshi_terms
        score = len(overlap)

        # Heavy bonus for matching specific quantities and dates
        # These are the terms that distinguish one market from another
        month_map = {"january": "jan", "february": "feb", "march": "mar",
                     "april": "apr", "may": "may", "june": "jun", "july": "jul",
                     "august": "aug", "september": "sep", "october": "oct",
                     "november": "nov", "december": "dec"}
        date_patterns = re.findall(r'\b(?:january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\b', poly_question)
        for dp in date_patterns:
            short = month_map.get(dp, dp)
            if dp in kalshi_combined or short in kalshi_combined:
                score += 3
            else:
                # Wrong month is a strong negative signal
                score -= 2

        # Match year — and penalize wrong year
        years = re.findall(r'\b(202[4-9])\b', poly_question)
        for y in years:
            if y in kalshi_combined:
                score += 2
            else:
                score -= 2

        # Match specific numbers (bps, percentages, etc.)
        numbers = re.findall(r'\b(\d+)\s*(?:bps|bp|basis)', poly_question)
        for n in numbers:
            if n in kalshi_combined:
                score += 3

        # Match action words (cut, hike, decrease, increase)
        actions = {"cut": ["cut", "decrease", "lower", "reduce"],
                   "hike": ["hike", "increase", "raise"],
                   "decrease": ["cut", "decrease", "lower", "reduce"],
                   "increase": ["hike", "increase", "raise"]}
        for word in poly_question.split():
            synonyms = actions.get(word, [])
            for syn in synonyms:
                if syn in kalshi_combined:
                    score += 2
                    break

        # Match key entities (proper nouns, orgs)
        entities = re.findall(r'\b(?:fed|federal reserve|fomc|iran|trump|china|russia|nato|eu|ecb|bitcoin|btc|eth)\b', poly_question)
        for ent in entities:
            if ent in kalshi_combined:
                score += 2

        # Bonus for matching side direction
        if poly_side:
            side_lower = poly_side.lower()
            if side_lower in kalshi_title or side_lower in kalshi_ticker:
                score += 1

        return score

    def compare_positions(self, ledger_path=None):
        """Compare all open newsbot positions against Kalshi prices."""
        if ledger_path is None:
            ledger_path = Path(__file__).parent / "ledgers" / "newsbot.json"

        if not ledger_path.exists():
            print("No newsbot ledger found")
            return []

        ledger = json.loads(ledger_path.read_text())
        comparisons = []

        for pos in ledger.get("open_positions", []):
            if pos.get("resolved"):
                continue

            question = pos.get("market", "")
            side = pos.get("side", "")

            match = self.find_match(question, side)

            comp = {
                "polymarket_question": question,
                "polymarket_side": side,
                "polymarket_entry": pos.get("entry_price", 0),
                "polymarket_fair_value": pos.get("fair_value", 0),
                "kalshi_match": match,
            }

            if match:
                comp["divergence"] = match["kalshi_price"] - pos.get("entry_price", 0)
                comp["kalshi_agrees_with_trade"] = (
                    match["kalshi_price"] > pos.get("entry_price", 0)
                )

            comparisons.append(comp)

        return comparisons


# ─── CLI ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Kalshi Price Feed")
    parser.add_argument("--search", type=str, help="Search Kalshi events by keyword")
    parser.add_argument("--match", type=str, help="Find Kalshi match for a Polymarket question")
    parser.add_argument("--compare", action="store_true", help="Compare open newsbot positions vs Kalshi")
    parser.add_argument("--events", action="store_true", help="List all open Kalshi events")
    parser.add_argument("--markets", type=str, help="List markets for an event ticker")

    args = parser.parse_args()
    feed = KalshiFeed()

    if args.search:
        events = feed.search_events(args.search)
        for e in events:
            print(f"  {e['event_ticker']:40s} | {e['title']}")

    elif args.match:
        match = feed.find_match(args.match)
        if match:
            print(f"  Match: {match['kalshi_title']}")
            print(f"  Ticker: {match['kalshi_ticker']}")
            print(f"  Price: {match['kalshi_price']:.1%} (bid={match['kalshi_yes_bid']:.1%} ask={match['kalshi_yes_ask']:.1%})")
            print(f"  Volume: {match['kalshi_volume']:,} | OI: {match['kalshi_open_interest']:,}")
            print(f"  Match score: {match['match_score']}")
        else:
            print("  No match found")

    elif args.compare:
        comps = feed.compare_positions()
        if not comps:
            print("No open positions to compare")
            return
        for c in comps:
            print(f"\n  Polymarket: {c['polymarket_question'][:60]}")
            print(f"    Side: {c['polymarket_side']} @ {c['polymarket_entry']:.3f} (est fair: {c['polymarket_fair_value']:.3f})")
            m = c.get("kalshi_match")
            if m:
                agrees = "✅" if c.get("kalshi_agrees_with_trade") else "⚠️"
                print(f"    Kalshi:  {m['kalshi_title'][:60]}")
                print(f"    Price:   {m['kalshi_price']:.1%} (bid={m['kalshi_yes_bid']:.1%} ask={m['kalshi_yes_ask']:.1%})")
                print(f"    Divergence: {c['divergence']:+.1%} {agrees}")
                print(f"    Volume: {m['kalshi_volume']:,} | Match score: {m['match_score']}")
            else:
                print(f"    Kalshi:  No matching market found")

    elif args.markets:
        markets = feed.fetch_markets_for_event(args.markets)
        for m in markets:
            last = (m.get("last_price") or 0) / 100
            vol = m.get("volume", 0)
            print(f"  {m['ticker']:45s} | last={last:.1%} | vol={vol:>10,} | {m['title'][:50]}")

    elif args.events:
        events = feed.fetch_events()
        cats = {}
        for e in events:
            cat = e.get("category", "Other")
            cats.setdefault(cat, []).append(e)
        for cat in sorted(cats):
            print(f"\n  {cat} ({len(cats[cat])})")
            for e in cats[cat][:5]:
                print(f"    {e['event_ticker']:40s} | {e['title'][:55]}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
