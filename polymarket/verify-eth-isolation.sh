#!/bin/bash
# ETH Bot Isolation Verification Suite
# Ensures ETH trading is completely separate from BTC
# Run this BEFORE starting the ETH bot. All tests must pass.

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
PASS=0
FAIL=0

check() {
    local desc="$1"
    local ok="$2"  # 0=pass, 1=fail
    if [ "$ok" = "0" ]; then
        echo "  ✅ $desc"
        PASS=$((PASS + 1))
    else
        echo "  ❌ $desc"
        FAIL=$((FAIL + 1))
    fi
}

echo "═══════════════════════════════════════════"
echo "  ETH Bot Isolation Verification"
echo "═══════════════════════════════════════════"
echo ""

ETH_FEED="0x000359843a543ee2fe414dc14c7e7920ef10f4372990b79d6361cdc0dd1ba782"
BTC_FEED="0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8"

# ── Test 1: No BTC references in ETH execution scripts ──
echo "▸ Test 1: No BTC references in ETH execution scripts"
COUNT=$(cat "$BOT_DIR/live-trader-eth.py" "$BOT_DIR/reasoning-trader-eth.py" | grep -ci "btc\|bitcoin\|BTCUSDT\|btc-updown" || true)
check "Zero BTC references in live-trader-eth.py + reasoning-trader-eth.py (found: $COUNT)" "$([ "$COUNT" = "0" ] && echo 0 || echo 1)"

# ── Test 2: Ledger isolation ──
echo "▸ Test 2: Ledger isolation"
COUNT=$(cat "$BOT_DIR/live-trader-eth.py" "$BOT_DIR/reasoning-trader-eth.py" | grep -c "reasoning\.json" || true)
check "ETH scripts never reference reasoning.json (found: $COUNT)" "$([ "$COUNT" = "0" ] && echo 0 || echo 1)"

COUNT=$(cat "$BOT_DIR/live-trader-eth.py" "$BOT_DIR/reasoning-trader-eth.py" | grep -c "eth\.json" || true)
check "ETH scripts reference eth.json (found: $COUNT)" "$([ "$COUNT" -ge 2 ] && echo 0 || echo 1)"

# ── Test 3: Correct Chainlink feed ID ──
echo "▸ Test 3: Chainlink oracle feed"
check "live-trader-eth.py uses ETH Chainlink feed" "$(grep -q "$ETH_FEED" "$BOT_DIR/live-trader-eth.py" && echo 0 || echo 1)"
check "live-trader-eth.py does NOT use BTC Chainlink feed" "$(grep -q "$BTC_FEED" "$BOT_DIR/live-trader-eth.py" && echo 1 || echo 0)"
check "reasoning-trader-eth.py uses ETH Chainlink feed" "$(grep -q "$ETH_FEED" "$BOT_DIR/reasoning-trader-eth.py" && echo 0 || echo 1)"
check "reasoning-trader-eth.py does NOT use BTC Chainlink feed" "$(grep -q "$BTC_FEED" "$BOT_DIR/reasoning-trader-eth.py" && echo 1 || echo 0)"

# ── Test 4: Correct market slug ──
echo "▸ Test 4: Market slug"
check "live-trader-eth.py uses eth-updown-5m slug" "$(grep -q 'eth-updown-5m' "$BOT_DIR/live-trader-eth.py" && echo 0 || echo 1)"
check "live-trader-eth.py does NOT use btc-updown-5m slug" "$(grep -q 'btc-updown-5m' "$BOT_DIR/live-trader-eth.py" && echo 1 || echo 0)"
check "reasoning-trader-eth.py uses eth-updown-5m slug" "$(grep -q 'eth-updown-5m' "$BOT_DIR/reasoning-trader-eth.py" && echo 0 || echo 1)"
check "reasoning-trader-eth.py does NOT use btc-updown-5m slug" "$(grep -q 'btc-updown-5m' "$BOT_DIR/reasoning-trader-eth.py" && echo 1 || echo 0)"

# ── Test 5: Binance symbol ──
echo "▸ Test 5: Binance symbol"
COUNT=$(grep -c "ETHUSDT" "$BOT_DIR/reasoning-trader-eth.py" || true)
check "reasoning-trader-eth.py uses ETHUSDT (found: $COUNT)" "$([ "$COUNT" -ge 3 ] && echo 0 || echo 1)"
check "reasoning-trader-eth.py does NOT use BTCUSDT" "$(grep -q 'BTCUSDT' "$BOT_DIR/reasoning-trader-eth.py" && echo 1 || echo 0)"

