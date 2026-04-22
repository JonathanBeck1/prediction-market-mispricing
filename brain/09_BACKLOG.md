# Backlog and Build Plan

Manual-only invariant: all steps must preserve no-order-placement behavior.

## Build Strategy: Local-First

Everything works locally on your Mac with mock data before any integration. OpenClaw and Kalshi API come last.

---

## Session 45 TODO (Next Session — Complete In Order)

### 1. Fix 4 pre-existing test failures — P4 [~1 hour]
These 4 tests in `tests/test_scoring.py` have been failing since session 42's gate hardening.
Fix by updating expected reason codes to match current gate behavior.
- `test_watch_when_gates_fail` — expects `gate_pass=0` for wide-spread BUY_NO, but BUY_NO is now exempt from spread checks
- `test_off_topic_yes_guard_blocks_pre_event_yes` — expects `OFF_TOPIC_GUARD` but gate now emits `OFF_TOPIC`
- `test_pre_event_yes_threshold_filters_weak_yes` — expects `YES_EV_FILTER` but `YES_PRICE_FLOOR_BLOCK` now fires first (floor raised to 0.32 in session 42)
- `test_buy_no_hint_format` — `WEEKLY_WINDOW_NO_BLOCK` or `NO_CONVICTION_FLOOR` now blocks the BUY_NO before exec hint is generated

### 2. Bias map → BUY_NO activation [~2 hours]
The bias map correctly identifies 30 overpriced phrases but doesn't yet aggressively push BUY_NO.
Currently: bias map only replaces the base rate. Problem: EV and conviction gates still filter out many.
Fix: add a dedicated `BIAS_MAP_OVERPRICED` gate that lowers the NO_EV_PREMIUM requirement and NO_CONVICTION_FLOOR threshold when `bias_map.is_overpriced(series, phrase, threshold=0.20)`.
Top targets: `KXTRUMPMENTION/transgender` (+58pp gap), `KXTRUMPMENTION/crypto` (+54pp), `KXTRUMPMENTION/shutdown` (+53pp), `KXSECPRESSMENTION/radical left` (+51pp).
We only have 5 BUY_NO cards right now — with 30 empirically confirmed mispriced phrases, we should have many more.

### 3. Additional transcripts for Carney/Starmer/Homan [~1 hour]
We only have 1 transcript each for Carney and Homan, 3 for Starmer.
- Carney: election victory speech (Mar 2026), tariff response press conferences — use `rev_transcript_cleaner.py` or `scrape_corpus.py`
- Starmer: UK parliament PMQs (multiple sessions) — check hansard.parliament.uk for text
- Homan: CNN/Fox interviews, Senate hearings — Rev or manual HTML save

### 4. Sports broadcast mention markets [~3 hours]
315 open markets (KXNCAAB, KXMLB, KXNBA, KXFIGHT) with zero corpus.
Sports broadcaster language is extremely predictable. Phrases like "Triple Double", "Walk Off", "Transfer", "Recruit" have stable empirical rates across seasons.
Steps:
- Add series→speaker mappings in `kalshi_watcher_live.py` for ncaab, mlb, nba, fight
- Build `config/base_rates_priors.yaml` entries for each sport (phrases + context priors)
- Fetch 5-10 game transcripts per sport from Rev or YouTube auto-captions
- Add to THIN_SPEAKER map in `scoring.py`

### 5. Hazard model for live events [~4 hours]
Current live-event scoring uses a static exponential decay curve for probability as time progresses.
A proper hazard model would use per-phrase empirical hazard rates from corpus transcripts:
- h(t, phrase) = probability density of phrase being said at time t given it hasn't been said yet
- Estimated from corpus: for each phrase, bin events by time-of-first-mention → empirical hazard function
- Replaces `_time_decay_factor()` in `scoring.py` with calibrated hazard rates per phrase
- Expected impact: 5-10pp improvement in live-event EV accuracy

---

## Current Roadmap Status — SYSTEMATIC EDGE V35 LIVE (Session 35)

