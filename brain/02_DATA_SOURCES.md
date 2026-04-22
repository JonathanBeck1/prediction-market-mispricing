# Data Sources

Manual-only invariant: data sources inform Action Cards only; no execution calls are allowed.

## Source Categories

### 1) Kalshi Market Data

- Live: `LiveKalshiWatcher` polls Kalshi public `GET /markets` API per series, no auth required.
- Mock: `MockKalshiWatcher` generates deterministic snapshots for testing.
- Required snapshot fields: `market_id`, `yes_bid`, `yes_ask`, `no_bid`, `no_ask`, `spread`, `depth_yes`, `depth_no`, `volume_1h`, `ts`.
- Stored in: `market_snapshots` table + `data/raw/kalshi_snapshots.jsonl`.
- Snapshots pruned weekly (keep last 3 days) via `scripts/prune_snapshots.py` + launchd.
- Coverage model: a static seed list is used only to bootstrap discovery; the fetcher and watcher now dynamically admit additional mention-like speaker series discovered from Kalshi.
- Important Kalshi behavior: some series are true single-event contracts, while others are rolling window contracts. Example: `KXSECPRESSMENTION` is often a monthly/windowed Leavitt press-briefing series, so a real briefing can be represented by `...-26MAR31` rather than a same-day `...-26MAR10` event.

### 2) Transcript/Captions Data (Primary Alpha Input)

Priority corpus speakers today: Donald Trump, Karoline Leavitt, Zohran Mamdani.
Market coverage is broader than transcript coverage: markets without transcript support are still displayed and scored using deterministic priors plus market/signal inputs.

Two ingestion modes:

**Mode A: Historical corpus (post-event)**
Transcripts are added after events end to improve base rate calibration.
- **Automated pipeline (default):** `MaintenanceRunner` calls `scripts/auto_ingest_corpus.py` once per day. It discovers new Rev.com transcript URLs, extracts speaker text via `rev_transcript_cleaner.py`, writes corpus files, and runs `ingest_corpus.py` to import into the DB — all without manual intervention.
- **Manual override:** run `scripts/rev_transcript_cleaner.py` directly with a specific URL (see quick reference below).
- This is the statistics layer — it gets better over time.

#### Automated pipeline — how it works

```
MaintenanceRunner (daily, 86400s)
  └─ auto_ingest_corpus.py
       ├─ discover_rev_transcripts.py   ← finds new Rev URLs
       │    ├─ BraveAPI  (if BRAVE_API_KEY set — most precise)
       │    ├─ Rev RSS   (https://www.rev.com/blog/category/transcripts/feed/)
       │    └─ Rev HTML  (https://www.rev.com/blog/?s=trump)
       ├─ rev_transcript_cleaner.py     ← cleans each new URL
       └─ ingest_corpus.py              ← imports into DB + phrase hits
```

Ingestion ledger (`data/corpus/.rev_ingested.json`) tracks every URL: `ok`, `skipped` (no target speaker text), or `error` (HTTP/parse failure). Already-processed URLs are never re-fetched.

**Enable Brave Search** (strongly recommended) by setting `BRAVE_API_KEY` in `config/runtime.env` — free tier gives 2,000 queries/month, which is more than enough. Without it the pipeline falls back to Rev RSS + HTML scrape.

```bash
# Run the pipeline immediately (instead of waiting for the daily schedule):
python3 scripts/auto_ingest_corpus.py --verbose

# Dry-run — discover what's new without writing any files:
python3 scripts/auto_ingest_corpus.py --dry-run

# Force-ingest one specific URL (bypasses ledger):
python3 scripts/auto_ingest_corpus.py \
    --force-url "https://www.rev.com/blog/transcripts/trump-rally-03-24-26"

# See what URLs would be discovered right now:
python3 scripts/discover_rev_transcripts.py --verbose

# See ALL known Rev URLs (including already-ingested):
python3 scripts/discover_rev_transcripts.py --all
```

#### Rev transcript ingestion — quick reference

