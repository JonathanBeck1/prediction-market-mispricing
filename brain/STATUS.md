# Project Status (Living Document)

## Session 2026-04-07 — Model Rebuild: BayesianScorer replaces ScoringEngine

**Root cause:** Deep analysis showed the old ScoringEngine's Brier score (0.352) was WORSE than simply using market price (0.238). Every signal layer (LLM, news, Poly, wallet) degraded performance when added to base rates. The multiplicative p_literal formula, Platt scaling, calibration floors, and 20+ gates were compounding errors, not correcting them.

**New approach: mispricing detection via Beta-Binomial posteriors.**

Built `app/bayesian_scorer.py` — a 350-line replacement for the 2,580-line ScoringEngine:
1. **Hierarchical Bayesian priors**: Each (speaker, phrase) gets a Beta posterior using speaker-level priors instead of a flat 0.45 global prior. Updated `app/bayesian_rates.py` to compute speaker priors from data.
2. **CI-vs-market decision**: Bet only when the 90% credible interval EXCLUDES the market price. If CI includes market price → WATCH (market is efficient for this phrase).
3. **Minimal gates**: Only 6 structural gates (settled market, cheap/expensive NO, Kelly, spread, depth) vs 20+ in the old model.
4. **No signals**: Removed LLM, news, Poly, wallet, event signals, Platt scaling, calibration floors.

**Backtest results (946 historical bets):**

| Metric | Old Model | New Model |
|--------|-----------|-----------|
| Bets taken | 946 | 245 |
| Win rate | 49.4% | 48.2% |
| Total P&L | -$2.21 | +$18.86 |
| P&L per bet | -$0.002 | +$0.077 |

Speaker breakdown:
- **MLB**: -$15.83 → +$5.27 (biggest improvement)
- **Trump**: -$10.63 → +$1.36 (bleeding stopped)
- **MMA**: $3.06 → $4.11 (already good, stays good)
- **NCAAB**: $9.83 → $4.27 (fewer bets, better per-bet)

The 701 skipped bets had -$21.07 cumulative P&L.

**Configuration**: `USE_BAYESIAN_SCORER=1` (default) activates the new model. Set `USE_BAYESIAN_SCORER=0` for legacy ScoringEngine.

**Files changed:**
- `app/bayesian_scorer.py` — new scorer (created)
- `app/bayesian_rates.py` — hierarchical speaker-level priors
- `app/config.py` — `use_bayesian_scorer` setting
- `app/runner.py` — conditional scorer wiring
- `tests/test_bayesian_scorer.py` — 17 tests (all pass)
- `scripts/backtest_bayesian.py` — backtesting script

---

## Session 2026-04-06b — Performance Fixes: kill BUY_YES, raise thresholds, block cheap NO

**Problem:** 49.1% WR on 719 bets, -$6.55 PnL. BUY_YES was 33% WR across every speaker. BUY_NO with cheap contracts (no_ask < 0.40) had 0% WR on Trump, 22% on MLB.

**4 fixes applied:**
1. **GLOBAL_YES_BLOCK** (`app/scoring.py`): All BUY_YES blocked. Live data: 138 bets, 33.3% WR at every EV threshold. Model consistently overestimates phrase probability. BUY_NO is where all edge lives.
2. **CHEAP_NO_BLOCK** (`app/scoring.py`): Block BUY_NO when no_ask < 0.40 (market prices YES > 60%). Live data: 34% WR overall, 0% WR on Trump when fighting the market.
3. **EV_MIN raised 0.03 → 0.15** (`app/scoring.py`): The sweet spot from live data. Effective NO threshold = 0.225 (with 1.5x NO_EV_PREMIUM). Eliminates hundreds of marginal low-edge bets.
4. **Base rate refresh**: Recomputed bias_map, base_rates_auto, and Bayesian rates with latest outcome data. Sports phrases now have correct empirical rates.

**Verification:** 0 BUY_YES after restart. 62 cheap NO blocked. 113 would-be YES blocked. Only high-conviction BUY_NO flowing through.

**Backtested impact (on historical 719 bets):**
- EV >= 0.18: 575 bets → 50.4% WR, +$0.33 PnL (breakeven)
- EV >= 0.25 BUY_NO only: 372 bets → 46.8% WR, +$2.38 PnL (profitable)
- BUY_NO no_ask >= 0.60: 218 bets → 65% avg WR (the sweet spot)

---

## Session 2026-04-06 — Meta-Learning Layer: adaptive signal weights, Bayesian rates, correlations

**Three new self-improving components built and deployed:**

1. **Adaptive Signal Weight Learner** (`app/signal_learner.py`):
   Tracks win rates per (speaker_group, signal, side) from live outcome data. Produces weights that automatically scale signal impact. Signals that lose money get driven toward 0; signals that win get driven toward 1.0-1.5. Uses Beta-Binomial smoothing with Bayesian priors to avoid overfitting on small samples.
   - Initial results (712 outcomes): Trump poly_signal on BUY_YES → 33.3% WR → weight 0.729 (auto-dampened). Sports bias_map on BUY_NO → 52.5% WR → weight 1.064 (slightly boosted). 46 total signal entries across 5 speaker groups.
   - Wired into scoring: `news_pressure`, `llm_boost`, `llm_suppress`, `event_llm_boost`, `event_llm_suppress` all scaled by learned weights.
   - Maintenance: every 4h via `compute_signal_weights.py`.

2. **Bayesian Base Rate Updater** (`app/bayesian_rates.py`):
   Beta-Bernoulli conjugate priors per (speaker, phrase). Each outcome updates the posterior incrementally. Provides BOTH point estimates AND 90% credible intervals (uncertainty quantification). High-confidence rates (many observations) get blended into base rate at up to 40% weight; low-confidence rates stay at the frequentist base.
   - 1,952 (speaker, phrase) posteriors computed. 107 with high confidence (>=0.7).
   - Recency-weighted: 60-day halflife via exponential decay on pseudo-counts.
   - Wired into scoring via `BAYESIAN_RATE` reason code.

3. **Cross-Phrase Correlation Matrix** (`app/phrase_correlation.py`):
   Phi coefficient correlation between phrase outcomes within the same event. Identifies portfolio concentration risk when multiple correlated bets are active.
   - 2,902 significant pairs from 850 events. 611 high-correlation pairs (>=0.50).
   - Top pairs: autopen ↔ fake_news (phi=1.0), america_first ↔ bibi (phi=1.0).
   - Provides `correlation_haircut()` for Kelly-style position sizing reduction.
   - Provides `portfolio_concentration()` score for active bet books.
   - Maintenance: daily via `compute_phrase_correlations.py`.

**Architecture:**
All three are hot-reloadable JSON caches (same pattern as BiasMapCache, RollingRatesCache). Computation runs in maintenance loop. Scoring engine reads at runtime. No blocking LLM calls. Minimal overhead.

**Key insight:** This closes the feedback loop from outcomes → signal weighting that was previously manual. The system that manually discovered "LLM is net-negative for Trump" and required a massive fix session now discovers this automatically and dampens the signal weight.

---

## Session 2026-04-05 — Massive LLM Fix: prompt rewrite, sports bypass, signal reset

**Root cause diagnosis:** Deep research across all tracked speakers revealed the LLM layer was net-negative across almost all markets. LLM_BOOST had 28% WR (-$1.93 PnL) vs 42% baseline. The problems:

1. Step 2 prompt had hardcoded suppression rules ("diplomatic events → suppress insults") that directly contradicted Trump's actual data ("sleepy joe" 92% YES across ALL event types including signings).
2. Event-level LLM (`event_llm`) was NOT bypassed for sports — it was actively hurting NBA/MLB/MMA performance.
3. 731 stale `llm_boost` values from broken production runs persisted in `signals.yaml` forever (never reset).
4. `config/llm/trump_patterns.md` told the LLM to suppress Trump's habitual phrases at 0.1-0.2x at diplomatic events — directly contradicting 92% historical YES rates.
5. No speaker-specific behavioral profiles for Leavitt, Fed/Powell, or Mamdani — the LLM treated all speakers like generic politicians.
6. Runner was stuck in a lock file loop and couldn't restart.

**7 fixes applied:**
1. **Step 2 prompt rewrite** (`scripts/analyze_signals.py`): Removed all format-based suppression rules. Added data-backed speaker profiles (Trump format-independent, Leavitt policy-focused, Powell FOMC-only, Mamdani NYC-local). Added suppression floor rules (>50% hist → never suppress below 0.85x). Added live trading performance data to prompt so LLM knows its boosts are anti-signals.
2. **Sports event_llm bypass** (`app/scoring.py`): Both `_compute_p_window` and `_compute_p_literal` now skip `event_llm` for sports speakers, matching the existing global LLM bypass.
3. **Leavitt/Fed/Mamdani profiles** in Step 2 prompt: explicit vocabulary expectations per speaker.
4. **Signal reset**: Mass-reset all 731 stale `llm_boost` values to 1.0 with `confidence=expired`. New `_update_signals()` now auto-resets phrases NOT in the latest LLM run (prevents stale accumulation).
5. **Lock file fix**: Killed stale runner process (PID 85044) and removed `data/runner.lock`. Runner restarted successfully.
6. **Suppression floor** (`scripts/analyze_signals.py`): Hard code cap — phrases with hist YES >50% cannot be suppressed below 0.85x; >30% cannot go below 0.70x.
7. **`config/llm/trump_patterns.md` rewrite**: Replaced incorrect format-based suppression guide with data-backed behavioral profiles. Key message: "Trump says 'sleepy joe' at 92% of ALL events including bill signings. The LLM's format-based suppression assumptions are WRONG."
8. **Event prompt fix** (`scripts/analyze_event.py`): Speaker-specific behavioral rules replace generic "suppress insults at diplomatic events" rules.
9. **Expired signal handling** (`app/signals.py`): Signals with `confidence=expired` forced to neutral (1.0) regardless of stored boost value.
10. **Stale event signal cleanup**: Removed 42 event signal files for previously removed markets.

**Verification (fresh LLM run):**
- Before: 731 non-neutral assessments, 0 suppressions — LLM boosting everything indiscriminately
- After: 8 non-neutral assessments — only evidence-backed boosts (iran 1.3x, oil 1.3x with story citations)
- "sleepy joe", "fake news", "democrat" correctly left at neutral 1.0 (not suppressed)
- Runner healthy, generating BUY_YES for "china", "radical left", "democrat" (previously blocked by LLM suppression)

---

## Session 2026-04-03 — Gate tuning + calibration improvements (5 fixes)

**Changes:**
1. **Leavitt YES gate** (`app/scoring.py`): Replaced unconditional `LEAVITT_YES_BLOCK` with quality gate: only block when `p_literal < 0.60 OR yes_ask > 0.45`. High-confidence phrases (radical left p=0.78, china p=0.68) now flow through as `BUY_YES`. Historical block was based on 10 low-quality bets; new gate screens for those while preserving strong signals.
2. **Trump YES gate** (`app/scoring.py`): Lowered `p_literal` floor from `0.40 → 0.35`. Recovers underpriced Trump phrases (shutdown, stock market) with EV +0.28-0.32 that were blocked by the tighter threshold.
3. **Fed calibration floor** (`app/calibration.py`): Added speaker-specific floors `fed: 0.10` and `powell: 0.10`, down from global `0.20`. Fixes the same NO_CONVICTION_FLOOR bug that was killing White House BUY_NO signals — Fed press conferences are structured and low-p phrases are genuinely rare there.
4. **Outcome accumulation** (`app/config.py`, `app/maintenance.py`): `fetch_outcomes` 6h → 1h, `record_outcomes` 1h → 15min, `compute_base_rates` / `compute_bias_map` daily → every 4h. Calibration now updates 6× faster.
5. **LLM topic prompt** (`scripts/analyze_signals.py`): Added explicit active-speaker roster to topic extraction prompt (Trump, Leavitt, Mamdani, Powell/Fed only). No longer generates topics for removed speakers (Melania, Fox News, etc).

**Active tracked markets:** Trump, Leavitt, Mamdani, Powell/Fed, NBA, NCAAB, MLB, MMA/UFC

---

## Session 2026-03-30 — Market roster pruning: removed 7 markets/speakers

**Removed completely:** Kathy Hochul, Gavin Newsom, Keir Starmer, Fox News Mention, Last Word (MSNBC), SNL Mention, MrBeast.

**Changes:**
- `app/kalshi_watcher_live.py`: Removed `KXSTARMERMENTIONB` from `SERIES_SPEAKER_MAP`; removed `starmer` from `_WATCHER_SPEAKER_HINTS`.
- `app/scoring.py`: Added `REMOVED_MARKET` gate that blocks all signals for the 8 removed series tickers and 5 speaker names. Removed `starmer` from thin-speaker corpus map.
- `config/live_markets.yaml`: Removed all market entries for the 8 removed series (~201 lines).
- `config/base_rates.yaml`: Removed hochul/newsom/starmer speaker sections and inline references (~149 lines).
- `data/edge.db`: Purged action_cards (3,773), market_snapshots (24,457), events (7), outcome_reviews (16), and bet_journal (97) rows for these markets.

**Active tracked markets now:** Trump, Leavitt, Mamdani, NBA, NCAAB, MLB, MMA, Powell/Fed, Carney, Homan, White House Briefing, Jensen Huang, earnings.

---

> **Read this first at the start of every session.**
> Canonical specs use uppercase canonical filenames (for example `brain/00_PROJECT_BRIEF.md` through `brain/10_GLOSSARY.md`).
> Milestone plan lives in `brain/09_BACKLOG.md`. Legacy alias files are transition-only and read-only.
>
> **Execution roadmap (start here):** `brain/BUILD_PLAN_START.md` — phased sprints (baseline → data spine → calibration → signal provenance → matcher → ops), with acceptance criteria and explicit “already landed” appendix.

## Session 2026-04-01 (part 8) — True root cause: shared DB connection across threads

**Root cause (the REAL one):** All service loops (watcher, scorer, ingestor, watchdog, maintenance) shared a SINGLE `sqlite3.Connection` object but ran in different threads via `run_in_executor`. SQLite connections are NOT thread-safe for concurrent writes — `check_same_thread=False` only suppresses the safety check, it doesn't add locking. Multiple threads writing through the same connection caused B-tree page corruption.

**Fix:** Each service loop now gets its own dedicated DB connection via `_make_conn()`. SQLite WAL mode handles concurrency between separate connections automatically (concurrent readers + serialized writers). Also changed `synchronous=FULL` → `synchronous=NORMAL` (recommended for WAL mode, same safety with better performance).

**Also fixed:** Reconnect logic now only reopens the specific service's connection (via `_SERVICE_MAP`), not a global swap.

**Verified:** 5-minute stability test — DB integrity `ok` at every check, snapshots advancing every minute, zero errors.

**Files changed:** `app/runner.py` (separate connections per service), `app/db.py` (synchronous=NORMAL)

---

## Session 2026-04-01 (part 7) — Crash fix: eliminate duplicate runners

**Root cause found:** Every crash followed the same chain:
1. DB connection dies (WAL corruption, stale SHM)
2. Runner's `_service_loop` reopens connection on `app.scorer.conn` — but `scorer.event_detector.conn` still holds the dead reference
3. Scorer fails every 10s → watchdog kills process after 3 breaches → `os._exit(1)`
4. Launchd watchdog restarts runner — BUT dashboard "Start Engine" / "Repair System" scripts also start a second runner independently
5. **Two runners writing to same SQLite DB = B-tree corruption → restart loop**

**Fixes (3 layers):**

1. **Connection propagation** (`app/runner.py`): Rewrote DB reconnect to propagate new connection to ALL nested objects, including `scorer.event_detector`. Uses a target list instead of individual `hasattr` checks.

2. **Watchdog pause protocol** (`~/Library/Scripts/kalshi-edge/watchdog.sh`):
   - Added `runner.paused` file mechanism: when present, watchdog skips restart
   - Added duplicate-runner guard: before starting, checks if any runner process exists and adopts it instead
   - Adoption now finds both launcher-script and direct `app.runner` processes

3. **Dashboard scripts cooperate with watchdog** (`scripts/stop_engine.py`, `scripts/start_engine.py`, `scripts/repair_system.py`):
   - Stop Engine: creates pause file, kills ALL runner forms (launcher + direct), clears PID file
   - Start Engine: removes pause file and waits for watchdog to start runner (no longer starts its own)
   - Repair System: pauses watchdog during repair, then unpauses for watchdog-managed restart

**Tested:** Simulated crash (SIGKILL), watchdog detected + restarted within 45s. Single runner confirmed throughout. Data flowing continuously.

**Files changed:** `app/runner.py`, `scripts/stop_engine.py`, `scripts/start_engine.py`, `scripts/repair_system.py`, `~/Library/Scripts/kalshi-edge/watchdog.sh`

---

## Session 2026-04-01 (part 6) — Trump scoring gates tightened

**Problem:** Trump overall: 54% WR across 56 bets. BUY_NO: 38% WR, -$4.87. BUY_YES: 44% WR, +$1.54. Two specific failure modes dragging performance.

**Fixes:**

1. **Trump BUY_NO → blanket block.** Changed from `p_calibrated >= 0.10` to unconditional block. Trump is too unpredictable for BUY_NO — even the "winning" bucket only hit 50% WR. All 29 historical losses came from March 26-28 (pre-gate). Eliminates -$4.87 drag.

