#!/bin/bash
# Infrastructure watchdog — keeps lag-server and cloudflared alive
# Run via cron every 5 minutes

WORKSPACE="/Users/davidbaum/.openclaw/workspace"
RESTART_LOG="/tmp/infra-watchdog.log"

ok=true

# Check lag-server
if ! pgrep -f "lag-server.py" > /dev/null 2>&1; then
    echo "$(date -u '+%Y-%m-%d %H:%M:%S') Restarting lag-server" >> "$RESTART_LOG"
    cd "$WORKSPACE/polymarket/shared"
    nohup python3.12 lag-server.py >> /tmp/lag-server.log 2>&1 &
    ok=false
fi

# Check cloudflared tunnel
if ! pgrep -f "cloudflared tunnel run" > /dev/null 2>&1; then
    echo "$(date -u '+%Y-%m-%d %H:%M:%S') Restarting cloudflared" >> "$RESTART_LOG"
    nohup cloudflared tunnel run clawdtools >> /tmp/cloudflared.log 2>&1 &
    ok=false
fi

if [ "$ok" = true ]; then
    echo "OK"
else
    echo "RESTARTED — check /tmp/infra-watchdog.log"
fi