```bash
# Fetch a live Rev URL (date + event_type auto-inferred from URL slug and page title):
python3 scripts/rev_transcript_cleaner.py \
    "https://www.rev.com/transcripts/kennedy-center-luncheon-3-24-26" \
    --corpus-speaker trump \
    --speaker "Donald Trump"

# Supply date explicitly when it can't be inferred:
python3 scripts/rev_transcript_cleaner.py \
    "https://www.rev.com/transcripts/executive-orders-3-16-26" \
    --corpus-speaker trump \
    --speaker "Donald Trump" \
    --date 2026-03-16

# Parse a manually-saved HTML file (useful when Rev blocks direct fetches):
python3 scripts/rev_transcript_cleaner.py \
    --html-file ./saved_page.html \
    --corpus-speaker trump \
    --speaker "Donald Trump" \
    --date 2026-03-15 \
    --event-type general

# Print to stdout instead of writing a file (useful for spot-checks):
python3 scripts/rev_transcript_cleaner.py \
    "https://www.rev.com/transcripts/some-speech" \
    --corpus-speaker trump \
    --stdout

# Batch mode — one URL per line in urls.txt:
python3 scripts/rev_transcript_cleaner.py \
    --urls-file urls.txt \
    --corpus-speaker trump
```

Notes:
- Default speaker labels (when `--speaker` is omitted): "donald trump", "president trump", "president donald trump", "trump" (case-insensitive match).
- The script **never overwrites** existing files; it auto-increments the `_seq` suffix.
- If Rev serves a JS-rendered page that has no transcript text, the script prints a clear error and suggests saving the page manually with `File > Save As > Webpage, Complete`.
- Event types resolved from URL slug: `rally`, `signing`, `briefing`, `interview`, `address`, `presser`, `townhall`, `summit`, `announcement`, `visit`, `general`.
- Tests + fixtures: `tests/test_rev_transcript_cleaner.py`, `tests/fixtures/rev_sample_inline.html`, `tests/fixtures/rev_sample_structured.html`.

**Mode B: Live ingestion (during-event)**
Real-time caption pages are polled during events for instant phrase detection.
- Set `TRANSCRIPT_URLS` to a live caption URL before the event
- Ingestor polls every 30 seconds, diffs new text, fires phrase hits
- Sources: REV live captioning, YouTube auto-captions (via OpenClaw), WH.gov live feed, C-SPAN
- Fallback ladder: `DirectHTTPTranscriptSource` → `OpenClawTranscriptSource`

### Phrase co-occurrence file (single writer)

- **Canonical output:** `data/phrase_cooccurrence.json` in the **`pairs` array** format produced by **`scripts/compute_cooccurrence.py`** (recency-weighted counts, consumed by `CooccurrenceCache` / live settlement boosts).
- **`scripts/calibrate_base_rates.py`** must **not** overwrite this file (it previously emitted a legacy nested-dict shape). After changing outcomes or corpus, run `compute_cooccurrence.py` manually or rely on the daily **MaintenanceRunner** task (only one `compute_cooccurrence` task is registered).

Features implemented (both modes):
- Content-hash dedup (SHA-256 prefix, skip identical fetches)
- Incremental diffing (only phrase-match new text in growing streams)
- Stored in: `transcripts` table + `data/raw/transcripts.jsonl`

Corpus file naming convention:
- path: `data/corpus/<speaker>/<event_type>_<YYYY-MM-DD>_<seq>.txt`
- current corpus speakers: `trump`, `leavitt`, `mamdani` (exact spelling)

### 3) Polymarket Cross-Market Data

- Source: Polymarket Gamma API (`gamma-api.polymarket.com/events`), public, no auth
- `scripts/fetch_polymarket.py` discovers active mention market slugs, extracts YES prices per phrase
- Cross-matches with Kalshi markets by phrase + speaker + timeframe
- Output: `data/polymarket_prices.json` — loaded by `app/polymarket.py` at startup
- Refreshed via `make fetch-poly` before trading sessions; also runs on 15-min interval via `MaintenanceRunner`
- Scoring engine blends: `p_blended = 0.7 * model_p + 0.3 * poly_yes` (when confidence and phrase match)
- **Pool signal fallback (session 26):** `PolymarketPrices.get_pool_signal()` provides a weak anchor (confidence=0.30) from the weighted average of all same-speaker same-timeframe markets when no exact phrase match exists. Reason code: `POLY_POOL`.
- **Slug discovery improvement (session 26):** Gamma API now paginated (offset 0/100/200/300), title-search endpoint tried per speaker query. Fixed loop bug that was sending the same URL 12× instead of different speaker queries.

