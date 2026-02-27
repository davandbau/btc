#!/usr/bin/env python3
"""
Polymarket 5-Minute BTC Reasoning Trader — data + execution tool.

Used by the reasoning sub-agent. No LLM calls — the agent IS the brain.

Commands:
    python3 reasoning-trader.py --brief          # JSON market brief for current window
    python3 reasoning-trader.py --trade Up 0.45 "reason here"
    python3 reasoning-trader.py --resolve         # resolve open positions
    python3 reasoning-trader.py --stats           # show results
"""

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request

BOT_DIR = Path(__file__).parent
LEDGER_PATH = BOT_DIR / "ledgers" / "reasoning-15m.json"
WINDOW_SECONDS = 900
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
CHAINLINK_FEED_ID = "0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8"
CHAINLINK_API = "https://data.chain.link/api/query-timescale"

FEE_RATE = 0.25
FEE_EXPONENT = 2
POSITION_SIZE = 25.0  # Paper trading only — no real funds


def calc_fee(shares, price):
    if price <= 0 or price >= 1:
        return 0
    return shares * FEE_RATE * (price * (1 - price)) ** FEE_EXPONENT


def fetch_json(url, timeout=8):
    req = Request(url, headers={"User-Agent": "reasoning-trader/2.0"})
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
        "strategy": "reasoning",
        "trades": [],
        "open_positions": [],
        "stats": {
            "total_pnl": 0, "wins": 0, "losses": 0, "total_trades": 0,
            "gross_profit": 0, "gross_loss": 0, "total_fees": 0, "total_wagered": 0,
        },
    }


def save_ledger(ledger):
    LEDGER_PATH.parent.mkdir(exist_ok=True)
    LEDGER_PATH.write_text(json.dumps(ledger, indent=2))


# ─── Chainlink ────────────────────────────────────────────────────────
def get_chainlink_prices():
    url = (f"{CHAINLINK_API}?query=LIVE_STREAM_REPORTS_QUERY"
           f"&variables=%7B%22feedId%22%3A%22{CHAINLINK_FEED_ID}%22%7D")
    data = fetch_json(url)
    if not data:
        return []
    nodes = data.get("data", {}).get("liveStreamReports", {}).get("nodes", [])
    results = []
    for node in nodes:
        try:
            price = float(node["price"]) / (10 ** 18)
            ts_str = node["validFromTimestamp"]
            dt = datetime.fromisoformat(ts_str)
            results.append({"ts": dt.timestamp(), "price": price, "time": ts_str})
        except:
            continue
    return results


