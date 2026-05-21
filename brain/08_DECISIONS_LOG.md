# Decisions Log (Append-Only)

Manual-only invariant applies to all decisions below.

## 2026-05-21 — OSS polish and runner reconnect safety

- **Decision:** Add GitHub community metadata files (`.github/workflows/ci.yml`, issue forms, PR template, and `CODE_OF_CONDUCT.md`) so public contributions have a clear path and every PR runs compile/test checks.
- **Decision:** Stop documenting hardcoded test counts. The suite changes often; public docs and agent guidance now require `python3 -m pytest -q` to pass rather than naming stale counts.
- **Decision:** Document corpus source handling in `docs/CORPUS.md` instead of linking to a gitignored `data/corpus/README.md`.
- **Decision:** Pass `app=app` into the ingestor `_service_loop()` in `app/runner.py`. Watcher and scorer already had reconnect context; ingestor did not, so DB connection failures in that loop could not use the same targeted reconnect path.
- **Decision:** Preserve the `data/runner.lock` inode in `scripts/watchdog.sh`. The runner lock is `flock`-backed; deleting the file can create a new inode and allow duplicate runners. The script now honors `data/logs/runner.paused` instead of deleting the lock.
- **Verification:** `python3 -m compileall -q app scripts tests` passed. `python3 -m pytest -q` passed with 264 tests and 7 existing `datetime.utcnow()` deprecation warnings.

## 2026-04-07 — Model Rebuild: BayesianScorer replaces ScoringEngine

**Problem**: Old model's Brier score (0.352) worse than market price as predictor (0.238). Multiplicative p_literal formula, Platt scaling, calibration floors, and 20+ gates were compounding errors. Every signal layer (LLM, news, Poly, wallet) degraded performance when added to base rates.

**Decision**: Replace 2,580-line ScoringEngine with 350-line BayesianScorer based on hierarchical Beta-Binomial posteriors and credible-interval-based mispricing detection.

**Evidence**:
- 946 historical bets: old model -$2.21 P&L, new model +$18.86 P&L
- Per-bet: old -$0.002/bet, new +$0.077/bet
- MLB: -$15.83 → +$5.27 (118→28 bets)
- Trump: -$10.63 → +$1.36 (126→58 bets)
- 701 bets correctly skipped had -$21.07 cumulative P&L

**Key design decisions**:
1. Speaker-level hierarchical priors instead of flat 0.45 global prior
2. 90% credible interval must EXCLUDE market price to trigger a bet
3. No LLM, no news, no Poly, no wallet, no Platt, no calibration floors
4. Only 6 structural gates: settled market, cheap NO, expensive NO, Kelly, spread, depth
5. Toggle: `USE_BAYESIAN_SCORER=1` (default on), `=0` for legacy

**Risk**: Lower bet volume (245 vs 946). May miss some profitable opportunities that require real-time signal processing (e.g., phrase confirmed in transcript). Phrase hit detection is preserved.

## 2026-04-05 — Massive LLM Fix: prompt rewrite + sports bypass + signal reset

- **LLM prompt rewrite** (`scripts/analyze_signals.py`): Replaced format-based suppression rules with data-backed speaker profiles. Key insight: Trump says "sleepy joe" at 92% of ALL event types — format does NOT predict his vocabulary. New prompt includes live trading performance data (LLM_BOOST 28% WR) to enforce conservatism.
- **Sports event_llm bypass** (`app/scoring.py`): `event_llm` now forced to 1.0 for sports speakers. Research showed EVENT_LLM_SUPPRESS on sports was net-harmful.
- **Stale signal auto-reset** (`scripts/analyze_signals.py`): `_update_signals()` now resets all phrases NOT in the latest LLM run to `confidence=expired`, `llm_boost=1.0`. Prevents stale boosts from persisting across broken runs.
- **Suppression floor by base rate** (`scripts/analyze_signals.py`): Phrases with hist YES >50% cannot be suppressed below 0.85x; >30% cannot go below 0.70x. Codified the insight that suppressing high-frequency phrases is nearly always wrong.
- **`trump_patterns.md` rewrite**: Replaced incorrect guide with data-backed profiles. Previous guide said "suppress sleepy joe to 0.1x at diplomatic events" — actual data shows 92% YES at signings.
- **Event prompt speaker profiles** (`scripts/analyze_event.py`): Added Trump (format-independent), Leavitt (formal policy), Powell (FOMC only), Mamdani (NYC local) behavioral rules. Replaced generic "suppress insults at diplomatic events."
- **Expired signal handling** (`app/signals.py`): `confidence=expired` forces `llm_boost=1.0` regardless of stored value.

## 2026-04-01 — Model accuracy improvement fixes (7 changes from live-outcome analysis)

- **Calibration floors** (`app/calibration.py`): `_MIN_SAMPLES` 350→200; `_apply_floor()` enforces minimum calibrated probabilities (global 0.20, Trump 0.25, NBA/NCAAB 0.22) to prevent systematic 20-34pp underestimation observed in 394 live outcomes. Per-bucket diagnostics logged at refit.
- **TRUMP_NO_BLOCK** (`app/scoring.py`): All Trump BUY_NO blocked when `p_calibrated >= 0.10`. Trump BUY_NO had 37.9% WR (-$4.87, 29 bets). With calibration floors, nearly all would already be blocked by `NO_CONVICTION_FLOOR`; this is a final backstop.
- **LLM boost neutralized** (`app/signals.py`): All boost caps set to 1.00 (`MODIFIER_MAX`, `_BOOST_CAP_MEDIUM`, `_BOOST_CAP_LOW`). LLM boost was anti-signal on BUY_YES (31.3% WR, -$1.86). LLM suppress unchanged (45-56% WR on YES/NO).
- **Leavitt YES fully blocked** (`app/scoring.py`): Expanded from `yes_ask < 0.45` partial guard to full `LEAVITT_YES_BLOCK`. 20% WR across 10 bets, -$1.73. BUY_NO works (65.4% WR).
- **Window reasons NameError fixed** (`app/scoring.py`): `reasons = ["WINDOW_MARKET"]` was initialized AFTER `reasons.append("BIAS_MAP_RATE")` at line 444 — potential crash. Moved initialization before the bias_map block.
- **WALLET_LOW_CONF_BLOCK unconditional** (`app/scoring.py`): Removed "strong override" exception set. Low-conf wallet is noise regardless of co-occurring signals (25% WR on both BUY_NO and BUY_YES).
- **Calibration diagnostics card** (`app/dashboard.py`): `_query_calibration_buckets()` added; Intelligence tab shows "Model Health" table with actual vs model p and gap (pp) per bucket. `calib_buckets` in API payload.

## 2026-03-30 - `news_story_hashes` + shared `app/story_hash.py` (Sprint C.3)

- Decision: Centralize headline fingerprinting in **`app/story_hash.py`**; **`scripts/fetch_signals.py`** uses it for dedup/`source_story_hash`. **`scripts/fetch_news_signals.py`** writes per-phrase **`news_story_hashes`** (cap 8) from RSS title+description matches. **`SignalModifiers`** loads both lists. **`SAME_STORY_PROVENANCE_TRIM`** fires when pre-dampening news+LLM are hot **and** `_provenance_trim_story_id` is set: **intersection** of LLM + news hashes, else single **source** hash, else single **news** hash. Card **`scores`** in `raw_json` may include **`same_story_provenance_trim`**, **`source_story_hashes`**, **`news_story_hashes`** for dashboard/debug.
- Rationale: Deterministic news pipeline IDs align with fetch_signals where tokenization matches; overlap detection ties RSS hits to LLM `[story:…]` tags when headlines coincide.

## 2026-03-30 - `SAME_STORY_PROVENANCE_TRIM` (Sprint C.2)

- Decision: When **`news_pressure` and `llm_boost` were both above `SIGNAL_CORRELATION_THRESHOLD` (1.1) before double-count dampening** and provenance resolves to a single story id (`source_story_hashes`, `news_story_hashes`, or overlap), apply an additional **0.85× marginal** trim to the weaker arm after Correlation 1’s 0.5× step; emit **`SAME_STORY_PROVENANCE_TRIM`** and `components["same_story_provenance_trim"]`. Same logic on **window markets** using pre-correlation snapshot.
- Rationale: BUILD_PLAN_START C — LLM-proven single-story citation justifies one more de-stack on redundant news+LLM mass without waiting for perfect headline↔news_pressure linkage.

## 2026-03-30 - `source_story_hash` signal provenance (Sprint C.1 MVP)

- Decision: **`data/signal_context.json`** news rows (WH RSS, NewsAPI, Google News after dedup) and **Truth Social** posts carry `source_story_hash` (16 hex chars). Context strings shown to `analyze_signals.py` prefix lines with `[story:<hash>]`; the LLM is instructed to copy tags into `evidence` when citing a headline/post. Parsed hashes are stored per phrase in **`data/signals.yaml`** as `source_story_hashes` and loaded into **`SignalModifiers`** for audit (`score_components["source_story_hashes"]` in `app/scoring.py` when non-empty).
- Rationale: BUILD_PLAN_START Sprint C — same story can drive news + LLM + WH without a stable id; hashes enable dampening review and operator forensics before tighter scorer coupling.

## 2026-03-30 - Phrase co-occurrence canonical writer (Sprint A.3)

- Decision: **`scripts/compute_cooccurrence.py`** (and the maintenance task that runs it) is the only writer of production `data/phrase_cooccurrence.json` in the `pairs[]` schema. **`scripts/calibrate_base_rates.py`** no longer emits co-occurrence JSON (removed legacy `_build_cooccurrence`).
- Rationale: Alternating dict- vs list-shaped files caused schema flip-flop; one writer avoids silent consumer breakage.

## 2026-03-30 - `MENTION_SERIES` alignment (markets, watcher, outcomes)

- Decision: **`KXFEDMENTION` → `powell`** in `scripts/fetch_markets.py` (was `fed`) to match `app/kalshi_watcher_live.py`, `scripts/fetch_outcomes.py`, and Powell corpus/scoring paths. **`KXPRESMENTION` → `trump`** in `fetch_markets.py` and `kalshi_watcher_live.py` (was `auto`) to match `fetch_outcomes.py` / `scripts/fetch_settlements.py` and named-event Trump semantics.
- Rationale: Consistent speaker labels across discovery, live snapshots, and historical outcomes; `scripts/audit_mention_series.py` should report no mismatches.

## 2026-03-30 - Truth Social deduplication policy

- Decision: Near-duplicate posts (Unicode-normalized text, strip wrapping quotes for equality) merge to one entry; keep the later `posted_at`. Documented in runbook; applied to `data/truth_social_posts.json`.
- Rationale: Sprint A.1 acceptance — no duplicate near-identical posts inflating signal counts.

