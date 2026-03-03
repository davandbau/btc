#!/bin/bash
# Triggered by sniper bot on win — spawns a one-shot OpenClaw task to redeem via browser
# Waits 2 minutes for settlement, then checks. Retries if no Claim button yet.
openclaw cron add \
  --name "redeem-now" \
  --at "2m" \
  --session isolated \
  --delete-after-run \
  --message "Go to https://polymarket.com/portfolio in the openclaw browser (profile=openclaw, reuse the existing Polymarket tab if available). Look for a 'Claim' button for winnings. If you see it, click it and confirm the claim. If there's NO Claim button yet, wait 90 seconds, reload the page, and check again. Retry up to 8 times (total ~12 min of waiting). Report what you claimed or if nothing appeared after all retries." \
  --timeout-seconds 900 \
  --announce \
  --channel telegram \
  --to "-1003806164512:1205" \
  --description "Auto-redeem Polymarket winnings after win" \
  --json 2>&1
