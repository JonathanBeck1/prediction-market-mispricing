# Config Specification

Manual-only invariant: no config key can enable order placement.

## Precedence

1. Environment variables (highest)
2. YAML config files
3. Built-in defaults (lowest)

## Config Files

| File | Purpose | Format |
|---|---|---|
| `config/edge.yaml` | Runtime settings, intervals, source config | YAML |
| `config/runtime.env` | Runtime env overrides for local/launchd operation | dotenv |
| `config/base_rates.yaml` | Historical phrase probabilities by speaker + event type | YAML |
| `config/events.yaml` | Scheduled events (manual entry for v1) | YAML |
| `config/x_watchlist.yaml` | Tracked X accounts + priorities for deterministic x_buzz ingestion | YAML |
| `data/signals.yaml` | External signal modifiers: news_pressure, x_buzz | YAML |
| `data/kalshi_outcomes.json` | Finalized historical outcome cache for mention markets | JSON |
| `data/x_state.json` | X pipeline `since_id` and account state | JSON |
| `data/x_usage.json` | X pipeline month/day usage and spend telemetry | JSON |
| `data/wallet_reputation.json` | Wallet profitability scores from Polymarket `/closed-positions` | JSON |

## base_rates.yaml (Speaker + Event Type + Phrase -> Probability)

Primary maintenance path:
- Generated from corpus via `python3 scripts/calibrate_base_rates.py` (or `make calibrate`)
- Optional manual edits are allowed for emergency overrides, but regeneration is preferred
- Script safeguards:
  - minimum docs per event type block: `--min-event-docs` (default `3`)
  - minimum docs for speaker `_default`: `--min-speaker-docs` (default `5`)
  - Bayesian smoothing toward `_global_default` with `--prior-strength` (default `2.0`)

```yaml
trump:
  rally:
    tariff: 0.85
    nato: 0.40
    border: 0.90
    federal reserve: 0.30
    the fed: 0.30
  briefing:
    tariff: 0.60
    nato: 0.25
    border: 0.50
    federal reserve: 0.40
  interview:
    tariff: 0.50
    nato: 0.20
    border: 0.40
    federal reserve: 0.25
  _default:
    tariff: 0.45
    nato: 0.25
    border: 0.45
    federal reserve: 0.25

leavitt:
  briefing:
    press briefing: 0.95
    immigration: 0.80
    economy: 0.70
    energy: 0.35
  _default:
    press briefing: 0.60
    immigration: 0.50
    economy: 0.45
    energy: 0.25

mamdani:
  rally:
    housing: 0.85
    rent freeze: 0.60
    public transit: 0.50
    union: 0.40
  townhall:
    housing: 0.75
    rent freeze: 0.50
    transit: 0.60
    union: 0.45
  _default:
    housing: 0.55
    rent freeze: 0.35
    transit: 0.35
    union: 0.30

_global_default: 0.30
```

Lookup order:
1. `speaker.event_type.phrase` (exact match)
2. `speaker._default.phrase` (speaker default for any event type)
3. `_global_default` (0.30)

Calibration workflow:
```bash
make analyze
make fetch-outcomes
make calibrate
```

## events.yaml (Scheduled Events -- Manual for v1)

```yaml
events:
  - event_id: "trump:2026-03-05:rally-01"
    speaker: trump
    event_type: rally
    scheduled_start_ts: "2026-03-05T19:00:00-05:00"
    expected_duration_sec: 5400
    notes: "Campaign rally in Iowa"

  - event_id: "leavitt:2026-03-06:briefing-01"
    speaker: leavitt
    event_type: briefing
    scheduled_start_ts: "2026-03-06T14:00:00-05:00"
    expected_duration_sec: 2700
    notes: "Daily White House press briefing"
```

Events are loaded at startup and can be reloaded without restart.

## signals.yaml (External Signal Modifiers)

```yaml
signals:
  - phrase: "tariff"
    news_pressure: 1.2
    x_buzz: 1.4
    updated_at: "2026-03-05T14:00:00Z"
  - phrase: "nato"
    news_pressure: 0.9
    x_buzz: 1.0
    updated_at: "2026-03-05T14:00:00Z"
  - phrase: "border"
    news_pressure: 1.1
    x_buzz: 1.1
    updated_at: "2026-03-05T14:00:00Z"
  - phrase: "housing"
    news_pressure: 1.0
    x_buzz: 0.9
    updated_at: "2026-03-05T14:00:00Z"
```

Defaults if phrase not found: `news_pressure = 1.0`, `x_buzz = 1.0`.
Modifier ranges: `[0.7, 1.5]` (clamped by scoring engine).

For local testing: edit this file manually.
For production: OpenClaw scrapes X/news and overwrites this file periodically.

## edge.yaml (Runtime Config -- Canonical Target)

```yaml
runtime:
  mode: manual_only
  data_dir: data
  db_path: data/edge.db
  log_level: INFO
  watcher_interval_sec: 10
  transcript_interval_sec: 30
  scorer_interval_sec: 10

kalshi:
  source_mode: mock
  mock_enabled: true
  market_universe:
    speakers: [trump, leavitt, mamdani]

transcripts:
  source: fallback
  urls: []
  http_timeout_sec: 15
  openclaw:
    enabled: false
    scrape_cmd_template: ""
    timeout_sec: 20
  fallback_order: [directhttp, openclaw]
  dedupe:
    enabled: true
    hash_algo: sha256

scoring:
  liquidity:
    max_spread: 0.03
    min_depth_yes: 250
  ev_threshold: 0.03
  decay:
    exponent: 2
    min_factor: 0.05
  ranking:
    top_n: 5

alerts:
  channels: [console, jsonl, sqlite]
  throttle:
    enabled: true
    cooldown_sec: 120
    material_ev_delta: 0.03

events:
  pre_event_window_sec: 3600
  live_freshness_sec: 90
  ended_inactive_sec: 600
```

