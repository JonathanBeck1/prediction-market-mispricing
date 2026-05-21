# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A 24/7 local Python advisory engine for Kalshi political speaker mention markets.
It detects mispriced contracts and generates Action Cards (BUY_YES / BUY_NO / WATCH)
for manual review and trading. It never places orders — it only outputs recommendations.

## Non-Negotiable Rules

- NO order placement. NO trading automation. All outputs are advisory only.
- Never modify `app/scoring.py` or `app/runner.py` without reading the full gate stack first.
  Scoring gates are calibrated from live outcome data. Wrong changes cost money.
- Always run `python3 -m pytest -q` before finishing any change. The full test suite must pass.
- Update `brain/STATUS.md` at the end of any session where you changed code or data.
- Append key decisions to `brain/08_DECISIONS_LOG.md` — never delete existing entries.

## Read First Every Session

- `brain/STATUS.md` — current state, what has been done, what is next
- `brain/09_BACKLOG.md` — milestone plan and pending work
- `brain/08_DECISIONS_LOG.md` — every scoring gate decision and why it was made

## Commands

### Setup
```bash
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

### Running
```bash
make run                 # Mock mode (KALSHI_MOCK=1, safe for testing)
make run-live            # Live Kalshi API
make run-live-24x7       # Live + background maintenance loop
make dashboard           # Web UI at http://localhost:8777 (separate terminal)
make event-ready         # Pre-event: refresh all caches + validate health gates
make health-check        # Verify strict runtime gates
```

### Testing
```bash
python3 -m pytest -q                          # full suite
python3 -m pytest -q tests/test_scoring.py    # scoring-specific
python3 -m pytest -k "test_name" -q           # single test by name
```

### Data refresh
```bash
make fetch-markets    # Kalshi market definitions → data/kalshi_markets.json
make fetch-poly       # Polymarket prices → data/polymarket_prices.json
make fetch-wallet     # Wallet-flow alpha signals
make fetch-outcomes   # Settled market outcomes
make fetch-x          # X posts for buzz signals (requires X_BEARER_TOKEN)
```

### Calibration pipeline (run after adding corpus or on-demand)
```bash
python3 scripts/fetch_outcomes.py
python3 scripts/calibrate_base_rates.py
python3 scripts/compute_rolling_rates.py
python3 scripts/compute_cooccurrence.py
python3 scripts/compute_bias_map.py
python3 scripts/compute_phrase_trends.py
python3 scripts/report_outcomes.py
```

### Outcome tracking
```bash
make record-outcomes    # Match settled outcomes to BUY cards
make report-outcomes    # P&L report
make backtest           # Historical scorecards
make tune-policy        # Writes config/safe_mode.env with tuned thresholds
```

### Diagnostics
```bash
make doctor          # Verify repo root, venv, package structure
make local-status    # Show running processes and log tails
python3 scripts/health_check.py
```

## Project Layout

```
app/
  runner.py               Main entry point — orchestrates all async loops
  scoring.py              Scoring engine — ALL gate logic lives here
  dashboard.py            Local web UI on port 8777
  maintenance.py          Background maintenance task scheduler
  db.py                   SQLite connection + schema (WAL mode, FULL sync)
  kalshi_watcher_live.py  Live price polling from Kalshi API
  event_detector.py       speech_state transitions: scheduled/live/ended
  base_rates.py           Phrase base rate lookup (YAML-backed)
  rolling_rates.py        Last-3/5/10-speech hit rate blending
  polymarket.py           Polymarket cross-market price signal
  wallet_flow.py          Smart money flow signal
  bias_map.py             Per-phrase systematic mispricing detector
  event_signals.py        Per-event LLM-generated p_overrides/p_floors
  live_settlements.py     Real-time phrase settlement + co-occurrence
  window_state.py         Monthly/weekly window settlement state
  signals.py              LLM boost/suppress multipliers from signals.yaml
  market_family.py        single_event vs windowed market routing

