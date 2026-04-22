# Runbook

Manual-only invariant: operator workflow ends at Action Card review; no order execution automation.

## Start (macOS zsh)

### 1) Enter repo root

Run from the directory that contains both `app/` and `main.py`.

```bash
cd ~/kalshi-edge  # or wherever you cloned it
pwd
ls
which python3
```

### 2) Create and activate virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

### 3) Start runtime (either command)

```bash
KALSHI_MOCK=1 python3 -m app.runner
```

```bash
KALSHI_MOCK=1 python3 main.py
```

### 4) Run tests

```bash
python3 -m pytest -q
```

Current repo status: test suite is active (run `make test` for current pass count).

## Canonical Event-Day Workflow

Run these steps before a trading session to get the freshest data and health checks:

```bash
# 1) One-command pre-event refresh + health checks
make event-ready

# 2) Start live runner in terminal A
make run-live

# 3) Start dashboard in terminal B
make dashboard   # http://localhost:8777

# 4) After runner is live, enforce strict freshness gates
make health-check
```

The dashboard shows event-grouped cards with Polymarket comparisons. Click any event to expand and see all cards for that event. Best bets (highest EV) are listed first.

## Truth Social (Trump) — operator SOP

Manual-only: there is no automated Truth Social fetch in-repo; **you** supply posts so `process_truth_social.py` can set time-decayed boosts before speeches.

1. **Add a post** (preferred):  
   `python3 scripts/add_truth_social_post.py "Full post text" --eastern "2026-03-31 4:57pm"`  
   Optional: `--url https://truthsocial.com/...` — use **real** Truth Social `posted_at` for decay (2h / 6h / 12h buckets).
2. **Or** merge into `data/truth_social_posts.json` using the same schema as `add_truth_social_post.py` (`id`, `content`, `posted_at`, `url`).
3. **After bulk edits:** run `python3 scripts/process_truth_social.py` so `signals.yaml` updates (or wait for maintenance ~15m).
4. **Retention:** `add_truth_social_post` prunes posts **older than 7 days** on each run. For archival, snapshot the JSON elsewhere before pruning.
5. **Dedup:** near-duplicate posts differ only by smart quotes; normalize or keep one copy so phrase boosts are not double-counted.

## Exact Commands (Copy/Paste)

### Terminal A (runner)

```bash
cd ~/kalshi-edge  # or wherever you cloned it
source .venv/bin/activate
make event-ready
make run-live
```

### Terminal B (dashboard)

```bash
cd ~/kalshi-edge  # or wherever you cloned it
source .venv/bin/activate
make dashboard
```

### Terminal C (strict runtime gate check after runner is live)

```bash
cd ~/kalshi-edge  # or wherever you cloned it
source .venv/bin/activate
make health-check
```

### Single terminal (background runner + dashboard)

```bash
cd ~/kalshi-edge  # or wherever you cloned it
make local-up
make local-status
make local-restart
make local-down
```

Use this mode if repo path is under `~/Documents`, `~/Desktop`, or `~/Downloads`.
macOS privacy can prevent `launchd` services from reading `.venv` in those folders.

## 24/7 Always-On Mode

Run one live process with automatic maintenance refresh:

```bash
make run-live-24x7
```

Recommended for unattended mode:

```bash
cp config/runtime.env.example config/runtime.env
# then edit config/runtime.env with tokens/targets/URLs
```

`load_settings()` auto-loads `config/runtime.env` (or `RUNTIME_ENV_FILE` override), so launchd services get the same environment each reboot.

This enables the background maintenance loop (live mode default) to auto-run:
- `scripts/fetch_markets.py`
- `scripts/fetch_polymarket.py`
- `scripts/fetch_wallet_flow.py`
- `scripts/fetch_x_signals.py` (only if `X_BEARER_TOKEN` exists)
- `scripts/fetch_outcomes.py`
- `scripts/record_outcomes.py`