The deterministic edge pipeline is complete, the LLM layer is fully live, and systematic v35 improvements are implemented.

- [x] Kalshi reliability + coverage hardening (retry/backoff/pagination + explicit coverage reason codes)
- [x] Polymarket V2 matching (event-aware matching, confidence buckets, quality filters)
- [x] Confidence-weighted Poly blending in scoring + dashboard tags
- [x] Wallet-flow ingestion + normalized signal cache
- [x] Bounded wallet alpha feature in scoring + explainable reason tags
- [x] Canonical ops flow (`make event-ready`, `make health-check`) + runbook consolidation
- [x] Validation gates (full regression + live rehearsal report in `brain/11_VALIDATION_REHEARSAL.md`)
- [x] **LLM global signal layer** — `analyze_signals.py` (gpt-5-mini, runs every 45 min)
- [x] **LLM per-event scoring layer** — `analyze_event.py` (gpt-5-mini, runs every 30 min)
- [x] **LLM Markdown instruction architecture** — 6 files in `config/llm/`, editable without code changes
- [x] **Dashboard Scripts tab** — 29 runnable scripts, live terminal output, Stop Engine CTA
- [x] **Session 35 — Systematic Edge v35**: PRE_EVENT_NO_BLOCK, NO_EV_PREMIUM (1.5x), stratified Platt calibration, divergence-based score_confidence, LLM suppress widened, event LLM clamped, topics bug fix, two-pass EV-sorted cap, window settled protection, 4000-char LLM context, 1h outcome feedback loop

**Next priorities (from live data analysis):**
- [ ] Accumulate more live data (50+ LIVE bets) to validate stratified calibration for "live" stratum (only 32 samples currently)
- [ ] Add rolling window Brier score to dashboard for real-time model health
- [ ] Consider adding a PRE_EVENT_YES_LIMIT gate (pre-event YES bets also have questionable edge)
- [ ] Monitor NO_EV_PREMIUM 1.5x — if live WR for BUY_NO improves after gate hardening, may reduce to 1.25x
- [ ] Add event-type-aware base rate blending (diplomatic events should pull from diplomatic sub-corpus)

---

## Phase 1: Local-First Build (Steps 1-11) — COMPLETE

### Step 1-11: All DONE

---

## Phase 2: Integrations (Steps 12-15) — COMPLETE

### Step 12: OpenClaw live transcripts — DONE
### Step 13: OpenClaw X/news signal scraping — SUPERSEDED (see Phase 5, LLM layer)
### Step 14: OpenClaw WhatsApp delivery — DONE
### Step 15: Kalshi API (real market data) — DONE

---

## Phase 3: Cross-Market Intelligence + Dashboard (Steps 16-17) — COMPLETE

### Step 16: Polymarket cross-market integration — DONE
### Step 17: Event-grouped dashboard — DONE

---

## Phase 4: Ops Hardening (Step 18) — COMPLETE

### Step 18: DB snapshot pruning — DONE

- `scripts/prune_snapshots.py` — keeps last N days of snapshots, truncates JSONL if > 50MB, runs VACUUM
- `make prune` Makefile target
- Weekly launchd job: `com.kalshi-edge.prune` (Sunday 3am, logs to `/tmp/kalshi-prune.log`)

---

## Phase 5: Outcome Feedback + Risk Controls (Steps 19-22) — COMPLETE

### Step 19: Outcome tracking MVP — DONE

- `outcome_reviews` table stores decision-time BUY-card snapshots vs resolved outcomes
- `make record-outcomes` inserts settled rows from `data/kalshi_outcomes.json`
- `make report-outcomes` prints realized win-rate and P&L summaries

### Step 20: Guardrail layer — DONE

- Added configurable scorer guardrails:
  - `PRE_EVENT_YES_THRESHOLD`
  - `BLOCK_OFF_TOPIC_YES`
  - `PENNY_PRICE_THRESHOLD`
- Guardrail reason codes added to cards and dashboard tags

### Step 21: Historical scorecards — DONE

- `make backtest` reports by side, confidence bucket, and regime tags
- Gives weekly calibration/selection-quality visibility