## 2026-03-31 - `dashboard.py` indentation fixes (Phase 0 gate)

- Decision: Repair three broken indent blocks in `app/dashboard.py` that prevented `from app import dashboard` (event-label merge `if/else`, per-speaker BSS `try/except`, NBA `arena_override_count` under `if sport_key == "nba"`).
- Rationale: `health_check.py` and `tests/test_dashboard_coverage.py` import the dashboard module; syntax errors blocked Phase 0 baseline and any CI.

## 2026-03-31 - Canonical execution roadmap `BUILD_PLAN_START.md`

- Decision: Maintain `brain/BUILD_PLAN_START.md` as the **starting** build plan: Phase 0 (baseline + DB health + BSS), then sprints A–E (data spine, calibration hygiene, signal provenance, phrase matching, ops). Older plans (V3 audit, V4 themes) remain reference; START explicitly lists **already landed** items to avoid duplicate work.
- Rationale: V3/V4/STATUS overlap; operators need one ordered checklist with acceptance criteria and links to existing artifacts (`health_check`, `backtest_outcomes`, co-occurrence schema policy).

## 2026-03-30 - SQLite recovery via `.recover`

- Decision: When `edge.db` fails `PRAGMA integrity_check` and dashboard snapshot queries throw `database disk image is malformed`, stop all writers, run `sqlite3 edge.db ".recover"` into a new file, verify `ok`, re-apply WAL pragmas, swap in place, and retain the pre-recovery file under `data/edge.db.pre_recover_*` / `corrupt_swapped_*`.
- Rationale: `REINDEX` was insufficient; `.recover` salvages readable rows and restores a consistent database for the runner and dashboard.

## 2026-03-01 - M0 persistence

- Decision: use SQLite + WAL for local-first persistence.
- Rationale: minimal setup, reliable writes, easy inspection.

## 2026-03-01 - Operational logging

- Decision: append JSONL logs for watcher/transcript/action-card outputs.
- Rationale: low-overhead audit trail and replay-friendly format.

## 2026-03-01 - Mock-first market watcher

- Decision: implement deterministic mock watcher before real Kalshi integration.
- Rationale: enables end-to-end pipeline validation without API dependencies.

## 2026-03-01 - Transcript source abstraction

- Decision: define `TranscriptSource` interface with pluggable adapters.
- Rationale: isolate transcript retrieval complexity and enable fallback chains.

## 2026-03-01 - OpenClaw non-blocking adapter

- Decision: keep OpenClaw command template configurable and optional.
- Rationale: avoid blocking runtime on command specifics while preserving integration path.

## 2026-03-01 - Literal matching in early milestones

- Decision: defer rules-summary parsing; use literal token/phrase matching in M0-M2.
- Rationale: faster reliability path and lower ambiguity.

## 2026-03-01 - Canonical docs overhaul

- Decision: establish `brain/00-10` uppercase canonical docs as source of truth.
- Rationale: align roadmap, operating model, and acceptance criteria in one stable location.

## 2026-03-01 - Cost and invocation policy

- Decision: prioritize code-driven deterministic loops; LLM/agent calls are optional and event-triggered.
- Rationale: predictable runtime cost and fewer moving parts.

## 2026-03-01 - Market-specific phrase mapping

- Decision: add `MARKET_PHRASES` (market_id -> resolution phrases) alongside `SUBJECT_PHRASES` (speaker -> name variants).
- Rationale: markets resolve on literal phrases (e.g., "NATO", "tariff"), not speaker names. Matching speaker names alone produces no signal about market resolution.

## 2026-03-01 - Transcript content-hash dedup

- Decision: hash transcript text (SHA-256 prefix) and skip inserts when identical text already stored for the same source_ref.
- Rationale: prevents duplicate phrase hits and wasted storage when polling the same page repeatedly.

## 2026-03-01 - Three-way action card output

- Decision: every action card outputs BUY_YES, BUY_NO, or WATCH with executable price hint and size cap.
- Rationale: manual trader needs to know what to do (direction), at what price (executable ask), and how much (depth-based cap) in under 10 seconds.

## 2026-03-01 - Context persistence via STATUS.md + Cursor rule

- Decision: maintain `brain/STATUS.md` as session save-state and `.cursor/rules/kalshi-edge.mdc` for auto-injected project context.
- Rationale: multi-session project needs reliable context handoff without re-explaining the project each time.

## 2026-03-02 - Fallback transcript source chain

- Decision: `FallbackTranscriptSource` tries sources in order (directhttp -> openclaw). Order flips if `TRANSCRIPT_SOURCE=openclaw`.
- Rationale: real transcript ingestion must be resilient; if HTTP scrape fails, OpenClaw can try; no single point of failure.

## 2026-03-02 - Incremental transcript diffing

- Decision: track previous text per source_ref. If new text starts with old text, only diff (delta) is phrase-matched. If text is entirely different, full text is matched.
- Rationale: live caption streams grow over time. Re-matching the full document on every poll would produce duplicate phrase hits. Delta-only matching keeps hit counts accurate.

## 2026-03-02 - Test suite as M1 acceptance gate

- Decision: 42 tests covering phrase matching (boundary-safe, false positive rejection, determinism), transcript dedup, incremental diffing, scoring (all three sides), fallback sources.
- Rationale: M1 acceptance criteria require "known fixture inputs produce deterministic expected hits" and "duplicate polling does not duplicate stored hits."

## 2026-03-02 - Pre-event trading as first-class use case

- Decision: the system must generate action cards BEFORE speeches start, not just during them. Events have a `scheduled` state where scoring uses historical base rates.
- Rationale: pre-event is often the best time to enter positions -- prices are less crowded, liquidity is sometimes better, and base rate analysis can identify significant mispricings before the rush.

## 2026-03-02 - Event state machine: scheduled -> live -> ended

- Decision: events have 4 states: `scheduled`, `live`, `ended`, `unknown`. The `scheduled` state is new -- it enables pre-event scoring. Transitions are based on transcript freshness, scheduled start time, and inactivity timeouts.
- Rationale: the binary "hit today / no hit today" model misses the time dimension entirely. Real edge comes from knowing WHEN in the event lifecycle you are.

## 2026-03-02 - Event type conditioning

- Decision: each event has a `type` (rally, briefing, interview, townhall, other) that affects base rate lookups. Different formats have very different phrase distributions.
- Rationale: Trump saying "tariff" at a rally (85% likely) vs an interview (50% likely) are different bets. The scorer needs to know the format.

## 2026-03-02 - Base rates as configurable YAML

- Decision: historical phrase probabilities stored in `config/base_rates.yaml` keyed by (speaker, event_type, phrase). Maintained manually, refined with actual outcomes over time.
- Rationale: base rates are the foundation of pre-event scoring and decay calculations. They must be easy to update without code changes.

## 2026-03-02 - X and news as signal modifiers (not standalone sources)

- Decision: X buzz and news pressure are multiplicative modifiers on base rates, range [0.7, 1.5]. Stored in `data/signals.yaml`. Default to 1.0 (neutral) if missing.
- Rationale: trending topics shift phrase probability, but they're nudges not rewrites. The base rate from historical data is the anchor. X/news can push it up or down by up to 50%.

## 2026-03-02 - Local-first build order

- Decision: build the full brain locally (Steps 1-11) before any integration. OpenClaw and Kalshi API come last (Steps 12-15). Everything testable with mock data, fixture files, and YAML configs.
- Rationale: integrations are a source of instability and debugging friction. Get the logic right first with deterministic inputs, then plug in real data sources.

## 2026-03-02 - WhatsApp delivery via OpenClaw (no Telegram/Pushover)

- Decision: action card notifications will be delivered to WhatsApp through OpenClaw. No need to build Telegram, Pushover, or Discord integrations.
- Rationale: OpenClaw is already linked to WhatsApp. Adding a separate notification service would be redundant complexity.

## 2026-03-02 - Signals YAML as the bridge between manual and automated

- Decision: `data/signals.yaml` is manually edited during local development and automatically populated by OpenClaw during production. The scoring engine reads the same file either way.
- Rationale: decouples the scoring math from the signal source. You can test the full composite model locally by hand-editing signals, then seamlessly switch to automated population later.

## 2026-03-02 - Today's ended events still visible to scorer

- Decision: `get_active_event` returns today's ended events (priority: live > unknown > scheduled > ended-today). Old ended events are excluded.
- Rationale: an ended event should yield p=0.02 (strong NO signal). Without this, scorer falls back to no-event mode (p=0.10), which is a weaker signal. Market convergence to settlement price requires knowing the event actually ended.

## 2026-03-02 - Event transitions run inside scoring cycle

- Decision: `event_detector.run_transitions()` is called at the start of every `scorer.run_once()` cycle.
- Rationale: keeps state machine in sync with real time without needing a separate event loop. Transitions are cheap (single DB scan) and idempotent.

## 2026-03-02 - Test suite expanded to 90 tests (session 5), then 112 (session 6)

- Decision: 112 tests covering all modules. Added: card formatter (10), material-change throttling (8), end-to-end integration lifecycle (4).
- Rationale: local-first build is complete; comprehensive tests are the acceptance gate before integration phase.

## 2026-03-02 - Material-change throttling

- Decision: console output and future WhatsApp notifications are throttled. Only emit when: side flips, EV crosses threshold, speech state changes, EV delta exceeds 0.03, or cooldown (120s) expires. All cards still written to DB/JSONL every cycle.
- Rationale: without throttling, 12 markets * 10s interval = 72 notifications per minute. Users should only be notified when something actionable changes.

## 2026-03-02 - WhatsApp card format: plain text, no markdown

- Decision: `format_card_text()` produces plain text (no markdown, no emoji). Designed for 5-10 second phone reads. Shows: side, market, event context, score breakdown, price/EV/cap, and reason codes.
- Rationale: WhatsApp renders markdown inconsistently. Plain text is reliable, scannable, and works in all contexts.

## 2026-03-02 - Structured payload matches canonical schema

- Decision: action card payloads now include nested `scores` (p_literal, base_rate, time_decay, news_pressure, x_buzz, ev_yes, ev_no) and `liquidity` (yes_ask, no_ask, spread, depth, gates) sub-objects. Plus `time_remaining_sec` and `starts_in_sec` in event context.
- Rationale: aligns with `05_ALERT_SPEC.md` canonical schema. Enables rich formatting for different channels (console, WhatsApp, future dashboard) from same payload.

## 2026-03-02 - Signal reason thresholds: 1.2 / 0.8

- Decision: `NEWS_PRESSURE_HIGH` when > 1.2 (not 1.05), `NEWS_PRESSURE_LOW` when < 0.8. Same for X_BUZZ.
- Rationale: 1.05 was too sensitive -- would flag almost any non-default signal as "high". 1.2/0.8 thresholds mean only truly noteworthy pressure gets flagged as a reason code.

