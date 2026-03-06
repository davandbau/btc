#!/usr/bin/env python3.12
"""
Weather Edge Scanner — Prototype
Fetches NOAA forecasts, compares to Polymarket weather market prices,
and identifies mispricings.

Usage: python3.12 weather-scanner.py [--city NYC] [--date 2026-03-04]
"""

import json
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─── NOAA Grid Points ─────────────────────────────────────────────
# Format: (gridId, gridX, gridY, lat, lon, unit)
# unit: "F" for US cities, "C" for international (NOAA only covers US)
NOAA_CITIES = {
    "NYC":     ("OKX", 34, 38, 40.7831, -73.9712, "F"),
    "Chicago": ("LOT", 76, 73, 41.8781, -87.6298, "F"),
    "Miami":   ("MFL", 76, 50, 25.7617, -80.1918, "F"),
    "Dallas":  ("FWD", 86, 114, 32.7767, -96.7970, "F"),
    "Seattle": ("SEW", 125, 67, 47.6062, -122.3321, "F"),
    "Atlanta": ("FFC", 51, 86, 33.7490, -84.3880, "F"),
}

UA = "(clawdtools.ai, clive@clawdtools.ai)"
BRIEF_DIR = Path(__file__).parent / "briefs"
BRIEF_DIR.mkdir(exist_ok=True)


def fetch_noaa_hourly(city: str) -> list[dict]:
    """Fetch hourly forecast from NOAA api.weather.gov"""
    if city not in NOAA_CITIES:
        print(f"❌ City '{city}' not in NOAA_CITIES. Available: {list(NOAA_CITIES.keys())}")
        return []

    grid_id, grid_x, grid_y, _, _, _ = NOAA_CITIES[city]
    url = f"https://api.weather.gov/gridpoints/{grid_id}/{grid_x},{grid_y}/forecast/hourly"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
        return data["properties"]["periods"]
    except Exception as e:
        print(f"❌ NOAA fetch failed: {e}")
        return []


def get_noaa_daily_high(city: str, target_date: str) -> tuple[int | None, list]:
    """
    Get the forecasted daily high for a specific date.
    target_date: "YYYY-MM-DD"
    Returns: (high_temp_F, hourly_temps_for_that_day)
    """
    periods = fetch_noaa_hourly(city)
    if not periods:
        return None, []

    unit = NOAA_CITIES[city][5]
    day_temps = []
    for p in periods:
        period_date = p["startTime"][:10]
        if period_date == target_date:
            temp = p["temperature"]
            temp_unit = p["temperatureUnit"]
            # Convert to F if needed
            if temp_unit == "C" and unit == "F":
                temp = temp * 9 / 5 + 32
            elif temp_unit == "F" and unit == "C":
                temp = (temp - 32) * 5 / 9
            day_temps.append(temp)

    if not day_temps:
        return None, []

    return max(day_temps), day_temps