config/
  base_rates.yaml         Manually-curated phrase base rates by speaker
  base_rates_auto.yaml    Auto-calibrated rates from resolved outcomes (generated)
  base_rates_priors.yaml  Priors for new speakers (never overwritten by calibration)
  events.yaml             Scheduled events (keep populated 24-48h ahead)
  runtime.env             Live env vars (KALSHI_MOCK, API keys, etc.)
  llm/                    LLM instruction files (editable without code changes)

data/
  edge.db                 SQLite database — WAL mode, PRAGMA synchronous=FULL
  corpus/<speaker>/       Raw .txt transcripts — source of all phrase hit rates
  event_signals/          Per-event JSON with p_overrides/p_floors (auto-generated)
  signals.yaml            LLM phrase boost/suppress (auto-refreshed ~45min)
  bias_map.json           Systematic mispricing map (auto-refreshed daily)
  rolling_hit_rates.json  Last-3/5/10-speech phrase rates (auto-refreshed)
  polymarket_prices.json  Cross-market prices (auto-refreshed 15min)

scripts/                  On-demand and maintenance scripts
tests/                    pytest suite — must all pass before any commit
brain/                    All specs, decisions, status. Source of truth.
```

## Service Loop Architecture (`app/runner.py`)

The `Runner` starts async tasks via `_service_loop()`, each running a blocking function in a thread executor:
- **`KalshiWatcher`** — polls Kalshi `GET /markets` API (live) or returns deterministic fake prices (mock)
- **`TranscriptIngestor`** — polls transcript URLs, runs `PhraseMatcher`, writes `phrase_hits`
- **`ScoringEngine`** — reads snapshots + phrase hits, computes probability model, emits Action Cards
- **`MaintenanceRunner`** — 24/7 mode only; auto-runs fetch_markets, fetch_poly, fetch_wallet, fetch_outcomes on configurable intervals
- **`Watchdog`** — monitors snapshot age and scorer output freshness; exits process (triggering launchd restart) if stale

## Scoring Engine Architecture (`app/scoring.py`)

Scoring formula:
```
p_literal    = base_rate × time_decay × news_pressure × x_buzz × llm_boost × event_llm
p_calibrated = Platt calibration of p_literal
ev_yes       = p_calibrated - yes_ask
ev_no        = (1 - p_calibrated) - no_ask
```

Gate stack (applied in order — each can flip side to WATCH):
```
 1. SETTLED_MARKET_BLOCK        market resolved, skip
 2. PRE_EVENT_BUY_NO_BLOCK      no NO bets before event starts
 3. EVENT_ENDING_SOON           decay signal near end of event
 4. CROSS_MARKET_ARB_BLOCK      Poly prices YES 10c+ above Kalshi
 5. POLY_HIGHER_VETO            Poly prices YES > model+15c AND poly_confidence >= 0.25
 6. ROLLING_POLY_CONFLICT       ROLLING_N5 used AND Poly disagrees (HIGHER or DIVERGE)
 7. LOW_CONF_NO_BLOCK           BUY_NO requires SCORE_CONF_HIGH
 8. OFF_TOPIC_NO_BLOCK          phrase contextually irrelevant to event
 9. WIDE_SPREAD_NO_BLOCK        yes_spread > 0.05
