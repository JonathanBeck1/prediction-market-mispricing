# Build Plan V3 — Codebase Audit-Driven Improvement Roadmap

> Created: 2026-03-25 (Session 40)
> Supersedes `BUILD_PLAN_V2.md`. Based on systematic codebase interrogation across
> `app/scoring.py`, `app/phrase_matcher.py`, `app/calibration.py`, `app/base_rates.py`,
> `app/event_signals.py`, `app/rolling_rates.py`, `app/live_settlements.py`,
> `scripts/backtest_outcomes.py`, `scripts/fetch_signals.py`, `scripts/analyze_signals.py`,
> `scripts/compute_cooccurrence.py`, and `scripts/compute_price_velocity.py`.
>
> **Manual-only invariant preserved throughout. No execution automation.**

---

## Full Audit Findings — Exact Code References

### CRITICAL — Actively Causing Incorrect Bets

| ID | Issue | Severity | File | Key Line(s) |
|---|---|---|---|---|
| C1 | Signal double-counting (news/llm/wh/event_llm all fire on same headline) | Critical | `app/scoring.py:508` | `base * topic_rel * news * buzz * llm * event_llm + wh_keyword_boost` |
| C2 | Rolling window uses time-fraction not event-count for compound math | Critical | `app/scoring.py:364` | `p = 1-(1-p_full)**frac_remaining` — uses days, not N events |
| C3 | No walk-forward backtesting — lookahead bias in base rates | Critical | `scripts/backtest_outcomes.py:91` | LOO across full dataset, no temporal split |
| C4 | BSS vs market mid never computed — can't prove we have edge | Critical | `scripts/backtest_outcomes.py:145` | Brier only, no BSS, no market-mid baseline |
| C5 | No negation detection in phrase matcher | Critical | `app/phrase_matcher.py:20` | Regex match only, "will NOT say Iran" fires YES |

### HIGH — Causing Systematic Errors

| ID | Issue | Severity | File | Key Line(s) |
|---|---|---|---|---|
| H1 | No Unicode normalization before matching | High | `app/phrase_matcher.py:20` | No `unicodedata.normalize()` call anywhere |
| H2 | News dedup is exact-title only — same story counts N times | High | `scripts/fetch_signals.py:104` | `if title in seen:` — no MinHash/entity dedup |
| H3 | LLM runs on NBA + earnings — no domain skip | High | `scripts/analyze_event.py:271` | `"earnings": "earnings"` — LLM still called |
| H4 | Calibration not stratified by speaker — Trump/Leavitt same coefficients | High | `app/calibration.py:255` | Only `pre_event` / `live` strata, not by speaker |
| H5 | WH keyword boost double-counts news (additive after multiplicative) | High | `app/scoring.py:508` | `+ wh_keyword_boost` added after all multipliers |
| H6 | `signals.yaml` has no `source_event` field — can't audit double-counting | High | `data/signals.yaml` | Schema missing `causal_story`, `source_url` |

### MEDIUM — Degrading Accuracy

| ID | Issue | Severity | File | Key Line(s) |
|---|---|---|---|---|
| M1 | Global default base rate is 0.30 (code) — may be wrong after session 25 fix | Medium | `app/base_rates.py:9` | `_GLOBAL_DEFAULT = 0.30` |
| M2 | Cooccurrence has no time decay — 2022 patterns weighted same as last week | Medium | `scripts/compute_cooccurrence.py:41` | `MIN_N=5, MIN_RATE=0.50` — no recency weight |
| M3 | Dashboard shows no BSS, Brier trend, or win-rate over time | Medium | `app/dashboard.py` | System tab has engine status only |
| M4 | Polymarket comparison is price gap only — resolution rule differences ignored | Medium | `app/scoring.py:990` | `POLY_DIVERGE` on 15¢ gap, no rule check |
| M5 | Corpus has no timestamps — survival model impossible without rework | Medium | `data/corpus/leavitt/` | Plain prose, no word-position markers |

---

## Priority Tiers

---

## 🔴 P0 — Fix First (Causing Real Money Losses)

---

### P0-1: BSS vs Market Mid (Measure Edge Before Anything Else)

**Why P0:** This is the foundational question. The entire system has been built and tuned without ever answering: "do we beat just using the Kalshi market price?" From `scripts/backtest_outcomes.py:145`, the current metric is plain Brier. From `outcome_reviews` in `app/db.py`, `yes_ask` and `no_ask` are stored at decision time — everything needed is already there.

