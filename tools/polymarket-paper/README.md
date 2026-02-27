# Polymarket Fast Market Paper Trader

Paper trades BTC/ETH/SOL "Up or Down" 5-min and 15-min fast markets on Polymarket using Binance CEX price momentum as the signal.

## How it works

1. Discovers active fast markets via Gamma API
2. Fetches real-time BTC price from Binance (1m klines)
3. Calculates momentum (price change over lookback window)
4. Compares momentum direction vs current Polymarket odds
5. Simulates a trade when divergence exceeds threshold
6. Tracks P&L over time in a JSON ledger

## Usage

```bash
# Single cycle (run via cron every 1-5 min)
python3 tools/polymarket-paper/trader.py

# Watch mode (continuous, runs every 60s)
python3 tools/polymarket-paper/trader.py --watch

# Show current P&L
python3 tools/polymarket-paper/trader.py --stats

# Change asset
python3 tools/polymarket-paper/trader.py --asset ETH
```

## No API keys needed

This is paper trading only — uses only public APIs (Gamma, CLOB, Binance).
