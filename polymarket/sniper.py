#!/usr/local/bin/python3.12
"""
Sniper Bot — The David Strategy

Late-window entries using multi-exchange websocket feeds to see 3-5 seconds
into the future. Buys the obvious side when exchange momentum confirms direction.

Key principles:
- Delta threshold is a gliding scale (linear, not steps)
- Exchange prices lead Polymarket by 3-5 seconds
- DCA into position across multiple small buys
- One direction per window
- No LLM needed — pure price + momentum

Usage:
  python3.12 sniper.py              # Paper mode (log only)
  python3.12 sniper.py --live       # Live trading
"""

import asyncio
import json
import time
import sys
import signal
import urllib.request
from datetime import datetime, timezone
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

# ─── Config ───
BOT_DIR = Path(__file__).parent
CREDS_FILE = BOT_DIR.parent / ".polymarket-creds.json"
LEDGER_FILE = BOT_DIR / "ledgers" / "sniper.json"
LOG_DIR = BOT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LEDGER_FILE.parent.mkdir(exist_ok=True)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
CHAINLINK_FEED_ID = "0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8"
CHAINLINK_API = "https://data.chain.link/api/query-timescale"

# Gliding scale: linear interpolation
# (seconds_remaining, min_delta) — anything between is interpolated
DELTA_SCALE = [
    (120, 80),   # 2 min left → need $80+
    (60, 30),    # 1 min left → need $30+
    (30, 15),    # 30s left → need $15+
    (10, 5),     # 10s left → need $5+
    (3, 2),      # 3s left → need $2+
]

# Momentum: need N of M exchange ticks in same direction over last ~5s
MOMENTUM_WINDOW_SECS = 5
MOMENTUM_MIN_EXCHANGES = 3  # need at least 3 exchanges confirming

# DCA
MAX_POSITION_PER_WINDOW = 80
DCA_CHUNK = 20
MAX_ENTRIES = 4
MIN_ENTRY_GAP_S = 3

# Price limits
MAX_ASK_PRICE = 0.95
MIN_PROFIT_MARGIN = 0.02  # 2¢ after fees

# Safety
DAILY_LOSS_LIMIT = 200
WINDOW_SECONDS = 300
KILL_SWITCH = Path.home() / "POLY_KILL"
ENTRY_ZONE_START = 60   # start scanning with 1 min left
ENTRY_ZONE_END = 3      # stop 3s before close (order needs time to fill)

# ─── State ───
@dataclass
class WindowState:
    window_start: int = 0
    strike: float = 0
    up_token: str = ""
    down_token: str = ""
    condition_id: str = ""
    slug: str = ""
    entries: list = field(default_factory=list)
    total_cost: float = 0
    total_shares: float = 0
    last_entry_time: float = 0
    side: str = ""  # locked after first entry

exchange_prices = {}  # name -> deque of (timestamp, price)
window = WindowState()
live_mode = "--live" in sys.argv
running = True
clob_client = None

# ─── Logging ───
def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"  [{ts}] {msg}", flush=True)

def log_header():
    mode = "LIVE" if live_mode else "PAPER"
    print(f"{'='*65}")
    print(f"◈ Polymarket Sniper Bot — The David Strategy")
    print(f"   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"   Mode: {mode}")
    print(f"   Delta: {DELTA_SCALE[0][1]}$ @{DELTA_SCALE[0][0]}s → {DELTA_SCALE[-1][1]}$ @{DELTA_SCALE[-1][0]}s (linear)")
    print(f"   Momentum: {MOMENTUM_MIN_EXCHANGES}+ exchanges confirming over {MOMENTUM_WINDOW_SECS}s")
    print(f"   DCA: ${DCA_CHUNK} x {MAX_ENTRIES} (max ${MAX_POSITION_PER_WINDOW}/window)")
    print(f"   Entry zone: {ENTRY_ZONE_START}s → {ENTRY_ZONE_END}s before close")
    print(f"{'='*65}\n")

# ─── Helpers ───
def fetch_json(url, timeout=5):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "sniper/1.0"})
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except:
        return None

