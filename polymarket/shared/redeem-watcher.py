#!/usr/bin/env python3.12
"""
Redeem watcher — monitors the ledger for resolved wins and triggers browser redemption.
Runs independently of the trading bot. Checks every 30s.

Usage:
    python3.12 redeem-watcher.py          # run continuously
    python3.12 redeem-watcher.py --once   # single check, then exit
"""

import json
import time
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

BOT_DIR = Path(__file__).parent
LEDGER_PATH = BOT_DIR / "ledgers" / "reasoning.json"
STATE_FILE = BOT_DIR / "logs" / "redeem-state.json"
REDEEM_SCRIPT = BOT_DIR / "redeem-browser.sh"
CHECK_INTERVAL = 300  # seconds (5 minutes)

def load_state():
    """Load last redeemed trade timestamp."""
    try:
        return json.load(open(STATE_FILE))
    except:
        return {"last_redeemed_at": None, "redeemed_count": 0}

def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    json.dump(state, open(STATE_FILE, "w"), indent=2)

def get_unredeemed_wins(state):
    """Find wins resolved after our last redemption."""
    try:
        ledger = json.load(open(LEDGER_PATH))
    except Exception as e:
        print(f"[{ts()}] ❌ Can't read ledger: {e}")
        return []

    last = state.get("last_redeemed_at")
    wins = []
    for t in ledger.get("trades", []):
        if t.get("outcome") != "win" or not t.get("resolved"):
            continue
        resolved_at = t.get("resolved_at", "")
        if last and resolved_at <= last:
            continue
        wins.append(t)
    return wins

def trigger_redeem():
    """Schedule browser-based redemption via openclaw cron."""
    try:
        result = subprocess.run(
            ["bash", str(REDEEM_SCRIPT)],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            print(f"[{ts()}] ✅ Redeem task scheduled")
            return True
        else:
            print(f"[{ts()}] ⚠️  Redeem script failed: {result.stderr[:100]}")
            return False
    except Exception as e:
        print(f"[{ts()}] ❌ Redeem trigger error: {e}")
        return False

def ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

def check_and_redeem(state):
    """Check for unredeemed wins and trigger redemption."""
    wins = get_unredeemed_wins(state)
    if not wins:
        return state

    total_pnl = sum(w.get("pnl", 0) for w in wins)
    print(f"[{ts()}] 💰 {len(wins)} unredeemed win(s), +${total_pnl:.2f} — triggering redeem")

    if trigger_redeem():
        # Mark all as redeemed by setting last_redeemed_at to the latest resolved_at
        latest = max(w.get("resolved_at", "") for w in wins)
        state["last_redeemed_at"] = latest
        state["redeemed_count"] = state.get("redeemed_count", 0) + len(wins)
        save_state(state)

    return state

def main():
    once = "--once" in sys.argv
    state = load_state()

    print(f"[{ts()}] 🔄 Redeem watcher started (interval: {CHECK_INTERVAL}s)")
    print(f"[{ts()}]    Ledger: {LEDGER_PATH}")
    print(f"[{ts()}]    Last redeemed: {state.get('last_redeemed_at', 'never')}")
    print(f"[{ts()}]    Total redeemed: {state.get('redeemed_count', 0)}")

    if once:
        check_and_redeem(state)
        return

    while True:
        try:
            state = check_and_redeem(state)
        except Exception as e:
            print(f"[{ts()}] ❌ Error: {e}")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
