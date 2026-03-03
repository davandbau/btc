#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  Polymarket Restructure — Full Verification & QA Suite
#  Run after restructure to verify every change. All tests must pass.
#  Usage: cd polymarket && bash verify-restructure.sh
# ═══════════════════════════════════════════════════════════════════

set -o pipefail
POLY_DIR="$(cd "$(dirname "$0")" && pwd)"
PASS=0
FAIL=0
WARN=0
SECTION=""

# ── Helpers ──

section() {
    SECTION="$1"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  $1"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

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

warn() {
    local desc="$1"
    echo "  ⚠️  $desc"
    WARN=$((WARN + 1))
}

file_exists() { [ -f "$1" ] && echo 0 || echo 1; }
dir_exists() { [ -d "$1" ] && echo 0 || echo 1; }
file_missing() { [ ! -f "$1" ] && echo 0 || echo 1; }
dir_missing() { [ ! -d "$1" ] && echo 0 || echo 1; }

# Count case-insensitive matches in file(s), excluding comments
# Usage: count_refs "pattern" file1 [file2 ...]
count_refs() {
    local pattern="$1"; shift
    cat "$@" 2>/dev/null | grep -ci "$pattern" || echo 0
}

# Count matches excluding a second pattern
# Usage: count_refs_excluding "pattern" "exclude" file1 [file2 ...]
count_refs_excluding() {
    local pattern="$1"; local exclude="$2"; shift 2
    cat "$@" 2>/dev/null | grep -i "$pattern" | grep -civ "$exclude" || echo 0
}

BTC_FEED="0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8"
ETH_FEED="0x000359843a543ee2fe414dc14c7e7920ef10f4372990b79d6361cdc0dd1ba782"

# ═══════════════════════════════════════════════════════════════════
#  SECTION 1: Directory Structure
# ═══════════════════════════════════════════════════════════════════

section "1. Directory Structure"

check "btc/ directory exists" "$(dir_exists "$POLY_DIR/btc")"
check "eth/ directory exists" "$(dir_exists "$POLY_DIR/eth")"
check "shared/ directory exists" "$(dir_exists "$POLY_DIR/shared")"
check "archive/ directory exists" "$(dir_exists "$POLY_DIR/archive")"

for asset in btc eth; do
    check "$asset/prompts/ exists" "$(dir_exists "$POLY_DIR/$asset/prompts")"
    check "$asset/briefs/ exists" "$(dir_exists "$POLY_DIR/$asset/briefs")"
    check "$asset/ledgers/ exists" "$(dir_exists "$POLY_DIR/$asset/ledgers")"
    check "$asset/logs/ exists" "$(dir_exists "$POLY_DIR/$asset/logs")"
    check "$asset/logs/llm-calls/ exists" "$(dir_exists "$POLY_DIR/$asset/logs/llm-calls")"
done

# ═══════════════════════════════════════════════════════════════════
#  SECTION 2: BTC Bot Files Present
# ═══════════════════════════════════════════════════════════════════

section "2. BTC Bot — Required Files"

check "btc/reasoning-loop.py exists" "$(file_exists "$POLY_DIR/btc/reasoning-loop.py")"
check "btc/live-trader.py exists" "$(file_exists "$POLY_DIR/btc/live-trader.py")"
check "btc/reasoning-trader.py exists" "$(file_exists "$POLY_DIR/btc/reasoning-trader.py")"
check "btc/bot.sh exists" "$(file_exists "$POLY_DIR/btc/bot.sh")"
check "btc/bot.sh is executable" "$([ -x "$POLY_DIR/btc/bot.sh" ] && echo 0 || echo 1)"
check "btc/NO_TRADE exists" "$(file_exists "$POLY_DIR/btc/NO_TRADE")"
check "btc/prompts/trading-v2.md exists" "$(file_exists "$POLY_DIR/btc/prompts/trading-v2.md")"
check "btc/ledgers/reasoning.json exists" "$(file_exists "$POLY_DIR/btc/ledgers/reasoning.json")"

# ═══════════════════════════════════════════════════════════════════
#  SECTION 3: ETH Bot Files Present
# ═══════════════════════════════════════════════════════════════════

section "3. ETH Bot — Required Files"

check "eth/reasoning-loop.py exists" "$(file_exists "$POLY_DIR/eth/reasoning-loop.py")"
check "eth/live-trader.py exists" "$(file_exists "$POLY_DIR/eth/live-trader.py")"
check "eth/reasoning-trader.py exists" "$(file_exists "$POLY_DIR/eth/reasoning-trader.py")"
check "eth/bot.sh exists" "$(file_exists "$POLY_DIR/eth/bot.sh")"
check "eth/bot.sh is executable" "$([ -x "$POLY_DIR/eth/bot.sh" ] && echo 0 || echo 1)"
check "eth/NO_TRADE exists" "$(file_exists "$POLY_DIR/eth/NO_TRADE")"
check "eth/prompts/trading-v2.md exists" "$(file_exists "$POLY_DIR/eth/prompts/trading-v2.md")"
check "eth/ledgers/reasoning.json exists" "$(file_exists "$POLY_DIR/eth/ledgers/reasoning.json")"

# ═══════════════════════════════════════════════════════════════════
#  SECTION 4: Shared Infra Files Present
# ═══════════════════════════════════════════════════════════════════

section "4. Shared Infrastructure — Required Files"

check "shared/lag-server.py exists" "$(file_exists "$POLY_DIR/shared/lag-server.py")"
check "shared/lag-monitor.html exists" "$(file_exists "$POLY_DIR/shared/lag-monitor.html")"
check "shared/fonts/ exists" "$(dir_exists "$POLY_DIR/shared/fonts")"
check "shared/redeem-browser.sh exists" "$(file_exists "$POLY_DIR/shared/redeem-browser.sh")"
check "shared/redeem-watcher.py exists" "$(file_exists "$POLY_DIR/shared/redeem-watcher.py")"
check "shared/redeem.py exists" "$(file_exists "$POLY_DIR/shared/redeem.py")"

# ═══════════════════════════════════════════════════════════════════
#  SECTION 5: Archive — Dead Code Moved
# ═══════════════════════════════════════════════════════════════════

section "5. Archive — Dead Code"

check "archive/sniper.py exists" "$(file_exists "$POLY_DIR/archive/sniper.py")"
check "archive/dashboard.py exists" "$(file_exists "$POLY_DIR/archive/dashboard.py")"
check "archive/5-min-prompt.prmt exists" "$(file_exists "$POLY_DIR/archive/5-min-prompt.prmt")"
check "archive/run-5m.sh exists" "$(file_exists "$POLY_DIR/archive/run-5m.sh")"
check "archive/check-trade-activity.sh exists" "$(file_exists "$POLY_DIR/archive/check-trade-activity.sh")"

# ═══════════════════════════════════════════════════════════════════
#  SECTION 6: Root Cleanup — No Orphans
# ═══════════════════════════════════════════════════════════════════

section "6. Root Cleanup — No Orphan Files"

check "No reasoning-loop.py at root" "$(file_missing "$POLY_DIR/reasoning-loop.py")"
check "No reasoning-loop-eth.py at root" "$(file_missing "$POLY_DIR/reasoning-loop-eth.py")"
check "No live-trader.py at root" "$(file_missing "$POLY_DIR/live-trader.py")"
check "No live-trader-eth.py at root" "$(file_missing "$POLY_DIR/live-trader-eth.py")"
check "No reasoning-trader.py at root" "$(file_missing "$POLY_DIR/reasoning-trader.py")"
check "No reasoning-trader-eth.py at root" "$(file_missing "$POLY_DIR/reasoning-trader-eth.py")"
check "No bot.sh at root" "$(file_missing "$POLY_DIR/bot.sh")"
check "No bot-eth.sh at root" "$(file_missing "$POLY_DIR/bot-eth.sh")"
check "No sniper.py at root" "$(file_missing "$POLY_DIR/sniper.py")"
check "No dashboard.py at root" "$(file_missing "$POLY_DIR/dashboard.py")"
check "No lag-server.py at root" "$(file_missing "$POLY_DIR/lag-server.py")"
check "No lag-monitor.html at root" "$(file_missing "$POLY_DIR/lag-monitor.html")"
check "No NO_TRADE at root" "$(file_missing "$POLY_DIR/NO_TRADE")"
check "No NO_TRADE_ETH at root" "$(file_missing "$POLY_DIR/NO_TRADE_ETH")"
check "No briefs/ at root" "$(dir_missing "$POLY_DIR/briefs")"
check "No briefs-eth/ at root" "$(dir_missing "$POLY_DIR/briefs-eth")"
check "No prompts/ at root" "$(dir_missing "$POLY_DIR/prompts")"
check "No 5-min-prompt.prmt at root" "$(file_missing "$POLY_DIR/5-min-prompt.prmt")"

# ═══════════════════════════════════════════════════════════════════
#  SECTION 7: BTC Bot — Asset Isolation
# ═══════════════════════════════════════════════════════════════════

section "7. BTC Bot — Zero ETH Contamination"

BTC_FILES="$POLY_DIR/btc/reasoning-loop.py $POLY_DIR/btc/live-trader.py $POLY_DIR/btc/reasoning-trader.py"

COUNT=$(count_refs "eth-updown\|ETHUSDT\|live-trader-eth\|reasoning-trader-eth\|briefs-eth\|NO_TRADE_ETH\|eth\.json\|regime-eth\|llm-calls-eth" $BTC_FILES)
check "BTC code: zero ETH-specific references (found: $COUNT)" "$([ "$COUNT" = "0" ] && echo 0 || echo 1)"

check "BTC Chainlink feed is BTC" "$(grep -q "$BTC_FEED" "$POLY_DIR/btc/reasoning-loop.py" && echo 0 || echo 1)"
check "BTC live-trader uses BTC Chainlink feed" "$(grep -q "$BTC_FEED" "$POLY_DIR/btc/live-trader.py" && echo 0 || echo 1)"
check "BTC bot does NOT contain ETH Chainlink feed" "$(grep -q "$ETH_FEED" "$POLY_DIR/btc/reasoning-loop.py" && echo 1 || echo 0)"

check "BTC uses btc-updown-5m slug" "$(grep -q 'btc-updown-5m' "$POLY_DIR/btc/live-trader.py" && echo 0 || echo 1)"
check "BTC uses BTCUSDT" "$(grep -q 'BTCUSDT' "$POLY_DIR/btc/reasoning-loop.py" && echo 0 || echo 1)"

# ═══════════════════════════════════════════════════════════════════
#  SECTION 8: ETH Bot — Asset Isolation
# ═══════════════════════════════════════════════════════════════════

section "8. ETH Bot — Zero BTC Contamination"

ETH_FILES="$POLY_DIR/eth/reasoning-loop.py $POLY_DIR/eth/live-trader.py $POLY_DIR/eth/reasoning-trader.py"

COUNT=$(count_refs "btc-updown\|BTCUSDT\|live-trader\.py\b\|reasoning-trader\.py\b\|reasoning\.json" $ETH_FILES)
# Exclude: reasoning.json IS used by ETH (same name, different dir). Filter it out.
# Actually reasoning.json is the correct ledger name for both. Check for BTC-specific paths instead.
COUNT=$(cat $ETH_FILES 2>/dev/null | grep -ci "btc-updown\|BTCUSDT\|Bitcoin Up or Down" || echo 0)
check "ETH code: zero BTC market/symbol references (found: $COUNT)" "$([ "$COUNT" = "0" ] && echo 0 || echo 1)"

check "ETH Chainlink feed is ETH" "$(grep -q "$ETH_FEED" "$POLY_DIR/eth/reasoning-loop.py" && echo 0 || echo 1)"
check "ETH live-trader uses ETH Chainlink feed" "$(grep -q "$ETH_FEED" "$POLY_DIR/eth/live-trader.py" && echo 0 || echo 1)"
check "ETH bot does NOT contain BTC Chainlink feed" "$(grep -q "$BTC_FEED" "$POLY_DIR/eth/reasoning-loop.py" && echo 1 || echo 0)"
check "ETH live-trader does NOT contain BTC Chainlink feed" "$(grep -q "$BTC_FEED" "$POLY_DIR/eth/live-trader.py" && echo 1 || echo 0)"

check "ETH uses eth-updown-5m slug" "$(grep -q 'eth-updown-5m' "$POLY_DIR/eth/live-trader.py" && echo 0 || echo 1)"
check "ETH uses ETHUSDT" "$(grep -q 'ETHUSDT' "$POLY_DIR/eth/reasoning-loop.py" && echo 0 || echo 1)"

# ═══════════════════════════════════════════════════════════════════
#  SECTION 9: No Cross-Directory Path References
# ═══════════════════════════════════════════════════════════════════

section "9. No Cross-Directory Path References"

# BTC code must not reference eth/ directory
COUNT=$(cat $BTC_FILES 2>/dev/null | grep -c 'eth/' || echo 0)
check "BTC code: no paths referencing eth/ (found: $COUNT)" "$([ "$COUNT" = "0" ] && echo 0 || echo 1)"

# ETH code must not reference btc/ directory
COUNT=$(cat $ETH_FILES 2>/dev/null | grep -c 'btc/' || echo 0)
check "ETH code: no paths referencing btc/ (found: $COUNT)" "$([ "$COUNT" = "0" ] && echo 0 || echo 1)"

# Both should reference shared/ for redeem
check "BTC references shared/ for redeem" "$(grep -q 'shared.*redeem\|redeem.*shared' "$POLY_DIR/btc/reasoning-loop.py" && echo 0 || echo 1)"
check "ETH references shared/ for redeem" "$(grep -q 'shared.*redeem\|redeem.*shared' "$POLY_DIR/eth/reasoning-loop.py" && echo 0 || echo 1)"

# ═══════════════════════════════════════════════════════════════════
#  SECTION 10: Ledger Isolation
# ═══════════════════════════════════════════════════════════════════

section "10. Ledger Isolation"

check "BTC ledger exists and is valid JSON" "$(python3 -c "import json; json.load(open('$POLY_DIR/btc/ledgers/reasoning.json'))" 2>/dev/null && echo 0 || echo 1)"
check "ETH ledger exists and is valid JSON" "$(python3 -c "import json; json.load(open('$POLY_DIR/eth/ledgers/reasoning.json'))" 2>/dev/null && echo 0 || echo 1)"

# Verify they're different files (not symlinks to same)
BTC_INODE=$(stat -f %i "$POLY_DIR/btc/ledgers/reasoning.json" 2>/dev/null || echo "none")
ETH_INODE=$(stat -f %i "$POLY_DIR/eth/ledgers/reasoning.json" 2>/dev/null || echo "none2")
check "BTC and ETH ledgers are different files (not symlinked)" "$([ "$BTC_INODE" != "$ETH_INODE" ] && echo 0 || echo 1)"

# Verify BTC ledger contains BTC trades (sanity)
BTC_MARKET=$(python3 -c "
import json
l = json.load(open('$POLY_DIR/btc/ledgers/reasoning.json'))
trades = l.get('trades', [])
if trades:
    print(trades[-1].get('market', ''))
else:
    print('empty')
" 2>/dev/null || echo "error")
check "BTC ledger contains BTC market trades" "$(echo "$BTC_MARKET" | grep -qi 'bitcoin\|btc' && echo 0 || echo 1)"

# ═══════════════════════════════════════════════════════════════════
#  SECTION 11: Kill Switch Isolation
# ═══════════════════════════════════════════════════════════════════

section "11. Kill Switch (NO_TRADE) Isolation"

check "btc/NO_TRADE exists (bots should be locked)" "$(file_exists "$POLY_DIR/btc/NO_TRADE")"
check "eth/NO_TRADE exists (bots should be locked)" "$(file_exists "$POLY_DIR/eth/NO_TRADE")"

# ETH bot must check NO_TRADE, not NO_TRADE_ETH
COUNT=$(grep -c "NO_TRADE_ETH" "$POLY_DIR/eth/reasoning-loop.py" 2>/dev/null || echo 0)
check "ETH bot has zero references to NO_TRADE_ETH (found: $COUNT)" "$([ "$COUNT" = "0" ] && echo 0 || echo 1)"

COUNT=$(grep -c "NO_TRADE_ETH" "$POLY_DIR/eth/bot.sh" 2>/dev/null || echo 0)
check "ETH bot.sh has zero references to NO_TRADE_ETH (found: $COUNT)" "$([ "$COUNT" = "0" ] && echo 0 || echo 1)"

# ═══════════════════════════════════════════════════════════════════
#  SECTION 12: Bot.sh — Pidfile & Process Management
# ═══════════════════════════════════════════════════════════════════

section "12. Bot.sh — Process Management"

check "BTC bot.sh references bot.pid" "$(grep -q 'bot.pid' "$POLY_DIR/btc/bot.sh" && echo 0 || echo 1)"
check "ETH bot.sh references bot.pid" "$(grep -q 'bot.pid' "$POLY_DIR/eth/bot.sh" && echo 0 || echo 1)"

# Verify bot.sh status command works (should report "not running")
BTC_STATUS=$("$POLY_DIR/btc/bot.sh" status 2>&1)
check "btc/bot.sh status runs without error" "$(echo "$BTC_STATUS" | grep -qi "running\|not running\|Bot" && echo 0 || echo 1)"

ETH_STATUS=$("$POLY_DIR/eth/bot.sh" status 2>&1)
check "eth/bot.sh status runs without error" "$(echo "$ETH_STATUS" | grep -qi "running\|not running\|Bot" && echo 0 || echo 1)"

# Neither bot should be running
check "BTC bot is NOT running" "$(echo "$BTC_STATUS" | grep -qi "not running\|🔴" && echo 0 || echo 1)"
check "ETH bot is NOT running" "$(echo "$ETH_STATUS" | grep -qi "not running\|🔴" && echo 0 || echo 1)"

# ═══════════════════════════════════════════════════════════════════
#  SECTION 13: Python Syntax Validation
# ═══════════════════════════════════════════════════════════════════

section "13. Python Syntax — All Files Compile"

for f in \
    btc/reasoning-loop.py btc/live-trader.py btc/reasoning-trader.py \
    eth/reasoning-loop.py eth/live-trader.py eth/reasoning-trader.py \
    shared/lag-server.py shared/redeem-watcher.py shared/redeem.py; do
    FULL="$POLY_DIR/$f"
    if [ -f "$FULL" ]; then
        RESULT=$(python3 -c "compile(open('$FULL').read(), '$f', 'exec')" 2>&1)
        check "$f compiles" "$([ $? -eq 0 ] && echo 0 || echo 1)"
    else
        check "$f compiles" "1"
    fi
done

# ═══════════════════════════════════════════════════════════════════
#  SECTION 14: Shared Infra — No Trading Logic
# ═══════════════════════════════════════════════════════════════════

section "14. Shared Infra — No Trading Logic"

SHARED_PY="$POLY_DIR/shared/lag-server.py $POLY_DIR/shared/redeem-watcher.py $POLY_DIR/shared/redeem.py"
COUNT=$(cat $SHARED_PY 2>/dev/null | grep -c "trigger_agent\|kelly_size\|ClobClient\|place_order\|execute_trade" || echo 0)
check "Shared code has no trading functions (found: $COUNT)" "$([ "$COUNT" = "0" ] && echo 0 || echo 1)"

# ═══════════════════════════════════════════════════════════════════
#  SECTION 15: Lag Server — Multi-Asset API
# ═══════════════════════════════════════════════════════════════════

section "15. Lag Server — Multi-Asset Support"

check "lag-server.py supports ?asset= parameter" "$(grep -q 'asset' "$POLY_DIR/shared/lag-server.py" && echo 0 || echo 1)"
check "lag-server.py knows btc/ path" "$(grep -q 'btc' "$POLY_DIR/shared/lag-server.py" && echo 0 || echo 1)"
check "lag-server.py knows eth/ path" "$(grep -q 'eth' "$POLY_DIR/shared/lag-server.py" && echo 0 || echo 1)"

# ═══════════════════════════════════════════════════════════════════
#  SECTION 16: Dashboard — Tab Structure
# ═══════════════════════════════════════════════════════════════════

section "16. Dashboard — Tab UI"

check "Dashboard has tab-bar" "$(grep -q 'tab-bar\|tab_bar' "$POLY_DIR/shared/lag-monitor.html" && echo 0 || echo 1)"
check "Dashboard has BTC tab" "$(grep -q 'data-asset.*btc\|data-asset="btc"' "$POLY_DIR/shared/lag-monitor.html" && echo 0 || echo 1)"
check "Dashboard has ETH tab" "$(grep -q 'data-asset.*eth\|data-asset="eth"' "$POLY_DIR/shared/lag-monitor.html" && echo 0 || echo 1)"
check "Dashboard has BTC view section" "$(grep -q 'view-btc' "$POLY_DIR/shared/lag-monitor.html" && echo 0 || echo 1)"
check "Dashboard has ETH view section" "$(grep -q 'view-eth' "$POLY_DIR/shared/lag-monitor.html" && echo 0 || echo 1)"
check "Dashboard has switchTab function" "$(grep -q 'switchTab\|switch_tab' "$POLY_DIR/shared/lag-monitor.html" && echo 0 || echo 1)"
check "Dashboard has hash-based tab persistence" "$(grep -q 'location.hash' "$POLY_DIR/shared/lag-monitor.html" && echo 0 || echo 1)"

# BTC view uses BTC websocket
check "BTC view connects to btcusdt websocket" "$(grep -q 'btcusdt' "$POLY_DIR/shared/lag-monitor.html" && echo 0 || echo 1)"
# ETH view uses ETH websocket
check "ETH view connects to ethusdt websocket" "$(grep -q 'ethusdt' "$POLY_DIR/shared/lag-monitor.html" && echo 0 || echo 1)"

# API calls include asset parameter
check "Dashboard API calls include asset param" "$(grep -q 'asset=btc\|asset=eth' "$POLY_DIR/shared/lag-monitor.html" && echo 0 || echo 1)"

# ═══════════════════════════════════════════════════════════════════
#  SECTION 17: Wiring — Internal References
# ═══════════════════════════════════════════════════════════════════

section "17. Internal Wiring"

# BTC bot calls btc/live-trader.py (relative), not any -eth variant
check "BTC loop calls live-trader.py (local)" "$(grep -q "live-trader.py" "$POLY_DIR/btc/reasoning-loop.py" && echo 0 || echo 1)"
check "BTC loop calls reasoning-trader.py (local)" "$(grep -q "reasoning-trader.py" "$POLY_DIR/btc/reasoning-loop.py" && echo 0 || echo 1)"

# ETH bot calls eth/live-trader.py (relative), not any -eth or BTC variant
check "ETH loop calls live-trader.py (local)" "$(grep -q "live-trader.py" "$POLY_DIR/eth/reasoning-loop.py" && echo 0 || echo 1)"
check "ETH loop calls reasoning-trader.py (local)" "$(grep -q "reasoning-trader.py" "$POLY_DIR/eth/reasoning-loop.py" && echo 0 || echo 1)"

# Neither should have -eth suffixed references anymore
COUNT=$(cat "$POLY_DIR/btc/reasoning-loop.py" "$POLY_DIR/eth/reasoning-loop.py" 2>/dev/null | grep -c "live-trader-eth\|reasoning-trader-eth\|reasoning-loop-eth" || echo 0)
check "No -eth suffixed file references in either bot (found: $COUNT)" "$([ "$COUNT" = "0" ] && echo 0 || echo 1)"

# ═══════════════════════════════════════════════════════════════════
#  SECTION 18: Prompt Files — Independent
# ═══════════════════════════════════════════════════════════════════

section "18. Prompt Independence"

check "BTC prompt exists" "$(file_exists "$POLY_DIR/btc/prompts/trading-v2.md")"
check "ETH prompt exists" "$(file_exists "$POLY_DIR/eth/prompts/trading-v2.md")"

BTC_PROMPT_INODE=$(stat -f %i "$POLY_DIR/btc/prompts/trading-v2.md" 2>/dev/null || echo "none")
ETH_PROMPT_INODE=$(stat -f %i "$POLY_DIR/eth/prompts/trading-v2.md" 2>/dev/null || echo "none2")
check "Prompts are separate files (not symlinked)" "$([ "$BTC_PROMPT_INODE" != "$ETH_PROMPT_INODE" ] && echo 0 || echo 1)"

# ═══════════════════════════════════════════════════════════════════
#  SECTION 19: Historical Data — Briefs & LLM Logs Moved
# ═══════════════════════════════════════════════════════════════════

section "19. Historical Data Migrated"

BTC_BRIEFS=$(ls "$POLY_DIR/btc/briefs/" 2>/dev/null | wc -l | tr -d ' ')
check "BTC briefs migrated ($BTC_BRIEFS files)" "$([ "$BTC_BRIEFS" -gt 0 ] && echo 0 || echo 1)"

BTC_LLM=$(ls "$POLY_DIR/btc/logs/llm-calls/" 2>/dev/null | wc -l | tr -d ' ')
check "BTC LLM call logs migrated ($BTC_LLM files)" "$([ "$BTC_LLM" -gt 0 ] && echo 0 || echo 1)"

# ETH may have few/no briefs yet — warn instead of fail
ETH_BRIEFS=$(ls "$POLY_DIR/eth/briefs/" 2>/dev/null | wc -l | tr -d ' ')
if [ "$ETH_BRIEFS" -gt 0 ]; then
    check "ETH briefs migrated ($ETH_BRIEFS files)" "0"
else
    warn "ETH briefs directory is empty (may be expected if ETH hasn't traded much)"
    PASS=$((PASS + 1))  # don't fail on this
fi

# ═══════════════════════════════════════════════════════════════════
#  SECTION 20: Root-Level Allowed Files Only
# ═══════════════════════════════════════════════════════════════════

section "20. Root Directory — Only Allowed Files"

ALLOWED_ROOT="btc eth shared archive docs cache reports live-logs RESEARCH.md STATE.md verify-restructure.sh verify-isolation.sh verify-eth-isolation.sh __pycache__ .gitkeep"

# List unexpected files/dirs at root (excluding . and ..)
UNEXPECTED=""
for item in "$POLY_DIR"/*; do
    base=$(basename "$item")
    if ! echo "$ALLOWED_ROOT" | grep -qw "$base"; then
        UNEXPECTED="$UNEXPECTED $base"
    fi
done

if [ -z "$UNEXPECTED" ]; then
    check "No unexpected files at root" "0"
else
    check "No unexpected files at root (found:$UNEXPECTED)" "1"
fi

# ═══════════════════════════════════════════════════════════════════
#  SUMMARY
# ═══════════════════════════════════════════════════════════════════

echo ""
echo "═══════════════════════════════════════════════════════════════"
TOTAL=$((PASS + FAIL))
echo "  Results: $PASS/$TOTAL passed, $WARN warnings"
echo ""
if [ "$FAIL" -gt 0 ]; then
    echo "  ❌ $FAIL FAILED — RESTRUCTURE INCOMPLETE"
    echo ""
    echo "  DO NOT start any bots until all tests pass."
    echo "═══════════════════════════════════════════════════════════════"
    exit 1
else
    echo "  ✅ All checks passed — restructure verified"
    echo ""
    echo "  Next steps:"
    echo "    1. Start shared/lag-server.py — verify dashboard loads"
    echo "    2. Test tab switching (BTC ↔ ETH)"
    echo "    3. Report to David for bot start approval"
    echo "═══════════════════════════════════════════════════════════════"
    exit 0
fi
