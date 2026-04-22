# Build Plan START — Detailed Execution Roadmap

> **Purpose:** A single, ordered plan to *start* improving the Kalshi Mention Edge engine **from the current codebase** (post–Session 65).  
> **Invariant:** Manual-only — no order placement, no trading automation. All changes produce advisory Action Cards only.  
> **Companion docs:** `brain/00_PROJECT_BRIEF.md`, `brain/02_DATA_SOURCES.md`, `brain/STATUS.md`, `brain/BUILD_PLAN_V3.md` (audit IDs), `brain/BUILD_PLAN_V4.md` (calibration/signal themes).  
> **Note:** Many V4 items (speaker defaults, rolling staleness, pre-event/live split, LLM boost caps, hazard model, regime gates) are **already implemented** — this plan **does not** repeat them; it **verifies**, **measures**, then **extends**.

---

## How to use this document

1. Complete **Phase 0** before writing scorer logic.  
2. Execute **sprints in order** unless a task is blocked on credentials (mark SKIPPED, document in `brain/08_DECISIONS_LOG.md`).  
3. After each sprint: `python3 -m pytest -q`, `python3 scripts/health_check.py`, update `brain/STATUS.md`.  
4. Any change to gates or calibration: run `python3 scripts/backtest_outcomes.py --walk-forward` (or project-standard flags) and capture BSS / P&amp;L delta in the decisions log.

---

## Phase 0 — Baseline, safety, and inventory (Days 1–2)

### 0.1 Environment and invariants

| Step | Action | Done when |
|------|--------|-----------|
| 0.1.1 | Confirm `config/runtime.env` loads; `MAINTENANCE_ENABLED`, `OPENAI_API_KEY`, `X_BEARER_TOKEN` documented as optional | Checklist in runbook or README |
| 0.1.2 | Confirm single runner discipline (no concurrent `python3 -m app.runner`) | `brain/07_RUNBOOK.md` + operator note |
| 0.1.3 | `python3 -m pytest -q` | All tests green; record count in STATUS |

### 0.2 Data health snapshot

| Step | Action | Done when |
|------|--------|-----------|
| 0.2.1 | `sqlite3 data/edge.db "PRAGMA quick_check;"` | Returns `ok` |
| 0.2.2 | `python3 scripts/health_check.py` | Output saved to `data/logs/health_YYYYMMDD.txt` |
| 0.2.3 | Record: `MAX(ts)` from `market_snapshots`, `action_cards`; ages vs thresholds | Table in STATUS or this file appendix |

### 0.3 Measurement baselines (do not “improve” blind)

| Step | Action | Command / artifact | Success metric |
|------|--------|-------------------|----------------|
| 0.3.1 | Live vs market | `python3 scripts/backtest_outcomes.py --live --save` (use project help if flags differ) | `data/bss_metrics.json` updated; BSS direction recorded |
| 0.3.2 | Walk-forward | Same script with walk-forward mode per `BUILD_PLAN_V3` | BSS &gt; 0 vs market mid on held-out slice |
| 0.3.3 | Outcome reviews coverage | SQL: `SELECT COUNT(*), MIN(ts), MAX(ts) FROM outcome_reviews` | N, date span documented |
| 0.3.4 | Per-speaker calibration gap | SQL or small script: model p vs actual YES by `speaker` (30d) | Table: flag any &gt;15pp systematic gap |

**Deliverable:** One-page “Baseline card” appended to `brain/STATUS.md` (or linked note): test count, health red/green, BSS numbers, outcome_review N.

---

## Sprint A — Data spine: freshness, volume, and consistency (Days 3–10)

**Objective:** The model cannot beat markets if **inputs are stale, duplicated, or in the wrong schema.**

### A.1 Truth Social (manual-first; STATUS: no auto-fetch)

| ID | Task | Detail | Files / commands |
|----|------|--------|------------------|
| A.1.1 | **SOP for operators** | Define: minimum cadence (e.g. before each Trump event window), format, `posted_at` in Eastern or UTC | Doc in `brain/07_RUNBOOK.md` or `README.md` § Truth Social |
| A.1.2 | **Dedup policy** | Same post with different quote characters → one canonical entry; document normalization rule | `data/truth_social_posts.json` hygiene |
| A.1.3 | **Process after ingest** | After bulk adds: `python3 scripts/process_truth_social.py` | Verify `signals.yaml` mtime |
| A.1.4 | **Optional: retention audit** | `add_truth_social_post.py` prunes &gt;7d; confirm that window still matches product needs | If weekly markets need longer, decide in `08_DECISIONS_LOG.md` and adjust `MAX_AGE_HOURS` in script |

