#!/bin/bash
# Multi-asset isolation and integrity verification — macOS compatible
PASS=0; FAIL=0; WARN=0
ok()   { echo "  ✅ $1"; ((PASS++)); }
fail() { echo "  ❌ $1"; ((FAIL++)); }

echo "═══════════════════════════════════════════════════════"
echo "  Multi-Asset Isolation & Integrity Test Suite"
echo "═══════════════════════════════════════════════════════"

# 1. Directory structure
echo ""; echo "━━━ 1. Directories & Files ━━━"
for a in btc eth sol xrp; do
  for d in "" /prompts /briefs /ledgers /logs; do [ -d "$a$d" ] && ok "$a$d/" || fail "$a$d/ missing"; done
  for f in reasoning-loop.py live-trader.py reasoning-trader.py bot.sh NO_TRADE prompts/trading-v2.md ledgers/reasoning.json; do
    [ -f "$a/$f" ] && ok "$a/$f" || fail "$a/$f missing"
  done
done

# 2. Python compilation
echo ""; echo "━━━ 2. Compilation ━━━"
for a in btc eth sol xrp; do
  for f in reasoning-loop.py live-trader.py reasoning-trader.py; do
    python3.12 -c "import py_compile; py_compile.compile('$a/$f', doraise=True)" 2>/dev/null && ok "$a/$f" || fail "$a/$f"
  done
done
python3.12 -c "import py_compile; py_compile.compile('shared/lag-server.py', doraise=True)" 2>/dev/null && ok "shared/lag-server.py" || fail "shared/lag-server.py"

# 3. Feed ID correctness
echo ""; echo "━━━ 3. Correct Feed IDs ━━━"
check_has() { grep -q "$2" "$1" 2>/dev/null && ok "$1 has $3 feed" || fail "$1 MISSING $3 feed"; }
check_has btc/reasoning-loop.py "0x00039d9e" BTC; check_has btc/live-trader.py "0x00039d9e" BTC; check_has btc/reasoning-trader.py "0x00039d9e" BTC
check_has eth/reasoning-loop.py "0x000359" ETH; check_has eth/live-trader.py "0x000359" ETH; check_has eth/reasoning-trader.py "0x000359" ETH
check_has sol/reasoning-loop.py "0x0003b778" SOL; check_has sol/live-trader.py "0x0003b778" SOL; check_has sol/reasoning-trader.py "0x0003b778" SOL
check_has xrp/reasoning-loop.py "0x0003c16c" XRP; check_has xrp/live-trader.py "0x0003c16c" XRP; check_has xrp/reasoning-trader.py "0x0003c16c" XRP

# 4. Cross-contamination — wrong feeds
echo ""; echo "━━━ 4. No Wrong Feed IDs ━━━"
no_has() { grep -q "$2" "$1" 2>/dev/null && fail "$1 CONTAINS $3 feed!" || ok "$1 clean of $3"; }
for f in btc/reasoning-loop.py btc/live-trader.py btc/reasoning-trader.py; do no_has "$f" "0x000359" ETH; no_has "$f" "0x0003b778" SOL; no_has "$f" "0x0003c16c" XRP; done
for f in eth/reasoning-loop.py eth/live-trader.py eth/reasoning-trader.py; do no_has "$f" "0x00039d9e" BTC; no_has "$f" "0x0003b778" SOL; no_has "$f" "0x0003c16c" XRP; done
for f in sol/reasoning-loop.py sol/live-trader.py sol/reasoning-trader.py; do no_has "$f" "0x00039d9e" BTC; no_has "$f" "0x000359" ETH; no_has "$f" "0x0003c16c" XRP; done
for f in xrp/reasoning-loop.py xrp/live-trader.py xrp/reasoning-trader.py; do no_has "$f" "0x00039d9e" BTC; no_has "$f" "0x000359" ETH; no_has "$f" "0x0003b778" SOL; done

# 5. Binance symbol isolation
echo ""; echo "━━━ 5. Binance Symbols ━━━"
for a_sym in "btc BTCUSDT ETHUSDT,SOLUSDT,XRPUSDT" "eth ETHUSDT BTCUSDT,SOLUSDT,XRPUSDT" "sol SOLUSDT BTCUSDT,ETHUSDT,XRPUSDT" "xrp XRPUSDT BTCUSDT,ETHUSDT,SOLUSDT"; do
  a=$(echo $a_sym | cut -d' ' -f1); correct=$(echo $a_sym | cut -d' ' -f2); wrongs=$(echo $a_sym | cut -d' ' -f3)
  grep -q "$correct" "$a/reasoning-loop.py" && ok "$a uses $correct" || fail "$a MISSING $correct"
  for w in $(echo $wrongs | tr ',' ' '); do
    grep -q "$w" "$a/reasoning-loop.py" && fail "$a CONTAINS $w!" || ok "$a clean of $w"
  done
