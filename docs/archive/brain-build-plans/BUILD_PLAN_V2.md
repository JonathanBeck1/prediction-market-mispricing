# Build Plan V2 — Edge Engine Improvement Roadmap

> Created: 2026-03-25 (Session 38)
> Based on deep research synthesis + live gap analysis from today's Leavitt briefing session.
>
> **Manual-only invariant preserved throughout. No execution automation.**

---

## Gap Analysis: Where We Are vs. Where We Need to Be

Full audit against the deep research document. Items marked ❌ were NOT in the original plan.

| Research Requirement | Our Status | Gap Severity |
|---|---|---|
| Literal token frequency model | ✅ Done (base_rates.yaml + outcomes) | None |
| Market mid as baseline | ⚠️ Partial (Poly comparison only) | Medium |
| Walk-forward backtesting | ⚠️ Partial (no strict time ordering) | High |
| Contract type awareness (rolling vs same-day) | ❌ Missing | **Critical** |
| Double-counting mitigation | ❌ Missing | **Critical** |
| Survival/hazard intra-event model | ❌ Missing | High |
| Log-odds signal fusion | ❌ Missing (multiplicative only) | High |
| Order Book Imbalance (OBI) + VAMP | ❌ Partial (depth only, no OBI) | Medium |
| Brier Skill Score vs. all 3 baselines | ❌ Missing | High |
| Phrase synonym / resolution rule awareness | ❌ Missing | High |
| Live ASR dual-source confirmation | ❌ Single source only | High |
| Negation + attribution / quotation detection | ❌ Missing entirely | High |
| Exact string matching audit (no stemming) | ❌ Never audited | High |
| News deduplication (MinHash/entity-based) | ❌ Missing | Medium |
| Signal decay with calibrated half-lives | ⚠️ Partial (time_decay exists, not per-signal) | Medium |
| Cross-platform resolution rule divergence arbitrage | ⚠️ Partial (price gap only, not rule gap) | Medium |
| Speech structure hazard (intro/body/conclusion) | ❌ Missing | Medium |
| LLM domain-specific suppression | ❌ Apply LLM everywhere, should selectively skip | Medium |
| Reliability diagrams | ❌ Missing | Medium |
| Learned ensemble stacker | ⚠️ Platt only (no signal stacking) | Medium |

---

## Priority Tiers

### 🔴 P0 — Causing Real Losses Today (Fix First)

These created the exact failure modes we experienced in today's Leavitt session.

---

### P0-A: Rolling Window Contract Awareness

**Problem demonstrated today:** `KXSECPRESSMENTION-26MAR31` is a rolling monthly window
covering all of March. Phrases that were said in earlier March briefings (Iran, TSA, ICE)
already trade at $0.95–$1.00. Phrases at $0.85–$0.90 (nuclear, shutdown) may have been
said earlier this month too. Our BUY_NO recommendations on those were based on per-briefing
base rates — but the contract covers EVERY briefing for the rest of the month, meaning
multi-briefing probability accumulates.

**Fix required:**
1. Tag each market with its contract type: `same_day`, `rolling_window`, `duration`
2. For `rolling_window` contracts: load all prior settlements in that window (already in
   `window_state.py`) and compute the **remaining window probability** correctly:
   `P(YES by end of window) = 1 - (1-p_per_event)^N` where N = estimated remaining events
3. Scoring engine uses this compound probability instead of single-event p_literal
4. BUY_NO on rolling windows only valid when `P(YES by end) < 0.25` — much tighter gate
5. Dashboard should display contract type badge: `📅 Daily` / `🗓 Monthly window` / `⏱ Duration`

**Files to change:** `app/scoring.py`, `app/window_state.py`, `app/dashboard.py`,
`scripts/fetch_markets.py` (detect window type from close_time vs event date)

---

### P0-B: Signal Double-Counting Mitigation

