#!/usr/bin/env python3
"""Supervisor: keeps trading loops and dashboard alive."""
import subprocess
import sys
import time
import os
import signal

WORKDIR = "/Users/davidbaum/.openclaw/workspace-polymarket"
TRADING = os.path.join(WORKDIR, "tools/trading")

PROCESSES = {
    "5m": {
        "cmd": [sys.executable, "-u", os.path.join(TRADING, "reasoning-loop.py")],
        "log": os.path.join(TRADING, "loop-5m.log"),
    },
    "15m": {
        "cmd": [sys.executable, "-u", os.path.join(TRADING, "reasoning-loop-15m.py")],
        "log": os.path.join(TRADING, "loop-15m.log"),
    },
    "dashboard": {
        "cmd": [sys.executable, "-u", os.path.join(TRADING, "dashboard.py")],
        "log": os.path.join(TRADING, "dashboard.log"),
    },
}

running = {}
should_exit = False


def handle_signal(sig, frame):
    global should_exit
    should_exit = True
    for name, proc in running.items():
        if proc and proc.poll() is None:
            proc.terminate()
    sys.exit(0)


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


def start_process(name):
    cfg = PROCESSES[name]
    log_file = open(cfg["log"], "a", buffering=1)
    proc = subprocess.Popen(
        cfg["cmd"],
        cwd=WORKDIR,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    running[name] = proc
    ts = time.strftime("%H:%M:%S", time.gmtime())
    print(f"[{ts}] Started {name} (PID {proc.pid})", flush=True)
    return proc


# Start all
for name in PROCESSES:
    start_process(name)

# Monitor loop
while not should_exit:
    time.sleep(5)
    for name in PROCESSES:
        proc = running.get(name)
        if proc and proc.poll() is not None:
            ts = time.strftime("%H:%M:%S", time.gmtime())
            print(f"[{ts}] {name} exited (code {proc.returncode}), restarting...", flush=True)
            time.sleep(3)
            start_process(name)
