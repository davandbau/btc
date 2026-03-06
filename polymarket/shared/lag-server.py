#!/usr/local/bin/python3.12
"""Multi-asset dashboard server. Serves lag-monitor.html + per-asset APIs."""
import http.server, json, glob, os, threading, time, base64, subprocess

PORT = 8851
DIR = os.path.dirname(os.path.abspath(__file__))
POLY_DIR = os.path.dirname(DIR)  # parent: polymarket/

# Asset directories
ASSET_DIRS = {
    "btc": os.path.join(POLY_DIR, "btc"),
    "eth": os.path.join(POLY_DIR, "eth"),
    "sol": os.path.join(POLY_DIR, "sol"),
    "xrp": os.path.join(POLY_DIR, "xrp"),
}

# Chainlink feed IDs per asset
CHAINLINK_FEEDS = {
    "btc": "0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8",
    "eth": "0x000359843a543ee2fe414dc14c7e7920ef10f4372990b79d6361cdc0dd1ba782",
    "sol": "0x0003b778d3f6b2ac4991302b89cb313f99a42467d6c9c5f96f57c29c0d2bc24f",
    "xrp": "0x0003c16c6aed42294f5cb4741f6e59ba2d728f0eae2eb9e6d3f555808c59fc45",
}
CHAINLINK_API = "https://data.chain.link/api/query-timescale"

# Cached Chainlink data per asset
_chainlink_cache = {
    "btc": {"data": None, "raw": b'{"error":"no data yet"}'},
    "sol": {"data": None, "raw": b'{"error":"no data yet"}'},
    "xrp": {"data": None, "raw": b'{"error":"no data yet"}'},
    "eth": {"data": None, "raw": b'{"error":"no data yet"}'},
}

def poll_chainlink_prices():
    """Poll Chainlink for all asset prices (~2s cycle, staggered)."""
    import urllib.request
    while True:
        for asset, feed_id in CHAINLINK_FEEDS.items():
            try:
                url = f"{CHAINLINK_API}?query=LIVE_STREAM_REPORTS_QUERY&variables=%7B%22feedId%22%3A%22{feed_id}%22%7D"
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                resp = urllib.request.urlopen(req, timeout=5)
                raw = resp.read()
                data = json.loads(raw)
                _chainlink_cache[asset] = {"data": data, "raw": raw}
                nodes = data.get("data", {}).get("liveStreamReports", {}).get("nodes", [])
                if nodes:
                    price = round(float(nodes[0]["price"]) / 1e18, 2)
                    ts_str = nodes[0]["validFromTimestamp"]
                    pm_file = os.path.join(DIR, f"pm-live-price-{asset}.json")
                    with open(pm_file, "w") as f:
                        json.dump({"price": price, "timestamp": ts_str, "updated": time.time()}, f)
            except:
                pass
            time.sleep(1)  # stagger between assets

def update_tokens():
    """Write latest token IDs from most recent BTC brief."""
    tokens_file = os.path.join(DIR, "latest-tokens.json")
    btc_briefs = os.path.join(ASSET_DIRS["btc"], "briefs")
    while True:
        try:
            files = sorted(glob.glob(os.path.join(btc_briefs, "*_T1.json")), reverse=True)
            if files:
                b = json.load(open(files[0]))
                pm = b.get("polymarket", {})
                # Get price from cache
                cl_data = _chainlink_cache["btc"]["data"]
                cl_price = None
                if cl_data:
                    nodes = cl_data.get("data", {}).get("liveStreamReports", {}).get("nodes", [])
                    if nodes:
                        cl_price = round(float(nodes[0]["price"]) / 1e18, 2)
                data = {
                    "up_token": pm.get("up_token", ""),
                    "down_token": pm.get("down_token", ""),
                    "strike": b.get("strike", 0),
                    "chainlink_price": cl_price,
                    "window": b.get("window_label", ""),
                    "updated": time.time(),
                }
                with open(tokens_file, "w") as f:
                    json.dump(data, f)
        except Exception as e:
            print(f"Token update error: {e}")
        time.sleep(2)

# Basic auth
AUTH_USER = "david"
AUTH_PASS = "bjy0KerftE0YFYWzBV6hNw"
AUTH_REALM = "Trading Dashboard"

def parse_asset(path):
    """Extract ?asset=xxx from query string. Default: btc."""
    if '?' not in path:
        return "btc"
    qs = path.split('?')[1]
    for param in qs.split('&'):
        if param.startswith('asset='):
            a = param.split('=')[1].lower()
            if a in ASSET_DIRS:
                return a
    return "btc"