**Problem:** When a news headline about Iran drops, ALL of these fire simultaneously:
- `news_pressure` boost in signals.yaml (from fetch_signals.py)
- `LLM_BOOST` (analyze_signals.py reads same news)
- `WH_KEYWORD` boost (fetch_wh_schedule.py reads same news)
- `EVENT_LLM_BOOST` (analyze_event.py reads same news)

We're quadruple-counting a single piece of evidence. The research calls this "the most
pernicious practical issue" in signal fusion.

**Fix required:**
1. **Event registry**: each signal emission tagged with `(entity, event_type, source_ts)`
2. **Deduplication window**: within 4-hour window, only the strongest signal from each
   causal event cluster counts; confirmatory signals get 0.30× weight dampening
3. **Correlation-aware weights**: if `news_pressure` and `llm_boost` are both firing on
   same topic in same direction, the second gets 40% weight, not 100%
4. Add `DOUBLE_COUNT_DAMPENED` reason code to flagged signals

**Files to change:** `app/scoring.py` (add dedup layer), `scripts/analyze_signals.py`
(tag signal sources), `data/signals.yaml` schema (add source_event field)

---

### P0-C: Strict Walk-Forward Backtesting + Baseline Comparisons

**Problem:** Our backtest uses all outcomes to calibrate base rates, then tests the same
data. This is lookahead bias. The research is explicit: train on speeches BEFORE the test
speech; never use future data.

More critically: we never compare against "always predict market mid" — the efficient
markets baseline. If we don't beat the market mid, the whole system has no justification.

**Fix required:**
1. `scripts/backtest_outcomes.py` rewrite with strict temporal ordering:
   - Sort outcomes by date
   - Train base rates only on outcomes BEFORE each test window
   - Roll forward event-by-event
2. Three mandatory baselines reported alongside model:
   - **Baseline A**: always predict historical base rate
   - **Baseline B**: always predict market mid (this is the critical one)
   - **Baseline C**: keyword count logistic regression
3. Compute **Brier Skill Score**: `BSS = 1 - BS_model/BS_baseline`
   BSS > 0 means we beat the baseline. BSS < 0 means we're worse than doing nothing.
4. Report confidence intervals (bootstrap resampling, n=1000)
5. Dashboard System tab: live rolling BSS vs market-mid updated after each resolved event

**Files to change:** `scripts/backtest_outcomes.py`, `app/dashboard.py`

---

## 🟠 P1 — High Impact, Medium Effort (Build Next)

---

### P1-A: Survival Model for Intra-Event Updating

**What it is:** Treat the speech as an observation window. Model time-to-first-mention
as a survival process. The key formula:

```
P(mention by end | not mentioned by time t) = 1 - S(T_total) / S(t)
```

As the speech progresses without a phrase being said, the model UPDATES in real time —
not just based on initial base rate but on elapsed time and speech structure.

**Why it matters:** Currently, if we're 40 minutes into a 60-minute briefing and "tariff"
hasn't been said, our model still outputs the same pre-event probability. The survival
model would correctly lower the probability as time passes without a mention, and raise it
for phrases more likely to come in the closing segment.

**Implementation:**
1. `scripts/fit_survival_model.py` — fit discrete-time logistic hazard per (speaker, event_type)
   from historical timestamped outcomes (we have positions in outcome corpus but need timestamps)
2. `app/survival_model.py` — `SurvivalEstimator.conditional_p(phrase, t_elapsed, t_total)`
3. Wire into scoring as a live-only signal (only when `speech_state == 'live'`)
4. Dashboard: live event cards show "T+40min, conditional p updated" indicator

**Data needed:** Timestamped phrase positions in corpus transcripts (word index → approximate
minute). Approximate timestamps can be derived from word count + average speaking rate
(~130 words/min for briefings, ~110 for Trump rallies).

**Files:** New `app/survival_model.py`, `scripts/fit_survival_model.py`,
modify `app/scoring.py` to wire live survival update

---

### P1-B: Phrase Resolution Rules Parser + Synonym Risk Scoring

