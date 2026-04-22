# Scoring Rules

Manual-only invariant: scores and ranks are advisory only; no automated orders.

## Composite Probability Model

The core formula for each market:

```
IF phrase hit during current event/window:
    p_literal = 0.98

ELSE IF market_family == "windowed" (e.g. monthly/nickname windows):
    base = historical_series_phrase_rate(speaker, series_ticker, phrase)
           with Bayesian fallback to series-level prior and then speaker general base_rate
    p_full = base * news_pressure(phrase) * x_buzz(phrase)
    frac_remaining = remaining_window_fraction(close_time, series_ticker)   # usually 0..1
    p_literal = 1 - (1 - p_full) ^ frac_remaining

ELSE IF event.state == "scheduled":
    p_literal = base_rate(speaker, event_type, phrase)
                * news_pressure(phrase)
                * x_buzz(phrase)

ELSE IF event.state == "live":
    elapsed_frac = elapsed_sec / expected_duration_sec
    time_decay = 1 - elapsed_frac ^ 2
    p_literal = base_rate(speaker, event_type, phrase)
                * time_decay
                * news_pressure(phrase)
                * x_buzz(phrase)

ELSE IF event.state == "ended":
    p_literal = 0.02

ELSE IF event.state == "unknown":
    p_literal = 0.15

ELSE (no event):
    p_literal = 0.10 * news_pressure(phrase) * x_buzz(phrase)

p_literal is clamped to [0.02, 0.98]
```

Important routing note:
- The engine is no longer restricted to Trump/Leavitt/Mamdani-only market coverage.
- Newly discovered mention-like speaker series can still be displayed and scored even if the speaker has no transcript corpus yet.
- When a speaker has no calibrated corpus history, base rates fall back through speaker defaults and then the global default rather than hiding the market.

## Polymarket Cross-Market Blending

After computing model `p_literal`, the scorer checks for a matching Polymarket signal with confidence metadata.

```
# Step 1: exact phrase match
poly_signal = polymarket_prices.get_signal(phrase, speaker, timeframe_hint)

# Step 2: pool fallback — if no exact match, use weighted average of all
#         same-speaker same-timeframe Poly markets as a weak anchor
IF poly_signal is None:
    poly_signal = polymarket_prices.get_pool_signal(speaker, timeframe_hint,
                                                    min_markets=3, min_quality=0.50)
    # pool signal always has confidence_score=0.30 (weak — won't meaningfully move p)
    # adds reason code POLY_POOL

poly_yes = poly_signal.yes_price
poly_conf = poly_signal.confidence_score

IF poly_yes is not None AND poly_conf >= POLY_MIN_CONFIDENCE AND has_strong_match:
    effective_w = poly_blend_weight * poly_conf
    p_blended = (1 - effective_w) * p_literal + effective_w * poly_yes
    p_blended is clamped to [0.02, 0.98]
    p_literal = p_blended   # used for all downstream EV calculations

ELSE IF poly_yes is not None AND poly_conf < POLY_MIN_CONFIDENCE:
    p_literal unchanged
    add reason code POLY_LOW_CONF
```

Defaults:
- `poly_blend_weight`: `0.30`
- `POLY_MIN_CONFIDENCE`: `0.35`

The scorer iterates through all phrases associated with a market (not just the primary hit phrase) to maximize Polymarket match rate. Slug discovery was improved in session 26: the Gamma API is now paginated (`offset=0,100,200,300`) and a title-search endpoint is tried for each speaker query. The loop bug (sending identical requests 12× instead of varied queries) was fixed.

### Polymarket Reason Codes

- `POLY_DIVERGE`: `abs(poly_yes - model_p)` >= 0.15 — significant disagreement between markets
- `POLY_HIGHER`: `poly_yes > model_p + 0.05` — Polymarket prices YES higher than our model
- `POLY_LOWER`: `poly_yes < model_p - 0.05` — Polymarket prices YES lower than our model
- `POLY_CONF_HIGH`: Poly confidence bucket is high
- `POLY_CONF_MED`: Poly confidence bucket is medium
- `POLY_CONF_LOW`: Poly confidence bucket is low
- `POLY_LOW_CONF`: Poly found but confidence below blend threshold, so not blended
- `POLY_POOL`: No exact-phrase match; pool average used as weak anchor (conf=0.30)

These codes appear alongside standard reason codes and are surfaced in the dashboard.

## Wallet-Flow Alpha Modifier V2 (Conviction + Reputation Weighted)

After Polymarket blending, scorer applies wallet-flow signal as a bounded modifier
with conviction weighting and wallet reputation data:

```
wallet_signal = wallet_flow.get_signal(phrase, speaker, timeframe_hint)
wallet_alpha = wallet_signal.wallet_alpha_score     # [-1, 1] conviction-weighted
wallet_conf = wallet_signal.confidence              # [0, 1]
rep_alpha = wallet_signal.reputation_weighted_alpha  # [-1, 1] profitability-weighted

IF wallet_signal exists AND wallet_conf >= WALLET_MIN_CONFIDENCE:
    alpha = wallet_alpha
    IF abs(rep_alpha) > 0.02:
        alpha = 0.6 * wallet_alpha + 0.4 * rep_alpha   # blend reputation signal
    shift = WALLET_FLOW_WEIGHT * wallet_conf * alpha
    IF extreme_bet_count >= 2:
        shift *= 1.3                                     # amplify extreme conviction
    shift = clamp(shift, -0.15, +0.15)
    p_literal = clamp(p_literal + shift, 0.02, 0.98)
ELSE IF wallet_signal exists:
    p_literal unchanged
    add reason code WALLET_LOW_CONF
```

### V2 Signal Pipeline

The wallet flow fetcher (`scripts/fetch_wallet_flow.py` v2) now:
1. **Targeted fetching**: Queries `GET /trades?market=<condition_id>` per market instead of bulk unfiltered
2. **Price parsing**: Extracts trade `price` field for conviction weighting (distance from 0.50)
3. **Time decay**: Recent trades weighted higher via exponential decay (half-life 2h)
4. **Extreme bet detection**: Identifies trades at <=0.12 or >=0.88 price (high-conviction outliers)
5. **Wallet reputation**: Queries `/closed-positions?user=<addr>` for top wallets to compute win rate + PnL

Defaults:
- `WALLET_FLOW_WEIGHT`: `0.12`
- `WALLET_MIN_CONFIDENCE`: `0.35`

Wallet reason codes:
- `WALLET_FLOW_UP` / `WALLET_FLOW_DOWN`
- `WALLET_EXTREME_BETS` — 2+ extreme-price trades detected
- `WALLET_REP_STRONG` — reputation alpha > 0.05 (profitable wallets agree)
- `WALLET_LOW_CONF`

## Event-Topic Context Analysis

Before applying the composite probability model, the scorer checks event-topic relevance:

```
event_title = load_from_kalshi_cache(event_ticker)
topic = detect_topic(event_title)   # sports, economy, foreign_policy, etc.
topic_relevance = compute_relevance(topic, phrase)  # 0.20 to 1.20

base_rate_adjusted = base_rate * topic_relevance
```

Multipliers:
- **On-topic** (phrase matches event topic): `1.20x` boost
- **Weakly related** (adjacent topic): `0.50x`
- **Off-topic** (unrelated to event): `0.20x` dampen
- **Unknown phrase/topic**: `0.40x` floor
- **Generic event** (no detectable topic): `1.0x` (no adjustment)

Example: "Saving College Sports Roundtable"
- "NIL" → sports topic → ON_TOPIC → 1.20x
- "tariff" → economy topic → OFF_TOPIC → 0.20x (dampened from 62% to 12%)
- "iran" → foreign_policy → OFF_TOPIC → 0.20x

Implementation: `app/event_context.py`

## Live Settlement Signal

After Polymarket blending and wallet-flow, the scorer queries same-event settled markets:

```
settlement_sig = live_settlements.get_signal(event_ticker, phrase, speaker)

IF settlement_sig is not None AND settlement_sig.event_is_active:
    IF settlement_sig.phrase_settled_yes:
        p_literal = max(p_literal, 0.90)   # phrase already confirmed YES
        add reason code SETTLED_YES
    ELSE IF settlement_sig.phrase_settled_no:
        p_literal = min(p_literal, 0.10)   # phrase confirmed NOT said
        add reason code SETTLED_NO
    ELSE:
        p_literal += 0.04                   # event is active (speech is happening)
        add reason code EVENT_ACTIVE
```

Data source: `data/live_settlements.json`, refreshed every 3 minutes by `scripts/fetch_settlements.py` via `MaintenanceRunner`.

Settlement signals look back 6 hours (same speech window). For a market in event `KXPRESMENTION-DJT26MAR12`, if "tariff" settled YES at 2:38 PM today, then all other still-open markets in `KXPRESMENTION-DJT26MAR12` get `EVENT_ACTIVE +0.04`. If the exact phrase under evaluation is in the settled list, it gets hard-clamped.