Adjust cadence via env vars:
- `MAINTENANCE_INTERVAL_SEC` (default `60`)
- `EVENT_SEED_INTERVAL_SEC` (default `300`)
- `FETCH_MARKETS_INTERVAL_SEC` (default `3600`)
- `FETCH_POLY_INTERVAL_SEC` (default `1800`)
- `FETCH_WALLET_FLOW_INTERVAL_SEC` (default `1800`)
- `FETCH_X_INTERVAL_SEC` (default `1800`)
- `FETCH_OUTCOMES_INTERVAL_SEC` (default `21600`)
- `RECORD_OUTCOMES_INTERVAL_SEC` (default `21600`)

Watchdog controls (stale-data + scorer-idle restart protection):
- `WATCHDOG_ENABLED` (default `1` in live mode)
- `WATCHDOG_INTERVAL_SEC` (default `60`)
- `WATCHDOG_MAX_SNAPSHOT_AGE_SEC` (default `300`)
- `WATCHDOG_MAX_SCORER_IDLE_SEC` (default `300`)
- `WATCHDOG_STARTUP_GRACE_SEC` (default `180`)
- `WATCHDOG_BREACH_LIMIT` (default `3`)
- `WATCHDOG_EXIT_ON_STALE` (default `1`, triggers launchd restart)

Disable maintenance loop explicitly:

```bash
MAINTENANCE_ENABLED=0 make run-live
```

## 24/7 Auto-Start (macOS launchd)

Install persistent runner + dashboard services:

```bash
make install-24x7-all
make status-24x7
```

Uninstall:

```bash
make uninstall-24x7-all
```

## Outcome Review + Backtest Workflow

Run this after outcomes resolve to measure real edge:

```bash
# 1) Refresh finalized outcomes cache
make fetch-outcomes

# 2) Record resolved outcomes against decision-time BUY cards (default: first_buy)
make record-outcomes

# 3) Print realized win-rate / P&L summaries
make report-outcomes

# 4) Build regime scorecards (tags, confidence buckets, side splits)
make backtest

# 5) Tune safe-mode policy thresholds from settled outcomes
make tune-policy
# -> writes config/safe_mode.env
```

Use safe mode during live sessions:
```bash
source config/safe_mode.env
make run-live
```

## Common Runtime Modes

- M0 default:
  - `KALSHI_MOCK=1`
  - empty `TRANSCRIPT_URLS`
- Live Kalshi prices:
  - `KALSHI_MOCK=0` (or `make run-live`)
  - Fetches real bid/ask from Kalshi public API
- Direct HTTP transcripts:
  - `TRANSCRIPT_SOURCE=directhttp`
  - `TRANSCRIPT_URLS=https://...,...`
- OpenClaw Browser Relay mode:
  - `TRANSCRIPT_SOURCE=openclaw`
  - `OPENCLAW_BROWSER_PROFILE=openclaw` (or `chrome` for relay mode)
  - Make sure `openclaw` CLI is in PATH
  - The system runs: start browser -> open URL -> evaluate innerText -> parse JSON
- Legacy OpenClaw mode (single command):
  - `OPENCLAW_SCRAPE_CMD='your command {url}'` (overrides browser relay)

## Corpus + Calibration Workflow

Use this after adding new transcript files into `data/corpus/<speaker>/`:

```bash
# 1) Inspect observed phrase hit rates
make analyze

# 2) Rebuild base-rate priors from corpus
make calibrate

# 3) Refresh finalized historical outcomes (optional but recommended)
make fetch-outcomes
```

For stricter calibration on larger corpora:
```bash
source .venv/bin/activate
python3 scripts/calibrate_base_rates.py --min-event-docs 5 --min-speaker-docs 8 --prior-strength 3.0
```

To disable outcomes blending for a pure transcript-only run:
```bash
source .venv/bin/activate
python3 scripts/calibrate_base_rates.py --no-outcomes
```

Transcript file naming convention:
- path: `data/corpus/<speaker>/<event_type>_<YYYY-MM-DD>_<seq>.txt`
- speakers: `trump`, `leavitt`, `mamdani` (exact spelling)
- examples:
  - `data/corpus/trump/rally_2026-03-01_01.txt`
  - `data/corpus/leavitt/briefing_2026-02-19_01.txt`

## Polymarket Refresh

Fetch the latest Polymarket mention market prices:

```bash
make fetch-poly
```