**What the research says:** The single most common loss comes from the literal-vs-topic
mismatch. "You know immigration is 95% likely to be discussed" ≠ "the exact string
'illegal alien' will be spoken." Morphological variants, synonyms, and negation all trap
traders who don't read resolution rules.

**Our current gap:** We score phrases by historical hit rate, but we don't surface:
- What exact string resolves YES (case-sensitive? plural? "inflationary" counts for "inflation"?)
- What synonyms the speaker uses instead (does Leavitt say "undocumented" not "illegal alien"?)
- Negation risk ("We will NOT discuss immigration" still contains "immigration")

**Implementation:**
1. `scripts/parse_resolution_rules.py` — extract and store the literal match rule from
   each Kalshi market's subtitle/rules text (accessible via API)
2. Add `resolution_rule` and `match_type` (exact/stem/any) to `market_snapshots`
3. `scripts/build_synonym_map.py` — from corpus, detect when a phrase's base rate is
   low but semantically related phrases are high; flag as `SYNONYM_RISK`
4. Action cards surface resolution rule: "Resolves YES if speaker says 'illegal alien'
   (exact match, case-insensitive)"
5. `SYNONYM_RISK` reason code suppresses confidence when speaker has a preferred variant

**Files:** New `scripts/parse_resolution_rules.py`, `scripts/build_synonym_map.py`,
`app/scoring.py` (SYNONYM_RISK gate), `app/dashboard.py` (display resolution rule)

---

### P1-C: Order Book Imbalance (OBI) Signal

**What the research says:** OBI = (Q_bid - Q_ask) / (Q_bid + Q_ask) explains approximately
65% of short-interval price variance in prediction markets. OBI > 0.65 predicts price
increases within 15-30 minutes with ~58% accuracy.

**Our current gap:** We have Polymarket CLOB depth but we compute simple depth, not OBI.
We have Kalshi yes_bid/yes_ask but don't compute book imbalance.

**Implementation:**
1. `app/price_velocity.py` — add `compute_obi(yes_bid_depth, yes_ask_depth)` calculation
2. OBI > 0.65 → `BOOK_BULLISH` signal (+0.02 p_literal boost)
3. OBI < -0.65 → `BOOK_BEARISH` signal (-0.02 p_literal suppression)
4. VAMP (Volume-Adjusted Mid Price) = (bid * ask_vol + ask * bid_vol) / (bid_vol + ask_vol)
   replaces simple midpoint for market baseline comparisons

**Files:** `app/price_velocity.py`, `scripts/compute_price_velocity.py`

---

### P1-D: Negation + Attribution / Quotation Detection

**What the research says:** Two failure modes the research calls out explicitly that we
have ZERO protection against:

1. **Negation**: "We will NOT discuss Iran today" — contains "Iran", likely resolves YES
   on Kalshi (transcript-based). Our model would see "Iran" in captions and fire a
   BUY_YES boost. In reality the speaker just said the opposite.

2. **Attribution/quotation**: "My opponent said 'we will raise taxes'" — the current
   speaker is quoting someone else. "They say I'm 'extreme'" — sarcastic/rhetorical use.
   Our live caption detection would false-positive trigger.

The research specifies: resolution rules vary — some contracts count ANY utterance;
others require the speaker to express the view. We need to know which, and apply
detection accordingly.

**Implementation:**
1. `scripts/parse_resolution_rules.py` — fetch and store the resolution rule text from
   each Kalshi market's subtitle/rules field (already accessible in market metadata)
2. `app/phrase_matcher.py` — add `detect_negation(context_window, phrase)` using a
   15-word window around phrase detection: if "not", "never", "without", "no" appear
   before the phrase in the same clause → flag `NEGATION_CONTEXT`
3. `app/phrase_matcher.py` — add `detect_attribution(context_window)`: if phrase
   preceded by quotation mark, "said", "claimed", "argued", "called me" → flag
   `ATTRIBUTION_CONTEXT`
4. Both flags suppress live caption BUY_YES boost and annotate the Action Card
5. Dashboard: live event cards show ⚠️ "Detected in negation context — verify manually"