## 2026-03-02 - OpenClaw Browser Relay as primary transcript source

- Decision: `OpenClawTranscriptSource` now uses a 3-step pipeline: `openclaw browser start` -> `openclaw browser open <url>` -> `openclaw browser evaluate --fn "() => document.body.innerText" --json`. Falls back to legacy `OPENCLAW_SCRAPE_CMD` if that env var is set.
- Rationale: Browser Relay is not a single scrape command. It requires browser session management (start), navigation (open), and content extraction (evaluate). The 3-step pipeline maps directly to the CLI interface. Legacy mode preserved for backward compatibility.

## 2026-03-02 - WhatsApp notification via openclaw message send

- Decision: `WhatsAppNotifier` calls `openclaw message send --channel whatsapp --target <target> --message <text>`. Enabled via `WHATSAPP_ENABLED=1` and `WHATSAPP_TARGET`. Only BUY_YES and BUY_NO cards are sent (WATCH suppressed). Gated by material-change throttling.
- Rationale: OpenClaw is already linked to WhatsApp. Direct CLI invocation is simple and testable. Throttling ensures no notification spam.

## 2026-03-02 - X/news signal scraping deferred

- Decision: automated X and news scraping via OpenClaw deferred. Manual `data/signals.yaml` editing continues to work. The Browser Relay pipeline can be used later to scrape X search pages and news headlines.
- Rationale: the signal infrastructure is complete (SignalStore, modifier math, clamping, YAML loading). Automating the scraping adds complexity but doesn't change the scoring math. Ship the working system first, add automation when needed.

## 2026-03-02 - Kalshi API: no auth required for public market data

- Decision: use the public `GET /markets` endpoint (no API key) to fetch bid/ask prices, volume, open interest, and market rules. Auth only needed for order placement (which we never do).
- Rationale: simplifies integration. The public endpoint provides all data needed for scoring: `yes_bid_dollars`, `yes_ask_dollars`, `volume_24h`, `open_interest`, `rules_primary`.

## 2026-03-02 - LiveMarketCatalog replaces hardcoded MOCK_MARKETS

- Decision: `scripts/fetch_markets.py` fetches real mention markets from Kalshi by series ticker, extracts resolution phrases from `rules_primary` using regex, and caches to `data/kalshi_markets.json`. `LiveMarketCatalog` loads the cache at startup. The old `MOCK_MARKETS`/`MARKET_PHRASES` remain for test mode (`KALSHI_MOCK=1`).
- Rationale: dynamic market discovery is essential -- Kalshi adds/removes markets weekly. Static mocks can't track new phrase variants or new markets.

## 2026-03-02 - ScoringEngine receives market_phrases as constructor param

- Decision: `ScoringEngine.market_phrases` is now a required init param (dict[str, list[str]]) instead of importing the global `MARKET_PHRASES`. Runner injects mock or live phrases depending on mode.
- Rationale: decouples scoring from static catalog. Enables live market phrases to flow through without global state. Makes tests explicit about which phrases they're testing.

## 2026-03-02 - LiveKalshiWatcher polls by series ticker

- Decision: `LiveKalshiWatcher.run_once()` iterates all known `MENTION_SERIES` tickers and fetches `GET /markets?series_ticker=X&status=open` for each. 0.3s delay between series to respect rate limits.
- Rationale: Kalshi organizes mention markets by series (e.g., KXTRUMPSAY, KXLEAVITTMENTION). Fetching by series is the most efficient way to get all relevant markets without scanning all 10k+ markets on the platform.

## 2026-03-03 - Corpus is canonical source for base-rate calibration

- Decision: add `scripts/calibrate_base_rates.py` and `make calibrate` to regenerate `config/base_rates.yaml` directly from `data/corpus`.
- Rationale: manual base-rate editing does not scale once transcript ingestion becomes continuous. Calibration must be reproducible and fast after each corpus refresh.

## 2026-03-03 - Calibration safeguards: min-sample gating + smoothing

- Decision: calibration requires minimum sample counts (`min_event_docs`, `min_speaker_docs`) and applies Bayesian smoothing toward `_global_default` using `prior_strength`.
- Rationale: raw rates from sparse samples are unstable and can create overconfident scores. Gating and smoothing reduce noise while preserving directional signal.

## 2026-03-03 - Speaker directory normalization is strict

- Decision: corpus speaker folders must exactly match supported keys (`trump`, `leavitt`, `mamdani`). Non-canonical names are treated as data-quality issues.
- Rationale: the scorer and calibration pipeline join data by speaker key. Typos like `mamdoni` silently drop coverage from model calibration.

## 2026-03-03 - Finalized outcomes are now first-class calibration input

- Decision: ingest finalized mention-market outcomes from Kalshi API into `data/kalshi_outcomes.json` via `scripts/fetch_outcomes.py` (`make fetch-outcomes`).
- Rationale: transcript corpus alone is sparse for some event types and can lag evolving phrase usage. Finalized outcomes provide high-volume historical signal for phrase-level base rates.

## 2026-03-03 - Blended calibration (transcripts + outcomes)

- Decision: `scripts/calibrate_base_rates.py` blends transcript hit rates with finalized outcome rates using configurable `--outcome-weight` (default `1.0`), while keeping min-sample thresholds and Bayesian smoothing.
- Rationale: blended priors are more stable and data-rich than transcript-only priors, especially for low-frequency speakers/event types.

## 2026-03-03 - Explicit live run target

- Decision: add `make run-live` target which forces `KALSHI_MOCK=0`.
- Rationale: removes ambiguity about whether the runner is using mock prices or live Kalshi odds.

## 2026-03-03 - Live scoring defaults to event-focused pre/live windows

- Decision: add `FOCUS_EVENT_MARKETS` (default on for live mode) and `PRE_EVENT_WINDOW_SEC` (default 6h). When enabled, scorer suppresses generic no-event markets and scores only scheduled/live event windows.
- Rationale: actionable edge is strongest pre-event and during-event. Scoring all open markets with no event context creates noisy WATCH/NO_EVENT output and distracts from tradable pre-event setups.

## 2026-03-03 - Auto-seed scheduled events from live market contexts

- Decision: when live mode runs with `FOCUS_EVENT_MARKETS=1` and `config/events.yaml` is empty, runner auto-creates scheduled events from non-general live market contexts in the market cache (grouped by speaker + event_ticker).
- Rationale: pre-event filtering should still work on days with active event-linked markets (e.g., special bilateral meetings) even if manual event scheduling was not populated yet.

## 2026-03-05 - Polymarket as cross-market signal (read-only)

- Decision: integrate Polymarket's public Gamma API to fetch mention market YES prices and blend them into the scoring model. Polymarket is used only for signal generation — we never trade on Polymarket, only on Kalshi.
- Rationale: Polymarket has higher volume and generally more efficient prices for overlapping mention markets. Cross-referencing their prices against Kalshi reveals mispricings. A 30% blend weight gives Polymarket meaningful influence without overriding the model's base-rate analysis.

## 2026-03-05 - Polymarket blend weight: 30%

- Decision: use `p_blended = 0.7 * model_p + 0.3 * poly_yes` as the default probability blend.
- Rationale: Polymarket prices are a strong signal but not infallible — they can be thin on specific mention sub-markets. 30% gives enough weight to flag divergences and shift probabilities without fully deferring to Polymarket's price.

## 2026-03-05 - Polymarket phrase matching iterates all market phrases

- Decision: when looking up a Polymarket price for a Kalshi market, the scorer tries all phrases associated with that market (not just the single phrase from `_market_hit_today`).
- Rationale: Kalshi markets often have multiple resolution phrases (e.g., "tariff", "tariffs"). The primary hit phrase might not match Polymarket's exact wording, but an alternate phrase might. Iterating all phrases maximizes cross-market coverage.

## 2026-03-05 - Dashboard redesign: event-grouped accordion

- Decision: replace flat card list with event-grouped collapsible sections. Each section uses the real Kalshi event title (e.g., "What will Trump say during Inter Miami CF visit?") and contains all cards for that event, sorted by EV.
- Rationale: user feedback was clear — hundreds of flat cards are unreadable. Grouping by event matches how traders think (one event at a time) and makes it easy to find the best bets within each event.

## 2026-03-05 - Dashboard event titles from Kalshi market cache

- Decision: extract event-level titles from `data/kalshi_markets.json` by parsing the `subtitle` and `title` fields of markets sharing the same `event_ticker`. Fall back to ticker-derived labels for older events not in the cache.
- Rationale: Kalshi's event titles are the most accurate description of what each event is about. Generic labels like "Trump Mention Mar 6" don't tell the trader what the event actually is.

## 2026-03-05 - Relaxed liquidity gates as defaults

- Decision: change default `max_spread` from 0.03 to 0.15 and `min_depth` from 250 to 0. Configurable via `MAX_SPREAD`, `MIN_DEPTH` env vars.
- Rationale: mention markets are structurally thin — most have spreads of 5-15¢ and depth under 100 contracts. The original strict gates (3¢ spread, 250 depth) blocked every single card. Relaxed defaults let the system produce actionable signals while still flagging liquidity concerns via reason codes.

## 2026-03-05 - Weekly snapshot pruning via launchd

- Decision: add `scripts/prune_snapshots.py` to delete `market_snapshots` rows older than 3 days, truncate JSONL if > 50MB, and VACUUM the DB. Scheduled weekly via macOS launchd (`com.kalshi-edge.prune`, Sunday 3am). Also available as `make prune`.
- Rationale: `market_snapshots` grows at ~50 MB/day during continuous operation. Only the most recent snapshot per market is used by the scorer. Retaining 3 days is sufficient for debugging while keeping DB size manageable.

## 2026-03-05 - Edge thesis: statistical, not speed-based

- Decision: explicitly document that the system's edge is NOT about being first on a live phrase hit. Real-time BUY YES on phrase detection is weak — Kalshi likely has faster participants at events. The real edge is pre-event base rate mispricing, late-event time decay (NO side), and cross-market Polymarket divergence — all of which persist for hours.
- Rationale: correct prioritization of feature development. Investing in live transcript speed is low-ROI. Investing in better pre-event signal intelligence (LLM layer) is high-ROI because those edges don't require speed.

## 2026-03-05 - LLM signal intelligence layer (planned)

- Decision: replace manual `signals.yaml` editing with automated LLM-driven contextual reasoning. Scrape Truth Social RSS, WH official RSS, NewsAPI headlines, and Google Trends. Feed aggregated context to GPT-4o-mini to produce phrase-level probability adjustments with reasoning.
- Rationale: the current `signals.yaml` is manually edited and goes stale within hours. An LLM can read "Trump posted about lumber tariffs 2h ago" and reason that p(tariff) should be very high — something the stats model cannot do. Cost is negligible (~$1-2/month). This fills the gap between blind statistics and human-level contextual understanding.