10. NBA_THIN_BOOK_BLOCK         NBA + depth_no < 50
11. NBA_LOW_PRICE_BLOCK         NBA BUY_NO when yes_ask < 0.38
12. LEAVITT_LOW_YES_BLOCK       Leavitt BUY_YES when yes_ask < 0.45
13. NO_PRICE_RANGE_BLOCK        Trump BUY_NO when yes_ask > 0.35 (others > 0.75)
14. THIN_SPEAKER                speaker with < 100 known outcomes + score_confidence < 0.65
15. KELLY_WEAK                  Kelly fraction below minimum threshold
16. NO_CONVICTION_FLOOR         BUY_NO when p_calibrated >= 0.40 (near-50/50)
17. WEEKLY_WINDOW_NO_BLOCK      KXTRUMPSAY when p_calibrated >= 0.08
```

**NEVER add a gate without evidence from outcome_reviews data analysis.**
Every gate in `brain/08_DECISIONS_LOG.md` has the exact bet count, win rate, and P&L.

## Database — Critical Rules

- `data/edge.db` uses WAL mode + `PRAGMA synchronous=FULL`. Never change this.
- `data/runner.lock` is flock-based — **never delete it while a runner is alive**; deletion breaks the single-instance mechanism.
- When a process is SIGKILLed, stale `edge.db-shm` / `edge.db-wal` files cause corruption.
  Always run `rm -f data/edge.db-shm` after killing the runner.
- If you see "database disk image is malformed":
  ```bash
  # 1. Stop the runner
  rm -f data/edge.db-shm data/edge.db-wal
  sqlite3 data/edge.db "PRAGMA quick_check;"
  # 2. If still bad:
  sqlite3 data/edge.db ".recover" | sqlite3 data/edge_recovered.db
  mv data/edge.db data/edge.db.corrupt_$(date +%Y%m%d_%H%M%S)
  mv data/edge_recovered.db data/edge.db
  sqlite3 data/edge.db "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;"
  ```
- The runner has auto-reconnect: catches `sqlite3.DatabaseError`, `sqlite3.OperationalError`, and `sqlite3.ProgrammingError` and does an atomic connection swap (opens new connection before closing old one).

## Calibration Data

| Speaker  | Corpus transcripts | Resolved outcomes | Status               |
|----------|--------------------|-------------------|----------------------|
| trump    | ~750               | 5,652+            | Fully calibrated     |
| leavitt  | 85                 | 204               | Good coverage        |
| mamdani  | 27                 | 924               | Good coverage        |
| carney   | 3                  | ~100              | THIN — needs more    |
| starmer  | 0                  | ~180              | NO CORPUS            |
| homan    | 2                  | ~100              | THIN                 |
| nba      | 0 (not used)       | 104               | Arena overrides only |
| mlb      | 0 (not used)       | 13                | Arena overrides only |
| ncaab    | 0 (not used)       | 100               | Phrase floors only   |

Sports scoring: NBA/MLB/NCAAB skip LLM signals. Unknown phrases fall back to `_global_default = 0.48`. Profitable sports bets are structural venue/arena name overrides (`BIAS_MAP_RATE`), not probabilistic predictions.

## Adding Transcripts

```bash
# 1. Save to data/corpus/<speaker>/<event_type>_YYYY-MM-DD_01.txt
#    Event types: briefing, remarks, rally, presser, signing, interview
python3 scripts/auto_ingest_corpus.py
python3 scripts/compute_base_rates.py
python3 scripts/compute_rolling_rates.py
```

## Key Pending Work

- Add 2-3 Carney/Starmer transcripts to unlock auto-calibration
- Bias map BUY_NO activation (30 overpriced phrases not yet aggressively pushed to BUY_NO)
- Hazard model for live events (replace static time-decay with per-phrase empirical rates)

## Configuration

All settings read from env vars or `config/runtime.env` (auto-loaded by `app/config.py`). Key vars:
- `KALSHI_MOCK` (default `1`) — mock watcher; set `0` for live
- `TRANSCRIPT_SOURCE` — `directhttp` | `openclaw` | `file`
- `TRANSCRIPT_URLS` — comma-separated URLs to poll
- `FOCUS_EVENT_MARKETS` — `1` in live mode
- `MAINTENANCE_ENABLED` — `1` for 24/7 background refresh

Full list: `README.md` "Key env vars" section. For launchd: `cp config/runtime.env.example config/runtime.env`.

## Coding Conventions

- Python 3.10+, type hints, dataclasses
- `from __future__ import annotations` in every module
- No unnecessary dependencies — stdlib + httpx only
- Comments explain WHY, not WHAT
- All gate changes must cite outcome_reviews P&L data in the comment and in `brain/08_DECISIONS_LOG.md`