**Files:** New `app/phrase_matcher.py`, `scripts/parse_resolution_rules.py`,
`app/transcript_ingestor.py`, `app/scoring.py`

---

### P1-E: Exact String Matching Audit (No Stemming/Lemmatization)

**What the research says:** "Stemming and lemmatization are hazardous. Stemming reduces
'immigration' to 'immigr,' falsely matching 'immigrant' or 'immigrating.' For literal-
resolution contracts, the correct approach is exact case-insensitive string matching
after Unicode normalization (NFC form), stripping only punctuation."

**We have never audited this.** Our phrase matching in `app/phrase_matcher.py` and
`app/transcript_ingestor.py` could be using NLTK or spaCy stemming inherited from early
code. A single stemming step creates false positives across the entire corpus.

**Additional pitfalls the research lists we must check:**
- Unicode: curly quotes (U+2018/2019) vs. straight quotes — ASR outputs these differently
- Compound words: "healthcare" vs "health care" vs "health-care" — must handle all three
- Filler word insertion: "immi- immigration reform" stutters in raw ASR, cleaned in transcripts

**Implementation:**
1. Audit `app/phrase_matcher.py` — confirm exact substring match, no stemming
2. Add Unicode normalization: `unicodedata.normalize('NFC', text)` before matching
3. Add compound word expansion: each phrase entry in base_rates gets a list of
   `match_variants` (e.g., "healthcare": ["healthcare", "health care", "health-care"])
4. Test suite: `tests/test_phrase_matching.py` — verify "immigration" does NOT match
   "immigrant", "immigrating", "immigration's" (apostrophe variant)

**Files:** `app/phrase_matcher.py`, `config/base_rates.yaml` (add match_variants),
`tests/test_phrase_matching.py`

---

### P1-F: Dual-Source ASR Confirmation Architecture

**What the research says:** Modern ASR achieves 2.7% WER offline (Whisper Large-v3) but
14.5% WER in real-time streaming. Streaming takes a 6-7% additional WER hit. Confidence
scores "do not reliably predict actual correctness." The recommended pattern is:

1. **Streaming ASR (<500ms)**: initial phrase detection trigger
2. **Batch confirmation (1-5s)**: Whisper Large-v3 on the same audio segment for verification
3. **LLM analysis (2-10s)**: check for attribution/quotation context
4. **Official transcript (minutes-hours)**: cross-reference with authoritative source

Require agreement between layers 1 and 2 before firing an Action Card.
Multi-model strategies reduce error rates by 35-40% over single-model.

**Our current gap:** We have ONE transcript ingestor polling a single URL. No confirmation
layer, no dual-source, no confidence threshold.

**Implementation:**
1. `scripts/fetch_live_captions.py` — Layer 1: fast streaming source (yt-dlp YouTube
   auto-captions or WH.gov live briefing page, updated every 10-15s)
2. `scripts/confirm_caption_whisper.py` — Layer 2: when Layer 1 detects a phrase,
   fetch the last 30s of audio via yt-dlp, run Whisper locally for confirmation
3. Only fire `CAPTION_DETECTED` in scoring when BOTH layers agree
4. Layer 3 (negation/attribution from P1-D) runs as final filter
5. Action Card annotation: "Confirmed via dual-source ASR at T+42min"

**Files:** New `scripts/fetch_live_captions.py`, `scripts/confirm_caption_whisper.py`,
`app/transcript_ingestor.py`, `app/scoring.py`

---

### P1-G: Cross-Platform Resolution Rule Divergence Arbitrage

**What the research says:** "Cross-platform arbitrage between Polymarket and Kalshi is
documented edge: an estimated $40 million in near-risk-free arbitrage profits were
extracted from Polymarket between April 2024 and April 2025. These arise because platforms
have different resolution rules — Polymarket resolves if anyone on broadcast says the term;
Kalshi may restrict to named speakers and official transcripts — so a 10-point pricing gap
may reflect different questions rather than mispricing."