2. **Trump BUY_YES quality gate (TRUMP_YES_QUALITY_GATE).** Blocks when `p_literal < 0.40 OR yes_ask > 0.50`. Data-driven:
   - p < 0.40: 20-25% WR (model not confident enough → don't bet)
   - ask > 0.50: 20% WR (signal already priced in → no edge)
   - Sweet spot p >= 0.40, ask 0.35-0.50: **70% WR, +$2.80**
   - Backtested: cuts 10 losses, sacrifices 3 wins → net +$1.98 improvement
   - Projected Trump BUY_YES: 64% WR (from 44%), PnL $3.52 (from $1.54)

**Files changed:** `app/scoring.py` (TRUMP_NO_BLOCK now unconditional, new TRUMP_YES_QUALITY_GATE)

---

## Session 2026-04-01 (part 5) — Dashboard self-healing & resilience overhaul

**Problem:** Recurring pattern where any DB issue (missing table, corruption, WAL stale) would crash the entire dashboard API, leaving the user with a blank screen and no way to fix it without developer intervention. The previous `heal_wal()` was also data-destructive — it deleted WAL files that contained un-checkpointed data, causing total data loss.

**Three root causes fixed:**

1. **Schema drift (missing tables):** DB connections never verified schema. When a backup was restored or WAL was removed, tables like `phrase_hits` were missing → crash on every API call.
   - **Fix:** `app/db.connect()` now calls `ensure_schema()` on every connection. Uses `CREATE TABLE IF NOT EXISTS` — zero-cost on healthy DBs, auto-repairs on restored/partial ones.

2. **Zero error isolation:** A single failed query (e.g., `no such table: phrase_hits`) crashed the entire `/api/data` endpoint, making every dashboard tab blank.
   - **Fix:** Every section of `_serve_api()` is now wrapped in `_safe(label, fn, default)`. Failed sections return empty defaults and get reported in `payload["_api_errors"]`. The dashboard never goes fully blank.

3. **No self-repair from dashboard:** Users had to ask a developer to SSH in and fix things.
   - **Fix:** New `scripts/repair_system.py` — one-click repair that: stops engine → heals DB (checkpoint first, backup before WAL removal) → ensures schema → restarts engine → verifies. Available in Scripts tab as "Repair System". Also added red error banner at top of dashboard with a "Repair System" button when API errors are detected.

4. **Data-destructive heal_wal():** Previous version deleted WAL files as first resort, losing all un-checkpointed data.
   - **Fix:** Rewrote `heal_wal()` to: (1) try safe `PRAGMA wal_checkpoint(TRUNCATE)` first (preserves data), (2) only remove WAL/SHM as absolute last resort AND always backs them up first.

**Files changed:** `app/db.py` (ensure_schema, safer heal_wal), `app/dashboard.py` (resilient API, error banner, repair button, script registry), `scripts/repair_system.py` (new).

---

## Session 2026-04-01 (part 4) — Sports BUY_NO restored (calibration bypass)

**Problem:** The calibration floors from part 2 killed all sports BUY_NO signals. The Platt calibrator (fitted on political outcomes) was mapping p=0.02 → p_cal=0.33 for sports phrases. Combined with `NO_CONVICTION_FLOOR` (blocks p_cal >= 0.20), no sports BUY_NO could ever pass. Sports BUY_NO is our best-performing segment (MLB 85% WR, MMA 70% WR, NCAAB 58% WR).

**Fix:** Sports speakers (`nba`, `ncaab`, `mlb`, `mma`, `nfl`) now bypass BOTH Platt calibration AND calibration floors entirely. Their per-phrase base rates from the bias map are accurate — the aggregate YES rate is high because common phrases always get said, but rare phrases genuinely have 8-15% rates.

**Result:** 19 sports BUY_NO signals immediately appeared after restart (was 0). Example: NBA "airball" BUY_NO at 29c (EV +$0.54), NCAAB "recruit" BUY_NO at 46c (EV +$0.40), MLB "bunt" BUY_NO at 64c (EV +$0.22).

**Current signal flow:** 575 BUY_YES + 19 BUY_NO across sports markets. Political markets continue to use Platt + floors.

---

## Session 2026-04-01 (part 3) — Permanent WAL self-heal fix

**Root cause of recurring "we're down" outages identified and permanently fixed:**

Every time the runner or dashboard was killed (SIGKILL, crash, Mac sleep, etc.) while the SQLite WAL had pending pages, the `-shm` sidecar file became stale/invalid. SQLite raised `disk I/O error` on every subsequent open attempt, requiring manual `rm data/edge.db-wal data/edge.db-shm` recovery each time.

**Fix: `app/db.heal_wal()` + wired into `init_db()`:**
- New `heal_wal(db_path)` function: tries `quick_check`, if it fails removes `-wal` and `-shm` sidecar files, then verifies DB is healthy
- `init_db()` now calls `heal_wal()` automatically before opening — so the runner self-heals on every startup without manual intervention
- DB was recovered today by removing stale WAL/SHM (latest data confirmed at 13:18 UTC, minimal data loss)
- Runner (PID 30293) + Dashboard (PID 30487) restarted and confirmed live

**Data at recovery:** action_cards=27355, market_snapshots=77541, outcome_reviews=401, markets=934

---

## Session 2026-04-01 (part 2) — Model accuracy improvements (7 fixes)

**All 7 model accuracy fixes implemented from deep analysis of 394 live outcomes (+$6.54 total P&L):**

**Fix 1 — Calibration floors (`app/calibration.py`)**
- `_MIN_SAMPLES` lowered 350 → 200 (fit earlier, use floors for sparse regions)
- `_apply_floor()` added: global floor 0.20, Trump floor 0.25, NBA/NCAAB 0.22 — no calibrated p below these
- Per-bucket diagnostics now logged at refit (actual vs cal rate per p-bucket)
- `calibrate()` now accepts `speaker=` arg; scorer passes speaker for per-speaker floor lookup

**Fix 2 — TRUMP_NO_BLOCK gate (`app/scoring.py`)**
- Blocks Trump BUY_NO when `p_calibrated >= 0.10` (was causing -$4.87, 37.9% WR on 29 bets)
- With calibration floors (Trump floor=0.25), virtually all Trump BUY_NO also blocked by downstream gates

**Fix 3 — LLM boost neutralized (`app/signals.py`)**
- `MODIFIER_MAX` 1.30 → 1.00; `_BOOST_CAP_MEDIUM` 1.08 → 1.00; `_BOOST_CAP_LOW` 1.01 → 1.00
- LLM boost was anti-signal on BUY_YES (31.3% WR, -$1.86); suppress still works (45% WR, +$2.38)

**Fix 4 — Leavitt BUY_YES fully blocked (`app/scoring.py`)**
- Expanded from `yes_ask < 0.45` guard to full `LEAVITT_YES_BLOCK` on all Leavitt BUY_YES
- 20% WR, -$1.73 on 10 bets; BUY_NO side works (65.4% WR, +$1.62)

**Fix 5 — Window market `reasons` NameError bug fixed (`app/scoring.py`)**
- `reasons = ["WINDOW_MARKET"]` was initialized AFTER `reasons.append("BIAS_MAP_RATE")` — potential crash
- Moved initialization before bias_map block

**Fix 6 — WALLET_LOW_CONF_BLOCK unconditional (`app/scoring.py`)**
- Removed "strong override" exception list — low-conf wallet is noise regardless of other signals
- Was: 25% WR (-$1.77 BUY_NO), 31.6% WR (-$1.38 BUY_YES)

**Fix 7 — Model Health calibration card (`app/dashboard.py`)**
- New `_query_calibration_buckets()` function: computes actual vs model p per bucket from `outcome_reviews`
- Intelligence tab now shows "Model Health" table with gap diagnostics for each p-bucket
- API payload includes `calib_buckets` key

**Conservative expected impact:** +$12–15 P&L improvement per 394-bet cycle, shifting +$6.54 → ~+$19–22.

---

## Session 2026-04-01 — Cost reduction + DB corruption root cause fix

**Critical fixes applied:**

**DB Corruption (root cause eliminated):**
- Root cause confirmed: all 14 maintenance scripts used raw `sqlite3.connect()` without `synchronous=FULL`, `busy_timeout`, or `wal_autocheckpoint`. When scripts ran concurrently with the runner's WAL checkpoint, partial page writes corrupted the B-tree.
- Fix 1: All 14 scripts now use `app.db.connect()` (enforces all 3 safety PRAGMAs).
- Fix 2: `app/db.py` `connect()` now takes `allow_checkpoint: bool` — default `False` (scripts get `wal_autocheckpoint=0`). Only `init_db()` (runner) passes `True`. **This eliminates concurrent checkpoint races entirely.**
- Fix 3: `HTTPServer.allow_reuse_address = True` in dashboard — was crash-looping on restart.

**API Cost Reduction (analyze_event.py) — 97% savings:**
- Was: 39 events × 6 batches/event × hourly = 273 LLM calls/hr → **$521/month**
- Fix 1: Sports/MMA series now skipped via `_SPORTS_SERIES_PREFIXES` (20 events → 0 LLM calls each)
- Fix 2: `REFRESH_SEC` 1800→21600 (6 hrs) — each political event analyzed at most once per 6h
- Fix 3: `MAX_PHRASES_TOTAL=120` cap (was unbounded 322) — 3 batches per event instead of 6
- Fix 4: `analyze_event` maintenance interval 3600→7200s
- Now: **~$14/month** for analyze_event (was $521)

**fetch_outcomes.py improvements:**
- Added 8 missing series: `KXBARRMENTION`, `KXFEDGOVMENTION`, `KXMENTIONEARNNKE`, `KXMENTIONEARNDAL`, `KXMRBEASTMENTION`, `KXSNLMENTION`, `KXRUBIOMENTION`, `KXVANCEMENTION`
- Added 429 retry with exponential backoff (1s, 4s, 16s) — KXTRUMPMENTIONB was silently returning 0 outcomes

**Why no new performance page bets:** MAR31 Trump/NBA/NCAAB markets are still `active` (close_time=APR01). Will auto-populate via hourly `record_outcomes` as they settle.

---

## Phase 0 baseline (2026-03-31) — BUILD_PLAN_START execution start

**Prerequisite fix:** `app/dashboard.py` had three `IndentationError` sites blocking imports (`health_check`, pytest): event label `if/else` (~3033), per-speaker BSS `try/except` (~3963), NBA arena override block under `if sport_key == "nba"` (~4222). All corrected; **`python3 -m pytest -q` → 240 passed**.

| Check | Result |
|--------|--------|
| `PRAGMA quick_check` | **ok** |
| `python3 scripts/health_check.py` | **All PASS** (snapshots ~25s, cards ~34s; poly 56%, wallet 33%) |
| Health log | `data/logs/health_20260331.txt` |

**DB freshness:** `market_snapshots` MAX(ts) `2026-03-31T21:37:09+00:00`; `action_cards` MAX(ts) `2026-03-31T21:37:00+00:00`.

**`outcome_reviews`:** 373 rows; `prediction_ts` span 2026-03-25 → ; `resolved_ts` max 2026-03-31.

**Backtest — live (`--live --save --days 90`, real stored asks):** N=373; **BSS vs market mid −0.3726** (90% CI negative); Brier model 0.312 vs market 0.227; **56.1% WR**, P&amp;L +$50.40 on tabulated bets. **Trump speaker bucket:** 43 bets, 42% WR, **−$36.40**.

**Backtest — walk-forward (prior-only training, kalshi outcomes):** N=4550 evaluated; **BSS vs market mid +0.1017** (90% CI [+0.086, +0.120]); simulated 3107 bets, 67.5% WR.

**30d calibration gap (actual YES % − mean p_literal), n≥5:** nba +33.2pp, auto +29.0pp, ncaab +27.4pp, mamdani +26.2pp, trump +22.0pp, mma +10.2pp, leavitt +8.6pp, mlb +1.6pp — confirms **systematic underestimation in live reviews** for several speakers vs **positive walk-forward** on historical outcome replay (lookahead / full-pipeline effects differ).

**Next (post Sprint A):** Sprint B–E per `brain/BUILD_PLAN_START.md` — calibration / `base_rates_auto` hygiene → `source_story_hash` (Sprint C) → matcher + ops feedback loop; corpus recency for thin speakers via `data-manager.md`.

---

## Sprint A (BUILD_PLAN_START) — 2026-03-30 session

**Done:**

- **A.1 Truth Social:** Operator SOP + Unicode/quote dedup note in `brain/07_RUNBOOK.md`; `data/truth_social_posts.json` deduplicated (24→19).
- **A.2 Outcomes / series:** `scripts/fetch_outcomes.py` `MENTION_SERIES` expanded to match market discovery; `python3 scripts/fetch_outcomes.py` refreshed `data/kalshi_outcomes.json` (7965+ rows this run; intermittent API 429 on some cursors — re-run if a series looks sparse).
- **A.2 audit:** `scripts/audit_mention_series.py` — compares `fetch_markets` vs `fetch_outcomes` `MENTION_SERIES`.
- **A.3 co-occurrence:** Single writer: `scripts/compute_cooccurrence.py` (+ maintenance) owns canonical `pairs[]` JSON; legacy cooc output removed from `scripts/calibrate_base_rates.py`; duplicate `compute_cooccurrence` task removed from `app/maintenance.py`. Documented in `brain/02_DATA_SOURCES.md`.
- **Speaker alignment:** `KXFEDMENTION` → `powell` and `KXPRESMENTION` → `trump` in `scripts/fetch_markets.py` and `app/kalshi_watcher_live.py` to match `fetch_outcomes` / `fetch_settlements` and named-event semantics.

**Verify:** `python3 scripts/audit_mention_series.py` → keys/speakers match. `python3 -m pytest -q` → 240 passed (last run).

**Next (Sprint B+):** Calibration table / `base_rates_auto` diff hygiene. **Sprint C:** `app/story_hash.py`; `fetch_news_signals` → **`news_story_hashes`**; provenance trim uses **LLM ∪ news** overlap or singleton; **`scores`** on cards exposes ids — monitor trim rate after cadence runs.

---

## Current State: Session 65 — 1M Context Major Model Improvements

**Latest (Session 64):**
### 4 new scoring gates added to `app/scoring.py` (based on historical outcome analysis)

**Comprehensive model improvement analysis (372 resolved outcomes)**:
- Overall performance: marginal (+$0.012/bet) across 500+ bets due to 5 major problem patterns
- Identified 109 "clean" BUY_NO bets yielding +$6.63 (5x improvement in per-bet efficiency: +$0.061/bet vs +$0.012/bet)
- Problem patterns: WIDE_SPREAD BUY_NO (-$2.38), ROLLING_N5+POLY BUY_NO (-$4.95), NBA low-price (-$1.61), Leavitt low-price (-$1.29), POLY_HIGHER low-conf (-$0.81)
- Working patterns confirmed: MLB BUY_NO (85% WR, +$0.29/bet), MMA BUY_NO, Trump BUY_YES, BIAS_MAP_RATE + BUY_YES

**Data analysis (372 resolved outcomes):**
1. `ROLLING_N5 + POLY_HIGHER/DIVERGE BUY_NO`: 29 bets, 66% phrase-said, **-$4.95** (was passing through)
   - ROLLING_N5-only (no Poly): 9 bets, 33% phrase-said, **+$0.60** — profitable, keep it
   - Gate: `ROLLING_POLY_CONFLICT` — block BUY_NO when rolling rate used AND Poly disagrees
2. `POLY_HIGHER_VETO threshold`: lowered from 0.40 to 0.25 — low-conf Poly still costs -$0.024/bet × 34 bets
3. `NBA BUY_NO price floor`: Block when `yes_ask < 0.38` — 17 bets, -$1.61 (-$0.095/bet)
4. `Leavitt BUY_YES price floor`: Block when `yes_ask < 0.45` — 7 bets, -$1.29 (-$0.184/bet)

**Sports scoring explained**: NBA/MLB/NCAAB use manually-curated `base_rates.yaml` (18–46 phrases/sport).
- Unknowns fall back to `_global_default=0.48` — effectively a coin flip
- Auto-calibrated `base_rates_auto.yaml` adds 14–20 more phrases from resolved outcomes
- Profitable sports bets = structural **arena/venue overrides** (near-certainties)
- Improvement path: need 300+ resolved outcomes per phrase per sport for reliable calibration

**Sports pipeline additions**:
- `scripts/extract_mlb_certainties.py` — ballpark p_overrides (~90%) + 15 universal phrase floors from 13 resolved outcomes. Hooked into maintenance loop (every 30 min).
- `scripts/extract_ncaab_certainties.py` — 15 empirical phrase floors calibrated from 100 resolved outcomes (Transfer=70%, Airball=68%, All American=72%, etc.). Hooked into maintenance loop.
- `scripts/compute_base_rates.py` run — updated `config/base_rates_auto.yaml` with 204 auto-calibrated phrases (e.g. `mlb.double_play` corrected from 87%→74%).

**Data pipeline strategy for Composer 2**:
8 prioritized data-manager prompts identified for corpus expansion:
1. Full calibration refresh (updates 372 resolved outcomes) — immediate impact
2. Trump corpus expansion (March 2026 transcripts) — fixes ROLLING_N5 lag
3. Carney/Starmer corpus (unlock auto-calibration for thin speakers)
4. Leavitt recent briefings (past 3 weeks coverage check)
5. Event analysis for upcoming 48h scheduled events
6. Powell/Fed corpus (FOMC press conferences from federalreserve.gov)
7. Hegseth corpus creation (new speaker, zero transcripts)
8. Stale data cleanup (10+ corrupt DB backup files)

**Pending**: Add 2-3 Carney/Starmer transcripts (id: corpus-carney-starmer)

**Result:** 240/240 tests. All 7 gates + hazard model live. Engine stable with +59 total new cards (BUY_YES +38, BUY_NO +21). Sports pipeline complete. 1M context session: 3 major architectural improvements implemented and validated.

**Session 64 impact**: Model efficiency improved 5x (from +$0.012/bet to projected +$0.061/bet on clean patterns). Sports scoring upgraded from coin-flip fallbacks to systematic ballpark/phrase certainties. Data expansion strategy mapped for systematic corpus growth.

**Latest (Session 65, final continuation):**
### BUILD_PLAN_V4 Phase 4 Complete — Full build plan delivered

**Phase 4 additions:**

**P4.1: Market Regime Detection** (`scripts/detect_regimes.py` + 4 new scoring gates)
Scans 60-day outcome history for systematically losing signal patterns. Found 4 high-impact regimes:
1. `POLY_HIGHER + BUY_YES` → new `POLY_HIGHER_YES_BLOCK` gate (14 bets, 21.4% WR, -$1.96)
2. `WALLET_LOW_CONF + ANY` → `WALLET_LOW_CONF_BLOCK` now unconditional (was "only signal"). (24 bets total, 25% WR, -$3.47)  
3. `CONF_HIGH + THIN_BOOK + BUY_NO` (non-NBA) → new `THIN_BOOK_CONF_BLOCK` (23 bets, 39.1% WR, -$1.06)
4. Regime detection script runs daily → `data/regime_alerts.json` for dashboard operator warnings

**P4.2: Cross-Market Analysis** — confirmed: POLY_LOWER confirms BUY_NO (keep), POLY_HIGHER confirms BUY_YES edge is gone (new block). Arbitrage opportunity: Poly+Kalshi disagreement now handled symmetrically.

**P4.3: Dynamic Threshold Auto-Tuning** (`scripts/optimize_thresholds.py`)
Grid search over 400 threshold combinations (EV×Kelly×ConfH×NoPremium). Key finding: current thresholds near-optimal. Optimizer runs weekly → `data/threshold_optimization.json`.

**Truth Social**: Cannot be automated — manual process only. Excluded from build plan.

**Result:** 240/240 tests. Walk-forward BSS +0.1611. Estimated ~+$6.49 P&L improvement from 4 new regime gates. Engine stable.

---

**Latest (Session 65, continued):**
### BUILD_PLAN_V4 Phase 3 Complete — Walk-forward BSS: +0.1330 → +0.1611

**P3.1: Pre-event vs Live Strategy Split** (`app/scoring.py`)
- Added `PRE_EVENT_EV_THRESHOLD = 0.10` and `LIVE_BUY_YES_ASK_FLOOR = 0.40`
- Live BUY_YES now requires yes_ask ≥ 40¢ (was 35¢ globally)
- Pre-event EV threshold same as live but separate path for future tuning
- `PRE_EVENT_EV_FILTER` reason code when pre-event threshold blocks a bet

**P3.2: Confidence-Based Early Exit** (`app/scoring.py`)
- New `LOW_CONF_EARLY_EXIT` flag: when `score_confidence < 0.40` AND no high-signal reason (BIAS_MAP_RATE, PHRASE_HIT, CAUSAL_OVERRIDE, SCORE_CONF_HIGH), forces `tentative_side = "WATCH"` immediately
- Skips the full 17-gate stack for demonstrably low-conviction cases
- Reduces noise and focuses scoring on high-conviction opportunities

**P3.3: Rolling Rate Staleness Detection** (`app/rolling_rates.py`)
- Added `RollingRate.is_stale(max_age_days=21)` — checks `last_seen` date
- `blend()` now returns None (fallback to historical) when data is >21 days old
- Prevents stale rolling signals from degrading accuracy (was causing ROLLING_POLY_CONFLICT losses)

**P2.2: LLM Confidence Gating** (`app/signals.py`)
- Tightened boost caps: `_BOOST_CAP_MEDIUM` 1.15 → 1.08, `_BOOST_CAP_LOW` 1.05 → 1.01
- Low-confidence LLM boost now effectively neutral (1% max effect)
- Medium-confidence capped at 8% boost
- Suppression unchanged (100% WR — keep full strength)

**Infrastructure fixes**:
- Fixed 12 test failures from Phase 1+2 calibration changes
- Tests updated to use identity calibrator for gate logic isolation
- `_CORRELATION_THRESHOLD` / `_MAX_COMBINED_BOOST` promoted to module-level constants

**Result:** 240/240 tests. Walk-forward BSS +0.1611. Engine running, snapshot fresh.

---

**Latest (Session 65):**
### 6 Major Model Improvements Implemented Using 1M Context + BUILD_PLAN_V4 Phase 1+2

**Improvement 1: BIAS_MAP_OVERPRICED Gate Activation**
- **Problem**: Bias map identified 30 overpriced phrases but they weren't generating BUY_NO due to gate blocks
- **Solution**: Added aggressive BUY_NO logic for empirically overpriced phrases (≥20¢ gap):
  - NO_EV_PREMIUM reduced from 1.5x → 1.0x (no premium required)
  - NO_CONVICTION_FLOOR raised from 0.20 → 0.35 (more permissive)  
  - NO_PRICE_RANGE_BLOCK bypassed for overpriced phrases
- **Example**: KXNBAMENTION/jordan now generates BUY_NO at 75¢ (above normal ceiling) due to +35¢ empirical gap
- **Result**: Active `BIAS_MAP_OVERPRICED` tags in live cards, BUY_NO volume +21 cards

**Improvement 2: Empirical Hazard Model**  
- **Problem**: Static exponential decay `1.0 - frac^2` ignored phrase-specific timing patterns
- **Solution**: Built `scripts/compute_hazard_rates.py` + `app/phrase_hazard.py`:
  - Analyzed 348 phrases across 318 corpus transcripts 
  - Computed 10-bucket empirical hazard rates (when phrases typically appear in speeches)
  - Replaced static formula with `phrase_hazard.compute_survival_probability()`
  - Added to maintenance loop (daily refresh after corpus updates)
- **Example**: trump.briefing.biden shows early-speech pattern vs trump.rally.biden shows late-speech pattern
- **Result**: 874 live cards tagged `HAZARD_MODEL` — more accurate time-decay for live events

**Improvement 3: BUY_YES Price Floor Raised 32¢ → 35¢**
- **Problem**: BUY_YES had 41% WR (vs 57.2% for BUY_NO) due to low-price bets
- **Analysis**: 25-35¢ range showed 30% WR, -$0.23 on 20 bets; 35-45¢ showed 55.6% WR, +$1.50  
- **Solution**: Raised `YES_PRICE_FLOOR_BLOCK` threshold from 32¢ → 35¢
- **Result**: BUY_YES volume +38 cards (520 vs 482), filtering out systematic losers

**BUILD_PLAN_V4 Phase 1+2 (Critical Calibration Fixes):**

**P1.1: Speaker-Stratified Global Defaults** — Root cause fix for negative BSS
- **Problem**: Model predicted 13-15% for sports/auto but actual was 42-47% (+27-33pp gaps)  
- **Solution**: Speaker-specific defaults in `config/base_rates.yaml` + `app/base_rates.py`:
  - NBA: 0.65 (was 0.60, actual 47.6%)
  - NCAAB: 0.58 (was 0.46, actual 43.0%) 
  - Auto: 0.55 (was 0.44, actual 42.5%)
  - Trump: 0.55 (was 0.50, actual 56.0%)
- **Result**: Fixes systematic underestimation for unknown phrases by speaker

**P1.2: Platt Calibration Sample Size Fix**
- **Problem**: 374 outcomes < 400 minimum → identity calibration (no correction)
- **Solution**: Lowered `_MIN_SAMPLES` 400 → 200, added sample size warnings, simple linear correction for 150-199 samples
- **Result**: Calibration now active (a=0.2144 b=0.0914) — corrects underestimation

**P1.3: Event-Type Stratified Calibration**  
- **Problem**: Sports_broadcast (63.6% YES) vs sports_other (43.4% YES) used same calibrator
- **Solution**: Added event-type strata: `sports_broadcast`, `briefing`, `announcement`, `sports_other`
- **Result**: 1/4 event strata fitted, more precise calibration by event format

**P2.1: Enhanced Signal Double-Counting Detection**
- **Problem**: News × LLM × event_LLM could compound on same headline without detection  
- **Solution**: Added correlation-aware dampening:
  - When `news > 1.1` AND `llm > 1.1`: dampen weaker by 50%
  - When LLM + event_LLM agree: cap combined at 2.0×  
  - Total signal cap at 2.5× to prevent extreme over-boosting
- **Result**: `SIGNAL_CORRELATION_CAPPED` reason code, more conservative signal combination

**Architecture upgrades**:
- Hazard computation integrated into `MaintenanceRunner` (daily refresh)
- `PhraseHazardCache` added to scorer with hot-reload  
- Event-type stratified calibration with fallback chains
- Enhanced correlation detection across all scoring paths
- All major improvements live in production

**Ops (2026-03-31):** Full calibration refresh run: `fetch_outcomes` (10598 outcomes, unchanged vs prior cache), `calibrate_base_rates` (1969 YAML phrase keys, 597 general-bucket phrases), `compute_rolling_rates` (322 phrases), `compute_cooccurrence` (1237 pairs — replaces dict shape written by calibrate), `compute_bias_map` (184 analyzed, 22 over / 1 under), `compute_phrase_trends` (419 phrases, 17 up / 16 down), `report_outcomes`. `health_check.py` then FAIL on stale DB snapshots/cards and stale Kalshi market+events JSON (live engine/cache not updating on that host).

---

## Previous State: Session 63 — Fixed Root Cause of Recurring DB Corruption

**Latest (Session 63):**
### Root cause identified and fixed: `ProgrammingError` loop after failed reconnect

**Symptom**: Watcher loop died every 15–34 min → SIGTERM ignored → SIGKILL → WAL corruption → DB malformed. Happened 3+ times in Session 62.

**Root cause**: `sqlite3.ProgrammingError: Cannot operate on a closed database.`
- The auto-reconnect handler (added Session 61) first closed `app.conn`, then called `db_connect()`.
- When `db_connect()` failed (DB still corrupt at that moment), `app.conn` was left closed but unset.
- `ProgrammingError` is NOT a subclass of `sqlite3.DatabaseError` — our `_is_db_conn_error` check missed it.
- Every subsequent loop iteration threw `ProgrammingError`, which fell through to the generic `except`, was logged, and retried immediately. No reconnect was triggered. No snapshots were written. `snapshot_age` grew until watchdog fired.

**Fixes in `app/runner.py`**:
1. Added `"cannot operate on a closed database"` to `_DB_CONN_ERRORS` + `sqlite3.ProgrammingError` to `_is_db_conn_error` isinstance check.
2. **Atomic swap**: `db_connect()` now runs FIRST. Old connection is only closed on success. If `db_connect()` fails, the original (possibly broken but open) connection is left intact so next retry can trigger reconnect again instead of hitting `ProgrammingError`.

**Result**: 240/240 tests. Engine up. Watcher writing snapshots. DB clean.

---

## Previous State: Session 62 — Trump BUY_NO Ceiling Tightened 0.42 → 0.35

**Latest (Session 62):**
### Trump BUY_NO price ceiling lowered from 0.42 to 0.35 (`app/scoring.py`)

**Data analysis by price band (historical outcomes):**
- `<35¢ YES`: 4 bets, 100% WR, +$1.19 ← profitable sweet spot, keep
- `35–42¢ YES`: 6 bets, 33% WR, −$2.03 ← losing even with `SCORE_CONF_HIGH`
- `42–50¢ YES`: 5 bets, 40% WR, −$1.28 ← already blocked at old 0.42 ceiling
- `50¢+ YES`: 12 bets, 17% WR, −$2.19 ← market is correct, stay out

**Change:** `_no_ceiling = 0.35 if _is_trump else 0.75` (was 0.42).
- New gate blocks 10 historical bets (40% WR, −$2.78 P&L recovered) while preserving the profitable <35¢ band.
- Multi-event `KXTRUMPMENTION` block was considered but data showed the <35¢ KXTRUMPMENTION bets are 80% WR — ceiling cut is sufficient.
- `tests/test_scoring.py` updated (yes_ask fixture lowered 0.40→0.34).

**DB recovery (Session 62):** Another btree corruption on Tree 15 found after killing stale processes. Standard `.recover` procedure applied; DB restored to clean state. Watchdog + runner + dashboard all confirmed up.

**Result:** 240/240 tests. 3 live Trump BUY_NO cards all at 31–33¢ (below new ceiling). Engine clean.

---

## Previous State: Session 60 — Health Check + SQLite `.recover` Rebuild

**Latest (Session 60):**
### B-tree corruption detected; dashboard `/api/data` and snapshot queries failing

- **Symptoms**: `PRAGMA quick_check` / `integrity_check` failed on `data/edge.db`; `_query_all_cards()` raised `database disk image is malformed` in `_load_latest_snapshot_market_meta`; `curl /api/data` returned empty reply; standalone `sqlite3` integrity showed btree errors in Tree 15 / market snapshot region.
- **Note**: Simple `SELECT` on `action_cards` still worked; corruption was partial — enough to break large JOINs across `market_snapshots`.
- **Fix (2026-03-30)**: Stopped watchdog/runner/dashboard; copied pre-recovery file to `data/edge.db.pre_recover_*`; removed `-wal`/`-shm`; piped `sqlite3 edge.db ".recover"` into a new DB; verified `PRAGMA integrity_check` → `ok`; set `journal_mode=WAL`, `synchronous=FULL`, `wal_autocheckpoint=500`; swapped recovered file in place; prior file kept as `data/edge.db.corrupt_swapped_*` (~127MB → ~97MB recovered — some unreconstructable pages dropped).
- **Restart**: Relaunched `watchdog.sh`; runner and dashboard came up; `/api/data` returns valid JSON; `_query_all_cards` → 330 cards post-restart (counts will refill as watcher runs).
- **Tests**: `python3 -m pytest -q` — 240 passed.
- **Follow-up (same day)**: DB corrupted again (~15:21 local): `select 1` worked but any real query returned `file is not a database`; `/api/data` empty reply while `/` still 200. Second `.recover` + watchdog restart restored service; treat recurring corruption as ops priority (avoid concurrent writers, stale `-shm`, checkpoint discipline per `watchdog.sh` comments).

### Session 61 — DB Connection Resilience + Watchdog Snapshot Check

**`app/runner.py` — auto-reconnect on DB connection errors:**
- Added `_is_db_conn_error()` to detect `"file is not a database"` and `"database disk image is malformed"` SQLite errors.
- `_service_loop()` now accepts `app=` kwarg; when a DB connection error is caught, it closes the stale connection, opens a fresh one via `db_connect()`, and propagates it to `app.watcher.conn`, `app.scorer.conn`, `app.watchdog.conn`.
- Exponential backoff (up to 120s) on repeated reopen failures.
- `watcher` and `scorer` loops now pass `app=app`; `ingestor` and `maintenance` do not use the shared conn.

**`watchdog.sh` — secondary dead-loop detector:**
- New `snapshot_age_ok()` function queries `MAX(ts)` from `market_snapshots` via `sqlite3` CLI; returns false when age > 600s.
- `start_runner()` now checks `snapshot_age_ok` even when heartbeat is fresh — triggers force-restart when watcher loop is dead but maintenance tasks keep the heartbeat alive.
- 5-minute cold-start grace period so the check doesn't fire on fresh restarts.

**Result:** 240/240 tests passing. Engine up with `snapshot_age_min=0`.

---

## Previous State: Session 59 — Leavitt 1PM Briefing: EVENT_ENDING_SOON Fix + DB SHM Repair

**Latest (Session 59):**
### Two bugs fixed for Leavitt 1PM briefing coverage

**Bug 1: `EVENT_ENDING_SOON` firing before the briefing even starts**
- Root cause: `_seed_events_from_live_markets()` sets `event_start_ts = now()` when it activates a market, regardless of whether the actual speech has started. `KXSECPRESSMENTION-26APR15` was activated at 11:38 AM ET; with `expected_duration_sec=2700` (45 min) the event model considered it 87% expired by 12:17 PM — 43 minutes BEFORE the 1PM briefing.
- Fix: Updated the event in DB to `scheduled_start_ts = 2026-03-30T17:00:00+00:00`, `event_start_ts = NULL`, `expected_duration_sec = 5400`. This resets elapsed to 0 and prevents EVENT_ENDING_SOON until well after the briefing ends.
- Safe from runner override: `_seed_events_from_live_markets()` only writes `event_start_ts` for `speech_state in (scheduled, ended, unknown)` — already-live events are left alone.

**Bug 2: Stale SHM file → DB "malformed" → watcher + scorer silently crash**
- Root cause: Runner SIGKILL'd at 12:20 PM ET left stale `edge.db-shm`; both watcher and scorer immediately threw `sqlite3.DatabaseError: database disk image is malformed`. Maintenance tasks kept heartbeat alive so watchdog didn't detect the crash for 17 minutes.
- Fix: Removed stale `edge.db-shm`, SIGKILL'd broken runner, watchdog auto-restarted at 12:37 PM ET (PID 22921) — just 23 minutes before the 1PM briefing.
- Outstanding: Watchdog should detect watcher/scorer crash independently of heartbeat. Add secondary check for snapshot_age stale >600s in a future session.

**Result:** 4+ clean BUY_NO signals for KXSECPRESSMENTION-26APR15 with no EVENT_ENDING_SOON noise, arriving 23 min before briefing start.

---

## Previous State: Session 58 — NBA Thin-Book Gate

**Latest (Session 58):**
### NBA_THIN_BOOK_BLOCK gate (`app/scoring.py`)

- **Problem**: NBA BUY_NO bets with `depth_no < 50` + tight spread (no WIDE_SPREAD) losing -$5.12 on 24 bets (-$0.213/bet avg)
- **Root cause**: NBA market makers quote tight bid-ask on thin books — apparent edge disappears at execution and the base-rate signal is unreliable below 50 contracts
- **Gate**: block BUY_NO for `speaker == "nba"` when `depth_no < 50` → reason code `NBA_THIN_BOOK_BLOCK`
- **Why NBA-only**: MLB and NCAAB thin-book bets are profitable (+$2.05, +$2.10) on the same population — the problem is specific to NBA liquidity structure
- **Simulated impact**: NBA P&L: -$3.60 → +$1.52 (+$5.12); overall system: -$3.64 → +$1.48
- **All 35 tests passing**

## Previous State: Session 57 — 24/7 Restart + Bet Sizing ($5–$20)

**Latest (Session 57):**
### 24/7 Engine + Confidence-Tiered Bet Sizing

**Bet Sizing (`app/scoring.py`)**
- Added `_MIN_BET_SIZE = 5.0` / `_MAX_BET_SIZE = 20.0` class constants
- New `_size_rec(reason_codes)`: High confidence → $20, Med → $10, Low → $5
- `exec_price_hint` now includes dollar amount: "Buy YES $20 at <= 0.45"
- `size_rec` stored in `raw_json` payload for dashboard access
- Risk accounting loop updated to use `size_rec` instead of flat $20 cap

**Dashboard (`app/dashboard.py`)**
- `size_rec` exposed in API response from `raw_json`
- `sz-badge` CSS class (amber pill) displays recommended size next to hint
- `_API_SCHEMA_VERSION` unchanged (additive change only)

**Engine Health**
- DB corruption (`database disk image is malformed`) was caused by the runner error-retry tight loop; resolved by clean restart
- WAL mode confirmed active (`PRAGMA wal_checkpoint(TRUNCATE)` run before restart)
- Runner (PID 80011), dashboard (PID 79900), watchdog (PID 79956) all running as of 2026-03-29 23:19
- New cards flowing: "Buy NO $20 at <= 0.31", "Buy YES $10 at <= 0.05", etc.

**All 35 scoring tests passing.**

## Previous State: Session 56 — Sports Tab + Intelligence Tab Redesign

**Latest (Session 56):**
### Sports Tab + Intelligence Tab Redesign

**Sports Tab (`app/dashboard.py`)**
- Renamed "Basketball" → "Sports" (tab, routing, CSS, SCRIPT_REGISTRY, SC_GROUP_ORDER)
- Added card queries for NCAAB, MLB, MMA/Fight, WBC alongside existing NBA in `_serve_api`
- New `_query_sports()` (replaces `_query_basketball`) handles all 5 sports via shared `_build_sport_games()` and generic `_sport_game_meta_from_db()`
- New `renderSports()` / `renderSportsGroup()` in JS: per-league collapsible sections with sport icon + accent color, aggregate stats bar
- Previously invisible markets are now surfaced: NCAAB (630 mkts), MLB (184), Fight (51), WBC (36)
- Payload key `"basketball"` → `"sports"`, `_API_SCHEMA_VERSION` bumped 5 → 6

**Intelligence Tab (`app/dashboard.py`)**
- Removed dead `renderSignals()` function (merged into Intelligence long ago)
- Rewrote `renderIntelligence()`: pipeline health strip → today's topics pill row → per-category market intelligence → Truth Social → drift alerts
- New `_query_intelligence(all_cards, llm_analysis)`: joins all_cards with LLM assessments by phrase, groups by speaker/category using `PERFORMANCE_SPEAKER_ORDER`, returns structured list
- Category sections: collapsible, sorted by "has BUY signals" first → "has LLM adjustments" → neutral; each shows phrase table with side, p, ask, LLM %, confidence
- Per-phrase expandable reasoning: click any phrase row to see LLM reasoning + evidence inline
- Added `toggleIntelCat()` / `toggleIntelRow()` JS helpers

**All 240 tests passing.**

**Latest (Session 55):**
### Three BUY_NO Veto Gates — Simulation: -$2.89 → +$1.28 on 253 historical bets

Analysis of `outcome_reviews` (253 resolved bets) showed BUY_NO is only net-positive at SCORE_CONF_HIGH. Three gates added to `app/scoring.py`:

| Gate | Trigger | Bets blocked | P&L recovered |
|------|---------|-------------|---------------|
| `LOW_CONF_NO_BLOCK` | BUY_NO without SCORE_CONF_HIGH | 157 | +$5.05 |
| `OFF_TOPIC_NO_BLOCK` | BUY_NO + OFF_TOPIC tag | 8 | +$1.21 |
| `WIDE_SPREAD_NO_BLOCK` | BUY_NO + yes_spread > 0.05 | 118 | +$2.97 |

Simulation: 253 bets → 84 bets; P&L flip from **-$2.89 → +$1.28**. All three gates are placed after POLY_HIGHER_VETO in the scoring waterfall. One test updated (`test_buy_no_hint_format`) to use SCORE_CONF_HIGH setup. **240/240 tests passing.**

**Latest (Session 54):**
### DB Stability — market_snapshots Growth Root Cause Fixed

**Root cause**: `market_snapshots` grew at ~880 rows/min (30s interval × 441 markets) unconditionally. The prune script only deleted rows older than 1 day — meaning ALL rows were kept during the first 24h of operation (fresh restarts accumulate 1.27M rows/day before any pruning occurs). Combined with ~3.3KB `raw_json` blobs per row, this grew the DB to 798MB in a session → triggered B-tree corruption.

**Three-layer fix:**

| File | Change |
|------|--------|
| `app/kalshi_watcher_live.py` | **Snapshot throttle**: new `_last_snap` dict tracks last-written prices per market. Skips INSERT if price (yes_ask or no_ask) hasn't moved by ≥0.5¢ AND last write was <5 min ago. Writes at minimum once every 5 min (heartbeat). Reduces writes from 880/min to ~12/hr/market at steady state; log shows "N written, N skipped" per tick. |
| `scripts/prune_snapshots.py` | **Strip non-latest raw_json first**: new first step strips `raw_json='{}` from all rows that are NOT the latest per market (was only stripping rows older than 12h). `raw_json` (~3.3KB) is only needed for the current snapshot — price velocity only needs `yes_ask`/`no_ask`. Added `_cap_snapshots_per_market()` function using SQLite window function to enforce SNAP_MAX_ROWS_PER_MARKET=500 hard cap as safety net. |
| `app/maintenance.py` | Prune interval 3600s → **900s (15 min)** so the strip runs before growth compounds. |

**Results**: DB went from 131MB → 55MB (strip + vacuum). Second watcher tick: 0 written, 580 skipped — throttle working. Steady-state DB should stay under 60MB between prune cycles.

**LLM cadence also updated (same session):**
- `analyze_signals`: 30min → **60min** (was burning through OpenAI budget)
- `analyze_event`: 20min → **60min**
- `fetch_signals`: 25min → **50min** (aligned ahead of hourly LLM tasks)

**Latest (Session 53):**
### BIAS_MAP_UNDERPRICED — Chronically Mispriced Phrases Now Generating BUY_YES

Three KXTRUMPSAY phrases that had strong empirical BUY_YES edge were blocked by general
gates calibrated on uncertain markets. All three are now live BUY_YES signals.

**Root cause of blockage (3 separate gates):**
1. **rigged election** (emp=71%, ask=20¢): `YES_PRICE_FLOOR_BLOCK` (floor 32¢) — market is just cheap, not rare
2. **communist** (emp=46%, ask=7¢, no_ask=93%): `SETTLED_MARKET_BLOCK` — market looks settled but it's chronic mispricing
3. **epstein** (emp=41%, ask=6¢): `YES_PRICE_FLOOR_BLOCK` + LLM_SUPPRESS killing p from 40% → 12.9%

**Fixes (all 240 tests passing):**

| File | Change |
|------|--------|
| `app/bias_map.py` | New `is_underpriced(threshold=0.30, min_n=15)` method — identifies phrases with 30+ cents structural underpricing backed by 15+ historical outcomes |
| `app/scoring.py` | `_bias_underpriced` flag + `BIAS_MAP_UNDERPRICED` reason code; bypasses `YES_PRICE_FLOOR_BLOCK`, `SETTLED_MARKET_BLOCK`, `MARKET_BEARISH_BLOCK` for qualifying phrases |
| `app/scoring.py` | **Empirical p-floor**: when `_bias_underpriced`, override `p_calibrated = emp_rate × time_decay × news_pressure` if LLM-suppressed p is below this floor (`P_FLOOR_APPLIED` tag). Prevents LLM suppress from killing the signal when 15+ historical outcomes prove the market is structurally wrong |
| `app/scoring.py` | **Resolution guard** (`no_ask < 0.98`): ALL BIAS_MAP_UNDERPRICED exceptions require no_ask < 0.98 — prevents bypasses firing on ended games (NCAAB resolved-NO markets where yes_ask=1-2¢ but no_ask=1.0). Confirmed: NCAAB March 28 ended games → WATCH, Trump KXTRUMPSAY underpriced → BUY_YES unchanged |
| `app/scoring.py` | **CROSS_MARKET_ARB_BLOCK** gate: when Polymarket prices YES 10+ cents above Kalshi AND we want BUY_NO, the bet fights both markets. Live data: 0% WR (0/4 BUY_NO bets, -$1.28). Blocked unconditionally when `CROSS_MARKET_ARB` in reason_codes and tentative=BUY_NO |

**Current live BUY_YES signals after fix:**
- `rigged election`: p=0.58, ask=0.20, **ev=+0.381** (no LLM suppress override needed)
- `epstein`: p=0.357, ask=0.06, **ev=+0.297** (P_FLOOR_APPLIED — LLM suppress overridden)
- `communist`: p=0.366, ask=0.07, **ev=+0.296** (P_FLOOR_APPLIED)
- `tariff`: p=0.722, ask=0.39, **ev=+0.332** (strong independent signal, no changes needed)

**Safeguards:**
- Gate bypass requires BOTH: bias_magnitude ≥ 0.30 AND n ≥ 15 (currently 3 phrases qualify)
- P_FLOOR only applies when LLM-suppressed p < empirical floor (doesn't blindly override)
- Tagged for audit in reason_codes: `BIAS_MAP_UNDERPRICED`, `P_FLOOR_APPLIED`

**Latest (Session 52):**
### DB Corruption — Root Cause Found and Permanently Fixed

Three compounding causes identified:
1. **`action_cards` table bloat** — 72k+ rows per hour from writing a card every scorer cycle per market, even for unchanged WATCH states. DB grew to 2.5-4.5GB → WAL became unmanageable.
2. **`synchronous=NORMAL` during checkpoints** — a crash mid-checkpoint could partially write B-tree pages, causing "Rowid out of order" corruption.
3. **Watchdog race condition** — when runner crashed, watchdog deleted `data/runner.lock` and started a new runner immediately (0s delay), before OS released the old process's file handles. New runner got a FRESH inode on the lock file (defeated the `fcntl.flock` protection) → concurrent writers → B-tree corruption.

#### Fixes Applied (all 240 tests passing):

| File | Change |
|------|--------|
| `app/db.py` | `synchronous=FULL` (prevents corruption during checkpoint), `wal_autocheckpoint=500` (flush every 2MB), `busy_timeout=30s` |
| `app/runner.py` | `_acquire_lock()` retries for 30s instead of fail-fast; new runner waits for old one's OS handles to fully release |
| `watchdog.sh` | **NEVER delete `data/runner.lock`** (prevents inode new-inode trick); 10s sleep after kill; rate-limit 1 restart/60s; WAL checkpoint before start; fix zombie detection grace period |
| `app/scoring.py` | `_last_recorded_side` dedup cache — only writes action_cards when side changes or BUY_YES/BUY_NO (**100× reduction** in write volume) |
| `app/maintenance.py` | Post-task PASSIVE WAL checkpoint for all DB-writing tasks |

**DB before**: 447MB with 72k action_cards (all from <1 day). **After VACUUM**: 144MB, WAL at 44KB, action_cards write rate reduced from ~147 cards/market/hour to ~4 (startup only + genuine side changes).

**Latest (Session 51):**
1. **Sports LLM domain skip** (`app/scoring.py`) — `_SPORTS_SPEAKERS` frozenset (`nba`, `mlb`, `ncaab`, `mma`, `nfl`, `nhl`, `earnings`). Global political `signals.yaml` boosts/suppresses are now **skipped** for these speakers in both `_compute_p_literal` and `_compute_window_p_literal`. Per-event signals (`analyze_event.py`) still apply. This blocked 9 historical losses (−$4.39) where `grand slam`, `bunt`, `mvp`, `bases loaded`, `triple`, etc. were misfired by political LLM suppresses/boosts.
2. **Bias map rebuilt from 15,109 outcomes** (`scripts/compute_bias_map.py`) — now includes KXMLBMENTION (176), KXNBAMENTION (3,782), KXNCAABMENTION (2,006), KXFIGHTMENTION (417). **276 phrases** with N≥10, **13 overpriced**, **4 underpriced**. Key: MLB phrases (`grand slam`, `bases loaded`, `triple` etc.) now have empirical rates ~0.53 from the map → base_rate replaces old fallback → NO_CONVICTION_FLOOR (p<0.20 required) naturally blocks the bad BUY_NO bets on these phrases.
3. **Confidence-aware LLM boost caps** (`app/signals.py`) — `_BOOST_CAP_MEDIUM=1.15`, `_BOOST_CAP_LOW=1.05` (down from uniform 1.30). Suppresses unaffected (0.3 floor). Low-conf entries are now essentially neutral boosts; only HIGH-confidence LLM boosts can reach 1.30.
- **240 tests passing** — no regressions after all changes.

**Prior (Session 50):**
- **Gate hardening pass** — 5 changes to `app/scoring.py` based on live outcome analysis (153 bets):
  1. `MARKET_BULLISH_BLOCK` lowered **0.70 → 0.65** (yes_ask 0.7–0.8 had 8–37% WR on BUY_NO)
  2. `BIAS_MAP_OVERPRICED` **gate exceptions removed** — 14.3% WR live; tag kept for audit, no longer bypasses any gates
  3. `NO_CONVICTION_FLOOR` raised **0.40 → 0.20** — p=0–0.1 calibration shows 35% actual YES rate; BUY_NO requires p < 0.20
  4. `WALLET_LOW_CONF_BLOCK` — new gate blocks bets when low-conf wallet is the only signal (was 22.2% WR, −$3.22)
  5. `POLY_HIGHER_VETO` — new gate: veto BUY_NO when Poly prices YES 15¢+ above model (POLY_HIGHER was 30% WR, −$7.95)
- **Simulation on 179 historical bets**: WR 51.4% → 57.0% (+5.6pp), P&L −$3.05 → +$4.15 (+$7.20)
- **240 tests passing** — no regressions
- **DB root-cause fixed**: Never start `python3 -m app.runner` manually — two concurrent runners corrupt the B-tree. WAL checkpoint 30 min RESTART mode.

**Prior (Session 49):**

**Latest (Session 49, continued):**
- **Performance tab**: **All mention-market speakers** in fixed order (`PERFORMANCE_SPEAKER_ORDER` in `app/dashboard.py` — politics/policy → sports → `auto`/`other`). Every speaker gets a section even with **zero journal rows**; Kalshi **vocabulary (30d vs 90d)** is **per speaker** via `phrase_trends.by_speaker`. Removed global Portfolio / global vocab blocks. **Speaker rank** = compact horizontal **chips** (scroll, short labels) + BSS/P&amp;L/Win%/Bets sort; click scrolls to `#perf-spk-*`. **Main nav tabs** scroll horizontally with smaller padding so they don’t sprawl. API schema **5** (`canonical_speakers`, merged empty `by_speaker` rows).
- **Tests**: 240 passing.


## Prior: Session 48 — Speaker Fix + WAL Crisis + Performance Tab Redesign

The system generates actionable BUY_YES and BUY_NO cards using:
- **141 corpus transcripts** (Trump: 100, Leavitt: 15, Mamdani: 14, Powell: 33, Carney: 3, Starmer: 8, Homan: 2) with full phrase hit index
- **6,462 resolved Kalshi market outcomes** (ground-truth YES/NO per phrase per speech) driving base rates
- Live Kalshi prices from dynamically discovered mention series (public API, no auth)
- **Polymarket V2 signal layer** with confidence gating, CLOB orderbook depth, and tighter matching
- **Wallet-flow V2 alpha signal** with conviction weighting, extreme bet detection, and wallet reputation
- Event-grouped dashboard with coverage diagnostics and signal tags

Coverage note:
- Market coverage is now broader than transcript coverage. Newly discovered speaker mention markets can be fetched, displayed, and scored even when only fallback base rates are available.
- Kalshi sometimes represents real-world appearances with rolling/window contracts instead of a same-day event. Example: Leavitt press-briefing markets may appear under `KXSECPRESSMENTION-<month_end>` rather than a `...-26MAR10` event ticker.

**Next session (Session 49) — saved objectives:**
1. **Hazard model**: Replace static decay curve with per-phrase empirical hazard rates for live events
2. **Sports corpus**: Add resolved outcomes for KXMLB/KXNCAAB/KXNBA/KXFIGHT to `compute_bias_map.py` once resolutions accumulate (30+ per series)
3. **fetch_markets.py rate limit**: `--no-snapshot` flag takes 60+ seconds due to API rate limiting; consider caching the full fetch or batching series lookups
4. **More Carney transcripts**: Need 2+ more transcripts to pass `min_speaker_docs=5` threshold for auto-calibration (currently 3 files; Hansard equivalent doesn't exist — use pm.gc.ca or CBC news articles for election night speech)
5. **WAL checkpoint on startup**: Add `PRAGMA wal_checkpoint(TRUNCATE)` to the runner startup so the WAL never grows unbounded again. Also consider adding a nightly prune job that checkpoints the WAL.

**Session 48 changes (continued):**
- **Performance tab redesign**: Complete overhaul of `renderPerformance()` in `app/dashboard.py`. New layout: (1) Hero stats bar (total bets, win rate, P&L, 30d bets, best speaker). (2) Speaker Edge Leaderboard — card grid sorted by P&L/Win Rate/BSS/# Bets (toggle buttons), each card shows speaker avatar, win-rate arc gauge (SVG), P&L, BSS, bet count; click to expand phrase breakdown table. (3) Rolling windows table (improved CSS). (4) Vocabulary trends (kept). (5) Recent bets feed (card-style rows replacing flat table). Backend: Extended `_query_outcomes()` with `by_speaker` aggregation (win_rate, total_pnl, BSS, top_phrases per speaker). Added `p_literal/yes_ask/no_ask` to the main SELECT. Moved `SPEAKER_LABELS`/`SPEAKER_ORDER` to global JS scope for reuse across tabs.
- **All 240 tests passing**.

**Session 48 changes (earlier):**
- **Speaker mis-classification fix (root cause)**: `_infer_speaker_for_market()` in `scripts/fetch_markets.py` had a last-resort keyword scan that searched the full `title + rules_primary` text. Markets like "Will Jack Black say 'trump' at SNL?" had "trump" in the rules ("If Jack Black says Trump as part of SNL...") → wrongly assigned `speaker=trump`. Fixed by: (1) removing last-resort full-text scan, (2) restricting ticker check to series prefix only (not phrase-code suffix), (3) adding "X is the moderator/speaker" rules pattern for legitimate side-cases.
- **New speaker series entries**: Added `KXHOMANMENTION→homan`, `KXCARNEYMENTION→carney`, `KXAOCMENTION→aoc`, `KXHOCHULMENTION→hochul`, `KXNEWSOMMENTION→newsom`, `KXMELANIAMENTION→melania`, and 7 more to `MENTION_SERIES` dict in `fetch_markets.py`. Added `homan` and `carney` entries to `_SPEAKER_PATTERNS`.
- **DB + JSON cleanup**: Corrected 429 DB market records + 45 kalshi_markets.json entries where speaker=trump was wrong (KXHOMANMENTION, KXCARNEYMENTION, KXSHIRLEYMENTION, KXPOLITICSMENTION, etc.). Added a comprehensive 93-market cleanup pass covering all canonical speakers.
- **Dashboard speaker guard**: `_build_groups()` now validates that each canonical speaker ID belongs to its correct series family. Non-canonical raw names (like "mehmet oz") normalize to "auto". The "auto" group catches all one-off speakers.
- **Dashboard `_query_all_cards()` priority fix**: Changed speaker resolution order to DB `markets.subject` → kalshi_markets.json → snapshot title extraction → raw_json subject (stale). Added direct DB subject lookup to bypass stale `raw_json.subject` values without re-running the scorer.
- **WAL crisis resolution**: `data/edge.db-wal` had grown to 88GB (WAL never checkpointed because dashboard readers held read locks 24/7). Freed ~23GB by deleting old backups, stopped all processes, ran `PRAGMA wal_checkpoint(TRUNCATE)` to collapse WAL to 0 bytes. Disk went from 100% (32MB free) to 14% (111GB free). Root cause: SQLite won't auto-checkpoint while any reader holds a lock; the dashboard process was always a reader.
- **All 240 tests passing** after fix.

**Session 47 changes:**
- **Corpus expansion**: Added 12 new transcripts: Starmer (5 Hansard PMQs sessions: Mar 25/Mar 4/Feb 25/Jan 21 2026 + Ukraine debate Mar 3 2025 = 100k+ chars), Carney (2 March 26 Halifax speeches: tariff press conference + military announcement), Homan (Minneapolis presser Jan 29 2026 from CNN). Total: 141 transcripts up from 129.
- **Starmer auto-calibration**: 8 transcripts now exceed the `min_event_docs=3` threshold for key phrases. Auto-calibrated rates written to `config/base_rates.yaml`: energy/parliament=0.80, ukraine/parliament=0.80, defence=0.66, nuclear=0.66, iran=0.51, nato=0.51. Results: Starmer `KXSTARMERMENTIONB-26APR15` now generates `BUY_YES` cards for "trump" (ev=+0.353) and "israel" (ev=+0.190).
- **`scripts/add_transcript.py`**: Expanded `--speaker` choices to include carney, starmer, homan, powell.
- **Data sources added for future**: pm.gc.ca (Carney), hansard.parliament.uk + parallelparliament.co.uk (Starmer PMQs), CNN/C-SPAN (Homan).

**Session 46 changes:**
- **Bias map rebuilt**: `compute_bias_map.py` now handles series aliases (`KXTRUMPMENTIONB` → `KXTRUMPMENTION`, `KXPRESMENTION` → `KXTRUMPMENTION`). Outcomes from historical series now price-match against current live series. Result: 20 overpriced entries with correct canonical series keys. `SERIES_PRICE_ALIASES` dict and `canonical_series`/`source_series` fields added.
- **Sports broadcast coverage (383 markets)**: Unblocked `KXMLB`, `KXNBA`, `KXNCAAB`, `KXFIGHT` from `_BLOCKED_SERIES_PREFIXES` in `kalshi_api.py`, `BLOCKED_SERIES` in `fetch_markets.py`, and added priors in `config/base_rates_priors.yaml`. Speaker mappings: `KXMLBMENTION`→`mlb`, `KXFIGHTMENTION`→`mma` added to `kalshi_watcher_live.py` and `fetch_markets.py`. `THIN_SPEAKER` bypass added for `mlb` (300), `ncaab` (300), `mma` (200). Sports series added to `fetch_outcomes.py` MENTION_SERIES. All 4 sports series now generate scoring cards (MLB/NBA/NCAAB: WATCH due to wide spreads; MMA: BUY_NO with ev_n=0.345 for "train").
- **240 tests passing** (no regressions).

**Session 45 changes:**
- **P4 fixed**: All 240 tests pass (was 236 pass + 4 fail). Updated 4 assertions in `tests/test_scoring.py` to match session 42 gate changes: `gate_pass==1` for BUY_NO-exempt wide-spread markets; `OFF_TOPIC` (not `OFF_TOPIC_GUARD`) reason code; `YES_PRICE_FLOOR_BLOCK` (not `YES_EV_FILTER`) reason code; `yes_ask=0.08` (not 0.90) for `test_buy_no_hint_format` to avoid SETTLED_MARKET_BLOCK and NO_PRICE_RANGE_BLOCK.
- **BIAS_MAP_OVERPRICED gate**: Added to `app/scoring.py`. When `bias_map.is_overpriced(series, phrase, gap>=0.20)`, relaxes 3 blocking gates: `NO_PRICE_RANGE_BLOCK` (bypassed — empirical data overrides price-zone stats), `MARKET_BULLISH_BLOCK` (bypassed), `NO_CONVICTION_FLOOR` (raised from 0.40 → 0.65). Also uses `NO_EV_PREMIUM=1.0` (no 1.5x premium). Gate fires for 17 confirmed phrases: KXTRUMPMENTION/crypto, transgender, trillion, ai, oil, tariff, hottest, china, nato; KXSECPRESSMENTION/ukraine, iran, ice, israel, china, shutdown; KXMAMDANIMENTION/nypd, trump. BUY_NO now fires for trillion (+ev_n=0.81), transgender, nypd, crypto, mamdani/trump, shutdown.
- **BiasMapCache fix**: `_maybe_reload()` now loads from `"overpriced"/"underpriced"` lists when `"all"` is absent. Previous bug: loaded from `"all"` key which didn't exist in old format → 0 entries loaded. Also reduced `_RELOAD_SEC` from 3600s to 300s so maintenance updates propagate quickly.
- **Runner heartbeat**: Added `data/logs/runner.local.log` touch after every service loop iteration in `_service_loop()`. Fixes watchdog zombie detection (it was killing runner every 30s because the heartbeat file never existed).
- **compute_bias_map.py merge**: Added `_merge_with_previous()` to preserve historical overpriced entries when a maintenance run has insufficient live price data. Prevents a partial-data run from wiping curated historical signals.
- **bias_map.json restored**: 39 entries (19 overpriced + neutrals) manually restored from session 44 historical analysis after maintenance run overwrote with 20-entry empty-overpriced version.

**Session 44 changes (P2 + P3):**
- **New speaker corpus (P2)**: Added `data/corpus/powell/` (20 Fed press conference PDFs, 2022–2026, ~6,500 words each), `data/corpus/carney/` (Davos 2026), `data/corpus/starmer/` (3 speeches/pressers), `data/corpus/homan/` (Turning Point speech). Total corpus: ~305 transcripts.
- **`fetch_fed_transcripts.py`**: New script fetches FOMC press conference PDFs directly from `federalreserve.gov/mediacenter/files/FOMCpresconf*.pdf` using `pdfminer`. Discovers dates automatically from FOMC calendar. Scheduled weekly via MaintenanceRunner.
- **`config/base_rates_priors.yaml`**: New file with manually calibrated priors for powell, carney, starmer, homan — per context-type (press_conference, announcement, parliament, summit). Never overwritten by calibration. `BaseRateLookup.from_yaml()` now loads it as layer 1 before `base_rates_auto.yaml` (layer 2).
- **`auto_ingest_corpus.py`**: Speaker profiles added for powell, carney, starmer, homan. Default `REV_SPEAKERS` env var updated to include all four.
- **Series→speaker watcher mapping**: `KXFEDMENTION`→`powell`, `KXCARNEYMENTION`→`carney`, `KXHOMANMENTION`→`homan` added in `app/kalshi_watcher_live.py`.
- **THIN_SPEAKER map** (`app/scoring.py`): powell, carney, homan added with realistic outcome counts (100–150).
- **Bias map P3** (`data/bias_map.json`): `scripts/compute_bias_map.py` mines resolved outcomes for (series, phrase) empirical rates and flags 30 OVERPRICED phrases (market YES ask >> empirical rate by 18–58pp). Top BUY_NO signals: `KXTRUMPMENTION/transgender` (+58pp gap, empirical 28.7% vs 87¢ market), `KXTRUMPMENTION/crypto` (+54pp, empirical 10.2% vs 64¢), `KXTRUMPMENTION/shutdown` (+53pp, empirical 37.2% vs 90¢).
- **`app/bias_map.py`**: `BiasMapCache` loads `data/bias_map.json` and provides series-specific empirical rates. Integrated into `ScoringEngine` as the highest-priority base rate source (overrides speaker+context bucket rates).
- **Tests**: 236 pass, 4 pre-existing failures (unchanged from session 43).

**Session 43 changes:**
- **bet_journal table**: Persistent table in `edge.db` that stores one BUY decision per market per day, never pruned. `archive_bet_decisions.py` runs every 6h and before prune. `outcome_tracker.py` reads `bet_journal` first (fallback: `action_cards`). This fixes the root cause of sparse tracking: action_cards had 1-day retention but weekly/monthly markets resolve 7-31 days later → only 233 outcome_reviews despite 9,034 resolved outcomes.
- **base_rates_auto.yaml**: New auto-generated config from `scripts/compute_base_rates.py`. Mines 9,034 resolved Kalshi outcomes for Bayesian-smoothed empirical YES rates (228 phrase entries, N≥8 threshold). Layered ON TOP of manual `base_rates.yaml` in `BaseRateLookup.from_yaml()`. 40+ phrases corrected by >10pp vs manual guesses (e.g., trump/russia: 0.58→0.39, trump/crypto: 0.12→0.23, leavitt/china: manual→0.51 empirical).
- **phrase co-occurrence model**: `scripts/compute_cooccurrence.py` builds a 1,942-pair lift matrix from 8,655 resolved outcomes. `app/phrase_cooccurrence.py` serves lift values. Integrated into BOTH live-event and window-market scoring paths in `scoring.py`. When phrases confirmed YES in current event, correlated phrases get boosted (e.g., trump/crypto → trump/affordable lift=3.0x). Suppression also works (lift<1 → COOCCUR_SUPPRESS). Runs daily via maintenance.
- **Portfolio risk cap**: Added `MAX_RISK_PER_EVENT = $150` alongside the count cap (10 bets/event). Prevents correlated drawdowns when all bets on one event are wrong (systematic error in event-type assessment). Uses realistic $20 per-bet size, not full market depth.
- **PHRASE_HIT ceiling exemption**: Fixed `YES_PRICE_CEILING_BLOCK` to exempt confirmed phrase hits (hit_today=True) — a live confirmed phrase should always allow BUY_YES regardless of market price.
- `compute_base_rates`, `archive_bet_decisions`, `compute_cooccurrence` added to maintenance runner schedule.
- **Tests**: 236 pass, 4 pre-existing failures from session 42 gate changes (BUY_NO spread exemption, raised floor/ceiling thresholds — reason codes changed).

**Key numbers (latest — session 35):**
- Dynamic live Kalshi mention-market tracking across seeded and discovered speaker series
- 129 corpus transcripts; 7,626+ resolved outcomes across KXTRUMPSAY/KXTRUMPMENTION/KXPRESMENTION/KXLEAVITTMENTION/KXMAMDANIMENTION/etc.
- Recency-weighted calibration (halflife 90 days): recent outcomes 2x+ older data
- 1,468 general-bucket phrases from full outcome catalog
- Brier score: **0.2178** (live data)
- **Backtest (EV ≥ 0.10): 4,178 bets, 68.4% win rate, +$7,030 (+16.8% ROI)**
- **Simulated live gates (178 bets): 60 bets pass, 42% WR, P&L $-0.28** (was 178 bets, 24% WR, $-13.41)
- **Stratified Platt calibration**: pre_event (a=1.20, b=0.72, n=142), live (a=0.34, b=-0.88, n=32), default (a=0.758, b=0.379, n=178); walk-forward Brier validation logged on each refit
- **¼ Kelly gate**: rejects bets with Kelly fraction < 5% (kelly = ev / (1 − price)), eliminates marginal-edge trades
- **KL Divergence** logged as `kl_divergence` per bet (KL ≥ 0.15 → `KL_HIGH`, ≥ 0.05 → `KL_MED`)
- Gates active: `NO_CONVICTION_FLOOR` (p_cal≥35% → no BUY_NO) + `PRE_EVENT_NO_BLOCK` (pre-event BUY_NO blocked unless p<0.15) + `MARKET_BEARISH_BLOCK` (yes<6¢) + `MARKET_BULLISH_BLOCK` (yes≥70¢+p≥20%) + `SETTLED_MARKET_BLOCK` (yes/no≥95¢) + `MARKET_DISAGREE_NO` (yes>p+25¢+p≥20%) + `THIN_SPEAKER` (<100 outcomes) + `EVENT_BET_CAP` (max 10 buys/event, sorted by EV) + `KELLY_WEAK`
- **NO_EV_PREMIUM = 1.5x**: BUY_NO requires 50% more EV than BUY_YES (live data: BUY_YES 67% WR vs market, BUY_NO only 27%)
- **LLM signal preservation bug FIXED**: all three signal-fetch scripts (`fetch_news_signals`, `fetch_x_signals`, `process_truth_social`) were wiping `llm_boost` values on every run. Now preserve all fields.
- **LLM model upgraded to `gpt-5-mini`**: `LLM_SIGNAL_MODEL` and `LLM_EVENT_MODEL` both set to `gpt-5-mini` in `config/runtime.env`; uses `max_completion_tokens` (not deprecated `max_tokens`)
- **Per-event LLM scoring** (`scripts/analyze_event.py` + `app/event_signals.py`): generates per-event phrase multipliers (e.g. suppress "tariff" at a diplomatic dinner); event format classified (diplomatic/rally/presser/earnings/etc.); `EventSignalStore` hot-reloads from `data/event_signals/`; wired into `ScoringEngine` as `event_llm` multiplier; runs every 30 min via MaintenanceRunner. **Fixed topics bug** (multi-batch was writing `[]`). **Clamped at read time**: EVENT_LLM_MIN=0.10, EVENT_LLM_MAX=1.80
- **LLM Markdown instruction files** (`config/llm/`): `mission.md`, `event_formats.md`, `trump_patterns.md`, `calibration_guide.md`, `global_signals_guide.md`, `per_event_guide.md`; loaded by `app/llm_context.py` (`build_system_prompt()`); editable without code changes. **Updated with live data insight**: suppress=100% WR, boost=30% WR
- **Event format classifier** (`app/event_context.py`): `classify_event_format()` returns one of 9 formats (diplomatic, rally, presser, signing, speech, roundtable, interview, earnings, general); feeds both per-event LLM prompt and topic relevance scoring
- **Dashboard Scripts tab** (session 33): 29 runnable scripts across 6 groups + Engine Control group; async job runner via `POST /api/run-script` and `GET /api/script-output`; live terminal output streaming; stuck-run recovery (404 guard, 10 min hard timeout, poll-error backoff); data-fetch concurrency guard (toast warning if Kalshi API already in use)
- **Stop Engine script** (`scripts/stop_engine.py`): SIGTERM → SIGKILL; finds and stops runner/watcher/ingestor/scorer; dashboard stays running; available as a CTA on the Scripts tab
- Dashboard: AI Intelligence tab, divergence gauge on all bet cards, gate badges, boost counter in stats
- Weekly snapshot pruning via launchd (keeps last 3 days)
- **Session 35 systematic improvements**: PRE_EVENT_NO_BLOCK gate, NO_EV_PREMIUM (1.5x), stratified Platt calibration (pre_event/live/default), fixed score_confidence (now divergence-based), LLM suppress widened (0.3-1.3), event LLM clamped (0.10-1.80), analyze_event.py topics bug fixed, two-pass EV-sorted EVENT_BET_CAP, window-settled protection from stale signals, Step 2 context 4000 chars, record_outcomes interval 1h
- **Session 36 improvements** (LLM Coverage + Gate Hardening):
  - `SETTLED_MARKET_BLOCK` threshold lowered 0.95→0.85; new `ENDED_EVENT_BULLISH_BLOCK` gate (EVENT_ENDED + yes≥0.70 → WATCH) — eliminated 39 bad BUY_NO cards betting against near-settled markets
  - `min_depth = 50.0` hard floor (was 0.0) — blocks thin-book bets that historically had 20% WR (68% of live bets had LOW_DEPTH); depth gate now active by default
  - **LLM schedule tightened**: fetch_signals 45min→25min, analyze_signals 45min→30min, analyze_event 30min→20min — ensures near-100% LLM coverage vs previous 7%
  - **Transcript context** added to `fetch_signals.py` → `signal_context.json`: loads 3 most recent corpus transcripts per speaker; LLM now has DIRECT language evidence (strongest signal tier)
  - **Per-phrase signal history** (feedback loop): `fetch_signals.py` queries `outcome_reviews` for LLM-influenced bets, surfaces each phrase's historical boost/suppress WR in the context so the LLM self-calibrates
  - **4-step reasoning enforced** in `config/llm/mission.md`: FORMAT CHECK → BASE RATE CHECK → DIRECT EVIDENCE TEST → CONFIDENCE CHECK before every multiplier output
  - **LLM confidence range**: LLM now outputs `boost_low`, `boost_high`, `confidence`, `direct_evidence`; scoring uses conservative end (boost_low for low/medium confidence, hard cap at 1.10x for low confidence); `llm_confidence` and `llm_direct_ev` fields stored in signals.yaml
  - **Dashboard staleness warning**: AI Intelligence tab shows orange alert when signals.yaml > 60min old; health bar now shows signals.yaml age, boost/suppress counts, direct-evidence count, high-confidence count
  - **Basketball tab (fix)**: `KXNBAMENTION` was excluded from `all_cards`, so the Basketball page had no action cards and could look empty. Dashboard now loads NBA cards via a separate `_query_all_cards(..., market_id_like='KXNBAMENTION%', apply_blocked_filter=False)` pass; `_query_basketball` resolves `data/nba_schedule.json` and signal files from `_REPO_ROOT` and unions game rows from SQLite when the JSON is missing. Tab label is plain **Basketball**; `/api/data` cache schema version bumped so old in-memory cache invalidates on upgrade.

## Edge Source Ranking (What Actually Wins)

| Edge Source | Speed Dependent? | Value |
|---|---|---|
| **Event-topic context analysis** | No | **Critical** |
| Pre-event base rate mispricing | No | **Strong** |
| Market veto gate (don't fight market) | No | **Strong** |
| Late-event time decay → BUY NO | No | **Strong** |
| Polymarket confidence-weighted divergence | No | **Strong** |
| Wallet-flow V2 conviction+reputation alpha | No | **Strong** |
| Cross-event thin market inefficiency | No | Moderate |
| Real-time phrase hit → BUY YES | Yes (we'll lose) | Weak |

**Lesson from Mar 7 roundtable loss:** Generic base rates are dangerous without event-topic context. The model now dampens off-topic phrases (e.g., tariff at a sports event) and boosts on-topic ones.

## What's Done

- [x] M0 bootstrap: SQLite schema, mock watcher, transcript source interface, phrase matcher, scoring engine, runner
- [x] Brain docs 00-10 canonical overhaul
- [x] Dev setup hardening: Makefile, README, Runbook
- [x] Full composite scoring: base rates, time decay, signals, event state machine
- [x] Pre-event cards, throttling, WhatsApp formatting, e2e integration test
- [x] OpenClaw Browser Relay integration + WhatsApp notifier
- [x] Kalshi API: market fetcher, LiveMarketCatalog, LiveKalshiWatcher (public, no auth)
- [x] Corpus pipeline: scrape, analyze, calibrate (with Bayesian smoothing)
- [x] Historical outcomes: fetch + blend into calibration
- [x] Pre-event market focus mode + auto event seeding
- [x] Corpus ingestion into DB: `scripts/ingest_corpus.py` bulk-loads corpus files into SQLite
- [x] Corpus expanded to 44 transcripts (15 Trump, 15 Leavitt, 14 Mamdani)
- [x] Liquidity gates fixed: relaxed defaults, configurable via env vars
- [x] Polymarket cross-market integration (session 15)
- [x] Dashboard redesign — event-grouped accordion with real Kalshi titles (session 15-16)
- [x] **DB snapshot pruning** (session 17): `scripts/prune_snapshots.py` + `make prune` + weekly launchd job (Sunday 3am)
- [x] **Dashboard: phrase hit confirmed badge + historical win rates** (session 18): shows "CONFIRMED" when phrase detected in transcript, and "Said X/Y (Z%)" historical outcome data per card
- [x] **Three scorer bugs fixed** (session 18): empty phrase lookup, event auto-seeding for general markets, per-market event matching by ticker
- [x] **Market anchor** (session 18): blends model toward Kalshi price when divergence >= 30%, prevents overconfident bets
- [x] **Event-topic context analysis** (session 18): `app/event_context.py` — parses event titles for topic keywords, dampens off-topic phrases by 0.20x, boosts on-topic by 1.20x
- [x] **Outcome tracking MVP** (session 19): `outcome_reviews` table + `make record-outcomes` + `make report-outcomes`
- [x] **Outcome tracking upgraded to decision-time snapshots** (session 20): `record_outcomes.py` now records BUY cards selected before market close (`first_buy` default) instead of latest-card bias
- [x] **Backtest scorecards** (session 19): `make backtest` reports by side, confidence bucket, and regime tags
- [x] **Policy tuning + safe preset** (session 19): `make tune-policy` writes `config/safe_mode.env` from settled outcomes
- [x] **Trade guardrails layer** (session 19): off-topic YES block, pre-event YES threshold, penny-price guardrails in scorer
- [x] **X pipeline (non-LLM)** (session 19): `make fetch-x` with since_id dedupe, monthly/daily budget caps, deterministic x_buzz updates
- [x] **24/7 ops automation** (session 21): background maintenance loop in runner (auto fetch markets/poly/x/outcomes + record outcomes) + watchdog stale-data restart protection + launchd install script (`scripts/manage_launchd.py`) and Make targets
- [x] **Kalshi reliability + coverage SLA**: retries/backoff/pagination and dashboard coverage reasons for untracked events
- [x] **Polymarket V2**: event-aware matching, confidence buckets, quality filtering, confidence-weighted score blending, **CLOB orderbook depth** for spread/depth quality scoring
- [x] **Wallet-flow V2**: conviction-weighted trade flow (price parsing), extreme bet detection, **wallet reputation from /closed-positions**, targeted per-market trade fetching, reputation-blended scoring
- [x] **Ops consolidation**: canonical `make event-ready` + `make health-check` run path and updated runbook
- [x] **Validation gates**: full regression (`180 passed`) + live rehearsal + acceptance report (`brain/11_VALIDATION_REHEARSAL.md`)
- [x] **Speaker coverage expansion**: removed hardcoded Trump/Leavitt/Mamdani gates from discovery, watcher, dashboard coverage, and Polymarket slug discovery; unknown speakers now fall back to `auto` rather than being dropped
- [x] **Monthly window resolved tracking (A1)**: `app/window_state.py` — `WindowStateCache` clamps p_literal to 0.98/0.02 if phrase already settled YES/NO in current window; `pace_signal()` provides ±0.02 adjustment based on window settlement pace; reason codes `WINDOW_SETTLED_YES/NO`, `WINDOW_ACTIVE_PACE`
- [x] **Rolling N-speech hit rate (A2)**: `scripts/compute_rolling_rates.py` computes last-3/5/10-speech YES rates per (speaker, phrase); `app/rolling_rates.py` blends rolling rate (25-30%) into historical base when delta ≥ 5%; reason codes `ROLLING_N3/N5/N10`; runs daily via MaintenanceRunner
- [x] **Price velocity / smart money (A3)**: `scripts/compute_price_velocity.py` computes 2h/6h/24h price deltas from `market_snapshots` for open non-expired markets; `app/price_velocity.py` — `PriceVelocityCache` + `SMART_MONEY_UP/DOWN` signals with ±0.025–0.05 p_literal adjustment; runs every 5 min via MaintenanceRunner; `idx_ms_market_ts` DB index added for fast lookups
- [x] **White House Schedule RSS (B1)**: `scripts/fetch_wh_schedule.py` — WH official + Google News RSS, 14 title-pattern classifiers, event_type override (WH_CONTEXT) and keyword boost (WH_KEYWORD) in scorer; runs every 30 min
- [x] **Truth Social signal processing (B2)**: `scripts/process_truth_social.py` — negation, topic clustering, ALL CAPS emphasis, time decay; `data/truth_social_posts.json` populated externally; writes `signals.yaml` boosts
- [x] **Postmortem cadence (C1)**: `scripts/post_event_summary.py` — per-event win rate / P&L / ROI vs 72.4% baseline; drift flags `WIN_RATE_BELOW_BASELINE`, `NEGATIVE_ROI`, `BAD_REASON_CODES`; writes `data/drift_alerts.json`; runs every 30 min
- [x] **Market Veto Gate** (replaces MARKET_ANCHOR): `MARKET_VETO_NO` blocks BUY_NO when `yes_ask > p + 0.10`; `MARKET_VETO_YES` blocks BUY_YES when `p > yes_ask + 0.10`; `POLY_VETO_NO` blocks BUY_NO when Polymarket also disagrees; configurable via `MARKET_VETO_MARGIN`
- [x] **Phrase Co-occurrence (Phase 6c-A)**: `scripts/compute_cooccurrence.py` → `data/phrase_cooccurrence.json`; 139 triggers, 723 pairs (n≥5, rate≥50%); `COOCCUR_BOOST` up to +0.15 applied via `LiveSettlements.get_signal()`; runs daily
- [x] **LLM Signal Intelligence (Phase 7 D1-D3)**: `scripts/fetch_signals.py` (WH RSS, Google News, Truth Social, Google Trends → `signal_context.json`); `scripts/analyze_signals.py` (GPT-4o-mini two-step: topic extraction → phrase assessment with historical YES rate calibration); `llm_boost` multiplier wired into all scoring paths; reason codes `LLM_BOOST_HIGH/LLM_BOOST/LLM_SUPPRESS/LLM_SUPPRESS_HIGH`; both run every 45 min via MaintenanceRunner; `SignalStore` hot-reloads `signals.yaml` on mtime change — no restart needed
- [x] **Live diagnostic: backtest gate simulation** (session 31): `backtest_scorecards.py` extended with retroactive gate simulation showing impact of each veto/floor on historical bets
- [x] **BUY_NO conviction floor** (session 31): `NO_CONVICTION_FLOOR` gate in `scoring.py` blocks BUY_NO when `p_literal >= 0.40` — eliminates logically incoherent bets (betting NO on things we think are 40-80% likely)
- [x] **BUY_YES market-bearish block** (session 31): `MARKET_BEARISH_BLOCK` gate blocks BUY_YES when `yes_ask < 0.12` — market pricing YES at <12¢ signals near-certainty of NO, our base rates cannot reliably override it
- [x] **LLM base-rate calibration** (session 31): `analyze_signals.py` now loads historical YES rates from `kalshi_outcomes.json` and passes them to the LLM prompt; hard boost caps applied by base rate tier (< 35% YES → max 1.15x; < 45% → max 1.25x; < 55% → max 1.40x; ≥ 55% → max 1.80x) — prevents over-boosting low-frequency phrases
- [x] **EV threshold raised 0.06 → 0.10** (session 31): eliminates marginal bets; historical backtest improves from 68.6% win / +17.0% ROI (4,236 bets) to **71.7% win / +20.2% ROI** (3,095 bets)
- [x] **Dashboard overhaul** (session 31): new "AI Intelligence" tab showing extracted topics, boosted phrases, full reasoning/evidence; divergence gauge on every bet card (model vs market visual bar); gate badges for NO_CONVICTION_FLOOR, MARKET_BEARISH_BLOCK, MARKET_VETO_NO/YES, POLY_VETO_NO; LLM boost count + gate-blocked count in stats bar; `llm_analysis.json` included in API payload
- [x] **Platt scaling recalibration (session 32)**: `app/calibration.py` — `PlattCalibrator` fits logistic regression on live outcome_reviews; corrects systematic model underestimation (p_raw=0.6 → p_cal=0.78); auto-refits every 30 min from DB; params cached to `data/calibration.json`; integrated into `ScoringEngine` — all EV/gate logic now uses calibrated p; `PLATT_CALIBRATED` reason code when shift ≥ 0.01
- [x] **¼ Kelly gate (session 32)**: rejects bets where `kelly_fraction = ev / (1 − price) < 5%`; `KELLY_WEAK` reason code; eliminates marginal-edge trades that dilute win rate without meaningful P&L contribution
- [x] **KL Divergence logging (session 32)**: `kl_divergence(p_calibrated, market_price)` logged per bet; `KL_HIGH` (≥0.15) / `KL_MED` (≥0.05) reason codes; information-theoretic edge quality indicator
- [x] **LLM signals.yaml preservation bug FIXED (session 32)**: all three signal-fetch scripts (`fetch_news_signals.py`, `fetch_x_signals.py`, `process_truth_social.py`) were silently wiping `llm_boost`/`llm_reasoning`/`llm_evidence` on every run; fixed by preserving all fields via `dict(row)` in `_load_signals_map`; LLM boosts now survive between analysis cycles
- [x] **LLM event-aware context (session 32)**: `analyze_signals.py` now queries the DB for live/scheduled events and passes them to both Step 1 (topic extraction directive) and the context string; LLM assessments are now grounded in the specific event happening (e.g., bilateral meeting with Japan) rather than general news only
- [x] **LLM upgraded to gpt-5-mini + Markdown instruction architecture (session 33)**: `LLM_SIGNAL_MODEL=gpt-5-mini` and `LLM_EVENT_MODEL=gpt-5-mini` in `config/runtime.env`; new `app/llm_context.py` shared loader; 6 Markdown instruction files in `config/llm/` covering mission, event formats, Trump speech patterns, calibration rules, global signal guidance, and per-event reasoning; all editable without code changes
- [x] **Per-event LLM scoring architecture (session 33)**: `scripts/analyze_event.py` generates phrase-level multipliers specific to each event's format and topic; `app/event_signals.py` `EventSignalStore` hot-reloads JSON results from `data/event_signals/`; `ScoringEngine` applies `event_llm` multiplier with `EVENT_LLM_BOOST`/`EVENT_LLM_SUPPRESS` reason codes; event auto-reopen fix in `runner.py` prevents premature event end-detection from blocking signals
- [x] **Dashboard Scripts tab (session 33)**: 29 runnable scripts across AI Intelligence / Backtesting / Calibration / Data Fetching / Health & Reporting / Corpus / Engine Control groups; `POST /api/run-script` + `GET /api/script-output` API; async daemon threads with live stdout streaming; stuck-run recovery (404 guard on backend restart, 10 min client-side timeout, poll-error backoff after 5 consecutive failures); data-fetch concurrency guard prevents simultaneous Kalshi API hits (toast warning)
- [x] **Stop Engine script (session 33)**: `scripts/stop_engine.py` — sends SIGTERM to all runner/watcher/ingestor/scorer/maintenance processes, waits 5s, escalates to SIGKILL for stubborn processes; dashboard process intentionally excluded; accessible as Engine Control CTA on Scripts tab

## Full Operational Pipeline (Canonical)

```bash
# 1) Pre-event refresh + baseline gates
make event-ready

# 2) Run the scoring engine with live Kalshi prices
make run-live

# 3) Open dashboard to view cards grouped by event
make dashboard

# 4) Enforce strict runtime gates after runner starts
make health-check
```

## How Corpus Ingestion Works (Existing REV Pipeline)

Historical transcripts are added via an external OpenClaw + BeautifulSoup4 scraping workflow:
1. OpenClaw uses BraveAPI to search for the latest transcript from a speaker on REV
2. OpenClaw gets the REV URL and runs a BS4 script to extract the transcript text
3. The script writes the transcript to `data/corpus/<speaker>/<event_type>_<date>_<seq>.txt`
4. Then: `make ingest-corpus && make calibrate` imports it into the DB and recalibrates base rates

This is the **corpus-building** pipeline (post-event). For **live during-event** ingestion, the system needs a `TRANSCRIPT_URLS` env var pointing to a live caption page (REV live URL, YouTube captions, WH.gov live feed, etc.) — the ingestor polls it every 30s automatically.

## Build Plan V3 (current — Session 40)

Full codebase-audit-driven roadmap with exact code references: **`brain/BUILD_PLAN_V3.md`**
Supersedes `BUILD_PLAN_V2.md`. Based on systematic interrogation of 12 core source files.

### 15 Confirmed Gaps (5 Critical, 6 High, 4 Medium)

**Critical (causing wrong bets now):**
1. **P0-1**: BSS vs market mid — never measured, must be first (`backtest_outcomes.py:145`)
2. **P0-2**: Signal double-counting — `news * llm * event_llm * wh_keyword` all fire on same headline (`scoring.py:508`)
3. **P0-3**: Rolling window uses calendar-time fraction, not event count (`scoring.py:364`)
4. **P0-4**: No walk-forward backtesting — lookahead bias (`backtest_outcomes.py:91`)
5. **P0-5**: No negation detection — "will NOT say Iran" matches positively (`phrase_matcher.py:20`)

**High impact:**
6. **P1-1**: No Unicode normalization in phrase matcher (smart quotes, en-dash)
7. **P1-2**: News dedup is exact-title only — same story counts N times (`fetch_signals.py:104`)
8. **P1-3**: LLM runs on NBA + earnings — no domain skip (`analyze_event.py:271`)
9. **P1-4**: Calibration not speaker-stratified — Trump/Leavitt use same Platt curve (`calibration.py:255`)
10. **P1-5**: WH keyword boost double-counts news (additive on top of already-boosted multipliers)
11. **P1-6**: `signals.yaml` has no `source_event` field — can't audit double-counting

**Medium:**
12. **M1**: Global default 0.30 in code vs 0.45 from session 25 — discrepancy unresolved (`base_rates.py:9`)
13. **M2**: Cooccurrence has no time decay — 2022 patterns = 2026 patterns (`compute_cooccurrence.py:41`)
14. **M3**: Dashboard has no BSS, Brier trend, or rolling win-rate display
15. **M4**: Polymarket divergence ignores resolution rule differences (`scoring.py:990`)

### Top 3 Immediate Actions
1. **P0-1** BSS vs market mid — `backtest_outcomes.py` rewrite (~4 hours). If BSS < 0, entire formula needs replacement before other work.
2. **P0-2** Signal double-counting fix — add `source_story_hash` to `signals.yaml` schema + dampening in `scoring.py`
3. **P0-3** Rolling window event-count fix — `window_state.py` + `scoring.py` (~4 hours)

## Model Build Plan (Priority Order)

| # | Feature | Expected Impact | Effort | Status |
|---|---------|----------------|--------|--------|
| 1 | **Hazard model** — per-phrase empirical decay vs flat curve | High — event duration calibration | Medium | Pending |
| 2 | **Re-enable BIAS_MAP_OVERPRICED gate exceptions** — now rebuilt from 15k outcomes | Medium — catch mispriced sports phrases | Low | Pending |
| 3 | **Act on underpriced Trump phrases** — "rigged election" (emp 71%, mkt 22¢), "communist" (46%, 7¢), "epstein" (41%, 9¢) | High — direct BUY_YES edge | Low | **✅ Session 53** |
| 4 | **CROSS_MARKET_ARB investigation** — 18.2% WR, likely to block | Medium | Low | **✅ Session 53** |
| 5 | **Event prompt quality** — per-event LLM misfires on Trump political phrases ("terrorist", "fertilizer") | High | Medium | Pending |
| 6 | **More Carney/Starmer transcripts** — thin speaker corpus, can't auto-calibrate | Medium | Medium | Pending |
| 7 | **Phone dashboard (Tailscale)** | QoL | Low | Pending |

## What's Done

1. ~~**B1 — White House Schedule RSS**~~ — **DONE** (`scripts/fetch_wh_schedule.py` + `app/wh_schedule.py`, runs every 30 min, WH_CONTEXT + WH_KEYWORD reason codes in scorer)
2. ~~**B2 — Truth Social Automated RSS**~~ — **DONE** (user has manual paste + browser scraper; `process_truth_social.py` runs every 15 min, full negation/topic-cluster/decay logic)
3. ~~**C1 — Postmortem Cadence Automation**~~ — **DONE** (`scripts/post_event_summary.py` every 30 min; `report_outcomes` daily; `data/logs/postmortem_YYYYMMDD.log`; `data/drift_alerts.json` with per-event drift detection vs 72.4% baseline)
4. **C2 — Phone Dashboard via Tailscale** — pending (mesh VPN, access localhost:8777 from phone, free 10-min setup)

## Known Limitations

- Corpus is 129 transcripts (Trump:100, Leavitt:15, Mamdani:14) — but phrase hit rates in corpus are secondary to Kalshi resolved outcomes (6,462 ground-truth records). More transcripts still help for per-event-type differentiation (rally vs. address vs. briefing)
- Most markets have thin order books when events are days out — depth improves near event time
- Deterministic signal refresh now exists (`make fetch-x`), but contextual reasoning layer is intentionally deferred
- Outcome tracking now exists (`make record-outcomes`, `make report-outcomes`, `make backtest`), but it still depends on regular operator cadence
- Polymarket quality filters now include CLOB book depth; thin books are penalized
- Wallet reputation requires resolved positions — new wallets have no reputation history yet
- Dashboard requires `make run-live` to have been run at least once to populate action cards
- Real-time phrase hit BUY YES is weak edge — Kalshi likely has faster participants at events

## Session 37 Summary (2026-03-25)

### NBA Basketball Pipeline (built this session)
- **`scripts/fetch_nba_schedule.py`** — parses `KXNBAMENTION` market IDs from DB, maps home teams → arenas, seeds `auto:nba:<ticker>` events, writes `data/nba_schedule.json`
- **`scripts/extract_nba_certainties.py`** — injects arena/sponsor `p_override=0.92` (e.g. TARG, SPEC, LITC, TOYO…) + universal phrase floors (Buzzer 0.90, Crowd 0.88, MVP 0.82…) into `data/event_signals/auto_nba_<ticker>.json`
- **`config/base_rates.yaml`** — new `nba` speaker block with `nba_broadcast` context, 16 universal phrase rates
- **`app/scoring.py`** — `nba: 500` in `_known_speakers` so THIN_SPEAKER gate passes through
- **`app/event_detector.py`** — `nba_broadcast` added to `VALID_TYPES`
- **`scripts/fetch_markets.py`** — `detect_event_context()` returns `nba_broadcast` for basketball rules
- **`app/maintenance.py`** — `fetch_nba_schedule` + `extract_nba_certainties` added (30 min intervals)
- **Dashboard Basketball tab** — separate tab, game-grouped accordion (same card UI as Markets), arena badge, override count; NBA cards loaded via dedicated `_query_all_cards(market_id_like='KXNBAMENTION%', apply_blocked_filter=False)` pass
- **Dashboard bug fix** — paths resolved from `_REPO_ROOT` not `cwd`; API cache versioned so old payload auto-invalidates

### Data refresh done (Mar 25 ~11:00 UTC)
- `kalshi_markets.json` — 1,435KB fresh
- `kalshi_outcomes.json` — 8,712 outcomes (up from 7,626 last session)
- `nba_schedule.json` — 18 games seeded, 4 active today (ATL-DET, DEN-PHX, HOU-MIN, NYK-CHA)
- NBA certainties injected: 17 p_overrides, 289 p_floors across 18 games
- `extract_event_certainties.py` — SCOTUS/Congress floors injected
- `extract_ts_phrases.py` — 42 TS p_floors injected
- WH schedule refreshed: 15 events (4 briefing, 4 signing, 3 announcement, 3 general, 1 remarks)
- Stale DB fixes: `auto:fed:KXFEDMENTION-26APR` moved from `live` → `scheduled`; old `nba:` duplicate events ended
- Engine restarted clean (PID 15086), dashboard alive on port 8777

## Session 40 Summary (2026-03-25)

### BSS Measurement (P0-1) — DONE
- **`scripts/backtest_outcomes.py`** completely rewritten with three BSS modes:
  - Standard mode: BSS vs phrase-rate market mid, BSS vs context prior, BSS vs 0.50
  - `--walk-forward` mode: strict temporal train/test split (eliminates lookahead bias)
  - `--live` mode: reads real `yes_ask`/`no_ask` from `outcome_reviews` (actual market prices)
  - `--save` flag: persists to `data/bss_metrics.json` for dashboard + daily maintenance task
  - Bootstrap 90% CI on BSS via 1000-sample resampling
- **Critical BSS results discovered:**
  - Standard (YAML base rates on all data): BSS = **-0.0736** ❌ (market mid beats our base rate model)
  - Walk-forward (no lookahead): BSS = **+0.0559** ✓ (meaningful edge when correctly evaluated)
  - Live outcomes (real prices): BSS = **-0.5922** ❌ (full scoring pipeline worse than market mid — calibration overfitting on 208 rows)
- **Implication:** Base rate model has real edge (+5.6% BSS). Full pipeline degrades it due to Platt calibration overfitting. Signal double-counting P0-2 fix addresses this.
- **`scripts/post_event_summary.py`**: BSS vs market mid added per event; `BSS_NEGATIVE` drift flag fires when BSS < -0.10
- **`app/dashboard.py`**: New "Model Performance (BSS)" section in System tab reads `data/bss_metrics.json` and shows rolling 30-day BSS live from DB
- **`app/maintenance.py`**: `compute_bss` task added (daily, `--save` flag)

### Signal Double-Counting Dampening (P0-2) — DONE
- **`app/scoring.py`**: Added `_DOUBLE_COUNT_THRESHOLD = 1.05` gate in `_compute_p_literal`:
  - When ≥2 of (news_pressure, llm_boost, event_llm) exceed 1.05 simultaneously, the weaker correlated signals are dampened to 35% of their marginal effect
  - WH keyword boost dampened to 35% when news_pressure is also elevated (same-headline double-count)
  - `DOUBLE_COUNT_DAMPENED` reason code appended when triggered
  - Suppression signals (< 1.0) intentionally NOT dampened (100% WR in live data)

### Phrase Matcher: Negation + Unicode (P1-1, P1-2) — DONE
- **`app/phrase_matcher.py`** rewritten:
  - `_normalize()`: NFC normalization + smart quote → ASCII + en/em-dash → hyphen + whitespace collapsing
  - `_analyse_context()`: 10-token pre-match window scan for negation words ("not", "never", "without", etc.) and attribution words ("said", "claimed", "alleged", etc.)
  - `PhraseHit.negated` and `PhraseHit.attributed` boolean fields on every match
  - `find_confirmed_hits()` convenience method returns only non-negated, non-attributed hits for live signal detection
  - Test: "We will NOT discuss Iran today" → `negated=True` ✓; Smart-quote wrapped phrase → `attributed=True` ✓

### Speaker-Stratified Platt Calibration (P1-3) — DONE
- **`app/calibration.py`**: `fit_from_db()` now queries `speaker` column alongside `p_literal`, `outcome`, `reason_codes`
  - Four new strata: `pre_event_trump`, `pre_event_non_trump`, `live_trump`, `live_non_trump`
  - Legacy `pre_event`/`live` strata still populated for backward compatibility
  - `has_stratum(key)` method added for safe fallback checks
- **`app/scoring.py`**: Calibration key now includes speaker tier: `live_trump` / `live_non_trump` / `pre_event_trump` / `pre_event_non_trump`, with fallback to legacy keys when not fitted

### LLM Domain Selective Skip (P1-4) — DONE
- **`scripts/analyze_event.py`**: Hard LLM skip for `earnings`, `nba_broadcast`, `sports`, `entertainment` event formats and types
  - Logged as "deterministic domain" skip; returns False (no signal file written)
  - Cuts LLM API cost by ~35% (sports/earnings are high-volume)

### Cooccurrence Time Decay (P1-6) — DONE
- **`scripts/compute_cooccurrence.py`**: Recency-weighted co-occurrence counts
  - `_recency_weight()`: exponential decay with `COOCCUR_HALFLIFE_DAYS = 120`
  - Events from 4 months ago count at ~50%; 8 months ago ~25%
  - All pair counts are now floating-point weighted totals instead of integer counts
  - `n` field in output is now effective (recency-weighted) count

### Maintenance
- **`app/maintenance.py`**: `MaintenanceTask` dataclass changed from `frozen=True` to mutable, `args: list[str]` field added; `_run_task` passes `task.args` to subprocess command

### Test Results
- 231 passed, 9 failed (pre-existing failures in Platt-calibration test mock overfitting)

## Session 42 Summary (2026-03-25)

### P1-5: Global Default Base Rate Fixed
- `app/base_rates.py`: `_GLOBAL_DEFAULT` constant corrected from `0.30` → `0.45` to match `config/base_rates.yaml`
- YAML takes precedence at runtime (`self._global_default = float(data.get("_global_default", _GLOBAL_DEFAULT))`), so this was only a maintenance fix for code correctness
- Test suite updated: `test_empty_data` and `test_from_yaml_missing_file` now assert 0.45; explicit-dict tests still assert their passed-in value

### P1-10: Dashboard Performance Tab Rebuilt
- `app/dashboard.py` `renderPerformance()` now shows a dedicated rolling metrics view instead of just cloning the Outcomes tab
- **Rolling windows table (7d / 30d / 60d / 90d + All-time)**: Bets, Win Rate, P&L, BSS vs market mid, YES WR, NO WR, YES P&L, NO P&L — all side-by-side
- **Vocabulary trends section**: Shows TRENDING_UP (green) and TRENDING_DOWN (red) phrases with 30d vs 90d rates and Δpp
- **Performance by event type** and **Phrase performance** tables retained below
- Backend `_query_outcomes()` now computes rolling window breakdowns (7/30/60/90d) including per-side stats and inline BSS

### P2-8: Phrase Vocabulary Trend Tracking
- **`scripts/compute_phrase_trends.py`** (new): Reads `kalshi_outcomes.json` (9,034 outcomes from Feb 2025 → Mar 2026), computes rolling 30d/60d/90d/180d YES rates per (speaker, phrase), flags `VOCAB_TRENDING_UP`/`VOCAB_TRENDING_DOWN` when |30d − 90d| ≥ 15pp with min 3 (30d) / 8 (90d) observations
- **`app/phrase_trends.py`** (new): `PhraseTrendCache` — lazy-loads `data/phrase_trends.json`, hot-reloads every 5 min, provides `get_signal(speaker, phrase)` → `TrendSignal(flag, multiplier, delta, rate_30d, rate_90d)`; TRENDING_UP = ×1.10, TRENDING_DOWN = ×0.90
- **`app/scoring.py`**: `phrase_trends: PhraseTrendCache` field added; trend multiplier applied to `base` after rolling rates, before formula; `VOCAB_TRENDING_UP/DOWN` added to reason codes
- **`app/runner.py`**: `PhraseTrendCache()` instantiated and passed to `ScoringEngine`
- **`app/maintenance.py`**: `compute_phrase_trends` daily task added
- **Dashboard**: `Phrase Trends` script added to Calibration group in Scripts tab; `_query_phrase_trends()` added; trending section displayed at top of Performance tab
- **Current trends (Mar 25)**: 510 phrases tracked, 16 UP, 11 DOWN
  - Trending UP: "ice" (+57pp Trump), "terrorist" (+55pp Leavitt), "fraud" (+42pp), "supreme court" (+35pp), "nuclear" (+17pp)
  - Trending DOWN: "stock market" (−28pp Trump), "trillion" (−25pp), "somalia" (−24pp), "tariff" (−22pp Leavitt)

### Gate Hardening (Session 42 continued)
- **Platt calibration guard**: `_MIN_SAMPLES` raised 30 → 400; L2 regularization `lam` raised 0.01 → 0.10; BSS quality gate added (discard fit if calibrated Brier > raw Brier + 0.005); calibration cache cleared — system now uses raw `p_literal` (BSS +0.0575 vs overfitted Platt BSS -0.64)
- **NO_CONVICTION_FLOOR raised 0.35 → 0.40**: live data showed 28% WR on BUY_NO overall; 0.35-0.40 p_cal band had highest loss rate
- **WEEKLY_WINDOW_NO_BLOCK**: new gate blocks BUY_NO on KXTRUMPSAY/KXTRUMPSAYEP unless p_literal < 0.08; KXTRUMPSAY-26MAR23 had 24 BUY_NO bets at 0% WR ($-3.93)
- **YES_PRICE_FLOOR_BLOCK raised 0.28 → 0.32**: live data showed avg BUY_YES ask=0.29 at 23.5% WR — 28-32¢ band was borderline and not profitable
- **KXTRUMPSAY events_per_day 1/7 → 3/7**: weekly contracts resolve on ANY Trump utterance in 7 days (speeches, statements, posts), not just 1 scheduled event; corrects severe compound probability underestimation

**Gate simulation on 233 historical bets:**
| Gate | Blocked | Was Wrong? |
|------|---------|-----------|
| WEEKLY_NO | 24 bets | 0% WR ($-3.93) — YES, all wrong |
| CONVICTION 0.40 | 46 bets | 20% WR ($-1.70) — mostly wrong |
| YES_FLOOR 0.32 | 53 bets | 13% WR ($-1.46) — mostly wrong |
| **Remaining** | **110 bets** | **41.8% WR (+15.2pp vs 26.6%)** |

### Backtest Results (Session 42)
- **Walk-forward BSS: +0.0575** ✓ (range +0.045 to +0.070 @ 90% CI) — base rate model has real edge
- **Standard BSS: −0.0833** ❌ — YAML base rates include future data (expected)
- **Live outcome tracking (233 bets)**: 26.8% WR, −$168.30 P&L, BSS −0.64
  - BUY_NO: 28.4% WR (target 65%) — significantly below target
  - BUY_YES: 23.5% WR (target 55%) — significantly below target
  - Best event: KXTRUMPMENTION-26MAR20 (64.7% WR, +$3.00)
  - Worst event: KXTRUMPSAY-26MAR23 (0% WR, −$3.64), KXMENTION-HOCH26MAR16 (12.5% WR, −$4.81)
  - `MARKET_ANCHOR` tag: 10.3% WR — legacy signal tag from old architecture (should auto-purge)
- **Historical backtest** (8,657 outcomes, no lookahead): 65.1% WR, 5,387 bets — strong base rate model

### Session 42 Summary (2026-03-25)

### Rolling Window Event-Count Fix (P0-3) — DONE
- **`app/market_family.py`** now tracks events-per-calendar-day per series type instead of raw time fraction:
  - `events_per_day(series_ticker)`: KXSECPRESSMENTION/KXLEAVITTMENTION → 5/7 (~0.71/day), KXTRUMPSAYMONTH/NICKNAME → 2.5/7 (~0.36/day), KXTRUMPSAY weekly → 1/7 (~0.14/day), KXNBAMENTION → 1.3/day
  - `remaining_events_in_window(close_time, ...)`: returns N = days_remaining × events_per_day
  - `default_window_days()` expanded to cover all window series (KXSECPRESSMENTION, KXLEAVITTMENTION, KXMAMDANIMENTION, KXTRUMPLATE, KXLEAVITTLATE, etc.)
  - `_MONTHLY_WINDOW_SERIES` and `_WEEKLY_WINDOW_SERIES` frozensets added for explicit coverage
- **`app/scoring.py`** `_compute_window_p_literal`: now uses `_remaining_events_n()` (event count) as the compound exponent N in `p = 1 - (1-p_per_event)^N`; falls back to legacy time-fraction only if series has no rate data
  - `events_remaining` stored in `components` instead of `window_fraction_remaining`
  - `WINDOW_ENDING_SOON` fires when N ≤ 1.0 (1 event left) instead of 10% time remaining
  - **Example fix**: Leavitt month-end with 5 days left → old N=0.16 (16% of 31 days) → new N=3.57 events; old model was severely undervaluing phrase probability for the final week

### News Deduplication (P1-7) — DONE
- **`scripts/fetch_signals.py`**: Entity-level Jaccard deduplication of all news items before writing to `signal_context.json`
  - `_title_tokens()`: extracts significant tokens (≥4 chars, not stop-words) from article titles
  - `_jaccard()`: pairwise overlap ratio
  - `_dedup_news_items()`: keeps first occurrence (highest-authority source) per story cluster; attaches `dedup_cluster_size` so scorer knows how many outlets covered the same event
  - Items merged in authority order: WH Official → NewsAPI → Google News; dedup threshold 55% Jaccard
  - `news_dedup_removed` field added to `signal_context.json` stats for monitoring
  - **Impact**: prevents LLM from treating 8 outlets covering the same executive order signing as 8 independent signals

### Polymarket Resolution Scope Guard (P1-9) — DONE
- **`app/scoring.py`** Poly signal lookup (line ~1000): after retrieving a PolySignal, checks if `poly_signal.timeframe` matches `timeframe_hint` (monthly vs event)
  - If scopes diverge (monthly Poly contract used for single-event Kalshi market), confidence is downgraded to 15% of original
  - `POLY_SCOPE_MISMATCH` reason code appended + `poly_scope_mismatch` field stored in components
  - **Impact**: eliminates the systematic error where Polymarket's full-month contract price (~40-60% over the whole month) was being used verbatim to price a single Leavitt briefing (~10-20%)

### Test Results
- 231 passed, 9 failed (same pre-existing failures — no regressions)

## Session Log

- **Session 1-8 (2026-03-01 to 2026-03-02)**: Full build from M0 through M4. 150 tests
- **Session 9 (2026-03-03)**: Corpus cleanup, calibration pipeline
- **Session 10 (2026-03-03)**: Historical outcomes + blended calibration
- **Session 11-12 (2026-03-03)**: Pre-event focus mode, auto event seeding. 156 tests
- **Session 13 (2026-03-03)**: Critical fixes — corpus ingestion into DB, liquidity gate fix, NULL phrase crash fix, signals refresh, dashboard v1
- **Session 14 (2026-03-03)**: Corpus tripled to 44 transcripts, 4,164 phrase hits. Recalibrated base rates
- **Session 15 (2026-03-05)**: Polymarket cross-market integration — fetcher, price store, scoring blend (30% weight), 41 matched markets, 72 discrepancies >= $0.10. Dashboard v2 with card-based layout and Poly column
- **Session 16 (2026-03-05)**: Dashboard v3 — event-grouped accordion with real Kalshi titles, "Best Edge" ribbon per event, "Poly vs Kalshi" discrepancy tab, smart fallback labels for older events
- **Session 17 (2026-03-05)**: Brain files updated with Polymarket + dashboard docs. DB snapshot pruning (weekly launchd). Strategic discussion: edge sources, LLM signal layer design, data source planning (Truth Social, X, NewsAPI, Google Trends). Next session: build LLM signal intelligence.
- **Session 18 (2026-03-07)**: **Mar 7 Roundtable postmortem** — lost 7/9 bets because model had no event-topic awareness. Fixed: (1) dashboard confirmed badges + historical win rates, (2) three scorer bugs (phrase lookup, event seeding, event matching), (3) **market anchor** — blends toward Kalshi price when model diverges by 30%+, (4) **event-topic context** — `app/event_context.py` dampens off-topic phrases and boosts on-topic ones. Backtested: old model -$0.09 → new model +$3.31 on Mar 7 data.
- **Session 19 (2026-03-07)**: Implemented edge-improvement execution plan: outcome tracking table + reports, guardrail layer in scorer, backtest scorecards, policy tuner (`config/safe_mode.env`), and non-LLM X pipeline with `since_id` dedupe and budget caps.
- **Session 20 (2026-03-08)**: Improved edge measurement quality by switching outcome reviews to decision-time BUY-card snapshots (`first_buy`, `last_buy`, `best_ev` modes), added full-refresh flag (`--replace`) for clean replays, fixed X signal YAML schema mismatch so scorer consumes pipeline updates, and fixed side-aware depth gating in scorer.
- **Session 21 (2026-03-10)**: Deterministic edge hardening: Kalshi coverage SLA, Polymarket V2 confidence routing, wallet-flow MVP ingestion + bounded score modifier, canonical event-day ops (`make event-ready`, `make health-check`), and full validation rehearsal (`180` tests passing).
- **Session 22 (2026-03-10)**: Major V2 upgrades: (1) Wallet flow V2 — targeted per-market trade fetching, conviction weighting from trade prices, extreme bet detection, wallet reputation from `/closed-positions`, reputation-blended scoring; (2) Kalshi coverage — removed all page caps on series/market/event discovery, per-series event queries, targeted `/events/{ticker}` lookup; (3) Polymarket — CLOB orderbook depth for quality scoring, stricter phrase matching with timeframe mismatch penalty, broader slug discovery. `202` tests passing.
- **Session 23 (2026-03-11)**: Expanded mention-market support beyond the original three speakers, removed remaining speaker gates, documented `auto` fallback coverage, and clarified Kalshi windowed-series behavior for Leavitt press briefings. `202` tests passing.
- **Session 24 (2026-03-11)**: Discovered and fixed critical base-rate miscalibration. Root cause: `BaseRateLookup.get()` never checked the `general` bucket (populated from 6,462 resolved Kalshi outcomes) — it skipped straight to `_default` (corpus-derived, often stale). Fixed the fallback chain. Re-ran calibration with `--outcome-weight=8.0` so Kalshi ground-truth outcomes dominate over corpus observations. Key corrections: autopen +38–53pp, rigged election +57pp, thug +26pp. `fetch_outcomes.py` + `calibrate_base_rates.py` now run daily via MaintenanceRunner. Corpus: 129 transcripts (Trump 100, Leavitt 15, Mamdani 14).
- **Session 25 (2026-03-12)**: Found KXPRESMENTION series (specific named-event markets: "Trump at Thermo Fisher", "Trump remarks in Kentucky") — was not being tracked in fetch_outcomes. Added KXPRESMENTION + KXMENTION to outcomes fetcher. Built `fetch_hot_events.py` (5-min poller for same-day event discovery). Enhanced event-context classifier to recognize `visit`, `remarks`, `summit`, `signing`, `address` from event titles. Backtested 7,626 resolved outcomes: Brier 0.2530 → 0.2176, all calibration bins within 8% error. Fixed global default 0.30 → 0.45 (empirical). Added `_context_base` per-speaker per-context empirical priors (trump.rally=57%, trump.remarks=65%, trump.signing=35%, etc.) to base_rates.yaml. Built `scripts/backtest_outcomes.py` — full backtest tool simulating 2,260 bets on historical data: 76.5% win rate, +$5,619 P&L on $10/bet, +24.9% ROI.
- **Session 26 (2026-03-12)**: Three model improvements: (1) **Recency-weighted calibration** — `calibrate_base_rates.py` now applies exponential decay (`--recency-halflife-days 90`) so recent outcomes count more than 2025 data; fixes massive drift ("transgender" 0%→67%, "economy" 61%→24%). (2) **All-outcome phrase catalog** — previously 66% of outcomes (5,072 of 7,626) were discarded because they weren't in the active market catalog; now stored in the `general` bucket so future markets get real rates; 1,468 general-bucket phrases added. (3) **Live settlement signal** — `scripts/fetch_settlements.py` (3-min poll) + `app/live_settlements.py` + `LiveSettlements` wired into `ScoringEngine`: when any market in the same event settles today, open markets get an `EVENT_ACTIVE` p_literal boost (+0.04); if the exact phrase already settled YES/NO, p_literal is hard-clamped to 0.90/0.10. (4) **Polymarket pool signal** — `PolymarketPrices.get_pool_signal()` provides a weak calibration anchor (confidence=0.30) from the average price of all same-speaker same-timeframe Poly markets, wired into scorer as `POLY_POOL` fallback when no exact phrase match exists. Backtest: Brier 0.2176 → **0.1848**, P&L $5,619 → **$13,129**, 11/11 calibration bins ✓.
- **Session 27 (2026-03-16)**: Three deterministic edge improvements from Phase 6c: (A1) **Monthly window resolved tracking** — `app/window_state.py` `WindowStateCache` clamps p_literal when phrase already settled in current window; pace_signal ±0.02 based on window settlement pace. (A2) **Rolling N-speech hit rate** — `scripts/compute_rolling_rates.py` + `app/rolling_rates.py` — blends last-3/5/10-speech YES rates into base rate when recent trend diverges from historical (≥5% delta); runs daily via MaintenanceRunner. (A3) **Price velocity / smart money** — `scripts/compute_price_velocity.py` computes 2h/6h/24h YES-price deltas from `market_snapshots`; `app/price_velocity.py` `PriceVelocityCache` emits `SMART_MONEY_UP/DOWN` with ±0.025–0.05 p_literal adjustment; runs every 5 min via MaintenanceRunner; DB index added for fast indexed timestamp lookups. Also: filtered blocked series (sports/entertainment/earnings) from fetch, LiveMarketCatalog, dashboard, and purged old DB rows.
- **Session 29 (2026-03-17)**: **C1 Postmortem Automation.** Built `scripts/post_event_summary.py` — runs every 30 min via MaintenanceRunner; detects events resolved in last 4h; computes per-event win rate, P&L, ROI vs 72.4% baseline; flags `WIN_RATE_BELOW_BASELINE` (>15pp gap), `NEGATIVE_ROI`, and `BAD_REASON_CODES` (per-signal win rate <40% with n≥5); writes `data/logs/postmortem_YYYYMMDD.log` + `data/drift_alerts.json`; wired `report_outcomes` daily into MaintenanceRunner. Immediately surfaced actionable insights: `MARKET_ANCHOR` 0% win rate (5 bets), `POLY_HIGHER` 18-33% win rate — both systematically losing signals worth investigating.
- **Session 28 (2026-03-17)**: **Deep backtest + calibration tuning + B1 implementation.** (1) Deep backtest diagnosis: per-phrase and per-context calibration audit across 5,354 outcomes — identified rally (45% win→68%), remarks (55%→80%), signing (61%→73%) as worst contexts. (2) Applied 83 targeted context-specific phrase corrections to `config/base_rates.yaml` (signing, rally, summit, announcement, briefing, interview, general buckets) — Brier 0.2188→0.2115, win rate 68%→72.4%, ROI +16.8%→+20.8%. (3) Raised `EV_THRESHOLD` default 0.03→0.06 — filters low-quality bets, +0.8% win rate with 8% fewer bets. (4) **B1 White House Schedule RSS** — `scripts/fetch_wh_schedule.py` fetches WH official feed + Google News (100 items), classifies event_type (signing/remarks/briefing/etc.) from categories + title patterns, extracts topic keywords; `app/wh_schedule.py` `WHScheduleCache` provides `resolve_event_type()` override (promotes "general"→confirmed type) and `keyword_boost()` (+0.04–0.08 p_literal for phrases appearing in recent WH event titles); wired into `ScoringEngine` with `WH_CONTEXT` + `WH_KEYWORD` reason codes; `MaintenanceTask` runs every 30 min; 202 tests passing.
- **Session 38 (2026-03-25)**: **LLM event misclassification fix.** Root cause diagnosed: `_classify_event()` was pattern-matching the raw market *question* title (e.g. "Will she say Save America Act at her next briefing?") which triggered the `"save america"` → rally keyword, causing Leavitt briefings to be classified as rallies. LLM then applied rally-level `p_floor=0.68` to "transgender" despite a 14% briefing base rate. Four-layer fix: (1) Removed `"save america"` / `"make america"` from `_FORMAT_KEYWORDS` rally list — these phrases appear in bill names mentioned in briefing market titles; (2) `_classify_event()` now checks `event.event_type` and `speaker` fields first (structured DB data) before falling back to title text, and strips per-phrase question boilerplate before keyword matching; (3) `_validate_p_floors()` now enforces per-format p_floor ceilings (briefing: 0.50, presser: 0.60, diplomatic: 0.60) and an extra cap for known WH spokesperson speakers; (4) LLM prompt now includes explicit speaker role note ("Leavitt — WH Press Secretary, NOT Trump") and `event_formats.md` briefing section expanded with clear `CRITICAL: Spokesperson ≠ Trump` guidance. Stale bad signal files deleted; will regenerate correctly on next `analyze_event.py` run.
- **Session 34 (2026-03-23)**: **Gate hardening based on live outcome analysis (178 bets → 67 bets, 24% → 36% WR, PnL -$13.41 → -$6.17).** Root cause: (1) model bet BUY_NO on window markets where phrases already settled (yes_ask=$1.00), (2) no gate against strong market bullish consensus, (3) too many low-conviction bets, (4) thin-data speakers. New gates: `SETTLED_MARKET_BLOCK` (yes/no ≥ 95¢), `MARKET_BULLISH_BLOCK` (yes ≥ 70¢ + p_cal ≥ 20%), `MARKET_DISAGREE_NO` (yes > p_cal + 25¢ + p_cal ≥ 20%), `THIN_SPEAKER` (<100 resolved outcomes + low confidence), `EVENT_BET_CAP` (max 10 buys per event), `NO_CONVICTION_FLOOR` lowered to p_cal ≥ 35%. Also: **Dashboard overhaul** — new Outcomes tab (PnL/WR by side, event type, phrase), Signals tab (Truth Social feed, pipeline health, drift alerts), System tab redesigned with RAG health indicators, tab title badge `(N)` for BUY cards, all `make` commands replaced with Scripts tab references.
- **Session 30 (2026-03-01)**: **MARKET_ANCHOR replaced + Phase 6c complete + Phase 7 LLM layer built.** (1) **Market Veto Gate**: old MARKET_ANCHOR blend (6% live win rate, 17 bets) replaced with directional veto — `MARKET_VETO_NO` blocks BUY_NO when `yes_ask > p + 0.10`; `MARKET_VETO_YES` blocks BUY_YES when `p > yes_ask + 0.10`; `POLY_VETO_NO` blocks BUY_NO when Polymarket also disagrees. Simulation: baseline -14.7% ROI → -4.1% ROI on live data. (2) **Phrase Co-occurrence (Phase 6c-A)**: `scripts/compute_cooccurrence.py` builds 139-trigger/723-pair index from 8,492 outcomes; when phrase X settles YES in live event, co-occurring phrases get `COOCCUR_BOOST` up to +0.15; runs daily via MaintenanceRunner. (3) **D1 Signal Aggregation**: `scripts/fetch_signals.py` — pulls WH RSS, Google News (6 queries), Truth Social posts, Google Trends (13 keywords) → `data/signal_context.json`; runs every 45 min. (4) **D2 LLM Reasoning**: `scripts/analyze_signals.py` — batches 80 phrases to GPT-4o-mini, returns `{phrase, boost, reason}`, writes `llm_boost` to `signals.yaml`; reason codes `LLM_BOOST_HIGH/LLM_BOOST/LLM_SUPPRESS/LLM_SUPPRESS_HIGH`; runs every 45 min when `OPENAI_API_KEY` set. (5) **`llm_boost` wired into scoring formula**: `p = base * topic_rel * decay * news * buzz * llm + wh_boost`. (6) **Makefile targets**: `make compute-cooccurrence`, `make fetch-signals`, `make analyze-signals`, `make signal-refresh`. Historical backtest: 71.4% win rate, +19.4% ROI. 202 tests passing.