**Acceptance:** `truth_social_posts.json` never older than **operator-defined SLA** before high-impact Trump events; no duplicate near-identical posts.

### A.2 Kalshi outcomes and market cache

| ID | Task | Detail |
|----|------|--------|
| A.2.1 | Scheduled `fetch_outcomes.py` | Ensure maintenance or cron runs at least daily; verify `total_outcomes` monotonicity |
| A.2.2 | Series coverage audit | Compare `MENTION_SERIES` in `scripts/fetch_outcomes.py` vs `scripts/fetch_markets.py` / watcher — no orphan series |
| A.2.3 | `kalshi_markets.json` age | Must stay within `health_check` market cache threshold when engine is “on” |

### A.3 Corpus and ingestion

| ID | Task | Detail |
|----|------|--------|
| A.3.1 | **Trump / Leavitt recency** | Target: rolling `last_seen` for top phrases not &gt;21d behind (aligns with `RollingRate` staleness) |
| A.3.2 | **Thin speakers** | Carney, Starmer, Homan, Hegseth, Powell: minimum transcript count per `data-manager.md` (e.g. ≥5 for calibration paths) |
| A.3.3 | **Single co-occurrence writer** | `calibrate_base_rates.py` writes a **dict-shaped** `phrase_cooccurrence.json`; `compute_cooccurrence.py` writes **`pairs`**. Maintenance must run **`compute_cooccurrence` after `calibrate_base_rates`** OR remove cooc from calibrate to avoid schema flip-flop. **Pick one strategy** and document in `02_DATA_SOURCES.md`. |

**Acceptance:** Documented pipeline order; one co-occurrence schema in production JSON at steady state.

### A.4 Raw archives and DB growth

| ID | Task | Detail |
|----|------|--------|
| A.4.1 | `prune_snapshots.py` | Confirm interval in `app/maintenance.py`; DB size trend flat or down |
| A.4.2 | Purge stale `data/edge.db.*` backups | STATUS already flagged 10+ corrupt backups — archive off-machine or delete with log |

---

## Sprint B — Calibration and probability hygiene (Days 8–18)

**Objective:** Unknown-phrase and small-N behavior must match **empirical** speaker/context reality.

### B.1 Verify existing Session 65 calibration stack

| ID | Task | Where | Action |
|----|------|-------|--------|
| B.1.1 | Speaker defaults | `app/base_rates.py` `_SPEAKER_DEFAULTS` | Recompute quarterly from `outcome_reviews` (script or notebook); adjust if gap &gt;10pp persists |
| B.1.2 | Platt / calibration | `app/calibration.py` | Document current `_MIN_SAMPLES`, fallback when N small; align with BUILD_PLAN_V4 P1.2 if not already |
| B.1.3 | Rolling + hazard | `app/rolling_rates.py`, `app/phrase_hazard.py` | Confirm maintenance runs `compute_rolling_rates.py` + `compute_hazard_rates.py` (if present) after corpus/outcomes refresh |

### B.2 Auto layer

| ID | Task | Detail |
|----|------|--------|
| B.2.1 | `config/base_rates_auto.yaml` | Ensure `compute_base_rates.py` runs on schedule; diff occasionally for absurd jumps |
| B.2.2 | Bias map | `compute_bias_map.py` — track `overpriced_count` / `underpriced_count` drift week-over-week |

### B.3 Stratification backlog (from V3/V4)

| ID | Task | Detail |
|----|------|--------|
| B.3.1 | **Event-type calibration** | If not fully live: add strata in `calibration.py` (e.g. briefing vs rally vs sports) with fallback chain |
| B.3.2 | **Sports depth** | Per STATUS: pursue more resolved outcomes per phrase; expand `base_rates_auto` + arena scripts |

**Acceptance:** 30d table: per-speaker average `p_literal` vs outcome YES rate within agreed tolerance (e.g. 10pp) except documented thin buckets.

---

## Sprint C — Signal provenance and double evidence (Days 15–28)

**Objective:** Same news story must not multiply through `news`, `llm`, `event_llm`, and WH keyword without **auditability**.

### C.1 Schema and ingest

| ID | Task | Detail | Primary files |
|----|------|--------|---------------|
| C.1.1 | **Source hash** | Extend `signals.yaml` / internal store with `source_story_hash` or equivalent | `scripts/analyze_signals.py`, `scripts/fetch_signals.py`, `app/signals.py` |
| C.1.2 | **Scorer dampening** | Verify `DOUBLE_COUNT_DAMPENED` (and allies) cover WH + news + LLM; extend if new combo paths added | `app/scoring.py` |
| C.1.3 | **V3 spot-checks** | H5 WH additive boost vs multiplicative stack — confirm still safe after edits | `scoring.py` |

