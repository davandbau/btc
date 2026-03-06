#!/usr/bin/env python3.12
"""
Weather Data Pipeline — Snapshots forecast + market data for analysis.

Captures from multiple sources at regular intervals:
1. NOAA api.weather.gov (GFS-derived hourly forecasts)
2. Open-Meteo multi-model (GFS, ECMWF, ICON, GEM, JMA deterministic)
3. Open-Meteo ensemble (GFS 31-member, ECMWF 51-member, ICON, GEM)
4. METAR/ASOS live observations (KLGA etc — the actual resolution source!)
5. Polymarket CLOB prices for all buckets
6. Weather Underground current conditions (resolution source scrape)

Data stored as JSONL in data/{city}/{date}/snapshots.jsonl
One line per snapshot, timestamped.

Usage:
  python3.12 data-pipeline.py                    # snapshot all active markets
  python3.12 data-pipeline.py --loop --interval 1800  # every 30 min
  python3.12 data-pipeline.py --city NYC --date 2026-03-04
"""

import json
import sys
import time
import urllib.request
import urllib.error
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

UA = "(clawdtools.ai, clive@clawdtools.ai)"
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# ─── City configs ──────────────────────────────────────────────────
# (lat, lon, noaa_grid_id, noaa_grid_x, noaa_grid_y, metar_station, pm_slug_name, unit)
CITIES = {
    "NYC":     (40.7831, -73.9712, "OKX", 34, 38, "KLGA", "nyc", "F"),
    "Chicago": (41.8781, -87.6298, "LOT", 76, 73, "KORD", "chicago", "F"),
    "Miami":   (25.7617, -80.1918, "MFL", 76, 50, "KMIA", "miami", "F"),
    "Dallas":  (32.7767, -96.7970, "FWD", 86, 114, "KDFW", "dallas", "F"),
    "Seattle": (47.6062, -122.3321, "SEW", 125, 67, "KSEA", "seattle", "F"),
    "Atlanta": (33.7490, -84.3880, "FFC", 51, 86, "KATL", "atlanta", "F"),
    "London":  (51.5074, -0.1278, None, None, None, "EGLL", "london", "C"),
    "Toronto": (43.6532, -79.3832, None, None, None, "CYYZ", "toronto", "C"),
    "Seoul":   (37.5665, 126.9780, None, None, None, "RKSI", "seoul", "C"),
    "Paris":   (48.8566, 2.3522, None, None, None, "LFPG", "paris", "C"),
    "Buenos Aires": (-34.6037, -58.3816, None, None, None, "SAEZ", "buenos-aires", "C"),
    "Sao Paulo": (-23.5505, -46.6333, None, None, None, "SBGR", "sao-paulo", "C"),
    "Wellington": (-41.2924, 174.7787, None, None, None, "NZWN", "wellington", "C"),
    "Ankara":  (39.9334, 32.8597, None, None, None, "LTAC", "ankara", "C"),
}