def parse_param(path, key, default=None):
    """Extract a query param value."""
    if '?' not in path:
        return default
    qs = path.split('?')[1]
    for param in qs.split('&'):
        if param.startswith(f'{key}='):
            return param.split('=')[1]
    return default

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=DIR, **kw)
    def log_message(self, *a):
        pass
    def check_auth(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            return decoded == f"{AUTH_USER}:{AUTH_PASS}"
        except:
            return False
    def list_directory(self, path):
        self.send_error(403, "Forbidden")
        return None
    def do_GET(self):
        if not self.path.startswith('/api/') and not self.check_auth():
            self.send_response(401)
            self.send_header("WWW-Authenticate", f'Basic realm="{AUTH_REALM}"')
            self.end_headers()
            return
        base = self.path.split('?')[0]
        if base == '/api/time':
            import time as _t
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"ts": int(_t.time() * 1000)}).encode())
            return
        if base == '/api/chainlink':
            self.proxy_chainlink()
        elif base == '/api/pm-price':
            self.serve_pm_price()
        elif base == '/api/futures-live':
            self.serve_futures_live()
        elif base == '/api/regime':
            self.serve_regime()
        elif base == '/api/tail':
            self.serve_tail()
        elif base == '/api/futures':
            self.serve_futures()
        else:
            super().do_GET()
    def end_headers(self):
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def _json_response(self, data_str, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data_str.encode() if isinstance(data_str, str) else data_str)

    def serve_tail(self):
        """Tail the reasoning bot log for a given asset."""
        try:
            asset = parse_asset(self.path)
            n = int(parse_param(self.path, 'n', '20'))
            n = min(n, 100)
            asset_dir = ASSET_DIRS.get(asset, ASSET_DIRS["btc"])
            log_path = os.path.join(asset_dir, "logs", "reasoning-loop.log")
            result = subprocess.run(['tail', f'-{n}', log_path], capture_output=True, text=True)
            lines = result.stdout.strip().split('\n') if result.stdout.strip() else []
            self._json_response(json.dumps({"lines": lines, "asset": asset}))
        except Exception as e:
            self._json_response(f'{{"error":"{e}"}}', 500)

    def serve_regime(self):
        """Serve live regime/trend data for a given asset."""
        try:
            asset = parse_asset(self.path)
            asset_dir = ASSET_DIRS.get(asset, ASSET_DIRS["btc"])
            path = os.path.join(asset_dir, "logs", "regime-live.json")
            data = open(path).read() if os.path.exists(path) else '{"error":"no data yet"}'
            self._json_response(data)
        except Exception as e:
            self._json_response(f'{{"error":"{e}"}}', 500)

    def serve_futures_live(self):
        """Serve live futures state from BTC shadow."""
        try:
            live_path = os.path.join(ASSET_DIRS["btc"], "logs", "futures-live.json")
            data = open(live_path).read() if os.path.exists(live_path) else '{"error":"no data yet"}'
            self._json_response(data)
        except Exception as e:
            self._json_response(f'{{"error":"{e}"}}', 500)

    def serve_futures(self):
        """Serve futures shadow data (BTC only)."""
        try:
            jsonl_path = os.path.join(ASSET_DIRS["btc"], "logs", "futures-shadow.jsonl")
            lines = []
            if os.path.exists(jsonl_path):
                with open(jsonl_path) as f:
                    lines = f.readlines()
            recent = [json.loads(l) for l in lines[-12:]]
            scored = [r for r in recent if r.get("spread_correct") is not None]
            liq_scored = [r for r in recent if r.get("liq_correct") is not None]
            payload = {
                "windows": recent[-5:],
                "spread_accuracy": round(sum(1 for r in scored if r["spread_correct"]) / len(scored) * 100, 1) if scored else None,
                "liq_accuracy": round(sum(1 for r in liq_scored if r["liq_correct"]) / len(liq_scored) * 100, 1) if liq_scored else None,
                "total_windows": len(recent),
                "latest": recent[-1] if recent else None,
                "updated": time.time(),
            }
            self._json_response(json.dumps(payload))
        except Exception as e:
            self._json_response(f'{{"error":"{e}"}}', 500)

    def serve_pm_price(self):
        """Serve cached Chainlink price for asset."""
        try:
            asset = parse_asset(self.path)
            pm_file = os.path.join(DIR, f"pm-live-price-{asset}.json")
            # Fallback to old filename for backwards compat
            if not os.path.exists(pm_file):
                pm_file = os.path.join(DIR, "pm-live-price.json")
            data = open(pm_file).read()
            self._json_response(data)
        except:
            self.send_response(404)
            self.end_headers()

    def proxy_chainlink(self):
        """Serve cached Chainlink data for asset."""
        asset = parse_asset(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.end_headers()
        self.wfile.write(_chainlink_cache[asset]["raw"])

if __name__ == "__main__":
    t = threading.Thread(target=update_tokens, daemon=True)
    t.start()
    t2 = threading.Thread(target=poll_chainlink_prices, daemon=True)
    t2.start()
    print(f"Dashboard: http://0.0.0.0:{PORT}/lag-monitor.html")
    http.server.HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
