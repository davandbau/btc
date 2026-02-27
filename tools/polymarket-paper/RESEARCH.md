# Alternative Short-Term Automated Trading Strategies

*Research compiled: 2026-02-24*
*Context: We have a working Polymarket paper trader for BTC 5-min fast markets. What else can we do with the same tech stack?*

---

## Table of Contents
1. [What the Sharp Traders Are Actually Doing](#whats-working-now)
2. [Strategy 1: Cross-Platform Prediction Market Arbitrage](#strategy-1)
3. [Strategy 2: Polymarket Market Making on Long-Tail Events](#strategy-2)
4. [Strategy 3: LLM-Powered News → Prediction Market Trading](#strategy-3)
5. [Strategy 4: Crypto Funding Rate Arbitrage](#strategy-4)
6. [Strategy 5: Cross-Exchange Crypto Spot Arbitrage](#strategy-5)
7. [Strategy 6: Polymarket Multi-Outcome Mispricing (Dutching)](#strategy-6)
8. [Strategy 7: Sports/Event Prediction Market Arbitrage](#strategy-7)
9. [Comparison Matrix](#comparison-matrix)
10. [Honest Assessment: What's Realistic for Us](#honest-assessment)

---

## What the Sharp Traders Are Actually Doing <a name="whats-working-now"></a>

### The $313 → $414K Bot (Jan 2026)
The most documented success story: a bot trading **exclusively BTC/ETH/SOL 15-minute up/down markets** on Polymarket. It placed $4,000-$5,000 bets with a **98% win rate**. The strategy was pure **latency arbitrage** — monitoring Binance/Coinbase spot prices and buying when the outcome was near-certain but Polymarket prices hadn't fully adjusted.

**Key insight**: This worked under zero-fee conditions. Polymarket has since introduced **dynamic taker fees** (up to 3.15% at 50/50 odds, declining toward extremes) specifically to kill this strategy. The golden era of pure latency arb on fast markets is largely over.

### Polymarket's Dynamic Fee Response (Jan 2026)
- Taker fees now apply to 15-min crypto markets (and expanding)
- Fees **scale inversely with certainty**: peak 3.15% at 50/50, declining as probability approaches 0% or 100%
- Maker rebates program redistributes taker fees to liquidity providers
- This makes the "wait until near-certain, then slam the book" strategy much less profitable
- **5-min markets may still have lower fees or different structures** — worth checking

### What Still Works (per Polymarket leaderboards & forums)
1. **Market making** with tight spreads on volatile markets (~consistent small gains)
2. **Cross-platform arb** between Polymarket and Kalshi (structural price differences persist)
3. **LLM/information edge** on political/event markets (slower-moving, less bot competition)
4. **Multi-outcome mispricing** where YES probabilities across outcomes don't sum to 100%
5. **Temporal arbitrage on new market creation** (markets often misprice at launch)

### Industry Numbers
- April 2024 – April 2025: ~$40 million extracted by arbitrage bots on Polymarket
- Betmoar (automated tool) drove ~$110M in cumulative volume (~5% of Polymarket monthly)
- Average arb opportunity duration: **2.7 seconds** (down from 12.3s in 2024)
- 73% of arbitrage profits captured by sub-100ms bots
- Median arb spread: 0.3% (barely profitable after fees)

---

## Strategy 1: Cross-Platform Prediction Market Arbitrage <a name="strategy-1"></a>

**One-liner**: Buy YES on Polymarket + NO on Kalshi (or vice versa) when the same event is priced differently.

### Platform(s) & Market Type
- **Polymarket** (crypto, CLOB API, no KYC for non-US)
- **Kalshi** (CFTC-regulated, US-only, REST API)
- Market types: BTC hourly/daily price, Fed rate decisions, elections, macro events

### The Edge
Structural price differences persist because:
- Different user bases (crypto-native vs. TradFi)
- Different fee structures
- Different liquidity profiles
- Settlement timing differences
- Information propagation delays between platforms

Open-source bots exist: [polymarket-kalshi-btc-arbitrage-bot](https://github.com/CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot), [polymarket-arbitrage](https://github.com/ImMike/polymarket-arbitrage)

### Technical Implementation
```
Stack: Python + Polymarket CLOB API + Kalshi REST API
1. Poll both APIs for matching markets (same event, same expiry)
2. Compare implied probabilities: if YES_poly + NO_kalshi < $1.00 → arb exists
3. Calculate edge after fees on both platforms
4. Execute simultaneously on both platforms
5. Wait for settlement, collect guaranteed profit
```

**APIs**:
- Polymarket: `https://clob.polymarket.com` (REST + WebSocket)
- Kalshi: `https://trading-api.kalshi.com/trade-api/v2` (REST, requires API key)
- Match markets by event description + expiry (fuzzy matching needed)

### Fee Structure & Breakeven
- **Polymarket**: Dynamic taker fees 0-3.15% on fast markets; 0% on most longer-dated markets
- **Kalshi**: ~2-7¢ per contract (varies), no fee on settlement
- **Breakeven**: Need >3-5% spread on fast markets, >1-2% on longer-dated markets
- Longer-dated event markets (elections, Fed decisions) often have wider spreads

### Risk Assessment
- ⚠️ **Execution risk**: Can't guarantee simultaneous fills on both platforms
- ⚠️ **Settlement risk**: Different resolution criteria for "same" event
- ⚠️ **Kalshi access**: US-only, requires KYC, may limit/ban perceived arb accounts
- ⚠️ **Capital lockup**: Funds locked until event resolves (hours to months)
- 🟢 **True arbitrage**: If executed properly, profit is mathematically guaranteed

### Effort to Build
**2-3 days**. Market matching is the hard part. Both APIs are well-documented. Open source code exists to reference.

### Realistic Profit Potential
- On BTC hourly markets: spreads of 1-4% appear multiple times daily
- On political/macro events: spreads of 2-8% but resolve over days/weeks
- **Daily**: $5-50 on $1K capital (fast markets), more with scale
- **Weekly**: $30-200 realistic with $2-5K deployed
- Capital efficiency is low on long-dated markets (money locked up)

---

## Strategy 2: Polymarket Market Making on Long-Tail Events <a name="strategy-2"></a>

**One-liner**: Provide liquidity on volatile prediction markets, earn the bid-ask spread plus maker rebates.

### Platform(s) & Market Type
- **Polymarket** CLOB (Central Limit Order Book)
- Best on: mid-volatility markets with decent volume but not dominated by HFT bots
- Sweet spot: political events, crypto milestones, sports with 24h-1 week resolution

### The Edge
- Polymarket now pays **maker rebates** from taker fees collected in each market
- Most retail traders are takers (they hit your resting orders)
- You earn the spread + rebates while managing inventory
- LLM reasoning can help you set better fair-value estimates than naive MM bots
- Less competitive than fast markets — HFT bots focus on crypto price markets

### Technical Implementation
```
Stack: Python + Polymarket CLOB API + LLM for fair-value estimation
1. Select markets with moderate volume and wide spreads (>3%)
2. Use LLM to estimate fair probability from news, polls, data
3. Place limit orders on both sides: BID at (fair - spread/2), ASK at (fair + spread/2)
4. Monitor and requote as information changes
5. Manage inventory: don't accumulate too much of one side
```

**Key components**:
- Polymarket WebSocket for real-time order book
- News/data feeds for fair-value updates
- Position/inventory management logic
- Auto-hedging when inventory gets unbalanced

### Fee Structure & Breakeven
- **Maker fees**: 0% (makers are fee-free on Polymarket)
- **Maker rebates**: Variable, funded by taker fees in each market
- **Breakeven**: Any positive spread after accounting for adverse selection
- Sweet spot: 2-5% spread on markets where you can estimate fair value well

### Risk Assessment
- ⚠️ **Adverse selection**: Informed traders pick you off when news breaks
- ⚠️ **Inventory risk**: You end up holding a losing position
- ⚠️ **LLM accuracy**: If your fair-value estimate is wrong, you lose systematically
- ⚠️ **Competition**: Other MMs will tighten spreads over time
- 🟢 **Lower competition** than fast crypto markets — event markets are more LLM-friendly

### Effort to Build
**3-5 days**. The MM logic is straightforward; the hard part is good fair-value estimation and inventory management.

### Realistic Profit Potential
- **Daily**: $10-100 depending on capital deployed and market selection
- **Weekly**: $50-500
- Scales with capital but adverse selection risk also scales
- Documented: Polymarket's own blog highlights successful market makers

---

## Strategy 3: LLM-Powered News → Prediction Market Trading <a name="strategy-3"></a>

**One-liner**: Use LLM to parse breaking news faster than markets can reprice, then trade prediction markets before they adjust.

### Platform(s) & Market Type
- **Polymarket** (political, crypto, world events)
- **Kalshi** (same categories, different user base)
- Market types: elections, policy decisions, geopolitical events, crypto milestones

### The Edge
This is **the** edge our tech stack is uniquely suited for:
- LLMs can parse and interpret complex news in seconds
- Most prediction market participants react slowly (minutes to hours)
- Polymarket event markets are priced by crowd sentiment, not algorithms
- **Concrete example**: Fed announces surprise rate decision → LLM parses FOMC statement → trades "Fed rate" markets before crowd reprices
- **Another**: Breaking news about a political candidate → LLM assesses impact → trades election markets

### Technical Implementation
```
Stack: Python + news APIs + LLM (Claude/GPT) + Polymarket API
1. Monitor news sources in real-time:
   - Twitter/X API or scraping (breaking news)
   - RSS feeds (Reuters, AP, Bloomberg terminals if available)
   - Polymarket's own event feeds
   - Reddit/Discord for crypto-specific alpha
2. LLM pipeline:
   a. Classify: Is this market-moving for any open prediction market?
   b. Assess: What should the new probability be?
   c. Compare: Current market price vs. LLM estimate
   d. Size: How confident are we? Position size accordingly.
3. Execute via Polymarket API if edge > threshold (e.g., >5%)
4. Set exit: sell when market converges to LLM estimate, or stop-loss
```

**Key APIs**:
- News: NewsAPI, Twitter/X, RSS aggregators
- LLM: Claude API (we already have it via OpenClaw)
- Execution: Polymarket CLOB

### Fee Structure & Breakeven
- **Polymarket event markets**: Currently 0% fees (most non-crypto-fast markets)
- **Breakeven**: Need LLM to be right more than wrong on direction
- Even a small information-processing edge (30 seconds faster) can be worth 2-10% on event markets

### Risk Assessment
- ⚠️ **LLM hallucination**: Misinterpreting news = wrong trade
- ⚠️ **Speed**: Other bots/LLMs doing the same thing
- ⚠️ **Low frequency**: Major market-moving news is sparse (few per day)
- ⚠️ **Illiquidity**: Event markets may not have enough liquidity to trade at good prices
- 🟢 **Our sweet spot**: We have LLM reasoning + automation, which most retail traders don't
- 🟢 **Fee-free on most event markets**: No drag on returns

### Effort to Build
**1-2 days** for MVP (monitor + analyze + alert). **3-5 days** for full auto-trading.

### Realistic Profit Potential
- **Highly variable**: $0 on quiet days, $100-1000 on big news days
- **Weekly average**: $50-300 if trading 3-5 events per week
- **The honest truth**: This is more like "skilled discretionary trading assisted by LLM" than pure automation. The edge is real but episodic.

---

## Strategy 4: Crypto Funding Rate Arbitrage <a name="strategy-4"></a>

**One-liner**: Go long spot + short perpetual (or vice versa) to collect funding rate payments while being market-neutral.

### Platform(s) & Market Type
- **Any crypto exchange with perpetual futures**: Binance, Bybit, OKX, Bitget
- All have built-in funding rate arbitrage bots now
- Works on BTC, ETH, SOL, and any high-volume perpetual pair

### The Edge
- Perpetual futures have **funding rates** paid every 8 hours
- When funding is positive (longs pay shorts): short perps + buy spot = collect funding
- When funding is negative: long perps + sell/short spot = collect funding
- **Delta-neutral**: price movements cancel out between spot and perps
- Rates can be 0.01-0.3% per 8h period (annualized 10-300%+ during volatile periods)

### Technical Implementation
```
Stack: Python + CCXT library + exchange APIs
1. Monitor funding rates across exchanges (CCXT provides unified API)
2. When funding rate > threshold (e.g., > 0.05% per 8h):
   a. Open spot long + perp short (or vice versa for negative rates)
   b. Size positions equally for delta-neutral
3. Collect funding every 8 hours
4. Close when funding rate drops below threshold
5. Rotate to whichever pair has highest funding
```

**Key library**: `ccxt` (Python, supports 100+ exchanges with unified API)

### Fee Structure & Breakeven
- **Trading fees**: ~0.1% maker / 0.1% taker per leg (0.2% round-trip per leg × 2 legs = 0.4%)
- **Funding collection**: 0.01-0.3% per 8h
- **Breakeven**: Need ~0.05%+ per 8h to cover entry/exit fees (assuming 1-2 day hold)
- Many exchanges (OKX, Bitget, Crypto.com) now offer **built-in funding arb bots** with reduced fees

### Risk Assessment
- ⚠️ **Execution risk**: Spot and perp legs must be opened simultaneously
- ⚠️ **Liquidation risk**: If perp position moves against you before spot hedges
- ⚠️ **Funding rate reversal**: Rate can flip, making you pay instead of collect
- ⚠️ **Exchange risk**: Funds on centralized exchange (counterparty risk)
- 🟢 **Market-neutral**: Don't need to predict price direction
- 🟢 **Well-understood**: Many exchanges provide this as a built-in product

### Effort to Build
**1 day** if using exchange built-in bots (just configure). **2-3 days** for custom implementation with cross-exchange optimization.

### Realistic Profit Potential
- **Typical funding**: 0.01-0.05% per 8h in calm markets = ~1-5% monthly
- **During volatility spikes**: 0.1-0.3% per 8h = 10-30%+ monthly
- **On $5K capital**: $50-250/month in calm markets, $500-1500 during volatile periods
- **Caveat**: Built-in exchange bots have made this very competitive. Custom edge comes from cross-exchange rate shopping and timing entries.

---

## Strategy 5: Cross-Exchange Crypto Spot Arbitrage <a name="strategy-5"></a>

**One-liner**: Buy crypto on Exchange A where it's cheaper, sell on Exchange B where it's more expensive.

### Platform(s) & Market Type
- Any centralized exchanges: Binance, Coinbase, Kraken, KuCoin, MEXC, etc.
- Best on: smaller altcoins with fragmented liquidity
- Also viable on DEXs vs. CEXs (Uniswap vs. Binance)

### The Edge
- Price differences between exchanges persist due to:
  - Regional demand differences
  - Deposit/withdrawal delays creating temporary dislocations
  - Listing timing differences
  - Liquidity fragmentation on smaller tokens
- Speed of detection + execution = edge

### Technical Implementation
```
Stack: Python + CCXT + WebSockets
1. Subscribe to real-time price feeds from 5-10 exchanges via CCXT
2. Maintain local order book snapshots for target pairs
3. When price difference > (fees + slippage threshold):
   a. Buy on cheap exchange, sell on expensive exchange
   b. OR: Pre-fund both exchanges, buy/sell simultaneously, rebalance later
4. Rebalance across exchanges periodically
```

### Fee Structure & Breakeven
- **Trading fees**: 0.1% per trade × 4 trades (buy + sell on each exchange) = 0.4%
- **Transfer fees**: Varies wildly (BTC ~$1-5, ETH ~$2-10, stablecoins ~$1)
- **Breakeven**: Need >0.5% price difference for pre-funded approach, >1% with transfers
- Smaller altcoins have wider spreads but also more slippage

### Risk Assessment
- ⚠️ **Latency**: HFT bots with co-located servers eat these opportunities in milliseconds
- ⚠️ **Transfer risk**: Blockchain congestion can delay transfers, price can converge before you complete
- ⚠️ **Capital inefficiency**: Need pre-funded accounts on multiple exchanges
- ⚠️ **Slippage**: Advertised price ≠ execution price, especially on thin books
- 🔴 **Honest take**: This is the most hyped and least profitable strategy for retail. The spreads that exist are either too small or too fleeting for a Mac-based bot to capture.

### Effort to Build
**1-2 days** for monitoring. **3-5 days** for execution.

### Realistic Profit Potential
- **Major pairs (BTC, ETH)**: Spreads of 0.01-0.05% — not profitable after fees for us
- **Mid-cap altcoins**: Spreads of 0.1-0.5% — marginal at best
- **New listings/delistings**: Spreads of 1-10% — episodic, requires fast execution
- **Honest weekly estimate**: $0-50 on $5K capital. Not worth it as primary strategy.
- **Exception**: DEX-CEX arb on new token launches can be lucrative but requires DeFi integration

---

## Strategy 6: Polymarket Multi-Outcome Mispricing (Dutching) <a name="strategy-6"></a>

**One-liner**: Find markets where the sum of YES prices across all outcomes ≠ 100%, and exploit the gap.

### Platform(s) & Market Type
- **Polymarket** multi-outcome markets (elections, "which country will X", ranges, etc.)
- Also applicable on Kalshi

### The Edge
- In multi-outcome markets (e.g., "Who will win the 2028 Democratic primary?"), the sum of all YES prices should equal ~$1.00
- When sum < $1.00: buy all outcomes → guaranteed profit at settlement
- When sum > $1.00: sell/short all outcomes (if possible)
- Mispricings happen because:
  - New candidates/outcomes added
  - Low-probability outcomes are illiquid
  - Retail traders overreact to news on one outcome without adjusting others

### Technical Implementation
```
Stack: Python + Polymarket API
1. Scan all multi-outcome markets via Polymarket API
2. Sum YES prices across all outcomes for each market
3. Alert when sum deviates from 1.00 by > threshold (e.g., >2%)
4. If sum < 0.97: buy all YES outcomes proportionally → guaranteed 3%+ profit
5. If sum > 1.03: more complex (need to short/sell), but still exploitable
6. Monitor and exit if prices normalize before settlement
```

### Fee Structure & Breakeven
- **Polymarket event markets**: 0% fees (most multi-outcome markets)
- **Breakeven**: Any deviation from 100% is pure profit if you can buy all outcomes
- **Liquidity constraint**: May not be able to fill all outcomes at listed prices

### Risk Assessment
- ⚠️ **Liquidity**: Low-probability outcomes often have thin order books
- ⚠️ **Market changes**: New outcomes can be added (diluting existing ones)
- ⚠️ **Settlement ambiguity**: "None of the above" or resolution disputes
- ⚠️ **Capital lockup**: Money locked until event resolves
- 🟢 **True arbitrage** when sum < 100% and you can fill all sides
- 🟢 **Zero fees** on most event markets

### Effort to Build
**4-8 hours**. Scanner is trivial. The hard part is assessing whether you can actually fill all legs at the listed prices.

### Realistic Profit Potential
- Mispricings of 1-3% are common, 5%+ rare but they happen
- **Issue**: Capital lockup. $1K locked for a month to make $30 is 3% monthly — decent but illiquid
- **Best use**: Combine with market making — if you're already providing liquidity, catching these is bonus alpha
- **Weekly**: $10-50 on $2-5K capital, but with long lockup periods

---

## Strategy 7: Sports/Event Prediction Market Arbitrage <a name="strategy-7"></a>

**One-liner**: Exploit odds differences between prediction markets (Polymarket) and traditional sportsbooks for the same events.

### Platform(s) & Market Type
- **Polymarket** (expanding into sports — NBA, soccer, etc.)
- **Traditional sportsbooks** via odds APIs (The Odds API, OddsJam)
- **Kalshi** (limited sports markets)

### The Edge
- Prediction markets and sportsbooks price the same events differently
- Sportsbooks have vig (overround); prediction markets may not
- When combined odds create guaranteed profit, you have an arb
- **Bonus**: Polymarket is expanding sports markets aggressively (Serie A, NBA, etc.), creating new inefficiencies during launch

### Technical Implementation
```
Stack: Python + The Odds API + Polymarket API + browser automation for sportsbook execution
1. Subscribe to The Odds API ($40/month) for real-time sportsbook odds
2. Monitor Polymarket sports markets
3. Convert all odds to implied probabilities
4. When Polymarket + sportsbook creates arb (combined probability < 100%):
   a. Place bet on Polymarket via API
   b. Place opposing bet on sportsbook (browser automation or API)
5. Collect guaranteed profit at settlement
```

**The Odds API**: Covers 70+ sportsbooks, $40/month for real-time odds.

### Fee Structure & Breakeven
- **Polymarket**: 0% on most sports markets (or low taker fees)
- **Sportsbooks**: Built into odds (typically 4-10% overround)
- **The Odds API**: $40/month
- **Breakeven**: Need >5% combined edge to overcome sportsbook vig

### Risk Assessment
- ⚠️ **Sportsbook bans**: Sportsbooks actively ban winning/arb bettors
- ⚠️ **Execution speed**: Odds change fast, especially pre-game
- ⚠️ **Settlement differences**: Different rules for voided events, etc.
- ⚠️ **Geographic restrictions**: Many sportsbooks are geo-restricted
- ⚠️ **Browser automation fragility**: Sportsbook UIs change frequently
- 🟡 **Moderate edge**: Opportunities exist but sportsbook account longevity is the constraint

### Effort to Build
**3-5 days**. Odds comparison is easy. Sportsbook execution via browser automation is the hard/fragile part.

### Realistic Profit Potential
- **Per arb**: 1-5% margin, typically $5-50 per bet
- **Frequency**: 5-20 opportunities per day across all sports
- **Weekly**: $100-500 if you have multiple sportsbook accounts
- **Account longevity**: 1-6 months before getting limited/banned on sportsbooks

---

## Comparison Matrix <a name="comparison-matrix"></a>

| Strategy | Effort | Capital Needed | Daily Profit | Edge Durability | Risk Level | Our Fit |
|---|---|---|---|---|---|---|
| 1. Cross-Platform Arb (Poly/Kalshi) | 2-3 days | $2-5K | $5-50 | Medium (structural) | Low | ⭐⭐⭐⭐ |
| 2. Polymarket Market Making | 3-5 days | $2-10K | $10-100 | Medium | Medium | ⭐⭐⭐ |
| 3. LLM News → Prediction Markets | 1-5 days | $1-5K | $0-1000 (episodic) | High (unique) | Medium | ⭐⭐⭐⭐⭐ |
| 4. Funding Rate Arbitrage | 1-3 days | $3-10K | $5-50 | Low (commoditized) | Low-Medium | ⭐⭐⭐ |
| 5. Cross-Exchange Spot Arb | 3-5 days | $5-10K | $0-50 | Very Low | Medium | ⭐ |
| 6. Multi-Outcome Mispricing | 4-8 hours | $2-5K | $5-20 | Medium | Low | ⭐⭐⭐⭐ |
| 7. Sports Prediction Arb | 3-5 days | $1-5K | $20-100 | Low (bans) | Medium | ⭐⭐ |

---

## Honest Assessment: What's Realistic for Us <a name="honest-assessment"></a>

### The Uncomfortable Truth

1. **Pure latency arbitrage is dead for us.** The $313→$414K bot was running on infrastructure we can't match (sub-100ms execution). Polymarket's dynamic fees specifically target this. Don't waste time here.

2. **Cross-exchange spot arb is a trap.** Every Medium article about "$4K/day DeFi arb bots" is either lying, selling something, or describing edge cases from 2021. With a Mac running Python, you cannot compete with co-located servers running Rust.

3. **Our actual edge is LLM reasoning.** This is the one thing we have that most market participants don't. The ability to:
   - Parse complex news and assess market impact in seconds
   - Evaluate multi-variable political/event outcomes
   - Monitor dozens of markets simultaneously for mispricings
   - Combine multiple data sources (polls, sentiment, news) into probability estimates

### Recommended Priority Order

**Tier 1 — Build This Week:**
1. **LLM News Trading (Strategy 3)** — Highest edge-per-effort ratio. Start with monitoring + alerts, graduate to auto-trading. This is our unique advantage.
2. **Multi-Outcome Scanner (Strategy 6)** — Build in a few hours, runs passively, catches free money when it exists.

**Tier 2 — Build Next:**
3. **Cross-Platform Arb (Strategy 1)** — Real structural edge, but need Kalshi access (US KYC). Open-source code to reference.
4. **Polymarket Market Making (Strategy 2)** — Natural evolution of our existing infrastructure. LLM provides fair-value estimates, we provide liquidity.

**Tier 3 — Consider Later:**
5. **Funding Rate Arb (Strategy 4)** — Easy to set up via exchange built-in bots, but it's commoditized. Low effort, low edge. Good for parking idle capital.

**Skip:**
- Cross-exchange spot arb (can't compete on latency)
- Sports arb (account bans make it unsustainable)

### Realistic Combined Expectations

Running strategies 3 + 6 + 1 together with $5K capital:
- **Quiet week**: $50-150
- **Active news week**: $200-1000
- **Monthly average**: $400-2000
- **Annualized**: 100-500% ROI on $5K (but highly variable)

These numbers assume:
- Active monitoring and tuning
- Not scaling too aggressively (market impact)
- Accepting that some weeks will be near-zero
- Treating this as a skill-building exercise that could scale

### What the Research Conclusively Shows

The biggest profits in prediction markets right now come from **information edge**, not speed. The latency arbitrage era peaked in late 2025 and Polymarket is actively closing it. The next era favors:

1. **Better probability estimation** (LLMs are genuinely good at this)
2. **Cross-platform structural arbitrage** (fragmented markets = persistent inefficiencies)
3. **Market making with informed fair values** (earn spread + rebates)

Our tech stack (Python + OpenClaw + LLM + browser automation + cron) is **perfectly suited** for strategies 1-3 above. We should lean into our strengths (LLM reasoning, automation) rather than trying to compete on raw speed.