**Our current gap:** We compare prices (Poly vs Kalshi) and flag divergence, but we treat
every price gap as potential arb. We do NOT check whether the gap is explained by
different resolution rules. This creates false signals — we might recommend BUY_YES on
Kalshi because Poly is higher, but Poly is higher because their contract resolves YES if
ANY person in the room says it, while Kalshi requires the named speaker only.

**Implementation:**
1. `scripts/fetch_resolution_rules.py` — for each market, store:
   - Kalshi resolution criteria (from market subtitle/rules API field)
   - Polymarket resolution criteria (from Poly condition_id description)
   - `resolution_scope`: "named_speaker_only" vs "any_broadcast_speaker" vs "any_transcript"
2. `app/polymarket_prices.py` — when computing divergence, check resolution scope parity:
   - If scopes differ → flag `RESOLUTION_SCOPE_MISMATCH`, reduce arb weight to 0.2×
   - If scopes match → full arb weight (current behavior)
3. Dashboard: Poly column on cards shows scope badge (🎙 Speaker only / 📺 Full broadcast)

**Files:** `scripts/fetch_resolution_rules.py`, `app/polymarket_prices.py`,
`app/dashboard.py`

---

### P1-H: News Signal Deduplication (Entity-Based, MinHash)

**What the research says:** "Headline factory spam — aggregator sites repackaging the
same wire story — inflates counts without adding information. Deduplication via
MinHash/SimHash fingerprinting or entity+event tuple clustering is essential. Exponential
decay helps, but λ must be calibrated per signal type: breaking news decays fast
(half-life ~1.4 hours), structural changes decay slowly (~14 hours)."

**Our current gap:**
- `fetch_signals.py` pulls Google News RSS (6 queries) and counts articles mentioning
  phrases — but the same Reuters story gets repackaged by 15 aggregators, each counting
  as a separate data point. Our `news_pressure` signal is inflated by duplicate articles.
- We have a single time_decay function, not per-signal-type calibrated decay.

**Implementation:**
1. `scripts/fetch_signals.py` — add MinHash deduplication on article titles:
   - Hash each article title into a MinHash signature (Python `datasketch` library)
   - Within each fetch run, deduplicate articles with Jaccard similarity > 0.7
   - Across runs: deduplicate against last 4-hour window
2. Entity+event tuple deduplication as fallback: extract (entity, verb, date) and
   deduplicate on tuple match
3. Per-signal-type exponential decay in `app/scoring.py`:
   - Breaking news (article < 2h old): half-life 1.4h
   - Developing story (2-12h): half-life 4h
   - Structural/background (> 12h): half-life 14h
4. `NEWS_DEDUPED_COUNT` field in `signal_context.json` showing raw vs. deduped count

**Files:** `scripts/fetch_signals.py`, `app/scoring.py`, requirements.txt (add datasketch)

---

### P1-D: Live ASR Caption Monitoring (TRANSCRIPT_URLS actually working)

**What we have:** `TRANSCRIPT_URLS` env var and transcript ingestor polling every 30s.
**What we're missing:** An actual live caption source wired in. We've been flying blind
during live events.

**Sources to integrate (in priority order):**
1. **WH.gov live feed** — `https://www.whitehouse.gov/briefing-room/speeches-remarks/`
   live briefing pages have caption text that updates during the event
2. **YouTube live stream captions** via `yt-dlp --write-auto-sub` (free, works for
   C-SPAN / WH YouTube channel during briefings)
3. **Rev.ai streaming** — $0.02/min, professional-grade, direct API integration
4. **AssemblyAI streaming** — $0.012/min, good for backup

**Implementation:**
1. `scripts/fetch_live_captions.py` — polls YouTube/WH for live caption text, writes to
   `data/live_caption_buffer.txt` (rolling 5-minute window)
2. Modify `app/transcript_ingestor.py` to accept this as a caption source
3. When phrase detected in live captions: fire `CAPTION_DETECTED` signal, boost to 0.90
4. **Key architectural rule:** caption detection is advisory only — it triggers an
   Action Card update, not automatic execution