**Exact gap:** `backtest_outcomes.py` computes:
```python
brier_sum += (model_p - int(actual_yes)) ** 2
```
It never computes `brier_market = (yes_ask - int(actual_yes)) ** 2` to compare. The market-mid baseline is **already in the DB** in the `yes_ask` column of `outcome_reviews`.

**Fix — 3 files, ~4 hours:**

1. **`scripts/backtest_outcomes.py`** — add three baselines alongside model Brier:
   - `BS_model`: current metric
   - `BS_market_mid`: `(yes_ask - actual_yes)^2` using stored `yes_ask`
   - `BS_base_rate`: `(historical_rate - actual_yes)^2`
   - `BSS_vs_market = 1 - BS_model / BS_market_mid` — this is the key number
   - Walk-forward loop: sort outcomes by `resolved_ts`, train base rates only on
     outcomes `resolved_ts < test_event_date`, evaluate on remaining

2. **`scripts/post_event_summary.py`** — add `bss_vs_market_mid` to per-event summary output

3. **`app/dashboard.py`** — add to System tab: rolling 30-day BSS vs market mid with
   a clear visual (green = we're beating market, red = we're not)

**Success condition:** BSS > 0.05 for BUY_YES bets, BSS > 0.02 for BUY_NO bets.
If BSS < 0, the model has negative value and needs structural changes before any other P0 work.

---

### P0-2: Signal Double-Counting Mitigation

**Why P0:** The exact scoring formula in `app/scoring.py:508`:
```python
p = _clamp_p(base * topic_rel * news * buzz * llm * event_llm + wh_keyword_boost)
```
When Iran is in the news, ALL of these fire simultaneously on the same Reuters wire story:
- `news` → `news_pressure` boosted in `signals.yaml` via `fetch_signals.py`
- `llm` → `llm_boost` boosted by `analyze_signals.py` reading same news
- `event_llm` → `event_llm` boosted by `analyze_event.py` reading same news
- `wh_keyword_boost` → fires if phrase appears in WH schedule titles from same news

This is 4× counting a single piece of evidence. There is no deduplication layer and `signals.yaml` has no `source_event` field to trace causality.

**Fix — 3 files, ~1 day:**

1. **`data/signals.yaml` schema change** — add `source_story_hash` and `source_ts` per signal:
   ```yaml
   - phrase: Iran
     news_pressure: 1.18
     llm_boost: 1.22
     source_story_hash: "abc123"   # NEW: sha256[:8] of title+date
     source_ts: "2026-03-25T14:30Z"
     llm_boost_source_hash: "abc123"   # NEW: same hash if from same story
   ```

2. **`scripts/analyze_signals.py`** — tag each boost with the top-1 source story hash
   that drove it (highest `news_pressure` contributor). Pass to `_update_signals()`.

3. **`app/scoring.py`** — add dampening rule in `_compute_p_literal`:
   - If `news_pressure > 1.05` AND `llm_boost > 1.05` AND they share same `source_story_hash`:
     → apply only the stronger one at full weight, second at 35% weight
   - Add `DOUBLE_COUNT_DAMPENED` reason code when triggered
   - Specific case for `wh_keyword_boost`: skip if `news` signal is already boosted
     by the same headline (check `source_ts` within 4-hour window)

**Expected impact:** Reduces overconfident BUY_YES on hot-news phrases by 15-25%.

---

### P0-3: Rolling Window Uses Time-Fraction, Not Event-Count

**Why P0:** The compound formula in `app/scoring.py:364`:
```python
p = 1.0 - (1.0 - p_full) ** frac_remaining
```
`frac_remaining` is computed from `_remaining_window_fraction()` as calendar days remaining
divided by total window days. This assumes one event per day. For Leavitt briefings
(3-5 per week, Mon-Fri only) a contract with 5 calendar days remaining might have
3 briefings remaining — `p` would be severely underestimated using `frac_remaining=0.17`
when the correct value is `N_remaining=3`.

**Fix — 2 files, ~4 hours:**

1. **`app/window_state.py`** — add `remaining_events_in_window(market_id, now_dt)` method:
   - Load `data/nba_schedule.json` for NBA events
   - For WH briefings: estimate from `data/wh_schedule.json` — count scheduled briefings
     between now and `close_time`
   - Default fallback: `ceil(remaining_calendar_days * events_per_day_by_type)` where
     `events_per_day_by_type = {"briefing": 0.7, "nba_broadcast": 1.3, "rally": 0.15, ...}`

