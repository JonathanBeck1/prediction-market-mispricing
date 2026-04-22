# Alert Specification

Manual-only invariant: alerts never trigger trade execution.

## Action Card Schema (Canonical)

```json
{
  "ts": "ISO-8601 UTC",
  "market_id": "string",
  "subject": "trump|leavitt|mamdani",
  "phrase": "string (resolution phrase for this market)",

  "event_id": "string|null",
  "event_type": "rally|briefing|interview|townhall|other|null",
  "speech_state": "scheduled|live|ended|unknown|null",
  "time_remaining_sec": "float|null",

  "side": "BUY_YES|BUY_NO|WATCH",
  "exec_price_hint": "string (e.g. 'Buy YES at <= 0.47')",
  "size_cap": "float (max position from depth)",

  "scores": {
    "p_literal": "float (composite probability)",
    "base_rate": "float|null",
    "time_decay": "float|null (1.0 if pre-event)",
    "news_pressure": "float (modifier, default 1.0)",
    "x_buzz": "float (modifier, default 1.0)",
    "ev_yes": "float",
    "ev_no": "float"
  },

  "liquidity": {
    "yes_ask": "float",
    "no_ask": "float",
    "spread": "float",
    "depth_yes": "float",
    "depth_no": "float",
    "spread_ok": "bool",
    "depth_ok": "bool",
    "gate_pass": "bool"
  },

  "evidence": {
    "transcript_id": "integer|null",
    "source": "string|null",
    "snippet": "string|null",
    "hit_count_today": "integer"
  },

  "reason_codes": ["string"],
  "rationale": "string (human-readable summary)"
}
```

## Reason Codes

### Phrase / hit status
- `PHRASE_HIT` -- resolution phrase was detected in transcript
- `NO_PHRASE_HIT` -- no hit for this market's resolution phrases

### Event state
- `PRE_EVENT` -- event is scheduled, not yet started
- `EVENT_LIVE` -- speech is in progress
- `EVENT_ENDING_SOON` -- event is >75% through duration
- `EVENT_ENDED` -- speech is over
- `EVENT_UNKNOWN` -- signal lost or uncertain
- `NO_EVENT` -- no event scheduled for this speaker

### Signal modifiers
- `HIGH_BASE_RATE` -- base rate > 0.70 for this speaker/event/phrase
- `LOW_BASE_RATE` -- base rate < 0.20
- `NEWS_PRESSURE_HIGH` -- news_pressure > 1.2
- `NEWS_PRESSURE_LOW` -- news_pressure < 0.8
- `X_BUZZ_HIGH` -- x_buzz > 1.2
- `X_BUZZ_LOW` -- x_buzz < 0.8

### Liquidity
- `GATE_PASS` -- spread and depth gates both pass
- `GATE_FAIL` -- one or both gates fail
- `SPREAD_TOO_WIDE` -- spread exceeds max
- `DEPTH_TOO_THIN` -- depth below min

### Data quality
- `DATA_STALE` -- market snapshot is older than threshold
- `FALLBACK_SOURCE_USED` -- transcript came from fallback source

## Pre-Event Card Example

```
BUY_YES | MKT-TRUMP-004 (tariffs)
Event: Rally scheduled 7:00 PM (in 3h 20m)
Base rate: 0.85 (rally + tariff) | News: 1.2 | X: 1.4
Composite p: 0.85 * 1.2 * 1.4 = 0.98 (capped)
Price: YES at <= 0.65 | Cap: $500
EV: +0.33
Reasons: PRE_EVENT, HIGH_BASE_RATE, NEWS_PRESSURE_HIGH, X_BUZZ_HIGH
```

## Live Event Card Example (NO edge)

```
BUY_NO | MKT-TRUMP-001 (NATO)
Event: Rally LIVE (72 min elapsed, ~18 min remaining)
Base rate: 0.40 | Decay: 0.36 | p_literal: 0.14
Price: NO at <= 0.30 | Cap: $451
EV: +0.56
Reasons: EVENT_LIVE, EVENT_ENDING_SOON, NO_PHRASE_HIT
```

## Phrase Hit Confirmation Card Example

```
BUY_YES | MKT-TRUMP-004 (tariffs)
Event: Rally LIVE (23 min elapsed)
PHRASE HIT: "...and we're going to put tariffs on every single..."
Price: YES at <= 0.72 | Cap: $484
EV: +0.26
Reasons: PHRASE_HIT, EVENT_LIVE, GATE_PASS
```

## Delivery Channels

- Console stream (always on)
- SQLite `action_cards` table (always on)
- `data/action_cards.jsonl` (always on)
- WhatsApp via OpenClaw (integration phase, after local testing)

## Material-Change Throttling

Only emit a notification-worthy card when:
- New phrase hit detected (side may flip)
- Side changes (WATCH -> BUY_YES, BUY_YES -> BUY_NO, etc.)
- EV crosses threshold (was below 0.03, now above, or vice versa)
- Event state changes (scheduled -> live, live -> ended)
- Market enters or exits top-N ranking

Full card set still written to DB/JSONL every cycle for audit. Throttling only affects console output and future WhatsApp notifications.

Per-market cooldown: minimum 120 seconds between repeated notifications for the same market unless a material change occurs.
