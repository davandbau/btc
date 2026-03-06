#!/usr/bin/env python3.12
"""
Weather Market Alert Scanner
Scans all cities for weather market edges and outputs actionable alerts.
Designed to be called by an OpenClaw cron job that forwards output to Telegram.

Filters:
- Edge > 15% (model prob vs market price)
- YES price between $0.03 and $0.85 (actionable range — skip near-zero/near-one)
- Uses multi-model ensemble (NOAA + Open-Meteo GFS/ECMWF/ICON/GEM/JMA) for better σ
"""

import json
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from math import exp, sqrt, pi
from pathlib import Path

UA = "(clawdtools.ai, clive@clawdtools.ai)"

# ─── City configs ─────────────────────────────────────────────────
CITIES = {
    "NYC":     ("OKX", 34, 38, 40.7831, -73.9712),
    "Chicago": ("LOT", 76, 73, 41.8781, -87.6298),
    "Miami":   ("MFL", 76, 50, 25.7617, -80.1918),
    "Dallas":  ("FWD", 86, 114, 32.7767, -96.7970),
    "Seattle": ("SEW", 125, 67, 47.6062, -122.3321),
    "Atlanta": ("FFC", 51, 86, 33.7490, -84.3880),
}

MIN_EDGE = 0.15          # 15% minimum edge to alert
MIN_YES_PRICE = 0.03     # Skip near-zero buckets (no liquidity)
MAX_YES_PRICE = 0.85     # Skip near-certain buckets
MIN_NO_PRICE = 0.03      # Same for NO side
MAX_NO_PRICE = 0.85


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
    """Fetch daily high from multiple weather models via Open-Meteo."""
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


def fetch_pm_markets(city: str, target_date: str) -> tuple[list[dict], str]:
    """Returns (buckets, event_slug) for PM market link construction."""
    city_slug_map = {
        "NYC": "nyc", "Chicago": "chicago", "Miami": "miami",
        "Dallas": "dallas", "Seattle": "seattle", "Atlanta": "atlanta",
    }
    slug_city = city_slug_map.get(city, city.lower())
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    slug = f"highest-temperature-in-{slug_city}-on-{dt.strftime('%B').lower()}-{dt.day}-{dt.year}"

    url = f"https://gamma-api.polymarket.com/events?slug={slug}"
    try:
        resp = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=15)
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

        if len(prices) < 2:
            continue

        yes_price = float(prices[0])
        no_price = float(prices[1])

        # Parse bucket
        import re
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
            "condition_id": condition_id,
            "volume": volume,
            "slug": slug_market,
        })

    buckets.sort(key=lambda b: b["bucket_low"])
    return buckets, slug


def compute_ensemble_edges(model_highs: dict[str, float], buckets: list[dict]) -> list[dict]:
    """
    Use ensemble of model forecasts to estimate probability distribution.
    Mean of all models = forecast center, stddev of models = uncertainty.
    Minimum σ = 1.5°F (don't be overconfident even if models agree).
    """
    values = list(model_highs.values())
    if not values:
        return []

    mu = sum(values) / len(values)
    if len(values) > 1:
        variance = sum((v - mu) ** 2 for v in values) / (len(values) - 1)
        sigma = max(1.5, sqrt(variance))
    else:
        sigma = 2.0  # Single model fallback

    def normal_cdf_range(lo, hi):
        """Integrate normal PDF from lo to hi."""
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

    edges = []
    for b in buckets:
        lo, hi = b["bucket_low"], b["bucket_high"]
        prob = normal_cdf_range(lo, hi)

        edge_yes = prob - b["yes_price"]
        edge_no = (1 - prob) - b["no_price"]

        alert = None
        side = None
        if edge_yes > MIN_EDGE and MIN_YES_PRICE <= b["yes_price"] <= MAX_YES_PRICE:
            alert = f"BUY YES @ ${b['yes_price']:.3f}"
            side = "yes"
        elif edge_no > MIN_EDGE and MIN_NO_PRICE <= b["no_price"] <= MAX_NO_PRICE:
            alert = f"BUY NO @ ${b['no_price']:.3f}"
            side = "no"

        if alert:
            edges.append({
                **b,
                "model_prob": prob,
                "edge_yes": edge_yes,
                "edge_no": edge_no,
                "alert": alert,
                "side": side,
                "mu": mu,
                "sigma": sigma,
            })

    # Sort by absolute edge descending
    edges.sort(key=lambda e: max(e["edge_yes"], e["edge_no"]), reverse=True)
    return edges


def format_bucket(b: dict) -> str:
    lo, hi = b["bucket_low"], b["bucket_high"]
    if lo == -999:
        return f"≤{hi}°F"
    elif hi == 999:
        return f"≥{lo}°F"
    else:
        return f"{lo}-{hi}°F"


def main():
    now = datetime.now(timezone.utc)

    # Scan today and tomorrow
    dates = [
        (now + timedelta(days=0)).strftime("%Y-%m-%d"),
        (now + timedelta(days=1)).strftime("%Y-%m-%d"),
        (now + timedelta(days=2)).strftime("%Y-%m-%d"),
    ]

    all_alerts = []

    for city, (grid_id, gx, gy, lat, lon) in CITIES.items():
        for target_date in dates:
            # Fetch forecasts from multiple models
            noaa_high = fetch_noaa_high(city, target_date)
            om_models = fetch_open_meteo_models(lat, lon, target_date)

            # Combine all model forecasts
            model_highs = dict(om_models)
            if noaa_high is not None:
                model_highs["noaa"] = noaa_high

            if not model_highs:
                continue

            # Fetch PM markets
            buckets, event_slug = fetch_pm_markets(city, target_date)
            if not buckets:
                continue

            # Compute edges
            edges = compute_ensemble_edges(model_highs, buckets)
            for e in edges:
                e["city"] = city
                e["date"] = target_date
                e["models"] = model_highs
                e["event_slug"] = event_slug
                all_alerts.append(e)

    if not all_alerts:
        print("No actionable weather market edges found.")
        return

    # Sort all alerts by edge size
    all_alerts.sort(key=lambda e: max(e["edge_yes"], e["edge_no"]), reverse=True)

    # Format output
    print(f"🌡️ Weather Market Alerts — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print()

    for a in all_alerts[:10]:  # Top 10
        edge = max(a["edge_yes"], a["edge_no"])
        model_avg = a["mu"]
        sigma = a["sigma"]
        bucket = format_bucket(a)
        pm_link = f"https://polymarket.com/event/{a['event_slug']}"

        models_str = ", ".join(f"{k}={v:.0f}°F" for k, v in sorted(a["models"].items()))

        print(f"🎯 {a['city']} {a['date']} — {bucket}")
        print(f"   {a['alert']} | edge {edge:+.0%} | model prob {a['model_prob']:.0%}")
        print(f"   Forecast: {model_avg:.1f}°F ± {sigma:.1f}° ({len(a['models'])} models)")
        print(f"   Models: {models_str}")
        print(f"   {pm_link}")
        print()


if __name__ == "__main__":
    main()