### Step 22: Policy tuning preset — DONE

- `make tune-policy` sweeps settled outcomes and writes `config/safe_mode.env`
- Enables reproducible safe-mode live runs

---

## Phase 6: Deterministic External Signals (Non-LLM) — COMPLETE

### Step 23: X pipeline with budget controls — DONE

- `make fetch-x` script implemented with:
  - tracked accounts config (`config/x_watchlist.yaml`)
  - `since_id` dedupe state (`data/x_state.json`)
  - usage telemetry (`data/x_usage.json`)
  - monthly/daily budget caps (`X_MONTHLY_BUDGET_USD`, `X_DAILY_POST_RESOURCE_CAP`)
  - deterministic `x_buzz` updates in `data/signals.yaml`

---

## Phase 6b: Model Accuracy Improvements (Session 26) — COMPLETE

### Recency-weighted calibration — DONE
- `calibrate_base_rates.py --recency-halflife-days 90` — exponential decay on outcome age
- Fixes major political drift: "transgender" 0%→67%, "economy" 61%→24%, "golden dome" 38%→0%
- Run: `python3 scripts/calibrate_base_rates.py --outcome-weight 8.0 --recency-halflife-days 90`

### All-outcome phrase catalog — DONE
- Previously 66% of 7,626 outcomes discarded (phrases not in active catalog)
- Now stored in `general` bucket: 1,468 unique phrases with real empirical rates
- Future markets for any of those phrases get real rates instead of context prior

### Live settlement signal — DONE
- `scripts/fetch_settlements.py` (3-min MaintenanceRunner poll)
- `app/live_settlements.py` + `LiveSettlements` in `ScoringEngine`
- `SETTLED_YES/NO` clamps, `EVENT_ACTIVE +0.04` boost

### Polymarket pool signal fallback — DONE
- `PolymarketPrices.get_pool_signal()` — weak anchor from speaker-week average
- Wired into scorer as `POLY_POOL` fallback
- Improved slug discovery (pagination + title search, loop bug fixed)

**Backtest improvement: Brier 0.2530 → 0.1848, P&L $5,619 → $13,129 on 7,626 outcomes.**

---

## Phase 7: LLM Signal Intelligence (Steps 24-27) — COMPLETE

This is the highest-value remaining work. Replaces manual `signals.yaml` with
automated contextual reasoning about what phrases a speaker will say.

### Step 24: Data ingestion layer (`scripts/fetch_signals.py`) — DONE

- Fetches: WH official RSS (`whitehouse.gov/news/feed/`), WH schedule JSON, Google News RSS (6 query strings), Truth Social posts (`data/truth_social_posts.json`), Google Trends via `pytrends` (13 political keywords)
- Output: `data/signal_context.json` with all sources + active phrase universe (340 phrases)
- `make fetch-signals` Makefile target
- No API key required for base sources; `NEWSAPI_KEY` enables additional AP/Reuters/NYT layer

### Step 25: LLM reasoning layer (`scripts/analyze_signals.py`) — DONE

Two-step architecture:
- **Step 1 (topic extraction)**: LLM reads full context (WH schedule + Truth Social + news + trends) and identifies 5-8 key topics with evidence + likely_phrases. This grounds all phrase reasoning.
- **Step 2 (phrase assessment, 60/batch)**: LLM assesses each phrase against extracted topics, returns per phrase:
  - `boost`: multiplier (1.1–2.0 up, 0.4–0.9 down)
  - `reasoning`: 2-3 sentence explanation citing specific sources
  - `evidence`: direct quote or data point (e.g. "Trump TS: 'Total denuclearization'" or "Iran trends 59/100")
  - `signals_used`: which sources triggered it (truth_social/wh_schedule/google_news/google_trends)
  - `topic`: which extracted key topic this phrase connects to