def fetch_pm_weather_markets(city: str, target_date: str) -> list[dict]:
    """
    Fetch active PM weather markets for a city/date.
    Returns list of {question, yes_price, no_price, bucket_low, bucket_high, token_yes, token_no, condition_id}
    """
    # Map city names to PM slug patterns
    city_slug_map = {
        "NYC": "nyc",
        "Chicago": "chicago",
        "Miami": "miami",
        "Dallas": "dallas",
        "Seattle": "seattle",
        "Atlanta": "atlanta",
        "London": "london",
        "Toronto": "toronto",
        "Seoul": "seoul",
        "Paris": "paris",
        "Buenos Aires": "buenos-aires",
        "Sao Paulo": "sao-paulo",
        "Wellington": "wellington",
        "Ankara": "ankara",
    }

    slug_city = city_slug_map.get(city, city.lower().replace(" ", "-"))
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    month_name = dt.strftime("%B").lower()
    day = dt.day
    year = dt.year
    slug = f"highest-temperature-in-{slug_city}-on-{month_name}-{day}-{year}"

    url = f"https://gamma-api.polymarket.com/events?slug={slug}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        events = json.loads(resp.read())
    except Exception as e:
        print(f"❌ PM fetch failed: {e}")
        return []

    if not events:
        print(f"⚠️  No PM event found for slug: {slug}")
        return []

    event = events[0]
    print(f"📊 PM Event: {event['title']}")
    print(f"   Description: {event.get('description', '')[:200]}")
    print()

    buckets = []
    for m in event.get("markets", []):
        question = m.get("question", "")
        prices = json.loads(m.get("outcomePrices", "[]"))
        tokens = json.loads(m.get("clobTokenIds", "[]"))
        condition_id = m.get("conditionId", "")
        volume = m.get("volumeNum", 0)

        if len(prices) < 2 or len(tokens) < 2:
            continue

        no_price = float(prices[0])
        yes_price = float(prices[1])

        # Parse bucket range from question
        bucket_low, bucket_high = parse_bucket(question)

        buckets.append({
            "question": question,
            "yes_price": yes_price,
            "no_price": no_price,
            "bucket_low": bucket_low,
            "bucket_high": bucket_high,
            "token_yes": tokens[1] if len(tokens) > 1 else tokens[0],
            "token_no": tokens[0],
            "condition_id": condition_id,
            "volume": volume,
        })

    # Sort by bucket_low
    buckets.sort(key=lambda b: b["bucket_low"] if b["bucket_low"] is not None else -999)
    return buckets


def parse_bucket(question: str) -> tuple[int | None, int | None]:
    """Parse temperature bucket from PM question text."""
    q = question.lower()

    # "be X°F or below" → (-inf, X]
    if "or below" in q:
        import re
        m = re.search(r"be (\d+)°", q)
        if m:
            return -999, int(m.group(1))

    # "be X°F or higher" → [X, inf)
    if "or higher" in q:
        import re
        m = re.search(r"be (\d+)°", q)
        if m:
            return int(m.group(1)), 999

    # "between X-Y°F" → [X, Y]
    import re
    m = re.search(r"between (\d+)-(\d+)°", q)
    if m:
        return int(m.group(1)), int(m.group(2))

    # "be X°C" (exact, for international)
    m = re.search(r"be (\d+)°", q)
    if m:
        return int(m.group(1)), int(m.group(1))

    return None, None


