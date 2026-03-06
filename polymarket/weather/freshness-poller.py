#!/usr/bin/env python3.12
"""
Weather Model Freshness Poller
Checks if Open-Meteo has published new GFS/ECMWF model data.
If fresh data detected → runs yes-sniper.py and outputs results.
If data is stale (already scanned this run) → exits silently.

State tracked in weather/state/last-model-run.json

Designed to run every 15min during model arrival windows via cron.
"""

import json
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

WEATHER_DIR = Path(__file__).parent
STATE_DIR = WEATHER_DIR / "state"
STATE_DIR.mkdir(exist_ok=True)
STATE_PATH = STATE_DIR / "last-model-run.json"

# Open-Meteo metadata endpoint — returns model init times
# We check GFS since it's the primary model and updates 4x/day
METEO_CHECK_URL = (
    "https://api.open-meteo.com/v1/forecast?"
    "latitude=40.78&longitude=-73.97"  # NYC as reference point
    "&hourly=temperature_2m"
    "&models=gfs_seamless"
    "&forecast_days=1"
)


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"last_model_init": None, "last_scan_utc": None, "scans_today": 0}


def save_state(state: dict):
    STATE_PATH.write_text(json.dumps(state, indent=2))


def get_model_init_time() -> str | None:
    """
    Query Open-Meteo and extract the model initialization time.
    Open-Meteo includes this in the response metadata.
    Returns ISO string or None.
    """
    try:
        resp = urllib.request.urlopen(METEO_CHECK_URL, timeout=15)
        data = json.loads(resp.read())

        # Open-Meteo doesn't always expose init time directly,
        # but we can infer freshness from the first timestamp in hourly data
        # and compare to what we had before.
        #
        # Better approach: check the generationtime_ms and the time range.
        # If the hourly data starts from a different base, the model updated.

        # The most reliable signal: check if hourly data has changed
        # by hashing the first few temperature values
        hourly = data.get("hourly", {})
        temps = hourly.get("temperature_2m_gfs_seamless",
                          hourly.get("temperature_2m", []))[:6]

        if not temps:
            return None

        # Create a fingerprint from the first 6 hourly values
        fingerprint = "|".join(f"{t:.1f}" for t in temps if t is not None)
        return fingerprint

    except Exception as e:
        print(f"⚠️ Model check failed: {e}", file=sys.stderr)
        return None


def main():
    state = load_state()
    now = datetime.now(timezone.utc)

    # Get current model fingerprint
    fingerprint = get_model_init_time()

    if fingerprint is None:
        print("STALE — couldn't fetch model data")
        return

    if fingerprint == state.get("last_model_init"):
        # Same data as last check — no new model run
        print("STALE — no new model data")
        return

    # New data detected!
    print(f"FRESH — new model data detected")
    print(f"  Old fingerprint: {state.get('last_model_init', 'none')}")
    print(f"  New fingerprint: {fingerprint}")
    print()

    # Update state
    state["last_model_init"] = fingerprint
    state["last_scan_utc"] = now.isoformat()
    state["scans_today"] = state.get("scans_today", 0) + 1
    save_state(state)

    # Run the YES sniper
    import subprocess
    result = subprocess.run(
        [sys.executable, str(WEATHER_DIR / "yes-sniper.py")],
        capture_output=True, text=True, timeout=120
    )

    if result.stdout.strip():
        print(result.stdout)
    if result.stderr.strip():
        print(result.stderr, file=sys.stderr)


if __name__ == "__main__":
    main()
