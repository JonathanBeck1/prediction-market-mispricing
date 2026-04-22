# kalshi-edge

I spent a few months building a system that tries to find mispriced contracts on [Kalshi](https://kalshi.com) prediction markets — specifically the "speaker mention" markets where you're betting on whether a politician or sports broadcaster will say a specific phrase.

It runs 24/7, polls prices every 30 seconds, and tells me when it thinks a market is mispriced. It never places any orders. I review the alerts and decide whether to trade.

**It's currently losing money.** As of writing, -$15.34 across 1,107 resolved bets. I'm open-sourcing it because I think the codebase and the data are more interesting than the P&L, and I'd genuinely like to know what I'm missing.

> This is not financial advice. See [DISCLAIMER.md](DISCLAIMER.md).

---

## The core idea

Kalshi runs markets like "Will Trump say the word 'tariff' in his next press conference?" that settle YES or NO. The idea was: if I can measure how often a speaker actually uses a phrase across hundreds of speeches, and the market price doesn't match that frequency, I can bet on the difference.

Sounds simple. Harder in practice.

---

## Two scoring models

I ended up building two completely different approaches — partly to test whether LLM signals actually help, partly because the first model stopped working and I needed to understand why.

**BayesianScorer** (default, `USE_BAYESIAN_SCORER=1`)

Uses historical outcome data to build a Beta-Binomial posterior for each (speaker, phrase) pair. If the market price falls outside the 90% credible interval, it flags a bet. No LLM involved, costs nothing to run.

**ScoringEngine** (legacy, `USE_BAYESIAN_SCORER=0`)

Starts from the same base rates but layers on: LLM signal boosts from OpenAI, news pressure, Polymarket cross-market prices, and a 17-gate decision stack built from ~3 months of live outcome data. Costs ~$50/month in OpenAI calls.

**What the data actually showed about the LLM layer:**

The LLM-enhanced model's [Brier score](https://en.wikipedia.org/wiki/Brier_score) was 0.352. Just using the raw market price as your prediction gives a Brier score of 0.238. Lower is better — so the model was significantly *worse* than the market at predicting outcomes, even after months of tuning. Every signal layer I added made things worse, not better.

---

## What's actually working vs what isn't

Here's the real breakdown by segment (all resolved bets, live production):

| Segment | Bets | Win Rate | P&L |
|---------|------|----------|-----|
| NBA BUY_NO | 283 | 51.9% | **+$12.62** |
| NCAAB BUY_NO | 235 | 57.0% | **+$6.20** |
| NCAAB BUY_YES | 24 | 41.7% | **+$3.63** |
| MMA BUY_NO | 60 | 61.7% | **+$2.03** |
| MLB BUY_NO | 128 | 42.2% | **-$17.61** |
| NBA BUY_YES | 93 | 33.3% | **-$8.68** |
| Trump BUY_NO | 42 | 33.3% | **-$8.50** |
| Trump BUY_YES | 125 | 33.6% | **-$3.28** |

The sports BUY_NO markets (NBA, NCAAB, MMA) are profitable. The Bayesian model keeps tripping over NBA BUY_YES — it thinks common phrases like "elbow" or "airball" are underpriced when the market has already priced them correctly. MLB BUY_NO is the single biggest money drain and I haven't figured out why "bunt" has a 19% win rate on 17 bets when historical data says it should be much rarer.

If I'd only traded the segments that are working, the total P&L retroactively would be around +$26-41. That's the question I'm still trying to answer: is there a way to identify the profitable segments reliably, or is it just overfitting to historical patterns?

---

## How it works

```
Runner (24/7 async)
├── KalshiWatcher      polls Kalshi prices every 30 seconds
├── TranscriptIngestor polls live transcript URLs, detects phrase hits
├── BayesianScorer     computes posteriors, compares to market price, emits cards
├── MaintenanceRunner  refreshes market data, outcomes, Polymarket prices
└── Watchdog           exits the process if data goes stale (launchd auto-restarts)

Dashboard at http://localhost:8777
```

The DB is SQLite in WAL mode. I had a lot of corruption issues early on — the fix was giving each service loop its own dedicated DB connection instead of sharing one across threads. There's auto-healing logic on startup that recovers from stale WAL files.

### How the Bayesian scoring works

For each market, I look up the (speaker, phrase) pair in a pre-computed table of Beta posteriors trained from 10,500+ historical Kalshi outcomes. The posterior gives me a mean probability and a 90% credible interval.

- If `yes_ask < ci_low` → market is underpricing YES → flag BUY_YES
- If `1 - no_ask > ci_high` → market is overpricing YES → flag BUY_NO  
- Otherwise → WATCH (market price is within my uncertainty range)

Kelly criterion determines sizing. A few structural gates handle edge cases (settled markets, depth below threshold, etc.).

### How the legacy LLM scoring works

```
p_literal = base_rate × time_decay × news_pressure × x_buzz × llm_boost × event_llm
p_calibrated = Platt scaling of p_literal
ev = p_calibrated - market_price
```

Then 17 gates, most of which were added after watching specific bet patterns lose money (e.g. TRUMP_NO_BLOCK added after 29 Trump BUY_NO bets hit 37.9% win rate).

---

## Running it

### Requirements

- Python 3.9+
- macOS (for 24/7 launchd auto-restart) or Linux (manual process management)
- Kalshi account — the public API works without a key for basic polling

### Setup

```bash
git clone https://github.com/yourusername/kalshi-edge.git
cd kalshi-edge
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 -m pytest -q   # 246 tests — good baseline check
```

### Mock mode (safe, no API calls)

```bash
KALSHI_MOCK=1 python3 -m app.runner
# Second terminal:
python3 -m app.dashboard
# Open http://localhost:8777
```

Mock mode generates deterministic fake prices for ~660 markets. Good for exploring the dashboard and understanding the scoring logic without any real data.

### Live mode

```bash
cp config/runtime.env.example config/runtime.env
# Set KALSHI_MOCK=0 in that file
make run-live
```

See [docs/QUICKSTART.md](docs/QUICKSTART.md) for a full walkthrough including what each dashboard tab shows.

### 24/7 on macOS

```bash
make install-24x7-all   # installs launchd services, starts on login
make local-status        # check what's running
```

---

## The calibration data

The posteriors are trained from outcomes fetched via `make fetch-outcomes` (Kalshi's public API). Phrase base rates come from transcripts I collected manually — ~337 files across Trump, Leavitt, Mamdani, Powell, Starmer, and others.

The transcripts aren't included in this repo because of copyright uncertainty (some came from Rev.com, some from YouTube auto-captions). [data/corpus/README.md](data/corpus/README.md) has a guide to building your own corpus from public-domain sources (whitehouse.gov and federalreserve.gov transcripts are US government works and completely free to use).

| Speaker | Resolved outcomes | Transcripts I collected |
|---------|------------------|------------------------|
| Trump | 5,652+ | 179 (not included) |
| Leavitt | 338 | 85 (not included) |
| Mamdani | 924 | 27 (not included) |
| Powell | 54 | 33 (not included) |
| NBA/NCAAB/MLB/MMA | 900+ combined | N/A — uses phrase floor overrides |

---

## Useful commands

```bash
# Refresh data
make fetch-markets      # pull current Kalshi market definitions
make fetch-outcomes     # pull resolved outcomes (runs in ~30 seconds)
make fetch-poly         # Polymarket cross-prices (optional)

# Calibration
make calibrate          # full pipeline — takes a few minutes

# After you've been running live
make record-outcomes    # match your BUY cards to their outcomes
make report-outcomes    # print a P&L breakdown
make backtest           # historical win rate by segment
```

---

## Known problems

1. **BayesianScorer fires too many NBA BUY_YES bets.** The NBA speaker prior is 0.591 (high, because many NBA phrases do get said), which pushes estimates too high for individual phrases the market has already priced. Adding a minimum CI exclusion margin would likely fix this.

2. **MLB BUY_NO is losing badly.** "Bunt" (19% win rate, -$5.94), "triple" (8% win rate, -$3.39). Something about how MLB games generate common phrases doesn't match the historical frequency data I'm using. Either the outcomes data is different from what I think it is, or there's a recency bias I haven't accounted for.

3. **The legacy scorer is worse than the market at predicting outcomes.** This was a hard thing to admit after building it, but the Brier score doesn't lie. The Bayesian model is cleaner conceptually but is currently losing money too — so neither is actually working yet.

4. **No corpus included.** This means calibration out of the box is thin. The system falls back to priors from `config/base_rates_priors.yaml`, which are conservative but less accurate than trained rates.

5. **macOS only for 24/7 mode.** The auto-restart mechanism uses launchd. A Docker setup would make this portable to Linux servers.

---

## Project layout

```
app/
  bayesian_scorer.py   the current model — ~600 lines
  scoring.py           the legacy model — ~2,600 lines with 17 gates
  runner.py            async service orchestrator
  dashboard.py         web UI
  bayesian_rates.py    loads/computes Beta posteriors
  db.py                SQLite setup, WAL healing
  ... (40 modules total)

scripts/               67 maintenance and data scripts
config/
  base_rates.yaml      manually set phrase rates
  base_rates_auto.yaml auto-calibrated from outcomes
  runtime.env.example  all env var options
  llm/                 LLM instruction files (text, editable without code changes)
brain/                 architecture docs, decisions log, scoring rules spec
tests/                 246 pytest tests
docs/
  QUICKSTART.md        getting started walkthrough
  archive/             65-session development log, all build plan versions
```

---

## If you want to contribute

The most useful thing anyone could do is figure out why MLB BUY_NO keeps losing. I've looked at it and can't find the pattern. If you have a theory, open an issue.

The second most useful thing is a Docker setup for Linux.

Please don't open PRs that add more LLM signal layers — the data is pretty clear that they make things worse.

If you make changes to the scoring logic in `app/scoring.py` or `app/bayesian_scorer.py`, the convention (documented in `brain/08_DECISIONS_LOG.md`) is to cite actual bet counts and win rates from the outcome data, not just theoretical arguments.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full set of guidelines.

---

## License

MIT. See [LICENSE](LICENSE).

---

## Disclaimer

This software lost money in live trading. It is a research project. See [DISCLAIMER.md](DISCLAIMER.md).