def required_delta(seconds_remaining):
    """Linear interpolation on the gliding scale."""
    if seconds_remaining >= DELTA_SCALE[0][0]:
        return DELTA_SCALE[0][1]
    if seconds_remaining <= DELTA_SCALE[-1][0]:
        return DELTA_SCALE[-1][1]
    for i in range(len(DELTA_SCALE) - 1):
        t1, d1 = DELTA_SCALE[i]
        t2, d2 = DELTA_SCALE[i + 1]
        if t2 <= seconds_remaining <= t1:
            ratio = (seconds_remaining - t2) / (t1 - t2)
            return d2 + ratio * (d1 - d2)
    return DELTA_SCALE[0][1]

def get_exchange_avg():
    """Average price across all exchanges (latest tick each, within 10s)."""
    now = time.time()
    prices = []
    for ticks in exchange_prices.values():
        recent = [(t, p) for t, p in ticks if now - t < 10]
        if recent:
            prices.append(recent[-1][1])
    return sum(prices) / len(prices) if prices else None

def get_exchange_momentum(direction):
    """Check if exchanges moved in direction over last few seconds."""
    now = time.time()
    agreeing = 0
    total = 0
    for ex_name, ticks in exchange_prices.items():
        recent = [(t, p) for t, p in ticks if now - t < MOMENTUM_WINDOW_SECS + 1]
        if len(recent) < 2:
            continue
        prices = [p for _, p in sorted(recent)]
        trend = prices[-1] - prices[0]
        total += 1
        if direction == "Up" and trend > 0:
            agreeing += 1
        elif direction == "Down" and trend < 0:
            agreeing += 1
    confirmed = agreeing >= MOMENTUM_MIN_EXCHANGES and total >= MOMENTUM_MIN_EXCHANGES
    return confirmed, agreeing, total

def get_book_ask(token_id):
    """Best ask price for a token."""
    book = fetch_json(f"{CLOB_BASE}/book?token_id={token_id}")
    if not book:
        return None
    asks = sorted(book.get("asks", []), key=lambda x: float(x["price"]))
    return float(asks[0]["price"]) if asks else None

# ─── Ledger ───
def load_ledger():
    if LEDGER_FILE.exists():
        return json.loads(LEDGER_FILE.read_text())
    return {
        "strategy": "sniper",
        "trades": [],
        "open_positions": [],
        "stats": {"total_pnl": 0, "wins": 0, "losses": 0, "total_trades": 0,
                  "total_wagered": 0, "total_fees": 0},
    }

def save_ledger(ledger):
    LEDGER_FILE.write_text(json.dumps(ledger, indent=2))

def record_window(window_state, outcome=None, pnl=None):
    """Record completed window to ledger."""
    if not window_state.entries:
        return
    ledger = load_ledger()
    trade = {
        "timestamp": datetime.fromtimestamp(window_state.window_start, tz=timezone.utc).isoformat(),
        "slug": window_state.slug,
        "strike": window_state.strike,
        "side": window_state.side,
        "entries": window_state.entries,
        "total_cost": round(window_state.total_cost, 2),
        "total_shares": round(window_state.total_shares, 2),
        "avg_price": round(window_state.total_cost / window_state.total_shares, 4) if window_state.total_shares else 0,
        "condition_id": window_state.condition_id,
        "up_token": window_state.up_token,
        "down_token": window_state.down_token,
        "resolved": outcome is not None,
        "outcome": outcome,
        "pnl": round(pnl, 2) if pnl is not None else None,
        "mode": "LIVE" if live_mode else "PAPER",
    }
    ledger["trades"].append(trade)
    if outcome is not None:
        if pnl and pnl > 0:
            ledger["stats"]["wins"] += 1
        elif pnl is not None:
            ledger["stats"]["losses"] += 1
        ledger["stats"]["total_pnl"] = round(ledger["stats"]["total_pnl"] + (pnl or 0), 2)
    ledger["stats"]["total_trades"] += 1
    ledger["stats"]["total_wagered"] = round(ledger["stats"]["total_wagered"] + window_state.total_cost, 2)
    save_ledger(ledger)