**Files:** New `scripts/fetch_live_captions.py`, modify `app/transcript_ingestor.py`,
`app/scoring.py` (CAPTION_DETECTED reason code)

---

## 🟡 P2 — Medium Impact, Lower Urgency

---

### P2-A: Speech Structure Hazard Rates (Intro / Body / Conclusion)

**What the research says:** The survival model can "capture speech-structure effects
(introduction vs. body vs. conclusion)" via covariates that shift the hazard rate based
on where in the speech we are. Some phrases cluster in openings (greetings, agenda
preview), some in the middle (policy substance), some at the end (calls to action,
signature catchphrases).

**Why this matters:** For a live event 40 minutes in, "we're in the body/policy section"
is meaningful information. Leavitt signature closes include "This is a great country" and
"We're proud of this administration" — if we're past the 80% mark of average briefing
length, these become more likely. Conversely, detailed policy phrases peak at 40-60%.

**Implementation:**
1. `scripts/fit_survival_model.py` — include speech position as a covariate:
   - `position_bucket`: [0-25%, 25-50%, 50-75%, 75-100%] of estimated speech length
   - Fit separate baseline hazard per bucket from timestamped corpus
2. Action Card: "T+40min (est. 65% through briefing) — structural hazard model active"
3. Use average historical speech length per (speaker, event_type) to estimate position

**Files:** `scripts/fit_survival_model.py`, `app/survival_model.py`

---

### P2-B: LLM Domain-Selective Application

**What the research says:** "Adding news context to LLM forecasts actually HURTS accuracy
in some domains (entertainment, technology) while helping in others (geopolitics,
politics)." The LLM should not be applied uniformly to all market types.

**Our current gap:** We run `analyze_event.py` on EVERY event — NBA games, earnings calls,
SCOTUS oral arguments, congressional hearings. The research suggests this hurts us on
earnings (LLM knows nothing useful about whether a CEO says "synergies" vs "integration")
and sports (LLM has no insight into what TV announcers say).

**Fix:**
1. Add `llm_eligible` flag to event types in `app/event_context.py`:
   - LLM-eligible: diplomatic, rally, presser, briefing, testimony, signing, address, interview
   - LLM-NOT-eligible: earnings, nba_broadcast, mlb_broadcast, sports, entertainment
2. `scripts/analyze_event.py` — skip LLM analysis for non-eligible event types
3. NBA/earnings: rely purely on deterministic base rates + arena certainties
4. This also cuts LLM API cost by ~40% (sports/earnings are high volume)

**Files:** `app/event_context.py`, `scripts/analyze_event.py`

---

### P2-C: Log-Odds Signal Fusion (Replace Raw Multiplicative)

**What the research says:** Log-odds aggregation is theoretically optimal when signals
are conditionally independent. Our current multiplicative formula
`p = base * topic_rel * decay * news * buzz * llm * event_llm` compounds percentage
changes, which creates non-linear distortions at extremes.

**Fix:** Convert to log-odds space before combining:
```python
log_odds = logit(base_rate)
log_odds += w_rolling * (logit(rolling_rate) - logit(base_rate))
log_odds += w_llm * llm_log_odds_increment
log_odds += w_news * news_log_odds_increment
p_combined = sigmoid(log_odds)
```

Benefits: properly bounded at 0/1, additive increments are interpretable, easier to
implement double-counting dampening (just reduce w_* for correlated signals).

**Files:** `app/scoring.py` (full formula rewrite), `app/base_rates.py`

---

### P2-B: Reliability Diagrams + Calibration Dashboard Tab

**What the research says:** Calibration is the property that "when the model says 70%,
it's right ~70% of the time." Measured via reliability diagrams (bin predictions by
0-10%, 10-20%, etc., plot actual frequency vs. predicted probability).

**Implementation:**
1. `scripts/plot_reliability.py` — bin 8,712 resolved outcomes by predicted probability,
   compute actual frequency per bin, output data JSON
