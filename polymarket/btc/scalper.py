#!/usr/bin/env python3.12
"""
Late-Window Scalper — BTC 5-min Polymarket
==========================================
Watches last 30s of each 5-min window. When delta is large and PM entry
price is favorable, places a scalp trade (or paper logs it).

Strategy:
  - At T-30s (270s into window), check Binance spot delta vs CL strike
  - If |delta| >= MIN_DELTA and entry_price <= MAX_ENTRY, take the trade
  - Direction = sign of delta (positive → Up, negative → Down)
  - Settlement via Chainlink at T+300s

Usage:
  python3.12 scalper.py              # paper mode (default)
  python3.12 scalper.py --live       # live trading (requires explicit flag)
"""

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MIN_DELTA = 100          # minimum |delta| in $ to trigger entry
MAX_ENTRY = 0.92         # max entry price (PM side price)
POSITION_SIZE = 50       # $ per scalp trade
OBSERVE_START = 255      # start watching/logging at T-45s
ENTRY_WINDOW_START = 270 # only trade from T-30s
ENTRY_WINDOW_END = 290   # stop trying at T-10s (need time for order)
SAMPLE_INTERVAL = 3      # poll every 3s during entry window
CL_STRIKE_DELAY = 8      # seconds to wait before capturing CL strike

CHAINLINK_FEED_ID = "0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8"
CHAINLINK_API = "https://data.chain.link/api/query-timescale"
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

BOT_DIR = Path(__file__).parent
LEDGER_PATH = BOT_DIR / "ledgers" / "scalper.json"
LOG_PATH = BOT_DIR / "logs" / "scalper.log"
NO_TRADE_PATH = BOT_DIR / "NO_TRADE"
CREDS_PATH = Path.home() / ".openclaw" / "workspace" / ".polymarket-creds.json"

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
_running = True


def _shutdown(sig, frame):
    global _running
    print(f"\n[{ts()}] Shutting down (signal {sig})...")
    _running = False


signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def ts():
    return datetime.now(timezone(timedelta(hours=-4))).strftime("%H:%M:%S")


def log(msg):
    line = f"[{ts()}] {msg}"
    print(line, flush=True)