def fetch_json(url: str, ua: str = "Mozilla/5.0", timeout: int = 15) -> dict | list | None:
    """Fetch JSON from URL, return None on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read())
    except Exception as e:
        print(f"  ⚠️  Fetch failed: {url[:60]}... — {e}")
        return None


# ─── 1. NOAA api.weather.gov ──────────────────────────────────────
def fetch_noaa(city: str, target_date: str) -> dict | None:
    """Fetch NOAA hourly forecast for a US city."""
    cfg = CITIES.get(city)
    if not cfg or cfg[2] is None:
        return None  # No NOAA for non-US cities

    lat, lon, grid_id, grid_x, grid_y, _, _, _ = cfg
    url = f"https://api.weather.gov/gridpoints/{grid_id}/{grid_x},{grid_y}/forecast/hourly"
    data = fetch_json(url, ua=UA)
    if not data:
        return None

    props = data.get("properties", {})
    periods = props.get("periods", [])

    day_temps = []
    for p in periods:
        if p["startTime"][:10] == target_date:
            day_temps.append({
                "time": p["startTime"],
                "temp_f": p["temperature"] if p["temperatureUnit"] == "F"
                          else round(p["temperature"] * 9/5 + 32, 1),
                "forecast": p["shortForecast"],
            })

    if not day_temps:
        return None

    temps = [t["temp_f"] for t in day_temps]
    return {
        "source": "noaa_api",
        "update_time": props.get("updateTime"),
        "generated_at": props.get("generatedAt"),
        "high_f": max(temps),
        "low_f": min(temps),
        "hourly": day_temps,
    }


# ─── 2. Open-Meteo multi-model ────────────────────────────────────
def fetch_open_meteo_models(city: str, target_date: str) -> dict | None:
    """Fetch deterministic forecasts from multiple models."""
    cfg = CITIES.get(city)
    if not cfg:
        return None

    lat, lon = cfg[0], cfg[1]
    unit = cfg[7]
    temp_unit = "fahrenheit" if unit == "F" else "celsius"
    models = "gfs_seamless,ecmwf_ifs04,jma_seamless,gem_seamless,icon_seamless"

    url = (f"https://api.open-meteo.com/v1/forecast?"
           f"latitude={lat}&longitude={lon}&hourly=temperature_2m"
           f"&temperature_unit={temp_unit}&models={models}&forecast_days=3")

    data = fetch_json(url)
    if not data:
        return None

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])

    result = {"source": "open_meteo_models"}
    for key in hourly:
        if key == "time":
            continue
        model_name = key.replace("temperature_2m_", "").replace("_seamless", "")
        temps = [temp for t, temp in zip(times, hourly[key])
                 if t.startswith(target_date) and temp is not None]
        if temps:
            result[model_name] = {
                "high": max(temps),
                "low": min(temps),
                "unit": unit,
            }

    return result if len(result) > 1 else None


# ─── 3. Open-Meteo ensemble ───────────────────────────────────────
def fetch_open_meteo_ensemble(city: str, target_date: str) -> dict | None:
    """Fetch ensemble forecasts for probability distribution."""
    cfg = CITIES.get(city)
    if not cfg:
        return None

    lat, lon = cfg[0], cfg[1]
    unit = cfg[7]
    temp_unit = "fahrenheit" if unit == "F" else "celsius"

    url = (f"https://ensemble-api.open-meteo.com/v1/ensemble?"
           f"latitude={lat}&longitude={lon}&hourly=temperature_2m"
           f"&temperature_unit={temp_unit}"
           f"&models=gfs_seamless,ecmwf_ifs025,icon_seamless,gem_global"
           f"&forecast_days=3")

    data = fetch_json(url)
    if not data:
        return None

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])

    # Collect all member highs per model
    from collections import defaultdict
    model_members = defaultdict(list)

    for key in hourly:
        if key == "time" or not key.startswith("temperature"):
            continue
        # Parse model name from key like "temperature_2m_member01_ncep_gefs_seamless"
        parts = key.replace("temperature_2m_", "")
        # Open-Meteo format: "member01_ncep_gefs_seamless" or just "ncep_gefs_seamless"
        member_match = re.match(r"member\d+_(.*)", parts)
        if member_match:
            model = member_match.group(1)
        elif parts.startswith("member"):
            model = "unknown"
        else:
            model = parts

        temps = [temp for t, temp in zip(times, hourly[key])
                 if t.startswith(target_date) and temp is not None]
        if temps:
            model_members[model].append(max(temps))

    result = {"source": "open_meteo_ensemble"}
    for model, highs in model_members.items():
        highs.sort()
        n = len(highs)
        if n == 0:
            continue
        result[model] = {
            "n_members": n,
            "min": round(min(highs), 1),
            "p10": round(highs[max(0, n // 10)], 1),
            "p25": round(highs[max(0, n // 4)], 1),
            "median": round(highs[n // 2], 1),
            "p75": round(highs[min(n - 1, 3 * n // 4)], 1),
            "p90": round(highs[min(n - 1, 9 * n // 10)], 1),
            "max": round(max(highs), 1),
            "unit": unit,
            "all_highs": [round(h, 1) for h in highs],
        }

    return result if len(result) > 1 else None


# ─── 4. METAR live observations ───────────────────────────────────
def fetch_metar(city: str) -> dict | None:
    """Fetch latest METAR observations (actual temps — resolution source!)."""
    cfg = CITIES.get(city)
    if not cfg:
        return None

    station = cfg[5]
    url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json&hours=6"
    data = fetch_json(url, ua=UA)
    if not data or not isinstance(data, list):
        return None

    observations = []
    for obs in data[:12]:  # Last 12 observations
        temp_c = obs.get("temp")
        if temp_c is None:
            continue
        observations.append({
            "time": obs.get("reportTime"),
            "temp_c": temp_c,
            "temp_f": round(temp_c * 9/5 + 32, 1),
            "raw": obs.get("rawOb", "")[:100],
        })

    if not observations:
        return None

    return {
        "source": "metar_asos",
        "station": station,
        "observations": observations,
        "current_temp_f": observations[0]["temp_f"] if observations else None,
        "max_temp_f": max(o["temp_f"] for o in observations),
    }


# ─── 5. Weather Underground (resolution source scrape) ────────────
def fetch_wunderground(city: str, target_date: str) -> dict | None:
    """Scrape Weather Underground for the resolution data."""
    cfg = CITIES.get(city)
    if not cfg:
        return None

    station = cfg[5]
    # WU URL format: /history/daily/us/ny/new-york-city/KLGA/date/2026-3-4
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    date_str = f"{dt.year}-{dt.month}-{dt.day}"

    # Map city to WU location path
    wu_paths = {
        "NYC": "us/ny/new-york-city",
        "Chicago": "us/il/chicago",
        "Miami": "us/fl/miami",
        "Dallas": "us/tx/dallas",
        "Seattle": "us/wa/seattle",
        "Atlanta": "us/ga/atlanta",
        "London": "gb/london",
        "Toronto": "ca/on/toronto",
    }

    wu_path = wu_paths.get(city)
    if not wu_path:
        return None

    url = f"https://www.wunderground.com/history/daily/{wu_path}/{station}/date/{date_str}"

    # We can't easily scrape WU (JavaScript-rendered), but we can note the URL
    return {
        "source": "wunderground",
        "station": station,
        "url": url,
        "note": "Resolution source — check manually or via browser automation",
    }


# ─── 6. Polymarket CLOB prices ────────────────────────────────────
def fetch_pm_prices(city: str, target_date: str) -> dict | None:
    """Fetch current PM bucket prices."""
    cfg = CITIES.get(city)
    if not cfg:
        return None

    slug_city = cfg[6]
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    month = dt.strftime("%B").lower()
    slug = f"highest-temperature-in-{slug_city}-on-{month}-{dt.day}-{dt.year}"

    data = fetch_json(f"https://gamma-api.polymarket.com/events?slug={slug}")
    if not data or not data:
        return None

    event = data[0]
    buckets = []

    for m in event.get("markets", []):
        question = m.get("question", "")
        prices = json.loads(m.get("outcomePrices", "[]"))
        tokens = json.loads(m.get("clobTokenIds", "[]"))
        volume = m.get("volumeNum", 0)

        if len(prices) < 2 or len(tokens) < 2:
            continue

        # Parse bucket range
        bucket_low, bucket_high = None, None
        q = question.lower()
        match = re.search(r"between (\d+)-(\d+)°", q)
        if match:
            bucket_low, bucket_high = int(match.group(1)), int(match.group(2))
        elif "or below" in q:
            match = re.search(r"be (\d+)°", q)
            if match:
                bucket_low, bucket_high = -999, int(match.group(1))
        elif "or higher" in q:
            match = re.search(r"be (\d+)°", q)
            if match:
                bucket_low, bucket_high = int(match.group(1)), 999
        elif re.search(r"be (\d+)°", q):
            v = int(re.search(r"be (\d+)°", q).group(1))
            bucket_low = bucket_high = v

        buckets.append({
            "question": question,
            "bucket_low": bucket_low,
            "bucket_high": bucket_high,
            "yes_price": float(prices[1]),
            "no_price": float(prices[0]),
            "volume": volume,
            "token_yes": tokens[1] if len(tokens) > 1 else None,
            "token_no": tokens[0],
            "condition_id": m.get("conditionId", ""),
        })

    buckets.sort(key=lambda b: b["bucket_low"] if b["bucket_low"] is not None else -999)

    return {
        "source": "polymarket_clob",
        "event_title": event.get("title", ""),
        "event_slug": slug,
        "total_volume": sum(b["volume"] for b in buckets),
        "buckets": buckets,
    }


# ─── 7. HRRR (via Open-Meteo, hourly + 15-min) ────────────────────
def fetch_hrrr(city: str, target_date: str) -> dict | None:
    """Fetch HRRR rapid-refresh forecast (US only, updates hourly)."""
    cfg = CITIES.get(city)
    if not cfg:
        return None

    lat, lon = cfg[0], cfg[1]
    unit = cfg[7]
    if unit != "F":
        return None  # HRRR is US-only

    temp_unit = "fahrenheit"

    # Hourly data
    url_h = (f"https://api.open-meteo.com/v1/forecast?"
             f"latitude={lat}&longitude={lon}&hourly=temperature_2m"
             f"&temperature_unit={temp_unit}&models=ncep_hrrr_conus&forecast_days=3")
    data_h = fetch_json(url_h)

    # 15-minute data (sub-hourly resolution)
    url_15 = (f"https://api.open-meteo.com/v1/forecast?"
              f"latitude={lat}&longitude={lon}&minutely_15=temperature_2m"
              f"&temperature_unit={temp_unit}&forecast_days=2")
    data_15 = fetch_json(url_15)

    result = {"source": "hrrr"}

    if data_h:
        hourly = data_h.get("hourly", {})
        times = hourly.get("time", [])
        for k in hourly:
            if k == "time":
                continue
            temps = [temp for t, temp in zip(times, hourly[k])
                     if t.startswith(target_date) and temp is not None]
            if temps:
                result["hourly_high"] = max(temps)
                result["hourly_low"] = min(temps)
                result["hourly_points"] = len(temps)

    if data_15:
        m15 = data_15.get("minutely_15", {})
        times = m15.get("time", [])
        temps_15 = m15.get("temperature_2m", [])
        day_temps = [temp for t, temp in zip(times, temps_15)
                     if t.startswith(target_date) and temp is not None]
        if day_temps:
            result["min15_high"] = max(day_temps)
            result["min15_low"] = min(day_temps)
            result["min15_points"] = len(day_temps)

    return result if len(result) > 1 else None


# ─── 8. Nearby stations (ground truth cluster) ────────────────────
def fetch_nearby_stations(city: str) -> dict | None:
    """Fetch observations from nearby METAR stations for cross-validation.
    Multiple stations near the resolution station help detect microclimates
    and validate the primary station's readings."""

    # Nearby station clusters
    NEARBY = {
        "NYC":     ["KLGA", "KJFK", "KEWR", "KNYC"],  # LaGuardia + JFK + Newark + Central Park
        "Chicago": ["KORD", "KMDW", "KPWK"],           # O'Hare + Midway + Palwaukee
        "Miami":   ["KMIA", "KFLL", "KOPF"],            # Miami + Ft Lauderdale + Opa-locka
        "Dallas":  ["KDFW", "KDAL", "KADS"],             # DFW + Love Field + Addison
        "Seattle": ["KSEA", "KBFI", "KPAE"],             # Sea-Tac + Boeing Field + Paine
        "Atlanta": ["KATL", "KPDK", "KFTY"],             # Hartsfield + DeKalb + Fulton
        "London":  ["EGLL", "EGLC", "EGKK"],             # Heathrow + City + Gatwick
        "Toronto": ["CYYZ", "CYKZ", "CYTZ"],             # Pearson + Buttonville + Island
    }

    stations = NEARBY.get(city, [])
    if not stations:
        return None

    results = {"source": "nearby_stations", "stations": {}}

    for station in stations:
        url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json&hours=24"
        data = fetch_json(url, ua=UA)
        if not data or not isinstance(data, list):
            continue

        temps = []
        for obs in data:
            temp_c = obs.get("temp")
            if temp_c is not None:
                temps.append(round(temp_c * 9/5 + 32, 1))

        if temps:
            results["stations"][station] = {
                "current_f": temps[0],
                "max_24h_f": max(temps),
                "min_24h_f": min(temps),
                "n_obs": len(temps),
            }

    return results if results["stations"] else None