2. Dashboard **Calibration** sub-tab (under System): reliability diagram as a
   bar chart (predicted vs. actual), Brier decomposition (Reliability + Resolution),
   rolling 30/60/90-day Brier scores
3. Alert if any bin is off by >10% (miscalibration flag)

**Files:** `scripts/plot_reliability.py`, `app/dashboard.py`

---

### P2-C: Learned Ensemble Stacker (Signal Meta-Model)

**What the research says:** Training a meta-model (logistic regression) on base model
outputs can more than double R-squared while cutting noise nearly in half. But requires
50+ resolved outcomes per signal combination.

**Our status:** We have 8,712 resolved outcomes, but only ~178 with full signal tracking
in `outcome_reviews`. Need to get to 300+ tracked outcomes before stacker is reliable.

**Implementation:**
1. Ensure `outcome_reviews` captures full signal decomposition for every BUY card
2. `scripts/train_stacker.py` — logistic regression on [rolling_rate, llm_boost,
   news_pressure, poly_signal, obi_signal, price_velocity] → actual_yes
3. Walk-forward cross-validation on 60/40 train/test temporal split
4. Only deploy if walk-forward BSS > 0.05 (5% improvement over market mid)
5. `app/stacker.py` — hot-reload trained model coefficients from `data/stacker.json`

**Files:** New `scripts/train_stacker.py`, `app/stacker.py`,
expand `app/scoring.py` stacker integration

---

### P2-D: Corpus Expansion — Timestamped Transcripts

**Current gap:** Our 129 transcripts have phrase hit counts but NO timestamps within the
speech. The survival model (P1-A) requires word-position timestamps to know WHERE in the
speech a phrase typically appears.

**Fix:**
1. Modify `data/corpus/<speaker>/` format to include word-position index
2. `scripts/timestamp_corpus.py` — for existing transcripts, estimate timestamps using
   word count / average speaking rate (130 WPM briefings, 110 WPM rallies)
3. Future scrapes via REV pipeline store word positions directly
4. Minimum target: 30 timestamped Leavitt briefings, 50 timestamped Trump events

**Files:** `scripts/timestamp_corpus.py`, `scripts/ingest_corpus.py` (store word positions)

---

### P2-E: Phrase Frequency Trend Tracking (Vocabulary Drift Detection)

**What the research says:** Speakers' vocabulary changes over time. A phrase that was
30% two years ago might be 60% now. Our recency-weighted calibration helps but doesn't
explicitly flag when a phrase's rate is trending strongly up or down.

**Implementation:**
1. `scripts/compute_phrase_trends.py` — for each (speaker, phrase), compute rate over
   last 30/60/90 days, flag phrases where 30-day rate differs from 90-day by >15%
2. `VOCAB_TRENDING_UP` / `VOCAB_TRENDING_DOWN` signals in scoring
3. Dashboard AI Intelligence tab: "Trending phrases" section showing vocabulary shifts

**Files:** `scripts/compute_phrase_trends.py`, `app/scoring.py`, `app/dashboard.py`

---

### P2-F: Pre-Event Corpus Auto-Expansion from Kalshi Outcomes

**Gap:** We have 8,712 resolved outcomes but only 129 transcript files in the corpus.
Every resolved YES outcome IS evidence that the phrase was used at that event type.
We're underusing the outcome data for base rate construction.

**Fix:**
1. `scripts/expand_corpus_from_outcomes.py` — for each resolved YES outcome, create a
   synthetic "observation" in the per-speaker, per-event-type frequency table
2. This gives us 8,712 additional phrase×event_type observations for calibration
3. Particularly valuable for thin speakers (Mamdani, SCOTUS, Congressional hearings)
   where we have few transcripts but many resolved outcomes

**Files:** `scripts/expand_corpus_from_outcomes.py`, `scripts/calibrate_base_rates.py`

---

## 🟢 P3 — Long-Term / Nice-to-Have