# ─── Market Brief ─────────────────────────────────────────────────────
def get_market_brief():
    """Build complete market brief for current window."""
    now = time.time()
    current_window = int(now) // WINDOW_SECONDS * WINDOW_SECONDS
    elapsed = now - current_window
    remaining = (current_window + WINDOW_SECONDS) - now

    brief = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "window_start": current_window,
        "window_start_utc": datetime.fromtimestamp(current_window, tz=timezone.utc).strftime("%H:%M:%S"),
        "window_end_utc": datetime.fromtimestamp(current_window + WINDOW_SECONDS, tz=timezone.utc).strftime("%H:%M:%S"),
        "elapsed_s": round(elapsed),
        "remaining_s": round(remaining),
    }

    # Chainlink prices
    cl_prices = get_chainlink_prices()
    if cl_prices:
        brief["chainlink_current"] = round(cl_prices[0]["price"], 2)
        best_strike = None
        best_diff = float("inf")
        for p in cl_prices:
            diff = abs(p["ts"] - current_window)
            if diff < best_diff:
                best_diff = diff
                best_strike = p["price"]
        if best_diff < 60:
            brief["strike"] = round(best_strike, 2)
            brief["delta_from_strike"] = round(cl_prices[0]["price"] - best_strike, 2)

    # Binance order book
    book = fetch_json("https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=20")
    if book:
        bids = sum(float(b[1]) for b in book["bids"])
        asks = sum(float(a[1]) for a in book["asks"])
        brief["orderbook"] = {
            "bids_btc": round(bids, 3),
            "asks_btc": round(asks, 3),
            "ratio": round(bids / asks, 2) if asks > 0 else 999,
            "signal": "BUY pressure" if bids > asks * 1.5 else "SELL pressure" if asks > bids * 1.5 else "balanced",
        }

    # Binance recent trades
    trades = fetch_json("https://api.binance.com/api/v3/aggTrades?symbol=BTCUSDT&limit=200")
    if trades:
        buy_vol = sum(float(t["q"]) for t in trades if not t["m"])
        sell_vol = sum(float(t["q"]) for t in trades if t["m"])
        total = buy_vol + sell_vol
        brief["trade_flow"] = {
            "buy_pct": round(buy_vol / total * 100, 1) if total > 0 else 50,
            "sell_pct": round(sell_vol / total * 100, 1) if total > 0 else 50,
            "signal": "buyers aggressive" if buy_vol > sell_vol * 1.3 else "sellers aggressive" if sell_vol > buy_vol * 1.3 else "even",
        }

    # Binance candles (last 15 minutes)
    klines = fetch_json("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=15")
    if klines:
        candles = []
        for k in klines:
            o, h, l, c, vol = float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])
            candles.append({
                "open": round(o, 1), "high": round(h, 1),
                "low": round(l, 1), "close": round(c, 1),
                "volume": round(vol, 3),
                "direction": "green" if c >= o else "red",
                "range": round(h - l, 1),
            })
        brief["candles_1m"] = candles

        last5 = candles[-5:]
        greens = sum(1 for c in last5 if c["direction"] == "green")
        total_move = candles[-1]["close"] - candles[-5]["open"] if len(candles) >= 5 else 0
        avg_range = sum(c["range"] for c in last5) / len(last5)
        all_highs = [c["high"] for c in candles]
        all_lows = [c["low"] for c in candles]
        brief["candle_summary"] = {
            "last_5_greens": greens,
            "last_5_reds": 5 - greens,
            "net_move_5m": round(total_move, 1),
            "avg_range_1m": round(avg_range, 1),
            "trend": "up" if greens >= 4 else "down" if greens <= 1 else "mixed",
            "recent_high": max(all_highs),
            "recent_low": min(all_lows),
        }

    # Polymarket odds and orderbook
    slug = f"btc-updown-15m-{current_window}"
    pm_data = fetch_json(f"{GAMMA_BASE}/events?slug={slug}")
    if pm_data:
        event = pm_data[0]
        for m in event.get("markets", []):
            if not m.get("closed"):
                try:
                    prices = json.loads(m.get("outcomePrices", "[]"))
                    outcomes = json.loads(m.get("outcomes", "[]"))
                    up_idx = 0 if "Up" in outcomes[0] else 1
                    tokens = json.loads(m.get("clobTokenIds", "[]"))

                    pm = {
                        "up_price": float(prices[up_idx]),
                        "down_price": float(prices[1 - up_idx]),
                        "slug": slug,
                    }

                    if len(tokens) >= 2:
                        up_token = tokens[up_idx]
                        down_token = tokens[1 - up_idx]
                        pm["up_token"] = up_token
                        pm["down_token"] = down_token

                        for side_name, token in [("up", up_token), ("down", down_token)]:
                            ob = fetch_json(f"{CLOB_BASE}/book?token_id={token}")
                            if ob:
                                asks = sorted([{"p": float(a["price"]), "s": float(a["size"])}
                                              for a in ob.get("asks", [])], key=lambda x: x["p"])
                                bids = sorted([{"p": float(b["price"]), "s": float(b["size"])}
                                              for b in ob.get("bids", [])], key=lambda x: -x["p"])
                                pm[f"{side_name}_best_ask"] = asks[0]["p"] if asks else None
                                pm[f"{side_name}_best_bid"] = bids[0]["p"] if bids else None
                                pm[f"{side_name}_ask_depth"] = round(sum(a["s"] for a in asks[:3]), 1) if asks else 0
                                pm[f"{side_name}_bid_depth"] = round(sum(b["s"] for b in bids[:3]), 1) if bids else 0

                    brief["polymarket"] = pm
                except:
                    pass

    # Funding rate
    funding = fetch_json("https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=1")
    if funding:
        brief["funding_rate"] = float(funding[0]["fundingRate"])

    # Previous 3 window results
    prev_results = []
    for i in range(1, 4):
        prev_slug = f"btc-updown-15m-{current_window - (i * WINDOW_SECONDS)}"
        prev_data = fetch_json(f"{GAMMA_BASE}/events?slug={prev_slug}")
        if prev_data:
            pe = prev_data[0]
            for m in pe.get("markets", []):
                if m.get("closed"):
                    try:
                        p2 = json.loads(m.get("outcomePrices", "[]"))
                        o2 = json.loads(m.get("outcomes", "[]"))
                        ui = 0 if "Up" in o2[0] else 1
                        won_up = float(p2[ui]) > 0.9
                        prev_results.append({"window_ago": i, "result": "Up" if won_up else "Down"})
                    except:
                        pass
    if prev_results:
        brief["prev_windows"] = prev_results

    # Our stats + recent trades
    ledger = load_ledger()
    brief["our_stats"] = ledger["stats"]
    brief["open_positions"] = len(ledger["open_positions"])
    recent = ledger["trades"][-5:]
    if recent:
        brief["recent_trades"] = [
            {"side": t["side"], "entry_price": t["entry_price"],
             "outcome": t["outcome"], "pnl": t["pnl"],
             "reasoning": t.get("reasoning", "")[:100]}
            for t in recent
        ]

    return brief