This scrapes the Polymarket Gamma API, discovers active mention markets, extracts YES prices per phrase, and cross-matches with Kalshi markets. Output goes to `data/polymarket_prices.json`.

Run this before each trading session. The scoring engine automatically loads the cached prices.

## X Refresh (Deterministic, Non-LLM)

```bash
make fetch-x
```

Requirements:
- `X_BEARER_TOKEN` must be set
- account list in `config/x_watchlist.yaml`

Behavior:
- Fetches only new posts (`since_id`) for tracked accounts
- Writes raw posts to `data/x_posts.jsonl`
- Tracks usage in `data/x_usage.json`
- Updates deterministic `x_buzz` in `data/signals.yaml`
- Enforces budget caps:
  - `X_MONTHLY_BUDGET_USD` (default 25)
  - `X_DAILY_POST_RESOURCE_CAP` (default 300)

## Dashboard

```bash
make dashboard
# → http://localhost:8777
```

Features:
- **Event groups**: each Kalshi event is a collapsible section with its real title
- **Best Edge ribbon**: top bets per event sorted by EV
- **Poly vs Kalshi tab**: dedicated cross-market discrepancy table
- **Stats bar**: BUY YES/NO counts, coverage gaps, Poly links, wallet signal links, data age
- **Auto-refresh**: updates every 5 seconds

If port 8777 is already in use:
```bash
lsof -ti:8777 | xargs kill -9 2>/dev/null; make dashboard
```

## WhatsApp Notifications

Enable with:
```bash
export WHATSAPP_ENABLED=1
export WHATSAPP_TARGET="+1234567890"  # or WhatsApp chat ID
```
Sends formatted action cards via `openclaw message send --channel whatsapp`.
Only BUY_YES and BUY_NO cards are sent (WATCH is suppressed).
Throttled by material-change detection (no spam).

## DB Snapshot Pruning

Snapshots are pruned automatically every Sunday at 3am via macOS launchd (`com.kalshi-edge.prune`).
To run manually:

```bash
make prune
```

To keep more or fewer days:
```bash
source .venv/bin/activate && python3 scripts/prune_snapshots.py --days 7
```

Check the launchd job:
```bash
launchctl list | grep kalshi
```

Prune logs at `/tmp/kalshi-prune.log`.

## Makefile Targets Reference

| Target | Description |
|---|---|
| `make doctor` | Check environment (pwd, python, app/, .venv) |
| `make run` | Run with mock data (`KALSHI_MOCK=1`) |
| `make run-live` | Run with live Kalshi prices (`KALSHI_MOCK=0`) |
| `make run-live-24x7` | Live mode + background maintenance loop |
| `make event-ready` | Canonical pre-event refresh + health checks |
| `make health-check` | Freshness + coverage + signal completeness checks |
| `make test` | Run pytest |
| `make fetch-markets` | Fetch Kalshi mention market definitions |
| `make fetch-outcomes` | Fetch finalized historical outcomes |
| `make fetch-poly` | Fetch Polymarket prices + cross-match with Kalshi |
| `make fetch-wallet` | Build wallet-flow signal cache from recent public trades |
| `make fetch-x` | Fetch X posts for deterministic x_buzz updates |
| `make scrape` | Scrape transcripts into `data/corpus/` |
| `make analyze` | Corpus phrase hit analysis |
| `make calibrate` | Calibrate base rates from corpus + outcomes |
| `make ingest-corpus` | Ingest corpus transcripts into DB (phrase matching) |
| `make record-outcomes` | Insert resolved outcome reviews vs decision-time BUY cards |
| `make report-outcomes` | Realized win-rate / P&L report |
| `make backtest` | Regime scorecards from settled outcomes |
| `make tune-policy` | Tune thresholds and write `config/safe_mode.env` |
| `make dashboard` | Run dashboard web UI (localhost:8777) |
| `make prune` | Prune stale market snapshots (keep last 3 days) |
| `make install-24x7` | Install launchd runner service (auto-start) |
| `make install-24x7-all` | Install launchd runner + dashboard services |
| `make uninstall-24x7` | Remove launchd runner service |
| `make uninstall-24x7-all` | Remove launchd runner + dashboard services |
| `make local-up` | Start runner + dashboard in background |
| `make local-restart` | Restart local background runner + dashboard |
| `make local-down` | Stop local background runner + dashboard |
| `make local-status` | Show local background process status and logs |