# ── Test 6: ETH loop wiring ──
echo "▸ Test 6: reasoning-loop-eth.py wiring"
# Must NOT reference BTC trader scripts (even in comments)
BTC_TRADER_REFS=$(grep "live-trader\.py\|reasoning-trader\.py" "$BOT_DIR/reasoning-loop-eth.py" | grep -cv "eth" || true)
check "No references to BTC trader scripts (found: $BTC_TRADER_REFS)" "$([ "$BTC_TRADER_REFS" = "0" ] && echo 0 || echo 1)"
check "References live-trader-eth.py" "$(grep -q 'live-trader-eth\.py' "$BOT_DIR/reasoning-loop-eth.py" && echo 0 || echo 1)"
check "References reasoning-trader-eth.py" "$(grep -q 'reasoning-trader-eth\.py' "$BOT_DIR/reasoning-loop-eth.py" && echo 0 || echo 1)"

# ── Test 7: Brief directory isolation ──
echo "▸ Test 7: Brief directory"
BARE=$(grep '"briefs"' "$BOT_DIR/reasoning-loop-eth.py" | grep -cv "briefs-eth" || true)
check "Uses briefs-eth/, not bare briefs/ (bare refs: $BARE)" "$([ "$BARE" = "0" ] && echo 0 || echo 1)"

# ── Test 8: Kill switch isolation ──
echo "▸ Test 8: Kill switch isolation"
check "ETH bot checks NO_TRADE_ETH" "$(grep -q 'NO_TRADE_ETH' "$BOT_DIR/reasoning-loop-eth.py" && echo 0 || echo 1)"
BARE_NT=$(grep 'NO_TRADE' "$BOT_DIR/reasoning-loop-eth.py" | grep -cv 'NO_TRADE_ETH' || true)
check "ETH bot does NOT check bare NO_TRADE (bare refs: $BARE_NT)" "$([ "$BARE_NT" = "0" ] && echo 0 || echo 1)"

# ── Test 9: LLM call log isolation ──
echo "▸ Test 9: LLM call log directory"
check "ETH bot logs to llm-calls-eth/" "$(grep -q 'llm-calls-eth' "$BOT_DIR/reasoning-loop-eth.py" && echo 0 || echo 1)"

# ── Test 10: BTC bot purity (no ETH contamination) ──
echo "▸ Test 10: BTC bot purity"
ETH_IN_BTC=$(cat "$BOT_DIR/reasoning-loop.py" "$BOT_DIR/live-trader.py" | grep -ci "eth-updown\|live-trader-eth\|reasoning-trader-eth\|briefs-eth\|NO_TRADE_ETH" || true)
check "BTC bot files have zero ETH references (found: $ETH_IN_BTC)" "$([ "$ETH_IN_BTC" = "0" ] && echo 0 || echo 1)"

# ── Test 11: ETH loop has no BTC contamination ──
echo "▸ Test 11: ETH loop asset references"
check "reasoning-loop-eth.py uses ETHUSDT" "$(grep -q 'ETHUSDT' "$BOT_DIR/reasoning-loop-eth.py" && echo 0 || echo 1)"
check "reasoning-loop-eth.py does NOT use BTCUSDT" "$(grep -q 'BTCUSDT' "$BOT_DIR/reasoning-loop-eth.py" && echo 1 || echo 0)"
check "reasoning-loop-eth.py uses ETH Chainlink feed" "$(grep -q "$ETH_FEED" "$BOT_DIR/reasoning-loop-eth.py" && echo 0 || echo 1)"
check "reasoning-loop-eth.py does NOT use BTC Chainlink feed" "$(grep -q "$BTC_FEED" "$BOT_DIR/reasoning-loop-eth.py" && echo 1 || echo 0)"

# ── Summary ──
echo ""
echo "═══════════════════════════════════════════"
TOTAL=$((PASS + FAIL))
echo "  Results: $PASS/$TOTAL passed"
if [ "$FAIL" -gt 0 ]; then
    echo "  ❌ $FAIL FAILED — DO NOT START ETH BOT"
    echo "═══════════════════════════════════════════"
    exit 1
else
    echo "  ✅ All checks passed — ETH is fully isolated"
    echo "═══════════════════════════════════════════"
    exit 0
fi