This gives us a real-time "the speech is live" signal that's especially powerful for KXPRESMENTION specific-event markets which settle phrase-by-phrase during the speech.

Series monitored: `KXTRUMPSAY`, `KXTRUMPSAYEP`, `KXTRUMPMENTION`, `KXTRUMPMENTIONB`, `KXPRESMENTION`, `KXLEAVITTMENTION`, `KXSECPRESSMENTION`, `KXMAMDANIMENTION`.

## Market Veto Gate

After all probability calculations (model + Polymarket blend), the scorer applies a directional veto:

```
market_divergence = kalshi_yes_ask - p_literal  # positive = market more bullish

IF tentative_side == "BUY_NO" AND market_divergence >= market_veto_margin:
    side = WATCH  (reason: MARKET_VETO_NO)

IF tentative_side == "BUY_YES" AND -market_divergence >= market_veto_margin:
    side = WATCH  (reason: MARKET_VETO_YES)
```

Default: `market_veto_margin = 0.10`
Configurable via: `MARKET_VETO_MARGIN`

Rationale: When the live market prices a phrase 10%+ higher (or lower) than our model, the market has access to real-time event context that our static base rates lack. Attempting to fight the market in these cases loses systematically. The veto does NOT blend `p_literal` — it simply blocks the bet. This replaces the old "Market Anchor" blend (which was shown to lose at 6% win rate across 17 live bets).

### Polymarket Veto (POLY_VETO_NO)

```
IF tentative_side == "BUY_NO"
   AND POLY_HIGHER in reason_codes
   AND market_divergence > 0:
    side = WATCH  (reason: POLY_VETO_NO)
```

When both Polymarket and Kalshi price the phrase higher than our model, two independent liquid markets are disagreeing with a BUY_NO bet. This is blocked regardless of the market_veto_margin threshold.

## Input Components

### Market Family Routing

- `single_event` families use the event-state model (`scheduled/live/ended/unknown/no-event`)
- `windowed` families (currently monthly + nickname windows) are **not** treated as one speech
- Windowed families are scored from historical series outcomes + remaining window time
- This prevents blanket late-event BUY_NO artifacts on contracts that resolve across many appearances
- Some Kalshi briefing/mention series that look event-like are still effectively windowed from an operator perspective. Example: Leavitt `KXSECPRESSMENTION` may cover "next press briefing" with a month-end close rather than a same-day event ticker.
- Those markets should remain visible on the dashboard and be scored against the active Kalshi contract structure, not against an assumed same-day event slug.

### Base Rates

Lookup: `base_rates.yaml` keyed by `(speaker, event_type, phrase)`.

Example: `base_rate("trump", "rally", "tariff") = 0.85` means Trump mentions "tariff" in 85% of rallies historically.

#### Fallback Chain (updated)

`BaseRateLookup.get(speaker, event_type, phrase)` now uses:
1. `speaker.{event_type}.{phrase}` — corpus-calibrated per-type rate
2. `speaker.general.{phrase}` — **Kalshi-outcome-derived rate** (ground truth from 7,952+ resolved markets, weighted 8x vs transcript observations)
3. `speaker._default.{phrase}` — corpus-blended rate across all event types
4. `speaker._context_base.{event_type}` — **per-context empirical prior** (e.g. trump.rally=57%, trump.remarks=65%, trump.signing=35%) — fires for entirely new phrases with no prior data
5. `speaker._context_base.general` — overall speaker empirical prior (trump≈50%, leavitt≈41%, mamdani≈40%)
6. Global default `0.45` (empirical average YES rate across all 7,952 outcomes; was `0.30`)

The old flat `0.30` global default caused 76% of outcomes to be mis-priced by +15pp. The new `_context_base`, empirical global default, and recency-weighted calibration reduce Brier score from 0.253 → **0.185** and all calibration bins are within ±8% of actual.

**Recency Weighting (added session 26):** `calibrate_base_rates.py` now applies exponential decay with `--recency-halflife-days 90` (default). Outcomes from the last 90 days count ~2x, outcomes from 6+ months ago count ~0.2x. This catches fast political drift — "transgender" went from 0% pre-2026 to 67% post-2026; "economy" from 61% to 24%.

**All-Outcome Phrase Catalog (added session 26):** Previously 66% of outcomes (5,072 of 7,626) were discarded because their phrases weren't in the active live market catalog. Now ALL outcome phrases are stored in the `general` bucket regardless of catalog status — 1,468 unique general-bucket phrases. Future markets for retired or new phrases get real empirical rates instead of falling back to the context prior.

