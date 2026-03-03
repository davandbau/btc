#!/usr/local/bin/python3.12
"""
Experiment: Does 10-second trade flow predict short-term BTC direction?

Every 10 seconds:
1. Snapshot taker buy/sell volume from Binance, Kraken, OKX, Bybit
2. Record BTC price at snapshot time
3. Record BTC price at +10s, +30s, +60s later
4. Save everything to CSV for analysis

Run for 2-4 hours, then analyze with flow-analysis.py
"""

import json, urllib.request, time, csv, os, threading
from datetime import datetime, timezone
from collections import deque

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_FILE = os.path.join(OUT_DIR, f"flow-data-{datetime.now().strftime('%Y%m%d-%H%M')}.csv")

# Price buffer for lookback
price_history = deque(maxlen=600)  # 10 min of per-second prices
current_price = None

def get(url, timeout=3):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())

def binance_price_loop():
    """Poll Binance price every second for the lookback buffer."""
    global current_price
    while True:
        try:
            d = get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
            current_price = float(d["price"])
            price_history.append((time.time(), current_price))
        except:
            pass
        time.sleep(1)

def get_flow_snapshot():
    """Get taker buy/sell BTC volume from last ~10 seconds across exchanges."""
    now = time.time()
    cutoff_ms = int((now - 10) * 1000)
    flows = {}

    # Binance — has timestamp per trade, can filter precisely
    try:
        trades = get("https://api.binance.com/api/v3/aggTrades?symbol=BTCUSDT&limit=200")
        recent = [t for t in trades if t["T"] >= cutoff_ms]
        buy = sum(float(t["q"]) for t in recent if not t["m"])
        sell = sum(float(t["q"]) for t in recent if t["m"])
        flows["binance"] = {"buy": buy, "sell": sell, "n": len(recent)}
    except:
        pass

    # Kraken
    try:
        kr = get("https://api.kraken.com/0/public/Trades?pair=XBTUSD&count=200")
        kr_trades = list(kr["result"].values())[0]
        recent = [t for t in kr_trades if float(t[2]) >= now - 10]
        buy = sum(float(t[1]) for t in recent if t[3] == "b")
        sell = sum(float(t[1]) for t in recent if t[3] == "s")
        flows["kraken"] = {"buy": buy, "sell": sell, "n": len(recent)}
    except:
        pass

    # OKX
    try:
        ok = get("https://www.okx.com/api/v5/market/trades?instId=BTC-USDT&limit=100")["data"]
        recent = [t for t in ok if int(t["ts"]) >= cutoff_ms]
        buy = sum(float(t["sz"]) for t in recent if t["side"] == "buy")
        sell = sum(float(t["sz"]) for t in recent if t["side"] == "sell")
        flows["okx"] = {"buy": buy, "sell": sell, "n": len(recent)}
    except:
        pass

    # Bybit
    try:
        bb = get("https://api.bybit.com/v5/market/recent-trade?category=spot&symbol=BTCUSDT&limit=200")["result"]["list"]
        recent = [t for t in bb if int(t["time"]) >= cutoff_ms]
        buy = sum(float(t["size"]) for t in recent if t["side"] == "Buy")
        sell = sum(float(t["size"]) for t in recent if t["side"] == "Sell")
        flows["bybit"] = {"buy": buy, "sell": sell, "n": len(recent)}
    except:
        pass

    # Aggregate
    total_buy = sum(f["buy"] for f in flows.values())
    total_sell = sum(f["sell"] for f in flows.values())
    total = total_buy + total_sell

    return {
        "exchanges": flows,
        "total_buy": total_buy,
        "total_sell": total_sell,
        "total_vol": total,
        "buy_pct": round(total_buy / total * 100, 1) if total > 0 else 50,
        "net_flow": total_buy - total_sell,  # positive = buying pressure
        "n_exchanges": len(flows),
    }

def get_price_at(target_time):
    """Get the closest price to target_time from history."""
    best = None
    best_dist = float("inf")
    for ts, price in price_history:
        d = abs(ts - target_time)
        if d < best_dist:
            best_dist = d
            best = price
    return best if best_dist < 3 else None  # within 3 seconds

