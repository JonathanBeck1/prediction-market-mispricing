# Per-Event Assessment Guide

This guide is for the per-event phrase scoring step (analyze_event.py).
You are assessing ONE specific event with a known format, participants, and topic.
Your job is to answer: **"Given this exact event, what multiplier is appropriate
for each phrase — specifically because of this format and these participants?"**

## Your Unique Role vs. Global Layer

The global layer already handled "is this topic hot in the news today?"
Your job is different:

> "Even if Iran is hot in the news globally, would THIS specific speaker
> say 'iran' at THIS specific event with THESE specific participants?"

A Congressional hearing on Housing Reform → suppress "iran" to 0.20x even if
Iran is in the news, because the committee's jurisdiction is housing.

A Diplomatic Dinner with Japan's PM → suppress "tampon tim" to 0.05x because
Trump would not use attack nicknames at a formal state dinner.

## Step 1: Read the Event Format Note

The format note tells you the general suppression/boost profile for this event
type. Apply it as the baseline for all phrases.

## Step 2: Identify the Bilateral Topic (if diplomatic)

For diplomatic events, the key variable is WHO is in the room and WHY.
Before assessing phrases, answer:
1. Who is the foreign participant? (name, country, title)
2. What is the primary bilateral agenda? (trade, security, treaty, etc.)
3. What country-specific phrases should be boosted?
4. What domestic US phrases should be suppressed?

## Step 3: Apply Format × Topic Constraints

For each phrase:
1. Does this phrase fit the event format? (Rally phrase at a signing → suppress)
2. Is this phrase specifically relevant to today's bilateral topic? → boost or suppress accordingly
3. Does the speaker's known style in this format align with using this phrase?

## Decision Framework

Ask these questions in order for each phrase:

```
Q1: Is this phrase categorically inappropriate for this event FORMAT?
    → Yes → suppress 0.1x–0.3x (don't go higher unless strong evidence)
    → No → continue

Q2: Is this phrase specifically CENTRAL to this event's topic?
    → Yes → boost 1.3x–1.8x
    → No → continue

Q3: Is this phrase RELEVANT but not central to this event's topic?
    → Yes → boost 1.1x–1.2x
    → No → continue

Q4: Does today's news context suppress or boost this phrase beyond format effects?
    → Suppress signal → 0.6x–0.8x
    → Boost signal → 1.05x–1.1x
    → Neither → 1.0x (omit)
```

## Bilateral Partner Quick Reference

### Japan
Boost: toyota, honda, nissan, shinzo, abe, japan, semiconductor, chip, taiwan, china (security), tariff, trade deal
Suppress: domestic US attack phrases, unrelated geopolitics (Venezuela, etc.), most rally catchphrases

### UK / Starmer
Boost: nato, ukraine, defense, special relationship, sterling, london, trade deal
Suppress: domestic US partisan phrases, Japan/China-specific phrases

### Israel / Netanyahu / Bibi
Boost: bibi, israel, gaza, hamas, hostages, iran, nuclear, normalization, abraham accords
Suppress: phrases unrelated to Middle East or domestic US politics that are off-bilateral-agenda

### NATO / European Summit
Boost: ukraine, nato, article 5, defense spending, russia, europe, burden sharing
Suppress: Asian geopolitics unrelated to Russia/Ukraine, domestic US attack phrases

### Saudi Arabia / Gulf
Boost: oil, aramco, energy, normalization, iran, trump (deal-making framing)
Suppress: pro-Israel specific phrases (if Saudi context), unrelated domestic phrases

### China / Xi Jinping
Boost: china, tariff, trade, fentanyl, taiwan, south china sea, huawei, tech
Suppress: unrelated geopolitics, domestic political attack phrases

## Reason Quality

Each reason should be ONE specific sentence explaining the FORMAT and PARTICIPANT
constraint, not the news cycle. Bad:

> "Iran is a hot topic right now" ← This is the global layer's job

Good:

> "Diplomatic dinner format suppresses domestic attack phrases; Trump has never
> used 'tampon tim' at a formal state dinner in our corpus"

> "Japan bilateral meeting — Toyota tariff discussions are the primary trade agenda;
> automotive companies are consistently mentioned in Trump-Japan meetings"

## Confidence in Suppression vs. Boost

Be MORE confident in suppressions than boosts.

Suppression logic: "The format PREVENTS this phrase" is verifiable.
Boost logic: "The format makes this phrase MORE likely" is probabilistic.

When in doubt:
- Suppress confidently if the phrase is categorically wrong for the format
- Boost conservatively if unsure whether the topic will arise

## Edge Cases

**Long diplomatic dinner with many topics:**
Don't suppress ALL foreign policy phrases — Trump may pivot to multiple topics.
Be selective: suppress phrases from topics NOT on the bilateral agenda;
boost phrases from topics that ARE on the agenda.

**Trump at formal ceremony but with crowd/reporters:**
If reporters are present at a signing or ceremony, off-script probability rises.
Moderate your suppression — 0.4x instead of 0.15x for borderline cases.

**Scheduled event vs. live event:**
For live events, the scorer applies a time-decay that makes the probability drop
as the event progresses without the phrase appearing. Your job is still the
format/participant constraint, not the time component.
