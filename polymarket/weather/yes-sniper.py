#!/usr/bin/env python3.12
"""
Weather YES Sniper — Phase 1
Multi-model ensemble scanner that identifies high-conviction YES bets
on Polymarket weather markets. Outputs structured Telegram alerts.
Tracks paper trades in a ledger for performance evaluation.

Runs via cron 4x/day aligned with GFS model updates:
  06:00 Oslo (GFS 00z), 12:00 (GFS 06z), 18:00 (GFS 12z), 00:00 (GFS 18z)

Usage:
  python3.12 yes-sniper.py              # Scan and alert
  python3.12 yes-sniper.py --resolve    # Resolve paper trades with actual outcomes
  python3.12 yes-sniper.py --status     # Show paper trading P&L
"""

import json
import sys
import re
import urllib.request
from datetime import datetime, timezone, timedelta
from math import exp, sqrt, pi
from pathlib import Path

UA = "(clawdtools.ai, clive@clawdtools.ai)"
WEATHER_DIR = Path(__file__).parent
LEDGER_PATH = WEATHER_DIR / "ledgers" / "yes-sniper.json"
LEDGER_PATH.parent.mkdir(exist_ok=True)

# ─── City configs (grid_id, grid_x, grid_y, lat, lon) ────────────
CITIES = {
    "NYC":     ("OKX", 34, 38, 40.7831, -73.9712),
    "Chicago": ("LOT", 76, 73, 41.8781, -87.6298),
    "Miami":   ("MFL", 76, 50, 25.7617, -80.1918),
    "Dallas":  ("FWD", 86, 114, 32.7767, -96.7970),
    "Seattle": ("SEW", 125, 67, 47.6062, -122.3321),
    "Atlanta": ("FFC", 51, 86, 33.7490, -84.3880),
}

# NWS observation stations for resolution verification
NWS_STATIONS = {
    "NYC": "KLGA",       # LaGuardia
    "Chicago": "KORD",   # O'Hare
    "Miami": "KMIA",     # Miami Intl
    "Dallas": "KDFW",    # DFW
    "Seattle": "KSEA",   # Sea-Tac
    "Atlanta": "KATL",   # Hartsfield
}

# ─── Thresholds ───────────────────────────────────────────────────
MIN_EDGE = 0.08           # 8% minimum edge (paper phase — calibrating)
MIN_MODELS = 4            # Need at least 4 models agreeing
MAX_SIGMA = 8.0           # Allow high disagreement — ensemble handles uncertainty
MIN_YES_PRICE = 0.05      # Skip illiquid near-zero
MAX_YES_PRICE = 0.70      # Don't buy expensive YES (diminishing returns)
PAPER_BET_SIZE = 20.0     # $20 paper bets
MIN_PROB = 0.15           # Model probability must be >= 15% (lower for tail bets)


def fetch_noaa_high(city: str, target_date: str) -> float | None:
    grid_id, grid_x, grid_y, _, _ = CITIES[city]
    url = f"https://api.weather.gov/gridpoints/{grid_id}/{grid_x},{grid_y}/forecast/hourly"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
        temps = [p["temperature"] for p in data["properties"]["periods"]
                 if p["startTime"][:10] == target_date]
        return max(temps) if temps else None
    except:
        return None


def fetch_open_meteo_models(lat: float, lon: float, target_date: str) -> dict[str, float]:
    models = "gfs_seamless,ecmwf_ifs04,jma_seamless,gem_seamless,icon_seamless"
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
           f"&hourly=temperature_2m&temperature_unit=fahrenheit&models={models}")
    try:
        resp = urllib.request.urlopen(url, timeout=15)
        data = json.loads(resp.read())
    except:
        return {}

    results = {}
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])

    for key in hourly:
        if key == "time":
            continue
        model_name = key.replace("temperature_2m_", "").replace("temperature_2m", "default")
        temps = []
        for i, t in enumerate(times):
            if t[:10] == target_date and hourly[key][i] is not None:
                temps.append(hourly[key][i])
        if temps:
            results[model_name] = max(temps)

    return results