# ─── 9. NWS station observations (pressure, wind, conditions) ─────
def fetch_nws_observations(city: str) -> dict | None:
    """Fetch detailed NWS observations including pressure (useful for
    predicting temperature changes — falling pressure = fronts/storms)."""
    cfg = CITIES.get(city)
    if not cfg or cfg[2] is None:
        return None  # US only

    station = cfg[5]
    url = f"https://api.weather.gov/stations/{station}/observations?limit=12"
    data = fetch_json(url, ua=UA)
    if not data:
        return None

    observations = []
    for feat in data.get("features", [])[:12]:
        props = feat.get("properties", {})
        temp_c = props.get("temperature", {}).get("value")
        pressure = props.get("barometricPressure", {}).get("value")
        wind = props.get("windSpeed", {}).get("value")
        humidity = props.get("relativeHumidity", {}).get("value")

        observations.append({
            "time": props.get("timestamp", "")[:19],
            "temp_f": round(temp_c * 9/5 + 32, 1) if temp_c is not None else None,
            "pressure_pa": pressure,
            "wind_ms": wind,
            "humidity_pct": round(humidity, 1) if humidity is not None else None,
            "description": props.get("textDescription", ""),
        })

    if not observations:
        return None

    # Compute pressure trend (rising/falling = weather change indicator)
    pressures = [o["pressure_pa"] for o in observations if o["pressure_pa"] is not None]
    pressure_trend = None
    if len(pressures) >= 2:
        pressure_trend = round(pressures[0] - pressures[-1], 0)  # positive = rising

    return {
        "source": "nws_observations",
        "station": station,
        "pressure_trend_pa": pressure_trend,
        "pressure_trend_label": "rising" if pressure_trend and pressure_trend > 50 else
                                "falling" if pressure_trend and pressure_trend < -50 else "steady",
        "observations": observations[:6],  # Keep last 6 to save space
    }


