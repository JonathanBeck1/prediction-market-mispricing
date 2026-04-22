# Multiplier Calibration Guide

## How Multipliers Work in the Model

Your multiplier is applied multiplicatively to the historical base rate:

```
p_adjusted = base_rate × your_multiplier
```

The result is then combined with other factors (time decay, spread, etc.) and
clamped to [0.02, 0.98].

## Calibration Table

Use this table to check whether your multiplier makes intuitive sense:

| Base Rate | Your Multiplier | Resulting Probability | When to Use |
|-----------|-----------------|----------------------|-------------|
| 0.60      | 1.5x            | 0.90                 | Direct evidence this WILL be said today |
| 0.60      | 1.3x            | 0.78                 | Strong relevance to event topic |
| 0.60      | 1.1x            | 0.66                 | Modest relevance |
| 0.60      | 0.8x            | 0.48                 | Slightly less likely than usual |
| 0.60      | 0.5x            | 0.30                 | Significantly off-context |
| 0.60      | 0.2x            | 0.12                 | Phrase is highly inappropriate for format |
| 0.40      | 1.3x            | 0.52                 | Strong relevance (but capped for low base) |
| 0.40      | 1.15x           | 0.46                 | Tangential relevance |
| 0.40      | 0.5x            | 0.20                 | Off-context |
| 0.40      | 0.2x            | 0.08                 | Very off-context |
| 0.15      | 1.15x           | 0.17                 | Slight relevance (max for low base rate) |
| 0.15      | 0.5x            | 0.08                 | Further suppressed |
| 0.15      | 0.1x            | 0.02                 | Near zero — extremely unlikely |

## Hard Rules

### Boost Caps by Base Rate (STRICT — live data evidence)
- Base rate < 35%: **max boost 1.10x** — (was 1.15x, reduced: live data shows 30% WR on boosts)
- Base rate 35–45%: max boost 1.15x (was 1.25x)
- Base rate 45–55%: max boost 1.25x (was 1.40x)
- Base rate > 55%: max boost 1.40x (was 1.60x, higher only with direct evidence)

**These caps are hard limits. If you find yourself wanting to boost higher,
you need stronger evidence — not a higher number. The system will clamp to 1.30x
at read time regardless; overshooting wastes your reasoning.**

### Suppression Floor
- Never go below 0.05x (5% of base rate)
- 0.05x–0.15x: phrase is contextually impossible (earnings call mentioning "witch hunt")
- 0.15x–0.35x: phrase is highly inappropriate for the format
- 0.35x–0.65x: phrase is unlikely but not impossible (Trump can go off-script)
- 0.65x–0.90x: phrase is slightly less likely than historical average

### When to Use Each Band

**1.4x – 1.8x (Strong Boost)**
Use ONLY when:
- Direct evidence in Truth Social post / breaking news
- Phrase is literally central to the event's subject (e.g., "iran" at Iran negotiation event)
- Speaker has shown this phrase in MULTIPLE recent signals

**1.1x – 1.3x (Moderate Boost)**
Use when:
- Phrase is clearly relevant to today's top news story
- Event topic overlaps with phrase's semantic domain
- Recent signals (WH schedule, news) mention this topic

**0.7x – 0.9x (Mild Suppression)**
Use when:
- Topic is present in news but NOT today's primary focus
- Format slightly discourages the phrase but doesn't prohibit it

**0.3x – 0.6x (Moderate Suppression)**
Use when:
- Phrase category is inappropriate for the event format
- No current signals support this phrase
- Event format discourages this category (e.g., personal insults at diplomatic event)

**0.05x – 0.25x (Heavy Suppression)**
Use when:
- Phrase is categorically wrong for this event type
- The speaker would almost never use this phrase in this context
- Earnings call political phrases, diplomatic event rally catchphrases

## Common Calibration Errors to Avoid

❌ **Applying 0.8x when 0.2x is warranted**
> "baseball" at a diplomatic dinner should be 0.10x, not 0.80x.
> The market is pricing in historical base rate (~12%); 0.80x gives 9.6% — almost no edge.
> At 0.10x, the model sees 1.2%, creating real BUY_NO signal.

❌ **Boosting a 10% phrase to 1.5x "because it's in the news"**
> A phrase with 10% base rate that appears once in news headlines → 1.10x max.
> 1.5x would give 15%, but there's no evidence that's accurate.

❌ **Forgetting to suppress clearly off-context phrases**
> If the event is a Congressional testimony on UN Reform, phrases like
> "election", "woke", "mog" should all be suppressed. Don't leave them neutral.

❌ **Treating all diplomatic events the same**
> Meeting Japan PM ≠ Meeting Israel PM ≠ Meeting NATO allies.
> The bilateral TOPIC determines which phrases to boost. Japan → toyota, shinzo, taiwan.
> Israel → bibi, gaza, hostages. NATO → ukraine, defense spending, article 5.

## Uncertainty Handling

If you genuinely cannot determine whether a phrase will be suppressed or boosted:
- Prefer a mild suppression (0.7x – 0.8x) over a boost for unknown phrases
- The model's base rate already reflects the average. Uncertainty means "less than average."
- Only boost when you have SPECIFIC evidence, not general relevance.

## Critical Reminder: Suppress > Boost

Live trading data from 178 real-money bets shows:
- **LLM suppress** (your multiplier < 1.0): **100% win rate** — the model was right every time
- **LLM boost** (your multiplier > 1.0): **30% win rate** — worse than flipping a coin

This means: for every phrase you're uncertain about, apply mild suppression.
Reserve boosts ONLY for phrases where you have specific, direct, traceable evidence.
"It's in the news" is NOT enough. "Trump posted about this exact topic yesterday" IS enough.