def fetch_ensemble_spread(lat: float, lon: float, target_date: str) -> dict:
    """Fetch ensemble members for tighter probability estimation."""
    url = (f"https://ensemble-api.open-meteo.com/v1/ensemble?"
           f"latitude={lat}&longitude={lon}"
           f"&hourly=temperature_2m&temperature_unit=fahrenheit"
           f"&models=gfs_seamless,ecmwf_ifs025,icon_seamless,gem_global")
    try:
        resp = urllib.request.urlopen(url, timeout=15)
        data = json.loads(resp.read())
    except:
        return {}

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])

    # Collect daily highs per ensemble member
    member_highs = []
    for key in hourly:
        if key == "time":
            continue
        temps = []
        for i, t in enumerate(times):
            if t[:10] == target_date and hourly[key][i] is not None:
                temps.append(hourly[key][i])
        if temps:
            member_highs.append(max(temps))

    if not member_highs:
        return {}

    mu = sum(member_highs) / len(member_highs)
    if len(member_highs) > 1:
        variance = sum((v - mu) ** 2 for v in member_highs) / (len(member_highs) - 1)
        sigma = sqrt(variance)
    else:
        sigma = 2.0

    return {
        "members": len(member_highs),
        "mu": mu,
        "sigma": sigma,
        "min": min(member_highs),
        "max": max(member_highs),
        "highs": sorted(member_highs),
    }


def fetch_pm_markets(city: str, target_date: str) -> tuple[list[dict], str]:
    city_slug_map = {
        "NYC": "nyc", "Chicago": "chicago", "Miami": "miami",
        "Dallas": "dallas", "Seattle": "seattle", "Atlanta": "atlanta",
    }
    slug_city = city_slug_map.get(city, city.lower())
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    slug = f"highest-temperature-in-{slug_city}-on-{dt.strftime('%B').lower()}-{dt.day}-{dt.year}"

    url = f"https://gamma-api.polymarket.com/events?slug={slug}"
    try:
        resp = urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=15)
        events = json.loads(resp.read())
    except:
        return [], slug

    if not events:
        return [], slug

    event = events[0]
    buckets = []
    for m in event.get("markets", []):
        question = m.get("question", "")
        prices = json.loads(m.get("outcomePrices", "[]"))
        tokens = json.loads(m.get("clobTokenIds", "[]"))
        condition_id = m.get("conditionId", "")
        volume = m.get("volumeNum", 0)
        slug_market = m.get("slug", "")

        if len(prices) < 2 or len(tokens) < 2:
            continue

        yes_price = float(prices[0])
        no_price = float(prices[1])

        # Parse bucket
        q = question.lower()
        bucket_low, bucket_high = None, None
        if "or below" in q:
            match = re.search(r"be (\d+)°", q)
            if match:
                bucket_low, bucket_high = -999, int(match.group(1))
        elif "or higher" in q:
            match = re.search(r"be (\d+)°", q)
            if match:
                bucket_low, bucket_high = int(match.group(1)), 999
        else:
            match = re.search(r"between (\d+)-(\d+)°", q)
            if match:
                bucket_low, bucket_high = int(match.group(1)), int(match.group(2))

        if bucket_low is None:
            continue

        buckets.append({
            "question": question,
            "yes_price": yes_price,
            "no_price": no_price,
            "bucket_low": bucket_low,
            "bucket_high": bucket_high,
            "token_yes": tokens[0],
            "token_no": tokens[1] if len(tokens) > 1 else tokens[0],
            "condition_id": condition_id,
            "volume": volume,
            "slug": slug_market,
            "event_slug": slug,
        })

    buckets.sort(key=lambda b: b["bucket_low"])
    return buckets, slug


def normal_cdf_range(mu: float, sigma: float, lo: float, hi: float) -> float:
    lo = max(lo, mu - 6 * sigma)
    hi = min(hi, mu + 6 * sigma)
    if lo >= hi:
        return 0.0001
    steps = 200
    step_size = (hi - lo) / steps
    return max(0.001, min(0.999, sum(
        exp(-0.5 * ((lo + (i + 0.5) * step_size - mu) / sigma) ** 2) / (sigma * sqrt(2 * pi)) * step_size
        for i in range(steps)
    )))


