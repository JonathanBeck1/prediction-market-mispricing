# kalshi-edge

I spent about three months building a system to find mispriced markets on [Kalshi](https://kalshi.com). Specifically the "speaker mention" markets — bets on whether a politician says a specific phrase during a speech, or whether a sports broadcaster says a specific word during a game.

The idea was simple enough: if I could measure how often someone actually uses a phrase across hundreds of real speeches and games, and the market price didn't match that frequency, I should be able to bet on the gap.

**It's running right now on my Mac and it's currently down $15.34 across 1,107 resolved bets.** I'm open-sourcing it because I think the code and the data pipeline are more interesting than the P&L, and honestly I'd like to know what I'm missing.

> This is not financial advice. See [DISCLAIMER.md](DISCLAIMER.md).

---

## What Kalshi mention markets actually are

If you haven't traded these before — Kalshi has markets like:

- "Will Trump say the word 'tariff' in his press conference today?" → resolves YES or NO at close
- "Will the NBA broadcast mention 'alley-oop' during tonight's game?" → same idea
- "Will Karoline Leavitt say 'illegal alien' at the White House briefing?" → YES/NO

Each market resolves based on whether Kalshi's team finds the exact phrase (or a close match) in the official transcript. You can buy YES if you think the phrase gets said, or NO if you think it doesn't. The prices work like any other prediction market — 55¢ for YES means the market thinks there's a 55% chance the phrase gets said.

The edge I was looking for: the market prices these based on general sentiment. I wanted to price them based on historical frequency — how often has Trump actually said "tariff" in the last 179 speeches I have transcripts for?

---

## The numbers behind this

Before getting into how it works, here's the scale of data the system is built on:

**Kalshi outcomes tracked:** 12,490 total resolved markets across 17 speaker categories

| Speaker / category | Resolved outcomes |
|---|---|
| NBA broadcasts | 2,747 |
| Auto-detected | 3,119 |
| NCAAB broadcasts | 1,886 |
| Trump | 2,025 |
| MLB broadcasts | 712 |
| MMA/UFC broadcasts | 504 |
| Mamdani | 425 |
| Hochul | 243 |
| Starmer | 89 |
| Leavitt | 338 |
| Newsom | 126 |
| Carney | 96 |
| Melania | 84 |
| Powell/Fed | 54 |
| Homan | 25 |

**Corpus transcripts collected:** 337 files across 7 speakers
- Trump: 179 transcripts (rallies, briefings, addresses, signings, interviews — from 2022 through early 2026)
- Leavitt: 85 press briefings
- Mamdani: 27 events
- Powell: 33 Fed press conferences
- Starmer: 8 transcripts
- Carney: 3 transcripts
- Homan: 2 transcripts

**What's in the database right now:**
- 5,951 tracked markets
- 547,265 price snapshots
- 161,312 scored action cards
- 39,487 phrase hit records
- 343 tracked events
- 1,107 bets matched to outcomes (with P&L)

**Statistical model:** 1,761 Beta-Binomial posteriors across (speaker, phrase) pairs, trained from the 12,490 outcomes above

---

## How I was getting data

This is the part that took the most work to get right.

### Market prices

The Kalshi public API (`GET /markets`) gives you current YES/NO ask prices, bid-ask spreads, and book depth for every active market. No authentication needed, 20 requests/second on the free tier. The system polls this every 30 seconds and writes every price change to SQLite. Over a few months that accumulates to half a million snapshots.

If you want the higher rate tier (30 req/s + WebSocket streaming), Kalshi has an Advanced API program — it's free, you just apply through their typeform. The `KALSHI_API_KEY_ID` and `KALSHI_API_KEY_PATH` settings in the config handle that.

### Transcripts (the hard part)

This is where OpenClaw comes in. Getting live transcripts during active speeches is genuinely difficult. For most sources, the transcript page updates live as someone speaks — you need a real browser to get it, not just an HTTP request.

**OpenClaw** is a browser automation relay. You point it at a URL and it opens the page in an actual browser session, waits for JavaScript to render, and returns the body text. The system has three modes:

1. **Direct HTTP** — for transcript sources that serve plain text or static HTML. Fast, no browser needed, works for most pre-event corpus building.

2. **OpenClaw browser relay** — for live transcripts that require JavaScript rendering. The runner calls `openclaw browser open <url>` then `openclaw browser evaluate --fn "() => document.body.innerText"` to get the rendered text. Works for news wire sites, live caption feeds, etc.

3. **Fallback chain** — tries direct HTTP first, falls back to OpenClaw if that returns empty. Both sources run in the background; whichever returns first wins.

The transcript ingestor polls whatever URLs you configure every 30 seconds, runs the text through the phrase matcher, and writes hits to the `phrase_hits` table. When a hit is recorded, the scorer immediately sees it on the next cycle and the affected market's probability jumps to 0.98.

For corpus building (historical transcripts, not live), I used a mix of:
- **whitehouse.gov** — the press briefing and remarks transcripts are public domain and update within hours of any event
- **Rev.com** — professional transcription service, has most Trump rallies. The `scripts/rev_transcript_cleaner.py` handles their HTML format
- **factba.se** and **C-SPAN** — supplementary sources for older transcripts
- Manual HTML saves for a few edge cases

The corpus files live in `data/corpus/<speaker>/` with filenames like `briefing_2026-01-15_01.txt`. The convention matters because the calibration scripts use the event type in the filename to compute event-specific base rates — "rally" vs "briefing" vs "signing" can have very different phrase frequencies.

### Outcome data

`make fetch-outcomes` hits the Kalshi `/settlements` API which gives you every resolved market and whether it settled YES or NO. I have 12,490 of these. This is what trains the Bayesian model and calibrates the legacy scorer.

### Cross-market signals (Polymarket)

`make fetch-poly` pulls prices from Polymarket for markets that match the same underlying questions. When Polymarket has Trump saying "tariff" at 62¢ and Kalshi has it at 41¢, that divergence is a signal (though usually the divergence exists because the markets don't perfectly overlap in time or resolution criteria).

The matching is fuzzy — it uses phrase overlap and event title similarity to link the two markets. There's a confidence score attached to each match that the legacy scorer uses to decide how much weight to give the signal.

### Wallet flow signals

`make fetch-wallet` hits Polymarket's trade flow API. The idea was to detect "smart money" — large trades moving the price in an unusual direction shortly before an event. The signal tracks things like `conviction_weighted_flow` (price-distance weighted) and `extreme_bet_count` to identify when sophisticated traders are loading up on one side.

In practice this signal was noisy and the adaptive signal learner (more on that below) ended up driving its weight close to zero.

### News signals

`make fetch-x` pulls posts from a configured list of political journalists and White House accounts via the X API. The signal is used to boost phrase probabilities when a specific topic is heavily in the news. Again, the adaptive learner mostly neutralized this for political markets.

### White House schedule

`make fetch-wh` pulls the official White House daily schedule. This tells you if Trump is speaking at a bill signing vs a press conference vs a rally today — different event types have very different phrase frequency profiles.

---

## What I actually built

### The core loop

There's one Python process running 24/7 that manages five concurrent service loops:

**KalshiWatcher** — polls the Kalshi API every 30 seconds, writes price changes to `market_snapshots`. In live mode it hits the real API. In mock mode it returns deterministic fake prices so you can test without touching anything real.

**TranscriptIngestor** — polls whatever transcript URLs you've configured, runs the phrase matcher on the text, writes hits to `phrase_hits`. The phrase matcher does boundary-safe literal matching with negation detection — "will NOT say Iran" doesn't count as a hit on "Iran". It also detects attribution ("he said 'tariff'") and skips those.

**Scorer** (BayesianScorer or ScoringEngine) — reads the latest price snapshots and phrase hits, runs every market through the scoring model, and emits action cards. More on the two models below.

**MaintenanceRunner** — runs in the background on configurable intervals. Fetches fresh market definitions, pulls new outcomes, updates Polymarket prices, recomputes calibration. In 24/7 mode this keeps everything current without you doing anything.

**Watchdog** — monitors how stale the price data is. If snapshots stop updating for more than 5 minutes, it exits the process and launchd auto-restarts it. This is what makes the system genuinely 24/7 — it recovers from crashes, network blips, and sleep/wake cycles automatically.

### The phrase matcher

The matcher does boundary-aware regex matching, not substring search. "Iran" doesn't match "Iranian". "tariff" matches "tariffs" because it uses word-boundary stemming. Unicode normalization handles smart quotes and em-dashes.

It also maintains a negation window — 10 tokens before a phrase match, it checks for negation words ("not", "never", "refused to") and attribution words ("said", "claimed", "according to"). Hits with these are still recorded but flagged, so the scorer can treat them differently.

The phrase dictionary is built from `config/base_rates.yaml` plus auto-calibrated entries from `config/base_rates_auto.yaml`. There are currently ~1,800+ phrases tracked across all speakers.

### The database

Everything goes into SQLite at `data/edge.db`. WAL mode with `synchronous=FULL`. Five tables matter:

- `markets` — market definitions (ID, speaker, phrase, close time)
- `market_snapshots` — every price change (yes_ask, yes_bid, no_ask, depth, spread)
- `phrase_hits` — every time a phrase was detected in a transcript
- `action_cards` — every scored card the system produced, with full model state
- `outcome_reviews` — bets matched to their resolved outcomes, with P&L

The DB had a lot of corruption issues early on. The root cause was all five service loops sharing a single SQLite connection across threads. SQLite connections are not thread-safe even with `check_same_thread=False` — that flag just suppresses the exception, it doesn't add locking. The fix was giving each loop its own connection. There's also auto-healing logic that runs on startup: if the WAL file is stale (from a crash mid-write), it does a checkpoint and removes the orphaned sidecar files before opening.

### The scoring models

**BayesianScorer** (the current default)

Built from scratch after the legacy model's performance analysis. For each (speaker, phrase) pair in the market, it loads a pre-computed Beta-Binomial posterior from `data/bayesian_rates.json`. That file is rebuilt from the 12,490 historical outcomes every few hours by the maintenance runner.

The posterior gives you a mean probability and a 90% credible interval. The decision logic:

```
if yes_ask < ci_low:
    → BUY_YES (market is pricing YES below our CI lower bound)
if (1 - no_ask) > ci_high:
    → BUY_NO  (market's implied YES price is above our CI upper bound)
else:
    → WATCH   (market price is inside our uncertainty range, no edge)
```

Kelly criterion sizes the bet based on edge and the loss if wrong. A handful of structural gates handle edge cases: settled markets, books too thin to enter, spreads too wide.

The hierarchical part: for phrases with sparse data (< 3 observations), instead of returning nothing, it uses the speaker-level prior — the average YES rate across all phrases for that speaker. So if you have a new Trump phrase you've never seen resolved, it starts at ~0.45 (Trump's overall base rate) rather than 0 or some arbitrary number.

**ScoringEngine** (the legacy model, still available)

The original model. Starts with the same historical base rates but runs them through several signal layers:

```
p_literal = base_rate × time_decay × news_pressure × x_buzz × llm_boost × event_llm
```

- **base_rate** — from the manual `config/base_rates.yaml` or auto-calibrated `config/base_rates_auto.yaml`
- **time_decay** — if there's a live event, probability decays as time runs out. Replaced a static exponential curve with empirical hazard rates: some phrases get said early, some late, some uniformly — the hazard model captures these patterns from the corpus
- **news_pressure** — how heavily the phrase is trending in recent news and X posts
- **x_buzz** — signal from tracked political journalist accounts
- **llm_boost** — OpenAI GPT-5-mini analyzes the phrase in the context of today's event and outputs a multiplier. Runs every 45 minutes on a schedule
- **event_llm** — separate per-event LLM analysis (not per-phrase). Looks at the specific event happening today and generates p_floor and p_override values for contextually certain phrases

Then Platt scaling calibrates the p_literal to account for systematic bias in the model, and a 17-gate decision stack filters out bets that historical data showed were consistently losing.

This is the model that cost ~$50/month in OpenAI calls and whose Brier score was 0.352 vs the market's 0.238.

### The adaptive signal learner

One of the later additions. It watches `outcome_reviews` and tracks win rates per `(speaker, signal_source)` combination — e.g. "Trump bets that were influenced by LLM_BOOST: 31% WR". It outputs a weight file that the scorer applies to scale each signal.

A signal that consistently loses money gets its weight driven toward 0. A signal that wins gets boosted toward 1.5. This is what eventually neutralized the wallet flow and X buzz signals for most speakers — the live data showed they were noise.

### The event system

Events are the core context the scorer uses. There are three states:

- **Scheduled** — speech hasn't started, using historical base rates + pre-event signals
- **Live** — speech is in progress, time decay applies, phrase hits override everything
- **Ended** — speech finished without the phrase → probability collapses toward 0.02

The system auto-seeds events from the Kalshi market metadata (markets tied to a specific date/event get detected automatically). You can also manually add events to `config/events.yaml` with custom p_overrides and p_floors:

```yaml
events:
  - event_id: "trump:2026-03-24:signing-01"
    speaker: trump
    event_type: signing
    p_overrides:
      kristi: 0.95    # Kristi Noem is being sworn in — guaranteed mention
    p_floors:
      deport: 0.65    # DHS Secretary ceremony — elevated structural probability
```

### The LLM instruction layer

The LLM analysis is driven by Markdown files in `config/llm/` rather than hardcoded prompts. There's a `global_signals_guide.md` that tells the model which phrases to boost or suppress, a `per_event_guide.md` for event-specific analysis, and a `trump_patterns.md` with data-backed behavioral profiles.

The key lesson from debugging the LLM layer: the model's intuition was wrong about Trump. It suppressed phrases like "sleepy joe" at diplomatic events because "it's inappropriate for a diplomatic ceremony." The historical data showed Trump says "sleepy joe" at 92% of ALL event types including bill signings. The fix was rewriting the prompts to include actual win rate data so the model couldn't override observed frequencies with creative reasoning.

### The dashboard

Local web UI at port 8777. Five tabs:

- **Action Cards** — live scored markets, sorted by conviction
- **Performance** — P&L by segment, win rate charts, calibration health
- **Events** — upcoming and live events with phrase-by-phrase probability breakdown
- **Intelligence** — signal weight table, LLM analysis, regime alerts
- **Sports** — NBA/NCAAB/MLB/MMA markets grouped by game

The whole thing is single-file Python (`app/dashboard.py`, about 5,200 lines) serving a custom HTML/JS page. No external dashboard framework. It runs as a separate process from the runner and connects to the same SQLite DB.

### WhatsApp alerts

When a high-conviction BUY card appears, it can ping your phone via OpenClaw. The notifier formats the action card as a readable text message (`app/notifier.py`) and sends it through `openclaw message send <number> <text>`. This is how I used it during live events — the system would text me "BUY NO @ 0.31 | NBA alley-oop | KXNBAMENTION-ATLLAL" and I'd go check the market.

---

## What's working and what isn't

Here's the real segment breakdown from 1,107 resolved bets:

| Segment | Bets | Win Rate | P&L | Per bet |
|---------|------|----------|-----|---------|
| NBA BUY_NO | 283 | 51.9% | **+$12.62** | +$0.045 |
| NCAAB BUY_NO | 235 | 57.0% | **+$6.20** | +$0.026 |
| NCAAB BUY_YES | 24 | 41.7% | **+$3.63** | +$0.151 |
| MMA BUY_NO | 60 | 61.7% | **+$2.03** | +$0.034 |
| MMA BUY_YES | 12 | 41.7% | **+$1.03** | +$0.086 |
| MLB BUY_NO | 128 | 42.2% | **-$17.61** | -$0.138 |
| NBA BUY_YES | 93 | 33.3% | **-$8.68** | -$0.093 |
| Trump BUY_NO | 42 | 33.3% | **-$8.50** | -$0.202 |
| Trump BUY_YES | 125 | 33.6% | **-$3.28** | -$0.026 |
| Legacy model total | 975 | 49.2% | **-$1.97** | -$0.002 |
| Bayesian model total | 132 | 32.6% | **-$13.37** | -$0.101 |

The sports BUY_NO markets (NBA, NCAAB, MMA) are consistently profitable across hundreds of bets. If the system had only ever traded those three segments, the total P&L would be around +$20 gross.

The Bayesian model has only been live since April 14 (132 resolved bets). The main problem with it right now: 76% of its bets are BUY_YES, and most of those are NBA. The NBA speaker prior is 0.591 — high because so many NBA phrases do get mentioned — and the model uses that prior for thin-data phrases, which biases it toward buying YES on things the market has already priced correctly.

The biggest single money drain is MLB BUY_NO. "Bunt" has a 19% win rate on 17 bets (-$5.94). "Triple" has 8% win rate on 12 bets (-$3.39). Somehow these phrases are getting said far more often than the historical data suggests they should. I genuinely don't know why and it's the thing I most want someone to look at.

Retroactively: if I'd only traded NBA/NCAAB/MMA BUY_NO and excluded everything else, total P&L would be around +$20 on 578 bets. Whether that edge holds forward or whether it's historical pattern-fitting is the open question.

---

## Getting started

### Requirements

- Python 3.9+
- macOS (for 24/7 auto-restart via launchd) or Linux (manual process management)
- Kalshi account — the public API doesn't require authentication for basic polling

### Install

```bash
git clone https://github.com/yourusername/kalshi-edge.git
cd kalshi-edge
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 -m pytest -q   # 246 tests, should all pass
```

### Mock mode first

```bash
KALSHI_MOCK=1 python3 -m app.runner
# Second terminal:
python3 -m app.dashboard
# Open http://localhost:8777
```

Mock mode generates ~660 deterministic fake markets with realistic price distributions. Good for exploring the dashboard and understanding how scoring works without touching any real data.

### Live mode

```bash
cp config/runtime.env.example config/runtime.env
# Set KALSHI_MOCK=0 in that file
# Everything else is optional
make run-live
```

### Switch between models

```bash
# Bayesian (default) — no LLM, free to run
USE_BAYESIAN_SCORER=1 python3 -m app.runner

# Legacy with LLM signals — requires OPENAI_API_KEY in runtime.env
USE_BAYESIAN_SCORER=0 python3 -m app.runner
```

### 24/7 on macOS

**Before installing, do these three things — they will save you hours of debugging:**

1. Remove the quarantine flag macOS puts on cloned files:
   ```bash
   xattr -dr com.apple.quarantine /path/to/kalshi-edge
   ```

2. Go to **System Settings → Privacy & Security → Full Disk Access** and enable Terminal (or your terminal emulator). Without this, launchd services silently fail to read files.

3. Keep the repo outside `~/Documents`, `~/Desktop`, `~/Downloads` — macOS applies extra sandbox restrictions to those folders that block launchd.

Then install:

```bash
make install-24x7-all   # sets up launchd services, starts on login
make local-status        # check what's running, tail logs
```

See [docs/QUICKSTART.md](docs/QUICKSTART.md) for the full macOS setup walkthrough including Gatekeeper approval and sleep prevention.

---

## Useful commands

```bash
# Fetch fresh data
make fetch-markets     # Kalshi market definitions
make fetch-outcomes    # resolved outcomes (the training data)
make fetch-poly        # Polymarket cross-prices

# Rebuild calibration from outcomes
make calibrate

# After running live for a while
make record-outcomes   # match your BUY cards to outcomes
make report-outcomes   # P&L breakdown
make backtest          # win rates by segment, confidence, regime

# Health check
make health-check
make doctor            # verify repo, venv, package structure
```

---

## Project layout

```
app/
  runner.py              async service orchestrator (5 loops)
  bayesian_scorer.py     current model — Beta-Binomial CI comparison
  scoring.py             legacy model — 17-gate LLM-enhanced stack (~2,600 lines)
  dashboard.py           web UI (~5,200 lines)
  transcript_sources.py  HTTP + OpenClaw + file sources, fallback chain
  transcript_ingestor.py polls sources, runs phrase matcher, writes hits
  phrase_matcher.py      boundary-safe regex with negation/attribution detection
  bayesian_rates.py      Beta posteriors loader + hot-reload cache
  bayesian_scorer.py     CI-vs-market decision, Kelly sizing
  base_rates.py          YAML base rate lookup
  bias_map.py            series-specific empirical rate overrides
  rolling_rates.py       last-3/5/10-speech phrase frequency signals
  phrase_hazard.py       empirical hazard rates for live event time decay
  signal_learner.py      adaptive signal weight learner from outcomes
  phrase_cooccurrence.py lift table — "if A was said, does that change P(B)?"
  phrase_correlation.py  phi-coefficient correlation matrix
  event_detector.py      scheduled/live/ended state machine
  event_signals.py       per-event LLM overrides and floors
  polymarket.py          Polymarket cross-market price signal
  wallet_flow.py         smart-money flow signal from Polymarket trades
  price_velocity.py      rate-of-change signal on YES prices
  calibration.py         Platt scaling calibrator for legacy model
  db.py                  SQLite setup, WAL healing, schema management
  maintenance.py         background task scheduler
  notifier.py            WhatsApp alerts via OpenClaw
  watchdog.py            staleness monitor, triggers auto-restart
  ... (40 modules total)

scripts/
  fetch_*.py             data fetching scripts (12 scripts)
  compute_*.py           signal/calibration computation (11 scripts)
  backtest_*.py          historical analysis (4 scripts)
  calibrate_base_rates.py full calibration pipeline
  record_outcomes.py     match BUY cards to outcomes
  report_outcomes.py     P&L breakdown
  analyze_event.py       LLM per-event analysis
  analyze_signals.py     LLM global signal analysis
  scrape_corpus.py       corpus transcript scraper
  rev_transcript_cleaner.py  Rev.com HTML → plain text
  health_check.py        runtime health verification

config/
  base_rates.yaml        manually-curated phrase base rates by speaker
  base_rates_auto.yaml   auto-calibrated from resolved outcomes
  base_rates_priors.yaml conservative priors for thin/new speakers
  runtime.env.example    all config options with comments
  events.yaml            upcoming events with p_overrides/p_floors
  llm/                   LLM instruction files (edit without code changes)
    trump_patterns.md    data-backed behavioral profiles for prompts
    global_signals_guide.md  what to boost/suppress globally
    per_event_guide.md   how to analyze per-event context

brain/                   architecture specs, decisions log, scoring rules
  08_DECISIONS_LOG.md    every gate decision with bet counts + P&L evidence

tests/                   246 pytest tests
data/                    runtime state — gitignored
  edge.db                the database
  corpus/                transcripts — not included (see corpus/README.md)
  *.json                 signal caches, calibration outputs
docs/
  QUICKSTART.md          getting started walkthrough
  archive/               65-session development log, all build plan versions
```

---

## Known problems

**BayesianScorer is firing too many NBA BUY_YES bets.** The NBA speaker prior (0.591) is high because many NBA phrases actually do get said, but individual thin-data phrases borrow that prior and end up overpriced. Adding a minimum CI exclusion margin — requiring `yes_ask < ci_low - 0.05` rather than just `< ci_low` — should help.

**MLB BUY_NO keeps losing.** "Bunt" at 19% win rate on 17 bets. "Triple" at 8% on 12. "Wild pitch" at 46% on 11. Something structural is wrong. My best guess is that the outcome data I'm training on has different resolution criteria from what Kalshi is actually using to settle recent markets, but I haven't confirmed that.

**The legacy scorer is worse than the market at predicting outcomes.** Brier score 0.352 vs the market's 0.238. This was hard to accept after building it, but it's what the data shows. The Bayesian model is conceptually cleaner but losing money in live trading too.

**No corpus included.** The 337 transcripts are the single most valuable calibration resource and they're not in the repo because of copyright uncertainty. Without them, calibration falls back to conservative priors and accuracy is lower.

**macOS-only for 24/7 daemon mode.** The auto-restart mechanism uses launchd. Should work on Linux with systemd with minor changes to `scripts/manage_launchd.py`, but I haven't tested it.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

The thing I most want help with is figuring out what's wrong with MLB BUY_NO. If you have a theory — wrong calibration data, resolution criteria mismatch, something else — open an issue.

The second most useful thing is corpus expansion: if you know reliable, redistributable public-domain sources for speaker transcripts (especially Carney, Starmer, or Homan where coverage is thin), that directly improves calibration accuracy.

If you make changes to any scoring logic, the convention in this project is to cite actual bet counts and win rates from `outcome_reviews` in the PR. Every gate in the legacy model exists because a specific data pattern justified it — there's a record of those decisions in `brain/08_DECISIONS_LOG.md`. New scoring changes should follow the same standard.

---

## License

MIT. See [LICENSE](LICENSE).

---

## Disclaimer

This software lost real money in live trading. Read [DISCLAIMER.md](DISCLAIMER.md).
