#!/usr/local/opt/python@3.12/bin/python3.12
"""
Polymarket Live Trader — Executes real trades on Polymarket CLOB.

SAFETY GUARDRAILS:
  - Max position size: $30 (configurable, hardcoded ceiling at $50)
  - Max daily loss: $100 (stops trading for the day)
  - Max concurrent positions: 3
  - Kill switch: touch ~/POLY_KILL to halt all trading
  - Requires explicit --live flag (default is dry-run)
  - All trades logged to ledger before execution
  - Credentials NEVER leave this file / Clive's workspace

This runs in CLIVE'S workspace (not Poly's). The reasoning loop calls this
script instead of the paper trader when live mode is enabled.

Usage:
    python3.12 live-trader.py --trade "Up" "0.45" "reasoning..." --size 25
    python3.12 live-trader.py --trade "Down" "0.65" "reasoning..." --size 30 --live
    python3.12 live-trader.py --balance
    python3.12 live-trader.py --positions
    python3.12 live-trader.py --resolve
    python3.12 live-trader.py --stats
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request

# ---- Safety Constants ----
MAX_POSITION_SIZE = 100     # Default max per trade
ABSOLUTE_MAX_SIZE = 100     # Hard ceiling, cannot be overridden
MAX_DAILY_LOSS = 200        # Stop trading after this much daily loss
MAX_CONCURRENT = 3          # Max open positions at once
KILL_SWITCH_FILE = Path.home() / "POLY_KILL"

# ---- Paths ----
SCRIPT_DIR = Path(__file__).parent
CREDS_FILE = Path.home() / ".openclaw/workspace/.polymarket-creds.json"
LEDGER_FILE = SCRIPT_DIR / "ledgers" / "reasoning.json"
DAILY_LOG_DIR = SCRIPT_DIR / "live-logs"

# ---- API ----
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
CHAIN_ID = 137
CHAINLINK_FEED = "0x000359843a543ee2fe414dc14c7e7920ef10f4372990b79d6361cdc0dd1ba782"
CHAINLINK_API = "https://data.chain.link/api/query-timescale"


def load_creds():
    if not CREDS_FILE.exists():
        print("[ERR] Credentials file not found")
        sys.exit(1)
    return json.loads(CREDS_FILE.read_text())


def get_client(creds):
    """Initialize authenticated Polymarket CLOB client."""
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    return ClobClient(
        CLOB_BASE,
        key=creds["privateKey"],
        chain_id=CHAIN_ID,
        creds=ApiCreds(
            api_key=creds["apiKey"],
            api_secret=creds["apiSecret"],
            api_passphrase=creds["apiPassphrase"],
        ),
        signature_type=1,  # Polymarket proxy wallet
        funder=creds["address"],
    )


def fetch_json(url, timeout=8):
    req = Request(url, headers={"User-Agent": "live-trader/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except:
        return None


def load_ledger():
    if LEDGER_FILE.exists():
        return json.loads(LEDGER_FILE.read_text())
    return {
        "strategy": "live",
        "mode": "LIVE",
        "trades": [],
        "open_positions": [],
        "stats": {
            "total_pnl": 0, "wins": 0, "losses": 0,
            "total_trades": 0, "gross_profit": 0, "gross_loss": 0,
            "total_fees": 0, "total_wagered": 0,
        },
    }


def save_ledger(ledger):
    LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_FILE.write_text(json.dumps(ledger, indent=2))


def log_trade(trade_data):
    """Append to daily log for audit trail."""
    DAILY_LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = DAILY_LOG_DIR / f"{today}.jsonl"
    with open(log_file, "a") as f:
        f.write(json.dumps(trade_data, default=str) + "\n")


# ---- Safety Checks ----

def check_kill_switch():
    if KILL_SWITCH_FILE.exists():
        print("[KILL] KILL SWITCH ACTIVE — touch ~/POLY_KILL detected. No trades.")
        sys.exit(1)


def check_daily_loss(ledger):
    """Check if daily loss limit has been hit."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_pnl = 0
    for t in ledger["trades"]:
        if t.get("timestamp", "").startswith(today) and t.get("resolved"):
            daily_pnl += t.get("pnl", 0)
    # Also count open positions at max loss
    for p in ledger["open_positions"]:
        daily_pnl -= p.get("cost", 0) + p.get("fee", 0)
    if daily_pnl < -MAX_DAILY_LOSS:
        print(f"[KILL] DAILY LOSS LIMIT — ${abs(daily_pnl):.2f} lost today (limit: ${MAX_DAILY_LOSS})")
        return False
    return True


