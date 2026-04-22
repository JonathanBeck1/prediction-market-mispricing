# kalshi-edge

**A 24/7 mispricing detector for [Kalshi](https://kalshi.com) prediction markets.**

> ⚠️ **Honest disclosure**: This system has lost **$15.34** over **1,107 live resolved bets** in production. It is a research prototype, not a proven strategy. See [Performance Data](#performance-data) below.
>
> This is NOT financial advice. See [DISCLAIMER.md](DISCLAIMER.md).

---

## What This Is

A fully automated, 24/7 local engine that:
- Polls Kalshi market prices every 30 seconds
- Scores each market using a statistical mispricing model
- Generates advisory **Action Cards** (BUY_YES / BUY_NO / WATCH) for manual review
- Tracks outcomes and computes realized P&L

**It never places orders.** All outputs are advisory only. You decide what to trade.

---

## Two Models — One Toggle

The core research question this project explores: *does adding LLM signals to a statistical base rate model improve prediction market returns?*

| Model | How it works | Cost | Toggle |
|-------|-------------|------|--------|
| **BayesianScorer** (default) | Hierarchical Beta-Binomial posteriors per (speaker, phrase). Bets only when the 90% credible interval excludes market price. Kelly sizing. No LLM. | Free | `USE_BAYESIAN_SCORER=1` |
| **ScoringEngine** (legacy) | Base rates × LLM signal boosts × news pressure × Polymarket cross-check × 17 calibrated gates. | ~$50/month OpenAI | `USE_BAYESIAN_SCORER=0` |

**What the live data showed:** The LLM signal layer was net-negative across most markets. The LLM's Brier score (0.352) was worse than using market price alone (0.238). Every signal layer added cost without improving win rate.

---

## Performance Data

### Overall: 1,107 bets — $-15.34

The loss comes almost entirely from specific losing segments. The profitable segments genuinely work:

| Segment | Bets | Win Rate | P&L | Notes |
|---------|------|----------|-----|-------|
| **NBA BUY_NO** | 283 | 51.9% | **+$12.62** | Structural arena/phrase edge |
| **NCAAB BUY_NO** | 235 | 57.0% | **+$6.20** | Similar structural edge |
| **MMA BUY_NO** | 60 | 61.7% | **+$2.03** | Rare phrase NO bets |
| **NCAAB BUY_YES** | 24 | 41.7% | **+$3.63** | |
| MLB BUY_NO | 128 | 42.2% | **-$17.61** | Model overestimates common MLB phrases |
| Trump BUY_NO | 42 | 33.3% | **-$8.50** | Trump says things we don't expect |
| NBA BUY_YES | 93 | 33.3% | **-$8.68** | Bayesian model overfires on BUY_YES |

**Counterfactual:** Restricting to only the profitable (speaker, side) segments retroactively yields **+$26-41** on 464-680 bets. This is the key open research question — can we learn to restrict reliably, or is it overfitting?

### Model Comparison (Bayesian vs Legacy)

The BayesianScorer has only been live since April 14, 2026 (132 resolved bets at the time of writing):

| | Legacy ScoringEngine | BayesianScorer |
|--|--|--|
| Bets | 975 | 132 |
| Win rate | 49.2% | 32.6% |
| P&L | -$1.97 | -$13.37 |
| BUY_YES % | 20% | 76% |

The Bayesian model is over-indexed on BUY_YES — its NBA speaker prior (0.591) biases it toward buying YES on NBA phrases the market has already priced correctly. Active area of work.

---

## Architecture

```
Runner (24/7 async process)
├── KalshiWatcher        — polls GET /markets every 30s, writes market_snapshots
├── TranscriptIngestor   — polls transcript URLs, runs PhraseMatcher, writes phrase_hits
├── BayesianScorer       — reads snapshots, scores markets, emits Action Cards
│     └── (or ScoringEngine for LLM-enhanced scoring)
├── MaintenanceRunner    — fetches markets, outcomes, Polymarket, Kalshi settlements
└── Watchdog             — monitors staleness, exits for launchd auto-restart

Dashboard (port 8777)
└── Web UI with live action cards, performance charts, scoring intelligence view
```

**Self-healing:** SQLite WAL-mode with automatic corruption recovery. launchd keeps the process alive 24/7. Watchdog restarts if snapshots go stale.

### Scoring Pipeline (BayesianScorer)

```
For each live Kalshi market:
  1. Look up (speaker, phrase) → Beta posterior {α, β, mean, ci_low, ci_high}
  2. Compute 90% credible interval
  3. If yes_ask < ci_low  → BUY_YES (market underprices YES)
     If 1-no_ask > ci_high → BUY_NO  (market overprices YES)
     Else                  → WATCH   (market is within our uncertainty)
  4. Kelly criterion sizing based on posterior mean and edge
  5. Structural gates: settled market, cheap/expensive NO, depth/spread
```

### Scoring Pipeline (ScoringEngine — Legacy)

```
p_literal = base_rate × time_decay × news_pressure × x_buzz × llm_boost × event_llm
p_calibrated = Platt scaling of p_literal
ev_yes = p_calibrated - yes_ask
ev_no  = (1 - p_calibrated) - no_ask
→ 17-gate stack (SETTLED_MARKET_BLOCK, TRUMP_NO_BLOCK, KELLY_WEAK, ...)
```

---

## Quick Start

### Requirements
- macOS or Linux
- Python 3.9+
- A free [Kalshi account](https://kalshi.com) (no API key needed for basic operation)

### Setup

```bash
git clone https://github.com/yourusername/kalshi-edge.git
cd kalshi-edge
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Run in mock mode (safe, no real API calls)

```bash
KALSHI_MOCK=1 python3 -m app.runner
# In a second terminal:
python3 -m app.dashboard
# Open http://localhost:8777
```

### Run with live Kalshi prices

```bash
cp config/runtime.env.example config/runtime.env
# Edit config/runtime.env — at minimum set KALSHI_MOCK=0
make run-live
```

See [docs/QUICKSTART.md](docs/QUICKSTART.md) for a full walkthrough.

---

## Calibration Data

The scoring models are calibrated from **10,500+ historical Kalshi market outcomes**. Outcomes are fetched from the Kalshi public API via `make fetch-outcomes`.

Phrase base rates come from speaker transcripts in `data/corpus/`. The corpus is not included in this repo (copyright concerns). See [data/corpus/README.md](data/corpus/README.md) for how to build your own.

| Speaker | Outcomes | Corpus transcripts |
|---------|----------|--------------------|
| trump | 5,652+ | 179 (not included) |
| leavitt | 204 | 85 (not included) |
| mamdani | 924 | 27 (not included) |
| nba | 283+ | N/A (arena overrides) |
| ncaab | 359+ | N/A (phrase floors) |
| mlb | 131+ | N/A (phrase floors) |
| mma | 72+ | N/A (phrase floors) |

---

## Key Commands

```bash
# Testing
python3 -m pytest -q                    # 246 tests — all must pass

# Data refresh
make fetch-markets                      # Kalshi market definitions
make fetch-outcomes                     # Settled market outcomes
make fetch-poly                         # Polymarket cross-prices

# Calibration pipeline
make calibrate                          # Full calibration refresh

# Outcome tracking
make record-outcomes                    # Match settled outcomes to BUY cards
make report-outcomes                    # P&L report
make backtest                           # Historical scorecards

# 24/7 operation (macOS launchd)
make install-24x7                       # Install as a background service
make local-status                       # Show running processes + log tails
```

---

## Project Structure

```
app/                    Core modules (runner, scoring, watcher, dashboard, ...)
├── bayesian_scorer.py  New: hierarchical Beta-Binomial mispricing detector
├── scoring.py          Legacy: 2,500-line LLM-enhanced ScoringEngine
├── runner.py           Async service orchestrator
├── dashboard.py        Web UI (port 8777)
└── ...

scripts/                On-demand and maintenance scripts (67 files)
config/
├── base_rates.yaml     Manually-curated phrase base rates
├── base_rates_auto.yaml Auto-calibrated rates from outcomes
├── runtime.env.example Template for secrets / env vars
└── llm/                LLM instruction files (editable without code changes)
brain/                  Project specs, decisions, architecture docs
tests/                  246 pytest tests
data/                   Runtime state (gitignored)
docs/                   Additional documentation
└── archive/            Development history and build plans
```

---

## Known Issues / Open Questions

1. **BayesianScorer overfires on NBA BUY_YES** — NBA speaker prior (0.591) inflated by common phrases. Needs minimum CI exclusion margin (e.g. `yes_ask < ci_low - 0.05`).
2. **Legacy scorer's Brier score (0.352) > market mid (0.238)** — The model is worse than the market at predicting outcomes. This is the core motivation for the Bayesian rebuild.
3. **MLB BUY_NO is consistently losing** — Phrases like "bunt" (19% WR, -$5.94) are said far more often than historical rates suggest. Needs phrase-level blacklist or better calibration.
4. **Corpus is not included** — Restricts reproducibility. We provide a public-domain transcript sourcing guide instead.
5. **Only tested on macOS** — launchd integration is macOS-specific. Docker/Linux setup is planned.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

Community questions and critique are welcome. Given the honest P&L above, the most useful contributions would be:
- Analysis of why specific segments lose (especially MLB BUY_NO)
- Better phrase base rates from alternative public sources
- Backtest results on your own Kalshi account data
- Linux/Docker compatibility fixes

---

## License

MIT — see [LICENSE](LICENSE).

---

## Disclaimer

See [DISCLAIMER.md](DISCLAIMER.md). This software has lost money in live trading. It is a research tool, not a winning strategy.
