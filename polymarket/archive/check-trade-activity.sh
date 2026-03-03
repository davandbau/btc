#!/bin/bash
# Check if reasoning bot made any trades since last check
LEDGER="/Users/davidbaum/.openclaw/workspace/polymarket/ledgers/reasoning.json"
TRADES=$(python3.12 -c "import json; l=json.load(open('$LEDGER')); print(l['stats']['total_trades'])")
LOG_TAIL=$(tail -30 /Users/davidbaum/.openclaw/workspace/polymarket/logs/reasoning-loop.log | grep -E "LIVE|Action: (UP|DOWN)" | tail -3)

if [ "$TRADES" -gt 0 ]; then
  PNL=$(python3.12 -c "import json; l=json.load(open('$LEDGER')); print(f\"{l['stats']['wins']}W/{l['stats']['losses']}L PnL: \${l['stats']['total_pnl']:+.2f}\")")
  echo "TRADE_ACTIVITY: $PNL"
  echo "$LOG_TAIL"
else
  echo "NO_TRADES"
fi
