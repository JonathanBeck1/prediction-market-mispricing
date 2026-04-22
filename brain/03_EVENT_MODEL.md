# Event Model

Manual-only invariant: event state controls scoring only; it never triggers order execution.

## Core Entities

- `event_id`: stable identifier, format `{speaker}:{date}:{type}-{seq}` (e.g., `trump:2026-03-05:rally-01`).
- `speaker`: normalized key (for example `trump`, `leavitt`, `mamdani`, `sanders`, `fed`, `nba`, or generic `auto` fallback).
- `event_type`: one of `rally`, `briefing`, `interview`, `townhall`, `other`.
- `speech_state`: one of `scheduled`, `live`, `ended`, `unknown`.
- `scheduled_start_ts`: expected start time (ISO-8601 UTC).
- `expected_duration_sec`: expected length in seconds (default 5400 = 90 min).
- `event_start_ts`: actual observed start (set when state -> `live`).
- `event_end_ts`: actual observed end (set when state -> `ended`).
- `last_transcript_ts`: most recent transcript ingest for this event.

## Speech States

### `scheduled`
- Event is on the calendar but hasn't started yet.
- **This is when pre-event trading happens.** Scoring uses base rates + external signals.
- No transcripts expected yet.
- Transitions to `live` when transcripts start arriving, or when `now >= scheduled_start_ts`.

### `live`
- Active transcript updates indicate ongoing remarks.
- New text growth detected within freshness threshold.
- **Time-remaining decay applies.** `p_literal` drops as time passes without phrase hits.
- This is the window for real-time edge: phrase detection, NO-side alerts.

### `ended`
- Explicit end cue, or sustained inactivity beyond end timeout after prior `live` state.
- No further phrase opportunity.
- `p_literal` drops to near-zero for unresolved phrases.
- Markets should converge to resolution price.

### `unknown`
- Default when event certainty is low, signal is stale, or source lost during live.
- Conservative scoring: reduced confidence, no strong signals emitted.

## State Transitions

```
scheduled ──→ live ──→ ended
                \──→ unknown ──→ live (if signal recovers)
                                  \──→ ended
```

Transition triggers:

| From | To | Trigger |
|---|---|---|
| `scheduled` | `live` | Fresh transcripts arrive, OR `now >= scheduled_start_ts` |
| `live` | `ended` | Inactivity > `ended_inactive_sec` after being live, OR explicit end signal |
| `live` | `unknown` | Source instability without enough evidence to call ended |
| `unknown` | `live` | Fresh transcripts resume |
| `unknown` | `ended` | Inactivity > `ended_inactive_sec` with no recovery |
| `ended` | (terminal) | No further transitions. New speech = new event_id. |

## Detection Thresholds (Configurable)

- `live_freshness_sec`: 90 -- transcript must be this fresh to count as live
- `ended_inactive_sec`: 600 -- silence this long after live = ended
- `unknown_stale_sec`: 1800 -- no data this long = unknown
- `pre_event_window_sec`: 3600 -- start emitting pre-event cards this many seconds before scheduled start

## Event Types and Their Significance

Event type affects base rate probabilities. Different formats have very different phrase distributions:

| Type | Description | Typical duration | Phrase predictability |
|---|---|---|---|
| `rally` | Campaign/political rally | 60-120 min | High for catchphrases, medium for policy |
| `briefing` | Press briefing (Leavitt) | 30-60 min | High for policy topics, low for unusual phrases |
| `interview` | Media interview | 15-45 min | Low -- driven by interviewer questions |
| `townhall` | Q&A format | 60-90 min | Medium -- mix of prepared + spontaneous |
| `other` | Anything else | Varies | Use conservative baseline |

## Event Identity Rules

- Same speaker can have multiple events per day (different `seq` numbers).
- Events are created manually via config/CLI for v1, schedule feed later.
- Transcript chunks are assigned to the nearest active event window by speaker.
- Kalshi event identity is not always "one real-world appearance = one Kalshi event". Some series are rolling windows keyed to a future close date rather than a same-day speech.
- Example: Leavitt press-briefing markets may live under `KXSECPRESSMENTION-<month_end>` even when the actual briefing is on an earlier day. The scorer should still treat those markets as active briefing-related opportunities.

## Data Contract for Scoring

Scorer consumes per market:

| Field | Type | Notes |
|---|---|---|
| `event_id` | string or null | null if no event scheduled |
| `speaker` | string | normalized key |
| `event_type` | string | affects base rate lookup |
| `speech_state` | string | controls scoring mode |
| `scheduled_start_ts` | ISO string or null | for pre-event timing |
| `time_remaining_sec` | float or null | estimated, null when unknown |
| `elapsed_sec` | float or null | time since event started |
| `expected_duration_sec` | int | for decay calculation |

Scoring behavior by state:

| State | p_literal (no hit) | p_literal (hit) | Edge type |
|---|---|---|---|
| `scheduled` | base_rate * news * buzz | n/a | Pre-event positioning |
| `live`, early | base_rate * slight_decay * news * buzz | 0.98 | Confirmation or entry |
| `live`, late | base_rate * heavy_decay * news * buzz (near 0) | 0.98 | Strong NO edge |
| `ended` | 0.02 | 0.98 | Settlement convergence |
| `unknown` | 0.15 (conservative) | 0.98 | Reduced confidence |
| no event | 0.10 * news * buzz | 0.98 | Only if massive mispricing |