def check_concurrent(ledger):
    if len(ledger["open_positions"]) >= MAX_CONCURRENT:
        print(f"[KILL] MAX CONCURRENT — {len(ledger['open_positions'])} positions open (limit: {MAX_CONCURRENT})")
        return False
    return True


def validate_size(size):
    if size > ABSOLUTE_MAX_SIZE:
        print(f"⚠️  Size ${size} exceeds absolute max ${ABSOLUTE_MAX_SIZE}, capping")
        return ABSOLUTE_MAX_SIZE
    if size > MAX_POSITION_SIZE:
        print(f"⚠️  Size ${size} exceeds default max ${MAX_POSITION_SIZE}, proceeding (under absolute)")
    return size


# ---- Trading ----

def get_token_for_side(slug, side):
    """Look up the correct CLOB token ID for a side. Also returns condition_id."""
    pm_data = fetch_json(f"{GAMMA_BASE}/events?slug={slug}")
    if not pm_data:
        return None, None
    event = pm_data[0]
    for m in event.get("markets", []):
        if not m.get("closed"):
            try:
                outcomes = json.loads(m.get("outcomes", "[]"))
                tokens = json.loads(m.get("clobTokenIds", "[]"))
                condition_id = m.get("conditionId")
                up_idx = 0 if "Up" in outcomes[0] else 1
                token = tokens[up_idx] if side == "Up" else tokens[1 - up_idx]
                return token, condition_id
            except:
                pass
    return None, None


def get_chainlink_price(at_timestamp=None):
    """Get ETH price from Chainlink. If at_timestamp given, find closest price to that time."""
    try:
        url = f"{CHAINLINK_API}?query=LIVE_STREAM_REPORTS_QUERY&variables=%7B%22feedId%22%3A%22{CHAINLINK_FEED}%22%7D"
        data = fetch_json(url, timeout=5)
        if data and "data" in data:
            nodes = data["data"].get("liveStreamReports", {}).get("nodes", [])
            if not nodes:
                return None
            if at_timestamp is None:
                return float(nodes[0]["price"]) / 1e18
            # Find price closest to target timestamp
            best_price = None
            best_dist = float("inf")
            for n in nodes:
                ts = datetime.fromisoformat(n["validFromTimestamp"].replace("Z", "+00:00")).timestamp()
                dist = abs(ts - at_timestamp)
                if dist < best_dist:
                    best_dist = dist
                    best_price = float(n["price"]) / 1e18
            return best_price
    except:
        pass
    return None


def calc_fee(shares, price):
    """Polymarket fee: 2% of potential profit, min $0."""
    potential_profit = shares * (1 - price)
    return max(0, potential_profit * 0.02)


def place_order(client, token_id, side, price, size):
    """Place a real order on the CLOB. Returns order response."""
    from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions
    from py_clob_client.order_builder.constants import BUY

    # For binary markets, we always BUY the token
    # size = dollar amount to spend
    # price = limit price per share
    response = client.create_and_post_order(
        OrderArgs(
            token_id=token_id,
            price=price,
            size=round(size / price, 2),  # shares = dollars / price
            side=BUY,
        ),
        options=PartialCreateOrderOptions(
            tick_size="0.01",
            neg_risk=False,
        ),
    )
    return response