# ─── 10. PM price history ─────────────────────────────────────────
def fetch_pm_price_history(token_id: str) -> list | None:
    """Fetch CLOB price history for a token."""
    url = f"https://clob.polymarket.com/prices-history?market={token_id}&interval=all&fidelity=60"
    data = fetch_json(url)
    if not data:
        return None
    return data.get("history", [])


# ─── Snapshot ──────────────────────────────────────────────────────
def snapshot(city: str, target_date: str) -> dict:
    """Take a full snapshot of all data sources for a city/date."""
    print(f"\n{'='*60}")
    print(f"📸 Snapshot: {city} — {target_date}")
    print(f"   Time: {datetime.now(timezone.utc).isoformat()}")
    print(f"{'='*60}")

    snap = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "city": city,
        "target_date": target_date,
    }

    # 1. NOAA
    print("  📡 NOAA api.weather.gov...", end=" ")
    noaa = fetch_noaa(city, target_date)
    if noaa:
        print(f"✅ high={noaa['high_f']}°F")
        snap["noaa"] = noaa
    else:
        print("⏭️  skipped (non-US or unavailable)")

    # 2. Open-Meteo models
    print("  🌍 Open-Meteo multi-model...", end=" ")
    om = fetch_open_meteo_models(city, target_date)
    if om:
        models = [k for k in om if k != "source"]
        highs = [om[k]["high"] for k in models if "high" in om[k]]
        print(f"✅ {len(models)} models, highs={[round(h,1) for h in highs]}")
        snap["open_meteo_models"] = om
    else:
        print("❌")

    # 3. Open-Meteo ensemble
    print("  🎲 Open-Meteo ensemble...", end=" ")
    ens = fetch_open_meteo_ensemble(city, target_date)
    if ens:
        models = [k for k in ens if k != "source"]
        total = sum(ens[k]["n_members"] for k in models)
        print(f"✅ {total} total members across {len(models)} models")
        snap["ensemble"] = ens
    else:
        print("❌")

    # 4. METAR
    print("  ✈️  METAR/ASOS observations...", end=" ")
    metar = fetch_metar(city)
    if metar:
        print(f"✅ current={metar['current_temp_f']}°F, max(6h)={metar['max_temp_f']}°F")
        snap["metar"] = metar
    else:
        print("❌")

    # 5. HRRR
    print("  ⚡ HRRR rapid-refresh...", end=" ")
    hrrr = fetch_hrrr(city, target_date)
    if hrrr:
        high = hrrr.get("hourly_high") or hrrr.get("min15_high")
        print(f"✅ high={high}°F")
        snap["hrrr"] = hrrr
    else:
        print("⏭️  skipped (non-US)")

    # 6. Nearby stations
    print("  📍 Nearby stations...", end=" ")
    nearby = fetch_nearby_stations(city)
    if nearby:
        n = len(nearby["stations"])
        print(f"✅ {n} stations")
        snap["nearby_stations"] = nearby
    else:
        print("⏭️  none configured")

    # 7. NWS detailed observations (pressure/wind)
    print("  🌡️  NWS obs (pressure/wind)...", end=" ")
    nws = fetch_nws_observations(city)
    if nws:
        trend = nws.get("pressure_trend_label", "?")
        print(f"✅ pressure {trend}")
        snap["nws_observations"] = nws
    else:
        print("⏭️  skipped")

    # 8. WU
    wu = fetch_wunderground(city, target_date)
    if wu:
        snap["wunderground"] = wu

    # 9. PM prices
    print("  📊 Polymarket CLOB...", end=" ")
    pm = fetch_pm_prices(city, target_date)
    if pm:
        print(f"✅ {len(pm['buckets'])} buckets, vol=${pm['total_volume']:,.0f}")
        snap["polymarket"] = pm
    else:
        print("❌ no market found")

    return snap