## 2026-03-05 - Truth Social as primary Trump signal source

- Decision: Truth Social RSS is the #1 data source for Trump market signal intelligence, ahead of X/Twitter.
- Rationale: Trump posts on Truth Social far more frequently than X. His Truth Social posts directly preview speech content and often repeat the exact phrases he'll use live. RSS scraping is free and reliable. X API costs $100/month and Trump is less active there.

## 2026-03-05 - Free-first data acquisition strategy

- Decision: start with free data sources (Truth Social RSS, WH RSS, NewsAPI free tier, Google Trends via pytrends) before paying for X API. Free sources cover ~80% of signal value.
- Rationale: X API Basic costs $100/month. Don't pay for it until the system is consistently profitable and the free sources have been validated. OpenClaw can attempt X scraping for free as a backup.

## 2026-03-05 - Vercel rejected for dashboard hosting

- Decision: do not deploy dashboard to Vercel. Use Tailscale (mesh VPN) for phone access instead.
- Rationale: Vercel is stateless — no persistent filesystem (SQLite can't live there) and no persistent processes (scoring loops time out). Tailscale gives private phone access to localhost:8777 for free with zero code changes. Cloud hosting (Railway/Fly.io) only makes sense after migrating to Postgres.

## 2026-03-07 - Mar 7 Roundtable postmortem: model lacks event-topic awareness

- Loss: 7/9 bets lost on "Saving College Sports Roundtable". Only Iran hit. Tariff, trillion, soccer, stock market, crypto, mog all lost. Total P&L: -$1.45 per $1.
- Root cause: model uses historical base rates calibrated from ALL Trump events (rallies, briefings, addresses). A college sports roundtable is a completely different context. Tariff (62% historical) was given 97% model confidence but had <5% chance at a sports event. The model had no concept of event topic.
- What actually hit: Freshman, Afford, Scholarship, Transfer, Democrat, Olympic, NIL, NFL, Iran, Golf — mostly college-sports vocabulary.

## 2026-03-07 - Market anchor: blend model toward Kalshi price when divergence is extreme

- Decision: when `abs(model_p - kalshi_yes_ask) >= 0.30`, blend 35% toward the Kalshi market price. Controlled by `MARKET_ANCHOR_WEIGHT` (default 0.35) and `MARKET_ANCHOR_THRESHOLD` (default 0.30).
- Rationale: the Kalshi market IS an information source. When our model says 97% and the market says 28%, the model is almost certainly wrong. The anchor caps overconfidence. Backtested on Mar 7 data: would have prevented the worst losses (tariff, trillion flipped from BUY_YES to BUY_NO).
- Bypass: anchor does not fire when `hit_today=True` (phrase confirmed in transcript).

## 2026-03-07 - Event-topic context analysis (`app/event_context.py`)

- Decision: parse event titles for topic keywords (sports, economy, foreign policy, etc.) and apply a relevance multiplier to base rates. Off-topic phrases get 0.20x, on-topic get 1.20x.
- Rationale: "tariff" has a 62% historical base rate across all Trump events, but a college sports roundtable is not a political rally. Topic relevance dampens unrelated phrases so the model doesn't blindly apply generic rates.
- Backtested on Mar 7: new model produces +$3.31 vs old model -$0.09 (improvement of +$3.40). Win rate goes from 39% to 67%.

## 2026-03-07 - Three scorer bugs fixed (phrase lookup, event seeding, event matching)

- Bug 1: scorer passed empty string as phrase to base_rate lookup when `market_phrases` map didn't have the market. All markets got the 0.30 global default. Fixed by merging catalog phrase map in all modes.
- Bug 2: auto-seeding skipped `event_context="general"` markets. All Trump markets are "general". Fixed by removing the filter.
- Bug 3: scorer used `get_active_event(speaker)` returning ONE event per speaker. Markets from different events got matched to wrong events. Fixed by extracting event_ticker from market_id and matching by ticker.

## 2026-03-07 - Outcome tracking is mandatory before policy changes

- Decision: every resolved market now gets a review row in `outcome_reviews` via `make record-outcomes`, and all policy changes are evaluated with `make report-outcomes` + `make backtest` first.
- Rationale: edge must be validated with realized outcomes and P&L, not anecdotal sessions.

## 2026-03-07 - Guardrails are configurable but deterministic

- Decision: add deterministic selection guardrails in scorer with env controls:
  - `PRE_EVENT_YES_THRESHOLD`
  - `BLOCK_OFF_TOPIC_YES`
  - `PENNY_PRICE_THRESHOLD`
- Rationale: reduce fragile pre-event BUY_YES exposure and penny-line noise while keeping behavior auditable.

## 2026-03-07 - X pipeline ships before LLM, with hard cost caps

- Decision: implement non-LLM X ingestion first (`make fetch-x`) with `since_id` dedupe and budget telemetry/caps. Defer LLM reasoning to final phase.
- Rationale: deterministic pipeline gives low-cost signal uplift and operational reliability before introducing probabilistic LLM behavior.

## 2026-03-01 (Session 33) - LLM model upgraded to gpt-5-mini

- Decision: replace `gpt-4o-mini` with `gpt-5-mini` for both `LLM_SIGNAL_MODEL` and `LLM_EVENT_MODEL` in `config/runtime.env`.
- Rationale: user explicitly requested the most capable available model for true edge. gpt-5-mini significantly improves contextual reasoning quality. API change: must use `max_completion_tokens` not `max_tokens` (breaking API difference fixed in both `analyze_signals.py` and `analyze_event.py`).

## 2026-03-01 (Session 33) - LLM instructions externalized to Markdown files

- Decision: all LLM system prompts are now loaded from `config/llm/*.md` via `app/llm_context.py` (`build_system_prompt()`). Files: `mission.md`, `event_formats.md`, `trump_patterns.md`, `calibration_guide.md`, `global_signals_guide.md`, `per_event_guide.md`.
- Rationale: the user's insight was that the LLM needs rich context to reason well — ground-truth behavioral data on Trump's speech patterns, event-format intelligence, calibration hard rules. Markdown files allow iterating on LLM intelligence without touching Python code. Prompts can be reviewed and improved like documentation.

## 2026-03-01 (Session 33) - Per-event LLM scoring architecture

- Decision: introduce a dedicated per-event LLM scoring pass (`scripts/analyze_event.py`) that generates phrase-level multipliers specific to each event's format (diplomatic/rally/presser/signing/etc.). Results cached in `data/event_signals/<event_id>.json` and hot-reloaded by `app/event_signals.py` `EventSignalStore`. Wired into `ScoringEngine` as `event_llm` multiplier.
- Rationale: root cause of Japan dinner loss (3/8 on BUY_NO) was the global LLM using rally-calibrated phrase boosts for a diplomatic dinner. Diplomatic events have completely different speech patterns — the same phrases that fire at rallies (tariff, trillion, incredible, etc.) are suppressed at formal bilateral meetings. A per-event pass with format classification fixes this systematically.
- Japan dinner lesson: "tariff," "deal," "trade" should be suppressed at diplomatic dinners; "cooperation," "alliance," "security" should be boosted. The global LLM could not know this without event context.

## 2026-03-01 (Session 33) - Event auto-reopen fix in runner.py

- Bug: `event_detector` was marking events as `ended` when no transcripts arrived within `expected_duration_sec + ended_inactive_sec`, even when the Kalshi markets were still active. This caused all bet cards to return to WATCH state mid-event.
- Fix: `_seed_events_from_live_markets()` in `runner.py` now re-opens `ended` or `unknown` events if their corresponding Kalshi markets are still active (close_time in future). This ensures the event lifecycle tracks market activity, not transcript arrival.

## 2026-03-01 (Session 33) - Dashboard Scripts tab

- Decision: add a Scripts tab to the dashboard with 29 runnable scripts organized into 6 groups: AI Intelligence, Backtesting, Calibration, Data Fetching, Health & Reporting, Corpus, plus an Engine Control group with the Stop Engine script.
- Rationale: the project has grown to 30+ scripts and the operator needs a single place to run any of them without touching the terminal. Live terminal output streaming, job status tracking, and stuck-run recovery (10 min timeout, 404 guard on backend restart, poll-error backoff) make it safe to use in production.
- Rate-limit guard: data-fetch scripts (Kalshi API) now show a toast warning and refuse to start if another data-fetch script is already running. This prevents the "too many requests" API error when multiple scripts are launched simultaneously.

## 2026-03-01 (Session 33) - Stop Engine script

- Decision: `scripts/stop_engine.py` sends SIGTERM to all engine processes (runner, watcher, ingestor, scorer, maintenance), waits 5s for graceful shutdown, then escalates to SIGKILL for any still-alive processes. The dashboard process is intentionally excluded so the operator can still see output.
- Rationale: operators need a safe, one-click way to stop the engine without going to the terminal. The previous approach (manual `pkill`) was error-prone and easy to forget.

## 2026-03-23 (Session 35) - Systematic edge improvements (v35)

- Decision: Implemented 4-tier systematic improvement plan based on deep analysis of 178 live outcome_reviews. Evidence base:
  - PRE_EVENT bets: 18% WR, -$14.37 PnL on 142 bets (catastrophic)
  - LIVE bets: 53% WR, +$1.06 PnL on 32 bets (model works)
  - BUY_NO vs market consensus: market right 73% — huge weakness
  - BUY_YES vs market consensus: model right 67% — genuine edge
  - SCORE_CONF_HIGH: 17% WR (inversely predictive — was double-counting)
  - LLM boost: 30% WR (hurts). LLM suppress: 100% WR (helps)
  - Platt calibration: p_literal too bearish by 8-20% in 0.2-0.6 range
- Changes:
  **TIER 1 (Highest Impact):**
  1. `PRE_EVENT_NO_BLOCK` — block BUY_NO when PRE_EVENT in reason_codes UNLESS p_calibrated < 0.15. Pre-event BUY_NO had 17% WR (worse than random). Now only very rare phrases (p<15%) allowed as pre-event BUY_NO.
  2. `NO_EV_PREMIUM = 1.5` — BUY_NO requires 50% more EV than BUY_YES to trigger. Addresses asymmetric NO weakness.
  3. Stratified Platt calibration — separate (a,b) parameters for pre_event vs live vs default. Pre-event fitter: a=1.20, b=0.72 (corrects bearish bias). Live: a=0.34, b=-0.88 (compresses toward center). Walk-forward Brier validation logged on each refit.
  4. `pre_event_yes_threshold` raised from 0.03 to 0.08 — stronger signal required for pre-event YES bets.
  **TIER 2 (Fix Broken Signals):**
  5. `score_confidence` fixed — replaced `source_quality` (double-counting poly/wallet data) with `divergence_quality` (model-market disagreement). New formula: 0.30 spread + 0.25 depth + 0.25 divergence + 0.20 topic.
  6. `MODIFIER_MIN` lowered 0.7 → 0.3, `MODIFIER_MAX` lowered 1.5 → 1.3 in signals.py. Asymmetric: allow stronger suppress, tighter boost cap.
  7. Event signals clamped at read time: EVENT_LLM_MIN=0.10, EVENT_LLM_MAX=1.80.
  8. Fixed analyze_event.py topics bug — multi-batch events were writing `topics: []` because loop var was checked at write time.
  **TIER 3 (Structural):**
  9. Two-pass EV-sorted EVENT_BET_CAP — cap now keeps highest-EV bets, not alphabetically-first. Used `_pending_payloads` list approach.
  10. Window settled is now final — when WINDOW_SETTLED_YES/NO detected, skip poly blend, wallet shift, source agreement, velocity signal. Prevents stale data from diluting a settled p.
  11. Increased Step 2 LLM context from 1500 → 4000 chars in analyze_signals.py.
  12. Updated config/llm/mission.md and calibration_guide.md with live data evidence (suppress=100% WR, boost=30% WR). Lowered boost caps.
  13. `record_outcomes` interval reduced 6h → 1h for faster Platt recalibration.
  **TIER 4 (Cleanup):**
  14. Removed unused `_decide_side` method.
  15. `market_veto_margin` field now documented as legacy (still present for backward compat).
  16. `p_pre_anchor` renamed to `p_pre_calibration` in components.
  17. `STALE_AFTER_SEC` removed from event_signals.py (replaced by EVENT_LLM_MIN/MAX).
  18. `_is_material_change` now accepts optional `effective_ev_threshold` parameter for accurate threshold-crossing detection.
- Impact: simulated on 178 live bets — 60 pass (was 178), 42% WR (was 24%), P&L $-0.28 (was $-13.41). +$13.13 P&L improvement.
- Backtest: 4,178 bets, 68.4% WR, +$7,031 P&L, +16.8% ROI (stable).

## 2026-03-23 (Session 34) - Gate hardening for live-data edge

- Decision: Added 6 new scoring gates and tightened 1 existing gate based on analysis of 178 live outcome_reviews:
  1. **SETTLED_MARKET_BLOCK** — force WATCH when yes_ask ≥ 0.95 (phrase already settled YES) or no_ask ≥ 0.95 (settled NO). Blocked 43 bets (all losses).
  2. **MARKET_BULLISH_BLOCK** — block BUY_NO when yes_ask ≥ 0.70 AND p_calibrated ≥ 0.20. The p_cal ≥ 0.20 exemption preserves strong-NO-conviction edge. Blocked 16 bets (14 were losses).
  3. **MARKET_DISAGREE_NO** — block BUY_NO when yes_ask > p_calibrated + 0.25 AND p_calibrated ≥ 0.20. Catches cases where model is barely NO (p=0.35) but market is firmly YES (ask=$0.65).
  4. **THIN_SPEAKER** — block bets on speakers with < 100 resolved historical outcomes when score_confidence < 0.65. Prevents poorly-calibrated bets on starmer, hochul, etc.
  5. **EVENT_BET_CAP** — max 10 BUY cards per event_ticker. Prevents 20+ low-quality bets on a single event from overwhelming the portfolio.
  6. **NO_CONVICTION_FLOOR** tightened from p_calibrated ≥ 0.40 to ≥ 0.35. Catches more ambiguous BUY_NO bets in the "coin flip" zone (p=0.35-0.40).
  7. Removed RISK_REWARD_BAD gate (cost/payout ratio > 4:1) — simulation showed it blocked 3 winners and 0 losers.
- Impact: 178 bets → 67 bets (62% filtered), win rate 24% → 36%, PnL -$13.41 → -$6.17 (+$7.24 saved).
- Historical backtest unchanged: 4,178 bets, 68.4% WR, +$7,031 P&L, +16.8% ROI.
- Rationale: Live outcome review revealed systematic failures: (a) betting against already-settled window markets (KXTRUMPSAY-26MAR23 at $1.00 YES), (b) no protection against strong market bullish consensus, (c) high volume of low-conviction bets diluting ROI, (d) speakers with insufficient calibration data. Mar 19-20 data (after LLM improvements) showed much better natural performance (46-61% WR), confirming the LLM + event-aware scoring is working — the gates needed to catch the edge cases.

## 2026-03-23 (Session 36) - LLM Coverage Expansion + Gate Hardening v2

- Decision: 7 improvements targeting (a) LLM coverage gap (93% of live bets had no LLM signal), (b) thin-book bets, (c) residual risky BUY_NO cards on near-settled markets:

  **GATE FIXES:**
  1. `SETTLED_MARKET_BLOCK` threshold lowered 0.95→0.85. Dry-run revealed 39 BUY_NO cards against 80-94¢ YES markets on ended events — old 0.95 threshold missed this band.
  2. New `ENDED_EVENT_BULLISH_BLOCK`: when EVENT_ENDED + yes_ask ≥ 0.70 → WATCH. Prevents model's end-of-event p_literal=0.02 from generating BUY_NO against markets that have already priced in the event result.
  3. `min_depth = 0.0 → 50.0` hard floor. 122/178 live bets had LOW_DEPTH with only 20% WR (barely above random). Activating the existing depth gate eliminates this thin-book noise category.

  **LLM PIPELINE:**
  4. MaintenanceRunner intervals tightened: fetch_signals 45→25min, analyze_signals 45→30min, analyze_event 30→20min. Root cause of 93% signal-free bets was stale/missing runs, not logic errors.
  5. Transcript context in LLM: `fetch_signals.py` now loads 3 most recent corpus transcripts (speaker's OWN WORDS) into `signal_context.json`. Tier-1 signal in the hierarchy above news headlines.
  6. Per-phrase signal history feedback loop: `fetch_signals.py` queries `outcome_reviews` for historical LLM boost/suppress accuracy per phrase; surfaces "oil: boost 2 bets, 0% WR" directly in LLM context so it self-calibrates.

  **LLM QUALITY:**
  7. 4-step reasoning enforced in `config/llm/mission.md`: FORMAT CHECK → BASE RATE CHECK → DIRECT EVIDENCE TEST → CONFIDENCE CHECK before every multiplier. Low-confidence boosts capped at 1.10x, uncertainty collapses to conservative end.
  8. LLM confidence range: `analyze_signals.py` parses new fields (`boost_low`, `boost_high`, `confidence`, `direct_evidence`); low/medium confidence → uses `boost_low`; stores `llm_confidence` + `llm_direct_ev` in signals.yaml.
  9. Dashboard: staleness warning banner when signals.yaml > 60min old; health bar shows signals.yaml freshness, boost/suppress counts, direct-evidence phrase count, high-confidence count.

- Evidence base: 166/178 live bets (93%) had no LLM signal. Bets WITH LLM: 42% WR. Bets WITHOUT: 22% WR. Gap = +20pp WR. Closing coverage gap is the single highest-leverage improvement available.
- Backtest unchanged: 4,178 bets, 68.4% WR, +$7,031 P&L, +16.8% ROI.

## 2026-03-25 (Session 41) - Rolling Window Event-Count + News Dedup + Poly Scope Guard

### P0-3: Rolling Window — Event-Count Exponent
- **Decision**: Replace the time-fraction exponent in `p = 1-(1-p_per_event)^N` with an event-count estimate.
- **Root cause**: `remaining_window_fraction` returned 0.16 for "5 days left in a 31-day month". That 0.16 was used as N directly, making the compound probability far too low (as if only 16% of one speech remained). The correct N for a briefing series with 5/7 briefings/day over 5 days is ~3.57.
- **Fix**: Added `events_per_day(series_ticker)` to `market_family.py` with calibrated rates per series; `remaining_events_in_window()` converts calendar days → event count. `ScoringEngine._remaining_events_n()` uses this, falling back to legacy fraction for unknown series.
- **Rates chosen**: KXSECPRESSMENTION/KXLEAVITTMENTION = 5/7 (Mon-Fri); KXTRUMPSAYMONTH = 2.5/7 (2-3x/week); weekly series = 1/7; NBA = 1.3/day.
- **Impact**: Fixes systematic undervaluation of remaining-window probability in the final week of monthly contracts. E.g., 5 days left: old p≈p_full^0.16, new p≈1-(1-p_full)^3.57.

### P1-7: News Deduplication — Jaccard Token Fingerprint
- **Decision**: Deduplicate news items before passing to LLM using 55% token-Jaccard similarity.
- **Root cause**: 8 different outlets covering the same executive-order signing appeared as 8 independent items in `signal_context.json`. The LLM treated this as "8 sources agree" → inflated `llm_boost`. In reality it's one data point.
- **Fix**: `_dedup_news_items()` in `fetch_signals.py` removes near-duplicate titles, keeps highest-authority source, and attaches `dedup_cluster_size` so LLM can see "this story was covered by 8 outlets" as context without having 8 redundant entries.
- **Authority order**: WH Official > NewsAPI > Google News (items merged in this order before dedup pass).

### P1-9: Polymarket Resolution Scope Guard
- **Decision**: Downgrade Polymarket confidence to 15% when the Poly contract timeframe mismatches the Kalshi market type.
- **Root cause**: Polymarket's "Will Trump say X in March?" (~40-60% YES reflecting a full month) was being used verbatim to price a single Leavitt briefing market (~10-20%). Caused BUY_YES signals when the single-event probability was low.
- **Fix**: After retrieving `PolySignal`, check `poly_signal.timeframe` vs `timeframe_hint` (monthly vs event). On mismatch: `poly_confidence *= 0.15`, append `POLY_SCOPE_MISMATCH` reason code.
- **Pairs correctly**: monthly-Poly → monthly-Kalshi (fine); event-Poly → event-Kalshi (fine); monthly-Poly → single-event-Kalshi (15% penalty).

### S45-1: BIAS_MAP_OVERPRICED Gate — Bypass Key Gates for Empirically Confirmed Overpriced Phrases
- **Decision**: Add a dedicated `BIAS_MAP_OVERPRICED` flag that bypasses `NO_PRICE_RANGE_BLOCK`, `MARKET_BULLISH_BLOCK`, and `NO_CONVICTION_FLOOR` when `bias_map.is_overpriced(series, phrase, gap>=0.20)`. Also sets `NO_EV_PREMIUM=1.0` (no 1.5x surcharge) for these phrases.
- **Root cause**: The 17 confirmed overpriced phrases (KXTRUMPMENTION/crypto, transgender, trillion, etc.) were generating 0 BUY_NO cards because general-pattern gates (NO_PRICE_RANGE_BLOCK: trump ceiling 0.42, MARKET_BULLISH_BLOCK: yes_ask≥0.70) fired before the bias evidence could help. These gates were calibrated on the average bet, but the bias_map entries are outliers with 10-81 resolved outcomes proving the market is wrong by 18-50pp.
- **Reasoning**: The NO_PRICE_RANGE_BLOCK says "BUY_NO at yes_ask>0.42 loses money on average for Trump markets" (27% WR). But `crypto` at 53¢ with empirical_rate=10% is precisely the exception case — 10 outcomes say the market is wrong by 43pp. Empirical ground truth > statistical averages.
- **Ceiling preserved**: SETTLED_MARKET_BLOCK (yes_ask≥0.85) still fires — if the market is at 85¢+, it may have already resolved.
- **Weekly window excluded**: KXTRUMPSAY not relaxed for WEEKLY_WINDOW_NO_BLOCK (7-day compound probability remains a real constraint regardless of bias).

### S45-2: BiasMapCache JSON Key Bug Fix
- **Decision**: Fix `BiasMapCache._maybe_reload()` to load from `"overpriced"+"underpriced"` keys when `"all"` is absent.
- **Root cause**: The cache called `data.get("all", [])` but old `bias_map.json` format used separate `"overpriced"` and `"underpriced"` lists with no `"all"` key → 0 entries loaded → `BIAS_MAP_RATE` never fired despite correct data.
- **Also**: Reduced `_RELOAD_SEC` from 3600s to 300s so maintenance-script regenerations propagate within 5 minutes.

### S45-3: Runner Watchdog Heartbeat Fix
- **Decision**: Touch `data/logs/runner.local.log` after every service loop iteration in `_service_loop()`.
- **Root cause**: The launchd `com.kalshi-edge.watchdog` script checks `runner.local.log` modification time. Since the app never wrote this file, the watchdog always saw the runner as a "zombie" and killed it every 30 seconds → DB lock contention + frequent crash loops.

### S45-4: compute_bias_map.py — Merge-with-Previous Strategy
- **Decision**: Add `_merge_with_previous()` to preserve historical overpriced entries when a maintenance run has fewer outcomes or no live prices for some phrases.
- **Root cause**: Maintenance runner regenerated `bias_map.json` with only 20 KXTRUMPMENTIONB entries (no live price data → all neutral bias). Overwrote 204-entry historical dataset including 19 confirmed overpriced phrases. New merge logic: new run's entries always win; old entries with `live_ask != None` and `bias in (overpriced, underpriced)` are preserved if not covered by new run.

## Session 46 — Sports Broadcast Coverage + Bias Map Rebuild (2026-03-27)

### Decision: Series price aliases for compute_bias_map.py
- **Problem**: `KXTRUMPMENTIONB` (574 outcomes) and `KXPRESMENTION` (331 outcomes) have historical resolution data but no live market prices. `KXTRUMPMENTION` (active series) has live prices but 0 outcomes. `compute_bias_map.py` was finding 0 overpriced entries because the series didn't match.
- **Solution**: Added `SERIES_PRICE_ALIASES` dict in `compute_bias_map.py`. When no direct price match exists, look up the aliased live series. Output uses `canonical_series = live_series` so the scorer can find entries by `KXTRUMPMENTION` ticker.
- **Result**: 20 overpriced entries generated with correct keys. BIAS_MAP_OVERPRICED gate now fires correctly for KXTRUMPMENTION markets.

### Decision: Unblock sports broadcast mention markets
- **Previous state**: `KXNBA`, `KXNCAAB`, `KXFIGHT` were in `_BLOCKED_SERIES_PREFIXES` in `kalshi_api.py` (preventing catalog loading). `KXMLB` was not blocked in kalshi_api.py but was blocked in fetch_markets.py. All 4 had no priors.
- **Solution**: Removed sports series from all block lists. Added priors for `mlb`, `mma`, `ncaab`, `nba` speakers in `config/base_rates_priors.yaml`. Added `KXMLBMENTION`→`mlb` and `KXFIGHTMENTION`→`mma` to `kalshi_watcher_live.py` and `fetch_markets.py` speaker maps. Added to `THIN_SPEAKER` map with threshold bypass. Added to `fetch_outcomes.py` for future outcome tracking.
- **Observed behavior**: Wide bid-ask spreads mean most sports markets get `WIDE_SPREAD` + negative ev_no → WATCH. MMA (Chiesa vs Price fight, Mar 28) has better spreads and generates BUY_NO (ev_n=0.345 on "train"). This is correct — only trade when the edge survives the spread.
- **Not done**: Sports corpus transcripts. The priors are statistical guesses; real outcomes will calibrate them once 30+ resolutions accumulate. `compute_bias_map.py` will naturally pick up sports overpriced phrases once there's enough data.

## Session 49 — Dashboard Performance tab per-speaker layout (2026-03-27)

- **Decision**: Render Performance as **separate `spk-section` blocks per speaker** (same visual language as Markets), each with speaker-scoped rolling windows, YES/NO split, recent activity, and phrase breakdown — not one combined leaderboard.
- **Rationale**: Many speakers make a single sorted list hard to scan; parity with Markets grouping reduces cognitive load.
- **Implementation**: `_query_outcomes()` enriches `by_speaker` with `rolling`, `recent`, and `by_side`; `_API_SCHEMA_VERSION` bumped to 4. Global portfolio summary (hero + one “Portfolio” card) retained for totals.

### Session 49 (b) — Full speaker grid + per-speaker vocabulary (2026-03-27)
- **Decision**: **`PERFORMANCE_SPEAKER_ORDER`** defines every mention-market speaker the product tracks; outcomes payload includes **`canonical_speakers`** and **placeholder metrics** for speakers with no journal rows. **`phrase_trends.by_speaker`** supplies 30d/90d tables inside each speaker block; remove global Performance vocab/portfolio sections. **Speaker rank** uses compact horizontal chips (not tall stacked tabs). Main **nav tabs** use horizontal scroll + tighter padding.
- **API**: `_API_SCHEMA_VERSION` = **5**.

---

## Session 50 — Gate Hardening (2026-03-28)

**Context**: Live outcome analysis of 153 resolved bets showed the full scoring pipeline (BSS −0.56) was underperforming the base rate model alone (walk-forward BSS +0.129). Reason-code breakdown identified 8 signals with negative live WR that together accounted for the entire P&L deficit.

**Decisions made:**

1. **`NO_CONVICTION_FLOOR` raised 0.40 → 0.20**: Calibration shows p_literal=0.0–0.1 bets have 35% actual YES rate. The model's "rare phrase" signal is unreliable — we are not confident enough at p=0.1–0.4 to bet NO. Only truly rare phrases (p < 0.20) qualify for BUY_NO.

2. **`MARKET_BULLISH_BLOCK` threshold lowered 0.70 → 0.65**: yes_ask 0.7–0.8 had 8–37% WR on BUY_NO. Market consensus at 65¢+ is reliable; fighting it without strong NO conviction is losing bet. `_bias_overpriced` exception removed (see #3).

3. **`BIAS_MAP_OVERPRICED` gate exceptions stripped**: This flag previously bypassed `MARKET_BULLISH_BLOCK`, `NO_CONVICTION_FLOOR`, and `NO_EV_PREMIUM`. Live data: 14.3% WR (−$2.79) on bets using these exceptions. The bias map was built in early 2026 and market pricing has adapted; the map is now stale. Keep tag for monitoring; rebuild from last-90d data before re-enabling exceptions.

4. **`WALLET_LOW_CONF_BLOCK` (new gate)**: When `WALLET_LOW_CONF` is the only non-base signal on a bet, block it. Live: 22.2% WR (−$3.22). Low-confidence wallet flow without corroborating signals is pure noise.

5. **`POLY_HIGHER_VETO` (new gate)**: When Polymarket prices YES 15¢+ above our model with ≥40% confidence, veto BUY_NO. `POLY_HIGHER` had 30% WR (−$7.95); `POLY_CONF_HIGH` had 14.3% WR (−$2.98). These were the two worst signals in the stack. Polymarket smart money is right when bullish — we should not be betting against it.

**Result (gate simulation on 179 historical bets)**:
- WR: 51.4% → 57.0% (+5.6pp)
- P&L: −$3.05 → +$4.15 (+$7.20)
- Bets blocked: 44 of 179 (25%)
- 240 tests passing, no regressions

**What's still a concern**:
- `CROSS_MARKET_ARB` 18.2% WR and `SOURCE_SINGLE` 16.7% WR not yet addressed (small n)
- Bias map exceptions still disabled; now rebuilt from 15k outcomes — can re-enable when enough new live data validates the fresh map
- LLM boost signals (43% WR overall) still underperforming on political bets — per-event signal quality is the main lever

## 2026-03-29 — Session 51: Sports LLM Domain Fix + Bias Map Rebuild + Signal Confidence Caps

**Context**: Analysis of 179 live outcomes identified that 9 BUY_NO losses (−$4.39) on sports phrases were caused by global political `signals.yaml` LLM boosts/suppresses being applied incorrectly to NBA/MLB/NCAAB phrases. Additionally, the bias map only had 13 overpriced entries (limited data). Signal confidence was uniform (no cap difference between high/low confidence entries).

**Changes**:

1. **`_SPORTS_SPEAKERS` domain skip** (`app/scoring.py`): `nba`, `mlb`, `ncaab`, `mma`, `nfl`, `nhl`, `earnings` speakers now skip the global LLM multiplier in both `_compute_p_literal` and `_compute_window_p_literal`. Per-event signals from `analyze_event.py` still apply. Rationale: global political news LLM (signals.yaml) has no predictive power for sports broadcast phrases. The `grand slam` suppress (0.5x) was artificially pushing p_literal to 0.04 → triggering BUY_NO → losing.

2. **Bias map rebuilt from 15,109 outcomes** (`scripts/compute_bias_map.py`): Now includes KXMLBMENTION (176 outcomes), KXNBAMENTION (3,782), KXNCAABMENTION (2,006), KXFIGHTMENTION (417). Result: 276 phrases analyzed (N≥10), 13 overpriced, 4 underpriced. Key new entries: MLB phrases (`grand slam`, `bases loaded`, `triple`) have empirical rate ~0.53 → NO_CONVICTION_FLOOR (p<0.20) now naturally blocks BUY_NO on these. NBA `jordan` emp=56.8% vs market 87¢ (overpriced); NBA `mvp` emp=62.2% vs market 42¢ (underpriced, correct signal flip to BUY_YES).

3. **Confidence-aware LLM boost caps** (`app/signals.py`): Changed from uniform 1.30 cap to: LOW→1.05, MEDIUM→1.15, HIGH→1.30. Suppresses unchanged (floor 0.30). Rationale: low/medium confidence LLM boosts add noise close to random chance; restricting them limits false conviction without impacting the genuine high-confidence signals.

**Result**: 240 tests passing. Expected future improvement: ~9 fewer sports losses per cycle, better base-rate calibration for all sports phrases via bias map.

## 2026-03-29 — Session 52: Permanent DB Corruption Fix

**Root Cause Analysis** (3 compounding causes confirmed from watchdog.log):

1. **`action_cards` table bloat** — scorer wrote one row per market per cycle regardless of side change. 72k+ rows/hour → DB grew to 2.5-4.5GB → WAL blocked, couldn't checkpoint → lock contention.
2. **`synchronous=NORMAL` in WAL mode** — a process crash *during a checkpoint* (when WAL frames are written back to the main DB file) could partially write B-tree pages. Confirmed by "Tree X page Y: Rowid out of order" corruption pattern.
3. **Watchdog `rm -f runner.lock` race** — watchdog deleted `data/runner.lock` immediately after killing the old runner, then launched a new runner. Python cleanup handlers on the old process still had the original inode open. New runner opened a NEW inode → got `fcntl.flock` immediately (uncontested) → two processes with DB open → concurrent writes → B-tree corruption.

**Fixes**:
- `db.py`: `synchronous=FULL` (fsync after every WAL write AND checkpoint), `wal_autocheckpoint=500` (flush every 2MB), `busy_timeout=30s`
- `runner.py`: `_acquire_lock()` retries for 30s — new runner waits for OS to release old process's file handles
- `watchdog.sh`: NEVER `rm runner.lock`; 10s sleep post-kill; rate-limit 1 restart/60s; WAL checkpoint before start; fix grace period for newly-started runners
- `scoring.py`: `_last_recorded_side` dedup — only INSERT to action_cards on side change or BUY_YES/BUY_NO (100× write reduction: ~72k/hr → ~4/market startup)
- `maintenance.py`: PASSIVE WAL checkpoint after each DB-writing task (prune, archive, record_outcomes, fetch_nba_schedule)

**Verification**: DB 447MB → 144MB after VACUUM. WAL stays at 44-50KB. 240 tests passing.

## 2026-03-29 — Session 53: BIAS_MAP_UNDERPRICED Gate Exceptions

**Problem**: Three KXTRUMPSAY phrases with strong empirical BUY_YES edge were blocked:
- `rigged election` emp=71%, ask=20¢, gap=+49¢, n=21 → blocked by `YES_PRICE_FLOOR_BLOCK` (0.32 floor)
- `communist` emp=46%, ask=7¢, no_ask=93%, n=17 → blocked by `SETTLED_MARKET_BLOCK` (no_ask ≥ 0.85)
- `epstein` emp=41%, ask=6¢, n=25 → blocked by YES_PRICE_FLOOR_BLOCK + LLM_SUPPRESS_HIGH reducing p from ~40% to 12.9%, below the 10¢ EV threshold

**Decision: Add `is_underpriced()` to BiasMapCache + bypass gates + empirical p-floor**

Criteria for bypass (very selective — must satisfy ALL):
- `bias_magnitude ≥ 0.30` (30+ cents structural underpricing)
- `n ≥ 15` (at least 15 historical resolution outcomes)
- Currently only 3 phrases qualify: `KXTRUMPSAY/{rigged election, communist, epstein}`

Gates bypassed for `_bias_underpriced` phrases:
1. `YES_PRICE_FLOOR_BLOCK` — low ask reflects chronic market-maker mispricing, not phrase rareness
2. `SETTLED_MARKET_BLOCK` (BUY_YES side, no_ask ≥ 0.85) — market pricing NO 93% is wrong per 17+ outcomes
3. `MARKET_BEARISH_BLOCK` (< 6¢) — same rationale

**Empirical p-floor** (the main fix for epstein): When `_bias_underpriced = True` and
`p_calibrated < emp_rate * time_decay * news_pressure`, override p_calibrated with the
empirical floor. Tags `P_FLOOR_APPLIED`. This prevents LLM_SUPPRESS from killing the signal
when we have 25 historical outcomes proving the market is structurally wrong.

**Result after fix**:
- `rigged election`: **BUY_YES**, ev=+0.381
- `communist`: **BUY_YES**, ev=+0.296 (P_FLOOR_APPLIED)
- `epstein`: **BUY_YES**, ev=+0.297 (P_FLOOR_APPLIED)
- 240 tests passing, no regressions

## 2026-03-29 — Session 53: CROSS_MARKET_ARB_BLOCK + Resolution Guard

### CROSS_MARKET_ARB_BLOCK
Investigated CROSS_MARKET_ARB signal (Polymarket prices YES 10+ cents above Kalshi):
- BUY_NO with CROSS_MARKET_ARB: 0% WR (0/4 bets), -$1.28 — fighting both Kalshi AND Polymarket
- BUY_YES with CROSS_MARKET_ARB: 28.6% WR (2/7 bets) — below break-even

**Decision**: Block BUY_NO unconditionally when `CROSS_MARKET_ARB` is in reason_codes.
New gate: `CROSS_MARKET_ARB_BLOCK` in `app/scoring.py` (added before POLY_HIGHER_VETO).

### Resolution Guard for BIAS_MAP_UNDERPRICED
Bug discovered: BIAS_MAP_UNDERPRICED exceptions were firing on ended NCAAB games
(yes_ask=1-2¢, no_ask=1.0). These games resolved NO but the bias map flags them as
"underpriced" based on the ~50% historical empirical rate across ALL games.

**Root cause**: KXNCAABMENTION/{buzzer, all american, recruit, schedule} have mag=0.37-0.52 
and n≥15 in the bias map. After game ends without the phrase, market drops to 1-2¢ ask, 
no_ask=1.0. Our new bypass treated this as "chronically cheap market" instead of "resolved NO."

**Fix**: Added `no_ask < 0.98` condition to ALL four BIAS_MAP_UNDERPRICED exceptions:
- `SETTLED_MARKET_BLOCK` bypass
- `YES_PRICE_FLOOR_BLOCK` bypass
- `MARKET_BEARISH_BLOCK` bypass
- `P_FLOOR_APPLIED` empirical override

Logic: no_ask >= 0.98 means market has resolved/near-resolved at NO. Never override this.
KXTRUMPSAY phrases (communist/epstein: no_ask=0.95, rigged election: ~0.80-0.97 pre-window)
are unaffected. NCAAB ended games (no_ask=1.0) are correctly blocked.

## Session 58 — NBA_THIN_BOOK_BLOCK gate

**Decision**: Add `NBA_THIN_BOOK_BLOCK` gate in `app/scoring.py` to block `BUY_NO` for NBA markets when `depth_no < 50` and spread is tight (≤ 0.05).

**Evidence**:
- 24 NBA BUY_NO bets with LOW_DEPTH + tight spread: -$5.12 total, -$0.213/bet avg
- Same condition for MLB (7 bets): +$2.05, +$0.293/bet — profitable, do not block
- Same condition for NCAAB (27 bets): +$2.10, +$0.078/bet — profitable, do not block

**Why NBA is different**: NBA market makers quote tight spreads on thin books due to the speed of NBA games and automated quoting. The price looks reliable but isn't backed by real size.

**Net simulated P&L impact**: +$5.12 on 296 bets (overall -$3.64 → +$1.48)

## Session 62 — Trump BUY_NO ceiling tightened 0.42 → 0.35

**Decision**: Lower Trump-specific `NO_PRICE_RANGE_BLOCK` ceiling in `app/scoring.py` from `yes_ask > 0.42` to `yes_ask > 0.35`.

**Evidence (historical outcome_reviews)**:
- `<35¢` bucket: 4 bets, 100% WR, +$1.19 ← profitable sweet spot
- `35–42¢` bucket: 6 bets, 33% WR, −$2.03 ← losing even at SCORE_CONF_HIGH
- `42–50¢` bucket: 5 bets, 40% WR, −$1.28 ← already blocked by old ceiling
- `50¢+` bucket: 12 bets, 17% WR, −$2.19 ← market is correct

**Why 35¢ is the right cut**: The 35–42¢ band showed consistent losses even when filtering to SCORE_CONF_HIGH bets only. The market price in this range is already efficient for Trump — his base rates are extremely well-publicized and Kalshi prices them accurately. Below 35¢, we have a small-but-real edge (rare phrases priced too high by 10+ points).

**KXTRUMPMENTION multi-event block rejected**: Considered blocking KXTRUMPMENTION BUY_NO on contracts closing >7 days out. Simulation showed this would block 5 bets with 80% WR and +$0.43 P&L — the ceiling cut is sufficient, per-event KXTRUMPMENTION bets at <35¢ remain profitable.

**Net P&L impact**: Ceiling cut recovers −$2.78 on 10 newly blocked bets (40% WR). Does not block the 1 remaining profitable bucket.

## Session 64 — 4 New Scoring Gates + Sports Model Analysis

**Decision 1: ROLLING_POLY_CONFLICT gate** (`app/scoring.py`)
Block BUY_NO when both `ROLLING_N5` (rolling rate used) AND `POLY_HIGHER` or `POLY_DIVERGE` are in reason_codes.

**Evidence**: ROLLING_N5 + POLY BUY_NO → 29 bets, 66% phrase-said rate, −$4.95 (−$0.171/bet).
ROLLING_N5 without Poly → 9 bets, 33% phrase-said, +$0.60 — profitable, not blocked.
**Rationale**: When rolling compression pushes p_literal down AND Poly simultaneously prices YES at 50%+, Poly has fresher information. The rolling rate is likely stale. The combination is the signal, not rolling alone.

**Decision 2: POLY_HIGHER_VETO threshold lowered 0.40 → 0.25**
Low-confidence Poly HIGHER+DIVERGE still loses −$0.024/bet across 34 bets. Broader coverage without false positives since the extra condition (`poly_yes > model + 0.15`) prevents spurious blocks.

**Decision 3: NBA_LOW_PRICE_BLOCK** — Block NBA BUY_NO when `yes_ask < 0.38`.
17 historical bets, −$1.61 (−$0.095/bet). NBA base rates (104 outcomes, ~20 calibrated phrases) are too thin to override efficient market pricing below 38¢.

**Decision 4: LEAVITT_LOW_YES_BLOCK** — Block Leavitt BUY_YES when `yes_ask < 0.45`.
7 historical bets, −$1.29 (−$0.184/bet). Short unpredictable briefings; phrase probability signals insufficient at low prices.

**Sports scoring assessment**: NBA/MLB/NCAAB/MMA skip global LLM signals correctly. Base rates rely on 18–46 manually-curated phrases + 14–20 auto-calibrated. Unknown phrases fall back to `_global_default=0.48` — uninformed. Profitable sports bets are structural arena/venue certainties (BIAS_MAP_RATE), not probabilistic model predictions. The model cannot reliably improve sports scoring without 300+ resolved outcomes per phrase. Current sample: 104 NBA, 100 NCAAB, 13 MLB.

**Net combined P&L impact of all 4 gates**: ~+$8–10 saved across future bets in the problem patterns identified.

**Sports pipeline expansion**: Built `extract_mlb_certainties.py` and `extract_ncaab_certainties.py` mirroring NBA's architecture. MLB injects ballpark name p_overrides (~90%) for 25 ballparks (Oracle Park, Dodger Stadium, etc.) + 15 universal phrase floors. NCAAB injects 15 empirical phrase floors from 100 resolved outcomes (Transfer=70%, All American=72%, Airball=68%, Alley-oop=5%). Both hooked into maintenance loop every 30 min. 

**Rationale**: Sports scoring was essentially blind — unknown phrases fell back to `_global_default=0.48` (coin flip). The profitable sports bets were structural certainties (ballpark names) that the model wasn't capturing. This expansion gives sports the same systematic advantage NBA already had while remaining conservative on probabilistic phrases where data is thin (104 NBA, 100 NCAAB, 13 MLB resolved outcomes vs 5600+ for Trump).

## Session 65 — 1M Context Major Model Improvements

**Decision 1: BIAS_MAP_OVERPRICED Gate Reactivation** (`app/scoring.py`)  
Restored aggressive BUY_NO logic for empirically confirmed overpriced phrases, with improved targeting vs the old 14.3% WR implementation.

**Changes**:
1. `_effective_no_premium = 1.0 if _bias_overpriced else NO_EV_PREMIUM` — reduces EV premium from 1.5x to 1.0x
2. `_price_range_exception = _bias_overpriced` — bypasses NO_PRICE_RANGE_BLOCK ceiling
3. `_conviction_threshold = 0.35 if _bias_overpriced else 0.20` — raises NO_CONVICTION_FLOOR from 20% to 35%

**Evidence**: 30 phrases with ≥20¢ systematic overpricing in fresh bias map. Top examples: KXSECPRESSMENTION/israel (+42¢), KXTRUMPMENTIONB/hottest (+39¢), KXNBAMENTION/jordan (+35¢). Live verification: jordan BUY_NO now generates at 75¢ (above normal ceiling) with `BIAS_MAP_OVERPRICED` tag.

**Decision 2: Empirical Hazard Model Implementation** (`app/phrase_hazard.py` + `scripts/compute_hazard_rates.py`)
Replaced static exponential time-decay with phrase-specific empirical hazard rates from corpus analysis.

**Architecture**: 
- `compute_hazard_rates.py` analyzes first-mention timing across 318 transcripts, builds 10-bucket hazard functions
- `PhraseHazardCache` provides `compute_survival_probability()` — phrase-specific decay calculation
- Integrated into `ScoringEngine` at line 678: `decay = phrase_hazard.compute_survival_probability()` 
- Fallback to static decay when hazard data unavailable
- `HAZARD_MODEL` reason code when empirical rates differ from static formula

**Evidence**: 348 phrases with ≥3 events computed. Example: trump.briefing.biden shows early-mention pattern (high hazard 0.11 at 50-60% elapsed) vs trump.rally.biden shows late-mention pattern. Live result: 874 cards tagged `HAZARD_MODEL` in 20 minutes.

**Decision 3: BUY_YES Price Floor Raised 32¢ → 35¢** (`app/scoring.py`)
**Evidence**: Recent 30d BUY_YES performance analysis showed 25-35¢ range: 20 bets, 30% WR, -$0.23. Compared to 35-45¢ range: 9 bets, 55.6% WR, +$1.50. The 32-35¢ gap was a systematic money-loser.

**Change**: `if tentative_side == "BUY_YES" and yes_ask < 0.35` (was 0.32) in `YES_PRICE_FLOOR_BLOCK`.

**Expected impact**: Eliminate 20-bet losing pattern while preserving profitable 35¢+ range. Live result: BUY_YES volume +38 cards (520 vs 482).

## Session 65 — BUILD_PLAN_V4 Phase 1+2: Critical Model Calibration Overhaul

**Root Cause Analysis**: Live BSS = -0.3646 despite walk-forward BSS = +0.1330. Analysis of 374 recent outcomes revealed systematic underestimation across ALL speakers — model predictions 13-34% vs actual 28-56%.

**Phase 1.1: Speaker-Stratified Global Defaults** (`app/base_rates.py` + `config/base_rates.yaml`)
**Evidence**: Massive calibration gaps by speaker:
- NBA: Model 14.4% vs Actual 47.6% (+33.2pp gap) 
- Auto: Model 13.5% vs Actual 42.5% (+29.0pp gap)
- NCAAB: Model 15.6% vs Actual 43.0% (+27.4pp gap)  
- Trump: Model 34.1% vs Actual 56.0% (+21.9pp gap)

**Implementation**: Added speaker-specific `_context_base.general` values:
- `nba._context_base.general: 0.65` (was 0.60)
- `ncaab._context_base.general: 0.58` (was 0.46)  
- `auto._context_base.general: 0.55` (was 0.44)
- `trump._context_base.general: 0.55` (was 0.50)

**Phase 1.2: Platt Calibration Sample Size Fix** (`app/calibration.py`)
**Evidence**: 374 outcomes < 400 minimum → system using identity calibration (no bias correction)
**Change**: `_MIN_SAMPLES` 400 → 200, added sample size warnings, linear correction for 150-199 samples
**Result**: Calibration now active with parameters a=0.2144 b=0.0914, providing +28pp upward correction for low predictions

**Phase 1.3: Event-Type Stratified Calibration** (`app/calibration.py` + `app/scoring.py`)  
**Evidence**: Sports_broadcast 63.6% YES vs sports_other 43.4% YES — different event types need separate calibrators
**Implementation**: Added event-type strata: `sports_broadcast`, `briefing`, `announcement`, `sports_other` with fallback chain to speaker×timing strata
**Result**: 1/4 event strata fitted with sufficient samples

**Phase 2.1: Enhanced Signal Double-Counting** (`app/scoring.py`)
**Evidence**: Brain noted "news × llm × event_llm all fire on same headline" causing compound effects
**Improvements**:
1. News + LLM correlation detection: when both > 1.1, dampen weaker by 50%
2. LLM + event_LLM agreement capping: combined effect capped at 2.0×  
3. Total signal capping: prevent extreme over-boosting above 2.5×
4. Applied to both single-event and window market scoring paths
5. `SIGNAL_CORRELATION_CAPPED` reason code for audit trail

**Expected BSS Impact**: -0.36 → +0.10+ as corrected base rates and calibration fix systematic underestimation. Sports accuracy should improve dramatically (NBA predictions now start at 65% vs 14.4% before).

## Session 65 (continued) — BUILD_PLAN_V4 Phase 3 + P2.2

**Observed improvement**: Walk-forward BSS improved +0.1330 → +0.1611 after Phase 1+2 implementations. Confirms base rate corrections are having the intended effect.

**Phase 3.1: Pre-event vs Live Strategy Split**
Pre-event bets use historical base rates only — no live signal to confirm direction.
`LIVE_BUY_YES_ASK_FLOOR = 0.40`: live BUY_YES requires yes_ask ≥ 40¢ (data: <40¢ has marginal edge at best).
`PRE_EVENT_EV_FILTER` reason code when pre-event threshold blocks a standard-threshold bet.

**Phase 3.2: Confidence-Based Early Exit**
When `score_confidence < 0.40` and no high-signal reason codes present, skip full gate stack → WATCH immediately with `LOW_CONF_EARLY_EXIT` reason code. High-signal overrides: BIAS_MAP_RATE, PHRASE_HIT, CAUSAL_OVERRIDE, SCORE_CONF_HIGH, BIAS_MAP_UNDERPRICED/OVERPRICED.
Rationale: Low-confidence bets with no structural signal source rarely produce edge. Skipping 17 gates saves computation and reduces noise.

**Phase 3.3: Rolling Rate Staleness Detection**
`RollingRate.is_stale(max_age_days=21)`: auto-fallback to historical base rate when rolling data is >21 days old. Prevents stale rolling rate signals from pushing BUY_NO into the ROLLING_POLY_CONFLICT pattern (-$4.95 historical cost). Rolling rates computed from corpus which updates when new transcripts are added — if no new transcripts for 3 weeks, rates reflect outdated speech patterns.

**Phase 2.2: LLM Confidence Gating**
Evidence: LLM_BOOST: 50% WR, $0.88 P&L (24 bets) — marginal. LLM_SUPPRESS: 56.2% WR, $1.21 P&L (233 bets) — works well.
Fix: Tighten boost caps asymmetrically:
- LOW confidence: 1.01× (was 1.05) — essentially neutral
- MEDIUM confidence: 1.08× (was 1.15) — modest boost only
- HIGH confidence: 1.30× unchanged — trust high-confidence analysis
- SUPPRESS: 0.30 floor unchanged — 100% WR, never dampen
Net: LLM boosts no longer risk over-inflating p_literal on uncertain signals.

## Session 65 (Phase 4) — Regime Detection + 4 New Gates

**P4.1 regime analysis** (60-day outcome data, 373 bets):

**Gate 1: POLY_HIGHER_YES_BLOCK** — Block BUY_YES when POLY_HIGHER tagged.
Evidence: 14 bets, 21.4% WR, -$1.96. When Poly already prices YES above Kalshi, the crowd agrees with YES — our edge is gone. We would be chasing a move that has already happened.

**Gate 2: WALLET_LOW_CONF_BLOCK unconditional** — Block any bet when WALLET_LOW_CONF present.
Previous logic blocked only when "only signal." Evidence: 24 bets (12 NO + 12 YES), 25% WR both sides, -$3.47 total. Low-confidence wallet noise contaminates decisions regardless of other signals. Extended to unconditional block.

**Gate 3: THIN_BOOK_CONF_BLOCK** — Block BUY_NO when CONF_HIGH + tight spread + thin book (all non-NBA speakers).
Evidence: 23 bets, 39.1% WR, -$1.06. NBA already blocked via NBA_THIN_BOOK_BLOCK. Even with SCORE_CONF_HIGH, thin books with tight spreads reflect automated market makers — the apparent edge evaporates at execution.

**Regime detection script** (scripts/detect_regimes.py) now runs daily.
Scans 17+ single-code and combo patterns for WR < 45% over 10+ bets. Writes data/regime_alerts.json.

**P4.3: Threshold optimization** (scripts/optimize_thresholds.py).
Grid search over 400 combinations (EV x Kelly x ConfH x NoPremium). Key finding: current thresholds near-optimal. The improvement from "optimal" parameters comes from selectivity (163 vs 373 bets), not fundamental threshold changes. Runs weekly.

**Truth Social**: Cannot be automated — manual-only process. Excluded from BUILD_PLAN_V4.

**Total estimated savings from Phase 4 regime gates**: ~$6.49 P&L on the 49 bets identified in 60-day window.