## Manual Script Commands (Copy/Paste, Non-24/7)

Use this for one-off/manual workflows (not always-on daemon mode):

```bash
cd ~/kalshi-edge  # or wherever you cloned it
source .venv/bin/activate
```

```bash
# Market and pricing data
python3 scripts/fetch_markets.py
python3 scripts/fetch_polymarket.py
python3 scripts/fetch_wallet_flow.py
python3 scripts/fetch_outcomes.py
python3 scripts/fetch_x_signals.py
python3 scripts/health_check.py
```

```bash
# Corpus and calibration
python3 scripts/scrape_corpus.py
python3 scripts/ingest_corpus.py --fresh
python3 scripts/analyze_corpus.py
python3 scripts/calibrate_base_rates.py
python3 scripts/calibrate_base_rates.py --no-outcomes
```

```bash
# Outcome review and policy analysis
python3 scripts/record_outcomes.py --decision-mode first_buy
python3 scripts/record_outcomes.py --replace --decision-mode first_buy
python3 scripts/report_outcomes.py --days 120
python3 scripts/backtest_scorecards.py --days 120
python3 scripts/tune_policy.py --days 120
```

```bash
# Ops helpers
python3 scripts/prune_snapshots.py --days 3
python3 scripts/manage_launchd.py install --repo-root .
python3 scripts/manage_launchd.py install --repo-root . --with-dashboard
python3 scripts/manage_launchd.py uninstall
python3 scripts/manage_launchd.py uninstall --with-dashboard
```

```bash
# Utility scripts
python3 scripts/add_transcript.py --help
python3 scripts/cleanup_trump_corpus.py --help
```

## Key Environment Variables

- `DATA_DIR`, `DB_PATH`, `ACTION_CARDS_PATH`
- `WATCHER_INTERVAL_SEC`, `TRANSCRIPT_INTERVAL_SEC`, `SCORER_INTERVAL_SEC`
- `KALSHI_MOCK`
- `TRANSCRIPT_SOURCE`, `TRANSCRIPT_URLS`, `TRANSCRIPT_HTTP_TIMEOUT_SEC`
- `OPENCLAW_SCRAPE_CMD`, `OPENCLAW_TIMEOUT_SEC`, `OPENCLAW_BROWSER_PROFILE`
- `KALSHI_EVENT_SCRAPE_CMD` (optional event scrape fallback; stdout JSON)
- `WHATSAPP_ENABLED`, `WHATSAPP_TARGET`
- `FOCUS_EVENT_MARKETS` (default `1` in live mode, `0` in mock mode)
- `PRE_EVENT_WINDOW_SEC` (default `21600`, i.e. 6 hours)
- `MAX_SPREAD` (default `0.15`), `MIN_DEPTH` (default `0`), `EV_THRESHOLD` (default `0.03`)
- `PRE_EVENT_YES_THRESHOLD` (default `0.03`)
- `BLOCK_OFF_TOPIC_YES` (`0`/`1`, default `0`)
- `PENNY_PRICE_THRESHOLD` (default `0.00`)
- `MARKET_ANCHOR_WEIGHT`, `MARKET_ANCHOR_THRESHOLD`
- `POLY_MIN_CONFIDENCE`
- `WALLET_FLOW_WEIGHT`, `WALLET_MIN_CONFIDENCE`
- `X_BEARER_TOKEN`, `X_MONTHLY_BUDGET_USD`, `X_DAILY_POST_RESOURCE_CAP`
- `MAINTENANCE_ENABLED` (`0`/`1`, default `1` in live mode)
- `MAINTENANCE_INTERVAL_SEC`
- `EVENT_SEED_INTERVAL_SEC`
- `FETCH_MARKETS_INTERVAL_SEC`, `FETCH_POLY_INTERVAL_SEC`
- `FETCH_WALLET_FLOW_INTERVAL_SEC`
- `FETCH_X_INTERVAL_SEC`, `FETCH_OUTCOMES_INTERVAL_SEC`, `RECORD_OUTCOMES_INTERVAL_SEC`
- `WATCHDOG_ENABLED`, `WATCHDOG_INTERVAL_SEC`
- `WATCHDOG_MAX_SNAPSHOT_AGE_SEC`, `WATCHDOG_MAX_SCORER_IDLE_SEC`
- `WATCHDOG_STARTUP_GRACE_SEC`, `WATCHDOG_BREACH_LIMIT`, `WATCHDOG_EXIT_ON_STALE`

