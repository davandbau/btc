#!/usr/local/bin/python3.12
"""Serve lag-monitor.html + latest-tokens.json from briefs."""
import http.server, json, glob, os, threading, time

PORT = 8851
DIR = os.path.dirname(os.path.abspath(__file__))
BRIEFS = os.path.join(DIR, "briefs")
TOKENS_FILE = os.path.join(DIR, "latest-tokens.json")

CHAINLINK_FEED_ID = "0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8"
CHAINLINK_API = "https://data.chain.link/api/query-timescale"

def fetch_chainlink():
    """Get latest Chainlink BTC price."""
    import urllib.request
    try:
        url = f"{CHAINLINK_API}?query=LIVE_STREAM_REPORTS_QUERY&variables=%7B%22feedId%22%3A%22{CHAINLINK_FEED_ID}%22%7D"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        nodes = data.get("data", {}).get("liveStreamReports", {}).get("nodes", [])
        if nodes:
            return round(float(nodes[0]["price"]) / 1e18, 2)
    except:
        pass
    return None

def poll_polymarket_price():
    """Poll Chainlink for PM displayed price (same source, ~1s updates)."""
    import urllib.request
    while True:
        try:
            url = f"{CHAINLINK_API}?query=LIVE_STREAM_REPORTS_QUERY&variables=%7B%22feedId%22%3A%22{CHAINLINK_FEED_ID}%22%7D"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            resp = urllib.request.urlopen(req, timeout=5)
            data = json.loads(resp.read())
            nodes = data.get("data", {}).get("liveStreamReports", {}).get("nodes", [])
            if nodes:
                price = round(float(nodes[0]["price"]) / 1e18, 2)
                ts_str = nodes[0]["validFromTimestamp"]
                pm_file = os.path.join(DIR, "pm-live-price.json")
                with open(pm_file, "w") as f:
                    json.dump({"price": price, "timestamp": ts_str, "updated": time.time()}, f)
        except Exception as e:
            pass
        time.sleep(1)

def update_tokens():
    """Write latest token IDs + Chainlink price from most recent brief."""
    while True:
        try:
            files = sorted(glob.glob(os.path.join(BRIEFS, "*_T1.json")), reverse=True)
            cl_price = fetch_chainlink()
            if files:
                b = json.load(open(files[0]))
                pm = b.get("polymarket", {})
                data = {
                    "up_token": pm.get("up_token", ""),
                    "down_token": pm.get("down_token", ""),
                    "strike": b.get("strike", 0),
                    "chainlink_price": cl_price,
                    "window": b.get("window_label", ""),
                    "updated": time.time(),
                }
                with open(TOKENS_FILE, "w") as f:
                    json.dump(data, f)
        except Exception as e:
            print(f"Token update error: {e}")
        time.sleep(2)

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=DIR, **kw)
    def log_message(self, *a):
        pass
    def do_GET(self):
        if self.path.startswith('/api/chainlink'):
            self.proxy_chainlink()
        elif self.path.startswith('/api/pm-price'):
            self.serve_pm_price()
        elif self.path == '/api/futures-live':
            self.serve_futures_live()
        elif self.path.startswith('/api/futures'):
            self.serve_futures()
        else:
            super().do_GET()
    def serve_futures_live(self):
        """Serve live futures state (liqs, spread) from shadow's JSON file."""
        try:
            live_path = os.path.join(DIR, "logs", "futures-live.json")
            if os.path.exists(live_path):
                data = open(live_path).read()
            else:
                data = '{"error":"no data yet"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data.encode())
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f'{{"error":"{e}"}}'.encode())

    def serve_futures(self):
        """Serve latest futures shadow data (last 5 windows + live stats)."""
        try:
            jsonl_path = os.path.join(DIR, "logs", "futures-shadow.jsonl")
            lines = []
            if os.path.exists(jsonl_path):
                with open(jsonl_path) as f:
                    lines = f.readlines()
            recent = [json.loads(l) for l in lines[-12:]]  # last hour
            # Summary stats
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
            data = json.dumps(payload)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data.encode())
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f'{{"error":"{e}"}}'.encode())
    def serve_pm_price(self):
        try:
            pm_file = os.path.join(DIR, "pm-live-price.json")
            with open(pm_file) as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data.encode())
        except:
            self.send_response(404)
            self.end_headers()
    def proxy_chainlink(self):
        import urllib.request
        try:
            url = f"{CHAINLINK_API}?query=LIVE_STREAM_REPORTS_QUERY&variables=%7B%22feedId%22%3A%22{CHAINLINK_FEED_ID}%22%7D"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            resp = urllib.request.urlopen(req, timeout=5)
            data = resp.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(f'{{"error":"{e}"}}'.encode())

if __name__ == "__main__":
    t = threading.Thread(target=update_tokens, daemon=True)
    t.start()
    t2 = threading.Thread(target=poll_polymarket_price, daemon=True)
    t2.start()
    print(f"Lag monitor: http://0.0.0.0:{PORT}/lag-monitor.html")
    http.server.HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