## Environment Variable Mapping (Current)

| Env Var | Config Path | Default |
|---|---|---|
| `KALSHI_MOCK` | `kalshi.mock_enabled` | `1` |
| `DATA_DIR` | `runtime.data_dir` | `data` |
| `DB_PATH` | `runtime.db_path` | `data/edge.db` |
| `ACTION_CARDS_PATH` | action card output path | `data/action_cards.jsonl` |
| `WATCHER_INTERVAL_SEC` | `runtime.watcher_interval_sec` | `10` |
| `TRANSCRIPT_INTERVAL_SEC` | `runtime.transcript_interval_sec` | `30` |
| `SCORER_INTERVAL_SEC` | `runtime.scorer_interval_sec` | `10` |
| `TRANSCRIPT_SOURCE` | `transcripts.source` | `directhttp` |
| `TRANSCRIPT_URLS` | `transcripts.urls` | (empty) |
| `TRANSCRIPT_HTTP_TIMEOUT_SEC` | `transcripts.http_timeout_sec` | `15` |
| `OPENCLAW_SCRAPE_CMD` | `transcripts.openclaw.scrape_cmd_template` | (empty, legacy mode) |
| `OPENCLAW_TIMEOUT_SEC` | `transcripts.openclaw.timeout_sec` | `30` |
| `OPENCLAW_BROWSER_PROFILE` | `transcripts.openclaw.browser_profile` | `openclaw` |
| `WHATSAPP_ENABLED` | `alerts.whatsapp.enabled` | `0` |
| `WHATSAPP_TARGET` | `alerts.whatsapp.target` | (empty) |
| `FOCUS_EVENT_MARKETS` | `events.focus_event_markets` | `1` in live, `0` in mock |
| `PRE_EVENT_WINDOW_SEC` | `events.pre_event_window_sec` | `21600` |
| `MAX_SPREAD` | `scoring.liquidity.max_spread` | `0.15` |
| `MIN_DEPTH` | `scoring.liquidity.min_depth_yes` | `0` |
| `EV_THRESHOLD` | `scoring.ev_threshold` | `0.03` |
| `MARKET_ANCHOR_WEIGHT` | `scoring.market_anchor.weight` | `0.35` |
| `MARKET_ANCHOR_THRESHOLD` | `scoring.market_anchor.threshold` | `0.30` |
| `PRE_EVENT_YES_THRESHOLD` | `scoring.guardrails.pre_event_yes_threshold` | `0.03` |
| `BLOCK_OFF_TOPIC_YES` | `scoring.guardrails.block_off_topic_yes` | `0` |
| `PENNY_PRICE_THRESHOLD` | `scoring.guardrails.penny_price_threshold` | `0.00` |
| `MAINTENANCE_ENABLED` | `runtime.maintenance.enabled` | `1` in live mode, `0` in mock |
| `MAINTENANCE_INTERVAL_SEC` | `runtime.maintenance.loop_interval_sec` | `60` |
| `EVENT_SEED_INTERVAL_SEC` | `events.auto_seed.sync_interval_sec` | `300` |
| `FETCH_MARKETS_INTERVAL_SEC` | `runtime.maintenance.fetch_markets_interval_sec` | `3600` |
| `FETCH_POLY_INTERVAL_SEC` | `runtime.maintenance.fetch_poly_interval_sec` | `1800` |
| `FETCH_WALLET_FLOW_INTERVAL_SEC` | `runtime.maintenance.fetch_wallet_flow_interval_sec` | `1800` |
| `FETCH_X_INTERVAL_SEC` | `runtime.maintenance.fetch_x_interval_sec` | `1800` |
| `FETCH_OUTCOMES_INTERVAL_SEC` | `runtime.maintenance.fetch_outcomes_interval_sec` | `21600` |
| `RECORD_OUTCOMES_INTERVAL_SEC` | `runtime.maintenance.record_outcomes_interval_sec` | `21600` |
| `POLY_MIN_CONFIDENCE` | `scoring.poly.min_confidence` | `0.35` |
| `WALLET_FLOW_WEIGHT` | `scoring.wallet_flow.weight` | `0.12` |
| `WALLET_MIN_CONFIDENCE` | `scoring.wallet_flow.min_confidence` | `0.35` |
| `WATCHDOG_ENABLED` | `runtime.watchdog.enabled` | `1` in live mode, `0` in mock |
| `WATCHDOG_INTERVAL_SEC` | `runtime.watchdog.loop_interval_sec` | `60` |
| `WATCHDOG_MAX_SNAPSHOT_AGE_SEC` | `runtime.watchdog.max_snapshot_age_sec` | `300` |
| `WATCHDOG_MAX_SCORER_IDLE_SEC` | `runtime.watchdog.max_scorer_idle_sec` | `300` |
| `WATCHDOG_STARTUP_GRACE_SEC` | `runtime.watchdog.startup_grace_sec` | `180` |
| `WATCHDOG_BREACH_LIMIT` | `runtime.watchdog.breach_limit` | `3` |
| `WATCHDOG_EXIT_ON_STALE` | `runtime.watchdog.exit_on_stale` | `1` |
| `X_BEARER_TOKEN` | `x.api.bearer_token` | (empty) |
| `X_MONTHLY_BUDGET_USD` | `x.budget.monthly_usd` | `25` |
| `X_DAILY_POST_RESOURCE_CAP` | `x.budget.daily_post_resources` | `300` |
