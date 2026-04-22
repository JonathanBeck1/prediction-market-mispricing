# Trump Speech Pattern Intelligence

Ground-truth behavioral data from ACTUAL BET OUTCOMES. Trust this data over
assumptions about event format constraints.

---

## CRITICAL: Trump Is Format-Independent

**The single most important fact about Trump prediction markets:**
Trump's vocabulary does NOT change significantly by event format. Our actual
trading data proves this:

| Phrase            | Hist YES | Event Types Sampled         | Format Effect |
|-------------------|----------|-----------------------------|---------------|
| "sleepy joe"      | 92%      | Signings, pressers, remarks | None observed |
| "transgender"     | 93%      | KXTRUMPSAY (all formats)    | None observed |
| "democrat"        | 64%      | All formats                 | None observed |
| "fake news"       | 66%      | All formats                 | None observed |
| "china"           | 65%      | All formats                 | Slight boost at trade events |
| "deal"            | 70%      | All formats                 | None observed |

**Rule: NEVER suppress a Trump phrase below 0.85x if hist YES > 50%.**
The data proves format does not predict Trump's word choice.

---

## Universal Trump Phrases (>50% YES across ALL formats)

These should almost never be suppressed. Multiplier range: 0.90x – 1.3x.

- **"sleepy joe"** (92%) — Says this at bill signings, diplomatic events, everything
- **"transgender"** (93% in KXTRUMPSAY) — Culture war staple at every appearance
- **"deal"** (~70%) — Universal
- **"china"** (~65%) — Near-universal, boost at trade events
- **"fake news"** (~66%) — Habitual, format-independent
- **"democrat"** (~64%) — Constant political framing
- **"iran"** (~60%) — Consistently elevated since 2025
- **"beautiful"** — Universal descriptor
- **"tremendous"** — Universal descriptor
- **"oil"** (~55%) — Energy policy focus at every event type
- **"biden"** (~55%) — Habitual reference at domestic events
- **"great"** — Universal filler

---

## Habitual Phrases (20-50% YES, format-independent)

These have moderate base rates but are NOT rally-specific. Trump uses them
across all contexts. Multiplier range: 0.80x – 1.2x.

- **"radical left"** (~20%) — Low but consistent across ALL event types
- **"witch hunt"** (~25%) — Habitual, not rally-specific
- **"crooked"** (~30%) — Habitual nickname usage
- **"rigged election"** (~20%) — Format-independent
- **"deep state"** (~15%) — Occasional at all event types
- **"woke"** (~18%) — Culture war vocabulary, all formats
- **"shutdown"** (~15%) — When government funding is relevant
- **"stock market"** (~20%) — Economic boasting, all formats
- **"tariff"** (~35%) — Boost when trade is on agenda

---

## Topically-Driven Phrases (legitimate boost/suppress candidates)

These DO vary by topic and can be boosted or suppressed based on TODAY'S news:

- **"tariff"** — Boost when trade policy is on the agenda
- **"ukraine"** / **"russia"** — Boost when European security is discussed
- **"nato"** — Boost at European diplomatic events
- **"taiwan"** — Boost at Asia-focused events
- **"north korea"** — Boost when Korea is the bilateral topic
- **"israel"** / **"bibi"** — Boost when Middle East is active
- **"epstein"** — Slight boost when in news cycle, otherwise leave at 1.0
- **"cryptocurrency"** / **"bitcoin"** — Boost when crypto policy is active

---

## Low-Frequency Phrases (<15% YES, appropriate for cautious boosts only)

These are genuinely rare. Max boost: 1.15x from news context alone.

- "autism", "marijuana", "ufo", "discombobulator", "mog"
- Any very niche or situational phrase

---

## Anti-Pattern: DO NOT USE FORMAT-BASED SUPPRESSION FOR TRUMP

Previous LLM runs incorrectly suppressed phrases like "sleepy joe" at signing
events, reasoning "formal diplomatic event = no insults." This cost us money.

The data is clear: Trump says "sleepy joe" at 92% of ALL events including
formal bill signings. The LLM's format-based suppression assumptions are WRONG
for Trump. Trust the historical rates.

Only suppress Trump phrases when:
1. The phrase is topically irrelevant (e.g. "baseball" at a tariff event)
2. AND the historical YES rate is below 30%
3. AND there is no direct evidence (Truth Social post, news headline) suggesting it

---

## Japan-Specific Context (bilateral events)

When the bilateral partner is Japan or Japanese officials:

**Boost (1.2x – 1.5x):**
- "toyota", "honda", "sony", "japan", "japanese"
- "tariff" — automotive tariffs central to US-Japan trade
- "china" — Japan-China tensions
- "semiconductors", "chips" — US-Japan tech supply chain

**Leave at 1.0 (do NOT suppress):**
- All of Trump's habitual phrases — he still says them at bilateral meetings