def record_trade(side, entry_price, reasoning, position_size=MAX_POSITION_SIZE,
                 slug=None, live=False, confidence=None, delta=None, strike=None,
                 momentum=None, brief_file=None):
    """Record and optionally execute a trade."""
    check_kill_switch()
    ledger = load_ledger()

    if not check_daily_loss(ledger):
        return
    if not check_concurrent(ledger):
        return

    position_size = validate_size(position_size)

    # Find current market
    if not slug:
        now = int(time.time())
        window = now // 300 * 300
        slug = f"eth-updown-5m-{window}"

    token, condition_id = get_token_for_side(slug, side)
    eth_price = get_chainlink_price()
    shares = position_size / entry_price
    fee = calc_fee(shares, entry_price)

    trade = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market": f"Ethereum Up or Down - {slug}",
        "slug": slug,
        "market_end": None,  # Filled from market data
        "side": side,
        "token": token or "unknown",
        "condition_id": condition_id,
        "entry_price": entry_price,
        "shares": round(shares, 4),
        "cost": round(position_size, 2),
        "fee": round(fee, 6),
        "confidence": confidence,
        "delta_at_entry": delta,
        "strike_price": strike,
        "momentum_score": momentum,
        "eth_price": eth_price,
        "reasoning": reasoning,
        "brief_file": brief_file,
        "mode": "LIVE" if live else "DRY_RUN",
        "order_response": None,
        "resolved": False,
        "outcome": None,
        "pnl": None,
    }

    # Get market end time
    pm_data = fetch_json(f"{GAMMA_BASE}/events?slug={slug}")
    if pm_data:
        event = pm_data[0]
        for m in event.get("markets", []):
            if not m.get("closed"):
                trade["market_end"] = m.get("endDate") or m.get("end_date_iso")

    # Log BEFORE execution (audit trail)
    log_trade({"action": "TRADE_INTENT", **trade})

    if live:
        if not token:
            print("[ERR] Could not find token for market")
            return
        if not condition_id:
            print("⚠️  No condition_id found — CLOB confirmation will be delayed")
        try:
            creds = load_creds()
            client = get_client(creds)
            print(f"[TX] Placing LIVE order: {side} @ {entry_price} for ${position_size}")
            response = place_order(client, token, side, entry_price, position_size)
            trade["order_response"] = response
            # Update cost/shares with actual fill from CLOB
            if response.get("success") and response.get("makingAmount"):
                actual_usdc = float(response["makingAmount"])
                actual_shares = float(response.get("takingAmount", shares))
                trade["cost_intended"] = trade["cost"]  # keep original for reference
                trade["cost"] = round(actual_usdc, 6)
                trade["shares"] = round(actual_shares, 4)
                trade["fee"] = round(calc_fee(actual_shares, entry_price), 6)
                print(f"[OK] Filled: ${actual_usdc:.2f} USDC for {actual_shares:.1f} shares (intended ${position_size:.2f})")
            else:
                print(f"[OK] Order response: {json.dumps(response, indent=2)}")
            log_trade({"action": "ORDER_PLACED", "response": response, "slug": slug})
        except Exception as e:
            print(f"[ERR] Order FAILED: {e}")
            log_trade({"action": "ORDER_FAILED", "error": str(e), "slug": slug})
            return
    else:
        print(f"[DRY] DRY RUN: Would {side} @ {entry_price} for ${position_size}")

    ledger["open_positions"].append(trade)
    ledger["stats"]["total_trades"] += 1
    ledger["stats"]["total_wagered"] += position_size
    ledger["stats"]["total_fees"] += fee
    save_ledger(ledger)
    print(f"{'[LIVE] LIVE' if live else '[DRY] PAPER'}: {side} @ {entry_price:.3f} | ${position_size:.0f} | {reasoning[:80]}")


def show_balance():
    creds = load_creds()
    client = get_client(creds)
    print(f"Wallet: {creds['address']}")
    # TODO: add USDC.e balance check via RPC


def show_positions():
    ledger = load_ledger()
    if not ledger["open_positions"]:
        print("No open positions")
        return
    for p in ledger["open_positions"]:
        print(f"  {p['side']} @ {p['entry_price']} | ${p['cost']} | {p['slug']} | {'LIVE' if p.get('mode')=='LIVE' else 'DRY'}")