def empirical_prob(highs: list[float], lo: float, hi: float) -> float:
    """Direct probability from ensemble member counts."""
    if not highs:
        return 0.0
    count = sum(1 for h in highs if lo <= h <= hi)
    return count / len(highs)


def format_bucket(lo: int, hi: int) -> str:
    if lo == -999:
        return f"≤{hi}°F"
    elif hi == 999:
        return f"≥{lo}°F"
    else:
        return f"{lo}-{hi}°F"


def hours_until_resolution(target_date: str) -> float:
    """Estimate hours until market resolves (assume ~midnight ET next day)."""
    # PM weather markets resolve based on daily high, typically by end of day
    from datetime import timezone
    target = datetime.strptime(target_date, "%Y-%m-%d").replace(
        hour=23, minute=59, tzinfo=timezone(timedelta(hours=-5)))  # ET
    now = datetime.now(timezone.utc)
    delta = target - now
    return max(0, delta.total_seconds() / 3600)


def load_ledger() -> dict:
    if LEDGER_PATH.exists():
        return json.loads(LEDGER_PATH.read_text())
    return {
        "trades": [],
        "stats": {"wins": 0, "losses": 0, "pending": 0, "total_pnl": 0.0},
    }


def save_ledger(ledger: dict):
    LEDGER_PATH.write_text(json.dumps(ledger, indent=2))


def add_paper_trade(ledger: dict, signal: dict):
    """Add a paper trade to the ledger."""
    trade = {
        "id": len(ledger["trades"]) + 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "city": signal["city"],
        "date": signal["date"],
        "bucket": format_bucket(signal["bucket_low"], signal["bucket_high"]),
        "bucket_low": signal["bucket_low"],
        "bucket_high": signal["bucket_high"],
        "side": "YES",
        "entry_price": signal["yes_price"],
        "size": PAPER_BET_SIZE,
        "shares": PAPER_BET_SIZE / signal["yes_price"],
        "model_prob": signal["model_prob"],
        "edge": signal["edge"],
        "mu": signal["mu"],
        "sigma": signal["sigma"],
        "n_models": signal["n_models"],
        "ensemble_members": signal.get("ensemble_members", 0),
        "status": "pending",
        "pnl": 0.0,
        "event_slug": signal["event_slug"],
    }
    ledger["trades"].append(trade)
    ledger["stats"]["pending"] += 1
    return trade


