# Global Signal Assessment Guide

This guide is for the global phrase assessment step (analyze_signals.py).
At this stage, you are NOT reasoning about a specific event format.
You are assessing whether the CURRENT NEWS CYCLE makes each phrase more or
less likely than its historical average across ALL upcoming events.

## What You Have Access To

- **White House Schedule**: Official WH events for today/tomorrow. Highest signal.
  If a phrase appears in a WH event title → strong relevance signal.
- **Truth Social Posts**: Trump's own words from the last 24–48h.
  Direct quote containing a phrase → strong evidence of current focus.
- **White House RSS**: Official statements and press releases.
- **Google News Headlines**: Broader news context from AP, Reuters, NYT.
- **Google Trends**: What the public is searching (indicates salience).
- **Live Kalshi Events**: Which markets are currently open and active.

## Signal Hierarchy (most to least important)

1. **Trump Truth Social post EXPLICITLY uses the phrase** → boost 1.3x–1.5x
2. **WH Schedule event title contains or strongly implies the phrase** → boost 1.2x–1.4x
3. **Breaking news headline directly about this phrase's topic** → boost 1.1x–1.3x
4. **Google Trends showing topic at >50/100 interest** → boost 1.1x–1.2x
5. **Multiple weaker signals pointing same direction** → boost 1.05x–1.15x
6. **No signals at all** → 1.0x (omit from response)
7. **Today's schedule dominated by different topic entirely** → suppress 0.7x–0.9x
8. **Strong evidence this topic will NOT come up today** → suppress 0.3x–0.7x

## Suppression Rules for Global Layer

The global layer is less aggressive with suppression than the event layer
(because we don't know which specific event will be the context). But you
should still actively suppress:

- **Phrases with no signal in ANY source today** that have high historical rates:
  Apply mild suppression (0.8x–0.9x) if today's context is clearly about other topics.
- **Domestic political phrases on days dominated by foreign policy**:
  If WH schedule is all foreign diplomacy, suppress rally/domestic attack phrases.
- **Technical phrases when no relevant policy is active**:
  "debt ceiling", "budget" when no fiscal crisis is current → suppress to 0.7x.

## Do Not Double-Count

The event LLM layer will apply further event-specific adjustments on top of your global
multiplier. So:
- Global layer: "Is this phrase relevant to today's news cycle in general?"
- Event layer: "Given this specific event format and participants, adjust further."

Keep your global boosts moderate (max 1.5x usually) knowing the event layer will
refine further. Don't try to do both jobs at once.

## Distinguishing "In the News" from "Will Be Said"

A phrase being in the news does NOT guarantee it will be said.
Trump talks about Iran every week. If Iran is slightly elevated in trends today,
that's a 1.05x–1.1x boost, not 1.5x.

1.5x is only appropriate when there's direct, specific evidence that Trump will
address Iran TODAY at a specific event — like a scheduled Iran negotiation meeting
or a Truth Social post directly about Iran sanctions.

## Using Recent Transcripts

When the context includes RECENT SPEECH TRANSCRIPTS, prioritize them above news:
- If the transcript shows Trump using a phrase multiple times → boost 1.2x–1.4x
- If the transcript shows Trump NOT using a usual phrase → suppress 0.7x–0.85x
- Transcripts are the GROUND TRUTH of his actual language patterns, not predictions.

## Using Your Past Accuracy (Signal History)

When the context includes YOUR PAST LLM SIGNAL ACCURACY:
- If your past boosts for a phrase had <40% WR → be conservative, max 1.08x
- If your past suppression for a phrase had >70% WR → continue suppressing
- Ignore history for phrases where n < 3 (not enough data)

## Confidence Range — Express Uncertainty

When you are uncertain about a multiplier, express a range in your reasoning:
> "boost: 1.10x–1.25x — central estimate 1.15x, confidence: moderate"

The system will apply the conservative end when confidence is moderate or low.
Direct evidence = high confidence. News relevance only = low confidence.

## Examples of Good Global Assessments

**Scenario:** WH schedule shows "Bilateral Meeting with Japan PM" today.
Truth Social has no posts. Google News: Japan trade talks, Toyota tariffs.

Good assessments:
- "toyota": 1.20x — Japan automotive tariffs are central to today's bilateral.
- "tariff": 1.15x — Tariff policy is directly tied to the Japan meeting.
- "shinzo": 1.10x — Reference to late Shinzo Abe is common at Japan meetings.
- "china": 1.10x — US-Japan-China dynamics likely relevant.
- "epstein": 0.60x — Completely off-agenda for a diplomatic event day.
- "rally": 0.70x — No rally scheduled; domestic event phrases less likely.

**Scenario:** Trump Truth Social post: "Iran must stop or face OBLITERATION!"
WH schedule: empty. Breaking news: Iran military threat.

Good assessments:
- "obliterate": 1.50x — Directly in his own recent post. Strong signal.
- "iran": 1.40x — Central to current moment.
- "hormuz": 1.30x — Iran → Strait of Hormuz → oil supply chain.
- "oil": 1.20x — Iran crisis elevates oil discussion.
- "sanction": 1.20x — Logical policy response being discussed.