2. **`app/scoring.py`** — in `_compute_window_p_literal`, prefer event-count over time-fraction:
   ```python
   n_remaining = self.window_state.remaining_events_in_window(market_id, now_dt)
   if n_remaining is not None and n_remaining > 0:
       p = 1.0 - (1.0 - p_full) ** n_remaining
       components["window_events_remaining"] = n_remaining
   else:
       # fall back to time fraction
       p = 1.0 - (1.0 - p_full) ** frac_remaining
   ```

---

### P0-4: Walk-Forward Backtesting

**Why P0:** `scripts/backtest_outcomes.py:91` uses a leave-one-out across the full dataset.
This means base rates computed from March 2026 outcomes are used to evaluate January 2025
outcomes. The base rate lookup is trained on future data.

**Fix — 1 file, ~6 hours:**

**`scripts/backtest_outcomes.py`** — full rewrite of evaluation loop:
```python
# Sort all outcomes by resolved_ts ascending
outcomes_sorted = sorted(all_outcomes, key=lambda r: r["resolved_ts"])

# Walk-forward: for each test window, train only on prior outcomes
results = []
TRAIN_MIN = 200  # minimum outcomes before evaluation starts
for i, test_row in enumerate(outcomes_sorted):
    if i < TRAIN_MIN:
        continue
    train_rows = outcomes_sorted[:i]
    # Fit base rates from train_rows only
    train_br = BaseRateLookup.fit_from_rows(train_rows)
    model_p = train_br.get(test_row["speaker"], test_row["event_context"], test_row["phrase"])
    market_p = test_row["yes_ask"]  # stored at decision time
    actual = test_row["outcome"]
    results.append({"model_p": model_p, "market_p": market_p, "actual": actual,
                    "resolved_ts": test_row["resolved_ts"]})

# Compute BSS per quarter
for quarter in group_by_quarter(results):
    bs_model = brier(quarter["model_p"], quarter["actual"])
    bs_market = brier(quarter["market_p"], quarter["actual"])
    bss = 1 - bs_model / bs_market
    print(f"{quarter['label']}: BSS={bss:.3f} (model BS={bs_model:.4f}, mkt BS={bs_market:.4f})")
```

---

## 🟠 P1 — High Impact (Build Next, Each 2-8 Hours)

---

### P1-1: Negation Detection in Phrase Matcher

**Why P1:** The phrase matcher in `app/phrase_matcher.py:20` uses a plain regex with word
boundaries, `re.IGNORECASE`. No window check for negation context. "We will NOT discuss
Iran today" produces the same match as "We just announced the Iran deal." For live caption
monitoring, a false BUY_YES trigger from a negated phrase is money lost.

**Fix — 1 file, ~3 hours:**

**`app/phrase_matcher.py`** — add `MatchResult` dataclass and negation window:
```python
NEGATION_WORDS = frozenset(["not", "never", "without", "no", "won't", "didn't",
                             "doesn't", "cannot", "can't", "refuse", "avoid"])
ATTRIBUTION_WORDS = frozenset(["said", "claimed", "argued", "alleged", "quoted",
                                "according to", "called me", "accused"])
_NEGATION_WINDOW = 8   # words before phrase to scan

@dataclass
class MatchResult:
    phrase: str
    matched: bool
    negated: bool      # True if negation word in window before phrase
    attributed: bool   # True if attribution word precedes phrase or quote marks wrap it

def match_with_context(self, text: str, phrase: str) -> MatchResult:
    pattern = self._compiled[phrase]
    m = pattern.search(text)
    if not m:
        return MatchResult(phrase, False, False, False)
    # Extract window before match
    pre = text[max(0, m.start() - 60): m.start()].lower().split()[-_NEGATION_WINDOW:]
    negated = bool(NEGATION_WORDS & set(pre))
    # Check for open quote or attribution
    attributed = bool(ATTRIBUTION_WORDS & set(pre)) or '"' in text[max(0, m.start()-5): m.start()]
    return MatchResult(phrase, True, negated, attributed)
```

In `app/transcript_ingestor.py` and live caption scoring: when `negated=True`, do NOT
trigger CAPTION_DETECTED boost. Add `NEGATION_CONTEXT` reason code. Annotate Action Card.

---

### P1-2: Unicode Normalization in Phrase Matcher

**Why P1:** ASR transcripts and live captions frequently output Unicode variants:
- Smart quotes U+2018/2019 vs straight apostrophes U+0027
- En-dash U+2013 in compound phrases
- Unicode spaces (non-breaking, thin space)

`phrase_matcher.py` uses `re.escape(phrase)` directly with no normalization. A transcript
with "it's" (smart quote) won't match a phrase "it's" (straight quote).