The `general` bucket is populated from all resolved Kalshi market outcomes (settled YES/NO) with recency weighting. As of the most recent run: 7,626 outcomes across Trump, Leavitt, Mamdani, and auto-detected speakers. Key rates (Trump, from 191 unique speech dates):

| Phrase | Outcome Rate | Old Config | Change |
|--------|-------------|------------|--------|
| sleepy joe | 82% | 37% | +45pp |
| fake news | 72% | 61% | +11pp |
| autopen / auto pen | 59–74% | 21% | +38–53pp |
| rigged election | 74% | 17% | +57pp |
| thug | 40% | 14% | +26pp |
| marijuana / weed | 10% | 3–8% | corrected |

#### Auto-recalibration

`fetch_outcomes.py` and `calibrate_base_rates.py --outcome-weight=8.0 --recency-halflife-days=90` now run daily via the `MaintenanceRunner`. The `config/base_rates.yaml` is refreshed automatically without manual intervention.

To manually trigger a full recalibration:
```bash
python3 scripts/calibrate_base_rates.py --outcome-weight 8.0 --recency-halflife-days 90
```

If no base rate exists for a (speaker, event_type, phrase) tuple, fall back to:
1. Speaker-level default for that phrase (any event type)
2. Global default: `0.45` (empirical average across all outcomes)

This fallback behavior is what allows newly discovered speakers and partially covered series to still receive deterministic scores before speaker-specific corpus coverage exists.

### Time Decay (Live Events Only)

```
time_decay = max(min_decay, 1 - (elapsed_frac) ^ decay_exponent)
```

Defaults:
- `decay_exponent`: 2 (quadratic -- gentle early, steep late)
- `min_decay`: 0.05 (never exactly zero while live)

Example at 80% through a 90-min rally:
- `elapsed_frac = 0.8`
- `time_decay = 1 - 0.64 = 0.36`
- If base_rate = 0.85: `p_literal = 0.85 * 0.36 = 0.31`

This generates the NO edge: market still prices YES at 0.50, but real probability is 0.31.

### News Pressure Modifier

Range: `[0.7, 1.5]` (bounded multiplier).

Source: `data/signals.yaml`, field `news_pressure` per phrase.
- `1.0` = normal (topic not especially in/out of news)
- `> 1.0` = topic is in the news cycle (increases probability)
- `< 1.0` = topic has been quiet (decreases probability)

Updated manually for now. Later populated by OpenClaw scraping news headlines.

### X (Twitter) Buzz Modifier

Range: `[0.7, 1.5]` (bounded multiplier).

Source: `data/signals.yaml`, field `x_buzz` per phrase.
- `1.0` = normal mention volume
- `> 1.0` = topic trending / spiking on X
- `< 1.0` = topic unusually quiet

Updated manually for now. Later populated by OpenClaw scraping X search results.

## EV Calculation