# ─── CLOB Client ───
def init_clob_client():
    global clob_client
    if not live_mode:
        return
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        creds = json.loads(CREDS_FILE.read_text())
        clob_client = ClobClient(
            CLOB_BASE,
            key=creds["privateKey"],
            chain_id=137,
            creds=ApiCreds(
                api_key=creds["apiKey"],
                api_secret=creds["apiSecret"],
                api_passphrase=creds["apiPassphrase"],
            ),
            signature_type=1,
            funder=creds["address"],
        )
        log("CLOB client initialized")
    except Exception as e:
        log(f"⚠ CLOB client init failed: {e}")
        clob_client = None

def place_order(direction, token_id, price, size_usd):
    """Place order. Returns dict with fill info."""
    shares = round(size_usd / price, 2)
    
    if not live_mode:
        log(f"    [PAPER] BUY {direction} {shares:.1f} shares @ {price:.3f} = ${size_usd:.2f}")
        return {"filled": True, "shares": shares, "cost": size_usd, "price": price}
    
    if not clob_client:
        log("    ⚠ No CLOB client — skipping")
        return None
    
    if KILL_SWITCH.exists():
        log("    ⚠ KILL SWITCH active")
        return None
    
    try:
        from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions
        from py_clob_client.order_builder.constants import BUY
        
        response = clob_client.create_and_post_order(
            OrderArgs(token_id=token_id, price=price, size=shares, side=BUY),
            options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=False),
        )
        
        if response.get("success") or response.get("orderID"):
            try:
                actual_cost = float(response["makingAmount"]) if response.get("makingAmount") else size_usd
            except (ValueError, TypeError):
                actual_cost = size_usd
            try:
                actual_shares = float(response["takingAmount"]) if response.get("takingAmount") else shares
            except (ValueError, TypeError):
                actual_shares = shares
            log(f"    [LIVE] Filled: {actual_shares:.1f} shares @ {price:.3f} = ${actual_cost:.2f}")
            return {"filled": True, "shares": actual_shares, "cost": actual_cost, "price": price,
                    "order_id": response.get("orderID", "")}
        else:
            log(f"    ⚠ Order not filled: {json.dumps(response)[:200]}")
            return None
    except Exception as e:
        log(f"    ⚠ Order error: {e}")
        return None

# ─── Resolution ───
async def resolve_window(ws):
    """Check CLOB for window resolution after it closes."""
    if not ws.entries:
        return
    
    # Wait for CLOB to resolve (typically 5-10 min)
    await asyncio.sleep(60)  # initial wait
    
    for attempt in range(10):
        try:
            if ws.condition_id:
                market = fetch_json(f"{CLOB_BASE}/markets/{ws.condition_id}")
                if market:
                    tokens = market.get("tokens", [])
                    for tok in tokens:
                        if tok.get("winner"):
                            winner_id = tok.get("token_id")
                            our_token = ws.up_token if ws.side == "Up" else ws.down_token
                            won = winner_id == our_token
                            pnl = (ws.total_shares - ws.total_cost) if won else -ws.total_cost
                            fee = ws.total_shares * min(ws.entries[0]["price"], 1 - ws.entries[0]["price"]) * 0.02
                            pnl -= fee
                            result = "WIN" if won else "LOSS"
                            log(f"■  Resolved: {result} | {ws.side} | PnL: ${pnl:+.2f} | "
                                f"cost: ${ws.total_cost:.2f} | shares: {ws.total_shares:.1f}")
                            record_window(ws, outcome=result, pnl=pnl)
                            
                            # Auto-redeem disabled — smart contract wallet can't receive native POL for gas
                            # David redeems manually on polymarket.com
                            if False and won and live_mode and ws.condition_id:
                                for redeem_attempt in range(5):
                                    await asyncio.sleep(10 if redeem_attempt == 0 else 30)
                                    try:
                                        import subprocess
                                        r = subprocess.run(
                                            ["/usr/local/bin/python3.12",
                                             str(BOT_DIR / "redeem.py"),
                                             ws.condition_id],
                                            capture_output=True, text=True, timeout=30
                                        )
                                        output = (r.stdout or "").strip()
                                        for line in output.split("\n"):
                                            if line.strip():
                                                log(f"  {line.strip()}")
                                        if "Redeemed!" in output:
                                            break
                                        if "failed" in output.lower() or r.returncode != 0:
                                            log(f"  ⚠ Redeem attempt {redeem_attempt+1}/5 failed, retrying...")
                                    except Exception as e:
                                        log(f"  ⚠ Redeem attempt {redeem_attempt+1}/5 error: {e}")
                            return
        except Exception as e:
            pass
        
        await asyncio.sleep(60)  # retry every 60s
    
    # Timed out — record unresolved
    log(f"⚠  Resolution timeout for {ws.slug}")
    record_window(ws)