### 4) Live Settlement Signals (Real-Time)

- Source: Kalshi public API, `status=settled&limit=100` per series
- `scripts/fetch_settlements.py` fetches recently settled (last 12h) mention-market outcomes
- Output: `data/live_settlements.json` — loaded by `app/live_settlements.py` at scorer startup
- Runs every 3 minutes via `MaintenanceRunner`
- **Signal:** When any market in the same event_ticker settled YES/NO in the last 6 hours:
  - `SETTLED_YES`: exact phrase confirmed — p_literal clamped to 0.90
  - `SETTLED_NO`: exact phrase confirmed not said — p_literal clamped to 0.10
  - `EVENT_ACTIVE`: any settlement in event → all open markets get +0.04 boost
- Series monitored: KXTRUMPSAY, KXTRUMPSAYEP, KXTRUMPMENTION, KXTRUMPMENTIONB, KXPRESMENTION, KXLEAVITTMENTION, KXSECPRESSMENTION, KXMAMDANIMENTION

### 5) Truth Social Posts — Trump Only

- **Speaker:** Trump only — this is the highest-signal pre-speech source for Trump markets
- **How it works:** Trump posts on Truth Social 2-6h before speeches, often using the EXACT phrases he'll say. "DRILL BABY DRILL! America First!" at 1pm = near-certainty for those phrases at 4pm.
- **Input file:** `data/truth_social_posts.json` — written by OpenClaw
- **Consumer:** `scripts/process_truth_social.py` — runs every 15 min via MaintenanceRunner
- **Output:** Updates `data/signals.yaml` with time-decayed `news_pressure` boosts
- **Manual add:** `scripts/add_truth_social_post.py "content" [--posted-at ISO8601]`
  — supply `--posted-at` with the real Truth Social publish time for accurate decay;
  defaults to ingestion time (now) if omitted. Posts are retained for 7 days.

**OpenClaw must write to `data/truth_social_posts.json` with this schema:**
```json
{
  "fetched_at": "2026-03-12T14:30:00Z",
  "posts": [
    {
      "id": "123456789",
      "content": "We are going to DRILL BABY DRILL! America First!",
      "posted_at": "2026-03-12T12:30:00Z",
      "url": "https://truthsocial.com/@realDonaldTrump/posts/123456789"
    }
  ]
}
```