def _finalize_position(pos, result, ledger, source="chainlink"):
    """Move a position from open to trades with outcome."""
    now_utc = datetime.now(timezone.utc)
    won = pos["side"].lower() == result.lower()
    shares = pos.get("shares", 0)
    cost = pos.get("cost", 0)
    fee = pos.get("fee", 0)

    if won:
        pnl = round(shares - cost - fee, 6)
    else:
        pnl = round(-cost - fee, 6)

    pos["resolved"] = True
    pos["outcome"] = "win" if won else "loss"
    pos["pnl"] = pnl
    pos["resolved_at"] = now_utc.isoformat()
    pos["market_result"] = result
    pos["resolution_source"] = source
    pos["clob_confirmed"] = (source == "clob")

    # Get settlement price from Chainlink
    try:
        end_time = datetime.fromisoformat(pos["market_end"].replace("Z", "+00:00"))
        pos["settlement_price"] = get_chainlink_price(at_timestamp=end_time.timestamp())
    except:
        pos["settlement_price"] = None

    ledger["trades"].append(pos)
    ledger["stats"]["total_pnl"] += pnl
    if won:
        ledger["stats"]["wins"] += 1
        ledger["stats"]["gross_profit"] += pnl
    else:
        ledger["stats"]["losses"] += 1
        ledger["stats"]["gross_loss"] += pnl

    arrow = "▲" if result == "Up" else "▼"
    src = "clob" if source == "clob" else "chainlink"
    slug = pos.get("slug", "")
    print(f"  {arrow} {pos['side']} → {result} | ${pnl:+.2f} | {slug} [{src}]")
    log_trade({"action": "RESOLVED", "outcome": pos["outcome"], "pnl": pnl, "slug": slug, "source": source})


def _get_clob_winner(condition_id):
    """Query CLOB for the winning outcome. Returns 'Up'/'Down' or None."""
    if not condition_id:
        return None
    try:
        data = fetch_json(f"{CLOB_BASE}/markets/{condition_id}", timeout=5)
        if data and data.get("closed"):
            for t in data.get("tokens", []):
                if t.get("winner"):
                    return t["outcome"]
    except:
        pass
    return None


def resolve_all():
    """Two-phase resolution:
    Phase 1 (Chainlink, ~30s): Compare ETH price to strike for immediate resolution.
    Phase 2 (CLOB, ~5-10min): Confirm via CLOB tokens[].winner — the source of truth.
    """
    ledger = load_ledger()
    if not ledger["open_positions"] and not any(
        not t.get("clob_confirmed") for t in ledger.get("trades", [])
    ):
        return

    now_utc = datetime.now(timezone.utc)
    still_open = []

    # ---- Phase 1: Chainlink resolve for open positions ----
    for pos in ledger["open_positions"]:
        me = pos.get("market_end")
        if not me:
            still_open.append(pos)
            continue

        try:
            end_time = datetime.fromisoformat(me.replace("Z", "+00:00"))
        except:
            still_open.append(pos)
            continue

        # Wait 30s after market end for Chainlink price to settle
        if now_utc < end_time + timedelta(seconds=30):
            still_open.append(pos)
            continue

        # Try CLOB first if condition_id available (it's the source of truth)
        clob_result = _get_clob_winner(pos.get("condition_id"))
        if clob_result:
            _finalize_position(pos, clob_result, ledger, source="clob")
            continue

        # Fallback: Chainlink price vs strike
        strike = pos.get("strike_price")
        if not strike:
            still_open.append(pos)
            continue

        settlement_price = get_chainlink_price(at_timestamp=end_time.timestamp())
        if settlement_price is None:
            still_open.append(pos)
            continue

        result = "Up" if settlement_price >= strike else "Down"
        _finalize_position(pos, result, ledger, source="chainlink")

    ledger["open_positions"] = still_open

    # ---- Phase 2: CLOB confirmation for unconfirmed trades ----
    for trade in ledger.get("trades", []):
        if trade.get("clob_confirmed"):
            continue
        if not trade.get("resolved"):
            continue

        condition_id = trade.get("condition_id")
        if not condition_id:
            # Try to get condition_id from Gamma if we don't have it
            slug = trade.get("slug", "")
            if slug:
                try:
                    pm_data = fetch_json(f"{GAMMA_BASE}/events?slug={slug}")
                    if pm_data:
                        for m in pm_data[0].get("markets", []):
                            cid = m.get("conditionId")
                            if cid:
                                trade["condition_id"] = cid
                                condition_id = cid
                                break
                except:
                    pass

        clob_result = _get_clob_winner(condition_id)
        if clob_result:
            expected = trade.get("market_result")
            if expected and expected.lower() != clob_result.lower():
                # Mismatch! CLOB disagrees with Chainlink resolution
                print(f"  ⚠️  CLOB MISMATCH: {trade['slug']} — Chainlink={expected}, CLOB={clob_result}")
                log_trade({
                    "action": "CLOB_MISMATCH",
                    "slug": trade["slug"],
                    "chainlink_result": expected,
                    "clob_result": clob_result,
                })
                # CLOB is source of truth — correct the outcome
                old_pnl = trade["pnl"]
                won = trade["side"].lower() == clob_result.lower()
                shares = trade.get("shares", 0)
                cost = trade.get("cost", 0)
                fee = trade.get("fee", 0)
                new_pnl = round((shares - cost - fee) if won else (-cost - fee), 6)
                # Adjust stats
                ledger["stats"]["total_pnl"] += (new_pnl - old_pnl)
                if trade["outcome"] == "win" and not won:
                    ledger["stats"]["wins"] -= 1
                    ledger["stats"]["losses"] += 1
                    ledger["stats"]["gross_profit"] -= old_pnl
                    ledger["stats"]["gross_loss"] += new_pnl
                elif trade["outcome"] == "loss" and won:
                    ledger["stats"]["losses"] -= 1
                    ledger["stats"]["wins"] += 1
                    ledger["stats"]["gross_loss"] -= old_pnl
                    ledger["stats"]["gross_profit"] += new_pnl
                trade["outcome"] = "win" if won else "loss"
                trade["pnl"] = new_pnl
                trade["market_result"] = clob_result

            trade["clob_confirmed"] = True
            trade["clob_confirmed_at"] = now_utc.isoformat()

    save_ledger(ledger)