done

# 6. PM slug isolation
echo ""; echo "━━━ 6. Polymarket Slugs ━━━"
for a_slug in "btc btc-updown-5m eth-updown-5m,sol-updown-5m,xrp-updown-5m" "eth eth-updown-5m btc-updown-5m,sol-updown-5m,xrp-updown-5m" "sol sol-updown-5m btc-updown-5m,eth-updown-5m,xrp-updown-5m" "xrp xrp-updown-5m btc-updown-5m,eth-updown-5m,sol-updown-5m"; do
  a=$(echo $a_slug | cut -d' ' -f1); correct=$(echo $a_slug | cut -d' ' -f2); wrongs=$(echo $a_slug | cut -d' ' -f3)
  for f in reasoning-loop.py live-trader.py reasoning-trader.py; do
    grep -q "$correct" "$a/$f" && ok "$a/$f uses $correct" || fail "$a/$f MISSING $correct"
    for w in $(echo $wrongs | tr ',' ' '); do
      grep -q "$w" "$a/$f" && fail "$a/$f CONTAINS $w!" || ok "$a/$f clean of $w"
    done
  done
done

# 7. NO_TRADE gates
echo ""; echo "━━━ 7. NO_TRADE Gates ━━━"
for a in btc eth sol xrp; do
  grep -q "observe mode" "$a/reasoning-loop.py" && ok "$a Gate 1: observe mode" || fail "$a MISSING observe mode"
  grep -q "NO_TRADE.*BLOCKED\|NO_TRADE.*would" "$a/reasoning-loop.py" && ok "$a Gate 1: trade block" || fail "$a MISSING trade block"
  grep -q "refusing to place order" "$a/live-trader.py" && ok "$a Gate 2: live-trader" || fail "$a MISSING Gate 2 live-trader"
  grep -q "refusing to place order" "$a/reasoning-trader.py" && ok "$a Gate 2: reasoning-trader" || fail "$a MISSING Gate 2 reasoning-trader"
done

# 8. Phase offsets
echo ""; echo "━━━ 8. Phase Offsets ━━━"
grep -q "PHASE_OFFSET" btc/reasoning-loop.py && fail "BTC has unexpected offset" || ok "BTC: no offset (first)"
for a_off in "eth 6" "sol 13" "xrp 17"; do
  a=$(echo $a_off | cut -d' ' -f1); expected=$(echo $a_off | cut -d' ' -f2)
  actual=$(grep "PHASE_OFFSET = " "$a/reasoning-loop.py" | head -1 | grep -oE '[0-9]+')
  [ "$actual" = "$expected" ] && ok "$a offset: ${actual}s" || fail "$a offset: got $actual, expected $expected"
done

# 9. Delta floors
echo ""; echo "━━━ 9. Delta Floors ━━━"
for a in btc eth sol xrp; do
  floor=$(grep "abs(delta) < " "$a/reasoning-loop.py" | head -1 | grep -oE '[0-9]+\.?[0-9]*')
  [ -n "$floor" ] && ok "$a floor: \$$floor" || fail "$a missing delta floor"
done

# 10. Ledger paths — all use reasoning.json via BOT_DIR/SCRIPT_DIR
echo ""; echo "━━━ 10. Ledger Consistency ━━━"
for a in btc eth sol xrp; do
  for f in reasoning-loop.py live-trader.py reasoning-trader.py; do
    grep -q "reasoning.json" "$a/$f" && ok "$a/$f → reasoning.json" || fail "$a/$f wrong ledger"
  done
done

# 11. Dashboard & lag-server
echo ""; echo "━━━ 11. Shared Infrastructure ━━━"
for a in btc eth sol xrp; do
  grep -q "\"$a\":" shared/lag-server.py && ok "lag-server has $a" || fail "lag-server MISSING $a"
  grep -q "data-asset=\"$a\"" shared/lag-monitor.html && ok "dashboard tab: $a" || fail "dashboard MISSING tab: $a"
  grep -q "  $a:" shared/lag-monitor.html && ok "dashboard config: $a" || fail "dashboard MISSING config: $a"
done

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] && echo "  ✅ All checks passed" || echo "  ❌ $FAIL FAILURES — fix before proceeding"
echo "═══════════════════════════════════════════════════════"