---

### P3-A: Hawkes Process for Multi-Mention Count Markets

Only relevant for contracts like "will speaker say X 3+ times." Research-grade but low
practical volume. Defer until we see these market types regularly on Kalshi.

### P3-B: Mobile Dashboard via Tailscale

10-minute setup: install Tailscale on Mac and phone, access `http://100.x.x.x:8777`
from anywhere. Deferred to when live monitoring is established.

### P3-C: WebSocket Real-Time Market Streaming

Sub-second market updates via Kalshi WebSocket API. Currently polling every 30s is
sufficient for pre-event work. Only needed if we pursue intra-event speed trading
(which the research notes is increasingly competed away by bots).

### P3-D: Social Media Firehose

Research explicitly says this disappoints for literal-mention markets. X/Twitter API
costs $5,000/month for Pro tier. Social sentiment scores "lack robust predictive power"
for literal token prediction. Skip indefinitely.

---

## Implementation Order

```
Week 1: P0 — Critical gaps (causing real losses now)
  Day 1-2:  P0-A  Rolling window compound probability
  Day 3:    P0-B  Signal double-counting / event deduplication
  Day 4-5:  P0-C  Walk-forward backtest + Brier Skill Score vs market mid

Week 2: P1 quick wins (each 2-4 hours)
  Day 1:    P1-B  Resolution rules parser + synonym risk
  Day 2:    P1-C  OBI + VAMP signal
  Day 3:    P1-E  Exact string matching audit (no stemming) + Unicode normalization
  Day 4:    P1-G  Cross-platform resolution scope detection
  Day 5:    P1-H  News deduplication (MinHash) + calibrated per-type decay

Week 3: P1 safety layer for live events
  Day 1-2:  P1-D  Negation + attribution/quotation detection
  Day 3-5:  P1-F  Dual-source ASR confirmation (yt-dlp + Whisper local)
            P1-I  Live caption monitoring wired end-to-end

Week 4-5: P1-A Survival model (requires corpus timestamps first)
  Day 1-2:  P2-D  Timestamp corpus (word positions → minute estimates)
  Day 3-5:  P1-A  Survival model + speech structure hazard (intro/body/close)

Week 6+: P2 items in priority order
  P2-B  LLM domain-selective application (skip sports/earnings — saves cost too)
  P2-C  Log-odds fusion
  P2-F  Reliability diagrams + calibration dashboard
  P2-G  Corpus expansion from outcomes
  P2-E  Phrase trend tracking
  P2-H  Ensemble stacker (after 300+ tracked outcomes)
```

---

## Success Metrics

The system is improving if and only if:

| Metric | Current | Target |
|---|---|---|
| Brier Skill Score vs. market mid | Unknown (never measured) | BSS > 0.05 |
| Live win rate (BUY_YES) | ~42% | > 55% |
| Live win rate (BUY_NO) | ~27% | > 45% |
| False BUY_NO on monthly windows | High (today) | < 5% of BUY_NO cards |
| Corpus transcripts | 129 | 200+ (with timestamps) |
| Tracked live outcomes (outcome_reviews) | ~178 | 300+ |
| Walk-forward BSS confidence interval | Unknown | ±0.02 or better |

---

## The Honest Assessment

From the research: *"The market mid is the benchmark every other model must beat.
If your model doesn't improve on 'always predict the market mid,' it has negative value."*

We have never measured our Brier Skill Score against the market mid. **This is the most
important thing to build and measure.** Everything else is secondary until we know
whether we actually have edge.

The second most honest thing the research says: domain expertise consistently outperforms
automated signals. The accounts making thousands are specialists who understand a
speaker's exact vocabulary, monitor 2-3 event types deeply, and use large bets on
high-certainty situations — not systems scoring hundreds of phrases at $10 each.

**Our model is better suited as a filtering tool (find the 3-5 highest-confidence bets
per event) than a breadth scanner (score all 34 phrases and take everything with EV > 0.10).**
The build plan above moves us in that direction.