**Boost multipliers (news_pressure):**
- Post < 2h old: up to **1.50x** (pre-speech signal — maximum boost)
- Post < 6h old: up to **1.35x** (event day)
- Post < 12h old: up to **1.20x** (warm)
- Post < 24h old: up to **1.10x** (mild)
- ALL CAPS words get an extra **+0.10x** (Trump's emphasis marker)

**Corpus auto-ingestion (added session 26):** New transcripts dropped in `data/corpus/` are automatically picked up by `scripts/ingest_corpus.py` which now runs hourly via `MaintenanceRunner`. No manual `make ingest-corpus` needed after this. Daily `calibrate_base_rates.py` then updates `config/base_rates.yaml` with the new data.

### 6) Price Velocity / Smart Money

- Source: `market_snapshots` table in `data/edge.db` (snapshots written every ~90s by runner)
- `scripts/compute_price_velocity.py` computes `delta_2h`, `delta_6h`, `delta_24h` per open market
- Only scores currently open, non-expired markets (cross-referenced with `data/kalshi_markets.json`)
- Quality gates: data must be within 2× of requested window; `SMART_MONEY_DOWN` requires past price ≥ 0.50
- Output: `data/price_velocity.json` — loaded by `app/price_velocity.py` at startup and every 15 min
- Refreshed every 5 minutes via `MaintenanceRunner` (`compute_price_velocity` task)
- Stale data (> 10 min old) is ignored — `PriceVelocityCache` returns empty if JSON is too old
- **Signal:** `SMART_MONEY_UP` (price rising fast → informed buying) / `SMART_MONEY_DOWN` (price falling → informed selling or event cancelled)
- p_literal adjustment: ±0.025 – ±0.05 scaled by signal_strength; applied after source agreement
- DB index: `idx_ms_market_ts` on `(market_id, ts)` makes per-market lookups instant (~1ms each)
- Signal improves with continuous runner uptime — most useful after 7+ days of uninterrupted operation

### 8) External Signals — Current (Automated RSS + Manual)

File: `data/signals.yaml`

Two multipliers per phrase applied to base rates:
- `news_pressure` (0.7–1.5): is this topic in the news cycle?
- `x_buzz` (0.7–1.5): is this phrase trending on X?

Currently edited manually. Being replaced by LLM signal intelligence layer (Phase 5).

### 9) External Signals — Planned (LLM Intelligence Layer)

The next build replaces manual signal editing with automated contextual reasoning:

**Data sources to ingest:**

| Source | Method | Cost | Value |
|---|---|---|---|
| Trump Truth Social | RSS feed scrape | Free | Very High — primary signal for Trump markets |
| White House official RSS | RSS (`whitehouse.gov/feed/`) | Free | High — agenda previews, scheduled topics |
| NewsAPI headlines | REST API (free tier, 100 req/day) | Free | High — AP, Reuters, NYT coverage |
| Google Trends | `pytrends` library | Free | Medium — real-time search volume spikes |
| X key accounts | X API Basic or OpenClaw scrape | $0-100/mo | High — @PressSec, @WhiteHouse, reporters |
| C-SPAN schedule | RSS | Free | Medium — event titles predict phrase content |

**Key signal accounts:**

| Account | Platform | Why |
|---|---|---|
| @realDonaldTrump | Truth Social | Pre-speech posts repeat exact phrases he'll say live |
| @PressSec (Leavitt) | X | Previews briefing topics |
| @WhiteHouse | X | Official agenda, announces topics 24h ahead |
| @maggieNYT, @jonkarl | X | Senior reporters telegraph what's coming |

**LLM reasoning layer:**
- Aggregated context (posts, headlines, trends) + phrase universe → GPT-4o-mini
- LLM reasons about which phrases are likely to appear and outputs probability adjustments
- Output overwrites `data/signals.yaml` — scoring engine picks it up unchanged
- This is **reasoning, not statistics**: "Trump posted about lumber tariffs 2h ago → p(tariff) = 0.96"
- Cost: ~$1-2/month total

**Important distinction:**
- The stats model (base rates, time decay) answers: "historically, how often?"
- The LLM layer answers: "given what's happening RIGHT NOW, how likely?"
- Both feed into the same scoring formula — they're complementary, not competing

### 10) Signals YAML Format

File: `data/signals.yaml`

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
```

Default values if phrase not in file: `news_pressure = 1.0`, `x_buzz = 1.0` (neutral).

## Invocation Policy (Cost Control)

- Poll by event relevance, not high-frequency global scraping.
- X/news signals are modifiers, not blocking inputs. System works without them.
- All external signal sources are optional and gracefully default to neutral.
- LLM calls are cheap (~$0.001/call) but should be event-driven, not continuous.
- Free-tier sources (Truth Social RSS, WH RSS, NewsAPI, Google Trends) cover ~80% of signal value.
- X API paid tier ($100/mo) deferred until consistently profitable.

## Caching and Dedupe

- Transcript cache key: `source_ref + normalized_text_hash`.
- Skip duplicate inserts at both in-memory and DB levels.
- Signals YAML is read at scorer startup and periodically refreshed.
- Polymarket prices loaded from JSON cache at startup (refreshed via `make fetch-poly`).
- Maintain fetch metadata: `attempt_count`, `fallback_stage`, `last_success_ts`, `last_error`.

## Reliability Rules

- Never crash loop due to one source failure.
- Log source failures with stage and reason.
- Preserve raw ingest metadata for audit.
- Prefer stale-but-known data over hard stop.
- Missing signals default to neutral (1.0); never block scoring.