## Expected Outputs

- SQLite DB at `data/edge.db`
- Raw logs:
  - `data/raw/kalshi_snapshots.jsonl`
  - `data/raw/transcripts.jsonl` (when transcripts ingest)
- Action Cards:
  - console
  - `data/action_cards.jsonl`
  - `action_cards` table
- Caches:
  - `data/kalshi_markets.json`
  - `data/kalshi_events.json`
  - `data/kalshi_outcomes.json`
  - `data/polymarket_prices.json`
  - `data/wallet_signals.json`

## Health Checks

Run:

```bash
make health-check
```

Checks:
- latest snapshot freshness (`market_snapshots`)
- latest action-card freshness (`action_cards`)
- cache freshness (`data/kalshi_markets.json`, `data/kalshi_events.json`)
- coverage gap count (untracked open events)
- discovered-but-unsnapshotted event count/age
- Poly-link completeness ratio on cards
- wallet-signal availability ratio on cards

## Troubleshooting

### No transcript ingestion

- Verify `TRANSCRIPT_URLS` not empty when using `directhttp`.
- Verify URL accessibility and timeout values.
- For OpenClaw Browser Relay:
  - confirm `openclaw` CLI is in PATH: `which openclaw`
  - test manually: `openclaw browser --browser-profile openclaw start`
  - test page open: `openclaw browser --browser-profile openclaw open "https://example.com"`
  - test extract: `openclaw browser --browser-profile openclaw evaluate --fn "() => document.body.innerText" --json`
  - check logs for `OpenClaw cmd failed` warnings
- For legacy mode:
  - confirm `OPENCLAW_SCRAPE_CMD` set
  - run command manually with sample URL

### Action cards missing

- Confirm `markets` table populated.
- Confirm `market_snapshots` table receiving rows.
- Check scoring loop interval and exceptions.
- If `FOCUS_EVENT_MARKETS=1`, confirm `config/events.yaml` has upcoming scheduled events.
  - No scheduled/live events means scorer intentionally suppresses generic NO_EVENT cards.

### Dashboard shows no data

- Confirm `make run-live` (or `make run`) has been run at least once to populate `action_cards` table.
- Check that `data/edge.db` exists and has rows: `sqlite3 data/edge.db "SELECT COUNT(*) FROM action_cards"`
- If port 8777 is busy: `lsof -ti:8777 | xargs kill -9`

### Polymarket data not showing

- Run `make fetch-poly` to refresh `data/polymarket_prices.json`.
- Check that the file exists and has entries: `python3 -c "import json; d=json.load(open('data/polymarket_prices.json')); print(d.get('total_markets', 0), 'markets')"`
- After refreshing Poly data, re-run `make run-live` to regenerate action cards with `poly_yes` field.

### Wallet-flow data not showing

- Run `make fetch-wallet` to refresh `data/wallet_signals.json`.
- Verify cache has rows: `python3 -c "import json; d=json.load(open('data/wallet_signals.json')); print(d.get('signal_count', 0))"`
- Re-run scorer (`make run-live`) so action cards include wallet signal metadata.
- If coverage is sparse, wallet trade flow may simply be low in recent public trade windows.

### High cost / over-polling

- Increase transcript polling interval.
- Enable stronger dedupe/caching.
- Limit OpenClaw usage to fallback cases only.

## Restart and Shutdown

- Graceful shutdown:
  - `Ctrl+C` sends `SIGINT`
  - runner stops loops and closes DB
- Restart by rerunning launch command.
- WAL mode allows safe frequent writes for 24/7 operation.

## On-Call Safety Rules

- Do not add any order placement integration in ops scripts.
- Treat all outputs as decision support, not execution directives.
- Preserve raw logs for postmortems and model-quality audits.
