#!/bin/bash
# Supervisor: keeps all three processes alive
WORKDIR=/Users/davidbaum/.openclaw/workspace-polymarket
LOG5="$WORKDIR/tools/trading/loop-5m.log"
LOG15="$WORKDIR/tools/trading/loop-15m.log"
LOGDASH="$WORKDIR/tools/trading/dashboard.log"

restart_5m() {
    while true; do
        cd "$WORKDIR"
        python3 -u tools/trading/reasoning-loop.py >> "$LOG5" 2>&1
        echo "[$(date -u +%H:%M:%S)] 5m loop exited, restarting in 5s..." >> "$LOG5"
        sleep 5
    done
}

restart_15m() {
    while true; do
        cd "$WORKDIR"
        python3 -u tools/trading/reasoning-loop-15m.py >> "$LOG15" 2>&1
        echo "[$(date -u +%H:%M:%S)] 15m loop exited, restarting in 5s..." >> "$LOG15"
        sleep 5
    done
}

restart_dash() {
    while true; do
        cd "$WORKDIR"
        python3 -u tools/trading/dashboard.py >> "$LOGDASH" 2>&1
        echo "[$(date -u +%H:%M:%S)] dashboard exited, restarting in 5s..." >> "$LOGDASH"
        sleep 5
    done
}

restart_5m &
restart_15m &
restart_dash &

# Wait for all background jobs
wait