- `SignalModifiers` carries `llm_boost + llm_reasoning + llm_evidence + llm_topic`
- Scoring wires reasoning into `components["llm_reasoning/evidence/topic"]` → flows to action card
- **Dashboard**: cards with active LLM signal show a green/red insight panel with full reasoning text, key topic label, and direct evidence quote
- Saves full analysis to `data/llm_analysis.json` for inspection
- `make analyze-signals` Makefile target; `make signal-refresh` = fetch + analyze in sequence
- **Requires**: `OPENAI_API_KEY` env var — add to `config/runtime.env`; cost ≈$0.01-0.05/run

### Step 26: Key accounts monitoring — COVERED by D1+D2

- Truth Social posts fed via `data/truth_social_posts.json` → `fetch_signals.py` → `analyze_signals.py`
- Google News RSS covers major reporters and WH announcements

### Step 27: Signal auto-refresh loop — DONE (D3)

- `fetch_signals.py` wired into MaintenanceRunner every 45 min (always runs)
- `analyze_signals.py` wired into MaintenanceRunner every 45 min (`requires_env_var=OPENAI_API_KEY`)
- Together they auto-refresh `signals.yaml` with LLM phrase adjustments every 45 min during live sessions

---

## Phase 8: Live Operations + Validation (Steps 28-32)

### Step 28: Live event rehearsal — DONE

- Completed with canonical flow and strict health gates.
- Evidence recorded in `brain/11_VALIDATION_REHEARSAL.md`.

### Step 29: Live transcript ingestion

- Goal: wire real-time transcript URLs into `TRANSCRIPT_URLS` for during-event phrase detection
- Existing REV pipeline (OpenClaw + BraveAPI + BeautifulSoup4) handles post-event corpus
- For live ingestion, need a URL that updates during the speech:
  - REV live captioning URL (if available)
  - YouTube live stream captions via OpenClaw
  - C-SPAN live transcript
  - WH.gov live feed for briefings
- Set `TRANSCRIPT_URLS` before event, ingestor polls every 30s automatically

### Step 30: Postmortem cadence automation

- Goal: operationalize the existing outcome stack after each event resolve
- Run `make record-outcomes && make report-outcomes && make backtest` on a fixed cadence
- Track drift and flag systematic biases for weekly threshold tuning

### Step 31: Phone dashboard access

- Options evaluated:
  - **Tailscale** (recommended): mesh VPN, access localhost:8777 from phone, free, private, 10-min setup
  - **ngrok**: public HTTPS URL, free tier, password-protect in dashboard
  - **Vercel**: won't work (stateless, no SQLite, no persistent processes)
  - **Railway/Fly.io**: works but requires Postgres migration, ~$5-10/month
- Vercel only viable if architecture migrated to cloud DB + separate scoring server

### Step 32: Historical backtest framework hardening

- Goal: replay past events with stored market snapshots and transcripts
- Measure hypothetical P&L if all BUY cards had been executed
- Identify which edge sources contribute most

---

## Phase 7: LLM Signal Intelligence — COMPLETE (Session 31)

All Phase 7 items are now live.

### D1: WH schedule + news context aggregation — **DONE**
- `scripts/fetch_signals.py` → `data/signal_context.json` (WH RSS, Google News, Truth Social, Google Trends)

### D2: LLM phrase assessment — **DONE**
- `scripts/analyze_signals.py` two-step gpt-5-mini: topic extraction → per-phrase boost assessment
- Historical YES rate calibration: LLM receives historical freq in prompt; hard boost caps by base-rate tier
- `data/llm_analysis.json` stores topics + assessments for the dashboard AI Intelligence tab
- Model configurable via `LLM_SIGNAL_MODEL` env var (currently `gpt-5-mini`)

### D3: Scoring integration — **DONE**
- `llm_boost` multiplier from `signals.yaml` hot-reloaded into `SignalStore` on mtime change
- Reason codes: `LLM_BOOST_HIGH / LLM_BOOST / LLM_SUPPRESS / LLM_SUPPRESS_HIGH`