def find_active_markets() -> list[tuple[str, str]]:
    """Discover all active weather temperature markets on PM."""
    print("🔍 Discovering active weather markets...")
    data = fetch_json(
        "https://gamma-api.polymarket.com/events?closed=false&limit=200&tag=Weather&order=volume24hr&ascending=false"
    )
    if not data:
        return []

    markets = []
    for event in data:
        title = event.get("title", "")
        if "temperature" not in title.lower():
            continue

        # Parse city and date from title
        # "Highest temperature in NYC on March 4?"
        match = re.search(r"temperature in (.+?) on (.+?)\?", title)
        if not match:
            continue

        city_raw = match.group(1)
        date_raw = match.group(2)

        # Map PM city name to our city key
        city_map = {
            "nyc": "NYC", "new york city": "NYC",
            "chicago": "Chicago", "miami": "Miami",
            "dallas": "Dallas", "seattle": "Seattle",
            "atlanta": "Atlanta", "london": "London",
            "toronto": "Toronto", "seoul": "Seoul",
            "paris": "Paris", "buenos aires": "Buenos Aires",
            "sao paulo": "Sao Paulo", "são paulo": "Sao Paulo",
            "wellington": "Wellington", "ankara": "Ankara",
        }

        city = city_map.get(city_raw.lower())
        if not city:
            print(f"  ⚠️  Unknown city: {city_raw}")
            continue

        # Parse date: "March 4" → 2026-03-04
        try:
            year = datetime.now().year
            dt = datetime.strptime(f"{date_raw} {year}", "%B %d %Y")
            target_date = dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

        slug = event.get("slug", "")
        vol = event.get("volume24hr", 0)
        markets.append((city, target_date))
        print(f"  📌 {city} — {target_date} (vol=${vol:,.0f})")

    return markets