def main():
    print(f"Flow Correlation Experiment")
    print(f"Output: {CSV_FILE}")
    print(f"Sampling every 10s. Ctrl+C to stop.\n")

    # Start price polling thread
    t = threading.Thread(target=binance_price_loop, daemon=True)
    t.start()

    # Wait for initial price
    time.sleep(3)

    # CSV setup
    fields = [
        "timestamp", "utc_time",
        "price_now",
        "total_buy_btc", "total_sell_btc", "total_vol_btc",
        "buy_pct", "net_flow_btc", "n_exchanges",
        "bin_buy", "bin_sell", "bin_n",
        "kr_buy", "kr_sell", "kr_n",
        "okx_buy", "okx_sell", "okx_n",
        "bb_buy", "bb_sell", "bb_n",
        # These get filled in later by the analysis script
        "price_10s", "price_30s", "price_60s",
        "delta_10s", "delta_30s", "delta_60s",
    ]

    with open(CSV_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()

    # Pending rows that need future prices
    pending = deque()  # (timestamp, row_index)
    all_rows = []

    sample = 0
    try:
        while True:
            now = time.time()
            flow = get_flow_snapshot()
            price = current_price

            if not price:
                time.sleep(10)
                continue

            sample += 1
            row = {
                "timestamp": now,
                "utc_time": datetime.fromtimestamp(now, tz=timezone.utc).strftime("%H:%M:%S"),
                "price_now": round(price, 2),
                "total_buy_btc": round(flow["total_buy"], 4),
                "total_sell_btc": round(flow["total_sell"], 4),
                "total_vol_btc": round(flow["total_vol"], 4),
                "buy_pct": flow["buy_pct"],
                "net_flow_btc": round(flow["net_flow"], 4),
                "n_exchanges": flow["n_exchanges"],
            }

            for ex, key in [("binance","bin"), ("kraken","kr"), ("okx","okx"), ("bybit","bb")]:
                d = flow["exchanges"].get(ex, {})
                row[f"{key}_buy"] = round(d.get("buy", 0), 4)
                row[f"{key}_sell"] = round(d.get("sell", 0), 4)
                row[f"{key}_n"] = d.get("n", 0)

            all_rows.append(row)
            pending.append((now, len(all_rows) - 1))

            # Fill in future prices for old rows
            filled = []
            for pend_time, idx in pending:
                r = all_rows[idx]
                for offset, label in [(10, "10s"), (30, "30s"), (60, "60s")]:
                    key_p = f"price_{label}"
                    key_d = f"delta_{label}"
                    if not r.get(key_p):
                        future_price = get_price_at(pend_time + offset)
                        if future_price:
                            r[key_p] = round(future_price, 2)
                            r[key_d] = round(future_price - r["price_now"], 2)

                # Check if all filled
                if all(r.get(f"price_{l}") for l in ["10s", "30s", "60s"]):
                    filled.append((pend_time, idx))

            for item in filled:
                pending.remove(item)

            # Rewrite CSV with all data
            with open(CSV_FILE, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                for r in all_rows:
                    writer.writerow(r)

            # Log
            arrow = "▲" if flow["net_flow"] > 0 else "▼"
            ts = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%H:%M:%S")
            print(f"  [{ts}] #{sample:4d} | ${price:,.2f} | "
                  f"Buy {flow['buy_pct']:5.1f}% | "
                  f"Net {flow['net_flow']:+.3f} BTC {arrow} | "
                  f"Vol {flow['total_vol']:.3f} | "
                  f"{flow['n_exchanges']}ex | "
                  f"pending: {len(pending)}")

            time.sleep(10)

    except KeyboardInterrupt:
        print(f"\nStopped. {len(all_rows)} samples saved to {CSV_FILE}")
        # Final fill pass
        time.sleep(2)
        for pend_time, idx in pending:
            r = all_rows[idx]
            for offset, label in [(10, "10s"), (30, "30s"), (60, "60s")]:
                key_p = f"price_{label}"
                key_d = f"delta_{label}"
                if not r.get(key_p):
                    future_price = get_price_at(pend_time + offset)
                    if future_price:
                        r[key_p] = round(future_price, 2)
                        r[key_d] = round(future_price - r["price_now"], 2)

        with open(CSV_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for r in all_rows:
                writer.writerow(r)
        print(f"Final save complete.")

if __name__ == "__main__":
    main()