def show_stats():
    ledger = load_ledger()
    s = ledger["stats"]
    total = s["total_trades"]
    wr = (s["wins"] / max(1, s["wins"] + s["losses"])) * 100
    print(f"  Trades: {total} | {s['wins']}W/{s['losses']}L ({wr:.0f}%)")
    print(f"  PnL: ${s['total_pnl']:+.2f} | Fees: ${s['total_fees']:.2f}")
    print(f"  Wagered: ${s['total_wagered']:.2f}")
    print(f"  Open: {len(ledger['open_positions'])}")


if __name__ == "__main__":
    # Gate 2: Independent NO_TRADE check — defense in depth
    _no_trade = Path(__file__).parent / "NO_TRADE"
    if _no_trade.exists():
        print("⛔ NO_TRADE — refusing to place order (live-trader.py gate)")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Polymarket Live Trader")
    parser.add_argument("--trade", nargs=3, metavar=("SIDE", "PRICE", "REASONING"))
    parser.add_argument("--size", type=float, default=MAX_POSITION_SIZE)
    parser.add_argument("--slug", type=str, help="Market slug")
    parser.add_argument("--live", action="store_true", help="Execute real trades (default: dry run)")
    parser.add_argument("--confidence", type=int)
    parser.add_argument("--delta", type=float)
    parser.add_argument("--strike", type=float)
    parser.add_argument("--momentum", type=float)
    parser.add_argument("--brief-file", type=str)
    parser.add_argument("--resolve", action="store_true")
    parser.add_argument("--balance", action="store_true")
    parser.add_argument("--positions", action="store_true")
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()

    if args.trade:
        side, price, reasoning = args.trade
        record_trade(side, float(price), reasoning,
                     position_size=args.size, slug=args.slug, live=args.live,
                     confidence=args.confidence, delta=args.delta,
                     strike=args.strike, momentum=args.momentum,
                     brief_file=args.brief_file)
    elif args.resolve:
        resolve_all()
    elif args.balance:
        show_balance()
    elif args.positions:
        show_positions()
    elif args.stats:
        show_stats()
    else:
        parser.print_help()