def save_snapshot(snap: dict):
    """Append snapshot to JSONL file."""
    city = snap["city"].lower().replace(" ", "_")
    date = snap["target_date"]
    out_dir = DATA_DIR / city / date
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "snapshots.jsonl"

    with open(out_path, "a") as f:
        f.write(json.dumps(snap) + "\n")

    print(f"  💾 Saved to {out_path}")


def run_once(cities_dates: list[tuple[str, str]] | None = None):
    """Run one snapshot cycle."""
    if not cities_dates:
        cities_dates = find_active_markets()

    if not cities_dates:
        print("❌ No active weather markets found")
        return

    for city, target_date in cities_dates:
        try:
            snap = snapshot(city, target_date)
            save_snapshot(snap)
        except Exception as e:
            print(f"  ❌ Error snapshotting {city}/{target_date}: {e}")

    print(f"\n✅ Snapshot cycle complete — {len(cities_dates)} markets captured")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Weather Data Pipeline")
    parser.add_argument("--city", help="Specific city to snapshot")
    parser.add_argument("--date", help="Specific date (YYYY-MM-DD)")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--interval", type=int, default=1800, help="Seconds between snapshots (default: 30 min)")
    args = parser.parse_args()

    cities_dates = None
    if args.city and args.date:
        cities_dates = [(args.city, args.date)]

    if args.loop:
        print(f"🔄 Running in loop mode (interval: {args.interval}s)")
        while True:
            try:
                run_once(cities_dates)
                print(f"\n⏰ Next snapshot in {args.interval}s...")
                time.sleep(args.interval)
            except KeyboardInterrupt:
                print("\n🛑 Stopped")
                break
    else:
        run_once(cities_dates)


if __name__ == "__main__":
    main()
