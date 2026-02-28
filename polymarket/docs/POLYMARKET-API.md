# Polymarket API Reference

## Source of Truth
Polymarket's CLOB and Gamma API determine all market outcomes. Never use external sources.

## Key Endpoints

### Gamma API (https://gamma-api.polymarket.com)
- `GET /events?slug={slug}` — market details, outcomes, resolution status
  - `outcomePrices: ["1", "0"]` — "1" = winning outcome
  - `outcomes: ["Up", "Down"]` — maps to outcomePrices by index
  - `closed: true` — market has resolved
  - `clobTokenIds` — token IDs for each outcome

### Data API (https://data-api.polymarket.com)  
- `GET /positions?user={wallet}` — current positions with size, currentValue, pnl
- `GET /activity?user={wallet}` — trade history with usdcSize

### CLOB API (https://clob.polymarket.com)
- Trading via py-clob-client SDK
- `create_and_post_order(OrderArgs, options=PartialCreateOrderOptions)` — no order_type param
- OrderArgs: token_id, price, size, side
- PartialCreateOrderOptions: tick_size, neg_risk

## Resolution
- Markets resolve via UMA oracle or automatically
- Check `outcomePrices` array — outcome with price "1" is the winner
- Redemption: call `redeemPositions` on CTF contract (0x4D97DCd97eC945f40cF65F87097ACe5EA0476045)
  - Requires proxy wallet signing (Magic Link users can't do this from EOA)
  - Collateral: USDC.e (0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174)
  - indexSets: [1, 2] redeems both outcomes

## Fees
- Fee rate varies by token — check `GET /fee-rate?tokenID={id}`
- Generally 1-2% of potential profit

## Our Setup
- Proxy wallet: 0x872bb6923b1a336ffff2d7a2b9179c58e26e1073
- EOA signer: 0x880C869Dd74299826292388Efc164c35B48de9aE  
- signature_type: 1 (Magic Link proxy)
- Creds: .polymarket-creds.json
