#!/bin/bash
cd /Users/davidbaum/.openclaw/workspace-polymarket
exec python3 -u tools/trading/reasoning-loop.py 2>&1 | sed -u 's/^/[5m] /'