### Session 31 improvements (logical gate hardening + ROI tuning):
- **`NO_CONVICTION_FLOOR`**: blocks BUY_NO when `p_literal ≥ 0.40` (betting NO on near-50/50 events is incoherent)
- **`MARKET_BEARISH_BLOCK`**: blocks BUY_YES when `yes_ask < 0.12` (market says near-certain NO)
- **EV threshold raised**: 0.06 → 0.10; historical backtest improves to **71.7% win / +20.2% ROI** (3,095 bets)
- **Dashboard AI Intelligence tab**: full topic/reasoning/evidence display, divergence gauge on every card, gate badges
- **LLM backtest script**: `scripts/backtest_llm_impact.py` for simulating boost impact on historical outcomes

### Session 33 upgrades (gpt-5-mini + per-event LLM + Scripts tab):
- **Model upgraded to gpt-5-mini** — `LLM_SIGNAL_MODEL=gpt-5-mini`, `LLM_EVENT_MODEL=gpt-5-mini` in `config/runtime.env`; uses `max_completion_tokens` API param
- **Markdown instruction architecture** — `app/llm_context.py` + 6 files in `config/llm/`: `mission.md`, `event_formats.md`, `trump_patterns.md`, `calibration_guide.md`, `global_signals_guide.md`, `per_event_guide.md`; full LLM context editable without code changes
- **Per-event LLM scoring** — `scripts/analyze_event.py`: classifies event format (diplomatic/rally/presser/etc.), generates per-event phrase multipliers; `app/event_signals.py` `EventSignalStore` hot-reloads from `data/event_signals/`; wired into `ScoringEngine` as `event_llm` multiplier with `EVENT_LLM_BOOST`/`EVENT_LLM_SUPPRESS` reason codes; runs every 30 min
- **Event format classifier** — `app/event_context.py` `classify_event_format()`: 9 formats; feeds per-event LLM prompt and topic relevance scoring
- **Dashboard Scripts tab** — 29 runnable scripts, 7 groups (AI Intelligence / Backtesting / Calibration / Data Fetching / Health & Reporting / Corpus / Engine Control); async job runner `POST /api/run-script` + `GET /api/script-output`; live terminal streaming; stuck-run recovery; data-fetch concurrency guard
- **Stop Engine script** — `scripts/stop_engine.py`: SIGTERM → SIGKILL on all engine processes, dashboard stays running; Engine Control CTA on Scripts tab
- **Dashboard model display fixed** — AI Intelligence tab now shows configured model from env (not stale cached value); staleness warning when analysis > 2h old

---

## Phase 6c: Next Deterministic Edge Improvements

These require NO new data sources — all use data already collected.

### A. Phrase co-occurrence signal — **DONE**
- `scripts/compute_cooccurrence.py` analyzes 8,492 resolved outcomes → `data/phrase_cooccurrence.json`
- 139 trigger phrases, 723 high-confidence pairs (n≥5, conditional rate ≥50%)
- Logic already in `app/live_settlements.py` — when phrase X settles YES in a live event, `LiveSettlements.get_signal()` returns `CooccurrenceBoost` for every correlated phrase
- Scoring: applies `best_boost.p_boost` (up to +0.15) with reason code `COOCCUR_BOOST` + `cooccur_trigger` / `cooccur_rate` in components
- Runs daily via `MaintenanceRunner`; `make compute-cooccurrence` Makefile target
- Top pair: `tariff` → `biden` at 72% (n=100, boost=+0.15)

### B. Truth Social RSS automated feed (HIGH IMPACT, FREE) — **B2 in current roadmap**
- Trump posts on Truth Social 2-6h before speeches with exact phrases he'll repeat
- Free RSS: `https://truthsocial.com/@realDonaldTrump/feed.rss` or equivalent scrape
- Extract capitalized phrases/words, match to Kalshi phrase catalog
- Write to `signals.yaml` as `news_pressure` boost (e.g., 1.5x for exact phrase matches)
- Script: `scripts/fetch_truth_social.py` — runs every 15 min via MaintenanceRunner