def scan_and_alert():
    """Main scan: fetch forecasts, find YES edges, output alerts."""
    now = datetime.now(timezone.utc)
    dates = [(now + timedelta(days=d)).strftime("%Y-%m-%d") for d in range(3)]

    signals = []

    for city, (grid_id, gx, gy, lat, lon) in CITIES.items():
        for target_date in dates:
            hours_left = hours_until_resolution(target_date)
            if hours_left < 2:
                continue  # Too late, skip

            # Multi-model deterministic forecasts
            noaa_high = fetch_noaa_high(city, target_date)
            om_models = fetch_open_meteo_models(lat, lon, target_date)
            model_highs = dict(om_models)
            if noaa_high is not None:
                model_highs["noaa"] = noaa_high

            if len(model_highs) < MIN_MODELS:
                continue

            # Ensemble spread for tighter probability
            ensemble = fetch_ensemble_spread(lat, lon, target_date)

            # Deterministic model stats
            values = list(model_highs.values())
            mu = sum(values) / len(values)
            if len(values) > 1:
                sigma = max(1.5, sqrt(sum((v - mu) ** 2 for v in values) / (len(values) - 1)))
            else:
                sigma = 2.0

            if sigma > MAX_SIGMA:
                continue  # Models disagree too much

            # PM markets
            buckets, event_slug = fetch_pm_markets(city, target_date)
            if not buckets:
                continue

            # Score each bucket for YES opportunity
            for b in buckets:
                lo, hi = b["bucket_low"], b["bucket_high"]

                # Compute probability from both deterministic and ensemble
                det_prob = normal_cdf_range(mu, sigma, lo, hi)

                # If we have ensemble data, blend it (ensemble is more reliable)
                if ensemble and ensemble.get("highs"):
                    emp_prob = empirical_prob(ensemble["highs"], lo, hi)
                    # Weighted blend: 70% ensemble, 30% deterministic
                    prob = 0.7 * emp_prob + 0.3 * det_prob
                    # Also compute from ensemble normal dist
                    ens_normal_prob = normal_cdf_range(
                        ensemble["mu"], max(1.5, ensemble["sigma"]), lo, hi)
                    # Final: average of empirical blend and ensemble normal
                    prob = (prob + ens_normal_prob) / 2
                else:
                    prob = det_prob

                prob = max(0.001, min(0.999, prob))
                edge = prob - b["yes_price"]

                if (edge >= MIN_EDGE
                        and MIN_YES_PRICE <= b["yes_price"] <= MAX_YES_PRICE
                        and prob >= MIN_PROB):
                    signals.append({
                        "city": city,
                        "date": target_date,
                        "bucket_low": lo,
                        "bucket_high": hi,
                        "yes_price": b["yes_price"],
                        "no_price": b["no_price"],
                        "model_prob": round(prob, 3),
                        "edge": round(edge, 3),
                        "mu": round(mu, 1),
                        "sigma": round(sigma, 1),
                        "n_models": len(model_highs),
                        "models": {k: round(v, 1) for k, v in model_highs.items()},
                        "ensemble_members": ensemble.get("members", 0),
                        "ensemble_mu": round(ensemble["mu"], 1) if ensemble else None,
                        "ensemble_sigma": round(ensemble["sigma"], 1) if ensemble else None,
                        "hours_left": round(hours_left, 1),
                        "volume": b["volume"],
                        "event_slug": event_slug,
                        "token_yes": b.get("token_yes", ""),
                        "condition_id": b.get("condition_id", ""),
                    })

    if not signals:
        print("No YES sniper signals found.")
        return

    # Sort by edge descending
    signals.sort(key=lambda s: s["edge"], reverse=True)

    # Load ledger for dedup
    ledger = load_ledger()
    existing = {(t["city"], t["date"], t["bucket"]) for t in ledger["trades"]}

    # Output alerts
    print(f"🌡️ YES Sniper — {now.strftime('%Y-%m-%d %H:%M UTC')}\n")

    new_trades = 0
    for s in signals[:8]:  # Top 8 signals
        bucket = format_bucket(s["bucket_low"], s["bucket_high"])
        models_str = " | ".join(f"{k}={v:.0f}" for k, v in sorted(s["models"].items()))
        pm_link = f"https://polymarket.com/event/{s['event_slug']}"

        # Check if already in ledger
        trade_key = (s["city"], s["date"], bucket)
        is_new = trade_key not in existing

        print(f"{'🎯 NEW' if is_new else '📊 UPD'} {s['city']} {s['date']} — {bucket}")
        print(f"   BUY YES @ ${s['yes_price']:.3f} | Prob {s['model_prob']:.0%} | Edge {s['edge']:+.0%}")
        ens_str = f", {s['ensemble_members']} ensemble" if s['ensemble_members'] else ""
        print(f"   Forecast: {s['mu']:.0f}°F ± {s['sigma']:.1f}° ({s['n_models']} models{ens_str})")
        print(f"   Models: {models_str}")
        print(f"   Resolves in {s['hours_left']:.0f}h | Vol: ${s['volume']:,.0f}")
        print(f"   {pm_link}")

        if is_new:
            trade = add_paper_trade(ledger, s)
            print(f"   📝 PAPER: #{trade['id']} BUY {trade['shares']:.1f} shares @ ${s['yes_price']:.3f} (${PAPER_BET_SIZE})")
            new_trades += 1
            existing.add(trade_key)

        print()

    if new_trades:
        save_ledger(ledger)
        p = ledger["stats"]
        print(f"📊 Paper ledger: {p['wins']}W/{p['losses']}L/{p['pending']}P | PnL: ${p['total_pnl']:+.2f}")