# ─── Market Discovery ───
def discover_market():
    """Get current window's strike, tokens."""
    now = int(time.time())
    current_window = now - (now % WINDOW_SECONDS)
    slug = f"btc-updown-5m-{current_window}"
    
    info = {"window_start": current_window, "slug": slug,
            "strike": None, "up_token": None, "down_token": None, "condition_id": None}
    
    data = fetch_json(f"{GAMMA_BASE}/events?slug={slug}")
    if data:
        for event in data:
            for m in event.get("markets", []):
                if not m.get("closed"):
                    try:
                        outcomes = json.loads(m.get("outcomes", "[]"))
                        tokens = json.loads(m.get("clobTokenIds", "[]"))
                        up_idx = 0 if "Up" in outcomes[0] else 1
                        info["up_token"] = tokens[up_idx]
                        info["down_token"] = tokens[1 - up_idx]
                        info["condition_id"] = m.get("conditionId", "")
                    except:
                        pass
    
    # Strike from Polymarket's own API (authoritative)
    try:
        start_utc = datetime.fromtimestamp(current_window, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_utc = datetime.fromtimestamp(current_window + WINDOW_SECONDS, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        pm_url = f"https://polymarket.com/api/crypto/crypto-price?symbol=BTC&eventStartTime={start_utc}&variant=fiveminute&endDate={end_utc}"
        pm_data = fetch_json(pm_url)
        if pm_data and pm_data.get("openPrice"):
            info["strike"] = pm_data["openPrice"]
    except:
        pass
    
    # Fallback to Chainlink if PM API fails
    if not info["strike"]:
        cl = fetch_json(f"{CHAINLINK_API}?query=LIVE_STREAM_REPORTS_QUERY&variables=%7B%22feedId%22%3A%22{CHAINLINK_FEED_ID}%22%7D")
        if cl and "data" in cl:
            nodes = cl["data"].get("liveStreamReports", {}).get("nodes", [])
            best_price, best_dist = None, float("inf")
            for n in nodes[:60]:
                ts = datetime.fromisoformat(n["validFromTimestamp"].replace("Z", "+00:00")).timestamp()
                d = abs(ts - current_window)
                if d < best_dist:
                    best_dist = d
                    best_price = float(n["price"]) / 1e18
            info["strike"] = best_price
    
    return info

# ─── Daily P&L Check ───
def check_daily_limit():
    ledger = load_ledger()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_pnl = sum(t.get("pnl", 0) for t in ledger["trades"]
                    if t.get("timestamp", "").startswith(today) and t.get("resolved"))
    if daily_pnl < -DAILY_LOSS_LIMIT:
        log(f"⚠ DAILY LOSS LIMIT (${daily_pnl:.2f}) — stopping")
        return False
    return True

# ─── Websocket Feeds ───
async def exchange_feed(name, url, subscribe_msg, parse_fn):
    """Generic exchange websocket feed."""
    import websockets
    while running:
        try:
            async with websockets.connect(url) as ws:
                if subscribe_msg:
                    await ws.send(json.dumps(subscribe_msg))
                log(f"{name} connected")
                if name not in exchange_prices:
                    exchange_prices[name] = deque(maxlen=200)
                async for msg in ws:
                    if not running:
                        break
                    price = parse_fn(msg)
                    if price:
                        exchange_prices[name].append((time.time(), price))
        except Exception as e:
            if running:
                await asyncio.sleep(3)

def parse_binance(msg):
    d = json.loads(msg)
    return float(d.get("p", 0)) or None

def parse_coinbase(msg):
    d = json.loads(msg)
    return float(d["price"]) if d.get("type") == "ticker" and d.get("price") else None

def parse_kraken(msg):
    d = json.loads(msg)
    if d.get("channel") == "ticker" and d.get("data"):
        return float(d["data"][0].get("last", 0)) or None
    return None

def parse_okx(msg):
    d = json.loads(msg)
    if d.get("data") and d["data"][0].get("last"):
        return float(d["data"][0]["last"])
    return None

def parse_bybit(msg):
    d = json.loads(msg)
    if d.get("data") and d["data"].get("lastPrice"):
        return float(d["data"]["lastPrice"])
    return None

# ─── Main Loop ───
async def sniper_loop():
    global window, running
    
    last_window = 0
    resolve_tasks = []
    
    while running:
        try:
            now = int(time.time())
            current_window = now - (now % WINDOW_SECONDS)
            elapsed = now - current_window
            remaining = WINDOW_SECONDS - elapsed
            
            # New window
            if current_window != last_window:
                # Close previous window
                if last_window > 0 and window.entries:
                    log(f"■  Window closed — {len(window.entries)} entries, "
                        f"${window.total_cost:.2f} cost, {window.total_shares:.1f} shares, "
                        f"avg {window.total_cost/window.total_shares:.3f}")
                    # Spawn resolution task
                    prev = WindowState(**{k: getattr(window, k) for k in window.__dataclass_fields__})
                    prev.entries = list(window.entries)
                    task = asyncio.create_task(resolve_window(prev))
                    resolve_tasks.append(task)
                
                window = WindowState(window_start=current_window)
                last_window = current_window
                
                # Wait for PM API to have the correct strike
                await asyncio.sleep(5)
                
                # Discover market
                info = discover_market()
                if info.get("strike"):
                    window.strike = info["strike"]
                    window.up_token = info.get("up_token", "")
                    window.down_token = info.get("down_token", "")
                    window.condition_id = info.get("condition_id", "")
                    window.slug = info["slug"]
                    ts_str = datetime.fromtimestamp(current_window, tz=timezone.utc).strftime("%H:%M")
                    log(f"►  Window {ts_str} | Strike ${window.strike:,.2f} | "
                        f"tokens: {'ok' if window.up_token else 'MISSING'}")
            
            # Only scan in entry zone
            if remaining > ENTRY_ZONE_START or remaining < ENTRY_ZONE_END:
                await asyncio.sleep(0.5)
                continue
            
            # Guards
            if not window.strike or not window.up_token:
                await asyncio.sleep(1)
                continue
            if len(window.entries) >= MAX_ENTRIES or window.total_cost >= MAX_POSITION_PER_WINDOW:
                await asyncio.sleep(1)
                continue
            if time.time() - window.last_entry_time < MIN_ENTRY_GAP_S:
                await asyncio.sleep(0.5)
                continue
            if not check_daily_limit():
                running = False
                break
            
            # Exchange average
            avg_price = get_exchange_avg()
            if not avg_price:
                await asyncio.sleep(0.5)
                continue
            
            # Delta + gliding scale
            delta = avg_price - window.strike
            abs_delta = abs(delta)
            direction = "Up" if delta > 0 else "Down"
            min_delta = required_delta(remaining)
            
            if abs_delta < min_delta:
                await asyncio.sleep(0.5)
                continue
            
            # Direction lock
            if window.side and window.side != direction:
                await asyncio.sleep(0.5)
                continue
            
            # Momentum confirmation
            confirmed, agreeing, total = get_exchange_momentum(direction)
            if not confirmed:
                await asyncio.sleep(0.5)
                continue
            
            # Book price
            token_id = window.up_token if direction == "Up" else window.down_token
            ask = get_book_ask(token_id)
            if not ask or ask > MAX_ASK_PRICE:
                await asyncio.sleep(0.5)
                continue
            
            # Profit margin check
            fee_pct = min(ask, 1 - ask) * 0.02
            margin = (1 - ask) - fee_pct
            if margin < MIN_PROFIT_MARGIN:
                await asyncio.sleep(0.5)
                continue
            
            # ─── ENTRY ───
            entry_num = len(window.entries) + 1
            log(f"▲  DCA #{entry_num} | {direction} | Δ={delta:+.1f} (need {min_delta:.0f}) | "
                f"ask={ask:.3f} | {remaining:.0f}s left | {agreeing}/{total} ex confirm | "
                f"margin: {margin*100:.1f}%")
            
            result = place_order(direction, token_id, ask, DCA_CHUNK)
            
            if result is None:
                # API error (425, rate limit, etc.) — back off instead of spamming
                log(f"    ◈ Backing off 5s after order error")
                await asyncio.sleep(5)
                continue
            
            if result.get("filled"):
                window.entries.append({
                    "time": time.time(),
                    "utc": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                    "direction": direction,
                    "price": result["price"],
                    "cost": result["cost"],
                    "shares": result["shares"],
                    "delta": round(delta, 2),
                    "remaining_s": round(remaining, 1),
                    "momentum": f"{agreeing}/{total}",
                })
                window.total_cost += result["cost"]
                window.total_shares += result["shares"]
                window.last_entry_time = time.time()
                window.side = direction
                
                log(f"    ✓ ${window.total_cost:.0f}/{MAX_POSITION_PER_WINDOW} "
                    f"({entry_num}/{MAX_ENTRIES}) | "
                    f"avg: {window.total_cost/window.total_shares:.3f}")
            
        except Exception as e:
            log(f"⚠  Error: {e}")
        
        await asyncio.sleep(0.5)
    
    # Record any final open window
    if window.entries:
        record_window(window)

async def main():
    global running
    
    log_header()
    init_clob_client()
    
    feeds = [
        ("Binance", "wss://stream.binance.com:9443/ws/btcusdt@trade", None, parse_binance),
        ("Coinbase", "wss://ws-feed.exchange.coinbase.com",
         {"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["ticker"]}, parse_coinbase),
        ("Kraken", "wss://ws.kraken.com/v2",
         {"method": "subscribe", "params": {"channel": "ticker", "symbol": ["BTC/USD"]}}, parse_kraken),
        ("OKX", "wss://ws.okx.com:8443/ws/v5/public",
         {"op": "subscribe", "args": [{"channel": "tickers", "instId": "BTC-USDT"}]}, parse_okx),
        ("Bybit", "wss://stream.bybit.com/v5/public/spot",
         {"op": "subscribe", "args": ["tickers.BTCUSDT"]}, parse_bybit),
    ]
    
    tasks = [asyncio.create_task(exchange_feed(*f)) for f in feeds]
    tasks.append(asyncio.create_task(sniper_loop()))
    
    await asyncio.sleep(3)
    connected = len(exchange_prices)
    log(f"►  {connected} exchanges connected — scanning\n")
    
    def shutdown(sig, frame):
        global running
        log("\nShutting down...")
        running = False
    
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        pass

if __name__ == "__main__":
    asyncio.run(main())