### C.2 LLM policy

| ID | Task | Detail |
|----|------|--------|
| C.2.1 | Domain skip | Confirm sports/earnings skips in `analyze_event.py` for cost and misfire reduction |
| C.2.2 | Suppress vs boost | Treat suppress as higher trust than boost (per historical WR notes in STATUS) |

**Acceptance:** For a synthetic duplicate headline, logs or reason codes show dampening; optional unit test with mocked signals.

---

## Sprint D — Phrase truth and resolution alignment (Days 20–35)

**Objective:** Literal resolution on Kalshi must match **normalized text and negation**.

| ID | Task | Detail | Files |
|----|------|--------|-------|
| D.1 | Unicode / normalization | NFC, smart quotes, dash normalization in matcher | `app/phrase_matcher.py` |
| D.2 | Negation / attribution | “Will NOT say X” vs quoted attribution | `phrase_matcher.py`, tests |
| D.3 | Cross-market resolution | Poly scope mismatch already flagged in V3 M4 — extend tests when changing Poly blend | `app/scoring.py`, `app/polymarket.py` |

**Acceptance:** Dedicated tests from V3 scenarios; no regression on `pytest`.

---

## Sprint E — Operations, dashboard, and feedback loop (continuous)

| ID | Task | Detail |
|----|------|--------|
| E.1 | Watchdog / health | Secondary check if scorer idle but heartbeat alive (STATUS Session 59 note) |
| E.2 | `outcome_reviews` cadence | `record_outcomes.py` + `bet_journal` / archive path — maximize labeled decisions for gates |
| E.3 | Dashboard BSS / regime | Surface `bss_metrics.json`, `regime_alerts.json`, `drift_alerts.json` in one “model health” strip |
| E.4 | `detect_regimes.py` | Weekly or daily run; operator reads output; new patterns → gated in scorer after review |

---

## Priority matrix (start order)

| Order | Sprint | Rationale |
|-------|--------|-----------|
| 1 | **Phase 0** | No edits without baseline BSS and DB health |
| 2 | **A.1–A.2** | Cheapest edge: fresh TS + outcomes + markets |
| 3 | **A.3** | Corpus + co-occurrence consistency |
| 4 | **B.1–B.2** | Align probabilities with measured reality |
| 5 | **C** | Stop paying for the same headline 4× |
| 6 | **D** | Reduce “said/didn’t say” disputes |
| 7 | **E** | Sustainability and continuous learning |

---

## Session breakdown (example: 2-week start)

| Week | Focus | Exit criteria |
|------|-------|----------------|
| **Week 1** | Phase 0 + Sprint A | Health mostly green with engine on; TS SOP; outcomes/markets fresh; co-occurrence strategy decided |
| **Week 2** | Sprint B + start C | Speaker calibration table updated; first `source_story_hash` prototype or documented blocker |

---

## Definition of Done (global)

- [ ] Tests pass.  
- [ ] `health_check.py` documented interpretation (known false positives allowed if justified).  
- [ ] `brain/STATUS.md` updated.  
- [ ] Non-obvious behavior changes logged in `brain/08_DECISIONS_LOG.md`.  
- [ ] BSS / walk-forward or agreed metric recorded before vs after for scorer-affecting work.

---

## Appendix A — Quick command card

```bash
python3 -m pytest -q
python3 scripts/health_check.py
sqlite3 data/edge.db "PRAGMA quick_check;"
python3 scripts/fetch_outcomes.py
python3 scripts/process_truth_social.py
python3 scripts/backtest_outcomes.py --help   # pick --live / --walk-forward / --save per current CLI
```

---

## Appendix B — Explicit “already landed” (do not redo without audit)

The following themes appear **implemented** in recent STATUS / code review — **verify** rather than re-implement:

- Speaker-specific default rates (`app/base_rates.py`).  
- Rolling rate staleness (`app/rolling_rates.py`).  
- Hazard-based live decay (`app/phrase_hazard.py` + scripts per STATUS).  
- Pre-event vs live EV / ask floors (Session 65).  
- LLM boost caps / low-confidence neutering (`app/signals.py`).  
- Regime-related gates and `scripts/detect_regimes.py`.  
- Many P0 measurement items from V3 (BSS, walk-forward) if STATUS reports walk-forward BSS positive.

---

*End of BUILD_PLAN_START.md — revise monthly or after major sessions.*
