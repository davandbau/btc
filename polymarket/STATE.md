# Polymarket Trading State
_Auto-updated. Read this at session start._

## Active Bots
- **Reasoning bot** (`reasoning-loop.py --live`): RUNNING
  - Log: `logs/reasoning-loop.log`
  - Ledger: `ledgers/reasoning.json`
  - Config: $100 max/window, Kelly sizing, min edge 7%, min conviction 70%
  - Regime filter: choppy markets require 80%+ conviction
  - Resolves via `live-trader.py --resolve` every 15s
  - Auto-redeems via `redeem-browser.sh` on WIN
  
- **Sniper bot** (`sniper.py --live`): PAUSED (stopped by David)
  - Has half-Kelly dynamic sizing, ask collapse guard, momentum fade guard
  - Delta floor $20
  - Don't restart without David's explicit approval

## Cron Jobs
- `reasoning-watchdog` (every 5m): auto-restarts reasoning bot if dead
- `polymarket-sweep` (every 30m): browser sweep for unclaimed winnings
- `polymarket-daily-summary` (23:00 Oslo): daily P&L report

## Infrastructure
- Lag server: `lag-server.py` on port 8851, serves `lag-monitor.html`
- Futures shadow: `futures-shadow.py`, writes `logs/futures-live.json` every 2s
- Dashboard URL: `http://192.168.86.53:8851`
- Browser profile: `openclaw` (Polymarket logged in)

## Account
- Wallet: `0x872bb6923b1a336ffff2d7a2b9179c58e26e1073`
- Telegram alerts: `-1003806164512:1205` (trading topic)
- Started ~$457 on 2026-03-01 morning → ended ~$219
- **Ledger reset 20:40 UTC**: archived old ledgers to `ledgers/archive/`, fresh start at $219
- Sniper STARTING_CAPITAL updated to $219
- GitHub: `davandbau/btc`

## Key Decisions (2026-03-01)
- Sniper paused: 79% WR insufficient for asymmetric payoff (wins ~5-10%, losses ~100%)
- Reasoning bot kept: 62% WR but 2.4x profit factor (big wins, small losses)
- Added regime filter: Hurst mean-reverting → require 80% conviction
- Min edge raised 5% → 7%, min conviction floor 70%
- Half-Kelly dynamic sizing on sniper (if restarted)
- Ask collapse guard (>20% drop between DCA entries)
- Momentum fade guard (>25% delta shrink from first entry)

## ⚠️ HOW TO STOP A BOT (READ THIS BEFORE KILLING ANYTHING)
1. **Disable the watchdog cron FIRST**: `openclaw cron list` → find the watchdog → `openclaw cron remove <id>`
2. **Create kill switch**: `touch ~/POLY_KILL` (sniper checks this)
3. **THEN kill the process**: `pkill -f "bot-name.py"`
4. **Verify it stays dead**: wait 60s, check `ps aux | grep bot-name`
5. **Update STATE.md**: mark bot as PAUSED

**NEVER kill a process without disabling its watchdog first. The watchdog WILL restart it.**

## How to Check Status
```bash
# Bot alive?
ps aux | grep reasoning-loop.py | grep -v grep

# Recent trades
tail -30 logs/reasoning-loop.log

# Track record
python3.12 -c "import json; l=json.load(open('ledgers/reasoning.json')); print(f'{l[\"stats\"][\"wins\"]}W/{l[\"stats\"][\"losses\"]}L PnL: \${l[\"stats\"][\"total_pnl\"]:+.2f}')"

# Account balance (browser)
# Go to https://polymarket.com/portfolio in openclaw browser
```

## Known Issues
- Kill switch `~/POLY_KILL` affects BOTH sniper and reasoning bot (shared via live-trader.py) — need bot-specific switches
- Reasoning ledger costs show $0.00 — PnL tracking doesn't capture wagered amounts properly
- Kill switch was REMOVED at 20:30 Oslo — reasoning bot is free to trade

## Last Updated
2026-03-01 20:33 Oslo