# ─── Trade Recording ─────────────────────────────────────────────────
def record_trade(side, entry_price, reasoning, position_size=POSITION_SIZE,
                  confidence=None, delta=None, strike=None, momentum=None, brief_file=None):
    now = time.time()
    current_window = int(now) // WINDOW_SECONDS * WINDOW_SECONDS
    slug = f"btc-updown-15m-{current_window}"
    end_ts = current_window + WINDOW_SECONDS
    end_date = datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat()

    cl = get_chainlink_prices()
    btc_price = cl[0]["price"] if cl else 0

    # Get token from Polymarket
    token = "unknown"
    pm_data = fetch_json(f"{GAMMA_BASE}/events?slug={slug}")
    if pm_data:
        event = pm_data[0]
        for m in event.get("markets", []):
            if not m.get("closed"):
                try:
                    outcomes = json.loads(m.get("outcomes", "[]"))
                    tokens = json.loads(m.get("clobTokenIds", "[]"))
                    up_idx = 0 if "Up" in outcomes[0] else 1
                    token = tokens[up_idx] if side == "Up" else tokens[1 - up_idx]
                except:
                    pass

    shares = position_size / entry_price
    fee = calc_fee(shares, entry_price)

    trade = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market": f"Bitcoin Up or Down - {slug}",
        "slug": slug,
        "market_end": end_date,
        "window_start_ts": current_window,
        "side": side,
        "token": token,
        "entry_price": entry_price,
        "shares": round(shares, 4),
        "cost": position_size,
        "fee": round(fee, 6),
        "confidence": confidence,
        "delta_at_entry": delta,
        "strike_price": strike,
        "momentum_score": momentum,
        "btc_price": round(btc_price, 2),
        "time_remaining": round((current_window + WINDOW_SECONDS) - now, 1),
        "reasoning": reasoning,
        "brief_file": brief_file,
        "resolved": False,
        "outcome": None,
        "pnl": None,
    }

    ledger = load_ledger()
    ledger["open_positions"].append(trade)
    ledger["stats"]["total_wagered"] += position_size
    save_ledger(ledger)
    print(json.dumps({"status": "ok", "trade": trade}, indent=2))