**Fix — 1 file, ~1 hour:**

**`app/phrase_matcher.py`** — normalize both text and phrase at match time:
```python
import unicodedata

def _normalize(text: str) -> str:
    """NFC normalization + smart quote replacement + whitespace collapsing."""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\u2018", "'").replace("\u2019", "'")  # smart single quotes
    text = text.replace("\u201c", '"').replace("\u201d", '"')  # smart double quotes
    text = text.replace("\u2013", "-").replace("\u2014", "-")  # en/em dash
    text = " ".join(text.split())  # collapse whitespace
    return text
```
Apply `_normalize()` to both `text` in `match()` and each `phrase` in `__init__` before compiling.

Also add compound-word expansion — each phrase should compile 3 variants:
- `health care` → also matches `healthcare` and `health-care`

---

### P1-3: Speaker-Stratified Platt Calibration

**Why P1:** `app/calibration.py:255` splits only on `pre_event` / `live`. Both Trump and
Leavitt markets are pooled into the same stratum. But their distributions are fundamentally
different: Trump rally phrases average ~55% YES, Leavitt briefing phrases average ~18% YES.
Fitting one logistic curve to both systematically over-calibrates Leavitt (pushing her
model probabilities upward toward Trump's distribution center).

**Current strata:**
```python
strata_raw: dict[str, tuple[list[float], list[int]]] = {
    "pre_event": ([], []),
    "live":      ([], []),
}
```

**Fix — 1 file, ~4 hours:**

**`app/calibration.py`** — add speaker-tier strata:
```python
strata_raw = {
    "pre_event_trump":    ([], []),
    "pre_event_non_trump": ([], []),
    "live_trump":         ([], []),
    "live_non_trump":     ([], []),
}
# Assign to stratum based on speaker field in outcome_reviews
speaker = r[3]  # add speaker column to the query
is_trump = (speaker or "").lower() == "trump"
if "PRE_EVENT" in codes:
    key = "pre_event_trump" if is_trump else "pre_event_non_trump"
elif "LIVE" in codes:
    key = "live_trump" if is_trump else "live_non_trump"
```

At scoring time in `app/scoring.py:1143` — pass speaker to `calibrate()`:
```python
is_trump = (speaker or "").lower() == "trump"
if "LIVE" in reason_codes:
    _calib_key = "live_trump" if is_trump else "live_non_trump"
elif "PRE_EVENT" in reason_codes:
    _calib_key = "pre_event_trump" if is_trump else "pre_event_non_trump"
else:
    _calib_key = "default"
```

---

### P1-4: LLM Domain-Selective Skip (NBA + Earnings)

**Why P1:** `scripts/analyze_event.py:271` runs the LLM for every event including NBA
games (`"earnings": "earnings"` + NBA events). NBA announcers are deterministic (arena
sponsors, buzzer sounds) — the LLM adds noise, not signal. Earnings calls are similar.
The LLM prompt for earnings says "suppress ALL political phrases" but doesn't hard-skip.
This wastes API budget and risks injecting political multipliers into NBA markets.

**Fix — 2 files, ~1 hour:**

1. **`app/event_context.py`** — add `LLM_ELIGIBLE_FORMATS` constant:
   ```python
   LLM_ELIGIBLE_FORMATS = frozenset([
       "diplomatic", "rally", "presser", "briefing", "testimony",
       "signing", "address", "interview", "announcement",
   ])
   LLM_SKIP_FORMATS = frozenset([
       "earnings", "nba_broadcast", "sports", "entertainment",
   ])
   ```

2. **`scripts/analyze_event.py`** — hard skip before LLM call:
   ```python
   from app.event_context import LLM_SKIP_FORMATS
   event_fmt = _classify_event(event, event_title)
   if event_fmt in LLM_SKIP_FORMATS:
       logger.info("Skipping LLM for %s (format=%s)", event_title, event_fmt)
       return {}  # rely on deterministic base rates + arena certainties
   ```

**Bonus:** cuts LLM API cost by ~35% (NBA + earnings are high-volume markets).

---

### P1-5: Verify and Fix Global Default Base Rate

**Why P1:** `app/base_rates.py:9` has `_GLOBAL_DEFAULT = 0.30`. Session 25 STATUS.md says
"Fixed global default 0.30 → 0.45" but the code still shows 0.30. Need to reconcile.
For phrases with no history, 0.30 means the system assumes a 30% YES probability before
any signals — which is 3× the actual rate for most political phrases.

**Fix — verify code + YAML, ~30 mins:**

1. Confirm actual value in `app/base_rates.py:9` and `config/base_rates.yaml` `_global_default`
2. Set to match the empirical session 25 analysis:
   - Global default: 0.20 (conservative until data exists)
   - `_context_base` values already set per speaker/context in `config/base_rates.yaml`
     should take precedence over the global fallback
3. Add a test in `tests/` that verifies the fallback chain hits `_context_base` before `_global_default`

---

### P1-6: Cooccurrence Time Decay

**Why P1:** `scripts/compute_cooccurrence.py:41` uses all historical data with no recency
weighting (`MIN_N=5, MIN_RATE=0.50`). A co-occurrence pattern from 2022 is weighted
identically to last month. Trump's rhetoric shifts meaningfully over time — if "tariff →
China" was a 2022 co-occurrence but the current focus is "tariff → Canada", the 2022 data
is misleading the model.

**Fix — 1 file, ~2 hours:**

**`scripts/compute_cooccurrence.py`** — add recency decay to event weights:
```python
COOCCUR_HALFLIFE_DAYS = 120  # 4-month halflife for co-occurrence patterns

def _event_weight(event_date: str, now: datetime) -> float:
    """Exponential decay: events from 4 months ago count at ~50%."""
    try:
        dt = datetime.fromisoformat(event_date)
        days_old = (now - dt).days
        return 2.0 ** (-days_old / COOCCUR_HALFLIFE_DAYS)
    except Exception:
        return 1.0
```
Pass weight into numerator and denominator when computing conditional rates.

---

### P1-7: News Deduplication (Entity-Level)

**Why P1:** `scripts/fetch_signals.py:104` deduplicates only on exact title string.
The same Reuters story gets repackaged by AP, CNN, Fox, NBC, Politico under different
headlines. Each version inflates `news_pressure` independently. On a hot news day, 8
versions of one story could push `news_pressure` to 1.30+ when the true signal is one story.

**Fix — 1 file, ~3 hours:**

**`scripts/fetch_signals.py`** — add entity-tuple deduplication as MinHash substitute
(avoids `datasketch` dependency):
```python
import hashlib

def _entity_fingerprint(title: str) -> str:
    """Extract (entities, verb, date) tuple and hash it for dedup."""
    title_lower = title.lower()
    # Simple: extract capitalized words as entity proxies
    words = title.split()
    entities = sorted(w.lower() for w in words if w[0].isupper() and len(w) > 2)
    key = " ".join(entities[:5])  # top 5 entities as fingerprint
    return hashlib.sha256(key.encode()).hexdigest()[:12]

# In _fetch_google_news():
seen_fingerprints = set()
for entry in feed.entries[:8]:
    title = entry.get("title", "")
    fp = _entity_fingerprint(title)
    if title in seen or fp in seen_fingerprints:
        continue
    seen.add(title)
    seen_fingerprints.add(fp)
```

Also add **per-signal-type news age decay** in `app/scoring.py`:
```python
def _news_age_factor(signal_ts: float | None, now: float) -> float:
    """Breaking news (< 2h): full weight. Older: decay."""
    if signal_ts is None:
        return 1.0
    age_hours = (now - signal_ts) / 3600
    if age_hours < 2:
        return 1.0
    elif age_hours < 12:
        return 0.7  # developing story
    else:
        return 0.4  # background / stale
```

---

### P1-8: Resolution Rule Parsing + Exact Match Audit

**Why P1:** The research is explicit: "stemming reduces 'immigration' to 'immigr,' falsely
matching 'immigrant.' For literal-resolution contracts, the correct approach is exact
case-insensitive string matching after Unicode normalization."

The `phrase_matcher.py` audit confirmed: no stemming (good). But resolution rules are
stored in `market.rules_primary` and never parsed to confirm what string Kalshi actually
checks. "Will Leavitt say 'illegal alien'?" might resolve YES on "illegal aliens" (plural)
or might not. We don't know without parsing the rule.

**Fix — 2 files, ~4 hours:**

1. **`scripts/parse_resolution_rules.py`** (new) — for each market in `kalshi_markets.json`,
   extract the resolution criterion string from `rules_primary` using regex:
   ```python
   # Kalshi rules text: "...market resolves YES if Karoline Leavitt says 'illegal alien'..."
   # or: "...resolves YES if speaker uses the word 'tariff'..."
   LITERAL_PATTERNS = [
       r"says?\s+['\"](.+?)['\"]",
       r"uses?\s+(?:the\s+word|the\s+phrase|the\s+term)\s+['\"](.+?)['\"]",
       r"mentions?\s+['\"](.+?)['\"]",
   ]
   ```
   Store extracted string as `resolution_literal` field in market metadata.

2. **`app/scoring.py`** — add `RESOLUTION_LITERAL_MISMATCH` warning when:
   `phrase.lower() != resolution_literal.lower()` (e.g. market phrase is "illegal aliens"
   but corpus hit tracking records "illegal alien")

---

### P1-9: Polymarket Resolution Scope Detection

**Why P1:** `app/scoring.py:990` fires `POLY_DIVERGE` / `POLY_VETO_NO` on price gaps
without checking whether the contracts are asking the same question. Kalshi often
restricts to named speaker; Polymarket often resolves if anyone on broadcast says it.
A 10¢ gap can be the correct price for two different questions, not mispricing.

**Fix — 2 files, ~3 hours:**

1. **`app/polymarket.py`** — add `resolution_scope` to `PolySignal`:
   ```python
   @dataclass(frozen=True)
   class PolySignal:
       # ... existing fields
       resolution_scope: str = "unknown"  # "named_speaker" | "any_broadcast" | "unknown"
   ```
   Parse from Polymarket condition description text.

2. **`app/scoring.py`** — in POLY_VETO logic, discount divergence when scopes differ:
   ```python
   if poly_signal.resolution_scope != "named_speaker":
       # Poly likely has broader resolution — price gap may be legitimate
       poly_veto_weight = 0.25  # was 1.0
       reason_codes.append("POLY_SCOPE_MISMATCH")
   ```

---

### P1-10: Dashboard Performance Metrics Tab

**Why P1:** The System tab in `app/dashboard.py` shows engine health, calibration coefficients,
and snapshot counts — but zero performance feedback. After a Leavitt briefing, there's
no way to see "are we beating the market?" without running `make backtest` manually.
The `outcome_reviews` table has everything needed.

**Fix — 1 file, ~4 hours:**

**`app/dashboard.py`** — add "Performance" section to System tab using existing data:
```javascript
// New section: Rolling Performance (last 30/60/90 days)
renderPerformanceSection(health) {
    const metrics = health["Perf:"] || {};
    return `
    <div class="perf-grid">
        <div>BSS vs Market Mid (30d): <b>${metrics.bss_30d ?? 'N/A'}</b></div>
        <div>Win Rate BUY_YES (30d): <b>${metrics.wr_yes_30d ?? 'N/A'}</b></div>
        <div>Win Rate BUY_NO (30d): <b>${metrics.wr_no_30d ?? 'N/A'}</b></div>
        <div>P&L (30d): <b>${metrics.pnl_30d ?? 'N/A'}</b></div>
        <div>Brier (30d): <b>${metrics.brier_30d ?? 'N/A'}</b></div>
        <div>Outcomes tracked: <b>${metrics.n_outcomes ?? 'N/A'}</b></div>
    </div>`;
}
```

Backend in `/api/health`: compute rolling metrics from `outcome_reviews` and inject
as `Perf:*` keys.

---

## 🟡 P2 — Medium Impact (Build After P1)

---

### P2-1: Log-Odds Signal Fusion (Replace Raw Multiplicative)

**Current formula** (`app/scoring.py:508`):
```python
p = base * topic_rel * decay * news * buzz * llm * event_llm + wh_keyword_boost
```

**Problem:** Multiplicative compounding at extremes behaves badly. If `base=0.40` and all
multipliers are 1.20, `p` can exceed 1.0 before clamping, and the math implies
independence that doesn't exist. Log-odds aggregation is theoretically optimal when
signals are conditionally independent:

```python
def _fuse_log_odds(base: float, *adjustments: tuple[float, float]) -> float:
    """
    adjustments: (signal_value, weight) pairs where signal_value is a multiplier
    Converts to log-odds space, applies weighted increments, converts back.
    """
    lo = math.log(base / (1.0 - base))
    for signal_mult, weight in adjustments:
        if signal_mult > 0:
            lo += weight * math.log(signal_mult)
    p = 1.0 / (1.0 + math.exp(-lo))
    return max(0.01, min(0.99, p))
```

Benefits: properly bounded, additive increments interpretable, weight reduction for
correlated signals is straightforward (just reduce weight). Pairs naturally with P0-2
double-counting fix.

**Files:** `app/scoring.py` (formula rewrite), `app/base_rates.py` (expose as logit)

---

### P2-2: Survival / Hazard Intra-Event Model

**What it does:** As a speech progresses without a phrase being said, the system currently
keeps outputting the same pre-event probability. A survival model updates in real time:

```
P(mention by end | not mentioned by time t) = 1 - S(T_total) / S(t)
```

After 40 minutes of a 60-minute briefing with "tariff" unsaid, the probability
should DROP, not stay flat at 35%.

**Data needed first:** Corpus needs word-position timestamps (see P2-4). 

**Files:** New `app/survival_model.py`, `scripts/fit_survival_model.py`,
modify `app/scoring.py` for live survival update.

---

### P2-3: Per-Signal Decay Half-Lives

**Current:** `app/scoring.py` has a single `time_decay` factor for live events (speech
elapsed fraction). There's no per-signal-type decay for `news_pressure`, `llm_boost`,
`x_buzz`.

**What the research specifies:**
- Breaking news: half-life ~1.4 hours
- Developing story: half-life ~4 hours
- Background/structural: half-life ~14 hours

**Fix:** Add `updated_at` to `signals.yaml` (already present for some entries). In scoring,
apply age-based decay to `news_pressure` and `llm_boost` using article timestamps.

---

### P2-4: Timestamp Corpus for Survival Model

**Current:** `data/corpus/leavitt/` and `data/corpus/trump/` contain plain prose paragraphs
with no timestamps, no word positions, no timing metadata.

**Fix:** `scripts/timestamp_corpus.py` — for existing transcripts, estimate word positions:
- Average speaking rate: 130 WPM (briefings), 110 WPM (rallies), 160 WPM (Trump)
- Walk each transcript word by word, compute `estimated_minute = word_idx / words_per_min`
- Write annotated corpus format: `data/corpus_timed/<speaker>/<event_type>_<date>.jsonl`
  where each line is `{"phrase": "...", "first_mention_min": 14.3, "context": "..."}`

Minimum target: 30 Leavitt briefings, 50 Trump events.

---

### P2-5: Reliability Diagrams + Calibration Dashboard

**Fix:** `scripts/plot_reliability.py` — bin resolved `outcome_reviews` by predicted
`p_literal` (8 bins: 0-10%, 10-20%, ..., 90-100%), compute actual YES rate per bin.
Output to `data/reliability.json`. Dashboard Calibration tab renders as bar chart.

Alert rule: if any bin's actual rate differs from predicted by >12%, flag as
`CALIBRATION_DRIFT`.

---

### P2-6: Corpus Expansion from Outcome Data

**Gap:** 8,712 resolved outcomes exist but only 129 transcript files. Every resolved YES
outcome IS evidence that the phrase was used at that event type. Currently unused for
per-speaker per-context frequency tables.

**Fix:** `scripts/expand_corpus_from_outcomes.py` — for each resolved YES outcome, add
a synthetic "hit" to the per-speaker per-event-type frequency count in base rate calibration.
Weight at 0.5× vs real transcript hits (less reliable than actual text evidence).

---

### P2-7: Dual-Source ASR Confirmation

**Architecture for live events:**
1. Layer 1: Fast streaming source (WH.gov live or YouTube auto-captions, 10-15s latency)
2. Layer 2: Whisper local confirmation on same audio segment (1-5s after Layer 1)
3. Only fire CAPTION_DETECTED when both agree

**Files:** New `scripts/fetch_live_captions.py`, `scripts/confirm_caption_whisper.py`,
modify `app/transcript_ingestor.py`.

---

### P2-8: Phrase Vocabulary Trend Tracking

**Fix:** `scripts/compute_phrase_trends.py` — for each (speaker, phrase), compute YES rate
over rolling 30/60/90-day windows. Flag `VOCAB_TRENDING_UP` / `VOCAB_TRENDING_DOWN` when
30-day rate differs from 90-day rate by >15 percentage points.

Wire into scoring as a small multiplier (trending up: ×1.10, trending down: ×0.90).

---

## 🟢 P3 — Long-Term / Nice-to-Have

| Item | Notes |
|---|---|
| Mobile dashboard via Tailscale | 10-min setup, deferred |
| WebSocket real-time market streaming | Only if speed becomes bottleneck |
| Learned ensemble stacker | Need 300+ tracked outcomes first |
| Hawkes process for multi-mention count markets | Rare market type |
| Social media firehose | Research says disappoints for literal-mention; $5k/mo API |

---

## Implementation Order

```
WEEK 1 — Measure first, then fix signal quality

  Day 1:   P0-1  BSS vs market mid (backtest_outcomes.py + dashboard metric)
           — This gates all other work. If BSS < 0, scoring formula is broken first.

  Day 2:   P0-4  Walk-forward backtesting (proper temporal split in backtest loop)
           P1-5  Verify + fix global default base rate (30 min audit)

  Day 3:   P0-2  Signal double-counting (source_story_hash in signals.yaml + dampening)
           H6    Add source_event field to signals.yaml schema

  Day 4:   P0-3  Rolling window event-count fix (remaining_events_in_window)
           P1-6  Cooccurrence time decay (2 hours)

  Day 5:   P1-3  Speaker-stratified Platt calibration (pre_event_trump vs non-trump)


WEEK 2 — Fix phrase matching + signal hygiene

  Day 1:   P1-1  Negation detection in phrase_matcher (3 hours)
  Day 2:   P1-2  Unicode normalization in phrase_matcher (1 hour)
           P1-8  Resolution rules parsing audit (exact match verification)

  Day 3:   P1-7  News deduplication entity fingerprint + per-signal news age decay
  Day 4:   P1-4  LLM domain-selective skip (NBA + earnings hard skip)
           P1-9  Polymarket resolution scope detection

  Day 5:   P1-10 Dashboard Performance tab (BSS/WR/PnL/Brier rolling metrics)


WEEK 3 — Model architecture improvements

  Day 1-2: P2-1  Log-odds signal fusion (scoring formula rewrite)
  Day 3:   P2-3  Per-signal decay half-lives
  Day 4-5: P2-4  Corpus timestamp estimation (word-position annotation)


WEEK 4+ — Advanced features (when P0-P1 are stable)

  P2-2  Survival / hazard intra-event model (needs timestamped corpus from week 3)
  P2-5  Reliability diagrams + calibration dashboard tab
  P2-6  Corpus expansion from outcome data
  P2-8  Phrase vocabulary trend tracking
  P2-7  Dual-source ASR (only if live monitoring is active)
```

---

## Success Metrics

The system is improving if and only if:

| Metric | Current | Target | How to Measure |
|---|---|---|---|
| **BSS vs market mid (30-day)** | Never measured | > 0.05 BUY_YES, > 0.02 BUY_NO | `backtest_outcomes.py` after P0-1 |
| Walk-forward Brier | Unknown | < 0.20 (better than 0.2178 current) | `backtest_outcomes.py` after P0-4 |
| BUY_YES win rate (30-day) | ~42% | > 55% | `post_event_summary.py` |
| BUY_NO win rate (30-day) | ~27% | > 40% | `post_event_summary.py` |
| False BUY_NO on monthly windows | High | < 5% of BUY_NO cards | Gate hit tracking |
| Negation false-positives (live) | Unknown | < 2% of CAPTION_DETECTED | Manual review |
| LLM API cost/day | ~$0.50 | < $0.30 (after P1-4 skip) | OpenAI billing |
| Cooccurrence co-sigs per event | Untracked | Track % dampened by P0-2 | DOUBLE_COUNT_DAMPENED rate |

---

## Critical: Do This Before Any Other Work

> From the research: *"The market mid is the benchmark every other model must beat.
> If your model doesn't improve on 'always predict the market mid,' it has negative value."*

**P0-1 (BSS vs market mid) must be the first thing built.** If BSS is negative, the
current multiplicative scoring formula is making things worse and needs to be replaced
(P2-1 log-odds fusion moves up to P0). Every other improvement is premature optimization
until we have a positive BSS number.

The data is already in `outcome_reviews` (`yes_ask`, `no_ask`, `p_literal`, `outcome`).
This is a ~4 hour implementation with immediate, game-changing feedback.
```

---

## What V3 Adds vs V2

| Area | V2 | V3 Addition |
|---|---|---|
| Code specificity | General descriptions | Exact file + line references for every issue |
| Double-counting | Identified | Specific fix with `source_story_hash` schema change |
| Rolling window | Identified | Exact bug traced to `frac_remaining` = time not events |
| Calibration | Not in V2 | Speaker-stratified Platt (trump vs non-trump strata) |
| Negation | Identified | Exact `MatchResult` dataclass + `NEGATION_WINDOW` implementation |
| Cooccurrence | Not in V2 | Time decay with `COOCCUR_HALFLIFE_DAYS=120` |
| Global default | Not in V2 | Verify code vs session 25 fix discrepancy |
| WH keyword boost | Not in V2 | Identified as specific double-count with additive formula |
| Implementation order | Week-level | Day-level with explicit gates between items |
| BSS gating | Mentioned last | P0-1 — must be first, gates all other work |