### C. Price velocity / smart money detection — **DONE (A3)**
- `scripts/compute_price_velocity.py` — queries `market_snapshots` for 2h/6h/24h price deltas
- Only scores currently open, non-expired markets (from `kalshi_markets.json`)
- Quality gates: past price ≥ 0.35 (not already-low decay), data within 2× of window age
- Output: `data/price_velocity.json`, refreshed every 5 min via `MaintenanceRunner`
- `app/price_velocity.py` — `PriceVelocityCache` provides `VelocitySignal` per market
- Reason codes: `SMART_MONEY_UP` / `SMART_MONEY_DOWN` in action card components
- p_literal adjustment: ±0.025 – ±0.05, scaled by `signal_strength`
- Signal quality improves with 7+ days of continuous runner operation

### D. White House official schedule RSS — **DONE (B1)**
- Sources: `whitehouse.gov/news/feed/` (official categories) + Google News RSS (100 items, schedule previews)
- `scripts/fetch_wh_schedule.py` → `data/wh_schedule.json`; runs every 30 min via `MaintenanceRunner`
- `app/wh_schedule.py` — `WHScheduleCache`:
  - `resolve_event_type(current_type)` — overrides "general" with WH-confirmed type (signing/remarks/briefing/etc.) from title patterns + feed categories
  - `keyword_boost(phrase, hours=6)` — +0.08 if phrase in signing/remarks title, +0.04 otherwise
  - `upcoming_keywords(hours=6)` — returns all topic keywords from recent WH events
  - Reloaded in `ScoringEngine._refresh_outcomes_cache()` when stale (>30 min)
- Reason codes: `WH_CONTEXT` (event_type override), `WH_KEYWORD` (phrase boost)
- Event type detection: 14 regex patterns + 10 WH category mappings (Executive Orders → signing, etc.)

### E. Monthly window already-resolved tracking — **DONE (A1)**
- `app/window_state.py` — `WindowStateCache` consolidates settled phrases from
  `kalshi_outcomes.json` and `live_settlements.json` per event_ticker
- `WindowState.is_yes(phrase)` / `is_no(phrase)`: clamps p_literal to 0.98/0.02 if
  phrase already settled in the current month's window
- `WindowState.pace_signal()`: small ±0.02 additive adjustment based on window settlement pace
- Reason codes: `WINDOW_SETTLED_YES`, `WINDOW_SETTLED_NO`, `WINDOW_ACTIVE_PACE`
- Reloaded in `ScoringEngine._refresh_outcomes_cache()` (every 15 min)

### F. Rolling N-speech hit rate — **DONE (A2)**
- `scripts/compute_rolling_rates.py` — for each `(speaker, phrase)`, computes YES rates
  over last 3, 5, and 10 speeches; writes `data/rolling_hit_rates.json`
- `app/rolling_rates.py` — `RollingRatesCache.blend(speaker, phrase, p_historical)`:
  returns a blended probability if rolling rate differs from historical by ≥ 5%
- Blend weight: n5 preferred (30% rolling / 70% historical); n3 used at 25%; n10 at 20%
- Reason codes: `ROLLING_N3`, `ROLLING_N5`, `ROLLING_N10`
- Runs daily via `MaintenanceRunner`; reloaded on outcomes refresh cycle

---

## Deferred

- Rules-summary parsing (LLM-based ambiguity/clarity scoring)
- TrapIndex / substitution maps
- WebSocket-based market streaming for sub-second updates
- Telegram/Pushover notification channels (WhatsApp is primary)
- X API paid tier ($100/mo) — defer until consistently profitable

---

## Operating Tasks (Ongoing)

1. Corpus growth plan:
   - Bring sparse buckets to >= 10 transcripts (priority: `trump/rally`, `leavitt/briefing`)
2. Calibration cadence:
   - After each corpus update: `make analyze` -> `make calibrate`
3. Pre-event reliability:
   - Keep `config/events.yaml` populated 24-48h ahead of known speeches
4. Polymarket refresh:
   - Run `make fetch-poly` before each trading session
5. Dashboard monitoring:
   - Run `make dashboard` alongside `make run-live` for real-time card visibility
6. Snapshot pruning:
   - Automatic weekly via launchd. Manual: `make prune`