# ─── Resolution ───────────────────────────────────────────────────────
def resolve_all():
    ledger = load_ledger()
    now = datetime.now(timezone.utc)
    resolved_count = 0

    for trade in ledger["open_positions"]:
        if trade.get("resolved"):
            continue
        end_str = trade.get("market_end", "")
        if not end_str:
            continue
        try:
            end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except:
            continue
        if now < end + timedelta(seconds=30):
            continue

        data = fetch_json(f"{GAMMA_BASE}/events?slug={trade['slug']}")
        if not data:
            continue
        event = data[0]
        market = None
        for m in event.get("markets", []):
            if m.get("closed"):
                market = m
                break
        # Fallback: if market_end passed, use progressively lower price thresholds
        if not market and now > end + timedelta(seconds=30):
            secs_past = (now - end).total_seconds()
            # Lower threshold the longer we wait: 0.85 at 30s, 0.75 at 60s, 0.65 at 90s+
            threshold = max(0.65, 0.90 - secs_past / 200)
            for m in event.get("markets", []):
                try:
                    p = json.loads(m.get("outcomePrices", "[]"))
                    if p and (float(p[0]) > threshold or float(p[1]) > threshold):
                        market = m
                        break
                except:
                    pass
        if not market:
            continue

        try:
            prices = json.loads(market.get("outcomePrices", "[]"))
            outcomes = json.loads(market.get("outcomes", "[]"))
        except:
            continue
        if not prices:
            continue

        up_idx = 0 if "Up" in outcomes[0] else 1
        resolved_up = float(prices[up_idx]) > 0.9
        result = "Up" if resolved_up else "Down"
        won = trade["side"].lower() == result.lower()

        if won:
            pnl = (1.0 - trade["entry_price"]) * trade["shares"] - trade["fee"]
        else:
            pnl = -(trade["entry_price"] * trade["shares"] + trade["fee"])

        trade["resolved"] = True
        trade["outcome"] = "win" if won else "loss"
        trade["pnl"] = round(pnl, 4)
        trade["resolved_at"] = now.isoformat()
        trade["market_result"] = result

        s = ledger["stats"]
        s["total_pnl"] += pnl
        s["total_trades"] += 1
        s["total_fees"] += trade["fee"]
        if won:
            s["wins"] += 1
        else:
            s["losses"] += 1

        ledger["trades"].append(trade)
        resolved_count += 1

        emoji = "✅" if won else "❌"
        print(f"{emoji} {trade['side']} @ {trade['entry_price']:.3f} → {result} | PnL: ${pnl:+.2f} | {trade.get('reasoning','')[:60]}")

    ledger["open_positions"] = [t for t in ledger["open_positions"] if not t.get("resolved")]
    if resolved_count:
        save_ledger(ledger)
    print(f"Resolved {resolved_count} positions")


def show_stats():
    ledger = load_ledger()
    s = ledger["stats"]
    print(f"PnL: ${s['total_pnl']:+.2f} | {s['wins']}W/{s['losses']}L | wagered: ${s['total_wagered']:.0f}")
    if s["total_trades"] > 0:
        print(f"Win rate: {s['wins']/s['total_trades']*100:.0f}% | Avg: ${s['total_pnl']/s['total_trades']:+.2f}/trade")
    for t in ledger["trades"][-5:]:
        e = "✅" if t["outcome"] == "win" else "❌"
        print(f"  {e} {t['side']} @{t['entry_price']:.3f} → ${t['pnl']:+.2f} | {t.get('reasoning','')[:50]}")
    if ledger["open_positions"]:
        print(f"Open: {len(ledger['open_positions'])}")
        for t in ledger["open_positions"]:
            print(f"  ⏳ {t['side']} @{t['entry_price']:.3f} | {t.get('reasoning','')[:50]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--brief", action="store_true")
    parser.add_argument("--trade", nargs=3, metavar=("SIDE", "PRICE", "REASONING"))
    parser.add_argument("--size", type=float, default=POSITION_SIZE, help="Position size for this trade")
    parser.add_argument("--confidence", type=int, help="Confidence score 1-5")
    parser.add_argument("--delta", type=float, help="BTC delta from strike at entry")
    parser.add_argument("--strike", type=float, help="Strike price (Chainlink)")
    parser.add_argument("--momentum", type=float, help="Momentum alignment score")
    parser.add_argument("--brief-file", type=str, help="Path to saved brief JSON snapshot")
    parser.add_argument("--resolve", action="store_true")
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()

    if args.trade:
        side, price, reasoning = args.trade
        record_trade(side, float(price), reasoning, position_size=args.size,
                     confidence=args.confidence, delta=args.delta, strike=args.strike,
                     momentum=args.momentum, brief_file=args.brief_file)
    elif args.resolve:
        resolve_all()
    elif args.stats:
        show_stats()
    else:
        brief = get_market_brief()
        print(json.dumps(brief, indent=2))