def fetch_json(url, timeout=8):
    req = Request(url, headers={"User-Agent": "scalper/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def get_binance_price():
    """Get current BTC spot price from Binance."""
    data = fetch_json("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
    if data and "price" in data:
        return round(float(data["price"]), 2)
    return None


def get_cl_strike():
    """Get current Chainlink price (used for strike capture)."""
    cl_url = f"{CHAINLINK_API}?query=LIVE_STREAM_REPORTS_QUERY&variables=%7B%22feedId%22%3A%22{CHAINLINK_FEED_ID}%22%7D"
    data = fetch_json(cl_url)
    if not data or "data" not in data:
        return None, None
    nodes = data["data"].get("liveStreamReports", {}).get("nodes", [])
    if not nodes:
        return None, None
    price = round(float(nodes[0]["price"]) / 1e18, 2)
    report_ts = int(nodes[0].get("observationsTimestamp", 0))
    return price, report_ts


def get_pm_prices(window_ts):
    """Get Polymarket prices for current window. Returns dict with up/down prices and tokens."""
    slug = f"btc-updown-5m-{window_ts}"
    data = fetch_json(f"{GAMMA_BASE}/events?slug={slug}")
    if not data:
        return None
    event = data[0] if data else None
    if not event:
        return None

    for m in event.get("markets", []):
        if m.get("closed"):
            continue
        try:
            prices = json.loads(m.get("outcomePrices", "[]"))
            outcomes = json.loads(m.get("outcomes", "[]"))
            up_idx = 0 if "Up" in outcomes[0] else 1
            down_idx = 1 - up_idx
            tokens = json.loads(m.get("clobTokenIds", "[]"))

            result = {
                "up_price": round(float(prices[up_idx]), 4),
                "down_price": round(float(prices[down_idx]), 4),
                "up_token": tokens[up_idx] if tokens else "",
                "down_token": tokens[down_idx] if tokens else "",
            }

            # Get CLOB midpoint for more accurate pricing
            for side, idx in [("up", up_idx), ("down", down_idx)]:
                if tokens:
                    mid = fetch_json(f"{CLOB_BASE}/midpoint?token_id={tokens[idx]}")
                    if mid and mid.get("mid"):
                        result[f"{side}_mid"] = round(float(mid["mid"]), 4)
                    book = fetch_json(f"{CLOB_BASE}/book?token_id={tokens[idx]}")
                    if book:
                        asks = sorted(book.get("asks", []), key=lambda x: float(x["price"]))
                        bids = sorted(book.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
                        if asks:
                            result[f"{side}_best_ask"] = round(float(asks[0]["price"]), 4)
                            # Sum available size at reasonable prices
                            total_size = sum(float(a["size"]) for a in asks[:5])
                            result[f"{side}_ask_depth"] = round(total_size, 2)
                        if bids:
                            result[f"{side}_best_bid"] = round(float(bids[0]["price"]), 4)

            return result
        except Exception:
            continue
    return None


def get_entry_price(pm, direction):
    """Get best entry price for a direction. Prefers mid > best_ask > gamma price."""
    side = "up" if direction == "Up" else "down"
    return pm.get(f"{side}_mid",
           pm.get(f"{side}_best_ask",
           pm.get(f"{side}_price", 0.5)))


def load_ledger():
    if LEDGER_PATH.exists():
        return json.loads(LEDGER_PATH.read_text())
    return {
        "trades": [],
        "stats": {"wins": 0, "losses": 0, "total_pnl": 0.0, "total_trades": 0}
    }


def save_ledger(ledger):
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_PATH.write_text(json.dumps(ledger, indent=2))


def place_order(token_id, side_price, size, is_paper=True):
    """Place order on Polymarket CLOB. Returns order info or paper sim."""
    if is_paper:
        shares = size / side_price
        return {"paper": True, "price": side_price, "size": size, "shares": round(shares, 2)}

    # Live order placement
    try:
        creds = json.loads(CREDS_PATH.read_text())
    except Exception as e:
        log(f"⊘ Failed to load creds: {e}")
        return None

    # Use live-trader.py subprocess for actual order placement
    # (reuses existing signing/order infrastructure)
    trader_script = BOT_DIR / "live-trader.py"
    import subprocess
    result = subprocess.run(
        [sys.executable, str(trader_script), token_id, str(side_price), str(size)],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode == 0:
        try:
            return json.loads(result.stdout)
        except Exception:
            return {"status": "submitted", "stdout": result.stdout[:200]}
    else:
        log(f"⊘ Order failed: {result.stderr[:200]}")
        return None


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="BTC Late-Window Scalper")
    parser.add_argument("--live", action="store_true", help="Enable live trading")
    parser.add_argument("--min-delta", type=float, default=MIN_DELTA, help=f"Min |delta| in $ (default {MIN_DELTA})")
    parser.add_argument("--max-entry", type=float, default=MAX_ENTRY, help=f"Max entry price (default {MAX_ENTRY})")
    parser.add_argument("--size", type=float, default=POSITION_SIZE, help=f"Position size $ (default {POSITION_SIZE})")
    args = parser.parse_args()

    is_paper = not args.live
    min_delta = args.min_delta
    max_entry = args.max_entry
    size = args.size

    mode_str = "PAPER" if is_paper else "LIVE"
    log(f"=== Scalper started ({mode_str}) ===")
    log(f"    min_delta=${min_delta:.0f}  max_entry={max_entry:.2f}  size=${size:.0f}")

    ledger = load_ledger()
    stats = ledger["stats"]
    log(f"    Ledger: {stats['wins']}W/{stats['losses']}L  PnL=${stats['total_pnl']:+.2f}")

    # Track state per window
    state = {
        "current_window": 0,
        "strike": None,
        "traded": False,
        "pending": None,   # pending trade awaiting settlement
    }

    while _running:
        now = time.time()
        window_start = int(now) // 300 * 300
        elapsed = now - window_start
        window_et = datetime.fromtimestamp(window_start, tz=timezone(timedelta(hours=-4)))
        window_str = window_et.strftime("%H%M")

        # New window — capture strike
        if window_start != state["current_window"]:
            # Resolve pending trade from previous window
            if state["pending"]:
                resolve_trade(state, ledger, is_paper)

            state["current_window"] = window_start
            state["strike"] = None
            state["traded"] = False
            state["pending"] = None

            # Wait for CL to populate, then capture strike
            time.sleep(CL_STRIKE_DELAY)
            cl_price, cl_ts = get_cl_strike()
            if cl_price:
                state["strike"] = cl_price
                log(f"📌 Window {window_str} | Strike: ${cl_price:,.2f}")
            else:
                log(f"⊘ Window {window_str} | Failed to capture CL strike")

        # Not in observe window yet — sleep until we are
        if elapsed < OBSERVE_START - 5:
            sleep_for = OBSERVE_START - 5 - elapsed
            time.sleep(min(sleep_for, 10))  # wake up periodically to check _running
            continue

        # Past entry window — wait for next window
        if elapsed > ENTRY_WINDOW_END or state["traded"]:
            if state["traded"] and not state["pending"]:
                pass  # already resolved or just waiting
            remaining = 300 - elapsed
            time.sleep(min(remaining + 1, 10))
            continue

        # === OBSERVE + ENTRY WINDOW ===
        if state["strike"] is None or state["traded"]:
            time.sleep(SAMPLE_INTERVAL)
            continue

        # Check NO_TRADE (live mode only)
        if not is_paper and NO_TRADE_PATH.exists():
            time.sleep(SAMPLE_INTERVAL)
            continue

        # Get current price and delta
        btc_price = get_binance_price()
        if btc_price is None:
            time.sleep(SAMPLE_INTERVAL)
            continue

        delta = btc_price - state["strike"]
        abs_delta = abs(delta)
        direction = "Up" if delta > 0 else "Down"
        time_left = 300 - elapsed

        # Check delta threshold
        if abs_delta < min_delta:
            if elapsed >= ENTRY_WINDOW_START:  # only log during entry window to reduce noise
                log(f"  {window_str} T-{time_left:.0f}s | δ={delta:+.1f} | Below threshold (need ≥${min_delta:.0f})")
            time.sleep(SAMPLE_INTERVAL)
            continue

        # Get PM prices
        pm = get_pm_prices(window_start)
        if not pm:
            log(f"  {window_str} T-{time_left:.0f}s | δ={delta:+.1f} | No PM data")
            time.sleep(SAMPLE_INTERVAL)
            continue

        entry_price = get_entry_price(pm, direction)
        side = "up" if direction == "Up" else "down"
        depth = pm.get(f"{side}_ask_depth", 0)

        # OBSERVE phase (T-45s to T-30s): log prices but don't trade
        if elapsed < ENTRY_WINDOW_START:
            log(f"  {window_str} T-{time_left:.0f}s | δ={delta:+.1f} | {direction} @ {entry_price:.3f} | depth={depth:.0f} [OBSERVE]")
            time.sleep(SAMPLE_INTERVAL)
            continue

        # ENTRY phase (T-30s to T-10s): trade if conditions met
        # Check entry price threshold
        if entry_price > max_entry:
            log(f"  {window_str} T-{time_left:.0f}s | δ={delta:+.1f} | {direction} @ {entry_price:.3f} > {max_entry} — too expensive")
            time.sleep(SAMPLE_INTERVAL)
            continue

        # === ENTRY ===
        shares = size / entry_price
        log(f"  ✦ SCALP {direction} @ {entry_price:.3f} | δ={delta:+.1f} | T-{time_left:.0f}s | ${size:.0f} → {shares:.1f} shares | depth={depth:.0f}")

        token_id = pm.get(f"{side.lower()}_token", "")
        order = place_order(token_id, entry_price, size, is_paper=is_paper)

        if order:
            state["traded"] = True
            state["pending"] = {
                "direction": direction,
                "entry_price": entry_price,
                "size": size,
                "shares": shares,
                "delta_at_entry": delta,
                "time_left_at_entry": time_left,
                "window": window_str,
                "window_ts": window_start,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "order": order,
                "btc_at_entry": btc_price,
            }
            log(f"  ✓ Order {'simulated' if is_paper else 'placed'}")
        else:
            log(f"  ✗ Order failed")

        time.sleep(SAMPLE_INTERVAL)

    # Final cleanup
    if state.get("pending"):
        log("Resolving final pending trade before exit...")
        resolve_trade(state, ledger, is_paper)

    log("=== Scalper stopped ===")


def resolve_trade(state, ledger, is_paper):
    """Wait for settlement and resolve a pending trade."""
    pending = state["pending"]
    if not pending:
        return

    window_end = pending["window_ts"] + 300

    # Wait for window to end + buffer for CL settlement
    now = time.time()
    if now < window_end + 10:
        wait = window_end + 10 - now
        log(f"  ⏳ Waiting {wait:.0f}s for settlement...")
        time.sleep(wait)

    # Get settlement price from Chainlink
    cl_price, _ = get_cl_strike()
    if cl_price is None:
        log(f"  ⊘ Failed to get settlement price")
        # Try again
        time.sleep(5)
        cl_price, _ = get_cl_strike()

    if cl_price is None:
        log(f"  ⊘ Settlement failed — marking as unknown")
        state["pending"] = None
        return

    # Determine outcome
    went_up = cl_price > pending.get("strike_at_entry", state.get("strike", 0))
    # Actually we need to compare settlement vs strike
    strike = state.get("strike", 0)
    went_up = cl_price > strike

    won = (pending["direction"] == "Up" and went_up) or \
          (pending["direction"] == "Down" and not went_up)

    if won:
        payout = pending["shares"] * 1.0  # shares pay $1 each
        profit = payout - pending["size"]
        ledger["stats"]["wins"] += 1
    else:
        profit = -pending["size"]
        ledger["stats"]["losses"] += 1

    ledger["stats"]["total_pnl"] = round(ledger["stats"]["total_pnl"] + profit, 2)
    ledger["stats"]["total_trades"] += 1

    result = "WIN" if won else "LOSS"
    w, l = ledger["stats"]["wins"], ledger["stats"]["losses"]
    pnl = ledger["stats"]["total_pnl"]

    trade_record = {
        **pending,
        "settlement": cl_price,
        "strike": strike,
        "result": result,
        "profit": round(profit, 2),
        "paper": is_paper,
    }
    # Remove non-serializable bits
    trade_record.pop("order", None)
    ledger["trades"].append(trade_record)
    save_ledger(ledger)

    emoji = "✅" if won else "❌"
    log(f"  {emoji} {result}: {pending['direction']} | entry {pending['entry_price']:.3f} | "
        f"strike ${strike:,.2f} → settle ${cl_price:,.2f} | "
        f"P&L ${profit:+.2f} | Record: {w}W/{l}L ${pnl:+.2f}")

    state["pending"] = None


if __name__ == "__main__":
    main()
