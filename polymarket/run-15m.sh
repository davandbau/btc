#!/bin/bash
cd /Users/davidbaum/.openclaw/workspace-polymarket
exec python3 -u tools/trading/reasoning-loop-15m.py 2>&1 | sed -u 's/^/[15m] /'