def compute_edges(noaa_high: int, buckets: list[dict]) -> list[dict]:
    """
    Given NOAA's forecasted high, compute probability distribution
    and compare to PM prices.

    Simple model: NOAA point forecast ± uncertainty band.
    Use a crude normal distribution centered on NOAA forecast
    with stddev ~2°F (typical NOAA 24h forecast error).
    """
    from math import exp, sqrt, pi

    sigma = 2.0  # °F — typical NOAA 24h forecast error
    mu = noaa_high

    def normal_pdf(x):
        return exp(-0.5 * ((x - mu) / sigma) ** 2) / (sigma * sqrt(2 * pi))

    edges = []
    for b in buckets:
        low, high = b["bucket_low"], b["bucket_high"]
        if low is None or high is None:
            continue

        # Compute probability that high temp falls in this bucket
        # Integrate normal PDF over bucket range
        # Use simple numerical integration
        actual_low = max(low, mu - 6 * sigma)
        actual_high = min(high, mu + 6 * sigma)

        if actual_low > mu + 6 * sigma or actual_high < mu - 6 * sigma:
            prob = 0.0001
        else:
            steps = 100
            step_size = (actual_high - actual_low) / steps
            prob = sum(normal_pdf(actual_low + (i + 0.5) * step_size) * step_size
                       for i in range(steps))

        # Clamp
        prob = max(0.001, min(0.999, prob))

        # Edge = model probability - market price
        edge_yes = prob - b["yes_price"]
        edge_no = (1 - prob) - b["no_price"]

        b_copy = dict(b)
        b_copy["model_prob"] = prob
        b_copy["edge_yes"] = edge_yes
        b_copy["edge_no"] = edge_no
        b_copy["recommendation"] = None

        # Only flag if edge > 10%
        if edge_yes > 0.10:
            b_copy["recommendation"] = f"BUY YES @ ${b['yes_price']:.4f} (model={prob:.1%}, edge={edge_yes:+.1%})"
        elif edge_no > 0.10:
            b_copy["recommendation"] = f"BUY NO @ ${b['no_price']:.4f} (model={1-prob:.1%}, edge={edge_no:+.1%})"

        edges.append(b_copy)

    return edges


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Weather Edge Scanner")
    parser.add_argument("--city", default="NYC", help="City to scan")
    parser.add_argument("--date", default=None, help="Target date (YYYY-MM-DD)")
    parser.add_argument("--sigma", type=float, default=2.0, help="Forecast uncertainty (°F)")
    args = parser.parse_args()

    if args.date is None:
        # Default to tomorrow
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
        args.date = tomorrow.strftime("%Y-%m-%d")

    city = args.city
    target_date = args.date

    print(f"🌡️  Weather Edge Scanner")
    print(f"   City: {city} | Date: {target_date}")
    print(f"   Forecast uncertainty: σ={args.sigma}°F")
    print("=" * 60)

    # 1. Fetch NOAA forecast
    print(f"\n📡 Fetching NOAA forecast for {city}...")
    high, hourly = get_noaa_daily_high(city, target_date)
    if high is None:
        print(f"❌ No NOAA forecast available for {target_date}")
        return

    print(f"   🌡️  NOAA forecasted high: {high}°F")
    print(f"   📊 Hourly range: {min(hourly)}-{max(hourly)}°F ({len(hourly)} hours)")

    # 2. Fetch PM markets
    print(f"\n📡 Fetching Polymarket weather markets...")
    buckets = fetch_pm_weather_markets(city, target_date)
    if not buckets:
        print("❌ No PM markets found")
        return

    print(f"   Found {len(buckets)} buckets")

    # 3. Compute edges
    print(f"\n🧮 Computing edges (NOAA {high}°F ± {args.sigma}°F)...")
    print("=" * 60)

    edges = compute_edges(high, buckets)

    # Display all buckets
    print(f"\n{'Bucket':<20} {'PM Yes':>8} {'Model':>8} {'Edge':>8} {'Signal'}")
    print("-" * 70)
    for e in edges:
        low, high_b = e["bucket_low"], e["bucket_high"]
        if low == -999:
            label = f"≤{high_b}°F"
        elif high_b == 999:
            label = f"≥{low}°F"
        elif low == high_b:
            label = f"{low}°F"
        else:
            label = f"{low}-{high_b}°F"

        signal = ""
        if e["recommendation"]:
            signal = "⚡ " + e["recommendation"]

        print(f"{label:<20} {e['yes_price']:>7.1%} {e['model_prob']:>7.1%} {e['edge_yes']:>+7.1%}  {signal}")

    # Summary
    opportunities = [e for e in edges if e["recommendation"]]
    print(f"\n{'=' * 60}")
    if opportunities:
        print(f"🎯 {len(opportunities)} OPPORTUNITIES FOUND:")
        for opp in opportunities:
            print(f"   {opp['recommendation']}")
    else:
        print("😐 No actionable edges found (threshold: 10%)")

    # Save brief
    brief = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "city": city,
        "date": target_date,
        "noaa_high": high,
        "hourly_temps": hourly,
        "buckets": edges,
        "opportunities": len(opportunities),
    }
    brief_path = BRIEF_DIR / f"{city.lower()}_{target_date}.json"
    brief_path.write_text(json.dumps(brief, indent=2))
    print(f"\n💾 Brief saved: {brief_path}")


if __name__ == "__main__":
    main()
