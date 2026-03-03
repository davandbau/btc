#!/bin/bash
# Bot control script — start/stop the reasoning bot + watchdog
# Usage: ./bot.sh start | stop | status | unlock | lock

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$BOT_DIR/logs/reasoning-loop.log"
REDEEM_LOG="$BOT_DIR/logs/redeem-watcher.log"
NO_TRADE="$BOT_DIR/NO_TRADE"
PIDFILE="$BOT_DIR/bot.pid"
WATCHDOG_ID="ed2b9212-92a1-4689-90ae-c14f4b18cff9"

get_pid() {
    # Primary: pidfile. Fallback: pgrep with full path.
    if [ -f "$PIDFILE" ]; then
        local pid=$(cat "$PIDFILE" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo "$pid"
            return
        fi
        rm -f "$PIDFILE"  # stale pidfile
    fi
    pgrep -f "$BOT_DIR/reasoning-loop.py" 2>/dev/null | head -1
}

get_redeem_pid() {
    ps aux | grep "redeem-watcher.py" | grep -v grep | awk '{print $2}' | head -1
}

watchdog_enabled() {
    openclaw cron list --json 2>/dev/null | python3.12 -c "
import sys, json
data = json.load(sys.stdin)
for j in data.get('jobs', []):
    if j['id'] == '$WATCHDOG_ID':
        print('true' if j.get('enabled') else 'false')
        break
else:
    print('missing')
"
}

case "$1" in
    start)
        PID=$(get_pid)
        if [ -n "$PID" ]; then
            echo "⚠️  Bot already running (PID $PID)"
            exit 1
        fi

        # Enable watchdog first — must succeed before starting bot
        echo "🐕 Enabling watchdog..."
        RESULT=$(openclaw cron enable "$WATCHDOG_ID" 2>&1)
        if [ $? -ne 0 ]; then
            echo "❌ Failed to enable watchdog — aborting start"
            echo "   $RESULT"
            exit 1
        fi

        # Verify watchdog is actually enabled
        WD_STATE=$(watchdog_enabled | tr -d '[:space:]')
        if [ "$WD_STATE" != "true" ]; then
            echo "❌ Watchdog not confirmed enabled ($WD_STATE) — aborting start"
            exit 1
        fi
        echo "   ✅ Watchdog confirmed enabled"

        # Start bot
        echo "🚀 Starting reasoning bot..."
        cd "$BOT_DIR"
        nohup python3.12 -u reasoning-loop.py --live > "$LOG" 2>&1 &
        NEW_PID=$!
        echo "$NEW_PID" > "$PIDFILE"
        sleep 2

        if kill -0 "$NEW_PID" 2>/dev/null; then
            echo "   ✅ Bot running (PID $NEW_PID)"

            # Start redeem watcher
            REDEEM_PID=$(get_redeem_pid)
            if [ -z "$REDEEM_PID" ]; then
                nohup python3.12 "$BOT_DIR/redeem-watcher.py" > "$REDEEM_LOG" 2>&1 &
                echo "   ✅ Redeem watcher running (PID $!)"
            else
                echo "   ℹ️  Redeem watcher already running (PID $REDEEM_PID)"
            fi

            if [ -f "$NO_TRADE" ]; then
                echo ""
                echo "⛔ NO_TRADE block still active — run './bot.sh unlock' when ready"
            fi
        else
            echo "❌ Bot failed to start — check $LOG"
            echo "🐕 Rolling back watchdog..."
            openclaw cron disable "$WATCHDOG_ID" > /dev/null 2>&1
            exit 1
        fi
        ;;

    stop)
        # Disable watchdog first
        echo "🐕 Disabling watchdog..."
        openclaw cron disable "$WATCHDOG_ID" > /dev/null 2>&1

        # Verify watchdog is disabled
        WD_STATE=$(watchdog_enabled | tr -d "[:space:]")
        if [ "$WD_STATE" = "true" ]; then
            echo "❌ Watchdog still enabled — retrying..."
            openclaw cron disable "$WATCHDOG_ID" > /dev/null 2>&1
            sleep 1
            WD_STATE=$(watchdog_enabled | tr -d "[:space:]")
            if [ "$WD_STATE" = "true" ]; then
                echo "❌ FAILED to disable watchdog — manual intervention needed"
                exit 1
            fi
        fi
        echo "   ✅ Watchdog confirmed disabled"

        # Kill bot
        PID=$(get_pid)
        if [ -n "$PID" ]; then
            echo "🛑 Killing bot (PID $PID)..."
            kill "$PID"
            # Wait for process to die
            for i in 1 2 3 4 5; do
                sleep 1
                if ! kill -0 "$PID" 2>/dev/null; then
                    break
                fi
            done
            # Force kill if still alive
            if kill -0 "$PID" 2>/dev/null; then
                echo "   ⚠️  Bot didn't exit cleanly, force killing..."
                kill -9 "$PID"
                sleep 1
            fi
            # Final verify
            if kill -0 "$PID" 2>/dev/null; then
                echo "   ❌ FAILED to kill bot — manual intervention needed"
                exit 1
            fi
            echo "   ✅ Bot confirmed stopped"
            rm -f "$PIDFILE"
        else
            echo "   ℹ️  Bot wasn't running"
            rm -f "$PIDFILE"
        fi

        # Kill redeem watcher
        REDEEM_PID=$(get_redeem_pid)
        if [ -n "$REDEEM_PID" ]; then
            kill "$REDEEM_PID" 2>/dev/null
            echo "   ✅ Redeem watcher stopped"
        fi

        # Lock trading
        echo "$(date '+%Y-%m-%d %H:%M:%S') — stopped via bot.sh" > "$NO_TRADE"
        echo "⛔ NO_TRADE lock set"

        # Final verification
        echo ""
        FINAL_PID=$(get_pid)
        FINAL_WD=$(watchdog_enabled | tr -d "[:space:]")
        if [ -z "$FINAL_PID" ] && [ "$FINAL_WD" != "true" ] && [ -f "$NO_TRADE" ]; then
            echo "✅ Full shutdown confirmed: bot down, watchdog disabled, trading locked"
        else
            echo "⚠️  Shutdown incomplete:"
            [ -n "$FINAL_PID" ] && echo "   ❌ Bot still running (PID $FINAL_PID)"
            [ "$FINAL_WD" = "true" ] && echo "   ❌ Watchdog still enabled"
            [ ! -f "$NO_TRADE" ] && echo "   ❌ NO_TRADE lock not set"
            exit 1
        fi
        ;;

    unlock)
        if [ ! -f "$NO_TRADE" ]; then
            echo "✅ Already unlocked — trading is allowed"
            exit 0
        fi
        rm "$NO_TRADE"
        echo "🔓 Trading unlocked"
        ;;

    lock)
        echo "$(date '+%Y-%m-%d %H:%M:%S') — locked via bot.sh" > "$NO_TRADE"
        echo "⛔ Trading locked (bot continues monitoring)"
        ;;

    status)
        PID=$(get_pid)
        if [ -n "$PID" ]; then
            echo "🟢 Bot running (PID $PID)"
        else
            echo "🔴 Bot not running"
        fi

        WD_STATE=$(watchdog_enabled | tr -d "[:space:]")
        if [ "$WD_STATE" = "true" ]; then
            echo "🐕 Watchdog: enabled"
        else
            echo "🐕 Watchdog: disabled"
        fi

        REDEEM_PID=$(get_redeem_pid)
        if [ -n "$REDEEM_PID" ]; then
            echo "💰 Redeem watcher: running (PID $REDEEM_PID)"
        else
            echo "💰 Redeem watcher: not running"
        fi

        if [ -f "$NO_TRADE" ]; then
            echo "⛔ NO_TRADE block active"
        else
            echo "🔓 Trading allowed"
        fi
        ;;

        ack-tilt)
        # Acknowledge a losing streak — reset tilt guard watermark
        LEDGER="$BOT_DIR/ledgers/reasoning.json"
        if [ ! -f "$LEDGER" ]; then
            echo "❌ No ledger found"
            exit 1
        fi
        # Check bot is stopped
        if pgrep -f "reasoning-loop.py" > /dev/null 2>&1; then
            echo "❌ Stop the bot first (bot.sh stop)"
            exit 1
        fi
        TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")
        python3.12 -c "
import json, sys
ledger = json.load(open('$LEDGER'))
ledger['tilt_reset_after'] = '$TIMESTAMP'
json.dump(ledger, open('$LEDGER', 'w'), indent=2)
print(f'✅ Tilt watermark set to $TIMESTAMP')
print(f'   Losses before this timestamp will be ignored by tilt guard')
"
        ;;

    *)
        echo "Usage: ./bot.sh start | stop | status | unlock | lock | ack-tilt"
        exit 1
        ;;
esac
