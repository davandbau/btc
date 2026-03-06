#!/usr/bin/env python3.12
"""
Polymarket Portfolio Sweep — Check for claimable winnings via CLOB API.
Falls back to gamma API if needed.
Outputs condition IDs that need browser-based claiming.

Usage: python3.12 sweep-portfolio.py
"""

import json
import sys
import urllib.request
from pathlib import Path

CREDS_FILE = Path(__file__).parent.parent.parent / ".polymarket-creds.json"


def check_claimable():
    """Check for claimable positions via Polymarket APIs."""
    creds = json.loads(CREDS_FILE.read_text())
    wallet = creds["address"].lower()
    
    # Try data-api for user positions
    urls = [
        f"https://data-api.polymarket.com/positions?user={wallet}&redeemable=true",
        f"https://gamma-api.polymarket.com/positions?user={wallet}&redeemable=true",
        f"https://data-api.polymarket.com/positions?user={wallet}&sizeThreshold=0.01",
    ]
    
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            resp = urllib.request.urlopen(req, timeout=15)
            positions = json.loads(resp.read())
            if positions:
                claimable = []
                for p in positions:
                    # Check if position is redeemable/resolved
                    if p.get("redeemable") or p.get("curPrice") in (0, 1) or p.get("cashPnl", 0) > 0:
                        claimable.append(p)
                if claimable:
                    return claimable
        except Exception as e:
            continue
    
    return []


def main():
    positions = check_claimable()
    
    if not positions:
        # No API-detected claims — output for cron
        print("NO_CLAIMS")
        return
    
    total = sum(p.get("cashPnl", 0) for p in positions)
    print(f"CLAIMABLE: ${total:.2f} across {len(positions)} positions")
    for p in positions:
        title = p.get("title", p.get("question", "?"))[:60]
        value = p.get("cashPnl", p.get("currentValue", 0))
        print(f"  {title} — ${value:.2f}")
    
    print("\nBROWSER_CLAIM_NEEDED")


if __name__ == "__main__":
    main()