def resolve_trades():
    """Check pending trades and resolve them against actual observed temps."""
    ledger = load_ledger()
    pending = [t for t in ledger["trades"] if t["status"] == "pending"]

    if not pending:
        print("No pending trades to resolve.")
        return

    now = datetime.now(timezone.utc)
    resolved = 0

    for trade in pending:
        # Only try to resolve if date has passed
        trade_date = datetime.strptime(trade["date"], "%Y-%m-%d")
        if trade_date.date() >= now.date():
            continue  # Not yet resolved

        city = trade["city"]
        station = NWS_STATIONS.get(city)
        if not station:
            continue

        # Fetch observed high from NWS
        actual_high = fetch_observed_high(station, trade["date"])
        if actual_high is None:
            print(f"⏳ #{trade['id']} {city} {trade['date']} — observation not yet available")
            continue

        # Determine outcome
        lo, hi = trade["bucket_low"], trade["bucket_high"]
        won = lo <= actual_high <= hi

        trade["actual_high"] = actual_high
        trade["status"] = "win" if won else "loss"
        trade["pnl"] = (trade["shares"] * 1.0 - trade["size"]) if won else -trade["size"]

        ledger["stats"]["pending"] -= 1
        if won:
            ledger["stats"]["wins"] += 1
        else:
            ledger["stats"]["losses"] += 1
        ledger["stats"]["total_pnl"] += trade["pnl"]

        icon = "✅" if won else "❌"
        print(f"{icon} #{trade['id']} {city} {trade['date']} {trade['bucket']}"
              f" | Actual: {actual_high}°F | PnL: ${trade['pnl']:+.2f}")
        resolved += 1

    if resolved:
        save_ledger(ledger)
        p = ledger["stats"]
        print(f"\n📊 Updated: {p['wins']}W/{p['losses']}L/{p['pending']}P | PnL: ${p['total_pnl']:+.2f}")
    else:
        print("No trades ready for resolution yet.")


def fetch_observed_high(station: str, target_date: str) -> float | None:
    """Fetch actual observed high from NWS observation history."""
    url = f"https://w1.weather.gov/data/obhistory/{station}.html"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        resp = urllib.request.urlopen(req, timeout=15)
        html = resp.read().decode("utf-8", errors="replace")
    except:
        return None

    # Parse the observation table — look for temperature readings
    # NWS obs history shows last 3 days of hourly observations
    # Format: each row has date, time, wind, vis, weather, sky, temp, dewpt, etc.
    temps = []
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)
    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        if len(cells) >= 7:
            try:
                # Date is in first cell (DD format), temp is typically cell 6 or 7
                date_cell = cells[0].strip()
                temp_cell = re.sub(r'<[^>]+>', '', cells[6]).strip()
                temp = float(temp_cell)
                # Match by day of month
                target_day = int(target_date.split("-")[2])
                if date_cell.isdigit() and int(date_cell) == target_day:
                    temps.append(temp)
            except (ValueError, IndexError):
                continue

    return max(temps) if temps else None


def show_status():
    """Show paper trading performance."""
    ledger = load_ledger()
    trades = ledger["trades"]
    stats = ledger["stats"]

    if not trades:
        print("No paper trades yet.")
        return

    print(f"🌡️ YES Sniper — Paper Trading Status")
    print(f"{'='*55}")
    print(f"Record: {stats['wins']}W / {stats['losses']}L / {stats['pending']} pending")
    total = stats['wins'] + stats['losses']
    if total:
        print(f"Win rate: {stats['wins']/total:.0%}")
    print(f"PnL: ${stats['total_pnl']:+.2f}")
    print()

    for t in trades[-10:]:  # Last 10
        icon = {"win": "✅", "loss": "❌", "pending": "⏳"}[t["status"]]
        actual = f" → {t['actual_high']}°F" if t.get("actual_high") else ""
        print(f"{icon} #{t['id']} {t['city']} {t['date']} {t['bucket']}"
              f" @ ${t['entry_price']:.3f} (prob {t['model_prob']:.0%})"
              f"{actual} PnL: ${t['pnl']:+.2f}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Weather YES Sniper")
    parser.add_argument("--resolve", action="store_true", help="Resolve pending paper trades")
    parser.add_argument("--status", action="store_true", help="Show paper P&L")
    args = parser.parse_args()

    if args.resolve:
        resolve_trades()
    elif args.status:
        show_status()
    else:
        # Always try to resolve old trades first
        resolve_trades()
        print()
        scan_and_alert()


if __name__ == "__main__":
    main()
