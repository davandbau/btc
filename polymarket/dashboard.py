#!/usr/bin/env python3
"""
Polymarket Trading Desk — Real-time web UI.

Serves a live dashboard at http://localhost:8850 that auto-refreshes
with data from all strategy ledgers.

Usage:
    python3 dashboard.py              # start on port 8851
    python3 dashboard.py --port 9000  # custom port
"""

import argparse
import base64
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

BOT_DIR = Path(__file__).parent
LEDGER_DIR = BOT_DIR / "ledgers"
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
CHAINLINK_FEED = "0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8"

DASH_USER = "david"
DASH_PASS = "polymarket2026"


def fetch_json(url, timeout=8):
    req = Request(url, headers={"User-Agent": "polymarket-dashboard/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except:
        return None


def get_chainlink_price():
    """Fetch current BTC/USD from Chainlink."""
    try:
        url = f"https://data.chain.link/api/query-timescale?query=LIVE_STREAM_REPORTS_QUERY&variables=%7B%22feedId%22%3A%22{CHAINLINK_FEED}%22%7D"
        data = fetch_json(url, timeout=5)
        if data and "data" in data:
            nodes = data["data"].get("liveStreamReports", {}).get("nodes", [])
            if nodes:
                return float(nodes[0]["price"]) / 1e18
    except:
        pass
    return None


def get_live_prices(positions):
    """Get live prices from CLOB midpoint endpoint (same source as Polymarket UI)."""
    prices = {}
    for pos in positions:
        slug = pos.get("slug", "")
        token = pos.get("token", "")
        if not slug or slug in prices:
            continue
        
        side = pos.get("side", "Up")
        
        if token:
            mid = fetch_json(f"{CLOB_BASE}/midpoint?token_id={token}", timeout=5)
            if mid and mid.get("mid"):
                midpoint = float(mid["mid"])
                price_map = {}
                for case in [side, side.upper(), side.lower(), side.capitalize()]:
                    price_map[case] = midpoint
                other = "Down" if side.capitalize() == "Up" else "Up"
                for case in [other, other.upper(), other.lower()]:
                    price_map[case] = round(1 - midpoint, 3)
                prices[slug] = price_map
    return prices


def get_all_ledgers():
    """Load all strategy ledgers."""
    ledgers = {}
    if not LEDGER_DIR.exists():
        return ledgers
    for f in LEDGER_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            name = f.stem
            ledgers[name] = data
        except:
            pass
    return ledgers


# Cache for API response
_last_resolve_time = 0
_cached_response = None
_cache_time = 0

def build_api_response():
    global _last_resolve_time, _cached_response, _cache_time

    now = time.time()
    if _cached_response and now - _cache_time < 3:
        return _cached_response

    ledgers = get_all_ledgers()
    all_open = []
    all_closed = []
    totals = {"pnl": 0, "wins": 0, "losses": 0, "trades": 0, "fees": 0, "open_cost": 0}

    for name, ledger in ledgers.items():
        for pos in ledger.get("open_positions", []):
            pos["_strategy"] = name
            all_open.append(pos)
        for trade in ledger.get("trades", []):
            trade["_strategy"] = name
            all_closed.append(trade)
        stats = ledger.get("stats", {})
        totals["pnl"] += stats.get("total_pnl", 0)
        totals["wins"] += stats.get("wins", 0)
        totals["losses"] += stats.get("losses", 0)
        totals["trades"] += stats.get("total_trades", 0)
        totals["fees"] += stats.get("total_fees", 0)

    # Resolve expired positions via reasoning-trader (uses events API, more reliable)
    has_expired = False
    for p in all_open:
        me = p.get("market_end")
        if me:
            try:
                if datetime.fromisoformat(me) < datetime.now(timezone.utc):
                    has_expired = True
                    break
            except:
                pass
    if has_expired and now - _last_resolve_time > 10:
        _last_resolve_time = now
        try:
            subprocess.run(["python3", str(BOT_DIR / "reasoning-trader.py"), "--resolve"],
                          capture_output=True, text=True, timeout=10)
            subprocess.run(["python3", str(BOT_DIR / "reasoning-trader-15m.py"), "--resolve"],
                          capture_output=True, text=True, timeout=10)
            # Reload after resolution
            ledgers = get_all_ledgers()
            all_open = []
            all_closed = []
            totals = {"pnl": 0, "wins": 0, "losses": 0, "trades": 0, "fees": 0, "open_cost": 0}
            for name, ledger in ledgers.items():
                for pos in ledger.get("open_positions", []):
                    pos["_strategy"] = name
                    all_open.append(pos)
                for trade in ledger.get("trades", []):
                    trade["_strategy"] = name
                    all_closed.append(trade)
                stats = ledger.get("stats", {})
                totals["pnl"] += stats.get("total_pnl", 0)
                totals["wins"] += stats.get("wins", 0)
                totals["losses"] += stats.get("losses", 0)
                totals["trades"] += stats.get("total_trades", 0)
                totals["fees"] += stats.get("total_fees", 0)
        except Exception:
            pass

    # Fetch live prices for open positions
    live_prices = get_live_prices(all_open)

    # Enrich open positions with live prices and unrealized P&L
    for pos in all_open:
        slug = pos.get("slug", "")
        side = pos.get("side", "")
        if slug in live_prices and side in live_prices[slug]:
            current = live_prices[slug][side]
            pos["_current_price"] = current
            pos["_unrealized_pnl"] = (current - pos.get("entry_price", 0)) * pos.get("shares", 0)
        else:
            pos["_current_price"] = None
            pos["_unrealized_pnl"] = None

    # Split active vs resolving (expired but not yet resolved)
    now_utc = datetime.now(timezone.utc)
    active_open = []
    for p in all_open:
        me = p.get("market_end")
        if me:
            try:
                if datetime.fromisoformat(me.replace("Z", "+00:00")) < now_utc:
                    continue
            except:
                pass
        active_open.append(p)

    totals["open_cost"] = sum(p.get("cost", 0) for p in active_open)
    unrealized = sum(p.get("_unrealized_pnl", 0) or 0 for p in active_open)

    # Sort
    all_open.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    all_closed.sort(key=lambda x: x.get("resolved_at", x.get("timestamp", "")), reverse=True)

    response = {
        "totals": {
            **{k: round(v, 2) for k, v in totals.items()},
            "unrealized_pnl": round(unrealized, 2),
            "total_pnl_incl_unrealized": round(totals["pnl"] + unrealized, 2),
        },
        "open_positions": all_open,
        "active_count": len(active_open),
        "closed_trades": all_closed[-50:],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    _cached_response = response
    _cache_time = now
    return response


HTML_PAGE = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polymarket Trading Desk</title>
<style>
:root {
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e; --green: #3fb950; --red: #f85149;
    --yellow: #d29922; --blue: #58a6ff; --orange: #db6d28; --purple: #bc8cff;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); padding: 12px; }
h1 { font-size: 20px; font-weight: 600; margin-bottom: 2px; }
.subtitle { color: var(--muted); font-size: 13px; margin-bottom: 16px; }
.live-dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: var(--green); margin-right: 4px; animation: pulse 2s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
.refresh-bar { position: fixed; top: 0; left: 0; height: 2px; background: var(--blue); transition: width 1s linear; z-index: 100; }

/* Cards */
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-bottom: 16px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
.card-label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }
.card-value { font-size: 24px; font-weight: 700; margin: 4px 0; }
.card-sub { color: var(--muted); font-size: 12px; }

/* Trade cards (mobile) */
.trade-card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 10px; margin-bottom: 8px; cursor: pointer; }
.trade-card.win { border-left: 3px solid var(--green); }
.trade-card.loss { border-left: 3px solid var(--red); }
.trade-card.open { border-left: 3px solid var(--blue); }
.tc-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
.tc-meta { display: flex; flex-wrap: wrap; gap: 8px; color: var(--muted); font-size: 12px; margin-bottom: 6px; }
.tc-meta span { white-space: nowrap; }
.tc-meta a { white-space: nowrap; }
.tc-reason { font-size: 11px; color: var(--muted); word-break: break-word; }
.tc-reason.collapsed { display: -webkit-box; -webkit-line-clamp: 1; -webkit-box-orient: vertical; overflow: hidden; }
.tc-side { font-weight: 600; }
.tc-side.up { color: var(--green); }
.tc-side.down { color: var(--red); }
.tc-pnl { font-weight: 600; }
.tc-strat { font-size: 10px; padding: 2px 6px; border-radius: 4px; }
.strat-reasoning { background: var(--purple); color: #000; }
.strat-reasoning-15m { background: var(--blue); color: #000; }
.tc-type { font-size: 10px; padding: 2px 6px; border-radius: 4px; font-weight: 600; }
.type-5m { background: var(--purple); color: #000; }
.type-15m { background: var(--blue); color: #000; }
.resolving { opacity: 0.7; }
.spinner { display: inline-block; width: 12px; height: 12px; border: 2px solid var(--muted); border-top-color: var(--blue); border-radius: 50%; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* Table (desktop) */
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: var(--muted); font-size: 11px; text-transform: uppercase; padding: 6px 8px; border-bottom: 1px solid var(--border); }
td { padding: 6px 8px; border-bottom: 1px solid var(--border); }
.reason-cell { max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); font-size: 11px; }

.green { color: var(--green); } .red { color: var(--red); } .yellow { color: var(--yellow); }
.empty { color: var(--muted); padding: 20px; text-align: center; }

h2 { font-size: 15px; margin: 16px 0 8px; color: var(--muted); }

.flash { animation: flash 0.8s ease-out; }
@keyframes flash { from { background: rgba(88,166,255,0.15); } to { background: transparent; } }

/* Responsive */
.desktop-only { display: none; }
.mobile-only { display: block; }
@media (min-width: 768px) {
  .desktop-only { display: block; }
  .mobile-only { display: none; }
  body { padding: 20px 30px; }
}
</style>
</head>
<body>
<div class="refresh-bar" id="refreshBar"></div>
<h1>Polymarket Trading Desk</h1>
<div class="subtitle"><span class="live-dot"></span>Live · <span id="liveClock"></span></div>

<div class="cards" id="cards"></div>

<h2>Open Positions</h2>
<div class="mobile-only" id="openCards"></div>
<div class="desktop-only" id="openTable"></div>

<h2>Closed Trades</h2>
<div class="mobile-only" id="closedCards"></div>
<div class="desktop-only" id="closedTable"></div>

<script>
const REFRESH_MS = 5000;
const pnlCls = v => v > 0 ? 'green' : v < 0 ? 'red' : '';
const pnlStr = (v, sign) => v == null ? '—' : (sign && v > 0 ? '+' : '') + '$' + v.toFixed(2);
const pctStr = v => v == null ? '—' : (v * 100).toFixed(1) + '¢';
const sideCls = s => (s||'').toLowerCase() === 'up' ? 'up' : 'down';
const stratBadge = s => `<span class="tc-strat strat-${s}">${s}</span>`;
const typeBadge = s => {
  const is15 = (s||'').includes('15m');
  return `<span class="tc-type ${is15 ? 'type-15m' : 'type-5m'}">${is15 ? '15m' : '5m'}</span>`;
};
const isExpired = ts => { if (!ts) return false; const t = new Date(ts.replace('+00:00','Z')).getTime(); return !isNaN(t) && t < Date.now(); };
const resolvingBadge = '<span class="spinner"></span> Resolving';

function timeAgo(ts) {
  if (!ts) return '—';
  const d = (Date.now() - new Date(ts).getTime()) / 1000;
  if (d < 60) return Math.floor(d) + 's';
  if (d < 3600) return Math.floor(d/60) + 'm';
  if (d < 86400) return Math.floor(d/3600) + 'h';
  return Math.floor(d/86400) + 'd';
}

function timeUntil(ts) {
  if (!ts) return '—';
  const d = (new Date(ts).getTime() - Date.now()) / 1000;
  if (d < 0) return '🔒 Closed';
  const m = Math.floor(d / 60);
  const s = Math.floor(d % 60);
  if (m > 0) return m + ':' + String(s).padStart(2,'0');
  return s + 's';
}

function shortTime(ts) {
  if (!ts) return '—';
  return new Date(ts).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
}

function renderCards(data) {
  const t = data.totals;
  const wr = t.trades > 0 ? ((t.wins / t.trades) * 100).toFixed(0) + '%' : '—';
  document.getElementById('cards').innerHTML = `
    <div class="card">
      <div class="card-label">Total P&L</div>
      <div class="card-value ${pnlCls(t.total_pnl_incl_unrealized)}">${pnlStr(t.total_pnl_incl_unrealized)}</div>
      <div class="card-sub">Realized ${pnlStr(t.pnl)}</div>
    </div>
    <div class="card">
      <div class="card-label">Win Rate</div>
      <div class="card-value">${wr}</div>
      <div class="card-sub">${t.wins}W / ${t.losses}L</div>
    </div>
    <div class="card">
      <div class="card-label">Open</div>
      <div class="card-value yellow">${data.active_count}</div>
      <div class="card-sub">$${t.open_cost.toFixed(0)} deployed</div>
    </div>
    <div class="card">
      <div class="card-label">Unrealized</div>
      <div class="card-value ${pnlCls(t.unrealized_pnl)}">${pnlStr(t.unrealized_pnl)}</div>
      <div class="card-sub">Fees ${pnlStr(-t.fees)}</div>
    </div>
  `;
}

function renderOpenCards(positions) {
  const el = document.getElementById('openCards');
  const active = positions.filter(p => !isExpired(p.market_end));
  if (!active.length) { el.innerHTML = '<div class="empty">No open positions</div>'; return; }
  el.innerHTML = active.map(p => {
    const curr = p._current_price != null ? pctStr(p._current_price) : '—';
    const upnl = p._unrealized_pnl;
    const reason = p.reasoning || p.reason || '';
    return `<div class="trade-card open" onclick="this.querySelector('.tc-reason').classList.toggle('collapsed')">
      <div class="tc-header">
        <span>${typeBadge(p._strategy)} <span class="tc-side ${sideCls(p.side)}">▶ ${p.side}</span></span>
        <span class="tc-pnl ${pnlCls(upnl)}">${pnlStr(upnl,true)}</span>
      </div>
      <div class="tc-meta">
        <span>Entry ${pctStr(p.entry_price)}</span>
        <span>Now ${curr}</span>
        <span>$${(p.cost||0).toFixed(0)}</span>
        <span class="countdown" data-expires="${p.market_end||''}">⏱ ${p.market_end ? timeUntil(p.market_end) : '—'}</span>
        <span>${shortTime(p.timestamp)}</span>
        ${p.slug ? `<a href="https://polymarket.com/event/${p.slug}" target="_blank" style="color:var(--blue);text-decoration:none">↗</a>` : ''}
      </div>
      ${reason ? `<div class="tc-reason collapsed">${reason}</div>` : ''}
    </div>`;
  }).join('');
}

function renderClosedCards(trades) {
  const el = document.getElementById('closedCards');
  if (!trades.length) { el.innerHTML = '<div class="empty">No closed trades yet</div>'; return; }
  el.innerHTML = trades.map(t => {
    const cls = t.outcome === 'win' ? 'win' : 'loss';
    const icon = t.outcome === 'win' ? '<span style="color:#4caf50">●</span>' : '<span style="color:#f44336">●</span>';
    const reason = t.reasoning || t.reason || '';
    return `<div class="trade-card ${cls}" onclick="this.querySelector('.tc-reason')&&this.querySelector('.tc-reason').classList.toggle('collapsed')">
      <div class="tc-header">
        <span>${icon} <span class="tc-side ${sideCls(t.side)}">${t.side}</span> · ${stratBadge(t._strategy)}</span>
        <span class="tc-pnl ${pnlCls(t.pnl)}">${pnlStr(t.pnl,true)}</span>
      </div>
      <div class="tc-meta">
        <span>Entry ${pctStr(t.entry_price)}</span>
        <span>Result: ${t.market_result || '—'}</span>
        <span>$${(t.cost||0).toFixed(0)}</span>
        <span>${timeAgo(t.resolved_at)} ago</span>
        <span>${shortTime(t.timestamp)}</span>
        ${t.slug ? `<a href="https://polymarket.com/event/${t.slug}" target="_blank" style="color:var(--blue);text-decoration:none">↗</a>` : ''}
      </div>
      ${reason ? `<div class="tc-reason collapsed">${reason}</div>` : ''}
    </div>`;
  }).join('');
}

function renderOpenTable(positions) {
  const el = document.getElementById('openTable');
  const active = positions.filter(p => !isExpired(p.market_end));
  if (!active.length) { el.innerHTML = '<div class="empty">No open positions</div>'; return; }
  let h = `<table><tr><th>Time</th><th>Type</th><th>Side</th><th>Entry</th><th>Current</th><th>P&L</th><th>Cost</th><th>Expires</th><th>Reasoning</th><th></th></tr>`;
  for (const p of active) {
    const curr = p._current_price != null ? pctStr(p._current_price) : '—';
    h += `<tr>
      <td>${shortTime(p.timestamp)}</td>
      <td>${typeBadge(p._strategy)}</td>
      <td class="${sideCls(p.side) === 'up' ? 'green' : 'red'}">${p.side}</td>
      <td>${pctStr(p.entry_price)}</td>
      <td>${curr}</td>
      <td class="${pnlCls(p._unrealized_pnl)}">${pnlStr(p._unrealized_pnl,true)}</td>
      <td>$${(p.cost||0).toFixed(0)}</td>
      <td class="countdown" data-expires="${p.market_end||''}">${p.market_end ? timeUntil(p.market_end) : '—'}</td>
      <td class="reason-cell">${(p.reasoning||p.reason||'').substring(0,80)}</td>
      <td>${p.slug ? `<a href="https://polymarket.com/event/${p.slug}" target="_blank" style="color:var(--blue)">↗</a>` : ''}</td>
    </tr>`;
  }
  el.innerHTML = h + '</table>';
}

function renderClosedTable(trades, openPositions) {
  const el = document.getElementById('closedTable');
  // Resolving = expired but not yet resolved (still in open_positions)
  const resolving = (openPositions||[]).filter(p => isExpired(p.market_end));
  if (!trades.length && !resolving.length) { el.innerHTML = '<div class="empty">No closed trades yet</div>'; return; }
  let h = `<table><tr><th>Time</th><th>Type</th><th>Side</th><th>Entry</th><th>Result</th><th>P&L</th><th>Cost</th><th>Closed</th><th>Reasoning</th><th></th></tr>`;
  // Show resolving positions first
  for (const p of resolving) {
    h += `<tr class="resolving">
      <td>${shortTime(p.timestamp)}</td>
      <td>${typeBadge(p._strategy)}</td>
      <td class="${sideCls(p.side) === 'up' ? 'green' : 'red'}">${p.side}</td>
      <td>${pctStr(p.entry_price)}</td>
      <td>${resolvingBadge}</td>
      <td>—</td>
      <td>$${(p.cost||0).toFixed(0)}</td>
      <td>—</td>
      <td class="reason-cell">${(p.reasoning||p.reason||'').substring(0,80)}</td>
      <td>${p.slug ? `<a href="https://polymarket.com/event/${p.slug}" target="_blank" style="color:var(--blue)">↗</a>` : ''}</td>
    </tr>`;
  }
  for (const t of trades) {
    const icon = t.outcome === 'win' ? '<span style="color:#4caf50">●</span>' : '<span style="color:#f44336">●</span>';
    h += `<tr>
      <td>${shortTime(t.timestamp)}</td>
      <td>${typeBadge(t._strategy)}</td>
      <td class="${sideCls(t.side) === 'up' ? 'green' : 'red'}">${t.side}</td>
      <td>${pctStr(t.entry_price)}</td>
      <td>${icon} ${t.market_result||'—'}</td>
      <td class="${pnlCls(t.pnl)}">${pnlStr(t.pnl,true)}</td>
      <td>$${(t.cost||0).toFixed(0)}</td>
      <td>${timeAgo(t.resolved_at)}</td>
      <td class="reason-cell">${(t.reasoning||t.reason||'').substring(0,80)}</td>
      <td>${t.slug ? `<a href="https://polymarket.com/event/${t.slug}" target="_blank" style="color:var(--blue)">↗</a>` : ''}</td>
    </tr>`;
  }
  el.innerHTML = h + '</table>';
}

let prevData = null;
function applyData(data) {
  const t = data.totals;
  const p = prevData ? prevData.totals : t;
  const changed = !prevData ||
      t.pnl !== p.pnl ||
      t.total_pnl_incl_unrealized !== p.total_pnl_incl_unrealized ||
      data.open_positions.length !== (prevData?prevData.open_positions.length:0) ||
      (t.wins !== p.wins || t.losses !== p.losses) ||
      t.unrealized_pnl !== p.unrealized_pnl;

  if (changed || !prevData) {
    renderCards(data);
    renderOpenCards(data.open_positions);
    renderClosedCards(data.closed_trades);
    renderOpenTable(data.open_positions);
    renderClosedTable(data.closed_trades, data.open_positions);
    if (prevData) {
      document.querySelectorAll('.card').forEach(el => {
        el.classList.add('flash');
        void el.offsetWidth;
      });
    }
  }
  prevData = data;
  // clock handled by setInterval above
}

// SSE: real-time push updates
let sseConnected = false;
function startSSE() {
  const es = new EventSource('/api/stream');
  es.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      applyData(data);
      sseConnected = true;
      document.querySelector('.live-dot').style.background = 'var(--green)';
    } catch(err) { console.error('SSE parse error:', err); }
  };
  es.onerror = () => {
    sseConnected = false;
    document.querySelector('.live-dot').style.background = 'var(--yellow)';
    es.close();
    setTimeout(startSSE, 3000);
  };
}

// Live clock
setInterval(() => {
  const el = document.getElementById('liveClock');
  if (el) el.textContent = new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
}, 1000);

// Live countdown ticker
setInterval(() => {
  document.querySelectorAll('.countdown[data-expires]').forEach(el => {
    const ts = el.dataset.expires;
    if (ts) { const v = timeUntil(ts); el.textContent = v.includes('Closed') ? v : '⏱ ' + v; }
  });
}, 1000);

// Fallback poll every 30s in case SSE drops
setInterval(async () => {
  if (sseConnected) return;
  try {
    const resp = await fetch('/api/data');
    applyData(await resp.json());
  } catch(e) {}
}, 30000);

async function init() {
  try {
    const resp = await fetch('/api/data');
    const data = await resp.json();
    applyData(data);
  } catch(e) { console.error('Initial fetch failed:', e); }
  startSSE();
}

init();
</script>
</body>
</html>"""


import hashlib
import threading

_ledger_hash = ""
_ledger_lock = threading.Lock()

def get_ledger_hash():
    """Quick hash of all ledger files to detect changes."""
    h = hashlib.md5()
    if LEDGER_DIR.exists():
        for f in sorted(LEDGER_DIR.glob("*.json")):
            try:
                h.update(f.read_bytes())
            except:
                pass
    return h.hexdigest()


class DashboardHandler(BaseHTTPRequestHandler):
    def _check_auth(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            return decoded == f"{DASH_USER}:{DASH_PASS}"
        except:
            return False

    def do_GET(self):
        # SSE stream and data API skip auth (page already authenticated)
        if self.path not in ("/api/stream", "/api/data", "/api/health") and not self._check_auth():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Trading Desk"')
            self.end_headers()
            return

        if self.path == "/api/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        elif self.path == "/api/data":
            data = build_api_response()
            body = json.dumps(data, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            global _ledger_hash
            last_hash = ""
            try:
                while True:
                    current_hash = get_ledger_hash()
                    if current_hash != last_hash:
                        data = build_api_response()
                        msg = f"data: {json.dumps(data, default=str)}\n\n"
                        self.wfile.write(msg.encode())
                        self.wfile.flush()
                        last_hash = current_hash
                    time.sleep(5)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            body = HTML_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, format, *args):
        sys.stderr.write(f"[HTTP] {self.client_address[0]} {format % args}\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8850)
    args = parser.parse_args()

    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
    server = ThreadedHTTPServer(("0.0.0.0", args.port), DashboardHandler)
    server.daemon_threads = True
    print(f"📊 Dashboard live at http://localhost:{args.port}")
    print(f"   Press Ctrl+C to stop")
    print("-" * 40)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