Uses executable prices (what you'd actually pay):

```
ev_yes = p_literal - yes_ask
ev_no  = (1 - p_literal) - no_ask
```

Note: `p_literal` here is the final value after Poly blending and wallet-flow adjustment (when enabled and above confidence gates).

## Side Decision

```
IF NOT gate_pass:
    side = WATCH
ELSE IF ev_yes >= ev_threshold AND ev_yes >= ev_no:
    side = BUY_YES
ELSE IF ev_no >= ev_threshold:
    side = BUY_NO
ELSE:
    side = WATCH
```

Default `ev_threshold`: 0.03

## Liquidity Gates (Must Pass)

- `spread_ok`: spread <= `max_spread` (default 0.15)
- `depth_ok`: depth_yes >= `min_depth` (default 0)
- `gate_pass`: both must be true for BUY_YES or BUY_NO; otherwise WATCH
- Configurable via env vars: `MAX_SPREAD`, `MIN_DEPTH`, `EV_THRESHOLD`

Note: defaults were relaxed from the original (0.03 spread, 250 depth) because mention markets are typically thin. The original strict gates blocked all cards.

## Executable Price Hint

- BUY_YES: "Buy YES at <= {yes_ask}"
- BUY_NO: "Buy NO at <= {no_ask}"
- WATCH: no hint

## Size Cap

Based on top-of-book depth for the relevant side:
- BUY_YES: size_cap = depth_yes
- BUY_NO: size_cap = depth_no
- WATCH: 0

## Ranking Rules

Primary sort:
1. `gate_pass` (true first)
2. `abs(ev)` of the chosen side, descending (strongest edge first)
3. `p_literal` confidence
4. Narrower spread
5. Higher depth

Tie-breaker: latest snapshot timestamp.

## Monthly Window Resolved Tracking

For windowed markets (e.g. `KXTRUMPSAYMONTH-26APR01`, `KXTRUMPSAYNICKNAME`), the scorer
checks if a phrase has already settled in the current window before scoring:

```
window_state = WindowStateCache.from_cache()   # loads outcomes + live settlements
ws = window_state.get(event_ticker, series_ticker)

IF ws.is_yes(phrase):
    p_literal = P_WINDOW_SETTLED_YES (0.98)    # already confirmed this month
    add reason code WINDOW_SETTLED_YES

ELSE IF ws.is_no(phrase):
    p_literal = P_WINDOW_SETTLED_NO (0.02)     # explicitly settled NO this month
    add reason code WINDOW_SETTLED_NO

ELSE:
    # Apply pace signal: if window is running hot/cold vs historical average
    pace_adj = ws.pace_signal()                # range [-0.02, +0.02]
    p_literal += pace_adj
    IF pace_adj != 0.0:
        add reason code WINDOW_ACTIVE_PACE
```

Data source: `app/window_state.py`, reloaded every 15 min alongside outcomes refresh.

## Rolling N-Speech Hit Rate

After computing the base rate, the scorer checks for a rolling N-speech rate that differs
significantly from the historical average:

```
roll_result = rolling_rates.blend(speaker, phrase, base_rate)

IF roll_result is not None:
    blended_base, tag = roll_result
    # tag is ROLLING_N3, ROLLING_N5, or ROLLING_N10
    base = blended_base
    add reason code tag

# Blend weights:
#   n5 (last 5 speeches, min 3 obs): 30% rolling / 70% historical  [preferred]
#   n3 (last 3 speeches, min 2 obs): 25% rolling / 75% historical
#   n10 (last 10 speeches, min 5 obs): 20% rolling / 80% historical
# Only applies if |rolling_rate - historical| >= 0.05
```

Data source: `data/rolling_hit_rates.json`, written daily by `scripts/compute_rolling_rates.py`.

## Price Velocity (Smart Money Signal)

After source agreement, the scorer applies a market price momentum signal:

```
vel_sig = price_velocity.get(market_id)   # returns None if NEUTRAL or no data

IF vel_sig is not None:
    vel_adj = vel_sig.p_adjustment()      # +0.025 to +0.05, scaled by signal_strength
    p_literal = clamp(p_literal + vel_adj, 0.02, 0.98)
    add reason code vel_sig.signal        # "SMART_MONEY_UP" or "SMART_MONEY_DOWN"
```

Signal classification (from `data/price_velocity.json`, refreshed every 5 min):

| Window | UP threshold | DOWN threshold | Applies when |
|--------|-------------|----------------|--------------|
| 2h     | delta > +0.06 | delta < -0.06 | past price ≥ 0.35, data age ≤ 4h |
| 6h     | delta > +0.10 | delta < -0.10 | past price ≥ 0.35, data age ≤ 12h |
| 24h    | delta > +0.15 | delta < -0.15 | past price ≥ 0.35, data age ≤ 48h |

Additional DOWN gate: past price must be ≥ 0.50 (otherwise drop is natural time decay, not informed selling).

p_adjustment scales with `signal_strength` (0–1):
- `strength=0.5` → ±0.025
- `strength=1.0` → ±0.050 (max cap)

Signal quality note: improves significantly after 7+ days of continuous runner operation,
when 2h/6h deltas reliably capture same-session informed flow rather than gap artifacts.

Velocity data stale after 10 min — `PriceVelocityCache` returns empty if JSON is older.

## Implementation Status

The composite model is **fully implemented** in `app/scoring.py`.
All five states (scheduled, live, ended, unknown, no-event) are active with base rates,
time decay, and signal modifiers. Polymarket confidence-weighted blending (with CLOB book depth,
pool fallback, improved slug discovery), conviction/reputation-weighted wallet-flow alpha (V2),
live settlement signals, monthly window resolved tracking, rolling N-speech hit rates, and
price velocity smart-money signal are fully integrated.
**202 tests** verify correctness across states, integration paths, and edge cases.

**Backtest results (as of session 26, 7,626 historical outcomes):**
- Brier score: **0.1848** (0=perfect, 0.25=random; was 0.2530 before sessions 24-26)
- 11/11 calibration bins within ±8% of actual YES rate
- Simulated 5,958 bets at $10/bet: **73.7% win rate, +$13,129 P&L, +22% ROI**
